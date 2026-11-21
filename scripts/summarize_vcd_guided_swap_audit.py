#!/usr/bin/env python3
"""Summarize placement-only VCD-guided swap trials and feature movement."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

WORKLOADS = ("idle", "typical", "peak")


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def read_net_features(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="") as source:
        return {row["net"]: row for row in csv.DictReader(source)}


def read_activity(path: Path) -> dict[str, float]:
    with path.open(newline="") as source:
        return {row["net"]: float(row["density_hz"]) for row in csv.DictReader(source, delimiter="\t")}


def summarize_pool(audit: Path) -> int:
    candidates = read_tsv(audit / "candidate_pool.tsv")
    trials = read_tsv(audit / "audit_pool_typical.tsv")
    if {row["pair_id"] for row in candidates} != {row["pair_id"] for row in trials}:
        raise SystemExit("typical pool trials and candidate pool do not match")
    legal = [row for row in trials if row["status"] in {"evaluated", "timing_regression"}]
    timing_safe = [row for row in trials if row["status"] == "evaluated" and row["timing_ok"] == "1"]
    improvements = [
        row for row in timing_safe if float(row["predicted_delta_w"]) < -1e-9
    ]
    integrity = dict(
        line.split("\t", 1)
        for line in (audit / "integrity.tsv").read_text().splitlines()
        if "\t" in line
    )
    baseline_unchanged = (
        integrity.get("baseline_place_odb_sha256_before")
        == integrity.get("baseline_place_odb_sha256_after")
    )
    best = min(trials, key=lambda row: float(row["predicted_delta_w"] or "inf")) if trials else None
    lines = [
        "VCD-guided location-aware swap search (placement-only pool screen)",
        f"Proposed swaps screened in typical workload: {len(candidates)}.",
        f"Legal swaps: {len(legal)}/{len(candidates)}.",
        f"Timing-safe swaps: {len(timing_safe)}/{len(candidates)} (50 ps slack-loss guard).",
        f"Timing-safe estimated reductions exceeding 1 nW: {len(improvements)}.",
    ]
    if best:
        lines.append(
            f"Best pool candidate: {best['pair_id']} "
            f"({float(best['predicted_delta_w'])*1e9:+.3f} nW, {best['status']})."
        )
    lines.extend([
        "No routed SPEF conclusion is made by this pool-only screen.",
        f"Baseline placement ODB unchanged: {'yes' if baseline_unchanged else 'NO'} "
        f"(SHA-256 {integrity.get('baseline_place_odb_sha256_after', 'missing')}).",
    ])
    summary_path = audit / "summary.md"
    summary_path.write_text("\n".join(lines) + "\n")
    print(summary_path.read_text(), end="")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--pool-only", action="store_true")
    args = parser.parse_args()
    audit = args.audit_dir
    if args.pool_only:
        return summarize_pool(audit)

    by_workload = {
        workload: {row["pair_id"]: row for row in read_tsv(audit / f"audit_{workload}.tsv")}
        for workload in WORKLOADS
    }
    keys = set(by_workload["typical"])
    if any(set(by_workload[workload]) != keys for workload in WORKLOADS):
        raise SystemExit("the workloads did not evaluate the same swap candidates")

    candidates = read_tsv(audit / "candidate_swaps.tsv")
    candidate_by_id = {row["pair_id"]: row for row in candidates}
    if set(candidate_by_id) != keys:
        raise SystemExit("candidate list and workload trial tables do not match")

    baseline = read_net_features(audit / "physical_features/baseline_placement.nets.csv")
    activity = read_activity(audit / "activity/typical.active_net_density.tsv")
    lines = [
        "VCD-guided same-master swap feature audit (placement estimates only)",
        f"Swap proposals: {len(candidates)}; workload trials: {len(candidates) * len(WORKLOADS)}.",
        "Each swap exchanges two same-master cells on the same row; each candidate starts from the shared baseline.",
        "Power deltas are candidate minus baseline; negative means lower placement-estimated switching power.",
        "",
    ]
    delta_errors_nw = [
        abs(float(row["predicted_delta_w"]) - float(row["observed_delta_w"])) * 1e9
        for workload in WORKLOADS
        for row in by_workload[workload].values()
        if row["predicted_delta_w"] and row["observed_delta_w"]
    ]

    fields = [
        "pair", "high/low cell", "VCD activity ratio", "status idle/typical/peak",
        "predicted power delta idle/typical/peak (nW)", "measured placement power delta idle/typical/peak (nW)",
        "HPWL changed nets", "sum abs HPWL delta (um)", "activity-weighted abs HPWL (Hz*um)",
    ]
    lines.append("| " + " | ".join(fields) + " |")
    lines.append("|" + "|".join("---" for _ in fields) + "|")

    for pair_id in sorted(candidate_by_id):
        candidate = candidate_by_id[pair_id]
        workload_rows = [by_workload[workload][pair_id] for workload in WORKLOADS]
        statuses = "/".join(row["status"] for row in workload_rows)
        predicted = "/".join(
            f"{float(row['predicted_delta_w']) * 1e9:+.2f}" if row["predicted_delta_w"] else "n/a"
            for row in workload_rows
        )
        measured = "/".join(
            f"{float(row['observed_delta_w']) * 1e9:+.2f}" if row["observed_delta_w"] else "n/a"
            for row in workload_rows
        )

        candidate_features_path = audit / f"physical_features/{pair_id}_placement.nets.csv"
        changed_nets = 0
        abs_hpwl_um = 0.0
        activity_weighted_hpwl = 0.0
        if candidate_features_path.exists():
            moved = read_net_features(candidate_features_path)
            for net, base_row in baseline.items():
                new_row = moved.get(net)
                if new_row is None:
                    continue
                delta_dbu = int(new_row["hpwl_dbu"]) - int(base_row["hpwl_dbu"])
                if delta_dbu:
                    delta_um = abs(delta_dbu) / float(base_row["dbu_per_micron"])
                    changed_nets += 1
                    abs_hpwl_um += delta_um
                    activity_weighted_hpwl += delta_um * activity.get(net, 0.0)

        activity_ratio = (
            "∞" if candidate["activity_ratio"] == "inf"
            else f"{float(candidate['activity_ratio']):.1f}x"
        )
        lines.append(
            f"| {pair_id} | {candidate['high_instance']} ↔ {candidate['low_instance']} | "
            f"{activity_ratio} | "
            f"{statuses} | {predicted} | {measured} | "
            f"{changed_nets} | {abs_hpwl_um:.3f} | {activity_weighted_hpwl:.3e} |"
        )

    fully_legal = [
        pair_id for pair_id in keys
        if all(by_workload[workload][pair_id]["status"] == "evaluated" for workload in WORKLOADS)
    ]
    timing_safe = [
        pair_id for pair_id in fully_legal
        if all(by_workload[workload][pair_id]["timing_ok"] == "1" for workload in WORKLOADS)
    ]
    route_candidates = read_tsv(audit / "route_candidates.tsv")
    integrity = dict(
        line.split("\t", 1)
        for line in (audit / "integrity.tsv").read_text().splitlines()
        if "\t" in line
    )
    baseline_unchanged = (
        integrity.get("baseline_place_odb_sha256_before")
        == integrity.get("baseline_place_odb_sha256_after")
    )
    candidate_hashes = [key for key in integrity if key.startswith("candidate_")]
    lines.extend([
        "",
        f"Legal and measured in all workloads: {len(fully_legal)}/{len(candidates)}.",
        f"Timing-safe across idle/typical/peak: {len(timing_safe)}/{len(candidates)} (50 ps slack-loss guard).",
        f"Candidates timing-safe and estimated to improve power by at least 1 nW in all workloads: {len(route_candidates)}.",
        f"Placement-score versus OpenSTA switching delta maximum absolute difference: {max(delta_errors_nw, default=0.0):.4f} nW across {len(delta_errors_nw)} workload trials.",
        "Only candidates in route_candidates.tsv should advance to routed SPEF calibration.",
        "No routed-power conclusion is made by this placement-only audit.",
        f"Baseline placement ODB unchanged: {'yes' if baseline_unchanged else 'NO'} (SHA-256 {integrity.get('baseline_place_odb_sha256_after', 'missing')}).",
        f"Saved candidate ODB hashes recorded: {len(candidate_hashes)}.",
    ])
    summary_path = audit / "summary.md"
    summary_path.write_text("\n".join(lines) + "\n")
    print(summary_path.read_text(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
