#!/usr/bin/env python3
"""Evaluate a frozen CTS spatial score against final routed clock SPEF labels."""

from __future__ import annotations

import argparse
import csv
import math
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEAF_NET_RE = re.compile(r"^clknet_2_[0-9]+__leaf_clk$")
WORKLOADS = ("idle", "typical", "peak")


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"no rows to write: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[order[k]] = rank
        i = j
    return ranks


def pearson(x: list[float], y: list[float]) -> float | None:
    if len(x) < 2:
        return None
    mean_x = sum(x) / len(x)
    mean_y = sum(y) / len(y)
    dx = [value - mean_x for value in x]
    dy = [value - mean_y for value in y]
    denom = math.sqrt(sum(value * value for value in dx) * sum(value * value for value in dy))
    return sum(a * b for a, b in zip(dx, dy)) / denom if denom else None


def correlations(x: list[float], y: list[float]) -> tuple[float | None, float | None]:
    """Return Spearman rho and tie-adjusted Kendall tau-b."""
    rho = pearson(average_ranks(x), average_ranks(y))
    concordant = discordant = ties_x = ties_y = 0
    for i in range(len(x)):
        for j in range(i + 1, len(x)):
            dx = (x[i] > x[j]) - (x[i] < x[j])
            dy = (y[i] > y[j]) - (y[i] < y[j])
            if dx == 0 and dy != 0:
                ties_x += 1
            elif dy == 0 and dx != 0:
                ties_y += 1
            elif dx and dy:
                if dx == dy:
                    concordant += 1
                else:
                    discordant += 1
    denom = math.sqrt((concordant + discordant + ties_x) * (concordant + discordant + ties_y))
    tau = (concordant - discordant) / denom if denom else None
    return rho, tau


def parse_timing_and_power(summary_path: Path) -> dict[str, float | str]:
    text = summary_path.read_text(errors="replace")
    result: dict[str, float | str] = {
        "route_summary_status": "PASS" if "Candidate routing completed: PASS" in text else "FAIL",
        "timing_guard_status": "FAIL",
    }
    timing_deltas: list[float] = []
    for line in text.splitlines():
        for match in re.finditer(r"Timing slack: setup ([+-]?[0-9.eE+-]+) -> ([+-]?[0-9.eE+-]+) ns; hold ([+-]?[0-9.eE+-]+) -> ([+-]?[0-9.eE+-]+) ns", line):
            setup_base, setup_cand, hold_base, hold_cand = map(float, match.groups())
            timing_deltas.extend((setup_cand - setup_base, hold_cand - hold_base))
        power = re.search(
            r"- (idle|typical|peak): .*?dynamic \(internal \+ net switching\) delta ([+-]?[0-9.eE+-]+) W;",
            line,
        )
        if power:
            result[f"routed_dynamic_delta_nw_{power.group(1)}"] = float(power.group(2)) * 1e9
        switching = re.search(
            r"- (idle|typical|peak): .*?net switching [+-]?[0-9.eE+-]+ -> [+-]?[0-9.eE+-]+ W "
            r"\(delta ([+-]?[0-9.eE+-]+) W\);",
            line,
        )
        if switching:
            result[f"routed_net_switching_delta_nw_{switching.group(1)}"] = float(switching.group(2)) * 1e9
    if timing_deltas:
        result["worst_abs_timing_delta_ps"] = max(abs(delta) for delta in timing_deltas) * 1000.0
        result["timing_guard_status"] = "PASS" if max(abs(delta) for delta in timing_deltas) <= 0.050 else "FAIL"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--route-manifest", type=Path)
    parser.add_argument("--score-dir", type=Path)
    parser.add_argument("--feature-dir", type=Path)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    experiment = args.experiment_dir if args.experiment_dir.is_absolute() else ROOT / args.experiment_dir
    route_manifest = args.route_manifest or experiment / "route_manifest.tsv"
    score_dir = args.score_dir or experiment / "analysis/frozen_stage12"
    feature_dir = args.feature_dir or experiment / "analysis/placement_spatial_features"
    labels_path = args.labels or experiment / "analysis/clock_wire_labels.tsv"
    output_dir = args.output_dir or experiment / "analysis/spatial_holdout_evaluation"
    for path in (route_manifest, score_dir / "frozen_stage12_scores.tsv", feature_dir / "branch_features.tsv", labels_path):
        if not path.is_file():
            raise FileNotFoundError(path)

    frozen_rows = read_tsv(score_dir / "frozen_stage12_scores.tsv")
    scores = {row["candidate_id"]: row for row in frozen_rows}
    branch_rows = read_tsv(feature_dir / "branch_features.tsv")
    proxy: dict[str, float] = {}
    for row in branch_rows:
        candidate = row["candidate_id"]
        if candidate == "baseline":
            continue
        proxy[candidate] = proxy.get(candidate, 0.0) + float(row["typical_vcd_pair_relative_transition_proximity_proxy"])

    labels = read_tsv(labels_path)
    by_candidate: dict[str, dict[str, float]] = {}
    baseline: dict[str, dict[str, float]] = {}
    for row in labels:
        candidate = row["candidate_id"]
        net = row["net"]
        target = baseline if candidate == "baseline" else by_candidate
        target.setdefault(candidate, {})[net] = {
            "wire": float(row["spef_wire_ff"]),
            "ground": float(row["spef_ground_ff"]),
            "coupling": float(row["spef_coupling_ff"]),
        }

    # Use the four CTS leaf nets for the coupling feature target; report root+leaf separately.
    route_rows = read_tsv(route_manifest)
    routes = {row["candidate_id"]: row for row in route_rows}
    candidate_ids = sorted(set(scores) & set(proxy) & set(by_candidate))
    if set(candidate_ids) != set(routes):
        raise ValueError(
            "candidate mismatch among frozen score, spatial features, routed labels, and route manifest: "
            f"scores={len(scores)} proxy={len(proxy)} labels={len(by_candidate)} routes={len(routes)}"
        )
    rows: list[dict[str, object]] = []
    baseline_nets = baseline["baseline"]
    leaves = sorted(net for net in baseline_nets if LEAF_NET_RE.match(net))
    if len(leaves) != 4:
        raise ValueError(f"expected 4 CTS leaf nets in routed labels, got {leaves}")
    all_nets = sorted(baseline_nets)

    for candidate in candidate_ids:
        metrics: dict[str, float] = {}
        for scope_name, nets in (("leaf", leaves), ("all_clock", all_nets)):
            for cap in ("wire", "ground", "coupling"):
                metrics[f"delta_{scope_name}_{cap}_ff"] = sum(
                    by_candidate[candidate][net][cap] - baseline_nets[net][cap] for net in nets
                )
        route = routes[candidate]
        summary_path = Path(route["summary"])
        if not summary_path.is_absolute():
            summary_path = ROOT / summary_path
        route_metrics = parse_timing_and_power(summary_path)
        row: dict[str, object] = {
            "candidate_id": candidate,
            "stage1_dummy_transition_proxy": scores[candidate]["stage1_dummy_transition_proxy"],
            "stage2_typical_pair_proxy": proxy[candidate],
            "routed_return_code": route["return_code"],
            **metrics,
            **route_metrics,
        }
        rows.append(row)

    write_tsv(output_dir / "candidate_holdout_labels.tsv", rows)
    summaries: list[dict[str, object]] = []
    for group_name, selected in (
        ("all_candidates", rows),
        ("stage1_dummy_transition_winners", [row for row in rows if row["stage1_dummy_transition_proxy"] == "1"]),
    ):
        x = [float(row["stage2_typical_pair_proxy"]) for row in selected]
        targets = ["delta_leaf_coupling_ff", "delta_leaf_wire_ff", "delta_all_clock_coupling_ff"]
        if selected and "routed_net_switching_delta_nw_typical" in selected[0]:
            targets.append("routed_net_switching_delta_nw_typical")
        if selected and "routed_dynamic_delta_nw_typical" in selected[0]:
            targets.append("routed_dynamic_delta_nw_typical")
        for target in targets:
            y = [float(row[target]) for row in selected]
            rho, tau = correlations(x, y)
            summaries.append({
                "group": group_name,
                "n": len(selected),
                "stage2_feature": "typical_vcd_pair_relative_transition_proximity_proxy_sum",
                "routed_target": target,
                "spearman_rho": "" if rho is None else rho,
                "kendall_tau_b": "" if tau is None else tau,
                "interpretation": "expected positive correlation: lower Stage 2 score should align with more negative candidate-minus-baseline delta",
            })

    # Preserve the CTS leaf assignment and compare each branch score only with
    # the corresponding routed leaf net, avoiding cancellation in candidate sums.
    branch_eval: list[dict[str, object]] = []
    for feature_row in branch_rows:
        candidate = feature_row["candidate_id"]
        branch = feature_row["branch"]
        if candidate == "baseline" or branch not in leaves:
            continue
        branch_eval.append({
            "candidate_id": candidate,
            "branch": branch,
            "stage1_dummy_transition_proxy": scores[candidate]["stage1_dummy_transition_proxy"],
            "stage2_typical_pair_proxy": float(feature_row["typical_vcd_pair_relative_transition_proximity_proxy"]),
            "delta_leaf_coupling_ff": by_candidate[candidate][branch]["coupling"] - baseline_nets[branch]["coupling"],
            "delta_leaf_wire_ff": by_candidate[candidate][branch]["wire"] - baseline_nets[branch]["wire"],
        })
    branch_groups = [
        ("all_candidate_leaf_branches", branch_eval),
        ("stage1_winner_leaf_branches", [row for row in branch_eval if row["stage1_dummy_transition_proxy"] == "1"]),
    ]
    branch_groups.extend(
        (f"leaf_branch_{net}", [row for row in branch_eval if row["branch"] == net])
        for net in leaves
    )
    for group_name, selected in branch_groups:
        x = [float(row["stage2_typical_pair_proxy"]) for row in selected]
        for target in ("delta_leaf_coupling_ff", "delta_leaf_wire_ff"):
            y = [float(row[target]) for row in selected]
            rho, tau = correlations(x, y)
            summaries.append({
                "group": group_name,
                "n": len(selected),
                "stage2_feature": "per_leaf_typical_vcd_pair_relative_transition_proximity_proxy",
                "routed_target": target,
                "spearman_rho": "" if rho is None else rho,
                "kendall_tau_b": "" if tau is None else tau,
                "interpretation": "expected positive correlation: lower Stage 2 score should align with more negative candidate-minus-baseline delta",
            })
    write_tsv(output_dir / "rank_correlations.tsv", summaries)

    all_rows = rows
    timing_failures = [row["candidate_id"] for row in rows if row["timing_guard_status"] != "PASS"]
    route_failures = [row["candidate_id"] for row in rows if row["route_summary_status"] != "PASS" or row["routed_return_code"] != "0"]
    winner_rows = [row for row in rows if row["stage1_dummy_transition_proxy"] == "1"]
    worst_timing_delta = max(float(row["worst_abs_timing_delta_ps"]) for row in rows)
    report = [
        "# Frozen spatial-score holdout evaluation",
        "",
        f"Candidates with route labels: {len(all_rows)}; Stage 1 dummy-transition proxy winners: {len(winner_rows)}.",
        f"Leaf clock nets evaluated: {', '.join(leaves)}.",
        "The primary target is the summed candidate-minus-baseline SPEF coupling capacitance across the four CTS leaf nets.",
        "For capacitance, positive rank correlation means a larger activity-proximity score tends to accompany a larger (worse) candidate-minus-baseline delta; a negative delta is a reduction. A well-oriented score should therefore have positive correlation with the delta.",
        "The Stage 1 winner subgroup is descriptive only when it contains fewer than five candidates.",
        "",
        f"Timing guard failures (>50 ps): {len(timing_failures)}" + (f" ({', '.join(timing_failures)})" if timing_failures else " (none)"),
        f"Worst absolute setup/hold change across candidates: {worst_timing_delta:.3f} ps.",
        f"Route/summary failures: {len(route_failures)}" + (f" ({', '.join(route_failures)})" if route_failures else " (none)"),
        "",
    ]
    for workload in WORKLOADS:
        key = f"routed_dynamic_delta_nw_{workload}"
        if not rows or key not in rows[0]:
            continue
        best = min(rows, key=lambda row: float(row[key]))
        better = sum(float(row[key]) < 0 for row in rows)
        report.append(
            f"{workload}: {better}/{len(rows)} candidates reduced routed dynamic power; "
            f"best was {best['candidate_id']} at {float(best[key]):+.3f} nW."
        )
    if winner_rows:
        report.extend(["", "Stage 1 winner candidates:"])
        for row in sorted(winner_rows, key=lambda value: str(value["candidate_id"])):
            report.append(
                f"- {row['candidate_id']}: leaf coupling delta {float(row['delta_leaf_coupling_ff']):+.6f} fF; "
                f"typical dynamic-power delta {float(row['routed_dynamic_delta_nw_typical']):+.3f} nW."
            )
    report.extend([
        "",
        "## Score result",
        "",
    ])
    primary = next(row for row in summaries if row["group"] == "all_candidates" and row["routed_target"] == "delta_leaf_coupling_ff")
    report.append(
        f"Stage 2 missed the intended coupling ranking: leaf coupling had Spearman rho "
        f"{float(primary['spearman_rho']):+.3f} and Kendall tau-b {float(primary['kendall_tau_b']):+.3f} "
        "(weak inverse ordering; expected positive)."
    )
    branch_primary = next(row for row in summaries if row["group"] == "all_candidate_leaf_branches" and row["routed_target"] == "delta_leaf_coupling_ff")
    report.append(
        f"Branch-matched leaf analysis (n={branch_primary['n']}) had Spearman rho "
        f"{float(branch_primary['spearman_rho']):+.3f} and Kendall tau-b {float(branch_primary['kendall_tau_b']):+.3f}."
    )
    if len(winner_rows) == 2:
        winner_scores = {float(row["stage2_typical_pair_proxy"]) for row in winner_rows}
        winner_ranks = sorted(int(scores[str(row["candidate_id"])]["stage2_rank_all"]) for row in winner_rows)
        report.append(
            "The two Stage 1 transition winners have "
            + (
                f"identical Stage 2 primary scores and ranked {winner_ranks[0]} and {winner_ranks[1]} of {len(rows)} overall, "
                "so Stage 2 did not distinguish their order and ranked both low."
                if len(winner_scores) == 1
                else "different Stage 2 primary scores; the two-point subgroup is descriptive only."
            )
        )
    report.extend([
        "",
        "See `rank_correlations.tsv` for Spearman rho and Kendall tau-b, and `candidate_holdout_labels.tsv` for per-candidate values.",
    ])
    (output_dir / "spatial_holdout_report.md").write_text("\n".join(report) + "\n")
    print(f"SPATIAL_HOLDOUT_EVALUATION candidates={len(rows)} winners={len(winner_rows)} output={output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
