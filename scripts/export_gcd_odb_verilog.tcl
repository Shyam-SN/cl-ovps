foreach required {AUDIT_INPUT_ODB AUDIT_VERILOG_OUT} {
  if {![info exists ::env($required)]} { error "required environment variable $required is not set" }
}
set project_root [file normalize [file join [file dirname [info script]] ..]]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $::env(AUDIT_INPUT_ODB)
write_verilog $::env(AUDIT_VERILOG_OUT)
puts "AUDIT_VERILOG_WRITTEN $::env(AUDIT_VERILOG_OUT)"
exit
