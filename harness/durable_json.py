"""Atomic, crash-durable JSON records for bounded execution and accounting."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_json(path: Path, value: Any) -> None:
    data = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    _atomic_write(path, data)


def save_json(path: Path, value: Any) -> None:
    """Keep the participant controller's compact, ASCII-escaped record format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(value, allow_nan=False))


def _atomic_write(path: Path, data: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
