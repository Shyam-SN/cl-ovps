"""Unit tests for CL-OVPS evidence verification and sign-off logic.

Runs cleanly under both pytest (CI) and standard library unittest (offline / local).
"""

from __future__ import annotations

import math
import unittest

try:
    import pytest
except ImportError:
    pytest = None

from scripts.verify_results import (
    EVIDENCE_DIR,
    ROOT,
    SHA256_FILE,
    re_derive_manifest_metrics,
    verify_sha256sums,
)


class TestVerifyResults(unittest.TestCase):
    def test_sha256_checksums(self):
        """Verify that all evidence files match their SHA-256 sums."""
        passed, failed = verify_sha256sums(SHA256_FILE)
        self.assertEqual(failed, 0, f"Expected 0 failed checksums, got {failed}")
        self.assertGreater(passed, 30, f"Expected at least 30 verified files, got {passed}")

    def test_manifest_rederivation_gcd(self):
        """Verify GCD manifest re-derivation from committed OpenSTA-derived manifest values."""
        manifest_path = EVIDENCE_DIR / "gcd/gcd_cold/cl_ovps_manifest.json"
        self.assertTrue(manifest_path.exists())

        metrics = re_derive_manifest_metrics(manifest_path)
        self.assertEqual(metrics["status"], "PASS")

        typ = metrics["workloads"]["typical"]
        self.assertTrue(math.isclose(typ["baseline_dynamic_w"], 1.50769e-4, rel_tol=1e-3))
        self.assertTrue(math.isclose(typ["candidate_dynamic_w"], 1.26086e-4, rel_tol=1e-3))
        self.assertTrue(math.isclose(typ["derived_pct"], -16.37, abs_tol=0.1))
        self.assertGreater(typ["derived_setup_delta_ps"], 200.0)

    def test_manifest_rederivation_aes(self):
        """Verify AES manifest re-derivation from committed OpenSTA-derived manifest values."""
        manifest_path = EVIDENCE_DIR / "aes/aes_graph_smoke/cl_ovps_manifest.json"
        self.assertTrue(manifest_path.exists())

        metrics = re_derive_manifest_metrics(manifest_path)
        self.assertEqual(metrics["status"], "PASS")

        idle = metrics["workloads"]["idle"]
        typ = metrics["workloads"]["typical"]
        peak = metrics["workloads"]["peak"]

        self.assertTrue(math.isclose(idle["derived_pct"], -37.73, abs_tol=0.1))
        self.assertTrue(math.isclose(typ["derived_pct"], -7.99, abs_tol=0.1))
        # Peak power delta under 2-decimal rounding from unrounded watts is -7.81%
        self.assertTrue(math.isclose(peak["derived_pct"], -7.81, abs_tol=0.01))
        self.assertTrue(math.isclose(typ["derived_setup_delta_ps"], 43.84, abs_tol=0.5))

        route = metrics["evidence"]["route"]
        self.assertEqual(route["drc_violations"], 0)
        self.assertEqual(route["antenna_pin_violations"], 0)

    def test_manifest_rederivation_riscv32i(self):
        """Verify RISCV32I manifest re-derivation from committed OpenSTA-derived manifest values."""
        manifest_path = EVIDENCE_DIR / "riscv32i/riscv_graph_smoke/cl_ovps_manifest.json"
        self.assertTrue(manifest_path.exists())

        metrics = re_derive_manifest_metrics(manifest_path)
        self.assertEqual(metrics["status"], "PASS")

        typ = metrics["workloads"]["typical"]
        self.assertTrue(math.isclose(typ["derived_pct"], -17.65, abs_tol=0.1))
        self.assertLess(abs(typ["derived_setup_delta_ps"]), 50.0)
        self.assertGreater(typ["derived_hold_delta_ps"], 10.0)

    def test_manifest_rederivation_timing_floor_replay(self):
        """Verify RISCV32I timing-floor replay manifest re-derivation."""
        manifest_path = EVIDENCE_DIR / "riscv32i/timing_floor_replay/cl_ovps_manifest.json"
        self.assertTrue(manifest_path.exists())

        metrics = re_derive_manifest_metrics(manifest_path)
        self.assertEqual(metrics["status"], "PASS")

        typ = metrics["workloads"]["typical"]
        self.assertTrue(math.isclose(typ["derived_pct"], -17.65, abs_tol=0.1))
        self.assertLess(abs(typ["derived_setup_delta_ps"]), 50.0)

    def test_asap7_gcd_same_route(self):
        """Verify ASAP7 GCD multi-Vt in-place replacement metrics and nW precision."""
        manifest_path = EVIDENCE_DIR / "asap7_gcd/phase3a_asap7_gcd_500ps/cl_ovps_manifest.json"
        self.assertTrue(manifest_path.exists())

        metrics = re_derive_manifest_metrics(manifest_path)
        self.assertEqual(metrics["status"], "PASS")

        idle = metrics["workloads"]["idle"]
        typ = metrics["workloads"]["typical"]
        peak = metrics["workloads"]["peak"]

        # 3-decimal percentage precision
        self.assertTrue(math.isclose(idle["derived_pct"], -0.003, abs_tol=0.001))
        self.assertTrue(math.isclose(typ["derived_pct"], -0.420, abs_tol=0.005))
        self.assertTrue(math.isclose(peak["derived_pct"], -0.526, abs_tol=0.005))

        # Absolute deltas in nW
        self.assertTrue(math.isclose(idle["derived_delta_w"] * 1e9, -4.800, abs_tol=0.05))
        self.assertTrue(math.isclose(typ["derived_delta_w"] * 1e9, -1088.272, abs_tol=0.5))
        self.assertTrue(math.isclose(peak["derived_delta_w"] * 1e9, -2151.530, abs_tol=0.5))
        self.assertGreaterEqual(typ["derived_setup_delta_ps"], 0.0)

    def test_negative_result_phase3b_icg(self):
        """Verify that Phase 3B ICG negative result correctly fails acceptance criteria."""
        manifest_path = EVIDENCE_DIR / "ablation/phase3b_icg/cl_ovps_manifest.json"
        self.assertTrue(manifest_path.exists())

        metrics = re_derive_manifest_metrics(manifest_path)
        self.assertEqual(metrics["status"], "FAIL")

        idle = metrics["workloads"]["idle"]
        self.assertGreater(idle["derived_delta_w"], 0)
        self.assertTrue(math.isclose(idle["derived_delta_w"] * 1e6, 41.115, abs_tol=0.1))
        self.assertLess(idle["derived_setup_delta_ps"], -300.0)

    def test_provenance_tsv_exists_and_valid(self):
        """Verify that PROVENANCE.tsv maps all evidence files to valid records."""
        prov_path = EVIDENCE_DIR / "PROVENANCE.tsv"
        self.assertTrue(prov_path.exists())

        lines = prov_path.read_text().splitlines()
        self.assertGreater(len(lines), 30)
        header = lines[0].split("\t")
        self.assertEqual(
            header,
            ["original_path", "original_sha256", "evidence_path", "evidence_sha256", "notes"],
        )

        for row in lines[1:]:
            parts = row.split("\t")
            self.assertEqual(len(parts), 5)
            ev_file = ROOT / parts[2]
            self.assertTrue(ev_file.exists(), f"Evidence file missing: {parts[2]}")


if __name__ == "__main__":
    unittest.main()
