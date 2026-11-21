# Activity-based power analysis for the routed baseline.
# ACTIVITY_VCD is the gate-level VCD produced from the matching final netlist.

set project_root [file normalize [file join [file dirname [info script]] ..]]
set result_dir [file join $project_root build orfs results sky130hd picorv32_baseline baseline]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]

read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db [file join $result_dir 6_final.odb]
read_sdc [file join $result_dir 6_final.sdc]
read_spef [file join $result_dir 6_final.spef]
read_upf -file [file join $project_root designs picorv32 flow constraints picorv32.upf]

read_vcd -scope tb_picorv32_activity/dut $::env(ACTIVITY_VCD)
report_activity_annotation
report_power -digits 6
