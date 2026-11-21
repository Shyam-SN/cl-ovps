#!/usr/bin/env python3
"""Run SPEF/VCD power and timing reports for routed AES variants/workloads."""

from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "build/orfs/results/sky130hd/aes"
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
POWER_TCL = ROOT / "scripts/aes_report_spef_power.tcl"
DEFAULT_ACTIVITY = ROOT / "build/activity/aes_power38_20261101a"


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def run_report(variant: str, workload: str, activity_dir: Path, out_dir: Path) -> tuple[dict[str, object], str]:
    result_dir = RESULTS / variant
    odb, sdc, spef = result_dir / "6_final.odb", result_dir / "6_final.sdc", result_dir / "6_final.spef"
    vcd = activity_dir / variant / workload / "aes_activity.vcd"
    for path in (odb, sdc, spef, vcd):
        if not path.is_file():
            raise FileNotFoundError(path)
    env = os.environ.copy()
    env.update({"AES_POWER_ODB": str(odb), "AES_POWER_SDC": str(sdc),
                "AES_POWER_SPEF": str(spef), "AES_ACTIVITY_VCD": str(vcd)})
    result = subprocess.run([str(OPENROAD), "-no_init", "-exit", str(POWER_TCL)], cwd=ROOT,
                            env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    log = out_dir / f"{variant}.{workload}.power.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(result.stdout)
    if result.returncode:
        raise RuntimeError(f"SPEF/VCD report failed; inspect {log}")

    annotation = re.search(r"Annotated\s+(\d+) pin activities", result.stdout)
    unannotated = re.search(r"^unannotated\s+(\d+)\s*$", result.stdout, re.MULTILINE | re.IGNORECASE)
    total = re.search(
        r"^Total\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)",
        result.stdout, re.MULTILINE)
    timing = re.search(r"AES_SPEF_TIMING setup_ns=([0-9.eE+-]+) hold_ns=([0-9.eE+-]+)", result.stdout)
    clock_cells = re.search(r"AES_SPEF_CLOCK_CELLS buffers=(\d+) dummies=(\d+) inverters=(\d+)", result.stdout)
    cell_rows = re.compile(
        r"^\s*([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+[0-9.eE+-]+\s+[0-9.eE+-]+\s+(clk(?:buf|load|inv)[^\s]*)$",
        re.MULTILINE,
    ).findall(result.stdout)
    if not all((annotation, unannotated, total, timing, clock_cells)):
        raise RuntimeError(f"SPEF/VCD report parser did not find all required summary fields in {log}")
    internal, switching, leakage, total_power = map(float, total.groups())
    setup_ns, hold_ns = map(float, timing.groups())
    clock_internal = sum(float(row[0]) for row in cell_rows)
    clock_switching = sum(float(row[1]) for row in cell_rows)
    return ({"variant": variant, "workload": workload, "vcd_annotated_pin_activities": int(annotation.group(1)),
             "vcd_unannotated_pins": int(unannotated.group(1)), "setup_slack_ns": setup_ns,
             "hold_slack_ns": hold_ns, "internal_power_w": internal, "switching_power_w": switching,
             "dynamic_power_w": internal + switching, "leakage_power_w": leakage,
             "total_power_w": total_power, "clock_cell_internal_power_w": clock_internal,
             "clock_cell_switching_power_w": clock_switching,
             "clock_buffers": int(clock_cells.group(1)), "clock_dummies": int(clock_cells.group(2)),
             "clock_inverters": int(clock_cells.group(3)), "report_log": str(log), "vcd": str(vcd)}, result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variants", nargs="+", required=True)
    parser.add_argument("--workloads", nargs="+", choices=("idle", "typical", "peak"),
                        default=["idle", "typical", "peak"])
    parser.add_argument("--activity-dir", type=Path, default=DEFAULT_ACTIVITY)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    activity_dir = args.activity_dir.resolve()

    rows: list[dict[str, object]] = []
    for variant in args.variants:
        for workload in args.workloads:
            row, _text = run_report(variant, workload, activity_dir, output)
            rows.append(row)
            print(f"AES_POWER_REPORT variant={variant} workload={workload} dynamic_mw="
                  f"{float(row['dynamic_power_w']) * 1e3:.6f} unannotated={row['vcd_unannotated_pins']} "
                  f"setup_ns={row['setup_slack_ns']}")

    baseline = {str(row["workload"]): row for row in rows if row["variant"] == args.variants[0]}
    for row in rows:
        base = baseline[str(row["workload"])]
        setup_delta_ps = (float(row["setup_slack_ns"]) - float(base["setup_slack_ns"])) * 1000.0
        hold_delta_ps = (float(row["hold_slack_ns"]) - float(base["hold_slack_ns"])) * 1000.0
        row["dynamic_delta_w_vs_baseline"] = float(row["dynamic_power_w"]) - float(base["dynamic_power_w"])
        row["clock_cell_internal_delta_w_vs_baseline"] = (
            float(row["clock_cell_internal_power_w"]) - float(base["clock_cell_internal_power_w"]))
        row["setup_delta_ps_vs_baseline"] = setup_delta_ps
        row["hold_delta_ps_vs_baseline"] = hold_delta_ps
        row["routed_50ps_timing_guard_pass"] = setup_delta_ps >= -50.0 and hold_delta_ps >= -50.0
        row["routed_setup_nonnegative"] = float(row["setup_slack_ns"]) >= 0.0
        row["vcd_full_annotation_pass"] = int(row["vcd_unannotated_pins"]) == 0
    write_tsv(output / "aes_routed_power_summary.tsv", rows)
    failures: list[str] = []
    for row in rows:
        variant, workload = str(row["variant"]), str(row["workload"])
        if int(row["vcd_unannotated_pins"]) != 0:
            failures.append(f"{variant}/{workload}: {row['vcd_unannotated_pins']} unannotated pins")
        if variant != args.variants[0]:
            if float(row["setup_delta_ps_vs_baseline"]) < -50.0:
                failures.append(f"{variant}/{workload}: setup regression {row['setup_delta_ps_vs_baseline']:.3f} ps")
            if float(row["hold_delta_ps_vs_baseline"]) < -50.0:
                failures.append(f"{variant}/{workload}: hold regression {row['hold_delta_ps_vs_baseline']:.3f} ps")
            if variant == args.variants[-1] and float(row["dynamic_delta_w_vs_baseline"]) >= 0.0:
                failures.append(f"{variant}/{workload}: dynamic power did not improve")
    if failures:
        (output / "signoff_gate_failures.txt").write_text("\n".join(failures) + "\n")
        raise RuntimeError("AES routed sign-off gates failed; inspect "
                           f"{output / 'signoff_gate_failures.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
