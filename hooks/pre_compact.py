#!/usr/bin/env python3
"""
PreCompact hook — READ ONLY in v2 (per the design spec).

Pre-compaction is a bad time to write: working memory is shifting, the model is
losing context, dedupe checks may misfire. Instead, we use this moment to surface
the most-relevant pinned drawers so the post-compact context retains them.

Output: stdout markdown block of pinned drawers in current scope.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from typed.read import search_typed  # noqa: E402


def detect_scope() -> str:
    return (
        os.environ.get("SYNAPTIC_V2_SCOPE")
        or os.environ.get("CLAUDE_PROJECT_SLUG")
        or Path.cwd().name
    )


def main() -> int:
    scope = detect_scope()
    try:
        drawers = search_typed(query=f"{scope} pinned", scope=scope, top_k=5)
        pinned = [d for d in drawers if d.pinned]
        if not pinned:
            return 0
        sys.stdout.write("<!-- typed/precompact -->\n")
        sys.stdout.write("## Pinned memory (preserved through compaction)\n")
        for d in pinned:
            sys.stdout.write(f"- {d.summary(max_chars=180)}\n")
        sys.stdout.write("<!-- /typed/precompact -->\n")
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[typed] precompact failed: {e}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



