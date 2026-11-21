#!/usr/bin/env python3
"""Build paired activity VCDs and differentially simulate routed ASAP7 GCD netlists.

The PDK ships functional RVT Verilog models, but not duplicate LVT/SLVT logic
models. Since Pass 3A changes threshold only and its family audit proves the
Boolean/pin signature is identical, this harness canonicalizes only the cell
master VT suffix to the RVT functional model for logic simulation. OpenSTA
power is still measured on the actual RVT/LVT/SLVT ODB and its Liberty tables.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STD = ROOT / "tools/openroad-flow-scripts/flow/platforms/asap7/verilog/stdcell"
MODELS = [
    STD / "asap7sc7p5t_AO_RVT_TT_201020.v",
    STD / "asap7sc7p5t_INVBUF_RVT_TT_201020.v",
    STD / "asap7sc7p5t_OA_RVT_TT_201020.v",
    STD / "asap7sc7p5t_SEQ_RVT_TT_220101.v",
    STD / "asap7sc7p5t_SIMPLE_RVT_TT_201020.v",
]
VT_SUFFIX = re.compile(r"_ASAP7_75t_(?:SL|L)\b")


def canonical_netlist(source: Path, target: Path, module: str) -> None:
    text = source.read_text(errors="replace")
    text, count = re.subn(r"(?m)^module\s+gcd\s*\(", f"module {module} (", text, count=1)
    if count != 1:
        raise SystemExit(f"could not rename top module gcd in {source}")
    target.write_text(VT_SUFFIX.sub("_ASAP7_75t_R", text))


def run(command: list[str], cwd: Path) -> None:
    print("+", " ".join(command), flush=True)
    result = subprocess.run(command, cwd=cwd, check=False)
    if result.returncode:
        raise SystemExit(result.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-netlist", type=Path, required=True)
    parser.add_argument("--candidate-netlist", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--iverilog", default="iverilog")
    parser.add_argument("--vvp", default="vvp")
    args = parser.parse_args()
    for path in [args.baseline_netlist, args.candidate_netlist, *MODELS]:
        if not path.is_file():
            raise SystemExit(f"missing gate-simulation input: {path}")
    out = args.output_dir.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"refusing to overwrite non-empty activity output directory: {out}")
    out.mkdir(parents=True, exist_ok=True)
    canonical = out / "canonical_netlists"
    canonical.mkdir()
    base = canonical / "baseline_functional.v"
    candidate = canonical / "candidate_functional.v"
    canonical_netlist(args.baseline_netlist.resolve(), base, "gcd_base")
    canonical_netlist(args.candidate_netlist.resolve(), candidate, "gcd_candidate")

    sim = out / "gcd_multivt_activity.vvp"
    # Do not enable specify timing checks here. Icarus does not implement the
    # ASAP7 sequential models' edge-sensitive `ifnone` checks; logic remains
    # modeled by the functional UDPs in the SIMPLE and SEQ files.
    run([args.iverilog, "-g2012", "-s", "tb", "-o", str(sim),
         str(ROOT / "scripts/asap7_gcd_multivt_activity_tb.v"),
         str(base), str(candidate), *map(str, MODELS)], ROOT)

    combined = out / "combined"
    baseline_dir = out / "baseline"
    candidate_dir = out / "candidate"
    for directory in (combined, baseline_dir, candidate_dir):
        directory.mkdir()
    for workload in ("idle", "typical", "peak"):
        vcd = combined / f"{workload}.vcd"
        run([args.vvp, str(sim), f"+workload={workload}", f"+vcd={vcd}"], ROOT)
        shutil.copy2(vcd, baseline_dir / vcd.name)
        shutil.copy2(vcd, candidate_dir / vcd.name)
    print(f"ACTIVITY_VCDS_PASS output={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
