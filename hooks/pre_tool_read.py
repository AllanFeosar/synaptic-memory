#!/usr/bin/env python3
"""
PreToolUse hook — fires before every Read tool call.

When Claude is about to read a file, this hook:
  1. Extracts the file stem from the path (e.g. "auth" from "src/auth.py")
  2. Queries graphify for that file's node + structural neighbors
  3. Searches mempalace for memories about those nodes via spreading activation
  4. Injects relevant memories as additionalContext before Claude reads the file

This is more targeted than SessionStart (scope-level) because it is file-specific
and fires at exactly the right moment — just before Claude inspects a specific file.

Outputs nothing (exit 0) if:
  - graphify-out/graph.json not found in cwd
  - The file has no graphify node (not in the graph)
  - No relevant memories found in mempalace

Wire-up in ~/.claude/settings.json:
  "PreToolUse": [{"matcher": "Read", "hooks": [{"type": "command",
    "command": "python3.11 \"<synaptic-memory-path>/hooks/pre_tool_read.py\""}]}]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from typed.graphify_client import LocalGraphifyClient  # noqa: E402
from typed.read import spreading_activation_search     # noqa: E402


def _summary_max_chars():
    from typed.config import get_config
    return get_config().retrieval.summary_max_chars
def _max_drawers():
    from typed.config import get_config
    return get_config().hooks.pre_tool_read_max_drawers


from hooks._common import detect_scope as _detect_scope  # noqa: E402


def main() -> int:
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return 0

    file_path = event.get("tool_input", {}).get("file_path", "")
    if not file_path:
        return 0

    # Use file stem as the targeted query (e.g. "auth" from "src/auth/session.py")
    stem = Path(file_path).stem
    if not stem or stem.startswith("."):
        return 0

    graphify_client = LocalGraphifyClient.from_cwd()
    if graphify_client is None:
        return 0  # no graph — nothing to contribute

    # Quick check: does this file have any graphify presence?
    g_hits = graphify_client.query(stem, limit=1)
    if not g_hits:
        return 0  # file not in graph — stay silent

    scope = _detect_scope()
    try:
        drawers = spreading_activation_search(
            query=f"{stem} {scope}",
            scope=scope,
            top_k=_max_drawers(),
            depth=1,            # shallow — keep it fast (< 200ms)
            graphify_client=graphify_client,
        )
    except Exception:  # noqa: BLE001
        return 0

    if not drawers:
        return 0

    lines = [f"Memory: {len(drawers)} relevant drawer(s) for `{Path(file_path).name}`"]
    for d in drawers:
        lines.append(f"  - {d.summary(max_chars=_summary_max_chars())}")

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": "\n".join(lines),
        }
    }
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
