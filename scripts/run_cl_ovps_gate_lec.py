#!/usr/bin/env python3
"""Compare routed Sky130HD netlists after graph-verified clock abstraction.

Only pure buffer/inverter cones that terminate exclusively at recognized clock
boundary pins are removed. Clock gates, muxes, dividers, macros, and sequential
elements remain in the proof. Every endpoint must retain the same source and
inversion parity in baseline and candidate; ambiguity is a hard failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from cl_ovps_sky130_lec_models import CELL_MODEL, PRIMITIVES, primitive_blackbox_stubs
from cl_ovps_clock_topology import (
    ClockTopologyError,
    analyze_clock_topology,
    assert_topology_equivalent,
    extract_top_module,
    parse_instances,
    rewrite_clock_cones,
    _is_sky130_antenna_diode,
    _simple_net,
    _top_ports,
)

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sky130_cell_ports(model_path: Path) -> dict[str, set[str]]:
    """Read Sky130HD module port names for adding omitted physical supplies."""
    model_text = model_path.read_text(errors="replace")
    ports: dict[str, set[str]] = {}
    for match in re.finditer(
        r"(?ms)^module\s+(sky130_fd_sc_hd__[A-Za-z0-9_$]+)\s*\((.*?)\)\s*;",
        model_text,
    ):
        ports.setdefault(match.group(1), set()).update(
            re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", match.group(2)))
    return ports


def expand_icgs_to_enable_flops(text: str, top: str) -> tuple[str, int]:
    """Expand latch-based ICG clocking to equivalent enable-flop logic for LEC.

    The physical netlist and its VCD simulation retain the real ICG. In the
    formal copy, each supported ICG must exclusively clock positive-edge
    Sky130 dfxtp/edfxtp sinks. Its GCLK is then replaced by CLK and its GATE
    condition is represented on the sink's DE pin. This is the synchronous
    next-state abstraction of the ICG; the separate Liberty clock-gating STA
    audit checks that the physical latch receives a stable enable.
    """
    module_start, module_end, module_text = extract_top_module(text, top)
    instances = parse_instances(module_text)
    icgs = [instance for instance in instances
            if re.search(r"(?:^|__)s?dlclkp_[124]$", instance.master, re.IGNORECASE)]
    if not icgs:
        return text, 0
    consumers: dict[str, list[tuple[object, str]]] = {}
    for instance in instances:
        for connection in instance.connections:
            try:
                net = _simple_net(connection.expression, f"{instance.name}/{connection.pin}")
            except ClockTopologyError:
                continue
            consumers.setdefault(net, []).append((instance, connection.pin))

    replacements: list[tuple[int, int, str]] = []
    processed_sinks: set[str] = set()
    for icg in icgs:
        pins = icg.pins()
        if not {"CLK", "GATE", "GCLK"} <= set(pins):
            raise ClockTopologyError(f"unsupported ICG pin set on {icg.name}")
        clock_net = _simple_net(pins["CLK"].expression, f"{icg.name}/CLK")
        gate_net = _simple_net(pins["GATE"].expression, f"{icg.name}/GATE")
        gated_net = _simple_net(pins["GCLK"].expression, f"{icg.name}/GCLK")
        if gate_net in {clock_net, gated_net}:
            raise ClockTopologyError(f"ambiguous or self-referential gate enable on {icg.name}")
        sink_rows = [(consumer, pin) for consumer, pin in consumers.get(gated_net, [])
                     if not (consumer.name == icg.name and pin == "GCLK")]
        if not sink_rows:
            raise ClockTopologyError(f"ICG {icg.name} has no sequential GCLK sinks")
        for sink, pin in sink_rows:
            master = sink.master
            is_enabled = bool(re.fullmatch(r"sky130_fd_sc_hd__edfxtp_[124]", master))
            is_plain = bool(re.fullmatch(r"sky130_fd_sc_hd__dfxtp_[124]", master))
            if pin != "CLK" or not (is_enabled or is_plain):
                if ("clkload" in sink.name.lower() or "clkload" in master.lower()
                        or _is_sky130_antenna_diode(sink, pin)):
                    continue
                raise ClockTopologyError(
                    f"ICG {icg.name} also drives unsupported/non-clock sink {sink.name}/{pin} ({master})"
                )
            sink_name = sink.name.lstrip("\\")
            if sink_name in processed_sinks:
                raise ClockTopologyError(f"sequential sink {sink_name} is driven by multiple ICGs")
            processed_sinks.add(sink_name)
            sink_text = module_text[sink.start:sink.end]
            clk_conn = sink.pins()["CLK"]
            local_start, local_end = clk_conn.start - sink.start, clk_conn.end - sink.start
            sink_text = sink_text[:local_start] + clock_net + sink_text[local_end:]
            if is_enabled:
                de_connection = sink.pins().get("DE")
                if de_connection is None or _simple_net(de_connection.expression, f"{sink.name}/DE") != gate_net:
                    raise ClockTopologyError(
                        f"ICG enable does not match existing DE pin on {sink.name}"
                    )
            else:
                sink_text, count = re.subn(
                    r"\bsky130_fd_sc_hd__dfxtp_([124])\b",
                    r"sky130_fd_sc_hd__edfxtp_\1", sink_text, count=1)
                if count != 1:
                    raise ClockTopologyError(f"could not map plain DFF {sink.name} to enabled DFF")
                close = sink_text.rfind(");")
                if close < 0:
                    raise ClockTopologyError(f"malformed DFF instance {sink.name}")
                sink_text = sink_text[:close] + f", .DE({gate_net})" + sink_text[close:]
            replacements.append((module_start + sink.start, module_start + sink.end, sink_text))
        replacements.append((module_start + icg.start, module_start + icg.end, ""))

    result = text
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result, len(icgs)


def namespace_modules(text: str, module_names: list[str], top_name: str,
                     top_prefix: str, prefix: str) -> str:
    """Rename every user module and instance reference to isolate each side."""
    replacements: dict[str, str] = {}
    index = 0
    for old in module_names:
        canonical = old[1:] if old.startswith("\\") else old
        if canonical == top_name:
            new = top_prefix
        else:
            new = f"{prefix}_hier_{index}"
            index += 1
        replacements[old] = new

    # Replace all old module tokens in one pass, including escaped Verilog
    # identifiers. A one-pass substitution avoids a replacement name matching
    # a later source module name.
    alternatives = []
    for old in sorted(replacements, key=len, reverse=True):
        token = re.escape(old)
        if not old.startswith("\\"):
            token = rf"(?<![A-Za-z0-9_$]){token}(?![A-Za-z0-9_$])"
        alternatives.append(f"(?P<m{len(alternatives)}>{token})")
    pattern = re.compile("|".join(alternatives))
    by_group = {f"m{index}": old for index, old in enumerate(
        sorted(replacements, key=len, reverse=True))}
    return pattern.sub(lambda match: replacements[by_group[match.lastgroup]], text)


def normalize_netlist(source: Path, output: Path, top: str, prefix: str,
                      clock_input: str | None, model_path: Path) -> tuple[int, int, int, int, dict[str, object], int]:
    text = source.read_text(errors="replace")
    module_re = re.compile(
        r"(?m)^\s*module\s+(\\[^\s(]+|[A-Za-z_$][A-Za-z0-9_$]*)")
    declarations = list(module_re.finditer(text))
    modules = [match.group(1) for match in declarations]
    matching_top = [name for name in modules
                    if (name[1:] if name.startswith("\\") else name) == top]
    if len(matching_top) != 1:
        raise ValueError(f"expected exactly one module {top!r} in {source}; found {modules}")
    top_decl = matching_top[0]
    header = re.search(rf"(?ms)^\s*module\s+{re.escape(top_decl)}\s*\((.*?)\);", text)
    if not header:
        raise ValueError(f"top module {top!r} has no parseable port list")
    if clock_input:
        inputs, _outputs, _inouts = _top_ports(extract_top_module(text, top)[2])
        if clock_input not in inputs:
            raise ValueError(f"top module {top!r} has no declared input clock {clock_input!r}")

    topology = analyze_clock_topology(text, top)
    text, clock_counts = rewrite_clock_cones(text, topology, top)
    text, icg_expansions = expand_icgs_to_enable_flops(text, top)

    # Remove balancing loads only if they have no functional output fanout.
    module_start, module_end, top_text = extract_top_module(text, top)
    top_instances = parse_instances(top_text)
    _inputs, top_outputs, top_inouts = _top_ports(top_text)
    consumers: dict[str, set[str]] = {}
    output_pin_names = {"X", "Y", "Z", "Q", "QN", "Q_N", "GCLK", "OUT", "O"}
    for item in top_instances:
        for connection in item.connections:
            try:
                net = _simple_net(connection.expression, f"{item.name}/{connection.pin}")
            except ClockTopologyError:
                continue
            if connection.pin in output_pin_names:
                consumers.setdefault(net, set()).add(item.name.lstrip("\\"))
    remove_dummies: set[str] = set()
    for item in top_instances:
        name = item.name.lstrip("\\")
        if not (name.lower().startswith("clkload") or "clkload" in item.master.lower()):
            continue
        pins = item.pins()
        for pin in ("X", "Y", "Z", "Q", "OUT", "O"):
            connection = pins.get(pin)
            if connection is None:
                continue
            try:
                net = _simple_net(connection.expression, f"{name}/{pin}")
            except ClockTopologyError:
                continue
            if net in top_outputs or net in top_inouts or consumers.get(net, set()) - {name}:
                raise ClockTopologyError(f"dummy load {name} drives a functional or clock net")
        remove_dummies.add(name)
    if remove_dummies:
        rewritten = top_text
        for item in sorted((i for i in top_instances if i.name.lstrip("\\") in remove_dummies),
                           key=lambda i: i.start, reverse=True):
            rewritten = rewritten[:item.start] + rewritten[item.end:]
        text = text[:module_start] + rewritten + text[module_end:]

    # Baseline and candidate often contain many identically named mapped
    # hierarchy modules. Namespace the complete module graph before reading
    # both netlists into one Yosys design; the proof script then flattens each
    # selected top independently.
    top_prefix = f"{top}_{prefix}"
    text = namespace_modules(text, modules, top, top_prefix, prefix)

    removed_clock = int(clock_counts["transport_removed"])
    removed_dummy = len(remove_dummies)

    tie_cell = re.compile(
        r"(?ms)^[ \t]*sky130_fd_sc_hd__conb_1\s+[^\s(]+\s*\((.*?)\);[ \t]*(?:\r?\n|$)")

    def expand_tie(match: re.Match[str]) -> str:
        pins = dict(re.findall(r"\.([A-Za-z0-9_$]+)\s*\(\s*([^()]+?)\s*\)", match.group(1)))
        assigns = []
        if "HI" in pins:
            assigns.append(f"  assign {pins['HI']} = 1'b1;")
        if "LO" in pins:
            assigns.append(f"  assign {pins['LO']} = 1'b0;")
        if not assigns:
            raise ValueError(f"could not expand conb_1 in {source}")
        return "\n".join(assigns) + "\n"

    text, ties = tie_cell.subn(expand_tie, text)

    # ORFS gate-level Verilog omits physical supply pins. Tie those ports to
    # their functional values so the Liberty/Verilog power-good models do not
    # leave otherwise valid datapath state unknown in Yosys.
    cell_ports = sky130_cell_ports(model_path)
    supply_values = {"VPWR": "1'b1", "VGND": "1'b0", "VPB": "1'b1", "VNB": "1'b0"}
    supply_pin_count = 0
    cell_instance = re.compile(
        r"(?ms)^([ \t]*)(sky130_fd_sc_hd__[A-Za-z0-9_$]+)([ \t]+)"
        r"([^\s(]+)\s*\((.*?)\);"
    )

    def connect_supplies(match: re.Match[str]) -> str:
        nonlocal supply_pin_count
        cell_type, body = match.group(2), match.group(5)
        if cell_type not in cell_ports:
            return match.group(0)
        connected = set(re.findall(r"\.([A-Za-z0-9_$]+)\s*\(", body))
        missing = [pin for pin, value in supply_values.items()
                   if pin in cell_ports[cell_type] and pin not in connected]
        if not missing:
            return match.group(0)
        supply_pin_count += len(missing)
        instance = match.group(4)
        separator = " " if instance.startswith("\\") else ""
        prefix_text = match.group(1) + cell_type + match.group(3) + instance + separator + "("
        additions = ",\n".join(
            f"    .{pin}({supply_values[pin]})" for pin in missing)
        return f"{prefix_text}{body.rstrip()},\n{additions});"

    text = cell_instance.sub(connect_supplies, text)

    # Map the DFF variants present in this netlist to transparent behavioral
    # wrappers.  Their data and enable/reset behavior remains in the proof.
    sequential_map = {
        r"sky130_fd_sc_hd__edfxtp_(?:1|2|4)": "lec_edfxtp_1",
        r"sky130_fd_sc_hd__edfxtn_(?:1|2|4)": "lec_edfxtn_1",
        r"sky130_fd_sc_hd__dfxtp_(?:1|2|4)": "lec_dfxtp_1",
        r"sky130_fd_sc_hd__dfxtn_(?:1|2|4)": "lec_dfxtn_1",
        r"sky130_fd_sc_hd__dfrtp_(?:1|2|4)": "lec_dfrtp_1",
        r"sky130_fd_sc_hd__dfrtn_(?:1|2|4)": "lec_dfrtn_1",
    }
    for pattern, new in sequential_map.items():
        text = re.sub(rf"\b{pattern}(?=\s)", new, text)

    output.write_text(text)
    return removed_clock, removed_dummy, ties, supply_pin_count, topology, icg_expansions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-netlist", required=True, type=Path)
    parser.add_argument("--candidate-netlist", required=True, type=Path)
    parser.add_argument("--top", required=True)
    parser.add_argument("--clock-input",
                        help="optional assertion that a named top-level clock exists; roots are analyzed per graph")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--yosys", default=shutil.which("yosys"))
    parser.add_argument("--seq-depth", type=int, default=12)
    args = parser.parse_args()

    baseline, candidate, output = (p.resolve() for p in
                                   (args.baseline_netlist, args.candidate_netlist, args.output_dir))
    if not baseline.is_file() or not candidate.is_file():
        raise FileNotFoundError(baseline if not baseline.is_file() else candidate)
    if args.yosys is None:
        raise FileNotFoundError("yosys is required for Sky130HD gate-level LEC")
    if CELL_MODEL is None or PRIMITIVES is None or not CELL_MODEL.is_file() or not PRIMITIVES.is_file():
        raise FileNotFoundError("Sky130HD functional cell models are missing")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty LEC output {output}")
    output.mkdir(parents=True, exist_ok=True)

    gold = output / "baseline_normalized.v"
    gate = output / "candidate_normalized.v"
    gold_counts = normalize_netlist(baseline, gold, args.top, "gold", args.clock_input, CELL_MODEL)
    gate_counts = normalize_netlist(candidate, gate, args.top, "candidate", args.clock_input, CELL_MODEL)
    assert_topology_equivalent(gold_counts[4], gate_counts[4])
    topology_report = output / "clock_topology.json"
    topology_report.write_text(json.dumps({"baseline": gold_counts[4],
        "candidate": gate_counts[4], "source_and_parity_equivalence": "PASS"},
        indent=2, sort_keys=True) + "\n")
    model_dir = CELL_MODEL.parent
    stubs = output / "udp_blackboxes.v"
    stubs.write_text(primitive_blackbox_stubs(PRIMITIVES) + "\n" +
        "module lec_dfrtp_1(output reg Q, input CLK, input D, input RESET_B, "
        "input VPWR, input VGND, input VPB, input VNB);\n"
        "  always @(posedge CLK or negedge RESET_B)\n"
        "    if (!RESET_B) Q <= 1'b0; else Q <= D;\n"
        "endmodule\n"
        "module lec_dfrtn_1(output reg Q, input CLK_N, input D, input RESET_B, "
        "input VPWR, input VGND, input VPB, input VNB);\n"
        "  always @(negedge CLK_N or negedge RESET_B)\n"
        "    if (!RESET_B) Q <= 1'b0; else Q <= D;\n"
        "endmodule\n")
    proof_script = output / "equivalence.ys"
    reset_commands: list[str] = []
    reset_model = "native_cell_model"
    if any(model in gold.read_text(errors="replace") or model in gate.read_text(errors="replace")
           for model in ("lec_dfrtp_1", "lec_dfrtn_1")):
        # Yosys' SAT engine cannot directly model $adff. For designs using the
        # modeled async-reset DFF, compare next-state behavior with reset
        # sampled at the clock. Other designs retain native reset semantics.
        reset_commands = [f"async2sync {args.top}_gold",
                          f"async2sync {args.top}_candidate"]
        reset_model = "async2sync_for_dfrtp"
    proof_script.write_text("\n".join((
        f"read_verilog -D FUNCTIONAL -D NO_PRIMITIVES -DUNIT_DELAY= -I{model_dir} {stubs}",
        f"read_verilog -D FUNCTIONAL -D NO_PRIMITIVES -DUNIT_DELAY= -I{model_dir} {CELL_MODEL}",
        f"read_verilog {gold}", f"read_verilog {gate}",
        f"proc {args.top}_gold", f"proc {args.top}_candidate",
        f"flatten {args.top}_gold", f"proc {args.top}_gold",
        f"flatten {args.top}_candidate", f"proc {args.top}_candidate",
        *reset_commands,
        f"equiv_make {args.top}_gold {args.top}_candidate {args.top}_equiv",
        f"hierarchy -check -top {args.top}_equiv", f"prep -top {args.top}_equiv",
        # Use a short one-step simplification pass before induction. A deeper
        # bounded search here can dominate runtime on large AES-style mapped
        # netlists without improving the final induction proof.
        "equiv_simple -seq 1 -short", f"equiv_induct -seq {args.seq_depth}",
        "equiv_status -assert", "design -reset", "")))
    run = subprocess.run([args.yosys, "-Q", "-s", str(proof_script)], cwd=ROOT,
                         text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         check=False)
    log = output / "equivalence.log"
    log.write_text(run.stdout)
    status = "LEC_PASS" if run.returncode == 0 and "Equivalence successfully proven" in run.stdout else "LEC_NOT_PROVEN"
    (output / "status.txt").write_text(status + "\n")
    (output / "integrity.tsv").write_text(
        "artifact\tpath\tsha256\n" +
        f"baseline_netlist\t{baseline}\t{sha256(baseline)}\n" +
        f"candidate_netlist\t{candidate}\t{sha256(candidate)}\n")
    print(f"CL_OVPS_LEC status={status} top={args.top} "
          f"baseline_removed_clock={gold_counts[0]} candidate_removed_clock={gate_counts[0]} "
          f"baseline_removed_dummy={gold_counts[1]} candidate_removed_dummy={gate_counts[1]} "
          f"baseline_ties={gold_counts[2]} candidate_ties={gate_counts[2]} "
          f"baseline_supply_pins_added={gold_counts[3]} candidate_supply_pins_added={gate_counts[3]} "
          f"reset_model={reset_model} icg_enable_expansions="
          f"{gold_counts[5]}/{gate_counts[5]} "
          f"clock_sinks={gold_counts[4]['sink_count']} "
          f"clock_transport_abstraction=graph_verified topology={topology_report} log={log}")
    if status != "LEC_PASS":
        return run.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
