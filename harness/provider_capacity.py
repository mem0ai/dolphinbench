"""Opt-in cross-process capacity and transport timing for staged authoring."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from harness.environment import get_setting
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from harness.durable_json import atomic_json


@lru_cache(maxsize=1)
def _encoding():
    import tiktoken
    return tiktoken.get_encoding("o200k_base")


def _estimated_tokens(request: dict) -> int:
    return len(_encoding().encode(json.dumps(request, ensure_ascii=False), disallowed_special=())) + int(
        request.get("max_completion_tokens", request.get("max_output_tokens", 16384)))


def _safe_headers(exc: BaseException) -> dict[str, str]:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        headers = getattr(exc, "headers", {})
    return safe_rate_headers(headers)


def safe_rate_headers(headers: Any) -> dict[str, str]:
    allowed = {"retry-after", "retry-after-ms", "x-ms-retry-after-ms",
               "x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens",
               "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests", "x-ratelimit-reset-requests"}
    return {str(k).lower(): str(v) for k, v in headers.items() if str(k).lower() in allowed}


def chat_completion(client: Any, request: dict, transport: dict) -> Any:
    if not get_setting("DOLPHINBENCH_AUTHORING_PROVIDER_CAPACITY"):
        return client.chat.completions.create(**request)
    raw = client.with_raw_response.chat.completions.create(**request)
    transport["rate_limit_headers"] = safe_rate_headers(raw.headers)
    return raw.parse()


def _retry_delay(headers: dict[str, str], now: float) -> float:
    for name, scale in (("retry-after-ms", .001), ("x-ms-retry-after-ms", .001), ("retry-after", 1)):
        if name in headers:
            try:
                return max(1., float(headers[name]) * scale)
            except ValueError:
                if name == "retry-after":
                    try:
                        return max(1., parsedate_to_datetime(headers[name]).timestamp() - now)
                    except (ValueError, TypeError, OverflowError):
                        pass
    return 60.


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _rate_state(root: Path, model: str, tokens: int, *, ticket: str | None = None,
              enqueue_only: bool = False, cancel: bool = False, cooldown: float = 0,
              headers: dict[str, str] | None = None, observed_start: float = 0) -> float:
    key = hashlib.sha256(model.encode()).hexdigest()[:24]
    path = root / f"rate-{key}.json"
    with (root / f"rate-{key}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else {}
        previous = state.copy()
        queue = [row for row in state.get("queue", []) if _alive(row["pid"])]
        now = time.time()
        feedback = get_setting("DOLPHINBENCH_AUTHORING_PROVIDER_HEADER_FEEDBACK") == "1"
        active = {key: row for key, row in state.get("active", {}).items() if _alive(row["pid"])}
        if cancel:
            queue = [row for row in queue if row["ticket"] != ticket]
            active.pop(ticket, None)
            if feedback and headers and observed_start >= state.get("observed_start", 0):
                try:
                    limit = int(headers["x-ratelimit-limit-tokens"])
                    remaining = int(headers["x-ratelimit-remaining-tokens"])
                    reset = float(headers["x-ratelimit-reset-tokens"])
                    if limit <= 0 or not 0 <= remaining <= limit or not 0 <= reset <= 60:
                        raise ValueError("invalid token headroom")
                except (KeyError, ValueError, TypeError):
                    pass
                else:
                    configured = int(get_setting("DOLPHINBENCH_AUTHORING_PROVIDER_TPM", "300000"))
                    # Reserve all other active calls even if the header already counted them.
                    state["available_tokens"] = max(0, min(configured, int(.9 * limit), int(.9 * remaining))
                                                       - sum(row["tokens"] for row in active.values()))
                    state["headroom_until"] = now + max(10., reset)
                    state["observed_start"] = observed_start
                    rpm = int(get_setting("DOLPHINBENCH_AUTHORING_PROVIDER_RPM", "60"))
                    state["next_at"] = min(state.get("next_at", now), now + 60 / rpm)
        elif ticket is not None and not any(row["ticket"] == ticket for row in queue):
            queue.append({"ticket": ticket, "pid": os.getpid()})
        delay = max(0., state.get("pause_until", 0) - now, state.get("next_at", 0) - now)
        if cooldown:
            state["pause_until"] = max(state.get("pause_until", 0), now + cooldown)
            delay = max(delay, cooldown)
        elif not cancel and not enqueue_only:
            if queue and queue[0]["ticket"] != ticket:
                delay = max(delay, .25)
            current_headroom = feedback and now < state.get("headroom_until", 0)
            if current_headroom and tokens > state.get("available_tokens", 0):
                delay = max(delay, min(1., state["headroom_until"] - now))
            if not delay:
                tpm = int(get_setting("DOLPHINBENCH_AUTHORING_PROVIDER_TPM", "300000"))
                rpm = int(get_setting("DOLPHINBENCH_AUTHORING_PROVIDER_RPM", "60"))
                if tpm <= 0 or rpm <= 0:
                    raise ValueError("provider rate limits must be positive")
                state["next_at"] = now + (60 / rpm if current_headroom else max(60 * tokens / tpm, 60 / rpm))
                if feedback:
                    active[ticket] = {"tokens": tokens, "pid": os.getpid()}
                    if current_headroom:
                        state["available_tokens"] -= tokens
                if queue:
                    queue.pop(0)
        state["queue"] = queue
        if feedback:
            state["active"] = active
        if state != previous:
            atomic_json(path, state)
        return delay


@contextmanager
def provider_call(*, model: str, request: dict[str, Any], kind: str):
    directory = get_setting("DOLPHINBENCH_AUTHORING_PROVIDER_CAPACITY")
    if not directory:
        yield {}
        return
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    slots = int(get_setting("DOLPHINBENCH_AUTHORING_PROVIDER_SLOTS"))
    if not 1 <= slots <= 128:
        raise ValueError("invalid authoring provider capacity")
    started = datetime.now(timezone.utc).isoformat()
    queued = time.monotonic()
    held = None
    response: dict[str, Any] = {}
    error = None
    ticket = None
    try:
        tokens = _estimated_tokens(request)
        response["estimated_reserved_tokens"] = tokens
        ticket = uuid.uuid4().hex
        _rate_state(root, model, tokens, ticket=ticket, enqueue_only=True)
        while held is None:
            if time.monotonic() - queued > 600:
                raise TimeoutError("authoring provider capacity wait exceeded 600 seconds")
            for index in range(slots):
                handle = (root / f"{index:03d}.lock").open("a")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                else:
                    held = handle
                    break
            if held is not None:
                delay = _rate_state(root, model, tokens, ticket=ticket)
                if delay:
                    fcntl.flock(held, fcntl.LOCK_UN)
                    held.close()
                    held = None
                    time.sleep(min(delay, 1.))
            else:
                time.sleep(.05)
        response["capacity_wait_seconds"] = round(time.monotonic() - queued, 3)
        response["provider_started_at"] = datetime.now(timezone.utc).isoformat()
        yield response
    except BaseException as exc:
        error = type(exc).__name__
        headers = _safe_headers(exc)
        if headers:
            response["rate_limit_headers"] = headers
        if getattr(exc, "status_code", getattr(exc, "code", None)) == 429:
            response["cooldown_seconds"] = _retry_delay(headers, time.time())
            _rate_state(root, model, 0, cooldown=response["cooldown_seconds"])
        raise
    finally:
        if held is not None:
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()
        if ticket is not None:
            observed_start = response.get("provider_started_at")
            _rate_state(root, model, 0, ticket=ticket, cancel=True,
                        headers=response.get("rate_limit_headers"),
                        observed_start=datetime.fromisoformat(observed_start).timestamp() if observed_start else 0)
        ledger = get_setting("DOLPHINBENCH_AUTHORING_TRANSPORT_LEDGER")
        if ledger:
            path = Path(ledger)
            path.parent.mkdir(parents=True, exist_ok=True)
            row = {"kind": kind, "model": model, "started_at": started,
                   "ended_at": datetime.now(timezone.utc).isoformat(), "error_type": error,
                   "request_sha256": hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                   **response}
            with path.open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                handle.write(json.dumps(row) + "\n")
                fcntl.flock(handle, fcntl.LOCK_UN)
