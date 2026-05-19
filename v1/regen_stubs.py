#!/usr/bin/env python3
"""Regenerate gRPC stubs from all proto files.

SiLA2Common is generated into both src/pnp_stubs/ (used by the core client)
and SiLA2/ (imported directly by the server implementations).
"""

import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.absolute()
OUT_DIR = BASE_DIR / "src" / "pnp_stubs"

PROTOS = [
    # SiLA2Common — used by Strategy 1 (legacy server support) in client.py
    {
        "file": BASE_DIR / "SiLA2" / "SiLA2Common.proto",
        "include": BASE_DIR / "SiLA2",
        "extra_out": BASE_DIR / "SiLA2",
    },
    # TecanLegacyBridge — Python bridge to C# TecanSiLA2Server COM layer (port 50055)
    # Used by TecanM200SiLA2Server/src/tecan_bridge_client.py
    {
        "file": BASE_DIR / "SiLA2" / "TecanM200SiLA2Server" / "src" / "TecanLegacyBridge.proto",
        "include": BASE_DIR / "SiLA2" / "TecanM200SiLA2Server" / "src",
        "extra_out": BASE_DIR / "SiLA2" / "TecanM200SiLA2Server" / "src",
    },
]


def regenerate() -> bool:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Ensure __init__.py exists
    init_file = OUT_DIR / "__init__.py"
    if not init_file.exists():
        init_file.write_text("")

    all_ok = True
    for entry in PROTOS:
        proto_file = entry["file"]
        include_dir = entry["include"]

        if not proto_file.exists():
            print(f"  SKIP (not found): {proto_file.name}")
            continue

        output_dirs = [OUT_DIR]
        if "extra_out" in entry:
            output_dirs.append(entry["extra_out"])

        for out in output_dirs:
            out.mkdir(parents=True, exist_ok=True)
            cmd = [
                sys.executable, "-m", "grpc_tools.protoc",
                f"-I{include_dir}",
                f"--python_out={out}",
                f"--grpc_python_out={out}",
                str(proto_file),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"  FAIL: {proto_file.name} → {out.name}/")
                if result.stderr:
                    print(f"    {result.stderr.strip()}")
                all_ok = False
                break
        else:
            print(f"  OK: {proto_file.name}")

    return all_ok


if __name__ == "__main__":
    print("Regenerating gRPC stubs...")
    success = regenerate()
    print("Done." if success else "Completed with errors.")
    sys.exit(0 if success else 1)
