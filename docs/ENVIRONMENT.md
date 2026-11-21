# Environment & Toolchain Specifications

This document defines the exact host environment, EDA toolchain revisions, compiler options, and PDK versions used to produce the signed-off CL-OVPS results.

---

## 1. Host Hardware & Operating System
- **Platform**: Apple Silicon (MacBook Air / ARM64 `darwin24.0.0`)
- **Host Compiler**: AppleClang 17.0.0 (clang-1700.0.13.5)
- **Host Build Tools**:
  - GNU Bison: 3.8.2
  - Flex: 2.6.4
  - CMake: 3.30+
- **Host Python**: Python 3.14.7 (`/opt/homebrew/bin/python3`)

---

## 2. Pinned EDA Toolchain & Component Commits
The exact commits and platform configs are frozen in [`versions.lock`](../versions.lock):

| Tool / Component | Repository URL | Pinned Commit / Version | Build Date | Notes |
|---|---|---|---|---|
| **OpenROAD** | `https://github.com/The-OpenROAD-Project/OpenROAD.git` | `bc739caf43047bfc2e225fe10dd968d672b617f8` | 2026-10-16 | Release version `26Q4-518-gbc739caf43` |
| **OpenSTA** | Integrated inside OpenROAD submodule | Embedded in OpenROAD `bc739ca` | 2026-10-16 | Used for sign-off SPEF/VCD timing & dynamic power analysis |
| **OpenROAD-flow-scripts (ORFS)** | `https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts.git` | `64939f3717ad7c694199ba04cb98924b98022868` | 2026-10-25 | Baseline physical design flows |
| **Yosys** | `https://github.com/YosysHQ/yosys.git` | `c12172fbae8af5e20f6fb52e3d4e92d56ed587b6` | 2026-10-26 | Version `0.68+post`; powers LEC equivalence verification |
| **PicoRV32** | `https://github.com/YosysHQ/picorv32.git` | `ef203c2b0a3fb793280f5114941416c425c5b461` | 2026-10-26 | Claire Wolf PicoRV32 RISC-V CPU core |

---

## 3. Process Design Kits (PDKs)
1. **SkyWater Sky130HD**:
   - PDK Manager: Volare `0.20.6`
   - open_pdks Commit: `c6d73a35f524070e85faff4a6a9eef49553ebc2b`
   - Standard Cell Library: `sky130_fd_sc_hd`
   - Liberty View: `sky130_fd_sc_hd__tt_025C_1v80.lib` (SHA-256: `ec0e1067a35c8bf20b11e58d1e8ac53326067e4dac84a125cc1b917a3518d0d9`)
   - RCX View: `sky130hd.rcx.rules` (SHA-256: `d5bd9b1077ceac9929416abca272a6d52e51cd9a575453a31d129817bb286476`)
   - Verilog Simulation Models: `sky130_fd_sc_hd.v` (SHA-256: `12d5d6448ccf265c544d88402d5c10ab6de9bee8b8cde48eee1f31ea8c3c3e45`)
2. **ASAP7 (7nm Predictive Academic PDK)**:
   - Source: OpenROAD test suite / ORFS platform distribution (`platforms/asap7`)
   - Standard Cell Libraries: 15 TT libraries (AO, INVBUF, OA, SEQ, SIMPLE × LVT, RVT, SLVT)
   - Grid & Rules: 7.5T library, 4x scaled routing grid for OpenROAD routing.

---

## 4. Verification Without OpenROAD
Reviewers do not need OpenROAD or Yosys installed to audit the published evidence.
- Run `python3 scripts/verify_results.py` to parse manifests, verify SHA-256 checksums, and re-derive all power, timing, and gate metrics.
- Run `pytest` to execute unit tests on gate evaluators and manifest parsers.

