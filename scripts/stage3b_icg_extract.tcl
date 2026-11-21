# Read-only extractor for direct feedback muxes and mapped enable flops.
foreach required {P3B_INPUT_ODB P3B_LIBS P3B_CANDIDATES} {
  if {![info exists ::env($required)] || $::env($required) eq ""} { error "$required is required" }
}
foreach liberty [split $::env(P3B_LIBS) "\n"] { if {$liberty ne ""} { read_liberty $liberty } }
read_db $::env(P3B_INPUT_ODB)
set block [ord::get_db_block]
set dbu [$block getDbUnitsPerMicron]
set out [open $::env(P3B_CANDIDATES) w]
puts $out "flop\tflop_master\tkind\tclock_net\tmux\tmux_master\tfeedback_pin\tdata_pin\tenable_net\tdata_net\tclock_x_dbu\tclock_y_dbu\tstatus\treason"

proc p3bx_emit {fields} {
  global out
  set normalized [lrange [concat $fields [lrepeat 14 ""]] 0 13]
  puts $out [join $normalized "\t"]
}
proc p3bx_net {inst pin} {
  set iterm [$inst findITerm $pin]
  if {$iterm eq "NULL"} { return "NULL" }
  return [$iterm getNet]
}
proc p3bx_drivers {net} {
  set result {}
  if {$net eq "NULL"} { return $result }
  foreach iterm [$net getITerms] {
    if {[[$iterm getMTerm] getIoType] eq "OUTPUT"} { lappend result $iterm }
  }
  return $result
}
proc p3bx_sinks {net} {
  set result {}
  if {$net eq "NULL"} { return $result }
  foreach iterm [$net getITerms] {
    if {[[$iterm getMTerm] getIoType] eq "INPUT"} { lappend result $iterm }
  }
  return $result
}

set total_seq 0
set direct_d_mux 0
set eligible 0
set reasons [dict create]
foreach flop [$block getInsts] {
  set master [$flop getMaster]
  if {![$master isSequential]} { continue }
  incr total_seq
  set fname [$flop getName]
  set fmname [$master getName]
  set reason ""
  set is_enabled_ff [regexp {sky130_fd_sc_hd__edfxtp_[0-9]+$} $fmname]
  if {!$is_enabled_ff && ![string match "sky130_fd_sc_hd__dfxtp_*" $fmname]} {
    set reason "unsupported_or_scan_master"
  } elseif {[regexp -nocase {(sync|synchron|reset|rst|scan|test|mbist|jtag|macro)} $fname]} {
    set reason "protected_instance_name"
  } elseif {[$master isBlock]} {
    set reason "hard_macro"
  }
  if {$reason ne ""} {
    dict incr reasons $reason
    p3bx_emit [list $fname $fmname "" "" "" "" "" "" "" "" "" "" REJECT $reason]
    continue
  }
  set pins {}
  foreach iterm [$flop getITerms] { lappend pins [[$iterm getMTerm] getName] }
  if {[lsearch -exact $pins CLK] < 0 || [lsearch -exact $pins D] < 0 || [lsearch -exact $pins Q] < 0} {
    dict incr reasons missing_core_pins
    p3bx_emit [list $fname $fmname "" "" "" "" "" "" "" "" "" "" REJECT missing_core_pins]
    continue
  }
  set protected 0
  foreach pin $pins {
    if {[regexp -nocase {^(SCE|SE|SI|SCAN|TE|TEST|RESET|RESET_B|SET|SET_B|RST|RST_B|CLR|PRE|PRE_B)} $pin]} { set protected 1 }
  }
  if {$protected} {
    dict incr reasons scan_or_reset_pin
    p3bx_emit [list $fname $fmname "" "" "" "" "" "" "" "" "" "" REJECT scan_or_reset_pin]
    continue
  }
  set qnet [p3bx_net $flop Q]
  set dnet [p3bx_net $flop D]
  set clkn [p3bx_net $flop CLK]
  if {$qnet eq "NULL" || $dnet eq "NULL" || $clkn eq "NULL"} {
    dict incr reasons missing_connections
    p3bx_emit [list $fname $fmname "" "" "" "" "" "" "" "" "" "" REJECT missing_connections]
    continue
  }
  lassign [[$flop findITerm CLK] getAvgXY] valid x y
  if {!$valid} {
    dict incr reasons no_clock_pin_location
    p3bx_emit [list $fname $fmname "" "" "" "" "" "" "" "" "" "" REJECT no_clock_pin_location]
    continue
  }
  if {$is_enabled_ff} {
    set enable_net [p3bx_net $flop DE]
    if {$enable_net eq "NULL"} {
      dict incr reasons missing_enable
      p3bx_emit [list $fname $fmname enable_flop [$clkn getName] "" "" "" "" "" "" $x $y REJECT missing_enable]
      continue
    }
    incr eligible
    p3bx_emit [list $fname $fmname enable_flop [$clkn getName] "" "" "" "" [$enable_net getName] "" $x $y CANDIDATE shared_integrated_enable_pin]
    continue
  }

  set ddrivers [p3bx_drivers $dnet]
  if {[llength $ddrivers] != 1} {
    dict incr reasons d_not_single_driven
    p3bx_emit [list $fname $fmname feedback_mux [$clkn getName] "" "" "" "" "" "" $x $y REJECT d_not_single_driven]
    continue
  }
  set mux_iterm [lindex $ddrivers 0]
  set mux [$mux_iterm getInst]
  set mmname [[$mux getMaster] getName]
  if {![regexp {sky130_fd_sc_hd__mux2_[0-9]+$} $mmname]} {
    dict incr reasons d_not_mux2
    p3bx_emit [list $fname $fmname feedback_mux [$clkn getName] [$mux getName] $mmname "" "" "" "" $x $y REJECT d_not_mux2]
    continue
  }
  incr direct_d_mux
  set a0 [p3bx_net $mux A0]
  set a1 [p3bx_net $mux A1]
  set select [p3bx_net $mux S]
  set output [p3bx_net $mux X]
  set feedback_pin ""; set data_pin ""
  if {$a0 eq $qnet && $a1 ne "NULL" && $a1 ne $qnet} {
    set feedback_pin A0; set data_pin A1
  } elseif {$a1 eq $qnet && $a0 ne "NULL" && $a0 ne $qnet} {
    set feedback_pin A1; set data_pin A0
  } else {
    dict incr reasons no_direct_self_feedback
    p3bx_emit [list $fname $fmname feedback_mux [$clkn getName] [$mux getName] $mmname "" "" "" "" $x $y REJECT no_direct_self_feedback]
    continue
  }
  if {$output ne $dnet || [llength [p3bx_sinks $output]] != 1 || [$mux getPlacementStatus] in {FIXED FIRM COVER}} {
    dict incr reasons mux_output_not_exclusive
    p3bx_emit [list $fname $fmname feedback_mux [$clkn getName] [$mux getName] $mmname $feedback_pin $data_pin "" "" $x $y REJECT mux_output_not_exclusive]
    continue
  }
  if {$select eq "NULL" || $feedback_pin ne "A0"} {
    dict incr reasons inverted_enable_unsupported
    p3bx_emit [list $fname $fmname feedback_mux [$clkn getName] [$mux getName] $mmname $feedback_pin $data_pin "" "" $x $y REJECT inverted_enable_unsupported]
    continue
  }
  set data_net [p3bx_net $mux $data_pin]
  if {$data_net eq "NULL"} {
    dict incr reasons missing_data_net
    p3bx_emit [list $fname $fmname feedback_mux [$clkn getName] [$mux getName] $mmname $feedback_pin $data_pin [$select getName] "" $x $y REJECT missing_data_net]
    continue
  }
  incr eligible
  p3bx_emit [list $fname $fmname feedback_mux [$clkn getName] [$mux getName] $mmname $feedback_pin $data_pin [$select getName] [$data_net getName] $x $y CANDIDATE structural_match]
}
close $out
puts "P3B_EXTRACT sequential=$total_seq d_mux2=$direct_d_mux eligible=$eligible dbu_per_micron=$dbu reasons=$reasons output=$::env(P3B_CANDIDATES)"
exit
