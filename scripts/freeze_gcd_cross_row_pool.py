#!/usr/bin/env python3
"""Freeze the legal, <=1.5% HPWL portion of a cross-row GCD holdout pool."""

from __future__ import annotations

import argparse
import csv
import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty TSV {path}")
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
    parser.add_argument("--proposal-pool", type=Path, action="append", required=True)
    parser.add_argument("--audit", type=Path, action="append", required=True)
    parser.add_argument("--hpwl-limit-pct", type=float, default=1.5)
    args = parser.parse_args()
    experiment = args.experiment_dir.resolve()
    proposal_paths = [path.resolve() for path in args.proposal_pool]
    audit_paths = [path.resolve() for path in args.audit]
    proposals = {row["candidate_id"]: row for path in proposal_paths for row in read_tsv(path)}
    audits = {row["candidate_id"]: row for path in audit_paths for row in read_tsv(path)}
    if len(proposals) != sum(len(read_tsv(path)) for path in proposal_paths):
        raise SystemExit("candidate IDs are duplicated across proposal pools")
    if len(audits) != sum(len(read_tsv(path)) for path in audit_paths):
        raise SystemExit("candidate IDs are duplicated across placement audits")
    if set(proposals) != set(audits):
        raise SystemExit(f"proposal/audit ID mismatch: missing_audits={sorted(set(proposals)-set(audits))[:8]} missing_pools={sorted(set(audits)-set(proposals))[:8]}")

    candidate_rows: list[dict[str, str]] = []
    filter_rows: list[dict[str, object]] = []
    candidate_odb_dir = experiment / "candidate_odb"
    for candidate_id, proposal in proposals.items():
        audit = audits[candidate_id]
        odb = candidate_odb_dir / f"{candidate_id}.odb"
        hpwl = float(audit["signal_hpwl_delta_pct"])
        hpwl_pass = hpwl <= args.hpwl_limit_pct + 1e-9
        route_ready = bool(audit.get("odb")) and odb.is_file()
        accepted = hpwl_pass and route_ready
        reason = "frozen_for_oracle_and_route" if accepted else (
            "above_initial_1.5_percent_hpwl_ceiling" if not hpwl_pass else "no_legal_candidate_odb"
        )
        filter_rows.append({
            "candidate_id": candidate_id,
            "instance": proposal["instance"],
            "branch_before": proposal.get("branch_before", ""),
            "move_type": proposal.get("move_type", ""),
            "signal_hpwl_delta_pct": f"{hpwl:.6f}",
            "placement_status": audit["status"],
            "timing_screen_pass": int(audit["status"] == "timing_safe"),
            "initial_hpwl_ceiling_pass": int(hpwl_pass),
            "candidate_odb_exists": int(route_ready),
            "frozen_pool_member": int(accepted),
            "reason": reason,
        })
        if accepted:
            candidate_rows.append(proposal)

    if not candidate_rows:
        raise SystemExit("no candidates survived the initial HPWL ceiling and legal-placement screen")
    candidate_rows.sort(key=lambda row: row["candidate_id"])
    baseline_sinks = read_tsv(experiment / "baseline_cts_sinks.tsv")
    sink_branches = {row["instance"]: row["branch"] for row in baseline_sinks}
    sink_names = set(sink_branches)
    represented = {row["instance"] for row in candidate_rows}
    missing_sinks = sorted(sink_names - represented)
    if missing_sinks:
        raise SystemExit(f"frozen pool does not cover all CTS sinks: {missing_sinks}")
    branches = {sink_branches[row["instance"]] for row in candidate_rows}
    if len(branches) != 4:
        raise SystemExit(f"expected all four CTS leaves in pool; found {sorted(branches)}")
    moves = [(row["instance"], int(row["target_x_dbu"]), int(row["target_y_dbu"])) for row in candidate_rows]
    if len(moves) != len(set(moves)):
        raise SystemExit("duplicate instance/target moves are present in the frozen pool")
    if any(
        abs(int(row["target_x_dbu"]) - int(row["from_x_dbu"]))
        + abs(int(row["target_y_dbu"]) - int(row["from_y_dbu"])) > 60000
        for row in candidate_rows
    ):
        raise SystemExit("candidate exceeds evaluator's 60 um Manhattan displacement bound")

    pool_path = experiment / "candidate_pool.tsv"
    write_tsv(pool_path, candidate_rows)
    write_tsv(experiment / "pool_filter.tsv", filter_rows)
    integrity_files = [
        pool_path,
        experiment / "pool_filter.tsv",
        experiment / "raw_candidate_pool.tsv",
        experiment / "supplemental_candidate_pool.tsv",
        experiment / "supplemental2_candidate_pool.tsv",
        experiment / "initial_relocation_audit.tsv",
        experiment / "relocation_audit.tsv",
        experiment / "supplemental_audit.tsv",
        experiment / "supplemental_candidates.tsv",
        experiment / "move_overlap_audit.tsv",
        experiment / "previous_pool_source_manifest.tsv",
        experiment / "raw_candidate_pool.integrity.tsv",
        experiment / "supplemental_candidate_pool.integrity.tsv",
        experiment / "supplemental2_candidate_pool.integrity.tsv",
        experiment / "baseline_cts_sinks.tsv",
        experiment / "inventory.tsv",
        experiment / "rows.tsv",
        ROOT / "scripts/make_gcd_cross_row_pool.py",
        ROOT / "scripts/export_gcd_placement_rows.tcl",
        ROOT / "scripts/evaluate_gcd_flop_relocations.tcl",
        ROOT / "scripts/freeze_gcd_cross_row_pool.py",
    ]
    integrity = [
        {"artifact": "frozen_input", "path": str(path.resolve()), "sha256": sha256(path), "size_bytes": path.stat().st_size}
        for path in integrity_files if path.is_file()
    ]
    integrity.extend(
        {"artifact": "candidate_placement_odb", "path": str(odb.resolve()), "sha256": sha256(odb), "size_bytes": odb.stat().st_size}
        for odb in sorted(candidate_odb_dir.glob("*.odb"))
        if odb.stem in {row["candidate_id"] for row in candidate_rows}
    )
    write_tsv(experiment / "frozen_pool_integrity.tsv", integrity)
    print(
        f"GCD_CROSS_ROW_FREEZE proposals={len(proposals)} frozen={len(candidate_rows)} "
        f"hpwl_rejects={len(proposals)-len(candidate_rows)} sinks={len(represented)} "
        f"leaves={len(branches)} output={pool_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
