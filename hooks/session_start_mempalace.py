import subprocess
import sys

try:
    r = subprocess.run(
        [sys.executable, "-m", "mempalace", "hook", "run",
         "--hook", "session-start", "--harness", "claude-code"],
        timeout=30,
        capture_output=True,
        text=True,
        check=False,
    )
    sys.stdout.write(r.stdout or "")
except subprocess.TimeoutExpired:
    sys.stderr.write("[session_start_mempalace] timed out after 30s — skipping\n")
except Exception as e:
    sys.stderr.write(f"[session_start_mempalace] error: {e}\n")