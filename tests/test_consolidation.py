"""
Tests for the consolidation module (typed/consolidate.py).

Run:
    cd synaptic-memory
    python -m pytest tests/test_consolidation.py -v
    # or
    python tests/test_consolidation.py
"""

from __future__ import annotations

import datetime as _dt
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Make typed importable from sibling dir
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from typed.client import MockClient, SearchHit
from typed.config import ConsolidationConfig, SynapticConfig
from typed.consolidate import (
    ConsolidateReport,
    _archive_old,
    _detect_contradictions,
    _rerank_and_pin,
    consolidate,
)
from typed.types import (
    Confidence,
    DrawerType,
    MemoryTier,
    TypedDrawer,
    serialize_drawer,
)


# Patch record_retrieval globally so MockClient searches don't pollute audit log.
_record_retrieval_patcher = mock.patch("typed.budget.record_retrieval")


def setUpModule():
    _record_retrieval_patcher.start()


def tearDownModule():
    _record_retrieval_patcher.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**consolidation_overrides) -> SynapticConfig:
    """Build a SynapticConfig with custom consolidation tunables."""
    cfg = SynapticConfig()
    for k, v in consolidation_overrides.items():
        setattr(cfg.consolidation, k, v)
    return cfg


def _aged_drawer(
    tier: MemoryTier,
    age_days: float,
    usage: int = 0,
    pinned: bool = False,
    dtype: DrawerType = DrawerType.DECISION,
    scope: str = "x",
    body: str = "body",
) -> TypedDrawer:
    """Create a TypedDrawer with a controlled creation timestamp."""
    now = _dt.datetime.now(_dt.timezone.utc)
    created = now - _dt.timedelta(days=age_days)
    return TypedDrawer(
        drawer_id=f"drw_test_{tier.value}_{age_days}_{usage}",
        type=dtype,
        scope=scope,
        confidence=Confidence.HIGH,
        body=body,
        tier=tier,
        created_at=created,
        usage_count=usage,
        pinned=pinned,
    )


def _populate_client(client: MockClient, drawers: list[TypedDrawer]) -> list[tuple[str, TypedDrawer]]:
    """Add drawers to a MockClient and return (mempalace_id, drawer) pairs."""
    items = []
    for d in drawers:
        mid = client.add_drawer(d.scope, "general", serialize_drawer(d))
        items.append((mid, d))
    return items


# ---------------------------------------------------------------------------
# Archive tests
# ---------------------------------------------------------------------------

class TestArchiveOld(unittest.TestCase):
    """Tests for _archive_old: TTL-based archival with tier/pin/usage rules."""

    def _run_archive(self, drawers, **config_kw):
        """Helper: populate client, run _archive_old, return report."""
        client = MockClient()
        items = _populate_client(client, drawers)
        report = ConsolidateReport(started_at="test")
        cfg = _make_config(**config_kw)
        with mock.patch("typed.consolidate.get_config", return_value=cfg):
            _archive_old(items, client, report)
        return report, client

    def test_ephemeral_archived_after_1_day(self):
        d = _aged_drawer(MemoryTier.EPHEMERAL, age_days=2, usage=5)
        report, _ = self._run_archive([d])
        self.assertIn(d.drawer_id, report.archived)

    def test_ephemeral_not_archived_before_1_day(self):
        d = _aged_drawer(MemoryTier.EPHEMERAL, age_days=0.5, usage=0)
        report, _ = self._run_archive([d])
        self.assertNotIn(d.drawer_id, report.archived)

    def test_long_term_archived_after_90_days_if_unused(self):
        d = _aged_drawer(MemoryTier.LONG_TERM, age_days=91, usage=0)
        report, _ = self._run_archive([d])
        self.assertIn(d.drawer_id, report.archived)

    def test_long_term_survives_if_used(self):
        d = _aged_drawer(MemoryTier.LONG_TERM, age_days=91, usage=1)
        report, _ = self._run_archive([d])
        self.assertNotIn(d.drawer_id, report.archived)

    def test_permanent_never_archived(self):
        d = _aged_drawer(MemoryTier.PERMANENT, age_days=9999, usage=0)
        report, _ = self._run_archive([d])
        self.assertNotIn(d.drawer_id, report.archived)

    def test_pinned_exempt(self):
        d = _aged_drawer(MemoryTier.EPHEMERAL, age_days=100, usage=0, pinned=True)
        report, _ = self._run_archive([d])
        self.assertNotIn(d.drawer_id, report.archived)

    def test_forget_after_days_caps_ttl(self):
        """When forget_after_days < tier TTL, drawers archive sooner."""
        # Long-term TTL is 90 days, but config caps at 30.
        # Drawer aged 35 days, unused => should be archived.
        d = _aged_drawer(MemoryTier.LONG_TERM, age_days=35, usage=0)
        report, _ = self._run_archive([d], forget_after_days=30)
        self.assertIn(d.drawer_id, report.archived)

    def test_archived_drawer_scope_prefixed(self):
        """Archived drawers get scope prefixed with __archive__."""
        d = _aged_drawer(MemoryTier.EPHEMERAL, age_days=2, usage=0, scope="auth")
        client = MockClient()
        items = _populate_client(client, [d])
        report = ConsolidateReport(started_at="test")
        cfg = _make_config()
        with mock.patch("typed.consolidate.get_config", return_value=cfg):
            _archive_old(items, client, report)
        self.assertEqual(d.scope, "__archive__auth")


# ---------------------------------------------------------------------------
# Contradiction detection tests
# ---------------------------------------------------------------------------

class TestDetectContradictions(unittest.TestCase):
    """Tests for _detect_contradictions: opposing-type and same-type conflict detection."""

    def _run_detect(self, drawers, **config_kw):
        client = MockClient()
        items = _populate_client(client, drawers)
        report = ConsolidateReport(started_at="test")
        cfg = _make_config(
            contradiction_sim_threshold=0.01,  # low threshold so MockClient overlap scoring fires
            **config_kw,
        )
        with mock.patch("typed.consolidate.get_config", return_value=cfg):
            _detect_contradictions(items, client, report)
        return report

    def test_opposing_types_flagged(self):
        """DECISION + ANTI_PATTERN with similar content => contradiction."""
        d1 = _aged_drawer(
            MemoryTier.LONG_TERM, age_days=1,
            dtype=DrawerType.DECISION, scope="auth",
            body="Use JWT tokens for authentication across services",
        )
        d1.drawer_id = "drw_contra_decision"
        d2 = _aged_drawer(
            MemoryTier.LONG_TERM, age_days=1,
            dtype=DrawerType.ANTI_PATTERN, scope="auth",
            body="Do not use JWT tokens for authentication",
        )
        d2.drawer_id = "drw_contra_antipattern"
        report = self._run_detect([d1, d2])
        self.assertEqual(len(report.contradictions), 1)
        self.assertEqual(report.contradictions[0]["kind"], "opposing-type")

    def test_same_types_not_flagged_unless_both_high_decision(self):
        """Two PATTERNs with similar content => not a contradiction."""
        d1 = _aged_drawer(
            MemoryTier.LONG_TERM, age_days=1,
            dtype=DrawerType.PATTERN, scope="api",
            body="Always validate input parameters before processing",
        )
        d2 = _aged_drawer(
            MemoryTier.LONG_TERM, age_days=1,
            dtype=DrawerType.PATTERN, scope="api",
            body="Always validate input data before processing the request",
        )
        # Make drawer_ids unique
        d2.drawer_id = d1.drawer_id + "_2"
        report = self._run_detect([d1, d2])
        self.assertEqual(len(report.contradictions), 0)

    def test_non_contradiction_types_skipped(self):
        """SUMMARY and RECIPE types are not checked for contradictions."""
        d1 = _aged_drawer(
            MemoryTier.LONG_TERM, age_days=1,
            dtype=DrawerType.SUMMARY, scope="proj",
            body="Session ended with two decisions about JWT auth",
        )
        d2 = _aged_drawer(
            MemoryTier.LONG_TERM, age_days=1,
            dtype=DrawerType.RECIPE, scope="proj",
            body="Steps for JWT auth setup and token rotation",
        )
        report = self._run_detect([d1, d2])
        self.assertEqual(len(report.contradictions), 0)


# ---------------------------------------------------------------------------
# Rerank and pin tests
# ---------------------------------------------------------------------------

class TestRerankAndPin(unittest.TestCase):
    """Tests for _rerank_and_pin: auto-pins top fraction by salience."""

    def test_top_fraction_pinned(self):
        """With pin_top_fraction=0.5 and 4 drawers, top 2 should be pinned."""
        # Create drawers with varying usage to control salience ranking
        drawers = [
            _aged_drawer(MemoryTier.LONG_TERM, age_days=1, usage=10, scope="auth", body="high usage A"),
            _aged_drawer(MemoryTier.LONG_TERM, age_days=1, usage=8, scope="auth", body="high usage B"),
            _aged_drawer(MemoryTier.LONG_TERM, age_days=1, usage=0, scope="auth", body="low usage C"),
            _aged_drawer(MemoryTier.LONG_TERM, age_days=1, usage=0, scope="auth", body="low usage D"),
        ]
        # Ensure unique IDs
        for i, d in enumerate(drawers):
            d.drawer_id = f"drw_pin_test_{i}"

        client = MockClient()
        items = _populate_client(client, drawers)
        report = ConsolidateReport(started_at="test")
        cfg = _make_config(pin_top_fraction=0.5)
        with mock.patch("typed.consolidate.get_config", return_value=cfg):
            _rerank_and_pin(items, client, report)

        self.assertEqual(len(report.auto_pinned), 2)
        # The top 2 by salience (highest usage) should be pinned
        self.assertIn("drw_pin_test_0", report.auto_pinned)
        self.assertIn("drw_pin_test_1", report.auto_pinned)

    def test_already_pinned_not_re_pinned(self):
        """Drawers already pinned are not added to auto_pinned list."""
        d = _aged_drawer(MemoryTier.LONG_TERM, age_days=1, usage=10, pinned=True, scope="auth", body="already pinned")
        d.drawer_id = "drw_already_pinned"

        client = MockClient()
        items = _populate_client(client, [d])
        report = ConsolidateReport(started_at="test")
        cfg = _make_config(pin_top_fraction=1.0)
        with mock.patch("typed.consolidate.get_config", return_value=cfg):
            _rerank_and_pin(items, client, report)

        self.assertEqual(len(report.auto_pinned), 0)


# ---------------------------------------------------------------------------
# Partial failure in consolidate()
# ---------------------------------------------------------------------------

class TestConsolidatePartialFailure(unittest.TestCase):
    """consolidate() writes report with partial=True when an exception occurs mid-run."""

    @mock.patch("typed.consolidate._notify_errors")
    @mock.patch("typed.consolidate._enumerate_drawers", side_effect=RuntimeError("boom"))
    def test_partial_on_exception(self, _mock_enum, _mock_notify):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.json"
            client = MockClient()
            report = consolidate(client=client, report_path=report_path)
            self.assertTrue(report.partial)
            self.assertTrue(any("boom" in e for e in report.errors))
            # Report file should still be written
            self.assertTrue(report_path.exists())
            self.assertIsNotNone(report.finished_at)


# ---------------------------------------------------------------------------
# ConsolidateReport dataclass
# ---------------------------------------------------------------------------

class TestConsolidateReport(unittest.TestCase):

    def test_report_has_partial_field(self):
        r = ConsolidateReport(started_at="test")
        self.assertFalse(r.partial)

    def test_to_json_roundtrip(self):
        import json
        r = ConsolidateReport(started_at="2026-01-01T00:00:00Z", partial=True)
        r.archived = ["drw_1"]
        r.errors = ["something broke"]
        data = json.loads(r.to_json())
        self.assertTrue(data["partial"])
        self.assertEqual(data["archived"], ["drw_1"])


if __name__ == "__main__":
    unittest.main()
