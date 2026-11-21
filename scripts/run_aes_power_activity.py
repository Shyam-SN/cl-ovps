#!/usr/bin/env python3
"""Simulate routed AES gate-level netlists for idle, typical, and peak workloads."""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "build/orfs/results/sky130hd/aes"
TESTBENCH = ROOT / "designs/aes/sim/tb_aes_activity.sv"
CELL_MODEL = next((ROOT / "pdk/volare/sky130/versions").glob(
    "*/sky130B/libs.ref/sky130_fd_sc_hd/verilog/sky130_fd_sc_hd.v"), None)
DEFAULT_OUTPUT = ROOT / "build/activity/aes_power38_20261101a"
WORKLOADS = ("idle", "typical", "peak")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variants", nargs="+", required=True)
    parser.add_argument("--workloads", nargs="+", choices=WORKLOADS, default=list(WORKLOADS))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args()

    iverilog, vvp = shutil.which("iverilog"), shutil.which("vvp")
    if not iverilog or not vvp:
        raise FileNotFoundError("Icarus Verilog (iverilog and vvp) is required")
    if CELL_MODEL is None or not TESTBENCH.is_file():
        raise FileNotFoundError("AES testbench or Sky130HD functional cell model is missing")
    primitives = CELL_MODEL.with_name("primitives.v")
    if not primitives.is_file():
        raise FileNotFoundError(f"Sky130HD primitive model is missing: {primitives}")

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, object]] = []
    results_by_workload: dict[str, list[list[str]]] = {}
    for variant in args.variants:
        netlist = RESULTS / variant / "6_final.v"
        if not netlist.is_file():
            raise FileNotFoundError(f"final gate-level netlist is missing: {netlist}")
        variant_dir = output / variant
        variant_dir.mkdir(parents=True, exist_ok=True)
        executable = variant_dir / "aes_activity.vvp"
        compile_log = variant_dir / "compile.log"
        compiled = subprocess.run(
            [iverilog, "-g2012", "-s", "tb_aes_activity", "-I", str(CELL_MODEL.parent),
             "-o", str(executable), str(TESTBENCH), str(netlist), str(CELL_MODEL), str(primitives)],
            cwd=variant_dir, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=args.timeout_seconds, check=False)
        compile_log.write_text(compiled.stdout)
        if compiled.returncode:
            raise RuntimeError(f"gate-level compile failed for {variant}; see {compile_log}")

        for workload in args.workloads:
            workload_dir = variant_dir / workload
            if workload_dir.exists():
                raise FileExistsError(f"refusing to overwrite activity run: {workload_dir}")
            workload_dir.mkdir()
            sim_log = workload_dir / "simulation.log"
            simulated = subprocess.run(
                [vvp, str(executable), f"+WORKLOAD={workload}"], cwd=workload_dir,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=args.timeout_seconds, check=False)
            sim_log.write_text(simulated.stdout)
            vcd = workload_dir / "aes_activity.vcd"
            if simulated.returncode or not vcd.is_file() or "$enddefinitions $end" not in vcd.read_text(errors="replace"):
                raise RuntimeError(f"gate-level simulation failed or emitted incomplete VCD: {sim_log}")
            if "ERROR:" in simulated.stdout:
                raise RuntimeError(f"testbench rejected workload {workload}; see {sim_log}")
            results = re.findall(r"^AES_RESULT text_out=([0-9a-fA-FxXzZ]+)$", simulated.stdout, re.MULTILINE)
            results_by_workload.setdefault(workload, []).append(results)
            manifest.append({"variant": variant, "workload": workload, "netlist": str(netlist),
                             "vcd": str(vcd), "vcd_bytes": vcd.stat().st_size,
                             "simulation_log": str(sim_log)})
            print(f"AES_POWER_ACTIVITY variant={variant} workload={workload} vcd={vcd} bytes={vcd.stat().st_size}")

    for workload, traces in results_by_workload.items():
        if (workload != "idle" and not traces[0]) or any(trace != traces[0] for trace in traces[1:]):
            raise RuntimeError(f"gate-level AES output mismatch across variants for workload={workload}")

    with (output / "activity_manifest.tsv").open("w", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(manifest[0]), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
