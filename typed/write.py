"""
Typed write API.

Replaces direct `mempalace_add_drawer` calls. Enforces:
  - required type tuple {type, scope, confidence, tier}
  - embedding-based duplicate detection (cosine via mempalace_search)
  - supersedes chain validation

Every drawer that lands in mempalace via this module is a v2 typed drawer.
"""

from __future__ import annotations

from typing import Optional

from typed.client import InProcessClient, MempalaceClient
from typed.types import (
    Confidence,
    DrawerType,
    MemoryTier,
    TypedDrawer,
    parse_drawer,
    serialize_drawer,
)


# Default similarity threshold for "this is a duplicate."
# Tune in production: 0.92 is conservative, 0.85 catches more dupes but risks
# merging genuinely-distinct decisions.
DEFAULT_DUPE_THRESHOLD = 0.92


class DuplicateDrawerError(Exception):
    def __init__(self, existing_id: str, similarity: float):
        super().__init__(
            f"Embedding-similar drawer already exists: {existing_id} (sim={similarity:.3f})"
        )
        self.existing_id = existing_id
        self.similarity = similarity


def _wing_for(scope: str) -> str:
    """mempalace 'wing' = project. We map scope == project."""
    return scope.lower().strip() or "global"


def _room_for(drawer_type: DrawerType) -> str:
    return drawer_type.value


def write_drawer(
    *,
    type: DrawerType | str,
    scope: str,
    confidence: Confidence | str,
    body: str,
    tier: MemoryTier | str = MemoryTier.LONG_TERM,
    supersedes: Optional[str] = None,
    pinned: bool = False,
    client: Optional[MempalaceClient] = None,
    dupe_threshold: float = DEFAULT_DUPE_THRESHOLD,
    skip_dupe_check: bool = False,
) -> TypedDrawer:
    """Persist a typed drawer.

    Raises:
        ValueError on missing/invalid fields.
        DuplicateDrawerError if an embedding-similar drawer exists.
    """
    client = client or InProcessClient()

    drawer = TypedDrawer.new(
        type=type,
        scope=scope,
        confidence=confidence,
        body=body,
        tier=tier,
        supersedes=supersedes,
        pinned=pinned,
    )

    if not skip_dupe_check:
        hits = client.search(query=drawer.body, top_k=1, wing=_wing_for(scope))
        if hits and hits[0].score >= dupe_threshold:
            try:
                existing = parse_drawer(hits[0].content)
                raise DuplicateDrawerError(existing.drawer_id, hits[0].score)
            except ValueError:
                raise DuplicateDrawerError(hits[0].drawer_id, hits[0].score)

    serialized = serialize_drawer(drawer)
    client.add_drawer(
        wing=_wing_for(scope),
        room=_room_for(drawer.type),
        content=serialized,
    )
    return drawer


# ---------------------------------------------------------------------------
# Convenience helpers — sensible tier defaults per drawer type
# ---------------------------------------------------------------------------

def write_decision(
    scope: str,
    body: str,
    *,
    confidence: Confidence | str = Confidence.HIGH,
    tier: MemoryTier | str = MemoryTier.LONG_TERM,
    supersedes: Optional[str] = None,
    client: Optional[MempalaceClient] = None,
) -> TypedDrawer:
    return write_drawer(
        type=DrawerType.DECISION,
        scope=scope,
        confidence=confidence,
        body=body,
        tier=tier,
        supersedes=supersedes,
        client=client,
    )


def write_anti_pattern(
    scope: str,
    body: str,
    *,
    confidence: Confidence | str = Confidence.HIGH,
    client: Optional[MempalaceClient] = None,
) -> TypedDrawer:
    # Anti-patterns are permanent — a mistake that burned you once should
    # never quietly expire and surprise you again.
    return write_drawer(
        type=DrawerType.ANTI_PATTERN,
        scope=scope,
        confidence=confidence,
        body=body,
        tier=MemoryTier.PERMANENT,
        client=client,
        pinned=True,
    )


def write_recipe(
    scope: str,
    body: str,
    *,
    client: Optional[MempalaceClient] = None,
) -> TypedDrawer:
    # Recipes are procedural knowledge — permanent so they're always available.
    return write_drawer(
        type=DrawerType.RECIPE,
        scope=scope,
        confidence=Confidence.HIGH,
        body=body,
        tier=MemoryTier.PERMANENT,
        client=client,
    )


def write_postmortem(
    scope: str,
    body: str,
    *,
    confidence: Confidence | str = Confidence.HIGH,
    client: Optional[MempalaceClient] = None,
) -> TypedDrawer:
    return write_drawer(
        type=DrawerType.POSTMORTEM,
        scope=scope,
        confidence=confidence,
        body=body,
        tier=MemoryTier.LONG_TERM,
        client=client,
    )


def write_session_summary(
    scope: str,
    body: str,
    *,
    client: Optional[MempalaceClient] = None,
) -> TypedDrawer:
    """For Stop@15msgs hook — short-term tier, no dupe check."""
    return write_drawer(
        type=DrawerType.SUMMARY,
        scope=scope,
        confidence=Confidence.MEDIUM,
        body=body,
        tier=MemoryTier.SHORT_TERM,
        client=client,
        skip_dupe_check=True,  # summaries are time-tagged; dedupe is noise
    )
