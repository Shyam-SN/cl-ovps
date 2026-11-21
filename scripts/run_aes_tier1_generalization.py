#!/usr/bin/env python3
"""Probe CTS dummy transitions and GRT-vs-SPEF capacitance on ORFS AES/Sky130HD."""

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
CONFIG = ROOT / "tools/openroad-flow-scripts/flow/designs/sky130hd/aes/config.mk"
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
WORK_HOME = ROOT / "build/orfs"
RESULTS = WORK_HOME / "results/sky130hd/aes"
BASE_TAG = "aes_tier1gen_20261031_base"
GENERATOR = ROOT / "scripts/make_gcd_cross_row_pool.py"
INVENTORY_TCL = ROOT / "scripts/export_gcd_relocation_inventory.tcl"
ROWS_TCL = ROOT / "scripts/export_gcd_placement_rows.tcl"
CTS_EXPORT_TCL = ROOT / "scripts/export_gcd_cts_oracle_data.tcl"
MOVE_TCL = ROOT / "scripts/apply_generic_clock_sink_moves.tcl"
GRT_CAP_TCL = ROOT / "scripts/report_global_route_capacitance.tcl"
SPEF_CAP_TCL = ROOT / "scripts/report_spef_capacitance.tcl"
LIBERTY = ROOT / "tools/openroad-flow-scripts/flow/platforms/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib"


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
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_aes_pool_generator(destination: Path) -> Path:
    """Adapt only the GCD generator's leaf-net name test for ORFS AES naming."""
    source = GENERATOR.read_text()
    old = 'sink_rows = [row for row in sinks if row.get("branch", "").endswith("__leaf_clk")]'
    new = ('sink_rows = [row for row in sinks if row.get("branch", "").endswith("__leaf_clk") '
           'or (row.get("branch", "").startswith("clknet_leaf_") and row.get("branch", "").endswith("_clk"))]')
    if old not in source:
        raise ValueError("the source candidate generator changed; review its leaf-name filter before reuse")
    destination.write_text(source.replace(old, new, 1))
    return destination


def run_openroad(tcl: Path, env: dict[str, str], log: Path) -> None:
    process = subprocess.run([str(OPENROAD), "-no_init", "-exit", str(tcl)], cwd=ROOT, env=env,
                             text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(process.stdout)
    if process.returncode:
        raise RuntimeError(f"OpenROAD Tcl failed; see {log}")


def run_make(variant: str, targets: list[str], log: Path, cores: int) -> None:
    yosys = shutil.which("yosys")
    if not yosys:
        raise FileNotFoundError("Yosys is required for a clean AES ORFS build")
    command = ["make", "-C", str(FLOW), f"DESIGN_CONFIG={CONFIG}", "DESIGN_NICKNAME=aes",
               f"FLOW_VARIANT={variant}", f"WORK_HOME={WORK_HOME}", f"OPENROAD_EXE={OPENROAD}",
               f"YOSYS_EXE={yosys}", f"NUM_CORES={cores}", *targets]
    process = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, check=False)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(process.stdout)
    if process.returncode:
        raise RuntimeError(f"AES OpenROAD flow failed ({variant}, {targets}); see {log}")


def export_cts(odb: Path, out_dir: Path, name: str) -> tuple[Path, Path]:
    sinks, cells = out_dir / f"{name}_sinks.tsv", out_dir / f"{name}_cells.tsv"
    env = os.environ.copy()
    env.update({"CTS_ORACLE_ODB": str(odb), "CTS_ORACLE_SINK_OUT": str(sinks), "CTS_ORACLE_CELL_OUT": str(cells)})
    run_openroad(CTS_EXPORT_TCL, env, out_dir / f"{name}.cts_export.log")
    return sinks, cells


def parse_cap_report(path: Path) -> dict[str, float]:
    result = {"signal": 0.0, "clock": 0.0}
    with path.open() as source:
        for line in source:
            if not line.startswith("#GRT_CAP\t"):
                continue
            _, category, _net, _pin, _wire, total = line.rstrip("\n").split("\t")
            if category == "class":
                continue
            result[category] += float(total)
    return result


def parse_spef_report(path: Path) -> dict[str, float]:
    result = {"signal": 0.0, "clock": 0.0}
    with path.open() as source:
        for line in source:
            if not line.startswith("#SPEF_CAP\t"):
                continue
            _, category, _net, _pin, _wire, total = line.rstrip("\n").split("\t")
            if category == "class":
                continue
            result[category] += float(total)
    return result


def cap_export(variant: str, stage: str, output: Path, log: Path) -> dict[str, float]:
    result_dir = RESULTS / variant
    if stage == "grt":
        env = os.environ.copy()
        env.update({"STAGE_ODB": str(result_dir / "5_1_grt.odb"),
                    "STAGE_SDC": str(result_dir / "5_1_grt.sdc"), "GRT_CAP_REPORT": str(output)})
        run_openroad(GRT_CAP_TCL, env, log)
        return parse_cap_report(output)
    env = os.environ.copy()
    env.update({"SPEF_ODB": str(result_dir / "6_final.odb"),
                "SPEF_SDC": str(result_dir / "6_final.sdc"),
                "SPEF_FILE": str(result_dir / "6_final.spef"), "SPEF_CAP_REPORT": str(output)})
    run_openroad(SPEF_CAP_TCL, env, log)
    return parse_spef_report(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("build/power/aes_tier1_generalization_20261031"))
    parser.add_argument("--candidate-sinks", type=int, default=4)
    parser.add_argument("--moves-per-sink", type=int, default=2)
    parser.add_argument("--cores", type=int, default=2)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    output = (ROOT / args.output_dir).resolve() if not args.output_dir.is_absolute() else args.output_dir.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite AES experiment {output}")
    if not CONFIG.is_file() or not LIBERTY.is_file():
        raise FileNotFoundError("ORFS AES/Sky130HD config or liberty is missing")
    output.mkdir(parents=True)
    analysis = output / "analysis"
    analysis.mkdir()
    baseline_dir = RESULTS / BASE_TAG

    # Baseline goes only through global route. No GRT score is changed after this point.
    baseline_place = baseline_dir / "3_5_place_dp.odb"
    baseline_sdc = baseline_dir / "3_place.sdc"
    baseline_cts_odb = baseline_dir / "4_1_cts.odb"
    baseline_required = (baseline_place, baseline_sdc, baseline_cts_odb, baseline_dir / "5_1_grt.odb")
    if not all(path.is_file() for path in baseline_required):
        # ORFS `do-grt` is a leaf target; build its prerequisites explicitly.
        run_make(BASE_TAG, ["synth", "do-floorplan", "do-place", "do-cts", "do-grt"],
                 output / "baseline_grt_flow.log", args.cores)
    for path in (baseline_place, baseline_sdc, baseline_cts_odb, baseline_dir / "5_1_grt.odb"):
        if not path.is_file():
            raise FileNotFoundError(path)

    inventory = analysis / "inventory.tsv"
    rows_path = analysis / "rows.tsv"
    env = os.environ.copy()
    env.update({"PLACEMENT_ODB": str(baseline_place), "RELOCATION_INVENTORY_OUT": str(inventory)})
    run_openroad(INVENTORY_TCL, env, analysis / "inventory.log")
    env = os.environ.copy()
    env.update({"PLACEMENT_ODB": str(baseline_place), "ROW_INVENTORY_OUT": str(rows_path)})
    run_openroad(ROWS_TCL, env, analysis / "rows.log")
    base_sinks, base_cells = export_cts(baseline_cts_odb, analysis, "baseline")

    # Reuse the calibrated deficit/frontier ordering from the GCD Tier-1 selector,
    # while sampling the larger AES sink set through the generic count mode.
    sys.path.insert(0, str(ROOT / "scripts"))
    from gcd_tier1_greedy_optimizer import select_boundary_sinks
    selected = select_boundary_sinks(base_sinks, base_cells, set(), args.candidate_sinks, (4.8365,))
    pool = analysis / "candidate_pool.tsv"
    generic_generator = make_aes_pool_generator(analysis / "make_aes_cross_row_pool.py")
    command = [sys.executable, str(generic_generator), "--inventory", str(inventory), "--rows", str(rows_path),
               "--cts-sinks", str(base_sinks), "--output", str(pool), "--seed", "20261031",
               "--moves-per-sink", str(args.moves_per_sink), "--candidate-id-offset", "9000",
               "--min-move-dbu", "4000", "--max-move-dbu", "60000",
               "--preferred-min-move-dbu", "20000", "--preferred-max-move-dbu", "45000",
               "--exclude-root", str(ROOT / "build/power"), "--allow-any-sink-count", "--allow-incomplete"]
    for instance in selected:
        command.extend(["--only-instance", instance])
    process = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, check=False)
    (analysis / "pool_generation.log").write_text(process.stdout)
    if process.returncode:
        raise RuntimeError(f"AES relocation pool generation failed; see {analysis / 'pool_generation.log'}")

    candidate_odb_dir = output / "candidate_odb"
    candidate_odb_dir.mkdir()
    move_audit = analysis / "move_audit.tsv"
    env = os.environ.copy()
    env.update({"PLACEMENT_ODB": str(baseline_place), "RELOCATION_POOL": str(pool),
                "CANDIDATE_ODB_DIR": str(candidate_odb_dir), "MOVE_AUDIT": str(move_audit)})
    run_openroad(MOVE_TCL, env, analysis / "move_export.log")
    saved = [row for row in read_tsv(move_audit) if row["status"] == "saved"]
    if len(saved) < 4:
        raise RuntimeError(f"only {len(saved)} legal AES placement trials were generated; need at least 4")

    # Each isolated candidate gets an independent CTS+GRT run from the baseline placement.
    candidate_variants: dict[str, str] = {}
    for row in saved:
        variant = f"aes_tier1gen_20261031_{row['candidate_id']}"
        result_dir = RESULTS / variant
        result_dir.mkdir(parents=True)
        shutil.copy2(row["odb"], result_dir / "3_5_place_dp.odb")
        shutil.copy2(row["odb"], result_dir / "3_place.odb")
        shutil.copy2(baseline_sdc, result_dir / "3_place.sdc")
        run_make(variant, ["do-cts", "do-grt"], output / f"{row['candidate_id']}.grt_flow.log", args.cores)
        candidate_variants[row["candidate_id"]] = variant

    all_variants = {"baseline": BASE_TAG, **candidate_variants}
    cts_inventory: dict[str, tuple[Path, Path]] = {"baseline": (base_sinks, base_cells)}
    grt_caps: dict[str, dict[str, float]] = {}
    prediction_rows: list[dict[str, object]] = []
    for candidate, variant in all_variants.items():
        result_dir = RESULTS / variant
        if candidate != "baseline":
            cts_inventory[candidate] = export_cts(result_dir / "4_1_cts.odb", analysis, candidate)
        cap_path = analysis / f"{candidate}.grt_caps.tsv"
        grt_caps[candidate] = cap_export(variant, "grt", cap_path, analysis / f"{candidate}.grt_caps.log")

    base_sink_map = {row["instance"]: row["branch"] for row in read_tsv(base_sinks)}
    base_dummy_map = {row["instance"]: row["master"] for row in read_tsv(base_cells) if row["instance"].startswith("clkload")}
    move_by_id = {row["candidate_id"]: row for row in saved}
    for candidate, variant in candidate_variants.items():
        sink_file, cell_file = cts_inventory[candidate]
        sink_map = {row["instance"]: row["branch"] for row in read_tsv(sink_file)}
        dummy_map = {row["instance"]: row["master"] for row in read_tsv(cell_file) if row["instance"].startswith("clkload")}
        changed_dummies = sorted(name for name in base_dummy_map.keys() | dummy_map.keys()
                                 if base_dummy_map.get(name) != dummy_map.get(name))
        branch_changes = sum(base_sink_map.get(name) != sink_map.get(name)
                             for name in base_sink_map.keys() | sink_map.keys())
        proposal = move_by_id[candidate]
        prediction_rows.append({
            "candidate_id": candidate,
            "instance": proposal["instance"],
            "from_x_dbu": proposal["from_x_dbu"], "from_y_dbu": proposal["from_y_dbu"],
            "target_x_dbu": proposal["target_x_dbu"], "target_y_dbu": proposal["target_y_dbu"],
            "move_type": proposal["move_type"],
            "cts_sink_count": len(sink_map), "cts_leaf_count": len(set(sink_map.values())),
            "cts_sink_branch_changes": branch_changes,
            "dummy_master_changes": ";".join(
                f"{name}:{base_dummy_map.get(name, 'ABSENT')}->{dummy_map.get(name, 'ABSENT')}" for name in changed_dummies),
            "dummy_master_change_count": len(changed_dummies),
            "grt_signal_cap_delta_ff": (grt_caps[candidate]["signal"] - grt_caps["baseline"]["signal"]) * 1e15,
            "grt_clock_cap_delta_ff": (grt_caps[candidate]["clock"] - grt_caps["baseline"]["clock"]) * 1e15,
            "candidate_flow_variant": variant,
        })
    write_tsv(analysis / "frozen_grt_cap_predictions.tsv", prediction_rows)

    # Freeze the structural/GRT holdout before any detailed route is started.
    hashes: list[dict[str, object]] = []
    source_paths = [CONFIG, LIBERTY, GENERATOR, generic_generator, INVENTORY_TCL, ROWS_TCL, CTS_EXPORT_TCL,
                    MOVE_TCL, GRT_CAP_TCL, SPEF_CAP_TCL, OPENROAD, baseline_place, baseline_sdc,
                    baseline_cts_odb, baseline_dir / "5_1_grt.odb", pool, move_audit,
                    analysis / "frozen_grt_cap_predictions.tsv"]
    source_paths.extend(candidate_odb_dir.glob("*.odb"))
    for variant in candidate_variants.values():
        result_dir = RESULTS / variant
        source_paths.extend([result_dir / "3_5_place_dp.odb", result_dir / "4_1_cts.odb", result_dir / "5_1_grt.odb"])
    for path in sorted({Path(p).resolve() for p in source_paths if Path(p).is_file()}):
        hashes.append({"path": str(path), "sha256": sha256(path), "size_bytes": path.stat().st_size})
    write_tsv(analysis / "frozen_prediction_integrity.tsv", hashes)
    marker = analysis / "pre_detail_route_freeze.tsv"
    write_tsv(marker, [
        {"metric": "frozen_before_detailed_route", "value": "YES"},
        {"metric": "candidate_count", "value": len(prediction_rows)},
        {"metric": "prediction_sha256", "value": sha256(analysis / "frozen_grt_cap_predictions.tsv")},
        {"metric": "integrity_manifest_sha256", "value": sha256(analysis / "frozen_prediction_integrity.tsv")},
    ])

    # Route baseline and every trial only after the above freeze.
    for variant in all_variants.values():
        run_make(variant, ["do-route", "do-finish"], output / f"{variant}.detail_route.log", args.cores)

    base_spef = cap_export(BASE_TAG, "spef", analysis / "baseline.spef_caps.tsv", analysis / "baseline.spef_caps.log")
    result_rows: list[dict[str, object]] = []
    grt_signal, route_signal, grt_clock, route_clock = [], [], [], []
    for row in prediction_rows:
        candidate = str(row["candidate_id"])
        variant = str(row["candidate_flow_variant"])
        route_caps = cap_export(variant, "spef", analysis / f"{candidate}.spef_caps.tsv", analysis / f"{candidate}.spef_caps.log")
        actual_signal = (route_caps["signal"] - base_spef["signal"]) * 1e15
        actual_clock = (route_caps["clock"] - base_spef["clock"]) * 1e15
        row.update({"routed_signal_cap_delta_ff": actual_signal, "routed_clock_cap_delta_ff": actual_clock})
        result_rows.append(row)
        grt_signal.append(float(row["grt_signal_cap_delta_ff"]))
        route_signal.append(actual_signal)
        grt_clock.append(float(row["grt_clock_cap_delta_ff"]))
        route_clock.append(actual_clock)
    write_tsv(analysis / "grt_vs_spef_capacitance.tsv", result_rows)
    from analyze_gcd_cts_branch_proxy import kendall_tau_b, spearman
    correlations = [
        {"component": "signal_cap_delta", "n": len(result_rows),
         "spearman_rho": spearman(grt_signal, route_signal), "kendall_tau_b": kendall_tau_b(grt_signal, route_signal)},
        {"component": "clock_cap_delta", "n": len(result_rows),
         "spearman_rho": spearman(grt_clock, route_clock), "kendall_tau_b": kendall_tau_b(grt_clock, route_clock)},
    ]
    write_tsv(analysis / "correlation_metrics.tsv", correlations)
    (analysis / "report.md").write_text(
        "# AES/Sky130HD Tier-1 and GRT generalization probe\n\n"
        f"CTS sinks in baseline: {len(base_sink_map)} across {len(set(base_sink_map.values()))} leaves.\n"
        f"Isolated cross-row trials routed: {len(result_rows)}.\n"
        f"Candidates with at least one CTS dummy-master change: {sum(int(r['dummy_master_change_count'])>0 for r in result_rows)}.\n\n"
        "GRT capacitance predictions were frozen before detailed routing. The post-route comparison uses total "
        "signal and clock capacitance from SPEF; no AES workload VCD was available, so this is a physical-capacitance "
        "generalization check, not a dynamic-power result.\n\n"
        f"Signal-cap GRT vs SPEF Spearman rho: {correlations[0]['spearman_rho']:.3f}; "
        f"Kendall tau-b: {correlations[0]['kendall_tau_b']:.3f}.\n"
        f"Clock-cap GRT vs SPEF Spearman rho: {correlations[1]['spearman_rho']:.3f}; "
        f"Kendall tau-b: {correlations[1]['kendall_tau_b']:.3f}.\n"
    )
    print(f"AES_TIER1_GENERALIZATION candidates={len(result_rows)} dummy_events="
          f"{sum(int(r['dummy_master_change_count'])>0 for r in result_rows)} "
          f"signal_rho={correlations[0]['spearman_rho']:.3f} output={analysis / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
