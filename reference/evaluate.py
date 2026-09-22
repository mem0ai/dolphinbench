"""Hash-bound evaluation execution and suite validation."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import Any


import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from construction.checkpoints import validated_checkpoint_identity  # noqa: E402
from harness.environment import load_environment
from harness.dataset import load_test
from harness.adapters.hermes_profile import CONFIGURATIONS, REQUIRED_TOOLSETS, toolsets_for  # noqa: E402
HERMES_HOME = Path.home() / ".hermes"
HERMES_SOURCE = Path(os.environ.get("HERMES_SOURCE", HERMES_HOME / "hermes-agent"))
_HERMES_VENV = HERMES_SOURCE / ".venv"
if not _HERMES_VENV.exists():
    _HERMES_VENV = HERMES_SOURCE / "venv"
HERMES_BIN = Path(os.environ.get("HERMES_BIN", _HERMES_VENV / "bin" / "hermes"))
HERMES_PYTHON = Path(os.environ.get("HERMES_PYTHON", _HERMES_VENV / "bin" / "python"))

HERMES_MEMORY_CONFIGURATIONS = CONFIGURATIONS
AGENT_RUNTIMES = ("hermes", "claude")
SHARED_BACKEND_CONFIGURATIONS = frozenset({"mem0", "honcho", "hindsight", "supermemory"})
_HONCHO_INTERNAL_REASONING_EFFORTS = {
    "DERIVER_MODEL_CONFIG__THINKING_EFFORT": "medium",
    "SUMMARY_MODEL_CONFIG__THINKING_EFFORT": "medium",
    "DREAM_DEDUCTION_MODEL_CONFIG__THINKING_EFFORT": "none",
    "DREAM_INDUCTION_MODEL_CONFIG__THINKING_EFFORT": "none",
    "DIALECTIC_LEVELS__minimal__MODEL_CONFIG__THINKING_EFFORT": "none",
    "DIALECTIC_LEVELS__low__MODEL_CONFIG__THINKING_EFFORT": "none",
    "DIALECTIC_LEVELS__medium__MODEL_CONFIG__THINKING_EFFORT": "none",
    "DIALECTIC_LEVELS__high__MODEL_CONFIG__THINKING_EFFORT": "none",
    "DIALECTIC_LEVELS__max__MODEL_CONFIG__THINKING_EFFORT": "none",
}
PERSONAS = ("alex", "morgan", "riley")
PERSONA_TIMEZONES = {
    "alex": "America/New_York",
    "morgan": "America/Los_Angeles",
    "riley": "America/Chicago",
}
REQUIRED_CHECKPOINT_FILES = ("life_sim.yaml", "app_state.json", "facts.yaml", "checkpoint.json")
MANIFEST_VERSION = 2
DEFAULT_SMOKE_DISTRACTOR_SESSIONS = 100
HONCHO_SOURCE_DEFAULT = Path("/home/ubuntu/services/honcho")
HONCHO_OVERLAY_NAME = "docker-compose.dolphinbench.yml"
AGENT_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


def _hermes_executables(source: Path) -> tuple[Path, Path]:
    """Return the Hermes and Python executables belonging to *source*."""
    venv = source / ".venv"
    if not venv.exists():
        venv = source / "venv"
    return venv / "bin" / "hermes", venv / "bin" / "python"


class PreparationError(ValueError):
    """Raised for an invalid local evaluation preparation."""


class ManifestDriftError(RuntimeError):
    """Raised when a saved launch manifest no longer describes local inputs."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    if not cleaned:
        raise PreparationError("run id must contain letters or numbers")
    return cleaned


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_revision(path: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _distribution_version(python: Path, distribution: str) -> str | None:
    """Read an installed package version without importing the package."""
    if not python.is_file():
        return None
    code = (
        "import importlib.metadata as m; "
        f"print(m.version({distribution!r}))"
    )
    try:
        return subprocess.check_output(
            [str(python), "-c", code], text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _iter_tree_files(
    directory: Path, ignored_part_prefixes: tuple[str, ...] = (),
) -> Iterable[Path]:
    ignored = {".git", "__pycache__", "node_modules", ".venv", "venv"}
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or any(part in ignored for part in path.parts):
            continue
        if any(
            part.startswith(prefix)
            for part in path.relative_to(directory).parts
            for prefix in ignored_part_prefixes
        ):
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        yield path


def _snapshot_files(paths: Iterable[Path]) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted({Path(p).resolve() for p in paths}):
        if not path.is_file():
            raise PreparationError(f"required input is missing or not a file: {path}")
        snapshot[str(path)] = _sha256(path)
    return snapshot


def _snapshot_tree(
    directory: Path, ignored_part_prefixes: tuple[str, ...] = (),
) -> dict[str, Any]:
    if not directory.is_dir():
        raise PreparationError(f"required source tree is missing: {directory}")
    files = _snapshot_files(_iter_tree_files(directory, ignored_part_prefixes))
    digest = hashlib.sha256()
    for name, value in files.items():
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(value.encode())
        digest.update(b"\0")
    return {
        "root": str(directory.resolve()),
        "git_revision": _git_revision(directory),
        "sha256": digest.hexdigest(),
        "files": files,
        "ignored_part_prefixes": list(ignored_part_prefixes),
    }


def _pinned_honcho_commit(overlay: Path) -> str | None:
    """Return the upstream Honcho commit declared by the local overlay."""
    match = re.search(
        r"^\s*#\s*Pinned upstream Honcho commit:\s*([0-9a-f]{7,64})\s*$",
        overlay.read_text(),
        flags=re.MULTILINE,
    )
    return match.group(1) if match else None


def _honcho_provenance(env: dict[str, str]) -> tuple[dict[str, Any], list[Path], dict[str, Any]]:
    """Describe the exact local Honcho service used by this matrix.

    This is deliberately local-only.  The compose overlay is the source of
    truth for the internal LLM and embedding configuration; no service is
    contacted while preparing a manifest.
    """
    source = Path(env.get("DOLPHINBENCH_HONCHO_SOURCE", HONCHO_SOURCE_DEFAULT)).resolve()
    overlay = Path(env.get("DOLPHINBENCH_HONCHO_OVERLAY", source / HONCHO_OVERLAY_NAME)).resolve()
    if not source.is_dir():
        raise PreparationError(f"Honcho source tree is missing: {source}")
    if not overlay.is_file():
        raise PreparationError(f"Honcho Docker overlay is missing: {overlay}")
    compose = _load_yaml(overlay)
    environment = compose.get("x-honcho-dolphinbench-environment") or {}
    if not isinstance(environment, dict):
        raise PreparationError(f"Honcho overlay has no environment mapping: {overlay}")
    internal_models = {
        str(key): str(value)
        for key, value in environment.items()
        if str(key).endswith("MODEL_CONFIG__MODEL")
        and not str(key).startswith("EMBEDDING_MODEL_CONFIG__")
    }
    internal_reasoning_efforts = {
        str(key): str(value)
        for key, value in environment.items()
        if str(key).endswith("MODEL_CONFIG__THINKING_EFFORT")
    }
    internal_transports = {
        str(key): str(value)
        for key, value in environment.items()
        if str(key).endswith("MODEL_CONFIG__TRANSPORT")
        and not str(key).startswith("EMBEDDING_MODEL_CONFIG__")
    }
    embedding_model = environment.get("EMBEDDING_MODEL_CONFIG__MODEL")
    if not internal_models or not isinstance(embedding_model, str) or not embedding_model:
        raise PreparationError(f"Honcho overlay lacks model configuration: {overlay}")
    model_prefixes = {
        key.removesuffix("MODEL_CONFIG__MODEL") for key in internal_models
    }
    effort_prefixes = {
        key.removesuffix("MODEL_CONFIG__THINKING_EFFORT")
        for key in internal_reasoning_efforts
    }
    if (
        model_prefixes != effort_prefixes
        or internal_reasoning_efforts != _HONCHO_INTERNAL_REASONING_EFFORTS
    ):
        raise PreparationError(
            "Honcho extraction and summary calls must use medium reasoning; "
            "tool-using dialectic and dream calls must use none for Azure chat compatibility"
        )
    pinned_commit = _pinned_honcho_commit(overlay)
    if not pinned_commit:
        raise PreparationError(f"Honcho overlay has no pinned upstream commit: {overlay}")
    source_snapshot = _snapshot_tree(source, ("runtime",))
    return (
        {
            "source_root": str(source),
            "source_git_revision": source_snapshot["git_revision"],
            "source_sha256": source_snapshot["sha256"],
            "pinned_upstream_commit": pinned_commit,
            "overlay_path": str(overlay),
            "overlay_sha256": _sha256(overlay),
            "internal_models": dict(sorted(internal_models.items())),
            "internal_reasoning_efforts": dict(sorted(internal_reasoning_efforts.items())),
            "internal_transports": dict(sorted(internal_transports.items())),
            "embedding_model": embedding_model,
            "embedding_transport": environment.get("EMBEDDING_MODEL_CONFIG__TRANSPORT"),
        },
        [overlay],
        source_snapshot,
    )


def _runtime_provenance(
    hermes_source: Path,
    configurations: tuple[str, ...],
    env: dict[str, str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[Path]]:
    """Capture local code and service inputs that affect evaluation behavior."""
    hermes_snapshot = _snapshot_tree(hermes_source)
    _hermes_bin, hermes_python = _hermes_executables(hermes_source)
    package_python = hermes_python if hermes_python.is_file() else HERMES_PYTHON
    mem0_plugin = hermes_source / "plugins" / "memory" / "mem0"
    mem0_snapshot = _snapshot_tree(mem0_plugin)
    provenance: dict[str, Any] = {
        "agent_container": {
            "image": env.get("DOLPHINBENCH_AGENT_IMAGE"),
            "backend": ("modal-sandbox" if env.get("DOLPHINBENCH_AGENT_IMAGE", "").startswith("im-") else "local-docker"),
        },
        "hermes": {
            "source_root": str(hermes_source.resolve()),
            "git_revision": hermes_snapshot["git_revision"],
            "source_sha256": hermes_snapshot["sha256"],
        },
        "mem0": {
            "plugin_root": str(mem0_plugin.resolve()),
            "plugin_git_revision": mem0_snapshot["git_revision"],
            "plugin_sha256": mem0_snapshot["sha256"],
            "package": "mem0ai",
            "package_version": _distribution_version(package_python, "mem0ai"),
        },
    }
    source_trees = {
        "hermes_code": hermes_snapshot,
        "hermes_plugins": _snapshot_tree(hermes_source / "plugins"),
        "mem0_plugin": mem0_snapshot,
    }
    files: list[Path] = []
    if "honcho" in configurations:
        honcho, honcho_files, honcho_snapshot = _honcho_provenance(env)
        provenance["honcho"] = honcho
        source_trees["honcho_source"] = honcho_snapshot
        files.extend(honcho_files)
    for provider, package in (
        ("hindsight", "hindsight-client"),
        ("supermemory", "supermemory"),
    ):
        if provider not in configurations:
            continue
        plugin = hermes_source / "plugins" / "memory" / provider
        snapshot = _snapshot_tree(plugin)
        provenance[provider] = {
            "plugin_root": str(plugin.resolve()),
            "plugin_git_revision": snapshot["git_revision"],
            "plugin_sha256": snapshot["sha256"],
            "package": package,
            "package_version": _distribution_version(package_python, package),
        }
        source_trees[f"{provider}_plugin"] = snapshot
    return provenance, source_trees, files


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise PreparationError(f"expected a mapping in {path}")
    return data


def _parse_date(value: Any, location: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PreparationError(f"{location} is missing an ISO-8601 date")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        try:
            parsed = datetime.fromisoformat(value + "T00:00:00")
        except ValueError:
            raise PreparationError(f"{location} has invalid date {value!r}") from exc
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)


def _test_files(root: Path, persona: str, directory: Path | None = None) -> list[Path]:
    test_dir = directory or root / "tests" / persona
    files = sorted(test_dir.glob("[0-9][0-9][0-9].yaml"))
    expected = [f"{index:03d}.yaml" for index in range(1, 201)]
    if [path.name for path in files] != expected:
        raise PreparationError(
            f"{persona}: expected exactly tests/{persona}/001.yaml through 200.yaml"
        )
    return files


def _load_release_manifest(root: Path, persona: str) -> tuple[Path, dict[str, Any]]:
    path = root / "authoring" / "release_manifests" / f"{persona}.json"
    if not path.is_file():
        raise PreparationError(f"{persona}: accepted test release manifest is missing: {path}")
    try:
        manifest = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise PreparationError(f"{persona}: invalid test release manifest: {path}") from exc
    if manifest.get("persona") != persona:
        raise PreparationError(f"{persona}: test release manifest names another persona")
    if manifest.get("test_count") != 200:
        raise PreparationError(f"{persona}: test release manifest does not certify 200 tests")
    if not manifest.get("evaluation_date"):
        raise PreparationError(f"{persona}: test release manifest has no evaluation_date")
    if not manifest.get("checkpoint_identity"):
        raise PreparationError(f"{persona}: test release manifest has no checkpoint_identity")
    return path, manifest


def _validate_checkpoint(
    persona: str,
    checkpoint: Path,
    test_files: list[Path],
    release_manifest: dict[str, Any],
) -> dict[str, Any]:
    checkpoint = checkpoint.resolve()
    missing = [name for name in REQUIRED_CHECKPOINT_FILES if not (checkpoint / name).is_file()]
    if missing:
        raise PreparationError(f"{persona}: checkpoint {checkpoint} is missing {missing}")

    life_sim = _load_yaml(checkpoint / "life_sim.yaml")
    metadata = json.loads((checkpoint / "checkpoint.json").read_text())
    if not isinstance(metadata, dict):
        raise PreparationError(f"{persona}: checkpoint metadata must be a JSON object")
    try:
        json.loads((checkpoint / "app_state.json").read_text())
    except json.JSONDecodeError as exc:
        raise PreparationError(f"{persona}: checkpoint app_state.json is invalid JSON") from exc
    _load_yaml(checkpoint / "facts.yaml")

    checkpoint_identity = validated_checkpoint_identity(checkpoint)
    if checkpoint_identity != release_manifest["checkpoint_identity"]:
        raise PreparationError(
            f"{persona}: checkpoint identity {checkpoint_identity} does not match "
            f"the accepted tests {release_manifest['checkpoint_identity']}"
        )

    sessions = life_sim.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        raise PreparationError(f"{persona}: checkpoint has no sessions")
    ids: list[str] = []
    dates: list[datetime] = []
    for index, session in enumerate(sessions):
        if not isinstance(session, dict):
            raise PreparationError(f"{persona}: session {index} is not a mapping")
        session_id = str(session.get("id") or "")
        if not session_id:
            raise PreparationError(f"{persona}: session {index} is missing id")
        ids.append(session_id)
        dates.append(_parse_date(session.get("narrative_date"), f"{persona} session {session_id}"))
        messages = session.get("messages")
        if not isinstance(messages, list) or not all(isinstance(message, str) for message in messages):
            raise PreparationError(f"{persona}: session {session_id} has non-string messages")
    if len(ids) != len(set(ids)):
        raise PreparationError(f"{persona}: checkpoint session ids are not unique")
    if dates != sorted(dates):
        raise PreparationError(f"{persona}: checkpoint sessions are not chronological")
    declared_sessions = metadata.get("sessions")
    if declared_sessions is not None and declared_sessions != len(sessions):
        raise PreparationError(
            f"{persona}: checkpoint metadata sessions={declared_sessions} but history has {len(sessions)}"
        )
    accepted_through = metadata.get("accepted_through")
    if accepted_through:
        accepted = _parse_date(str(accepted_through), f"{persona} checkpoint accepted_through")
        if dates[-1].date() > accepted.date():
            raise PreparationError(f"{persona}: checkpoint history extends after accepted_through")

    evaluation_date = _parse_date(
        release_manifest["evaluation_date"],
        f"{persona} release evaluation_date",
    )
    test_dates: list[datetime] = []
    for path in test_files:
        test = load_test(path)
        try:
            test_number = int(test.get("id"))
        except (TypeError, ValueError) as exc:
            raise PreparationError(
                f"{persona}: {path.name} has an invalid numeric id"
            ) from exc
        if test_number != int(path.stem):
            raise PreparationError(f"{persona}: {path.name} id does not match filename")
        if not isinstance(test.get("test"), str) or not test["test"].strip():
            raise PreparationError(f"{persona}: {path.name} has no test request")
        test_date = test.get("narrative_anchor_date") or test.get("narrative_date")
        parsed_test_date = _parse_date(test_date, f"{persona} test {path.stem}")
        if parsed_test_date.date() != evaluation_date.date():
            raise PreparationError(
                f"{persona}: test {path.stem} uses {parsed_test_date.date()} instead "
                f"of accepted evaluation date {evaluation_date.date()}"
            )
        test_dates.append(parsed_test_date)
    if min(test_dates) < dates[-1]:
        raise PreparationError(f"{persona}: a test date precedes the final history session")

    return {
        "life_sim": life_sim,
        "sessions": len(sessions),
        "tests": len(test_files),
        "checkpoint_identity": checkpoint_identity,
        "evaluation_date": evaluation_date.date().isoformat(),
        "first_session_date": dates[0].isoformat(),
        "last_session_date": dates[-1].isoformat(),
        "first_test_date": min(test_dates).isoformat(),
        "last_test_date": max(test_dates).isoformat(),
        "metadata": {
            "accepted_through": metadata.get("accepted_through"),
            "declared_sessions": declared_sessions,
            "history_tokens_o200k_base": metadata.get("history_tokens_o200k_base"),
        },
    }


def _validate_canonical_runner_dates(root: Path) -> str:
    runner = root / "harness" / "run_simulation.py"
    source = runner.read_text()
    required_fragments = (
        "def _test_narrative_time",
        "spec.get(\"narrative_anchor_date\")",
        "test_narrative_time = _test_narrative_time(pre_spec)",
        "narrative_time=test_narrative_time",
    )
    missing = [fragment for fragment in required_fragments if fragment not in source]
    if missing:
        raise PreparationError(
            "canonical runner does not pass each test's narrative_anchor_date to the "
            f"agent: missing {missing}"
        )
    return "test.narrative_anchor_date"


def _token_count(text: str) -> int:
    try:
        import tiktoken
    except ImportError as exc:  # pragma: no cover - required by the benchmark release.
        raise PreparationError("tiktoken is required to count diagnostic history tokens") from exc
    return len(tiktoken.get_encoding("o200k_base").encode(text))


def _session_token_count(session: dict[str, Any]) -> int:
    return sum(_token_count(message) for message in session.get("messages") or [])


def _evenly_spaced(items: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """Choose deterministic items across the full chronological range."""
    if count <= 0 or not items:
        return []
    if count >= len(items):
        return list(items)
    if count == 1:
        return [items[len(items) // 2]]
    indexes = [round(index * (len(items) - 1) / (count - 1)) for index in range(count)]
    return [items[index] for index in indexes]


def _build_smoke_history(
    life_sim: dict[str, Any],
    facts_data: dict[str, Any],
    selected_tests: list[dict[str, Any]],
    distractor_session_count: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a small, chronological ingestion diagnostic without changing canon."""
    if distractor_session_count < 0:
        raise PreparationError("smoke distractor session count cannot be negative")

    sessions = life_sim.get("sessions") or []
    session_by_id = {str(session["id"]): session for session in sessions}
    facts = facts_data.get("facts") or []
    fact_by_id = {int(fact["id"]): fact for fact in facts}
    graph: dict[int, set[int]] = {fact_id: set() for fact_id in fact_by_id}
    for fact_id, fact in fact_by_id.items():
        for raw_previous in fact.get("supersedes") or []:
            previous = int(raw_previous)
            if previous not in fact_by_id:
                raise PreparationError(
                    f"fact {fact_id} supersedes missing fact {previous}"
                )
            graph[fact_id].add(previous)
            graph[previous].add(fact_id)

    requested_fact_ids: set[int] = set()
    for test in selected_tests:
        raw_ids = test.get("load_bearing_facts")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise PreparationError(
                f"smoke test {test.get('id')} has no load_bearing_facts"
            )
        requested_fact_ids.update(int(value) for value in raw_ids)
    missing_facts = sorted(requested_fact_ids - set(fact_by_id))
    if missing_facts:
        raise PreparationError(f"smoke tests refer to missing facts: {missing_facts}")

    connected_fact_ids = set(requested_fact_ids)
    pending = list(requested_fact_ids)
    while pending:
        fact_id = pending.pop()
        for related in graph[fact_id] - connected_fact_ids:
            connected_fact_ids.add(related)
            pending.append(related)

    required_session_ids: set[str] = set()
    related_history_ids: set[str] = set()
    for fact_id in connected_fact_ids:
        source_ids = fact_by_id[fact_id].get("source_session_ids")
        if not isinstance(source_ids, list) or not source_ids:
            raise PreparationError(f"fact {fact_id} has no source_session_ids")
        required_session_ids.update(str(value) for value in source_ids)
        related_history_ids.update(
            str(value)
            for value in fact_by_id[fact_id].get("related_history_session_ids") or []
        )
    missing_sessions = sorted(required_session_ids - set(session_by_id))
    if missing_sessions:
        raise PreparationError(
            f"smoke fact evidence refers to missing sessions: {missing_sessions[:10]}"
        )

    excluded_from_distractors = required_session_ids | related_history_ids
    unrelated = [
        session for session in sessions
        if str(session["id"]) not in excluded_from_distractors
    ]
    chosen_distractors: dict[str, dict[str, Any]] = {}

    # Exercise both long and multi-message ingestion. The remaining unrelated
    # history spans the complete chronology instead of clustering around facts.
    varied = sorted(
        (session for session in unrelated if len(session.get("messages") or []) > 1),
        key=lambda session: (-_session_token_count(session), str(session["id"])),
    )[: min(5, distractor_session_count)]
    for session in varied:
        chosen_distractors[str(session["id"])] = session
    remaining_slots = distractor_session_count - len(chosen_distractors)
    remaining = [
        session for session in unrelated
        if str(session["id"]) not in chosen_distractors
    ]
    for session in _evenly_spaced(remaining, remaining_slots):
        chosen_distractors[str(session["id"])] = session

    included_ids = required_session_ids | set(chosen_distractors)
    selected_sessions = [
        session for session in sessions if str(session["id"]) in included_ids
    ]
    token_count = sum(_session_token_count(session) for session in selected_sessions)
    diagnostic = {
        "requested_test_ids": [str(test["id"]) for test in selected_tests],
        "requested_fact_ids": sorted(requested_fact_ids),
        "connected_fact_ids": sorted(connected_fact_ids),
        "required_source_session_ids": sorted(required_session_ids),
        "related_history_excluded_from_distractors": sorted(related_history_ids),
        "required_source_sessions": len(required_session_ids),
        "distractor_sessions": len(chosen_distractors),
        "total_sessions": len(selected_sessions),
        "user_message_tokens_o200k_base": token_count,
        "first_session_date": selected_sessions[0]["narrative_date"],
        "last_session_date": selected_sessions[-1]["narrative_date"],
    }
    return {"sessions": selected_sessions}, diagnostic


def _provider_identity(
    configuration: str, persona: str, run_id: str, agent_runtime: str = "hermes",
) -> dict[str, str]:
    identity = f"{run_id}-{configuration}-{persona}"
    if configuration == "mem0":
        return {
            "kind": "mem0",
            "user_id": identity,
            "narrative_timezone": PERSONA_TIMEZONES[persona],
            "agent_runtime": agent_runtime,
        }
    if configuration == "honcho":
        return {
            "kind": "honcho",
            "workspace": identity,
            "peer_name": f"hermes-{identity}",
            "ai_peer": "hermes",
            "agent_runtime": agent_runtime,
        }
    if configuration == "hindsight":
        return {
            "kind": "hindsight",
            "bank_id": identity,
            "narrative_timezone": PERSONA_TIMEZONES[persona],
            "agent_runtime": agent_runtime,
        }
    if configuration == "supermemory":
        return {
            "kind": "supermemory",
            "container_tag": identity,
            "narrative_timezone": PERSONA_TIMEZONES[persona],
            "agent_runtime": agent_runtime,
        }
    return {
        "kind": configuration if configuration in SHARED_BACKEND_CONFIGURATIONS else "builtin",
        "namespace": identity,
        # Builtin memory belongs to the outer agent, never to a shared global
        # configuration or another agent runtime.
        "agent_runtime": agent_runtime,
    }


def _manifest_agent_runtime(manifest: dict[str, Any]) -> str:
    """Return the explicit runtime, treating completed legacy manifests as Hermes."""
    runtime = str(manifest.get("agent_runtime") or "hermes")
    if runtime not in AGENT_RUNTIMES:
        raise PreparationError(
            f"unsupported agent runtime {runtime!r}; choose one of {AGENT_RUNTIMES}"
        )
    return runtime


def _validate_runtime_configuration(
    agent_runtime: str, configuration: str,
) -> None:
    if agent_runtime not in AGENT_RUNTIMES:
        raise PreparationError(
            f"unsupported agent runtime {agent_runtime!r}; choose one of {AGENT_RUNTIMES}"
        )
    if configuration not in CONFIGURATIONS:
        raise PreparationError(f"unknown configuration {configuration!r}")
    if agent_runtime == "claude" and configuration not in SHARED_BACKEND_CONFIGURATIONS | {"builtin"}:
        raise PreparationError(
            f"Claude supports an external memory backend or its frozen native auto-memory; got {configuration!r}"
        )


def _assert_manifest_launch_wiring(manifest: dict[str, Any]) -> None:
    """Fail before profile creation when the canonical runner cannot run a job."""
    agent_runtime = _manifest_agent_runtime(manifest)
    config_dirs: set[str] = set()
    for job in manifest.get("jobs") or []:
        runtime = str(job.get("agent_runtime") or agent_runtime)
        configuration = str(job.get("configuration") or "")
        _validate_runtime_configuration(runtime, configuration)
        if runtime != agent_runtime:
            raise PreparationError(
                f"{job.get('id')}: job runtime {runtime!r} differs from manifest runtime {agent_runtime!r}"
            )
        config_dir = job.get("runtime_config_dir")
        if not isinstance(config_dir, str) or not config_dir:
            raise PreparationError(f"{job.get('id')}: missing isolated runtime config directory")
        if config_dir in config_dirs:
            raise PreparationError(f"{job.get('id')}: runtime config directory is not unique")
        config_dirs.add(config_dir)
        identity = job.get("provider_identity") or {}
        if runtime == "hermes":
            if configuration not in HERMES_MEMORY_CONFIGURATIONS:
                raise PreparationError(
                    f"{job.get('id')}: Hermes has no driver wiring for memory configuration "
                    f"{configuration!r}"
                )
            if configuration == "builtin" and identity.get("agent_runtime", runtime) != runtime:
                raise PreparationError(
                    f"{job.get('id')}: builtin memory identity must belong to its agent runtime"
                )
            continue
        if runtime != "claude":
            raise PreparationError(f"{job.get('id')}: unsupported agent runtime {runtime!r}")
        if (manifest.get("evaluation_scope") or {}).get("kind") != "test_only":
            raise PreparationError(f"{job.get('id')}: {runtime} supports test-only runs only")
        if runtime == "claude" and configuration == "builtin":
            native = job.get("native_memory")
            if not isinstance(native, dict):
                raise PreparationError(f"{job.get('id')}: Claude built-in job has no frozen native-memory input")
            if identity.get("kind") != "claude_native_auto_memory":
                raise PreparationError(f"{job.get('id')}: Claude built-in job has the wrong memory identity")
            if not isinstance(native.get("archive_path"), str) or not native.get("archive_sha256"):
                raise PreparationError(f"{job.get('id')}: Claude built-in job has no verified memory archive")
            if native.get("fixed_project_dir") != "/tmp/dolphinbench-claude-native-memory-project":
                raise PreparationError(f"{job.get('id')}: Claude built-in job uses the wrong project directory")
            if (manifest.get("shared_backend_reuse") or {}).get("allow_cross_agent") is not False:
                raise PreparationError(f"{job.get('id')}: Claude native auto-memory cannot be shared across agents")
            continue
        raise PreparationError(
            "This retained runner does not execute Claude external-memory evaluations."
        )


def _resolve_agent_identity(
    model: str,
    context_length: int,
    agent_base_url: str | None,
    agent_api_key_env: str | None,
    env: dict[str, str],
    reasoning_effort: str = "medium",
) -> dict[str, Any]:
    if reasoning_effort not in AGENT_REASONING_EFFORTS:
        raise PreparationError(
            f"unsupported agent reasoning effort {reasoning_effort!r}; "
            f"choose one of {AGENT_REASONING_EFFORTS}"
        )
    if bool(agent_base_url) != bool(agent_api_key_env):
        raise PreparationError(
            "--agent-base-url and --agent-api-key-env must be provided together"
        )
    if agent_base_url:
        if not env.get(agent_api_key_env or ""):
            raise PreparationError(f"missing agent API key environment variable {agent_api_key_env}")
        endpoint = agent_base_url.rstrip("/")
        api_key_env = agent_api_key_env
        hostname = (urlparse(endpoint).hostname or "").lower()
        provider = (
            "azure-foundry"
            if hostname == "openai.azure.com" or hostname.endswith(".openai.azure.com")
            else "openrouter"
            if hostname == "openrouter.ai" or hostname.endswith(".openrouter.ai")
            else "custom"
        )
    else:
        missing = [key for key in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY") if not env.get(key)]
        if missing:
            raise PreparationError(f"missing required environment variables: {missing}")
        endpoint = env["AZURE_OPENAI_ENDPOINT"].rstrip("/") + "/openai/v1"
        api_key_env = "AZURE_OPENAI_API_KEY"
        provider = "azure-foundry"
    api_mode = env.get("DOLPHINBENCH_AGENT_API_MODE", "codex_responses")
    if api_mode not in {"chat_completions", "codex_responses"}:
        raise PreparationError(f"unsupported agent API mode {api_mode!r}")
    return {
        "provider": provider,
        "model": model,
        "base_url": endpoint,
        "api_key_env": api_key_env,
        "api_mode": api_mode,
        "context_length": context_length,
        "reasoning_effort": reasoning_effort,
        "max_turns": 60,
    }


def _subscription_agent_identity(runtime: str, model: str) -> dict[str, Any]:
    """Describe a Claude Code subscription run without API billing fields."""
    if runtime == "claude":
        return {
            "provider": "claude-subscription",
            "model": model,
            "auth_mode": "claude_subscription_oauth",
            "api_key_env": None,
            "base_url": None,
        }
    raise PreparationError(f"{runtime!r} is not a subscription runtime")


def _resolve_judge_identity(env: dict[str, str]) -> dict[str, Any]:
    backend = env.get("DOLPHINBENCH_JUDGE_BACKEND", "azure").lower()
    common = {
        "backend": backend,
        "reasoning_effort": env.get(
            "DOLPHINBENCH_JUDGE_REASONING_EFFORT", "medium"
        ),
        "max_tokens": int(env.get("DOLPHINBENCH_JUDGE_MAX_TOKENS", "8000")),
        "timeout_seconds": int(env.get("DOLPHINBENCH_JUDGE_TIMEOUT", "60")),
        "max_retries": int(env.get("DOLPHINBENCH_JUDGE_MAX_RETRIES", "5")),
        "parallelism_per_test": int(env.get("DOLPHINBENCH_JUDGE_PARALLELISM", "8")),
        "concurrent_test_grades": int(env.get("DOLPHINBENCH_GRADING_WORKERS", "2")),
    }
    if backend == "azure":
        missing = [
            key for key in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY")
            if not env.get(key)
        ]
        if missing:
            raise PreparationError(f"missing Azure judge environment variables: {missing}")
        return {
            **common,
            "deployment": env.get("DOLPHINBENCH_JUDGE_DEPLOYMENT", "gpt-5.6-sol"),
            "endpoint": env["AZURE_OPENAI_ENDPOINT"].rstrip("/"),
            "api_version": env.get("DOLPHINBENCH_JUDGE_API_VERSION", "2024-08-01-preview"),
            "api_key_env": "AZURE_OPENAI_API_KEY",
        }
    if backend == "openai":
        key_env = env.get("DOLPHINBENCH_JUDGE_KEY_ENV", "OPENROUTER_API_KEY")
        if not env.get(key_env):
            raise PreparationError(f"missing judge API key environment variable {key_env}")
        return {
            **common,
            "model": env.get("DOLPHINBENCH_JUDGE_MODEL", "openai/gpt-4o-mini"),
            "endpoint": env.get(
                "DOLPHINBENCH_JUDGE_ENDPOINT",
                "https://openrouter.ai/api/v1/chat/completions",
            ),
            "api_key_env": key_env,
        }
    raise PreparationError(f"unsupported judge backend {backend!r}")


def _enforce_judge_parallelism(
    judge: dict[str, Any], configurations: tuple[str, ...],
) -> dict[str, Any]:
    """Keep the three-provider comparison within the shared Azure limit."""
    if {"builtin", "mem0", "honcho"}.issubset(configurations):
        judge = dict(judge)
        judge["parallelism_per_test"] = 1
        judge["parallelism_reason"] = (
            "The builtin, Mem0, and Honcho jobs share the Azure endpoint; "
            "judge calls are serialized within each test."
        )
    return judge


def _pricing_identity(root: Path, model: str) -> tuple[dict[str, Any], Path]:
    pricing_path = root / "pricing" / "latest.json"
    if not pricing_path.is_file():
        raise PreparationError(f"pricing snapshot is missing: {pricing_path}")
    raw = json.loads(pricing_path.read_text())
    pricing = raw.get("snapshot", raw)
    entry = (pricing.get("models") or {}).get(model)
    if not isinstance(entry, dict):
        raise PreparationError(f"pricing snapshot has no exact entry for model {model!r}")
    max_input = entry.get("max_input_tokens") or entry.get("max_tokens")
    try:
        max_input_tokens = int(max_input)
    except (TypeError, ValueError) as exc:
        raise PreparationError(f"pricing entry for {model!r} has no numeric max_input_tokens") from exc
    return {
        "path": str(pricing_path.resolve()),
        "sha256": _sha256(pricing_path),
        "snapshot_date": pricing.get("snapshot_date"),
        "model": model,
        "max_input_tokens": max_input_tokens,
        "rates": {
            key: entry.get(key)
            for key in (
                "input_cost_per_token",
                "output_cost_per_token",
                "cache_read_input_token_cost",
                "cache_creation_input_token_cost",
            )
        },
    }, pricing_path


def _subscription_pricing_identity(root: Path, model: str) -> tuple[dict[str, Any], Path]:
    """Record that a subscription run has no per-token API price here."""
    pricing_path = root / "pricing" / "latest.json"
    if not pricing_path.is_file():
        raise PreparationError(f"pricing snapshot is missing: {pricing_path}")
    raw = json.loads(pricing_path.read_text())
    pricing = raw.get("snapshot", raw)
    return {
        "path": str(pricing_path.resolve()),
        "sha256": _sha256(pricing_path),
        "snapshot_date": pricing.get("snapshot_date"),
        "model": model,
        "billing_mode": "subscription",
        "metered_api_cost": False,
        "rates": None,
    }, pricing_path


def _checkpoint_for(persona: str, supplied: dict[str, Path], root: Path) -> Path:
    if persona == "morgan":
        checkpoint = (root / "construction" / "v2" / "morgan" / "release" / "500k_final" / "checkpoint").resolve()
        override = supplied.get(persona)
        if override and override.resolve() != checkpoint:
            raise PreparationError(
                "Morgan evaluations must use construction/v2/morgan/release/500k_final/checkpoint"
            )
        return checkpoint
    if persona not in supplied:
        raise PreparationError(
            f"{persona}: pass --checkpoint {persona}=<authenticated-final-checkpoint-directory>"
        )
    return supplied[persona].resolve()


def _checkpoint_args(values: list[str], personas: tuple[str, ...]) -> dict[str, Path]:
    parsed: dict[str, Path] = {}
    for value in values:
        if "=" in value:
            persona, raw_path = value.split("=", 1)
        elif len(personas) == 1:
            persona, raw_path = personas[0], value
        else:
            raise PreparationError("checkpoint must be PERSONA=PATH when more than one persona is selected")
        if persona not in personas or not raw_path:
            raise PreparationError(f"invalid checkpoint selection {value!r}")
        if persona in parsed:
            raise PreparationError(f"duplicate checkpoint selection for {persona}")
        parsed[persona] = Path(raw_path)
    return parsed


def _parse_choices(raw: str, allowed: tuple[str, ...], name: str) -> tuple[str, ...]:
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    invalid = sorted(set(values) - set(allowed))
    if not values or invalid:
        raise PreparationError(f"invalid {name}: {invalid or 'none selected'}")
    return values


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def prepare_matrix(
    *,
    output_root: Path,
    personas: tuple[str, ...],
    configurations: tuple[str, ...],
    checkpoints: dict[str, Path],
    model: str,
    agent_context_length: int,
    agent_base_url: str | None,
    agent_api_key_env: str | None,
    agent_reasoning_effort: str = "medium",
    agent_runtime: str = "hermes",
    allow_cross_agent_shared_backend_reuse: bool = False,
    shared_backend_provider_identity: str | None = None,
    root: Path = ROOT,
    hermes_source: Path = HERMES_SOURCE,
    env: dict[str, str] | None = None,
    smoke_test_ids: tuple[str, ...] | None = None,
    smoke_distractor_sessions: int = DEFAULT_SMOKE_DISTRACTOR_SESSIONS,
) -> Path:
    """Create one offline manifest.  This function never creates a profile."""
    root = root.resolve()
    output_root = output_root.resolve()
    if output_root.exists():
        raise PreparationError(f"output root already exists; each run needs a new path: {output_root}")
    env = dict(env) if env is not None else load_environment(HERMES_HOME / ".env")
    if not configurations:
        raise PreparationError("select at least one configuration")
    for configuration in configurations:
        _validate_runtime_configuration(agent_runtime, configuration)
    if allow_cross_agent_shared_backend_reuse or shared_backend_provider_identity:
        raise PreparationError("cross-agent shared-backend reuse is disabled")
    if smoke_test_ids is not None:
        if len(personas) != 1:
            raise PreparationError("a diagnostic smoke accepts exactly one persona")
    agent = _resolve_agent_identity(
        model,
        agent_context_length,
        agent_base_url,
        agent_api_key_env,
        env,
        agent_reasoning_effort,
    )
    judge = _enforce_judge_parallelism(_resolve_judge_identity(env), configurations)
    pricing, pricing_path = _pricing_identity(root, model)
    if agent_context_length > pricing["max_input_tokens"]:
        raise PreparationError(
            f"declared context length {agent_context_length} exceeds {model} pricing capacity "
            f"{pricing['max_input_tokens']}"
        )

    run_id = _safe_name(
        f"dolphinbench-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"
    )
    output_root.mkdir(parents=True, exist_ok=False)
    try:
        persona_inputs: dict[str, Any] = {}
        jobs: list[dict[str, Any]] = []
        generated_paths: list[Path] = []
        input_paths: list[Path] = [
            root / "harness" / "benchmark_soul.md",
            root / "harness" / "environment.py",
            root / "harness" / "run_simulation.py",
            root / "harness" / "hermes_driver.py",
            root / "harness" / "claude_driver.py",
            root / "harness" / "memory_capture.py",
            root / "harness" / "costing.py",
            root / "graders" / "mechanical.py",
            root / "graders" / "llm_judge.py",
            root / "mock_mcp" / "server.py",
            root / 'reference' / 'runtimes' / 'hermes.py',
            root / "harness" / "adapters" / "hermes_profile.py",
            root / 'reference' / 'evaluate.py',
            pricing_path,
        ]
        for persona in personas:
            checkpoint = _checkpoint_for(persona, checkpoints, root)
            tests = _test_files(root, persona)
            mock_manifest_path = root / "mock_mcp" / "manifests" / f"{persona}.yaml"
            mock_manifest = _load_yaml(mock_manifest_path)
            simulated_app_tools = [
                str(tool) for tool in mock_manifest.get("tools", []) or []
            ]
            if not simulated_app_tools or len(simulated_app_tools) != len(set(simulated_app_tools)):
                raise PreparationError(
                    f"{persona}: simulated app tool manifest is empty or contains duplicates"
                )
            release_manifest_path, release_manifest = _load_release_manifest(root, persona)
            validated = _validate_checkpoint(
                persona, checkpoint, tests, release_manifest,
            )
            selected_test_paths = tests
            seed_life_sim = validated["life_sim"]
            smoke_details = None
            if smoke_test_ids is not None:
                selected = set(smoke_test_ids)
                selected_test_paths = [path for path in tests if path.stem in selected]
                if len(selected_test_paths) != len(selected):
                    missing = sorted(selected - {path.stem for path in selected_test_paths})
                    raise PreparationError(f"unknown smoke test ids: {missing}")
                selected_specs = [load_test(path) for path in selected_test_paths]
                facts_data = _load_yaml(checkpoint / "facts.yaml")
                seed_life_sim, smoke_details = _build_smoke_history(
                    validated["life_sim"],
                    facts_data,
                    selected_specs,
                    smoke_distractor_sessions,
                )
            numeric_path = output_root / "simulations" / persona / "numeric_life_sim.yaml"
            numeric = {
                "name": (
                    f"{persona} diagnostic evaluation smoke"
                    if smoke_test_ids is not None
                    else f"{persona} numeric final evaluation corpus"
                ),
                "description": (
                    "A disposable ingestion diagnostic built from canonical fact evidence "
                    "and chronology-spanning unrelated history. It is not a benchmark corpus."
                    if smoke_test_ids is not None
                    else "Generated locally from an authenticated final checkpoint and numeric test corpus."
                ),
                "sessions": seed_life_sim["sessions"],
                "test_queries": [{"id": path.stem} for path in selected_test_paths],
            }
            runner_date_source = _validate_canonical_runner_dates(root)
            numeric_path.parent.mkdir(parents=True, exist_ok=True)
            numeric_path.write_text(yaml.safe_dump(numeric, sort_keys=False, allow_unicode=True))
            checkpoint_paths = list(_iter_tree_files(checkpoint))
            input_paths.extend([
                *checkpoint_paths,
                *tests,
                release_manifest_path,
                root / "registry" / "personas.yaml",
                mock_manifest_path,
                root / "mock_mcp" / "state" / f"{persona}_baseline.json",
            ])
            generated_paths.append(numeric_path)
            persona_inputs[persona] = {
                "checkpoint_dir": str(checkpoint),
                "numeric_simulation": str(numeric_path),
                "validation": {key: value for key, value in validated.items() if key != "life_sim"},
                "canonical_runner_test_time_source": runner_date_source,
                "simulated_app_tools": simulated_app_tools,
                "diagnostic_smoke": smoke_details,
            }
            for configuration in configurations:
                run_dir = output_root / "jobs" / agent_runtime / configuration / persona
                runtime_config_dir = run_dir / "runtime-config"
                job_id = f"{configuration}-{persona}"
                profile = f"{run_id}-{configuration}-{persona}-profile"
                if len(profile) > 64:
                    raise PreparationError(
                        f"generated Hermes profile name exceeds 64 characters: {profile}"
                    )
                jobs.append({
                    "id": job_id,
                    "run_id": f"{run_id}-{configuration}-{persona}",
                    "persona": persona,
                    "agent_runtime": agent_runtime,
                    "configuration": configuration,
                    "provider": configuration,
                    "profile": profile,
                    "toolsets": toolsets_for(configuration),
                    "provider_identity": _provider_identity(
                        configuration, persona, run_id, agent_runtime,
                    ),
                    # Runtime configuration is per job.  Preparation creates
                    # no config and never reads a global runtime configuration.
                    "runtime_config_dir": str(runtime_config_dir),
                    "run_dir": str(run_dir),
                    "state_path": str(run_dir / "state.json"),
                    "log_path": str(run_dir / "calls.jsonl"),
                    "tool_config_path": str(run_dir / "tool_config.json"),
                    "result_path": str(run_dir / "results.json"),
                    "ledger_path": str(run_dir / "cost_ledger.jsonl"),
                    "profile_soul_path": str(run_dir / "profile_soul.md"),
                    "sim_path": str(numeric_path),
                    "prefill_path": None,
                })

        runtime_provenance, source_trees, provenance_files = _runtime_provenance(
            hermes_source, configurations, env,
        )
        input_paths.extend(provenance_files)
        file_hashes = _snapshot_files([*input_paths, *generated_paths])
        hermes_bin, hermes_python = _hermes_executables(hermes_source)
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "prepared_at": _now(),
            "run_id": run_id,
            "phase": "prepared_offline",
            "root": str(root),
            "output_root": str(output_root),
            "agent_runtime": agent_runtime,
            "agent": agent,
            "judge": judge,
            "pricing": pricing,
            "runtime_provenance": runtime_provenance,
            "personas": list(personas),
            "configurations": list(configurations),
            "shared_backend_reuse": {
                "allow_cross_agent": allow_cross_agent_shared_backend_reuse,
                "provider_identity": shared_backend_provider_identity,
            },
            "evaluation_scope": (
                {
                    "kind": "diagnostic_smoke",
                    "test_ids": list(smoke_test_ids),
                    "scores_are_publishable": False,
                    "provider_identities_are_disposable": True,
                }
                if smoke_test_ids is not None
                else {"kind": "final_corpus", "scores_are_publishable": True}
            ),
            "persona_inputs": persona_inputs,
            "jobs": jobs,
            "hashes": {"files": file_hashes, "source_trees": source_trees},
            "launch": {
                "canonical_runner": str((root / "harness" / "run_simulation.py").resolve()),
                "hermes_bin": str(hermes_bin),
                "hermes_python": str(hermes_python),
                "mock_mcp_python": sys.executable,
                "profile_creation": "deferred_until_explicit_launch",
                "provider_creation": "deferred_until_explicit_launch",
                "runtime_driver_wiring": {
                    "hermes": "harness/run_simulation.py: HERMES_PROVIDERS",
                    "claude": None,
                },
                "runner_resume_contract": {
                    "interrupted_phase": "--resume",
                    "later_disjoint_phase": "--skip-seeding",
                },
            },
        }
        manifest_path = output_root / "launch_manifest.json"
        _write_json(manifest_path, manifest)
        return manifest_path
    except Exception:
        # Preparation may leave only its own local artifacts.  Remove them on a
        # failed validation so a retry cannot accidentally reuse a partial run.
        import shutil
        shutil.rmtree(output_root, ignore_errors=True)
        raise


def _seed_source_state(
    seed_manifest_path: Path,
    seed_state_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load and validate the completed seed run without checking old test hashes."""
    try:
        seed_manifest = json.loads(seed_manifest_path.read_text())
        seed_state = json.loads(seed_state_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PreparationError(f"cannot read completed seed artifacts: {exc}") from exc
    if seed_manifest.get("manifest_version") != MANIFEST_VERSION:
        raise PreparationError("completed seed manifest has an unsupported version")
    if seed_manifest.get("phase") != "prepared_offline":
        raise PreparationError("completed seed manifest was already launched or is invalid")
    if seed_state.get("manifest") != str(seed_manifest_path.resolve()):
        raise PreparationError("seed state belongs to a different seed manifest")
    if not seed_state.get("phases"):
        raise PreparationError("seed state contains no launch phase")
    return seed_manifest, seed_state


def _source_job_result_paths(state: dict[str, Any], job_id: str) -> list[Path]:
    paths = _job_result_paths(state, job_id)
    if not paths:
        raise PreparationError(f"seed job {job_id} has no durable result file")
    return paths


def _profile_hashes_match(
    expected: dict[str, str | None], actual: dict[str, str | None],
) -> bool:
    """Treat a newly tracked but absent config file like an older missing key."""
    return all(
        expected.get(name) == actual.get(name)
        for name in set(expected) | set(actual)
    )


def _write_test_only_runtime_files(job: dict[str, Any]) -> None:
    """Create test output files without changing the completed seed run."""
    run_dir = Path(job["run_dir"])
    run_dir.mkdir(parents=True, exist_ok=False)
    Path(job["ledger_path"]).write_text("")
    if job.get("test_runtime") is not None:
        _write_json(Path(job["test_runtime_path"]), job["test_runtime"])
    if job.get("agent_runtime") == "claude":
        (run_dir / "memory_provider_identity.json").write_text(
            json.dumps({
                "provider_identity": job["provider_identity"],
                "gateway_config": job.get("gateway_config") or {},
            }, sort_keys=True) + "\n"
        )


def prepare_test_only(
    *,
    seed_manifest_path: Path,
    seed_state_path: Path,
    output_root: Path,
    configurations: tuple[str, ...],
    test_ids: tuple[str, ...],
    root: Path = ROOT,
    hermes_source: Path = HERMES_SOURCE,
    env: dict[str, str] | None = None,
    agent_runtime: str | None = None,
    allow_cross_agent_shared_backend_reuse: bool = False,
    shared_backend_provider_identity: str | None = None,
    model: str | None = None,
    agent_base_url: str | None = None,
    agent_api_key_env: str | None = None,
    agent_context_length: int | None = None,
    agent_reasoning_effort: str | None = None,
    published_tests_directory: Path | None = None,
    published_test_runtime: dict[str, Any] | None = None,
) -> Path:
    """Prepare a fresh test manifest from already-complete seeded profiles.

    This function is local-only.  It never creates a Hermes profile, contacts a
    memory provider, or runs the canonical runner.
    """
    root = root.resolve()
    seed_manifest_path = seed_manifest_path.resolve()
    seed_state_path = seed_state_path.resolve()
    output_root = output_root.resolve()
    if output_root.exists():
        raise PreparationError(
            f"output root already exists; each run needs a new path: {output_root}"
        )
    if not configurations:
        raise PreparationError("select at least one configuration")
    invalid = sorted(set(configurations) - set(CONFIGURATIONS))
    if invalid:
        raise PreparationError(f"invalid configurations: {invalid}")
    if not test_ids:
        raise PreparationError("select at least one test id")
    runtime_tests: dict[str, Any] | None = None
    runtime_mock_root: Path | None = None
    if published_test_runtime is not None:
        runtime_tests = published_test_runtime.get("tests")
        if not isinstance(runtime_tests, dict) or set(runtime_tests) != set(test_ids):
            raise PreparationError(
                "published test runtime must contain exactly one entry per selected test"
            )
        runtime_mock_root = Path(str(published_test_runtime.get("mock_mcp_root") or "")).resolve()
        if not runtime_mock_root.is_dir():
            raise PreparationError(
                f"published test runtime mock MCP directory is missing: {runtime_mock_root}"
            )

    seed_manifest, seed_state = _seed_source_state(
        seed_manifest_path, seed_state_path,
    )
    source_agent_runtime = _manifest_agent_runtime(seed_manifest)
    target_agent_runtime = agent_runtime or source_agent_runtime
    if target_agent_runtime not in AGENT_RUNTIMES:
        raise PreparationError(
            f"unsupported agent runtime {target_agent_runtime!r}; choose one of {AGENT_RUNTIMES}"
        )
    if target_agent_runtime != "hermes":
        raise PreparationError(
            "prepare_test_only consumes Hermes ingestion manifests; "
            "use prepare_claude_native_memory_test_only for Claude Built-In receipts."
        )
    if source_agent_runtime != "hermes" or allow_cross_agent_shared_backend_reuse or shared_backend_provider_identity:
        raise PreparationError("cross-agent shared-backend reuse is disabled")
    if Path(seed_manifest["root"]).resolve() != root:
        raise PreparationError("seed manifest belongs to a different repository")
    source_jobs = {
        str(job["id"]): job for job in seed_manifest.get("jobs") or []
    }
    selected_jobs: list[dict[str, Any]] = []
    for persona in seed_manifest.get("personas") or []:
        for configuration in configurations:
            job_id = f"{configuration}-{persona}"
            source_job = source_jobs.get(job_id)
            if source_job is None:
                raise PreparationError(
                    f"completed seed manifest has no selected job {job_id}"
                )
            source_runtime = str(source_job.get("agent_runtime") or source_agent_runtime)
            if source_runtime != "hermes":
                raise PreparationError(
                    f"{job_id}: completed seeds are reusable only through a wired Hermes runtime"
                )
            result_paths = _source_job_result_paths(seed_state, job_id)
            if not _seed_is_complete_for_job(seed_state, source_job):
                raise PreparationError(
                    f"{job_id}: ingestion is incomplete; test-only runs require a complete seed"
                )
            profile_dir = HERMES_HOME / "profiles" / source_job["profile"]
            if not profile_dir.is_dir():
                raise PreparationError(
                    f"{job_id}: completed seeded profile is missing: {profile_dir}"
                )
            expected_profile_hashes = (seed_state.get("profiles") or {}).get(job_id)
            if not expected_profile_hashes:
                raise PreparationError(
                    f"{job_id}: seed state has no profile configuration hashes"
                )
            from reference.runtimes.hermes import profile_config_hashes
            actual_profile_hashes = profile_config_hashes(profile_dir)
            if not _profile_hashes_match(
                expected_profile_hashes, actual_profile_hashes,
            ):
                raise PreparationError(
                    f"{job_id}: seeded profile configuration changed after ingestion"
                )
            selected_jobs.append({
                "source": source_job,
                "result_paths": result_paths,
                "profile_hashes": actual_profile_hashes,
            })

    env = dict(env) if env is not None else load_environment(HERMES_HOME / ".env")
    seed_agent = seed_manifest.get("agent")
    if not isinstance(seed_agent, dict):
        raise PreparationError("completed seed manifest is missing its agent configuration")
    agent = dict(seed_agent)
    if model:
        context_length = agent_context_length or int(seed_agent.get("context_length") or 0)
        if context_length < 1:
            raise PreparationError(
                "a Hermes model override requires a positive context length"
            )
        agent = _resolve_agent_identity(
            model,
            context_length,
            agent_base_url,
            agent_api_key_env,
            env,
            agent_reasoning_effort or str(seed_agent.get("reasoning_effort") or "medium"),
        )
        pricing, _pricing_path = _pricing_identity(root, model)
        if context_length > pricing["max_input_tokens"]:
            raise PreparationError(
                f"declared context length {context_length} exceeds {model} pricing capacity "
                f"{pricing['max_input_tokens']}"
            )
    else:
        pricing, _pricing_path = _pricing_identity(root, str(agent.get("model") or ""))
    judge = _enforce_judge_parallelism(
        _resolve_judge_identity(env), configurations,
    )
    run_id = _safe_name(
        f"dolphinbench-tests-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"
    )
    output_root.mkdir(parents=True, exist_ok=False)
    try:
        input_paths: list[Path] = [
            root / "harness" / "benchmark_soul.md",
            root / "harness" / "environment.py",
            root / "harness" / "run_simulation.py",
            root / "harness" / "hermes_driver.py",
            root / "harness" / "claude_driver.py",
            root / "harness" / "memory_capture.py",
            root / "harness" / "costing.py",
            root / "graders" / "mechanical.py",
            root / "graders" / "llm_judge.py",
            root / "mock_mcp" / "server.py",
            root / 'reference' / 'runtimes' / 'hermes.py',
            root / "harness" / "adapters" / "hermes_profile.py",
            root / 'reference' / 'evaluate.py',
            root / "pricing" / "latest.json",
            seed_manifest_path,
            seed_state_path,
        ]
        if published_test_runtime is not None and runtime_tests is not None:
            input_paths.append(Path(published_test_runtime["source_manifest"]))
            input_paths.extend(
                Path(entry[path_key])
                for entry in runtime_tests.values()
                for path_key in ("workspace", "tool_config")
            )
        personas = tuple(str(persona) for persona in seed_manifest.get("personas") or [])
        if not personas:
            raise PreparationError("completed seed manifest has no personas")
        persona_inputs: dict[str, Any] = {}
        jobs: list[dict[str, Any]] = []
        for persona in personas:
            tests = _test_files(root, persona, published_tests_directory)
            release_manifest_path, release_manifest = _load_release_manifest(root, persona)
            checkpoint = Path(
                (seed_manifest.get("persona_inputs") or {}).get(persona, {}).get(
                    "checkpoint_dir", ""
                )
            )
            if not checkpoint:
                raise PreparationError(f"{persona}: seed manifest has no checkpoint")
            validated = _validate_checkpoint(persona, checkpoint, tests, release_manifest)
            selected_paths = [path for path in tests if path.stem in set(test_ids)]
            if len(selected_paths) != len(test_ids):
                missing = sorted(set(test_ids) - {path.stem for path in selected_paths})
                raise PreparationError(f"{persona}: unknown test ids: {missing}")
            mock_manifest_path = root / "mock_mcp" / "manifests" / f"{persona}.yaml"
            mock_manifest = _load_yaml(mock_manifest_path)
            simulated_app_tools = [str(tool) for tool in mock_manifest.get("tools", []) or []]
            if not simulated_app_tools or len(simulated_app_tools) != len(set(simulated_app_tools)):
                raise PreparationError(
                    f"{persona}: simulated app tool manifest is empty or contains duplicates"
                )
            persona_source = (seed_manifest.get("persona_inputs") or {}).get(persona) or {}
            numeric_path = Path(persona_source.get("numeric_simulation", ""))
            if not numeric_path.is_file():
                raise PreparationError(f"{persona}: seed simulation is missing: {numeric_path}")
            input_paths.extend([
                *list(_iter_tree_files(checkpoint)),
                *tests,
                release_manifest_path,
                root / "registry" / "personas.yaml",
                mock_manifest_path,
                root / "mock_mcp" / "state" / f"{persona}_baseline.json",
                numeric_path,
            ])
            persona_inputs[persona] = {
                "checkpoint_dir": str(checkpoint.resolve()),
                "numeric_simulation": str(numeric_path.resolve()),
                "validation": {key: value for key, value in validated.items() if key != "life_sim"},
                "simulated_app_tools": simulated_app_tools,
                "test_ids": list(test_ids),
                "source_seed_manifest": str(seed_manifest_path),
                "published_test_runtime": (
                    {
                        "source_manifest": published_test_runtime["source_manifest"],
                        "source_manifest_sha256": published_test_runtime["source_manifest_sha256"],
                        "mock_mcp_root": published_test_runtime["mock_mcp_root"],
                    }
                    if published_test_runtime is not None
                    else None
                ),
            }
            for configuration in configurations:
                source_job = source_jobs[f"{configuration}-{persona}"]
                run_dir = output_root / "jobs" / target_agent_runtime / configuration / persona
                job_id = f"{configuration}-{persona}"
                provider_identity = dict(source_job["provider_identity"])
                job = {
                    "id": job_id,
                    "run_id": f"{run_id}-{configuration}-{persona}",
                    "persona": persona,
                    "agent_runtime": target_agent_runtime,
                    "configuration": configuration,
                    "provider": source_job["provider"],
                    "profile": source_job["profile"],
                    "source_seed_profile": source_job["profile"],
                    "toolsets": list(source_job["toolsets"]),
                    "provider_identity": provider_identity,
                    "source_seed_provider_identity": dict(source_job["provider_identity"]),
                    "gateway_config": {},
                    "runtime_config_dir": str(run_dir / "runtime-config"),
                    "run_dir": str(run_dir),
                    "state_path": source_job["state_path"],
                    "log_path": source_job["log_path"],
                    "tool_config_path": source_job["tool_config_path"],
                    "result_path": str(run_dir / "results.json"),
                    "ledger_path": str(run_dir / "cost_ledger.jsonl"),
                    "profile_soul_path": str(run_dir / "profile_soul.md"),
                    "sim_path": str(numeric_path.resolve()),
                    "tests_dir": str(tests[0].parent.resolve()),
                    "prefill_path": None,
                    "test_runtime_path": str(run_dir / "test_runtime.json"),
                    "test_runtime": (
                        {
                            "version": 1,
                            "mock_mcp_root": published_test_runtime["mock_mcp_root"],
                            "mock_mcp_entrypoint": published_test_runtime["mock_mcp_entrypoint"],
                            "mock_manifest": str(runtime_mock_root / "manifests" / f"{persona}.yaml"),
                            "tests": runtime_tests,
                        }
                        if published_test_runtime is not None and runtime_tests is not None
                        else None
                    ),
                    "seed_profile_config_hashes": next(
                        item["profile_hashes"]
                        for item in selected_jobs
                        if item["source"]["id"] == job_id
                    ),
                    "seed_result_paths": [str(path) for path in next(
                        item["result_paths"]
                        for item in selected_jobs
                        if item["source"]["id"] == job_id
                    )],
                }
                jobs.append(job)

        runtime_provenance, source_trees, provenance_files = _runtime_provenance(
            hermes_source, configurations, env,
        )
        if runtime_mock_root is not None:
            source_trees["published_test_mock_mcp"] = _snapshot_tree(runtime_mock_root)
        input_paths.extend(provenance_files)
        source_artifact_paths = [
            Path(path)
            for job in jobs
            for path in job["seed_result_paths"]
        ]
        input_paths.extend(source_artifact_paths)
        file_hashes = _snapshot_files(input_paths)
        source_seed_artifacts = {
            "manifest": str(seed_manifest_path),
            "state": str(seed_state_path),
            "jobs": {
                job["id"]: {
                    "profile": job["profile"],
                    "source_seed_profile": job["source_seed_profile"],
                    "profile_config_hashes": job["seed_profile_config_hashes"],
                    "result_paths": job["seed_result_paths"],
                    "result_hashes": {
                        path: file_hashes[str(Path(path).resolve())]
                        for path in job["seed_result_paths"]
                    },
                }
                for job in jobs
            },
        }
        hermes_bin, hermes_python = _hermes_executables(hermes_source)
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "prepared_at": _now(),
            "run_id": run_id,
            "phase": "prepared_offline",
            "root": str(root),
            "output_root": str(output_root),
            "agent_runtime": target_agent_runtime,
            "agent": agent,
            "judge": judge,
            "pricing": pricing,
            "runtime_provenance": runtime_provenance,
            "personas": list(personas),
            "configurations": list(configurations),
            "shared_backend_reuse": {
                "allow_cross_agent": False,
                "provider_identity": None,
            },
            "evaluation_scope": {
                "kind": "test_only",
                "test_ids": list(test_ids),
                "scores_are_publishable": True,
                "memory_ingestion_runtime": source_agent_runtime,
                "outer_agent_runtime": target_agent_runtime,
                "profiles_are_reused": True,
            },
            "source_seed_artifacts": source_seed_artifacts,
            "persona_inputs": persona_inputs,
            "jobs": jobs,
            "hashes": {"files": file_hashes, "source_trees": source_trees},
            "launch": {
                "canonical_runner": str((root / "harness" / "run_simulation.py").resolve()),
                "hermes_bin": str(hermes_bin),
                "hermes_python": str(hermes_python),
                "mock_mcp_python": sys.executable,
                "profile_creation": "reused_from_completed_seed",
                "provider_creation": "not_performed",
                "runner_mode": "test_only_skip_seeding",
                "runner_resume_contract": {"interrupted_phase": "--resume"},
            },
        }
        manifest_path = output_root / "launch_manifest.json"
        _write_json(manifest_path, manifest)
        return manifest_path
    except Exception:
        import shutil
        shutil.rmtree(output_root, ignore_errors=True)
        raise


def prepare_claude_native_memory_test_only(
    *,
    seed_receipt_path: Path,
    output_root: Path,
    test_ids: tuple[str, ...],
    model: str,
    root: Path = ROOT,
    env: dict[str, str] | None = None,
) -> Path:
    """Prepare one Claude built-in-memory test-only manifest from a frozen seed.

    Unlike the external providers, Claude's own auto-memory is not a Hermes
    profile or an MCP service.  Its completed receipt and credential-free
    archive are the seed.  The worker restores that archive before the runner
    creates one disposable copy per test.
    """
    from reference.runtimes import claude_code as native_memory

    root = root.resolve()
    seed_receipt_path = seed_receipt_path.resolve()
    output_root = output_root.resolve()
    if output_root.exists():
        raise PreparationError(f"output root already exists; each run needs a new path: {output_root}")
    if not test_ids:
        raise PreparationError("select at least one test id")
    receipt = native_memory.validate_completed_seed(seed_receipt_path)
    corpus = receipt["corpus"]
    expected_corpus = (root / "registry" / "personas" / "morgan" / "life_sim.yaml").resolve()
    if Path(str(corpus["path"])).resolve() != expected_corpus:
        raise PreparationError("Claude native-memory receipt does not point to Morgan's frozen final corpus")
    snapshot = receipt["snapshot"]
    archive_reference = Path(str(snapshot["path"]))
    archive_path = (
        archive_reference.resolve()
        if archive_reference.is_absolute()
        else (seed_receipt_path.parent / archive_reference).resolve()
    )
    if not archive_path.is_file():
        raise PreparationError("Claude native-memory receipt references a missing snapshot archive")
    persona = "morgan"
    tests = _test_files(root, persona)
    selected_paths = [path for path in tests if path.stem in set(test_ids)]
    if len(selected_paths) != len(test_ids):
        missing = sorted(set(test_ids) - {path.stem for path in selected_paths})
        raise PreparationError(f"morgan: unknown test ids: {missing}")
    env = dict(env) if env is not None else load_environment(HERMES_HOME / ".env")
    agent = _subscription_agent_identity("claude", model)
    pricing, _pricing_path = _subscription_pricing_identity(root, model)
    judge = _enforce_judge_parallelism(_resolve_judge_identity(env), ("builtin",))
    run_id = _safe_name(f"dolphinbench-claude-native-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}")
    output_root.mkdir(parents=True, exist_ok=False)
    try:
        simulation = output_root / "simulations" / persona / "test_only.yaml"
        simulation.parent.mkdir(parents=True, exist_ok=True)
        simulation.write_text(yaml.safe_dump({
            "name": "Morgan Claude native auto-memory evaluation",
            "description": "Test-only evaluation over a separately frozen Claude native-memory seed.",
            "sessions": [],
            "test_queries": [{"id": path.stem} for path in selected_paths],
        }, sort_keys=False), encoding="utf-8")
        run_dir = output_root / "jobs" / "claude" / "builtin" / persona
        native_identity = {
            "kind": "claude_native_auto_memory",
            "persona": persona,
            "fixed_project_dir": native_memory.FIXED_PROJECT_DIR,
            "source_ids_sha256": corpus["source_ids_sha256"],
            "snapshot_sha256": snapshot["sha256"],
        }
        job = {
            "id": "builtin-morgan",
            "run_id": f"{run_id}-builtin-morgan",
            "persona": persona,
            "agent_runtime": "claude",
            "configuration": "builtin",
            "provider": "claude_native_memory",
            "profile": None,
            "source_seed_profile": None,
            "toolsets": ["dolphinbench-apps", "claude-native-memory"],
            "provider_identity": native_identity,
            "source_seed_provider_identity": native_identity,
            "gateway_config": {},
            "runtime_config_dir": str(run_dir / "runtime-config"),
            "run_dir": str(run_dir),
            "state_path": str(run_dir / "state.json"),
            "log_path": str(run_dir / "calls.jsonl"),
            "tool_config_path": str(run_dir / "tool_config.json"),
            "result_path": str(run_dir / "results.json"),
            "ledger_path": str(run_dir / "cost_ledger.jsonl"),
            "profile_soul_path": str(run_dir / "profile_soul.md"),
            "sim_path": str(simulation),
            "prefill_path": None,
            "seed_profile_config_hashes": None,
            "seed_result_paths": [str(seed_receipt_path)],
            "native_memory": {
                "receipt_path": str(seed_receipt_path),
                "receipt_sha256": _sha256(seed_receipt_path),
                "archive_path": str(archive_path),
                "archive_sha256": snapshot["sha256"],
                "fixed_project_dir": native_memory.FIXED_PROJECT_DIR,
            },
        }
        input_paths = [
            root / "registry" / "personas.yaml",
            root / "harness" / "benchmark_soul.md",
            root / "harness" / "environment.py",
            root / "harness" / "run_simulation.py",
            root / "harness" / "claude_driver.py",
            root / 'reference' / 'runtimes' / 'claude_code.py',
            root / "harness" / "claude_native_read_guard.py",
            root / 'reference' / 'evaluate.py',
            root / "graders" / "mechanical.py",
            root / "graders" / "llm_judge.py",
            root / "mock_mcp" / "server.py",
            root / "mock_mcp" / "manifests" / "morgan.yaml",
            root / "mock_mcp" / "state" / "morgan_baseline.json",
            *selected_paths,
            seed_receipt_path,
            simulation,
            root / "pricing" / "latest.json",
        ]
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "prepared_at": _now(),
            "run_id": run_id,
            "phase": "prepared_offline",
            "root": str(root),
            "output_root": str(output_root),
            "agent_runtime": "claude",
            "agent": agent,
            "judge": judge,
            "pricing": pricing,
            "runtime_provenance": {"claude_native_memory": {
                "receipt_sha256": _sha256(seed_receipt_path),
                "archive_sha256": snapshot["sha256"],
            }},
            "personas": [persona],
            "configurations": ["builtin"],
            "shared_backend_reuse": {"allow_cross_agent": False, "provider_identity": None},
            "evaluation_scope": {
                "kind": "test_only",
                "test_ids": list(test_ids),
                "scores_are_publishable": True,
                "memory_ingestion_runtime": "claude",
                "outer_agent_runtime": "claude",
                "profiles_are_reused": False,
                "native_memory_is_copied_per_test": True,
            },
            "source_seed_artifacts": {"claude_native_memory": {
                "receipt": str(seed_receipt_path),
                "receipt_sha256": _sha256(seed_receipt_path),
                "archive": str(archive_path),
                "archive_sha256": snapshot["sha256"],
            }},
            "persona_inputs": {persona: {
                "numeric_simulation": str(simulation),
                "test_ids": list(test_ids),
                "source_seed_receipt": str(seed_receipt_path),
            }},
            "jobs": [job],
            "hashes": {"files": _snapshot_files(input_paths), "source_trees": {}},
            "launch": {
                "canonical_runner": str((root / "harness" / "run_simulation.py").resolve()),
                "hermes_bin": str(HERMES_BIN),
                "hermes_python": str(HERMES_PYTHON),
                "mock_mcp_python": sys.executable,
                "profile_creation": "not_used_by_claude_native_memory",
                "provider_creation": "not_performed",
                "runner_mode": "test_only_skip_seeding",
                "runner_resume_contract": {"interrupted_phase": "--resume"},
            },
        }
        manifest_path = output_root / "launch_manifest.json"
        _write_json(manifest_path, manifest)
        return manifest_path
    except Exception:
        import shutil
        shutil.rmtree(output_root, ignore_errors=True)
        raise


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestDriftError(f"cannot read launch manifest {path}: {exc}") from exc
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise ManifestDriftError("unsupported launch manifest version")
    if manifest.get("phase") != "prepared_offline":
        raise ManifestDriftError("launch requires an unlaunched prepared manifest")
    return manifest


def verify_manifest_hashes(manifest: dict[str, Any]) -> None:
    drift: list[str] = []
    for raw_path, expected in (manifest.get("hashes") or {}).get("files", {}).items():
        path = Path(raw_path)
        if not path.is_file():
            drift.append(f"missing {path}")
        elif _sha256(path) != expected:
            drift.append(f"changed {path}")
    for name, expected in (manifest.get("hashes") or {}).get("source_trees", {}).items():
        root = Path(expected["root"])
        if not root.is_dir():
            drift.append(f"missing {name} source tree {root}")
            continue
        for raw_path, expected_hash in (expected.get("files") or {}).items():
            path = Path(raw_path)
            if not path.is_file():
                drift.append(f"missing {name} source file {path}")
            elif _sha256(path) != expected_hash:
                drift.append(f"changed {name} source file {path}")
        actual = _snapshot_tree(
            root, tuple(expected.get("ignored_part_prefixes") or ()),
        )
        if expected.get("relocated_for_bundle"):
            expected_inside = {
                raw_path: expected_hash
                for raw_path, expected_hash in (expected.get("files") or {}).items()
                if Path(raw_path).is_relative_to(root)
            }
            if actual["files"] != expected_inside:
                drift.append(f"changed {name} source tree {root}")
                continue
            digest = hashlib.sha256()
            for raw_path, expected_hash in sorted((expected.get("files") or {}).items()):
                digest.update(raw_path.encode())
                digest.update(b"\0")
                digest.update(expected_hash.encode())
                digest.update(b"\0")
            if digest.hexdigest() != expected.get("sha256"):
                drift.append(f"changed {name} source tree record {root}")
        elif actual["sha256"] != expected.get("sha256"):
            drift.append(f"changed {name} source tree {root}")
    if drift:
        raise ManifestDriftError("launch manifest hash drift: " + "; ".join(drift))


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    _write_json(temporary, payload)
    os.replace(temporary, path)


def _parse_test_ids(raw: str) -> tuple[str, ...]:
    if raw.strip().lower() == "all":
        return tuple(f"{index:03d}" for index in range(1, 201))
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not values:
        raise PreparationError("select at least one test id")
    normalized: list[str] = []
    for value in values:
        try:
            number = int(value)
        except ValueError as exc:
            raise PreparationError(f"invalid test id {value!r}") from exc
        if number < 1 or number > 200:
            raise PreparationError(f"test id {value!r} is outside 001 through 200")
        normalized.append(f"{number:03d}")
    if len(normalized) != len(set(normalized)):
        raise PreparationError("test ids contain duplicates")
    return tuple(normalized)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = percentile / 100 * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _result_test_ids(path: Path) -> set[str]:
    """Read the durable test prefix written by the canonical runner."""
    if not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestDriftError(f"cannot read runner result {path}: {exc}") from exc
    tests = payload.get("test_results") or []
    if not isinstance(tests, list):
        raise ManifestDriftError(f"runner result has invalid test_results: {path}")
    ids: set[str] = set()
    for test in tests:
        if not isinstance(test, dict):
            raise ManifestDriftError(f"runner result has invalid test entry: {path}")
        test_id = str(test.get("source_id") or test.get("test_id") or "")
        if not re.fullmatch(r"[0-9]{3}", test_id):
            raise ManifestDriftError(f"runner result has invalid test id {test_id!r}: {path}")
        if test_id in ids:
            raise ManifestDriftError(f"runner result repeats test {test_id}: {path}")
        ids.add(test_id)
    return ids


def _expected_seed_session_ids(job: dict[str, Any]) -> set[str]:
    try:
        simulation = _load_yaml(Path(job["sim_path"]))
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestDriftError(f"cannot read seed simulation for {job['id']}: {exc}") from exc
    sessions = simulation.get("sessions") or []
    if not isinstance(sessions, list):
        raise ManifestDriftError(f"seed simulation has invalid sessions for {job['id']}")
    ids = {str(session.get("id") or "") for session in sessions if isinstance(session, dict)}
    if "" in ids or len(ids) != len(sessions):
        raise ManifestDriftError(f"seed simulation has missing or duplicate session ids for {job['id']}")
    return ids


def _result_has_complete_seed(job: dict[str, Any], path: Path) -> bool:
    """Only reuse a profile after the runner durably recorded all seed sessions."""
    expected = _expected_seed_session_ids(job)
    if not expected:
        return True
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestDriftError(f"cannot read runner result {path}: {exc}") from exc
    sessions = payload.get("seed_sessions") or []
    if not isinstance(sessions, list):
        return False
    observed = {
        str(session.get("id") or "")
        for session in sessions
        if isinstance(session, dict)
    }
    return observed == expected and len(sessions) == len(expected)


def _result_has_completed_ingestion(job: dict[str, Any], path: Path) -> bool:
    """Require all inputs plus the provider's end-of-ingestion processing."""
    if not _result_has_complete_seed(job, path):
        return False
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestDriftError(f"cannot read runner result {path}: {exc}") from exc
    return bool(payload.get("phase1_ended_at"))


def _job_result_paths(state: dict[str, Any], job_id: str) -> list[Path]:
    return [
        Path(phase_job["result_path"])
        for phase in state.get("phases") or []
        for phase_job in phase.get("jobs") or []
        if phase_job.get("id") == job_id and phase_job.get("result_path")
    ]


def _completed_tests_for_job(state: dict[str, Any], job_id: str) -> set[str]:
    completed: set[str] = set()
    for result_path in _job_result_paths(state, job_id):
        overlap = completed & _result_test_ids(result_path)
        if overlap:
            raise ManifestDriftError(
                f"{job_id}: duplicate durable test results across phases: {sorted(overlap)}"
            )
        completed.update(_result_test_ids(result_path))
    return completed


def _seed_is_complete_for_job(state: dict[str, Any], job: dict[str, Any]) -> bool:
    return any(
        _result_has_completed_ingestion(job, path)
        for path in _job_result_paths(state, job["id"])
    )


def _completed_tests_for_all_jobs(manifest: dict[str, Any], state: dict[str, Any]) -> set[str]:
    per_job = [_completed_tests_for_job(state, job["id"]) for job in manifest["jobs"]]
    return set.intersection(*per_job) if per_job else set()


def _judge_environment(judge: dict[str, Any]) -> dict[str, str]:
    """Make the runner's judge process match the saved manifest exactly."""
    values = {
        "DOLPHINBENCH_JUDGE_BACKEND": str(judge["backend"]),
        "DOLPHINBENCH_JUDGE_REASONING_EFFORT": str(judge["reasoning_effort"]),
        "DOLPHINBENCH_JUDGE_MAX_TOKENS": str(judge["max_tokens"]),
        "DOLPHINBENCH_JUDGE_TIMEOUT": str(judge["timeout_seconds"]),
        "DOLPHINBENCH_JUDGE_MAX_RETRIES": str(judge["max_retries"]),
        "DOLPHINBENCH_JUDGE_PARALLELISM": str(judge["parallelism_per_test"]),
        "DOLPHINBENCH_GRADING_WORKERS": str(judge.get("concurrent_test_grades", 1)),
    }
    if judge["backend"] == "azure":
        values.update({
            "DOLPHINBENCH_JUDGE_DEPLOYMENT": str(judge["deployment"]),
            "DOLPHINBENCH_JUDGE_API_VERSION": str(judge["api_version"]),
            "AZURE_OPENAI_ENDPOINT": str(judge["endpoint"]),
        })
    else:
        values.update({
            "DOLPHINBENCH_JUDGE_MODEL": str(judge["model"]),
            "DOLPHINBENCH_JUDGE_ENDPOINT": str(judge["endpoint"]),
            "DOLPHINBENCH_JUDGE_KEY_ENV": str(judge["api_key_env"]),
        })
    return values


def _write_combined_results(manifest: dict[str, Any], state: dict[str, Any]) -> None:
    """Combine seed accounting and disjoint test phases for each job."""
    for job in manifest["jobs"]:
        phase_payloads: list[dict[str, Any]] = []
        for phase in state["phases"]:
            phase_job = next(item for item in phase["jobs"] if item["id"] == job["id"])
            result_path = Path(phase_job["result_path"])
            # A failed process can still leave a valid prefix of seed calls
            # and tests.  Resume schedules only the missing suffix, so both
            # parts belong in the final per-provider record.
            if phase_job.get("status") in {"completed", "failed"} and result_path.is_file():
                phase_payloads.append(json.loads(result_path.read_text()))
        if not phase_payloads:
            continue

        seed_calls: list[dict[str, Any]] = []
        tests: list[dict[str, Any]] = []
        seen: set[str] = set()
        for payload in phase_payloads:
            if payload.get("seed_calls"):
                if seed_calls:
                    raise RuntimeError(f"{job['id']}: more than one phase contains seeding")
                seed_calls = list(payload["seed_calls"])
            for test in payload.get("test_results") or []:
                test_id = str(test.get("source_id") or test.get("test_id"))
                if test_id in seen:
                    raise RuntimeError(f"{job['id']}: test {test_id} ran more than once")
                seen.add(test_id)
                tests.append(test)

        seed_costs = [call.get("cost_usd") for call in seed_calls]
        test_costs = [test.get("cost_usd") for test in tests]
        costs_complete = all(value is not None for value in [*seed_costs, *test_costs])
        seed_cost = sum(float(value) for value in seed_costs if value is not None)
        test_cost = sum(float(value) for value in test_costs if value is not None)
        latencies = [float(test["latency_seconds"]) for test in tests if test.get("latency_seconds") is not None]
        passes = sum(bool(test.get("passed")) for test in tests)
        combined = {
            "manifest": state["manifest"],
            "job_id": job["id"],
            "persona": job["persona"],
            "configuration": job["configuration"],
            "provider": job["provider"],
            "phase_result_paths": [
                str(Path(phase_job["result_path"]))
                for phase in state["phases"]
                for phase_job in phase["jobs"]
                if phase_job["id"] == job["id"]
                and phase_job.get("status") in {"completed", "failed"}
                and Path(phase_job["result_path"]).is_file()
            ],
            "seed_calls": seed_calls,
            "test_results": tests,
            "summary": {
                "passes": passes,
                "total": len(tests),
                "pass_rate": passes / len(tests) if tests else 0,
                "complete_200_test_run": len(tests) == 200,
                "total_cost_usd": seed_cost + test_cost if costs_complete else None,
                "total_cost_usd_seed_calls": seed_cost,
                "total_cost_usd_test_calls": test_cost,
                "cost_unavailable_count": sum(value is None for value in [*seed_costs, *test_costs]),
                "agent_inference_cost_scope": (
                    "Token-priced agent inference only; excludes hosted memory-provider, "
                    "Honcho, plugin, and other provider service fees."
                ),
                "median_latency_seconds": statistics.median(latencies) if latencies else None,
                "p95_latency_seconds": _percentile(latencies, 95),
            },
        }
        _write_json(Path(job["run_dir"]) / "combined_results.json", combined)


def _phase_requested_test_ids(phase: dict[str, Any]) -> tuple[str, ...]:
    """Read the fixed test scope of one saved phase."""
    raw = phase.get("requested_test_ids", phase.get("test_ids"))
    if not isinstance(raw, list):
        raise ManifestDriftError("saved phase has no requested test ids")
    if not raw and phase.get("operation") != "seed_only":
        raise ManifestDriftError("saved test phase has no requested test ids")
    return tuple(str(test_id) for test_id in raw)


def _phase_completed(phase: dict[str, Any]) -> bool:
    """A later disjoint phase is safe only after every job ended cleanly."""
    jobs = phase.get("jobs") or []
    return bool(
        phase.get("ended_at")
        and phase.get("failure_count") == 0
        and jobs
        and all(job.get("status") in {"completed", "already_completed"} for job in jobs)
    )


def launch_matrix(
    manifest_path: Path,
    concurrency: int,
    test_ids: tuple[str, ...],
    *,
    resume: bool,
    seed_only: bool = False,
) -> int:
    """Run ingestion only or one test phase from a hash-bound manifest."""
    if concurrency < 1 or concurrency > 4:
        raise PreparationError("concurrency must be between 1 and 4")
    if seed_only and test_ids:
        raise PreparationError("an ingestion-only run cannot include test ids")
    if not seed_only and not test_ids:
        raise PreparationError("a test run requires at least one test id")
    manifest_path = manifest_path.resolve()
    manifest = load_manifest(manifest_path)
    verify_manifest_hashes(manifest)
    # Do this before writing launch state or importing the profile creator.
    _assert_manifest_launch_wiring(manifest)
    scope = manifest.get("evaluation_scope") or {"kind": "final_corpus"}
    test_only = scope.get("kind") == "test_only"
    if test_only and seed_only:
        raise PreparationError("a test-only manifest cannot run ingestion")
    if not seed_only and scope.get("kind") == "diagnostic_smoke":
        expected_ids = tuple(scope.get("test_ids") or [])
        if test_ids != expected_ids:
            raise ManifestDriftError(
                "a diagnostic smoke must run exactly its prepared test ids: "
                f"{','.join(expected_ids)}"
            )
    output_root = Path(manifest["output_root"])
    launch_state_path = output_root / "launch_state.json"
    if resume and not launch_state_path.exists():
        raise ManifestDriftError("resume requires an earlier launch phase")
    if not resume and launch_state_path.exists():
        raise ManifestDriftError(
            f"manifest has already entered launch and cannot reuse profiles or provider identities: {launch_state_path}"
        )
    if resume:
        state = json.loads(launch_state_path.read_text())
        if state.get("manifest") != str(manifest_path):
            raise ManifestDriftError("launch state belongs to another manifest")
        if not state.get("phases"):
            raise ManifestDriftError("resume requires an earlier launch phase")
    else:
        state = {
            "manifest": str(manifest_path),
            "started_at": _now(),
            "phases": [],
            "profiles": {},
        }

    in_place_resume = False
    if resume and not _phase_completed(state["phases"][-1]):
        phase = state["phases"][-1]
        expected_operation = (
            "seed_only" if seed_only else "test_only" if test_only else "tests"
        )
        if phase.get("operation", "tests") != expected_operation:
            raise ManifestDriftError(
                f"interrupted {phase.get('operation', 'tests')} phase cannot resume as "
                f"{expected_operation}"
            )
        existing_ids = _phase_requested_test_ids(phase)
        if test_ids != existing_ids:
            raise ManifestDriftError(
                "an interrupted phase must resume with exactly its original test ids: "
                f"{','.join(existing_ids)}"
            )
        in_place_resume = True
        phase["resume_started_at"] = _now()
        phase["resume_runner_flag"] = "--resume"
    else:
        completed_for_all = _completed_tests_for_all_jobs(manifest, state)
        overlap = sorted(completed_for_all & set(test_ids))
        if overlap:
            raise ManifestDriftError(f"tests already completed by every provider: {overlap}")
        phase_index = len(state["phases"]) + 1
        phase = {
            "index": phase_index,
            "operation": "seed_only" if seed_only else "test_only" if test_only else "tests",
            "mode": "later_disjoint_phase" if resume else "initial",
            "requested_test_ids": list(test_ids),
            "started_at": _now(),
            "jobs": [
                {
                    "id": job["id"],
                    "status": "queued",
                    "result_path": str(Path(job["run_dir"]) / f"results_phase_{phase_index:02d}.json"),
                    "requested_test_ids": list(test_ids),
                }
                for job in manifest["jobs"]
            ],
        }
        state["phases"].append(phase)
    state_jobs = {job["id"]: job for job in phase["jobs"]}
    lock = threading.Lock()
    honcho_slot = threading.Lock()
    _atomic_json(launch_state_path, state)

    def execute(job: dict[str, Any]) -> int:
        current = state_jobs[job["id"]]
        with lock:
            current.update({"status": "starting", "started_at": _now()})
            _atomic_json(launch_state_path, state)
        runtime = str(job.get("agent_runtime") or _manifest_agent_runtime(manifest))
        if runtime == "hermes":
            from reference.runtimes.hermes import create_profile_from_manifest
            from reference.runtimes.hermes import profile_config_hashes

            profile_dir = HERMES_HOME / "profiles" / job["profile"]
        else:
            profile_dir = None
            if not test_only:
                raise RuntimeError(f"{job['id']}: {runtime} supports test-only runs only")
        if test_only and runtime == "hermes":
            if not profile_dir.is_dir():
                raise RuntimeError(f"seeded profile is missing: {profile_dir}")
            expected_hashes = job.get("seed_profile_config_hashes")
            if not expected_hashes:
                raise RuntimeError(f"{job['id']}: test-only manifest has no seeded profile hashes")
            actual_hashes = profile_config_hashes(profile_dir)
            if not _profile_hashes_match(expected_hashes, actual_hashes):
                raise RuntimeError(
                    f"{job['id']}: seeded profile configuration changed after test preparation"
                )
            with lock:
                state.setdefault("profiles", {})[job["id"]] = actual_hashes
                current["profile_config_hashes"] = actual_hashes
                _atomic_json(launch_state_path, state)
            if not in_place_resume:
                _write_test_only_runtime_files(job)
        elif test_only:
            if not in_place_resume:
                _write_test_only_runtime_files(job)
        elif resume:
            if not profile_dir.is_dir():
                raise RuntimeError(f"seeded profile is missing: {profile_dir}")
            if not in_place_resume and not _seed_is_complete_for_job(state, job):
                raise RuntimeError(
                    f"{job['id']}: cannot resume because seeding did not finish; "
                    "start a new disposable manifest rather than risking duplicate ingestion"
                )
            expected_hashes = (state.get("profiles") or {}).get(job["id"])
            actual_hashes = profile_config_hashes(profile_dir)
            if expected_hashes != actual_hashes:
                raise RuntimeError(
                    f"{job['id']}: seeded profile configuration changed after launch; refusing resume"
                )
            current["profile_config_hashes"] = actual_hashes
        else:
            hashes = create_profile_from_manifest(manifest, job)
            with lock:
                state.setdefault("profiles", {})[job["id"]] = hashes
                current["profile_config_hashes"] = hashes
                _atomic_json(launch_state_path, state)

        completed_for_job = _completed_tests_for_job(state, job["id"])
        remaining_ids = tuple(test_id for test_id in test_ids if test_id not in completed_for_job)
        current_result_path = Path(current["result_path"])
        work_is_complete = (
            _result_has_complete_seed(job, current_result_path)
            if seed_only
            else not remaining_ids and (
                not in_place_resume or _result_has_complete_seed(job, current_result_path)
            )
        )
        if work_is_complete:
            with lock:
                current.update({
                    "status": "already_completed",
                    "completed_test_ids": sorted(completed_for_job & set(test_ids)),
                    "ended_at": _now(),
                })
                _atomic_json(launch_state_path, state)
            return 0
        env = load_environment(HERMES_HOME / ".env")
        if job.get("tests_dir"):
            env["DOLPHINBENCH_TESTS_DIR"] = job["tests_dir"]
        if job.get("test_runtime") is not None:
            env["DOLPHINBENCH_TEST_RUNTIME_PATH"] = job["test_runtime_path"]
        if runtime == "hermes":
            container_image = manifest.get("runtime_provenance", {}).get("agent_container", {}).get("image")
            from reference.execution.agent_container import image_backend
            if not container_image:
                raise RuntimeError("Prepare a new configuration with DOLPHINBENCH_AGENT_IMAGE; do not change an existing run in place")
            image_backend(container_image)
            if env.get("DOLPHINBENCH_AGENT_IMAGE") != container_image:
                raise RuntimeError("The agent container image differs from the prepared configuration")
            env["HERMES_BIN"] = str(manifest["launch"]["hermes_bin"])
        if runtime == "hermes":
            agent_key_env = manifest["agent"].get("api_key_env")
            agent_key = env.get(agent_key_env or "", "")
            if not agent_key:
                raise RuntimeError(
                    f"missing agent API key environment variable {agent_key_env}"
                )
            # Hermes resolves custom-endpoint credentials from OPENAI_API_KEY.
            # Keep the value in the child process environment and out of profile
            # configuration and saved artifacts.
            env["OPENAI_API_KEY"] = agent_key
            env["CUSTOM_API_KEY"] = agent_key
            if manifest["agent"]["provider"] == "azure-foundry":
                env["AZURE_FOUNDRY_API_KEY"] = agent_key
            env.update({
                "DOLPHINBENCH_HERMES_PROFILE": job["profile"],
                "DOLPHINBENCH_HERMES_TOOLSETS": ",".join(job["toolsets"]),
                "ENACT_STATE_PATH": job["state_path"],
                "ENACT_LOG_PATH": job["log_path"],
                "ENACT_TOOL_CONFIG_PATH": job["tool_config_path"],
                "DOLPHINBENCH_COST_LEDGER_PATH": job["ledger_path"],
                "DOLPHINBENCH_KEEP_HERMES_TEST_PROFILES": "0",
            })
        env.update(_judge_environment(manifest["judge"]))
        identity = job["provider_identity"]
        if runtime == "hermes" and job["configuration"] == "mem0":
            env.pop("MEM0_AGENT_ID", None)
            env.update({
                "MEM0_USER_ID": identity["user_id"],
                "ENACT_NARRATIVE_TIMEZONE": identity["narrative_timezone"],
                "DOLPHINBENCH_MEM0_FORCE_ENV_API_KEY": "1",
                "DOLPHINBENCH_MEM0_SYNC_WRITES": "1",
                "DOLPHINBENCH_MEM0_WAIT_EVENTS_AT_END": "1",
                "DOLPHINBENCH_MEM0_EVENT_BARRIER_TIMEOUT": "7200",
                "DOLPHINBENCH_MEM0_EVENT_POLL": "1.5",
            })
        elif runtime == "hermes" and job["configuration"] == "honcho":
            env.update({
                "HONCHO_WORKSPACE_ID": identity["workspace"],
                "HONCHO_PEER_NAME": identity["peer_name"],
            })
        command = [
            sys.executable, "-u", manifest["launch"]["canonical_runner"],
            "--persona", job["persona"],
            "--provider", job["provider"],
            "--out", current["result_path"],
            "--model", manifest["agent"]["model"],
            "--sim", job["sim_path"],
            "--agent-runtime", runtime,
        ]
        if runtime == "hermes":
            command.extend(["--capture-memory", "--read-only-test-memory", "--isolate-hermes-test-sessions"])
        else:
            command.extend([
                "--runtime-config-dir", job["runtime_config_dir"],
                "--memory-provider-identity-path", str(Path(job["run_dir"]) / "memory_provider_identity.json"),
                "--memory-gateway-python", manifest["launch"]["hermes_python"],
                "--mock-mcp-python", manifest["launch"]["mock_mcp_python"],
                "--runtime-run-id", job["run_id"],
                "--no-capture-memory",
            ])
        if seed_only:
            command.append("--seed-only")
        else:
            command.extend([
                "--only", ",".join(test_ids if in_place_resume else remaining_ids),
            ])
        if in_place_resume:
            command.append("--resume")
            current["runner_resume_flag"] = "--resume"
        elif test_only or resume:
            command.append("--skip-seeding")
            current["runner_resume_flag"] = "--skip-seeding"
        run_dir = Path(job["run_dir"])
        with lock:
            current.update({
                "status": "running",
                "operation": "seed_only" if seed_only else "test_only" if test_only else "tests",
                "scheduled_test_ids": list(test_ids if in_place_resume else remaining_ids),
                "judge_parallelism_per_test": manifest["judge"]["parallelism_per_test"],
            })
            _atomic_json(launch_state_path, state)
        log_mode = "a" if in_place_resume else "w"
        with (run_dir / "run.log").open(log_mode) as log:
            if in_place_resume:
                log.write("\n=== resuming interrupted run ===\n")
                log.flush()
            completed = subprocess.run(command, cwd=manifest["root"], env=env, stdout=log, stderr=subprocess.STDOUT)
        with lock:
            durable_ids = _result_test_ids(Path(current["result_path"]))
            returncode = completed.returncode
            expected_ids = set(test_ids if in_place_resume else remaining_ids)
            if returncode == 0:
                if seed_only and not _result_has_complete_seed(job, Path(current["result_path"])):
                    returncode = 1
                    current["error"] = (
                        "canonical runner exited successfully without a complete durable seed"
                    )
                elif not seed_only and durable_ids != expected_ids:
                    returncode = 1
                    current["error"] = (
                        "canonical runner exited successfully without a complete durable result; "
                        f"expected {sorted(expected_ids)}, found {sorted(durable_ids)}"
                    )
            current.update({
                "status": "completed" if returncode == 0 else "failed",
                "returncode": returncode,
                "completed_test_ids": sorted(durable_ids),
                "ended_at": _now(),
            })
            _atomic_json(launch_state_path, state)
        return returncode

    def guarded_execute(job: dict[str, Any]) -> int:
        if job["configuration"] == "honcho":
            with honcho_slot:
                return execute(job)
        return execute(job)

    jobs_to_run = list(manifest["jobs"])
    if in_place_resume:
        if seed_only:
            jobs_to_run = [
                job for job in manifest["jobs"]
                if not _result_has_complete_seed(
                    job, Path(state_jobs[job["id"]]["result_path"])
                )
            ]
        else:
            requested = set(test_ids)
            jobs_to_run = [
                job for job in manifest["jobs"]
                if _result_test_ids(Path(state_jobs[job["id"]]["result_path"])) != requested
            ]
    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(guarded_execute, job): job for job in jobs_to_run}
        for future in concurrent.futures.as_completed(futures):
            job = futures[future]
            try:
                failures += bool(future.result())
            except Exception as exc:
                failures += 1
                with lock:
                    state_jobs[job["id"]].update({"status": "failed", "error": str(exc), "ended_at": _now()})
                    _atomic_json(launch_state_path, state)
    phase["ended_at"] = _now()
    phase["failure_count"] = failures
    phase["completed_test_ids_by_every_provider"] = sorted(
        _completed_tests_for_all_jobs(manifest, state)
    )
    state["ended_at"] = phase["ended_at"]
    _atomic_json(launch_state_path, state)
    if not failures:
        _write_combined_results(manifest, state)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="phase", required=True)
    prepare = subparsers.add_parser("prepare", help="local-only, unpaid manifest preparation")
    prepare.add_argument("--output-root", type=Path, required=True)
    prepare.add_argument("--personas", default=",".join(PERSONAS))
    prepare.add_argument("--configurations", default=",".join(HERMES_MEMORY_CONFIGURATIONS))
    prepare.add_argument(
        "--agent-runtime", choices=AGENT_RUNTIMES, default="hermes",
        help="outer agent runtime",
    )
    prepare.add_argument("--checkpoint", action="append", default=[], metavar="PERSONA=DIR")
    prepare.add_argument(
        "--model",
        required=True,
        help="Exact outer-agent model identifier recorded in the manifest (for example gpt-5.6-luna)",
    )
    prepare.add_argument("--agent-base-url")
    prepare.add_argument("--agent-api-key-env")
    prepare.add_argument("--agent-context-length", type=int, default=1_050_000)
    prepare.add_argument(
        "--agent-reasoning-effort",
        choices=AGENT_REASONING_EFFORTS,
        default="medium",
        help="Reasoning effort recorded for the outer agent",
    )
    smoke = subparsers.add_parser(
        "prepare-smoke",
        help="prepare a disposable small-history ingestion diagnostic without provider calls",
    )
    smoke.add_argument("--output-root", type=Path, required=True)
    smoke.add_argument("--persona", choices=PERSONAS, required=True)
    smoke.add_argument(
        "--configurations",
        default=",".join(HERMES_MEMORY_CONFIGURATIONS),
        help="Hermes memory systems to diagnose",
    )
    smoke.add_argument(
        "--agent-runtime", choices=AGENT_RUNTIMES, default="hermes",
        help="outer agent runtime",
    )
    smoke.add_argument("--checkpoint", action="append", default=[], metavar="PERSONA=DIR")
    smoke.add_argument("--test-ids", required=True)
    smoke.add_argument(
        "--distractor-sessions",
        type=int,
        default=DEFAULT_SMOKE_DISTRACTOR_SESSIONS,
    )
    smoke.add_argument(
        "--model",
        required=True,
        help="Exact outer-agent model identifier recorded in the disposable smoke manifest",
    )
    smoke.add_argument("--agent-base-url")
    smoke.add_argument("--agent-api-key-env")
    smoke.add_argument("--agent-context-length", type=int, default=1_050_000)
    smoke.add_argument(
        "--agent-reasoning-effort",
        choices=AGENT_REASONING_EFFORTS,
        default="medium",
        help="Reasoning effort recorded for the outer agent",
    )
    launch = subparsers.add_parser(
        "launch",
        help="create fresh profiles, seed once, and run the selected tests",
    )
    launch.add_argument("--manifest", type=Path, required=True)
    launch.add_argument("--concurrency", type=int, default=2)
    launch.add_argument(
        "--test-ids",
        default="all",
        help="comma-separated numeric test ids, or 'all'",
    )
    resume = subparsers.add_parser(
        "resume",
        help="continue the latest interrupted phase, or run a later disjoint test phase",
    )
    resume.add_argument("--manifest", type=Path, required=True)
    resume.add_argument("--concurrency", type=int, default=2)
    resume.add_argument(
        "--test-ids",
        required=True,
        help="exact interrupted-phase ids, or a new disjoint set after a completed phase",
    )
    seed = subparsers.add_parser(
        "seed",
        help="create fresh profiles, complete ingestion, and stop before tests",
    )
    seed.add_argument("--manifest", type=Path, required=True)
    seed.add_argument("--concurrency", type=int, default=2)
    resume_seed = subparsers.add_parser(
        "resume-seed",
        help="continue an interrupted ingestion-only phase and stop before tests",
    )
    resume_seed.add_argument("--manifest", type=Path, required=True)
    resume_seed.add_argument("--concurrency", type=int, default=2)
    prepare_test = subparsers.add_parser(
        "prepare-test-only",
        help="prepare a fresh test manifest from completed seeded profiles",
    )
    prepare_test.add_argument("--seed-manifest", type=Path, required=True)
    prepare_test.add_argument("--seed-state", type=Path, required=True)
    prepare_test.add_argument("--output-root", type=Path, required=True)
    prepare_test.add_argument(
        "--configurations",
        default=",".join(HERMES_MEMORY_CONFIGURATIONS),
        help="memory systems in the completed Hermes seed manifest",
    )
    prepare_test.add_argument("--test-ids", required=True)
    prepare_test.add_argument(
        "--agent-runtime", choices=AGENT_RUNTIMES,
        help="target runtime; omitted means the completed seed runtime",
    )
    prepare_test.add_argument(
        "--model",
        help=(
            "Optional override to run a different Hermes model against the completed memory."
        ),
    )
    prepare_test.add_argument("--agent-base-url")
    prepare_test.add_argument("--agent-api-key-env")
    prepare_test.add_argument("--agent-context-length", type=int)
    prepare_test.add_argument(
        "--agent-reasoning-effort",
        choices=AGENT_REASONING_EFFORTS,
        help="Reasoning effort for a different Hermes outer model.",
    )
    test_only = subparsers.add_parser(
        "test-only",
        help="run tests against completed seeded profiles without seeding or profile creation",
    )
    test_only.add_argument("--manifest", type=Path, required=True)
    test_only.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args(argv)
    try:
        if args.phase in {"prepare", "prepare-smoke"}:
            if args.phase == "prepare-smoke":
                personas = (args.persona,)
                smoke_test_ids = _parse_test_ids(args.test_ids)
                smoke_distractors = args.distractor_sessions
            else:
                personas = _parse_choices(args.personas, PERSONAS, "personas")
                smoke_test_ids = None
                smoke_distractors = DEFAULT_SMOKE_DISTRACTOR_SESSIONS
            configurations = _parse_choices(args.configurations, CONFIGURATIONS, "configurations")
            manifest = prepare_matrix(
                output_root=args.output_root,
                personas=personas,
                configurations=configurations,
                checkpoints=_checkpoint_args(args.checkpoint, personas),
                model=args.model,
                agent_context_length=args.agent_context_length,
                agent_base_url=args.agent_base_url,
                agent_api_key_env=args.agent_api_key_env,
                agent_reasoning_effort=args.agent_reasoning_effort,
                agent_runtime=args.agent_runtime,
                smoke_test_ids=smoke_test_ids,
                smoke_distractor_sessions=smoke_distractors,
            )
            print(manifest)
            return 0
        if args.phase in {"seed", "resume-seed"}:
            return launch_matrix(
                args.manifest,
                args.concurrency,
                (),
                resume=args.phase == "resume-seed",
                seed_only=True,
            )
        if args.phase == "prepare-test-only":
            configurations = _parse_choices(
                args.configurations, CONFIGURATIONS, "configurations",
            )
            manifest = prepare_test_only(
                seed_manifest_path=args.seed_manifest,
                seed_state_path=args.seed_state,
                output_root=args.output_root,
                configurations=configurations,
                test_ids=_parse_test_ids(args.test_ids),
                agent_runtime=args.agent_runtime,
                model=args.model,
                agent_base_url=args.agent_base_url,
                agent_api_key_env=args.agent_api_key_env,
                agent_context_length=args.agent_context_length,
                agent_reasoning_effort=args.agent_reasoning_effort,
            )
            print(manifest)
            return 0
        if args.phase == "test-only":
            manifest = load_manifest(args.manifest)
            if (manifest.get("evaluation_scope") or {}).get("kind") != "test_only":
                parser.error("test-only requires a manifest prepared by prepare-test-only")
            test_ids = tuple(
                str(test_id)
                for test_id in (manifest["evaluation_scope"].get("test_ids") or [])
            )
            return launch_matrix(
                args.manifest,
                args.concurrency,
                test_ids,
                resume=False,
            )
        return launch_matrix(
            args.manifest,
            args.concurrency,
            _parse_test_ids(args.test_ids),
            resume=args.phase == "resume",
        )
    except (PreparationError, ManifestDriftError) as exc:
        parser.error(str(exc))
    return 2


ALLOWED_ACTIONS = frozenset({"seed", "launch", "test-only", "resume", "resume-seed"})
_TEST_ID_ACTIONS = frozenset({"launch", "resume"})
_NO_TEST_ID_ACTIONS = frozenset({"seed", "test-only", "resume-seed"})
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class SuiteError(ValueError):
    """Raised when a local suite or saved suite state is invalid."""


@dataclass(frozen=True)
class SuiteJob:
    """One validated invocation of the existing matrix runner."""

    id: str
    manifest_path: Path
    action: str
    test_ids: tuple[str, ...]
    resources: tuple[str, ...]


@dataclass(frozen=True)
class Suite:
    """Validated suite input, resolved relative to its JSON file."""

    path: Path
    digest: str
    global_limit: int
    resource_limits: dict[str, int]
    jobs: tuple[SuiteJob, ...]


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise SuiteError(f"{label} does not exist: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SuiteError(f"{label} is not valid JSON: {path}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise SuiteError(f"{label} must contain a JSON object: {path}")
    return value


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SuiteError(f"{label} must be an integer greater than zero")
    return value


def _test_ids(value: Any, job_id: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise SuiteError(f"{job_id}: test_ids must be a non-empty JSON list")
    normalized: list[str] = []
    for raw_id in value:
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
            raise SuiteError(f"{job_id}: test_ids must contain strings or integers")
        try:
            number = int(str(raw_id))
        except ValueError as exc:
            raise SuiteError(f"{job_id}: invalid test id {raw_id!r}") from exc
        if number < 1 or number > 200:
            raise SuiteError(f"{job_id}: test id must be between 1 and 200: {raw_id!r}")
        test_id = f"{number:03d}"
        if test_id in normalized:
            raise SuiteError(f"{job_id}: duplicate test id {test_id}")
        normalized.append(test_id)
    return tuple(normalized)


def load_suite(path: Path) -> Suite:
    """Parse and validate every suite job before a subprocess can start."""

    path = path.resolve()
    raw_bytes = path.read_bytes() if path.is_file() else b""
    raw = _load_json_object(path, "suite file")
    global_limit = _positive_integer(raw.get("global_limit"), "global_limit")

    raw_limits = raw.get("resource_limits")
    if not isinstance(raw_limits, dict):
        raise SuiteError("resource_limits must be a JSON object")
    resource_limits: dict[str, int] = {}
    for key, value in raw_limits.items():
        if not isinstance(key, str) or not key.strip():
            raise SuiteError("resource limit keys must be non-empty strings")
        resource_limits[key] = _positive_integer(value, f"resource limit {key!r}")

    raw_jobs = raw.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise SuiteError("jobs must be a non-empty JSON list")

    jobs: list[SuiteJob] = []
    ids: set[str] = set()
    for index, raw_job in enumerate(raw_jobs, start=1):
        if not isinstance(raw_job, dict):
            raise SuiteError(f"job {index} must be a JSON object")
        job_id = raw_job.get("id")
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise SuiteError(f"job {index}: id must use letters, numbers, '.', '_' or '-'")
        if job_id in ids:
            raise SuiteError(f"duplicate job id {job_id!r}")
        ids.add(job_id)

        action = raw_job.get("action")
        if action not in ALLOWED_ACTIONS:
            raise SuiteError(f"{job_id}: action must be one of {sorted(ALLOWED_ACTIONS)}")

        raw_manifest = raw_job.get("manifest")
        if not isinstance(raw_manifest, str) or not raw_manifest.strip():
            raise SuiteError(f"{job_id}: manifest must be a non-empty path string")
        manifest_path = (path.parent / raw_manifest).resolve()
        manifest = _load_json_object(manifest_path, f"{job_id}: manifest")
        manifest_jobs = manifest.get("jobs")
        if not isinstance(manifest_jobs, list) or len(manifest_jobs) != 1:
            raise SuiteError(
                f"{job_id}: a suite manifest must contain exactly one matrix job"
            )

        has_test_ids = "test_ids" in raw_job
        if action in _TEST_ID_ACTIONS:
            if not has_test_ids:
                raise SuiteError(f"{job_id}: action {action!r} requires test_ids")
            test_ids = _test_ids(raw_job["test_ids"], job_id)
        elif has_test_ids:
            raise SuiteError(f"{job_id}: action {action!r} does not accept test_ids")
        else:
            test_ids = ()

        raw_resources = raw_job.get("resources")
        if not isinstance(raw_resources, list) or not raw_resources:
            raise SuiteError(f"{job_id}: resources must be a non-empty JSON list")
        resources: list[str] = []
        for resource in raw_resources:
            if not isinstance(resource, str) or not resource.strip():
                raise SuiteError(f"{job_id}: resource keys must be non-empty strings")
            if resource in resources:
                raise SuiteError(f"{job_id}: duplicate resource key {resource!r}")
            if resource not in resource_limits:
                raise SuiteError(f"{job_id}: no resource limit exists for {resource!r}")
            resources.append(resource)

        matrix_job = manifest_jobs[0]
        runtime = str(matrix_job.get("agent_runtime") or manifest.get("agent_runtime") or "")
        configuration = str(matrix_job.get("configuration") or "")
        agent_provider = str((manifest.get("agent") or {}).get("provider") or "")
        judge_backend = str((manifest.get("judge") or {}).get("backend") or "")
        required_resources = {
            f"agent:{runtime}",
            f"memory:{configuration}",
            f"provider:{agent_provider}",
            f"judge:{judge_backend}",
        }
        missing_resources = sorted(required_resources - set(resources))
        if "" in {runtime, configuration, agent_provider, judge_backend}:
            raise SuiteError(f"{job_id}: manifest is missing runtime, memory, agent provider, or judge")
        if missing_resources:
            raise SuiteError(
                f"{job_id}: resources omit values required by its manifest: "
                + ", ".join(missing_resources)
            )

        jobs.append(SuiteJob(
            id=job_id,
            manifest_path=manifest_path,
            action=action,
            test_ids=test_ids,
            resources=tuple(resources),
        ))

    return Suite(
        path=path,
        digest=hashlib.sha256(raw_bytes).hexdigest(),
        global_limit=global_limit,
        resource_limits=resource_limits,
        jobs=tuple(jobs),
    )

if __name__ == "__main__":
    raise SystemExit(main())
