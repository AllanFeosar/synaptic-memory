#!/usr/bin/env python3
"""
PreCompact hook — READ ONLY in v2 (per the design spec).

Pre-compaction is a bad time to write: working memory is shifting, the model is
losing context, dedupe checks may misfire. Instead, we use this moment to surface
the most-relevant memories so the post-compact context retains them.

Uses spreading activation + graphify structural hops (same as SessionStart) because
pre-compaction is the highest-stakes retrieval moment in a session — context is being
compressed, so we need the richest possible set of relevant memories injected now.

Output priority:
  1. Pinned drawers in current scope (must survive compaction)
  2. High-salience related drawers surfaced via spreading activation
  Total capped at 5 entries to stay within token budget.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from typed.graphify_client import LocalGraphifyClient  # noqa: E402
from typed.read import spreading_activation_search     # noqa: E402


MAX_INJECT = 5
SUMMARY_MAX_CHARS = 180


def detect_scope() -> str:
    return (
        os.environ.get("SYNAPTIC_V2_SCOPE")
        or os.environ.get("CLAUDE_PROJECT_SLUG")
        or Path.cwd().name
    )


def main() -> int:
    scope = detect_scope()
    try:
        graphify_client = LocalGraphifyClient.from_cwd()

        # Spreading activation surfaces both direct matches and structurally
        # adjacent memories. Request more than we'll show so we can prioritise.
        candidates = spreading_activation_search(
            query=scope,
            scope=scope,
            top_k=MAX_INJECT * 2,
            graphify_client=graphify_client,
        )

        if not candidates:
            return 0

        # Pinned drawers first — they carry the session's load-bearing decisions.
        # Fill remaining slots with highest-salience non-pinned drawers.
        pinned = [d for d in candidates if d.pinned]
        others = [d for d in candidates if not d.pinned]
        ordered = (pinned + others)[:MAX_INJECT]

        sys.stdout.write("<!-- typed/precompact -->\n")
        sys.stdout.write("## Pinned memory (preserved through compaction)\n")
        for d in ordered:
            sys.stdout.write(f"- {d.summary(max_chars=SUMMARY_MAX_CHARS)}\n")
        sys.stdout.write("<!-- /typed/precompact -->\n")

    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[typed] precompact failed: {e}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
