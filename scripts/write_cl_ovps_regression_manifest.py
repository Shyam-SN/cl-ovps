#!/usr/bin/env python3
"""Combine design-neutral CL-OVPS per-design manifests into one regression report."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


WORKLOADS = ("idle", "typical", "peak")


def format_power(watts: float) -> str:
    magnitude = abs(watts)
    if magnitude >= 1.0e-3:
        return f"{watts * 1.0e3:.6f} mW"
    if magnitude >= 1.0e-6:
        return f"{watts * 1.0e6:.3f} µW"
    return f"{watts * 1.0e9:.3f} nW"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", action="append", required=True, type=Path,
                        help="per-design cl_ovps_manifest.json; repeat for each design")
    parser.add_argument("--output_dir", required=True, type=Path)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f"refusing to overwrite non-empty output directory: {output}")
    output.mkdir(parents=True, exist_ok=True)

    designs = []
    for raw_path in args.manifest:
        path = raw_path.resolve()
        data = json.loads(path.read_text())
        if data.get("schema_version") != 1 or "workloads" not in data or "gates" not in data:
            parser.error(f"unsupported or incomplete CL-OVPS manifest: {path}")
        designs.append({"design": data["design"], "status": data["status"],
                        "manifest": str(path), "sha256": sha256(path), "data": data})

    names = [item["design"] for item in designs]
    if len(names) != len(set(names)):
        parser.error("design labels must be unique")
    passed = all(item["status"] == "PASS" for item in designs)
    report = {
        "schema_version": 1,
        "engine": "CL-OVPS universal OpenROAD measurement regression",
        "status": "PASS" if passed else "FAIL",
        "designs": [{key: value for key, value in item.items() if key != "data"}
                    | {"workloads": item["data"]["workloads"], "gates": item["data"]["gates"],
                       "evidence": item["data"].get("evidence", {})}
                    for item in designs],
        "measurement_boundary": "Routed ODB/SPEF/VCD measurement and signoff evidence validation; no optimization was rerun.",
    }
    (output / "cl_ovps_unified_signoff.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    lines = ["# CL-OVPS unified routed regression", "", f"**Status: {report['status']}**", "",
             "| Design | Workload | Baseline dynamic | Candidate dynamic | Delta | Reduction | Setup Δ | Hold Δ | Annotation |",
             "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for item in designs:
        data = item["data"]
        for workload in WORKLOADS:
            row = data["workloads"][workload]
            base = row.get("baseline")
            cand = row["candidate"]
            if base:
                reduction = 100.0 * (base["dynamic_power_w"] - cand["dynamic_power_w"]) / base["dynamic_power_w"]
                lines.append(
                    f"| {item['design']} | {workload} | {format_power(base['dynamic_power_w'])} | "
                    f"{format_power(cand['dynamic_power_w'])} | {format_power(row['dynamic_delta_w'])} | "
                    f"{reduction:.3f}% | {row['setup_delta_ps']:+.2f} ps | "
                    f"{row['hold_delta_ps']:+.2f} ps | {cand['annotated_pins']} / "
                    f"{cand['unannotated_pins']} missing |")
            else:
                lines.append(f"| {item['design']} | {workload} | — | {format_power(cand['dynamic_power_w'])} | — | — | — | — | "
                             f"{cand['annotated_pins']} / {cand['unannotated_pins']} missing |")
    lines += ["", "## Signoff gates", ""]
    for item in designs:
        lines.append(f"### {item['design']}: {item['status']}")
        lines.append("")
        for name, gate in item["data"]["gates"].items():
            lines.append(f"- {'PASS' if gate['pass'] else 'FAIL'} — {name}: {gate.get('detail', '')}")
        lines.append("")
    lines += ["## Scope", "",
              report["measurement_boundary"],
              "OpenROAD re-read each final routed ODB/SPEF and each workload VCD. DRC, antenna, PDN, and LEC checks reference the saved signoff logs/status artifacts.", ""]
    (output / "cl_ovps_unified_signoff.md").write_text("\n".join(lines))
    print(f"CL_OVPS_REGRESSION designs={len(designs)} status={report['status']} "
          f"manifest={output / 'cl_ovps_unified_signoff.json'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
