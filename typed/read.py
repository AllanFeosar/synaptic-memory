"""
Read protocol — token-disciplined retrieval for Claude Code sessions.

Two entry points:

  inject_session_start(scope) -> str
      Returns a markdown block ready to inject as initial context.
      Token-budgeted: top-3 summaries only, never full drawers.

  expand_drawer(drawer_id) -> str
      Lazy-fetches a single drawer's body when Claude asks for detail.
      Increments usage_count via telemetry.
"""

from __future__ import annotations

from typing import Optional

from typed.client import InProcessClient, MempalaceClient, SearchHit
from typed.types import TypedDrawer, parse_drawer, serialize_drawer


# Hard cap on tokens injected at SessionStart.
# Approximate: 1 token ≈ 4 chars for English. 3 summaries × ~180 chars ≈ 135 tokens.
SESSION_START_TOP_K = 3
SUMMARY_MAX_CHARS = 180


def _hit_to_drawer(hit: SearchHit) -> Optional[TypedDrawer]:
    try:
        return parse_drawer(hit.content)
    except ValueError:
        return None  # untyped legacy drawer; skip


def search_typed(
    query: str,
    scope: Optional[str] = None,
    top_k: int = SESSION_START_TOP_K,
    *,
    client: Optional[MempalaceClient] = None,
    include_low_confidence: bool = True,
) -> list[TypedDrawer]:
    """Semantic search returning only typed drawers.

    Reranks raw search hits by salience to surface the most useful drawers,
    not just the most semantically similar.
    """
    client = client or InProcessClient()
    raw_hits = client.search(query=query, top_k=top_k * 3, wing=scope)
    drawers: list[TypedDrawer] = []
    for h in raw_hits:
        d = _hit_to_drawer(h)
        if d is None:
            continue
        if not include_low_confidence and d.confidence.value == "low":
            continue
        drawers.append(d)

    # Rerank by salience (usage + recency + pin) — better proxy for "useful"
    # than raw cosine similarity alone.
    drawers.sort(key=lambda d: -d.salience())
    return drawers[:top_k]


def inject_session_start(
    scope: str,
    intent_hint: str = "",
    *,
    client: Optional[MempalaceClient] = None,
) -> str:
    """Return a token-disciplined markdown block to prepend to a Claude session.

    Format:

        <!-- typed/sessionstart -->
        ## Memory: top 3 relevant drawers (call expand_drawer(id) for details)
        - [drw_xxx] decision/auth: We chose JWT because ...
        - [drw_yyy] anti-pattern/api: Don't use bare except ...
        - [drw_zzz] recipe/deploy: Standard rollback steps ...
        <!-- /typed/sessionstart -->
    """
    query = f"{scope} {intent_hint}".strip() or scope
    drawers = search_typed(query=query, scope=scope, top_k=SESSION_START_TOP_K, client=client)

    if not drawers:
        return ""  # nothing to inject; don't spend tokens on a header

    lines = [
        "<!-- typed/sessionstart -->",
        f"## Memory ({len(drawers)} drawers — call expand_drawer(id) for full text)",
    ]
    for d in drawers:
        lines.append(f"- {d.summary(max_chars=SUMMARY_MAX_CHARS)}")
    lines.append("<!-- /typed/sessionstart -->")
    return "\n".join(lines)


def expand_drawer(
    drawer_id: str,
    *,
    client: Optional[MempalaceClient] = None,
    bump_usage: bool = True,
) -> Optional[TypedDrawer]:
    """Fetch full drawer by id. Increments usage_count for trust calibration."""
    client = client or InProcessClient()
    # mempalace doesn't have a direct get-by-id MCP tool exposed in v0; we
    # search by the drawer_id token which appears in our frontmatter.
    hits = client.search(query=drawer_id, top_k=1)
    if not hits:
        return None
    drawer = _hit_to_drawer(hits[0])
    if drawer is None or drawer.drawer_id != drawer_id:
        return None

    if bump_usage:
        drawer.usage_count += 1
        # write back updated frontmatter
        client.update_drawer(hits[0].drawer_id, serialize_drawer(drawer))
    return drawer


# ---------------------------------------------------------------------------
# File-summary lookup (the big token-saver)
# ---------------------------------------------------------------------------

def file_summary(
    file_path: str,
    *,
    scope: Optional[str] = None,
    client: Optional[MempalaceClient] = None,
) -> Optional[str]:
    """Try to find a stored summary for a file before reading the full file.

    Use BEFORE Claude reads any large file. If a summary exists with high
    similarity and is not stale, return it — Claude can avoid loading the
    full file content (the single biggest token saver in the system).
    """
    client = client or InProcessClient()
    query = f"file_summary {file_path}"
    drawers = search_typed(query=query, scope=scope, top_k=1, client=client)
    if not drawers:
        return None
    d = drawers[0]
    if d.stale:
        return None  # don't trust stale summaries
    return d.summary(max_chars=400)


