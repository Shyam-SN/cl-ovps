# Save one legal isolated placement perturbation per row of a relocation pool.
# Required environment: PLACEMENT_ODB, RELOCATION_POOL, CANDIDATE_ODB_DIR, MOVE_AUDIT.
foreach required {PLACEMENT_ODB RELOCATION_POOL CANDIDATE_ODB_DIR MOVE_AUDIT} {
  if {![info exists ::env($required)]} { error "required environment variable $required is not set" }
}

read_db $::env(PLACEMENT_ODB)
set block [ord::get_db_block]

proc read_tsv {path} {
  set input [open $path r]
  set header [split [gets $input] "\t"]
  set rows {}
  while {[gets $input line] >= 0} {
    if {$line eq ""} { continue }
    set values [split $line "\t"]
    if {[llength $values] != [llength $header]} { error "malformed relocation row: $line" }
    set row [dict create]
    for {set i 0} {$i < [llength $header]} {incr i} { dict set row [lindex $header $i] [lindex $values $i] }
    lappend rows $row
  }
  close $input
  return $rows
}

file mkdir $::env(CANDIDATE_ODB_DIR)
set output [open $::env(MOVE_AUDIT) w]
puts $output "candidate_id\tinstance\tfrom_x_dbu\tfrom_y_dbu\tfrom_orient\ttarget_x_dbu\ttarget_y_dbu\ttarget_orient\tstatus\todb\tdetail"
set saved 0
foreach proposal [read_tsv $::env(RELOCATION_POOL)] {
  set candidate [dict get $proposal candidate_id]
  set name [dict get $proposal instance]
  set inst [$block findInst $name]
  if {$inst eq "NULL"} {
    puts $output "$candidate\t$name\t\t\t\t\t\t\tnot_found\t\tinstance missing from ODB"
    continue
  }
  lassign [$inst getLocation] old_x old_y
  set old_orient [$inst getOrient]
  set target_x [dict get $proposal target_x_dbu]
  set target_y [dict get $proposal target_y_dbu]
  set target_orient [dict get $proposal target_orient]
  set rc [catch {
    $inst setOrient $target_orient
    $inst setLocation $target_x $target_y
    check_placement -placeable
    set odb [file join $::env(CANDIDATE_ODB_DIR) "${candidate}.odb"]
    write_db $odb
  } detail]
  set restore_rc [catch {
    $inst setOrient $old_orient
    $inst setLocation $old_x $old_y
  } restore_detail]
  if {$restore_rc} { error "failed to restore $name after move: $restore_detail" }
  if {$rc} {
    puts $output "$candidate\t$name\t$old_x\t$old_y\t$old_orient\t$target_x\t$target_y\t$target_orient\tplacement_rejected\t\t$detail"
  } else {
    incr saved
    puts $output "$candidate\t$name\t$old_x\t$old_y\t$old_orient\t$target_x\t$target_y\t$target_orient\tsaved\t$odb\t"
  }
}
close $output
puts "GENERIC_CLOCK_MOVE_EXPORT saved=$saved audit=$::env(MOVE_AUDIT)"
exit
