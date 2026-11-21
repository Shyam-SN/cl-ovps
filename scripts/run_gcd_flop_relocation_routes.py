#!/usr/bin/env python3
"""Route every saved candidate ODB from a frozen GCD relocation screen."""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTE_SCRIPT = ROOT / "scripts/run_gcd_candidate_spef_flow.sh"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--continue-on-error", action="store_true")
    args = parser.parse_args()

    with args.audit.open(newline="") as source:
        rows = list(csv.DictReader(source, delimiter="\t"))
    selected = [row for row in rows if row["odb"]]
    if not selected:
        raise SystemExit("the relocation audit contains no timing-safe candidate ODBs")
    if args.manifest.exists():
        raise SystemExit(f"refusing to overwrite route manifest: {args.manifest}")

    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    result_rows: list[dict[str, object]] = []
    failed = False
    for row in selected:
        candidate = row["candidate_id"]
        variant = f"gcd_reloc_{args.tag}_{candidate}"
        log_path = args.manifest.parent / f"{candidate}.route_driver.log"
        environment = os.environ.copy()
        environment.update({
            "MOVE_CANDIDATE_ODB": row["odb"],
            "CANDIDATE_FLOW_VARIANT": variant,
        })
        process = subprocess.run(
            ["bash", str(ROUTE_SCRIPT)], cwd=ROOT, env=environment,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        log_path.write_text(process.stdout)
        result_rows.append({
            "candidate_id": candidate,
            "instance": row["instance"],
            "from_x_dbu": row["from_x_dbu"],
            "from_y_dbu": row["from_y_dbu"],
            "to_x_dbu": row["to_x_dbu"],
            "to_y_dbu": row["to_y_dbu"],
            "route_variant": variant,
            "return_code": process.returncode,
            "route_log": str(log_path),
            "summary": str(ROOT / "build/power" / variant / "summary.txt"),
        })
        print(f"GCD_RELOCATION_ROUTE candidate={candidate} variant={variant} return_code={process.returncode}", flush=True)
        if process.returncode:
            failed = True
            if not args.continue_on_error:
                break

    with args.manifest.open("w", newline="") as output:
        fields = list(result_rows[0])
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(result_rows)
    print(f"GCD_RELOCATION_ROUTE_MANIFEST candidates={len(result_rows)} output={args.manifest}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
