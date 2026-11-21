`timescale 1ns/1ps

module tb_picorv32_activity;
  reg clk = 1'b0;
  reg resetn = 1'b0;
  wire mem_ready;
  wire [31:0] mem_rdata;
  wire trap;
  wire mem_valid;
  wire mem_instr;
  wire [31:0] mem_addr;
  wire [31:0] mem_wdata;
  wire [3:0] mem_wstrb;

  reg [31:0] memory [0:255];
  reg [8*16-1:0] workload;
  integer cycles;
  integer i;

  always #10 clk = ~clk;

  // A zero-wait-state unified instruction/data memory. Stores are applied on
  // the accepting clock edge; the same memory also supplies instruction words.
  assign mem_ready = mem_valid;
  assign mem_rdata = memory[mem_addr[9:2]];

  always @(posedge clk) begin
    if (resetn && mem_valid && mem_ready && !mem_instr && (|mem_wstrb)) begin
      if (mem_wstrb[0]) memory[mem_addr[9:2]][ 7: 0] <= mem_wdata[ 7: 0];
      if (mem_wstrb[1]) memory[mem_addr[9:2]][15: 8] <= mem_wdata[15: 8];
      if (mem_wstrb[2]) memory[mem_addr[9:2]][23:16] <= mem_wdata[23:16];
      if (mem_wstrb[3]) memory[mem_addr[9:2]][31:24] <= mem_wdata[31:24];
    end
  end

  picorv32_top dut (
      .clk(clk),
      .resetn(resetn),
      .mem_ready(mem_ready),
      .mem_rdata(mem_rdata),
      .trap(trap),
      .mem_valid(mem_valid),
      .mem_instr(mem_instr),
      .mem_addr(mem_addr),
      .mem_wdata(mem_wdata),
      .mem_wstrb(mem_wstrb)
  );

  initial begin
    workload = "typical";
    cycles = 2000;
    if ($value$plusargs("workload=%s", workload) == 0) workload = "typical";
    if ($value$plusargs("cycles=%d", cycles) == 0) cycles = 2000;

    for (i = 0; i < 256; i = i + 1) memory[i] = 32'h0000_0013; // addi x0,x0,0

    case (workload)
      "idle": begin
        memory[0] = 32'h0000_006f; // jal x0, 0
      end
      "typical": begin
        memory[0] = 32'h0010_8093; // addi x1,x1,1
        memory[1] = 32'h1010_2023; // sw x1,256(x0)
        memory[2] = 32'hff9f_f06f; // jal x0,-8
      end
      "peak": begin
        // Toggle many datapath bits and exercise writes in a short loop.
        memory[0] = 32'hfff0_c093; // xori x1,x1,-1
        memory[1] = 32'h1010_2023; // sw x1,256(x0)
        memory[2] = 32'h0011_0113; // addi x2,x2,1
        memory[3] = 32'h0020_c1b3; // xor x3,x1,x2
        memory[4] = 32'h0021_8233; // add x4,x3,x2
        memory[5] = 32'h1040_2223; // sw x4,260(x0)
        memory[6] = 32'h0011_9293; // slli x5,x3,1
        memory[7] = 32'h0042_c333; // xor x6,x5,x4
        memory[8] = 32'h1060_2423; // sw x6,264(x0)
        memory[9] = 32'h0013_8393; // addi x7,x7,1
        memory[10] = 32'hfd9f_f06f; // jal x0,-40 (back to instruction 0)
      end
      default: begin
        $fatal(1, "Unknown workload '%s' (use idle, typical, or peak)", workload);
      end
    endcase

    $dumpfile("activity.vcd");
    $dumpvars(0, dut);

    repeat (8) @(negedge clk);
    resetn = 1'b1;
    repeat (cycles) @(posedge clk);
    $finish;
  end
endmodule
