#!/usr/bin/env python3
"""Run the frozen-pool GCD Tier-1 search followed by routed Stage 2/3 passes."""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_PLACE_ODB = ROOT / "build/orfs/results/sky130hd/gcd_power/gcd_baseline20ns/3_place.odb"
FROZEN_POOL = ROOT / "build/power/gcd_flop_relocation_cross_row_holdout_20261029a/candidate_pool.tsv"
OPTIMIZER = ROOT / "scripts/gcd_tier1_greedy_optimizer.py"
AUDITOR = ROOT / "scripts/audit_gcd_multistage_power.py"
FLOW_RESULTS = ROOT / "build/orfs/results/sky130hd/gcd_power"
POWER_RESULTS = ROOT / "build/power"

STAGES = (
    ("Stage 2A: prune unused clock loads", "s2a", ROOT / "scripts/stage2_clock_restructurer.tcl",
     {"CLOCK_RESTRUCTURE_ACTION": "prune"}),
    ("Stage 2B: parity-preserving inverter tree", "s2b", ROOT / "scripts/stage2_clock_restructurer.tcl",
     {"CLOCK_RESTRUCTURE_ACTION": "replace"}),
    ("Stage 3A: VCD/timing/slew-gated sizing", "s3a", ROOT / "scripts/stage3_vcd_opt.tcl",
     {"VCD_OPT_ACTION": "downsize", "AUDIT_DOWNSIZE_BUDGET_PS": "50", "AUDIT_DOWNSIZE_MAX_CELLS": "40"}),
    ("Stage 3B: VCD/timing/power-gated pin permutation", "s3b", ROOT / "scripts/stage3_vcd_opt.tcl",
     {"VCD_OPT_ACTION": "pinswap"}),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], env: dict[str, str], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.run(command, cwd=ROOT, env=env, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             check=False)
    log_path.write_text(process.stdout)
    if process.returncode:
        raise RuntimeError(f"command failed ({process.returncode}); see {log_path}")


def parse_summary(path: Path) -> dict[str, dict[str, float | int]]:
    text = path.read_text(errors="replace")
    result: dict[str, dict[str, float | int]] = {}
    workload_re = re.compile(
        r"^- (?P<workload>idle|typical|peak): .*?dynamic \(internal \+ net switching\) delta "
        r"(?P<dynamic>[+-]?[0-9.eE+-]+) W;",
        re.MULTILINE,
    )
    timing_re = re.compile(
        r"^  Timing slack: setup (?P<setup_base>[+-]?[0-9.eE+-]+) -> "
        r"(?P<setup_cand>[+-]?[0-9.eE+-]+) ns; hold (?P<hold_base>[+-]?[0-9.eE+-]+) -> "
        r"(?P<hold_cand>[+-]?[0-9.eE+-]+) ns\. Activity annotated: "
        r"(?P<annot_base>[0-9]+) baseline / (?P<annot_cand>[0-9]+) candidate; "
        r"unannotated: (?P<unannot_base>[0-9]+) / (?P<unannot_cand>[0-9]+)\.",
    )
    for match in workload_re.finditer(text):
        workload = match.group("workload")
        lines = text[match.end():].splitlines()
        timing = next((timing_re.search(line) for line in lines if "Timing slack:" in line), None)
        if timing is None:
            raise ValueError(f"timing/annotation row missing for {workload} in {path}")
        result[workload] = {"dynamic_delta_w": float(match.group("dynamic"))}
        result[workload].update({key: float(value) for key, value in timing.groupdict().items()
                                 if key.startswith(("setup_", "hold_"))})
        result[workload].update({key: int(value) for key, value in timing.groupdict().items()
                                 if key.startswith(("annot_", "unannot_"))})
    if set(result) != {"idle", "typical", "peak"}:
        raise ValueError(f"expected three workloads in {path}, got {sorted(result)}")
    for gate in ("SPEF candidate acceptance: PASS.", "VCD annotation coverage complete: PASS.",
                 "Setup/hold guard (50 ps): PASS."):
        if gate not in text:
            raise RuntimeError(f"routed result failed {gate.rstrip('.')} in {path}")
    return result


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(dict.fromkeys(k for row in rows for k in row)),
                                delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-place-odb", type=Path, default=BASE_PLACE_ODB)
    parser.add_argument("--candidate-pool", type=Path, default=FROZEN_POOL)
    parser.add_argument("--output-tag", default="gcd_auto_multistage_20261101a")
    parser.add_argument("--num-cores", type=int, default=4)
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument("--clock-internal-cutoff-nw", type=float, default=-1000.0)
    parser.add_argument("--hpwl-limit-pct", type=float, default=1.5)
    args = parser.parse_args()

    source_place = args.source_place_odb.resolve()
    pool = args.candidate_pool.resolve()
    if not source_place.is_file() or not pool.is_file():
        raise FileNotFoundError(source_place if not source_place.is_file() else pool)
    out_root = POWER_RESULTS / args.output_tag
    if out_root.exists():
        raise FileExistsError(f"refusing to overwrite pipeline output {out_root}")
    out_root.mkdir(parents=True)

    optimizer_tag = f"gcd_tier1_82pool_totalclock_{args.output_tag.rsplit('_', 1)[-1]}"
    optimizer_dir = POWER_RESULTS / optimizer_tag
    optimizer_cmd = [sys.executable, str(OPTIMIZER), "--source-odb", str(source_place),
                     "--candidate-pool", str(pool), "--output-dir", str(optimizer_dir),
                     "--max-iterations", str(args.max_iterations), "--candidate-sinks", "6",
                     "--moves-per-sink", "2", "--clock-internal-cutoff-nw",
                     str(args.clock_internal_cutoff_nw), "--hpwl-limit-pct", str(args.hpwl_limit_pct),
                     "--num-cores", str(args.num_cores)]
    run(optimizer_cmd, os.environ.copy(), out_root / "stage1_optimizer.log")

    optimizer_manifest = optimizer_dir / "optimization_manifest.tsv"
    optimized_odb = optimizer_dir / "optimized.odb"
    stage1_variant = f"gcd_tier1opt_{optimizer_dir.name}_final"
    stage1_result = FLOW_RESULTS / stage1_variant
    stage1_summary = POWER_RESULTS / stage1_variant / "summary.txt"
    for required in (optimizer_manifest, optimized_odb, stage1_result / "4_cts.odb",
                     stage1_result / "4_cts.sdc", stage1_summary,
                     ROOT / "build/activity" / stage1_variant / "candidate/typical.vcd"):
        if not required.is_file():
            raise FileNotFoundError(required)
    stage1_metrics = parse_summary(stage1_summary)
    with (out_root / "frozen_inputs.tsv").open("w", newline="") as output:
        writer = csv.writer(output, delimiter="\t", lineterminator="\n")
        writer.writerow(("artifact", "path", "sha256"))
        for path in (source_place, pool, optimizer_manifest, optimized_odb,
                     stage1_result / "4_cts.odb", stage1_result / "4_cts.sdc"):
            writer.writerow((path.name, path, sha256(path)))

    stage_rows: list[dict[str, object]] = []
    for workload, values in stage1_metrics.items():
        stage_rows.append({"stage": "Stage 1: 82-candidate Tier-1 search", "variant": stage1_variant,
                           "workload": workload, "cumulative_dynamic_delta_uw": float(values["dynamic_delta_w"]) * 1e6,
                           "incremental_dynamic_delta_uw": float(values["dynamic_delta_w"]) * 1e6,
                           "setup_slack_delta_ps": (float(values["setup_cand"]) - float(values["setup_base"])) * 1000.0,
                           "hold_slack_delta_ps": (float(values["hold_cand"]) - float(values["hold_base"])) * 1000.0,
                           "candidate_unannotated_pins": values["unannot_cand"], "summary": stage1_summary})

    previous_result = stage1_result
    previous_metrics = stage1_metrics
    stage_manifest: list[dict[str, object]] = []
    activity_vcd = ROOT / "build/activity" / stage1_variant / "candidate/typical.vcd"
    for label, suffix, transform_tcl, stage_env in STAGES:
        variant = f"gcd_{args.output_tag}_{suffix}"
        env = os.environ.copy()
        env.update(stage_env)
        env["AUDIT_ACTIVITY_VCD"] = str(activity_vcd)
        source_odb = previous_result / "4_cts.odb"
        source_sdc = previous_result / "4_cts.sdc"
        if not source_odb.is_file() or not source_sdc.is_file():
            raise FileNotFoundError(source_odb if not source_odb.is_file() else source_sdc)
        stage_cmd = [sys.executable, str(AUDITOR), "--source-cts-odb", str(source_odb),
                     "--source-cts-sdc", str(source_sdc), "--variant", variant,
                     "--num-cores", str(args.num_cores), "--transform-tcl", str(transform_tcl)]
        log_path = out_root / f"{suffix}.log"
        run(stage_cmd, env, log_path)
        stage_power_dir = POWER_RESULTS / variant
        stage_summary = stage_power_dir / "summary.txt"
        routed_dir = FLOW_RESULTS / variant
        for required in (stage_summary, routed_dir / "4_cts.odb", routed_dir / "4_cts.sdc",
                         routed_dir / "6_final.odb", routed_dir / "6_final.spef"):
            if not required.is_file():
                raise FileNotFoundError(required)
        metrics = parse_summary(stage_summary)
        source_backup = stage_power_dir / "source_post_cts.odb"
        if sha256(source_backup) != sha256(source_odb):
            raise RuntimeError(f"stage {suffix} did not consume the previous stage's ODB")
        stage_manifest.append({"stage": label, "variant": variant, "transform_tcl": transform_tcl,
                               "source_odb": source_odb, "source_odb_sha256": sha256(source_odb),
                               "transformed_odb": routed_dir / "4_cts.odb",
                               "transformed_odb_sha256": sha256(routed_dir / "4_cts.odb"),
                               "summary": stage_summary, "summary_sha256": sha256(stage_summary),
                               "log": log_path})
        for workload, values in metrics.items():
            current_delta = float(values["dynamic_delta_w"])
            prior_delta = float(previous_metrics[workload]["dynamic_delta_w"])
            stage_rows.append({"stage": label, "variant": variant, "workload": workload,
                               "cumulative_dynamic_delta_uw": current_delta * 1e6,
                               "incremental_dynamic_delta_uw": (current_delta - prior_delta) * 1e6,
                               "setup_slack_delta_ps": (float(values["setup_cand"]) - float(values["setup_base"])) * 1000.0,
                               "hold_slack_delta_ps": (float(values["hold_cand"]) - float(values["hold_base"])) * 1000.0,
                               "candidate_unannotated_pins": values["unannot_cand"], "summary": stage_summary})
        previous_result = routed_dir
        previous_metrics = metrics
        print(f"GCD_PIPELINE stage={suffix} summary={stage_summary}", flush=True)

    write_tsv(out_root / "routed_stage_attribution.tsv", stage_rows)
    write_tsv(out_root / "stage_manifest.tsv", stage_manifest)
    (out_root / "final_variant.txt").write_text(previous_result.name + "\n")
    print(f"GCD multi-stage optimizer complete: {out_root}")
    print(f"Routed stage attribution: {out_root / 'routed_stage_attribution.tsv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
