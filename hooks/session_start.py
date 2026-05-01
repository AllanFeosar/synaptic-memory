#!/usr/bin/env python3
"""
SessionStart hook — wraps mempalace's existing hook + adds v2 read protocol.

Wire-up (in ~/.claude/settings.json):

    {
      "hooks": {
        "SessionStart": {
          "command": "py -3.11 -m typed.hooks.session_start",
          "args": ["--harness", "claude-code"]
        }
      }
    }

The hook prints a markdown block to stdout, which Claude Code injects as
initial context. Token budget: ~150 tokens for top-3 summaries.

Stays additive — does NOT replace the mempalace hook output. If the existing
hook prints anything, both are concatenated.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Best-effort: when called as a hook, sys.path may not include the repo root.
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[1]  # hooks/session_start.py -> repo
sys.path.insert(0, str(_REPO_ROOT))

from typed.read import inject_session_start  # noqa: E402


def detect_scope() -> str:
    """Best-effort current scope detection.

    Order:
      1. SYNAPTIC_V2_SCOPE env var (explicit override)
      2. CLAUDE_PROJECT_SLUG env var (set by mempalace's project config)
      3. cwd folder name
    """
    for env_var in ("SYNAPTIC_V2_SCOPE", "CLAUDE_PROJECT_SLUG"):
        if v := os.environ.get(env_var):
            return v.strip()
    return Path.cwd().name


def detect_intent() -> str:
    """Lightweight intent hint from environment, if available.

    Claude Code may set CLAUDE_LAST_USER_MESSAGE — use the first 80 chars as
    a query hint. If absent, return empty.
    """
    msg = os.environ.get("CLAUDE_LAST_USER_MESSAGE", "").strip()
    return msg[:80]


def run_mempalace_hook(harness: str) -> str:
    """Invoke the existing mempalace hook so we don't break the current setup."""
    try:
        result = subprocess.run(
            ["py", "-3.11", "-m", "mempalace", "hook", "run",
             "--hook", "session-start", "--harness", harness],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout or ""
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", default="claude-code")
    parser.add_argument("--no-mempalace-passthrough", action="store_true")
    args = parser.parse_args()

    scope = detect_scope()
    intent = detect_intent()

    # 1. Run the existing mempalace hook (don't break current behavior).
    if not args.no_mempalace_passthrough:
        passthrough = run_mempalace_hook(args.harness)
        if passthrough.strip():
            sys.stdout.write(passthrough)
            if not passthrough.endswith("\n"):
                sys.stdout.write("\n")

    # 2. Add v2 typed-drawer summaries on top.
    try:
        block = inject_session_start(scope=scope, intent_hint=intent)
        if block:
            sys.stdout.write(block)
            sys.stdout.write("\n")
    except Exception as e:  # noqa: BLE001 - hooks must never crash the session
        sys.stderr.write(f"[typed] inject_session_start failed: {e}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())



