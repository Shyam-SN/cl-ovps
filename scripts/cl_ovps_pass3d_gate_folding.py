#!/usr/bin/env python3
"""Liberty-truth-table and placement-aware Stage 3D fold trials.

The pass exports exact pin maps, then tries candidates in isolated OpenROAD
processes. A candidate is promoted only after physical/timing/power checks and
an external graph-aware LEC proof; rejected trials cannot mutate the input
ODB. Every proposal and rejection reason is retained in the report.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from cl_ovps_clock_topology import (
    ClockTopologyError, _simple_net, _top_ports, analyze_clock_topology,
    extract_top_module, parse_instances,
)
from cl_ovps_liberty_audit import BooleanParser, LibertyParser, combinational_index, consolidate_attributes, walk, unquote

ROOT = Path(__file__).resolve().parents[1]
INVENTORY_TCL = ROOT / "scripts/export_cl_ovps_pass3d_inventory.tcl"
TRIAL_TCL = ROOT / "scripts/stage3d_fold_trial.tcl"
EXPORT_VERILOG_TCL = ROOT / "scripts/export_odb_verilog.tcl"
DEFAULT_OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
COMPLEX_FAMILY_RE = re.compile(r"^(?:a21o|a22o|o21a|o22a|aoi21|oai21|a21oi|o21ai|a2111o|a211o|o211a|o211ai|a31o|o31a|mux4)")
CONTROL_NET_RE = re.compile(r"(?:^|[._/])(rst|reset|set|scan|sdi|sdo|scan_en|scan_enable)(?:$|[._/\[])", re.I)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scalar_net(expression: str, where: str) -> str | None:
    try:
        return _simple_net(expression, where)
    except ClockTopologyError:
        return None


def truth_for_expression(expression: str, pin_to_net: dict[str, str], variable_order: list[str]) -> tuple[int, ...]:
    results = []
    for value in range(1 << len(variable_order)):
        assignment = {name: (value >> index) & 1 for index, name in enumerate(variable_order)}
        pin_values = {pin: assignment[net] for pin, net in pin_to_net.items()}
        results.append(BooleanParser(expression).parse(pin_values))
    return tuple(results)


def composite_truth(up: dict[str, Any], down: dict[str, Any],
                    up_pin_nets: dict[str, str], down_pin_nets: dict[str, str],
                    intermediate: str, variable_order: list[str]) -> tuple[int, ...]:
    results = []
    for value in range(1 << len(variable_order)):
        assignment = {name: (value >> index) & 1 for index, name in enumerate(variable_order)}
        up_values = {pin: assignment[net] for pin, net in up_pin_nets.items()}
        intermediate_value = BooleanParser(str(up["function"])).parse(up_values)
        down_values = {
            pin: (intermediate_value if net == intermediate else assignment[net])
            for pin, net in down_pin_nets.items()
        }
        results.append(BooleanParser(str(down["function"])).parse(down_values))
    return tuple(results)


def reset_scan_taint(instances: list[Any], entries: dict[str, dict[str, Any]],
                     input_ports: set[str]) -> set[str]:
    tainted = {net for net in input_ports if CONTROL_NET_RE.search(net)}
    changed = True
    while changed:
        changed = False
        for inst in instances:
            entry = entries.get(inst.master)
            if not entry:
                continue
            pins = inst.pins()
            nets = {pin: scalar_net(conn.expression, f"{inst.name}/{pin}")
                    for pin, conn in pins.items()}
            if any(nets.get(pin) in tainted for pin in entry["input_pins"]):
                out = nets.get(entry["output_pin"])
                if out and out not in tainted:
                    tainted.add(out)
                    changed = True
    return tainted


def discover(netlist: Path, top: str, inventory_path: Path, functions: dict[str, Any],
             min_activity: float, clock_period_ns: float,
             max_box_um: float, row_height_dbu: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source = netlist.read_text(errors="replace")
    _, _, top_text = extract_top_module(source, top)
    instances = parse_instances(top_text)
    input_ports, output_ports, inout_ports = _top_ports(top_text)
    ports = input_ports | output_ports | inout_ports
    topology = analyze_clock_topology(top_text, top)
    clock_transport = set(topology["transport_instances"])

    entries = {str(entry["master"]): entry for entry in functions["entries"]}
    inventory: dict[str, dict[str, Any]] = {}
    with inventory_path.open(newline="") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            inventory.setdefault(row["instance"], row)
    drivers: dict[str, list[tuple[Any, str]]] = {}
    consumers: dict[str, list[tuple[Any, str]]] = {}
    net_by_pin: dict[str, dict[str, str | None]] = {}
    for inst in instances:
        entry = entries.get(inst.master)
        pins = inst.pins()
        nets = {pin: scalar_net(conn.expression, f"{inst.name}/{pin}")
                for pin, conn in pins.items()}
        net_by_pin[inst.name.lstrip("\\")] = nets
        if not entry:
            continue
        output_pin = str(entry["output_pin"])
        output_net = nets.get(output_pin)
        if output_net:
            drivers.setdefault(output_net, []).append((inst, output_pin))
        for pin in entry["input_pins"]:
            net = nets.get(str(pin))
            if net:
                consumers.setdefault(net, []).append((inst, str(pin)))

    tainted = reset_scan_taint(instances, entries, input_ports)
    targets = [entry for entry in functions["entries"]
               if COMPLEX_FAMILY_RE.match(str(entry["family"]).split("__")[-1])]
    targets.sort(key=lambda entry: (entry["drive_strength"] or 0, str(entry["master"])))
    candidate_rows: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str, str]] = set()

    for net, source_pins in sorted(drivers.items()):
        if len(source_pins) != 1:
            continue
        if net in ports or net in tainted or CONTROL_NET_RE.search(net):
            continue
        loads = consumers.get(net, [])
        if len(loads) != 1:
            continue
        up_inst, up_out_pin = source_pins[0]
        down_inst, down_pin = loads[0]
        up_name, down_name = up_inst.name.lstrip("\\"), down_inst.name.lstrip("\\")
        pair_key = (up_name, down_name, net)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        reason = ""
        if up_name in clock_transport or down_name in clock_transport:
            reason = "TRACED_CLOCK_PATH"
        elif any(token in up_inst.master.lower() or token in down_inst.master.lower()
                 for token in ("__clkload", "__conb_", "__dlclkp_", "__sdlclkp_")):
            # Clock-transport membership is handled above from the traced
            # clock graph. Do not reject generic master names containing
            # "clk", nor one-output combinational cells just because their
            # family resembles a half/full adder. Multi-output cells are
            # absent from the Liberty single-output truth-table index.
            reason = "CLOCK_GATE_OR_TIE_CELL_EXCLUSION"
        up_entry, down_entry = entries[up_inst.master], entries[down_inst.master]
        up_nets = {pin: net_by_pin[up_name].get(pin) for pin in up_entry["input_pins"]}
        down_nets = {pin: net_by_pin[down_name].get(pin) for pin in down_entry["input_pins"]}
        all_nets = [value for value in (*up_nets.values(), *down_nets.values())
                    if value is not None and value != net]
        leaf_nets = sorted(set(all_nets))
        if not reason and any(value is None for value in (*up_nets.values(), *down_nets.values())):
            reason = "NON_SCALAR_OR_UNCONNECTED_INPUT"
        if not reason and len(leaf_nets) > 4:
            reason = "COMPOSITE_INPUT_COUNT_OVER_4"
        if not reason and any(value in ports for value in leaf_nets):
            reason = "TESTBENCH_INTERFACE_NET"
        if not reason and any(value in tainted or CONTROL_NET_RE.search(value) for value in leaf_nets):
            reason = "RESET_SET_OR_SCAN_TREE"
        if not reason and len(up_entry["input_pins"]) != len(set(up_nets.values())):
            reason = "UPSTREAM_INPUT_REMAP_AMBIGUOUS"
        if not reason and len(down_entry["input_pins"]) != len(set(down_nets.values())):
            # The intermediate is one input of the downstream gate; all other
            # pins must map to distinct source variables for exact folding.
            reason = "DOWNSTREAM_INPUT_REMAP_AMBIGUOUS"

        up_place = inventory.get(up_name)
        down_place = inventory.get(down_name)
        if not reason and (not up_place or not down_place):
            reason = "PLACEMENT_COORDINATE_MISSING"
        if not reason:
            dbu = int(up_place["dbu_per_micron"])
            ux, uy = int(up_place["x_dbu"]), int(up_place["y_dbu"])
            dx, dy = int(down_place["x_dbu"]), int(down_place["y_dbu"])
            uw, uh = int(up_place["width_dbu"]), int(up_place["height_dbu"])
            dw, dh = int(down_place["width_dbu"]), int(down_place["height_dbu"])
            bbox = max(ux + uw, dx + dw) - min(ux, dx) + max(uy + uh, dy + dh) - min(uy, dy)
            row_sep = abs((uy + uh // 2) - (dy + dh // 2))
            if bbox / dbu > max_box_um:
                reason = "PHYSICAL_BBOX_OVER_15UM"
            elif row_sep > max(row_height_dbu, uh, dh):
                reason = "NOT_SAME_OR_ADJACENT_ROW"
            elif up_place["status"] in {"FIXED", "FIRM", "COVER"} or down_place["status"] in {"FIXED", "FIRM", "COVER"}:
                reason = "FIXED_OR_DONT_TOUCH_PLACEMENT"

        alpha = 0.0
        activity_hz = None
        origin = "unknown"
        if not reason:
            activity_row = inventory.get(up_name)
            if activity_row and activity_row["output_pin"] == up_out_pin and activity_row["activity_hz"]:
                activity_hz = float(activity_row["activity_hz"])
                origin = activity_row["activity_origin"]
                alpha = activity_hz * clock_period_ns * 1.0e-9
            if not activity_row or activity_hz is None or origin not in {"vcd", "saif"} or alpha < min_activity:
                reason = "LOW_ACTIVITY_OR_UNKNOWN"

        if not reason:
            chain = composite_truth(up_entry, down_entry, up_nets, down_nets, net, leaf_nets)
            for target in targets:
                if len(target["input_pins"]) != len(leaf_nets):
                    continue
                found_mapping = False
                for permuted_nets in itertools.permutations(leaf_nets):
                    target_map = {str(pin): net_name for pin, net_name in zip(target["input_pins"], permuted_nets)}
                    if truth_for_expression(str(target["function"]), target_map, leaf_nets) == chain:
                        candidate_rows.append({
                            "upstream_instance": up_name, "upstream_master": up_inst.master,
                            "upstream_output_pin": up_out_pin, "downstream_instance": down_name,
                            "downstream_master": down_inst.master, "downstream_input_pin": down_pin,
                            "intermediate_net": net, "target_master": target["master"],
                            "target_output_pin": target["output_pin"],
                            "downstream_output_pin": down_entry["output_pin"],
                            "target_input_net_map": target_map,
                            "truth_table": "".join(map(str, chain)), "leaf_nets": leaf_nets,
                            "activity_hz": activity_hz, "activity_per_cycle": alpha,
                            "activity_origin": origin, "bbox_um": round(bbox / dbu, 6),
                            "row_separation_um": round(row_sep / dbu, 6),
                            "physical_status": "PREFLIGHT_PASS_NOT_TRIALLED",
                        })
                        found_mapping = True
                        break
            if not any(row["upstream_instance"] == up_name and row["downstream_instance"] == down_name
                       and row["intermediate_net"] == net for row in candidate_rows):
                reason = "NO_COMPLEX_EQUIVALENT"
        if reason:
            rejected.append({"upstream_instance": up_name, "upstream_master": up_inst.master,
                             "downstream_instance": down_name, "downstream_master": down_inst.master,
                             "intermediate_net": net, "reason": reason})

    candidate_rows.sort(key=lambda row: (-float(row["activity_per_cycle"]),
                                         float(row["bbox_um"]), row["upstream_instance"],
                                         row["downstream_instance"], str(row["target_master"])))
    return candidate_rows, rejected


def export_verilog(openroad: Path, odb: Path, verilog: Path, log: Path) -> None:
    env = os.environ.copy()
    env.update({"EXPORT_ODB": str(odb.resolve()), "EXPORT_VERILOG": str(verilog.resolve())})
    result = subprocess.run([str(openroad.resolve()), "-no_init", "-exit", str(EXPORT_VERILOG_TCL)],
                           cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, check=False)
    log.write_text(result.stdout)
    if result.returncode or not verilog.is_file():
        raise RuntimeError(f"OpenROAD Verilog export failed; inspect {log}")


def write_functional_proof_view(source: Path, destination: Path, top: str) -> int:
    """Remove only known non-functional Sky130HD filler/antenna/decap/tap cells.

    OpenDB's Verilog view includes physical-only instances whose empty
    positional pins conflict with the LEC adapter's named supply-pin ties.
    They do not implement Boolean behavior and are not part of the proof.
    """
    text = source.read_text(errors="replace")
    start, end, top_text = extract_top_module(text, top)
    physical_instance = re.compile(
        r"(?ms)^[ \t]*sky130_fd_sc_hd__(?:fill|decap|diode|tapvpwrvgnd)"
        r"_[A-Za-z0-9_$]+[ \t]+(?:\\[^\s]+|[A-Za-z_$][A-Za-z0-9_$]*)"
        r"[ \t]*\([^;]*\);[ \t]*(?:\r?\n|$)")
    remove = list(physical_instance.finditer(top_text))
    for match in reversed(remove):
        top_text = top_text[:match.start()] + top_text[match.end():]
    destination.write_text(text[:start] + top_text + text[end:])
    return len(remove)


def run_trial(candidate: dict[str, Any], current_odb: Path, current_netlist: Path,
              args: argparse.Namespace, top: str, trial_dir: Path) -> dict[str, Any]:
    trial_dir.mkdir(parents=True, exist_ok=False)
    trial_odb = trial_dir / "candidate.odb"
    trial_v = trial_dir / "candidate.v"
    trial_tsv = trial_dir / "trial.tsv"
    trial_log = trial_dir / "openroad.log"
    map_value = ";".join(f"{pin}={net}" for pin, net in sorted(candidate["target_input_net_map"].items()))
    env = os.environ.copy()
    env.update({"P3D_INPUT_ODB": str(current_odb.resolve()),
                "P3D_SDC": str(args.sdc.resolve()), "P3D_LIB": str(args.lib.resolve()),
                "P3D_RC_TCL": str(args.rc_tcl.resolve()), "P3D_VCD": str(args.vcd.resolve()),
                "P3D_SCOPE": args.scope, "P3D_UP_INST": candidate["upstream_instance"],
                "P3D_DOWN_INST": candidate["downstream_instance"],
                "P3D_TARGET": candidate["target_master"],
                "P3D_TARGET_OUT": candidate["target_output_pin"],
                "P3D_DOWN_OUT": candidate["downstream_output_pin"],
                "P3D_INT_NET": candidate["intermediate_net"],
                "P3D_INPUT_MAP": map_value,
                "P3D_OUTPUT_ODB": str(trial_odb), "P3D_OUTPUT_V": str(trial_v),
                "P3D_REPORT": str(trial_tsv), "P3D_UPF": str(args.upf.resolve()) if args.upf else ""})
    result = subprocess.run([str(args.openroad.resolve()), "-no_init", "-exit", str(TRIAL_TCL)],
                            cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    trial_log.write_text(result.stdout)
    marker = re.search(r"(?m)^PASS3D_TRIAL\s+status=(\S+)(?:\s+reason=(\S+))?", result.stdout)
    if result.returncode or not marker:
        return {"status": "REJECT", "reason": "OPENROAD_TRIAL_ERROR", "log": str(trial_log)}
    status = marker.group(1)
    reason = marker.group(2) or ""
    row = {"status": status, "reason": reason, "log": str(trial_log)}
    if status != "PRELEC_PASS":
        return row

    lec_dir = trial_dir / "lec"
    baseline_lec_v = trial_dir / "baseline_functional.v"
    candidate_lec_v = trial_dir / "candidate_functional.v"
    row["baseline_physical_only_cells_removed"] = write_functional_proof_view(
        current_netlist, baseline_lec_v, top)
    row["candidate_physical_only_cells_removed"] = write_functional_proof_view(
        trial_v, candidate_lec_v, top)
    lec = subprocess.run([sys.executable, str(ROOT / "scripts/run_cl_ovps_gate_lec.py"),
                          "--baseline-netlist", str(baseline_lec_v.resolve()),
                          "--candidate-netlist", str(candidate_lec_v.resolve()), "--top", top,
                          "--output-dir", str(lec_dir)], cwd=ROOT, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    (trial_dir / "lec_driver.log").write_text(lec.stdout)
    status_file = lec_dir / "status.txt"
    lec_status = status_file.read_text().strip() if status_file.is_file() else "LEC_NOT_PROVEN"
    row.update({"lec_status": lec_status, "lec_log": str(lec_dir / "equivalence.log")})
    row["status"] = "ACCEPT" if lec.returncode == 0 and lec_status == "LEC_PASS" else "REJECT"
    if row["status"] != "ACCEPT":
        row["reason"] = "LEC_FAIL"
        return row
    row.update({"output_odb": str(trial_odb), "output_netlist": str(candidate_lec_v),
                "report": str(trial_tsv)})
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-odb", required=True, type=Path)
    parser.add_argument("--input-netlist", required=True, type=Path)
    parser.add_argument("--top", required=True)
    parser.add_argument("--sdc", required=True, type=Path)
    parser.add_argument("--vcd", required=True, type=Path)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--lib", required=True, type=Path)
    parser.add_argument("--rc-tcl", required=True, type=Path)
    parser.add_argument("--lef", required=True, type=Path, action="append",
                        help="technology/cell LEF; repeatable (include a SITE definition)")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--openroad", type=Path, default=DEFAULT_OPENROAD)
    parser.add_argument("--min-activity", type=float, default=0.1,
                        help="minimum VCD transitions per clock cycle")
    parser.add_argument("--max-bbox-um", type=float, default=15.0)
    parser.add_argument("--clock-period-ns", type=float)
    parser.add_argument("--max-trials", type=int, default=64,
                        help="maximum pre-route trial/LEC candidates; remaining proposals are logged as TRIAL_LIMIT")
    parser.add_argument("--preflight-only", action="store_true",
                        help="export exact proposals without mutating the ODB")
    parser.add_argument("--upf", type=Path)
    args = parser.parse_args()
    for path in (args.input_odb, args.input_netlist, args.sdc, args.vcd, args.lib,
                 args.rc_tcl, *args.lef, args.openroad):
        if not path.is_file(): raise SystemExit(f"missing input file: {path}")
    if args.upf and not args.upf.is_file(): raise SystemExit(f"missing UPF file: {args.upf}")
    if args.max_trials < 0: raise SystemExit("--max-trials must be non-negative")
    if args.min_activity < 0 or args.max_bbox_um <= 0: raise SystemExit("invalid Pass 3D threshold")
    period = args.clock_period_ns
    if period is None:
        sdc_text = args.sdc.read_text(errors="replace")
        periods = [float(value) for value in re.findall(r"(?i)create_clock\b[^\n;]*?-period\s+([0-9.]+)", sdc_text)]
        unique_periods = sorted(set(periods))
        if len(unique_periods) != 1: raise SystemExit("Pass 3D requires one unique SDC create_clock period or --clock-period-ns")
        period = unique_periods[0]
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()): raise SystemExit(f"refusing to overwrite {output}")
    output.mkdir(parents=True, exist_ok=True)
    inventory = output / "stage3d_inventory.tsv"
    log = output / "stage3d_inventory.log"
    env = os.environ.copy()
    env.update({"PASS3D_ODB": str(args.input_odb.resolve()), "PASS3D_SDC": str(args.sdc.resolve()),
                "PASS3D_LIB": str(args.lib.resolve()), "PASS3D_RC_TCL": str(args.rc_tcl.resolve()),
                "PASS3D_VCD": str(args.vcd.resolve()), "PASS3D_SCOPE": args.scope,
                "PASS3D_INVENTORY": str(inventory)})
    run = subprocess.run([str(args.openroad.resolve()), "-no_init", "-exit", str(INVENTORY_TCL)],
                         cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, check=False)
    log.write_text(run.stdout)
    if run.returncode or not inventory.is_file(): raise SystemExit(f"Pass 3D inventory failed; inspect {log}")

    text = args.lib.read_text(errors="replace")
    ast = LibertyParser(text).parse()
    for node in ast: consolidate_attributes(node)
    functions = combinational_index(list(walk(ast, "cell")))
    functions_path = output / "stage3d_liberty_functions.json"
    functions["liberty_sha256"] = sha256(args.lib)
    functions_path.write_text(json.dumps(functions, indent=2, sort_keys=True) + "\n")
    # The standard-cell row height is read from the technology LEF's SITE
    # declaration, not inferred from a cell name or a placement heuristic.
    lef_text = "\n".join(path.read_text(errors="replace") for path in args.lef)
    site = re.search(r"(?ms)^SITE\s+unithd\s*(.*?)^END\s+unithd\b", lef_text)
    if not site or not (height := re.search(r"(?m)^\s*SIZE\s+[0-9.]+\s+BY\s+([0-9.]+)\s*;", site.group(1))):
        raise SystemExit("could not read standard-cell row height from LEF SITE declaration")
    row_height_um = float(height.group(1))
    with inventory.open() as stream:
        header = stream.readline().strip().split("\t")
        first = stream.readline().strip().split("\t")
    dbu = int(first[header.index("dbu_per_micron")]) if first and len(first) == len(header) else 1000
    candidates, rejects = discover(args.input_netlist, args.top, inventory, functions,
                                   args.min_activity, period, args.max_bbox_um,
                                   int(round(row_height_um * dbu)))
    candidates_path = output / "stage3d_candidates.json"
    reject_path = output / "stage3d_rejections.tsv"
    candidates_path.write_text(json.dumps(candidates, indent=2, sort_keys=True) + "\n")
    with reject_path.open("w") as stream:
        stream.write("upstream_instance\tupstream_master\tdownstream_instance\tdownstream_master\tintermediate_net\treason\n")
        for row in rejects:
            stream.write("\t".join(str(row.get(key, "")) for key in (
                "upstream_instance", "upstream_master", "downstream_instance",
                "downstream_master", "intermediate_net", "reason")) + "\n")
    output_odb = output / "stage3d.odb"
    trial_report = output / "stage3d_trial_report.tsv"
    trial_rows: list[dict[str, Any]] = []
    current_odb = args.input_odb.resolve()
    current_netlist = args.input_netlist.resolve()
    used_instances: set[str] = set()
    attempted = 0
    if candidates and not args.preflight_only:
        unique_candidates: list[dict[str, Any]] = []
        seen = set()
        for candidate in candidates:
            key = (candidate["upstream_instance"], candidate["downstream_instance"], candidate["target_master"])
            if key not in seen:
                seen.add(key)
                unique_candidates.append(candidate)
        for index, candidate in enumerate(unique_candidates, start=1):
            up_name = str(candidate["upstream_instance"])
            down_name = str(candidate["downstream_instance"])
            if up_name in used_instances or down_name in used_instances:
                result = {"status": "REJECT", "reason": "OVERLAP_WITH_ACCEPTED_FOLD"}
            elif attempted >= args.max_trials:
                result = {"status": "REJECT", "reason": "TRIAL_LIMIT"}
            else:
                attempted += 1
                result = run_trial(candidate, current_odb, current_netlist, args, args.top,
                                   output / f"trial_{attempted:04d}")
                if result["status"] == "ACCEPT":
                    used_instances.update({up_name, down_name})
                    current_odb = Path(result["output_odb"])
                    current_netlist = Path(result["output_netlist"])
            candidate["trial_result"] = result
            trial_rows.append({"index": index, "upstream_instance": up_name,
                               "downstream_instance": down_name,
                               "target_master": candidate["target_master"],
                               "status": result["status"], "reason": result.get("reason", ""),
                               "log": result.get("log", ""),
                               "lec_status": result.get("lec_status", "")})
    elif candidates:
        trial_rows = [{"index": index, "upstream_instance": row["upstream_instance"],
                       "downstream_instance": row["downstream_instance"],
                       "target_master": row["target_master"],
                       "status": "NOT_RUN", "reason": "PREFLIGHT_ONLY", "log": "", "lec_status": ""}
                      for index, row in enumerate(candidates, start=1)]
    if current_odb.resolve() != output_odb.resolve():
        shutil.copy2(current_odb, output_odb)
    else:
        shutil.copy2(args.input_odb, output_odb)
    if current_netlist.resolve() != args.input_netlist.resolve():
        shutil.copy2(current_netlist, output / "stage3d.v")
    candidates_path.write_text(json.dumps(candidates, indent=2, sort_keys=True) + "\n")
    with trial_report.open("w") as stream:
        stream.write("index\tupstream_instance\tdownstream_instance\ttarget_master\tstatus\treason\tlec_status\tlog\n")
        for row in trial_rows:
            stream.write("\t".join(str(row.get(key, "")) for key in (
                "index", "upstream_instance", "downstream_instance", "target_master",
                "status", "reason", "lec_status", "log")) + "\n")
    committed = sum(row["status"] == "ACCEPT" for row in trial_rows)
    if not candidates:
        status = "NO_CANDIDATES"
    elif args.preflight_only:
        status = "PREFLIGHT_ONLY"
    elif committed:
        status = "PASS"
    else:
        status = "NO_ACCEPTED_TRIALS"
    manifest = {
        "schema_version": 1, "pass": "3D", "name": "truth-table verified complex-gate folding",
        "status": status, "committed_folds": committed,
        "preflight": {"candidate_mappings": len(candidates), "rejected_chains": len(rejects),
                      "min_activity_per_cycle": args.min_activity, "max_bbox_um": args.max_bbox_um,
                      "clock_period_ns": period, "row_height_um_from_lef": row_height_um,
                      "truth_tables_from_liberty_function_attributes": functions["cell_count"]},
        "trials": {"enabled": not args.preflight_only, "attempted": attempted,
                   "accepted": committed,
                   "rejected": sum(row["status"] == "REJECT" for row in trial_rows),
                   "max_trials": args.max_trials,
                   "lec_gate": "Yosys graph-aware LEC after each pre-route timing/power-safe trial"},
        "inputs": {"odb": {"path": str(args.input_odb.resolve()), "sha256": sha256(args.input_odb)},
                   "netlist": {"path": str(args.input_netlist.resolve()), "sha256": sha256(args.input_netlist)},
                   "vcd": {"path": str(args.vcd.resolve()), "sha256": sha256(args.vcd), "scope": args.scope},
                   "liberty": {"path": str(args.lib.resolve()), "sha256": sha256(args.lib)},
                   "lef": [{"path": str(path.resolve()), "sha256": sha256(path)} for path in args.lef]},
        "outputs": {"odb": {"path": str(output_odb), "sha256": sha256(output_odb)},
                    "candidates": {"path": str(candidates_path), "sha256": sha256(candidates_path)},
                    "rejections": {"path": str(reject_path), "sha256": sha256(reject_path)},
                    "trial_report": {"path": str(trial_report), "sha256": sha256(trial_report)},
                    "inventory": {"path": str(inventory), "sha256": sha256(inventory)},
                    "log": {"path": str(log), "sha256": sha256(log)},
                    "liberty_functions": {"path": str(functions_path), "sha256": sha256(functions_path)}},
        "phase1_preserved": True,
    }
    if (output / "stage3d.v").is_file():
        manifest["outputs"]["netlist"] = {"path": str(output / "stage3d.v"),
                                           "sha256": sha256(output / "stage3d.v")}
    (output / "stage3d_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"PASS3D_RESULT status={status} candidates={len(candidates)} attempted={attempted} accepted={committed} rejected={sum(row['status'] == 'REJECT' for row in trial_rows)} manifest={output / 'stage3d_manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
