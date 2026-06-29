"""Fast-start mempalace MCP wrapper — instant MCP handshake, lazy heavy imports.

Problem: mempalace 3.5.0 imports chromadb, onnxruntime, and other heavy
dependencies at module level, then runs _refresh_sqlite_integrity_status()
and _refresh_vector_disabled_flag() before entering the MCP loop. On the
2.3 GB palace hosted on a spinning HDD (D:), cold-start import + startup
routinely exceeds Claude Code's 60-second subprocess init timeout.

Solution: this wrapper handles the MCP initialize/ping handshake using only
Python stdlib (instant), then imports mempalace in a background thread.
Once the real server is ready, all subsequent tool calls are forwarded to it.

Critical detail: mempalace.mcp_server redirects fd 1 → stderr at import
time (to protect the JSON-RPC stream from C-level print noise). This wrapper
saves a private fd for the real stdout BEFORE the background import starts,
so the main loop's writes always reach the MCP client regardless of what
the import does to fd 1.

Usage in .mcp.json:
    "command": "C:/Users/allge/.../python.exe",
    "args": ["<path>/mempalace_mcp_fast.py", "--palace", "D:/Memory/palace"]
"""

import json
import os
import sys
import threading
import time

_PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
_SERVER_VERSION = None

_real_handler = None
_real_handler_lock = threading.Lock()
_import_error = None
_import_done = threading.Event()

# Save a private copy of stdout fd BEFORE the background import can touch
# fd 1. The main loop writes exclusively through _stdout_fd / _stdout_writer
# so mempalace's import-time os.dup2(2, 1) cannot corrupt our output.
_stdout_fd = os.dup(sys.stdout.fileno())
_stdout_writer = os.fdopen(_stdout_fd, "w", encoding="utf-8", closefd=False)


def _load_real_server():
    """Import mempalace.mcp_server in a background thread."""
    global _real_handler, _import_error, _SERVER_VERSION

    try:
        import mempalace.mcp_server as mcp

        mcp._sqlite_integrity_checked = True
        mcp._sqlite_integrity_errors = []
        mcp._sqlite_integrity_check_error = ""
        mcp._refresh_sqlite_integrity_status = lambda: None
        mcp._refresh_vector_disabled_flag = lambda: None

        _SERVER_VERSION = mcp.__version__

        # Restore mempalace's internal stdout so its handle_request writes
        # go to the right place. After this, fd 1 is the real stdout again.
        mcp._restore_stdout()

        for stream in (sys.stdin, sys.stdout):
            if hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except (AttributeError, OSError):
                    pass

        mcp._start_idle_exit_watchdog()

        with _real_handler_lock:
            _real_handler = mcp.handle_request

    except Exception as exc:
        _import_error = exc

    finally:
        _import_done.set()


def _handle_early(request):
    """Handle MCP requests before the real server is loaded."""
    if not isinstance(request, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}

    method = request.get("method") or ""
    params = request.get("params") or {}
    req_id = request.get("id")

    if method == "initialize":
        client_ver = params.get("protocolVersion", _PROTOCOL_VERSIONS[-1])
        negotiated = client_ver if client_ver in _PROTOCOL_VERSIONS else _PROTOCOL_VERSIONS[0]
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mempalace", "version": _SERVER_VERSION or "3.5.0"},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method.startswith("notifications/"):
        return None

    # tools/list and tools/call need the real handler — block until ready
    if not _import_done.wait(timeout=120):
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": "Server still loading"}}
    if _import_error:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(_import_error)}}
    with _real_handler_lock:
        if _real_handler:
            return _real_handler(request)
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": "Server not ready"}}


def _write_response(response):
    """Write a JSON-RPC response through our private stdout fd."""
    _stdout_writer.write(json.dumps(response, ensure_ascii=False) + "\n")
    _stdout_writer.flush()


def main():
    os.environ.pop("PYTHONPATH", None)

    if hasattr(sys.stdin, "reconfigure"):
        try:
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    loader = threading.Thread(target=_load_real_server, name="mempalace-loader", daemon=True)
    loader.start()

    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break

            line = line.strip()
            if not line:
                continue

            request = json.loads(line)

            with _real_handler_lock:
                handler = _real_handler

            if handler:
                response = handler(request)
            else:
                response = _handle_early(request)

            if response is not None:
                _write_response(response)

        except KeyboardInterrupt:
            break
        except Exception:
            pass


if __name__ == "__main__":
    main()
