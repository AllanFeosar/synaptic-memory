import sys
import json

d = json.load(sys.stdin)
p = d.get("tool_input", {}).get("file_path", "").replace("\\", "/")

if ".claude/memory" in p:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": "BLOCKED: use mcp__mempalace__mempalace_add_drawer, not .claude/memory/"
        }
    }))
    sys.exit(2)
