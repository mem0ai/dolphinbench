"""Supermemory client and document-processing checks."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import inspect
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol
import http.client
import urllib.error
import urllib.request


import yaml


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = ROOT / "construction/v2/morgan/release/500k_final/checkpoint/life_sim.yaml"
DEFAULT_RECEIPTS = ROOT / "artifacts/supermemory"
RECEIPT_VERSION = 2
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_SETTLE_TIMEOUT_SECONDS = 3600.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
_SAFE_TAG = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_SAFE_CUSTOM_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_PENDING_STATES = frozenset(
    {"unknown", "pending", "processing", "queued", "extracting", "chunking", "embedding", "indexing"}
)
_FAILED_STATES = frozenset({"failed", "error", "deleted", "cancelled"})
_SUCCESS_STATES = frozenset({"done"})


class IngestionError(RuntimeError):
    """Raised when the local corpus, receipt, or provider state is unsafe."""


class ProviderCapabilityError(IngestionError):
    """Raised when the SDK cannot provide a required scoped operation."""


class PendingDocumentsError(IngestionError):
    """Raised while the provider is still processing expected documents."""


class DocumentStore(Protocol):
    """The small provider surface required by the importer."""

    def list_documents(self, *, container_tag: str) -> list[dict[str, Any]]: ...

    def add_document(
        self,
        *,
        content: str,
        container_tag: str,
        source_id: str,
        document_date: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CorpusItem:
    persona: str
    session_id: str
    message_index: int
    narrative_date: str
    content: str

    @property
    def source_id(self) -> str:
        return stable_source_id(self.persona, self.session_id, self.message_index)


def stable_container_tag(persona: str) -> str:
    """Return the fixed final-corpus container name for a persona."""
    persona = persona.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", persona):
        raise IngestionError(f"invalid persona name: {persona!r}")
    return f"dolphinbench-{persona}-500k-final-v1"


def validate_container_tag(tag: str) -> str:
    tag = str(tag).strip().lower()
    if not _SAFE_TAG.fullmatch(tag):
        raise IngestionError(
            "container tag must use only lowercase letters, numbers, '.', '_', ':', or '-' "
            "and must be at most 128 characters"
        )
    return tag


def stable_source_id(persona: str, session_id: str, message_index: int) -> str:
    """Return the same provider custom ID on every invocation."""
    persona = str(persona).strip().lower()
    session_id = str(session_id).strip()
    try:
        message_index = int(message_index)
    except (TypeError, ValueError) as exc:
        raise IngestionError("message index must be a non-negative integer") from exc
    if not persona or not session_id or message_index < 0:
        raise IngestionError("source IDs require a persona, session ID, and non-negative message index")
    readable_session_id = re.sub(r"[^A-Za-z0-9._-]+", "-", session_id).strip("-") or "session"
    candidate = f"dolphinbench-{persona}-{readable_session_id}-{message_index}"
    if not _SAFE_CUSTOM_ID.fullmatch(candidate):
        digest = hashlib.sha256(session_id.encode()).hexdigest()[:24]
        candidate = f"dolphinbench-{persona}-{digest}-{message_index}"
    if not _SAFE_CUSTOM_ID.fullmatch(candidate):
        raise IngestionError(f"source ID cannot satisfy Supermemory custom_id rules: {candidate!r}")
    return candidate


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _message_text(value: Any, where: str) -> str:
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, Mapping) and isinstance(value.get("content"), str):
        text = str(value["content"]).strip()
    else:
        raise IngestionError(f"{where} must be a string message or an object with string content")
    if not text:
        raise IngestionError(f"{where} is empty")
    return text


def load_corpus(path: Path, *, persona: str = "morgan") -> tuple[list[CorpusItem], str]:
    """Load and validate the immutable source file without modifying it."""
    path = Path(path).resolve()
    if not path.is_file():
        raise IngestionError(f"corpus file does not exist: {path}")
    try:
        payload = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise IngestionError(f"cannot read corpus {path}: {exc}") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("sessions"), list):
        raise IngestionError("corpus must be a mapping with a sessions list")

    items: list[CorpusItem] = []
    seen: set[str] = set()
    previous_date = ""
    for session_number, session in enumerate(payload["sessions"], 1):
        if not isinstance(session, Mapping):
            raise IngestionError(f"sessions[{session_number - 1}] is not an object")
        session_id = str(session.get("id", "")).strip()
        narrative_date = str(session.get("narrative_date", "")).strip()
        messages = session.get("messages")
        if not session_id or not narrative_date or not isinstance(messages, list) or not messages:
            raise IngestionError(
                f"session {session_number} must have id, narrative_date, and a non-empty messages list"
            )
        if previous_date and narrative_date < previous_date:
            raise IngestionError("corpus sessions are not chronological")
        previous_date = narrative_date
        for message_index, raw_message in enumerate(messages):
            content = _message_text(raw_message, f"session {session_id} message {message_index}")
            source_id = stable_source_id(persona, session_id, message_index)
            if source_id in seen:
                raise IngestionError(f"duplicate source ID in corpus: {source_id}")
            seen.add(source_id)
            items.append(CorpusItem(persona, session_id, message_index, narrative_date, content))
    if not items:
        raise IngestionError("corpus contains no messages")
    return items, sha256_file(path)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return vars(value)
    return value


def _field(value: Any, *names: str) -> Any:
    value = _jsonable(value)
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


def normalize_document(value: Any) -> dict[str, Any]:
    """Normalize SDK objects and dictionaries to fields used by verification."""
    value = _jsonable(value)
    metadata = _field(value, "metadata") or {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    content = _field(value, "content")
    content_sha256 = metadata.get("dolphinbench_content_sha256")
    if isinstance(content, str):
        content_sha256 = hashlib.sha256(content.encode()).hexdigest()
    container_tags = _field(value, "container_tags", "containerTags") or []
    if isinstance(container_tags, str):
        container_tags = [container_tags]
    source_id = _field(value, "custom_id", "customId", "source_id", "sourceId")
    if not source_id:
        source_id = metadata.get("dolphinbench_source_id")
    processing = _field(value, "processing_metadata", "processingMetadata") or {}
    if not isinstance(processing, Mapping):
        processing = {}
    return {
        "id": str(_field(value, "id", "document_id", "documentId") or ""),
        "source_id": str(source_id or ""),
        "status": str(_field(value, "status", "state") or "").lower(),
        "metadata": dict(metadata),
        "content_sha256": str(content_sha256 or ""),
        "container_tags": [str(tag) for tag in container_tags] if isinstance(container_tags, list) else [],
        "processing": dict(processing),
    }


def _response_documents(response: Any) -> tuple[list[Any], Any]:
    response = _jsonable(response)
    if isinstance(response, list):
        return response, None
    if isinstance(response, Mapping):
        raw = (
            response.get("memories")
            or response.get("results")
            or response.get("documents")
            or response.get("data")
            or []
        )
        pagination = response.get("pagination")
    else:
        raw = _field(response, "memories", "results", "documents", "data") or []
        pagination = _field(response, "pagination")
    if not isinstance(raw, list):
        raise ProviderCapabilityError("Supermemory document listing returned no document list")
    return raw, pagination


class SupermemorySdkStore:
    """Adapter for the installed Supermemory SDK, with capability checks."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        try:
            from supermemory import Supermemory
        except ImportError as exc:
            raise ProviderCapabilityError(
                "the Supermemory Python SDK is required for ingestion"
            ) from exc
        client_args: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout,
            "max_retries": 0,
        }
        if base_url:
            client_args["base_url"] = base_url.rstrip("/")
        self._client = Supermemory(**client_args)
        try:
            version = importlib.metadata.version("supermemory")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        self.provenance = {
            "package": "supermemory",
            "version": version,
            "client": "Supermemory",
            "base_url": str(self._client.base_url),
        }

    def add_document(
        self,
        *,
        content: str,
        container_tag: str,
        source_id: str,
        document_date: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        documents = getattr(self._client, "documents", None)
        method = getattr(documents, "add", None)
        if not callable(method):
            raise ProviderCapabilityError("Supermemory SDK has no documents.add method")
        result = method(
            content=content,
            container_tag=container_tag,
            custom_id=source_id,
            document_date=document_date,
            metadata=metadata,
        )
        normalized = normalize_document(result)
        if not normalized["id"]:
            raise ProviderCapabilityError("Supermemory documents.add returned no document ID")
        normalized["source_id"] = normalized["source_id"] or source_id
        return normalized

    @staticmethod
    def _list_kwargs(method: Callable[..., Any], container_tag: str, page: int) -> dict[str, Any]:
        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_any = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
        kwargs: dict[str, Any] = {}

        def put(names: tuple[str, ...], value: Any) -> bool:
            for name in names:
                if name in parameters or accepts_any:
                    kwargs[name] = value
                    return True
            return False

        for name in ("container_tag", "containerTag", "container_tags", "containerTags"):
            if name in parameters or accepts_any:
                kwargs[name] = [container_tag] if name in {"container_tags", "containerTags"} else container_tag
                break
        else:
            raise ProviderCapabilityError(
                "Supermemory document listing cannot be scoped to a container tag"
            )
        put(("include_content", "includeContent"), True)
        put(("limit", "page_size", "pageSize"), 200)
        put(("order",), "asc")
        put(("sort",), "createdAt")
        if not put(("page",), page) and page > 1:
            raise ProviderCapabilityError("Supermemory document listing cannot continue to the next page")
        return kwargs

    def list_documents(self, *, container_tag: str) -> list[dict[str, Any]]:
        documents = getattr(self._client, "documents", None)
        method = getattr(documents, "list", None) or getattr(documents, "list_documents", None)
        if not callable(method):
            raise ProviderCapabilityError(
                "Supermemory SDK must expose documents.list or documents.list_documents "
                "for durable resume and count verification"
            )
        output: list[dict[str, Any]] = []
        page = 1
        for _page in range(10000):
            response = method(**self._list_kwargs(method, container_tag, page))
            raw, pagination = _response_documents(response)
            output.extend(normalize_document(item) for item in raw)
            total_pages = _field(pagination, "total_pages", "totalPages") if pagination else None
            current_page = _field(pagination, "current_page", "currentPage") if pagination else page
            if total_pages is None or int(current_page) >= int(total_pages):
                return output
            next_page = int(current_page) + 1
            if next_page <= page:
                raise ProviderCapabilityError("Supermemory document listing returned a non-increasing page")
            page = next_page
        raise ProviderCapabilityError("Supermemory document listing exceeded 10,000 pages")

    def get_document(self, document_id: str) -> dict[str, Any]:
        documents = getattr(self._client, "documents", None)
        method = getattr(documents, "get", None)
        if not callable(method):
            raise ProviderCapabilityError("Supermemory SDK has no documents.get method")
        return normalize_document(method(document_id))


def _new_receipt(
    *,
    persona: str,
    container_tag: str,
    corpus_path: Path,
    corpus_sha256: str,
    items: list[CorpusItem],
) -> dict[str, Any]:
    now = _utc_now()
    manifest = [_manifest_entry(item) for item in items]
    source_ids_digest = hashlib.sha256(
        "\n".join(item["source_id"] for item in manifest).encode()
    ).hexdigest()
    return {
        "receipt_version": RECEIPT_VERSION,
        "provider": "supermemory",
        "persona": persona,
        "container_tag": container_tag,
        "created_at": now,
        "updated_at": now,
        "corpus": {
            "path": str(corpus_path.resolve()),
            "sha256": corpus_sha256,
            "source_count": len(items),
            "source_ids_sha256": source_ids_digest,
        },
        "manifest": manifest,
        "sources": {
            entry["source_id"]: {
                **entry,
                "status": "pending",
                "attempts": [],
            }
            for entry in manifest
        },
        "status": "in_progress",
    }


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _manifest_entry(item: CorpusItem) -> dict[str, Any]:
    return {
        "source_id": item.source_id,
        "session_id": item.session_id,
        "message_index": item.message_index,
        "narrative_date": item.narrative_date,
        "content_sha256": hashlib.sha256(item.content.encode()).hexdigest(),
    }


def _check_receipt(
    receipt: dict[str, Any],
    *,
    persona: str,
    container_tag: str,
    corpus_path: Path,
    corpus_sha256: str,
    expected_ids: set[str],
    expected_manifest: list[dict[str, Any]],
) -> None:
    if receipt.get("provider") != "supermemory":
        raise IngestionError("receipt belongs to a different provider")
    if receipt.get("persona") != persona or receipt.get("container_tag") != container_tag:
        raise IngestionError("receipt belongs to a different persona or container")
    corpus = receipt.get("corpus") or {}
    if corpus.get("path") != str(corpus_path.resolve()) or corpus.get("sha256") != corpus_sha256:
        raise IngestionError("receipt does not match the immutable corpus file")
    if corpus.get("source_count") != len(expected_ids):
        raise IngestionError("receipt source count does not match the immutable corpus")
    expected_source_ids_digest = hashlib.sha256(
        "\n".join(entry["source_id"] for entry in expected_manifest).encode()
    ).hexdigest()
    if corpus.get("source_ids_sha256") != expected_source_ids_digest:
        raise IngestionError("receipt source ID digest does not match the immutable corpus")
    if receipt.get("manifest") != expected_manifest:
        raise IngestionError("receipt input manifest does not match the immutable corpus")
    sources = receipt.get("sources") or {}
    if set(sources) != expected_ids:
        missing = sorted(expected_ids - set(sources))
        extra = sorted(set(sources) - expected_ids)
        raise IngestionError(
            f"receipt source records do not match the immutable corpus: "
            f"missing={missing[:3]}, extra={extra[:3]}"
        )


def _remote_index(documents: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for document in documents:
        source_id = str(document.get("source_id") or "")
        if not source_id:
            raise IngestionError(
                "a Supermemory document has no dolphinbench source ID; refusing to mix it into the final container"
            )
        if source_id in index:
            raise IngestionError(f"Supermemory contains duplicate source ID: {source_id}")
        index[source_id] = document
    return index


def verify_complete(
    store: DocumentStore,
    *,
    container_tag: str,
    expected_ids: set[str],
    expected_content_sha256: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Verify exact source coverage, content, scope, and provider status."""
    remote = _remote_index(store.list_documents(container_tag=container_tag))
    actual_ids = set(remote)
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise IngestionError(
            f"Supermemory count/source check failed: expected {len(expected_ids)}, "
            f"found {len(actual_ids)}; missing={missing[:3]}, extra={extra[:3]}"
        )
    for source_id, document in remote.items():
        tags = document.get("container_tags") or []
        if tags and container_tag not in tags:
            raise IngestionError(f"Supermemory document {source_id} is outside container {container_tag}")
    failed = sorted(
        (source_id, str(document.get("status")))
        for source_id, document in remote.items()
        if document.get("status") in _FAILED_STATES
    )
    if failed:
        states: list[str] = []
        get_document = getattr(store, "get_document", None)
        for source_id, status in failed[:3]:
            detail = ""
            if callable(get_document):
                document = remote[source_id]
                full_document = get_document(str(document.get("id") or ""))
                processing = full_document.get("processing") or {}
                errors = [str(processing.get("error") or "").strip()]
                steps = processing.get("steps") or []
                if isinstance(steps, list):
                    errors.extend(
                        str(step.get("error") or "").strip()
                        for step in steps
                        if isinstance(step, Mapping)
                    )
                errors = [error for error in errors if error]
                if errors:
                    detail = f": {' | '.join(dict.fromkeys(errors))}"
            states.append(f"{source_id}={status}{detail}")
        raise IngestionError(
            f"Supermemory has {len(failed)} documents in terminal failure states; "
            + "; ".join(states)
        )
    pending = sorted(
        source_id for source_id, document in remote.items()
        if document.get("status") in _PENDING_STATES
    )
    if pending:
        raise PendingDocumentsError(
            f"Supermemory has {len(pending)} documents still processing; first={pending[0]}"
        )
    unknown_status = sorted(
        (source_id, str(document.get("status")))
        for source_id, document in remote.items()
        if document.get("status") not in _SUCCESS_STATES
    )
    if unknown_status:
        states = ", ".join(f"{source_id}={status or '<missing>'}" for source_id, status in unknown_status[:3])
        raise ProviderCapabilityError(f"Supermemory returned an unrecognized document status: {states}")
    if expected_content_sha256 is not None:
        if set(expected_content_sha256) != expected_ids:
            raise IngestionError("expected content hashes do not match expected source IDs")
        mismatches: list[str] = []
        for source_id, expected_hash in expected_content_sha256.items():
            actual_hash = remote[source_id].get("content_sha256")
            if not actual_hash:
                raise ProviderCapabilityError(
                    f"Supermemory listing did not return content or provenance hash for {source_id}"
                )
            if actual_hash != expected_hash:
                mismatches.append(f"{source_id} expected={expected_hash} actual={actual_hash}")
        if mismatches:
            raise IngestionError(
                "Supermemory content verification failed: " + "; ".join(mismatches[:3])
            )
    return {
        "count": len(actual_ids),
        "source_ids": sorted(actual_ids),
        "status_counts": {
            status: sum(document.get("status") == status for document in remote.values())
            for status in sorted(_SUCCESS_STATES)
        },
        "content_verified": expected_content_sha256 is not None,
    }


def wait_for_complete(
    store: DocumentStore,
    *,
    container_tag: str,
    expected_ids: set[str],
    expected_content_sha256: Mapping[str, str],
    timeout_seconds: float,
    poll_interval_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll until every expected document is done, or fail with a clear receipt error."""
    if timeout_seconds < 0 or poll_interval_seconds < 0:
        raise IngestionError("settle timeout and poll interval must be non-negative")
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            return verify_complete(
                store,
                container_tag=container_tag,
                expected_ids=expected_ids,
                expected_content_sha256=expected_content_sha256,
            )
        except PendingDocumentsError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IngestionError(f"Supermemory processing did not finish within {timeout_seconds:g}s: {exc}") from exc
            sleep(min(poll_interval_seconds, remaining))


def wait_for_submitted_sources(
    store: DocumentStore,
    *,
    container_tag: str,
    source_ids: set[str],
    expected_content_sha256: Mapping[str, str],
    timeout_seconds: float,
    poll_interval_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, dict[str, Any]]:
    """Wait until already-submitted sources have a final successful provider state.

    This deliberately reads the provider before every retry. A receipt that says a
    source was submitted is never enough reason to submit it again.
    """

    if timeout_seconds < 0 or poll_interval_seconds < 0:
        raise IngestionError("settle timeout and poll interval must be non-negative")
    deadline = time.monotonic() + timeout_seconds
    while True:
        remote = _remote_index(store.list_documents(container_tag=container_tag))
        missing = sorted(source_ids - set(remote))
        if missing:
            raise IngestionError(
                "provider no longer lists a submitted source; refusing to submit it again: "
                f"{missing[0]}"
            )
        pending: list[str] = []
        complete: dict[str, dict[str, Any]] = {}
        for source_id in source_ids:
            document = remote[source_id]
            _assert_remote_matches(
                document,
                source_id=source_id,
                expected_hash=expected_content_sha256[source_id],
            )
            status = str(document.get("status") or "").lower()
            if status in _SUCCESS_STATES:
                complete[source_id] = document
            elif status in _PENDING_STATES:
                pending.append(source_id)
            else:
                raise ProviderCapabilityError(
                    f"Supermemory returned an unrecognized document status for {source_id}: {status!r}"
                )
        if not pending:
            return complete
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IngestionError(
                "Supermemory submitted sources did not finish within "
                f"{timeout_seconds:g}s: first={pending[0]}"
            )
        sleep(min(poll_interval_seconds, remaining))


def _assert_remote_matches(
    document: Mapping[str, Any], *, source_id: str, expected_hash: str
) -> None:
    provider_id = str(document.get("id") or "")
    if not provider_id:
        raise ProviderCapabilityError(f"Supermemory document {source_id} has no provider ID")
    status = str(document.get("status") or "").lower()
    if status in _FAILED_STATES:
        raise IngestionError(f"Supermemory document {source_id} is in failure state {status!r}")
    actual_hash = str(document.get("content_sha256") or "")
    if actual_hash and actual_hash != expected_hash:
        raise IngestionError(
            f"Supermemory document {source_id} has different content: "
            f"expected={expected_hash} actual={actual_hash}"
        )


KNOWN_ID = "MR39jbNFQR1k5cGN7syvkm"
KNOWN_SOURCE = "session-extension_morgan_2024_05_06_004:0"
KNOWN_HASH = "6179b80af82dad93dcbd7ea53c51778e877d8819cb3a3547e4150a2a24f79da9"
TAG = "dolphinbench_morgan_500k_final_v1"
PENDING = {"queued", "extracting", "chunking", "embedding", "indexing", "processing"}


def check_documents(documents, *, allow_known_failure=True):
    """Accept only the inspected failure; every other failure remains fatal."""
    known = [d for d in documents if d.get("id") == KNOWN_ID]
    if allow_known_failure and (len(known) != 1 or known[0].get("customId") != KNOWN_SOURCE):
        raise RuntimeError("Approved Supermemory failure is absent or has changed identity")
    for document in documents:
        status = document.get("status")
        if status == "done" or status in PENDING:
            continue
        if allow_known_failure and document.get("id") == KNOWN_ID and status == "failed":
            continue
        raise RuntimeError(f"New Supermemory document failure: {document.get('id')} {status}")
    return sum(d.get("status") in PENDING for d in documents)


def request(key, path, payload=None, *, retry_timeout=900, require_running=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:6767" + path, data=data,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    read_only = path == "/v3/documents/list" or (
        payload is None and path.startswith("/v3/documents/")
    )
    deadline = time.monotonic() + retry_timeout
    attempt = 0
    while True:
        if require_running is not None:
            require_running()
        try:
            with urllib.request.urlopen(req, timeout=min(60, max(1, deadline - time.monotonic()))) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in {408, 429, 500, 502, 503, 504}:
                raise
            error = exc
            exc.close()
        except (TimeoutError, ConnectionError, urllib.error.URLError,
                http.client.RemoteDisconnected, http.client.IncompleteRead) as exc:
            error = exc
        if not read_only or time.monotonic() >= deadline:
            raise error
        attempt += 1
        delay = min(10, 2 * attempt, max(0, deadline - time.monotonic()))
        print(json.dumps({"supermemory_read_retry": attempt, "path": path,
                          "error_type": type(error).__name__, "delay_seconds": delay}), flush=True)
        time.sleep(delay)


# Stuck processing becomes retryable after 30 minutes; the retry check runs
# every 30 minutes. Leave time for that check and the resulting processing.
def wait_for_processing(key, require_running, timeout=5400, *, container_tag=TAG, allow_known_failure=True):
    if allow_known_failure:
        if container_tag != TAG:
            raise RuntimeError("The document exception is only approved for Morgan")
        known = request(key, "/v3/documents/" + KNOWN_ID)
        if (known.get("customId") != KNOWN_SOURCE or
                hashlib.sha256(known.get("content", "").encode()).hexdigest() != KNOWN_HASH):
            raise RuntimeError("Approved Supermemory exception content or identity changed")
    deadline = time.monotonic() + timeout
    unfinished = []
    last_report = None
    last_report_time = 0
    while time.monotonic() < deadline:
        require_running()
        documents = []
        for page in range(1, 10001):
            response = request(key, "/v3/documents/list", {
                "containerTags": [container_tag], "page": page, "limit": 200,
                "sort": "createdAt", "order": "asc", "includeContent": False,
            }, retry_timeout=min(900, max(0, deadline - time.monotonic())),
                require_running=require_running)
            rows = response.get("memories")
            if not isinstance(rows, list):
                raise RuntimeError("Unexpected Supermemory document listing")
            documents.extend(rows)
            pagination = response.get("pagination", {})
            if "totalPages" not in pagination or "totalItems" not in pagination:
                raise RuntimeError("Supermemory listing lacks pagination evidence")
            if page >= pagination["totalPages"]:
                if len(documents) != pagination["totalItems"]:
                    raise RuntimeError("Supermemory listing is incomplete")
                break
        else:
            raise RuntimeError("Supermemory listing exceeded page limit")
        pending = check_documents(documents, allow_known_failure=allow_known_failure)
        if not pending:
            if not allow_known_failure:
                return {
                    "status": "completed", "documents": len(documents), "pending_documents": 0,
                    "approved_failed_documents": [], "ingest_concurrency": 1,
                }
            return {
                "status": "completed_with_document_failure",
                "documents": len(documents), "pending_documents": 0,
                "approved_failed_documents": [{"id": KNOWN_ID, "custom_id": KNOWN_SOURCE,
                                               "content_sha256": KNOWN_HASH}],
                "approval": "User approved leaving this document unchanged on 2026-09-09",
                "ingest_concurrency": 1,
            }
        unfinished = [
            {"id": d.get("id"), "customId": d.get("customId"),
             "status": d.get("status"), "updatedAt": d.get("updatedAt"),
             "retry_count": (d.get("processingMetadata") or {}).get("processingRetryCount"),
             "retry_state": (d.get("processingMetadata") or {}).get("ingestRetryState")}
            for d in documents if d.get("status") in PENDING
        ]
        report = json.dumps({"supermemory_processing_pending": pending, "documents": unfinished})
        now = time.monotonic()
        if report != last_report or now - last_report_time >= 60:
            print(report, flush=True)
            last_report, last_report_time = report, now
        time.sleep(1)
    raise TimeoutError(
        f"Supermemory background processing did not finish within {timeout:g} seconds; "
        f"unfinished documents: {json.dumps(unfinished)}"
    )
