"""
Tests for typed/budget.py — token budget tracking + kill switch.

Covers:
  - record_session appends to budget.jsonl
  - weekly_report with multi-week synthetic data
  - Kill switch triggers when reduction < target
  - Zero baseline tokens doesn't crash (div-by-zero guard)

Run:
    python -m pytest tests/test_budget.py -v
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from typed.budget import (
    SessionRecord,
    _load_records,
    _week_buckets,
    record_session,
    weekly_report,
)
from typed.config import BudgetConfig, SynapticConfig

# Suppress retrieval-audit writes during tests.
_record_retrieval_patcher = mock.patch("typed.budget.record_retrieval")


def setUpModule():
    _record_retrieval_patcher.start()


def tearDownModule():
    _record_retrieval_patcher.stop()


def _default_config(**budget_overrides):
    """Build a SynapticConfig with optional budget overrides."""
    cfg = SynapticConfig()
    for k, v in budget_overrides.items():
        setattr(cfg.budget, k, v)
    return cfg


def _make_record(
    base: _dt.datetime,
    day_offset: int,
    tokens_in: int,
    tokens_out: int,
    drawers_written: int = 0,
    file_summary_hits: int = 0,
) -> str:
    """Create a JSON line for a SessionRecord at base + day_offset days."""
    ts = (base + _dt.timedelta(days=day_offset)).isoformat()
    rec = {
        "ts": ts,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "drawers_written": drawers_written,
        "drawers_expanded": 0,
        "file_summary_hits": file_summary_hits,
        "file_summary_misses": 0,
        "note": "",
    }
    return json.dumps(rec)


def _write_synthetic_log(log_path: Path, lines: list[str]) -> None:
    """Write pre-built JSON lines to a budget log file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# record_session
# ---------------------------------------------------------------------------

class TestRecordSession(unittest.TestCase):

    def test_appends_to_log(self):
        """Two calls should produce two lines in the JSONL file."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "budget.jsonl"
            record_session(tokens_in=10, tokens_out=5, log_path=log)
            record_session(tokens_in=20, tokens_out=10, log_path=log)
            recs = _load_records(log)
            self.assertEqual(len(recs), 2)
            self.assertEqual(recs[0].tokens_in, 10)
            self.assertEqual(recs[1].tokens_in, 20)

    def test_returns_session_record(self):
        """record_session should return a populated SessionRecord."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "budget.jsonl"
            rec = record_session(
                tokens_in=42000, tokens_out=8300,
                drawers_written=2, drawers_expanded=4,
                file_summary_hits=3, file_summary_misses=7,
                note="test session",
                log_path=log,
            )
            self.assertIsInstance(rec, SessionRecord)
            self.assertEqual(rec.tokens_in, 42000)
            self.assertEqual(rec.tokens_out, 8300)
            self.assertEqual(rec.drawers_written, 2)
            self.assertEqual(rec.note, "test session")
            self.assertEqual(rec.total(), 50300)

    def test_creates_parent_directories(self):
        """Should create parent dirs if they don't exist."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "nested" / "deep" / "budget.jsonl"
            record_session(tokens_in=1, tokens_out=1, log_path=log)
            self.assertTrue(log.exists())

    def test_load_records_empty_file(self):
        """Loading from a nonexistent file should return an empty list."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "does_not_exist.jsonl"
            recs = _load_records(log)
            self.assertEqual(recs, [])


# ---------------------------------------------------------------------------
# _week_buckets
# ---------------------------------------------------------------------------

class TestWeekBuckets(unittest.TestCase):

    def test_single_week(self):
        """All records within 7 days should land in week 0."""
        base = _dt.datetime(2026, 6, 1, tzinfo=_dt.timezone.utc)
        records = [
            SessionRecord(ts=(base + _dt.timedelta(days=d)).isoformat(),
                          tokens_in=100, tokens_out=50)
            for d in range(7)
        ]
        buckets = _week_buckets(records)
        self.assertEqual(set(buckets.keys()), {0})
        self.assertEqual(len(buckets[0]), 7)

    def test_multi_week(self):
        """Records spanning 3 weeks should produce 3 buckets."""
        base = _dt.datetime(2026, 6, 1, tzinfo=_dt.timezone.utc)
        records = [
            SessionRecord(ts=(base + _dt.timedelta(days=d)).isoformat(),
                          tokens_in=100, tokens_out=50)
            for d in [0, 3, 7, 10, 14, 17]
        ]
        buckets = _week_buckets(records)
        self.assertIn(0, buckets)  # days 0, 3
        self.assertIn(1, buckets)  # days 7, 10
        self.assertIn(2, buckets)  # days 14, 17

    def test_empty_records(self):
        buckets = _week_buckets([])
        self.assertEqual(buckets, {})


# ---------------------------------------------------------------------------
# weekly_report — multi-week synthetic data
# ---------------------------------------------------------------------------

class TestWeeklyReport(unittest.TestCase):

    def _build_log(self, tmp_path: Path, week_data: list[tuple[int, int, int]]) -> Path:
        """Build a budget.jsonl with synthetic data.

        week_data: list of (day_offset, tokens_in, tokens_out) tuples.
        """
        base = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
        lines = [
            _make_record(base, day, tin, tout)
            for day, tin, tout in week_data
        ]
        log = tmp_path / "budget.jsonl"
        _write_synthetic_log(log, lines)
        return log

    def test_report_with_improvement(self):
        """When week 1 uses fewer tokens than baseline, report shows negative %."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Week 0 (baseline): 3 sessions at 10000 total tokens each
            # Week 1: 3 sessions at 7000 total tokens each (-30%)
            log = self._build_log(tmp_path, [
                (0, 5000, 5000),   # week 0
                (2, 5000, 5000),   # week 0
                (4, 5000, 5000),   # week 0
                (7, 3500, 3500),   # week 1
                (9, 3500, 3500),   # week 1
                (11, 3500, 3500),  # week 1
            ])

            with mock.patch("typed.budget.get_config", return_value=_default_config()):
                report = weekly_report(log)

            self.assertIn("Baseline (week 0)", report)
            self.assertIn("Week 1", report)
            self.assertIn("-30.0%", report)

    def test_report_with_increase(self):
        """When week 1 uses MORE tokens, report shows positive %."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            log = self._build_log(tmp_path, [
                (0, 5000, 5000),    # week 0: 10000 avg
                (2, 5000, 5000),    # week 0
                (7, 7500, 7500),    # week 1: 15000 avg
                (9, 7500, 7500),    # week 1
            ])

            with mock.patch("typed.budget.get_config", return_value=_default_config()):
                report = weekly_report(log)

            self.assertIn("+50.0%", report)

    def test_empty_log_returns_message(self):
        """No records should return an informative message, not crash."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "empty.jsonl"
            report = weekly_report(log)
            self.assertIn("No session records yet", report)

    def test_single_week_only_baseline(self):
        """With only baseline data (week 0), report should show baseline info only."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            log = self._build_log(tmp_path, [
                (0, 5000, 5000),
                (3, 5000, 5000),
            ])

            with mock.patch("typed.budget.get_config", return_value=_default_config()):
                report = weekly_report(log)

            self.assertIn("Baseline (week 0)", report)
            self.assertNotIn("Week 1", report)


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------

class TestKillSwitch(unittest.TestCase):

    def test_kill_switch_triggers(self):
        """When reduction < half of week_4_target_drop by week 4+, KILL SWITCH fires."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            base = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)

            # Week 0 (baseline): avg 10000 tokens
            # Week 4 (day 28+): avg 9800 tokens => -2% reduction
            # Default week_4_target_drop = 0.20, half = 0.10
            # -2% < 10% => KILL SWITCH
            lines = [
                _make_record(base, 0, 5000, 5000),
                _make_record(base, 2, 5000, 5000),
                _make_record(base, 4, 5000, 5000),
                # Week 4 — barely any improvement
                _make_record(base, 28, 4900, 4900),
                _make_record(base, 30, 4900, 4900),
                _make_record(base, 32, 4900, 4900),
            ]
            log = tmp_path / "budget.jsonl"
            _write_synthetic_log(log, lines)

            with mock.patch("typed.budget.get_config", return_value=_default_config()):
                report = weekly_report(log)

            self.assertIn("KILL SWITCH", report)

    def test_kill_switch_does_not_trigger_on_good_reduction(self):
        """With sufficient reduction, no KILL SWITCH message."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            base = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)

            # Week 0: avg 10000. Week 4: avg 5000 => -50% reduction.
            # 50% > 10% (half of 20%) => no kill switch.
            lines = [
                _make_record(base, 0, 5000, 5000),
                _make_record(base, 2, 5000, 5000),
                _make_record(base, 4, 5000, 5000),
                _make_record(base, 28, 2500, 2500),
                _make_record(base, 30, 2500, 2500),
                _make_record(base, 32, 2500, 2500),
            ]
            log = tmp_path / "budget.jsonl"
            _write_synthetic_log(log, lines)

            with mock.patch("typed.budget.get_config", return_value=_default_config()):
                report = weekly_report(log)

            self.assertNotIn("KILL SWITCH", report)

    def test_kill_switch_not_before_week_4(self):
        """Kill switch logic only applies at week 4+. Week 1 should not trigger it."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            base = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)

            # Only weeks 0 and 1 — even with 0% improvement, no kill switch
            lines = [
                _make_record(base, 0, 5000, 5000),
                _make_record(base, 2, 5000, 5000),
                _make_record(base, 7, 5000, 5000),
                _make_record(base, 9, 5000, 5000),
            ]
            log = tmp_path / "budget.jsonl"
            _write_synthetic_log(log, lines)

            with mock.patch("typed.budget.get_config", return_value=_default_config()):
                report = weekly_report(log)

            self.assertNotIn("KILL SWITCH", report)


# ---------------------------------------------------------------------------
# Division-by-zero guard
# ---------------------------------------------------------------------------

class TestZeroBaselineGuard(unittest.TestCase):

    def test_zero_baseline_tokens_no_crash(self):
        """When baseline avg is 0 tokens, pct_change should be 0.0 (not ZeroDivisionError)."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            base = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)

            # Week 0: all zeros. Week 1: some tokens.
            lines = [
                _make_record(base, 0, 0, 0),
                _make_record(base, 2, 0, 0),
                _make_record(base, 7, 500, 500),
                _make_record(base, 9, 500, 500),
            ]
            log = tmp_path / "budget.jsonl"
            _write_synthetic_log(log, lines)

            with mock.patch("typed.budget.get_config", return_value=_default_config()):
                # Should not raise ZeroDivisionError
                report = weekly_report(log)

            # The report should still be a valid string
            self.assertIsInstance(report, str)
            self.assertIn("Week 1", report)


# ---------------------------------------------------------------------------
# Write overhead verdict
# ---------------------------------------------------------------------------

class TestWriteOverheadVerdict(unittest.TestCase):

    def test_write_overhead_too_high_verdict(self):
        """When write overhead > read savings * fraction, verdict warns."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            base = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)

            # Week 0: baseline
            # Week 1: high writes (10 drawers * 250 = 2500 overhead),
            #         low reads (1 hit * 1500 = 1500 savings)
            #         2500 > 1500 * 0.30 (=450) => warn
            lines = [
                _make_record(base, 0, 5000, 5000, drawers_written=1, file_summary_hits=1),
                _make_record(base, 2, 5000, 5000, drawers_written=1, file_summary_hits=1),
                _make_record(base, 7, 5000, 5000, drawers_written=10, file_summary_hits=1),
                _make_record(base, 9, 5000, 5000, drawers_written=10, file_summary_hits=1),
            ]
            log = tmp_path / "budget.jsonl"
            _write_synthetic_log(log, lines)

            with mock.patch("typed.budget.get_config", return_value=_default_config()):
                report = weekly_report(log)

            self.assertIn("write overhead too high", report)

    def test_balanced_overhead_verdict_ok(self):
        """When write overhead is within budget, verdict should be 'ok'."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            base = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)

            # Week 1: 1 drawer * 250 = 250 overhead,
            #         10 hits * 1500 = 15000 savings
            #         250 < 15000 * 0.30 (=4500) => ok
            lines = [
                _make_record(base, 0, 5000, 5000, drawers_written=1, file_summary_hits=10),
                _make_record(base, 2, 5000, 5000, drawers_written=1, file_summary_hits=10),
                _make_record(base, 7, 4000, 4000, drawers_written=1, file_summary_hits=10),
                _make_record(base, 9, 4000, 4000, drawers_written=1, file_summary_hits=10),
            ]
            log = tmp_path / "budget.jsonl"
            _write_synthetic_log(log, lines)

            with mock.patch("typed.budget.get_config", return_value=_default_config()):
                report = weekly_report(log)

            self.assertIn("ok", report)


if __name__ == "__main__":
    unittest.main()
