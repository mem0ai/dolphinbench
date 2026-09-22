"""Public interaction boundary; grading specifications never cross it."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Literal, Protocol


@dataclass(frozen=True)
class Interaction:
    persona: str
    phase: Literal["ingestion", "tests"]
    interaction_id: str
    message: str
    narrative_time: str
    apps: dict
    work_dir: Path
    fresh_session: bool = True

    @property
    def dated_message(self) -> str:
        return f"[{self.narrative_time}] {self.message}"


@dataclass
class InteractionRecord:
    settings: dict
    messages: list[dict]
    duration_ms: float | None = None
    attempts: list[dict] = field(default_factory=list)
    app_calls: list[dict] | None = None


class Adapter(Protocol):
    def identity(self) -> dict:
        """Pin runtime dependencies and configuration; omit credentials."""
        ...

    def run_interaction(self, request: Interaction) -> InteractionRecord: ...

    def freeze(self, persona: str) -> dict:
        """Return a JSON checkpoint identity after all writes have completed."""
        ...

    def verify_checkpoint(self, persona: str, checkpoint: dict) -> None: ...

    def total_cost_usd(self, phase: Literal["ingestion", "tests"]) -> float:
        """Return completed phase cost for all personas, including agent and memory processing.

        Include retries and background work; exclude grading and infrastructure.
        Read durable accounting records so collection can resume without agent calls.
        """
        ...


def load_adapter(target: str, options: dict, work_dir: Path) -> Adapter:
    module, separator, name = target.partition(":")
    if not separator or not module or not name:
        raise ValueError("adapter must be a module:factory import path")
    factory = getattr(import_module(module), name)
    return factory(options, work_dir)
