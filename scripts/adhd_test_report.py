"""ADHD layer 1-week test monitor. Run daily: py -3.11 scripts/adhd_test_report.py"""

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

LOG = Path.home() / ".synaptic-memory" / "retrieval-audit.jsonl"
BASELINE_DATE = "2026-06-27"
BASELINE_RECORDS = 3193

def main():
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
        if evts:
            test_interrupt_count += 1
            test_interrupt_events += len(evts)
            for e in evts:
                interrupt_kinds[e.get("kind", "?")] += 1
        t = rec.get("effective_threshold")
        if isinstance(t, (int, float)):
            thresholds.append(t)

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
