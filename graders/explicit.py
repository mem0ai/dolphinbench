"""Version 2 tool-trace checks: declared comparisons on named actions."""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any


MECHANICAL_TYPES = {
    "field_equals", "field_equals_all_calls", "field_eq_number", "field_lte", "field_gte", "date_on",
    "date_on_or_after", "date_on_or_before", "datetime_absolute_eq",
    "field_list_includes", "field_list_equals_unordered", "field_absent", "field_absent_or_empty", "field_regex", "field_markdown_bullets",
    "tool_called", "tool_call_count", "tool_success_count", "tool_not_called",
}
CHECK_TYPES = MECHANICAL_TYPES | {"field_llm_judge"}


def validate_checks(assertions: list[dict[str, Any]]) -> None:
    if not isinstance(assertions, list) or not assertions:
        raise ValueError("explicit grading requires checks")
    ids: set[str] = set()
    action_tools: dict[str, str] = {}
    for check in assertions:
        allowed = {"type", "tool", "path", "value", "values", "pattern", "criterion",
                   "count", "check_id", "action_id"}
        if not isinstance(check, dict) or set(check) - allowed:
            raise ValueError("explicit check has unsupported fields")
        kind = check.get("type")
        if kind not in CHECK_TYPES:
            raise ValueError(f"unsupported explicit check type: {kind}")
        operands = {
            "field_llm_judge": {"criterion"}, "field_regex": {"pattern"},
            "field_list_includes": {"values"}, "tool_call_count": {"count"},
            "field_list_equals_unordered": {"values"}, "tool_success_count": {"count"},
            "tool_called": set(), "tool_not_called": set(),
            "field_absent": set(), "field_absent_or_empty": set(),
        }.get(kind, {"value"})
        if set(check) & ({"value", "values", "pattern", "criterion", "count"} - operands):
            raise ValueError("explicit check contains unused comparison operands")
        if kind.startswith("tool_") and "path" in check:
            raise ValueError("tool checks cannot name a field path")
        for key in ("check_id", "tool"):
            if not isinstance(check.get(key), str) or not check[key].strip():
                raise ValueError(f"explicit check needs {key}")
        if check["check_id"] in ids:
            raise ValueError("check IDs must be unique")
        ids.add(check["check_id"])
        if kind in {"tool_call_count", "tool_success_count", "tool_not_called", "field_equals_all_calls"}:
            if check.get("action_id"):
                raise ValueError("trace-wide checks cannot belong to one action")
            if kind in {"tool_call_count", "tool_success_count"} and (
                type(check.get("count")) is not int or check["count"] < 0
            ):
                raise ValueError("tool count must be a nonnegative integer")
        else:
            action = check.get("action_id")
            if not isinstance(action, str) or not action.strip():
                raise ValueError("each action check needs an action_id")
            if action in action_tools and action_tools[action] != check["tool"]:
                raise ValueError("checks on one action must name the same tool")
            action_tools[action] = check["tool"]
        if kind.startswith("field_") or kind.startswith("date"):
            path = check.get("path")
            if not isinstance(path, str) or not (
                path in {"args", "result"} or path.startswith(("args.", "result."))
            ):
                raise ValueError("explicit field checks must name an args or result path")
        if kind == "field_llm_judge":
            if not isinstance(check.get("criterion"), str) or not check["criterion"].strip():
                raise ValueError("LLM checks require a generated rubric")
        elif check.get("criterion") is not None:
            raise ValueError("mechanical checks cannot contain an LLM rubric")
        if kind == "field_regex":
            if not isinstance(check.get("pattern"), str) or not check["pattern"]:
                raise ValueError("literal pattern check needs a pattern")
            re.compile(check["pattern"])
        if kind == "field_list_includes" and (
            not isinstance(check.get("values"), list) or not check["values"]
        ):
            raise ValueError("list check needs required values")
        if kind == "field_list_equals_unordered" and not isinstance(check.get("values"), list):
            raise ValueError("unordered list comparison needs a values list")
        if kind == "field_markdown_bullets":
            from graders.markdown_bullets import validate_limits

            validate_limits(check.get("value"))
        if kind in {"field_equals", "field_equals_all_calls", "field_eq_number", "field_lte", "field_gte",
                    "date_on", "date_on_or_after", "date_on_or_before", "datetime_absolute_eq"}:
            if "value" not in check:
                raise ValueError("comparison needs an expected value")
        if kind in {"field_eq_number", "field_lte", "field_gte"}:
            if _number(check["value"]) is None:
                raise ValueError("numeric check needs a finite number")
        if kind in {"date_on", "date_on_or_after", "date_on_or_before"}:
            try:
                datetime.strptime(check["value"], "%Y-%m-%d")
            except (TypeError, ValueError):
                raise ValueError("calendar check needs YYYY-MM-DD") from None
        if kind == "datetime_absolute_eq":
            value = _datetime(check["value"])
            if value is None or value.utcoffset() is None:
                raise ValueError("instant check needs a timezone-aware timestamp")


def _walk(value: Any, path: str) -> tuple[bool, Any]:
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return False, None
    return True, value


def _equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    return left == right


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def _datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _evaluate(check: dict, call: dict, request: str, today: str) -> tuple[bool, str, Any]:
    kind = check["type"]
    if kind == "tool_called":
        return True, "requested tool was called", call.get("args")
    present, actual = _walk(call, check["path"])
    if kind == "field_absent":
        ok = not present
    elif kind == "field_absent_or_empty":
        ok = not present or actual in (None, "", [], {})
    elif not present:
        return False, "required field is missing", None
    elif kind == "field_llm_judge":
        from graders.llm_judge import judge_field_value

        context = json.dumps({"user_request": request, "today": today,
                              "tool": call["tool"], "arguments": call.get("args", {}),
                              "field_being_checked": check["path"]})
        result = judge_field_value(actual, check["criterion"], context=context,
                                   config={"raise_on_error": True})
        if not isinstance(result.get("ok"), bool):
            raise ValueError("LLM check returned no Boolean decision")
        return result["ok"], result.get("reason", ""), actual
    elif kind in {"field_equals", "field_equals_all_calls"}:
        ok = _equal(actual, check["value"])
    elif kind in {"field_eq_number", "field_lte", "field_gte"}:
        left, right = _number(actual), _number(check["value"])
        ok = left is not None and right is not None and (
            left == right if kind == "field_eq_number" else
            left <= right if kind == "field_lte" else left >= right
        )
    elif kind == "field_list_includes":
        ok = isinstance(actual, list) and all(
            any(_equal(value, item) for item in actual) for value in check["values"]
        )
    elif kind == "field_list_equals_unordered":
        remaining = list(actual) if isinstance(actual, list) else []
        ok = isinstance(actual, list) and len(actual) == len(check["values"])
        if ok:
            for value in check["values"]:
                match = next((i for i, item in enumerate(remaining) if _equal(value, item)), None)
                if match is None:
                    ok = False
                    break
                remaining.pop(match)
    elif kind == "field_regex":
        ok = isinstance(actual, str) and re.search(check["pattern"], actual) is not None
    elif kind == "field_markdown_bullets":
        from graders.markdown_bullets import matches_bullets

        ok = matches_bullets(actual, check["value"])
    else:
        left, right = _datetime(actual), _datetime(check["value"])
        if kind == "datetime_absolute_eq":
            ok = left is not None and right is not None and left.utcoffset() is not None and left == right
        else:
            ok = left is not None and right is not None and (
                left.date() == right.date() if kind == "date_on" else
                left.date() >= right.date() if kind == "date_on_or_after" else
                left.date() <= right.date()
            )
    return bool(ok), "comparison passed" if ok else "comparison failed", actual


def grade_tool_trace(calls: list[dict], config: dict, test_message: str = "") -> dict:
    from graders.judge_recording import JudgeConversation, record_judge

    assertions = config.get("assertions")
    validate_checks(assertions)
    if set(config) - {"check_version", "assertions", "today", "raise_on_judge_error", "semantic_judge_version"}:
        raise ValueError("unsupported explicit grading configuration")
    semantic_version = config.get("semantic_judge_version", 1)
    if type(semantic_version) is not int or semantic_version not in (1, 2):
        raise ValueError("unsupported semantic judge version")
    groups: dict[str, list[int]] = {}
    results: dict[int, tuple[bool, str, Any, int | None]] = {}
    evaluations: dict[tuple[str, int], list[tuple[bool, str, Any]]] = {}
    eligible: dict[str, list[int]] = {}
    evidence: dict[int, JudgeConversation] = {}
    for index, check in enumerate(assertions):
        if check["type"] == "field_equals_all_calls":
            values = [_evaluate(check, call, test_message, config.get("today", ""))
                      for call in calls if call.get("tool") == check["tool"]]
            ok = bool(values) and all(value[0] for value in values)
            results[index] = (ok, "every call matched" if ok else "missing call or a field did not match",
                              [value[2] for value in values], None)
        elif check["type"] in {"tool_call_count", "tool_success_count", "tool_not_called"}:
            count = sum(call.get("tool") == check["tool"] and (
                check["type"] != "tool_success_count" or (
                    isinstance(call.get("result"), dict) and call["result"].get("ok") is True
                )) for call in calls)
            expected = 0 if check["type"] == "tool_not_called" else check["count"]
            results[index] = (count == expected, f"observed {count} calls; required {expected}", count, None)
        else:
            groups.setdefault(check["action_id"], []).append(index)
    for action, indexes in groups.items():
        eligible[action] = []
        for call_index, call in enumerate(calls):
            if call.get("tool") != assertions[indexes[0]]["tool"]:
                continue
            # A failed direct comparison already disqualifies this call. Do not
            # ask a model about its text, even if the text check is listed first.
            direct = {index: _evaluate(assertions[index], call, test_message, config.get("today", ""))
                      for index in indexes if assertions[index]["type"] != "field_llm_judge"}
            direct_passed = all(value[0] for value in direct.values())
            semantic = {}
            if direct_passed and semantic_version == 2:
                from graders.llm_judge import judge_action_fields
                fields = []
                for index in indexes:
                    check = assertions[index]
                    if check["type"] == "field_llm_judge":
                        present, actual = _walk(call, check["path"])
                        if present:
                            fields.append({"check_id": check["check_id"], "path": check["path"],
                                           "criterion": check["criterion"], "value": actual})
                if fields:
                    # A batched response is retained once, beside its first check.
                    owner = next(i for i in indexes if assertions[i]["check_id"] == fields[0]["check_id"])
                    with record_judge(evidence.setdefault(owner, JudgeConversation())):
                        semantic = judge_action_fields(call=call, fields=fields,
                                                       request=test_message, today=config.get("today", ""))
            values = []
            for index in indexes:
                check = assertions[index]
                if index in direct:
                    values.append(direct[index])
                elif not direct_passed:
                    values.append((False, "not evaluated: a direct comparison failed for this call",
                                   _walk(call, check["path"])[1]))
                elif check["check_id"] in semantic:
                    verdict = semantic[check["check_id"]]
                    values.append((verdict["ok"], verdict["reason"], _walk(call, check["path"])[1]))
                else:
                    with record_judge(evidence.setdefault(index, JudgeConversation())):
                        values.append(_evaluate(check, call, test_message, config.get("today", "")))
            evaluations[action, call_index] = values
            if all(value[0] for value in values):
                eligible[action].append(call_index)

    # Each requested action needs its own complete call. Augmenting paths allow
    # overlapping valid alternatives without depending on trace ordering.
    assigned: dict[int, str] = {}
    def assign(action: str, visited: set[int]) -> bool:
        for call_index in eligible[action]:
            if call_index in visited:
                continue
            visited.add(call_index)
            if call_index not in assigned or assign(assigned[call_index], visited):
                assigned[call_index] = action
                return True
        return False
    for action in groups:
        assign(action, set())
    chosen = {action: index for index, action in assigned.items()}
    for action, indexes in groups.items():
        call_index = chosen.get(action)
        if call_index is not None:
            for index, value in zip(indexes, evaluations[action, call_index]):
                results[index] = (*value, call_index)
        else:
            for index in indexes:
                results[index] = (False, "no distinct call satisfies every check for this action", None, None)
    details = []
    for index, check in enumerate(assertions):
        ok, reason, actual, call_index = results[index]
        observed = [
            {"tool": call.get("tool"), "call_index": i,
             "value": _walk(call, check.get("path", "args"))[1]}
            for i, call in enumerate(calls) if call.get("tool") == check["tool"]
        ]
        details.append({"name": check["check_id"], "ok": ok, "reason": reason,
                        "assertion": dict(check), "observed_values": observed,
                        "matched_call_index": call_index,
                        "call_evaluations": [
                            {"call_index": i, "ok": values[position][0],
                             "reason": values[position][1], "value": values[position][2]}
                            for (action, i), values in evaluations.items()
                            if action == check.get("action_id")
                            for position in [groups[action].index(index)]
                        ]})
        if index in evidence and evidence[index].messages:
            details[-1]["judge_messages"] = evidence[index].messages
            details[-1]["judge_settings"] = evidence[index].settings
    passed = sum(item["ok"] for item in details)
    return {"grader_type": "tool_trace", "passed": passed == len(details),
            "score": float(passed == len(details)), "details": details,
            "diagnostic": [], "total_checks": len(details), "passed_checks": passed}
