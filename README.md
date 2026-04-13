# synaptic-memory

Persistent memory for Claude Code — stored in [mempalace](https://github.com/MemPalace/mempalace) (ChromaDB), wired into [graphify](https://github.com/safishamsi/graphify) (knowledge graph), visualized in [Obsidian](https://obsidian.md).

---

## Credits

This repo is a setup guide and bridge layer. It depends entirely on two open-source projects:

| Project | Repo | What it provides |
|---|---|---|
| **mempalace** | [github.com/MemPalace/mempalace](https://github.com/MemPalace/mempalace) | ChromaDB palace, MCP server, hook runner, semantic search |
| **graphify** | [github.com/safishamsi/graphify](https://github.com/safishamsi/graphify) | AST-based code knowledge graph, community detection, Obsidian wiki |

All the actual memory storage, MCP tools, and hook logic comes from **mempalace**.
All the code graph generation and visualization comes from **graphify**.
This repo only provides the bridge scripts and setup instructions to wire them together.

---

## What is this?

Every time you work with Claude Code, valuable context disappears when the session ends — architecture decisions, bug fixes, why a certain approach was chosen, what was tried and failed. The next session starts cold.

**synaptic-memory** solves this by building a persistent, searchable memory layer that connects directly to your codebase:

- Claude Code automatically saves memory during every session (via hooks)
- Those memories are stored in a local vector database (mempalace / ChromaDB) — fully offline, nothing leaves your machine
- A bridge script mines those memories and injects them as nodes into your project's code knowledge graph (graphify)
- Each memory node is linked to the actual classes, methods, and files it relates to — using keyword matching against the graph
- The result is navigable in Obsidian as a graph — you can see memory sitting next to the code it documents

**Memory sits next to the code it documents — not in a separate system.**

### Why is this useful?

| Without synaptic-memory | With synaptic-memory |
|---|---|
| Every Claude session starts cold | Claude reads previous decisions and context automatically |
| "Why did we do it this way?" has no answer | Memory nodes link to the exact code with the reasoning |
| Architecture decisions live in chat history | Stored, searchable, and visible in Obsidian graph |
| You repeat context every session | One command syncs all memory into the graph |
| Memory is separate from code | Memory nodes sit next to the code they document |

### What it produces

After running the two scripts you get:
- A **ChromaDB palace** — semantically searchable memory for your project, queryable by Claude or any LLM
- A **graphify knowledge graph** — your codebase as a graph, with memory nodes injected and linked to code
- An **Obsidian vault** — browse everything visually, see memory clustered with the code it relates to

---

## How it works

```
Claude Code session
      │
      ├─ SessionStart hook fires → initializes session state
      │
      │  Stop hook fires every 15 messages
      │  PreCompact hook fires before context compression
      ▼
Claude calls MCP tools directly:
  mempalace_add_drawer   → saves decisions, quotes, code to ChromaDB
  mempalace_diary_write  → saves compressed session summary
      │
      │  (manual, after session)
      │  py -3.11 scripts/mempal_to_graphify.py
      │
      ├─ graphify rebuild → code graph (graphify-out/graph.json)
      ├─ query ChromaDB by wing → memory documents for this project
      └─ inject memory nodes + keyword links into graph.json
      │
      │  python scripts/graphify_wiki.py --clean
      ▼
graphify-out/wiki/*.md  ← open as Obsidian vault → Ctrl+G

On-demand (any time via MCP):
  mempalace_search("query")  → semantic search across all memory
  mempalace_status()         → see all wings + counts
```

---

## AI-assisted setup

If you're working with an AI assistant (Claude, Copilot, Cursor, etc.), share [`INSTALL.ai.md`](INSTALL.ai.md) with it.
That file is written as a direct prompt — your AI will read it and execute the full install for you, asking only the questions it needs (Python versions, storage path, project name).

---

## Step 1 — Install mempalace

mempalace stores and searches memory via ChromaDB + semantic embeddings.
Requires **Python 3.11** (incompatible with 3.14 due to ChromaDB/pydantic constraints).

```bash
git clone https://github.com/MemPalace/mempalace.git
py -3.11 -m pip install -e mempalace/
```

On first run, mempalace downloads `all-MiniLM-L6-v2` (79MB ONNX model) — one-time only.

Configure storage location in `~/.mempalace/config.json`:
```json
{
  "palace_path": "/your/chosen/memory/storage/palace"
}
```

---

## Step 2 — Install graphify

graphify builds a knowledge graph from your codebase via AST extraction.
Requires **Python 3.14**.

```bash
git clone https://github.com/safishamsi/graphify.git
py -3.14 -m pip install -e graphify/
```

---

## Step 3 — Install Obsidian

Download from [obsidian.md](https://obsidian.md) — free desktop app, no account needed.

---

## Step 4 — Register Claude Code hooks (one-time)

No files to copy. mempalace ships a built-in hook runner. Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{"type": "command", "command": "py -3.11 -m mempalace hook run --hook session-start --harness claude-code", "timeout": 30}]
    }],
    "Stop": [{
      "matcher": "*",
      "hooks": [{"type": "command", "command": "py -3.11 -m mempalace hook run --hook stop --harness claude-code", "timeout": 30}]
    }],
    "PreCompact": [{
      "hooks": [{"type": "command", "command": "py -3.11 -m mempalace hook run --hook precompact --harness claude-code", "timeout": 30}]
    }]
  }
}
```

- **SessionStart** — initializes session state tracking
- **Stop** — fires every 15 messages, tells Claude to save via MCP tools
- **PreCompact** — fires before context compression for a final thorough save

## Step 4.5 — Register the MCP server (global, one-time)

mempalace ships a built-in MCP server. Register it globally so Claude can query and write memory from any project:

```bash
claude mcp add mempalace -- py -3.11 -m mempalace.mcp_server --palace "<storage_path>/palace"
```

Replace `<storage_path>` with your chosen path (e.g. `D:\.lmstudio\Memory`).

This gives Claude these tools in every session:

| Tool | What it does |
|---|---|
| `mempalace_search` | Semantic search across all stored memory |
| `mempalace_add_drawer` | Save content directly to ChromaDB |
| `mempalace_diary_write` | Save compressed session summary |
| `mempalace_status` | Show all wings + drawer counts |
| `mempalace_list_wings` / `mempalace_list_rooms` | Browse the palace structure |
| `mempalace_check_duplicate` | Avoid saving the same thing twice |

---

## Step 5 — Add to your project (per project)

Run these from the **synaptic-memory repo root**, replacing `<project_root>` with your project path:

**Copy scripts:**
```bash
cp scripts/mempal_to_graphify.py "<project_root>/scripts/"
cp scripts/graphify_wiki.py "<project_root>/scripts/"
```

**Create `mempalace.project.json`** in the project root (copy from `mempalace.project.json.example`):
```bash
cp mempalace.project.json.example "<project_root>/mempalace.project.json"
```
Then edit it:
```json
{
  "project_slug": "MY-PROJECT",
  "wing":         "MyProject",
  "memory_archive": "/your/chosen/memory/storage"
}
```
- `project_slug` — partial match against your Claude Code project folder name
- `wing` — unique tag per project, isolates memories in ChromaDB
- `memory_archive` — same root as your `palace_path` minus `/palace`

**Create `mempalace.yaml`** in the project root (copy from `mempalace.yaml.example`, change wing name):
```bash
cp mempalace.yaml.example "<project_root>/mempalace.yaml"
# then edit wing: MyProject → your wing name
```

**Create empty placeholder folder:**
```bash
mkdir "<project_root>/mempalace-refs"
```

---

## Daily usage

**During sessions — fully automatic:**

Hooks fire automatically. Claude saves memory via MCP tools (`mempalace_add_drawer`, `mempalace_diary_write`) directly to ChromaDB. No manual steps needed.

**To query memory on-demand** (any time, inside any Claude session):

Ask Claude to call `mempalace_search("your query")` or `mempalace_status()`.

**To sync memory into the graphify knowledge graph** (optional, after session):

```bash
py -3.11 scripts/mempal_to_graphify.py
```

Injects memory nodes + keyword code links into `graphify-out/graph.json`.

**Refresh Obsidian:**
```bash
python scripts/graphify_wiki.py --clean
```

---

## Viewing memory in Obsidian

1. Open Obsidian → **Open folder as vault** → select `graphify-out/` inside your project
2. Press `Ctrl+G` — full graph view
3. Memory nodes appear as `[MEM] <name>` — linked to related code nodes
4. Click any `[MEM]` node to see which classes and methods it documents
5. Click any code node to see which memories relate to it

**In the graph panel:**
- Filter nodes by `type: memory` to highlight only memory nodes
- Set node size to **Connections** to surface the most-linked nodes

---

## Adding to a new project

Only 3 things change per project:

| What | Change |
|---|---|
| `mempalace.project.json` | `project_slug`, `wing`, `memory_archive` |
| `mempalace.yaml` | `wing` name + room keywords |
| `mempalace-refs/` | Create empty folder |

Scripts are copied unchanged.

---

## File reference

| File | Committed | Purpose |
|---|---|---|
| `scripts/mempal_to_graphify.py` | Yes | Bridge: mine → inject → rebuild |
| `scripts/graphify_wiki.py` | Yes | Obsidian wiki generator |
| `mempalace.project.json.example` | Yes | Config template |
| `mempalace.yaml.example` | Yes | Room definitions template |
| `mempalace.project.json` | No | Per-project config (gitignored) |
| `mempalace.yaml` | No | Room definitions (gitignored) |
| `mempalace-refs/` | No | Empty placeholder folder (gitignored — scripts never write here) |
| `graphify-out/` | No | Auto-generated graph + Obsidian vault (gitignored) |
