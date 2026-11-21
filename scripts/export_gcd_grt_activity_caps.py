#!/usr/bin/env python3
"""Export VCD-weighted clock/signal capacitance from GCD GRT checkpoints."""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
TCL = ROOT / "scripts/gcd_report_global_route_activity_caps.tcl"
EXP = ROOT / "build/power/gcd_flop_relocation_cross_row_holdout_20261029a"
RESULTS = ROOT / "build/orfs/results/sky130hd/gcd_power"
ACTIVITY = ROOT / "build/activity"
WORKLOADS = ("idle", "typical", "peak")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reuse-raw", action="store_true", help="reuse already exported per-variant files")
    args = parser.parse_args()

    feature_rows = read_tsv(EXP / "analysis/grt_clock_features.tsv")
    out_dir = EXP / "analysis/grt_activity_caps"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, str]] = []
    for index, row in enumerate(feature_rows, start=1):
        candidate = row["candidate_id"]
        variant = row["variant"]
        result_dir = RESULTS / variant
        odb = result_dir / "5_1_grt.odb"
        sdc = result_dir / "5_1_grt.sdc"
        vcd_dir = ACTIVITY / variant / ("routed" if candidate == "baseline" else "candidate")
        vcds = {workload: vcd_dir / f"{workload}.vcd" for workload in WORKLOADS}
        if not odb.is_file() or not sdc.is_file() or not all(path.is_file() for path in vcds.values()):
            raise FileNotFoundError(f"missing GRT checkpoint or matched VCD for {candidate}/{variant}")
        output = out_dir / f"{candidate}.tsv"
        log = out_dir / f"{candidate}.log"
        before = sha256(odb)
        if not (args.reuse_raw and output.is_file() and log.is_file()):
            env = os.environ.copy()
            env.update({
                "STAGE_ODB": str(odb),
                "STAGE_SDC": str(sdc),
                "IDLE_VCD": str(vcds["idle"]),
                "TYPICAL_VCD": str(vcds["typical"]),
                "PEAK_VCD": str(vcds["peak"]),
                "GRT_ACTIVITY_CAP_REPORT": str(output),
            })
            proc = subprocess.run(
                [str(OPENROAD), "-no_init", "-exit", str(TCL)],
                cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, check=False,
            )
            log.write_text(proc.stdout)
            if proc.returncode:
                raise RuntimeError(f"GRT activity-cap export failed for {candidate}; see {log}")
        after = sha256(odb)
        if before != after:
            raise ValueError(f"read-only exporter modified GRT ODB for {candidate}")
        rows = [line for line in output.read_text().splitlines() if line.startswith("#GRT_DRIVER_CAP\t")]
        if not rows or not any("\tidle\t" in line for line in rows) or not any("\ttypical\t" in line for line in rows) or not any("\tpeak\t" in line for line in rows):
            raise ValueError(f"incomplete GRT activity-cap export for {candidate}")
        manifest.append({
            "candidate_id": candidate,
            "variant": variant,
            "odb_sha256": before,
            "idle_vcd_sha256": sha256(vcds["idle"]),
            "typical_vcd_sha256": sha256(vcds["typical"]),
            "peak_vcd_sha256": sha256(vcds["peak"]),
            "report_sha256": sha256(output),
            "rows": str(len(rows) - 1),
            "output": str(output.relative_to(ROOT)),
        })
        print(f"[{index}/{len(feature_rows)}] exported {candidate} rows={len(rows)-1}", flush=True)

    path = out_dir / "manifest.tsv"
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(manifest[0]), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(manifest)
    print(f"GRT_ACTIVITY_CAP_EXPORT candidates={len(manifest)} output={path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
