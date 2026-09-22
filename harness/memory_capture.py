"""Capture Hermes profile files and compare before/after snapshots.

Capture failures are recorded without aborting the benchmark.
"""

from __future__ import annotations

import difflib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HERMES_PROFILES_ROOT = Path.home() / ".hermes" / "profiles"

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_text(p: Path) -> str | None:
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return f"<read error: {exc}>"


def _read_honcho(p: Path) -> Any:
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return f"<read error: {exc}>"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def snapshot_hermes(profile: str) -> dict:
    """Snapshot a hermes profile's memory substrate.

    Reads ``MEMORY.md``, ``USER.md`` from ``<profile>/memories/`` and
    ``honcho.json`` from the profile root if present. Always returns a
    dict — missing files surface as ``None`` values, not errors.
    """
    out: dict[str, Any] = {
        "snapshot_at": _now_iso(),
        "profile": profile,
        "MEMORY.md": None,
        "USER.md": None,
        "honcho.json": None,
    }
    if not profile:
        out["error"] = "no_profile"
        return out
    root = HERMES_PROFILES_ROOT / profile
    if not root.exists():
        out["error"] = f"profile_dir_missing: {root}"
        return out
    mem_dir = root / "memories"
    out["MEMORY.md"] = _read_text(mem_dir / "MEMORY.md")
    out["USER.md"] = _read_text(mem_dir / "USER.md")
    honcho_path = root / "honcho.json"
    if honcho_path.exists():
        out["honcho.json"] = _read_honcho(honcho_path)
    return out


def snapshot_for_provider(provider: str, profile: str | None) -> dict:
    """Dispatch to the right snapshot function for ``provider``."""
    if provider in ("builtin", "honcho", "mem0"):
        return snapshot_hermes(profile or "")
    return {
        "snapshot_at": _now_iso(),
        "error": f"unknown_provider: {provider}",
    }


# ── diffing ──────────────────────────────────────────────────────────


def _unified_diff(before: str | None, after: str | None, fname: str) -> dict:
    b = before or ""
    a = after or ""
    if b == a:
        return {"file": fname, "changed": False}
    diff = list(difflib.unified_diff(
        b.splitlines(),
        a.splitlines(),
        fromfile=f"before/{fname}",
        tofile=f"after/{fname}",
        lineterm="",
        n=2,
    ))
    added = sum(1 for line in diff
                if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff
                  if line.startswith("-") and not line.startswith("---"))
    # Keep the diff readable; cap at ~400 lines to bound JSON size.
    if len(diff) > 400:
        diff = diff[:400] + [f"... ({len(diff)-400} more diff lines truncated)"]
    return {
        "file": fname,
        "changed": True,
        "lines_added": added,
        "lines_removed": removed,
        "before_chars": len(b),
        "after_chars": len(a),
        "diff": "\n".join(diff),
    }


def _diff_hermes(before: dict, after: dict) -> dict:
    files = {}
    for fname in ("MEMORY.md", "USER.md"):
        files[fname] = _unified_diff(before.get(fname), after.get(fname), fname)

    honcho_diff: dict[str, Any] | None = None
    b_h, a_h = before.get("honcho.json"), after.get("honcho.json")
    if b_h is not None or a_h is not None:
        # Normalize to JSON text for a stable diff.
        def _to_text(v: Any) -> str:
            if v is None:
                return ""
            if isinstance(v, str):
                return v
            try:
                return json.dumps(v, indent=2, sort_keys=True, default=str)
            except Exception:
                return str(v)
        honcho_diff = _unified_diff(_to_text(b_h), _to_text(a_h), "honcho.json")
    return {
        "provider": "hermes",
        "files": files,
        "honcho.json": honcho_diff,
        "any_changes": (
            any(f.get("changed") for f in files.values())
            or (honcho_diff is not None and honcho_diff.get("changed"))
        ),
    }


def diff_memory_dumps(before: dict, after: dict, provider: str) -> dict:
    """Compute a delta between two snapshots taken from the same provider.

    Bounded in size (top-N entries / capped diff lines) so it can be
    inlined into the per-test results JSON without bloating it.
    """
    if provider in ("builtin", "honcho", "mem0"):
        return _diff_hermes(before, after)
    return {"provider": provider, "error": "unknown_provider"}
