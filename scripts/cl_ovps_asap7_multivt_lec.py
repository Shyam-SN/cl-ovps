#!/usr/bin/env python3
"""Prove Boolean equivalence of ASAP7 baseline and multi-Vt gate netlists.

The verified Pass 3A families preserve cell functions and pin signatures.
Before Yosys equivalence, this adapter canonicalizes only the master VT suffix
to RVT, so corresponding standard-cell instances remain structurally matched.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = ROOT / "tools/openroad-flow-scripts/flow/platforms/asap7/lib/NLDM"
LIBRARIES = [
    LIB_DIR / "asap7sc7p5t_AO_RVT_TT_nldm_211120.lib.gz",
    LIB_DIR / "asap7sc7p5t_INVBUF_RVT_TT_nldm_220122.lib.gz",
    LIB_DIR / "asap7sc7p5t_OA_RVT_TT_nldm_211120.lib.gz",
    LIB_DIR / "asap7sc7p5t_SEQ_RVT_TT_nldm_220123.lib",
    LIB_DIR / "asap7sc7p5t_SIMPLE_RVT_TT_nldm_211120.lib.gz",
]
VT_SUFFIX = re.compile(r"_ASAP7_75t_(?:SL|L)\b")


def normalize_netlist(source: Path, target: Path, module_name: str) -> int:
    text = source.read_text(errors="replace")
    text, changed = re.subn(r"(?m)^module\s+gcd\s*\(", f"module {module_name} (", text, count=1)
    if changed != 1:
        raise SystemExit(f"could not locate top module gcd in {source}")
    return target.write_text(VT_SUFFIX.sub("_ASAP7_75t_R", text))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-netlist", type=Path, required=True)
    parser.add_argument("--candidate-netlist", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--yosys", default="yosys")
    args = parser.parse_args()
    for path in (args.baseline_netlist, args.candidate_netlist, *LIBRARIES):
        if not path.is_file():
            raise SystemExit(f"missing LEC input: {path}")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    baseline = output / "baseline_vt_normalized.v"
    candidate = output / "candidate_vt_normalized.v"
    normalize_netlist(args.baseline_netlist, baseline, "gcd_baseline")
    normalize_netlist(args.candidate_netlist, candidate, "gcd_candidate")
    script = output / "asap7_multivt_equiv.ys"
    lines = [f'read_liberty -lib "{p.resolve()}"' for p in LIBRARIES]
    lines += [f'read_verilog "{baseline}"', f'read_verilog "{candidate}"',
              "equiv_make gcd_baseline gcd_candidate equiv",
              "hierarchy -check -top equiv", "proc", "flatten", "opt_clean",
              "equiv_simple -seq 1", "equiv_induct -seq 1", "equiv_status -assert"]
    script.write_text("\n".join(lines) + "\n")
    log = output / "asap7_multivt_equiv.log"
    result = subprocess.run([args.yosys, "-s", str(script)], cwd=ROOT, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    log.write_text(result.stdout)
    if result.returncode:
        raise SystemExit(f"ASAP7 Yosys LEC failed ({result.returncode}); inspect {log}")
    print(f"ASAP7_MULTIVT_LEC_PASS log={log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
