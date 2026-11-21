#!/usr/bin/env python3
"""Design-dispatching entry point for the CL-OVPS multi-stage flow.

The AES branch uses the frozen total-clock-cell CTS oracle, routes the selected
placement, applies clock restructuring and VCD-guided transforms, then performs
three-workload SPEF sign-off.  The GCD branch runs the established frozen-pool
optimizer.  For both designs the SDC must match the project's locked benchmark
constraints; arbitrary constraint changes are rejected to keep comparisons valid.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCKED_SDC = {
    "aes": ROOT / "designs/aes/flow/constraint_3p8.sdc",
    "gcd": ROOT / "designs/gcd/flow/constraints/gcd.sdc",
}
LOCKED_PERIOD_NS = {"aes": 3.8, "gcd": 20.0}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_sdc(design: str, supplied: Path) -> Path:
    supplied = supplied.resolve()
    expected = LOCKED_SDC[design]
    if not supplied.is_file():
        raise FileNotFoundError(supplied)
    if not expected.is_file():
        raise FileNotFoundError(expected)
    if sha256(supplied) != sha256(expected):
        text = supplied.read_text(errors="replace")
        period = re.search(r"(?m)^\s*set\s+clk_period\s+([0-9.eE+-]+)\s*$", text)
        actual = float(period.group(1)) if period else None
        raise ValueError(
            f"{design} requires the locked {LOCKED_PERIOD_NS[design]:g} ns SDC "
            f"({expected}); supplied SDC SHA differs (parsed period={actual})")
    return supplied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design", required=True, choices=("gcd", "aes"))
    parser.add_argument("--sdc", required=True, type=Path,
                        help="must be byte-identical to the locked benchmark SDC")
    parser.add_argument("--vcd", type=Path,
                        help="optional activity trace for provenance; sign-off regenerates gate-level VCDs")
    parser.add_argument("--baseline-tag", help="routed baseline variant (defaults to the locked benchmark)")
    parser.add_argument("--tier1-experiment", type=Path,
                        help="AES frozen CTS-oracle search directory; omit to run a fresh search")
    parser.add_argument("--candidate-pool", type=Path,
                        help="optional frozen relocation pool (AES or GCD)")
    parser.add_argument("--setup-margin", type=float,
                        help="AES OpenROAD setup repair margin in ns; baseline-tag must use it too")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cores", type=int, default=4)
    parser.add_argument("--validate-only", action="store_true",
                        help="validate locked SDC/VCD inputs without launching the long physical flow")
    args = parser.parse_args()

    sdc = validate_sdc(args.design, args.sdc)
    if args.vcd is not None and not args.vcd.resolve().is_file():
        raise FileNotFoundError(args.vcd.resolve())
    if args.validate_only:
        print(f"CL_OVPS_INPUTS_PASS design={args.design} period_ns={LOCKED_PERIOD_NS[args.design]} "
              f"sdc_sha256={sha256(sdc)} vcd={'provided' if args.vcd else 'gate-level-generated'}")
        return 0
    output = args.output_dir.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    output.mkdir(parents=True)
    provenance = ["artifact\tpath\tsha256\n", f"locked_sdc\t{sdc}\t{sha256(sdc)}\n"]
    if args.vcd is not None:
        vcd = args.vcd.resolve()
        provenance.append(f"reference_vcd\t{vcd}\t{sha256(vcd)}\n")
    (output / "input_integrity.tsv").write_text("".join(provenance))

    if args.design == "aes":
        baseline = args.baseline_tag or "aes_powerbase38_20261101a"
        search_dir = args.tier1_experiment.resolve() if args.tier1_experiment else output / "tier1_search"
        if args.tier1_experiment is None:
            search_cmd = [sys.executable, str(ROOT / "scripts/run_aes_tier1_powersearch.py"),
                          "--base-tag", baseline, "--variant-prefix", f"{output.name}_xrow",
                          "--config", str(ROOT / "designs/aes/flow/config.mk"),
                          "--output-dir", str(search_dir), "--cores", str(args.cores),
                          "--candidate-sinks", "40", "--moves-per-sink", "2"]
            if args.candidate_pool:
                search_cmd += ["--candidate-pool", str(args.candidate_pool.resolve())]
            subprocess.run(search_cmd, cwd=ROOT, check=True)
        cmd = [sys.executable, str(ROOT / "scripts/run_aes_multistage_pipeline.py"),
               "--baseline-tag", baseline, "--tier1-experiment", str(search_dir),
               "--output-dir", str(output / "routed_pipeline"), "--cores", str(args.cores)]
        if args.setup_margin is not None:
            cmd += ["--setup-margin", str(args.setup_margin)]
    else:
        if args.setup_margin is not None:
            raise ValueError("--setup-margin is currently supported only for AES")
        baseline = args.baseline_tag or "gcd_baseline20ns"
        if baseline != "gcd_baseline20ns":
            raise ValueError("the current GCD pipeline is locked to gcd_baseline20ns")
        cmd = [sys.executable, str(ROOT / "scripts/run_gcd_multistage_optimizer.py"),
               "--output-tag", f"{output.name}_gcd", "--num-cores", str(args.cores)]
        if args.candidate_pool:
            cmd += ["--candidate-pool", str(args.candidate_pool.resolve())]
        (output / "result_location.txt").write_text(
            str(ROOT / "build/power" / f"{output.name}_gcd") + "\n")
    print(f"CL_OVPS design={args.design} baseline={baseline} sdc_sha256={sha256(sdc)}", flush=True)
    print("CL_OVPS command=" + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)
    print(f"CL_OVPS complete output={output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
