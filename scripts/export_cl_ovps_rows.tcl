# Export row/site geometry used to create rail-compatible legal relocation sites.
foreach required {CL_OVPS_ODB CL_OVPS_ROWS_OUT} {
  if {![info exists ::env($required)]} { error "required environment variable $required is not set" }
}
read_db $::env(CL_OVPS_ODB)
set block [ord::get_db_block]
set output [open $::env(CL_OVPS_ROWS_OUT) w]
puts $output "row\torient\tsite\tx_min_dbu\ty_min_dbu\tx_max_dbu\ty_max_dbu\tx_step_dbu\tsite_width_dbu\tsite_height_dbu"
foreach row [$block getRows] {
  set box [$row getBBox]
  set site [$row getSite]
  puts $output [join [list [$row getName] [$row getOrient] [$site getName] \
    [$box xMin] [$box yMin] [$box xMax] [$box yMax] [$row getSpacing] \
    [$site getWidth] [$site getHeight]] "\t"]
}
close $output
exit
