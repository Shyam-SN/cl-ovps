#!/usr/bin/env python3
"""Freeze an unfit Stage 2 CTS spatial score before detailed routing.

The primary score is the Stage 2 typical-workload, VCD-pair-weighted
neighbor-pin proximity proxy. Access features are preregistered lexicographic
tie-breakers; no routed SPEF, DEF, or power labels are read here.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WINNER_INTERNAL_THRESHOLD_NW = -1000.0
SCORE_SCRIPT = Path(__file__).resolve()
GEOMETRY_SCRIPT = ROOT / "scripts/export_gcd_cts_spatial_geometry.tcl"
FEATURE_SCRIPT = ROOT / "scripts/export_gcd_cts_spatial_features.py"


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--spatial-feature-dir", type=Path, required=True)
    parser.add_argument("--stage1-score-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    def rooted(path: Path) -> Path:
        return path.resolve() if path.is_absolute() else (ROOT / path).resolve()

    experiment = rooted(args.experiment_dir)
    spatial_dir = rooted(args.spatial_feature_dir)
    stage1_dir = rooted(args.stage1_score_dir)
    output_dir = rooted(args.output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite frozen Stage 1/2 score directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    pool_path = experiment / "candidate_pool.tsv"
    audit_path = experiment / "relocation_audit.tsv"
    cts_manifest_path = experiment / "analysis/cts_oracle_manifest.tsv"
    stage1_path = stage1_dir / "frozen_oracle_scores.tsv"
    spatial_path = spatial_dir / "branch_features.tsv"
    for path in (pool_path, audit_path, cts_manifest_path, stage1_path, spatial_path):
        if not path.is_file():
            raise FileNotFoundError(path)

    stage1_rows = read_tsv(stage1_path)
    stage1 = {
        row["candidate_id"]: row
        for row in stage1_rows
        if row["workload"] == "typical" and row["candidate_id"] != "baseline"
    }
    branch_rows = read_tsv(spatial_path)
    stage2: dict[str, dict[str, float]] = {}
    for row in branch_rows:
        candidate = row["candidate_id"]
        if candidate == "baseline":
            continue
        entry = stage2.setdefault(candidate, {
            "idle": 0.0,
            "typical": 0.0,
            "peak": 0.0,
            "access_sites": 0.0,
            "access_clear_sites": 0.0,
            "met1_free_tracks": 0.0,
            "met2_free_tracks": 0.0,
            "li1_free_tracks": 0.0,
        })
        for workload in ("idle", "typical", "peak"):
            entry[workload] += float(row[f"{workload}_vcd_pair_relative_transition_proximity_proxy"])
        entry["access_sites"] += float(row["sum_L1M1_PR_MR_track_intersection_sites"])
        entry["access_clear_sites"] += float(row["sum_L1M1_PR_MR_geometry_clear_sites"])
        entry["met1_free_tracks"] += float(row["sum_r0.5_met1_free_track_count"])
        entry["met2_free_tracks"] += float(row["sum_r0.5_met2_free_track_count"])
        entry["li1_free_tracks"] += float(row["sum_r0.5_li1_free_track_count"])

    pool_ids = [row["candidate_id"] for row in read_tsv(pool_path)]
    if set(pool_ids) != set(stage1) or set(pool_ids) != set(stage2):
        raise ValueError(
            "candidate ID mismatch across candidate pool, Stage 1 scores, and Stage 2 features: "
            f"pool={len(pool_ids)} stage1={len(stage1)} stage2={len(stage2)}"
        )

    rows: list[dict[str, object]] = []
    for candidate in pool_ids:
        stage1_typical = stage1[candidate]
        internal_delta = float(stage1_typical["oracle_internal_delta_nw"])
        candidate_stage2 = stage2[candidate]
        transition = int(internal_delta <= WINNER_INTERNAL_THRESHOLD_NW)
        access_fraction = (
            candidate_stage2["access_clear_sites"] / candidate_stage2["access_sites"]
            if candidate_stage2["access_sites"] > 0
            else 0.0
        )
        rows.append({
            "candidate_id": candidate,
            "stage1_clock_dynamic_delta_nw_typical": float(stage1_typical["oracle_clock_dynamic_delta_nw"]),
            "stage1_internal_delta_nw_typical": internal_delta,
            "stage1_dummy_transition_proxy": transition,
            "stage2_pair_proxy_idle": candidate_stage2["idle"],
            "stage2_pair_proxy_typical_primary": candidate_stage2["typical"],
            "stage2_pair_proxy_peak": candidate_stage2["peak"],
            "stage2_access_site_count": candidate_stage2["access_sites"],
            "stage2_geometry_clear_site_fraction": access_fraction,
            "stage2_met1_free_track_count": candidate_stage2["met1_free_tracks"],
            "stage2_met2_free_track_count": candidate_stage2["met2_free_tracks"],
            "stage2_li1_free_track_count": candidate_stage2["li1_free_tracks"],
        })

    ordered_all = sorted(
        rows,
        key=lambda row: (
            float(row["stage2_pair_proxy_typical_primary"]),
            -float(row["stage2_geometry_clear_site_fraction"]),
            -float(row["stage2_met1_free_track_count"]),
            -float(row["stage2_met2_free_track_count"]),
            -float(row["stage2_li1_free_track_count"]),
            str(row["candidate_id"]),
        ),
    )
    rank_all = {str(row["candidate_id"]): index for index, row in enumerate(ordered_all, start=1)}
    winner_rows = [row for row in rows if int(row["stage1_dummy_transition_proxy"]) == 1]
    ordered_winners = sorted(
        winner_rows,
        key=lambda row: (
            float(row["stage2_pair_proxy_typical_primary"]),
            -float(row["stage2_geometry_clear_site_fraction"]),
            -float(row["stage2_met1_free_track_count"]),
            -float(row["stage2_met2_free_track_count"]),
            -float(row["stage2_li1_free_track_count"]),
            str(row["candidate_id"]),
        ),
    )
    rank_winners = {str(row["candidate_id"]): index for index, row in enumerate(ordered_winners, start=1)}
    for row in rows:
        row["stage2_rank_all"] = rank_all[str(row["candidate_id"])]
        row["stage2_rank_within_stage1_dummy_winners"] = rank_winners.get(str(row["candidate_id"]), "")
    rows.sort(key=lambda row: str(row["candidate_id"]))
    write_tsv(output_dir / "frozen_stage12_scores.tsv", rows)

    inputs = [
        ("candidate_pool", pool_path),
        ("placement_audit", audit_path),
        ("cts_oracle_manifest", cts_manifest_path),
        ("stage1_frozen_scores", stage1_path),
        ("stage2_branch_features", spatial_path),
        ("stage2_feature_exporter", FEATURE_SCRIPT),
        ("stage2_geometry_exporter", GEOMETRY_SCRIPT),
        ("stage12_score_definition", SCORE_SCRIPT),
    ]
    integrity = [
        {"artifact": label, "path": str(path), "sha256": sha256(path)}
        for label, path in inputs
    ]
    write_tsv(output_dir / "frozen_inputs.tsv", integrity)
    summary = [
        "# Frozen Stage 1 and Stage 2 candidate ranking",
        "",
        f"Candidates: {len(rows)}; Stage 1 dummy-transition proxy winners: {len(winner_rows)}.",
        "No routed DEF, detailed-route SPEF, or routed power label was read while computing this ranking.",
        "",
        "## Fixed ranking rule",
        "",
        f"1. Stage 1 transition bucket: typical-workload CTS internal-power delta ≤ {WINNER_INTERNAL_THRESHOLD_NW:g} nW. This threshold isolates the known multi-microwatt dummy-cell transition.",
        "2. Within the bucket (and for diagnostic all-candidate ordering), rank Stage 2 by lower summed typical-workload VCD pair-relative-transition proximity proxy.",
        "3. Break exact ties by higher geometry-clear L1M1 access-site fraction, then more local met1, met2, and li1 free tracks.",
        "4. Idle and peak Stage 2 proxies are recorded as workload diagnostics; typical is the predeclared primary workload.",
        "",
        "This is a preregistered, unfit heuristic. The score proxy multiplies timestamp-paired VCD relative-transition activity by nearby signal-pin geometry area and distance weight; it is not capacitance or power. The access tie-breakers are geometry screens, not DRC proof.",
        "",
        "Use only `frozen_stage12_scores.tsv` to select/rank candidates. Preserve `frozen_inputs.tsv` and verify all hashes before comparing with routed labels.",
    ]
    (output_dir / "frozen_stage12_summary.md").write_text("\n".join(summary) + "\n")
    print(
        f"FROZEN_STAGE12 candidates={len(rows)} stage1_transition_winners={len(winner_rows)} "
        f"scores={output_dir / 'frozen_stage12_scores.tsv'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
