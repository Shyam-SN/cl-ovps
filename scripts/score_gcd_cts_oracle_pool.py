#!/usr/bin/env python3
"""Freeze placement-CTS clock-power scores before routing a candidate pool."""

from __future__ import annotations

import argparse
import csv
import hashlib
from pathlib import Path

from analyze_gcd_cts_oracle import run_power_report
from analyze_gcd_cts_branch_proxy import read_tsv, write_tsv


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
    parser.add_argument("--pool-dir", type=Path, required=True)
    parser.add_argument("--oracle-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--baseline-activity-dir", type=Path, default=Path("build/activity/gcd_baseline20ns/routed"))
    args = parser.parse_args()

    def rooted(path: Path) -> Path:
        return path.resolve() if path.is_absolute() else (ROOT / path).resolve()

    pool_dir = rooted(args.pool_dir)
    output_dir = rooted(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = read_tsv(rooted(args.oracle_manifest))
    baseline_vcd_dir = rooted(args.baseline_activity_dir)
    openroad = ROOT / "tools/openroad/build/bin/openroad"
    power_tcl = ROOT / "scripts/gcd_report_placed_cts_power.tcl"

    raw_power: dict[str, dict[str, tuple[float, float, float, float]]] = {}
    integrity_rows: list[dict[str, str]] = []
    for artifact_name, artifact_path in (
        ("candidate_pool_manifest", pool_dir / "candidate_pool.tsv"),
        ("placement_screen_audit", pool_dir / "relocation_audit.tsv"),
        ("cts_oracle_manifest", rooted(args.oracle_manifest)),
    ):
        integrity_rows.append({
            "candidate_id": "pool",
            "artifact": artifact_name,
            "path": str(artifact_path),
            "sha256": sha256(artifact_path),
        })
    for row in manifest:
        candidate_id = row["candidate_id"]
        source_odb = pool_dir / "candidate_odb" / f"{candidate_id}.odb" if candidate_id != "baseline" else ROOT / "build/orfs/results/sky130hd/gcd_power/gcd_baseline20ns/3_5_place_dp.odb"
        if source_odb.is_file():
            integrity_rows.append({
                "candidate_id": candidate_id,
                "artifact": "source_placement_odb",
                "path": str(source_odb),
                "sha256": sha256(source_odb),
            })
        cts_odb = Path(row["cts_odb"])
        cts_sdc = Path(row["cts_sdc"])
        integrity_rows.append({
            "candidate_id": candidate_id,
            "artifact": "cts_oracle_odb",
            "path": str(cts_odb),
            "sha256": sha256(cts_odb),
        })
        raw_power[candidate_id] = {}
        for workload in WORKLOADS:
            log_path = output_dir / "power_logs" / candidate_id / f"{workload}.log"
            raw_power[candidate_id][workload] = run_power_report(
                openroad,
                power_tcl,
                cts_odb,
                cts_sdc,
                baseline_vcd_dir / f"{workload}.vcd",
                workload,
                log_path,
            )

    baseline = raw_power["baseline"]
    score_rows: list[dict[str, object]] = []
    for candidate_id, workload_power in raw_power.items():
        for workload in WORKLOADS:
            power = workload_power[workload]
            base = baseline[workload]
            score_rows.append({
                "candidate_id": candidate_id,
                "workload": workload,
                "oracle_internal_delta_nw": (power[0] - base[0]) * 1e9,
                "oracle_switching_delta_nw": (power[1] - base[1]) * 1e9,
                "oracle_clock_dynamic_delta_nw": ((power[0] + power[1]) - (base[0] + base[1])) * 1e9,
                "oracle_leakage_delta_nw": (power[2] - base[2]) * 1e9,
                "vcd_source": "baseline_workload_trace",
                "parasitics": "placement_estimate",
            })

    # Rank each workload independently; lower predicted delta ranks as better.
    for workload in WORKLOADS:
        selected = [row for row in score_rows if row["workload"] == workload]
        ranked = sorted(selected, key=lambda row: float(row["oracle_clock_dynamic_delta_nw"]))
        for rank, row in enumerate(ranked, start=1):
            row["clock_dynamic_rank"] = rank
    write_tsv(output_dir / "frozen_oracle_scores.tsv", score_rows)
    write_tsv(output_dir / "frozen_score_integrity.tsv", integrity_rows)

    top_by_workload = {}
    for workload in WORKLOADS:
        top_by_workload[workload] = sorted(
            (row for row in score_rows if row["workload"] == workload and row["candidate_id"] != "baseline"),
            key=lambda row: float(row["oracle_clock_dynamic_delta_nw"]),
        )[:5]
    summary = [
        "# Frozen CTS-oracle scores (before routing)",
        "",
        f"Scored {len(manifest) - 1} placement candidates plus baseline.",
        "Frozen score = placement-CTS `report_power` internal + switching power for CTS buffers and dummy loads, using the same baseline workload VCD for every candidate.",
        "Lower (more negative) score delta predicts lower clock dynamic power. No fitted scale or candidate-specific tuning is applied.",
        "These predictions were written before routing and should remain unchanged during holdout evaluation.",
        "",
        "## Five lowest predicted clock-power deltas",
        "",
    ]
    for workload in WORKLOADS:
        summary.append(f"### {workload}")
        summary.append("")
        for row in top_by_workload[workload]:
            summary.append(
                f"- {row['candidate_id']}: {float(row['oracle_clock_dynamic_delta_nw']):+.3f} nW "
                f"(internal {float(row['oracle_internal_delta_nw']):+.3f} nW; "
                f"switching {float(row['oracle_switching_delta_nw']):+.3f} nW)."
            )
        summary.append("")
    summary.append(f"Manifest and source hashes: `{output_dir / 'frozen_score_integrity.tsv'}`.")
    (output_dir / "frozen_score_summary.md").write_text("\n".join(summary) + "\n")
    print(f"FROZEN_CTS_ORACLE_SCORE candidates={len(manifest)-1} output={output_dir / 'frozen_oracle_scores.tsv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
