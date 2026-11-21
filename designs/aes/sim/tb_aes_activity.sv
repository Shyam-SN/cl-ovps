`timescale 1ns/1ps

module tb_aes_activity;
  reg clk = 1'b0;
  reg rst = 1'b0;
  reg ld = 1'b0;
  reg [127:0] key = 128'b0;
  reg [127:0] text_in = 128'b0;
  wire done;
  wire [127:0] text_out;
  reg [63:0] workload = "typical";
  integer i;
  integer transactions;

  aes_cipher_top dut (
    .clk(clk), .rst(rst), .ld(ld), .done(done), .key(key),
    .text_in(text_in), .text_out(text_out)
  );

  // Match the project-local AES 3.8 ns constraint.
  always #1.9 clk = ~clk;

  always @(posedge done) begin
    #0.1;
    $display("AES_RESULT text_out=%032h", text_out);
  end

  initial begin
    if ($value$plusargs("WORKLOAD=%s", workload) == 0) workload = "typical";
    if (workload == "idle") transactions = 0;
    else if (workload == "typical") transactions = 8;
    else if (workload == "peak") transactions = 32;
    else begin
      $display("ERROR: unsupported WORKLOAD=%0s", workload);
      $finish;
    end

    $dumpfile("aes_activity.vcd");
    $dumpvars(0, tb_aes_activity);
    repeat (4) @(negedge clk);
    rst = 1'b1;

    if (workload == "idle") begin
      repeat (80) @(negedge clk);
    end else begin
      for (i = 0; i < transactions; i = i + 1) begin
        @(negedge clk);
        key = 128'h00112233445566778899aabbccddeeff ^ {16{i[7:0]}};
        text_in = 128'hffeeddccbbaa99887766554433221100 ^ {16{(i[7:0] ^ 8'ha5)}};
        ld = 1'b1;
        @(negedge clk);
        ld = 1'b0;
        wait (done === 1'b1);
        if (workload == "typical") @(negedge clk);
      end
      repeat (12) @(negedge clk);
    end
    $dumpoff;
    $finish;
  end
endmodule
