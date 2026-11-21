#!/usr/bin/env python3
"""Export per-net placement topology and routed geometry from calibration ODBs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FLOW_RESULTS = ROOT / "build/orfs/results/sky130hd/picorv32_baseline"
BASELINE_PLACE = FLOW_RESULTS / "baseline/3_5_place_dp.odb"
BASELINE_ROUTE = FLOW_RESULTS / "baseline/6_final.odb"
PRIOR_CANDIDATE_PLACE = ROOT / "build/power/one_move_candidate/3_5_place_dp_one_move.odb"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slug(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")


def load_batch(batch_dir: Path, prior_calibration: Path) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    with (batch_dir / "batch_manifest.tsv").open(newline="") as source:
        for row in csv.DictReader(source, delimiter="\t"):
            row["candidate"] = f"{row['instance']}:{int(row['dx_dbu']):+d}"
            row["place_odb"] = str(Path(row["placement"]).resolve())
            row["route_variant"] = row["route_variant"]
            candidates.append(row)

    prior_manifest = prior_calibration / "manifest.tsv"
    values = dict(
        line.split("\t", 1)
        for line in prior_manifest.read_text().splitlines()
        if "\t" in line
    )
    candidates.insert(
        0,
        {
            "candidate": "place538:+460",
            "instance": "place538",
            "dx_dbu": "460",
            "place_odb": str(PRIOR_CANDIDATE_PLACE),
            "route_variant": values["routed_flow_variant"],
            "calibration_report": str(prior_calibration),
        },
    )
    return candidates


def read_tsv_manifest(path: Path) -> dict[str, str]:
    return dict(
        line.split("\t", 1)
        for line in path.read_text().splitlines()
        if "\t" in line
    )


def export_one(
    openroad: Path,
    odb: Path,
    stage: str,
    stem: str,
    out_dir: Path,
) -> tuple[Path, Path, str]:
    if not odb.is_file():
        raise FileNotFoundError(f"ODB is missing: {odb}")
    before_hash = sha256(odb)
    net_csv = out_dir / f"{stem}.nets.csv"
    layer_csv = out_dir / f"{stem}.layers.csv"
    log_path = out_dir / f"{stem}.log"
    env = os.environ.copy()
    env.update(
        {
            "FEATURE_ODB": str(odb),
            "FEATURE_STAGE": stage,
            "FEATURE_NET_CSV": str(net_csv),
            "FEATURE_LAYER_CSV": str(layer_csv),
        }
    )
    completed = subprocess.run(
        [str(openroad), "-no_init", "-exit", str(ROOT / "scripts/export_net_physical_features.tcl")],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path.write_text(completed.stdout)
    if completed.returncode != 0:
        raise RuntimeError(
            f"OpenROAD feature export failed for {odb}; see {log_path}:\n{completed.stdout}"
        )
    after_hash = sha256(odb)
    if before_hash != after_hash:
        raise RuntimeError(f"source ODB changed during read-only feature export: {odb}")
    if not net_csv.is_file() or not layer_csv.is_file():
        raise RuntimeError(f"OpenROAD did not produce both CSV files for {odb}")
    return net_csv, layer_csv, before_hash


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--prior-calibration", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--openroad", type=Path, default=ROOT / "tools/openroad/build/bin/openroad"
    )
    args = parser.parse_args()
    batch_dir = args.batch_dir.resolve()
    prior_calibration = args.prior_calibration.resolve()
    output_dir = args.output_dir.resolve()
    openroad = args.openroad.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        candidates = load_batch(batch_dir, prior_calibration)
        exports: list[dict[str, str]] = []
        source_hashes: dict[str, str] = {}

        def one_export(label: str, stage: str, odb: Path) -> None:
            stem = "_".join((slug(label), stage))
            net_csv, layer_csv, digest = export_one(
                openroad, odb, stage, stem, output_dir
            )
            source_hashes[str(odb)] = digest
            exports.append(
                {
                    "candidate": label,
                    "stage": stage,
                    "odb": str(odb),
                    "odb_sha256": digest,
                    "net_csv": str(net_csv),
                    "layer_csv": str(layer_csv),
                }
            )

        one_export("baseline", "placement", BASELINE_PLACE)
        one_export("baseline", "routed", BASELINE_ROUTE)
        for row in candidates:
            label = row["candidate"]
            place_odb = Path(row["place_odb"])
            final_odb = FLOW_RESULTS / row["route_variant"] / "6_final.odb"
            report = Path(row["calibration_report"])
            manifest = read_tsv_manifest(report / "manifest.tsv")
            expected_place_hash = manifest["candidate_place_odb_sha256"]
            if sha256(place_odb) != expected_place_hash:
                raise RuntimeError(f"candidate placement hash does not match manifest for {label}")
            one_export(label, "placement", place_odb)
            one_export(label, "routed", final_odb)

        for export in exports:
            with Path(export["net_csv"]).open(newline="") as source:
                export["net_rows"] = str(sum(1 for _ in csv.DictReader(source)))
            with Path(export["layer_csv"]).open(newline="") as source:
                export["layer_rows"] = str(sum(1 for _ in csv.DictReader(source)))

        with (output_dir / "physical_export_manifest.tsv").open("w", newline="") as output:
            fields = ["candidate", "stage", "odb", "odb_sha256", "net_rows", "layer_rows", "net_csv", "layer_csv"]
            writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t")
            writer.writeheader()
            writer.writerows(exports)
        print(f"Exported physical features from {len(exports)} ODB checkpoints.")
        print(f"Source ODB hashes checked before and after export: {len(source_hashes)} unique files.")
        print(f"Manifest: {output_dir / 'physical_export_manifest.tsv'}")
        return 0
    except (OSError, KeyError, ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"physical feature export failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
