# Slack-budgeted in-place ASAP7 threshold-voltage swaps.
# Same-footprint/function families are pre-screened by cl_ovps_pass3a_multivt.py.

foreach required {PASS3A_INPUT_ODB PASS3A_SDC PASS3A_LIBS PASS3A_RC_TCL PASS3A_PAIRS PASS3A_REPORT PASS3A_OUTPUT_ODB PASS3A_SETUP_MARGIN_PS PASS3A_TIMING_GUARD_PS PASS3A_MAX_TRIALS} {
  if {![info exists ::env($required)] || $::env($required) eq ""} { error "$required is required" }
}
foreach liberty [split $::env(PASS3A_LIBS) "\n"] { if {$liberty ne ""} { read_liberty $liberty } }
read_db $::env(PASS3A_INPUT_ODB)
read_sdc $::env(PASS3A_SDC)
source $::env(PASS3A_RC_TCL)
if {[info exists ::env(PASS3A_UPF)] && $::env(PASS3A_UPF) ne ""} {
  set project_root [file normalize [file join [file dirname [info script]] ..]]
  source [file join $project_root scripts cl_ovps_load_upf.tcl]
  cl_ovps_load_upf $::env(PASS3A_UPF)
}
if {[info exists ::env(PASS3A_SPEF)] && $::env(PASS3A_SPEF) ne ""} {
  read_spef $::env(PASS3A_SPEF)
  set use_extracted_parasitics 1
} else {
  estimate_parasitics -placement
  set use_extracted_parasitics 0
}
set_thread_count 1

if {[info exists ::env(PASS3A_VCD)] && $::env(PASS3A_VCD) ne ""} {
  if {![info exists ::env(PASS3A_SCOPE)] || $::env(PASS3A_SCOPE) eq ""} { error "PASS3A_SCOPE is required with PASS3A_VCD" }
  read_vcd -scope $::env(PASS3A_SCOPE) $::env(PASS3A_VCD)
  puts "PASS3A_ACTIVITY source=vcd scope=$::env(PASS3A_SCOPE)"
} else {
  set activity 0.1
  if {[info exists ::env(PASS3A_ACTIVITY)]} { set activity $::env(PASS3A_ACTIVITY) }
  set_power_activity -global -activity $activity -duty 0.5
  puts "PASS3A_ACTIVITY source=global_default activity=$activity duty=0.5"
}

set block [ord::get_db_block]
set db [ord::get_db]
proc p3a_slacks {} {
  set setup [sta::worst_slack_cmd max]
  set hold [sta::worst_slack_cmd min]
  set tns [sta::total_negative_slack_cmd max]
  return [list $setup $hold $tns]
}
proc p3a_power {} {
  set values [sta::design_power [sta::cmd_scene]]
  if {[llength $values] < 4} { error "OpenSTA did not return internal/switching/leakage/total power" }
  return $values
}
proc p3a_score {power} { return [expr {[lindex $power 0] + [lindex $power 2]}] }
proc p3a_finite {value} {
  if {$value eq ""} { return 0 }
  set normalized [string tolower $value]
  return [expr {$normalized ni {inf +inf -inf nan} && [string is double -strict $value]}]
}
proc p3a_cell_setup_slack {inst_name} {
  set sta_cell [get_cells -quiet $inst_name]
  if {[llength $sta_cell] != 1} { return "" }
  set values {}
  foreach pin [get_pins -quiet -of_objects $sta_cell -filter "direction==input || direction==output"] {
    set lib_pin [get_property -quiet $pin lib_pin_name]
    if {[string match -nocase {clk*} $lib_pin] || [string match -nocase {ck*} $lib_pin]} { continue }
    if {![catch {set slack [get_property -quiet $pin slack_max]}] && [p3a_finite $slack]} {
      # get_property returns Liberty/UI units (ps in ASAP7), while WNS and
      # swap margins are in SI seconds. Convert UI -> STA seconds.
      lappend values [sta::time_ui_sta $slack]
    }
  }
  if {![llength $values]} { return "" }
  return [lindex [lsort -real $values] 0]
}
proc p3a_bad_slew_cap {} {
  if {[catch {with_output_to_variable report {
      report_check_types -max_slew -max_capacitance -violators -format slack_only
    }}]} { return "CHECK_COMMAND_FAILED" }
  set report [string trim $report]
  if {$report eq "" || [regexp -nocase {no paths found|0 violating paths|no violations} $report]} { return "" }
  return $report
}
proc p3a_rank {tag} {
  switch -- $tag { SL {return 0} L {return 1} R {return 2} default {return -1} }
}
proc p3a_candidate_compare {left right} {
  global candidate_direction
  set ls [lindex $left 0]; set rs [lindex $right 0]
  if {$ls < $rs} { return [expr {$candidate_direction eq "down" ? 1 : -1}] }
  if {$ls > $rs} { return [expr {$candidate_direction eq "down" ? -1 : 1}] }
  return [string compare [lindex $left 1] [lindex $right 1]]
}

set pair_file [open $::env(PASS3A_PAIRS) r]
gets $pair_file
set pairs_by_master [dict create]
while {[gets $pair_file line] >= 0} {
  if {[string trim $line] eq ""} { continue }
  set fields [split $line "\t"]
  if {[llength $fields] < 5} { close $pair_file; error "malformed replacement pair: $line" }
  lassign $fields family source_vt target_vt source target
  dict lappend pairs_by_master $source [list $family $source_vt $target_vt $target]
}
close $pair_file

lassign [p3a_slacks] baseline_setup baseline_hold baseline_tns
if {$baseline_setup eq "" || $baseline_hold eq ""} { error "Pass 3A could not determine baseline WNS" }
set baseline_power [p3a_power]
set baseline_checks [p3a_bad_slew_cap]
puts "PASS3A_START setup_s=$baseline_setup hold_s=$baseline_hold tns_s=$baseline_tns internal_w=[lindex $baseline_power 0] switching_w=[lindex $baseline_power 1] leakage_w=[lindex $baseline_power 2]"

set candidates {}
set scanned 0
set instance_slack_file [open [file join [file dirname $::env(PASS3A_REPORT)] stage3a_instance_slack.tsv] w]
puts $instance_slack_file "instance\tmaster\tsetup_slack_s\tsetup_wns_s\tsetup_tns_s"
foreach inst [$block getInsts] {
  set master [[$inst getMaster] getName]
  set name [$inst getName]
  set slack [p3a_cell_setup_slack $name]
  if {$slack eq ""} { continue }
  puts $instance_slack_file "$name\t$master\t$slack\t$baseline_setup\t$baseline_tns"
  if {![dict exists $pairs_by_master $master]} { continue }
  foreach pair [dict get $pairs_by_master $master] {
    lassign $pair family source_vt target_vt target
    set direction [expr {[p3a_rank $target_vt] > [p3a_rank $source_vt] ? "down" : "up"}]
    if {$baseline_setup >= 0.0 && $direction ne "down"} { continue }
    if {$baseline_setup < 0.0 && ($direction ne "up" || $slack >= 0.0)} { continue }
    lappend candidates [list $slack $name $master $family $source_vt $target_vt $target $direction]
    incr scanned
  }
}
close $instance_slack_file
if {$baseline_setup >= 0.0} { set candidate_direction down } else { set candidate_direction up }
set candidates [lsort -command p3a_candidate_compare $candidates]
set report [open $::env(PASS3A_REPORT) w]
puts $report "instance\tfamily\tfrom_vt\tto_vt\tfrom_master\tto_master\tslack_before_s\tsetup_before_s\tsetup_after_s\ttns_before_s\ttns_after_s\thold_before_s\thold_after_s\tinternal_before_w\tinternal_after_w\tleakage_before_w\tleakage_after_w\tswitching_before_w\tswitching_after_w\tdecision\treason"
set accepted 0
set rejected 0
set tried 0
set current_setup $baseline_setup
set current_hold $baseline_hold
set current_tns $baseline_tns
set current_power $baseline_power
set current_checks $baseline_checks
set guard [expr {$::env(PASS3A_TIMING_GUARD_PS) * 1.0e-12}]
set margin [expr {$::env(PASS3A_SETUP_MARGIN_PS) * 1.0e-12}]
foreach candidate $candidates {
  if {$tried >= $::env(PASS3A_MAX_TRIALS)} { break }
  lassign $candidate initial_slack name source family source_vt target_vt target direction
  set inst [$block findInst $name]
  if {$inst eq "NULL" || [[$inst getMaster] getName] ne $source} { continue }
  set slack [p3a_cell_setup_slack $name]
  if {$slack eq ""} { continue }
  if {$current_setup >= 0.0} {
    if {$direction ne "down" || $slack <= $margin} { continue }
  } elseif {$direction ne "up" || $slack >= 0.0} { continue }
  incr tried
  set location_before [$inst getLocation]
  set status_before [$inst getPlacementStatus]
  set source_master [$inst getMaster]
  set target_master [$db findMaster $target]
  if {$target_master eq "NULL"} {
    puts $report "$name\t$family\t$source_vt\t$target_vt\t$source\t$target\t$slack\t$current_setup\t\t$current_tns\t\t$current_hold\t\t[lindex $current_power 0]\t\t[lindex $current_power 2]\t\t[lindex $current_power 1]\t\tREJECT\tTARGET_MASTER_NOT_IN_ODB"
    incr rejected
    puts "PASS3A_REJECT instance=$name reason=TARGET_MASTER_NOT_IN_ODB"
    continue
  }
  if {[$source_master getWidth] != [$target_master getWidth] || [$source_master getHeight] != [$target_master getHeight]} {
    puts $report "$name\t$family\t$source_vt\t$target_vt\t$source\t$target\t$slack\t$current_setup\t\t$current_tns\t\t$current_hold\t\t[lindex $current_power 0]\t\t[lindex $current_power 2]\t\t[lindex $current_power 1]\t\tREJECT\tODB_MASTER_DIMENSION_MISMATCH"
    incr rejected
    puts "PASS3A_REJECT instance=$name reason=ODB_MASTER_DIMENSION_MISMATCH"
    continue
  }
  if {$current_checks ne ""} {
    puts $report "$name\t$family\t$source_vt\t$target_vt\t$source\t$target\t$slack\t$current_setup\t\t$current_tns\t\t$current_hold\t\t[lindex $current_power 0]\t\t[lindex $current_power 2]\t\t[lindex $current_power 1]\t\tREJECT\tBASELINE_MAX_SLEW_OR_CAP_VIOLATION"
    incr rejected
    puts "PASS3A_REJECT instance=$name reason=BASELINE_MAX_SLEW_OR_CAP_VIOLATION"
    continue
  }
  if {[catch {set swapped [$inst swapMaster $target_master]} swap_error] || !$swapped} {
    puts $report "$name\t$family\t$source_vt\t$target_vt\t$source\t$target\t$slack\t$current_setup\t\t$current_tns\t\t$current_hold\t\t[lindex $current_power 0]\t\t[lindex $current_power 2]\t\t[lindex $current_power 1]\t\tREJECT\tMASTER_SWAP_FAILED"
    incr rejected
    puts "PASS3A_REJECT instance=$name reason=MASTER_SWAP_FAILED"
    continue
  }
  if {[$inst getLocation] ne $location_before || [$inst getPlacementStatus] ne $status_before} {
    if {[catch {set restored [$inst swapMaster $source_master]}] || !$restored} { error "failed to restore placement-changed swap for $name" }
    error "same-footprint VT swap changed placement for $name"
  }
  if {!$use_extracted_parasitics} { estimate_parasitics -placement }
  lassign [p3a_slacks] trial_setup trial_hold trial_tns
  set trial_power [p3a_power]
  set trial_checks [p3a_bad_slew_cap]
  set setup_loss [expr {$current_setup - $trial_setup}]
  set hold_loss [expr {$current_hold - $trial_hold}]
  set setup_ok 0
  if {$current_setup >= 0.0} {
    set setup_ok [expr {$trial_setup >= 0.0 && $setup_loss <= $guard}]
  } else {
    set setup_ok [expr {$direction eq "up" && $trial_setup > $current_setup && $setup_loss <= $guard}]
  }
  if {$current_hold >= 0.0} {
    set hold_ok [expr {$trial_hold >= 0.0 && $hold_loss <= $guard}]
  } else {
    set hold_ok [expr {$trial_hold >= $current_hold && $hold_loss <= $guard}]
  }
  set score_ok [expr {[p3a_score $trial_power] < [p3a_score $current_power] - 1.0e-18}]
  set dynamic_ok 1
  if {[info exists ::env(PASS3A_REQUIRE_DYNAMIC_NONINCREASING)] && $::env(PASS3A_REQUIRE_DYNAMIC_NONINCREASING)} {
    set trial_dynamic [expr {[lindex $trial_power 0] + [lindex $trial_power 1]}]
    set current_dynamic [expr {[lindex $current_power 0] + [lindex $current_power 1]}]
    set dynamic_ok [expr {$trial_dynamic <= $current_dynamic + 1.0e-18}]
  }
  set checks_ok [expr {$trial_checks eq ""}]
  if {$setup_ok && $hold_ok && $score_ok && $dynamic_ok && $checks_ok} {
    puts $report "$name\t$family\t$source_vt\t$target_vt\t$source\t$target\t$slack\t$current_setup\t$trial_setup\t$current_tns\t$trial_tns\t$current_hold\t$trial_hold\t[lindex $current_power 0]\t[lindex $trial_power 0]\t[lindex $current_power 2]\t[lindex $trial_power 2]\t[lindex $current_power 1]\t[lindex $trial_power 1]\tACCEPT\tPASS"
    puts "PASS3A_ACCEPT instance=$name from=$source to=$target setup_s=$trial_setup hold_s=$trial_hold tns_s=$trial_tns internal_delta_w=[expr {[lindex $trial_power 0]-[lindex $current_power 0]}] leakage_delta_w=[expr {[lindex $trial_power 2]-[lindex $current_power 2]}]"
    incr accepted
    set current_setup $trial_setup
    set current_hold $trial_hold
    set current_tns $trial_tns
    set current_power $trial_power
    set current_checks $trial_checks
  } else {
    if {[catch {set restored [$inst swapMaster $source_master]} restore_error] || !$restored} {
      error "Pass 3A failed to rollback rejected swap $name: $restore_error"
    }
    if {!$use_extracted_parasitics} { estimate_parasitics -placement }
    set reason ""
    if {!$setup_ok} { set reason SETUP_FLOOR_OR_50PS_GUARD
    } elseif {!$hold_ok} { set reason HOLD_FLOOR_OR_50PS_GUARD
    } elseif {!$score_ok} { set reason LEAKAGE_PLUS_INTERNAL_NOT_LOWER
    } elseif {!$dynamic_ok} { set reason VCD_DYNAMIC_POWER_INCREASE
    } elseif {!$checks_ok} { set reason MAX_SLEW_OR_CAP_CHECK }
    puts $report "$name\t$family\t$source_vt\t$target_vt\t$source\t$target\t$slack\t$current_setup\t$trial_setup\t$current_tns\t$trial_tns\t$current_hold\t$trial_hold\t[lindex $current_power 0]\t[lindex $trial_power 0]\t[lindex $current_power 2]\t[lindex $trial_power 2]\t[lindex $current_power 1]\t[lindex $trial_power 1]\tREJECT\t$reason"
    puts "PASS3A_REJECT instance=$name reason=$reason"
    incr rejected
  }
}
close $report
write_db $::env(PASS3A_OUTPUT_ODB)
puts "PASS3A_FINISH setup_s=$current_setup hold_s=$current_hold tns_s=[sta::total_negative_slack_cmd max] internal_w=[lindex $current_power 0] switching_w=[lindex $current_power 1] leakage_w=[lindex $current_power 2] swaps=$accepted"
puts "PASS3A_SUMMARY scanned=$scanned tried=$tried accepted=$accepted rejected=$rejected"
