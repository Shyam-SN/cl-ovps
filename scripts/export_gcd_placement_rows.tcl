# Export row geometry and orientation for rail-aligned cross-row relocation.
# Required environment variables: PLACEMENT_ODB, ROW_INVENTORY_OUT
foreach required {PLACEMENT_ODB ROW_INVENTORY_OUT} {
  if {![info exists ::env($required)]} { error "required environment variable $required is not set" }
}
read_db $::env(PLACEMENT_ODB)
set block [ord::get_db_block]
set output [open $::env(ROW_INVENTORY_OUT) w]
puts $output "row\torient\tsite\tx_min_dbu\ty_min_dbu\tx_max_dbu\ty_max_dbu\tx_step_dbu\tsite_width_dbu\tsite_height_dbu"
set count 0
foreach row [$block getRows] {
  set box [$row getBBox]
  set site [$row getSite]
  puts $output [join [list [$row getName] [$row getOrient] [$site getName] \
    [$box xMin] [$box yMin] [$box xMax] [$box yMax] [$row getSpacing] \
    [$site getWidth] [$site getHeight]] "\t"]
  incr count
}
close $output
puts "GCD_PLACEMENT_ROWS rows=$count output=$::env(ROW_INVENTORY_OUT)"
exit
