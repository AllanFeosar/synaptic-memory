"""
mempal_to_graphify.py
─────────────────────
Bridge: mempalace palace (ChromaDB) → graphify graph nodes

HOW IT WORKS:
  1. Reads mempalace.project.json for: project_slug, wing, memory_archive
  2. Runs graphify rebuild (code nodes only)
  3. Connects to ChromaDB palace at memory_archive/palace
  4. Pulls ALL documents tagged with this project's wing from ChromaDB
  5. Injects them as MEMORY nodes directly into graphify-out/graph.json

WHY INJECT DIRECTLY:
  graphify's collect_files() only scans code extensions (.cs, .py, .ts etc).
  .md files and excluded folders (like mempalace-refs/) are never picked up.
  Direct injection into graph.json is the only reliable way to add memory nodes.

REUSABLE: Drop this script into any project unchanged.
The only thing that changes per project is mempalace.project.json:
  {
    "project_slug": "MY-PROJECT",
    "wing":         "MyProject",
    "memory_archive": "D:\\.lmstudio\\Memory"
  }

USAGE:
  py -3.11 scripts/mempal_to_graphify.py             # full run
  py -3.11 scripts/mempal_to_graphify.py --bridge-only  # inject only, skip rebuild
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

# ── Locate project root (one level up from this script) ──────────────────────
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
GRAPH_JSON   = PROJECT_ROOT / "graphify-out" / "graph.json"
CONFIG_FILE  = PROJECT_ROOT / "mempalace.project.json"

# Exit code signaling "the palace was locked by another process (e.g. a live
# mempalace MCP server) — skip this run, safe to retry later." consolidate.py
# checks for this exact code and reports it as a soft skip, not a failure.
EXIT_SKIPPED = 75


# ── Load project config ───────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print(f"[mempal_bridge] ERROR: {CONFIG_FILE} not found.")
        print('  Create mempalace.project.json with:')
        print('  {"project_slug": "...", "wing": "...", "memory_archive": "..."}')
        sys.exit(1)
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


# ── Resolve palace path ───────────────────────────────────────────────────────
def resolve_palace_path(memory_archive: str) -> Path:
    """
    Primary:  memory_archive/palace  (from mempalace.project.json)
    Fallback: ~/.mempalace/config.json → palace_path
    """
    if memory_archive:
        candidate = Path(memory_archive) / "palace"
        if candidate.exists():
            return candidate
        print(f"[mempal_bridge] WARNING: {candidate} not found, trying fallback.")

    config_file = Path.home() / ".mempalace" / "config.json"
    if config_file.exists():
        with open(config_file, encoding="utf-8") as f:
            cfg = json.load(f)
        palace = cfg.get("palace_path", "")
        if palace:
            return Path(palace)

    print("[mempal_bridge] ERROR: Cannot locate palace.")
    print("  Set memory_archive in mempalace.project.json or run: mempalace init <dir>")
    sys.exit(1)


# ── Find Claude memory dir for this project ──────────────────────────────────
def find_memory_dir(project_slug: str) -> Path | None:
    """
    Finds ~/.claude/projects/<folder>/memory/ where folder name contains project_slug.
    """
    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.exists():
        return None
    for folder in claude_projects.iterdir():
        if project_slug.lower() in folder.name.lower():
            memory_dir = folder / "memory"
            if memory_dir.exists():
                return memory_dir
    return None


# ── Bounded subprocess runner (kills the whole process tree on timeout) ──────
def _run_with_timeout(cmd: list[str], timeout: int) -> subprocess.CompletedProcess | None:
    """
    Runs cmd, returns None on timeout instead of raising.

    Windows launchers like "py -3.11" spawn a real python.exe as a child —
    subprocess.run(timeout=...) only kills the launcher itself, orphaning the
    interpreter (and whatever lock it's holding). We track the PID ourselves
    and use `taskkill /T` to kill the full tree on timeout.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.kill()
        proc.communicate()
        return None


# ── Mine Claude memory into palace ────────────────────────────────────────────
def mine_memory_into_palace(memory_dir: Path, wing: str) -> bool:
    """
    Runs: py -3.11 -m mempalace mine <memory_dir> --wing <wing>
    Tags all Claude memory files for this project into ChromaDB.
    Copies mempalace.yaml into memory_dir first (required by mempalace mine).

    Returns True if the palace was busy/locked and mining had to be skipped.
    """
    # mempalace mine requires mempalace.yaml in the target dir
    # Look in project root first, then mempalace-refs/ as fallback
    yaml_src = PROJECT_ROOT / "mempalace.yaml"
    if not yaml_src.exists():
        yaml_src = PROJECT_ROOT / "mempalace-refs" / "mempalace.yaml"
    if yaml_src.exists():
        shutil.copy2(yaml_src, memory_dir / "mempalace.yaml")
    else:
        print("[mempal_bridge] WARNING: mempalace.yaml not found — mine may fail.")

    print(f"[mempal_bridge] Mining {memory_dir} → palace (wing={wing})...")
    mine_cmd = ["-m", "mempalace", "mine", str(memory_dir), "--wing", wing]
    result = None
    for py in ("py -3.11", "python3.11", "python3", "python"):
        cmd = py.split() + mine_cmd
        result = _run_with_timeout(cmd, timeout=60)
        if result is None:
            # Every launcher hits the same palace file — retrying with a
            # different interpreter won't unstick a lock. Stop immediately.
            print(
                "[mempal_bridge] SKIPPED: palace busy (locked by another process, "
                "e.g. a live mempalace MCP server) — mine timed out after 60s"
            )
            return True
        if result.returncode == 0:
            break
        if "No module named" not in (result.stderr or ""):
            break  # real error, stop trying
    if result and result.returncode == 0:
        print("[mempal_bridge] Mine complete.")
    else:
        out = (result.stdout or "") + (result.stderr or "") if result else ""
        print(f"[mempal_bridge] Mine warning (exit {result.returncode if result else '?'}): {out[-300:] if out else 'no output'}")
    return False


# ── Query ChromaDB by wing ────────────────────────────────────────────────────
def query_palace_by_wing(palace_path: Path, wing: str) -> list[dict]:
    """
    Returns all documents tagged with this wing.
    Each entry: {text, wing, room, source_file}

    Runs the actual ChromaDB open+query on a daemon thread with a bounded
    wait: chromadb.PersistentClient() blocks forever (not a slow query — a
    real lock-wait) if another process already has this palace open, e.g. a
    live mempalace MCP server. A daemon thread (not ThreadPoolExecutor, whose
    worker threads are joined at interpreter exit) plus os._exit() lets the
    process leave promptly without waiting on the stuck worker.
    """
    try:
        import chromadb
    except ImportError:
        print("[mempal_bridge] ERROR: chromadb not installed.")
        print("  Run: py -3.11 -m pip install chromadb")
        sys.exit(1)

    result_q: "queue.Queue[tuple[str, object]]" = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            client = chromadb.PersistentClient(path=str(palace_path))
            col = client.get_collection("mempalace_drawers")
            res = col.get(where={"wing": wing}, include=["documents", "metadatas"])
            result_q.put(("ok", res))
        except Exception as e:  # noqa: BLE001
            result_q.put(("error", e))

    threading.Thread(target=_worker, daemon=True).start()
    try:
        status, payload = result_q.get(timeout=30)
    except queue.Empty:
        print(
            "[mempal_bridge] SKIPPED: palace busy (locked by another process, "
            "e.g. a live mempalace MCP server) — wing query timed out after 30s"
        )
        os._exit(EXIT_SKIPPED)  # force-exit; the stuck worker thread is abandoned

    if status == "error":
        print(f"[mempal_bridge] ERROR: Cannot open palace at {palace_path}: {payload}")
        sys.exit(1)

    result = payload
    docs  = result.get("documents", [])
    metas = result.get("metadatas", [])

    return [
        {
            "text":        doc,
            "wing":        meta.get("wing", wing),
            "room":        meta.get("room", "general"),
            "source_file": meta.get("source_file", ""),
        }
        for doc, meta in zip(docs, metas)
    ]


# ── Build code label → node id lookup ────────────────────────────────────────
def build_code_label_index(graph: dict) -> dict[str, str]:
    """
    Returns {label: node_id} for all code nodes with label length > 4.
    Excludes file path labels (contain / or \\) and purely numeric labels.
    """
    index = {}
    for n in graph["nodes"]:
        if n.get("file_type") != "code":
            continue
        label = n.get("label", "")
        if len(label) <= 4:
            continue
        if "/" in label or "\\" in label:
            continue
        if label.isdigit():
            continue
        index[label] = n["id"]
    return index


# ── Inject memory nodes directly into graph.json ─────────────────────────────
def inject_memory_nodes(hits: list[dict], wing: str) -> int:
    """
    Reads graphify-out/graph.json, strips stale memory nodes,
    injects fresh nodes + edges from palace, writes back.
    Edges: memory_node --relates_to--> code_node (keyword match on text).
    """
    if not GRAPH_JSON.exists():
        print(f"[mempal_bridge] ERROR: {GRAPH_JSON} not found. Run graphify rebuild first.")
        sys.exit(1)

    with open(GRAPH_JSON, encoding="utf-8") as f:
        graph = json.load(f)

    # Strip previously injected memory nodes and their links
    graph["nodes"] = [n for n in graph["nodes"] if n.get("file_type") != "memory"]
    graph["links"] = [
        lnk for lnk in graph["links"]
        if not lnk.get("source", "").startswith("mem__")
        and not lnk.get("target", "").startswith("mem__")
    ]

    # Assign memory nodes to a dedicated community (max + 1)
    max_community = max((n.get("community", 0) for n in graph["nodes"]), default=0)
    memory_community = max_community + 1

    # Build code label index for keyword matching
    code_index = build_code_label_index(graph)
    total_links = 0

    def slugify(s: str) -> str:
        return re.sub(r"[^a-z0-9_]", "_", s.lower())[:80]

    for i, hit in enumerate(hits):
        source_file = hit["source_file"]
        room        = hit["room"]
        text        = hit["text"]
        name        = Path(source_file).stem if source_file else f"{wing}_{room}_{i}"
        node_id     = f"mem__{slugify(name)}_{i}"
        label       = f"[MEM] {name}"

        graph["nodes"].append({
            "label":           label,
            "file_type":       "memory",
            "source_file":     source_file,
            "source_location": "",
            "id":              node_id,
            "community":       memory_community,
            "wing":            hit["wing"],
            "room":            room,
        })

        # Link to code nodes whose labels appear in this memory text
        seen = set()
        for code_label, code_id in code_index.items():
            if code_label in text and code_id not in seen:
                seen.add(code_id)
                graph["links"].append({
                    "relation":        "relates_to",
                    "confidence":      "INFERRED",
                    "source_file":     source_file,
                    "source_location": "",
                    "weight":          0.5,
                    "_src":            node_id,
                    "_tgt":            code_id,
                    "source":          node_id,
                    "target":          code_id,
                    "confidence_score": 0.5,
                })
        total_links += len(seen)

    with open(GRAPH_JSON, "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2)

    print(f"[mempal_bridge] Created {total_links} memory→code links")

    return len(hits)


# ── Run graphify rebuild ──────────────────────────────────────────────────────
def run_graphify() -> None:
    print("[mempal_bridge] Running graphify rebuild...")
    rebuild_cmd = (
        "from graphify.watch import _rebuild_code; "
        "from pathlib import Path; "
        f"_rebuild_code(Path(r'{PROJECT_ROOT}'))"
    )
    # Try py launchers in order — graphify is on 3.14 on this machine.
    # Each launcher is an independent interpreter (unlike the palace mine
    # step, a hang here isn't a shared-lock guarantee), so on timeout we
    # still try the next one rather than giving up immediately.
    result = None
    for py in ("py -3.14", "py -3.11", "python"):
        cmd = py.split() + ["-c", rebuild_cmd]
        result = _run_with_timeout(cmd, timeout=60)
        if result is None:
            print(f"[mempal_bridge] '{py}' timed out after 60s — trying next launcher")
            continue
        if result.returncode == 0:
            break
        if "No module named" not in (result.stderr or ""):
            break  # real error, stop trying

    if result and result.returncode == 0:
        print("[mempal_bridge] Graphify rebuild complete.")
    elif result is None:
        print("[mempal_bridge] Graphify rebuild SKIPPED: all launchers timed out.")
    else:
        stderr = result.stderr[-300:] if result.stderr else ""
        print(f"[mempal_bridge] Graphify rebuild error: {stderr}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Bridge mempalace palace → graphify nodes")
    parser.add_argument("--bridge-only", action="store_true", help="Skip graphify rebuild")
    args = parser.parse_args()

    cfg            = load_config()
    wing           = cfg.get("wing", cfg["project_slug"])
    memory_archive = cfg.get("memory_archive", "")

    print(f"[mempal_bridge] Project: {cfg['project_slug']} | Wing: {wing}")

    palace_path = resolve_palace_path(memory_archive)
    print(f"[mempal_bridge] Palace:  {palace_path}")

    # Mine Claude memory files into ChromaDB before querying
    memory_dir = find_memory_dir(cfg["project_slug"])
    palace_busy = False
    if memory_dir:
        print(f"[mempal_bridge] Memory dir: {memory_dir}")
        palace_busy = mine_memory_into_palace(memory_dir, wing)
    else:
        print(f"[mempal_bridge] WARNING: No Claude memory dir found for '{cfg['project_slug']}' — skipping mine step.")

    if not args.bridge_only:
        run_graphify()

    if palace_busy:
        # Already confirmed locked during the mine step — querying it again
        # would just wait out the same lock. Skip straight to exit.
        print("[mempal_bridge] SKIPPED: palace was busy — skipping wing query too (will retry next run).")
        sys.exit(EXIT_SKIPPED)

    hits = query_palace_by_wing(palace_path, wing)

    if not hits:
        print(f"[mempal_bridge] No documents found for wing '{wing}'.")
        print(f"  Run: mempalace mine <dir> --wing {wing}")
        return

    count = inject_memory_nodes(hits, wing)
    print(f"[mempal_bridge] Injected {count} memory node(s) → graph.json")


if __name__ == "__main__":
    main()
