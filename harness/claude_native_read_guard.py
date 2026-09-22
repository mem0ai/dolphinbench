"""Reject Claude Read calls outside the current test's auto-memory directory."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def permits_read(event: object, memory: Path) -> bool:
    if not isinstance(event, dict) or event.get("tool_name") != "Read":
        return False
    arguments = event.get("tool_input")
    if not isinstance(arguments, dict):
        return False
    raw = arguments.get("file_path")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        root = memory.resolve(strict=True)
        path = Path(raw)
        if not path.is_absolute():
            cwd = event.get("cwd")
            if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                return False
            path = Path(cwd) / path
        resolved = path.resolve(strict=True)
        return root.name == "auto-memory" and resolved.is_relative_to(root) and resolved.is_file()
    except (OSError, RuntimeError, ValueError):
        return False


def main() -> int:
    try:
        allowed = len(sys.argv) == 2 and permits_read(json.load(sys.stdin), Path(sys.argv[1]))
    except (ValueError, OSError):
        allowed = False
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" if allowed else "deny",
        "permissionDecisionReason": "Only this test's frozen auto-memory files may be read.",
    }}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
