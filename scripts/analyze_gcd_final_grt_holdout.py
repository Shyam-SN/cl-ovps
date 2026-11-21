#!/usr/bin/env python3
"""Evaluate frozen GCD Tier 1/2 gates against detailed-route holdout labels."""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
from pathlib import Path

from analyze_gcd_cts_branch_proxy import kendall_tau_b, spearman
from analyze_gcd_cts_branch_proxy import cts_cell_masters, extract_cts_cell_power

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "build/orfs/results/sky130hd/gcd_power"
ACTIVITY = ROOT / "build/activity"
CTS_POWER_TCL = ROOT / "scripts/gcd_report_cts_cell_power.tcl"
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
WORKLOADS = ("idle", "typical", "peak")


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    """Write rows with the union of their fields (gate and rank rows differ)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def confusion(predicted: list[bool], actual: list[bool]) -> dict[str, object]:
    tp = sum(p and a for p, a in zip(predicted, actual))
    fp = sum(p and not a for p, a in zip(predicted, actual))
    fn = sum(not p and a for p, a in zip(predicted, actual))
    tn = sum(not p and not a for p, a in zip(predicted, actual))
    return {
        "n": len(predicted), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": tp / (tp + fp) if tp + fp else math.nan,
        "recall": tp / (tp + fn) if tp + fn else math.nan,
    }


def verify_pre_route_hashes(analysis: Path) -> int:
    errors: list[str] = []
    for row in read_tsv(analysis / "frozen_prediction_integrity.tsv"):
        path = Path(row["path"])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            errors.append(str(path))
    if errors:
        raise ValueError("pre-route freeze hash mismatch: " + ", ".join(errors[:5]))
    marker = {row["metric"]: row["value"] for row in read_tsv(analysis / "frozen_before_detail_route.tsv")}
    predictions = analysis / "frozen_three_gate_predictions.tsv"
    if marker.get("frozen_before_detail_route") != "YES" or marker.get("three_gate_predictions_sha256") != hashlib.sha256(predictions.read_bytes()).hexdigest():
        raise ValueError("pre-route freeze marker or score hash is invalid")
    return len(read_tsv(analysis / "frozen_prediction_integrity.tsv"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--actionable-precision-cutoff", type=float, default=0.70)
    args = parser.parse_args()
    experiment = args.experiment_dir.resolve()
    analysis = experiment / "analysis"
    verified_hash_count = verify_pre_route_hashes(analysis)

    frozen_rows = read_tsv(analysis / "frozen_three_gate_predictions.tsv")
    frozen = {row["candidate_id"]: row for row in frozen_rows}
    routes = read_tsv(experiment / "route_manifest.tsv")
    route_by = {row["candidate_id"]: row for row in routes}
    if set(route_by) != set(frozen) or any(int(row["return_code"]) != 0 for row in routes):
        raise ValueError("route manifest is incomplete or contains failed routes")

    route_summary = {row["candidate_id"]: row for row in read_tsv(analysis / "candidate_summary.tsv")}
    power_rows = read_tsv(analysis / "workload_power.tsv")
    power_by = {(row["candidate_id"], row["workload"]): row for row in power_rows}
    if set(route_summary) != set(frozen) or any((cid, workload) not in power_by for cid in frozen for workload in WORKLOADS):
        raise ValueError("routed summary does not cover all frozen candidates/workloads")

    baseline_result = RESULTS / "gcd_baseline20ns"
    baseline_netlist = baseline_result / "6_final.v"
    baseline_masters = cts_cell_masters(baseline_netlist)
    baseline_power = extract_cts_cell_power(
        OPENROAD, CTS_POWER_TCL, baseline_result,
        ACTIVITY / "gcd_baseline20ns/routed/idle.vcd",
        analysis / "routed_cts_cell_power/baseline_idle.log",
    )
    baseline_dummy = sum(value[0] for name, value in baseline_power.items() if name.startswith("clkload"))

    decision_rows: list[dict[str, object]] = []
    for candidate_id in sorted(frozen):
        prediction = frozen[candidate_id]
        route = route_by[candidate_id]
        summary = route_summary[candidate_id]
        variant = route["route_variant"]
        result_dir = RESULTS / variant
        candidate_vcd = ACTIVITY / variant / "candidate/idle.vcd"
        cell_power = extract_cts_cell_power(
            OPENROAD, CTS_POWER_TCL, result_dir, candidate_vcd,
            analysis / "routed_cts_cell_power" / f"{candidate_id}_idle.log",
        )
        candidate_masters = cts_cell_masters(result_dir / "6_final.v")
        dummy_names = {name for name in baseline_masters.keys() | candidate_masters.keys() if name.startswith("clkload")}
        changed = sorted(name for name in dummy_names if baseline_masters.get(name) != candidate_masters.get(name))
        candidate_dummy = sum(cell_power.get(name, (0.0, 0.0, 0.0, 0.0))[0] for name in dummy_names)
        dummy_delta_nw = (candidate_dummy - baseline_dummy) * 1e9
        timing_ok = summary["timing_guard"] == "PASS"
        zero_unannotated = all(int(power_by[(candidate_id, workload)]["unannotated_pins"]) == 0 for workload in WORKLOADS)
        all_net_workload_win = all(float(power_by[(candidate_id, workload)]["switching_delta_nw"]) < 0.0 for workload in WORKLOADS)
        actual_tier2 = timing_ok and zero_unannotated and all_net_workload_win
        actual_tier1 = timing_ok and zero_unannotated and bool(changed) and dummy_delta_nw <= -1000.0
        row: dict[str, object] = {
            "candidate_id": candidate_id,
            "instance": route["instance"],
            "signal_hpwl_delta_pct": float(prediction["signal_hpwl_delta_pct"]),
            "tier1_predicted": int(prediction["tier1_pass"]),
            "tier1_routed_dummy_internal_delta_nw": dummy_delta_nw,
            "tier1_routed_dummy_master_changes": ";".join(
                f"{name}:{baseline_masters.get(name, 'ABSENT')}->{candidate_masters.get(name, 'ABSENT')}" for name in changed
            ),
            "tier1_routed_positive": int(actual_tier1),
            "tier2a_predicted": int(prediction["tier2a_pass"]),
            "tier2b_predicted": int(prediction["tier2b_pass"]),
            "routed_timing_guard": "PASS" if timing_ok else "FAIL",
            "zero_unannotated_vcd_pins": int(zero_unannotated),
            "routed_all_workload_net_switching_win": int(all_net_workload_win),
            "tier2_routed_actionable_positive": int(actual_tier2),
        }
        for workload in WORKLOADS:
            route_power = power_by[(candidate_id, workload)]
            for component in ("signal", "clock", "switching"):
                row[f"routed_{workload}_{component}_delta_nw"] = float(route_power[f"{component}_delta_nw"])
            for component in ("signal", "clock", "total"):
                row[f"grt_{workload}_{component}_delta_nw"] = float(prediction[f"grt_{workload}_{component}_delta_nw"])
        decision_rows.append(row)

    write_tsv(analysis / "routed_final_holdout_decisions.tsv", decision_rows)
    actual = [bool(row["tier2_routed_actionable_positive"]) for row in decision_rows]
    predicted_a = [bool(row["tier2a_predicted"]) for row in decision_rows]
    predicted_b = [bool(row["tier2b_predicted"]) for row in decision_rows]
    tier1_actual = [bool(row["tier1_routed_positive"]) for row in decision_rows]
    tier1_pred = [bool(row["tier1_predicted"]) for row in decision_rows]
    metric_rows: list[dict[str, object]] = []
    for label, pred, truth in (("tier1_dummy_power", tier1_pred, tier1_actual),
                               ("tier2a_combined_grt", predicted_a, actual),
                               ("tier2b_decoupled_grt", predicted_b, actual)):
        metric_rows.append({"gate": label, **confusion(pred, truth)})

    for workload in WORKLOADS:
        for component in ("signal", "clock", "total"):
            pred_key = f"grt_{workload}_{component}_delta_nw"
            actual_key = f"routed_{workload}_{'switching' if component == 'total' else component}_delta_nw"
            pred_values = [float(row[pred_key]) for row in decision_rows]
            actual_values = [float(row[actual_key]) for row in decision_rows]
            metric_rows.append({
                "gate": f"rank_{workload}_{component}", "n": len(pred_values),
                "spearman_rho": spearman(pred_values, actual_values),
                "kendall_tau_b": kendall_tau_b(pred_values, actual_values),
            })
    write_tsv(analysis / "routed_final_holdout_metrics.tsv", metric_rows)

    tier2a_precision = float(metric_rows[1]["precision"])
    tier2b_precision = float(metric_rows[2]["precision"])
    finite_precisions = [value for value in (tier2a_precision, tier2b_precision) if math.isfinite(value)]
    actionable = max(finite_precisions, default=0.0) >= args.actionable_precision_cutoff
    decision = "RETAIN_TIER2_ON_GCD" if actionable else "RETIRE_PRE_ROUTE_TIER2_ON_GCD"
    report_lines = [
        "# Final GCD fixed-VCD GRT holdout",
        "",
        f"Pre-route hashes verified: {verified_hash_count}.",
        f"Detailed-routed candidates: {len(decision_rows)}/{len(frozen)}.",
        f"Candidates passing the 50 ps setup/hold guard: {sum(row['routed_timing_guard']=='PASS' for row in decision_rows)}/{len(decision_rows)}.",
        f"Candidates passing zero-unannotated-pin VCD coverage: {sum(int(row['zero_unannotated_vcd_pins']) for row in decision_rows)}/{len(decision_rows)}.",
        f"Tier 1 routed precision: {metric_rows[0]['precision']:.3f}; recall: {metric_rows[0]['recall']:.3f}.",
        f"Tier 2A actionable precision: {tier2a_precision:.3f}; recall: {metric_rows[1]['recall']:.3f}.",
        f"Tier 2B actionable precision: {tier2b_precision:.3f}; recall: {metric_rows[2]['recall']:.3f}.",
        f"Decision at {args.actionable_precision_cutoff:.0%} precision threshold: **{decision}**.",
        "",
        "Actionable Tier 2 positives require routed net switching reductions in idle, typical, and peak, "
        "the 50 ps timing guard, and zero unannotated pins. The GRT score and gates were hashed before detailed routing.",
        "",
        "## Tier 2 classification counts",
        "",
        "| Gate | n | TP | FP | FN | TN | Precision | Recall |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in metric_rows[:3]:
        precision = float(row["precision"])
        recall = float(row["recall"])
        report_lines.append(
            f"| {row['gate']} | {row['n']} | {row['tp']} | {row['fp']} | {row['fn']} | {row['tn']} | "
            f"{'NA' if math.isnan(precision) else f'{precision:.3f}'} | {'NA' if math.isnan(recall) else f'{recall:.3f}'} |"
        )
    report_lines.append("")
    report_lines.append("## Rank correlation")
    report_lines.append("")
    report_lines.append("| Workload/component | Spearman ρ | Kendall τ-b |")
    report_lines.append("|---|---:|---:|")
    for row in metric_rows[3:]:
        report_lines.append(f"| {row['gate']} | {float(row['spearman_rho']):.3f} | {float(row['kendall_tau_b']):.3f} |")
    (analysis / "final_grt_holdout_report.md").write_text("\n".join(report_lines) + "\n")
    (analysis / "tier2_go_no_go.txt").write_text(
        f"{decision}\nprecision_cutoff={args.actionable_precision_cutoff:.3f}\n"
        f"tier2a_precision={tier2a_precision:.6f}\ntier2b_precision={tier2b_precision:.6f}\n"
    )
    print(f"GCD_FINAL_GRT_HOLDOUT decision={decision} tier2a_precision={tier2a_precision:.3f} "
          f"tier2b_precision={tier2b_precision:.3f} output={analysis / 'final_grt_holdout_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
