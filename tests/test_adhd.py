"""Tests for the ADHD InterruptLayer."""

from __future__ import annotations

import datetime as _dt
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from typed.adhd import ADHDConfig, ImpulsivityMode, InterruptLayer
from typed.client import MockClient
from typed.types import Confidence, DrawerType, MemoryTier, TypedDrawer
from typed.write import write_decision, write_drawer

_record_retrieval_patcher = mock.patch("typed.budget.record_retrieval")


def setUpModule():
    _record_retrieval_patcher.start()


def tearDownModule():
    _record_retrieval_patcher.stop()


def _make_drawer(drawer_id: str = "drw_test", body: str = "test body") -> TypedDrawer:
    return TypedDrawer(
        drawer_id=drawer_id,
        type=DrawerType.DECISION,
        scope="test",
        confidence=Confidence.HIGH,
        body=body,
        tier=MemoryTier.LONG_TERM,
        created_at=_dt.datetime.now(_dt.timezone.utc),
    )


class TestInterruptLayerDisabled(unittest.TestCase):

    def test_noop_when_disabled(self):
        config = ADHDConfig(enabled=False, level=0)
        layer = InterruptLayer(config)
        seeds = [(_make_drawer("drw_1"), 0.95)]
        layer.check_seeds(seeds)
        self.assertEqual(layer.events, [])
        ranked = [(_make_drawer("drw_2"), 0.5)]
        result = layer.post_merge(ranked)
        self.assertIs(result, ranked)

    def test_noop_default_config(self):
        config = ADHDConfig()
        layer = InterruptLayer(config)
        seeds = [(_make_drawer("drw_1"), 0.99)]
        layer.check_seeds(seeds)
        self.assertEqual(layer.events, [])


class TestImpulsivityModeOff(unittest.TestCase):

    def test_off_mode_no_interrupts_regardless_of_scores(self):
        config = ADHDConfig(enabled=True, level=0)
        self.assertEqual(config.impulsivity_mode, ImpulsivityMode.OFF)
        layer = InterruptLayer(config)
        seeds = [(_make_drawer("drw_1"), 0.99), (_make_drawer("drw_2"), 0.50)]
        layer.check_seeds(seeds)
        self.assertEqual(layer.events, [])


class TestThresholdInterrupt(unittest.TestCase):

    def test_fires_when_score_above_threshold(self):
        config = ADHDConfig(enabled=True, level=1, impulse_threshold=0.88)
        layer = InterruptLayer(config)
        seeds = [(_make_drawer("drw_high"), 0.92), (_make_drawer("drw_low"), 0.40)]
        layer.check_seeds(seeds)
        self.assertEqual(len(layer.events), 1)
        self.assertEqual(layer.events[0].kind, "threshold")
        self.assertEqual(layer.events[0].drawer.drawer_id, "drw_high")

    def test_does_not_fire_when_score_below_threshold(self):
        config = ADHDConfig(enabled=True, level=1, impulse_threshold=0.88)
        layer = InterruptLayer(config)
        seeds = [(_make_drawer("drw_1"), 0.85), (_make_drawer("drw_2"), 0.40)]
        layer.check_seeds(seeds)
        self.assertEqual(len(layer.events), 0)

    def test_fires_at_exact_threshold(self):
        config = ADHDConfig(enabled=True, level=1, impulse_threshold=0.88)
        layer = InterruptLayer(config)
        seeds = [(_make_drawer("drw_exact"), 0.88)]
        layer.check_seeds(seeds)
        self.assertEqual(len(layer.events), 1)

    def test_low_mode_only_fires_threshold_not_margin(self):
        config = ADHDConfig(enabled=True, level=1, impulse_threshold=0.88, impulse_margin=0.18)
        layer = InterruptLayer(config)
        # top1 - top2 = 0.75 - 0.50 = 0.25 > 0.18, but neither hits threshold
        seeds = [(_make_drawer("drw_1"), 0.75), (_make_drawer("drw_2"), 0.50)]
        layer.check_seeds(seeds)
        self.assertEqual(len(layer.events), 0)


class TestMarginInterrupt(unittest.TestCase):

    def test_fires_when_margin_exceeded_medium_mode(self):
        config = ADHDConfig(enabled=True, level=2, impulse_threshold=0.88, impulse_margin=0.18)
        layer = InterruptLayer(config)
        # top1 - top2 = 0.80 - 0.50 = 0.30 > 0.18, but top1 < threshold
        seeds = [(_make_drawer("drw_dominant"), 0.80), (_make_drawer("drw_weak"), 0.50)]
        layer.check_seeds(seeds)
        events = layer.events
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "margin")
        self.assertEqual(events[0].drawer.drawer_id, "drw_dominant")

    def test_no_margin_when_gap_too_small(self):
        config = ADHDConfig(enabled=True, level=2, impulse_threshold=0.88, impulse_margin=0.18)
        layer = InterruptLayer(config)
        seeds = [(_make_drawer("drw_1"), 0.60), (_make_drawer("drw_2"), 0.55)]
        layer.check_seeds(seeds)
        self.assertEqual(len(layer.events), 0)

    def test_threshold_plus_margin_no_duplicate(self):
        config = ADHDConfig(enabled=True, level=2, impulse_threshold=0.88, impulse_margin=0.18)
        layer = InterruptLayer(config)
        # drw_top hits threshold AND dominates by margin -- should appear once
        seeds = [(_make_drawer("drw_top"), 0.95), (_make_drawer("drw_low"), 0.50)]
        layer.check_seeds(seeds)
        ids = [e.drawer.drawer_id for e in layer.events]
        self.assertEqual(ids.count("drw_top"), 1)


class TestPostMerge(unittest.TestCase):

    def test_prepends_registered_hits_and_deduplicates(self):
        config = ADHDConfig(enabled=True, level=1, impulse_threshold=0.88)
        layer = InterruptLayer(config)

        drw_a = _make_drawer("drw_a")
        drw_b = _make_drawer("drw_b")
        drw_c = _make_drawer("drw_c")

        layer.check_seeds([(drw_a, 0.95)])

        ranked = [(drw_a, 0.70), (drw_b, 0.60), (drw_c, 0.50)]
        result = layer.post_merge(ranked)

        result_ids = [d.drawer_id for d, _ in result]
        self.assertEqual(result_ids[0], "drw_a")
        self.assertEqual(result_ids.count("drw_a"), 1)
        self.assertIn("drw_b", result_ids)
        self.assertIn("drw_c", result_ids)

    def test_post_merge_noop_when_no_events(self):
        config = ADHDConfig(enabled=True, level=1, impulse_threshold=0.88)
        layer = InterruptLayer(config)
        ranked = [(_make_drawer("drw_x"), 0.5)]
        result = layer.post_merge(ranked)
        self.assertEqual(result, ranked)


class TestEnvVarConfig(unittest.TestCase):

    def test_adhd_level_1_enables_impulse(self):
        with mock.patch.dict(os.environ, {"SYNAPTIC_ADHD_LEVEL": "1"}, clear=False):
            config = ADHDConfig.from_env()
            self.assertTrue(config.enabled)
            self.assertEqual(config.level, 1)
            self.assertEqual(config.impulsivity_mode, ImpulsivityMode.LOW)

    def test_adhd_level_0_disabled(self):
        with mock.patch.dict(os.environ, {"SYNAPTIC_ADHD_LEVEL": "0"}, clear=False):
            config = ADHDConfig.from_env()
            self.assertFalse(config.enabled)
            self.assertEqual(config.impulsivity_mode, ImpulsivityMode.OFF)

    def test_adhd_disable_overrides(self):
        with mock.patch.dict(os.environ, {"SYNAPTIC_ADHD_DISABLE": "1", "SYNAPTIC_ADHD_LEVEL": "3"}, clear=False):
            config = ADHDConfig.from_env()
            self.assertFalse(config.enabled)

    def test_no_env_vars_returns_default(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("SYNAPTIC_ADHD")}
        with mock.patch.dict(os.environ, env, clear=True):
            config = ADHDConfig.from_env()
            self.assertFalse(config.enabled)
            self.assertEqual(config.level, 0)


class TestSpreadingActivationIntegration(unittest.TestCase):

    def _populate(self, client: MockClient) -> None:
        write_decision(scope="auth", body="Use JWT for stateless auth.", client=client)
        write_drawer(
            type=DrawerType.DECISION, scope="auth", confidence=Confidence.HIGH,
            body="JWT refresh tokens rotate on each use.",
            client=client, skip_dupe_check=True,
        )

    def test_existing_search_unchanged_with_adhd_disabled(self):
        from typed.read import spreading_activation_search
        client = MockClient()
        self._populate(client)

        results_no_config = spreading_activation_search(
            query="JWT auth", scope="auth", top_k=5, client=client,
        )
        results_disabled = spreading_activation_search(
            query="JWT auth", scope="auth", top_k=5, client=client,
            adhd_config=ADHDConfig(enabled=False),
        )
        self.assertEqual(
            [d.drawer_id for d in results_no_config],
            [d.drawer_id for d in results_disabled],
        )

    def test_existing_search_unchanged_with_default_config(self):
        from typed.read import spreading_activation_search
        client = MockClient()
        self._populate(client)

        results_none = spreading_activation_search(
            query="JWT auth", scope="auth", top_k=5, client=client,
        )
        results_default = spreading_activation_search(
            query="JWT auth", scope="auth", top_k=5, client=client,
            adhd_config=ADHDConfig(),
        )
        self.assertEqual(
            [d.drawer_id for d in results_none],
            [d.drawer_id for d in results_default],
        )

    def test_enabled_config_still_returns_results(self):
        from typed.read import spreading_activation_search
        client = MockClient()
        self._populate(client)

        config = ADHDConfig(enabled=True, level=1, impulse_threshold=0.88)
        results = spreading_activation_search(
            query="JWT auth", scope="auth", top_k=5, client=client,
            adhd_config=config,
        )
        self.assertGreater(len(results), 0)
        for d in results:
            self.assertIsInstance(d, TypedDrawer)


if __name__ == "__main__":
    unittest.main()
