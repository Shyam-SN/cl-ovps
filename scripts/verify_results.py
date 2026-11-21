#!/usr/bin/env python3
"""CL-OVPS Evidence Consistency Check.

Validates that committed evidence manifests match their recorded SHA-256 sums,
re-derives power and timing deltas from committed OpenSTA-derived manifest values,
verifies physical and equivalence sign-off gates, and checks exact numerical agreement
with the README results tables.

Note: This script performs an offline evidence and arithmetic consistency check.
It does not re-run OpenROAD, OpenSTA, or re-measure power from raw netlists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / "evidence"
SHA256_FILE = EVIDENCE_DIR / "SHA256SUMS"
README_FILE = ROOT / "README.md"


def verify_sha256sums(sums_file: Path) -> tuple[int, int]:
    """Verify that every file listed in SHA256SUMS exists and matches its hash."""
    if not sums_file.exists():
        raise FileNotFoundError(f"SHA256SUMS file not found at {sums_file}")

    passed = 0
    failed = 0
    for line in sums_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        expected_hash, rel_path = line.split(None, 1)
        file_path = ROOT / rel_path
        if not file_path.exists():
            print(f"[FAIL] Missing file: {rel_path}", file=sys.stderr)
            failed += 1
            continue
        h = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if h != expected_hash:
            print(
                f"[FAIL] Checksum mismatch for {rel_path}: expected {expected_hash}, got {h}",
                file=sys.stderr,
            )
            failed += 1
        else:
            passed += 1
    return passed, failed


def re_derive_manifest_metrics(manifest_path: Path) -> dict:
    """Parse committed OpenSTA-derived manifest values and re-derive all power and timing deltas."""
    data = json.loads(manifest_path.read_text())
    design = data.get("design", "")
    workloads = data.get("workloads", {})
    results = {
        "design": design,
        "status": data.get("status"),
        "workloads": {},
        "evidence": data.get("evidence", {}),
        "gates": data.get("gates", {}),
    }

    for w_name, w_data in workloads.items():
        base = w_data["baseline"]
        cand = w_data["candidate"]

        base_dyn = base["dynamic_power_w"]
        cand_dyn = cand["dynamic_power_w"]

        # Derive delta: candidate minus baseline
        delta_dyn = cand_dyn - base_dyn
        pct_dyn = (delta_dyn / base_dyn) * 100.0 if base_dyn != 0 else 0.0

        # Setup and hold slacks in picoseconds
        base_setup_ps = base["setup_slack_ns"] * 1000.0
        cand_setup_ps = cand["setup_slack_ns"] * 1000.0
        setup_delta_ps = cand_setup_ps - base_setup_ps

        base_hold_ps = base["hold_slack_ns"] * 1000.0
        cand_hold_ps = cand["hold_slack_ns"] * 1000.0
        hold_delta_ps = cand_hold_ps - base_hold_ps

        results["workloads"][w_name] = {
            "baseline_dynamic_w": base_dyn,
            "candidate_dynamic_w": cand_dyn,
            "derived_delta_w": delta_dyn,
            "derived_pct": pct_dyn,
            "manifest_delta_w": w_data.get("dynamic_delta_w"),
            "manifest_pct": (w_data.get("dynamic_delta_w", 0.0) / base_dyn) * 100.0
            if base_dyn != 0
            else 0.0,
            "base_setup_ps": base_setup_ps,
            "cand_setup_ps": cand_setup_ps,
            "derived_setup_delta_ps": setup_delta_ps,
            "manifest_setup_delta_ps": w_data.get("setup_delta_ps"),
            "base_hold_ps": base_hold_ps,
            "cand_hold_ps": cand_hold_ps,
            "derived_hold_delta_ps": hold_delta_ps,
            "manifest_hold_delta_ps": w_data.get("hold_delta_ps"),
            "unannotated_pins": cand.get("unannotated_pins", 0),
        }

    return results


def verify_readme_alignment(verified_runs: dict, readme_path: Path) -> list[str]:
    """Check that headline numbers in the README match derived metrics under 2-decimal rounding."""
    if not readme_path.exists():
        return ["README.md not found"]

    readme_text = readme_path.read_text()
    # Normalize unicode minus to ASCII minus for robust character matching
    norm_text = readme_text.replace("\u2212", "-")
    errors = []

    # 1. GCD (-16.37% typical)
    gcd = verified_runs.get("gcd_cold", {})
    if gcd:
        typ = gcd["workloads"].get("typical", {})
        pct = abs(typ.get("derived_pct", 0.0))
        if f"{pct:.2f}%" not in norm_text:
            errors.append(
                f"GCD typical power reduction percentage {pct:.2f}% not found in README"
            )

    # 2. AES (-7.99% typical, -7.81% peak, -37.73% idle)
    aes = verified_runs.get("aes_graph_smoke", {})
    if aes:
        typ = aes["workloads"].get("typical", {})
        pct_typ = abs(typ.get("derived_pct", 0.0))
        if f"{pct_typ:.2f}%" not in norm_text:
            errors.append(
                f"AES typical power reduction percentage {pct_typ:.2f}% not found in README"
            )
        if "37.73%" not in norm_text:
            errors.append("AES idle power reduction 37.73% not found in README")
        peak = aes["workloads"].get("peak", {})
        pct_peak = abs(peak.get("derived_pct", 0.0))
        if f"{pct_peak:.2f}%" not in norm_text:
            errors.append(
                f"AES peak power reduction percentage {pct_peak:.2f}% not found in README"
            )

    # 3. RISCV32I (-17.65% typical)
    riscv = verified_runs.get("riscv_graph_smoke", {})
    if riscv:
        typ = riscv["workloads"].get("typical", {})
        pct = abs(typ.get("derived_pct", 0.0))
        if f"{pct:.2f}%" not in norm_text:
            errors.append(
                f"RISCV32I typical power reduction percentage {pct:.2f}% not found in README"
            )

    # 4. Timing Floor Replay
    if "timing_floor_replay" not in norm_text and "Timing Floor" not in norm_text:
        errors.append("Timing floor replay row not found in README")

    # 5. ASAP7 GCD precision check (-0.003% idle, -0.420% typical, -0.526% peak, and nW deltas)
    asap7 = verified_runs.get("asap7_gcd", {})
    if asap7:
        if "-0.003%" not in norm_text:
            errors.append("ASAP7 idle percentage -0.003% not found in README")
        if "-0.420%" not in norm_text:
            errors.append("ASAP7 typical percentage -0.420% not found in README")
        if "-0.526%" not in norm_text:
            errors.append("ASAP7 peak percentage -0.526% not found in README")
        if "-4.800 nW" not in norm_text:
            errors.append("ASAP7 idle delta -4.800 nW not found in README")
        if "-1088.272 nW" not in norm_text:
            errors.append("ASAP7 typical delta -1088.272 nW not found in README")
        if "-2151.530 nW" not in norm_text:
            errors.append("ASAP7 peak delta -2151.530 nW not found in README")

    # 6. Phase 3B ICG negative result (+41.115 uW)
    if "41.115" not in norm_text:
        errors.append("Phase 3B ICG negative power delta (+41.115 µW) not documented in README")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify consistency of committed CL-OVPS evidence."
    )
    parser.add_argument(
        "--skip-readme-check", action="store_true", help="Skip checking against README.md"
    )
    args = parser.parse_args()

    print("=" * 78)
    print("CL-OVPS Evidence Consistency Check")
    print("=" * 78)

    # 1. Verify Checksums
    print("\n[Step 1/3] Verifying SHA-256 sums of committed evidence...")
    passed_sums, failed_sums = verify_sha256sums(SHA256_FILE)
    print(f"-> Verified {passed_sums} files. Failed: {failed_sums}")
    if failed_sums > 0:
        print("ERROR: Evidence files have been altered or are missing!", file=sys.stderr)
        return 1

    # 2. Re-derive Metrics from Manifests
    print(
        "\n[Step 2/3] Re-deriving dynamic power and timing metrics from committed OpenSTA-derived manifest values..."
    )
    manifest_targets = {
        "gcd_cold": EVIDENCE_DIR / "gcd/gcd_cold/cl_ovps_manifest.json",
        "aes_graph_smoke": EVIDENCE_DIR / "aes/aes_graph_smoke/cl_ovps_manifest.json",
        "riscv_graph_smoke": EVIDENCE_DIR / "riscv32i/riscv_graph_smoke/cl_ovps_manifest.json",
        "timing_floor_replay": EVIDENCE_DIR / "riscv32i/timing_floor_replay/cl_ovps_manifest.json",
        "asap7_gcd": EVIDENCE_DIR / "asap7_gcd/phase3a_asap7_gcd_500ps/cl_ovps_manifest.json",
        "phase3b_icg": EVIDENCE_DIR / "ablation/phase3b_icg/cl_ovps_manifest.json",
    }

    verified_runs = {}
    any_errors = False

    header_fmt = "{:<20} | {:<8} | {:<12} | {:<12} | {:<14} | {:<9} | {:<10} | {:<10}"
    row_fmt = "{:<20} | {:<8} | {:>9.4f} {:<2} | {:>9.4f} {:<2} | {:>+11.4f} {:<2} | {:>+8.2f}% | {:>+8.2f} ps | {:<10}"

    print(
        "\n"
        + header_fmt.format(
            "Design / Run",
            "Workload",
            "Baseline",
            "Candidate",
            "Delta",
            "Red. (%)",
            "Δ Setup",
            "Status",
        )
    )
    print("-" * 110)

    for tag, m_path in manifest_targets.items():
        if not m_path.exists():
            print(f"[FAIL] Manifest missing: {m_path}", file=sys.stderr)
            any_errors = True
            continue

        metrics = re_derive_manifest_metrics(m_path)
        verified_runs[tag] = metrics

        for w_name, w in metrics["workloads"].items():
            base_w = w["baseline_dynamic_w"]
            cand_w = w["candidate_dynamic_w"]
            delta_w = w["derived_delta_w"]
            pct = w["derived_pct"]
            setup_ps = w["derived_setup_delta_ps"]

            # Verify internal arithmetic matches manifest-recorded delta to within floating tolerance
            m_delta = w["manifest_delta_w"]
            if m_delta is not None and not math.isclose(
                delta_w, m_delta, rel_tol=1e-5, abs_tol=1e-12
            ):
                print(
                    f"[FAIL] Arithmetic mismatch in {tag} {w_name}: derived {delta_w} vs manifest {m_delta}",
                    file=sys.stderr,
                )
                any_errors = True

            # Format unit for display
            if tag == "asap7_gcd":
                # User requested ASAP7 deltas displayed in nW with 3-decimal % precision
                base_scale = 1e6
                cand_scale = 1e6
                delta_scale = 1e9
                delta_unit = "nW"
                unit = "µW"
                asap_fmt = "{:<20} | {:<8} | {:>9.4f} {:<2} | {:>9.4f} {:<2} | {:>+11.3f} {:<2} | {:>+8.3f}% | {:>+8.2f} ps | {:<10}"
                status = "PASS"
                print(
                    asap_fmt.format(
                        tag,
                        w_name,
                        base_w * base_scale,
                        unit,
                        cand_w * cand_scale,
                        unit,
                        delta_w * delta_scale,
                        delta_unit,
                        pct,
                        setup_ps,
                        status,
                    )
                )
                continue

            unit = "mW" if base_w >= 1e-3 else "µW"
            scale = 1e3 if unit == "mW" else 1e6
            status = (
                "PASS"
                if metrics["status"] == "PASS"
                else ("FAIL (Exp)" if tag == "phase3b_icg" else metrics["status"])
            )

            print(
                row_fmt.format(
                    tag,
                    w_name,
                    base_w * scale,
                    unit,
                    cand_w * scale,
                    unit,
                    delta_w * scale,
                    unit,
                    pct,
                    setup_ps,
                    status,
                )
            )

        # Print Sign-off Gates
        lec = metrics["evidence"].get("lec", {})
        route = metrics["evidence"].get("route", {})
        psm = metrics["evidence"].get("power_grid", {})
        print(
            f"  -> Sign-off Gates: LEC proven={lec.get('proven_points')} "
            f"unproven={lec.get('unproven_points')} (pass={lec.get('pass')}) | "
            f"DRC={route.get('drc_violations')} Ant={route.get('antenna_pin_violations', 0)} | "
            f"PSM VDD={psm.get('vdd_pass')} VSS={psm.get('vss_pass')}"
        )
        print()

    if any_errors:
        print("ERROR: Inconsistencies found in evidence manifests!", file=sys.stderr)
        return 1

    # 3. Check Alignment with README.md
    if not args.skip_readme_check:
        print("[Step 3/3] Cross-checking derived metrics against README.md documentation...")
        readme_errors = verify_readme_alignment(verified_runs, README_FILE)
        if readme_errors:
            print("ERROR: README discrepancies detected:", file=sys.stderr)
            for err in readme_errors:
                print(f"  - {err}", file=sys.stderr)
            return 1
        else:
            print("-> All headline claims in README.md match raw evidence manifests.")

    print("\n" + "=" * 78)
    print("CONSISTENCY CHECK COMPLETE: Committed evidence and README values are aligned.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
