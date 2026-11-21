#!/usr/bin/env python3
"""Check baseline/final GCD netlist equivalence with CTS normalized to input clock."""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
from pathlib import Path

from cl_ovps_sky130_lec_models import CELL_MODEL, PRIMITIVES, primitive_blackbox_stubs

ROOT = Path(__file__).resolve().parents[1]
BASE_NETLIST = ROOT / "build/orfs/results/sky130hd/gcd_power/gcd_baseline20ns/6_final.v"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_clock_and_rename(source: Path, output: Path, module_prefix: str) -> None:
    text = source.read_text(errors="replace")
    module_names = re.findall(r"(?m)^module\s+(\\[^\s(]+|[A-Za-z_$][A-Za-z0-9_$]*)", text)
    if not module_names or "gcd" not in module_names:
        raise ValueError(f"expected GCD module declaration in {source}")
    for index, module in enumerate(module_names):
        new_name = f"gcd_{module_prefix}" if module == "gcd" else f"{module_prefix}_alu"
        text = text.replace(module, new_name)

    clock_instance = re.compile(
        r"(?ms)^[ \t]*sky130_fd_sc_hd__(?:clkbuf|clkinv)_[0-9]+"
        r"\s+[A-Za-z_$][A-Za-z0-9_$]*\s*\(.*?\);[ \t]*(?:\r?\n|$)"
    )
    dummy_instance = re.compile(
        r"(?ms)^[ \t]*sky130_fd_sc_hd__\S+\s+clkload[0-9]+"
        r"\s*\(.*?\);[ \t]*(?:\r?\n|$)"
    )
    text, removed_clock_instances = clock_instance.subn("", text)
    text, removed_dummies = dummy_instance.subn("", text)
    removed_clock_instances += removed_dummies
    tie_pattern = re.compile(
        r"(?ms)^[ \t]*sky130_fd_sc_hd__conb_1\s+[^\s(]+\s*\((.*?)\);[ \t]*(?:\r?\n|$)"
    )

    def replace_tie(match: re.Match[str]) -> str:
        pins = dict(re.findall(r"\.([A-Za-z0-9_$]+)\s*\(\s*([^()]+?)\s*\)", match.group(1)))
        assignments = []
        if "HI" in pins:
            assignments.append(f"  assign {pins['HI']} = 1'b1;")
        if "LO" in pins:
            assignments.append(f"  assign {pins['LO']} = 1'b0;")
        if not assignments:
            raise ValueError(f"could not resolve conb_1 output pins in {source}")
        return "\n".join(assignments) + "\n"

    text, expanded_tie_cells = tie_pattern.subn(replace_tie, text)
    text = re.sub(r"sky130_fd_sc_hd__dfxtp_1(?=\s)", "lec_dfxtp_1", text)
    text = re.sub(r"sky130_fd_sc_hd__edfxtp_1(?=\s)", "lec_edfxtp_1", text)
    text, normalized_clocks = re.subn(r"\.CLK\(\s*[^()]+\s*\)", ".CLK(clk)", text)
    if normalized_clocks == 0:
        raise ValueError(f"no sequential CLK connections found in {source}")
    output.write_text(text)
    print(f"LEC_NORMALIZE source={source.name} top=gcd_{module_prefix} "
          f"clock_pins={normalized_clocks} removed_cts_cells={removed_clock_instances} "
          f"expanded_tie_cells={expanded_tie_cells}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-netlist", type=Path, default=BASE_NETLIST)
    parser.add_argument("--candidate-netlist", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    baseline = args.baseline_netlist.resolve()
    candidate = args.candidate_netlist.resolve()
    output = args.output_dir.resolve()
    if CELL_MODEL is None or PRIMITIVES is None or not CELL_MODEL.is_file() or not PRIMITIVES.is_file():
        raise FileNotFoundError("Sky130HD functional cell models are missing")
    if not baseline.is_file() or not candidate.is_file():
        raise FileNotFoundError(baseline if not baseline.is_file() else candidate)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to overwrite LEC output {output}")
    output.mkdir(parents=True, exist_ok=True)

    base_norm = output / "baseline_normalized.v"
    cand_norm = output / "candidate_normalized.v"
    normalize_clock_and_rename(baseline, base_norm, "gold")
    normalize_clock_and_rename(candidate, cand_norm, "candidate")
    stubs = output / "udp_blackboxes.v"
    stubs.write_text(primitive_blackbox_stubs(PRIMITIVES))
    script = output / "equivalence.ys"
    model_dir = CELL_MODEL.parent
    script.write_text("\n".join((
        f"read_verilog -D FUNCTIONAL -D NO_PRIMITIVES -DUNIT_DELAY= -I{model_dir} {stubs}",
        f"read_verilog -D FUNCTIONAL -D NO_PRIMITIVES -DUNIT_DELAY= -I{model_dir} {CELL_MODEL}",
        f"read_verilog {base_norm}",
        f"read_verilog {cand_norm}",
        "proc gcd_gold",
        "proc gcd_candidate",
        "flatten gcd_gold",
        "proc gcd_gold",
        "flatten gcd_candidate",
        "proc gcd_candidate",
        "equiv_make gcd_gold gcd_candidate gcd_equiv",
        "hierarchy -check -top gcd_equiv",
        "prep -top gcd_equiv",
        "equiv_simple -seq 8",
        "equiv_induct -seq 12",
        "equiv_status -assert",
        "design -reset",
        "",
    )))
    yosys = shutil.which("yosys")
    if not yosys:
        raise FileNotFoundError("yosys is required for GCD LEC")
    process = subprocess.run([yosys, "-Q", "-s", str(script)], cwd=ROOT, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    log = output / "equivalence.log"
    log.write_text(process.stdout)
    (output / "integrity.tsv").write_text(
        "artifact\tpath\tsha256\n" +
        f"baseline_netlist\t{baseline}\t{sha256(baseline)}\n" +
        f"candidate_netlist\t{candidate}\t{sha256(candidate)}\n")
    if process.returncode:
        (output / "status.txt").write_text("LEC_NOT_PROVEN\n")
        (output / "status_detail.txt").write_text(
            "Yosys did not complete a formal proof with the current Sky130 functional-model "
            "abstraction. Inspect equivalence.log; this status alone is not a design mismatch.\n"
        )
        raise RuntimeError(f"Yosys formal proof was not completed; see {log}")
    if "Equivalence successfully proven" not in process.stdout and "Equivalence successfully proven!" not in process.stdout:
        (output / "status.txt").write_text("LEC_NOT_PROVEN\n")
        (output / "status_detail.txt").write_text(
            "Yosys completed without a formal proof result. Inspect equivalence.log.\n"
        )
        raise RuntimeError(f"Yosys did not report a completed proof; see {log}")
    (output / "status.txt").write_text("LEC_PASS\n")
    print(f"GCD LEC PASS: {log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
