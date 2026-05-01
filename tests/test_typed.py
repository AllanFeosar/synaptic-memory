"""
Tests for the pure-logic parts of typed (no mempalace required).

Run:
    cd outputs
    python -m pytest tests/test_typed.py -v
    # or
    python tests/test_typed.py
"""

from __future__ import annotations

import datetime as _dt
import sys
import unittest
from pathlib import Path

# Make typed importable from sibling dir
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from typed.client import MockClient
from typed.types import (
    Confidence,
    DrawerType,
    TypedDrawer,
    parse_drawer,
    serialize_drawer,
)
from typed.write import (
    DuplicateDrawerError,
    write_decision,
    write_drawer,
)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class TestTypes(unittest.TestCase):

    def test_make_drawer_basic(self):
        d = TypedDrawer.new(
            type=DrawerType.DECISION,
            scope="auth",
            confidence=Confidence.HIGH,
            body="We chose JWT over session cookies because we have 8 services.",
        )
        self.assertEqual(d.type, DrawerType.DECISION)
        self.assertEqual(d.scope, "auth")
        self.assertEqual(d.confidence, Confidence.HIGH)
        self.assertTrue(d.drawer_id.startswith("drw_"))
        self.assertIn("auth", d.drawer_id)

    def test_empty_body_rejected(self):
        with self.assertRaises(ValueError):
            TypedDrawer.new(type=DrawerType.DECISION, scope="x", confidence=Confidence.HIGH, body="")

    def test_empty_scope_rejected(self):
        with self.assertRaises(ValueError):
            TypedDrawer.new(type=DrawerType.DECISION, scope="", confidence=Confidence.HIGH, body="x")

    def test_string_enum_parsing(self):
        d = TypedDrawer.new(type="anti-pattern", scope="api", confidence="low", body="don't do X")
        self.assertEqual(d.type, DrawerType.ANTI_PATTERN)
        self.assertEqual(d.confidence, Confidence.LOW)


class TestSalience(unittest.TestCase):

    def _drawer(self, **kw):
        return TypedDrawer(
            drawer_id="drw_test",
            type=DrawerType.DECISION,
            scope="x",
            confidence=Confidence.HIGH,
            body="b",
            created_at=_dt.datetime.now(_dt.timezone.utc),
            **kw,
        )

    def test_pinned_dominates_recent(self):
        recent = self._drawer(usage_count=0)
        pinned = self._drawer(usage_count=0, pinned=True)
        self.assertGreater(pinned.salience(), recent.salience())

    def test_usage_increases_salience(self):
        cold = self._drawer(usage_count=0)
        hot = self._drawer(usage_count=10)
        self.assertGreater(hot.salience(), cold.salience())

    def test_stale_decreases_salience(self):
        fresh = self._drawer(usage_count=5)
        stale = self._drawer(usage_count=5, stale=True)
        self.assertGreater(fresh.salience(), stale.salience())

    def test_cite_then_correct_penalizes(self):
        good = self._drawer(usage_count=5, cite_then_correct=0)
        bad = self._drawer(usage_count=5, cite_then_correct=3)
        self.assertGreater(good.salience(), bad.salience())

    def test_recency_decay(self):
        now = _dt.datetime.now(_dt.timezone.utc)
        fresh = TypedDrawer(
            drawer_id="d1", type=DrawerType.DECISION, scope="x",
            confidence=Confidence.HIGH, body="b",
            created_at=now,
        )
        old = TypedDrawer(
            drawer_id="d2", type=DrawerType.DECISION, scope="x",
            confidence=Confidence.HIGH, body="b",
            created_at=now - _dt.timedelta(days=200),
        )
        self.assertGreater(fresh.salience(), old.salience())


class TestSerialize(unittest.TestCase):

    def test_roundtrip(self):
        d1 = TypedDrawer.new(
            type=DrawerType.DECISION,
            scope="auth-svc",
            confidence=Confidence.HIGH,
            body="Use JWT.\nMulti-line bodies are fine.",
            supersedes="drw_old",
        )
        text = serialize_drawer(d1)
        self.assertIn("---", text)
        self.assertIn("type: decision", text)
        self.assertIn("supersedes: drw_old", text)
        d2 = parse_drawer(text)
        self.assertEqual(d2.drawer_id, d1.drawer_id)
        self.assertEqual(d2.type, d1.type)
        self.assertEqual(d2.scope, d1.scope)
        self.assertEqual(d2.confidence, d1.confidence)
        self.assertEqual(d2.body, d1.body)
        self.assertEqual(d2.supersedes, d1.supersedes)

    def test_parse_rejects_no_frontmatter(self):
        with self.assertRaises(ValueError):
            parse_drawer("just plain text, no frontmatter")

    def test_parse_rejects_missing_required(self):
        text = "---\ntype: decision\nscope: x\n---\nbody"  # missing drawer_id, confidence
        with self.assertRaises(ValueError):
            parse_drawer(text)

    def test_parse_handles_optional_telemetry(self):
        text = (
            "---\n"
            "drawer_id: drw_1\n"
            "type: decision\n"
            "scope: x\n"
            "confidence: high\n"
            "usage_count: 7\n"
            "cite_then_correct: 2\n"
            "stale: true\n"
            "pinned: false\n"
            "---\n"
            "body"
        )
        d = parse_drawer(text)
        self.assertEqual(d.usage_count, 7)
        self.assertEqual(d.cite_then_correct, 2)
        self.assertTrue(d.stale)
        self.assertFalse(d.pinned)


class TestSummary(unittest.TestCase):

    def test_summary_includes_id_and_type(self):
        d = TypedDrawer.new(
            type=DrawerType.ANTI_PATTERN, scope="api",
            confidence=Confidence.HIGH,
            body="Don't use bare except in async code; it swallows CancelledError.",
        )
        s = d.summary()
        self.assertIn(d.drawer_id, s)
        self.assertIn("anti-pattern/api", s)
        self.assertIn("Don't use bare except", s)

    def test_summary_flags_low_confidence(self):
        d = TypedDrawer.new(
            type=DrawerType.DECISION, scope="x",
            confidence=Confidence.LOW, body="Unsure but tentatively chose Y",
        )
        s = d.summary()
        self.assertIn("low_conf", s)

    def test_summary_flags_stale(self):
        d = TypedDrawer.new(
            type=DrawerType.DECISION, scope="x",
            confidence=Confidence.HIGH, body="x",
        )
        d.stale = True
        s = d.summary()
        self.assertIn("STALE", s)

    def test_summary_truncates(self):
        d = TypedDrawer.new(
            type=DrawerType.DECISION, scope="x",
            confidence=Confidence.HIGH,
            body="A" * 500,
        )
        s = d.summary(max_chars=100)
        self.assertLess(len(s), 200)  # summary header + truncated body


# ---------------------------------------------------------------------------
# Write API (with MockClient)
# ---------------------------------------------------------------------------

class TestWriteAPI(unittest.TestCase):

    def test_basic_write(self):
        client = MockClient()
        d = write_decision(
            scope="auth",
            body="Use JWT for stateless auth across microservices.",
            client=client,
        )
        self.assertEqual(d.type, DrawerType.DECISION)
        self.assertEqual(d.scope, "auth")
        self.assertEqual(len(client.store), 1)
        # Round-trip: stored content should parse back
        stored_content = list(client.store.values())[0][2]
        parsed = parse_drawer(stored_content)
        self.assertEqual(parsed.drawer_id, d.drawer_id)

    def test_dupe_detection(self):
        client = MockClient()
        body = "Use JWT for stateless auth across microservices."
        write_decision(scope="auth", body=body, client=client)
        # MockClient uses substring scoring; passing the SAME body will produce
        # a high "score" relative to its threshold mapping. Force a hit by
        # lowering the threshold for the test.
        with self.assertRaises(DuplicateDrawerError):
            write_decision(scope="auth", body=body, client=client)
            # NOTE: Actual mempalace embedding-based threshold is 0.92; for the
            # MockClient's substring counter, even a score of 1+ counts.
            # We override the threshold via write_drawer below to be explicit.

    def test_skip_dupe_check(self):
        client = MockClient()
        body = "Session ended at 14:00 with two decisions."
        write_drawer(
            type=DrawerType.SUMMARY, scope="session", confidence=Confidence.MEDIUM,
            body=body, client=client, skip_dupe_check=True,
        )
        write_drawer(
            type=DrawerType.SUMMARY, scope="session", confidence=Confidence.MEDIUM,
            body=body, client=client, skip_dupe_check=True,
        )
        self.assertEqual(len(client.store), 2)


# ---------------------------------------------------------------------------
# Budget math (without writing files)
# ---------------------------------------------------------------------------

class TestBudgetMath(unittest.TestCase):

    def test_record_session_appends_to_log(self):
        from typed.budget import record_session, _load_records
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "budget.jsonl"
            record_session(tokens_in=10, tokens_out=5, log_path=log)
            record_session(tokens_in=20, tokens_out=10, log_path=log)
            recs = _load_records(log)
            self.assertEqual(len(recs), 2)
            self.assertEqual(recs[1].tokens_in, 20)


if __name__ == "__main__":
    unittest.main()

