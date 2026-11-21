# Stage 3E diagnostic only: load routed parasitics, export SDF and arrival
# windows for multi-input combinational cells. No database mutation occurs.
foreach required {PASS3E_ODB PASS3E_SPEF PASS3E_SDC PASS3E_LIBS PASS3E_RC_TCL PASS3E_SDF PASS3E_ARRIVAL_TSV PASS3E_NET_CAP_TSV} {
  if {![info exists ::env($required)] || $::env($required) eq ""} { error "$required is required" }
}
foreach lib [split $::env(PASS3E_LIBS) "\n"] {
  if {$lib ne ""} { read_liberty $lib }
}
read_db $::env(PASS3E_ODB)
read_sdc $::env(PASS3E_SDC)
source $::env(PASS3E_RC_TCL)
read_spef $::env(PASS3E_SPEF)
set_thread_count 1
set_propagated_clock [all_clocks]
write_sdf -include_typ -no_timestamp $::env(PASS3E_SDF)

proc p3e_num {value} {
  if {$value eq "" || ![string is double -strict $value] || [string match -nocase *inf* $value] || [string match -nocase *nan* $value]} { return "" }
  return $value
}
set block [ord::get_db_block]
set arrival [open $::env(PASS3E_ARRIVAL_TSV) w]
puts $arrival "instance\tmaster\tinput_pin_count\tearly_arrival_ns\tlate_arrival_ns\tarrival_skew_ns\toutput_late_arrival_ns\tcell_delay_proxy_ns"
set families {xor2 xor3 xnor2 xnor3 mux2 ha_ fa_ a21o a22o o21a o22a aoi21 oai21 a21oi o21ai a2111o a211o o211a o211ai a31o o31a}
foreach inst [$block getInsts] {
  set master [[$inst getMaster] getName]
  if {![string match sky130_fd_sc_hd__* $master]} { continue }
  set family [string range $master [string length sky130_fd_sc_hd__] end]
  set matched 0
  foreach prefix $families { if {[string match ${prefix}* $family]} { set matched 1; break } }
  if {!$matched} { continue }
  set name [$inst getName]
  set arrivals {}
  set outputs {}
  foreach iterm [$inst getITerms] {
    set mterm [$iterm getMTerm]
    set pin_name [$mterm getName]
    if {[$mterm getIoType] ni {INPUT OUTPUT}} { continue }
    set sta_pin [get_pins -quiet "$name/$pin_name"]
    if {[llength $sta_pin] != 1} { continue }
    set pin [lindex $sta_pin 0]
    if {[$mterm getIoType] eq "INPUT"} {
      set rise [p3e_num [get_property -quiet $pin arrival_max_rise]]
      set fall [p3e_num [get_property -quiet $pin arrival_max_fall]]
      set pin_arrivals {}
      foreach value [list $rise $fall] { if {$value ne ""} { lappend pin_arrivals $value } }
      if {[llength $pin_arrivals]} { lappend arrivals [lindex [lsort -real $pin_arrivals] end] }
    } else {
      set rise [p3e_num [get_property -quiet $pin arrival_max_rise]]
      set fall [p3e_num [get_property -quiet $pin arrival_max_fall]]
      set pin_arrivals {}
      foreach value [list $rise $fall] { if {$value ne ""} { lappend pin_arrivals $value } }
      if {[llength $pin_arrivals]} { lappend outputs [lindex [lsort -real $pin_arrivals] end] }
    }
  }
  if {[llength $arrivals] < 2} { continue }
  set early [lindex [lsort -real $arrivals] 0]
  set late [lindex [lsort -real $arrivals] end]
  set skew [expr {$late - $early}]
  set out_late ""
  set cell_delay ""
  if {[llength $outputs]} {
    set out_late [lindex [lsort -real $outputs] end]
    set cell_delay [expr {$out_late - $late}]
  }
  puts $arrival [join [list $name $master [llength $arrivals] $early $late $skew $out_late $cell_delay] "\t"]
}
close $arrival

set caps [open $::env(PASS3E_NET_CAP_TSV) w]
puts $caps "net\tcapacitance_f\tcapacitance_ff"
set scene [sta::cmd_scene]
foreach net [$block getNets] {
  if {[catch {set cap [$net capacitance $scene max]}] || ![string is double -strict $cap]} { continue }
  puts $caps [join [list [$net getName] $cap [expr {$cap * 1.0e15}]] "\t"]
}
close $caps
puts "PASS3E_RESULT sdf=$::env(PASS3E_SDF) arrival_rows=[expr {[llength [get_cells -quiet *]]}]"
exit
