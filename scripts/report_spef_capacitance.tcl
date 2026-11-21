# Export per-net ground capacitance after detailed-route SPEF extraction.
# Required environment: SPEF_ODB, SPEF_SDC, SPEF_FILE, SPEF_CAP_REPORT.
foreach required {SPEF_ODB SPEF_SDC SPEF_FILE SPEF_CAP_REPORT} {
  if {![info exists ::env($required)]} { error "required environment variable $required is not set" }
}
set project_root [file normalize [file join [file dirname [info script]] ..]]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $::env(SPEF_ODB)
read_sdc $::env(SPEF_SDC)
read_spef $::env(SPEF_FILE)
set_thread_count 1
set scene [sta::cmd_scene]
set report [open $::env(SPEF_CAP_REPORT) w]
puts $report "#SPEF_CAP\tclass\tnet\tpin_cap_f\twire_cap_f\ttotal_cap_f"
foreach net [get_nets -hierarchical *] {
  set name [get_full_name $net]
  set leaf [lindex [split $name /] end]
  set class signal
  if {$leaf eq "clk" || [string match "clknet_*" $leaf]} { set class clock }
  puts $report "#SPEF_CAP\t$class\t$name\t[$net pin_capacitance $scene max]\t[$net wire_capacitance $scene max]\t[$net capacitance $scene max]"
}
close $report
puts "SPEF_CAPACITANCE exported=$::env(SPEF_CAP_REPORT)"
exit
