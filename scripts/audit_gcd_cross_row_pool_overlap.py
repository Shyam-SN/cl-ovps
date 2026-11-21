#!/usr/bin/env python3
"""Verify a frozen GCD pool has no exact move overlap with previous pools."""

from __future__ import annotations

import argparse
import csv
import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def move(row: dict[str, str]) -> tuple[str, int, int] | None:
    instance = row.get("instance", "")
    x = next((row.get(key, "") for key in ("target_x_dbu", "to_x_dbu", "target_x", "to_x") if row.get(key, "") != ""), "")
    y = next((row.get(key, "") for key in ("target_y_dbu", "to_y_dbu", "target_y", "to_y") if row.get(key, "") != ""), "")
    if not y:
        y = next((row.get(key, "") for key in ("from_y_dbu", "y_dbu", "from_y", "y") if row.get(key, "") != ""), "")
    if not instance or not x or not y:
        return None
    try:
        return instance, int(float(x)), int(float(y))
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--previous-root", type=Path, default=ROOT / "build/power")
    args = parser.parse_args()
    experiment = args.experiment_dir.resolve()
    pool = read_tsv(experiment / "candidate_pool.tsv")
    current_moves = {move(row) for row in pool}
    if None in current_moves:
        raise SystemExit("frozen candidate pool has a row without explicit target coordinates")
    sink_names = {row["instance"] for row in pool}

    source_paths = sorted({
        path.resolve()
        for pattern in ("candidate_pool.tsv", "relocation_audit.tsv")
        for path in args.previous_root.rglob(pattern)
        if experiment not in path.resolve().parents
    })
    previous_moves: set[tuple[str, int, int]] = set()
    source_rows = []
    for path in source_paths:
        parsed = 0
        for row in read_tsv(path):
            candidate = move(row)
            if candidate and candidate[0] in sink_names:
                previous_moves.add(candidate)
                parsed += 1
        source_rows.append({"path": str(path), "sha256": sha256(path), "matched_gcd_moves": parsed})
    overlaps = sorted(current_moves & previous_moves)
    if overlaps:
        raise SystemExit(f"frozen pool contains {len(overlaps)} exact moves found in prior pools: {overlaps[:5]}")

    output = experiment / "move_overlap_audit.tsv"
    with output.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["metric", "value"], delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows([
            {"metric": "frozen_candidate_moves", "value": len(current_moves)},
            {"metric": "previous_source_files", "value": len(source_paths)},
            {"metric": "prior_gcd_move_records", "value": len(previous_moves)},
            {"metric": "exact_move_overlaps", "value": len(overlaps)},
            {"metric": "status", "value": "PASS"},
        ])
    manifest = experiment / "previous_pool_source_manifest.tsv"
    with manifest.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["path", "sha256", "matched_gcd_moves"], delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(source_rows)
    print(
        f"GCD_CROSS_ROW_OVERLAP candidates={len(current_moves)} previous_moves={len(previous_moves)} "
        f"source_files={len(source_paths)} overlaps=0 output={output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
