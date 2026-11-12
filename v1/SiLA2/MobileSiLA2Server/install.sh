#!/bin/bash
# ============================================================
# LabOS Mobile SiLA2 Server — Ubuntu Setup Script
# ============================================================
#
# Run once on the robot PC (Ubuntu 20.04 + ROS Noetic recommended)
# after copying the MobileSiLA2Server folder from the Windows PC.
#
# Usage:
#   chmod +x install.sh
#   ./install.sh
#
# After install:
#   ./run_server.sh              # manual start
#   sudo systemctl start mobile_sila2   # if systemd service installed

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SERVICE_USER="${USER:-inspection}"
INSTALL_DIR="$SCRIPT_DIR"

echo "============================================================"
echo " LabOS MobileSiLA2Server — Installation"
echo "============================================================"
echo " User:        $SERVICE_USER"
echo " Install dir: $INSTALL_DIR"
echo ""

# ── 1. Detect ROS ────────────────────────────────────────────
echo "[1/6] Detecting ROS..."
if [ -f /opt/ros/noetic/setup.bash ]; then
    ROS_DISTRO_FOUND="noetic"
    echo "  ✓ ROS Noetic found"
elif [ -f /opt/ros/melodic/setup.bash ]; then
    ROS_DISTRO_FOUND="melodic"
    echo "  ✓ ROS Melodic found"
else
    echo "  ✗ ROS not found at /opt/ros/"
    echo "    Install ROS Noetic: http://wiki.ros.org/noetic/Installation/Ubuntu"
    echo "    Or proceed without ROS to run in simulation mode."
    ROS_DISTRO_FOUND=""
fi

# ── 2. Install Python dependencies ───────────────────────────
echo ""
echo "[2/6] Installing Python dependencies..."
pip3 install -r "$INSTALL_DIR/requirements.txt" --user --quiet
echo "  ✓ Python packages installed"

# ── 3. Verify sila2 library and generated stubs ───────────────
echo ""
echo "[3/6] Checking sila2 library..."
cd "$INSTALL_DIR"

# Source ROS if available so python3 can find rospy
if [ -n "$ROS_DISTRO_FOUND" ]; then
    source /opt/ros/$ROS_DISTRO_FOUND/setup.bash 2>/dev/null || true
fi

if python3 -c "import sila2; print(f'  ✓ sila2 {sila2.__version__} available')" 2>/dev/null; then
    :
else
    echo "  ✗ sila2 not installed — installing now..."
    pip3 install --user "sila2[codegen]>=0.14.0" --quiet
    echo "  ✓ sila2 installed"
fi

if python3 -c "from generated.taskmanagement import TaskManagementFeature; print('  ✓ Generated stubs OK')" 2>/dev/null; then
    :
else
    echo "  ✗ Generated stubs check failed — verify generated/ directory is present"
fi

# ── 4. Verify rpwc_msgs ───────────────────────────────────────
echo ""
echo "[4/6] Checking rpwc_msgs (ROS custom messages)..."

RPWC_OK=false
for ws in ~/exsensia/tmp_ws ~/exsensia/catkin_ws ~/exsensia ~/catkin_ws; do
    if [ -f "$ws/devel/setup.bash" ]; then
        source "$ws/devel/setup.bash" 2>/dev/null || true
        if python3 -c "from rpwc_msgs.msg import executionCommandsAction" 2>/dev/null; then
            echo "  ✓ rpwc_msgs found in $ws"
            RPWC_OK=true
            break
        fi
    fi
done

if [ "$RPWC_OK" = false ]; then
    echo "  ✗ rpwc_msgs NOT found."
    echo "    The server will start in simulation mode without ROS."
    echo "    To enable real robot control, build rpwc_msgs in your catkin workspace:"
    echo "      cd ~/catkin_ws && catkin_make && source devel/setup.bash"
fi

# ── 5. Create log directory ───────────────────────────────────
echo ""
echo "[5/6] Creating log directory..."
mkdir -p "$INSTALL_DIR/logs"
echo "  ✓ logs/ directory ready"

# ── 6. Install systemd service ────────────────────────────────
echo ""
echo "[6/6] Installing systemd service..."

SERVICE_FILE="$INSTALL_DIR/mobile_sila2.service"
SYSTEMD_DEST="/etc/systemd/system/mobile_sila2.service"

# Substitute paths/user into service template
sed "s|__INSTALL_DIR__|$INSTALL_DIR|g; s|__SERVICE_USER__|$SERVICE_USER|g" \
    "$SERVICE_FILE" > /tmp/mobile_sila2_resolved.service

if sudo cp /tmp/mobile_sila2_resolved.service "$SYSTEMD_DEST" 2>/dev/null; then
    sudo systemctl daemon-reload
    sudo systemctl enable mobile_sila2
    echo "  ✓ Systemd service installed and enabled"
    echo "    Start: sudo systemctl start mobile_sila2"
    echo "    Logs:  sudo journalctl -u mobile_sila2 -f"
else
    echo "  ✗ Could not install systemd service (need sudo)."
    echo "    Run manually:  sudo cp mobile_sila2.service /etc/systemd/system/"
    echo "                   sudo systemctl daemon-reload && sudo systemctl enable mobile_sila2"
fi

rm -f /tmp/mobile_sila2_resolved.service

echo ""
echo "============================================================"
echo " Installation complete."
echo ""
echo " Quick start:"
echo "   ./run_server.sh                  # manual (foreground)"
echo "   sudo systemctl start mobile_sila2  # via systemd"
echo ""
echo " Test (no ROS):"
echo "   python3 main.py --simulate"
echo ""
echo " LabOS config (lab_config.yaml on Windows PC):"
echo "   servers:"
echo "     mobile:"
echo "       host: $(hostname -I | awk '{print $1}')   # this PC's WiFi IP"
echo "       port: 50053"
echo "============================================================"
