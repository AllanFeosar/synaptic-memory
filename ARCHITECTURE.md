# synaptic-memory — Architecture

```text
                   ┌──────────────────────────────────────┐
                   │           CLAUDE CODE SESSION         │
                   │                                       │
                   │   working memory (context window)     │
                   └────┬──────────────────┬───────────────┘
                        │                  │
          SessionStart  │                  │   write_decision()
          hook          │                  │   write_anti_pattern()
                        │                  │   write_recipe()
                        ▼                  ▼
         ┌──────────────────┐    ┌────────────────────┐
         │  read.py         │    │  write.py          │
         │  - search_typed  │    │  - typed tuple     │
         │  - inject ~150t  │    │  - embedding dedup │
         │  - expand lazy   │    │  - supersedes link │
         │  - file_summary  │    │  - frontmatter     │
         │  ┌─────────────┐ │    └─────────┬──────────┘
         │  │  adhd.py    │ │              │
         │  │ InterruptLayer│              │
         │  │ check_seeds │ │              │
         │  │ post_merge  │ │              │
         │  └─────────────┘ │              │
         └────────┬─────────┘              │
                  │                        │
                  └─────────┬──────────────┘
                            ▼
                ┌──────────────────────┐
                │    client.py         │
                │  InProcessClient     │
                │  get_or_create()     │  ← process-level singleton
                │  _col cache          │  ← 1 HNSW load per process
                └──────────┬───────────┘
                           │
            ┌──────────────▼──────────────┐
            │    mempalace (Python 3.11)  │
            │    ChromaDB drawers         │
            │    Wing / Room / Drawer     │
            └──────────────┬──────────────┘
                           │
    ────── nightly 1am ────┘
                           │
                           ▼
            ┌────────────────────────────┐
            │    consolidate.py          │
            │  - salience rerank         │
            │  - contradiction detect    │
            │  - stale-flag (file mtime) │
            │  - archive by tier TTL     │
            └──────────────┬─────────────┘
                           │
                           ▼
            ┌────────────────────────────┐
            │  scripts/                  │
            │  mempal_to_graphify.py     │
            │  (subprocess, Python 3.14) │
            └──────────────┬─────────────┘
                           │
                           ▼
            ┌────────────────────────────┐
            │    graphify (offline)      │
            │  Code knowledge graph      │
            │  god nodes, communities    │
            │  graphify-out/wiki/        │
            └──────────────┬─────────────┘
                           │
                           ▼
                      Obsidian vault
                      (human read-only)

         ┌──────────────────────────────────┐
         │  config.py  (all modules read)   │
         │  ~/.synaptic-memory/config.json  │
         │  → retrieval / adhd /            │
         │    consolidation / write /       │
         │    telemetry / budget            │
         └──────────────────────────────────┘
```

## Why graphify stays offline

The Python 3.11/3.14 split makes live in-process integration costly.
graphify's value is *structural awareness* (god nodes, communities,
decision-to-code links), not retrieval ranking — mempalace's embeddings
already handle retrieval. Putting graphify in the hot path adds a serial
bottleneck for marginal gain.

The nightly consolidation cron is when graphify earns its keep: it enriches
mempalace drawers with `stale` flags and pinned status based on graph
analysis, then renders the Obsidian wiki. Its palace writes (auto-pin, archive,
stale-marking) **defer cleanly** when a live process holds the palace write-lock
(`report.palace_locked`, via `_write_drawer_deferring_lock` in
`typed/consolidate.py`) instead of erroring — a lock-busy run is retried next
time, not surfaced as a nightly error popup.

## Memory tier mapping

| Tier       | Storage                   | Lifespan                        | Created by                      |
|------------|---------------------------|---------------------------------|---------------------------------|
| Working    | Claude's context window   | turn                            | the conversation                |
| Episodic   | mempalace drawers         | 1d / 7d / 90d (by MemoryTier)  | `write_decision`, `write_*`     |
| Semantic   | pinned drawers + graphify | ∞                               | consolidate.py auto-pins top 5% |
| Procedural | drawers with type=recipe  | ∞                               | `write_recipe`                  |

## Token math

Per session (estimate — drawer counts are auto-recorded each Stop hook; view with `python3.11 -m typed.budget`):

| Path                 | Without v2 | With v2 | Δ           |
|----------------------|------------|---------|-------------|
| SessionStart context | 0          | +150    | +150        |
| File loads (large)   | ~10,000    | ~7,000  | −3,000      |
| Decision recap       | ~10,000    | ~500    | −9,500      |
| Drawer writes        | 0          | ~2,000  | +2,000      |
| **Net per session**  | ~50,000    | ~39,600 | **−10,000** |

Over 100 sessions: ~1M tokens saved. Compounds as the palace gets denser.

## Coupling surface (what breaks if mempalace updates)

| What changes in mempalace                    | What breaks here                                    |
|----------------------------------------------|-----------------------------------------------------|
| Hook CLI flags / subcommands                 | each project's `.claude/settings.json` — manual edit |
| `mempalace.config.MempalaceConfig` fields    | `typed/client.py` `__init__` only                   |
| `mempalace.palace.get_collection` signature  | `typed/client.py` `_collection()` only              |
| `mempalace.searcher.build_where_filter` API  | `typed/client.py` `search()` only                   |
| ChromaDB metadata field names                | `typed/client.py` `add_drawer()` / `search()` only  |

All business logic (`write.py`, `read.py`, `adhd.py`, `consolidate.py`, `telemetry.py`, `budget.py`)
talks only to the `MempalaceClient` abstract interface — if mempalace updates, fix one file
(`typed/client.py`). All tunables are centralized in `typed/config.py` — no constants are
scattered in module bodies.

## Config coupling surface (what breaks if config.json schema changes)

| What changes                                 | What breaks here                                    |
|----------------------------------------------|-----------------------------------------------------|
| Section name renamed (e.g. `retrieval` → `r`)| All callers of `get_config().<section>` in 6 modules|
| Field added with no default                  | Nothing — `_merge()` only sets fields that exist    |
| Field removed                                | Nothing — callers use the dataclass default         |
| `CONFIG_PATH` moved                          | `typed/config.py` `CONFIG_PATH` constant only       |
