#!/usr/bin/env python3
"""DolphinBench oracle reference agent.

Stateless personal-assistant agent: takes a test YAML + persona id and
produces (response_text, tool_trace) by running a multi-turn tool-using
LLM loop against the persona's mock_mcp tool surface.

The oracle has no memory backend. With memory enabled, it resolves the test's
numeric fact references through ``facts.yaml`` and places only the complete
linked user-message sessions in the system prompt. Tool execution happens in-process by importing
mock_mcp/server.py with DOLPHINBENCH_PERSONA set, then calling the FastMCP-
registered tool functions directly.

Public entry point:
    run_oracle(test_yaml: dict, persona_id: str, model: str = "gpt-5.4",
               max_turns: int = 10, checkpoint: Path | None = None) -> dict

Returns:
    {
      "response_text": str,
      "tool_calls": [{"tool": str, "args": dict, "result": Any}, ...],
      "tool_trace": [verbatim calls.jsonl entries for this test],
      "turn_count": int,
      "turn_ceiling_hit": bool,
      "usage": {"prompt_tokens": int, "completion_tokens": int},
      "latency_seconds": float,
      "errors": [str, ...],
      "messages": [final message list],
    }

Models:
- Default "gpt-5.4" routes through an OpenAI-compatible Azure endpoint.
  Set AZURE_OPENAI_API_KEY and AZURE_OPENAI_BASE_URL, or point
  DOLPHINBENCH_OPENAI_CONFIG at a YAML file with
  llm.providers.openai.{api_key,base_url}.
- "anthropic/claude-opus-4-7" (or any "anthropic/...", "openrouter/...")
  routes through OpenRouter using OPENROUTER_API_KEY from ~/.hermes/.env.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
from harness.environment import get_setting, openai_config_path
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import yaml
from openai import OpenAI

from registry.facts import CHECKPOINT_ENV, load_sessions, source_session_ids_for_test


DOLPHINBENCH_ROOT = Path(__file__).resolve().parent.parent
MOCK_MCP_DIR = DOLPHINBENCH_ROOT / "mock_mcp"
# Honor DOLPHINBENCH_LOG_PATH / DOLPHINBENCH_STATE_PATH so parallel Oracle processes (e.g.
# concurrent per-persona scans) can isolate their mock files. Same env
# contract the mock server (mock_mcp/server.py) already reads, keeping the
# in-process server and oracle.py's reset/trace-read pointed at one path.
MOCK_LOG_PATH = Path(get_setting("DOLPHINBENCH_LOG_PATH", str(MOCK_MCP_DIR / "calls.jsonl")))
MOCK_STATE_PATH = Path(get_setting("DOLPHINBENCH_STATE_PATH", str(MOCK_MCP_DIR / "state.json")))


NORMAL_ASSISTANT_PROMPT = """You are the user's personal assistant. Today is {evaluation_date}.

Complete the current request using the available tools and information. Use
relevant facts, preferences, and instructions from earlier messages when they
are provided. Interpret those messages using their original dates and later
updates. An explicit newer instruction takes precedence over an older one.

Earlier messages provide context; they are not separate requests to execute now.
Do not invent personal facts or claim an action happened without evidence. If
information necessary to complete the current work is unavailable, ask for it.
Use the tools for requested actions and give a brief, accurate completion reply."""


# ---------------------------------------------------------------------------
# mock_mcp server import — lazy, persona-scoped
# ---------------------------------------------------------------------------

_SERVER_MOD = None
_SERVER_PERSONA = None


def _import_mock_server(persona_id: str):
    """Import mock_mcp/server.py with DOLPHINBENCH_PERSONA set.

    The module is cached per-process; switching personas in the same
    process is not supported (the manifest is read at import time).
    """
    global _SERVER_MOD, _SERVER_PERSONA
    if _SERVER_MOD is not None:
        if _SERVER_PERSONA != persona_id:
            raise RuntimeError(
                f"mock_mcp server already imported for persona "
                f"{_SERVER_PERSONA!r}; cannot switch to {persona_id!r} "
                f"in the same process."
            )
        return _SERVER_MOD
    os.environ["DOLPHINBENCH_PERSONA"] = persona_id
    spec = importlib.util.spec_from_file_location(
        "_dolphinbench_oracle_mock_server", str(MOCK_MCP_DIR / "server.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _SERVER_MOD = mod
    _SERVER_PERSONA = persona_id
    return mod


def _registered_tools(server_mod) -> dict:
    """Return {tool_name: tool_info_obj} from FastMCP's tool manager."""
    return dict(server_mod.mcp._tool_manager._tools)


def _openai_tool_specs(server_mod) -> list[dict]:
    """Render the persona-scoped tools as OpenAI tools=[...] specs."""
    out = []
    for name, info in _registered_tools(server_mod).items():
        params = info.parameters or {"type": "object", "properties": {}}
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": (info.description or "").strip(),
                "parameters": params,
            },
        })
    return out


def _call_tool(server_mod, name: str, args: dict) -> Any:
    """Invoke a tool function in-process. Returns the raw return value
    (typically a JSON string from the tool itself).
    """
    info = _registered_tools(server_mod).get(name)
    if info is None:
        return json.dumps({"error": "UNKNOWN_TOOL",
                           "instruction": f"Tool {name!r} not available."})
    fn = info.fn
    try:
        return fn(**(args or {}))
    except TypeError as e:
        return json.dumps({"error": "BAD_ARGS",
                           "instruction": f"Argument shape rejected: {e}"})
    except Exception as e:
        return json.dumps({"error": "TOOL_RAISED",
                           "instruction": f"Tool raised: {e}"})


# ---------------------------------------------------------------------------
# LLM client setup
# ---------------------------------------------------------------------------

_CLIENT_CACHE: dict[str, OpenAI] = {}


def _load_azure_creds():
    api_key = os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    base_url = (
        os.environ.get("AZURE_OPENAI_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
    )
    if not base_url and os.environ.get("AZURE_OPENAI_ENDPOINT"):
        base_url = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/") + "/openai/v1/"
    if api_key and base_url:
        return api_key, base_url

    cfg_path = openai_config_path()
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)
        p = cfg["llm"]["providers"]["openai"]
        return p["api_key"], p["base_url"]

    raise RuntimeError(
        "Azure OpenAI credentials not found. Set AZURE_OPENAI_API_KEY and "
        "AZURE_OPENAI_BASE_URL, or set DOLPHINBENCH_OPENAI_CONFIG to a YAML file "
        "with llm.providers.openai.{api_key,base_url}."
    )


def _load_openrouter_key():
    # Prefer env, then ~/.hermes/.env
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        return key
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("OPENROUTER_API_KEY not found in env or ~/.hermes/.env")


def _resolve_client_and_model(model: str) -> tuple[OpenAI, str]:
    """Return (client, deployment_or_model_name) for the given model id.

    - 'gpt-5.4' / 'gpt-5.4-mini' / unqualified ids → Azure (default).
    - 'anthropic/...', 'openai/...', 'google/...', 'openrouter/...' → OpenRouter.
    """
    if "/" in model:
        # OpenRouter (any provider/model slug)
        key = "openrouter"
        if key not in _CLIENT_CACHE:
            _CLIENT_CACHE[key] = OpenAI(
                api_key=_load_openrouter_key(),
                base_url="https://openrouter.ai/api/v1",
                timeout=120.0, max_retries=5,
            )
        # Strip "openrouter/" prefix if present; pass slug straight through.
        slug = model[len("openrouter/"):] if model.startswith("openrouter/") else model
        return _CLIENT_CACHE[key], slug
    # Azure default
    key = "azure"
    if key not in _CLIENT_CACHE:
        api_key, base_url = _load_azure_creds()
        # timeout+retries so a stalled Azure call fails fast and the backoff
        # wrapper retries it, instead of hanging the whole run indefinitely.
        _CLIENT_CACHE[key] = OpenAI(api_key=api_key, base_url=base_url,
                                    timeout=120.0, max_retries=5)
    return _CLIENT_CACHE[key], model


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def _load_sim(persona_id: str, checkpoint: str | Path | None = None) -> dict:
    return {"sessions": load_sessions(persona_id, root=DOLPHINBENCH_ROOT, checkpoint=checkpoint)}


def _effective_checkpoint(checkpoint: str | Path | None) -> str | Path | None:
    """Use the subprocess bridge only when the caller did not pass a path."""
    if checkpoint is not None:
        return checkpoint
    value = os.environ.get(CHECKPOINT_ENV)
    return value if value and value.strip() else None


def _persona_display_name(persona_id: str) -> str:
    # Cheap lookup: registry/personas.yaml has display name.
    try:
        with open(DOLPHINBENCH_ROOT / "registry" / "personas.yaml") as f:
            top = yaml.safe_load(f) or {}
        for p in (top.get("personas") or []):
            if p.get("id") == persona_id:
                return p.get("display_name") or p.get("name") or persona_id.capitalize()
    except Exception:
        pass
    return persona_id.capitalize()


def _time_of_day_label(iso_dt: str) -> str:
    """Map an ISO datetime to a coarse time-of-day label."""
    try:
        # Take HH from the time portion.
        t = iso_dt.split("T", 1)[1]
        hh = int(t[:2])
    except Exception:
        return "midday"
    if hh < 6:
        return "early morning"
    if hh < 11:
        return "morning"
    if hh < 14:
        return "midday"
    if hh < 18:
        return "afternoon"
    if hh < 21:
        return "evening"
    return "night"


def _resolve_source_session(
    persona_id: str, session_id: str, checkpoint: str | Path | None = None
) -> dict | None:
    sim = _load_sim(persona_id, checkpoint=checkpoint)
    for s in (sim.get("sessions") or []):
        if str(s.get("id")) == str(session_id):
            return s
    return None


def _format_source_session(
    persona_id: str, session_id: str, checkpoint: str | Path | None = None
) -> str:
    s = _resolve_source_session(persona_id, session_id, checkpoint=checkpoint)
    return _format_session(session_id, s)


def _format_session(session_id: str, session: dict | None) -> str:
    """Render one already-resolved source session for the memory prompt."""
    s = session
    if not s:
        return f"[{session_id} — (source session not found in life_sim.yaml)]"
    nd = s.get("narrative_date") or ""
    date_part = nd.split("T", 1)[0] if "T" in nd else nd
    tod = _time_of_day_label(nd) if nd else "midday"
    msgs = s.get("messages") or []
    body_parts = []
    for m in msgs:
        if isinstance(m, str):
            body_parts.append(m.strip())
        elif isinstance(m, dict):
            t = m.get("content") or m.get("text") or ""
            if t:
                body_parts.append(t.strip())
    body = "\n\n".join(body_parts)
    return f"[{date_part} — {tod}]\n{body}"


def build_system_prompt(test: dict, persona_id: str,
                        with_memory: bool = True,
                        style_hint: str = "",
                        checkpoint: str | Path | None = None) -> str:
    """Build the oracle's system prompt.

    When ``with_memory`` is False, the source-session message blocks are omitted —
    the oracle's only "memory" channel. Everything else (identity, tool
    access, action-grounding rules) is unchanged. This is the G2
    counterfactual: if the oracle still solves the test without these
    blocks, the answer leaked via the query or via a state read
    (mock_state, reachable through tools), not memory.

    With memory, the channel is the CONVERSATIONAL source-session text ONLY. The
    distilled load-bearing fact is deliberately NOT injected: it is an
    answer key no real agent ever sees, and its canonical wording biased
    the oracle into parroting it — letting phrasing-brittle rubrics slip
    through G1. Source-session-only G1 certifies "an agent with perfect memory of
    the conversation can solve this" (the honest ceiling), and a rubric
    that rejects natural phrasings of correct behavior now fails the gate.

    ``style_hint`` (used on later G1 shots) adds a phrasing-diversity
    instruction so the shots sample different natural wordings.
    """
    prepared = test.get("_oracle_input")
    if prepared is not None:
        if prepared.get("version") != 4 or style_hint:
            raise ValueError("unsupported certification prompt or condition-specific hint")
        expected = NORMAL_ASSISTANT_PROMPT.format(evaluation_date=test["narrative_anchor_date"])
        if prepared.get("system") != expected:
            raise ValueError("certification prompt does not match the ordinary assistant instructions")
        lines = [expected]
        if with_memory:
            lines.append("Earlier user messages:")
            for source in prepared["source_messages"]:
                lines.append(f"[{source['date']}] {source['text']}")
        return "\n\n".join(lines)
    checkpoint = _effective_checkpoint(checkpoint)
    display = _persona_display_name(persona_id)
    nd = test.get("narrative_anchor_date") or test.get("narrative_date") or "2026-05-22"
    # Some tests use "narrative_anchor" as a relative tag (e.g. "phase4+28d")
    # rather than an ISO date; in that case fall back to a fixed default
    # consistent with the prior audit's anchor.
    if not str(nd).startswith("20"):
        nd = "2026-05-22"

    lines = [f"You are {display}'s personal assistant. Today is {nd}."]
    lines.append("")
    if with_memory:
        source_session_ids = source_session_ids_for_test(
            test, persona_id, root=DOLPHINBENCH_ROOT, checkpoint=checkpoint
        )
        sessions_by_id = {
            str(session.get("id")): session
            for session in load_sessions(persona_id, root=DOLPHINBENCH_ROOT, checkpoint=checkpoint)
        }

        # Present memory CHRONOLOGICALLY (oldest -> newest) so "current" status is
        # unambiguous: a later-dated block plainly supersedes an earlier one. In
        # authoring order the agent had to re-sort dates itself and could grab a
        # stale value (e.g. 75% from Jun 19 over 90% from Jun 26).
        source_session_ids.sort(
            key=lambda sid: (sessions_by_id.get(str(sid)) or {}).get("narrative_date") or ""
        )

        if source_session_ids:
            lines.append("Recent relevant context from their messages to you:")
            for sid in source_session_ids:
                lines.append("")
                lines.append(_format_session(sid, sessions_by_id.get(str(sid))))
            lines.append("")

    lines.append(
        "You have access to tools (calendar, email, slack, etc.) — "
        "use them when appropriate."
    )
    lines.append("")
    lines.append(
        "Use only what's in the context above. Don't invent details about "
        "them or their work. Proactively APPLY their standing preferences, "
        "habits, defaults, and rules from that context even when this "
        "request doesn't restate them — their usual choices, formats, "
        "wording length, people to involve, and limits."
    )
    lines.append("")
    lines.append(
        "When the context shows a value that changed over time (dated "
        "updates to the same thing — a status, percentage, count, decision), "
        "the MOST RECENT dated value is the current one. Use it; an earlier "
        "dated value is superseded."
    )
    lines.append("")
    if style_hint:
        lines.append(style_hint)
        lines.append("")
    # Final-priority action-grounding rules. These come last on purpose:
    # they override any earlier permissive reading of the user's request.
    lines.append("RULES — follow exactly:")
    lines.append("")
    lines.append(
        "1. EXECUTE, DON'T NARRATE. If the request asks you to do something "
        "(send, update, deploy, schedule, create, process, reschedule, "
        "post, mark, set, ...), you MUST call the tool that actually "
        "changes state. Describing what you'd do, or claiming it's done, "
        "without that tool call is a failure."
    )
    lines.append("")
    lines.append(
        "2. READING IS NOT ACTING. Tools like list_*, get_*, search_* only "
        "gather information — they NEVER complete the task. After a read "
        "returns, CONTINUE and call the mutation tool (update_*, send_*, "
        "create_*, deploy_*, process_*, ...). Never stop or write a final "
        "summary until the requested mutation has been performed."
    )
    lines.append("")
    lines.append(
        "3. FILL REAL CONTENT. Populate every argument with the actual "
        "specific value recalled from what you know above — the real name, "
        "number, key, body text. Never an empty, generic, or placeholder "
        "argument (no body=None, no items=['the usual'])."
    )
    lines.append("")
    lines.append(
        "4. FIX AND RETRY. If a tool returns an error (NO_FIELDS, "
        "UNSPECIFIED_ITEMS, NOT_FOUND, ...), read its instruction, correct "
        "the arguments, and call it again. Do not give up and reply in prose."
    )
    lines.append("")
    lines.append(
        "5. CLOSEST TOOL. If the exact tool isn't listed, use the closest "
        "one that performs the action (e.g. create_calendar_event when "
        "update_calendar_event is absent). Reply in prose ONLY if no tool "
        "can perform any part of the action."
    )
    lines.append("")
    lines.append(
        "DRAFT EXCEPTION: produce text instead of calling a tool ONLY when "
        "the request explicitly says draft / preview / \"run it by me\" / "
        "\"what would you say\" / \"show me first\". Otherwise, execute, "
        "then optionally add a one-line confirmation after the tool call."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def _maybe_parse_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {}
        try:
            return json.loads(s)
        except Exception:
            return {"_raw_args": s}
    return {}


def _create_with_retry(client, kwargs, max_retries: int = 8):
    """client.chat.completions.create with exponential backoff on transient
    errors (429 rate limits, 5xx, timeouts). Necessary for parallel Oracle
    runs against Azure, which otherwise 429 and return empty traces — making
    a leaked test falsely look clean."""
    clean = {k: v for k, v in kwargs.items() if v is not None}
    delay = 2.0
    last_exc = None
    for attempt in range(max_retries):
        try:
            from harness.paid_budget import active_budget, budgeted_completion
            budget = active_budget()
            from harness.provider_capacity import provider_call
            with provider_call(model=clean["model"], request=clean, kind="oracle") as transport:
                response = (budgeted_completion(client, clean, budget) if budget is not None
                            else client.chat.completions.create(**clean))
                if hasattr(response.usage, "model_dump"):
                    transport["usage"] = response.usage.model_dump()
                return response
        except Exception as e:
            last_exc = e
            msg = str(e).lower()
            transient = any(x in msg for x in (
                "429", "rate", "timeout", "timed out", "502", "503", "504",
                "overloaded", "temporarily", "connection",
            ))
            if attempt == max_retries - 1 or not transient:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
    raise last_exc  # pragma: no cover


def run_oracle(
    test_yaml: dict,
    persona_id: str,
    model: str = "gpt-5.4",
    max_turns: int = 10,
    reset_mock_state: bool = True,
    with_memory: bool = True,
    style_hint: str = "",
    checkpoint: str | Path | None = None,
) -> dict:
    """Run the oracle agent against a single test.

    Pre-conditions:
    - mock_mcp/state.json should already be reset to the persona baseline
      by the caller (audit_realism.py does this under a lock). If
      reset_mock_state is True, we will not touch state — we leave that
      to the caller. The flag is reserved for the CLI ad-hoc mode.
    - mock_mcp/calls.jsonl should be truncated by the caller before this
      call so the tool_trace returned is scoped to the test.
    """
    t0 = time.time()
    errors: list[str] = []

    server_mod = _import_mock_server(persona_id)
    tools = _openai_tool_specs(server_mod)
    client, deployment = _resolve_client_and_model(model)
    execution_settings = (test_yaml.get("_oracle_input") or {}).get("execution_settings")
    if execution_settings is not None:
        if (set(execution_settings) not in (
                {"model", "reasoning_effort", "timeout", "max_retries"},
                {"model", "reasoning_effort", "timeout", "max_retries", "api"})
                or execution_settings.get("api", "chat_completions") not in {"chat_completions", "responses"}
                or execution_settings["model"] != model
                or execution_settings["reasoning_effort"] not in {"medium", "high"}
                or type(execution_settings["timeout"]) is not int
                or not 30 <= execution_settings["timeout"] <= 300
                or execution_settings["max_retries"] != 0):
            raise ValueError("invalid staged certification settings")
        client = client.with_options(timeout=execution_settings["timeout"], max_retries=0)

    system_prompt = build_system_prompt(test_yaml, persona_id,
                                        with_memory=with_memory,
                                        style_hint=style_hint,
                                        checkpoint=checkpoint)
    user_message = test_yaml.get("test") or ""
    if test_yaml.get("_oracle_input") is None:
        user_message = user_message.strip()

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    use_responses = execution_settings is not None and execution_settings.get("api") == "responses"
    response_inputs = copy.deepcopy(messages)
    provider_continuation = []
    if use_responses:
        from harness.oracle_responses import complete_turn, function_tools
        response_tools = function_tools(tools)
    initial_state = json.loads(MOCK_STATE_PATH.read_text()) if MOCK_STATE_PATH.is_file() else None
    agent_input = {
        "messages": copy.deepcopy(messages), "tools": copy.deepcopy(response_tools if use_responses else tools),
        **({"provider_api": "responses"} if use_responses else {}),
        "starting_state_sha256": hashlib.sha256(
            json.dumps(initial_state, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
    }

    tool_calls_log: list[dict] = []
    response_text_parts: list[str] = []
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}
    turn_count = 0
    turn_ceiling_hit = False
    final_text = ""

    for turn in range(max_turns):
        turn_count = turn + 1
        try:
            kwargs = dict(
                model=deployment,
                messages=messages,
                tools=tools if tools else None,
                tool_choice="auto" if tools else None,
            )
            # gpt-5.x reasoning deployments reject non-default temperature.
            if not str(deployment).lower().startswith("gpt-5"):
                kwargs["temperature"] = 0.2
            if use_responses:
                resp, output_items = complete_turn(client=client, model=deployment, inputs=response_inputs,
                                                  tools=response_tools, reasoning_effort=execution_settings["reasoning_effort"])
                response_inputs.extend(output_items)
                provider_continuation.append({"turn": turn_count, "response_items": copy.deepcopy(output_items)})
            elif execution_settings is not None:
                kwargs["reasoning_effort"] = execution_settings["reasoning_effort"]
                resp = _create_with_retry(client, kwargs, max_retries=1)
            else:
                resp = _create_with_retry(client, kwargs)
        except Exception as e:
            errors.append(f"turn {turn_count}: LLM call failed: {e}")
            break

        try:
            u = resp.usage
            usage_totals["prompt_tokens"] += getattr(u, "prompt_tokens", 0) or 0
            usage_totals["completion_tokens"] += getattr(u, "completion_tokens", 0) or 0
        except Exception:
            pass

        choice = resp.choices[0]
        msg = choice.message
        content = msg.content or ""
        tcalls = getattr(msg, "tool_calls", None) or []

        # Echo the assistant message into the conversation, faithfully.
        assistant_entry: dict = {"role": "assistant", "content": content}
        if tcalls:
            assistant_entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in tcalls
            ]
        messages.append(assistant_entry)

        if content:
            response_text_parts.append(content)

        if not tcalls:
            final_text = content
            break

        # Execute tools and append results.
        for tc in tcalls:
            tname = tc.function.name
            targs = _maybe_parse_args(tc.function.arguments)
            try:
                result = _call_tool(server_mod, tname, targs)
            except Exception as e:
                result = json.dumps({"error": "ORACLE_EXEC_ERROR",
                                     "instruction": str(e)})
                errors.append(f"turn {turn_count} {tname}: {e}")
            tool_calls_log.append({"tool": tname, "args": targs, "result": result})
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result if isinstance(result, str) else json.dumps(result, default=str),
            })
            if use_responses:
                output_item = {"type": "function_call_output", "call_id": tc.id,
                               "output": messages[-1]["content"]}
                response_inputs.append(output_item)
                provider_continuation.append({"turn": turn_count, "tool_output": output_item})
        # continue loop
    else:
        # Loop did not break — hit ceiling.
        turn_ceiling_hit = True
        if response_text_parts:
            final_text = response_text_parts[-1]

    # Pull the verbatim trace from calls.jsonl (the source of truth used
    # by graders/mechanical.py).
    trace: list[dict] = []
    try:
        if MOCK_LOG_PATH.exists():
            for line in MOCK_LOG_PATH.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    trace.append(json.loads(line))
                except Exception:
                    pass
    except Exception as e:
        errors.append(f"reading mock log: {e}")

    full_response_text = "\n".join(t for t in response_text_parts if t).strip()

    return {
        "response_text": full_response_text or final_text,
        "tool_calls": [{"tool": c["tool"], "args": c["args"]} for c in tool_calls_log],
        "tool_trace": trace,
        "turn_count": turn_count,
        "turn_ceiling_hit": turn_ceiling_hit,
        "usage": usage_totals,
        "latency_seconds": round(time.time() - t0, 2),
        "errors": errors,
        "model": model,
        "agent_input": agent_input,
        "messages": messages,
        **({"provider_continuation": provider_continuation} if use_responses else {}),
        "tool_results": [{"call_index": index, **call} for index, call in enumerate(tool_calls_log)],
    }


# ---------------------------------------------------------------------------
# CLI for ad-hoc testing
# ---------------------------------------------------------------------------

def _reset_mock_state(persona_id: str) -> None:
    """Copy the persona baseline over state.json and truncate calls.jsonl."""
    import shutil
    baseline = MOCK_MCP_DIR / "state" / f"{persona_id}_baseline.json"
    if baseline.exists():
        shutil.copy(baseline, MOCK_STATE_PATH)
    open(MOCK_LOG_PATH, "w").close()


def _cli():
    ap = argparse.ArgumentParser(description="Run the DolphinBench oracle on a single test.")
    ap.add_argument("--test", required=True, help="path to a test YAML")
    ap.add_argument("--persona", required=True, help="persona id (e.g. alex)")
    ap.add_argument("--model", default="gpt-5.4", help="model id (default gpt-5.4)")
    ap.add_argument("--max-turns", type=int, default=10)
    ap.add_argument(
        "--checkpoint",
        type=Path,
        default=None,
        help="authenticated checkpoint to use for facts and source sessions",
    )
    ap.add_argument("--out", default=None, help="optional output JSON path")
    args = ap.parse_args()

    from harness.dataset import load_test
    test = load_test(Path(args.test))
    _reset_mock_state(args.persona)
    result = run_oracle(
        test,
        args.persona,
        model=args.model,
        max_turns=args.max_turns,
        checkpoint=args.checkpoint,
    )
    print(f"# turns:         {result['turn_count']}  ceiling_hit={result['turn_ceiling_hit']}")
    print(f"# tool calls:    {len(result['tool_calls'])}")
    print(f"# trace entries: {len(result['tool_trace'])}")
    print(f"# latency:       {result['latency_seconds']}s")
    print(f"# usage:         {result['usage']}")
    if result["errors"]:
        print(f"# errors:        {result['errors']}")
    print("--- response_text ---")
    print(result["response_text"])
    print("--- tool calls ---")
    for c in result["tool_calls"]:
        a = json.dumps(c["args"], default=str)
        if len(a) > 200:
            a = a[:197] + "..."
        print(f"- {c['tool']}({a})")
    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"# wrote {args.out}")


if __name__ == "__main__":
    _cli()
