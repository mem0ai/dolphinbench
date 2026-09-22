"""
OpenAI-direct provider driver for DolphinBench — the "no memory" baseline.

Hits the OpenAI Chat Completions API directly with no memory system, no
retrieval, no tools. Every call is a fresh context. This is intended as
a floor that any memory-equipped system should beat — if your memory
provider can't clear this baseline, something is wrong.

Env vars:
    OPENAI_API_KEY      — required
    OPENAI_MODEL        — optional, default "gpt-4o-mini"
    OPENAI_BASE_URL     — optional, for Azure/OpenRouter/etc. Default
                          "https://api.openai.com/v1"
    OPENAI_SYSTEM_PROMPT — optional, short system prompt. Default is a
                          generic "helpful assistant" instruction.

Why "no tools"? This driver is a lower bound. Real memory systems get
access to the DolphinBench mock MCP tools; this one doesn't. That's intentional
— a tool-equipped-but-memory-less baseline is easy to add by extending
this module. A memory-less-AND-tool-less baseline is the simplest thing
that still produces a response for every DolphinBench test query.

Wire-up: ``harness/run_simulation.py`` accepts ``--provider openai_direct``
and dispatches here. See ``docs/DRIVER_CONTRACT.md`` for the full contract.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field


@dataclass
class OpenAIDirectResult:
    """Return shape for the openai_direct driver. Mirrors HermesResult/MagentResult."""
    ok: bool
    session_id: str | None
    response_text: str
    tool_calls: list[dict] = field(default_factory=list)
    memory_recall: dict = field(default_factory=dict)
    memory_events_created: int = 0
    token_usage: dict = field(default_factory=dict)
    error: str | None = None
    model: str | None = None


DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
DEFAULT_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
DEFAULT_SYSTEM_PROMPT = os.environ.get(
    "OPENAI_SYSTEM_PROMPT",
    "You are a helpful assistant. Answer the user's question as best you "
    "can from the information provided. You have no access to prior "
    "conversations, no memory, and no external tools.",
)


def run_openai_direct(
    message: str,
    session_id: str | None = None,
    workspace_id: str | None = None,
    narrative_time: str | None = None,
    timeout: int = 120,
    model: str | None = None,
    **kwargs,
) -> OpenAIDirectResult:
    """Send a single-turn chat completion to OpenAI.

    Because this driver has no memory, ``session_id`` is informational only —
    we still return a fresh id so the harness can log it, but we do NOT
    carry any conversation history between calls. Each call is a new
    system+user round-trip with no context from prior turns.

    ``narrative_time`` is appended to the system prompt as a "date context"
    hint so the model knows the simulation's temporal frame.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return OpenAIDirectResult(
            ok=False,
            session_id=None,
            response_text="",
            error="Missing OPENAI_API_KEY env var",
        )

    effective_model = model or DEFAULT_MODEL
    system = DEFAULT_SYSTEM_PROMPT
    if narrative_time:
        system += f"\n\nCurrent simulated date/time: {narrative_time}"

    body = {
        "model": effective_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ],
        "temperature": 0.0,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{DEFAULT_BASE_URL}/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        return OpenAIDirectResult(
            ok=False,
            session_id=None,
            response_text="",
            error=f"HTTP {e.code}: {detail or e.reason}",
            model=effective_model,
        )
    except Exception as e:
        return OpenAIDirectResult(
            ok=False,
            session_id=None,
            response_text="",
            error=str(e),
            model=effective_model,
        )

    try:
        text = raw["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        return OpenAIDirectResult(
            ok=False,
            session_id=None,
            response_text="",
            error=f"malformed response: {e}",
            model=effective_model,
        )

    usage = raw.get("usage") or {}
    token_usage = {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }

    return OpenAIDirectResult(
        ok=bool(text),
        session_id=session_id or f"oai-{uuid.uuid4().hex[:8]}",
        response_text=text,
        tool_calls=[],
        memory_recall={},
        memory_events_created=0,
        token_usage=token_usage,
        error=None,
        model=effective_model,
    )


if __name__ == "__main__":
    # Quick sanity check — requires OPENAI_API_KEY to be set.
    r = run_openai_direct("Say 'openai-direct-ok' and nothing else.")
    print(f"ok={r.ok}")
    print(f"session_id={r.session_id}")
    print(f"response={r.response_text[:200]}")
    print(f"tokens={r.token_usage}")
    print(f"model={r.model}")
    if r.error:
        print(f"error={r.error}")
