#!/usr/bin/env python3
"""Launch the local agent bridge service (stdlib only)."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "bridge" / "agent_bridge.py"

if not SCRIPT.is_file():
    print("bridge/agent_bridge.py is missing")
    sys.exit(1)

raise SystemExit(subprocess.call([sys.executable, str(SCRIPT), *sys.argv[1:]]))
