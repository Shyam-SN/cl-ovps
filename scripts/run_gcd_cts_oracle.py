#!/usr/bin/env python3
"""Run only the OpenROAD CTS stage for saved GCD placement ODBs."""

from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FLOW_DIR = ROOT / "tools/openroad-flow-scripts/flow"
DESIGN_CONFIG = ROOT / "designs/gcd/flow/config.mk"
BASELINE_RESULT_DIR = (
    ROOT / "build/orfs/results/sky130hd/gcd_power/gcd_baseline20ns"
)


def run_cts(odb: Path, variant: str, num_cores: int) -> dict[str, object]:
    result_dir = ROOT / "build/orfs/results/sky130hd/gcd_power" / variant
    report_dir = ROOT / "build/power" / variant
    if result_dir.exists() or report_dir.exists():
        raise FileExistsError(f"refusing to overwrite CTS oracle output {variant}")
    if not odb.is_file():
        raise FileNotFoundError(odb)
    sdc = BASELINE_RESULT_DIR / "3_place.sdc"
    if not sdc.is_file():
        raise FileNotFoundError(sdc)

    result_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)
    shutil.copy2(odb, result_dir / "3_place.odb")
    shutil.copy2(odb, result_dir / "3_5_place_dp.odb")
    shutil.copy2(sdc, result_dir / "3_place.sdc")
    command = [
        "make",
        "-C",
        str(FLOW_DIR),
        f"DESIGN_CONFIG={DESIGN_CONFIG}",
        "DESIGN_NICKNAME=gcd_power",
        f"FLOW_VARIANT={variant}",
        f"WORK_HOME={ROOT / 'build/orfs'}",
        f"OPENROAD_EXE={ROOT / 'tools/openroad/build/bin/openroad'}",
        f"NUM_CORES={num_cores}",
        "do-cts",
    ]
    start = time.monotonic()
    process = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    elapsed = time.monotonic() - start
    (report_dir / "orfs_cts.log").write_text(process.stdout)
    cts_odb = result_dir / "4_1_cts.odb"
    cts_sdc = result_dir / "4_cts.sdc"
    succeeded = process.returncode == 0 and cts_odb.is_file() and cts_sdc.is_file()
    return {
        "variant": variant,
        "source_odb": str(odb),
        "result_dir": str(result_dir),
        "return_code": process.returncode,
        "elapsed_seconds": f"{elapsed:.3f}",
        "cts_odb": str(cts_odb) if cts_odb.is_file() else "",
        "cts_sdc": str(cts_sdc) if cts_sdc.is_file() else "",
        "status": "ok" if succeeded else "failed",
        "log": str(report_dir / "orfs_cts.log"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pool-dir", type=Path)
    source.add_argument("--placement-odb", type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--num-cores", type=int, default=1)
    args = parser.parse_args()

    if args.pool_dir:
        pool_dir = args.pool_dir if args.pool_dir.is_absolute() else ROOT / args.pool_dir
        placements = [
            ("baseline", BASELINE_RESULT_DIR / "3_5_place_dp.odb"),
            *[
                (path.stem, path)
                for path in sorted((pool_dir / "candidate_odb").glob("*.odb"))
            ],
        ]
        if len(placements) == 1:
            raise SystemExit(f"no candidate ODBs found under {pool_dir / 'candidate_odb'}")
    else:
        placements = [(args.placement_odb.stem, args.placement_odb)]

    manifest = args.manifest if args.manifest.is_absolute() else ROOT / args.manifest
    manifest.parent.mkdir(parents=True, exist_ok=True)
    if manifest.exists():
        raise FileExistsError(f"refusing to overwrite manifest {manifest}")

    rows: list[dict[str, object]] = []
    for candidate_id, odb in placements:
        variant = f"gcd_cts_oracle_{args.tag}_{candidate_id}"
        row = run_cts(odb.resolve(), variant, args.num_cores)
        row["candidate_id"] = candidate_id
        rows.append(row)
        print(
            f"GCD_CTS_ORACLE candidate={candidate_id} status={row['status']} "
            f"elapsed_seconds={row['elapsed_seconds']}",
            flush=True,
        )
        if row["status"] != "ok":
            break

    with manifest.open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=list(rows[0]),
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"GCD_CTS_ORACLE_MANIFEST rows={len(rows)} output={manifest}")
    return 0 if all(row["status"] == "ok" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
