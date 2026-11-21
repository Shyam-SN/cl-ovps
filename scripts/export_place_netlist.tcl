# Export the same global-placement checkpoint that is used for power analysis.
set project_root [file normalize [file join [file dirname [info script]] ..]]
set result_dir [file join $project_root build orfs results sky130hd picorv32_baseline baseline]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
if {[info exists ::env(PLACEMENT_ODB)]} {
  set placement_odb $::env(PLACEMENT_ODB)
} else {
  set placement_odb [file join $result_dir 3_3_place_gp.odb]
}

read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $placement_odb
write_verilog $::env(PLACEMENT_NETLIST)
