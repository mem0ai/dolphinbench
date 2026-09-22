#!/usr/bin/env python3
"""
DolphinBench Mock MCP Server

Exposes fake Gmail / Calendar / SMS / CRM / Research tools as MCP tools.
State is pre-populated so tests can check buffer rules, delete-vs-archive,
CC rules, etc. Every tool call is appended to a JSONL log; the harness
reads that log to grade action-memory tests.

Per-run isolation via env vars:
  DOLPHINBENCH_STATE_PATH  -> where to load/save state (defaults to ./state.json)
  DOLPHINBENCH_LOG_PATH    -> where to append tool call log (defaults to ./calls.jsonl)
  DOLPHINBENCH_RUN_ID      -> free-form tag added to every log line
  DOLPHINBENCH_SESSION_ID  -> calling construction session ID
  DOLPHINBENCH_NARRATIVE_DATETIME -> ISO datetime used for construction timestamps

The server reads state at startup and writes it back on every mutation so
the harness can inspect final state after the run.
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import date as calendar_date
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

# Profiles also launch this file directly, outside the repository directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.environment import get_setting

# ------------------------------------------------------------------
# Config via env vars (set by the harness)
# ------------------------------------------------------------------
STATE_PATH = Path(get_setting(
    "DOLPHINBENCH_STATE_PATH",
    str(Path(__file__).parent / "state.json"),
))
LOG_PATH = Path(get_setting(
    "DOLPHINBENCH_LOG_PATH",
    str(Path(__file__).parent / "calls.jsonl"),
))
RUN_ID = get_setting("DOLPHINBENCH_RUN_ID", "adhoc")


# argv overrides for the state/log paths. magent spawns this as a stdio
# subprocess and reliably forwards argv (but NOT per-server env), so parallel
# benchmark workers must isolate their mock state/log via these flags.
def _argv_val(flag: str) -> str | None:
    try:
        if flag in sys.argv:
            i = sys.argv.index(flag)
            if i + 1 < len(sys.argv):
                return sys.argv[i + 1]
    except Exception:
        pass
    return None


_sp = _argv_val("--state-path")
if _sp:
    STATE_PATH = Path(_sp)
_lp = _argv_val("--log-path")
if _lp:
    LOG_PATH = Path(_lp)


# ----------------------------------------------------------------------
# Persona-bound manifest. As of the persona-namespace refactor the
# mock MCP server reads its tool roster and baseline state from a
# per-persona manifest (mock_mcp/manifests/<persona>.yaml). The
# persona is taken from DOLPHINBENCH_PERSONA (required) or the manifest path
# can be supplied explicitly via DOLPHINBENCH_MOCK_MANIFEST.
# ----------------------------------------------------------------------
def _resolve_persona_id() -> str | None:
    """argparse --persona overrides DOLPHINBENCH_PERSONA, both feed the manifest."""
    pid = get_setting("DOLPHINBENCH_PERSONA")
    try:
        # Only treat as a flag when the script is invoked directly. When
        # imported (tests, harness in-process), sys.argv may be unrelated.
        if "--persona" in sys.argv:
            i = sys.argv.index("--persona")
            if i + 1 < len(sys.argv):
                pid = sys.argv[i + 1]
    except Exception:
        pass
    return pid


def _load_manifest() -> dict:
    """Return the persona manifest dict {persona_id, baseline_state, tools}.

    Lookup order:
      1. DOLPHINBENCH_MOCK_MANIFEST env var (absolute or repo-relative path)
      2. mock_mcp/manifests/<DOLPHINBENCH_PERSONA>.yaml
    Missing persona is fatal — we refuse to register any tools without
    a manifest, since the harness contract is "only listed tools active".
    """
    explicit = get_setting("DOLPHINBENCH_MOCK_MANIFEST")
    if explicit:
        path = Path(explicit)
    else:
        pid = _resolve_persona_id()
        if not pid:
            raise RuntimeError(
                "mock_mcp/server.py requires DOLPHINBENCH_PERSONA or DOLPHINBENCH_MOCK_MANIFEST "
                "to be set so it can load the persona's tool manifest."
            )
        path = Path(__file__).parent / "manifests" / f"{pid}.yaml"
    if not path.exists():
        raise RuntimeError(f"Persona manifest not found at {path}")
    import yaml  # local import; mcp host already brings yaml in via deps
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"Persona manifest {path} did not parse to a dict")
    return data


_MANIFEST = _load_manifest()
_MANIFEST_TOOLS: set[str] = set(_MANIFEST.get("tools") or [])
_baseline_field = _MANIFEST.get("baseline_state") or ""
if _baseline_field:
    # Manifest paths are relative to mock_mcp/ (e.g. "state/morgan_baseline.json").
    _baseline = (Path(__file__).parent / _baseline_field).resolve()
else:
    _baseline = Path(__file__).parent.parent / "fixtures" / "baseline_state.json"
BASELINE_PATH = _baseline

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def _load_state() -> dict[str, Any]:
    """Load state from STATE_PATH, falling back to baseline fixture."""
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    if BASELINE_PATH.exists():
        return json.loads(BASELINE_PATH.read_text())
    return {"calendar": [], "inbox": [], "crm": [], "sms_log": [], "sent_emails": []}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


SESSION_MARKER_PATH = get_setting("DOLPHINBENCH_SESSION_MARKER_PATH", "")


def _read_session_marker() -> str:
    """Return the explicit session ID or the marker stamped by the MCP wrapper.

    Returns "" if not configured or unreadable. The marker file is updated
    immediately before each MCP call_tool invocation, so the read here is
    racy only if two sessions in the same magent process call MCP within
    the same microsecond — not a concern for the bench's serial workload.
    """
    explicit_session_id = get_setting("DOLPHINBENCH_SESSION_ID", "").strip()
    if explicit_session_id:
        return explicit_session_id
    if not SESSION_MARKER_PATH:
        return ""
    try:
        return Path(SESSION_MARKER_PATH).read_text().strip()
    except (OSError, FileNotFoundError):
        return ""


def _now_iso() -> str:
    """Return narrative time during construction, otherwise current UTC."""
    narrative_datetime = get_setting("DOLPHINBENCH_NARRATIVE_DATETIME", "").strip()
    if narrative_datetime:
        return narrative_datetime
    return datetime.utcnow().isoformat() + "Z"


def _today_iso() -> str:
    """Return the date component of the active clock."""
    timestamp = _now_iso()
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return timestamp[:10]


def _log_call(tool: str, args: dict, result: Any) -> None:
    """Append a tool call record to the log for grading."""
    entry = {
        "run_id": RUN_ID,
        "ts": _now_iso(),
        "tool": tool,
        "args": args,
        "result": result,
        "session_id": _read_session_marker(),
    }
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# ------------------------------------------------------------------
# MCP server definition
# ------------------------------------------------------------------
mcp = FastMCP("dolphinbench-apps")


# ------------------------------------------------------------------
# Per-test tool scoping
# ------------------------------------------------------------------
# The harness can restrict which tools are exposed to the agent for a
# specific test by writing a JSON config file before triggering an MCP
# reload in magent. This eliminates the context-rot of making every
# test pick from all 17 tools when it only needs 2-3.
#
# Config file (default path below, overridable via DOLPHINBENCH_TOOL_CONFIG_PATH):
#   {"active_tools": ["send_email", "book_flight"]}  → register only these
#   {} or missing                                     → register everything
#
# Reload flow:
#   1. Harness writes /tmp/dolphinbench_mock_config.json
#   2. Harness POSTs /api/mcp/reload to magent
#   3. Magent disconnects and respawns the mock subprocess
#   4. New subprocess reads config at import, registers only listed tools
#   5. Magent re-runs tools/list; agent sees the scoped set only
TOOL_CONFIG_PATH = Path(get_setting(
    "DOLPHINBENCH_TOOL_CONFIG_PATH",
    "/tmp/dolphinbench_mock_config.json",
))


def _load_active_tools() -> set | None:
    """Return the set of tool names to register, or None for 'all'."""
    try:
        if TOOL_CONFIG_PATH.exists():
            cfg = json.loads(TOOL_CONFIG_PATH.read_text())
            active = cfg.get("active_tools")
            if isinstance(active, list):
                return set(active)
    except Exception:
        pass
    return None  # no filter — register everything


_ACTIVE_TOOLS = _load_active_tools()


def conditional_tool():
    """FastMCP tool decorator gated by BOTH the persona manifest and the
    per-test active-tools filter.

    Gating order:
      1. Persona manifest (mock_mcp/manifests/<persona>.yaml) — if the
         tool name isn't listed for this persona, it never registers
         regardless of the per-test config. This is the structural
         partition added by the persona-namespace refactor.
      2. Per-test active_tools filter (legacy) — within the persona's
         allowed set, the harness can further narrow scope per test.

    If a gated-out tool gets called anyway, FastMCP returns its own
    standard "unknown tool" MCP error to the agent — which is what we
    want: unlisted tools must look as if they don't exist.
    """
    def decorator(fn):
        # Persona gate (hard structural filter).
        if _MANIFEST_TOOLS and fn.__name__ not in _MANIFEST_TOOLS:
            return fn
        # Per-test gate (existing soft filter).
        if _ACTIVE_TOOLS is None or fn.__name__ in _ACTIVE_TOOLS:
            return mcp.tool()(fn)
        return fn
    return decorator


# ---- Calendar ----

@conditional_tool()
def list_calendar_events(date: str) -> str:
    """
    List existing calendar events on a given date.

    Args:
        date: ISO date string, e.g. "2026-04-09"

    Returns:
        JSON-encoded list of events that overlap that date. Each event contains only id,
        title, start, end, and attendees, plus body and location when present.
        Returns an empty list if nothing is scheduled.
    """
    state = _load_state()
    requested_date = calendar_date.fromisoformat(date)
    events = []
    for event in state.get("calendar", []):
        if not _calendar_event_overlaps_date(event, requested_date):
            continue
        visible_event = {
            "id": event.get("id", event.get("event_id")),
            "title": event.get("title"),
            "start": event.get("start"),
            "end": event.get("end"),
            "attendees": event.get("attendees"),
        }
        for optional_field in ("body", "location"):
            if optional_field in event:
                visible_event[optional_field] = event[optional_field]
        events.append(visible_event)
    _log_call("list_calendar_events", {"date": date}, {"count": len(events)})
    return json.dumps(events, indent=2)


def _calendar_event_overlaps_date(event: dict[str, Any], requested_date: calendar_date) -> bool:
    """Return whether an event overlaps any instant in the requested local date."""
    try:
        start = datetime.fromisoformat(str(event["start"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(event["end"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False
    day_start = datetime.combine(requested_date, datetime.min.time(), tzinfo=start.tzinfo)
    day_end = day_start + timedelta(days=1)
    return start < day_end and end > day_start


def _has_explicit_tz(ts: str) -> bool:
    """True if timestamp ends with 'Z' or a +HH:MM / -HH:MM offset."""
    if not isinstance(ts, str) or len(ts) < 10:
        return False
    if ts.endswith("Z"):
        return True
    # Look for +HH:MM or -HH:MM in the last 6 chars
    tail = ts[-6:]
    if len(tail) == 6 and tail[0] in ("+", "-") and tail[3] == ":":
        return True
    return False


def _parse_offset_datetime(ts: str) -> datetime | None:
    """Parse an ISO timestamp only when it has an explicit UTC offset."""
    if not _has_explicit_tz(ts):
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


@conditional_tool()
def create_calendar_event(
    title: str,
    start: str,
    end: str,
    attendees: list[str] | None = None,
    body: str | None = None,
) -> str:
    """
    Create a new calendar event. Events may represent meetings, focus blocks,
    reminders, or protected holds; attendees are optional.

    Args:
        title: Event title
        start: ISO datetime with explicit timezone — either UTC with 'Z'
            suffix (e.g. "2026-04-17T22:00:00Z") or with offset
            (e.g. "2026-04-17T15:00:00-07:00"). Naive timestamps rejected.
        end: ISO datetime with explicit timezone (same rules).
        attendees: Optional list of email addresses
        body: Optional event body / description / notes

    Returns:
        JSON {ok, id}, where id is the new calendar event ID, or an error if
        start/end are invalid, the event has no positive duration, or another
        event has the same start and end and shares an attendee.
    """
    if not (_has_explicit_tz(start) and _has_explicit_tz(end)):
        err = {
            "error": "INVALID_TIMEZONE",
            "instruction": (
                "create_calendar_event requires explicit timezone on start "
                "and end. Use ISO-8601 UTC with 'Z' suffix "
                "(e.g. 2026-04-17T22:00:00Z) or with offset "
                "(e.g. 2026-04-17T15:00:00-07:00). Convert from the user's "
                "local timezone before calling."
            ),
        }
        _log_call("create_calendar_event", {
            "title": title, "start": start, "end": end,
            "attendees": attendees or [], "body": body,
        }, err)
        return json.dumps(err)

    start_time = _parse_offset_datetime(start)
    end_time = _parse_offset_datetime(end)
    if start_time is None or end_time is None:
        err = {
            "error": "INVALID_DATETIME",
            "instruction": (
                "create_calendar_event requires valid ISO-8601 start and end "
                "datetimes with explicit timezones."
            ),
        }
        _log_call("create_calendar_event", {
            "title": title, "start": start, "end": end,
            "attendees": attendees or [], "body": body,
        }, err)
        return json.dumps(err)
    if end_time <= start_time:
        err = {
            "error": "INVALID_EVENT_RANGE",
            "instruction": "create_calendar_event requires end to be later than start.",
        }
        _log_call("create_calendar_event", {
            "title": title, "start": start, "end": end,
            "attendees": attendees or [], "body": body,
        }, err)
        return json.dumps(err)

    state = _load_state()
    requested_attendees = set(attendees or [])
    for existing in state.get("calendar", []):
        if not isinstance(existing, dict):
            continue
        existing_start = _parse_offset_datetime(str(existing.get("start") or ""))
        existing_end = _parse_offset_datetime(str(existing.get("end") or ""))
        if existing_start != start_time or existing_end != end_time:
            continue
        if not requested_attendees.intersection(existing.get("attendees") or []):
            continue
        conflict_id = existing.get("id") or existing.get("event_id")
        err = {
            "error": "CALENDAR_EVENT_CONFLICT",
            "instruction": (
                "create_calendar_event: an event with the same start and end "
                f"already exists for an attendee (event id={conflict_id!r})."
            ),
        }
        _log_call("create_calendar_event", {
            "title": title, "start": start, "end": end,
            "attendees": attendees or [], "body": body,
        }, err)
        return json.dumps(err)

    evt = {
        "id": f"evt_{int(time.time()*1000)}",
        "title": title,
        "start": start,
        "end": end,
        "attendees": attendees or [],
    }
    if body is not None:
        evt["body"] = body
    state.setdefault("calendar", []).append(evt)
    _save_state(state)
    _log_call("create_calendar_event", {
        "title": title, "start": start, "end": end,
        "attendees": attendees or [], "body": body,
    }, {"id": evt["id"], "ok": True})
    return json.dumps({"ok": True, "id": evt["id"]})


@conditional_tool()
def update_calendar_event(
    event_id: str,
    start: str | None = None,
    end: str | None = None,
    title: str | None = None,
    attendees: list[str] | None = None,
    body: str | None = None,
) -> str:
    """
    Update an existing calendar event in place. Preserves event_id;
    only mutates the fields you provide. Use this instead of
    cancel-and-recreate when fixing a calendar clash — keeps the
    invitation thread intact and avoids spam-emailing attendees.

    Args:
        event_id: ID of the existing event to update.
        start: Optional new ISO datetime with explicit timezone (Z or
            +HH:MM offset). Naive timestamps rejected.
        end: Optional new ISO datetime with explicit timezone.
        title: Optional new event title.
        attendees: Optional new attendee list (replaces, not merges).
        body: Optional event body / description / notes. Free-form text
            attached to the event (e.g. rationale for a reschedule).
            Persisted on the event under the "body" field.

    Returns:
        JSON {ok: true, event: {...updated...}} on success, or
        {error, instruction} on validation failure / not-found.
    """
    if start is not None and not _has_explicit_tz(start):
        err = {
            "error": "INVALID_TIMEZONE",
            "instruction": (
                "update_calendar_event requires explicit timezone on start "
                "when provided. Use ISO-8601 UTC with 'Z' suffix or with "
                "offset (e.g. 2026-04-17T15:00:00-07:00)."
            ),
        }
        _log_call("update_calendar_event", {
            "event_id": event_id, "start": start, "end": end,
            "title": title, "attendees": attendees, "body": body,
        }, err)
        return json.dumps(err)
    if end is not None and not _has_explicit_tz(end):
        err = {
            "error": "INVALID_TIMEZONE",
            "instruction": (
                "update_calendar_event requires explicit timezone on end "
                "when provided. Use ISO-8601 UTC with 'Z' suffix or with "
                "offset (e.g. 2026-04-17T16:00:00-07:00)."
            ),
        }
        _log_call("update_calendar_event", {
            "event_id": event_id, "start": start, "end": end,
            "title": title, "attendees": attendees, "body": body,
        }, err)
        return json.dumps(err)

    state = _load_state()
    target = None
    for e in state.get("calendar", []):
        if e.get("id") == event_id:
            target = e
            break
    if target is None:
        err = {
            "error": "NOT_FOUND",
            "instruction": (
                f"update_calendar_event: no event with id={event_id!r} in "
                "the calendar. Call list_calendar_events(date) first to "
                "find the event_id."
            ),
        }
        _log_call("update_calendar_event", {
            "event_id": event_id, "start": start, "end": end,
            "title": title, "attendees": attendees, "body": body,
        }, err)
        return json.dumps(err)

    if start is not None:
        target["start"] = start
    if end is not None:
        target["end"] = end
    if title is not None:
        target["title"] = title
    if attendees is not None:
        target["attendees"] = list(attendees)
    if body is not None:
        target["body"] = body
    _save_state(state)
    _log_call("update_calendar_event", {
        "event_id": event_id, "start": start, "end": end,
        "title": title, "attendees": attendees, "body": body,
    }, {"ok": True, "event": target})
    return json.dumps({"ok": True, "event": target})


# ---- Email ----

@conditional_tool()
def list_inbox(folder: str = "inbox", limit: int = 20) -> str:
    """
    List emails in a folder.

    Args:
        folder: "inbox", "archive", or "trash" (default: "inbox")
        limit: Max number of emails to return

    Returns:
        JSON list of emails with id, from, subject, date, category.
    """
    state = _load_state()
    emails = [e for e in state.get("inbox", []) if e.get("folder", "inbox") == folder]
    emails.sort(key=lambda e: e.get("date", ""), reverse=True)
    emails = emails[:limit]
    _log_call("list_inbox", {"folder": folder, "limit": limit}, {"count": len(emails)})
    return json.dumps(emails, indent=2)


@conditional_tool()
def send_email(
    to: str,
    subject: str,
    body: str,
    cc: list[str] | None = None,
) -> str:
    """
    Send a new email on the user's behalf.

    This tool creates a new email only. It has no thread ID, message ID, or
    reply capability, so it cannot send an in-thread reply.

    Args:
        to: Recipient email address or unambiguous contact identity
        subject: Email subject line
        body: Full email body text, written from the user's point of view
            because this tool sends from the user's account. Include any
            signature you need.
        cc: Optional list of CC recipients. A person listed here is not the
            sender.

    Returns:
        JSON confirmation with a message ID.
    """
    state = _load_state()
    msg = {
        "id": f"sent_{int(time.time()*1000)}",
        "to": to,
        "subject": subject,
        "body": body,
        "cc": cc or [],
        "sent_at": _now_iso(),
    }
    state.setdefault("sent_emails", []).append(msg)
    _save_state(state)
    _log_call("send_email", {
        "to": to, "subject": subject, "body": body, "cc": cc or [],
    }, {"id": msg["id"], "ok": True})
    return json.dumps({"ok": True, "id": msg["id"]})


@conditional_tool()
def archive_email(email_id: str) -> str:
    """
    Archive an email (move from inbox to archive). Non-destructive.

    Args:
        email_id: ID of the email to archive (from list_inbox)

    Returns:
        JSON confirmation.
    """
    state = _load_state()
    hit = False
    for e in state.get("inbox", []):
        if e.get("id") == email_id:
            e["folder"] = "archive"
            hit = True
            break
    _save_state(state)
    _log_call("archive_email", {"email_id": email_id}, {"ok": hit})
    return json.dumps({"ok": hit})


@conditional_tool()
def delete_email(email_id: str) -> str:
    """
    Permanently delete an email. Destructive and irreversible.

    Args:
        email_id: ID of the email to delete (from list_inbox)

    Returns:
        JSON confirmation.
    """
    state = _load_state()
    before = len(state.get("inbox", []))
    state["inbox"] = [e for e in state.get("inbox", []) if e.get("id") != email_id]
    hit = len(state["inbox"]) < before
    _save_state(state)
    _log_call("delete_email", {"email_id": email_id}, {"ok": hit})
    return json.dumps({"ok": hit})


# ---- SMS ----

@conditional_tool()
def send_sms(to: str, body: str) -> str:
    """
    Send an SMS text message.

    Args:
        to: Phone number in E.164 format or an unambiguous contact identity such as "Mom"
        body: Message body (max 160 chars recommended)

    Returns:
        JSON confirmation.
    """
    state = _load_state()
    msg = {
        "id": f"sms_{int(time.time()*1000)}",
        "to": to,
        "body": body,
        "sent_at": _now_iso(),
    }
    state.setdefault("sms_log", []).append(msg)
    _save_state(state)
    _log_call("send_sms", {"to": to, "body": body}, {"id": msg["id"], "ok": True})
    return json.dumps({"ok": True, "id": msg["id"]})


# ---- CRM ----

@conditional_tool()
def add_crm_row(
    name: str,
    company: str,
    notes: str = "",
    tags: list[str] | None = None,
) -> str:
    """
    Add a new person to the CRM.

    Args:
        name: Full name
        company: Company or affiliation
        notes: Free-text context
        tags: Optional list of tags (e.g. ["sf-apr26", "ai"])

    Returns:
        JSON confirmation with the new row ID.
    """
    state = _load_state()
    row = {
        "id": f"crm_{int(time.time()*1000)}",
        "name": name,
        "company": company,
        "notes": notes,
        "tags": tags or [],
        "added_at": _now_iso(),
    }
    state.setdefault("crm", []).append(row)
    _save_state(state)
    _log_call("add_crm_row", {
        "name": name, "company": company, "notes": notes, "tags": tags or [],
    }, {"id": row["id"], "ok": True})
    return json.dumps({"ok": True, "id": row["id"]})


# ---- Research / Stock data ----

@conditional_tool()
def get_stock_data(ticker: str) -> str:
    """
    Fetch current market data for a stock ticker.

    Args:
        ticker: Stock symbol, e.g. "NVDA", "AAPL"

    Returns:
        JSON with price, 52-week range, P/E ratio, analyst consensus,
        market cap. Data is mocked for testing.
    """
    # Deterministic mock data per ticker
    mock = {
        "NVDA": {"price": 892.50, "52w_high": 950.02, "52w_low": 394.17,
                 "pe_ratio": 68.4, "analyst_consensus": "Strong Buy",
                 "market_cap": 2_193_000_000_000},
        "AAPL": {"price": 189.25, "52w_high": 199.62, "52w_low": 164.08,
                 "pe_ratio": 28.7, "analyst_consensus": "Buy",
                 "market_cap": 2_880_000_000_000},
        "TSLA": {"price": 178.40, "52w_high": 299.29, "52w_low": 138.80,
                 "pe_ratio": 45.3, "analyst_consensus": "Hold",
                 "market_cap": 567_000_000_000},
    }
    data = mock.get(ticker.upper(), {
        "price": 100.00, "52w_high": 120.00, "52w_low": 80.00,
        "pe_ratio": 25.0, "analyst_consensus": "Hold",
        "market_cap": 50_000_000_000,
    })
    data["ticker"] = ticker.upper()
    _log_call("get_stock_data", {"ticker": ticker}, data)
    return json.dumps(data, indent=2)


# ---- Messaging (Discord / Slack) ----

@conditional_tool()
def send_discord_message(channel: str, message: str) -> str:
    """
    Send a message to a Discord channel.

    Args:
        channel: Channel name, e.g. "#general", "#eng-team"
        message: Message body

    Returns:
        JSON confirmation.
    """
    state = _load_state()
    msg = {
        "id": f"discord_{int(time.time()*1000)}",
        "channel": channel,
        "message": message,
        "sent_at": _now_iso(),
    }
    state.setdefault("discord_log", []).append(msg)
    _save_state(state)
    _log_call("send_discord_message", {"channel": channel, "message": message},
              {"id": msg["id"], "ok": True})
    return json.dumps({"ok": True, "id": msg["id"]})


@conditional_tool()
def send_slack_message(channel: str, message: str) -> str:
    """
    Send a message to a Slack channel.

    Args:
        channel: Channel name, e.g. "#general", "#eng-team"
        message: Message body

    Returns:
        JSON confirmation.
    """
    state = _load_state()
    msg = {
        "id": f"slack_{int(time.time()*1000)}",
        "channel": channel,
        "message": message,
        "sent_at": _now_iso(),
    }
    state.setdefault("slack_log", []).append(msg)
    _save_state(state)
    _log_call("send_slack_message", {"channel": channel, "message": message},
              {"id": msg["id"], "ok": True})
    return json.dumps({"ok": True, "id": msg["id"]})


@conditional_tool()
def send_slack_dm(user: str, message: str) -> str:
    """
    Send a direct (private) message to a specific Slack user.

    Use this for 1:1 conversations with a specific person, rather than
    posting to a public channel. Example: DMing a colleague to ask a
    scheduling question.

    Args:
        user: Slack user handle, e.g. "@priya", "@jake", "priya", "U01ABC"
        message: Message body

    Returns:
        JSON confirmation.
    """
    state = _load_state()
    msg = {
        "id": f"slack_dm_{int(time.time()*1000)}",
        "user": user,
        "message": message,
        "sent_at": _now_iso(),
    }
    state.setdefault("slack_dm_log", []).append(msg)
    _save_state(state)
    _log_call("send_slack_dm", {"user": user, "message": message},
              {"id": msg["id"], "ok": True})
    return json.dumps({"ok": True, "id": msg["id"]})


@conditional_tool()
def send_discord_dm(user: str, message: str) -> str:
    """
    Send a direct (private) message to a specific Discord user.

    Use this for 1:1 conversations with a specific person, rather than
    posting to a channel.

    Args:
        user: Discord user handle, e.g. "@priya", "@jake"
        message: Message body

    Returns:
        JSON confirmation.
    """
    state = _load_state()
    msg = {
        "id": f"discord_dm_{int(time.time()*1000)}",
        "user": user,
        "message": message,
        "sent_at": _now_iso(),
    }
    state.setdefault("discord_dm_log", []).append(msg)
    _save_state(state)
    _log_call("send_discord_dm", {"user": user, "message": message},
              {"id": msg["id"], "ok": True})
    return json.dumps({"ok": True, "id": msg["id"]})


# ---- Invoice processing ----

@conditional_tool()
def process_payment(
    invoice_id: str,
    amount: float,
    vendor: str,
    priority: str | None = None,
) -> str:
    """
    Process payment for an invoice. Sends funds to the vendor.
    This action is IRREVERSIBLE — funds are transferred immediately.

    Args:
        invoice_id: Invoice reference number
        amount: Payment amount in USD
        vendor: Vendor/company name
        priority: Optional priority hint ('high' or 'urgent') —
            required for invoices over $3000.

    Returns:
        JSON confirmation with transaction ID, or error if validation fails.
    """
    if amount > 3000 and priority not in ("high", "urgent"):
        err = {
            "error": "high_value_requires_priority",
            "instruction": (
                "Invoices over $3000 require priority='high' or 'urgent'. "
                "Re-issue the call with the priority field set."
            ),
        }
        _log_call("process_payment", {
            "invoice_id": invoice_id, "amount": amount, "vendor": vendor,
            "priority": priority,
        }, err)
        return json.dumps(err)

    state = _load_state()
    txn = {
        "id": f"pay_{int(time.time()*1000)}",
        "invoice_id": invoice_id,
        "amount": amount,
        "vendor": vendor,
        "priority": priority,
        "status": "completed",
        "processed_at": _now_iso(),
    }
    state.setdefault("payments", []).append(txn)
    _save_state(state)
    _log_call("process_payment", {
        "invoice_id": invoice_id, "amount": amount, "vendor": vendor,
        "priority": priority,
    }, {"id": txn["id"], "ok": True})
    return json.dumps({"ok": True, "id": txn["id"], "status": "completed"})


@conditional_tool()
def flag_for_review(
    invoice_id: str,
    amount: float,
    vendor: str,
    reason: str = "",
) -> str:
    """
    Flag an invoice for manual review instead of auto-processing.
    Use when an invoice requires human approval before payment.

    Args:
        invoice_id: Invoice reference number
        amount: Invoice amount in USD
        vendor: Vendor/company name
        reason: Why this needs manual review

    Returns:
        JSON confirmation.
    """
    state = _load_state()
    flag = {
        "id": f"flag_{int(time.time()*1000)}",
        "invoice_id": invoice_id,
        "amount": amount,
        "vendor": vendor,
        "reason": reason,
        "flagged_at": _now_iso(),
    }
    state.setdefault("review_flags", []).append(flag)
    _save_state(state)
    _log_call("flag_for_review", {
        "invoice_id": invoice_id, "amount": amount,
        "vendor": vendor, "reason": reason,
    }, {"id": flag["id"], "ok": True})
    return json.dumps({"ok": True, "id": flag["id"]})


# ---- Food ordering ----

@conditional_tool()
def place_order(
    restaurant: str,
    items: list[str],
    notes: str = "",
) -> str:
    """
    Place a food/drink order from a restaurant or cafe.

    Args:
        restaurant: Restaurant or cafe name
        items: List of items to order
        notes: Special instructions or customizations

    Returns:
        JSON confirmation with order ID and ETA.
    """
    # NB: the "the usual"/placeholder regex rejection was removed — it was a
    # keyword stand-in for "did the agent resolve the order from memory". Every
    # place_order test grades the items directly (a value/judge check), so a punt
    # like "the usual" fails the grader on its own; the tool no longer second-guesses.
    if not items:
        err = {
            "error": "UNSPECIFIED_ITEMS",
            "instruction": "place_order needs at least one item.",
        }
        _log_call("place_order", {"restaurant": restaurant, "items": items}, err)
        return json.dumps(err)
    state = _load_state()
    order = {
        "id": f"order_{int(time.time()*1000)}",
        "restaurant": restaurant,
        "items": items,
        "notes": notes,
        "ordered_at": _now_iso(),
    }
    state.setdefault("orders", []).append(order)
    _save_state(state)
    _log_call("place_order", {
        "restaurant": restaurant, "items": items, "notes": notes,
    }, {"id": order["id"], "ok": True})
    return json.dumps({"ok": True, "id": order["id"], "eta_minutes": 15})


# ---- Project status ----

@conditional_tool()
def check_project_status(project_name: str) -> str:
    """
    Check the current status of a project or workstream.

    Args:
        project_name: Project name or codename (e.g. "atlas", "mercury",
            "design"). Pass "all" or "this week" or "weekly" to get a
            roll-up across every active workstream — useful when posting
            weekly ship updates to per-team channels.

    Returns:
        JSON with project status including progress, recent milestones,
        blockers, team lead, and channel routing (working_channel +
        platform = "discord" or "slack"). The channel/platform fields
        tell you where each team actually hangs out so per-team ship
        posts go to the right place. Data is mocked for testing.
    """
    state = _load_state()
    projects = state.get("projects", {})
    if isinstance(projects, dict):
        project_items = [(str(key), value) for key, value in projects.items()]
    elif isinstance(projects, list):
        project_items = [("", value) for value in projects]
    else:
        project_items = []

    def normalized_name(value: str) -> str:
        value = value.strip().casefold()
        if value.startswith("project "):
            value = value.removeprefix("project ").strip()
        return value

    key = normalized_name(project_name)
    # Roll-up mode: "all" or "this week" / "weekly" returns the full
    # workstream set so the agent sees every team that shipped.
    rollup_aliases = {
        "*",
        "all",
        "all projects",
        "all workstreams",
        "roll-up",
        "rollup",
        "ship",
        "shipped",
        "this week",
        "weekly",
    }
    if key in rollup_aliases:
        workstreams = [
            dict(value) for _, value in project_items if isinstance(value, dict)
        ]
        rollup = {
            "workstreams": workstreams,
            "summary": f"{len(workstreams)} project records available.",
        }
        _log_call("check_project_status", {"project_name": project_name}, rollup)
        return json.dumps(rollup, indent=2)

    data = None
    for stored_key, record in project_items:
        if not isinstance(record, dict):
            continue
        candidate_names = {normalized_name(stored_key)} if stored_key else set()
        project_label = record.get("project") or record.get("name")
        if isinstance(project_label, str):
            candidate_names.add(normalized_name(project_label))
        if key in candidate_names:
            data = dict(record)
            break
    if data is None:
        data = {
            "project": project_name,
            "status": "unknown",
            "error": "No project with that exact name exists in the current state.",
        }
    _log_call("check_project_status", {"project_name": project_name}, data)
    return json.dumps(data, indent=2)


# ---- Venue search (scoped tool, typically only for offsite-planning tests) ----

@conditional_tool()
def search_venues(
    location_radius_miles: int = 100,
    capacity: int = 12,
    nights: int = 3,
    max_price_per_night_usd: float = 0.0,
) -> str:
    """
    Search the corporate venue database for offsite/retreat venues that
    fit the team's needs. Returns venues with capacity, price, location,
    and amenity attributes the agent can filter against.

    Args:
        location_radius_miles: How far from the SF Bay Area to search.
        capacity: Number of attendees the venue must accommodate.
        nights: Trip duration in nights.
        max_price_per_night_usd: Optional cap on per-night price. 0 = no cap.

    Returns:
        JSON list of venues, each with name, location, capacity,
        price_per_night, wifi (bool), amenities (list).
    """
    venues = [
        {
            "name": "Sonoma Lodge",
            "location": "Sonoma, CA",
            "capacity": 20,
            "price_per_night": 3500.00,
            "wifi": False,
            "amenities": ["pool", "fire pit", "private chef", "vineyard tours"],
        },
        {
            "name": "Tahoe House",
            "location": "South Lake Tahoe, CA",
            "capacity": 16,
            "price_per_night": 4800.00,
            "wifi": True,
            "amenities": ["wifi", "indoor meeting space", "lake access", "kitchen"],
        },
        {
            "name": "Half Moon Bay Ridge",
            "location": "Half Moon Bay, CA",
            "capacity": 14,
            "price_per_night": 5200.00,
            "wifi": True,
            "amenities": ["wifi", "ocean view", "conference room", "spa"],
        },
        {
            "name": "Calistoga Springs Retreat",
            "location": "Calistoga, CA",
            "capacity": 12,
            "price_per_night": 4200.00,
            "wifi": True,
            "amenities": ["wifi", "hot springs", "meeting rooms", "private chef"],
        },
        {
            "name": "Big Sur Cliff House",
            "location": "Big Sur, CA",
            "capacity": 18,
            "price_per_night": 6500.00,
            "wifi": False,
            "amenities": ["ocean view", "hiking", "fire pit"],
        },
        {
            "name": "Carmel Valley Inn",
            "location": "Carmel Valley, CA",
            "capacity": 24,
            "price_per_night": 3800.00,
            "wifi": True,
            "amenities": ["wifi", "tennis courts", "conference center", "pool"],
        },
        {
            "name": "Santa Cruz Mountain Lodge",
            "location": "Santa Cruz Mountains, CA",
            "capacity": 20,
            "price_per_night": 2900.00,
            "wifi": True,
            "amenities": ["wifi", "redwood forest", "meeting hall"],
        },
    ]

    matched = [v for v in venues if v["capacity"] >= capacity]
    if max_price_per_night_usd > 0:
        matched = [v for v in matched if v["price_per_night"] <= max_price_per_night_usd]

    args = {
        "location_radius_miles": location_radius_miles,
        "capacity": capacity,
        "nights": nights,
        "max_price_per_night_usd": max_price_per_night_usd,
    }
    _log_call("search_venues", args, {"count": len(matched)})
    return json.dumps(matched, indent=2)


# ---- Travel (scoped tool, typically only active for travel-booking tests) ----

@conditional_tool()
def book_flight(
    origin: str,
    destination: str,
    outbound_date: str,
    return_date: str,
    airline: str = "",
    seat_preference: str = "any",
    max_price_usd: float = 0.0,
    price: float = 0.0,
    passenger_name: str = "",
    trip_purpose: str = "",
) -> str:
    """
    Book a flight via the company travel desk. The desk handles payment,
    confirmations, and logistics — this is the canonical way to book
    business travel for the user.

    Args:
        origin: IATA airport code, e.g. "SFO"
        destination: IATA airport code, e.g. "JFK" or "LGA"
        outbound_date: ISO date for outbound flight, e.g. "2026-04-20"
        return_date: ISO date for return flight
        airline: Preferred airline, e.g. "Delta". Leave empty to let the desk pick.
        seat_preference: One of "aisle", "window", "middle", "any". Default "any".
        max_price_usd: Total trip budget cap in USD (flights + fees).
                       0 means no cap specified.
        price: Optional fare price for the booking in USD (the actual
               quoted fare, not the cap). Same units as max_price_usd.
               Either field can be used to express the per-traveler cost.
        passenger_name: Name of the traveler.
        trip_purpose: Short note on why the trip is happening.

    Returns:
        JSON confirmation with booking_id, status, and price.
    """
    booking_id = f"bk_{int(time.time() * 1000)}"
    # If only max_price_usd is set, mirror it as price; if only price is
    # set, mirror it as max_price_usd. Lets graders check either field.
    effective_price = price if price else max_price_usd
    effective_cap = max_price_usd if max_price_usd else price
    args = {
        "origin": origin,
        "destination": destination,
        "outbound_date": outbound_date,
        "return_date": return_date,
        "airline": airline,
        "seat_preference": seat_preference,
        "max_price_usd": effective_cap,
        "price": effective_price,
        "passenger_name": passenger_name,
        "trip_purpose": trip_purpose,
    }
    response = {
        "ok": True,
        "booking_id": booking_id,
        "price": effective_price,
        "status": "confirmed_pending_traveler_review",
    }
    _log_call("book_flight", args, response)
    return json.dumps(response)


# ---- IMP-style mock tools (procedural-learning fixtures) ----

@conditional_tool()
def batch_dedup(items: list[str]) -> str:
    """
    Deduplicate a list of items using a streaming comparator. Requires
    items to be pre-sorted ascending — the streaming comparator only
    detects adjacent duplicates.

    Args:
        items: List of strings to deduplicate, must be sorted ascending.

    Returns:
        JSON with unique_count and unique items, or error if input unsorted.
    """
    if items != sorted(items):
        err = {
            "error": "UNSORTED_INPUT",
            "instruction": (
                "batch_dedup uses a streaming comparator and requires items "
                "pre-sorted ascending. Sort the input then retry."
            ),
        }
        _log_call("batch_dedup", {"items": items}, err)
        return json.dumps(err)
    unique = []
    for x in items:
        if not unique or unique[-1] != x:
            unique.append(x)
    result = {"unique_count": len(unique), "unique": unique}
    _log_call("batch_dedup", {"items": items}, result)
    return json.dumps(result)


@conditional_tool()
def embeddings_encode(text: str) -> str:
    """
    Encode a text string into an embedding vector. Required as a
    pre-step for vector_search.

    Args:
        text: The text to encode.

    Returns:
        JSON with the embedding vector (deterministic stub for testing).
    """
    # Deterministic stub vector based on hash for reproducibility
    h = hashlib.sha256(text.encode()).digest()
    vec = [b / 255.0 for b in h[:8]]
    result = {"vector": vec, "dim": len(vec)}
    _log_call("embeddings_encode", {"text": text}, {"dim": len(vec)})
    return json.dumps(result)


@conditional_tool()
def vector_search(
    query_vec: list[float] | None = None,
    namespace: str = "",
    query: str | None = None,
    top_k: int = 5,
) -> str:
    """
    Search the vector store for results similar to query_vec.

    Args:
        query_vec: Embedding vector — REQUIRED. Get this by calling
            embeddings_encode(text) first. Raw query strings are NOT
            accepted; you must encode first.
        namespace: Vector namespace to search in (user-specific).
        query: DEPRECATED. Do not pass raw query strings; use query_vec.
        top_k: Number of results to return.

    Returns:
        JSON with results, or error if query_vec is missing.
    """
    if query and not query_vec:
        err = {
            "error": "RAW_QUERY_REJECTED",
            "instruction": (
                "vector_search expects an embedding vector in 'query_vec'. "
                "Call embeddings_encode(text) first and pass the result."
            ),
        }
        _log_call("vector_search", {
            "query": query, "query_vec": None, "namespace": namespace,
        }, err)
        return json.dumps(err)
    if not query_vec:
        err = {
            "error": "MISSING_QUERY_VEC",
            "instruction": "vector_search requires a 'query_vec' parameter.",
        }
        _log_call("vector_search", {"namespace": namespace}, err)
        return json.dumps(err)
    # Deterministic stub results
    results = [
        {"id": f"{namespace or 'default'}-{i}", "score": 0.9 - i * 0.1,
         "snippet": f"Stub result {i} for namespace={namespace}"}
        for i in range(min(top_k, 3))
    ]
    _log_call("vector_search", {
        "query_vec_dim": len(query_vec), "namespace": namespace, "top_k": top_k,
    }, {"count": len(results)})
    return json.dumps({"results": results})


@conditional_tool()
def gh_pr_create(
    title: str,
    body: str,
    branch: str = "",
    base: str = "main",
) -> str:
    """
    Create a GitHub pull request.

    Repo policy requires:
    - The body must reference a Linear ticket id matching ENG-\\d+.
    - The body must include a '## Test plan' heading.

    Args:
        title: PR title.
        body: PR body in markdown. Must contain Linear ticket and Test plan.
        branch: Source branch (head). Optional.
        base: Target branch (default 'main').

    Returns:
        JSON with pr_url, or error if validation fails.
    """
    has_ticket = bool(re.search(r"ENG-\d+", body))
    has_test_plan = "## Test plan" in body
    if not (has_ticket and has_test_plan):
        missing = []
        if not has_ticket:
            missing.append("Linear ticket id matching ENG-\\d+")
        if not has_test_plan:
            missing.append("'## Test plan' section")
        err = {
            "error": "PR_VALIDATION_FAILED",
            "instruction": (
                f"Repo policy requires the PR body to include: "
                f"{', and '.join(missing)}. Update body and retry."
            ),
        }
        _log_call("gh_pr_create", {
            "title": title, "branch": branch, "base": base,
            "body": body, "body_len": len(body),
        }, err)
        return json.dumps(err)
    pr_id = int(time.time() * 1000) % 100000
    result = {"ok": True, "pr_url": f"https://github.com/example/repo/pull/{pr_id}"}
    _log_call("gh_pr_create", {
        "title": title, "branch": branch, "base": base, "body": body, "body_len": len(body),
    }, result)
    return json.dumps(result)


# Per-process rolling window for metrics_fetch rate limiting
_METRICS_FETCH_WINDOW: list[float] = []
_METRICS_FETCH_LIMIT_PER_MIN = 3


@conditional_tool()
def metrics_fetch(service: str) -> str:
    """
    Fetch metrics for a single service.

    NOTE: This endpoint enforces a 3-concurrent-call/minute rate limit.
    For multi-service queries, use metrics_fetch_batch to avoid the cap.

    Args:
        service: The service name to fetch metrics for.

    Returns:
        JSON with metrics, or error if rate-limited.
    """
    global _METRICS_FETCH_WINDOW
    now = time.time()
    _METRICS_FETCH_WINDOW = [t for t in _METRICS_FETCH_WINDOW if now - t < 60]
    if len(_METRICS_FETCH_WINDOW) >= _METRICS_FETCH_LIMIT_PER_MIN:
        err = {
            "error": "RATE_LIMITED",
            "retry_after_seconds": 60,
            "instruction": (
                f"metrics_fetch caps at {_METRICS_FETCH_LIMIT_PER_MIN} "
                "concurrent calls per minute per user. "
                "For multi-service queries, use metrics_fetch_batch "
                "(takes a list of services, no cap)."
            ),
        }
        _log_call("metrics_fetch", {"service": service}, err)
        return json.dumps(err)
    _METRICS_FETCH_WINDOW.append(now)
    result = {
        "service": service,
        "metrics": {"cpu_pct": 42.0, "mem_pct": 65.5, "rps": 120},
    }
    _log_call("metrics_fetch", {"service": service}, {"ok": True})
    return json.dumps(result)


@conditional_tool()
def metrics_fetch_batch(services: list[str]) -> str:
    """
    Fetch metrics for multiple services in one call. No rate limit.

    Args:
        services: List of service names.

    Returns:
        JSON keyed by service name with metrics for each.
    """
    result = {
        s: {"cpu_pct": 42.0, "mem_pct": 65.5, "rps": 120}
        for s in services
    }
    _log_call("metrics_fetch_batch", {"services": services},
              {"count": len(services)})
    return json.dumps({"results": result})


@conditional_tool()
def shell_run(command: str) -> str:
    """
    Run a shell command. The command is recorded but NOT actually executed
    against the host filesystem — this is a sandboxed mock for testing
    procedural-learning behaviors (e.g. preferring `rg` over `grep`).

    Args:
        command: The full shell command line, e.g. 'rg "TODO" ~/dotfiles'.

    Returns:
        JSON with a stub stdout, the recorded command, and ok=True.
    """
    result = {
        "ok": True,
        "command": command,
        "stdout": f"[mock shell] recorded command: {command}",
        "exit_code": 0,
    }
    _log_call("shell_run", {"command": command}, {"ok": True})
    return json.dumps(result)


# ---- IMP-G fixtures: tool-substitution learning (legacy vs current) ----
# Two MCP-only tool variants the agent can be told to prefer one over.
# Used by IMP-G to test whether a learned preference (use v2 not v1)
# survives across sessions. v1 always succeeds with a deprecation warning;
# v2 always succeeds clean. Memory test: did the agent learn to skip v1?

@conditional_tool()
def internal_search_v1_legacy(query: str) -> str:
    """
    LEGACY internal knowledge-base search (v1).

    NOTE: v1 returns stale results (snapshot from before 2026-04-01)
    and is being decommissioned. Prefer internal_search_v2 for current
    results.

    Args:
        query: Search query string.

    Returns:
        JSON with a deprecation warning and stub (stale) results.
    """
    result = {
        "warning": (
            "internal_search_v1_legacy returns stale results from a "
            "snapshot before 2026-04-01 and is being decommissioned. "
            "Use internal_search_v2 for current results."
        ),
        "results": [
            {"title": f"[STALE] doc matching '{query}'", "snapshot_date": "2026-03-15"},
        ],
        "stale": True,
    }
    _log_call("internal_search_v1_legacy", {"query": query},
              {"ok": True, "stale": True})
    return json.dumps(result)


@conditional_tool()
def internal_search_v2(query: str) -> str:
    """
    Current internal knowledge-base search (v2). Use this — v1 is being
    decommissioned and returns stale results.

    Args:
        query: Search query string.

    Returns:
        JSON with current stub results.
    """
    result = {
        "results": [
            {"title": f"Result for '{query}'",
             "snippet": "Current doc content from v2 index.",
             "updated": "2026-04-22"},
        ],
    }
    _log_call("internal_search_v2", {"query": query}, {"ok": True})
    return json.dumps(result)


# ---- IMP-H fixtures: deployment procedure deprecation ----
# kubectl-based deployment ALWAYS errors with DEPRECATED instruction.
# pipeline-based deployment always works. Memory test: did the agent
# learn (during execution-seed) that kubectl is dead and use pipeline
# on first call during the test phase?

@conditional_tool()
def deploy_via_kubectl(branch: str, env: str = "staging") -> str:
    """
    LEGACY deployment via direct kubectl apply.

    Returns a DEPRECATED error pointing to deploy_via_pipeline.
    This path was retired on 2026-04-13.

    Args:
        branch: Source git branch to deploy.
        env: Target environment ('staging' or 'prod').

    Returns:
        JSON error with deprecation instructions.
    """
    err = {
        "error": "DEPRECATED",
        "instruction": (
            "deploy_via_kubectl was retired on 2026-04-13. Use "
            "deploy_via_pipeline(branch, env) instead — it handles both "
            "staging and prod through the same orchestrator."
        ),
    }
    _log_call("deploy_via_kubectl", {"branch": branch, "env": env}, err)
    return json.dumps(err)


@conditional_tool()
def deploy_via_pipeline(branch: str, env: str = "staging") -> str:
    """
    Current deployment path. Pushes the branch through the pipeline
    orchestrator which handles build, test, and rollout.

    Args:
        branch: Source git branch to deploy.
        env: Target environment ('staging' or 'prod').

    Returns:
        JSON confirmation with deployment_id.
    """
    deployment_id = f"dep_{int(time.time() * 1000)}"
    result = {
        "ok": True,
        "deployment_id": deployment_id,
        "branch": branch,
        "env": env,
        "status": "queued",
    }
    _log_call("deploy_via_pipeline", {"branch": branch, "env": env},
              {"ok": True, "deployment_id": deployment_id})
    return json.dumps(result)


# ---- PRs / code review ----

@conditional_tool()
def list_open_prs(
    repo: str | None = None,
    author: str | None = None,
    assignee: str | None = None,
    status: str | None = None,
) -> str:
    """
    List pull requests, optionally filtered. Reads from both the
    `prs` and `open_prs` state keys so personas with either shape
    work transparently.

    Args:
        repo: Optional repo name filter — e.g. "metrics-router",
            "atlas", "mercury". If omitted, returns PRs across all repos.
        author: Optional author handle filter (e.g. "alex", "nadia").
        assignee: Optional assignee handle filter — matches if the
            handle appears in the PR's `assignees` list or `reviewers`
            list (treated equivalently for engineer-PR retrieval).
        status: Optional status filter. Defaults to "open" if none
            of the other filters are set; pass status=None explicitly
            to disable the implicit open-only filter.

    Returns:
        JSON list of {id, title, author, repo, branch, status,
        opened_at, age_days, ...}. Empty list if nothing matches.
    """
    state = _load_state()
    raw = list(state.get("prs", [])) + list(state.get("open_prs", []))
    # Default to status=open when no other narrowing filter is set.
    if status is None and not (author or assignee or repo):
        status_filter = "open"
    else:
        status_filter = status
    prs = raw
    if status_filter is not None:
        prs = [p for p in prs if p.get("status") == status_filter]
    if repo:
        prs = [p for p in prs if p.get("repo") == repo]
    if author:
        prs = [p for p in prs if p.get("author") == author]
    if assignee:
        prs = [
            p for p in prs
            if assignee in (p.get("assignees") or [])
            or assignee in (p.get("reviewers") or [])
        ]
    _log_call(
        "list_open_prs",
        {"repo": repo, "author": author, "assignee": assignee, "status": status},
        {"count": len(prs)},
    )
    return json.dumps(prs, indent=2)


# ---- Engineer-shape PR / oncall / incident / runbook / deploy / observability ----
# Added for Alex's persona; Morgan's manifest doesn't list any of these so
# they're persona-gated off for Morgan. Tools live in server.py (not split
# into a separate module) to match the existing per-tool pattern.


@conditional_tool()
def get_pr(pr_id: str) -> str:
    """
    Fetch the full record for a single pull request.

    Args:
        pr_id: PR identifier (e.g. "metrics-router#412").

    Returns:
        JSON object describing the PR, or {error} if not found.
    """
    state = _load_state()
    raw = list(state.get("prs", [])) + list(state.get("open_prs", []))
    for p in raw:
        if p.get("id") == pr_id:
            _log_call("get_pr", {"pr_id": pr_id}, {"ok": True})
            return json.dumps(p, indent=2)
    err = {"error": "NOT_FOUND", "instruction": f"No PR with id={pr_id!r}."}
    _log_call("get_pr", {"pr_id": pr_id}, err)
    return json.dumps(err)


@conditional_tool()
def review_pr(
    pr_id: str,
    decision: str,
    body: str | None = None,
) -> str:
    """
    Submit a review on a pull request.

    Args:
        pr_id: Supplied PR identifier, such as "metrics-router#412" or a PR URL.
            The PR does not need to be looked up first or already exist in app
            state when the supplied material contains what must be reviewed.
        decision: One of "approve", "request_changes", "comment".
        body: Optional review comment body (markdown).

    Returns:
        JSON {ok, review_id, decision} on success, or {error} on
        invalid decision.
    """
    allowed = {"approve", "request_changes", "comment"}
    if decision not in allowed:
        err = {
            "error": "INVALID_DECISION",
            "instruction": (
                "review_pr decision must be one of 'approve', "
                "'request_changes', or 'comment'."
            ),
        }
        _log_call(
            "review_pr",
            {"pr_id": pr_id, "decision": decision, "body": body},
            err,
        )
        return json.dumps(err)
    state = _load_state()
    pull_requests = list(state.get("prs", [])) + list(state.get("open_prs", []))
    pull_request = next(
        (pr for pr in pull_requests if pr.get("id") == pr_id),
        None,
    )
    status = str((pull_request or {}).get("status", "")).casefold()
    if status in {"merged", "closed"}:
        err = {
            "error": "PR_NOT_OPEN",
            "instruction": (
                f"Cannot review or comment on {pr_id!r} because its current "
                f"status is {status!r}."
            ),
        }
        _log_call(
            "review_pr",
            {"pr_id": pr_id, "decision": decision, "body": body},
            err,
        )
        return json.dumps(err)
    review = {
        "id": f"rev_{int(time.time()*1000)}",
        "pr_id": pr_id,
        "decision": decision,
        "body": body or "",
        "reviewed_at": _now_iso(),
    }
    state.setdefault("pr_reviews", []).append(review)
    _save_state(state)
    _log_call(
        "review_pr",
        {"pr_id": pr_id, "decision": decision, "body": body},
        {"ok": True, "review_id": review["id"]},
    )
    return json.dumps({"ok": True, "review_id": review["id"], "decision": decision})


@conditional_tool()
def merge_pr(pr_id: str, method: str = "squash") -> str:
    """
    Merge a pull request.

    Args:
        pr_id: PR identifier (e.g. "metrics-router#412").
        method: Merge strategy — one of "squash", "merge", "rebase".
            Default "squash".

    Returns:
        JSON {ok, pr_id, method, merged_at} on success.
    """
    allowed = {"squash", "merge", "rebase"}
    if method not in allowed:
        err = {
            "error": "INVALID_MERGE_METHOD",
            "instruction": (
                "merge_pr method must be one of 'squash', 'merge', 'rebase'."
            ),
        }
        _log_call("merge_pr", {"pr_id": pr_id, "method": method}, err)
        return json.dumps(err)
    state = _load_state()
    # Best-effort: flip the matching PR's status to merged if present.
    for bucket in ("prs", "open_prs"):
        for p in state.get(bucket, []):
            if p.get("id") == pr_id:
                p["status"] = "merged"
                p["merged_at"] = _now_iso()
                break
    state.setdefault("pr_merges", []).append({
        "pr_id": pr_id,
        "method": method,
        "merged_at": _now_iso(),
    })
    _save_state(state)
    result = {
        "ok": True,
        "pr_id": pr_id,
        "method": method,
        "merged_at": _now_iso(),
    }
    _log_call("merge_pr", {"pr_id": pr_id, "method": method}, {"ok": True})
    return json.dumps(result)


@conditional_tool()
def list_oncall_schedule(
    team: str | None = None,
    week_of: str | None = None,
) -> str:
    """
    List the on-call schedule.

    Args:
        team: Optional team filter (e.g. "infra", "data-platform").
        week_of: Optional ISO date — returns only entries whose
            `week_of` field matches (week-anchor format, e.g.
            "2026-05-11").

    Returns:
        JSON {team, week_of, rotations: [...]} dict describing the
        current rotation membership and shifts.
    """
    state = _load_state()
    entries = list(state.get("oncall_schedule", []))
    if team:
        entries = [e for e in entries if e.get("team") == team]
    if week_of:
        entries = [e for e in entries if e.get("week_of") == week_of]
    result = {"count": len(entries), "rotations": entries}
    _log_call(
        "list_oncall_schedule",
        {"team": team, "week_of": week_of},
        {"count": len(entries)},
    )
    return json.dumps(result, indent=2)


@conditional_tool()
def acknowledge_incident(incident_id: str) -> str:
    """
    Acknowledge an active incident (claim it from the page queue).

    Args:
        incident_id: Incident identifier (e.g. "INC-2026-04-14-001").

    Returns:
        JSON {ok, incident_id, acknowledged_at}.
    """
    state = _load_state()
    target = None
    for inc in state.get("incidents", []):
        if inc.get("incident_id") == incident_id or inc.get("id") == incident_id:
            target = inc
            break
    if target is None:
        # Tolerant create — many tests reference incidents not in baseline.
        target = {
            "incident_id": incident_id,
            "id": incident_id,
            "status": "open",
        }
        state.setdefault("incidents", []).append(target)
    target["status"] = "acknowledged"
    target["acknowledged_at"] = _now_iso()
    _save_state(state)
    _log_call(
        "acknowledge_incident",
        {"incident_id": incident_id},
        {"ok": True},
    )
    return json.dumps({
        "ok": True,
        "incident_id": incident_id,
        "acknowledged_at": target["acknowledged_at"],
    })


@conditional_tool()
def escalate_incident(incident_id: str, to: str) -> str:
    """
    Escalate an incident to another responder (page secondary, page
    manager, etc.).

    Args:
        incident_id: Incident identifier.
        to: Responder handle to escalate to (e.g. "hema", "nadia",
            "secondary").

    Returns:
        JSON {ok, incident_id, escalated_to, escalated_at}.
    """
    state = _load_state()
    record = {
        "incident_id": incident_id,
        "to": to,
        "escalated_at": _now_iso(),
    }
    state.setdefault("incident_escalations", []).append(record)
    _save_state(state)
    _log_call(
        "escalate_incident",
        {"incident_id": incident_id, "to": to},
        {"ok": True},
    )
    return json.dumps({"ok": True, **record})


@conditional_tool()
def create_runbook_entry(service: str, title: str, body: str) -> str:
    """
    Create a new runbook entry for a service.

    Args:
        service: Service identifier (e.g. "metrics-router",
            "shard-keeper").
        title: Short title (e.g. "Recovering from a label-name
            mismatch").
        body: Full runbook body in markdown.

    Returns:
        JSON entry dict including `id`, `service`, `title`, `created_at`.
    """
    state = _load_state()
    entry = {
        "id": f"rb_{int(time.time()*1000)}",
        "service": service,
        "title": title,
        "body": body,
        "created_at": _now_iso(),
    }
    state.setdefault("runbooks", []).append(entry)
    _save_state(state)
    _log_call(
        "create_runbook_entry",
        {"service": service, "title": title, "body": body},
        {"id": entry["id"], "ok": True},
    )
    return json.dumps({"ok": True, **entry})


@conditional_tool()
def update_runbook_entry(
    entry_id: str,
    body: str | None = None,
    title: str | None = None,
    owner: str | None = None,
    backup: str | None = None,
) -> str:
    """
    Update a runbook entry, creating it if it does not already exist. The
    entry does NOT need to pre-exist — calling this with a new entry_id
    creates it. No prior lookup is required.

    Args:
        entry_id: Runbook entry identifier. Created if new.
        body: Optional complete replacement body (markdown). When supplied for
            an existing entry, it replaces that entry's entire current body.
        title: Optional new title.
        owner: Optional current primary owner identifier.
        backup: Optional current backup owner identifier.

    Returns:
        JSON {ok, entry_id, updated_at}.
    """
    if body is None and title is None and owner is None and backup is None:
        err = {
            "error": "NO_FIELDS",
            "instruction": (
                "update_runbook_entry: no content. Pass the actual body "
                "or another field — an entry_id-only call writes nothing."
            ),
        }
        _log_call("update_runbook_entry", {"entry_id": entry_id}, err)
        return json.dumps(err)
    state = _load_state()
    target = None
    for rb in state.get("runbooks", []):
        if rb.get("id") == entry_id:
            target = rb
            break
    if target is None:
        # Upsert: create the entry instead of refusing. The world should not
        # block a memory-driven update just because the container is absent.
        target = {"id": entry_id, "created_at": _now_iso()}
        state.setdefault("runbooks", []).append(target)
    if body is not None:
        target["body"] = body
    if title is not None:
        target["title"] = title
    if owner is not None:
        target["owner"] = owner
    if backup is not None:
        target["backup"] = backup
    target["updated_at"] = _now_iso()
    _save_state(state)
    _log_call(
        "update_runbook_entry",
        {
            "entry_id": entry_id,
            "body": body,
            "title": title,
            "owner": owner,
            "backup": backup,
        },
        {"ok": True},
    )
    return json.dumps({
        "ok": True,
        "entry_id": entry_id,
        "updated_at": target["updated_at"],
    })


@conditional_tool()
def get_runbook(
    service: str | None = None,
    entry_id: str | None = None,
) -> str:
    """
    Fetch runbook content — either by service (returns all entries
    for that service) or by entry_id (returns one entry).

    Args:
        service: Optional service identifier.
        entry_id: Optional specific entry identifier.

    Returns:
        JSON list of matching runbook entries, or {error} if neither
        filter is provided.
    """
    if not (service or entry_id):
        err = {
            "error": "MISSING_FILTER",
            "instruction": (
                "get_runbook requires either `service` or `entry_id`."
            ),
        }
        _log_call(
            "get_runbook",
            {"service": service, "entry_id": entry_id},
            err,
        )
        return json.dumps(err)
    state = _load_state()
    runbooks = state.get("runbooks", [])
    if entry_id:
        matched = [r for r in runbooks if r.get("id") == entry_id]
    else:
        matched = [r for r in runbooks if r.get("service") == service]
    _log_call(
        "get_runbook",
        {"service": service, "entry_id": entry_id},
        {"count": len(matched)},
    )
    return json.dumps({"count": len(matched), "entries": matched}, indent=2)


@conditional_tool()
def deploy_service(service: str, version: str, env: str, strategy: str = "direct") -> str:
    """
    Deploy a service version to an environment.

    Args:
        service: Service identifier (e.g. "metrics-router").
        version: Version tag (e.g. "v1.42.0", "sha:abc123").
        env: One of "dev", "staging", "prod".
        strategy: Rollout strategy -- "direct" (default) or "canary". One
            "canary" call performs both phases: it deploys to the canary slice,
            then completes the full deployment before returning. Do not call
            deploy_service again with "direct" for the same service, version,
            and environment.

    Returns:
        JSON {ok, deploy_id, status} on success, or {error} on
        invalid env.
    """
    allowed_envs = {"dev", "staging", "prod"}
    if env not in allowed_envs:
        err = {
            "error": "INVALID_ENV",
            "instruction": (
                "deploy_service env must be one of 'dev', 'staging', 'prod'."
            ),
        }
        _log_call(
            "deploy_service",
            {"service": service, "version": version, "env": env},
            err,
        )
        return json.dumps(err)
    state = _load_state()
    deploy = {
        "deploy_id": f"dep_{int(time.time()*1000)}",
        "service": service,
        "version": version,
        "env": env,
        "strategy": strategy,
        "status": "completed",
        "deployed_at": _now_iso(),
    }
    state.setdefault("deploys", []).append(deploy)
    _save_state(state)
    _log_call(
        "deploy_service",
        {"service": service, "version": version, "env": env, "strategy": strategy},
        {"deploy_id": deploy["deploy_id"], "ok": True},
    )
    return json.dumps({
        "ok": True,
        "deploy_id": deploy["deploy_id"],
        "status": deploy["status"],
    })


@conditional_tool()
def rollback_deploy(
    service: str,
    deploy_id: str | None = None,
) -> str:
    """
    Roll back a deploy — either a specific deploy_id, or the most
    recent deploy for the given service.

    Args:
        service: Service identifier.
        deploy_id: Optional specific deploy_id to roll back.

    Returns:
        JSON {ok, service, rolled_back_deploy_id, rolled_back_at}.
    """
    state = _load_state()
    target = None
    deploys = state.get("deploys", [])
    if deploy_id:
        for d in deploys:
            if d.get("deploy_id") == deploy_id:
                target = d
                break
    else:
        for d in reversed(deploys):
            if d.get("service") == service:
                target = d
                break
    rolled_id = target.get("deploy_id") if target else None
    record = {
        "service": service,
        "rolled_back_deploy_id": rolled_id,
        "rolled_back_at": _now_iso(),
    }
    state.setdefault("rollbacks", []).append(record)
    _save_state(state)
    _log_call(
        "rollback_deploy",
        {"service": service, "deploy_id": deploy_id},
        {"ok": True, "rolled_back_deploy_id": rolled_id},
    )
    return json.dumps({"ok": True, **record})


@conditional_tool()
def get_service_metrics(service: str, metric: str, timerange: str) -> str:
    """
    Fetch a metric time-series for a service.

    Args:
        service: Service identifier (e.g. "metrics-router").
        metric: Metric name — e.g. "p99_ms", "rps", "error_rate",
            "cpu_pct", "mem_pct".
        timerange: Natural-language window — e.g. "1h", "6h", "24h",
            "7d".

    Returns:
        JSON dict with `service`, `metric`, `timerange`, and a small
        synthetic time-series (deterministic stub for testing).
    """
    # Deterministic synthetic series — hash-based so repeated calls
    # return the same shape but different metrics/services differ.
    seed = int(hashlib.sha256(f"{service}|{metric}|{timerange}".encode()).hexdigest(), 16)
    base = (seed % 100) + 1
    points = []
    for i in range(12):
        # Mild deterministic wave around base.
        delta = ((seed >> (i * 4)) & 0xF) - 8
        points.append({
            "t_minus": 11 - i,
            "value": max(0, base + delta),
        })
    result = {
        "service": service,
        "metric": metric,
        "timerange": timerange,
        "unit": _metric_unit(metric),
        "points": points,
    }
    _log_call(
        "get_service_metrics",
        {"service": service, "metric": metric, "timerange": timerange},
        {"point_count": len(points)},
    )
    return json.dumps(result, indent=2)


def _metric_unit(metric: str) -> str:
    """Best-effort unit label for a metric name (mock convenience)."""
    m = metric.lower()
    if "ms" in m or "latency" in m or "p99" in m or "p95" in m or "p50" in m:
        return "ms"
    if "rps" in m or "qps" in m or "throughput" in m:
        return "req/s"
    if "rate" in m or "pct" in m or "percent" in m:
        return "%"
    if "bytes" in m or "mem" in m:
        return "MB"
    return "count"


@conditional_tool()
def query_logs(
    service: str,
    query: str,
    timerange: str,
    limit: int = 20,
) -> str:
    """
    Query a service's logs.

    Args:
        service: Service identifier.
        query: Log filter expression (free-text or grep-shape).
        timerange: Window (e.g. "1h", "6h", "24h").
        limit: Max log lines to return (default 20).

    Returns:
        JSON {service, query, timerange, count, lines: [...]} with
        a small deterministic synthetic sample.
    """
    # Deterministic synthetic log lines.
    seed = int(hashlib.sha256(f"{service}|{query}|{timerange}".encode()).hexdigest(), 16)
    n = min(limit, max(1, (seed % 8) + 2))
    lines = []
    for i in range(n):
        ts = _now_iso()
        lines.append({
            "ts": ts,
            "service": service,
            "level": "INFO" if (i % 4) else "WARN",
            "message": f"[mock] {service} matched '{query}' (stub line {i})",
        })
    _log_call(
        "query_logs",
        {"service": service, "query": query, "timerange": timerange, "limit": limit},
        {"count": len(lines)},
    )
    return json.dumps({
        "service": service,
        "query": query,
        "timerange": timerange,
        "count": len(lines),
        "lines": lines,
    }, indent=2)


# ---- Invoices / finance ----

@conditional_tool()
def list_invoices(status: str | None = "pending") -> str:
    """
    List invoices, filtered by status.

    Args:
        status: Status filter — one of "pending", "paid", "flagged".
            Defaults to "pending". Pass None to return all statuses.

    Returns:
        JSON list of {id, vendor, amount, description, due_date, status}.
    """
    state = _load_state()
    invoices = list(state.get("invoices", []))
    if status is not None:
        invoices = [i for i in invoices if i.get("status") == status]
    _log_call("list_invoices", {"status": status}, {"count": len(invoices)})
    return json.dumps(invoices, indent=2)


# ---- Deck editing ----

@conditional_tool()
def update_deck_slide(
    deck: str,
    slide: int,
    change: str,
) -> str:
    """
    Update a slide in a deck, creating the deck and/or the slide if it does
    not already exist. The deck does NOT need to pre-exist in the workspace —
    calling this with a new deck name creates it. Use this to record or revise
    slide content directly; no prior lookup or deck file is required.

    Args:
        deck: Deck identifier (e.g. "Board_Prep_Deck_v18"). Created if new.
        slide: Slide number to update or add.
        change: Free-text content/edit to apply to the slide.

    Returns:
        JSON confirmation with the updated deck identifier and slide.
    """
    state = _load_state()
    edit = {
        "id": f"deck_edit_{int(time.time()*1000)}",
        "deck": deck,
        "slide": slide,
        "change": change,
        "applied_at": _now_iso(),
    }
    state.setdefault("deck_edits", []).append(edit)
    _save_state(state)
    _log_call(
        "update_deck_slide",
        {"deck": deck, "slide": slide, "change": change},
        {"id": edit["id"], "ok": True},
    )
    return json.dumps({"ok": True, "id": edit["id"], "deck": deck, "slide": slide})


# ---- Docs / agendas / prep ----

@conditional_tool()
def create_doc(
    title: str,
    body: str,
    folder: str | None = None,
) -> str:
    """
    Create a new document (notes / agenda / prep doc / one-pager).

    Args:
        title: Doc title (e.g. "Q3 offsite agenda — one day").
        body: Full doc body — markdown or plain text.
        folder: Optional folder / workspace name (e.g. "Eng", "Team").
            None means the user's default folder.

    Returns:
        JSON {ok, id, url} for the newly created doc.
    """
    state = _load_state()
    doc = {
        "id": f"doc_{int(time.time()*1000)}",
        "title": title,
        "body": body,
        "folder": folder,
        "created_at": _now_iso(),
    }
    state.setdefault("docs", []).append(doc)
    _save_state(state)
    url = f"https://docs.mock/{doc['id']}"
    _log_call(
        "create_doc",
        {"title": title, "body": body, "folder": folder},
        {"id": doc["id"], "url": url, "ok": True},
    )
    return json.dumps({"ok": True, "id": doc["id"], "url": url})


@conditional_tool()
def update_doc(
    doc_id: str,
    body: str,
    title: str | None = None,
    folder: str | None = None,
    mode: str = "replace",
) -> str:
    """Replace or append to the contents of an existing document.

    Args:
        doc_id: Exact ID of the existing document.
        body: Complete replacement body, or only the new text when mode is append.
            When the existing body is non-empty, append inserts a blank line before
            the new text.
        title: Optional replacement title.
        folder: Optional replacement folder.
        mode: replace replaces the body. append retains the body and adds the new
            text after a blank line.

    Returns:
        JSON {ok, id, url} for the updated document, or {error, instruction}
        when the document does not exist or mode is invalid.
    """
    if mode not in {"replace", "append"}:
        err = {
            "error": "INVALID_UPDATE_MODE",
            "instruction": "update_doc: mode must be 'replace' or 'append'.",
        }
        _log_call(
            "update_doc",
            {"doc_id": doc_id, "body": body, "title": title, "folder": folder, "mode": mode},
            err,
        )
        return json.dumps(err)

    state = _load_state()
    target = next(
        (
            doc
            for doc in state.get("docs", [])
            if isinstance(doc, dict) and doc.get("id") == doc_id
        ),
        None,
    )
    if target is None:
        err = {
            "error": "NOT_FOUND",
            "instruction": f"update_doc: no document with id={doc_id!r}.",
        }
        _log_call(
            "update_doc",
            {"doc_id": doc_id, "body": body, "title": title, "folder": folder, "mode": mode},
            err,
        )
        return json.dumps(err)

    if mode == "append" and target.get("body"):
        target["body"] = f"{str(target['body']).rstrip()}\n\n{body.lstrip()}"
    else:
        target["body"] = body
    if title is not None:
        target["title"] = title
    if folder is not None:
        target["folder"] = folder
    target["updated_at"] = _now_iso()
    _save_state(state)
    result = {
        "ok": True,
        "id": doc_id,
        "url": f"https://docs.mock/{doc_id}",
    }
    _log_call(
        "update_doc",
        {"doc_id": doc_id, "body": body, "title": title, "folder": folder, "mode": mode},
        result,
    )
    return json.dumps(result)


# ---- Hotel booking ----

@conditional_tool()
def book_hotel(
    hotel: str,
    check_in: str,
    check_out: str,
    location: str | None = None,
    guests: int = 1,
) -> str:
    """
    Book a hotel stay.

    Args:
        hotel: Hotel name or identifier (e.g. "Pod Hotel Times Square").
        check_in: ISO date or datetime of arrival (e.g. "2026-03-17").
        check_out: ISO date or datetime of departure.
        location: Optional neighborhood / city / area string.
        guests: Number of guests (default 1).

    Returns:
        JSON {ok, id} confirming the booking.
    """
    state = _load_state()
    booking = {
        "id": f"hotel_{int(time.time()*1000)}",
        "hotel": hotel,
        "check_in": check_in,
        "check_out": check_out,
        "location": location,
        "guests": guests,
        "booked_at": _now_iso(),
    }
    state.setdefault("hotel_bookings", []).append(booking)
    _save_state(state)
    _log_call(
        "book_hotel",
        {"hotel": hotel, "check_in": check_in, "check_out": check_out,
         "location": location, "guests": guests},
        {"id": booking["id"], "ok": True},
    )
    return json.dumps({"ok": True, "id": booking["id"]})


# ---- PR comments ----

@conditional_tool()
def post_pr_comment(
    pr: str,
    body: str,
) -> str:
    """
    Post a comment on a pull request thread, identified by `pr`. The PR does
    NOT need to be looked up first — just post the comment to the named PR.

    Args:
        pr: PR identifier (e.g. "auth-refactor-pr" or "scaffold/api#142").
        body: Comment body (markdown supported).

    Returns:
        JSON {ok, id} for the new comment.
    """
    state = _load_state()
    comment = {
        "id": f"prc_{int(time.time()*1000)}",
        "pr": pr,
        "body": body,
        "posted_at": _now_iso(),
    }
    state.setdefault("pr_comments", []).append(comment)
    _save_state(state)
    _log_call(
        "post_pr_comment",
        {"pr": pr, "body": body},
        {"id": comment["id"], "ok": True},
    )
    return json.dumps({"ok": True, "id": comment["id"]})


@conditional_tool()
def post_doc_comment(
    doc_id: str,
    body: str,
) -> str:
    """
    Post a comment on an existing document or a standalone comment thread.

    Args:
        doc_id: Exact existing document ID, or a new standalone thread name.
        body: Comment body (markdown supported).

    Returns:
        JSON {ok, id} for the new comment.
    """
    state = _load_state()
    documents = [doc for doc in state.get("docs", []) if isinstance(doc, dict)]
    if not any(doc.get("id") == doc_id for doc in documents):
        normalized_doc_id = re.sub(r"[^a-z0-9]+", "-", doc_id.casefold()).strip("-")
        matched_document = next(
            (
                doc
                for doc in documents
                if doc_id == doc.get("title")
                or (
                    isinstance(doc.get("title"), str)
                    and normalized_doc_id
                    == re.sub(r"[^a-z0-9]+", "-", doc["title"].casefold()).strip("-")
                )
            ),
            None,
        )
        if matched_document is not None:
            err = {
                "error": "INVALID_DOCUMENT_ID",
                "instruction": (
                    "post_doc_comment target matches an existing document; use "
                    f"its exact ID: {matched_document.get('id')}."
                ),
            }
            _log_call("post_doc_comment", {"doc_id": doc_id, "body": body}, err)
            return json.dumps(err)
    comment = {
        "id": f"docc_{int(time.time()*1000)}",
        "doc_id": doc_id,
        "body": body,
        "posted_at": _now_iso(),
    }
    state.setdefault("doc_comments", []).append(comment)
    _save_state(state)
    _log_call(
        "post_doc_comment",
        {"doc_id": doc_id, "body": body},
        {"id": comment["id"], "ok": True},
    )
    return json.dumps({"ok": True, "id": comment["id"]})


# ---- CRM row updates ----
# Design choice: match the row by `name` (string key) rather than an opaque
# row_id. Tests like 014 specify "Elena Volkov" as the mock_state key and
# the rubric reads `args.status`, `args.trigger_condition`,
# `args.next_touch_date` — none reference an id. add_crm_row uses `name`
# as the primary input too, so keeping the lookup-by-name shape is the
# more common pattern across existing tools.

@conditional_tool()
def update_crm_row(
    name: str,
    status: str | None = None,
    next_touch_date: str | None = None,
    trigger_condition: str | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """
    Update fields on a CRM row, looked up by `name`, creating the row if it
    does not already exist. The row does NOT need to pre-exist — no prior
    lookup is required; just set the fields.

    Args:
        name: Name of the person/company whose CRM row to update.
        status: Optional new status (e.g. "warm_later", "active", "passed").
        next_touch_date: Optional next touch date (ISO date or descriptive).
        trigger_condition: Optional natural-language trigger for re-engagement.
        notes: Optional complete replacement value for the notes field. To
            preserve existing notes, include the existing text followed by the
            new text in this argument.
        tags: Optional tag list to replace existing tags.

    Returns:
        JSON {ok, id, name} for the updated row, or {ok: false, error}
        if no row matches the name.
    """
    state = _load_state()
    rows = state.setdefault("crm", [])
    target = None
    for r in rows:
        if r.get("name") == name:
            target = r
            break
    if target is None:
        # Create the row if it doesn't exist — graders care about the
        # final args dict regardless of whether the row pre-existed.
        target = {
            "id": f"crm_{int(time.time()*1000)}",
            "name": name,
            "added_at": _now_iso(),
        }
        rows.append(target)
    if status is not None:
        target["status"] = status
    if next_touch_date is not None:
        target["next_touch_date"] = next_touch_date
    if trigger_condition is not None:
        target["trigger_condition"] = trigger_condition
    if notes is not None:
        target["notes"] = notes
    if tags is not None:
        target["tags"] = list(tags)
    target["updated_at"] = _now_iso()
    _save_state(state)
    _log_call(
        "update_crm_row",
        {"name": name, "status": status, "next_touch_date": next_touch_date,
         "trigger_condition": trigger_condition, "notes": notes,
         "tags": tags},
        {"id": target["id"], "ok": True},
    )
    return json.dumps({"ok": True, "id": target["id"], "name": name})


# ---- Headcount plan updates ----

@conditional_tool()
def update_headcount_plan(
    plan_id: str,
    next_two_hires: list | None = None,
    revisit_after: str | None = None,
    rationale_tags: list | None = None,
    notes: str | None = None,
) -> str:
    """
    Update structured fields on a headcount plan doc, creating the plan if it
    does not already exist. The plan does NOT need to pre-exist — calling this
    with a new plan_id creates it. No prior lookup is required. For an existing
    plan, every omitted or null optional argument leaves that field unchanged;
    provide a non-null replacement value to change a structured field.

    Args:
        plan_id: Plan identifier (e.g. "mercury_headcount_v1"). Created if new.
        next_two_hires: Ordered list of the next two planned hires
            (e.g. ["mid_level", null] or ["mid_level", "revisit_post_alpha"]).
        revisit_after: Non-null milestone that replaces the current revisit
            point (e.g. "mercury_alpha"). Null leaves the current value unchanged.
        rationale_tags: List of tags justifying the plan
            (e.g. ["senior_close_time", "salary_band_drag"]).
        notes: Optional free-text notes.

    Returns:
        JSON {ok, plan_id} for the updated plan.
    """
    state = _load_state()
    plans = state.setdefault("headcount_plans", {})
    plan = plans.setdefault(plan_id, {"plan_id": plan_id,
                                       "created_at": _now_iso()})
    if next_two_hires is not None:
        plan["next_two_hires"] = list(next_two_hires)
    if revisit_after is not None:
        plan["revisit_after"] = revisit_after
    if rationale_tags is not None:
        plan["rationale_tags"] = list(rationale_tags)
    if notes is not None:
        plan["notes"] = notes
    plan["updated_at"] = _now_iso()
    _save_state(state)
    _log_call(
        "update_headcount_plan",
        {"plan_id": plan_id, "next_two_hires": next_two_hires,
         "revisit_after": revisit_after, "rationale_tags": rationale_tags,
         "notes": notes},
        {"plan_id": plan_id, "ok": True},
    )
    return json.dumps({"ok": True, "plan_id": plan_id})


# ======================================================================
# Riley (growth/product operator) tools
# ----------------------------------------------------------------------
# Riley owns activation, retention, lifecycle email, and feature-flag
# experimentation at Helio. Her tool surface mirrors a working-day mix
# of PostHog (feature flags + product analytics), Stripe (subscriptions
# + billing), Baremetrics (MRR/churn/segments), an experimentation
# layer, and a Notion-style "growth brief" doc surface. Standard comms
# (calendar, email, Slack, Notion docs) are shared with Morgan/Alex
# above — Riley's manifest reuses those directly.
# ======================================================================


# ---- PostHog-shaped: feature flags ----

@conditional_tool()
def list_feature_flags(
    status: str | None = None,
    owner: str | None = None,
) -> str:
    """
    List feature flags in the PostHog-shaped flag registry, optionally
    filtered by status and/or owner.

    Args:
        status: Optional status filter — one of "draft", "running",
            "shipped", "rolled_back". None returns all.
        owner: Optional owner string (e.g. "riley") to filter by.

    Returns:
        JSON list of {id, key, name, rollout_pct, status, owner, created_at}.
    """
    state = _load_state()
    flags = list(state.get("feature_flags", []))
    if status is not None:
        flags = [f for f in flags if f.get("status") == status]
    if owner is not None:
        flags = [f for f in flags if f.get("owner") == owner]
    _log_call(
        "list_feature_flags",
        {"status": status, "owner": owner},
        {"count": len(flags)},
    )
    return json.dumps(flags, indent=2)


@conditional_tool()
def get_feature_flag(key: str) -> str:
    """
    Get a feature flag by key.

    Args:
        key: Flag key (e.g. "lifecycle_onboarding_trigger_v2").

    Returns:
        JSON object for the flag, or {error, instruction} if not found.
    """
    state = _load_state()
    for f in state.get("feature_flags", []):
        if f.get("key") == key or f.get("id") == key:
            _log_call("get_feature_flag", {"key": key}, {"found": True})
            return json.dumps(f, indent=2)
    err = {
        "error": "NOT_FOUND",
        "instruction": (
            f"get_feature_flag: no flag with key={key!r}. Call "
            "list_feature_flags() first to enumerate available keys."
        ),
    }
    _log_call("get_feature_flag", {"key": key}, err)
    return json.dumps(err)


@conditional_tool()
def update_feature_flag(
    key: str,
    rollout_pct: int | None = None,
    status: str | None = None,
    name: str | None = None,
    notes: str | None = None,
) -> str:
    """
    Update fields on a feature flag (creates the flag if it doesn't exist).

    Args:
        key: Flag key (e.g. "lifecycle_onboarding_trigger_v2").
        rollout_pct: Optional rollout percentage (0-100).
        status: Optional new status — "draft" | "running" | "shipped"
            | "rolled_back".
        name: Optional new human-readable name.
        notes: Optional free-text notes (e.g. rollout rationale).

    Returns:
        JSON {ok, id, key, rollout_pct, status} for the updated flag.
    """
    if all(v is None for v in (rollout_pct, status, name, notes)):
        err = {
            "error": "NO_FIELDS",
            "instruction": (
                "update_feature_flag: no fields to update. Pass the actual "
                "change (status / rollout_pct / notes) — a key-only call "
                "changes nothing."
            ),
        }
        _log_call("update_feature_flag", {"key": key}, err)
        return json.dumps(err)
    state = _load_state()
    flags = state.setdefault("feature_flags", [])
    target = None
    for f in flags:
        if f.get("key") == key:
            target = f
            break
    if target is None:
        target = {
            "id": f"flag_{int(time.time()*1000)}",
            "key": key,
            "name": name or key,
            "rollout_pct": 0,
            "status": "draft",
            "owner": "riley",
            "created_at": _now_iso(),
        }
        flags.append(target)
    if rollout_pct is not None:
        target["rollout_pct"] = rollout_pct
    if status is not None:
        target["status"] = status
    if name is not None:
        target["name"] = name
    if notes is not None:
        target["notes"] = notes
    target["updated_at"] = _now_iso()
    _save_state(state)
    _log_call(
        "update_feature_flag",
        {"key": key, "rollout_pct": rollout_pct, "status": status,
         "name": name, "notes": notes},
        {"id": target["id"], "ok": True},
    )
    return json.dumps({
        "ok": True,
        "id": target["id"],
        "key": key,
        "rollout_pct": target.get("rollout_pct"),
        "status": target.get("status"),
    })


# ---- PostHog-shaped: product analytics queries ----

@conditional_tool()
def query_event_funnel(
    funnel_name: str,
    cohort: str | None = None,
    window_days: int = 14,
) -> str:
    """
    Run a funnel query (signup → activation → first paid month) over a
    named cohort and lookback window.

    Args:
        funnel_name: Funnel identifier (e.g. "activation_funnel_v1",
            "signup_to_first_paid").
        cohort: Optional cohort filter (e.g. "mid_seg", "starter").
            None means all cohorts.
        window_days: Lookback window in days (default 14).

    Returns:
        JSON {funnel_name, cohort, window_days, steps: [{name, count, pct}]}.
    """
    state = _load_state()
    saved = next(
        (f for f in state.get("funnels", []) if f.get("name") == funnel_name),
        None,
    )
    if saved is None:
        err = {
            "error": "NOT_FOUND",
            "instruction": (
                f"query_event_funnel: no funnel with name={funnel_name!r}."
            ),
        }
        _log_call(
            "query_event_funnel",
            {"funnel_name": funnel_name, "cohort": cohort, "window_days": window_days},
            err,
        )
        return json.dumps(err)
    result = dict(saved)
    result.setdefault("cohort", cohort)
    result.setdefault("window_days", window_days)
    _log_call(
        "query_event_funnel",
        {"funnel_name": funnel_name, "cohort": cohort, "window_days": window_days},
        {"steps": len(result.get("steps", []))},
    )
    return json.dumps(result, indent=2)


@conditional_tool()
def query_retention_cohort(
    cohort_id: str,
    weeks: int = 12,
    asof_date: str | None = None,
) -> str:
    """
    Pull a week-over-week retention curve for a named cohort.

    Args:
        cohort_id: Cohort identifier (e.g. "mid_seg_50_200_v1",
            "starter_lt_50").
        weeks: Number of weeks of retention to return (default 12).
        asof_date: Optional ISO date the snapshot is anchored to.
            None means "latest available".

    Returns:
        JSON {cohort_id, asof_date, weeks, retention_curve: [pct_per_week]}.
    """
    state = _load_state()
    cohort_snaps = [
        c for c in state.get("retention_cohorts", [])
        if c.get("cohort_id") == cohort_id
    ]
    if asof_date is not None:
        snap = next(
            (c for c in cohort_snaps if c.get("asof_date") == asof_date),
            None,
        )
    else:
        snap = max(
            cohort_snaps,
            key=lambda c: c.get("asof_date", ""),
            default=None,
        )
    if snap is None:
        err = {
            "error": "NOT_FOUND",
            "instruction": (
                f"query_retention_cohort: no snapshot for cohort_id={cohort_id!r}"
                + (f" asof_date={asof_date!r}." if asof_date is not None else ".")
            ),
        }
        _log_call(
            "query_retention_cohort",
            {"cohort_id": cohort_id, "weeks": weeks, "asof_date": asof_date},
            err,
        )
        return json.dumps(err)
    snap = dict(snap)
    snap.setdefault("weeks", weeks)
    _log_call(
        "query_retention_cohort",
        {"cohort_id": cohort_id, "weeks": weeks, "asof_date": asof_date},
        {"len_curve": len(snap.get("retention_curve", []))},
    )
    return json.dumps(snap, indent=2)


@conditional_tool()
def query_user_path(
    start_event: str,
    end_event: str | None = None,
    cohort: str | None = None,
    max_steps: int = 5,
) -> str:
    """
    Run a path-analysis query — what do users do between two events?

    Args:
        start_event: Event name to anchor paths from (e.g. "signup").
        end_event: Optional target event (e.g. "first_paid"). None means
            "open-ended — show top-N forward paths".
        cohort: Optional cohort filter (e.g. "mid_seg").
        max_steps: Max path depth (default 5).

    Returns:
        JSON {start_event, end_event, cohort, top_paths: [{path, count}]}.
    """
    state = _load_state()
    saved = None
    for p in state.get("user_paths", []):
        if (p.get("start_event") == start_event and
                p.get("end_event") == end_event):
            saved = p
            break
    if saved is None:
        err = {
            "error": "NOT_FOUND",
            "instruction": (
                f"query_user_path: no path from start_event={start_event!r} "
                f"to end_event={end_event!r}."
            ),
        }
        _log_call(
            "query_user_path",
            {"start_event": start_event, "end_event": end_event,
             "cohort": cohort, "max_steps": max_steps},
            err,
        )
        return json.dumps(err)
    _log_call(
        "query_user_path",
        {"start_event": start_event, "end_event": end_event,
         "cohort": cohort, "max_steps": max_steps},
        {"paths": len(saved.get("top_paths", []))},
    )
    return json.dumps(saved, indent=2)


# ---- Stripe-shaped: subscriptions / customers / billing ----

@conditional_tool()
def list_subscriptions(
    tier: str | None = None,
    status: str | None = "active",
    limit: int = 50,
) -> str:
    """
    List subscriptions in the Stripe-shaped subscription store, optionally
    filtered by tier and status.

    Args:
        tier: Optional tier filter (e.g. "starter", "mid_seg", "growth").
        status: Status filter — "active" | "paused" | "canceled" |
            "past_due". Defaults to "active". Pass None to return all.
        limit: Max rows to return (default 50).

    Returns:
        JSON list of subscription records.
    """
    state = _load_state()
    subs = list(state.get("subscriptions", []))
    if tier is not None:
        subs = [s for s in subs if s.get("tier") == tier]
    if status is not None:
        subs = [s for s in subs if s.get("status") == status]
    subs = subs[:limit]
    _log_call(
        "list_subscriptions",
        {"tier": tier, "status": status, "limit": limit},
        {"count": len(subs)},
    )
    return json.dumps(subs, indent=2)


@conditional_tool()
def get_subscription(subscription_id: str) -> str:
    """
    Get a single subscription by id.

    Args:
        subscription_id: Stripe-shaped subscription id (e.g. "sub_...").

    Returns:
        JSON object for the subscription, or {error} if not found.
    """
    state = _load_state()
    for s in state.get("subscriptions", []):
        if s.get("subscription_id") == subscription_id or s.get("id") == subscription_id:
            _log_call("get_subscription", {"subscription_id": subscription_id},
                      {"found": True})
            return json.dumps(s, indent=2)
    err = {
        "error": "NOT_FOUND",
        "instruction": (
            f"get_subscription: no subscription with id={subscription_id!r}. "
            "Call list_subscriptions() first to enumerate."
        ),
    }
    _log_call("get_subscription", {"subscription_id": subscription_id}, err)
    return json.dumps(err)


@conditional_tool()
def list_stripe_invoices(
    customer_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> str:
    """
    List Stripe-shaped invoices (separate from Morgan's vendor-invoice
    review queue — these are customer billing invoices).

    Args:
        customer_id: Optional customer filter.
        status: Optional status — "open" | "paid" | "void" |
            "uncollectible". None returns all.
        limit: Max rows (default 50).

    Returns:
        JSON list of invoice records.
    """
    state = _load_state()
    invs = list(state.get("stripe_invoices", []))
    if customer_id is not None:
        invs = [i for i in invs if i.get("customer_id") == customer_id]
    if status is not None:
        invs = [i for i in invs if i.get("status") == status]
    invs = invs[:limit]
    _log_call(
        "list_stripe_invoices",
        {"customer_id": customer_id, "status": status, "limit": limit},
        {"count": len(invs)},
    )
    return json.dumps(invs, indent=2)


@conditional_tool()
def get_customer(customer_id: str) -> str:
    """
    Get a customer record by id.

    Args:
        customer_id: Customer id (e.g. "cus_acme_dev_shop").

    Returns:
        JSON object for the customer, or {error} if not found.
    """
    state = _load_state()
    for c in state.get("customers", []):
        if c.get("customer_id") == customer_id or c.get("id") == customer_id:
            _log_call("get_customer", {"customer_id": customer_id},
                      {"found": True})
            return json.dumps(c, indent=2)
    err = {
        "error": "NOT_FOUND",
        "instruction": (
            f"get_customer: no customer with id={customer_id!r}. "
        ),
    }
    _log_call("get_customer", {"customer_id": customer_id}, err)
    return json.dumps(err)


@conditional_tool()
def list_recent_churn(
    tier: str | None = None,
    days: int = 30,
    limit: int = 100,
) -> str:
    """
    List customers who churned in the recent window, optionally filtered
    by tier.

    Args:
        tier: Optional tier filter (e.g. "mid_seg").
        days: Lookback window in days (default 30).
        limit: Max rows (default 100).

    Returns:
        JSON list of churn-event records:
        {customer_id, name, tier, churned_at, mrr_lost, reason}.
    """
    state = _load_state()
    events = list(state.get("churn_events", []))
    if tier is not None:
        events = [e for e in events if e.get("tier") == tier]
    events = events[:limit]
    _log_call(
        "list_recent_churn",
        {"tier": tier, "days": days, "limit": limit},
        {"count": len(events)},
    )
    return json.dumps({
        "tier": tier,
        "window_days": days,
        "events": events,
    }, indent=2)


# ---- Baremetrics-shaped: MRR / churn / segment metrics ----

@conditional_tool()
def get_mrr_metrics(asof_date: str | None = None) -> str:
    """
    Get the MRR snapshot (total MRR, ARR, net-new, expansion, churned)
    for a given date — or latest if none provided.

    Args:
        asof_date: Optional ISO date (e.g. "2026-02-28"). None means
            "latest snapshot".

    Returns:
        JSON snapshot {asof_date, total_mrr, arr, net_new_mrr,
        expansion_mrr, churned_mrr}.
    """
    state = _load_state()
    snaps = list(state.get("mrr_snapshots", []))
    if asof_date is not None:
        snap = next(
            (s for s in snaps if s.get("asof_date") == asof_date),
            None,
        )
    else:
        snap = max(snaps, key=lambda s: s.get("asof_date", ""), default=None)
    if snap is None:
        err = {
            "error": "NOT_FOUND",
            "instruction": (
                "get_mrr_metrics: no snapshot"
                + (f" for asof_date={asof_date!r}." if asof_date is not None else ".")
            ),
        }
        _log_call("get_mrr_metrics", {"asof_date": asof_date}, err)
        return json.dumps(err)
    _log_call("get_mrr_metrics", {"asof_date": asof_date}, {"found": True})
    return json.dumps(snap, indent=2)


@conditional_tool()
def get_churn_metrics(
    tier: str | None = None,
    asof_date: str | None = None,
) -> str:
    """
    Get monthly churn metrics (rate, $-churned, accounts-churned) for a
    tier, anchored at a date.

    Args:
        tier: Optional tier — "starter" | "mid_seg" | "growth" |
            "enterprise". None returns the top-level aggregate.
        asof_date: Optional ISO month-end date.

    Returns:
        JSON {tier, asof_date, churn_rate_pct, churned_mrr, churned_accounts}.
    """
    state = _load_state()
    cm = state.get("churn_metrics") or {}
    tier_key = tier or "all"
    if asof_date is not None:
        snap = cm.get(f"{tier_key}|{asof_date}")
    else:
        candidates = [
            (key, value) for key, value in cm.items()
            if key.startswith(f"{tier_key}|")
        ]
        snap = max(
            candidates,
            key=lambda item: item[1].get("asof_date", item[0].split("|", 1)[1]),
            default=(None, None),
        )[1]
    if snap is None:
        err = {
            "error": "NOT_FOUND",
            "instruction": (
                f"get_churn_metrics: no snapshot for tier={tier_key!r}"
                + (f" asof_date={asof_date!r}." if asof_date is not None else ".")
            ),
        }
        _log_call(
            "get_churn_metrics",
            {"tier": tier, "asof_date": asof_date},
            err,
        )
        return json.dumps(err)
    _log_call(
        "get_churn_metrics",
        {"tier": tier, "asof_date": asof_date},
        {"churn_rate_pct": snap.get("churn_rate_pct")},
    )
    return json.dumps(snap, indent=2)


@conditional_tool()
def get_arr_segments(asof_date: str | None = None) -> str:
    """
    Get ARR broken down by segment/tier.

    Args:
        asof_date: Optional ISO date. None means latest.

    Returns:
        JSON {asof_date, segments: [{tier, arr, pct_of_total,
        account_count}]}.
    """
    state = _load_state()
    segments = list(state.get("arr_segments", []))
    if asof_date is not None:
        seg = next(
            (s for s in segments if s.get("asof_date") == asof_date),
            None,
        )
    else:
        seg = max(segments, key=lambda s: s.get("asof_date", ""), default=None)
    if seg is None:
        err = {
            "error": "NOT_FOUND",
            "instruction": (
                "get_arr_segments: no snapshot"
                + (f" for asof_date={asof_date!r}." if asof_date is not None else ".")
            ),
        }
        _log_call("get_arr_segments", {"asof_date": asof_date}, err)
        return json.dumps(err)
    _log_call("get_arr_segments", {"asof_date": asof_date},
              {"segments": len(seg.get("segments", []))})
    return json.dumps(seg, indent=2)


# ---- Experiment lifecycle ----

@conditional_tool()
def create_experiment(
    experiment_id: str,
    name: str,
    hypothesis: str,
    metric_of_record: str,
    cohort_scope: str,
    flag_id: str | None = None,
    designed_at: str | None = None,
) -> str:
    """
    Register a new experiment in the experiment log. Status defaults to
    "designed". Use update_experiment to move it through running →
    significant → shipped / rolled_back.

    Args:
        experiment_id: Stable identifier (e.g.
            "EXP-2026-02-onboarding-trigger").
        name: Human-readable name.
        hypothesis: Single-sentence hypothesis (will the metric move,
            and by how much).
        metric_of_record: Primary metric (e.g. "week-1 retention,
            mid-seg cohort").
        cohort_scope: Cohort identifier or natural-language scope.
        flag_id: Optional PostHog flag key wiring the experiment.
        designed_at: Optional ISO date the design landed. Defaults to
            today.

    Returns:
        JSON {ok, experiment_id, status} for the new experiment.
    """
    state = _load_state()
    exps = state.setdefault("experiments", [])
    # Replace-by-id semantics so re-runs are idempotent
    for i, e in enumerate(exps):
        if e.get("experiment_id") == experiment_id:
            exps.pop(i)
            break
    exp = {
        "experiment_id": experiment_id,
        "name": name,
        "hypothesis": hypothesis,
        "metric_of_record": metric_of_record,
        "cohort_scope": cohort_scope,
        "flag_id": flag_id,
        "status": "designed",
        "designed_at": designed_at or _today_iso(),
        "created_at": _now_iso(),
    }
    exps.append(exp)
    _save_state(state)
    _log_call(
        "create_experiment",
        {"experiment_id": experiment_id, "name": name, "hypothesis": hypothesis,
         "metric_of_record": metric_of_record, "cohort_scope": cohort_scope,
         "flag_id": flag_id, "designed_at": designed_at},
        {"ok": True, "experiment_id": experiment_id},
    )
    return json.dumps({"ok": True, "experiment_id": experiment_id,
                       "status": "designed"})


@conditional_tool()
def update_experiment(
    experiment_id: str,
    status: str | None = None,
    result: str | None = None,
    landed_at: str | None = None,
    learning_carryforward: str | None = None,
) -> str:
    """
    Update an existing experiment — typically to move it through the
    status state machine (designed → running → significant → shipped
    or rolled_back / inconclusive).

    Args:
        experiment_id: Existing experiment id.
        status: New status. Allowed: "designed", "running",
            "significant", "shipped", "rolled_back", "inconclusive".
        result: Optional natural-language summary of the result.
        landed_at: Optional ISO date the experiment shipped or rolled back.
        learning_carryforward: Optional "what we now believe" note.

    Returns:
        JSON {ok, experiment_id, status} for the updated row, or
        {error} if no experiment matches.
    """
    state = _load_state()
    exps = state.get("experiments") or []
    target = None
    for e in exps:
        if e.get("experiment_id") == experiment_id:
            target = e
            break
    if target is None:
        err = {
            "error": "NOT_FOUND",
            "instruction": (
                f"update_experiment: no experiment with id={experiment_id!r}. "
                "Call create_experiment first."
            ),
        }
        _log_call("update_experiment",
                  {"experiment_id": experiment_id, "status": status},
                  err)
        return json.dumps(err)
    if all(
        v is None for v in (status, result, landed_at, learning_carryforward)
    ):
        err = {
            "error": "NO_FIELDS",
            "instruction": (
                "update_experiment: no fields to update. Pass the actual "
                "revised content (status / result / learning_carryforward) "
                "— an experiment_id-only call changes nothing."
            ),
        }
        _log_call("update_experiment",
                  {"experiment_id": experiment_id}, err)
        return json.dumps(err)
    if status is not None:
        target["status"] = status
    if result is not None:
        target["result"] = result
    if landed_at is not None:
        target["landed_at"] = landed_at
    if learning_carryforward is not None:
        target["learning_carryforward"] = learning_carryforward
    target["updated_at"] = _now_iso()
    _save_state(state)
    _log_call(
        "update_experiment",
        {"experiment_id": experiment_id, "status": status, "result": result,
         "landed_at": landed_at,
         "learning_carryforward": learning_carryforward},
        {"ok": True, "experiment_id": experiment_id,
         "status": target.get("status")},
    )
    return json.dumps({"ok": True, "experiment_id": experiment_id,
                       "status": target.get("status")})


@conditional_tool()
def query_experiment_results(experiment_id: str) -> str:
    """
    Pull statistical results for an experiment — uplift, p-value, sample
    size, per-arm breakdowns.

    Args:
        experiment_id: Experiment id.

    Returns:
        JSON {experiment_id, metric, control, treatment, uplift_pts,
        p_value, n_total, arms: [{name, n, value}]}.
    """
    state = _load_state()
    saved = None
    for r in state.get("experiment_results", []):
        if r.get("experiment_id") == experiment_id:
            saved = r
            break
    if saved is None:
        err = {
            "error": "NOT_FOUND",
            "instruction": (
                f"query_experiment_results: no results for id={experiment_id!r}."
            ),
        }
        _log_call("query_experiment_results", {"experiment_id": experiment_id}, err)
        return json.dumps(err)
    _log_call(
        "query_experiment_results",
        {"experiment_id": experiment_id},
        {"uplift_pts": saved.get("uplift_pts")},
    )
    return json.dumps(saved, indent=2)


# ---- Growth-brief docs (Notion-style brief surface) ----
# Distinct from create_doc above — growth_briefs carry structured
# hypothesis/metric/timeline fields the brief surface treats as
# first-class, rather than free-form markdown.

@conditional_tool()
def create_growth_brief(
    brief_id: str,
    title: str,
    hypothesis: str,
    metric_of_record: str,
    cohort_scope: str | None = None,
    timeline: str | None = None,
    body: str | None = None,
) -> str:
    """
    Create a structured growth brief — Riley's working doc for an
    investigation or experiment. Carries explicit hypothesis, metric,
    and cohort fields the brief tool treats as first-class (in addition
    to free-form body).

    Args:
        brief_id: Stable identifier (e.g.
            "BRIEF-2026-02-midseg-churn-investigation").
        title: Human-readable title.
        hypothesis: Leading hypothesis as of brief creation.
        metric_of_record: Primary metric.
        cohort_scope: Optional cohort the brief is anchored on.
        timeline: Optional natural-language timeline.
        body: Optional free-form markdown body.

    Returns:
        JSON {ok, brief_id, url}.
    """
    state = _load_state()
    briefs = state.setdefault("growth_briefs", [])
    for i, b in enumerate(briefs):
        if b.get("brief_id") == brief_id:
            briefs.pop(i)
            break
    brief = {
        "brief_id": brief_id,
        "title": title,
        "hypothesis": hypothesis,
        "metric_of_record": metric_of_record,
        "cohort_scope": cohort_scope,
        "timeline": timeline,
        "body": body or "",
        "owner": "riley",
        "created_at": _now_iso(),
    }
    briefs.append(brief)
    _save_state(state)
    url = f"https://notion.mock/growth-briefs/{brief_id}"
    _log_call(
        "create_growth_brief",
        {"brief_id": brief_id, "title": title, "hypothesis": hypothesis,
         "metric_of_record": metric_of_record, "cohort_scope": cohort_scope,
         "timeline": timeline, "body": body},
        {"ok": True, "brief_id": brief_id, "url": url},
    )
    return json.dumps({"ok": True, "brief_id": brief_id, "url": url})


@conditional_tool()
def update_growth_brief(
    brief_id: str,
    hypothesis: str | None = None,
    metric_of_record: str | None = None,
    cohort_scope: str | None = None,
    timeline: str | None = None,
    body: str | None = None,
    status: str | None = None,
) -> str:
    """
    Update fields on a growth brief in place.

    Args:
        brief_id: Existing brief id.
        hypothesis: Optional revised hypothesis.
        metric_of_record: Optional revised primary metric.
        cohort_scope: Optional revised cohort scope.
        timeline: Optional revised timeline.
        body: Optional revised markdown body (replaces).
        status: Optional brief status — "open", "in_progress",
            "shipped", "closed".

    Returns:
        JSON {ok, brief_id} for the updated brief, or {error} on miss.
    """
    state = _load_state()
    briefs = state.get("growth_briefs") or []
    target = None
    for b in briefs:
        if b.get("brief_id") == brief_id:
            target = b
            break
    if target is None:
        err = {
            "error": "NOT_FOUND",
            "instruction": (
                f"update_growth_brief: no brief with id={brief_id!r}. "
                "Call create_growth_brief first."
            ),
        }
        _log_call(
            "update_growth_brief",
            {"brief_id": brief_id}, err,
        )
        return json.dumps(err)
    if all(
        v is None for v in (
            hypothesis, metric_of_record, cohort_scope,
            timeline, body, status,
        )
    ):
        err = {
            "error": "NO_FIELDS",
            "instruction": (
                "update_growth_brief: no fields to update. Pass the actual "
                "revised content (e.g. body=<the full note/text>, or a "
                "specific field) — a brief_id-only call changes nothing."
            ),
        }
        _log_call("update_growth_brief", {"brief_id": brief_id}, err)
        return json.dumps(err)
    if hypothesis is not None:
        target["hypothesis"] = hypothesis
    if metric_of_record is not None:
        target["metric_of_record"] = metric_of_record
    if cohort_scope is not None:
        target["cohort_scope"] = cohort_scope
    if timeline is not None:
        target["timeline"] = timeline
    if body is not None:
        target["body"] = body
    if status is not None:
        target["status"] = status
    target["updated_at"] = _now_iso()
    _save_state(state)
    _log_call(
        "update_growth_brief",
        {"brief_id": brief_id, "hypothesis": hypothesis,
         "metric_of_record": metric_of_record, "cohort_scope": cohort_scope,
         "timeline": timeline, "body": body, "status": status},
        {"ok": True, "brief_id": brief_id},
    )
    return json.dumps({"ok": True, "brief_id": brief_id})


if __name__ == "__main__":
    # Write a tiny startup marker so we know the server spawned
    startup_log = LOG_PATH.with_suffix(".startup.log")
    startup_log.parent.mkdir(parents=True, exist_ok=True)
    with startup_log.open("a") as f:
        f.write(f"{_now_iso()} startup run_id={RUN_ID} "
                f"state={STATE_PATH} log={LOG_PATH}\n")

    # Transport selection. Default to stdio (used by hermes/magent which
    # spawn the server as a subprocess). Set DOLPHINBENCH_MCP_TRANSPORT to "sse"
    # or "streamable-http" to listen on DOLPHINBENCH_MCP_PORT (default 3001) —
    # useful for external drivers that want an HTTP endpoint.
    transport = get_setting("DOLPHINBENCH_MCP_TRANSPORT", "stdio")
    port = int(get_setting("DOLPHINBENCH_MCP_PORT", "3001"))
    if transport in ("sse", "streamable-http"):
        # FastMCP reads its own `settings.port` at construction; mutate
        # it here so the selected transport binds to the requested port.
        try:
            mcp.settings.port = port  # type: ignore[attr-defined]
        except Exception:
            pass
        mcp.run(transport=transport)
    else:
        mcp.run()
