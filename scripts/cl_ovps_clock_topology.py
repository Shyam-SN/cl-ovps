#!/usr/bin/env python3
"""Conservative clock-transport graph analysis for CL-OVPS gate-level netlists.

The analyzer intentionally recognizes only single-input/single-output buffer
and inverter cells. Unknown logic, clock muxes, dividers, ICGs, macros, and
sequential outputs are graph boundaries. It refuses ambiguous drivers or
non-clock fanout on a transport cone instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


class ClockTopologyError(ValueError):
    pass


@dataclass(frozen=True)
class Connection:
    pin: str
    expression: str
    start: int
    end: int


@dataclass(frozen=True)
class Instance:
    master: str
    name: str
    start: int
    end: int
    connections: tuple[Connection, ...]

    def pins(self) -> dict[str, Connection]:
        return {connection.pin: connection for connection in self.connections}


@dataclass(frozen=True)
class SinkPath:
    sink: str
    pin: str
    net: str
    kind: str
    root: str
    root_net: str
    rewire_net: str
    parity: int
    transport: tuple[str, ...]
    icgs: tuple[str, ...]


def _mask_comments(text: str) -> str:
    """Replace comments with spaces while preserving source offsets."""
    chars = list(text)
    for match in re.finditer(r"(?ms)/\*.*?\*/|//[^\n]*", text):
        for index in range(match.start(), match.end()):
            if chars[index] != "\n":
                chars[index] = " "
    return "".join(chars)


def _matching_paren(text: str, start: int) -> int:
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    raise ClockTopologyError("unterminated instance connection list")


def parse_instances(text: str) -> list[Instance]:
    masked = _mask_comments(text)
    header = re.compile(
        r"(?m)^[ \t]*(?P<master>[A-Za-z_$\\][A-Za-z0-9_$\\.$]*)"
        r"[ \t]+(?P<name>\\[^\s]+|[A-Za-z_$][A-Za-z0-9_$]*)[ \t]*\("
    )
    parsed: list[Instance] = []
    offset = 0
    while True:
        match = header.search(masked, offset)
        if not match:
            break
        if match.group("master") in {"module", "primitive", "function", "task"}:
            offset = match.end()
            continue
        open_paren = masked.find("(", match.start(), match.end())
        close_paren = _matching_paren(masked, open_paren)
        after = close_paren + 1
        while after < len(masked) and masked[after].isspace():
            after += 1
        if after >= len(masked) or masked[after] != ";":
            offset = match.end()
            continue
        body_start = open_paren + 1
        body = masked[body_start:close_paren]
        connections: list[Connection] = []
        for connection in re.finditer(
            r"\.\s*(?P<pin>[A-Za-z_$][A-Za-z0-9_$]*)\s*\(\s*(?P<expr>[^()]*)\s*\)", body
        ):
            expression = connection.group("expr").strip()
            start = body_start + connection.start("expr")
            end = body_start + connection.end("expr")
            connections.append(Connection(connection.group("pin"), expression, start, end))
        if not connections:
            offset = after + 1
            continue
        parsed.append(Instance(match.group("master"), match.group("name"),
                               match.start(), after + 1, tuple(connections)))
        offset = after + 1
    names = [instance.name for instance in parsed]
    if len(names) != len(set(names)):
        raise ClockTopologyError("duplicate instance names in selected module")
    return parsed


def _basename(master: str) -> str:
    return master.rsplit("__", 1)[-1].lower()


def transport_kind(master: str) -> tuple[str, int, str, str] | None:
    """Return (kind, parity, input-pin, output-pin) for known pure cells."""
    base = _basename(master)
    if re.fullmatch(r"(?:clk)?buf(?:buf)?_[0-9]+", base):
        return ("PURE_CLK_BUF", 0, "A", "X")
    if re.fullmatch(r"(?:clk)?inv_[0-9]+", base):
        return ("PURE_CLK_INV", 1, "A", "Y")
    return None


def _is_sky130_antenna_diode(instance: Instance, pin: str) -> bool:
    """Identify the known passive Sky130HD antenna diode connection.

    Antenna repair inserts ``sky130_fd_sc_hd__diode_2`` as a one-pin load on
    routed nets, including clock nets. It does not drive functional logic, so
    it must not make a pure clock-transport cone look like datapath fanout for
    LEC abstraction. Keep this exact to the characterized cell and pin.
    """
    pins = set(instance.pins())
    return (_basename(instance.master) == "diode_2"
            and "DIODE" in pins
            and pins <= {"DIODE", "VPWR", "VGND", "VPB", "VNB"}
            and pin == "DIODE")


def _is_sequential(master: str) -> bool:
    base = _basename(master)
    return bool(re.match(r"^(?:e?df|sdf|sdff|dfbb|dfsb|dfr|dfst|dfrb|dfrn|dfsn)", base))


def _is_icg(master: str) -> bool:
    return bool(re.search(r"(?:dlclkp|sdlclkp|clkgate|icg)", master, re.I))


def _is_macro(master: str) -> bool:
    return bool(re.search(r"(?:sram|ram|mem|register.?file|(?:^|[_$])rf(?:$|[_$])|macro)", master, re.I))


def _is_macro_clock_pin(pin: str) -> bool:
    return bool(re.fullmatch(r"(?:CLK(?:_?N|[0-9]+|[AB])?|CLOCK|CK(?:[AB][0-9]*)?|RCLK|WCLK|CLKIN)",
                             pin.upper()))


def _simple_net(expression: str, where: str) -> str:
    value = expression.strip()
    if not re.fullmatch(r"(?:\\[^\s]+|[A-Za-z_$][A-Za-z0-9_$]*)", value):
        raise ClockTopologyError(f"non-scalar clock connection at {where}: {value!r}")
    return value[1:] if value.startswith("\\") else value


def _driver_pin(instance: Instance, net: str) -> str | None:
    pins = instance.pins()
    kind = transport_kind(instance.master)
    if kind:
        output = kind[3]
        connection = pins.get(output)
        if connection and _simple_net(connection.expression, f"{instance.name}/{output}") == net:
            return output
        return None
    # These are output names in the Sky130HD standard-cell library and common
    # macro wrappers. Unknown cells remain boundaries; their logic is retained.
    output_names = {"X", "Y", "Z", "Q", "QN", "Q_N", "GCLK", "CLKOUT", "OUT", "O"}
    for pin in output_names:
        connection = pins.get(pin)
        if connection:
            try:
                if _simple_net(connection.expression, f"{instance.name}/{pin}") == net:
                    return pin
            except ClockTopologyError:
                continue
    return None


def _top_ports(text: str) -> tuple[set[str], set[str], set[str]]:
    masked = _mask_comments(text)
    module = re.search(r"(?ms)^\s*module\s+(?:\\[^\s(]+|[A-Za-z_$][A-Za-z0-9_$]*)\s*\((.*?)\)\s*;", masked)
    if not module:
        raise ClockTopologyError("could not parse top module port list")
    inputs: set[str] = set()
    outputs: set[str] = set()
    inouts: set[str] = set()
    for direction, names in re.findall(
        r"(?m)^\s*(input|output|inout)\s+(?:wire\s+|reg\s+|logic\s+)?(?:\[[^]]+\]\s*)?([^;]+);", masked
    ):
        for name in names.split(","):
            match = re.search(r"([A-Za-z_$][A-Za-z0-9_$]*|\\[^\s,]+)\s*$", name.strip())
            if not match:
                continue
            signal = match.group(1)
            if signal.startswith("\\"):
                signal = signal[1:]
            {"input": inputs, "output": outputs, "inout": inouts}[direction].add(signal)
    # ANSI style declarations may place directions in the module header.
    header = module.group(1)
    for direction, names in re.findall(r"\b(input|output|inout)\b([^,]*)", header):
        name_match = re.search(r"([A-Za-z_$][A-Za-z0-9_$]*|\\[^\s,]+)\s*$", names.strip())
        if name_match:
            signal = name_match.group(1).lstrip("\\")
            {"input": inputs, "output": outputs, "inout": inouts}[direction].add(signal)
    return inputs, outputs, inouts


def extract_top_module(text: str, top: str) -> tuple[int, int, str]:
    """Locate one module body and return its source span and text."""
    masked = _mask_comments(text)
    declarations = list(re.finditer(
        r"(?m)^\s*module\s+(?P<name>\\[^\s(]+|[A-Za-z_$][A-Za-z0-9_$]*)", masked
    ))
    matching = [match for match in declarations
                if match.group("name").lstrip("\\") == top]
    if len(matching) != 1:
        raise ClockTopologyError(
            f"expected exactly one top module {top!r}; found {len(matching)}"
        )
    start = matching[0].start()
    endmodule = re.search(r"(?m)^\s*endmodule\b", masked[matching[0].end():])
    if not endmodule:
        raise ClockTopologyError(f"top module {top!r} has no endmodule")
    end = matching[0].end() + endmodule.end()
    return start, end, text[start:end]


def analyze_clock_topology(text: str, top: str | None = None) -> dict[str, object]:
    """Return sink source/parity paths and transport nodes for a flat gate netlist."""
    top_start = 0
    if top is not None:
        top_start, top_end, text = extract_top_module(text, top)
        _ = top_end
    instances = parse_instances(text)
    instance_by_name = {instance.name.lstrip("\\"): instance for instance in instances}
    inputs, outputs, inouts = _top_ports(text)
    drivers: dict[str, list[tuple[Instance, str]]] = {}
    consumers: dict[str, list[tuple[Instance, str]]] = {}
    for instance in instances:
        for connection in instance.connections:
            try:
                net = _simple_net(connection.expression, f"{instance.name}/{connection.pin}")
            except ClockTopologyError:
                continue
            consumers.setdefault(net, []).append((instance, connection.pin))
            output_pin = _driver_pin(instance, net)
            if output_pin == connection.pin:
                drivers.setdefault(net, []).append((instance, output_pin))

    sink_pins: list[tuple[Instance, str, str, str]] = []
    for instance in instances:
        master = instance.master
        pins = instance.pins()
        clock_pins: set[str] = set()
        if _is_sequential(master):
            if "CLK" in pins:
                clock_pins.add("CLK")
            if "CLK_N" in pins and "CLK" not in pins:
                clock_pins.add("CLK_N")
        elif _is_icg(master):
            if "CLK" in pins:
                clock_pins.add("CLK")
        elif _is_macro(master):
            clock_pins.update(pin for pin in pins if _is_macro_clock_pin(pin))
        elif not master.startswith("sky130_fd_sc_hd__"):
            possible = [pin for pin in pins if _is_macro_clock_pin(pin)]
            if possible:
                raise ClockTopologyError(
                    f"unsupported clock-bearing instance {instance.name} ({master}); refusing to infer its semantics"
                )
        for pin in clock_pins:
            connection = pins.get(pin)
            if connection is None:
                continue
            net = _simple_net(connection.expression, f"{instance.name}/{pin}")
            kind = "icg" if _is_icg(master) else "macro" if _is_macro(master) else "sequential"
            sink_pins.append((instance, pin, net, kind))
    if not sink_pins:
        raise ClockTopologyError("no sequential, ICG, or recognized macro clock pins were found")

    paths: list[SinkPath] = []
    all_transport: set[str] = set()
    for sink, pin, initial_net, sink_kind in sink_pins:
        parity = 0
        transport: list[str] = []
        icgs: list[str] = []
        seen_nets: set[str] = set()
        net = initial_net
        rewire_net = initial_net
        while True:
            if net in seen_nets:
                raise ClockTopologyError(f"cycle in clock graph at net {net}")
            seen_nets.add(net)
            sources = drivers.get(net, [])
            if len(sources) > 1:
                raise ClockTopologyError(f"ambiguous multiple drivers on clock net {net}")
            if not sources:
                if net not in inputs:
                    raise ClockTopologyError(
                        f"clock net {net} has no recognized driver or primary input root"
                    )
                root = f"port:{net}"
                root_net = net
                break
            source, output_pin = sources[0]
            kind = transport_kind(source.master)
            if _is_icg(source.master):
                if output_pin != "GCLK":
                    raise ClockTopologyError(
                        f"unexpected clock-gate output {output_pin} on {source.name}"
                    )
                gate_name = source.name.lstrip("\\")
                if not icgs:
                    rewire_net = net
                icgs.append(gate_name)
                # Preserve the gated waveform in the proof copy. Strip only
                # the CTS transport cells downstream of GCLK, then reconnect
                # the sequential endpoint to this boundary net.
                clock_input = source.pins().get("CLK")
                if clock_input is None:
                    raise ClockTopologyError(f"clock gate {gate_name} lacks CLK input")
                net = _simple_net(clock_input.expression, f"{gate_name}/CLK")
                continue
            if not kind:
                root = f"driver:{source.name.lstrip('\\')}/{output_pin}"
                root_net = net
                break
            _, invert, input_pin, expected_output = kind
            if output_pin != expected_output:
                raise ClockTopologyError(f"unexpected output pin on clock transport {source.name}")
            input_connection = source.pins().get(input_pin)
            if input_connection is None:
                raise ClockTopologyError(f"clock transport {source.name} lacks {input_pin}")
            transport.append(source.name.lstrip("\\"))
            parity ^= invert
            net = _simple_net(input_connection.expression, f"{source.name}/{input_pin}")
        if not icgs:
            rewire_net = root_net
        paths.append(SinkPath(sink.name.lstrip("\\"), pin, initial_net, sink_kind,
                              root, root_net, rewire_net, parity,
                              tuple(transport), tuple(icgs)))
        all_transport.update(transport)

    # A cell recognized on a clock path is removable from the LEC copy only
    # when every one of its output consumers is also in that clock cone.
    for name in sorted(all_transport):
        instance = instance_by_name[name]
        kind = transport_kind(instance.master)
        assert kind is not None
        output_pin = kind[3]
        output_connection = instance.pins().get(output_pin)
        if not output_connection:
            raise ClockTopologyError(f"clock transport {name} lacks {output_pin}")
        net = _simple_net(output_connection.expression, f"{name}/{output_pin}")
        if net in outputs or net in inouts:
            raise ClockTopologyError(f"clock transport {name} drives a top-level output/inout; refusing LEC abstraction")
        for consumer, pin in consumers.get(net, []):
            if consumer.name.lstrip("\\") == name and pin == output_pin:
                continue
            if consumer.name.lstrip("\\") in all_transport:
                continue
            if _is_sky130_antenna_diode(consumer, pin):
                continue
            if (consumer.name.lstrip("\\").lower().startswith("clkload")
                    or "clkload" in consumer.master.lower()) and pin == "A":
                continue
            if any(path.sink == consumer.name.lstrip("\\") and path.pin == pin for path in paths):
                continue
            raise ClockTopologyError(
                f"clock transport {name} output {net} also feeds non-clock pin {consumer.name}/{pin}"
            )

    serialized = [
        {"sink": path.sink, "pin": path.pin, "net": path.net, "kind": path.kind,
         "root": path.root, "root_net": path.root_net, "parity": path.parity,
         "rewire_net": path.rewire_net, "transport": list(path.transport),
         "icgs": list(path.icgs)}
        for path in sorted(paths, key=lambda item: (item.sink, item.pin))
    ]
    return {"sinks": serialized, "transport_instances": sorted(all_transport),
            "sink_count": len(serialized), "transport_count": len(all_transport),
            "top_offset": top_start}


def assert_topology_equivalent(baseline: dict[str, object], candidate: dict[str, object]) -> None:
    """Require exact root, endpoint, and phase agreement for every clock boundary."""
    base_sinks = baseline["sinks"]
    candidate_sinks = candidate["sinks"]
    assert isinstance(base_sinks, list) and isinstance(candidate_sinks, list)
    key = lambda row: (row["sink"], row["pin"])
    # ICG clock-input endpoints are additions in the candidate by design. The
    # sequential/macro endpoints still need identical clock source and phase;
    # each ICG input must originate from one of those same validated domains.
    base_functional = [row for row in base_sinks if row.get("kind") != "icg"]
    cand_functional = [row for row in candidate_sinks if row.get("kind") != "icg"]
    base = {key(row): row for row in base_functional}
    cand = {key(row): row for row in cand_functional}
    if base.keys() != cand.keys():
        missing = sorted(base.keys() - cand.keys())
        added = sorted(cand.keys() - base.keys())
        raise ClockTopologyError(f"clock endpoint set changed; missing={missing[:8]} added={added[:8]}")
    mismatches = []
    for endpoint in sorted(base):
        before, after = base[endpoint], cand[endpoint]
        if before["root"] != after["root"] or before["parity"] != after["parity"]:
            mismatches.append((endpoint, before["root"], before["parity"],
                               after["root"], after["parity"]))
    if mismatches:
        raise ClockTopologyError(f"clock phase/source changed at endpoints: {mismatches[:8]}")
    allowed_domains = {(row["root"], row["parity"]) for row in base.values()}
    bad_gates = [(row["sink"], row["root"], row["parity"])
                 for row in candidate_sinks if row.get("kind") == "icg"
                 and (row["root"], row["parity"]) not in allowed_domains]
    if bad_gates:
        raise ClockTopologyError(
            f"ICG clock inputs do not belong to a validated baseline clock domain: {bad_gates[:8]}"
        )


def find_legal_clock_stars(text: str, top: str) -> list[dict[str, object]]:
    """Enumerate locally safe parent/child stars in a flat clock netlist.

    This is the executable reference rule used by the synthetic torture test:
    a parent's output must feed only child pure buffers (plus isolated clkload
    inputs), with no direct clock sinks, macros, ICG pins, or top-level taps.
    """
    _start, _end, module_text = extract_top_module(text, top)
    instances = parse_instances(module_text)
    clock_transport = set(analyze_clock_topology(module_text, top)["transport_instances"])
    drivers: dict[str, list[tuple[Instance, str]]] = {}
    consumers: dict[str, list[tuple[Instance, str]]] = {}
    for instance in instances:
        for connection in instance.connections:
            try:
                net = _simple_net(connection.expression, f"{instance.name}/{connection.pin}")
            except ClockTopologyError:
                continue
            consumers.setdefault(net, []).append((instance, connection.pin))
            if _driver_pin(instance, net) == connection.pin:
                drivers.setdefault(net, []).append((instance, connection.pin))
    _inputs, outputs, inouts = _top_ports(module_text)
    stars: list[dict[str, object]] = []
    for parent in instances:
        kind = transport_kind(parent.master)
        parent_name = parent.name.lstrip("\\")
        if (not kind or kind[0] != "PURE_CLK_BUF"
                or parent_name not in clock_transport):
            continue
        output_pin = kind[3]
        output = parent.pins().get(output_pin)
        if output is None:
            continue
        net = _simple_net(output.expression, f"{parent.name}/{output_pin}")
        if net in outputs or net in inouts or len(drivers.get(net, [])) > 1:
            continue
        children: list[str] = []
        legal = True
        for consumer, pin in consumers.get(net, []):
            name = consumer.name.lstrip("\\")
            if name == parent.name.lstrip("\\") and pin == output_pin:
                continue
            child_kind = transport_kind(consumer.master)
            if (child_kind and child_kind[0] == "PURE_CLK_BUF"
                    and name in clock_transport and pin == child_kind[2]):
                children.append(name)
                continue
            if (name.lower().startswith("clkload") or "clkload" in consumer.master.lower()) and pin == "A":
                continue
            legal = False
            break
        if legal and children:
            vertices = [parent_name, *sorted(set(children))]
            if len(vertices) == len(set(vertices)):
                stars.append({"parent": parent.name.lstrip("\\"),
                              "children": sorted(set(children)),
                              "vertices": vertices})
    return sorted(stars, key=lambda row: str(row["parent"]))


def rewrite_clock_cones(text: str, topology: dict[str, object],
                        top: str | None = None) -> tuple[str, dict[str, int]]:
    """Remove verified pure transport nodes and reconnect only clock boundary pins."""
    module_start = 0
    if top is not None:
        module_start, module_end, module_text = extract_top_module(text, top)
    else:
        module_text = text
        module_end = len(text)
    instances = parse_instances(module_text)
    sink_rows = topology["sinks"]
    assert isinstance(sink_rows, list)
    sink_roots = {(row["sink"], row["pin"]): row.get("rewire_net", row["root_net"])
                  for row in sink_rows}
    remove = set(topology["transport_instances"])
    replacements: list[tuple[int, int, str]] = []
    removed = 0
    for instance in instances:
        name = instance.name.lstrip("\\")
        if name in remove:
            replacements.append((instance.start, instance.end, ""))
            removed += 1
            continue
        for connection in instance.connections:
            root_net = sink_roots.get((name, connection.pin))
            if root_net is not None and root_net != _simple_net(connection.expression, f"{name}/{connection.pin}"):
                replacements.append((connection.start, connection.end, root_net))
    output = module_text
    for start, end, value in sorted(replacements, reverse=True):
        output = output[:start] + value + output[end:]
    output = text[:module_start] + output + text[module_end:]
    return output, {"transport_removed": removed,
                    "clock_pins_reconnected": sum(1 for row in sink_rows
                    if row["net"] != row.get("rewire_net", row["root_net"]))}

