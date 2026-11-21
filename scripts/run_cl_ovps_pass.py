#!/usr/bin/env python3
"""Run a guarded Stage 2 clock or Stage 3 activity-aware transform on Sky130HD."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from run_cl_ovps_universal import activity_coverage


ROOT = Path(__file__).resolve().parents[1]
OPENROAD_DEFAULT = ROOT / "tools/openroad/build/bin/openroad"
STAGE_SCRIPTS = {
    "clock": ROOT / "scripts/stage2_clock_restructurer.tcl",
    "sizing": ROOT / "scripts/stage3_vcd_opt.tcl",
}
EXPORT_VERILOG_TCL = ROOT / "scripts/export_odb_verilog.tcl"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=tuple(STAGE_SCRIPTS))
    parser.add_argument("--input-odb", required=True, type=Path)
    parser.add_argument("--sdc", required=True, type=Path)
    parser.add_argument("--vcd", type=Path, help="optional activity VCD; omitted uses a disclosed global estimate")
    parser.add_argument("--scope", help="VCD scope selected by Module 0")
    parser.add_argument("--default-activity", type=float, default=0.1,
                        help="fallback transitions/cycle when no VCD is supplied")
    parser.add_argument("--lib", required=True, action="append", type=Path)
    parser.add_argument("--rc-tcl", type=Path)
    parser.add_argument("--upf", type=Path)
    parser.add_argument("--output-odb", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path,
                        help="CSV/TSV audit report destination")
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--action", default="all",
                        help="clock: prune|replace|all; sizing: downsize|pinswap|all")
    parser.add_argument("--swap-levels", help="comma-separated CTS levels to replace")
    parser.add_argument("--downsize-budget-ps", type=float, default=50.0)
    parser.add_argument("--downsize-max-cells", type=int, default=40)
    parser.add_argument("--downsize-min-slack-ps", type=float, default=50.0)
    parser.add_argument("--enable-pass-3c", action="store_true",
                        help="run the Liberty-verified sequential sizing pass after legacy Stage 3A/3B")
    parser.add_argument("--enable-pass-3d", action="store_true",
                        help="run the exact-function/physical Pass 3D preflight after Stage 3C")
    parser.add_argument("--enable-pass-3e", action="store_true",
                        help="run the read-only routed SDF/arrival diagnostic after Stage 3C/3D")
    parser.add_argument("--pass3d-lef", type=Path, action="append",
                        help="technology/cell LEF for Pass 3D; repeat for all relevant LEFs")
    parser.add_argument("--pass3d-max-trials", type=int, default=64,
                        help="maximum Pass 3D trial/LEC candidates per invocation")
    parser.add_argument("--pass3e-spef", type=Path,
                        help="routed SPEF required by Pass 3E (the diagnostic never mutates the ODB)")
    parser.add_argument("--pass3e-zero-delay-vcd", type=Path)
    parser.add_argument("--pass3e-sdf-vcd", type=Path)
    parser.add_argument("--pass3e-scope")
    parser.add_argument("--openroad", type=Path, default=OPENROAD_DEFAULT)
    args = parser.parse_args()

    allowed_actions = {"clock": {"prune", "replace", "all"},
                       "sizing": {"downsize", "pinswap", "all"}}
    if args.action not in allowed_actions[args.stage]:
        parser.error(f"action {args.action!r} is not valid for Stage {args.stage}")
    if args.vcd and not args.scope:
        parser.error("--scope is required when --vcd is supplied")
    if args.default_activity < 0:
        parser.error("--default-activity must be non-negative")
    phase2_requested = args.enable_pass_3c or args.enable_pass_3d or args.enable_pass_3e
    if phase2_requested and args.stage != "sizing":
        parser.error("Passes 3C/3D/3E are available only with --stage sizing")
    if args.enable_pass_3d and not args.pass3d_lef:
        parser.error("--enable-pass-3d requires at least one --pass3d-lef")
    if args.pass3d_max_trials < 0:
        parser.error("--pass3d-max-trials must be non-negative")
    if args.enable_pass_3e and not args.pass3e_spef:
        parser.error("--enable-pass-3e requires --pass3e-spef from the matching routed checkpoint")
    if phase2_requested and not args.rc_tcl:
        parser.error("Passes 3C/3D/3E require --rc-tcl")
    if bool(args.pass3e_zero_delay_vcd) != bool(args.pass3e_sdf_vcd):
        parser.error("Pass 3E VCD comparison requires both --pass3e-zero-delay-vcd and --pass3e-sdf-vcd")
    if args.pass3e_zero_delay_vcd and not args.pass3e_scope:
        parser.error("--pass3e-scope is required with paired Pass 3E VCDs")

    paths = [args.input_odb, args.sdc, *args.lib, args.openroad]
    if args.vcd: paths.append(args.vcd)
    if args.rc_tcl: paths.append(args.rc_tcl)
    if args.upf: paths.append(args.upf)
    if args.pass3d_lef: paths.extend(args.pass3d_lef)
    if args.pass3e_spef: paths.append(args.pass3e_spef)
    if args.pass3e_zero_delay_vcd: paths.extend([args.pass3e_zero_delay_vcd, args.pass3e_sdf_vcd])
    for path in paths:
        if not path.is_file():
            raise SystemExit(f"missing input file: {path}")
    output_odb, report, log = (path.resolve() for path in (args.output_odb, args.report, args.log))
    for path in (output_odb, report, log):
        path.parent.mkdir(parents=True, exist_ok=True)
    if output_odb.exists() or report.exists() or log.exists():
        raise SystemExit("refusing to overwrite an existing Stage 2/3 output")

    env = os.environ.copy()
    env.update({
        "AUDIT_INPUT_ODB": str(args.input_odb.resolve()),
        "AUDIT_SDC": str(args.sdc.resolve()),
        "ACTIVITY_VCD": str(args.vcd.resolve()) if args.vcd else "",
        "AUDIT_ACTIVITY_SCOPE": args.scope or "",
        "AUDIT_ACTIVITY_MODE": "vcd" if args.vcd else "global_default",
        "AUDIT_DEFAULT_ACTIVITY": str(args.default_activity),
        "AUDIT_LIBERTY": "\n".join(str(lib.resolve()) for lib in args.lib),
        "AUDIT_RC_TCL": str(args.rc_tcl.resolve()) if args.rc_tcl else "",
        "AUDIT_UPF_FILE": str(args.upf.resolve()) if args.upf else "",
        "AUDIT_SKIP_UPF": "1" if args.upf is None else "0",
        "AUDIT_CHECK_PDN": "1",
        "AUDIT_REPORT": str(report),
        "AUDIT_OUT_ODB": str(output_odb),
        "CLOCK_RESTRUCTURE_ACTION": args.action if args.stage == "clock" else "all",
        "VCD_OPT_ACTION": args.action if args.stage == "sizing" else "all",
        "AUDIT_SWAP_LEVELS": args.swap_levels or "",
        "AUDIT_DOWNSIZE_BUDGET_PS": str(args.downsize_budget_ps),
        "AUDIT_DOWNSIZE_MAX_CELLS": str(args.downsize_max_cells),
        "AUDIT_DOWNSIZE_MIN_SLACK_PS": str(args.downsize_min_slack_ps),
    })
    command = [str(args.openroad.resolve()), "-no_init", "-exit", str(STAGE_SCRIPTS[args.stage])]
    result = subprocess.run(command, cwd=ROOT, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    log.write_text(result.stdout)
    coverage = activity_coverage(result.stdout) if result.returncode == 0 else None
    if result.returncode:
        raise SystemExit(f"OpenROAD Stage {args.stage} failed; inspect {log}")
    if coverage is None or coverage["unannotated_pins"] != 0:
        raise SystemExit(f"Stage {args.stage} has pins without effective activity; inspect {log}")
    if "AUDIT_PREFLIGHT_GATE=PASS" not in result.stdout:
        raise SystemExit(f"Stage {args.stage} did not pass the timing guard; inspect {log}")
    if not output_odb.is_file() or not report.is_file():
        raise SystemExit(f"Stage {args.stage} did not write its ODB/report")

    phase2: list[dict[str, str]] = []
    current_odb = output_odb
    pass_root = output_odb.parent
    if args.enable_pass_3c:
        if not args.vcd or not args.scope:
            raise SystemExit("Pass 3C requires a measured --vcd and --scope")
        if len(args.lib) != 1:
            raise SystemExit("Pass 3C currently requires exactly one Liberty file")
        pass_dir = pass_root / "pass3c"
        command = ["python3", str(ROOT / "scripts/cl_ovps_pass3c_seq_sizing.py"),
                   "--input-odb", str(current_odb), "--sdc", str(args.sdc),
                   "--vcd", str(args.vcd), "--scope", args.scope,
                   "--lib", str(args.lib[0]), "--rc-tcl", str(args.rc_tcl),
                   "--output-dir", str(pass_dir), "--openroad", str(args.openroad)]
        if args.upf: command += ["--upf", str(args.upf)]
        run = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, check=False)
        (pass_root / "pass3c.driver.log").write_text(run.stdout)
        if run.returncode:
            raise SystemExit(f"Pass 3C failed; inspect {pass_root / 'pass3c.driver.log'}")
        manifest_path = pass_dir / "stage3c_manifest.json"
        pass_manifest = json.loads(manifest_path.read_text())
        if pass_manifest.get("status") not in {"PASS", "NO_CANDIDATES"}:
            raise SystemExit(f"Pass 3C returned unsafe status {pass_manifest.get('status')}")
        phase2.append({"pass": "3C", "status": str(pass_manifest["status"]),
                       "manifest": str(manifest_path)})
        current_odb = Path(pass_manifest["outputs"]["odb"]["path"])

    if args.enable_pass_3d:
        if not args.vcd or not args.scope or len(args.lib) != 1 or not args.rc_tcl:
            raise SystemExit("Pass 3D requires --vcd, --scope, one --lib, and --rc-tcl")
        pass_dir = pass_root / "pass3d"
        pass_dir.mkdir(parents=True, exist_ok=False)
        netlist_path = pass_root / "pass3d_stage_input.v"
        export_log = pass_root / "pass3d_export.log"
        export_env = os.environ.copy()
        export_env.update({"EXPORT_ODB": str(current_odb), "EXPORT_VERILOG": str(netlist_path)})
        export = subprocess.run([str(args.openroad.resolve()), "-no_init", "-exit",
                                 str(EXPORT_VERILOG_TCL)], cwd=ROOT, env=export_env,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                check=False)
        export_log.write_text(export.stdout)
        if export.returncode or not netlist_path.is_file():
            raise SystemExit(f"Pass 3D matching Verilog export failed; inspect {export_log}")
        module_names = re.findall(r"(?m)^\s*module\s+(\\[^\s(]+|[A-Za-z_$][A-Za-z0-9_$]*)", netlist_path.read_text(errors="replace"))
        if len(module_names) != 1:
            raise SystemExit(f"Pass 3D expected a single flattened top module in {netlist_path}, found {module_names}")
        top = module_names[0].lstrip("\\")
        command = ["python3", str(ROOT / "scripts/cl_ovps_pass3d_gate_folding.py"),
                   "--input-odb", str(current_odb), "--input-netlist", str(netlist_path),
                   "--top", top, "--sdc", str(args.sdc), "--vcd", str(args.vcd),
                   "--scope", args.scope, "--lib", str(args.lib[0]),
                   "--rc-tcl", str(args.rc_tcl), "--output-dir", str(pass_dir),
                   "--openroad", str(args.openroad), "--max-trials", str(args.pass3d_max_trials)]
        for lef in args.pass3d_lef: command += ["--lef", str(lef)]
        if args.upf: command += ["--upf", str(args.upf)]
        run = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, check=False)
        (pass_root / "pass3d.driver.log").write_text(run.stdout)
        if run.returncode:
            raise SystemExit(f"Pass 3D preflight failed; inspect {pass_root / 'pass3d.driver.log'}")
        manifest_path = pass_dir / "stage3d_manifest.json"
        pass_manifest = json.loads(manifest_path.read_text())
        phase2.append({"pass": "3D", "status": str(pass_manifest["status"]),
                       "manifest": str(manifest_path)})
        # Current Pass 3D emits exact mappings but deliberately does not fold
        # until each candidate has passed timing, power, and Yosys LEC trials.
        current_odb = Path(pass_manifest["outputs"]["odb"]["path"])

    if args.enable_pass_3e:
        pass_dir = pass_root / "pass3e"
        command = ["python3", str(ROOT / "scripts/cl_ovps_pass3e_glitch_diag.py"),
                   "--input-odb", str(current_odb), "--sdc", str(args.sdc),
                   "--spef", str(args.pass3e_spef), "--rc-tcl", str(args.rc_tcl),
                   "--output-dir", str(pass_dir), "--openroad", str(args.openroad)]
        for lib in args.lib: command += ["--lib", str(lib)]
        if args.pass3e_zero_delay_vcd:
            command += ["--zero-delay-vcd", str(args.pass3e_zero_delay_vcd),
                        "--sdf-vcd", str(args.pass3e_sdf_vcd), "--scope", args.pass3e_scope]
        run = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, check=False)
        (pass_root / "pass3e.driver.log").write_text(run.stdout)
        if run.returncode:
            raise SystemExit(f"Pass 3E diagnostic failed; inspect {pass_root / 'pass3e.driver.log'}")
        manifest_path = pass_dir / "stage3e_manifest.json"
        pass_manifest = json.loads(manifest_path.read_text())
        phase2.append({"pass": "3E", "status": str(pass_manifest["status"]),
                       "manifest": str(manifest_path)})
        current_odb = Path(pass_manifest["outputs"]["odb"]["path"])

    if current_odb.resolve() != output_odb.resolve():
        shutil.copy2(current_odb, output_odb)

    manifest = {
        "schema_version": 1,
        "stage": args.stage,
        "action": args.action,
        "status": "PASS",
        "inputs": {"odb": {"path": str(args.input_odb.resolve()), "sha256": sha256(args.input_odb)},
                   "sdc": {"path": str(args.sdc.resolve()), "sha256": sha256(args.sdc)},
                   "activity": ({"mode": "vcd", "path": str(args.vcd.resolve()),
                                 "sha256": sha256(args.vcd), "scope": args.scope}
                                if args.vcd else {"mode": "estimated_global_default",
                                                 "transitions_per_cycle": args.default_activity})},
        "outputs": {"odb": {"path": str(output_odb), "sha256": sha256(output_odb)},
                    "report": {"path": str(report), "sha256": sha256(report)},
                    "log": {"path": str(log), "sha256": sha256(log)}},
        "activity_coverage": coverage,
        "phase2_passes": phase2,
    }
    (output_odb.parent / f"{output_odb.stem}.cl_ovps.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"CL_OVPS_STAGE stage={args.stage} action={args.action} status=PASS output={output_odb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
