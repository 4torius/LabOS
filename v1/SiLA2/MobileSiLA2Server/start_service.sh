#!/bin/bash
# ============================================================
# Systemd wrapper for MobileSiLA2Server
# ============================================================
# Called by mobile_sila2.service — sources ROS and the catkin
# workspace before starting the Python server.
# systemd does NOT execute .bashrc, so all environment setup
# must happen here explicitly.

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# ── Source ROS ────────────────────────────────────────────────
if [ -f /opt/ros/noetic/setup.bash ]; then
    source /opt/ros/noetic/setup.bash
elif [ -f /opt/ros/melodic/setup.bash ]; then
    source /opt/ros/melodic/setup.bash
else
    echo "ERROR: ROS not found" >&2
    exit 1
fi

# ── Source catkin workspace (rpwc_msgs lives here) ────────────
for ws in ~/exsensia/tmp_ws ~/exsensia/catkin_ws ~/exsensia ~/catkin_ws; do
    if [ -f "$ws/devel/setup.bash" ]; then
        source "$ws/devel/setup.bash"
        break
    fi
done

# ── ROS environment ───────────────────────────────────────────
export ROS_MASTER_URI="${ROS_MASTER_URI:-http://localhost:11311}"
# Use the first non-loopback IP as ROS_IP (WiFi interface for LabOS communication)
export ROS_IP="${ROS_IP:-$(hostname -I | awk '{print $1}')}"

echo "Starting MobileSiLA2Server"
echo "  ROS_MASTER_URI: $ROS_MASTER_URI"
echo "  ROS_IP:         $ROS_IP"

# ── Start server ──────────────────────────────────────────────
exec python3 "$SCRIPT_DIR/main.py" --production
