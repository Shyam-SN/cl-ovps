# CL-OVPS Power & Timing Methodology

This document details the rigorous measurement principles, physical definitions, activity extraction policies, and timing constraint configurations applied across all CL-OVPS benchmarks.

---

## 1. Dynamic Power Formulation

All reported dynamic power quantities represent post-detailed-route metrics calculated using OpenSTA with extracted SPEF parasitics and gate-level VCD activity annotation.

$$P_{\text{dynamic}} = P_{\text{internal}} + P_{\text{switching}}$$

- **Internal Power ($P_{\text{internal}}$)**: Power dissipated within standard cells during input/output transitions, evaluated via multi-dimensional non-linear power models (NLPM) in the Liberty (`.lib`) files as a function of input transition time (slew) and output load capacitance ($C_{\text{load}}$). Flip-flop internal clock power ($P_{\text{clk}}$) is explicitly captured under clock pin toggling.
- **Switching Power ($P_{\text{switching}}$)**: Power dissipated in charging and discharging the physical parasitic capacitance of interconnect wires and load pin capacitances:
  $$P_{\text{switching}} = \frac{1}{2} \sum_{i \in \text{nets}} C_{i} \cdot V_{\text{DD}}^2 \cdot \alpha_{i} \cdot f_{\text{clk}}$$
  where $C_i$ is the extracted total capacitance from SPEF (ground plus coupling), $\alpha_i$ is the measured toggle activity per clock cycle from gate-level simulation VCDs, and $V_{\text{DD}} = 1.8\text{ V}$ for Sky130HD ($0.7\text{ V}$ for ASAP7).
- **Leakage Power ($P_{\text{leakage}}$)**: Static sub-threshold and gate leakage power is computed and reported separately in raw manifests; it is **strictly excluded** from all dynamic power numbers and percentages.

---

## 2. Gate-Level Activity (VCD) Generation

Workload-specific activity is captured by simulating the mapped, placed, and routed gate-level netlist in Icarus Verilog (`iverilog`) or native simulators with standard-cell functional models:

| Design | Workload Name | Activity Profile / Description | Transaction Count | Scope Name |
|---|---|---|---|---|
| **GCD** | `idle` | Zero input requests; clock running continuously with clock enable low. | 0 transactions (1,000 cycles) | `tb_gcd_activity/dut` |
| **GCD** | `typical` | Pseudo-random operand pairs with typical bit toggling; answers verified against algorithmic reference. | 300 transactions | `tb_gcd_activity/dut` |
| **GCD** | `peak` | Alternating worst-case high-Hamming-distance bit patterns (`0xAAAA` / `0x5555`). | 500 transactions | `tb_gcd_activity/dut` |
| **AES** | `idle` | Inactive core with clock running; load signal deasserted. | 0 encryptions (500 cycles) | `tb_aes_activity/dut` |
| **AES** | `typical` | Sequential 128-bit block encryptions with randomized plaintext and keys. | 25 encryptions | `tb_aes_activity/dut` |
| **AES** | `peak` | Dense back-to-back encryptions with maximum bit inversion across key rounds. | 50 encryptions | `tb_aes_activity/dut` |
| **RISCV32I** | `idle` | CPU in idle wait loop (`WFI` / branch-to-self loop). | 1,000 cycles | `tb_picorv32_activity/dut` |
| **RISCV32I** | `typical` | Dhrystone 2.1 benchmark execution loop. | 5,000 instructions | `tb_picorv32_activity/dut` |
| **RISCV32I** | `peak` | High-intensity ALU and shift stress test sequence. | 5,000 instructions | `tb_picorv32_activity/dut` |

### Pin Annotation Policy
OpenSTA annotates toggle rates directly from VCDs. The flow mandates:
- **100% Pin Annotation**: Any unannotated sequential pin causes an immediate sign-off gate failure.
- Clock pins are driven by the defined SDC clock domain; all data and sequential output pins derive transition densities directly from the VCD.

---

## 3. Parasitic Extraction (SPEF) Settings

Parasitic resistance and capacitance are extracted post-detailed-route using OpenROAD's integrated **OpenRCX** engine:
- **Sky130HD**:
  - Rules: `sky130hd.rcx.rules` (calibrated against field solvers for 5 metal layers: `met1` through `met5`).
  - Corner: Nominal typical process (`tt`, $25^\circ\text{C}$, $1.8\text{ V}$).
  - Coupling Capacitance: Extracted per coupled segment without decoupling approximations.
- **ASAP7**:
  - Rules: 7.5T PDK rules with 4x scaled routing pitches.
  - Corner: Typical corner (`tt`, $25^\circ\text{C}$, $0.7\text{ V}$).

---

## 4. Exact Timing Constraints per Design

SDC timing constraints enforce target performance without artificial relaxation:

### GCD (Sky130HD)
- Source: [`configs/gcd_baseline20ns.sdc`](../configs/gcd_baseline20ns.sdc)
- Target Period: **20.00 ns** ($f_{\text{clk}} = 50.0\text{ MHz}$)
- Clock Port: `clk`, Clock Name: `core_clock`
- Clock Latency: $0.290\text{ ns}$ (propagated post-CTS)
- I/O Delay: $20\%$ of clock period ($4.00\text{ ns}$) referenced to virtual clock `vclk_core_clock`.

### AES (Sky130HD)
- Source: [`configs/aes_power38.sdc`](../configs/aes_power38.sdc)
- Target Period: **3.80 ns** ($f_{\text{clk}} = 263.16\text{ MHz}$) — unrelaxed
- Clock Port: `clk`, Clock Name: `clk`
- Clock Latency: $0.710\text{ ns}$ (propagated post-CTS)
- I/O Delay: $20\%$ of clock period ($0.760\text{ ns}$) referenced to virtual clock `vclk_clk`.
- Non-Default Rule (NDR): All 107 leaf clock nets constrained with `AES_CLK_LEAF_1W_2S` (single width, double spacing).

### RISCV32I (Sky130HD)
- Source: [`build/power/cl_ovps_riscv32i_7ns_20261103/final_7ns.sdc`](../build/power/cl_ovps_riscv32i_7ns_20261103/final_7ns.sdc)
- Target Period: **7.00 ns** ($f_{\text{clk}} = 142.86\text{ MHz}$)
- Clock Port: `clk`, Clock Name: `clk`
- Clock Latency: $0.775\text{ ns}$ referenced to virtual clock `vclk_clk`.
- Output Delay: $1.150\text{ ns}$ on all outputs.

### ASAP7 GCD (ASAP7 7nm)
- Source: [`configs/asap7_gcd_phase3a_500ps.sdc`](../configs/asap7_gcd_phase3a_500ps.sdc)
- Target Period: **500.0 ps** ($f_{\text{clk}} = 2.0\text{ GHz}$)
- Clock Port: `clk`, Clock Name: `core_clock`
- Clock Latency: $36.225\text{ ps}$ referenced to `vclk_core_clock`.
- I/O Delay: $20\%$ of clock period ($100.0\text{ ps}$).

---

## 5. Logical Equivalence Checking (LEC) Methodology

Logical equivalence verification confirms functional invariance between baseline and candidate routed netlists:
- **Engine**: Yosys open synthesis suite (`equiv_make`, `equiv_simple`, `equiv_induct`).
- **Clock Tree Normalization**: Because CL-OVPS transforms physical clock distribution networks (replacing buffers with inverters or resizing clock cells), clock inputs to all sequential registers are normalized to the primary root clock domain in the equivalence representation.
- **Sequential Reset Abstraction**: Flip-flops with asynchronous reset (`$_DFF_PP0_` / `sky130_fd_sc_hd__dfrtp`) utilize an `async2sync` reset mapping in Yosys to allow inductive proving of sequential state invariants across clock restructuring.
- **Strict Boundary**: This open-source verification flow proves functional identity under clock normalization; it is documented as a Yosys equivalence flow, not a commercial sign-off formal equivalence tool.

