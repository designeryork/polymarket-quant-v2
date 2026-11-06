#!/usr/bin/env python3
"""Paper v2 launcher: isolated cwd, paper only.

用法：
    python start_v2.py
"""
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
env = dict(os.environ)
env["PAPER"] = "true"
env["ALLOW_LIVE"] = "false"
cmd = [sys.executable, "quant_engine.py", "--loop", "--interval", "60", "--paper"]
print(f"[paper-v2] cwd={BASE}")
print(f"[paper-v2] {' '.join(cmd)}")
raise SystemExit(subprocess.call(cmd, cwd=str(BASE), env=env))
