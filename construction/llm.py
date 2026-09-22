"""Small resumable JSON client for construction calls."""

from __future__ import annotations

import hashlib
import json
import os
from harness.environment import get_setting, openai_config_path
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from openai import AzureOpenAI, OpenAI


_CALL_LEDGER_LOCK = threading.Lock()


def request_hash(
    system: str,
    payload: dict[str, Any] | str,
    *,
    response_schema: dict[str, Any] | None = None,
    response_schema_name: str | None = None,
) -> str:
    request: dict[str, Any] = {"system": system, "payload": payload}
    if response_schema is not None:
        request["response_schema"] = {
            "name": response_schema_name,
            "schema": response_schema,
        }
    raw = json.dumps(
        request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _append_direct_call(
    *,
    call_path: Path | None,
    system: str,
    payload: dict[str, Any] | str,
    model: str,
    response: dict[str, Any] | None,
    started_at: str,
    error: Exception | None,
    response_schema: dict[str, Any] | None = None,
    response_schema_name: str | None = None,
) -> None:
    ledger_value = get_setting("DOLPHINBENCH_CALL_LEDGER")
    if call_path is None or not ledger_value:
        return
    row: dict[str, Any] = {
        "cache_path": str(call_path),
        "request_sha256": request_hash(
            system,
            payload,
            response_schema=response_schema,
            response_schema_name=response_schema_name,
        ),
        "model": model,
        "cache_hit": False,
        "response_id": response.get("_response_id") if response else None,
        "usage": dict(response.get("_usage") or {}) if response else {},
        "started_at": started_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
    }
    run_id = get_setting("DOLPHINBENCH_CONSTRUCTION_RUN_ID")
    if run_id:
        row["run_id"] = run_id
    if error is not None:
        row["error"] = f"{type(error).__name__}: {error}"
    ledger_path = Path(ledger_value).expanduser()
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with _CALL_LEDGER_LOCK:
        with ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _credentials() -> tuple[str, str]:
    key = os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    base = os.environ.get("AZURE_OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    if key and base:
        return key, base

    path = openai_config_path()
    if not path.exists():
        raise RuntimeError(
            "Set AZURE_OPENAI_API_KEY and AZURE_OPENAI_BASE_URL, or "
            "DOLPHINBENCH_OPENAI_CONFIG to an OpenAI-compatible provider config."
        )
    provider = yaml.safe_load(path.read_text())["llm"]["providers"]["openai"]
    return provider["api_key"], provider["base_url"]


class JsonClient:
    def __init__(self, model: str = "gpt-5.4") -> None:
        key, base = _credentials()
        self.model = model
        self.client = OpenAI(api_key=key, base_url=base, timeout=300.0, max_retries=5)

    def complete(self, system: str, payload: dict[str, Any], max_tokens: int = 30000) -> dict[str, Any]:
        delay = 2.0
        last_error: Exception | None = None
        for attempt in range(8):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    response_format={"type": "json_object"},
                    max_completion_tokens=max_tokens,
                )
                content = response.choices[0].message.content or ""
                parsed = json.loads(content)
                usage = response.usage
                parsed["_usage"] = {
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                }
                return parsed
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                transient = any(
                    marker in str(exc).lower()
                    for marker in ("429", "rate", "timeout", "502", "503", "504", "connection")
                )
                if attempt == 7 or not transient:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise RuntimeError(f"construction model call failed: {last_error}") from last_error


class AzureJsonClient:
    """JSON client for the Azure-hosted construction writer."""

    def __init__(
        self, model: str = "gpt-5.4", *, reasoning_effort: str = "high",
        timeout: float = 900.0, max_retries: int = 3,
        max_completion_tokens: int | None = None,
    ) -> None:
        key = os.environ.get("AZURE_OPENAI_API_KEY")
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
        if not key or not endpoint:
            raise RuntimeError("AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT are required")
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_completion_tokens = max_completion_tokens
        self.client = AzureOpenAI(
            api_key=key,
            azure_endpoint=endpoint,
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
            timeout=timeout,
            max_retries=max_retries,
        )

    def complete(
        self,
        system: str,
        payload: dict[str, Any] | str,
        *,
        call_path: Path | None = None,
        response_schema: dict[str, Any] | None = None,
        response_schema_name: str | None = None,
    ) -> dict[str, Any]:
        if (response_schema is None) != (response_schema_name is None):
            raise ValueError(
                "response_schema and response_schema_name must be supplied together"
            )
        response_format: dict[str, Any]
        if response_schema is None:
            response_format = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema_name,
                    "strict": True,
                    "schema": response_schema,
                },
            }
        started_at = datetime.now(timezone.utc).isoformat()
        parsed: dict[str, Any] | None = None
        error: Exception | None = None
        try:
            request = dict(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": (
                            payload
                            if isinstance(payload, str)
                            else json.dumps(payload, ensure_ascii=False)
                        ),
                    },
                ],
                response_format=response_format,
                reasoning_effort=self.reasoning_effort,
            )
            from harness.paid_budget import active_budget, budgeted_completion
            if self.max_completion_tokens is not None:
                request["max_completion_tokens"] = self.max_completion_tokens
            budget = active_budget()
            from harness.provider_capacity import chat_completion, provider_call
            with provider_call(model=self.model, request=request, kind="authoring") as transport:
                response = (budgeted_completion(self.client, request, budget) if budget is not None
                            else chat_completion(self.client, request, transport))
                if hasattr(response.usage, "model_dump"):
                    transport["usage"] = response.usage.model_dump()
            choice = response.choices[0]
            content = choice.message.content or ""
            if not content:
                usage = response.usage
                raise RuntimeError(
                    "construction model returned an empty message: "
                    f"response_id={response.id}, "
                    f"finish_reason={choice.finish_reason}, "
                    f"refusal={getattr(choice.message, 'refusal', None)!r}, "
                    f"prompt_tokens={getattr(usage, 'prompt_tokens', 0) or 0}, "
                    f"completion_tokens={getattr(usage, 'completion_tokens', 0) or 0}, "
                    "reasoning_tokens="
                    f"{getattr(getattr(usage, 'completion_tokens_details', None), 'reasoning_tokens', 0) or 0}"
                )
            parsed = json.loads(content)
            usage = response.usage
            parsed["_usage"] = {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                "reasoning_tokens": getattr(
                    getattr(usage, "completion_tokens_details", None),
                    "reasoning_tokens",
                    0,
                )
                or 0,
                "total_tokens": getattr(usage, "total_tokens", 0) or 0,
            }
            parsed["_response_id"] = response.id
            return parsed
        except Exception as exc:
            error = exc
            raise
        finally:
            _append_direct_call(
                call_path=call_path,
                system=system,
                payload=payload,
                model=self.model,
                response=parsed,
                started_at=started_at,
                error=error,
                response_schema=response_schema,
                response_schema_name=response_schema_name,
            )


class OpenAIResponsesJsonClient:
    """JSON client for the direct OpenAI construction reviewer."""

    def __init__(self, model: str = "gpt-5.5") -> None:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is required")
        self.model = model
        self.client = OpenAI(api_key=key, timeout=900.0, max_retries=2)

    def complete(
        self,
        system: str,
        payload: dict[str, Any],
        *,
        response_schema: dict[str, Any] | None = None,
        response_schema_name: str | None = None,
    ) -> dict[str, Any]:
        if (response_schema is None) != (response_schema_name is None):
            raise ValueError(
                "response_schema and response_schema_name must be supplied together"
            )
        if response_schema is None:
            text_format: dict[str, Any] = {"type": "json_object"}
        else:
            text_format = {
                "type": "json_schema",
                "name": response_schema_name,
                "strict": True,
                "schema": response_schema,
            }
        response = self.client.responses.create(
            model=self.model,
            reasoning={"effort": "high"},
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            text={"format": text_format},
        )
        parsed = json.loads(response.output_text)
        usage = response.usage
        parsed["_usage"] = {
            "prompt_tokens": getattr(usage, "input_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
            "total_tokens": getattr(usage, "total_tokens", 0) or 0,
        }
        parsed["_response_id"] = response.id
        return parsed
