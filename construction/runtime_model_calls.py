"""Cached model-call recording used by the canonical quarter runtime."""

from __future__ import annotations

import json
from harness.environment import get_setting
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from construction.llm import AzureJsonClient, OpenAIResponsesJsonClient, request_hash


_CALL_LEDGER_LOCK = threading.Lock()


def copy_data(value: Any) -> Any:
    return json.loads(json.dumps(value))


def append_call_ledger(
    *,
    cache_path: Path,
    fingerprint: str,
    model: str,
    cache_hit: bool,
    response: dict[str, Any] | None,
    started_at: str,
    ended_at: str,
    error: Exception | None = None,
) -> None:
    ledger_value = get_setting("DOLPHINBENCH_CALL_LEDGER")
    if not ledger_value:
        return
    row: dict[str, Any] = {
        "cache_path": str(cache_path),
        "request_sha256": fingerprint,
        "model": model,
        "cache_hit": cache_hit,
        "response_id": response.get("_response_id") if response else None,
        "usage": copy_data(response.get("_usage") or {}) if response else {},
        "started_at": started_at,
        "ended_at": ended_at,
    }
    run_id = get_setting("DOLPHINBENCH_CONSTRUCTION_RUN_ID")
    if run_id:
        row["run_id"] = run_id
    if error is not None:
        row["error"] = f"{type(error).__name__}: {error}"
    ledger_path = Path(ledger_value).expanduser()
    line = json.dumps(row, ensure_ascii=False) + "\n"
    with _CALL_LEDGER_LOCK:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(line)


def cached_client_complete(
    path: Path,
    system: str,
    payload: dict[str, Any] | str,
    client: AzureJsonClient | OpenAIResponsesJsonClient,
    *,
    response_schema: dict[str, Any] | None = None,
    response_schema_name: str | None = None,
) -> dict[str, Any]:
    if (response_schema is None) != (response_schema_name is None):
        raise ValueError(
            "response_schema and response_schema_name must be supplied together"
        )
    fingerprint = request_hash(
        system,
        payload,
        response_schema=response_schema,
        response_schema_name=response_schema_name,
    )
    started_at = datetime.now(timezone.utc).isoformat()
    response: dict[str, Any] | None = None
    cache_hit = False
    error: Exception | None = None
    model = str(getattr(client, "model", type(client).__name__))
    try:
        if path.exists():
            cached = json.loads(path.read_text())
            if cached.get("request_sha256") == fingerprint:
                response = cached["response"]
                cache_hit = True
                return response
        if response_schema is None:
            response = client.complete(system, payload)
        else:
            response = client.complete(
                system,
                payload,
                response_schema=response_schema,
                response_schema_name=response_schema_name,
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"request_sha256": fingerprint, "response": response}, indent=2, ensure_ascii=False) + "\n")
        return response
    except Exception as exc:
        error = exc
        raise
    finally:
        append_call_ledger(
            cache_path=path,
            fingerprint=fingerprint,
            model=model,
            cache_hit=cache_hit,
            response=response,
            started_at=started_at,
            ended_at=datetime.now(timezone.utc).isoformat(),
            error=error,
        )
