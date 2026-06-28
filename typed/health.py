"""Health check for the synaptic-memory stack. Run: py -3.11 -m typed.health"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REPORT_PATH = Path.home() / ".synaptic-memory" / "consolidate-report.json"
AUDIT_LOG = Path.home() / ".synaptic-memory" / "retrieval-audit.jsonl"
BUDGET_LOG = Path.home() / ".synaptic-memory" / "budget.jsonl"


def check() -> dict:
    """Run all health probes and return a structured report."""
    results: dict = {"ts": datetime.now(timezone.utc).isoformat(), "checks": []}
    checks = results["checks"]

    # 1. Palace accessible
    try:
        from typed.client import InProcessClient
        client = InProcessClient.get_or_create()
        status = client.status()
        checks.append({
            "name": "palace",
            "ok": status.get("drawer_count", 0) > 0,
            "detail": f"{status.get('drawer_count', 0)} drawers at {status.get('palace_path', '?')}",
        })
    except Exception as exc:
        checks.append({"name": "palace", "ok": False, "detail": str(exc)})

    # 2. Config loads
    try:
        from typed.config import get_config
        cfg = get_config()
        checks.append({
            "name": "config",
            "ok": True,
            "detail": f"adhd.enabled={cfg.adhd.enabled}, level={cfg.adhd.level}",
        })
    except Exception as exc:
        checks.append({"name": "config", "ok": False, "detail": str(exc)})

    # 3. Consolidation recency
    if REPORT_PATH.exists():
        try:
            report = json.loads(REPORT_PATH.read_text())
            finished = report.get("finished_at")
            partial = report.get("partial", False)
            if finished:
                age_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(finished)).total_seconds() / 3600
                ok = age_hours < 48
                checks.append({
                    "name": "consolidation",
                    "ok": ok and not partial,
                    "detail": f"last run {age_hours:.0f}h ago" + (" (PARTIAL)" if partial else ""),
                })
            else:
                checks.append({"name": "consolidation", "ok": False, "detail": "no finished_at in report"})
        except Exception as exc:
            checks.append({"name": "consolidation", "ok": False, "detail": str(exc)})
    else:
        checks.append({"name": "consolidation", "ok": False, "detail": "no report file yet"})

    # 4. Retrieval audit health
    if AUDIT_LOG.exists():
        try:
            size_kb = AUDIT_LOG.stat().st_size / 1024
            line_count = sum(1 for _ in open(AUDIT_LOG, encoding="utf-8", errors="replace"))
            zero_results = 0
            tail_lines = []
            with open(AUDIT_LOG, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    tail_lines.append(line)
                    if len(tail_lines) > 100:
                        tail_lines.pop(0)
            for line in tail_lines:
                try:
                    rec = json.loads(line)
                    if rec.get("result_count", 1) == 0:
                        zero_results += 1
                except (ValueError, TypeError):
                    pass
            zero_pct = (zero_results / len(tail_lines) * 100) if tail_lines else 0
            checks.append({
                "name": "retrieval_audit",
                "ok": zero_pct < 20,
                "detail": f"{line_count} records ({size_kb:.0f} KB), {zero_pct:.0f}% zero-result in last 100",
            })
        except Exception as exc:
            checks.append({"name": "retrieval_audit", "ok": False, "detail": str(exc)})
    else:
        checks.append({"name": "retrieval_audit", "ok": True, "detail": "no audit log yet (will be created on first session)"})

    # 5. Hook scripts present
    from pathlib import Path as _P
    hooks_dir = _P(__file__).resolve().parent.parent / "hooks"
    hook_files = sorted([
        f.name for f in hooks_dir.glob("*.py")
        if f.name not in ("__init__.py", "_common.py") and not f.name.startswith("_")
    ]) if hooks_dir.exists() else []
    checks.append({
        "name": "hooks",
        "ok": len(hook_files) > 0,
        "detail": f"{len(hook_files)} hook scripts found" + (f": {', '.join(hook_files)}" if hook_files else ""),
    })

    # Summary
    all_ok = all(c["ok"] for c in checks)
    results["overall"] = "HEALTHY" if all_ok else "DEGRADED"
    return results


def main():
    result = check()
    print(f"=== synaptic-memory health: {result['overall']} ===")
    print()
    for c in result["checks"]:
        status = "[OK]  " if c["ok"] else "[WARN]"
        print(f"  {status} {c['name']}: {c['detail']}")
    print()


if __name__ == "__main__":
    main()
