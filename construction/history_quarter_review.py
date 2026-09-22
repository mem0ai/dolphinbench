"""Review one completed quarter before it becomes an accepted checkpoint."""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from construction.checkpoints import dump_json, load_checkpoint, load_json
from construction.llm import AzureJsonClient
from construction.runtime_inputs import exact_persona_tools, facts_with_current
from construction.runtime_model_calls import cached_client_complete


REVIEW_MODEL = "gpt-5.6-sol"
REVIEW_REASONING_EFFORT = "high"

REVIEW_SYSTEM = """\
Review the completed quarter described in the input.

Return exactly four fields:
- passed: true or false
- findings: a list of objects, each with session_ids and problem
- week_results: one object for every generated week section, in the same order.
  Each object has week_id and passed.
- cross_quarter_findings: a list of findings that require comparing more than
  one generated week.

Report only concrete problems in the generated quarter. A problem must name at least
one generated session ID and explain the problem in plain English.

Check for these problems:
- an accepted quarter-plan event does not occur, occurs on the wrong date, or
  fails to establish its stated lasting result;
- a generated message contradicts an earlier or current fact;
- an exact name, number, date, or other important term changes without an explanation;
- the same small story or wording pattern is repeated across different weeks;
- a person, project, or product is used inconsistently;
- a message is unclear, synthetic, or does not make sense to a reader;
- a message says one thing but the recorded app operation does another.

Before answering, complete these four checks in order:
1. Compare every accepted quarter-plan event with the generated sessions and facts.
2. Read every generated session in date order and compare it with the facts that
   were already true at that time.
3. Compare sessions across weeks that mention the same person, project, routine,
   app record, measurement, or decision.
4. Compare every requested app action with its recorded operation and relevant
   starting app record.
Report every concrete problem found during any of these checks.

Values explicitly presented as disputed, duplicated, imported, or possibly wrong
records are not automatically contradictory facts. Report a contradiction only when
the generated history treats incompatible values as simultaneously authoritative.

Do not report ordinary variation, repeated legitimate people or projects, a matter that
is still unfinished, or a message merely because it contains an app operation.
Use a listed simulated tool's description, arguments, required arguments, and documented
state effect to judge a recorded operation. Do not decide what a tool does from its name.

`facts_established_by_this_session` contains only facts that became established in that
session. A fact in the final-facts section may cite earlier sessions as supporting evidence;
that does not mean the complete fact was already true in those earlier sessions.

The input is not the complete earlier history. You receive the current facts, a short
tail of accepted messages before the quarter, and the starting app records listed in
the starting-app-records section. Do not say that a detail was invented merely because
an older source message is absent. Report unsupported content only when the supplied
evidence contradicts it or the generated quarter itself shows that the content lacks
its required source.

Read each generated week section separately. `week_results` must contain each exact
week_id from the input once, with no omissions or duplicates, and in the input order.
Set its passed value to false when that week's messages or operations have a concrete
problem. Set it to true when you found no such problem in that week. Keep this result
compact; it does not need session IDs or an explanation.
If any finding names a session from a week, set that week's passed value to false.
This also applies when one finding names sessions from more than one week.

Put every concrete problem in `findings`. This remains the complete list of problems for
the quarter. Put a copy of each finding that requires comparing more than one generated
week in `cross_quarter_findings`; leave that list empty when there are none. Set passed to
true exactly when findings is empty.

Use the generated session IDs from the input in every finding. Do not include explanations
outside the required fields.
"""


# These are the operations that can change an existing state record. Each tuple is
# (operation argument, state collection, record field). The lookup is exact; titles,
# normalized names, and other fuzzy matches are intentionally not used here.
_STARTING_RECORD_TARGETS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "update_calendar_event": (("event_id", "calendar", "id"),),
    "archive_email": (("email_id", "inbox", "id"),),
    "delete_email": (("email_id", "inbox", "id"),),
    "update_doc": (("doc_id", "docs", "id"),),
    "update_runbook_entry": (("entry_id", "runbooks", "id"),),
    "merge_pr": (("pr_id", "prs", "id"), ("pr_id", "open_prs", "id")),
    "update_crm_row": (("name", "crm", "name"),),
    "update_feature_flag": (("key", "feature_flags", "key"),),
    "update_experiment": (("experiment_id", "experiments", "experiment_id"),),
    "update_growth_brief": (("brief_id", "growth_briefs", "brief_id"),),
    "update_headcount_plan": (("plan_id", "headcount_plans", "plan_id"),),
}


FINDING_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "session_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
        "problem": {"type": "string"},
    },
    "required": ["session_ids", "problem"],
}


REVIEW_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "passed": {"type": "boolean"},
        "findings": {
            "type": "array",
            "items": FINDING_RESPONSE_SCHEMA,
        },
        "week_results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "week_id": {"type": "string"},
                    "passed": {"type": "boolean"},
                },
                "required": ["week_id", "passed"],
            },
        },
        "cross_quarter_findings": {
            "type": "array",
            "items": FINDING_RESPONSE_SCHEMA,
        },
    },
    "required": ["passed", "findings", "week_results", "cross_quarter_findings"],
}


def _date_from_value(value: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError):
        try:
            return date.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None


def _sort_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("narrative_date") or row.get("date") or ""), str(row.get("id") or row.get("session_id") or ""))


def _fact_id(fact: dict[str, Any]) -> str:
    return str(fact.get("id") or "")


def _fact_without_derived_fields(fact: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(fact)
    result.pop("current", None)
    return result


_STARTING_FACT_FIELDS = ("id", "statement", "applies_when", "subjects", "supersedes")
_COMPACT_RESULT_FIELDS = {"ok", "id", "url", "status"}


def _starting_fact(fact: dict[str, Any]) -> dict[str, Any]:
    return {
        field: copy.deepcopy(fact[field])
        for field in _STARTING_FACT_FIELDS
        if field in fact
    }


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (bool, int, float, str))


def _review_operation_result(operation: Any, result: Any) -> Any:
    tool = operation.get("tool") if isinstance(operation, dict) else None
    if isinstance(tool, str) and tool.startswith(("get_", "list_", "search_", "find_")):
        return copy.deepcopy(result)
    if not isinstance(result, dict):
        return {}
    compact: dict[str, Any] = {}
    for key, value in result.items():
        if key in _COMPACT_RESULT_FIELDS and _is_scalar(value):
            compact[key] = copy.deepcopy(value)
        elif key == "event" and isinstance(value, dict) and _is_scalar(value.get("id")):
            compact[key] = {"id": copy.deepcopy(value["id"])}
    return compact


def _load_window_plan_rows(
    window_records: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for record in window_records:
        window_output = Path(str(record.get("output") or ""))
        plan_path = window_output / "plan.json"
        if not plan_path.is_file():
            continue
        document = load_json(plan_path)
        plan = document.get("plan") if isinstance(document, dict) else None
        plan = plan if isinstance(plan, dict) else document
        sessions = plan.get("sessions") if isinstance(plan, dict) else None
        if not isinstance(sessions, list):
            continue
        for row in sessions:
            if isinstance(row, dict) and str(row.get("session_id") or ""):
                rows[str(row["session_id"])] = copy.deepcopy(row)
    return rows


def _generated_sessions(
    starting_state: dict[str, Any],
    final_state: dict[str, Any],
    *,
    window_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    starting_ids = {
        str(row.get("id") or "")
        for row in starting_state.get("history") or []
        if isinstance(row, dict)
    }
    plan_rows = _load_window_plan_rows(window_records)
    generated = [
        row
        for row in final_state.get("history") or []
        if isinstance(row, dict) and str(row.get("id") or "") not in starting_ids
    ]
    return [
        {
            "session_id": str(session.get("id") or ""),
            "narrative_date": session.get("narrative_date"),
            "purpose": copy.deepcopy(
                (plan_rows.get(str(session.get("id") or "")) or {}).get("purpose")
            ),
            "messages": copy.deepcopy(session.get("messages") or []),
            "_new_fact_keys": [
                str(fact.get("key"))
                for fact in (
                    (plan_rows.get(str(session.get("id") or "")) or {}).get("new_facts")
                    or []
                )
                if isinstance(fact, dict) and str(fact.get("key") or "")
            ],
        }
        for session in sorted(generated, key=_sort_key)
    ]


def _operation_rows(
    starting_state: dict[str, Any], final_state: dict[str, Any], generated_ids: set[str]
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {session_id: [] for session_id in generated_ids}
    starting_operation_ids = {
        (str(row.get("session_id") or ""), str(row.get("replay_epoch_ms") or ""))
        for row in starting_state.get("operation_results") or []
        if isinstance(row, dict)
    }
    for row in final_state.get("operation_results") or []:
        if not isinstance(row, dict):
            continue
        session_id = str(row.get("session_id") or "")
        if session_id not in generated_ids:
            continue
        identity = (session_id, str(row.get("replay_epoch_ms") or ""))
        if identity in starting_operation_ids:
            continue
        result[session_id].append(
            {
                "operation": copy.deepcopy(row.get("operation")),
                "result": _review_operation_result(
                    row.get("operation"), row.get("result")
                ),
            }
        )
    return result


def _starting_app_records(
    starting_state: dict[str, Any],
    operations_by_session: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Collect exact starting records targeted by generated update operations."""
    app_state = starting_state.get("app_state") or {}
    found: dict[tuple[str, str], dict[str, Any]] = {}

    for session_id in sorted(operations_by_session):
        for row in operations_by_session[session_id]:
            operation = row.get("operation")
            if not isinstance(operation, dict):
                continue
            tool = str(operation.get("tool") or "")
            args = operation.get("args")
            if not isinstance(args, dict):
                continue
            for argument, collection, record_field in _STARTING_RECORD_TARGETS.get(tool, ()):
                target = args.get(argument)
                if target is None or target == "":
                    continue
                records = app_state.get(collection)
                if isinstance(records, dict):
                    record_rows = records.items()
                elif isinstance(records, list):
                    record_rows = ((None, record) for record in records)
                else:
                    continue
                for mapping_key, record in record_rows:
                    if not isinstance(record, dict):
                        continue
                    candidate = (
                        mapping_key
                        if collection == "headcount_plans"
                        else record.get(record_field)
                    )
                    if candidate != target:
                        continue
                    key = (collection, str(target))
                    entry = found.setdefault(
                        key,
                        {
                            "state_collection": collection,
                            "record_id_argument": argument,
                            "record_id": target,
                            "record": copy.deepcopy(record),
                        },
                    )
                    break

    return sorted(
        found.values(),
        key=lambda row: (
            str(row.get("state_collection") or ""),
            str(row.get("record_id") or ""),
        ),
    )


def _used_tool_contracts(
    *, persona: str, operations_by_session: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Return canonical definitions for, and only for, tools actually used."""
    used_tools = {
        str(operation.get("tool") or "")
        for operations in operations_by_session.values()
        for row in operations
        if isinstance(row, dict)
        for operation in [row.get("operation")]
        if isinstance(operation, dict) and str(operation.get("tool") or "")
    }
    definitions = exact_persona_tools(persona)
    unknown_tools = sorted(used_tools - set(definitions))
    if unknown_tools:
        raise ValueError(
            "generated app operations use tools without canonical runtime definitions: "
            + ", ".join(unknown_tools)
        )
    return [
        {"tool": tool_name, **copy.deepcopy(schema)}
        for tool_name, schema in definitions.items()
        if tool_name in used_tools
    ]


def _generated_week_sections(
    generated: list[dict[str, Any]], window_records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Group generated sessions under the exact seven-day windows the runner used."""
    sections: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for record in window_records:
        start = _date_from_value(record.get("start_date") or record.get("period_start"))
        end = _date_from_value(record.get("end_date") or record.get("period_end"))
        if start is None or end is None or end < start:
            raise ValueError("quarter review window record has an invalid date range")
        week_id = f"{start.isoformat()}_to_{end.isoformat()}"
        if week_id in seen_ids:
            raise ValueError(f"quarter review window record is duplicated: {week_id}")
        seen_ids.add(week_id)
        sections.append(
            {
                "week_id": week_id,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "sessions": [],
            }
        )
    sections.sort(key=lambda section: (section["start_date"], section["end_date"]))

    for session in generated:
        session_date = _date_from_value(session.get("narrative_date"))
        matching_sections = [
            section
            for section in sections
            if date.fromisoformat(section["start_date"])
            <= session_date
            <= date.fromisoformat(section["end_date"])
        ] if session_date is not None else []
        if len(matching_sections) != 1:
            raise ValueError(
                "generated session does not belong to exactly one quarter review week: "
                + str(session.get("session_id") or "")
            )
        matching_sections[0]["sessions"].append(copy.deepcopy(session))
    return sections


def _facts_by_construction_key(facts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        key = str(fact.get("construction_key") or "")
        if key:
            result[key] = {
                "id": fact.get("id"),
                "construction_key": key,
                "statement": fact.get("statement"),
            }
    return result


def _accepted_tail(state: dict[str, Any], quarter_start: date) -> list[dict[str, Any]]:
    first_day = quarter_start - timedelta(days=14)
    rows = []
    for session in state.get("history") or []:
        if not isinstance(session, dict):
            continue
        session_date = _date_from_value(session.get("narrative_date"))
        if session_date is not None and first_day <= session_date < quarter_start:
            rows.append(
                {
                    "session_id": str(session.get("id") or ""),
                    "narrative_date": session.get("narrative_date"),
                    "messages": copy.deepcopy(session.get("messages") or []),
                }
            )
    return sorted(rows, key=_sort_key)


def build_review_request(
    *,
    config: dict[str, Any],
    quarter_plan: dict[str, Any],
    starting_checkpoint: Path,
    final_checkpoint: Path,
    window_records: list[dict[str, Any]],
) -> dict[str, Any]:
    _, starting_state = load_checkpoint(starting_checkpoint)
    _, final_state = load_checkpoint(final_checkpoint)
    for state, checkpoint in (
        (starting_state, starting_checkpoint),
        (final_state, final_checkpoint),
    ):
        operation_results_path = checkpoint / "operation_results.json"
        operation_results = (
            load_json(operation_results_path) if operation_results_path.is_file() else []
        )
        if not isinstance(operation_results, list):
            raise ValueError(f"checkpoint operation_results.json must contain a list: {checkpoint}")
        state["operation_results"] = operation_results
    quarter_start = date.fromisoformat(str(config["quarter_start"]))
    starting_facts = [
        _starting_fact(fact)
        for fact in facts_with_current(copy.deepcopy(starting_state.get("facts") or []))
        if isinstance(fact, dict) and fact.get("current")
    ]
    starting_facts.sort(key=lambda row: str(row.get("id") or ""))
    generated = _generated_sessions(
        starting_state, final_state, window_records=window_records
    )
    generated_ids = {str(row["session_id"]) for row in generated}
    facts_by_key = _facts_by_construction_key(final_state.get("facts") or [])
    operations_by_session = _operation_rows(starting_state, final_state, generated_ids)
    starting_app_records = _starting_app_records(starting_state, operations_by_session)
    for row in generated:
        new_fact_keys = row.pop("_new_fact_keys")
        row["facts_established_by_this_session"] = [
            copy.deepcopy(facts_by_key[key])
            for key in new_fact_keys
            if key in facts_by_key
        ]
        row["app_operations"] = operations_by_session.get(row["session_id"], [])

    starting_by_id = {
        _fact_id(fact): _fact_without_derived_fields(fact)
        for fact in starting_state.get("facts") or []
        if isinstance(fact, dict) and _fact_id(fact)
    }
    final_current_facts = [
        fact
        for fact in facts_with_current(copy.deepcopy(final_state.get("facts") or []))
        if isinstance(fact, dict) and fact.get("current")
    ]
    changed_facts = [
        _fact_without_derived_fields(fact)
        for fact in final_current_facts
        if _fact_id(fact) not in starting_by_id
        or _fact_without_derived_fields(fact) != starting_by_id[_fact_id(fact)]
    ]
    changed_facts.sort(key=lambda row: str(row.get("id") or ""))
    generated_weeks = _generated_week_sections(generated, window_records)

    return {
        "persona": str(config["persona"]),
        "quarter_id": str(config["quarter_id"]),
        "quarter_start": config["quarter_start"],
        "quarter_end": config["quarter_end"],
        "accepted_quarter_plan": copy.deepcopy(quarter_plan),
        "starting_current_facts": starting_facts,
        "accepted_messages_before_quarter": _accepted_tail(starting_state, quarter_start),
        "starting_app_records": starting_app_records,
        "used_tool_contracts": _used_tool_contracts(
            persona=str(config["persona"]), operations_by_session=operations_by_session
        ),
        "generated_week_sections": generated_weeks,
        "final_current_facts_added_or_changed": changed_facts,
    }


def _render_value(value: Any, *, indent: int = 0) -> list[str]:
    prefix = " " * indent
    if isinstance(value, dict):
        if not value:
            return [f"{prefix}{{}}"]
        lines: list[str] = []
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.extend(_render_value(child, indent=indent + 2))
            else:
                lines.append(f"{prefix}{key}: {child}")
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{prefix}[]"]
        lines = []
        for child in value:
            if isinstance(child, (dict, list)):
                lines.append(f"{prefix}-")
                lines.extend(_render_value(child, indent=indent + 2))
            else:
                lines.append(f"{prefix}- {child}")
        return lines
    return [f"{prefix}{value}"]


def render_review_input(request: dict[str, Any]) -> str:
    sections = [
        ("QUARTER", {key: request[key] for key in ("persona", "quarter_id", "quarter_start", "quarter_end")}),
        ("ACCEPTED QUARTER PLAN", request["accepted_quarter_plan"]),
        ("CURRENT FACTS AT THE START", request["starting_current_facts"]),
        ("LAST 14 DAYS OF ACCEPTED MESSAGES BEFORE THE QUARTER", request["accepted_messages_before_quarter"]),
        ("SIMULATED TOOL CONTRACTS USED BY GENERATED APP OPERATIONS", request["used_tool_contracts"]),
        ("STARTING APP RECORDS DIRECTLY MODIFIED BY GENERATED OPERATIONS", request["starting_app_records"]),
    ]
    lines = ["Read the sections below as one chronological record.", ""]
    for title, value in sections:
        lines.append(title)
        lines.append("=" * len(title))
        lines.extend(_render_value(value))
        lines.append("")
    for week in request["generated_week_sections"]:
        title = (
            f"GENERATED WEEK {week['week_id']} "
            f"({week['start_date']} through {week['end_date']})"
        )
        lines.append(title)
        lines.append("=" * len(title))
        lines.extend(_render_value(week))
        lines.append("")
    title = "FINAL CURRENT FACTS ADDED OR CHANGED DURING THE QUARTER"
    lines.append(title)
    lines.append("=" * len(title))
    lines.extend(_render_value(request["final_current_facts_added_or_changed"]))
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def validate_review_response(
    response: Any,
    *,
    generated_session_ids: set[str],
    generated_session_ids_by_week: dict[str, set[str]],
    expected_week_ids: list[str],
) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(response, dict):
        errors.append("response must be an object")
        return {"valid": False, "passed": False, "errors": errors, "finding_count": 0}
    unexpected = sorted(key for key in response if key not in {
        "passed", "findings", "week_results", "cross_quarter_findings", "_usage", "_response_id"
    })
    if unexpected:
        errors.append("response contains unexpected fields: " + ", ".join(unexpected))
    if not isinstance(response.get("passed"), bool):
        errors.append("passed must be a boolean")
    findings = response.get("findings")
    if not isinstance(findings, list):
        errors.append("findings must be a list")
        findings = []

    def validate_findings(rows: Any, *, label_prefix: str) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            errors.append(f"{label_prefix} must be a list")
            return []
        valid_rows: list[dict[str, Any]] = []
        for index, finding in enumerate(rows):
            label = f"{label_prefix}[{index}]"
            if not isinstance(finding, dict):
                errors.append(f"{label} must be an object")
                continue
            row_errors_before = len(errors)
            if set(finding) != {"session_ids", "problem"}:
                errors.append(f"{label} must contain only session_ids and problem")
            session_ids = finding.get("session_ids")
            if (
                not isinstance(session_ids, list)
                or not session_ids
                or not all(isinstance(session_id, str) and session_id.strip() for session_id in session_ids)
            ):
                errors.append(f"{label}.session_ids must be a non-empty list of strings")
            elif not any(session_id in generated_session_ids for session_id in session_ids):
                errors.append(f"{label}.session_ids must name a generated session")
            problem = finding.get("problem")
            if not isinstance(problem, str) or not problem.strip():
                errors.append(f"{label}.problem must be a non-empty string")
            if len(errors) == row_errors_before:
                valid_rows.append(finding)
        return valid_rows

    valid_findings = validate_findings(findings, label_prefix="findings")
    week_results = response.get("week_results")
    if not isinstance(week_results, list):
        errors.append("week_results must be a list")
        week_results = []
    actual_week_ids: list[str] = []
    for index, result in enumerate(week_results):
        label = f"week_results[{index}]"
        if not isinstance(result, dict):
            errors.append(f"{label} must be an object")
            continue
        if set(result) != {"week_id", "passed"}:
            errors.append(f"{label} must contain only week_id and passed")
        week_id = result.get("week_id")
        if not isinstance(week_id, str) or not week_id:
            errors.append(f"{label}.week_id must be a non-empty string")
        else:
            actual_week_ids.append(week_id)
        if not isinstance(result.get("passed"), bool):
            errors.append(f"{label}.passed must be a boolean")
    duplicate_week_ids = sorted(
        {week_id for week_id in actual_week_ids if actual_week_ids.count(week_id) > 1}
    )
    if duplicate_week_ids:
        errors.append("week_results duplicates week sections: " + ", ".join(duplicate_week_ids))
    missing_week_ids = [week_id for week_id in expected_week_ids if week_id not in actual_week_ids]
    if missing_week_ids:
        errors.append("week_results omits week sections: " + ", ".join(missing_week_ids))
    unexpected_week_ids = [week_id for week_id in actual_week_ids if week_id not in expected_week_ids]
    if unexpected_week_ids:
        errors.append("week_results names unknown week sections: " + ", ".join(unexpected_week_ids))
    if (
        not duplicate_week_ids
        and not missing_week_ids
        and not unexpected_week_ids
        and actual_week_ids != expected_week_ids
    ):
        errors.append("week_results must list week sections in the input order")

    finding_session_ids = {
        session_id
        for finding in valid_findings
        for session_id in finding["session_ids"]
    }
    for index, result in enumerate(week_results):
        if not isinstance(result, dict):
            continue
        week_id = result.get("week_id")
        week_passed = result.get("passed")
        if not isinstance(week_id, str) or not isinstance(week_passed, bool):
            continue
        week_session_ids = generated_session_ids_by_week.get(week_id)
        if week_session_ids is None:
            continue
        has_week_finding = bool(week_session_ids & finding_session_ids)
        if week_passed == has_week_finding:
            expected_value = "false" if has_week_finding else "true"
            errors.append(
                f"week_results[{index}].passed must be {expected_value} to match "
                "top-level findings for that week"
            )

    cross_quarter_findings = validate_findings(
        response.get("cross_quarter_findings"), label_prefix="cross_quarter_findings"
    )
    finding_keys = {
        json.dumps(finding, sort_keys=True, separators=(",", ":"))
        for finding in valid_findings
    }
    for finding in cross_quarter_findings:
        if json.dumps(finding, sort_keys=True, separators=(",", ":")) not in finding_keys:
            errors.append("cross_quarter_findings entries must also appear in findings")
            break

    if isinstance(response.get("passed"), bool) and response["passed"] != (len(findings) == 0):
        errors.append("passed must be true exactly when findings is empty")
    return {
        "valid": not errors,
        "passed": bool(response.get("passed")) if not errors else False,
        "errors": errors,
        "finding_count": len(findings),
    }


def run_review(
    *,
    output_dir: Path,
    request: dict[str, Any],
    rendered_input: str,
    client: AzureJsonClient | None = None,
) -> dict[str, Any]:
    request_path = output_dir / "history_quarter_review_request.json"
    system_path = output_dir / "history_quarter_review_system.txt"
    input_path = output_dir / "history_quarter_review_input.txt"
    response_path = output_dir / "history_quarter_review_response.json"
    validation_path = output_dir / "history_quarter_review_validation.json"
    cache_path = output_dir / "work" / "history_quarter_review_response_cache.json"
    dump_json(request_path, request)
    system_path.write_text(REVIEW_SYSTEM)
    input_path.write_text(rendered_input)
    generated_ids = {
        str(session.get("session_id") or "")
        for week in request.get("generated_week_sections") or []
        if isinstance(week, dict)
        for session in week.get("sessions") or []
        if isinstance(session, dict)
    }
    expected_week_ids = [
        str(week.get("week_id") or "")
        for week in request.get("generated_week_sections") or []
        if isinstance(week, dict)
    ]
    generated_session_ids_by_week = {
        str(week.get("week_id") or ""): {
            str(session.get("session_id") or "")
            for session in week.get("sessions") or []
            if isinstance(session, dict) and str(session.get("session_id") or "")
        }
        for week in request.get("generated_week_sections") or []
        if isinstance(week, dict) and str(week.get("week_id") or "")
    }
    try:
        response = cached_client_complete(
            cache_path,
            REVIEW_SYSTEM,
            rendered_input,
            client or AzureJsonClient(model=REVIEW_MODEL, reasoning_effort=REVIEW_REASONING_EFFORT),
            response_schema=REVIEW_RESPONSE_SCHEMA,
            response_schema_name="history_quarter_review",
        )
    except Exception as exc:
        validation = {
            "valid": False,
            "passed": False,
            "errors": [f"review call failed: {type(exc).__name__}: {exc}"],
            "finding_count": 0,
        }
        dump_json(validation_path, validation)
        raise
    dump_json(response_path, response)
    validation = validate_review_response(
        response,
        generated_session_ids=generated_ids,
        generated_session_ids_by_week=generated_session_ids_by_week,
        expected_week_ids=expected_week_ids,
    )
    dump_json(validation_path, validation)
    return {
        "status": "passed" if validation["valid"] and validation["passed"] else "failed",
        "validation": validation,
        "request": str(request_path),
        "system": str(system_path),
        "input": str(input_path),
        "response": str(response_path),
        "validation_file": str(validation_path),
        "cache": str(cache_path),
    }
