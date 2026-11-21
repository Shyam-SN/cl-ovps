# Activity hook used by the AES comparison dashboard before OpenROAD global
# placement. The built-in power-driven placer consumes this activity data.
if {![info exists ::env(AES_COMPARE_VCD)] || ![file isfile $::env(AES_COMPARE_VCD)]} {
  error "AES_COMPARE_VCD is missing or not a file"
}
puts "AES_POWER_COMPARE_ACTIVITY scope=tb_aes_activity/dut file=$::env(AES_COMPARE_VCD)"
read_vcd -scope tb_aes_activity/dut $::env(AES_COMPARE_VCD)
report_activity_annotation -report_unannotated
