`timescale 1ps/1ps

module tb;
  reg clk = 1'b0;
  reg req_val = 1'b0;
  reg reset = 1'b1;
  reg resp_rdy = 1'b0;
  reg [31:0] req_msg = 32'b0;

  wire base_req_rdy, base_resp_val;
  wire [15:0] base_resp_msg;
  wire opt_req_rdy, opt_resp_val;
  wire [15:0] opt_resp_msg;

  reg [8*1024-1:0] workload;
  reg [8*1024-1:0] vcd_path;
  integer cycle;

  always #250 clk = ~clk;

  gcd_base dut_base (
    .clk(clk), .req_msg(req_msg), .req_rdy(base_req_rdy),
    .req_val(req_val), .reset(reset), .resp_msg(base_resp_msg),
    .resp_rdy(resp_rdy), .resp_val(base_resp_val)
  );
  gcd_candidate dut_candidate (
    .clk(clk), .req_msg(req_msg), .req_rdy(opt_req_rdy),
    .req_val(req_val), .reset(reset), .resp_msg(opt_resp_msg),
    .resp_rdy(resp_rdy), .resp_val(opt_resp_val)
  );

  task check_equivalence;
    begin
      if ({base_req_rdy, base_resp_val, base_resp_msg} !==
          {opt_req_rdy, opt_resp_val, opt_resp_msg}) begin
        $display("DIFF cycle=%0d workload=%0s base=%h opt=%h", cycle, workload,
                 {base_req_rdy, base_resp_val, base_resp_msg},
                 {opt_req_rdy, opt_resp_val, opt_resp_msg});
        $fatal(1, "baseline and multi-Vt outputs differ");
      end
      if ((^({base_req_rdy, base_resp_val, base_resp_msg}) === 1'bx) ||
          (^({opt_req_rdy, opt_resp_val, opt_resp_msg}) === 1'bx)) begin
        $fatal(1, "unknown output after reset at cycle %0d", cycle);
      end
    end
  endtask

  initial begin
    if (!$value$plusargs("workload=%s", workload)) $fatal(1, "missing +workload");
    if (!$value$plusargs("vcd=%s", vcd_path)) $fatal(1, "missing +vcd");
    $dumpfile(vcd_path);
    $dumpvars(0, dut_base);
    $dumpvars(0, dut_candidate);

    repeat (4) @(negedge clk);
    reset = 1'b0;
    req_val = 1'b0;
    resp_rdy = 1'b1;
    req_msg = 32'b0;
    cycle = 0;

    if (workload == "idle") begin
      repeat (200) begin
        @(posedge clk); #1; check_equivalence(); cycle = cycle + 1;
      end
    end else if (workload == "typical") begin
      for (cycle = 0; cycle < 300; cycle = cycle + 1) begin
        @(negedge clk);
        req_val = ((cycle % 8) < 3);
        resp_rdy = ((cycle % 5) != 0);
        req_msg[31:16] = 16'h1357 + (cycle * 17);
        req_msg[15:0] = 16'h0f0f ^ (cycle * 29);
        @(posedge clk); #1; check_equivalence();
      end
    end else if (workload == "peak") begin
      for (cycle = 0; cycle < 300; cycle = cycle + 1) begin
        @(negedge clk);
        req_val = 1'b1;
        resp_rdy = 1'b1;
        req_msg = (cycle[0] ? 32'h5555aaaa : 32'haaaa5555);
        @(posedge clk); #1; check_equivalence();
      end
    end else begin
      $fatal(1, "unknown workload '%0s'", workload);
    end

    $display("GATE_SIM_PASS workload=%0s cycles=%0d", workload, cycle);
    $finish;
  end
endmodule
