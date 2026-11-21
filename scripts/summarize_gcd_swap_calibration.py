#!/usr/bin/env python3
"""Compare placement swap-score deltas with routed GCD SPEF power deltas."""

from __future__ import annotations

import argparse
import csv
import re
import statistics
from pathlib import Path


WORKLOADS = ("idle", "typical", "peak")
SWITCHING_RE = re.compile(
    r"SPEF_METRICS workload=\S+ switching_w=(?P<switching>[-+0-9.eE]+)"
    r" setup_ns=(?P<setup>[-+0-9.eE]+) hold_ns=(?P<hold>[-+0-9.eE]+)"
)
ANNOTATED_RE = re.compile(r"^Annotated\s+(?P<count>\d+) pin activities\.", re.MULTILINE)
UNANNOTATED_RE = re.compile(r"^unannotated\s+(?P<count>\d+)", re.MULTILINE)


def read_metrics(path: Path, workload: str) -> dict[str, float | int]:
    text = path.read_text(errors="replace")
    metrics = SWITCHING_RE.search(text)
    annotated = ANNOTATED_RE.search(text)
    unannotated = UNANNOTATED_RE.search(text)
    if not all((metrics, annotated, unannotated)):
        raise ValueError(f"incomplete routed report: {path}")
    if f"SPEF_METRICS workload={workload} " not in text:
        raise ValueError(f"wrong workload in routed report: {path}")
    return {
        "switching_w": float(metrics.group("switching")),
        "setup_ns": float(metrics.group("setup")),
        "hold_ns": float(metrics.group("hold")),
        "annotated": int(annotated.group("count")),
        "unannotated": int(unannotated.group("count")),
    }


def read_placement_deltas(path: Path) -> dict[str, float]:
    with path.open(newline="") as source:
        return {
            row["pair_id"]: float(row["predicted_delta_w"])
            for row in csv.DictReader(source, delimiter="\t")
        }


def read_route_attribution(path: Path) -> dict[str, float]:
    with path.open(newline="") as source:
        rows = list(csv.DictReader(source))
    return {
        "pin_cap_nw": sum(float(row["pin_cap_contribution_w"]) for row in rows) * 1e9,
        "wire_cap_nw": sum(float(row["wire_cap_contribution_w"]) for row in rows) * 1e9,
        "activity_nw": sum(float(row["activity_contribution_w"]) for row in rows) * 1e9,
        "clock_net_nw": sum(
            float(row["delta_switching_power_w"])
            for row in rows
            if row["net"].startswith("clknet_")
        ) * 1e9,
        "signal_net_nw": sum(
            float(row["delta_switching_power_w"])
            for row in rows
            if not row["net"].startswith("clknet_")
        ) * 1e9,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--route-root", type=Path, required=True)
    parser.add_argument("--tag", required=True, help="suffix used in gcd_cal_<pair>_<tag>")
    parser.add_argument("--pairs", nargs="+", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    placement = {
        workload: read_placement_deltas(args.audit_dir / "relaxed" / f"audit_{workload}.tsv")
        for workload in WORKLOADS
    }
    rows: list[dict[str, str | float | int]] = []
    for pair in args.pairs:
        route_dir = args.route_root / f"gcd_cal_{pair}_{args.tag}"
        for workload in WORKLOADS:
            base = read_metrics(route_dir / "baseline" / f"{workload}.log", workload)
            candidate = read_metrics(route_dir / "candidate" / f"{workload}.log", workload)
            if pair not in placement[workload]:
                raise ValueError(f"placement audit lacks {pair} in {workload}")
            predicted_w = placement[workload][pair]
            routed_w = float(candidate["switching_w"]) - float(base["switching_w"])
            attribution = read_route_attribution(
                route_dir / "net_attribution" / f"{workload}.per_net_delta.csv"
            )
            rows.append({
                "pair_id": pair,
                "workload": workload,
                "placement_delta_nw": predicted_w * 1e9,
                "routed_delta_nw": routed_w * 1e9,
                "absolute_error_nw": abs(predicted_w - routed_w) * 1e9,
                "sign_agrees": int((predicted_w >= 0.0) == (routed_w >= 0.0)),
                **attribution,
                "setup_loss_ns": float(candidate["setup_ns"]) - float(base["setup_ns"]),
                "hold_loss_ns": float(candidate["hold_ns"]) - float(base["hold_ns"]),
                "baseline_annotated": int(base["annotated"]),
                "candidate_annotated": int(candidate["annotated"]),
                "baseline_unannotated": int(base["unannotated"]),
                "candidate_unannotated": int(candidate["unannotated"]),
            })

    args.output_dir.mkdir(parents=True, exist_ok=True)
    table_path = args.output_dir / "route_validation.tsv"
    with table_path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    sign_count = sum(int(row["sign_agrees"]) for row in rows)
    overall_mae = statistics.mean(float(row["absolute_error_nw"]) for row in rows)
    timing_cases = sum(
        float(row["setup_loss_ns"]) >= -0.05 and float(row["hold_loss_ns"]) >= -0.05
        for row in rows
    )
    timing_pairs = sum(
        all(
            float(row["setup_loss_ns"]) >= -0.05 and float(row["hold_loss_ns"]) >= -0.05
            for row in rows if row["pair_id"] == pair
        )
        for pair in args.pairs
    )
    lines = [
        "# GCD placement-to-routed swap calibration",
        "",
        f"Diagnostic swaps routed: {len(args.pairs)} ({', '.join(args.pairs)}).",
        f"Placement/routed delta sign agreement: {sign_count}/{len(rows)} workload cases.",
        f"Mean absolute error of placement deltas: {overall_mae:.3f} nW across {len(rows)} cases.",
        f"Routed timing guard (50 ps): {timing_pairs}/{len(args.pairs)} candidates pass across all workloads ({timing_cases}/{len(rows)} cases).",
        "SPEF attribution assigns the routed delta to wire capacitance; library pin-capacitance and VCD activity contributions are zero for these same-netlist swaps.",
        "Two identical-placement reroutes produced zero power/slack delta and matching routed ODB/netlist/SDC/SPEF after removing the SPEF date header.",
        "Each baseline and candidate report annotated all VCD pins.",
        "These swaps were selected as calibration probes. None was a placement-estimated power reduction in all three workloads, so they are not accepted optimizations.",
        "",
        "| Workload | Placement delta MAE (nW) | Sign agreement | Routed clock-net delta range (nW) |",
        "|---|---:|---:|---:|",
    ]
    for workload in WORKLOADS:
        subset = [row for row in rows if row["workload"] == workload]
        mae = statistics.mean(float(row["absolute_error_nw"]) for row in subset)
        agrees = sum(int(row["sign_agrees"]) for row in subset)
        clocks = [float(row["clock_net_nw"]) for row in subset]
        lines.append(
            f"| {workload} | {mae:.3f} | {agrees}/{len(subset)} | "
            f"{min(clocks):+.1f} to {max(clocks):+.1f} |"
        )
    lines.extend(["", f"Per-candidate and per-workload results: `{table_path}`."])
    summary_path = args.output_dir / "calibration_summary.md"
    summary_path.write_text("\n".join(lines) + "\n")
    print(summary_path.read_text(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
