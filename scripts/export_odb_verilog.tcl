# Export a matching Verilog view for a placement ODB without changing it.
foreach required {EXPORT_ODB EXPORT_VERILOG} {
  if {![info exists ::env($required)] || $::env($required) eq ""} { error "$required is required" }
}
read_db $::env(EXPORT_ODB)
# Keep physical supply nets out of the functional proof netlist. The generic
# LEC adapter re-connects omitted cell supply pins to ideal rails; exposing
# ODB VDD/VSS as candidate top-level inouts would create unmatched gold ports.
write_verilog $::env(EXPORT_VERILOG)
exit
