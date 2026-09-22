"""Frozen ingestion evidence, profile materialization, and app verification."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import asyncio
import tempfile


import yaml

from harness.adapters.hermes_profile import profile_config_hashes


MANIFEST_VERSION = 2
SUPERMEMORY_RECEIPT_VERSION = 2
EXPECTED_PERSONA = "morgan"
EXPECTED_MESSAGES = 3400
EXPECTED_SESSIONS = 2765
FORBIDDEN_PROFILE_NAMES = frozenset({
    ".env", "auth.json", "credentials.json", "tokens.json",
    "mem0.json", "honcho.json", "supermemory.json",
    "auth.lock",
})
FORBIDDEN_KEY_PARTS = frozenset({
    "apikey", "api_key", "access_token", "refresh_token", "secret",
    "password", "authorization", "credential", "private_key",
})
MATRIX_TOOLSETS = ["dolphinbench-apps", "memory", "session_search"]
PROVIDER_TOOLSETS = {
    "hindsight": [*MATRIX_TOOLSETS, "hindsight"],
    "supermemory": [*MATRIX_TOOLSETS, "supermemory"],
}


class BridgeError(RuntimeError):
    """Raised when the bridge cannot prove that its output is safe to use."""


def _path(value: Path | str) -> Path:
    return Path(value).expanduser().resolve()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BridgeError(f"{path} must contain a JSON object")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _stable_source_id(persona: str, session_id: str, message_index: int) -> str:
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", session_id).strip("-") or "session"
    return f"dolphinbench-{persona}-{readable}-{message_index}"


def _stable_hindsight_id(persona: str, session_id: str, message_index: int) -> str:
    return (
        f"dolphinbench-{persona}-session-{session_id}-message-{message_index:03d}"
    )


def _credential_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9_]", "", key.lower())
    if normalized.endswith("_env"):
        return False
    return any(part in normalized for part in FORBIDDEN_KEY_PARTS)


def _assert_no_credential_keys(value: Any, where: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _credential_key(str(key)):
                raise BridgeError(f"credential-like field {where}.{key} is not allowed")
            _assert_no_credential_keys(child, f"{where}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_credential_keys(child, f"{where}[{index}]")


def _assert_http_url(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BridgeError(f"{where} must be a non-empty HTTP(S) URL")
    parsed = urlparse(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise BridgeError(f"{where} must be a non-empty HTTP(S) URL")
    return value.strip().rstrip("/")


def _load_provider_config(path: Path, receipt: Mapping[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    value = _read_json(path)
    _assert_no_credential_keys(value, "provider_config")
    provider = value.get("provider")
    if provider not in PROVIDER_TOOLSETS:
        raise BridgeError("provider_config.provider must be 'hindsight' or 'supermemory'")
    identity = value.get("identity")
    config = value.get("config")
    if not isinstance(identity, dict) or not isinstance(config, dict):
        raise BridgeError("provider_config must contain object fields identity and config")
    if identity.get("kind") != provider or identity.get("agent_runtime") != "hermes":
        raise BridgeError("provider identity kind and agent_runtime do not match the provider")
    if identity.get("narrative_timezone") != "America/Los_Angeles":
        raise BridgeError("Morgan provider identity must use America/Los_Angeles")

    if provider == "hindsight":
        identity_value = identity.get("bank_id")
        receipt_value = receipt.get("bank_id")
        if not isinstance(identity_value, str) or identity_value != receipt_value:
            raise BridgeError("Hindsight bank_id does not match the receipt")
        if config.get("mode") != "local_external":
            raise BridgeError("Hindsight config.mode must be local_external")
        config = dict(config)
        config["api_url"] = _assert_http_url(config.get("api_url"), "Hindsight config.api_url")
        if config.get("bank_id") not in {None, identity_value}:
            raise BridgeError("Hindsight config.bank_id does not match identity.bank_id")
        config["bank_id"] = identity_value
        config.setdefault("budget", "mid")
        if config["budget"] not in {"low", "mid", "high"}:
            raise BridgeError("Hindsight config.budget must be low, mid, or high")
        config.setdefault("memory_mode", "hybrid")
        config.setdefault("auto_recall", True)
        config.setdefault("auto_retain", True)
    else:
        identity_value = identity.get("container_tag")
        receipt_value = receipt.get("container_tag")
        if not isinstance(identity_value, str) or identity_value != receipt_value:
            raise BridgeError("Supermemory container_tag does not match the receipt")
        config = dict(config)
        config["container_tag"] = identity_value
        if config.get("base_url"):
            config["base_url"] = _assert_http_url(config["base_url"], "Supermemory config.base_url")
        else:
            config["base_url"] = ""
        config.setdefault("auto_recall", True)
        config.setdefault("auto_capture", True)
        config.setdefault("max_recall_results", 10)
        config.setdefault("search_mode", "hybrid")
        if not isinstance(config["max_recall_results"], int) or config["max_recall_results"] < 1:
            raise BridgeError("Supermemory max_recall_results must be a positive integer")
        if config["search_mode"] != "hybrid":
            raise BridgeError("Supermemory config.search_mode must be hybrid")
    return provider, identity, config


def _corpus_documents(corpus_path: Path) -> tuple[list[str], list[str]]:
    try:
        raw = yaml.safe_load(corpus_path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise BridgeError(f"cannot read corpus {corpus_path}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("sessions"), list):
        raise BridgeError("corpus must be an object with a sessions list")
    session_ids: list[str] = []
    source_ids: list[str] = []
    seen_sessions: set[str] = set()
    for session in raw["sessions"]:
        if not isinstance(session, dict) or not isinstance(session.get("id"), str):
            raise BridgeError("every corpus session must have a string id")
        session_id = session["id"]
        if session_id in seen_sessions:
            raise BridgeError(f"duplicate corpus session id: {session_id}")
        seen_sessions.add(session_id)
        messages = session.get("messages")
        if not isinstance(messages, list) or any(not isinstance(item, str) for item in messages):
            raise BridgeError(f"session {session_id} must contain text messages")
        session_ids.append(session_id)
        source_ids.extend(
            _stable_source_id(EXPECTED_PERSONA, session_id, index)
            for index in range(len(messages))
        )
    if len(session_ids) != EXPECTED_SESSIONS:
        raise BridgeError(f"corpus has {len(session_ids)} sessions; expected {EXPECTED_SESSIONS}")
    if len(source_ids) != EXPECTED_MESSAGES:
        raise BridgeError(f"corpus has {len(source_ids)} messages; expected {EXPECTED_MESSAGES}")
    return session_ids, source_ids


def _receipt_source_ids(receipt: Mapping[str, Any], provider: str) -> set[str]:
    if provider == "hindsight":
        batches = receipt.get("batches")
        if not isinstance(batches, list) or not batches:
            raise BridgeError("Hindsight receipt has no batches")
        ids: list[str] = []
        for batch in batches:
            if not isinstance(batch, dict) or batch.get("status") != "completed":
                raise BridgeError("Hindsight receipt contains a batch that is not completed")
            batch_ids = batch.get("document_ids")
            completed_ids = batch.get("completed_document_ids")
            if not isinstance(batch_ids, list) or not isinstance(completed_ids, list):
                raise BridgeError("Hindsight batch lacks document completion IDs")
            if set(batch_ids) != set(completed_ids):
                raise BridgeError("Hindsight batch completion IDs do not match document IDs")
            ids.extend(str(item) for item in batch_ids)
        if len(ids) != len(set(ids)):
            raise BridgeError("Hindsight receipt contains duplicate document IDs")
        return set(ids)
    sources = receipt.get("sources")
    if not isinstance(sources, dict):
        raise BridgeError("Supermemory receipt has no sources object")
    for source_id, record in sources.items():
        if not isinstance(record, dict):
            raise BridgeError(f"Supermemory source {source_id} is not an object")
        status = str(record.get("status", "")).lower()
        if status in {"pending", "processing", "queued", "failed", "error", "cancelled"}:
            raise BridgeError(f"Supermemory source {source_id} is not complete")
    return {str(item) for item in sources}


def _validate_receipt(receipt_path: Path, provider: str, corpus_path: Path, source_ids: list[str]) -> dict[str, Any]:
    receipt = _read_json(receipt_path)
    if receipt.get("provider") != provider or receipt.get("persona") != EXPECTED_PERSONA:
        raise BridgeError("receipt provider or persona does not match Morgan")
    if provider == "hindsight":
        if receipt.get("schema_version") != 1 or receipt.get("status") != "completed":
            raise BridgeError("Hindsight receipt is not a completed version-1 receipt")
        if receipt.get("documents_total") != EXPECTED_MESSAGES:
            raise BridgeError("Hindsight receipt does not report 3,400 documents")
        receipt_corpus = receipt.get("corpus_path")
        receipt_hash = receipt.get("corpus_sha256")
    else:
        if (
            receipt.get("receipt_version") != SUPERMEMORY_RECEIPT_VERSION
            or receipt.get("status") != "complete"
        ):
            raise BridgeError(
                f"Supermemory receipt is not a completed version-{SUPERMEMORY_RECEIPT_VERSION} receipt"
            )
        corpus = receipt.get("corpus")
        if not isinstance(corpus, dict) or corpus.get("source_count") != EXPECTED_MESSAGES:
            raise BridgeError("Supermemory receipt does not report 3,400 source messages")
        receipt_corpus = corpus.get("path")
        receipt_hash = corpus.get("sha256")
    if _path(str(receipt_corpus)) != corpus_path or receipt_hash != _sha256(corpus_path):
        raise BridgeError("receipt does not point to the immutable final corpus")
    actual_ids = _receipt_source_ids(receipt, provider)
    if provider == "supermemory":
        expected_ids = set(source_ids)
    else:
        # Hindsight IDs are reconstructed from the source IDs by validating the
        # stable session/message form directly below.
        expected_ids = set()
        for source_id in source_ids:
            match = re.fullmatch(r"dolphinbench-morgan-(.+)-(\d+)", source_id)
            if not match:
                raise BridgeError(f"invalid source ID generated from corpus: {source_id}")
            session_id, message_index = match.groups()
            expected_ids.add(_stable_hindsight_id(EXPECTED_PERSONA, session_id, int(message_index)))
    if actual_ids != expected_ids:
        raise BridgeError(
            f"receipt source coverage differs from corpus: {len(actual_ids)} actual, {len(expected_ids)} expected"
        )
    return receipt


def _find_builtin_job(manifest: Mapping[str, Any]) -> dict[str, Any]:
    jobs = manifest.get("jobs")
    if not isinstance(jobs, list):
        raise BridgeError("seed manifest has no jobs list")
    matches = [job for job in jobs if isinstance(job, dict) and job.get("id") == "builtin-morgan"]
    if len(matches) != 1:
        raise BridgeError("seed manifest must contain exactly one builtin-morgan job")
    return matches[0]


def _find_state_job(state: Mapping[str, Any], job_id: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for phase in state.get("phases", []):
        for job in phase.get("jobs", []) if isinstance(phase, dict) else []:
            if isinstance(job, dict) and job.get("id") == job_id:
                matches.append(job)
    if not matches:
        raise BridgeError(f"seed state has no job {job_id}")
    return matches[-1]


def _simulation_session_ids(path: Path) -> set[str]:
    try:
        value = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise BridgeError(f"cannot read numeric simulation {path}: {exc}") from exc
    sessions = value.get("sessions") if isinstance(value, dict) else None
    if not isinstance(sessions, list):
        raise BridgeError("numeric simulation has no sessions list")
    ids = [item.get("id") for item in sessions if isinstance(item, dict)]
    if any(not isinstance(item, str) for item in ids) or len(ids) != len(set(ids)):
        raise BridgeError("numeric simulation has invalid or duplicate session IDs")
    return set(ids)


def _validate_seed(
    manifest_path: Path,
    state_path: Path,
    profile_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], Path, list[str]]:
    manifest_path = _path(manifest_path)
    state_path = _path(state_path)
    profile_dir = _path(profile_dir)
    manifest = _read_json(manifest_path)
    state = _read_json(state_path)
    if manifest.get("manifest_version") != MANIFEST_VERSION or manifest.get("phase") != "prepared_offline":
        raise BridgeError("built-in seed manifest must be an unlaunched version-2 manifest")
    if _path(state.get("manifest", "")) != manifest_path:
        raise BridgeError("built-in seed state does not belong to the supplied manifest")
    if manifest.get("personas") != [EXPECTED_PERSONA]:
        raise BridgeError("built-in seed must be a Morgan seed")
    root = _path(manifest.get("root", ""))
    job = _find_builtin_job(manifest)
    if job.get("agent_runtime", "hermes") != "hermes" or job.get("provider") != "builtin":
        raise BridgeError("source job is not the built-in Hermes job")
    if profile_dir.name != str(job.get("profile")):
        raise BridgeError("profile_dir is not the profile named by builtin-morgan")
    if not profile_dir.is_dir() or not (profile_dir / "config.yaml").is_file():
        raise BridgeError("built-in Hermes profile is missing or has no config.yaml")
    persona_input = manifest.get("persona_inputs", {}).get(EXPECTED_PERSONA)
    if not isinstance(persona_input, dict):
        raise BridgeError("seed manifest has no Morgan input record")
    checkpoint_dir = _path(persona_input.get("checkpoint_dir", ""))
    corpus_path = checkpoint_dir / "life_sim.yaml"
    if not corpus_path.is_file():
        raise BridgeError("Morgan checkpoint has no life_sim.yaml")
    session_ids, source_ids = _corpus_documents(corpus_path)
    receipt_hashes = manifest.get("hashes", {}).get("files", {})
    if not isinstance(receipt_hashes, dict):
        raise BridgeError("seed manifest has no file hashes")
    for filename in ("life_sim.yaml", "checkpoint.json", "facts.yaml", "entities.yaml", "app_state.json"):
        path = checkpoint_dir / filename
        expected = receipt_hashes.get(str(path))
        if not path.is_file() or expected != _sha256(path):
            raise BridgeError(f"seed manifest hash does not match {path}")
    validation = persona_input.get("validation", {})
    if validation.get("sessions") != EXPECTED_SESSIONS:
        raise BridgeError("seed manifest Morgan session count is wrong")
    metadata = validation.get("metadata", {})
    if metadata.get("declared_sessions") != EXPECTED_SESSIONS:
        raise BridgeError("seed manifest does not declare 2,765 sessions")
    sim_path = _path(job.get("sim_path", ""))
    expected_sim_ids = _simulation_session_ids(sim_path)
    if expected_sim_ids != set(session_ids):
        raise BridgeError("numeric simulation sessions do not match the final corpus")
    source_state_job = _find_state_job(state, "builtin-morgan")
    result_path = _path(source_state_job.get("result_path", job.get("result_path", "")))
    result = _read_json(result_path)
    result_ids = {str(item.get("id")) for item in result.get("seed_sessions", []) if isinstance(item, dict)}
    if result_ids != expected_sim_ids or not result.get("phase1_ended_at"):
        raise BridgeError("built-in seed result is not a complete 2,765-session seed")
    source_profile_hashes = state.get("profiles", {}).get("builtin-morgan")
    if not isinstance(source_profile_hashes, dict):
        raise BridgeError("built-in seed state has no profile hashes")
    actual_source_hashes = _profile_config_hashes(profile_dir)
    for key, expected in source_profile_hashes.items():
        if actual_source_hashes.get(key) != expected:
            raise BridgeError(f"built-in profile hash differs for {key}")
    return manifest, state, job, result, corpus_path, source_ids


def _profile_config_hashes(profile_dir: Path) -> dict[str, str | None]:
    try:
        return profile_config_hashes(profile_dir)
    except RuntimeError as exc:
        raise BridgeError(str(exc)) from exc


def _copy_profile(source: Path, destination: Path, provider: str, provider_config: Mapping[str, Any]) -> None:
    if destination.exists():
        raise BridgeError(f"refusing to overwrite existing profile: {destination}")

    def ignore(_directory: str, names: list[str]) -> set[str]:
        ignored = set(FORBIDDEN_PROFILE_NAMES)
        ignored.update({"hindsight"})
        if provider == "hindsight":
            ignored.discard("hindsight")
        if provider == "supermemory":
            ignored.discard("supermemory.json")
        return ignored.intersection(names)

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, ignore=ignore)
    config_path = destination / "config.yaml"
    try:
        config = yaml.safe_load(config_path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise BridgeError(f"profile config is not valid YAML: {config_path}") from exc
    if not isinstance(config, dict) or not isinstance(config.get("memory"), dict):
        raise BridgeError("source profile config has no memory object")
    original = copy.deepcopy(config)
    config["memory"]["provider"] = provider
    _assert_no_credential_keys(config, "profile.config.yaml")
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    for filename in ("mem0.json", "honcho.json", "supermemory.json"):
        (destination / filename).unlink(missing_ok=True)
    shutil.rmtree(destination / "hindsight", ignore_errors=True)
    if provider == "hindsight":
        target = destination / "hindsight" / "config.json"
    else:
        target = destination / "supermemory.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(dict(provider_config), indent=2, sort_keys=True) + "\n")
    _assert_no_credential_keys(provider_config, f"profile.{provider}")
    _assert_no_credential_keys(original, "source_profile.config.yaml")


def _validate_no_credentials(root: Path, *, allowed_names: set[str] | None = None) -> None:
    allowed_names = allowed_names or set()
    for path in root.rglob("*"):
        if path.name in FORBIDDEN_PROFILE_NAMES - allowed_names or path.name in {"auth.lock"}:
            raise BridgeError(f"credential file was copied: {path}")
        if path.is_file() and path.suffix in {".json", ".yaml", ".yml"}:
            try:
                value = json.loads(path.read_text()) if path.suffix == ".json" else yaml.safe_load(path.read_text())
            except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
                raise BridgeError(f"cannot inspect output file {path}: {exc}") from exc
            _assert_no_credential_keys(value, str(path))


def _remove_test_hashes(file_hashes: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """Keep corpus/checkpoint hashes while dropping test hashes from the seed."""
    tests_root = (root / "tests").resolve()
    kept: dict[str, Any] = {}
    for name, digest in file_hashes.items():
        candidate = Path(name)
        path = _path(candidate if candidate.is_absolute() else root / candidate)
        try:
            path.relative_to(tests_root)
        except ValueError:
            kept[name] = digest
    return kept


def bridge_external_seed(
    *,
    receipt_path: Path,
    seed_manifest_path: Path,
    seed_state_path: Path,
    builtin_profile_dir: Path,
    provider_config_path: Path,
    output_dir: Path,
    hermes_home: Path | None = None,
) -> dict[str, Any]:
    receipt_path = _path(receipt_path)
    seed_manifest_path = _path(seed_manifest_path)
    seed_state_path = _path(seed_state_path)
    provider_config_path = _path(provider_config_path)
    output_dir = _path(output_dir)
    if output_dir.exists():
        raise BridgeError(f"output directory already exists: {output_dir}")
    manifest, state, source_job, source_result, corpus_path, source_ids = _validate_seed(
        seed_manifest_path, seed_state_path, _path(builtin_profile_dir)
    )
    receipt_header = _read_json(receipt_path)
    provider, identity, provider_config = _load_provider_config(provider_config_path, receipt_header)
    receipt = _validate_receipt(receipt_path, provider, corpus_path, source_ids)
    profile_name = f"dolphinbench-bridge-{provider}-morgan-{uuid.uuid4().hex[:12]}"
    hermes_home = _path(hermes_home or (Path.home() / ".hermes"))
    output_profile = output_dir / "profile"
    matrix_profile = hermes_home / "profiles" / profile_name
    output_manifest = output_dir / "launch_manifest.json"
    output_state = output_dir / "launch_state.json"
    output_result = output_dir / "seed_result.json"
    if matrix_profile.exists():
        raise BridgeError(f"refusing to overwrite existing matrix profile: {matrix_profile}")
    output_dir.mkdir(parents=True)
    try:
        _copy_profile(_path(builtin_profile_dir), output_profile, provider, provider_config)
        _copy_profile(_path(builtin_profile_dir), matrix_profile, provider, provider_config)
        if _profile_config_hashes(output_profile) != _profile_config_hashes(matrix_profile):
            raise BridgeError("output and matrix profile copies have different config hashes")
        allowed_provider_basename = "config.json" if provider == "hindsight" else "supermemory.json"
        _validate_no_credentials(output_profile, allowed_names={allowed_provider_basename})
        _validate_no_credentials(matrix_profile, allowed_names={allowed_provider_basename})

        bridge_id = f"{manifest['run_id']}-external-{provider}-morgan"
        run_dir = output_dir / "jobs" / provider / EXPECTED_PERSONA
        result = copy.deepcopy(source_result)
        result["external_ingestion"] = {
            "provider": provider,
            "identity": identity,
            "receipt_path": str(receipt_path),
            "receipt_sha256": _sha256(receipt_path),
        }
        _write_json(output_result, result)
        external_job_id = f"{provider}-morgan"
        source_hashes = _profile_config_hashes(output_profile)
        external_job = copy.deepcopy(source_job)
        external_job.update({
            "id": external_job_id,
            "run_id": f"{bridge_id}-job",
            "configuration": provider,
            "provider": provider,
            "provider_identity": identity,
            "agent_runtime": "hermes",
            "profile": profile_name,
            "toolsets": PROVIDER_TOOLSETS[provider],
            "run_dir": str(run_dir),
            "state_path": str(run_dir / "state.json"),
            "log_path": str(run_dir / "calls.jsonl"),
            "tool_config_path": str(run_dir / "tool_config.json"),
            "result_path": str(output_result),
            "ledger_path": str(run_dir / "cost_ledger.jsonl"),
            "profile_soul_path": str(run_dir / "profile_soul.md"),
            "seed_receipt_path": str(receipt_path),
            "seed_receipt_sha256": _sha256(receipt_path),
            "gateway_config": provider_config,
        })
        bridge_metadata = {
            "schema_version": 1,
            "provider": provider,
            "persona": EXPECTED_PERSONA,
            "expected_messages": EXPECTED_MESSAGES,
            "expected_sessions": EXPECTED_SESSIONS,
            "source_seed_manifest": str(seed_manifest_path),
            "source_seed_state": str(seed_state_path),
            "source_builtin_profile": str(_path(builtin_profile_dir)),
            "cloned_profile": str(matrix_profile),
            "profile_config_hashes": source_hashes,
            "corpus_path": str(corpus_path),
            "corpus_sha256": _sha256(corpus_path),
            "receipt_path": str(receipt_path),
            "receipt_sha256": _sha256(receipt_path),
            "receipt_status": receipt.get("status"),
            "receipt_document_count": EXPECTED_MESSAGES,
        }
        output_manifest_data = copy.deepcopy(manifest)
        output_persona_input = output_manifest_data.get("persona_inputs", {}).get(EXPECTED_PERSONA)
        if isinstance(output_persona_input, dict):
            output_validation = output_persona_input.get("validation")
            if isinstance(output_validation, dict):
                output_validation.pop("tests", None)
        output_hashes = copy.deepcopy(manifest.get("hashes", {}))
        if isinstance(output_hashes.get("files"), dict):
            output_hashes["files"] = _remove_test_hashes(output_hashes["files"], _path(manifest["root"]))
        output_manifest_data.update({
            "run_id": bridge_id,
            "output_root": str(output_dir),
            "configurations": [provider],
            "jobs": [external_job],
            "phase": "prepared_offline",
            "launch": {
                **copy.deepcopy(manifest.get("launch", {})),
                "profile_creation": "completed_by_local_external_seed_bridge",
                "provider_creation": "not_performed",
            },
            "source_seed_artifacts": {
                "manifest": str(seed_manifest_path),
                "manifest_sha256": _sha256(seed_manifest_path),
                "state": str(seed_state_path),
                "state_sha256": _sha256(seed_state_path),
                "builtin_profile": str(_path(builtin_profile_dir)),
                "builtin_profile_config_hashes": _profile_config_hashes(_path(builtin_profile_dir)),
                "receipt": str(receipt_path),
                "receipt_sha256": _sha256(receipt_path),
            },
            "bridge": bridge_metadata,
            "hashes": {
                **output_hashes,
                "bridge_files": {
                    str(output_result): _sha256(output_result),
                    str(output_profile / "config.yaml"): _sha256(output_profile / "config.yaml"),
                    str(output_profile / ("hindsight/config.json" if provider == "hindsight" else "supermemory.json")): _sha256(
                        output_profile / ("hindsight/config.json" if provider == "hindsight" else "supermemory.json")
                    ),
                },
            },
        })
        _assert_no_credential_keys(output_manifest_data, "launch_manifest")
        _write_json(output_manifest, output_manifest_data)
        state_data = {
            "manifest": str(output_manifest.resolve()),
            "started_at": None,
            "profiles": {external_job_id: source_hashes},
            "bridge": bridge_metadata,
            "phases": [{
                "index": 1,
                "mode": "external_ingestion_complete",
                "requested_test_ids": [],
                "jobs": [{
                    "id": external_job_id,
                    "status": "completed",
                    "result_path": str(output_result),
                    "profile_config_hashes": source_hashes,
                    "requested_test_ids": [],
                    "completed_test_ids": [],
                    "external_ingestion_receipt": str(receipt_path),
                    "external_ingestion_receipt_sha256": _sha256(receipt_path),
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                }],
            }],
        }
        _assert_no_credential_keys(state_data, "launch_state")
        _write_json(output_state, state_data)
        _validate_no_credentials(output_dir, allowed_names={allowed_provider_basename})
        return {
            "manifest": str(output_manifest),
            "state": str(output_state),
            "result": str(output_result),
            "profile": str(output_profile),
            "matrix_profile": str(matrix_profile),
            "profile_name": profile_name,
            "profile_config_hashes": source_hashes,
            "provider": provider,
            "receipt": str(receipt_path),
        }
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        shutil.rmtree(matrix_profile, ignore_errors=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--seed-manifest", type=Path, required=True)
    parser.add_argument("--seed-state", type=Path, required=True)
    parser.add_argument("--builtin-profile-dir", type=Path, required=True)
    parser.add_argument("--provider-config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--hermes-home", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = bridge_external_seed(
            receipt_path=args.receipt,
            seed_manifest_path=args.seed_manifest,
            seed_state_path=args.seed_state,
            builtin_profile_dir=args.builtin_profile_dir,
            provider_config_path=args.provider_config,
            output_dir=args.output_dir,
            hermes_home=args.hermes_home,
        )
    except BridgeError as exc:
        print(f"bridge failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


import yaml

from reference import evaluate as matrix
from reference.runtimes.hermes import profile_config_hashes


SECRET_NAMES = {".env", "auth.json", "credentials.json", "tokens.json", "auth.lock"}


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _database_check(directory: Path, expected_ids: list[str], bank: str) -> dict:
    """Recover only a disposable copy in single-user mode, without listeners."""
    installations = list((directory / "installation").glob("*/bin/postgres"))
    if len(installations) != 1:
        raise ValueError("snapshot must contain one PostgreSQL installation")
    postgres = installations[0]
    data = directory / "instances/hindsight/data"
    data.chmod(0o700)
    pid = data / "postmaster.pid"
    if pid.exists():
        pid.rename(directory / "archived-postmaster.pid")
    env = {**os.environ, "LD_LIBRARY_PATH": str(postgres.parent.parent / "lib")}
    query = """COPY (SELECT json_build_object(
      'documents', (SELECT json_agg(json_build_object('id', id, 'bank_id', bank_id)) FROM documents),
      'operations', (SELECT json_object_agg(status, n) FROM (SELECT status, count(*) n FROM async_operations GROUP BY status) s),
      'memory_units', (SELECT count(*) FROM memory_units)
    )) TO STDOUT;"""
    # The standalone backend treats a newline as the end of a statement.
    result = subprocess.run(
        [str(postgres), "--single", "-D", str(data), "-c", "listen_addresses=",
         "-c", "unix_socket_directories=", "-c", "default_transaction_read_only=on", "hindsight"],
        input=" ".join(query.splitlines()) + "\n", text=True,
        capture_output=True, env=env, timeout=180,
    )
    (directory / "recovery.log").write_text(result.stderr)
    result.check_returncode()
    start = result.stdout.find('{"documents"')
    if start < 0 or "ERROR:" in result.stderr:
        raise ValueError("could not inspect recovered Hindsight database")
    payload, _ = json.JSONDecoder().raw_decode(result.stdout[start:])
    docs = payload.pop("documents")
    ids = {row["id"] for row in docs}
    if not set(expected_ids) <= ids or any(row["bank_id"] != bank for row in docs):
        raise ValueError("database source-document coverage or bank identity mismatch")
    if set(payload["operations"] or {}) != {"completed"}:
        raise ValueError("database has unfinished or failed operations")
    checksums = subprocess.run(
        [str(postgres.parent / "pg_checksums"), "--check", "-D", str(data)],
        text=True, capture_output=True, env=env, timeout=180, check=True,
    )
    (directory / "checksums.log").write_text(checksums.stdout + checksums.stderr)
    return {**payload, "source_documents": len(expected_ids), "total_documents": len(docs),
            "additional_documents": len(ids - set(expected_ids)), "checksums_passed": True,
            "recovery_mode": "disposable_copy_postgres_single_user_no_listener"}


def _materialize_hindsight(
    spec: dict, *, output: Path, persona: str, tests: dict,
) -> dict[str, Path]:
    """Verify the actual ingestion, then expose its profile to test preparation."""
    archive, checksum, receipt_path, checkpoint = (
        Path(spec[key]) for key in ("archive", "checksum", "receipt", "checkpoint_dir")
    )
    url = spec["service_url"].rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("snapshot service_url must be an HTTPS URL without credentials")
    digest = _sha(archive)
    if checksum.read_text().split()[0] != digest:
        raise ValueError("snapshot checksum mismatch")
    receipt = json.loads(receipt_path.read_text())
    life = yaml.safe_load((checkpoint / "life_sim.yaml").read_text())
    ids = [f"{session['id']}:{index}" for session in life["sessions"]
           for index, _ in enumerate(session["messages"])]
    bank = receipt.get("identity")
    if (receipt.get("ingestion_path") != "hermes_official_memory_plugin"
            or receipt.get("provider") != "hindsight" or receipt.get("persona") != persona
            or receipt.get("status") != "completed" or not isinstance(bank, str)
            or receipt.get("source_count") != len(ids) or receipt.get("completed_source_ids") != ids
            or receipt.get("corpus_sha256") != _sha(checkpoint / "life_sim.yaml")):
        raise ValueError("completed snapshot receipt does not match the source checkpoint")
    if len(ids) != len(set(ids)):
        raise ValueError("source checkpoint contains duplicate source IDs")
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    profile_name = f"dolphinbench-hindsight-{persona}-frozen-{uuid.uuid4().hex[:12]}"
    profile = matrix.HERMES_HOME / "profiles" / profile_name
    database = output / "database-verification"
    database.mkdir(mode=0o700)
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise ValueError("duplicate archive member")
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("unsafe archive member")
        def read(name: str) -> bytes:
            member = bundle.getmember(name)
            if not member.isfile():
                raise ValueError(f"expected regular archive file: {name}")
            return bundle.extractfile(member).read()
        if read("ingestion-receipt.json") != receipt_path.read_bytes():
            raise ValueError("embedded receipt differs from completed receipt")
        result_bytes = read("hermes-seed-results.json")
        result = json.loads(result_bytes)
        ledger_bytes = read("hermes-seed-results.hindsight_seed_receipts.jsonl")
        ledger = [json.loads(line) for line in ledger_bytes.splitlines() if line]
        if ([row.get("seed_item_id") for row in ledger] != ids
                or any(row.get("document_id") != row.get("seed_item_id")
                       or row.get("bank_id") != bank or row.get("provider") != "hindsight"
                       or row.get("operation_kind") != "automatic_sync_turn" for row in ledger)):
            raise ValueError("official plugin receipt coverage or identity mismatch")
        if (not result.get("phase1_ended_at") or not result.get("summary", {}).get("seed_complete")
                or result.get("test_results") or len(result.get("seed_calls", [])) != len(ids)
                or [s["id"] for s in result["seed_sessions"]] != [s["id"] for s in life["sessions"]]):
            raise ValueError("snapshot does not contain a complete test-free Hermes ingestion")
        source_profile = result["profile"]
        prefix = f"hermes-home/profiles/{source_profile}/"
        config = yaml.safe_load(read(prefix + "config.yaml"))
        provider_config = json.loads(read(prefix + "hindsight/config.json"))
        if (config.get("memory", {}).get("provider") != "hindsight"
                or config.get("hindsight", {}).get("bank_id") != bank
                or provider_config.get("bank_id") != bank):
            raise ValueError("ingested Hermes profile does not match the bank")
        for member in members:
            if member.name.startswith(("installation/", "instances/")):
                bundle.extract(member, database, filter="data")
        try:
            verification = _database_check(database, ids, bank)
        finally:
            shutil.rmtree(database, ignore_errors=True)
        profile.mkdir(parents=True, mode=0o700, exist_ok=False)
        for member in members:
            if not member.name.startswith(prefix) or member.isdir():
                continue
            relative = PurePosixPath(member.name[len(prefix):])
            if any(part.lower() in SECRET_NAMES for part in relative.parts):
                continue
            if not member.isfile():
                raise ValueError("non-regular file in frozen Hermes profile")
            target = profile.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(read(member.name))
            target.chmod(member.mode & 0o700)
    config["hindsight"]["api_url"] = url
    provider_config["api_url"] = url
    (profile / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    _json(profile / "hindsight/config.json", provider_config)
    results_path = output / "hermes-seed-results.json"
    results_path.write_bytes(result_bytes)
    (output / "plugin-receipts.jsonl").write_bytes(ledger_bytes)
    (output / "ingestion-receipt.json").write_bytes(receipt_path.read_bytes())
    simulation = output / "numeric_life_sim.yaml"
    simulation.write_text(yaml.safe_dump({"name": f"{persona} frozen evaluation corpus",
        "sessions": life["sessions"], "test_queries": [{"id": i} for i in tests["ids"]]}, sort_keys=False))
    identity = {"kind": "hindsight", "bank_id": bank, "agent_runtime": "hermes",
                "narrative_timezone": "America/Los_Angeles"}
    job_id = f"hindsight-{persona}"
    job = {"id": job_id, "agent_runtime": "hermes", "configuration": "hindsight",
           "persona": persona, "provider": "hindsight", "profile": profile_name,
           "provider_identity": identity, "toolsets": config["toolsets"],
           "sim_path": str(simulation), "state_path": str(output / "mock-state.json"),
           "log_path": str(output / "mock-calls.jsonl"),
           "tool_config_path": str(output / "tool_config.json"), "result_path": str(results_path)}
    manifest_path, state_path = output / "launch_manifest.json", output / "launch_state.json"
    proof = {"archive_sha256": digest, "receipt_sha256": _sha(receipt_path),
             "source_profile": source_profile, "database": verification,
             "profile_changes": ["credential files excluded", "Hindsight API URL changed"],
             "service_url": url, "live_service_verified": False}
    _json(output / "verification.json", proof)
    _json(manifest_path, {"manifest_version": matrix.MANIFEST_VERSION,
          "phase": "prepared_offline", "root": str(matrix.ROOT), "personas": [persona],
          "agent": {"runtime": "hermes", "context_length": 1050000, "reasoning_effort": "high"},
          "persona_inputs": {persona: {"checkpoint_dir": str(checkpoint),
                                       "numeric_simulation": str(simulation)}},
          "snapshot_verification": proof, "jobs": [job]})
    _json(state_path, {"manifest": str(manifest_path), "profiles": {job_id: profile_config_hashes(profile)},
          "phases": [{"mode": "frozen_plugin_ingestion_verified", "jobs": [
              {"id": job_id, "status": "completed", "result_path": str(results_path)}]}]})
    return {"manifest": manifest_path, "state": state_path}


def _supermemory_container_tag(value: str) -> str:
    """Match the official Hermes Supermemory plugin's container normalization."""
    tag = re.sub(r"[^a-zA-Z0-9_]", "_", value)
    tag = re.sub(r"_+", "_", tag)
    return tag.strip("_") or "hermes"


def _materialize_supermemory(
    spec: dict, *, output: Path, persona: str, tests: dict,
) -> dict[str, Path]:
    """Verify a completed Supermemory snapshot and freeze its Hermes profile."""
    archive, checksum, receipt_path, checkpoint = (
        Path(spec[key]) for key in ("archive", "checksum", "receipt", "checkpoint_dir")
    )
    url = spec["service_url"].rstrip("/")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("snapshot service_url must be an HTTPS URL without credentials")

    digest = _sha(archive)
    if checksum.read_text().split()[0] != digest:
        raise ValueError("snapshot checksum mismatch")
    receipt = json.loads(receipt_path.read_text())
    life = yaml.safe_load((checkpoint / "life_sim.yaml").read_text())
    ids = [
        f"{session['id']}:{index}"
        for session in life["sessions"]
        for index, _ in enumerate(session["messages"])
    ]
    container_tag = receipt.get("identity")
    processing = receipt.get("provider_processing")
    processing_ok = (
        isinstance(processing, dict)
        and processing.get("pending_documents") == 0
        and processing.get("status") in {"completed", "completed_with_document_failure"}
    )
    if isinstance(processing, dict) and processing.get("status") == "completed_with_document_failure":
        processing_ok = processing_ok and bool(processing.get("approval")) and bool(
            processing.get("approved_failed_documents")
        )
    if (
        receipt.get("ingestion_path") != "hermes_official_memory_plugin"
        or receipt.get("provider") != "supermemory"
        or receipt.get("persona") != persona
        or receipt.get("status") != "completed"
        or not isinstance(container_tag, str)
        or not container_tag
        or receipt.get("source_count") != len(ids)
        or receipt.get("completed_source_ids") != ids
        or receipt.get("corpus_sha256") != _sha(checkpoint / "life_sim.yaml")
        or not processing_ok
    ):
        raise ValueError("completed snapshot receipt does not match the source checkpoint")
    if len(ids) != len(set(ids)):
        raise ValueError("source checkpoint contains duplicate source IDs")

    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    profile_name = f"dolphinbench-supermemory-{persona}-frozen-{uuid.uuid4().hex[:12]}"
    profile = matrix.HERMES_HOME / "profiles" / profile_name
    normalized_tag = _supermemory_container_tag(container_tag)
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise ValueError("duplicate archive member")
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("unsafe archive member")

        def read(name: str) -> bytes:
            member = bundle.getmember(name)
            if not member.isfile():
                raise ValueError(f"expected regular archive file: {name}")
            return bundle.extractfile(member).read()

        if read("ingestion-receipt.json") != receipt_path.read_bytes():
            raise ValueError("embedded receipt differs from completed receipt")
        result_bytes = read("hermes-seed-results.json")
        result = json.loads(result_bytes)
        ledger_bytes = read("hermes-seed-results.supermemory_seed_receipts.jsonl")
        ledger = [json.loads(line) for line in ledger_bytes.splitlines() if line]
        if (
            [row.get("seed_item_id") for row in ledger] != ids
            or any(
                row.get("conversation_id") != row.get("seed_item_id")
                or row.get("container_tag") != normalized_tag
                or row.get("provider") != "supermemory"
                or row.get("operation_kind") != "automatic_sync_turn"
                or row.get("receipt_type") != "memory_seed_operation"
                or not row.get("hermes_session_id")
                for row in ledger
            )
        ):
            raise ValueError("official plugin receipt coverage or identity mismatch")
        if (
            not result.get("phase1_ended_at")
            or not result.get("summary", {}).get("seed_complete")
            or result.get("test_results")
            or len(result.get("seed_calls", [])) != len(ids)
            or [session["id"] for session in result["seed_sessions"]]
            != [session["id"] for session in life["sessions"]]
        ):
            raise ValueError("snapshot does not contain a complete test-free Hermes ingestion")
        source_profile = result["profile"]
        prefix = f"hermes-home/profiles/{source_profile}/"
        config = yaml.safe_load(read(prefix + "config.yaml"))
        provider_config = json.loads(read(prefix + "supermemory.json"))
        if (
            config.get("memory", {}).get("provider") != "supermemory"
            or config.get("supermemory", {}).get("container_tag") != container_tag
            or provider_config.get("container_tag") != container_tag
        ):
            raise ValueError("ingested Hermes profile does not match the Supermemory container")
        profile.mkdir(parents=True, mode=0o700, exist_ok=False)
        for member in members:
            if not member.name.startswith(prefix) or member.isdir():
                continue
            relative = PurePosixPath(member.name[len(prefix):])
            if any(part.lower() in SECRET_NAMES for part in relative.parts):
                continue
            if not member.isfile():
                raise ValueError("non-regular file in frozen Hermes profile")
            target = profile.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(read(member.name))
            target.chmod(member.mode & 0o700)

    config["supermemory"]["base_url"] = url
    provider_config["base_url"] = url
    (profile / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    _json(profile / "supermemory.json", provider_config)
    results_path = output / "hermes-seed-results.json"
    results_path.write_bytes(result_bytes)
    (output / "plugin-receipts.jsonl").write_bytes(ledger_bytes)
    (output / "ingestion-receipt.json").write_bytes(receipt_path.read_bytes())
    simulation = output / "numeric_life_sim.yaml"
    simulation.write_text(
        yaml.safe_dump(
            {
                "name": f"{persona} frozen evaluation corpus",
                "sessions": life["sessions"],
                "test_queries": [{"id": test_id} for test_id in tests["ids"]],
            },
            sort_keys=False,
        )
    )
    identity = {
        "kind": "supermemory",
        "container_tag": container_tag,
        "agent_runtime": "hermes",
        "narrative_timezone": "America/Los_Angeles",
    }
    job_id = f"supermemory-{persona}"
    job = {
        "id": job_id,
        "agent_runtime": "hermes",
        "configuration": "supermemory",
        "persona": persona,
        "provider": "supermemory",
        "profile": profile_name,
        "provider_identity": identity,
        "toolsets": config["toolsets"],
        "sim_path": str(simulation),
        "state_path": str(output / "mock-state.json"),
        "log_path": str(output / "mock-calls.jsonl"),
        "tool_config_path": str(output / "tool_config.json"),
        "result_path": str(results_path),
    }
    manifest_path = output / "launch_manifest.json"
    state_path = output / "launch_state.json"
    proof = {
        "archive_sha256": digest,
        "receipt_sha256": _sha(receipt_path),
        "source_profile": source_profile,
        "provider_processing": processing,
        "profile_changes": ["credential files excluded", "Supermemory API URL changed"],
        "service_url": url,
        "live_service_verified": False,
    }
    _json(output / "verification.json", proof)
    _json(
        manifest_path,
        {
            "manifest_version": matrix.MANIFEST_VERSION,
            "phase": "prepared_offline",
            "root": str(matrix.ROOT),
            "personas": [persona],
            "agent": {
                "runtime": "hermes",
                "context_length": 1050000,
                "reasoning_effort": "high",
            },
            "persona_inputs": {
                persona: {
                    "checkpoint_dir": str(checkpoint),
                    "numeric_simulation": str(simulation),
                }
            },
            "snapshot_verification": proof,
            "jobs": [job],
        },
    )
    _json(
        state_path,
        {
            "manifest": str(manifest_path),
            "profiles": {job_id: profile_config_hashes(profile)},
            "phases": [
                {
                    "mode": "frozen_plugin_ingestion_verified",
                    "jobs": [
                        {"id": job_id, "status": "completed", "result_path": str(results_path)}
                    ],
                }
            ],
        },
    )
    return {"manifest": manifest_path, "state": state_path}


def materialize(spec: dict, *, output: Path, persona: str, tests: dict) -> dict[str, Path]:
    """Verify one supported completed plugin snapshot for test-only evaluation."""
    receipt = json.loads(Path(spec["receipt"]).read_text())
    provider = receipt.get("provider")
    if provider == "hindsight":
        return _materialize_hindsight(spec, output=output, persona=persona, tests=tests)
    if provider == "supermemory":
        return _materialize_supermemory(spec, output=output, persona=persona, tests=tests)
    raise ValueError(f"unsupported completed snapshot provider: {provider!r}")


ROOT = Path(__file__).resolve().parents[1]


async def verify(app_python: str) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    fixtures = json.loads((ROOT / "examples/reference/morgan-app-tools.json").read_text())
    counts = {}
    with tempfile.TemporaryDirectory(prefix="dolphinbench-app-check-") as temporary:
        os.environ["HERMES_HOME"] = str(Path(temporary) / "hermes")
        from tools.mcp_tool import _normalize_mcp_input_schema

        for persona in ("morgan", "alex", "riley"):
            root = Path(temporary) / persona
            root.mkdir()
            (root / "state.json").write_text("{}")
            parameters = StdioServerParameters(command=app_python,
                args=[str(ROOT / "mock_mcp/server.py")], env={
                    "DOLPHINBENCH_PERSONA": persona,
                    "DOLPHINBENCH_STATE_PATH": str(root / "state.json"),
                    "DOLPHINBENCH_LOG_PATH": str(root / "calls.jsonl"),
                    "DOLPHINBENCH_TOOL_CONFIG_PATH": str(root / "tools.json"),
                })
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    response = await session.list_tools()
                    actual = {tool.name: {"description": tool.description,
                                         "parameters": _normalize_mcp_input_schema(tool.input_schema)}
                              for tool in response.tools}
                    if not actual:
                        raise ValueError(f"No app tools exposed for {persona}")
                    counts[persona] = len(actual)
                    if persona == "morgan":
                        for name, fixture in fixtures.items():
                            if actual.get(name) != fixture["definition"]:
                                raise ValueError(f"Canonical schema mismatch: {name}: "
                                                 f"{json.dumps(actual.get(name), sort_keys=True)}")
    return {"tool_counts": counts, "morgan_schemas_compared": len(fixtures), "model_calls": 0}


def check_apps_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-python", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(verify(str(Path(args.app_python).absolute()))), indent=2))

if __name__ == "__main__":
    raise SystemExit(main())
