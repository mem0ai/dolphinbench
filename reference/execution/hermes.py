"""Reference hermes implementation."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib
import json
import os
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence


import yaml

from reference import artifacts as bridge_external_seed
from reference.memory import hindsight as ingest_hindsight
from reference.memory import supermemory as ingest_supermemory
from reference.memory import services as services

try:
    import modal
except ModuleNotFoundError:  # Allows static helpers to be imported without Modal installed.
    modal = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[2]
PERSONA_ENV = "DOLPHINBENCH_MEMORY_INGESTION_PERSONA"
PERSONA = os.environ.get(PERSONA_ENV, "morgan")
if PERSONA not in {"morgan", "alex", "riley"}:
    raise ValueError(f"{PERSONA_ENV} must be morgan, alex, or riley")
AGENT_ENV = "DOLPHINBENCH_MEMORY_INGESTION_AGENT"
AGENT = os.environ.get(AGENT_ENV, "luna")
if AGENT not in {"luna", "minimax-m3"}:
    raise ValueError(f"{AGENT_ENV} must be luna or minimax-m3")
AGENT_MODEL = "minimax/minimax-m3" if AGENT == "minimax-m3" else "gpt-5.6-luna"
AGENT_CONTEXT_LENGTH = 1048576 if AGENT == "minimax-m3" else 1050000
DEFAULT_CORPUS = ROOT / (
    "construction/v2/morgan/release/500k_final/checkpoint/life_sim.yaml"
    if PERSONA == "morgan" else f"construction/v2/{PERSONA}/release/500k_final/life_sim.yaml"
)
REMOTE_ROOT = Path("/home/ubuntu/.hermes/benchmark/hbme")
REMOTE_CORPUS = REMOTE_ROOT / DEFAULT_CORPUS.relative_to(ROOT)
REMOTE_RUNTIME_ROOT = Path("/tmp/dolphinbench-memory-ingestion")
LOCAL_HERMES_ROOT = Path("/home/ubuntu/.hermes/hermes-agent-dolphinbench-final")
REMOTE_HERMES_ROOT = Path("/opt/hermes")
REMOTE_HERMES_BIN = REMOTE_HERMES_ROOT / ".venv/bin/hermes"
REMOTE_MOCK_MCP_ROOT = Path("/opt/dolphinbench-mock-mcp")
REMOTE_MOCK_MCP_PYTHON = REMOTE_MOCK_MCP_ROOT / "bin/python"
FINAL_MODE = "final"
SMOKE_MODE = "smoke"
SMOKE_SOURCE_LIMIT = 10
FINAL_HINDSIGHT_BATCHES_PER_INVOCATION = 5
FINAL_SUPERMEMORY_SOURCES_PER_INVOCATION = 100
FINAL_SOURCE_CHECKPOINT = f"{PERSONA}-500k-final-v1"
FINAL_HINDSIGHT_BANK = (
    ingest_hindsight.DEFAULT_BANK_ID if PERSONA == "morgan" and AGENT == "luna"
    else f"dolphinbench-hermes-{AGENT}-{PERSONA}-500k-final-v1"
)
FINAL_SUPERMEMORY_CONTAINER = (
    ingest_supermemory.stable_container_tag(PERSONA) if PERSONA == "morgan" and AGENT == "luna"
    else f"dolphinbench_hermes_{AGENT.replace('-', '_')}_{PERSONA}_500k_final_v1"
)
SMOKE_HINDSIGHT_BANK = f"dolphinbench-{PERSONA}-500k-smoke-v1"
SMOKE_SUPERMEMORY_CONTAINER = f"dolphinbench-{PERSONA}-500k-smoke-v1"
SUPERMEMORY_SMOKE_QUERY = "What company did Morgan and Devon start?"
PROGRESS_SCHEMA_VERSION = 1
PROGRESS_DIRECTORY_NAME = "in-progress"
PLUGIN_RECEIPT_SCHEMA_VERSION = 1
HERMES_MESSAGES_PER_GROUP = 100
HERMES_RUN_BUDGET_SECONDS = 16 * 3600
HERMES_AUTOMATIC_CONTINUATION_RETRIES = 10
HERMES_MEMORY_SYNC_DRAIN_TIMEOUT_SECONDS = 6000
HERMES_EXIT_WATCHDOG_SECONDS = HERMES_MEMORY_SYNC_DRAIN_TIMEOUT_SECONDS + 120
HINDSIGHT_PLUGIN_TIMEOUT_SECONDS = 600
SUPERMEMORY_PLUGIN_TIMEOUT_SECONDS = 15
HINDSIGHT_OPERATION_DRAIN_TIMEOUT_SECONDS = 6000
HINDSIGHT_OPERATION_POLL_SECONDS = 5
HINDSIGHT_OPERATION_RETRY_BATCH_SIZE = 100
HINDSIGHT_RECOVERY_WORKER_ENVIRONMENT = {
    "HINDSIGHT_API_WORKER_MAX_SLOTS": "1",
    "HINDSIGHT_API_WORKER_CONSOLIDATION_RESERVED_SLOTS": "0",
}
HINDSIGHT_DATABASE_BACKUP_NAME = "hindsight-database.zip"
HERMES_PROGRESS_ARCHIVE_NAME = "hermes-progress.tar.gz"
HINDSIGHT_LIVE_CHECKPOINT_KIND = "hindsight-admin-backup-v1"
HERMES_SYNTHETIC_CONTINUATION_MESSAGES = {
    "[System: Continue now. Execute the required tool calls and only send your final answer after completing the task.]",
    "Your previous turn indicated a tool call but none was included. Do not narrate a plan or restate intent — issue the actual tool call now to continue the task.",
    "You just executed tool calls but returned an empty response. Please process the tool results above and continue with the task.",
}

Mode = Literal["smoke", "final"]
Provider = Literal["hindsight", "supermemory"]


class IngestionPreparationError(RuntimeError):
    """The requested ingestion cannot safely start or publish a snapshot."""


class PaidCallApprovalRequired(IngestionPreparationError):
    """A remote ingestion was requested without explicit approval."""


class AutomaticContinuationRequired(RuntimeError):
    """A clean checkpoint is ready for the next Modal worker attempt."""


@dataclass(frozen=True)
class IngestionIdentity:
    provider: Provider
    mode: Mode
    name: str


@dataclass(frozen=True)
class IngestionPlan:
    provider: Provider
    mode: Mode
    identity: IngestionIdentity
    corpus_path: Path
    corpus_sha256: str
    source_count: int
    source_checkpoint: str
    snapshot_volume: str
    archive_path: str
    checksum_path: str
    receipt_path: str
    bridge_contract_error: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "persona": PERSONA,
            "agent_runtime": "hermes",
            "model": AGENT_MODEL,
            "agent_image": os.environ.get("DOLPHINBENCH_AGENT_IMAGE"),
            "reasoning_effort": "high",
            "context_length": AGENT_CONTEXT_LENGTH,
            "ingestion_environment": {PERSONA_ENV: PERSONA, **({AGENT_ENV: AGENT} if AGENT != "luna" else {})},
            "mode": self.mode,
            "identity": self.identity.name,
            "corpus": str(self.corpus_path),
            "corpus_sha256": self.corpus_sha256,
            "source_count": self.source_count,
            "source_checkpoint": self.source_checkpoint,
            "snapshot_volume": self.snapshot_volume,
            "archive_path": self.archive_path,
            "checksum_path": self.checksum_path,
            "receipt_path": self.receipt_path,
            "bridge_contract_error": self.bridge_contract_error,
            "provider_calls_require": "--confirm-paid-calls",
        }


@dataclass(frozen=True)
class InProgressPaths:
    """Mutable checkpoint paths that are separate from final snapshot evidence."""

    root: Path

    @property
    def current_path(self) -> Path:
        return self.root / "current.json"

    @property
    def generations_root(self) -> Path:
        return self.root / "generations"


def _provider_snapshot(provider: Provider) -> services.ProviderSnapshot:
    if provider == "hindsight":
        snapshot = services.HINDSIGHT_SNAPSHOT
    elif provider == "supermemory":
        snapshot = services.SUPERMEMORY_SNAPSHOT
    else:
        raise IngestionPreparationError(f"unsupported provider: {provider}")
    if PERSONA == "morgan" and AGENT == "luna":
        return snapshot
    return services.ProviderSnapshot(
        provider=provider, version=snapshot.version,
        volume_name=f"dolphinbench-{provider}-hermes-{AGENT}-{PERSONA}-final-snapshot",
        volume_root=Path(f"/mnt/{provider}-hermes-{AGENT}-{PERSONA}-snapshot"),
    )


def _in_progress_paths(provider: Provider) -> InProgressPaths:
    """Return the provider's mutable checkpoint area on its snapshot Volume."""

    return InProgressPaths(_provider_snapshot(provider).volume_root / PROGRESS_DIRECTORY_NAME)


def identity_for(provider: Provider, mode: Mode) -> IngestionIdentity:
    if mode not in {SMOKE_MODE, FINAL_MODE}:
        raise IngestionPreparationError("mode must be 'smoke' or 'final'")
    names: dict[Provider, dict[Mode, str]] = {
        "hindsight": {
            SMOKE_MODE: SMOKE_HINDSIGHT_BANK,
            FINAL_MODE: FINAL_HINDSIGHT_BANK,
        },
        "supermemory": {
            SMOKE_MODE: SMOKE_SUPERMEMORY_CONTAINER,
            FINAL_MODE: FINAL_SUPERMEMORY_CONTAINER,
        },
    }
    try:
        return IngestionIdentity(provider=provider, mode=mode, name=names[provider][mode])
    except KeyError as exc:
        raise IngestionPreparationError(f"unsupported provider: {provider}") from exc


def _source_inventory(provider: Provider, corpus_path: Path, source_checkpoint: str) -> tuple[str, int]:
    if provider == "hindsight":
        documents, corpus_sha256 = ingest_hindsight.load_source_documents(
            corpus_path,
            persona=PERSONA,
            source_checkpoint=source_checkpoint,
        )
        return corpus_sha256, len(documents)
    items, corpus_sha256 = ingest_supermemory.load_corpus(corpus_path, persona=PERSONA)
    return corpus_sha256, len(items)


def _expected_plugin_source_ids(corpus_path: Path) -> list[str]:
    payload = yaml.safe_load(corpus_path.read_text(encoding="utf-8")) or {}
    source_ids: list[str] = []
    for session in payload.get("sessions") or []:
        session_id = str(session.get("id") or "")
        if not session_id:
            raise IngestionPreparationError("source session has no id")
        for message_index, message in enumerate(session.get("messages") or []):
            if not isinstance(message, str):
                raise IngestionPreparationError(
                    f"source message {session_id}:{message_index} is not text"
                )
            source_ids.append(f"{session_id}:{message_index}")
    if len(source_ids) != len(set(source_ids)):
        raise IngestionPreparationError("source corpus contains repeated message ids")
    return source_ids


def _read_plugin_receipts(path: Path, provider: Provider) -> list[str]:
    if not path.is_file():
        return []
    source_ids: list[str] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IngestionPreparationError(
                f"invalid Hermes plugin receipt at line {line_number}: {exc}"
            ) from exc
        if (
            not isinstance(record, dict)
            or record.get("provider") != provider
            or record.get("operation_kind") != "automatic_sync_turn"
            or not isinstance(record.get("seed_item_id"), str)
        ):
            raise IngestionPreparationError(
                f"invalid {provider} Hermes plugin receipt at line {line_number}"
            )
        source_ids.append(record["seed_item_id"])
    if len(source_ids) != len(set(source_ids)):
        raise IngestionPreparationError(
            f"duplicate {provider} Hermes plugin receipt"
        )
    return source_ids


def _write_plugin_ingestion_receipt(
    *, provider: Provider, plan: IngestionPlan, plugin_receipt_path: Path,
    destination: Path,
) -> dict[str, Any]:
    expected = _expected_plugin_source_ids(plan.corpus_path)
    completed = _read_plugin_receipts(plugin_receipt_path, provider)
    unexpected = sorted(set(completed) - set(expected))
    if unexpected:
        raise IngestionPreparationError(
            f"{provider} wrote an unexpected source id: {unexpected[0]}"
        )
    receipt = {
        "schema_version": PLUGIN_RECEIPT_SCHEMA_VERSION,
        "ingestion_path": "hermes_official_memory_plugin",
        "agent_image": os.environ.get("DOLPHINBENCH_AGENT_IMAGE"),
        "provider": provider,
        "persona": PERSONA,
        "identity": plan.identity.name,
        "source_checkpoint": plan.source_checkpoint,
        "corpus_sha256": plan.corpus_sha256,
        "source_count": len(expected),
        "completed_source_ids": completed,
        "status": "completed" if len(completed) == len(expected) else "paused",
    }
    if AGENT != "luna":
        receipt["agent"] = {"runtime": "hermes", "model": AGENT_MODEL,
                            "provider": "openrouter", "api_mode": "chat_completions",
                            "context_length": AGENT_CONTEXT_LENGTH, "reasoning_effort": "high"}
    _atomic_write_json(destination, receipt)
    return receipt


def bridge_contract_error(provider: Provider) -> str | None:
    """Return a local incompatibility that would invalidate a paid final run."""

    if provider == "hindsight":
        sample = ingest_hindsight._new_state(
            bank_id="sample",
            persona=PERSONA,
            corpus_path=Path("/tmp/sample.yaml"),
            corpus_sha256="0" * 64,
            source_checkpoint="sample",
            batch_size=1,
            documents=[],
        )
        if "provider" not in sample:
            return (
                "bridge_external_seed requires receipt.provider='hindsight', but "
                "ingest_hindsight._new_state does not write provider"
            )
    if (
        provider == "supermemory"
        and ingest_supermemory.RECEIPT_VERSION
        != bridge_external_seed.SUPERMEMORY_RECEIPT_VERSION
    ):
        return (
            "bridge_external_seed and ingest_supermemory expect different receipt versions: "
            f"ingest_supermemory writes receipt_version={ingest_supermemory.RECEIPT_VERSION}"
        )
    return None


def require_final_bridge_contract(provider: Provider) -> None:
    error = bridge_contract_error(provider)
    if error:
        raise IngestionPreparationError(
            f"cannot start paid final {provider} ingestion: {error}"
        )


def build_plan(
    *,
    provider: Provider,
    mode: Mode,
    corpus_path: Path = DEFAULT_CORPUS,
    source_checkpoint: str = FINAL_SOURCE_CHECKPOINT,
) -> IngestionPlan:
    """Build a local, no-network plan from immutable source files."""

    corpus_path = corpus_path.expanduser().resolve()
    if not corpus_path.is_file():
        raise IngestionPreparationError(f"corpus does not exist: {corpus_path}")
    identity = identity_for(provider, mode)
    corpus_sha256, source_count = _source_inventory(provider, corpus_path, source_checkpoint)
    if mode == SMOKE_MODE and source_count > SMOKE_SOURCE_LIMIT:
        with tempfile.TemporaryDirectory(prefix="dolphinbench-memory-plan-") as raw:
            smoke_corpus = _write_smoke_corpus(
                corpus_path,
                Path(raw) / "smoke-life_sim.yaml",
            )
            corpus_sha256, source_count = _source_inventory(
                provider, smoke_corpus, source_checkpoint,
            )
    expected_count = {"alex": 5011, "riley": 5128}.get(PERSONA, bridge_external_seed.EXPECTED_MESSAGES)
    if mode == FINAL_MODE and source_count != expected_count:
        raise IngestionPreparationError(
            f"final {PERSONA} {provider} ingestion requires {expected_count} source messages; found {source_count}"
        )
    if mode == FINAL_MODE and PERSONA == "alex":
        if corpus_sha256 != "eb209e56e18b3b75d9d45a4a1b95433f60fab17c3d3f46ee1809bfa62d6b6b02":
            raise IngestionPreparationError("Alex corpus does not match the accepted final checkpoint")
        if source_checkpoint != FINAL_SOURCE_CHECKPOINT:
            raise IngestionPreparationError("Alex source checkpoint identity changed")
    if mode == FINAL_MODE and PERSONA == "riley":
        if corpus_sha256 != "569a0c4d7bde3bfef387d2b8688c7f732c6fda6cdc64e6942d3cc9252515da8a":
            raise IngestionPreparationError("Riley corpus does not match the accepted final checkpoint")
        if source_checkpoint != FINAL_SOURCE_CHECKPOINT:
            raise IngestionPreparationError("Riley source checkpoint identity changed")
    snapshot = _provider_snapshot(provider)
    publishes_final_snapshot = mode == FINAL_MODE
    return IngestionPlan(
        provider=provider,
        mode=mode,
        identity=identity,
        corpus_path=corpus_path,
        corpus_sha256=corpus_sha256,
        source_count=source_count,
        source_checkpoint=source_checkpoint,
        snapshot_volume=snapshot.volume_name if publishes_final_snapshot else "",
        archive_path=str(snapshot.archive_path) if publishes_final_snapshot else "temporary container only",
        checksum_path=str(snapshot.checksum_path) if publishes_final_snapshot else "temporary container only",
        receipt_path=str(snapshot.receipt_path) if publishes_final_snapshot else "temporary container only",
        bridge_contract_error=bridge_contract_error(provider) if mode == FINAL_MODE else None,
    )


def assert_final_snapshot_is_empty(snapshot: services.ProviderSnapshot) -> None:
    """Refuse to overwrite any completed final-ingestion evidence."""

    occupied = [
        path.name
        for path in (snapshot.archive_path, snapshot.checksum_path, snapshot.receipt_path)
        if path.exists()
    ]
    if occupied:
        raise IngestionPreparationError(
            f"final {snapshot.provider} snapshot already has {', '.join(occupied)}; refusing to overwrite it"
        )


def _write_smoke_corpus(source: Path, destination: Path, *, source_limit: int = SMOKE_SOURCE_LIMIT) -> Path:
    """Write a small chronological corpus for a temporary paid provider smoke."""

    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise IngestionPreparationError(f"cannot read smoke source corpus: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), list):
        raise IngestionPreparationError("corpus must contain a sessions list")
    selected: list[dict[str, Any]] = []
    used = 0
    for raw_session in payload["sessions"]:
        if used >= source_limit:
            break
        if not isinstance(raw_session, dict) or not isinstance(raw_session.get("messages"), list):
            raise IngestionPreparationError("every smoke source session needs a messages list")
        session = copy.deepcopy(raw_session)
        available = source_limit - used
        session["messages"] = session["messages"][:available]
        if session["messages"]:
            selected.append(session)
            used += len(session["messages"])
    if used != source_limit:
        raise IngestionPreparationError(
            f"corpus contains only {used} source messages; smoke requires {source_limit}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(yaml.safe_dump({**payload, "sessions": selected}, sort_keys=False), encoding="utf-8")
    return destination


def _first_message(corpus_path: Path) -> str:
    payload = yaml.safe_load(corpus_path.read_text(encoding="utf-8"))
    for session in payload.get("sessions", []):
        for message in session.get("messages", []):
            if isinstance(message, str) and message.strip():
                return message.strip()
    raise IngestionPreparationError("smoke corpus contains no non-empty message")


def _supermemory_search_results(
    store: Any,
    *,
    container_tag: str,
    query: str = SUPERMEMORY_SMOKE_QUERY,
) -> list[Any]:
    """Ask a short question whose answer appears in the smoke corpus."""

    response = store._client.search.memories(
        q=query,
        container_tag=container_tag,
        limit=5,
        rerank=False,
        rewrite_query=False,
        search_mode="hybrid",
    )
    if isinstance(response, Mapping):
        return list(response.get("results", []) or [])
    return list(getattr(response, "results", []) or [])


def validate_completed_receipt(
    *,
    provider: Provider,
    receipt_path: Path,
    corpus_path: Path,
    corpus_sha256: str,
    source_count: int,
    final: bool,
) -> dict[str, Any]:
    """Validate source coverage before a receipt can enter a snapshot Volume."""

    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IngestionPreparationError(f"cannot read completed {provider} receipt: {exc}") from exc
    if receipt.get("ingestion_path") == "hermes_official_memory_plugin":
        expected_ids = _expected_plugin_source_ids(corpus_path)
        completed_ids = receipt.get("completed_source_ids")
        expected = {
            "schema_version": PLUGIN_RECEIPT_SCHEMA_VERSION,
            "provider": provider,
            "persona": PERSONA,
            "identity": identity_for(provider, FINAL_MODE).name,
            "corpus_sha256": corpus_sha256,
            "source_count": source_count,
            "status": "completed",
        }
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise IngestionPreparationError(
                f"{provider} receipt does not describe the completed Hermes plugin ingestion"
            )
        if completed_ids != expected_ids:
            raise IngestionPreparationError(
                f"{provider} receipt does not contain every source message exactly once and in order"
            )
        return receipt
    if provider == "hindsight":
        if not isinstance(receipt, dict) or receipt.get("hindsight_version") != ingest_hindsight.HINDSIGHT_VERSION:
            raise IngestionPreparationError("receipt is not a Hindsight receipt")
        if receipt.get("status") != "completed" or receipt.get("documents_total") != source_count:
            raise IngestionPreparationError("Hindsight receipt is not complete for every source message")
        if receipt.get("corpus_sha256") != corpus_sha256:
            raise IngestionPreparationError("Hindsight receipt corpus hash does not match the planned corpus")
        verification = receipt.get("verification")
        if not isinstance(verification, dict) or verification.get("actual_count") != source_count:
            raise IngestionPreparationError("Hindsight receipt does not verify the expected document count")
    else:
        if not isinstance(receipt, dict) or receipt.get("provider") != "supermemory":
            raise IngestionPreparationError("receipt is not a Supermemory receipt")
        corpus = receipt.get("corpus")
        if (
            receipt.get("receipt_version") != ingest_supermemory.RECEIPT_VERSION
            or receipt.get("status") != "complete"
            or not isinstance(corpus, dict)
        ):
            raise IngestionPreparationError("Supermemory receipt is not complete")
        if corpus.get("sha256") != corpus_sha256 or corpus.get("source_count") != source_count:
            raise IngestionPreparationError("Supermemory receipt corpus does not match the planned corpus")
        sources = receipt.get("sources")
        if not isinstance(sources, dict) or len(sources) != source_count:
            raise IngestionPreparationError("Supermemory receipt does not cover every source message")
        if any(not isinstance(item, dict) or item.get("status") != "complete" for item in sources.values()):
            raise IngestionPreparationError("Supermemory receipt contains an incomplete source message")
    if final:
        require_final_bridge_contract(provider)
        # This is the same full final-corpus coverage validation used when the
        # credential-free evaluation profile is later created.
        _, source_ids = bridge_external_seed._corpus_documents(corpus_path)
        bridge_external_seed._validate_receipt(receipt_path, provider, corpus_path, source_ids)
    return receipt


def _safe_archive_members(runtime_root: Path, provider: Provider) -> list[Path]:
    forbidden = {".instance.lock"} if provider == "supermemory" else set()
    members: list[Path] = []
    for path in (runtime_root, *runtime_root.rglob("*")):
        if path != runtime_root and path.name in forbidden:
            continue
        if provider == "supermemory" and path == runtime_root / services.SUPERMEMORY_ENGINE_PID_PATH:
            continue
        mode = path.lstat().st_mode
        if stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            members.append(path)
    if not any(path != runtime_root and not path.is_dir() for path in members):
        raise IngestionPreparationError(f"{provider} local runtime has no state to snapshot")
    return sorted(
        members,
        key=lambda path: (
            len(path.relative_to(runtime_root).parts) if path != runtime_root else -1,
            str(path),
        ),
    )


def create_snapshot_archive(*, provider: Provider, runtime_root: Path, destination: Path) -> str:
    """Create a compressed snapshot after the provider has stopped cleanly."""

    runtime_root = runtime_root.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as archive:
        for member in _safe_archive_members(runtime_root, provider):
            arcname = "." if member == runtime_root else str(member.relative_to(runtime_root))
            archive.add(member, arcname=arcname, recursive=False)
    return services._sha256_file(destination)


def _create_hermes_progress_archive(runtime_root: Path, destination: Path) -> str:
    """Save the Hermes files that are needed to resume provider ingestion."""

    names = (
        "hermes-home",
        "hermes-seed-results.json",
        "hermes-seed-results.hindsight_seed_receipts.jsonl",
        "ingestion-receipt.json",
        "mock-state.json",
        "mock-calls.jsonl",
        "mock-tools.json",
        "startup-gate.json",
        "startup-documents.json",
        "traces",
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as archive:
        found = False
        for name in names:
            path = runtime_root / name
            if path.exists():
                archive.add(path, arcname=name)
                found = True
        if not found:
            raise IngestionPreparationError("Hindsight checkpoint has no Hermes progress to save")
    return services._sha256_file(destination)


def _hindsight_admin_environment() -> dict[str, str]:
    environment = services._hindsight_environment()
    environment.update({
        "HINDSIGHT_API_DATABASE_URL": "pg0",
        "HINDSIGHT_API_DATABASE_SCHEMA": "public",
    })
    return environment


def _run_hindsight_admin(*arguments: str) -> None:
    completed = subprocess.run(
        ["hindsight-admin", *arguments],
        env=_hindsight_admin_environment(),
        text=True,
        capture_output=True,
        check=False,
        user=1000,
        group=1000,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout)[-4000:]
        raise IngestionPreparationError(f"hindsight-admin failed: {detail}")


def _align_hindsight_history_sequences() -> list[dict[str, Any]]:
    """Repair and verify restored Hindsight counters before its API starts."""

    completed = subprocess.run(
        ["/app/api/.venv/bin/python", "-m", "reference.memory.hindsight"],
        env=_hindsight_admin_environment(),
        text=True,
        capture_output=True,
        check=False,
        user=1000,
        group=1000,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout)[-4000:]
        raise IngestionPreparationError(
            f"Hindsight restored ID counters could not be repaired: {detail}"
        )
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        rows = payload["history_sequence_recovery"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise IngestionPreparationError(
            "Hindsight counter repair returned no verifiable result"
        ) from exc
    expected_tables = {"observation_history", "mental_model_history"}
    if (
        not isinstance(rows, list)
        or len(rows) != len(expected_tables)
        or {row.get("table") for row in rows if isinstance(row, Mapping)} != expected_tables
        or not all(isinstance(row, Mapping) and row.get("verified") is True for row in rows)
    ):
        raise IngestionPreparationError(
            "Hindsight counter repair did not verify both history tables"
        )
    print(json.dumps(payload, sort_keys=True), flush=True)
    return rows


def _hindsight_operation_id(operation: Mapping[str, Any]) -> str:
    operation_id = operation.get("operation_id") or operation.get("id")
    if not isinstance(operation_id, str) or not operation_id:
        raise IngestionPreparationError("Hindsight returned an operation without an ID")
    return operation_id


def _is_hindsight_sequence_failure(operation: Mapping[str, Any]) -> bool:
    operation_type = operation.get("operation_type") or operation.get("task_type")
    error = str(operation.get("error_message") or "")
    return (
        operation_type == "consolidation"
        and "duplicate key value violates unique constraint" in error
        and any(
            constraint in error
            for constraint in (
                "observation_history_pkey",
                "mental_model_history_pkey",
            )
        )
    )


def _wait_for_clean_hindsight_operations(
    api: ingest_hindsight.HindsightApi,
    bank_id: str,
    *,
    timeout_seconds: float = HINDSIGHT_OPERATION_DRAIN_TIMEOUT_SECONDS,
    poll_seconds: float = HINDSIGHT_OPERATION_POLL_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Reuse the runtime-neutral recovery gate while retaining this API's errors."""
    from reference.memory.hindsight import wait_for_clean_operations
    try:
        return wait_for_clean_operations(
            api, bank_id, timeout_seconds=timeout_seconds, poll_seconds=poll_seconds,
            retry_batch_size=HINDSIGHT_OPERATION_RETRY_BATCH_SIZE, sleep_fn=sleep_fn,
        )
    except RuntimeError as exc:
        raise IngestionPreparationError(str(exc)) from exc


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically replace a small checkpoint control file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    """Persist a directory entry before another atomic pointer can name it."""

    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IngestionPreparationError(f"cannot read {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise IngestionPreparationError(f"{label} must contain a JSON object")
    return payload


def _validate_in_progress_receipt(
    *,
    provider: Provider,
    receipt_path: Path,
    plan: IngestionPlan,
) -> dict[str, Any]:
    """Check that a saved receipt belongs to this exact unfinished import.

    This is intentionally local-only. Provider reconciliation remains the
    importer's responsibility after the service is restored.
    """

    receipt = _read_json(receipt_path, f"{provider} in-progress receipt")
    if image := os.environ.get("DOLPHINBENCH_AGENT_IMAGE"):
        if receipt.get("agent_image") != image:
            raise IngestionPreparationError("Saved ingestion used a different agent image; resume with its recorded code and image")
    if receipt.get("ingestion_path") == "hermes_official_memory_plugin":
        expected_ids = _expected_plugin_source_ids(plan.corpus_path)
        completed_ids = receipt.get("completed_source_ids")
        required = {
            "schema_version": PLUGIN_RECEIPT_SCHEMA_VERSION,
            "provider": provider,
            "persona": PERSONA,
            "identity": plan.identity.name,
            "source_checkpoint": plan.source_checkpoint,
            "corpus_sha256": plan.corpus_sha256,
            "source_count": plan.source_count,
        }
        if any(receipt.get(key) != value for key, value in required.items()):
            raise IngestionPreparationError(
                f"{provider} in-progress Hermes plugin receipt does not match this run"
            )
        if (
            not isinstance(completed_ids, list)
            or completed_ids != expected_ids[:len(completed_ids)]
            or receipt.get("status") not in {"paused", "completed"}
        ):
            raise IngestionPreparationError(
                f"{provider} in-progress Hermes plugin receipt has invalid source coverage"
            )
        return receipt
    if provider == "hindsight":
        documents, corpus_sha256 = ingest_hindsight.load_source_documents(
            plan.corpus_path,
            persona=PERSONA,
            source_checkpoint=plan.source_checkpoint,
        )
        expected = {
            "schema_version": ingest_hindsight.STATE_VERSION,
            "provider": "hindsight",
            "hindsight_version": ingest_hindsight.HINDSIGHT_VERSION,
            "bank_id": plan.identity.name,
            "persona": PERSONA,
            "corpus_path": str(plan.corpus_path),
            "corpus_sha256": corpus_sha256,
            "source_checkpoint": plan.source_checkpoint,
            "documents_total": len(documents),
        }
        for key, value in expected.items():
            if receipt.get(key) != value:
                raise IngestionPreparationError(
                    f"Hindsight in-progress receipt {key} does not match this final import"
                )
        batch_size = receipt.get("batch_size")
        if not isinstance(batch_size, int) or batch_size < 1:
            raise IngestionPreparationError("Hindsight in-progress receipt has no valid batch size")
        expected_state = ingest_hindsight._new_state(
            bank_id=plan.identity.name,
            persona=PERSONA,
            corpus_path=plan.corpus_path,
            corpus_sha256=corpus_sha256,
            source_checkpoint=plan.source_checkpoint,
            batch_size=batch_size,
            documents=documents,
        )
        batches = receipt.get("batches")
        expected_batches = expected_state["batches"]
        if not isinstance(batches, list) or len(batches) != len(expected_batches):
            raise IngestionPreparationError("Hindsight in-progress receipt has an invalid batch list")
        for batch, expected_batch in zip(batches, expected_batches, strict=True):
            if not isinstance(batch, Mapping) or any(
                batch.get(key) != expected_batch[key]
                for key in ("batch_index", "document_ids", "payload_sha256")
            ):
                raise IngestionPreparationError(
                    "Hindsight in-progress receipt batches do not match the immutable source messages"
                )
            if batch.get("status") == "completed" and set(batch.get("completed_document_ids") or []) != set(batch["document_ids"]):
                raise IngestionPreparationError(
                    "Hindsight in-progress receipt marks a batch complete without all of its documents"
                )
        return receipt

    items, corpus_sha256 = ingest_supermemory.load_corpus(plan.corpus_path, persona=PERSONA)
    expected_ids = {item.source_id for item in items}
    expected_manifest = [ingest_supermemory._manifest_entry(item) for item in items]
    ingest_supermemory._check_receipt(
        receipt,
        persona=PERSONA,
        container_tag=plan.identity.name,
        corpus_path=plan.corpus_path,
        corpus_sha256=corpus_sha256,
        expected_ids=expected_ids,
        expected_manifest=expected_manifest,
    )
    sources = receipt["sources"]
    invalid = [
        source_id
        for source_id, record in sources.items()
        if not isinstance(record, Mapping)
        or record.get("status") not in {"pending", "submitting", "submitted", "retrying", "complete"}
    ]
    if invalid:
        raise IngestionPreparationError(
            f"Supermemory in-progress receipt has invalid source status for {invalid[0]}"
        )
    return receipt


def _remove_checkpoint_entry(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _remove_stale_in_progress_entries(paths: InProgressPaths) -> None:
    """Keep only the generation named by the committed checkpoint pointer."""

    manifest = _read_json(paths.current_path, "in-progress checkpoint pointer")
    current_generation = manifest.get("generation")
    if not isinstance(current_generation, str) or not current_generation.startswith("generation-"):
        raise IngestionPreparationError("in-progress checkpoint pointer has no safe generation name")

    if paths.generations_root.exists():
        for candidate in paths.generations_root.iterdir():
            if candidate.name == current_generation:
                continue
            if candidate.name.startswith("generation-") or candidate.name.endswith(".tmp"):
                _remove_checkpoint_entry(candidate)
    if paths.root.exists():
        for candidate in paths.root.iterdir():
            if candidate in {paths.current_path, paths.generations_root}:
                continue
            if candidate.name.endswith(".tmp"):
                _remove_checkpoint_entry(candidate)


def save_in_progress_checkpoint(
    *,
    provider: Provider,
    runtime_root: Path,
    receipt_path: Path,
    plan: IngestionPlan,
) -> dict[str, Any]:
    """Save a stopped provider database and its exact receipt as one generation.

    A new generation is written before the small ``current.json`` pointer is
    replaced. A container loss can therefore recover the previous generation
    or the complete new one, but never a mixed archive and receipt.
    """

    if plan.mode != FINAL_MODE:
        raise IngestionPreparationError("only final mode has durable in-progress checkpoints")
    assert_final_snapshot_is_empty(_provider_snapshot(provider))
    _validate_in_progress_receipt(provider=provider, receipt_path=receipt_path, plan=plan)
    paths = _in_progress_paths(provider)
    generation = f"generation-{time.time_ns()}-{os.getpid()}"
    staging = paths.root / f".{generation}.tmp"
    destination = paths.generations_root / generation
    if staging.exists() or destination.exists():
        raise IngestionPreparationError("in-progress checkpoint generation already exists")
    staging.mkdir(parents=True)
    try:
        archive = staging / services.SNAPSHOT_ARCHIVE_NAME
        archive_sha256 = create_snapshot_archive(
            provider=provider,
            runtime_root=runtime_root,
            destination=archive,
        )
        _fsync_file(archive)
        saved_receipt = staging / services.INGESTION_RECEIPT_NAME
        shutil.copy2(receipt_path, saved_receipt)
        _fsync_file(saved_receipt)
        receipt_sha256 = services._sha256_file(saved_receipt)
        manifest = {
            "schema_version": PROGRESS_SCHEMA_VERSION,
            "provider": provider,
            "identity": plan.identity.name,
            "source_checkpoint": plan.source_checkpoint,
            "corpus_sha256": plan.corpus_sha256,
            "source_count": plan.source_count,
            "generation": generation,
            "archive": services.SNAPSHOT_ARCHIVE_NAME,
            "archive_sha256": archive_sha256,
            "receipt": services.INGESTION_RECEIPT_NAME,
            "receipt_sha256": receipt_sha256,
        }
        _atomic_write_json(staging / "checkpoint.json", manifest)
        paths.generations_root.mkdir(parents=True, exist_ok=True)
        os.replace(staging, destination)
        _fsync_directory(destination)
        _fsync_directory(paths.generations_root)
        _atomic_write_json(paths.current_path, manifest)
        _remove_stale_in_progress_entries(paths)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {
        "provider": provider,
        "generation": generation,
        "archive_sha256": archive_sha256,
        "receipt_sha256": receipt_sha256,
        "path": str(destination),
    }


def save_hindsight_live_checkpoint(
    *,
    runtime_root: Path,
    receipt_path: Path,
    plan: IngestionPlan,
) -> dict[str, Any]:
    """Save Hindsight and Hermes progress without stopping Hindsight."""

    if plan.provider != "hindsight" or plan.mode != FINAL_MODE:
        raise IngestionPreparationError("live Hindsight checkpoints require final Hindsight ingestion")
    assert_final_snapshot_is_empty(_provider_snapshot("hindsight"))
    _validate_in_progress_receipt(
        provider="hindsight", receipt_path=receipt_path, plan=plan,
    )
    paths = _in_progress_paths("hindsight")
    generation = f"generation-{time.time_ns()}-{os.getpid()}"
    staging = paths.root / f".{generation}.tmp"
    destination = paths.generations_root / generation
    local_backup = runtime_root.parent / f".{generation}.zip"
    if staging.exists() or destination.exists() or local_backup.exists():
        raise IngestionPreparationError("Hindsight checkpoint generation already exists")
    staging.mkdir(parents=True)
    try:
        _backup_hindsight_with_retry(local_backup)
        database_backup = staging / HINDSIGHT_DATABASE_BACKUP_NAME
        shutil.copy2(local_backup, database_backup)
        _fsync_file(database_backup)
        database_sha256 = services._sha256_file(database_backup)

        hermes_archive = staging / HERMES_PROGRESS_ARCHIVE_NAME
        hermes_sha256 = _create_hermes_progress_archive(runtime_root, hermes_archive)
        _fsync_file(hermes_archive)

        saved_receipt = staging / services.INGESTION_RECEIPT_NAME
        shutil.copy2(receipt_path, saved_receipt)
        _fsync_file(saved_receipt)
        receipt_sha256 = services._sha256_file(saved_receipt)
        manifest = {
            "schema_version": PROGRESS_SCHEMA_VERSION,
            "checkpoint_kind": HINDSIGHT_LIVE_CHECKPOINT_KIND,
            "provider": "hindsight",
            "identity": plan.identity.name,
            "source_checkpoint": plan.source_checkpoint,
            "corpus_sha256": plan.corpus_sha256,
            "source_count": plan.source_count,
            "generation": generation,
            "database_backup": HINDSIGHT_DATABASE_BACKUP_NAME,
            "database_sha256": database_sha256,
            "hermes_archive": HERMES_PROGRESS_ARCHIVE_NAME,
            "hermes_sha256": hermes_sha256,
            "receipt": services.INGESTION_RECEIPT_NAME,
            "receipt_sha256": receipt_sha256,
        }
        _atomic_write_json(staging / "checkpoint.json", manifest)
        paths.generations_root.mkdir(parents=True, exist_ok=True)
        os.replace(staging, destination)
        _fsync_directory(destination)
        _fsync_directory(paths.generations_root)
        _atomic_write_json(paths.current_path, manifest)
        _remove_stale_in_progress_entries(paths)
    finally:
        local_backup.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging)
    return {
        "provider": "hindsight",
        "generation": generation,
        "database_sha256": database_sha256,
        "hermes_sha256": hermes_sha256,
        "receipt_sha256": receipt_sha256,
        "path": str(destination),
    }


def _backup_hindsight_with_retry(destination: Path) -> None:
    for attempt in range(3):
        try:
            _run_hindsight_admin("backup", str(destination), "--schema", "public")
            return
        except (OSError, IngestionPreparationError):
            if attempt == 2:
                raise
            destination.unlink(missing_ok=True)
            time.sleep(attempt + 1)


def _preserve_hindsight_failure(*, process, error: Exception, runtime_root: Path,
                                commit: Callable[[], None]) -> None:
    """Keep uncertain progress separate from the last approved resume checkpoint."""
    destination = _provider_snapshot("hindsight").volume_root / "failed-runs" / f"failure-{time.time_ns()}"
    evidence = {"provider": "hindsight", "error_type": type(error).__name__,
                "error": str(error)[-4000:], "eligible_for_automatic_resume": False}
    try:
        destination.mkdir(parents=True)
        _atomic_write_json(destination / "failure.json", evidence)
        commit()
        try:
            evidence["hermes_sha256"] = _create_hermes_progress_archive(
                runtime_root, destination / HERMES_PROGRESS_ARCHIVE_NAME)
        except Exception as archive_error:
            evidence["hermes_archive_error"] = str(archive_error)
        try:
            if process.poll() is not None:
                raise IngestionPreparationError("Hindsight exited; live backup was not attempted")
            database = destination / HINDSIGHT_DATABASE_BACKUP_NAME
            local = runtime_root.parent / f".{destination.name}.zip"
            try:
                _backup_hindsight_with_retry(local)
                shutil.copy2(local, database)
                evidence["database_sha256"] = services._sha256_file(database)
            finally:
                local.unlink(missing_ok=True)
        except Exception as backup_error:
            evidence["database_backup_error"] = str(backup_error)
        _atomic_write_json(destination / "failure.json", evidence)
        commit()
    except Exception as preservation_error:
        evidence["preservation_error"] = str(preservation_error)
    print(json.dumps({"hindsight_failure": evidence}), flush=True)


def restore_in_progress_checkpoint(
    *,
    provider: Provider,
    runtime_root: Path,
    receipt_path: Path,
    plan: IngestionPlan,
) -> bool:
    """Restore only a verified checkpoint for this exact planned final import."""

    paths = _in_progress_paths(provider)
    if not paths.current_path.exists():
        return False
    manifest = _read_json(paths.current_path, f"{provider} in-progress checkpoint pointer")
    required = {
        "schema_version": PROGRESS_SCHEMA_VERSION,
        "provider": provider,
        "identity": plan.identity.name,
        "source_checkpoint": plan.source_checkpoint,
        "corpus_sha256": plan.corpus_sha256,
        "source_count": plan.source_count,
    }
    for key, value in required.items():
        if manifest.get(key) != value:
            raise IngestionPreparationError(
                f"{provider} in-progress checkpoint {key} does not match this final import"
            )
    generation = manifest.get("generation")
    if not isinstance(generation, str) or not generation.startswith("generation-") or "/" in generation:
        raise IngestionPreparationError(f"{provider} in-progress checkpoint has an unsafe generation name")
    directory = paths.generations_root / generation
    archived_manifest = _read_json(directory / "checkpoint.json", f"{provider} in-progress checkpoint")
    if archived_manifest != manifest:
        raise IngestionPreparationError(f"{provider} in-progress checkpoint pointer does not match its generation")
    if manifest.get("checkpoint_kind") == HINDSIGHT_LIVE_CHECKPOINT_KIND:
        if provider != "hindsight":
            raise IngestionPreparationError("only Hindsight can use a live database checkpoint")
        database_backup = directory / HINDSIGHT_DATABASE_BACKUP_NAME
        hermes_archive = directory / HERMES_PROGRESS_ARCHIVE_NAME
        saved_receipt = directory / services.INGESTION_RECEIPT_NAME
        checks = (
            (database_backup, manifest.get("database_sha256"), "database backup"),
            (hermes_archive, manifest.get("hermes_sha256"), "Hermes progress archive"),
            (saved_receipt, manifest.get("receipt_sha256"), "receipt"),
        )
        for path, expected_sha256, label in checks:
            if not path.is_file():
                raise IngestionPreparationError(f"Hindsight live checkpoint has no {label}")
            if services._sha256_file(path) != expected_sha256:
                raise IngestionPreparationError(
                    f"Hindsight live checkpoint {label} checksum does not match"
                )
        _validate_in_progress_receipt(
            provider="hindsight", receipt_path=saved_receipt, plan=plan,
        )
        _restore_local_archive(hermes_archive, runtime_root)
        subprocess.run(
            ["chown", "-R", "1000:1000", str(runtime_root)], check=True,
        )
        local_backup = runtime_root.parent / f".{generation}-restore.zip"
        try:
            shutil.copy2(database_backup, local_backup)
            _run_hindsight_admin("run-db-migration", "--schema", "public")
            _run_hindsight_admin(
                "restore", str(local_backup), "--schema", "public", "--yes",
            )
            _align_hindsight_history_sequences()
        finally:
            local_backup.unlink(missing_ok=True)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(saved_receipt, receipt_path)
        return True
    if (
        manifest.get("archive") != services.SNAPSHOT_ARCHIVE_NAME
        or manifest.get("receipt") != services.INGESTION_RECEIPT_NAME
    ):
        raise IngestionPreparationError(f"{provider} in-progress checkpoint has unexpected file names")
    archive = directory / services.SNAPSHOT_ARCHIVE_NAME
    saved_receipt = directory / services.INGESTION_RECEIPT_NAME
    if not archive.is_file() or not saved_receipt.is_file():
        raise IngestionPreparationError(f"{provider} in-progress checkpoint is incomplete")
    if services._sha256_file(archive) != manifest.get("archive_sha256"):
        raise IngestionPreparationError(f"{provider} in-progress checkpoint archive checksum does not match")
    if services._sha256_file(saved_receipt) != manifest.get("receipt_sha256"):
        raise IngestionPreparationError(f"{provider} in-progress checkpoint receipt checksum does not match")
    _validate_in_progress_receipt(provider=provider, receipt_path=saved_receipt, plan=plan)
    _restore_local_archive(archive, runtime_root)
    if provider == "supermemory":
        services.discard_restored_supermemory_pid(runtime_root)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = receipt_path.with_name(f".{receipt_path.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(saved_receipt, temporary)
        os.replace(temporary, receipt_path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def remove_in_progress_checkpoint(provider: Provider) -> None:
    """Remove mutable recovery files only after immutable final evidence exists."""

    root = _in_progress_paths(provider).root
    if root.exists():
        shutil.rmtree(root)


def _restore_local_archive(archive: Path, destination: Path) -> None:
    """Restore a smoke archive after checking every member path."""

    with tarfile.open(archive, "r:gz") as bundle:
        root_member: tarfile.TarInfo | None = None
        for member in bundle.getmembers():
            relative = Path(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise IngestionPreparationError(
                    f"smoke snapshot contains an unsafe archive member: {member.name}"
                )
            if member.name == ".":
                root_member = member
        shutil.rmtree(destination, ignore_errors=True)
        destination.mkdir(parents=True)
        try:
            bundle.extractall(destination, filter=services._snapshot_extract_filter)
            if root_member is not None:
                destination.chmod(root_member.mode & 0o777)
        except (OSError, tarfile.TarError) as exc:
            raise IngestionPreparationError(f"cannot restore smoke snapshot: {exc}") from exc


def publish_final_snapshot(
    *,
    provider: Provider,
    runtime_root: Path,
    receipt_path: Path,
    plan: IngestionPlan,
) -> dict[str, Any]:
    """Atomically publish exactly one verified final archive and receipt."""

    if plan.mode != FINAL_MODE:
        raise IngestionPreparationError("only final mode can publish a snapshot")
    snapshot = _provider_snapshot(provider)
    assert_final_snapshot_is_empty(snapshot)
    validate_completed_receipt(
        provider=provider,
        receipt_path=receipt_path,
        corpus_path=plan.corpus_path,
        corpus_sha256=plan.corpus_sha256,
        source_count=plan.source_count,
        final=True,
    )
    with tempfile.TemporaryDirectory(prefix=f"dolphinbench-{provider}-snapshot-") as raw:
        archive = Path(raw) / services.SNAPSHOT_ARCHIVE_NAME
        digest = create_snapshot_archive(provider=provider, runtime_root=runtime_root, destination=archive)
        temporary_archive = snapshot.archive_path.with_suffix(".tar.gz.tmp")
        temporary_checksum = snapshot.checksum_path.with_suffix(".sha256.tmp")
        temporary_receipt = snapshot.receipt_path.with_suffix(".json.tmp")
        snapshot.volume_root.mkdir(parents=True, exist_ok=True)
        if any(path.exists() for path in (temporary_archive, temporary_checksum, temporary_receipt)):
            raise IngestionPreparationError("snapshot Volume has unfinished temporary files; refusing to overwrite them")
        shutil.copy2(archive, temporary_archive)
        temporary_checksum.write_text(f"{digest}  {services.SNAPSHOT_ARCHIVE_NAME}\n", encoding="ascii")
        shutil.copy2(receipt_path, temporary_receipt)
        os.replace(temporary_archive, snapshot.archive_path)
        os.replace(temporary_checksum, snapshot.checksum_path)
        os.replace(temporary_receipt, snapshot.receipt_path)
    remove_in_progress_checkpoint(provider)
    return {
        "provider": provider,
        "archive_sha256": digest,
        "archive": str(snapshot.archive_path),
        "checksum": str(snapshot.checksum_path),
        "receipt": str(snapshot.receipt_path),
    }


def _stop_service(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=60)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait(timeout=10)
        raise IngestionPreparationError("memory service did not stop cleanly before snapshot") from exc


def _stop_hindsight_service(process: subprocess.Popen[bytes]) -> None:
    _stop_service(process)
    # pg0's PostgreSQL daemon is not necessarily a child of the API launcher.
    code = (
        "import pg0\n"
        "if pg0.info('hindsight').running: pg0.stop('hindsight')\n"
        "if pg0.info('hindsight').running: raise RuntimeError('PostgreSQL is still running')\n"
    )
    result = subprocess.run(
        ["/app/api/.venv/bin/python", "-c", code], env=_hindsight_admin_environment(),
        user=1000, group=1000, capture_output=True, text=True, timeout=90,
    )
    if result.returncode:
        raise IngestionPreparationError("Hindsight database shutdown failed; raw snapshot refused")


def _active_process_group_members(process_group: int) -> list[int]:
    members: list[int] = []
    for candidate in Path("/proc").glob("[0-9]*"):
        try:
            stat = candidate.joinpath("stat").read_text(encoding="utf-8")
            fields = stat[stat.rfind(")") + 2 :].split()
            state = fields[0]
            group = int(fields[2])
        except (OSError, ValueError, IndexError):
            continue
        if group == process_group and state != "Z":
            members.append(int(candidate.name))
    return sorted(members)


def _stop_supermemory_service(process: subprocess.Popen[bytes]) -> None:
    """Stop Supermemory and every child before its data directory is copied."""

    process_group = process.pid
    # Supermemory drains saved tasks before stopping its engine. Signalling the
    # whole group here would stop that engine while the parent still needs it.
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 60
    children_signalled = False
    while time.monotonic() < deadline:
        if process.poll() is not None and not children_signalled:
            try:
                os.killpg(process_group, signal.SIGTERM)
            except ProcessLookupError:
                pass
            children_signalled = True
        if not _active_process_group_members(process_group):
            if process.poll() is None:
                process.wait(timeout=1)
            if process.returncode not in (0, -signal.SIGTERM):
                raise IngestionPreparationError(
                    f"Supermemory exited with code {process.returncode}; refusing a clean snapshot"
                )
            return
        time.sleep(0.1)
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    raise IngestionPreparationError(
        "Supermemory process group did not stop cleanly before snapshot"
    )


def _require_supermemory_running(process: subprocess.Popen[bytes]) -> None:
    returncode = process.poll()
    if returncode is not None:
        raise IngestionPreparationError(
            f"Supermemory exited with code {returncode}; "
            f"see {services.SUPERMEMORY_SERVER_LOG_PATH}"
        )


def _checkpoint_and_restart_supermemory(
    *,
    process: subprocess.Popen[bytes],
    runtime_root: Path,
    receipt_path: Path,
    plan: IngestionPlan,
    commit: Callable[[], None],
) -> tuple[subprocess.Popen[bytes], dict[str, Any]]:
    """Stop, save, and restart Supermemory at a completed group boundary."""

    _require_supermemory_running(process)
    _stop_supermemory_service(process)
    checkpoint = save_in_progress_checkpoint(
        provider="supermemory",
        runtime_root=runtime_root,
        receipt_path=receipt_path,
        plan=plan,
    )
    commit()
    restarted = services._start_supermemory(require_snapshot=False)
    return restarted, checkpoint


def _record_supermemory_failure(
    *,
    process: subprocess.Popen[bytes] | None,
    error: BaseException,
    receipt_path: Path,
    commit: Callable[[], None],
    runtime_root: Path | None = None,
    shutdown_error: BaseException | None = None,
    shutdown_diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Preserve exit evidence without replacing the last usable checkpoint."""

    snapshot = _provider_snapshot("supermemory")
    failures_root = snapshot.volume_root / "failed-runs"
    generation = f"failure-{time.time_ns()}-{os.getpid()}"
    staging = failures_root / f".{generation}.tmp"
    destination = failures_root / generation
    staging.mkdir(parents=True, exist_ok=False)
    try:
        metadata: dict[str, Any] = {
            "schema_version": 1,
            "provider": "supermemory",
            "error_type": type(error).__name__,
            "error": str(error)[-4000:],
            "process_returncode": process.poll() if process is not None else None,
        }
        if shutdown_error is not None:
            metadata["shutdown_error"] = str(shutdown_error)[-4000:]
        if shutdown_diagnostics is not None:
            metadata["shutdown_diagnostics"] = dict(shutdown_diagnostics)
        if receipt_path.is_file():
            receipt = _read_json(receipt_path, "Supermemory failure receipt")
            metadata["completed_sources"] = _completed_source_count(
                "supermemory", receipt
            )
            metadata["receipt_status"] = receipt.get("status")
        log_path = services.SUPERMEMORY_SERVER_LOG_PATH
        if log_path.is_file():
            saved_log = staging / "supermemory-server.log"
            shutil.copy2(log_path, saved_log)
            _fsync_file(saved_log)
            metadata["server_log"] = saved_log.name
            metadata["server_log_sha256"] = services._sha256_file(saved_log)
        if runtime_root is not None and runtime_root.is_dir():
            # A crashed database is evidence, never an authenticated resume point.
            archive_path = staging / "unverified-runtime.tar.gz"
            try:
                if process is not None and process.poll() is None:
                    raise IngestionPreparationError("Cannot archive a running failed server")
                with tarfile.open(archive_path, "w:gz") as archive:
                    for member in _safe_archive_members(runtime_root, "supermemory"):
                        name = "." if member == runtime_root else str(member.relative_to(runtime_root))
                        archive.add(member, arcname=name, recursive=False)
                _fsync_file(archive_path)
                metadata["unverified_runtime"] = {
                    "archive": archive_path.name,
                    "sha256": services._sha256_file(archive_path),
                    "eligible_for_automatic_resume": False,
                }
            except Exception as archive_error:
                archive_path.unlink(missing_ok=True)
                metadata["unverified_runtime_error"] = str(archive_error)[-2000:]
        _atomic_write_json(staging / "failure.json", metadata)
        failures_root.mkdir(parents=True, exist_ok=True)
        os.replace(staging, destination)
        _fsync_directory(destination)
        _fsync_directory(failures_root)
        commit()
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {**metadata, "path": str(destination)}


def _preserve_supermemory_failure(
    *, process: subprocess.Popen[bytes], error: BaseException,
    receipt_path: Path, runtime_root: Path, commit: Callable[[], None],
) -> bool:
    """Keep the original error even when stopping or archiving also fails."""
    returncode_before_shutdown = process.poll()
    was_running = returncode_before_shutdown is None
    process_group = getattr(process, "pid", None)
    members_before = (
        _active_process_group_members(process_group)
        if isinstance(process_group, int) else []
    )
    cgroup_memory = {}
    for name in ("memory.events", "memory.current", "memory.peak"):
        path = Path("/sys/fs/cgroup") / name
        try:
            cgroup_memory[name] = path.read_text(encoding="utf-8")[:4000]
        except OSError:
            continue
    shutdown_error = None
    try:
        _stop_supermemory_service(process)
    except Exception as exc:
        shutdown_error = exc
    returncode_after_shutdown = process.poll()
    shutdown_diagnostics = {
        "returncode_before_shutdown": returncode_before_shutdown,
        "returncode_after_shutdown": returncode_after_shutdown,
        "forced_kill": bool(
            was_running and shutdown_error is not None
            and returncode_after_shutdown == -signal.SIGKILL
        ),
        "process_group_members_before_shutdown": members_before,
        "cgroup_memory": cgroup_memory,
    }
    try:
        failure = _record_supermemory_failure(
            process=process, error=error, receipt_path=receipt_path,
            runtime_root=runtime_root, commit=commit, shutdown_error=shutdown_error,
            shutdown_diagnostics=shutdown_diagnostics,
        )
        print(json.dumps({"supermemory_failure": failure}, sort_keys=True), flush=True)
    except Exception as evidence_error:
        print(f"Could not preserve Supermemory failure evidence: {evidence_error}",
              file=sys.stderr, flush=True)
    return was_running and shutdown_error is None


def _runtime_corpus(mode: Mode) -> Path:
    if mode == FINAL_MODE:
        return REMOTE_CORPUS
    return _write_smoke_corpus(
        REMOTE_CORPUS,
        REMOTE_RUNTIME_ROOT / "smoke-life_sim.yaml",
    )


def _completed_source_count(provider: Provider, receipt: Mapping[str, Any]) -> int:
    if receipt.get("ingestion_path") == "hermes_official_memory_plugin":
        completed = receipt.get("completed_source_ids")
        if not isinstance(completed, list):
            raise IngestionPreparationError("Hermes plugin receipt has no completed source ids")
        return len(completed)
    if provider == "hindsight":
        batches = receipt.get("batches")
        if not isinstance(batches, list):
            raise IngestionPreparationError("Hindsight receipt has no batch list")
        return sum(
            len(batch.get("document_ids") or [])
            for batch in batches
            if isinstance(batch, Mapping) and batch.get("status") == "completed"
        )
    sources = receipt.get("sources")
    if not isinstance(sources, Mapping):
        raise IngestionPreparationError("Supermemory receipt has no source records")
    return sum(
        1
        for source in sources.values()
        if isinstance(source, Mapping) and source.get("status") == "complete"
    )


def _concise_group_result(
    *,
    provider: Provider,
    mode: Mode,
    plan: IngestionPlan,
    receipt: Mapping[str, Any],
    resumed_from_checkpoint: bool,
    publication: Mapping[str, Any],
    elapsed_seconds: float,
) -> dict[str, Any]:
    checkpoint = publication.get("checkpoint")
    checkpoint_generation = (
        checkpoint.get("generation") if isinstance(checkpoint, Mapping) else None
    )
    final_published = mode == FINAL_MODE and receipt.get("status") in {"completed", "complete"}
    return {
        "provider": provider,
        "mode": mode,
        "source_count": plan.source_count,
        "completed_sources": _completed_source_count(provider, receipt),
        "receipt_status": receipt.get("status"),
        "resumed_from_checkpoint": resumed_from_checkpoint,
        "checkpoint_generation": checkpoint_generation,
        "final_published": final_published,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }


def _run_continuous_groups(
    *,
    provider: Provider,
    source_count: int,
    run_group: Callable[[], Mapping[str, Any]],
    save_progress: Callable[[], None],
    should_pause: Callable[[], bool] = lambda: False,
) -> tuple[Mapping[str, Any], int, bool]:
    """Process 100-message groups and save each incomplete group boundary."""

    groups_completed = 0
    previous_completed_sources = 0
    while True:
        receipt = run_group()
        if receipt.get("provider") != provider:
            raise IngestionPreparationError(
                f"{provider} ingestion group returned the wrong provider receipt"
            )
        completed_sources = _completed_source_count(provider, receipt)
        groups_completed += 1
        if completed_sources <= previous_completed_sources:
            raise IngestionPreparationError(
                f"{provider} final ingestion made no progress after group {groups_completed}"
            )
        if completed_sources > source_count:
            raise IngestionPreparationError(
                f"{provider} recorded more source messages than the corpus contains"
            )
        if completed_sources == source_count:
            if receipt.get("status") != "completed":
                raise IngestionPreparationError(
                    f"{provider} reached every source message without a completed receipt"
                )
            return receipt, groups_completed, False
        save_progress()
        previous_completed_sources = completed_sources
        if should_pause():
            return receipt, groups_completed, True


def _collect_provider_results(calls: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Collect every already-started provider run, even when one has failed."""

    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for provider, call in calls.items():
        try:
            result = call.get()
        except Exception as exc:
            errors[provider] = f"{type(exc).__name__}: {exc}"
            continue
        if isinstance(result, Mapping) and result.get("worker_failed") is True:
            errors[provider] = (
                f"{result.get('error_type', 'RemoteWorkerError')}: "
                f"{result.get('error', 'remote ingestion worker failed')}"
            )
        else:
            results[provider] = result
    return results, errors


def _require_remote_worker_result(provider: Provider, result: Any) -> Mapping[str, Any]:
    """Turn a reported remote worker failure back into a local command error."""

    if not isinstance(result, Mapping):
        raise IngestionPreparationError(f"{provider} worker returned an invalid result")
    if result.get("worker_failed") is True:
        raise IngestionPreparationError(
            f"{provider} worker failed: {result.get('error_type', 'RemoteWorkerError')}: "
            f"{result.get('error', 'remote ingestion worker failed')}"
        )
    return result


def _prepare_hermes_seed_profile(provider: Provider, runtime_root: Path, plan: IngestionPlan) -> tuple[str, dict[str, str]]:
    hermes_home = runtime_root / "hermes-home"
    profile = f"dolphinbench-{provider}-{PERSONA}-final" + (f"-{AGENT}" if AGENT != "luna" else "")
    profile_root = hermes_home / "profiles" / profile
    state_path = runtime_root / "mock-state.json"
    log_path = runtime_root / "mock-calls.jsonl"
    tool_config_path = runtime_root / "mock-tools.json"
    toolsets = ["hermes-cli", "dolphinbench-apps", "memory", "session_search", provider]
    env = {
        "HERMES_HOME": str(hermes_home),
        "AZURE_FOUNDRY_API_KEY": os.environ[services.AZURE_API_KEY_ENV],
        "HERMES_BIN": str(REMOTE_HERMES_BIN),
        "DOLPHINBENCH_HERMES_PROFILE": profile,
        "DOLPHINBENCH_HERMES_TOOLSETS": ",".join(toolsets),
        "ENACT_STATE_PATH": str(state_path),
        "ENACT_LOG_PATH": str(log_path),
        "ENACT_TOOL_CONFIG_PATH": str(tool_config_path),
        "ENACT_RUN_ID": f"{provider}-{PERSONA}-final-ingestion",
    }
    model = {
        "provider": "azure-foundry", "default": AGENT_MODEL,
        "base_url": services.azure_openai_base_url(os.environ[services.AZURE_ENDPOINT_ENV]).rstrip("/"),
        "api_mode": "codex_responses", "context_length": AGENT_CONTEXT_LENGTH,
    }
    if AGENT == "minimax-m3":
        if not os.environ.get("OPENROUTER_API_KEY", "").strip():
            raise IngestionPreparationError("MiniMax ingestion requires OPENROUTER_API_KEY")
        model.update(provider="openrouter", base_url="https://openrouter.ai/api/v1", api_mode="chat_completions")
        env["OPENROUTER_API_KEY"] = os.environ["OPENROUTER_API_KEY"]
        env["ENACT_RUN_ID"] = f"{provider}-{PERSONA}-{AGENT}-final-ingestion"
    if provider == "hindsight":
        # Hindsight can finish a valid retain after the plugin's 120-second
        # default. Keep the ingestion client inside the runner's wider drain.
        env["HINDSIGHT_TIMEOUT"] = str(HINDSIGHT_PLUGIN_TIMEOUT_SECONDS)
    if profile_root.is_dir():
        if AGENT != "luna":
            saved_config = yaml.safe_load((profile_root / "config.yaml").read_text())
            if saved_config.get("model") != model:
                raise IngestionPreparationError("Saved MiniMax profile model settings changed")
        if provider == "supermemory":
            config_path = profile_root / "supermemory.json"
            existing = _read_json(config_path, "Supermemory profile")
            if existing.get("api_timeout") != SUPERMEMORY_PLUGIN_TIMEOUT_SECONDS:
                existing["api_timeout"] = SUPERMEMORY_PLUGIN_TIMEOUT_SECONDS
                _atomic_write_json(config_path, existing)
        return profile, env
    profile_root.mkdir(parents=True)
    mcp_env = {
        "ENACT_PERSONA": PERSONA,
        "ENACT_MOCK_MANIFEST": str(REMOTE_ROOT / f"mock_mcp/manifests/{PERSONA}.yaml"),
        **{key: env[key] for key in (
            "ENACT_STATE_PATH", "ENACT_LOG_PATH", "ENACT_TOOL_CONFIG_PATH", "ENACT_RUN_ID"
        )},
    }
    config: dict[str, Any] = {
        "model": model,
        "memory": {"memory_enabled": True, "user_profile_enabled": True, "provider": provider},
        "toolsets": toolsets,
        "platform_toolsets": {"cli": toolsets},
        "mcp_servers": {
            "dolphinbench-apps": {
                "command": str(REMOTE_MOCK_MCP_PYTHON),
                "args": [str(REMOTE_ROOT / "mock_mcp/server.py")],
                "env": mcp_env,
                "tools": {"resources": False, "prompts": False},
            }
        },
        "agent": {
            "personalities": {"default": (
                "You are Morgan's personal assistant. Respond naturally and complete her request when she makes one."
                if PERSONA == "morgan" else
                f"You are {PERSONA.title()}'s personal assistant. Respond naturally and complete requests."
            )},
            "api_max_retries": 8,
            "retry_rate_limits_until_success": True,
            "reasoning_effort": "high",
            "max_turns": 60,
        },
    }
    if provider == "hindsight":
        config["hindsight"] = {
            "mode": "local_external",
            "api_url": "http://127.0.0.1:8888",
            "bank_id": plan.identity.name,
            "budget": "mid",
            "memory_mode": "hybrid",
            "auto_recall": True,
            "auto_retain": True,
            "retain_async": False,
        }
        (profile_root / "hindsight").mkdir()
        (profile_root / "hindsight/config.json").write_text(
            json.dumps(config["hindsight"], indent=2) + "\n", encoding="utf-8"
        )
    else:
        config["supermemory"] = {
            "container_tag": plan.identity.name,
            "base_url": "http://127.0.0.1:6767",
            "api_timeout": SUPERMEMORY_PLUGIN_TIMEOUT_SECONDS,
            "auto_recall": True,
            "auto_capture": True,
            "max_recall_results": 10,
            "search_mode": "hybrid",
        }
        (profile_root / "supermemory.json").write_text(
            json.dumps(config["supermemory"], indent=2) + "\n", encoding="utf-8"
        )
    (profile_root / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    (profile_root / "SOUL.md").write_text(f"You are {PERSONA.title()}'s personal assistant.\n", encoding="utf-8")
    return profile, env


def _verify_supermemory_recovery_store(
    runtime_root: Path, source_id: str, child_env: Mapping[str, str],
) -> None:
    receipts = runtime_root / "hermes-seed-results.supermemory_seed_receipts.jsonl"
    code = (
        "import collections,json,os,sys\n"
        "from reference.memory.supermemory import SupermemorySdkStore\n"
        "rows=[json.loads(line) for line in open(sys.argv[1]) if line.strip()]\n"
        "tags={r['container_tag'] for r in rows}\n"
        "assert len(tags)==1, 'Expected one saved container tag'\n"
        "store=SupermemorySdkStore(os.environ['SUPERMEMORY_API_KEY'],base_url='http://127.0.0.1:6767')\n"
        "docs=store.list_documents(container_tag=tags.pop())\n"
        "ids=collections.Counter(d['source_id'] for d in docs if d['source_id'])\n"
        "expected={'session-'+r['seed_item_id'] for r in rows}\n"
        "assert all(ids[s]==1 for s in expected), 'Missing or duplicate acknowledged conversation'\n"
        "assert ids['session-'+sys.argv[2]]==0, 'Recovery conversation already exists'\n"
        "print(json.dumps({'acknowledged_verified':len(expected),'documents':len(docs),"
        "'documents_without_custom_id':sum(not d['source_id'] for d in docs),"
        "'duplicate_custom_ids':sum(n-1 for n in ids.values())}))\n"
    )
    result = subprocess.run(
        [str(REMOTE_HERMES_ROOT / ".venv/bin/python"), "-c", code, str(receipts), source_id],
        env=dict(child_env), capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise IngestionPreparationError(f"Supermemory recovery store check failed: {result.stderr[-2000:]}")
    print(result.stdout.strip(), flush=True)


def _recover_plugin_write(
    runtime_root: Path, corpus_path: Path, source_id: str, trace_sha256: str,
    child_env: Mapping[str, str], *, provider: Provider = "hindsight",
    receipt_override: Path | None = None,
) -> None:
    """Repeat only an inspected missing plugin write, using the original turn."""
    if provider not in {"hindsight", "supermemory"}:
        raise IngestionPreparationError("Unsupported recovery provider")
    result_path = runtime_root / "hermes-seed-results.json"
    results = json.loads(result_path.read_text())
    calls = [c for c in results["seed_calls"] if c.get("seed_item_id") == source_id]
    if len(calls) != 1 or not calls[0].get("driver_ok"):
        raise IngestionPreparationError("Recovery needs one saved successful Hermes response")
    call = calls[0]
    trace_path = runtime_root / "traces/seed" / Path(call["trace_path"]).name
    raw = trace_path.read_bytes()
    if not trace_sha256 or hashlib.sha256(raw).hexdigest() != trace_sha256:
        raise IngestionPreparationError("Recovery trace differs from the inspected conversation")
    trace = json.loads(raw)
    messages = trace["messages"]
    users = [m["content"] for m in messages if m["role"] == "user"]
    last = messages[-1]
    if (
        not users
        or any(message not in HERMES_SYNTHETIC_CONTINUATION_MESSAGES for message in users[1:])
        or last["role"] != "assistant"
        or last.get("finish_reason") != "stop"
    ):
        raise IngestionPreparationError(
            "Recovery requires one source message, only known synthetic continuations, and a finished reply"
        )
    session_id = call["hermes_session_id"]
    if trace["session"]["id"] != session_id:
        raise IngestionPreparationError("Recovery session does not match the saved call")
    corpus = yaml.safe_load(corpus_path.read_text())
    source_session = next(s for s in corpus["sessions"] if s["id"] == call["session_id"])
    expected = source_session["messages"][call["message_index"]].strip()
    if source_session.get("narrative_date"):
        expected = f'[{source_session["narrative_date"]}] {expected}'
    if users[0] != expected:
        raise IngestionPreparationError("Recovery message does not match the frozen history")
    receipt_path = receipt_override or result_path.with_suffix(f".{provider}_seed_receipts.jsonl")
    if source_id in _read_plugin_receipts(receipt_path, provider):
        return
    packet_path = runtime_root / f"{provider}-missing-write.json"
    packet_path.write_text(json.dumps({
        "source_id": source_id, "trace_sha256": trace_sha256,
        "session_id": session_id, "profile": results["profile"],
        "user": users[0], "assistant": last["content"],
        "messages": [{"role": m["role"], "content": m["content"]}
                     for m in messages if m["role"] in {"user", "assistant"}],
        "original_attempt": call,
    }, indent=2) + "\n")
    env = dict(child_env)
    env.update({
        "HERMES_HOME": str(runtime_root / "hermes-home/profiles" / results["profile"]),
        "DOLPHINBENCH_SEED_ITEM_ID": source_id,
        f"DOLPHINBENCH_{provider.upper()}_SEED_RECEIPTS": str(receipt_path),
    })
    provider_class = "HindsightMemoryProvider" if provider == "hindsight" else "SupermemoryMemoryProvider"
    write_code = (
        " provider.sync_turn(p['user'],p['assistant'],session_id=p['session_id'])\n"
        if provider == "hindsight" else " provider.on_session_end(p['messages'])\n"
    )
    code = (
        "import json,sys\n"
        f"from plugins.memory.{provider} import {provider_class}\n"
        "p=json.load(open(sys.argv[1]))\n"
        f"provider={provider_class}()\n"
        "provider.initialize(p['session_id'], platform='cli', agent_identity=p['profile'], agent_workspace='hermes')\n"
        "try:\n"
        f"{write_code}"
        "finally:\n"
        " provider.shutdown()\n"
    )
    log_path = runtime_root / f"{provider}-missing-write.log"
    with log_path.open("a") as log:
        completed = subprocess.run(
            [str(REMOTE_HERMES_ROOT / ".venv/bin/python"), "-c", code, str(packet_path)],
            env=env, stdout=log, stderr=log, check=False,
        )
    if completed.returncode or source_id not in _read_plugin_receipts(receipt_path, provider):
        raise IngestionPreparationError(f"Missing {provider} write was not confirmed; see recovery log")
    print(f"Recovered {provider} write for {source_id}; original Hermes response preserved", flush=True)


def _read_supermemory_document(source_id: str, api_key: str) -> dict[str, Any] | None:
    import urllib.error
    import urllib.parse
    import urllib.request

    request = urllib.request.Request(
        "http://127.0.0.1:6767/v3/documents/" + urllib.parse.quote(f"session-{source_id}", safe=""),
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            document = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    normalized = ingest_supermemory.normalize_document(document)
    if normalized["source_id"] != f"session-{source_id}":
        raise IngestionPreparationError("Supermemory direct lookup returned a different custom ID")
    return normalized


def _repair_supermemory_receipt_gap(
    runtime_root: Path, corpus_path: Path, recovery_json: str, child_env: Mapping[str, str],
) -> None:
    recovery = json.loads(recovery_json)
    if not isinstance(recovery, dict) or set(recovery) != {"source_id", "trace_sha256"}:
        raise IngestionPreparationError("Receipt-gap recovery requires one source ID and inspected trace hash")
    source_id = recovery["source_id"]
    receipt_path = runtime_root / "hermes-seed-results.supermemory_seed_receipts.jsonl"
    if source_id not in _read_plugin_receipts(receipt_path, "supermemory"):
        raise IngestionPreparationError("Receipt-gap recovery source has no original receipt")
    original_receipt = receipt_path.read_bytes()
    api_key = child_env["SUPERMEMORY_API_KEY"]
    repair_receipt = runtime_root / "supermemory-receipt-gap-repair.jsonl"
    existing = _read_supermemory_document(source_id, api_key)
    if existing is not None and source_id not in _read_plugin_receipts(repair_receipt, "supermemory"):
        raise IngestionPreparationError("Receipt-gap recovery document already exists; inspect before replay")
    _recover_plugin_write(
        runtime_root, corpus_path, source_id, recovery["trace_sha256"], child_env,
        provider="supermemory", receipt_override=repair_receipt,
    )
    document = _read_supermemory_document(source_id, api_key)
    if document is None or receipt_path.read_bytes() != original_receipt:
        raise IngestionPreparationError("Receipt-gap repair was not verified with the original receipt preserved")
    print(json.dumps({"supermemory_receipt_gap_repaired": source_id, "document_id": document["id"], "original_receipt_preserved": True}), flush=True)


def _startup_gate_remaining(runtime_root: Path, plan: IngestionPlan) -> int | None:
    proof_path = runtime_root / "startup-gate.json"
    completed = _read_plugin_receipts(
        runtime_root / f"hermes-seed-results.{plan.provider}_seed_receipts.jsonl", plan.provider,
    )
    expected = _expected_plugin_source_ids(plan.corpus_path)[:2]
    if proof_path.exists():
        proof = _read_json(proof_path, "startup gate")
        if (proof.get("status") != "passed" or proof.get("identity") != plan.identity.name
                or proof.get("corpus_sha256") != plan.corpus_sha256
                or proof.get("source_ids") != expected or completed[:2] != expected):
            raise IngestionPreparationError("Startup gate proof does not match this ingestion")
        return None
    if len(completed) > 2 or completed != expected[:len(completed)]:
        raise IngestionPreparationError("Startup gate requires the first two sources in order")
    return 2 - len(completed)


def _verify_startup_retrieval(runtime_root: Path, plan: IngestionPlan, *, query: str | None = None) -> dict[str, Any]:
    expected = _expected_plugin_source_ids(plan.corpus_path)[:2]
    if _startup_gate_remaining(runtime_root, plan) != 0:
        raise IngestionPreparationError("Startup retrieval requires exactly two acknowledged sources")
    query = query or _first_message(plan.corpus_path)
    if plan.provider == "hindsight":
        api = ingest_hindsight.HindsightApi(
            "http://127.0.0.1:8888", os.environ[services.HINDSIGHT_API_KEY_ENV],
        )
        _wait_for_clean_hindsight_operations(api, plan.identity.name)
        inventory = _read_json(runtime_root / "startup-documents.json", "startup document inventory")
        actual = api.list_document_ids(plan.identity.name)
        if (inventory.get("identity") != plan.identity.name
                or inventory.get("corpus_sha256") != plan.corpus_sha256
                or actual != set(inventory.get("document_ids", []))
                or not set(expected).issubset(actual)):
            raise IngestionPreparationError("Restored Hindsight startup documents differ from the saved inventory")
        key = os.environ[services.HINDSIGHT_API_KEY_ENV]
        code = (
            "import asyncio,json,sys\n"
            "from hindsight_client import Hindsight\n"
            "p=json.load(sys.stdin)\n"
            "client=Hindsight(base_url='http://127.0.0.1:8888',api_key=p['key'])\n"
            "response=asyncio.run(client.arecall(bank_id=p['container'],query=p['query'],budget='mid'))\n"
            "print(json.dumps({'results':len(getattr(response,'results',[]) or [])}))\n"
        )
    else:
        key = services._supermemory_server_key()
        for source_id in expected:
            document = _read_supermemory_document(source_id, key)
            if document is None or document.get("status") != "done":
                raise IngestionPreparationError("Restored Supermemory startup document is not complete")
        code = (
            "import json,sys\n"
            "from supermemory import Supermemory\n"
            "p=json.load(sys.stdin)\n"
            "client=Supermemory(api_key=p['key'],base_url='http://127.0.0.1:6767')\n"
            "response=client.search.memories(q=p['query'],container_tag=p['container'],"
            "limit=5,rerank=False,rewrite_query=False,search_mode='hybrid')\n"
            "value=response.model_dump() if hasattr(response,'model_dump') else response\n"
            "print(json.dumps({'results':len(value.get('results',[]) or [])}))\n"
        )
    # Both SDKs are installed with the official plugins, in Hermes' own venv.
    result = subprocess.run(
        [str(REMOTE_HERMES_ROOT / ".venv/bin/python"), "-c", code],
        input=json.dumps({"key": key, "query": query, "container": plan.identity.name}),
        capture_output=True, text=True, check=False, timeout=180,
    )
    if result.returncode:
        raise IngestionPreparationError(f"{plan.provider} startup retrieval failed in the Hermes SDK environment")
    result_count = json.loads(result.stdout)["results"]
    if not isinstance(result_count, int) or result_count <= 0:
        raise IngestionPreparationError("Restored startup database returned no retrieval results")
    return {"status": "passed", "identity": plan.identity.name,
            "corpus_sha256": plan.corpus_sha256, "source_ids": expected,
            "retrieval_result_count": result_count, "checkpoint_restored": True,
            "retrieval_query": query}


def _ingest_through_hermes(
    mode: Mode,
    provider: Provider,
    confirm_paid_calls: bool,
    *,
    commit: Callable[[], None] | None = None,
    recover_hindsight_write: str = "",
    recover_supermemory_write: str = "",
    recovery_trace_sha256: str = "",
    supermemory_receipt_gap_json: str = "",
    run_budget_seconds: float = HERMES_RUN_BUDGET_SECONDS,
    supermemory_known_failure: bool = False,
) -> dict[str, Any]:
    if not confirm_paid_calls:
        raise PaidCallApprovalRequired(f"{provider} ingestion requires --confirm-paid-calls")
    from reference.execution.agent_container import image_backend
    if image_backend(os.environ.get("DOLPHINBENCH_AGENT_IMAGE")) != "modal-sandbox":
        raise IngestionPreparationError("Managed ingestion requires a Modal agent image ID")
    if run_budget_seconds <= 0:
        raise ValueError("run_budget_seconds must be positive")
    if supermemory_known_failure:
        if PERSONA != "morgan" or AGENT != "luna" or provider != "supermemory" or mode != FINAL_MODE:
            raise IngestionPreparationError("The approved document exception is only for final Morgan Supermemory")
        os.environ["SUPERMEMORY_INGEST_CONCURRENCY"] = "1"
    processing_barrier = supermemory_known_failure or ((PERSONA in {"alex", "riley"} or AGENT != "luna") and provider == "supermemory")
    if processing_barrier:
        os.environ["SUPERMEMORY_INGEST_CONCURRENCY"] = "1"
    commit = commit or (lambda: None)
    started = time.monotonic()
    deadline = started + run_budget_seconds
    corpus_path = _runtime_corpus(mode)
    plan = build_plan(provider=provider, mode=mode, corpus_path=corpus_path)
    runtime_root = (
        services.HINDSIGHT_RUNTIME_ROOT if provider == "hindsight"
        else services.SUPERMEMORY_RUNTIME_ROOT
    )
    summary_path = runtime_root / "ingestion-receipt.json"
    restored = False
    if mode == FINAL_MODE:
        assert_final_snapshot_is_empty(_provider_snapshot(provider))
        restored = restore_in_progress_checkpoint(
            provider=provider, runtime_root=runtime_root, receipt_path=summary_path, plan=plan,
        )
    if not restored:
        shutil.rmtree(runtime_root, ignore_errors=True)
        runtime_root.mkdir(parents=True)
    capture = None
    capture_environment: dict[str, str] = {}
    if PERSONA == "riley":
        evidence = _provider_snapshot(provider).volume_root / "diagnostics" / f"usage-{time.time_ns()}"
        if provider == "hindsight":
            from reference.memory.hindsight import InfrastructureCapture

            capture = InfrastructureCapture(evidence, interval_seconds=30)
            capture.__enter__()
            capture_environment = capture.environment()
        else:
            evidence.mkdir(parents=True)
            os.environ["DOLPHINBENCH_OPENAI_USAGE_LOG"] = str(evidence / "backend-usage.jsonl")
    try:
        process = (
            services._start_hindsight(
                require_snapshot=False,
                environment_overrides=(
                    {**(HINDSIGHT_RECOVERY_WORKER_ENVIRONMENT if restored else {}), **capture_environment}
                ),
            ) if provider == "hindsight"
            else services._start_supermemory(require_snapshot=False)
        )
    except Exception as exc:
        if capture is not None:
            capture.__exit__(None, None, None)
        if provider == "supermemory":
            try:
                failure = _record_supermemory_failure(
                    process=None,
                    error=exc,
                    receipt_path=summary_path,
                    commit=commit,
                    runtime_root=runtime_root,
                )
                print(json.dumps({"supermemory_failure": failure}, sort_keys=True), flush=True)
            except Exception as evidence_error:
                print(
                    f"Could not preserve Supermemory startup failure evidence: {evidence_error}",
                    file=sys.stderr,
                    flush=True,
                )
        raise
    stopped = False
    receipt: Mapping[str, Any] | None = None
    groups_completed = 0
    continuation_required = False
    try:
        if provider == "hindsight" and restored:
            api = ingest_hindsight.HindsightApi(
                "http://127.0.0.1:8888",
                os.environ[services.HINDSIGHT_API_KEY_ENV],
            )
            operation_recovery = _wait_for_clean_hindsight_operations(
                api, plan.identity.name
            )
            print(json.dumps({
                "hindsight_operation_recovery": operation_recovery,
            }, sort_keys=True), flush=True)
        _profile, env_updates = _prepare_hermes_seed_profile(provider, runtime_root, plan)
        result_path = runtime_root / "hermes-seed-results.json"
        child_env = os.environ.copy()
        child_env.update(env_updates)
        child_env["PYTHONPATH"] = f"{REMOTE_ROOT}:{REMOTE_HERMES_ROOT}"
        child_env["HERMES_MEMORY_SYNC_DRAIN_TIMEOUT_SECONDS"] = str(
            HERMES_MEMORY_SYNC_DRAIN_TIMEOUT_SECONDS
        )
        # Hermes' cleanup watchdog must outlive the allowed memory drain.
        child_env["HERMES_EXIT_WATCHDOG_S"] = str(HERMES_EXIT_WATCHDOG_SECONDS)
        if provider == "hindsight":
            child_env.update({"HINDSIGHT_MODE": "local_external", "HINDSIGHT_API_URL": "http://127.0.0.1:8888"})
        else:
            child_env["SUPERMEMORY_BASE_URL"] = "http://127.0.0.1:6767"
            child_env["SUPERMEMORY_API_KEY"] = services._supermemory_server_key()

        if recover_hindsight_write:
            if provider != "hindsight" or not restored:
                raise IngestionPreparationError("Write recovery requires a restored Hindsight ingestion")
            _recover_plugin_write(
                runtime_root, corpus_path, recover_hindsight_write, recovery_trace_sha256, child_env,
            )
            receipt = _write_plugin_ingestion_receipt(
                provider=provider, plan=plan,
                plugin_receipt_path=result_path.with_suffix(".hindsight_seed_receipts.jsonl"),
                destination=summary_path,
            )
            save_hindsight_live_checkpoint(runtime_root=runtime_root, receipt_path=summary_path, plan=plan)
            commit()

        if supermemory_receipt_gap_json:
            if provider != "supermemory" or not restored:
                raise IngestionPreparationError("Receipt-gap repair requires restored Supermemory")
            _repair_supermemory_receipt_gap(runtime_root, corpus_path, supermemory_receipt_gap_json, child_env)

        if recover_supermemory_write:
            if provider != "supermemory" or not restored:
                raise IngestionPreparationError("Write recovery requires a restored Supermemory ingestion")
            already_receipted = recover_supermemory_write in _read_plugin_receipts(
                result_path.with_suffix(".supermemory_seed_receipts.jsonl"), "supermemory",
            )
            if not already_receipted:
                _verify_supermemory_recovery_store(runtime_root, recover_supermemory_write, child_env)
                _recover_plugin_write(
                    runtime_root, corpus_path, recover_supermemory_write, recovery_trace_sha256,
                    child_env, provider="supermemory",
                )
            receipt = _write_plugin_ingestion_receipt(
                provider=provider, plan=plan,
                plugin_receipt_path=result_path.with_suffix(".supermemory_seed_receipts.jsonl"),
                destination=summary_path,
            )

        if provider == "supermemory" and (supermemory_receipt_gap_json or recover_supermemory_write):
            if recover_supermemory_write and _read_supermemory_document(recover_supermemory_write, child_env["SUPERMEMORY_API_KEY"]) is None:
                raise IngestionPreparationError("Recovered interrupted document is not readable")
            process, _checkpoint = _checkpoint_and_restart_supermemory(
                process=process, runtime_root=runtime_root, receipt_path=summary_path,
                plan=plan, commit=commit,
            )
            repaired_sources = [recover_supermemory_write] if recover_supermemory_write else []
            if supermemory_receipt_gap_json:
                repaired_sources.append(json.loads(supermemory_receipt_gap_json)["source_id"])
            for repaired_source in repaired_sources:
                if _read_supermemory_document(repaired_source, child_env["SUPERMEMORY_API_KEY"]) is None:
                    raise IngestionPreparationError("Repaired Supermemory document did not survive checkpoint restart")
            print(json.dumps({"supermemory_repaired_sources_survived_restart": repaired_sources}), flush=True)

        def run_one_batch(limit: int = HERMES_MESSAGES_PER_GROUP) -> Mapping[str, Any]:
            if provider == "supermemory":
                _require_supermemory_running(process)
                child_env["DOLPHINBENCH_SUPERMEMORY_SERVER_PID"] = str(process.pid)
            command = [
                str(REMOTE_HERMES_ROOT / ".venv/bin/python"), "-m", "harness.run_simulation",
                "--persona", PERSONA, "--sim", str(corpus_path), "--provider", provider,
                "--out", str(result_path), "--seed-only", "--no-capture-memory",
                "--max-new-seed-items", str(limit),
            ]
            if processing_barrier:
                processing_container = plan.identity.name
                if supermemory_known_failure:
                    from reference.memory.supermemory import TAG

                    processing_container = TAG
                    command.append("--supermemory-known-failure")
                command.extend(["--supermemory-processing-container", processing_container])
            if result_path.exists():
                command.append("--resume")
            completed = subprocess.run(
                command, env=child_env, text=True, capture_output=True, check=False,
            )
            if completed.returncode != 0:
                # A provider failure can follow many successful plugin writes.
                # Rebuild the summary from the per-turn ledger before saving.
                plugin_receipt_path = result_path.with_suffix(
                    f".{provider}_seed_receipts.jsonl"
                )
                if plugin_receipt_path.is_file():
                    failed_receipt = _write_plugin_ingestion_receipt(
                        provider=provider, plan=plan,
                        plugin_receipt_path=plugin_receipt_path,
                        destination=summary_path,
                    )
                    if processing_barrier:
                        failed_receipt["provider_processing"] = {"status": "pending_validation"}
                        _atomic_write_json(summary_path, failed_receipt)
                service_detail = ""
                if provider == "supermemory" and process.poll() is not None:
                    service_detail = (
                        f" Supermemory exited with code {process.returncode};"
                        f" see {services.SUPERMEMORY_SERVER_LOG_PATH}."
                    )
                raise IngestionPreparationError(
                    f"Hermes {provider} seed group failed: "
                    f"{(completed.stderr or completed.stdout)[-4000:]}{service_detail}"
                )
            if provider == "supermemory":
                _require_supermemory_running(process)
            plugin_receipt_path = result_path.with_suffix(
                f".{provider}_seed_receipts.jsonl"
            )
            return _write_plugin_ingestion_receipt(
                provider=provider,
                plan=plan,
                plugin_receipt_path=plugin_receipt_path,
                destination=summary_path,
            )

        def run_group(limit: int = HERMES_MESSAGES_PER_GROUP) -> Mapping[str, Any]:
            if not processing_barrier:
                return run_one_batch(limit)
            from reference.memory.supermemory import wait_for_processing

            def drain():
                return wait_for_processing(
                    child_env["SUPERMEMORY_API_KEY"],
                    lambda: _require_supermemory_running(process),
                    **({"container_tag": plan.identity.name, "allow_known_failure": False}
                       if not supermemory_known_failure else {}),
                )

            group_receipt = run_one_batch(limit)
            # Keep the checkpoint disclosure separate from per-turn runner progress.
            group_receipt["provider_processing"] = {"status": "pending_validation"}
            if supermemory_known_failure:
                group_receipt["provider_processing"]["approved_exception"] = "session-extension_morgan_2024_05_06_004:0"
            _atomic_write_json(summary_path, group_receipt)
            group_receipt["provider_processing"] = drain()
            _atomic_write_json(summary_path, group_receipt)
            print(json.dumps({"supermemory_acknowledged_sources": len(group_receipt["completed_source_ids"]),
                              "approved_document_failures": int(supermemory_known_failure)}), flush=True)
            return group_receipt

        def save_progress() -> None:
            nonlocal process, stopped
            if mode != FINAL_MODE:
                return
            if provider == "hindsight":
                save_hindsight_live_checkpoint(
                    runtime_root=runtime_root,
                    receipt_path=summary_path,
                    plan=plan,
                )
                commit()
            else:
                stopped = True
                process, checkpoint = _checkpoint_and_restart_supermemory(
                    process=process,
                    runtime_root=runtime_root,
                    receipt_path=summary_path,
                    plan=plan,
                    commit=commit,
                )
                stopped = False
                child_env["SUPERMEMORY_API_KEY"] = services._supermemory_server_key()
                saved_receipt = _read_json(
                    summary_path, "Supermemory checkpoint receipt"
                )
                print(json.dumps({
                    "supermemory_checkpoint": checkpoint,
                    "completed_sources": _completed_source_count(
                        provider, saved_receipt
                    ),
                }, sort_keys=True), flush=True)

        if mode == FINAL_MODE and (PERSONA == "riley" or AGENT != "luna"):
            remaining = _startup_gate_remaining(runtime_root, plan)
            if remaining is not None:
                if remaining:
                    receipt = run_group(remaining)
                if provider == "hindsight":
                    api = ingest_hindsight.HindsightApi(
                        "http://127.0.0.1:8888", os.environ[services.HINDSIGHT_API_KEY_ENV],
                    )
                    _wait_for_clean_hindsight_operations(api, plan.identity.name)
                    documents = api.list_document_ids(plan.identity.name)
                    if not set(_expected_plugin_source_ids(plan.corpus_path)[:2]).issubset(documents):
                        raise IngestionPreparationError("Hindsight startup inventory is missing an acknowledged source")
                    _atomic_write_json(runtime_root / "startup-documents.json", {
                        "identity": plan.identity.name, "corpus_sha256": plan.corpus_sha256,
                        "document_ids": sorted(documents),
                    })
                    save_hindsight_live_checkpoint(runtime_root=runtime_root, receipt_path=summary_path, plan=plan)
                    commit()
                    _stop_hindsight_service(process)
                else:
                    _stop_supermemory_service(process)
                    save_in_progress_checkpoint(provider=provider, runtime_root=runtime_root, receipt_path=summary_path, plan=plan)
                    commit()
                stopped = True
                if not restore_in_progress_checkpoint(provider=provider, runtime_root=runtime_root, receipt_path=summary_path, plan=plan):
                    raise IngestionPreparationError("Startup checkpoint restore produced no state")
                process = (services._start_hindsight(require_snapshot=False, environment_overrides={**HINDSIGHT_RECOVERY_WORKER_ENVIRONMENT, **capture_environment})
                           if provider == "hindsight" else services._start_supermemory(require_snapshot=False))
                stopped = False
                if provider == "supermemory":
                    child_env["SUPERMEMORY_API_KEY"] = services._supermemory_server_key()
                proof = _verify_startup_retrieval(runtime_root, plan)
                _atomic_write_json(runtime_root / "startup-gate.json", proof)
                save_progress()
                print(json.dumps({"startup_gate": proof}), flush=True)

        if mode == FINAL_MODE:
            receipt, groups_completed, continuation_required = _run_continuous_groups(
                provider=provider,
                source_count=plan.source_count,
                run_group=run_group,
                save_progress=save_progress,
                should_pause=lambda: time.monotonic() >= deadline,
            )
        else:
            receipt = run_group()
            groups_completed = 1
        if provider == "hindsight" and not continuation_required:
            api = ingest_hindsight.HindsightApi(
                "http://127.0.0.1:8888",
                os.environ[services.HINDSIGHT_API_KEY_ENV],
            )
            operation_drain = _wait_for_clean_hindsight_operations(
                api, plan.identity.name
            )
            print(json.dumps({
                "hindsight_operation_drain": operation_drain,
            }, sort_keys=True), flush=True)
    except Exception as exc:
        if provider == "hindsight":
            stopped = True
            if mode == FINAL_MODE:
                _preserve_hindsight_failure(process=process, error=exc, runtime_root=runtime_root, commit=commit)
            try:
                _stop_hindsight_service(process)
            except Exception as shutdown_error:
                print(json.dumps({"hindsight_shutdown_error": str(shutdown_error),
                                  "original_error": str(exc)[-4000:]}), flush=True)
            raise
        save_stopped_state = True
        if provider == "supermemory":
            stopped = True
            save_stopped_state = _preserve_supermemory_failure(
                process=process, error=exc, receipt_path=summary_path,
                runtime_root=runtime_root, commit=commit,
            )
        else:
            _stop_service(process)
        stopped = True
        if mode == FINAL_MODE and summary_path.is_file() and save_stopped_state:
            save_in_progress_checkpoint(
                provider=provider,
                runtime_root=runtime_root,
                receipt_path=summary_path,
                plan=plan,
            )
            commit()
        raise
    finally:
        try:
            if not stopped:
                if provider == "supermemory":
                    try:
                        _stop_supermemory_service(process)
                    except Exception as exc:
                        _preserve_supermemory_failure(
                            process=process, error=exc, receipt_path=summary_path,
                            runtime_root=runtime_root, commit=commit,
                        )
                        raise
                else:
                    _stop_hindsight_service(process)
                stopped = True
        finally:
            if capture is not None:
                capture.__exit__(None, None, None)

    if receipt is None:
        raise IngestionPreparationError(f"{provider} ingestion produced no receipt")
    if mode == FINAL_MODE and continuation_required:
        publication = {
            "snapshot_published": False,
            "checkpoint": _read_json(
                _in_progress_paths(provider).current_path,
                f"{provider} continuation checkpoint",
            ),
        }
    elif mode == FINAL_MODE:
        publication = publish_final_snapshot(
            provider=provider, runtime_root=runtime_root, receipt_path=summary_path, plan=plan,
        )
        commit()
    else:
        publication = {"snapshot_published": False}
    result = _concise_group_result(
        provider=provider,
        mode=mode,
        plan=plan,
        receipt=receipt,
        resumed_from_checkpoint=restored,
        publication=publication,
        elapsed_seconds=time.monotonic() - started,
    )
    result["groups_completed"] = groups_completed
    result["continuation_required"] = continuation_required
    return result


def _run_retryable_ingestion_attempt(
    *,
    provider: Provider,
    mode: Mode,
    confirm_paid_calls: bool,
    commit: Callable[[], None] | None,
    recover_hindsight_write: str = "",
    recover_supermemory_write: str = "",
    recovery_trace_sha256: str = "",
    supermemory_receipt_gap_json: str = "",
    supermemory_known_failure: bool = False,
) -> dict[str, Any]:
    """Expose clean handoffs to Modal retries and return ordinary failures."""

    if not confirm_paid_calls:
        raise PaidCallApprovalRequired(f"{provider} ingestion requires --confirm-paid-calls")
    try:
        result = _ingest_through_hermes(
            mode,
            provider,
            confirm_paid_calls,
            commit=commit,
            recover_hindsight_write=recover_hindsight_write,
            recover_supermemory_write=recover_supermemory_write,
            recovery_trace_sha256=recovery_trace_sha256,
            supermemory_receipt_gap_json=supermemory_receipt_gap_json,
            supermemory_known_failure=supermemory_known_failure,
        )
    except Exception as exc:
        failure = {
            "provider": provider,
            "mode": mode,
            "worker_failed": True,
            "error_type": type(exc).__name__,
            "error": str(exc)[-4000:],
        }
        print(json.dumps({"ingestion_worker_failure": failure}, sort_keys=True), flush=True)
        return failure
    if result.get("continuation_required") is True:
        print(json.dumps({"automatic_continuation": result}, sort_keys=True), flush=True)
        raise AutomaticContinuationRequired(
            f"{provider} saved a clean checkpoint and requires another worker attempt"
        )
    return result


def _supermemory_ingestion_environment(persona: str) -> dict[str, str]:
    environment = {"SUPERMEMORY_EMBEDDING_RAM_LIMIT": "4gb"}
    if persona in {"morgan", "alex", "riley"}:
        # Mitigate the inspected Bun 1.3.4 JSC crash without changing the server binary.
        environment["BUN_JSC_useJIT"] = "false"
    return environment


def _add_runtime_files(image: Any) -> Any:
    """Copy the frozen corpus, harness, and pinned Hermes source into the worker."""

    image = image.add_local_file(str(DEFAULT_CORPUS), str(REMOTE_CORPUS), copy=True)
    image = image.add_local_dir(
        str(ROOT / "reference"), str(REMOTE_ROOT / "reference"), copy=True,
        ignore=["tests/**", "**/__pycache__/**", "**/*.pyc"],
    )
    for name in ("__init__.py", "checkpoints.py", "construction_io.py"):
        image = image.add_local_file(
            str(ROOT / "construction" / name), str(REMOTE_ROOT / "construction" / name), copy=True,
        )
    for directory in ("harness", "graders", "registry", "mock_mcp", "pricing"):
        image = image.add_local_dir(
            str(ROOT / directory), str(REMOTE_ROOT / directory), copy=True,
        )
    image = image.add_local_dir(
        str(LOCAL_HERMES_ROOT), str(REMOTE_HERMES_ROOT), copy=True,
    )
    return image.env({"PYTHONPATH": f"{REMOTE_ROOT}:{REMOTE_HERMES_ROOT}", PERSONA_ENV: PERSONA, AGENT_ENV: AGENT,
                      "DOLPHINBENCH_AGENT_IMAGE": os.environ.get("DOLPHINBENCH_AGENT_IMAGE", "")})


if modal is not None:
    app = modal.App("dolphinbench-memory-ingestion" + (f"-{PERSONA}-{AGENT}" if PERSONA != "morgan" or AGENT != "luna" else ""))
    hindsight_snapshot_volume = (
        services.hindsight_snapshot_volume if PERSONA == "morgan" and AGENT == "luna" else
        modal.Volume.from_name(_provider_snapshot("hindsight").volume_name, create_if_missing=True)
    )
    supermemory_snapshot_volume = (
        services.supermemory_snapshot_volume if PERSONA == "morgan" and AGENT == "luna" else
        modal.Volume.from_name(_provider_snapshot("supermemory").volume_name, create_if_missing=True)
    )
    hindsight_ingestion_image = _add_runtime_files(services.hindsight_image).run_commands(
        "python -m venv /opt/hermes/.venv",
        "/opt/hermes/.venv/bin/pip install --no-cache-dir -e '/opt/hermes[mcp,hindsight]'",
        f"python -m venv '{REMOTE_MOCK_MCP_ROOT}'",
        f"'{REMOTE_MOCK_MCP_ROOT}/bin/pip' install mcp==1.29.0 pyyaml==6.0.3",
    )
    supermemory_ingestion_image = (
        _add_runtime_files(services.supermemory_image)
        .pip_install("pyyaml==6.0.3")
        .run_commands(
            "python -m venv /opt/hermes/.venv",
            "/opt/hermes/.venv/bin/pip install --no-cache-dir -e '/opt/hermes[mcp,supermemory]'",
            f"python -m venv '{REMOTE_MOCK_MCP_ROOT}'",
            f"'{REMOTE_MOCK_MCP_ROOT}/bin/pip' install mcp==1.29.0 pyyaml==6.0.3",
        )
    )

    # Both provider images import the shared compatibility and recovery code.
    def _add_service_import_dependency(image: Any) -> Any:
        return image.add_local_file(
            str(ROOT / "reference/memory/hindsight.py"),
            str(REMOTE_ROOT / "reference/memory/hindsight.py"), copy=True,
        ).run_commands("python -c 'import reference.memory.hindsight'")

    hindsight_ingestion_image = _add_service_import_dependency(hindsight_ingestion_image)
    supermemory_ingestion_image = _add_service_import_dependency(supermemory_ingestion_image)

    @app.function(
        image=hindsight_ingestion_image,
        cpu=2,
        memory=4096,
        max_containers=1,
        nonpreemptible=PERSONA in {"alex", "riley"},
        timeout=86_400,
        startup_timeout=300,
        retries=modal.Retries(
            max_retries=HERMES_AUTOMATIC_CONTINUATION_RETRIES,
            backoff_coefficient=1.0,
            initial_delay=10.0,
        ),
        secrets=[services.hindsight_secret],
        volumes={str(_provider_snapshot("hindsight").volume_root): hindsight_snapshot_volume},
    )
    def ingest_hindsight_snapshot(
        mode: Mode = FINAL_MODE, confirm_paid_calls: bool = False,
        recover_hindsight_write: str = "", recovery_trace_sha256: str = "",
    ) -> dict[str, Any]:
        """Run one approved Hindsight ingestion through final publication."""

        return _run_retryable_ingestion_attempt(
            provider="hindsight",
            mode=mode,
            confirm_paid_calls=confirm_paid_calls,
            commit=(hindsight_snapshot_volume.commit if mode == FINAL_MODE else None),
            recover_hindsight_write=recover_hindsight_write,
            recovery_trace_sha256=recovery_trace_sha256,
        )

    @app.function(
        image=supermemory_ingestion_image.env(_supermemory_ingestion_environment(PERSONA)),
        cpu=2,
        memory=8192,
        max_containers=1,
        nonpreemptible=PERSONA in {"alex", "riley"},
        timeout=86_400,
        startup_timeout=300,
        retries=modal.Retries(
            max_retries=HERMES_AUTOMATIC_CONTINUATION_RETRIES,
            backoff_coefficient=1.0,
            initial_delay=10.0,
        ),
        secrets=[services.supermemory_secret],
        volumes={str(_provider_snapshot("supermemory").volume_root): supermemory_snapshot_volume},
    )
    def ingest_supermemory_snapshot(
        mode: Mode = FINAL_MODE, confirm_paid_calls: bool = False,
        recover_supermemory_write: str = "", recovery_trace_sha256: str = "",
        supermemory_receipt_gap_json: str = "",
        supermemory_known_failure: bool = False,
    ) -> dict[str, Any]:
        """Run one approved Supermemory ingestion through final publication."""

        return _run_retryable_ingestion_attempt(
            provider="supermemory",
            mode=mode,
            confirm_paid_calls=confirm_paid_calls,
            commit=(supermemory_snapshot_volume.commit if mode == FINAL_MODE else None),
            recover_supermemory_write=recover_supermemory_write,
            recovery_trace_sha256=recovery_trace_sha256,
            supermemory_receipt_gap_json=supermemory_receipt_gap_json,
            supermemory_known_failure=supermemory_known_failure,
        )

    @app.function(
        image=hindsight_ingestion_image,
        cpu=2,
        memory=4096,
        timeout=7200,
        startup_timeout=300,
        single_use_containers=True,
        secrets=[services.hindsight_secret],
        volumes={
            str(_provider_snapshot("hindsight").volume_root): hindsight_snapshot_volume,
        },
    )
    def inspect_saved_hindsight(confirm_paid_calls: bool = False) -> dict[str, Any]:
        """Restore and clean the saved Hindsight state without ingesting a message."""

        if not confirm_paid_calls:
            raise PaidCallApprovalRequired(
                "Starting saved Hindsight can resume paid background processing"
            )
        root = services.HINDSIGHT_RUNTIME_ROOT
        plan = build_plan(
            provider="hindsight",
            mode=FINAL_MODE,
            corpus_path=_runtime_corpus(FINAL_MODE),
        )
        summary = root / "ingestion-receipt.json"
        assert_final_snapshot_is_empty(_provider_snapshot("hindsight"))
        if not restore_in_progress_checkpoint(
            provider="hindsight", runtime_root=root, receipt_path=summary, plan=plan,
        ):
            raise IngestionPreparationError("No saved Hindsight ingestion to inspect")

        # Restore already performs this repair. Repeating the check makes the
        # preflight result explicitly prove that both counters are now safe.
        sequence_verification = _align_hindsight_history_sequences()
        receipt = _read_json(summary, "Hindsight checkpoint receipt")
        completed_sources = _completed_source_count("hindsight", receipt)
        process = services._start_hindsight(
            require_snapshot=False,
            environment_overrides=HINDSIGHT_RECOVERY_WORKER_ENVIRONMENT,
        )
        try:
            api = ingest_hindsight.HindsightApi(
                "http://127.0.0.1:8888",
                os.environ[services.HINDSIGHT_API_KEY_ENV],
            )
            operation_recovery = _wait_for_clean_hindsight_operations(
                api, plan.identity.name,
            )
            checkpoint = save_hindsight_live_checkpoint(
                runtime_root=root,
                receipt_path=summary,
                plan=plan,
            )
            hindsight_snapshot_volume.commit()
            return {
                "provider": "hindsight",
                "completed_sources": completed_sources,
                "sequence_verification": sequence_verification,
                "operation_recovery": operation_recovery,
                "checkpoint": checkpoint,
                "new_messages_sent": 0,
            }
        finally:
            _stop_service(process)

    @app.local_entrypoint()
    def ingest(
        provider: Provider, mode: Mode = FINAL_MODE, confirm_paid_calls: bool = False,
        recover_hindsight_write: str = "", recovery_trace_sha256: str = "",
        background: bool = False,
        recover_supermemory_write: str = "",
        supermemory_receipt_gap_json: str = "",
        supermemory_known_failure: bool = False,
    ) -> None:
        """Launch an explicitly approved paid ingestion."""

        if not confirm_paid_calls:
            raise PaidCallApprovalRequired("remote ingestion requires --confirm-paid-calls")
        if provider == "hindsight":
            call = ingest_hindsight_snapshot.spawn(
                mode, confirm_paid_calls, recover_hindsight_write, recovery_trace_sha256,
            )
            if background:
                print(json.dumps({"provider": provider, "call_id": call.object_id}), flush=True)
                return
            result = call.get()
        elif provider == "supermemory":
            call = ingest_supermemory_snapshot.spawn(
                mode, confirm_paid_calls, recover_supermemory_write, recovery_trace_sha256,
                supermemory_receipt_gap_json,
                supermemory_known_failure,
            )
            if background:
                print(json.dumps({"provider": provider, "call_id": call.object_id}), flush=True)
                return
            result = call.get()
        else:
            raise IngestionPreparationError("provider must be 'hindsight' or 'supermemory'")
        result = _require_remote_worker_result(provider, result)
        print(json.dumps(result, indent=2, sort_keys=True))

    @app.local_entrypoint()
    def ingest_both(mode: Mode = FINAL_MODE, confirm_paid_calls: bool = False,
                    background: bool = False, output: str = "", resume: bool = False) -> None:
        """Start Hindsight and Supermemory ingestion at the same time."""

        if not confirm_paid_calls:
            raise PaidCallApprovalRequired("remote ingestion requires --confirm-paid-calls")
        if background and not output:
            raise IngestionPreparationError("Background ingestion requires a durable launch output")
        record_path = Path(output) if output else None
        if record_path and record_path.exists() and not resume:
            raise IngestionPreparationError("Launch record already exists; inspect it before resuming")
        payload = {"persona": PERSONA, "mode": mode, "call_ids": {}, "launch_approved": True}
        if resume:
            if not record_path or not record_path.exists():
                raise IngestionPreparationError("Resume requires the original launch record")
            payload = _read_json(record_path, "ingestion launch")
            if payload.get("persona") != PERSONA or payload.get("mode") != mode:
                raise IngestionPreparationError("Resume launch identity changed")
            payload.setdefault("worker_sources", {
                provider: payload.get("worker_source_sha256") for provider in payload["call_ids"]
            })
        if record_path:
            _atomic_write_json(record_path, payload)
        calls = {}
        for provider, worker in (("hindsight", ingest_hindsight_snapshot), ("supermemory", ingest_supermemory_snapshot)):
            previous_id = payload["call_ids"].get(provider)
            if previous_id:
                previous = modal.FunctionCall.from_id(previous_id)
                try:
                    previous_result = previous.get(timeout=0)
                except TimeoutError:
                    calls[provider] = previous
                    continue
                if not isinstance(previous_result, dict) or not previous_result.get("worker_failed"):
                    calls[provider] = previous
                    continue
                payload.setdefault("previous_attempts", {}).setdefault(provider, []).append({
                    "call_id": previous_id, "result": previous_result,
                })
            calls[provider] = worker.spawn(mode, confirm_paid_calls)
            payload["call_ids"][provider] = calls[provider].object_id
            payload["worker_source_sha256"] = services._sha256_file(Path(__file__))
            payload.setdefault("worker_sources", {})[provider] = payload["worker_source_sha256"]
            if record_path:
                _atomic_write_json(record_path, payload)
            print(json.dumps({"provider": provider, "call_id": calls[provider].object_id}), flush=True)
        if background:
            return
        results, errors = _collect_provider_results(calls)
        payload = {
            "mode": mode,
            "call_ids": {provider: call.object_id for provider, call in calls.items()},
            "results": results,
            "errors": errors,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        if errors:
            raise IngestionPreparationError(
                "one or more concurrent ingestions failed; inspect the printed provider results"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="create a local plan without contacting Modal or a provider")
    plan.add_argument("--provider", choices=("hindsight", "supermemory"), required=True)
    plan.add_argument("--persona", choices=("morgan", "alex", "riley"), default=PERSONA)
    plan.add_argument("--mode", choices=(SMOKE_MODE, FINAL_MODE), default=FINAL_MODE)
    plan.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    plan.add_argument("--source-checkpoint", default=FINAL_SOURCE_CHECKPOINT)
    plan.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.persona != PERSONA:
        # Volume declarations are fixed at import. Select the persona in a new
        # process instead of mutating another persona's worker configuration.
        return subprocess.run(
            [sys.executable, "-m", "reference.execution.hermes", *(list(argv) if argv is not None else sys.argv[1:])],
            env={**os.environ, PERSONA_ENV: args.persona}, check=False,
        ).returncode
    if args.command == "plan":
        plan = build_plan(
            provider=args.provider,
            mode=args.mode,
            corpus_path=args.corpus,
            source_checkpoint=args.source_checkpoint,
        )
        payload = plan.as_dict()
        if PERSONA in {"alex", "riley"}:
            from construction.checkpoints import validated_checkpoint_identity

            payload["checkpoint_identity"] = validated_checkpoint_identity(DEFAULT_CORPUS.parent)
            payload["worker_memory_mib"] = 8192 if args.provider == "supermemory" else 4096
            payload["nonpreemptible"] = True
            payload["checkpoint_every_messages"] = HERMES_MESSAGES_PER_GROUP
            payload["processing_barrier_between_turns"] = args.provider == "supermemory"
            payload["approved_document_failures"] = []
            payload["model_and_provider_calls_made"] = False
            payload["launch_approved"] = False
            if PERSONA == "riley":
                payload["startup_gate_messages"] = 2
                payload["startup_gate_requires"] = ["processing", "checkpoint_restore", "retrieval"]
        if args.output:
            if args.output.exists():
                raise IngestionPreparationError("plan output already exists; refusing to replace it")
            _atomic_write_json(args.output, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    raise AssertionError(f"unsupported command {args.command!r}")

if __name__ == "__main__":
    raise SystemExit(main())
