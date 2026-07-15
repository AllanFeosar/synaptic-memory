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

import logging
import re
import time

logger = logging.getLogger(__name__)
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from typed.adhd import ADHDConfig, InterruptLayer
from typed.client import InProcessClient, MempalaceClient, SearchHit
from typed.config import get_config
from typed.types import MemoryTier, TypedDrawer, parse_drawer, serialize_drawer

if TYPE_CHECKING:
    from typed.graphify_client import GraphifyClient

# Regex for file references in drawer bodies, e.g. `auth.py`, `jwt.ts`
from typed.types import FILE_REF_RE as _FILE_REF_RE
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
    return deduped[:get_config().hooks.max_code_refs_per_drawer]


def search_typed(
    query: str,
    scope: Optional[str] = None,
    top_k: Optional[int] = None,
    *,
    client: Optional[MempalaceClient] = None,
    include_low_confidence: bool = True,
    tier_filter: Optional[MemoryTier] = None,
) -> list[TypedDrawer]:
    """Semantic search returning only typed drawers, reranked by salience."""
    cfg = get_config().retrieval
    top_k = top_k or cfg.session_start_top_k
    client = client or InProcessClient.get_or_create()
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


def _expand_graphify(seed_drawer, decay, cfg, graphify_client, _search, activation, next_frontier):
    """Expand via graphify structural edges — extracted to reduce nesting depth."""
    code_refs = _extract_code_refs(seed_drawer.body)
    for ref in code_refs[:cfg.graphify_refs_per_hop]:
        g_labels = graphify_client.query(ref, limit=cfg.graphify_labels_per_ref)
        for label in g_labels:
            g_hits = _search(label, 2)
            for hit in g_hits:
                d = _hit_to_drawer(hit)
                if d is None or d.drawer_id in activation:
                    continue
                score = hit.score * decay * cfg.graphify_hop_discount
                activation[d.drawer_id] = (d, score)
                next_frontier.append((d, score))


def spreading_activation_search(
    query: str,
    scope: Optional[str] = None,
    top_k: Optional[int] = None,
    depth: Optional[int] = None,
    hop_decay: Optional[float] = None,
    *,
    client: Optional[MempalaceClient] = None,
    graphify_client: Optional["GraphifyClient"] = None,
    adhd_config: Optional[ADHDConfig] = None,
    source: Optional[str] = None,
) -> list[TypedDrawer]:
    """Spreading activation retrieval with optional graphify structural hops."""
    cfg = get_config().retrieval
    top_k = top_k if top_k is not None else cfg.session_start_top_k
    depth = depth if depth is not None else cfg.hop_depth
    hop_decay = hop_decay if hop_decay is not None else cfg.hop_decay

    _t0 = time.perf_counter()
    client = client or InProcessClient.get_or_create()
    wing = scope.lower() if scope else None
    _interrupt = InterruptLayer(adhd_config or ADHDConfig())

    search_calls = [0]

    def _search(q: str, k: int):
        if search_calls[0] >= cfg.max_search_calls:
            return []
        search_calls[0] += 1
        return client.search(query=q, top_k=k, wing=wing)

    # activation map: drawer_id -> (drawer, activation_score)
    activation: dict[str, tuple[TypedDrawer, float]] = {}

    # Seed: initial semantic search
    seeds = _search(query, top_k * 2)
    frontier: list[tuple[TypedDrawer, float]] = []
    for hit in seeds:
        d = _hit_to_drawer(hit)
        if d is None:
            continue
        score = hit.score
        if d.drawer_id not in activation:
            activation[d.drawer_id] = (d, score)
            frontier.append((d, score))

    _interrupt.check_seeds(frontier)

    # Ripple outward hop by hop
    for hop in range(1, depth + 1):
        decay = hop_decay ** hop
        next_frontier: list[tuple[TypedDrawer, float]] = []

        # Only expand the strongest activations — caps fan-out instead of
        # rippling from every drawer activated so far.
        ripple_from = sorted(frontier, key=lambda kv: -kv[1])[:cfg.frontier_fanout]

        for seed_drawer, seed_score in ripple_from:
            neighbor_query = seed_drawer.body[:300]
            neighbors = _search(neighbor_query, 3)
            for hit in neighbors:
                d = _hit_to_drawer(hit)
                if d is None or d.drawer_id in activation:
                    continue
                score = hit.score * decay
                activation[d.drawer_id] = (d, score)
                next_frontier.append((d, score))

            if graphify_client is not None:
                _expand_graphify(seed_drawer, decay, cfg, graphify_client,
                                _search, activation, next_frontier)

        frontier = next_frontier
        if not frontier or search_calls[0] >= cfg.max_search_calls:
            break

    # Rank by activation * salience — rewards both relatedness and importance
    blend = get_config().salience.blend_ratio
    ranked = sorted(
        activation.values(),
        key=lambda kv: -(kv[1] + kv[0].salience() * blend),
    )
    ranked = _interrupt.post_merge(ranked)
    top = ranked[:top_k]

    try:
        from typed.budget import record_retrieval as _rr
        _rr(
            query=query,
            scope=scope,
            top_k=top_k,
            results=[
                {
                    "drawer_id": d.drawer_id,
                    "score": round(s, 4),
                    "type": d.type.value,
                    "snippet": d.body[:80],
                }
                for d, s in top
            ],
            duration_ms=(time.perf_counter() - _t0) * 1000,
            interrupt_events=[
                {"kind": e.kind, "score": round(e.score, 4), "drawer_id": e.drawer.drawer_id}
                for e in _interrupt.events
            ],
            # Only meaningful when the layer actually ran. Logging it while the
            # layer is inactive (the default on most retrieval paths pre-2026-07-15)
            # polluted every interrupt-rate/should-fire analysis with records that
            # could never fire. None => layer was off for this retrieval.
            effective_threshold=(
                round(_interrupt.effective_threshold, 4) if _interrupt.active else None
            ),
            source=source,
        )
    except Exception:
        logger.debug("retrieval audit record failed", exc_info=True)

    return [d for d, _ in top]


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
    cfg = get_config().retrieval
    query = f"{scope} {intent_hint}".strip() or scope
    drawers = spreading_activation_search(
        query=query,
        scope=scope,
        top_k=cfg.session_start_top_k,
        client=client,
        graphify_client=graphify_client,
        adhd_config=ADHDConfig.from_env(),
        source="session_start",
    )

    if not drawers:
        return ""

    lines = [
        "<!-- typed/sessionstart -->",
        f"## Memory ({len(drawers)} drawers — call expand_drawer(id) for full text)",
    ]
    for d in drawers:
        lines.append(f"- {d.summary(max_chars=cfg.summary_max_chars)}")
    lines.append("<!-- /typed/sessionstart -->")
    return "\n".join(lines)


def expand_drawer(
    drawer_id: str,
    *,
    client: Optional[MempalaceClient] = None,
    bump_usage: bool = True,
) -> Optional[TypedDrawer]:
    """Fetch full drawer by id. Increments usage_count for trust calibration."""
    client = client or InProcessClient.get_or_create()
    hit = client.get_drawer(drawer_id)
    if not hit:
        return None
    drawer = _hit_to_drawer(hit)
    if drawer is None:
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
    client = client or InProcessClient.get_or_create()
    query = f"file_summary {file_path}"
    drawers = search_typed(query=query, scope=scope, top_k=1, client=client)
    if not drawers:
        return None
    d = drawers[0]
    if d.stale:
        return None
    return d.summary(max_chars=400)
