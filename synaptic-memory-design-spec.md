# Synaptic Memory — Operational Design Spec

> Final design for using mempalace + graphify together to reduce token usage,
> compound Claude's retrieval quality over time, and approximate (not imitate)
> brain-like memory.

---

## 0. Framing corrections (skip the cargo cult)

**The brain analogy is inspiration, not architecture.** Real brains forget on purpose, confabulate, hold ~7 working items, recall by reconstruction. None of that helps an LLM. What DOES help, drawn from the analogy:

- Separation between fast/short-term capture and slow/durable indexing
- Offline consolidation (a "sleep" pass)
- Salience-based recall (most-used surfaces first)
- Forgetting curve for irrelevance (not for compression)

Use those four. Drop the rest.

**"Reduce tokens" and "smarter Claude" are NOT in tension.** Smarter ≠ more context. Smarter = *better* context. A 200-token decision summary beats a 5,000-token re-explanation every time.

**Don't fuse mempalace and graphify in the hot path.** They solve different problems:

- mempalace = retrieval (vector DB, semantic search) — primary memory query layer
- graphify = structural awareness (code graph, decision links) — OFFLINE consolidator and architectural index

Claude queries mempalace directly. Graphify runs nightly to enrich mempalace with structural edges and surface cross-project patterns. This avoids the Python 3.11/3.14 architectural conflict in the live path.

---

## 1. Full Session Flow

```text
Claude Code session
        │
        ├─ SessionStart hook
        │       → mempalace session-start  (loads past session memory)
        │       → typed/read.py inject_session_start  (top-3 typed summaries ~150 tokens)
        │       ← graphify: NOT here (graph loads lazily on demand)
        │
        │   [work happens]
        │       │
        │       ├─ Claude uses Glob or Grep tool
        │       │       ↑ PreToolUse hook fires FIRST
        │       │       → checks if graphify-out/graph.json exists
        │       │       → injects: "check GRAPH_REPORT.md before searching files"
        │       │       → Claude reads GRAPH_REPORT.md or calls graphify MCP tools:
        │       │               query_graph("how does auth work?")
        │       │               get_node("UserService")
        │       │               get_neighbors("config")
        │       │               shortest_path("client", "database")
        │       │
        │       └─ User types /graphify
        │               → Claude calls graphify MCP tools directly to traverse graph
        │
        ├─ Stop hook (every ~15 messages)
        │       → typed/write.py decides what to save  (session memory → mempalace)
        │       → mempalace stop hook
        │       ← graphify: NOT here (graph stores code structure, not session memory)
        │
        └─ PreCompact hook
                → typed/read.py search_typed  (injects pinned summaries before compaction)
                → mempalace precompact hook
                ← graphify: NOT here

[After session ends — manual or nightly cron]
        python scripts/mempal_to_graphify.py
        → reads all mempalace memories
        → injects as [MEM] nodes into graphify graph
        → links memory to code (keyword match on class/function names)
        python scripts/graphify_wiki.py --clean
        → regenerates Obsidian vault in graphify-out/
```

Key invariant: **graphify never runs in the session hot path.** It loads on demand via MCP
tools when Claude explicitly needs structural code awareness, and syncs offline after sessions.

---

## 2. The Four Memory Tiers

| Tier | What | Where | Lifespan | Created by |
| --- | --- | --- | --- | --- |
| **Working** | Current session context | Claude's context window | This turn | Claude |
| **Episodic** | What happened (decisions, debugs, postmortems) | mempalace drawers | 90 days default | `mempalace_add_drawer` |
| **Semantic** | Cross-session patterns, architecture, anti-patterns | graphify nodes + pinned drawers | ∞ | Consolidation cron |
| **Procedural** | Recipes, workflows, scripts | mempalace `room=recipes` drawers | ∞ | Manual + diary_write |

Each drawer must declare its tier on creation. No untyped drawers.

---

## 3. WRITE Protocol

### Required tag tuple on every drawer

```yaml
type: decision | pattern | anti-pattern | recipe | postmortem | summary
scope: <project_slug> | global
confidence: low | medium | high
supersedes: <drawer_id> | null
```

Untyped writes are rejected. This is the single biggest fix to retrieval quality — without it, the graph has no semantics.

### When to write

- **`add_drawer`** — any decision that took >2 minutes of reasoning, any postmortem, any pattern observed in 2+ files, any anti-pattern Claude was about to repeat
- **`diary_write`** — only at session end, only the compressed summary
- **PreCompact hook** — call `check_duplicate` BEFORE writing; do not write from inside PreCompact

### When to skip

- Trivial code edits, typo fixes, debugging traces (those are working memory)
- Anything Auto Memory already captures (don't double-write)

### Stop@15msgs hook

Captures one `summary` drawer with the last 15 messages compressed. Does NOT capture individual decisions — those go through explicit `add_drawer` calls during the session. Also records drawer count to `typed/budget.py` for token-savings tracking.

---

## 4. READ Protocol

### SessionStart hook

```text
1. mempalace_search(query=current_repo + recent_intent, top_k=3)
2. Inject SUMMARIES ONLY (50 tokens each) into context, plus drawer_id
3. Claude can call mempalace_get_drawer(id) on demand to expand
```

Token cost of SessionStart: ~150 tokens injected. Token saved: 1500-3000 (skipping recap of past decisions).

### Mid-session

Claude only calls `mempalace_search` in three situations:

1. About to load a large file (>300 lines) — try `search(file_summary)` first
2. About to make a decision that smells familiar — search past decisions on the same scope
3. About to repeat a pattern — search for anti-patterns first

Default: don't search. Working memory should handle the active task.

### Retrieval response shape (token-disciplined)

```text
[3 summaries returned, each: id + 1-line + supersedes_chain + staleness_flag]
```

Never inject full drawer text into context unless Claude explicitly expands.

---

## 5. CONSOLIDATION (the sleep cycle)

Runs nightly at 1am via OS scheduler (crontab on Linux/macOS, Task Scheduler on Windows — see README Step 4). This is where graphify earns its keep.

```text
1. python scripts/mempal_to_graphify.py
   — Pull new drawers since last sync
   — Generate semantic edges between drawers (cosine similarity, NOT keyword match)
   — Update graphify nodes; rebuild GRAPH_REPORT.md

2. Rerank salience
   — salience = (usage_count × 2) + recency_decay + (pin_status × 5)
   — Top 5% per scope get auto-pinned (semantic tier)

3. Contradiction detection
   — Find drawer pairs with high embedding similarity + opposing valences
   — Flag for human review (don't auto-resolve)

4. Stale detection
   — For each drawer, check if referenced files changed since last sync
   — Mark stale=true (don't delete; flag in retrieval results)

5. Forgetting (not deletion)
   — Drawers with 0 hits in 90 days AND not pinned AND not graph-referenced
   — Move to "archive" wing (still searchable with --include-archive flag)

6. Token-budget report
   — Estimate next-session savings vs. baseline
   — Append to ~/.synaptic-memory/usage.log
```

No drawer is ever hard-deleted. Rollback must always be possible.

---

## 6. Trust Calibration (the feedback loop)

This is the part that makes Claude smarter over time.

### Per-drawer telemetry (lightweight, write to drawer metadata)

```yaml
last_used_at: <timestamp>
usage_count: <int>
cite_then_correct: <int>   # times Claude used it AND user corrected the result
```

### How calibration works

- Drawers that get cited and not corrected → salience boost next consolidation
- Drawers that hit `cite_then_correct` ≥ 2 → confidence drops to `low`
- Low-confidence drawers are still returned but with a `[low_confidence]` flag in the summary

This is the closest analogue to the brain analogy that's actually useful: the system literally weights what it's used effectively.

---

## 7. Token Budget & Kill Switch

### Baseline

Run 1 week without the system. Record avg tokens/session. Call this `T0`.

### Targets

- Week 4: tokens/session ≤ 0.80 × T0 (20% reduction)
- Week 8: tokens/session ≤ 0.70 × T0 (30% reduction)
- Write overhead must stay below 30% of read savings (otherwise it's net negative)

### Kill switch

If week 4 shows <10% reduction → simplify aggressively:

- Disable graphify (keep it offline-only, no consolidation)
- Reduce drawer write threshold to 5+ minute decisions only
- Re-baseline at week 6

If week 8 still <15% → archive mempalace, fall back to Auto Memory + a flat `decisions.md` file.

---

## 8. Failure Modes (what the Contrarian was right about)

These will happen. Prevent or detect each.

| Failure | Prevention |
| --- | --- |
| Knowledge poisoning (bad decision saved high-confidence) | Confidence field + cite_then_correct telemetry → auto-demote |
| Stale memory poisoning new sessions | Stale detection + warning flag in retrieval |
| Top-k returns 5-10 marginal drawers | Strict top_3 + summary-only injection + Claude rejects irrelevant on first read |
| Hash-based dedupe permits semantic dupes | check_duplicate uses embedding similarity, not content hash |
| Stop@15msgs captures stale state | Hook captures summary, not individual decisions; decisions come from explicit calls |
| Python 3.11/3.14 split breaks the bridge | Bridge stays offline; runtime never depends on graphify's Python |
| Drift (vocabulary shift after 20+ sessions) | Quarterly: re-embed all drawers; check embedding-space drift |

---

## 9. The Token Math (why this works)

### Average session today (estimated)

- 50,000 tokens
- ~30% is recapping prior decisions and re-explaining context
- ~20% is loading full files for context
- ~50% is the actual work

### Average session with this system

- ~3 SessionStart summaries × 50 tokens = 150 tokens (was: 0)
- File summary hits replace ~30% of full-file loads = save ~3000 tokens
- Decision recap collapses to summary refs = save ~10,000 tokens
- Write overhead: ~2000 tokens/session (drawer creation + embedding)

**Net per session: ~10,000-15,000 tokens saved (~25% of T0).**

Over 100 sessions: 1M-1.5M tokens. Real money. And the system gets sharper as drawers get cited/calibrated.

---

## 10. What This Repo Provides

All six items from the original gap analysis are now implemented in `typed/`:

1. **Typed drawers (tag tuple)** — `typed/write.py` enforces type, scope, confidence on every write.
2. **Embedding-based dedupe** — cosine similarity check before write; raises `DuplicateDrawerError` at > 0.92.
3. **Supersedes field** — `write_decision(supersedes=drw_xxx)` links and archives the prior drawer.
4. **Salience reranking** — `typed/consolidate.py` reranks by usage_count + recency + pin_status nightly.
5. **Stale-flag detection** — consolidation checks referenced file mtimes; marks `stale=true` in metadata.
6. **cite_then_correct telemetry** — `typed/telemetry.py` demotes confidence to `low` after 2 corrections.

All six run through `InProcessClient` in `typed/client.py` — no subprocess, no CLI dependency.

---

## 11. Anti-Patterns (don't do this)

- ❌ Fusing graphify into the mempalace retrieval path — graphify answers structural questions, not memory recall; keep them separate
- ❌ Loading full drawers into context — always inject summaries with id
- ❌ Writing to mempalace from PreCompact — use that hook to READ only
- ❌ Hard-deleting drawers — always archive, never delete
- ❌ Auto-resolving contradictions — flag for human review
- ❌ Stop@15msgs writing decisions — only writes summaries; decisions are explicit

---

## 12. The First Move (baseline first)

Everything in sections 3–10 is already implemented. The only thing left is proving it saves tokens on your real workflow.

1. Run 1 week normally — record avg tokens/session (`T0`)
2. Enable hooks + MCP servers as per README Step 5–6
3. Check `mempalace status` at week's end — look for drawer growth
4. At week 4, compare tokens/session to `T0`

If tokens dropped ≥ 20%: the system is real, build on it.
If tokens dropped < 10%: retrieval quality is the bottleneck — improve search queries before adding more features.

---

## 13. The honest test

Open a session next week. Ask yourself: *"In what specific moment did mempalace save me from re-explaining something, or save me from making a mistake I already learned about?"*

If you can name 3 moments by Friday, the system is real.
If you can't, the system is decorative — and you should rip out everything except the embeddings + recency rerank.
