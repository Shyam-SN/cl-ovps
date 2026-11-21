#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VOLARE="${ROOT_DIR}/tools/volare-env/bin/volare"
PDK_ROOT="${ROOT_DIR}/pdk"
PDK_VERSION="c6d73a35f524070e85faff4a6a9eef49553ebc2b"

if [[ ! -x "${VOLARE}" ]]; then
  python3 -m venv "${ROOT_DIR}/tools/volare-env"
  "${ROOT_DIR}/tools/volare-env/bin/python" -m pip install volare==0.20.6
fi

"${VOLARE}" fetch --pdk-root "${PDK_ROOT}" --pdk sky130 "${PDK_VERSION}"
"${VOLARE}" enable --pdk-root "${PDK_ROOT}" --pdk sky130 "${PDK_VERSION}"
