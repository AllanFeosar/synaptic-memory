"""
graphify_wiki.py
────────────────
Generate Obsidian-compatible wiki from graphify-out/graph.json

HOW IT WORKS:
  1. Reads graphify-out/graph.json from the project root
  2. Creates one .md file per node in graphify-out/wiki/
  3. Each file has YAML frontmatter + [[wikilinks]] to connected nodes
  4. Open graphify-out/ as an Obsidian vault → Ctrl+G for graph view

REUSABLE: Drop this script into any project unchanged.
No configuration needed — reads graph.json from the standard graphify-out/ location.

USAGE:
  python scripts/graphify_wiki.py          # generate wiki
  python scripts/graphify_wiki.py --clean  # clear wiki/ first, then generate
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

# ── Locate project root (one level up from this script) ──────────────────────
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
GRAPH_JSON   = PROJECT_ROOT / "graphify-out" / "graph.json"
WIKI_DIR     = PROJECT_ROOT / "graphify-out" / "wiki"


# ── Sanitize label → safe filename ───────────────────────────────────────────
def safe_name(label: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", label)[:120]


# ── Load graph ────────────────────────────────────────────────────────────────
def load_graph() -> tuple[dict, dict, list]:
    if not GRAPH_JSON.exists():
        print(f"[graphify_wiki] ERROR: {GRAPH_JSON} not found.")
        print("  Run graphify rebuild first, or: py -3.11 scripts/mempal_to_graphify.py")
        sys.exit(1)

    with open(GRAPH_JSON, encoding="utf-8") as f:
        g = json.load(f)

    nodes = {n["id"]: n for n in g["nodes"]}
    links = g["links"]
    return nodes, links, g["nodes"]


# ── Build adjacency maps ──────────────────────────────────────────────────────
def build_adjacency(nodes: dict, links: list) -> tuple[dict, dict]:
    outgoing: dict[str, list] = {nid: [] for nid in nodes}
    incoming: dict[str, list] = {nid: [] for nid in nodes}

    for link in links:
        src = link.get("source") or link.get("_src", "")
        tgt = link.get("target") or link.get("_tgt", "")
        rel = link.get("relation", "")
        if src in nodes and tgt in nodes:
            outgoing[src].append((rel, tgt))
            incoming[tgt].append((rel, src))

    return outgoing, incoming


# ── Generate one .md file for a node ─────────────────────────────────────────
def node_to_md(node: dict, outgoing: list, incoming: list) -> str:
    label       = node.get("label", node["id"])
    community   = node.get("community", "")
    file_type   = node.get("file_type", "")
    source_file = node.get("source_file", "")
    wing        = node.get("wing", "")
    room        = node.get("room", "")

    lines = ["---"]
    lines.append(f"community: {community}")
    lines.append(f"type: {file_type}")
    if source_file:
        lines.append(f'source: "{source_file}"')
    if wing:
        lines.append(f"wing: {wing}")
    if room:
        lines.append(f"room: {room}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {label}")
    lines.append("")

    if outgoing:
        lines.append("## Links To")
        for rel, tgt_id in outgoing[:30]:
            tgt_label = nodes_ref.get(tgt_id, {}).get("label", tgt_id)
            lines.append(f"- {rel}: [[{safe_name(tgt_label)}]]")
        lines.append("")

    if incoming:
        lines.append("## Linked From")
        for rel, src_id in incoming[:30]:
            src_label = nodes_ref.get(src_id, {}).get("label", src_id)
            lines.append(f"- {rel}: [[{safe_name(src_label)}]]")
        lines.append("")

    return "\n".join(lines)


# Module-level ref used inside node_to_md (set before calling)
nodes_ref: dict = {}


# ── Generate full wiki ────────────────────────────────────────────────────────
def generate_wiki(clean: bool = False) -> int:
    global nodes_ref

    nodes, links, nodes_list = load_graph()
    nodes_ref = nodes

    outgoing, incoming = build_adjacency(nodes, links)

    if clean and WIKI_DIR.exists():
        shutil.rmtree(WIKI_DIR)
        print("[graphify_wiki] Cleared existing wiki/")

    WIKI_DIR.mkdir(parents=True, exist_ok=True)

    written = 0
    for nid, node in nodes.items():
        label    = node.get("label", nid)
        fname    = safe_name(label) + ".md"
        fpath    = WIKI_DIR / fname
        content  = node_to_md(node, outgoing.get(nid, []), incoming.get(nid, []))

        fpath.write_text(content, encoding="utf-8")
        written += 1

    return written


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Obsidian wiki from graphify-out/graph.json")
    parser.add_argument("--clean", action="store_true", help="Clear wiki/ before generating")
    args = parser.parse_args()

    count = generate_wiki(clean=args.clean)
    print(f"[graphify_wiki] Generated {count} nodes → graphify-out/wiki/")
    print(f"[graphify_wiki] Open graphify-out/ as Obsidian vault → Ctrl+G")


if __name__ == "__main__":
    main()
