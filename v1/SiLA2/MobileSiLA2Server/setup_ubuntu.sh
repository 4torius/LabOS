#!/bin/bash
# Setup MobileSiLA2Server on Ubuntu ROS PC
# =========================================
#
# This script sets up and runs the MobileSiLA2Server on the Ubuntu machine
# where ROS is installed.
#
# Prerequisites:
# - ROS Noetic/Melodic installed and sourced
# - Python 3.8+
# - rpwc_msgs package (with executionCommand.action and getSubtaskInfo.srv)
#
# Quick setup:
#   1. Copy this folder to the Ubuntu PC
#   2. Run: ./setup_ubuntu.sh
#   3. Run: ./run_server.sh

set -e

echo "========================================"
echo " MobileSiLA2Server Setup for Ubuntu"
echo "========================================"

# Check ROS
if [ -z "$ROS_DISTRO" ]; then
    echo "ERROR: ROS not sourced. Run: source /opt/ros/noetic/setup.bash"
    exit 1
fi
echo "✓ ROS $ROS_DISTRO detected"

# Check Python
PYTHON_VERSION=$(python3 --version)
echo "✓ $PYTHON_VERSION"

# Install Python dependencies
echo ""
echo "Installing Python dependencies..."
pip3 install --user grpcio grpcio-tools pyyaml zeroconf

# Check rospy
python3 -c "import rospy" && echo "✓ rospy available" || echo "✗ rospy not found (install ros-$ROS_DISTRO-rospy)"

# Check actionlib  
python3 -c "import actionlib" && echo "✓ actionlib available" || echo "✗ actionlib not found (install ros-$ROS_DISTRO-actionlib)"

# Check rpwc_msgs (optional, depends on your setup)
echo ""
echo "Checking rpwc_msgs..."
python3 -c "from rpwc_msgs.msg import executionCommandsAction" 2>/dev/null && \
    echo "✓ rpwc_msgs.msg executionCommandsAction available" || \
    echo "✗ rpwc_msgs not found - make sure it's in your catkin workspace"

echo ""
echo "========================================"
echo " Setup complete!"
echo "========================================"
echo ""
echo "To start the server:"
echo "  ./run_server.sh"
echo ""
echo "Or manually:"
echo "  source /opt/ros/$ROS_DISTRO/setup.bash"
echo "  source ~/catkin_ws/devel/setup.bash  # if you have a catkin workspace"
echo "  python3 main.py"
