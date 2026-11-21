#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENROAD_SRC="${ROOT_DIR}/tools/openroad"
PATCH_FILE="${ROOT_DIR}/patches/openroad-fmt12.patch"
TEST_PATCH_FILE="${ROOT_DIR}/patches/openroad-stt-tests-off.patch"

if ! grep -Fq 'fmt 12 moved fmt::format out of fmt/core.h' "${OPENROAD_SRC}/third-party/CMakeLists.txt"; then
  git -C "${OPENROAD_SRC}" apply "${PATCH_FILE}"
fi

if ! grep -Fq 'if (ENABLE_TESTS)' "${OPENROAD_SRC}/src/stt/CMakeLists.txt"; then
  git -C "${OPENROAD_SRC}" apply "${TEST_PATCH_FILE}"
fi

export PATH="/opt/homebrew/opt/bison/bin:/opt/homebrew/opt/flex/bin:${PATH}"
export CC="/usr/bin/clang"
export CXX="/usr/bin/clang++"

cmake \
  -DBUILD_GUI=OFF \
  -DBUILD_PYTHON=OFF \
  -DENABLE_TESTS=OFF \
  -DBUILD_TESTS=OFF \
  -DCMAKE_BUILD_TYPE=Release \
  -DBISON_EXECUTABLE=/opt/homebrew/opt/bison/bin/bison \
  -DFLEX_EXECUTABLE=/opt/homebrew/opt/flex/bin/flex \
  -DFLEX_INCLUDE_DIR=/opt/homebrew/opt/flex/include \
  -DTCL_LIBRARY=/opt/homebrew/opt/tcl-tk@8/lib/libtcl8.6.dylib \
  -DTCL_INCLUDE_PATH=/opt/homebrew/opt/tcl-tk@8/include/tcl-tk \
  -DTCL_HEADER=/opt/homebrew/opt/tcl-tk@8/include/tcl-tk/tcl.h \
  -DCMAKE_CXX_FLAGS=-DBOOST_STACKTRACE_GNU_SOURCE_NOT_REQUIRED \
  -DCMAKE_EXE_LINKER_FLAGS=-L/opt/homebrew/lib \
  -S "${OPENROAD_SRC}" \
  -B "${OPENROAD_SRC}/build"

cmake --build "${OPENROAD_SRC}/build" -j "${OPENROAD_BUILD_JOBS:-4}"
