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
                        continue
            setattr(target, f.name, val)


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


@dataclass
class ConsolidationConfig:
    forget_after_days: int = 90
    contradiction_sim_threshold: float = 0.88
    pin_top_fraction: float = 0.05
    scan_max_depth: int = 6


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


@dataclass
class SynapticConfig:
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    adhd: ADHDDefaults = field(default_factory=ADHDDefaults)
    consolidation: ConsolidationConfig = field(default_factory=ConsolidationConfig)
    write: WriteConfig = field(default_factory=WriteConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)


_cached: Optional[SynapticConfig] = None


def get_config(path: Path = CONFIG_PATH) -> SynapticConfig:
    global _cached
    if _cached is not None:
        return _cached
    cfg = SynapticConfig()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for section in ("retrieval", "adhd", "consolidation", "write", "telemetry", "budget"):
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
