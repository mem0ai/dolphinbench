"""
Dummy provider driver for DolphinBench — demonstrates the driver contract.

Returns fixed canned responses for every query. No LLM calls, no memory
system, no tools. Purpose: let you exercise the full harness end-to-end
without any API keys or network, to confirm your setup works before you
plug in a real provider.

All DolphinBench drivers expose a single entry point with the following shape:

    def run_<provider>(message, session_id=None, workspace_id=None,
                       narrative_time=None, timeout=300, **kwargs) -> Result

where Result is a dataclass with fields documented in
``docs/DRIVER_CONTRACT.md`` (ok, session_id, response_text, tool_calls,
memory_recall, memory_events_created, token_usage, error).

This module deliberately has zero third-party dependencies.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass
class DummyResult:
    """Return shape for the dummy driver. Mirrors HermesResult/MagentResult."""
    ok: bool
    session_id: str | None
    response_text: str
    tool_calls: list[dict] = field(default_factory=list)
    memory_recall: dict = field(default_factory=dict)
    memory_events_created: int = 0
    token_usage: dict = field(default_factory=dict)
    error: str | None = None
    model: str | None = "dummy-v0"


# A tiny canned-response table keyed on substrings in the user's message.
# Deliberately silly — the point is to exercise the grading path, not to
# actually pass any tests. Every query falls through to the default reply.
_CANNED_REPLIES: list[tuple[str, str]] = [
    ("reply with exactly", "driver-test-ok"),  # magent-style smoke test
    ("hello", "Hello! I'm the DolphinBench dummy driver. I can't remember anything."),
    ("book", "I would book a meeting, but I'm only a dummy driver."),
    ("send an email", "I would send an email, but I'm only a dummy driver."),
]

_DEFAULT_REPLY = (
    "[dummy-driver] I received your message but I have no memory and no "
    "tools. For real results, wire up a memory-aware provider — see "
    "docs/DRIVER_CONTRACT.md."
)


def run_dummy(
    message: str,
    session_id: str | None = None,
    workspace_id: str | None = None,
    narrative_time: str | None = None,
    timeout: int = 300,
    **kwargs,
) -> DummyResult:
    """Fixed-response provider. Always returns ok=True.

    Args match the DolphinBench driver contract. Extra kwargs are ignored so the
    harness can pass provider-specific params without breaking us.
    """
    msg_lc = (message or "").lower()
    reply = _DEFAULT_REPLY
    for needle, canned in _CANNED_REPLIES:
        if needle in msg_lc:
            reply = canned
            break

    # Fabricate plausible token usage so cost instrumentation has something
    # to chew on. These values are obviously fake; the model id 'dummy-v0'
    # is not in the pricing snapshot, so run_simulation will record
    # cost_unavailable_reason="model_id_not_in_pricing_snapshot:dummy-v0".
    fake_usage = {
        "input_tokens": max(1, len(message) // 4),
        "output_tokens": max(1, len(reply) // 4),
    }

    return DummyResult(
        ok=True,
        session_id=session_id or f"dummy-{uuid.uuid4().hex[:8]}",
        response_text=reply,
        tool_calls=[],
        memory_recall={},
        memory_events_created=0,
        token_usage=fake_usage,
        error=None,
    )


if __name__ == "__main__":
    # Quick sanity check — run the driver directly without the full harness.
    r = run_dummy("Book a 30-minute call with Kenji for tomorrow.")
    print(f"ok={r.ok}")
    print(f"session_id={r.session_id}")
    print(f"response={r.response_text}")
    print(f"tokens={r.token_usage}")
    print(f"model={r.model}")
