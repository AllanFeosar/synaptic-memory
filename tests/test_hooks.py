"""
Tests for Claude Code hooks in hooks/ directory.

Run:
    cd synaptic-memory
    python -m pytest tests/test_hooks.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

# Make hooks importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hooks._common import detect_scope


# ---------------------------------------------------------------------------
# _common.detect_scope()
# ---------------------------------------------------------------------------


class TestDetectScope:
    """Tests for hooks._common.detect_scope()."""

    def test_synaptic_v2_scope_takes_priority(self):
        env = {"SYNAPTIC_V2_SCOPE": "my-scope", "CLAUDE_PROJECT_SLUG": "slug-val"}
        with mock.patch.dict(os.environ, env, clear=True):
            assert detect_scope() == "my-scope"

    def test_claude_project_slug_fallback(self):
        env = {"CLAUDE_PROJECT_SLUG": "slug-val"}
        with mock.patch.dict(os.environ, env, clear=True):
            assert detect_scope() == "slug-val"

    def test_cwd_name_when_no_env_vars(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("hooks._common.Path") as MockPath:
                MockPath.cwd.return_value = Path("/fake/project-dir")
                assert detect_scope() == "project-dir"

    def test_strips_whitespace(self):
        env = {"SYNAPTIC_V2_SCOPE": "  padded  "}
        with mock.patch.dict(os.environ, env, clear=True):
            assert detect_scope() == "padded"

    def test_empty_synaptic_falls_through(self):
        env = {"SYNAPTIC_V2_SCOPE": "", "CLAUDE_PROJECT_SLUG": "fallback"}
        with mock.patch.dict(os.environ, env, clear=True):
            assert detect_scope() == "fallback"


# ---------------------------------------------------------------------------
# pre_tool_write.py  (invoked as a subprocess — it's a script, not a module)
# ---------------------------------------------------------------------------

HOOK_DIR = Path(__file__).resolve().parents[1] / "hooks"
PRE_TOOL_WRITE = HOOK_DIR / "pre_tool_write.py"


class TestPreToolWrite:
    """Tests for hooks/pre_tool_write.py."""

    def _run(self, stdin_data: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(PRE_TOOL_WRITE)],
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_blocks_write_to_claude_memory(self):
        payload = json.dumps({
            "tool_input": {"file_path": "C:/proj/.claude/memory/foo.md"}
        })
        r = self._run(payload)
        assert r.returncode == 2
        out = json.loads(r.stdout)
        assert "BLOCKED" in out["hookSpecificOutput"]["additionalContext"]

    def test_blocks_backslash_memory_path(self):
        payload = json.dumps({
            "tool_input": {"file_path": "C:\\proj\\.claude\\memory\\bar.md"}
        })
        r = self._run(payload)
        assert r.returncode == 2

    def test_allows_other_paths(self):
        payload = json.dumps({
            "tool_input": {"file_path": "/src/main.py"}
        })
        r = self._run(payload)
        assert r.returncode == 0

    def test_malformed_json_exits_zero(self):
        r = self._run("not json at all")
        assert r.returncode == 0

    def test_non_dict_tool_input_exits_zero(self):
        payload = json.dumps({"tool_input": "a string, not a dict"})
        r = self._run(payload)
        assert r.returncode == 0

    def test_missing_tool_input_exits_zero(self):
        payload = json.dumps({"something_else": 123})
        r = self._run(payload)
        assert r.returncode == 0


# ---------------------------------------------------------------------------
# session_start_mempalace.py  (tested via mocked subprocess)
# ---------------------------------------------------------------------------

SESSION_START = HOOK_DIR / "session_start_mempalace.py"


class TestSessionStartMempalace:
    """Tests for hooks/session_start_mempalace.py — mocks subprocess.run."""

    @mock.patch("subprocess.run")
    def test_stdout_forwarded(self, mock_run, capsys):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="hello from mempalace\n", stderr=""
        )
        # exec the script in this process so we can capture stdout
        with mock.patch("sys.stdin"):
            exec(compile(PRE_TOOL_WRITE.parent.joinpath("session_start_mempalace.py").read_text(),
                         str(SESSION_START), "exec"),
                 {"__name__": "__main__"})
        captured = capsys.readouterr()
        assert "hello from mempalace" in captured.out

    @mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=30))
    def test_timeout_graceful(self, mock_run, capsys):
        exec(compile(SESSION_START.read_text(), str(SESSION_START), "exec"),
             {"__name__": "__main__"})
        captured = capsys.readouterr()
        assert "timed out" in captured.err

    @mock.patch("subprocess.run", side_effect=FileNotFoundError("py not found"))
    def test_generic_error_graceful(self, mock_run, capsys):
        exec(compile(SESSION_START.read_text(), str(SESSION_START), "exec"),
             {"__name__": "__main__"})
        captured = capsys.readouterr()
        assert "error" in captured.err
