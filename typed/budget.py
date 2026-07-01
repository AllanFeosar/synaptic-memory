"""
Retrieval effectiveness tracker.

Answers the only question that matters: is synaptic-memory surfacing useful
memories, and how much context re-derivation is it likely avoiding?

Tracks via retrieval-audit.jsonl (one record per spreading-activation call).
Session writes are tracked in budget.jsonl (one record per Stop-hook fire).

CLI:
    py -m typed.budget               # savings estimate from retrieval audit
    py -m typed.budget --raw         # raw retrieval stats (hit rate, latency)
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean
from typing import Optional

from typed.config import get_config
import logging
logger = logging.getLogger(__name__)


DEFAULT_LOG = Path.home() / ".synaptic-memory" / "budget.jsonl"
DEFAULT_RETRIEVAL_LOG = Path.home() / ".synaptic-memory" / "retrieval-audit.jsonl"


def _rotate_if_needed(log_path: Path) -> None:
    """Rotate JSONL log when it exceeds configured max size."""
    bcfg = get_config().budget
    try:
        if not log_path.exists() or log_path.stat().st_size < bcfg.max_log_bytes:
            return
    except OSError:
        return
    for i in range(bcfg.log_keep_rotated - 1, 0, -1):
        src = log_path.with_suffix(f".{i}.jsonl")
        dst = log_path.with_suffix(f".{i + 1}.jsonl")
        if src.exists():
            try:
                src.rename(dst)
            except OSError:
                pass
    try:
        log_path.rename(log_path.with_suffix(".1.jsonl"))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Session records (lightweight — just tracks drawer writes per Stop-hook fire)
# ---------------------------------------------------------------------------

@dataclass
class SessionRecord:
    ts: str
    drawers_written: int = 0
    note: str = ""


def record_session(
    *,
    drawers_written: int = 0,
    note: str = "",
    log_path: Path = DEFAULT_LOG,
) -> SessionRecord:
    rec = SessionRecord(
        ts=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        drawers_written=drawers_written,
        note=note,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(rec)) + "\n")
    return rec


def _load_records(log_path: Path) -> list[SessionRecord]:
    if not log_path.exists():
        return []
    out: list[SessionRecord] = []
    for line in open(log_path, encoding="utf-8", errors="replace"):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            out.append(SessionRecord(
                ts=d["ts"],
                drawers_written=d.get("drawers_written", 0),
                note=d.get("note", ""),
            ))
        except (ValueError, TypeError, KeyError):
            continue
    return out


# ---------------------------------------------------------------------------
# Retrieval audit
# ---------------------------------------------------------------------------

@dataclass
class RetrievalRecord:
    ts: str
    query: str
    scope: Optional[str]
    top_k: int
    result_count: int
    duration_ms: float
    results: list
    interrupt_events: list = field(default_factory=list)
    effective_threshold: Optional[float] = None


def record_retrieval(
    *,
    query: str,
    scope: Optional[str] = None,
    top_k: int,
    results: list,
    duration_ms: float,
    interrupt_events: Optional[list] = None,
    effective_threshold: Optional[float] = None,
    log_path: Path = DEFAULT_RETRIEVAL_LOG,
) -> None:
    """Append one retrieval event to retrieval-audit.jsonl. Never raises."""
    rec = RetrievalRecord(
        ts=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        query=query,
        scope=scope,
        top_k=top_k,
        result_count=len(results),
        duration_ms=round(duration_ms, 1),
        results=results,
        interrupt_events=interrupt_events or [],
        effective_threshold=effective_threshold,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(log_path)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(rec)) + "\n")


def _load_retrieval_records(log_path: Path) -> list[dict]:
    records: list[dict] = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    try:
                        records.append(json.loads(line))
                    except (ValueError, TypeError):
                        continue
    except OSError:
        pass
    return records


def retrieval_report(log_path: Path = DEFAULT_RETRIEVAL_LOG) -> str:
    """Retrieval quality stats — hit rate, latency, ADHD interrupt layer."""
    if not log_path.exists():
        return "No retrieval records yet. Run sessions to populate retrieval-audit.jsonl."

    records = _load_retrieval_records(log_path)
    if not records:
        return "No retrieval records yet."

    total = len(records)
    avg_dur = mean(r["duration_ms"] for r in records)
    avg_results = mean(r["result_count"] for r in records)
    zero_result = sum(1 for r in records if r["result_count"] == 0)
    under_half = sum(1 for r in records if 0 < r["result_count"] < r["top_k"] / 2)

    has_interrupts = sum(1 for r in records if r.get("interrupt_events"))
    total_interrupts = sum(len(r.get("interrupt_events", [])) for r in records)
    thresholds = [r["effective_threshold"] for r in records if r.get("effective_threshold") is not None]
    latest_threshold = thresholds[-1] if thresholds else None

    lines = [
        "retrieval audit report",
        "=" * 28,
        f"Total retrievals logged : {total}",
        f"Avg duration            : {avg_dur:.1f} ms",
        f"Avg results returned    : {avg_results:.1f}",
        f"Zero-result queries     : {zero_result} ({zero_result / total * 100:.1f}%)",
        f"Under-half-k queries    : {under_half} ({under_half / total * 100:.1f}%)",
        "",
        "Interrupt layer:",
        f"  Retrievals with interrupts : {has_interrupts}/{total} ({has_interrupts / total * 100:.1f}%)",
        f"  Total interrupt events     : {total_interrupts}",
        f"  Effective threshold (last) : {latest_threshold or 'n/a'}",
        "",
        "Last 5 queries:",
    ]
    for r in records[-5:]:
        evt_tag = ""
        if r.get("interrupt_events"):
            kinds = [e["kind"] for e in r["interrupt_events"]]
            evt_tag = f"  INT:{','.join(kinds)}"
        lines.append(
            f"  [{r['ts'][:16]}] scope={r['scope'] or 'global'!r:12s} "
            f"→ {r['result_count']}/{r['top_k']} in {r['duration_ms']:.0f}ms"
            f"  q={r['query'][:50]!r}{evt_tag}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Savings estimate — answers "is the project reducing tokens?"
# ---------------------------------------------------------------------------

# Conservative estimate: each drawer surfaced replaces one context re-ask.
# A typical re-ask costs: user message ~20 tok + Claude file-search ~400 tok
# + Claude response ~80 tok = ~500 tok total.
_TOKENS_SAVED_PER_DRAWER = 500


def savings_report(
    log_path: Path = DEFAULT_RETRIEVAL_LOG,
    tokens_per_drawer: int = _TOKENS_SAVED_PER_DRAWER,
) -> str:
    """Estimate token reduction from retrieval audit data.

    Each drawer surfaced to Claude during a session replaces context the user
    would otherwise spend tokens re-deriving. This estimates the total savings.
    """
    if not log_path.exists():
        return "No retrieval data yet. Run sessions to populate retrieval-audit.jsonl."

    records = _load_retrieval_records(log_path)
    if not records:
        return "No retrieval data yet."

    total = len(records)
    hit_count = sum(1 for r in records if r["result_count"] > 0)
    total_drawers = sum(r["result_count"] for r in records)
    hit_rate = hit_count / total if total else 0

    interrupt_events = sum(len(r.get("interrupt_events", [])) for r in records)
    interrupt_retrievals = sum(1 for r in records if r.get("interrupt_events"))

    estimated_saved = total_drawers * tokens_per_drawer
    low_est = total_drawers * 200
    high_est = total_drawers * 1000

    lines = [
        "token reduction estimate",
        "=" * 40,
        f"Total retrieval events      : {total:,}",
        f"Retrievals with hits        : {hit_count:,} ({hit_rate * 100:.1f}%)",
        f"Total drawers surfaced      : {total_drawers:,}",
        f"ADHD early-exits            : {interrupt_events} across {interrupt_retrievals} retrievals",
        "",
        "Estimated tokens saved (drawers × avoided re-derivation cost):",
        f"  Conservative @ 200 tok/drawer : {low_est:>12,}",
        f"  Realistic    @ 500 tok/drawer : {estimated_saved:>12,}",
        f"  High         @ 1000 tok/drawer: {high_est:>12,}",
        "",
        "Note: percentage vs. baseline requires a controlled A/B experiment.",
        "      mem0 reports 50–91% reduction (published benchmarks, controlled).",
        "      synaptic-memory: estimate only — no pre-installation baseline captured.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse
    import sys
    p = argparse.ArgumentParser(description="synaptic-memory effectiveness report")
    p.add_argument("--raw", action="store_true", help="Raw retrieval audit stats")
    p.add_argument("--retrieval-log", type=Path, default=DEFAULT_RETRIEVAL_LOG)
    args = p.parse_args()

    if args.raw:
        sys.stdout.write(retrieval_report(args.retrieval_log) + "\n")
    else:
        sys.stdout.write(savings_report(args.retrieval_log) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
