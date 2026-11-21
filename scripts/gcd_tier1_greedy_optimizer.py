#!/usr/bin/env python3
"""Greedily commit isolated GCD moves that lower total CTS-cell internal power."""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from run_gcd_cts_oracle import run_cts  # noqa: E402

OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
BASE_RESULT = ROOT / "build/orfs/results/sky130hd/gcd_power/gcd_baseline20ns"
PLACEMENT_VCD = ROOT / "build/activity/gcd_baseline20ns/placement/typical.vcd"
INVENTORY_TCL = ROOT / "scripts/export_gcd_relocation_inventory.tcl"
ROWS_TCL = ROOT / "scripts/export_gcd_placement_rows.tcl"
CTS_DATA_TCL = ROOT / "scripts/export_gcd_cts_oracle_data.tcl"
EVALUATOR_TCL = ROOT / "scripts/evaluate_gcd_flop_relocations.tcl"
POWER_TCL = ROOT / "scripts/gcd_report_placed_cts_power.tcl"
POOL_GENERATOR = ROOT / "scripts/make_gcd_cross_row_pool.py"
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
    env.update({"PLACEMENT_ODB": str(odb), "RELOCATION_INVENTORY_OUT": str(inventory)})
    run_openroad(INVENTORY_TCL, env, iteration_dir / "inventory.log")
    env = os.environ.copy()
    env.update({"PLACEMENT_ODB": str(odb), "ROW_INVENTORY_OUT": str(rows)})
    run_openroad(ROWS_TCL, env, iteration_dir / "rows.log")
    return inventory, rows


def export_cts_data(odb: Path, iteration_dir: Path, label: str) -> tuple[Path, Path]:
    sinks = iteration_dir / f"{label}_sinks.tsv"
    cells = iteration_dir / f"{label}_cells.tsv"
    env = os.environ.copy()
    env.update({"CTS_ORACLE_ODB": str(odb), "CTS_ORACLE_SINK_OUT": str(sinks),
                "CTS_ORACLE_CELL_OUT": str(cells)})
    run_openroad(CTS_DATA_TCL, env, iteration_dir / f"{label}_export.log")
    return sinks, cells


def clock_internal_by_instance(odb: Path, sdc: Path, log_path: Path) -> dict[str, float]:
    env = os.environ.copy()
    env.update({"CTS_ORACLE_ODB": str(odb), "CTS_ORACLE_SDC": str(sdc),
                "ACTIVITY_VCD": str(PLACEMENT_VCD), "WORKLOAD": "typical"})
    process = subprocess.run([str(OPENROAD), "-no_init", "-exit", str(POWER_TCL)], cwd=ROOT,
                             env=env, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, check=False)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(process.stdout)
    if process.returncode:
        raise RuntimeError(f"CTS clock-cell power query failed; see {log_path}")
    import re
    pattern = re.compile(
        r"^\s*([0-9.eE+-]+)\s+[0-9.eE+-]+\s+[0-9.eE+-]+\s+[0-9.eE+-]+\s+((?:clkload|clkbuf|clkinv)[^\s]*)$",
        re.MULTILINE,
    )
    result = {match.group(2): float(match.group(1)) for match in pattern.finditer(process.stdout)}
    if not result:
        raise ValueError(f"no CTS clock-cell power rows found in {log_path}")
    return result


def select_boundary_sinks(sinks_path: Path, cells_path: Path, excluded: set[str],
                          count: int, boundaries_ff: tuple[float, ...]) -> list[str]:
    sinks = read_tsv(sinks_path)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in sinks:
        branch = row["branch"]
        # OpenROAD names GCD leaf nets `clknet_N__leaf_clk`, while ORFS AES
        # names them `clknet_leaf_N_clk`. Accept both conventions so the
        # boundary-targeting selector can be reused on another design.
        is_leaf = "__leaf_clk" in branch or (branch.startswith("clknet_leaf_") and branch.endswith("_clk"))
        if is_leaf and row["instance"] not in excluded:
            grouped[row["branch"]].append(row)
    loads = {branch: sum(float(row["cap_ff"]) for row in rows) for branch, rows in grouped.items()}
    if len(loads) < 2:
        raise ValueError("CTS inventory has fewer than two leaf branches")
    max_load = max(loads.values())
    centroids = {
        branch: (sum(int(row["x_dbu"]) for row in rows) / len(rows),
                 sum(int(row["y_dbu"]) for row in rows) / len(rows))
        for branch, rows in grouped.items()
    }
    dummy_by_net = {
        row["input_net"]: row for row in read_tsv(cells_path) if row["instance"].startswith("clkload")
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-odb", type=Path, required=True)
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
    parser.add_argument("--skip-final-route", action="store_true",
                         help="do not run detailed-route/SPEF validation on the final committed ODB")
    args = parser.parse_args()
    source_odb = args.source_odb.resolve()
    output = args.output_dir.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite optimizer output {output}")
    output.mkdir(parents=True)
    if not source_odb.is_file():
        raise FileNotFoundError(source_odb)
    if not BASE_RESULT.joinpath("3_place.sdc").is_file():
        raise FileNotFoundError(BASE_RESULT / "3_place.sdc")

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

        baseline_variant = f"gcd_tier1opt_{output.name}_i{iteration:02d}_baseline"
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
                   "--preferred-max-move-dbu", "11000", "--exclude-root", str(ROOT / "build/power"),
                   "--allow-incomplete"]
            for instance in selected:
                cmd.extend(["--only-instance", instance])
            process = subprocess.run(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, check=False)
            (work / "pool_generation.log").write_text(process.stdout)
            if process.returncode:
                raise RuntimeError(f"candidate generation failed in iteration {iteration}; see {work / 'pool_generation.log'}")

        candidate_odb_dir = work / "candidate_odb"
        candidate_odb_dir.mkdir()
        audit_path = work / "relocation_audit.tsv"
        env = os.environ.copy()
        env.update({
            "PLACEMENT_ODB": str(current_odb),
            "PLACEMENT_SDC": str(BASE_RESULT / "3_place.sdc"),
            "RELOCATION_POOL": str(generated_pool),
            "RELOCATION_AUDIT": str(audit_path),
            "CANDIDATE_ODB_DIR": str(candidate_odb_dir),
            "IDLE_VCD": str(ROOT / "build/activity/gcd_baseline20ns/placement/idle.vcd"),
            "TYPICAL_VCD": str(PLACEMENT_VCD),
            "PEAK_VCD": str(ROOT / "build/activity/gcd_baseline20ns/placement/peak.vcd"),
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
        baseline_masters = {row["instance"]: row["master"] for row in read_tsv(baseline_cells)
                            if row["instance"].startswith(("clkload", "clkbuf", "clkinv"))}
        trials: list[dict[str, object]] = []
        for candidate in eligible:
            candidate_id = candidate["candidate_id"]
            variant = f"gcd_tier1opt_{output.name}_i{iteration:02d}_{candidate_id}"
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
                       if row["instance"].startswith(("clkload", "clkbuf", "clkinv"))}
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
    final_route_row: dict[str, object] | None = None
    if moved_instances and not args.skip_final_route:
        variant = f"gcd_tier1opt_{output.name}_final"
        env = os.environ.copy()
        env.update({
            "MOVE_CANDIDATE_ODB": str(output / "optimized.odb"),
            "CANDIDATE_FLOW_VARIANT": variant,
            "NUM_CORES": str(args.num_cores),
        })
        route = subprocess.run(["bash", str(ROOT / "scripts/run_gcd_candidate_spef_flow.sh")],
                               cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, check=False)
        (output / "final_route_driver.log").write_text(route.stdout)
        final_route_row = {
            "route_variant": variant,
            "return_code": route.returncode,
            "summary": str(ROOT / "build/power" / variant / "summary.txt"),
            "route_log": str(output / "final_route_driver.log"),
        }
        write_tsv(output / "final_route_manifest.tsv", [final_route_row])
        if route.returncode != 0:
            raise RuntimeError(f"final composed-placement route failed; see {output / 'final_route_driver.log'}")
    print(f"TIER1_OPT result={output / 'optimized.odb'} committed_moves={len(moved_instances)} "
          f"sha256={sha256(output / 'optimized.odb')} "
          f"final_route={final_route_row['summary'] if final_route_row else 'not_run'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
