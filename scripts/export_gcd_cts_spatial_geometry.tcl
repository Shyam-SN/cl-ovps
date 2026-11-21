# Export placed LEF pin/obstruction rectangles and routing tracks around a CTS ODB.
# The Python driver turns these raw geometries into placement-time features.

foreach required {SPATIAL_ODB SPATIAL_CANDIDATE SPATIAL_SINK_OUT SPATIAL_GEOMETRY_OUT SPATIAL_TRACKS_OUT} {
  if {![info exists ::env($required)]} {
    error "required environment variable $required is not set"
  }
}

set project_root [file normalize [file join [file dirname [info script]] ..]]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $::env(SPATIAL_ODB)
set block [ord::get_db_block]
set dbu_per_micron [$block getDbUnitsPerMicron]
set wanted_layers {li1 mcon met1 met2}

proc canonical_net_name {name} {
  return [string map [list {\[} {[} {\]} {]}] $name]
}

proc transform_xy {x y orient ox oy} {
  switch -- $orient {
    R0      { set tx $x;        set ty $y }
    R90     { set tx [expr {-$y}]; set ty $x }
    R180    { set tx [expr {-$x}]; set ty [expr {-$y}] }
    R270    { set tx $y;        set ty [expr {-$x}] }
    MY      { set tx [expr {-$x}]; set ty $y }
    MYR90   { set tx [expr {-$y}]; set ty [expr {-$x}] }
    MX      { set tx $x;        set ty [expr {-$y}] }
    MXR90   { set tx $y;        set ty $x }
    default { error "unsupported instance orientation $orient" }
  }
  return [list [expr {$tx + $ox}] [expr {$ty + $oy}]]
}

proc transform_rect {x1 y1 x2 y2 orient ox oy} {
  set xs {}
  set ys {}
  foreach point [list [list $x1 $y1] [list $x1 $y2] [list $x2 $y1] [list $x2 $y2]] {
    lassign $point x y
    lassign [transform_xy $x $y $orient $ox $oy] tx ty
    lappend xs $tx
    lappend ys $ty
  }
  return [list [tcl::mathfunc::min {*}$xs] [tcl::mathfunc::min {*}$ys] \
    [tcl::mathfunc::max {*}$xs] [tcl::mathfunc::max {*}$ys]]
}

proc emit_row {channel values} {
  puts $channel [join $values "\t"]
}

set sink_out [open $::env(SPATIAL_SINK_OUT) w]
set geometry_out [open $::env(SPATIAL_GEOMETRY_OUT) w]
set tracks_out [open $::env(SPATIAL_TRACKS_OUT) w]
emit_row $sink_out {candidate_id branch sink_instance master orient clock_pin pin_xmin_dbu pin_ymin_dbu pin_xmax_dbu pin_ymax_dbu pin_center_x_dbu pin_center_y_dbu dbu_per_micron}
emit_row $geometry_out {candidate_id kind instance master orient net sigtype pin layer xmin_dbu ymin_dbu xmax_dbu ymax_dbu}
emit_row $tracks_out {candidate_id grid_kind layer axis coord_dbu preferred_direction}

# Trial CTS assigns each sink to an exact leaf net. Keep only leaf branches;
# the root and intermediate clock nets are handled by the CTS oracle stage.
set sink_count 0
foreach net [$block getNets] {
  set branch [canonical_net_name [$net getName]]
  if {![regexp {^clknet_2_[0-9]+__leaf_clk$} $branch]} {
    continue
  }
  foreach iterm [$net getITerms] {
    set mterm [$iterm getMTerm]
    if {[$mterm getName] ne "CLK" || [$mterm getIoType] ne "INPUT"} {
      continue
    }
    set inst [$iterm getInst]
    set master [$inst getMaster]
    set orient [$inst getOrient]
    lassign [$inst getOrigin] ox oy
    lassign [$iterm getAvgXY] valid cx cy
    if {!$valid} {
      continue
    }
    set pin_boxes {}
    foreach mpin [$mterm getMPins] {
      foreach box [$mpin getGeometry] {
        set tech_layer [$box getTechLayer]
        if {$tech_layer eq "NULL" || [$tech_layer getName] ne "li1"} {
          continue
        }
        lappend pin_boxes [transform_rect [$box xMin] [$box yMin] [$box xMax] [$box yMax] $orient $ox $oy]
      }
    }
    if {[llength $pin_boxes] == 0} {
      error "clock sink [$iterm getName] has no li1 pin geometry"
    }
    set xmin [lindex [lindex $pin_boxes 0] 0]
    set ymin [lindex [lindex $pin_boxes 0] 1]
    set xmax [lindex [lindex $pin_boxes 0] 2]
    set ymax [lindex [lindex $pin_boxes 0] 3]
    foreach rect [lrange $pin_boxes 1 end] {
      set xmin [expr {min($xmin, [lindex $rect 0])}]
      set ymin [expr {min($ymin, [lindex $rect 1])}]
      set xmax [expr {max($xmax, [lindex $rect 2])}]
      set ymax [expr {max($ymax, [lindex $rect 3])}]
    }
    emit_row $sink_out [list $::env(SPATIAL_CANDIDATE) $branch [$inst getName] [$master getName] \
      $orient CLK $xmin $ymin $xmax $ymax $cx $cy $dbu_per_micron]
    incr sink_count
  }
}
if {$sink_count == 0} {
  error "no CLK sinks found on clknet_2_*__leaf_clk nets in $::env(SPATIAL_ODB)"
}

# Export layer-specific master pin geometry and LEF OBS geometry in placed
# coordinates. The Python driver clips these rectangles to sink/branch windows.
set geometry_count 0
foreach inst [$block getInsts] {
  set master [$inst getMaster]
  set master_name [$master getName]
  set orient [$inst getOrient]
  lassign [$inst getOrigin] ox oy
  foreach iterm [$inst getITerms] {
    set mterm [$iterm getMTerm]
    set net [$iterm getNet]
    set net_name ""
    if {$net ne "NULL"} {
      set net_name [canonical_net_name [$net getName]]
    }
    set sigtype [$mterm getSigType]
    foreach mpin [$mterm getMPins] {
      foreach box [$mpin getGeometry] {
        set tech_layer [$box getTechLayer]
        if {$tech_layer eq "NULL"} { continue }
        set layer [$tech_layer getName]
        if {$layer ni $wanted_layers} { continue }
        lassign [transform_rect [$box xMin] [$box yMin] [$box xMax] [$box yMax] $orient $ox $oy] x1 y1 x2 y2
        emit_row $geometry_out [list $::env(SPATIAL_CANDIDATE) pin [$inst getName] $master_name $orient \
          $net_name $sigtype [$mterm getName] $layer $x1 $y1 $x2 $y2]
        incr geometry_count
      }
    }
  }
  foreach box [$master getObstructions] {
    set tech_layer [$box getTechLayer]
    if {$tech_layer eq "NULL"} { continue }
    set layer [$tech_layer getName]
    if {$layer ni $wanted_layers} { continue }
    lassign [transform_rect [$box xMin] [$box yMin] [$box xMax] [$box yMax] $orient $ox $oy] x1 y1 x2 y2
    emit_row $geometry_out [list $::env(SPATIAL_CANDIDATE) obs [$inst getName] $master_name $orient \
      "" OBS "" $layer $x1 $y1 $x2 $y2]
    incr geometry_count
  }
}

# Add block-level routing obstructions, when present.
foreach obstruction [$block getObstructions] {
  set box [$obstruction getBBox]
  set layer [$box getTechLayer]
  if {$layer eq "NULL"} { continue }
  set layer_name [$layer getName]
  if {$layer_name ni $wanted_layers} { continue }
  emit_row $geometry_out [list $::env(SPATIAL_CANDIDATE) block_obs "" "" R0 "" OBS "" \
    $layer_name [$box xMin] [$box yMin] [$box xMax] [$box yMax]]
  incr geometry_count
}

# Export actual track coordinates from the ODB track grids.
foreach grid [$block getTrackGrids] {
  set layer [$grid getTechLayer]
  set layer_name [$layer getName]
  if {$layer_name ni $wanted_layers} { continue }
  set direction [$layer getDirection]
  foreach coord [$grid getGridX] {
    emit_row $tracks_out [list $::env(SPATIAL_CANDIDATE) track $layer_name x $coord $direction]
  }
  foreach coord [$grid getGridY] {
    emit_row $tracks_out [list $::env(SPATIAL_CANDIDATE) track $layer_name y $coord $direction]
  }
}

# A CTS ODB has no global-route GCell grid. Export it when a later stage has
# populated one; the driver records that absence rather than inventing a pitch.
set gcell_grid [$block getGCellGrid]
if {$gcell_grid ne "NULL"} {
  foreach coord [$gcell_grid getGridX] {
    emit_row $tracks_out [list $::env(SPATIAL_CANDIDATE) gcell gcell x $coord NA]
  }
  foreach coord [$gcell_grid getGridY] {
    emit_row $tracks_out [list $::env(SPATIAL_CANDIDATE) gcell gcell y $coord NA]
  }
}

close $sink_out
close $geometry_out
close $tracks_out
puts "GCD_CTS_SPATIAL_GEOMETRY candidate=$::env(SPATIAL_CANDIDATE) sinks=$sink_count shapes=$geometry_count"
exit
