#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BicoccaLab WebApp Launcher
==========================

Avvia la WebApp integrata di BicoccaLab.

Usage:
    python start_webapp.py [--port PORT] [--host HOST] [--reload]

Author: BicoccaLab Team
"""

import sys
import os
import argparse
from pathlib import Path

# Add project root to path
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

def main():
    parser = argparse.ArgumentParser(description="BicoccaLab WebApp")
    parser.add_argument("--port", type=int, default=5000, help="Port number (default: 5000)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host address (default: 127.0.0.1)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")
    args = parser.parse_args()
    
    print(r"""
    ╔══════════════════════════════════════════════════════════════╗
    ║                                                              ║
    ║   ██████╗ ██╗ ██████╗ ██████╗  ██████╗ ██████╗ █████╗       ║
    ║   ██╔══██╗██║██╔════╝██╔═══██╗██╔════╝██╔════╝██╔══██╗      ║
    ║   ██████╔╝██║██║     ██║   ██║██║     ██║     ███████║      ║
    ║   ██╔══██╗██║██║     ██║   ██║██║     ██║     ██╔══██║      ║
    ║   ██████╔╝██║╚██████╗╚██████╔╝╚██████╗╚██████╗██║  ██║      ║
    ║   ╚═════╝ ╚═╝ ╚═════╝ ╚═════╝  ╚═════╝ ╚═════╝╚═╝  ╚═╝      ║
    ║                                                              ║
    ║              🧪  LAB AUTOMATION WEBAPP  🧪                   ║
    ║                                                              ║
    ╚══════════════════════════════════════════════════════════════╝
    """)
    
    print(f"  🌐 Starting WebApp on http://{args.host}:{args.port}")
    print(f"  📂 Base directory: {BASE_DIR}")
    print()
    
    # Check dependencies
    try:
        import fastapi
        import uvicorn
        import jinja2
        print("  ✅ Dependencies OK")
    except ImportError as e:
        print(f"  ❌ Missing dependency: {e}")
        print("     Run: pip install fastapi uvicorn jinja2 python-multipart")
        sys.exit(1)
    
    # Check webapp module
    webapp_path = BASE_DIR / "webapp" / "app.py"
    if not webapp_path.exists():
        print(f"  ❌ WebApp not found at {webapp_path}")
        sys.exit(1)
    
    print(f"  ✅ WebApp module found")
    
    # Check templates
    templates_dir = BASE_DIR / "webapp" / "templates"
    if templates_dir.exists():
        templates = list(templates_dir.glob("*.html"))
        print(f"  ✅ Templates: {len(templates)} found")
    else:
        print("  ⚠️  Templates directory not found")
    
    print()
    print("  " + "═" * 50)
    print(f"  🚀 Open browser: http://{args.host}:{args.port}")
    print("  " + "═" * 50)
    print()
    print("  Press Ctrl+C to stop the server")
    print()
    
    # Start server
    try:
        uvicorn.run(
            "webapp.app:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_level="info",
            ws="websockets"  # Explicitly set WebSocket implementation
        )
    except Exception as e:
        print(f"\n  ❌ Error starting server: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
