#!/bin/bash
# Run MobileSiLA2Server on Ubuntu
# ================================
#
# This server runs on the ROS PC (inspection@iitreh002lw012u)
# and exposes tasks via SiLA2 to the orchestrator.
#
# Usage:
#   ./run_server.sh              # Normal mode (connect to real ROS)
#   ./run_server.sh --simulate   # Test without ROS

# Source ROS
if [ -f /opt/ros/noetic/setup.bash ]; then
    source /opt/ros/noetic/setup.bash
    echo "✓ Sourced ROS Noetic"
elif [ -f /opt/ros/melodic/setup.bash ]; then
    source /opt/ros/melodic/setup.bash
    echo "✓ Sourced ROS Melodic"
else
    echo "ERROR: ROS not found. Install ROS Noetic/Melodic first."
    exit 1
fi

# Source catkin workspace - check multiple locations
WS_FOUND=false

# Check exsensia tmp_ws workspace (used for rpwc_msgs based on user setup)
if [ -f ~/exsensia/tmp_ws/devel/setup.bash ]; then
    source ~/exsensia/tmp_ws/devel/setup.bash
    echo "✓ Sourced ~/exsensia/tmp_ws"
    WS_FOUND=true
# Check exsensia workspace (primary for rpwc_msgs)
elif [ -f ~/exsensia/catkin_ws/devel/setup.bash ]; then
    source ~/exsensia/catkin_ws/devel/setup.bash
    echo "✓ Sourced ~/exsensia/catkin_ws"
    WS_FOUND=true
elif [ -f ~/exsensia/devel/setup.bash ]; then
    source ~/exsensia/devel/setup.bash
    echo "✓ Sourced ~/exsensia workspace"
    WS_FOUND=true
elif [ -f ~/catkin_ws/devel/setup.bash ]; then
    source ~/catkin_ws/devel/setup.bash
    echo "✓ Sourced ~/catkin_ws"
    WS_FOUND=true
fi

if [ "$WS_FOUND" = false ]; then
    echo "WARNING: No catkin workspace found. rpwc_msgs may not be available."
    echo "         Checked: ~/exsensia/tmp_ws, ~/exsensia/catkin_ws, ~/exsensia, ~/catkin_ws"
fi

# Verify rpwc_msgs is available
echo ""
echo "Checking rpwc_msgs..."
python3 -c "from rpwc_msgs.msg import executionCommandsAction; print('✓ rpwc_msgs.msg executionCommandsAction available')" 2>/dev/null || echo "✗ rpwc_msgs.msg NOT FOUND - check catkin workspace"
python3 -c "from rpwc_msgs.srv import getSubtasksInfo; print('✓ rpwc_msgs.srv getSubtasksInfo available')" 2>/dev/null || echo "✗ rpwc_msgs.srv NOT FOUND"

# Get this script's directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Set ROS environment
# For local ROS (server runs on same machine as ROS master)
export ROS_IP=$(hostname -I | awk '{print $1}')
export ROS_MASTER_URI=${ROS_MASTER_URI:-http://localhost:11311}

# Check if ROS master is running
echo ""
rostopic list >/dev/null 2>&1
if [ $? -eq 0 ]; then
    echo "✓ ROS Master is running"
else
    echo "✗ WARNING: Cannot connect to ROS Master at $ROS_MASTER_URI"
    echo "  Make sure roscore is running or check ROS_MASTER_URI"
fi

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "             MobileSiLA2Server - Task Execution Bridge"
echo "═══════════════════════════════════════════════════════════════════"
echo " ROS_MASTER_URI: $ROS_MASTER_URI"
echo " ROS_IP:         $ROS_IP"
echo " SiLA2 Server:   0.0.0.0:50053"
echo ""
echo " Action Server:  /setup1/state_exec"
echo " Task Service:   /setup1/getSubtasksInfo"
echo "═══════════════════════════════════════════════════════════════════"
echo ""

# Check protobuf compatibility and regenerate if needed
echo "Checking protobuf compatibility..."
if ! python3 -c "from features import TaskManagement_pb2" 2>/dev/null; then
    echo "⚠ Protobuf version mismatch detected - regenerating..."
    
    # Install grpcio-tools if needed
    pip3 install --user grpcio-tools 2>/dev/null || pip3 install grpcio-tools
    
    # Regenerate proto files
    python3 -m grpc_tools.protoc \
        -I./features \
        --python_out=./features \
        --grpc_python_out=./features \
        ./features/TaskManagement.proto
    
    # Fix imports
    sed -i 's/import TaskManagement_pb2/from . import TaskManagement_pb2/' ./features/TaskManagement_pb2_grpc.py 2>/dev/null || true
    
    echo "✓ Protobuf files regenerated"
else
    echo "✓ Protobuf files compatible"
fi
echo ""

# Run server
# Options:
#   --simulate    : Run without actual ROS connection (for testing)
#   --production  : Connect to real ROS (default)
#   --config X    : Use custom config file

python3 main.py --production "$@"
