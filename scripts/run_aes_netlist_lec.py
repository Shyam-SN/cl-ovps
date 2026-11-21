#!/usr/bin/env python3
"""Prove equivalence of two routed AES netlists with CTS abstracted."""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
from pathlib import Path

from run_gcd_netlist_lec import primitive_blackbox_stubs

ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = next((ROOT / "pdk/volare/sky130/versions").glob(
    "*/sky130B/libs.ref/sky130_fd_sc_hd/verilog/primitives.v"), None)
CELL_MODEL = None if PRIMITIVES is None else PRIMITIVES.with_name("sky130_fd_sc_hd.v")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize(source: Path, output: Path, top: str, side: str) -> int:
    text = source.read_text(errors="replace")
    module = re.compile(rf"(?m)^module\s+{re.escape(top)}(?=\s|\()")
    text, renamed = module.subn(f"module aes_{side}", text, count=1)
    if renamed != 1:
        raise ValueError(f"expected exactly one top module {top} in {source}")

    # CTS changes clock-tree logic and load-only dummy cells. The equivalence
    # objective is the functional data path, so compare sequential cells at a
    # common abstract clock input after removing those physical-only cells.
    # CTS instances in the ORFS netlist use the `clkbuf...` instance prefix;
    # datapath clkinv cells have synthesized `_NNNNN_` names and must remain.
    clock_cell = re.compile(
        r"(?ms)^[ \t]*sky130_fd_sc_hd__(?:clkbuf|clkinv)_[0-9]+"
        r"\s+clkbuf[A-Za-z0-9_$]*\s*\(.*?\);[ \t]*(?:\r?\n|$)"
    )
    dummy_cell = re.compile(
        r"(?ms)^[ \t]*sky130_fd_sc_hd__\S+\s+clkload[0-9]+"
        r"\s*\(.*?\);[ \t]*(?:\r?\n|$)"
    )
    text, removed_clock = clock_cell.subn("", text)
    text, removed_dummy = dummy_cell.subn("", text)

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
            raise ValueError(f"could not resolve conb_1 outputs in {source}")
        return "\n".join(assignments) + "\n"

    text, tie_count = tie_pattern.subn(replace_tie, text)

    # ORFS gate-level Verilog omits physical VPWR/VGND pins. Without tying
    # those omitted pins to the functional supply values, the Sky130 models'
    # power-good UDPs propagate X through otherwise valid logic cones.
    cell_ports: dict[str, set[str]] = {}
    model_text = CELL_MODEL.read_text(errors="replace")
    for cell_match in re.finditer(
        r"(?ms)^module\s+(sky130_fd_sc_hd__[A-Za-z0-9_$]+)\s*\((.*?)\);",
        model_text,
    ):
        cell_ports.setdefault(
            cell_match.group(1),
            set(re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", cell_match.group(2))),
        )
    supply_values = {"VPWR": "1'b1", "VGND": "1'b0", "VPB": "1'b1", "VNB": "1'b0"}
    supply_pin_count = 0
    supply_cell_count = 0

    def connect_omitted_supplies(match: re.Match[str]) -> str:
        nonlocal supply_pin_count, supply_cell_count
        cell_type, body = match.group(2), match.group(5)
        if cell_type not in cell_ports:
            return match.group(0)
        connected = set(re.findall(r"\.([A-Za-z0-9_$]+)\s*\(", body))
        missing = [pin for pin, value in supply_values.items()
                   if pin in cell_ports[cell_type] and pin not in connected]
        if not missing:
            return match.group(0)
        supply_pin_count += len(missing)
        supply_cell_count += 1
        instance = match.group(4)
        separator = " " if instance.startswith("\\") else ""
        prefix = match.group(1) + cell_type + match.group(3) + instance + separator + "("
        body = body.rstrip()
        additions = ",\n".join(f"    .{pin}({supply_values[pin]})" for pin in missing)
        return f"{prefix}{body},\n{additions});"

    cell_instance = re.compile(
        r"(?ms)^([ \t]*)(sky130_fd_sc_hd__[A-Za-z0-9_$]+)([ \t]+)"
        r"([^\s(]+)\s*\((.*?)\);"
    )
    text = cell_instance.sub(connect_omitted_supplies, text)
    text, dfxtp_count = re.subn(r"sky130_fd_sc_hd__dfxtp_(?:1|2|4)(?=\s)", "lec_dfxtp_1", text)
    text, edfxtp_count = re.subn(r"sky130_fd_sc_hd__edfxtp_1(?=\s)", "lec_edfxtp_1", text)
    text, clock_pins = re.subn(r"\.CLK\(\s*[^()]+\s*\)", ".CLK(clk)", text)
    if clock_pins == 0:
        raise ValueError(f"no sequential CLK pins found in {source}")
    output.write_text(text)
    print(f"AES_LEC_NORMALIZE side={side} clock_pins={clock_pins} removed_cts={removed_clock} "
          f"removed_dummies={removed_dummy} tie_cells={tie_count} "
          f"ideal_supply_pins={supply_pin_count} supply_cells={supply_cell_count} "
          f"dfxtp={dfxtp_count} edfxtp={edfxtp_count}")
    return clock_pins


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-netlist", type=Path, required=True)
    parser.add_argument("--candidate-netlist", type=Path, required=True)
    parser.add_argument("--top", default="aes_cipher_top")
    parser.add_argument("--induction-depth", type=int, default=32)
    parser.add_argument("--match-instance-names", action="store_true",
                        help="also match internal gate/$_ names when constructing equivalence points")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    baseline, candidate, output = (args.baseline_netlist.resolve(), args.candidate_netlist.resolve(),
                                   args.output_dir.resolve())
    if CELL_MODEL is None or PRIMITIVES is None or not CELL_MODEL.is_file() or not PRIMITIVES.is_file():
        raise FileNotFoundError("Sky130HD functional cell models are missing")
    for path in (baseline, candidate):
        if not path.is_file():
            raise FileNotFoundError(path)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to overwrite LEC output {output}")
    output.mkdir(parents=True, exist_ok=True)

    base_norm, candidate_norm = output / "baseline_normalized.v", output / "candidate_normalized.v"
    baseline_clocks = normalize(baseline, base_norm, args.top, "gold")
    candidate_clocks = normalize(candidate, candidate_norm, args.top, "candidate")
    if baseline_clocks != candidate_clocks:
        raise RuntimeError(f"sequential clock-pin counts differ: {baseline_clocks} vs {candidate_clocks}")

    stubs = output / "udp_blackboxes.v"
    stubs.write_text(primitive_blackbox_stubs(PRIMITIVES))
    model_dir = CELL_MODEL.parent
    script = output / "equivalence.ys"
    script.write_text("\n".join((
        f"read_verilog -D FUNCTIONAL -D NO_PRIMITIVES -DUNIT_DELAY= -I{model_dir} {stubs}",
        f"read_verilog -D FUNCTIONAL -D NO_PRIMITIVES -DUNIT_DELAY= -I{model_dir} {CELL_MODEL}",
        f"read_verilog {base_norm}",
        f"read_verilog {candidate_norm}",
        "proc aes_gold",
        "proc aes_candidate",
        "flatten aes_gold",
        "proc aes_gold",
        "flatten aes_candidate",
        "proc aes_candidate",
        "equiv_make " + ("-inames " if args.match_instance_names else "") +
        "aes_gold aes_candidate aes_equiv",
        "hierarchy -check -top aes_equiv",
        "prep -top aes_equiv",
        "equiv_simple -seq 1 -short",
        f"equiv_induct -seq {args.induction_depth}",
        "equiv_status -assert",
        "design -reset",
        "",
    )))
    yosys = shutil.which("yosys")
    if not yosys:
        raise FileNotFoundError("Yosys is required for AES LEC")
    result = subprocess.run([yosys, "-Q", "-s", str(script)], cwd=ROOT, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    log = output / "equivalence.log"
    log.write_text(result.stdout)
    (output / "integrity.tsv").write_text(
        "artifact\tpath\tsha256\n" +
        f"baseline_netlist\t{baseline}\t{sha256(baseline)}\n" +
        f"candidate_netlist\t{candidate}\t{sha256(candidate)}\n")
    passed = (result.returncode == 0 and "Equivalence successfully proven" in result.stdout)
    (output / "status.txt").write_text("LEC_PASS\n" if passed else "LEC_NOT_PROVEN\n")
    if not passed:
        raise RuntimeError(f"AES formal proof did not complete; inspect {log}")
    print(f"AES LEC PASS: {log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
