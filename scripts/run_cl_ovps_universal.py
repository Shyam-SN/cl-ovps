#!/usr/bin/env python3
"""Measure and sign off a routed Sky130HD design with one design-neutral CLI.

The supplied ODB/SPEF/VCDs are re-opened in OpenROAD. When a baseline ODB is
also supplied, the tool measures matched baseline/candidate power and timing,
checks activity coverage, and validates routed signoff evidence. It does not
silently claim that an optimization was rerun: this command verifies the
artifacts it is given.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORT_TCL = ROOT / "scripts/cl_ovps_report.tcl"
DEFAULT_OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
WORKLOADS = ("idle", "typical", "peak")


class FlowError(RuntimeError):
    pass


def digest(path: Path) -> str:
    if path.is_dir():
        sha = hashlib.sha256()
        for child in sorted(item for item in path.rglob("*") if item.is_file()):
            sha.update(str(child.relative_to(path)).encode())
            sha.update(bytes.fromhex(digest(child)))
        return sha.hexdigest()
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def require_file(path: Path | None, label: str) -> Path:
    if path is None or not path.exists():
        raise FlowError(f"{label} does not exist: {path}")
    return path.resolve()


def find_vcd(directory: Path, workload: str) -> Path:
    files = sorted(directory.rglob("*.vcd"))
    matches = [path for path in files if workload.lower() in path.name.lower()
               or workload.lower() in {part.lower() for part in path.parts}]
    if len(matches) != 1:
        raise FlowError(f"expected exactly one {workload} VCD below {directory}, found {len(matches)}")
    return matches[0].resolve()


def activity_coverage(stdout: str) -> dict[str, Any]:
    raw_match = re.search(r"(?im)^\s*unannotated\s+(\d+)\s*$", stdout)
    if raw_match is None:
        raise FlowError("OpenROAD activity report omitted the unannotated-pin count")
    raw_count = int(raw_match.group(1))
    names_match = re.search(
        r"(?im)^Unannotated pins:\s*\n((?:[ \t]+[^\r\n]+\r?\n)*)", stdout)
    names = {line.strip() for line in names_match.group(1).splitlines() if line.strip()} if names_match else set()
    origins = {name: origin for name, origin in re.findall(
        r"(?m)^CL_OVPS_ACTIVITY_PIN name=(\S+) origin=(\S+)$", stdout)}
    fallback = re.search(r"(?m)^CL_OVPS_ACTIVITY_POLICY(?:\s+\S+)*\s+source=global_default(?:\s|$)", stdout)
    effective_origins = {"clock", "propagated", "vcd", "saif", "global", "input", "user"}
    clock_covered = {name for name in names if origins.get(name) in effective_origins}
    # A count without a name list cannot be safely distinguished from real
    # missing datapath activity, so leave it uncovered.
    if fallback:
        effective_missing = 0
    elif raw_count != len(names) and raw_count:
        effective_missing = raw_count
    else:
        effective_missing = raw_count - len(clock_covered)
    return {"vcd_unannotated_pins": None if fallback else raw_count,
            "raw_unannotated_pins": raw_count,
            "clock_activity_covered_pins": len(clock_covered),
            "unannotated_pins": effective_missing,
            "activity_policy": "estimated_global_default" if fallback else "vcd_or_clock_propagated"}


def vcd_scope_candidates(path: Path, explicit: str | None, top_hint: str = "") -> list[str]:
    if explicit:
        return [explicit]
    stack: list[str] = []
    scopes: set[str] = set()
    with path.open("r", errors="replace") as stream:
        for line in stream:
            if line.startswith("$scope"):
                parts = line.split()
                if len(parts) >= 4:
                    stack.append(parts[2])
                    scopes.add("/".join(stack))
            elif line.startswith("$upscope"):
                if stack:
                    stack.pop()
            elif line.startswith("$enddefinitions"):
                break
    if not scopes:
        raise FlowError(f"VCD has no module scopes in its header: {path}")
    top = top_hint.lower()

    def rank(scope: str) -> tuple[int, int, int, str]:
        parts = scope.split("/")
        leaf = parts[-1].lower()
        known = 0 if leaf in {"dut", "top", "uut", "design"} else 1
        is_top = 0 if leaf == top or top in {part.lower() for part in parts} else 1
        depth = len(parts)
        # Prefer a DUT/top boundary and then the shallowest such scope. Deeper
        # netlist instance scopes are considered only as a fallback.
        return known, is_top, depth, scope

    candidates = sorted(scopes, key=rank)
    preferred = [scope for scope in candidates
                 if scope.split("/")[-1].lower() in {"dut", "top", "uut", "design"}
                 or scope.split("/")[-1].lower() == top]
    shallow = [scope for scope in candidates if len(scope.split("/")) <= 3]
    ordered = preferred + [scope for scope in shallow if scope not in preferred]
    return ordered or candidates[:8]


def parse_report(stdout: str, workload: str, scope: str | None, vcd: Path | None) -> dict[str, Any]:
    profile = re.search(
        r"CL_OVPS_PROFILE top=(\S+) seq_sinks=(\d+) clock_domains=(\d+) "
        r"cts_buffers=(\d+) cts_inverters=(\d+) cts_dummies=(\d+) "
        r"signal_hpwl_dbu=(\d+) dbu_per_micron=(\d+) setup_ns=([+\-0-9.eE]+) "
        r"hold_ns=([+\-0-9.eE]+) tns_ns=(\S+) clocks=(\S*)", stdout)
    power = re.search(
        r"CL_OVPS_POWER internal_w=([+\-0-9.eE]+) switching_w=([+\-0-9.eE]+) "
        r"leakage_w=([+\-0-9.eE]+) total_w=([+\-0-9.eE]+)", stdout)
    annotated = re.search(r"Annotated\s+(\d+)\s+pin activities", stdout, re.IGNORECASE)
    if not all((profile, power)):
        raise FlowError(f"OpenROAD report is missing summary markers for {workload}; inspect log")
    coverage = activity_coverage(stdout)
    if annotated is None and coverage["activity_policy"] != "estimated_global_default":
        raise FlowError(f"OpenROAD report omitted measured annotation count for {workload}")
    fields = profile.groups()
    internal, switching, leakage, total = (float(value) for value in power.groups())
    return {
        "workload": workload,
        "activity_mode": "estimated_global_default" if coverage["activity_policy"] == "estimated_global_default" else "vcd",
        "vcd": str(vcd) if vcd else None,
        "vcd_sha256": digest(vcd) if vcd else None,
        "vcd_scope": scope,
        "annotated_pins": int(annotated.group(1)) if annotated else None,
        **coverage,
        "top": fields[0],
        "sequential_sinks": int(fields[1]),
        "clock_domains": int(fields[2]),
        "cts_buffers": int(fields[3]),
        "cts_inverters": int(fields[4]),
        "cts_dummy_loads": int(fields[5]),
        "signal_hpwl_dbu": int(fields[6]),
        "dbu_per_micron": int(fields[7]),
        "signal_hpwl_um": int(fields[6]) / int(fields[7]),
        "setup_slack_ns": float(fields[8]),
        "hold_slack_ns": float(fields[9]),
        "tns_ns": None if fields[10] == "NA" else float(fields[10]),
        "clock_names": [name for name in fields[11].split(",") if name],
        "internal_power_w": internal,
        "switching_power_w": switching,
        "dynamic_power_w": internal + switching,
        "leakage_power_w": leakage,
        "total_power_w": total,
    }


def run_openroad_measurement(openroad: Path, odb: Path, sdc: Path, spef: Path | None,
                             libs: list[Path], rc_tcl: Path | None, upf: Path | None, vcd: Path | None,
                             scope_options: list[str | None], workload: str,
                             activity_mode: str, default_activity: float,
                             output_dir: Path, label: str) -> dict[str, Any]:
    last_error = ""
    for scope_index, scope in enumerate(scope_options):
        env = os.environ.copy()
        env.update({
            "CL_OVPS_ODB": str(odb),
            "CL_OVPS_SDC": str(sdc),
            "CL_OVPS_SPEF": str(spef or ""),
            "CL_OVPS_LIBS": "\n".join(str(path) for path in libs),
            "CL_OVPS_RC_TCL": str(rc_tcl or ""),
            "CL_OVPS_UPF": str(upf or ""),
            "CL_OVPS_VCD": str(vcd or ""),
            "CL_OVPS_VCD_SCOPE": scope or "",
            "CL_OVPS_ACTIVITY_MODE": activity_mode,
            "CL_OVPS_DEFAULT_ACTIVITY": str(default_activity),
            "CL_OVPS_WORKLOAD": workload,
        })
        result = subprocess.run(
            [str(openroad), "-no_init", "-exit", str(REPORT_TCL)],
            cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, check=False)
        log_path = output_dir / f"{label}.{workload}.scope{scope_index:02d}.log"
        log_path.write_text(result.stdout)
        if result.returncode:
            last_error = f"OpenROAD failed for scope {scope!r}; see {log_path}"
            continue
        try:
            row = parse_report(result.stdout, workload, scope, vcd)
        except FlowError as exc:
            last_error = f"{exc}; see {log_path}"
            continue
        row["openroad_log"] = str(log_path)
        if row["unannotated_pins"] == 0 and ((row["annotated_pins"] or 0) > 0 or activity_mode == "global_default"):
            return row
        last_error = (f"scope {scope!r} left {row['unannotated_pins']} pins without effective activity "
                      f"({row['vcd_unannotated_pins']} lack VCD records; "
                      f"{row['clock_activity_covered_pins']} have clock-derived activity); see {log_path}")
    raise FlowError(f"no VCD scope achieved complete annotation for {workload}: {last_error}")


def route_evidence(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"available": False, "drc_violations": None, "antenna_net_violations": None,
                "antenna_pin_violations": None, "pass": False}
    text = path.read_text(errors="replace")
    drc = re.findall(r"Number of violations\s*=\s*(\d+)", text, re.IGNORECASE)
    ant_nets = re.findall(r"Found\s+(\d+)\s+net violations", text, re.IGNORECASE)
    ant_pins = re.findall(r"Found\s+(\d+)\s+pin violations", text, re.IGNORECASE)
    drc_count = int(drc[-1]) if drc else None
    net_count = int(ant_nets[-1]) if ant_nets else None
    pin_count = int(ant_pins[-1]) if ant_pins else None
    return {"available": True, "path": str(path), "sha256": digest(path),
            "drc_violations": drc_count, "antenna_net_violations": net_count,
            "antenna_pin_violations": pin_count,
            "pass": drc_count == 0 and net_count == 0 and pin_count == 0}


def psm_evidence(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"available": False, "vdd_pass": False, "vss_pass": False, "pass": False}
    text = path.read_text(errors="replace")

    def net_pass(net: str) -> bool:
        patterns = (rf"CONNECTIVITY_RESULT\s+net={net}\s+PASS\b",
                    rf"All shapes on net {net} are connected\.")
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)

    vdd, vss = net_pass("VDD"), net_pass("VSS")
    warnings = re.findall(r"PSM-0069", text)
    return {"available": True, "path": str(path), "sha256": digest(path),
            "vdd_pass": vdd, "vss_pass": vss, "psm_0069_count": len(warnings),
            "pass": vdd and vss and not warnings}


def lec_evidence(status_path: Path | None, log_path: Path | None) -> dict[str, Any]:
    if status_path is None:
        return {"available": False, "status": "MISSING", "proven_points": None,
                "unproven_points": None, "pass": False}
    status = status_path.read_text(errors="replace").strip()
    log_text = log_path.read_text(errors="replace") if log_path else ""
    point_match = re.findall(r"Of those cells\s+(\d+)\s+are proven and\s+(\d+)\s+are unproven", log_text)
    if not point_match:
        point_match = re.findall(r"LEC_PROOF_POINTS\s+proven=(\d+)\s+unproven=(\d+)", log_text)
    proven = int(point_match[-1][0]) if point_match else None
    unproven = int(point_match[-1][1]) if point_match else None
    passed = status == "LEC_PASS" and (unproven in (None, 0))
    return {"available": True, "status": status, "path": str(status_path),
            "sha256": digest(status_path), "log": str(log_path) if log_path else None,
            "proven_points": proven, "unproven_points": unproven, "pass": passed}


def plan_path(value: str, context: dict[str, Any]) -> Path:
    expanded = value.format_map(context)
    path = Path(expanded)
    return (path if path.is_absolute() else ROOT / path).resolve()


def execute_flow_plan(plan_pathname: Path, args: argparse.Namespace, output: Path) -> dict[str, Any]:
    plan_file = require_file(plan_pathname, "flow plan")
    plan = json.loads(plan_file.read_text())
    if plan.get("schema_version") != 1:
        raise FlowError("flow plan schema_version must be 1")
    stages = plan.get("stages")
    expected = ["stage1", "stage2", "stage3", "stage4"]
    if not isinstance(stages, list) or [stage.get("name") for stage in stages] != expected:
        raise FlowError("flow plan must contain ordered stage1, stage2, stage3, stage4 entries")
    if args.enable_pass_3c or args.enable_pass_3d:
        injected = False
        for stage in stages:
            if stage.get("name") != "stage3":
                continue
            commands = stage.get("commands")
            if commands is None:
                commands = [stage.get("command")]
            for command in commands:
                if not isinstance(command, list):
                    continue
                joined = " ".join(map(str, command))
                if "run_cl_ovps_pass.py" not in joined or "sizing" not in command:
                    continue
                if args.enable_pass_3c:
                    command.append("--enable-pass-3c")
                if args.enable_pass_3d:
                    command.append("--enable-pass-3d")
                    for lef in args.pass3d_lef:
                        command.extend(["--pass3d-lef", str(lef)])
                    command.extend(["--pass3d-max-trials", str(args.pass3d_max_trials)])
                injected = True
        if not injected:
            raise FlowError("Phase 2 flags were requested, but Stage 3 does not invoke run_cl_ovps_pass.py --stage sizing")
    if args.enable_pass_3a:
        stage3 = next(stage for stage in stages if stage.get("name") == "stage3")
        outputs = stage3.get("outputs", {})
        if "sizing_odb" not in outputs:
            raise FlowError("Phase 3A needs Stage 3 output 'sizing_odb' so it can swap the routed checkpoint")
        base_sizing_odb = str(outputs["sizing_odb"])
        stage3["outputs"]["pre_multivt_odb"] = base_sizing_odb
        multivt_odb = "{output_dir}/stage3/pass3a/stage3a.odb"
        stage3["outputs"]["sizing_odb"] = multivt_odb
        commands = stage3.get("commands")
        if commands is None:
            command = stage3.get("command")
            if not isinstance(command, list):
                raise FlowError("Phase 3A requires Stage 3 to expose a command list")
            commands = [command]
            stage3["commands"] = commands
            stage3.pop("command", None)
        p3a_command = [
            "python3", "{root}/scripts/cl_ovps_pass3a_multivt.py",
            "--input-odb", base_sizing_odb,
            "--sdc", "{sdc}", "--rc-tcl", "{rc_tcl}",
            "--output-dir", "{output_dir}/stage3/pass3a",
            "--activity", str(args.pass3a_activity),
            "--setup-margin-ps", str(args.pass3a_setup_margin_ps),
            "--timing-guard-ps", str(args.timing_guard_ps),
            "--max-trials", str(args.pass3a_max_trials),
        ]
        for liberty in args.lib:
            p3a_command.extend(["--lib", str(liberty.resolve())])
        for lef in args.pass3a_lef:
            p3a_command.extend(["--lef", str(lef.resolve())])
        if args.upf:
            p3a_command.extend(["--upf", str(args.upf.resolve())])
        if args.pass3a_vcd:
            p3a_command.extend(["--vcd", str(args.pass3a_vcd.resolve()),
                                "--scope", args.pass3a_scope])
        commands.append(p3a_command)
    if args.enable_pass_3b:
        stage2 = next(stage for stage in stages if stage.get("name") == "stage2")
        commands = stage2.get("commands")
        if commands is None:
            command = stage2.get("command")
            if not isinstance(command, list):
                raise FlowError("Phase 3B requires Stage 2 to invoke run_cl_ovps_cts.py")
            commands = [command]
            stage2["commands"] = commands
            stage2.pop("command", None)
        cts_index = next((index for index, command in enumerate(commands)
                          if isinstance(command, list) and "run_cl_ovps_cts.py" in " ".join(map(str, command))), None)
        if cts_index is None:
            raise FlowError("--enable-pass-3b requires a Stage 2 command invoking run_cl_ovps_cts.py")
        cts_command = commands[cts_index]
        try:
            placement_arg = cts_command.index("--placement-odb") + 1
        except ValueError as exc:
            raise FlowError("Phase 3B cannot locate --placement-odb in run_cl_ovps_cts.py command") from exc
        cts_command[placement_arg] = "{output_dir}/stage2/pass3b/phase3b.odb"
        p3b_command = [
            "python3", "{root}/scripts/cl_ovps_pass3b_icg_insertion.py",
            "--input-odb", "{stage1_optimized_odb}",
            "--sdc", "{sdc}", "--lib", "{lib}", "--rc-tcl", "{rc_tcl}",
            "--output-dir", "{output_dir}/stage2/pass3b",
            "--min-instances", str(args.pass3b_min_instances),
            "--max-sinks", str(args.pass3b_max_sinks),
            "--max-span-um", str(args.pass3b_max_span_um),
            "--timing-guard-ps", str(args.timing_guard_ps), "--apply",
        ]
        if args.upf:
            p3b_command.extend(["--upf", str(args.upf.resolve())])
        commands.insert(cts_index, p3b_command)
        # Audit the actual CTS-built, post-clock-optimization ODB before Stage 3.
        clock_index = next((index for index, command in enumerate(commands)
                            if isinstance(command, list) and "run_cl_ovps_pass.py" in " ".join(map(str, command))
                            and "--stage" in command and
                            command[command.index("--stage") + 1] == "clock"), None)
        if clock_index is None:
            clock_odb = ("{work_home}/results/{platform}/{design_nickname}/"
                         "{cts_variant}/4_1_cts.odb")
            audit_index = cts_index + 2
        else:
            clock_command = commands[clock_index]
            try:
                clock_odb = clock_command[clock_command.index("--output-odb") + 1]
            except (ValueError, IndexError) as exc:
                raise FlowError("Phase 3B cannot locate Stage 2 clock-pass --output-odb") from exc
            audit_index = clock_index + 1
        postcts_audit = [
            "python3", "{root}/scripts/cl_ovps_pass3b_postcts_check.py",
            "--input-odb", clock_odb,
            "--sdc", "{work_home}/results/{platform}/{design_nickname}/{cts_variant}/4_cts.sdc",
            "--lib", "{lib}", "--rc-tcl", "{rc_tcl}",
            "--output-dir", "{output_dir}/stage2/pass3b/postcts_audit",
        ]
        commands.insert(audit_index, postcts_audit)
        # Re-check after detailed routing with extracted parasitics; this is the
        # final clock-gating timing evidence in the sign-off chain.
        stage4 = next(stage for stage in stages if stage.get("name") == "stage4")
        stage4_commands = stage4.get("commands")
        if stage4_commands is None:
            stage4_command = stage4.get("command")
            if not isinstance(stage4_command, list):
                raise FlowError("Phase 3B requires Stage 4 to expose a command list")
            stage4_commands = [stage4_command]
            stage4["commands"] = stage4_commands
            stage4.pop("command", None)
        final_audit = [
            "python3", "{root}/scripts/cl_ovps_pass3b_postcts_check.py",
            "--input-odb", "{route_results}/6_final.odb",
            "--sdc", "{route_results}/6_final.sdc",
            "--spef", "{route_results}/6_final.spef",
            "--lib", "{lib}", "--rc-tcl", "{rc_tcl}",
            "--output-dir", "{output_dir}/stage4/pass3b_final_audit",
        ]
        stage4_commands.append(final_audit)
    context: dict[str, Any] = {
        "design": args.design,
        "placed_odb": str(args.placed_odb.resolve()),
        "sdc": str(args.sdc.resolve()),
        "vcd_dir": str(args.vcd_dir.resolve()) if args.vcd_dir else "",
        "lib": str(args.lib[0].resolve()),
        "upf": str(args.upf.resolve()) if args.upf else "",
        "output_dir": str(output),
        "root": str(ROOT),
    }
    variables = plan.get("variables", {})
    if not isinstance(variables, dict):
        raise FlowError("flow plan 'variables' must be a JSON object")
    for key, value in variables.items():
        if not isinstance(key, str) or not key.isidentifier() or key in context:
            raise FlowError(f"invalid or reserved flow-plan variable name: {key!r}")
        context[key] = str(value).format_map(context)
    summary: dict[str, Any] = {"path": str(plan_file), "sha256": digest(plan_file), "stages": []}
    for stage in stages:
        name = stage["name"]
        commands = stage.get("commands")
        if commands is None:
            commands = [stage.get("command")]
        if not isinstance(commands, list) or not commands or any(not isinstance(cmd, list) or not cmd for cmd in commands):
            raise FlowError(f"{name} must define a non-empty argv list under command or commands")
        stage_entry: dict[str, Any] = {"name": name, "commands": []}
        for index, template in enumerate(commands, start=1):
            command = [str(token).format_map(context) for token in template]
            log = output / "flow_plan" / f"{name}.{index}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            run = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, check=False)
            log.write_text(run.stdout)
            command_record = {"argv": command, "return_code": run.returncode,
                              "log": str(log), "log_sha256": digest(log)}
            stage_entry["commands"].append(command_record)
            if run.returncode:
                raise FlowError(f"{name} command {index} failed ({run.returncode}); inspect {log}")
        outputs: dict[str, Any] = {}
        for key, value in stage.get("outputs", {}).items():
            path = plan_path(str(value), context)
            if not path.exists():
                raise FlowError(f"{name} declared output does not exist: {path}")
            outputs[key] = {"path": str(path), "sha256": digest(path)}
            context[name] = {**context.get(name, {}), key: str(path)}
            context[f"{name}_{key}"] = str(path)
        stage_entry["outputs"] = outputs
        summary["stages"].append(stage_entry)

    final = plan.get("final", {})
    required_final = {"placed_odb", "sdc", "spef"}
    if args.activity_policy == "vcd": required_final.add("vcd_dir")
    if not required_final.issubset(final):
        raise FlowError(f"flow plan final section must define {sorted(required_final)}")
    resolved_final = {key: plan_path(str(value), context) for key, value in final.items()}
    for key in required_final:
        if not resolved_final[key].exists():
            raise FlowError(f"flow plan final {key} path does not exist: {resolved_final[key]}")
    summary["final"] = {key: {"path": str(path), "sha256": digest(path)}
                         for key, path in resolved_final.items()}
    evidence = plan.get("evidence", {})
    for key in ("route_log", "psm_log", "lec_status", "lec_log"):
        if key in evidence:
            resolved_final[key] = plan_path(str(evidence[key]), context)
            if not resolved_final[key].is_file():
                raise FlowError(f"flow plan evidence {key} does not exist: {resolved_final[key]}")
            summary.setdefault("evidence", {})[key] = {
                "path": str(resolved_final[key]), "sha256": digest(resolved_final[key])}
    return {"summary": summary, "final": resolved_final}


def write_outputs(output: Path, manifest: dict[str, Any]) -> None:
    (output / "cl_ovps_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    lines = [
        f"# CL-OVPS signoff: {manifest['design']}", "",
        f"**Status: {manifest['status']}**", "",
        f"Measured candidate ODB: `{manifest['inputs']['placed_odb']['path']}`", "",
        "| Workload | Baseline dynamic | Candidate dynamic | Delta | Reduction | Δ setup | Δ hold | VCD missing |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for workload in WORKLOADS:
        row = manifest["workloads"].get(workload)
        if not row or "baseline" not in row:
            lines.append(f"| {workload} | — | {row['candidate']['dynamic_power_w']:.9g} W | — | — | — | — | {row['candidate']['unannotated_pins']} |")
            continue
        reduction = -100.0 * row["dynamic_delta_w"] / row["baseline"]["dynamic_power_w"]
        lines.append(
            f"| {workload} | {row['baseline']['dynamic_power_w']:.9g} W | "
            f"{row['candidate']['dynamic_power_w']:.9g} W | {row['dynamic_delta_w']:.9g} W | "
            f"{reduction:.3f}% | {row['setup_delta_ps']:+.2f} ps | "
            f"{row['hold_delta_ps']:+.2f} ps | {row['candidate']['unannotated_pins']} |")
    lines += ["", "## Gates", ""]
    for gate, result in manifest["gates"].items():
        lines.append(f"- {'PASS' if result['pass'] else 'FAIL'} — {gate}: {result.get('detail', '')}")
    lines += ["", "## Measurement boundary", "",
              "OpenROAD re-read the supplied final ODB, SDC, SPEF, Liberty, and each workload VCD. "
              + ("The configured four-stage flow plan ran in this invocation."
                 if manifest.get("flow_plan") else
                 "This invocation measured existing routed artifacts; it did not rerun optimization or routing."), ""]
    (output / "cl_ovps_manifest.md").write_text("\n".join(lines))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design", required=True, help="human-readable top/design label")
    parser.add_argument("--placed_odb", required=True, type=Path,
                        help="candidate ODB; for routed signoff pass the final routed ODB")
    parser.add_argument("--sdc", required=True, type=Path, help="candidate SDC")
    parser.add_argument("--vcd_dir", type=Path,
                        help="optional root containing exactly one VCD per idle/typical/peak workload")
    parser.add_argument("--activity_policy", choices=("auto", "vcd", "global-default"), default="auto",
                        help="use measured VCDs when present or a disclosed global toggle estimate")
    parser.add_argument("--idle_activity", type=float, default=0.01, help="fallback transitions/cycle")
    parser.add_argument("--typical_activity", type=float, default=0.1, help="fallback transitions/cycle")
    parser.add_argument("--peak_activity", type=float, default=0.3, help="fallback transitions/cycle")
    parser.add_argument("--lib", required=True, action="append", type=Path,
                        help="Liberty file; repeat for multiple libraries")
    parser.add_argument("--output_dir", required=True, type=Path)
    parser.add_argument("--flow_plan", type=Path,
                        help="optional JSON command plan that runs Stage 1–4 before signoff measurement")
    parser.add_argument("--enable-pass-3c", action="store_true",
                        help="inject Liberty-verified sequential sizing into the flow-plan Stage 3")
    parser.add_argument("--enable-pass-3a", "--enable-multivt", dest="enable_pass_3a",
                        action="store_true",
                        help="inject same-footprint ASAP7 multi-Vt swaps after the Stage 3 flow command")
    parser.add_argument("--pass3a-lef", type=Path, action="append",
                        help="ASAP7 standard-cell LEF for each of R, L, and SL variants; repeatable")
    parser.add_argument("--pass3a-vcd", type=Path,
                        help="optional activity VCD for the placement-time multi-Vt trial")
    parser.add_argument("--pass3a-scope",
                        help="required VCD scope when --pass3a-vcd is supplied")
    parser.add_argument("--pass3a-activity", type=float, default=0.1,
                        help="global transition activity for Pass 3A when no VCD is supplied")
    parser.add_argument("--pass3a-setup-margin-ps", type=float, default=100.0,
                        help="minimum instance setup slack for slowing a cell when WNS is closed")
    parser.add_argument("--pass3a-max-trials", type=int, default=500)
    parser.add_argument("--enable-pass-3d", action="store_true",
                        help="inject truth-table/geometry Pass 3D preflight into flow-plan Stage 3")
    parser.add_argument("--pass3d-lef", type=Path, action="append",
                        help="technology/cell LEF for Pass 3D; repeatable")
    parser.add_argument("--pass3d-max-trials", type=int, default=64)
    parser.add_argument("--enable-pass-3b", action="store_true",
                        help="insert ABC/SAT-proven ICGs after placement and before CTS in the flow plan")
    parser.add_argument("--pass3b-min-instances", type=int, default=4,
                        help="minimum same-enable sequential sinks per inserted ICG")
    parser.add_argument("--pass3b-max-sinks", "--pass3b-max-cover", dest="pass3b_max_sinks", type=int, default=16,
                        help="maximum sinks connected to one inserted ICG")
    parser.add_argument("--pass3b-max-span-um", type=float, default=25.0,
                        help="maximum geometric cluster diameter in microns")
    parser.add_argument("--enable-pass-3e", "--run-3e-diagnostic", dest="run_3e_diagnostic",
                        action="store_true",
                        help="after routed measurement, write SDF and arrival/glitch diagnostics")
    parser.add_argument("--sdf_vcd_dir", type=Path,
                        help="optional timing-annotated gate-level VCD directory for Pass 3E")
    parser.add_argument("--spef", type=Path, help="candidate routed SPEF; omitted for placement estimates")
    parser.add_argument("--baseline_odb", type=Path)
    parser.add_argument("--baseline_sdc", type=Path)
    parser.add_argument("--baseline_spef", type=Path)
    parser.add_argument("--baseline_vcd_dir", type=Path)
    parser.add_argument("--rc_tcl", type=Path,
                        help="RC setup Tcl; defaults to setRC.tcl beside the Liberty platform")
    parser.add_argument("--upf", type=Path,
                        help="optional UPF; used to initialize power domains if absent from the ODB")
    parser.add_argument("--openroad", type=Path, default=DEFAULT_OPENROAD)
    parser.add_argument("--vcd_scope", help="force the VCD scope; otherwise inspect the VCD header")
    parser.add_argument("--baseline_vcd_scope")
    parser.add_argument("--route_log", type=Path)
    parser.add_argument("--psm_log", type=Path)
    parser.add_argument("--lec_status", type=Path)
    parser.add_argument("--lec_log", type=Path)
    parser.add_argument("--require_signoff", action="store_true",
                        help="fail unless routed, PDN, timing, VCD and LEC evidence all pass")
    parser.add_argument("--timing_guard_ps", type=float, default=50.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.pass3d_max_trials < 0:
        raise FlowError("--pass3d-max-trials must be non-negative")
    if args.enable_pass_3d and not args.pass3d_lef:
        args.pass3d_lef = [ROOT / "openroad/test/sky130hd/sky130_fd_sc_hd__nom.tlef",
                           ROOT / "openroad/test/sky130hd/sky130_fd_sc_hd.lef"]
    if args.pass3d_lef:
        args.pass3d_lef = [require_file(path, "Pass 3D LEF") for path in args.pass3d_lef]
    if (args.enable_pass_3a or args.enable_pass_3b or args.enable_pass_3c or args.enable_pass_3d) and not args.flow_plan:
        raise FlowError("--enable-pass-3a/3b/3c/3d require --flow_plan for stage injection")
    if args.pass3a_vcd and not args.pass3a_scope:
        raise FlowError("--pass3a-scope is required with --pass3a-vcd")
    if args.pass3a_activity < 0 or args.pass3a_setup_margin_ps < 0 or args.pass3a_max_trials < 0:
        raise FlowError("Pass 3A activity, setup margin, and max trials must be non-negative")
    if args.enable_pass_3a and not args.pass3a_lef:
        platform_lef = ROOT / "tools/openroad-flow-scripts/flow/platforms/asap7/lef"
        inferred = [platform_lef / f"asap7sc7p5t_28_{tag}_1x_220121a.lef" for tag in ("R", "L", "SL")]
        if any(not path.is_file() for path in inferred):
            raise FlowError("Pass 3A could not find the bundled ASAP7 R/L/SL cell LEFs; provide --pass3a-lef")
        args.pass3a_lef = inferred
    if args.pass3a_lef:
        args.pass3a_lef = [require_file(path, "Pass 3A LEF") for path in args.pass3a_lef]
    if args.pass3b_min_instances < 2 or args.pass3b_max_sinks < args.pass3b_min_instances or args.pass3b_max_span_um <= 0:
        raise FlowError("Pass 3B requires 2 <= minimum sinks <= maximum sinks and a positive cluster span")
    if args.sdf_vcd_dir:
        require_file(args.sdf_vcd_dir, "SDF-annotated VCD directory")
        if not args.run_3e_diagnostic:
            raise FlowError("--sdf_vcd_dir requires --run-3e-diagnostic")
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise FlowError(f"refusing to overwrite non-empty output directory: {output}")
    output.mkdir(parents=True, exist_ok=True)

    candidate_odb = require_file(args.placed_odb, "candidate ODB")
    candidate_sdc = require_file(args.sdc, "candidate SDC")
    initial_candidate_sdc = candidate_sdc
    candidate_vcd_dir = require_file(args.vcd_dir, "candidate VCD directory") if args.vcd_dir else None
    libs = [require_file(path, "Liberty file") for path in args.lib]
    openroad = require_file(args.openroad, "OpenROAD executable")
    rc_tcl = args.rc_tcl.resolve() if args.rc_tcl else None
    upf = require_file(args.upf, "UPF file") if args.upf else None
    if rc_tcl is None:
        inferred = libs[0].parent.parent / "setRC.tcl"
        rc_tcl = inferred if inferred.is_file() else None
    if rc_tcl is not None:
        require_file(rc_tcl, "RC setup Tcl")

    flow_plan_result = None
    candidate_spef = None
    if args.flow_plan:
        flow_plan_result = execute_flow_plan(args.flow_plan, args, output)
        final = flow_plan_result["final"]
        candidate_odb = require_file(final["placed_odb"], "flow-plan final ODB")
        candidate_sdc = require_file(final["sdc"], "flow-plan final SDC")
        candidate_spef = require_file(final["spef"], "flow-plan final SPEF")
        candidate_vcd_dir = require_file(final["vcd_dir"], "flow-plan final VCD directory") if "vcd_dir" in final else None
        args.route_log = final.get("route_log", args.route_log)
        args.psm_log = final.get("psm_log", args.psm_log)
        args.lec_status = final.get("lec_status", args.lec_status)
        args.lec_log = final.get("lec_log", args.lec_log)

    compare = args.baseline_odb is not None
    baseline_odb = require_file(args.baseline_odb, "baseline ODB") if compare else None
    baseline_sdc = require_file(args.baseline_sdc or initial_candidate_sdc, "baseline SDC") if compare else None
    baseline_spef = require_file(args.baseline_spef, "baseline SPEF") if compare else None
    baseline_vcd_dir = require_file(args.baseline_vcd_dir, "baseline VCD directory") if compare and args.baseline_vcd_dir else candidate_vcd_dir
    if not flow_plan_result:
        candidate_spef = require_file(args.spef, "candidate SPEF") if args.spef else None
    if compare and candidate_spef is None:
        raise FlowError("--spef is required when --baseline_odb is supplied")
    if args.require_signoff and not compare:
        raise FlowError("--require_signoff requires baseline ODB/SPEF inputs")

    input_paths = {"placed_odb": candidate_odb, "sdc": candidate_sdc, "libraries": libs}
    if candidate_vcd_dir: input_paths["vcd_dir"] = candidate_vcd_dir
    if candidate_spef: input_paths["spef"] = candidate_spef
    if upf: input_paths["upf"] = upf
    if compare:
        if baseline_vcd_dir is not None:
            input_paths["baseline_vcd_dir"] = baseline_vcd_dir
        input_paths.update({"baseline_odb": baseline_odb, "baseline_sdc": baseline_sdc,
                            "baseline_spef": baseline_spef})

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "engine": "CL-OVPS universal OpenROAD measurement runner",
        "design": args.design,
        "created_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "status": "INCOMPLETE",
        "inputs": {},
        "workloads": {},
        "gates": {},
    }
    if flow_plan_result:
        manifest["flow_plan"] = flow_plan_result["summary"]
        manifest["inputs"]["flow_plan"] = {
            "path": flow_plan_result["summary"]["path"],
            "sha256": flow_plan_result["summary"]["sha256"]}
    for label, value in input_paths.items():
        if isinstance(value, list):
            manifest["inputs"][label] = [{"path": str(path), "sha256": digest(path)} for path in value]
        elif isinstance(value, Path):
            manifest["inputs"][label] = {"path": str(value), "sha256": digest(value)}
        else:
            manifest["inputs"][label] = {"path": str(value), "sha256": None}

    profile_activity = {"idle": args.idle_activity, "typical": args.typical_activity,
                        "peak": args.peak_activity}
    if any(value < 0 for value in profile_activity.values()):
        raise FlowError("fallback activity values must be non-negative transitions/cycle")
    candidate_vcds: dict[str, Path | None] = {}
    baseline_vcds: dict[str, Path | None] = {}
    if args.activity_policy == "global-default":
        measured = False
    elif candidate_vcd_dir is not None:
        try:
            candidate_vcds = {workload: find_vcd(candidate_vcd_dir, workload) for workload in WORKLOADS}
            if compare:
                source_dir = baseline_vcd_dir or candidate_vcd_dir
                baseline_vcds = {workload: find_vcd(source_dir, workload) for workload in WORKLOADS}
            measured = True
        except FlowError:
            if args.activity_policy == "vcd": raise
            candidate_vcds, baseline_vcds = {}, {}
            measured = False
    else:
        measured = False
        if args.activity_policy == "vcd":
            raise FlowError("--activity-policy=vcd requires --vcd_dir")
    if args.activity_policy == "vcd" and not measured:
        raise FlowError("VCD mode requires complete candidate and baseline idle/typical/peak traces")
    activity_mode = "vcd" if measured else "global_default"
    manifest["activity_model"] = {
        "mode": "measured_vcd" if measured else "estimated_global_default",
        "fallback_transitions_per_cycle": None if measured else profile_activity,
        "signoff_eligible": bool(measured),
    }
    scope_cache: dict[Path, str] = {}
    for workload in WORKLOADS:
        candidate_vcd = candidate_vcds.get(workload)
        candidate_scopes: list[str | None] = ([args.vcd_scope] if args.vcd_scope else
                            ([scope_cache[candidate_vcd]] if candidate_vcd and candidate_vcd in scope_cache else
                             vcd_scope_candidates(candidate_vcd, None) if candidate_vcd else [None]))
        # The first database load supplies a top name, but DUT scopes are a
        # stable generic default; OpenROAD rejects incomplete mappings below.
        candidate_row = run_openroad_measurement(
            openroad, candidate_odb, candidate_sdc, candidate_spef, libs, rc_tcl,
            upf, candidate_vcd, candidate_scopes, workload, activity_mode,
            profile_activity[workload], output, "candidate")
        if candidate_vcd: scope_cache[candidate_vcd] = candidate_row["vcd_scope"]
        row: dict[str, Any] = {"candidate": candidate_row}
        if compare:
            baseline_vcd = baseline_vcds.get(workload) if measured else None
            baseline_scope = args.baseline_vcd_scope
            baseline_scopes: list[str | None] = ([baseline_scope] if baseline_scope else
                               vcd_scope_candidates(baseline_vcd, None, candidate_row["top"])
                               if baseline_vcd else [None])
            baseline_row = run_openroad_measurement(
                openroad, baseline_odb, baseline_sdc, baseline_spef, libs, rc_tcl,
                upf, baseline_vcd, baseline_scopes, workload, activity_mode,
                profile_activity[workload], output, "baseline")
            setup_delta = (candidate_row["setup_slack_ns"] - baseline_row["setup_slack_ns"]) * 1000.0
            hold_delta = (candidate_row["hold_slack_ns"] - baseline_row["hold_slack_ns"]) * 1000.0
            delta = candidate_row["dynamic_power_w"] - baseline_row["dynamic_power_w"]
            absolute_setup_floor_pass = (baseline_row["setup_slack_ns"] < 0.0
                                         or candidate_row["setup_slack_ns"] >= 0.0)
            relative_timing_pass = (setup_delta >= -args.timing_guard_ps
                                    and hold_delta >= -args.timing_guard_ps)
            row.update({"baseline": baseline_row, "dynamic_delta_w": delta,
                        "setup_delta_ps": setup_delta, "hold_delta_ps": hold_delta,
                        "relative_timing_guard_pass": relative_timing_pass,
                        "absolute_setup_floor_pass": absolute_setup_floor_pass,
                        "timing_guard_pass": relative_timing_pass and absolute_setup_floor_pass,
                        "dynamic_improved": delta < 0.0})
        manifest["workloads"][workload] = row
        print(f"CL_OVPS_MEASURE design={args.design} workload={workload} "
              f"candidate_dynamic_w={candidate_row['dynamic_power_w']:.12e} "
              f"unannotated={candidate_row['unannotated_pins']} scope={candidate_row['vcd_scope']}",
              flush=True)

    all_annotation = all(item["candidate"]["unannotated_pins"] == 0
                         and ("baseline" not in item or item["baseline"]["unannotated_pins"] == 0)
                         for item in manifest["workloads"].values())
    manifest["gates"]["Activity coverage"] = {
        "pass": all_annotation,
        "detail": (("all pins have measured or clock-propagated activity"
                    if measured else "all pins receive explicit global default activity; power is an estimate")
                   if all_annotation else "one or more pins lack effective activity")}
    if args.require_signoff:
        manifest["gates"]["Measured workload VCD evidence"] = {
            "pass": measured,
            "detail": "gate-level VCDs required for workload-specific power signoff"}
    if compare:
        timing_pass = all(item["timing_guard_pass"] for item in manifest["workloads"].values())
        dynamic_pass = all(item["dynamic_improved"] for item in manifest["workloads"].values())
        manifest["gates"]["setup/hold regression within guard"] = {
            "pass": timing_pass, "guard_ps": args.timing_guard_ps,
            "detail": "each workload compared against baseline slack"}
        setup_floor_pass = all(item["absolute_setup_floor_pass"] for item in manifest["workloads"].values())
        manifest["gates"]["non-negative setup floor when baseline is closed"] = {
            "pass": setup_floor_pass,
            "detail": "candidate setup slack remains non-negative whenever baseline setup slack is non-negative"}
        manifest["gates"]["dynamic power improves in all workloads"] = {
            "pass": dynamic_pass, "detail": "internal plus net switching power, routed SPEF/VCD"}
    if args.require_signoff:
        route = route_evidence(require_file(args.route_log, "candidate detailed-route log"))
        psm = psm_evidence(require_file(args.psm_log, "candidate PSM log"))
        lec = lec_evidence(require_file(args.lec_status, "LEC status file"),
                           require_file(args.lec_log, "LEC log") if args.lec_log else None)
        manifest["evidence"] = {"route": route, "power_grid": psm, "lec": lec}
        manifest["gates"]["detailed route DRC and antenna"] = {
            "pass": route["pass"], "detail": f"DRC={route['drc_violations']}; antenna net={route['antenna_net_violations']}, pin={route['antenna_pin_violations']}"}
        manifest["gates"]["VDD/VSS connectivity"] = {
            "pass": psm["pass"], "detail": f"VDD={psm['vdd_pass']}; VSS={psm['vss_pass']}; PSM-0069={psm.get('psm_0069_count')}"}
        manifest["gates"]["Yosys LEC"] = {
            "pass": lec["pass"], "detail": f"{lec['proven_points']} proven, {lec['unproven_points']} unproven; status={lec['status']}"}

    if args.run_3e_diagnostic:
        if candidate_spef is None:
            raise FlowError("Pass 3E requires a routed candidate SPEF")
        if rc_tcl is None:
            raise FlowError("Pass 3E requires --rc_tcl")
        if candidate_vcd_dir is None or "typical" not in candidate_vcds:
            raise FlowError("Pass 3E requires the measured candidate typical VCD")
        diagnostic_dir = output / "pass3e"
        command = [sys.executable, str(ROOT / "scripts/cl_ovps_pass3e_glitch_diag.py"),
                   "--input-odb", str(candidate_odb), "--sdc", str(candidate_sdc),
                   "--spef", str(candidate_spef), "--rc-tcl", str(rc_tcl),
                   "--output-dir", str(diagnostic_dir), "--openroad", str(openroad)]
        if args.sdf_vcd_dir:
            sdf_vcd = find_vcd(args.sdf_vcd_dir, "typical")
            command += ["--zero-delay-vcd", str(candidate_vcds["typical"]),
                        "--sdf-vcd", str(sdf_vcd),
                        "--scope", str(scope_cache.get(candidate_vcds["typical"], args.vcd_scope or ""))]
        for lib in libs:
            command += ["--lib", str(lib)]
        run = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, check=False)
        diagnostic_log = output / "pass3e.driver.log"
        diagnostic_log.write_text(run.stdout)
        if run.returncode:
            raise FlowError(f"Pass 3E diagnostic failed; inspect {diagnostic_log}")
        pass3e_manifest = diagnostic_dir / "stage3e_manifest.json"
        manifest["phase2_pass_3e"] = {
            "manifest": str(pass3e_manifest),
            "manifest_sha256": digest(pass3e_manifest),
            "status": json.loads(pass3e_manifest.read_text())["status"],
            "driver_log": str(diagnostic_log),
        }

    manifest["status"] = "PASS" if all(gate["pass"] for gate in manifest["gates"].values()) else "FAIL"
    write_outputs(output, manifest)
    print(f"CL_OVPS_RESULT design={args.design} status={manifest['status']} "
          f"manifest={output / 'cl_ovps_manifest.json'}")
    return 0 if manifest["status"] == "PASS" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FlowError as error:
        print(f"CL-OVPS ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
