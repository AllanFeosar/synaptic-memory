#!/usr/bin/env python3
"""
Stop hook (fires every ~15 messages per existing mempalace setup).

v2 behavior:
  - Pass through to existing mempalace stop hook (unless --no-mempalace-passthrough)
  - Add ONE typed `summary` drawer per fire (NOT individual decisions)
  - Decisions are written explicitly during the session via write.write_decision()
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from typed.budget import record_session  # noqa: E402
from typed.write import write_session_summary  # noqa: E402


def detect_scope() -> str:
    return (
        os.environ.get("SYNAPTIC_V2_SCOPE")
        or os.environ.get("CLAUDE_PROJECT_SLUG")
        or Path.cwd().name
    )


def passthrough_mempalace() -> None:
    try:
        subprocess.run(
            ["py", "-3.11", "-m", "mempalace", "hook", "run",
             "--hook", "stop", "--harness", "claude-code"],
            timeout=10, check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-mempalace-passthrough", action="store_true",
        help="Skip internal mempalace stop call. Use when settings.json "
             "already chains the mempalace hook separately.",
    )
    args = parser.parse_args()

    if not args.no_mempalace_passthrough:
        passthrough_mempalace()

    summary = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
    if not summary:
        return 0

    try:
        write_session_summary(scope=detect_scope(), body=summary)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[typed] stop hook write failed: {e}\n")
        return 0  # never fail the session

    try:
        record_session(tokens_in=0, tokens_out=0, drawers_written=1)
    except Exception:  # noqa: BLE001
        pass  # budget tracking must never crash the hook

    return 0


if __name__ == "__main__":
    raise SystemExit(main())



