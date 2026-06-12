# AI Install Guide — synaptic-memory

> **For AI assistants:** Read this file top to bottom and execute each step in order.
> This installs the full synaptic-memory stack: mempalace (ChromaDB memory) + graphify (code graph) + Claude Code hooks + per-project wiring.
> Ask the user to confirm their Python versions and storage path before starting. Everything else you can execute directly.

---

## What you are installing

| Component | Purpose | Requires |
| --- | --- | --- |
| mempalace | Stores Claude Code session memory in ChromaDB via semantic embeddings | Python 3.11 |
| graphify | Builds a knowledge graph from the codebase via AST extraction | Python 3.14 |
| Claude Code hooks | Triggers memory saves automatically (SessionStart, Stop, PreCompact, PreToolUse) | Claude Code CLI |
| MCP servers | Gives Claude read/write access to memory and the code graph from any project | Python 3.11 + 3.14 |
| bridge script | Injects palace memory nodes into the graphify code graph | Python 3.11 |
| Obsidian | Visualizes the graph — open `graphify-out/` as a vault | Manual install |

---

## Step 0 — Ask the user these questions before starting

Ask:

1. **Python versions**: Run `python3.11 --version` and `python3.14 --version` (Windows: `py -3.11 --version` and `py -3.14 --version`). Are both available?
   - If 3.11 missing: install from <https://www.python.org/downloads/> (required for mempalace)
   - If 3.14 missing: install from <https://www.python.org/downloads/> (required for graphify)
2. **Storage path**: Where should memory be stored? (e.g. `/home/user/memory` or `C:\Memory`)
   - The palace (ChromaDB) will be stored at `<storage_path>/palace`
3. **Project info**: What is this project's name/slug and a short wing name?
   - `project_slug` — partial match against the Claude Code project folder (e.g. `my-app`)
   - `wing` — short unique tag for this project in ChromaDB (e.g. `my-app`)
4. **Clone location**: Where to clone mempalace, graphify, and synaptic-memory? (e.g. `/home/user/repos/`)
5. **Scan root**: Which root directory contains all your projects? (e.g. `E:\` or `/home/user/projects`)
   - The nightly consolidation cron reads this from `.mcp.json` — set once in the graphify env block and it works everywhere
   - On Windows this is typically a drive letter like `E:\`; on Linux/macOS a folder like `/home/user/projects`

Record the answers — you will use them in Steps 1–8b.

---

## Step 1 — Clone and install mempalace (Python 3.11)

```bash
cd "<clone-location>"
git clone https://github.com/MemPalace/mempalace.git
python3.11 -m pip install -e mempalace/
# Windows: py -3.11 -m pip install -e mempalace/
```

Configure the palace path. Write `~/.mempalace/config.json` replacing `<storage-path>` with the user's chosen path:

```json
{
  "palace_path": "<storage-path>/palace"
}
```

Verify:

```bash
python3.11 -m mempalace --help
# Windows: py -3.11 -m mempalace --help
```

---

## Step 2 — Clone and install graphify (Python 3.14)

```bash
cd "<clone-location>"
git clone https://github.com/safishamsi/graphify.git
python3.14 -m pip install -e graphify/
python3.14 -m pip install mcp
# Windows: py -3.14 -m pip install -e graphify/ && py -3.14 -m pip install mcp
```

Verify:

```bash
python3.14 -c "import graphify; print('graphify ok')"
```

---

## Step 3 — Clone synaptic-memory

```bash
cd "<clone-location>"
git clone https://github.com/your-org/synaptic-memory.git
```

Remember the path — you will reference it as `<synaptic-memory-path>` throughout the remaining steps.

---

## Step 4 — Install Obsidian (manual — tell the user)

Tell the user:

> Download Obsidian from <https://obsidian.md> — free desktop app, no account needed.
> After setup, open `graphify-out/` inside your project as a vault to see the knowledge graph.

No commands needed — proceed to Step 5.

---

## Step 5 — Register Claude Code hooks (global, one-time)

Read `~/.claude/settings.json` first, then merge in the hooks block below (do not overwrite unrelated settings).

Replace `<synaptic-memory-path>` with the actual path from Step 3.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Glob|Grep",
        "hooks": [
          {"type": "command", "command": "python3.14 -c \"import os,json; d=os.path.exists('graphify-out/graph.json'); d and print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse','additionalContext':'graphify: graph exists — read graphify-out/GRAPH_REPORT.md before searching raw files.'}}))\""}
        ]
      },
      {
        "matcher": "Write",
        "hooks": [
          {"type": "command", "command": "python3.11 \"<synaptic-memory-path>/hooks/pre_tool_write.py\""}
        ]
      },
      {
        "matcher": "Read",
        "hooks": [
          {"type": "command", "command": "python3.11 \"<synaptic-memory-path>/hooks/pre_tool_read.py\""}
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {"type": "command", "command": "python3.11 -c \"import sys,json; d=json.load(sys.stdin); p=d.get('tool_input',{}).get('file_path',''); remind=p and any(p.endswith(x) for x in ('.py','.ts','.tsx','.js','.cs','.sql','.json','.yaml','.yml')); remind and print(json.dumps({'hookSpecificOutput':{'hookEventName':'PostToolUse','additionalContext':'Code edited — if this completes a bug fix, feature, or decision, call mcp__mempalace__mempalace_add_drawer now.'}}))\""},
          {"type": "command", "command": "python3.11 \"<synaptic-memory-path>/hooks/post_tool_edit.py\""}
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

- **PreToolUse / Glob|Grep** — before any file search, reminds Claude to check `graphify-out/GRAPH_REPORT.md` first (only fires if graph exists)
- **PreToolUse / Write** — blocks writes to `.claude/memory/`; redirects Claude to use `mcp__mempalace__mempalace_add_drawer` instead
- **PreToolUse / Read** — before reading a file, queries graphify for that file's node + neighbors, searches mempalace for related memories, injects them as context. Silent if no graph or no memories found.
- **PostToolUse / Edit|Write** — after editing any code file: (1) reminds Claude to save decisions to mempalace, (2) surfaces graphify structural neighbors that may also need updating
- **SessionStart** — mempalace loads prior context; typed layer injects top-3 typed summaries (~150 tokens)
- **Stop** — fires every ~15 messages; typed layer writes one summary drawer and records drawer count to `typed/budget.py`
- **PreCompact** — read-only; surfaces pinned drawers into context before compaction

---

## Step 6 — Configure MCP servers (per project)

Copy the template and fill in your paths:

```bash
cd "<project-root>"
cp "<synaptic-memory-path>/.mcp.json.example" .mcp.json
```

Edit `.mcp.json`:

```json
{
  "mcpServers": {
    "mempalace": {
      "type": "stdio",
      "command": "python3.11",
      "args": ["-m", "mempalace.mcp_server", "--palace", "<storage-path>/palace"],
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

> **Windows:** Use full Python executable paths if `python3.11` / `python3.14` don't resolve (e.g. `C:/Python311/python.exe`).

The graphify MCP server starts working once `graphify-out/graph.json` exists (after running `/graphify` in Claude Code). It safely exits without error if the graph hasn't been built yet.

---

## Step 7 — Create per-project config files

**`<project-root>/mempalace.project.json`** — fill in using answers from Step 0:

```json
{
  "project_slug": "<project-slug>",
  "wing":         "<wing>",
  "memory_archive": "<storage-path>"
}
```

**`<project-root>/mempalace.yaml`** — copy from `mempalace.yaml.example` and update the wing:

```yaml
wing: <wing>
rooms:
- name: decisions
  description: Architecture and design decisions
  keywords: [decision, chose, refactor, changed]
- name: features
  description: Features implemented and their context
  keywords: [feature, implement, added, api, endpoint]
- name: bugs
  description: Bugs found and fixes applied
  keywords: [bug, fix, error, issue, exception]
- name: setup
  description: Environment and tooling setup
  keywords: [install, setup, configure, hook]
- name: general
  description: General project notes and context
  keywords: [note, context, general]
```

Both files are gitignored — safe to put local paths in them.

---

## Step 8 — Verify the full setup

Check the palace is reachable:

```bash
python3.11 -m mempalace status
```

Run the bridge script to sync any existing memory into the graphify graph:

```bash
cd "<project-root>"
python3.11 scripts/mempal_to_graphify.py
```

Build the knowledge graph (or run `/graphify` in Claude Code):

```bash
python3.14 -m graphify .
```

Then generate the Obsidian wiki:

```bash
python3.14 scripts/graphify_wiki.py --clean
```

Tell the user:

> Open Obsidian → **Open folder as vault** → select `graphify-out/` inside your project.
> Press `Ctrl+G` to see the full graph. Memory nodes appear as `[MEM] <name>` linked to related code.

---

## Step 8b — Configure scan root and schedule the nightly consolidation cron

**First — set the scan root in `.mcp.json`** (from Step 6), using the answer from Step 0 question 5:

```json
"graphify": {
  "env": {
    "SYNAPTIC_SCAN_ROOT": "<scan-root>"
  }
}
```

This is the only place the scan root is configured. The cron reads it from here — no separate config file or environment variable needed. If `SYNAPTIC_SCAN_ROOT` is not set, graphify sync is skipped entirely.

**Then — register the cron** (one global entry covers all projects):

**Linux / macOS** — add to crontab (`crontab -e`):

```bash
0 1 * * * cd <synaptic-memory-path> && python3.11 -m typed.consolidate \
    --synaptic-repo <synaptic-memory-path>
```

**Windows** — run this once in PowerShell:

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

Verify it was registered:

```powershell
Get-ScheduledTask -TaskName "synaptic-memory-consolidate"
# Expected State: Ready
```

What the cron does per project found under the scan root:

1. Checks for `scripts/mempal_to_graphify.py` in that project root
2. Runs it → injects memories into `graphify-out/graph.json`
3. Runs `scripts/graphify_wiki.py --clean` → refreshes Obsidian wiki
4. Any failures are written to `~/.synaptic-memory/sync-errors.log` and opened in Notepad

---

## Step 9 — Tell the user how to use it daily

**During sessions — fully automatic:**

- Hooks fire at session start, every ~15 messages, and before compaction
- Claude writes memory via the `mempalace_*` MCP tools directly to ChromaDB
- Before every Glob/Grep, Claude is reminded to check the knowledge graph first

**To query memory on-demand inside any Claude session:**

```text
mempalace_search("your query")
mempalace_status()
query_graph("how does auth work?")
```

**To sync into the graphify graph (manual, after session):**

```bash
python3.11 scripts/mempal_to_graphify.py   # rebuild + inject memory nodes
python3.14 scripts/graphify_wiki.py --clean  # refresh Obsidian
```

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `python3.11` not found | Install Python 3.11 from python.org; on Windows use `py -3.11` |
| `python3.14` not found | Install Python 3.14 from python.org; on Windows use `py -3.14` |
| `No module named 'chromadb'` | `python3.11 -m pip install -e <mempalace-clone>/` |
| `No module named 'graphify'` | `python3.14 -m pip install -e <graphify-clone>/` |
| `No module named 'mcp'` | `python3.14 -m pip install mcp` |
| Hook outputs error | Check `python3.11 -m mempalace --help` resolves; verify mempalace is on 3.11 |
| MCP server not appearing | Check `.mcp.json` exists in project root with correct paths |
| `mempalace.yaml` not found | Copy `mempalace.yaml.example` → `mempalace.yaml` in project root |
| graphify PreToolUse not firing | `graphify-out/graph.json` not built yet — run `/graphify` first |
| 0 memory nodes injected | No sessions saved yet; complete a session so hooks fire |
| Obsidian shows no graph | Open `<project-root>/graphify-out/` not the synaptic-memory repo root |
| Palace HNSW diverged | `python3.11 -m mempalace repair --yes` |
