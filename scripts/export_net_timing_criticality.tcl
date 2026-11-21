# Export placement-stage setup timing criticality by signal net.
# Inputs: TIMING_ODB, TIMING_SDC, TIMING_FEATURE_OUT.

foreach required {TIMING_ODB TIMING_SDC TIMING_FEATURE_OUT} {
  if {![info exists ::env($required)]} {
    error "required environment variable $required is not set"
  }
}

set project_root [file normalize [file join [file dirname [info script]] ..]]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $::env(TIMING_ODB)
read_sdc $::env(TIMING_SDC)
source [file join $platform_dir setRC.tcl]
estimate_parasitics -placement
sta::find_timing

set period_native 0.0
foreach clock [get_clocks *] {
  set period [get_property $clock period]
  if {$period ne "INF" && $period ne "-INF" && $period > $period_native} {
    set period_native $period
  }
}
if {$period_native <= 0.0} {
  error "no finite clock period found for timing criticality normalization"
}
set period_ns $period_native

proc canonical_name {name} {
  return [string map [list {\[} {[} {\]} {]}] $name]
}

proc finite_slack_ns {pin} {
  if {[catch {get_property $pin slack_max} slack]} { return "" }
  set normalized [string tolower $slack]
  if {$normalized eq "inf" || $normalized eq "+inf" || $normalized eq "-inf" || $normalized eq "nan"} {
    return ""
  }
  return $slack
}

set output [open $::env(TIMING_FEATURE_OUT) w]
puts $output "net\tsetup_min_sink_slack_ns\tsetup_mean_sink_slack_ns\tsetup_criticality\tconstrained_sink_count"
set rows [dict create]
foreach net [get_nets -hierarchical *] {
  set name [canonical_name [get_full_name $net]]
  set slacks {}
  foreach pin [get_pins -quiet -of_objects $net -filter "direction==input"] {
    set slack [finite_slack_ns $pin]
    if {$slack ne ""} { lappend slacks $slack }
  }
  # Use instance pins here. OpenROAD's get_ports -of_objects DB-net query
  # crashes in this build; top-level outputs remain represented through their
  # connected instance-side pins when present.
  if {[llength $slacks] == 0} { continue }
  set min_slack [lindex $slacks 0]
  set sum_slack 0.0
  foreach slack $slacks {
    if {$slack < $min_slack} { set min_slack $slack }
    set sum_slack [expr {$sum_slack + $slack}]
  }
  set mean_slack [expr {$sum_slack / [llength $slacks]}]
  set criticality [expr {1.0 - max(0.0, min($min_slack, $period_ns)) / $period_ns}]
  if {![dict exists $rows $name] || $criticality > [dict get $rows $name criticality]} {
    dict set rows $name [dict create min_slack $min_slack mean_slack $mean_slack \
      criticality $criticality count [llength $slacks]]
  }
}

dict for {name values} $rows {
  puts $output [join [list $name [dict get $values min_slack] \
    [dict get $values mean_slack] [dict get $values criticality] \
    [dict get $values count]] "\t"]
}
close $output
puts "TIMING_FEATURE_EXPORT nets=[dict size $rows] period_ns=$period_ns output=$::env(TIMING_FEATURE_OUT)"
