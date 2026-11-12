#!/usr/bin/env python3
"""Regenerate gRPC Python stubs from TaskManagement.proto.

Output goes to v1/src/pnp_stubs/ so the webapp can import TaskManagement_pb2.
Run from anywhere:
    python ext/MobileSiLA2Server/regen_stubs.py
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent.absolute()
PROTO_FILE = HERE / "features" / "TaskManagement.proto"
INCLUDE_DIR = HERE / "features"

V1_STUBS = HERE.parent.parent / "v1" / "src" / "pnp_stubs"


def regenerate() -> bool:
    if not PROTO_FILE.exists():
        print(f"SKIP: {PROTO_FILE} not found")
        return False

    V1_STUBS.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "grpc_tools.protoc",
        f"-I{INCLUDE_DIR}",
        f"--python_out={V1_STUBS}",
        f"--grpc_python_out={V1_STUBS}",
        str(PROTO_FILE),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"FAIL: {result.stderr.strip()}")
        return False

    print(f"OK: TaskManagement.proto -> {V1_STUBS}/")
    return True


if __name__ == "__main__":
    success = regenerate()
    sys.exit(0 if success else 1)
