"""
GraphifyClient — read-only interface to a graphify knowledge graph.

Allows spreading_activation_search() to ripple through code structure edges
stored in graphify-out/graph.json, then search mempalace for memories about
the discovered neighbor nodes.

Implementations:
  LocalGraphifyClient   — reads graphify-out/graph.json from disk (used in hooks)
  MockGraphifyClient    — pure-Python dict-based backend for tests
"""

from __future__ import annotations

import abc
import json
from pathlib import Path
from typing import Optional


class GraphifyClient(abc.ABC):
    """Read-only interface to a graphify graph."""

    @abc.abstractmethod
    def query(self, query: str, limit: int = 5) -> list[str]:
        """Return node labels whose id or norm_label contains query (substring)."""

    @abc.abstractmethod
    def get_neighbors(self, node_label: str, limit: int = 5) -> list[str]:
        """Return labels of nodes adjacent to the node with this label."""


# ---------------------------------------------------------------------------
# Local file-based implementation (for hooks — no MCP, no subprocess)
# ---------------------------------------------------------------------------

class LocalGraphifyClient(GraphifyClient):
    """Reads graphify-out/graph.json directly from disk.

    graph.json structure:
      nodes: [{id, label, norm_label, file_type, source_file, ...}]
      links: [{source, target, relation, confidence_score, ...}]

    Builds an in-memory adjacency index on init. Fast for <=50k nodes.
    """

    def __init__(self, graph_json: Path) -> None:
        data = json.loads(graph_json.read_text(encoding="utf-8"))

        # id -> node dict
        self._nodes: dict[str, dict] = {
            n["id"]: n for n in data.get("nodes", []) if "id" in n
        }
        # norm_label -> id (for label-based lookups)
        self._label_index: dict[str, str] = {
            (n.get("norm_label") or n.get("label", "").lower()): n["id"]
            for n in self._nodes.values()
        }
        # id -> list[neighbor id]
        self._adj: dict[str, list[str]] = {}
        for link in data.get("links", []):
            src = link.get("source") or link.get("_src")
            tgt = link.get("target") or link.get("_tgt")
            if src and tgt:
                self._adj.setdefault(src, []).append(tgt)
                self._adj.setdefault(tgt, []).append(src)

    def query(self, query: str, limit: int = 5) -> list[str]:
        """Substring match against norm_label and id."""
        q = query.lower().strip()
        if not q:
            return []
        results: list[str] = []
        for node in self._nodes.values():
            nl = node.get("norm_label") or node.get("label", "").lower()
            if q in nl or q in node.get("id", ""):
                results.append(node.get("label") or node["id"])
                if len(results) >= limit:
                    break
        return results

    def get_neighbors(self, node_label: str, limit: int = 5) -> list[str]:
        """Return labels of neighbors, looked up by label or norm_label."""
        nl = node_label.lower()
        node_id = self._label_index.get(nl)
        if node_id is None:
            # Fallback: substring search
            for nid, ndata in self._nodes.items():
                if nl in (ndata.get("norm_label") or ndata.get("label", "").lower()):
                    node_id = nid
                    break
        if node_id is None:
            return []
        neighbor_ids = self._adj.get(node_id, [])
        return [
            self._nodes[nid].get("label") or nid
            for nid in neighbor_ids[:limit]
            if nid in self._nodes
        ]

    @classmethod
    def from_cwd(cls, hint: Optional[Path] = None) -> Optional["LocalGraphifyClient"]:
        """Try to load from cwd/graphify-out/graph.json (or hint path).

        Returns None if not found — spreading activation degrades gracefully.
        """
        candidates = []
        if hint:
            candidates.append(hint / "graphify-out" / "graph.json")
        candidates.append(Path.cwd() / "graphify-out" / "graph.json")

        for path in candidates:
            if path.exists():
                try:
                    return cls(path)
                except (json.JSONDecodeError, KeyError, OSError):
                    return None
        return None


# ---------------------------------------------------------------------------
# Mock implementation (for tests)
# ---------------------------------------------------------------------------

class MockGraphifyClient(GraphifyClient):
    """Dict-based graph for tests. graph = {label: [neighbor_label, ...]}."""

    def __init__(self, graph: Optional[dict[str, list[str]]] = None) -> None:
        self._graph: dict[str, list[str]] = graph or {}

    def query(self, query: str, limit: int = 5) -> list[str]:
        q = query.lower()
        return [label for label in self._graph if q in label.lower()][:limit]

    def get_neighbors(self, node_label: str, limit: int = 5) -> list[str]:
        return list(self._graph.get(node_label, []))[:limit]
