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

Graphify integration: if graphify-out/graph.json exists in cwd, spreading
activation automatically uses graphify structural edges to surface code-linked
memories. No config needed — it degrades gracefully if graph.json is absent.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from pathlib import Path

# Best-effort: when called as a hook, sys.path may not include the repo root.
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[1]  # hooks/session_start.py -> repo
sys.path.insert(0, str(_REPO_ROOT))

from typed.config import get_config                    # noqa: E402
from typed.graphify_client import LocalGraphifyClient  # noqa: E402
from typed.read import inject_session_start            # noqa: E402


from hooks._common import detect_scope  # noqa: E402


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
            [sys.executable, "-m", "mempalace", "hook", "run",
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

    # 2+3. Auto-detect graphify graph, then add v2 typed-drawer summaries
    # (mempalace + graphify hops if available). Both run on a worker thread
    # with a hard wall-clock budget: cold ONNX embedder / HNSW segment loads
    # can take 30s+ on a spinning disk, and this hook runs concurrently with
    # the MCP server's own cold-start import of the same palace — without a
    # cap, contention between the two can block session start past Claude
    # Code's 60s subprocess-init timeout and kill the whole session.
    result: dict = {}

    def _worker() -> None:
        try:
            graphify_client = LocalGraphifyClient.from_cwd()
            result["block"] = inject_session_start(
                scope=scope,
                intent_hint=intent,
                graphify_client=graphify_client,
            )
        except Exception as e:  # noqa: BLE001 - hooks must never crash the session
            result["error"] = str(e)

    budget = get_config().hooks.session_start_timeout_seconds
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=budget)

    if t.is_alive():
        sys.stderr.write(
            f"[typed] inject_session_start exceeded {budget}s budget — "
            "skipping memory injection for this session\n"
        )
    elif "error" in result:
        sys.stderr.write(f"[typed] inject_session_start failed: {result['error']}\n")
    elif result.get("block"):
        sys.stdout.write(result["block"])
        sys.stdout.write("\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
