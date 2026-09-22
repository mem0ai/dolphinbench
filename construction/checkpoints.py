"""Authenticated construction checkpoint I/O and compatibility checks."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import tiktoken
import yaml

from construction.construction_io import file_hash, resolve_path
from harness.environment import get_setting


CHECKPOINT_PAYLOAD_FILES = (
    "life_sim.yaml",
    "session_purposes.json",
    "entities.yaml",
    "facts.yaml",
    "app_state.json",
)
CHECKPOINT_PAYLOAD_FILES_V2 = (*CHECKPOINT_PAYLOAD_FILES, "operation_results.json")


class CompletedOutputError(ValueError):
    """The selected output already contains a completed construction run."""


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text()) or {}


def dump_yaml(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(value, sort_keys=False, allow_unicode=True, width=100)
    )


def copy_data(value: Any) -> Any:
    return json.loads(json.dumps(value))


def data_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def history_tokens(history: list[dict[str, Any]]) -> int:
    encoder = tiktoken.get_encoding("o200k_base")
    return sum(
        len(encoder.encode(str(message)))
        for session in history
        for message in session.get("messages") or []
    )


def checkpoint_identity_version(manifest: dict[str, Any]) -> int:
    """Return the declared identity version; missing means legacy v1."""
    version = manifest.get("identity_version", 1)
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in {1, 2}
    ):
        raise ValueError("checkpoint identity_version must be 1 or 2")
    return version


def checkpoint_payload_files(identity_version: int) -> tuple[str, ...]:
    if identity_version == 1:
        return CHECKPOINT_PAYLOAD_FILES
    if identity_version == 2:
        return CHECKPOINT_PAYLOAD_FILES_V2
    raise ValueError("checkpoint identity_version must be 1 or 2")


def checkpoint_payload_hashes(
    checkpoint_dir: Path, *, identity_version: int = 1
) -> dict[str, str]:
    """Hash the complete state bundle required by an identity version."""
    payload_files = checkpoint_payload_files(identity_version)
    missing = [
        name for name in payload_files if not (checkpoint_dir / name).is_file()
    ]
    if missing:
        raise ValueError(
            "checkpoint is missing required state payloads: " + ", ".join(missing)
        )
    return {name: file_hash(checkpoint_dir / name) for name in payload_files}


def validated_checkpoint_identity(checkpoint_dir: Path) -> str:
    """Return the identity of a checkpoint after authenticating every payload."""
    manifest_path = checkpoint_dir / "checkpoint.json"
    if not manifest_path.is_file():
        raise ValueError("checkpoint is missing checkpoint.json")
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError("checkpoint.json must contain an object")
    identity_version = checkpoint_identity_version(manifest)
    payload_files = checkpoint_payload_files(identity_version)
    payload_hashes = checkpoint_payload_hashes(
        checkpoint_dir, identity_version=identity_version
    )
    recorded_hashes = manifest.get("state_payload_sha256")
    if not isinstance(recorded_hashes, dict) or set(recorded_hashes) != set(
        payload_files
    ):
        raise ValueError("checkpoint manifest must contain complete state payload hashes")
    mismatched = [
        name
        for name in payload_files
        if recorded_hashes.get(name) != payload_hashes[name]
    ]
    if mismatched:
        raise ValueError(
            "checkpoint state payload hashes do not match: " + ", ".join(mismatched)
        )
    files_sha256 = {
        "checkpoint.json": file_hash(manifest_path),
        **payload_hashes,
    }
    return data_hash(files_sha256)


def load_checkpoint(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    validated_checkpoint_identity(path)
    metadata = load_json(path / "checkpoint.json")
    unfinished_threads = metadata.get("unfinished_threads") or []
    if not isinstance(unfinished_threads, list) or not all(
        isinstance(row, dict) for row in unfinished_threads
    ):
        raise ValueError("checkpoint unfinished_threads must be a list of objects")
    state = {
        "history": load_yaml(path / "life_sim.yaml").get("sessions") or [],
        "session_purposes": load_json(path / "session_purposes.json"),
        "entities": load_yaml(path / "entities.yaml").get("entities") or [],
        "facts": load_yaml(path / "facts.yaml").get("facts") or [],
        "app_state": load_json(path / "app_state.json"),
        "unfinished_threads": copy_data(unfinished_threads),
    }
    return metadata, state


def save_checkpoint(
    *,
    path: Path,
    persona: str,
    week: dict[str, Any],
    previous_hash: str,
    inputs: dict[str, str],
    state: dict[str, Any],
    operation_results: list[dict[str, Any]],
    covered_events: list[str],
    system_sha256: str,
    session_budget: dict[str, Any] | None = None,
    revalidated_against_current_state: bool = False,
    weekly_planning: dict[str, Any] | None = None,
    progress: dict[str, Any] | None = None,
    identity_version: int = 1,
) -> dict[str, Any]:
    path.mkdir(parents=True, exist_ok=True)
    dump_yaml(path / "life_sim.yaml", {"sessions": state["history"]})
    dump_json(path / "session_purposes.json", state["session_purposes"])
    dump_yaml(
        path / "entities.yaml",
        {"version": 1, "persona": persona, "entities": state["entities"]},
    )
    dump_yaml(
        path / "facts.yaml",
        {"version": 2, "persona": persona, "facts": state["facts"]},
    )
    dump_json(path / "app_state.json", state["app_state"])
    dump_json(path / "operation_results.json", operation_results)
    state_payload_sha256 = checkpoint_payload_hashes(
        path, identity_version=identity_version
    )
    unfinished_threads = state.get("unfinished_threads") or []
    if not isinstance(unfinished_threads, list) or not all(
        isinstance(row, dict) for row in unfinished_threads
    ):
        raise ValueError("state.unfinished_threads must be a list of objects")
    metadata: dict[str, Any] = {
        "week_id": week["week_id"],
        "accepted_through": week["end_date"],
        "previous_checkpoint_sha256": previous_hash,
        "input_sha256": inputs,
        "construction_system_sha256": system_sha256,
        "sessions": len(state["history"]),
        "entities": len(state["entities"]),
        "facts": len(state["facts"]),
        "history_tokens_o200k_base": history_tokens(state["history"]),
        "covered_quarter_event_ids": list(covered_events),
        "state_sha256": data_hash(state["app_state"]),
        "state_payload_sha256": state_payload_sha256,
        "unfinished_threads": copy_data(unfinished_threads),
        "revalidated_against_current_state": revalidated_against_current_state,
    }
    if identity_version == 2:
        metadata["identity_version"] = 2
    if session_budget is not None:
        metadata["session_budget"] = session_budget
    if weekly_planning is not None:
        metadata["weekly_planning"] = copy_data(weekly_planning)
    if progress is not None:
        metadata["progress"] = copy_data(progress)
    dump_json(path / "checkpoint.json", metadata)
    return metadata


def validate_resume_checkpoint_for_config(
    *,
    config: dict[str, Any],
    checkpoint: dict[str, Any],
    checkpoint_dir: Path,
) -> None:
    persona = str(config.get("persona") or "")
    if not persona:
        raise ValueError("config persona is missing")
    recorded_personas: list[tuple[str, str]] = []
    for name in ("facts.yaml", "entities.yaml"):
        document = load_yaml(checkpoint_dir / name)
        if document.get("persona") is not None:
            recorded_personas.append((name, str(document["persona"])))
    if not recorded_personas:
        raise ValueError("resume checkpoint does not declare its persona")
    mismatched_personas = [
        f"{name}={recorded}"
        for name, recorded in recorded_personas
        if recorded != persona
    ]
    if mismatched_personas:
        raise ValueError(
            f"resume checkpoint persona does not match config persona {persona}: "
            + ", ".join(mismatched_personas)
        )

    quarter_start = date.fromisoformat(str(config["quarter_start"]))
    quarter_end = date.fromisoformat(str(config["quarter_end"]))
    accepted_through = date.fromisoformat(str(checkpoint.get("accepted_through") or ""))
    earliest_accepted_through = quarter_start - timedelta(days=1)
    if not earliest_accepted_through <= accepted_through <= quarter_end:
        raise ValueError(
            "resume checkpoint accepted_through is outside the configured quarter: "
            f"{accepted_through.isoformat()} not in "
            f"{earliest_accepted_through.isoformat()}..{quarter_end.isoformat()}"
        )

    recorded_hashes = checkpoint.get("input_sha256") or {}
    if not isinstance(recorded_hashes, dict):
        raise ValueError("resume checkpoint input_sha256 must be an object")
    # The checkpoint immediately before a quarter contains the previous quarter's
    # construction inputs. It supplies state only; the first checkpoint created in
    # this quarter records the new plan for all later resumes.
    if accepted_through != earliest_accepted_through:
        selected_paths = {
            "quarter_plan.json": resolve_path(config["quarter_plan"]),
            "overview.json": resolve_path(config["overview"]),
        }
        for name, selected_path in selected_paths.items():
            recorded = recorded_hashes.get(name)
            if recorded is None:
                continue
            if not selected_path.is_file():
                raise ValueError(f"current selected {name} is missing: {selected_path}")
            if str(recorded) != file_hash(selected_path):
                raise ValueError(
                    f"resume checkpoint {name} hash does not match the current selected file"
                )


def set_run_provenance_environment(output_dir: Path) -> None:
    """Set direct-run defaults without replacing an orchestrator's root ledger."""
    os.environ.setdefault("DOLPHINBENCH_CALL_LEDGER", get_setting(
        "DOLPHINBENCH_CALL_LEDGER", str(output_dir / "call_ledger.jsonl")))
    os.environ.setdefault("DOLPHINBENCH_CONSTRUCTION_RUN_ID", get_setting(
        "DOLPHINBENCH_CONSTRUCTION_RUN_ID", output_dir.name))
