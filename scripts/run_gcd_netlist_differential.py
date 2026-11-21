#!/usr/bin/env python3
"""Differentially simulate baseline and optimized routed GCD netlists."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "build/orfs/results/sky130hd/gcd_power/gcd_baseline20ns/6_final.v"
CELL_MODEL = next((ROOT / "pdk/volare/sky130/versions").glob(
    "*/sky130B/libs.ref/sky130_fd_sc_hd/verilog/sky130_fd_sc_hd.v"), None)

TESTBENCH = r'''`timescale 1ns/1ps
module tb_gcd_compare;
  reg clk = 0;
  reg reset = 1;
  reg [31:0] req_msg = 0;
  reg req_val = 0;
  reg resp_rdy = 1;
  wire req_rdy_base, req_rdy_cand, resp_val_base, resp_val_cand;
  wire [15:0] resp_msg_base, resp_msg_cand;
  integer workload = 1;
  integer cycle_count = 0;
  integer sent = 0;
  integer i;
  reg [31:0] seed = 32'h5eed1234;

  always #10 clk = ~clk;
  always @(posedge clk) begin
    cycle_count <= cycle_count + 1;
    #5;
    if (req_rdy_base !== req_rdy_cand || resp_val_base !== resp_val_cand ||
        (resp_val_base && resp_msg_base !== resp_msg_cand))
      $fatal(1, "Netlist mismatch at cycle %0d: rdy %b/%b val %b/%b msg %h/%h",
             cycle_count, req_rdy_base, req_rdy_cand, resp_val_base, resp_val_cand,
             resp_msg_base, resp_msg_cand);
  end

  gcd_baseline dut_baseline (.clk(clk), .req_rdy(req_rdy_base), .req_val(req_val),
    .reset(reset), .resp_rdy(resp_rdy), .resp_val(resp_val_base),
    .req_msg(req_msg), .resp_msg(resp_msg_base));
  gcd_candidate dut_candidate (.clk(clk), .req_rdy(req_rdy_cand), .req_val(req_val),
    .reset(reset), .resp_rdy(resp_rdy), .resp_val(resp_val_cand),
    .req_msg(req_msg), .resp_msg(resp_msg_cand));

  function automatic [15:0] gcd_ref(input [15:0] a_in, input [15:0] b_in);
    reg [15:0] a, b, t;
    begin
      a = a_in; b = b_in;
      while (b != 0) begin t = a % b; a = b; b = t; end
      gcd_ref = a;
    end
  endfunction

  task automatic transact(input [15:0] a, input [15:0] b);
    reg [15:0] expected;
    integer timeout;
    begin
      expected = gcd_ref(a, b);
      @(negedge clk);
      while (!req_rdy_base || !req_rdy_cand) @(negedge clk);
      req_msg = {a, b}; req_val = 1;
      @(negedge clk); req_val = 0;
      timeout = 0;
      while ((!resp_val_base || !resp_val_cand) && timeout < 1000) begin
        @(negedge clk); timeout = timeout + 1;
      end
      if (!resp_val_base || !resp_val_cand)
        $fatal(1, "GCD transaction timed out: %h %h", a, b);
      if (resp_msg_base !== expected || resp_msg_cand !== expected)
        $fatal(1, "GCD result mismatch: %h %h got %h/%h expected %h",
               a, b, resp_msg_base, resp_msg_cand, expected);
      @(negedge clk); sent = sent + 1;
    end
  endtask

  initial begin
    if (!$value$plusargs("workload=%d", workload)) workload = 1;
    repeat (4) @(negedge clk);
    reset = 0;
    if (workload == 0) begin
      repeat (2000) @(negedge clk);
    end else if (workload == 1) begin
      for (i = 0; i < 16; i = i + 1) begin
        seed = seed * 32'd1664525 + 32'd1013904223;
        transact({8'b0, seed[31:24] | 8'h01}, {8'b0, seed[23:16] | 8'h01});
        repeat (4) @(negedge clk);
      end
      if (cycle_count < 2000) repeat (2000-cycle_count) @(negedge clk);
    end else begin
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
    if (workload != 0 && sent == 0) $fatal(1, "No requests were checked");
    $display("GCD_DIFF_SIM_PASS workload=%0d cycles=%0d transactions=%0d",
             workload, cycle_count, sent);
    $finish;
  end
endmodule
'''


def uniquify_netlist(source: Path, target: Path, suffix: str) -> None:
    text = source.read_text(errors="replace")
    import re
    declarations = re.findall(r"(?m)^module\s+(\\[^\s(]+|[A-Za-z_$][A-Za-z0-9_$]*)", text)
    if not declarations or "gcd" not in declarations:
        raise ValueError(f"no gcd top module in {source}")
    # Each file is compiled into the same simulation; rename only this design's
    # module declarations and corresponding cell references so the two routed
    # netlists can coexist. Escaped Yosys module names end at whitespace.
    for index, old_name in enumerate(declarations):
        new_name = f"gcd_{suffix}" if old_name == "gcd" else f"helper_{suffix}_{index}"
        if old_name.startswith("\\"):
            text = re.sub(re.escape(old_name) + r"(?=\s)", new_name, text)
        else:
            text = re.sub(rf"\b{re.escape(old_name)}\b", new_name, text)
    target.write_text(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-netlist", type=Path, default=BASELINE)
    parser.add_argument("--candidate-netlist", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    if CELL_MODEL is None:
        raise FileNotFoundError("Sky130HD functional cell model not found")
    primitives = CELL_MODEL.with_name("primitives.v")
    baseline = args.baseline_netlist.resolve()
    candidate = args.candidate_netlist.resolve()
    out = args.output_dir.resolve()
    if not baseline.is_file() or not candidate.is_file() or not primitives.is_file():
        raise FileNotFoundError("baseline, candidate, or primitive model is missing")
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"refusing to overwrite {out}")
    out.mkdir(parents=True, exist_ok=True)
    iverilog, vvp = shutil.which("iverilog"), shutil.which("vvp")
    if not iverilog or not vvp:
        raise FileNotFoundError("Icarus Verilog (iverilog and vvp) is required")

    base_copy, cand_copy = out / "baseline.v", out / "candidate.v"
    uniquify_netlist(baseline, base_copy, "baseline")
    uniquify_netlist(candidate, cand_copy, "candidate")
    tb = out / "tb_gcd_compare.sv"
    tb.write_text(TESTBENCH)
    executable = out / "gcd_compare.vvp"
    compile_cmd = [iverilog, "-g2012", "-DFUNCTIONAL", "-DUNIT_DELAY=", "-s",
                   "tb_gcd_compare", "-I", str(CELL_MODEL.parent), "-o", str(executable),
                   str(primitives), str(CELL_MODEL), str(base_copy), str(cand_copy), str(tb)]
    compiled = subprocess.run(compile_cmd, cwd=out, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=args.timeout_seconds, check=False)
    (out / "compile.log").write_text(compiled.stdout)
    if compiled.returncode:
        raise RuntimeError(f"Icarus compilation failed; see {out / 'compile.log'}")

    summary = []
    for workload in (0, 1, 2):
        result = subprocess.run([vvp, str(executable), f"+workload={workload}"], cwd=out,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=args.timeout_seconds, check=False)
        (out / f"workload_{workload}.log").write_text(result.stdout)
        if result.returncode or "GCD_DIFF_SIM_PASS" not in result.stdout:
            raise RuntimeError(f"differential simulation failed for workload {workload}; "
                               f"see {out / f'workload_{workload}.log'}")
        summary.append(result.stdout.strip().splitlines()[-2])
    (out / "status.txt").write_text("DIFFERENTIAL_SIMULATION_PASS\n")
    (out / "summary.txt").write_text("\n".join(summary) + "\n")
    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
