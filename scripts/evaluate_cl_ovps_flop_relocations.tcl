# Move a sequential cell to an empty, rail-compatible placement site, then
# screen placement legality, setup/hold, HPWL, and VCD-weighted placement power.
set project_root [file normalize [file join [file dirname [info script]] ..]]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
if {[info exists ::env(RELOCATION_PLATFORM_DIR)] && $::env(RELOCATION_PLATFORM_DIR) ne ""} {
  set platform_dir $::env(RELOCATION_PLATFORM_DIR)
}
foreach required_env {PLACEMENT_ODB PLACEMENT_SDC RELOCATION_POOL RELOCATION_AUDIT \
                      CANDIDATE_ODB_DIR} {
  if {![info exists ::env($required_env)]} {
    error "required environment variable $required_env is not set"
  }
}

set max_timing_loss_ns 0.05
if {[info exists ::env(RELOCATION_MAX_TIMING_LOSS_NS)]} {
  set max_timing_loss_ns $::env(RELOCATION_MAX_TIMING_LOSS_NS)
}
set max_global_hpwl_increase 0.005
if {[info exists ::env(RELOCATION_MAX_GLOBAL_HPWL_INCREASE)]} {
  set max_global_hpwl_increase $::env(RELOCATION_MAX_GLOBAL_HPWL_INCREASE)
}
set maximum_search_dbu 60000
if {[info exists ::env(RELOCATION_MAX_SEARCH_DBU)]} {
  set maximum_search_dbu $::env(RELOCATION_MAX_SEARCH_DBU)
}
set activity_mode vcd
if {[info exists ::env(RELOCATION_ACTIVITY_MODE)]} { set activity_mode $::env(RELOCATION_ACTIVITY_MODE) }
set activity_scope ""
if {[info exists ::env(RELOCATION_ACTIVITY_SCOPE)]} { set activity_scope $::env(RELOCATION_ACTIVITY_SCOPE) }
if {$activity_mode eq "vcd" && $activity_scope eq ""} { error "RELOCATION_ACTIVITY_SCOPE is required for VCD activity" }
if {![info exists ::env(RELOCATION_LIBERTY)] || $::env(RELOCATION_LIBERTY) eq ""} {
  error "RELOCATION_LIBERTY must name at least one Sky130HD Liberty file"
}
foreach liberty [split $::env(RELOCATION_LIBERTY) "\n"] {
  if {$liberty ne ""} { read_liberty $liberty }
}
read_db $::env(PLACEMENT_ODB)
read_sdc $::env(PLACEMENT_SDC)
if {[info exists ::env(RELOCATION_RC_TCL)] && $::env(RELOCATION_RC_TCL) ne ""} {
  source $::env(RELOCATION_RC_TCL)
}
estimate_parasitics -placement
if {[info exists ::env(RELOCATION_UPF)] && $::env(RELOCATION_UPF) ne ""} {
  source [file join $project_root scripts cl_ovps_load_upf.tcl]
  cl_ovps_load_upf $::env(RELOCATION_UPF)
}
set_thread_count 1

set workloads {idle typical peak}
set vcds [dict create idle "" typical "" peak ""]
foreach workload $workloads {
  set key [string toupper $workload]
  if {[info exists ::env(${key}_VCD)]} { dict set vcds $workload $::env(${key}_VCD) }
}
proc load_workload {workload} {
  global vcds activity_scope activity_mode
  if {$activity_mode eq "vcd"} {
    read_vcd -scope $activity_scope [dict get $vcds $workload]
  } elseif {$activity_mode eq "global_default"} {
    set key [string toupper $workload]
    set activity $::env(RELOCATION_${key}_ACTIVITY)
    set_power_activity -global -activity $activity -duty 0.5
    puts "CL_OVPS_ACTIVITY_POLICY workload=$workload source=global_default activity=$activity duty=0.5"
  } else {
    error "unsupported RELOCATION_ACTIVITY_MODE '$activity_mode'"
  }
}
proc switching_power {} {
  set scene [sta::cmd_scene]
  return [lindex [sta::design_power $scene] 1]
}
proc internal_power {} {
  set scene [sta::cmd_scene]
  return [lindex [sta::design_power $scene] 0]
}
proc leakage_power {} {
  set scene [sta::cmd_scene]
  return [lindex [sta::design_power $scene] 2]
}
proc row_for_y {block y} {
  foreach row [$block getRows] {
    set box [$row getBBox]
    if {[$box yMin] == $y} { return $row }
  }
  return "NULL"
}
proc vertical_flip_parity {orient} {
  switch -- $orient {
    R0 - MY { return 0 }
    MX - R180 { return 1 }
    default { return -1 }
  }
}
proc target_orientation_is_rail_compatible {orient row_orient} {
  set inst_parity [vertical_flip_parity $orient]
  set row_parity [vertical_flip_parity $row_orient]
  return [expr {$inst_parity >= 0 && $inst_parity == $row_parity}]
}
proc site_is_clear {block inst x y row} {
  set box [$inst getBBox]
  set width [expr {[$box xMax] - [$box xMin]}]
  set x_max [expr {$x + $width}]
  set row_box [$row getBBox]
  if {$x < [$row_box xMin] || $x_max > [$row_box xMax]
      || $y < [$row_box yMin] || $y >= [$row_box yMax]} {
    return 0
  }
  foreach other [$block getInsts] {
    if {$other eq $inst} { continue }
    set status [$other getPlacementStatus]
    if {[lsearch -exact {PLACED FIXED COVER FIRM LOCKED} $status] < 0} { continue }
    set other_box [$other getBBox]
    if {$x < [$other_box xMax] && $x_max > [$other_box xMin]
        && $y < [$other_box yMax] && $y + [$box yMax] - [$box yMin] > [$other_box yMin]} {
      return 0
    }
  }
  return 1
}
proc find_target {block inst direction minimum maximum} {
  lassign [$inst getLocation] x y
  set row [row_for_y $block $y]
  if {$row eq "NULL"} { return [list "" "" "no_row"] }
  set pitch [$row getSpacing]
  if {$pitch <= 0} { set pitch [[$row getSite] getWidth] }
  set row_box [$row getBBox]
  set origin [$row_box xMin]
  set inst_box [$inst getBBox]
  set width [expr {[$inst_box xMax] - [$inst_box xMin]}]
  set min_delta [expr {max($minimum, $pitch)}]
  if {$direction eq "right"} {
    set x_hi [expr {[$row_box xMax] - $width}]
    set search_hi [expr {min($x_hi, $x + $maximum)}]
    set start [expr {$origin + (($search_hi - $origin) / $pitch) * $pitch}]
    for {set target $start} {$target >= $x + $min_delta && $target - $x <= $maximum} {set target [expr {$target - $pitch}]} {
      if {[site_is_clear $block $inst $target $y $row]} { return [list $target $y [$inst getOrient]] }
    }
  } elseif {$direction eq "left"} {
    set search_lo [expr {max($origin, $x - $maximum)}]
    set start [expr {$origin + (($search_lo - $origin + $pitch - 1) / $pitch) * $pitch}]
    for {set target $start} {$target <= $x - $min_delta && $x - $target <= $maximum} {set target [expr {$target + $pitch}]} {
      if {[site_is_clear $block $inst $target $y $row]} { return [list $target $y [$inst getOrient]] }
    }
  } else {
    return [list "" "" "invalid_direction"]
  }
  return [list "" "" "no_vacant_site_in_range"]
}
proc target_for_proposal {block inst proposal minimum maximum} {
  if {![dict exists $proposal target_x_dbu] || ![dict exists $proposal target_y_dbu]} {
    return [find_target $block $inst [dict get $proposal direction] $minimum $maximum]
  }
  lassign [$inst getLocation] x y
  set target_x [dict get $proposal target_x_dbu]
  set target_y [dict get $proposal target_y_dbu]
  set displacement [expr {abs($target_x - $x) + abs($target_y - $y)}]
  if {$displacement < $minimum || $displacement > $maximum} {
    return [list "" "" "invalid_explicit_target_displacement"]
  }
  set source_row [row_for_y $block $y]
  set row [row_for_y $block $target_y]
  if {$source_row eq "NULL" || $row eq "NULL"} { return [list "" "" "no_row"] }
  if {[[$source_row getSite] getName] ne [[$row getSite] getName]} {
    return [list "" "" "incompatible_row_site"]
  }
  set pitch [$row getSpacing]
  if {$pitch <= 0} { set pitch [[$row getSite] getWidth] }
  set row_box [$row getBBox]
  set inst_box [$inst getBBox]
  set width [expr {[$inst_box xMax] - [$inst_box xMin]}]
  if {$target_x < [$row_box xMin] || $target_x + $width > [$row_box xMax]} {
    return [list "" "" "target_outside_row"]
  }
  if {($target_x - [$row_box xMin]) % $pitch != 0} {
    return [list "" "" "target_not_site_aligned"]
  }
  set target_orient [$inst getOrient]
  if {[dict exists $proposal target_orient]} { set target_orient [dict get $proposal target_orient] }
  if {![target_orientation_is_rail_compatible $target_orient [$row getOrient]]} {
    return [list "" "" "target_orientation_rail_mismatch"]
  }
  if {![site_is_clear $block $inst $target_x $target_y $row]} {
    return [list "" "" "target_site_not_clear"]
  }
  return [list $target_x $target_y $target_orient]
}
proc check_timing_guard {candidate baseline maximum_loss} {
  set floor [expr {$baseline - $maximum_loss}]
  if {$baseline >= 0.0 && $floor < 0.0} { set floor 0.0 }
  return [expr {$candidate >= $floor}]
}
proc signal_hpwl_dbu {block} {
  set total 0
  foreach net [$block getNets] {
    set sig_type [$net getSigType]
    if {$sig_type eq "POWER" || $sig_type eq "GROUND"} { continue }
    set xs {}
    set ys {}
    foreach iterm [$net getITerms] {
      set avg [$iterm getAvgXY]
      if {[llength $avg] >= 3 && [lindex $avg 0]} {
        lappend xs [lindex $avg 1]
        lappend ys [lindex $avg 2]
      }
    }
    foreach bterm [$net getBTerms] {
      set box [$bterm getBBox]
      if {$box ne "NULL"} {
        lappend xs [expr {([$box xMin] + [$box xMax]) / 2}]
        lappend ys [expr {([$box yMin] + [$box yMax]) / 2}]
      }
    }
    if {[llength $xs] < 2} { continue }
    set xmin [lindex $xs 0]
    set xmax $xmin
    set ymin [lindex $ys 0]
    set ymax $ymin
    foreach x $xs y $ys {
      if {$x < $xmin} { set xmin $x }
      if {$x > $xmax} { set xmax $x }
      if {$y < $ymin} { set ymin $y }
      if {$y > $ymax} { set ymax $y }
    }
    incr total [expr {($xmax - $xmin) + ($ymax - $ymin)}]
  }
  return $total
}
proc read_pool {path} {
  set input [open $path r]
  set header [split [gets $input] "\t"]
  set rows {}
  while {[gets $input line] >= 0} {
    if {$line eq ""} { continue }
    set fields [split $line "\t"]
    if {[llength $fields] != [llength $header]} { error "malformed relocation row: $line" }
    set row [dict create]
    for {set i 0} {$i < [llength $header]} {incr i} { dict set row [lindex $header $i] [lindex $fields $i] }
    lappend rows $row
  }
  close $input
  return $rows
}

set block [ord::get_db_block]
if {[catch {check_placement -placeable} placement_error]} {
  error "baseline detailed placement is not legal: $placement_error"
}
set baseline_setup [expr {[sta::worst_slack_cmd max] * 1.0e9}]
set baseline_hold [expr {[sta::worst_slack_cmd min] * 1.0e9}]
set baseline_hpwl_dbu [signal_hpwl_dbu $block]
set baseline_power [dict create]
set baseline_internal [dict create]
set baseline_leakage [dict create]
foreach workload $workloads {
  load_workload $workload
  dict set baseline_power $workload [switching_power]
  dict set baseline_internal $workload [internal_power]
  dict set baseline_leakage $workload [leakage_power]
}
puts [format "CL_OVPS_RELOCATION_BASELINE setup_ns=%.9f hold_ns=%.9f signal_hpwl_dbu=%d hpwl_limit_pct=%.3f idle_w=%.12e typical_w=%.12e peak_w=%.12e" \
  $baseline_setup $baseline_hold $baseline_hpwl_dbu [expr {$max_global_hpwl_increase * 100.0}] [dict get $baseline_power idle] \
  [dict get $baseline_power typical] [dict get $baseline_power peak]]

set output [open $::env(RELOCATION_AUDIT) w]
puts $output "candidate_id\tinstance\tmaster\tdirection\tfrom_x_dbu\tfrom_y_dbu\tto_x_dbu\tto_y_dbu\tdisplacement_dbu\tstatus\tsignal_hpwl_before_dbu\tsignal_hpwl_after_dbu\tsignal_hpwl_delta_dbu\tsignal_hpwl_delta_pct\thpwl_guard_status\tsetup_before_ns\tsetup_after_ns\thold_before_ns\thold_after_ns\tidle_before_w\tidle_after_w\tidle_delta_w\ttypical_before_w\ttypical_after_w\ttypical_delta_w\tpeak_before_w\tpeak_after_w\tpeak_delta_w\todb\tdetail\tidle_internal_delta_w\tidle_dynamic_delta_w\tidle_leakage_delta_w\ttypical_internal_delta_w\ttypical_dynamic_delta_w\ttypical_leakage_delta_w\tpeak_internal_delta_w\tpeak_dynamic_delta_w\tpeak_leakage_delta_w\tfrom_orient\tto_orient"
set legal_count 0
set saved_count 0
set max_restore_error 0.0
foreach proposal [read_pool $::env(RELOCATION_POOL)] {
  set candidate [dict get $proposal candidate_id]
  set inst_name [dict get $proposal instance]
  set inst [$block findInst $inst_name]
  set master [dict get $proposal master]
  set direction [dict get $proposal direction]
  set before_x [dict get $proposal from_x_dbu]
  set before_y [dict get $proposal from_y_dbu]
  set from_orient [expr {$inst eq "NULL" ? "" : [$inst getOrient]}]
  set blank [list $candidate $inst_name $master $direction $before_x $before_y "" "" "" no_site \
    $baseline_hpwl_dbu "" "" "" not_evaluated $baseline_setup "" $baseline_hold "" [dict get $baseline_power idle] "" "" \
    [dict get $baseline_power typical] "" "" [dict get $baseline_power peak] "" "" "" ""]
  foreach workload $workloads { lappend blank "" "" "" }
  lappend blank $from_orient ""
  if {$inst eq "NULL"} {
    lset blank 9 not_found
    lset blank 29 "instance missing from placement ODB"
    puts $output [join $blank "\t"]
    continue
  }
  if {[$inst getPlacementStatus] ne "PLACED"
      || ![string match "*dfxtp*" [[$inst getMaster] getName]]
      && ![string match "*edfxtp*" [[$inst getMaster] getName]]} {
    lset blank 9 not_movable
    lset blank 29 "instance is not a placed sequential cell"
    puts $output [join $blank "\t"]
    continue
  }
  lassign [$inst getLocation] old_x old_y
  if {$old_x != $before_x || $old_y != $before_y} {
    lset blank 9 stale_candidate
    lset blank 29 "source coordinates changed since pool generation"
    puts $output [join $blank "\t"]
    continue
  }
  set target [target_for_proposal $block $inst $proposal [dict get $proposal min_displacement_dbu] $maximum_search_dbu]
  if {[llength $target] != 3 || [lindex $target 2] eq "no_row" || [lindex $target 0] eq ""} {
    lset blank 9 [lindex $target 2]
    lset blank 29 "no target satisfying legal site, row orientation, rail, and displacement constraints"
    puts $output [join $blank "\t"]
    continue
  }
  lassign $target target_x target_y target_orient
  set move_rc [catch {
    $inst setOrient $target_orient
    $inst setLocation $target_x $target_y
    estimate_parasitics -placement
    check_placement -placeable
    set candidate_setup [expr {[sta::worst_slack_cmd max] * 1.0e9}]
    set candidate_hold [expr {[sta::worst_slack_cmd min] * 1.0e9}]
    set candidate_hpwl_dbu [signal_hpwl_dbu $block]
    set candidate_power [dict create]
    set candidate_internal [dict create]
    set candidate_leakage [dict create]
    foreach workload $workloads {
      load_workload $workload
      dict set candidate_power $workload [switching_power]
      dict set candidate_internal $workload [internal_power]
      dict set candidate_leakage $workload [leakage_power]
    }
  } move_error]
  set restore_rc [catch {
    $inst setOrient $from_orient
    $inst setLocation $old_x $old_y
    estimate_parasitics -placement
    set restored_setup [expr {[sta::worst_slack_cmd max] * 1.0e9}]
    set restored_hold [expr {[sta::worst_slack_cmd min] * 1.0e9}]
    set restored_power [dict create]
    set restored_internal [dict create]
    set restored_leakage [dict create]
    foreach workload $workloads {
      load_workload $workload
      dict set restored_power $workload [switching_power]
      dict set restored_internal $workload [internal_power]
      dict set restored_leakage $workload [leakage_power]
    }
  } restore_error]
  if {$restore_rc} { error "failed to restore $inst_name after relocation: $restore_error" }
  foreach workload $workloads {
    foreach restored_value [list [dict get $restored_power $workload] \
        [dict get $restored_internal $workload] [dict get $restored_leakage $workload]] \
        baseline_value [list [dict get $baseline_power $workload] \
        [dict get $baseline_internal $workload] [dict get $baseline_leakage $workload]] {
      set err [expr {abs($restored_value - $baseline_value)}]
      if {$err > $max_restore_error} { set max_restore_error $err }
    }
  }
  if {abs($restored_setup - $baseline_setup) > 1.0e-6 || abs($restored_hold - $baseline_hold) > 1.0e-6} {
    error "timing did not restore after candidate $candidate"
  }
  if {$move_rc} {
    lset blank 6 $target_x
    lset blank 7 $target_y
    lset blank 8 [expr {abs($target_x - $old_x)}]
    lset blank 9 illegal
    lset blank 29 $move_error
    puts $output [join $blank "\t"]
    continue
  }
  incr legal_count
  set timing_ok [expr {[check_timing_guard $candidate_setup $baseline_setup $max_timing_loss_ns]
    && [check_timing_guard $candidate_hold $baseline_hold $max_timing_loss_ns]}]
  set hpwl_delta_dbu [expr {$candidate_hpwl_dbu - $baseline_hpwl_dbu}]
  set hpwl_delta_pct [expr {$baseline_hpwl_dbu > 0 ? 100.0 * $hpwl_delta_dbu / $baseline_hpwl_dbu : 0.0}]
  set hpwl_ok [expr {$candidate_hpwl_dbu <= $baseline_hpwl_dbu * (1.0 + $max_global_hpwl_increase)}]
  if {$timing_ok && $hpwl_ok} {
    set status timing_safe
  } elseif {!$timing_ok && !$hpwl_ok} {
    set status timing_and_hpwl_guard_failed
  } elseif {!$timing_ok} {
    set status timing_regression
  } else {
    set status hpwl_guard_failed
  }
  set odb_path ""
  if {$hpwl_ok} {
    set save_rc [catch {
      $inst setOrient $target_orient
      $inst setLocation $target_x $target_y
      estimate_parasitics -placement
      check_placement -placeable
      set odb_path [file join $::env(CANDIDATE_ODB_DIR) "${candidate}.odb"]
      write_db $odb_path
      $inst setOrient $from_orient
      $inst setLocation $old_x $old_y
      estimate_parasitics -placement
    } save_error]
    if {$save_rc} { error "failed to save or restore candidate $candidate: $save_error" }
    incr saved_count
  }
  set hpwl_guard_status [expr {$hpwl_ok ? "PASS" : "FAIL"}]
  set row [list $candidate $inst_name $master $direction $old_x $old_y $target_x $target_y \
    [expr {abs($target_x - $old_x)}] $status $baseline_hpwl_dbu $candidate_hpwl_dbu \
    $hpwl_delta_dbu [format %.6f $hpwl_delta_pct] $hpwl_guard_status \
    $baseline_setup $candidate_setup $baseline_hold $candidate_hold]
  foreach workload $workloads {
    set before [dict get $baseline_power $workload]
    set after [dict get $candidate_power $workload]
    lappend row $before $after [expr {$after - $before}]
  }
  lappend row $odb_path ""
  foreach workload $workloads {
    set internal_delta [expr {[dict get $candidate_internal $workload] - [dict get $baseline_internal $workload]}]
    set switching_delta [expr {[dict get $candidate_power $workload] - [dict get $baseline_power $workload]}]
    set dynamic_delta [expr {$internal_delta + $switching_delta}]
    set leakage_delta [expr {[dict get $candidate_leakage $workload] - [dict get $baseline_leakage $workload]}]
    lappend row $internal_delta $dynamic_delta $leakage_delta
  }
  lappend row $from_orient $target_orient
  puts $output [join $row "\t"]
}
close $output
puts [format "CL_OVPS_RELOCATION_SCREEN legal=%d saved_under_hpwl_guard=%d maximum_restore_power_error_w=%.3e audit=%s" \
  $legal_count $saved_count $max_restore_error $::env(RELOCATION_AUDIT)]
if {$max_restore_error > 1.0e-9} {
  error "relocation evaluator failed its power restore-to-baseline check"
}
