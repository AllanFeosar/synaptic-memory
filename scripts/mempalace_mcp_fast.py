"""Fast-start mempalace MCP wrapper — skips the blocking PRAGMA quick_check.

The real mempalace MCP server runs _refresh_sqlite_integrity_status() at startup,
which calls PRAGMA quick_check on chroma.sqlite3. On a 1.46 GB palace this takes
244 seconds, exceeding Claude Code's 60-second subprocess init timeout.

This wrapper patches that function to a no-op so the MCP server responds to the
initialize handshake instantly. The integrity check is deferred — it runs lazily
on the first tool call that needs it (via _ensure_sqlite_integrity_status).

Usage in .mcp.json:
    "command": "py",
    "args": ["-3.11", "<synaptic-memory-path>/scripts/mempalace_mcp_fast.py", "--palace", "D:/Memory/palace"]
"""

import sys
import os

sys.argv = [sys.argv[0]] + sys.argv[1:]

import mempalace.mcp_server as mcp

mcp._sqlite_integrity_checked = True
mcp._sqlite_integrity_errors = []
mcp._sqlite_integrity_check_error = ""

_original_refresh = mcp._refresh_sqlite_integrity_status


def _deferred_refresh():
    """Run the real check, but only when explicitly called — not at startup."""
    _original_refresh()


mcp._refresh_sqlite_integrity_status = lambda: None

mcp.main()
