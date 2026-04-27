#!/usr/bin/env python3
"""
================================================================================
                          🧬 LabOS LAUNCHER 🧬
                    One-Click Lab Automation Startup
================================================================================

The ONLY file you need to start the system!

Usage:
    python launcher.py              # Interactive menu
    python launcher.py --all        # Start everything
    python launcher.py --servers    # Start SiLA2 servers only
    python launcher.py --webapp     # Start WebApp only
"""

import os
import sys
import socket
import subprocess
import time
import argparse
from pathlib import Path
from typing import Dict, List, Optional

#                              SETUP

BASE_DIR = Path(__file__).parent.absolute()
os.chdir(BASE_DIR)

# Add to path
sys.path.insert(0, str(BASE_DIR))

from src.config_schema import load_lab_config

# Resolve venv Python: prefer LabOS-root venv, fallback to current interpreter
_venv_python = BASE_DIR.parent / ".venv" / "Scripts" / "python.exe"
PYTHON_EXE = str(_venv_python) if _venv_python.exists() else sys.executable

#                              COLORS

class C:
    """ANSI color codes for terminal output."""
    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    
    BG_BLUE = '\033[44m'
    BG_GREEN = '\033[42m'

def ok(msg): print(f"  {C.GREEN}✓{C.RESET} {msg}")
def err(msg): print(f"  {C.RED}✗{C.RESET} {msg}")
def info(msg): print(f"  {C.CYAN}ℹ{C.RESET} {msg}")
def warn(msg): print(f"  {C.YELLOW}⚠{C.RESET} {msg}")

#                              CONFIG

def load_config() -> dict:
    """Load and validate configuration from lab_config.yaml."""
    config_path = BASE_DIR / "lab_config.yaml"
    
    try:
        config, validation = load_lab_config(config_path, apply_defaults=True, strict=True)
        for warning in validation.warnings:
            warn(f"Config warning: {warning}")
        return config
    except ValueError as e:
        err(f"Invalid configuration: {e}")
        raise SystemExit(2)
    except Exception as e:
        err(f"Error loading configuration: {e}")
        raise SystemExit(2)

CONFIG = load_config()

#                              BANNER

def print_banner():
    """Print startup banner."""
    print(f"""
{C.CYAN}{C.BOLD}
    ╔══════════════════════════════════════════════════════════════════╗
    ║                                                                  ║
    ║         ██╗      █████╗ ██████╗  ██████╗ ███████╗                ║
    ║         ██║     ██╔══██╗██╔══██╗██╔═══██╗██╔════╝                ║
    ║         ██║     ███████║██████╔╝██║   ██║███████╗                ║
    ║         ██║     ██╔══██║██╔══██╗██║   ██║╚════██║                ║
    ║         ███████╗██║  ██║██████╔╝╚██████╔╝███████║                ║
    ║         ╚══════╝╚═╝  ╚═╝╚═════╝  ╚═════╝ ╚══════╝                ║
    ║                                                                  ║
    ║                 🧬 Laboratory Automation System 🧬               ║
    ║                                                                  ║
    ╚══════════════════════════════════════════════════════════════════╝
{C.RESET}
{C.DIM}                       Laboratory Operating System{C.RESET}
""")

#                              UTILITIES

def check_port(host: str, port: int, timeout: float = 0.5) -> bool:
    """Check if a port is open."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except:
        return False

def get_pid_on_port(port: int) -> Optional[int]:
    """Get the PID of process listening on a port (Windows)."""
    if sys.platform != 'win32':
        return None
    try:
        result = subprocess.run(
            ['netstat', '-ano'],
            capture_output=True,
            text=True,
            timeout=10
        )
        for line in result.stdout.split('\n'):
            if f':{port}' in line and 'LISTENING' in line:
                parts = line.split()
                if parts:
                    return int(parts[-1])
    except:
        pass
    return None

def stop_processes(stop_servers: bool = True, stop_webapp: bool = True):
    """Stop running LabOS processes."""
    print(f"\n{C.BOLD}🛑 Stopping running processes...{C.RESET}\n")
    
    pids_to_kill = set()
    
    # Collect PIDs from server ports
    if stop_servers:
        servers = get_servers_config()
        for key, srv in servers.items():
            if srv.get('enabled', True):
                port = srv.get('port', 0)
                if port:
                    pid = get_pid_on_port(port)
                    if pid and pid > 0:
                        pids_to_kill.add(pid)
                        info(f"{srv['name']}: Found process on :{port} (PID {pid})")
    
    # Collect PID from webapp port
    if stop_webapp:
        webapp_pid = get_pid_on_port(5000)
        if webapp_pid and webapp_pid > 0:
            pids_to_kill.add(webapp_pid)
            info(f"WebApp: Found process on :5000 (PID {webapp_pid})")
    
    # Kill processes
    if pids_to_kill:
        if sys.platform == 'win32':
            pids_str = ' '.join([f'/PID {pid}' for pid in pids_to_kill])
            try:
                subprocess.run(
                    f'taskkill /F {pids_str}',
                    shell=True,
                    capture_output=True,
                    timeout=10
                )
                ok(f"Terminated {len(pids_to_kill)} process(es)")
            except Exception as e:
                warn(f"Error killing processes: {e}")
        else:
            import signal
            for pid in pids_to_kill:
                try:
                    os.kill(pid, signal.SIGTERM)
                except:
                    pass
            ok(f"Sent SIGTERM to {len(pids_to_kill)} process(es)")
        
        # Wait for processes to terminate
        time.sleep(2)
    else:
        info("No running processes found")
    
    print()

def get_servers_config() -> Dict:
    """Get server configurations from lab_config.yaml."""
    return CONFIG.get('servers', {
        'tecan': {'name': 'Tecan M200 Pro', 'port': 50051, 'enabled': True},
        'opentrons': {'name': 'Opentrons Flex', 'port': 50057, 'enabled': True},
        'mobile': {'name': 'Mobile Robot', 'port': 50053, 'enabled': True},
        'manual_station': {'name': 'Manual Station', 'port': 50360, 'enabled': True},
    })

def print_status():
    """Print status of all components."""
    print(f"\n{C.BOLD}╔══════════════════════════════════════════════════════════════════╗{C.RESET}")
    print(f"{C.BOLD}║                       SYSTEM STATUS                              ║{C.RESET}")
    print(f"{C.BOLD}╚══════════════════════════════════════════════════════════════════╝{C.RESET}\n")
    
    servers = get_servers_config()
    
    print(f"  {C.BOLD}SiLA2 Servers:{C.RESET}")
    for key, srv in servers.items():
        if not srv.get('enabled', True):
            print(f"    {C.DIM}○ {srv['name']:25} [DISABLED]{C.RESET}")
            continue
            
        port = srv.get('port', 0)
        host = srv.get('host', 'localhost')
        is_up = check_port(host, port) if port else False
        
        if is_up:
            print(f"    {C.GREEN}●{C.RESET} {srv['name']:25} {C.GREEN}[ONLINE]{C.RESET}  :{port}")
        else:
            print(f"    {C.RED}○{C.RESET} {srv['name']:25} {C.DIM}[OFFLINE]{C.RESET} :{port}")
    
    # WebApp
    print(f"\n  {C.BOLD}WebApp:{C.RESET}")
    if check_port('localhost', 5000):
        print(f"    {C.GREEN}●{C.RESET} WebApp Dashboard           {C.GREEN}[ONLINE]{C.RESET}  http://localhost:5000")
    else:
        print(f"    {C.RED}○{C.RESET} WebApp Dashboard           {C.DIM}[OFFLINE]{C.RESET}")
    
    print()

#                              STUBS

def ensure_stubs():
    """Regenerate gRPC stubs if grpc_tools is available."""
    try:
        import grpc_tools  # noqa: F401
        from regen_stubs import regenerate
        info("Regenerating gRPC stubs...")
        ok_result = regenerate()
        if ok_result:
            ok("gRPC stubs up to date")
        else:
            warn("Some stubs failed to generate — see above")
    except ImportError:
        warn("grpc_tools not installed, skipping stub generation")
    except Exception as e:
        warn(f"Stub generation skipped: {e}")

#                              LAUNCHERS

def start_servers(wait: bool = True, restart: bool = True):
    """Start all enabled SiLA2 servers."""
    
    # Stop existing servers first if restart=True
    if restart:
        stop_processes(stop_servers=True, stop_webapp=False)
    
    print(f"\n{C.BOLD}🚀 Starting SiLA2 Servers...{C.RESET}\n")
    
    servers = get_servers_config()
    started = []
    
    for key, srv in servers.items():
        if not srv.get('enabled', True):
            info(f"{srv['name']}: Disabled in config")
            continue
        
        port = srv.get('port', 0)
        host = srv.get('host', 'localhost')
        is_remote = bool(srv.get('remote', False)) or host not in ('localhost', '127.0.0.1', '')

        if is_remote:
            if port and check_port(host, port):
                ok(f"{srv['name']}: Remote server reachable at {host}:{port}")
            else:
                warn(f"{srv['name']}: Remote server not reachable at {host}:{port}")
            continue
        
        # Check if already running
        if check_port(host, port):
            ok(f"{srv['name']}: Already running on :{port}")
            continue
        
        # Get directory and command
        srv_dir = BASE_DIR / srv.get('directory', f"SiLA2/{key.capitalize()}SiLA2Server")
        
        if not srv_dir.exists():
            err(f"{srv['name']}: Directory not found ({srv_dir})")
            continue
        
        # Get command based on OS
        if sys.platform == 'win32':
            cmd = srv.get('command_windows', ['python', 'main.py'])
        else:
            cmd = srv.get('command_unix', ['python', 'main.py'])

        if not cmd:
            warn(f"{srv['name']}: No startup command configured, skipping")
            continue
        
        try:
            info(f"Starting {srv['name']}...")
            
            if sys.platform == 'win32':
                # Windows: open in new terminal
                cmd_str = " ".join(cmd)
                
                if cmd[0] == 'dotnet':
                    # For dotnet commands, use PowerShell with explicit PATH
                    dotnet_path = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'dotnet')
                    ps_cmd = f'$env:PATH = \\"{dotnet_path};$env:PATH\\"; {cmd_str}'
                    subprocess.Popen(
                        f'start "{srv["name"]}" powershell -NoExit -Command "{ps_cmd}"',
                        cwd=srv_dir,
                        shell=True
                    )
                else:
                    # For Python commands, use the venv interpreter explicitly
                    if cmd[0] in ('python', 'python3'):
                        cmd[0] = PYTHON_EXE
                    cmd_str = " ".join(f'"{c}"' if ' ' in c else c for c in cmd)
                    subprocess.Popen(
                        f'start "{srv["name"]}" cmd /k {cmd_str}',
                        cwd=srv_dir,
                        shell=True
                    )
            else:
                # Unix: run in background
                subprocess.Popen(
                    cmd,
                    cwd=srv_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
            
            started.append((key, srv['name'], port))
            
        except Exception as e:
            err(f"{srv['name']}: Failed to start - {e}")
    
    # Wait for servers to be ready
    if wait and started:
        print(f"\n  {C.DIM}Waiting for servers to start...{C.RESET}")
        time.sleep(3)
        
        for key, name, port in started:
            if check_port('localhost', port):
                ok(f"{name}: Ready on :{port}")
            else:
                warn(f"{name}: Still starting (check terminal window)")
    
    print()

def start_webapp(restart: bool = True):
    """Start the WebApp."""
    
    # Stop existing webapp first if restart=True
    if restart and check_port('localhost', 5000):
        stop_processes(stop_servers=False, stop_webapp=True)
    
    print(f"\n{C.BOLD}🌐 Starting WebApp...{C.RESET}\n")
    
    webapp_script = BASE_DIR / "start_webapp.py"
    
    if not webapp_script.exists():
        err("start_webapp.py not found")
        return
    
    try:
        if sys.platform == 'win32':
            subprocess.Popen(
                f'start "LabOS WebApp" cmd /k "{PYTHON_EXE}" start_webapp.py --port 5000',
                cwd=BASE_DIR,
                shell=True
            )
        else:
            subprocess.Popen(
                [PYTHON_EXE, str(webapp_script), '--port', '5000'],
                cwd=BASE_DIR,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
        
        time.sleep(2)
        
        if check_port('localhost', 5000):
            ok("WebApp ready at http://localhost:5000")
            info("Opening browser...")
            
            # Open browser
            import webbrowser
            webbrowser.open('http://localhost:5000')
        else:
            info("WebApp starting... Check the terminal window")
            
    except Exception as e:
        err(f"Failed to start WebApp: {e}")
    
    print()

def start_all():
    """Start everything: servers, webapp, and open browser."""
    print(f"\n{C.BG_GREEN}{C.BOLD} 🚀 STARTING COMPLETE SYSTEM {C.RESET}\n")
    
    # 0. Stop all running processes first
    stop_processes(stop_servers=True, stop_webapp=True)
    
    # 1. Start servers (restart=False since we already stopped)
    start_servers(wait=True, restart=False)
    
    # 2. Start WebApp (restart=False since we already stopped)
    start_webapp(restart=False)
    
    print(f"\n{C.GREEN}{C.BOLD}✅ System Started!{C.RESET}")
    print(f"\n  WebApp: {C.CYAN}http://localhost:5000{C.RESET}")
    print()

#                              MENU

def show_menu():
    """Show interactive menu."""
    print_banner()
    print_status()
    
    while True:
        print(f"{C.BOLD}╔══════════════════════════════════════════════════════════════════╗{C.RESET}")
        print(f"{C.BOLD}║                         MAIN MENU                                ║{C.RESET}")
        print(f"{C.BOLD}╚══════════════════════════════════════════════════════════════════╝{C.RESET}")
        print()
        print(f"    {C.CYAN}1{C.RESET}  🚀 {C.GREEN}START ALL{C.RESET} (Servers + WebApp + Browser)")
        print()
        print(f"    {C.CYAN}2{C.RESET}  📡 Start SiLA2 Servers")
        print(f"    {C.CYAN}3{C.RESET}  🌐 Start WebApp")
        print()
        print(f"    {C.CYAN}4{C.RESET}  📊 Refresh Status")
        print(f"    {C.CYAN}5{C.RESET}  🔧 Open Configuration (lab_config.yaml)")
        print(f"    {C.CYAN}6{C.RESET}  🛑 {C.RED}Stop All Processes{C.RESET}")
        print()
        print(f"    {C.CYAN}Q{C.RESET}  Exit")
        print()
        
        try:
            choice = input(f"  {C.BOLD}Select option:{C.RESET} ").strip().lower()
            print()
            
            if choice == '1':
                start_all()
            elif choice == '2':
                start_servers()
            elif choice == '3':
                start_webapp()
            elif choice == '4':
                print_status()
            elif choice == '5':
                open_config()
            elif choice == '6':
                stop_processes(stop_servers=True, stop_webapp=True)
                print_status()
            elif choice in ('q', 'quit', 'exit', ''):
                print(f"  {C.DIM}Goodbye! 👋{C.RESET}\n")
                break
            else:
                warn("Invalid option. Please try again.")
                
        except KeyboardInterrupt:
            print(f"\n\n  {C.DIM}Goodbye! 👋{C.RESET}\n")
            break
        except Exception as e:
            err(f"Error: {e}")

def open_config():
    """Open the configuration file."""
    config_path = BASE_DIR / "lab_config.yaml"
    
    try:
        if sys.platform == 'win32':
            os.startfile(config_path)
        elif sys.platform == 'darwin':
            subprocess.run(['open', str(config_path)])
        else:
            subprocess.run(['xdg-open', str(config_path)])
        ok(f"Opened {config_path}")
    except Exception as e:
        err(f"Could not open config: {e}")
        info(f"Path: {config_path}")

#                              MAIN

def main():
    parser = argparse.ArgumentParser(
        description="🧬 LabOS Launcher - One-Click Lab Automation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python launcher.py              Interactive menu
  python launcher.py --all        Start everything (recommended)
  python launcher.py --servers    Start SiLA2 servers only
  python launcher.py --webapp     Start WebApp only
  python launcher.py --status     Show system status
        """
    )
    
    parser.add_argument('--all', '-a', action='store_true', 
                        help='Start everything (servers + webapp)')
    parser.add_argument('--servers', '-s', action='store_true',
                        help='Start SiLA2 servers only')
    parser.add_argument('--webapp', '-w', action='store_true',
                        help='Start WebApp only')
    parser.add_argument('--status', action='store_true',
                        help='Show system status')
    parser.add_argument('--stop', action='store_true',
                        help='Stop all running processes')
    
    args = parser.parse_args()

    if not args.status and not args.stop:
        ensure_stubs()

    # Handle command line arguments
    if args.all:
        print_banner()
        start_all()
    elif args.servers:
        print_banner()
        start_servers()
    elif args.webapp:
        print_banner()
        start_webapp()
    elif args.status:
        print_banner()
        print_status()
    elif args.stop:
        print_banner()
        stop_processes(stop_servers=True, stop_webapp=True)
        print_status()
    else:
        # Show interactive menu
        show_menu()

if __name__ == "__main__":
    main()
