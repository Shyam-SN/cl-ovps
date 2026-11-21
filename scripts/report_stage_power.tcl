# Activity-based power for a placement or global-route ODB estimate.
# Inputs are matched to the candidate ODB and workload VCD by the Python runner.
set project_root [file normalize [file join [file dirname [info script]] ..]]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
foreach required_env {STAGE PARASITIC_MODE STAGE_ODB STAGE_SDC ACTIVITY_VCD WORKLOAD CAP_REPORT} {
  if {![info exists ::env($required_env)]} {
    error "required environment variable $required_env is not set"
  }
}

read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $::env(STAGE_ODB)
read_sdc $::env(STAGE_SDC)
source [file join $platform_dir setRC.tcl]
set_thread_count 1
set_propagated_clock [all_clocks]
if {$::env(PARASITIC_MODE) eq "placement"} {
  estimate_parasitics -placement
} elseif {$::env(PARASITIC_MODE) eq "global_routing"} {
  estimate_parasitics -global_routing
} else {
  error "unknown PARASITIC_MODE: $::env(PARASITIC_MODE)"
}
read_upf -file [file join $project_root designs picorv32 flow constraints picorv32.upf]
read_vcd -scope tb_picorv32_activity/dut $::env(ACTIVITY_VCD)

# Export the annotated driver activity and estimated capacitance by net. This
# makes the reported total independently reconcilable with 0.5*C*V^2*f.
set cap_file [open $::env(CAP_REPORT) w]
puts $cap_file "#STAGE_DRIVER_CAP\tpin\tnet\tpin_cap\twire_cap\ttotal_cap\tdensity_hz\tduty\tactivity_origin"
set cap_scene [sta::cmd_scene]
foreach net [get_nets *] {
  set net_name [get_full_name $net]
  set pin_cap [$net pin_capacitance $cap_scene "max"]
  set wire_cap [$net wire_capacitance $cap_scene "max"]
  set total_cap [$net capacitance $cap_scene "max"]
  set pin_iter [$net connected_pin_iterator]
  while {[$pin_iter has_next]} {
    set pin [$pin_iter next]
    if {[$pin is_driver] && ![$pin is_top_level_port] && ![$pin is_pwr_gnd]} {
      set activity [get_property $pin activity]
      puts $cap_file "#STAGE_DRIVER_CAP\t[get_full_name $pin]\t$net_name\t$pin_cap\t$wire_cap\t$total_cap\t[lindex $activity 0]\t[lindex $activity 1]\t[lindex $activity 2]"
    }
  }
  $pin_iter finish
}
close $cap_file

set scene [sta::cmd_scene]
set switching_w [lindex [sta::design_power $scene] 1]
set setup_ns [expr {[sta::worst_slack_cmd max] * 1.0e9}]
set hold_ns [expr {[sta::worst_slack_cmd min] * 1.0e9}]
puts [format "STAGE_METRICS stage=%s workload=%s switching_w=%.12e setup_ns=%.9f hold_ns=%.9f" \
  $::env(STAGE) $::env(WORKLOAD) $switching_w $setup_ns $hold_ns]
report_activity_annotation
report_power -digits 12
report_checks -path_delay max -group_path_count 1 -digits 6
report_checks -path_delay min -group_path_count 1 -digits 6
