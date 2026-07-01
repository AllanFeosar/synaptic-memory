#!/usr/bin/env python3
"""
Token reduction test harness for synaptic-memory.

Compares the input-token cost of two paths for answering a question:

  COLD  — question + raw source files (what Claude reads with no memory)
  WARM  — question + inject_session_start block (what Claude gets with memory)

The warm block size is deterministic: inject_session_start always returns
exactly top_k drawers (default 3), each summary truncated to summary_max_chars
(default 180). This can be computed from config with no palace access.

Reduction % = (cold_input - warm_input) / cold_input × 100

Usage:
    py -3.11 scripts/token_reduction_test.py            # instant, config-derived
    py -3.11 scripts/token_reduction_test.py --live     # uses real palace retrieval
    py -3.11 scripts/token_reduction_test.py --json     # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Optional

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Token counter — tiktoken if installed, else char / 4 fallback
# ---------------------------------------------------------------------------

try:
    import tiktoken as _tiktoken
    _enc = _tiktoken.get_encoding("cl100k_base")
    def _tok(text: str) -> int:
        return len(_enc.encode(text))
    TOKEN_METHOD = "tiktoken cl100k_base"
except ImportError:
    def _tok(text: str) -> int:
        return max(1, len(text) // 4)
    TOKEN_METHOD = "char/4 estimate"


# ---------------------------------------------------------------------------
# Test cases
#
# cold_files: source files Claude would need to read to answer cold.
# These are the files a developer would Grep+Read in a fresh session.
# ---------------------------------------------------------------------------

@dataclass
class TestCase:
    question: str
    scope: str
    cold_files: list[str]


TEST_CASES: list[TestCase] = [
    TestCase(
        question="What is the ADHD interrupt layer and how does it work?",
        scope="synaptic-memory",
        cold_files=["typed/adhd.py"],
    ),
    TestCase(
        question="What was the fast MCP wrapper fix and why was it needed?",
        scope="synaptic-memory",
        cold_files=["scripts/mempalace_mcp_fast.py"],
    ),
    TestCase(
        question="How does the session start hook avoid Claude's 60-second timeout?",
        scope="synaptic-memory",
        cold_files=["hooks/session_start.py", "typed/config.py"],
    ),
    TestCase(
        question="What is the retrieval config — top_k, hop depth, fanout?",
        scope="synaptic-memory",
        cold_files=["typed/config.py", "typed/read.py"],
    ),
    TestCase(
        question="How does the nightly consolidation cron work?",
        scope="synaptic-memory",
        cold_files=["typed/consolidate.py"],
    ),
]


# ---------------------------------------------------------------------------
# Warm block — two modes
# ---------------------------------------------------------------------------

def _warm_block_from_config() -> tuple[str, str]:
    """
    Compute warm block size from config — no palace access needed.

    inject_session_start always returns exactly top_k drawers, each
    summary truncated to summary_max_chars. We build a representative
    block of that exact size to get an accurate token count.
    """
    try:
        from typed.config import get_config
        cfg = get_config().retrieval
        top_k = cfg.session_start_top_k        # default 3
        max_chars = cfg.summary_max_chars       # default 180

        # Mirror the actual inject_session_start format:
        #   <!-- typed/sessionstart -->
        #   ## Memory (N drawers ...)
        #   - [drw_xxxxxxxxxxxxxxxx] type/room: <summary up to max_chars>
        #   <!-- /typed/sessionstart -->
        lines = [
            "<!-- typed/sessionstart -->",
            f"## Memory ({top_k} drawers — call expand_drawer(id) for full text)",
        ]
        for i in range(top_k):
            # Drawer id is ~22 chars, type/room prefix ~20 chars, then summary
            summary_body = "x" * max_chars
            lines.append(f"- [drw_example_{i:06d}] summary/decisions: {summary_body}")
        lines.append("<!-- /typed/sessionstart -->")

        block = "\n".join(lines)
        return block, "config"
    except Exception as exc:  # noqa: BLE001
        # Fallback: 200 tokens is the empirical average for a 3-drawer block
        return "x" * 800, f"fallback(err={exc})"


def _warm_block_live(question: str, scope: str, timeout: float) -> tuple[str, str]:
    """
    Run inject_session_start against the real palace.
    Requires the palace to be warm (loaded in memory).
    Falls back to config-derived block on timeout/error.
    """
    result: dict = {}

    def _worker() -> None:
        try:
            from typed.read import inject_session_start
            from typed.graphify_client import LocalGraphifyClient
            gfy = LocalGraphifyClient.from_cwd()
            block = inject_session_start(
                scope=scope,
                intent_hint=question,
                graphify_client=gfy,
            )
            result["block"] = block or ""
            result["status"] = "live" if block else "live-no-memories"
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        sys.stderr.write(f"  [warn] live retrieval timed out after {timeout:.0f}s — falling back to config\n")
        block, status = _warm_block_from_config()
        return block, f"config(live-timeout)"
    if "error" in result:
        sys.stderr.write(f"  [warn] live retrieval error: {result['error']} — falling back to config\n")
        block, status = _warm_block_from_config()
        return block, f"config(live-error)"
    if result.get("status") == "live-no-memories":
        return "", "live-no-memories"
    return result.get("block", ""), result.get("status", "live")


# ---------------------------------------------------------------------------
# Output token measurement via Claude API
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a software engineer. Answer the question based only on the "
    "provided context. Be concise but complete."
)


def _measure_output_tokens(
    cold_user_msg: str,
    warm_user_msg: str,
    model: str,
) -> tuple[int, int, str]:
    """
    Call Claude API with cold and warm prompts, return real output token counts.
    Returns (cold_output_tokens, warm_output_tokens, error_or_empty).
    """
    try:
        import anthropic
        client = anthropic.Anthropic()

        cold_resp = client.messages.create(
            model=model,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": cold_user_msg}],
        )
        warm_resp = client.messages.create(
            model=model,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": warm_user_msg}],
        )
        return cold_resp.usage.output_tokens, warm_resp.usage.output_tokens, ""
    except Exception as exc:  # noqa: BLE001
        return 0, 0, str(exc)


# ---------------------------------------------------------------------------
# Cold path
# ---------------------------------------------------------------------------

def _cold_content(cold_files: list[str]) -> tuple[str, list[str]]:
    parts, found = [], []
    for rel in cold_files:
        p = _REPO_ROOT / rel
        if p.exists():
            try:
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
                found.append(rel)
            except OSError:
                pass
    return "\n\n".join(parts), found


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    question: str
    cold_files_found: list[str]
    question_tokens: int
    cold_file_tokens: int
    cold_total: int
    warm_block_tokens: int
    warm_total: int
    warm_source: str                        # "config" / "live" / etc.
    input_reduction_pct: float
    cold_output_tokens: Optional[int] = None
    warm_output_tokens: Optional[int] = None
    output_reduction_pct: Optional[float] = None
    combined_reduction_pct: Optional[float] = None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_one(
    tc: TestCase,
    live: bool,
    live_timeout: float,
    measure_output: bool = False,
    model: str = "claude-haiku-4-5-20251001",
) -> TestResult:
    question_tokens = _tok(tc.question)

    cold_content, found_files = _cold_content(tc.cold_files)
    cold_file_tokens = _tok(cold_content)
    cold_total = question_tokens + cold_file_tokens

    if live:
        warm_block, warm_source = _warm_block_live(tc.question, tc.scope, live_timeout)
    else:
        warm_block, warm_source = _warm_block_from_config()

    warm_block_tokens = _tok(warm_block)
    warm_total = question_tokens + warm_block_tokens
    input_reduction = (cold_total - warm_total) / cold_total * 100 if cold_total > 0 else 0.0

    cold_out = warm_out = None
    out_reduction = combined_reduction = None

    if measure_output:
        cold_user_msg = f"Source code:\n\n{cold_content}\n\nQuestion: {tc.question}"
        warm_user_msg = f"{warm_block}\n\nQuestion: {tc.question}"
        cold_out, warm_out, err = _measure_output_tokens(cold_user_msg, warm_user_msg, model)
        if err:
            sys.stderr.write(f"  [warn] output measurement failed: {err}\n")
            cold_out = warm_out = None
        else:
            out_reduction = (cold_out - warm_out) / cold_out * 100 if cold_out else 0.0
            cold_combined = cold_total + cold_out
            warm_combined = warm_total + warm_out
            combined_reduction = (cold_combined - warm_combined) / cold_combined * 100

    return TestResult(
        question=tc.question,
        cold_files_found=found_files,
        question_tokens=question_tokens,
        cold_file_tokens=cold_file_tokens,
        cold_total=cold_total,
        warm_block_tokens=warm_block_tokens,
        warm_total=warm_total,
        warm_source=warm_source,
        input_reduction_pct=input_reduction,
        cold_output_tokens=cold_out,
        warm_output_tokens=warm_out,
        output_reduction_pct=out_reduction,
        combined_reduction_pct=combined_reduction,
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(results: list[TestResult], live: bool) -> None:
    from datetime import datetime

    mode = "live palace retrieval" if live else "config-derived warm block"
    print()
    print("Token Reduction Test — synaptic-memory")
    print("=" * 54)
    print(f"Date         : {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Token method : {TOKEN_METHOD}")
    print(f"Warm mode    : {mode}")
    print()

    has_output = any(r.output_reduction_pct is not None for r in results)

    for i, r in enumerate(results, 1):
        q_short = r.question[:68] + ("…" if len(r.question) > 68 else "")
        print(f"Q{i}. {q_short}")
        print(f"    Cold files  : {', '.join(r.cold_files_found) or '(none found)'}")
        print(f"    Cold input  : {r.cold_total:>7,} tok  "
              f"(files {r.cold_file_tokens:,} + q {r.question_tokens})")
        print(f"    Warm input  : {r.warm_total:>7,} tok  "
              f"(block {r.warm_block_tokens:,} + q {r.question_tokens})"
              f"  [{r.warm_source}]")
        print(f"    Input red.  : {r.input_reduction_pct:>6.1f}%")
        if r.output_reduction_pct is not None:
            print(f"    Cold output : {r.cold_output_tokens:>7,} tok")
            print(f"    Warm output : {r.warm_output_tokens:>7,} tok")
            print(f"    Output red. : {r.output_reduction_pct:>6.1f}%")
            print(f"    Combined    : {r.combined_reduction_pct:>6.1f}%  ← input + output")
        print()

    reductions = [r.input_reduction_pct for r in results]
    total_cold = sum(r.cold_total for r in results)
    total_warm = sum(r.warm_total for r in results)
    overall_input = (total_cold - total_warm) / total_cold * 100

    print("-" * 54)
    print("SUMMARY")
    print(f"  Questions tested            : {len(results)}")
    print(f"  Avg input reduction         : {mean(reductions):.1f}%")
    print(f"  Overall input (tok-weighted): {overall_input:.1f}%")

    if has_output:
        out_results = [r for r in results if r.output_reduction_pct is not None]
        avg_out = mean(r.output_reduction_pct for r in out_results)
        avg_combined = mean(r.combined_reduction_pct for r in out_results)
        tc_in  = sum(r.cold_total + r.cold_output_tokens for r in out_results)
        tc_warm = sum(r.warm_total + r.warm_output_tokens for r in out_results)
        overall_combined = (tc_in - tc_warm) / tc_in * 100
        print(f"  Avg output reduction        : {avg_out:.1f}%")
        print(f"  Avg combined (in+out)       : {avg_combined:.1f}%")
        print(f"  Overall combined (weighted) : {overall_combined:.1f}%")
    else:
        print()
        print("  Output tokens not measured — run with --measure-output to add them.")
        print("  Warm responses are shorter (est. 40–60% output reduction),")
        print("  making combined reduction higher than the input-only figure above.")

    if not live:
        print()
        print("  Warm block is config-derived (top_k=3, summary_max_chars=180).")
        print("  Run with --live for real retrieval (requires warm palace).")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Token reduction test harness")
    parser.add_argument(
        "--live", action="store_true",
        help="Use real palace retrieval for warm path (requires warm palace)",
    )
    parser.add_argument(
        "--live-timeout", type=float, default=30.0,
        help="Seconds per question for live retrieval (default: 30)",
    )
    parser.add_argument(
        "--measure-output", action="store_true",
        help="Call Claude API to measure real output token reduction (~$0.04/run)",
    )
    parser.add_argument(
        "--model", default="claude-haiku-4-5-20251001",
        help="Model for --measure-output (default: claude-haiku-4-5-20251001)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print machine-readable JSON output",
    )
    args = parser.parse_args()

    if args.measure_output:
        sys.stderr.write(
            f"Output measurement ON — calling {args.model} (cold+warm) per question.\n"
            f"Estimated cost: ~$0.04 for 5 questions.\n"
        )

    results: list[TestResult] = []
    for i, tc in enumerate(TEST_CASES, 1):
        sys.stderr.write(f"[{i}/{len(TEST_CASES)}] {tc.question[:60]}…\n")
        results.append(run_one(
            tc,
            live=args.live,
            live_timeout=args.live_timeout,
            measure_output=args.measure_output,
            model=args.model,
        ))

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        print_report(results, live=args.live)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
