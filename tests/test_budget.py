"""
Tests for typed/budget.py — retrieval effectiveness tracker.

Covers:
  - record_session appends to budget.jsonl
  - savings_report produces correct estimates from retrieval audit data
  - retrieval_report summarises audit log correctly

Run:
    python -m pytest tests/test_budget.py -v
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from typed.budget import (
    SessionRecord,
    _load_records,
    record_session,
    retrieval_report,
    savings_report,
)


# ---------------------------------------------------------------------------
# record_session
# ---------------------------------------------------------------------------

class TestRecordSession(unittest.TestCase):

    def test_appends_to_log(self):
        """Two calls should produce two lines in the JSONL file."""
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "budget.jsonl"
            record_session(drawers_written=1, log_path=log)
            record_session(drawers_written=2, log_path=log)
            recs = _load_records(log)
            self.assertEqual(len(recs), 2)
            self.assertEqual(recs[0].drawers_written, 1)
            self.assertEqual(recs[1].drawers_written, 2)

    def test_returns_session_record(self):
        """record_session should return a populated SessionRecord."""
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "budget.jsonl"
            rec = record_session(drawers_written=3, note="test", log_path=log)
            self.assertIsInstance(rec, SessionRecord)
            self.assertEqual(rec.drawers_written, 3)
            self.assertEqual(rec.note, "test")

    def test_creates_parent_directories(self):
        """Should create parent dirs if they don't exist."""
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "nested" / "deep" / "budget.jsonl"
            record_session(drawers_written=1, log_path=log)
            self.assertTrue(log.exists())

    def test_load_records_empty_file(self):
        """Loading from a nonexistent file should return an empty list."""
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "does_not_exist.jsonl"
            recs = _load_records(log)
            self.assertEqual(recs, [])

    def test_load_ignores_legacy_token_fields(self):
        """Old records with tokens_in/tokens_out/file_summary_hits load without crashing."""
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "legacy.jsonl"
            legacy = {
                "ts": "2026-01-01T00:00:00+00:00",
                "tokens_in": 50000,
                "tokens_out": 8000,
                "drawers_written": 2,
                "drawers_expanded": 0,
                "file_summary_hits": 3,
                "file_summary_misses": 1,
                "note": "old record",
            }
            log.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
            recs = _load_records(log)
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0].drawers_written, 2)
            self.assertEqual(recs[0].note, "old record")


# ---------------------------------------------------------------------------
# savings_report
# ---------------------------------------------------------------------------

def _make_retrieval(result_count: int, interrupts: int = 0, ts_offset_days: int = 0) -> str:
    ts = (_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
          + _dt.timedelta(days=ts_offset_days)).isoformat()
    events = [{"kind": "threshold", "score": 0.9, "drawer_id": "x"}] * interrupts
    return json.dumps({
        "ts": ts,
        "query": "test query",
        "scope": "TEST",
        "top_k": 3,
        "result_count": result_count,
        "duration_ms": 100.0,
        "results": [{"drawer_id": f"d{i}", "score": 0.8} for i in range(result_count)],
        "interrupt_events": events,
        "effective_threshold": 0.42 if interrupts else None,
    })


class TestSavingsReport(unittest.TestCase):

    def test_no_file_returns_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "missing.jsonl"
            report = savings_report(log)
            self.assertIn("No retrieval data yet", report)

    def test_estimates_from_hit_data(self):
        """Report should compute drawers surfaced and estimate savings."""
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "audit.jsonl"
            # 4 retrievals: 3 hits (2+3+1 drawers) + 1 miss
            lines = [
                _make_retrieval(2),
                _make_retrieval(3),
                _make_retrieval(1),
                _make_retrieval(0),
            ]
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            report = savings_report(log, tokens_per_drawer=500)

        self.assertIn("Total retrieval events", report)
        self.assertIn("4", report)          # 4 total
        self.assertIn("6", report)          # 6 total drawers surfaced (2+3+1)
        self.assertIn("3,000", report)      # 6 × 500 = 3,000 realistic estimate

    def test_hit_rate_calculation(self):
        """3 hits out of 4 retrievals = 75% hit rate."""
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "audit.jsonl"
            lines = [_make_retrieval(1), _make_retrieval(1), _make_retrieval(1), _make_retrieval(0)]
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            report = savings_report(log)

        self.assertIn("75.0%", report)

    def test_interrupt_events_reported(self):
        """ADHD early-exits should appear in the report."""
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "audit.jsonl"
            lines = [_make_retrieval(2, interrupts=3), _make_retrieval(1)]
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            report = savings_report(log)

        self.assertIn("3", report)   # 3 interrupt events
        self.assertIn("ADHD", report)


# ---------------------------------------------------------------------------
# retrieval_report
# ---------------------------------------------------------------------------

class TestRetrievalReport(unittest.TestCase):

    def test_no_file_returns_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "missing.jsonl"
            report = retrieval_report(log)
            self.assertIn("No retrieval records yet", report)

    def test_basic_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "audit.jsonl"
            lines = [_make_retrieval(2), _make_retrieval(0), _make_retrieval(3)]
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            report = retrieval_report(log)

        self.assertIn("Total retrievals logged", report)
        self.assertIn("3", report)
        self.assertIn("Zero-result", report)


if __name__ == "__main__":
    unittest.main()
