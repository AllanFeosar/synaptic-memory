"""QueryDriftLayer -- Module 2 (Inattention) of the ADHD retrieval layer.

Gated stochastic exploration bolted onto spreading_activation_search: with a small
probability it mutates the query (one of three strategies, chosen by Boltzmann
sampling), runs one extra search, and merges only *salient* survivors into the
activation map. Active only at level >= 2. See ADHD_MODULE2_QUERYDRIFT_PLAN.md.
"""

from __future__ import annotations

import json
import logging
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from typed.adhd import ADHDConfig
from typed.types import TypedDrawer

logger = logging.getLogger(__name__)

# Drifted drawers enter ranking discounted so they can't outrank genuine hits
# but a truly salient bridge can still compete.
DRIFT_DISCOUNT = 0.7

# retrieve(query, k, scope_escape=False) -> list[tuple[TypedDrawer, float]]
RetrieveFn = Callable[..., list]

# Order matters: index maps to the Boltzmann logit vector.
STRATEGIES = ("temporal", "tangent", "scope_escape")


@dataclass
class DriftEvent:
    strategy: str
    query: str   # the mutated query actually searched (truncated for telemetry)
    kept: int    # drawers merged in after the salience gate


# ---------------------------------------------------------------------------
# Adaptive salience gate — same self-calibration idea as the interrupt layer's
# _calibrate_threshold, but for drift admission. A fixed gate can't know the
# corpus's salience distribution; this reads it from the audit log so drift
# admits genuine bridges (fresh, mid-salience) while still excluding the
# penalized tail (stale / cite-then-corrected). Cached per process.
# ---------------------------------------------------------------------------

_calibrated_gate: Optional[float] = None


def reset_gate_calibration() -> None:
    """Clear cached gate calibration (for testing)."""
    global _calibrated_gate
    _calibrated_gate = None


def _tail_saliences(log_path: Path, need: int) -> list[float]:
    """Collect up to `need` result salience values from the tail of one audit file."""
    if need <= 0 or not log_path.exists():
        return []
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 131_072)  # 128 KB — salience is on every logged result
            f.seek(size - chunk)
            tail = f.read().decode("utf-8", errors="replace")
        nl = tail.find("\n")
        if nl != -1 and chunk < size:
            tail = tail[nl + 1:]
        lines = tail.splitlines()
    except OSError:
        return []

    out: list[float] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        for r in rec.get("results", []):
            s = r.get("salience")
            if isinstance(s, (int, float)):
                out.append(s)
                if len(out) >= need:
                    return out
    return out


def _calibrate_salience_gate(
    percentile: float = 0.25,
    window: int = 400,
    min_samples: int = 30,
) -> Optional[float]:
    """Nth-percentile of recent result salience — an adaptive drift admission gate.

    Reads the audit-log tail (topping up from rotated files when short), caches per
    process, returns None when there is not enough salience data yet (caller falls
    back to the fixed gate). `salience` is logged per result since 2026-08-12.
    """
    global _calibrated_gate
    if _calibrated_gate is not None:
        return _calibrated_gate

    base = Path.home() / ".synaptic-memory" / "retrieval-audit.jsonl"
    sals = _tail_saliences(base, window)
    idx = 1
    while len(sals) < window and idx <= 3:
        rotated = base.with_name(f"retrieval-audit.{idx}.jsonl")
        if not rotated.exists():
            break
        sals.extend(_tail_saliences(rotated, window - len(sals)))
        idx += 1

    if len(sals) < min_samples:
        return None

    sals.sort()
    i = min(int(len(sals) * percentile), len(sals) - 1)
    _calibrated_gate = sals[i]
    return _calibrated_gate


def _boltzmann_choice(logits: list[float], temperature: float, rng: random.Random) -> int:
    """Sample an index from `logits` via a Boltzmann (softmax) distribution.

    Uniform logits => uniform choice; the vector is a hook for learning per-strategy
    weights later without touching the call site.
    """
    t = max(temperature, 1e-6)
    m = max(logits)
    exps = [math.exp((val - m) / t) for val in logits]  # subtract max for stability
    total = sum(exps)
    if total <= 0:
        return rng.randrange(len(logits))
    r = rng.random() * total
    cum = 0.0
    for i, e in enumerate(exps):
        cum += e
        if r <= cum:
            return i
    return len(logits) - 1


class QueryDriftLayer:
    """One gated drift pass per retrieval. Mutates activation/frontier in place."""

    def __init__(self, config: ADHDConfig, rng: Optional[random.Random] = None) -> None:
        self._config = config
        self._rng = rng or random
        self._events: list[DriftEvent] = []
        self._effective_gate = self._resolve_gate()

    def _resolve_gate(self) -> float:
        """Adaptive salience gate if enabled + enough data, else the fixed fallback.
        Skips calibration entirely when inactive to avoid a log read per retrieval."""
        if not self.active:
            return self._config.drift_salience_gate
        if self._config.drift_adaptive_gate:
            calibrated = _calibrate_salience_gate(percentile=self._config.drift_gate_percentile)
            if calibrated is not None:
                return calibrated
        return self._config.drift_salience_gate

    @property
    def active(self) -> bool:
        return self._config.enabled and self._config.level >= 2

    @property
    def effective_gate(self) -> float:
        return self._effective_gate

    @property
    def events(self) -> list[DriftEvent]:
        return list(self._events)

    def maybe_drift(
        self,
        query: str,
        frontier: list[tuple[TypedDrawer, float]],
        retrieve: RetrieveFn,
        activation: dict[str, tuple[TypedDrawer, float]],
    ) -> None:
        """With prob p_inattention, run one drifted search and merge gated survivors."""
        if not self.active:
            return
        if self._rng.random() >= self._config.p_inattention:
            return  # dice miss — the common case

        strategy = STRATEGIES[
            _boltzmann_choice([0.0, 0.0, 0.0], self._config.drift_temperature, self._rng)
        ]
        drift_query, scope_escape = self._build_query(strategy, query, frontier)
        if not drift_query:
            return

        try:
            hits = retrieve(drift_query, 3, scope_escape=scope_escape)
        except Exception:  # noqa: BLE001 — drift must never break base retrieval
            logger.debug("drift retrieve failed", exc_info=True)
            hits = []

        kept = self._merge(strategy, hits, activation, frontier)
        self._events.append(DriftEvent(strategy, drift_query[:120], kept))

    # ------------------------------------------------------------------
    def _build_query(
        self, strategy: str, query: str, frontier: list[tuple[TypedDrawer, float]]
    ) -> tuple[str, bool]:
        """Return (mutated_query, scope_escape_flag). Empty query => skip drift."""
        if strategy == "scope_escape":
            return query, True
        if strategy == "tangent":
            if not frontier:
                return "", False
            weakest = min(frontier, key=lambda t: t[1])[0]  # peripheral hit
            tail = weakest.body[-self._config.drift_tangent_chars:]
            return tail.strip(), False
        # temporal: same query; recency preference is applied in _merge
        return query, False

    def _merge(
        self,
        strategy: str,
        hits: list[tuple[TypedDrawer, float]],
        activation: dict[str, tuple[TypedDrawer, float]],
        frontier: list[tuple[TypedDrawer, float]],
    ) -> int:
        """Salience-gate `hits`, then merge up to max_extra_drawers into activation."""
        gate = self._effective_gate
        cap = self._config.max_extra_drawers

        survivors = [
            (d, s) for d, s in hits
            if d is not None and d.drawer_id not in activation and d.salience() > gate
        ]
        if strategy == "temporal":
            survivors.sort(key=lambda ds: ds[0].created_at)   # oldest-first (inverse recency)
        else:
            survivors.sort(key=lambda ds: -ds[1])             # strongest-first

        kept = 0
        for d, s in survivors[:cap]:
            score = s * DRIFT_DISCOUNT
            activation[d.drawer_id] = (d, score)
            frontier.append((d, score))
            kept += 1
        return kept
