# AI Install Guide — synaptic-memory

> **For AI assistants:** Read this file top to bottom and execute each step in order.
> This installs the full synaptic-memory stack for the user: mempalace (ChromaDB memory) + graphify (code graph) + Claude Code hooks + per-project wiring.
> Ask the user to confirm their Python versions and storage path before starting. Everything else you can execute directly.

---

## What you are installing

| Component | Purpose | Requires |
|---|---|---|
| mempalace | Stores Claude Code session memory in ChromaDB via semantic embeddings | Python 3.11 |
| graphify | Builds a knowledge graph from the codebase via AST extraction | Python 3.14 |
| Claude Code hooks | Triggers memory saves automatically during every session | Claude Code CLI |
| bridge script | Mines memory into ChromaDB, injects memory nodes into the graph | Python 3.11 |
| Obsidian | Visualizes the graph — open `graphify-out/` as a vault | Manual install |

---

## Step 0 — Ask the user these questions before starting

Ask:
1. **Python versions**: Run `py -3.11 --version` and `py -3.14 --version`. Are both available?
   - If 3.11 missing: install from https://www.python.org/downloads/ (required for mempalace)
   - If 3.14 missing: install from https://www.python.org/downloads/ (required for graphify)
2. **Storage path**: Where should memory be stored? (e.g. `D:\.lmstudio\Memory`)
   - This becomes `memory_archive` in `mempalace.project.json`
   - The palace (ChromaDB) will be stored at `<storage_path>/palace`
3. **Project info**: What is this project's name/slug and a short wing name?
   - `project_slug` — partial match against the Claude Code project folder (e.g. `EMR-REPORTS`)
   - `wing` — short unique tag for this project in ChromaDB (e.g. `EMR.REPORTS`)
4. **Clone location**: Where to clone mempalace and graphify? (e.g. `E:\Allan Project\Git Repo Project\`)

Record the answers — you will use them in Steps 1–6.

---

## Step 1 — Clone and install mempalace (Python 3.11)

```bash
# Replace <clone_location> with the user's chosen path
cd "<clone_location>"
git clone https://github.com/milla-jovovich/mempalace.git
py -3.11 -m pip install -e mempalace/
```

Configure the palace path:

```bash
# Create config dir if it doesn't exist
mkdir -p ~/.mempalace
```

Write `~/.mempalace/config.json` — replace `<storage_path>` with the user's chosen storage path:

```json
{
  "palace_path": "<storage_path>/palace"
}
```

Verify:
```bash
py -3.11 -m mempalace --help
```

---

## Step 2 — Clone and install graphify (Python 3.14)

```bash
cd "<clone_location>"
git clone https://github.com/safishamsi/graphify.git
py -3.14 -m pip install -e graphify/
```

Verify:
```bash
py -3.14 -c "import graphify; print('graphify ok')"
```

---

## Step 3 — Install Obsidian (manual — tell the user)

Tell the user:
> Download Obsidian from https://obsidian.md — free desktop app, no account needed.
> After setup, you will open `graphify-out/` inside your project as a vault.

No commands needed — proceed to Step 4.

---

## Step 4 — Install Claude Code hooks (global, one-time)

Copy hooks from the mempalace repo (cloned in Step 1):

```bash
cp "<clone_location>/mempalace/hooks/mempal_save_hook.sh" ~/.claude/hooks/
cp "<clone_location>/mempalace/hooks/mempal_precompact_hook.sh" ~/.claude/hooks/
chmod +x ~/.claude/hooks/mempal_save_hook.sh
chmod +x ~/.claude/hooks/mempal_precompact_hook.sh
```

Now update `~/.claude/settings.json` to register both hooks.
Read the existing file first. Then add the hooks block (merge with any existing hooks — do not overwrite unrelated settings):

```json
{
  "hooks": {
    "Stop": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "/absolute/path/to/.claude/hooks/mempal_save_hook.sh",
        "timeout": 30
      }]
    }],
    "PreCompact": [{
      "hooks": [{
        "type": "command",
        "command": "/absolute/path/to/.claude/hooks/mempal_precompact_hook.sh",
        "timeout": 30
      }]
    }]
  }
}
```

**Important:** Replace `/absolute/path/to/` with the real absolute path (e.g. `C:/Users/<username>/.claude/hooks/`).

Verify hooks file exists:
```bash
ls ~/.claude/hooks/
```

---

## Step 5 — Add synaptic-memory scripts to the project

Copy the two bridge scripts into the user's project:

```bash
# Replace <project_root> with the user's project directory
cp scripts/mempal_to_graphify.py "<project_root>/scripts/"
cp scripts/graphify_wiki.py "<project_root>/scripts/"
```

Create empty required folder:
```bash
mkdir -p "<project_root>/mempalace-refs"
```

---

## Step 6 — Create per-project config files

**`<project_root>/mempalace.project.json`** — fill in using answers from Step 0:

```json
{
  "project_slug": "<project_slug>",
  "wing":         "<wing>",
  "memory_archive": "<storage_path>"
}
```

Example:
```json
{
  "project_slug": "EMR-REPORTS",
  "wing":         "EMR.REPORTS",
  "memory_archive": "/your/chosen/memory/storage"
}
```

**`<project_root>/mempalace.yaml`** (project root, not inside mempalace-refs/) — copy and rename wing:

```yaml
wing: <wing>
rooms:
- name: decisions
  description: Architecture and design decisions made during development
  keywords:
  - decision
  - chose
  - created
  - refactor
  - changed
- name: features
  description: Features implemented and their context
  keywords:
  - feature
  - implement
  - added
  - api
  - endpoint
- name: bugs
  description: Bugs found and fixes applied
  keywords:
  - bug
  - fix
  - error
  - issue
  - exception
- name: setup
  description: Environment and tooling setup
  keywords:
  - install
  - setup
  - configure
  - hook
  - cron
- name: general
  description: General project notes and context
  keywords:
  - note
  - context
  - general
```

---

## Step 7 — Verify the full setup

Run the bridge script for the first time:

```bash
cd "<project_root>"
py -3.11 scripts/mempal_to_graphify.py
```

Expected output:
```
[mempal_bridge] Mine complete.
[mempal_bridge] Graphify rebuild complete.
[mempal_bridge] Created <M> memory→code links
[mempal_bridge] Injected <N> memory node(s) → graph.json
```

Then generate the Obsidian wiki:
```bash
python scripts/graphify_wiki.py --clean
```

Tell the user:
> Open Obsidian → **Open folder as vault** → select `graphify-out/` inside your project.
> Press `Ctrl+G` to see the full graph. Memory nodes appear as `[MEM] <name>` linked to related code.

---

## Step 8 — Tell the user how to use it daily

After each Claude Code session:
```bash
py -3.11 scripts/mempal_to_graphify.py   # mine + rebuild + inject
python scripts/graphify_wiki.py --clean  # refresh Obsidian
```

Hooks fire automatically during sessions — no manual trigger needed.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `py -3.11` not found | Install Python 3.11 from python.org; check "Add to PATH" |
| `py -3.14` not found | Install Python 3.14 from python.org; check "Add to PATH" |
| `No module named 'chromadb'` | Run `py -3.11 -m pip install -e <clone_location>/mempalace/` |
| `No module named 'graphify'` | Run `py -3.14 -m pip install -e <clone_location>/graphify/` |
| Mine warning on first run | Normal — palace is empty; run again after a Claude session |
| `mempalace.yaml` not found | Ensure `mempalace.yaml` exists in the project root (copy from `mempalace.yaml.example`) |
| 0 memory nodes injected | No Claude sessions recorded yet for this wing; complete a session first |
| Obsidian shows no graph | Open the correct folder: `<project_root>/graphify-out/` not the repo root |
