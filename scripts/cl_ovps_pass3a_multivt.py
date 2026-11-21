#!/usr/bin/env python3
"""Audit and trial same-footprint ASAP7 multi-Vt master swaps.

The ASAP7 PDK names threshold variants with ``_ASAP7_75t_R``, ``_L`` and
``_SL`` suffixes (RVT, LVT and SLVT). This pass does not infer equivalence
from names alone: it checks Liberty function/pin signatures and LEF pin
geometry before it offers any pair to the in-memory OpenROAD trial engine.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
TCL = ROOT / "scripts/stage3a_multivt.tcl"
VT_TO_NAME = {"R": "RVT", "L": "LVT", "SL": "SLVT"}
VT_RANK = {"SL": 0, "L": 1, "R": 2}
VT_SUFFIX = re.compile(r"_ASAP7_75t_(SL|R|L)$")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", errors="replace") as f:
            return f.read()
    return path.read_text(errors="replace")


def strip_comments(text: str) -> str:
    """Remove Liberty // and /* */ comments without touching quoted strings."""
    out: list[str] = []
    i = 0
    in_string = False
    escaped = False
    while i < len(text):
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            if end < 0:
                break
            out.append("\n")
            i = end + 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                break
            out.append(" " * (end + 2 - i))
            i = end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def matching_brace(text: str, opening: int) -> int:
    depth = 0
    in_string = False
    escaped = False
    for i in range(opening, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unterminated Liberty group")


def group_blocks(text: str, group: str) -> Iterator[tuple[str, str]]:
    pattern = re.compile(rf"\b{re.escape(group)}\s*\(\s*(\"(?:\\.|[^\"\\])*\"|[^)]*)\s*\)\s*\{{")
    for match in pattern.finditer(text):
        brace = text.find("{", match.start(), match.end())
        end = matching_brace(text, brace)
        arg = match.group(1).strip()
        if arg.startswith('"') and arg.endswith('"'):
            arg = bytes(arg[1:-1], "utf-8").decode("unicode_escape")
        yield arg, text[brace + 1:end]


def attr(body: str, name: str) -> str | None:
    match = re.search(rf"\b{re.escape(name)}\s*:\s*(\"(?:\\.|[^\"\\])*\"|[^;]+)\s*;", body)
    if not match:
        return None
    value = match.group(1).strip()
    if value.startswith('"') and value.endswith('"'):
        value = bytes(value[1:-1], "utf-8").decode("unicode_escape")
    return value


def call_arg(body: str, name: str) -> str | None:
    match = re.search(rf"\b{re.escape(name)}\s*\(\s*(\"(?:\\.|[^\"\\])*\"|[^)]*)\s*\)\s*;", body, re.S)
    if not match:
        return None
    value = match.group(1).strip()
    if value.startswith('"') and value.endswith('"'):
        value = bytes(value[1:-1], "utf-8").decode("unicode_escape")
    return value


def parse_liberty_cells(path: Path) -> list[dict[str, Any]]:
    text = strip_comments(read_text(path))
    cells: list[dict[str, Any]] = []
    for name, body in group_blocks(text, "cell"):
        vt_match = VT_SUFFIX.search(name)
        if not vt_match:
            continue
        vt = vt_match.group(1)
        family = name[:vt_match.start()]
        pins: dict[str, Any] = {}
        internal_tables: list[dict[str, Any]] = []
        for pin_name, pin_body in group_blocks(body, "pin"):
            cap = attr(pin_body, "capacitance")
            pins[pin_name] = {
                "direction": attr(pin_body, "direction"),
                "function": attr(pin_body, "function"),
                "capacitance": cap,
            }
            for _power_arg, power_body in group_blocks(pin_body, "internal_power"):
                record: dict[str, Any] = {
                    "pin": pin_name,
                    "related_pin": attr(power_body, "related_pin"),
                    "related_pg_pin": attr(power_body, "related_pg_pin"),
                    "when": attr(power_body, "when"),
                    "tables": [],
                }
                for table_kind in ("rise_power", "fall_power"):
                    for template, table_body in group_blocks(power_body, table_kind):
                        value_groups = list(re.finditer(r"\bvalues\s*\((.*?)\)\s*;", table_body, re.S))
                        raw_values = [v.strip() for group in value_groups
                                      for v in re.findall(r'"((?:\\.|[^"\\])*)"', group.group(1), re.S)]
                        record["tables"].append({
                            "transition": table_kind,
                            "template": template.strip().strip('"'),
                            "index_1": call_arg(table_body, "index_1"),
                            "index_2": call_arg(table_body, "index_2"),
                            "values": raw_values,
                        })
                internal_tables.append(record)
        seq: list[dict[str, Any]] = []
        for seq_kind in ("ff", "latch", "ff_bank", "latch_bank"):
            for args, seq_body in group_blocks(body, seq_kind):
                seq.append({
                    "kind": seq_kind,
                    "args": re.sub(r"\s+", "", args),
                    "next_state": attr(seq_body, "next_state"),
                    "clocked_on": attr(seq_body, "clocked_on"),
                    "clear": attr(seq_body, "clear"),
                    "preset": attr(seq_body, "preset"),
                    "clear_preset_var1": attr(seq_body, "clear_preset_var1"),
                    "clear_preset_var2": attr(seq_body, "clear_preset_var2"),
                })
        leakage = attr(body, "cell_leakage_power")
        leakage_power_groups = []
        for _arg, leak_body in group_blocks(body, "leakage_power"):
            leakage_power_groups.append({
                "when": attr(leak_body, "when"),
                "value": attr(leak_body, "value"),
                "related_pg_pin": attr(leak_body, "related_pg_pin"),
            })
        cells.append({
            "master": name,
            "family": family,
            "vt": vt,
            "area": attr(body, "area"),
            "cell_leakage_power": leakage,
            "leakage_power_groups": leakage_power_groups,
            "pins": pins,
            "internal_power_tables": internal_tables,
            "sequential_signature": seq,
            "library": str(path.resolve()),
        })
    return cells


def parse_lef(path: Path) -> dict[str, dict[str, Any]]:
    text = path.read_text(errors="replace")
    out: dict[str, dict[str, Any]] = {}
    macro_re = re.compile(r"(?m)^\s*MACRO\s+(\S+)\s*$")
    for match in macro_re.finditer(text):
        name = match.group(1)
        end_match = re.search(rf"(?m)^\s*END\s+{re.escape(name)}\s*$", text[match.end():])
        if not end_match:
            continue
        start = match.end()
        body = text[start:start + end_match.start()]
        size = re.search(r"(?m)^\s*SIZE\s+([^;]+);", body)
        origin = re.search(r"(?m)^\s*ORIGIN\s+([^;]+);", body)
        symmetry = re.search(r"(?m)^\s*SYMMETRY\s+([^;]+);", body)
        site = re.search(r"(?m)^\s*SITE\s+(\S+)\s*;", body)
        pin_shapes: dict[str, list[str]] = {}
        pin_re = re.compile(r"(?ms)^\s*PIN\s+(\S+)\s*\n(.*?)^\s*END\s+\1\s*$")
        for pin_match in pin_re.finditer(body):
            pin_name, pin_body = pin_match.group(1), pin_match.group(2)
            entries: list[str] = []
            for line in pin_body.splitlines():
                stripped = line.strip()
                if not stripped or stripped == "PORT" or stripped == "END":
                    if stripped == "PORT":
                        entries.append("PORT")
                    continue
                if re.match(r"(?:LAYER|RECT|POLYGON|PATH|VIA|WIDTH|SPACING|DESIGNRULEWIDTH|DIRECTION|USE|ITERATE)\b", stripped):
                    if re.match(r"LAYER\s+(?:RVTN|RVTP|LVTN|LVTP|SLVTN|SLVTP)\b", stripped):
                        continue
                    entries.append(re.sub(r"\s+", " ", stripped))
            pin_shapes[pin_name] = entries
        normalized_name = VT_SUFFIX.sub("_ASAP7_75t_VT", name)
        sig = {
            "class": re.search(r"(?m)^\s*CLASS\s+([^;]+);", body).group(1).strip() if re.search(r"(?m)^\s*CLASS\s+([^;]+);", body) else None,
            "origin": re.sub(r"\s+", " ", origin.group(1).strip()) if origin else None,
            "size": re.sub(r"\s+", " ", size.group(1).strip()) if size else None,
            "symmetry": re.sub(r"\s+", " ", symmetry.group(1).strip()) if symmetry else None,
            "site": site.group(1) if site else None,
            "pins": pin_shapes,
        }
        canonical = json.dumps(sig, sort_keys=True, separators=(",", ":"))
        out[normalized_name] = {"signature": hashlib.sha256(canonical.encode()).hexdigest(),
                                "geometry": sig, "master": name, "lef": str(path.resolve())}
    return out


def load_lef_inventory(paths: list[Path]) -> dict[str, dict[str, Any]]:
    inventory: dict[str, dict[str, Any]] = {}
    for path in paths:
        vt_match = re.search(r"(?:^|_)28_(R|L|SL)_", path.name)
        if not vt_match:
            raise ValueError(f"cannot determine ASAP7 VT tag from LEF filename: {path}")
        vt = vt_match.group(1)
        for canonical, data in parse_lef(path).items():
            row = inventory.setdefault(canonical, {})
            row[vt] = data
    return inventory


def write_audit(libs: list[Path], lefs: list[Path], output_dir: Path) -> tuple[int, int]:
    cells: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for lib in libs:
        for cell in parse_liberty_cells(lib):
            family = cell["family"]
            vt = cell["vt"]
            if vt in cells[family]:
                prior = cells[family][vt]
                raise ValueError(f"duplicate Liberty master {cell['master']} in {prior['library']} and {lib}")
            cells[family][vt] = cell
    lef_inventory = load_lef_inventory(lefs)
    expected_vts = {"R", "L", "SL"}
    family_info: dict[str, dict[str, Any]] = {}
    complete_count = 0
    pair_rows: list[dict[str, Any]] = []
    audit_path = output_dir / "stage3a_multivt_family_audit.tsv"
    fields = ["family", "vt", "master", "area", "cell_leakage_power", "leakage_power_groups_json",
              "pin_capacitances_json", "pin_functions_json", "internal_power_tables_json", "sequential_signature_json",
              "library", "lef_signature", "logical_signature", "complete_three_vt_family",
              "same_footprint_and_pin_map", "functionally_equivalent"]
    with audit_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for family in sorted(cells):
            variants = cells[family]
            canonical_lef = family + "_ASAP7_75t_VT"
            physical = lef_inventory.get(canonical_lef, {})
            lef_signatures = {vt: physical.get(vt, {}).get("signature") for vt in variants}
            logical_signatures = {}
            for vt, cell in variants.items():
                logic = {
                    pin: {"direction": info.get("direction"), "function": info.get("function")}
                    for pin, info in sorted(cell["pins"].items())
                }
                logic_sig = {"pins": logic, "sequential": cell["sequential_signature"]}
                logical_signatures[vt] = hashlib.sha256(
                    json.dumps(logic_sig, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            complete = expected_vts.issubset(variants)
            all_lef = complete and all(lef_signatures.get(vt) for vt in expected_vts)
            same_lef = bool(all_lef and len({lef_signatures[vt] for vt in expected_vts}) == 1)
            same_logic = bool(complete and len({logical_signatures.get(vt) for vt in expected_vts}) == 1)
            family_info[family] = {"variants": variants, "complete": complete,
                                   "same_lef": same_lef, "same_logic": same_logic,
                                   "lef": physical}
            if complete:
                complete_count += 1
            for vt in sorted(variants, key=lambda value: VT_RANK[value]):
                cell = variants[vt]
                writer.writerow({
                    "family": family,
                    "vt": VT_TO_NAME[vt],
                    "master": cell["master"],
                    "area": cell["area"] or "",
                    "cell_leakage_power": cell["cell_leakage_power"] or "",
                    "leakage_power_groups_json": json.dumps(cell["leakage_power_groups"], separators=(",", ":")),
                    "pin_capacitances_json": json.dumps({k: v.get("capacitance") for k, v in sorted(cell["pins"].items())}, separators=(",", ":")),
                    "pin_functions_json": json.dumps({k: {"direction": v.get("direction"), "function": v.get("function")} for k, v in sorted(cell["pins"].items())}, separators=(",", ":")),
                    "internal_power_tables_json": json.dumps(cell["internal_power_tables"], separators=(",", ":")),
                    "sequential_signature_json": json.dumps(cell["sequential_signature"], separators=(",", ":")),
                    "library": cell["library"],
                    "lef_signature": lef_signatures.get(vt) or "",
                    "logical_signature": logical_signatures[vt],
                    "complete_three_vt_family": int(complete),
                    "same_footprint_and_pin_map": int(same_lef),
                    "functionally_equivalent": int(same_logic),
                })
            if not (same_lef and same_logic):
                continue
            # Include adjacent threshold steps in each direction; the Tcl
            # selector decides whether to down- or up-swap from current slack.
            for source_vt in sorted(variants, key=lambda value: VT_RANK[value]):
                for target_vt in sorted(variants, key=lambda value: VT_RANK[value]):
                    if source_vt == target_vt:
                        continue
                    if abs(VT_RANK[source_vt] - VT_RANK[target_vt]) != 1:
                        continue
                    source = variants[source_vt]
                    target = variants[target_vt]
                    pair_rows.append({
                        "family": family, "source_vt": source_vt, "target_vt": target_vt,
                        "source": source["master"], "target": target["master"],
                        "source_leakage": source["cell_leakage_power"] or "",
                        "target_leakage": target["cell_leakage_power"] or "",
                        "source_leakage_groups": json.dumps(source["leakage_power_groups"], separators=(",", ":")),
                        "target_leakage_groups": json.dumps(target["leakage_power_groups"], separators=(",", ":")),
                        "source_library": source["library"], "target_library": target["library"],
                        "lef_signature": lef_signatures[source_vt],
                        "logical_signature": logical_signatures[source_vt],
                    })
    pair_path = output_dir / "stage3a_multivt_replacement_pairs.tsv"
    pair_fields = ["family", "source_vt", "target_vt", "source", "target", "source_leakage",
                   "target_leakage", "source_leakage_groups", "target_leakage_groups",
                   "source_library", "target_library", "lef_signature", "logical_signature"]
    with pair_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=pair_fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(pair_rows)
    (output_dir / "stage3a_multivt_audit_manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "pass": "3A multi-Vt static audit",
        "threshold_tags": {"R": "RVT", "L": "LVT", "SL": "SLVT"},
        "libraries": [{"path": str(p.resolve()), "sha256": digest(p)} for p in libs],
        "lefs": [{"path": str(p.resolve()), "sha256": digest(p)} for p in lefs],
        "liberty_cells_with_multi_vt_tags": sum(len(v) for v in cells.values()),
        "family_count": len(cells),
        "complete_three_vt_families": complete_count,
        "eligible_families_same_lef_and_logic": sum(1 for d in family_info.values() if d["same_lef"] and d["same_logic"]),
        "adjacent_directional_pairs": len(pair_rows),
        "audit_tsv": str(audit_path.resolve()),
        "pair_tsv": str(pair_path.resolve()),
    }, indent=2, sort_keys=True) + "\n")
    return len(cells), len(pair_rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-odb", type=Path)
    parser.add_argument("--sdc", type=Path)
    parser.add_argument("--lib", required=True, action="append", type=Path,
                        help="Liberty file; repeat for all VT variants in one timing corner")
    parser.add_argument("--lef", required=True, action="append", type=Path,
                        help="ASAP7 cell LEF for R, L, and SL variants")
    parser.add_argument("--rc-tcl", type=Path)
    parser.add_argument("--upf", type=Path)
    parser.add_argument("--spef", type=Path)
    parser.add_argument("--vcd", type=Path)
    parser.add_argument("--scope")
    parser.add_argument("--activity", type=float, default=0.1,
                        help="global default transition activity when no VCD is supplied")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--openroad", type=Path, default=DEFAULT_OPENROAD)
    parser.add_argument("--setup-margin-ps", type=float, default=100.0)
    parser.add_argument("--timing-guard-ps", type=float, default=50.0)
    parser.add_argument("--max-trials", type=int, default=500)
    parser.add_argument("--audit-only", action="store_true",
                        help="emit Liberty/LEF family audit and pair inventory without changing an ODB")
    args = parser.parse_args()

    if args.vcd and not args.scope:
        parser.error("--scope is required with --vcd")
    if not args.audit_only and (not args.input_odb or not args.sdc or not args.rc_tcl):
        parser.error("apply mode requires --input-odb, --sdc, and --rc-tcl")
    if args.activity < 0 or args.setup_margin_ps < 0 or args.timing_guard_ps < 0 or args.max_trials < 0:
        parser.error("activity, timing margins, and max trials must be non-negative")
    required = [*args.lib, *args.lef]
    if args.input_odb: required.append(args.input_odb)
    if args.sdc: required.append(args.sdc)
    if args.rc_tcl: required.append(args.rc_tcl)
    if args.upf: required.append(args.upf)
    if args.spef: required.append(args.spef)
    if args.vcd: required.append(args.vcd)
    if not args.audit_only: required.append(args.openroad)
    for path in required:
        if not path.is_file():
            parser.error(f"missing Pass 3A input: {path}")
    if args.spef and not args.input_odb:
        parser.error("--spef requires --input-odb")

    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"refusing to overwrite non-empty Pass 3A directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    family_count, pair_count = write_audit(args.lib, args.lef, output)
    if args.audit_only:
        print(f"PASS3A_AUDIT families={family_count} eligible_directional_pairs={pair_count} output={output}")
        return 0

    output_odb = output / "stage3a.odb"
    report = output / "stage3a_multivt.tsv"
    log = output / "stage3a_multivt.log"
    env = os.environ.copy()
    env.update({
        "PASS3A_INPUT_ODB": str(args.input_odb.resolve()),
        "PASS3A_SDC": str(args.sdc.resolve()),
        "PASS3A_LIBS": "\n".join(str(p.resolve()) for p in args.lib),
        "PASS3A_RC_TCL": str(args.rc_tcl.resolve()),
        "PASS3A_UPF": str(args.upf.resolve()) if args.upf else "",
        "PASS3A_SPEF": str(args.spef.resolve()) if args.spef else "",
        "PASS3A_VCD": str(args.vcd.resolve()) if args.vcd else "",
        "PASS3A_SCOPE": args.scope or "",
        "PASS3A_ACTIVITY": str(args.activity),
        "PASS3A_PAIRS": str((output / "stage3a_multivt_replacement_pairs.tsv").resolve()),
        "PASS3A_REPORT": str(report.resolve()),
        "PASS3A_OUTPUT_ODB": str(output_odb.resolve()),
        "PASS3A_SETUP_MARGIN_PS": str(args.setup_margin_ps),
        "PASS3A_TIMING_GUARD_PS": str(args.timing_guard_ps),
        "PASS3A_MAX_TRIALS": str(args.max_trials),
        "PASS3A_REQUIRE_DYNAMIC_NONINCREASING": "1" if args.vcd else "0",
    })
    run = subprocess.run([str(args.openroad.resolve()), "-no_init", "-exit", str(TCL)],
                         cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, check=False)
    log.write_text(run.stdout)
    if run.returncode:
        raise SystemExit(f"Pass 3A OpenROAD failed ({run.returncode}); inspect {log}")
    if not output_odb.is_file() or not report.is_file():
        raise SystemExit("Pass 3A did not emit its ODB and trial report")
    accepted = len(re.findall(r"(?m)^PASS3A_ACCEPT\b", run.stdout))
    rejected = len(re.findall(r"(?m)^PASS3A_REJECT\b", run.stdout))
    start = re.search(r"(?m)^PASS3A_START setup_s=(\S+) hold_s=(\S+) tns_s=(\S+) internal_w=(\S+) switching_w=(\S+) leakage_w=(\S+)", run.stdout)
    finish = re.search(r"(?m)^PASS3A_FINISH setup_s=(\S+) hold_s=(\S+) tns_s=(\S+) internal_w=(\S+) switching_w=(\S+) leakage_w=(\S+) swaps=(\d+)", run.stdout)
    if not start or not finish:
        raise SystemExit("Pass 3A log omitted baseline/final STA power markers")
    manifest = {
        "schema_version": 1,
        "pass": "3A multi-Vt in-place swapping",
        "status": "PASS" if accepted else "NO_ACCEPTED_SWAPS",
        "policy": {"setup_margin_ps": args.setup_margin_ps,
                   "timing_guard_ps": args.timing_guard_ps,
                   "upward_swaps_only_when_baseline_setup_negative": True,
                   "measured_vcd_requires_nonincreasing_dynamic_power": bool(args.vcd),
                   "max_trials": args.max_trials},
        "inputs": {"odb": {"path": str(args.input_odb.resolve()), "sha256": digest(args.input_odb)},
                   "sdc": {"path": str(args.sdc.resolve()), "sha256": digest(args.sdc)},
                   "libraries": [{"path": str(p.resolve()), "sha256": digest(p)} for p in args.lib],
                   "lefs": [{"path": str(p.resolve()), "sha256": digest(p)} for p in args.lef],
                   "vcd": {"path": str(args.vcd.resolve()), "sha256": digest(args.vcd), "scope": args.scope} if args.vcd else None,
                   "spef": {"path": str(args.spef.resolve()), "sha256": digest(args.spef)} if args.spef else None},
        "eligibility": {"families": family_count, "directional_adjacent_pairs": pair_count,
                        "accepted_swaps": accepted, "rejected_trials": rejected},
        "power": {"before": {"setup_slack_s": float(start.group(1)), "hold_slack_s": float(start.group(2)),
                               "setup_tns_s": float(start.group(3)),
                               "internal_w": float(start.group(4)), "switching_w": float(start.group(5)), "leakage_w": float(start.group(6))},
                  "after": {"setup_slack_s": float(finish.group(1)), "hold_slack_s": float(finish.group(2)),
                            "setup_tns_s": float(finish.group(3)),
                            "internal_w": float(finish.group(4)), "switching_w": float(finish.group(5)), "leakage_w": float(finish.group(6))}},
        "outputs": {"odb": {"path": str(output_odb.resolve()), "sha256": digest(output_odb)},
                    "report": {"path": str(report.resolve()), "sha256": digest(report)},
                    "log": {"path": str(log.resolve()), "sha256": digest(log)},
                    "family_audit": str((output / "stage3a_multivt_family_audit.tsv").resolve()),
                    "replacement_pairs": str((output / "stage3a_multivt_replacement_pairs.tsv").resolve())},
        "placement_preserved_by_swapMaster": True,
    }
    (output / "stage3a_multivt_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"PASS3A_RESULT status={manifest['status']} accepted={accepted} rejected={rejected} manifest={output / 'stage3a_multivt_manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
