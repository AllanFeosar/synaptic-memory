"""
typed — typed memory + consolidation layer over mempalace + graphify.

Drop-in addition to the synaptic-memory repo. Does not require modifying
mempalace or graphify directly — wraps their public APIs.

Public API:
    from typed import write, read, consolidate, telemetry, budget
    from typed.types import DrawerType, Confidence, TypedDrawer
"""

__version__ = "0.1.0"

from typed.types import (
    DrawerType,
    Confidence,
    TypedDrawer,
    parse_drawer,
    serialize_drawer,
)

__all__ = [
    "DrawerType",
    "Confidence",
    "TypedDrawer",
    "parse_drawer",
    "serialize_drawer",
]


