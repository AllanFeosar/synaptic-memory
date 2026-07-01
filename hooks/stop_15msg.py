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
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from typed.budget import record_session  # noqa: E402
from typed.write import write_session_summary  # noqa: E402


from hooks._common import detect_scope  # noqa: E402


def _read_session_tokens() -> tuple[int, int, int]:
    """Read token usage from the current session's Claude Code conversation log.

    Finds the most recently modified JSONL in ~/.claude/projects/*/
    (that file IS the current session's log — Stop fires before Claude closes it).
    Returns (tokens_in, tokens_out, cache_read_tokens).
    All three are real API-reported counts, same source as the "Claude Code Usage" extension.
    """
    try:
        projects_dir = Path.home() / ".claude" / "projects"
        if not projects_dir.exists():
            return 0, 0, 0

        # Session ID env var lets us find the exact file; fall back to most-recent.
        session_id = (
            os.environ.get("CLAUDE_SESSION_ID")
            or os.environ.get("CLAUDE_CONVERSATION_ID")
        )
        candidates = list(projects_dir.glob("*/*.jsonl"))
        if not candidates:
            return 0, 0, 0

        if session_id:
            exact = [p for p in candidates if p.stem == session_id]
            log_file = exact[0] if exact else max(candidates, key=lambda p: p.stat().st_mtime)
        else:
            log_file = max(candidates, key=lambda p: p.stat().st_mtime)

        # Safety: ignore logs older than 4 hours (can't be the current session)
        if time.time() - log_file.stat().st_mtime > 14400:
            return 0, 0, 0

        tokens_in = tokens_out = cache_read = 0
        for line in log_file.open(encoding="utf-8", errors="replace"):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                usage = (r.get("message") or {}).get("usage") or r.get("usage") or {}
                tokens_in += (
                    usage.get("input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                )
                tokens_out += usage.get("output_tokens", 0)
                cache_read += usage.get("cache_read_input_tokens", 0)
            except (json.JSONDecodeError, TypeError, KeyError):
                continue

        return tokens_in, tokens_out, cache_read
    except Exception:  # noqa: BLE001
        return 0, 0, 0


def passthrough_mempalace() -> None:
    try:
        subprocess.run(
            [sys.executable, "-m", "mempalace", "hook", "run",
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
        tok_in, tok_out, cache_read = _read_session_tokens()
        record_session(
            tokens_in=tok_in,
            tokens_out=tok_out,
            drawers_written=1,
            note=f"cache_read={cache_read}" if cache_read else "",
        )
    except Exception:  # noqa: BLE001
        pass  # budget tracking must never crash the hook

    return 0


if __name__ == "__main__":
    raise SystemExit(main())



