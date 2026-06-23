"""InterruptLayer -- interrupt-driven early-exit retrieval for spreading activation."""

from __future__ import annotations

import enum
import os
from dataclasses import dataclass, field
from typing import Optional

from typed.types import TypedDrawer


class ImpulsivityMode(str, enum.Enum):
    OFF = "off"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class ADHDConfig:
    enabled: bool = False
    level: int = 0  # 0=off, 1=impulse only, 2=+drift, 3=+burst
    impulse_threshold: float = 0.88
    impulse_margin: float = 0.18
    impulse_mode: str = "prepend"
    p_inattention: float = 0.05
    burst_n: int = 3
    max_extra_drawers: int = 2
    burst_timeout_ms: int = 200

    @property
    def impulsivity_mode(self) -> ImpulsivityMode:
        if not self.enabled or self.level == 0:
            return ImpulsivityMode.OFF
        if self.level == 1:
            return ImpulsivityMode.LOW
        if self.level == 2:
            return ImpulsivityMode.MEDIUM
        return ImpulsivityMode.HIGH

    @staticmethod
    def from_env() -> ADHDConfig:
        """Build from config.json defaults, then apply env var overrides."""
        from dataclasses import fields as _fields
        from typed.config import get_config
        d = get_config().adhd
        cfg = ADHDConfig(**{f.name: getattr(d, f.name) for f in _fields(d)})

        if os.environ.get("SYNAPTIC_ADHD_DISABLE") == "1":
            return ADHDConfig(enabled=False, level=0)
        raw_level = os.environ.get("SYNAPTIC_ADHD_LEVEL")
        if raw_level is not None:
            try:
                cfg.level = int(raw_level)
            except ValueError:
                cfg.level = 0
            cfg.enabled = cfg.level > 0
        return cfg


@dataclass
class InterruptEvent:
    kind: str  # "threshold", "margin", "saturation"
    drawer: TypedDrawer
    score: float


class InterruptLayer:
    """Registers strong seed hits pre-hop and prepends them post-merge."""

    def __init__(self, config: ADHDConfig) -> None:
        self._config = config
        self._registered: list[InterruptEvent] = []

    @property
    def active(self) -> bool:
        return self._config.enabled and self._config.level > 0

    def check_seeds(self, seeds: list[tuple[TypedDrawer, float]]) -> None:
        """Scan seed results for interrupt-worthy hits. Call after seeding, before hop loop."""
        if not self.active:
            return

        mode = self._config.impulsivity_mode
        if mode == ImpulsivityMode.OFF:
            return

        for drawer, score in seeds:
            if score >= self._config.impulse_threshold:
                self._registered.append(InterruptEvent("threshold", drawer, score))

        if mode in (ImpulsivityMode.MEDIUM, ImpulsivityMode.HIGH) and len(seeds) >= 2:
            sorted_seeds = sorted(seeds, key=lambda t: -t[1])
            top1_score = sorted_seeds[0][1]
            top2_score = sorted_seeds[1][1]
            if (top1_score - top2_score) > self._config.impulse_margin:
                d, s = sorted_seeds[0]
                if not any(e.drawer.drawer_id == d.drawer_id for e in self._registered):
                    self._registered.append(InterruptEvent("margin", d, s))

    def check_frontier(self, frontier_scores: list[float]) -> None:
        """Check for saturation interrupt (HIGH mode only). Not wired in v1."""
        if not self.active:
            return
        if self._config.impulsivity_mode != ImpulsivityMode.HIGH:
            return
        if frontier_scores and all(s < 0.1 for s in frontier_scores):
            pass  # saturation detected -- reserved for future use

    def post_merge(self, ranked: list[tuple[TypedDrawer, float]]) -> list[tuple[TypedDrawer, float]]:
        """Prepend registered interrupt hits, deduplicate against ranked results."""
        if not self.active or not self._registered:
            return ranked

        seen: set[str] = set()
        merged: list[tuple[TypedDrawer, float]] = []

        for event in self._registered:
            if event.drawer.drawer_id not in seen:
                seen.add(event.drawer.drawer_id)
                merged.append((event.drawer, event.score))

        for drawer, score in ranked:
            if drawer.drawer_id not in seen:
                seen.add(drawer.drawer_id)
                merged.append((drawer, score))

        return merged

    @property
    def events(self) -> list[InterruptEvent]:
        return list(self._registered)
