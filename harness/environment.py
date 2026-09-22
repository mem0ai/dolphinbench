"""DolphinBench settings with read compatibility for older local configurations."""

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

_Default = TypeVar("_Default")


def load_environment(path: Path) -> dict[str, str]:
    """Fill missing environment values from a file of literal KEY=VALUE lines."""
    values = dict(os.environ)
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values.setdefault(key.strip(), value.strip())
    return values


def get_setting(
    name: str,
    default: _Default | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | _Default | None:
    """Prefer the canonical key, including an explicitly empty value."""
    values = os.environ if environ is None else environ
    if name in values:
        return values[name]
    if name.startswith("DOLPHINBENCH_"):
        legacy = "ENACT_" + name.removeprefix("DOLPHINBENCH_")
        if legacy in values:
            return values[legacy]
    return default


def openai_config_path() -> Path:
    configured = get_setting("DOLPHINBENCH_OPENAI_CONFIG")
    if configured:
        return Path(configured).expanduser()
    canonical = Path.home() / ".dolphinbench" / "config.yaml"
    legacy = Path.home() / ".enact" / "config.yaml"
    return canonical if canonical.exists() or not legacy.exists() else legacy


@contextmanager
def call_ledger(path: Path) -> Iterator[None]:
    previous_ledger = os.environ.get("DOLPHINBENCH_CALL_LEDGER")
    previous_run_id = os.environ.get("DOLPHINBENCH_CONSTRUCTION_RUN_ID")
    os.environ["DOLPHINBENCH_CALL_LEDGER"] = str(path)
    os.environ["DOLPHINBENCH_CONSTRUCTION_RUN_ID"] = path.parent.name
    try:
        yield
    finally:
        if previous_ledger is None:
            os.environ.pop("DOLPHINBENCH_CALL_LEDGER", None)
        else:
            os.environ["DOLPHINBENCH_CALL_LEDGER"] = previous_ledger
        if previous_run_id is None:
            os.environ.pop("DOLPHINBENCH_CONSTRUCTION_RUN_ID", None)
        else:
            os.environ["DOLPHINBENCH_CONSTRUCTION_RUN_ID"] = previous_run_id
