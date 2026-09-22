"""Hindsight client, processing checks, and database recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen
from typing import Any, Callable, Mapping
import asyncio
import gzip
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


import yaml


HINDSIGHT_VERSION = "0.9.2"
STATE_VERSION = 1
DEFAULT_BANK_ID = "dolphinbench-morgan-500k-final-v1"
DEFAULT_BATCH_SIZE = 20
DEFAULT_POLL_INTERVAL_SECONDS = 10.0
DEFAULT_POLL_TIMEOUT_SECONDS = 24 * 60 * 60
DEFAULT_HTTP_ATTEMPTS = 5
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


class IngestionError(RuntimeError):
    """Raised when ingestion cannot proceed without risking data loss."""


class UnknownOperationError(IngestionError):
    """Raised when the API cannot tell us whether a submitted operation exists."""


class HindsightHttpError(IngestionError):
    """An HTTP failure with the response status and body preserved."""

    def __init__(self, status: int, method: str, path: str, body: str) -> None:
        super().__init__(f"Hindsight {method} {path} returned HTTP {status}: {body[:500]}")
        self.status = status
        self.method = method
        self.path = path
        self.body = body


@dataclass(frozen=True)
class SourceDocument:
    """One source message retained as one Hindsight document."""

    session_id: str
    message_index: int
    timestamp: str
    content: str
    document_id: str
    payload_sha256: str

    def item(self, persona: str, source_checkpoint: str) -> dict[str, Any]:
        return {
            "content": self.content,
            "timestamp": self.timestamp,
            "context": f"DolphinBench conversation message for {persona}",
            "document_id": self.document_id,
            "metadata": {
                "benchmark": "DolphinBench",
                "persona": persona,
                "source_checkpoint": source_checkpoint,
                "source_session_id": self.session_id,
                "source_message_index": str(self.message_index),
            },
        }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    )


def _safe_component(value: str) -> str:
    cleaned = _SAFE_COMPONENT.sub("_", value).strip("._-")
    if not cleaned:
        raise IngestionError(f"cannot create a stable identifier from {value!r}")
    return cleaned


def stable_document_id(persona: str, session_id: str, message_index: int) -> str:
    """Return the stable ID for one source session/message pair."""
    if message_index < 0:
        raise ValueError("message_index must be non-negative")
    return (
        f"dolphinbench-{_safe_component(persona)}-session-"
        f"{_safe_component(session_id)}-message-{message_index:03d}"
    )


def stable_operation_id(
    bank_id: str,
    batch_index: int,
    payload_sha256: str,
    attempt: int = 0,
) -> str:
    """Return a deterministic UUID for one exact batch attempt."""
    if batch_index < 0 or attempt < 0:
        raise ValueError("batch_index and attempt must be non-negative")
    name = f"dolphinbench/hindsight/{bank_id}/batch/{batch_index}/{payload_sha256}/attempt/{attempt}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def _parse_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IngestionError(f"{label} must be a non-empty ISO timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IngestionError(f"{label} is not an ISO timestamp: {value!r}") from exc
    return value


def load_source_documents(
    corpus_path: Path,
    *,
    persona: str,
    source_checkpoint: str,
) -> tuple[list[SourceDocument], str]:
    """Load the accepted life-sim corpus and preserve every source message."""
    raw_bytes = corpus_path.read_bytes()
    corpus_sha256 = _sha256_bytes(raw_bytes)
    try:
        payload = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as exc:
        raise IngestionError(f"corpus is not valid YAML: {corpus_path}") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("sessions"), list):
        raise IngestionError("corpus must be an object with a sessions list")

    documents: list[SourceDocument] = []
    seen_sessions: set[str] = set()
    previous_timestamp: datetime | None = None
    for session_number, session in enumerate(payload["sessions"], start=1):
        if not isinstance(session, Mapping):
            raise IngestionError(f"session {session_number} is not an object")
        session_id = session.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise IngestionError(f"session {session_number} has no string id")
        if session_id in seen_sessions:
            raise IngestionError(f"duplicate source session id: {session_id}")
        seen_sessions.add(session_id)
        timestamp = _parse_timestamp(session.get("narrative_date"), f"session {session_id} narrative_date")
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if previous_timestamp is not None and parsed_timestamp < previous_timestamp:
            raise IngestionError(f"source sessions are not chronological at {session_id}")
        previous_timestamp = parsed_timestamp
        messages = session.get("messages")
        if not isinstance(messages, list) or not messages:
            raise IngestionError(f"session {session_id} must contain at least one message")
        for message_index, message in enumerate(messages):
            if not isinstance(message, str) or not message.strip():
                raise IngestionError(
                    f"session {session_id} message {message_index} must be non-empty text"
                )
            content = f"User message at {timestamp}:\n{message}"
            documents.append(
                SourceDocument(
                    session_id=session_id,
                    message_index=message_index,
                    timestamp=timestamp,
                    content=content,
                    document_id=stable_document_id(persona, session_id, message_index),
                    payload_sha256=_sha256_bytes(content.encode()),
                )
            )
    return documents, corpus_sha256


class HindsightApi:
    """Small authenticated REST client with safe retry behavior."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = 60.0,
        max_attempts: int = DEFAULT_HTTP_ATTEMPTS,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise IngestionError("HINDSIGHT_API_URL must start with http:// or https://")
        if not api_key:
            raise IngestionError("HINDSIGHT_API_KEY is required")
        self.base_url = base_url.rstrip("/") + "/"
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.sleep_fn = sleep_fn

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        url = urljoin(self.base_url, path.lstrip("/"))
        for attempt in range(self.max_attempts):
            request = Request(url, data=body, headers=headers, method=method)
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    raw = response.read().decode()
                parsed = json.loads(raw) if raw else {}
                if not isinstance(parsed, dict):
                    raise IngestionError(f"Hindsight returned a non-object response for {method} {path}")
                return parsed
            except HTTPError as exc:
                response_body = exc.read().decode(errors="replace")
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if not retryable or attempt + 1 >= self.max_attempts:
                    raise HindsightHttpError(exc.code, method, path, response_body) from exc
            except (TimeoutError, URLError, OSError, json.JSONDecodeError) as exc:
                if attempt + 1 >= self.max_attempts:
                    raise IngestionError(f"Hindsight {method} {path} failed after retries: {exc}") from exc
            self.sleep_fn(min(60.0, 2.0**attempt))
        raise AssertionError("unreachable")

    def find_operation(self, bank_id: str, operation_id: str) -> dict[str, Any] | None:
        path = f"/v1/default/banks/{quote(bank_id, safe='')}/operations/{quote(operation_id, safe='')}"
        try:
            result = self._request("GET", path)
        except HindsightHttpError as exc:
            if exc.status == 404:
                return None
            raise
        # Hindsight 0.9.2 returns HTTP 200 with this status when the operation
        # does not exist. Treat it the same as an HTTP 404.
        if result.get("status") == "not_found":
            return None
        return result

    def submit_batch(
        self,
        bank_id: str,
        items: list[dict[str, Any]],
        operation_id: str,
    ) -> dict[str, Any]:
        path = f"/v1/default/banks/{quote(bank_id, safe='')}/memories"
        return self._request(
            "POST",
            path,
            {"items": items, "async": True, "operation_id": operation_id},
        )

    def operation(self, bank_id: str, operation_id: str) -> dict[str, Any]:
        result = self.find_operation(bank_id, operation_id)
        if result is None:
            raise UnknownOperationError(
                f"Hindsight has no record of operation {operation_id}; refusing to resend it automatically"
            )
        return result

    def list_operations(
        self,
        bank_id: str,
        *,
        status: str | None = None,
        operation_type: str | None = None,
        page_size: int = 100,
        consistency_attempts: int = 5,
    ) -> list[dict[str, Any]]:
        """Return every operation matching the supported server filters."""

        if consistency_attempts <= 0:
            raise ValueError("consistency_attempts must be positive")

        for consistency_attempt in range(consistency_attempts):
            operations: list[dict[str, Any]] = []
            offset = 0
            expected_total: int | None = None
            while True:
                query: dict[str, str | int] = {"limit": page_size, "offset": offset}
                if status is not None:
                    query["status"] = status
                if operation_type is not None:
                    query["type"] = operation_type
                path = (
                    f"/v1/default/banks/{quote(bank_id, safe='')}/operations?"
                    f"{urlencode(query)}"
                )
                response = self._request("GET", path)
                page = response.get("operations")
                total = response.get("total")
                if (
                    not isinstance(page, list)
                    or isinstance(total, bool)
                    or not isinstance(total, int)
                    or total < 0
                    or any(not isinstance(operation, dict) for operation in page)
                ):
                    raise IngestionError("Hindsight operation list returned an invalid response")
                if expected_total is None:
                    expected_total = total
                elif total != expected_total:
                    break
                operations.extend(page)
                offset += len(page)
                if offset == expected_total:
                    return operations
                if offset > expected_total or not page:
                    break

            if consistency_attempt + 1 >= consistency_attempts:
                raise IngestionError(
                    "Hindsight operation list did not return a coherent snapshot after "
                    f"{consistency_attempts} attempts"
                )
            # Hindsight counts and fetches in separate queries. A live status
            # transition can make one response internally inconsistent.
            self.sleep_fn(min(1.0, 0.1 * (consistency_attempt + 1)))

        raise AssertionError("unreachable")

    def retry_operation(self, bank_id: str, operation_id: str) -> dict[str, Any]:
        path = (
            f"/v1/default/banks/{quote(bank_id, safe='')}/operations/"
            f"{quote(operation_id, safe='')}/retry"
        )
        return self._request("POST", path)

    def list_document_ids(self, bank_id: str, *, page_size: int = 500) -> set[str]:
        """Return every document ID through Hindsight's supported list API."""
        found: set[str] = set()
        offset = 0
        expected_total: int | None = None
        while expected_total is None or offset < expected_total:
            path = (
                f"/v1/default/banks/{quote(bank_id, safe='')}/documents"
                f"?limit={page_size}&offset={offset}"
            )
            response = self._request("GET", path)
            items = response.get("items")
            total = response.get("total")
            if not isinstance(items, list) or isinstance(total, bool) or not isinstance(total, int):
                raise IngestionError("Hindsight document list returned an invalid response")
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise IngestionError("Hindsight document count changed during verification")
            page_ids: list[str] = []
            for item in items:
                document_id = item.get("id") if isinstance(item, Mapping) else None
                if not isinstance(document_id, str) or not document_id:
                    raise IngestionError("Hindsight document list contains an item without an ID")
                page_ids.append(document_id)
            if len(page_ids) != len(set(page_ids)) or found.intersection(page_ids):
                raise IngestionError("Hindsight document list contains duplicate IDs")
            found.update(page_ids)
            offset += len(page_ids)
            if not page_ids and offset < expected_total:
                raise IngestionError("Hindsight document list ended before its reported total")
        if len(found) != expected_total:
            raise IngestionError(
                f"Hindsight returned {len(found)} unique documents but reported {expected_total}"
            )
        return found


def _operation_document_ids(value: Any) -> set[str]:
    """Collect document IDs from an operation and any child-operation records."""
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "document_ids" and isinstance(child, list):
                found.update(item for item in child if isinstance(item, str))
            else:
                found.update(_operation_document_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_operation_document_ids(child))
    return found


def _operation_extraction_errors(value: Any) -> int:
    """Count extraction errors reported by an operation or its children."""
    if isinstance(value, Mapping):
        total = 0
        for key, child in value.items():
            if key == "extraction_errors_count":
                if isinstance(child, bool) or not isinstance(child, int) or child < 0:
                    raise IngestionError("Hindsight returned an invalid extraction error count")
                total += child
            else:
                total += _operation_extraction_errors(child)
        return total
    if isinstance(value, list):
        return sum(_operation_extraction_errors(child) for child in value)
    return 0


def _operation_status(operation: Mapping[str, Any]) -> str:
    status = operation.get("status")
    if not isinstance(status, str) or not status:
        raise IngestionError(f"Hindsight operation response has no status: {operation!r}")
    return status.lower()


def _new_state(
    *,
    bank_id: str,
    persona: str,
    corpus_path: Path,
    corpus_sha256: str,
    source_checkpoint: str,
    batch_size: int,
    documents: list[SourceDocument],
) -> dict[str, Any]:
    batches: list[dict[str, Any]] = []
    for index in range(0, len(documents), batch_size):
        batch_documents = documents[index : index + batch_size]
        batch_index = len(batches)
        payload = [
            document.item(persona, source_checkpoint)
            for document in batch_documents
        ]
        payload_sha256 = _sha256_json(payload)
        batches.append({
            "batch_index": batch_index,
            "document_ids": [document.document_id for document in batch_documents],
            "payload_sha256": payload_sha256,
            "attempt": 0,
            "operation_id": stable_operation_id(bank_id, batch_index, payload_sha256),
            "status": "planned",
            "operation_history": [],
        })
    return {
        "schema_version": STATE_VERSION,
        "provider": "hindsight",
        "hindsight_version": HINDSIGHT_VERSION,
        "bank_id": bank_id,
        "persona": persona,
        "corpus_path": str(corpus_path),
        "corpus_sha256": corpus_sha256,
        "source_checkpoint": source_checkpoint,
        "batch_size": batch_size,
        "documents_total": len(documents),
        "status": "planned",
        "batches": batches,
    }

def use_named_openai_tool_choice() -> None:
    """Apply the correction while building the Claude Hindsight server image."""
    from pathlib import Path

    path = Path("/app/api/hindsight_api/engine/providers/openai_compatible_llm.py")
    source = path.read_text()
    before = """            tools = filtered
            request_tool_choice = LLMToolChoiceMode.REQUIRED.value"""
    after = """            if self.provider == "openai" and "deepseek" not in self.model.lower():
                request_tool_choice = {
                    "type": "function", "function": {"name": forced_name}
                }
            else:
                tools = filtered
                request_tool_choice = LLMToolChoiceMode.REQUIRED.value"""
    if source.count(before) != 1:
        raise RuntimeError("Hindsight's named-tool code differs from the reviewed 0.9.2 source")
    source = source.replace(before, after).replace(
        "request_tool_choice: str | None", "request_tool_choice: str | dict[str, Any] | None"
    )
    compile(source, str(path), "exec")
    path.write_text(source)


WORKER_ENVIRONMENT = {
    "HINDSIGHT_API_WORKER_MAX_SLOTS": "1",
    "HINDSIGHT_API_WORKER_CONSOLIDATION_RESERVED_SLOTS": "0",
}


def operation_id(operation: Mapping[str, Any]) -> str:
    value = operation.get("operation_id") or operation.get("id")
    if not isinstance(value, str) or not value:
        raise RuntimeError("Hindsight returned an operation without an ID")
    return value


def is_sequence_failure(operation: Mapping[str, Any]) -> bool:
    kind = operation.get("operation_type") or operation.get("task_type")
    error = str(operation.get("error_message") or "")
    return (
        kind == "consolidation"
        and "duplicate key value violates unique constraint" in error
        and any(name in error for name in ("observation_history_pkey", "mental_model_history_pkey"))
    )


def wait_for_clean_operations(
    api: Any, bank_id: str, *, timeout_seconds: float = 6000,
    poll_seconds: float = 5, retry_batch_size: int = 100,
    sleep_fn: Callable[[float], None] = time.sleep,
    allowed_retry_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Prove one retry before a batch; refuse unrelated or repeated failures."""
    deadline = time.monotonic() + timeout_seconds
    retried: set[str] = set()
    active_retry_batch: set[str] = set()
    serial_retry_succeeded = False
    while True:
        pending = api.list_operations(bank_id, status="pending")
        processing = api.list_operations(bank_id, status="processing")
        if pending or processing:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Hindsight background work did not finish before the drain timeout: "
                    f"{len(pending)} pending, {len(processing)} processing"
                )
            sleep_fn(poll_seconds)
            continue
        if active_retry_batch:
            statuses = {key: api.operation(bank_id, key).get("status") for key in sorted(active_retry_batch)}
            failed_again = [key for key, status in statuses.items() if status == "failed"]
            if failed_again:
                raise RuntimeError(
                    "Hindsight background operations failed again after their ID "
                    f"counter was repaired: {failed_again[:10]}"
                )
            unresolved = {key: status for key, status in statuses.items() if status != "completed"}
            if unresolved:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Hindsight retried operations did not complete before the drain "
                        f"timeout: {dict(list(unresolved.items())[:10])}"
                    )
                sleep_fn(poll_seconds)
                continue
            active_retry_batch.clear()
            serial_retry_succeeded = True
        cancelled = api.list_operations(bank_id, status="cancelled")
        if cancelled:
            raise RuntimeError(
                "Hindsight has cancelled background work that cannot certify a complete "
                f"ingestion: {[operation_id(row) for row in cancelled[:10]]}"
            )
        failed = api.list_operations(bank_id, status="failed")
        blocked = [row for row in failed if not is_sequence_failure(row)]
        if blocked:
            raise RuntimeError(
                "Hindsight has failed background work that is not an ID-counter failure: "
                + str([{"operation_id": operation_id(row),
                        "error": str(row.get("error_message") or "")[-1000:]} for row in blocked[:10]])
            )
        ids = {operation_id(row) for row in failed}
        if allowed_retry_ids is not None and ids - allowed_retry_ids:
            raise RuntimeError("Hindsight failed operations differ from the approved retry set")
        if ids & retried:
            raise RuntimeError("Hindsight background operations failed again after their ID counter was repaired")
        if not failed:
            return {"bank_id": bank_id, "retried_sequence_failures": sorted(retried),
                    "pending": 0, "processing": 0, "failed": 0}
        limit = retry_batch_size if serial_retry_succeeded else 1
        batch = sorted(failed, key=operation_id)[:limit]
        print(json.dumps({"hindsight_retry_batch": [operation_id(row) for row in batch],
                          "first_retry_verified": serial_retry_succeeded}), flush=True)
        for row in batch:
            if time.monotonic() >= deadline:
                raise RuntimeError("Hindsight background work did not finish before the drain timeout")
            key = operation_id(row)
            api.retry_operation(bank_id, key)
            retried.add(key)
            active_retry_batch.add(key)
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Hindsight background work did not finish before the drain timeout: "
                f"0 pending, 0 processing, {len(failed)} failed"
            )
        sleep_fn(poll_seconds)


async def align_history_sequences(connection) -> list[dict]:
    """Advance stale counters and prove their next IDs cannot collide with rows."""
    result = []
    async with connection.transaction():
        for table in ("observation_history", "mental_model_history"):
            await connection.execute(f"LOCK TABLE public.{table} IN SHARE ROW EXCLUSIVE MODE")
            sequence = await connection.fetchval("SELECT pg_get_serial_sequence($1,$2)", f"public.{table}", "id")
            if sequence != f"public.{table}_id_seq":
                raise RuntimeError(f"Unexpected Hindsight sequence for {table}")
            increment = await connection.fetchval(
                "SELECT seqincrement FROM pg_sequence WHERE seqrelid=$1::regclass",
                sequence,
            )
            if increment != 1:
                raise RuntimeError(f"Unexpected Hindsight sequence increment for {table}")
            highest = await connection.fetchval(f"SELECT max(id) FROM public.{table}")
            state = await connection.fetchrow(f"SELECT last_value, is_called FROM {sequence}")
            current = state["last_value"]
            advanced = highest is not None and (highest > current or (highest == current and not state["is_called"]))
            if advanced:
                await connection.fetchval("SELECT setval($1::regclass,$2,true)", sequence, highest)
            verified_state = await connection.fetchrow(
                f"SELECT last_value, is_called FROM {sequence}"
            )
            next_id = (
                verified_state["last_value"] + increment
                if verified_state["is_called"]
                else verified_state["last_value"]
            )
            if highest is not None and next_id <= highest:
                raise RuntimeError(
                    f"Hindsight sequence for {table} would generate duplicate ID {next_id}"
                )
            result.append({
                "table": table,
                "previous_sequence": current,
                "highest_row": highest,
                "advanced": advanced,
                "next_id": next_id,
                "verified": True,
            })
    return result


async def main() -> None:
    import asyncpg
    from hindsight_api.pg0 import resolve_database_url

    url = await resolve_database_url("pg0")
    connection = await asyncpg.connect(url, timeout=30)
    try:
        print(json.dumps({"history_sequence_recovery": await align_history_sequences(connection)}), flush=True)
    finally:
        await connection.close()


CGROUP_FILES = (
    "cpu.max", "cpu.stat", "cpu.pressure", "memory.current", "memory.max",
    "memory.events", "memory.pressure", "io.stat", "io.pressure",
    "cpu/cpu.cfs_quota_us", "cpu/cpu.cfs_period_us", "cpu/cpu.stat",
    "cpuacct/cpuacct.usage", "memory/memory.limit_in_bytes",
    "memory/memory.usage_in_bytes", "memory/memory.failcnt",
    "memory/memory.oom_control",
)
THREAD_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")
SPAN_ATTRIBUTES = frozenset({
    "hindsight.bank_id", "hindsight.operation", "hindsight.scope",
    "hindsight.fact_types", "hindsight.thinking_budget", "hindsight.max_tokens",
    "hindsight.candidates_count", "hindsight.scored_count", "hindsight.pre_filtered_count",
    "hindsight.semantic_count", "hindsight.bm25_count", "hindsight.graph_count",
    "hindsight.temporal_count", "hindsight.merged_count",
    "gen_ai.request.model", "gen_ai.response.model", "gen_ai.provider.name",
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.cached_tokens",
    "error.type",
})
DB_QUERIES = {
    "settings": """SELECT name, setting, unit FROM pg_settings WHERE name = ANY($1) ORDER BY name""",
    "activity": """SELECT state, wait_event_type, wait_event, count(*) AS connections
        FROM pg_stat_activity WHERE datname=current_database() AND pid <> pg_backend_pid()
        GROUP BY state, wait_event_type, wait_event""",
    "database": """SELECT numbackends, blks_read, blks_hit, temp_files, temp_bytes,
        deadlocks, blk_read_time, blk_write_time, stats_reset
        FROM pg_stat_database WHERE datname=current_database()""",
    "tables": """SELECT relname, n_live_tup, n_dead_tup, seq_scan, idx_scan,
        last_analyze, last_autoanalyze, last_autovacuum
        FROM pg_stat_user_tables WHERE schemaname='public' ORDER BY relname""",
    "indexes": """SELECT t.relname AS table_name, i.relname AS index_name,
        x.indisvalid, x.indisready, am.amname AS method
        FROM pg_index x JOIN pg_class i ON i.oid=x.indexrelid
        JOIN pg_class t ON t.oid=x.indrelid JOIN pg_namespace n ON n.oid=t.relnamespace
        JOIN pg_am am ON am.oid=i.relam
        WHERE n.nspname='public' ORDER BY t.relname,i.relname""",
}
DB_SETTINGS = [
    "shared_buffers", "work_mem", "effective_cache_size", "maintenance_work_mem",
    "max_connections", "max_parallel_workers_per_gather", "track_io_timing",
    "autovacuum", "data_directory", "fsync",
]


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def resource_snapshot(proc: Path = Path("/proc"), cgroup: Path = Path("/sys/fs/cgroup")) -> dict:
    """Preserve unknown counters as null; never infer container limits from host totals."""
    processes = []
    process_read_errors = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            name = (entry / "comm").read_text().strip()
            if name not in {"hindsight-api", "postgres"}:
                continue
            status = dict(line.split(":", 1) for line in (entry / "status").read_text().splitlines())
            # The comm field can contain spaces and parentheses; numeric fields follow the last ')'.
            stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            process = {
                "pid": int(entry.name), "name": name, "start_ticks": int(stat[19]),
                "cpu_ticks": int(stat[11]) + int(stat[12]),
                "rss_kib": int(status["VmRSS"].split()[0]) if "VmRSS" in status else None,
                "threads": int(status["Threads"]) if "Threads" in status else None,
                "io": _read(entry / "io"),
            }
        except (OSError, ValueError, IndexError) as exc:
            process_read_errors.append({"pid": int(entry.name), "error_type": type(exc).__name__})
            continue
        # /proc/environ can be restricted even when CPU/RSS counters are readable.
        try:
            env = dict(item.split(b"=", 1) for item in (entry / "environ").read_bytes().split(b"\0") if b"=" in item)
            process["thread_environment_at_exec"] = {key: env[key.encode()].decode(errors="replace")
                for key in THREAD_ENV if key.encode() in env}
        except OSError as exc:
            process["thread_environment_at_exec"] = None
            process["environment_read_error"] = type(exc).__name__
        processes.append(process)
    return {
        "unix_seconds": time.time(), "monotonic_seconds": time.monotonic(),
        "clock_ticks_per_second": os.sysconf("SC_CLK_TCK"),
        "visible_cpu_count": os.cpu_count(),
        "affinity_cpu_count": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "cgroup": {name: _read(cgroup / name) for name in CGROUP_FILES},
        "processes": processes,
        "process_read_errors": process_read_errors,
        "limits_note": "Visible cgroup/affinity values may describe host capacity, not Modal reservations.",
    }


async def database_snapshot(connection) -> dict:
    """Catalog/statistics only, bounded and read-only; no corpus text, plans, or mutations."""
    result = {"unix_seconds": time.time(), "diagnostic_limits": {
        "statement_timeout_ms": 3000, "lock_timeout_ms": 500, "read_only": True,
    }}
    async with connection.transaction(readonly=True):
        await connection.execute("SET LOCAL statement_timeout = '3s'")
        await connection.execute("SET LOCAL lock_timeout = '500ms'")
        for name, query in DB_QUERIES.items():
            rows = await connection.fetch(query, DB_SETTINGS) if name == "settings" else await connection.fetch(query)
            result[name] = [dict(row) for row in rows]
    return result


def sanitize_span(span: dict) -> dict:
    """OTLP JSON mapping to timing-only evidence; drop events, links, bodies and resources."""
    attrs = {a["key"]: a["value"] for a in span.get("attributes", []) if a.get("key") in SPAN_ATTRIBUTES}
    start, end = int(span.get("startTimeUnixNano", 0)), int(span.get("endTimeUnixNano", 0))
    return {
        "trace_id": span.get("traceId"), "span_id": span.get("spanId"),
        "parent_span_id": span.get("parentSpanId"), "name": span.get("name"),
        "start_unix_ns": start, "end_unix_ns": end,
        "duration_ms": (end - start) / 1e6 if end >= start > 0 else None,
        "status_code": span.get("status", {}).get("code", 0), "attributes": attrs,
    }


class InfrastructureCapture:
    """Loopback-only OTLP sink and resource sampler for an explicitly approved service.

    Start before Hindsight; pass environment() to that disposable service; stop the
    service cleanly (flushing OTLP) before closing this collector. No API/model calls
    or environment changes are made by this class itself.
    """

    def __init__(self, directory: Path, interval_seconds: float = 1):
        if interval_seconds < 0.25:
            raise ValueError("Sampling interval must be at least 250ms")
        self.directory = directory
        self.interval = interval_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._server = None
        self._threads = []

    def write(self, filename: str, value: dict) -> None:
        with self._lock:
            with (self.directory / filename).open("a") as handle:
                handle.write(json.dumps(value, default=str) + "\n")

    def __enter__(self):
        # Fail before starting threads if the runtime lacks its existing OTLP dependency.
        from google.protobuf.json_format import MessageToDict
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

        self.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                if self.path != "/v1/traces":
                    self.send_error(404)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 32 * 1024 * 1024:
                        raise ValueError("Invalid length")
                    data = self.rfile.read(size)
                    encoding = self.headers.get("Content-Encoding", "identity")
                    if encoding == "gzip":
                        # Stream-limited decompression prevents compressed payloads bypassing the bound.
                        import io
                        with gzip.GzipFile(fileobj=io.BytesIO(data)) as zipped:
                            data = zipped.read(32 * 1024 * 1024 + 1)
                    elif encoding != "identity":
                        raise ValueError("Unsupported encoding")
                    if len(data) > 32 * 1024 * 1024:
                        raise ValueError("Payload too large")
                    request = ExportTraceServiceRequest()
                    request.ParseFromString(data)
                    for resource in MessageToDict(request).get("resourceSpans", []):
                        for scope in resource.get("scopeSpans", []):
                            for span in scope.get("spans", []):
                                owner.write("spans.jsonl", sanitize_span(span))
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-protobuf")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except Exception as exc:
                    owner.write("collector-errors.jsonl", {"error_type": type(exc).__name__})
                    self.send_error(400)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

        def sample():
            while not self._stop.is_set():
                try:
                    self.write("resources.jsonl", resource_snapshot())
                except Exception as exc:
                    self.write("collector-errors.jsonl", {"error_type": type(exc).__name__})
                self._stop.wait(self.interval)

        self._threads = [threading.Thread(target=self._server.serve_forever), threading.Thread(target=sample)]
        for thread in self._threads:
            thread.start()
        return self

    def environment(self) -> dict[str, str]:
        if self._server is None:
            raise RuntimeError("Collector has not started")
        return {
            "HINDSIGHT_API_OTEL_TRACES_ENABLED": "true",
            "HINDSIGHT_API_OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{self._server.server_port}",
            "HINDSIGHT_API_OTEL_EXPORTER_OTLP_HEADERS": "",
            "OTEL_BSP_SCHEDULE_DELAY": "1000",
        }

    def __exit__(self, *_args):
        self._stop.set()
        self._server.shutdown()
        self._server.server_close()
        for thread in self._threads:
            thread.join(timeout=5)

if __name__ == "__main__":
    asyncio.run(main())
