# Apply a specified legal move to a fresh copy of the detailed-placement ODB.
# This is for calibration candidates, including moves that are not placement-Pareto.
set project_root [file normalize [file join [file dirname [info script]] ..]]
set result_dir [file join $project_root build orfs results sky130hd picorv32_baseline baseline]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
set placement_odb [file join $result_dir 3_5_place_dp.odb]
set placement_sdc [file join $result_dir 3_place.sdc]

foreach required_env {ACTIVITY_VCD MOVE_EVAL_DIR MOVE_SELECTED_INSTANCE MOVE_SELECTED_DX CANDIDATE_ODB MOVE_APPLY_SUMMARY} {
  if {![info exists ::env($required_env)]} {
    error "required environment variable $required_env is not set"
  }
}
if {[file normalize $::env(CANDIDATE_ODB)] eq [file normalize $placement_odb]} {
  error "candidate ODB must be separate from the baseline checkpoint"
}

proc read_move_row {path instance dx} {
  set input [open $path r]
  set header_line [gets $input]
  if {$header_line eq ""} {
    close $input
    error "move table is empty: $path"
  }
  set header [split $header_line "\t"]
  set columns [dict create]
  for {set i 0} {$i < [llength $header]} {incr i} {
    dict set columns [lindex $header $i] $i
  }
  foreach name {instance dx_dbu from_x from_y to_x to_y status timing_ok observed_delta_w} {
    if {![dict exists $columns $name]} {
      close $input
      error "move table $path lacks column $name"
    }
  }
  set found {}
  while {[gets $input line] >= 0} {
    if {$line eq ""} { continue }
    set fields [split $line "\t"]
    if {[lindex $fields [dict get $columns instance]] eq $instance &&
        [lindex $fields [dict get $columns dx_dbu]] eq $dx} {
      if {$found ne ""} {
        close $input
        error "multiple rows match $instance dx=$dx in $path"
      }
      set found [dict create]
      foreach name {instance dx_dbu from_x from_y to_x to_y status timing_ok observed_delta_w} {
        dict set found $name [lindex $fields [dict get $columns $name]]
      }
    }
  }
  close $input
  if {$found eq ""} { error "no row matches $instance dx=$dx in $path" }
  if {[dict get $found status] ne "evaluated" || [dict get $found timing_ok] ne "1"} {
    error "move $instance dx=$dx is not legal and timing-safe in $path"
  }
  return $found
}

set rows [dict create]
foreach workload {idle typical peak} {
  dict set rows $workload [read_move_row \
    [file join $::env(MOVE_EVAL_DIR) candidate_moves_${workload}.tsv] \
    $::env(MOVE_SELECTED_INSTANCE) $::env(MOVE_SELECTED_DX)]
}
set move [dict get $rows typical]
foreach workload {idle peak} {
  set other [dict get $rows $workload]
  foreach field {from_x from_y to_x to_y} {
    if {[dict get $other $field] ne [dict get $move $field]} {
      error "workload move tables disagree for $field"
    }
  }
}

read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $placement_odb
read_sdc $placement_sdc
source [file join $platform_dir setRC.tcl]
estimate_parasitics -placement
read_upf -file [file join $project_root designs picorv32 flow constraints picorv32.upf]
set_thread_count 1
read_vcd -scope tb_picorv32_activity/dut $::env(ACTIVITY_VCD)
check_placement -placeable

set inst_name [dict get $move instance]
set inst [[ord::get_db_block] findInst $inst_name]
if {$inst eq "NULL"} { error "selected instance $inst_name is absent from baseline ODB" }
lassign [$inst getLocation] actual_x actual_y
if {$actual_x != [dict get $move from_x] || $actual_y != [dict get $move from_y]} {
  error "stale move: $inst_name is at ($actual_x,$actual_y)"
}
set scene [sta::cmd_scene]
set base_power [lindex [sta::design_power $scene] 1]
set base_setup [expr {[sta::worst_slack_cmd max] * 1.0e9}]
set base_hold [expr {[sta::worst_slack_cmd min] * 1.0e9}]
$inst setLocation [dict get $move to_x] [dict get $move to_y]
estimate_parasitics -placement
check_placement -placeable
read_vcd -scope tb_picorv32_activity/dut $::env(ACTIVITY_VCD)
set candidate_power [lindex [sta::design_power $scene] 1]
set candidate_setup [expr {[sta::worst_slack_cmd max] * 1.0e9}]
set candidate_hold [expr {[sta::worst_slack_cmd min] * 1.0e9}]
set guard 0.05
if {$candidate_setup < $base_setup - $guard || $candidate_hold < $base_hold - $guard} {
  error "candidate violates 50 ps setup/hold guard"
}
write_db $::env(CANDIDATE_ODB)

set summary [open $::env(MOVE_APPLY_SUMMARY) w]
puts $summary "instance\tdx_dbu\tfrom_x\tfrom_y\tto_x\tto_y\tidle_predicted_delta_w\ttypical_predicted_delta_w\tpeak_predicted_delta_w\ttypical_measured_delta_w\tsetup_before_ns\tsetup_after_ns\thold_before_ns\thold_after_ns\tcandidate_odb"
puts $summary [join [list $inst_name [dict get $move dx_dbu] \
  [dict get $move from_x] [dict get $move from_y] [dict get $move to_x] [dict get $move to_y] \
  [dict get [dict get $rows idle] observed_delta_w] \
  [dict get [dict get $rows typical] observed_delta_w] \
  [dict get [dict get $rows peak] observed_delta_w] \
  [expr {$candidate_power - $base_power}] $base_setup $candidate_setup $base_hold $candidate_hold \
  $::env(CANDIDATE_ODB)] "\t"]
close $summary
puts [format "CALIBRATION_MOVE instance=%s dx=%s power_delta=%.12e W setup=%.6f->%.6f ns hold=%.6f->%.6f ns" \
  $inst_name [dict get $move dx_dbu] [expr {$candidate_power - $base_power}] \
  $base_setup $candidate_setup $base_hold $candidate_hold]
