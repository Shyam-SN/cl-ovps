# Check legality, timing, and power for one workload on a saved candidate ODB.
set project_root [file normalize [file join [file dirname [info script]] ..]]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
foreach required_env {CANDIDATE_ODB PLACEMENT_SDC ACTIVITY_VCD WORKLOAD} {
  if {![info exists ::env($required_env)]} {
    error "required environment variable $required_env is not set"
  }
}

read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $::env(CANDIDATE_ODB)
read_sdc $::env(PLACEMENT_SDC)
source [file join $platform_dir setRC.tcl]
estimate_parasitics -placement
read_upf -file [file join $project_root designs picorv32 flow constraints picorv32.upf]
set_thread_count 1
read_vcd -scope tb_picorv32_activity/dut $::env(ACTIVITY_VCD)
check_placement -placeable

set scene [sta::cmd_scene]
set switching_w [lindex [sta::design_power $scene] 1]
set setup_ns [expr {[sta::worst_slack_cmd max] * 1.0e9}]
set hold_ns [expr {[sta::worst_slack_cmd min] * 1.0e9}]
puts [format "CANDIDATE_METRICS workload=%s legal=pass switching_w=%.12e setup_ns=%.9f hold_ns=%.9f" \
  $::env(WORKLOAD) $switching_w $setup_ns $hold_ns]
report_power -digits 12
