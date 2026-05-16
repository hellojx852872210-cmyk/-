#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    cmd = [
        sys.executable,
        "-m",
        "zhuanzhuan_pricing.automation.agent_runner",
        "post-qc-report",
        "--report-dates",
        "today,yesterday",
    ]
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), text=True)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
