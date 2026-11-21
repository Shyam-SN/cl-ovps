# Apply guarded post-CTS clock and sizing transforms to a copied Sky130HD ODB.

foreach required {AUDIT_INPUT_ODB AUDIT_SDC AUDIT_OUT_ODB AUDIT_REPORT} {
  if {![info exists ::env($required)]} { error "required environment variable $required is not set" }
}

set project_root [file normalize [file join [file dirname [info script]] ..]]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
if {[info exists ::env(AUDIT_PLATFORM_DIR)] && $::env(AUDIT_PLATFORM_DIR) ne ""} {
  set platform_dir $::env(AUDIT_PLATFORM_DIR)
}
if {![info exists ::env(AUDIT_LIBERTY)] || $::env(AUDIT_LIBERTY) eq ""} {
  error "AUDIT_LIBERTY must name at least one Sky130HD Liberty file"
}
foreach liberty [split $::env(AUDIT_LIBERTY) "\n"] {
  if {$liberty ne ""} { read_liberty $liberty }
}
read_db $::env(AUDIT_INPUT_ODB)
read_sdc $::env(AUDIT_SDC)
if {[info exists ::env(AUDIT_RC_TCL)] && $::env(AUDIT_RC_TCL) ne ""} {
  source $::env(AUDIT_RC_TCL)
}
if {[info exists ::env(AUDIT_SKIP_UPF)] && $::env(AUDIT_SKIP_UPF)} {
  puts "AUDIT_UPF skipped=1"
} else {
  if {![info exists ::env(AUDIT_UPF_FILE)] || $::env(AUDIT_UPF_FILE) eq ""} {
    error "AUDIT_UPF_FILE is required unless AUDIT_SKIP_UPF=1"
  }
  source [file join $project_root scripts cl_ovps_load_upf.tcl]
  cl_ovps_load_upf $::env(AUDIT_UPF_FILE)
}
set_thread_count 1
estimate_parasitics -placement
set activity_mode vcd
if {[info exists ::env(AUDIT_ACTIVITY_MODE)]} { set activity_mode $::env(AUDIT_ACTIVITY_MODE) }
if {$activity_mode eq "vcd"} {
  foreach required {ACTIVITY_VCD AUDIT_ACTIVITY_SCOPE} {
    if {![info exists ::env($required)] || $::env($required) eq ""} {
      error "$required is required when AUDIT_ACTIVITY_MODE=vcd"
    }
  }
  read_vcd -scope $::env(AUDIT_ACTIVITY_SCOPE) $::env(ACTIVITY_VCD)
  puts "CL_OVPS_ACTIVITY_POLICY source=vcd"
} elseif {$activity_mode eq "global_default"} {
  set activity $::env(AUDIT_DEFAULT_ACTIVITY)
  set duty 0.5
  if {![string is double -strict $activity] || $activity < 0.0} {
    error "AUDIT_DEFAULT_ACTIVITY must be a non-negative transitions-per-cycle value"
  }
  set_power_activity -global -activity $activity -duty $duty
  puts "CL_OVPS_ACTIVITY_POLICY source=global_default activity=$activity duty=$duty"
} else {
  error "unsupported AUDIT_ACTIVITY_MODE '$activity_mode'"
}
report_activity_annotation -report_unannotated
set block [ord::get_db_block]
foreach inst [$block getInsts] {
  set inst_name [$inst getName]
  if {![regexp {^(clkbuf|clkinv|clkload)} $inst_name]} { continue }
  foreach iterm [$inst getITerms] {
    set pin_name [[$iterm getMTerm] getName]
    set sta_pin [get_pins -quiet "$inst_name/$pin_name"]
    if {[llength $sta_pin] != 1} { continue }
    if {[catch {set activity [get_property -quiet [lindex $sta_pin 0] activity]}]} { continue }
    if {[llength $activity] < 3} { continue }
    puts "CL_OVPS_ACTIVITY_PIN name=$inst_name/$pin_name origin=[lindex $activity 2]"
  }
}

set block [ord::get_db_block]
set db [ord::get_db]
set report_file [open $::env(AUDIT_REPORT) w]
puts $report_file "stage\tmetric\tvalue"

proc scalar_slack {pin property} {
  if {[catch {set value [get_property -quiet $pin $property]}]} { return "" }
  if {$value eq "" || ![string is double -strict $value]} { return "" }
  return $value
}

proc current_worst_slacks {} {
  set scene [sta::cmd_scene]
  return [list [sta::worst_slack_cmd max] [sta::worst_slack_cmd min]]
}

proc clock_cell_power {stage} {
  global block
  set buffers {}
  set dummies {}
  set inverters {}
  set clock_gates {}
  foreach inst [$block getInsts] {
    set name [$inst getName]
    set master [[$inst getMaster] getName]
    if {[string match "clkload*" $name] || [regexp {__clkload_[0-9]+$} $master]} {
      set dummies [concat $dummies [get_cells -quiet $name]]
    } elseif {[string match "clkbuf*" $name]} {
      if {[regexp {__(clkinv|inv)_[0-9]+$} $master]} {
        set inverters [concat $inverters [get_cells -quiet $name]]
      } elseif {[regexp {__(clkbuf|bufbuf|buf)_[0-9]+$} $master]} {
        set buffers [concat $buffers [get_cells -quiet $name]]
      } else {
        error "unrecognized CTS transport master $master on $name"
      }
    }
  }
  foreach cell [get_cells -quiet -hierarchical *] {
    set master [get_property -quiet $cell ref_name]
    if {[regexp {__(dlclkp|sdlclkp)_[0-9]+$} $master]} { lappend clock_gates $cell }
  }
  set cells [concat $buffers $dummies $inverters $clock_gates]
  puts "AUDIT_CLOCK_CELLS stage=$stage buffers=[llength $buffers] dummies=[llength $dummies] inverters=[llength $inverters] clock_gates=[llength $clock_gates]"
  report_power -instances $cells -digits 12
}

proc design_total_power_watts {} {
  set power [sta::design_power [sta::cmd_scene]]
  if {[llength $power] < 3} { error "OpenSTA did not return internal/switching/leakage power" }
  return [expr {[lindex $power 0] + [lindex $power 1] + [lindex $power 2]}]
}

proc max_slew_violations {} {
  with_output_to_variable report { report_check_types -max_slew -violators -format slack_only }
  return [string trim $report]
}

proc instance_slack_safe {inst_name margin} {
  set cells [get_cells -quiet $inst_name]
  if {[llength $cells] != 1} { return 0 }
  set pins [get_pins -quiet -of_objects $cells]
  if {[llength $pins] == 0} { return 0 }
  set setup_seen 0
  set hold_seen 0
  set worst_setup 1.0e99
  set worst_hold 1.0e99
  foreach pin $pins {
    set direction [get_property -quiet $pin direction]
    if {$direction eq "output"} {
      set setup [scalar_slack $pin slack_max]
      set hold [scalar_slack $pin slack_min]
      if {$setup ne ""} { set setup_seen 1; set worst_setup [expr {min($worst_setup, $setup)}] }
      if {$hold ne ""} { set hold_seen 1; set worst_hold [expr {min($worst_hold, $hold)}] }
    }
  }
  return [expr {$setup_seen && $hold_seen && $worst_setup >= $margin && $worst_hold >= $margin}]
}

proc downsize_slack_safe_cells {} {
  global block db report_file
  set names {}
  foreach inst [$block getInsts] { lappend names [$inst getName] }
  set resized 0
  set skipped_slack 0
  set skipped_library 0
  foreach name $names {
    if {[regexp {^(clkbuf|clkload|clkinv)} $name]} { continue }
    set inst [$block findInst $name]
    if {$inst eq "NULL"} { continue }
    set old_master [[$inst getMaster] getName]
    if {![regexp {^(sky130_fd_sc_hd__.+)_([2-9][0-9]*)$} $old_master -> family strength]} { continue }
    set target_name ${family}_1
    set target [$db findMaster $target_name]
    if {$target eq "NULL"} { incr skipped_library; continue }
    if {![instance_slack_safe $name 5.0e-11]} { incr skipped_slack; continue }
    if {[catch {set swapped [$inst swapMaster $target]} swap_error] || !$swapped} {
      puts "AUDIT_DOWNSIZE_SKIP instance=$name from=$old_master to=$target_name reason=master_swap_failed"
      continue
    }
    puts $report_file "stageB\tresize\t$name:$old_master->$target_name"
    incr resized
  }
  puts "AUDIT_DOWNSIZE resized=$resized skipped_slack=$skipped_slack skipped_library=$skipped_library"
}

proc next_smaller_master {master_name} {
  global db
  if {![regexp {^(sky130_fd_sc_hd__.+)_([0-9]+)$} $master_name -> family strength]} { return "NULL" }
  set strengths {1 2 4 6 8 12 16}
  set previous 0
  foreach candidate_strength $strengths {
    if {$candidate_strength >= $strength} { break }
    set candidate [$db findMaster ${family}_${candidate_strength}]
    if {$candidate ne "NULL"} { set previous $candidate_strength }
  }
  if {$previous == 0} { return "NULL" }
  return [$db findMaster ${family}_${previous}]
}

proc downsize_with_global_timing_budget {budget_ps max_cells local_slack_ps} {
  global block db report_file
  lassign [current_worst_slacks] start_setup start_hold
  if {$start_setup eq "" || $start_hold eq ""} {
    error "cannot establish setup/hold budget before experimental downsizing"
  }
  set names {}
  foreach inst [$block getInsts] { lappend names [$inst getName] }
  set names [lsort -dictionary $names]
  set accepted 0
  set accepted_steps 0
  set rejected_timing 0
  set rejected_power 0
  set rejected_slew 0
  set rejected_library 0
  set sizeable_by_family [dict create]
  set no_smaller_by_family [dict create]
  set budget_s [expr {$budget_ps * 1.0e-12}]
  set local_margin_s [expr {$local_slack_ps * 1.0e-12}]
  set resized_cells [dict create]
  foreach name $names {
    if {$accepted >= $max_cells} { break }
    if {[regexp {^(clkbuf|clkload|clkinv)} $name]} { continue }
    set inst [$block findInst $name]
    if {$inst eq "NULL"} { continue }
    set initial_master [[$inst getMaster] getName]
    if {![regexp {^(sky130_fd_sc_hd__.+)_[0-9]+$} $initial_master -> family]} { continue }
    if {[next_smaller_master $initial_master] eq "NULL"} {
      dict incr no_smaller_by_family $family
      continue
    }
    dict incr sizeable_by_family $family
    while {1} {
      if {$accepted >= $max_cells && ![dict exists $resized_cells $name]} { break }
      set old_master [[$inst getMaster] getName]
      set target [next_smaller_master $old_master]
      if {$target eq "NULL"} { break }
      set target_name [$target getName]
      if {![instance_slack_safe $name $local_margin_s]} { incr rejected_timing; break }
      set before_power [design_total_power_watts]
      set before_slew [max_slew_violations]
      if {[catch {set swapped [$inst swapMaster $target]}] || !$swapped} {
        incr rejected_library
        break
      }
      estimate_parasitics -placement
      lassign [current_worst_slacks] trial_setup trial_hold
      set setup_loss [expr {$start_setup - $trial_setup}]
      set hold_loss [expr {$start_hold - $trial_hold}]
      set trial_power [design_total_power_watts]
      set trial_slew [max_slew_violations]
      set timing_ok [expr {$trial_setup ne "" && $trial_hold ne "" &&
                           $setup_loss <= $budget_s && $hold_loss <= $budget_s}]
      set power_ok [expr {$trial_power < $before_power - 1.0e-15}]
      set slew_ok [expr {$trial_slew eq ""}]
      if {$timing_ok && $power_ok && $slew_ok} {
        puts $report_file "stage3a\tresize\t$name:$old_master->$target_name:power_before=$before_power:power_after=$trial_power:setup_loss_ps=[expr {$setup_loss * 1.0e12}]:hold_loss_ps=[expr {$hold_loss * 1.0e12}]"
        incr accepted_steps
        if {![dict exists $resized_cells $name]} { dict set resized_cells $name 1; incr accepted }
        continue
      }
      if {[catch {set restored [$inst swapMaster [$db findMaster $old_master]]}] || !$restored} {
        error "failed to restore $name to $old_master after downsizing rejection"
      }
      estimate_parasitics -placement
      if {!$timing_ok} { incr rejected_timing }
      if {!$power_ok} { incr rejected_power }
      if {!$slew_ok} { incr rejected_slew }
      break
    }
  }
  puts "AUDIT_DOWNSIZE_BUDGET resized_cells=$accepted resize_steps=$accepted_steps rejected_timing=$rejected_timing rejected_power=$rejected_power rejected_slew=$rejected_slew rejected_library=$rejected_library budget_ps=$budget_ps max_cells=$max_cells local_slack_ps=$local_slack_ps"
  foreach family [lsort -dictionary [concat [dict keys $sizeable_by_family] [dict keys $no_smaller_by_family]]] {
    set sizeable [expr {[dict exists $sizeable_by_family $family] ? [dict get $sizeable_by_family $family] : 0}]
    set unavailable [expr {[dict exists $no_smaller_by_family $family] ? [dict get $no_smaller_by_family $family] : 0}]
    puts "AUDIT_DOWNSIZE_LIBRARY family=$family downsizeable_instances=$sizeable no_smaller_instances=$unavailable"
  }
}

proc pin_activity_density {sta_pin} {
  set activity [get_property -quiet $sta_pin activity]
  if {[llength $activity] < 3 || ![string is double -strict [lindex $activity 0]]} { return "" }
  if {[lindex $activity 2] ni {vcd saif user propagated clock}} { return "" }
  return [lindex $activity 0]
}

proc pin_cap_ff {master pin_name} {
  set pins [get_lib_pins -quiet */${master}/${pin_name}]
  if {[llength $pins] != 1} { return "" }
  return [expr {[get_property [lindex $pins 0] capacitance] * 1000.0}]
}

proc apply_activity_pin_swaps {} {
  global block report_file
  set symmetric_groups [dict create \
    nand2 {{A B}} nor2 {{A B}} and2 {{A B}} or2 {{A B}} xor2 {{A B}} xnor2 {{A B}} \
    a21o {{A1 A2}} a21oi {{A1 A2}} a21boi {{A1 A2}} o21ai {{A1 A2}} o21bai {{A1 A2}} \
    a22oi {{A1 A2} {B1 B2}} o22ai {{A1 A2} {B1 B2}} o31ai {{A1 A2 A3}}]
  set selected_families {}
  if {[info exists ::env(AUDIT_PIN_SWAP_FAMILIES)] && $::env(AUDIT_PIN_SWAP_FAMILIES) ne ""} {
    set selected_families [split $::env(AUDIT_PIN_SWAP_FAMILIES) ","]
  }
  set names {}
  foreach inst [$block getInsts] { lappend names [$inst getName] }
  set swaps 0
  set eligible 0
  set hierarchy_skipped 0
  lassign [current_worst_slacks] start_setup start_hold
  set budget_s 5.0e-11
  foreach name $names {
    set inst [$block findInst $name]
    if {$inst eq "NULL"} { continue }
    # OpenDB's hierarchical instance pins are represented through module
    # terminals. Reconnecting their child ITerms as if they were flat nets can
    # silently bind an input to the wrong parent net. Limit this audit pass to
    # cells in the top block until hierarchical port remapping is implemented.
    if {[string first "/" $name] >= 0} { incr hierarchy_skipped; continue }
    set master [[$inst getMaster] getName]
    if {![regexp {^sky130_fd_sc_hd__(.+)_[0-9]+$} $master -> family]} { continue }
    if {![dict exists $symmetric_groups $family]} { continue }
    if {[llength $selected_families] > 0 && $family ni $selected_families} { continue }
    incr eligible
    set sta_cells [get_cells -quiet $name]
    if {[llength $sta_cells] != 1} { continue }
    set sta_pins [get_pins -quiet -of_objects $sta_cells]
    set pin_by_name [dict create]
    foreach sta_pin $sta_pins { dict set pin_by_name [get_property $sta_pin lib_pin_name] $sta_pin }
    foreach group [dict get $symmetric_groups $family] {
      set entries {}
      set valid 1
      foreach pin_name $group {
        if {![dict exists $pin_by_name $pin_name]} { set valid 0; break }
        set sta_pin [dict get $pin_by_name $pin_name]
        set density [pin_activity_density $sta_pin]
        set cap [pin_cap_ff $master $pin_name]
        set iterm [$inst findITerm $pin_name]
        if {$density eq "" || $cap eq "" || $iterm eq "NULL" || [$iterm getNet] eq "NULL"} {
          set valid 0; break
        }
        lappend entries [list $density $cap $pin_name $iterm [$iterm getNet]]
      }
      if {!$valid || [llength $entries] < 2} { continue }
      set net_names {}
      foreach entry $entries { lappend net_names [[lindex $entry 4] getName] }
      if {[llength [lsort -unique $net_names]] != [llength $entries]} { continue }
      set by_density [lsort -decreasing -real -index 0 $entries]
      set by_cap [lsort -real -index 1 $entries]
      set before 0.0
      set after 0.0
      set swap_plan {}
      for {set idx 0} {$idx < [llength $entries]} {incr idx} {
        set original [lindex $entries $idx]
        set desired_net [lindex [lindex $by_density $idx] 4]
        set desired_pin [lindex [lindex $by_cap $idx] 2]
        set desired_cap [lindex [lindex $by_cap $idx] 1]
        set current_cap [lindex $original 1]
        set density [lindex $original 0]
        set before [expr {$before + $density * $current_cap}]
        set after [expr {$after + $density * $desired_cap}]
        set desired_net_name [$desired_net getName]
        lappend swap_plan "[lindex [lindex $by_density $idx] 2]:$desired_net_name->$desired_pin"
      }
      if {$after >= $before - 1.0e-9} { continue }
      set before_power [design_total_power_watts]
      foreach entry $entries { [lindex $entry 3] disconnect }
      for {set idx 0} {$idx < [llength $entries]} {incr idx} {
        set desired_pin [lindex [lindex $by_cap $idx] 2]
        set desired_net [lindex [lindex $by_density $idx] 4]
        odb::dbITerm_connect [$inst findITerm $desired_pin] $desired_net
      }
      estimate_parasitics -placement
      lassign [current_worst_slacks] trial_setup trial_hold
      set after_power [design_total_power_watts]
      set slew_report [max_slew_violations]
      set timing_ok [expr {$trial_setup ne "" && $trial_hold ne "" &&
        ($start_setup - $trial_setup) <= $budget_s && ($start_hold - $trial_hold) <= $budget_s}]
      if {$timing_ok && $after_power < $before_power - 1.0e-15 && $slew_report eq ""} {
        puts $report_file "stage3b\tpin_swap\t$name/$family:proxy=$before->$after:power_before=$before_power:power_after=$after_power:[join $swap_plan ,]"
        incr swaps
      } else {
        foreach entry $entries { [lindex $entry 3] disconnect }
        foreach entry $entries {
          set pin_name [lindex $entry 2]
          set original_net [lindex $entry 4]
          odb::dbITerm_connect [$inst findITerm $pin_name] $original_net
        }
        estimate_parasitics -placement
        set slew_clean [expr {$slew_report eq ""}]
        puts "AUDIT_PIN_SWAP_REJECT instance=$name family=$family timing=$timing_ok power_drop=[expr {$before_power - $after_power}] slew_clean=$slew_clean"
      }
    }
  }
  puts "AUDIT_PIN_SWAP eligible_cells=$eligible applied_groups=$swaps hierarchical_instances_skipped=$hierarchy_skipped"
}

# Override the legacy level-based helpers only after the stage procedures have
# been defined; all live transforms below use graph-based endpoint signatures.
source [file join [file dirname [info script]] cl_ovps_clock_topology.tcl]
estimate_parasitics -placement
lassign [current_worst_slacks] before_setup before_hold
puts $report_file "baseline\tsetup_slack_s\t$before_setup"
puts $report_file "baseline\thold_slack_s\t$before_hold"
puts "AUDIT_PRETRANSFORM_SETUP_S=$before_setup"
puts "AUDIT_PRETRANSFORM_HOLD_S=$before_hold"
clock_cell_power before

set initial_clock_graph [validate_clock_polarity]
set initial_clock_gate_violations [clovps_clock_gate_violations]
set initial_slew_violations [max_slew_violations]
set timing_guard_ps 50.0
if {[info exists ::env(AUDIT_TIMING_GUARD_PS)]} { set timing_guard_ps $::env(AUDIT_TIMING_GUARD_PS) }
set prune_dummies 1
set replace_buffers 1
set downsize_cells 1
set swap_pins 1
foreach {env_name var_name} {AUDIT_PRUNE_DUMMIES prune_dummies AUDIT_SWAP_CLOCK_BUFFERS replace_buffers AUDIT_DOWNSIZE downsize_cells AUDIT_PIN_SWAP swap_pins} {
  if {[info exists ::env($env_name)]} { set $var_name $::env($env_name) }
}
if {$prune_dummies} {
  set dummy_names {}
  foreach inst [$block getInsts] {
    set master [[$inst getMaster] getName]
    lassign [clovps_master_kind $master] kind _output _phase
    if {($kind eq "DUMMY_LOAD" || [string match "clkload*" [$inst getName]]) && [clovps_dummy_output_unused $inst]} {
      lappend dummy_names [$inst getName]
    }
  }
  set dummy_removed 0
  foreach name $dummy_names {
    if {$initial_clock_gate_violations ne "" || $initial_slew_violations ne ""} {
      puts "AUDIT_DUMMY_PRUNE_SKIP instance=$name reason=baseline_clock_gate_or_slew_violation"
      continue
    }
    set inst [$block findInst $name]
    if {$inst eq "NULL"} { continue }
    if {[clovps_dummy_touches_macro_domain $name $initial_clock_graph]} {
      puts "AUDIT_DUMMY_PRUNE_SKIP instance=$name reason=macro_clock_domain"
      continue
    }
    set snapshot [clovps_snapshot_instance $name]
    lassign [current_worst_slacks] trial_start_setup trial_start_hold
    set power_before [clovps_dynamic_power_watts]
    remove_unused_dummy $name
    estimate_parasitics -placement
    lassign [current_worst_slacks] trial_setup trial_hold
    set power_after [clovps_dynamic_power_watts]
    set gate_report [clovps_clock_gate_violations]
    set slew_report [max_slew_violations]
    set timing_ok [clovps_timing_safe $before_setup $before_hold $trial_setup $trial_hold $timing_guard_ps]
    if {$timing_ok && $gate_report eq "" && $slew_report eq "" && $power_after < $power_before - 1.0e-15} {
      incr dummy_removed
      puts "AUDIT_DUMMY_PRUNE_ACCEPT instance=$name dynamic_delta_w=[expr {$power_after - $power_before}]"
    } else {
      clovps_restore_instance $snapshot
      estimate_parasitics -placement
      set gate_clean [expr {$gate_report eq ""}]
      set slew_clean [expr {$slew_report eq ""}]
      puts "AUDIT_DUMMY_PRUNE_REJECT instance=$name timing=$timing_ok gate_clean=$gate_clean slew_clean=$slew_clean dynamic_delta_w=[expr {$power_after - $power_before}]"
    }
    clovps_assert_same_clock_graph $initial_clock_graph [validate_clock_polarity] "dummy-prune-trial"
  }
  puts "AUDIT_DUMMY_PRUNE removed=$dummy_removed candidates=[llength $dummy_names]"
} else {
  puts "AUDIT_DUMMY_PRUNE removed=0 disabled=1"
}

if {$replace_buffers} {
  if {$initial_clock_gate_violations ne "" || $initial_slew_violations ne ""} {
    puts "AUDIT_CLOCK_REPLACE stars=0 reason=baseline_clock_gate_or_slew_violation"
  } else {
    set legal_stars [clovps_legal_stars]
    set weighted_stars {}
    foreach star $legal_stars {
      set weight [clovps_measure_star_weight $star]
      dict set star weight $weight
      if {$weight > 1.0e-15} { lappend weighted_stars $star }
    }
    set selected_stars [clovps_select_disjoint_stars $weighted_stars]
    set selected_stars [lsort -command clovps_star_weight_compare $selected_stars]
    set committed_stars 0
    set committed_buffers 0
    foreach star $selected_stars {
      set parent [dict get $star parent]
      set children [dict get $star children]
      if {[catch {set snapshots [clovps_replace_star_atomic $parent $children]} replace_error]} {
        puts "AUDIT_CLOCK_STAR_REJECT parent=$parent reason=atomic_replace_error"
        continue
      }
      estimate_parasitics -placement
      lassign [current_worst_slacks] trial_setup trial_hold
      set gate_report [clovps_clock_gate_violations]
      set slew_report [max_slew_violations]
      set timing_ok [clovps_timing_safe $before_setup $before_hold $trial_setup $trial_hold $timing_guard_ps]
      set graph_ok 1
      if {[catch {clovps_assert_same_clock_graph $initial_clock_graph [validate_clock_polarity] "star-$parent"} graph_error]} {
        set graph_ok 0
      }
      if {$timing_ok && $gate_report eq "" && $slew_report eq "" && $graph_ok} {
        incr committed_stars
        incr committed_buffers [expr {1 + [llength $children]}]
        puts "AUDIT_CLOCK_STAR_ACCEPT parent=$parent children=[join $children ,] predicted_dynamic_delta_w=[dict get $star weight]"
      } else {
        clovps_restore_star $snapshots
        estimate_parasitics -placement
        set gate_clean [expr {$gate_report eq ""}]
        set slew_clean [expr {$slew_report eq ""}]
        puts "AUDIT_CLOCK_STAR_REJECT parent=$parent timing=$timing_ok gate_clean=$gate_clean slew_clean=$slew_clean graph=$graph_ok"
      }
    }
    puts "AUDIT_CLOCK_REPLACE stars=$committed_stars buffers=$committed_buffers legal_stars=[llength $legal_stars] scored=[llength $weighted_stars] selected=[llength $selected_stars]"
  }
} else {
  puts "AUDIT_CLOCK_REPLACE stars=0 disabled=1"
}

if {$downsize_cells} {
  set downsize_mode "local"
  if {[info exists ::env(AUDIT_DOWNSIZE_MODE)]} { set downsize_mode $::env(AUDIT_DOWNSIZE_MODE) }
  if {$downsize_mode eq "local"} {
    downsize_slack_safe_cells
  } elseif {$downsize_mode eq "budget"} {
    set budget_ps 50.0
    set max_cells 40
    set local_slack_ps 50.0
    if {[info exists ::env(AUDIT_DOWNSIZE_BUDGET_PS)]} { set budget_ps $::env(AUDIT_DOWNSIZE_BUDGET_PS) }
    if {[info exists ::env(AUDIT_DOWNSIZE_MAX_CELLS)]} { set max_cells $::env(AUDIT_DOWNSIZE_MAX_CELLS) }
    if {[info exists ::env(AUDIT_DOWNSIZE_MIN_SLACK_PS)]} { set local_slack_ps $::env(AUDIT_DOWNSIZE_MIN_SLACK_PS) }
    downsize_with_global_timing_budget $budget_ps $max_cells $local_slack_ps
  } else {
    error "unsupported AUDIT_DOWNSIZE_MODE '$downsize_mode'"
  }
} else {
  puts "AUDIT_DOWNSIZE resized=0 disabled=1"
}
if {$swap_pins && $activity_mode eq "vcd"} {
  apply_activity_pin_swaps
} else {
  puts "AUDIT_PIN_SWAP applied_groups=0 disabled=1 reason=[expr {$activity_mode eq "vcd" ? "configured" : "uniform_fallback_has_no_pin_ranking"}]"
}

estimate_parasitics -placement
if {[catch {detailed_placement} legalization_error]} {
  error "post-transform legalization failed: $legalization_error"
}
estimate_parasitics -placement
if {[catch {check_placement -verbose} placement_error]} {
  error "post-transform placement check failed: $placement_error"
}
puts "AUDIT_PLACEMENT_CHECK=PASS"
if {[info exists ::env(AUDIT_CHECK_PDN)] && $::env(AUDIT_CHECK_PDN)} {
  foreach power_net {VDD VSS} {
    if {[catch {check_power_grid -net $power_net} pdn_error]} {
      error "post-transform $power_net connectivity check failed: $pdn_error"
    }
    puts "AUDIT_PSM_CHECK net=$power_net status=PASS"
  }
}
lassign [current_worst_slacks] after_setup after_hold
clovps_assert_same_clock_graph $initial_clock_graph [validate_clock_polarity] "final-stage23"
set final_clock_gate_violations [clovps_clock_gate_violations]
set final_slew_violations [max_slew_violations]
set clock_gate_ok 0
if {$initial_clock_gate_violations eq "" && $final_clock_gate_violations eq ""} { set clock_gate_ok 1 }
if {$initial_clock_gate_violations ne "" && $initial_clock_gate_violations eq $final_clock_gate_violations} { set clock_gate_ok 1 }
set slew_ok 0
if {$initial_slew_violations eq "" && $final_slew_violations eq ""} { set slew_ok 1 }
if {$initial_slew_violations ne "" && $initial_slew_violations eq $final_slew_violations} { set slew_ok 1 }
set setup_delta [expr {($after_setup - $before_setup) * 1.0e9}]
set hold_delta [expr {($after_hold - $before_hold) * 1.0e9}]
puts $report_file "audit\tsetup_delta_ps\t$setup_delta"
puts $report_file "audit\thold_delta_ps\t$hold_delta"
puts "AUDIT_POSTTRANSFORM_SETUP_S=$after_setup"
puts "AUDIT_POSTTRANSFORM_HOLD_S=$after_hold"
puts "AUDIT_PRE_ROUTE_TIMING_DELTA_PS setup=$setup_delta hold=$hold_delta limit=50"
set clock_gate_status [expr {$clock_gate_ok ? "PASS" : "FAIL"}]
set slew_status [expr {$slew_ok ? "PASS" : "FAIL"}]
puts "AUDIT_CLOCK_GATING_CHECK status=$clock_gate_status"
puts "AUDIT_MAX_SLEW_CHECK status=$slew_status"
clock_cell_power after
report_power -digits 12

write_db $::env(AUDIT_OUT_ODB)
close $report_file
set setup_floor_ok [expr {$before_setup < 0.0 || $after_setup >= 0.0}]
if {$setup_delta < -$timing_guard_ps || $hold_delta < -$timing_guard_ps || !$setup_floor_ok || !$clock_gate_ok || !$slew_ok} {
  puts "AUDIT_PREFLIGHT_GATE=FAIL transformed ODB saved for inspection, route not authorized by timing gate"
  error "pre-route timing, absolute setup floor, clock-gating, or slew guard failed"
}
puts "AUDIT_PREFLIGHT_GATE=PASS output=$::env(AUDIT_OUT_ODB)"
exit
