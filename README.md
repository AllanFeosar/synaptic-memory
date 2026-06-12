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

## Global setup (one-time per machine)

### Step 1 — Clone this repo

```bash
git clone https://github.com/your-org/synaptic-memory.git
# remember where you cloned it — you'll need the path in later steps
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
python3.14 -m pip install mcp
# Windows: py -3.14 -m pip install -e graphify/ && py -3.14 -m pip install mcp
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

- **PreToolUse / Glob|Grep** — before any file search, reminds Claude to check `graphify-out/GRAPH_REPORT.md` first (only fires if the graph exists)
- **PreToolUse / Write** — blocks writes to `.claude/memory/`; redirects Claude to use `mcp__mempalace__mempalace_add_drawer` instead
- **PreToolUse / Read** — before reading a file, queries graphify for that file's node + neighbors, searches mempalace for related memories, injects them as context. Silent if no graph or no memories found.
- **PostToolUse / Edit|Write** — after editing a code file: (1) reminds Claude to save decisions to mempalace, (2) queries graphify for structurally linked files that may also need updating, (3) surfaces related memories about those neighbors
- **SessionStart** — mempalace loads prior context; typed layer runs spreading activation search (mempalace + graphify hops) and injects top-3 drawer summaries (~150 tokens)
- **Stop** (every 15 messages) — mempalace saves raw memory; typed layer writes one summary drawer and records drawer count to `typed/budget.py`
- **PreCompact** — highest-stakes retrieval moment; runs spreading activation + graphify hops to surface pinned drawers + high-salience related memories before context compaction

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
      "env": {
        "SYNAPTIC_SCAN_ROOT": "/path/to/your/projects"
      }
    }
  }
}
```

> **Windows:** Use `py -3.11` / `py -3.14` and full Python executable paths if the `py` launcher doesn't resolve correctly.

`SYNAPTIC_SCAN_ROOT` tells the nightly consolidation cron which root directory to scan for projects. Set it to the parent folder that contains all your projects (e.g. `E:\Allan Project` or `/home/user/projects`). If not set, graphify sync is skipped.

Claude now has these tools in every session:

| Tool | What it does |
| --- | --- |
| `mempalace_search` | Semantic search across all stored memory |
| `mempalace_add_drawer` | Save content to ChromaDB |
| `mempalace_diary_write` | Save compressed session summary |
| `mempalace_status` | Show all wings + drawer counts |
| `query_graph` | Traverse the codebase knowledge graph |
| `god_nodes` | Find the most connected code concepts |

### Step 7 — Schedule nightly consolidation

One global cron entry — runs at 1am, reads `SYNAPTIC_SCAN_ROOT` from `.mcp.json`, discovers every project with a built graphify graph, and syncs each one.

**Linux / macOS** — add to crontab (`crontab -e`):

```bash
0 1 * * * cd <synaptic-memory-path> && python3.11 -m typed.consolidate \
    --synaptic-repo <synaptic-memory-path>
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

`StartWhenAvailable` ensures the task runs on next boot if the machine was asleep at 1am.

**Managing the task (PowerShell or via Claude):**

| Action | Command |
| --- | --- |
| Disable (pause) | `Disable-ScheduledTask -TaskName "synaptic-memory-consolidate"` |
| Enable | `Enable-ScheduledTask -TaskName "synaptic-memory-consolidate"` |
| Remove permanently | `Unregister-ScheduledTask -TaskName "synaptic-memory-consolidate" -Confirm:$false` |
| Re-create | Re-run the `Register-ScheduledTask` block above |

The task window appears briefly at 1am and closes on its own — do not close it while it is running.

**Logs (both platforms):**

| File | Contents |
| --- | --- |
| `~/.synaptic-memory/consolidate-report.json` | Full run report — drawers scanned, auto-pinned, contradictions, projects discovered/synced/failed |
| `~/.synaptic-memory/sync-errors.log` | Only created if errors occurred. On Windows, Notepad opens it automatically after a failed run. |

On Windows `~` resolves to `C:\Users\<your-username>`.

What consolidation does per run:

1. Walks all typed drawers — salience reranks and auto-pins top 5%
2. Detects contradictions (high-similarity opposing drawer pairs)
3. Flags stale drawers (references files modified since last run)
4. Archives zero-hit drawers older than 90 days
5. Discovers all projects under `SYNAPTIC_SCAN_ROOT` with a built graphify graph
6. Runs `mempal_to_graphify.py` + `graphify_wiki.py --clean` for each project
7. Writes `consolidate-report.json`; opens Notepad error log on Windows if anything failed

---

## Per-project setup

Only these things change per project — global hooks, MCP servers, and the consolidation cron never need to change.

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

### 3. Exclude files from the graph (`.graphifyignore`)

Place a `.graphifyignore` file in your project root to tell graphify which files and folders to skip. Uses fnmatch glob syntax (same as `.gitignore`). Graphify also walks up to parent directories (stopping at `.git`) so a single file at a monorepo root covers all sub-projects.

Graphify already skips common noise by default (`node_modules`, `__pycache__`, `.git`, `dist`, `build`, `graphify-out`, etc.). Only add patterns for things not in that built-in list.

```gitignore
# .graphifyignore

# Python artefacts
*.pyc
*.pyo
__pycache__/
*.egg-info/

# Virtual environments (if not named venv/.venv)
env/
.env/

# Test fixtures and coverage output
tests/fixtures/
htmlcov/

# Lock files (no useful graph signal)
*.lock
poetry.lock
requirements*.txt

# Secrets — never graph these
.env
*.pem
*.key
secrets/

# IDE noise
.idea/
.vscode/
```

Run `/graphify --update` after editing it to apply changes to an existing graph.

### 4. Scope tag (only needed without `mempalace.project.json`)

Hooks resolve scope in this order:

1. `SYNAPTIC_V2_SCOPE` env var — explicit override
2. `CLAUDE_PROJECT_SLUG` env var — set automatically by mempalace from `mempalace.project.json`
3. `Path.cwd().name` — last-resort fallback (current folder name)

If you have `mempalace.project.json` with a `project_slug` field (Step 1 above), scope is already correct — you do not need to set `SYNAPTIC_V2_SCOPE`. Only set it if you are skipping `mempalace.project.json` or need to override the slug.

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

Drawer counts are recorded automatically each time the Stop hook fires. View the weekly report at any time:

```bash
python3.11 -m typed.budget          # show baseline + weekly targets
```

To also record token counts manually:

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
| `.graphifyignore` | Yes | Files/folders excluded from graphify scanning |
| `.mcp.json` | No (gitignored) | Your local MCP config with real paths |
| `mempalace.project.json` | No (gitignored) | Your local per-project config |
| `mempalace.yaml` | No (gitignored) | Your local room definitions |
| `hooks/session_start.py` | Yes | SessionStart hook — spreading activation + graphify |
| `hooks/stop_15msg.py` | Yes | Stop hook (every 15 messages) — write-only |
| `hooks/pre_compact.py` | Yes | PreCompact hook — spreading activation + graphify before compaction |
| `hooks/pre_tool_write.py` | Yes | PreToolUse/Write hook — blocks writes to `.claude/memory/`, redirects to mempalace |
| `hooks/pre_tool_read.py` | Yes | PreToolUse/Read hook — file-targeted memory injection |
| `hooks/post_tool_edit.py` | Yes | PostToolUse/Edit hook — graphify neighbor surfacing after edits |
| `scripts/mempal_to_graphify.py` | Yes | Bridge: mine ChromaDB → inject into graphify |
| `scripts/graphify_wiki.py` | Yes | Obsidian wiki generator |
| `typed/` | Yes | Typed memory package (types, write, read, consolidate, telemetry, budget) |
| `tests/` | Yes | Unit tests |
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
| Hook error: `File "<string>", line 1` | Inline `-c` hook has shell quoting issue on Windows — replace with a `.py` file (all hooks in this repo now use `.py` files) |
| Hook not firing | Verify `python3.11 -m mempalace --help` resolves |
| MCP tools missing | Check `.mcp.json` exists in project root with correct paths |
| `mempalace.yaml` not found | Copy `mempalace.yaml.example` → `mempalace.yaml` in project root |
| graphify PreToolUse not firing | Ensure `graphify-out/graph.json` exists (run `/graphify` first) |
| 0 memory nodes in graphify | Complete a session so hooks fire, then run `mempal_to_graphify.py` |
| Obsidian shows no graph | Open `<project>/graphify-out/` not the synaptic-memory repo root |
| Palace HNSW diverged | Run `python3.11 -m mempalace repair --yes` |
| Consolidation task missing | Re-run the `Register-ScheduledTask` PowerShell block in Step 7 |
| `consolidate-report.json` empty | Task ran but mempalace has no typed drawers yet — complete sessions first |

---

## Roadmap

### Current status — testing phase (started May 2026)

The full stack is implemented and running:

- mempalace (ChromaDB) + typed layer — storing and retrieving memory across sessions
- graphify — building code knowledge graphs with community detection
- Claude Code hooks — SessionStart, Stop, PreCompact, PreToolUse/Read, PostToolUse/Edit all wired
- Nightly consolidation cron — auto-pinning, contradiction detection, stale flagging, multi-project sync
- `.graphifyignore` — per-project file exclusion
- Obsidian wiki — human-readable graph visualization
- Spreading activation retrieval — mempalace + graphify structural hops at every read moment
- Exponential decay / half-life salience — per-tier decay curves replace hard TTL cutoffs
- TTL-tiered expiration — EPHEMERAL (1d), SHORT_TERM (7d), LONG_TERM (90d), PERMANENT tiers

**The next 90 days are a testing and hardening period.** Real-world usage across multiple projects will surface edge cases, performance issues, and UX friction before a public release.

### Implemented — memory model improvements

After reviewing comparable systems ([resonantlabsai/synaptic](https://github.com/resonantlabsai/synaptic), [mikejaklitsch/synaptic](https://github.com/mikejaklitsch/synaptic), [jvanmelckebeke/mcp-synaptic](https://github.com/jvanmelckebeke/mcp-synaptic)), three features were added to the `typed/` layer without touching mempalace:

#### 1. Spreading activation retrieval

`spreading_activation_search()` in `typed/read.py` seeds from semantic matches then ripples outward through related drawers. When `graphify-out/graph.json` exists, each hop also extracts file/code references from drawer bodies and queries graphify for structural neighbors — memories about those neighboring files surface automatically. Used at SessionStart, PreCompact, PreToolUse/Read, and PostToolUse/Edit.

#### 2. Exponential decay / half-life

`salience()` in `typed/types.py` uses `exp(-ln2 * age_days / half_life)` per tier. Permanent drawers never decay. Ephemeral drawers (half-life 0.5 days) drop to near-zero in hours. Replaces flat recency scoring — drawers that keep getting cited stay ranked high indefinitely.

#### 3. TTL-tiered expiration

Four explicit tiers in `MemoryTier` (typed/types.py), driving both `salience()` decay and `_archive_old()` in `typed/consolidate.py`:

| Tier | TTL | Half-life | Use case |
| --- | --- | --- | --- |
| `ephemeral` | 1 day | 0.5 days | Scratch notes, temp context — archived even if used |
| `short-term` | 7 days | 3.5 days | Bug investigations, sprint context |
| `long-term` | 90 days | 45 days | Feature decisions, patterns (default) |
| `permanent` | Never | Never | Architecture decisions, core recipes |

Pinned drawers are always exempt from archiving regardless of tier.

### Done — retrieval audit + search-call budget (2026-06-10)

`spreading_activation_search()` now logs every call to `~/.synaptic-memory/retrieval-audit.jsonl` (ts, query, scope, top_k, results with drawer_id/score/type/snippet, duration_ms). Run `py -m typed.budget --retrieval-report` for a summary.

The 2-week audit (882 retrievals, 2026-05-24 → 2026-06-10) found the council's "wrong top-3" prerequisite was satisfiable (290/882 = 33% had top-1 score < 0.3, mostly graphify-hop noise on EMR.REPORTS code-symbol queries) — but the dominant failure was **latency, not relevance**: avg 24.5s per call, max 469s (7.8 min) on EMR.REPORTS. Root cause: hop expansion x graphify fan-out could trigger 100+ ChromaDB `client.search()` calls per invocation.

Fix applied in `typed/read.py`:

- `MAX_SEARCH_CALLS = 12` — hard cap on `client.search()` calls per invocation
- `_FRONTIER_FANOUT = 3` — only ripple outward from the 3 strongest activations per hop (was: all of them)
- Graphify fan-out reduced from 3 refs x 3 labels to `_GRAPHIFY_REFS_PER_HOP = 2` x `_GRAPHIFY_LABELS_PER_REF = 2`

All 56 existing tests pass unchanged. Re-check `--retrieval-report` after another week of usage to confirm avg duration has dropped from the 24.5s baseline.

### Planned — ADHD behavior layer (`typed/adhd.py`)

A non-linear retrieval layer that adds human-like associative behavior on top of `spreading_activation_search()`. Modeled on three ADHD cognitive patterns that outperform focused search for serendipitous discovery.

**Why:** Linear retrieval (query → top-k) misses bridging memories, cross-domain patterns, and high-value low-usage drawers. The ADHD layer surfaces what focused search buries — without replacing it.

**Status:** Retrieval is now instrumented (see "Done — retrieval audit" above) and the latency blocker has been fixed. Before building this layer, confirm with another `--retrieval-report` run that latency is under control, then re-evaluate whether the 33% low-relevance rate on code-symbol queries still justifies `QueryDriftLayer` / `InterruptLayer`.

#### The three modules

**1. InterruptLayer (Impulsivity) — build first, highest ROI**

Interrupt-driven early-exit retrieval. Surfaces a strong hit immediately without waiting for full hop expansion.

- Mode: `LOW` by default — fire on score >= 0.88 only
- Three interrupt types: `threshold` (score clears cutoff), `margin` (top-1 dominates top-2 by > 0.18), `saturation` (hop frontier has gone cold)
- **Do NOT use phasic gain score multiplier** — inflating scores before threshold check breaks the contract for all downstream callers
- `ImpulsivityMode` enum: `OFF / LOW / MEDIUM / HIGH` — hooks use `HIGH` (30ms budget), session start uses `LOW`

**2. QueryDriftLayer (Inattention) — build second**

Stochastic query mutation. With probability `p=0.05` (NOT 0.25 — too destructive), mutates the query before passing to core search via one of three strategies:

- **Temporal hop:** use most-recently-written drawer body as the query seed (PAM temporal co-occurrence)
- **Lexical tangent:** append a noun phrase extracted from a random high-salience drawer
- **Scope escape:** lift the wing filter and search globally instead of project-local

Uses Boltzmann sampling (`temperature=1.5`) instead of argmax top-k for neighbor selection. Tangent jumps originate from the **weakest** frontier member (not strongest), using the last 250 chars of its body as seed — trailing text is where tangential asides live.

Gate: drifted results must have `salience() > 1.5` to be admitted. Stale drawers excluded from tangent candidates.

**3. ParallelSearchLayer (Hyperactivity) — build last, never in default config**

Parallel burst searches across multiple query variants and cross-domain wings simultaneously. Uses `ThreadPoolExecutor` (ChromaDB is sync) with `asyncio.run_in_executor`.

- `burst_n=3` parallel variant queries (not 8 — token budget)
- `max_extra_drawers=2` cap — ADHD results compete with base results, never append on top
- `PreActivationCache` with **LRU eviction, max 100 entries** — required before shipping; without eviction this is a memory leak
- Background DMN loop (`asyncio.Task`): OFF until session persistence across invocations is verified — if Claude Code doesn't maintain a live Python process between turns, the loop never fires and is a no-op

#### Architecture

```
inject_session_start()
    └─► adhd_search(query, scope, config)          ← typed/adhd.py
            ① QueryDriftLayer.drift()              maybe mutate query/scope
            ② InterruptLayer.pre_check(seeds)      register strong hits before full search
            ③ spreading_activation_search()        existing core — UNCHANGED
            ④ ParallelSearchLayer.burst()          append up to 2 novel extras
            ⑤ InterruptLayer.post_merge()          prepend pre-registered hits, dedup
            ⑥ cap at SESSION_START_TOP_K=3
```

Single integration point: one line change in `inject_session_start()` in `typed/read.py`. Everything else untouched.

#### Default config

```python
ADHDConfig(
    enabled=False,           # OFF until retrieval failure is observed
    level=0,                 # 0=off, 1=impulse only, 2=+drift, 3=+burst
    p_inattention=0.05,      # NOT 0.25 — start conservative
    impulse_threshold=0.88,
    impulse_mode="prepend",  # "prepend" safe; "fast_path" for hooks only
    burst_n=3,
    max_extra_drawers=2,
    burst_timeout_ms=200,
)
```

Env var override: `SYNAPTIC_ADHD_LEVEL=1` enables impulse-only mode. `SYNAPTIC_ADHD_DISABLE=1` bypasses the layer entirely.

#### What NOT to build (scope boundary)

- **No serendipity feed / daily digest** — product roadmap item, not retrieval engineering
- **No phasic gain score multiplier** — correctness bug, not a tunable
- **No always-on DMN loop** — verify session persistence first
- **No fan-out width of 8** — 3 parallel queries is enough; 8 bloats the context window before the user issues a real query

#### Source research

- Foraging theory (Chevalier 2019): ADHD individuals retrieve more unique items via earlier patch departure + longer semantic jumps — `beta=0.004, p<0.02`
- PAM (arxiv 2602.11322): temporal co-occurrence achieves cross-boundary Recall@20=0.421 where cosine similarity scores zero
- RAG-R1 (arxiv 2507.02962): multi-query parallelism +13.2% recall, −11.1% latency vs sequential
- Stop-RAG (arxiv 2510.14337): value-based retrieval early stopping
- HNSW saturation early exit (Springer 2025): stop when frontier improvement < floor

---

### Planned — installer (after 90-day testing window)

After the testing period, synaptic-memory will be packaged as a proper installer — the goal is a single command that sets up the full stack:

```bash
# Target UX (not yet implemented)
pip install synaptic-memory
synaptic-memory install
```

The installer will handle:

- Dependency resolution (mempalace, graphify, correct Python versions)
- `~/.claude/settings.json` hook registration
- `.mcp.json` scaffolding per project
- `~/.mempalace/config.json` palace path setup
- Nightly consolidation cron registration (platform-aware: crontab on Linux/macOS, Task Scheduler on Windows)
- Upgrade path for existing setups

Until then, follow the manual setup steps in [INSTALL.ai.md](INSTALL.ai.md).
