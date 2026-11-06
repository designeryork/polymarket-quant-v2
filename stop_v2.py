#!/usr/bin/env python3
"""Create the STOP file for paper v2 and let the loop exit after the current round."""
from pathlib import Path

target = Path(__file__).resolve().parent / "STOP"
target.write_text("stop\n", encoding="utf-8")
print(f"[paper-v2] STOP created: {target}")
