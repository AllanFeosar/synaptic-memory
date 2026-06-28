#!/usr/bin/env python3
"""
PostToolUse hook — fires after every Edit or Write tool call.

After Claude edits a file, this hook:
  1. Extracts the edited file stem from tool_input.file_path
  2. Queries graphify for structural neighbors (files that import/call this one)
  3. Surfaces them as a reminder: "these linked files may also need updating"
  4. Also searches mempalace for any anti-patterns or decisions about those neighbors

This is additive to the existing "call mempalace_add_drawer" reminder already
in settings.json — that reminder stays in place via the existing PostToolUse hook.

Outputs nothing (exit 0) if:
  - graphify-out/graph.json not found in cwd
  - The edited file has no graphify neighbors
  - No relevant memories found

Wire-up in ~/.claude/settings.json:
  "PostToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command",
    "command": "python3.11 \"<synaptic-memory-path>/hooks/post_tool_edit.py\""}]}]
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
def _max_neighbors():
    from typed.config import get_config
    return get_config().hooks.post_tool_edit_max_neighbors

def _max_drawers():
    from typed.config import get_config
    return get_config().hooks.post_tool_edit_max_drawers


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

    stem = Path(file_path).stem
    if not stem or stem.startswith("."):
        return 0

    graphify_client = LocalGraphifyClient.from_cwd()
    if graphify_client is None:
        return 0

    # Find structural neighbors of the edited file
    neighbors = graphify_client.get_neighbors(stem, limit=_max_neighbors())
    if not neighbors:
        # Try querying by stem in case label differs from stem
        hits = graphify_client.query(stem, limit=1)
        if hits:
            neighbors = graphify_client.get_neighbors(hits[0], limit=_max_neighbors())

    scope = _detect_scope()
    lines: list[str] = []

    # Surface graphify neighbors as "may also need updating"
    if neighbors:
        neighbor_names = ", ".join(f"`{n}`" for n in neighbors[:_max_neighbors()])
        lines.append(f"Graphify: `{Path(file_path).name}` is linked to {neighbor_names} — check if they need updating.")

    # Surface any relevant memories via spreading activation on the edited file
    try:
        drawers = spreading_activation_search(
            query=f"{stem} {scope}",
            scope=scope,
            top_k=_max_drawers(),
            depth=1,
            graphify_client=graphify_client,
        )
        for d in drawers:
            lines.append(f"Memory: {d.summary(max_chars=_summary_max_chars())}")
    except Exception:  # noqa: BLE001
        pass

    if not lines:
        return 0

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n".join(lines),
        }
    }
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
