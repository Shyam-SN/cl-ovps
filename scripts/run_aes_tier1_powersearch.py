#!/usr/bin/env python3
"""Run a frozen AES CTS-oracle placement search using total clock-cell internal power."""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLOW = ROOT / "tools/openroad-flow-scripts/flow"
DEFAULT_CONFIG = ROOT / "designs/aes/flow/config.mk"
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
WORK_HOME = ROOT / "build/orfs"
RESULTS = WORK_HOME / "results/sky130hd/aes"
GENERATOR = ROOT / "scripts/make_gcd_cross_row_pool.py"
INVENTORY_TCL = ROOT / "scripts/export_gcd_relocation_inventory.tcl"
ROWS_TCL = ROOT / "scripts/export_gcd_placement_rows.tcl"
CTS_EXPORT_TCL = ROOT / "scripts/export_gcd_cts_oracle_data.tcl"
MOVE_TCL = ROOT / "scripts/apply_generic_clock_sink_moves.tcl"
HPWL_TCL = ROOT / "build/power/aes_tier1_generalization_20261031g/analysis/aes_signal_hpwl.tcl"
POWER_TCL = ROOT / "scripts/aes_report_clock_cell_power.tcl"


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_openroad(tcl: Path, env: dict[str, str], log: Path) -> str:
    result = subprocess.run([str(OPENROAD), "-no_init", "-exit", str(tcl)], cwd=ROOT,
                            env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(result.stdout)
    if result.returncode:
        raise RuntimeError(f"OpenROAD Tcl failed; inspect {log}")
    return result.stdout


def run_make(config: Path, variant: str, targets: list[str], log: Path, cores: int) -> None:
    yosys = shutil.which("yosys")
    if not yosys:
        raise FileNotFoundError("Yosys is required by the ORFS Makefile")
    command = ["make", "-C", str(FLOW), f"DESIGN_CONFIG={config}", "DESIGN_NICKNAME=aes",
               f"FLOW_VARIANT={variant}", f"WORK_HOME={WORK_HOME}", f"OPENROAD_EXE={OPENROAD}",
               f"YOSYS_EXE={yosys}", f"NUM_CORES={cores}", *targets]
    result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(result.stdout)
    if result.returncode:
        raise RuntimeError(f"ORFS failed for {variant} ({targets}); inspect {log}")


def export_cts(odb: Path, out: Path, label: str) -> tuple[Path, Path]:
    sinks, cells = out / f"{label}_sinks.tsv", out / f"{label}_cells.tsv"
    env = os.environ.copy()
    env.update({"CTS_ORACLE_ODB": str(odb), "CTS_ORACLE_SINK_OUT": str(sinks),
                "CTS_ORACLE_CELL_OUT": str(cells)})
    run_openroad(CTS_EXPORT_TCL, env, out / f"{label}_cts_export.log")
    return sinks, cells


def clock_power(odb: Path, sdc: Path, out: Path, label: str) -> tuple[float, float, float]:
    env = os.environ.copy()
    env.update({"AES_POWER_ODB": str(odb), "AES_POWER_SDC": str(sdc), "AES_REPORT_DESIGN_POWER": "1"})
    text = run_openroad(POWER_TCL, env, out / f"{label}.clock_power.log")
    import re
    power_rows = re.compile(
        r"^\s*([0-9.eE+-]+)\s+[0-9.eE+-]+\s+[0-9.eE+-]+\s+[0-9.eE+-]+\s+(clk(?:buf|load|inv)[^\s]*)$",
        re.MULTILINE,
    )
    rows = power_rows.findall(text)
    counts = re.search(r"AES_CLOCK_CELL_POWER buffers=(\d+) dummies=(\d+) inverters=(\d+)", text)
    timing = re.search(r"AES_CLOCK_CELL_TIMING setup_ns=([0-9.eE+-]+) hold_ns=([0-9.eE+-]+)", text)
    if not rows or counts is None or timing is None:
        raise RuntimeError(f"incomplete clock-cell objective/timing report: {out / f'{label}.clock_power.log'}")
    total_internal = sum(float(internal) for internal, _name in rows)
    expected = sum(map(int, counts.groups()))
    if len({name for _internal, name in rows}) != len(rows) or len(rows) != expected:
        raise RuntimeError(f"clock-cell power rows incomplete: parsed {len(rows)}, inventory says {expected}")
    return total_internal, float(timing.group(1)), float(timing.group(2))


def hpwl(odb: Path, out: Path, label: str) -> int:
    result = out / f"{label}.signal_hpwl_dbu"
    env = os.environ.copy()
    env.update({"FEATURE_ODB": str(odb), "FEATURE_HPWL_OUT": str(result)})
    run_openroad(HPWL_TCL, env, out / f"{label}.signal_hpwl.log")
    return int(result.read_text().strip())


def make_aes_generator(path: Path) -> Path:
    source = GENERATOR.read_text()
    old = 'sink_rows = [row for row in sinks if row.get("branch", "").endswith("__leaf_clk")]'
    new = ('sink_rows = [row for row in sinks if row.get("branch", "").endswith("__leaf_clk") '
           'or (row.get("branch", "").startswith("clknet_leaf_") and row.get("branch", "").endswith("_clk"))]')
    if old not in source:
        raise ValueError("CTS leaf branch filter in the generic candidate generator changed")
    path.write_text(source.replace(old, new, 1))
    return path


def select_aes_target_sinks(sinks_path: Path, cells_path: Path, count: int) -> list[str]:
    """Spread AES candidates across clkbuf_8 dummy leaves and high-fanout leaves."""
    sinks = read_tsv(sinks_path)
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in sinks:
        branch = row["branch"]
        is_leaf = "__leaf_clk" in branch or (branch.startswith("clknet_leaf_") and branch.endswith("_clk"))
        if is_leaf:
            grouped.setdefault(branch, []).append(row)
    if not grouped:
        raise RuntimeError("CTS inventory has no AES leaf clock sinks")

    cells = read_tsv(cells_path)
    clkbuf8_branches = {
        row["input_net"] for row in cells
        if row["instance"].startswith("clkload") and "__clkbuf_8" in row["master"]
    }
    centroids = {
        branch: (sum(int(row["x_dbu"]) for row in rows) / len(rows),
                 sum(int(row["y_dbu"]) for row in rows) / len(rows))
        for branch, rows in grouped.items()
    }
    frontiers: dict[str, list[str]] = {}
    for branch, rows in grouped.items():
        cx, cy = centroids[branch]
        # Select sinks on the outside edge of each leaf first, where a legal
        # relocation is most likely to perturb CTS clustering and load balance.
        frontiers[branch] = [row["instance"] for row in sorted(
            rows, key=lambda row: (-((int(row["x_dbu"])-cx) ** 2 + (int(row["y_dbu"])-cy) ** 2),
                                   row["instance"]))]

    priority = sorted(clkbuf8_branches & grouped.keys(),
                      key=lambda branch: (-len(grouped[branch]), branch))
    high_fanout = sorted(grouped, key=lambda branch: (-len(grouped[branch]), branch))
    selected: list[str] = []
    # Round-robin across all clkbuf_8 leaves first, then across the heaviest
    # leaves. This avoids spending the entire pool on one branch.
    for branches in (priority, high_fanout):
        depth = 0
        while len(selected) < count:
            added = False
            for branch in branches:
                choices = frontiers[branch]
                if depth < len(choices):
                    instance = choices[depth]
                    if instance not in selected:
                        selected.append(instance)
                        added = True
                        if len(selected) >= count:
                            break
            if not added:
                break
            depth += 1
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-tag", required=True)
    parser.add_argument("--variant-prefix", required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--candidate-sinks", type=int, default=4)
    parser.add_argument("--moves-per-sink", type=int, default=2)
    parser.add_argument("--candidate-id-offset", type=int, default=39000)
    parser.add_argument("--candidate-pool", type=Path,
                        help="score an existing frozen relocation pool instead of generating new moves")
    parser.add_argument("--cores", type=int, default=4)
    parser.add_argument("--hpwl-limit-pct", type=float, default=1.5)
    parser.add_argument("--clock-internal-cutoff-uw", type=float, default=-1.0)
    args = parser.parse_args()

    config = args.config.resolve()
    output = args.output_dir.resolve()
    if not config.is_file() or output.exists():
        raise FileNotFoundError(config) if not config.is_file() else FileExistsError(output)
    base_dir = RESULTS / args.base_tag
    base_place = base_dir / "3_5_place_dp.odb"
    base_sdc = base_dir / "3_place.sdc"
    base_cts = base_dir / "4_1_cts.odb"
    for path in (base_place, base_sdc, base_cts, base_dir / "4_cts.sdc", base_dir / "5_1_grt.odb"):
        if not path.is_file():
            raise FileNotFoundError(f"AES 3.8 ns baseline checkpoint missing: {path}")

    output.mkdir(parents=True)
    analysis = output / "analysis"
    analysis.mkdir()
    inventory, rows = analysis / "inventory.tsv", analysis / "rows.tsv"
    env = os.environ.copy()
    env.update({"PLACEMENT_ODB": str(base_place), "RELOCATION_INVENTORY_OUT": str(inventory)})
    run_openroad(INVENTORY_TCL, env, analysis / "inventory.log")
    env = os.environ.copy()
    env.update({"PLACEMENT_ODB": str(base_place), "ROW_INVENTORY_OUT": str(rows)})
    run_openroad(ROWS_TCL, env, analysis / "rows.log")
    base_sinks, base_cells = export_cts(base_cts, analysis, "baseline")

    sys.path.insert(0, str(ROOT / "scripts"))
    selected = select_aes_target_sinks(base_sinks, base_cells, args.candidate_sinks)
    if not selected:
        raise RuntimeError("CTS oracle could not identify any AES boundary-targeted sinks")

    pool = analysis / "candidate_pool.tsv"
    if args.candidate_pool:
        frozen_pool = args.candidate_pool.resolve()
        if not frozen_pool.is_file():
            raise FileNotFoundError(frozen_pool)
        shutil.copy2(frozen_pool, pool)
        (analysis / "pool_generation.log").write_text(
            f"Using frozen candidate pool {frozen_pool}\nsha256={sha256(frozen_pool)}\n")
        generator = None
    else:
        generator = make_aes_generator(analysis / "make_aes_cross_row_pool.py")
        command = [sys.executable, str(generator), "--inventory", str(inventory), "--rows", str(rows),
                   "--cts-sinks", str(base_sinks), "--output", str(pool), "--seed", "20261102",
                   "--moves-per-sink", str(args.moves_per_sink),
                   "--min-move-dbu", "4000", "--max-move-dbu", "60000",
                   "--preferred-min-move-dbu", "20000", "--preferred-max-move-dbu", "45000",
                   "--candidate-id-offset", str(args.candidate_id_offset),
                   "--exclude-root", str(ROOT / "build/power"), "--allow-any-sink-count", "--allow-incomplete"]
        for instance in selected:
            command.extend(["--only-instance", instance])
        generated = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, check=False)
        (analysis / "pool_generation.log").write_text(generated.stdout)
        if generated.returncode:
            raise RuntimeError(f"AES CTS candidate generation failed; inspect {analysis / 'pool_generation.log'}")

    candidate_odb_dir = output / "candidate_odb"
    candidate_odb_dir.mkdir()
    move_audit = analysis / "move_audit.tsv"
    env = os.environ.copy()
    env.update({"PLACEMENT_ODB": str(base_place), "RELOCATION_POOL": str(pool),
                "CANDIDATE_ODB_DIR": str(candidate_odb_dir), "MOVE_AUDIT": str(move_audit)})
    run_openroad(MOVE_TCL, env, analysis / "move_export.log")
    saved = [row for row in read_tsv(move_audit) if row["status"] == "saved"]
    if not saved:
        raise RuntimeError("no legal candidates survived the OpenDB move check")

    base_power, base_setup, base_hold = clock_power(base_cts, base_dir / "4_cts.sdc", analysis, "baseline")
    base_hpwl = hpwl(base_place, analysis, "baseline")
    predictions: list[dict[str, object]] = []
    for proposal in saved:
        candidate = proposal["candidate_id"]
        variant = f"{args.variant_prefix}_{candidate}"
        result_dir = RESULTS / variant
        result_dir.mkdir(parents=True)
        shutil.copy2(proposal["odb"], result_dir / "3_5_place_dp.odb")
        shutil.copy2(proposal["odb"], result_dir / "3_place.odb")
        shutil.copy2(base_sdc, result_dir / "3_place.sdc")
        run_make(config, variant, ["do-cts"], output / f"{candidate}.cts.log", args.cores)
        candidate_cts = result_dir / "4_1_cts.odb"
        candidate_power, candidate_setup, candidate_hold = clock_power(
            candidate_cts, result_dir / "4_cts.sdc", analysis, candidate)
        candidate_hpwl = hpwl(Path(proposal["odb"]), analysis, candidate)
        hpwl_delta_pct = 100.0 * (candidate_hpwl - base_hpwl) / base_hpwl
        power_delta_uw = (candidate_power - base_power) * 1e6
        setup_delta_ps = (candidate_setup - base_setup) * 1000.0
        hold_delta_ps = (candidate_hold - base_hold) * 1000.0
        predictions.append({
            **proposal,
            "candidate_variant": variant,
            "candidate_place_odb_sha256": sha256(Path(proposal["odb"])),
            "signal_hpwl_delta_pct": hpwl_delta_pct,
            "baseline_total_clock_internal_uw": base_power * 1e6,
            "candidate_total_clock_internal_uw": candidate_power * 1e6,
            "total_clock_internal_delta_uw": power_delta_uw,
            "setup_delta_ps": setup_delta_ps,
            "hold_delta_ps": hold_delta_ps,
            "timing_50ps_pre_route_pass": setup_delta_ps >= -50.0 and hold_delta_ps >= -50.0,
            "tier1_gate_pass": (power_delta_uw <= args.clock_internal_cutoff_uw
                                and hpwl_delta_pct <= args.hpwl_limit_pct
                                and setup_delta_ps >= -50.0 and hold_delta_ps >= -50.0),
        })

    predictions.sort(key=lambda row: (float(row["total_clock_internal_delta_uw"]),
                                      float(row["signal_hpwl_delta_pct"]), str(row["candidate_id"])))
    write_tsv(analysis / "frozen_tier1_predictions.tsv", predictions)
    if not predictions:
        raise RuntimeError("empty candidate score table")
    passing = [row for row in predictions if row["tier1_gate_pass"]]
    winner = passing[0] if passing else None
    if winner:
        shutil.copy2(candidate_odb_dir / f"{winner['candidate_id']}.odb", output / "selected_tier1_place.odb")

    integrity_paths = [config, ROOT / "designs/aes/flow/constraint_3p8.sdc", POWER_TCL,
                       GENERATOR, INVENTORY_TCL, ROWS_TCL, CTS_EXPORT_TCL, MOVE_TCL,
                       base_place, base_sdc, base_cts, base_dir / "4_cts.sdc", base_dir / "5_1_grt.odb",
                       pool, move_audit, analysis / "frozen_tier1_predictions.tsv"]
    if generator is not None:
        integrity_paths.append(generator)
    integrity_paths.extend(Path(row["odb"]) for row in saved)
    integrity_rows = [{"path": str(path.resolve()), "sha256": sha256(path), "size_bytes": path.stat().st_size}
                      for path in sorted(set(integrity_paths)) if path.is_file()]
    write_tsv(analysis / "frozen_prediction_integrity.tsv", integrity_rows)
    write_tsv(analysis / "pre_route_freeze.tsv", [{
        "candidate_count": len(predictions),
        "gate_pass_count": len(passing),
        "winner": winner["candidate_id"] if winner else "NONE",
        "objective": "total clkbuf+clkload+clkinv internal power; no leakage or net switching",
        "score_sha256": sha256(analysis / "frozen_tier1_predictions.tsv"),
        "integrity_sha256": sha256(analysis / "frozen_prediction_integrity.tsv"),
    }])
    (analysis / "tier1_selection_report.md").write_text(
        "# AES 3.8 ns Tier-1 CTS-oracle search\n\n"
        f"Baseline variant: `{args.base_tag}`. Candidates: {len(predictions)}.\n\n"
        f"The frozen objective sums per-instance Liberty internal power for `clkbuf*`, `clkload*`, and `clkinv*`. "
        f"Tier-1 acceptance requires at least {-args.clock_internal_cutoff_uw:g} uW reduction, signal HPWL "
        f"increase <= {args.hpwl_limit_pct:g}%, and no more than 50 ps pre-route setup or hold loss.\n\n"
        + (f"Selected automatically: `{winner['candidate_id']}`; predicted total clock-cell internal delta "
           f"{float(winner['total_clock_internal_delta_uw']):.6f} uW; signal HPWL delta "
           f"{float(winner['signal_hpwl_delta_pct']):.4f}%.\n" if winner else
           "No candidate passed the frozen Tier-1 gate. No candidate was selected.\n")
    )
    print(f"AES_TIER1_POWERSEARCH candidates={len(predictions)} pass={len(passing)} "
          f"winner={winner['candidate_id'] if winner else 'NONE'} "
          f"output={analysis / 'tier1_selection_report.md'}")
    if not winner:
        raise RuntimeError("no AES relocation passed the frozen Tier-1 gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
