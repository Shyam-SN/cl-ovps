#!/usr/bin/env python3
"""Route and validate the AES 3.8 ns Tier-1/clock-restructure/VCD-optimization pipeline."""

from __future__ import annotations

import argparse
import csv
import itertools
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLOW = ROOT / "tools/openroad-flow-scripts/flow"
CONFIG = ROOT / "designs/aes/flow/config.mk"
WORK_HOME = ROOT / "build/orfs"
RESULTS = WORK_HOME / "results/sky130hd/aes"
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
STAGE2_TCL = ROOT / "scripts/stage2_clock_restructurer.tcl"
STAGE3_TCL = ROOT / "scripts/stage3_vcd_opt.tcl"
LEVELS_TCL = ROOT / "scripts/export_clock_tree_levels.tcl"
ACTIVITY_SCRIPT = ROOT / "scripts/run_aes_power_activity.py"
REPORT_SCRIPT = ROOT / "scripts/run_aes_power_reports.py"
PROFILE_SCRIPT = ROOT / "scripts/profile_aes_netlist.py"
POWER_TCL = ROOT / "scripts/aes_report_clock_cell_power.tcl"
SINK_POWER_TCL = ROOT / "scripts/aes_report_spef_power.tcl"
EXPORT_VERILOG_TCL = ROOT / "scripts/export_gcd_odb_verilog.tcl"
LEC_SCRIPT = ROOT / "scripts/run_aes_netlist_lec.py"


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


def run_openroad(tcl: Path, env: dict[str, str], log: Path) -> None:
    result = subprocess.run([str(OPENROAD), "-no_init", "-exit", str(tcl)], cwd=ROOT,
                            env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(result.stdout)
    if result.returncode:
        raise RuntimeError(f"OpenROAD transform failed; inspect {log}")


def run_make(variant: str, targets: list[str], log: Path, cores: int,
             setup_margin: float | None = None) -> None:
    yosys = shutil.which("yosys")
    if not yosys:
        raise FileNotFoundError("Yosys is required by ORFS")
    command = ["make", "-C", str(FLOW), f"DESIGN_CONFIG={CONFIG}", "DESIGN_NICKNAME=aes",
               f"FLOW_VARIANT={variant}", f"WORK_HOME={WORK_HOME}", f"OPENROAD_EXE={OPENROAD}",
               f"YOSYS_EXE={yosys}", f"NUM_CORES={cores}"]
    if setup_margin is not None:
        command.append(f"SETUP_SLACK_MARGIN={setup_margin:g}")
    command.extend(targets)
    result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(result.stdout)
    if result.returncode:
        raise RuntimeError(f"ORFS failed for {variant} ({targets}); inspect {log}")


def assert_route_clean(variant: str, require_leaf_ndr: bool = True) -> None:
    log_dir = ROOT / "build/orfs/logs/sky130hd/aes" / variant
    grt_log, drt_log = log_dir / "5_1_grt.log", log_dir / "5_2_route.log"
    spef = RESULTS / variant / "6_final.spef"
    if not grt_log.is_file() or not drt_log.is_file() or not spef.is_file():
        raise FileNotFoundError(f"routed sign-off artifacts missing for {variant}")
    if drt_log.stat().st_mtime < grt_log.stat().st_mtime or spef.stat().st_mtime < drt_log.stat().st_mtime:
        raise RuntimeError(f"stale DRT/SPEF artifacts for {variant}; reroute before sign-off")
    grt_text = grt_log.read_text(errors="replace")
    drt_text = drt_log.read_text(errors="replace")
    if require_leaf_ndr and not re.search(
            r"AES_LEAF_CLOCK_NDR .*width_factor=1 spacing_factor=2 nets=[1-9][0-9]*", grt_text):
        raise RuntimeError(f"leaf-clock 1x/2x NDR was not applied to {variant}")
    violations = re.findall(r"Number of violations\s*=\s*(\d+)", drt_text)
    antenna_nets = re.findall(r"Found\s+(\d+) net violations", drt_text)
    antenna_pins = re.findall(r"Found\s+(\d+) pin violations", drt_text)
    if not violations or int(violations[-1]) != 0:
        raise RuntimeError(f"TritonRoute DRC violations remain for {variant}: {violations[-3:]}")
    if not antenna_nets or not antenna_pins or int(antenna_nets[-1]) or int(antenna_pins[-1]):
        raise RuntimeError(f"antenna violations remain for {variant}: nets={antenna_nets[-2:]} pins={antenna_pins[-2:]}")


def clock_power(odb: Path, sdc: Path, out: Path, label: str) -> tuple[float, float, float]:
    env = os.environ.copy()
    env.update({"AES_POWER_ODB": str(odb), "AES_POWER_SDC": str(sdc), "AES_REPORT_DESIGN_POWER": "1"})
    run_openroad(POWER_TCL, env, out / f"{label}.clock_power.log")
    import re
    text = (out / f"{label}.clock_power.log").read_text(errors="replace")
    row_pattern = re.compile(
        r"^\s*([0-9.eE+-]+)\s+[0-9.eE+-]+\s+[0-9.eE+-]+\s+[0-9.eE+-]+\s+(clk(?:buf|load|inv)[^\s]*)$",
        re.MULTILINE,
    )
    rows = row_pattern.findall(text)
    timing = re.search(r"AES_CLOCK_CELL_TIMING setup_ns=([0-9.eE+-]+) hold_ns=([0-9.eE+-]+)", text)
    counts = re.search(r"AES_CLOCK_CELL_POWER buffers=(\d+) dummies=(\d+) inverters=(\d+)", text)
    if not rows or timing is None or counts is None or len(rows) != sum(map(int, counts.groups())):
        raise RuntimeError(f"incomplete clock-cell objective report in {out / f'{label}.clock_power.log'}")
    return sum(float(row[0]) for row in rows), float(timing.group(1)), float(timing.group(2))


def transform(action_tcl: Path, action: str, source_odb: Path, sdc: Path, vcd: Path,
              report: Path, output_odb: Path, analysis: Path, extra_env: dict[str, str] | None = None) -> None:
    env = os.environ.copy()
    env.update({"CLOCK_RESTRUCTURE_ACTION": action, "VCD_OPT_ACTION": action,
                "AUDIT_INPUT_ODB": str(source_odb), "AUDIT_SDC": str(sdc),
                "ACTIVITY_VCD": str(vcd), "AUDIT_REPORT": str(report),
                "AUDIT_OUT_ODB": str(output_odb), "AUDIT_SKIP_UPF": "1",
                "AUDIT_ACTIVITY_SCOPE": "tb_aes_activity/dut"})
    if extra_env:
        env.update(extra_env)
    run_openroad(action_tcl, env, analysis / f"{report.stem}.openroad.log")
    text = (analysis / f"{report.stem}.openroad.log").read_text(errors="replace")
    annotation = re.search(r"(?im)^unannotated\s+(\d+)\s*$", text)
    if annotation is None:
        raise RuntimeError(
            f"activity annotation summary is missing; refusing to accept {report.stem}")
    if int(annotation.group(1)) != 0:
        raise RuntimeError(
            f"activity annotation gate failed for {report.stem}: "
            f"{annotation.group(1)} pins are unannotated")
    if "AUDIT_PREFLIGHT_GATE=PASS" not in text:
        raise RuntimeError(f"pre-route timing/polarity guard did not pass; inspect {analysis / f'{report.stem}.openroad.log'}")


def generate_typical_activity(variant: str, output_dir: Path) -> Path:
    """Generate a VCD from this exact routed stage before the next VCD-guided pass."""
    subprocess.run([sys.executable, str(ACTIVITY_SCRIPT), "--variants", variant,
                    "--workloads", "typical", "--output-dir", str(output_dir)],
                   cwd=ROOT, check=True)
    vcd = output_dir / variant / "typical/aes_activity.vcd"
    if not vcd.is_file():
        raise FileNotFoundError(f"stage-matched AES VCD was not generated: {vcd}")
    return vcd


def prove_odb_equivalence(baseline_netlist: Path, candidate_odb: Path,
                          analysis: Path, label: str) -> bool:
    candidate_netlist = analysis / f"{label}.v"
    env = os.environ.copy()
    env.update({"AUDIT_INPUT_ODB": str(candidate_odb), "AUDIT_VERILOG_OUT": str(candidate_netlist)})
    run_openroad(EXPORT_VERILOG_TCL, env, analysis / f"{label}.export.log")
    return prove_netlist_equivalence(baseline_netlist, candidate_netlist, analysis / f"lec_{label}")


def prove_netlist_equivalence(baseline_netlist: Path, candidate_netlist: Path,
                              output: Path) -> bool:
    command = [sys.executable, str(LEC_SCRIPT), "--baseline-netlist", str(baseline_netlist),
               "--candidate-netlist", str(candidate_netlist), "--output-dir", str(output)]
    result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    (output.parent / f"{output.name}.driver.log").write_text(result.stdout)
    return (result.returncode == 0 and (output / "status.txt").is_file()
            and (output / "status.txt").read_text().strip() == "LEC_PASS")


def install_transformed_checkpoint(source_variant: str, new_variant: str, transformed_odb: Path,
                                   output: Path, cores: int,
                                   setup_margin: float | None = None) -> None:
    source_dir = RESULTS / source_variant
    target_dir = RESULTS / new_variant
    if target_dir.exists():
        raise FileExistsError(target_dir)
    target_dir.mkdir(parents=True)
    for filename in ("3_5_place_dp.odb", "3_place.odb", "3_place.sdc", "4_cts.sdc"):
        shutil.copy2(source_dir / filename, target_dir / filename)
    shutil.copy2(transformed_odb, target_dir / "4_1_cts.odb")
    shutil.copy2(transformed_odb, target_dir / "4_cts.odb")
    run_make(new_variant, ["do-grt", "do-route", "do-finish"],
             output / f"{new_variant}.route.log", cores, setup_margin)
    assert_route_clean(new_variant)


def clone_placement_checkpoint(source_variant: str, new_variant: str) -> None:
    """Give a frozen placement candidate a fresh route variant for matched timing repair."""
    source_dir, target_dir = RESULTS / source_variant, RESULTS / new_variant
    if target_dir.exists():
        raise FileExistsError(target_dir)
    target_dir.mkdir(parents=True)
    for filename in ("3_5_place_dp.odb", "3_place.odb", "3_place.sdc",
                     "4_1_cts.odb", "4_cts.odb", "4_cts.sdc"):
        shutil.copy2(source_dir / filename, target_dir / filename)


def assert_setup_margin(variant: str, expected_margin: float) -> None:
    log = ROOT / "build/orfs/logs/sky130hd/aes" / variant / "5_1_grt.log"
    if not log.is_file():
        raise FileNotFoundError(log)
    match = re.search(r"repair_timing\s+-setup_margin\s+([0-9.eE+-]+)",
                      log.read_text(errors="replace"))
    if match is None or abs(float(match.group(1)) - expected_margin) > 1.0e-9:
        raise RuntimeError(f"{variant} was not routed with the requested setup margin "
                           f"({expected_margin:g} ns)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-tag", required=True)
    parser.add_argument("--tier1-experiment", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cores", type=int, default=4)
    parser.add_argument("--max-downsize-cells", type=int, default=40)
    parser.add_argument("--downsize-budget-ps", type=float, default=50.0)
    parser.add_argument("--setup-margin", type=float,
                        help="apply the same OpenROAD setup repair margin to each routed stage; "
                             "baseline-tag must already use this margin")
    args = parser.parse_args()

    output = args.output_dir.resolve()
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    analysis = output / "analysis"
    analysis.mkdir()
    baseline_dir = RESULTS / args.baseline_tag
    tier1_dir = args.tier1_experiment.resolve()
    predictions = read_tsv(tier1_dir / "analysis/frozen_tier1_predictions.tsv")
    frozen = read_tsv(tier1_dir / "analysis/pre_route_freeze.tsv")[0]
    winner_id = frozen["winner"]
    if winner_id == "NONE":
        # A clean no-move Stage 1 result is still valid: the frozen oracle
        # rejected every relocation, so continue with clock restructuring from
        # the locked baseline rather than forcing an unapproved placement move.
        tier1_variant = args.baseline_tag
        winner_row = None
    else:
        winner_row = next(row for row in predictions if row["candidate_id"] == winner_id)
        tier1_variant = winner_row["candidate_variant"]
    tier1_dir_result = RESULTS / tier1_variant
    for path in (baseline_dir / "6_final.spef", baseline_dir / "6_final.v",
                 tier1_dir_result / "4_1_cts.odb", tier1_dir_result / "4_cts.sdc"):
        if not path.is_file():
            raise FileNotFoundError(path)
    assert_route_clean(args.baseline_tag)
    if args.setup_margin is not None:
        assert_setup_margin(args.baseline_tag, args.setup_margin)

    # Route the already-scored Stage-1 winner; no score is changed after the frozen table.
    # If the oracle rejected every move, the baseline is the frozen Stage-1 state.
    if tier1_variant != args.baseline_tag:
        if args.setup_margin is not None:
            source_variant = tier1_variant
            tier1_variant = f"{source_variant}_{output.name}_m{int(round(args.setup_margin * 100))}"
            clone_placement_checkpoint(source_variant, tier1_variant)
            tier1_dir_result = RESULTS / tier1_variant
        run_make(tier1_variant, ["do-grt", "do-route", "do-finish"],
                 output / f"{tier1_variant}.route.log", args.cores, args.setup_margin)
        assert_route_clean(tier1_variant)
        if args.setup_margin is not None:
            assert_setup_margin(tier1_variant, args.setup_margin)

    stage1_activity = output / "stage1_activity"
    subprocess.run([sys.executable, str(ACTIVITY_SCRIPT), "--variants", tier1_variant,
                    "--output-dir", str(stage1_activity)], cwd=ROOT, check=True)
    typical_vcd = stage1_activity / tier1_variant / "typical/aes_activity.vcd"
    stage1_odb = tier1_dir_result / "4_1_cts.odb"
    stage1_sdc = tier1_dir_result / "4_cts.sdc"
    stage1_power, stage1_setup, stage1_hold = clock_power(stage1_odb, stage1_sdc, analysis, "stage1")

    # Stage 2A: prune CTS load-only dummy cells; Stage 2B: select a parity-safe
    # pair of clock-buffer depth levels and score the total clock-cell objective.
    stage2a_odb = analysis / "stage2a_pruned.odb"
    stage2a_report = analysis / "stage2a_pruned.tsv"
    transform(STAGE2_TCL, "prune", stage1_odb, stage1_sdc, typical_vcd,
              stage2a_report, stage2a_odb, analysis)
    stage2a_power, stage2a_setup, stage2a_hold = clock_power(stage2a_odb, stage1_sdc, analysis, "stage2a")

    levels_path, sinks_path = analysis / "clock_levels.tsv", analysis / "clock_sinks.tsv"
    env = os.environ.copy()
    env.update({"CLOCK_LEVEL_ODB": str(stage2a_odb), "CLOCK_LEVEL_BUFFERS_OUT": str(levels_path),
                "CLOCK_LEVEL_SINKS_OUT": str(sinks_path)})
    run_openroad(LEVELS_TCL, env, analysis / "clock_levels.log")
    level_rows, sink_rows = read_tsv(levels_path), read_tsv(sinks_path)
    levels = sorted({int(row["level"]) for row in level_rows})
    valid_pairs = [pair for pair in itertools.combinations(levels, 2)
                   if all(sum(level <= int(sink["depth"]) for level in pair) % 2 == 0 for sink in sink_rows)]
    stage2_trials: list[dict[str, object]] = []
    selected_stage2_odb = stage2a_odb
    selected_levels: tuple[int, int] | None = None
    selected_power = stage2a_power
    selected_setup = stage2a_setup
    selected_hold = stage2a_hold
    for index, pair in enumerate(valid_pairs, start=1):
        trial_odb = analysis / f"stage2b_levels_{pair[0]}_{pair[1]}.odb"
        trial_report = analysis / f"stage2b_levels_{pair[0]}_{pair[1]}.tsv"
        transform(STAGE2_TCL, "replace", stage2a_odb, stage1_sdc, typical_vcd,
                  trial_report, trial_odb, analysis,
                  {"AUDIT_SWAP_LEVELS": f"{pair[0]} {pair[1]}", "AUDIT_PRUNE_DUMMIES": "0"})
        power, setup, hold = clock_power(trial_odb, stage1_sdc, analysis, f"stage2b_{index}")
        setup_delta_ps = (setup - stage1_setup) * 1000.0
        hold_delta_ps = (hold - stage1_hold) * 1000.0
        pass_gate = setup_delta_ps >= -50.0 and hold_delta_ps >= -50.0
        stage2_trials.append({"levels": f"{pair[0]},{pair[1]}", "clock_internal_power_w": power,
                              "delta_clock_internal_uw_vs_stage1": (power - stage1_power) * 1e6,
                              "setup_delta_ps_vs_stage1": setup_delta_ps,
                              "hold_delta_ps_vs_stage1": hold_delta_ps,
                              "timing_guard_pass": pass_gate, "trial_odb": str(trial_odb)})
        if pass_gate and power < selected_power:
            selected_stage2_odb, selected_levels, selected_power = trial_odb, pair, power
            selected_setup, selected_hold = setup, hold
    write_tsv(analysis / "stage2b_parity_trials.tsv", stage2_trials)
    shutil.copy2(selected_stage2_odb, analysis / "stage2_final_cts.odb")

    # Route each accepted stage before using its activity trace to drive the
    # next transform. A VCD from an earlier netlist can leave newly introduced
    # cell pins unannotated, so it must never be reused across a stage change.
    stage2_variant = f"{tier1_variant}_{output.name}_stage2"
    install_transformed_checkpoint(tier1_variant, stage2_variant, selected_stage2_odb,
                                   output, args.cores, args.setup_margin)
    if not prove_netlist_equivalence(
            baseline_dir / "6_final.v", RESULTS / stage2_variant / "6_final.v",
            analysis / "lec_routed_stage2"):
        raise RuntimeError("routed Stage 2 clock transform failed LEC")
    stage2_activity = output / "stage2_activity"
    stage2_typical_vcd = generate_typical_activity(stage2_variant, stage2_activity)

    # Stage 3A sizing and 3B pin permutation are applied and proved separately.
    # A failed optional pin permutation is discarded while the proven sizing pass remains.
    stage3a_odb = analysis / "stage3a_downsize.odb"
    stage3a_report = analysis / "stage3a_downsize.tsv"
    stage3a_power, stage3a_setup, stage3a_hold = selected_power, selected_setup, selected_hold
    stage3a_accepted = False
    try:
        transform(STAGE3_TCL, "downsize", selected_stage2_odb, stage1_sdc, stage2_typical_vcd,
                  stage3a_report, stage3a_odb, analysis,
                  {"AUDIT_DOWNSIZE_BUDGET_PS": str(args.downsize_budget_ps),
                   "AUDIT_DOWNSIZE_MAX_CELLS": str(args.max_downsize_cells),
                   "AUDIT_DOWNSIZE_MIN_SLACK_PS": "50.0"})
        stage3a_power, stage3a_setup, stage3a_hold = clock_power(
            stage3a_odb, stage1_sdc, analysis, "stage3a")
        stage3a_timing_ok = ((stage3a_setup-stage1_setup)*1000.0 >= -50.0
                             and (stage3a_hold-stage1_hold)*1000.0 >= -50.0)
        stage3a_accepted = stage3a_timing_ok
        if stage3a_accepted:
            stage3a_variant = f"{tier1_variant}_{output.name}_stage3a"
            install_transformed_checkpoint(tier1_variant, stage3a_variant, stage3a_odb,
                                           output, args.cores, args.setup_margin)
            stage3a_accepted = prove_netlist_equivalence(
                baseline_dir / "6_final.v", RESULTS / stage3a_variant / "6_final.v",
                analysis / "lec_routed_stage3a")
        if stage3a_accepted:
            stage3a_activity = output / "stage3a_activity"
            stage3a_typical_vcd = generate_typical_activity(stage3a_variant, stage3a_activity)
    except RuntimeError as exc:
        stage3a_accepted = False
        (analysis / "stage3a_failure.txt").write_text(f"{exc}\n")

    final_stage_odb = stage3a_odb if stage3a_accepted else selected_stage2_odb
    final_stage_power = stage3a_power if stage3a_accepted else selected_power
    final_stage_setup = stage3a_setup if stage3a_accepted else selected_setup
    final_stage_hold = stage3a_hold if stage3a_accepted else selected_hold
    stage3b_accepted = False
    stage3b_odb = analysis / "stage3b_pinswap.odb"
    if stage3a_accepted:
        stage3b_report = analysis / "stage3b_pinswap.tsv"
        try:
            transform(STAGE3_TCL, "pinswap", stage3a_odb, stage1_sdc, stage3a_typical_vcd,
                      stage3b_report, stage3b_odb, analysis)
            stage3b_power, stage3b_setup, stage3b_hold = clock_power(
                stage3b_odb, stage1_sdc, analysis, "stage3b")
            stage3b_timing_ok = ((stage3b_setup-stage1_setup)*1000.0 >= -50.0
                                 and (stage3b_hold-stage1_hold)*1000.0 >= -50.0)
            stage3b_accepted = stage3b_timing_ok
            if stage3b_accepted:
                stage3b_variant = f"{tier1_variant}_{output.name}_stage3b"
                install_transformed_checkpoint(tier1_variant, stage3b_variant, stage3b_odb,
                                               output, args.cores, args.setup_margin)
                stage3b_accepted = prove_netlist_equivalence(
                    baseline_dir / "6_final.v", RESULTS / stage3b_variant / "6_final.v",
                    analysis / "lec_routed_stage3b")
            if stage3b_accepted:
                final_stage_odb, final_stage_power = stage3b_odb, stage3b_power
                final_stage_setup, final_stage_hold = stage3b_setup, stage3b_hold
            else:
                (analysis / "stage3b_rejected_by_timing_or_lec.txt").write_text(
                    "pin permutation did not pass cumulative timing or routed-netlist LEC\n")
        except RuntimeError as exc:
            stage3b_accepted = False
            final_stage_odb, final_stage_power = stage3a_odb, stage3a_power
            final_stage_setup, final_stage_hold = stage3a_setup, stage3a_hold
            (analysis / "stage3b_failure.txt").write_text(f"{exc}\n")

    final_variant = stage2_variant
    if stage3a_accepted:
        final_variant = stage3b_variant if stage3b_accepted else stage3a_variant

    variants = list(dict.fromkeys([args.baseline_tag, tier1_variant, stage2_variant]))
    if stage3a_accepted:
        variants.append(stage3a_variant)
    if stage3b_accepted:
        variants.append(stage3b_variant)
    if final_variant != stage2_variant:
        variants.append(final_variant)
    for variant in dict.fromkeys((stage2_variant, final_variant)):
        candidate_netlist = RESULTS / variant / "6_final.v"
        if not prove_netlist_equivalence(baseline_dir / "6_final.v", candidate_netlist,
                                         analysis / f"lec_routed_{variant}"):
            raise RuntimeError(f"post-route LEC failed for {variant}; inspect "
                               f"{analysis / f'lec_routed_{variant}' / 'equivalence.log'}")
    complete_activity = output / "activity_all_variants"
    subprocess.run([sys.executable, str(ACTIVITY_SCRIPT), "--variants", *variants,
                    "--output-dir", str(complete_activity)], cwd=ROOT, check=True)
    reports = output / "power_reports"
    subprocess.run([sys.executable, str(REPORT_SCRIPT), "--variants", *variants,
                    "--activity-dir", str(complete_activity), "--output-dir", str(reports)], cwd=ROOT, check=True)
    subprocess.run([sys.executable, str(PROFILE_SCRIPT), "--netlist",
                    str(baseline_dir / "6_final.v"), "--power-summary",
                    str(reports / "aes_routed_power_summary.tsv"), "--variant", args.baseline_tag,
                    "--output-dir", str(output / "module0_profile")], cwd=ROOT, check=True)

    stage_rows = [
        {"stage": "stage1", "variant": tier1_variant, "clock_internal_w": stage1_power,
         "delta_clock_internal_uw_vs_stage1": 0.0, "setup_slack_ns": stage1_setup, "hold_slack_ns": stage1_hold},
        {"stage": "stage2a_dummy_prune", "variant": "pre_route_odb", "clock_internal_w": stage2a_power,
         "delta_clock_internal_uw_vs_stage1": (stage2a_power-stage1_power)*1e6,
         "setup_slack_ns": stage2a_setup, "hold_slack_ns": stage2a_hold},
        {"stage": "stage2b_parity_inverter", "variant": "pre_route_odb",
         "clock_internal_w": selected_power, "delta_clock_internal_uw_vs_stage1": (selected_power-stage1_power)*1e6,
         "setup_slack_ns": selected_setup, "hold_slack_ns": selected_hold,
         "levels": ",".join(map(str, selected_levels)) if selected_levels else "NONE"},
        {"stage": "stage3a_slew_guarded_downsize", "variant": final_variant,
         "clock_internal_w": stage3a_power, "delta_clock_internal_uw_vs_stage1": (stage3a_power-stage1_power)*1e6,
         "setup_slack_ns": stage3a_setup, "hold_slack_ns": stage3a_hold,
         "accepted": "YES" if stage3a_accepted else "NO"},
        {"stage": "stage3b_safe_pin_permutation", "variant": final_variant,
         "clock_internal_w": final_stage_power, "delta_clock_internal_uw_vs_stage1": (final_stage_power-stage1_power)*1e6,
         "setup_slack_ns": final_stage_setup, "hold_slack_ns": final_stage_hold,
         "accepted": "YES" if stage3b_accepted else "NO"},
    ]
    write_tsv(analysis / "stage_attribution_pre_route.tsv", stage_rows)
    write_tsv(analysis / "pipeline_variants.tsv", [{"baseline": args.baseline_tag, "stage1": tier1_variant,
                                                    "stage2": stage2_variant, "final": final_variant,
                                                    "stage2_inverter_levels": ",".join(map(str, selected_levels))
                                                    if selected_levels else "NONE"}])
    print(f"AES_MULTISTAGE_PIPELINE baseline={args.baseline_tag} stage1={tier1_variant} "
          f"stage1_move={'NONE' if winner_row is None else winner_id} "
          f"stage2={stage2_variant} final={final_variant} stage3a_accepted={stage3a_accepted} "
          f"stage3b_accepted={stage3b_accepted} "
          f"reports={reports / 'aes_routed_power_summary.tsv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
