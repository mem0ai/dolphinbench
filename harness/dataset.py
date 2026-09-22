"""Load test definitions with optional shared initial app state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

import yaml


def spec_sha256(spec: dict) -> str:
    """Fingerprint parsed inputs independently of YAML layout and storage."""
    raw = json.dumps(spec, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def load_test(path: Path, *, read_bytes: Callable[[Path], bytes] = Path.read_bytes) -> dict:
    spec = yaml.load(read_bytes(path), Loader=yaml.CSafeLoader)
    if not isinstance(spec, dict):
        raise ValueError(f"Expected a test mapping: {path}")
    if "mock_state_base" not in spec:
        return spec
    reference = spec.pop("mock_state_base")
    if (not isinstance(reference, dict) or set(reference) != {"path", "sha256"}
            or reference["path"] != "state.json"):
        raise ValueError(f"Invalid shared app state reference: {path}")
    base_path = (path.parent / reference["path"]).resolve()
    base_path.relative_to(path.parent.resolve())
    raw = read_bytes(base_path)
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError(f"Shared app state hash mismatch: {base_path}")
    state = json.loads(raw)
    overrides = spec.get("mock_state", {})
    if not isinstance(state, dict) or not isinstance(overrides, dict):
        raise ValueError(f"Expected app state mappings: {path}")
    # Replace entire top-level collections; never merge records or append lists.
    state.update(overrides)
    spec["mock_state"] = state
    return spec
