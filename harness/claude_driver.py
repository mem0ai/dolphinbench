"""Claude Code CLI driver for isolated DolphinBench agent calls.

The driver deliberately requires an explicit Claude config directory and MCP
configuration.  A benchmark caller must choose those paths for its run; this
module never falls back to the operator's normal Claude configuration.
"""

from __future__ import annotations

import json
import fnmatch
import os
import re
import shlex
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")


_CLAUDE_AUTH_VARIABLES = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
}


def _subscription_environment(env: Mapping[str, str] | None, *, inherit: bool = True) -> dict[str, str]:
    """Build a Claude child environment that can use only subscription auth."""
    child = dict(os.environ) if inherit else {
        key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
        if key in os.environ
    }
    oauth_token = None if env is None else env.get("CLAUDE_CODE_OAUTH_TOKEN")
    child.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    if env is not None:
        child.update({str(key): str(value) for key, value in env.items()})
    if oauth_token is None:
        child.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    else:
        child["CLAUDE_CODE_OAUTH_TOKEN"] = str(oauth_token)

    for name in list(child):
        if (
            name in _CLAUDE_AUTH_VARIABLES
            or name.startswith(("ANTHROPIC_BEDROCK_", "ANTHROPIC_VERTEX_", "ANTHROPIC_FOUNDRY_"))
            or name.startswith(("VERTEXAI_", "FOUNDRY_", "AZURE_OPENAI_", "AZURE_AI_"))
        ):
            child.pop(name, None)
    return child


@dataclass
class ClaudeResult:
    """DolphinBench Driver Contract result from one Claude Code invocation."""

    ok: bool
    session_id: str | None
    response_text: str
    tool_calls: list[dict] = field(default_factory=list)
    memory_recall: dict = field(default_factory=dict)
    memory_events_created: int = 0
    token_usage: dict = field(default_factory=dict)
    error: str | None = None
    model: str | None = None
    stdout: str = ""
    stderr: str = ""
    status_code: int | None = None
    retry_after_seconds: float | None = None
    available_tools: list[str] = field(default_factory=list)
    rate_limit_reset_at: int | None = None


def _short_error(value: Any, limit: int = 500) -> str:
    if isinstance(value, dict):
        value = value.get("message") or value.get("error") or json.dumps(value)
    text = " ".join(str(value or "").split())
    return text[:limit] or "Claude Code failed without an error message"


def _transport_metadata(text: str, stderr: str = "") -> tuple[int | None, float | None, int | None]:
    events = _trace_events(text)
    results = [event for event in events if event.get("type") == "result"]
    terminal = results[-1] if results else {}
    rejected = [event["rate_limit_info"] for event in events
                if event.get("type") == "rate_limit_event"
                and isinstance(event.get("rate_limit_info"), dict)
                and event["rate_limit_info"].get("status") == "rejected"]
    code = terminal.get("api_error_status")
    if isinstance(code, bool) or not isinstance(code, int) or not 400 <= code <= 599:
        code = None
    # A warning or an earlier rejection must not override a completed final turn.
    if terminal and terminal.get("is_error") is False and code is None:
        return None, None, None
    if code is None and rejected:
        code = 429
    reset = rejected[-1].get("resetsAt") if rejected and code == 429 else None
    if isinstance(reset, bool) or not isinstance(reset, int) or reset <= 0:
        reset = None
    if code is not None:
        return code, None, reset
    if events:
        # Do not interpret quoted tool output or ordinary answer text as transport errors.
        text = str(terminal.get("result", "")) if terminal.get("is_error") is True else ""
    text = f"{text}\n{stderr}"
    status_match = re.search(
        r"(?:HTTP(?: Error)?|status(?: code)?)\s*[:=]?\s*(\d{3})", text, re.I,
    )
    status_code = int(status_match.group(1)) if status_match else None
    if status_code is None and re.search(r"rate limit|rate limited", text, re.I):
        status_code = 429
    retry_match = re.search(r"retry[- ]after\s*[:=]?\s*([0-9.]+)", text, re.I)
    return status_code, float(retry_match.group(1)) if retry_match else None, None


def _trace_events(stdout: str) -> list[dict[str, Any]]:
    """Read either one JSON result object or Claude's stream-json JSONL."""
    text = stdout.strip()
    if not text:
        return []
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        return [decoded]
    if isinstance(decoded, list):
        return [event for event in decoded if isinstance(event, dict)]

    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _text_blocks(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        block.get("text", "")
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]


def _tool_calls(content: Any, seen_ids: set[str]) -> list[dict]:
    if not isinstance(content, list):
        return []
    calls: list[dict] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if not isinstance(name, str) or not name:
            continue
        call_id = block.get("id")
        if isinstance(call_id, str) and call_id:
            if call_id in seen_ids:
                continue
            seen_ids.add(call_id)
        args = block.get("input", block.get("arguments", {}))
        calls.append({"tool": name, "args": args if isinstance(args, dict) else {}})
    return calls


def _rejected_unavailable_tools(stdout: str) -> set[str]:
    """Exempt only calls the runtime proves could not execute."""
    events = _trace_events(stdout)
    declarations = [event.get("tools") for event in events
                    if event.get("type") == "system" and event.get("subtype") == "init"]
    if not declarations or any(not isinstance(value, list) for value in declarations):
        return set()
    registered = {name for names in declarations for name in names if isinstance(name, str)}
    calls: dict[str, list[dict]] = {}
    results: dict[str, list[dict]] = {}
    for event in events:
        message = event.get("message")
        content = message.get("content", []) if isinstance(message, dict) else []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if event.get("type") == "assistant" and block.get("type") == "tool_use":
                calls.setdefault(block.get("name", ""), []).append(block)
            elif event.get("type") == "user" and block.get("type") == "tool_result":
                results.setdefault(block.get("tool_use_id", ""), []).append(block)
    rejected = set()
    for name, attempts in calls.items():
        if not name or name in registered:
            continue
        expected = f"<tool_use_error>Error: No such tool available: {name}</tool_use_error>"
        if all(attempt.get("id") and results.get(attempt["id"])
               and all(result.get("is_error") is True and result.get("content") == expected
                       for result in results[attempt["id"]]) for attempt in attempts):
            rejected.add(name)
    return rejected


def _usage_block(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    usage = dict(value)
    aliases = {
        "inputTokens": "input_tokens",
        "outputTokens": "output_tokens",
        "cacheCreationInputTokens": "cache_creation_input_tokens",
        "cacheReadInputTokens": "cache_read_input_tokens",
    }
    for source, target in aliases.items():
        if target not in usage and source in usage:
            usage[target] = usage[source]
    return usage


def _parse_trace(stdout: str) -> tuple[
    str | None, str, list[dict], dict, str | None, str | None, bool
]:
    """Extract contract data from a Claude JSON result or stream-json trace."""
    events = _trace_events(stdout)
    session_id: str | None = None
    model: str | None = None
    usage: dict = {}
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    seen_tool_ids: set[str] = set()
    final_event: dict[str, Any] | None = None
    stream_error: str | None = None

    for event in events:
        raw_session_id = event.get("session_id")
        if isinstance(raw_session_id, str) and raw_session_id:
            session_id = raw_session_id
        raw_model = event.get("model")
        if isinstance(raw_model, str) and raw_model:
            model = raw_model
        message = event.get("message")
        if isinstance(message, dict):
            message_model = message.get("model")
            if isinstance(message_model, str) and message_model:
                model = message_model
            text_parts.extend(_text_blocks(message.get("content")))
            tool_calls.extend(_tool_calls(message.get("content"), seen_tool_ids))
            message_usage = _usage_block(message.get("usage"))
            if message_usage:
                usage = message_usage
        tool_calls.extend(_tool_calls(event.get("content"), seen_tool_ids))
        event_usage = _usage_block(event.get("usage"))
        if event_usage:
            usage = event_usage
        if event.get("type") == "result":
            final_event = event
        elif event.get("type") == "error":
            stream_error = _short_error(event.get("error") or event.get("message"))

    if final_event is None:
        return session_id, "", tool_calls, usage, model, stream_error, False

    result_model = final_event.get("model")
    if isinstance(result_model, str) and result_model:
        model = result_model
    result_usage = _usage_block(final_event.get("usage"))
    if result_usage:
        usage = result_usage
    if model is None:
        model_usage = final_event.get("modelUsage")
        if isinstance(model_usage, dict) and model_usage:
            first_model = next(iter(model_usage), None)
            if isinstance(first_model, str):
                model = first_model

    if not usage:
        model_usage = final_event.get("modelUsage")
        if isinstance(model_usage, dict) and model_usage:
            selected_usage = model_usage.get(model) if model else None
            if not isinstance(selected_usage, dict):
                selected_usage = next(iter(model_usage.values()), None)
            usage = _usage_block(selected_usage)

    result = final_event.get("result")
    response_text = result if isinstance(result, str) and result else "\n".join(text_parts)
    is_error = bool(final_event.get("is_error"))
    error = stream_error
    if is_error:
        error = _short_error(final_event.get("error") or result or "Claude returned an error result")
    return session_id, response_text, tool_calls, usage, model, error, not is_error


_NATIVE_MEMORY_TOOLS = {
    "seed": ("Read", "Write", "Edit"),
    "read_only": ("Read",),
}


def _validate_native_memory(
    mode: str | None, memory_dir: str | Path | None,
) -> tuple[str | None, Path | None, list[str]]:
    if mode is None and memory_dir is None:
        return None, None, []
    if mode not in _NATIVE_MEMORY_TOOLS:
        raise ValueError("native_memory_mode must be 'seed' or 'read_only'")
    if memory_dir is None:
        raise ValueError("native_memory_dir is required when native_memory_mode is set")
    root = Path(memory_dir).expanduser().resolve()
    if mode == "read_only" and not root.is_dir():
        raise ValueError(f"native_memory_dir does not exist: {root}")
    if root.name != "auto-memory":
        raise ValueError("native_memory_dir must use the configured auto-memory directory name")
    return mode, root, list(_NATIVE_MEMORY_TOOLS[mode])


def _write_native_memory_settings(config_dir: Path, memory_dir: Path) -> Path:
    """Point Claude's supported autoMemoryDirectory setting at the isolated tree."""
    path = config_dir / "dolphinbench-native-memory-settings.json"
    path.write_text(json.dumps({"autoMemoryDirectory": str(memory_dir)}) + "\n", encoding="utf-8")
    return path


def _native_allowed_tools(mode: str | None, memory_dir: Path | None) -> list[str]:
    if mode is None or memory_dir is None:
        return []
    scope = str(memory_dir / "**")
    return [f"{tool}({scope})" for tool in _NATIVE_MEMORY_TOOLS[mode]]


def _validate_configuration(
    claude_config_dir: str | Path | None,
    mcp_config_path: str | Path | None,
    allowed_mcp_tools: Sequence[str] | None,
    *,
    plugin_mcp: bool = False,
) -> tuple[Path, Path | None, list[str]]:
    if claude_config_dir is None:
        raise ValueError("claude_config_dir is required; global Claude config is not used")
    if allowed_mcp_tools is None:
        raise ValueError("allowed_mcp_tools is required; no default tool allowlist exists")
    if isinstance(allowed_mcp_tools, (str, bytes)):
        raise ValueError("allowed_mcp_tools must be a sequence of MCP tool names")

    config_dir = Path(claude_config_dir).expanduser().resolve()
    tools = list(allowed_mcp_tools)
    if any(not isinstance(name, str) or not name.startswith("mcp__") for name in tools):
        raise ValueError("allowed_mcp_tools may contain only fully-qualified mcp__ tools")
    if len(set(tools)) != len(tools):
        raise ValueError("allowed_mcp_tools must not contain duplicates")
    if mcp_config_path is None:
        if tools and not plugin_mcp:
            raise ValueError("mcp_config_path is required when allowed_mcp_tools is non-empty")
        return config_dir, None, tools
    mcp_path = Path(mcp_config_path).expanduser().resolve()
    if not mcp_path.is_file():
        raise ValueError(f"mcp_config_path does not exist: {mcp_path}")
    try:
        mcp_config = json.loads(mcp_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid MCP config {mcp_path}: {exc}") from exc
    if not isinstance(mcp_config, dict) or not isinstance(mcp_config.get("mcpServers"), dict):
        raise ValueError(f"MCP config {mcp_path} must contain an mcpServers object")
    if not mcp_config["mcpServers"]:
        raise ValueError(f"MCP config {mcp_path} must define at least one MCP server")
    return config_dir, mcp_path, tools


def run_claude(
    message: str,
    session_id: str | None = None,
    workspace_id: str | None = None,
    narrative_time: str | None = None,
    timeout: int = 300,
    *,
    claude_command: str | Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    claude_config_dir: str | Path | None = None,
    mcp_config_path: str | Path | None = None,
    allowed_mcp_tools: Sequence[str] | None = None,
    cwd: str | Path | None = None,
    model: str | None = None,
    native_memory_mode: str | None = None,
    native_memory_dir: str | Path | None = None,
    plugin_directories: Sequence[str | Path] = (),
    settings: Mapping[str, Any] | None = None,
    persist_session: bool = False,
    builtin_tools: Sequence[str] | None = None,
    permission_mode: str | None = None,
    run_as_user: int | None = None,
    run_as_group: int | None = None,
    recovery_session_id: str | None = None,
    **kwargs: Any,
) -> ClaudeResult:
    """Run one fresh, non-interactive Claude Code agent session.

    ``session_id`` and ``workspace_id`` are accepted for Driver Contract
    compatibility but intentionally do not resume state. Only an explicitly
    adjudicated ingestion recovery can use ``recovery_session_id``; normal
    benchmark calls remain independent. ``claude_command``, ``env``,
    and all config paths are injectable so tests can use a local fake binary.
    """
    del session_id, workspace_id, kwargs
    try:
        config_dir, mcp_path, allowed_tools = _validate_configuration(
            claude_config_dir, mcp_config_path, allowed_mcp_tools,
            plugin_mcp=bool(plugin_directories or (settings or {}).get("hooks")),
        )
        native_mode, native_dir, native_tools = _validate_native_memory(
            native_memory_mode, native_memory_dir,
        )
        plugin_paths = [Path(p).resolve() for p in plugin_directories]
        if any(not p.is_dir() for p in plugin_paths):
            raise ValueError("each plugin directory must exist")
        if permission_mode not in (None, "default", "bypassPermissions"):
            raise ValueError("unsupported permission mode")
        if builtin_tools is not None:
            native_tools = list(builtin_tools)
        if recovery_session_id is not None:
            uuid.UUID(recovery_session_id)
            if not persist_session or native_mode != "seed":
                raise ValueError("Session recovery requires persistent ingestion mode")
    except ValueError as exc:
        return ClaudeResult(ok=False, session_id=None, response_text="", error=str(exc))

    # A caller must select the config root.  Removing an ambient value first
    # prevents a process-level default from leaking into this benchmark call.
    # Keep the normal executable environment (PATH, credentials, TLS settings,
    # and similar runtime requirements), then apply the job-specific overlay.
    # Only the global Claude config root is forbidden for benchmark isolation.
    subprocess_env = _subscription_environment(env, inherit=run_as_user is None)
    subprocess_env.pop("CLAUDE_CONFIG_DIR", None)
    subprocess_env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return ClaudeResult(
            ok=False, session_id=None, response_text="",
            error=f"cannot create claude_config_dir: {_short_error(exc)}",
            available_tools=allowed_tools,
        )

    command = claude_command if claude_command is not None else CLAUDE_BIN
    command_parts = [command] if isinstance(command, str) else list(command)
    if not command_parts or not all(isinstance(part, str) and part for part in command_parts):
        return ClaudeResult(ok=False, session_id=None, response_text="", error="invalid claude_command")

    fresh_session_id = recovery_session_id or str(uuid.uuid4())
    effective_message = f"[{narrative_time}] {message}" if narrative_time else message
    combined_settings = dict(settings or {})
    if native_dir:
        combined_settings["autoMemoryDirectory"] = str(native_dir)
    if native_mode == "read_only":
        guard = Path(__file__).with_name("claude_native_read_guard.py").resolve()
        hooks = dict(combined_settings.get("hooks") or {})
        hooks["PreToolUse"] = [*hooks.get("PreToolUse", []), {
            "matcher": "Read",
            "hooks": [{
                "type": "command",
                "command": shlex.join([sys.executable, str(guard), str(native_dir)]),
                "timeout": 5,
            }],
        }]
        combined_settings["hooks"] = hooks
    settings_path = None
    if combined_settings:
        settings_path = config_dir / "dolphinbench-native-memory-settings.json"
        settings_path.write_text(json.dumps(combined_settings) + "\n", encoding="utf-8")
        if run_as_user is not None:
            os.chown(settings_path, run_as_user, run_as_group if run_as_group is not None else -1)
    cmd = [
        *command_parts,
        "--print",
        effective_message,
        "--output-format",
        "stream-json",
        "--verbose",
        "--tools",
        ",".join(native_tools),
        "--resume" if recovery_session_id else "--session-id",
        fresh_session_id,
    ]
    if not persist_session:
        cmd.append("--no-session-persistence")
    for plugin_path in plugin_paths:
        cmd.extend(["--plugin-dir", str(plugin_path)])
    if permission_mode:
        cmd.extend(["--permission-mode", permission_mode])
    if mcp_path is not None:
        cmd.extend(["--strict-mcp-config", "--mcp-config", str(mcp_path)])
    if settings_path is not None:
        cmd.extend(["--settings", str(settings_path)])
    allowed_surface = [*allowed_tools, *_native_allowed_tools(native_mode, native_dir)]
    if allowed_surface:
        cmd.extend(["--allowedTools", ",".join(allowed_surface)])
    if model:
        cmd.extend(["--model", model])

    try:
        identity = {}
        if run_as_user is not None:
            identity = {"user": run_as_user, "group": run_as_group, "extra_groups": []}
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd is not None else None,
            env=subprocess_env,
            check=False,
            **identity,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
        status_code, retry_after_seconds, reset_at = _transport_metadata(stdout, stderr)
        return ClaudeResult(
            ok=False, session_id=fresh_session_id, response_text="", stdout=stdout,
            stderr=stderr, error=f"timeout after {timeout}s", status_code=status_code,
            retry_after_seconds=retry_after_seconds, available_tools=allowed_tools,
            rate_limit_reset_at=reset_at,
        )
    except OSError as exc:
        return ClaudeResult(
            ok=False, session_id=None, response_text="",
            error=f"transport failure: {_short_error(exc)}", available_tools=allowed_tools,
        )

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    parsed_session_id, response_text, tool_calls, usage, detected_model, trace_error, complete = _parse_trace(stdout)
    status_code, retry_after_seconds, reset_at = _transport_metadata(stdout, stderr)
    result_session_id = parsed_session_id or fresh_session_id
    allowed_trace_tools = set(allowed_tools) | set(native_tools)
    rejected_unavailable = _rejected_unavailable_tools(stdout) if native_memory_mode == "seed" else set()
    unexpected_tools = sorted({
        call["tool"] for call in tool_calls
        if not any(fnmatch.fnmatchcase(call["tool"], pattern) for pattern in allowed_trace_tools)
        and call["tool"] not in rejected_unavailable
    })

    error = trace_error
    if unexpected_tools:
        error = f"Claude used tools outside the allowed MCP surface: {', '.join(unexpected_tools)}"
    elif error:
        pass
    elif proc.returncode != 0:
        error = f"Claude exited with status {proc.returncode}: {_short_error(stderr or stdout)}"
    elif not complete:
        error = error or "Claude trace did not contain a final result event"
    elif not response_text.strip():
        error = "Claude returned an empty final response"

    return ClaudeResult(
        ok=error is None,
        session_id=result_session_id,
        response_text=response_text,
        tool_calls=tool_calls,
        token_usage=usage,
        error=error,
        model=detected_model or model,
        stdout=stdout,
        stderr=stderr,
        status_code=status_code,
        retry_after_seconds=retry_after_seconds,
        available_tools=[*allowed_tools, *native_tools],
        rate_limit_reset_at=reset_at,
    )
