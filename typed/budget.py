"""
Token budget tracking + kill switch.

Goal: prove the system pays for itself.

Records per-session:
  - tokens_in (input tokens for the session)
  - tokens_out (output tokens)
  - drawers_written
  - drawers_expanded
  - file_summary_hits
  - file_summary_misses

Computes weekly aggregates and warns if the targets aren't met.

Usage:
    from typed.budget import record_session, weekly_report

    record_session(tokens_in=42_000, tokens_out=8_300,
                   drawers_written=2, drawers_expanded=4,
                   file_summary_hits=3, file_summary_misses=7)

    print(weekly_report())
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
    """Rotate JSONL log file when it exceeds configured max size."""
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



@dataclass
class SessionRecord:
    ts: str
    tokens_in: int
    tokens_out: int
    drawers_written: int = 0
    drawers_expanded: int = 0
    file_summary_hits: int = 0
    file_summary_misses: int = 0
    note: str = ""

    def total(self) -> int:
        return self.tokens_in + self.tokens_out


@dataclass
class RetrievalRecord:
    ts: str
    query: str
    scope: Optional[str]
    top_k: int
    result_count: int
    duration_ms: float
    results: list  # [{drawer_id, score, type, snippet}]
    interrupt_events: list = field(default_factory=list)  # [{kind, score, drawer_id}]
    effective_threshold: Optional[float] = None


@dataclass
class WeeklyReport:
    week_index: int  # 0 = baseline, 1 = first week with system, etc.
    sessions: int
    avg_tokens: float
    median_tokens: float
    pct_change_vs_baseline: Optional[float]
    write_overhead_tokens: int
    estimated_read_savings: int
    target_met: Optional[bool]
    verdict: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def record_session(
    *,
    tokens_in: int,
    tokens_out: int,
    drawers_written: int = 0,
    drawers_expanded: int = 0,
    file_summary_hits: int = 0,
    file_summary_misses: int = 0,
    note: str = "",
    log_path: Path = DEFAULT_LOG,
) -> SessionRecord:
    rec = SessionRecord(
        ts=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        drawers_written=drawers_written,
        drawers_expanded=drawers_expanded,
        file_summary_hits=file_summary_hits,
        file_summary_misses=file_summary_misses,
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
            out.append(SessionRecord(**d))
        except (ValueError, TypeError):
            continue
    return out


# ---------------------------------------------------------------------------
# Retrieval audit
# ---------------------------------------------------------------------------

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


def retrieval_report(log_path: Path = DEFAULT_RETRIEVAL_LOG) -> str:
    """Summarise retrieval-audit.jsonl to surface quality signals."""
    if not log_path.exists():
        return "No retrieval records yet. Run sessions to populate retrieval-audit.jsonl."

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
        return "Could not read retrieval audit log."

    if not records:
        return "No retrieval records yet."

    total = len(records)
    avg_dur = mean(r["duration_ms"] for r in records)
    avg_results = mean(r["result_count"] for r in records)
    zero_result = sum(1 for r in records if r["result_count"] == 0)
    under_half = sum(1 for r in records if 0 < r["result_count"] < r["top_k"] / 2)

    # Interrupt stats (new fields may not exist in older records)
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
# Weekly report
# ---------------------------------------------------------------------------

def _week_buckets(records: list[SessionRecord]) -> dict[int, list[SessionRecord]]:
    """Bucket records by week index (week 0 = first 7 days of logging)."""
    if not records:
        return {}
    start = _dt.datetime.fromisoformat(records[0].ts)
    buckets: dict[int, list[SessionRecord]] = {}
    for r in records:
        ts = _dt.datetime.fromisoformat(r.ts)
        idx = int((ts - start).days // 7)
        buckets.setdefault(idx, []).append(r)
    return buckets


def weekly_report(log_path: Path = DEFAULT_LOG) -> str:
    records = _load_records(log_path)
    if not records:
        return "No session records yet. Call record_session() per Claude session."

    buckets = _week_buckets(records)
    if 0 not in buckets:
        return "Need at least 1 week of baseline data."

    bcfg = get_config().budget
    baseline_avg = mean(r.total() for r in buckets[0])
    write_overhead_per_session = mean(
        r.drawers_written * bcfg.tokens_per_drawer_write for r in buckets[0]  # ~250 tokens per drawer write
    )
    read_savings_per_session = mean(
        r.file_summary_hits * bcfg.tokens_per_cache_hit for r in buckets[0]  # ~1500 tokens per cache hit
    )

    lines = [
        "typed budget report",
        "=" * 28,
        f"Baseline (week 0): {len(buckets[0])} sessions, avg {baseline_avg:.0f} tokens",
        "",
    ]

    for idx in sorted(buckets.keys())[1:]:
        weeks = buckets[idx]
        avg = mean(r.total() for r in weeks)
        change = (avg - baseline_avg) / baseline_avg if baseline_avg else 0.0
        write_o = mean(r.drawers_written * bcfg.tokens_per_drawer_write for r in weeks)
        read_s = mean(r.file_summary_hits * bcfg.tokens_per_cache_hit for r in weeks)

        verdict = []
        bcfg = get_config().budget
        if idx >= 4 and -change < bcfg.week_4_target_drop:
            verdict.append("⚠ week-4 target missed")
        if write_o > read_s * bcfg.write_overhead_budget_fraction and read_s > 0:
            verdict.append("⚠ write overhead too high")
        if not verdict:
            verdict.append("ok")

        lines += [
            f"Week {idx}: {len(weeks)} sessions, avg {avg:.0f} tokens "
            f"({change*100:+.1f}% vs baseline)",
            f"  write_overhead≈{write_o:.0f}/session  read_savings≈{read_s:.0f}/session",
            f"  verdict: {', '.join(verdict)}",
            "",
        ]

    last_idx = max(buckets)
    if last_idx >= 4:
        last_avg = mean(r.total() for r in buckets[last_idx])
        change = (last_avg - baseline_avg) / baseline_avg if baseline_avg else 0.0
        if -change < get_config().budget.week_4_target_drop * 0.5:
            lines.append("KILL SWITCH: <10% reduction by week 4. Simplify aggressively.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse
    import sys
    p = argparse.ArgumentParser(description="typed budget report")
    p.add_argument("--log", type=Path, default=DEFAULT_LOG)
    p.add_argument("--record", action="store_true",
                   help="Record a session from --tokens-in/--tokens-out args")
    p.add_argument("--tokens-in", type=int, default=0)
    p.add_argument("--tokens-out", type=int, default=0)
    p.add_argument("--drawers-written", type=int, default=0)
    p.add_argument("--drawers-expanded", type=int, default=0)
    p.add_argument("--file-summary-hits", type=int, default=0)
    p.add_argument("--file-summary-misses", type=int, default=0)
    p.add_argument("--note", default="")
    p.add_argument("--retrieval-report", action="store_true",
                   help="Print retrieval-audit.jsonl summary")
    p.add_argument("--retrieval-log", type=Path, default=DEFAULT_RETRIEVAL_LOG)
    args = p.parse_args()

    if args.retrieval_report:
        sys.stdout.write(retrieval_report(args.retrieval_log) + "\n")
        return 0

    if args.record:
        rec = record_session(
            tokens_in=args.tokens_in,
            tokens_out=args.tokens_out,
            drawers_written=args.drawers_written,
            drawers_expanded=args.drawers_expanded,
            file_summary_hits=args.file_summary_hits,
            file_summary_misses=args.file_summary_misses,
            note=args.note,
            log_path=args.log,
        )
        sys.stdout.write(json.dumps(asdict(rec)) + "\n")
        return 0

    sys.stdout.write(weekly_report(args.log) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


