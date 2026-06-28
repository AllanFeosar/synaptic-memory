import sys
import json

try:
    d = json.load(sys.stdin)
except (json.JSONDecodeError, OSError):
    sys.exit(0)

tool_input = d.get("tool_input")
if not isinstance(tool_input, dict):
    sys.exit(0)
p = tool_input.get("file_path", "").replace("\\", "/")

if ".claude/memory" in p:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": "BLOCKED: use mcp__mempalace__mempalace_add_drawer, not .claude/memory/"
        }
    }))
    sys.exit(2)
