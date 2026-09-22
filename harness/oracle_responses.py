"""Responses transport for reasoning-enabled reference-agent tool turns."""

from __future__ import annotations

import copy
import os
from harness.environment import get_setting
from typing import Any

from openai.types.chat import ChatCompletion


def function_tools(chat_tools: list[dict]) -> list[dict]:
    return [{"type": "function", **copy.deepcopy(tool["function"]),
             "strict": tool["function"].get("strict", False)} for tool in chat_tools]


def complete_turn(*, client: Any, model: str, inputs: list[dict], tools: list[dict],
                  reasoning_effort: str) -> tuple[ChatCompletion, list[dict]]:
    from harness.paid_budget import active_budget
    from harness.provider_capacity import provider_call, safe_rate_headers

    request = {"model": model, "input": inputs, "tools": tools,
               "tool_choice": "auto", "reasoning": {"effort": reasoning_effort},
               "include": ["reasoning.encrypted_content"], "store": False,
               "max_output_tokens": 16384}
    budget = active_budget()
    call_id = budget.reserve(model, request) if budget is not None else None
    with provider_call(model=model, request=request, kind="oracle_responses") as transport:
        if get_setting("DOLPHINBENCH_AUTHORING_PROVIDER_CAPACITY"):
            raw = client.with_raw_response.responses.create(**request)
            transport["rate_limit_headers"] = safe_rate_headers(raw.headers)
            response = raw.parse()
        else:
            response = client.responses.create(**request)
        usage = response.usage.model_dump() if response.usage is not None else {}
        transport["usage"] = usage
    if budget is not None:
        budget.settle(call_id, {"prompt_tokens": usage.get("input_tokens"),
                               "completion_tokens": usage.get("output_tokens")})
    if response.status != "completed":
        raise RuntimeError(f"reference Responses call did not complete: {response.status}; {response.incomplete_details}")
    output = [item.model_dump(mode="json", exclude_none=True) for item in response.output]
    text = []
    calls = []
    for item in output:
        if item["type"] == "message":
            for content in item.get("content", []):
                if content["type"] == "output_text":
                    text.append(content["text"])
                elif content["type"] == "refusal":
                    text.append(content["refusal"])
        elif item["type"] == "function_call":
            calls.append({"id": item["call_id"], "type": "function",
                          "function": {"name": item["name"], "arguments": item["arguments"]}})
    if not text and not calls:
        raise RuntimeError("reference Responses call returned no assistant text or function calls")
    normalized = ChatCompletion.model_validate({
        "id": response.id, "created": int(response.created_at), "object": "chat.completion", "model": model,
        "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
                     "message": {"role": "assistant", "content": "\n".join(text), "tool_calls": calls or None}}],
        "usage": {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0),
                  "total_tokens": usage.get("total_tokens", 0)},
    })
    return normalized, output
