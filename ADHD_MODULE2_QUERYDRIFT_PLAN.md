# ADHD Module 2 — QueryDriftLayer (Inattention) — Implementation Plan

Status: **IMPLEMENTED + LIVE** (`level >= 2`), default stays level 1 so install behavior is unchanged.
Code: `typed/adhd_drift.py` (QueryDriftLayer), integrated in `typed/read.py`, telemetry via
`drift_events` + `drift_effective_gate` in `typed/budget.py`, report block in
`scripts/adhd_test_report.py`.

**Live test 1 (2026-08-04 → 08-12):** drift fired 6.7% (on target) but was **inert** — 9/10 fires
merged nothing, because the *fixed* salience gate (1.5) rejected fresh/cross-domain bridges
(salience ≈ 1.0). **Fix (2026-08-12):** replaced the fixed gate with an **adaptive salience gate**
(`_calibrate_salience_gate`, 25th pct of recent salience, same pattern as the interrupt threshold),
fixed fallback lowered 1.5 → 0.5, `salience` now logged per result. 27 tests in
`tests/test_adhd_drift.py`. Re-test running from 2026-08-12; success = drift showing `kept > 0`.

## 1. Goal

Linear retrieval always seeds from the literal query and ripples outward greedily (argmax fan-out).
It reliably misses **bridging memories** — relevant drawers that don't lexically match the query but
sit one associative jump away. QueryDrift injects a small, gated dose of stochastic exploration so
those surface, without degrading the common case.

Design constraints carried from the council review (do not relax without a new decision):
- Drift probability `p = 0.05`, **not** 0.25. It is a garnish, not the main dish.
- Drifted results must clear a **salience gate** (`salience() > 1.5`) or they are discarded — drift
  may only add *important* memories, never noise.
- Sampling is **Boltzmann** (temperature 1.5), not argmax, only for the drift path — the base
  retrieval ranking stays deterministic.
- No always-on background drift; it runs inline, synchronously, per retrieval, gated by level.

## 2. Where it plugs into the current code

Module 1 is **inline** inside `spreading_activation_search()` (typed/read.py), not a separate
`adhd_search()` wrapper. Module 2 follows the same pattern — no new orchestration layer.

Current flow (typed/read.py):
```
seeds = _search(query, top_k*2)          # line ~158
build frontier + activation
_interrupt.check_seeds(frontier)         # line 169  (Module 1 pre-hop)
for hop in range(1, depth+1): ...        # hop loop
ranked = sorted(activation, by activation+salience*blend)
ranked = _interrupt.post_merge(ranked)   # line 205  (Module 1 post-rank)
top = ranked[:top_k]
record_retrieval(..., interrupt_events=, effective_threshold=, source=)
```

Module 2 inserts **one drift pass right after the initial seed search**, before the hop loop:
```
seeds = _search(query, top_k*2)
build frontier + activation
_drift = QueryDriftLayer(adhd_config)
_drift.maybe_drift(query, wing, frontier, _search, activation)   # NEW — Module 2
_interrupt.check_seeds(frontier)
... hop loop unchanged ...
```
`maybe_drift` mutates `activation`/`frontier` in place (adds gated drifted drawers), so the existing
hop loop and ranking naturally incorporate them. No change to ranking math.

## 3. Config (typed/config.py — ADHDDefaults)

Already scaffolded: `p_inattention = 0.05`. Add three tunables (with range rules in `_RANGE_RULES`):

| key | default | range | meaning |
|---|---|---|---|
| `drift_salience_gate` | 1.5 | 0.0–100.0 | min `salience()` for a drifted drawer to be kept |
| `drift_temperature` | 1.5 | 0.1–10.0 | Boltzmann temperature for strategy/frontier sampling |
| `drift_tangent_chars` | 250 | 50–1000 | tail chars of weakest frontier body used as tangent query |

Activation: QueryDrift runs only when `config.level >= 2` (i.e. `impulsivity_mode` MEDIUM/HIGH).
Level 1 stays interrupt-only, preserving today's behavior as the default.

## 4. QueryDriftLayer API (new file: typed/adhd_drift.py)

Keep it in a sibling module to avoid bloating adhd.py; import into read.py alongside InterruptLayer.

```python
@dataclass
class DriftEvent:
    strategy: str          # "temporal" | "tangent" | "scope_escape"
    query: str             # the mutated query actually searched
    kept: int              # drawers added after the salience gate

class QueryDriftLayer:
    def __init__(self, config: ADHDConfig, rng: random.Random | None = None): ...

    @property
    def active(self) -> bool:
        return self._config.enabled and self._config.level >= 2

    def maybe_drift(self, query, wing, frontier, search_fn, activation) -> None:
        """With prob p_inattention, pick a strategy (Boltzmann), run one drifted
        search, salience-gate the hits, and merge survivors into activation +
        frontier. Records a DriftEvent. No-op if inactive or the dice miss."""

    @property
    def events(self) -> list[DriftEvent]: ...
```

`rng` is injectable so tests are deterministic (seed it); production passes `None` → module RNG.

## 5. The three strategies

All three produce a *mutated query*, run **one** `search_fn(q, k)` call (respects the existing
`max_search_calls` budget — drift consumes from the same pool), then salience-gate the hits.

1. **temporal** — surface older/adjacent-era memories. Query = original query; after search,
   re-rank candidates by *inverse* recency (oldest-first) before the gate, so the drift preferentially
   pulls in aged-but-salient drawers the greedy path skips. (Cheapest, no new text.)
2. **tangent** (lexical) — pick the **weakest** frontier member (min activation), take the last
   `drift_tangent_chars` of its `body`, use that as the query. Jumps to a topic adjacent to a
   peripheral hit rather than the center of mass.
3. **scope_escape** — rerun the original query with `wing=None` (drop the scope filter) to surface
   cross-project / cross-domain memories that scope filtering hides.

Strategy is chosen by **Boltzmann sampling** over a fixed logit vector (start uniform `[0,0,0]`,
temperature `drift_temperature`); this leaves room to later learn per-strategy weights from telemetry
without changing the call site.

## 6. Salience gate + merge

For each drifted hit → drawer `d`, score `s`:
```
if d.drawer_id in activation:      continue          # don't override a real hit
if d.salience() <= drift_salience_gate:  continue    # gate: importance only
activation[d.drawer_id] = (d, s * DRIFT_DISCOUNT)    # enters ranking, slightly discounted
frontier.append((d, s * DRIFT_DISCOUNT))             # can seed a hop
```
`DRIFT_DISCOUNT` (~0.7) keeps drifted drawers from outranking genuine top hits while still letting a
truly salient bridge compete. Cap survivors per drift pass at `max_extra_drawers` (already in config).

## 7. Telemetry — reuse the pattern we just built

Add `drift_events` to the audit record exactly like `interrupt_events`:
- `typed/budget.py`: add `drift_events: list = field(default_factory=list)` to `RetrievalRecord`
  and a param to `record_retrieval`.
- `typed/read.py`: pass `drift_events=[asdict(e) for e in _drift.events]`.
- Gate it the same way `effective_threshold` is now gated — only meaningful when `_drift.active`.
- `scripts/adhd_test_report.py`: add a "Drift events by strategy" block mirroring "By kind", and a
  **drift-usefulness** metric: of drifted drawers that entered the top-k, how many were later cited
  (cross-ref the write/citation log). Drift that never makes the cut is dead weight — measure it.

## 8. Tests (tests/test_adhd_drift.py)

- inactive at level < 2 (no drift events, activation unchanged)
- dice-miss path (`rng` forced > p) → no-op
- dice-hit path (`rng` forced < p) → exactly one DriftEvent, correct strategy for a forced sample
- salience gate: a low-salience drifted hit is discarded; a high-salience one is kept
- merge never overrides an existing activation entry
- `max_extra_drawers` cap respected
- budget: drift respects `max_search_calls` (no extra searches when pool exhausted)
- Boltzmann sampler: with a seeded RNG, strategy distribution matches expected over N draws

Target ~12 tests, matching Module 1's coverage density.

## 9. Rollout & validation

1. Ship at **level 2 behind config** (default stays level 1 → zero behavior change on install).
2. Enable level 2 locally for a 1-week window (same protocol as the interrupt test; baseline the
   record count, run `scripts/adhd_test_report.py`).
3. Success = drift fires ~5% of session-start retrievals **and** ≥ some fraction of drifted drawers
   reach top-k and get cited. If drift never gets cited, it is noise → tune the gate up or cut a
   strategy before considering Module 3.

## 10. Scope boundaries (do NOT build)

- No `p = 0.25`, no always-on/background drift loop (that is Module 3 territory, and gated on session
  persistence).
- No phasic-gain score multiplier (correctness bug flagged in the original review).
- Drift must never *replace* the base ranking — it only *adds* gated candidates.
- All changes live in synaptic-memory; never touch mempalace or graphify.

## Effort estimate

~1 focused session: new `typed/adhd_drift.py` (~120 lines), 3 small edits (config, budget, read),
report block, and the test file. No changes to mempalace/graphify. Module 1's inline integration
pattern means no orchestration refactor.
