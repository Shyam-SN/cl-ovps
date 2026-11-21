#!/usr/bin/env python3
"""Measure AES Tier-1 oracle trials from frozen placement/CTS and routed outputs."""

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
POWER_TCL = ROOT / "build/power/aes_tier1_generalization_20261031g/analysis/aes_report_cts_power.tcl"
HPWL_TCL = ROOT / "build/power/aes_tier1_generalization_20261031g/analysis/aes_signal_hpwl.tcl"


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def run_openroad(script: Path, env: dict[str, str], log: Path) -> str:
    result = subprocess.run([str(OPENROAD), "-no_init", "-exit", str(script)], cwd=ROOT,
                            env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    log.write_text(result.stdout)
    if result.returncode:
        raise RuntimeError(f"OpenROAD query failed; inspect {log}")
    return result.stdout


def signal_hpwl(odb: Path, analysis: Path, label: str) -> int:
    result_path = analysis / f"{label}.signal_hpwl_dbu"
    log_path = analysis / f"{label}.signal_hpwl.log"
    env = os.environ.copy()
    env.update({"FEATURE_ODB": str(odb), "FEATURE_HPWL_OUT": str(result_path)})
    run_openroad(HPWL_TCL, env, log_path)
    return int(result_path.read_text().strip())


def cts_cell_power(variant: str, analysis: Path, label: str, stage: str) -> dict[str, object]:
    result_dir = RESULTS / variant
    if stage == "cts":
        odb, sdc, spef = result_dir / "4_1_cts.odb", result_dir / "4_cts.sdc", None
    else:
        odb, sdc, spef = result_dir / "6_final.odb", result_dir / "6_final.sdc", result_dir / "6_final.spef"
    for path in (odb, sdc, *([spef] if spef is not None else [])):
        if not path.is_file():
            raise FileNotFoundError(f"{stage} power query missing {path}")
    env = os.environ.copy()
    env.update({"AES_POWER_ODB": str(odb), "AES_POWER_SDC": str(sdc)})
    if spef is not None:
        env["AES_POWER_SPEF"] = str(spef)
    log = analysis / f"{label}.{stage}_clock_power.log"
    output = run_openroad(POWER_TCL, env, log)
    pattern = re.compile(
        r"^\s*([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+(\S+)$",
        re.MULTILINE,
    )
    cells: dict[str, tuple[float, float, float, float]] = {}
    for match in pattern.finditer(output):
        name = match.group(5)
        if name.startswith(("clkbuf", "clkload")):
            cells[name] = tuple(float(match.group(i)) for i in range(1, 5))
    if not cells:
        raise RuntimeError(f"no clock-cell power rows parsed from {log}")
    dummy = [values for name, values in cells.items() if name.startswith("clkload")]
    buffers = [values for name, values in cells.items() if name.startswith("clkbuf")]
    setup_match = re.search(r"worst slack max\s+([0-9.eE+-]+)", output)
    hold_match = re.search(r"worst slack min\s+([0-9.eE+-]+)", output)
    return {
        "cells": cells,
        "dummy_count": len(dummy),
        "buffer_count": len(buffers),
        "dummy_internal_w": sum(values[0] for values in dummy),
        "dummy_switching_w": sum(values[1] for values in dummy),
        "buffer_internal_w": sum(values[0] for values in buffers),
        "buffer_switching_w": sum(values[1] for values in buffers),
        "cts_internal_w": sum(values[0] for values in cells.values()),
        "cts_switching_w": sum(values[1] for values in cells.values()),
        # report_worst_slack prints values in the SDC time unit (ns here).
        "setup_slack_ns": float(setup_match.group(1)) if setup_match else "",
        "hold_slack_ns": float(hold_match.group(1)) if hold_match else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    args = parser.parse_args()
    experiment = args.experiment_dir.resolve()
    analysis = experiment / "analysis"
    predictions = read_tsv(analysis / "frozen_grt_cap_predictions.tsv")
    pool_rows = read_tsv(analysis / "candidate_pool.tsv")
    baseline_variant = "aes_tier1gen_20261031_base"
    candidate_placements = experiment / "candidate_odb"
    candidates = [row["candidate_id"] for row in predictions]
    sampled_branches = sorted({row.get("branch_before", "") for row in pool_rows if row.get("branch_before")})
    variants = {"baseline": baseline_variant}
    variants.update({row["candidate_id"]: row["candidate_flow_variant"] for row in predictions})

    placement_hpwl = {"baseline": signal_hpwl(RESULTS / baseline_variant / "3_5_place_dp.odb", analysis, "baseline")}
    for row in predictions:
        candidate = row["candidate_id"]
        placement_hpwl[candidate] = signal_hpwl(candidate_placements / f"{candidate}.odb", analysis, candidate)

    power: dict[tuple[str, str], dict[str, object]] = {}
    for candidate, variant in variants.items():
        for stage in ("cts", "routed"):
            power[(candidate, stage)] = cts_cell_power(variant, analysis, candidate, stage)

    base_hpwl = placement_hpwl["baseline"]
    decisions: list[dict[str, object]] = []
    for pred in predictions:
        candidate = pred["candidate_id"]
        hpwl_delta_pct = 100.0 * (placement_hpwl[candidate] - base_hpwl) / base_hpwl
        row: dict[str, object] = {
            "candidate_id": candidate,
            "instance": pred["instance"],
            "move_type": pred["move_type"],
            "dummy_master_change_count": pred["dummy_master_change_count"],
            "dummy_master_changes": pred["dummy_master_changes"],
            "signal_hpwl_delta_pct": hpwl_delta_pct,
            "grt_signal_cap_delta_ff": pred["grt_signal_cap_delta_ff"],
            "grt_clock_cap_delta_ff": pred["grt_clock_cap_delta_ff"],
        }
        for stage, prefix in (("cts", "cts"), ("routed", "routed")):
            candidate_power = power[(candidate, stage)]
            baseline_power = power[("baseline", stage)]
            for field in ("dummy_count", "buffer_count"):
                row[f"{prefix}_{field}"] = candidate_power[field]
            for field in ("dummy_internal_w", "dummy_switching_w", "buffer_internal_w",
                          "buffer_switching_w", "cts_internal_w", "cts_switching_w"):
                row[f"{prefix}_{field}_delta_uw"] = (
                    float(candidate_power[field]) - float(baseline_power[field])) * 1e6
            if stage == "routed":
                setup_delta_ps = (float(candidate_power["setup_slack_ns"])
                                  - float(baseline_power["setup_slack_ns"])) * 1000.0
                hold_delta_ps = (float(candidate_power["hold_slack_ns"])
                                 - float(baseline_power["hold_slack_ns"])) * 1000.0
                row["routed_setup_slack_ns"] = candidate_power["setup_slack_ns"]
                row["routed_hold_slack_ns"] = candidate_power["hold_slack_ns"]
                row["setup_slack_delta_ps"] = setup_delta_ps
                row["hold_slack_delta_ps"] = hold_delta_ps
                row["timing_50ps_guard_pass"] = setup_delta_ps >= -50.0 and hold_delta_ps >= -50.0
        row["tier1_gate_pass"] = (
            float(row["cts_dummy_internal_w_delta_uw"]) <= -1.0 and hpwl_delta_pct <= 1.5
        )
        row["routed_dummy_internal_win"] = float(row["routed_dummy_internal_w_delta_uw"]) <= -1.0
        decisions.append(row)
    write_tsv(analysis / "tier1_aes_trial_validation.tsv", decisions)

    gate_passes = [row for row in decisions if row["tier1_gate_pass"]]
    route_wins = [row for row in decisions if row["routed_dummy_internal_win"]]
    timing_passes = [row for row in decisions if row.get("timing_50ps_guard_pass")]
    report = [
        "# AES Sky130HD Tier-1 generalization probe",
        "",
        f"The baseline CTS has {power[('baseline', 'cts')]['dummy_count']} dummy cells and "
        f"{power[('baseline', 'cts')]['buffer_count']} named clock buffers.",
        f"Frozen isolated candidates evaluated: {len(decisions)}.",
        f"Baseline leaf branch(es) sampled: {', '.join(sampled_branches)}.",
        "This was a boundary-targeted mechanism probe. All selected sinks landed on one baseline leaf, "
        "so the candidate count is not a yield estimate across the full 90-leaf AES tree.",
        f"Candidates passing the Tier-1 oracle rule (CTS dummy internal delta <= -1 uW, "
        f"signal HPWL <= +1.5%): {len(gate_passes)}.",
        f"Candidates retaining a >=1 uW dummy internal reduction after routing: {len(route_wins)}.",
        f"Candidates within the 50 ps setup/hold delta guard against baseline: {len(timing_passes)}/{len(decisions)}.",
        "",
        "Clock-cell internal power is reported from the Sky130 Liberty model at the SDC-constrained 3.6 ns clock, "
        "with placement parasitics for CTS prediction and extracted SPEF for routed validation. Only `clkload*` "
        "and `clkbuf*` instances are included; the RTL VCD was excluded from power totals because it left "
        "52,826 pins unannotated. No whole-design AES dynamic-power claim is made.",
        "",
        "Per-candidate HPWL, CTS-oracle and routed clock-cell power deltas are in `tier1_aes_trial_validation.tsv`.",
    ]
    (analysis / "tier1_generalization_report.md").write_text("\n".join(report) + "\n")
    print(f"AES_TIER1_VALIDATION candidates={len(decisions)} gate_passes={len(gate_passes)} "
          f"routed_dummy_wins={len(route_wins)} report={analysis / 'tier1_generalization_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
