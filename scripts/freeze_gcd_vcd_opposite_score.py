#!/usr/bin/env python3
"""Freeze an unweighted VCD opposite-transition score before candidate routing.

Neighbor nets are selected from the trial-CTS leaf-branch corridor. Their VCD
opposite-direction transition rates are summed without multiplying by pin area,
distance, SPEF capacitance, or a fitted coefficient.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKLOADS = ("idle", "typical", "peak")
STAGE1_TRANSITION_THRESHOLD_NW = -1000.0
FEATURE_EXPORTER = ROOT / "scripts/export_gcd_cts_spatial_features.py"


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--feature-dir", type=Path)
    parser.add_argument("--stage1-score-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    experiment = args.experiment_dir.resolve()
    feature_dir = (args.feature_dir or experiment / "analysis/placement_spatial_features").resolve()
    stage1_dir = (args.stage1_score_dir or experiment / "analysis/frozen_stage1").resolve()
    output_dir = (args.output_dir or experiment / "analysis/frozen_vcd_score").resolve()
    if (experiment / "route_manifest.tsv").exists():
        raise RuntimeError("refusing to freeze the VCD score after candidate routing has started")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite frozen VCD score: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    pool_path = experiment / "candidate_pool.tsv"
    audit_path = experiment / "relocation_audit.tsv"
    cts_manifest_path = experiment / "analysis/cts_oracle_manifest.tsv"
    stage1_path = stage1_dir / "frozen_oracle_scores.tsv"
    stage1_integrity_path = stage1_dir / "frozen_score_integrity.tsv"
    activity_path = feature_dir / "branch_neighbor_activity.tsv"
    required = (pool_path, audit_path, cts_manifest_path, stage1_path,
                stage1_integrity_path, activity_path, FEATURE_EXPORTER)
    for path in required:
        if not path.is_file():
            raise FileNotFoundError(path)

    pool_rows = read_tsv(pool_path)
    pool_ids = [row["candidate_id"] for row in pool_rows]
    if len(pool_ids) != len(set(pool_ids)):
        raise ValueError("candidate pool has duplicate IDs")
    safe_ids = {
        row["candidate_id"] for row in read_tsv(audit_path)
        if row["status"] == "timing_safe" and row["odb"]
    }
    cts_ids = {
        row["candidate_id"] for row in read_tsv(cts_manifest_path)
        if row["candidate_id"] != "baseline" and row["status"] == "ok"
    }
    stage1_rows = {
        row["candidate_id"]: row for row in read_tsv(stage1_path)
        if row["workload"] == "typical" and row["candidate_id"] != "baseline"
    }
    if set(pool_ids) != safe_ids or set(pool_ids) != cts_ids or set(pool_ids) != set(stage1_rows):
        raise ValueError(
            f"candidate mismatch: pool={len(pool_ids)} timing_safe={len(safe_ids)} "
            f"CTS={len(cts_ids)} Stage1={len(stage1_rows)}"
        )

    activity: dict[str, dict[str, float]] = {
        candidate: {workload: 0.0 for workload in WORKLOADS} for candidate in pool_ids
    }
    mapped_rows = {candidate: 0 for candidate in pool_ids}
    neighbor_rows = {candidate: 0 for candidate in pool_ids}
    for row in read_tsv(activity_path):
        candidate = row["candidate_id"]
        if candidate == "baseline":
            continue
        if candidate not in activity:
            raise ValueError(f"VCD feature contains unexpected candidate {candidate}")
        neighbor_rows[candidate] += 1
        for workload in WORKLOADS:
            key = f"{workload}_pair_opposite_direction_hz"
            if row.get(key, "") != "":
                activity[candidate][workload] += float(row[key])
                if workload == "typical":
                    mapped_rows[candidate] += 1

    ranked = sorted(pool_ids, key=lambda candidate: (
        activity[candidate]["typical"], candidate
    ))
    rank_all = {candidate: rank for rank, candidate in enumerate(ranked, start=1)}
    transition_ids = {
        candidate for candidate in pool_ids
        if float(stage1_rows[candidate]["oracle_internal_delta_nw"])
        <= STAGE1_TRANSITION_THRESHOLD_NW
    }
    ranked_transition = sorted(transition_ids, key=lambda candidate: (
        activity[candidate]["typical"], candidate
    ))
    rank_transition = {candidate: rank for rank, candidate in enumerate(ranked_transition, start=1)}

    score_rows: list[dict[str, object]] = []
    for candidate in pool_ids:
        stage1 = stage1_rows[candidate]
        proposal = next(row for row in pool_rows if row["candidate_id"] == candidate)
        score_rows.append({
            "candidate_id": candidate,
            "instance": proposal["instance"],
            "from_x_dbu": proposal["from_x_dbu"],
            "from_y_dbu": proposal["from_y_dbu"],
            "to_x_dbu": proposal["target_x_dbu"],
            "to_y_dbu": proposal["target_y_dbu"],
            "stage1_internal_delta_nw_typical": float(stage1["oracle_internal_delta_nw"]),
            "stage1_clock_dynamic_delta_nw_typical": float(stage1["oracle_clock_dynamic_delta_nw"]),
            "stage1_dummy_transition_proxy": int(candidate in transition_ids),
            "vcd_opposite_activity_hz_idle": activity[candidate]["idle"],
            "vcd_opposite_activity_hz_typical_primary": activity[candidate]["typical"],
            "vcd_opposite_activity_hz_peak": activity[candidate]["peak"],
            "vcd_typical_mapped_neighbor_rows": mapped_rows[candidate],
            "vcd_typical_total_neighbor_rows": neighbor_rows[candidate],
            "vcd_only_rank_all": rank_all[candidate],
            "vcd_only_rank_within_stage1_dummy_winners": rank_transition.get(candidate, ""),
        })
    score_rows.sort(key=lambda row: str(row["candidate_id"]))
    write_tsv(output_dir / "frozen_vcd_stage12_scores.tsv", score_rows)

    inputs = [
        ("candidate_pool", pool_path),
        ("placement_screen_audit", audit_path),
        ("cts_oracle_manifest", cts_manifest_path),
        ("stage1_frozen_scores", stage1_path),
        ("stage1_frozen_integrity", stage1_integrity_path),
        ("trial_cts_vcd_activity_features", activity_path),
        ("feature_exporter", FEATURE_EXPORTER),
        ("vcd_score_definition", Path(__file__).resolve()),
    ]
    for workload in WORKLOADS:
        inputs.append((
            f"baseline_{workload}_vcd",
            ROOT / f"build/activity/gcd_baseline20ns/routed/{workload}.vcd",
        ))
    integrity_rows = [
        {"artifact": label, "path": str(path), "sha256": sha256(path)}
        for label, path in inputs
    ]
    write_tsv(output_dir / "frozen_vcd_inputs.tsv", integrity_rows)

    lines = [
        "# Frozen VCD and CTS score (before routing)",
        "",
        f"Candidates frozen: {len(pool_ids)}; Stage1 dummy-transition proxy candidates: {len(transition_ids)}.",
        "No routed candidate SPEF, DEF, or power label was read. `route_manifest.tsv` was absent when this score was frozen.",
        "",
        "## Fixed scoring rule",
        "",
        f"1. Stage1 is a hard candidate bucket when typical CTS-oracle internal-power delta is at or below {STAGE1_TRANSITION_THRESHOLD_NW:g} nW.",
        "2. Within the bucket, lower summed typical VCD opposite-direction transition rate across trial-CTS leaf-branch neighbor nets ranks better.",
        "3. The activity rate is summed without pin-area, distance, SPEF-capacitance, or fitted weighting. Trial-CTS branch geometry only defines the candidate's neighboring-net set.",
        "4. Idle and peak rates are recorded as workload diagnostics; typical is the fixed primary activity score.",
        "5. Candidate proposals are constrained to legal same-row moves no longer than 20 µm, and the placement screen applies a 50 ps setup/hold guard.",
        "",
        "This is a frozen observational score. It is not OpenSTA coupling power; the later SPEF/VCD estimate is a routed label for out-of-sample comparison.",
        "See `frozen_vcd_inputs.tsv` for input hashes.",
    ]
    (output_dir / "frozen_vcd_stage12_summary.md").write_text("\n".join(lines) + "\n")
    print(
        f"FROZEN_VCD_STAGE12 candidates={len(pool_ids)} "
        f"stage1_transition_winners={len(transition_ids)} "
        f"scores={output_dir / 'frozen_vcd_stage12_scores.tsv'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
