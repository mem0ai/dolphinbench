"""Record judge conversations and replay them locally without a transport."""

from __future__ import annotations

import copy
from collections import defaultdict, deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable


_recording: ContextVar[JudgeConversation | None] = ContextVar("judge_recording", default=None)
_replay: ContextVar[Callable | None] = ContextVar("judge_replay", default=None)


@dataclass
class JudgeConversation:
    settings: dict[str, Any] | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)

    def add(self, settings: dict, messages: list[dict], response: dict,
            duration_ms: float) -> None:
        if self.settings is not None and self.settings != settings:
            raise ValueError("Judge settings changed within a grading check")
        self.settings = copy.deepcopy(settings)
        answer = copy.deepcopy(response["choices"][0]["message"])
        answer["role"] = "assistant"
        answer["duration_ms"] = duration_ms
        if response.get("usage") is not None:
            answer["usage"] = copy.deepcopy(response["usage"])
        self.messages.extend(copy.deepcopy(messages))
        self.messages.append(answer)


@contextmanager
def record_judge(conversation: JudgeConversation | None = None):
    conversation = conversation if conversation is not None else JudgeConversation()
    token = _recording.set(conversation)
    try:
        yield conversation
    finally:
        _recording.reset(token)


def record_response(settings: dict, messages: list[dict], response: dict,
                    duration_ms: float) -> None:
    conversation = _recording.get()
    if conversation is not None:
        conversation.add(settings, messages, response, duration_ms)


def replay_response(system_prompt: str, user_prompt: str, config: dict) -> dict | None:
    handler = _replay.get()
    if handler is None:
        return None
    return handler(system_prompt, user_prompt, config)


@contextmanager
def replay_judge(messages: list[dict], settings: dict):
    """Match actual grader prompts; never route a missing response to a provider."""
    responses = defaultdict(deque)
    position = 0
    while position < len(messages):
        if messages[position].get("role") == "system":
            system = messages[position].get("content")
            position += 1
        else:
            system = settings.get("system_prompt")
        if (not isinstance(system, str) or position + 1 >= len(messages)
                or messages[position].get("role") != "user"
                or messages[position + 1].get("role") != "assistant"
                or not isinstance(messages[position].get("content"), str)):
            raise ValueError("Judge messages must contain a prompt and its response")
        responses[system, messages[position]["content"]].append(messages[position + 1])
        position += 2

    def handler(system_prompt, user_prompt, config):
        from graders.llm_judge import _extract_json

        if config.get("backend", "azure") != "azure":
            raise ValueError("Submission grading requires the benchmark's Azure judge")
        if (settings.get("model") != "gpt-5.6-sol"
                or settings.get("reasoning_effort") != "medium"):
            raise ValueError("Submission grading requires gpt-5.6-sol with medium reasoning")
        matches = responses[system_prompt, user_prompt]
        if not matches:
            raise ValueError("Missing judge conversation matching this action and grading check")
        answer = matches.popleft()
        if answer.get("role") != "assistant" or not isinstance(answer.get("content"), str):
            raise ValueError("Missing judge response")
        parsed = _extract_json(answer["content"])
        if type(parsed.get("passed")) is not bool and not isinstance(parsed.get("results"), list):
            raise ValueError("Judge response must contain a Boolean passed verdict")
        return parsed

    token = _replay.set(handler)
    try:
        yield
        if any(responses.values()):
            raise ValueError("Unused judge messages do not belong to this grading check")
    finally:
        _replay.reset(token)
