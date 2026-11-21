# Third-Party Notices and Licences

CL-OVPS incorporates, interfaces with, or evaluates against several open-source hardware designs and electronic design automation (EDA) platforms. All upstream copyrights, licences, and original notices are preserved.

---

## 1. PicoRV32 (RISC-V CPU Core)
- **Origin**: [YosysHQ/picorv32](https://github.com/YosysHQ/picorv32)
- **Author**: Claire Xenia Wolf (`claire@yosyshq.com`)
- **Pinned Commit**: `ef203c2b0a3fb793280f5114941416c425c5b461`
- **Location in repo**: `designs/picorv32/` (upstream `.git` repository excluded)
- **Licence**: ISC Licence
- **Notice**:
```
ISC License

Copyright (C) 2015 - 2021  Claire Xenia Wolf <claire@yosyshq.com>

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

---

## 2. GCD (Greatest Common Divisor)
- **Origin**: OpenROAD-flow-scripts benchmark suite ([The-OpenROAD-Project/OpenROAD-flow-scripts](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts))
- **Original Source**: Generated from PyMTL (Cornell University / Batten Research Group), modified for OpenROAD tools.
- **Location in repo**: `designs/gcd/` (`gcd.v`, testbench `sim/tb_gcd_activity.sv`, UPF configuration)
- **Licence**: BSD-3-Clause (OpenROAD-flow-scripts licence)
- **Notice**:
```
Copyright (c) 2019-2023, The Regents of the University of California
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its contributors
   may be used to endorse or promote products derived from this software without
   specific prior written permission.
```

---

## 3. AES (Advanced Encryption Standard Core)
- **Origin**: OpenCores AES Core ([OpenCores/aes_core](http://www.opencores.org/cores/aes_core/)), distributed within OpenROAD-flow-scripts (`designs/src/aes/`).
- **Author**: Rudolf Usselmann (`rudi@asics.ws`)
- **Location in repo**: `designs/aes/` contains CL-OVPS flow configurations, leaf-clock NDR routing script, and testbench `sim/tb_aes_activity.sv`. The Verilog RTL source is referenced from the pinned ORFS tree.
- **Licence**: Permissive BSD-style notice:
```
Copyright (C) 2000-2002 Rudolf Usselmann
                         www.asics.ws
                         rudi@asics.ws

This source file may be used and distributed without restriction provided that
this copyright statement is not removed from the file and that any derivative
work contains the original copyright notice and the associated disclaimer.

THIS SOFTWARE IS PROVIDED ``AS IS'' AND WITHOUT ANY EXPRESS OR IMPLIED
WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
```

---

## 4. EDA Toolchains and PDKs (Not Vendored)
- **OpenROAD & OpenSTA**: BSD 3-Clause, [The-OpenROAD-Project/OpenROAD](https://github.com/The-OpenROAD-Project/OpenROAD). Pinned commit `bc739caf43047bfc2e225fe10dd968d672b617f8`.
- **OpenROAD Flow Scripts (ORFS)**: BSD 3-Clause, [The-OpenROAD-Project/OpenROAD-flow-scripts](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts). Pinned commit `64939f3717ad7c694199ba04cb98924b98022868`.
- **Yosys**: ISC Licence, [YosysHQ/yosys](https://github.com/YosysHQ/yosys). Commit `c12172fbae8af5e20f6fb52e3d4e92d56ed587b6`.
- **SkyWater Sky130 PDK**: Apache 2.0 Licence, accessed via Volare (`0.20.6`), commit `c6d73a35f524070e85faff4a6a9eef49553ebc2b`.
- **ASAP7 Predictive PDK**: Academic/Open research PDK developed by Arizona State University in collaboration with Arm, distributed with OpenROAD test suites under Apache 2.0.

