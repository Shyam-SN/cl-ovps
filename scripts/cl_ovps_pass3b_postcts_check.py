#!/usr/bin/env python3
"""Fail-closed Liberty clock-gating setup/hold audit for CTS or routed ODBs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
CHECK_TCL = ROOT / "scripts/stage3b_icg_postcts_check.tcl"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def matching_brace(text: str, opening: int) -> int:
    depth = 0
    quoted = False
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("unbalanced Liberty braces")


def clock_gate_sta_overlay(source_text: str) -> tuple[str, int]:
    """Expose the Sky130 latch gate as an AND clock gate to OpenSTA only."""
    cells = list(re.finditer(r'\bcell\s*\("sky130_fd_sc_hd__(?:s)?dlclkp_[124]"\)\s*\{', source_text))
    if not cells:
        raise ValueError("no Sky130 dlclkp/sdlclkp masters found in Liberty")
    replacements = 0
    edits: list[tuple[int, int, str]] = []
    for cell_match in cells:
        cell_open = source_text.find("{", cell_match.start())
        cell_close = matching_brace(source_text, cell_open)
        cell_text = source_text[cell_open + 1:cell_close]
        pin_match = re.search(r'\bpin\s*\("GCLK"\)\s*\{', cell_text)
        if not pin_match:
            raise ValueError(f"GCLK pin missing in {cell_match.group(0)}")
        pin_open = cell_open + 1 + cell_text.find("{", pin_match.start())
        pin_close = matching_brace(source_text, pin_open)
        pin_text = source_text[pin_open + 1:pin_close]
        state = 'state_function : "(CLK*M0)";'
        if state not in pin_text:
            raise ValueError("expected Sky130 ICG state_function is absent from GCLK pin")
        offset = pin_open + 1 + pin_text.index(state)
        edits.append((offset, offset + len(state), 'function : "(CLK*GATE)";'))
        # OpenSTA's gated-clock detector walks a combinational GATE->GCLK arc.
        # The physical latch filters enable changes; the separate Liberty
        # setup/hold checks below enforce that the enable is stable in phase.
        timing = ('\n            timing () {\n'
                  '                related_pin : "GATE";\n'
                  '                timing_sense : positive_unate;\n'
                  '                timing_type : combinational;\n'
                  '            }\n')
        edits.append((pin_close, pin_close, timing))
        replacements += 1
    result = source_text
    for begin, end, replacement in sorted(edits, reverse=True):
        result = result[:begin] + replacement + result[end:]
    return result, replacements


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-odb", required=True, type=Path)
    parser.add_argument("--sdc", required=True, type=Path)
    parser.add_argument("--lib", required=True, action="append", type=Path)
    parser.add_argument("--rc-tcl", required=True, type=Path)
    parser.add_argument("--spef", type=Path,
                        help="optional extracted SPEF; omit for placement-estimated RC")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--openroad", type=Path, default=DEFAULT_OPENROAD)
    args = parser.parse_args()
    required = [args.input_odb, args.sdc, args.rc_tcl, args.openroad, *args.lib]
    if args.spef:
        required.append(args.spef)
    for path in required:
        if not path.is_file():
            parser.error(f"missing clock-gating audit input: {path}")
    out = args.output_dir.resolve()
    if out.exists() and any(out.iterdir()):
        parser.error(f"refusing to overwrite non-empty audit directory: {out}")
    out.mkdir(parents=True, exist_ok=True)
    log = out / "clock_gating_check.log"
    report = out / "clock_gating_check.rpt"
    # OpenSTA's gated-clock checker needs a Boolean output function and a
    # GATE->GCLK timing edge. Sky130 models the latch using state_function and
    # omits that combinational arc. Make a private timing-only Liberty copy;
    # the original model remains in use for simulation and power analysis.
    sta_libs: list[Path] = []
    overlays: list[dict[str, str]] = []
    for index, source in enumerate(args.lib):
        source_text = source.read_text(errors="replace")
        try:
            overlay_text, replacements = clock_gate_sta_overlay(source_text)
        except ValueError as exc:
            parser.error(f"cannot make clock-gating STA overlay for {source}: {exc}")
        overlay = out / f"clock_gating_sta_overlay_{index}.lib"
        overlay.write_text(overlay_text)
        sta_libs.append(overlay)
        overlays.append({"path": str(overlay), "sha256": sha256(overlay),
                         "source": str(source.resolve()), "icg_cell_models": str(replacements)})
    env = os.environ.copy()
    env.update({
        "P3B_INPUT_ODB": str(args.input_odb.resolve()),
        "P3B_SDC": str(args.sdc.resolve()),
        "P3B_LIBS": "\n".join(str(path.resolve()) for path in sta_libs),
        "P3B_RC_TCL": str(args.rc_tcl.resolve()),
        "P3B_REPORT": str(report),
        "P3B_SPEF": str(args.spef.resolve()) if args.spef else "",
    })
    run = subprocess.run([str(args.openroad.resolve()), "-no_init", "-exit", str(CHECK_TCL)],
                         cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, check=False)
    log.write_text(run.stdout)
    match = re.search(r"(?m)^P3B_POSTCTS_RESULT status=(\S+) icgs=(\d+) "
                      r"setup_checks=(\S+) hold_checks=(\S+) violations=(\S+)$", run.stdout)
    status = match.group(1) if match else "ERROR"
    icgs = int(match.group(2)) if match else None
    if run.returncode or status not in {"PASS", "SKIPPED_NO_ICG"}:
        manifest_status = "FAIL"
    else:
        manifest_status = "PASS" if status == "PASS" else "SKIPPED_NO_ICG"
    manifest = {
        "schema_version": 1,
        "status": manifest_status,
        "openroad_status": status,
        "icg_count": icgs,
        "setup_checks": match.group(3) if match else None,
        "hold_checks": match.group(4) if match else None,
        "violations": match.group(5) if match else None,
        "inputs": {"odb": {"path": str(args.input_odb.resolve()), "sha256": sha256(args.input_odb)},
                   "sdc": {"path": str(args.sdc.resolve()), "sha256": sha256(args.sdc)},
                   "spef": ({"path": str(args.spef.resolve()), "sha256": sha256(args.spef)}
                            if args.spef else None),
                   "liberty": [{"path": str(path.resolve()), "sha256": sha256(path)}
                               for path in args.lib]},
        "sta_only_liberty_overlays": overlays,
        "outputs": {"report": str(report), "log": str(log)},
    }
    (out / "clock_gating_check_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"PASS3B_AUDIT status={manifest_status} icgs={icgs} report={report}")
    if manifest_status == "FAIL":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
