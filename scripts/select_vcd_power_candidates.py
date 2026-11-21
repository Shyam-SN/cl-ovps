#!/usr/bin/env python3
"""Select placement-power-improving VCD-guided swaps from OpenROAD audit TSVs."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

WORKLOADS = ("idle", "typical", "peak")


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=fields, delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--typical-audit", type=Path)
    parser.add_argument("--all-workloads-audit-dir", type=Path)
    parser.add_argument("--max-candidates", type=int, default=24)
    parser.add_argument("--minimum-improvement-nw", type=float, default=1.0)
    args = parser.parse_args()
    if (args.typical_audit is None) == (args.all_workloads_audit_dir is None):
        parser.error("provide exactly one of --typical-audit or --all-workloads-audit-dir")
    if args.max_candidates < 1 or args.minimum_improvement_nw < 0:
        parser.error("candidate limit must be positive and improvement threshold nonnegative")

    candidates = read_tsv(args.candidates)
    fields = list(candidates[0]) if candidates else [
        "pair_id", "high_instance", "low_instance", "master", "row_y_dbu", "orient",
        "high_activity_hz", "low_activity_hz", "activity_ratio",
        "high_switching_power_w", "low_switching_power_w",
        "high_load_cap_f", "low_load_cap_f", "load_cap_ratio",
    ]
    by_id = {row["pair_id"]: row for row in candidates}
    threshold_w = args.minimum_improvement_nw * 1e-9
    eligible: list[tuple[float, dict[str, str]]] = []

    if args.typical_audit:
        audit_rows = read_tsv(args.typical_audit)
        audit = {row["pair_id"]: row for row in audit_rows}
        if set(audit) != set(by_id):
            raise SystemExit("typical pool audit and candidate list do not match")
        legal = sum(row["status"] in {"evaluated", "timing_regression"} for row in audit.values())
        timing_safe = sum(row["status"] == "evaluated" and row["timing_ok"] == "1" for row in audit.values())
        improving = []
        for pair_id, row in audit.items():
            delta = float(row["predicted_delta_w"] or "inf")
            if row["status"] == "evaluated" and row["timing_ok"] == "1" and delta < -threshold_w:
                improving.append((delta, by_id[pair_id]))
        eligible = sorted(improving, key=lambda item: item[0])[: args.max_candidates]
        summary_lines = [
            f"Candidate proposals evaluated in typical workload: {len(candidates)}.",
            f"Legal placement swaps: {legal}/{len(candidates)}.",
            f"Timing-safe swaps: {timing_safe}/{len(candidates)}.",
            f"Typical estimated improvements exceeding {args.minimum_improvement_nw:g} nW: {len(improving)}.",
            f"Shortlisted for idle/peak verification: {len(eligible)} (limit {args.max_candidates}).",
        ]
        if audit:
            best_id, best_row = min(audit.items(), key=lambda item: float(item[1]["predicted_delta_w"] or "inf"))
            summary_lines.append(
                f"Best typical delta in the full pool: {best_id} "
                f"({float(best_row['predicted_delta_w'])*1e9:+.3f} nW, {best_row['status']})."
            )
    else:
        workload_audits = {
            workload: {row["pair_id"]: row for row in read_tsv(
                args.all_workloads_audit_dir / f"audit_{workload}.tsv"
            )}
            for workload in WORKLOADS
        }
        for workload, audit in workload_audits.items():
            if set(audit) != set(by_id):
                raise SystemExit(f"{workload} audit and candidate list do not match")
        for pair_id, candidate in by_id.items():
            rows = [workload_audits[workload][pair_id] for workload in WORKLOADS]
            if all(
                row["status"] == "evaluated"
                and row["timing_ok"] == "1"
                and float(row["predicted_delta_w"] or "inf") < -threshold_w
                for row in rows
            ):
                score = max(float(row["predicted_delta_w"]) for row in rows)
                eligible.append((score, candidate))
        eligible.sort(key=lambda item: item[0])
        summary_lines = [
            f"Shortlisted candidates verified in all three workloads: {len(candidates)}.",
            f"Timing-safe and estimated to improve power by more than {args.minimum_improvement_nw:g} nW in idle, typical, and peak: {len(eligible)}.",
            "Only these candidates should advance to routed SPEF evaluation.",
        ]

    selected = [row for _, row in eligible]
    write_tsv(args.output, selected, fields)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text("\n".join(summary_lines) + "\n")
    print("\n".join(summary_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
