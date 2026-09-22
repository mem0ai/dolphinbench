"""
Thin wrapper around `hermes chat -q "..." -Q` for programmatic benchmarking.

Hermes's -Q mode writes the final response to TTY via rich, but when stdout
is piped (subprocess.run), that output is empty. Current Hermes stores sessions
in state.db; older versions wrote JSON files under sessions/. The driver reads
either format and matches the exact user message submitted by this process.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from urllib.error import HTTPError, URLError
from pathlib import Path
from typing import Any


import yaml

from harness.environment import get_setting

DOLPHINBENCH_ROOT = Path(__file__).resolve().parents[1]

HERMES_BIN = os.environ.get("HERMES_BIN", "hermes")
_TOOL_SURFACE_BY_PROFILE: dict[str, list[str]] = {}

# Token estimation via tiktoken. Hermes' session JSON does NOT persist
# API-reported usage blocks, so we estimate post-hoc from message text.
# Estimates are typically within 1-3% of API counts for normal text.
try:
    import tiktoken
    _TIKTOKEN_AVAILABLE = True
except ImportError:
    _TIKTOKEN_AVAILABLE = False


@dataclass
class HermesResult:
    ok: bool
    session_id: str | None
    response_text: str
    stdout: str
    stderr: str
    session_path: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    error: str | None = None
    token_usage: dict = field(default_factory=dict)
    model: str | None = None
    available_tools: list[str] = field(default_factory=list)
    status_code: int | None = None
    retry_after_seconds: float | None = None


def _transport_metadata(text: str) -> tuple[int | None, float | None]:
    """Extract transport metadata emitted by the Hermes CLI, when present."""
    status_match = re.search(
        r"(?:HTTP(?: Error)?|status(?: code)?)\s*[:=]?\s*(\d{3})", text, re.I,
    )
    status_code = int(status_match.group(1)) if status_match else None
    if status_code is None and re.search(
        r"(?:exceeded rate limit|rate limited after)", text, re.I,
    ):
        status_code = 429
    retry_match = re.search(r"retry-after\s*[:=]\s*([0-9.]+)", text, re.I)
    return (
        status_code,
        float(retry_match.group(1)) if retry_match else None,
    )


def hermes_profiles_dir() -> Path:
    """Match Hermes's named-profile resolution for HERMES_HOME."""
    raw = os.environ.get("HERMES_HOME", "").strip()
    root = Path(raw) if raw else Path.home() / ".hermes"
    if raw and root.parent.name == "profiles":
        root = root.parent.parent
    return root / "profiles"


def _agent_log_path(profile: str) -> Path:
    return hermes_profiles_dir() / profile / "logs" / "agent.log"


def _log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _read_log_since(path: Path, offset: int) -> str:
    """Read only log records written by the current Hermes subprocess."""
    try:
        with path.open("rb") as source:
            if path.stat().st_size >= offset:
                source.seek(offset)
            return source.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _terminal_transport_metadata(text: str) -> tuple[int | None, float | None]:
    """Return metadata only when Hermes exhausted its own API attempts."""
    terminal_lines = "\n".join(
        line for line in text.splitlines()
        if "API call failed after" in line or "Rate limited after" in line
    )
    return _transport_metadata(terminal_lines)


def _terminal_transport_error(status_code: int | None) -> str | None:
    if status_code == 429:
        return "HTTP 429: model deployment rate limit exceeded"
    if status_code is not None:
        return f"HTTP {status_code}: model request failed"
    return None


def _get_encoding(model: str | None):
    """Return a tiktoken encoding for the model, falling back to cl100k_base."""
    if not _TIKTOKEN_AVAILABLE:
        return None
    if model:
        try:
            return tiktoken.encoding_for_model(model)
        except KeyError:
            pass
    # Fallback: cl100k_base covers GPT-4-class and most modern models.
    # For GPT-5 / GPT-4o specifically, o200k_base is more accurate.
    try:
        return tiktoken.get_encoding("o200k_base")
    except (KeyError, ValueError):
        return tiktoken.get_encoding("cl100k_base")


def _count_message_tokens(messages: list[dict], model: str | None) -> dict:
    """Estimate input/output tokens from a Hermes session messages array.

    Counts: input = system + user + tool messages + assistant tool_call args;
    output = assistant text content + reasoning text.

    Returns {"input_tokens": int, "output_tokens": int} or empty dict if
    tiktoken unavailable.
    """
    enc = _get_encoding(model)
    if enc is None:
        return {}
    input_tokens = 0
    output_tokens = 0
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        # Normalize content to a string for counting
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for chunk in content:
                if isinstance(chunk, dict):
                    text += chunk.get("text", "") or ""
                elif isinstance(chunk, str):
                    text += chunk
        n = len(enc.encode(text)) if text else 0
        if role == "assistant":
            output_tokens += n
            # Reasoning text (when present) was generated by the model
            r = m.get("reasoning") or ""
            if isinstance(r, str) and r:
                output_tokens += len(enc.encode(r))
            # Tool call arguments are also assistant output
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                args = fn.get("arguments", "")
                if isinstance(args, str):
                    output_tokens += len(enc.encode(args))
                elif isinstance(args, dict):
                    output_tokens += len(enc.encode(json.dumps(args)))
        else:
            # system, user, tool, function — all input from the model's POV
            input_tokens += n
            # Tool result payloads (role=tool) include 'name' and 'tool_call_id'
            # — small extras, ignored for now (~negligible).
    # Per-message overhead: OpenAI's tokenization adds ~4 tokens per message
    # for role/separator framing. Add as a conservative correction.
    input_tokens += 4 * sum(1 for m in messages if m.get("role") != "assistant")
    output_tokens += 4 * sum(1 for m in messages if m.get("role") == "assistant")
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def _sessions_dir(profile: str) -> Path:
    return hermes_profiles_dir() / profile / "sessions"


def _state_db(profile: str) -> Path:
    return hermes_profiles_dir() / profile / "state.db"


def _snapshot_state_sessions(profile: str) -> set[str]:
    db = _state_db(profile)
    if not db.exists():
        return set()
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        try:
            return {str(row[0]) for row in conn.execute("SELECT id FROM sessions")}
        finally:
            conn.close()
    except Exception:
        return set()


def _find_matching_state_session(
    profile: str, before: set[str], effective_message: str,
) -> str | None:
    db = _state_db(profile)
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = conn.execute(
                "SELECT DISTINCT s.id, s.parent_session_id, s.source, s.started_at "
                "FROM sessions s JOIN messages m ON m.session_id = s.id "
                "WHERE m.role = 'user' AND m.content = ? "
                "ORDER BY (s.parent_session_id IS NULL) DESC, "
                "(s.source = 'cli') DESC, s.started_at DESC",
                (effective_message,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return None
    for session_id, _parent, _source, _started_at in rows:
        if str(session_id) not in before:
            return str(session_id)
    return None


def _snapshot_request_dumps(profile: str) -> dict[str, int]:
    directory = _sessions_dir(profile)
    if not directory.exists():
        return {}
    return {
        path.name: path.stat().st_mtime_ns
        for path in directory.glob("request_dump_*.json")
    }


def _model_accessible_tool_names(tool_definitions: list[dict]) -> list[str]:
    """Return tools the model can call directly or through Hermes tool search."""
    direct: list[str] = []
    deferred: set[str] = set()
    for tool in tool_definitions:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        function = function if isinstance(function, dict) else tool
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        direct.append(name)
        if name != "tool_search":
            continue
        description = function.get("description")
        if not isinstance(description, str):
            continue
        _prefix, marker, catalog = description.partition("Deferred tool catalog")
        if not marker:
            continue
        for line in catalog.splitlines():
            match = re.match(r"^-\s+([A-Za-z0-9_.-]+)(?::\s|$)", line.strip())
            if match:
                deferred.add(match.group(1))
    return [*direct, *sorted(deferred - set(direct))]


def _tool_names_from_request_dumps(
    profile: str, before: dict[str, int], session_id: str,
) -> list[str]:
    names: set[str] = set()
    directory = _sessions_dir(profile)
    if directory.exists():
        for path in directory.glob("request_dump_*.json"):
            if path.name in before and path.stat().st_mtime_ns <= before[path.name]:
                continue
            try:
                dump = json.loads(path.read_text())
            except Exception:
                continue
            if str(dump.get("session_id") or "") != session_id:
                continue
            body = ((dump.get("request") or {}).get("body") or {})
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except json.JSONDecodeError:
                    body = {}
            tool_definitions = body.get("tools", []) if isinstance(body, dict) else []
            names.update(_model_accessible_tool_names(tool_definitions))

    cache_path = (
        hermes_profiles_dir() / profile
        / "cache" / "mcp_schema_cache.json"
    )
    try:
        cache = json.loads(cache_path.read_text())
    except Exception:
        cache = {}
    if isinstance(cache, dict):
        for server, entry in cache.items():
            server_key = str(server).replace("-", "_")
            for tool in (entry or {}).get("tools", []):
                name = tool.get("name") if isinstance(tool, dict) else None
                if isinstance(name, str) and name:
                    names.add(f"mcp__{server_key}__{name}")
    return sorted(names)


def _parse_state_session(
    profile: str, session_id: str, available_tools: list[str],
) -> tuple[str, str, list[dict], dict, str | None, list[str]]:
    db = _state_db(profile)
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        try:
            session_row = conn.execute(
                "SELECT model FROM sessions WHERE id = ?", (session_id,),
            ).fetchone()
            rows = conn.execute(
                "SELECT role, content, tool_calls FROM messages "
                "WHERE session_id = ? AND active = 1 ORDER BY id",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return (session_id, "", [], {}, None, available_tools)

    model = session_row[0] if session_row else None
    final_text = ""
    clarify_text = ""
    tool_calls: list[dict] = []
    for role, content, raw_calls in rows:
        if role != "assistant":
            continue
        if isinstance(content, str) and content.strip():
            final_text = content
        try:
            calls = json.loads(raw_calls) if raw_calls else []
        except json.JSONDecodeError:
            calls = []
        for call in calls if isinstance(calls, list) else []:
            function = call.get("function") or {}
            name = function.get("name") or call.get("name") or ""
            raw_args = function.get("arguments", call.get("arguments", {}))
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {"_raw": raw_args}
            tool_calls.append({"tool": name, "args": args or {}})
            if "clarify" in name.lower() and not clarify_text and isinstance(args, dict):
                question = args.get("question", "")
                if question:
                    clarify_text = f"[AGENT ASKED FOR CLARIFICATION]\n{question}"
                    choices = args.get("choices", [])
                    if choices:
                        clarify_text += "\n\nChoices offered: " + ", ".join(map(str, choices))
    usage = _read_real_usage(profile, session_id) or {}
    return (
        session_id,
        clarify_text or final_text,
        tool_calls,
        usage,
        (usage.get("model") if usage else None) or model,
        available_tools,
    )


def _read_real_usage(profile: str, session_id: str) -> dict | None:
    """Read API-reported usage for a hermes session from profile state.db.

    Hermes accumulates the canonical per-call usage into the `sessions`
    row via `update_token_counts(...)` (run_agent.py:7773). Columns are:
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
    reasoning_tokens, estimated_cost_usd, cost_status, cost_source, model.

    Map the database columns to the token fields used by
    `costing.compute_call_cost`.

    Returns None on any lookup failure (missing DB, missing row, schema
    drift, sqlite error) — callers should fall back to tiktoken estimate.
    """
    if not session_id:
        return None
    db = _state_db(profile)
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT model, input_tokens, output_tokens, cache_read_tokens, "
                "cache_write_tokens, reasoning_tokens, estimated_cost_usd, "
                "cost_status, cost_source FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if not row:
        return None
    m, i, o, cr, cw, rt, cost, cs, csrc = row
    i = i or 0
    o = o or 0
    cr = cr or 0
    cw = cw or 0
    return {
        "input_tokens": i,
        "output_tokens": o,
        "cache_read_input_tokens": cr,        # matches costing.py expected key
        "cache_creation_input_tokens": cw,    # matches costing.py expected key
        "total_tokens": i + o,
        "model": m,
        "reasoning_tokens": rt or 0,
        "estimated_cost_usd": cost,
        "cost_status": cs,
        "cost_source": csrc,
        "_source": "hermes_state_db",
    }


def _snapshot_sessions(profile: str) -> dict[str, int]:
    d = _sessions_dir(profile)
    if not d.exists():
        return {}
    return {f.name: f.stat().st_mtime_ns for f in d.glob("*.json")}


def _find_new_session(profile: str, before: dict[str, int] | set[str]) -> Path | None:
    d = _sessions_dir(profile)
    if not d.exists():
        return None
    if isinstance(before, set):
        before = {name: -1 for name in before}
    after = list(d.glob("*.json"))
    changed = [
        f for f in after
        if f.name not in before or f.stat().st_mtime_ns > before[f.name]
    ]
    if changed:
        return max(changed, key=lambda f: f.stat().st_mtime_ns)
    return None


def _find_matching_session(
    profile: str,
    before: dict[str, int] | set[str],
    effective_message: str,
) -> Path | None:
    """Find the session produced by this exact command.

    The benchmark can run multiple Hermes jobs against the same profile. A
    plain newest-file lookup can pick up a neighboring run's session file if
    timestamps race or a previous session is touched late. Matching the user
    message keeps result attribution tied to the subprocess we just launched.
    """
    d = _sessions_dir(profile)
    if not d.exists():
        return None
    if isinstance(before, set):
        before = {name: -1 for name in before}
    changed = [
        f for f in d.glob("*.json")
        if f.name not in before or f.stat().st_mtime_ns > before[f.name]
    ]
    changed.sort(key=lambda f: f.stat().st_mtime_ns, reverse=True)
    fallback: Path | None = None
    for path in changed:
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        user_messages = [
            message.get("content")
            for message in data.get("messages", [])
            if message.get("role") == "user"
        ]
        if not user_messages or user_messages[0] != effective_message:
            continue
        # Hermes background memory/skill review sessions are forked from the
        # main session. Their first user message is the benchmark query, then a
        # second synthetic review prompt is appended. Prefer the main command
        # session, which has exactly the benchmark user message.
        if len(user_messages) == 1:
            return path
        if fallback is None:
            fallback = path
    if fallback is not None:
        return fallback
    for path in changed:
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        for message in data.get("messages", []):
            if message.get("role") != "user":
                continue
            if message.get("content") == effective_message:
                return path
            break
    return None


def _parse_session_json(path: Path) -> tuple[str, str, list[dict], dict, str | None, list[str]]:
    """Parse a Hermes session JSON file.

    Returns: (session_id, final_assistant_text, tool_calls, token_usage, model, available_tools)

    Special handling: if the agent calls a `clarify` tool (Hermes's built-in
    tool for asking the user a question), the clarify question IS treated as
    the agent's response. In non-interactive benchmark mode, clarify times
    out and force-proceeds, but the agent's intent was to ask — that's the
    real response we should grade.

    Token usage is estimated via tiktoken since Hermes' session JSON does not
    persist API-reported usage blocks. Estimates are typically within 1-3%
    of actual API counts for normal text.
    """
    try:
        data = json.loads(path.read_text())
    except Exception:
        return ("", "", [], {}, None, [])
    session_id = data.get("session_id", "") or ""
    model = data.get("model")
    messages = data.get("messages", [])
    token_usage = _count_message_tokens(messages, model)
    available_tools = _model_accessible_tool_names(data.get("tools", []) or [])
    final_text = ""
    tool_calls: list[dict] = []
    clarify_text = ""

    for m in messages:
        role = m.get("role")
        if role == "assistant":
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                final_text = content
            elif isinstance(content, list):
                for chunk in content:
                    if isinstance(chunk, dict) and chunk.get("type") == "text":
                        t = chunk.get("text", "")
                        if t.strip():
                            final_text = t
            # Collect tool calls — check for clarify
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                args_raw = fn.get("arguments", "")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except json.JSONDecodeError:
                    args = {"_raw": args_raw}
                tool_calls.append({"tool": name, "args": args})

                # If agent called clarify, capture the question as a
                # candidate response. The FIRST clarify wins — it represents
                # the agent's initial intent before the tool forced it to
                # proceed. We prepend [CLARIFY] so graders/judges can see
                # this was a question, not a statement.
                if "clarify" in name.lower() and not clarify_text:
                    q = args.get("question", "") if isinstance(args, dict) else ""
                    choices = args.get("choices", []) if isinstance(args, dict) else []
                    if q:
                        clarify_text = f"[AGENT ASKED FOR CLARIFICATION]\n{q}"
                        if choices:
                            clarify_text += "\n\nChoices offered: " + ", ".join(
                                str(c) for c in choices
                            )

    # If the agent tried to clarify, use that as the response —
    # it's what the agent WANTED to say before the harness forced it to proceed.
    if clarify_text:
        return (session_id, clarify_text, tool_calls, token_usage, model, available_tools)

    return (session_id, final_text, tool_calls, token_usage, model, available_tools)


def run_hermes(
    profile: str,
    message: str,
    timeout: int | None = None,
    model: str | None = None,
    cwd: str = "/tmp",
    narrative_time: str | None = None,
) -> HermesResult:
    """Run `hermes -p <profile> chat -q "<message>" -Q --yolo` and return results.

    Uses state.db or legacy session JSON because -Q mode does not reliably
    write the final response to piped stdout.

    If ``narrative_time`` is set (ISO-8601), the timestamp is prepended inline
    to the message as ``[<iso>] <message>``. Hermes has no native clock-override
    mechanism, so this is how we give the agent narrative-time context during
    benchmark ingestion and testing.
    """
    effective_message = (
        f"[{narrative_time}] {message}" if narrative_time else message
    )
    cmd = [HERMES_BIN, "-p", profile, "chat", "-q", effective_message, "-Q", "--yolo"]
    toolsets = os.environ.get("DOLPHINBENCH_HERMES_TOOLSETS")
    if toolsets:
        cmd += ["-t", toolsets]
    if model:
        cmd += ["-m", model]

    before = _snapshot_sessions(profile)
    before_state = _snapshot_state_sessions(profile)
    before_dumps = _snapshot_request_dumps(profile)
    agent_log = _agent_log_path(profile)
    before_log_size = _log_size(agent_log)
    subprocess_env = os.environ.copy()
    capture_submission = os.environ.get("DOLPHINBENCH_CAPTURE_SUBMISSION", "1") == "1"
    if capture_submission:
        subprocess_env["DOLPHINBENCH_SUBMISSION_TRACE_DIR"] = str(
            hermes_profiles_dir() / profile / "submission-traces"
        )
        # This limit affects observation only, not the actual provider request.
        subprocess_env["HERMES_PLUGIN_PAYLOAD_MAX_CHARS"] = "10000000"
    binary = shutil.which(HERMES_BIN) or HERMES_BIN
    interpreter = [sys.executable]
    if Path(binary).is_file():
        with Path(binary).open("rb") as source:
            first_line = source.readline(1024).decode("utf-8", errors="replace").strip()
        if first_line.startswith("#!") and "python" in first_line:
            interpreter = shlex.split(first_line[2:])
    subprocess_env["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(Path(__file__).resolve().parents[1]), subprocess_env.get("PYTHONPATH"),
    )))
    cmd = [*interpreter, "-m", "harness.hermes_observed_cli", binary, *cmd[1:]]
    if profile not in _TOOL_SURFACE_BY_PROFILE:
        subprocess_env["HERMES_DUMP_REQUESTS"] = "1"

    try:
        with tempfile.TemporaryDirectory(prefix="dolphinbench-turn-") as status_dir:
            status_path = Path(status_dir) / "status.json"
            subprocess_env["DOLPHINBENCH_TURN_STATUS_PATH"] = str(status_path)
            from reference.execution.hermes_agent import run_cli
            proc = run_cli(
                cmd,
                profile=profile,
                profile_path=hermes_profiles_dir() / profile,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=subprocess_env,
            )
            turn_status = json.loads(status_path.read_text()) if status_path.exists() else None
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        stdout = (e.stdout or "") if isinstance(e.stdout, str) else ""
        stderr = (e.stderr or "") if isinstance(e.stderr, str) else ""
        session_path = _find_matching_session(profile, before, effective_message)
        if session_path:
            sid, text, tcs, usage, mdl, tools = _parse_session_json(session_path)
            # Prefer real API-reported usage from state.db over tiktoken estimate.
            real = _read_real_usage(profile, sid)
            if real:
                usage = real
                mdl = real.get("model") or mdl
            return HermesResult(
                ok=False,
                session_id=sid,
                response_text=text,
                stdout=stdout, stderr=stderr,
                session_path=str(session_path),
                tool_calls=tcs,
                token_usage=usage,
                model=mdl,
                available_tools=tools,
                error=f"timeout after {timeout}s but got response from session file",
            )
        state_session_id = _find_matching_state_session(
            profile, before_state, effective_message,
        )
        if state_session_id:
            tools = _TOOL_SURFACE_BY_PROFILE.get(profile)
            if tools is None:
                tools = _tool_names_from_request_dumps(
                    profile, before_dumps, state_session_id,
                )
                _TOOL_SURFACE_BY_PROFILE[profile] = tools
            sid, text, tcs, usage, mdl, tools = _parse_state_session(
                profile, state_session_id, tools,
            )
            return HermesResult(
                ok=False, session_id=sid, response_text=text,
                stdout=stdout, stderr=stderr, session_path=str(_state_db(profile)),
                tool_calls=tcs, token_usage=usage, model=mdl,
                available_tools=tools,
                error=f"timeout after {timeout}s but got response from state.db",
            )
        return HermesResult(
            ok=False, session_id=None, response_text="",
            stdout=stdout, stderr=stderr,
            error=f"timeout after {timeout}s",
        )
    except Exception as e:
        return HermesResult(
            ok=False, session_id=None, response_text="",
            stdout="", stderr="", error=str(e),
        )

    session_path = _find_matching_session(profile, before, effective_message)
    terminal_status, terminal_retry_after = _terminal_transport_metadata(
        stdout + "\n" + stderr + "\n" + _read_log_since(agent_log, before_log_size)
    )
    terminal_error = _terminal_transport_error(terminal_status)
    if turn_status is None:
        terminal_error = terminal_error or "Hermes did not record a completed turn"
    elif (turn_status.get("completed") is not True or turn_status.get("partial")
          or turn_status.get("error")):
        terminal_error = str(turn_status.get("error") or "Hermes returned an incomplete turn")
    if session_path:
        sid, text, tcs, usage, mdl, tools = _parse_session_json(session_path)
        # Prefer real API-reported usage from state.db over tiktoken estimate.
        # state.db is written per-LLM-call inside run_agent so by the time
        # hermes exits the row is fully flushed.
        real = _read_real_usage(profile, sid)
        if real:
            usage = real
            mdl = real.get("model") or mdl
        return HermesResult(
            # A completed answer can survive a later CLI shutdown failure.
            ok=bool(text) and terminal_error is None,
            session_id=sid or None,
            response_text=text,
            stdout=stdout,
            stderr=stderr,
            session_path=str(session_path),
            tool_calls=tcs,
            token_usage=usage,
            model=mdl,
            available_tools=tools,
            error=terminal_error,
            status_code=terminal_status,
            retry_after_seconds=terminal_retry_after,
        )

    state_session_id = _find_matching_state_session(
        profile, before_state, effective_message,
    )
    if state_session_id:
        tools = _TOOL_SURFACE_BY_PROFILE.get(profile)
        if tools is None:
            tools = _tool_names_from_request_dumps(
                profile, before_dumps, state_session_id,
            )
            _TOOL_SURFACE_BY_PROFILE[profile] = tools
        sid, text, tcs, usage, mdl, tools = _parse_state_session(
            profile, state_session_id, tools,
        )
        return HermesResult(
            ok=bool(text) and terminal_error is None, session_id=sid, response_text=text,
            stdout=stdout, stderr=stderr, session_path=str(_state_db(profile)),
            tool_calls=tcs, token_usage=usage, model=mdl,
            available_tools=tools,
            error=terminal_error,
            status_code=terminal_status,
            retry_after_seconds=terminal_retry_after,
        )

    # No legacy session file or current state.db session found — truly failed.
    status_code, retry_after_seconds = _transport_metadata(stdout + "\n" + stderr)
    return HermesResult(
        ok=False, session_id=None, response_text="",
        stdout=stdout, stderr=stderr,
        error=f"no new session file found in {_sessions_dir(profile)}",
        status_code=status_code,
        retry_after_seconds=retry_after_seconds,
    )


_SEED_RECEIPT_ENV = {
    "mem0": "DOLPHINBENCH_MEM0_SEED_RECEIPTS",
    "honcho": "DOLPHINBENCH_HONCHO_SEED_RECEIPTS",
    "hindsight": "DOLPHINBENCH_HINDSIGHT_SEED_RECEIPTS",
    "supermemory": "DOLPHINBENCH_SUPERMEMORY_SEED_RECEIPTS",
}


@dataclass(frozen=True)
class FailureDetails:
    """A transport failure normalized from a driver result."""

    transient: bool
    status_code: int | None = None
    retry_after_seconds: float | None = None
    kind: str = "non_transient"


_TRANSIENT_HTTP_STATUS = {408, 409, 429, 500, 502, 503, 504}


def _retry_after_seconds(value: Any) -> float | None:
    """Parse a Retry-After value in seconds or HTTP-date form."""
    if value is None:
        return None
    text = str(value).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, IndexError):
        return None


def _failure_details(result_or_error: Any) -> FailureDetails:
    """Classify the failure without treating a model response as transport noise."""
    error = getattr(result_or_error, "error", result_or_error)
    status = getattr(result_or_error, "status_code", None)
    retry_after = getattr(result_or_error, "retry_after_seconds", None)
    headers = getattr(error, "headers", None)
    if isinstance(error, HTTPError):
        status = error.code
        headers = error.headers
    if status is None:
        match = re.search(r"(?:HTTP(?: Error)?|status(?: code)?)\s*[:=]?\s*(\d{3})", str(error), re.I)
        if match:
            status = int(match.group(1))
    if retry_after is None and headers is not None:
        retry_after = _retry_after_seconds(headers.get("Retry-After"))
    if retry_after is None:
        match = re.search(r"retry-after\s*[:=]\s*([^,;\s]+)", str(error), re.I)
        if match:
            retry_after = _retry_after_seconds(match.group(1))
    if isinstance(status, str) and status.isdigit():
        status = int(status)
    if isinstance(status, int) and status in _TRANSIENT_HTTP_STATUS:
        return FailureDetails(True, status, retry_after, "http")
    if isinstance(error, (TimeoutError, ConnectionError, URLError)):
        return FailureDetails(True, None, retry_after, "connection_or_timeout")
    text = str(error or "").lower()
    if any(token in text for token in (
        "timeout", "timed out", "connection reset", "connection refused",
        "connection aborted", "broken pipe", "remote end closed", "dns",
        "name or service not known", "temporarily unavailable",
    )):
        return FailureDetails(True, None, retry_after, "connection_or_timeout")
    return FailureDetails(False, status if isinstance(status, int) else None, retry_after)


def _retry_delay(details: FailureDetails, retry_number: int) -> float:
    """Use server guidance when supplied; otherwise use bounded exponential delay."""
    base = float(os.environ.get("DOLPHINBENCH_RETRY_BASE_SECONDS", "5"))
    cap = float(os.environ.get("DOLPHINBENCH_RETRY_MAX_SECONDS", "60"))
    delay = min(cap, base * (2 ** max(retry_number - 1, 0)))
    if details.retry_after_seconds is not None:
        delay = max(delay, details.retry_after_seconds)
    return delay


def _seed_message_was_delivered(
    provider: str,
    result: Any,
    *,
    matching_seed_receipt: bool = False,
) -> bool:
    """Whether one seed message reached a durable agent session."""
    if provider in _SEED_RECEIPT_ENV:
        return matching_seed_receipt
    if bool(getattr(result, "ok", False)):
        return True
    session_path = getattr(result, "session_path", None)
    return bool(
        getattr(result, "session_id", None)
        and session_path and Path(session_path).is_file()
    )


def _seed_delivery_state(
    provider: str,
    result: Any,
    *,
    matching_seed_receipt: bool = False,
) -> str:
    """Return delivered, known_not_delivered, or ambiguous for a seed call."""
    if _seed_message_was_delivered(
        provider, result, matching_seed_receipt=matching_seed_receipt,
    ):
        return "delivered"
    details = _failure_details(result)
    # An explicit rate-limit response and a failure before a connection was
    # established are the only cases where resubmission is safe. A timeout,
    # 408, 409, or 5xx may have reached the provider despite a failed client.
    if details.status_code == 429 or (
        details.kind == "connection_or_timeout"
        and "timeout" not in str(getattr(result, "error", "")).lower()
        and "timed out" not in str(getattr(result, "error", "")).lower()
    ):
        return "known_not_delivered"
    # For receipt-backed providers, any other missing receipt is uncertain.
    # The provider may have accepted the write and then failed to persist the
    # receipt, so replaying it could duplicate the seed item.
    if provider in _SEED_RECEIPT_ENV:
        return "ambiguous"
    return "ambiguous" if details.transient else "known_not_delivered"


_REQUIRED_PROVIDER_RUNTIME_TOOLS = {
    "builtin": set(),
    "builtin_dolphinbench": set(),
    "honcho": {
        "honcho_profile",
        "honcho_search",
        "honcho_context",
        "honcho_reasoning",
    },
    "mem0": {"search_memories", "mem0_search"},
    "hindsight": {"hindsight_recall", "hindsight_reflect"},
    "supermemory": {"supermemory_search", "supermemory_profile"},
}


_SEED_ONLY_PROVIDER_RUNTIME_TOOLS = {
    # Mem0 needs write tools while ingesting the synthetic history. Phase 2
    # is read-only and must expose search_memories only.
    "mem0": {"save_memory", "update_memory", "delete_memory", "mem0_add", "mem0_update", "mem0_delete"},
    "honcho": {"honcho_conclude"},
    "hindsight": {"hindsight_retain"},
    "supermemory": {"supermemory_store", "supermemory_forget"},
}


_REQUIRED_NATIVE_RUNTIME_TOOLS = {"session_search"}


_MOCK_TOOL_PREFIXES = (
    "mcp_dolphinbench_apps_",
    "mcp__dolphinbench_apps__",
)


def _load_all_mock_tool_names() -> set[str]:
    names: set[str] = set()
    for manifest in (DOLPHINBENCH_ROOT / "mock_mcp" / "manifests").glob("*.yaml"):
        try:
            data = yaml.safe_load(manifest.read_text()) or {}
        except Exception:
            continue
        names.update(str(name) for name in data.get("tools", []) or [])
    return names


_ALL_MOCK_TOOL_NAMES = _load_all_mock_tool_names()


@lru_cache(maxsize=None)
def _mock_tool_names_for_persona(persona: str) -> frozenset[str]:
    manifest = DOLPHINBENCH_ROOT / "mock_mcp" / "manifests" / f"{persona}.yaml"
    if not manifest.is_file():
        return frozenset()
    data = yaml.safe_load(manifest.read_text()) or {}
    return frozenset(str(tool) for tool in data.get("tools", []) or [])


def _is_dolphinbench_mock_tool_name(name: str) -> bool:
    persona = get_setting("DOLPHINBENCH_PERSONA")
    expected = _mock_tool_names_for_persona(persona) if persona else _ALL_MOCK_TOOL_NAMES
    if name in expected:
        return True
    for prefix in _MOCK_TOOL_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):] in expected
    return False


def _validate_hermes_runtime_tools(
    r: Any,
    phase: str,
    item_id: str,
    provider: str,
) -> None:
    """Fail fast if Hermes did not actually expose the DolphinBench mock tools.

    Profile config checks are necessary but not sufficient: Hermes can have a
    safe-looking profile while the MCP subprocess fails to start, leaving the
    session with memory/native tools only. That creates fake 0/200 scores and
    can expose real platform tools. The session JSON is the ground truth.
    """
    tools = list(getattr(r, "available_tools", []) or [])
    if not tools:
        raise SystemExit(
            f"FATAL: Hermes session for {phase} {item_id} recorded no available "
            "tools. Refusing to continue because DolphinBench mock tools may be absent."
        )
    mock_tools = [name for name in tools if _is_dolphinbench_mock_tool_name(name)]
    if not mock_tools:
        preview = ", ".join(tools[:12])
        raise SystemExit(
            f"FATAL: Hermes session for {phase} {item_id} exposed no DolphinBench mock "
            f"tools. First tools seen: {preview}. This run is invalid."
        )
    required_native = set(_REQUIRED_NATIVE_RUNTIME_TOOLS)
    if phase == "seed":
        required_native.add("memory")
    missing_native = sorted(required_native - set(tools))
    if missing_native:
        preview = ", ".join(tools[:20])
        raise SystemExit(
            f"FATAL: Hermes session for {phase} {item_id} is missing required "
            f"native memory/search tools {missing_native}. First tools seen: "
            f"{preview}."
        )
    if phase == "test" and "memory" in tools:
        raise SystemExit(
            f"FATAL: Hermes session for test {item_id} exposed the writable native "
            "memory tool. Test runs must be read-only."
        )
    required_provider_tools = _REQUIRED_PROVIDER_RUNTIME_TOOLS.get(provider, set())
    if required_provider_tools and not (set(tools) & required_provider_tools):
        preview = ", ".join(tools[:20])
        raise SystemExit(
            f"FATAL: Hermes session for {phase} {item_id} exposed DolphinBench mock "
            f"tools but none of the expected {provider} memory tools "
            f"{sorted(required_provider_tools)}. First tools seen: {preview}."
        )
    forbidden = sorted(set(tools) & _SEED_ONLY_PROVIDER_RUNTIME_TOOLS.get(provider, set())) if phase == "test" else []
    if forbidden:
        raise SystemExit(
            f"FATAL: Hermes session for {phase} {item_id} exposed memory write "
            f"tools {forbidden}. Tests must leave the ingested store unchanged."
        )
    other_memory_tools = set().union(*(
        _REQUIRED_PROVIDER_RUNTIME_TOOLS.get(name, set())
        | _SEED_ONLY_PROVIDER_RUNTIME_TOOLS.get(name, set())
        for name in ("mem0", "honcho", "hindsight", "supermemory")
        if name != provider
    ))
    if unexpected := sorted(set(tools) & other_memory_tools):
        raise SystemExit(
            f"FATAL: {item_id} exposes tools from a different memory provider: {unexpected}"
        )


def _fallback_session_calls_for_grading(tool_calls: list[dict]) -> list[dict]:
    """Normalize Hermes session tool calls when the MCP JSONL log is empty.

    The action grader is written against the mock MCP tool names
    (``send_email``, ``post_pr_comment``, ...). Hermes' session DB stores MCP
    calls under fully-qualified names such as
    ``mcp_dolphinbench_apps_send_email``. If the JSONL MCP log is unavailable,
    those saved session calls are still the same actions, so strip only the
    known mock-tool prefixes and ignore non-action memory/search tools.
    """
    normalized: list[dict] = []
    for call in tool_calls or []:
        tool = call.get("tool") or call.get("name")
        if not isinstance(tool, str):
            continue
        for prefix in _MOCK_TOOL_PREFIXES:
            if tool.startswith(prefix):
                args = call.get("args")
                if args is None:
                    args = call.get("arguments")
                normalized.append({
                    **call,
                    "tool": tool[len(prefix):],
                    "args": args or {},
                })
                break
    return normalized
