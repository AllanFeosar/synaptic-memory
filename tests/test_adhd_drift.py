"""Tests for the ADHD Module 2 QueryDriftLayer."""

from __future__ import annotations

import datetime as _dt
import random
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from typed.adhd import ADHDConfig
from typed.adhd_drift import (
    DRIFT_DISCOUNT,
    QueryDriftLayer,
    _boltzmann_choice,
    _calibrate_salience_gate,
    _tail_saliences,
    reset_gate_calibration,
)
from typed.client import MockClient
from typed.types import Confidence, DrawerType, MemoryTier, TypedDrawer
from typed.write import write_decision, write_drawer

# Keep gate calibration from reading the real audit log during tests: force the
# tail reader to return nothing so _calibrate_salience_gate yields None (fixed
# fallback). Adaptive-path tests override this locally.
_tail_patcher = mock.patch("typed.adhd_drift._tail_saliences", return_value=[])


def setUpModule():
    _tail_patcher.start()
    reset_gate_calibration()


def tearDownModule():
    _tail_patcher.stop()
    reset_gate_calibration()


def _make_drawer(
    drawer_id: str = "drw_test",
    body: str = "test body",
    *,
    pinned: bool = False,
    usage_count: int = 0,
    created_at: _dt.datetime | None = None,
) -> TypedDrawer:
    return TypedDrawer(
        drawer_id=drawer_id,
        type=DrawerType.DECISION,
        scope="test",
        confidence=Confidence.HIGH,
        body=body,
        tier=MemoryTier.LONG_TERM,
        created_at=created_at or _dt.datetime.now(_dt.timezone.utc),
        pinned=pinned,
        usage_count=usage_count,
    )


# Fresh, non-pinned, unused → salience ~1.0 (below the 1.5 gate).
# Pinned → salience ~6.0 (above the gate).
def _low_salience(drawer_id="drw_low", body="low"):
    return _make_drawer(drawer_id, body)


def _high_salience(drawer_id="drw_high", body="high"):
    return _make_drawer(drawer_id, body, pinned=True)


class _SeqRandom:
    """Deterministic RNG stub: pops queued values from random()."""

    def __init__(self, values):
        self._values = list(values)

    def random(self):
        return self._values.pop(0) if self._values else 0.0

    def randrange(self, n):
        return 0


# Boltzmann second-draw values that select each strategy from uniform [0,0,0]:
#   r = value * 3 ; index0 (temporal) r<=1 ; index1 (tangent) 1<r<=2 ; index2 (scope_escape) r<=3
_HIT = 0.0            # dice < p_inattention
_TEMPORAL = 0.10      # -> r=0.30 -> temporal
_TANGENT = 0.50       # -> r=1.50 -> tangent
_SCOPE = 0.90         # -> r=2.70 -> scope_escape


def _retrieve_returning(items, recorder=None):
    def _r(q, k, scope_escape=False):
        if recorder is not None:
            recorder.append({"q": q, "k": k, "scope_escape": scope_escape})
        return list(items)
    return _r


def _cfg(level=2, **kw):
    return ADHDConfig(enabled=True, level=level, **kw)


class TestDriftInactive(unittest.TestCase):

    def test_noop_below_level_2(self):
        layer = QueryDriftLayer(_cfg(level=1), rng=_SeqRandom([_HIT, _TANGENT]))
        activation = {}
        layer.maybe_drift("q", [(_high_salience(), 0.9)], _retrieve_returning([]), activation)
        self.assertEqual(layer.events, [])
        self.assertEqual(activation, {})

    def test_noop_when_disabled(self):
        layer = QueryDriftLayer(ADHDConfig(enabled=False, level=2), rng=_SeqRandom([_HIT, _TANGENT]))
        layer.maybe_drift("q", [], _retrieve_returning([]), {})
        self.assertEqual(layer.events, [])


class TestDriftDice(unittest.TestCase):

    def test_dice_miss_no_drift(self):
        # random() returns 0.9 >= p_inattention(0.05) → skip
        layer = QueryDriftLayer(_cfg(), rng=_SeqRandom([0.9]))
        recorder = []
        layer.maybe_drift("q", [], _retrieve_returning([_high_salience()], recorder), {})
        self.assertEqual(layer.events, [])
        self.assertEqual(recorder, [])  # no search consumed

    def test_dice_hit_creates_one_event(self):
        layer = QueryDriftLayer(_cfg(), rng=_SeqRandom([_HIT, _TANGENT]))
        frontier = [(_low_salience("drw_weak", body="weak tail here"), 0.2)]
        layer.maybe_drift("q", frontier, _retrieve_returning([(_high_salience(), 0.5)]), {})
        self.assertEqual(len(layer.events), 1)
        self.assertEqual(layer.events[0].strategy, "tangent")


class TestStrategies(unittest.TestCase):

    def test_scope_escape_passes_flag(self):
        recorder = []
        layer = QueryDriftLayer(_cfg(), rng=_SeqRandom([_HIT, _SCOPE]))
        layer.maybe_drift("origq", [(_high_salience(), 0.3)], _retrieve_returning([], recorder), {})
        self.assertEqual(layer.events[0].strategy, "scope_escape")
        self.assertTrue(recorder[0]["scope_escape"])
        self.assertEqual(recorder[0]["q"], "origq")

    def test_tangent_uses_weakest_frontier_tail(self):
        recorder = []
        strong = (_high_salience("drw_strong", "strong body"), 0.9)
        weak = (_make_drawer("drw_weak", "PREFIX_SKIP ... UNIQUE_TAIL_MARKER"), 0.1)
        # 18 == len("UNIQUE_TAIL_MARKER") → tail captures the marker but not the prefix
        layer = QueryDriftLayer(_cfg(drift_tangent_chars=18), rng=_SeqRandom([_HIT, _TANGENT]))
        layer.maybe_drift("origq", [strong, weak], _retrieve_returning([], recorder), {})
        self.assertEqual(layer.events[0].strategy, "tangent")
        self.assertIn("UNIQUE_TAIL_MARKER", recorder[0]["q"])
        self.assertNotIn("PREFIX_SKIP", recorder[0]["q"])  # proves it used the tail
        self.assertFalse(recorder[0]["scope_escape"])

    def test_tangent_empty_frontier_skips(self):
        layer = QueryDriftLayer(_cfg(), rng=_SeqRandom([_HIT, _TANGENT]))
        recorder = []
        layer.maybe_drift("q", [], _retrieve_returning([], recorder), {})
        self.assertEqual(layer.events, [])  # no query built → no drift
        self.assertEqual(recorder, [])

    def test_temporal_uses_original_query(self):
        recorder = []
        layer = QueryDriftLayer(_cfg(), rng=_SeqRandom([_HIT, _TEMPORAL]))
        layer.maybe_drift("origq", [(_high_salience(), 0.5)], _retrieve_returning([], recorder), {})
        self.assertEqual(layer.events[0].strategy, "temporal")
        self.assertEqual(recorder[0]["q"], "origq")
        self.assertFalse(recorder[0]["scope_escape"])


class TestSalienceGateAndMerge(unittest.TestCase):

    def test_low_salience_discarded(self):
        # explicit fixed gate of 1.5 so a fresh (salience ~1.0) drawer is below it
        layer = QueryDriftLayer(_cfg(drift_adaptive_gate=False, drift_salience_gate=1.5),
                                rng=_SeqRandom([_HIT, _TEMPORAL]))
        activation = {}
        layer.maybe_drift("q", [(_high_salience(), 0.5)],
                          _retrieve_returning([(_low_salience(), 0.5)]), activation)
        self.assertEqual(layer.events[0].kept, 0)
        self.assertEqual(activation, {})

    def test_high_salience_kept_and_discounted(self):
        layer = QueryDriftLayer(_cfg(), rng=_SeqRandom([_HIT, _TEMPORAL]))
        activation = {}
        d = _high_salience("drw_keep")
        layer.maybe_drift("q", [(_high_salience("drw_seed"), 0.5)],
                          _retrieve_returning([(d, 0.5)]), activation)
        self.assertEqual(layer.events[0].kept, 1)
        self.assertIn("drw_keep", activation)
        self.assertAlmostEqual(activation["drw_keep"][1], 0.5 * DRIFT_DISCOUNT)

    def test_merge_does_not_override_existing(self):
        layer = QueryDriftLayer(_cfg(), rng=_SeqRandom([_HIT, _TEMPORAL]))
        existing = _high_salience("drw_dup")
        activation = {"drw_dup": (existing, 0.99)}
        layer.maybe_drift("q", [(existing, 0.99)],
                          _retrieve_returning([(_high_salience("drw_dup"), 0.9)]), activation)
        self.assertEqual(layer.events[0].kept, 0)
        self.assertEqual(activation["drw_dup"][1], 0.99)  # untouched

    def test_cap_max_extra_drawers(self):
        layer = QueryDriftLayer(_cfg(max_extra_drawers=2), rng=_SeqRandom([_HIT, _TEMPORAL]))
        activation = {}
        hits = [(_high_salience(f"drw_{i}"), 0.5) for i in range(4)]
        layer.maybe_drift("q", [(_high_salience("seed"), 0.5)],
                          _retrieve_returning(hits), activation)
        self.assertEqual(layer.events[0].kept, 2)
        self.assertEqual(len(activation), 2)

    def test_temporal_prefers_oldest_under_cap(self):
        now = _dt.datetime.now(_dt.timezone.utc)
        old = _make_drawer("drw_old", "old", pinned=True, created_at=now - _dt.timedelta(days=30))
        new = _make_drawer("drw_new", "new", pinned=True, created_at=now)
        layer = QueryDriftLayer(_cfg(max_extra_drawers=1), rng=_SeqRandom([_HIT, _TEMPORAL]))
        activation = {}
        layer.maybe_drift("q", [(_high_salience("seed"), 0.5)],
                          _retrieve_returning([(new, 0.5), (old, 0.5)]), activation)
        self.assertIn("drw_old", activation)   # oldest-first under the cap
        self.assertNotIn("drw_new", activation)

    def test_retrieve_exception_swallowed(self):
        def _boom(q, k, scope_escape=False):
            raise RuntimeError("search down")
        layer = QueryDriftLayer(_cfg(), rng=_SeqRandom([_HIT, _TEMPORAL]))
        activation = {}
        layer.maybe_drift("q", [(_high_salience(), 0.5)], _boom, activation)  # must not raise
        self.assertEqual(layer.events[0].kept, 0)
        self.assertEqual(activation, {})


class TestDriftIntegration(unittest.TestCase):
    """End-to-end through spreading_activation_search (MockClient, record_retrieval patched)."""

    def _client(self):
        client = MockClient()
        write_decision(scope="auth", body="Use JWT for stateless auth.", client=client)
        write_drawer(type=DrawerType.DECISION, scope="auth", confidence=Confidence.HIGH,
                     body="JWT refresh tokens rotate on each use.",
                     client=client, skip_dupe_check=True)
        return client

    def test_drift_events_recorded_at_level_2(self):
        from typed.read import spreading_activation_search
        client = self._client()
        captured = {}
        with mock.patch("typed.budget.record_retrieval", lambda **kw: captured.update(kw)):
            spreading_activation_search(
                query="JWT auth", scope="auth", top_k=3, client=client,
                adhd_config=ADHDConfig(enabled=True, level=2, p_inattention=1.0),
            )
        self.assertGreaterEqual(len(captured.get("drift_events", [])), 1)
        self.assertIn(captured["drift_events"][0]["strategy"], STRATEGIES_NAMES)

    def test_no_drift_at_level_1(self):
        from typed.read import spreading_activation_search
        client = self._client()
        captured = {}
        with mock.patch("typed.budget.record_retrieval", lambda **kw: captured.update(kw)):
            spreading_activation_search(
                query="JWT auth", scope="auth", top_k=3, client=client,
                adhd_config=ADHDConfig(enabled=True, level=1, p_inattention=1.0),
            )
        self.assertEqual(captured.get("drift_events"), [])


STRATEGIES_NAMES = ("temporal", "tangent", "scope_escape")


class TestAdaptiveGate(unittest.TestCase):
    """The drift salience gate self-calibrates from the audit log, like the interrupt threshold."""

    def setUp(self):
        reset_gate_calibration()

    def tearDown(self):
        reset_gate_calibration()

    def test_effective_gate_uses_calibration_when_available(self):
        with mock.patch("typed.adhd_drift._calibrate_salience_gate", return_value=0.72):
            layer = QueryDriftLayer(_cfg())  # active + adaptive on
            self.assertAlmostEqual(layer.effective_gate, 0.72)

    def test_falls_back_to_fixed_when_no_calibration(self):
        with mock.patch("typed.adhd_drift._calibrate_salience_gate", return_value=None):
            layer = QueryDriftLayer(_cfg(drift_salience_gate=0.5))
            self.assertAlmostEqual(layer.effective_gate, 0.5)

    def test_adaptive_off_ignores_calibration(self):
        with mock.patch("typed.adhd_drift._calibrate_salience_gate", return_value=0.9):
            layer = QueryDriftLayer(_cfg(drift_adaptive_gate=False, drift_salience_gate=0.3))
            self.assertAlmostEqual(layer.effective_gate, 0.3)

    def test_inactive_layer_uses_fixed_gate(self):
        layer = QueryDriftLayer(_cfg(level=1, drift_salience_gate=0.4))
        self.assertAlmostEqual(layer.effective_gate, 0.4)

    def test_calibrate_percentile_from_saliences(self):
        data = [float(i) for i in range(100)]  # salience values 0..99
        with mock.patch("typed.adhd_drift._tail_saliences", side_effect=[data, [], [], []]):
            g = _calibrate_salience_gate(percentile=0.25, window=400, min_samples=30)
        self.assertEqual(g, 25.0)  # 25th percentile of 0..99

    def test_calibrate_none_when_insufficient(self):
        with mock.patch("typed.adhd_drift._tail_saliences", side_effect=[[1.0, 2.0], [], [], []]):
            g = _calibrate_salience_gate(min_samples=30)
        self.assertIsNone(g)

    def test_adaptive_gate_admits_fresh_drawer(self):
        # calibrated gate 0.9 < fresh salience (~1.0) → fresh bridge is now KEPT,
        # which the old fixed 1.5 gate rejected. This is the fix.
        with mock.patch("typed.adhd_drift._calibrate_salience_gate", return_value=0.9):
            layer = QueryDriftLayer(_cfg(), rng=_SeqRandom([_HIT, _TEMPORAL]))
            activation = {}
            layer.maybe_drift("q", [(_high_salience(), 0.5)],
                              _retrieve_returning([(_low_salience("drw_fresh"), 0.5)]), activation)
        self.assertEqual(layer.events[0].kept, 1)
        self.assertIn("drw_fresh", activation)


class TestTailSaliences(unittest.TestCase):

    def test_reads_salience_from_results(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "audit.jsonl"
            p.write_text(
                json.dumps({"results": [{"salience": 1.2}, {"salience": 0.4}]}) + "\n"
                + json.dumps({"results": [{"salience": 3.0}]}) + "\n",
                encoding="utf-8",
            )
            vals = _tail_saliences(p, need=10)
        self.assertEqual(sorted(vals), [0.4, 1.2, 3.0])

    def test_missing_file_returns_empty(self):
        self.assertEqual(_tail_saliences(Path("/no/such/file.jsonl"), need=10), [])


class TestBoltzmann(unittest.TestCase):

    def test_uniform_logits_hit_all_indices(self):
        rng = random.Random(1234)
        seen = {_boltzmann_choice([0.0, 0.0, 0.0], 1.5, rng) for _ in range(300)}
        self.assertEqual(seen, {0, 1, 2})

    def test_temperature_low_favors_max_logit(self):
        rng = random.Random(7)
        picks = [_boltzmann_choice([0.0, 5.0, 0.0], 0.2, rng) for _ in range(200)]
        # index 1 has the dominant logit at low temperature
        self.assertGreater(picks.count(1), 150)


if __name__ == "__main__":
    unittest.main()
