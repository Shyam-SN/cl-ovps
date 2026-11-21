`timescale 1ns/1ps

module tb_gcd_activity;
  reg clk = 0;
  reg reset = 1;
  reg [31:0] req_msg = 0;
  reg req_val = 0;
  wire req_rdy;
  wire [15:0] resp_msg;
  reg resp_rdy = 1;
  wire resp_val;

  integer cycle_count = 0;
  integer workload = 0;
  integer i;
  integer sent = 0;
  reg [31:0] seed = 32'h5eed1234;

  always #10 clk = ~clk;
  always @(posedge clk) cycle_count <= cycle_count + 1;

  gcd dut (
    .clk(clk), .req_msg(req_msg), .req_rdy(req_rdy), .req_val(req_val),
    .reset(reset), .resp_msg(resp_msg), .resp_rdy(resp_rdy), .resp_val(resp_val)
  );

  function automatic [15:0] gcd_ref(input [15:0] a_in, input [15:0] b_in);
    reg [15:0] a;
    reg [15:0] b;
    reg [15:0] t;
    begin
      a = a_in;
      b = b_in;
      while (b != 0) begin
        t = a % b;
        a = b;
        b = t;
      end
      gcd_ref = a;
    end
  endfunction

  task automatic transact(input [15:0] a, input [15:0] b);
    reg [15:0] expected;
    integer timeout;
    begin
      expected = gcd_ref(a, b);
      @(negedge clk);
      while (!req_rdy) @(negedge clk);
      req_msg = {a, b};
      req_val = 1;
      @(negedge clk);
      req_val = 0;
      timeout = 0;
      while (!resp_val && timeout < 1000) begin
        @(negedge clk);
        timeout = timeout + 1;
      end
      if (!resp_val) $fatal(1, "GCD transaction timed out: %h %h", a, b);
      if (resp_msg !== expected)
        $fatal(1, "GCD mismatch: %h %h got %h expected %h", a, b, resp_msg, expected);
      @(negedge clk);
      sent = sent + 1;
    end
  endtask

  initial begin
    if (!$value$plusargs("workload=%d", workload)) workload = 1;
    $dumpfile("activity.vcd");
    $dumpvars(0, dut);
    repeat (4) @(negedge clk);
    reset = 0;

    if (workload == 0) begin
      // Idle reference: only the clock and internal clocked activity run.
      repeat (2000) @(negedge clk);
    end else if (workload == 1) begin
      // Typical: deterministic varied operands with idle gaps between requests.
      for (i = 0; i < 16; i = i + 1) begin
        seed = seed * 32'd1664525 + 32'd1013904223;
        transact({8'b0, seed[31:24] | 8'h01}, {8'b0, seed[23:16] | 8'h01});
        repeat (4) @(negedge clk);
      end
      if (cycle_count < 2000) repeat (2000-cycle_count) @(negedge clk);
    end else begin
      // Peak: near-continuous requests with relatively long subtractive paths.
      for (i = 0; i < 75; i = i + 1) begin
        case (i % 4)
          0: transact(16'd2584, 16'd1597);
          1: transact(16'd4093, 16'd2584);
          2: transact(16'd4181, 16'd2584);
          3: transact(16'd6765, 16'd4181);
        endcase
      end
      if (cycle_count < 2000) repeat (2000-cycle_count) @(negedge clk);
    end

    if (workload != 0 && sent == 0) $fatal(1, "non-idle workload sent no requests");
    $display("GCD_ACTIVITY workload=%0d cycles=%0d transactions=%0d", workload, cycle_count, sent);
    $finish;
  end
endmodule
