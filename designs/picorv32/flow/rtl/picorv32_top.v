module picorv32_top (
    input         clk,
    input         resetn,
    input         mem_ready,
    input  [31:0] mem_rdata,
    output        trap,
    output        mem_valid,
    output        mem_instr,
    output [31:0] mem_addr,
    output [31:0] mem_wdata,
    output [ 3:0] mem_wstrb
);

  picorv32 #(
      .ENABLE_COUNTERS(1),
      .ENABLE_COUNTERS64(0),
      .ENABLE_REGS_16_31(1),
      .ENABLE_REGS_DUALPORT(1),
      .ENABLE_PCPI(0),
      .ENABLE_MUL(1),
      .ENABLE_FAST_MUL(0),
      .ENABLE_DIV(1),
      .ENABLE_IRQ(0),
      .ENABLE_TRACE(0),
      .REGS_INIT_ZERO(1),
      .PROGADDR_RESET(32'h0000_0000)
  ) cpu (
      .clk(clk),
      .resetn(resetn),
      .trap(trap),
      .mem_valid(mem_valid),
      .mem_instr(mem_instr),
      .mem_ready(mem_ready),
      .mem_addr(mem_addr),
      .mem_wdata(mem_wdata),
      .mem_wstrb(mem_wstrb),
      .mem_rdata(mem_rdata),
      .pcpi_wr(1'b0),
      .pcpi_rd(32'b0),
      .pcpi_wait(1'b0),
      .pcpi_ready(1'b0),
      .irq(32'b0)
  );

endmodule
