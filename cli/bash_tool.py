"""! bash tool: run a shell command, return capped output as a tool result.

Mirrors opencode docs: a leading `!` runs the shell and the output is added
to the conversation as a tool result. Runs as the invoking user with a
timeout and output cap; never raises.
"""

from __future__ import annotations

import os
import subprocess

TIMEOUT_S = 30
CAP_CHARS = 8000


def project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_bash(cmd: str, timeout: int = TIMEOUT_S, cap: int = CAP_CHARS) -> dict:
    """Run cmd in the project root. Always returns a dict, never raises."""
    cmd = (cmd or "").strip()
    if not cmd:
        return {"exit": 2, "output": "", "truncated": False, "note": "empty command"}
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=project_root(),
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        truncated = len(out) > cap
        return {
            "exit": proc.returncode,
            "output": out[:cap],
            "truncated": truncated,
            "note": f" (truncated at {cap} chars)" if truncated else "",
        }
    except subprocess.TimeoutExpired:
        return {
            "exit": 124,
            "output": "",
            "truncated": False,
            "note": f"timed out after {timeout}s",
        }
    except Exception as e:
        return {"exit": 127, "output": "", "truncated": False, "note": f"failed: {e}"}
