"""Observe Hermes lifecycle hooks without changing model or tool execution."""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from pathlib import Path

from harness.submission import SETTINGS_FIELDS, SubmissionError, clean_message, load_json


def _text(content):
    if isinstance(content, list) and all(
        isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"}
        and isinstance(part.get("text"), str) for part in content
    ):
        return "".join(part["text"] for part in content)
    return content


def chat_messages(raw: list[dict], system_prompt: str | None = None) -> list[dict]:
    messages = []
    for item in raw:
        kind = item.get("type")
        if kind == "reasoning":
            continue
        if kind == "function_call":
            call = {"id": item["call_id"], "name": item["name"], "arguments": item["arguments"]}
            if messages and messages[-1]["role"] == "assistant":
                messages[-1].setdefault("tool_calls", []).append(call)
                messages[-1] = clean_message(messages[-1])
            else:
                messages.append(clean_message({"role": "assistant", "tool_calls": [call]}))
        elif kind == "function_call_output":
            messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
        elif "role" in item:
            message = clean_message(item)
            if "content" in message:
                message["content"] = _text(message["content"])
            messages.append(message)
        else:
            raise SubmissionError("Unsupported provider message in submission recording")
    if system_prompt and not any(m.get("role") == "system" and m.get("content") == system_prompt for m in messages):
        messages.insert(0, {"role": "system", "content": system_prompt})
    return messages


def _plain(message):
    result = {key: value for key, value in clean_message(message).items()
              if key not in {"usage", "duration_ms", "settings"}}
    if result.get("tool_calls") and not result.get("content"):
        result.pop("content", None)
    return result


def build_rollout(events: list[dict]) -> dict:
    messages = []
    settings = None
    for event in events:
        if event.get("error"):
            raise SubmissionError(f"Submission recording failed: {event['error']}")
        current = event["settings"]
        if settings is None:
            settings = copy.deepcopy(current)
        request = event["messages"]
        if (len(request) < len(messages)
                or [_plain(m) for m in request[:len(messages)]] != [_plain(m) for m in messages]):
            raise SubmissionError("Conversation context was rewritten; cannot silently export it as an append-only rollout")
        messages.extend(copy.deepcopy(request[len(messages):]))
        answer = copy.deepcopy(event["assistant"])
        if current != settings:
            answer["settings"] = copy.deepcopy(current)
        messages.append(answer)
    if settings is None:
        raise SubmissionError("No recorded model responses")
    return {"settings": settings, "messages": messages}


class Recorder:
    def __init__(self, directory: Path):
        self.directory = directory
        self.pending = {}
        self.lock = threading.Lock()

    def write(self, session_id: str, event: dict) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise ValueError("Invalid Hermes session ID")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{session_id}.jsonl"
        content = (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        with self.lock:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "ab") as output:
                output.write(content)

    def observe(self, name: str, **kwargs) -> None:
        session_id = kwargs.get("session_id", "")
        key = (session_id, kwargs.get("api_request_id"))
        if name == "pre_api_request":
            body = (kwargs.get("request") or {}).get("body") or {}
            if not isinstance(body, dict):
                raise ValueError("Missing model settings in Hermes hook")
            settings = {k: copy.deepcopy(v) for k, v in body.items() if k in SETTINGS_FIELDS and v is not None}
            settings["model"] = kwargs["model"]
            if isinstance(body.get("reasoning"), dict) and "effort" in body["reasoning"]:
                settings["reasoning_effort"] = body["reasoning"]["effort"]
            request = kwargs.get("request_messages")
            if not isinstance(request, list) or not request:
                raise ValueError("Missing request messages in Hermes hook")
            self.pending[key] = {"settings": settings,
                                 "messages": chat_messages(request, kwargs.get("system_prompt"))}
        elif name == "post_api_request":
            event = self.pending.pop(key)
            assistant = kwargs["assistant_message"]
            if hasattr(assistant, "model_dump"):
                assistant = assistant.model_dump(exclude_none=True)
            elif not isinstance(assistant, dict):
                calls = []
                for call in (getattr(assistant, "tool_calls", None) or []):
                    if hasattr(call, "model_dump"):
                        calls.append(call.model_dump(exclude_none=True))
                    elif isinstance(call, dict):
                        calls.append(call)
                    else:
                        calls.append({"id": call.id, "function": {
                            "name": call.function.name, "arguments": call.function.arguments}})
                assistant = {"role": "assistant", "content": assistant.content,
                             "tool_calls": calls}
            answer = clean_message({**assistant, "role": "assistant"})
            answer["duration_ms"] = kwargs["api_duration"] * 1000
            if kwargs.get("usage") is not None:
                answer["usage"] = copy.deepcopy(kwargs["usage"])
            event["assistant"] = answer
            self.write(session_id, event)
        elif name == "api_request_error":
            self.pending.pop(key, None)


def install(directory: Path) -> Recorder:
    from hermes_cli import lifecycle

    recorder = Recorder(directory)
    invoke = lifecycle.invoke_hook
    has_hook = lifecycle.has_hook
    observed = {"pre_api_request", "post_api_request", "api_request_error"}

    def wrapped(name, **kwargs):
        if name in observed:
            try:
                recorder.observe(name, **kwargs)
            except Exception as exc:
                try:
                    recorder.write(kwargs.get("session_id", ""), {"error": type(exc).__name__})
                except Exception:
                    pass
        return invoke(name, **kwargs)

    lifecycle.invoke_hook = wrapped
    lifecycle.has_hook = lambda name: name in observed or has_hook(name)
    return recorder


def read_recording(path: Path) -> dict:
    return build_rollout([load_json(line) for line in path.read_text().splitlines() if line.strip()])
