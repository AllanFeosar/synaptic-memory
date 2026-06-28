"""
Tests for typed/telemetry.py — trust calibration feedback loop.

Covers:
  - mark_correction increments cite_then_correct
  - mark_correction auto-demotes at threshold
  - mark_correction on missing drawer_id is a no-op
  - mark_useful increments usage_count
  - mark_useful on missing drawer_id returns 0

Run:
    python -m pytest tests/test_telemetry.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from typed.client import MockClient
from typed.config import SynapticConfig, TelemetryConfig
from typed.telemetry import mark_correction, mark_useful
from typed.types import (
    Confidence,
    DrawerType,
    TypedDrawer,
    parse_drawer,
    serialize_drawer,
)
from typed.write import write_drawer

# Suppress retrieval-audit writes during tests.
_record_retrieval_patcher = mock.patch("typed.budget.record_retrieval")


def setUpModule():
    _record_retrieval_patcher.start()


def tearDownModule():
    _record_retrieval_patcher.stop()


def _default_config(**telemetry_overrides):
    """Build a SynapticConfig with optional telemetry overrides."""
    cfg = SynapticConfig()
    for k, v in telemetry_overrides.items():
        setattr(cfg.telemetry, k, v)
    return cfg


def _seed_drawer(client: MockClient, **drawer_kw) -> TypedDrawer:
    """Write a drawer into MockClient and return the TypedDrawer."""
    defaults = dict(
        type=DrawerType.DECISION,
        scope="auth",
        confidence=Confidence.HIGH,
        body="Use JWT for stateless auth across microservices.",
    )
    defaults.update(drawer_kw)
    return write_drawer(client=client, **defaults)


# ---------------------------------------------------------------------------
# mark_correction
# ---------------------------------------------------------------------------

class TestMarkCorrection(unittest.TestCase):

    def test_increments_cite_then_correct(self):
        """A single call should bump cite_then_correct from 0 to 1."""
        client = MockClient()
        d = _seed_drawer(client)

        with mock.patch("typed.telemetry.get_config", return_value=_default_config()):
            result = mark_correction([d.drawer_id], client=client)

        # Verify the returned dict
        self.assertIn(d.drawer_id, result)

        # Read back and verify the persisted value
        hit = client.get_drawer(d.drawer_id)
        updated = parse_drawer(hit.content)
        self.assertEqual(updated.cite_then_correct, 1)

    def test_increments_accumulate(self):
        """Calling mark_correction twice should yield cite_then_correct == 2."""
        client = MockClient()
        d = _seed_drawer(client)

        with mock.patch("typed.telemetry.get_config", return_value=_default_config()):
            mark_correction([d.drawer_id], client=client)
            mark_correction([d.drawer_id], client=client)

        hit = client.get_drawer(d.drawer_id)
        updated = parse_drawer(hit.content)
        self.assertEqual(updated.cite_then_correct, 2)

    def test_auto_demotes_at_default_threshold(self):
        """At threshold=2 (default), the second correction should demote to LOW."""
        client = MockClient()
        d = _seed_drawer(client, confidence=Confidence.HIGH)
        self.assertEqual(d.confidence, Confidence.HIGH)

        with mock.patch("typed.telemetry.get_config", return_value=_default_config()):
            mark_correction([d.drawer_id], client=client)  # cite_then_correct=1
            result = mark_correction([d.drawer_id], client=client)  # cite_then_correct=2

        self.assertEqual(result[d.drawer_id], "low")
        hit = client.get_drawer(d.drawer_id)
        updated = parse_drawer(hit.content)
        self.assertEqual(updated.confidence, Confidence.LOW)

    def test_auto_demotes_at_custom_threshold(self):
        """With threshold=1, a single correction should demote to LOW."""
        client = MockClient()
        d = _seed_drawer(client, confidence=Confidence.MEDIUM)

        cfg = _default_config(auto_demote_threshold=1)
        with mock.patch("typed.telemetry.get_config", return_value=cfg):
            result = mark_correction([d.drawer_id], client=client)

        self.assertEqual(result[d.drawer_id], "low")

    def test_already_low_stays_low(self):
        """A drawer already at LOW should stay LOW (no crash or double-demote)."""
        client = MockClient()
        d = _seed_drawer(client, confidence=Confidence.LOW)

        with mock.patch("typed.telemetry.get_config", return_value=_default_config()):
            mark_correction([d.drawer_id], client=client)
            mark_correction([d.drawer_id], client=client)
            result = mark_correction([d.drawer_id], client=client)

        self.assertEqual(result[d.drawer_id], "low")

    def test_missing_drawer_id_is_noop(self):
        """Correcting a nonexistent drawer should silently skip it."""
        client = MockClient()

        with mock.patch("typed.telemetry.get_config", return_value=_default_config()):
            result = mark_correction(["drw_does_not_exist"], client=client)

        self.assertEqual(result, {})

    def test_multiple_drawers(self):
        """Correcting multiple drawers at once should update each independently."""
        client = MockClient()
        d1 = _seed_drawer(client, body="First decision about auth flow.")
        d2 = _seed_drawer(client, body="Second decision about token rotation.", scope="tokens")

        with mock.patch("typed.telemetry.get_config", return_value=_default_config()):
            result = mark_correction([d1.drawer_id, d2.drawer_id], client=client)

        self.assertIn(d1.drawer_id, result)
        self.assertIn(d2.drawer_id, result)

        for did in (d1.drawer_id, d2.drawer_id):
            hit = client.get_drawer(did)
            updated = parse_drawer(hit.content)
            self.assertEqual(updated.cite_then_correct, 1)

    def test_mix_existing_and_missing(self):
        """Existing drawers are updated; missing ones are silently skipped."""
        client = MockClient()
        d = _seed_drawer(client)

        with mock.patch("typed.telemetry.get_config", return_value=_default_config()):
            result = mark_correction([d.drawer_id, "drw_ghost"], client=client)

        self.assertIn(d.drawer_id, result)
        self.assertNotIn("drw_ghost", result)


# ---------------------------------------------------------------------------
# mark_useful
# ---------------------------------------------------------------------------

class TestMarkUseful(unittest.TestCase):

    def test_increments_usage_count(self):
        """A single call should bump usage_count from 0 to 1."""
        client = MockClient()
        d = _seed_drawer(client)

        n = mark_useful([d.drawer_id], client=client)

        self.assertEqual(n, 1)
        hit = client.get_drawer(d.drawer_id)
        updated = parse_drawer(hit.content)
        self.assertEqual(updated.usage_count, 1)

    def test_increments_accumulate(self):
        """Multiple calls should accumulate."""
        client = MockClient()
        d = _seed_drawer(client)

        mark_useful([d.drawer_id], client=client)
        mark_useful([d.drawer_id], client=client)
        mark_useful([d.drawer_id], client=client)

        hit = client.get_drawer(d.drawer_id)
        updated = parse_drawer(hit.content)
        self.assertEqual(updated.usage_count, 3)

    def test_missing_drawer_returns_zero(self):
        """Marking a nonexistent drawer should return 0 updated."""
        client = MockClient()
        n = mark_useful(["drw_nonexistent"], client=client)
        self.assertEqual(n, 0)

    def test_multiple_drawers(self):
        """Should return count of successfully updated drawers."""
        client = MockClient()
        d1 = _seed_drawer(client, body="Decision about caching strategy.")
        d2 = _seed_drawer(client, body="Decision about retry logic.", scope="infra")

        n = mark_useful([d1.drawer_id, d2.drawer_id], client=client)

        self.assertEqual(n, 2)
        for did in (d1.drawer_id, d2.drawer_id):
            hit = client.get_drawer(did)
            updated = parse_drawer(hit.content)
            self.assertEqual(updated.usage_count, 1)

    def test_does_not_change_confidence(self):
        """mark_useful should never alter confidence."""
        client = MockClient()
        d = _seed_drawer(client, confidence=Confidence.HIGH)

        mark_useful([d.drawer_id], client=client)

        hit = client.get_drawer(d.drawer_id)
        updated = parse_drawer(hit.content)
        self.assertEqual(updated.confidence, Confidence.HIGH)

    def test_empty_list_returns_zero(self):
        """Empty input should return 0 without error."""
        client = MockClient()
        n = mark_useful([], client=client)
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
