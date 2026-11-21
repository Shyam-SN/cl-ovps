# Apply the CL-OVPS 1x-width / 2x-spacing NDR to all nets that terminate on
# Liberty clock pins. Instance/net names are discovered from OpenDB and Liberty.
create_ndr -name CL_OVPS_CLK_LEAF_1W_2S -width {*1} -spacing {*2}
set block [ord::get_db_block]
set leaf_nets [dict create]
foreach inst [$block getInsts] {
  set master [[$inst getMaster] getName]
  foreach iterm [$inst getITerms] {
    set mterm [$iterm getMTerm]
    if {[$mterm getIoType] ne "INPUT"} { continue }
    set lib_pins [get_lib_pins -quiet */${master}/[$mterm getName]]
    if {[llength $lib_pins] != 1 ||
        [get_property -quiet [lindex $lib_pins 0] is_register_clock] ne "1"} { continue }
    set net [$iterm getNet]
    if {$net ne "NULL"} { dict set leaf_nets [$net getName] 1 }
  }
}
set count 0
foreach net_name [dict keys $leaf_nets] {
  assign_ndr -ndr CL_OVPS_CLK_LEAF_1W_2S -net $net_name
  incr count
}
if {$count == 0} { error "CL-OVPS clock-leaf discovery matched no sequential clock nets" }
puts "CL_OVPS_CLOCK_LEAF_NDR name=CL_OVPS_CLK_LEAF_1W_2S width_factor=1 spacing_factor=2 nets=$count"
