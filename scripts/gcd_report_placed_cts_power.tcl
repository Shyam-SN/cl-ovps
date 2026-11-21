# Estimate per-instance CTS cell power using placement parasitics after CTS.
#
# Required environment variables:
#   CTS_ORACLE_ODB CTS_ORACLE_SDC ACTIVITY_VCD WORKLOAD

foreach required {CTS_ORACLE_ODB CTS_ORACLE_SDC ACTIVITY_VCD WORKLOAD} {
  if {![info exists ::env($required)]} {
    error "required environment variable $required is not set"
  }
}

set project_root [file normalize [file join [file dirname [info script]] ..]]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $::env(CTS_ORACLE_ODB)
read_sdc $::env(CTS_ORACLE_SDC)
source [file join $platform_dir setRC.tcl]
source [file join $project_root scripts load_single_domain_upf.tcl]
load_single_domain_upf [file join $project_root designs gcd flow constraints gcd.upf]
set_thread_count 1
estimate_parasitics -placement
read_vcd -scope tb_gcd_activity/dut $::env(ACTIVITY_VCD)

set clock_buffers [get_cells *clkbuf*]
set dummy_loads [get_cells *clkload*]
set clock_inverters [get_cells *clkinv*]
set cts_cells [concat $clock_buffers $dummy_loads $clock_inverters]
puts "CTS_PLACEMENT_POWER workload=$::env(WORKLOAD) buffers=[llength $clock_buffers] dummy_loads=[llength $dummy_loads] inverters=[llength $clock_inverters]"
report_power -instances $cts_cells -digits 12
exit
