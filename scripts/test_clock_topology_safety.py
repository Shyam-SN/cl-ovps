#!/usr/bin/env python3
"""Synthetic mixed-topology torture test for CL-OVPS clock safety.

The fixture includes a tapped hybrid branch, a legal lower parent/child star,
an ICG, a negedge flop, and a second mux-selected clock source. It checks the
same graph rules used by the Stage 2 Tcl pass and runs the graph-aware Yosys
LEC adapter on the baseline and parity-preserving candidate netlists.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from .cl_ovps_clock_topology import (
        ClockTopologyError, analyze_clock_topology, assert_topology_equivalent,
        find_legal_clock_stars)
except ImportError:
    from cl_ovps_clock_topology import (
        ClockTopologyError, analyze_clock_topology, assert_topology_equivalent,
        find_legal_clock_stars)


ROOT = Path(__file__).resolve().parents[1]
LEC = ROOT / "scripts/run_cl_ovps_gate_lec.py"


BASELINE = r"""module torture(clk, clk2, enable, select, d, direct_q, gated_q, neg_q, mux_q);
input clk, clk2, enable, select, d;
output direct_q, gated_q, neg_q, mux_q;
wire tapped, child_net, leaf_clk, gated_clk, mux_clk;
sky130_fd_sc_hd__clkbuf_8 root_buf (.A(clk), .X(tapped));
sky130_fd_sc_hd__dfxtp_1 direct_ff (.CLK(tapped), .D(d), .Q(direct_q));
sky130_fd_sc_hd__clkbuf_4 child_buf (.A(tapped), .X(child_net));
sky130_fd_sc_hd__clkbuf_2 leaf_buf (.A(child_net), .X(leaf_clk));
sky130_fd_sc_hd__dfxtp_1 leaf_pos_ff (.CLK(leaf_clk), .D(d), .Q());
sky130_fd_sc_hd__dfxtn_1 leaf_neg_ff (.CLK_N(leaf_clk), .D(d), .Q(neg_q));
sky130_fd_sc_hd__dlclkp_1 clock_gate (.CLK(leaf_clk), .GATE(enable), .GCLK(gated_clk));
sky130_fd_sc_hd__dfxtp_1 gated_ff (.CLK(gated_clk), .D(d), .Q(gated_q));
sky130_fd_sc_hd__mux2_1 clock_mux (.A0(clk), .A1(clk2), .S(select), .X(mux_clk));
sky130_fd_sc_hd__dfxtp_1 mux_ff (.CLK(mux_clk), .D(d), .Q(mux_q));
endmodule
"""


def candidate_netlist() -> str:
    text = BASELINE
    text = text.replace(
        "sky130_fd_sc_hd__clkbuf_4 child_buf (.A(tapped), .X(child_net));",
        "sky130_fd_sc_hd__clkinv_4 child_buf (.A(tapped), .Y(child_net));")
    text = text.replace(
        "sky130_fd_sc_hd__clkbuf_2 leaf_buf (.A(child_net), .X(leaf_clk));",
        "sky130_fd_sc_hd__clkinv_2 leaf_buf (.A(child_net), .Y(leaf_clk));")
    return text


class ClockTopologySafetyTest(unittest.TestCase):
    def test_tapped_parent_is_rejected_but_child_star_is_legal(self) -> None:
        stars = find_legal_clock_stars(BASELINE, "torture")
        star_parents = {row["parent"]: row["children"] for row in stars}
        self.assertNotIn("root_buf", star_parents,
                         "root output has a direct flop tap and must not be inverted")
        self.assertEqual(star_parents.get("child_buf"), ["leaf_buf"],
                         "the untapped child->leaf subtree remains eligible")

    def test_buffer_shaped_data_path_is_not_a_clock_star(self) -> None:
        data_path = r"""module data_path(clk, d, q, data_out);
input clk, d;
output q, data_out;
wire child_clk, data_mid;
sky130_fd_sc_hd__clkbuf_4 child_buf (.A(clk), .X(child_clk));
sky130_fd_sc_hd__dfxtp_1 ff (.CLK(child_clk), .D(d), .Q(q));
sky130_fd_sc_hd__clkbuf_4 data_parent (.A(d), .X(data_mid));
sky130_fd_sc_hd__clkbuf_2 data_child (.A(data_mid), .X(data_out));
endmodule
"""
        stars = find_legal_clock_stars(data_path, "data_path")
        self.assertEqual(stars, [],
                         "buffer-shaped signal logic must stay outside the clock graph")

    def test_hard_macro_clock_is_a_stop_boundary(self) -> None:
        macro_tree = r"""module macro_tree(clk, d, q);
input clk, d; output q;
wire clknet;
sky130_fd_sc_hd__clkbuf_4 clkbuf (.A(clk), .X(clknet));
sky130_fd_sc_hd__dfxtp_1 ff (.CLK(clknet), .D(d), .Q(q));
sky130_fd_sc_hd__sram_macro_1 memory (.CLK(clknet));
endmodule
"""
        topology = analyze_clock_topology(macro_tree, "macro_tree")
        macro_sinks = [row for row in topology["sinks"] if row["sink"] == "memory"]
        self.assertEqual(len(macro_sinks), 1)
        self.assertEqual(macro_sinks[0]["root"], "port:clk")
        self.assertEqual(macro_sinks[0]["parity"], 0)
        self.assertEqual(find_legal_clock_stars(macro_tree, "macro_tree"), [],
                         "the macro clock tap prevents a star across its boundary")

    def test_antenna_diode_is_passive_clock_load_not_functional_fanout(self) -> None:
        antenna_clock = r"""module antenna_clock(clk, d, q);
input clk, d; output q;
wire clknet;
sky130_fd_sc_hd__clkbuf_4 clkbuf (.A(clk), .X(clknet));
sky130_fd_sc_hd__dfxtp_1 ff (.CLK(clknet), .D(d), .Q(q));
sky130_fd_sc_hd__diode_2 ANTENNA_1 (.DIODE(clknet));
endmodule
"""
        topology = analyze_clock_topology(antenna_clock, "antenna_clock")
        self.assertEqual(topology["sink_count"], 1)
        self.assertEqual(topology["transport_instances"], ["clkbuf"])

    def test_every_clock_boundary_keeps_root_and_phase(self) -> None:
        before = analyze_clock_topology(BASELINE, "torture")
        after = analyze_clock_topology(candidate_netlist(), "torture")
        assert_topology_equivalent(before, after)
        self.assertEqual(before["sink_count"], 6)
        sinks = {(row["sink"], row["pin"]) for row in before["sinks"]}
        self.assertIn(("leaf_neg_ff", "CLK_N"), sinks)
        self.assertIn(("clock_gate", "CLK"), sinks)
        roots = {row["root"] for row in before["sinks"]}
        self.assertTrue(any(root.startswith("driver:clock_mux/") for root in roots))

    def test_floating_clock_root_fails_closed(self) -> None:
        floating = """module floating(clk, d, q);
input d; output q; wire clk;
sky130_fd_sc_hd__dfxtp_1 ff (.CLK(clk), .D(d), .Q(q));
endmodule
"""
        with self.assertRaisesRegex(ClockTopologyError, "no recognized driver or primary input root"):
            analyze_clock_topology(floating, "floating")

    def test_yosys_lec_expands_icg_and_preserves_negedge_behavior(self) -> None:
        with tempfile.TemporaryDirectory(prefix="clovps_clock_torture_") as temp:
            temp_path = Path(temp)
            baseline = temp_path / "baseline.v"
            candidate = temp_path / "candidate.v"
            baseline.write_text(BASELINE)
            candidate.write_text(candidate_netlist())
            output = temp_path / "lec"
            run = subprocess.run(
                [sys.executable, str(LEC), "--baseline-netlist", str(baseline),
                 "--candidate-netlist", str(candidate), "--top", "torture",
                 "--output-dir", str(output), "--seq-depth", "8"],
                cwd=ROOT, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, check=False)
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertIn("status=LEC_PASS", run.stdout)
            normalized = (output / "candidate_normalized.v").read_text()
            self.assertIn(".DE(enable)", normalized)
            self.assertNotIn("sky130_fd_sc_hd__dlclkp_1", normalized)
            self.assertIn("lec_dfxtn_1", normalized)
            self.assertNotRegex(normalized, r"\b(?:root_buf|child_buf|leaf_buf)\s*\(")

    def test_yosys_lec_proves_added_icg_against_mapped_enable_flop(self) -> None:
        baseline_text = r"""module icg_enable(clk, enable, d, q);
input clk, enable, d;
output q;
sky130_fd_sc_hd__edfxtp_1 ff (.CLK(clk), .D(d), .DE(enable), .Q(q));
endmodule
"""
        candidate_text = r"""module icg_enable(clk, enable, d, q);
input clk, enable, d;
output q;
wire gclk;
sky130_fd_sc_hd__dlclkp_1 gate0 (.CLK(clk), .GATE(enable), .GCLK(gclk));
sky130_fd_sc_hd__dfxtp_1 ff (.CLK(gclk), .D(d), .Q(q));
endmodule
"""
        with tempfile.TemporaryDirectory(prefix="clovps_icg_enable_lec_") as temp:
            temp_path = Path(temp)
            baseline = temp_path / "baseline.v"
            candidate = temp_path / "candidate.v"
            baseline.write_text(baseline_text)
            candidate.write_text(candidate_text)
            output = temp_path / "lec"
            run = subprocess.run(
                [sys.executable, str(LEC), "--baseline-netlist", str(baseline),
                 "--candidate-netlist", str(candidate), "--top", "icg_enable",
                 "--output-dir", str(output), "--seq-depth", "8"],
                cwd=ROOT, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, check=False)
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertIn("status=LEC_PASS", run.stdout)
            self.assertIn("icg_enable_expansions=0/1", run.stdout)

    def test_openroad_stage2_rejects_hybrid_root_and_commits_lower_star(self) -> None:
        openroad = ROOT / "tools/openroad/build/bin/openroad"
        source_odb = ROOT / (
            "build/orfs/results/sky130hd/gcdflowplan_cold_v2/"
            "clovps_flowplan_gcd_cold_20261103_v2_cts/4_1_cts.odb")
        source_sdc = source_odb.with_name("4_cts.sdc")
        liberty = ROOT / (
            "tools/openroad-flow-scripts/flow/platforms/sky130hd/lib/"
            "sky130_fd_sc_hd__tt_025C_1v80.lib")
        upf = ROOT / "designs/gcd/flow/constraints/gcd.upf"
        if not all(path.is_file() for path in (openroad, source_odb, source_sdc, liberty, upf)):
            self.skipTest("built OpenROAD and frozen GCD CTS artifacts are required")

        with tempfile.TemporaryDirectory(prefix="clovps_stage2_hybrid_") as temp:
            temp_path = Path(temp)
            torture_odb = temp_path / "torture.odb"
            env = os.environ.copy()
            env.update({
                "CL_OVPS_ODB": str(source_odb),
                "CL_OVPS_SDC": str(source_sdc),
                "CL_OVPS_LIBERTY": str(liberty),
                "CL_OVPS_TORTURE_OUT": str(torture_odb),
            })
            fixture = subprocess.run(
                [str(openroad), "-no_init", "-exit",
                 str(ROOT / "scripts/make_cl_ovps_hybrid_torture_odb.tcl")],
                cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, check=False)
            self.assertEqual(fixture.returncode, 0, fixture.stdout)
            self.assertIn("hybrid_root_tap=", fixture.stdout)
            self.assertTrue(torture_odb.is_file(), fixture.stdout)

            stage2_log = temp_path / "stage2.log"
            stage2 = subprocess.run(
                [sys.executable, str(ROOT / "scripts/run_cl_ovps_pass.py"),
                 "--stage", "clock", "--input-odb", str(torture_odb),
                 "--sdc", str(source_sdc), "--default-activity", "0.1",
                 "--lib", str(liberty), "--rc-tcl",
                 str(ROOT / "tools/openroad-flow-scripts/flow/platforms/sky130hd/setRC.tcl"),
                 "--upf", str(upf), "--output-odb", str(temp_path / "stage2.odb"),
                 "--report", str(temp_path / "stage2.tsv"), "--log", str(stage2_log),
                 "--action", "all"],
                cwd=ROOT, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, check=False)
            self.assertEqual(stage2.returncode, 0, stage2.stdout)
            log = stage2_log.read_text()
            self.assertIn(
                "AUDIT_CLOCK_STAR_ACCEPT parent=clkbuf_2_0__f_clk "
                "children=clkbuf_2_1__f_clk", log)
            self.assertNotRegex(log, r"AUDIT_CLOCK_STAR_ACCEPT parent=clkbuf_0_clk(?:\s|$)")
            self.assertIn("AUDIT_CLOCK_GRAPH_CHECK context=final-stage23 endpoints=35 status=PASS", log)
            self.assertIn("AUDIT_PREFLIGHT_GATE=PASS", log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
