#!/usr/bin/env python3
"""Compare preregistered CTS-oracle scores with routed holdout power."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from analyze_gcd_cts_branch_proxy import kendall_tau_b, read_tsv, spearman, write_tsv


ROOT = Path(__file__).resolve().parents[1]
WORKLOADS = ("idle", "typical", "peak")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--frozen-scores", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    experiment_dir = args.experiment_dir.resolve()
    frozen_scores_path = args.frozen_scores.resolve()
    output_dir = args.output_dir.resolve() if args.output_dir else experiment_dir / "analysis/holdout_comparison"
    output_dir.mkdir(parents=True, exist_ok=True)
    score_rows = read_tsv(frozen_scores_path)
    routed_rows = read_tsv(experiment_dir / "analysis/candidate_metrics.tsv")
    routed = {row["candidate_id"]: row for row in routed_rows}
    predicted = {
        (row["candidate_id"], row["workload"]): row
        for row in score_rows
        if row["candidate_id"] != "baseline"
    }
    candidate_ids = sorted(routed)
    if set(candidate_ids) != {cid for cid, _ in predicted}:
        raise ValueError("frozen score candidate set does not match routed candidate set")

    timing_failures = [
        candidate_id for candidate_id, row in routed.items()
        if row.get("routed_timing_guard") != "PASS"
    ]
    if timing_failures:
        raise ValueError(f"holdout candidates failed routed timing guard: {timing_failures}")

    rank_rows: list[dict[str, object]] = []
    topk_rows: list[dict[str, object]] = []
    for workload in WORKLOADS:
        workload_scores = [predicted[(cid, workload)] for cid in candidate_ids]
        predictors = {
            "oracle_clock_dynamic_delta_nw": [float(row["oracle_clock_dynamic_delta_nw"]) for row in workload_scores],
            "oracle_internal_delta_nw": [float(row["oracle_internal_delta_nw"]) for row in workload_scores],
            "oracle_switching_delta_nw": [float(row["oracle_switching_delta_nw"]) for row in workload_scores],
        }
        targets = {
            "routed_clock_internal_delta_nw": [float(routed[cid][f"routed_{workload}_clock_internal_delta_nw"]) for cid in candidate_ids],
            "routed_clock_switching_delta_nw": [float(routed[cid][f"routed_{workload}_clock_switching_delta_nw"]) for cid in candidate_ids],
            "routed_clock_dynamic_delta_nw": [float(routed[cid][f"routed_{workload}_clock_total_delta_nw"]) for cid in candidate_ids],
            "routed_total_dynamic_delta_nw": [float(routed[cid][f"routed_{workload}_dynamic_delta_nw"]) for cid in candidate_ids],
        }
        for predictor, x in predictors.items():
            for target, y in targets.items():
                rank_rows.append({
                    "workload": workload,
                    "predictor": predictor,
                    "target": target,
                    "candidate_count": len(candidate_ids),
                    "spearman_rho": spearman(x, y),
                    "kendall_tau_b": kendall_tau_b(x, y),
                })

        predicted_order = sorted(candidate_ids, key=lambda cid: float(predicted[(cid, workload)]["oracle_clock_dynamic_delta_nw"]))
        for target_suffix in ("clock_total_delta_nw", "dynamic_delta_nw"):
            actual_order = sorted(candidate_ids, key=lambda cid: float(routed[cid][f"routed_{workload}_{target_suffix}"]))
            for k in (1, 3, 5, 10):
                predicted_top = set(predicted_order[:k])
                actual_top = set(actual_order[:k])
                topk_rows.append({
                    "workload": workload,
                    "routed_target": f"routed_{target_suffix}",
                    "k": k,
                    "oracle_top_candidates": ",".join(predicted_order[:k]),
                    "routed_top_candidates": ",".join(actual_order[:k]),
                    "overlap_count": len(predicted_top & actual_top),
                    "overlap_fraction": len(predicted_top & actual_top) / k,
                })

    write_tsv(output_dir / "rank_correlations.tsv", rank_rows)
    write_tsv(output_dir / "topk_overlap.tsv", topk_rows)

    integrity_path = frozen_scores_path.parent / "frozen_score_integrity.tsv"
    integrity_rows = read_tsv(integrity_path)
    integrity_checks = []
    for row in integrity_rows:
        artifact_path = Path(row["path"])
        actual = sha256(artifact_path)
        integrity_checks.append({
            "candidate_id": row["candidate_id"],
            "artifact": row["artifact"],
            "path": row["path"],
            "expected_sha256": row["sha256"],
            "actual_sha256": actual,
            "match": int(actual == row["sha256"]),
        })
    write_tsv(output_dir / "frozen_input_integrity.tsv", integrity_checks)
    if any(not int(row["match"]) for row in integrity_checks):
        raise ValueError("one or more frozen oracle inputs changed after scoring")

    summary = [
        "# Frozen CTS-oracle holdout comparison",
        "",
        f"Candidates: {len(candidate_ids)}; routed timing guard passed: {len(candidate_ids)}/{len(candidate_ids)}.",
        f"All frozen score input hashes match: {len(integrity_checks)}/{len(integrity_checks)}.",
        "The oracle score was generated and saved before routing; this comparison did not refit or retune it.",
        "",
        "## Clock dynamic power rank correlation",
        "",
    ]
    for row in rank_rows:
        if row["predictor"] == "oracle_clock_dynamic_delta_nw" and row["target"] == "routed_clock_dynamic_delta_nw":
            summary.append(
                f"- {row['workload']}: Spearman ρ={float(row['spearman_rho']):.3f}, "
                f"Kendall τb={float(row['kendall_tau_b']):.3f}."
            )
    summary.extend(["", "## Top-five overlap", ""])
    for row in topk_rows:
        if row["k"] == 5 and row["routed_target"] == "routed_clock_total_delta_nw":
            summary.append(
                f"- {row['workload']}: {row['overlap_count']}/5 clock-power candidates overlap."
            )
    summary.extend([
        "",
        "The diagnostic pool showed exact CTS topology reproduction but weak small-delta power ranking. These frozen holdout results determine whether the oracle generalizes; interpret clock internal power separately from clock-wire switching.",
        "",
        "See rank_correlations.tsv, topk_overlap.tsv, and frozen_input_integrity.tsv for the full comparison.",
    ])
    (output_dir / "summary.md").write_text("\n".join(summary) + "\n")
    print(f"CTS_ORACLE_HOLDOUT candidates={len(candidate_ids)} summary={output_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
