# Export input-pin capacitance for the CTS dummy-load masters observed in GCD.
#
# Required environment variable:
#   CTS_DUMMY_CAP_OUT   Output TSV path

if {![info exists ::env(CTS_DUMMY_CAP_OUT)]} {
  error "required environment variable CTS_DUMMY_CAP_OUT is not set"
}

set project_root [file normalize [file join [file dirname [info script]] ..]]
set liberty [file join $project_root tools openroad-flow-scripts flow platforms \
                 sky130hd lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_liberty $liberty

set output [open $::env(CTS_DUMMY_CAP_OUT) w]
puts $output "master\tinput_pin\tinput_cap_ff"
foreach master {sky130_fd_sc_hd__clkbuf_1 sky130_fd_sc_hd__clkbuf_8 \
                sky130_fd_sc_hd__clkinv_2} {
  set pins [get_lib_pins -quiet */${master}/A]
  if {[llength $pins] != 1} {
    error "expected one input pin A for $master; found [llength $pins]"
  }
  set cap_pf [get_property [lindex $pins 0] capacitance]
  puts $output "$master\tA\t[expr {$cap_pf * 1000.0}]"
}
close $output
puts "GCD_CTS_DUMMY_CAPS output=$::env(CTS_DUMMY_CAP_OUT)"
exit
