# synaptic-memory

Persistent memory layer for Claude Code — stored in [mempalace](https://github.com/MemPalace/mempalace) (ChromaDB), enriched by [graphify](https://github.com/safishamsi/graphify) (knowledge graph), visualized in [Obsidian](https://obsidian.md).

---

## What is this?

Every Claude Code session ends cold — architecture decisions, bug fixes, reasoning behind choices all disappear. synaptic-memory solves this with two layers:

- **mempalace** — ChromaDB vector database + MCP server. Claude saves raw memory during sessions via hooks.
- **typed/** — Typed memory layer on top of mempalace. Adds schemas, embedding-based dedup, salience reranking, auto SessionStart injection, trust calibration, and nightly consolidation.

Both layers run together. The `typed/` layer wraps mempalace — it passes through to mempalace rather than replacing it.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full flow diagram and token math.

---

## Credits

| Project | Repo | What it provides |
| --- | --- | --- |
| **mempalace** | [github.com/MemPalace/mempalace](https://github.com/MemPalace/mempalace) | ChromaDB palace, MCP server, hook runner, semantic search |
| **graphify** | [github.com/safishamsi/graphify](https://github.com/safishamsi/graphify) | AST-based code knowledge graph, community detection, Obsidian wiki |

---

## Global setup (one-time)

### Step 1 — Clone this repo

```bash
git clone https://github.com/your-org/synaptic-memory.git
# remember where you cloned it — you'll need the path in Step 4
```

### Step 2 — Install mempalace (Python 3.11)

```bash
git clone https://github.com/MemPalace/mempalace.git
python3.11 -m pip install -e mempalace/
# Windows: py -3.11 -m pip install -e mempalace/
```

Configure storage in `~/.mempalace/config.json`:

```json
{
  "palace_path": "/path/to/your/memory/palace"
}
```

### Step 3 — Install graphify (Python 3.14)

```bash
git clone https://github.com/safishamsi/graphify.git
python3.14 -m pip install -e graphify/
# Windows: py -3.14 -m pip install -e graphify/
```

### Step 4 — Install Obsidian (optional, for visualization)

Download from [obsidian.md](https://obsidian.md) — free, no account needed.

### Step 5 — Register Claude Code hooks

Add to `~/.claude/settings.json`. Replace `<synaptic-memory-path>` with where you cloned this repo.

The hooks chain mempalace first, then the typed layer with `--no-mempalace-passthrough` to avoid double-firing:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Glob|Grep|Read",
        "hooks": [
          {"type": "command", "command": "python3.14 -c \"import os,json; d=os.path.exists('graphify-out/graph.json'); d and print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse','additionalContext':'graphify: graph exists — read graphify-out/GRAPH_REPORT.md before searching raw files.'}}))\""}
        ]
      },
      {
        "matcher": "Write",
        "hooks": [
          {"type": "command", "command": "python3.11 -c \"import sys,json; d=json.load(sys.stdin); p=d.get('tool_input',{}).get('file_path','').replace('\\\\','/'); b='.claude/memory' in p or 'MEMORY.md' in p; b and (print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse','additionalContext':'BLOCKED: use mcp__mempalace__mempalace_add_drawer, not .claude/memory/'}})) or sys.exit(2))\""}
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {"type": "command", "command": "python3.11 -c \"import sys,json; d=json.load(sys.stdin); p=d.get('tool_input',{}).get('file_path',''); remind=p and any(p.endswith(x) for x in ('.py','.ts','.tsx','.js','.cs','.sql','.json','.yaml','.yml')); remind and print(json.dumps({'hookSpecificOutput':{'hookEventName':'PostToolUse','additionalContext':'Code edited — if this completes a bug fix, feature, or decision, call mcp__mempalace__mempalace_add_drawer now.'}}))\""}
        ]
      }
    ],
    "SessionStart": [{
      "hooks": [
        {"type": "command", "command": "python3.11 -m mempalace hook run --hook session-start --harness claude-code"},
        {"type": "command", "command": "python3.11 \"<synaptic-memory-path>/hooks/session_start.py\" --no-mempalace-passthrough"}
      ]
    }],
    "Stop": [{
      "hooks": [
        {"type": "command", "command": "python3.11 -m mempalace hook run --hook stop --harness claude-code"},
        {"type": "command", "command": "python3.11 \"<synaptic-memory-path>/hooks/stop_15msg.py\" --no-mempalace-passthrough"}
      ]
    }],
    "PreCompact": [{
      "hooks": [
        {"type": "command", "command": "python3.11 -m mempalace hook run --hook precompact --harness claude-code"},
        {"type": "command", "command": "python3.11 \"<synaptic-memory-path>/hooks/pre_compact.py\""}
      ]
    }]
  }
}
```

> **Windows:** Replace `python3.11` with `py -3.11` and `python3.14` with `py -3.14`.

Hook behavior:

- **PreToolUse / Glob|Grep|Read** — before any file search or read, reminds Claude to check `graphify-out/GRAPH_REPORT.md` first (only fires if the graph exists)
- **PreToolUse / Write** — blocks writes to `.claude/memory/` or `MEMORY.md`; redirects Claude to use `mcp__mempalace__mempalace_add_drawer` instead
- **PostToolUse / Edit|Write** — after editing any code file (`.py`, `.ts`, `.js`, `.cs`, `.sql`, `.json`, `.yaml`, etc.), reminds Claude to save decisions to mempalace
- **SessionStart** — mempalace loads prior context; typed layer injects top-3 summaries (~150 tokens)
- **Stop** (every 15 messages) — mempalace saves raw memory; typed layer writes one summary drawer and records drawer count to `typed/budget.py`
- **PreCompact** — read-only; surfaces pinned drawers before context compaction

### Step 6 — Configure MCP servers

Copy `.mcp.json.example` to `.mcp.json` in your project root and fill in your paths:

```bash
cp .mcp.json.example .mcp.json
```

Then edit `.mcp.json`:

```json
{
  "mcpServers": {
    "mempalace": {
      "type": "stdio",
      "command": "python3.11",
      "args": ["-m", "mempalace.mcp_server", "--palace", "/path/to/your/memory/palace"],
      "env": {"MEMPALACE_HARNESS": "claude-code"}
    },
    "graphify": {
      "type": "stdio",
      "command": "python3.14",
      "args": ["-m", "graphify.serve", "graphify-out/graph.json"],
      "env": {}
    }
  }
}
```

> **Windows:** Use `py -3.11` / `py -3.14` and full Python executable paths if `py` launcher doesn't resolve correctly.

Claude now has these tools in every session:

| Tool | What it does |
| --- | --- |
| `mempalace_search` | Semantic search across all stored memory |
| `mempalace_add_drawer` | Save content to ChromaDB |
| `mempalace_diary_write` | Save compressed session summary |
| `mempalace_status` | Show all wings + drawer counts |
| `query_graph` | Traverse the codebase knowledge graph |
| `god_nodes` | Find the most connected code concepts |

---

## Per-project setup

Only these things change per project — global hooks and MCP servers never need to change.

### 1. Config files (project root)

**`mempalace.project.json`** — copy from `mempalace.project.json.example` and fill in:

```json
{
  "project_slug": "MY-PROJECT",
  "wing":         "my-project",
  "memory_archive": "/path/to/your/memory"
}
```

- `project_slug` — matches your Claude Code project folder name
- `wing` — unique tag that isolates this project's memories in ChromaDB
- `memory_archive` — parent directory of your palace (same as palace path, minus `/palace`)

**`mempalace.yaml`** — copy from `mempalace.yaml.example` and update the wing name:

```yaml
wing: my-project
rooms:
- name: decisions
- name: features
- name: bugs
- name: setup
- name: general
```

Both files are gitignored — safe to put local paths in them.

### 2. Build the knowledge graph

```bash
cd <your-project>
python3.14 -m graphify .   # or: /graphify in Claude Code
```

This generates `graphify-out/graph.json`. Once built, the graphify MCP server starts serving it and the PreToolUse hook activates automatically.

### 3. Scope tag (optional but recommended)

Set `SYNAPTIC_V2_SCOPE=MY-PROJECT` in the project's environment so hooks tag drawers to the correct project scope. Without this, scope falls back to the current folder name.

### 4. Nightly consolidation cron

Reranks drawers by salience, detects contradictions, flags stale memories, archives zero-hit drawers older than 90 days, and syncs to graphify automatically.

**Linux / macOS** — add to crontab (`crontab -e`):

```bash
0 1 * * * cd <synaptic-memory-path> && python3.11 -m typed.consolidate --synaptic-repo <synaptic-memory-path>
```

**Windows** — run once in PowerShell (registers a persistent Task Scheduler entry):

```powershell
$action = New-ScheduledTaskAction `
    -Execute "py.exe" `
    -Argument '-3.11 -m typed.consolidate --synaptic-repo "<synaptic-memory-path>"' `
    -WorkingDirectory "<synaptic-memory-path>"
$trigger = New-ScheduledTaskTrigger -Daily -At 1:00AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "synaptic-memory-consolidate" `
    -Action $action -Trigger $trigger -Settings $settings -Force
```

Replace `<synaptic-memory-path>` with your actual clone path. `StartWhenAvailable` ensures the task runs on next boot if the machine was asleep at 1am.

---

## Daily usage

**During sessions — fully automatic.**
Hooks fire at session start, every 15 messages, and before compaction. No manual steps.

**On-demand memory search** (any Claude session):

```text
mempalace_search("your query")
mempalace_status()
```

**Manual graphify sync** (if not using nightly cron):

```bash
python3.11 scripts/mempal_to_graphify.py   # inject memories into code graph
python3.14 scripts/graphify_wiki.py --clean  # rebuild Obsidian wiki
```

**View in Obsidian:**
Open `graphify-out/` as vault → `Ctrl+G` → memory nodes appear as `[MEM] <name>` linked to code.

---

## Typed write API

When you want to explicitly save a typed decision mid-session:

```python
from typed.write import write_decision, write_pattern, write_recipe

write_decision(
    scope="auth",
    body="Chose JWT over session cookies — 8 microservices need stateless auth.",
    confidence="high",
)
```

Drawer types: `decision`, `pattern`, `anti-pattern`, `recipe`, `postmortem`, `summary`

If a semantically similar drawer exists (cosine similarity > 0.92), `DuplicateDrawerError` is raised with the existing `drawer_id` — use `supersedes=` to replace it.

**Trust calibration** — when a user corrects Claude after a drawer was cited:

```python
from typed.telemetry import mark_correction
mark_correction(["drw_xxx"])  # auto-demotes to confidence=low after 2 hits
```

**Token budget tracking:**

Drawer counts are recorded automatically each time the Stop hook fires (via `stop_15msg.py` → `record_session()`). View the weekly report at any time:

```bash
python3.11 -m typed.budget          # show baseline + weekly targets
```

To also record token counts (not available from hooks automatically):

```bash
python3.11 -m typed.budget --record --tokens-in 42000 --tokens-out 8500 --note "session"
```

---

## File reference

| File / Folder | Committed | Purpose |
| --- | --- | --- |
| `ARCHITECTURE.md` | Yes | Full flow diagram, token math, coupling surface |
| `INSTALL.ai.md` | Yes | Step-by-step setup prompt for AI assistants |
| `synaptic-memory-design-spec.md` | Yes | Full operational design spec |
| `.mcp.json.example` | Yes | MCP server config template |
| `mempalace.project.json.example` | Yes | Per-project config template |
| `mempalace.yaml.example` | Yes | Room definitions template |
| `.mcp.json` | No (gitignored) | Your local MCP config with real paths |
| `mempalace.project.json` | No (gitignored) | Your local per-project config |
| `mempalace.yaml` | No (gitignored) | Your local room definitions |
| `hooks/session_start.py` | Yes | SessionStart hook |
| `hooks/stop_15msg.py` | Yes | Stop hook (every 15 messages) |
| `hooks/pre_compact.py` | Yes | PreCompact hook (read-only) |
| `scripts/mempal_to_graphify.py` | Yes | Bridge: mine ChromaDB → inject into graphify |
| `scripts/graphify_wiki.py` | Yes | Obsidian wiki generator |
| `typed/` | Yes | Typed memory package (types, write, read, consolidate, telemetry, budget) |
| `tests/` | Yes | 21 unit tests |
| `graphify-out/` | No (gitignored) | Auto-generated graph + Obsidian vault |

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `python3.11` not found | Install Python 3.11 from python.org; on Windows use `py -3.11` |
| `python3.14` not found | Install Python 3.14 from python.org; on Windows use `py -3.14` |
| `No module named 'chromadb'` | `python3.11 -m pip install -e <mempalace-clone>/` |
| `No module named 'graphify'` | `python3.14 -m pip install -e <graphify-clone>/` |
| `No module named 'mcp'` | `python3.14 -m pip install mcp` |
| Hook not firing | Verify `python3.11 -m mempalace --help` resolves |
| MCP tools missing | Check `.mcp.json` exists in project root with correct paths |
| `mempalace.yaml` not found | Copy `mempalace.yaml.example` → `mempalace.yaml` in project root |
| graphify PreToolUse not firing | Ensure `graphify-out/graph.json` exists (run `/graphify` first) |
| 0 memory nodes in graphify | Complete a session so hooks fire, then run `mempal_to_graphify.py` |
| Obsidian shows no graph | Open `<project>/graphify-out/` not the synaptic-memory repo root |
| Palace HNSW diverged | Run `python3.11 -m mempalace repair --yes` |
