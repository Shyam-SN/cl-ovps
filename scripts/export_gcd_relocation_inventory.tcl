# Export every instance so the randomized relocation generator can account for
# fixed and placed obstructions as well as movable sequential cells.
foreach required {PLACEMENT_ODB RELOCATION_INVENTORY_OUT} {
  if {![info exists ::env($required)]} {
    error "required environment variable $required is not set"
  }
}

read_db $::env(PLACEMENT_ODB)
set block [ord::get_db_block]
set output [open $::env(RELOCATION_INVENTORY_OUT) w]
puts $output "instance\tmaster\twidth_dbu\theight_dbu\tx_dbu\ty_dbu\torient\tstatus"
set count 0
foreach inst [$block getInsts] {
  set master [$inst getMaster]
  lassign [$inst getLocation] x y
  puts $output [join [list [$inst getName] [$master getName] [$master getWidth] \
    [$master getHeight] $x $y [$inst getOrient] [$inst getPlacementStatus]] "\t"]
  incr count
}
close $output
puts "GCD_RELOCATION_INVENTORY instances=$count output=$::env(RELOCATION_INVENTORY_OUT)"
