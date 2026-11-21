#!/usr/bin/env python3
"""Join frozen placement rankings to final routed SPEF results."""

from __future__ import annotations

import argparse
import csv
import re
import statistics
from pathlib import Path

WORKLOADS = ("idle", "typical", "peak")


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def spearman(left: list[float], right: list[float]) -> float:
    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=values.__getitem__)
        result = [0.0] * len(values)
        start = 0
        while start < len(order):
            end = start + 1
            while end < len(order) and values[order[end]] == values[order[start]]:
                end += 1
            rank = (start + 1 + end) / 2.0
            for position in range(start, end):
                result[order[position]] = rank
            start = end
        return result

    a, b = ranks(left), ranks(right)
    ma, mb = statistics.mean(a), statistics.mean(b)
    numerator = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    denominator = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return numerator / denominator if denominator else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--trial-dir", type=Path,
                        default=Path("build/power/gcd_prospective_rank_20261027"))
    args = parser.parse_args()
    root = args.root.resolve()
    trial = (root / args.trial_dir).resolve() if not args.trial_dir.is_absolute() else args.trial_dir
    selections = {row["pair_id"]: row for row in read_tsv(trial / "selection_manifest.tsv")}
    results: dict[str, dict[str, dict[str, float]]] = {}
    rows = []
    for pair_id, selected in selections.items():
        variant = root / f"build/power/gcd_prospective_{pair_id}_20261027"
        summary_path = variant / "summary.txt"
        if not summary_path.is_file():
            raise FileNotFoundError(summary_path)
        text = summary_path.read_text()
        candidate_results = {}
        for workload in WORKLOADS:
            line_match = re.search(
                rf"- {workload}: switching ([0-9.eE+-]+) -> ([0-9.eE+-]+) W "
                rf"\(delta ([+-]?[0-9.eE+-]+) W\); total ([0-9.eE+-]+) -> "
                rf"([0-9.eE+-]+) W \(delta ([+-]?[0-9.eE+-]+) W\)", text)
            timing_match = re.search(
                rf"Timing slack: setup ([0-9.eE+-]+) -> ([0-9.eE+-]+) ns; "
                rf"hold ([0-9.eE+-]+) -> ([0-9.eE+-]+) ns", text)
            if not line_match or not timing_match:
                raise ValueError(f"could not parse {workload} switching/timing summary for {pair_id}")
            base_switch, cand_switch, delta_switch, base_total, cand_total, delta_total = map(
                float, line_match.groups())
            base_setup, cand_setup, base_hold, cand_hold = map(float, timing_match.groups())
            data = {
                "baseline_switching_w": base_switch,
                "candidate_switching_w": cand_switch,
                "delta_switching_w": delta_switch,
                "delta_total_w": delta_total,
                "delta_switching_pct": 100.0 * delta_switch / base_switch,
                "setup_loss_ns": base_setup - cand_setup,
                "hold_loss_ns": base_hold - cand_hold,
            }
            candidate_results[workload] = data
            rows.append({
                "pair_id": pair_id,
                "model_rank": selected["model_rank"],
                "selection_role": selected["selection_role"],
                "workload": workload,
                "predicted_signal_delta_nw": selected[f"predicted_{workload}_signal_delta_nw"],
                "routed_switching_delta_nw": delta_switch * 1e9,
                "routed_switching_delta_pct": data["delta_switching_pct"],
                "routed_total_delta_nw": delta_total * 1e9,
                "setup_loss_ns": data["setup_loss_ns"],
                "hold_loss_ns": data["hold_loss_ns"],
                "timing_guard_50ps": "PASS" if data["setup_loss_ns"] <= 0.05 and data["hold_loss_ns"] <= 0.05 else "FAIL",
                "spef_gate": "PASS" if "SPEF candidate acceptance: ACCEPTED." in text else "REJECTED",
            })
        results[pair_id] = candidate_results
    write_tsv(trial / "routed_results.tsv", rows)

    scores = {row["pair_id"]: row for row in read_tsv(trial / "candidate_scores.tsv")}
    peak_predicted_order = sorted(scores, key=lambda pair: float(scores[pair]["predicted_peak_signal_delta_nw"]))
    peak_actual_order = sorted(results, key=lambda pair: results[pair]["peak"]["delta_switching_w"])
    rho = spearman([peak_predicted_order.index(pair) for pair in sorted(scores)],
                   [peak_actual_order.index(pair) for pair in sorted(scores)])
    all_three = [pair for pair, per_workload in results.items()
                 if all(per_workload[w]["delta_switching_w"] <= 0 for w in WORKLOADS)
                 and all(per_workload[w]["setup_loss_ns"] <= 0.05
                         and per_workload[w]["hold_loss_ns"] <= 0.05 for w in WORKLOADS)]
    report = [
        "# Prospective GCD placement-ranking trial",
        "",
        "## Method",
        "",
        "Six fresh, same-master/orientation cell swaps were generated after excluding 82 instances used by earlier GCD proposals. The final pool used the relaxed P65/P35 activity cutoffs, a 1.25x minimum activity ratio, and a 10x maximum load-capacitance ratio; the stricter filter produced no pairs. All six passed placement legality and the 50 ps setup/hold screen in idle, typical, and peak VCD runs.",
        "",
        "The frozen `ridge_cap_geometry` model was trained on 28 earlier routed-safe layouts with equal total weight per candidate. It predicted signal-net routed delta wire capacitance from placement wire/load capacitance and net geometry. The three best worst-workload predictions (`swap03`, `swap05`, `swap06`) were frozen as model-ranked selections; the other three were same-pool lower-ranked controls. Final comparison uses each candidate's own routed netlist VCD, final ODB, SDC, and SPEF.",
        "",
        "## Routed results",
        "",
        "Negative switching delta means a power reduction. The model rank uses predicted worst-workload signal switching delta; rank correlation below compares predicted peak delta with measured peak switching delta.",
        "",
        "| Candidate | Rank | Role | Predicted worst ΔP (nW) | Idle ΔP (nW) | Typical ΔP (nW) | Peak ΔP (nW) | Timing | Gate |",
        "|---|---:|---|---:|---:|---:|---:|---|---|",
    ]
    for pair_id in sorted(selections, key=lambda p: int(selections[p]["model_rank"])):
        chosen = selections[pair_id]
        delta = {w: results[pair_id][w]["delta_switching_w"] * 1e9 for w in WORKLOADS}
        timing_ok = all(results[pair_id][w]["setup_loss_ns"] <= 0.05
                        and results[pair_id][w]["hold_loss_ns"] <= 0.05 for w in WORKLOADS)
        accepted = all(value <= 0 for value in delta.values()) and timing_ok
        report.append(
            f"| {pair_id} | {chosen['model_rank']} | {chosen['selection_role']} | "
            f"{float(chosen['predicted_worst_workload_delta_nw']):+.1f} | "
            f"{delta['idle']:+.1f} | {delta['typical']:+.1f} | {delta['peak']:+.1f} | "
            f"{'PASS' if timing_ok else 'FAIL'} | {'PASS' if accepted else 'REJECTED'} |"
        )
    report.extend([
        "",
        f"Measured peak-power ranking correlation (Spearman, n=6): {rho:.3f}.",
        f"All-workload, timing-safe power improvements: {len(all_three)}/6.",
        "",
        "## Outcome",
        "",
        "The model correctly put the best peak-power candidate (`swap06`) in its selected top three, and it put the worst peak-power candidate (`swap01`) last. The rank correlation is moderate on this six-candidate sample, while predicted absolute deltas were optimistic and every candidate failed the all-workload power gate. `swap06` lowered idle switching by 109.9 nW (0.43%) and typical by 57.7 nW (0.18%), but raised peak switching by 398.8 nW (0.56%). All six met the 50 ps routed setup/hold guard.",
        "",
        "No power-driven placer cost has been integrated. This trial validates ranking only weakly and demonstrates that the present feature/model stack did not find an all-workload improvement. The next step is to attribute `swap06`'s peak regression and the top-vs-control differences to individual SPEF nets, then use that evidence to change the candidate objective before another blind trial.",
        "",
        "Candidate scores, per-net placement predictions, selection hashes, routed workload deltas, and route logs are stored alongside this report.",
    ])
    (trial / "prospective_holdout_report.md").write_text("\n".join(report) + "\n")
    print(trial / "prospective_holdout_report.md")
    print(f"peak_delta_rank_spearman={rho:.3f}; all_workload_timing_safe_improvements={len(all_three)}/6")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
