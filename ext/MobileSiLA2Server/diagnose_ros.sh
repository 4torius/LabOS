#!/bin/bash
# diagnose_ros.sh - Diagnose ROS setup for MobileSiLA2Server
# ===========================================================
#
# Run this script on the Ubuntu robot PC to verify the ROS
# environment is correctly set up.

echo "═══════════════════════════════════════════════════════════════════"
echo "            MobileSiLA2Server - ROS Environment Diagnostic"
echo "═══════════════════════════════════════════════════════════════════"
echo ""

# Check ROS distro
echo "1. ROS Distribution:"
if [ -z "$ROS_DISTRO" ]; then
    echo "   ✗ ROS not sourced!"
    echo "   → Run: source /opt/ros/noetic/setup.bash"
else
    echo "   ✓ ROS $ROS_DISTRO sourced"
fi

# Check ROS Master
echo ""
echo "2. ROS Master:"
echo "   ROS_MASTER_URI: ${ROS_MASTER_URI:-not set}"
rostopic list >/dev/null 2>&1
if [ $? -eq 0 ]; then
    echo "   ✓ ROS Master is accessible"
else
    echo "   ✗ Cannot connect to ROS Master"
    echo "   → Make sure roscore is running"
fi

# Check action server
echo ""
echo "3. Action Server (/setup1/state_exec):"
ACTION_TOPICS=$(rostopic list 2>/dev/null | grep "/setup1/state_exec" | wc -l)
if [ "$ACTION_TOPICS" -gt 0 ]; then
    echo "   ✓ Action server topics found ($ACTION_TOPICS topics)"
    rostopic list 2>/dev/null | grep "/setup1/state_exec" | while read topic; do
        echo "      - $topic"
    done
else
    echo "   ✗ Action server not found"
    echo "   → Check that the robot controller is running"
fi

# Check action goal type
echo ""
echo "4. Action Goal Type:"
GOAL_TYPE=$(rostopic type /setup1/state_exec/goal 2>/dev/null)
if [ -n "$GOAL_TYPE" ]; then
    echo "   ✓ Goal type: $GOAL_TYPE"
else
    echo "   ✗ Cannot determine goal type"
fi

# Check service
echo ""
echo "5. Task Discovery Service (/setup1/getSubtasksInfo):"
SVC=$(rosservice list 2>/dev/null | grep "getSubtasksInfo")
if [ -n "$SVC" ]; then
    echo "   ✓ Service available: $SVC"
    SVC_TYPE=$(rosservice type $SVC 2>/dev/null)
    echo "   Type: ${SVC_TYPE:-unknown}"
else
    echo "   ✗ Service not found (may be normal - loaded on demand)"
fi

# Check rpwc_msgs
echo ""
echo "6. rpwc_msgs Package:"
python3 -c "from rpwc_msgs.msg import executionCommandsAction; print('   ✓ rpwc_msgs.msg executionCommandsAction available')" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "   ✗ rpwc_msgs.msg executionCommandsAction NOT in PYTHONPATH"
fi

python3 -c "from rpwc_msgs.srv import getSubtaskInfo; print('   ✓ rpwc_msgs.srv getSubtaskInfo available')" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "   ✗ rpwc_msgs.srv getSubtaskInfo NOT in PYTHONPATH"
fi

# Check PYTHONPATH for catkin
echo ""
echo "7. Python Path (catkin):"
CATKIN_PATHS=$(echo $PYTHONPATH | tr ':' '\n' | grep -E "catkin|devel")
if [ -n "$CATKIN_PATHS" ]; then
    echo "$CATKIN_PATHS" | while read p; do
        if [ -d "$p" ]; then
            echo "   ✓ $p"
        else
            echo "   ✗ $p (not found)"
        fi
    done
else
    echo "   ✗ No catkin workspace in PYTHONPATH"
    echo "   → Source your catkin workspace: source ~/exsensia/devel/setup.bash"
fi

# Search for rpwc_msgs
echo ""
echo "8. Searching for rpwc_msgs package:"
RPWC_PATHS=$(find ~/exsensia -name "rpwc_msgs" -type d 2>/dev/null | head -3)
if [ -z "$RPWC_PATHS" ]; then
    RPWC_PATHS=$(find ~/catkin_ws -name "rpwc_msgs" -type d 2>/dev/null | head -3)
fi
if [ -n "$RPWC_PATHS" ]; then
    echo "$RPWC_PATHS" | while read p; do
        echo "   Found: $p"
    done
else
    echo "   ✗ rpwc_msgs not found in ~/exsensia or ~/catkin_ws"
fi

# Check for catkin workspaces
echo ""
echo "9. Catkin Workspaces Found:"
for ws in ~/exsensia/tmp_ws ~/exsensia/catkin_ws ~/exsensia ~/catkin_ws; do
    if [ -f "$ws/devel/setup.bash" ]; then
        echo "   ✓ $ws"
    fi
done

# Check SiLA2 server dependencies
echo ""
echo "10. Python Dependencies:"
python3 -c "import grpc; print('   ✓ grpcio')" 2>/dev/null || echo "   ✗ grpcio (pip3 install grpcio)"
python3 -c "import yaml; print('   ✓ pyyaml')" 2>/dev/null || echo "   ✗ pyyaml (pip3 install pyyaml)"
python3 -c "import zeroconf; print('   ✓ zeroconf')" 2>/dev/null || echo "   ✗ zeroconf (pip3 install zeroconf)"

# Network info
echo ""
echo "11. Network Configuration:"
IP=$(hostname -I | awk '{print $1}')
echo "    Local IP: $IP"
echo "    SiLA2 Server will be available at: $IP:50053"

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo ""
echo "If rpwc_msgs is not in PYTHONPATH, run:"
echo "  source ~/exsensia/tmp_ws/devel/setup.bash"
echo "  # or wherever your catkin workspace with rpwc_msgs is located"
echo ""
echo "Then start the server:"
echo "  ./run_server.sh"
echo ""
