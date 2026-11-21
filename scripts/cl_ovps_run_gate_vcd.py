#!/usr/bin/env python3
"""Run a design's existing gate-level activity harness on a routed netlist."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", required=True, type=Path,
                        help="design-specific run_activity.sh harness")
    parser.add_argument("--netlist", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    for path in (args.script, args.netlist):
        if not path.is_file():
            parser.error(f"required gate-level activity input is missing: {path}")
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f"refusing to overwrite non-empty VCD directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["GATELEVEL_NETLIST"] = str(args.netlist.resolve())
    env["ACTIVITY_OUT_DIR"] = str(output)
    result = subprocess.run(["bash", str(args.script.resolve())], env=env,
                            text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    (output / "activity_harness.log").write_text(result.stdout)
    if result.returncode:
        print(result.stdout, end="")
        return result.returncode
    missing = [name for name in ("idle.vcd", "typical.vcd", "peak.vcd")
               if not (output / name).is_file()]
    if missing:
        raise SystemExit(f"activity harness omitted workload VCDs: {missing}")
    print(f"CL_OVPS_GATE_VCD status=PASS netlist={args.netlist.resolve()} output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
