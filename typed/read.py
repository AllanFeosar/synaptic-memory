"""
Read protocol — token-disciplined retrieval for Claude Code sessions.

Three entry points:

  inject_session_start(scope) -> str
      Returns a markdown block ready to inject as initial context.
      Uses spreading activation — surfaces directly relevant drawers
      AND semantically adjacent ones you didn't explicitly ask for.
      Token-budgeted: top-3 summaries only, never full drawers.

  expand_drawer(drawer_id) -> str
      Lazy-fetches a single drawer's body when Claude asks for detail.
      Increments usage_count via telemetry.

  spreading_activation_search(query) -> list[TypedDrawer]
      Graph-diffusion retrieval: starts from semantic seed hits, ripples
      outward through related drawers with hop_decay per step, combines
      activation strength with salience for final ranking.

      When graphify_client is provided, each hop also extracts file/code
      references from drawer bodies, queries graphify for structural
      neighbors, then searches mempalace for memories about those nodes.
      This lets structural code edges (imports, calls) guide memory retrieval
      without polluting SessionStart with raw graph data.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from typed.client import InProcessClient, MempalaceClient, SearchHit
from typed.types import MemoryTier, TypedDrawer, parse_drawer, serialize_drawer

if TYPE_CHECKING:
    from typed.graphify_client import GraphifyClient


# Hard cap on tokens injected at SessionStart.
# Approximate: 1 token ≈ 4 chars for English. 3 summaries × ~180 chars ≈ 135 tokens.
SESSION_START_TOP_K = 3
SUMMARY_MAX_CHARS = 180

# Extra discount applied to graphify-sourced hops vs pure semantic hops.
# Structural neighbors are less certain than direct embedding similarity.
_GRAPHIFY_HOP_DISCOUNT = 0.6

# Regex for file references in drawer bodies, e.g. `auth.py`, `jwt.ts`
_FILE_REF_RE = re.compile(r"`([^`]+\.(?:py|ts|tsx|js|jsx|go|rs|java|cs|md))`")
# CamelCase multi-word identifiers, e.g. JwtRefreshToken, AuthService
_CAMEL_RE = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b")


def _hit_to_drawer(hit: SearchHit) -> Optional[TypedDrawer]:
    try:
        return parse_drawer(hit.content)
    except ValueError:
        return None  # untyped legacy drawer; skip


def _extract_code_refs(body: str) -> list[str]:
    """Extract file stems and CamelCase identifiers from a drawer body.

    Used to query graphify for structurally adjacent code nodes.
    Returns at most 5 unique refs (file stems first, then identifiers).
    """
    refs: list[str] = []
    for m in _FILE_REF_RE.finditer(body):
        refs.append(Path(m.group(1)).stem)
    refs.extend(_CAMEL_RE.findall(body))
    # Dedupe preserving order, cap to avoid search explosion
    seen: set[str] = set()
    deduped: list[str] = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            deduped.append(r)
    return deduped[:5]


def search_typed(
    query: str,
    scope: Optional[str] = None,
    top_k: int = SESSION_START_TOP_K,
    *,
    client: Optional[MempalaceClient] = None,
    include_low_confidence: bool = True,
    tier_filter: Optional[MemoryTier] = None,
) -> list[TypedDrawer]:
    """Semantic search returning only typed drawers, reranked by salience.

    tier_filter: if set, only return drawers of that tier.
    """
    client = client or InProcessClient()
    raw_hits = client.search(query=query, top_k=top_k * 3, wing=scope.lower() if scope else None)
    drawers: list[TypedDrawer] = []
    for h in raw_hits:
        d = _hit_to_drawer(h)
        if d is None:
            continue
        if not include_low_confidence and d.confidence.value == "low":
            continue
        if tier_filter is not None and d.tier != tier_filter:
            continue
        drawers.append(d)

    drawers.sort(key=lambda d: -d.salience())
    return drawers[:top_k]


def spreading_activation_search(
    query: str,
    scope: Optional[str] = None,
    top_k: int = SESSION_START_TOP_K,
    depth: int = 2,
    hop_decay: float = 0.5,
    *,
    client: Optional[MempalaceClient] = None,
    graphify_client: Optional["GraphifyClient"] = None,
) -> list[TypedDrawer]:
    """Spreading activation retrieval with optional graphify structural hops.

    Phase 1 — Mempalace semantic hops (always active):
      Starts with semantic seed hits, ripples outward through related drawers
      at each hop with activation decayed by hop_decay per step.

    Phase 2 — Graphify structural hops (when graphify_client is provided):
      After each mempalace hop, extracts file/code references from drawer
      bodies, queries graphify for structurally adjacent nodes (imports,
      calls, etc.), then searches mempalace for memories about those nodes.
      Graphify-sourced hops receive an extra _GRAPHIFY_HOP_DISCOUNT (0.6×)
      because structural adjacency is weaker evidence than embedding similarity.

    Together: surfaces context you didn't explicitly ask for.
      - A query about "auth" activates a JWT decision (mempalace hop)
      - That drawer mentions `jwt.py` → graphify finds JwtService neighbor
      - Mempalace is searched for memories about JwtService (graphify hop)
      - A postmortem about token expiry edge cases surfaces

    Args:
        query:           The search query (typically scope + intent hint).
        scope:           Wing to search within. None = global search.
        top_k:           Number of drawers to return.
        depth:           Number of hops to ripple. 2 is a good default.
        hop_decay:       Activation multiplier per hop (0.5 = halved each hop).
        graphify_client: Optional graphify backend. When None, graphify hops
                         are skipped and behavior is identical to before.
    """
    client = client or InProcessClient()
    wing = scope.lower() if scope else None

    # activation map: drawer_id -> (drawer, activation_score)
    activation: dict[str, tuple[TypedDrawer, float]] = {}

    # Seed: initial semantic search
    seeds = client.search(query=query, top_k=top_k * 2, wing=wing)
    frontier: list[tuple[TypedDrawer, float]] = []
    for hit in seeds:
        d = _hit_to_drawer(hit)
        if d is None:
            continue
        score = hit.score
        if d.drawer_id not in activation:
            activation[d.drawer_id] = (d, score)
            frontier.append((d, score))

    # Ripple outward hop by hop
    for hop in range(1, depth + 1):
        decay = hop_decay ** hop
        next_frontier: list[tuple[TypedDrawer, float]] = []

        for seed_drawer, seed_score in frontier:
            # --- Phase 1: mempalace semantic hop ---
            neighbor_query = seed_drawer.body[:300]
            neighbors = client.search(query=neighbor_query, top_k=3, wing=wing)
            for hit in neighbors:
                d = _hit_to_drawer(hit)
                if d is None or d.drawer_id in activation:
                    continue
                score = hit.score * decay
                activation[d.drawer_id] = (d, score)
                next_frontier.append((d, score))

            # --- Phase 2: graphify structural hop (optional) ---
            if graphify_client is not None:
                code_refs = _extract_code_refs(seed_drawer.body)
                for ref in code_refs[:3]:  # cap to avoid search explosion
                    g_labels = graphify_client.query(ref, limit=3)
                    for label in g_labels:
                        # Search mempalace for memories mentioning this code node
                        g_hits = client.search(query=label, top_k=2, wing=wing)
                        for hit in g_hits:
                            d = _hit_to_drawer(hit)
                            if d is None or d.drawer_id in activation:
                                continue
                            # Structural hops discounted vs semantic hops
                            score = hit.score * decay * _GRAPHIFY_HOP_DISCOUNT
                            activation[d.drawer_id] = (d, score)
                            next_frontier.append((d, score))

        frontier = next_frontier
        if not frontier:
            break

    # Rank by activation * salience — rewards both relatedness and importance
    ranked = sorted(
        activation.values(),
        key=lambda kv: -(kv[1] + kv[0].salience() * 0.3),
    )
    return [d for d, _ in ranked[:top_k]]


def inject_session_start(
    scope: str,
    intent_hint: str = "",
    *,
    client: Optional[MempalaceClient] = None,
    graphify_client: Optional["GraphifyClient"] = None,
) -> str:
    """Return a token-disciplined markdown block to prepend to a Claude session.

    Uses spreading activation (mempalace + optional graphify hops) so adjacent
    context surfaces at session start — not just direct query matches.

    Format:

        <!-- typed/sessionstart -->
        ## Memory (3 drawers — call expand_drawer(id) for full text)
        - [drw_xxx] decision/auth: We chose JWT because ...
        - [drw_yyy] anti-pattern/api: Don't use bare except ...
        - [drw_zzz] recipe/deploy: Standard rollback steps ...
        <!-- /typed/sessionstart -->
    """
    query = f"{scope} {intent_hint}".strip() or scope
    drawers = spreading_activation_search(
        query=query,
        scope=scope,
        top_k=SESSION_START_TOP_K,
        client=client,
        graphify_client=graphify_client,
    )

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
    hits = client.search(query=drawer_id, top_k=1)
    if not hits:
        return None
    drawer = _hit_to_drawer(hits[0])
    if drawer is None or drawer.drawer_id != drawer_id:
        return None

    if bump_usage:
        drawer.usage_count += 1
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
    """Try to find a stored summary for a file before reading the full file."""
    client = client or InProcessClient()
    query = f"file_summary {file_path}"
    drawers = search_typed(query=query, scope=scope, top_k=1, client=client)
    if not drawers:
        return None
    d = drawers[0]
    if d.stale:
        return None
    return d.summary(max_chars=400)
