# Check Liberty clock-gating setup/hold arcs on the CTS or routed checkpoint.
foreach required {P3B_INPUT_ODB P3B_SDC P3B_LIBS P3B_RC_TCL P3B_REPORT} {
  if {![info exists ::env($required)] || $::env($required) eq ""} { error "$required is required" }
}
foreach liberty [split $::env(P3B_LIBS) "\n"] { if {$liberty ne ""} { read_liberty $liberty } }
read_db $::env(P3B_INPUT_ODB)
read_sdc $::env(P3B_SDC)
source $::env(P3B_RC_TCL)
set_clock_gating_check -setup 0.0 -hold 0.0
sta::set_gated_clk_checks_enabled 1
set_thread_count 1
if {[info exists ::env(P3B_SPEF)] && $::env(P3B_SPEF) ne ""} {
  read_spef $::env(P3B_SPEF)
} else {
  estimate_parasitics -placement
}
set_propagated_clock [all_clocks]

set block [ord::get_db_block]
set gate_count 0
set gate_enable_pins {}
foreach inst [$block getInsts] {
  if {[regexp -nocase {__(dlclkp|sdlclkp)_[0-9]+$} [[$inst getMaster] getName]]} {
    incr gate_count
    lappend gate_enable_pins "[$inst getName]/GATE"
  }
}
set report [open $::env(P3B_REPORT) w]
puts $report "input_odb=$::env(P3B_INPUT_ODB)"
puts $report "input_sdc=$::env(P3B_SDC)"
puts $report "icg_count=$gate_count"
if {$gate_count == 0} {
  puts $report "status=SKIPPED_NO_ICG"
  close $report
  puts "P3B_POSTCTS_RESULT status=SKIPPED_NO_ICG icgs=0 setup_checks=0 hold_checks=0 violations=0"
  exit
}
foreach gate_pin $gate_enable_pins {
  # OpenSTA stores one active phase per gate pin, so setup and hold are
  # configured and reported in separate phases below.
}
set setup_summary ""
set hold_summary ""
set setup_violations ""
set hold_violations ""
with_output_to_variable all_setup_paths {report_checks -path_delay max -group_path_count 5 -format full_clock_expanded}
with_output_to_variable all_hold_paths {report_checks -path_delay min -group_path_count 5 -format full_clock_expanded}
puts $report "--- top_setup_paths ---"
puts $report $all_setup_paths
puts $report "--- top_hold_paths ---"
puts $report $all_hold_paths
puts $report "clock_objects=[all_clocks]"
set clock_pin_names {}
foreach clock_pin [all_registers -clock_pins] { lappend clock_pin_names [get_full_name $clock_pin] }
set core_clock_pin_names {}
foreach clock_pin [all_registers -clock_pins -clock core_clock] { lappend core_clock_pin_names [get_full_name $clock_pin] }
puts $report "clock_pin_names=$clock_pin_names"
puts $report "core_clock_pin_names=$core_clock_pin_names"
foreach gate_pin $gate_enable_pins {
  puts $report "gate_enable_pin=$gate_pin objects=[get_pins $gate_pin]"
  set gate_inst [$block findInst [lindex [split $gate_pin /] 0]]
  foreach iterm [$gate_inst getITerms] {
    set iterm_pin [[$iterm getMTerm] getName]
    set iterm_net [$iterm getNet]
    set iterm_net_name "NULL"
    if {$iterm_net ne "NULL"} { set iterm_net_name [$iterm_net getName] }
    puts $report "odb_pin=$iterm_pin net=$iterm_net_name"
  }
  with_output_to_variable gate_pin_setup {report_checks -to [get_pins $gate_pin] -path_delay max -format full}
  with_output_to_variable gate_pin_hold {report_checks -to [get_pins $gate_pin] -path_delay min -format full}
  puts $report "--- gate_pin_setup_path ---"
  puts $report $gate_pin_setup
  puts $report "--- gate_pin_hold_path ---"
  puts $report $gate_pin_hold
}
foreach gate_pin $gate_enable_pins {
  # The 0.5 ns setup margin exceeds the largest positive Sky130HD dlclkp setup
  # constraint in the supplied Liberty tables.
  set_clock_gating_check -setup 0.5 -high [get_pins $gate_pin]
}
if {[catch {with_output_to_variable setup_summary {report_check_types -clock_gating_setup -verbose}} err]} {
  close $report
  error "clock-gating setup report failed: $err"
}
if {[catch {with_output_to_variable setup_violations {report_check_types -clock_gating_setup -violators -verbose}} err]} {
  close $report
  error "clock-gating setup violation report failed: $err"
}
foreach gate_pin $gate_enable_pins {
  # For a latch_posedge ICG the latch closes at the rising edge. OpenSTA's
  # generic -high hold check is placed at the next falling edge, so subtract
  # the high phase duration from its margin to move the required edge back to
  # the closing edge while retaining the 0.5 ns hold window.
  set physical_clocks {}
  foreach clock [all_clocks] {
    if {![get_property $clock is_virtual]} { lappend physical_clocks $clock }
  }
  if {[llength $physical_clocks] != 1} {
    close $report
    error "Phase 3B timing audit currently requires exactly one physical clock domain"
  }
  set period [get_property [lindex $physical_clocks 0] period]
  set hold_margin [expr {0.5 - $period / 2.0}]
  puts $report "clock_period_ns=$period latch_capture_hold_margin_ns=0.5 sdc_hold_margin_ns=$hold_margin"
  set_clock_gating_check -hold $hold_margin -high [get_pins $gate_pin]
}
if {[catch {with_output_to_variable hold_summary {report_check_types -clock_gating_hold -verbose}} err]} {
  close $report
  error "clock-gating hold report failed: $err"
}
if {[catch {with_output_to_variable hold_violations {report_check_types -clock_gating_hold -violators -verbose}} err]} {
  close $report
  error "clock-gating hold violation report failed: $err"
}
puts $report "--- clock_gating_setup ---"
puts $report $setup_summary
puts $report "--- clock_gating_hold ---"
puts $report $hold_summary
puts $report "--- clock_gating_setup_violators ---"
puts $report $setup_violations
puts $report "--- clock_gating_hold_violators ---"
puts $report $hold_violations

set setup_missing [regexp -nocase {no paths found|no checks} $setup_summary]
set hold_missing [regexp -nocase {no paths found|no checks} $hold_summary]
set setup_bad [expr {![regexp -nocase {no paths found|no checks} $setup_violations]}]
set hold_bad [expr {![regexp -nocase {no paths found|no checks} $hold_violations]}]
if {$setup_missing || $hold_missing} {
  puts $report "status=FAIL_MISSING_CLOCK_GATING_CHECKS"
  close $report
  puts "P3B_POSTCTS_RESULT status=FAIL_MISSING_CHECKS icgs=$gate_count setup_checks=[expr {!$setup_missing}] hold_checks=[expr {!$hold_missing}] violations=unknown"
  error "ICG exists but OpenSTA did not report both Liberty clock-gating setup and hold checks"
}
if {$setup_bad || $hold_bad} {
  puts $report "status=FAIL_CLOCK_GATING_VIOLATION"
  close $report
  puts "P3B_POSTCTS_RESULT status=FAIL_VIOLATION icgs=$gate_count setup_checks=1 hold_checks=1 violations=present"
  error "clock-gating setup/hold violation(s) found"
}
puts $report "status=PASS"
close $report
puts "P3B_POSTCTS_RESULT status=PASS icgs=$gate_count setup_checks=1 hold_checks=1 violations=0"
exit
