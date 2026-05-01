"""
Consolidation — the "sleep cycle". Runs nightly (or on demand).

Steps:
  1. Walk all typed drawers
  2. Rerank by salience (usage + recency + pin) — write back updated metadata
  3. Detect contradictions (high-similarity drawer pairs with opposing valences)
  4. Detect stale (drawers referencing files modified since last consolidate)
  5. Forget (archive zero-hit drawers older than 90 days)
  6. Sync into graphify (delegated to existing mempal_to_graphify.py)
  7. Emit a JSON report
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from typed.client import InProcessClient, MempalaceClient
from typed.types import (
    Confidence,
    DrawerType,
    TypedDrawer,
    parse_drawer,
    serialize_drawer,
)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

FORGET_AFTER_DAYS = 90
CONTRADICTION_SIM_THRESHOLD = 0.88  # high similarity
PIN_TOP_FRACTION = 0.05             # top 5% per scope auto-pinned
DEFAULT_REPORT_PATH = Path.home() / ".synaptic-memory" / "consolidate-report.json"


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------

@dataclass
class ConsolidateReport:
    started_at: str
    finished_at: Optional[str] = None
    drawers_scanned: int = 0
    drawers_typed: int = 0
    drawers_untyped: int = 0
    auto_pinned: list[str] = field(default_factory=list)
    contradictions: list[dict] = field(default_factory=list)
    marked_stale: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    graphify_synced: bool = False
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, indent=2, default=str)


# ---------------------------------------------------------------------------
# Walk + load
# ---------------------------------------------------------------------------

def _enumerate_drawers(client: MempalaceClient) -> list[tuple[str, TypedDrawer]]:
    """Return [(mempalace_id, TypedDrawer)] for all v2 typed drawers."""
    typed: list[tuple[str, TypedDrawer]] = []

    for wing in client.list_wings():
        # We sweep with a wide search ('*') to enumerate. Mempalace's search
        # API may need a more specific query — we use the wing name as a fallback.
        hits = client.search(query=wing, top_k=10_000, wing=wing)
        for hit in hits:
            try:
                d = parse_drawer(hit.content)
                typed.append((hit.drawer_id, d))
            except ValueError:
                continue
    return typed


# ---------------------------------------------------------------------------
# Salience rerank
# ---------------------------------------------------------------------------

def _rerank_and_pin(
    items: list[tuple[str, TypedDrawer]],
    client: MempalaceClient,
    report: ConsolidateReport,
) -> None:
    by_scope: dict[str, list[tuple[str, TypedDrawer]]] = {}
    for mid, d in items:
        by_scope.setdefault(d.scope, []).append((mid, d))

    for scope, group in by_scope.items():
        group.sort(key=lambda kv: -kv[1].salience())
        cutoff = max(1, int(len(group) * PIN_TOP_FRACTION))
        for mid, d in group[:cutoff]:
            if not d.pinned:
                d.pinned = True
                try:
                    client.update_drawer(mid, serialize_drawer(d))
                    report.auto_pinned.append(d.drawer_id)
                except Exception as e:  # noqa: BLE001
                    report.errors.append(f"pin {d.drawer_id}: {e}")


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------

_OPPOSING_TYPE_PAIRS = {
    (DrawerType.DECISION, DrawerType.ANTI_PATTERN),
    (DrawerType.PATTERN, DrawerType.ANTI_PATTERN),
}


def _detect_contradictions(
    items: list[tuple[str, TypedDrawer]],
    client: MempalaceClient,
    report: ConsolidateReport,
) -> None:
    """Pairs with high embedding similarity AND opposing types/valences.

    Doesn't auto-resolve — flags for human review in the report.
    """
    # Embedding-pair detection: for each drawer, search top-k and look for
    # high-similarity matches in opposing types or contradictory bodies.
    by_id = {d.drawer_id: (mid, d) for mid, d in items}
    seen_pairs: set[frozenset[str]] = set()

    for mid, d in items:
        hits = client.search(query=d.body, top_k=5, wing=d.scope)
        for hit in hits:
            if hit.drawer_id == mid:
                continue
            if hit.score < CONTRADICTION_SIM_THRESHOLD:
                continue
            try:
                other = parse_drawer(hit.content)
            except ValueError:
                continue
            if other.drawer_id == d.drawer_id:
                continue
            pair_key = frozenset({d.drawer_id, other.drawer_id})
            if pair_key in seen_pairs:
                continue

            type_pair = (d.type, other.type)
            opposing = type_pair in _OPPOSING_TYPE_PAIRS or type_pair[::-1] in _OPPOSING_TYPE_PAIRS

            # Same-type contradiction signal: both decisions, both high-confidence,
            # but supersedence chain doesn't link them.
            same_type_conflict = (
                d.type == other.type == DrawerType.DECISION
                and d.confidence == Confidence.HIGH
                and other.confidence == Confidence.HIGH
                and not (d.supersedes == other.drawer_id or other.supersedes == d.drawer_id)
            )

            if opposing or same_type_conflict:
                seen_pairs.add(pair_key)
                report.contradictions.append({
                    "a": d.drawer_id,
                    "b": other.drawer_id,
                    "scope": d.scope,
                    "similarity": round(hit.score, 3),
                    "kind": "opposing-type" if opposing else "same-type-no-supersedes",
                })


# ---------------------------------------------------------------------------
# Stale detection
# ---------------------------------------------------------------------------

_FILE_REF_RE = __import__("re").compile(r"`([^`]+\.(?:py|ts|tsx|js|jsx|go|rs|java|md))`")


def _mark_stale_for_changed_files(
    items: list[tuple[str, TypedDrawer]],
    repo_root: Optional[Path],
    last_consolidate: Optional[_dt.datetime],
    client: MempalaceClient,
    report: ConsolidateReport,
) -> None:
    if not repo_root or not last_consolidate:
        return
    for mid, d in items:
        if d.stale:
            continue
        for match in _FILE_REF_RE.finditer(d.body):
            rel = match.group(1)
            p = repo_root / rel
            try:
                if p.exists() and _dt.datetime.fromtimestamp(
                    p.stat().st_mtime, _dt.timezone.utc
                ) > last_consolidate:
                    d.stale = True
                    client.update_drawer(mid, serialize_drawer(d))
                    report.marked_stale.append(d.drawer_id)
                    break
            except OSError:
                continue


# ---------------------------------------------------------------------------
# Forgetting (archive, never delete)
# ---------------------------------------------------------------------------

def _archive_old(
    items: list[tuple[str, TypedDrawer]],
    client: MempalaceClient,
    report: ConsolidateReport,
) -> None:
    now = _dt.datetime.now(_dt.timezone.utc)
    for mid, d in items:
        if d.pinned:
            continue
        age_days = (now - d.created_at).total_seconds() / 86400.0
        if age_days < FORGET_AFTER_DAYS:
            continue
        if d.usage_count > 0:
            continue
        # Move to archive wing by re-tagging scope. We don't delete.
        if d.scope.startswith("__archive__"):
            continue
        d.scope = f"__archive__{d.scope}"
        try:
            client.update_drawer(mid, serialize_drawer(d))
            report.archived.append(d.drawer_id)
        except Exception as e:  # noqa: BLE001
            report.errors.append(f"archive {d.drawer_id}: {e}")


# ---------------------------------------------------------------------------
# Graphify sync (delegate to existing script)
# ---------------------------------------------------------------------------

def _sync_graphify(synaptic_repo: Optional[Path], report: ConsolidateReport) -> None:
    if not synaptic_repo:
        return
    script = synaptic_repo / "scripts" / "mempal_to_graphify.py"
    if not script.exists():
        report.errors.append(f"graphify script not found: {script}")
        return
    try:
        result = subprocess.run(
            ["py", "-3.14", str(script)],
            capture_output=True, text=True, timeout=300,
        )
        report.graphify_synced = result.returncode == 0
        if result.returncode != 0:
            report.errors.append(f"graphify sync stderr: {result.stderr.strip()[:500]}")
    except subprocess.SubprocessError as e:
        report.errors.append(f"graphify sync exception: {e}")


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def consolidate(
    *,
    repo_root: Optional[Path] = None,
    synaptic_repo: Optional[Path] = None,
    report_path: Path = DEFAULT_REPORT_PATH,
    client: Optional[MempalaceClient] = None,
) -> ConsolidateReport:
    """Run the full nightly consolidation. Returns a JSON-serializable report."""
    client = client or InProcessClient()
    report = ConsolidateReport(started_at=_dt.datetime.now(_dt.timezone.utc).isoformat())

    # Load last-run timestamp for stale detection.
    last_consolidate: Optional[_dt.datetime] = None
    if report_path.exists():
        try:
            prev = json.loads(report_path.read_text())
            last_consolidate = _dt.datetime.fromisoformat(prev.get("finished_at"))
        except (ValueError, KeyError, json.JSONDecodeError):
            last_consolidate = None

    items = _enumerate_drawers(client)
    report.drawers_scanned = len(items)
    report.drawers_typed = len(items)

    _rerank_and_pin(items, client, report)
    _detect_contradictions(items, client, report)
    _mark_stale_for_changed_files(items, repo_root, last_consolidate, client, report)
    _archive_old(items, client, report)
    _sync_graphify(synaptic_repo, report)

    report.finished_at = _dt.datetime.now(_dt.timezone.utc).isoformat()

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.to_json())
    return report


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="typed consolidation cron")
    p.add_argument("--repo-root", type=Path, default=None,
                   help="Root of the project being consolidated (used for stale detection).")
    p.add_argument("--synaptic-repo", type=Path,
                   default=Path(os.environ.get("SYNAPTIC_REPO", "")),
                   help="Path to synaptic-memory repo (for graphify script).")
    p.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    p.add_argument("--dry-run", action="store_true", help="Skip mutations; print plan only.")
    args = p.parse_args()

    if args.dry_run:
        sys.stderr.write("[typed] dry-run mode not yet implemented\n")
        return 1

    report = consolidate(
        repo_root=args.repo_root,
        synaptic_repo=args.synaptic_repo if args.synaptic_repo and args.synaptic_repo.exists() else None,
        report_path=args.report,
    )
    sys.stdout.write(report.to_json())
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


