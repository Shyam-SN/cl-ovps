# Topology-proven clock edits for the Sky130HD post-CTS flow.
# This file is sourced by cl_ovps_sky130hd_stage23.tcl.

proc clovps_master_kind {master} {
  if {[regexp {__(?:clkbuf|bufbuf|buf)_[0-9]+$} $master]} { return [list PURE_CLK_BUF X 0] }
  if {[regexp {__(?:clkinv|inv)_[0-9]+$} $master]} { return [list PURE_CLK_INV Y 1] }
  if {[regexp {__clkload_[0-9]+$} $master]} { return [list DUMMY_LOAD X 0] }
  return [list STOP_BOUNDARY "" 0]
}

proc clovps_clock_root_trace {net seen} {
  if {$net eq "NULL"} { error "clock graph contains an unconnected pin" }
  set net_name [$net getName]
  if {[dict exists $seen $net_name]} { error "cycle in clock graph at net $net_name" }
  dict set seen $net_name 1
  set drivers {}
  foreach iterm [$net getITerms] {
    set mterm [$iterm getMTerm]
    if {[$mterm getIoType] eq "OUTPUT"} { lappend drivers $iterm }
  }
  if {[llength $drivers] > 1} { error "ambiguous multiple drivers on clock net $net_name" }
  if {[llength $drivers] == 1} {
    set iterm [lindex $drivers 0]
    set inst [$iterm getInst]
    set pin [[$iterm getMTerm] getName]
    set master [[$inst getMaster] getName]
    lassign [clovps_master_kind $master] kind output_pin invert
    if {$kind eq "PURE_CLK_BUF" || $kind eq "PURE_CLK_INV"} {
      if {$pin ne $output_pin} { error "unexpected $master clock output pin $pin" }
      set input [$inst findITerm A]
      if {$input eq "NULL"} { error "clock transport [$inst getName] has no A input" }
      lassign [clovps_clock_root_trace [$input getNet] $seen] root parity path
      return [list $root [expr {($parity + $invert) % 2}] [concat $path [$inst getName]]]
    }
    return [list "inst:[$inst getName]/$pin" 0 {}]
  }
  set ports {}
  foreach bterm [$net getBTerms] {
    if {[$bterm getIoType] eq "INPUT"} { lappend ports [$bterm getName] }
  }
  if {[llength $ports] > 1} { error "multiple primary clock roots on net $net_name" }
  if {[llength $ports] == 1} { return [list "port:[lindex $ports 0]" 0 {}] }
  error "clock net $net_name has no recognized driver or primary input root"
}

proc clovps_is_block_master {inst} {
  set master [$inst getMaster]
  if {![catch {set type [$master getType]}] && [string match -nocase *BLOCK* $type]} { return 1 }
  if {![catch {set type [$inst getMasterType]}] && [string match -nocase *BLOCK* $type]} { return 1 }
  set name [$master getName]
  return [regexp -nocase {(sram|macro|mem|register.?file|(^|__)ram|(^|__)rf($|[_0-9]))} $name]
}

proc clovps_is_macro_clock_pin {pin_name} {
  return [regexp -nocase {^(CLK([_]?N|[0-9]+|[AB])?|CLOCK|CK([AB][0-9]*)?|RCLK|WCLK|CLKIN)$} $pin_name]
}

proc validate_clock_polarity {} {
  global block
  set signatures [dict create]
  set sinks 0
  foreach inst [$block getInsts] {
    set inst_name [$inst getName]
    set master_name [[$inst getMaster] getName]
    set is_icg [regexp -nocase {(__dlclkp_|__sdlclkp_|__.*clkgate|__.*icg)} $master_name]
    foreach iterm [$inst getITerms] {
      set mterm [$iterm getMTerm]
      if {[$mterm getIoType] ne "INPUT"} { continue }
      set pin_name [$mterm getName]
      set is_clock 0
      set lib_pins [get_lib_pins -quiet */${master_name}/${pin_name}]
      if {[llength $lib_pins] == 1} {
        if {![catch {set is_register_clock [get_property -quiet [lindex $lib_pins 0] is_register_clock]}] && $is_register_clock eq "1"} {
          set is_clock 1
        }
        if {!$is_clock && ![catch {set liberty_clock [get_property -quiet [lindex $lib_pins 0] is_clock]}] && $liberty_clock eq "1"} {
          set is_clock 1
        }
      }
      if {$is_icg && $pin_name eq "CLK"} { set is_clock 1 }
      # Treat every macro input as a graph boundary. This is intentionally
      # conservative when a memory view uses a nonstandard clock-pin name.
      if {!$is_clock && [clovps_is_block_master $inst]} {
        set is_clock 1
      }
      if {!$is_clock} { continue }
      set net [$iterm getNet]
      if {$net eq "NULL"} { error "clock boundary $inst_name/$pin_name has no net" }
      lassign [clovps_clock_root_trace $net [dict create]] root parity _path
      set key "$inst_name/$pin_name"
      if {[dict exists $signatures $key]} { error "duplicate clock endpoint $key" }
      set boundary sequential
      if {$is_icg} { set boundary icg }
      if {[clovps_is_block_master $inst]} { set boundary macro }
      dict set signatures $key [list $root $parity $boundary]
      incr sinks
    }
  }
  if {$sinks == 0} { error "no sequential, ICG, or macro clock boundaries could be classified" }
  puts "AUDIT_CLOCK_GRAPH endpoints=$sinks pure_transport=verified"
  return $signatures
}

proc clovps_assert_same_clock_graph {reference current context} {
  if {[dict size $reference] != [dict size $current]} {
    error "$context changed clock endpoint count ([dict size $reference] -> [dict size $current])"
  }
  dict for {endpoint before} $reference {
    if {![dict exists $current $endpoint]} { error "$context removed clock endpoint $endpoint" }
    set after [dict get $current $endpoint]
    if {$before ne $after} { error "$context changed clock root/phase at $endpoint ($before -> $after)" }
  }
  puts "AUDIT_CLOCK_GRAPH_CHECK context=$context endpoints=[dict size $reference] status=PASS"
}

proc clovps_dummy_output_unused {inst} {
  set outputs {}
  foreach iterm [$inst getITerms] {
    set mterm [$iterm getMTerm]
    if {[$mterm getIoType] eq "OUTPUT"} { lappend outputs $iterm }
  }
  if {[llength $outputs] != 1} { return 0 }
  set output [lindex $outputs 0]
  set net [$output getNet]
  if {$net eq "NULL"} { return 1 }
  foreach iterm [$net getITerms] {
    if {[$iterm getInst] eq $inst && [[$iterm getMTerm] getName] eq [[$output getMTerm] getName]} { continue }
    return 0
  }
  if {[llength [$net getBTerms]] > 0} { return 0 }
  return 1
}

proc remove_unused_dummy {inst_name} {
  global block
  set inst [$block findInst $inst_name]
  if {$inst eq "NULL"} { error "dummy cell disappeared: $inst_name" }
  if {![clovps_dummy_output_unused $inst]} {
    error "$inst_name output is connected to a used net; refusing to delete it"
  }
  odb::dbInst_destroy $inst
}

proc clovps_dummy_touches_macro_domain {inst signatures} {
  global block
  set dummy [$block findInst $inst]
  set input [$dummy findITerm A]
  if {$input eq "NULL" || [$input getNet] eq "NULL"} { return 0 }
  lassign [clovps_clock_root_trace [$input getNet] [dict create]] root _parity _path
  dict for {_endpoint signature} $signatures {
    if {[lindex $signature 2] eq "macro" && [lindex $signature 0] eq $root} { return 1 }
  }
  return 0
}

proc clovps_star_weight_compare {left right} {
  set left_weight [dict get $left weight]
  set right_weight [dict get $right weight]
  if {$left_weight > $right_weight} { return -1 }
  if {$left_weight < $right_weight} { return 1 }
  return [string compare [dict get $left parent] [dict get $right parent]]
}

proc clovps_clock_gate_violations {} {
  if {[catch {with_output_to_variable report {
      report_check_types -clock_gating_setup -clock_gating_hold -violators -format slack_only
    }} message]} {
    error "could not evaluate clock-gating setup/hold checks: $message"
  }
  set report [string trim $report]
  if {[regexp -nocase {no paths found|no clock.gating checks|0 violating paths} $report]} { return "" }
  return $report
}

proc clovps_dynamic_power_watts {} {
  set power [sta::design_power [sta::cmd_scene]]
  if {[llength $power] < 2} { error "OpenSTA did not return dynamic power components" }
  return [expr {[lindex $power 0] + [lindex $power 1]}]
}

proc clovps_snapshot_instance {inst_name} {
  global block
  set inst [$block findInst $inst_name]
  if {$inst eq "NULL"} { error "clock instance disappeared: $inst_name" }
  set pins [dict create]
  foreach iterm [$inst getITerms] {
    set net [$iterm getNet]
    if {$net ne "NULL"} { dict set pins [[$iterm getMTerm] getName] [$net getName] }
  }
  lassign [$inst getLocation] x y
  return [dict create name $inst_name master [[$inst getMaster] getName] x $x y $y \
    orient [$inst getOrient] status [$inst getPlacementStatus] pins $pins]
}

proc clovps_replace_instance_master {inst_name new_master_name} {
  global block db
  set inst [$block findInst $inst_name]
  if {$inst eq "NULL"} { error "clock instance disappeared: $inst_name" }
  set old_master [[$inst getMaster] getName]
  set target [$db findMaster $new_master_name]
  if {$target eq "NULL"} { error "replacement master is missing: $new_master_name" }
  set snapshot [clovps_snapshot_instance $inst_name]
  set old_pins [dict get $snapshot pins]
  set x [dict get $snapshot x]
  set y [dict get $snapshot y]
  set orient [dict get $snapshot orient]
  set status [dict get $snapshot status]
  odb::dbInst_destroy $inst
  set replacement [odb::dbInst_create $block $target $inst_name]
  $replacement setLocation $x $y
  $replacement setOrient $orient
  $replacement setPlacementStatus $status
  dict for {old_pin net_name} $old_pins {
    set new_pin $old_pin
    if {$old_pin eq "X"} { set new_pin Y }
    if {$old_pin eq "Y"} { set new_pin X }
    set new_iterm [$replacement findITerm $new_pin]
    if {$new_iterm eq "NULL"} { error "$new_master_name lacks mapped pin $new_pin (from $old_master)" }
    set net [$block findNet $net_name]
    if {$net eq "NULL"} { error "net $net_name disappeared while replacing $inst_name" }
    odb::dbITerm_connect $new_iterm $net
  }
}

proc clovps_restore_instance {snapshot} {
  global block db
  set name [dict get $snapshot name]
  set existing [$block findInst $name]
  if {$existing ne "NULL"} { odb::dbInst_destroy $existing }
  set master [$db findMaster [dict get $snapshot master]]
  if {$master eq "NULL"} { error "cannot restore missing master [dict get $snapshot master]" }
  set inst [odb::dbInst_create $block $master $name]
  $inst setLocation [dict get $snapshot x] [dict get $snapshot y]
  $inst setOrient [dict get $snapshot orient]
  $inst setPlacementStatus [dict get $snapshot status]
  dict for {pin net_name} [dict get $snapshot pins] {
    set iterm [$inst findITerm $pin]
    set net [$block findNet $net_name]
    if {$iterm eq "NULL" || $net eq "NULL"} { error "failed to restore $name/$pin" }
    odb::dbITerm_connect $iterm $net
  }
}

proc clovps_inverter_master_for {master} {
  if {[regexp {__(?:clkbuf|bufbuf|buf)_([0-9]+)$} $master -> strength]} {
    if {$strength >= 8} { return sky130_fd_sc_hd__clkinv_4 }
    if {$strength >= 2} { return sky130_fd_sc_hd__clkinv_2 }
    return sky130_fd_sc_hd__clkinv_1
  }
  error "unsupported pure clock buffer master $master"
}

proc clovps_replace_star_atomic {parent children} {
  global block
  set names [concat [list $parent] $children]
  set snapshots {}
  foreach name $names { lappend snapshots [clovps_snapshot_instance $name] }
  set status [catch {
    foreach name $names {
      set inst [$block findInst $name]
      set master [[$inst getMaster] getName]
      clovps_replace_instance_master $name [clovps_inverter_master_for $master]
    }
  } message options]
  if {$status} {
    foreach snapshot [lreverse $snapshots] {
      if {[catch {clovps_restore_instance $snapshot} restore_error]} {
        error "star replacement failed ($message) and rollback failed ($restore_error)"
      }
    }
    return -options $options $message
  }
  return $snapshots
}

proc clovps_restore_star {snapshots} {
  foreach snapshot [lreverse $snapshots] { clovps_restore_instance $snapshot }
}

proc clovps_clock_transport_instances {} {
  global block
  set transport [dict create]
  foreach inst [$block getInsts] {
    set master_name [[$inst getMaster] getName]
    set is_icg [regexp -nocase {(__dlclkp_|__sdlclkp_|__.*clkgate|__.*icg)} $master_name]
    foreach iterm [$inst getITerms] {
      set mterm [$iterm getMTerm]
      if {[$mterm getIoType] ne "INPUT"} { continue }
      set pin_name [$mterm getName]
      set is_clock 0
      set lib_pins [get_lib_pins -quiet */${master_name}/${pin_name}]
      if {[llength $lib_pins] == 1} {
        if {![catch {set register_clock [get_property -quiet [lindex $lib_pins 0] is_register_clock]}] && $register_clock eq "1"} {
          set is_clock 1
        }
        if {!$is_clock && ![catch {set liberty_clock [get_property -quiet [lindex $lib_pins 0] is_clock]}] && $liberty_clock eq "1"} {
          set is_clock 1
        }
      }
      if {$is_icg && $pin_name eq "CLK"} { set is_clock 1 }
      if {!$is_clock && [clovps_is_block_master $inst] && [clovps_is_macro_clock_pin $pin_name]} {
        set is_clock 1
      }
      if {!$is_clock} { continue }
      set net [$iterm getNet]
      if {$net eq "NULL"} { error "clock boundary [$inst getName]/$pin_name has no net" }
      lassign [clovps_clock_root_trace $net [dict create]] _root _parity path
      foreach node $path { dict set transport $node 1 }
    }
  }
  if {[dict size $transport] == 0} { error "no pure clock transport instances were traced" }
  return $transport
}

proc clovps_legal_stars {} {
  global block
  set stars {}
  # Buffer-shaped signal logic is not clock topology. A cell is eligible only
  # when the graph tracer found it on a clock source-to-boundary path.
  set clock_transport [clovps_clock_transport_instances]
  foreach parent [$block getInsts] {
    set parent_master [[$parent getMaster] getName]
    lassign [clovps_master_kind $parent_master] parent_kind parent_output _invert
    if {$parent_kind ne "PURE_CLK_BUF"} { continue }
    set parent_name [$parent getName]
    if {![dict exists $clock_transport $parent_name]} { continue }
    set output [$parent findITerm $parent_output]
    if {$output eq "NULL"} { continue }
    set net [$output getNet]
    if {$net eq "NULL"} { continue }
    if {[llength [$net getBTerms]] > 0} { continue }
    set children {}
    set legal 1
    foreach sink [$net getITerms] {
      set inst [$sink getInst]
      set sink_name [$inst getName]
      set pin [[$sink getMTerm] getName]
      if {$sink_name eq $parent_name && $pin eq $parent_output} { continue }
      set master [[$inst getMaster] getName]
      lassign [clovps_master_kind $master] kind _output _phase
      if {[string match "clkload*" $sink_name]} { set kind DUMMY_LOAD }
      if {$kind eq "DUMMY_LOAD" && $pin eq "A"} { continue }
      if {$kind eq "PURE_CLK_BUF" && $pin eq "A" && [dict exists $clock_transport $sink_name]} {
        lappend children $sink_name
      } else {
        set legal 0
        break
      }
    }
    if {!$legal || [llength $children] == 0} { continue }
    set vertices [concat [list $parent_name] $children]
    if {[llength [lsort -unique $vertices]] != [llength $vertices]} { continue }
    lappend stars [dict create parent $parent_name children [lsort -unique $children] \
      vertices $vertices weight 0.0]
  }
  return $stars
}

proc clovps_measure_star_weight {star} {
  set before [clovps_dynamic_power_watts]
  set snapshots [clovps_replace_star_atomic [dict get $star parent] [dict get $star children]]
  set status [catch {
    estimate_parasitics -placement
    set after [clovps_dynamic_power_watts]
  } message options]
  clovps_restore_star $snapshots
  estimate_parasitics -placement
  if {$status} { return -options $options $message }
  return [expr {$before - $after}]
}

proc clovps_intersects {left right} {
  foreach item $left { if {[lsearch -exact $right $item] >= 0} { return 1 } }
  return 0
}

proc clovps_mwis_dp {node parent adjacency weights inVar outVar visitingVar} {
  upvar 1 $inVar best_in $outVar best_out $visitingVar visiting
  if {[dict exists $visiting $node]} { error "star conflict graph is not a forest" }
  dict set visiting $node 1
  set include [dict get $weights $node]
  set exclude 0.0
  foreach child [dict get $adjacency $node] {
    if {$child eq $parent} { continue }
    lassign [clovps_mwis_dp $child $node $adjacency $weights best_in best_out visiting] child_in child_out
    set include [expr {$include + $child_out}]
    set exclude [expr {$exclude + max($child_in, $child_out)}]
  }
  dict unset visiting $node
  dict set best_in $node $include
  dict set best_out $node $exclude
  return [list $include $exclude]
}

proc clovps_mwis_backtrack {node parent take adjacency best_in best_out selectedVar} {
  upvar 1 $selectedVar selected
  if {$take} { lappend selected $node }
  foreach child [dict get $adjacency $node] {
    if {$child eq $parent} { continue }
    if {$take} {
      set child_take 0
    } else {
      set child_take [expr {[dict get $best_in $child] > [dict get $best_out $child]}]
    }
    clovps_mwis_backtrack $child $node $child_take $adjacency $best_in $best_out selected
  }
}

proc clovps_select_disjoint_stars {stars} {
  set ids {}
  set weights [dict create]
  set adjacency [dict create]
  set star_by_id [dict create]
  set index 0
  foreach star $stars {
    set weight [dict get $star weight]
    if {$weight <= 1.0e-15} { continue }
    set id "S$index"
    incr index
    lappend ids $id
    dict set weights $id $weight
    dict set adjacency $id {}
    dict set star_by_id $id $star
  }
  foreach id $ids {
    foreach other $ids {
      if {$id eq $other} { continue }
      if {[clovps_intersects [dict get $star_by_id $id vertices] [dict get $star_by_id $other vertices]]} {
        set row [dict get $adjacency $id]
        if {[lsearch -exact $row $other] < 0} { lappend row $other; dict set adjacency $id $row }
      }
    }
  }
  set visited [dict create]
  set best_in [dict create]
  set best_out [dict create]
  set selected {}
  foreach id $ids {
    if {[dict exists $visited $id]} { continue }
    set component {}
    set stack [list $id]
    while {[llength $stack]} {
      set current [lindex $stack end]
      set stack [lrange $stack 0 end-1]
      if {[dict exists $visited $current]} { continue }
      dict set visited $current 1
      lappend component $current
      foreach neighbor [dict get $adjacency $current] { lappend stack $neighbor }
    }
    set root [lindex $component 0]
    set recursion_seen [dict create]
    clovps_mwis_dp $root "" $adjacency $weights best_in best_out recursion_seen
    set take [expr {[dict get $best_in $root] > [dict get $best_out $root]}]
    clovps_mwis_backtrack $root "" $take $adjacency $best_in $best_out selected
  }
  set result {}
  foreach id $selected { lappend result [dict get $star_by_id $id] }
  return $result
}

proc clovps_timing_safe {start_setup start_hold trial_setup trial_hold budget_ps} {
  set budget_s [expr {$budget_ps * 1.0e-12}]
  if {$trial_setup eq "" || $trial_hold eq ""} { return 0 }
  if {($start_setup - $trial_setup) > $budget_s || ($start_hold - $trial_hold) > $budget_s} { return 0 }
  if {$start_setup >= 0.0 && $trial_setup < 0.0} { return 0 }
  return 1
}

