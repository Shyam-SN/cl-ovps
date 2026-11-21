# Generic CL-OVPS OpenROAD profiler and routed power measurement pass.
# Paths and activity scope are supplied by run_cl_ovps_universal.py.

foreach required {CL_OVPS_ODB CL_OVPS_SDC CL_OVPS_LIBS} {
  if {![info exists ::env($required)] || $::env($required) eq ""} {
    error "required environment variable $required is not set"
  }
}

foreach liberty [split $::env(CL_OVPS_LIBS) "\n"] {
  if {$liberty ne ""} { read_liberty $liberty }
}
read_db $::env(CL_OVPS_ODB)
read_sdc $::env(CL_OVPS_SDC)
if {[info exists ::env(CL_OVPS_RC_TCL)] && $::env(CL_OVPS_RC_TCL) ne ""} {
  source $::env(CL_OVPS_RC_TCL)
}
if {[info exists ::env(CL_OVPS_UPF)] && $::env(CL_OVPS_UPF) ne ""} {
  source [file join [file dirname [info script]] cl_ovps_load_upf.tcl]
  cl_ovps_load_upf $::env(CL_OVPS_UPF)
}
if {[info exists ::env(CL_OVPS_SPEF)] && $::env(CL_OVPS_SPEF) ne ""} {
  read_spef $::env(CL_OVPS_SPEF)
} else {
  estimate_parasitics -placement
}
set_thread_count 1
set activity_mode vcd
if {[info exists ::env(CL_OVPS_ACTIVITY_MODE)]} { set activity_mode $::env(CL_OVPS_ACTIVITY_MODE) }
if {$activity_mode eq "vcd"} {
  foreach required {CL_OVPS_VCD CL_OVPS_VCD_SCOPE} {
    if {![info exists ::env($required)] || $::env($required) eq ""} {
      error "$required is required for VCD activity"
    }
  }
  read_vcd -scope $::env(CL_OVPS_VCD_SCOPE) $::env(CL_OVPS_VCD)
  puts "CL_OVPS_ACTIVITY_POLICY source=vcd"
} elseif {$activity_mode eq "global_default"} {
  set activity $::env(CL_OVPS_DEFAULT_ACTIVITY)
  set_power_activity -global -activity $activity -duty 0.5
  puts "CL_OVPS_ACTIVITY_POLICY workload=$::env(CL_OVPS_WORKLOAD) source=global_default activity=$activity duty=0.5"
} else {
  error "unsupported CL_OVPS_ACTIVITY_MODE '$activity_mode'"
}

# Routed antenna cells do not appear in functional gate simulations. Transfer
# their connected net activity to their physical-only DIODE pin, including zero.
set diode_activity_annotated 0
foreach cell [get_cells -hierarchical -quiet *] {
  set master [get_property -quiet $cell ref_name]
  if {![string match "*diode*" [string tolower $master]]} { continue }
  foreach pin [get_pins -quiet -of_objects $cell] {
    if {[get_property -quiet $pin lib_pin_name] ne "DIODE"} { continue }
    set activity [get_property -quiet $pin activity]
    if {[llength $activity] < 2 || ![string is double -strict [lindex $activity 0]] ||
        ![string is double -strict [lindex $activity 1]]} { continue }
    set density_per_second [lindex $activity 0]
    set density_per_time_unit [expr {$density_per_second * [sta::time_ui_sta 1.0]}]
    set_power_activity -pins $pin -density $density_per_time_unit -duty [lindex $activity 1]
    incr diode_activity_annotated
  }
}

report_activity_annotation -report_unannotated
puts "CL_OVPS_DIODE_ACTIVITY annotated=$diode_activity_annotated"

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
set top [$block getName]
set dbu_per_micron [$block getDbUnitsPerMicron]
set seq_sinks 0
set cts_buffers 0
set cts_inverters 0
set cts_dummies 0
set clock_nets [dict create]
foreach inst [$block getInsts] {
  set instance_name [$inst getName]
  set master [[$inst getMaster] getName]
  if {[string match "clkload*" $instance_name]} { incr cts_dummies }
  if {[string match "clkbuf*" $instance_name]} {
    if {[string match "*__clkinv_*" $master]} { incr cts_inverters } else { incr cts_buffers }
  } elseif {[string match "clkinv*" $instance_name]} {
    incr cts_inverters
  }
  set is_seq 0
  foreach iterm [$inst getITerms] {
    set mterm [$iterm getMTerm]
    if {[$mterm getIoType] ne "INPUT"} { continue }
    set lib_pin [get_lib_pins -quiet */${master}/[$mterm getName]]
    if {[llength $lib_pin] != 1 || [get_property -quiet [lindex $lib_pin 0] is_register_clock] ne "1"} {
      continue
    }
    set is_seq 1
    set net [$iterm getNet]
    if {$net ne "NULL"} { dict set clock_nets [$net getName] 1 }
  }
  if {$is_seq} { incr seq_sinks }
}

set signal_hpwl_dbu 0
foreach net [$block getNets] {
  set type [$net getSigType]
  if {$type eq "POWER" || $type eq "GROUND"} { continue }
  set xs {}
  set ys {}
  foreach iterm [$net getITerms] {
    lassign [$iterm getAvgXY] valid x y
    if {$valid} { lappend xs $x; lappend ys $y }
  }
  foreach bterm [$net getBTerms] {
    set box [$bterm getBBox]
    if {$box ne "NULL"} {
      lappend xs [expr {([$box xMin] + [$box xMax]) / 2}]
      lappend ys [expr {([$box yMin] + [$box yMax]) / 2}]
    }
  }
  if {[llength $xs] < 2} { continue }
  set xmin [lindex $xs 0]; set xmax $xmin
  set ymin [lindex $ys 0]; set ymax $ymin
  foreach x $xs y $ys {
    if {$x < $xmin} { set xmin $x }
    if {$x > $xmax} { set xmax $x }
    if {$y < $ymin} { set ymin $y }
    if {$y > $ymax} { set ymax $y }
  }
  incr signal_hpwl_dbu [expr {($xmax - $xmin) + ($ymax - $ymin)}]
}

set clocks [all_clocks]
set clock_names {}
foreach clock $clocks { lappend clock_names [get_name $clock] }
set clock_names [lsort -unique $clock_names]
set setup_ns [expr {[sta::worst_slack_cmd max] * 1.0e9}]
set hold_ns [expr {[sta::worst_slack_cmd min] * 1.0e9}]
set tns_ns NA
if {![catch {sta::total_negative_slack_cmd max} tns_value]} {
  set tns_ns [expr {$tns_value * 1.0e9}]
}
set scene [sta::cmd_scene]
set power [sta::design_power $scene]
set internal_w [lindex $power 0]
set switching_w [lindex $power 1]
set leakage_w [lindex $power 2]
set total_w [expr {$internal_w + $switching_w + $leakage_w}]

puts [format "CL_OVPS_PROFILE top=%s seq_sinks=%d clock_domains=%d cts_buffers=%d cts_inverters=%d cts_dummies=%d signal_hpwl_dbu=%d dbu_per_micron=%d setup_ns=%.12g hold_ns=%.12g tns_ns=%s clocks=%s" \
  $top $seq_sinks [llength $clock_names] $cts_buffers $cts_inverters $cts_dummies \
  $signal_hpwl_dbu $dbu_per_micron $setup_ns $hold_ns $tns_ns [join $clock_names ,]]
puts [format "CL_OVPS_POWER internal_w=%.12e switching_w=%.12e leakage_w=%.12e total_w=%.12e" \
  $internal_w $switching_w $leakage_w $total_w]
report_power -digits 12
report_checks -path_delay max -group_path_count 1 -digits 6
report_checks -path_delay min -group_path_count 1 -digits 6
exit
