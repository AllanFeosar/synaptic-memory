"""
Trust calibration — the feedback loop that makes Claude smarter over time.

Tracks per-drawer signals:
  - usage_count: incremented every time the drawer is expanded/cited
  - cite_then_correct: incremented when user corrects a Claude response
    that cited this drawer

Drawers with cite_then_correct >= 2 get auto-demoted to confidence=low.
Low-confidence drawers still appear in retrieval but flagged in summaries.

Usage from Claude:
  - On expand_drawer(): bump usage_count (already wired in read.py)
  - On user correction:
        from typed.telemetry import mark_correction
        mark_correction(["drw_xxx", "drw_yyy"])
"""

from __future__ import annotations

from typing import Iterable, Optional

from typed.client import InProcessClient, MempalaceClient
from typed.config import get_config
from typed.types import Confidence, parse_drawer, serialize_drawer


def mark_correction(
    drawer_ids: Iterable[str],
    *,
    client: Optional[MempalaceClient] = None,
) -> dict[str, str]:
    """Increment cite_then_correct on the given drawers.

    Returns a dict[drawer_id -> new_confidence] for visibility.
    """
    client = client or InProcessClient()
    out: dict[str, str] = {}
    for drawer_id in drawer_ids:
        hits = client.search(query=drawer_id, top_k=1)
        if not hits:
            continue
        try:
            d = parse_drawer(hits[0].content)
        except ValueError:
            continue
        if d.drawer_id != drawer_id:
            continue
        d.cite_then_correct += 1
        if d.cite_then_correct >= get_config().telemetry.auto_demote_threshold and d.confidence != Confidence.LOW:
            d.confidence = Confidence.LOW
        client.update_drawer(hits[0].drawer_id, serialize_drawer(d))
        out[drawer_id] = d.confidence.value
    return out


def mark_useful(
    drawer_ids: Iterable[str],
    *,
    client: Optional[MempalaceClient] = None,
) -> int:
    """Bump usage_count without expanding.

    Use when Claude/the user explicitly wants to reinforce a drawer (e.g.,
    'this one is the canonical answer'). Returns count updated.
    """
    client = client or InProcessClient()
    n = 0
    for drawer_id in drawer_ids:
        hits = client.search(query=drawer_id, top_k=1)
        if not hits:
            continue
        try:
            d = parse_drawer(hits[0].content)
        except ValueError:
            continue
        if d.drawer_id != drawer_id:
            continue
        d.usage_count += 1
        client.update_drawer(hits[0].drawer_id, serialize_drawer(d))
        n += 1
    return n


