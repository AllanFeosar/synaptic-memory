"""
MempalaceClient — abstraction over mempalace's storage layer.

InProcessClient (default): imports mempalace Python modules directly.
  Safe to import: config, palace, searcher.
  NOT safe: mempalace.mcp_server — it does os.dup2(2,1) at module level
  which redirects stdout to stderr, breaking hook output to Claude.

MockClient: pure-Python in-memory backend for tests and dry-runs.
"""

from __future__ import annotations

import abc
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


# Process-level cache: (palace_path, collection_name) → InProcessClient.
# Prevents re-loading the ChromaDB HNSW index on every spreading_activation_search()
# call. Each hook is a fresh process, so this lives for the hook invocation only —
# but that's enough to collapse 12 cold loads per invocation into 1.
_CLIENT_CACHE: dict[tuple, "InProcessClient"] = {}


@dataclass
class SearchHit:
    drawer_id: str
    content: str
    score: float
    wing: Optional[str] = None
    room: Optional[str] = None


class MempalaceClient(abc.ABC):
    """Backend interface. Implementations: InProcessClient, MockClient."""

    @abc.abstractmethod
    def add_drawer(self, wing: str, room: str, content: str) -> str:
        """Persist a drawer. Returns the mempalace drawer id."""

    @abc.abstractmethod
    def search(self, query: str, top_k: int = 3, wing: Optional[str] = None) -> list[SearchHit]:
        """Semantic search. Hits sorted by similarity (highest first)."""

    @abc.abstractmethod
    def update_drawer(self, drawer_id: str, content: str) -> None:
        """Replace a drawer's content in-place (same ID, re-embedded)."""

    @abc.abstractmethod
    def list_wings(self) -> list[str]:
        ...

    @abc.abstractmethod
    def status(self) -> dict[str, Any]:
        ...


# ---------------------------------------------------------------------------
# In-process implementation — calls mempalace Python API directly
# ---------------------------------------------------------------------------

def _drawer_id(wing: str, room: str, content: str) -> str:
    return f"drawer_{wing}_{room}_{hashlib.sha256((wing + room + content).encode()).hexdigest()[:24]}"


class InProcessClient(MempalaceClient):
    """Calls mempalace Python modules directly — no subprocess, no MCP overhead.

    Uses the same drawer-id formula and metadata shape as tool_add_drawer in
    mempalace/mcp_server.py so drawers are fully interoperable with the MCP tools.
    """

    def __init__(self, palace_path: Optional[str] = None, collection_name: Optional[str] = None):
        from mempalace.config import MempalaceConfig
        cfg = MempalaceConfig()
        self._palace_path = palace_path or cfg.palace_path
        self._collection_name = collection_name or cfg.collection_name
        self._col = None  # loaded once on first search, then reused

    @classmethod
    def get_or_create(
        cls,
        palace_path: Optional[str] = None,
        collection_name: Optional[str] = None,
    ) -> "InProcessClient":
        """Return a cached InProcessClient for this (palace_path, collection_name) pair.

        Collapses N cold ChromaDB PersistentClient loads per spreading_activation_search()
        invocation into 1. Cache lives for the Python process lifetime (one hook run).
        """
        from mempalace.config import MempalaceConfig
        cfg = MempalaceConfig()
        key = (palace_path or cfg.palace_path, collection_name or cfg.collection_name)
        if key not in _CLIENT_CACHE:
            _CLIENT_CACHE[key] = cls(palace_path, collection_name)
        return _CLIENT_CACHE[key]

    def _collection(self, create: bool = False):
        if self._col is not None:
            return self._col
        from mempalace.palace import get_collection
        try:
            self._col = get_collection(self._palace_path, self._collection_name, create=create)
            return self._col
        except Exception as exc:
            # ChromaDB raises InvalidCollectionException (not NotFoundError) in newer
            # versions when a collection doesn't exist yet. mempalace's chroma.py catches
            # NotFoundError only — so new palaces fall through here.
            # Bootstrap directly, then let mempalace open it normally.
            if not create or "does not exist" not in str(exc):
                raise
            import chromadb
            os.makedirs(self._palace_path, exist_ok=True)
            _chroma = chromadb.PersistentClient(path=self._palace_path)
            _chroma.get_or_create_collection(
                self._collection_name,
                metadata={"hnsw:space": "cosine", "hnsw:num_threads": 1},
            )
            self._col = get_collection(self._palace_path, self._collection_name, create=False)
            return self._col

    def add_drawer(self, wing: str, room: str, content: str) -> str:
        col = self._collection(create=True)
        did = _drawer_id(wing, room, content)
        col.upsert(
            ids=[did],
            documents=[content],
            metadatas=[{
                "wing": wing,
                "room": room,
                "source_file": "",
                "chunk_index": 0,
                "added_by": "synaptic_memory",
                "filed_at": datetime.now().isoformat(),
            }],
        )
        return did

    def search(self, query: str, top_k: int = 3, wing: Optional[str] = None) -> list[SearchHit]:
        from mempalace.searcher import build_where_filter
        try:
            col = self._collection(create=False)
        except Exception:
            return []

        where = build_where_filter(wing, None)
        kwargs: dict = {
            "query_texts": [query],
            "n_results": min(top_k, col.count() or 1),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        try:
            raw = col.query(**kwargs)
        except Exception:
            return []

        ids = (raw.ids or [[]])[0]
        docs = (raw.documents or [[]])[0]
        metas = (raw.metadatas or [[]])[0]
        dists = (raw.distances or [[]])[0]

        hits = []
        for doc_id, doc, meta, dist in zip(ids, docs, metas, dists):
            meta = meta or {}
            hits.append(SearchHit(
                drawer_id=str(doc_id),
                content=str(doc),
                score=round(max(0.0, 1.0 - dist), 3),
                wing=meta.get("wing"),
                room=meta.get("room"),
            ))
        return hits

    def update_drawer(self, drawer_id: str, content: str) -> None:
        col = self._collection(create=False)
        col.update(ids=[drawer_id], documents=[content])

    def list_wings(self) -> list[str]:
        try:
            col = self._collection(create=False)
            result = col.get(include=["metadatas"])
            wings = {(m or {}).get("wing", "") for m in (result.metadatas or [])}
            return sorted(wings - {""})
        except Exception:
            return []

    def status(self) -> dict[str, Any]:
        try:
            col = self._collection(create=False)
            count = col.count()
        except Exception:
            count = 0
        return {
            "drawer_count": count,
            "palace_path": self._palace_path,
            "collection_name": self._collection_name,
        }


# ---------------------------------------------------------------------------
# In-memory mock (for tests + dry-run)
# ---------------------------------------------------------------------------

class MockClient(MempalaceClient):
    """Pure-Python in-memory backend. No embeddings — uses substring scoring."""

    def __init__(self) -> None:
        self.store: dict[str, tuple[str, str, str]] = {}  # id -> (wing, room, content)
        self._n = 0

    def add_drawer(self, wing: str, room: str, content: str) -> str:
        self._n += 1
        did = f"mock_{self._n}"
        self.store[did] = (wing, room, content)
        return did

    def search(self, query: str, top_k: int = 3, wing: Optional[str] = None) -> list[SearchHit]:
        q_tokens = set(query.lower().split())
        hits = []
        for did, (w, r, content) in self.store.items():
            if wing and w != wing:
                continue
            c_tokens = set(content.lower().split())
            overlap = len(q_tokens & c_tokens)
            if overlap == 0:
                continue
            # Recall-based score in [0.0, 1.0]: fraction of query tokens found in content.
            # Mirrors real embedding behavior — querying a body against its own serialized
            # content (body + frontmatter) yields 1.0 so dupe detection fires correctly.
            score = overlap / max(len(q_tokens), 1)
            hits.append(SearchHit(drawer_id=did, content=content, score=round(score, 3), wing=w, room=r))
        hits.sort(key=lambda h: -h.score)
        return hits[:top_k]

    def update_drawer(self, drawer_id: str, content: str) -> None:
        if drawer_id not in self.store:
            raise KeyError(drawer_id)
        w, r, _ = self.store[drawer_id]
        self.store[drawer_id] = (w, r, content)

    def list_wings(self) -> list[str]:
        return sorted({w for w, _, _ in self.store.values()})

    def status(self) -> dict[str, Any]:
        return {"drawer_count": len(self.store), "wings": self.list_wings()}
