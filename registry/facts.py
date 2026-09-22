"""Load the canonical DolphinBench fact registry.

``life_sim.yaml`` is authoritative for what the user said. ``facts.yaml``
only identifies which complete history sessions establish each remembered
fact. Tests contain numeric fact ids and never duplicate fact prose or session
ids.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from construction.checkpoints import validated_checkpoint_identity


ROOT = Path(__file__).resolve().parents[1]


class FactRegistryError(ValueError):
    pass


CHECKPOINT_ENV = "DOLPHINBENCH_CHECKPOINT"


def _checkpoint_path(checkpoint: str | Path | None) -> Path | None:
    """Resolve an explicitly supplied checkpoint path."""
    if checkpoint is None or not str(checkpoint).strip():
        return None
    return Path(checkpoint).expanduser().resolve()


def _session_id_list(value: Any, *, path: Path, fact_id: int, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise FactRegistryError(f"{path}: fact {fact_id} has invalid {field}")
    if not all(
        (isinstance(item, str) and item)
        or (isinstance(item, int) and not isinstance(item, bool))
        for item in value
    ):
        raise FactRegistryError(f"{path}: fact {fact_id} has invalid {field}")
    normalized = [str(item) for item in value]
    if len(normalized) != len(set(normalized)):
        raise FactRegistryError(f"{path}: fact {fact_id} repeats a session id")
    return normalized


def _parse_fact_registry(
    payload: Any,
    *,
    persona: str,
    path: Path,
    allowed_versions: set[int],
    normalize_session_ids: bool,
) -> dict[int, dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("version") not in allowed_versions:
        raise FactRegistryError(f"{path} has the wrong version or persona")
    if payload.get("persona") != persona:
        raise FactRegistryError(f"{path} has the wrong version or persona")

    version = payload["version"]
    allowed = {
        "id",
        "statement",
        "applies_when",
        "source_session_ids",
        "related_history_session_ids",
    }
    if version == 2:
        allowed |= {"construction_key", "subjects", "supersedes"}

    facts: dict[int, dict[str, Any]] = {}
    for position, raw_fact in enumerate(payload.get("facts") or [], start=1):
        if not isinstance(raw_fact, dict):
            raise FactRegistryError(f"{path}: fact {position} is not an object")
        extra = set(raw_fact) - allowed
        if extra:
            raise FactRegistryError(f"{path}: fact {position} has unknown fields {sorted(extra)}")
        fact_id = raw_fact.get("id")
        if not isinstance(fact_id, int) or isinstance(fact_id, bool) or fact_id <= 0:
            raise FactRegistryError(f"{path}: fact id must be a positive integer: {fact_id!r}")
        if fact_id in facts:
            raise FactRegistryError(f"{path}: duplicate fact id {fact_id}")
        if not str(raw_fact.get("statement") or "").strip():
            raise FactRegistryError(f"{path}: fact {fact_id} has no statement")

        source_ids = _session_id_list(
            raw_fact.get("source_session_ids"),
            path=path,
            fact_id=fact_id,
            field="source_session_ids",
        )
        if not normalize_session_ids and not all(
            isinstance(item, str) and item for item in raw_fact["source_session_ids"]
        ):
            raise FactRegistryError(f"{path}: fact {fact_id} has invalid source_session_ids")
        related_raw = raw_fact.get("related_history_session_ids") or []
        if not isinstance(related_raw, list):
            raise FactRegistryError(
                f"{path}: fact {fact_id} has invalid related_history_session_ids"
            )
        related_ids = (
            _session_id_list(
                related_raw,
                path=path,
                fact_id=fact_id,
                field="related_history_session_ids",
            )
            if related_raw
            else []
        )
        if not normalize_session_ids and not all(
            isinstance(item, str) and item for item in related_raw
        ):
            raise FactRegistryError(
                f"{path}: fact {fact_id} has invalid related_history_session_ids"
            )
        # The legacy registry forbids duplicate source/related references.
        # Authenticated v2 checkpoints retain historical related context even
        # when it also supports the fact, so their source list remains usable.
        if version == 1 and set(source_ids) & set(related_ids):
            raise FactRegistryError(
                f"{path}: fact {fact_id} lists a source session as related history"
            )

        fact = dict(raw_fact)
        if normalize_session_ids:
            fact["source_session_ids"] = source_ids
            if "related_history_session_ids" in fact:
                fact["related_history_session_ids"] = related_ids
        facts[fact_id] = fact
    return facts


@lru_cache(maxsize=None)
def load_fact_registry(persona: str, root: Path = ROOT) -> dict[int, dict[str, Any]]:
    path = root / "registry" / "personas" / persona / "facts.yaml"
    try:
        payload = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise FactRegistryError(f"cannot load {path}: {exc}") from exc
    return _parse_fact_registry(
        payload,
        persona=persona,
        path=path,
        allowed_versions={1, 2},
        normalize_session_ids=payload.get("version") == 2,
    )


@lru_cache(maxsize=None)
def _load_registry_sessions(persona: str, root: Path) -> list[dict[str, Any]]:
    path = root / "registry" / "personas" / persona / "life_sim.yaml"
    try:
        payload = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise FactRegistryError(f"cannot load {path}: {exc}") from exc
    sessions = payload.get("sessions")
    if not isinstance(sessions, list) or not all(isinstance(session, dict) for session in sessions):
        raise FactRegistryError(f"{path}: sessions must be a list of objects")
    return sessions


@lru_cache(maxsize=None)
def _load_authenticated_checkpoint(
    persona: str, checkpoint: Path, identity: str
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    """Read checkpoint facts and sessions after the caller authenticated its bytes."""
    del identity  # It keys this cache so a changed authenticated checkpoint reloads.
    facts_path = checkpoint / "facts.yaml"
    entities_path = checkpoint / "entities.yaml"
    history_path = checkpoint / "life_sim.yaml"
    try:
        facts_payload = yaml.safe_load(facts_path.read_text()) or {}
        entities_payload = yaml.safe_load(entities_path.read_text()) or {}
        history_payload = yaml.safe_load(history_path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise FactRegistryError(f"cannot load checkpoint {checkpoint}: {exc}") from exc

    declared_personas = {
        "facts.yaml": facts_payload.get("persona") if isinstance(facts_payload, dict) else None,
        "entities.yaml": entities_payload.get("persona") if isinstance(entities_payload, dict) else None,
    }
    mismatched = [
        f"{name}={declared!r}"
        for name, declared in declared_personas.items()
        if declared != persona
    ]
    if mismatched:
        raise FactRegistryError(
            f"checkpoint persona does not match {persona!r}: " + ", ".join(mismatched)
        )

    facts = _parse_fact_registry(
        facts_payload,
        persona=persona,
        path=facts_path,
        allowed_versions={2},
        normalize_session_ids=True,
    )
    sessions = history_payload.get("sessions") if isinstance(history_payload, dict) else None
    if not isinstance(sessions, list) or not all(isinstance(session, dict) for session in sessions):
        raise FactRegistryError(f"{history_path}: sessions must be a list of objects")
    return facts, sessions


def _checkpoint_data(
    persona: str, checkpoint: Path
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    try:
        identity = validated_checkpoint_identity(checkpoint)
    except (OSError, ValueError) as exc:
        raise FactRegistryError(f"checkpoint authentication failed for {checkpoint}: {exc}") from exc
    return _load_authenticated_checkpoint(persona, checkpoint, identity)


def load_facts(
    persona: str, *, root: Path = ROOT, checkpoint: str | Path | None = None
) -> dict[int, dict[str, Any]]:
    """Load facts from the registry by default or an authenticated checkpoint."""
    checkpoint_path = _checkpoint_path(checkpoint)
    if checkpoint_path is None:
        return load_fact_registry(persona, root=root)
    return _checkpoint_data(persona, checkpoint_path)[0]


def load_sessions(
    persona: str, *, root: Path = ROOT, checkpoint: str | Path | None = None
) -> list[dict[str, Any]]:
    """Load sessions from the registry by default or an authenticated checkpoint."""
    checkpoint_path = _checkpoint_path(checkpoint)
    if checkpoint_path is None:
        return _load_registry_sessions(persona, root)
    return _checkpoint_data(persona, checkpoint_path)[1]


def source_session_ids_for_test(
    test: dict[str, Any],
    persona: str,
    root: Path = ROOT,
    checkpoint: str | Path | None = None,
) -> list[str]:
    facts = load_facts(persona, root=root, checkpoint=checkpoint)
    references = test.get("load_bearing_facts")
    if not isinstance(references, list) or not references:
        raise FactRegistryError("test has no load_bearing_facts")

    source_ids: list[str] = []
    seen: set[str] = set()
    for fact_id in references:
        if not isinstance(fact_id, int) or isinstance(fact_id, bool):
            raise FactRegistryError(
                f"test fact references must be integers, found {fact_id!r}"
            )
        if fact_id not in facts:
            raise FactRegistryError(f"test references unknown fact {fact_id}")
        for session_id in facts[fact_id]["source_session_ids"]:
            if session_id not in seen:
                source_ids.append(session_id)
                seen.add(session_id)
    return source_ids
