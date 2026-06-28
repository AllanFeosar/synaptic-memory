"""Shared utilities for Claude Code hooks."""

import os
from pathlib import Path


def detect_scope() -> str:
    """Detect the current project scope.

    Priority: SYNAPTIC_V2_SCOPE > CLAUDE_PROJECT_SLUG > cwd folder name.
    """
    for env_var in ("SYNAPTIC_V2_SCOPE", "CLAUDE_PROJECT_SLUG"):
        val = os.environ.get(env_var)
        if val:
            return val.strip()
    return Path.cwd().name
