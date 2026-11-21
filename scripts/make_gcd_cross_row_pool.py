#!/usr/bin/env python3
"""Generate a deterministic all-clock-sink GCD relocation pool with legal cross-row moves."""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import random
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEQUENTIAL_MARKERS = ("dfxtp", "edfxtp")
MOVABLE_STATUSES = {"PLACED", "FIXED", "COVER", "FIRM", "LOCKED"}


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise ValueError(f"refusing to write empty TSV {path}")
    with path.open("w", newline="") as stream:
        fields = list(dict.fromkeys(key for row in rows for key in row))
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_sequential(row: dict[str, str]) -> bool:
    return row.get("status") == "PLACED" and row.get("master", "").startswith("sky130_fd_sc_hd__") and any(
        marker in row.get("master", "") for marker in SEQUENTIAL_MARKERS
    )


def horizontal_flip(orient: str) -> int:
    if orient in {"R0", "MX"}:
        return 0
    if orient in {"MY", "R180"}:
        return 1
    raise ValueError(f"unsupported row orientation {orient}")


def vertical_flip(orient: str) -> int:
    if orient in {"R0", "MY"}:
        return 0
    if orient in {"MX", "R180"}:
        return 1
    raise ValueError(f"unsupported row orientation {orient}")


def orient_for_row(inst_orient: str, row_orient: str) -> str:
    """Preserve left/right pin mirroring while matching vertical VPWR/VGND rail parity."""
    hflip = horizontal_flip(inst_orient)
    vflip = vertical_flip(row_orient)
    return {(0, 0): "R0", (0, 1): "MX", (1, 0): "MY", (1, 1): "R180"}[(hflip, vflip)]


def parse_move_from_row(row: dict[str, str]) -> tuple[str, int, int] | None:
    instance = row.get("instance", "")
    if not instance:
        return None
    x = next((row.get(key, "") for key in ("target_x_dbu", "to_x_dbu", "target_x", "to_x") if row.get(key, "") != ""), "")
    y = next((row.get(key, "") for key in ("target_y_dbu", "to_y_dbu", "target_y", "to_y") if row.get(key, "") != ""), "")
    if not x:
        return None
    if not y:
        y = next((row.get(key, "") for key in ("from_y_dbu", "y_dbu", "from_y", "y") if row.get(key, "") != ""), "")
    if not y:
        return None
    try:
        return instance, int(float(x)), int(float(y))
    except ValueError:
        return None


def collect_excluded_moves(paths: list[Path]) -> tuple[set[tuple[str, int, int]], list[dict[str, object]]]:
    excluded: set[tuple[str, int, int]] = set()
    hashes = []
    for path in sorted({p.resolve() for p in paths if p.is_file()}):
        rows = read_tsv(path)
        count = 0
        for row in rows:
            move = parse_move_from_row(row)
            if move:
                excluded.add(move)
                count += 1
        hashes.append({"artifact": "excluded_pool", "path": str(path), "sha256": sha256(path), "parsed_moves": count})
    return excluded, hashes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--rows", type=Path, required=True)
    parser.add_argument("--cts-sinks", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20261030)
    parser.add_argument("--moves-per-sink", type=int, default=2)
    parser.add_argument("--candidate-id-offset", type=int, default=0)
    parser.add_argument("--only-instance", action="append", default=[])
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--allow-any-sink-count", action="store_true",
                        help="use the supplied CTS sink inventory size instead of asserting the 35-sink GCD")
    parser.add_argument("--min-move-dbu", type=int, default=4000)
    parser.add_argument("--max-move-dbu", type=int, default=20000)
    parser.add_argument("--preferred-min-move-dbu", type=int, default=5000)
    parser.add_argument("--preferred-max-move-dbu", type=int, default=11000)
    parser.add_argument("--exclude-root", type=Path, default=ROOT / "build/power")
    parser.add_argument("--exclude-pool", type=Path, action="append", default=[])
    args = parser.parse_args()
    if args.moves_per_sink < 1 or args.min_move_dbu <= 0 or args.max_move_dbu < args.min_move_dbu:
        raise SystemExit("moves-per-sink and move range must be positive")

    inventory = read_tsv(args.inventory)
    by_name = {row["instance"]: row for row in inventory}
    placed = [row for row in inventory if row.get("status") in MOVABLE_STATUSES]
    flops = [row for row in inventory if is_sequential(row)]
    sinks = read_tsv(args.cts_sinks)
    sink_rows = [row for row in sinks if row.get("branch", "").endswith("__leaf_clk")]
    if not sink_rows and args.allow_any_sink_count:
        # Other TritonCTS naming schemes do not encode leaf-ness in net names.
        # The generic mode treats every discovered sequential clock pin as a
        # legal target and lets the CTS oracle rank the resulting moves.
        sink_rows = sinks
    sink_by_instance = {row["instance"]: row for row in sink_rows}
    missing = sorted(set(sink_by_instance) - set(by_name))
    if missing:
        raise SystemExit(f"CTS sink(s) absent from placement inventory: {missing}")
    if args.allow_any_sink_count:
        flops = [by_name[name] for name in sink_by_instance if name in by_name
                 and by_name[name].get("status") == "PLACED"]
        if not flops or not sink_by_instance:
            raise SystemExit("CTS sink inventory contains no placed clock sinks")
    else:
        flops = [by_name[name] for name in sink_by_instance if name in by_name and is_sequential(by_name[name])]
        if len(flops) != 35 or len(sink_by_instance) != 35:
            raise SystemExit(f"expected 35 placed sequential clock sinks, found flops={len(flops)} cts_sinks={len(sink_by_instance)}")
    if args.only_instance:
        requested = set(args.only_instance)
        unknown = sorted(requested - set(sink_by_instance))
        if unknown:
            raise SystemExit(f"requested instance(s) are not CTS sinks: {unknown}")
        sink_by_instance = {name: row for name, row in sink_by_instance.items() if name in requested}

    row_data = read_tsv(args.rows)
    rows_by_y = {int(row["y_min_dbu"]): row for row in row_data}
    if len(rows_by_y) != len(row_data):
        raise SystemExit("row inventory has duplicate y origins")
    rows_by_site = defaultdict(list)
    for row in row_data:
        rows_by_site[row["site"]].append(row)

    # Discover every prior GCD experiment pool and its actual evaluator output.
    exclude_paths = set(args.exclude_pool)
    for path in args.exclude_root.rglob("candidate_pool.tsv"):
        exclude_paths.add(path)
    for path in args.exclude_root.rglob("relocation_audit.tsv"):
        exclude_paths.add(path)
    excluded, exclusion_hashes = collect_excluded_moves(list(exclude_paths))

    occupancy: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
    for cell in placed:
        y = int(cell["y_dbu"])
        occupancy[y].append((int(cell["x_dbu"]), int(cell["x_dbu"]) + int(cell["width_dbu"]), cell["instance"]))

    # Candidate type 0 is a row-preserving sweep; type 1 deliberately changes row,
    # alternating MX/R0 rail phase and updating the instance mirror orientation.
    base_rng = random.Random(args.seed)
    proposals: list[dict[str, object]] = []
    seen: set[tuple[str, int, int]] = set(excluded)
    missing_types: list[str] = []

    def legal_options(cell: dict[str, str], cross_row: bool) -> list[tuple[dict[str, str], int, int, str, int, int]]:
        instance = cell["instance"]
        x, y, width = int(cell["x_dbu"]), int(cell["y_dbu"]), int(cell["width_dbu"])
        source_orient = cell["orient"]
        source_row = rows_by_y.get(y)
        if source_row is None:
            return []
        if vertical_flip(source_orient) != vertical_flip(source_row["orient"]):
            raise ValueError(f"source orientation/rail mismatch for {instance}: {source_orient} on row {source_row['orient']}")
        options = []
        for target_row in rows_by_site[source_row["site"]]:
            target_y = int(target_row["y_min_dbu"])
            is_cross = target_y != y
            if is_cross != cross_row:
                continue
            target_orientation = orient_for_row(source_orient, target_row["orient"])
            x_min, x_max, pitch = int(target_row["x_min_dbu"]), int(target_row["x_max_dbu"]), int(target_row["x_step_dbu"])
            if pitch <= 0:
                continue
            first_x = x_min + ((x - x_min) % pitch)
            for target_x in range(first_x, x_max - width + 1, pitch):
                dx, dy = target_x - x, target_y - y
                manhattan = abs(dx) + abs(dy)
                if not (args.min_move_dbu <= manhattan <= args.max_move_dbu):
                    continue
                if target_x == x and target_y == y:
                    continue
                key = (instance, target_x, target_y)
                if key in seen:
                    continue
                if any(
                    other_name != instance and target_x < other_hi and target_x + width > other_lo
                    for other_lo, other_hi, other_name in occupancy[target_y]
                ):
                    continue
                options.append((target_row, target_x, target_y, target_orientation, dx, dy))
        return options

    for index, instance in enumerate(sorted(sink_by_instance), start=1):
        cell = by_name[instance]
        for move_index in range(args.moves_per_sink):
            cross_row = move_index % 2 == 1
            options = legal_options(cell, cross_row)
            if not options:
                # If a same-row vacancy is scarce, allow a second cross-row move;
                # still preserve at least one cross-row sample per sink when legal.
                options = legal_options(cell, not cross_row)
                move_type = "cross_row_fallback" if not cross_row else "same_row_fallback"
            else:
                move_type = "cross_row" if cross_row else "same_row"
            if not options:
                missing_types.append(instance)
                continue
            # Favor the previously observed 6–10 um boundary scale while keeping
            # a broad distribution over directions and nearby row separations.
            preferred = [
                option for option in options
                if args.preferred_min_move_dbu <= abs(option[4]) + abs(option[5]) <= args.preferred_max_move_dbu
            ]
            choices = preferred or options
            rng = random.Random(base_rng.randrange(1 << 32))
            target_row, target_x, target_y, target_orient, dx, dy = rng.choice(choices)
            key = (instance, target_x, target_y)
            seen.add(key)
            source_row = rows_by_y[int(cell["y_dbu"])]
            proposals.append({
                "candidate_id": f"xrow{args.candidate_id_offset + len(proposals) + 1:03d}",
                "instance": instance,
                "master": cell["master"],
                "from_x_dbu": int(cell["x_dbu"]),
                "from_y_dbu": int(cell["y_dbu"]),
                "from_orient": cell["orient"],
                "source_row": source_row["row"],
                "source_row_orient": source_row["orient"],
                "target_x_dbu": target_x,
                "target_y_dbu": target_y,
                "target_orient": target_orient,
                "target_row": target_row["row"],
                "target_row_orient": target_row["orient"],
                "direction": "right" if dx > 0 else "left" if dx < 0 else ("up" if dy > 0 else "down"),
                "move_type": move_type,
                "branch_before": sink_by_instance[instance]["branch"],
                "min_displacement_dbu": args.min_move_dbu,
                "manhattan_displacement_dbu": abs(dx) + abs(dy),
            })

    if missing_types and not args.allow_incomplete:
        raise SystemExit(f"could not find legal isolated moves for {len(missing_types)} sink/move samples: {missing_types}")
    counts = {instance: sum(row["instance"] == instance for row in proposals) for instance in sink_by_instance}
    absent = sorted(instance for instance, count in counts.items() if not count)
    if absent and not args.allow_incomplete:
        raise SystemExit(f"candidate pool failed to cover clock sinks: {absent}")
    if len({(r["instance"], r["target_x_dbu"], r["target_y_dbu"]) for r in proposals}) != len(proposals):
        raise SystemExit("generated duplicate target moves")

    write_tsv(args.output, proposals)
    manifest = args.output.with_name(f"{args.output.stem}.integrity.tsv")
    integrity = [
        {"artifact": "candidate_pool", "path": str(args.output.resolve()), "sha256": sha256(args.output), "rows": len(proposals)},
        {"artifact": "placement_inventory", "path": str(args.inventory.resolve()), "sha256": sha256(args.inventory), "rows": len(inventory)},
        {"artifact": "row_inventory", "path": str(args.rows.resolve()), "sha256": sha256(args.rows), "rows": len(row_data)},
        {"artifact": "baseline_cts_sinks", "path": str(args.cts_sinks.resolve()), "sha256": sha256(args.cts_sinks), "rows": len(sink_rows)},
        *exclusion_hashes,
    ]
    write_tsv(manifest, integrity)
    print(
        f"GCD_CROSS_ROW_POOL candidates={len(proposals)} sinks={len(sink_by_instance)} "
        f"site_rows={len(row_data)} excluded_moves={len(excluded)} "
        f"same_row={sum(row['move_type']=='same_row' for row in proposals)} "
        f"cross_row={sum(row['move_type']=='cross_row' for row in proposals)} seed={args.seed} "
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
