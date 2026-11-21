#!/usr/bin/env python3
"""Characterize Sky130HD sequential cells and truth-table-index combinational cells.

The parser intentionally reads Liberty source directly and does not infer cell
semantics from master-name conventions.  The sequential TSV is an audit input
for experimental Stage 3C work; it does not itself authorize a master swap.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


TOKEN_RE = re.compile(
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|'
    r'[A-Za-z_$][A-Za-z0-9_$./-]*|'
    r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|'
    r'[{}():;,\\]|[^\s]'
)
NUMBER_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


@dataclass
class Node:
    name: str
    args: list[str] = field(default_factory=list)
    attrs: dict[str, list[str]] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)

    def child(self, name: str) -> list["Node"]:
        return [item for item in self.children if item.name == name]

    def attr(self, name: str, default: str = "") -> str:
        values = self.attrs.get(name, [])
        return values[-1] if values else default


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
        value = value.replace(r"\"", '"').replace(r"\'", "'")
    return value


class LibertyParser:
    def __init__(self, text: str):
        text = re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.S)
        self.tokens = TOKEN_RE.findall(text)
        self.index = 0

    def _parenthesized(self) -> list[str]:
        if self.tokens[self.index] != "(":
            raise ValueError("expected '(' while parsing Liberty group")
        self.index += 1
        depth = 1
        result: list[str] = []
        while self.index < len(self.tokens) and depth:
            token = self.tokens[self.index]
            self.index += 1
            if token == "(":
                depth += 1
            elif token == ")":
                depth -= 1
                if not depth:
                    break
            result.append(token)
        if depth:
            raise ValueError("unterminated parenthesized Liberty expression")
        return result

    def _scope(self, stop_at_brace: bool = False) -> list[Node]:
        nodes: list[Node] = []
        while self.index < len(self.tokens):
            token = self.tokens[self.index]
            if token == "}":
                if not stop_at_brace:
                    self.index += 1
                    continue
                self.index += 1
                return nodes
            if token in {";", ",", "\\"}:
                self.index += 1
                continue
            name = token
            self.index += 1
            if self.index >= len(self.tokens):
                break
            if self.tokens[self.index] == "(":
                args = self._parenthesized()
                if self.index < len(self.tokens) and self.tokens[self.index] == "{":
                    self.index += 1
                    nodes.append(Node(name, args, children=self._scope(True)))
                elif self.index < len(self.tokens) and self.tokens[self.index] == ";":
                    self.index += 1
                    nodes.append(Node(name, args, attrs={"__call__": [" ".join(args)]}))
                else:
                    # Liberty extensions sometimes use a group declaration
                    # without a body; skip to its semicolon or next statement.
                    while self.index < len(self.tokens) and self.tokens[self.index] not in {";", "}"}:
                        self.index += 1
                    if self.index < len(self.tokens) and self.tokens[self.index] == ";":
                        self.index += 1
            elif self.tokens[self.index] == ":":
                self.index += 1
                value: list[str] = []
                parens = brackets = 0
                while self.index < len(self.tokens):
                    current = self.tokens[self.index]
                    if current == ";" and parens == 0 and brackets == 0:
                        self.index += 1
                        break
                    if current == "(" : parens += 1
                    elif current == ")": parens = max(0, parens - 1)
                    elif current == "[": brackets += 1
                    elif current == "]": brackets = max(0, brackets - 1)
                    value.append(current)
                    self.index += 1
                # Attributes are recorded on a synthetic node so duplicate
                # attributes can be retained without losing source order.
                nodes.append(Node("__attribute__", [name], attrs={name: [" ".join(value)]}))
            elif self.tokens[self.index] == "{":
                self.index += 1
                nodes.append(Node(name, children=self._scope(True)))
            else:
                # Ignore non-standard bare declarations conservatively.
                while self.index < len(self.tokens) and self.tokens[self.index] not in {";", "}"}:
                    self.index += 1
                if self.index < len(self.tokens) and self.tokens[self.index] == ";":
                    self.index += 1
        if stop_at_brace:
            raise ValueError("unterminated Liberty group")
        return nodes

    def parse(self) -> list[Node]:
        return self._scope()


def consolidate_attributes(node: Node) -> None:
    kept: list[Node] = []
    for child in node.children:
        if child.name == "__attribute__":
            for key, values in child.attrs.items():
                node.attrs.setdefault(key, []).extend(values)
        elif child.name != "__attribute__":
            consolidate_attributes(child)
            kept.append(child)
    node.children = kept


def walk(nodes: Iterable[Node], name: str | None = None) -> Iterable[Node]:
    for node in nodes:
        if name is None or node.name == name:
            yield node
        yield from walk(node.children, name)


def scalar_number(value: str) -> float | None:
    match = NUMBER_RE.search(unquote(value))
    return float(match.group(0)) if match else None


def number_list(value: str) -> list[float]:
    return [float(match.group(0)) for match in NUMBER_RE.finditer(unquote(value))]


def call_values(node: Node, key: str) -> list[float]:
    value = node.attr(key)
    if not value and key in node.attrs.get("__call__", []):
        value = node.attrs["__call__"][-1]
    return number_list(value)


def master_strength(master: str) -> tuple[str, int | None]:
    match = re.match(r"^(.*)_([0-9]+)$", master)
    return (match.group(1), int(match.group(2))) if match else (master, None)


def pin_record(pin: Node) -> dict[str, object]:
    return {
        "name": unquote(pin.args[0]) if pin.args else "",
        "direction": unquote(pin.attr("direction")),
        "function": unquote(pin.attr("function")),
        "capacitance": scalar_number(pin.attr("capacitance")),
        "rise_capacitance": scalar_number(pin.attr("rise_capacitance")),
        "fall_capacitance": scalar_number(pin.attr("fall_capacitance")),
    }


def power_tables(pin: Node) -> list[dict[str, object]]:
    tables: list[dict[str, object]] = []
    for internal in pin.child("internal_power"):
        related_pin = unquote(internal.attr("related_pin"))
        for table in internal.children:
            if table.name not in {"rise_power", "fall_power"}:
                continue
            grid = table.child(table.args[0] if table.args else "")
            # Table templates are not nested below a power table in common
            # Liberty; index/value attributes sit directly inside the group.
            values = table.attr("values")
            if not values:
                values_node = table.child("values")
                values = values_node[-1].attr("__call__") if values_node else ""
            indices = {}
            for index_name in ("index_1", "index_2", "index_3"):
                raw = table.attr(index_name)
                if raw:
                    indices[index_name] = number_list(raw)
                else:
                    index_node = table.child(index_name)
                    if index_node:
                        indices[index_name] = number_list(index_node[-1].attrs.get("__call__", [""])[-1])
            tables.append({
                "related_pin": related_pin,
                "transition": table.name.removesuffix("_power"),
                "template": unquote(table.args[0]) if table.args else "",
                "indices": indices,
                "values": number_list(values),
                "value_count": len(number_list(values)),
            })
    return tables


def _seq_signature(cell: Node) -> tuple[dict[str, object], list[dict[str, object]]]:
    seq_groups = cell.child("ff") + cell.child("latch") + cell.child("statetable")
    semantics = []
    for seq in seq_groups:
        values = {key: unquote(seq.attr(key)) for key in (
            "clocked_on", "clocked_on_also", "next_state", "clear", "preset",
            "data_in", "enable", "clear_preset_var1", "clear_preset_var2")
                  if seq.attr(key)}
        semantics.append({"type": seq.name, "state_args": [unquote(a) for a in seq.args], **values})
    pins = [pin_record(pin) for pin in cell.child("pin")]
    pin_semantics = sorted((pin["name"], pin["direction"], pin["function"]) for pin in pins)
    pin_names = {str(pin["name"]) for pin in pins}
    clock_pins = set()
    for seq in seq_groups:
        expression_names = ["clocked_on", "clocked_on_also"]
        # Liberty latch groups use `enable` for the transparent gate input;
        # on flip-flops, `enable` is a data-control pin and is not a clock.
        if seq.name == "latch":
            expression_names.append("enable")
        for expression_name in expression_names:
            expression = unquote(seq.attr(expression_name))
            for token in re.findall(r"[A-Za-z_$][A-Za-z0-9_.$/]*", expression):
                if token in pin_names:
                    clock_pins.add(token)
    for pin in cell.child("pin"):
        if unquote(pin.attr("clock_gate_clock_pin")).lower() == "true" and pin.args:
            clock_pins.add(unquote(pin.args[0]))
    signature_obj = {"sequential": semantics, "pins": pin_semantics}
    signature = json.dumps(signature_obj, sort_keys=True, separators=(",", ":"))
    signature_hash = hashlib.sha256(signature.encode()).hexdigest()[:16]
    names = [str(pin["name"]) for pin in pins]
    metadata = {
        "equivalence_class": signature_hash,
        "pin_list": names,
        "sequential_semantics": semantics,
        "clock_pins": sorted(clock_pins),
        "scan_pins": [name for name in names if name.upper() in {"SCD", "SCE", "SI", "SE", "SCANIN", "SCANEN"}],
        "pin_semantics": pin_semantics,
    }
    return metadata, pins


def sequential_rows(cells: list[Node], lib_path: Path) -> list[dict[str, object]]:
    rows = []
    for cell in cells:
        if not (cell.child("ff") or cell.child("latch") or cell.child("statetable")):
            continue
        master = unquote(cell.args[0]) if cell.args else ""
        base, strength = master_strength(master)
        metadata, pins = _seq_signature(cell)
        pin_by_name = {unquote(pin.args[0]): pin for pin in cell.child("pin") if pin.args}
        clock_pins = metadata["clock_pins"]
        pin_cap = {}
        for pin_name in sorted(set(clock_pins) | {"CLK", "D", "RESET_B", "SET_B", "SCD", "SCE"}):
            if pin_name in pin_by_name:
                record = pin_record(pin_by_name[pin_name])
                pin_cap[pin_name] = {key: record[key] for key in
                                     ("capacitance", "rise_capacitance", "fall_capacitance")}
        clock_pin_capacitance = {
            pin_name: pin_cap[pin_name] for pin_name in clock_pins if pin_name in pin_cap
        }
        power = {pin_name: power_tables(pin_by_name[pin_name]) for pin_name in pin_by_name}
        rows.append({
            "library": str(lib_path), "master": master, "family": base,
            "drive_strength": strength, "cell_footprint": unquote(cell.attr("cell_footprint")),
            "area": scalar_number(cell.attr("area")), **metadata,
            "pin_capacitance": pin_cap, "clock_pin_capacitance": clock_pin_capacitance,
            "internal_power_tables": power,
        })
    return rows


class BooleanParser:
    TOKEN = re.compile(r"\s*([A-Za-z_$][A-Za-z0-9_.$/]*|[01]|[!~&*|+^()]|.)")

    def __init__(self, expression: str):
        self.tokens = [match.group(1) for match in self.TOKEN.finditer(expression)
                       if match.group(1).strip()]
        self.i = 0

    def parse(self, values: dict[str, int]) -> int:
        result = self._or(values)
        if self.i != len(self.tokens):
            raise ValueError(f"unparsed boolean tokens: {self.tokens[self.i:]}")
        return int(bool(result))

    def _take(self, values: set[str]) -> bool:
        if self.i < len(self.tokens) and self.tokens[self.i] in values:
            self.i += 1
            return True
        return False

    def _or(self, values: dict[str, int]) -> int:
        value = self._xor(values)
        while self._take({"|", "+"}):
            right = self._xor(values)
            value = int(bool(value) or bool(right))
        return value

    def _xor(self, values: dict[str, int]) -> int:
        value = self._and(values)
        while self._take({"^"}): value ^= self._and(values)
        return value

    def _and(self, values: dict[str, int]) -> int:
        value = self._unary(values)
        while self._take({"&", "*"}):
            right = self._unary(values)
            value = int(bool(value) and bool(right))
        return value

    def _unary(self, values: dict[str, int]) -> int:
        if self._take({"!", "~"}): return 1 - self._unary(values)
        if self._take({"("}):
            value = self._or(values)
            if not self._take({")"}): raise ValueError("missing ')' in boolean function")
            return value
        if self.i >= len(self.tokens): raise ValueError("missing boolean operand")
        token = self.tokens[self.i]
        self.i += 1
        if token == "0": return 0
        if token == "1": return 1
        if token not in values: raise ValueError(f"unknown Liberty function variable {token!r}")
        return values[token]


def truth_table(function: str, pins: list[str]) -> dict[str, object] | None:
    variables = sorted(set(re.findall(r"[A-Za-z_$][A-Za-z0-9_.$/]*", function)) & set(pins))
    if not variables or len(variables) > 4:
        return None
    try:
        bits = []
        for number in range(1 << len(variables)):
            assignment = {name: (number >> index) & 1 for index, name in enumerate(variables)}
            bits.append(str(BooleanParser(function).parse(assignment)))
    except ValueError:
        return None
    return {"variables": variables, "truth_table_lsb_first": "".join(bits), "input_count": len(variables)}


def combinational_index(cells: list[Node]) -> dict[str, object]:
    entries = []
    for cell in cells:
        if cell.child("ff") or cell.child("latch") or cell.child("statetable"):
            continue
        master = unquote(cell.args[0]) if cell.args else ""
        pins = [pin_record(pin) for pin in cell.child("pin")]
        inputs = [str(pin["name"]) for pin in pins if pin["direction"] == "input"]
        outputs = [pin for pin in pins if pin["direction"] == "output" and pin["function"]]
        if len(outputs) != 1:
            continue
        output = outputs[0]
        table = truth_table(str(output["function"]), inputs)
        if table is None:
            continue
        base, strength = master_strength(master)
        entries.append({"master": master, "family": base, "drive_strength": strength,
                        "output_pin": output["name"], "function": output["function"],
                        "truth_table": table, "input_pins": inputs,
                        "input_capacitances": {str(pin["name"]): pin["capacitance"] for pin in pins
                                               if pin["direction"] == "input"}})
    grouped: dict[str, list[dict[str, object]]] = {}
    for entry in entries:
        table = entry["truth_table"]
        key = f"{table['input_count']}:{table['truth_table_lsb_first']}"
        grouped.setdefault(key, []).append(entry)
    return {"schema_version": 1, "source": "Liberty function attributes; names are metadata only",
            "cell_count": len(entries), "functions": grouped, "entries": entries}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lib", required=True, type=Path, action="append", help="Liberty file (repeatable)")
    parser.add_argument("--sequential-tsv", required=True, type=Path)
    parser.add_argument("--combinational-json", required=True, type=Path)
    args = parser.parse_args()
    for path in args.lib:
        if not path.is_file(): raise SystemExit(f"missing Liberty file: {path}")
    for out in (args.sequential_tsv, args.combinational_json):
        if out.exists(): raise SystemExit(f"refusing to overwrite existing output: {out}")
        out.parent.mkdir(parents=True, exist_ok=True)

    seq_rows: list[dict[str, object]] = []
    combo_entries: list[dict[str, object]] = []
    input_hashes = {}
    for lib in args.lib:
        text = lib.read_text(errors="replace")
        input_hashes[str(lib.resolve())] = hashlib.sha256(text.encode()).hexdigest()
        nodes = LibertyParser(text).parse()
        for root in nodes: consolidate_attributes(root)
        cells = [node for node in walk(nodes, "cell")]
        seq_rows.extend(sequential_rows(cells, lib.resolve()))
        combo_entries.extend(combinational_index(cells)["entries"])

    columns = ("library", "master", "family", "drive_strength", "cell_footprint", "area",
               "equivalence_class", "pin_list", "sequential_semantics", "scan_pins",
               "pin_capacitance", "internal_power_tables")
    args.sequential_tsv.write_text("\t".join(columns) + "\n" + "".join(
        "\t".join(json.dumps(row.get(column), sort_keys=True, separators=(",", ":"))
                  if isinstance(row.get(column), (dict, list)) else
                  "" if row.get(column) is None else str(row.get(column)) for column in columns) + "\n"
        for row in sorted(seq_rows, key=lambda row: (str(row["family"]), row["drive_strength"] or 0, str(row["master"])))
    ))
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in combo_entries:
        tt = row["truth_table"]
        key = f"{tt['input_count']}:{tt['truth_table_lsb_first']}"
        grouped.setdefault(key, []).append(row)
    index = {"schema_version": 1, "liberty_sha256": input_hashes,
             "cell_count": len(combo_entries), "functions": grouped,
             "entries": combo_entries}
    args.combinational_json.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    print(f"LIBERTY_AUDIT sequential_masters={len(seq_rows)} combinational_truth_tables={len(combo_entries)} "
          f"tsv={args.sequential_tsv} functions={args.combinational_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
