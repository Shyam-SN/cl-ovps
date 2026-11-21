#!/usr/bin/env python3
"""Route and measure one combined GCD clock/power upper-bound audit variant.

The source must be a saved post-CTS ODB. The script transforms a private copy,
then runs the remaining ORFS route/finish stages and three-workload SPEF power
checks against the immutable GCD baseline.
"""

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
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
FLOW = ROOT / "tools/openroad-flow-scripts/flow"
DESIGN_CONFIG = ROOT / "designs/gcd/flow/config.mk"
BASE_RESULT = ROOT / "build/orfs/results/sky130hd/gcd_power/gcd_baseline20ns"
WINNER_RESULT = ROOT / "build/orfs/results/sky130hd/gcd_power/gcd_tier1opt_gcd_tier1_total_clock_full_20261031a_final"
BASE_ACTIVITY = ROOT / "build/activity/gcd_baseline20ns/routed"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], cwd: Path, env: dict[str, str], log: Path) -> int:
    process = subprocess.run(command, cwd=cwd, env=env, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             check=False)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(process.stdout)
    return process.returncode


def run_power_reports(report_dir: Path, activity_dir: Path) -> None:
    candidate_dir = report_dir / "candidate"
    baseline_dir = report_dir / "baseline"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    baseline_dir.mkdir(parents=True, exist_ok=True)
    final_dir = ROOT / "build/orfs/results/sky130hd/gcd_power" / report_dir.name
    for workload in ("idle", "typical", "peak"):
        for label, result_dir, vcd, out_dir in (
            ("baseline", BASE_RESULT, BASE_ACTIVITY / f"{workload}.vcd", baseline_dir),
            ("candidate", final_dir, activity_dir / "candidate" / f"{workload}.vcd", candidate_dir),
        ):
            env = os.environ.copy()
            env.update({
                "SPEF_ODB": str(result_dir / "6_final.odb"),
                "SPEF_SDC": str(result_dir / "6_final.sdc"),
                "SPEF_FILE": str(result_dir / "6_final.spef"),
                "ACTIVITY_VCD": str(vcd),
                "WORKLOAD": workload,
                "ACTIVITY_TRACE": "1",
                "CAP_REPORT": str(out_dir / f"{workload}.driver_caps.tsv"),
            })
            rc = run([str(OPENROAD), "-no_init", "-exit",
                      str(ROOT / "scripts/gcd_report_spef_power.tcl")],
                     ROOT, env, out_dir / f"{workload}.log")
            if rc:
                raise RuntimeError(f"{label} {workload} SPEF power report failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-cts-odb", type=Path,
                        default=WINNER_RESULT / "4_cts.odb")
    parser.add_argument("--source-cts-sdc", type=Path,
                        default=WINNER_RESULT / "4_cts.sdc")
    parser.add_argument("--variant", default="gcd_multistage_audit_20261031a")
    parser.add_argument("--num-cores", type=int, default=4)
    parser.add_argument("--transform-tcl", type=Path,
                        default=ROOT / "scripts/audit_gcd_multistage_transform.tcl")
    args = parser.parse_args()

    source_odb = args.source_cts_odb.resolve()
    source_sdc = args.source_cts_sdc.resolve()
    if not source_odb.is_file() or not source_sdc.is_file():
        raise FileNotFoundError("source post-CTS ODB/SDC does not exist")
    result_dir = ROOT / "build/orfs/results/sky130hd/gcd_power" / args.variant
    report_dir = ROOT / "build/power" / args.variant
    activity_dir = ROOT / "build/activity" / args.variant
    if any(path.exists() for path in (result_dir, report_dir, activity_dir)):
        raise FileExistsError(f"refusing to overwrite existing audit variant {args.variant}")
    if not OPENROAD.is_file():
        raise FileNotFoundError(OPENROAD)
    result_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)
    (activity_dir / "candidate").mkdir(parents=True)

    baseline_odb = BASE_RESULT / "6_final.odb"
    baseline_spef = BASE_RESULT / "6_final.spef"
    for path in (baseline_odb, baseline_spef, BASE_RESULT / "6_final.sdc",
                 BASE_ACTIVITY / "typical.vcd"):
        if not path.is_file():
            raise FileNotFoundError(path)

    source_sha = sha256(source_odb)
    baseline_odb_sha = sha256(baseline_odb)
    baseline_spef_sha = sha256(baseline_spef)
    source_backup = report_dir / "source_post_cts.odb"
    shutil.copy2(source_odb, source_backup)
    shutil.copy2(source_sdc, result_dir / "4_cts.sdc")
    transformed_odb = result_dir / "4_cts.odb"
    transform_report = report_dir / "transform_audit.tsv"
    transform_log = report_dir / "transform_audit.log"
    env = os.environ.copy()
    env.update({
        "AUDIT_INPUT_ODB": str(source_backup),
        "AUDIT_SDC": str(source_sdc),
        "AUDIT_OUT_ODB": str(transformed_odb),
        "ACTIVITY_VCD": os.environ.get(
            "AUDIT_ACTIVITY_VCD",
            str(ROOT / "build/activity/gcd_tier1opt_gcd_tier1_total_clock_full_20261031a_final/candidate/typical.vcd"),
        ),
        "AUDIT_REPORT": str(transform_report),
        "AUDIT_PRUNE_DUMMIES": os.environ.get("AUDIT_PRUNE_DUMMIES", "1"),
        "AUDIT_SWAP_CLOCK_BUFFERS": os.environ.get("AUDIT_SWAP_CLOCK_BUFFERS", "1"),
        "AUDIT_DOWNSIZE": os.environ.get("AUDIT_DOWNSIZE", "1"),
        "AUDIT_PIN_SWAP": os.environ.get("AUDIT_PIN_SWAP", "1"),
    })
    transform_tcl = args.transform_tcl.resolve()
    if not transform_tcl.is_file():
        raise FileNotFoundError(transform_tcl)
    rc = run([str(OPENROAD), "-no_init", "-exit", str(transform_tcl)], ROOT, env, transform_log)
    if rc:
        raise RuntimeError(f"pre-route multistage transform/guard failed; see {transform_log}")
    if not transformed_odb.is_file():
        raise RuntimeError("transform script did not write its post-CTS ODB")

    with (report_dir / "integrity.tsv").open("w", newline="") as out:
        writer = csv.writer(out, delimiter="\t", lineterminator="\n")
        writer.writerow(("artifact", "sha256_before_route", "sha256_after_route"))
        writer.writerow(("source_post_cts_odb", source_sha, sha256(source_odb)))
        writer.writerow(("gcd_baseline_final_odb", baseline_odb_sha, ""))
        writer.writerow(("gcd_baseline_final_spef", baseline_spef_sha, ""))
        writer.writerow(("transformed_post_cts_odb", sha256(transformed_odb), ""))

    make_command = [
        "make", "-C", str(FLOW),
        f"DESIGN_CONFIG={DESIGN_CONFIG}", "DESIGN_NICKNAME=gcd_power",
        f"FLOW_VARIANT={args.variant}", f"WORK_HOME={ROOT / 'build/orfs'}",
        f"OPENROAD_EXE={OPENROAD}", f"NUM_CORES={args.num_cores}",
        f"YOSYS_EXE={os.environ.get('YOSYS_EXE', shutil.which('yosys') or '')}",
        f"PYTHON_EXE={os.environ.get('PYTHON_EXE', shutil.which('python3') or '')}",
        "do-route", "do-finish",
    ]
    rc = run(make_command, ROOT, os.environ.copy(), report_dir / "orfs_route.log")
    if rc:
        raise RuntimeError(f"ORFS route/finish failed (exit {rc}); see {report_dir / 'orfs_route.log'}")

    final_netlist = result_dir / "6_final.v"
    for path in (result_dir / "6_final.odb", result_dir / "6_final.sdc",
                 result_dir / "6_final.spef", final_netlist):
        if not path.is_file():
            raise RuntimeError(f"required routed audit output is missing: {path}")
    env = os.environ.copy()
    env.update({"GATELEVEL_NETLIST": str(final_netlist),
                "ACTIVITY_OUT_DIR": str(activity_dir / "candidate")})
    rc = run(["bash", str(ROOT / "designs/gcd/sim/run_activity.sh")], ROOT, env,
             report_dir / "candidate_simulation.log")
    if rc:
        raise RuntimeError("candidate three-workload gate-level simulation failed")

    run_power_reports(report_dir, activity_dir)
    (report_dir / "route_complete.txt").write_text(args.variant + "\n")
    summary = report_dir / "summary.txt"
    rc = run([sys.executable, str(ROOT / "scripts/summarize_spef_comparison.py"),
              "--baseline-dir", str(report_dir / "baseline"),
              "--candidate-dir", str(report_dir / "candidate"),
              "--route-marker", str(report_dir / "route_complete.txt"),
              "--output", str(summary)], ROOT, os.environ.copy(), report_dir / "summary_driver.log")
    if rc != 0:
        raise RuntimeError(f"SPEF comparison rejected the audit result; see {summary}")

    after_baseline_odb = sha256(baseline_odb)
    after_baseline_spef = sha256(baseline_spef)
    if (after_baseline_odb != baseline_odb_sha or after_baseline_spef != baseline_spef_sha
            or sha256(source_odb) != source_sha):
        raise RuntimeError("source or baseline artifacts changed during audit")
    print(f"Routed combined upper-bound audit: {args.variant}")
    print(f"Summary: {summary}")
    print(f"Transformed post-CTS ODB: {transformed_odb}")
    print(f"Baseline and source ODB hashes verified unchanged; SPEF comparison exit={rc}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
