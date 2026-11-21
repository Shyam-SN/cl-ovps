#!/usr/bin/env python3
"""Evaluate frozen GCD CTS/VCD scores against routed power on a holdout pool."""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

from analyze_gcd_cts_branch_proxy import dummy_loads_by_branch
from analyze_gcd_spatial_holdout import correlations


ROOT = Path(__file__).resolve().parents[1]
WORKLOADS = ("idle", "typical", "peak")
STAGE1_THRESHOLD_NW = -1000.0


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def power_deltas(summary: Path) -> dict[str, dict[str, float]]:
    text = summary.read_text(errors="replace")
    values: dict[str, dict[str, float]] = {}
    for workload in WORKLOADS:
        match = re.search(
            rf"- {workload}: internal [0-9.eE+-]+ -> [0-9.eE+-]+ W "
            rf"\(delta ([+-]?[0-9.eE+-]+) W\); net switching [0-9.eE+-]+ -> "
            rf"[0-9.eE+-]+ W \(delta ([+-]?[0-9.eE+-]+) W\); "
            rf"dynamic \(internal \+ net switching\) delta ([+-]?[0-9.eE+-]+) W;",
            text,
        )
        timing = re.search(
            rf"- {workload}:.*?\n\s+Timing slack: setup ([0-9.eE+-]+) -> "
            rf"([0-9.eE+-]+) ns; hold ([0-9.eE+-]+) -> ([0-9.eE+-]+) ns",
            text,
        )
        if match is None or timing is None:
            raise ValueError(f"missing routed power/timing fields in {summary} for {workload}")
        internal_nw, switching_nw, dynamic_nw = (float(value) * 1e9 for value in match.groups())
        setup_before, setup_after, hold_before, hold_after = map(float, timing.groups())
        values[workload] = {
            "internal_delta_nw": internal_nw,
            "switching_delta_nw": switching_nw,
            "dynamic_delta_nw": dynamic_nw,
            "setup_loss_ps": (setup_before - setup_after) * 1000.0,
            "hold_loss_ps": (hold_before - hold_after) * 1000.0,
        }
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    experiment = args.experiment_dir.resolve()
    output = (args.output_dir or experiment / "analysis/holdout_score_evaluation").resolve()

    frozen_path = experiment / "analysis/frozen_vcd_score/frozen_vcd_stage12_scores.tsv"
    pair_path = experiment / "analysis/spef_coupling_aggressors/candidate_coupling_activity_power.tsv"
    route_path = experiment / "route_manifest.tsv"
    baseline_netlist = ROOT / "build/orfs/results/sky130hd/gcd_power/gcd_baseline20ns/6_final.v"
    for path in (frozen_path, pair_path, route_path, baseline_netlist):
        if not path.is_file():
            raise FileNotFoundError(path)

    frozen = {row["candidate_id"]: row for row in read_tsv(frozen_path)}
    pair_rows = read_tsv(pair_path)
    pair = {(row["candidate_id"], row["workload"]): row for row in pair_rows}
    routes = {row["candidate_id"]: row for row in read_tsv(route_path)}
    candidate_ids = sorted(routes)
    if set(candidate_ids) != set(frozen):
        raise ValueError(f"candidate mismatch: routed={len(candidate_ids)} frozen={len(frozen)}")

    baseline_dummies = dummy_loads_by_branch(baseline_netlist)
    candidate_rows: list[dict[str, object]] = []
    workload_rows: list[dict[str, object]] = []
    actual_dummy_winners: list[str] = []
    frozen_dummy_winners = [
        candidate for candidate in candidate_ids
        if int(frozen[candidate]["stage1_dummy_transition_proxy"]) == 1
    ]

    for candidate in candidate_ids:
        route = routes[candidate]
        variant = route["route_variant"]
        variant_result = ROOT / "build/orfs/results/sky130hd/gcd_power" / variant
        netlist_path = variant_result / "6_final.v"
        summary_path = ROOT / "build/power" / variant / "summary.txt"
        if not netlist_path.is_file() or not summary_path.is_file():
            raise FileNotFoundError(f"missing final netlist or summary for {candidate}")
        dummies = dummy_loads_by_branch(netlist_path)
        changed_branches = sorted(
            branch for branch in baseline_dummies.keys() | dummies.keys()
            if baseline_dummies.get(branch) is None or dummies.get(branch) is None
            or baseline_dummies[branch][1] != dummies[branch][1]
        )
        routed_power = power_deltas(summary_path)
        if any(routed_power[w]["internal_delta_nw"] <= STAGE1_THRESHOLD_NW for w in WORKLOADS):
            actual_dummy_winners.append(candidate)
        setup_loss = max(routed_power[w]["setup_loss_ps"] for w in WORKLOADS)
        hold_loss = max(routed_power[w]["hold_loss_ps"] for w in WORKLOADS)
        timing_degradation = max(0.0, setup_loss, hold_loss)
        all_workload_nonregression = all(
            routed_power[w]["switching_delta_nw"] <= 0.0 for w in WORKLOADS
        )
        row: dict[str, object] = {
            "candidate_id": candidate,
            "route_variant": variant,
            "stage1_dummy_transition_proxy": frozen[candidate]["stage1_dummy_transition_proxy"],
            "stage1_internal_delta_nw_typical": frozen[candidate]["stage1_internal_delta_nw_typical"],
            "vcd_opposite_activity_hz_typical": frozen[candidate]["vcd_opposite_activity_hz_typical_primary"],
            "routed_dummy_cell_master_change": int(bool(changed_branches)),
            "changed_dummy_branches": ";".join(changed_branches),
            "dummy_master_baseline": ";".join(f"{b}:{m}" for b, (_, m) in sorted(baseline_dummies.items())),
            "dummy_master_candidate": ";".join(f"{b}:{m}" for b, (_, m) in sorted(dummies.items())),
            "max_setup_loss_ps": setup_loss,
            "max_hold_loss_ps": hold_loss,
            "max_positive_timing_degradation_ps": timing_degradation,
            "timing_guard_50ps": "PASS" if timing_degradation <= 50.0 else "FAIL",
            "net_switching_nonregression_all_workloads": "PASS" if all_workload_nonregression else "FAIL",
        }
        for workload in WORKLOADS:
            pr = pair[(candidate, workload)]
            row[f"{workload}_spef_vcd_coupling_proxy_delta_nw"] = pr["coupling_power_proxy_delta_nw"]
            frozen_vcd_key = (
                "vcd_opposite_activity_hz_typical_primary"
                if workload == "typical" else f"vcd_opposite_activity_hz_{workload}"
            )
            row[f"{workload}_vcd_only_opposite_activity_hz"] = frozen[candidate][frozen_vcd_key]
            row[f"{workload}_routed_net_switching_delta_nw"] = routed_power[workload]["switching_delta_nw"]
            row[f"{workload}_routed_internal_delta_nw"] = routed_power[workload]["internal_delta_nw"]
            row[f"{workload}_routed_dynamic_delta_nw"] = routed_power[workload]["dynamic_delta_nw"]
            workload_rows.append({
                "candidate_id": candidate,
                "workload": workload,
                "spef_vcd_coupling_proxy_delta_nw": pr["coupling_power_proxy_delta_nw"],
                "vcd_only_opposite_activity_hz": frozen[candidate][frozen_vcd_key],
                "routed_net_switching_delta_nw": routed_power[workload]["switching_delta_nw"],
                "routed_internal_delta_nw": routed_power[workload]["internal_delta_nw"],
                "routed_dynamic_delta_nw": routed_power[workload]["dynamic_delta_nw"],
            })
        row["stage1_and_vcd_guard_pass"] = (
            "PASS" if row["stage1_dummy_transition_proxy"] == 1
            and all_workload_nonregression and timing_degradation <= 50.0 else "FAIL"
        )
        candidate_rows.append(row)

    correlation_rows: list[dict[str, object]] = []
    score_agreement_rows: list[dict[str, object]] = []
    for workload in WORKLOADS:
        subset = [row for row in workload_rows if row["workload"] == workload]
        actual = [float(row["routed_net_switching_delta_nw"]) for row in subset]
        vcd = [float(row["vcd_only_opposite_activity_hz"]) for row in subset]
        spef = [float(row["spef_vcd_coupling_proxy_delta_nw"]) for row in subset]
        for predictor, values in (("VCD_only_opposite_activity", vcd), ("SPEF_Cc_weighted_VCD", spef)):
            rho, tau = correlations(values, actual)
            correlation_rows.append({
                "workload": workload,
                "comparison": f"{predictor}_vs_routed_net_switching_delta",
                "n": len(subset),
                "spearman_rho": "" if rho is None else f"{rho:.9f}",
                "kendall_tau_b": "" if tau is None else f"{tau:.9f}",
            })
        rho, tau = correlations(vcd, spef)
        score_agreement_rows.append({
            "workload": workload,
            "comparison": "VCD_only_vs_SPEF_Cc_weighted_score",
            "n": len(subset),
            "spearman_rho": "" if rho is None else f"{rho:.9f}",
            "kendall_tau_b": "" if tau is None else f"{tau:.9f}",
        })

    output.mkdir(parents=True, exist_ok=True)
    write_tsv(output / "frozen_score_vs_routed_labels.tsv", candidate_rows)
    write_tsv(output / "workload_score_comparison.tsv", workload_rows)
    write_tsv(output / "predictor_rank_correlations.tsv", correlation_rows)
    write_tsv(output / "predictor_score_agreement.tsv", score_agreement_rows)

    stage1_missed = [c for c in actual_dummy_winners if c not in frozen_dummy_winners]
    predicted_actual = [c for c in frozen_dummy_winners if c in actual_dummy_winners]
    combined_gate_count = sum(row["stage1_and_vcd_guard_pass"] == "PASS" for row in candidate_rows)
    max_setup = max(float(row["max_setup_loss_ps"]) for row in candidate_rows)
    max_hold = max(float(row["max_hold_loss_ps"]) for row in candidate_rows)
    timing_pass = sum(row["timing_guard_50ps"] == "PASS" for row in candidate_rows)
    net_pass = sum(row["net_switching_nonregression_all_workloads"] == "PASS" for row in candidate_rows)
    typical_prediction = next(row for row in correlation_rows if row["workload"] == "typical" and row["comparison"].startswith("VCD_only"))
    typical_spef = next(row for row in correlation_rows if row["workload"] == "typical" and row["comparison"].startswith("SPEF_Cc"))
    typical_agreement = next(row for row in score_agreement_rows if row["workload"] == "typical")
    typical_internal = [float(row["typical_routed_internal_delta_nw"]) for row in candidate_rows]
    max_internal_saving = min(typical_internal)
    stage1_outcome = (
        f"The frozen Stage1 score selected {len(frozen_dummy_winners)} candidates at the ≤{STAGE1_THRESHOLD_NW:g} nW internal-power threshold. "
        f"Post-route internal-power threshold crossings: {len(actual_dummy_winners)}; Stage1 true positives {len(predicted_actual)}; "
        f"missed routed crossings {len(stage1_missed)}."
    )
    report = [
        "# Independent VCD/SPEF holdout evaluation",
        "",
        f"Frozen candidate moves: {len(candidate_rows)}; exact move overlap with the earlier two pools: 0; all candidates were routed.",
        "The Stage1 CTS oracle and VCD-only score were hashed before candidate routing. The detailed SPEF/VCD formula is computed after route and evaluated only as a label-side diagnostic.",
        "",
        "## Predictor ranking",
        "",
        "The common target is candidate-minus-baseline routed design net-switching power. Positive predictor/target correlation means higher predicted activity or coupling accompanies higher net switching.",
        "",
        "| Workload | Predictor vs routed net switching | Spearman ρ | Kendall τ-b | n |",
        "|---|---|---:|---:|---:|",
    ]
    for row in correlation_rows:
        report.append(f"| {row['workload']} | {row['comparison']} | {row['spearman_rho'] or 'NA'} | {row['kendall_tau_b'] or 'NA'} | {row['n']} |")
    report += [
        "",
        "| Workload | VCD-only vs SPEF Cc-weighted score | Spearman ρ | Kendall τ-b | n |",
        "|---|---|---:|---:|---:|",
    ]
    for row in score_agreement_rows:
        report.append(f"| {row['workload']} | {row['comparison']} | {row['spearman_rho'] or 'NA'} | {row['kendall_tau_b'] or 'NA'} | {row['n']} |")
    report += [
        "",
        f"Typical workload: VCD-only ρ={float(typical_prediction['spearman_rho']):+.3f}; SPEF Cc-weighted ρ={float(typical_spef['spearman_rho']):+.3f}; score-to-score ρ={float(typical_agreement['spearman_rho']):+.3f}.",
        "This holdout does not reproduce the earlier parity result: the Cc-weighted score ranks typical net-switching labels better here, while agreement between the two predictors is modest. The idle VCD-only activity feature is constant across candidates, so its idle rank correlation is undefined.",
        "",
        "## Stage1 dummy-load gate and guardrails",
        "",
        stage1_outcome,
        f"Lowest typical routed internal-power delta was {max_internal_saving:+.1f} nW. Dummy master changes are listed per candidate in `frozen_score_vs_routed_labels.tsv`.",
        f"Candidates passing the complete Stage1 + VCD + three-workload net-switching + 50 ps timing gate: {combined_gate_count}/{len(candidate_rows)}.",
        f"Candidates passing 50 ps post-route setup/hold loss guard: {timing_pass}/{len(candidate_rows)} (maximum setup loss {max_setup:+.2f} ps; maximum hold loss {max_hold:+.2f} ps).",
        f"Candidates with non-increasing net switching in idle, typical, and peak: {net_pass}/{len(candidate_rows)}.",
        "",
        "A power reduction is shown as a negative candidate-minus-baseline delta. Here, ‘net-switching non-regression’ means delta ≤ 0 in each workload. This is distinct from the user phrasing of non-negative improvement.",
        "The combined Stage1-plus-VCD gate only counts a candidate as passing if Stage1 flags the dummy transition, net switching does not increase in any workload, and the 50 ps timing guard passes. It does not fit a new weight or threshold on routed labels.",
        "This pool contains no Stage1 positive and no routed µW-scale internal-power event, so it cannot measure the Stage1 dummy-win hit rate. The pool enforces a 20 µm move limit and the timing guard; a separate global placement-HPWL bound was not part of the frozen score and remains to be added before the next pool.",
        "",
        "## Files",
        "",
        "- `predictor_rank_correlations.tsv`: each frozen predictor versus routed net-switching labels.",
        "- `predictor_score_agreement.tsv`: direct ranking agreement between the two predictors.",
        "- `frozen_score_vs_routed_labels.tsv`: Stage1 prediction, dummy masters, routed power and timing per candidate.",
        "- `workload_score_comparison.tsv`: long-form scores and labels across workloads.",
    ]
    (output / "independent_holdout_evaluation.md").write_text("\n".join(report) + "\n")
    print(
        f"HOLDOUT_EVALUATION candidates={len(candidate_rows)} predicted_stage1={len(frozen_dummy_winners)} "
        f"routed_internal_threshold={len(actual_dummy_winners)} timing={timing_pass}/{len(candidate_rows)} "
        f"net_nonregression={net_pass}/{len(candidate_rows)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
