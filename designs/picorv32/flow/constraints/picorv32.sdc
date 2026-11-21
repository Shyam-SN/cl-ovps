create_clock -name core_clk -period 20.000 [get_ports clk]

set_clock_uncertainty -setup 0.200 [get_clocks core_clk]
set_clock_uncertainty -hold 0.050 [get_clocks core_clk]

set_input_delay 1.000 -clock core_clk [get_ports {resetn mem_ready mem_rdata[*]}]
set_input_transition 0.050 [get_ports {resetn mem_ready mem_rdata[*]}]

set_output_delay 1.000 -clock core_clk [get_ports {trap mem_valid mem_instr mem_addr[*] mem_wdata[*] mem_wstrb[*]}]
set_load 0.003 [get_ports {trap mem_valid mem_instr mem_addr[*] mem_wdata[*] mem_wstrb[*]}]
