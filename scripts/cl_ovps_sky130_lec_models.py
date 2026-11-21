"""Sky130HD functional model paths and UDP adapters used by CL-OVPS LEC."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = next((ROOT / "pdk/volare/sky130/versions").glob(
    "*/sky130B/libs.ref/sky130_fd_sc_hd/verilog/primitives.v"), None)
CELL_MODEL = None if PRIMITIVES is None else PRIMITIVES.with_name("sky130_fd_sc_hd.v")


def primitive_blackbox_stubs(path: Path) -> str:
    """Provide compact UDP models for muxes/power-good and blackbox the rest."""
    text = path.read_text(errors="replace")
    blocks = re.finditer(r"(?ms)^primitive\s+([^\s(]+)\s*\((.*?)\);(.*?)^endprimitive", text)
    modules: list[str] = []
    for match in blocks:
        name = match.group(1)
        port_names = [port.strip() for port in match.group(2).split(",") if port.strip()]
        directions = re.findall(r"^\s*(output|input)\s+([A-Za-z0-9_$]+)\s*;", match.group(3), re.M)
        if not port_names or len(directions) < len(port_names):
            raise ValueError(f"could not extract UDP ports for {name}")
        declarations = "\n".join(f"  {direction} {port};" for direction, port in directions)
        if name == "sky130_fd_sc_hd__udp_mux_2to1":
            modules.append(f"module {name} ({', '.join(port_names)});\n{declarations}\n"
                           f"  assign {port_names[0]} = S ? A1 : A0;\nendmodule\n")
        elif name == "sky130_fd_sc_hd__udp_mux_2to1_N":
            modules.append(f"module {name} ({', '.join(port_names)});\n{declarations}\n"
                           f"  assign {port_names[0]} = ~(S ? A1 : A0);\nendmodule\n")
        elif name == "sky130_fd_sc_hd__udp_mux_4to2":
            modules.append(f"module {name} ({', '.join(port_names)});\n{declarations}\n"
                           f"  assign {port_names[0]} = S1 ? (S0 ? A3 : A2) : (S0 ? A1 : A0);\n"
                           "endmodule\n")
        elif name.startswith("sky130_fd_sc_hd__udp_pwrgood"):
            # Routed netlists omit VPWR/VGND pins. Model the powered logical path.
            expression = (f"SLEEP ? 1'b0 : {port_names[1]}"
                          if name.endswith("$PG$S") else port_names[1])
            modules.append(f"module {name} ({', '.join(port_names)});\n{declarations}\n"
                           f"  assign {port_names[0]} = {expression};\nendmodule\n")
        elif name in {"sky130_fd_sc_hd__udp_dlatch$P",
                      "sky130_fd_sc_hd__udp_dlatch$P_pp$PG$N"}:
            # Functional model for the level-sensitive latch inside dlclkp.
            # The gate enable is held stable while CLK is high, which is the
            # glitch-free phase relationship enforced by the Sky130 cell.
            gate_condition = "GATE && VPWR && !VGND" if "VPWR" in port_names else "GATE"
            modules.append(f"module {name} ({', '.join(port_names)});\n{declarations}\n"
                           "  reg q_state;\n"
                           f"  always @* if ({gate_condition}) q_state = D;\n"
                           "  assign Q = q_state;\nendmodule\n")
        else:
            modules.append(f"(* blackbox *) module {name} ({', '.join(port_names)});\n" +
                           declarations + "\nendmodule\n")
    modules.extend((
        "module pullup(output O); assign O = 1'b1; endmodule\n",
        "module pulldown(output O); assign O = 1'b0; endmodule\n",
        "module lec_dfxtp_1(output reg Q, input CLK, input D, input VPWR, input VGND, input VPB, input VNB);\n"
        "  always @(posedge CLK) Q <= D;\nendmodule\n",
        "module lec_edfxtp_1(output reg Q, input CLK, input D, input DE, input VPWR, input VGND, input VPB, input VNB);\n"
        "  always @(posedge CLK) if (DE) Q <= D;\nendmodule\n",
        "module lec_dfxtn_1(output reg Q, input CLK_N, input D, input VPWR, input VGND, input VPB, input VNB);\n"
        "  always @(negedge CLK_N) Q <= D;\nendmodule\n",
        "module lec_edfxtn_1(output reg Q, input CLK_N, input D, input DE, input VPWR, input VGND, input VPB, input VNB);\n"
        "  always @(negedge CLK_N) if (DE) Q <= D;\nendmodule\n",
    ))
    return "\n".join(modules)
