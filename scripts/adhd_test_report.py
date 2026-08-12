"""ADHD layer test monitor. Run: py -3.11 scripts/adhd_test_report.py [--baseline-date DATE] [--baseline-records N]"""

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

LOG = Path.home() / ".synaptic-memory" / "retrieval-audit.jsonl"


def _classify_source(rec):
    """Hook source for a record: the explicit `source` tag if present, else
    inferred from the (top_k, query==scope) signature. Inferred labels get a
    `?` suffix. Records pre-date the source tag until the 2026-07-15 wiring."""
    src = rec.get("source")
    if src:
        return src
    tk = rec.get("top_k")
    q = (rec.get("query") or "").strip()
    s = (rec.get("scope") or "").strip()
    if tk == 3:
        return "session_start?" if q and q == s else "pre_tool_read?"
    if tk == 2:
        return "post_tool_edit?"
    if isinstance(tk, int) and tk >= 6:
        return "pre_compact?"
    return "unknown?"


def main():
    parser = argparse.ArgumentParser(description="ADHD layer test report")
    parser.add_argument("--baseline-date", default="2026-08-12")
    parser.add_argument("--baseline-records", type=int, default=1695)
    args = parser.parse_args()
    BASELINE_DATE = args.baseline_date
    BASELINE_RECORDS = args.baseline_records
    records = []
    with open(LOG, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except (ValueError, TypeError):
                continue

    total = len(records)
    new = total - BASELINE_RECORDS
    test_records = records[BASELINE_RECORDS:]

    all_scores = []
    test_scores = []
    test_interrupt_count = 0
    test_interrupt_events = 0
    interrupt_kinds = Counter()
    thresholds = []
    source_total = Counter()
    source_fired = Counter()
    drift_count = 0
    drift_kept = 0
    drift_strategies = Counter()

    for rec in records:
        for r in rec.get("results", []):
            s = r.get("score")
            if isinstance(s, (int, float)):
                all_scores.append(s)

    for rec in test_records:
        for r in rec.get("results", []):
            s = r.get("score")
            if isinstance(s, (int, float)):
                test_scores.append(s)
        evts = rec.get("interrupt_events", [])
        src = _classify_source(rec)
        source_total[src] += 1
        if evts:
            test_interrupt_count += 1
            test_interrupt_events += len(evts)
            source_fired[src] += 1
            for e in evts:
                interrupt_kinds[e.get("kind", "?")] += 1
        t = rec.get("effective_threshold")
        if isinstance(t, (int, float)):
            thresholds.append(t)
        dv = rec.get("drift_events") or []
        if dv:
            drift_count += 1
            for d in dv:
                drift_strategies[d.get("strategy", "?")] += 1
                drift_kept += d.get("kept", 0)

    all_scores.sort(reverse=True)
    test_scores.sort(reverse=True)

    print("=" * 55)
    print("ADHD LAYER — TEST REPORT")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Test started: {BASELINE_DATE} (baseline: {BASELINE_RECORDS} records)")
    print("=" * 55)
    print(f"New records since test start: {new}")
    print(f"New scores: {len(test_scores)}")
    print()

    print("Interrupt events (test period only):")
    print(f"  Retrievals with interrupts: {test_interrupt_count}/{new}")
    pct = f"{100 * test_interrupt_count / new:.1f}%" if new > 0 else "N/A"
    print(f"  Interrupt rate: {pct}")
    print(f"  Total events: {test_interrupt_events}")
    if interrupt_kinds:
        print(f"  By kind: {dict(interrupt_kinds)}")
    print()

    if source_total:
        print("Interrupt rate by source (test period):")
        print("  (all 4 hooks run the layer since 2026-07-15; `?` = inferred, pre-tag record)")
        for src in sorted(source_total, key=lambda k: -source_total[k]):
            tot = source_total[src]
            fr = source_fired[src]
            rate = f"{100 * fr / tot:.1f}%" if tot else "N/A"
            print(f"  {src:16s}: {fr:>4}/{tot:<6} = {rate}")
        print()

    print("Drift events (Module 2 QueryDrift — fires only at level >= 2):")
    if drift_count:
        pct = f"{100 * drift_count / new:.1f}%" if new > 0 else "N/A"
        print(f"  Retrievals with drift: {drift_count}/{new}  ({pct})")
        print(f"  By strategy: {dict(drift_strategies)}")
        print(f"  Drawers merged in (past salience gate): {drift_kept}")
    else:
        print("  none — level < 2 (interrupt-only) or no drift dice hits yet")
    print()

    if thresholds:
        print(f"Effective threshold range: {min(thresholds):.4f} — {max(thresholds):.4f}")
        print(f"Effective threshold (last): {thresholds[-1]:.4f}")
    print()

    if test_scores:
        n = len(test_scores)
        print(f"Score distribution (test period):")
        print(f"  Max:    {test_scores[0]:.4f}")
        print(f"  P95:    {test_scores[int(n * 0.05)]:.4f}")
        print(f"  Median: {test_scores[int(n * 0.50)]:.4f}")
    print()

    n = len(all_scores)
    if n > 0:
        print(f"Score distribution (all time, {n} scores):")
        print(f"  Max:    {all_scores[0]:.4f}")
        print(f"  P95:    {all_scores[int(n * 0.05)]:.4f}")
        print(f"  Median: {all_scores[int(n * 0.50)]:.4f}")
    print()

    if new == 0:
        print("⚠ No new retrievals yet — use Claude Code sessions to generate data")
    elif test_interrupt_events == 0:
        print("⚠ Zero interrupts — check if scores are reaching the adaptive threshold")
    else:
        print("✓ Interrupts are firing — monitor quality over the week")


if __name__ == "__main__":
    main()
