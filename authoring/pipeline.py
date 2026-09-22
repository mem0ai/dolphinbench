"""Deterministic assembly and local checks for authored DolphinBench tests."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from authoring.context import CheckpointContext
from authoring.models import BlindQueryResponse, DesignResponse, PlannedTask
from harness.task_schema import TestSpec


NUMERIC_ASSERTIONS = {"field_lte", "field_gte", "field_eq_number"}
EXACT_ASSERTIONS = {"field_equals", "recipient_matches"}
EMPTY_ASSERTIONS = {"field_absent_or_empty"}
EXACT_TOOL_COUNT_ASSERTIONS = {"tool_call_count"}
DATE_ASSERTIONS = {
    "datetime_absolute_eq",
    "date_on",
    "date_on_or_after",
    "date_on_or_before",
    "time_of_day_gte",
    "time_of_day_lte",
}
REGEX_ASSERTIONS = {
    "field_regex",
    "field_not_regex",
    "tool_called_with_arg_regex",
    "tool_called_with_value",
    "tool_not_called_with_arg_regex",
}
SUPPORTED_ASSERTIONS = {
    "field_list_includes",
    "email_recipients_received",
    "tool_called",
    "tool_not_called",
    "field_llm_judge",
    *EXACT_TOOL_COUNT_ASSERTIONS,
    *EXACT_ASSERTIONS,
    *EMPTY_ASSERTIONS,
    *NUMERIC_ASSERTIONS,
    *DATE_ASSERTIONS,
    *REGEX_ASSERTIONS,
}
NEGATIVE_TOOL_ASSERTIONS = {
    "tool_not_called",
    "tool_not_called_with_arg_regex",
}


def tool_has_final_effect(context: CheckpointContext, tool_name: str) -> bool:
    """Return whether a tool changes app state or acts outside the benchmark."""
    effect = context.tools.get(tool_name, {}).get("state_effect") or {}
    return bool(effect.get("writes_state_keys") or effect.get("external_effect"))
ARTIFICIAL_MEMORY_POINTERS = (
    "my usual",
    "my normal",
    "my preferred",
    "as we discussed",
    "you know how i like it",
    "use what i told you",
    "already established",
    "from the context available to you",
    "based on the background context",
)
STANDALONE_RECALL_QUERY = re.compile(
    r"^\s*(?:what|which|who|when|where|how(?: much| many)?)\b"
    r"[^?.!]*\b(?:did|do|was|were)\b[^?.!]*\??\s*$",
    re.IGNORECASE,
)
BLANK_REQUIRED_DETAIL_INSTRUCTION = re.compile(
    r"(?<!do not )(?<!don't )(?<!never )\b(?:leave|keep|set|mark)\b"
    r"(?=[^.!?]{0,160}\b(?:owner|value|boundary)\b)"
    r"(?=[^.!?]{0,160}\b(?:blank|tbd)\b)[^.!?]{0,160}\b(?:blank|tbd)\b",
    re.IGNORECASE,
)
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
WRITTEN_MONTH_DAY_YEAR = re.compile(
    r"\b(?:Jan(?:uary)?\.?|Feb(?:ruary)?\.?|Mar(?:ch)?\.?|Apr(?:il)?\.?|"
    r"May\.?|Jun(?:e)?\.?|Jul(?:y)?\.?|Aug(?:ust)?\.?|"
    r"Sep(?:t(?:ember)?)?\.?|Oct(?:ober)?\.?|Nov(?:ember)?\.?|"
    r"Dec(?:ember)?\.?)\s+\d{1,2},\s+\d{4}\b",
    re.IGNORECASE,
)
MONTH_NUMBERS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


class AuthoringValidationError(ValueError):
    def __init__(self, message: str, *, correction_fields: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.correction_fields = correction_fields


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _validate_answer_absence(
    *, query: str, state: dict[str, Any], answers: list[str]
) -> None:
    visible = _normalized_text(
        query + "\n" + json.dumps(state, ensure_ascii=False, sort_keys=True)
    )
    leaked = [
        answer
        for answer in answers
        if len(_normalized_text(answer)) >= 3
        and _normalized_text(answer) in visible
    ]
    if leaked:
        raise AuthoringValidationError(
            "the request or readable app state reveals answer-bearing values: "
            + ", ".join(repr(value) for value in leaked)
        )


def _canonical_written_date(date_text: str) -> str:
    month_text, day_text, year_text = re.match(
        r"([A-Za-z.]+)\s+(\d{1,2}),\s+(\d{4})", date_text
    ).groups()
    month = MONTH_NUMBERS[month_text.rstrip(".").casefold()]
    return f"{year_text}-{month:02d}-{int(day_text):02d}"


def _concrete_dates(text: str) -> set[str]:
    dates = {match.group(0) for match in ISO_DATE.finditer(text)}
    dates.update(
        _canonical_written_date(match.group(0))
        for match in WRITTEN_MONTH_DAY_YEAR.finditer(text)
    )
    return dates


def _validate_query_dates_against_handoff(
    *, query: str, design: DesignResponse
) -> None:
    """Do not let the blind writer invent a concrete date."""
    permitted_dates = _concrete_dates(
        "\n".join(
            (
                design.present_situation,
                design.requested_work,
            )
        )
    )
    for pattern in (ISO_DATE, WRITTEN_MONTH_DAY_YEAR):
        for match in pattern.finditer(query):
            date_text = match.group(0)
            canonical = (
                date_text
                if pattern is ISO_DATE
                else _canonical_written_date(date_text)
            )
            if canonical not in permitted_dates:
                raise AuthoringValidationError(
                    "request introduces a concrete date not present in the "
                    f"permitted handoff: {date_text!r}"
                )


def _validate_request_handoff(design: DesignResponse) -> None:
    """Keep answer-bearing values out of every field sent to the request writer."""
    for field_name in (
        "present_situation",
        "requested_work",
        "missing_information",
        "information_the_request_must_not_reveal",
    ):
        field_value = _normalized_text(getattr(design, field_name))
        leaked = [
            answer
            for answer in design.answers_that_must_not_appear
            if len(_normalized_text(answer)) >= 3
            and _normalized_text(answer) in field_value
        ]
        if leaked:
            raise AuthoringValidationError(
                f"{field_name} contains answer-bearing values: "
                + ", ".join(repr(value) for value in leaked)
            )


def _validate_query_contract(*, query: str, design: DesignResponse | None = None) -> None:
    """Reject request wording that makes hidden memory an explicit instruction."""
    normalized = _normalized_text(query)
    pointer = next(
        (phrase for phrase in ARTIFICIAL_MEMORY_POINTERS if phrase in normalized),
        None,
    )
    if pointer:
        raise AuthoringValidationError(
            f"request contains artificial memory pointer {pointer!r}"
        )
    if STANDALONE_RECALL_QUERY.fullmatch(query):
        raise AuthoringValidationError(
            "request is standalone recall rather than realistic new work"
        )
    if BLANK_REQUIRED_DETAIL_INSTRUCTION.search(query):
        raise AuthoringValidationError(
            "request instructs the assistant to leave a required owner, value, or boundary blank/TBD"
        )
    if design is not None:
        _validate_query_dates_against_handoff(query=query, design=design)


def _matches(record: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(record.get(key) == value for key, value in expected.items())


def _remove_field(record: dict[str, Any], path: str) -> None:
    parts = [part for part in path.split(".") if part]
    if not parts:
        raise AuthoringValidationError("remove_fields cannot contain an empty path")
    current: Any = record
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            raise AuthoringValidationError(f"cannot remove missing field {path!r}")
        current = current[part]
    if not isinstance(current, dict) or parts[-1] not in current:
        raise AuthoringValidationError(f"cannot remove missing field {path!r}")
    del current[parts[-1]]


def build_mock_state(
    context: CheckpointContext, design: DesignResponse
) -> dict[str, Any]:
    state: dict[str, Any] = {}
    readable_keys = {
        str(key)
        for tool in context.tools.values()
        for key in (tool.get("state_effect") or {}).get("reads_state_keys") or []
    }
    for selection in design.existing_records:
        key = selection.state_key
        if key not in readable_keys or key not in context.readable_state:
            raise AuthoringValidationError(
                f"existing record state_key {key!r} is not readable through a tool"
            )
        source = context.readable_state[key]
        if isinstance(source, list):
            if selection.record_key:
                raise AuthoringValidationError(
                    f"record_key must be empty for list-valued state key {key!r}"
                )
            if not selection.match:
                raise AuthoringValidationError(
                    f"existing record selection for {key!r} needs exact match fields"
                )
            matches = [row for row in source if isinstance(row, dict) and _matches(row, selection.match)]
            if len(matches) != 1:
                raise AuthoringValidationError(
                    f"existing record selection for {key!r} matched {len(matches)} records"
                )
            record = copy.deepcopy(matches[0])
            for field in selection.remove_fields:
                _remove_field(record, field)
            state.setdefault(key, []).append(record)
        elif isinstance(source, dict):
            if not selection.record_key or selection.record_key not in source:
                raise AuthoringValidationError(
                    f"existing dictionary record {key!r} needs a real record_key"
                )
            record = copy.deepcopy(source[selection.record_key])
            if selection.match and (
                not isinstance(record, dict) or not _matches(record, selection.match)
            ):
                raise AuthoringValidationError(
                    f"dictionary record {key!r}/{selection.record_key!r} does not match"
                )
            if not isinstance(record, dict):
                record = {"value": record}
            for field in selection.remove_fields:
                _remove_field(record, field)
            state.setdefault(key, {})[selection.record_key] = record
        else:
            raise AuthoringValidationError(f"unsupported state value for {key!r}")

    for addition in design.new_records:
        key = addition.state_key
        if key not in readable_keys:
            raise AuthoringValidationError(
                f"new record state_key {key!r} is not discoverable through a read tool"
            )
        source = context.app_state.get(key)
        if isinstance(source, dict):
            if not addition.record_key:
                raise AuthoringValidationError(
                    f"new dictionary record {key!r} requires record_key"
                )
            records = state.setdefault(key, {})
            if addition.record_key in records:
                raise AuthoringValidationError(
                    f"new dictionary record duplicates {key!r}/{addition.record_key!r}"
                )
            records[addition.record_key] = copy.deepcopy(addition.record)
        elif isinstance(source, list):
            if addition.record_key:
                record_id = addition.record.get("id")
                if not (
                    isinstance(record_id, str)
                    and record_id.strip()
                    and addition.record_key == record_id
                ):
                    raise AuthoringValidationError(
                        f"record_key must be empty for list-valued state key {key!r} "
                        "unless it exactly matches the new record's non-empty id"
                    )
            state.setdefault(key, []).append(copy.deepcopy(addition.record))
        else:
            raise AuthoringValidationError(
                f"new record state_key {key!r} is not list- or dictionary-valued"
            )
    return state


def _assertion_path_argument(assertion: dict[str, Any]) -> str | None:
    path = assertion.get("path") or assertion.get("arg_path")
    if not isinstance(path, str) or not path.startswith("args."):
        return None
    return path.split(".", 1)[1].split(".", 1)[0]


def validate_design(
    context: CheckpointContext,
    planned_task: PlannedTask,
    design: DesignResponse,
) -> list[str]:
    problems: list[str] = []
    if design.outcome == "reject":
        if not design.rejection_reason.strip():
            problems.append("reject outcome requires rejection_reason")
        return problems
    if design.rejection_reason:
        problems.append("design outcome must have an empty rejection_reason")
    try:
        design.blind_request_handoff()
    except ValueError as exc:
        problems.append(str(exc))
    try:
        _validate_request_handoff(design)
    except AuthoringValidationError as exc:
        problems.append(str(exc))
    if not design.expected_tool_calls:
        problems.append("expected_tool_calls is empty")
    if not design.grading_checks:
        problems.append("grading_checks is empty")
    selected = set(planned_task.fact_ids)
    covered_memory_facts: set[int] = set()
    tools_with_positive_evidence: set[str] = set()
    for index, check in enumerate(design.grading_checks):
        unknown_facts = set(check.fact_ids) - selected
        if unknown_facts:
            problems.append(f"check {index} cites unselected facts {sorted(unknown_facts)}")
        covered_memory_facts.update(check.fact_ids)
        assertion = check.assertion
        assertion_type = assertion.get("type")
        tool = assertion.get("tool")
        if assertion_type not in SUPPORTED_ASSERTIONS:
            problems.append(f"check {index} uses unsupported assertion {assertion_type!r}")
        if not isinstance(tool, str) or tool not in context.tools:
            problems.append(f"check {index} uses unknown tool {tool!r}")
            continue
        proves_call_happened = (
            assertion_type not in NEGATIVE_TOOL_ASSERTIONS
            and not (
                assertion_type in EXACT_TOOL_COUNT_ASSERTIONS
                and assertion.get("count") == 0
            )
        )
        if proves_call_happened:
            tools_with_positive_evidence.add(tool)
        if assertion_type in REGEX_ASSERTIONS:
            pattern = assertion.get("pattern") or assertion.get("arg_value_regex")
            if not isinstance(pattern, str) or not pattern:
                problems.append(f"check {index} needs a regex pattern")
            else:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    problems.append(f"check {index} has invalid regex: {exc}")
        if assertion_type in EXACT_ASSERTIONS | NUMERIC_ASSERTIONS | DATE_ASSERTIONS:
            if "value" not in assertion:
                problems.append(f"check {index} needs value")
            if not _assertion_path_argument(assertion):
                problems.append(f"check {index} needs an args.<field> path")
        if assertion_type in EMPTY_ASSERTIONS and not _assertion_path_argument(assertion):
            problems.append(f"check {index} needs an args.<field> path")
        if assertion_type in {"field_list_includes", "email_recipients_received"}:
            values = assertion.get("values")
            if not isinstance(values, list) or not values or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                problems.append(f"check {index} needs a non-empty list of string values")
            if assertion_type == "field_list_includes" and not _assertion_path_argument(assertion):
                problems.append(f"check {index} needs an args.<field> path")
            if assertion_type == "email_recipients_received" and tool != "send_email":
                problems.append(f"check {index} must check send_email delivery")
        if assertion_type in EXACT_TOOL_COUNT_ASSERTIONS:
            count = assertion.get("count")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                problems.append(f"check {index} needs a non-negative integer count")
        if assertion_type == "field_llm_judge":
            if not str(assertion.get("criterion") or "").strip():
                problems.append(f"check {index} needs one binary criterion")
            path = assertion.get("path")
            if path != "args" and not _assertion_path_argument(assertion):
                problems.append(f"check {index} needs path 'args' or 'args.<field>'")
        argument = _assertion_path_argument(assertion)
        if argument and argument not in context.tools[tool]["arguments"]:
            problems.append(
                f"check {index} path names {argument!r}, which is not an argument of {tool}"
            )
    if covered_memory_facts != selected:
        problems.append(
            "memory-dependent grading checks must cover every and only selected fact; "
            f"covered={sorted(covered_memory_facts)}, selected={sorted(selected)}"
        )
    unknown_expected = [
        tool for tool in design.expected_tool_calls if tool not in context.tools
    ]
    if unknown_expected:
        problems.append(f"expected_tool_calls contains unknown tools {unknown_expected}")
    missing_expected = tools_with_positive_evidence - set(design.expected_tool_calls)
    if missing_expected:
        problems.append(
            f"assertion tools are absent from expected_tool_calls: {sorted(missing_expected)}"
        )
    final_action_tools = {
        tool
        for tool in design.expected_tool_calls
        if tool_has_final_effect(context, tool)
    }
    expected_without_evidence = final_action_tools - tools_with_positive_evidence
    if expected_without_evidence:
        problems.append(
            "final action tools have no non-negative grading evidence for: "
            f"{sorted(expected_without_evidence)}"
        )
    planned_tools = set(planned_task.expected_tools)
    design_tools = set(design.expected_tool_calls)
    missing_planned_tools = planned_tools - design_tools
    if missing_planned_tools:
        problems.append(
            "expected_tool_calls omits planned actions: "
            f"{sorted(missing_planned_tools)}"
        )
    if planned_tools:
        supplied_state_keys = {
            record.state_key for record in [*design.existing_records, *design.new_records]
        }
        invalid_extra_tools: list[str] = []
        for tool_name in sorted(design_tools - planned_tools):
            effect = context.tools.get(tool_name, {}).get("state_effect") or {}
            reads = set(effect.get("reads_state_keys") or [])
            writes = set(effect.get("writes_state_keys") or [])
            if writes or not reads or not reads.issubset(supplied_state_keys):
                invalid_extra_tools.append(tool_name)
        if invalid_extra_tools:
            problems.append(
                "extra expected tool calls are not read-only lookups for supplied records: "
                f"{invalid_extra_tools}"
            )
    if not 0 <= design.without_memory_failed_check < len(design.grading_checks):
        problems.append("without_memory_failed_check is not a grading-check index")
    return problems


def assemble_candidate(
    *,
    context: CheckpointContext,
    planned_task: PlannedTask,
    evaluation_date: str,
    test_id: str,
    design: DesignResponse,
    blind_query: BlindQueryResponse,
) -> dict[str, Any]:
    problems = validate_design(context, planned_task, design)
    if problems:
        raise AuthoringValidationError("; ".join(problems))
    if design.outcome != "design":
        raise AuthoringValidationError(
            f"cannot assemble rejected design: {design.rejection_reason}"
        )
    _validate_query_contract(query=blind_query.query, design=design)
    state = build_mock_state(context, design)
    _validate_answer_absence(
        query=blind_query.query,
        state=state,
        answers=design.answers_that_must_not_appear,
    )
    candidate = {
        "id": int(test_id),
        "narrative_anchor_date": evaluation_date,
        "test": blind_query.query,
        "load_bearing_facts": list(planned_task.fact_ids),
        "expected_tool_calls": list(design.expected_tool_calls),
        "grade": {
            "type": "tool_trace",
            "config": {
                "assertions": [
                    copy.deepcopy(check.assertion) for check in design.grading_checks
                ],
                "today": evaluation_date,
            },
        },
        "mock_state": state,
    }
    if design.check_version == 2:
        from graders.explicit import validate_checks
        candidate["grade"]["config"]["check_version"] = 2
        validate_checks(candidate["grade"]["config"]["assertions"])
    return TestSpec.model_validate(candidate).model_dump(mode="python")


def write_candidate(path: Path, candidate: dict[str, Any]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True, width=100)
    )
