# Report per-instance power for CTS buffers and dummy balancing loads.
#
# Required environment variables match gcd_report_spef_power.tcl:
#   SPEF_ODB SPEF_SDC SPEF_FILE ACTIVITY_VCD WORKLOAD

set project_root [file normalize [file join [file dirname [info script]] ..]]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
foreach required_env {SPEF_ODB SPEF_SDC SPEF_FILE ACTIVITY_VCD WORKLOAD} {
  if {![info exists ::env($required_env)]} {
    error "required environment variable $required_env is not set"
  }
}

read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $::env(SPEF_ODB)
read_sdc $::env(SPEF_SDC)
read_spef $::env(SPEF_FILE)
set upf_file [file join $project_root designs gcd flow constraints gcd.upf]
source [file join $project_root scripts load_single_domain_upf.tcl]
load_single_domain_upf $upf_file
set_thread_count 1
read_vcd -scope tb_gcd_activity/dut $::env(ACTIVITY_VCD)

set clock_buffers [get_cells *clkbuf*]
set dummy_loads [get_cells *clkload*]
set cts_cells [concat $clock_buffers $dummy_loads]
puts "CTS_INSTANCE_POWER workload=$::env(WORKLOAD) buffers=[llength $clock_buffers] dummy_loads=[llength $dummy_loads]"
report_power -instances $cts_cells -digits 12
exit
