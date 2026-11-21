# Export clock-net capacitance estimated from a global-route checkpoint.
# Inputs: STAGE_ODB, STAGE_SDC, and CLOCK_CAP_REPORT.
set project_root [file normalize [file join [file dirname [info script]] ..]]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
foreach required_env {STAGE_ODB STAGE_SDC CLOCK_CAP_REPORT} {
  if {![info exists ::env($required_env)]} {
    error "required environment variable $required_env is not set"
  }
}

read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $::env(STAGE_ODB)
read_sdc $::env(STAGE_SDC)
source [file join $platform_dir setRC.tcl]
set_thread_count 1
estimate_parasitics -global_routing

set report [open $::env(CLOCK_CAP_REPORT) w]
puts $report "#GRT_CLOCK_CAP\tnet\tpin_cap_f\twire_cap_f\ttotal_cap_f\tendpoint_count"
set scene [sta::cmd_scene]
set total_pin 0.0
set total_wire 0.0
set clock_count 0
foreach net [get_nets -hierarchical *] {
  set name [get_full_name $net]
  set leaf [lindex [split $name /] end]
  if {$leaf ne "clk" && ![string match "clknet_*" $leaf]} {
    continue
  }
  set pin_cap [$net pin_capacitance $scene "max"]
  set wire_cap [$net wire_capacitance $scene "max"]
  set total_cap [$net capacitance $scene "max"]
  set endpoints [llength [get_pins -of_objects $net]]
  puts $report "#GRT_CLOCK_CAP\t$name\t$pin_cap\t$wire_cap\t$total_cap\t$endpoints"
  set total_pin [expr {$total_pin + $pin_cap}]
  set total_wire [expr {$total_wire + $wire_cap}]
  incr clock_count
}
puts $report "#GRT_CLOCK_TOTAL\t$clock_count\t$total_pin\t$total_wire\t[expr {$total_pin + $total_wire}]"
close $report
puts "GRT_CLOCK_CAP nets=$clock_count pin_cap_f=$total_pin wire_cap_f=$total_wire total_cap_f=[expr {$total_pin + $total_wire}]"
