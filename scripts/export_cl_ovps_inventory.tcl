# Export placement occupancy for the universal Sky130HD relocation generator.
foreach required {CL_OVPS_ODB CL_OVPS_INVENTORY_OUT} {
  if {![info exists ::env($required)]} { error "required environment variable $required is not set" }
}
read_db $::env(CL_OVPS_ODB)
set block [ord::get_db_block]
set output [open $::env(CL_OVPS_INVENTORY_OUT) w]
puts $output "instance\tmaster\twidth_dbu\theight_dbu\tx_dbu\ty_dbu\torient\tstatus"
foreach inst [$block getInsts] {
  set master [$inst getMaster]
  lassign [$inst getLocation] x y
  puts $output [join [list [$inst getName] [$master getName] [$master getWidth] \
    [$master getHeight] $x $y [$inst getOrient] [$inst getPlacementStatus]] "\t"]
}
close $output
exit
