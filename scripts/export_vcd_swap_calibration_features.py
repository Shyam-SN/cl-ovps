#!/usr/bin/env python3
"""Export placement/routed ODB geometry for the two VCD-guided pair swaps."""

from __future__ import annotations

import csv
import hashlib
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUDIT_TAG = os.environ.get("VCD_SWAP_AUDIT_TAG", "20261027_locsearch01")
AUDIT = ROOT / f"build/power/vcd_swap_audit_{AUDIT_TAG}"
FLOW_RESULTS = ROOT / "build/orfs/results/sky130hd/picorv32_baseline"
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_one(label: str, stage: str, odb: Path, output_dir: Path) -> dict[str, str]:
    before = sha256(odb)
    stem = f"{label}_{stage}"
    net_csv = output_dir / f"{stem}.nets.csv"
    layer_csv = output_dir / f"{stem}.layers.csv"
    log_path = output_dir / f"{stem}.log"
    env = os.environ.copy()
    env.update(
        FEATURE_ODB=str(odb),
        FEATURE_STAGE=stage,
        FEATURE_NET_CSV=str(net_csv),
        FEATURE_LAYER_CSV=str(layer_csv),
    )
    completed = subprocess.run(
        [str(OPENROAD), "-no_init", "-exit", str(ROOT / "scripts/export_net_physical_features.tcl")],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path.write_text(completed.stdout)
    if completed.returncode:
        raise RuntimeError(f"OpenROAD feature export failed for {odb}; see {log_path}\n{completed.stdout}")
    after = sha256(odb)
    if before != after:
        raise RuntimeError(f"source ODB changed during read-only feature export: {odb}")
    with net_csv.open(newline="") as source:
        net_rows = sum(1 for _ in csv.DictReader(source))
    with layer_csv.open(newline="") as source:
        layer_rows = sum(1 for _ in csv.DictReader(source))
    return {
        "candidate": label,
        "stage": stage,
        "odb": str(odb),
        "odb_sha256": after,
        "net_rows": str(net_rows),
        "layer_rows": str(layer_rows),
        "net_csv": str(net_csv),
        "layer_csv": str(layer_csv),
    }


def main() -> int:
    pair_dir = Path(os.environ.get("PAIR_CALIBRATION_DIR", AUDIT / "pair_calibration"))
    if not pair_dir.is_absolute():
        pair_dir = ROOT / pair_dir
    output_dir = pair_dir / "physical_features"
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline_place = FLOW_RESULTS / "baseline/3_5_place_dp.odb"
    baseline_route = FLOW_RESULTS / "baseline/6_final.odb"
    candidates = {}
    for label in ("swap44", "swap52"):
        report = ROOT / f"build/power/vcdswap_{AUDIT_TAG}_{label}"
        variant = (report / "flow_variant.txt").read_text().strip()
        place = AUDIT / f"candidate_odb/{label}.odb"
        route = FLOW_RESULTS / variant / "6_final.odb"
        candidates[label] = (place, route)

    exports = [
        export_one("baseline", "placement", baseline_place, output_dir),
        export_one("baseline", "routed", baseline_route, output_dir),
    ]
    for label, (place, route) in candidates.items():
        exports.append(export_one(label, "placement", place, output_dir))
        exports.append(export_one(label, "routed", route, output_dir))

    manifest = output_dir / "physical_export_manifest.tsv"
    with manifest.open("w", newline="") as output:
        fields = ["candidate", "stage", "odb", "odb_sha256", "net_rows", "layer_rows", "net_csv", "layer_csv"]
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(exports)
    print(f"Exported {len(exports)} ODB checkpoints; each source ODB hash matched before and after.")
    print(f"Manifest: {manifest}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, ValueError, RuntimeError) as exc:
        print(f"physical feature export failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
