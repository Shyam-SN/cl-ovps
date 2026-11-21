# Read-only what-if evaluation of nearby legal cell moves at detailed place.
set project_root [file normalize [file join [file dirname [info script]] ..]]
set result_dir [file join $project_root build orfs results sky130hd picorv32_baseline baseline]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
set placement_odb [file join $result_dir 3_5_place_dp.odb]
set placement_sdc [file join $result_dir 3_place.sdc]

foreach required_env {ACTIVITY_VCD MOVE_EVAL_CANDIDATES MOVE_EVAL_NET_ACTIVITY MOVE_EVAL_OUTPUT MOVE_EVAL_WORKLOAD} {
  if {![info exists ::env($required_env)]} {
    error "required environment variable $required_env is not set"
  }
}

set max_search_sites 20
if {[info exists ::env(MOVE_EVAL_MAX_SEARCH_SITES)]} {
  set max_search_sites $::env(MOVE_EVAL_MAX_SEARCH_SITES)
}
set max_moves_per_cell 2
if {[info exists ::env(MOVE_EVAL_MAX_MOVES_PER_CELL)]} {
  set max_moves_per_cell $::env(MOVE_EVAL_MAX_MOVES_PER_CELL)
}
set max_timing_loss_ns 0.05
if {[info exists ::env(MOVE_EVAL_MAX_TIMING_LOSS_NS)]} {
  set max_timing_loss_ns $::env(MOVE_EVAL_MAX_TIMING_LOSS_NS)
}
set nominal_voltage 1.8

read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $placement_odb
read_sdc $placement_sdc
source [file join $platform_dir setRC.tcl]
estimate_parasitics -placement
read_upf -file [file join $project_root designs picorv32 flow constraints picorv32.upf]
set_thread_count 1
read_vcd -scope tb_picorv32_activity/dut $::env(ACTIVITY_VCD)
report_activity_annotation

proc read_tsv_dict {path key_column value_column} {
  set input [open $path r]
  set mapping [dict create]
  set first 1
  while {[gets $input line] >= 0} {
    if {$line eq ""} {
      continue
    }
    set fields [split $line "\t"]
    if {$first} {
      set first 0
      continue
    }
    if {[llength $fields] <= $value_column} {
      close $input
      error "malformed TSV row in $path: $line"
    }
    dict set mapping [lindex $fields $key_column] [lindex $fields $value_column]
  }
  close $input
  return $mapping
}

proc placement_row_for_y {block y} {
  foreach row [$block getRows] {
    set box [$row getBBox]
    if {[$box yMin] == $y} {
      return $row
    }
  }
  return "NULL"
}

proc move_site_clear {block inst candidate_x candidate_y row} {
  set original_box [$inst getBBox]
  set width [expr {[$original_box xMax] - [$original_box xMin]}]
  set height [expr {[$original_box yMax] - [$original_box yMin]}]
  set candidate_x_max [expr {$candidate_x + $width}]
  set candidate_y_max [expr {$candidate_y + $height}]
  set row_box [$row getBBox]
  if {$candidate_x < [$row_box xMin] || $candidate_x_max > [$row_box xMax]
      || $candidate_y < [$row_box yMin] || $candidate_y_max > [$row_box yMax]} {
    return 0
  }

  foreach other [$block getInsts] {
    if {$other eq $inst} {
      continue
    }
    set status [$other getPlacementStatus]
    if {[lsearch -exact {PLACED FIXED COVER} $status] < 0} {
      continue
    }
    set other_box [$other getBBox]
    if {$candidate_x < [$other_box xMax] && $candidate_x_max > [$other_box xMin]
        && $candidate_y < [$other_box yMax] && $candidate_y_max > [$other_box yMin]} {
      return 0
    }
  }
  return 1
}

proc net_capacitance {net_name scene} {
  set nets [get_nets -quiet $net_name]
  if {[llength $nets] != 1} {
    return -1.0
  }
  return [[lindex $nets 0] capacitance $scene "max"]
}

proc instance_active_nets {inst activity scene} {
  set nets [dict create]
  foreach iterm [$inst getITerms] {
    set db_net [$iterm getNet]
    if {$db_net eq "NULL"} {
      continue
    }
    set net_name [$db_net getName]
    if {![dict exists $activity $net_name]} {
      continue
    }
    dict set nets $net_name [dict get $activity $net_name]
  }
  set active_nets [dict create]
  dict for {net_name density} $nets {
    set cap [net_capacitance $net_name $scene]
    if {$cap >= 0.0} {
      dict set active_nets $net_name [list $density $cap]
    }
  }
  return $active_nets
}

proc score_nets {net_data voltage} {
  set score 0.0
  dict for {net_name data} $net_data {
    lassign $data density cap
    set score [expr {$score + 0.5 * $cap * $voltage * $voltage * $density}]
  }
  return $score
}

proc all_net_score {activity scene voltage} {
  set score 0.0
  dict for {net_name density} $activity {
    set cap [net_capacitance $net_name $scene]
    if {$cap >= 0.0} {
      set score [expr {$score + 0.5 * $cap * $voltage * $voltage * $density}]
    }
  }
  return $score
}

proc check_move_timing {candidate_ns baseline_ns max_loss_ns} {
  set floor [expr {$baseline_ns - $max_loss_ns}]
  if {$baseline_ns >= 0.0 && $floor < 0.0} {
    set floor 0.0
  }
  return [expr {$candidate_ns >= $floor}]
}

proc evaluate_move {block inst inst_name target_x target_y scene activity voltage
                    baseline_switch baseline_setup_ns baseline_hold_ns max_loss_ns} {
  lassign [$inst getLocation] old_x old_y
  set old_nets [instance_active_nets $inst $activity $scene]
  set old_touch_score [score_nets $old_nets $voltage]

  set eval_rc [catch {
    $inst setLocation $target_x $target_y
    estimate_parasitics -placement
    # This validates the whole movable placement, including regions and rules
    # that the simple row/overlap prefilter cannot model.
    check_placement -placeable
    # OpenSTA caches instance power separately from placement parasitics.
    # Reapplying the same VCD invalidates that cache without changing activity.
    read_vcd -scope tb_picorv32_activity/dut $::env(ACTIVITY_VCD)
    set candidate_switch [lindex [sta::design_power $scene] 1]
    set candidate_setup_ns [expr {[sta::worst_slack_cmd max] * 1.0e9}]
    set candidate_hold_ns [expr {[sta::worst_slack_cmd min] * 1.0e9}]
    set new_nets [instance_active_nets $inst $activity $scene]
    set new_touch_score [score_nets $new_nets $voltage]
  } eval_error]

  # Always put the in-memory database back at the checkpoint location before
  # recording a result or trying the next proposal.
  set restore_rc [catch {
    $inst setLocation $old_x $old_y
    estimate_parasitics -placement
    read_vcd -scope tb_picorv32_activity/dut $::env(ACTIVITY_VCD)
    set restored_switch [lindex [sta::design_power $scene] 1]
  } restore_error]
  if {$restore_rc} {
    error "failed to restore $inst_name to ($old_x,$old_y): $restore_error"
  }
  set restore_error_w [expr {abs($restored_switch - $baseline_switch)}]

  if {$eval_rc} {
    return [dict create status illegal error $eval_error restore_error_w $restore_error_w]
  }

  set predicted_delta [expr {$new_touch_score - $old_touch_score}]
  set observed_delta [expr {$candidate_switch - $baseline_switch}]
  set delta_error [expr {$observed_delta - $predicted_delta}]
  set setup_ok [check_move_timing $candidate_setup_ns $baseline_setup_ns $max_loss_ns]
  set hold_ok [check_move_timing $candidate_hold_ns $baseline_hold_ns $max_loss_ns]
  set timing_ok [expr {$setup_ok && $hold_ok}]
  return [dict create \
    status [expr {$timing_ok ? "evaluated" : "timing_regression"}] \
    nets [dict size $old_nets] \
    baseline_touch_score $old_touch_score \
    candidate_touch_score $new_touch_score \
    baseline_switch $baseline_switch \
    candidate_switch $candidate_switch \
    observed_delta $observed_delta \
    predicted_delta $predicted_delta \
    delta_error $delta_error \
    setup_ns $candidate_setup_ns \
    hold_ns $candidate_hold_ns \
    timing_ok $timing_ok \
    restore_error_w $restore_error_w]
}

set candidates [read_tsv_dict $::env(MOVE_EVAL_CANDIDATES) 0 0]
set activity [read_tsv_dict $::env(MOVE_EVAL_NET_ACTIVITY) 0 1]
if {[dict size $candidates] == 0 || [dict size $activity] == 0} {
  error "move evaluator requires at least one cell and one active net"
}

set block [ord::get_db_block]
set scene [sta::cmd_scene]
set baseline_switch [lindex [sta::design_power $scene] 1]
set baseline_formula [all_net_score $activity $scene $nominal_voltage]
set baseline_setup_ns [expr {[sta::worst_slack_cmd max] * 1.0e9}]
set baseline_hold_ns [expr {[sta::worst_slack_cmd min] * 1.0e9}]
set baseline_score_error [expr {abs($baseline_formula - $baseline_switch)}]

if {[catch {check_placement -placeable} legality_error]} {
  error "detailed-place checkpoint is not legal: $legality_error"
}

puts [format "MOVE_EVAL_BASELINE switching=%.12e W formula=%.12e W error=%.3e W setup=%.6f ns hold=%.6f ns" \
  $baseline_switch $baseline_formula $baseline_score_error $baseline_setup_ns $baseline_hold_ns]
puts "MOVE_EVAL_WORKLOAD=$::env(MOVE_EVAL_WORKLOAD)"

set output [open $::env(MOVE_EVAL_OUTPUT) w]
puts $output "instance\tseed_pin\tseed_net\tseed_cost_w\tdx_dbu\tdy_dbu\tfrom_x\tfrom_y\tto_x\tto_y\tstatus\taffected_active_nets\tpredicted_delta_w\tobserved_delta_w\tdelta_error_w\tsetup_before_ns\tsetup_after_ns\thold_before_ns\thold_after_ns\ttiming_ok\trestore_error_w\tdetail"

set candidate_file [open $::env(MOVE_EVAL_CANDIDATES) r]
gets $candidate_file header
set trial_count 0
set legal_count 0
while {[gets $candidate_file line] >= 0} {
  if {$line eq ""} {
    continue
  }
  set fields [split $line "\t"]
  if {[llength $fields] < 5} {
    error "malformed move candidate row: $line"
  }
  lassign $fields inst_name seed_workload seed_pin seed_net seed_cost
  set inst [$block findInst $inst_name]
  if {$inst eq "NULL"} {
    puts $output [join [list $inst_name $seed_pin $seed_net $seed_cost "" "" "" "" "" "" \
      not_found 0 "" "" "" $baseline_setup_ns "" $baseline_hold_ns "" 0 0 \
      "db instance not found"] "\t"]
    continue
  }
  set status [$inst getPlacementStatus]
  if {$status ne "PLACED"} {
    puts $output [join [list $inst_name $seed_pin $seed_net $seed_cost "" "" "" "" "" "" \
      not_movable 0 "" "" "" $baseline_setup_ns "" $baseline_hold_ns "" 0 0 \
      "placement status $status"] "\t"]
    continue
  }
  lassign [$inst getLocation] x y
  set row [placement_row_for_y $block $y]
  if {$row eq "NULL"} {
    puts $output [join [list $inst_name $seed_pin $seed_net $seed_cost "" "" $x $y "" "" \
      no_row 0 "" "" "" $baseline_setup_ns "" $baseline_hold_ns "" 0 0 \
      "no row at instance y coordinate"] "\t"]
    continue
  }
  set pitch [$row getSpacing]
  if {$pitch <= 0} {
    set pitch [[ $row getSite] getWidth]
  }

  set proposals {}
  set found_positive 0
  set found_negative 0
  for {set step 1} {$step <= $max_search_sites} {incr step} {
    foreach direction {1 -1} {
      if {($direction > 0 && $found_positive) || ($direction < 0 && $found_negative)} {
        continue
      }
      set target_x [expr {$x + $direction * $step * $pitch}]
      if {![move_site_clear $block $inst $target_x $y $row]} {
        continue
      }
      lappend proposals [list $target_x $y]
      if {$direction > 0} {
        set found_positive 1
      } else {
        set found_negative 1
      }
      if {[llength $proposals] >= $max_moves_per_cell} {
        break
      }
    }
    if {[llength $proposals] >= $max_moves_per_cell} {
      break
    }
  }
  if {[llength $proposals] == 0} {
    puts $output [join [list $inst_name $seed_pin $seed_net $seed_cost "" "" $x $y "" "" \
      no_nearby_site 0 "" "" "" $baseline_setup_ns "" $baseline_hold_ns "" 0 0 \
      "no collision-free site within $max_search_sites sites"] "\t"]
    continue
  }

  foreach proposal $proposals {
    lassign $proposal target_x target_y
    set dx [expr {$target_x - $x}]
    set result [evaluate_move $block $inst $inst_name $target_x $target_y $scene \
      $activity $nominal_voltage $baseline_switch $baseline_setup_ns \
      $baseline_hold_ns $max_timing_loss_ns]
    incr trial_count
    if {[dict get $result status] eq "evaluated" || [dict get $result status] eq "timing_regression"} {
      incr legal_count
      puts $output [join [list \
        $inst_name $seed_pin $seed_net $seed_cost $dx 0 $x $y $target_x $target_y \
        [dict get $result status] [dict get $result nets] \
        [dict get $result predicted_delta] [dict get $result observed_delta] \
        [dict get $result delta_error] $baseline_setup_ns [dict get $result setup_ns] \
        $baseline_hold_ns [dict get $result hold_ns] [dict get $result timing_ok] \
        [dict get $result restore_error_w] ""] "\t"]
    } else {
      puts $output [join [list \
        $inst_name $seed_pin $seed_net $seed_cost $dx 0 $x $y $target_x $target_y \
        [dict get $result status] 0 "" "" "" $baseline_setup_ns "" \
        $baseline_hold_ns "" 0 [dict get $result restore_error_w] \
        [dict get $result error]] "\t"]
    }
  }
}
close $candidate_file
close $output

set final_switch [lindex [sta::design_power $scene] 1]
set final_setup_ns [expr {[sta::worst_slack_cmd max] * 1.0e9}]
set final_hold_ns [expr {[sta::worst_slack_cmd min] * 1.0e9}]
set restore_error [expr {abs($final_switch - $baseline_switch)}]
puts [format "MOVE_EVAL_RESTORED switching_error=%.3e W setup_error=%.3e ns hold_error=%.3e ns" \
  $restore_error [expr {abs($final_setup_ns - $baseline_setup_ns)}] \
  [expr {abs($final_hold_ns - $baseline_hold_ns)}]]
puts [format "MOVE_EVAL_TRIALS legal=%d total=%d baseline_score_error=%.3e W" \
  $legal_count $trial_count $baseline_score_error]

if {$restore_error > 1.0e-9
    || abs($final_setup_ns - $baseline_setup_ns) > 1.0e-6
    || abs($final_hold_ns - $baseline_hold_ns) > 1.0e-6} {
  error "move evaluator failed its restore-to-baseline check"
}
