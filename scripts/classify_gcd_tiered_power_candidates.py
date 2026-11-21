#!/usr/bin/env python3
"""Classify GCD relocation results with CTS and continuous-power tiers."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


WORKLOADS = ("idle", "typical", "peak")


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError("cannot write an empty tiered candidate table")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--dummy-power-bonus-cutoff-nw", type=float, default=-1000.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    experiment = args.experiment_dir.resolve()
    output = args.output or experiment / "analysis/tiered_acceptance.tsv"
    audits = {row["candidate_id"]: row for row in read_tsv(experiment / "relocation_audit.tsv")}
    routed = {
        row["candidate_id"]: row
        for row in read_tsv(experiment / "analysis/candidate_summary.tsv")
    }
    oracle = read_tsv(experiment / "analysis/frozen_stage1/frozen_oracle_scores.tsv")
    oracle_by_candidate_workload = {
        (row["candidate_id"], row["workload"]): row for row in oracle
    }
    inventory_dir = experiment / "analysis/cts_oracle/inventories"

    def dummy_masters(candidate: str) -> dict[str, str]:
        path = inventory_dir / f"{candidate}_cells.tsv"
        if not path.is_file():
            return {}
        return {
            row["instance"]: row["master"]
            for row in read_tsv(path)
            if row["instance"].startswith("clkload")
        }

    baseline_dummies = dummy_masters("baseline")
    output_rows: list[dict[str, object]] = []
    for candidate, audit in sorted(audits.items()):
        hpwl_pass = audit.get("hpwl_guard_status") == "PASS"
        placement_guard_pass = audit.get("status") == "timing_safe" and hpwl_pass
        routed_row = routed.get(candidate)
        routed_timing_pass = routed_row is not None and routed_row.get("timing_guard") == "PASS"
        dummy = dummy_masters(candidate)
        inventory_available = (inventory_dir / f"{candidate}_cells.tsv").is_file()
        changed_dummy_instances = sorted(
            name for name in baseline_dummies.keys() | dummy.keys()
            if inventory_available and baseline_dummies.get(name) != dummy.get(name)
        )
        oracle_typical = oracle_by_candidate_workload.get((candidate, "typical"), {})
        cts_internal_delta = (
            float(oracle_typical["oracle_internal_delta_nw"])
            if oracle_typical.get("oracle_internal_delta_nw") else None
        )
        cts_dynamic_delta = (
            float(oracle_typical["oracle_clock_dynamic_delta_nw"])
            if oracle_typical.get("oracle_clock_dynamic_delta_nw") else None
        )
        deltas = {
            workload: (
                float(routed_row[f"{workload}_switching_delta_nw"])
                if routed_row is not None else None
            )
            for workload in WORKLOADS
        }

        if not placement_guard_pass or (routed_row is not None and not routed_timing_pass):
            classification = "GUARD_REJECT"
            reason = "placement HPWL/timing guard or post-route timing guard failed"
        elif routed_row is None:
            classification = "NOT_ROUTED"
            reason = "placement guards passed, but no routed result is available"
        elif (
            changed_dummy_instances
            and cts_internal_delta is not None
            and cts_internal_delta <= args.dummy_power_bonus_cutoff_nw
        ):
            classification = "TIER1_CTS_DUMMY_POWER_BONUS"
            reason = "dummy master changed and CTS internal-power delta met the frozen bonus cutoff"
        elif all(deltas[workload] is not None and deltas[workload] < 0.0 for workload in WORKLOADS):
            classification = "TIER2_CONTINUOUS_SWITCHING"
            reason = "routed switching power decreased in idle, typical, and peak"
        else:
            classification = "NO_ALL_WORKLOAD_POWER_BENEFIT"
            reason = "no favorable dummy-power event and switching did not decrease in every workload"

        output_rows.append({
            "candidate_id": candidate,
            "instance": audit["instance"],
            "move_um": f"{int(audit['displacement_dbu']) / 1000.0:.3f}",
            "signal_hpwl_delta_pct": audit.get("signal_hpwl_delta_pct", ""),
            "placement_guard": "PASS" if placement_guard_pass else "FAIL",
            "post_route_timing_guard": "PASS" if routed_timing_pass else ("NOT_RUN" if routed_row is None else "FAIL"),
            "dummy_master_changes": ";".join(
                f"{name}:{baseline_dummies.get(name, 'ABSENT')}->{dummy.get(name, 'ABSENT')}"
                for name in changed_dummy_instances
            ),
            "cts_internal_delta_nw_typical": "" if cts_internal_delta is None else f"{cts_internal_delta:.6f}",
            "cts_clock_dynamic_delta_nw_typical": "" if cts_dynamic_delta is None else f"{cts_dynamic_delta:.6f}",
            "idle_switching_delta_nw": "" if deltas["idle"] is None else f"{deltas['idle']:.6f}",
            "typical_switching_delta_nw": "" if deltas["typical"] is None else f"{deltas['typical']:.6f}",
            "peak_switching_delta_nw": "" if deltas["peak"] is None else f"{deltas['peak']:.6f}",
            "classification": classification,
            "reason": reason,
        })

    write_tsv(output, output_rows)
    counts: dict[str, int] = {}
    for row in output_rows:
        classification = str(row["classification"])
        counts[classification] = counts.get(classification, 0) + 1
    print(f"GCD_TIERED_POWER_CLASSIFICATION rows={len(output_rows)} counts={counts} output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
