# Apply the best measured timing-safe move to a separate candidate ODB.
# The source checkpoint is read-only; write_db targets CANDIDATE_ODB only.
set project_root [file normalize [file join [file dirname [info script]] ..]]
set result_dir [file join $project_root build orfs results sky130hd picorv32_baseline baseline]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
set placement_odb [file join $result_dir 3_5_place_dp.odb]
set placement_sdc [file join $result_dir 3_place.sdc]

foreach required_env {ACTIVITY_VCD MOVE_EVAL_OUTPUT CANDIDATE_ODB MOVE_APPLY_SUMMARY} {
  if {![info exists ::env($required_env)]} {
    error "required environment variable $required_env is not set"
  }
}
if {[file normalize $::env(CANDIDATE_ODB)] eq [file normalize $placement_odb]} {
  error "candidate ODB must be separate from the baseline checkpoint"
}

read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $placement_odb
read_sdc $placement_sdc
source [file join $platform_dir setRC.tcl]
estimate_parasitics -placement
read_upf -file [file join $project_root designs picorv32 flow constraints picorv32.upf]
set_thread_count 1
read_vcd -scope tb_picorv32_activity/dut $::env(ACTIVITY_VCD)

set input [open $::env(MOVE_EVAL_OUTPUT) r]
set header_line [gets $input]
if {$header_line eq ""} {
  close $input
  error "move evaluation output is empty"
}
set header [split $header_line "\t"]
set columns [dict create]
for {set i 0} {$i < [llength $header]} {incr i} {
  dict set columns [lindex $header $i] $i
}
foreach required_column {instance from_x from_y to_x to_y idle_delta_w typical_delta_w peak_delta_w} {
  if {![dict exists $columns $required_column]} {
    close $input
    error "move evaluation is missing column $required_column"
  }
}

set tolerance 1.0e-12
if {[info exists ::env(MOVE_EVAL_NONREGRESSION_TOLERANCE_W)]} {
  set tolerance $::env(MOVE_EVAL_NONREGRESSION_TOLERANCE_W)
}
set selected_instance ""
set selected_to_x ""
set selected_to_y ""
if {[info exists ::env(MOVE_SELECTED_INSTANCE)]} {
  set selected_instance $::env(MOVE_SELECTED_INSTANCE)
}
if {[info exists ::env(MOVE_SELECTED_TO_X)]} {
  set selected_to_x $::env(MOVE_SELECTED_TO_X)
}
if {[info exists ::env(MOVE_SELECTED_TO_Y)]} {
  set selected_to_y $::env(MOVE_SELECTED_TO_Y)
}
set selection_requested [expr {$selected_instance ne "" || $selected_to_x ne "" || $selected_to_y ne ""}]
if {$selection_requested && ($selected_instance eq "" || $selected_to_x eq "" || $selected_to_y eq "")} {
  close $input
  error "select a Pareto move with all of MOVE_SELECTED_INSTANCE, MOVE_SELECTED_TO_X, and MOVE_SELECTED_TO_Y"
}

set pareto_rows {}
while {[gets $input line] >= 0} {
  if {$line eq ""} {
    continue
  }
  set fields [split $line "\t"]
  set idle_delta [expr {double([lindex $fields [dict get $columns idle_delta_w]])}]
  set typical_delta [expr {double([lindex $fields [dict get $columns typical_delta_w]])}]
  set peak_delta [expr {double([lindex $fields [dict get $columns peak_delta_w]])}]
  if {$idle_delta > $tolerance || $typical_delta > $tolerance || $peak_delta > $tolerance} {
    close $input
    error "Pareto input contains a workload regression beyond tolerance"
  }
  if {$idle_delta < -$tolerance || $typical_delta < -$tolerance || $peak_delta < -$tolerance} {
    lappend pareto_rows $fields
  }
}
close $input
set matches {}
foreach fields $pareto_rows {
  if {!$selection_requested
      || ([lindex $fields [dict get $columns instance]] eq $selected_instance
          && [lindex $fields [dict get $columns to_x]] eq $selected_to_x
          && [lindex $fields [dict get $columns to_y]] eq $selected_to_y)} {
    lappend matches $fields
  }
}
if {[llength $matches] == 0} {
  error "no Pareto move matched the requested selection"
}
if {!$selection_requested && [llength $matches] > 1} {
  error "Pareto frontier has [llength $matches] moves; inspect pareto_candidates.tsv and set MOVE_SELECTED_INSTANCE, MOVE_SELECTED_TO_X, and MOVE_SELECTED_TO_Y"
}
if {[llength $matches] > 1} {
  error "the requested instance/destination matches multiple Pareto rows"
}
set best [lindex $matches 0]

proc move_field {fields columns name} {
  return [lindex $fields [dict get $columns $name]]
}
proc within_timing_guard {candidate_ns baseline_ns max_loss_ns} {
  set floor [expr {$baseline_ns - $max_loss_ns}]
  if {$baseline_ns >= 0.0 && $floor < 0.0} {
    set floor 0.0
  }
  return [expr {$candidate_ns >= $floor}]
}
set inst_name [move_field $best $columns instance]
set from_x [move_field $best $columns from_x]
set from_y [move_field $best $columns from_y]
set to_x [move_field $best $columns to_x]
set to_y [move_field $best $columns to_y]
set inst [[ord::get_db_block] findInst $inst_name]
if {$inst eq "NULL"} {
  error "selected instance $inst_name is absent from the baseline checkpoint"
}
lassign [$inst getLocation] actual_x actual_y
if {$actual_x != $from_x || $actual_y != $from_y} {
  error "selected move is stale: expected $inst_name at ($from_x,$from_y), found ($actual_x,$actual_y)"
}

check_placement -placeable
set scene [sta::cmd_scene]
set baseline_switch [lindex [sta::design_power $scene] 1]
set baseline_setup_ns [expr {[sta::worst_slack_cmd max] * 1.0e9}]
set baseline_hold_ns [expr {[sta::worst_slack_cmd min] * 1.0e9}]
$inst setLocation $to_x $to_y
estimate_parasitics -placement
check_placement -placeable
# Re-annotate after changing geometry so OpenSTA refreshes its power cache.
read_vcd -scope tb_picorv32_activity/dut $::env(ACTIVITY_VCD)
set candidate_switch [lindex [sta::design_power $scene] 1]
set candidate_setup_ns [expr {[sta::worst_slack_cmd max] * 1.0e9}]
set candidate_hold_ns [expr {[sta::worst_slack_cmd min] * 1.0e9}]
set max_timing_loss_ns 0.05
if {[info exists ::env(MOVE_EVAL_MAX_TIMING_LOSS_NS)]} {
  set max_timing_loss_ns $::env(MOVE_EVAL_MAX_TIMING_LOSS_NS)
}
if {![within_timing_guard $candidate_setup_ns $baseline_setup_ns $max_timing_loss_ns]
    || ![within_timing_guard $candidate_hold_ns $baseline_hold_ns $max_timing_loss_ns]} {
  error "selected move no longer meets the setup/hold guard"
}
if {$candidate_switch > $baseline_switch + $tolerance} {
  error "selected move regresses typical-workload switching power beyond tolerance"
}

write_db $::env(CANDIDATE_ODB)
set summary [open $::env(MOVE_APPLY_SUMMARY) w]
puts $summary "instance\tfrom_x\tfrom_y\tto_x\tto_y\tpredicted_delta_w\tmeasured_delta_w\tsetup_before_ns\tsetup_after_ns\thold_before_ns\thold_after_ns\tcandidate_odb"
puts $summary [join [list $inst_name $from_x $from_y $to_x $to_y \
  [move_field $best $columns typical_delta_w] \
  [expr {$candidate_switch - $baseline_switch}] \
  $baseline_setup_ns $candidate_setup_ns $baseline_hold_ns $candidate_hold_ns \
  $::env(CANDIDATE_ODB)] "\t"]
close $summary
puts [format "APPLIED_MOVE instance=%s from=(%s,%s) to=(%s,%s) switching_delta=%.12e W setup=%.6f->%.6f ns hold=%.6f->%.6f ns" \
  $inst_name $from_x $from_y $to_x $to_y \
  [expr {$candidate_switch - $baseline_switch}] \
  $baseline_setup_ns $candidate_setup_ns $baseline_hold_ns $candidate_hold_ns]
