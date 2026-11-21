#!/usr/bin/env python3
"""Liberty-gated sequential master trial pass (CL-OVPS Stage 3C).

The pass forms replacement pairs only from identical Liberty sequential
semantic classes and non-increasing CLK capacitance. OpenSTA then checks the
instance's actual VCD-driven internal power and the complete design dynamic
power after each in-memory trial. Unsupported measurements fail closed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from cl_ovps_liberty_audit import LibertyParser, consolidate_attributes, sequential_rows, walk, unquote

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
TCL = ROOT / "scripts/stage3c_seq_sizing.tcl"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_audit(liberty: Path, out: Path) -> list[dict[str, object]]:
    text = liberty.read_text(errors="replace")
    tree = LibertyParser(text).parse()
    for node in tree:
        consolidate_attributes(node)
    cells = list(walk(tree, "cell"))
    rows = sequential_rows(cells, liberty.resolve())
    columns = ("master", "family", "drive_strength", "equivalence_class", "cell_footprint",
               "pin_list", "sequential_semantics", "clock_pins", "scan_pins",
               "pin_capacitance", "clock_pin_capacitance", "internal_power_tables")
    with out.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in sorted(rows, key=lambda item: (str(item["family"]), item["drive_strength"] or 0)):
            writer.writerow({key: json.dumps(row.get(key), sort_keys=True, separators=(",", ":"))
                             if isinstance(row.get(key), (dict, list)) else row.get(key, "")
                             for key in columns})
    return rows


def make_pairs(rows: list[dict[str, object]], out: Path) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row["equivalence_class"]), []).append(row)
    pairs: list[dict[str, object]] = []
    for members in grouped.values():
        for source in members:
            source_strength = source.get("drive_strength")
            source_clock_caps = source.get("clock_pin_capacitance", {})
            if source_strength is None or not source_clock_caps:
                continue
            for target in members:
                target_strength = target.get("drive_strength")
                target_clock_caps = target.get("clock_pin_capacitance", {})
                if target_strength is None or not target_clock_caps:
                    continue
                if set(target_clock_caps) != set(source_clock_caps):
                    continue
                if int(target_strength) >= int(source_strength):
                    continue
                # Require non-increasing capacitance on every clock pin, including
                # inverted and non-standard clock pin names such as CLK_N.
                if any(
                    target_clock_caps[pin].get("capacitance") is None
                    or source_clock_caps[pin].get("capacitance") is None
                    or float(target_clock_caps[pin]["capacitance"])
                    > float(source_clock_caps[pin]["capacitance"]) + 1e-15
                    for pin in source_clock_caps
                ):
                    continue
                source_clk = max(float(item["capacitance"]) for item in source_clock_caps.values())
                target_clk = max(float(item["capacitance"]) for item in target_clock_caps.values())
                pairs.append({"source": source["master"], "target": target["master"],
                              "equivalence_class": source["equivalence_class"],
                              "source_strength": source_strength, "target_strength": target_strength,
                              "source_clk_cap_fF": float(source_clk) * 1000.0,
                              "target_clk_cap_fF": float(target_clk) * 1000.0,
                              "source_clock_pin_capacitance": source_clock_caps,
                              "target_clock_pin_capacitance": target_clock_caps})
    pairs.sort(key=lambda item: (str(item["source"]), int(item["target_strength"])))
    with out.open("w") as stream:
        stream.write("source\ttarget\tequivalence_class\tsource_strength\ttarget_strength\t"
                     "source_clk_cap_fF\ttarget_clk_cap_fF\tsource_clock_pin_capacitance\t"
                     "target_clock_pin_capacitance\n")
        for pair in pairs:
            stream.write("\t".join(str(pair[key]) for key in (
                "source", "target", "equivalence_class", "source_strength", "target_strength",
                "source_clk_cap_fF", "target_clk_cap_fF")) + "\t" +
                json.dumps(pair["source_clock_pin_capacitance"], sort_keys=True, separators=(",", ":")) + "\t" +
                json.dumps(pair["target_clock_pin_capacitance"], sort_keys=True, separators=(",", ":")) + "\n")
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-odb", required=True, type=Path)
    parser.add_argument("--sdc", required=True, type=Path)
    parser.add_argument("--vcd", required=True, type=Path)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--lib", required=True, type=Path)
    parser.add_argument("--rc-tcl", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--upf", type=Path)
    parser.add_argument("--openroad", type=Path, default=DEFAULT_OPENROAD)
    parser.add_argument("--setup-floor-ps", type=float, default=0.0)
    parser.add_argument("--timing-guard-ps", type=float, default=50.0)
    args = parser.parse_args()
    for path in (args.input_odb, args.sdc, args.vcd, args.lib, args.rc_tcl, args.openroad):
        if not path.is_file():
            raise SystemExit(f"missing input file: {path}")
    if args.upf and not args.upf.is_file():
        raise SystemExit(f"missing UPF file: {args.upf}")
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"refusing to overwrite non-empty pass output: {output}")
    output.mkdir(parents=True, exist_ok=True)

    audit_path = output / "stage3c_liberty_sequential_audit.tsv"
    pair_path = output / "stage3c_replacement_pairs.tsv"
    output_odb = output / "stage3c.odb"
    report = output / "stage3c.tsv"
    log = output / "stage3c.log"
    rows = write_audit(args.lib, audit_path)
    pairs = make_pairs(rows, pair_path)
    env = os.environ.copy()
    env.update({
        "PASS3C_INPUT_ODB": str(args.input_odb.resolve()),
        "PASS3C_SDC": str(args.sdc.resolve()),
        "PASS3C_VCD": str(args.vcd.resolve()),
        "PASS3C_SCOPE": args.scope,
        "PASS3C_LIB": str(args.lib.resolve()),
        "PASS3C_RC_TCL": str(args.rc_tcl.resolve()),
        "PASS3C_UPF": str(args.upf.resolve()) if args.upf else "",
        "PASS3C_PAIR_TSV": str(pair_path),
        "PASS3C_OUTPUT_ODB": str(output_odb),
        "PASS3C_REPORT": str(report),
        "PASS3C_SETUP_FLOOR_PS": str(args.setup_floor_ps),
        "PASS3C_TIMING_GUARD_PS": str(args.timing_guard_ps),
    })
    run = subprocess.run([str(args.openroad.resolve()), "-no_init", "-exit", str(TCL)], cwd=ROOT,
                         env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         check=False)
    log.write_text(run.stdout)
    if run.returncode:
        raise SystemExit(f"Pass 3C failed closed; inspect {log}")
    if not output_odb.is_file() or not report.is_file():
        raise SystemExit("Pass 3C did not emit its ODB and candidate/rejection report")
    accepted = len(re.findall(r"(?m)^PASS3C_ACCEPT\b", run.stdout))
    rejected = len(re.findall(r"(?m)^PASS3C_REJECT\b", run.stdout))
    candidates = len(re.findall(r"(?m)^PASS3C_CANDIDATE\b", run.stdout))
    status_match = re.search(r"(?m)^PASS3C_STATUS\s+(\S+)", run.stdout)
    if not status_match:
        raise SystemExit("Pass 3C log omitted its final status marker")
    manifest = {
        "schema_version": 1,
        "pass": "3C",
        "name": "Liberty-verified sequential-cell downsizing",
        "status": status_match.group(1),
        "input": {"odb": str(args.input_odb.resolve()), "sha256": sha256(args.input_odb),
                  "sdc": str(args.sdc.resolve()), "vcd": str(args.vcd.resolve()),
                  "vcd_sha256": sha256(args.vcd), "scope": args.scope,
                  "liberty": str(args.lib.resolve()), "liberty_sha256": sha256(args.lib)},
        "eligibility": {"sequential_masters_audited": len(rows),
                        "cap_admissible_master_pairs": len(pairs),
                        "instance_candidates": candidates, "accepted_trials": accepted,
                        "rejected_trials": rejected,
                        "selection": "exact semantic class; target strength lower; every Liberty clock/gate pin capacitance non-increasing; OpenSTA instance internal power and full-design dynamic trial required"},
        "outputs": {"odb": {"path": str(output_odb), "sha256": sha256(output_odb)},
                    "report": {"path": str(report), "sha256": sha256(report)},
                    "log": {"path": str(log), "sha256": sha256(log)},
                    "liberty_audit": {"path": str(audit_path), "sha256": sha256(audit_path)},
                    "replacement_pairs": {"path": str(pair_path), "sha256": sha256(pair_path)}},
        "phase1_preserved": True,
    }
    (output / "stage3c_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"PASS3C_RESULT status={manifest['status']} candidates={candidates} accepted={accepted} manifest={output / 'stage3c_manifest.json'}")
    return 0 if manifest["status"] in {"PASS", "NO_CANDIDATES"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
