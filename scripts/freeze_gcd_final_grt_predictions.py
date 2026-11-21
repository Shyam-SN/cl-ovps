#!/usr/bin/env python3
"""Validate and hash the three pre-route gates before detailed routing."""

from __future__ import annotations

import argparse
import csv
import hashlib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKLOADS = ("idle", "typical", "peak")
RESULTS = ROOT / "build/orfs/results/sky130hd/gcd_power"
BASE = RESULTS / "gcd_baseline20ns"
VCD_ROOT = ROOT / "build/activity/gcd_baseline20ns"


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write an empty TSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--dummy-cutoff-nw", type=float, default=-1000.0)
    args = parser.parse_args()
    experiment = args.experiment_dir.resolve()
    analysis = experiment / "analysis"
    pool = read_tsv(experiment / "candidate_pool.tsv")
    pool_ids = {row["candidate_id"] for row in pool}
    hpwl = {row["candidate_id"]: float(row["signal_hpwl_delta_pct"])
            for row in read_tsv(experiment / "relocation_audit.tsv")}
    tier1_rows = read_tsv(analysis / "frozen_tier1_gate_predictions.tsv")
    tier1 = {row["candidate_id"]: row for row in tier1_rows}
    grt_rows = read_tsv(analysis / "frozen_grt_scores.tsv")
    grt: dict[str, dict[str, dict[str, float]]] = {}
    gate2a: dict[str, bool] = {}
    gate2b: dict[str, bool] = {}
    for row in grt_rows:
        candidate = row["candidate_id"]
        workload = row["workload"]
        grt.setdefault(candidate, {})[workload] = {
            key: float(row[key]) for key in (
                "grt_signal_switching_delta_nw", "grt_clock_switching_delta_nw",
                "grt_total_switching_delta_nw",
            )
        }
        value_a = bool(int(row["tier2a_gate_pass"]))
        value_b = bool(int(row["tier2b_gate_pass"]))
        if candidate in gate2a and gate2a[candidate] != value_a:
            raise ValueError(f"Tier 2A decision differs by workload for {candidate}")
        if candidate in gate2b and gate2b[candidate] != value_b:
            raise ValueError(f"Tier 2B decision differs by workload for {candidate}")
        gate2a[candidate] = value_a
        gate2b[candidate] = value_b
    expected_ids = pool_ids
    if set(tier1) != expected_ids or set(grt) != expected_ids or any(set(grt[cid]) != set(WORKLOADS) for cid in expected_ids):
        raise ValueError("pre-route CTS/GRT scores do not exactly cover the frozen candidate pool")
    if any(float(tier1[cid]["signal_hpwl_delta_pct"]) != hpwl[cid] for cid in pool_ids):
        raise ValueError("Tier 1 score HPWL does not match the frozen placement audit")

    gate_rows: list[dict[str, object]] = []
    for candidate in sorted(pool_ids):
        gate_rows.append({
            "candidate_id": candidate,
            "signal_hpwl_delta_pct": hpwl[candidate],
            "tier1_dummy_internal_delta_nw_typical": tier1[candidate]["typical_dummy_internal_delta_nw"],
            "tier1_dummy_master_changes": tier1[candidate]["dummy_master_changes"],
            "tier1_pass": int(bool(int(tier1[candidate]["tier1_gate_pass"]))
                               and float(tier1[candidate]["typical_dummy_internal_delta_nw"]) <= args.dummy_cutoff_nw
                               and hpwl[candidate] <= 1.5),
            "tier2a_pass": int(gate2a[candidate]),
            "tier2b_pass": int(gate2b[candidate]),
            **{
                f"grt_{workload}_{component}_delta_nw": grt[candidate][workload][f"grt_{component}_switching_delta_nw"]
                for workload in WORKLOADS for component in ("signal", "clock", "total")
            },
        })
    predictions = analysis / "frozen_three_gate_predictions.tsv"
    write_tsv(predictions, gate_rows)

    source_paths: set[Path] = set()
    fixed_paths = [
        experiment / "candidate_pool.tsv", experiment / "frozen_pool_integrity.tsv",
        experiment / "move_overlap_audit.tsv", experiment / "previous_pool_source_manifest.tsv",
        experiment / "raw_candidate_pool.tsv", experiment / "raw_candidate_pool.integrity.tsv",
        experiment / "supplemental_candidate_pool.tsv", experiment / "supplemental_candidate_pool.integrity.tsv",
        experiment / "initial_relocation_audit.tsv", experiment / "supplemental_relocation_audit.tsv",
        experiment / "relocation_audit.tsv", experiment / "pool_generation_attempt2.log",
        experiment / "pool_generation_attempt1.log", experiment / "pool_generation_attempt1_rejections.tsv",
        experiment / "supplemental_generation.log", experiment / "evaluator_initial.log",
        experiment / "evaluator_supplemental.log", analysis / "cts_oracle_manifest.tsv",
        analysis / "cts_oracle/oracle_inventory_manifest.tsv",
        analysis / "frozen_stage1/frozen_oracle_scores.tsv",
        analysis / "frozen_stage1/frozen_score_integrity.tsv",
        analysis / "frozen_tier1_dummy_scores.tsv", analysis / "frozen_tier1_gate_predictions.tsv",
        analysis / "frozen_grt_scores.tsv", analysis / "frozen_grt_activity_coverage.tsv",
        analysis / "frozen_grt_checkpoints.tsv", predictions,
        ROOT / "scripts/make_gcd_cross_row_pool.py", ROOT / "scripts/freeze_gcd_cross_row_pool.py",
        ROOT / "scripts/audit_gcd_cross_row_pool_overlap.py", ROOT / "scripts/evaluate_gcd_flop_relocations.tcl",
        ROOT / "scripts/run_gcd_cts_oracle.py", ROOT / "scripts/export_gcd_cts_oracle_pool.py",
        ROOT / "scripts/export_gcd_cts_oracle_data.tcl", ROOT / "scripts/score_gcd_cts_oracle_pool.py",
        ROOT / "scripts/score_gcd_frozen_tier1_dummy.py", ROOT / "scripts/freeze_gcd_grt_holdout_scores.py",
        ROOT / "scripts/freeze_gcd_final_grt_predictions.py", ROOT / "scripts/gcd_report_placed_cts_power.tcl",
        ROOT / "scripts/gcd_report_global_route_activity_caps.tcl",
        ROOT / "tools/openroad/build/bin/openroad",
        ROOT / "tools/openroad-flow-scripts/flow/Makefile",
        ROOT / "tools/openroad-flow-scripts/flow/platforms/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib",
        BASE / "3_5_place_dp.odb", BASE / "3_place.sdc",
        BASE / "6_final.odb", BASE / "6_final.sdc", BASE / "6_final.spef",
        ROOT / "designs/gcd/flow/constraints/gcd.upf",
    ]
    source_paths.update(path.resolve() for path in fixed_paths if path.is_file())
    source_paths.update(path.resolve() for path in sorted((experiment / "candidate_odb").glob("*.odb")))
    for row in read_tsv(analysis / "cts_oracle_manifest.tsv"):
        for key in ("cts_odb", "cts_sdc"):
            path = Path(row[key])
            if path.is_file():
                source_paths.add(path.resolve())
    for row in read_tsv(analysis / "frozen_grt_checkpoints.tsv"):
        for key in ("grt_odb", "grt_sdc", "report"):
            path = Path(row[key])
            if path.is_file():
                source_paths.add(path.resolve())
    source_paths.update(path.resolve() for path in (VCD_ROOT / "routed").glob("*.vcd"))
    source_paths.update(path.resolve() for path in (VCD_ROOT / "placement").glob("*.vcd"))
    source_paths.update(path.resolve() for path in sorted((experiment / "analysis/cts_oracle/inventories").glob("*.tsv")))
    source_paths.update(path.resolve() for path in sorted((experiment / "analysis/grt_fixed_vcd").glob("*.tsv")))
    source_paths.update(path.resolve() for path in sorted((analysis / "frozen_stage1/power_logs").rglob("*.log")))
    source_paths.update(path.resolve() for path in sorted((analysis / "grt_fixed_vcd").glob("*.log")))
    integrity_rows = [
        {"artifact": str(path.relative_to(ROOT)) if ROOT in path.parents else str(path),
         "path": str(path), "sha256": sha256(path), "size_bytes": path.stat().st_size}
        for path in sorted(source_paths)
    ]
    integrity = analysis / "frozen_prediction_integrity.tsv"
    write_tsv(integrity, integrity_rows)
    protocol = analysis / "frozen_gate_protocol.md"
    protocol.write_text(
        "# Frozen GCD holdout gates\n\n"
        f"Freeze time (UTC): {datetime.now(timezone.utc).isoformat()}\n\n"
        f"Frozen candidate count: {len(pool_ids)}. All candidates are isolated single-flop moves "
        "from the fixed baseline placement, cover all 35 CTS sinks and all four baseline CTS leaves, "
        "and have signal HPWL change no greater than +1.5%.\n\n"
        f"- Tier 1: CTS-oracle dummy-cell internal power delta in typical workload <= {args.dummy_cutoff_nw:.0f} nW, "
        "a dummy-master change is present, and signal HPWL <= +1.5%.\n"
        "- Tier 2A: fixed baseline VCD, post-CTS global-route total switching delta <= 0 nW in idle, typical, "
        "and peak, with signal HPWL <= +0.5%.\n"
        "- Tier 2B: fixed baseline VCD, post-CTS global-route signal and clock switching deltas each <= 0 nW "
        "in idle, typical, and peak, with signal HPWL <= +0.5%.\n\n"
        "All prediction and source hashes are in `frozen_prediction_integrity.tsv`. Detailed-route labels "
        "must not be opened or used to revise these gates. The 50 ps setup/hold and zero-unannotated-pin "
        "VCD checks are evaluated after routing; Tier 2 actual positives require routed net switching "
        "reductions in all three workloads and both checks to pass.\n"
    )
    # Include the protocol and integrity table in a separate marker after writing them.
    marker = analysis / "frozen_before_detail_route.tsv"
    write_tsv(marker, [
        {"metric": "frozen_before_detail_route", "value": "YES"},
        {"metric": "candidate_count", "value": len(pool_ids)},
        {"metric": "candidate_pool_sha256", "value": sha256(experiment / "candidate_pool.tsv")},
        {"metric": "three_gate_predictions_sha256", "value": sha256(predictions)},
        {"metric": "prediction_integrity_sha256", "value": sha256(integrity)},
        {"metric": "gate_protocol_sha256", "value": sha256(protocol)},
        {"metric": "freeze_time_utc", "value": datetime.now(timezone.utc).isoformat()},
    ])
    print(f"FROZEN_THREE_GATES candidates={len(gate_rows)} tier1={sum(int(r['tier1_pass']) for r in gate_rows)} "
          f"tier2a={sum(int(r['tier2a_pass']) for r in gate_rows)} tier2b={sum(int(r['tier2b_pass']) for r in gate_rows)} "
          f"integrity_files={len(integrity_rows)} marker={marker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
