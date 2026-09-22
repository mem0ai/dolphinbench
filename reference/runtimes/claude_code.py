"""Claude native memory and official-plugin ingestion."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
import time
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Callable, Mapping, Sequence


from harness.claude_driver import ClaudeResult, run_claude
from reference.memory import supermemory as ingest_supermemory


PERSONA_ENV = "DOLPHINBENCH_CLAUDE_PERSONA"
PERSONA = os.environ.get(PERSONA_ENV, "morgan")
SOURCE_COUNTS = {"morgan": 3400, "alex": 5011, "riley": 5128}
if PERSONA not in SOURCE_COUNTS:
    raise ValueError(f"{PERSONA_ENV} must be morgan, alex, or riley")
EXPECTED_SOURCE_MESSAGES = SOURCE_COUNTS[PERSONA]
ACCOUNT_ENV = "DOLPHINBENCH_CLAUDE_ACCOUNT"
ACCOUNT = os.environ.get(ACCOUNT_ENV, "1")
if ACCOUNT not in {"1", "2"}:
    raise ValueError(f"{ACCOUNT_ENV} must be 1 or 2")
TOKEN_KEY = "CLAUDE_CODE_OAUTH_TOKEN" if ACCOUNT == "1" else "CLAUDE_CODE_OAUTH_TOKEN_2"
RECEIPT_VERSION = 1
ARCHIVE_VERSION = 1
REQUIRED_CLAUDE_MODEL = "claude-sonnet-5"
FIXED_PROJECT_DIR = "/tmp/dolphinbench-claude-native-memory-project"
MEMORY_ROOT = Path("auto-memory")
REQUIRED_CLAUDE_CODE_VERSION = "2.1.259"
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_FORBIDDEN_NAMES = frozenset({
    ".env", "auth.json", "credentials.json", "tokens.json", "history.jsonl",
    "sessions", "session", "projects.json", "settings.json",
})


class NativeMemoryError(RuntimeError):
    """Raised when the frozen native-memory contract cannot be verified."""


@dataclass(frozen=True)
class SourceMessage:
    source_id: str
    timestamp: str
    content: str
    content_sha256: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_ids_sha256(items: Iterable[SourceMessage]) -> str:
    values = [item.source_id for item in items]
    return hashlib.sha256(
        json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()


def native_memory_dir(config_dir: Path, project_dir: str = FIXED_PROJECT_DIR) -> Path:
    """Return DolphinBench's explicit supported Claude auto-memory directory."""
    del project_dir
    return config_dir.resolve() / MEMORY_ROOT


def load_morgan_source_messages(corpus_path: Path) -> tuple[list[SourceMessage], str]:
    """Load the selected final history; retain the name for existing callers."""
    items, corpus_sha256 = ingest_supermemory.load_corpus(corpus_path, persona=PERSONA)
    messages = [
        SourceMessage(
            source_id=item.source_id,
            timestamp=item.narrative_date,
            content=item.content,
            content_sha256=hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
        )
        for item in items
    ]
    if len(messages) != EXPECTED_SOURCE_MESSAGES:
        raise NativeMemoryError(
            f"{PERSONA} native-memory seed requires exactly {EXPECTED_SOURCE_MESSAGES} "
            f"source messages; found {len(messages)}"
        )
    if len({item.source_id for item in messages}) != len(messages):
        raise NativeMemoryError(f"{PERSONA} corpus has duplicate native-memory source IDs")
    return messages, corpus_sha256


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with temporary.open("rb") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _load_receipt(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise NativeMemoryError(f"native-memory receipt does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise NativeMemoryError(f"native-memory receipt is invalid JSON: {path}") from exc
    if not isinstance(value, dict) or value.get("receipt_version") != RECEIPT_VERSION:
        raise NativeMemoryError(f"native-memory receipt has an unsupported schema: {path}")
    return value


def initialize_receipt(
    *,
    corpus_path: Path,
    receipt_path: Path,
    config_dir: Path,
    project_dir: str = FIXED_PROJECT_DIR,
    model: str = REQUIRED_CLAUDE_MODEL,
) -> dict[str, Any]:
    """Create the first durable receipt.  It never invokes Claude."""
    messages, corpus_sha256 = load_morgan_source_messages(corpus_path)
    corpus_path = corpus_path.resolve()
    config_dir = config_dir.resolve()
    if receipt_path.exists():
        raise NativeMemoryError(f"refusing to overwrite existing native-memory receipt: {receipt_path}")
    if model != REQUIRED_CLAUDE_MODEL:
        raise NativeMemoryError(f"Claude native-memory seed must use {REQUIRED_CLAUDE_MODEL}")
    if Path(project_dir).resolve() != Path(FIXED_PROJECT_DIR).resolve():
        raise NativeMemoryError("Claude native-memory seed must use the fixed empty project directory")
    if config_dir.exists() and any(config_dir.iterdir()):
        raise NativeMemoryError(f"native-memory config directory must start empty: {config_dir}")
    config_dir.mkdir(parents=True, exist_ok=True)
    Path(project_dir).mkdir(parents=True, exist_ok=True)
    if any(Path(project_dir).iterdir()):
        raise NativeMemoryError(f"native-memory project directory must stay empty: {project_dir}")
    receipt = {
        "receipt_version": RECEIPT_VERSION,
        "status": "in_progress",
        "persona": PERSONA,
        "runtime": "claude",
        "memory_provider": "builtin",
        "model": model,
        "claude_code_version": REQUIRED_CLAUDE_CODE_VERSION,
        "fixed_project_dir": FIXED_PROJECT_DIR,
        "config_dir": str(config_dir),
        "corpus": {
            "path": str(corpus_path),
            "sha256": corpus_sha256,
            "source_count": len(messages),
            "source_ids_sha256": _source_ids_sha256(messages),
        },
        "completed": {},
        "source_attempts": {},
        "snapshot": None,
    }
    _atomic_json(receipt_path, receipt)
    return receipt


def _validate_in_progress_receipt(
    receipt: Mapping[str, Any], messages: list[SourceMessage], corpus_path: Path, corpus_sha256: str,
) -> dict[str, dict[str, str]]:
    if receipt.get("persona") != PERSONA or receipt.get("runtime") != "claude":
        raise NativeMemoryError("receipt is not a Morgan Claude native-memory seed")
    if receipt.get("memory_provider") != "builtin":
        raise NativeMemoryError("receipt does not describe Claude native auto-memory")
    if receipt.get("model") != REQUIRED_CLAUDE_MODEL:
        raise NativeMemoryError("receipt must be seeded with Claude Sonnet 5")
    if receipt.get("claude_code_version") != REQUIRED_CLAUDE_CODE_VERSION:
        raise NativeMemoryError(
            f"receipt must be seeded with Claude Code {REQUIRED_CLAUDE_CODE_VERSION}"
        )
    if receipt.get("fixed_project_dir") != FIXED_PROJECT_DIR:
        raise NativeMemoryError("receipt uses a different Claude project directory")
    corpus = receipt.get("corpus")
    if not isinstance(corpus, Mapping):
        raise NativeMemoryError("receipt has no source corpus contract")
    expected = {
        "path": str(corpus_path.resolve()),
        "sha256": corpus_sha256,
        "source_count": EXPECTED_SOURCE_MESSAGES,
        "source_ids_sha256": _source_ids_sha256(messages),
    }
    if dict(corpus) != expected:
        raise NativeMemoryError("receipt source corpus contract differs from the frozen Morgan corpus")
    completed = receipt.get("completed")
    if not isinstance(completed, Mapping):
        raise NativeMemoryError("receipt.completed must be an object")
    expected_hashes = {item.source_id: item.content_sha256 for item in messages}
    unexpected = set(completed) - set(expected_hashes)
    if unexpected:
        raise NativeMemoryError(f"receipt contains unknown source IDs: {sorted(unexpected)[:3]}")
    normalized: dict[str, dict[str, str]] = {}
    for source_id, value in completed.items():
        if not isinstance(value, Mapping) or value.get("content_sha256") != expected_hashes[source_id]:
            raise NativeMemoryError(f"receipt content hash does not match source {source_id}")
        normalized[source_id] = {"content_sha256": expected_hashes[source_id]}
    _validate_source_attempts(
        receipt.get("source_attempts"), expected_hashes=expected_hashes,
        completed_ids=set(normalized),
    )
    return normalized


def _safe_number(value: Any, *, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise NativeMemoryError(f"source telemetry {field} must be a non-negative number")
    return value


def _normalize_source_telemetry(
    telemetry: Mapping[str, Any], *, expected_hash: str,
) -> dict[str, Any]:
    if telemetry.get("content_sha256") != expected_hash:
        raise NativeMemoryError("source telemetry content hash does not match its source")
    elapsed = _safe_number(telemetry.get("elapsed_seconds"), field="elapsed_seconds")
    model = telemetry.get("detected_model")
    if model is not None and (not isinstance(model, str) or len(model) > 256):
        raise NativeMemoryError("source telemetry detected_model must be a short string or null")
    status_code = telemetry.get("status_code")
    if status_code is not None and (isinstance(status_code, bool) or not isinstance(status_code, int)):
        raise NativeMemoryError("source telemetry status_code must be an integer or null")
    outcome = telemetry.get("outcome")
    if outcome not in {"completed", "rate_limited", "ambiguous_failure"}:
        raise NativeMemoryError("source telemetry outcome is not recognized")
    token_usage = telemetry.get("token_usage")
    if not isinstance(token_usage, Mapping):
        raise NativeMemoryError("source telemetry token_usage must be an object")
    normalized_usage: dict[str, int | float] = {}
    for name, value in token_usage.items():
        if not isinstance(name, str) or not _SAFE_COMPONENT.fullmatch(name):
            raise NativeMemoryError("source telemetry has an unsafe token_usage field")
        normalized_usage[name] = _safe_number(value, field=f"token_usage.{name}")
    return {
        "content_sha256": expected_hash,
        "elapsed_seconds": elapsed,
        "detected_model": model,
        "token_usage": normalized_usage,
        "status_code": status_code,
        "outcome": outcome,
    }


def _validate_source_attempts(
    value: Any, *, expected_hashes: Mapping[str, str], completed_ids: set[str],
) -> None:
    """Accept absent telemetry from older local receipts; validate it when present."""
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise NativeMemoryError("receipt.source_attempts must be an object")
    unknown = set(value) - set(expected_hashes)
    if unknown:
        raise NativeMemoryError(f"receipt telemetry has unknown source IDs: {sorted(unknown)[:3]}")
    for source_id, attempts in value.items():
        if not isinstance(attempts, list) or not attempts:
            raise NativeMemoryError("receipt telemetry attempts must be non-empty lists")
        for attempt in attempts:
            if not isinstance(attempt, Mapping):
                raise NativeMemoryError("receipt telemetry attempt must be an object")
            _normalize_source_telemetry(attempt, expected_hash=expected_hashes[source_id])
        if source_id in completed_ids and attempts[-1].get("outcome") != "completed":
            raise NativeMemoryError("a completed source must end with completed telemetry")


def record_source_attempt(
    *, corpus_path: Path, receipt_path: Path, source_id: str, telemetry: Mapping[str, Any],
) -> dict[str, Any]:
    """Save credential-free telemetry for one Claude attempt, bound to its source hash."""
    receipt = _load_receipt(receipt_path)
    messages, corpus_sha256 = load_morgan_source_messages(corpus_path)
    completed = _validate_in_progress_receipt(receipt, messages, corpus_path, corpus_sha256)
    del completed
    hashes = {item.source_id: item.content_sha256 for item in messages}
    if source_id not in hashes:
        raise NativeMemoryError(f"cannot record telemetry for unknown source {source_id}")
    normalized = _normalize_source_telemetry(telemetry, expected_hash=hashes[source_id])
    attempts = receipt.setdefault("source_attempts", {})
    if not isinstance(attempts, dict):
        raise NativeMemoryError("receipt.source_attempts must be an object")
    source_attempts = attempts.setdefault(source_id, [])
    if not isinstance(source_attempts, list):
        raise NativeMemoryError("receipt telemetry attempts must be lists")
    source_attempts.append(normalized)
    _atomic_json(receipt_path, receipt)
    return receipt


def pending_source_messages(*, corpus_path: Path, receipt_path: Path) -> list[SourceMessage]:
    """Return only source messages not durably recorded in a resumable receipt."""
    receipt = _load_receipt(receipt_path)
    messages, corpus_sha256 = load_morgan_source_messages(corpus_path)
    completed = _validate_in_progress_receipt(receipt, messages, corpus_path, corpus_sha256)
    return [message for message in messages if message.source_id not in completed]


def relocate_receipt_config_dir(*, receipt_path: Path, config_dir: Path) -> dict[str, Any]:
    """Atomically point an in-progress receipt at its restored local config tree."""
    receipt = _load_receipt(receipt_path)
    corpus = receipt.get("corpus") or {}
    corpus_path = Path(str(corpus.get("path") or ""))
    messages, corpus_sha256 = load_morgan_source_messages(corpus_path)
    _validate_in_progress_receipt(receipt, messages, corpus_path, corpus_sha256)
    target = config_dir.resolve()
    if not target.is_dir():
        raise NativeMemoryError("cannot relocate native-memory receipt to a missing config directory")
    receipt["config_dir"] = str(target)
    _atomic_json(receipt_path, receipt)
    return receipt


def record_completed_source(
    *, corpus_path: Path, receipt_path: Path, source_id: str,
    telemetry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Durably record one successful source call in chronological order.

    The caller must have already preserved the matching auto-memory files. This
    function records no model output and never invokes Claude.
    """
    receipt = _load_receipt(receipt_path)
    messages, corpus_sha256 = load_morgan_source_messages(corpus_path)
    completed = _validate_in_progress_receipt(receipt, messages, corpus_path, corpus_sha256)
    pending = [message for message in messages if message.source_id not in completed]
    if not pending:
        raise NativeMemoryError("all source messages are already recorded")
    expected = pending[0]
    if source_id != expected.source_id:
        raise NativeMemoryError(
            f"expected next source {expected.source_id}, not {source_id}; refusing out-of-order completion"
        )
    completed[source_id] = {"content_sha256": expected.content_sha256}
    receipt["completed"] = completed
    if telemetry is not None:
        normalized = _normalize_source_telemetry(telemetry, expected_hash=expected.content_sha256)
        if normalized["outcome"] != "completed":
            raise NativeMemoryError("completed source telemetry must have outcome completed")
        attempts = receipt.setdefault("source_attempts", {})
        if not isinstance(attempts, dict):
            raise NativeMemoryError("receipt.source_attempts must be an object")
        source_attempts = attempts.setdefault(source_id, [])
        if not isinstance(source_attempts, list):
            raise NativeMemoryError("receipt telemetry attempts must be lists")
        source_attempts.append(normalized)
    _atomic_json(receipt_path, receipt)
    return receipt


def source_telemetry_summary(receipt_path: Path, *, require_all_completed: bool = False) -> dict[str, Any]:
    """Summarize source-bound, credential-free ingestion telemetry."""
    receipt = _load_receipt(receipt_path)
    corpus = receipt.get("corpus") or {}
    messages, corpus_sha256 = load_morgan_source_messages(Path(str(corpus.get("path") or "")))
    completed = _validate_in_progress_receipt(receipt, messages, Path(str(corpus.get("path") or "")), corpus_sha256)
    attempts = receipt.get("source_attempts") or {}
    if require_all_completed and set(attempts) != set(completed):
        raise NativeMemoryError("completed native-memory seed has missing source telemetry")
    total_seconds = 0.0
    total_tokens: dict[str, float] = {}
    attempt_count = 0
    for source_attempts in attempts.values():
        for attempt in source_attempts:
            attempt_count += 1
            total_seconds += float(attempt["elapsed_seconds"])
            for name, value in attempt["token_usage"].items():
                total_tokens[name] = total_tokens.get(name, 0.0) + float(value)
    return {
        "attempt_count": attempt_count,
        "completed_source_count": len(completed),
        "elapsed_seconds": total_seconds,
        "token_usage": total_tokens,
    }


def _safe_archive_members(config_dir: Path) -> list[Path]:
    """Return only files Claude wrote in configured ``autoMemoryDirectory``."""
    memory_root = config_dir / MEMORY_ROOT
    if not memory_root.is_dir():
        raise NativeMemoryError("Claude did not create a native auto-memory directory")
    members = [memory_root]
    for path in sorted(memory_root.rglob("*")):
        relative = path.relative_to(config_dir)
        if path.is_symlink() or any(part.lower() in _FORBIDDEN_NAMES for part in relative.parts):
            raise NativeMemoryError(f"native-memory snapshot contains an unsafe path: {relative}")
        if not path.is_file() and not path.is_dir():
            raise NativeMemoryError(f"native-memory snapshot contains an unsupported path: {relative}")
        members.append(path)
    if not any(path.is_file() for path in members):
        raise NativeMemoryError("Claude native auto-memory directory has no files to freeze")
    return members


def _check_archive_member(name: str) -> None:
    parts = Path(name).parts
    if not parts or parts[0] != "auto-memory":
        raise NativeMemoryError(f"snapshot contains a path outside Claude native auto-memory: {name}")
    if any(part in {"", ".", ".."} for part in parts):
        raise NativeMemoryError(f"snapshot contains an unsafe path: {name}")
    if any(part.lower() in _FORBIDDEN_NAMES for part in parts):
        raise NativeMemoryError(f"snapshot contains credential or session data: {name}")


def create_snapshot(*, config_dir: Path, archive_path: Path) -> dict[str, Any]:
    """Freeze only Claude's native-memory tree; never archive its auth or session state."""
    config_dir = config_dir.resolve()
    archive_path = archive_path.resolve()
    if archive_path.exists():
        raise NativeMemoryError(f"refusing to overwrite native-memory archive: {archive_path}")
    members = _safe_archive_members(config_dir)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive_path.with_name(f".{archive_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tarfile.open(temporary, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            for path in members:
                relative = path.relative_to(config_dir)
                _check_archive_member(str(relative))
                info = archive.gettarinfo(str(path), arcname=str(relative))
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                if path.is_file():
                    with path.open("rb") as source:
                        archive.addfile(info, source)
                else:
                    archive.addfile(info)
        os.replace(temporary, archive_path)
    finally:
        temporary.unlink(missing_ok=True)
    validate_snapshot(archive_path)
    return {
        "archive_version": ARCHIVE_VERSION,
        "path": str(archive_path),
        "sha256": sha256_file(archive_path),
    }


def validate_snapshot(archive_path: Path) -> list[str]:
    """Validate the archive without extracting or contacting Claude."""
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            names: list[str] = []
            for member in archive.getmembers():
                _check_archive_member(member.name)
                if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                    raise NativeMemoryError(f"snapshot contains an unsupported archive member: {member.name}")
                names.append(member.name)
    except (tarfile.TarError, OSError) as exc:
        raise NativeMemoryError(f"cannot read native-memory snapshot {archive_path}: {exc}") from exc
    if "auto-memory/MEMORY.md" not in names:
        raise NativeMemoryError("snapshot has no Claude MEMORY.md index")
    return names


def restore_snapshot(*, archive_path: Path, config_dir: Path) -> Path:
    """Restore a verified archive to a fresh isolated Claude configuration directory."""
    archive_path = archive_path.resolve()
    config_dir = config_dir.resolve()
    if config_dir.exists() and any(config_dir.iterdir()):
        raise NativeMemoryError(f"destination config directory must be empty: {config_dir}")
    names = validate_snapshot(archive_path)
    config_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            target = (config_dir / member.name).resolve()
            if config_dir not in target.parents and target != config_dir:
                raise NativeMemoryError(f"snapshot escapes its config directory: {member.name}")
        archive.extractall(config_dir)
    memory_indexes = [config_dir / name for name in names if name == "auto-memory/MEMORY.md"]
    if len(memory_indexes) != 1 or not memory_indexes[0].is_file():
        raise NativeMemoryError("restored snapshot has no single readable MEMORY.md index")
    return memory_indexes[0].parent


def finalize_receipt(
    *, receipt_path: Path, archive_path: Path, published_archive_reference: str | None = None,
) -> dict[str, Any]:
    """Mark a completed 3,400-message seed only after the archive verifies."""
    receipt = _load_receipt(receipt_path)
    corpus = receipt.get("corpus") or {}
    corpus_path = Path(str(corpus.get("path") or ""))
    messages, corpus_sha256 = load_morgan_source_messages(corpus_path)
    completed = _validate_in_progress_receipt(receipt, messages, corpus_path, corpus_sha256)
    if len(completed) != EXPECTED_SOURCE_MESSAGES:
        raise NativeMemoryError(
            f"cannot finalize native memory: {len(completed)} of {EXPECTED_SOURCE_MESSAGES} messages completed"
        )
    if receipt.get("source_attempts"):
        receipt["ingestion_metrics"] = source_telemetry_summary(
            receipt_path, require_all_completed=True,
        )
    snapshot = create_snapshot(config_dir=Path(str(receipt["config_dir"])), archive_path=archive_path)
    if published_archive_reference is not None:
        reference = Path(published_archive_reference)
        if reference.is_absolute() or ".." in reference.parts:
            raise NativeMemoryError("published native-memory archive reference must be a relative path")
        snapshot["path"] = str(reference)
    receipt["status"] = "completed"
    receipt["snapshot"] = snapshot
    _atomic_json(receipt_path, receipt)
    return receipt


def validate_completed_seed(receipt_path: Path) -> dict[str, Any]:
    """Validate a frozen completed seed before it can enter an evaluation plan."""
    receipt = _load_receipt(receipt_path)
    if receipt.get("status") != "completed":
        raise NativeMemoryError("Claude native-memory seed is not completed")
    corpus = receipt.get("corpus") or {}
    corpus_path = Path(str(corpus.get("path") or ""))
    messages, corpus_sha256 = load_morgan_source_messages(corpus_path)
    completed = _validate_in_progress_receipt(receipt, messages, corpus_path, corpus_sha256)
    if len(completed) != EXPECTED_SOURCE_MESSAGES:
        raise NativeMemoryError("completed Claude native-memory seed does not cover all 3,400 messages")
    snapshot = receipt.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise NativeMemoryError("completed Claude native-memory seed has no snapshot")
    reference = Path(str(snapshot.get("path") or ""))
    if reference.is_absolute():
        archive = reference
    else:
        archive = (receipt_path.parent / reference).resolve()
        if receipt_path.parent.resolve() not in archive.parents:
            raise NativeMemoryError("completed Claude native-memory archive escapes its receipt directory")
    if not archive.is_file() or snapshot.get("sha256") != sha256_file(archive):
        raise NativeMemoryError("Claude native-memory snapshot is missing or changed")
    validate_snapshot(archive)
    return receipt


def _verify_claude_code_version(command: str | list[str] | None) -> None:
    """Refuse a seed unless its local Claude CLI reports the required version."""
    command_parts = ["claude"] if command is None else ([command] if isinstance(command, str) else command)
    try:
        completed = subprocess.run(
            [*command_parts, "--version"], text=True, capture_output=True,
            timeout=30, check=False,
        )
    except OSError as exc:
        raise NativeMemoryError(f"cannot inspect Claude Code version: {exc}") from exc
    output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode != 0 or REQUIRED_CLAUDE_CODE_VERSION not in output:
        raise NativeMemoryError(
            f"Claude native-memory seed requires Claude Code {REQUIRED_CLAUDE_CODE_VERSION}; "
            f"got {output.strip() or 'no version output'}"
        )


def seed(
    *,
    corpus_path: Path,
    receipt_path: Path,
    archive_path: Path,
    claude_command: str | list[str] | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = 600,
    invoke: Callable[..., ClaudeResult] = run_claude,
) -> dict[str, Any]:
    """Resume the approved seed, invoking Claude once for each unrecorded message."""
    receipt = _load_receipt(receipt_path)
    if receipt.get("status") == "completed":
        return validate_completed_seed(receipt_path)
    _verify_claude_code_version(claude_command)
    messages, corpus_sha256 = load_morgan_source_messages(corpus_path)
    completed = _validate_in_progress_receipt(receipt, messages, corpus_path, corpus_sha256)
    config_dir = Path(str(receipt.get("config_dir") or ""))
    for source in messages:
        if source.source_id in completed:
            continue
        result = invoke(
            source.content,
            narrative_time=source.timestamp,
            timeout=timeout,
            claude_command=claude_command,
            env=env,
            claude_config_dir=config_dir,
            mcp_config_path=None,
            allowed_mcp_tools=(),
            cwd=FIXED_PROJECT_DIR,
            model=str(receipt["model"]),
            native_memory_mode="seed",
            native_memory_dir=native_memory_dir(config_dir),
        )
        if not result.ok:
            raise NativeMemoryError(
                f"Claude did not complete {source.source_id}; receipt remains resumable: {result.error}"
            )
        completed[source.source_id] = {"content_sha256": source.content_sha256}
        receipt["completed"] = completed
        _atomic_json(receipt_path, receipt)
    return finalize_receipt(receipt_path=receipt_path, archive_path=archive_path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Local receipt validation for Claude native auto-memory. "
            "The only supported paid seed is reference.execution.claude_native."
        )
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--model", default=REQUIRED_CLAUDE_MODEL)
    args = parser.parse_args()
    if not args.receipt.exists():
        initialize_receipt(
            corpus_path=args.corpus,
            receipt_path=args.receipt,
            config_dir=args.config_dir,
            model=args.model,
        )
    print(json.dumps(_load_receipt(args.receipt), indent=2, sort_keys=True))
    return 0


from harness.claude_driver import ClaudeResult, run_claude
from harness.claude_plugin_setup import configure_plugin
from reference.runtimes.claude_code import SourceMessage
def child_environment(provider: str, home: str, plugin_env: Mapping[str, str], secrets: Mapping[str, str]) -> dict[str, str]:
    """Pass only subscription auth and this plugin's settings to Claude."""
    allowed = {
        "builtin": set(),
        "mem0": {"MEM0_API_KEY", "MEM0_USER_ID", "MEM0_API_URL"},
        "honcho": {"HONCHO_API_KEY", "HONCHO_ENDPOINT", "HONCHO_WORKSPACE", "HONCHO_PEER_NAME", "HONCHO_AI_PEER"},
        "hindsight": set(),
        "supermemory": {"SUPERMEMORY_CC_API_KEY", "SUPERMEMORY_API_URL", "SUPERMEMORY_MCP_URL", "SUPERMEMORY_REPO_TAG"},
    }
    if provider not in allowed or set(plugin_env) - allowed[provider]:
        raise ValueError("Unexpected environment settings for this plugin")
    token = secrets.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        raise ValueError("Claude subscription authentication is required")
    result = {"HOME": home, "CLAUDE_CODE_OAUTH_TOKEN": token, **plugin_env}
    if provider == "honcho":
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "no_proxy"):
            if key in secrets:
                result[key] = secrets[key]
        result["NODE_USE_ENV_PROXY"] = "1"
    return result


PLUGIN_RECEIPT_VERSION = 1
_PLUGIN_TOOLS = {
    "mem0": ("mcp__plugin_mem0_mem0__*",),
    "honcho": ("mcp__plugin_honcho_honcho__*",),
    "hindsight": ("mcp__hindsight__*",),
    "supermemory": ("mcp__plugin_supermemory_supermemory__*",),
}
_CREDENTIAL_FIELD_NAMES = frozenset({
    "api_key",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "MEM0_API_KEY",
    "HONCHO_API_KEY",
    "HINDSIGHT_API_KEY",
    "SUPERMEMORY_API_KEY",
    "SUPERMEMORY_CC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_FOUNDRY_API_KEY",
    "OPENAI_API_KEY",
    "TS_AUTHKEY",
})


class ClaudePluginIngestionError(RuntimeError):
    """Raised when an ingestion cannot safely continue."""


@dataclass(frozen=True)
class PluginIngestionConfig:
    """Caller-owned Claude and official-plugin configuration.

    ``plugin_identity`` must be a credential-free, immutable description of
    the official plugin build, normally including its version and SHA-256.
    ``plugin_settings`` may contain credentials and is never written to the
    receipt or traces by this module.
    """

    provider: str
    model: str
    home: Path
    project: Path
    claude_config_dir: Path
    plugin_source: Path
    plugin_identity: Mapping[str, Any]
    plugin_settings: Mapping[str, Any]
    claude_command: str | Sequence[str] | None = None
    environment: Mapping[str, str] | None = None
    run_as_user: int | None = None
    run_as_group: int | None = None


Persist = Callable[[Path, Mapping[str, Any]], None]
RunCall = Callable[..., ClaudeResult]


def credential_values(*sources: Mapping[str, Any]) -> tuple[str, ...]:
    """Return values of known credential fields without heuristic name matching."""
    return tuple(
        value
        for source in sources
        for name, value in source.items()
        if name in _CREDENTIAL_FIELD_NAMES and isinstance(value, str) and value
    )


def redact_credentials(value: Any, secrets: Sequence[str]) -> Any:
    """Redact known credential strings without changing non-string JSON values."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, Mapping):
        return {key: redact_credentials(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_credentials(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_credentials(item, secrets) for item in value)
    return value


def _json_value(value: Any, *, name: str) -> Any:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise ClaudePluginIngestionError(f"{name} must be JSON-serializable") from exc
    return json.loads(encoded)


def _source_contract(messages: Sequence[SourceMessage]) -> dict[str, Any]:
    if not messages:
        raise ClaudePluginIngestionError("at least one source message is required")
    hashes: dict[str, str] = {}
    for message in messages:
        if not message.source_id or message.source_id in hashes:
            raise ClaudePluginIngestionError("source IDs must be nonempty and unique")
        if hashlib.sha256(message.content.encode("utf-8")).hexdigest() != message.content_sha256:
            raise ClaudePluginIngestionError(f"source hash does not match {message.source_id}")
        hashes[message.source_id] = message.content_sha256
    ids = [message.source_id for message in messages]
    return {
        "source_count": len(messages),
        "source_ids": ids,
        "source_ids_sha256": hashlib.sha256(
            json.dumps(ids, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        ).hexdigest(),
        "source_hashes": hashes,
        "source_timestamps": {message.source_id: message.timestamp for message in messages},
    }


def _config_contract(config: PluginIngestionConfig, messages: Sequence[SourceMessage]) -> dict[str, Any]:
    if config.provider not in _PLUGIN_TOOLS:
        raise ClaudePluginIngestionError("provider must name an external official memory plugin")
    if not isinstance(config.model, str) or not config.model:
        raise ClaudePluginIngestionError("model is required")
    return {
        "provider": config.provider,
        "model": config.model,
        "home": str(config.home.expanduser().resolve()),
        "project": str(config.project.expanduser().resolve()),
        "claude_config_dir": str(config.claude_config_dir.expanduser().resolve()),
        "plugin_source": str(config.plugin_source.expanduser().resolve()),
        "plugin_identity": _json_value(config.plugin_identity, name="plugin_identity"),
        "store": {key: config.plugin_settings.get(key) for key in ("namespace", "api_url", "mcp_url")},
        "run_as_user": config.run_as_user,
        "run_as_group": config.run_as_group,
        **_source_contract(messages),
    }


def initialize_plugin_receipt(
    *, receipt_path: Path, config: PluginIngestionConfig, messages: Sequence[SourceMessage],
    persist: Persist = _atomic_json,
) -> dict[str, Any]:
    """Create a credential-free receipt before any Claude turn is attempted."""
    if receipt_path.exists():
        raise ClaudePluginIngestionError(f"refusing to overwrite receipt: {receipt_path}")
    receipt = {
        "receipt_version": PLUGIN_RECEIPT_VERSION,
        "status": "in_progress",
        "config": _config_contract(config, messages),
        "completed": {},
        "limitation": (
            "A completed Claude turn proves only that Claude returned a successful turn; "
            "it does not prove hosted background plugin processing has finished."
        ),
    }
    persist(receipt_path, receipt)
    return receipt


def _load_plugin_receipt(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ClaudePluginIngestionError(f"receipt does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ClaudePluginIngestionError(f"receipt is invalid JSON: {path}") from exc
    if not isinstance(value, dict) or value.get("receipt_version") != PLUGIN_RECEIPT_VERSION:
        raise ClaudePluginIngestionError("receipt has an unsupported schema")
    return value


def _validate_receipt(receipt: Mapping[str, Any], expected_config: Mapping[str, Any]) -> dict[str, str]:
    if receipt.get("status") not in {"in_progress", "completed"}:
        raise ClaudePluginIngestionError("receipt status is invalid")
    if receipt.get("config") != expected_config:
        raise ClaudePluginIngestionError(
            "receipt configuration differs (provider, model, plugin identity, paths, or source hashes)"
        )
    completed = receipt.get("completed")
    if not isinstance(completed, Mapping):
        raise ClaudePluginIngestionError("receipt.completed must be an object")
    hashes = expected_config["source_hashes"]
    if set(completed) - set(hashes):
        raise ClaudePluginIngestionError("receipt contains an unknown completed source")
    normalized: dict[str, str] = {}
    for source_id, entry in completed.items():
        if not isinstance(entry, Mapping) or entry.get("content_sha256") != hashes[source_id]:
            raise ClaudePluginIngestionError(f"receipt hash does not match {source_id}")
        normalized[source_id] = hashes[source_id]
    if receipt["status"] == "completed" and len(normalized) != len(hashes):
        raise ClaudePluginIngestionError("completed receipt is missing source messages")
    if set(normalized) != set(expected_config["source_ids"][:len(normalized)]):
        raise ClaudePluginIngestionError("completed sources are not the beginning of the history in order")
    return normalized


def _inflight_path(receipt_path: Path) -> Path:
    return receipt_path.with_name("in-flight.json")


def _require_safe_inflight(
    *, inflight_path: Path, completed: Mapping[str, str], expected_hashes: Mapping[str, str],
) -> None:
    if not inflight_path.exists():
        return
    try:
        marker = json.loads(inflight_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ClaudePluginIngestionError("in-flight marker is invalid JSON; a Claude turn may be ambiguous") from exc
    if not isinstance(marker, Mapping):
        raise ClaudePluginIngestionError("in-flight marker is invalid; a Claude turn may be ambiguous")
    source_id, content_hash = marker.get("source_id"), marker.get("content_sha256")
    if source_id in completed and completed[source_id] == content_hash:
        inflight_path.unlink()
        return
    if source_id not in expected_hashes or expected_hashes[source_id] != content_hash:
        raise ClaudePluginIngestionError("in-flight marker does not match this source contract")
    raise ClaudePluginIngestionError(
        f"in-flight Claude turn for {source_id} may have completed; refusing to replay it"
    )


def _outside_project(path: Path, project: Path) -> None:
    try:
        path.resolve().relative_to(project.resolve())
    except ValueError:
        return
    raise ClaudePluginIngestionError("trace directory must be outside the model-accessible project")


def _mcp_config(config_dir: Path, servers: Mapping[str, Any]) -> Path | None:
    if not servers:
        return None
    path = config_dir / "claude-plugin-ingestion-mcp.json"
    _atomic_json(path, {"mcpServers": dict(servers)})
    return path


def _trace_payload(result: Any) -> dict[str, Any]:
    if is_dataclass(result):
        value = asdict(result)
    elif isinstance(result, Mapping):
        value = dict(result)
    else:
        value = {"repr": repr(result)}
    return _json_value(value, name="Claude result trace")


def _turn_completed(result: Any) -> bool:
    """Trust the driver's final-result classification, not intermediate tools.

    A Claude trace can contain a failed search followed by a successful retry
    in the same completed turn.  Treating every ``tool_result.is_error`` as a
    failed ingestion would reject those ordinary successful turns.
    """
    if isinstance(result, Mapping):
        return result.get("ok") is True
    return isinstance(result, ClaudeResult) and result.ok


def _trace_path(trace_dir: Path, index: int, source_id: str) -> Path:
    # Source IDs are data, not trusted path components.
    digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:16]
    return trace_dir / f"{index:05d}-{digest}.json"


def ingest(
    *, receipt_path: Path, trace_dir: Path, config: PluginIngestionConfig,
    messages: Sequence[SourceMessage], max_messages: int, run_call: RunCall = run_claude,
    persist: Persist = _atomic_json,
    deadline: float | None = None,
    verify_turn: Callable[[SourceMessage, Any], None] | None = None,
    before_turn: Callable[[SourceMessage], None] | None = None,
    journal_turn: Callable[[int, SourceMessage, Any], None] | None = None,
) -> dict[str, Any]:
    """Run at most ``max_messages`` sequential fresh Claude sessions.

    A failed or interrupted call intentionally leaves its marker in place.  A
    later invocation must stop for operator review instead of replaying a turn
    whose provider-side effect cannot be known locally.
    """
    if not isinstance(max_messages, int) or isinstance(max_messages, bool) or max_messages <= 0:
        raise ClaudePluginIngestionError("max_messages must be a positive integer")
    expected = _config_contract(config, messages)
    receipt = _load_plugin_receipt(receipt_path)
    completed = _validate_receipt(receipt, expected)
    inflight_path = _inflight_path(receipt_path)
    _require_safe_inflight(
        inflight_path=inflight_path, completed=completed, expected_hashes=expected["source_hashes"],
    )
    if receipt["status"] == "completed":
        return receipt
    _outside_project(trace_dir, config.project)
    _outside_project(receipt_path, config.project)
    if run_call is run_claude and (config.run_as_user is None or config.run_as_user == 0):
        raise ClaudePluginIngestionError("Real ingestion must run Claude as its own non-root user")
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_dir.chmod(0o700)
    config.home.mkdir(parents=True, exist_ok=True)
    config.home.chmod(0o700)
    config.project.mkdir(parents=True, exist_ok=True)
    config.claude_config_dir.mkdir(parents=True, exist_ok=True)
    setup = configure_plugin(
        config.provider, config.home, config.project, config.plugin_source, dict(config.plugin_settings),
    )
    mcp_path = _mcp_config(config.claude_config_dir, setup.get("mcp_servers", {}))
    environment = child_environment(config.provider, str(config.home.resolve()), setup["env"], config.environment or {})
    if config.run_as_user is not None:
        for path in (config.home, *config.home.rglob("*")):
            os.chown(path, config.run_as_user, config.run_as_group or config.run_as_user, follow_symlinks=False)

    def save_trace(path: Path, value: Mapping[str, Any]) -> None:
        persist(path, redact_credentials(
            value, credential_values(config.environment or {}, config.plugin_settings),
        ))
    processed = 0
    for index, source in enumerate(messages):
        if source.source_id in completed:
            continue
        if processed == max_messages:
            break
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            break
        if before_turn is not None:
            before_turn(source)
        marker = {"source_id": source.source_id, "content_sha256": source.content_sha256}
        persist(inflight_path, marker)
        try:
            result = run_call(
                source.content,
                narrative_time=source.timestamp,
                timeout=remaining,
                claude_command=config.claude_command,
                env=environment,
                claude_config_dir=config.claude_config_dir,
                mcp_config_path=mcp_path,
                allowed_mcp_tools=_PLUGIN_TOOLS[config.provider],
                cwd=config.project,
                model=config.model,
                plugin_directories=setup["plugin_dirs"],
                settings=setup["settings"],
                persist_session=True,
                native_memory_mode="seed",
                native_memory_dir=config.claude_config_dir / "auto-memory",
                builtin_tools=["Read", "Write", "Edit", "Bash", "Skill", "ToolSearch"],
                permission_mode="bypassPermissions",
                run_as_user=config.run_as_user,
                run_as_group=config.run_as_group if config.run_as_group is not None else config.run_as_user,
            )
        except Exception as exc:
            save_trace(_trace_path(trace_dir, index, source.source_id), {
                "source_id": source.source_id,
                "content_sha256": source.content_sha256,
                "call_exception": f"{type(exc).__name__}: {exc}",
            })
            raise ClaudePluginIngestionError(
                f"Claude call for {source.source_id} raised; marker retained for review"
            ) from exc
        save_trace(_trace_path(trace_dir, index, source.source_id), {
            "source_id": source.source_id,
            "content_sha256": source.content_sha256,
            "result": _trace_payload(result),
        })
        if not _turn_completed(result):
            payload = _trace_payload(result)
            if payload.get("status_code") == 429:
                receipt["pause"] = {
                    "reason": "claude_usage_limit", "status_code": 429,
                    "reset_at": payload.get("rate_limit_reset_at"),
                    "source_id": source.source_id,
                    "content_sha256": source.content_sha256,
                    "requires_recovery_review": True,
                }
                persist(receipt_path, receipt)
                return receipt
            raise ClaudePluginIngestionError(
                f"Claude turn for {source.source_id} did not complete cleanly; marker retained for review"
            )
        if journal_turn is not None:
            journal_turn(index, source, result)
        if verify_turn is not None:
            verify_turn(source, result)
        completed[source.source_id] = source.content_sha256
        receipt.pop("pause", None)
        receipt["completed"] = {
            source_id: {"content_sha256": content_hash}
            for source_id, content_hash in completed.items()
        }
        if len(completed) == len(messages):
            receipt["status"] = "completed"
        persist(receipt_path, receipt)
        inflight_path.unlink()
        processed += 1
    return receipt
