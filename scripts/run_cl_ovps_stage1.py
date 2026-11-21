#!/usr/bin/env python3
"""Run a design-neutral Sky130HD CTS-oracle placement search through ORFS."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import re
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
FLOW_DIR = ROOT / "tools/openroad-flow-scripts/flow"
FLOW_CONFIG = None
WORK_HOME = ROOT / "build/orfs"
PLATFORM = "sky130hd"
DESIGN_NICKNAME = ""
SOURCE_SDC = None
RESULTS_DIR = None
CTS_LOG_ROOT = None
LIBERTY_FILES: list[Path] = []
RC_TCL = None
ACTIVITY_SCOPE = ""
ACTIVITY_VCDS: dict[str, Path | None] = {}
ACTIVITY_MODE = "vcd"
DEFAULT_ACTIVITY = {"idle": 0.01, "typical": 0.1, "peak": 0.3}
UPF_FILE: Path | None = None
INVENTORY_TCL = ROOT / "scripts/export_cl_ovps_inventory.tcl"
ROWS_TCL = ROOT / "scripts/export_cl_ovps_rows.tcl"
CTS_DATA_TCL = ROOT / "scripts/export_cl_ovps_cts_data.tcl"
EVALUATOR_TCL = ROOT / "scripts/evaluate_cl_ovps_flop_relocations.tcl"
POWER_TCL = ROOT / "scripts/cl_ovps_cts_power.tcl"
POOL_GENERATOR = ROOT / "scripts/make_cl_ovps_relocation_pool.py"
WORKLOADS = ("idle", "typical", "peak")


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_cts(odb: Path, variant: str, num_cores: int) -> dict[str, object]:
    if FLOW_CONFIG is None or SOURCE_SDC is None or RESULTS_DIR is None or CTS_LOG_ROOT is None:
        raise RuntimeError("Stage 1 ORFS configuration is incomplete")
    result_dir = RESULTS_DIR / variant
    report_dir = CTS_LOG_ROOT / variant
    if result_dir.exists() or report_dir.exists():
        raise FileExistsError(f"refusing to overwrite CTS oracle output {variant}")
    if not odb.is_file() or not SOURCE_SDC.is_file():
        raise FileNotFoundError(odb if not odb.is_file() else SOURCE_SDC)
    result_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)
    shutil.copy2(odb, result_dir / "3_place.odb")
    shutil.copy2(odb, result_dir / "3_5_place_dp.odb")
    shutil.copy2(SOURCE_SDC, result_dir / "3_place.sdc")
    command = ["make", "-C", str(FLOW_DIR), f"DESIGN_CONFIG={FLOW_CONFIG}",
               f"DESIGN_NICKNAME={DESIGN_NICKNAME}", f"FLOW_VARIANT={variant}",
               f"WORK_HOME={WORK_HOME}", f"OPENROAD_EXE={OPENROAD}",
               f"NUM_CORES={num_cores}", "do-cts"]
    started = time.monotonic()
    process = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, check=False)
    elapsed = time.monotonic() - started
    log = report_dir / "orfs_cts.log"
    log.write_text(process.stdout)
    cts_odb, cts_sdc = result_dir / "4_1_cts.odb", result_dir / "4_cts.sdc"
    success = process.returncode == 0 and cts_odb.is_file() and cts_sdc.is_file()
    return {"variant": variant, "source_odb": str(odb), "result_dir": str(result_dir),
            "return_code": process.returncode, "elapsed_seconds": f"{elapsed:.3f}",
            "cts_odb": str(cts_odb) if cts_odb.is_file() else "",
            "cts_sdc": str(cts_sdc) if cts_sdc.is_file() else "",
            "status": "ok" if success else "failed", "log": str(log)}


def run_openroad(tcl: Path, env: dict[str, str], log_path: Path) -> None:
    process = subprocess.run([str(OPENROAD), "-no_init", "-exit", str(tcl)], cwd=ROOT,
                             env=env, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, check=False)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(process.stdout)
    if process.returncode:
        raise RuntimeError(f"OpenROAD command failed; see {log_path}")


def export_placement(odb: Path, iteration_dir: Path) -> tuple[Path, Path]:
    inventory = iteration_dir / "inventory.tsv"
    rows = iteration_dir / "rows.tsv"
    env = os.environ.copy()
    env.update({"CL_OVPS_ODB": str(odb), "CL_OVPS_INVENTORY_OUT": str(inventory)})
    run_openroad(INVENTORY_TCL, env, iteration_dir / "inventory.log")
    env = os.environ.copy()
    env.update({"CL_OVPS_ODB": str(odb), "CL_OVPS_ROWS_OUT": str(rows)})
    run_openroad(ROWS_TCL, env, iteration_dir / "rows.log")
    return inventory, rows


def export_cts_data(odb: Path, iteration_dir: Path, label: str) -> tuple[Path, Path]:
    sinks = iteration_dir / f"{label}_sinks.tsv"
    cells = iteration_dir / f"{label}_cells.tsv"
    env = os.environ.copy()
    env.update({"CL_OVPS_ODB": str(odb), "CL_OVPS_SINK_OUT": str(sinks),
                "CL_OVPS_CELL_OUT": str(cells),
                "CL_OVPS_LIBERTY": "\n".join(str(path) for path in LIBERTY_FILES)})
    run_openroad(CTS_DATA_TCL, env, iteration_dir / f"{label}_export.log")
    return sinks, cells


def clock_internal_by_instance(odb: Path, sdc: Path, log_path: Path) -> dict[str, float]:
    env = os.environ.copy()
    env.update({"CL_OVPS_ODB": str(odb), "CL_OVPS_SDC": str(sdc),
                "CL_OVPS_LIBERTY": "\n".join(str(path) for path in LIBERTY_FILES),
                "CL_OVPS_RC_TCL": str(RC_TCL or ""),
                "CL_OVPS_VCD": str(ACTIVITY_VCDS.get("typical") or ""),
                "CL_OVPS_ACTIVITY_SCOPE": ACTIVITY_SCOPE,
                "CL_OVPS_ACTIVITY_MODE": ACTIVITY_MODE,
                "CL_OVPS_DEFAULT_ACTIVITY": str(DEFAULT_ACTIVITY["typical"])})
    process = subprocess.run([str(OPENROAD), "-no_init", "-exit", str(POWER_TCL)], cwd=ROOT,
                             env=env, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, check=False)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(process.stdout)
    if process.returncode:
        raise RuntimeError(f"CTS clock-cell power query failed; see {log_path}")
    clock_instances = set(re.findall(r"^CL_OVPS_CTS_POWER_INSTANCE name=(\S+) master=\S+$",
                                     process.stdout, re.MULTILINE))
    pattern = re.compile(
        r"^\s*([0-9.eE+-]+)\s+[0-9.eE+-]+\s+[0-9.eE+-]+\s+[0-9.eE+-]+\s+(\S+)$",
        re.MULTILINE,
    )
    result = {match.group(2): float(match.group(1)) for match in pattern.finditer(process.stdout)
              if (match.group(2) in clock_instances or
                  re.match(r"^(?:clkload|clkbuf|clkinv|dlclkp|sdlclkp)", match.group(2)))}
    if not result:
        raise ValueError(f"no CTS clock-cell power rows found in {log_path}")
    return result


def select_boundary_sinks(sinks_path: Path, cells_path: Path, excluded: set[str],
                          count: int, boundaries_ff: tuple[float, ...]) -> list[str]:
    sinks = read_tsv(sinks_path)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in sinks:
        branch = row["branch"]
        # CTS leaf net naming differs between clock-tree configurations. The
        # oracle groups every discovered register clock branch dynamically.
        is_leaf = "__leaf_clk" in branch or (branch.startswith("clknet_leaf_") and branch.endswith("_clk"))
        if row["instance"] not in excluded:
            grouped[row["branch"]].append(row)
    loads = {branch: sum(float(row["cap_ff"]) for row in rows) for branch, rows in grouped.items()}
    if not loads:
        raise ValueError("CTS inventory has no sequential sink branches")
    if len(loads) < 2:
        return [row["instance"] for row in sorted(sinks, key=lambda item: -float(item["cap_ff"]))[:count]]
    max_load = max(loads.values())
    centroids = {
        branch: (sum(int(row["x_dbu"]) for row in rows) / len(rows),
                 sum(int(row["y_dbu"]) for row in rows) / len(rows))
        for branch, rows in grouped.items()
    }
    options: list[tuple[float, float, str]] = []
    for branch, rows in grouped.items():
        deficit = max_load - loads[branch]
        boundary_distance = min(abs(deficit - value) for value in boundaries_ff)
        for row in rows:
            x, y = int(row["x_dbu"]), int(row["y_dbu"])
            own = math.hypot(x - centroids[branch][0], y - centroids[branch][1])
            other = min(math.hypot(x - cx, y - cy) for name, (cx, cy) in centroids.items() if name != branch)
            # The first key directs sampling toward branches near a dummy-cell
            # selection boundary; the second favors sinks near a leaf frontier.
            options.append((boundary_distance, other - own, row["instance"]))
    options.sort(key=lambda item: (item[0], item[1], item[2]))
    selected: list[str] = []
    for _distance, _frontier_margin, instance in options:
        if instance not in selected:
            selected.append(instance)
        if len(selected) >= count:
            break
    return selected


def find_workload_vcd(directory: Path, workload: str) -> Path:
    matches = [path for path in directory.rglob("*.vcd")
               if workload in path.name.lower()
               or workload in {part.lower() for part in path.parts}]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {workload} VCD below {directory}, found {len(matches)}")
    return matches[0].resolve()


def infer_activity_scope(vcd: Path, design: str) -> str:
    stack: list[str] = []
    scopes: set[str] = set()
    with vcd.open("r", errors="replace") as source:
        for line in source:
            if line.startswith("$scope"):
                fields = line.split()
                if len(fields) >= 4:
                    stack.append(fields[2])
                    scopes.add("/".join(stack))
            elif line.startswith("$upscope") and stack:
                stack.pop()
            elif line.startswith("$enddefinitions"):
                break
    top = design.lower()
    ranked = sorted(scopes, key=lambda scope: (
        0 if scope.split("/")[-1].lower() in {"dut", "top", "uut", top} else 1,
        len(scope.split("/")), scope))
    if not ranked:
        raise ValueError(f"no VCD module scopes found in {vcd}")
    return ranked[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design", required=True)
    parser.add_argument("--source-odb", type=Path, required=True)
    parser.add_argument("--source-sdc", type=Path, required=True)
    parser.add_argument("--flow-dir", type=Path, default=ROOT / "tools/openroad-flow-scripts/flow")
    parser.add_argument("--flow-config", type=Path, required=True,
                        help="ORFS DESIGN_CONFIG file for this design")
    parser.add_argument("--work-home", type=Path, default=ROOT / "build/orfs")
    parser.add_argument("--platform", default="sky130hd")
    parser.add_argument("--design-nickname", required=True,
                        help="ORFS DESIGN_NICKNAME; no source-code design branch is used")
    parser.add_argument("--activity-vcd-dir", type=Path,
                        help="directory containing idle/typical/peak VCDs; optional with global-default fallback")
    parser.add_argument("--activity-policy", choices=("auto", "vcd", "global-default"), default="auto",
                        help="auto uses complete VCDs when available, otherwise documented global toggle estimates")
    parser.add_argument("--idle-activity", type=float, default=0.01, help="fallback transitions/cycle")
    parser.add_argument("--typical-activity", type=float, default=0.1, help="fallback transitions/cycle")
    parser.add_argument("--peak-activity", type=float, default=0.3, help="fallback transitions/cycle")
    parser.add_argument("--activity-scope", help="VCD scope; defaults to an inferred DUT scope")
    parser.add_argument("--lib", required=True, action="append", type=Path)
    parser.add_argument("--rc-tcl", type=Path)
    parser.add_argument("--upf", type=Path,
                        help="optional UPF file; omitted when the design uses always-on power")
    parser.add_argument("--openroad", type=Path, default=ROOT / "tools/openroad/build/bin/openroad")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument("--candidate-sinks", type=int, default=6)
    parser.add_argument("--moves-per-sink", type=int, default=2)
    parser.add_argument("--candidate-pool", type=Path,
                        help="optional frozen TSV of explicit legal relocations to score on iteration 1")
    parser.add_argument("--clock-internal-cutoff-nw", "--dummy-cutoff-nw",
                        dest="clock_internal_cutoff_nw", type=float, default=-1000.0,
                        help="required reduction in total CTS clock-cell internal power (legacy alias: --dummy-cutoff-nw)")
    parser.add_argument("--hpwl-limit-pct", type=float, default=1.5)
    parser.add_argument("--dummy-boundary-ff", default="4.8365")
    parser.add_argument("--num-cores", type=int, default=1)
    parser.add_argument("--max-candidates", type=int, default=64,
                        help="maximum legal moves evaluated per iteration")
    parser.add_argument("--exclude-pool", type=Path, action="append", default=[])
    args = parser.parse_args()

    global OPENROAD, FLOW_DIR, FLOW_CONFIG, WORK_HOME, PLATFORM, DESIGN_NICKNAME
    global SOURCE_SDC, RESULTS_DIR, CTS_LOG_ROOT, LIBERTY_FILES, RC_TCL
    global ACTIVITY_SCOPE, ACTIVITY_VCDS, ACTIVITY_MODE, DEFAULT_ACTIVITY, UPF_FILE
    OPENROAD = args.openroad.resolve()
    FLOW_DIR = args.flow_dir.resolve()
    FLOW_CONFIG = args.flow_config.resolve()
    WORK_HOME = args.work_home.resolve()
    PLATFORM = args.platform
    DESIGN_NICKNAME = args.design_nickname
    SOURCE_SDC = args.source_sdc.resolve()
    RESULTS_DIR = WORK_HOME / "results" / PLATFORM / DESIGN_NICKNAME
    RC_TCL = args.rc_tcl.resolve() if args.rc_tcl else None
    LIBERTY_FILES = [path.resolve() for path in args.lib]
    UPF_FILE = args.upf.resolve() if args.upf else None
    output = args.output_dir.resolve()
    CTS_LOG_ROOT = output / "cts_trials"
    DEFAULT_ACTIVITY = {"idle": args.idle_activity, "typical": args.typical_activity,
                        "peak": args.peak_activity}
    if any(value < 0 for value in DEFAULT_ACTIVITY.values()):
        raise ValueError("fallback activities must be non-negative transitions/cycle")
    ACTIVITY_VCDS = {workload: None for workload in WORKLOADS}
    if args.activity_policy != "global-default" and args.activity_vcd_dir is not None:
        try:
            ACTIVITY_VCDS = {workload: find_workload_vcd(args.activity_vcd_dir.resolve(), workload)
                             for workload in WORKLOADS}
        except ValueError:
            if args.activity_policy == "vcd":
                raise
    elif args.activity_policy == "vcd":
        raise ValueError("--activity-policy=vcd requires --activity-vcd-dir with all three workloads")
    ACTIVITY_MODE = "vcd" if all(ACTIVITY_VCDS.values()) else "global_default"
    if ACTIVITY_MODE == "global_default":
        ACTIVITY_VCDS = {workload: None for workload in WORKLOADS}
        ACTIVITY_SCOPE = ""
    else:
        typical_vcd = ACTIVITY_VCDS["typical"]
        assert typical_vcd is not None
        ACTIVITY_SCOPE = args.activity_scope or infer_activity_scope(typical_vcd, args.design)

    source_odb = args.source_odb.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite optimizer output {output}")
    output.mkdir(parents=True)
    for path in (source_odb, SOURCE_SDC, FLOW_DIR / "Makefile", FLOW_CONFIG, OPENROAD, *LIBERTY_FILES):
        if not path.is_file():
            raise FileNotFoundError(path)
    if RC_TCL and not RC_TCL.is_file(): raise FileNotFoundError(RC_TCL)
    if UPF_FILE and not UPF_FILE.is_file(): raise FileNotFoundError(UPF_FILE)
    if args.max_candidates < 1:
        raise ValueError("--max-candidates must be positive")

    boundaries = tuple(float(value) for value in args.dummy_boundary_ff.split(",") if value.strip())
    current_odb = output / "initial.odb"
    shutil.copy2(source_odb, current_odb)
    moved_instances: set[str] = set()
    iteration_rows: list[dict[str, object]] = []

    for iteration in range(1, args.max_iterations + 1):
        work = output / f"iteration_{iteration:02d}"
        work.mkdir()
        current_hash = sha256(current_odb)
        inventory, rows_path = export_placement(current_odb, work)

        baseline_variant = f"clovps_{args.design}_{output.name}_i{iteration:02d}_baseline"
        baseline_cts = run_cts(current_odb, baseline_variant, args.num_cores)
        if baseline_cts["status"] != "ok":
            raise RuntimeError(f"current-placement CTS failed in iteration {iteration}")
        baseline_sinks, baseline_cells = export_cts_data(Path(baseline_cts["cts_odb"]), work, "baseline")
        selected = select_boundary_sinks(baseline_sinks, baseline_cells, moved_instances,
                                         args.candidate_sinks, boundaries)
        if not selected:
            iteration_rows.append({"iteration": iteration, "status": "no_unmoved_boundary_sinks",
                                   "source_odb_sha256": current_hash})
            break

        generated_pool = work / "candidate_pool.tsv"
        if iteration == 1 and args.candidate_pool is not None:
            frozen_pool = args.candidate_pool.resolve()
            if not frozen_pool.is_file():
                raise FileNotFoundError(frozen_pool)
            shutil.copy2(frozen_pool, generated_pool)
            (work / "pool_generation.log").write_text(
                f"Using frozen candidate pool {frozen_pool}\n"
                f"sha256={sha256(frozen_pool)}\n"
            )
        else:
            cmd = [sys.executable, str(POOL_GENERATOR), "--inventory", str(inventory), "--rows", str(rows_path),
                   "--cts-sinks", str(baseline_sinks), "--output", str(generated_pool),
                   "--seed", str(20261100 + iteration), "--moves-per-sink", str(args.moves_per_sink),
                   "--candidate-id-offset", str(iteration * 1000), "--min-move-dbu", "4000",
                   "--max-move-dbu", "60000", "--preferred-min-move-dbu", "5000",
                   "--preferred-max-move-dbu", "11000", "--exclude-root", str(output),
                   "--allow-incomplete"]
            for excluded_pool in args.exclude_pool:
                cmd.extend(["--exclude-pool", str(excluded_pool.resolve())])
            for instance in selected:
                cmd.extend(["--only-instance", instance])
            process = subprocess.run(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, check=False)
            (work / "pool_generation.log").write_text(process.stdout)
            if process.returncode:
                raise RuntimeError(f"candidate generation failed in iteration {iteration}; see {work / 'pool_generation.log'}")

        generated_rows = read_tsv(generated_pool)
        if len(generated_rows) > args.max_candidates:
            generated_rows = generated_rows[:args.max_candidates]
            write_tsv(generated_pool, generated_rows)

        candidate_odb_dir = work / "candidate_odb"
        candidate_odb_dir.mkdir()
        audit_path = work / "relocation_audit.tsv"
        env = os.environ.copy()
        env.update({
            "PLACEMENT_ODB": str(current_odb),
            "PLACEMENT_SDC": str(SOURCE_SDC),
            "RELOCATION_POOL": str(generated_pool),
            "RELOCATION_AUDIT": str(audit_path),
            "CANDIDATE_ODB_DIR": str(candidate_odb_dir),
            "IDLE_VCD": str(ACTIVITY_VCDS["idle"] or ""),
            "TYPICAL_VCD": str(ACTIVITY_VCDS["typical"] or ""),
            "PEAK_VCD": str(ACTIVITY_VCDS["peak"] or ""),
            "RELOCATION_ACTIVITY_SCOPE": ACTIVITY_SCOPE,
            "RELOCATION_ACTIVITY_MODE": ACTIVITY_MODE,
            "RELOCATION_IDLE_ACTIVITY": str(DEFAULT_ACTIVITY["idle"]),
            "RELOCATION_TYPICAL_ACTIVITY": str(DEFAULT_ACTIVITY["typical"]),
            "RELOCATION_PEAK_ACTIVITY": str(DEFAULT_ACTIVITY["peak"]),
            "RELOCATION_LIBERTY": "\n".join(str(path) for path in LIBERTY_FILES),
            "RELOCATION_RC_TCL": str(RC_TCL or ""),
            "RELOCATION_PLATFORM_DIR": str(LIBERTY_FILES[0].parent.parent),
            "RELOCATION_UPF": str(UPF_FILE or ""),
            "RELOCATION_MAX_GLOBAL_HPWL_INCREASE": str(args.hpwl_limit_pct / 100.0),
            "RELOCATION_MAX_TIMING_LOSS_NS": "0.05",
            "RELOCATION_MAX_SEARCH_DBU": "60000",
        })
        run_openroad(EVALUATOR_TCL, env, work / "placement_screen.log")
        audit = {row["candidate_id"]: row for row in read_tsv(audit_path)}
        eligible = [row for row in audit.values() if row.get("odb") and row["status"] == "timing_safe"]
        if not eligible:
            iteration_rows.append({"iteration": iteration, "status": "no_timing_safe_candidates",
                                   "source_odb_sha256": current_hash, "candidate_count": len(audit),
                                   "boundary_sinks": ";".join(selected)})
            break

        baseline_power = clock_internal_by_instance(Path(baseline_cts["cts_odb"]),
                                                    Path(baseline_cts["cts_sdc"]), work / "baseline_power.log")
        baseline_clock_internal_nw = sum(baseline_power.values()) * 1e9
        baseline_dummy_internal_nw = sum(value for name, value in baseline_power.items()
                                          if name.startswith("clkload")) * 1e9
        is_clock_master = lambda row: (row.get("instance", "").startswith(("clkload", "clkbuf"))
                                       or bool(re.search(r"__(?:dlclkp|sdlclkp)_[0-9]+$",
                                                         row.get("master", ""))))
        baseline_masters = {row["instance"]: row["master"] for row in read_tsv(baseline_cells)
                            if is_clock_master(row)}
        trials: list[dict[str, object]] = []
        for candidate in eligible:
            candidate_id = candidate["candidate_id"]
            variant = f"clovps_{args.design}_{output.name}_i{iteration:02d}_{candidate_id}"
            cts = run_cts(Path(candidate["odb"]), variant, args.num_cores)
            if cts["status"] != "ok":
                raise RuntimeError(f"candidate CTS failed for {candidate_id}")
            _sink_path, cell_path = export_cts_data(Path(cts["cts_odb"]), work, candidate_id)
            power = clock_internal_by_instance(Path(cts["cts_odb"]), Path(cts["cts_sdc"]),
                                               work / f"{candidate_id}.power.log")
            candidate_clock_internal_nw = sum(power.values()) * 1e9
            candidate_dummy_internal_nw = sum(value for name, value in power.items()
                                               if name.startswith("clkload")) * 1e9
            candidate_active_internal_nw = candidate_clock_internal_nw - candidate_dummy_internal_nw
            masters = {row["instance"]: row["master"] for row in read_tsv(cell_path)
                       if is_clock_master(row)}
            changed = sorted(name for name in baseline_masters.keys() | masters.keys()
                             if baseline_masters.get(name) != masters.get(name))
            clock_delta_nw = candidate_clock_internal_nw - baseline_clock_internal_nw
            dummy_delta_nw = candidate_dummy_internal_nw - baseline_dummy_internal_nw
            active_delta_nw = clock_delta_nw - dummy_delta_nw
            trials.append({
                "iteration": iteration, "candidate_id": candidate_id, "instance": candidate["instance"],
                "target_x_dbu": candidate["to_x_dbu"], "target_y_dbu": candidate["to_y_dbu"],
                "signal_hpwl_delta_pct": candidate["signal_hpwl_delta_pct"],
                "setup_before_ns": candidate["setup_before_ns"], "setup_after_ns": candidate["setup_after_ns"],
                "hold_before_ns": candidate["hold_before_ns"], "hold_after_ns": candidate["hold_after_ns"],
                "clock_internal_delta_nw": clock_delta_nw,
                "dummy_internal_delta_nw": dummy_delta_nw,
                "active_internal_delta_nw": active_delta_nw,
                "clock_cell_master_changes": ";".join(
                    f"{name}:{baseline_masters.get(name, 'ABSENT')}->{masters.get(name, 'ABSENT')}" for name in changed),
                "clock_cell_master_change_count": len(changed), "cts_variant": variant,
                "candidate_odb": candidate["odb"],
                "tier1_pass": int(clock_delta_nw <= args.clock_internal_cutoff_nw
                                   and float(candidate["signal_hpwl_delta_pct"]) <= args.hpwl_limit_pct
                                   and candidate["instance"] not in moved_instances),
            })
        write_tsv(work / "cts_trial_scores.tsv", trials)
        passing = [row for row in trials if int(row["tier1_pass"]) == 1]
        if not passing:
            iteration_rows.append({"iteration": iteration, "status": "no_tier1_candidate",
                                   "source_odb_sha256": current_hash, "candidate_count": len(trials),
                                   "boundary_sinks": ";".join(selected),
                                   "best_clock_internal_delta_nw": min(float(row["clock_internal_delta_nw"]) for row in trials)})
            break

        best = min(passing, key=lambda row: float(row["clock_internal_delta_nw"]))
        committed = work / "committed.odb"
        shutil.copy2(Path(str(best["candidate_odb"])), committed)
        current_odb = committed
        moved_instances.add(str(best["instance"]))
        iteration_rows.append({
            "iteration": iteration, "status": "committed_tier1_move", "candidate_id": best["candidate_id"],
            "instance": best["instance"], "target_x_dbu": best["target_x_dbu"],
            "target_y_dbu": best["target_y_dbu"], "signal_hpwl_delta_pct": best["signal_hpwl_delta_pct"],
            "clock_internal_delta_nw": best["clock_internal_delta_nw"],
            "dummy_internal_delta_nw": best["dummy_internal_delta_nw"],
            "active_internal_delta_nw": best["active_internal_delta_nw"],
            "clock_cell_master_changes": best["clock_cell_master_changes"],
            "source_odb_sha256": current_hash, "committed_odb": str(committed),
            "committed_odb_sha256": sha256(committed), "boundary_sinks": ";".join(selected),
        })
        print(f"TIER1_OPT iteration={iteration} committed={best['candidate_id']} "
              f"clock_internal_delta_nw={float(best['clock_internal_delta_nw']):+.3f} "
              f"hpwl_pct={float(best['signal_hpwl_delta_pct']):+.4f}", flush=True)

    write_tsv(output / "optimization_manifest.tsv", iteration_rows)
    shutil.copy2(current_odb, output / "optimized.odb")
    (output / "optimized.odb.sha256").write_text(sha256(output / "optimized.odb") + "  optimized.odb\n")
    (output / "flow_inputs.sha256").write_text(
        "\n".join(f"{sha256(path)}  {path}" for path in
                  [source_odb, SOURCE_SDC, FLOW_CONFIG, *LIBERTY_FILES,
                  *[path for path in ACTIVITY_VCDS.values() if path is not None], *([RC_TCL] if RC_TCL else []),
                   *([UPF_FILE] if UPF_FILE else [])]) + "\n")
    (output / "activity_policy.json").write_text(json.dumps({
        "mode": ACTIVITY_MODE,
        "quality": "measured_vcd" if ACTIVITY_MODE == "vcd" else "estimated_global_default",
        "fallback_transitions_per_cycle": DEFAULT_ACTIVITY if ACTIVITY_MODE != "vcd" else None,
        "scope": ACTIVITY_SCOPE or None,
    }, indent=2) + "\n")
    print(f"TIER1_OPT result={output / 'optimized.odb'} committed_moves={len(moved_instances)} "
          f"sha256={sha256(output / 'optimized.odb')} route=deferred-to-stage4")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
