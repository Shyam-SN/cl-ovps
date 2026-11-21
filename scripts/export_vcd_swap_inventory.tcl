# Export movable placed instances for VCD-guided, same-master swap proposals.
foreach required {PLACEMENT_ODB SWAP_INVENTORY_OUT} {
  if {![info exists ::env($required)]} {
    error "required environment variable $required is not set"
  }
}

read_db $::env(PLACEMENT_ODB)
set block [ord::get_db_block]
set output [open $::env(SWAP_INVENTORY_OUT) w]
puts $output "instance\tmaster\twidth_dbu\theight_dbu\tx_dbu\ty_dbu\torient\tstatus"
set count 0
foreach inst [$block getInsts] {
  set status [$inst getPlacementStatus]
  if {$status ne "PLACED"} {
    continue
  }
  set master [$inst getMaster]
  lassign [$inst getLocation] x y
  puts $output [join [list [$inst getName] [$master getName] [$master getWidth] \
    [$master getHeight] $x $y [$inst getOrient] $status] "\t"]
  incr count
}
close $output
puts "VCD_SWAP_INVENTORY placed_instances=$count output=$::env(SWAP_INVENTORY_OUT)"
