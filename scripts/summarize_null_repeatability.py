#!/usr/bin/env python3
"""Compare routed power reports from identical pre-CTS placement inputs."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path


WORKLOADS = ("idle", "typical", "peak")
TOTAL_RE = re.compile(
    r"^Total\s+[-+0-9.eE]+\s+(?P<switching>[-+0-9.eE]+)", re.MULTILINE
)
ANNOTATED_RE = re.compile(r"^Annotated\s+(?P<count>\d+) pin activities\.", re.MULTILINE)
UNANNOTATED_RE = re.compile(r"^unannotated\s+(?P<count>\d+)", re.MULTILINE)
METRICS_RE = re.compile(
    r"SPEF_METRICS workload=(?P<workload>\S+) switching_w=(?P<switching>[-+0-9.eE]+) "
    r"setup_ns=(?P<setup>[-+0-9.eE]+) hold_ns=(?P<hold>[-+0-9.eE]+)"
)


def read_report(path: Path, workload: str) -> tuple[float, int, int, float, float]:
    text = path.read_text(errors="replace")
    power = TOTAL_RE.search(text)
    annotated = ANNOTATED_RE.search(text)
    unannotated = UNANNOTATED_RE.search(text)
    metrics = METRICS_RE.search(text)
    if not (power and annotated and unannotated and metrics):
        raise ValueError(f"incomplete switching power/activity report: {path}")
    if metrics.group("workload") != workload:
        raise ValueError(f"wrong workload in {path}: {metrics.group('workload')}")
    return (
        float(power.group("switching")),
        int(annotated.group("count")),
        int(unannotated.group("count")),
        float(metrics.group("setup")),
        float(metrics.group("hold")),
    )


def read_manifest(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        fields = line.split("\t", 1)
        if len(fields) != 2:
            raise ValueError(f"malformed manifest row: {line!r}")
        values[fields[0]] = fields[1]
    return values


def spef_hash_without_date(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for line in source:
            if not line.startswith(b"*DATE "):
                digest.update(line)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-a-dir", type=Path, required=True)
    parser.add_argument("--run-b-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run-a-spef", type=Path, required=True)
    parser.add_argument("--run-b-spef", type=Path, required=True)
    parser.add_argument("--max-idle-variation-nw", type=float, default=4.5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        manifest = read_manifest(args.manifest)
        if not manifest.get("source_place_odb") or not manifest.get("source_place_sdc"):
            raise ValueError("manifest lacks source placement ODB/SDC hashes")
        for key in (
            "run_a_input_odb_sha256", "run_b_input_odb_sha256",
            "run_a_input_sdc_sha256", "run_b_input_sdc_sha256",
            "run_a_final_odb_sha256", "run_b_final_odb_sha256",
            "run_a_final_spef_sha256", "run_b_final_spef_sha256",
            "run_a_final_netlist_sha256", "run_b_final_netlist_sha256",
        ):
            if not manifest.get(key):
                raise ValueError(f"manifest lacks {key}")
        if manifest["run_a_input_odb_sha256"] != manifest["run_b_input_odb_sha256"]:
            raise ValueError("the two runs did not use byte-identical placement ODBs")
        if manifest["run_a_input_sdc_sha256"] != manifest["run_b_input_sdc_sha256"]:
            raise ValueError("the two runs did not use byte-identical placement SDCs")
        results = {
            workload: (
                read_report(args.run_a_dir / f"{workload}.log", workload),
                read_report(args.run_b_dir / f"{workload}.log", workload),
            )
            for workload in WORKLOADS
        }
    except (OSError, ValueError) as exc:
        print(f"Null-move repeatability summary failed: {exc}", file=sys.stderr)
        return 2

    normalized_spef_match = spef_hash_without_date(args.run_a_spef) == spef_hash_without_date(args.run_b_spef)
    raw_spef_match = manifest["run_a_final_spef_sha256"] == manifest["run_b_final_spef_sha256"]
    spef_match_text = f"SPEF byte-identical={raw_spef_match}, excluding *DATE header={normalized_spef_match}"
    if not raw_spef_match and normalized_spef_match:
        spef_match_text += "; raw difference is only generated *DATE metadata"
    lines = [
        "Null-move routed repeatability check",
        "Both runs were seeded from the same post-detailed-placement ODB and SDC; each has its own CTS, route, RCX, final netlist, and simulated VCD.",
        f"Source placement ODB SHA-256: {manifest['source_place_odb']}",
        f"Source placement SDC SHA-256: {manifest['source_place_sdc']}",
        "Final artifact comparison (run A vs B): "
        f"ODB byte-identical={manifest['run_a_final_odb_sha256'] == manifest['run_b_final_odb_sha256']}, "
        f"{spef_match_text}, "
        f"netlist={manifest['run_a_final_netlist_sha256'] == manifest['run_b_final_netlist_sha256']}.",
        "",
        "Power comparison (run B minus run A):",
    ]
    idle_abs_nw = 0.0
    annotation_match = True
    for workload, (run_a, run_b) in results.items():
        delta_nw = (run_b[0] - run_a[0]) * 1.0e9
        annotation_match = annotation_match and run_a[1:3] == run_b[1:3]
        if workload == "idle":
            idle_abs_nw = abs(delta_nw)
        lines.append(
            f"- {workload}: {run_a[0]:.12e} -> {run_b[0]:.12e} W; "
            f"delta {delta_nw:+.6f} nW. "
            f"Annotated pins {run_a[1]} / {run_b[1]}, unannotated {run_a[2]} / {run_b[2]}. "
            f"Setup slack {run_a[3]:.9f} / {run_b[3]:.9f} ns; "
            f"hold slack {run_a[4]:.9f} / {run_b[4]:.9f} ns."
        )

    idle_ok = idle_abs_nw <= args.max_idle_variation_nw
    lines.extend(
        [
            "",
            f"Idle variation target (|delta| <= {args.max_idle_variation_nw:.3f} nW): "
            f"{'PASS' if idle_ok else 'EXCEEDED'} ({idle_abs_nw:.6f} nW).",
            f"VCD annotation-count consistency: {'PASS' if annotation_match else 'MISMATCH'}.",
            "This is one paired repeat and gives an initial measured run-to-run difference, not a statistical confidence interval.",
            "Compare the idle difference with the previously observed +45.2 nW candidate delta; a larger repeat difference means more independent route repeats are needed before accepting small power changes.",
            "",
            f"Detailed run metadata: {args.manifest}",
        ]
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n")
    print(args.output.read_text(), end="")
    return 0 if annotation_match and normalized_spef_match else 3


if __name__ == "__main__":
    raise SystemExit(main())
