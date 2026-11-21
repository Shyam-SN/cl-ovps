#!/usr/bin/env python3
"""Export exact CTS sink-to-leaf assignments and clock/dummy masters for an oracle pool."""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
EXPORT_TCL = ROOT / "scripts/export_gcd_cts_oracle_data.tcl"


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = args.manifest.resolve()
    output = args.output_dir.resolve()
    inventory_dir = output / "inventories"
    inventory_dir.mkdir(parents=True, exist_ok=True)
    rows = read_tsv(manifest)
    exported = []
    for row in rows:
        candidate = row["candidate_id"]
        odb = Path(row["cts_odb"])
        if row.get("status") != "ok" or not odb.is_file():
            raise SystemExit(f"oracle CTS output is unavailable for {candidate}: {row}")
        sink_path = inventory_dir / f"{candidate}_sinks.tsv"
        cell_path = inventory_dir / f"{candidate}_cells.tsv"
        log_path = inventory_dir / f"{candidate}.export.log"
        env = os.environ.copy()
        env.update({
            "CTS_ORACLE_ODB": str(odb.resolve()),
            "CTS_ORACLE_SINK_OUT": str(sink_path),
            "CTS_ORACLE_CELL_OUT": str(cell_path),
        })
        start = time.monotonic()
        process = subprocess.run(
            [str(OPENROAD), "-no_init", "-exit", str(EXPORT_TCL)],
            cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, check=False,
        )
        log_path.write_text(process.stdout)
        if process.returncode or not sink_path.is_file() or not cell_path.is_file():
            raise RuntimeError(f"CTS oracle inventory export failed for {candidate}: {log_path}")
        sinks = read_tsv(sink_path)
        cells = read_tsv(cell_path)
        branches = {item["branch"] for item in sinks}
        if len(sinks) != 35 or len(branches) != 4:
            raise ValueError(f"{candidate} has {len(sinks)} sinks across {len(branches)} CTS leaves")
        exported.append({
            "candidate_id": candidate,
            "cts_odb": str(odb.resolve()),
            "cts_odb_sha256": sha256(odb),
            "sink_inventory": str(sink_path),
            "sink_inventory_sha256": sha256(sink_path),
            "cell_inventory": str(cell_path),
            "cell_inventory_sha256": sha256(cell_path),
            "sink_count": len(sinks),
            "leaf_count": len(branches),
            "clock_and_dummy_cell_count": len(cells),
            "export_elapsed_seconds": f"{time.monotonic()-start:.3f}",
        })
        print(f"GCD_CTS_ORACLE_EXPORT candidate={candidate} sinks={len(sinks)} leaves={len(branches)}", flush=True)
    out_manifest = output / "oracle_inventory_manifest.tsv"
    with out_manifest.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(exported[0]), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(exported)
    print(f"GCD_CTS_ORACLE_EXPORT candidates={len(exported)} output={out_manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
