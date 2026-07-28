"""
Central configuration — all tunables in ~/.synaptic-memory/config.json.

Missing keys use defaults. File is optional (pure defaults if absent).
Cached per process — each hook invocation loads once.

    from typed.config import get_config
    cfg = get_config()
    cfg.retrieval.max_search_calls   # 12
    cfg.adhd.impulse_threshold       # 0.88

CLI:
    py -m typed.config               # print current effective config
    py -m typed.config --init        # write default config.json (won't overwrite)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

CONFIG_PATH = Path.home() / ".synaptic-memory" / "config.json"


_COERCE_TYPES = {"int": int, "float": float, "bool": bool, "str": str}

_RANGE_RULES: dict[str, tuple[float, float]] = {
    "dupe_threshold": (0.01, 1.0),
    "pin_top_fraction": (0.001, 1.0),
    "hop_depth": (0, 10),
    "max_search_calls": (1, 100),
    "forget_after_days": (1, 3650),
    "session_start_top_k": (1, 50),
    "frontier_fanout": (1, 20),
    "adaptive_window": (10, 10000),
    "adaptive_min_samples": (1, 10000),
    "adaptive_percentile": (0.5, 1.0),
    "auto_demote_threshold": (1, 100),
    "week_4_target_drop": (0.01, 1.0),
    "week_8_target_drop": (0.01, 1.0),
    "session_start_timeout_seconds": (1.0, 60.0),
    "drift_salience_gate": (0.0, 100.0),
    "drift_temperature": (0.1, 10.0),
    "drift_tangent_chars": (50, 1000),
}

import logging as _logging
_log = _logging.getLogger(__name__)


def _merge(target: object, overrides: dict) -> None:
    for f in fields(target):  # type: ignore[arg-type]
        if f.name in overrides:
            val = overrides[f.name]
            expected = getattr(target, f.name)
            if expected is not None and not isinstance(val, type(expected)):
                coerce = _COERCE_TYPES.get(type(expected).__name__)
                if coerce:
                    try:
                        val = coerce(val)
                    except (ValueError, TypeError):
                        _log.warning("config: cannot coerce %s=%r to %s", f.name, val, type(expected).__name__)
                        continue
            if f.name in _RANGE_RULES:
                lo, hi = _RANGE_RULES[f.name]
                try:
                    if not (lo <= float(val) <= hi):
                        _log.warning("config: %s=%r out of range [%s, %s], using default", f.name, val, lo, hi)
                        continue
                except (TypeError, ValueError):
                    continue
            setattr(target, f.name, val)


@dataclass
class SalienceConfig:
    usage_weight: float = 2.0
    pin_bonus: float = 5.0
    correction_penalty: float = 1.5
    stale_penalty: float = 2.0
    blend_ratio: float = 0.3


@dataclass
class HooksConfig:
    pre_compact_max_inject: int = 5
    pre_tool_read_max_drawers: int = 3
    post_tool_edit_max_neighbors: int = 4
    post_tool_edit_max_drawers: int = 2
    max_code_refs_per_drawer: int = 5
    session_start_timeout_seconds: float = 12.0


@dataclass
class RetrievalConfig:
    session_start_top_k: int = 3
    summary_max_chars: int = 180
    max_search_calls: int = 12
    frontier_fanout: int = 3
    hop_depth: int = 2
    hop_decay: float = 0.5
    graphify_hop_discount: float = 0.6
    graphify_refs_per_hop: int = 2
    graphify_labels_per_ref: int = 2


@dataclass
class ADHDDefaults:
    enabled: bool = False
    level: int = 0
    impulse_threshold: float = 0.88
    impulse_margin: float = 0.18
    impulse_mode: str = "prepend"
    adaptive_threshold: bool = True
    adaptive_percentile: float = 0.95
    adaptive_window: int = 200
    adaptive_min_samples: int = 30
    p_inattention: float = 0.05
    burst_n: int = 3
    max_extra_drawers: int = 2
    burst_timeout_ms: int = 200
    # Module 2 — QueryDriftLayer (active at level >= 2)
    drift_salience_gate: float = 1.5
    drift_temperature: float = 1.5
    drift_tangent_chars: int = 250


@dataclass
class ConsolidationConfig:
    forget_after_days: int = 90
    contradiction_sim_threshold: float = 0.88
    pin_top_fraction: float = 0.05
    scan_max_depth: int = 6
    scan_skip_dirs: list = field(default_factory=list)


@dataclass
class WriteConfig:
    dupe_threshold: float = 0.92


@dataclass
class TelemetryConfig:
    auto_demote_threshold: int = 2


@dataclass
class BudgetConfig:
    week_4_target_drop: float = 0.20
    week_8_target_drop: float = 0.30
    write_overhead_budget_fraction: float = 0.30
    tokens_per_drawer_write: int = 250
    tokens_per_cache_hit: int = 1500
    max_log_bytes: int = 10485760
    log_keep_rotated: int = 3


@dataclass
class SynapticConfig:
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    adhd: ADHDDefaults = field(default_factory=ADHDDefaults)
    consolidation: ConsolidationConfig = field(default_factory=ConsolidationConfig)
    write: WriteConfig = field(default_factory=WriteConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    salience: SalienceConfig = field(default_factory=SalienceConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)


_cached: Optional[SynapticConfig] = None


def get_config(path: Path = CONFIG_PATH) -> SynapticConfig:
    global _cached
    if _cached is not None:
        return _cached
    cfg = SynapticConfig()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for section in ("retrieval", "adhd", "consolidation", "write", "telemetry", "budget", "salience", "hooks"):
                if section in raw and isinstance(raw[section], dict):
                    _merge(getattr(cfg, section), raw[section])
        except (json.JSONDecodeError, OSError):
            pass
    _cached = cfg
    return cfg


def reload_config(path: Path = CONFIG_PATH) -> SynapticConfig:
    global _cached
    _cached = None
    return get_config(path)


def main() -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(description="synaptic-memory config")
    p.add_argument("--init", action="store_true",
                   help="Write default config.json (won't overwrite existing)")
    args = p.parse_args()

    if args.init:
        if CONFIG_PATH.exists():
            sys.stderr.write(f"Already exists: {CONFIG_PATH}\n")
            return 1
        default = SynapticConfig()
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(asdict(default), indent=2) + "\n", encoding="utf-8",
        )
        sys.stdout.write(f"Written: {CONFIG_PATH}\n")
        return 0

    cfg = reload_config()
    sys.stdout.write(json.dumps(asdict(cfg), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
