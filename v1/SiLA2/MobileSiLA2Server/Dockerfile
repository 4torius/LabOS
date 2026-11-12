# MobileSiLA2Server container — reuses the already-working ROS Noetic +
# rpwc_msgs environment from exsensia:bicocca_release so we don't have to
# fight a Python 3.8 (ROS Noetic) vs Python 3.12 (host) mismatch. Adds only
# the Python deps the SiLA2 gRPC server itself needs; no GPU/X11/HALCON/
# privileged access required for this container (that stuff stays in the
# ros-rpwc/exsocker container).
FROM exsensia:bicocca_release

RUN pip3 install --no-cache-dir     'sila2[codegen]>=0.13.0'     'grpcio>=1.60.0'     'protobuf>=4.25.0'     'pyyaml>=6.0.1'     'zeroconf>=0.130.0'

WORKDIR /app
