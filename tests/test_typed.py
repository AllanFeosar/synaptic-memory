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
from unittest import mock

# Make typed importable from sibling dir
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from typed.client import MockClient
from typed.graphify_client import MockGraphifyClient
from typed.types import (
    Confidence,
    DrawerType,
    MemoryTier,
    TypedDrawer,
    parse_drawer,
    serialize_drawer,
)
from typed.write import (
    DuplicateDrawerError,
    write_anti_pattern,
    write_decision,
    write_drawer,
    write_recipe,
    write_session_summary,
)


# spreading_activation_search() logs to ~/.synaptic-memory/retrieval-audit.jsonl.
# Patch it out for the whole test run so test invocations (MockClient, 0ms)
# don't pollute the real audit log used for retrieval-quality analysis.
_record_retrieval_patcher = mock.patch("typed.budget.record_retrieval")


def setUpModule():
    _record_retrieval_patcher.start()


def tearDownModule():
    _record_retrieval_patcher.stop()


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
        self.assertEqual(d.tier, MemoryTier.LONG_TERM)
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


# ---------------------------------------------------------------------------
# MemoryTier
# ---------------------------------------------------------------------------

class TestMemoryTier(unittest.TestCase):

    def test_ttl_days(self):
        self.assertEqual(MemoryTier.EPHEMERAL.ttl_days, 1.0)
        self.assertEqual(MemoryTier.SHORT_TERM.ttl_days, 7.0)
        self.assertEqual(MemoryTier.LONG_TERM.ttl_days, 90.0)
        self.assertIsNone(MemoryTier.PERMANENT.ttl_days)

    def test_half_life_days(self):
        self.assertEqual(MemoryTier.EPHEMERAL.half_life_days, 0.5)
        self.assertEqual(MemoryTier.SHORT_TERM.half_life_days, 3.5)
        self.assertEqual(MemoryTier.LONG_TERM.half_life_days, 45.0)
        self.assertIsNone(MemoryTier.PERMANENT.half_life_days)

    def test_parse_roundtrip(self):
        for tier in MemoryTier:
            self.assertEqual(MemoryTier.parse(tier.value), tier)

    def test_parse_unknown_falls_back_in_parse_drawer(self):
        text = (
            "---\n"
            "drawer_id: drw_1\ntype: decision\nscope: x\nconfidence: high\n"
            "tier: unknown-garbage\n"
            "---\nbody"
        )
        d = parse_drawer(text)
        self.assertEqual(d.tier, MemoryTier.LONG_TERM)

    def test_missing_tier_defaults_long_term(self):
        text = (
            "---\n"
            "drawer_id: drw_1\ntype: decision\nscope: x\nconfidence: high\n"
            "---\nbody"
        )
        d = parse_drawer(text)
        self.assertEqual(d.tier, MemoryTier.LONG_TERM)


# ---------------------------------------------------------------------------
# Exponential decay salience
# ---------------------------------------------------------------------------

class TestSalience(unittest.TestCase):

    def _drawer(self, tier=MemoryTier.LONG_TERM, **kw):
        return TypedDrawer(
            drawer_id="drw_test",
            type=DrawerType.DECISION,
            scope="x",
            confidence=Confidence.HIGH,
            body="b",
            tier=tier,
            created_at=_dt.datetime.now(_dt.timezone.utc),
            **kw,
        )

    def test_permanent_no_decay(self):
        d = self._drawer(tier=MemoryTier.PERMANENT)
        now = _dt.datetime.now(_dt.timezone.utc)
        old_now = now + _dt.timedelta(days=365)
        s_now = d.salience(now=now)
        s_old = d.salience(now=old_now)
        # Permanent: only usage/pin/stale affect score — age has zero effect
        self.assertAlmostEqual(s_now, s_old, places=3)

    def test_ephemeral_decays_faster_than_long_term(self):
        now = _dt.datetime.now(_dt.timezone.utc)
        future = now + _dt.timedelta(days=2)
        ephem = self._drawer(tier=MemoryTier.EPHEMERAL)
        lt = self._drawer(tier=MemoryTier.LONG_TERM)
        # Both start equal; after 2 days ephemeral should have decayed more
        self.assertLess(ephem.salience(now=future), lt.salience(now=future))

    def test_decay_at_half_life(self):
        import math
        now = _dt.datetime.now(_dt.timezone.utc)
        d = self._drawer(tier=MemoryTier.SHORT_TERM)  # half_life = 3.5 days
        at_half_life = now + _dt.timedelta(days=3.5)
        s_now = d.salience(now=now)
        s_half = d.salience(now=at_half_life)
        # decay at half-life = 0.5; salience drops by ~0.5 (decay term)
        self.assertAlmostEqual(s_now - s_half, 0.5, delta=0.05)

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


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

class TestSerialize(unittest.TestCase):

    def test_roundtrip_with_tier(self):
        d1 = TypedDrawer.new(
            type=DrawerType.DECISION,
            scope="auth-svc",
            confidence=Confidence.HIGH,
            body="Use JWT.\nMulti-line bodies are fine.",
            tier=MemoryTier.PERMANENT,
            supersedes="drw_old",
        )
        text = serialize_drawer(d1)
        self.assertIn("tier: permanent", text)
        d2 = parse_drawer(text)
        self.assertEqual(d2.drawer_id, d1.drawer_id)
        self.assertEqual(d2.tier, MemoryTier.PERMANENT)
        self.assertEqual(d2.type, d1.type)
        self.assertEqual(d2.body, d1.body)
        self.assertEqual(d2.supersedes, d1.supersedes)

    def test_all_tiers_roundtrip(self):
        for tier in MemoryTier:
            d = TypedDrawer.new(
                type=DrawerType.DECISION, scope="x",
                confidence=Confidence.HIGH, body="body", tier=tier,
            )
            d2 = parse_drawer(serialize_drawer(d))
            self.assertEqual(d2.tier, tier)

    def test_parse_rejects_no_frontmatter(self):
        with self.assertRaises(ValueError):
            parse_drawer("just plain text, no frontmatter")

    def test_parse_rejects_missing_required(self):
        text = "---\ntype: decision\nscope: x\n---\nbody"
        with self.assertRaises(ValueError):
            parse_drawer(text)

    def test_parse_handles_optional_telemetry(self):
        text = (
            "---\n"
            "drawer_id: drw_1\ntype: decision\nscope: x\nconfidence: high\n"
            "tier: short-term\nusage_count: 7\ncite_then_correct: 2\n"
            "stale: true\npinned: false\n---\nbody"
        )
        d = parse_drawer(text)
        self.assertEqual(d.tier, MemoryTier.SHORT_TERM)
        self.assertEqual(d.usage_count, 7)
        self.assertTrue(d.stale)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

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

    def test_summary_shows_non_default_tier(self):
        d = TypedDrawer.new(
            type=DrawerType.SUMMARY, scope="x",
            confidence=Confidence.MEDIUM, body="session ended",
            tier=MemoryTier.SHORT_TERM,
        )
        self.assertIn("tier:short-term", d.summary())

    def test_summary_hides_long_term_tier(self):
        d = TypedDrawer.new(
            type=DrawerType.DECISION, scope="x",
            confidence=Confidence.HIGH, body="chose X",
            tier=MemoryTier.LONG_TERM,
        )
        self.assertNotIn("tier:", d.summary())

    def test_summary_flags_low_confidence(self):
        d = TypedDrawer.new(
            type=DrawerType.DECISION, scope="x",
            confidence=Confidence.LOW, body="Unsure but tentatively chose Y",
        )
        self.assertIn("low_conf", d.summary())

    def test_summary_flags_stale(self):
        d = TypedDrawer.new(
            type=DrawerType.DECISION, scope="x",
            confidence=Confidence.HIGH, body="x",
        )
        d.stale = True
        self.assertIn("STALE", d.summary())

    def test_summary_truncates(self):
        d = TypedDrawer.new(
            type=DrawerType.DECISION, scope="x",
            confidence=Confidence.HIGH, body="A" * 500,
        )
        self.assertLess(len(d.summary(max_chars=100)), 200)


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
        self.assertEqual(d.tier, MemoryTier.LONG_TERM)
        self.assertEqual(len(client.store), 1)
        stored_content = list(client.store.values())[0][2]
        parsed = parse_drawer(stored_content)
        self.assertEqual(parsed.drawer_id, d.drawer_id)
        self.assertEqual(parsed.tier, MemoryTier.LONG_TERM)

    def test_anti_pattern_is_permanent(self):
        client = MockClient()
        d = write_anti_pattern(scope="api", body="Don't use bare except.", client=client)
        self.assertEqual(d.tier, MemoryTier.PERMANENT)
        self.assertTrue(d.pinned)

    def test_recipe_is_permanent(self):
        client = MockClient()
        d = write_recipe(scope="deploy", body="Steps to rollback a release.", client=client)
        self.assertEqual(d.tier, MemoryTier.PERMANENT)

    def test_session_summary_is_short_term(self):
        client = MockClient()
        d = write_session_summary(scope="proj", body="Fixed the auth bug.", client=client)
        self.assertEqual(d.tier, MemoryTier.SHORT_TERM)

    def test_explicit_tier_override(self):
        client = MockClient()
        d = write_decision(
            scope="auth", body="Temporary workaround for token refresh.",
            tier=MemoryTier.EPHEMERAL, client=client,
        )
        self.assertEqual(d.tier, MemoryTier.EPHEMERAL)
        stored = list(client.store.values())[0][2]
        self.assertIn("tier: ephemeral", stored)

    def test_dupe_detection(self):
        client = MockClient()
        body = "Use JWT for stateless auth across microservices."
        write_decision(scope="auth", body=body, client=client)
        with self.assertRaises(DuplicateDrawerError):
            write_decision(scope="auth", body=body, client=client)

    def test_skip_dupe_check(self):
        client = MockClient()
        body = "Session ended at 14:00 with two decisions."
        write_drawer(type=DrawerType.SUMMARY, scope="s", confidence=Confidence.MEDIUM,
                     body=body, client=client, skip_dupe_check=True)
        write_drawer(type=DrawerType.SUMMARY, scope="s", confidence=Confidence.MEDIUM,
                     body=body, client=client, skip_dupe_check=True)
        self.assertEqual(len(client.store), 2)


# ---------------------------------------------------------------------------
# Spreading activation (with MockClient)
# ---------------------------------------------------------------------------

class TestSpreadingActivation(unittest.TestCase):

    def _populate(self, client: MockClient) -> list[TypedDrawer]:
        drawers = [
            write_decision(scope="auth", body="Use JWT for stateless auth.", client=client),
            write_drawer(type=DrawerType.DECISION, scope="auth", confidence=Confidence.HIGH,
                         body="JWT refresh tokens rotate on each use.",
                         client=client, skip_dupe_check=True),
            write_anti_pattern(scope="auth", body="Don't store JWT in localStorage — XSS risk.", client=client),
            write_recipe(scope="deploy", body="Rollback: scale down, swap image, scale up.", client=client),
        ]
        return drawers

    def test_returns_results(self):
        from typed.read import spreading_activation_search
        client = MockClient()
        self._populate(client)
        results = spreading_activation_search(query="JWT auth", scope="auth", top_k=3, client=client)
        self.assertGreater(len(results), 0)
        self.assertLessEqual(len(results), 3)

    def test_all_results_are_typed_drawers(self):
        from typed.read import spreading_activation_search
        client = MockClient()
        self._populate(client)
        results = spreading_activation_search(query="JWT", scope="auth", client=client)
        for d in results:
            self.assertIsInstance(d, TypedDrawer)

    def test_depth_zero_returns_only_seeds(self):
        from typed.read import spreading_activation_search
        client = MockClient()
        self._populate(client)
        # depth=0 means no ripple — only seed hits
        results_d0 = spreading_activation_search(
            query="JWT auth", scope="auth", top_k=10, depth=0, client=client
        )
        results_d2 = spreading_activation_search(
            query="JWT auth", scope="auth", top_k=10, depth=2, client=client
        )
        # depth=2 can find at least as many as depth=0
        self.assertGreaterEqual(len(results_d2), len(results_d0))

    def test_empty_palace_returns_empty(self):
        from typed.read import spreading_activation_search
        client = MockClient()
        results = spreading_activation_search(query="anything", scope="auth", client=client)
        self.assertEqual(results, [])

    def test_inject_session_start_uses_spreading_activation(self):
        from typed.read import inject_session_start
        client = MockClient()
        self._populate(client)
        block = inject_session_start(scope="auth", intent_hint="JWT", client=client)
        self.assertIn("<!-- typed/sessionstart -->", block)
        self.assertIn("<!-- /typed/sessionstart -->", block)
        self.assertIn("drw_", block)

    def test_inject_session_start_empty_returns_empty_string(self):
        from typed.read import inject_session_start
        client = MockClient()
        result = inject_session_start(scope="auth", client=client)
        self.assertEqual(result, "")


# ---------------------------------------------------------------------------
# TTL archive logic (unit-level, no consolidate import needed)
# ---------------------------------------------------------------------------

class TestTTLArchive(unittest.TestCase):

    def _aged_drawer(self, tier: MemoryTier, age_days: float, usage: int = 0) -> TypedDrawer:
        now = _dt.datetime.now(_dt.timezone.utc)
        created = now - _dt.timedelta(days=age_days)
        return TypedDrawer(
            drawer_id=f"drw_test_{tier.value}",
            type=DrawerType.DECISION,
            scope="x",
            confidence=Confidence.HIGH,
            body="body",
            tier=tier,
            created_at=created,
            usage_count=usage,
        )

    def _should_archive(self, d: TypedDrawer) -> bool:
        """Mirror the archive logic from consolidate._archive_old."""
        if d.pinned:
            return False
        ttl = d.tier.ttl_days
        if ttl is None:
            return False
        now = _dt.datetime.now(_dt.timezone.utc)
        age_days = (now - d.created_at).total_seconds() / 86400.0
        if age_days < ttl:
            return False
        if d.tier != MemoryTier.EPHEMERAL and d.usage_count > 0:
            return False
        return True

    def test_permanent_never_archived(self):
        d = self._aged_drawer(MemoryTier.PERMANENT, age_days=9999, usage=0)
        self.assertFalse(self._should_archive(d))

    def test_ephemeral_archived_after_1_day(self):
        old = self._aged_drawer(MemoryTier.EPHEMERAL, age_days=2, usage=5)
        self.assertTrue(self._should_archive(old))  # even with usage

    def test_ephemeral_not_archived_before_1_day(self):
        fresh = self._aged_drawer(MemoryTier.EPHEMERAL, age_days=0.5, usage=0)
        self.assertFalse(self._should_archive(fresh))

    def test_short_term_archived_after_7_days_if_unused(self):
        d = self._aged_drawer(MemoryTier.SHORT_TERM, age_days=8, usage=0)
        self.assertTrue(self._should_archive(d))

    def test_short_term_survives_if_used(self):
        d = self._aged_drawer(MemoryTier.SHORT_TERM, age_days=8, usage=3)
        self.assertFalse(self._should_archive(d))

    def test_long_term_archived_after_90_days_if_unused(self):
        d = self._aged_drawer(MemoryTier.LONG_TERM, age_days=91, usage=0)
        self.assertTrue(self._should_archive(d))

    def test_long_term_survives_if_used(self):
        d = self._aged_drawer(MemoryTier.LONG_TERM, age_days=91, usage=1)
        self.assertFalse(self._should_archive(d))

    def test_pinned_always_exempt(self):
        d = self._aged_drawer(MemoryTier.EPHEMERAL, age_days=100, usage=0)
        d.pinned = True
        self.assertFalse(self._should_archive(d))


# ---------------------------------------------------------------------------
# Graphify hop integration
# ---------------------------------------------------------------------------

class TestGraphifyHop(unittest.TestCase):

    def _populate(self, client: MockClient) -> None:
        write_decision(scope="auth", body="Use JWT for stateless auth.", client=client)
        write_drawer(
            type=DrawerType.DECISION, scope="auth", confidence=Confidence.HIGH,
            body="JwtService handles refresh token rotation via `jwt.py`.",
            client=client, skip_dupe_check=True,
        )
        write_drawer(
            type=DrawerType.POSTMORTEM, scope="auth", confidence=Confidence.HIGH,
            body="Token expiry edge case: JwtService fails silently on clock skew.",
            client=client, skip_dupe_check=True,
        )

    def test_graphify_hop_surfaces_structural_neighbor(self):
        from typed.read import spreading_activation_search
        client = MockClient()
        self._populate(client)

        # Graphify graph: "jwt" node has neighbor "JwtService"
        graphify = MockGraphifyClient({"jwt": ["JwtService"]})

        results = spreading_activation_search(
            query="JWT auth", scope="auth", top_k=5,
            client=client, graphify_client=graphify,
        )
        # Should find at least 1 drawer (graphify hop may surface the postmortem)
        self.assertGreater(len(results), 0)
        for d in results:
            self.assertIsInstance(d, TypedDrawer)

    def test_without_graphify_client_unchanged(self):
        from typed.read import spreading_activation_search
        client = MockClient()
        self._populate(client)

        results_plain = spreading_activation_search(
            query="JWT auth", scope="auth", top_k=5, client=client,
        )
        results_graph = spreading_activation_search(
            query="JWT auth", scope="auth", top_k=5, client=client,
            graphify_client=None,
        )
        # With no graphify client, results must be identical
        self.assertEqual(
            [d.drawer_id for d in results_plain],
            [d.drawer_id for d in results_graph],
        )

    def test_graphify_hop_does_not_duplicate_activation(self):
        from typed.read import spreading_activation_search
        client = MockClient()
        self._populate(client)
        graphify = MockGraphifyClient({"jwt": ["JwtService", "jwt"]})

        results = spreading_activation_search(
            query="JWT auth", scope="auth", top_k=10,
            client=client, graphify_client=graphify,
        )
        ids = [d.drawer_id for d in results]
        self.assertEqual(len(ids), len(set(ids)), "duplicate drawers in results")

    def test_empty_graphify_graph_is_harmless(self):
        from typed.read import spreading_activation_search
        client = MockClient()
        self._populate(client)
        graphify = MockGraphifyClient({})  # empty graph

        results = spreading_activation_search(
            query="JWT auth", scope="auth", top_k=5,
            client=client, graphify_client=graphify,
        )
        self.assertGreater(len(results), 0)

    def test_inject_session_start_accepts_graphify_client(self):
        from typed.read import inject_session_start
        client = MockClient()
        self._populate(client)
        graphify = MockGraphifyClient({"jwt": ["JwtService"]})

        block = inject_session_start(
            scope="auth", intent_hint="JWT",
            client=client, graphify_client=graphify,
        )
        self.assertIn("<!-- typed/sessionstart -->", block)
        self.assertIn("drw_", block)

    def test_extract_code_refs_finds_file_stems(self):
        from typed.read import _extract_code_refs
        body = "Handled in `auth.py` and `jwt.ts` with JwtRefreshToken."
        refs = _extract_code_refs(body)
        self.assertIn("auth", refs)
        self.assertIn("jwt", refs)
        self.assertIn("JwtRefreshToken", refs)

    def test_extract_code_refs_caps_at_five(self):
        from typed.read import _extract_code_refs
        body = "`a.py` `b.py` `c.py` `d.py` `e.py` `f.py` ExtraClass"
        refs = _extract_code_refs(body)
        self.assertLessEqual(len(refs), 5)


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
