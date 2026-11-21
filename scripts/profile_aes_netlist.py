#!/usr/bin/env python3
"""Profile AES sequential enables, timing envelope, and VCD annotation coverage."""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from pathlib import Path


CELL_RE = re.compile(
    r"(?ms)^\s*(sky130_fd_sc_hd__\w+)\s+(\S+)\s*\((.*?)\);"
)
PORT_RE = re.compile(r"\.([A-Za-z0-9_$]+)\s*\(\s*([^()]+?)\s*\)")


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def profile(netlist: Path) -> list[dict[str, str]]:
    text = netlist.read_text(errors="replace")
    cells: list[dict[str, object]] = []
    output_driver: dict[str, dict[str, object]] = {}
    for match in CELL_RE.finditer(text):
        master, instance, body = match.groups()
        pins = {pin: net.strip() for pin, net in PORT_RE.findall(body)}
        cell = {"master": master, "instance": instance, "pins": pins}
        cells.append(cell)
        output_pin = "Q" if master.endswith(("__dfxtp_1", "__dfxtp_2", "__dfxtp_4",
                                              "__edfxtp_1")) else None
        if master.startswith("sky130_fd_sc_hd__mux2"):
            output_pin = "Y"
        if output_pin and output_pin in pins:
            output_driver[pins[output_pin]] = cell

    rows: list[dict[str, str]] = []
    for cell in cells:
        master = str(cell["master"])
        if "__dfxtp_" not in master and "__edfxtp_" not in master:
            continue
        pins = cell["pins"]
        assert isinstance(pins, dict)
        q_net, d_net = pins.get("Q", ""), pins.get("D", "")
        enable, source = "", "none"
        if "DE" in pins:
            enable, source = pins["DE"], "direct_enable_pin"
        else:
            driver = output_driver.get(d_net)
            if driver:
                driver_master = str(driver["master"])
                driver_pins = driver["pins"]
                if driver_master.startswith("sky130_fd_sc_hd__mux2"):
                    data_inputs = [name for name in ("A0", "A1") if name in driver_pins]
                    if q_net in [driver_pins[name] for name in data_inputs]:
                        enable = driver_pins.get("S", "")
                        source = "feedback_mux_select" if enable else "feedback_mux_enable_unresolved"
        rows.append({
            "instance": str(cell["instance"]), "master": master,
            "clock_net": pins.get("CLK", ""), "d_net": d_net, "q_net": q_net,
            "enable_net": enable, "enable_source": source,
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--netlist", type=Path, required=True)
    parser.add_argument("--power-summary", type=Path, required=True)
    parser.add_argument("--variant", help="select this variant's idle/typical/peak power rows")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    netlist, summary, output = args.netlist.resolve(), args.power_summary.resolve(), args.output_dir.resolve()
    if not netlist.is_file() or not summary.is_file():
        raise FileNotFoundError(netlist if not netlist.is_file() else summary)
    output.mkdir(parents=True, exist_ok=True)
    rows = profile(netlist)
    with (output / "sequential_enable_profile.tsv").open("w", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    power_rows = read_tsv(summary)
    if args.variant:
        power_rows = [row for row in power_rows if row.get("variant") == args.variant]
    by_workload = {row["workload"]: row for row in power_rows}
    if set(by_workload) != {"idle", "typical", "peak"}:
        raise ValueError(f"expected idle/typical/peak power rows in {summary}")
    enable_counts = Counter(row["enable_source"] for row in rows)
    lines = [
        "# AES Module 0 profile",
        "",
        f"- Netlist: `{netlist}`",
        f"- Sequential clock sinks: {len(rows)}",
        f"- Direct enable pins: {enable_counts['direct_enable_pin']}",
        f"- Feedback-mux enable traces: {enable_counts['feedback_mux_select']}",
        f"- No explicit enable or feedback-mux pattern: {enable_counts['none'] + enable_counts['feedback_mux_enable_unresolved']}",
        "",
        "| Workload | WNS (ns) | Hold slack (ns) | Annotated pins | Unannotated pins |",
        "|---|---:|---:|---:|---:|",
    ]
    for workload in ("idle", "typical", "peak"):
        row = by_workload[workload]
        lines.append(f"| {workload} | {float(row['setup_slack_ns']):.6f} | "
                     f"{float(row['hold_slack_ns']):.6f} | {row['vcd_annotated_pin_activities']} | "
                     f"{row['vcd_unannotated_pins']} |")
    (output / "module0_profile.md").write_text("\n".join(lines) + "\n")
    print(f"AES_MODULE0_PROFILE sinks={len(rows)} direct_enables={enable_counts['direct_enable_pin']} "
          f"feedback_mux_enables={enable_counts['feedback_mux_select']} "
          f"no_enable_pattern={enable_counts['none'] + enable_counts['feedback_mux_enable_unresolved']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
