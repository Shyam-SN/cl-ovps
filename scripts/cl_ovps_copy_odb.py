#!/usr/bin/env python3
"""Copy a frozen placement ODB into a flow-plan Stage 1 output directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-odb", required=True, type=Path)
    parser.add_argument("--output-odb", required=True, type=Path)
    args = parser.parse_args()
    if not args.input_odb.is_file():
        parser.error(f"input ODB does not exist: {args.input_odb}")
    output = args.output_odb.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        parser.error(f"refusing to overwrite ODB: {output}")
    shutil.copy2(args.input_odb, output)
    manifest = {"status": "COPIED", "input_odb": str(args.input_odb.resolve()),
                "input_sha256": sha256(args.input_odb), "output_odb": str(output),
                "output_sha256": sha256(output)}
    (output.parent / "stage1_copy_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"CL_OVPS_STAGE1_COPY status=COPIED output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
