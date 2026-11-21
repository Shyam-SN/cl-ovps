#!/usr/bin/env python3
"""Detailed-route every candidate in a previously hashed GCD GRT holdout."""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTE_SCRIPT = ROOT / "scripts/run_gcd_candidate_spef_flow.sh"


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError("refusing to write an empty route manifest")
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def route_one(exp: Path, candidate: dict[str, str], tag: str, cores: int) -> dict[str, object]:
    candidate_id = candidate["candidate_id"]
    odb = exp / "candidate_odb" / f"{candidate_id}.odb"
    if not odb.is_file():
        raise FileNotFoundError(f"missing frozen placement ODB for {candidate_id}: {odb}")
    variant = f"gcd_reloc_{tag}_{candidate_id}"
    log_path = exp / f"{candidate_id}.route_driver.log"
    env = os.environ.copy()
    env.update({"MOVE_CANDIDATE_ODB": str(odb), "CANDIDATE_FLOW_VARIANT": variant,
                "NUM_CORES": str(cores)})
    process = subprocess.run(["bash", str(ROUTE_SCRIPT)], cwd=ROOT, env=env, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    log_path.write_text(process.stdout)
    print(f"GCD_FINAL_GRT_ROUTE candidate={candidate_id} variant={variant} "
          f"return_code={process.returncode}", flush=True)
    audit = next(row for row in read_tsv(exp / "relocation_audit.tsv") if row["candidate_id"] == candidate_id)
    return {
        "candidate_id": candidate_id,
        "instance": audit["instance"],
        "from_x_dbu": audit["from_x_dbu"],
        "from_y_dbu": audit["from_y_dbu"],
        "to_x_dbu": audit["to_x_dbu"],
        "to_y_dbu": audit["to_y_dbu"],
        "signal_hpwl_delta_pct": audit["signal_hpwl_delta_pct"],
        "route_variant": variant,
        "return_code": process.returncode,
        "route_log": str(log_path),
        "summary": str(ROOT / "build/power" / variant / "summary.txt"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--cores-per-route", type=int, default=1)
    args = parser.parse_args()
    experiment = args.experiment_dir.resolve()
    manifest = experiment / "route_manifest.tsv"
    if manifest.exists():
        raise FileExistsError(f"refusing to overwrite {manifest}")
    if args.workers < 1 or args.cores_per_route < 1:
        raise SystemExit("workers and cores-per-route must be positive")
    if not (experiment / "analysis/frozen_before_detail_route.tsv").is_file():
        raise SystemExit("missing pre-route freeze marker; refuse to begin detailed routing")
    marker = {row["metric"]: row["value"] for row in read_tsv(experiment / "analysis/frozen_before_detail_route.tsv")}
    if marker.get("frozen_before_detail_route") != "YES":
        raise SystemExit("pre-route freeze marker is invalid")
    analysis = experiment / "analysis"
    frozen_predictions = analysis / "frozen_three_gate_predictions.tsv"
    if hashlib.sha256(frozen_predictions.read_bytes()).hexdigest() != marker.get("three_gate_predictions_sha256"):
        raise SystemExit("frozen gate predictions changed after the pre-route freeze")
    if hashlib.sha256((analysis / "frozen_gate_protocol.md").read_bytes()).hexdigest() != marker.get("gate_protocol_sha256"):
        raise SystemExit("frozen gate protocol changed after the pre-route freeze")
    for row in read_tsv(analysis / "frozen_prediction_integrity.tsv"):
        path = Path(row["path"])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise SystemExit(f"pre-route frozen input changed: {path}")

    pool = read_tsv(experiment / "candidate_pool.tsv")
    rows: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(route_one, experiment, candidate, args.tag, args.cores_per_route): candidate["candidate_id"]
            for candidate in pool
        }
        failed = False
        for future in as_completed(futures):
            candidate_id = futures[future]
            try:
                row = future.result()
                rows.append(row)
                failed |= int(row["return_code"]) != 0
            except Exception as error:
                failed = True
                rows.append({"candidate_id": candidate_id, "route_variant": f"gcd_reloc_{args.tag}_{candidate_id}",
                             "return_code": 99, "error": str(error)})
                print(f"GCD_FINAL_GRT_ROUTE candidate={candidate_id} exception={error}", flush=True)
    rows.sort(key=lambda row: str(row["candidate_id"]))
    write_tsv(manifest, rows)
    print(f"GCD_FINAL_GRT_ROUTE_MANIFEST candidates={len(rows)} failed={sum(int(row['return_code']) != 0 for row in rows)} "
          f"output={manifest}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
