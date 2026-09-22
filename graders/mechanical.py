"""
Mechanical graders for DolphinBench — regex + tool-call-trace inspection.

Each grader returns a dict with:
    passed:      bool
    score:       float in [0, 1]
    details:     list of per-check dicts (name, ok, reason)
    grader_type: string
"""

from __future__ import annotations

import json
import re
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any


# =========================================================================
# Deprecated-primitive migration map — referenced in the warning text so
# rubric authors can find the replacement quickly. Kept here (not in
# docs/test_authoring_guide.md) so the migration list never drifts from
# the actual code.
# =========================================================================

_DEPRECATED_FIELD_CONTAINS_MSG = (
    "field_contains is ambiguous — it does set-membership when the "
    "field is a list and substring-search when it is a string. "
    "Use field_substring (substring in string OR in any list element) "
    "or field_in_list (exact element membership). "
    "See docs/test_authoring_guide.md."
)

_DEPRECATED_DATETIME_GTE_MSG = (
    "datetime_gte / datetime_lte are ambiguous — they compare absolute "
    "datetimes, but rubric authors often intend time-of-day or "
    "date-only comparisons. Use one of: time_of_day_gte, "
    "time_of_day_lte, date_on, date_on_or_after, date_on_or_before, "
    "datetime_absolute_gte, datetime_absolute_lte, weekday_in. "
    "See docs/test_authoring_guide.md."
)


# =========================================================================
# Path helpers — walk "args.start" style dotted paths through a dict
# =========================================================================

def _walk_path(obj: Any, path: str) -> Any:
    """Walk dotted path like 'args.to' through a nested dict/list."""
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _parse_iso(s: str) -> datetime | None:
    """Parse ISO datetime, tolerant of Z and +00:00."""
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# =========================================================================
# Tool-trace loading
# =========================================================================

def load_tool_calls(log_path: str | Path,
                    session_id: str | None = None) -> list[dict]:
    """Read MCP call log (JSONL) into a list of call records.

    When ``session_id`` is given, return only records stamped with that
    session. If no record carries the stamp (older logs, or the magent
    MCP wrapper didn't write the session marker), fall back to the full
    list — the caller's per-test log reset already isolates calls.
    """
    p = Path(log_path)
    if not p.exists():
        return []
    calls: list[dict] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            calls.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if session_id:
        filtered = [c for c in calls if c.get("session_id") == session_id]
        if filtered:
            return filtered
    return calls


# =========================================================================
# Regex grader — checks patterns in agent text output
# =========================================================================

def grade_regex(response_text: str, config: dict) -> dict:
    """
    Check regex patterns against agent response text.

    config:
        patterns_required:  list of {pattern, min_count?, max_count?, multiline?}
        patterns_forbidden: list of {pattern}  — ANY match = fail
        per_bullet_max_words: int — optional bullet length cap
        max_bullets: int — response must have at most N top-level bullet lines
        min_words: int — response must have at least N whitespace-split words
        max_words: int — response must have at most N whitespace-split words
        line_order_required: list[str] — substrings that must appear in order
    """
    details: list[dict] = []
    passed_checks = 0
    total_checks = 0

    # Required patterns
    for p in config.get("patterns_required", []) or []:
        total_checks += 1
        pat = p["pattern"] if isinstance(p, dict) else p
        flags = re.MULTILINE if (isinstance(p, dict) and p.get("multiline")) else 0
        matches = re.findall(pat, response_text, flags)
        count = len(matches)
        min_c = p.get("min_count", 1) if isinstance(p, dict) else 1
        max_c = p.get("max_count") if isinstance(p, dict) else None
        ok = count >= min_c and (max_c is None or count <= max_c)
        details.append({
            "name": f"required:{pat[:40]}",
            "ok": ok,
            "reason": f"found {count} match(es), need {min_c}"
                      + (f"-{max_c}" if max_c is not None else "+"),
        })
        if ok:
            passed_checks += 1

    # Forbidden patterns — any match is a fail
    for p in config.get("patterns_forbidden", []) or []:
        total_checks += 1
        pat = p["pattern"] if isinstance(p, dict) else p
        flags = re.MULTILINE if (isinstance(p, dict) and p.get("multiline")) else 0
        matches = re.findall(pat, response_text, flags)
        ok = len(matches) == 0
        details.append({
            "name": f"forbidden:{pat[:40]}",
            "ok": ok,
            "reason": "clean" if ok else f"forbidden pattern matched {len(matches)}x",
        })
        if ok:
            passed_checks += 1

    # Whole-response word floor (T1-style "must be detailed")
    if "min_words" in config:
        total_checks += 1
        floor = int(config["min_words"])
        word_count = len(response_text.split())
        ok = word_count >= floor
        details.append({
            "name": f"min_words:{floor}",
            "ok": ok,
            "reason": f"{word_count} word(s), need >= {floor}",
        })
        if ok:
            passed_checks += 1

    # Whole-response word cap (T8-style "must be concise")
    if "max_words" in config:
        total_checks += 1
        cap = int(config["max_words"])
        word_count = len(response_text.split())
        ok = word_count <= cap
        details.append({
            "name": f"max_words:{cap}",
            "ok": ok,
            "reason": f"{word_count} word(s), need <= {cap}",
        })
        if ok:
            passed_checks += 1

    # Per-bullet word cap (H1-style)
    if "per_bullet_max_words" in config:
        total_checks += 1
        cap = config["per_bullet_max_words"]
        bullets = re.findall(r"^\s*[-*•]\s+(.+)$", response_text, re.MULTILINE)
        over = [b for b in bullets if len(b.split()) > cap]
        ok = len(over) == 0 and len(bullets) > 0
        details.append({
            "name": f"per_bullet_max_words:{cap}",
            "ok": ok,
            "reason": f"{len(bullets)} bullets, {len(over)} over cap",
        })
        if ok:
            passed_checks += 1

    # Max-bullet count (T8-style "≈ 5 bullets max"). Vacuously passes
    # if the response uses prose with no bullets — paired with max_words
    # the conciseness intent is still enforced.
    if "max_bullets" in config:
        total_checks += 1
        cap = int(config["max_bullets"])
        bullets = re.findall(r"^\s*[-*•]\s+(.+)$", response_text, re.MULTILINE)
        ok = len(bullets) <= cap
        details.append({
            "name": f"max_bullets:{cap}",
            "ok": ok,
            "reason": f"{len(bullets)} bullet(s), need <= {cap}",
        })
        if ok:
            passed_checks += 1

    # Line-order check (A5-style)
    if "line_order_required" in config:
        total_checks += 1
        order = config["line_order_required"]
        last_pos = -1
        ok = True
        missing: list[str] = []
        for needle in order:
            pos = response_text.find(needle, last_pos + 1)
            if pos == -1:
                ok = False
                missing.append(needle)
                break
            last_pos = pos
        details.append({
            "name": "line_order_required",
            "ok": ok,
            "reason": "in order" if ok else f"missing/out-of-order: {missing}",
        })
        if ok:
            passed_checks += 1

    all_passed = passed_checks == total_checks and total_checks > 0
    return {
        "grader_type": "regex",
        "passed": all_passed,
        # Binary score under all-required grading — see llm_judge.py
        # for rationale. Per-check ratios still in passed/total below.
        "score": 1.0 if all_passed else 0.0,
        "details": details,
        "total_checks": total_checks,
        "passed_checks": passed_checks,
    }


# =========================================================================
# Tool-trace grader — checks MCP call log for expected calls and args
# =========================================================================

def _call_is_ok(call: dict) -> bool:
    """Return True if a tool call result indicates success.

    Handles a few common shapes:
      - {"result": {"ok": True, ...}}
      - {"ok": True}
      - {"result": {"error": ...}} → False
      - {"error": ...} → False
      - {"result": {"isError": True}} → False (MCP convention)
    Conservative: if no error indicator is present, treat as ok.
    """
    if not isinstance(call, dict):
        return False
    # Direct top-level ok flag
    if call.get("ok") is True:
        return True
    if call.get("ok") is False:
        return False
    # Top-level error
    if call.get("error") is not None:
        return False
    result = call.get("result")
    if isinstance(result, dict):
        if result.get("ok") is True:
            return True
        if result.get("ok") is False:
            return False
        if result.get("error") is not None:
            return False
        if result.get("isError") is True:
            return False
    # Fallback — no negative signal, count as success
    return True


def _to_text(v) -> str:
    """Flatten any arg value to searchable text so a regex assertion can match
    content inside lists/dicts/numbers, not only bare strings. Agents legitimately
    put values in list args (items=[...], attendees=[...], cohort_scope=[...]); the
    old `isinstance(actual, str)` guard silently failed every such correct action."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return " ".join(_to_text(x) for x in v)
    if isinstance(v, dict):
        return " ".join(f"{k} {_to_text(val)}" for k, val in v.items())
    return str(v)


def _search_tolerant(regex: "re.Pattern", text: str) -> bool:
    """regex.search with separator-only tolerance.

    A correct answer often writes "30-day" where the pattern has "30 day"
    (or "value per seat" vs "value-per-seat"). Collapse runs of hyphens/
    spaces to a single space on BOTH sides and retry — this only loosens
    separator matching, never content. (The former id-prefix-stripping
    fallback was removed: entity ids are system-generated, so grader
    patterns are authored from the canonical key and match exactly.)"""
    if regex.search(text):
        return True
    def _norm_sep(s: str) -> str:
        return re.sub(r"[-\s]+", " ", s)
    npat, ntext = _norm_sep(regex.pattern), _norm_sep(text)
    if npat != regex.pattern or ntext != text:
        try:
            if re.compile(npat, regex.flags).search(ntext):
                return True
        except re.error:
            pass
    return False


# Same-store mutation synonyms: two legitimate tools for the same action. A
# content check binds to ONE name, but a correct agent may use the sibling
# (create vs update an entry; review-with-comment vs plain comment) — the tool
# CHOICE between siblings is not load-bearing, the recalled VALUE is. Matching
# accepts the family. Deliberately NOT families: communication channels
# (email/sms/slack — channel choice IS load-bearing) and archive vs delete.
_TOOL_FAMILIES = [
    {"create_runbook_entry", "update_runbook_entry"},
    {"create_growth_brief", "update_growth_brief"},
    {"create_calendar_event", "update_calendar_event"},
    {"create_experiment", "update_experiment"},
    {"post_pr_comment", "review_pr"},
]
_TOOL_FAMILY = {t: fam for fam in _TOOL_FAMILIES for t in fam}


def _tool_matches(actual: str | None, want: str | None) -> bool:
    """True if the call's tool satisfies the assertion's tool — exact name or
    a same-family mutation synonym."""
    if actual == want:
        return True
    return want in _TOOL_FAMILY and actual in _TOOL_FAMILY[want]


def _evaluate_assertion(a: dict, tool_calls: list[dict],
                        target_calls: list[dict],
                        required_tool: str | None,
                        today: str | None = None,
                        test_message: str = "",
                        raise_on_judge_error: bool = False) -> tuple[bool, str]:
    """Evaluate one assertion dict against a tool-call trace.

    Returns (ok, reason). Pulled out so it can be reused by both
    pass/fail `assertions` and logged-only `diagnostic_assertions`.
    """
    atype = a.get("type")
    ok = False
    reason = "not evaluated"

    # on_call: "first" restricts the check to the first call to the
    # named tool. Useful in diagnostics like "did the FIRST PR body
    # include the Linear ticket id?".
    on_call = a.get("on_call")

    def _calls_for_tool(tool_name: str) -> list[dict]:
        matches = [c for c in tool_calls if _tool_matches(c.get("tool"), tool_name)]
        if on_call == "first":
            matches = matches[:1]
        return matches

    def _make_regex(pattern: str, flags_str: str | None):
        flags = 0
        if flags_str:
            for ch in flags_str:
                if ch == "i":
                    flags |= re.IGNORECASE
                elif ch == "m":
                    flags |= re.MULTILINE
                elif ch == "s":
                    flags |= re.DOTALL
        return re.compile(pattern, flags)

    if atype == "field_equals":
        # Must match against at least ONE of the target calls
        case = a.get("case_sensitive", True)
        expected = a["value"]
        if a.get("tool"):
            check_calls = _calls_for_tool(a["tool"])
        else:
            check_calls = target_calls if required_tool else tool_calls
        for c in check_calls:
            actual = _walk_path(c, a["path"])
            if not case and isinstance(actual, str) and isinstance(expected, str):
                if actual.lower() == expected.lower():
                    ok, reason = True, f"matched in call {c.get('tool')}"
                    break
            elif actual == expected:
                ok, reason = True, f"matched in call {c.get('tool')}"
                break
        if not ok:
            found = [_walk_path(c, a["path"]) for c in check_calls]
            reason = f"expected {expected!r}, got {found}"

    elif atype == "recipient_matches":
        from graders.llm_judge import judge_field_value

        expected = a["value"]
        check_calls = _calls_for_tool(a["tool"]) if a.get("tool") else tool_calls
        reasons: list[str] = []
        for c in check_calls:
            actual = _walk_path(c, a["path"])
            if actual == expected:
                ok, reason = True, "matched the recipient supplied in the request"
                break
            verdict = judge_field_value(
                actual,
                (
                    f"Does this value unambiguously identify the same recipient as "
                    f"{expected!r}? Accept only a name, address, or account handle for "
                    "that same person."
                ),
                context=(f"Today's date is {today}." if today else ""),
                **({"config": {"raise_on_error": True}} if raise_on_judge_error else {}),
            )
            if verdict.get("ok"):
                ok = True
                reason = verdict.get("reason", "recipient identity matched")
                break
            reasons.append(verdict.get("reason", "recipient identity did not match"))
        if not ok:
            reason = "; ".join(reasons[:2]) if reasons else "no matching tool calls"

    elif atype == "email_recipients_received":
        from graders.llm_judge import judge_field_value

        expected = a.get("values")
        if not isinstance(expected, list) or not expected or any(
            not isinstance(value, str) or not value.strip() for value in expected
        ):
            return False, "email delivery needs a non-empty list of recipients"
        reasons = []
        for call in _calls_for_tool(a["tool"]):
            arguments = call.get("args") or {}
            recipients = {key: arguments.get(key) for key in ("to", "cc", "bcc")}
            addresses = [
                value.strip().casefold()
                for values in recipients.values()
                for value in (values if isinstance(values, list) else [values])
                if isinstance(value, str) and value.strip()
            ]
            if all(value.strip().casefold() in addresses for value in expected):
                ok, reason = True, "every required recipient received this email"
                break
            if not addresses:
                continue
            verdict = judge_field_value(
                recipients,
                f"Do the actual To, CC, and BCC recipients include every recipient in {expected!r}? "
                "Accept unambiguous names, addresses, or handles for the same recipients. "
                "The recipients may be together or split across these headers. "
                "Additional recipients are allowed. A body mention is not delivery.",
                context=f"User request: {test_message}",
                **({"config": {"raise_on_error": True}} if raise_on_judge_error else {}),
            )
            if verdict.get("ok"):
                ok, reason = True, verdict.get("reason", "recipient identities matched")
                break
            reasons.append(verdict.get("reason", "a required recipient did not receive this email"))
        if not ok:
            reason = "; ".join(reasons[:2]) or "no email delivered to every required recipient"

    elif atype == "field_list_includes":
        expected = a.get("values")
        check_calls = _calls_for_tool(a["tool"])
        if isinstance(expected, list) and expected:
            for call in check_calls:
                actual = _walk_path(call, a["path"])
                if isinstance(actual, list) and all(value in actual for value in expected):
                    ok, reason = True, "list contains every required value"
                    break
        if not ok:
            reason = f"list at {a['path']} does not contain every required value {expected!r}"

    elif atype == "datetime_absolute_eq":
        expected = _parse_iso(a.get("value"))
        if expected is not None and expected.utcoffset() is not None:
            for call in _calls_for_tool(a["tool"]):
                actual = _parse_iso(_walk_path(call, a["path"]))
                if actual is not None and actual.utcoffset() is not None and actual == expected:
                    ok, reason = True, "timestamps identify the same instant"
                    break
        if not ok:
            reason = "no matching timezone-aware instant"

    elif atype == "field_absent_or_empty":
        check_calls = _calls_for_tool(a["tool"]) if a.get("tool") else tool_calls
        if not check_calls:
            ok, reason = False, "no matching tool calls"
        else:
            non_empty = []
            for c in check_calls:
                actual = _walk_path(c, a["path"])
                if actual not in (None, "", [], {}):
                    non_empty.append(actual)
            ok = not non_empty
            reason = "absent or empty" if ok else f"found non-empty values {non_empty}"

    elif atype == "field_substring":
        # Pass iff `value` (a string) appears as a substring of the
        # field at `path`, OR as a substring of any element in the
        # field if the field is a list-of-strings.
        expected = a["value"]
        if a.get("tool"):
            check_calls = _calls_for_tool(a["tool"])
        else:
            check_calls = target_calls if required_tool else tool_calls
        for c in check_calls:
            actual = _walk_path(c, a["path"])
            if isinstance(actual, str) and expected in actual:
                ok, reason = True, "substring found in string"
                break
            if isinstance(actual, list):
                hit = next(
                    (e for e in actual
                     if isinstance(e, str) and expected in e),
                    None,
                )
                if hit is not None:
                    ok, reason = True, f"substring found in list element {hit!r}"
                    break
        if not ok:
            reason = f"{expected!r} not found as substring at {a['path']}"

    elif atype == "field_in_list":
        # Pass iff `value` is an exact element of the list at `path`.
        # Field MUST be a list — strings/None fail.
        expected = a["value"]
        if a.get("tool"):
            check_calls = _calls_for_tool(a["tool"])
        else:
            check_calls = target_calls if required_tool else tool_calls
        for c in check_calls:
            actual = _walk_path(c, a["path"])
            if isinstance(actual, list) and expected in actual:
                ok, reason = True, "exact element found in list"
                break
        if not ok:
            reason = f"{expected!r} not an element of list at {a['path']}"

    elif atype == "field_contains":
        # Deprecated alias — silently delegates to the old "is it in
        # the list OR is it a substring" behavior so existing test
        # YAMLs keep grading the same way until the migration
        # subagent rewrites them. Emits a DeprecationWarning so
        # CI / rubric-authoring runs surface the issue.
        warnings.warn(_DEPRECATED_FIELD_CONTAINS_MSG, DeprecationWarning,
                      stacklevel=2)
        expected = a["value"]
        if a.get("tool"):
            check_calls = _calls_for_tool(a["tool"])
        else:
            check_calls = target_calls if required_tool else tool_calls
        for c in check_calls:
            actual = _walk_path(c, a["path"])
            if isinstance(actual, list) and expected in actual:
                ok, reason = True, "found in list"
                break
            elif isinstance(actual, str) and expected in actual:
                ok, reason = True, "found in string"
                break
        if not ok:
            reason = f"{expected!r} not found at {a['path']}"

    elif atype == "field_not_contains":
        # Pass if the value is NOT found in the field across ALL target calls
        expected = a["value"]
        if a.get("tool"):
            check_calls = _calls_for_tool(a["tool"])
        else:
            check_calls = target_calls if required_tool else tool_calls
        found_match = False
        for c in check_calls:
            actual = _walk_path(c, a["path"])
            if isinstance(actual, list) and expected in actual:
                found_match = True
                break
            elif isinstance(actual, str) and expected in actual:
                found_match = True
                break
        ok = not found_match
        reason = "clean (not found)" if ok else f"{expected!r} found at {a['path']}"

    elif atype in ("datetime_absolute_gte", "datetime_absolute_lte"):
        # Compare full ISO datetimes (date + time + tz). This is what
        # the deprecated `datetime_gte` was always doing — exposing it
        # under an unambiguous name so authors can opt in deliberately.
        expected_dt = _parse_iso(a["value"])
        check_calls = target_calls if required_tool else tool_calls
        for c in check_calls:
            actual = _walk_path(c, a["path"])
            actual_dt = _parse_iso(actual) if isinstance(actual, str) else None
            if actual_dt and expected_dt:
                if atype == "datetime_absolute_gte" and actual_dt >= expected_dt:
                    ok, reason = True, f"{actual} >= {a['value']}"
                    break
                if atype == "datetime_absolute_lte" and actual_dt <= expected_dt:
                    ok, reason = True, f"{actual} <= {a['value']}"
                    break
        if not ok:
            reason = (f"no call had {a['path']} "
                      f"{'>=' if atype.endswith('gte') else '<='} {a['value']}")

    elif atype in ("time_of_day_gte", "time_of_day_lte"):
        # Compare ONLY the time-of-day component of the field's
        # datetime against an HH:MM value. Date is ignored — this is
        # the primitive P2 should have used for "after 10am".
        # We use whatever timezone is encoded in the field's ISO
        # string (datetime.fromisoformat preserves it); the comparison
        # is against the wall-clock time in that same zone.
        try:
            hh, mm = a["value"].split(":")
            expected_h, expected_m = int(hh), int(mm)
        except (ValueError, KeyError, AttributeError):
            ok, reason = False, f"invalid HH:MM value: {a.get('value')!r}"
        else:
            check_calls = target_calls if required_tool else tool_calls
            for c in check_calls:
                actual = _walk_path(c, a["path"])
                actual_dt = _parse_iso(actual) if isinstance(actual, str) else None
                if actual_dt is None:
                    continue
                actual_minutes = actual_dt.hour * 60 + actual_dt.minute
                expected_minutes = expected_h * 60 + expected_m
                if atype == "time_of_day_gte" and actual_minutes >= expected_minutes:
                    ok, reason = True, (
                        f"{actual_dt.hour:02d}:{actual_dt.minute:02d} "
                        f">= {a['value']}"
                    )
                    break
                if atype == "time_of_day_lte" and actual_minutes <= expected_minutes:
                    ok, reason = True, (
                        f"{actual_dt.hour:02d}:{actual_dt.minute:02d} "
                        f"<= {a['value']}"
                    )
                    break
            if not ok:
                reason = (f"no call had {a['path']} time-of-day "
                          f"{'>=' if atype.endswith('gte') else '<='} {a['value']}")

    elif atype in ("date_on", "date_on_or_after", "date_on_or_before"):
        # Compare ONLY the date component (YYYY-MM-DD), ignoring
        # time-of-day and timezone. Useful for "the booking was on
        # Friday" / "the meeting was scheduled on or after the
        # offsite end date" rubrics.
        try:
            expected_date = datetime.strptime(a["value"], "%Y-%m-%d").date()
        except (ValueError, KeyError, TypeError):
            ok, reason = False, f"invalid YYYY-MM-DD value: {a.get('value')!r}"
        else:
            check_calls = target_calls if required_tool else tool_calls
            for c in check_calls:
                actual = _walk_path(c, a["path"])
                actual_dt = _parse_iso(actual) if isinstance(actual, str) else None
                if actual_dt is None:
                    continue
                actual_date = actual_dt.date()
                if atype == "date_on" and actual_date == expected_date:
                    ok, reason = True, f"{actual_date} == {expected_date}"
                    break
                if atype == "date_on_or_after" and actual_date >= expected_date:
                    ok, reason = True, f"{actual_date} >= {expected_date}"
                    break
                if atype == "date_on_or_before" and actual_date <= expected_date:
                    ok, reason = True, f"{actual_date} <= {expected_date}"
                    break
            if not ok:
                op = {"date_on": "==", "date_on_or_after": ">=",
                      "date_on_or_before": "<="}[atype]
                reason = f"no call had {a['path']} date {op} {a['value']}"

    elif atype == "weekday_in":
        # Pass iff the date component of the field's datetime falls
        # on one of the named weekdays. `value` is a list of full
        # weekday names (Monday..Sunday); case-insensitive.
        wanted_raw = a.get("value", []) or []
        if not isinstance(wanted_raw, list):
            wanted_raw = [wanted_raw]
        wanted = {str(w).strip().lower() for w in wanted_raw}
        weekday_names = ["monday", "tuesday", "wednesday", "thursday",
                         "friday", "saturday", "sunday"]
        check_calls = target_calls if required_tool else tool_calls
        seen: list[str] = []
        for c in check_calls:
            actual = _walk_path(c, a["path"])
            actual_dt = _parse_iso(actual) if isinstance(actual, str) else None
            if actual_dt is None:
                continue
            wd = weekday_names[actual_dt.weekday()]
            seen.append(wd)
            if wd in wanted:
                ok, reason = True, f"{actual_dt.date()} is {wd.capitalize()}"
                break
        if not ok:
            reason = (f"no call had {a['path']} on any of "
                      f"{sorted(wanted)}; saw {seen}")

    elif atype in ("datetime_gte", "datetime_lte"):
        # Deprecated alias — preserves the old absolute-datetime
        # comparison so existing test YAMLs keep grading the same
        # way until the migration subagent rewrites them. Emits a
        # DeprecationWarning pointing rubric authors to the named
        # replacements (time_of_day_gte / date_on_or_after / etc.).
        warnings.warn(_DEPRECATED_DATETIME_GTE_MSG, DeprecationWarning,
                      stacklevel=2)
        expected_dt = _parse_iso(a["value"])
        check_calls = target_calls if required_tool else tool_calls
        for c in check_calls:
            actual = _walk_path(c, a["path"])
            actual_dt = _parse_iso(actual) if isinstance(actual, str) else None
            if actual_dt and expected_dt:
                if atype == "datetime_gte" and actual_dt >= expected_dt:
                    ok, reason = True, f"{actual} >= {a['value']}"
                    break
                if atype == "datetime_lte" and actual_dt <= expected_dt:
                    ok, reason = True, f"{actual} <= {a['value']}"
                    break
        if not ok:
            reason = f"no call had {a['path']} {atype[-3:]} {a['value']}"

    elif atype == "duration_eq_minutes":
        expected_min = a["minutes"]
        check_calls = target_calls if required_tool else tool_calls
        for c in check_calls:
            s = _parse_iso(_walk_path(c, a["start_path"]))
            e = _parse_iso(_walk_path(c, a["end_path"]))
            if s and e:
                diff_min = (e - s).total_seconds() / 60
                if abs(diff_min - expected_min) < 0.5:
                    ok, reason = True, f"{diff_min:.0f} min"
                    break
        if not ok:
            reason = f"no call with {expected_min}-min duration"

    elif atype == "tool_not_called":
        tool_name = a["tool"]
        matches = [c for c in tool_calls if _tool_matches(c.get("tool"), tool_name)]
        ok = len(matches) == 0
        reason = "clean" if ok else f"{tool_name} called {len(matches)}x"

    elif atype == "tool_called_at_least":
        tool_name = a["tool"]
        min_n = a.get("count", 1)
        matches = [c for c in tool_calls if _tool_matches(c.get("tool"), tool_name)]
        ok = len(matches) >= min_n
        reason = f"{len(matches)} call(s), need >= {min_n}"

    elif atype == "tool_call_count":
        # Count after the assertion's tool filter. This is intentionally exact:
        # authoring uses it only when an extra or missing side effect is wrong.
        tool_name = a["tool"]
        expected = a["count"]
        matches = [
            call for call in tool_calls if _tool_matches(call.get("tool"), tool_name)
        ]
        ok = len(matches) == expected
        reason = f"{len(matches)} call(s), need exactly {expected}"

    elif atype == "tool_called":
        # Simple "was this tool called at all?" check — equivalent to
        # tool_called_at_least with count=1 but reads more naturally
        # for tests that just need to assert a tool fired. Supports an
        # optional `before_tool` modifier ("did X get called before
        # the first Y?"), used for IMP-F's encode-before-search check.
        tool_name = a["tool"]
        before_tool = a.get("before_tool")
        if before_tool:
            # Find first index of `before_tool`; check whether `tool`
            # was called at any index strictly less than that.
            before_idx: int | None = None
            for i, c in enumerate(tool_calls):
                if _tool_matches(c.get("tool"), before_tool):
                    before_idx = i
                    break
            if before_idx is None:
                ok = False
                reason = f"{before_tool} never called, can't compare ordering"
            else:
                earlier = [c for i, c in enumerate(tool_calls)
                           if i < before_idx and _tool_matches(c.get("tool"), tool_name)]
                ok = len(earlier) >= 1
                reason = (f"{tool_name} called before {before_tool}"
                          if ok else
                          f"{tool_name} not called before first {before_tool}")
        else:
            matches = [c for c in tool_calls if _tool_matches(c.get("tool"), tool_name)]
            ok = len(matches) >= 1
            reason = "called" if ok else "not called"

    elif atype in ("field_lte", "field_gte", "field_eq_number"):
        # Numeric comparisons against an args.X path. Handy for
        # budgets, counts, durations that aren't datetimes.
        expected = a["value"]
        check_calls = target_calls if required_tool else tool_calls
        for c in check_calls:
            actual = _walk_path(c, a["path"])
            try:
                actual_num = float(actual)
                expected_num = float(expected)
            except (TypeError, ValueError):
                continue
            if atype == "field_lte" and actual_num <= expected_num:
                ok, reason = True, f"{actual_num} <= {expected_num}"
                break
            if atype == "field_gte" and actual_num >= expected_num:
                ok, reason = True, f"{actual_num} >= {expected_num}"
                break
            if atype == "field_eq_number" and abs(actual_num - expected_num) < 1e-6:
                ok, reason = True, f"{actual_num} == {expected_num}"
                break
        if not ok:
            reason = f"no call satisfied {a['path']} {atype}={expected}"

    elif atype == "datetime_not_between":
        # Asserts the field's datetime value does NOT fall between
        # two ISO bounds (inclusive). Useful for blackout windows
        # like "avoid the offsite dates".
        lo = _parse_iso(a["start"])
        hi = _parse_iso(a["end"])
        check_calls = target_calls if required_tool else tool_calls
        ok, reason = True, "no matching calls (vacuously ok)"
        for c in check_calls:
            actual = _walk_path(c, a["path"])
            actual_dt = _parse_iso(actual) if isinstance(actual, str) else None
            if actual_dt and lo and hi and lo <= actual_dt <= hi:
                ok = False
                reason = f"{actual} falls inside blackout window {a['start']}..{a['end']}"
                break
            if actual_dt:
                reason = f"{actual} outside blackout"

    elif atype == "field_llm_judge":
        # LLM-judge a specific field value against a free-form
        # criterion. Local import so we don't pay the cost when no
        # field_llm_judge assertions are in the rubric.
        from graders.llm_judge import judge_field_value
        # Honor per-assertion `tool:` filter (mirrors field_equals/
        # field_substring/field_in_list). Without this, the judge
        # would run against every tool call in the trace and could
        # mask failures or produce false fails by reading the wrong
        # tool's args. See docs/v1_to_v1_1_flip_audit.md (H8) for
        # the bug write-up.
        if a.get("tool"):
            check_calls = _calls_for_tool(a["tool"])
        else:
            check_calls = target_calls if required_tool else tool_calls
        if not check_calls:
            ok, reason = False, "no tool calls to judge"
        else:
            # Grade the first matching tool call (same heuristic as
            # datetime_gte etc.) — if there are multiple, pass iff
            # ANY of them satisfy the criterion.
            reasons: list[str] = []
            # Give the judge the NARRATIVE "today" so date-relative criteria
            # ("tomorrow", "next week") resolve against the simulated present,
            # not the real wall-clock date (which silently failed correct
            # events scheduled on the narrative-tomorrow).
            _ctx = (f"Today's date is {today}. " if today else "") + a.get("context", "")
            for c in check_calls:
                actual = _walk_path(c, a["path"])
                action_context = json.dumps({
                    "user_request": test_message,
                    "tool": c.get("tool"),
                    "arguments": c.get("args", {}),
                    "field_being_checked": a["path"],
                }, default=str)
                verdict = judge_field_value(
                    actual, a["criterion"],
                    context=f"{_ctx}\nAction context (JSON):\n{action_context}",
                    **({"config": {"raise_on_error": True}} if raise_on_judge_error else {}),
                )
                if verdict.get("ok"):
                    ok = True
                    reason = verdict.get("reason", "llm-judge pass")
                    break
                reasons.append(verdict.get("reason", "llm-judge fail"))
            if not ok:
                reason = "; ".join(reasons[:2]) if reasons else "llm-judge: no calls matched"

    # ---------------- New IMP-suite assertion types ----------------

    elif atype == "tool_call_succeeded":
        # Pass iff at least one call to `tool` returned an ok result
        # (not an error). Used for "the agent eventually completed
        # the task" loose-accuracy grading.
        tool_name = a["tool"]
        matches = _calls_for_tool(tool_name)
        successes = [c for c in matches if _call_is_ok(c)]
        ok = len(successes) >= 1
        reason = (f"{len(successes)}/{len(matches)} call(s) succeeded"
                  if matches else f"{tool_name} never called")

    elif atype == "tool_call_succeeded_one_of":
        # Pass iff at least one call to ANY tool in `tools` returned
        # an ok result. IMP-L: "metrics_fetch OR metrics_fetch_batch
        # eventually returned data".
        tools_list = a.get("tools", [])
        successes: list[dict] = []
        for c in tool_calls:
            if c.get("tool") in tools_list and _call_is_ok(c):
                successes.append(c)
        ok = len(successes) >= 1
        reason = (f"{len(successes)} successful call(s) across {tools_list}"
                  if ok else f"no successful call to any of {tools_list}")

    elif atype == "tool_called_one_of":
        # Pass iff any call was made to ANY tool in the list. Used
        # for "deploy via either kubectl OR git push".
        tools_list = a.get("tools", [])
        matches = [c for c in tool_calls if c.get("tool") in tools_list]
        ok = len(matches) >= 1
        reason = (f"{len(matches)} call(s) across {tools_list}"
                  if ok else f"none of {tools_list} called")

    elif atype == "field_regex":
        # Pass iff the value at `path` (optionally restricted to a
        # specific tool) matches the regex.
        pattern = a["pattern"]
        regex = _make_regex(pattern, a.get("flags"))
        if a.get("tool"):
            check_calls = _calls_for_tool(a["tool"])
        else:
            check_calls = target_calls if required_tool else tool_calls
        for c in check_calls:
            actual = _walk_path(c, a["path"])
            if _search_tolerant(regex, _to_text(actual)):
                ok = True
                reason = f"regex matched in {c.get('tool')}"
                break
        if not ok:
            reason = f"regex {pattern!r} did not match {a['path']}"

    elif atype == "field_not_regex":
        # Pass iff NO matching call's `path` value matches the regex.
        pattern = a["pattern"]
        regex = _make_regex(pattern, a.get("flags"))
        if a.get("tool"):
            check_calls = _calls_for_tool(a["tool"])
        else:
            check_calls = target_calls if required_tool else tool_calls
        bad: dict | None = None
        for c in check_calls:
            actual = _walk_path(c, a["path"])
            if _search_tolerant(regex, _to_text(actual)):
                bad = c
                break
        ok = bad is None
        reason = ("clean (no match)" if ok
                  else f"regex {pattern!r} matched in {bad.get('tool') if bad else '?'}")

    elif atype == "tool_called_with_arg_regex":
        # Convenience: tool was called AND its arg at `path` (or
        # `arg_path`) matched the regex (or `arg_value_regex`). The
        # pair of field names exists because the IMP YAMLs use
        # `arg_path` / `arg_value_regex` while the user spec uses
        # `path` / `pattern`. We accept both.
        tool_name = a["tool"]
        path = a.get("path") or a.get("arg_path")
        pattern = a.get("pattern") or a.get("arg_value_regex")
        regex = _make_regex(pattern, a.get("flags"))
        matches = _calls_for_tool(tool_name)
        if not matches:
            ok, reason = False, f"{tool_name} not called"
        else:
            for c in matches:
                actual = _walk_path(c, path)
                if _search_tolerant(regex, _to_text(actual)):
                    ok = True
                    reason = f"matched regex in {tool_name} call"
                    break
            if not ok:
                seen = [_walk_path(c, path) for c in matches]
                reason = f"no {tool_name} call had {path} matching {pattern!r}; saw {seen}"

    elif atype == "tool_called_with_value":
        # Tool was called AND the fact's value appears SOMEWHERE in its args
        # (no path -- searches the whole flattened args). Robust to which field
        # the agent chose; measures "the fact was applied", not an exact location.
        tool_name = a["tool"]
        pattern = a.get("pattern") or a.get("value_regex")
        regex = _make_regex(pattern, a.get("flags"))
        matches = _calls_for_tool(tool_name)
        if not matches:
            ok, reason = False, f"{tool_name} not called"
        else:
            for c in matches:
                if _search_tolerant(regex, _to_text(c.get("args"))):
                    ok, reason = True, f"value matched in {tool_name} args"
                    break
            if not ok:
                reason = (f"no {tool_name} call had args matching {pattern!r}; "
                          f"saw {[_to_text(c.get('args')) for c in matches]}")

    elif atype == "tool_not_called_with_arg_regex":
        # Pass iff NO call to `tool` had its arg at `path` matching
        # the regex. Useful for "first call must NOT contain
        # `kubectl apply`" diagnostics.
        tool_name = a["tool"]
        path = a.get("path") or a.get("arg_path")
        pattern = a.get("pattern") or a.get("arg_value_regex")
        regex = _make_regex(pattern, a.get("flags"))
        matches = _calls_for_tool(tool_name)
        offender: dict | None = None
        for c in matches:
            actual = _walk_path(c, path)
            if _search_tolerant(regex, _to_text(actual)):
                offender = c
                break
        ok = offender is None
        if ok:
            reason = ("clean (no match)" if matches
                      else f"{tool_name} not called (vacuously ok)")
        else:
            reason = f"{tool_name} call had {path} matching forbidden {pattern!r}"

    elif atype == "tool_called_with_sorted_arg":
        # Pass iff `tool` was called AND the arg at `path` is a list
        # whose elements are monotonically non-decreasing. Used for
        # IMP-E (batch_dedup with sorted input diagnostic).
        tool_name = a["tool"]
        path = a.get("path") or a.get("arg_path")
        matches = _calls_for_tool(tool_name)
        if not matches:
            ok, reason = False, f"{tool_name} not called"
        else:
            for c in matches:
                actual = _walk_path(c, path)
                if isinstance(actual, list) and len(actual) >= 2:
                    try:
                        sorted_ok = all(actual[i] <= actual[i + 1]
                                        for i in range(len(actual) - 1))
                    except TypeError:
                        sorted_ok = False
                    if sorted_ok:
                        ok = True
                        reason = f"{path} is sorted ({len(actual)} items)"
                        break
                elif isinstance(actual, list) and len(actual) <= 1:
                    # Trivially sorted
                    ok = True
                    reason = f"{path} is trivially sorted"
                    break
            if not ok:
                reason = f"no {tool_name} call had {path} as a sorted list"

    elif atype == "tool_called_with_args":
        # IMP-B uses this: tool was called with ALL of the given
        # args.<key> = value pairs (subset match). Args block is a
        # flat dict that maps to args.<key> on the call.
        tool_name = a["tool"]
        wanted = a.get("args", {}) or {}
        matches = _calls_for_tool(tool_name)
        if not matches:
            ok, reason = False, f"{tool_name} not called"
        else:
            for c in matches:
                ok_call = True
                for k, v in wanted.items():
                    actual = _walk_path(c, f"args.{k}")
                    # Loose equality — coerce numbers to handle
                    # 8500 vs 8500.0 vs "8500" mock differences.
                    if actual != v:
                        try:
                            if float(actual) == float(v):
                                continue
                        except (TypeError, ValueError):
                            pass
                        ok_call = False
                        break
                if ok_call:
                    ok = True
                    reason = f"matched args {wanted} in a {tool_name} call"
                    break
            if not ok:
                reason = f"no {tool_name} call satisfied args {wanted}"

    elif atype == "tool_called_with_arg":
        # IMP-B / IMP-F use this for diagnostics: tool was called
        # AND its arg at `arg_path` (or `path`) matched
        # `arg_value_equals` (single value) or `arg_value_one_of`
        # (any of a list). Honors `on_call: first` for "did the
        # FIRST call carry priority='high'?" style diagnostics.
        tool_name = a["tool"]
        path = a.get("arg_path") or a.get("path")
        matches = _calls_for_tool(tool_name)
        accept_one_of = a.get("arg_value_one_of")
        accept_equals = a.get("arg_value_equals")
        if accept_equals is not None and accept_one_of is None:
            accept_one_of = [accept_equals]
        if accept_one_of is None:
            ok, reason = False, "tool_called_with_arg missing arg_value_*"
        elif not matches:
            ok, reason = False, f"{tool_name} not called"
        else:
            for c in matches:
                actual = _walk_path(c, path)
                if actual in accept_one_of:
                    ok = True
                    reason = f"{path}={actual!r} in a {tool_name} call"
                    break
            if not ok:
                seen = [_walk_path(c, path) for c in matches]
                reason = (f"no {tool_name} call had {path} in "
                          f"{accept_one_of!r}; saw {seen}")

    else:
        reason = f"unknown assertion type: {atype}"

    return ok, reason


def _assertion_observed_values(a: dict, tool_calls: list[dict]) -> list[dict[str, Any]]:
    """Return the tool-field values that a field assertion inspected."""
    path = a.get("path") or a.get("arg_path")
    if not isinstance(path, str) or not path:
        return []
    tool_name = a.get("tool")
    matching = [
        call
        for call in tool_calls
        if not tool_name or _tool_matches(call.get("tool"), tool_name)
    ]
    if a.get("on_call") == "first":
        matching = matching[:1]
    return [
        {"tool": call.get("tool"), "value": _walk_path(call, path)}
        for call in matching
    ]


def grade_tool_trace(tool_calls: list[dict], config: dict,
                     test_message: str = "") -> dict:
    """
    Check an MCP tool call trace against assertions.

    config:
        required_tool: optional str — at least one call to this tool must exist
        assertions: list of dicts; see _evaluate_assertion for types.
        diagnostic_assertions: optional sibling list — same dispatch,
            but results are LOGGED in `diagnostic` and do NOT affect
            pass/fail or the score.
    """
    version = config.get("check_version", 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError("unsupported tool-trace check version")
    if version == 2:
        from graders.explicit import grade_tool_trace as grade_explicit
        return grade_explicit(tool_calls, config, test_message)
    details: list[dict] = []
    diagnostic: list[dict] = []
    passed_checks = 0
    total_checks = 0

    # Required tool present?
    required_tool = config.get("required_tool")
    target_calls: list[dict] = []
    if required_tool:
        total_checks += 1
        target_calls = [c for c in tool_calls if _tool_matches(c.get("tool"), required_tool)]
        ok = len(target_calls) >= 1
        details.append({
            "name": f"required_tool:{required_tool}",
            "ok": ok,
            "reason": f"{len(target_calls)} call(s)",
        })
        if ok:
            passed_checks += 1

    today = config.get("today")   # narrative anchor date, for date-relative judge criteria

    # Pass/fail assertions
    for a in config.get("assertions", []) or []:
        total_checks += 1
        atype = a.get("type")
        name = f"{atype}:{a.get('path', a.get('tool', a.get('tools', '')))}"
        from graders.judge_recording import record_judge
        with record_judge() as evidence:
            ok, reason = _evaluate_assertion(
                a, tool_calls, target_calls, required_tool,
                today=today, test_message=test_message,
                raise_on_judge_error=bool(config.get("raise_on_judge_error")),
            )
        details.append(
            {
                "name": name,
                "ok": ok,
                "reason": reason,
                "assertion": dict(a),
                "observed_values": _assertion_observed_values(a, tool_calls),
            }
        )
        if evidence.messages:
            details[-1]["judge_messages"] = evidence.messages
            details[-1]["judge_settings"] = evidence.settings
        if ok:
            passed_checks += 1

    # Diagnostic assertions — evaluated but NOT scored.
    for a in config.get("diagnostic_assertions", []) or []:
        atype = a.get("type")
        name = f"{atype}:{a.get('path', a.get('tool', a.get('tools', '')))}"
        ok, reason = _evaluate_assertion(
            a, tool_calls, target_calls, required_tool,
            today=today, test_message=test_message,
            raise_on_judge_error=bool(config.get("raise_on_judge_error")),
        )
        diagnostic.append(
            {
                "name": name,
                "ok": ok,
                "reason": reason,
                "assertion": dict(a),
                "observed_values": _assertion_observed_values(a, tool_calls),
            }
        )

    all_passed = passed_checks == total_checks and total_checks > 0
    return {
        "grader_type": "tool_trace",
        "passed": all_passed,
        # Binary score under all-required grading — see llm_judge.py
        # for rationale. Per-check ratios still in passed/total below.
        "score": 1.0 if all_passed else 0.0,
        "details": details,
        "diagnostic": diagnostic,
        "total_checks": total_checks,
        "passed_checks": passed_checks,
    }


# =========================================================================
# Hybrid grader — combines regex + tool_trace + llm_judge (all must pass)
# =========================================================================

def grade_hybrid(response_text: str, tool_calls: list[dict], config: dict,
                 test_message: str = "") -> dict:
    """Every configured sub-grader must pass.

    Supported sub-keys: ``tool_trace``, ``regex``, ``llm_judge``. The
    llm_judge sub takes the same config block as the standalone
    llm_judge grader (a ``rubric`` list); used by tests like T1 / T8
    that mix a mechanical word-count check with semantic LLM-judged
    criteria.

    Shorthand: a top-level ``rubric:`` is treated as
    ``llm_judge: {rubric: ...}``; top-level ``assertions:`` /
    ``required_tool:`` / ``diagnostic_assertions:`` are treated as a
    ``tool_trace:`` block. This matches how most YAMLs are authored
    (see e.g. 044, 110, 011) and keeps the hybrid grader from silently
    dropping a populated rubric when no explicit sub-key is used.
    """
    sub_results: list[dict] = []
    total_checks = 0
    passed_checks = 0

    # Normalize shorthand: promote top-level rubric / tool_trace fields
    # into the recognized sub-blocks if those sub-blocks are absent.
    if "tool_trace" not in config and (
        "assertions" in config
        or "required_tool" in config
        or "diagnostic_assertions" in config
    ):
        tool_trace_cfg = {
            k: config[k]
            for k in ("assertions", "required_tool", "diagnostic_assertions")
            if k in config
        }
    else:
        tool_trace_cfg = config.get("tool_trace")

    if "llm_judge" not in config and "rubric" in config:
        llm_judge_cfg: dict | None = {"rubric": config["rubric"]}
        # Forward optional llm_judge-tier knobs if authored alongside
        # the rubric (backend / judge_model / azure_deployment).
        for k in ("backend", "judge_model", "azure_deployment"):
            if k in config:
                llm_judge_cfg[k] = config[k]
    else:
        llm_judge_cfg = config.get("llm_judge")

    if tool_trace_cfg is not None:
        r = grade_tool_trace(tool_calls, tool_trace_cfg, test_message=test_message)
        sub_results.append({"sub": "tool_trace", **r})
        total_checks += r["total_checks"]
        passed_checks += r["passed_checks"]

    if "regex" in config:
        r = grade_regex(response_text, config["regex"])
        sub_results.append({"sub": "regex", **r})
        total_checks += r["total_checks"]
        passed_checks += r["passed_checks"]

    if llm_judge_cfg is not None:
        # Local import to avoid pulling in network-using module at
        # mechanical-grader import time.
        from graders.llm_judge import grade_llm_judge
        r = grade_llm_judge(response_text, test_message,
                            llm_judge_cfg, tool_calls=tool_calls)
        sub_results.append({"sub": "llm_judge", **r})
        total_checks += r["total_checks"]
        passed_checks += r["passed_checks"]

    all_passed = all(s["passed"] for s in sub_results) and len(sub_results) > 0

    return {
        "grader_type": "hybrid",
        "passed": all_passed,
        # Binary score under all-required grading — see llm_judge.py
        # for rationale. Per-check ratios still in passed/total below
        # and per-sub-grader scores live in details[].
        "score": 1.0 if all_passed else 0.0,
        "details": sub_results,
        "total_checks": total_checks,
        "passed_checks": passed_checks,
    }
