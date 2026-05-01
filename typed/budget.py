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


DEFAULT_LOG = Path.home() / ".synaptic-memory" / "budget.jsonl"

# Targets from the design spec
WEEK_4_TARGET_DROP = 0.20  # 20% reduction vs baseline
WEEK_8_TARGET_DROP = 0.30
WRITE_OVERHEAD_BUDGET_FRACTION = 0.30  # write tokens must stay below 30% of read savings


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
    for line in log_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            out.append(SessionRecord(**d))
        except (ValueError, TypeError):
            continue
    return out


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

    baseline_avg = mean(r.total() for r in buckets[0])
    write_overhead_per_session = mean(
        r.drawers_written * 250 for r in buckets[0]  # ~250 tokens per drawer write
    )
    read_savings_per_session = mean(
        r.file_summary_hits * 1500 for r in buckets[0]  # ~1500 tokens per cache hit
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
        change = (avg - baseline_avg) / baseline_avg
        write_o = mean(r.drawers_written * 250 for r in weeks)
        read_s = mean(r.file_summary_hits * 1500 for r in weeks)

        verdict = []
        if idx >= 4 and -change < WEEK_4_TARGET_DROP:
            verdict.append("⚠ week-4 target missed")
        if write_o > read_s * WRITE_OVERHEAD_BUDGET_FRACTION and read_s > 0:
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
        change = (last_avg - baseline_avg) / baseline_avg
        if -change < WEEK_4_TARGET_DROP * 0.5:  # <10% drop = kill switch
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
    args = p.parse_args()

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


