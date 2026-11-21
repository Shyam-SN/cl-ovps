#!/usr/bin/env python3
"""Apply Tier-1 accepted AES moves greedily and re-query CTS after each addition."""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import analyze_aes_tier1_generalization as analyzer  # noqa: E402
import run_aes_tier1_generalization as aes  # noqa: E402


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def apply_pool(source_odb: Path, pool: Path, name: str, analysis: Path) -> Path | None:
    out_dir = analysis / f"{name}_placement"
    out_dir.mkdir()
    audit = analysis / f"{name}_move_audit.tsv"
    env = os.environ.copy()
    env.update({"PLACEMENT_ODB": str(source_odb), "RELOCATION_POOL": str(pool),
                "CANDIDATE_ODB_DIR": str(out_dir), "MOVE_AUDIT": str(audit)})
    aes.run_openroad(aes.MOVE_TCL, env, analysis / f"{name}_move_export.log")
    rows = read_tsv(audit)
    return Path(rows[0]["odb"]) if rows and rows[0]["status"] == "saved" else None


def run_candidate_cts(placement_odb: Path, variant: str, analysis: Path) -> tuple[dict[str, object], int, Path]:
    result_dir = analyzer.RESULTS / variant
    result_dir.mkdir(parents=True)
    shutil.copy2(placement_odb, result_dir / "3_5_place_dp.odb")
    shutil.copy2(placement_odb, result_dir / "3_place.odb")
    shutil.copy2(analyzer.RESULTS / aes.BASE_TAG / "3_place.sdc", result_dir / "3_place.sdc")
    aes.run_make(variant, ["do-cts", "do-grt"], analysis / f"{variant}.cts_grt.log", cores=2)
    power = analyzer.cts_cell_power(variant, analysis, variant, "cts")
    hpwl = analyzer.signal_hpwl(placement_odb, analysis, variant)
    return power, hpwl, result_dir / "4_1_cts.odb"


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} EXPERIMENT_DIR")
    experiment = Path(sys.argv[1]).resolve()
    analysis = experiment / "analysis"
    pool_rows = {row["candidate_id"]: row for row in read_tsv(analysis / "candidate_pool.tsv")}
    selection_rows = read_tsv(analysis / "tier1_greedy_selection.tsv")
    passing = [row for row in selection_rows if row["tier1_gate_pass"] == "YES"]
    if not passing:
        raise RuntimeError("the frozen AES candidate pool has no Tier-1 passing move")
    first = min(passing, key=lambda row: float(row["cts_dummy_internal_delta_uw"]))
    current_id = first["candidate_id"]
    current_variant = f"aes_tier1gen_20261031_{current_id}"
    current_odb = experiment / "candidate_odb" / f"{current_id}.odb"
    current_power = analyzer.cts_cell_power(current_variant, analysis, f"accepted_{current_id}", "cts")
    base_placement = analyzer.RESULTS / aes.BASE_TAG / "3_5_place_dp.odb"
    base_power = analyzer.cts_cell_power(aes.BASE_TAG, analysis, "chain_baseline", "cts")
    base_hpwl = analyzer.signal_hpwl(base_placement, analysis, "chain_baseline")
    current_hpwl = analyzer.signal_hpwl(current_odb, analysis, f"chain_accepted_{current_id}")
    remaining = [row["candidate_id"] for row in sorted(
        passing, key=lambda row: float(row["cts_dummy_internal_delta_uw"])) if row["candidate_id"] != current_id]
    committed = [current_id]
    trial_rows: list[dict[str, object]] = []
    stage_index = 1

    while remaining and stage_index <= 2:
        evaluated: list[tuple[float, str, Path, str, dict[str, object], int]] = []
        for candidate in remaining:
            source_row = pool_rows[candidate]
            trial_id = f"chain{stage_index}_{candidate}"
            one_move_pool = analysis / f"{trial_id}.pool.tsv"
            with one_move_pool.open("w", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=list(source_row), delimiter="\t", lineterminator="\n")
                writer.writeheader()
                writer.writerow({**source_row, "candidate_id": trial_id})
            combined_place = apply_pool(current_odb, one_move_pool, trial_id, analysis)
            if combined_place is None:
                trial_rows.append({"trial_id": trial_id, "added_move": candidate, "status": "placement_rejected"})
                continue
            variant = f"aes_tier1chain_20261031_{trial_id}"
            power, hpwl, _cts_odb = run_candidate_cts(combined_place, variant, analysis)
            delta_uw = (float(power["dummy_internal_w"]) - float(current_power["dummy_internal_w"])) * 1e6
            hpwl_total_pct = 100.0 * (hpwl - base_hpwl) / base_hpwl
            accepted = delta_uw <= -1.0 and hpwl_total_pct <= 1.5
            trial_rows.append({
                "trial_id": trial_id, "added_move": candidate, "status": "evaluated",
                "incremental_dummy_internal_delta_uw": delta_uw,
                "total_signal_hpwl_delta_pct": hpwl_total_pct,
                "dummy_cell_count": power["dummy_count"], "cts_buffer_count": power["buffer_count"],
                "tier1_chain_gate_pass": "YES" if accepted else "NO",
                "combined_place_odb": str(combined_place), "cts_grt_variant": variant,
            })
            if accepted:
                evaluated.append((delta_uw, candidate, combined_place, variant, power, hpwl))
        if not evaluated:
            break
        _delta, chosen, current_odb, current_variant, current_power, current_hpwl = min(evaluated)
        committed.append(chosen)
        remaining.remove(chosen)
        stage_index += 1

    final_place = experiment / "tier1_chain_final_place.odb"
    shutil.copy2(current_odb, final_place)
    write_tsv(analysis / "tier1_chain_trials.tsv", trial_rows)
    write_tsv(analysis / "tier1_chain_manifest.tsv", [{
        "committed_candidates": ";".join(committed),
        "move_count": len(committed), "final_place_odb": str(final_place),
        "final_signal_hpwl_delta_pct": 100.0 * (current_hpwl - base_hpwl) / base_hpwl,
        "final_dummy_internal_delta_uw": (float(current_power["dummy_internal_w"])
                                           - float(base_power["dummy_internal_w"])) * 1e6,
        "final_cts_variant": current_variant,
    }])
    print(f"AES_TIER1_CHAIN committed={','.join(committed)} trials={len(trial_rows)} final={final_place}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
