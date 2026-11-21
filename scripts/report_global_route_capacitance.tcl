# Export gross signal/clock capacitance from an OpenROAD GRT checkpoint.
# Required environment: STAGE_ODB, STAGE_SDC, GRT_CAP_REPORT.
foreach required {STAGE_ODB STAGE_SDC GRT_CAP_REPORT} {
  if {![info exists ::env($required)]} { error "required environment variable $required is not set" }
}
set project_root [file normalize [file join [file dirname [info script]] ..]]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $::env(STAGE_ODB)
read_sdc $::env(STAGE_SDC)
source [file join $platform_dir setRC.tcl]
set_thread_count 1
estimate_parasitics -global_routing

set report [open $::env(GRT_CAP_REPORT) w]
puts $report "#GRT_CAP\tclass\tnet\tpin_cap_f\twire_cap_f\ttotal_cap_f"
set scene [sta::cmd_scene]
foreach net [get_nets -hierarchical *] {
  set name [get_full_name $net]
  set leaf [lindex [split $name /] end]
  set class signal
  if {$leaf eq "clk" || [string match "clknet_*" $leaf]} { set class clock }
  puts $report "#GRT_CAP\t$class\t$name\t[$net pin_capacitance $scene max]\t[$net wire_capacitance $scene max]\t[$net capacitance $scene max]"
}
close $report
puts "GRT_CAPACITANCE exported=$::env(GRT_CAP_REPORT)"
exit
