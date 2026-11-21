#!/usr/bin/env python3
"""Route a transformed CTS ODB with ORFS and verify physical route/PDN gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FLOW = ROOT / "tools/openroad-flow-scripts/flow"
DEFAULT_OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
LEAF_NDR_TCL = ROOT / "scripts/cl_ovps_clock_leaf_ndr.tcl"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def route_evidence(path: Path) -> dict[str, object]:
    text = path.read_text(errors="replace")
    drc = re.findall(r"Number of violations\s*=\s*(\d+)", text, re.IGNORECASE)
    nets = re.findall(r"Found\s+(\d+)\s+net violations", text, re.IGNORECASE)
    pins = re.findall(r"Found\s+(\d+)\s+pin violations", text, re.IGNORECASE)
    return {"drc_violations": int(drc[-1]) if drc else None,
            "antenna_net_violations": int(nets[-1]) if nets else None,
            "antenna_pin_violations": int(pins[-1]) if pins else None,
            "pass": bool(drc and nets and pins and int(drc[-1]) == 0
                         and int(nets[-1]) == 0 and int(pins[-1]) == 0)}


def pdn_evidence(path: Path) -> dict[str, object]:
    text = path.read_text(errors="replace")
    vdd = bool(re.search(r"All shapes on net VDD are connected\.|CONNECTIVITY_RESULT\s+net=VDD\s+PASS", text, re.I))
    vss = bool(re.search(r"All shapes on net VSS are connected\.|CONNECTIVITY_RESULT\s+net=VSS\s+PASS", text, re.I))
    warnings = len(re.findall(r"PSM-0069", text))
    return {"vdd_pass": vdd, "vss_pass": vss, "psm_0069_count": warnings,
            "pass": vdd and vss and warnings == 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design", required=True)
    parser.add_argument("--flow-dir", type=Path, default=DEFAULT_FLOW)
    parser.add_argument("--flow-config", required=True, type=Path)
    parser.add_argument("--work-home", required=True, type=Path)
    parser.add_argument("--platform", default="sky130hd")
    parser.add_argument("--design-nickname", required=True)
    parser.add_argument("--source-place-odb", required=True, type=Path)
    parser.add_argument("--source-place-sdc", required=True, type=Path)
    parser.add_argument("--transformed-odb", required=True, type=Path,
                        help="Stage 3 ODB, including its post-CTS tree")
    parser.add_argument("--cts-sdc", required=True, type=Path)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--openroad", type=Path, default=DEFAULT_OPENROAD)
    parser.add_argument("--num-cores", type=int, default=4)
    parser.add_argument("--setup-margin", type=float)
    parser.add_argument("--pre-global-route-tcl", type=Path,
                        help="optional design-specific Tcl applied before global routing; defaults to the generic clock-leaf NDR script")
    parser.add_argument("--reuse-routed", action="store_true",
                        help="reuse an existing route variant and finish LEC/report generation")
    parser.add_argument("--lec-plan", type=Path,
                        help="JSON with a command argv template and optional pass marker")
    parser.add_argument("--baseline-netlist", type=Path)
    args = parser.parse_args()

    pre_global_route_tcl = (args.pre_global_route_tcl or LEAF_NDR_TCL).resolve()
    inputs = [args.flow_dir / "Makefile", args.flow_config, args.work_home,
              args.source_place_odb, args.source_place_sdc, args.transformed_odb,
              args.cts_sdc, args.openroad, pre_global_route_tcl]
    for path in inputs:
        if not path.exists():
            raise SystemExit(f"missing Stage 4 input: {path}")
    if args.lec_plan and (args.baseline_netlist is None or not args.baseline_netlist.is_file()):
        raise SystemExit("--baseline-netlist is required with --lec-plan")
    output = args.output_dir.resolve()
    if output.exists() and not args.reuse_routed:
        raise SystemExit(f"refusing to overwrite Stage 4 output {output}")
    output.mkdir(parents=True, exist_ok=True)
    results = args.work_home.resolve() / "results" / args.platform / args.design_nickname / args.variant
    logs = args.work_home.resolve() / "logs" / args.platform / args.design_nickname / args.variant
    route_driver_log = output / "orfs_route_driver.log"
    if args.reuse_routed:
        if not results.is_dir() or not route_driver_log.is_file():
            raise SystemExit("--reuse-routed requires existing ORFS results and orfs_route_driver.log")
    else:
        if results.exists():
            raise SystemExit(f"ORFS result variant already exists: {results}")
        results.mkdir(parents=True)
        shutil.copy2(args.source_place_odb, results / "3_place.odb")
        shutil.copy2(args.source_place_odb, results / "3_5_place_dp.odb")
        shutil.copy2(args.source_place_sdc, results / "3_place.sdc")
        shutil.copy2(args.transformed_odb, results / "4_1_cts.odb")
        shutil.copy2(args.transformed_odb, results / "4_cts.odb")
        shutil.copy2(args.cts_sdc, results / "4_cts.sdc")

        command = ["make", "-C", str(args.flow_dir.resolve()),
                   f"DESIGN_CONFIG={args.flow_config.resolve()}",
                   f"DESIGN_NICKNAME={args.design_nickname}", f"FLOW_VARIANT={args.variant}",
                   f"WORK_HOME={args.work_home.resolve()}", f"OPENROAD_EXE={args.openroad.resolve()}",
                   f"NUM_CORES={args.num_cores}",
                   f"PRE_GLOBAL_ROUTE_TCL={pre_global_route_tcl}"]
        if args.setup_margin is not None:
            command.append(f"SETUP_SLACK_MARGIN={args.setup_margin:g}")
        command.extend(["do-grt", "do-route", "do-finish"])
        run = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, check=False)
        route_driver_log.write_text(run.stdout)
        if run.returncode:
            raise SystemExit(f"ORFS routing failed ({run.returncode}); inspect {route_driver_log}")

    route_log = logs / "5_2_route.log"
    grt_log = logs / "5_1_grt.log"
    report_log = logs / "6_report.log"
    spef = results / "6_final.spef"
    final_odb = results / "6_final.odb"
    final_netlist = results / "6_final.v"
    for path in (route_log, grt_log, report_log, spef, final_odb, final_netlist):
        if not path.is_file():
            raise SystemExit(f"Stage 4 did not produce required artifact: {path}")
    route = route_evidence(route_log)
    pdn = pdn_evidence(report_log)
    grt_text = grt_log.read_text(errors="replace")
    setup_margin_match = re.search(
        r"repair_timing\s+-setup_margin\s+([-+0-9.eE]+)", grt_text)
    effective_setup_margin = (float(setup_margin_match.group(1))
                              if setup_margin_match else None)
    # Design-specific NDR Tcl files can use a design prefix (for example,
    # AES_LEAF_CLOCK_NDR) while the generic helper emits
    # CL_OVPS_CLOCK_LEAF_NDR. Accept both marker word orders, but still require
    # the explicit 1x-width / 2x-spacing recipe and a nonzero net count.
    ndr_match = re.search(
        r"(?m)^[A-Z0-9_]*(?:CLOCK_LEAF|LEAF_CLOCK)_NDR .*?"
        r"width_factor=1 spacing_factor=2 nets=(\d+)", grt_text)
    ndr_count = int(ndr_match.group(1)) if ndr_match else 0

    lec: dict[str, object] = {"status": "NOT_RUN", "pass": False}
    if args.lec_plan:
        plan_path = args.lec_plan.resolve()
        plan = json.loads(plan_path.read_text())
        command_template = plan.get("command")
        if not isinstance(command_template, list) or not command_template:
            raise SystemExit("LEC plan must contain a non-empty 'command' argument list")
        values = {"design": args.design, "top": plan.get("top", args.design),
                  "root": str(ROOT),
                  "baseline_netlist": str(args.baseline_netlist.resolve()),
                  "candidate_netlist": str(final_netlist.resolve()),
                  "output_dir": str(output / "lec")}
        lec_output = Path(values["output_dir"])
        lec_output.mkdir(parents=True, exist_ok=True)
        lec_command = [str(part).format(**values) for part in command_template]
        lec_run = subprocess.run(lec_command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, check=False)
        lec_log = output / "lec.log"
        lec_summary = ""
        proof_log = lec_output / "equivalence.log"
        if proof_log.is_file():
            point_match = re.findall(
                r"Of those cells\s+(\d+)\s+are proven and\s+(\d+)\s+are unproven",
                proof_log.read_text(errors="replace"))
            if point_match:
                proven_points, unproven_points = map(int, point_match[-1])
                lec_summary = (f"\nLEC_PROOF_POINTS proven={proven_points} "
                               f"unproven={unproven_points}\n")
        lec_log.write_text(lec_run.stdout + lec_summary)
        marker = str(plan.get("pass_marker", ""))
        status_file = plan.get("status_file")
        status_ok = False
        if status_file:
            status_path = Path(str(status_file).format(**values))
            status_ok = status_path.is_file() and status_path.read_text().strip() == str(plan.get("pass_status", "LEC_PASS"))
        elif marker:
            status_ok = marker in lec_run.stdout
        else:
            status_ok = lec_run.returncode == 0
        lec = {"status": "LEC_PASS" if lec_run.returncode == 0 and status_ok else "LEC_FAIL",
               "pass": lec_run.returncode == 0 and status_ok,
               "return_code": lec_run.returncode, "log": str(lec_log), "plan": str(plan_path)}
    lec_status_file = output / "lec_status.txt"
    lec_status_file.write_text(str(lec["status"]) + "\n")

    gates = {"route_drc_antenna": route["pass"], "pdn_vdd_vss": pdn["pass"],
             "clock_leaf_ndr": ndr_count > 0}
    if args.lec_plan:
        gates["logical_equivalence"] = bool(lec["pass"])
    physical_pass = all(gates.values())
    if not physical_pass:
        status = "FAIL"
    elif args.lec_plan:
        status = "PASS" if lec["pass"] else "FAIL"
    else:
        # Routing and PDN checks alone are useful, but must not be reported as
        # complete signoff while logical equivalence has not been run.
        status = "INCOMPLETE"
    manifest = {
        "schema_version": 1, "engine": "CL-OVPS Stage 4 ORFS route/signoff driver",
        "design": args.design, "variant": args.variant, "status": status,
        "timing_repair": {
            "requested_setup_margin_ns": args.setup_margin,
            "effective_setup_margin_ns": effective_setup_margin,
        },
        "pre_global_route_tcl": str(pre_global_route_tcl),
        "inputs": {str(path.resolve()): sha256(path) for path in
                   (args.source_place_odb, args.source_place_sdc, args.transformed_odb,
                    args.cts_sdc, args.flow_config, pre_global_route_tcl)},
        "outputs": {"odb": str(final_odb), "odb_sha256": sha256(final_odb),
                    "spef": str(spef), "spef_sha256": sha256(spef),
                    "netlist": str(final_netlist), "netlist_sha256": sha256(final_netlist),
                    "lec_status": str(lec_status_file)},
        "route": route, "power_grid": pdn, "clock_leaf_ndr_nets": ndr_count,
        "lec": lec, "gates": gates,
    }
    (output / "stage4_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"CL_OVPS_STAGE4 design={args.design} variant={args.variant} status={status} "
          f"spef={spef}")
    return 0 if status in {"PASS", "INCOMPLETE"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
