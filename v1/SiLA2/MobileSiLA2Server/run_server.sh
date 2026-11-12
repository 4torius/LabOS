#!/bin/bash
# Run MobileSiLA2Server on Ubuntu (sila2 standard library)
# =========================================================
#
# Usage:
#   ./run_server.sh              # production mode (real ROS)
#   ./run_server.sh --simulate   # test without ROS

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# ── Source ROS ────────────────────────────────────────────────────────────────
if [ -f /opt/ros/noetic/setup.bash ]; then
    source /opt/ros/noetic/setup.bash
    echo "✓ Sourced ROS Noetic"
elif [ -f /opt/ros/melodic/setup.bash ]; then
    source /opt/ros/melodic/setup.bash
    echo "✓ Sourced ROS Melodic"
else
    echo "WARNING: ROS not found — starting in simulation mode"
fi

# ── Source catkin workspace (rpwc_msgs) ───────────────────────────────────────
WS_FOUND=false
for ws in ~/exsensia/tmp_ws ~/exsensia/catkin_ws ~/exsensia ~/catkin_ws; do
    if [ -f "$ws/devel/setup.bash" ]; then
        source "$ws/devel/setup.bash"
        echo "✓ Sourced catkin workspace: $ws"
        WS_FOUND=true
        break
    fi
done

if [ "$WS_FOUND" = false ]; then
    echo "WARNING: No catkin workspace found — rpwc_msgs may not be available."
    echo "         Checked: ~/exsensia/tmp_ws, ~/exsensia/catkin_ws, ~/exsensia, ~/catkin_ws"
fi

# ── Check sila2 library ───────────────────────────────────────────────────────
echo ""
echo "Checking dependencies..."
python3 -c "import sila2; print(f'✓ sila2 {sila2.__version__} available')" 2>/dev/null \
    || { echo "✗ sila2 not found — run: pip3 install -r requirements.txt --user"; exit 1; }

python3 -c "from rpwc_msgs.msg import executionCommandsAction; print('✓ rpwc_msgs.msg available')" 2>/dev/null \
    || echo "  rpwc_msgs not found — starting in simulation mode"

# ── Set ROS environment ───────────────────────────────────────────────────────
export ROS_IP=${ROS_IP:-$(hostname -I | awk '{print $1}')}
export ROS_MASTER_URI=${ROS_MASTER_URI:-http://localhost:11311}

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "             MobileSiLA2Server - sila2 standard library"
echo "═══════════════════════════════════════════════════════════════════"
echo " ROS_MASTER_URI : $ROS_MASTER_URI"
echo " ROS_IP         : $ROS_IP"
echo " SiLA2 port     : 50053"
echo " Action server  : /setup1/state_exec"
echo " Task service   : /setup1/getSubtasksInfo"
echo "═══════════════════════════════════════════════════════════════════"
echo ""

# Pass --production by default (run_server.sh is for real use)
# Pass --simulate explicitly if needed
if [[ "$*" == *"--simulate"* ]]; then
    python3 main.py "$@"
else
    python3 main.py --production "$@"
fi
