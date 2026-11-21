#!/usr/bin/env python3
"""Generate routed SDF/arrival diagnostics and compare paired gate-level VCDs.

The pass is read-only. A routed SPEF is required for the STA/SDF result. A
glitch-overhead value is reported only when both zero-delay and SDF-annotated
VCDs are supplied and share signal paths; otherwise the manifest explicitly
marks that part of the diagnostic pending.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TCL = ROOT / "scripts/stage3e_glitch_diag.tcl"
DEFAULT_OPENROAD = ROOT / "tools/openroad/build/bin/openroad"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _known_bits(value: str) -> int | None:
    value = value.lower().replace("_", "")
    if not value or any(bit not in "01" for bit in value):
        return None
    return int(value, 2)


def vcd_toggle_counts(path: Path, scope: str) -> dict[str, int]:
    """Count bit transitions per VCD signal below scope, ignoring X/Z edges."""
    wanted = [part for part in scope.strip("/").split("/") if part]
    stack: list[str] = []
    code_paths: dict[str, list[tuple[str, int]]] = {}
    current: dict[str, int] = {}
    counts: dict[str, int] = {}
    in_definitions = True
    with path.open("r", errors="replace") as stream:
        for raw in stream:
            line = raw.strip()
            if in_definitions:
                if line.startswith("$scope"):
                    parts = line.split()
                    if len(parts) >= 3:
                        stack.append(parts[2])
                elif line.startswith("$upscope"):
                    if stack:
                        stack.pop()
                elif line.startswith("$var"):
                    parts = line.split()
                    if len(parts) >= 6:
                        try:
                            width = int(parts[2])
                        except ValueError:
                            continue
                        code = parts[3]
                        reference = " ".join(parts[4:-1])
                        scope_matches = len(stack) >= len(wanted) and stack[:len(wanted)] == wanted
                        if not scope_matches:
                            continue
                        relative = "/".join(stack[len(wanted):] + [reference])
                        code_paths.setdefault(code, []).append((relative, width))
                        counts.setdefault(relative, 0)
                elif line.startswith("$enddefinitions"):
                    in_definitions = False
                continue
            if not line or line[0] in "$#":
                continue
            if line[0] in "bB":
                parts = line[1:].split()
                if len(parts) != 2:
                    continue
                bits, code = parts
            elif line[0] in "01xXzZ":
                bits, code = line[0], line[1:].strip()
            else:
                continue
            now = _known_bits(bits)
            if now is None:
                current.pop(code, None)
                continue
            before = current.get(code)
            if before is not None:
                transitions = (before ^ now).bit_count()
                for name, _width in code_paths.get(code, []):
                    counts[name] = counts.get(name, 0) + transitions
            current[code] = now
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-odb", required=True, type=Path)
    parser.add_argument("--sdc", required=True, type=Path)
    parser.add_argument("--spef", required=True, type=Path)
    parser.add_argument("--lib", required=True, action="append", type=Path)
    parser.add_argument("--rc-tcl", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--zero-delay-vcd", type=Path)
    parser.add_argument("--sdf-vcd", type=Path)
    parser.add_argument("--scope", help="common VCD scope; required with paired VCD inputs")
    parser.add_argument("--zero-delay-scope")
    parser.add_argument("--sdf-scope")
    parser.add_argument("--openroad", type=Path, default=DEFAULT_OPENROAD)
    args = parser.parse_args()
    paths = [args.input_odb, args.sdc, args.spef, args.rc_tcl, args.openroad, *args.lib]
    if args.zero_delay_vcd:
        paths.append(args.zero_delay_vcd)
    if args.sdf_vcd:
        paths.append(args.sdf_vcd)
    for path in paths:
        if not path.is_file():
            raise SystemExit(f"missing input file: {path}")
    if bool(args.zero_delay_vcd) != bool(args.sdf_vcd):
        raise SystemExit("both --zero-delay-vcd and --sdf-vcd are required for glitch comparison")
    if args.zero_delay_vcd and not args.scope:
        if not (args.zero_delay_scope and args.sdf_scope):
            raise SystemExit("--scope or both --zero-delay-scope/--sdf-scope are required with paired VCD inputs")
    zero_scope = args.zero_delay_scope or args.scope
    sdf_scope = args.sdf_scope or args.scope
    out = args.output_dir.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"refusing to overwrite non-empty output directory: {out}")
    out.mkdir(parents=True, exist_ok=True)

    sdf = out / "stage3e_routed.sdf"
    arrival_tsv = out / "stage3e_arrival_skew.tsv"
    cap_tsv = out / "stage3e_net_capacitance.tsv"
    log = out / "stage3e.log"
    env = os.environ.copy()
    env.update({"PASS3E_ODB": str(args.input_odb.resolve()),
                "PASS3E_SPEF": str(args.spef.resolve()),
                "PASS3E_SDC": str(args.sdc.resolve()),
                "PASS3E_LIBS": "\n".join(str(path.resolve()) for path in args.lib),
                "PASS3E_RC_TCL": str(args.rc_tcl.resolve()),
                "PASS3E_SDF": str(sdf),
                "PASS3E_ARRIVAL_TSV": str(arrival_tsv),
                "PASS3E_NET_CAP_TSV": str(cap_tsv)})
    run = subprocess.run([str(args.openroad.resolve()), "-no_init", "-exit", str(TCL)],
                         cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, check=False)
    log.write_text(run.stdout)
    if run.returncode or not sdf.is_file() or not arrival_tsv.is_file() or not cap_tsv.is_file():
        raise SystemExit(f"OpenROAD SDF/arrival diagnostic failed; inspect {log}")

    cap_rows: dict[str, float] = {}
    with cap_tsv.open(newline="") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            try:
                cap_rows[row["net"]] = float(row["capacitance_ff"])
            except (ValueError, KeyError):
                continue
    glitch_tsv = out / "stage3e_glitch_diagnostic_report.tsv"
    glitch_result: dict[str, object]
    if args.zero_delay_vcd and args.sdf_vcd:
        zero = vcd_toggle_counts(args.zero_delay_vcd, zero_scope)
        sdf_counts = vcd_toggle_counts(args.sdf_vcd, sdf_scope)
        common = sorted(zero.keys() & sdf_counts.keys())
        with glitch_tsv.open("w") as stream:
            stream.write("signal\tzero_delay_transitions\tsdf_transitions\tglitch_transitions\tcapacitance_ff\tglitch_cap_product\n")
            for signal in common:
                extra = max(0, sdf_counts[signal] - zero[signal])
                cap = cap_rows.get(signal, 0.0)
                stream.write(f"{signal}\t{zero[signal]}\t{sdf_counts[signal]}\t{extra}\t{cap:.9g}\t{extra * cap:.9g}\n")
        total_sdf = sum(sdf_counts[name] for name in common)
        total_extra = sum(max(0, sdf_counts[name] - zero[name]) for name in common)
        glitch_result = {"status": "MEASURED" if common else "NO_COMMON_VCD_SIGNALS",
                         "matched_signal_count": len(common), "glitch_transitions": total_extra,
                         "sdf_transitions": total_sdf,
                         "glitch_transition_fraction": total_extra / total_sdf if total_sdf else None,
                         "vcd_capacitance_match_count": sum(name in cap_rows for name in common)}
    else:
        glitch_tsv.write_text("signal\tzero_delay_transitions\tsdf_transitions\tglitch_transitions\tcapacitance_ff\tglitch_cap_product\n")
        glitch_result = {"status": "PENDING_PAIRED_SDF_VCD", "matched_signal_count": 0,
                         "glitch_transitions": None, "glitch_transition_fraction": None}

    output_odb = out / "stage3e.odb"
    shutil.copy2(args.input_odb, output_odb)
    manifest = {
        "schema_version": 1, "pass": "3E", "name": "SDF glitch/arrival diagnostic",
        "status": "DIAGNOSTIC_COMPLETE" if glitch_result["status"] == "MEASURED" else "SDF_WRITTEN_GLITCH_MEASUREMENT_PENDING",
        "mutation_enabled": False,
        "inputs": {"odb": {"path": str(args.input_odb.resolve()), "sha256": sha256(args.input_odb)},
                   "sdc": {"path": str(args.sdc.resolve()), "sha256": sha256(args.sdc)},
                   "spef": {"path": str(args.spef.resolve()), "sha256": sha256(args.spef)},
                   "liberties": [{"path": str(path.resolve()), "sha256": sha256(path)} for path in args.lib],
                   "zero_delay_vcd": ({"path": str(args.zero_delay_vcd.resolve()), "sha256": sha256(args.zero_delay_vcd)} if args.zero_delay_vcd else None),
                   "sdf_vcd": ({"path": str(args.sdf_vcd.resolve()), "sha256": sha256(args.sdf_vcd)} if args.sdf_vcd else None),
                   "vcd_scope": {"zero_delay": zero_scope, "sdf": sdf_scope}},
        "outputs": {"odb": {"path": str(output_odb), "sha256": sha256(output_odb)},
                    "sdf": {"path": str(sdf), "sha256": sha256(sdf)},
                    "arrival_skew": {"path": str(arrival_tsv), "sha256": sha256(arrival_tsv)},
                    "net_capacitance": {"path": str(cap_tsv), "sha256": sha256(cap_tsv)},
                    "glitch_report": {"path": str(glitch_tsv), "sha256": sha256(glitch_tsv)},
                    "log": {"path": str(log), "sha256": sha256(log)}},
        "arrival_rows": max(0, sum(1 for _ in arrival_tsv.open()) - 1),
        "arrival_skew_distribution_ns": {},
        "glitch_measurement": glitch_result,
        "phase1_preserved": True,
    }
    with arrival_tsv.open(newline="") as stream:
        skews = sorted(float(row["arrival_skew_ns"]) for row in csv.DictReader(stream, delimiter="\t")
                       if row.get("arrival_skew_ns"))
    if skews:
        manifest["arrival_skew_distribution_ns"] = {
            "min": skews[0], "median": skews[(len(skews) - 1) // 2],
            "p95": skews[min(len(skews) - 1, int(0.95 * (len(skews) - 1)))],
            "max": skews[-1],
        }
    (out / "stage3e_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"PASS3E_RESULT status={manifest['status']} arrival_rows={manifest['arrival_rows']} "
          f"glitch_status={glitch_result['status']} manifest={out / 'stage3e_manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
