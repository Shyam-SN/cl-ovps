#!/usr/bin/env python3
"""Build the trial CTS checkpoint for a placement ODB using a supplied ORFS config."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design", required=True)
    parser.add_argument("--flow-dir", type=Path, default=ROOT / "tools/openroad-flow-scripts/flow")
    parser.add_argument("--flow-config", required=True, type=Path)
    parser.add_argument("--work-home", required=True, type=Path)
    parser.add_argument("--platform", default="sky130hd")
    parser.add_argument("--design-nickname", required=True)
    parser.add_argument("--placement-odb", required=True, type=Path)
    parser.add_argument("--placement-sdc", required=True, type=Path)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--openroad", type=Path, default=ROOT / "tools/openroad/build/bin/openroad")
    parser.add_argument("--num-cores", type=int, default=1)
    args = parser.parse_args()
    for path in (args.flow_dir / "Makefile", args.flow_config, args.work_home,
                 args.placement_odb, args.placement_sdc, args.openroad):
        if not path.exists():
            parser.error(f"missing CTS input: {path}")
    output = args.output_dir.resolve()
    if output.exists():
        parser.error(f"refusing to overwrite CTS output: {output}")
    output.mkdir(parents=True)
    results = args.work_home.resolve() / "results" / args.platform / args.design_nickname / args.variant
    if results.exists():
        parser.error(f"ORFS variant already exists: {results}")
    results.mkdir(parents=True)
    shutil.copy2(args.placement_odb, results / "3_place.odb")
    shutil.copy2(args.placement_odb, results / "3_5_place_dp.odb")
    shutil.copy2(args.placement_sdc, results / "3_place.sdc")
    command = ["make", "-C", str(args.flow_dir.resolve()),
               f"DESIGN_CONFIG={args.flow_config.resolve()}",
               f"DESIGN_NICKNAME={args.design_nickname}", f"FLOW_VARIANT={args.variant}",
               f"WORK_HOME={args.work_home.resolve()}", f"OPENROAD_EXE={args.openroad.resolve()}",
               f"NUM_CORES={args.num_cores}", "do-cts"]
    result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    log = output / "orfs_cts.log"
    log.write_text(result.stdout)
    cts_odb, cts_sdc = results / "4_1_cts.odb", results / "4_cts.sdc"
    if result.returncode or not cts_odb.is_file() or not cts_sdc.is_file():
        raise SystemExit(f"CTS failed; inspect {log}")
    manifest = {"schema_version": 1, "design": args.design, "variant": args.variant,
                "status": "PASS", "input_odb": str(args.placement_odb.resolve()),
                "input_odb_sha256": sha256(args.placement_odb),
                "cts_odb": str(cts_odb), "cts_odb_sha256": sha256(cts_odb),
                "cts_sdc": str(cts_sdc), "cts_sdc_sha256": sha256(cts_sdc),
                "log": str(log)}
    (output / "cts_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"CL_OVPS_CTS design={args.design} variant={args.variant} status=PASS odb={cts_odb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
