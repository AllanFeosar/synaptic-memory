# synaptic-memory

Persistent memory for Claude Code — stored in [mempalace](https://github.com/milla-jovovich/mempalace) (ChromaDB), wired into [graphify](https://github.com/safishamsi/graphify) (knowledge graph), visualized in [Obsidian](https://obsidian.md).

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
      │  Stop hook fires every 15 messages
      ▼
~/.claude/projects/<project>/memory/*.md
      │
      │  py -3.11 scripts/mempal_to_graphify.py
      │
      ├─ mempalace mine  → ChromaDB palace
      ├─ graphify rebuild → code graph (graphify-out/graph.json)
      ├─ query ChromaDB by wing → memory documents for this project
      └─ inject memory nodes + keyword links into graph.json
      │
      │  python scripts/graphify_wiki.py --clean
      ▼
graphify-out/wiki/*.md  ← open as Obsidian vault → Ctrl+G
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
git clone https://github.com/milla-jovovich/mempalace.git
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

## Step 4 — Install Claude Code hooks (one-time)

Hooks come from the [mempalace repo](https://github.com/milla-jovovich/mempalace) — copy them from there:

```bash
cp mempalace/hooks/mempal_save_hook.sh ~/.claude/hooks/
cp mempalace/hooks/mempal_precompact_hook.sh ~/.claude/hooks/
```

Add to `~/.claude/settings.json`:
```json
{
  "hooks": {
    "Stop": [{
      "matcher": "*",
      "hooks": [{"type": "command", "command": "/absolute/path/to/mempal_save_hook.sh", "timeout": 30}]
    }],
    "PreCompact": [{
      "hooks": [{"type": "command", "command": "/absolute/path/to/mempal_precompact_hook.sh", "timeout": 30}]
    }]
  }
}
```

The Stop hook fires every 15 messages and tells Claude to save key topics, decisions, and code to memory. The PreCompact hook fires before context compression for a final save.

---

## Step 5 — Add to your project (per project)

**Copy scripts:**
```
scripts/mempal_to_graphify.py
scripts/graphify_wiki.py
```

**Create `mempalace.project.json`** (copy from `mempalace.project.json.example`):
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
cp mempalace.yaml.example mempalace.yaml
# edit wing: MyProject → your wing name
```

**Create empty folder:**
```bash
mkdir mempalace-refs
```

---

## Daily usage

**After each Claude Code session:**
```bash
py -3.11 scripts/mempal_to_graphify.py
```

Does everything in one command:
- Mines Claude memory files into ChromaDB (tagged with project wing)
- Rebuilds graphify code graph
- Injects memory nodes + keyword code links into `graph.json`

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
| `mempalace-refs/` | No | Auto-generated (gitignored) |
| `graphify-out/` | No | Auto-generated graph + Obsidian vault (gitignored) |
"# synaptic-memory" 
