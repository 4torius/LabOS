#!/bin/bash
# regenerate_proto.sh - Regenerate protobuf files for current system
# ==================================================================
#
# Run this on the Ubuntu server to regenerate TaskManagement_pb2.py
# with the locally installed protobuf version.
#
# Usage:
#   ./regenerate_proto.sh

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

echo "═══════════════════════════════════════════════════════════════════"
echo "            Regenerating Protobuf Files"
echo "═══════════════════════════════════════════════════════════════════"

# Check grpcio-tools is installed
if ! python3 -c "import grpc_tools.protoc" 2>/dev/null; then
    echo "Installing grpcio-tools..."
    pip3 install --user grpcio-tools
fi

# Show versions
echo ""
echo "Protobuf version:"
python3 -c "import google.protobuf; print(f'  Runtime: {google.protobuf.__version__}')"
python3 -c "from grpc_tools import protoc; print('  grpc_tools: installed')"

# Regenerate
echo ""
echo "Regenerating from features/TaskManagement.proto..."

python3 -m grpc_tools.protoc \
    -I./features \
    --python_out=./features \
    --grpc_python_out=./features \
    ./features/TaskManagement.proto

# Fix imports in generated file
# The grpc file imports the pb2 module without the package prefix
sed -i 's/import TaskManagement_pb2/from . import TaskManagement_pb2/' ./features/TaskManagement_pb2_grpc.py 2>/dev/null || true

echo ""
echo "✓ Generated files:"
ls -la ./features/TaskManagement_pb2*.py

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo " Done! Now run: ./run_server.sh"
echo "═══════════════════════════════════════════════════════════════════"
