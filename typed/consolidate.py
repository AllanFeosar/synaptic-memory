"""
Consolidation — the "sleep cycle". Runs nightly (or on demand).

Steps:
  1. Walk all typed drawers
  2. Rerank by salience (usage + recency + pin) — write back updated metadata
  3. Detect contradictions (high-similarity drawer pairs with opposing valences)
  4. Detect stale (drawers referencing files modified since last consolidate)
  5. Forget (archive zero-hit drawers older than 90 days)
  6. Read SYNAPTIC_SCAN_ROOT from .mcp.json graphify env block — if set,
     discover all projects under that root with graphify-out/graph.json
     and scripts/mempal_to_graphify.py, then sync each one.
     If not set, graphify sync is skipped entirely (no default).
  7. Notify on errors (log file + Notepad popup on Windows)
  8. Emit a JSON report
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
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


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

FORGET_AFTER_DAYS = 90
CONTRADICTION_SIM_THRESHOLD = 0.88
PIN_TOP_FRACTION = 0.05
DEFAULT_REPORT_PATH = Path.home() / ".synaptic-memory" / "consolidate-report.json"
DEFAULT_ERROR_LOG   = Path.home() / ".synaptic-memory" / "sync-errors.log"
SCAN_MAX_DEPTH = 6

# Directories to skip when scanning for projects
_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", "venv", ".venv", "env",
    "dist", "build", ".tox", "graphify-out", ".claude", "site-packages",
}
if sys.platform == "win32":
    _SKIP_DIRS |= {
        "Windows", "Program Files", "Program Files (x86)", "$Recycle.Bin",
        "System Volume Information", "ProgramData", "Recovery",
    }


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
    projects_discovered: list[str] = field(default_factory=list)
    projects_synced: list[str] = field(default_factory=list)
    projects_failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


# ---------------------------------------------------------------------------
# Walk + load drawers
# ---------------------------------------------------------------------------

def _enumerate_drawers(client: MempalaceClient) -> list[tuple[str, TypedDrawer]]:
    typed: list[tuple[str, TypedDrawer]] = []
    for wing in client.list_wings():
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

_FILE_REF_RE = re.compile(r"`([^`]+\.(?:py|ts|tsx|js|jsx|go|rs|java|md))`")


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
    """Archive drawers that have exceeded their tier TTL.

    Tier TTLs:
        ephemeral  — 1 day  (archived even if used; scratch notes)
        short-term — 7 days (archived if zero hits)
        long-term  — 90 days (archived if zero hits)
        permanent  — never archived

    Pinned drawers are always exempt regardless of tier.
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    for mid, d in items:
        if d.pinned:
            continue
        if d.scope.startswith("__archive__"):
            continue

        ttl = d.tier.ttl_days
        if ttl is None:
            continue  # permanent — never archive

        age_days = (now - d.created_at).total_seconds() / 86400.0
        if age_days < ttl:
            continue

        # Ephemeral drawers expire regardless of usage (they're scratch notes).
        # All other tiers survive if they have been used at least once.
        if d.tier != MemoryTier.EPHEMERAL and d.usage_count > 0:
            continue

        d.scope = f"__archive__{d.scope}"
        try:
            client.update_drawer(mid, serialize_drawer(d))
            report.archived.append(d.drawer_id)
        except Exception as e:  # noqa: BLE001
            report.errors.append(f"archive {d.drawer_id}: {e}")


# ---------------------------------------------------------------------------
# Read scan root from .mcp.json
# ---------------------------------------------------------------------------

def _read_scan_root_from_mcp(synaptic_repo: Optional[Path] = None) -> Optional[Path]:
    """Read SYNAPTIC_SCAN_ROOT from .mcp.json graphify env block.

    Looks in synaptic_repo first, then CWD. Returns None if not configured —
    caller should skip graphify sync entirely in that case.
    """
    candidates: list[Path] = []
    if synaptic_repo:
        candidates.append(synaptic_repo / ".mcp.json")
    candidates.append(Path.cwd() / ".mcp.json")

    for mcp_path in candidates:
        if not mcp_path.exists():
            continue
        try:
            data = json.loads(mcp_path.read_text(encoding="utf-8"))
            val = (
                data.get("mcpServers", {})
                    .get("graphify", {})
                    .get("env", {})
                    .get("SYNAPTIC_SCAN_ROOT", "")
            )
            if val:
                return Path(val)
        except (json.JSONDecodeError, OSError):
            continue
    return None


# ---------------------------------------------------------------------------
# Project discovery
# ---------------------------------------------------------------------------

def _discover_projects(scan_root: Path, max_depth: int = SCAN_MAX_DEPTH) -> list[Path]:
    """Walk scan_root and return every project root that has graphify-out/graph.json."""
    found: list[Path] = []

    def walk(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            for child in sorted(path.iterdir()):
                if not child.is_dir():
                    continue
                if child.name in _SKIP_DIRS or child.name.startswith((".", "$")):
                    continue
                if (child / "graphify-out" / "graph.json").exists():
                    found.append(child)
                    # Don't recurse into a found project (no nested projects)
                else:
                    walk(child, depth + 1)
        except PermissionError:
            pass

    walk(scan_root, 0)
    return found


# ---------------------------------------------------------------------------
# Per-project graphify sync
# ---------------------------------------------------------------------------

def _sync_project_graph(
    project_root: Path,
    report: ConsolidateReport,
) -> bool:
    """Run mempal_to_graphify.py + graphify_wiki.py for one discovered project."""
    bridge = project_root / "scripts" / "mempal_to_graphify.py"
    wiki   = project_root / "scripts" / "graphify_wiki.py"

    if not bridge.exists():
        report.errors.append(
            f"[{project_root.name}] scripts/mempal_to_graphify.py not found — skipped"
        )
        report.projects_failed.append(str(project_root))
        return False

    # Run bridge script
    try:
        result = subprocess.run(
            ["py", "-3.11", str(bridge)],
            capture_output=True, text=True, timeout=300,
            cwd=str(project_root),
        )
        if result.returncode != 0:
            report.errors.append(
                f"[{project_root.name}] mempal_to_graphify failed: "
                f"{result.stderr.strip()[:400]}"
            )
            report.projects_failed.append(str(project_root))
            return False
    except subprocess.SubprocessError as e:
        report.errors.append(f"[{project_root.name}] mempal_to_graphify exception: {e}")
        report.projects_failed.append(str(project_root))
        return False

    # Run wiki rebuild (non-critical — failure doesn't fail the project)
    if wiki.exists():
        try:
            subprocess.run(
                ["py", "-3.14", str(wiki), "--clean"],
                capture_output=True, text=True, timeout=120,
                cwd=str(project_root),
            )
        except subprocess.SubprocessError:
            pass

    report.projects_synced.append(str(project_root))
    return True


# ---------------------------------------------------------------------------
# Error notification
# ---------------------------------------------------------------------------

def _notify_errors(errors: list[str], log_path: Path) -> None:
    """Write errors to log file. On Windows, open Notepad so you see it next morning."""
    if not errors:
        return

    timestamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"=== synaptic-memory consolidation errors — {timestamp} ===",
        "",
    ]
    lines.extend(f"  {e}" for e in errors)
    lines.append("")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines), encoding="utf-8")

    if sys.platform == "win32":
        try:
            subprocess.Popen(["notepad", str(log_path)])
        except FileNotFoundError:
            # Notepad not found — try cmd popup as fallback
            try:
                msg = f"synaptic-memory: {len(errors)} sync error(s). See {log_path}"
                subprocess.Popen(["cmd", "/c", f"echo {msg} & pause"], creationflags=0x00000010)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def consolidate(
    *,
    repo_root: Optional[Path] = None,
    synaptic_repo: Optional[Path] = None,
    scan_root: Optional[Path] = None,
    report_path: Path = DEFAULT_REPORT_PATH,
    client: Optional[MempalaceClient] = None,
) -> ConsolidateReport:
    """Run the full nightly consolidation. Returns a JSON-serializable report."""
    client = client or InProcessClient()
    report = ConsolidateReport(started_at=_dt.datetime.now(_dt.timezone.utc).isoformat())

    # Load last-run timestamp for stale detection
    last_consolidate: Optional[_dt.datetime] = None
    if report_path.exists():
        try:
            prev = json.loads(report_path.read_text())
            last_consolidate = _dt.datetime.fromisoformat(prev.get("finished_at"))
        except (ValueError, KeyError, json.JSONDecodeError):
            last_consolidate = None

    # --- mempalace health (global, runs once) ---
    items = _enumerate_drawers(client)
    report.drawers_scanned = len(items)
    report.drawers_typed = len(items)

    _rerank_and_pin(items, client, report)
    _detect_contradictions(items, client, report)
    _mark_stale_for_changed_files(items, repo_root, last_consolidate, client, report)
    _archive_old(items, client, report)

    # --- per-project graphify sync ---
    if scan_root and scan_root.exists():
        projects = _discover_projects(scan_root)
        report.projects_discovered = [str(p) for p in projects]
        for proj in projects:
            _sync_project_graph(proj, report)
    else:
        # Fallback: sync the synaptic-memory repo itself (old behavior)
        if synaptic_repo and (synaptic_repo / "scripts" / "mempal_to_graphify.py").exists():
            _sync_project_graph(synaptic_repo, report)

    report.finished_at = _dt.datetime.now(_dt.timezone.utc).isoformat()

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.to_json())

    _notify_errors(report.errors, DEFAULT_ERROR_LOG)

    return report


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="typed consolidation cron")
    p.add_argument("--repo-root", type=Path, default=None,
                   help="Root of the project being consolidated (used for stale detection).")
    p.add_argument("--synaptic-repo", type=Path, default=None,
                   help="Path to synaptic-memory repo (used to locate .mcp.json for scan root).")
    p.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    args = p.parse_args()

    synaptic_repo = args.synaptic_repo if args.synaptic_repo and args.synaptic_repo.exists() else None
    scan_root = _read_scan_root_from_mcp(synaptic_repo)

    report = consolidate(
        repo_root=args.repo_root,
        synaptic_repo=synaptic_repo,
        scan_root=scan_root,
        report_path=args.report,
    )
    sys.stdout.write(report.to_json())
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
