#!/usr/bin/env python3
"""Generate an AES activity VCD from each selected routed gate-level netlist."""

from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPERIMENT = ROOT / "build/power/aes_tier1_generalization_20261031g"
DEFAULT_TB = DEFAULT_EXPERIMENT / "analysis/aes_activity_tb.v"
DEFAULT_OUTPUT = ROOT / "build/activity/aes_gatelevel_spef_20261031a"
RESULTS = ROOT / "build/orfs/results/sky130hd/aes"
CELL_MODEL = next((ROOT / "pdk/volare/sky130/versions").glob(
    "*/sky130B/libs.ref/sky130_fd_sc_hd/verilog/sky130_fd_sc_hd.v"), None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variants", nargs="+", default=[
        "aes_tier1gen_20261031_base",
        "aes_tier1gen_20261031_xrow9002",
        "aes_tier1gen_20261031_xrow9006",
        "aes_tier1gen_20261031_xrow9008",
    ])
    parser.add_argument("--testbench", type=Path, default=DEFAULT_TB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()

    iverilog = shutil.which("iverilog")
    vvp = shutil.which("vvp")
    if not iverilog or not vvp:
        raise FileNotFoundError("Icarus Verilog (iverilog and vvp) is required")
    if CELL_MODEL is None:
        raise FileNotFoundError("Sky130HD functional Verilog cell model is missing")
    primitives = CELL_MODEL.with_name("primitives.v")
    if not args.testbench.is_file() or not primitives.is_file():
        raise FileNotFoundError("AES testbench or Sky130HD primitive models are missing")

    model_dir = CELL_MODEL.parent
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for variant in args.variants:
        netlist = RESULTS / variant / "6_final.v"
        if not netlist.is_file():
            raise FileNotFoundError(f"routed gate-level netlist is missing: {netlist}")
        out_dir = args.output_dir / variant
        if out_dir.exists():
            raise FileExistsError(f"refusing to overwrite existing activity run: {out_dir}")
        out_dir.mkdir(parents=True)
        executable = out_dir / "aes_activity.vvp"
        compile_log = out_dir / "compile.log"
        compile_cmd = [iverilog, "-g2012", "-s", "tb_aes_activity", "-I", str(model_dir),
                       "-o", str(executable), str(args.testbench), str(netlist),
                       str(CELL_MODEL), str(primitives)]
        compiled = subprocess.run(compile_cmd, cwd=out_dir, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  timeout=args.timeout_seconds, check=False)
        compile_log.write_text(compiled.stdout)
        if compiled.returncode:
            raise RuntimeError(f"Icarus compilation failed for {variant}; see {compile_log}")
        sim_log = out_dir / "simulation.log"
        simulated = subprocess.run([vvp, str(executable)], cwd=out_dir, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=args.timeout_seconds, check=False)
        sim_log.write_text(simulated.stdout)
        generated = out_dir / "aes_activity.vcd"
        if simulated.returncode or not generated.is_file():
            raise RuntimeError(f"gate-level activity simulation failed for {variant}; see {sim_log}")
        if "$enddefinitions $end" not in generated.read_text(errors="replace"):
            raise RuntimeError(f"VCD is incomplete for {variant}: {generated}")
        manifest.append({"variant": variant, "netlist": str(netlist),
                         "vcd": str(generated), "vcd_bytes": generated.stat().st_size,
                         "simulation_log": str(sim_log)})
        print(f"AES_GATELEVEL_ACTIVITY variant={variant} vcd={generated} bytes={generated.stat().st_size}")

    with (args.output_dir / "activity_manifest.tsv").open("w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=list(manifest[0]), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
