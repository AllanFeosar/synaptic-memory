"""
Drawer types + schema + frontmatter (de)serialization.

Strategy: encode the typed tuple as YAML frontmatter prepended to drawer
content. This is mempalace-compatible (drawer content is just text) but
makes drawers self-describing and parseable by the consolidation layer.

A typed drawer looks like:

    ---
    drawer_id: drw_20260430_a8f1
    type: decision
    scope: auth
    confidence: high
    tier: long-term
    supersedes: drw_20260315_d33b
    created_at: 2026-04-30T14:00:00+00:00
    usage_count: 0
    cite_then_correct: 0
    stale: false
    pinned: false
    ---
    We chose JWT over session cookies because we have 8 microservices and
    the team needs stateless auth for service-to-service calls. Refresh
    token rotation handles long sessions. See /docs/auth.md.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import hashlib
import math
import re
from typing import Any, Optional


FILE_REF_RE = re.compile(r"`([^`]+\.(?:py|ts|tsx|js|jsx|go|rs|java|cs|md))`")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class DrawerType(str, enum.Enum):
    DECISION = "decision"        # We chose X over Y because ...
    PATTERN = "pattern"          # This shape repeats across the codebase
    ANTI_PATTERN = "anti-pattern"  # Don't do this; here's why
    RECIPE = "recipe"            # Procedural: how to do X
    POSTMORTEM = "postmortem"    # Bug we hit + root cause + fix
    SUMMARY = "summary"          # Compressed session summary

    @classmethod
    def parse(cls, raw: str) -> "DrawerType":
        normalized = raw.strip().lower().replace("_", "-")
        try:
            return cls(normalized)
        except ValueError:
            import logging
            logging.getLogger(__name__).warning("unknown drawer type %r, falling back to summary", raw)
            return cls.SUMMARY


class Confidence(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @classmethod
    def parse(cls, raw: str) -> "Confidence":
        return cls(raw.strip().lower())


class MemoryTier(str, enum.Enum):
    """TTL tier controlling expiry and exponential decay half-life.

    EPHEMERAL  — session scratch notes; auto-archived after 1 day.
    SHORT_TERM — sprint/investigation context; 7-day TTL.
    LONG_TERM  — feature decisions and patterns; 90-day TTL (default).
    PERMANENT  — architecture decisions and recipes; never expires.
    """
    EPHEMERAL  = "ephemeral"
    SHORT_TERM = "short-term"
    LONG_TERM  = "long-term"
    PERMANENT  = "permanent"

    @classmethod
    def parse(cls, raw: str) -> "MemoryTier":
        return cls(raw.strip().lower())

    @property
    def ttl_days(self) -> Optional[float]:
        """Hard expiry in days. None = permanent, never archived by TTL."""
        return {
            MemoryTier.EPHEMERAL:  1.0,
            MemoryTier.SHORT_TERM: 7.0,
            MemoryTier.LONG_TERM:  90.0,
            MemoryTier.PERMANENT:  None,
        }[self]

    @property
    def half_life_days(self) -> Optional[float]:
        """Exponential decay half-life. None = permanent, no decay."""
        return {
            MemoryTier.EPHEMERAL:  0.5,
            MemoryTier.SHORT_TERM: 3.5,
            MemoryTier.LONG_TERM:  45.0,
            MemoryTier.PERMANENT:  None,
        }[self]


# ---------------------------------------------------------------------------
# Drawer model
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclasses.dataclass
class TypedDrawer:
    """A drawer with required type metadata.

    Untyped writes are rejected at the write layer. Every drawer that flows
    through typed has these fields.
    """

    drawer_id: str
    type: DrawerType
    scope: str
    confidence: Confidence
    body: str

    # Optional / defaulted
    tier: MemoryTier = MemoryTier.LONG_TERM
    supersedes: Optional[str] = None
    created_at: _dt.datetime = dataclasses.field(default_factory=lambda: _dt.datetime.now(_dt.timezone.utc))
    usage_count: int = 0
    cite_then_correct: int = 0
    stale: bool = False
    pinned: bool = False

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def make_id(scope: str, body: str, ts: Optional[_dt.datetime] = None) -> str:
        ts = ts or _dt.datetime.now(_dt.timezone.utc)
        digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:6]
        slug = re.sub(r"[^a-z0-9]+", "", scope.lower())[:8] or "global"
        return f"drw_{ts.strftime('%Y%m%d_%H%M%S')}_{slug}_{digest}"

    @classmethod
    def new(
        cls,
        type: DrawerType | str,
        scope: str,
        confidence: Confidence | str,
        body: str,
        tier: MemoryTier | str = MemoryTier.LONG_TERM,
        supersedes: Optional[str] = None,
        pinned: bool = False,
    ) -> "TypedDrawer":
        if not body or not body.strip():
            raise ValueError("Drawer body cannot be empty.")
        if not scope or not scope.strip():
            raise ValueError("Drawer scope is required (use 'global' if cross-project).")

        type_enum = type if isinstance(type, DrawerType) else DrawerType.parse(type)
        conf_enum = confidence if isinstance(confidence, Confidence) else Confidence.parse(confidence)
        tier_enum = tier if isinstance(tier, MemoryTier) else MemoryTier.parse(tier)
        ts = _dt.datetime.now(_dt.timezone.utc)
        return cls(
            drawer_id=cls.make_id(scope, body, ts),
            type=type_enum,
            scope=scope.strip(),
            confidence=conf_enum,
            body=body.strip(),
            tier=tier_enum,
            supersedes=supersedes.strip() if supersedes else None,
            created_at=ts,
            pinned=pinned,
        )

    # ------------------------------------------------------------------
    # Salience — exponential decay per tier half-life
    # ------------------------------------------------------------------

    def salience(self, now: Optional[_dt.datetime] = None) -> float:
        """Salience score used for retrieval reranking and prune decisions.

        Formula:
            decay = exp(-ln(2) * age_days / half_life)   # exponential decay
                  = 1.0                                   # permanent tier

            salience = (usage_count * 2)
                     + decay
                     + (pinned * 5)
                     - (cite_then_correct * 1.5)
                     - (stale * 2)

        Permanent drawers never decay — decay factor stays 1.0 forever.
        Ephemeral drawers (half_life=0.5d) reach ~0.25 in one day.
        """
        now = now or _dt.datetime.now(_dt.timezone.utc)
        age_days = max(0.0, (now - self.created_at).total_seconds() / 86400.0)

        half_life = self.tier.half_life_days
        if half_life is None:
            decay = 1.0  # permanent — never decays
        else:
            decay = math.exp(-math.log(2) * age_days / half_life)

        from typed.config import get_config
        sc = get_config().salience
        score = (
            self.usage_count * sc.usage_weight
            + decay
            + (sc.pin_bonus if self.pinned else 0.0)
            - self.cite_then_correct * sc.correction_penalty
            - (sc.stale_penalty if self.stale else 0.0)
        )
        return round(score, 3)

    # ------------------------------------------------------------------
    # Summary projection (for retrieval injection — token-disciplined)
    # ------------------------------------------------------------------

    def summary(self, max_chars: int = 180) -> str:
        """One-line summary suitable for SessionStart context injection."""
        first_line = self.body.splitlines()[0].strip()
        truncated = first_line if len(first_line) <= max_chars else first_line[: max_chars - 1] + "…"
        flags = []
        if self.stale:
            flags.append("STALE")
        if self.confidence == Confidence.LOW:
            flags.append("low_conf")
        if self.tier != MemoryTier.LONG_TERM:
            flags.append(f"tier:{self.tier.value}")
        if self.supersedes:
            flags.append(f"supersedes:{self.supersedes}")
        flag_str = f" [{','.join(flags)}]" if flags else ""
        return f"[{self.drawer_id}] {self.type.value}/{self.scope}{flag_str}: {truncated}"


# ---------------------------------------------------------------------------
# (De)serialization — frontmatter <-> object
# ---------------------------------------------------------------------------

def _yaml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, _dt.datetime):
        return v.isoformat()
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if any(c in s for c in ":#\n") or s != s.strip():
        return repr(s)  # quoted
    return s


def serialize_drawer(d: TypedDrawer) -> str:
    """Serialize TypedDrawer back into mempalace-storable text content."""
    lines = ["---"]
    fields = [
        ("drawer_id", d.drawer_id),
        ("type", d.type.value),
        ("scope", d.scope),
        ("confidence", d.confidence.value),
        ("tier", d.tier.value),
        ("supersedes", d.supersedes),
        ("created_at", d.created_at),
        ("usage_count", d.usage_count),
        ("cite_then_correct", d.cite_then_correct),
        ("stale", d.stale),
        ("pinned", d.pinned),
    ]
    for k, v in fields:
        if v is None:
            continue
        lines.append(f"{k}: {_yaml_value(v)}")
    lines.append("---")
    lines.append(d.body)
    return "\n".join(lines)


def _parse_frontmatter_value(raw: str) -> Any:
    s = raw.strip()
    if s in ("null", "~", ""):
        return None
    if s in ("true", "false"):
        return s == "true"
    if s.startswith("'") and s.endswith("'"):
        return s[1:-1]
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        pass
    return s


def parse_drawer(text: str) -> TypedDrawer:
    """Parse mempalace-stored content back into a TypedDrawer.

    Raises ValueError if frontmatter is missing or malformed.
    Old drawers without a `tier` field default to LONG_TERM for backward compatibility.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("Drawer is missing typed frontmatter — not a typed drawer.")

    fm_raw = m.group(1)
    body = text[m.end():].strip()
    fields: dict[str, Any] = {}
    for line in fm_raw.splitlines():
        if not line.strip() or ":" not in line:
            continue
        k, _, v = line.partition(":")
        fields[k.strip()] = _parse_frontmatter_value(v)

    required = ("drawer_id", "type", "scope", "confidence")
    missing = [k for k in required if k not in fields]
    if missing:
        raise ValueError(f"Drawer frontmatter missing required fields: {missing}")

    created_at = fields.get("created_at")
    if isinstance(created_at, str):
        try:
            created_at = _dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            created_at = _dt.datetime.now(_dt.timezone.utc)
    elif not isinstance(created_at, _dt.datetime):
        created_at = _dt.datetime.now(_dt.timezone.utc)

    raw_tier = fields.get("tier")
    try:
        tier = MemoryTier.parse(str(raw_tier)) if raw_tier else MemoryTier.LONG_TERM
    except ValueError:
        tier = MemoryTier.LONG_TERM  # unknown tier → safe default

    return TypedDrawer(
        drawer_id=str(fields["drawer_id"]),
        type=DrawerType.parse(str(fields["type"])),
        scope=str(fields["scope"]),
        confidence=Confidence.parse(str(fields["confidence"])),
        body=body,
        tier=tier,
        supersedes=fields.get("supersedes") or None,
        created_at=created_at,
        usage_count=int(fields.get("usage_count") or 0),
        cite_then_correct=int(fields.get("cite_then_correct") or 0),
        stale=bool(fields.get("stale") or False),
        pinned=bool(fields.get("pinned") or False),
    )
