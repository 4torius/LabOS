#!/usr/bin/env bash
# Regenerate gRPC stubs from TaskManagement.proto.
# Run this on the Ubuntu robot PC after any .proto change.
#
# Requirements:
#   pip install grpcio-tools

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FEATURES_DIR="$SCRIPT_DIR/features"

echo "Regenerating gRPC stubs from TaskManagement.proto..."

python3 -m grpc_tools.protoc \
    --proto_path="$FEATURES_DIR" \
    --python_out="$FEATURES_DIR" \
    --grpc_python_out="$FEATURES_DIR" \
    "$FEATURES_DIR/TaskManagement.proto"

echo "Done. Generated files:"
echo "  features/TaskManagement_pb2.py"
echo "  features/TaskManagement_pb2_grpc.py"
