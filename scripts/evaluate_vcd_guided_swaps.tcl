# Evaluate same-master high/low VCD-activity swaps from the baseline placement.
set project_root [file normalize [file join [file dirname [info script]] ..]]
set result_dir [file join $project_root build orfs results sky130hd picorv32_baseline baseline]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
set placement_odb [file join $result_dir 3_5_place_dp.odb]
set placement_sdc [file join $result_dir 3_place.sdc]
if {[info exists ::env(PLACEMENT_ODB)]} { set placement_odb $::env(PLACEMENT_ODB) }
if {[info exists ::env(PLACEMENT_SDC)]} { set placement_sdc $::env(PLACEMENT_SDC) }
set upf_file [file join $project_root designs picorv32 flow constraints picorv32.upf]
if {[info exists ::env(POWER_UPF)]} { set upf_file $::env(POWER_UPF) }
set activity_scope "tb_picorv32_activity/dut"
if {[info exists ::env(ACTIVITY_SCOPE)]} { set activity_scope $::env(ACTIVITY_SCOPE) }

foreach required_env {ACTIVITY_VCD MOVE_EVAL_NET_ACTIVITY SWAP_CANDIDATES SWAP_AUDIT_OUTPUT MOVE_EVAL_WORKLOAD} {
  if {![info exists ::env($required_env)]} {
    error "required environment variable $required_env is not set"
  }
}

set nominal_voltage 1.8
set max_timing_loss_ns 0.05
if {[info exists ::env(MOVE_EVAL_MAX_TIMING_LOSS_NS)]} {
  set max_timing_loss_ns $::env(MOVE_EVAL_MAX_TIMING_LOSS_NS)
}

read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $placement_odb
read_sdc $placement_sdc
source [file join $platform_dir setRC.tcl]
estimate_parasitics -placement
source [file join $project_root scripts load_single_domain_upf.tcl]
load_single_domain_upf $upf_file
set_thread_count 1
read_vcd -scope $activity_scope $::env(ACTIVITY_VCD)
report_activity_annotation

proc read_tsv_rows {path} {
  set input [open $path r]
  set header_line [gets $input]
  if {$header_line eq ""} {
    close $input
    error "TSV is empty: $path"
  }
  set header [split $header_line "\t"]
  set rows {}
  while {[gets $input line] >= 0} {
    if {$line eq ""} { continue }
    set fields [split $line "\t"]
    if {[llength $fields] != [llength $header]} {
      close $input
      error "malformed TSV row in $path: $line"
    }
    set row [dict create]
    for {set i 0} {$i < [llength $header]} {incr i} {
      dict set row [lindex $header $i] [lindex $fields $i]
    }
    lappend rows $row
  }
  close $input
  return $rows
}

proc read_activity_map {path} {
  set rows [read_tsv_rows $path]
  set mapping [dict create]
  foreach row $rows {
    dict set mapping [dict get $row net] [dict get $row density_hz]
  }
  return $mapping
}

proc net_capacitance {net_name scene} {
  global activity_net_objects
  if {![dict exists $activity_net_objects $net_name]} { return -1.0 }
  return [[dict get $activity_net_objects $net_name] capacitance $scene "max"]
}

proc all_net_score {activity scene voltage} {
  global activity_score_matched activity_score_skipped
  set score 0.0
  set matched 0
  set skipped 0
  set skipped_examples {}
  dict for {net_name density} $activity {
    set cap [net_capacitance $net_name $scene]
    if {$cap >= 0.0} {
      incr matched
      set score [expr {$score + 0.5 * $cap * $voltage * $voltage * $density}]
    } else {
      incr skipped
      if {[llength $skipped_examples] < 8} { lappend skipped_examples $net_name }
    }
  }
  set activity_score_matched $matched
  set activity_score_skipped $skipped
  puts "VCD_SWAP_SCORE_COVERAGE matched=$matched skipped=$skipped examples=[join $skipped_examples ,]"
  if {$skipped > 0} {
    error "VCD swap activity score skipped $skipped of [dict size $activity] nets"
  }
  return $score
}

proc check_move_timing {candidate_ns baseline_ns max_loss_ns} {
  set floor [expr {$baseline_ns - $max_loss_ns}]
  if {$baseline_ns >= 0.0 && $floor < 0.0} { set floor 0.0 }
  return [expr {$candidate_ns >= $floor}]
}

proc evaluate_swap {high_inst low_inst activity scene voltage baseline_switch baseline_score \
                    baseline_setup baseline_hold max_loss} {
  global activity_scope
  lassign [$high_inst getLocation] high_x high_y
  lassign [$low_inst getLocation] low_x low_y
  set result [dict create status illegal error ""]
  set eval_rc [catch {
    $high_inst setLocation $low_x $low_y
    $low_inst setLocation $high_x $high_y
    estimate_parasitics -placement
    check_placement -placeable
    read_vcd -scope $activity_scope $::env(ACTIVITY_VCD)
    set candidate_switch [lindex [sta::design_power $scene] 1]
    set candidate_setup [expr {[sta::worst_slack_cmd max] * 1.0e9}]
    set candidate_hold [expr {[sta::worst_slack_cmd min] * 1.0e9}]
    set candidate_score [all_net_score $activity $scene $voltage]
  } eval_error]

  set restore_rc [catch {
    $high_inst setLocation $high_x $high_y
    $low_inst setLocation $low_x $low_y
    estimate_parasitics -placement
    read_vcd -scope $activity_scope $::env(ACTIVITY_VCD)
    set restored_switch [lindex [sta::design_power $scene] 1]
    set restored_setup [expr {[sta::worst_slack_cmd max] * 1.0e9}]
    set restored_hold [expr {[sta::worst_slack_cmd min] * 1.0e9}]
  } restore_error]
  if {$restore_rc} {
    error "failed to restore swap placement: $restore_error"
  }
  set restore_power_error [expr {abs($restored_switch - $baseline_switch)}]
  set restore_setup_error [expr {abs($restored_setup - $baseline_setup)}]
  set restore_hold_error [expr {abs($restored_hold - $baseline_hold)}]

  if {$eval_rc} {
    dict set result error $eval_error
    dict set result restore_power_error $restore_power_error
    dict set result restore_setup_error $restore_setup_error
    dict set result restore_hold_error $restore_hold_error
    return $result
  }

  set timing_ok [expr {[check_move_timing $candidate_setup $baseline_setup $max_loss]
    && [check_move_timing $candidate_hold $baseline_hold $max_loss]}]
  dict set result status [expr {$timing_ok ? "evaluated" : "timing_regression"}]
  dict set result candidate_switch $candidate_switch
  dict set result observed_delta [expr {$candidate_switch - $baseline_switch}]
  dict set result predicted_delta [expr {$candidate_score - $baseline_score}]
  dict set result candidate_setup $candidate_setup
  dict set result candidate_hold $candidate_hold
  dict set result timing_ok $timing_ok
  dict set result restore_power_error $restore_power_error
  dict set result restore_setup_error $restore_setup_error
  dict set result restore_hold_error $restore_hold_error
  return $result
}

set activity [read_activity_map $::env(MOVE_EVAL_NET_ACTIVITY)]
if {[dict size $activity] == 0} { error "VCD activity map is empty" }
set activity_net_objects [dict create]
foreach net [get_nets -hierarchical *] {
  dict set activity_net_objects [get_full_name $net] $net
}
set block [ord::get_db_block]
set scene [sta::cmd_scene]
set baseline_switch [lindex [sta::design_power $scene] 1]
set baseline_score [all_net_score $activity $scene $nominal_voltage]
set max_score_error 0.01
if {[info exists ::env(MOVE_EVAL_MAX_SCORE_ERROR)]} {
  set max_score_error $::env(MOVE_EVAL_MAX_SCORE_ERROR)
}
set baseline_score_error [expr {$baseline_switch > 0.0
  ? abs($baseline_score - $baseline_switch) / $baseline_switch : 0.0}]
if {$baseline_score_error > $max_score_error} {
  error [format "VCD placement score differs from OpenSTA by %.3f%% (limit %.3f%%)" \
    [expr {$baseline_score_error * 100.0}] [expr {$max_score_error * 100.0}]]
}
set baseline_setup [expr {[sta::worst_slack_cmd max] * 1.0e9}]
set baseline_hold [expr {[sta::worst_slack_cmd min] * 1.0e9}]
check_placement -placeable
puts [format "VCD_SWAP_BASELINE workload=%s switching=%.12e W score=%.12e W relative_error=%.4f%% setup=%.6f ns hold=%.6f ns nets=%d/%d" \
  $::env(MOVE_EVAL_WORKLOAD) $baseline_switch $baseline_score \
  [expr {$baseline_score_error * 100.0}] $baseline_setup $baseline_hold \
  $activity_score_matched [dict size $activity]]

set output [open $::env(SWAP_AUDIT_OUTPUT) w]
puts $output "pair_id\thigh_instance\tlow_instance\tmaster\trow_y_dbu\thigh_activity_hz\tlow_activity_hz\tactivity_ratio\tdx_high_dbu\tdx_low_dbu\tstatus\tpredicted_delta_w\tobserved_delta_w\tsetup_before_ns\tsetup_after_ns\thold_before_ns\thold_after_ns\ttiming_ok\trestore_power_error_w\trestore_setup_error_ns\trestore_hold_error_ns\tdetail"

set trial_count 0
set legal_count 0
set maximum_restore_error 0.0
foreach row [read_tsv_rows $::env(SWAP_CANDIDATES)] {
  set pair_id [dict get $row pair_id]
  set high_name [dict get $row high_instance]
  set low_name [dict get $row low_instance]
  set high_inst [$block findInst $high_name]
  set low_inst [$block findInst $low_name]
  if {$high_inst eq "NULL" || $low_inst eq "NULL"} {
    puts $output [join [list $pair_id $high_name $low_name [dict get $row master] \
      [dict get $row row_y_dbu] [dict get $row high_activity_hz] [dict get $row low_activity_hz] \
      [dict get $row activity_ratio] "" "" not_found "" "" $baseline_setup "" $baseline_hold "" 0 "" "" "" "instance missing"] "\t"]
    continue
  }
  if {[$high_inst getPlacementStatus] ne "PLACED" || [$low_inst getPlacementStatus] ne "PLACED"
      || [[$high_inst getMaster] getName] ne [[$low_inst getMaster] getName]} {
    puts $output [join [list $pair_id $high_name $low_name [dict get $row master] \
      [dict get $row row_y_dbu] [dict get $row high_activity_hz] [dict get $row low_activity_hz] \
      [dict get $row activity_ratio] "" "" invalid_pair "" "" $baseline_setup "" $baseline_hold "" 0 "" "" "" \
      "pair is not a same-master pair of placed cells"] "\t"]
    continue
  }
  lassign [$high_inst getLocation] high_x high_y
  lassign [$low_inst getLocation] low_x low_y
  set cross_row_ok [expr {[info exists ::env(SWAP_ALLOW_CROSS_ROW)]
    && $::env(SWAP_ALLOW_CROSS_ROW) eq "1"}]
  if {$high_y != [dict get $row row_y_dbu]
      || (!$cross_row_ok && $high_y != $low_y)
      || [$high_inst getOrient] ne [$low_inst getOrient]} {
    puts $output [join [list $pair_id $high_name $low_name [dict get $row master] \
      [dict get $row row_y_dbu] [dict get $row high_activity_hz] [dict get $row low_activity_hz] \
      [dict get $row activity_ratio] "" "" invalid_pair "" "" $baseline_setup "" $baseline_hold "" 0 "" "" "" \
      "pair placement row or orientation changed since inventory export"] "\t"]
    continue
  }

  set result [evaluate_swap $high_inst $low_inst $activity $scene $nominal_voltage \
    $baseline_switch $baseline_score $baseline_setup $baseline_hold $max_timing_loss_ns]
  incr trial_count
  set status [dict get $result status]
  if {$status eq "evaluated" || $status eq "timing_regression"} { incr legal_count }
  foreach key {restore_power_error restore_setup_error restore_hold_error} {
    set value [dict get $result $key]
    if {$value > $maximum_restore_error} { set maximum_restore_error $value }
  }

  set candidate_setup ""
  set candidate_hold ""
  set timing_ok 0
  set predicted_delta ""
  set observed_delta ""
  if {$status eq "evaluated" || $status eq "timing_regression"} {
    set candidate_setup [dict get $result candidate_setup]
    set candidate_hold [dict get $result candidate_hold]
    set timing_ok [dict get $result timing_ok]
    set predicted_delta [dict get $result predicted_delta]
    set observed_delta [dict get $result observed_delta]
  }
  set detail [dict get $result error]
  puts $output [join [list $pair_id $high_name $low_name [dict get $row master] $high_y \
    [dict get $row high_activity_hz] [dict get $row low_activity_hz] [dict get $row activity_ratio] \
    [expr {$low_x - $high_x}] [expr {$high_x - $low_x}] $status $predicted_delta $observed_delta \
    $baseline_setup $candidate_setup $baseline_hold $candidate_hold $timing_ok \
    [dict get $result restore_power_error] [dict get $result restore_setup_error] \
    [dict get $result restore_hold_error] $detail] "\t"]

  if {$status eq "evaluated" && $timing_ok && [info exists ::env(SWAP_ODB_DIR)]} {
    set odb_path [file join $::env(SWAP_ODB_DIR) "${pair_id}.odb"]
    set saved_rc [catch {
      $high_inst setLocation $low_x $low_y
      $low_inst setLocation $high_x $high_y
      estimate_parasitics -placement
      check_placement -placeable
      write_db $odb_path
      $high_inst setLocation $high_x $high_y
      $low_inst setLocation $low_x $low_y
      estimate_parasitics -placement
    } saved_error]
    if {$saved_rc} { error "failed to save candidate ODB $odb_path: $saved_error" }
  }
}
close $output

set final_switch [lindex [sta::design_power $scene] 1]
set final_setup [expr {[sta::worst_slack_cmd max] * 1.0e9}]
set final_hold [expr {[sta::worst_slack_cmd min] * 1.0e9}]
set restore_power [expr {abs($final_switch - $baseline_switch)}]
set restore_setup [expr {abs($final_setup - $baseline_setup)}]
set restore_hold [expr {abs($final_hold - $baseline_hold)}]
puts [format "VCD_SWAP_RESTORED workload=%s power_error=%.3e W setup_error=%.3e ns hold_error=%.3e ns" \
  $::env(MOVE_EVAL_WORKLOAD) $restore_power $restore_setup $restore_hold]
puts [format "VCD_SWAP_TRIALS legal=%d total=%d maximum_restore_error=%.3e" \
  $legal_count $trial_count $maximum_restore_error]
if {$restore_power > 1.0e-9 || $restore_setup > 1.0e-6 || $restore_hold > 1.0e-6} {
  error "swap evaluator failed its restore-to-baseline check"
}
