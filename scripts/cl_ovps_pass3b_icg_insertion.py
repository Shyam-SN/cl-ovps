#!/usr/bin/env python3
"""Extract synchronous enable groups and insert Sky130HD ICGs before CTS.

Direct feedback muxes and mapped ``edfxtp`` groups are extracted separately.
Both require a positive, same-clock enable cone and legal spatial clusters.
Direct feedback mux candidates are eligible for mux removal; enable flops keep
their DE function intact. Asynchronous inputs, cross-domain logic, and
reset/scan flops are rejected. This complements OpenROAD's built-in
``clock_gating`` engine, which crashes on the project's hierarchical ODB.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
EXTRACT_TCL = ROOT / "scripts/stage3b_icg_extract.tcl"
INSERT_TCL = ROOT / "scripts/stage3b_icg_insertion.tcl"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_openroad(openroad: Path, tcl: Path, env: dict[str, str], log: Path) -> str:
    run = subprocess.run([str(openroad), "-no_init", "-exit", str(tcl)], cwd=ROOT,
                         env=env, text=True, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, check=False)
    log.write_text(run.stdout)
    if run.returncode:
        raise RuntimeError(f"OpenROAD failed ({run.returncode}); inspect {log}")
    return run.stdout


def cluster_candidates(rows: list[dict[str, str]], dbu: int, min_instances: int,
                       max_sinks: int, max_span_um: float) -> tuple[list[list[dict[str, str]]], int]:
    groups: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row.get("status") == "CANDIDATE" and row.get("kind") in {"enable_flop", "feedback_mux"}:
            groups[(row["kind"], row["clock_net"], row["enable_net"])].append(row)
    clusters: list[list[dict[str, str]]] = []
    underfilled = 0
    max_span = max_span_um * dbu
    for (_kind, _clock, _enable), members in sorted(groups.items()):
        ordered = sorted(members, key=lambda row: (int(row["clock_x_dbu"]),
                                                    int(row["clock_y_dbu"]), row["flop"]))
        current: list[dict[str, str]] = []
        xs: list[int] = []
        ys: list[int] = []
        for row in ordered:
            x, y = int(row["clock_x_dbu"]), int(row["clock_y_dbu"])
            new_xs, new_ys = xs + [x], ys + [y]
            diameter = math.hypot(max(new_xs) - min(new_xs), max(new_ys) - min(new_ys))
            if current and (len(current) >= max_sinks or diameter > max_span):
                if len(current) >= min_instances:
                    clusters.append(current)
                else:
                    underfilled += len(current)
                current, xs, ys = [], [], []
            current.append(row)
            xs.append(x)
            ys.append(y)
        if current:
            if len(current) >= min_instances:
                clusters.append(current)
            else:
                underfilled += len(current)
    return clusters, underfilled


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-odb", required=True, type=Path,
                        help="post-placement ODB before CTS")
    parser.add_argument("--sdc", required=True, type=Path)
    parser.add_argument("--lib", required=True, action="append", type=Path)
    parser.add_argument("--rc-tcl", required=True, type=Path)
    parser.add_argument("--upf", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--openroad", type=Path, default=DEFAULT_OPENROAD)
    parser.add_argument("--min-instances", type=int, default=4)
    parser.add_argument("--max-sinks", type=int, default=16)
    parser.add_argument("--max-span-um", type=float, default=25.0)
    parser.add_argument("--timing-guard-ps", type=float, default=50.0)
    parser.add_argument("--apply", action="store_true",
                        help="legalize and commit eligible ICGs to a new ODB")
    args = parser.parse_args()

    inputs = [args.input_odb, args.sdc, args.rc_tcl, args.openroad, *args.lib]
    if args.upf:
        inputs.append(args.upf)
    for path in inputs:
        if not path.is_file():
            parser.error(f"missing Phase 3B input: {path}")
    if args.min_instances < 2 or args.max_sinks < args.min_instances or args.max_span_um <= 0:
        parser.error("require 2 <= min-instances <= max-sinks and a positive max-span")
    output_dir = args.output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit(f"refusing to overwrite non-empty Phase 3B output: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_odb = output_dir / "phase3b.odb"
    candidate_tsv = output_dir / "feedback_candidates.tsv"
    cluster_tsv = output_dir / "icg_clusters.tsv"
    manifest_path = output_dir / "phase3b_manifest.json"
    extractor_log = output_dir / "extract.log"
    insertion_log = output_dir / "insertion.log"

    env = os.environ.copy()
    env.update({
        "P3B_INPUT_ODB": str(args.input_odb.resolve()),
        "P3B_LIBS": "\n".join(str(path.resolve()) for path in args.lib),
        "P3B_CANDIDATES": str(candidate_tsv),
    })
    try:
        extract_output = run_openroad(args.openroad.resolve(), EXTRACT_TCL, env, extractor_log)
        dbu_match = __import__("re").search(r"P3B_EXTRACT .*?dbu_per_micron=(\d+)", extract_output)
        if not dbu_match:
            raise RuntimeError("extractor omitted dbu_per_micron; inspect " + str(extractor_log))
        dbu = int(dbu_match.group(1))
        with candidate_tsv.open(newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
        clusters, underfilled = cluster_candidates(rows, dbu, args.min_instances,
                                                   args.max_sinks, args.max_span_um)
        with cluster_tsv.open("w", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
            writer.writerow(("cluster_id", "icg_master", "kind", "clock_net", "enable_net",
                             "flop", "mux", "data_net", "x_dbu", "y_dbu"))
            cluster_index = 0
            for cluster in clusters:
                count = len(cluster)
                drive = 1 if count <= 4 else 2 if count <= 12 else 4
                master = f"sky130_fd_sc_hd__dlclkp_{drive}"
                for row in cluster:
                    writer.writerow((f"p3b_{cluster_index:04d}", master, row["kind"],
                                     row["clock_net"], row["enable_net"], row["flop"],
                                     row.get("mux", ""), row.get("data_net", ""),
                                     row["clock_x_dbu"], row["clock_y_dbu"]))
                cluster_index += 1

        if args.apply and clusters:
            env.update({
                "P3B_SDC": str(args.sdc.resolve()),
                "P3B_RC_TCL": str(args.rc_tcl.resolve()),
                "P3B_PLAN": str(cluster_tsv),
                "P3B_REPORT": str(output_dir / "insertion.tsv"),
                "P3B_OUTPUT_ODB": str(output_odb),
                "P3B_TIMING_GUARD_PS": str(args.timing_guard_ps),
                "P3B_UPF": str(args.upf.resolve()) if args.upf else "",
            })
            insertion_output = run_openroad(args.openroad.resolve(), INSERT_TCL, env, insertion_log)
            result_match = __import__("re").search(
                r"(?m)^P3B_RESULT status=(\S+) inserted_icgs=(\d+) gated_sinks=(\d+) "
                r"original_icgs=(\d+) gate_check=(\S+) output=(\S+)$", insertion_output)
            if result_match is None:
                raise RuntimeError("insertion step omitted final result marker; inspect " + str(insertion_log))
            status, inserted_text, gated_text, original_text, gate_check, output_path = result_match.groups()
            if status not in {"INSERTED", "NO_SAFE_CLUSTERS"} or not output_odb.is_file():
                raise RuntimeError(f"insertion produced unexpected status {status}; inspect {insertion_log}")
            if status == "NO_SAFE_CLUSTERS":
                status = "NO_SAFE_GROUPS"
            timing: dict[str, dict[str, float]] = {}
            for match in __import__("re").finditer(
                r"(?m)^P3B_TIMING phase=(before|after) setup_s=([+\-0-9.eE]+) hold_s=([+\-0-9.eE]+)$",
                insertion_output):
                timing[match.group(1)] = {"setup_slack_s": float(match.group(2)),
                                          "hold_slack_s": float(match.group(3))}
        else:
            shutil.copy2(args.input_odb, output_odb)
            status = "ANALYZE_ONLY" if not args.apply else "NO_SAFE_GROUPS"
            inserted_text = "0"
            gated_text = "0"
            original_text = "0"
            gate_check = "NOT_RUN"
            timing = {}
    except Exception as exc:
        manifest = {
            "schema_version": 1, "status": "REJECTED", "mode": "apply" if args.apply else "analyze_only",
            "engine": "CL-OVPS same-clock enable-flop insertion",
            "error": str(exc), "input_odb": str(args.input_odb.resolve()),
            "input_odb_sha256": sha256(args.input_odb),
            "outputs": {"candidate_report": str(candidate_tsv) if candidate_tsv.exists() else None,
                        "cluster_plan": str(cluster_tsv) if cluster_tsv.exists() else None,
                        "extract_log": str(extractor_log), "insertion_log": str(insertion_log)},
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        raise SystemExit(str(exc))

    eligible = [row for row in rows if row.get("status") == "CANDIDATE"]
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "status": status,
        "mode": "apply" if args.apply else "analyze_only",
        "engine": "CL-OVPS conservative synchronous enable grouping",
        "implementation_stage": "post-placement, pre-CTS",
        "reason_for_pre_cts": "CTS must build clock trees through inserted ICG branches",
        "source_odb": {"path": str(args.input_odb.resolve()), "sha256": sha256(args.input_odb)},
        "constraints": {"sdc": str(args.sdc.resolve()), "liberty": [str(p.resolve()) for p in args.lib],
                        "rc_tcl": str(args.rc_tcl.resolve()), "upf": str(args.upf.resolve()) if args.upf else None},
        "parameters": {"minimum_sinks": args.min_instances, "maximum_sinks_per_icg": args.max_sinks,
                       "maximum_cluster_diameter_um": args.max_span_um,
                       "timing_degradation_guard_ps": args.timing_guard_ps},
        "extraction": {"sequential_instances": sum(1 for row in rows if row.get("flop_master")),
                       "candidate_instances": len(eligible), "candidate_groups": len(set(
                           (row["kind"], row["clock_net"], row["enable_net"]) for row in eligible)),
                       "planned_clusters": len(clusters), "underfilled_cluster_sinks": underfilled,
                       "candidate_tsv": str(candidate_tsv), "cluster_plan": str(cluster_tsv)},
        "inserted_icgs": int(inserted_text),
        "gated_sinks": int(gated_text),
        "timing": timing,
        "clock_gating_checks": gate_check,
        "limitations": ["Direct feedback mux removal requires the mux to exclusively drive the matched flop D pin.",
                        "Enable cones must trace only to same-clock registers/constants; primary-input and cross-clock cones are rejected.",
                        "Mapped enable-flop DE logic is preserved while its external clock is gated.",
                        "Routed SPEF/VCD power, LEC, DRC, antenna, and PDN signoff require the subsequent full flow."],
        "outputs": {"odb": str(output_odb), "odb_sha256": sha256(output_odb),
                    "manifest": str(manifest_path), "extract_log": str(extractor_log),
                    "insertion_log": str(insertion_log) if insertion_log.exists() else None},
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"PASS3B_RESULT status={status} mode={manifest['mode']} candidates={len(eligible)} "
          f"clusters={len(clusters)} inserted_icgs={inserted_text} gated_sinks={gated_text} "
          f"output={output_odb} manifest={manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
