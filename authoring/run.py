"""Run canonical DolphinBench test authoring after explicit paid-call approval."""

from __future__ import annotations

import argparse
import copy
import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from authoring.context import (
    CheckpointContext,
    design_payload,
    dump_json,
    load_authoring_tasks,
    load_checkpoint_context,
    load_config,
)
from authoring.models import (
    ApprovedIdea,
    AuthoredTestResponse,
    AuthoringConfig,
    BlindQueryResponse,
    DesignResponse,
    GradingCheck,
    GradingDesignResponse,
    PlannedTask,
)
from authoring.pipeline import (
    AuthoringValidationError,
    SUPPORTED_ASSERTIONS,
    assemble_candidate,
    build_mock_state,
    _concrete_dates,
    tool_has_final_effect,
    validate_design,
    write_candidate,
)
from authoring.prompts import (
    AUTHOR_TEST_SYSTEM,
    BLIND_QUERY_SYSTEM,
    DESIGN_SYSTEM,
    GRADING_DESIGN_SYSTEM,
    REDESIGN_SYSTEM,
    REAUTHOR_TEST_SYSTEM,
    COMPLETE_TEST_WRITER_SYSTEM,
)
from authoring.preflight import execute_preflight
from authoring.progress import authoring_artifacts, save_progress
from construction.llm import AzureJsonClient
from construction.runtime_model_calls import cached_client_complete
from harness.environment import call_ledger as _call_ledger
from authoring.input_rendering import render_authoring_input
from authoring.certify import certify_candidate


Certifier = Callable[..., dict[str, Any]]


GRADING_AUTHORING_FIELDS = (
    "expected_arguments", "expected_empty_arguments", "expected_tool_counts",
    "prose_requirements", "request_requirement_quotes", "requested_result_checks",
)


def _correction_fields_for_issues(issues: list[dict[str, Any]]) -> list[str]:
    """Translate named owners into the only authoring fields they may change."""
    owners = {issue["owning_step"] for issue in issues}
    if "planning" in owners or "execution" in owners:
        return []
    fields: set[str] = set()
    if owners & {"grading_checks", "user_request", "starting_app_data", "test_design"}:
        fields.update(GRADING_AUTHORING_FIELDS)
    if owners & {"user_request", "test_design"}:
        fields.add("query")
        fields.update((
            "history_dependent_results.*.present_request_states",
            "history_dependent_results.*.why_request_alone_does_not_determine_result",
        ))
    if owners & {"starting_app_data", "test_design"}:
        fields.update(("design.existing_records", "design.new_records"))
    if "test_design" in owners:
        fields.update(("design.source_message_meaning", "design.reason_it_applies_on_test_date"))
    return sorted(fields)


def _validate_correction_scope(
    *, previous: dict[str, Any], current: AuthoredTestResponse, allowed_fields: list[str]
) -> None:
    """Keep unowned fields byte-value equivalent across model-written corrections."""
    if not allowed_fields:
        raise AuthoringValidationError("a correction needs an explicitly identified set of fields")
    before = AuthoredTestResponse.model_validate(_without_call_metadata(previous)).model_dump(mode="json")
    after = current.model_dump(mode="json")
    # A current response can attach the new proof map to legacy saved work, but
    # that does not authorize changing its facts, request, state, or meaning.
    if before["schema_version"] == 1 and after["schema_version"] == 2:
        before["schema_version"] = after["schema_version"]
        before["requested_result_checks"] = after["requested_result_checks"]

    def compare(left: Any, right: Any, path: str) -> None:
        if any(fnmatch.fnmatchcase(path, field) for field in allowed_fields):
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in left.keys() | right.keys():
                compare(left.get(key), right.get(key), f"{path}.{key}" if path else key)
        elif isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
            for index, (old, new) in enumerate(zip(left, right, strict=True)):
                compare(old, new, f"{path}.{index}")
        elif left != right:
            raise AuthoringValidationError(f"correction changed an unrelated field: {path}")

    compare(before, after, "")


def _without_call_metadata(response: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in response.items() if not key.startswith("_")}


def _strict_response_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make every declared field explicit for Azure strict structured output."""
    strict_schema = copy.deepcopy(schema)

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("default", None)
            properties = value.get("properties")
            if isinstance(properties, dict):
                value["required"] = list(properties)
            if value.get("type") == "object":
                value["additionalProperties"] = False
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(strict_schema)
    return strict_schema


def _authoring_response_schema(version: int) -> dict[str, Any]:
    schema = AuthoredTestResponse.model_json_schema()
    schema["properties"]["schema_version"] = {"type": "integer", "const": version}
    definitions = schema["$defs"]
    mapping = definitions["RequestedResultChecks"]["properties"]
    mapping.pop("assertion_indexes" if version == 3 else "check_ids")
    if version == 3:
        definitions["ExpectedToolArgument"]["properties"]["comparison"] = {
            "type": "string", "enum": ["exact", "number", "date", "instant", "list_includes"]
        }
    else:
        for name in ("ExpectedToolArgument", "ExpectedEmptyToolArgument", "ProseRequirement"):
            definitions[name]["properties"].pop("check_id")
            definitions[name]["properties"].pop("action_id")
    return _strict_response_schema(schema)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _model_call(
    *,
    directory: Path,
    name: str,
    system: str,
    payload: dict[str, Any],
    client: AzureJsonClient,
    response_schema: dict[str, Any] | None = None,
    response_schema_name: str | None = None,
) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}_system.txt").write_text(system + "\n")
    dump_json(directory / f"{name}_request.json", payload)
    if response_schema is not None:
        dump_json(directory / f"{name}_schema.json", {
            "response_schema": response_schema, "response_schema_name": response_schema_name,
        })
    model_payload: dict[str, Any] | str = payload
    if response_schema_name == "dolphinbench_complete_test":
        model_payload = render_authoring_input(payload)
        (directory / f"{name}_request.md").write_text(model_payload, encoding="utf-8")
    response = cached_client_complete(
        directory / "work" / f"{name}_response_cache.json",
        system,
        model_payload,
        client,
        response_schema=response_schema,
        response_schema_name=response_schema_name,
    )
    dump_json(directory / f"{name}_response.json", response)
    return response


def _parse_design(
    *,
    context: CheckpointContext,
    planned_task: PlannedTask,
    response: dict[str, Any],
) -> DesignResponse:
    design = DesignResponse.model_validate(_without_call_metadata(response))
    problems = _validate_initial_design(
        context=context, planned_task=planned_task, design=design
    )
    if problems:
        raise AuthoringValidationError("; ".join(problems))
    return design


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _literal_excerpt(value: str) -> str:
    """Remove only quotation marks wrapped around a copied excerpt."""
    excerpt = value.strip()
    pairs = {'"': '"', "'": "'", "“": "”", "‘": "’", "`": "`"}
    if len(excerpt) >= 2 and excerpt[0] in pairs and excerpt[-1] == pairs[excerpt[0]]:
        return excerpt[1:-1].strip()
    return excerpt


def _is_date_argument(argument: str) -> bool:
    return argument == "date" or argument.endswith("_date")


def _request_value_matches_expected(
    *, argument: str, value: Any, quote: str, comparison: str | None = None
) -> bool:
    """Compare date wording semantically and every other field literally."""
    if comparison == "date" or (comparison is None and _is_date_argument(argument)):
        if not isinstance(value, str):
            return False
        expected_dates = _concrete_dates(value)
        request_dates = _concrete_dates(quote)
        return bool(expected_dates & request_dates)
    if isinstance(value, list):
        return bool(value) and all(
            _normalized_text(str(item)) in _normalized_text(quote) for item in value
        )
    return _normalized_text(str(value)) in _normalized_text(quote)


def _source_session_text(session: dict[str, Any]) -> str:
    parts: list[str] = []
    message = session.get("message")
    if isinstance(message, str):
        parts.append(message)
    messages = session.get("messages")
    if isinstance(messages, list):
        parts.extend(value for value in messages if isinstance(value, str))
    return "\n".join(parts)


def _assertion_for_expected_argument(
    *, tool: str, argument: str, value: Any, history_fact_ids: list[int],
    comparison: str | None = None,
) -> dict[str, Any]:
    """Compile the stated comparison; keep old saved-response defaults readable."""
    path = f"args.{argument}"
    if comparison is None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            comparison = "number"
        elif _is_date_argument(argument):
            comparison = "date"
        elif argument in {"to", "user", "recipient", "recipients"} and not history_fact_ids:
            comparison = "recipient_identity"
        elif isinstance(value, list):
            comparison = "list_includes"
        else:
            comparison = "exact"
    if comparison == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise AuthoringValidationError("number comparisons need a JSON number")
        return {"type": "field_eq_number", "tool": tool, "path": path, "value": value}
    if comparison == "exact":
        return {"type": "field_equals", "tool": tool, "path": path, "value": value}
    if comparison in {"list_includes", "email_delivery"}:
        values = value if isinstance(value, list) else [value]
        if not values or any(not isinstance(item, str) or not item.strip() for item in values):
            raise AuthoringValidationError(f"{comparison} needs non-empty string values")
        if comparison == "email_delivery":
            if tool != "send_email" or argument not in {"to", "cc", "bcc"}:
                raise AuthoringValidationError("email_delivery requires a send_email recipient argument")
            return {"type": "email_recipients_received", "tool": tool, "values": values}
        return {"type": "field_list_includes", "tool": tool, "path": path, "values": values}
    if not isinstance(value, str) or not value.strip():
        raise AuthoringValidationError(f"{comparison} comparisons need a non-empty string value")
    if comparison == "date":
        return {"type": "date_on", "tool": tool, "path": path, "value": value}
    if comparison == "instant":
        return {"type": "datetime_absolute_eq", "tool": tool, "path": path, "value": value}
    if comparison == "recipient_identity":
        return {
            "type": "recipient_matches",
            "tool": tool,
            "path": path,
            "value": value,
        }
    raise AuthoringValidationError(f"unknown argument comparison {comparison!r}")


def _validate_authoring_response(
    *,
    response: AuthoredTestResponse,
    context: CheckpointContext,
    planned_task: PlannedTask,
) -> None:
    """Reject unsourced, leaking, or mechanically ambiguous new-test output."""
    design = response.design.to_design_response(query=response.query)
    problems = _validate_initial_design(
        context=context, planned_task=planned_task, design=design
    )
    if problems:
        raise AuthoringValidationError("; ".join(problems))
    if design.outcome == "reject":
        return

    errors: list[str] = []
    correction_fields: set[str] = set()

    def problem(message: str, *fields: str) -> None:
        errors.append(message)
        correction_fields.update(fields)

    selected = set(planned_task.fact_ids)
    evidence_facts = {row.fact_id for row in response.history_dependent_results}
    if evidence_facts != selected:
        problem("history_dependent_results must cover every and only selected fact",
                "history_dependent_results")
    hidden_values: set[str] = set()
    visible = _normalized_text(
        response.query + "\n" + json.dumps(build_mock_state(context, design), ensure_ascii=False, sort_keys=True)
    )
    for row in response.history_dependent_results:
        fact_id = row.fact_id
        if fact_id not in selected:
            continue
        fact = context.facts_by_id[fact_id]
        source_ids = {str(value) for value in fact.get("source_session_ids") or []}
        if row.source_session_id not in source_ids:
            problem(
                f"fact {fact_id} cites source session {row.source_session_id!r} that does not support it",
                "history_dependent_results",
            )
            continue
        source = context.sessions_by_id.get(row.source_session_id)
        if source is None:
            problem(f"fact {fact_id} cites a missing source session", "history_dependent_results")
            continue
        quote = _normalized_text(row.supporting_source_text)
        if len(quote) < 3 or quote not in _normalized_text(_source_session_text(source)):
            problem(
                f"fact {fact_id} has a source quote that does not match session {row.source_session_id}",
                "history_dependent_results",
            )
        for value in row.hidden_values:
            normalized = _normalized_text(value)
            if len(normalized) < 3:
                problem("hidden values must contain at least three characters", "history_dependent_results")
            if normalized in visible and response.schema_version == 1:
                problem(f"the request or readable state reveals the history-only value {value!r}",
                        "query", "design.existing_records", "design.new_records")
            hidden_values.add(value)
    for item in response.expected_arguments:
        if item.tool not in design.expected_tool_calls:
            problem(f"expected argument uses tool {item.tool!r} outside expected_tool_calls",
                    *GRADING_AUTHORING_FIELDS)
            continue
        if item.argument not in context.tools[item.tool]["arguments"]:
            problem(
                f"expected argument names unknown field {item.tool}.{item.argument}",
                *GRADING_AUTHORING_FIELDS,
            )
        try:
            _assertion_for_expected_argument(
                tool=item.tool, argument=item.argument, value=item.value(),
                history_fact_ids=item.history_fact_ids, comparison=item.comparison,
            )
        except (ValueError, TypeError) as exc:
            problem(str(exc), *GRADING_AUTHORING_FIELDS)
        request_quote = _literal_excerpt(item.request_requirement_quote)
        if request_quote and request_quote not in response.query:
            problem("an expected argument's request quote does not appear in the user request",
                    *GRADING_AUTHORING_FIELDS)
        if not set(item.history_fact_ids).issubset(selected):
            problem("expected argument cites an unselected fact", *GRADING_AUTHORING_FIELDS)
        # Legacy responses predate mandatory meaning review. New responses keep
        # literal evidence checks here; early review judges what the request implies.
        if response.schema_version == 1 and not item.history_fact_ids:
            value = item.value()
            if not _request_value_matches_expected(
                argument=item.argument, value=value, quote=request_quote,
                comparison=item.comparison,
            ):
                problem(
                    "a request-only expected argument must quote the required value; "
                    "date arguments may use an equivalent calendar date",
                    *GRADING_AUTHORING_FIELDS,
                )

    for item in response.expected_empty_arguments:
        if item.tool not in design.expected_tool_calls:
            problem(f"empty argument uses tool {item.tool!r} outside expected_tool_calls",
                    *GRADING_AUTHORING_FIELDS)
            continue
        if item.argument not in context.tools[item.tool]["arguments"]:
            problem(f"empty argument names unknown field {item.tool}.{item.argument}",
                    *GRADING_AUTHORING_FIELDS)
        request_quote = _literal_excerpt(item.request_requirement_quote)
        if request_quote and request_quote not in response.query:
            problem("an empty argument's request quote does not appear in the user request",
                    *GRADING_AUTHORING_FIELDS)
        if not set(item.history_fact_ids).issubset(selected):
            problem("empty argument cites an unselected fact", *GRADING_AUTHORING_FIELDS)

    for item in response.expected_tool_counts:
        if item.tool not in design.expected_tool_calls:
            problem(f"expected count uses tool {item.tool!r} outside expected_tool_calls",
                    *GRADING_AUTHORING_FIELDS)
        request_quote = _literal_excerpt(item.request_requirement_quote)
        if request_quote not in response.query:
            problem("an expected tool count's request quote does not appear in the user request",
                    *GRADING_AUTHORING_FIELDS)

    for item in response.prose_requirements:
        if item.tool not in design.expected_tool_calls:
            problem(f"prose requirement uses tool {item.tool!r} outside expected_tool_calls",
                    *GRADING_AUTHORING_FIELDS)
            continue
        path = item.path if item.path.startswith("args") else f"args.{item.path}"
        if path != "args":
            if not path.startswith("args."):
                problem("prose requirement path must name a tool argument", *GRADING_AUTHORING_FIELDS)
                continue
            argument = path.split(".", 1)[1].split(".", 1)[0]
            if argument not in context.tools[item.tool]["arguments"]:
                problem(f"prose requirement names unknown field {item.tool}.{argument}",
                        *GRADING_AUTHORING_FIELDS)
        request_quote = _literal_excerpt(item.request_requirement_quote)
        if request_quote and request_quote not in response.query:
            problem("a prose requirement's request quote does not appear in the user request",
                    *GRADING_AUTHORING_FIELDS)
        if not set(item.history_fact_ids).issubset(selected):
            problem("prose requirement cites an unselected fact", *GRADING_AUTHORING_FIELDS)

    graded_fact_ids = {
        fact_id
        for item in [
            *response.expected_arguments,
            *response.expected_empty_arguments,
            *response.prose_requirements,
        ]
        for fact_id in item.history_fact_ids
    }
    if graded_fact_ids != selected:
        problem(
            "expected arguments and prose requirements must grade every and only selected fact",
            *GRADING_AUTHORING_FIELDS,
        )

    declared_request_quotes = {
        _literal_excerpt(value) for value in response.request_requirement_quotes
    }
    for quote in declared_request_quotes:
        if quote not in response.query:
            problem("a declared request requirement does not appear in the user request",
                    "request_requirement_quotes")
    covered_request_quotes = {
        _literal_excerpt(item.request_requirement_quote)
        for item in [
            *response.expected_arguments,
            *response.expected_empty_arguments,
            *response.expected_tool_counts,
            *response.prose_requirements,
        ]
        if item.request_requirement_quote.strip()
    }
    if response.schema_version == 1 and declared_request_quotes != covered_request_quotes:
        missing = sorted(declared_request_quotes - covered_request_quotes)
        undeclared = sorted(covered_request_quotes - declared_request_quotes)
        problem(
            "request requirements and request-backed grading checks differ; "
            f"missing_checks={missing}, undeclared_checks={undeclared}",
            *GRADING_AUTHORING_FIELDS,
        )

    if response.schema_version >= 2:
        approved = [item.model_dump(mode="json") for item in planned_task.required_results]
        authored = [item.model_dump(mode="json") for item in response.design.required_results]
        if authored != approved:
            problem(
                "design.required_results must copy the approved required results unchanged; "
                "reject an unsupported approved idea instead of redefining it",
                "design.required_results",
            )

    # Do not compile checks or dereference their mappings until all independent
    # evidence and field errors have been reported together.
    if errors:
        raise AuthoringValidationError("; ".join(errors), correction_fields=tuple(sorted(correction_fields)))

    if response.schema_version >= 2:
        design = _completed_design_from_authoring(
            response=response, planned_task=planned_task, context=context
        )
        approved_texts = [item.result for item in planned_task.required_results]
        mapped_texts = [item.requested_result for item in response.requested_result_checks]
        if len(mapped_texts) != len(set(mapped_texts)) or set(mapped_texts) != set(approved_texts):
            raise AuthoringValidationError(
                "requested_result_checks must cover every approved result exactly once",
                correction_fields=("requested_result_checks",),
            )
        covered = set()
        for row in response.requested_result_checks:
            planned = planned_task.required_results[approved_texts.index(row.requested_result)]
            facts = set()
            indexes = row.assertion_indexes
            if response.schema_version == 3:
                named = {check.assertion["check_id"]: index for index, check in enumerate(design.grading_checks)}
                if any(name not in named for name in row.check_ids):
                    problem("result mapping references an unknown check ID", "requested_result_checks")
                    continue
                indexes = [named[name] for name in row.check_ids]
            for index in indexes:
                if index >= len(design.grading_checks):
                    problem("requested_result_checks references an unknown check", "requested_result_checks")
                    continue
                check = design.grading_checks[index]
                if not check.fact_ids or not set(check.fact_ids).issubset(planned.fact_ids):
                    problem("a result must map only to checks supported by its facts", *GRADING_AUTHORING_FIELDS)
                facts.update(check.fact_ids)
                covered.add(index)
            if facts != set(planned.fact_ids):
                problem("a result's checks must cover all of its supporting facts", *GRADING_AUTHORING_FIELDS)
        memory_checks = {index for index, check in enumerate(design.grading_checks) if check.fact_ids}
        if memory_checks != covered:
            problem("every history-dependent check must measure an approved result", *GRADING_AUTHORING_FIELDS)
        serialized = [json.dumps({key: value for key, value in check.assertion.items()
                                  if key != "check_id"}, sort_keys=True)
                      for check in design.grading_checks]
        if len(serialized) != len(set(serialized)):
            problem("the same grading assertion occurs more than once", *GRADING_AUTHORING_FIELDS)
        if errors:
            raise AuthoringValidationError("; ".join(errors), correction_fields=tuple(sorted(correction_fields)))


def _completed_design_from_authoring(
    *,
    response: AuthoredTestResponse,
    planned_task: PlannedTask,
    context: CheckpointContext,
) -> DesignResponse:
    """Create all deterministic assertions after the one authoring call."""
    design = response.design.to_design_response(query=response.query)
    if design.outcome == "reject":
        return design
    checks: list[GradingCheck] = []
    for item in response.expected_arguments:
        checks.append(
            GradingCheck(
                fact_ids=list(item.history_fact_ids),
                assertion=_assertion_for_expected_argument(
                    tool=item.tool,
                    argument=item.argument,
                    value=item.value(),
                    history_fact_ids=list(item.history_fact_ids),
                    comparison=item.comparison,
                ),
            )
        )
    for item in response.expected_empty_arguments:
        checks.append(
            GradingCheck(
                fact_ids=list(item.history_fact_ids),
                assertion={
                    "type": "field_absent_or_empty",
                    "tool": item.tool,
                    "path": f"args.{item.argument}",
                },
            )
        )
    for item in response.prose_requirements:
        checks.append(
            GradingCheck(
                fact_ids=list(item.history_fact_ids),
                assertion={
                    "type": "field_llm_judge",
                    "tool": item.tool,
                    "path": (
                        item.path if item.path.startswith("args") else f"args.{item.path}"
                    ),
                    "criterion": item.criterion,
                },
            )
        )
    for item in response.expected_tool_counts:
        checks.append(
            GradingCheck(
                fact_ids=[],
                assertion={
                    "type": "tool_call_count",
                    "tool": item.tool,
                    "count": item.count,
                },
            )
        )
    tools_with_result_checks = {
        str(check.assertion.get("tool") or "") for check in checks
    }
    if response.schema_version == 3:
        authored_checks = [*response.expected_arguments, *response.expected_empty_arguments, *response.prose_requirements]
        for check, item in zip(checks, authored_checks):
            check.assertion.update(check_id=item.check_id, action_id=item.action_id)
        for check in checks[len(authored_checks):]:
            check.assertion.update(check_id=f"program:count:{check.assertion['tool']}", action_id="")
    for tool in design.expected_tool_calls:
        if tool not in tools_with_result_checks and tool_has_final_effect(context, tool):
            checks.append(
                GradingCheck(
                    fact_ids=[],
                    assertion={"type": "tool_called", "tool": tool, **(
                        {"check_id": f"program:final:{tool}", "action_id": f"program:final:{tool}"}
                        if response.schema_version == 3 else {}
                    )},
                )
            )
    without_memory_failed_check = next(
        (index for index, check in enumerate(checks) if check.fact_ids),
        -1,
    )
    if without_memory_failed_check < 0:
        raise AuthoringValidationError(
            "the generated grading checks do not measure any selected fact"
        )
    completed = design.model_copy(
        update={
            "grading_checks": checks,
            "check_version": 2 if response.schema_version == 3 else 1,
            "answers_that_must_not_appear": [] if response.schema_version >= 2 else list(
                dict.fromkeys(
                    value
                    for result in response.history_dependent_results
                    for value in result.hidden_values
                )
            ),
            "with_history_result": response.with_history_expected_result,
            "without_history_result": response.without_history_expected_result,
            "without_memory_failed_check": without_memory_failed_check,
        }
    )
    return completed


def _validate_initial_design(
    *, context: CheckpointContext, planned_task: PlannedTask, design: DesignResponse
) -> list[str]:
    """Validate work that must be settled before request writing."""
    if design.outcome == "reject":
        return []
    problems: list[str] = []
    try:
        design.blind_request_handoff()
    except ValueError as exc:
        problems.append(str(exc))
    if not design.expected_tool_calls:
        problems.append("expected_tool_calls is empty")
    if not design.required_results:
        problems.append("required_results is empty")
    if any(not result.fact_ids for result in design.required_results):
        problems.append(
            "required_results may contain only results that depend on selected facts"
        )
    required_fact_ids = {
        fact_id for result in design.required_results for fact_id in result.fact_ids
    }
    if required_fact_ids != set(planned_task.fact_ids):
        problems.append(
            "required_results must cover every and only selected fact; "
            f"covered={sorted(required_fact_ids)}, selected={sorted(planned_task.fact_ids)}"
        )
    unknown = [tool for tool in design.expected_tool_calls if tool not in context.tools]
    if unknown:
        problems.append(f"expected_tool_calls contains unknown tools {unknown}")
    missing = set(planned_task.expected_tools) - set(design.expected_tool_calls)
    if missing:
        problems.append(
            "expected_tool_calls omits planned actions: " + str(sorted(missing))
        )
    try:
        build_mock_state(context, design)
    except AuthoringValidationError as exc:
        problems.append(str(exc))
    return problems


def _supported_assertion_shapes() -> list[dict[str, str]]:
    """Describe the local grader's supported assertions in model-readable form."""
    shapes = [
        ("tool_called", '{"type":"tool_called","tool":"tool_name"}', "the tool must be used"),
        ("tool_not_called", '{"type":"tool_not_called","tool":"tool_name"}', "the tool must not be used"),
        ("tool_call_count", '{"type":"tool_call_count","tool":"tool_name","count":1}', "the exact number of calls matters"),
        ("field_equals", '{"type":"field_equals","tool":"tool_name","path":"args.argument_name","value":"exact value"}', "one argument needs one exact value"),
        ("field_list_includes", '{"type":"field_list_includes","tool":"tool_name","path":"args.argument_name","values":["required member"]}', "required list members, with no implied order or exclusivity"),
        ("email_recipients_received", '{"type":"email_recipients_received","tool":"send_email","path":"args.to","values":["required recipient"]}', "required recipients receive the same email through To, CC, or BCC"),
        ("datetime_absolute_eq", '{"type":"datetime_absolute_eq","tool":"tool_name","path":"args.argument_name","value":"2028-01-02T23:00:00Z"}', "the same timezone-aware instant, accepting equivalent offsets"),
        ("recipient_matches", '{"type":"recipient_matches","tool":"tool_name","path":"args.to","value":"requested recipient"}', "the literal requested recipient must pass and an unambiguous alias may pass"),
        ("field_absent_or_empty", '{"type":"field_absent_or_empty","tool":"tool_name","path":"args.cc"}', "an explicitly restricted optional argument must be absent or empty"),
        ("field_lte", '{"type":"field_lte","tool":"tool_name","path":"args.argument_name","value":100}', "one numeric argument has a maximum"),
        ("field_gte", '{"type":"field_gte","tool":"tool_name","path":"args.argument_name","value":100}', "one numeric argument has a minimum"),
        ("field_eq_number", '{"type":"field_eq_number","tool":"tool_name","path":"args.argument_name","value":100}', "one numeric argument needs an exact value"),
        ("date_on", '{"type":"date_on","tool":"tool_name","path":"args.argument_name","value":"YYYY-MM-DD"}', "a date argument needs one date"),
        ("date_on_or_after", '{"type":"date_on_or_after","tool":"tool_name","path":"args.argument_name","value":"YYYY-MM-DD"}', "a date argument has a minimum date"),
        ("date_on_or_before", '{"type":"date_on_or_before","tool":"tool_name","path":"args.argument_name","value":"YYYY-MM-DD"}', "a date argument has a maximum date"),
        ("time_of_day_gte", '{"type":"time_of_day_gte","tool":"tool_name","path":"args.argument_name","value":"HH:MM"}', "a time argument has a minimum time"),
        ("time_of_day_lte", '{"type":"time_of_day_lte","tool":"tool_name","path":"args.argument_name","value":"HH:MM"}', "a time argument has a maximum time"),
        ("field_regex", '{"type":"field_regex","tool":"tool_name","path":"args.argument_name","pattern":"regex"}', "a stable literal pattern is required"),
        ("field_not_regex", '{"type":"field_not_regex","tool":"tool_name","path":"args.argument_name","pattern":"regex"}', "a stable literal pattern must not appear"),
        ("tool_called_with_arg_regex", '{"type":"tool_called_with_arg_regex","tool":"tool_name","path":"args.argument_name","pattern":"regex"}', "one call needs an argument matching a pattern"),
        ("tool_called_with_value", '{"type":"tool_called_with_value","tool":"tool_name","pattern":"regex"}', "one call needs a value matching a pattern"),
        ("tool_not_called_with_arg_regex", '{"type":"tool_not_called_with_arg_regex","tool":"tool_name","path":"args.argument_name","pattern":"regex"}', "no call may use an argument matching a pattern"),
        ("field_llm_judge", '{"type":"field_llm_judge","tool":"tool_name","path":"args.argument_name","criterion":"one yes-or-no requirement"}', "prose can be correct in different words"),
    ]
    unexpected = {name for name, _, _ in shapes} ^ set(SUPPORTED_ASSERTIONS)
    if unexpected:
        raise RuntimeError(
            "supported assertion description is out of sync with the local grader: "
            f"{sorted(unexpected)}"
        )
    return [
        {"name": name, "shape": shape, "when_to_use": when_to_use}
        for name, shape, when_to_use in shapes
    ]


def _original_design_input(design_request: dict[str, Any]) -> dict[str, Any]:
    """Read source evidence from an initial or redesign request payload."""
    original = design_request.get("original_input")
    return original if isinstance(original, dict) else design_request


def _grading_design_request(
    *,
    design: DesignResponse,
    blind_query: BlindQueryResponse,
    planned_task: PlannedTask,
    context: CheckpointContext,
    config: AuthoringConfig,
    design_request: dict[str, Any],
) -> dict[str, Any]:
    """Give grading the final request and every fact it needs to judge it fairly."""
    source_input = _original_design_input(design_request)
    if not source_input.get("selected_fact_evidence"):
        source_input = design_payload(
            config=config, context=context, planned_task=planned_task
        )
    return {
        "final_user_request": blind_query.query,
        "planned_required_results": [
            result.model_dump(mode="json", exclude_none=True)
            for result in design.required_results
        ],
        "selected_fact_evidence": copy.deepcopy(
            source_input.get("selected_fact_evidence") or []
        ),
        "current_facts_that_may_change_the_meaning": copy.deepcopy(
            source_input.get("current_facts_that_may_change_the_meaning") or []
        ),
        "tools": copy.deepcopy(source_input.get("tools") or context.tools),
        "current_state_for_this_test": build_mock_state(context, design),
        "expected_tool_calls": list(design.expected_tool_calls),
        "supported_assertion_shapes": _supported_assertion_shapes(),
    }


def _parse_grading_design(
    *,
    response: dict[str, Any],
    design: DesignResponse,
    planned_task: PlannedTask,
    context: CheckpointContext,
) -> DesignResponse:
    """Validate later grading and convert it to the existing candidate shape."""
    grading = GradingDesignResponse.model_validate(_without_call_metadata(response))
    expected_results = [result.result for result in design.required_results]
    mapped_results = [item.requested_result for item in grading.requested_result_checks]
    if len(expected_results) != len(mapped_results) or set(expected_results) != set(mapped_results):
        raise AuthoringValidationError(
            "requested_result_checks must map every and only planned required result"
        )
    completed = design.with_grading(grading)
    problems = validate_design(context, planned_task, completed)
    if problems:
        raise AuthoringValidationError("; ".join(problems))
    return completed


def _blind_query_request(design: DesignResponse) -> dict[str, str]:
    """Build the fact-free payload for the blind request-writing call."""
    return design.blind_request_handoff()


def _blind_query_system_with_final_review_feedback(
    *, specific_problem: str, required_correction: str
) -> str:
    """Give the existing blind writer one concrete correction without facts."""
    return (
        BLIND_QUERY_SYSTEM
        + "\n\nA reviewer rejected the previous request. Rewrite only the request. "
        "Keep the four supplied fields unchanged. Do not add a fact, date, label, "
        "recipient, decision, or claim that those fields do not state.\n"
        f"The reviewer found this problem: {specific_problem}\n"
        f"Make this correction: {required_correction}\n"
        "Return only the required JSON object."
    )


def _certify_cached(
    *,
    persona: str,
    checkpoint: Path,
    candidate_path: Path,
    result_path: Path,
    certifier: Certifier,
    authoring_evidence: dict[str, Any] | None = None,
    oracle_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_sha256 = _sha256(candidate_path)
    if result_path.is_file():
        saved = json.loads(result_path.read_text())
        if (saved.get("candidate_sha256") == candidate_sha256
                and (authoring_evidence is None or saved.get("authoring_evidence") == authoring_evidence)
                and (oracle_input is None or saved.get("oracle_input") == oracle_input)):
            return saved["result"]
    result = certifier(
        persona,
        candidate_path,
        checkpoint=checkpoint,
        confirm_paid_calls=True,
        **({"oracle_input": oracle_input} if oracle_input is not None else {}),
    )
    wrapped = {"candidate_sha256": candidate_sha256, "result": result}
    if authoring_evidence is not None:
        wrapped["authoring_evidence"] = copy.deepcopy(authoring_evidence)
    if oracle_input is not None:
        wrapped["oracle_input"] = copy.deepcopy(oracle_input)
    if result.get("verdict") == "infra_error":
        dump_json(result_path.with_name(f"{result_path.stem}_infra_error.json"), wrapped)
        if authoring_evidence is None:
            raise RuntimeError("certification stopped because an oracle shot had an infrastructure error")
    dump_json(result_path, wrapped)
    return result


def _attempt(
    *,
    directory: Path,
    attempt_name: str,
    design_system: str,
    design_request: dict[str, Any],
    planned_task: PlannedTask,
    context: CheckpointContext,
    config: AuthoringConfig,
    test_id: int,
    design_client: AzureJsonClient,
    query_client: AzureJsonClient,
    certifier: Certifier,
    grading_client: AzureJsonClient | None = None,
    reuse_design: DesignResponse | None = None,
    query_final_review_feedback: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    try:
        if reuse_design is None:
            raw_design = _model_call(
                directory=directory,
                name=f"{attempt_name}_design",
                system=design_system,
                payload=design_request,
                client=design_client,
            )
            design = _parse_design(
                context=context, planned_task=planned_task, response=raw_design
            )
        else:
            design = reuse_design
            raw_design = {"reused_validated_design": design.model_dump(mode="json")}
        if design.outcome == "reject":
            return None, {
                "stage": "proposal_rejected",
                "reason": design.rejection_reason,
                "design": _without_call_metadata(raw_design),
                "query": {},
            }
        query_request = _blind_query_request(design)
        raw_query = _model_call(
            directory=directory,
            name=f"{attempt_name}_blind_query",
            system=(
                _blind_query_system_with_final_review_feedback(
                    specific_problem=query_final_review_feedback["specific_problem"],
                    required_correction=query_final_review_feedback["required_correction"],
                )
                if query_final_review_feedback is not None
                else BLIND_QUERY_SYSTEM
            ),
            payload=query_request,
            client=query_client,
        )
        query = BlindQueryResponse.model_validate(_without_call_metadata(raw_query))
        raw_grading = _model_call(
            directory=directory,
            name=f"{attempt_name}_grading",
            system=GRADING_DESIGN_SYSTEM,
            payload=_grading_design_request(
                design=design,
                blind_query=query,
                planned_task=planned_task,
                context=context,
                config=config,
                design_request=design_request,
            ),
            client=grading_client or design_client,
        )
        completed_design = _parse_grading_design(
            response=raw_grading,
            design=design,
            planned_task=planned_task,
            context=context,
        )
        candidate = assemble_candidate(
            context=context,
            planned_task=planned_task,
            evaluation_date=config.evaluation_date,
            test_id=str(test_id),
            design=completed_design,
            blind_query=query,
        )
        candidate_path = directory / f"{attempt_name}_candidate.yaml"
        write_candidate(candidate_path, candidate)
        gate = _certify_cached(
            persona=config.persona,
            checkpoint=context.checkpoint_path,
            candidate_path=candidate_path,
            result_path=directory / f"{attempt_name}_gate.json",
            certifier=certifier,
        )
        if not gate.get("valid"):
            return None, {
                "stage": "certification",
                "reason": str(gate.get("verdict") or "invalid"),
                "design": completed_design.model_dump(mode="json"),
                "initial_design": _without_call_metadata(raw_design),
                "query": _without_call_metadata(raw_query),
                "grading": _without_call_metadata(raw_grading),
                "gate": gate,
            }
        return candidate, {
            "stage": "accepted",
            "design": completed_design.model_dump(mode="json"),
            "initial_design": _without_call_metadata(raw_design),
            "query": _without_call_metadata(raw_query),
            "grading": _without_call_metadata(raw_grading),
            "gate": gate,
            "reused_design": reuse_design is not None,
            "final_review_feedback_for_request_writer": (
                dict(query_final_review_feedback)
                if query_final_review_feedback is not None
                else None
            ),
        }
    except (ValidationError, AuthoringValidationError, KeyError, TypeError, ValueError) as exc:
        return None, {
            "stage": "local_validation",
            "reason": f"{type(exc).__name__}: {exc}",
            "design": _without_call_metadata(locals().get("raw_design", {})),
            "query": _without_call_metadata(locals().get("raw_query", {})),
            "grading": _without_call_metadata(locals().get("raw_grading", {})),
        }


def _author_complete_test_attempt(
    *, directory: Path, attempt_name: str, payload: dict[str, Any],
    planned_task: ApprovedIdea, context: CheckpointContext, config: AuthoringConfig,
    test_id: int, client: Any, certifier: Certifier,
    reuse_authoring: dict[str, Any] | None = None,
    reuse_preflight_response: dict[str, Any] | None = None,
    reuse_preflight_request: dict[str, Any] | None = None,
    model_corrections_used: int | None = None,
    regrade_saved_gate: Path | None = None,
    regrader: Any = None,
    stop_after_preflight: bool = False,
    stop_after_writing: bool = False,
    validation_options: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    from authoring.complete_test import (
        assemble_written_test, authoring_evidence, certification_input, correction_scope, parse_writer_response,
        validate_writer_correction, writer_input_tokens, writer_response_schema,
    )
    from authoring.final_trace_review import build_test_review_request
    from authoring.review import check_review_examples, execute_review, review_decision

    stage = "authoring_pending"
    candidate_path = directory / f"{attempt_name}_candidate.yaml"
    gate_path = directory / f"{attempt_name}_gate.json"
    response_path = directory / f"{attempt_name}_authoring_response.json"
    raw: dict[str, Any] = {}
    validation_input = {**payload.get("original_authoring_input", payload), **(validation_options or {})}
    production = validation_input.get("workflow") == "production"
    writer_system = COMPLETE_TEST_WRITER_SYSTEM
    if production:
        from authoring.prompts import _prompt_text
        writer_system = _prompt_text("writer_production.md")

    def record_progress() -> None:
        artifacts = authoring_artifacts(directory, attempt_name)
        bound = payload.get("saved_execution_gate") or {}
        if isinstance(bound.get("file"), str) and Path(bound["file"]).name == bound["file"]:
            artifacts.append(directory / bound["file"])
        save_progress(
            path=directory / "progress.json", inputs_path=directory.parents[1] / "run_inputs.json",
            result={
                "id": test_id, "fact_ids": list(planned_task.fact_ids), "status": stage,
                "candidate": str(candidate_path) if candidate_path.is_file() else None,
                "gate": str(gate_path) if gate_path.is_file() else None,
                "authoring_response": str(response_path) if response_path.is_file() else None,
                "authoring_mode": "unified", "authoring_version": 4, "attempt_name": attempt_name,
                "model_corrections_used": model_corrections_used if model_corrections_used is not None else int(attempt_name != "initial"),
                **({"execution_reruns_used": 1} if (directory / "reserved_execution_retry.json").is_file() else {}),
            },
            artifact_paths=artifacts,
        )

    try:
        record_progress()
        schema = writer_response_schema(
            payload=validation_input if reuse_authoring is None else None,
        )
        if production and reuse_authoring is None:
            schema["properties"]["quality_audit"] = {"type": "null"}
        tokens = writer_input_tokens(writer_system, payload, schema)
        dump_json(directory / f"{attempt_name}_input_budget.json", {
            "encoding": "o200k_base", "serialized_input_tokens": tokens,
            "maximum": config.writer_max_input_tokens,
        })
        if reuse_authoring is None and tokens > config.writer_max_input_tokens:
            stage = "input_pending"
            raise ValueError(f"writer needs {tokens} input tokens; configured budget is {config.writer_max_input_tokens}; required data was not truncated")
        if reuse_authoring is not None:
            raw = copy.deepcopy(reuse_authoring)
            request_file = directory / f"{attempt_name}_authoring_request.json"
            schema_file = directory / f"{attempt_name}_authoring_schema.json"
            system_file = directory / f"{attempt_name}_authoring_system.txt"
            if request_file.exists():
                if json.loads(request_file.read_text()) != payload:
                    raise ValueError("saved writer request differs from the reused authoring input")
            else:
                dump_json(request_file, payload)
            if not schema_file.exists():
                dump_json(schema_file, {"response_schema": schema, "response_schema_name": "dolphinbench_complete_test_v4"})
            if not system_file.exists():
                system_file.write_text(writer_system + "\n")
            dump_json(response_path, raw)
        else:
            raw = _model_call(
                directory=directory, name=f"{attempt_name}_authoring",
                system=writer_system, payload=payload, client=client,
                response_schema=schema, response_schema_name="dolphinbench_complete_test_v4",
            )
        stage = "validation_pending"
        record_progress()
        response = parse_writer_response(raw, require_quality_audit=reuse_authoring is None and not production)
        bound = payload.get("saved_execution_gate")
        if bound is not None:
            if (not isinstance(bound, dict) or set(bound) != {"file", "sha256"}
                    or not isinstance(bound["file"], str) or Path(bound["file"]).name != bound["file"]):
                raise ValueError("invalid saved-execution binding")
            regrade_saved_gate = directory / bound["file"]
            if not regrade_saved_gate.is_file() or _sha256(regrade_saved_gate) != bound["sha256"]:
                raise ValueError("saved executions for the grading correction changed")
        original_input = payload.get("original_authoring_input", payload)
        if original_input.get("approved_idea") != planned_task.model_dump(mode="json"):
            raise ValueError("writer input does not match the approved idea")
        if payload.get("previous_authoring"):
            validate_writer_correction(parse_writer_response(payload["previous_authoring"]), response, payload)
        dump_json(directory / f"{attempt_name}_authoring_validated.json", response.model_dump(mode="json"))
        if response.test is None:
            return None, {"stage": "proposal_rejected", "reason": response.problem,
                          "authoring": _without_call_metadata(raw), "authoring_mode": "unified"}
        candidate = assemble_written_test(
            response=response, idea=planned_task, context=context, evaluation_date=config.evaluation_date,
            test_id=test_id, payload=validation_input,
        )
        write_candidate(candidate_path, candidate)
        evidence = authoring_evidence(response)
        stage = "preflight_pending"
        record_progress()
        if stop_after_writing:
            return None, {"stage": stage, "candidate": str(candidate_path),
                          "authoring": _without_call_metadata(raw), "authoring_mode": "unified"}
        request = build_test_review_request(candidate_path=candidate_path, planned_task=planned_task, context=context)
        request["authoring_evidence"] = evidence
        review_out = directory / f"{attempt_name}_preflight"
        review = execute_review(
            request=request, out=review_out, client=client, confirm_paid_calls=True,
            saved_response=reuse_preflight_response, saved_request=reuse_preflight_request,
        )
        decision = review_decision(review, request).model_dump(mode="json")
        if review.decision != "approve":
            scope = correction_scope([row.model_dump(mode="json") for row in review.issues], response)
            return None, {
                "stage": "preflight_rejected", "reason": decision["specific_problem"],
                "authoring": _without_call_metadata(raw), "candidate": str(candidate_path),
                "preflight": {"passed": False, "decision": decision},
                **scope, "authoring_mode": "unified",
            }
        examples = check_review_examples(response=review, request=request, out=review_out / "grading_examples")
        dump_json(
            review_out / "result.json",
            {
                "candidate_sha256": _sha256(candidate_path),
                "decision": decision,
                "passed": examples["passed"],
                "grading_examples": str(review_out / "grading_examples" / "result.json"),
            },
        )
        if not examples["passed"]:
            return None, {
                "stage": "preflight_pending", "reason": "the actual grader disagrees with the review examples",
                "authoring": _without_call_metadata(raw), "candidate": str(candidate_path),
                "preflight": {"passed": False, "decision": decision, "examples": examples},
                "authoring_mode": "unified",
            }
        stage = "certification_pending"
        record_progress()
        if stop_after_preflight:
            return None, {
                "stage": stage, "reason": "stopped after successful preflight as requested",
                "candidate": str(candidate_path), "authoring_mode": "unified",
                "preflight": {"passed": True, "decision": decision, "examples": examples},
            }
        oracle_input = certification_input(context, planned_task, config.evaluation_date)
        if regrade_saved_gate is not None:
            from authoring.create_tests import _default_regrader, _regraded_gate
            if json.loads(regrade_saved_gate.read_text()).get("oracle_input") != oracle_input:
                raise ValueError("grading-only correction changed the assistant's supplied history or instructions")
            wrapped = _regraded_gate(candidate_path=candidate_path, saved_gate_path=regrade_saved_gate,
                                    regrader=regrader or _default_regrader)
            wrapped["authoring_evidence"] = evidence
            wrapped["oracle_input"] = oracle_input
            dump_json(gate_path, wrapped)
            gate = wrapped["result"]
        else:
            gate = _certify_cached(
                persona=config.persona, checkpoint=context.checkpoint_path,
                candidate_path=candidate_path, result_path=gate_path, certifier=certifier,
                authoring_evidence=evidence,
                oracle_input=oracle_input,
            )
        stage = ("certified_initial" if attempt_name == "initial" else "certified_correction") if gate.get("valid") else "review_required"
        record_progress()
        details = {"authoring": _without_call_metadata(raw), "gate": gate, "authoring_mode": "unified"}
        if not gate.get("valid"):
            return None, {"stage": "certification", "reason": gate.get("verdict", "failed"), **details}
        return candidate, {"stage": "accepted", **details}
    except Exception as exc:
        record_progress()
        from authoring.runtime_policy import is_technical_error
        return None, {
            "stage": stage, "reason": f"{type(exc).__name__}: {exc}",
            "technical_error": is_technical_error(exc),
            "validation_correction_fields": list(getattr(exc, "correction_fields", ())),
            "authoring": _without_call_metadata(raw),
            "candidate": str(candidate_path) if candidate_path.is_file() else None,
            "authoring_mode": "unified",
        }


def _author_new_test_attempt(
    *,
    directory: Path,
    attempt_name: str,
    system: str,
    payload: dict[str, Any],
    planned_task: PlannedTask,
    context: CheckpointContext,
    config: AuthoringConfig,
    test_id: int,
    client: AzureJsonClient,
    certifier: Certifier,
    reuse_authoring: dict[str, Any] | None = None,
    reuse_preflight_response: dict[str, Any] | None = None,
    reuse_preflight_request: dict[str, Any] | None = None,
    model_corrections_used: int | None = None,
    regrade_saved_gate: Path | None = None,
    regrader: Any = None,
    stop_after_preflight: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Create a new test with one authoring call, then certify it."""
    if isinstance(planned_task, ApprovedIdea):
        return _author_complete_test_attempt(
            directory=directory, attempt_name=attempt_name, payload=payload, planned_task=planned_task,
            context=context, config=config, test_id=test_id, client=client, certifier=certifier,
            reuse_authoring=reuse_authoring, reuse_preflight_response=reuse_preflight_response,
            reuse_preflight_request=reuse_preflight_request, model_corrections_used=model_corrections_used,
            regrade_saved_gate=regrade_saved_gate, regrader=regrader, stop_after_preflight=stop_after_preflight,
        )
    stage = "authoring_pending"
    candidate_path = directory / f"{attempt_name}_candidate.yaml"
    gate_path = directory / f"{attempt_name}_gate.json"
    response_path = directory / f"{attempt_name}_authoring_response.json"

    def record_progress() -> None:
        save_progress(
            path=directory / "progress.json",
            inputs_path=directory.parents[1] / "run_inputs.json",
            result={
                "id": test_id, "fact_ids": list(planned_task.fact_ids),
                "status": stage,
                "candidate": str(candidate_path) if candidate_path.is_file() else None,
                "gate": str(gate_path) if gate_path.is_file() else None,
                "authoring_response": str(response_path) if response_path.is_file() else None,
                "authoring_mode": "unified", "attempt_name": attempt_name,
                "model_corrections_used": model_corrections_used if model_corrections_used is not None else int(attempt_name != "initial"),
            },
            artifact_paths=authoring_artifacts(directory, attempt_name),
        )

    try:
        record_progress()
        if reuse_authoring is not None:
            raw = copy.deepcopy(reuse_authoring)
            dump_json(response_path, raw)
        else:
            raw = _model_call(
                directory=directory,
                name=f"{attempt_name}_authoring",
                system=system,
                payload=payload,
                client=client,
                response_schema=_authoring_response_schema(
                    payload.get("original_authoring_input", payload).get("authoring_schema_version", 3)
                ),
                response_schema_name="dolphinbench_complete_test",
            )
        stage = "local_validation"
        record_progress()
        authored = AuthoredTestResponse.model_validate(_without_call_metadata(raw))
        original_input = payload.get("original_authoring_input", payload)
        expected_version = original_input.get("authoring_schema_version")
        if expected_version in (2, 3) and authored.schema_version != expected_version:
            raise AuthoringValidationError(f"new authoring must return schema_version {expected_version}", correction_fields=("schema_version",))
        if authored.design.outcome != "reject" and payload.get("previous_authoring"):
            _validate_correction_scope(
                previous=payload["previous_authoring"], current=authored,
                allowed_fields=payload.get("allowed_correction_fields") or [],
            )
        _validate_authoring_response(
            response=authored, context=context, planned_task=planned_task
        )
        if authored.design.outcome == "reject":
            return None, {
                "stage": "proposal_rejected",
                "reason": authored.design.rejection_reason,
                "authoring": _without_call_metadata(raw),
            }
        design = _completed_design_from_authoring(
            response=authored, planned_task=planned_task, context=context
        )
        problems = validate_design(context, planned_task, design)
        if problems:
            raise AuthoringValidationError("; ".join(problems))
        query = BlindQueryResponse(query=authored.query)
        candidate = assemble_candidate(
            context=context,
            planned_task=planned_task,
            evaluation_date=config.evaluation_date,
            test_id=str(test_id),
            design=design,
            blind_query=query,
        )
        write_candidate(candidate_path, candidate)
        if authored.schema_version >= 2:
            stage = "preflight_pending"
            record_progress()
            preflight = execute_preflight(
                candidate_path=candidate_path, planned_task=planned_task, context=context,
                authoring_evidence={
                    "history_dependent_results": [row.model_dump(mode="json") for row in authored.history_dependent_results],
                    "requested_result_checks": [row.model_dump(mode="json") for row in authored.requested_result_checks],
                },
                out=directory / f"{attempt_name}_preflight", client=client,
                confirm_paid_calls=True,
                saved_response=reuse_preflight_response,
                saved_request=reuse_preflight_request,
            )
            if not preflight["passed"]:
                decision = preflight["decision"]
                return None, {
                    "stage": "preflight_rejected" if not decision["accept"] else "preflight_pending",
                    "reason": decision.get("specific_problem") or preflight.get("reason"),
                    "authoring": _without_call_metadata(raw),
                    "candidate": str(candidate_path), "preflight": preflight,
                    "allowed_correction_fields": _correction_fields_for_issues(decision["issues"]),
                    "authoring_mode": "unified",
                }
        stage = "certification_pending"
        record_progress()
        gate = _certify_cached(
            persona=config.persona,
            checkpoint=context.checkpoint_path,
            candidate_path=candidate_path,
            result_path=gate_path,
            certifier=certifier,
        )
        evidence = {
            "authoring": _without_call_metadata(raw),
            "design": design.model_dump(mode="json"),
            "query": {"query": query.query},
            "mechanical_assertion_count": len(design.expected_tool_calls)
            + len(authored.expected_arguments),
            "prose_assertion_count": len(authored.prose_requirements),
            "gate": gate,
            "authoring_mode": "unified",
            "preflight": locals().get("preflight"),
        }
        if not gate.get("valid"):
            stage = "review_required"
            record_progress()
            return None, {
                "stage": "certification",
                "reason": str(gate.get("verdict") or "invalid"),
                **evidence,
            }
        stage = "certified_initial" if attempt_name == "initial" else "certified_correction"
        record_progress()
        return candidate, {"stage": "accepted", **evidence}
    except Exception as exc:
        allowed_fields = list(getattr(exc, "correction_fields", ()))
        if stage == "local_validation" and isinstance(exc, ValidationError):
            locations = [error["loc"] for error in exc.errors()]
            if locations and all(location for location in locations):
                allowed_fields = [".".join(str(part) for part in location) for location in locations]
        if stage == "local_validation" and not allowed_fields:
            stage = "validation_pending"
        record_progress()
        return None, {
            "stage": stage,
            "reason": f"{type(exc).__name__}: {exc}",
            "authoring": _without_call_metadata(locals().get("raw", {})),
            "candidate": str(candidate_path) if candidate_path.is_file() else None,
            "allowed_correction_fields": allowed_fields,
            "authoring_mode": "unified",
        }


def _run_identity(
    *,
    config_path: Path,
    plan_path: Path,
    config: AuthoringConfig,
    context: CheckpointContext,
    planned_tasks: list[PlannedTask],
    limit: int | None,
    start_id: int | None,
    test_ids: list[int] | None,
    selected_test_ids: list[int],
    selected_plan_positions: list[int],
) -> dict[str, Any]:
    system = COMPLETE_TEST_WRITER_SYSTEM if all(isinstance(task, ApprovedIdea) for task in planned_tasks) else AUTHOR_TEST_SYSTEM
    return {
        "config_path": str(config_path.resolve()),
        "config_sha256": _sha256(config_path),
        "plan_path": str(plan_path.resolve()),
        "plan_sha256": _sha256(plan_path),
        "persona": config.persona,
        "checkpoint": str(context.checkpoint_path),
        "checkpoint_identity": context.checkpoint_identity,
        "evaluation_date": config.evaluation_date,
        "planned_task_fact_ids": [task.fact_ids for task in planned_tasks],
        "limit": limit,
        "start_id": start_id,
        "test_ids": test_ids,
        "selected_test_ids": selected_test_ids,
        "selected_plan_positions": selected_plan_positions,
        "authoring_system_sha256": hashlib.sha256(system.encode()).hexdigest(),
        "reauthoring_system_sha256": hashlib.sha256(REAUTHOR_TEST_SYSTEM.encode()).hexdigest(),
    }


def parse_only_positions(value: str | None) -> list[int] | None:
    """Parse the one-based plan positions supplied to ``--only``."""
    if value is None:
        return None
    parts = [part.strip() for part in value.split(",")]
    if not parts or any(not part for part in parts):
        raise ValueError("--only must be a comma-separated list of plan positions")
    try:
        positions = [int(part) for part in parts]
    except ValueError as exc:
        raise ValueError("--only must contain only integer plan positions") from exc
    if any(position <= 0 for position in positions):
        raise ValueError("--only positions must be positive")
    if len(positions) != len(set(positions)):
        raise ValueError("--only positions must be unique")
    return positions


def parse_test_ids(value: str | None) -> list[int] | None:
    """Parse the test IDs assigned to every task in the plan."""
    if value is None:
        return None
    parts = [part.strip() for part in value.split(",")]
    if not parts or any(not part for part in parts):
        raise ValueError("--test-ids must be a comma-separated list of test IDs")
    try:
        test_ids = [int(part) for part in parts]
    except ValueError as exc:
        raise ValueError("--test-ids must contain only integer test IDs") from exc
    if any(test_id <= 0 for test_id in test_ids):
        raise ValueError("--test-ids must be positive")
    if len(test_ids) != len(set(test_ids)):
        raise ValueError("--test-ids must be unique")
    return test_ids


def _create_one_test(
    *, directory: Path, accepted_dir: Path, payload: dict[str, Any],
    planned_task: PlannedTask, context: CheckpointContext, config: AuthoringConfig,
    test_id: int, client: Any, certifier: Certifier,
    model_corrections_used: int = 0, reuse_authoring: dict[str, Any] | None = None,
    reuse_preflight_response: dict[str, Any] | None = None,
    reuse_preflight_request: dict[str, Any] | None = None,
    allow_model_correction: bool = True, regrader: Any = None,
    stop_after_preflight: bool = False,
) -> dict[str, Any]:
    """Use the same bounded writing flow for a new test and a saved-stage resume."""
    if model_corrections_used not in (0, 1):
        raise ValueError("a test may have at most one model-written correction")
    original_payload = payload.get("original_authoring_input", payload)
    first_failure = None
    for attempt_number in range(2 - model_corrections_used):
        attempt_name = "initial" if attempt_number == 0 else "correction"
        candidate, attempt = _author_new_test_attempt(
            directory=directory, attempt_name=attempt_name,
            system=AUTHOR_TEST_SYSTEM + ("\n\n" + REAUTHOR_TEST_SYSTEM if payload.get("previous_authoring") else ""),
            payload=payload, planned_task=planned_task, context=context, config=config,
            test_id=test_id, client=client, certifier=certifier,
            model_corrections_used=model_corrections_used,
            reuse_authoring=reuse_authoring if attempt_number == 0 else None,
            reuse_preflight_response=reuse_preflight_response if attempt_number == 0 else None,
            reuse_preflight_request=reuse_preflight_request if attempt_number == 0 else None,
            regrader=regrader, stop_after_preflight=stop_after_preflight,
        )
        result: dict[str, Any] = {
            "id": test_id, "fact_ids": list(planned_task.fact_ids),
            "authoring_mode": "unified", "attempt_name": attempt_name,
            "model_corrections_used": model_corrections_used,
        }
        if first_failure is not None:
            result["first_failure"] = first_failure
        if candidate is not None:
            final_path = accepted_dir / f"{test_id:03d}.yaml"
            write_candidate(final_path, candidate)
            result.update(
                status="certified_correction" if model_corrections_used else "certified_initial",
                candidate=str(final_path), gate=str(directory / f"{attempt_name}_gate.json"),
            )
            return result
        result["redesign_failure" if first_failure is not None else "first_failure"] = attempt
        stage = attempt["stage"]
        if stage == "certification":
            result.update(
                status="review_required", candidate=str(directory / f"{attempt_name}_candidate.yaml"),
                gate=str(directory / f"{attempt_name}_gate.json"),
            )
            return result
        if stage.endswith("_pending"):
            result.update(status=stage, candidate=attempt.get("candidate"))
            return result
        if not allow_model_correction:
            result.update(status="repair_pending", reason="the exact repair needs review; no further writer call is authorized")
            return result
        if stage == "proposal_rejected" or (
            stage == "preflight_rejected" and attempt["preflight"]["decision"]["part_to_correct"] == "planning"
        ):
            result.update(status="replacement_required", reason="proposal_rejected", proposal_rejected=attempt)
            return result
        if model_corrections_used:
            result["status"] = "replacement_required"
            return result
        if not attempt.get("allowed_correction_fields"):
            result.update(status="validation_pending", reason="the failure has no identified correction scope")
            return result
        first_failure = attempt
        model_corrections_used = 1
        payload = {
            "original_authoring_input": original_payload,
            "previous_authoring": attempt.get("authoring") or {},
            "allowed_correction_fields": attempt["allowed_correction_fields"],
            **({"protected_check_ids": attempt["protected_check_ids"]} if "protected_check_ids" in attempt else {}),
            "failure_to_fix": {
                "stage": stage, "reason": attempt.get("reason"), "preflight": attempt.get("preflight"),
            },
        }
    raise AssertionError("bounded authoring must return after its final attempt")


def run_authoring(
    *,
    config_path: Path,
    plan_path: Path,
    out: Path,
    limit: int | None,
    only: list[int] | None = None,
    start_id: int | None,
    test_ids: list[int] | None = None,
    design_client: AzureJsonClient,
    query_client: AzureJsonClient,
    certifier: Certifier = certify_candidate,
    confirm_paid_calls: bool = False,
) -> dict[str, Any]:
    if not confirm_paid_calls:
        raise ValueError("refusing model and certification calls without explicit confirmation")
    config = load_config(config_path)
    context = load_checkpoint_context(config)
    planned_tasks = load_authoring_tasks(context=context, config=config, path=plan_path)
    plan_task_count = len(planned_tasks)
    if (start_id is None) == (test_ids is None):
        raise ValueError("provide exactly one of --start-id or --test-ids")
    if test_ids is not None:
        test_ids = list(test_ids)
        if len(test_ids) != plan_task_count:
            raise ValueError(
                "--test-ids must contain exactly one ID for every task in the plan"
            )
        if any(not isinstance(test_id, int) or isinstance(test_id, bool) for test_id in test_ids):
            raise ValueError("--test-ids must contain integers")
        if any(test_id <= 0 for test_id in test_ids):
            raise ValueError("--test-ids must be positive")
        if len(test_ids) != len(set(test_ids)):
            raise ValueError("--test-ids must be unique")
    else:
        assert start_id is not None
        if start_id <= 0:
            raise ValueError("start_id must be positive")
    if limit is not None and only is not None:
        raise ValueError("--only and --limit are mutually exclusive")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")
    if only is not None:
        only = list(only)
        if not only:
            raise ValueError("--only must contain at least one plan position")
        if any(not isinstance(position, int) or isinstance(position, bool) for position in only):
            raise ValueError("--only positions must be integers")
        if len(only) != len(set(only)):
            raise ValueError("--only positions must be unique")
        if any(position <= 0 for position in only):
            raise ValueError("--only positions must be positive")
        if any(position > len(planned_tasks) for position in only):
            raise ValueError(
                f"--only positions must be in range 1-{len(planned_tasks)}"
            )
        selected_plan_positions = list(only)
        planned_tasks = [planned_tasks[position - 1] for position in only]
    else:
        selected_plan_positions = list(range(1, min(limit or len(planned_tasks), len(planned_tasks)) + 1))
        planned_tasks = planned_tasks[:limit]
    selected_test_ids = (
        [test_ids[position - 1] for position in selected_plan_positions]
        if test_ids is not None
        else [start_id + position - 1 for position in selected_plan_positions]
    )

    if out.exists() and any(out.iterdir()) and not (out / "run_inputs.json").is_file():
        raise ValueError("output directory is nonempty and is not an authoring run")
    out.mkdir(parents=True, exist_ok=True)
    identity = _run_identity(
        config_path=config_path,
        plan_path=plan_path,
        config=config,
        context=context,
        planned_tasks=planned_tasks,
        limit=limit,
        start_id=start_id,
        test_ids=test_ids,
        selected_test_ids=selected_test_ids,
        selected_plan_positions=selected_plan_positions,
    )
    identity_path = out / "run_inputs.json"
    if identity_path.is_file() and json.loads(identity_path.read_text()) != identity:
        raise ValueError("output directory belongs to different authoring inputs")
    dump_json(identity_path, identity)
    system = COMPLETE_TEST_WRITER_SYSTEM if all(isinstance(task, ApprovedIdea) for task in planned_tasks) else AUTHOR_TEST_SYSTEM
    (out / "authoring_system.txt").write_text(system + "\n")
    (out / "reauthoring_system.txt").write_text(REAUTHOR_TEST_SYSTEM + "\n")

    results: list[dict[str, Any]] = []
    accepted_dir = out / "candidates"

    def save_result(result: dict[str, Any]) -> None:
        proposal_dir = out / "proposals" / f"{result['id']:03d}"
        attempt = result.get("attempt_name") or ("correction" if result.get("model_corrections_used") else "initial")
        response_path = proposal_dir / f"{attempt}_authoring_response.json"
        if response_path.is_file():
            result["authoring_response"] = str(response_path)
        files = authoring_artifacts(proposal_dir, attempt)
        files.extend(Path(result[key]) for key in ("candidate", "gate") if result.get(key))
        saved = save_progress(
            path=proposal_dir / "status.json", result=result,
            inputs_path=identity_path, artifact_paths=files,
        )
        results.append(saved)
        dump_json(out / "manifest.json", {
            **identity, "status": "running", "results": results,
            "one_correction_limit": True,
        })

    dump_json(out / "manifest.json", {**identity, "status": "running", "results": []})
    with _call_ledger(out / "call_ledger.jsonl"):
        for test_id, planned_task in zip(selected_test_ids, planned_tasks, strict=True):
            directory = out / "proposals" / f"{test_id:03d}"
            payload = design_payload(
                config=config, context=context, planned_task=planned_task
            )
            result = _create_one_test(
                directory=directory, accepted_dir=accepted_dir, payload=payload,
                planned_task=planned_task, context=context, config=config,
                test_id=test_id, client=design_client, certifier=certifier,
            )
            save_result(result)

    summary: dict[str, int] = {}
    for result in results:
        status = str(result["status"])
        summary[status] = summary.get(status, 0) + 1
    manifest = {
        **identity,
        "status": "pending" if any(row["status"].endswith("_pending") for row in results) else "completed",
        "gate": "2/2 with source sessions and 0/2 without memory",
        "one_correction_limit": True,
        "summary": summary,
        "results": results,
    }
    dump_json(out / "manifest.json", manifest)
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--limit", type=int)
    selection.add_argument("--only", type=parse_only_positions)
    ids = parser.add_mutually_exclusive_group(required=True)
    ids.add_argument("--start-id", type=int)
    ids.add_argument("--test-ids", type=parse_test_ids)
    parser.add_argument("--confirm-paid-calls", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.confirm_paid_calls:
        raise ValueError("refusing model and certification calls without --confirm-paid-calls")
    if importlib.util.find_spec("mcp") is None and not os.environ.get(
        "DOLPHINBENCH_GATE_PYTHON"
    ):
        raise RuntimeError(
            "authoring certification requires the Hermes MCP runtime; run with "
            "that Python or set DOLPHINBENCH_GATE_PYTHON to it"
        )
    config = load_config(args.config)
    manifest = run_authoring(
        config_path=args.config,
        plan_path=args.plan,
        out=args.out,
        limit=args.limit,
        only=args.only,
        start_id=args.start_id,
        test_ids=args.test_ids,
        design_client=AzureJsonClient(model=config.designer_model, reasoning_effort="high"),
        query_client=AzureJsonClient(model=config.query_model, reasoning_effort="high"),
        confirm_paid_calls=True,
    )
    print(json.dumps({"out": str(args.out), "summary": manifest["summary"]}, indent=2))


if __name__ == "__main__":
    main()
