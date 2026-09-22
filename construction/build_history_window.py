"""Plan, enrich, write, and materialize one bounded history window."""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from construction.checkpoints import (
    CompletedOutputError,
    data_hash,
    dump_json,
    load_json,
    load_checkpoint,
    save_checkpoint,
    set_run_provenance_environment,
    validate_resume_checkpoint_for_config,
    validated_checkpoint_identity,
)
from construction.construction_io import file_hash, resolve_path
from construction.runtime_inputs import (
    exact_persona_tools,
    facts_with_current,
    readable_app_state,
)
from construction.runtime_model_calls import cached_client_complete
from construction.runtime_operations import (
    validate_operation_result_references,
    validate_tool_python,
)
from construction.llm import AzureJsonClient
from construction.long_history_prompts import (
    HISTORY_WINDOW_CONTACT_CORRECTION_SYSTEM,
    HISTORY_WINDOW_PLANNING_SYSTEM,
    HISTORY_WINDOW_STATE_CORRECTION_SYSTEM,
    HISTORY_WINDOW_STORY_SYSTEM,
    HISTORY_WINDOW_WRITING_SYSTEM,
)
from construction.model_input_text import render_model_input, render_story_planner_input
from construction.quarter_state import (
    current_existing_fact_ids,
    fact_reference,
    materialize_entities,
    materialize_facts,
    nonempty_string,
    project_app_operations,
    require_exact_fields,
    string_list,
    visible_fields,
)


PLANNER_MODEL = "gpt-5.6-sol"
WRITER_MODEL = "gpt-5.6-sol"
PLANNER_REASONING_EFFORT = "high"
WRITER_REASONING_EFFORT = "medium"
ORDINARY_CURRENT_LIFE_BASIS_ID = "ordinary_current_life"
WINDOW_EXCLUDED_TOOL_NAMES = frozenset({"get_service_metrics", "query_logs"})
# Retained as an import-compatible alias for legacy fixtures. The canonical
# weekly planner no longer receives quarter-authored active situations.
ORDINARY_CURRENT_LIFE_SITUATION_ID = ORDINARY_CURRENT_LIFE_BASIS_ID

MODEL_ENTITY_IDENTITY_FIELDS = ("id", "name", "kind", "aliases", "reason")
PLANNER_APP_RECORD_FIELDS = (
    "id",
    "plan_id",
    "asof_date",
    "cohort_id",
    "tier",
    "customer_id",
    "experiment_id",
    "funnel_name",
    "team",
    "week_of",
    "primary",
    "secondary",
    "members",
    "name",
    "title",
    "subject",
    "type",
    "status",
    "folder",
    "date",
    "start",
    "end",
    "created_at",
    "updated_at",
    "added_at",
    "applied_at",
    "deck",
    "slide",
    "owner",
    "attendees",
    "tags",
    "rationale_tags",
    "revisit_after",
)
PLANNER_PLAN_FIELDS = {"period_start", "period_end", "occurrences"}
OCCURRENCE_FIELDS = {
    "contact_id",
    "happening_id",
    "narrative_date",
    "basis_ids",
    "effect_kind",
    "thread_id",
    "continues_from",
    "uses_items_created_by_contact_ids",
    "subject_ids",
    "development_ids",
    "anchor_item_ids",
    "what_happened",
    "assistant_outcome",
    "source_material",
    "uses_fact_ids",
    "durable_facts",
    "current_state_changes",
    "app_operations",
    "thread_after",
}
SOURCE_MATERIAL_FIELDS = {"kind", "origin", "factual_contents"}
ASSISTANT_OUTCOME_FIELDS = {"kind", "requested_result"}
THREAD_AFTER_FIELDS = {"is_open", "what_remains"}
WRITER_PLAN_FIELDS = {"period_start", "period_end", "sessions"}
WRITER_SESSION_FIELDS = {
    "contact_id",
    "messages_before_request",
    "message_with_request",
    "conflicting_fact_ids",
}
FINAL_PLAN_FIELDS = {"period_start", "period_end", "new_entities", "sessions"}
ENTITY_FIELDS = {
    "id",
    "name",
    "kind",
    "role",
    "reason",
    "introduced_in_contact_id",
}
SESSION_FIELDS = {
    "contact_id",
    "narrative_date",
    "thread_id",
    "continues_from",
    "subject_ids",
    "development_ids",
    "anchor_item_ids",
    "purpose",
    "messages",
    "uses_facts",
    "new_facts",
    "app_operations",
    "thread_open_after_contact",
    "what_remains_after_contact",
}
FACT_FIELDS = {
    "key",
    "statement",
    "applies_when",
    "subjects",
    "supersedes",
    "evidence_contact_ids",
}
OPERATION_FIELDS = {"tool", "args"}
PLANNED_FACT_FIELDS = {"key", "statement", "applies_when", "subjects", "supersedes"}
CURRENT_STATE_CHANGE_FIELDS = {"key", "subject_ids", "statement"}
CURRENT_STATE_CHANGE_KEY_RE = re.compile(r"[a-z][a-z0-9_]*")
COMMUNICATION_DESTINATION_SPECS = {
    "send_email": (("to", "to"), ("cc", "cc")),
    "send_slack_message": (("channel", "channel"),),
    "send_slack_dm": (("user", "user"),),
}
APP_ITEM_RECORD_ID_PATHS_BY_TOOL = {
    "create_calendar_event": (("result", "id"),),
    "update_calendar_event": (("args", "event_id"), ("result", "event", "id")),
    "create_runbook_entry": (("result", "id"),),
    "update_runbook_entry": (("args", "entry_id"), ("result", "entry_id")),
    "update_deck_slide": (("result", "id"),),
    "create_doc": (("result", "id"),),
    "update_doc": (("args", "doc_id"), ("result", "id")),
    "update_crm_row": (("result", "id"),),
    "update_headcount_plan": (("args", "plan_id"), ("result", "plan_id")),
    "update_feature_flag": (("result", "id"),),
    "create_experiment": (
        ("args", "experiment_id"),
        ("result", "experiment_id"),
    ),
    "update_experiment": (
        ("args", "experiment_id"),
        ("result", "experiment_id"),
    ),
    "create_growth_brief": (("args", "brief_id"), ("result", "brief_id")),
    "update_growth_brief": (("args", "brief_id"), ("result", "brief_id")),
}
APP_ITEM_RESULT_ID_PATHS = (
    ("id",),
    ("event", "id"),
    ("entry_id",),
    ("plan_id",),
    ("experiment_id",),
    ("brief_id",),
)
APP_ITEM_ID_FIELDS = {
    "id",
    "event_id",
    "entry_id",
    "plan_id",
    "experiment_id",
    "brief_id",
}
EXISTING_APP_ITEM_ID_ARGUMENT_BY_TOOL = {
    "update_calendar_event": "event_id",
    "update_doc": "doc_id",
    "update_experiment": "experiment_id",
    "update_growth_brief": "brief_id",
    "update_runbook_entry": "entry_id",
}


def normalize_current_state_change_key(key: Any) -> str:
    """Convert an opaque planner key to a stable lowercase identifier."""
    if not isinstance(key, str):
        raise ValueError("current state change key must be a string")
    normalized = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
    if not CURRENT_STATE_CHANGE_KEY_RE.fullmatch(normalized):
        raise ValueError(
            f"current state change key {key!r} normalizes to an invalid key"
        )
    return normalized


def accepted_communication_destinations(
    state: dict[str, Any],
) -> list[dict[str, str]]:
    """Return deduplicated exact destinations from accepted communication ops."""
    purpose_by_session_id: dict[str, tuple[tuple[int, float, int], str]] = {}
    for purpose_index, purpose in enumerate(state.get("session_purposes") or []):
        if not isinstance(purpose, dict) or not nonempty_string(
            purpose.get("session_id")
        ):
            continue
        session_id = str(purpose["session_id"])
        try:
            when = datetime.fromisoformat(str(purpose.get("narrative_date") or ""))
            if when.tzinfo is not None:
                when = when.astimezone(timezone.utc)
            purpose_rank = (1, when.timestamp(), purpose_index)
        except ValueError:
            purpose_rank = (0, 0.0, purpose_index)
        purpose_text = " ".join(str(purpose.get("purpose") or "").split())
        if len(purpose_text) > 240:
            purpose_text = purpose_text[:237].rstrip() + "..."
        previous = purpose_by_session_id.get(session_id)
        if previous is None or purpose_rank >= previous[0]:
            purpose_by_session_id[session_id] = (purpose_rank, purpose_text)

    destinations_by_identity: dict[
        tuple[str, str, str], tuple[tuple[int, float, int], dict[str, str]]
    ] = {}
    identity_order: list[tuple[str, str, str]] = []
    for operation_index, row in enumerate(state.get("operation_results") or []):
        if not isinstance(row, dict):
            continue
        operation = row.get("operation")
        if not isinstance(operation, dict):
            continue
        tool = operation.get("tool")
        args = operation.get("args")
        if not isinstance(tool, str) or not isinstance(args, dict):
            continue
        for field, destination_type in COMMUNICATION_DESTINATION_SPECS.get(tool, ()):
            raw_destinations = args.get(field)
            values = (
                raw_destinations
                if isinstance(raw_destinations, list)
                else [raw_destinations]
            )
            for value in values:
                if not isinstance(value, str) or not value.strip():
                    continue
                destination = value
                identity = (tool, destination_type, destination)
                purpose_rank, purpose_text = purpose_by_session_id.get(
                    str(row.get("session_id") or ""),
                    ((0, 0.0, operation_index), ""),
                )
                candidate = {
                    "transport": "email" if tool == "send_email" else "slack",
                    "destination_type": destination_type,
                    "destination": destination,
                    "prior_contact_purpose": purpose_text,
                }
                previous = destinations_by_identity.get(identity)
                if previous is None:
                    identity_order.append(identity)
                if previous is None or purpose_rank >= previous[0]:
                    destinations_by_identity[identity] = (purpose_rank, candidate)
    return [destinations_by_identity[identity][1] for identity in identity_order]


def window_tools(tools: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Return tools allowed in planner, writer, validation, and replay inputs."""
    return {
        str(tool_name): copy.deepcopy(schema)
        for tool_name, schema in (tools or {}).items()
        if str(tool_name) not in WINDOW_EXCLUDED_TOOL_NAMES
    }


def _operation_reference_context(
    contacts: list[Any],
) -> tuple[dict[str, int], dict[str, int]]:
    positions: dict[str, int] = {}
    counts: dict[str, int] = {}
    for position, contact in enumerate(contacts):
        if not isinstance(contact, dict):
            continue
        contact_id = str(contact.get("contact_id") or "")
        positions.setdefault(contact_id, position)
        operations = contact.get("app_operations")
        counts.setdefault(
            contact_id, len(operations) if isinstance(operations, list) else 0
        )
    return positions, counts


def _direct_operation_result_contact_ids(args: dict[str, Any]) -> list[str]:
    """Return structurally valid direct operation-result reference contact IDs."""
    contact_ids: list[str] = []
    for value in args.values():
        if not isinstance(value, dict) or set(value) != {"$operation_result"}:
            continue
        reference = value["$operation_result"]
        if (
            not isinstance(reference, dict)
            or set(reference) != {"contact_id", "operation_index", "field"}
            or not isinstance(reference.get("contact_id"), str)
            or not reference["contact_id"].strip()
            or isinstance(reference.get("operation_index"), bool)
            or not isinstance(reference.get("operation_index"), int)
            or reference["operation_index"] < 0
            or not isinstance(reference.get("field"), str)
            or not reference["field"].strip()
        ):
            continue
        contact_id = reference["contact_id"]
        if contact_id not in contact_ids:
            contact_ids.append(contact_id)
    return contact_ids


def _direct_operation_result_references(
    operations: list[dict[str, Any]],
) -> list[tuple[str, int, str]]:
    """Return the earlier operation values explicitly used by a contact."""
    references: list[tuple[str, int, str]] = []
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        args = operation.get("args")
        if not isinstance(args, dict):
            continue
        for value in args.values():
            if not isinstance(value, dict) or set(value) != {"$operation_result"}:
                continue
            reference = value["$operation_result"]
            if (
                not isinstance(reference, dict)
                or set(reference) != {"contact_id", "operation_index", "field"}
                or not isinstance(reference.get("contact_id"), str)
                or not isinstance(reference.get("operation_index"), int)
                or isinstance(reference.get("operation_index"), bool)
                or not isinstance(reference.get("field"), str)
            ):
                continue
            item = (
                reference["contact_id"],
                reference["operation_index"],
                reference["field"],
            )
            if item not in references:
                references.append(item)
    return references


PERSISTENT_ENTITY_KINDS = {
    "person",
    "organization",
    "project",
    "product",
    "place",
    "team",
    "pet",
}

_UNRESOLVED_CHOICE_VALUES = frozenset({"tbd", "unknown", "unspecified"})
_UNRESOLVED_CHOICE_PATTERN = re.compile(
    r"(?:the )?(?:selected|usual|preferred|current) "
    r"(?:restaurant|venue|place|item|items|dinner items|order|option|choice)",
    re.IGNORECASE,
)


def has_unresolved_choice_value(value: Any) -> bool:
    """Return whether a structured user choice is still a stand-in, not a value."""
    if isinstance(value, list):
        return any(has_unresolved_choice_value(item) for item in value)
    if not isinstance(value, str):
        return False
    normalized = " ".join(value.split()).casefold()
    return normalized in _UNRESOLVED_CHOICE_VALUES or bool(
        _UNRESOLVED_CHOICE_PATTERN.fullmatch(normalized)
    )


def validate_concrete_choice_arguments(
    *, operation_label: str, args: dict[str, Any], schema: dict[str, Any], errors: list[str]
) -> None:
    """Reject unresolved stand-ins only in tool arguments declared as user choices."""
    required_values = (schema.get("state_effect") or {}).get(
        "requires_concrete_values", []
    )
    if not isinstance(required_values, list):
        return
    for argument_name in required_values:
        if not isinstance(argument_name, str) or argument_name not in args:
            continue
        if has_unresolved_choice_value(args[argument_name]):
            errors.append(
                f"{operation_label}.args.{argument_name} has an unresolved placeholder value"
            )


def _strict_object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _string_array() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


def _integer_array() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "integer"}}


def _fact_reference_array() -> dict[str, Any]:
    return {
        "type": "array",
        "items": {
            "anyOf": [
                {"type": "integer"},
                {"type": "string"},
            ]
        },
    }


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


SOURCE_MATERIAL_SCHEMA = _strict_object(
    {
        "kind": {"type": "string"},
        "origin": {"type": "string"},
        "factual_contents": {"type": "string"},
    }
)

ASSISTANT_OUTCOME_SCHEMA = _strict_object(
    {
        "kind": {"type": "string"},
        "requested_result": _nullable({"type": "string"}),
    }
)

THREAD_AFTER_SCHEMA = _strict_object(
    {
        "is_open": {"type": "boolean"},
        "what_remains": _nullable({"type": "string"}),
    }
)

PLANNED_FACT_SCHEMA = _strict_object(
    {
        "key": {"type": "string"},
        "statement": {"type": "string"},
        "applies_when": {"type": "string"},
        "subjects": _string_array(),
        "supersedes": _fact_reference_array(),
    }
)

CURRENT_STATE_CHANGE_SCHEMA = _strict_object(
    {
        "key": {"type": "string"},
        "subject_ids": _string_array(),
        "statement": {"type": "string"},
    }
)

PLANNED_OPERATION_SCHEMA = _strict_object(
    {
        "tool": {"type": "string"},
        "args_json": {"type": "string"},
    }
)

STORY_OCCURRENCE_SCHEMA = _strict_object(
    {
        "contact_id": {"type": "string"},
        "happening_id": {"type": "string"},
        "narrative_date": {"type": "string"},
        "thread_id": {"type": "string"},
        "continues_from": _string_array(),
        "uses_items_created_by_contact_ids": _string_array(),
        "subject_ids": _string_array(),
        "development_ids": _string_array(),
        "what_happened": {"type": "string"},
        "assistant_outcome": ASSISTANT_OUTCOME_SCHEMA,
        "source_material": _nullable(SOURCE_MATERIAL_SCHEMA),
        "thread_after": THREAD_AFTER_SCHEMA,
    }
)

STORY_RESPONSE_SCHEMA = _strict_object(
    {
        "plan": _strict_object(
            {"occurrences": {"type": "array", "items": STORY_OCCURRENCE_SCHEMA}}
        )
    }
)

ENRICHMENT_CONTACT_SCHEMA = _strict_object(
    {
        "contact_id": {"type": "string"},
        "uses_current_fact_ids": _integer_array(),
        "uses_new_fact_keys": _string_array(),
        "durable_facts": {"type": "array", "items": PLANNED_FACT_SCHEMA},
        "current_state_changes": {
            "type": "array",
            "items": CURRENT_STATE_CHANGE_SCHEMA,
        },
        "app_operations": {"type": "array", "items": PLANNED_OPERATION_SCHEMA},
        "execution_problem": _nullable({"type": "string"}),
        "factual_problem": _nullable({"type": "string"}),
    }
)

ENRICHMENT_RESPONSE_SCHEMA = _strict_object(
    {"contacts": {"type": "array", "items": ENRICHMENT_CONTACT_SCHEMA}}
)

CONTACT_CORRECTION_SCHEMA = _strict_object(
    {
        "contact_id": {"type": "string"},
        "what_happened": {"type": "string"},
        "assistant_outcome": ASSISTANT_OUTCOME_SCHEMA,
        "source_material": _nullable(SOURCE_MATERIAL_SCHEMA),
    }
)


def _allowed_array(values: list[Any], *, value_type: str) -> dict[str, Any]:
    unique = list(dict.fromkeys(values))
    if not unique:
        return {"type": "array", "items": {"type": value_type}, "maxItems": 0}
    return {
        "type": "array",
        "items": {"type": value_type, "enum": unique},
    }


def enrichment_response_schema(payload: dict[str, Any]) -> dict[str, Any]:
    """Constrain model-owned references to identifiers supplied in this call."""
    contact_ids = [
        str(row.get("contact_id") or "")
        for row in payload.get("fixed_contacts") or []
        if isinstance(row, dict)
    ]
    current_fact_ids = [
        int(row["id"])
        for row in payload.get("current_facts") or []
        if isinstance(row, dict) and type(row.get("id")) is int
    ]
    new_fact_keys = [
        str(row["key"])
        for row in payload.get("lasting_facts_that_must_be_established") or []
        if isinstance(row, dict) and nonempty_string(row.get("key"))
    ]
    contact_schema = copy.deepcopy(ENRICHMENT_CONTACT_SCHEMA)
    properties = contact_schema["properties"]
    properties["contact_id"] = {"type": "string", "enum": contact_ids}
    properties["uses_current_fact_ids"] = _allowed_array(
        current_fact_ids, value_type="integer"
    )
    properties["uses_new_fact_keys"] = _allowed_array(
        new_fact_keys, value_type="string"
    )
    return _strict_object(
        {
            "contacts": {
                "type": "array",
                "items": contact_schema,
                "minItems": len(contact_ids),
                "maxItems": len(contact_ids),
            }
        }
    )


def contact_correction_response_schema(contact_ids: list[str]) -> dict[str, Any]:
    """Require one correction for every blocked contact, in supplied order."""
    contact_schema = copy.deepcopy(CONTACT_CORRECTION_SCHEMA)
    contact_schema["properties"]["contact_id"] = {
        "type": "string",
        "enum": list(contact_ids),
    }
    return _strict_object(
        {
            "contacts": {
                "type": "array",
                "items": contact_schema,
                "minItems": len(contact_ids),
                "maxItems": len(contact_ids),
            }
        }
    )


def story_response_schema(payload: dict[str, Any]) -> dict[str, Any]:
    """Constrain story development IDs to changes scheduled for these dates."""
    allowed_development_ids = [
        str(row["id"])
        for row in payload.get("developments_for_this_week") or []
        if isinstance(row, dict) and nonempty_string(row.get("id"))
    ]
    schema = copy.deepcopy(STORY_RESPONSE_SCHEMA)
    occurrence = schema["properties"]["plan"]["properties"]["occurrences"]["items"]
    occurrence["properties"]["development_ids"] = _allowed_array(
        allowed_development_ids, value_type="string"
    )
    return schema

PLANNER_RESPONSE_SCHEMA = _strict_object(
    {
        "plan": _strict_object(
            {
                "occurrences": {
                    "type": "array",
                    "items": _strict_object(
                        {
                            "contact_id": {"type": "string"},
                            "happening_id": {"type": "string"},
                            "narrative_date": {"type": "string"},
                            "basis_ids": _string_array(),
                            "effect_kind": {"type": "string"},
                            "thread_id": {"type": "string"},
                            "continues_from": _string_array(),
                            "uses_items_created_by_contact_ids": _string_array(),
                            "subject_ids": _string_array(),
                            "development_ids": _string_array(),
                            "what_happened": {"type": "string"},
                            "assistant_outcome": ASSISTANT_OUTCOME_SCHEMA,
                            "source_material": _nullable(SOURCE_MATERIAL_SCHEMA),
                            "uses_current_fact_ids": _integer_array(),
                            "uses_new_fact_keys": _string_array(),
                            "durable_facts": {
                                "type": "array",
                                "items": PLANNED_FACT_SCHEMA,
                            },
                            "current_state_changes": {
                                "type": "array",
                                "items": CURRENT_STATE_CHANGE_SCHEMA,
                            },
                            "app_operations": {
                                "type": "array",
                                "items": PLANNED_OPERATION_SCHEMA,
                            },
                            "thread_after": THREAD_AFTER_SCHEMA,
                        }
                    ),
                },
            }
        )
    }
)

WRITER_CONTACT_RESPONSE_SCHEMA = _strict_object(
    {
        "messages_before_request": _string_array(),
        "message_with_request": _nullable({"type": "string"}),
        "conflicting_fact_ids": _integer_array(),
    }
)

WRITER_API_RESPONSE_SCHEMA = _strict_object(
    {
        "plan": _strict_object(
            {
                "sessions": {
                    "type": "array",
                    "items": _strict_object(
                        {
                            "contact_id": {"type": "string"},
                            "messages_before_request": _string_array(),
                            "message_with_request": _nullable({"type": "string"}),
                            "conflicting_fact_ids": _integer_array(),
                        }
                    ),
                },
            }
        )
    }
)


def writer_api_response_schema(contact_ids: list[str]) -> dict[str, Any]:
    """Return a strict keyed response schema for one fixed set of contacts."""
    if not contact_ids or any(not nonempty_string(contact_id) for contact_id in contact_ids):
        raise ValueError("writer schema needs non-empty fixed contact IDs")
    if len(contact_ids) != len(set(contact_ids)):
        raise ValueError("writer schema needs unique fixed contact IDs")
    schema = _strict_object(
        {
            "plan": _strict_object(
                {
                    "sessions": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    }
                }
            )
        }
    )
    sessions = schema["properties"]["plan"]["properties"]["sessions"]
    sessions["properties"] = {
        contact_id: copy.deepcopy(WRITER_CONTACT_RESPONSE_SCHEMA)
        for contact_id in contact_ids
    }
    sessions["required"] = list(contact_ids)
    return schema


def parse_iso_date(value: Any, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date") from exc


def validate_window_dates(
    config: dict[str, Any], checkpoint: dict[str, Any], start: date, end: date
) -> None:
    """Require an adjacent forward window contained by the configured quarter."""
    accepted_through = parse_iso_date(checkpoint.get("accepted_through"), "accepted_through")
    expected_start = accepted_through + timedelta(days=1)
    if start != expected_start:
        raise ValueError(
            "start-date must be exactly one day after checkpoint.accepted_through: "
            f"expected {expected_start.isoformat()}, got {start.isoformat()}"
        )
    quarter_start = parse_iso_date(config.get("quarter_start"), "quarter_start")
    quarter_end = parse_iso_date(config.get("quarter_end"), "quarter_end")
    if end < start:
        raise ValueError("end-date must not be before start-date")
    if start < quarter_start or end > quarter_end:
        raise ValueError(
            f"requested window must stay inside {quarter_start.isoformat()}.."
            f"{quarter_end.isoformat()}"
        )


def prepare_window_output(output_dir: Path) -> None:
    """Preserve resumable call caches while refusing completed output."""
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError(f"output path is not a directory: {output_dir}")
    manifest_path = output_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = load_json(manifest_path)
        if manifest.get("status") == "passed":
            raise CompletedOutputError(f"output is already completed: {output_dir}")
    candidate = output_dir / "candidate_checkpoint"
    if candidate.exists():
        if not candidate.is_dir():
            raise ValueError(f"candidate checkpoint is not a directory: {candidate}")
        try:
            validated_checkpoint_identity(candidate)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"output contains an incomplete candidate checkpoint: {candidate}"
            ) from exc
        raise CompletedOutputError(f"output already contains a candidate checkpoint: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    pending = output_dir / "candidate_checkpoint.pending"
    if pending.exists():
        if not pending.is_dir():
            raise ValueError(f"pending checkpoint path is not a directory: {pending}")
        shutil.rmtree(pending)
    (output_dir / "work").mkdir(exist_ok=True)


def quarter_plan_body(document: dict[str, Any]) -> dict[str, Any]:
    plan = document.get("plan")
    if isinstance(plan, dict):
        return plan
    story = document.get("story")
    return story if isinstance(story, dict) else document


def accepted_quarter_developments(document: dict[str, Any]) -> list[dict[str, Any]]:
    plan = quarter_plan_body(document)
    for key in ("events", "developments", "accepted_developments"):
        rows = plan.get(key)
        if isinstance(rows, list):
            return [copy.deepcopy(row) for row in rows if isinstance(row, dict)]
    raise ValueError("accepted quarter plan does not contain developments")


def accepted_protected_changes(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Load protected changes or derive no-earlier-than boundaries from events."""
    plan = quarter_plan_body(document)
    explicit = plan.get("protected_changes")
    if isinstance(explicit, list):
        return [copy.deepcopy(row) for row in explicit if isinstance(row, dict)]
    changes: list[dict[str, Any]] = []
    for development in accepted_quarter_developments(document):
        development_id = str(development.get("id") or "")
        if not development_id:
            continue
        lasting_outcome = str(development.get("lasting_outcome") or "").strip()
        boundary = (
            f"Before {development_start(development).isoformat()}, do not establish "
            f"or imply this later accepted outcome: {lasting_outcome}"
            if lasting_outcome
            else (
                "Do not establish the lasting change reserved for this accepted "
                f"development before {development_start(development).isoformat()}."
            )
        )
        changes.append(
            {
                "id": f"protected:{development_id}",
                "subject_ids": [
                    str(value)
                    for value in development.get("subjects") or []
                    if nonempty_string(value)
                ],
                "not_before": development_start(development).isoformat(),
                "description": boundary,
            }
        )
    return changes


def development_start(row: dict[str, Any]) -> date:
    value = row.get("start_date") or row.get("date") or row.get("end_date")
    return parse_iso_date(value, f"development {row.get('id')} start_date")


def development_completion(row: dict[str, Any]) -> date:
    # The accepted DolphinBench quarter schema uses end_date. date is supported for a
    # compatible single-day plan, but start_date alone is never completion.
    value = row.get("end_date") if row.get("end_date") is not None else row.get("date")
    return parse_iso_date(value, f"development {row.get('id')} end_date")


def load_replacement_evidence_refs(
    config: dict[str, Any],
) -> tuple[Path, dict[str, list[str]]]:
    """Load accepted replacement evidence without exposing it to model calls."""
    path = resolve_path(config["accepted_replacement_accounting"])
    if not path.is_file():
        raise ValueError(f"accepted replacement accounting is missing: {path}")
    document = load_json(path)
    if not isinstance(document, dict) or set(document) != {"replacement_accounting"}:
        raise ValueError(
            "accepted replacement accounting must contain exactly replacement_accounting"
        )
    rows = document["replacement_accounting"]
    if not isinstance(rows, list):
        raise ValueError("accepted replacement accounting replacement_accounting must be a list")

    refs_by_fact_key: dict[str, list[str]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"accepted replacement accounting row {index} must be an object")
        reference = row.get("replaced_fact_ref")
        fact_keys = row.get("replacement_fact_keys")
        if not isinstance(reference, str) or not reference:
            raise ValueError(
                f"accepted replacement accounting row {index} has an invalid replaced_fact_ref"
            )
        if reference.startswith("id:"):
            raw_id = reference.removeprefix("id:")
            if not raw_id.isdecimal() or int(raw_id) <= 0:
                raise ValueError(
                    f"accepted replacement accounting row {index} has invalid id reference {reference}"
                )
        elif reference.startswith("key:"):
            if not reference.removeprefix("key:").strip():
                raise ValueError(
                    f"accepted replacement accounting row {index} has invalid key reference {reference}"
                )
        else:
            raise ValueError(
                f"accepted replacement accounting row {index} reference must use id: or key:"
            )
        if not isinstance(fact_keys, list) or not fact_keys or any(
            not nonempty_string(value) for value in fact_keys
        ):
            raise ValueError(
                f"accepted replacement accounting row {index} needs replacement fact keys"
            )
        for raw_key in fact_keys:
            key = str(raw_key)
            refs = refs_by_fact_key.setdefault(key, [])
            if reference not in refs:
                refs.append(reference)
    return path, refs_by_fact_key


def replacement_source_fact_ids_for_window(
    *,
    replacement_refs_by_fact_key: dict[str, list[str]],
    facts: list[dict[str, Any]],
    accepted_history: list[dict[str, Any]],
    plan: dict[str, Any],
) -> dict[str, list[int]]:
    """Resolve sidecar references against accepted and earlier window facts.

    The returned mapping is construction-only validation context. It never becomes
    part of either model payload or released fact schema.
    """
    sessions = plan.get("sessions") if isinstance(plan, dict) else None
    if not isinstance(sessions, list):
        raise ValueError("cannot resolve replacement evidence without writer sessions")

    fact_by_id: dict[int, dict[str, Any]] = {}
    existing_ids_by_key: dict[str, list[int]] = {}
    for row in facts:
        if not isinstance(row, dict):
            continue
        fact_id = row.get("id")
        if isinstance(fact_id, bool) or not isinstance(fact_id, int):
            continue
        if fact_id in fact_by_id:
            raise ValueError(f"accepted checkpoint has duplicate fact ID {fact_id}")
        fact_by_id[fact_id] = row
        construction_key = row.get("construction_key")
        if nonempty_string(construction_key):
            key = str(construction_key)
            existing_ids_by_key.setdefault(key, []).append(fact_id)
    accepted_history_ids = {
        str(row.get("id"))
        for row in accepted_history
        if isinstance(row, dict) and nonempty_string(row.get("id"))
    }

    next_id = max(fact_by_id, default=0) + 1
    planned_key_to_id: dict[str, int] = {}
    planned_key_position: dict[str, tuple[int, int]] = {}
    for session_index, session in enumerate(sessions):
        if not isinstance(session, dict):
            continue
        new_facts = session.get("new_facts")
        if not isinstance(new_facts, list):
            continue
        for fact_index, fact in enumerate(new_facts):
            if not isinstance(fact, dict) or not nonempty_string(fact.get("key")):
                continue
            key = str(fact["key"])
            if key in planned_key_to_id:
                raise ValueError(f"writer plan has duplicate new fact key {key}")
            if key in existing_ids_by_key:
                raise ValueError(
                    f"writer plan reuses accepted construction key {key}"
                )
            planned_key_to_id[key] = next_id
            planned_key_position[key] = (session_index, fact_index)
            next_id += 1

    resolved_by_fact_key: dict[str, list[int]] = {}
    for target_key, references in replacement_refs_by_fact_key.items():
        if target_key not in planned_key_to_id:
            continue
        target_position = planned_key_position[target_key]
        resolved: list[int] = []
        for reference in references:
            if reference.startswith("id:"):
                fact_id = int(reference.removeprefix("id:"))
                if fact_id not in fact_by_id:
                    raise ValueError(
                        f"replacement evidence for {target_key} references missing or future "
                        f"accepted fact {reference}"
                    )
            else:
                source_key = reference.removeprefix("key:")
                existing_matches = existing_ids_by_key.get(source_key) or []
                if len(existing_matches) > 1:
                    raise ValueError(
                        f"replacement evidence key {source_key} is ambiguous for {target_key}"
                    )
                in_existing = existing_matches[0] if existing_matches else None
                in_window = planned_key_to_id.get(source_key)
                if in_existing is not None and in_window is not None:
                    raise ValueError(
                        f"replacement evidence key {source_key} is ambiguous for {target_key}"
                    )
                if in_existing is not None:
                    fact_id = in_existing
                elif in_window is not None:
                    source_position = planned_key_position[source_key]
                    if source_position > target_position:
                        raise ValueError(
                            f"replacement evidence for {target_key} references future "
                            f"current-window fact {source_key}"
                        )
                    fact_id = in_window
                else:
                    raise ValueError(
                        f"replacement evidence for {target_key} references missing fact key "
                        f"{source_key}"
                    )
            source_fact = fact_by_id.get(fact_id)
            if source_fact is not None:
                source_sessions = source_fact.get("source_session_ids") or []
                if not isinstance(source_sessions, list) or not all(
                    nonempty_string(value) for value in source_sessions
                ):
                    raise ValueError(
                        f"replacement evidence source fact {fact_id} has invalid source sessions"
                    )
                unavailable_sources = [
                    str(value)
                    for value in source_sessions
                    if str(value) not in accepted_history_ids
                ]
                if unavailable_sources:
                    raise ValueError(
                        f"replacement evidence source fact {fact_id} names sessions "
                        "outside accepted history: " + ", ".join(unavailable_sources)
                    )
            if fact_id not in resolved:
                resolved.append(fact_id)
        if not resolved:
            raise ValueError(f"replacement evidence for {target_key} resolves no source facts")
        resolved_by_fact_key[target_key] = resolved
    return resolved_by_fact_key


def required_fact_declarations(
    developments: list[dict[str, Any]],
    current_facts: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    current_fact_ids = current_existing_fact_ids(current_facts)
    facts_by_construction_key: dict[str, list[dict[str, Any]]] = {}
    for current_fact in current_facts:
        if not isinstance(current_fact, dict) or not nonempty_string(
            current_fact.get("construction_key")
        ):
            continue
        construction_key = str(current_fact["construction_key"])
        facts_by_construction_key.setdefault(construction_key, []).append(current_fact)

    def resolve_supersedes(fact: dict[str, Any], source: str) -> list[int]:
        explicit = fact.get("supersedes") or []
        aliases = fact.get("supersedes_fact_keys") or []
        if not isinstance(explicit, list):
            raise ValueError(f"{source} supersedes must be a list of numeric fact IDs")
        if not isinstance(aliases, list):
            raise ValueError(f"{source} supersedes_fact_keys must be a list")

        resolved: list[int] = []
        for reference in explicit:
            if not isinstance(reference, int) or isinstance(reference, bool):
                raise ValueError(
                    f"{source} has non-numeric explicit supersedes reference: {reference!r}"
                )
            if reference not in current_fact_ids:
                raise ValueError(
                    f"{source} has stale or unknown explicit supersedes fact ID: {reference}"
                )
            resolved.append(reference)

        for raw_alias in aliases:
            if not nonempty_string(raw_alias):
                raise ValueError(
                    f"{source} has an invalid supersedes_fact_keys alias: {raw_alias!r}"
                )
            alias = str(raw_alias)
            matches = facts_by_construction_key.get(alias) or []
            earlier_window_declaration = result.get(alias)
            if not matches and earlier_window_declaration is None:
                raise ValueError(
                    f"{source} supersedes_fact_keys alias is missing from accepted facts "
                    f"and earlier declarations in this window: {alias}"
                )
            if not matches:
                resolved.append(alias)
                continue
            invalid_ids = [
                row.get("id")
                for row in matches
                if not isinstance(row.get("id"), int)
                or isinstance(row.get("id"), bool)
            ]
            if invalid_ids:
                raise ValueError(
                    f"{source} supersedes_fact_keys alias has a non-numeric fact ID: "
                    f"{alias} -> {invalid_ids!r}"
                )
            current_matches = [
                row for row in matches if int(row["id"]) in current_fact_ids
            ]
            if not current_matches:
                raise ValueError(
                    f"{source} supersedes_fact_keys alias resolves only to stale facts: {alias}"
                )
            if len(current_matches) != 1:
                raise ValueError(
                    f"{source} supersedes_fact_keys alias is ambiguous among current facts: "
                    f"{alias} -> {[int(row['id']) for row in current_matches]}"
                )
            if earlier_window_declaration is not None:
                raise ValueError(
                    f"{source} supersedes_fact_keys alias is ambiguous between an accepted "
                    f"fact and an earlier declaration in this window: {alias}"
                )
            resolved.append(int(current_matches[0]["id"]))
        return list(dict.fromkeys(resolved))

    def add(
        fact: Any,
        *,
        source: str,
        development_ids: list[str],
    ) -> None:
        if not isinstance(fact, dict) or not nonempty_string(fact.get("key")):
            raise ValueError(f"{source} has an invalid fact declaration")
        key = str(fact["key"])
        declaration = {
            "key": key,
            "statement": fact.get("statement"),
            "applies_when": fact.get("applies_when"),
            "subjects": copy.deepcopy(fact.get("subjects") or []),
            "supersedes": resolve_supersedes(fact, source),
        }
        existing = result.get(key)
        if existing is not None:
            if any(existing[field] != declaration[field] for field in declaration):
                raise ValueError(
                    f"conflicting required fact declarations for key {key}: {source}"
                )
            existing["development_ids"] = list(
                dict.fromkeys([*existing["development_ids"], *development_ids])
            )
            return
        result[key] = {
            **declaration,
            "development_ids": list(dict.fromkeys(development_ids)),
        }

    for development in developments:
        development_id = str(development.get("id") or "")
        for fact in development.get("facts_to_establish") or []:
            add(
                fact,
                source=f"required development {development_id}",
                development_ids=[development_id],
            )
    return result


def existing_reference_ids(state: dict[str, Any]) -> set[str]:
    references = {
        str(row.get("id") or "")
        for row in state.get("history") or []
        if isinstance(row, dict)
    }
    for row in state.get("session_purposes") or []:
        if not isinstance(row, dict):
            continue
        references.update(
            str(row.get(field) or "")
            for field in ("session_id", "planned_interaction_id")
        )
        references.update(str(value) for value in row.get("event_ids") or [])
    for row in state.get("unfinished_threads") or []:
        if not isinstance(row, dict):
            continue
        references.update(
            str(row.get(field) or "") for field in ("interaction_id", "session_id")
        )
    references.discard("")
    return references


def valid_existing_continues_from_ids(
    state: dict[str, Any],
    *,
    chronology: list[dict[str, Any]],
    recent_exact_messages: list[dict[str, Any]],
) -> list[str]:
    """Return the bounded accepted references exposed to the planner."""
    validator_references = existing_reference_ids(state)
    references: set[str] = set()

    for row in chronology:
        if not isinstance(row, dict):
            continue
        for field in ("session_id", "planned_interaction_id"):
            if nonempty_string(row.get(field)):
                references.add(str(row[field]))

    for row in state.get("unfinished_threads") or []:
        if not isinstance(row, dict):
            continue
        for field in ("session_id", "interaction_id"):
            if nonempty_string(row.get(field)):
                references.add(str(row[field]))

    for row in recent_exact_messages:
        if isinstance(row, dict) and nonempty_string(row.get("id")):
            references.add(str(row["id"]))

    unavailable = sorted(references - validator_references)
    if unavailable:
        raise ValueError(
            "bounded existing continues_from references are not accepted by "
            "the current validator: " + ", ".join(unavailable)
        )
    return sorted(references)


def accepted_contact_chronology_for_planner(
    state: dict[str, Any], start: date, *, quarter_start: date
) -> list[dict[str, Any]]:
    """Return contacts from this quarter and the thirteen weeks before it."""
    earliest = quarter_start - timedelta(weeks=13)
    purposes_by_session_id = {
        str(row.get("session_id") or ""): row
        for row in state.get("session_purposes") or []
        if isinstance(row, dict) and nonempty_string(row.get("session_id"))
    }
    fact_ids_by_session_id: dict[str, list[int | str]] = {}
    subject_ids_by_session_id: dict[str, list[str]] = {}
    for fact in state.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        fact_id = fact_reference(fact.get("id"))
        if fact_id is None:
            continue
        for session_id in fact.get("source_session_ids") or []:
            key = str(session_id)
            values = fact_ids_by_session_id.setdefault(key, [])
            if fact_id not in values:
                values.append(fact_id)
            subjects = subject_ids_by_session_id.setdefault(key, [])
            for subject in fact.get("subjects") or []:
                value = str(subject)
                if value and value not in subjects:
                    subjects.append(value)

    chronology: list[dict[str, Any]] = []
    for session in state.get("history") or []:
        if not isinstance(session, dict):
            continue
        session_id = str(session.get("id") or "")
        narrative_date = str(session.get("narrative_date") or "")
        try:
            contact_date = datetime.fromisoformat(narrative_date).date()
        except ValueError:
            continue
        if not earliest <= contact_date < start:
            continue
        purpose = purposes_by_session_id.get(session_id) or {}
        accepted_contact_details = str(
            purpose.get("accepted_contact_details") or ""
        ).strip()
        if not accepted_contact_details:
            accepted_contact_details = str(purpose.get("purpose") or "")
        subject_ids: list[str] = []
        for subject in [
            *(purpose.get("subject_ids") or []),
            *subject_ids_by_session_id.get(session_id, []),
        ]:
            value = str(subject)
            if value and value not in subject_ids:
                subject_ids.append(value)
        chronology.append(
            {
                "session_id": session_id,
                "planned_interaction_id": (
                    str(purpose["planned_interaction_id"])
                    if nonempty_string(purpose.get("planned_interaction_id"))
                    else None
                ),
                "date": contact_date.isoformat(),
                "purpose": str(purpose.get("purpose") or ""),
                "accepted_contact_details": accepted_contact_details,
                "thread_id": purpose.get("thread_id"),
                "event_ids": copy.deepcopy(purpose.get("event_ids") or []),
                "fact_ids": copy.deepcopy(fact_ids_by_session_id.get(session_id, [])),
                "subject_ids": subject_ids,
            }
        )
    return sorted(chronology, key=lambda row: str(row["date"]))


def accepted_contact_details_for_occurrence(occurrence: dict[str, Any]) -> str:
    """Keep planner situation and supplied material as deterministic plain text."""
    what_happened = str(occurrence.get("what_happened") or "")
    source_material = occurrence.get("source_material")
    factual_contents = (
        str(source_material.get("factual_contents") or "")
        if isinstance(source_material, dict)
        else ""
    )
    if not factual_contents:
        return what_happened
    return (
        f"What happened:\n{what_happened}\n\n"
        f"Source material:\n{factual_contents}"
    )


def accepted_contact_details_for_rendered_session(session: dict[str, Any]) -> str:
    """Return only the user-visible text accepted into the history."""
    messages = session.get("messages")
    if not isinstance(messages, list):
        raise ValueError("rendered session messages must be a list")
    details = "\n\n".join(
        str(message).strip() for message in messages if str(message).strip()
    )
    if not details:
        raise ValueError("rendered session must contain a nonempty message")
    return details


def readable_finished_topic(row: dict[str, Any]) -> str:
    """Return a useful compact description of an accepted contact."""
    thread_id = str(row.get("thread_id") or "").strip()
    if thread_id and thread_id not in {"self_contained", ORDINARY_CURRENT_LIFE_BASIS_ID}:
        value = re.sub(r"^(?:thread|contact|session)[_:-]+", "", thread_id)
        return re.sub(r"[_-]+", " ", value).strip()
    if nonempty_string(row.get("purpose")):
        return str(row["purpose"]).strip()
    value = next(
        (
            str(row[field])
            for field in ("planned_interaction_id", "session_id")
            if nonempty_string(row.get(field))
        ),
        "completed contact",
    )
    value = re.sub(r"^(?:contact|session)[_:-]+", "", value)
    return re.sub(r"[_-]+", " ", value).strip()


OLDER_CONTACT_SUMMARY_LIMIT = 320
SAME_MATTER_MESSAGE_CONTACT_LIMIT = 8
OLDER_OPERATION_PAYLOAD_ARGUMENTS = {
    "body",
    "message",
    "content",
    "comment",
    "text",
}


def bounded_same_matter_user_messages(
    state: dict[str, Any], *, thread_id: str
) -> list[dict[str, Any]]:
    """Return the latest accepted user messages for one matter, oldest first."""
    if not thread_id:
        return []
    purposes_by_session_id = {
        str(row.get("session_id") or ""): row
        for row in state.get("session_purposes") or []
        if isinstance(row, dict)
        and nonempty_string(row.get("session_id"))
        and str(row.get("thread_id") or "") == thread_id
    }
    rows: list[tuple[datetime, dict[str, Any]]] = []
    for session in state.get("history") or []:
        if not isinstance(session, dict):
            continue
        session_id = str(session.get("id") or "")
        purpose = purposes_by_session_id.get(session_id)
        if purpose is None:
            continue
        try:
            when = datetime.fromisoformat(str(session.get("narrative_date") or ""))
        except ValueError:
            continue
        messages = [
            str(message)
            for message in session.get("messages") or []
            if nonempty_string(message)
        ]
        if not messages:
            continue
        rows.append(
            (
                when,
                {
                "date": when.date().isoformat(),
                "contact_id": str(
                    purpose.get("planned_interaction_id") or session_id
                ),
                "messages": messages,
                },
            )
        )
    rows.sort(key=lambda row: (row[0], str(row[1]["contact_id"])))
    return [row for _, row in rows[-SAME_MATTER_MESSAGE_CONTACT_LIMIT:]]


def compact_older_contact_summary(row: dict[str, Any]) -> str:
    """Expose bounded concrete details for older repetition checks."""
    topic = readable_finished_topic(row)
    details = " ".join(str(row.get("accepted_contact_details") or "").split())
    summary = details or topic
    if len(summary) > OLDER_CONTACT_SUMMARY_LIMIT:
        summary = summary[: OLDER_CONTACT_SUMMARY_LIMIT - 3].rstrip() + "..."
    return summary


def compact_older_contact_operation_identities(
    operations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep prior operation identities without exposing their written payloads."""
    compact: list[dict[str, Any]] = []
    for operation in operations:
        tool = operation.get("tool")
        args = operation.get("args")
        if not nonempty_string(tool) or not isinstance(args, dict):
            continue
        compact.append(
            {
                "tool": str(tool),
                "arguments": {
                    str(key): copy.deepcopy(value)
                    for key, value in args.items()
                    if str(key).casefold() not in OLDER_OPERATION_PAYLOAD_ARGUMENTS
                },
            }
        )
    return compact


def _nested_nonempty_string(value: Any, path: tuple[str, ...]) -> str | None:
    current = value
    for field in path:
        if not isinstance(current, dict):
            return None
        current = current.get(field)
    return str(current) if nonempty_string(current) else None


def _app_item_titles_by_id(app_state: dict[str, Any]) -> dict[str, str]:
    """Index titles already present in app state without exposing record bodies."""
    titles: dict[str, str] = {}

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                visit(child)
            return
        if not isinstance(value, dict):
            return
        title = value.get("title")
        if nonempty_string(title):
            for field in APP_ITEM_ID_FIELDS:
                record_id = value.get(field)
                if nonempty_string(record_id):
                    titles[str(record_id)] = str(title)
        for child in value.values():
            if isinstance(child, (dict, list)):
                visit(child)

    visit(app_state)
    return titles


def accepted_app_items_by_contact(
    state: dict[str, Any], *, accepted_contacts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Map accepted create/update operations to the exact app items they touched."""
    contacts_by_session_id: dict[str, dict[str, str]] = {}
    for row in accepted_contacts:
        if not isinstance(row, dict) or not nonempty_string(row.get("session_id")):
            continue
        session_id = str(row["session_id"])
        contacts_by_session_id[session_id] = {
            "contact_id": str(row.get("planned_interaction_id") or session_id),
            "session_id": session_id,
        }
    titles_by_id = _app_item_titles_by_id(state.get("app_state") or {})
    items_by_session_id: dict[str, list[dict[str, str]]] = {}
    for row in state.get("operation_results") or []:
        if not isinstance(row, dict):
            continue
        session_id = str(row.get("session_id") or "")
        if session_id not in contacts_by_session_id:
            continue
        operation = row.get("operation")
        result = row.get("result")
        if not isinstance(operation, dict) or not isinstance(result, dict):
            continue
        tool = str(operation.get("tool") or "")
        id_paths = APP_ITEM_RECORD_ID_PATHS_BY_TOOL.get(tool)
        args = operation.get("args")
        if id_paths is None or not isinstance(args, dict):
            continue
        sources = {"args": args, "result": result}
        record_id = next(
            (
                value
                for path in id_paths
                for value in [_nested_nonempty_string(sources, path)]
                if value is not None
            ),
            None,
        )
        operation_result_id = next(
            (
                value
                for path in APP_ITEM_RESULT_ID_PATHS
                for value in [_nested_nonempty_string(result, path)]
                if value is not None
            ),
            None,
        )
        if record_id is None or operation_result_id is None:
            continue
        item = {
            "tool": tool,
            "record_id": record_id,
            "operation_result_id": operation_result_id,
        }
        title = next(
            (
                value
                for value in (
                    _nested_nonempty_string(args, ("title",)),
                    _nested_nonempty_string(result, ("title",)),
                    _nested_nonempty_string(result, ("event", "title")),
                    titles_by_id.get(record_id),
                )
                if value is not None
            ),
            None,
        )
        if title is not None:
            item["title"] = title
        items_by_session_id.setdefault(session_id, []).append(item)

    return [
        {
            **contacts_by_session_id[session_id],
            "items": items_by_session_id[session_id],
        }
        for session_id in contacts_by_session_id
        if session_id in items_by_session_id
    ]


def current_ordinary_states_from_session_purposes(
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return the latest accepted ordinary state for each stable state key."""
    rows: list[tuple[datetime, int, dict[str, Any]]] = []
    for index, purpose in enumerate(state.get("session_purposes") or []):
        if not isinstance(purpose, dict) or not purpose.get("current_state_changes"):
            continue
        if not nonempty_string(purpose.get("session_id")):
            raise ValueError("ordinary state change is missing its source session_id")
        try:
            when = datetime.fromisoformat(str(purpose.get("narrative_date") or ""))
        except ValueError as error:
            raise ValueError(
                "ordinary state change is missing a valid source narrative_date"
            ) from error
        changes = purpose.get("current_state_changes")
        if not isinstance(changes, list):
            raise ValueError("ordinary state changes must be a list")
        rows.append((when, index, purpose))

    latest_by_key: dict[str, tuple[datetime, int, dict[str, Any]]] = {}
    for when, index, purpose in sorted(rows, key=lambda row: (row[0], row[1])):
        for change in purpose["current_state_changes"]:
            if not isinstance(change, dict):
                raise ValueError("ordinary state change must be an object")
            key = str(change.get("key") or "")
            if not key:
                raise ValueError("ordinary state change key must be non-empty")
            latest_by_key[key] = (
                when,
                index,
                {
                    "key": key,
                    "subject_ids": copy.deepcopy(change.get("subject_ids") or []),
                    "statement": str(change.get("statement") or ""),
                    "source_session_id": str(purpose["session_id"]),
                    "date": when.date().isoformat(),
                },
            )
    return [
        row[2]
        for _, row in sorted(
            latest_by_key.items(),
            key=lambda item: (item[1][0], item[1][1], item[0]),
        )
    ]


def current_ordinary_states_for_open_threads(
    state: dict[str, Any],
    *,
    unfinished_threads: list[dict[str, Any]],
    current_ordinary_states: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Return each open thread's latest ordinary states from that same thread."""
    open_thread_ids = {
        str(thread.get("thread_id") or "")
        for thread in unfinished_threads
        if isinstance(thread, dict) and nonempty_string(thread.get("thread_id"))
    }
    if not open_thread_ids:
        return {}
    thread_id_by_session_id = {
        str(purpose.get("session_id") or ""): str(purpose.get("thread_id") or "")
        for purpose in state.get("session_purposes") or []
        if isinstance(purpose, dict)
        and nonempty_string(purpose.get("session_id"))
        and nonempty_string(purpose.get("thread_id"))
    }
    result = {thread_id: [] for thread_id in open_thread_ids}
    for ordinary_state in current_ordinary_states:
        if not isinstance(ordinary_state, dict):
            raise ValueError("ordinary state must be an object")
        thread_id = thread_id_by_session_id.get(
            str(ordinary_state.get("source_session_id") or "")
        )
        if thread_id in result:
            result[thread_id].append(copy.deepcopy(ordinary_state))
    return result


def compact_current_facts_for_model(
    facts: list[dict[str, Any]], *, include_supersession_dependencies: bool = False
) -> list[dict[str, Any]]:
    """Compact active facts, with optional retired facts needed only for replacement."""
    compact: list[dict[str, Any]] = []
    normalized = facts_with_current(copy.deepcopy(facts))
    supplied_current = {
        fact_reference(fact.get("id")): fact.get("current")
        for fact in facts
        if isinstance(fact, dict) and isinstance(fact.get("current"), bool)
    }
    for fact in normalized:
        fact_id = fact_reference(fact.get("id"))
        is_current = supplied_current.get(fact_id, fact["current"])
        if not is_current and not include_supersession_dependencies:
            continue
        row = {
            "id": fact.get("id"),
            "statement": fact.get("statement"),
            "applies_when": fact.get("applies_when"),
            "subjects": copy.deepcopy(fact.get("subjects") or []),
        }
        if nonempty_string(fact.get("construction_key")):
            row["construction_key"] = str(fact["construction_key"])
        if not is_current:
            row["available_only_for_supersession"] = True
        compact.append(row)
    return compact


def resolve_current_fact_references_for_model(
    developments: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    *,
    current_fact_ids: set[int | str] | None = None,
) -> list[dict[str, Any]]:
    """Replace stale development fact references with current descendants."""
    current_fact_ids = set(
        current_fact_ids
        if current_fact_ids is not None
        else current_existing_fact_ids(facts)
    )
    superseding_fact_ids: dict[int | str, list[int | str]] = {}
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        fact_id = fact_reference(fact.get("id"))
        if fact_id is None:
            continue
        for raw_superseded_id in fact.get("supersedes") or []:
            superseded_id = fact_reference(raw_superseded_id)
            if superseded_id is not None:
                superseding_fact_ids.setdefault(superseded_id, []).append(fact_id)

    def validate_acyclic(fact_id: int | str, path: tuple[int | str, ...]) -> None:
        if fact_id in path:
            cycle = (*path[path.index(fact_id) :], fact_id)
            raise ValueError(
                "fact supersession cycle in explicit registry links: "
                + " -> ".join(map(str, cycle))
            )
        for successor in superseding_fact_ids.get(fact_id, []):
            validate_acyclic(successor, (*path, fact_id))

    for fact_id in superseding_fact_ids:
        validate_acyclic(fact_id, ())

    def current_descendants(fact_id: int | str) -> list[int | str]:
        if fact_id in current_fact_ids:
            return [fact_id]
        descendants: list[int | str] = []
        for successor in superseding_fact_ids.get(fact_id, []):
            for descendant in current_descendants(successor):
                if descendant not in descendants:
                    descendants.append(descendant)
        if not descendants:
            raise ValueError(
                f"fact {fact_id} has no route to a current fact through explicit "
                "supersedes links"
            )
        return descendants

    resolved_developments = copy.deepcopy(developments)
    for development in resolved_developments:
        if not isinstance(development, dict):
            continue
        references = development.get("relevant_existing_fact_ids")
        if not isinstance(references, list):
            continue
        resolved_references: list[int | str] = []
        for raw_reference in references:
            reference = fact_reference(raw_reference)
            if reference is None:
                continue
            for descendant in current_descendants(reference):
                if descendant not in resolved_references:
                    resolved_references.append(descendant)
        development["relevant_existing_fact_ids"] = resolved_references
    return resolved_developments


def model_fact_fields(fact: dict[str, Any]) -> dict[str, Any]:
    """Return exactly the fact fields that the planner is allowed to produce."""
    return {
        field: copy.deepcopy(fact.get(field))
        for field in ("key", "statement", "applies_when", "subjects", "supersedes")
    }


def planner_open_threads(
    unfinished_threads: list[dict[str, Any]],
    *,
    prior_accepted_contacts_by_thread: dict[str, list[dict[str, Any]]] | None = None,
    earlier_user_messages_by_thread: dict[str, list[dict[str, Any]]] | None = None,
    current_ordinary_states_by_thread: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Expose open work and its accepted contact history to the weekly planner."""
    prior_accepted_contacts_by_thread = prior_accepted_contacts_by_thread or {}
    earlier_user_messages_by_thread = earlier_user_messages_by_thread or {}
    current_ordinary_states_by_thread = current_ordinary_states_by_thread or {}
    result: list[dict[str, Any]] = []
    for index, thread in enumerate(unfinished_threads):
        if not isinstance(thread, dict):
            raise ValueError(f"unfinished_threads[{index}] must be an object")
        basis_id = str(thread.get("thread_id") or "")
        if not basis_id:
            raise ValueError(
                f"unfinished_threads[{index}] is missing thread_id; cannot expose a basis_id"
            )
        prior_contact_id = next(
            (
                str(thread[field])
                for field in ("session_id", "interaction_id")
                if nonempty_string(thread.get(field))
            ),
            "",
        )
        if not prior_contact_id:
            raise ValueError(
                f"unfinished_threads[{index}] is missing session_id or interaction_id; "
                "cannot expose a continues_from_id"
            )
        result.append(
            {
                "basis_id": basis_id,
                "continues_from_id": prior_contact_id,
                "subject_ids": copy.deepcopy(thread.get("subjects") or []),
                "what_already_happened": str(thread.get("contact_summary") or ""),
                "what_remains": copy.deepcopy(
                    thread.get("what_remains_after_contact")
                ),
                "current_fact_ids": copy.deepcopy(thread.get("current_fact_ids") or []),
                "prior_accepted_contacts": copy.deepcopy(
                    prior_accepted_contacts_by_thread.get(basis_id, [])
                ),
                "earlier_user_messages_in_same_matter": copy.deepcopy(
                    earlier_user_messages_by_thread.get(basis_id, [])
                ),
                "current_ordinary_states": copy.deepcopy(
                    current_ordinary_states_by_thread.get(basis_id, [])
                ),
            }
        )
    return result


def prior_accepted_contacts_for_open_threads(
    state: dict[str, Any],
    *,
    start: date,
    unfinished_threads: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Return each open thread's accepted contact details in chronological order."""
    thread_ids = {
        str(thread.get("thread_id"))
        for thread in unfinished_threads
        if isinstance(thread, dict) and nonempty_string(thread.get("thread_id"))
    }
    if not thread_ids:
        return {}

    purposes_by_session_id = {
        str(row.get("session_id") or ""): row
        for row in state.get("session_purposes") or []
        if isinstance(row, dict) and nonempty_string(row.get("session_id"))
    }
    result = {thread_id: [] for thread_id in thread_ids}
    for session in state.get("history") or []:
        if not isinstance(session, dict):
            continue
        session_id = str(session.get("id") or "")
        narrative_date = str(session.get("narrative_date") or "")
        try:
            contact_date = datetime.fromisoformat(narrative_date).date()
        except ValueError:
            continue
        if contact_date >= start:
            continue
        purpose = purposes_by_session_id.get(session_id) or {}
        thread_id = str(purpose.get("thread_id") or "")
        if thread_id not in result:
            continue
        accepted_contact_details = str(
            purpose.get("accepted_contact_details") or ""
        ).strip()
        if not accepted_contact_details:
            accepted_contact_details = "\n\n".join(
                str(message)
                for message in session.get("messages") or []
                if nonempty_string(message)
            ).strip()
        if not accepted_contact_details:
            accepted_contact_details = str(purpose.get("purpose") or "")
        result[thread_id].append(
            {
                "date": contact_date.isoformat(),
                "contact_id": (
                    str(purpose["planned_interaction_id"])
                    if nonempty_string(purpose.get("planned_interaction_id"))
                    else session_id
                ),
                "accepted_contact_details": accepted_contact_details,
            }
        )
    for contacts in result.values():
        contacts.sort(key=lambda row: (str(row["date"]), str(row["contact_id"])))
    return result


def unfinished_thread_current_fact_ids(
    facts: list[dict[str, Any]],
    *,
    recent_chronology: list[dict[str, Any]],
    unfinished_threads: list[dict[str, Any]],
) -> dict[str, list[int | str]]:
    """Return current fact IDs selected by the existing open-thread routes."""
    thread_rows = [
        (
            (
                str(row["thread_id"])
                if nonempty_string(row.get("thread_id"))
                else f"__unfinished_thread_{index}"
            ),
            row,
        )
        for index, row in enumerate(unfinished_threads)
        if isinstance(row, dict)
    ]
    thread_ids = list(dict.fromkeys(thread_id for thread_id, _ in thread_rows))
    if not thread_ids:
        return {}

    chronology_fact_ids: dict[str, set[int | str]] = {
        thread_id: set() for thread_id in thread_ids
    }
    for row in recent_chronology:
        if not isinstance(row, dict):
            continue
        thread_id = str(row.get("thread_id") or "")
        if thread_id not in chronology_fact_ids:
            continue
        for value in row.get("fact_ids") or []:
            reference = fact_reference(value)
            if reference is not None:
                chronology_fact_ids[thread_id].add(reference)

    thread_source_session_ids: dict[str, set[str]] = {}
    for thread_id, row in thread_rows:
        thread_source_session_ids.setdefault(thread_id, set()).update(
            str(row[field])
            for field in ("session_id", "interaction_id")
            if nonempty_string(row.get(field))
        )

    result: dict[str, list[int | str]] = {thread_id: [] for thread_id in thread_ids}
    for fact in facts_with_current(copy.deepcopy(facts)):
        if not fact.get("current"):
            continue
        fact_id = fact_reference(fact.get("id"))
        if fact_id is None:
            continue
        source_session_ids = {
            str(session_id)
            for session_id in fact.get("source_session_ids") or []
            if nonempty_string(session_id)
        }
        for thread_id in thread_ids:
            if (
                fact_id in chronology_fact_ids[thread_id]
                or source_session_ids & thread_source_session_ids[thread_id]
            ):
                result[thread_id].append(fact_id)
    return result


def planner_current_facts(
    facts: list[dict[str, Any]],
    *,
    quarter_developments: list[dict[str, Any]],
    recent_chronology: list[dict[str, Any]],
    primary_subject_ids: set[str],
    selected_subject_ids: set[str] | None = None,
    unfinished_threads: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Select required planning constraints in original registry order."""
    required_fact_ids: set[int | str] = set()

    fact_ids_by_construction_key: dict[str, int | str] = {}
    for fact in facts:
        if not isinstance(fact, dict) or not nonempty_string(
            fact.get("construction_key")
        ):
            continue
        reference = fact_reference(fact.get("id"))
        if reference is None:
            continue
        key = str(fact["construction_key"])
        existing = fact_ids_by_construction_key.get(key)
        if existing is not None and existing != reference:
            raise ValueError(
                f"accepted fact registry repeats construction_key: {key}"
            )
        fact_ids_by_construction_key[key] = reference

    def add_references(rows: list[dict[str, Any]], field: str) -> None:
        for row in rows:
            for value in row.get(field) or []:
                reference = fact_reference(value)
                if reference is not None:
                    required_fact_ids.add(reference)

    add_references(quarter_developments, "relevant_existing_fact_ids")
    for development in quarter_developments:
        for fact in development.get("facts_to_establish") or []:
            if isinstance(fact, dict):
                for value in fact.get("supersedes") or []:
                    reference = fact_reference(value)
                    if reference is not None:
                        required_fact_ids.add(reference)
                for key in fact.get("supersedes_fact_keys") or []:
                    reference = fact_ids_by_construction_key.get(str(key))
                    if reference is not None:
                        required_fact_ids.add(reference)
    facts_by_id = {
        reference: fact
        for fact in facts
        if isinstance(fact, dict)
        for reference in [fact_reference(fact.get("id"))]
        if reference is not None
    }

    missing_required_fact_ids = sorted(
        required_fact_ids - set(facts_by_id), key=str
    )
    if missing_required_fact_ids:
        raise ValueError(
            "accepted planner inputs select unknown fact IDs: "
            + ", ".join(map(str, missing_required_fact_ids))
        )

    # Recent chronology is evidence of what happened, not a reason to resend every
    # fact established recently. Open threads are the only history-based exception:
    # their current facts remain live planning constraints until resolved.
    open_thread_fact_ids = {
        fact_id
        for fact_ids in unfinished_thread_current_fact_ids(
            facts,
            recent_chronology=recent_chronology,
            unfinished_threads=unfinished_threads or [],
        ).values()
        for fact_id in fact_ids
    }
    selected_non_primary_subjects = {
        str(subject)
        for subject in selected_subject_ids or set()
        if nonempty_string(subject) and str(subject) not in primary_subject_ids
    }
    for development in quarter_developments:
        if not isinstance(development, dict):
            continue
        selected_non_primary_subjects.update(
            str(subject)
            for subject in development.get("subjects") or []
            if nonempty_string(subject) and str(subject) not in primary_subject_ids
        )
        for planned_fact in development.get("facts_to_establish") or []:
            if not isinstance(planned_fact, dict):
                continue
            selected_non_primary_subjects.update(
                str(subject)
                for subject in planned_fact.get("subjects") or []
                if nonempty_string(subject) and str(subject) not in primary_subject_ids
            )
    selected: list[dict[str, Any]] = []
    for fact in facts_with_current(copy.deepcopy(facts)):
        fact_id = fact_reference(fact.get("id"))
        fact_subjects = {
            str(subject)
            for subject in fact.get("subjects") or []
            if nonempty_string(subject)
        }
        selected_for_subject = bool(fact_subjects & selected_non_primary_subjects)
        selected_for_primary_only = bool(fact_subjects) and fact_subjects.issubset(
            primary_subject_ids
        )
        if (
            fact["current"]
            and (
                fact_id in required_fact_ids
                or fact_id in open_thread_fact_ids
                or selected_for_subject
                or selected_for_primary_only
            )
        ):
            selected.append(fact)
    return selected


def planner_known_entities(
    entities: list[dict[str, Any]],
    *,
    quarter_developments: list[dict[str, Any]],
    current_facts: list[dict[str, Any]],
    relevant_developments: list[dict[str, Any]],
    unfinished_threads: list[dict[str, Any]],
    required_entities: list[dict[str, Any]],
    primary_subject_ids: set[str],
    recent_subject_ids: set[str] | None = None,
    app_record_subject_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return only entities that can matter to this weekly planning window."""
    selected_ids = set(primary_subject_ids)
    for row in [
        *quarter_developments,
        *relevant_developments,
        *unfinished_threads,
    ]:
        if isinstance(row, dict):
            selected_ids.update(
                str(subject)
                for subject in row.get("subjects") or []
                if nonempty_string(subject)
            )
    for fact in current_facts:
        if isinstance(fact, dict):
            selected_ids.update(
                str(subject)
                for subject in fact.get("subjects") or []
                if nonempty_string(subject)
            )
    selected_ids.update(str(subject) for subject in recent_subject_ids or set())
    selected_ids.update(str(subject) for subject in app_record_subject_ids or set())
    selected_ids.update(
        str(entity.get("id"))
        for entity in required_entities
        if isinstance(entity, dict) and nonempty_string(entity.get("id"))
    )
    return [
        model_entity_identity(entity)
        for entity in entities
        if isinstance(entity, dict)
        and nonempty_string(entity.get("id"))
        and str(entity["id"]) in selected_ids
    ]


def new_entity_candidates_for_week(
    quarter_entities: list[dict[str, Any]],
    *,
    relevant_developments: list[dict[str, Any]],
    current_entities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose not-yet-known entities whose introduction event overlaps the week."""
    current_ids = {
        str(entity.get("id"))
        for entity in current_entities
        if isinstance(entity, dict) and nonempty_string(entity.get("id"))
    }
    relevant_ids = {
        str(development.get("id"))
        for development in relevant_developments
        if isinstance(development, dict) and nonempty_string(development.get("id"))
    }
    return [
        copy.deepcopy(entity)
        for entity in quarter_entities
        if isinstance(entity, dict)
        and nonempty_string(entity.get("id"))
        and str(entity["id"]) not in current_ids
        and str(entity.get("introduced_in_event_id") or "") in relevant_ids
    ]


def select_new_entities_for_week(
    planner_response: dict[str, Any],
    candidate_entities: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep only candidates used at or after their introduction occurrence."""
    candidates_by_id = {
        str(entity["id"]): entity
        for entity in candidate_entities
        if isinstance(entity, dict) and nonempty_string(entity.get("id"))
    }
    occurrences = planner_response.get("plan", {}).get("occurrences") or []
    introduction_positions = {
        entity_id: next(
            (
                index
                for index, occurrence in enumerate(occurrences)
                if isinstance(occurrence, dict)
                and introduction_id
                and introduction_id in {
                    str(value) for value in occurrence.get("development_ids") or []
                }
            ),
            None,
        )
        for entity_id, entity in candidates_by_id.items()
        for introduction_id in [str(entity.get("introduced_in_event_id") or "")]
    }
    selected_ids: set[str] = set()
    errors: list[str] = []
    for index, occurrence in enumerate(occurrences):
        if not isinstance(occurrence, dict):
            continue
        label = f"occurrences[{index}]"
        for raw_entity_id in occurrence.get("subject_ids") or []:
            entity_id = str(raw_entity_id)
            candidate = candidates_by_id.get(entity_id)
            if candidate is None:
                continue
            introduction_id = str(candidate.get("introduced_in_event_id") or "")
            introduction_index = introduction_positions[entity_id]
            if introduction_index is None:
                errors.append(
                    f"{label}.subject_ids uses new entity {entity_id} without "
                    f"its introduction event {introduction_id}"
                )
            elif index < introduction_index:
                errors.append(
                    f"{label}.subject_ids uses new entity {entity_id} before "
                    f"its introduction event {introduction_id}"
                )
            else:
                selected_ids.add(entity_id)
    return (
        [
            candidate
            for candidate in candidate_entities
            if str(candidate["id"]) in selected_ids
        ],
        errors,
    )


def model_entity_identity(entity: dict[str, Any]) -> dict[str, Any]:
    """Expose stable identity fields; mutable roles belong in current facts."""
    return {
        field: copy.deepcopy(entity[field])
        for field in MODEL_ENTITY_IDENTITY_FIELDS
        if field in entity and entity[field] is not None
    }


def entity_ids_named_in_app_records(
    entities: list[dict[str, Any]],
    app_records: dict[str, list[dict[str, Any]]],
) -> set[str]:
    """Return canonical IDs whose exact names or aliases appear in visible records."""
    record_values = {
        str(value).strip().casefold()
        for rows in app_records.values()
        for row in rows
        for raw_value in row.values()
        for value in (raw_value if isinstance(raw_value, list) else [raw_value])
        if isinstance(value, str) and value.strip()
    }
    selected: set[str] = set()
    for entity in entities:
        if not isinstance(entity, dict) or not nonempty_string(entity.get("id")):
            continue
        identity_values = [
            entity.get("id"),
            entity.get("name"),
            *(entity.get("aliases") or []),
        ]
        if any(
            isinstance(value, str)
            and value.strip().casefold() in record_values
            for value in identity_values
        ):
            selected.add(str(entity["id"]))
    return selected


def compact_planner_app_records(
    tools: dict[str, dict[str, Any]],
    app_state: dict[str, Any],
    *,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Expose compact records plus text that an available update tool can replace."""
    visible_state = readable_app_state(tools, app_state)
    compact: dict[str, list[dict[str, Any]]] = {}
    for app_name, value in visible_state.items():
        source_rows: list[tuple[str | None, dict[str, Any]]] = []
        if isinstance(value, list):
            source_rows.extend(
                (None, row) for row in value if isinstance(row, dict)
            )
        elif isinstance(value, dict):
            source_rows.extend(
                (str(record_key), row)
                for record_key, row in value.items()
                if isinstance(row, dict)
            )

        if start is not None and end is not None and app_name == "calendar":
            earliest = start - timedelta(days=14)
            latest = end + timedelta(days=90)
            source_rows = [
                (record_key, row)
                for record_key, row in source_rows
                if calendar_record_overlaps_dates(row, earliest, latest)
            ]
        elif start is not None and app_name == "inbox":
            earliest = start - timedelta(days=30)
            source_rows = [
                (record_key, row)
                for record_key, row in source_rows
                if (
                    (record_date := iso_date(row.get("date"))) is None
                    or record_date >= earliest
                )
            ]
        elif start is not None and end is not None and app_name == "oncall_schedule":
            earliest = start - timedelta(days=14)
            latest = end + timedelta(days=90)
            source_rows = [
                (record_key, row)
                for record_key, row in source_rows
                if (
                    (record_date := iso_date(row.get("week_of"))) is None
                    or earliest <= record_date <= latest
                )
            ]

        rows: list[dict[str, Any]] = []
        for record_key, row in source_rows:
            visible_fields = PLANNER_APP_RECORD_FIELDS
            record = {
                field: copy.deepcopy(row[field])
                for field in visible_fields
                if field in row
                and (
                    isinstance(row[field], (str, int, float, bool))
                    or row[field] is None
                    or (
                        isinstance(row[field], list)
                        and all(
                            isinstance(item, (str, int, float, bool))
                            or item is None
                            for item in row[field]
                        )
                    )
                )
            }
            if record_key is not None and not any(
                field in record for field in ("id", "plan_id", "name", "title")
            ):
                record["record_key"] = record_key
            if record:
                rows.append(record)
        compact[str(app_name)] = rows
    return compact


def _record_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        return [row for row in value.values() if isinstance(row, dict)]
    return []


def _exact_app_records_for_contacts(
    planner_payload: dict[str, Any], contacts: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Expose original records only when a fixed contact explicitly needs them."""
    raw_records = planner_payload.get("_exact_app_records") or {}
    compact_records = planner_payload.get("available_app_records") or {}
    contact_text = " ".join(
        json.dumps(contact, ensure_ascii=False, sort_keys=True)
        for contact in contacts
    ).casefold()
    linked_ids = {
        str(item.get("record_id"))
        for contact_id in {
            str(contact.get("contact_id") or "")
            for contact in contacts
            if contact.get("uses_items_created_by_contact_ids")
        }
        for group in planner_payload.get("earlier_accepted_app_items") or []
        if str(group.get("contact_id") or "") in {
            str(value)
            for contact in contacts
            for value in contact.get("uses_items_created_by_contact_ids") or []
        }
        for item in group.get("items") or []
        if isinstance(item, dict) and nonempty_string(item.get("record_id"))
    }
    selected: dict[str, list[dict[str, Any]]] = {}
    for app_name, compact_rows in compact_records.items():
        raw_by_identity = _record_rows(raw_records.get(app_name))
        rows: list[dict[str, Any]] = []
        for compact_row in compact_rows or []:
            if not isinstance(compact_row, dict):
                continue
            identities = {
                str(compact_row[field])
                for field in ("id", "plan_id", "name", "title", "subject", "key", "asof_date")
                if nonempty_string(compact_row.get(field))
            }
            if app_name != "calendar" and not identities.intersection(linked_ids) and not any(
                value.casefold() in contact_text for value in identities if len(value) >= 3
            ):
                continue
            matched: dict[str, Any] | None = None
            for field in ("id", "plan_id", "record_key"):
                if not nonempty_string(compact_row.get(field)):
                    continue
                matched = next(
                    (
                        raw_row
                        for raw_row in raw_by_identity
                        if str(raw_row.get(field) or "") == str(compact_row[field])
                    ),
                    None,
                )
                if matched is not None:
                    break
            if matched is None:
                descriptive_fields = ("name", "title", "subject", "key", "asof_date")
                candidates = [
                    raw_row
                    for raw_row in raw_by_identity
                    if any(
                        nonempty_string(compact_row.get(field))
                        and str(raw_row.get(field) or "") == str(compact_row[field])
                        for field in descriptive_fields
                    )
                ]
                if len(candidates) == 1:
                    matched = candidates[0]
            if matched is not None and matched not in rows:
                rows.append(copy.deepcopy(matched))
        if rows:
            selected[str(app_name)] = rows
    return selected


def calendar_update_context_errors(
    response: dict[str, Any],
    *,
    current_app_records: dict[str, Any],
    planner_app_records: dict[str, Any],
) -> list[str]:
    """Require the exact planner to see the current body of events it updates."""
    current_by_id = {
        str(row.get("id")): row
        for row in _record_rows(current_app_records.get("calendar"))
        if nonempty_string(row.get("id"))
    }
    supplied_by_id = {
        str(row.get("id")): row
        for row in _record_rows(planner_app_records.get("calendar"))
        if nonempty_string(row.get("id"))
    }
    errors: list[str] = []
    for occurrence in response.get("plan", {}).get("occurrences") or []:
        if not isinstance(occurrence, dict):
            continue
        for operation in occurrence.get("app_operations") or []:
            if not isinstance(operation, dict) or operation.get("tool") != "update_calendar_event":
                continue
            event_id = str((operation.get("args") or {}).get("event_id") or "")
            current = current_by_id.get(event_id)
            if current is None or not nonempty_string(current.get("body")):
                continue
            supplied = supplied_by_id.get(event_id)
            if supplied is None or supplied.get("body") != current.get("body"):
                contact_id = str(occurrence.get("contact_id") or "unknown contact")
                errors.append(
                    f"{contact_id} updates calendar event {event_id}, but the exact "
                    "planner did not receive its complete current body"
                )
    return list(dict.fromkeys(errors))


def iso_date(value: Any) -> date | None:
    if not nonempty_string(value):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def calendar_record_overlaps_dates(
    record: dict[str, Any], start: date, end: date
) -> bool:
    """Return whether a calendar record overlaps any requested calendar date."""
    record_start = iso_date(record.get("start"))
    record_end = iso_date(record.get("end")) or record_start
    if record_start is None or record_end is None:
        return False
    return record_start <= end and record_end >= start


def compact_tool_capabilities(
    tools: dict[str, dict[str, Any]] | None = None,
    app_records: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Expose only actions that can work from the planner's visible state."""
    visible_records = app_records or {}
    creatable_state_keys = {
        str(key)
        for tool in (tools or {}).values()
        for key in (tool.get("state_effect") or {}).get("writes_state_keys") or []
        if not (tool.get("state_effect") or {}).get("reads_state_keys")
        or (tool.get("state_effect") or {}).get("does_not_require_lookup")
    }
    capabilities: list[dict[str, Any]] = []
    for tool_name in sorted(tools or {}):
        state_effect = tools[tool_name].get("state_effect") or {}
        if state_effect.get("generation_available") is False:
            continue
        read_keys = [str(key) for key in state_effect.get("reads_state_keys") or []]
        if state_effect.get("generation_requires_visible_records") is True and any(
            not visible_records.get(key) for key in read_keys
        ):
            continue
        if (
            read_keys
            and not state_effect.get("empty_result_is_valid")
            and not all(
                visible_records.get(key) or key in creatable_state_keys
                for key in read_keys
            )
        ):
            continue
        description = str(tools[tool_name].get("description") or "").strip()
        if not description:
            raise ValueError(f"simulated tool lacks a plain description: {tool_name}")
        capability: dict[str, Any] = {
            "tool_name": tool_name,
            "description": description,
            "arguments": copy.deepcopy(tools[tool_name].get("arguments") or []),
            "required_arguments": copy.deepcopy(
                tools[tool_name].get("required_arguments") or []
            ),
        }
        for field in (
            "returns",
            "visibility",
            "notes",
            "hard_requirements",
            "does_not_accept_fields",
            "args_policy",
            "lookup_policy",
            "required_existing_record",
        ):
            value = state_effect.get(field)
            if isinstance(value, str) and value.strip():
                capability[field] = value.strip()
            elif isinstance(value, list) and all(
                isinstance(item, str) and item.strip() for item in value
            ):
                capability[field] = [item.strip() for item in value]
            elif isinstance(value, dict):
                capability[field] = copy.deepcopy(value)
        capabilities.append(capability)
    return capabilities


def build_planner_payload(
    *,
    config: dict[str, Any],
    checkpoint: dict[str, Any],
    state: dict[str, Any],
    start: date,
    end: date,
    tools: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    quarter_document = load_json(resolve_path(config["quarter_plan"]))
    spec = yaml.safe_load(resolve_path(config["spec"]).read_text()) or {}
    primary_subject_values = spec.get("primary_subject_ids")
    if not string_list(primary_subject_values, allow_empty=False):
        raise ValueError(
            "spec primary_subject_ids must be a non-empty list of unique entity IDs"
        )
    primary_subject_ids = {str(subject) for subject in primary_subject_values}
    known_entity_ids = {
        str(entity.get("id"))
        for entity in state.get("entities") or []
        if isinstance(entity, dict) and nonempty_string(entity.get("id"))
    }
    unknown_primary_subject_ids = sorted(primary_subject_ids - known_entity_ids)
    if unknown_primary_subject_ids:
        raise ValueError(
            "spec primary_subject_ids names entities absent from the current state: "
            + ", ".join(unknown_primary_subject_ids)
        )
    developments = accepted_quarter_developments(quarter_document)
    accepted_quarter_end = parse_iso_date(
        quarter_plan_body(quarter_document).get("period_end"),
        "accepted quarter plan period_end",
    )
    covered = {str(value) for value in checkpoint.get("covered_quarter_event_ids") or []}
    by_id: dict[str, dict[str, Any]] = {}
    for row in developments:
        development_id = str(row.get("id") or "")
        if not development_id or development_id in by_id:
            raise ValueError("accepted quarter developments need unique non-empty IDs")
        by_id[development_id] = row

    missed = sorted(
        development_id
        for development_id, row in by_id.items()
        if development_completion(row) < start and development_id not in covered
    )
    if missed:
        raise ValueError(
            "checkpoint is missing quarter developments due before this window: "
            + ", ".join(missed)
        )

    required = [
        row
        for development_id, row in by_id.items()
        if development_id not in covered
        and start <= development_completion(row) <= end
    ]
    relevant = [
        row
        for development_id, row in by_id.items()
        if development_id not in covered
        and (
            development_start(row) <= end
            and development_completion(row) >= start
        )
    ]
    later_accepted_developments = [
        copy.deepcopy(row)
        for row in developments
        if end < development_start(row) <= accepted_quarter_end
    ]
    current_fact_ids = set(current_existing_fact_ids(state["facts"]))
    relevant = resolve_current_fact_references_for_model(
        relevant,
        state["facts"],
        current_fact_ids=current_fact_ids,
    )
    relevant_ids = {str(row["id"]) for row in relevant}
    required_development_ids = {str(row["id"]) for row in required}
    candidate_entities = new_entity_candidates_for_week(
        quarter_plan_body(quarter_document).get("new_entities") or [],
        relevant_developments=relevant,
        current_entities=state["entities"],
    )
    required_facts = required_fact_declarations(required, state["facts"])

    quarter_start = parse_iso_date(
        quarter_plan_body(quarter_document).get("period_start"),
        "accepted quarter plan period_start",
    )
    planner_chronology = accepted_contact_chronology_for_planner(
        state, start, quarter_start=quarter_start
    )
    chronology_subject_ids = {
        str(subject)
        for row in planner_chronology
        for subject in row.get("subject_ids") or []
        if nonempty_string(subject)
    }
    recent_context_start = start - timedelta(weeks=8)
    recent_subject_ids = {
        str(subject)
        for row in planner_chronology
        if parse_iso_date(row["date"], "recent contact date")
        >= recent_context_start
        for subject in row.get("subject_ids") or []
        if nonempty_string(subject)
    }
    current_ordinary_states = current_ordinary_states_from_session_purposes(state)
    ordinary_state_subject_ids = {
        str(subject)
        for ordinary_state in current_ordinary_states
        for subject in ordinary_state["subject_ids"]
        if nonempty_string(subject)
    }
    allowed_tools = window_tools(tools)
    planner_app_records = compact_planner_app_records(
        allowed_tools, state["app_state"], start=start, end=end
    )
    app_record_subject_ids = entity_ids_named_in_app_records(
        state["entities"], planner_app_records
    )
    planner_subject_ids = {
        *recent_subject_ids,
        *ordinary_state_subject_ids,
        *app_record_subject_ids,
        *(
            str(entity.get("id"))
            for entity in candidate_entities
            if isinstance(entity, dict) and nonempty_string(entity.get("id"))
        ),
        *(
            str(subject)
            for development in relevant
            for subject in development.get("subjects") or []
            if nonempty_string(subject)
        ),
        *(
            str(subject)
            for thread in state.get("unfinished_threads") or []
            if isinstance(thread, dict)
            for subject in thread.get("subjects") or []
            if nonempty_string(subject)
        ),
    }
    # Call 1 decides what happens. It needs current constraints for the concrete
    # non-primary subjects already selected by accepted context.
    planner_facts = planner_current_facts(
        state["facts"],
        quarter_developments=relevant,
        recent_chronology=planner_chronology,
        primary_subject_ids=primary_subject_ids,
        selected_subject_ids=planner_subject_ids,
        unfinished_threads=state.get("unfinished_threads") or [],
    )
    # Only entities materialized into accepted state are available to the planner.
    # Future quarter-plan entities remain unavailable until they are introduced.
    planner_entities = planner_known_entities(
        state["entities"],
        quarter_developments=relevant,
        current_facts=planner_facts,
        relevant_developments=relevant,
        unfinished_threads=state.get("unfinished_threads") or [],
        required_entities=candidate_entities,
        primary_subject_ids=primary_subject_ids,
        recent_subject_ids=recent_subject_ids | ordinary_state_subject_ids,
        app_record_subject_ids=app_record_subject_ids,
    )
    required_development_ids = {str(row["id"]) for row in required}
    existing_continues_from_ids = valid_existing_continues_from_ids(
        state,
        chronology=[],
        recent_exact_messages=[],
    )
    unfinished_threads = copy.deepcopy(state.get("unfinished_threads") or [])
    current_fact_ids_by_thread = unfinished_thread_current_fact_ids(
        state["facts"],
        recent_chronology=planner_chronology,
        unfinished_threads=unfinished_threads,
    )
    for index, thread in enumerate(unfinished_threads):
        thread_id = (
            str(thread["thread_id"])
            if nonempty_string(thread.get("thread_id"))
            else f"__unfinished_thread_{index}"
        )
        thread["current_fact_ids"] = current_fact_ids_by_thread.get(thread_id, [])
    prior_accepted_contacts_by_thread = prior_accepted_contacts_for_open_threads(
        state,
        start=start,
        unfinished_threads=unfinished_threads,
    )
    earlier_user_messages_by_thread = {
        str(thread["thread_id"]): bounded_same_matter_user_messages(
            state, thread_id=str(thread["thread_id"])
        )
        for thread in unfinished_threads
        if isinstance(thread, dict) and nonempty_string(thread.get("thread_id"))
    }
    current_ordinary_states_by_thread = current_ordinary_states_for_open_threads(
        state,
        unfinished_threads=unfinished_threads,
        current_ordinary_states=current_ordinary_states,
    )
    model_open_threads = planner_open_threads(
        unfinished_threads,
        prior_accepted_contacts_by_thread=prior_accepted_contacts_by_thread,
        earlier_user_messages_by_thread=earlier_user_messages_by_thread,
        current_ordinary_states_by_thread=current_ordinary_states_by_thread,
    )

    compact_planner_facts = compact_current_facts_for_model(
        planner_facts
    )
    current_fact_statements = {
        fact_reference(fact.get("id")): str(fact.get("statement"))
        for fact in compact_planner_facts
        if fact_reference(fact.get("id")) is not None
        and not fact.get("available_only_for_supersession")
        and nonempty_string(fact.get("statement"))
    }

    model_developments: list[dict[str, Any]] = []
    for development in relevant:
        model_development = copy.deepcopy(development)
        development_id = str(development.get("id") or "")
        model_development["facts_to_establish"] = (
            [
                model_fact_fields(required_facts.get(str(fact.get("key")), fact))
                for fact in development.get("facts_to_establish") or []
                if isinstance(fact, dict)
            ]
            if development_id in required_development_ids
            else []
        )
        model_developments.append(model_development)

    operations_by_session_id: dict[str, list[dict[str, Any]]] = {}
    for row in state.get("operation_results") or []:
        if not isinstance(row, dict) or not nonempty_string(row.get("session_id")):
            continue
        operation = row.get("operation")
        if not isinstance(operation, dict):
            continue
        tool = operation.get("tool")
        args = operation.get("args")
        if not nonempty_string(tool) or not isinstance(args, dict):
            continue
        operations_by_session_id.setdefault(str(row["session_id"]), []).append(
            {"tool": str(tool), "args": copy.deepcopy(args)}
        )

    detailed_history_start = start - timedelta(days=14)
    older_contacts_that_already_happened = [
        {
            "session_id": str(row.get("session_id") or ""),
            "date": str(row["date"]),
            "thread_id": str(row.get("thread_id") or ""),
            "subject_ids": copy.deepcopy(row.get("subject_ids") or []),
            "contact_purpose": compact_older_contact_summary(row),
            "accepted_app_operation_identities": (
                compact_older_contact_operation_identities(
                    operations_by_session_id.get(str(row.get("session_id") or ""), [])
                )
            ),
        }
        for row in planner_chronology
        if parse_iso_date(row["date"], "older contact date")
        < detailed_history_start
    ]

    recent_contacts = [
        {
            "session_id": str(row.get("session_id") or ""),
            "date": str(row["date"]),
            "finished_topic": readable_finished_topic(row),
            "accepted_contact_details": str(
                row.get("accepted_contact_details") or ""
            ),
            "lasting_changes": [
                current_fact_statements[fact_reference(fact_id)]
                for fact_id in row.get("fact_ids") or []
                if fact_reference(fact_id) in current_fact_statements
            ],
            "subject_ids": copy.deepcopy(row.get("subject_ids") or []),
            "accepted_app_operations": copy.deepcopy(
                operations_by_session_id.get(str(row.get("session_id") or ""), [])
            ),
        }
        for row in planner_chronology
        if parse_iso_date(row["date"], "recent contact date")
        >= detailed_history_start
    ]

    protected_candidates = []
    for change in accepted_protected_changes(quarter_document):
        subject_ids = [
            str(subject)
            for subject in change.get("subject_ids") or []
            if nonempty_string(subject)
        ]
        if not subject_ids:
            continue
        try:
            not_before = parse_iso_date(change.get("not_before"), "protected change not_before")
        except ValueError:
            continue
        if not_before <= end:
            continue
        protected_candidates.append(
            {
                "id": str(change.get("id") or ""),
                "subject_ids": subject_ids,
                "not_before": not_before.isoformat(),
                "description": str(change.get("description") or ""),
            }
        )
    protected_changes = sorted(
        protected_candidates,
        key=lambda row: (str(row["not_before"]), str(row["id"])),
    )

    payload = {
        "persona": str(config["persona"]),
        "requested_dates": {"start": start.isoformat(), "end": end.isoformat()},
        "narrative_timezone": str(
            spec["narrative_timezone"]
        ),
        "world_description": resolve_path(config["generator_context"]).read_text(),
        "developments_for_this_week": [
            {
                **copy.deepcopy(development),
                "must_finish_in_this_window": str(development["id"])
                in required_development_ids,
            }
            for development in model_developments
        ],
        "accepted_developments_after_this_window": later_accepted_developments,
        "protected_changes": protected_changes,
        "current_facts": compact_planner_facts,
        # Keep the full set out of the story call and give it to the exact planner.
        "_all_current_facts": compact_current_facts_for_model(state["facts"]),
        "current_ordinary_states": current_ordinary_states,
        "known_entities": planner_entities,
        "available_simulated_actions": compact_tool_capabilities(
            allowed_tools, planner_app_records
        ),
        "available_app_records": planner_app_records,
        # Kept for deterministic validation/replay; never copied into the story
        # request or the exact planner unless a fixed contact names the record.
        "_exact_app_records": readable_app_state(allowed_tools, state["app_state"]),
        "earlier_accepted_app_items": accepted_app_items_by_contact(
            state, accepted_contacts=planner_chronology
        ),
        "accepted_contact_chronology": copy.deepcopy(planner_chronology),
        "older_contacts_that_already_happened": (
            older_contacts_that_already_happened
        ),
        "recently_finished_contacts": recent_contacts,
        "open_threads": model_open_threads,
        "valid_existing_continues_from_ids": existing_continues_from_ids,
        "previously_used_communication_destinations": (
            accepted_communication_destinations(state)
        ),
    }
    payload["new_entities_that_may_first_appear_this_week"] = copy.deepcopy(
        candidate_entities
    )
    payload["lasting_facts_that_must_be_established"] = copy.deepcopy(
        list(required_facts.values())
    )
    validation_context = {
        "required_development_ids": required_development_ids,
        "allowed_development_ids": relevant_ids,
        "candidate_entities": candidate_entities,
        "required_facts": required_facts,
        "primary_subject_ids": primary_subject_ids,
        "self_contained_basis_ids": {ORDINARY_CURRENT_LIFE_BASIS_ID},
        "protected_change_ids": {
            str(row.get("id"))
            for row in protected_changes
            if nonempty_string(row.get("id"))
        },
        "open_thread_ids": {
            str(row.get("thread_id"))
            for row in unfinished_threads
            if isinstance(row, dict) and nonempty_string(row.get("thread_id"))
        },
        "unfinished_thread_ids_by_reference": {
            str(reference): str(thread["thread_id"])
            for thread in unfinished_threads
            if isinstance(thread, dict) and nonempty_string(thread.get("thread_id"))
            for field in ("interaction_id", "session_id")
            for reference in [thread.get(field)]
            if nonempty_string(reference)
        },
        "open_thread_current_states_by_id": current_ordinary_states_by_thread,
        "development_start_dates": {
            str(development["id"]): development_start(development)
            for development in relevant
            if nonempty_string(development.get("id"))
        },
        "facts_by_development": {
            str(development.get("id")): [
                model_fact_fields(required_facts.get(str(fact.get("key")), fact))
                for fact in development.get("facts_to_establish") or []
                if isinstance(fact, dict)
            ]
            for development in required
            if nonempty_string(development.get("id"))
        },
        "subjects_by_basis": {
            **{
                str(development["id"]): {
                    str(subject)
                    for subject in [
                        *(development.get("subjects") or []),
                        *(
                            fact_subject
                            for fact in development.get("facts_to_establish") or []
                            if isinstance(fact, dict)
                            for fact_subject in fact.get("subjects") or []
                        ),
                    ]
                    if nonempty_string(subject)
                }
                for development in relevant
                if nonempty_string(development.get("id"))
            },
            ORDINARY_CURRENT_LIFE_BASIS_ID: {
                *chronology_subject_ids,
                *(
                    str(entity["id"])
                    for entity in planner_entities
                    if nonempty_string(entity.get("id"))
                ),
            },
            **{
                str(thread["thread_id"]): {
                    str(subject)
                    for subject in thread.get("subjects") or []
                    if nonempty_string(subject)
                }
                for thread in unfinished_threads
                if isinstance(thread, dict) and nonempty_string(thread.get("thread_id"))
            },
        },
    }
    return payload, validation_context


def story_existing_app_items(
    available_app_records: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, str]]]:
    """Describe app-item identity without exposing IDs or body text."""
    items: dict[str, list[dict[str, str]]] = {}
    for record_kind, records in available_app_records.items():
        if record_kind == "calendar" or not isinstance(records, list):
            continue
        rows: list[dict[str, str]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            label = next(
                (
                    str(record[field])
                    for field in ("title", "name", "subject", "key", "asof_date")
                    if nonempty_string(record.get(field))
                ),
                "",
            )
            if not label and record_kind == "prs" and nonempty_string(record.get("id")):
                label = str(record["id"])
            if not label:
                continue

            details: list[str] = []
            for field in ("service", "owner", "status", "repo", "author", "account"):
                if nonempty_string(record.get(field)):
                    details.append(f"{field}: {str(record[field]).strip()}")
            row = {"name": label, "details": "; ".join(details)}
            if row not in rows:
                rows.append(row)
        if rows:
            items[str(record_kind)] = rows
    return items


def build_story_payload(planner_payload: dict[str, Any]) -> dict[str, Any]:
    """Build the limited story-planner input from the richer planner payload."""
    developments = []
    developments_finishing_later = []
    for row in planner_payload["developments_for_this_week"]:
        if row.get("must_finish_in_this_window", True):
            developments.append(
                {
                    key: copy.deepcopy(value)
                    for key, value in row.items()
                    if key not in {"facts_to_establish", "relevant_existing_fact_ids"}
                }
            )
            continue
        developments_finishing_later.append(
            {
                key: copy.deepcopy(row.get(key))
                for key in (
                    "id",
                    "start_date",
                    "end_date",
                    "subjects",
                    "what_happens",
                    "lasting_outcome",
                )
            }
        )
    current_conditions = [
        {
            "statement": copy.deepcopy(row.get("statement")),
            "applies_when": copy.deepcopy(row.get("applies_when")),
            "subjects": copy.deepcopy(row.get("subjects") or []),
        }
        for row in reversed(planner_payload["current_facts"])
        if isinstance(row, dict) and nonempty_string(row.get("statement"))
    ]
    older_contacts = [
        {
            key: copy.deepcopy(value)
            for key, value in row.items()
            if key != "accepted_app_operation_identities"
        }
        for row in planner_payload["older_contacts_that_already_happened"]
    ]
    recent_contacts = [
        {
            key: copy.deepcopy(value)
            for key, value in row.items()
            if key not in {"accepted_app_operations", "lasting_changes"}
        }
        for row in planner_payload["recently_finished_contacts"]
    ]
    open_threads = [
        {
            key: copy.deepcopy(row.get(key))
            for key in (
                "basis_id",
                "continues_from_id",
                "subject_ids",
                "what_already_happened",
                "what_remains",
            )
        }
        for row in planner_payload["open_threads"]
        if isinstance(row, dict)
    ]
    current_ordinary_states = [
        {
            key: copy.deepcopy(row.get(key))
            for key in ("key", "subject_ids", "statement", "date")
        }
        for row in planner_payload["current_ordinary_states"]
        if isinstance(row, dict) and nonempty_string(row.get("statement"))
    ]
    current_ordinary_states.sort(
        key=lambda row: str(row.get("date") or ""), reverse=True
    )
    calendar_events = [
        {
            "id": copy.deepcopy(row.get("id") or row.get("event_id")),
            "title": copy.deepcopy(row.get("title")),
            "start": copy.deepcopy(row.get("start")),
            "end": copy.deepcopy(row.get("end")),
            "attendees": copy.deepcopy(row.get("attendees") or []),
        }
        for row in planner_payload["available_app_records"].get("calendar") or []
        if isinstance(row, dict)
    ]
    calendar_events.sort(key=lambda row: (str(row["start"] or ""), str(row["id"] or "")))
    return {
        "persona": planner_payload["persona"],
        "requested_dates": copy.deepcopy(planner_payload["requested_dates"]),
        "narrative_timezone": planner_payload["narrative_timezone"],
        "current_status_precedence": (
            "The current conditions and latest continuing states below are the "
            "newest applicable status of recurring named subjects. Read them before "
            "the identity and history sections. Do not move completed or live work "
            "backward to an earlier stage unless a supplied new event explicitly "
            "reopens it."
        ),
        "current_conditions": current_conditions,
        "current_ordinary_states": current_ordinary_states,
        "open_threads": open_threads,
        "developments_for_this_week": developments,
        "developments_finishing_later": developments_finishing_later,
        "accepted_developments_after_this_window": copy.deepcopy(
            planner_payload["accepted_developments_after_this_window"]
        ),
        "protected_changes": copy.deepcopy(planner_payload["protected_changes"]),
        "historical_identity_and_world_context": {
            "how_to_read": (
                "This describes identity and history, not current status. It does "
                "not override the current status above."
            ),
            "description": planner_payload["world_description"],
        },
        "historical_entity_identity_and_history": {
            "how_to_read": (
                "These descriptions identify subjects and summarize their history. "
                "They are not current status and do not override the current status "
                "above."
            ),
            "subjects": copy.deepcopy(planner_payload["known_entities"]),
        },
        "new_entities_that_may_first_appear_this_week": copy.deepcopy(
            planner_payload["new_entities_that_may_first_appear_this_week"]
        ),
        "supported_external_actions": story_action_categories(
            planner_payload["available_simulated_actions"]
        ),
        "existing_app_items": story_existing_app_items(
            planner_payload["available_app_records"]
        ),
        "upcoming_calendar_events": calendar_events,
        "older_contacts_that_already_happened": older_contacts,
        "recently_finished_contacts": recent_contacts,
    }


def story_action_categories(actions: list[dict[str, Any]]) -> list[str]:
    """Describe feasible outside actions without exposing tools or record schemas."""
    names = {
        str(row.get("tool_name") or "")
        for row in actions
        if isinstance(row, dict) and nonempty_string(row.get("tool_name"))
    }
    categories: list[str] = []
    if "send_email" in names:
        categories.append(
            "send a text-only email with a subject and body; email cannot include "
            "attachments or files"
        )
    if "send_slack_dm" in names:
        categories.append("send a private Slack direct message to one user")
    if "send_slack_message" in names:
        categories.append("post a message to a Slack channel")
    if "send_sms" in names:
        categories.append("send a text message by SMS")
    if "list_calendar_events" in names:
        categories.append("read scheduled calendar events")
    if names & {"create_calendar_event", "update_calendar_event"}:
        categories.append(
            "create or update a calendar event after stating its "
            "exact date, start time, and end time; for a repeating series, state "
            "the first date, last date, start time, and duration; these calendar "
            "actions do not create a video room or meeting link"
        )
    if names & {
        "create_doc",
        "update_doc",
        "post_doc_comment",
        "create_runbook_entry",
        "update_runbook_entry",
        "create_growth_brief",
        "update_growth_brief",
    }:
        categories.append("create or update supported documents, notes, comments, runbooks, or briefs")
    readable_sources = {
        "list_inbox": "read messages already present in the inbox",
        "list_oncall_schedule": "read the current on-call schedule",
        "get_runbook": "read an existing runbook by its supplied ID",
        "list_feature_flags": "read existing feature flags",
        "get_feature_flag": "read one existing feature flag by its supplied key",
        "query_event_funnel": "read a saved event-funnel result for a named funnel",
        "query_retention_cohort": "read a saved retention-cohort result for a named cohort",
        "query_user_path": "read a saved user-path result for named start and end events",
        "list_subscriptions": "read existing subscriptions",
        "get_subscription": "read one existing subscription by its supplied ID",
        "list_stripe_invoices": "read existing Stripe invoices",
        "get_customer": "read one existing customer by its supplied ID",
        "list_recent_churn": "read the recorded recent churn list",
        "get_mrr_metrics": "read a saved MRR metrics snapshot",
        "get_churn_metrics": "read a saved churn metrics snapshot",
        "get_arr_segments": "read a saved ARR segment snapshot",
        "query_experiment_results": "read saved results for a named experiment",
        "check_project_status": "read the status of a named project",
        "get_stock_data": "read current stock data for a named ticker",
        "list_invoices": "read existing invoices",
    }
    for tool_name in sorted(names):
        if tool_name in readable_sources:
            categories.append(readable_sources[tool_name])
    if names & {"get_pr", "list_open_prs", "post_pr_comment", "review_pr", "merge_pr"}:
        categories.append("read, comment on, review, or merge pull requests")
    if names & {"deploy_service", "rollback_deploy", "acknowledge_incident", "escalate_incident"}:
        categories.append("perform the supported deployment and incident-response actions")
    if names & {"create_experiment", "update_experiment", "update_feature_flag"}:
        categories.append("create or update supported experiments and feature flags")
    return categories


def add_code_owned_story_fields(story_response: dict[str, Any]) -> dict[str, Any]:
    """Derive internal validation fields from the story's developments and threads."""
    normalized = copy.deepcopy(story_response)
    occurrences = normalized.get("plan", {}).get("occurrences")
    if not isinstance(occurrences, list):
        return normalized
    for occurrence in occurrences:
        if not isinstance(occurrence, dict):
            continue
        development_ids = [
            str(value) for value in occurrence.get("development_ids") or []
        ]
        continues_from = [
            str(value) for value in occurrence.get("continues_from") or []
        ]
        thread_id = str(occurrence.get("thread_id") or "")
        if development_ids:
            occurrence["effect_kind"] = "realize_development"
            occurrence["basis_ids"] = list(development_ids)
            if continues_from and thread_id and thread_id not in occurrence["basis_ids"]:
                occurrence["basis_ids"].append(thread_id)
        elif continues_from:
            occurrence["effect_kind"] = "advance_open_thread"
            occurrence["basis_ids"] = [thread_id] if thread_id else []
        else:
            occurrence["effect_kind"] = "self_contained"
            occurrence["basis_ids"] = [ORDINARY_CURRENT_LIFE_BASIS_ID]
    return normalized


def story_contact_count_errors(
    response: dict[str, Any], *, start: date, end: date
) -> list[str]:
    """Enforce the accepted contact range only for complete seven-day windows."""
    if (end - start).days != 6:
        return []
    occurrences = response.get("plan", {}).get("occurrences")
    if not isinstance(occurrences, list):
        return []
    count = len(occurrences)
    if 23 <= count <= 24:
        return []
    return [
        f"a complete seven-day week must contain 23 to 24 assistant contacts; got {count}"
    ]


def assign_story_contact_ids(
    story_response: dict[str, Any],
    *,
    persona: str,
    timezone_name: str,
) -> dict[str, Any]:
    """Replace response-local contact labels with stable, unique IDs."""
    assigned = copy.deepcopy(story_response)
    occurrences = assigned.get("plan", {}).get("occurrences")
    if not isinstance(occurrences, list):
        return assigned

    local_labels = [str(row.get("contact_id") or "") for row in occurrences]
    if any(not label for label in local_labels):
        raise ValueError("every story contact must have a nonempty contact_id")
    if len(local_labels) != len(set(local_labels)):
        raise ValueError("story contact_id labels must be unique within the response")

    zone = ZoneInfo(timezone_name)
    next_number_by_date: dict[str, int] = {}
    earlier_ids: dict[str, str] = {}
    for occurrence, local_label in zip(occurrences, local_labels):
        when = datetime.fromisoformat(str(occurrence.get("narrative_date") or ""))
        if when.tzinfo is None:
            raise ValueError("story narrative_date must include a UTC offset")
        local_date = when.astimezone(zone).date().strftime("%Y_%m_%d")
        number = next_number_by_date.get(local_date, 1)
        contact_id = f"{persona}_{local_date}_{number:03d}"
        next_number_by_date[local_date] = number + 1

        for field in ("continues_from", "uses_items_created_by_contact_ids"):
            values = occurrence.get(field)
            if isinstance(values, list):
                occurrence[field] = [
                    earlier_ids.get(str(value), str(value)) for value in values
                ]
        occurrence["contact_id"] = contact_id
        earlier_ids[local_label] = contact_id
    return assigned


def build_enrichment_payload(
    planner_payload: dict[str, Any], story_response: dict[str, Any],
    *,
    other_fixed_contacts_this_week: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Add all current facts and exact app data to the fixed weekly story."""
    fixed_contacts = copy.deepcopy(story_response["plan"]["occurrences"])
    persona_id = str(planner_payload["persona"])
    selected_subjects = {
        str(subject)
        for contact in fixed_contacts
        for subject in contact.get("subject_ids") or []
        if str(subject) != persona_id
    }
    shared_prior_contacts = accepted_contacts_with_shared_subject_ids(
        planner_payload.get("accepted_contact_chronology") or [], selected_subjects
    )
    all_current_facts = planner_payload.get("_all_current_facts")
    if all_current_facts is None:
        all_current_facts = planner_payload["current_facts"]
    selected_entities = [
        copy.deepcopy(entity)
        for entity in planner_payload["known_entities"]
        if str(entity.get("id") or "") in selected_subjects
    ]
    used_threads = {
        str(contact.get("thread_id") or "")
        for contact in fixed_contacts
        if nonempty_string(contact.get("thread_id"))
    }
    exact_records = _exact_app_records_for_contacts(planner_payload, fixed_contacts)
    available_records = copy.deepcopy(planner_payload["available_app_records"])
    for app_name, rows in exact_records.items():
        index_rows = available_records.get(app_name) or []
        for full_row in rows:
            matched_index: int | None = None
            for field in ("id", "plan_id", "record_key"):
                if not nonempty_string(full_row.get(field)):
                    continue
                matched_index = next(
                    (
                        index
                        for index, index_row in enumerate(index_rows)
                        if str(index_row.get(field) or "") == str(full_row[field])
                    ),
                    None,
                )
                if matched_index is not None:
                    break
            if matched_index is None:
                descriptive_fields = ("name", "title", "subject", "key", "asof_date")
                candidates = [
                    index
                    for index, index_row in enumerate(index_rows)
                    if any(
                        nonempty_string(full_row.get(field))
                        and str(index_row.get(field) or "") == str(full_row[field])
                        for field in descriptive_fields
                    )
                ]
                if len(candidates) == 1:
                    matched_index = candidates[0]
            if matched_index is not None:
                index_rows[matched_index] = copy.deepcopy(full_row)
        available_records[app_name] = index_rows
    payload = {
        "persona": planner_payload["persona"],
        "requested_dates": copy.deepcopy(planner_payload["requested_dates"]),
        "fixed_contacts": fixed_contacts,
        "developments_for_this_week": copy.deepcopy(
            planner_payload.get("developments_for_this_week") or []
        ),
        "accepted_developments_after_this_window": copy.deepcopy(
            planner_payload.get("accepted_developments_after_this_window") or []
        ),
        "previous_accepted_contacts_with_shared_subject_ids": shared_prior_contacts,
        "current_facts": copy.deepcopy(all_current_facts),
        "lasting_facts_that_must_be_established": copy.deepcopy(
            planner_payload["lasting_facts_that_must_be_established"]
        ),
        "current_ordinary_states": copy.deepcopy(
            planner_payload["current_ordinary_states"]
        ),
        "known_entities": selected_entities,
        "open_threads": [
            copy.deepcopy(row)
            for row in planner_payload["open_threads"]
            if str(row.get("thread_id") or row.get("basis_id") or "") in used_threads
        ],
        "available_simulated_actions": copy.deepcopy(
            planner_payload["available_simulated_actions"]
        ),
        "available_app_records": available_records,
        "existing_app_item_update_rule": (
            "When a fixed contact asks to update an item listed in "
            "earlier_accepted_app_items, use the supplied existing record ID with "
            "the matching update action. Do not create a replacement item unless "
            "the fixed contact explicitly asks for a new item."
        ),
        "earlier_accepted_app_items": copy.deepcopy(
            planner_payload.get("earlier_accepted_app_items") or []
        ),
        "previously_used_communication_destinations": copy.deepcopy(
            planner_payload["previously_used_communication_destinations"]
        ),
    }
    if other_fixed_contacts_this_week:
        payload["other_fixed_contacts_this_week"] = copy.deepcopy(
            other_fixed_contacts_this_week
        )
    return payload


def accepted_contacts_with_shared_subject_ids(
    chronology: list[dict[str, Any]], selected_subjects: set[str]
) -> list[dict[str, Any]]:
    """Keep complete accepted contacts that share a fixed contact subject."""
    if not selected_subjects:
        return []
    return [
        copy.deepcopy(row)
        for row in chronology
        if isinstance(row, dict)
        and selected_subjects.intersection(
            str(subject) for subject in row.get("subject_ids") or []
        )
    ]


def correction_evidence_for_contact(
    planner_payload: dict[str, Any], contact: dict[str, Any]
) -> dict[str, Any]:
    """Return accepted outcome and history evidence for one corrected contact."""
    return {
        "developments_for_this_week": copy.deepcopy(
            planner_payload.get("developments_for_this_week") or []
        ),
        "accepted_developments_after_this_window": copy.deepcopy(
            planner_payload.get("accepted_developments_after_this_window") or []
        ),
        "previous_accepted_contacts_with_shared_subject_ids": (
            accepted_contacts_with_shared_subject_ids(
                planner_payload.get("accepted_contact_chronology") or [],
                {
                    str(subject)
                    for subject in contact.get("subject_ids") or []
                    if str(subject) != str(planner_payload["persona"])
                },
            )
        ),
    }


def merge_story_and_enrichment(
    story_response: dict[str, Any], enrichment_response: dict[str, Any]
) -> dict[str, Any]:
    """Merge enrichment by contact ID while enforcing the fixed story contact list."""
    occurrences = story_response.get("plan", {}).get("occurrences")
    additions = enrichment_response.get("contacts")
    if not isinstance(occurrences, list) or not isinstance(additions, list):
        raise ValueError("story occurrences and enrichment contacts must be lists")
    story_ids = [str(row.get("contact_id") or "") for row in occurrences]
    enrichment_ids = [str(row.get("contact_id") or "") for row in additions]
    if len(set(story_ids)) != len(story_ids) or any(not value for value in story_ids):
        raise ValueError("story contact IDs must be non-empty and unique")
    if enrichment_ids != story_ids:
        raise ValueError(
            "enrichment must contain every fixed contact exactly once in the same order"
        )

    merged_occurrences = []
    for fixed, addition in zip(occurrences, additions):
        merged = copy.deepcopy(fixed)
        for key in (
            "uses_current_fact_ids",
            "uses_new_fact_keys",
            "durable_facts",
            "current_state_changes",
            "app_operations",
        ):
            merged[key] = copy.deepcopy(addition[key])
        merged_occurrences.append(merged)
    return {"plan": {"occurrences": merged_occurrences}}


def planned_operation_record_problem(
    operation: dict[str, Any],
    *,
    available_app_records: dict[str, list[dict[str, Any]]],
    tools: dict[str, dict[str, Any]],
) -> str | None:
    """Explain when a read operation names an app record that does not exist."""
    tool_name = str(operation.get("tool") or "")
    state_effect = (tools.get(tool_name) or {}).get("state_effect") or {}
    requirement = state_effect.get("required_existing_record")
    if not isinstance(requirement, dict):
        return None
    records_key = str(requirement.get("app_records_key") or "")
    argument_fields = requirement.get("argument_fields")
    defaults = requirement.get("argument_defaults") or {}
    if not records_key or not isinstance(argument_fields, dict):
        raise ValueError(
            f"tool {tool_name!r} has an invalid required_existing_record contract"
        )
    raw_args = operation.get("args_json")
    if not isinstance(raw_args, str):
        return None
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError:
        return None
    if not isinstance(args, dict):
        return None

    expected_fields: dict[str, Any] = {}
    for argument_name, record_field in argument_fields.items():
        if argument_name in args and args[argument_name] is not None:
            expected_fields[str(record_field)] = args[argument_name]
        elif argument_name in defaults:
            expected_fields[str(record_field)] = defaults[argument_name]
    records = available_app_records.get(records_key) or []
    if any(
        isinstance(record, dict)
        and all(record.get(field) == value for field, value in expected_fields.items())
        for record in records
    ):
        return None
    if expected_fields:
        requested = ", ".join(
            f"{field}={value!r}" for field, value in expected_fields.items()
        )
        return (
            f"The {tool_name} action cannot find an existing record with {requested} "
            f"in available_app_records.{records_key}."
        )
    return (
        f"The {tool_name} action cannot find any existing record in "
        f"available_app_records.{records_key}."
    )


def planner_execution_problems(
    story_response: dict[str, Any],
    enrichment_response: dict[str, Any],
    *,
    available_app_records: dict[str, list[dict[str, Any]]] | None = None,
    tools: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Return external actions the exact planner could not execute."""
    occurrences = story_response.get("plan", {}).get("occurrences")
    additions = enrichment_response.get("contacts")
    if not isinstance(occurrences, list) or not isinstance(additions, list):
        raise ValueError("story occurrences and enrichment contacts must be lists")
    story_ids = [str(row.get("contact_id") or "") for row in occurrences]
    enrichment_ids = [str(row.get("contact_id") or "") for row in additions]
    if enrichment_ids != story_ids:
        raise ValueError(
            "enrichment must contain every fixed contact exactly once in the same order"
        )

    problems: list[dict[str, str]] = []
    for occurrence, addition in zip(occurrences, additions):
        contact_id = str(occurrence.get("contact_id") or "")
        outcome = occurrence.get("assistant_outcome")
        kind = str(outcome.get("kind") or "") if isinstance(outcome, dict) else ""
        operations = addition.get("app_operations")
        if not isinstance(operations, list):
            raise ValueError(f"planner contact {contact_id!r} app_operations must be a list")
        raw_problem = addition.get("execution_problem")
        problem = str(raw_problem or "").strip()
        factual_problem = str(addition.get("factual_problem") or "").strip()

        if factual_problem:
            problems.append(
                {
                    "contact_id": contact_id,
                    "reason": factual_problem,
                    "problem_kind": "factual",
                }
            )
            continue

        if kind == "external_action" and not operations:
            problems.append(
                {
                    "contact_id": contact_id,
                    "reason": problem
                    or "The requested external action could not be translated into a supported app operation.",
                }
            )
            continue
        record_problem = next(
            (
                found_problem
                for operation in operations
                if (
                    found_problem := planned_operation_record_problem(
                        operation,
                        available_app_records=available_app_records or {},
                        tools=tools or {},
                    )
                )
            ),
            None,
        )
        if kind == "external_action" and record_problem:
            problems.append({"contact_id": contact_id, "reason": record_problem})
            continue
        if problem:
            raise ValueError(
                f"planner contact {contact_id!r} reported an execution problem "
                "even though it did not return an unexecutable external action"
            )
    return problems


def apply_story_contact_corrections(
    story_response: dict[str, Any],
    correction_response: dict[str, Any],
    blocked_contact_ids: list[str],
    *,
    problem_kinds: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Apply only the corrected request text and outcome for blocked contacts."""
    expected_ids = [str(value) for value in blocked_contact_ids]
    if len(expected_ids) != len(set(expected_ids)) or any(
        not value for value in expected_ids
    ):
        raise ValueError("blocked contact IDs must be non-empty and unique")
    corrections = correction_response.get("contacts")
    if not isinstance(corrections, list):
        raise ValueError("contact correction response must contain a contacts list")
    correction_ids = [str(row.get("contact_id") or "") for row in corrections]
    if correction_ids != expected_ids:
        raise ValueError(
            "contact correction must contain every blocked contact exactly once in the same order"
        )

    corrected = copy.deepcopy(story_response)
    occurrences = corrected.get("plan", {}).get("occurrences")
    if not isinstance(occurrences, list):
        raise ValueError("story response must contain plan.occurrences")
    occurrence_by_id = {
        str(row.get("contact_id") or ""): row
        for row in occurrences
        if isinstance(row, dict)
    }
    if any(contact_id not in occurrence_by_id for contact_id in expected_ids):
        raise ValueError("contact correction names a contact absent from the story")

    for row in corrections:
        contact_id = str(row["contact_id"])
        occurrence_by_id[contact_id]["what_happened"] = copy.deepcopy(
            row["what_happened"]
        )
        occurrence_by_id[contact_id]["assistant_outcome"] = copy.deepcopy(
            row["assistant_outcome"]
        )
        if str((problem_kinds or {}).get(contact_id) or "") == "factual":
            if "source_material" not in row:
                raise ValueError(
                    f"factual correction for {contact_id} must include source_material"
                )
            occurrence_by_id[contact_id]["source_material"] = copy.deepcopy(
                row["source_material"]
            )
        elif "source_material" in row and row["source_material"] != occurrence_by_id[
            contact_id
        ].get("source_material"):
            raise ValueError(
                f"impossible-action correction for {contact_id} cannot change source_material"
            )
    return corrected


def replace_enrichment_contacts(
    enrichment_response: dict[str, Any],
    replacement_response: dict[str, Any],
    blocked_contact_ids: list[str],
) -> dict[str, Any]:
    """Replace only blocked enrichment rows while preserving full-week order."""
    expected_ids = [str(value) for value in blocked_contact_ids]
    if len(expected_ids) != len(set(expected_ids)) or any(
        not value for value in expected_ids
    ):
        raise ValueError("blocked contact IDs must be non-empty and unique")
    replacements = replacement_response.get("contacts")
    if not isinstance(replacements, list):
        raise ValueError("replacement enrichment must contain a contacts list")
    replacement_ids = [str(row.get("contact_id") or "") for row in replacements]
    if replacement_ids != expected_ids:
        raise ValueError(
            "replacement enrichment must contain every blocked contact exactly once in the same order"
        )

    normalized = copy.deepcopy(enrichment_response)
    contacts = normalized.get("contacts")
    if not isinstance(contacts, list):
        raise ValueError("enrichment response must contain a contacts list")
    indexes: dict[str, int] = {}
    for index, row in enumerate(contacts):
        contact_id = str(row.get("contact_id") or "")
        if not contact_id or contact_id in indexes:
            raise ValueError("enrichment contact IDs must be non-empty and unique")
        indexes[contact_id] = index
    if any(contact_id not in indexes for contact_id in expected_ids):
        raise ValueError("replacement enrichment names a contact absent from the full response")
    for replacement in replacements:
        contact_id = str(replacement["contact_id"])
        contacts[indexes[contact_id]] = copy.deepcopy(replacement)
    return normalized


def prior_same_week_operation_results_by_contact(
    occurrences: list[dict[str, Any]],
    operation_results: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Expose only earlier result values explicitly used by each contact."""
    timestamp_by_contact_id: dict[str, datetime] = {}
    for occurrence in occurrences:
        contact_id = str(occurrence.get("contact_id") or "")
        try:
            timestamp = datetime.fromisoformat(str(occurrence.get("narrative_date") or ""))
        except ValueError as error:
            raise ValueError(
                f"planned contact {contact_id!r} has an invalid narrative_date"
            ) from error
        if not contact_id or timestamp.tzinfo is None:
            raise ValueError(
                "planned contacts need non-empty IDs and offset-aware narrative dates"
            )
        timestamp_by_contact_id[contact_id] = timestamp

    results_by_operation: dict[tuple[str, int], dict[str, Any]] = {}
    for row in operation_results:
        if not isinstance(row, dict):
            raise ValueError("same-week operation result must be an object")
        source_contact_id = str(row.get("contact_id") or "")
        if source_contact_id not in timestamp_by_contact_id:
            raise ValueError(
                "same-week operation result names an unknown contact: "
                f"{source_contact_id!r}"
            )
        operation_index = row.get("operation_index")
        if isinstance(operation_index, bool) or not isinstance(operation_index, int):
            raise ValueError("same-week operation result needs an integer operation_index")
        results_by_operation[(source_contact_id, operation_index)] = row

    visible: dict[str, list[dict[str, Any]]] = {}
    for occurrence in occurrences:
        if not isinstance(occurrence, dict):
            continue
        contact_id = str(occurrence.get("contact_id") or "")
        target_timestamp = timestamp_by_contact_id[contact_id]
        selected: list[dict[str, Any]] = []
        for source_contact_id, operation_index, field in _direct_operation_result_references(
            occurrence.get("app_operations") or []
        ):
            source_timestamp = timestamp_by_contact_id.get(source_contact_id)
            if source_timestamp is None or source_timestamp >= target_timestamp:
                continue
            row = results_by_operation.get((source_contact_id, operation_index))
            result = row.get("result") if isinstance(row, dict) else None
            if not isinstance(result, dict) or field not in result:
                raise ValueError(
                    "planned contact references an earlier operation result that was not returned: "
                    f"{source_contact_id}[{operation_index}].{field}"
                )
            selected.append(
                {
                    "contact_id": source_contact_id,
                    "operation_index": operation_index,
                    "field": field,
                    "value": copy.deepcopy(result[field]),
                }
            )
        visible[contact_id] = selected
    return visible


def build_writer_payload(
    *,
    planner_response: dict[str, Any],
    config: dict[str, Any],
    state: dict[str, Any],
    start: date,
    end: date,
    timezone_name: str,
    required_entities: list[dict[str, Any]] | None = None,
    primary_subject_ids: set[str] | None = None,
    same_week_operation_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one self-contained writer input packet for every planned contact."""
    occurrence_plan = planner_response["plan"]
    occurrences = occurrence_plan["occurrences"]
    facts_by_contact = writer_facts_by_occurrence(
        occurrence_plan,
        state["facts"],
        accepted_history=state.get("history") or [],
    )
    prior_context_by_reference = {
        str(row["reference_id"]): row
        for row in exact_prior_context_for_occurrence_references(
            occurrence_plan, state
        )
        if isinstance(row, dict) and nonempty_string(row.get("reference_id"))
    }
    contacts: dict[str, dict[str, Any]] = {}
    prior_operation_results = prior_same_week_operation_results_by_contact(
        occurrences, same_week_operation_results or []
    )
    prior_occurrences = {
        str(occurrence.get("contact_id")): occurrence
        for occurrence in occurrences
        if isinstance(occurrence, dict) and nonempty_string(occurrence.get("contact_id"))
    }
    for occurrence in occurrences:
        if not isinstance(occurrence, dict) or not nonempty_string(
            occurrence.get("contact_id")
        ):
            raise ValueError("every writer occurrence needs a non-empty contact_id")
        contact_id = str(occurrence["contact_id"])
        local_facts = facts_by_contact.get(contact_id, [])
        local_entities = writer_known_entities_for_occurrence(
            state["entities"],
            occurrence=occurrence,
            current_facts=local_facts,
            required_entities=required_entities or [],
        )
        continued_contacts: list[dict[str, Any]] = []
        for reference in occurrence.get("continues_from") or []:
            row = prior_context_by_reference.get(str(reference))
            if isinstance(row, dict):
                continued_contacts.append(
                    {
                        "contact_id": str(reference),
                        "messages": copy.deepcopy(row.get("messages") or []),
                    }
                )
                continue
            prior_occurrence = prior_occurrences.get(str(reference))
            if isinstance(prior_occurrence, dict):
                continued_contacts.append(
                    {
                        "contact_id": str(reference),
                        "what_happened": copy.deepcopy(
                            prior_occurrence.get("what_happened")
                        ),
                        "requested_result": copy.deepcopy(
                            (prior_occurrence.get("assistant_outcome") or {}).get(
                                "requested_result"
                            )
                        ),
                    }
                )
                continue
            continued_contacts.append({"contact_id": str(reference)})
        outcome = occurrence.get("assistant_outcome") or {}
        thread_id = str(occurrence.get("thread_id") or "")
        earlier_items: list[dict[str, Any]] = []
        for source_contact_id in occurrence.get(
            "uses_items_created_by_contact_ids"
        ) or []:
            prior_occurrence = prior_occurrences.get(str(source_contact_id))
            if not isinstance(prior_occurrence, dict):
                continue
            prior_outcome = prior_occurrence.get("assistant_outcome") or {}
            earlier_items.append(
                {
                    "contact_id": str(source_contact_id),
                    "what_happened": copy.deepcopy(
                        prior_occurrence.get("what_happened")
                    ),
                    "requested_result": copy.deepcopy(
                        prior_outcome.get("requested_result")
                    ),
                    "app_operations": copy.deepcopy(
                        prior_occurrence.get("app_operations") or []
                    ),
                }
            )
        contacts[contact_id] = {
            "when": copy.deepcopy(occurrence.get("narrative_date")),
            "what_happened": copy.deepcopy(occurrence.get("what_happened")),
            "assistant_outcome_kind": copy.deepcopy(outcome.get("kind")),
            "requested_result": copy.deepcopy(outcome.get("requested_result")),
            "source_material": copy.deepcopy(occurrence.get("source_material")),
            "subjects": local_entities,
            "cited_facts": local_facts,
            "continues_from_contacts": continued_contacts,
            "earlier_user_messages_in_same_matter": (
                bounded_same_matter_user_messages(state, thread_id=thread_id)
                if occurrence.get("continues_from")
                else []
            ),
            "earlier_items_from_this_week": earlier_items,
            "fixed_app_operations": copy.deepcopy(occurrence.get("app_operations") or []),
            "referenced_operation_results": prior_operation_results[contact_id],
        }
    payload = {
        "persona": str(config["persona"]),
        "persona_description": resolve_path(config["generator_context"]).read_text(),
        "contacts": contacts,
    }
    return payload


def accepted_sessions_for_preceding_days(
    state: dict[str, Any], start: date, *, days: int
) -> list[dict[str, Any]]:
    """Return all accepted sessions in the bounded calendar days before start."""
    earliest = start - timedelta(days=days)
    sessions: list[dict[str, Any]] = []
    for session in state.get("history") or []:
        if not isinstance(session, dict):
            continue
        narrative_date = iso_date(session.get("narrative_date"))
        if narrative_date is not None and earliest <= narrative_date < start:
            sessions.append(copy.deepcopy(session))
    return sessions


def writer_current_facts_for_occurrences(
    occurrence_plan: dict[str, Any],
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only current facts explicitly selected by the planner."""
    current_facts_by_id = {
        fact_reference(fact.get("id")): fact
        for fact in facts_with_current(copy.deepcopy(facts))
        if isinstance(fact, dict)
        and fact.get("current")
        and fact_reference(fact.get("id")) is not None
    }
    available_fact_references: set[int | str] = set(current_facts_by_id)
    superseded_fact_references_by_development: dict[str, set[int | str]] = {}
    selected_fact_ids: set[int] = set()
    for occurrence_index, occurrence in enumerate(
        occurrence_plan.get("occurrences") or []
    ):
        if not isinstance(occurrence, dict):
            continue
        for raw_reference in occurrence.get("uses_fact_ids") or []:
            reference = fact_reference(raw_reference)
            if reference is None:
                raise ValueError(
                    f"writer occurrence {occurrence_index} contains an invalid fact "
                    f"reference: {raw_reference!r}"
                )
            if reference not in available_fact_references:
                raise ValueError(
                    f"writer occurrence {occurrence_index} references an unknown, stale, "
                    f"or future fact: {raw_reference}"
                )
            if isinstance(reference, int):
                selected_fact_ids.add(reference)

        occurrence_development_ids = {
            str(value) for value in occurrence.get("development_ids") or []
        }
        references_replaced_by_same_development = {
            reference
            for development_id in occurrence_development_ids
            for reference in superseded_fact_references_by_development.get(
                development_id, set()
            )
        }
        superseded: set[int | str] = set()
        added_keys: set[str] = set()
        for fact in occurrence.get("durable_facts") or []:
            if not isinstance(fact, dict) or not nonempty_string(fact.get("key")):
                continue
            for raw_reference in fact.get("supersedes") or []:
                reference = fact_reference(raw_reference)
                if reference is None or (
                    reference not in available_fact_references
                    and reference not in references_replaced_by_same_development
                ):
                    raise ValueError(
                        f"writer occurrence {occurrence_index} supersedes an unknown, "
                        f"stale, or future fact: {raw_reference}"
                    )
                superseded.add(reference)
            added_keys.add(str(fact["key"]))
        available_fact_references.difference_update(superseded)
        available_fact_references.update(added_keys)
        for development_id in occurrence_development_ids:
            superseded_fact_references_by_development.setdefault(
                development_id, set()
            ).update(superseded)
    return [
        copy.deepcopy(fact)
        for fact in current_facts_by_id.values()
        if isinstance(fact, dict)
        and fact_reference(fact.get("id")) in selected_fact_ids
    ]


def preferred_contact_subject_ids(
    subject_ids: list[Any],
    *,
    primary_subject_ids: set[str],
    entities: list[dict[str, Any]],
) -> set[str]:
    """Prefer the concrete thing involved over broadly shared people or organizations."""
    non_primary = {
        str(subject)
        for subject in subject_ids
        if nonempty_string(subject) and str(subject) not in primary_subject_ids
    }
    kinds = {
        str(entity.get("id")): str(entity.get("kind") or "").lower()
        for entity in entities
        if isinstance(entity, dict) and nonempty_string(entity.get("id"))
    }
    concrete = {
        subject
        for subject in non_primary
        if kinds.get(subject) not in {"person", "company", "organization", "team"}
    }
    return concrete or non_primary


def writer_facts_by_occurrence(
    occurrence_plan: dict[str, Any],
    facts: list[dict[str, Any]],
    *,
    primary_subject_ids: set[str] | None = None,
    entities: list[dict[str, Any]] | None = None,
    accepted_history: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return only facts that the planner explicitly cites for each contact."""
    source_dates_by_fact_id: dict[int | str, list[str]] = {}
    accepted_dates_by_session_id: dict[str, set[str]] = {}
    for session in accepted_history or []:
        if not isinstance(session, dict) or not nonempty_string(session.get("id")):
            continue
        session_date = iso_date(session.get("narrative_date"))
        if session_date is not None:
            accepted_dates_by_session_id.setdefault(str(session["id"]), set()).add(
                session_date.isoformat()
            )
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        fact_id = fact_reference(fact.get("id"))
        if fact_id is None:
            continue
        source_dates = {
            source_date
            for source_session_id in fact.get("source_session_ids") or []
            for source_date in accepted_dates_by_session_id.get(
                str(source_session_id), set()
            )
        }
        if source_dates:
            source_dates_by_fact_id[fact_id] = sorted(source_dates)
    available_facts = {
        fact_reference(fact.get("id")): fact
        for fact in compact_current_facts_for_model(facts)
        if fact_reference(fact.get("id")) is not None
    }
    superseded_fact_references_by_development: dict[str, set[int | str]] = {}
    result: dict[str, list[dict[str, Any]]] = {}
    for occurrence_index, occurrence in enumerate(
        occurrence_plan.get("occurrences") or []
    ):
        if not isinstance(occurrence, dict):
            continue
        contact_id = str(occurrence.get("contact_id") or "")
        if not nonempty_string(contact_id):
            raise ValueError(f"writer occurrence {occurrence_index} needs a contact_id")
        selected_references: list[int | str] = []
        for raw_reference in occurrence.get("uses_fact_ids") or []:
            reference = fact_reference(raw_reference)
            if reference is None:
                raise ValueError(
                    f"writer occurrence {occurrence_index} contains an invalid fact "
                    f"reference: {raw_reference!r}"
                )
            if reference not in available_facts:
                raise ValueError(
                    f"writer occurrence {occurrence_index} references an unknown, stale, "
                    f"or future fact: {raw_reference}"
                )
            if reference not in selected_references:
                selected_references.append(reference)

        occurrence_development_ids = {
            str(value) for value in occurrence.get("development_ids") or []
        }
        references_replaced_by_same_development = {
            reference
            for development_id in occurrence_development_ids
            for reference in superseded_fact_references_by_development.get(
                development_id, set()
            )
        }
        superseded: set[int | str] = set()
        for fact in occurrence.get("durable_facts") or []:
            if not isinstance(fact, dict) or not nonempty_string(fact.get("key")):
                continue
            for raw_reference in fact.get("supersedes") or []:
                reference = fact_reference(raw_reference)
                if reference is None or (
                    reference not in available_facts
                    and reference not in references_replaced_by_same_development
                ):
                    raise ValueError(
                        f"writer occurrence {occurrence_index} supersedes an unknown, "
                        f"stale, or future fact: {raw_reference}"
                    )
                superseded.add(reference)
        selected_facts: list[dict[str, Any]] = []
        for reference in selected_references:
            selected_fact = copy.deepcopy(available_facts[reference])
            source_dates = source_dates_by_fact_id.get(reference)
            if source_dates:
                selected_fact["source_message_dates"] = copy.deepcopy(source_dates)
            selected_facts.append(selected_fact)
        result[contact_id] = selected_facts
        for reference in superseded:
            available_facts.pop(reference, None)
        for fact in occurrence.get("durable_facts") or []:
            if isinstance(fact, dict) and nonempty_string(fact.get("key")):
                available_facts[str(fact["key"])] = model_fact_fields(fact)
        for development_id in occurrence_development_ids:
            superseded_fact_references_by_development.setdefault(
                development_id, set()
            ).update(superseded)
    return result


def writer_known_entities_for_occurrences(
    entities: list[dict[str, Any]],
    *,
    occurrence_plan: dict[str, Any],
    current_facts: list[dict[str, Any]],
    required_entities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only entities the accepted writer input can actually name."""
    entity_ids: set[str] = set()
    for occurrence in occurrence_plan.get("occurrences") or []:
        if not isinstance(occurrence, dict):
            continue
        for field in ("subject_ids",):
            entity_ids.update(
                str(value)
                for value in occurrence.get(field) or []
                if nonempty_string(value)
            )
    for fact in current_facts:
        entity_ids.update(
            str(value)
            for value in fact.get("subjects") or []
            if nonempty_string(value)
        )
    entity_ids.update(
        str(entity.get("id"))
        for entity in required_entities
        if isinstance(entity, dict) and nonempty_string(entity.get("id"))
    )
    selected = [
        model_entity_identity(entity)
        for entity in entities
        if isinstance(entity, dict) and str(entity.get("id") or "") in entity_ids
    ]
    selected_ids = {str(entity.get("id") or "") for entity in selected}
    selected.extend(
        model_entity_identity(entity)
        for entity in required_entities
        if isinstance(entity, dict)
        and nonempty_string(entity.get("id"))
        and str(entity["id"]) not in selected_ids
    )
    return selected


def writer_known_entities_for_occurrence(
    entities: list[dict[str, Any]],
    *,
    occurrence: dict[str, Any],
    current_facts: list[dict[str, Any]],
    required_entities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only entities named by one occurrence or introduced by it."""
    entity_ids = {
        str(value)
        for value in occurrence.get("subject_ids") or []
        if nonempty_string(value)
    }
    for fact in current_facts:
        entity_ids.update(
            str(value)
            for value in fact.get("subjects") or []
            if nonempty_string(value)
        )
    development_ids = {
        str(value)
        for value in occurrence.get("development_ids") or []
        if nonempty_string(value)
    }
    for entity in required_entities:
        if not isinstance(entity, dict):
            continue
        entity_id = str(entity.get("id") or "")
        if entity_id and str(entity.get("introduced_in_event_id") or "") in development_ids:
            entity_ids.add(entity_id)
    selected = [
        model_entity_identity(entity)
        for entity in entities
        if isinstance(entity, dict) and str(entity.get("id") or "") in entity_ids
    ]
    selected_ids = {str(entity.get("id") or "") for entity in selected}
    selected.extend(
        model_entity_identity(entity)
        for entity in required_entities
        if isinstance(entity, dict)
        and str(entity.get("id") or "") in entity_ids
        and str(entity.get("id") or "") not in selected_ids
    )
    return selected


def exact_prior_context_for_occurrence_references(
    occurrence_plan: dict[str, Any], state: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return exact accepted context only for explicitly continued contacts."""
    purposes = [
        row
        for row in state.get("session_purposes") or []
        if isinstance(row, dict) and nonempty_string(row.get("session_id"))
    ]
    history_by_session = {
        str(row.get("id")): row
        for row in state.get("history") or []
        if isinstance(row, dict) and nonempty_string(row.get("id"))
    }
    results_by_session: dict[str, list[dict[str, Any]]] = {}
    for row in state.get("operation_results") or []:
        if not isinstance(row, dict) or not nonempty_string(row.get("session_id")):
            continue
        results_by_session.setdefault(str(row["session_id"]), []).append(row)

    references: list[tuple[str, str]] = []
    seen_references: set[tuple[str, str]] = set()
    for occurrence in occurrence_plan.get("occurrences") or []:
        if not isinstance(occurrence, dict):
            continue
        thread_id = (
            str(occurrence.get("thread_id"))
            if nonempty_string(occurrence.get("thread_id"))
            else ""
        )
        for reference in occurrence.get("continues_from") or []:
            key = (thread_id, str(reference))
            if key not in seen_references:
                references.append(key)
                seen_references.add(key)
    context: list[dict[str, Any]] = []
    for thread_id, reference in references:
        thread_purposes = [
            row
            for row in purposes
            if thread_id
            and str(row.get("thread_id") or "") == thread_id
        ]
        session_matches = {
            (
                str(row.get("session_id") or ""),
                str(row.get("planned_interaction_id") or ""),
            ): row
            for row in thread_purposes
            if str(row.get("session_id") or "") == reference
        }
        planned_interaction_matches = {
            (
                str(row.get("session_id") or ""),
                str(row.get("planned_interaction_id") or ""),
            ): row
            for row in thread_purposes
            if str(row.get("planned_interaction_id") or "") == reference
        }
        if len(session_matches) == 1:
            purpose = next(iter(session_matches.values()))
        elif len(planned_interaction_matches) == 1:
            purpose = next(iter(planned_interaction_matches.values()))
        elif not session_matches and not planned_interaction_matches:
            continue
        else:
            raise ValueError(
                f"continued reference {reference} maps to multiple accepted sessions"
            )
        session_id = str(purpose["session_id"])
        history = history_by_session.get(session_id)
        if history is None:
            raise ValueError(
                f"continued reference {reference} resolves to missing history "
                f"session {session_id}"
            )
        recorded = results_by_session.get(session_id, [])
        context.append(
            {
                "reference_id": reference,
                "planned_interaction_id": purpose.get("planned_interaction_id"),
                "session_id": session_id,
                "messages": copy.deepcopy(history.get("messages") or []),
                "recorded_app_operations_and_results": [
                    {
                        "operation": copy.deepcopy(row.get("operation")),
                        "result": copy.deepcopy(row.get("result")),
                    }
                    for row in recorded
                ],
            }
        )
    return context


def prior_accepted_context_by_contact_subject(
    occurrence_plan: dict[str, Any],
    state: dict[str, Any],
    *,
    primary_subject_ids: set[str],
    entities: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Return short-term exact contacts about the same concrete subjects."""
    purposes_by_session_id = {
        str(row.get("session_id")): row
        for row in state.get("session_purposes") or []
        if isinstance(row, dict) and nonempty_string(row.get("session_id"))
    }
    history_by_session_id = {
        str(row.get("id")): row
        for row in state.get("history") or []
        if isinstance(row, dict) and nonempty_string(row.get("id"))
    }
    subjects_by_session_id: dict[str, set[str]] = {}
    for session_id, purpose in purposes_by_session_id.items():
        subjects_by_session_id[session_id] = {
            str(subject)
            for subject in purpose.get("subject_ids") or []
            if nonempty_string(subject)
        }
    for session_id, session in history_by_session_id.items():
        subjects_by_session_id.setdefault(session_id, set()).update(
            str(subject)
            for subject in session.get("subject_ids") or []
            if nonempty_string(subject)
        )
    for fact in state.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        subjects = {
            str(subject)
            for subject in fact.get("subjects") or []
            if nonempty_string(subject)
        }
        for session_id in fact.get("source_session_ids") or []:
            subjects_by_session_id.setdefault(str(session_id), set()).update(subjects)

    results_by_session_id: dict[str, list[dict[str, Any]]] = {}
    for row in state.get("operation_results") or []:
        if not isinstance(row, dict) or not nonempty_string(row.get("session_id")):
            continue
        results_by_session_id.setdefault(str(row["session_id"]), []).append(
            {
                "operation": copy.deepcopy(row.get("operation")),
                "result": copy.deepcopy(row.get("result")),
            }
        )

    context_by_contact: dict[str, list[dict[str, Any]]] = {}
    for occurrence in occurrence_plan.get("occurrences") or []:
        if not isinstance(occurrence, dict):
            continue
        contact_id = str(occurrence.get("contact_id") or "")
        if not contact_id:
            continue
        try:
            occurrence_time = datetime.fromisoformat(
                str(occurrence.get("narrative_date") or "")
            )
        except ValueError:
            continue
        if occurrence_time.tzinfo is None:
            continue
        contact_subjects = preferred_contact_subject_ids(
            occurrence.get("subject_ids") or [],
            primary_subject_ids=primary_subject_ids,
            entities=entities,
        )
        if not contact_subjects:
            context_by_contact[contact_id] = []
            continue
        earliest = occurrence_time - timedelta(days=14)
        selected: list[dict[str, Any]] = []
        seen_session_ids: set[str] = set()
        for session_id, history in history_by_session_id.items():
            narrative_date = str(history.get("narrative_date") or "")
            try:
                prior_time = datetime.fromisoformat(narrative_date)
            except ValueError:
                continue
            if prior_time.tzinfo is None or not earliest <= prior_time < occurrence_time:
                continue
            prior_subjects = preferred_contact_subject_ids(
                list(subjects_by_session_id.get(session_id, set())),
                primary_subject_ids=primary_subject_ids,
                entities=entities,
            )
            if not prior_subjects & contact_subjects:
                continue
            if session_id in seen_session_ids:
                continue
            seen_session_ids.add(session_id)
            purpose = purposes_by_session_id.get(session_id, {})
            selected.append(
                {
                    "reference_id": session_id,
                    "planned_interaction_id": purpose.get("planned_interaction_id"),
                    "session_id": session_id,
                    "date": narrative_date,
                    "subject_ids": sorted(subjects_by_session_id[session_id]),
                    "messages": copy.deepcopy(history.get("messages") or []),
                    "recorded_app_operations_and_results": copy.deepcopy(
                        results_by_session_id.get(session_id, [])
                    ),
                }
            )
        selected.sort(key=lambda row: (str(row.get("date") or ""), str(row["session_id"])))
        context_by_contact[contact_id] = selected
    return context_by_contact


def _validate_legacy_history_window_plan_response(
    response: dict[str, Any],
    *,
    start: date,
    end: date,
    timezone_name: str,
    entities: list[dict[str, Any]],
    existing_session_ids: set[str],
    existing_references: set[str],
    required_development_ids: set[str],
    allowed_development_ids: set[str],
    required_anchors: dict[str, dict[str, Any]],
    required_entities: list[dict[str, Any]],
    facts: list[dict[str, Any]] | None = None,
    tools: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """Validate the fixed occurrence sequence before exposing writer context."""
    errors: list[str] = []
    if not isinstance(response, dict):
        return ["response must be an object containing plan"]
    if visible_fields(response) != {"plan"}:
        errors.append("response must contain exactly plan")
    plan = response.get("plan")
    if not require_exact_fields(plan, PLANNER_PLAN_FIELDS, "plan", errors):
        return errors
    if plan.get("period_start") != start.isoformat():
        errors.append("plan.period_start does not match start-date")
    if plan.get("period_end") != end.isoformat():
        errors.append("plan.period_end does not match end-date")

    occurrences = plan.get("occurrences")
    if not isinstance(occurrences, list) or not occurrences:
        errors.append("plan.occurrences must be a non-empty list")
        return errors

    contact_ids: list[str] = []
    for index, occurrence in enumerate(occurrences):
        label = f"occurrences[{index}]"
        if isinstance(occurrence, dict):
            occurrence.setdefault("uses_items_created_by_contact_ids", [])
        if require_exact_fields(occurrence, OCCURRENCE_FIELDS, label, errors):
            contact_ids.append(str(occurrence.get("contact_id") or ""))
    if any(not value for value in contact_ids) or len(contact_ids) != len(set(contact_ids)):
        errors.append("occurrences must have unique non-empty contact_id values")
    collisions = sorted(set(contact_ids) & (existing_references | existing_session_ids))
    if collisions:
        errors.append(f"contact IDs already exist in accepted history: {collisions}")
    operation_contact_positions, operation_counts = _operation_reference_context(occurrences)

    required_entity_by_id = {
        str(row.get("id") or ""): row
        for row in required_entities
        if isinstance(row, dict) and nonempty_string(row.get("id"))
    }
    existing_entity_ids = {
        str(row.get("id")) for row in entities if nonempty_string(row.get("id"))
    }
    introduced_at: dict[str, int] = {}
    for entity_id, entity in required_entity_by_id.items():
        anchor_id = str(entity.get("introduced_in_item_id") or "")
        positions = [
            index
            for index, occurrence in enumerate(occurrences)
            if isinstance(occurrence, dict)
            and anchor_id in (occurrence.get("anchor_item_ids") or [])
        ]
        if len(positions) != 1:
            errors.append(
                f"required new entity {entity_id} needs exactly one introduction "
                f"occurrence carrying {anchor_id}"
            )
            continue
        introduced_at[entity_id] = positions[0]
        subjects = occurrences[positions[0]].get("subject_ids") or []
        if entity_id not in subjects:
            errors.append(
                f"required new entity {entity_id} is absent from its introduction occurrence"
            )

    development_counts = {value: 0 for value in required_development_ids}
    anchor_counts = {value: 0 for value in required_anchors}
    known_fact_ids = {
        int(row["id"])
        for row in facts or []
        if isinstance(row, dict)
        and isinstance(row.get("id"), int)
        and not isinstance(row.get("id"), bool)
    }
    current_fact_references: set[int | str] = set(current_existing_fact_ids(facts or []))
    planned_fact_keys: set[str] = set()
    previous_time: datetime | None = None
    prior_contacts = set(existing_references)

    for index, occurrence in enumerate(occurrences):
        if not isinstance(occurrence, dict):
            continue
        label = f"occurrences[{index}]"
        contact_id = str(occurrence.get("contact_id") or "")
        local_date: date | None = None
        try:
            when = datetime.fromisoformat(str(occurrence.get("narrative_date") or ""))
            if when.tzinfo is None:
                raise ValueError
            local_when = when.astimezone(ZoneInfo(timezone_name))
            local_date = local_when.date()
            expected_offset = local_when.replace(tzinfo=None).replace(
                tzinfo=ZoneInfo(timezone_name)
            ).utcoffset()
            if when.utcoffset() != expected_offset:
                errors.append(f"{label}.narrative_date offset does not match {timezone_name}")
            if not start <= local_date <= end:
                errors.append(f"{label}.narrative_date is outside the requested dates")
            if previous_time is not None and when <= previous_time:
                errors.append("occurrences must be strictly chronological")
            previous_time = when
        except ValueError:
            errors.append(f"{label}.narrative_date must be an offset-aware ISO datetime")

        for field in ("thread_id", "what_happens", "why_contact_assistant"):
            if not nonempty_string(occurrence.get(field)):
                errors.append(f"{label}.{field} must be a non-empty string")
        for field in (
            "continues_from",
            "subject_ids",
            "development_ids",
            "anchor_item_ids",
        ):
            if not string_list(occurrence.get(field)):
                errors.append(f"{label}.{field} must be a unique list of strings")

        uses = occurrence.get("uses_facts")
        if not isinstance(uses, list) or len(uses) != len(set(map(str, uses))):
            errors.append(f"{label}.uses_facts must be a unique list")
            uses = []
        for raw_reference in uses:
            if isinstance(raw_reference, int) and not isinstance(raw_reference, bool):
                if raw_reference not in known_fact_ids:
                    errors.append(
                        f"{label}.uses_facts contains unknown fact {raw_reference}"
                    )
                elif raw_reference not in current_fact_references:
                    errors.append(
                        f"{label}.uses_facts contains stale or future fact {raw_reference}"
                    )
                continue
            if not nonempty_string(raw_reference) or str(raw_reference).isdigit():
                errors.append(f"{label}.uses_facts contains an invalid reference")
            elif str(raw_reference) not in planned_fact_keys:
                errors.append(
                    f"{label}.uses_facts contains future or unknown fact key: "
                    f"{raw_reference}"
                )
            elif str(raw_reference) not in current_fact_references:
                errors.append(
                    f"{label}.uses_facts contains stale fact key: {raw_reference}"
                )

        source_material = occurrence.get("source_material")
        if source_material is not None:
            if require_exact_fields(
                source_material,
                SOURCE_MATERIAL_FIELDS,
                f"{label}.source_material",
                errors,
            ):
                for field in SOURCE_MATERIAL_FIELDS:
                    if not nonempty_string(source_material.get(field)):
                        errors.append(
                            f"{label}.source_material.{field} must be a non-empty string"
                        )
        requested_response_or_action = occurrence.get("requested_response_or_action")
        if requested_response_or_action is not None and not nonempty_string(
            requested_response_or_action
        ):
            errors.append(
                f"{label}.requested_response_or_action must be null or a non-empty string"
            )

        durable_facts = occurrence.get("durable_facts")
        if not isinstance(durable_facts, list):
            errors.append(f"{label}.durable_facts must be a list")
            durable_facts = []
        for fact_index, fact in enumerate(durable_facts):
            fact_label = f"{label}.durable_facts[{fact_index}]"
            if not require_exact_fields(fact, PLANNED_FACT_FIELDS, fact_label, errors):
                continue
            key = str(fact.get("key") or "")
            if not key or key.isdigit() or key in planned_fact_keys:
                errors.append(f"{fact_label}.key must be a unique non-numeric string")
                continue
            planned_fact_keys.add(key)
            if not nonempty_string(fact.get("statement")) or not nonempty_string(
                fact.get("applies_when")
            ):
                errors.append(f"{fact_label} needs statement and applies_when")
            if not string_list(fact.get("subjects"), allow_empty=False):
                errors.append(f"{fact_label}.subjects must be a non-empty unique string list")
            for entity_id in fact.get("subjects") or []:
                if entity_id not in existing_entity_ids and (
                    entity_id not in required_entity_by_id
                    or introduced_at.get(entity_id, len(occurrences)) > index
                ):
                    errors.append(f"{fact_label} uses unavailable entity: {entity_id}")
            supersedes = fact.get("supersedes")
            if not isinstance(supersedes, list) or len(supersedes) != len(
                set(map(str, supersedes or []))
            ):
                errors.append(f"{fact_label}.supersedes must be a unique list")
            elif facts is not None:
                for raw_reference in supersedes:
                    reference = fact_reference(raw_reference)
                    if reference is None:
                        errors.append(f"{fact_label}.supersedes has an invalid reference")
                    elif isinstance(reference, int) and reference not in known_fact_ids:
                        errors.append(f"{fact_label}.supersedes has unknown fact {reference}")
                    elif reference not in current_fact_references:
                        errors.append(f"{fact_label}.supersedes has stale or future fact {reference}")
            if facts is not None:
                current_fact_references.difference_update(
                    reference
                    for reference in (
                        fact_reference(value) for value in fact.get("supersedes") or []
                    )
                    if reference is not None
                )
                current_fact_references.add(key)

        operations = occurrence.get("app_operations")
        if not isinstance(operations, list):
            errors.append(f"{label}.app_operations must be a list")
            operations = []
        for operation_index, operation in enumerate(operations):
            operation_label = f"{label}.app_operations[{operation_index}]"
            if not require_exact_fields(
                operation, OPERATION_FIELDS, operation_label, errors
            ):
                continue
            tool = operation.get("tool")
            args = operation.get("args")
            if tools is None:
                continue
            if tool not in tools:
                errors.append(f"{operation_label} uses unknown tool: {tool}")
                continue
            if not isinstance(args, dict):
                errors.append(f"{operation_label}.args must be an object")
                continue
            schema = tools[str(tool)]
            unknown_args = sorted(set(args) - set(schema.get("arguments") or []))
            missing_args = sorted(set(schema.get("required_arguments") or []) - set(args))
            if unknown_args:
                errors.append(f"{operation_label} has unknown args: {unknown_args}")
            if missing_args:
                errors.append(f"{operation_label} lacks required args: {missing_args}")
            errors.extend(
                validate_operation_result_references(
                    args,
                    label=operation_label,
                    current_contact_id=contact_id,
                    current_operation_index=operation_index,
                    contact_positions=operation_contact_positions,
                    operation_counts=operation_counts,
                )
            )

        for reference in occurrence.get("continues_from") or []:
            if str(reference) not in prior_contacts:
                errors.append(f"{label}.continues_from has a non-prior reference: {reference}")

        for entity_id in occurrence.get("subject_ids") or []:
            if entity_id in existing_entity_ids:
                continue
            if entity_id not in required_entity_by_id:
                errors.append(f"{label}.subject_ids uses unknown entity: {entity_id}")
            elif introduced_at.get(entity_id, len(occurrences)) > index:
                errors.append(f"{label}.subject_ids uses unavailable entity: {entity_id}")

        for development_id in occurrence.get("development_ids") or []:
            if development_id not in allowed_development_ids:
                errors.append(
                    f"{label} uses a development outside this window: {development_id}"
                )
            if development_id in development_counts:
                development_counts[development_id] += 1

        for anchor_id in occurrence.get("anchor_item_ids") or []:
            if anchor_id not in required_anchors:
                errors.append(
                    f"{label} uses an accepted contact outside this window: {anchor_id}"
                )
                continue
            anchor_counts[anchor_id] += 1
            anchor = required_anchors[anchor_id]
            accepted_date = str(anchor.get("date") or "")
            if local_date is not None and local_date.isoformat() != accepted_date:
                errors.append(
                    f"{label} carries {anchor_id} on {local_date.isoformat()}, "
                    f"not its accepted date {accepted_date}"
                )
            for field in ("development_ids", "subject_ids", "continues_from"):
                required_values = [str(value) for value in anchor.get(field) or []]
                actual_values = [str(value) for value in occurrence.get(field) or []]
                if actual_values != required_values:
                    errors.append(
                        f"{label} carrying {anchor_id} changes accepted {field}: "
                        f"expected {required_values}, got {actual_values}"
                    )

        is_open = occurrence.get("thread_open_after_contact")
        remains = occurrence.get("what_remains_after_contact")
        if not isinstance(is_open, bool):
            errors.append(f"{label}.thread_open_after_contact must be a boolean")
        elif is_open and not nonempty_string(remains):
            errors.append(f"{label}.what_remains_after_contact must explain open work")
        elif not is_open and remains is not None:
            errors.append(f"{label}.what_remains_after_contact must be null when closed")

        prior_contacts.add(contact_id)
        prior_contacts.update(str(value) for value in occurrence.get("anchor_item_ids") or [])

    missing_developments = sorted(
        value for value, count in development_counts.items() if count == 0
    )
    if missing_developments:
        errors.append(f"plan does not complete required developments: {missing_developments}")
    incorrect_anchors = sorted(value for value, count in anchor_counts.items() if count != 1)
    if incorrect_anchors:
        errors.append(
            "each accepted contact in the window must appear exactly once: "
            f"{incorrect_anchors}"
        )
    return list(dict.fromkeys(errors))


def validate_history_window_plan_response(
    response: dict[str, Any],
    *,
    start: date,
    end: date,
    timezone_name: str,
    entities: list[dict[str, Any]],
    existing_session_ids: set[str],
    existing_references: set[str],
    required_development_ids: set[str],
    allowed_development_ids: set[str],
    required_entities: list[dict[str, Any]],
    facts: list[dict[str, Any]] | None = None,
    tools: dict[str, dict[str, Any]] | None = None,
    active_situation_ids: set[str] | None = None,
    self_contained_basis_ids: set[str] | None = None,
    open_thread_ids: set[str] | None = None,
    protected_change_ids: set[str] | None = None,
    facts_by_development: dict[str, list[dict[str, Any]]] | None = None,
    subjects_by_basis: dict[str, set[str]] | None = None,
    primary_subject_ids: set[str] | None = None,
    unfinished_thread_ids_by_reference: dict[str, str] | None = None,
    open_thread_current_states_by_id: dict[str, list[dict[str, Any]]] | None = None,
    development_start_dates: dict[str, date] | None = None,
    available_app_records: dict[str, list[dict[str, Any]]] | None = None,
    earlier_accepted_app_items: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Validate the weekly planner's complete, writer-independent decisions."""
    tools = window_tools(tools)
    return _validate_weekly_occurrence_plan(
        response,
        start=start,
        end=end,
        timezone_name=timezone_name,
        entities=entities,
        existing_session_ids=existing_session_ids,
        existing_references=existing_references,
        required_development_ids=required_development_ids,
        allowed_development_ids=allowed_development_ids,
        required_entities=required_entities,
        facts=facts or [],
        self_contained_basis_ids=(
            self_contained_basis_ids
            if self_contained_basis_ids is not None
            else active_situation_ids or set()
        ),
        open_thread_ids=open_thread_ids or set(),
        protected_change_ids=protected_change_ids or set(),
        facts_by_development=facts_by_development or {},
        subjects_by_basis=subjects_by_basis or {},
        primary_subject_ids=primary_subject_ids or set(),
        tools=tools,
        unfinished_thread_ids_by_reference=(
            unfinished_thread_ids_by_reference or {}
        ),
        open_thread_current_states_by_id=(
            open_thread_current_states_by_id or {}
        ),
        development_start_dates=development_start_dates or {},
        available_app_records=available_app_records or {},
        earlier_accepted_app_items=earlier_accepted_app_items or [],
    )


def add_known_world_basis_for_visible_subjects(
    response: dict[str, Any],
    *,
    facts: list[dict[str, Any]],
    subjects_by_basis: dict[str, set[str]],
    primary_subject_ids: set[str],
) -> dict[str, Any]:
    """Cite the known-world input when a plan uses one of its visible entities."""
    normalized = copy.deepcopy(response)
    plan = normalized.get("plan")
    if not isinstance(plan, dict):
        return normalized
    occurrences = plan.get("occurrences")
    if not isinstance(occurrences, list):
        return normalized

    visible_known_subjects = subjects_by_basis.get(
        ORDINARY_CURRENT_LIFE_BASIS_ID, set()
    )
    fact_subjects_by_reference = {
        reference: {
            str(subject)
            for subject in fact.get("subjects") or []
            if nonempty_string(subject)
        }
        for fact in facts
        if isinstance(fact, dict)
        for reference in [fact_reference(fact.get("id"))]
        if reference is not None
    }

    for occurrence in occurrences:
        if not isinstance(occurrence, dict):
            continue
        basis_ids = occurrence.get("basis_ids")
        subject_ids = occurrence.get("subject_ids")
        if not isinstance(basis_ids, list) or not isinstance(subject_ids, list):
            continue

        cited_subjects = set(primary_subject_ids)
        for basis_id in basis_ids:
            cited_subjects.update(subjects_by_basis.get(str(basis_id), set()))
        for development_id in occurrence.get("development_ids") or []:
            cited_subjects.update(
                subjects_by_basis.get(str(development_id), set())
            )
        for raw_fact_id in occurrence.get("uses_fact_ids") or []:
            fact_id = fact_reference(raw_fact_id)
            if fact_id is not None:
                cited_subjects.update(
                    fact_subjects_by_reference.get(fact_id, set())
                )
        for durable_fact in occurrence.get("durable_facts") or []:
            if isinstance(durable_fact, dict):
                cited_subjects.update(
                    str(subject)
                    for subject in durable_fact.get("subjects") or []
                    if nonempty_string(subject)
                )

        needs_known_world_basis = any(
            str(subject) in visible_known_subjects
            and str(subject) not in cited_subjects
            for subject in subject_ids
        )
        if (
            needs_known_world_basis
            and ORDINARY_CURRENT_LIFE_BASIS_ID not in basis_ids
        ):
            basis_ids.append(ORDINARY_CURRENT_LIFE_BASIS_ID)
    return normalized


def _validate_weekly_occurrence_plan(
    response: dict[str, Any],
    *,
    start: date,
    end: date,
    timezone_name: str,
    entities: list[dict[str, Any]],
    existing_session_ids: set[str],
    existing_references: set[str],
    required_development_ids: set[str],
    allowed_development_ids: set[str],
    required_entities: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    self_contained_basis_ids: set[str],
    open_thread_ids: set[str],
    protected_change_ids: set[str],
    facts_by_development: dict[str, list[dict[str, Any]]],
    subjects_by_basis: dict[str, set[str]],
    primary_subject_ids: set[str],
    tools: dict[str, dict[str, Any]],
    unfinished_thread_ids_by_reference: dict[str, str],
    open_thread_current_states_by_id: dict[str, list[dict[str, Any]]],
    development_start_dates: dict[str, date],
    available_app_records: dict[str, list[dict[str, Any]]],
    earlier_accepted_app_items: list[dict[str, Any]],
) -> list[str]:
    return _validate_weekly_occurrence_plan_v2(
        response,
        start=start,
        end=end,
        timezone_name=timezone_name,
        entities=entities,
        existing_session_ids=existing_session_ids,
        existing_references=existing_references,
        required_development_ids=required_development_ids,
        allowed_development_ids=allowed_development_ids,
        required_entities=required_entities,
        facts=facts,
        self_contained_basis_ids=self_contained_basis_ids,
        open_thread_ids=open_thread_ids,
        protected_change_ids=protected_change_ids,
        facts_by_development=facts_by_development,
        subjects_by_basis=subjects_by_basis,
        primary_subject_ids=primary_subject_ids,
        tools=tools,
        unfinished_thread_ids_by_reference=unfinished_thread_ids_by_reference,
        open_thread_current_states_by_id=open_thread_current_states_by_id,
        development_start_dates=development_start_dates,
        available_app_records=available_app_records,
        earlier_accepted_app_items=earlier_accepted_app_items,
    )
    errors: list[str] = []
    if not isinstance(response, dict):
        return ["response must be an object containing plan"]
    if visible_fields(response) != {"plan"}:
        errors.append("response must contain exactly plan")
    plan = response.get("plan")
    if not require_exact_fields(plan, PLANNER_PLAN_FIELDS, "plan", errors):
        return errors
    if plan.get("period_start") != start.isoformat():
        errors.append("plan.period_start does not match start-date")
    if plan.get("period_end") != end.isoformat():
        errors.append("plan.period_end does not match end-date")
    occurrences = plan.get("occurrences")
    if not isinstance(occurrences, list) or not occurrences:
        return [*errors, "plan.occurrences must be a non-empty list"]

    existing_entity_ids = {
        str(row.get("id")) for row in entities if isinstance(row, dict) and nonempty_string(row.get("id"))
    }
    required_entity_ids = {
        str(row.get("id")) for row in required_entities if isinstance(row, dict) and nonempty_string(row.get("id"))
    }
    previous_time: datetime | None = None
    prior_contacts = set(existing_references)
    contact_ids: list[str] = []
    development_counts = {value: 0 for value in required_development_ids}
    anchor_counts = {value: 0 for value in required_anchors}
    for index, occurrence in enumerate(occurrences):
        label = f"occurrences[{index}]"
        if isinstance(occurrence, dict):
            occurrence.setdefault("uses_items_created_by_contact_ids", [])
        if not require_exact_fields(occurrence, OCCURRENCE_FIELDS, label, errors):
            continue
        contact_id = str(occurrence.get("contact_id") or "")
        contact_ids.append(contact_id)
        local_when: datetime | None = None
        try:
            when = datetime.fromisoformat(str(occurrence.get("narrative_date") or ""))
            if when.tzinfo is None:
                raise ValueError
            local_when = when.astimezone(ZoneInfo(timezone_name))
            expected_offset = local_when.replace(tzinfo=None).replace(tzinfo=ZoneInfo(timezone_name)).utcoffset()
            if when.utcoffset() != expected_offset:
                errors.append(f"{label}.narrative_date offset does not match {timezone_name}")
            if not start <= local_when.date() <= end:
                errors.append(f"{label}.narrative_date is outside the requested dates")
            if previous_time is not None and when <= previous_time:
                errors.append("occurrences must be strictly chronological")
            previous_time = when
        except ValueError:
            errors.append(f"{label}.narrative_date must be an offset-aware ISO datetime")
        for field in ("thread_id", "what_happens", "why_contact_assistant"):
            if not nonempty_string(occurrence.get(field)):
                errors.append(f"{label}.{field} must be a non-empty string")
        for field in ("continues_from", "subject_ids", "development_ids", "anchor_item_ids"):
            if not string_list(occurrence.get(field)):
                errors.append(f"{label}.{field} must be a unique list of strings")
        for reference in occurrence.get("continues_from") or []:
            if str(reference) not in prior_contacts:
                errors.append(f"{label}.continues_from has a non-prior reference: {reference}")
        for entity_id in occurrence.get("subject_ids") or []:
            if entity_id not in existing_entity_ids and entity_id not in required_entity_ids:
                errors.append(f"{label}.subject_ids uses unknown entity: {entity_id}")
        for development_id in occurrence.get("development_ids") or []:
            if development_id not in allowed_development_ids:
                errors.append(f"{label} uses a development outside this window: {development_id}")
            if development_id in development_counts:
                development_counts[development_id] += 1
        for anchor_id in occurrence.get("anchor_item_ids") or []:
            if anchor_id not in required_anchors:
                errors.append(f"{label} uses an accepted contact outside this window: {anchor_id}")
            else:
                anchor_counts[anchor_id] += 1
        prior_contacts.add(contact_id)
    if any(not value for value in contact_ids) or len(contact_ids) != len(set(contact_ids)):
        errors.append("occurrences must have unique non-empty contact_id values")
    collisions = sorted(set(contact_ids) & (existing_references | existing_session_ids))
    if collisions:
        errors.append(f"contact IDs already exist in accepted history: {collisions}")
    missing = sorted(value for value, count in development_counts.items() if count == 0)
    if missing:
        errors.append(f"plan does not complete required developments: {missing}")
    incorrect = sorted(value for value, count in anchor_counts.items() if count != 1)
    if incorrect:
        errors.append("each accepted contact in the window must appear exactly once: " + str(incorrect))
    return list(dict.fromkeys(errors))


def planner_fact_declarations(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Expose planner-owned fact declarations to the deterministic writer check."""
    declarations: dict[str, dict[str, Any]] = {}
    for occurrence in plan.get("occurrences") or []:
        if not isinstance(occurrence, dict):
            continue
        for fact in occurrence.get("durable_facts") or []:
            if not isinstance(fact, dict) or not nonempty_string(fact.get("key")):
                continue
            declarations[str(fact["key"])] = copy.deepcopy(fact)
    return declarations


def _validate_weekly_occurrence_plan_v2(
    response: dict[str, Any],
    *,
    start: date,
    end: date,
    timezone_name: str,
    entities: list[dict[str, Any]],
    existing_session_ids: set[str],
    existing_references: set[str],
    required_development_ids: set[str],
    allowed_development_ids: set[str],
    required_entities: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    self_contained_basis_ids: set[str],
    open_thread_ids: set[str],
    protected_change_ids: set[str],
    facts_by_development: dict[str, list[dict[str, Any]]],
    subjects_by_basis: dict[str, set[str]],
    primary_subject_ids: set[str],
    tools: dict[str, dict[str, Any]],
    unfinished_thread_ids_by_reference: dict[str, str],
    open_thread_current_states_by_id: dict[str, list[dict[str, Any]]],
    development_start_dates: dict[str, date],
    available_app_records: dict[str, list[dict[str, Any]]],
    earlier_accepted_app_items: list[dict[str, Any]],
) -> list[str]:
    """Check the ownership boundary; prose semantics remain an empirical check."""
    errors: list[str] = []
    if not isinstance(response, dict) or visible_fields(response) != {"plan"}:
        return ["response must contain exactly plan"]
    plan = response.get("plan")
    if not require_exact_fields(plan, PLANNER_PLAN_FIELDS, "plan", errors):
        return errors
    if plan.get("period_start") != start.isoformat():
        errors.append("plan.period_start does not match start-date")
    if plan.get("period_end") != end.isoformat():
        errors.append("plan.period_end does not match end-date")
    occurrences = plan.get("occurrences")
    if not isinstance(occurrences, list) or not occurrences:
        return [*errors, "plan.occurrences must be a non-empty list"]

    existing_entity_ids = {
        str(row.get("id"))
        for row in entities
        if isinstance(row, dict) and nonempty_string(row.get("id"))
    }
    allowed_entity_ids = existing_entity_ids | {
        str(row.get("id"))
        for row in required_entities
        if isinstance(row, dict) and nonempty_string(row.get("id"))
    }
    current_fact_ids = set(current_existing_fact_ids(facts))
    fact_subjects_by_reference = {
        reference: {
            str(subject)
            for subject in row.get("subjects") or []
            if nonempty_string(subject)
        }
        for row in facts
        if isinstance(row, dict)
        for reference in [fact_reference(row.get("id"))]
        if reference in current_fact_ids
    }
    known_fact_ids = {
        fact_reference(row.get("id"))
        for row in facts
        if isinstance(row, dict) and fact_reference(row.get("id")) is not None
    }
    expected_facts = {
        development_id: {
            str(fact.get("key")): model_fact_fields(fact)
            for fact in declarations
            if isinstance(fact, dict) and nonempty_string(fact.get("key"))
        }
        for development_id, declarations in facts_by_development.items()
    }
    contact_ids: list[str] = []
    previous_time: datetime | None = None
    prior_contacts = set(existing_references)
    available_fact_references: set[int | str] = set(current_fact_ids)
    superseded_fact_references_by_development: dict[str, set[int | str]] = {}
    development_counts = {value: 0 for value in required_development_ids}
    required_declared_facts = {
        key: fact
        for development_id in required_development_ids
        for key, fact in expected_facts.get(development_id, {}).items()
    }
    required_declared_fact_counts = {
        key: 0 for key in required_declared_facts
    }
    durable_fact_keys_in_window: set[str] = set()
    allowed_basis_ids = (
        set(allowed_development_ids) | self_contained_basis_ids | open_thread_ids
    )
    open_contact_by_thread: dict[str, str] = {}
    closed_thread_ids: set[str] = set()
    open_thread_id_by_reference = dict(unfinished_thread_ids_by_reference)
    operation_contact_positions, operation_counts = _operation_reference_context(
        occurrences
    )
    supplied_app_record_ids = {
        str(value)
        for records in available_app_records.values()
        if isinstance(records, list)
        for record in records
        if isinstance(record, dict)
        for field in (*APP_ITEM_ID_FIELDS, "record_key")
        for value in [record.get(field)]
        if nonempty_string(value)
    }
    supplied_app_record_ids.update(
        str(value)
        for group in earlier_accepted_app_items
        if isinstance(group, dict)
        for item in group.get("items") or []
        if isinstance(item, dict)
        for field in ("record_id", "operation_result_id")
        for value in [item.get(field)]
        if nonempty_string(value)
    )

    for index, occurrence in enumerate(occurrences):
        label = f"occurrences[{index}]"
        if isinstance(occurrence, dict):
            occurrence.setdefault("uses_items_created_by_contact_ids", [])
        if not require_exact_fields(occurrence, OCCURRENCE_FIELDS, label, errors):
            continue
        contact_id = str(occurrence.get("contact_id") or "")
        contact_ids.append(contact_id)
        local_when: datetime | None = None
        try:
            when = datetime.fromisoformat(str(occurrence.get("narrative_date") or ""))
            if when.tzinfo is None:
                raise ValueError
            local_when = when.astimezone(ZoneInfo(timezone_name))
            if not start <= local_when.date() <= end:
                errors.append(f"{label}.narrative_date is outside the requested dates")
            if previous_time is not None and when <= previous_time:
                errors.append("occurrences must be strictly chronological")
            previous_time = when
        except ValueError:
            errors.append(f"{label}.narrative_date must be an offset-aware ISO datetime")
        for field in ("happening_id", "what_happened"):
            if not nonempty_string(occurrence.get(field)):
                errors.append(f"{label}.{field} must be a non-empty string")
        for field in (
            "basis_ids",
            "continues_from",
            "uses_items_created_by_contact_ids",
            "subject_ids",
            "development_ids",
            "anchor_item_ids",
        ):
            if not string_list(occurrence.get(field)):
                errors.append(f"{label}.{field} must be a unique list of strings")
        current_state_changes = occurrence.get("current_state_changes")
        if not isinstance(current_state_changes, list):
            errors.append(f"{label}.current_state_changes must be a list")
            current_state_changes = []
        seen_state_keys: set[str] = set()
        normalized_what_happened = " ".join(
            str(occurrence.get("what_happened") or "").split()
        )
        for state_index, change in enumerate(current_state_changes):
            state_label = f"{label}.current_state_changes[{state_index}]"
            if not require_exact_fields(
                change, CURRENT_STATE_CHANGE_FIELDS, state_label, errors
            ):
                continue
            key = str(change.get("key") or "")
            if not CURRENT_STATE_CHANGE_KEY_RE.fullmatch(key):
                errors.append(
                    f"{state_label}.key must be a stable lowercase underscore identifier"
                )
            elif key in seen_state_keys:
                errors.append(f"{state_label}.key appears more than once in this occurrence")
            seen_state_keys.add(key)
            subject_ids = change.get("subject_ids")
            if not string_list(subject_ids):
                errors.append(f"{state_label}.subject_ids must be a unique list of strings")
            else:
                for entity_id in subject_ids:
                    if str(entity_id) not in allowed_entity_ids:
                        errors.append(
                            f"{state_label}.subject_ids uses unknown entity: {entity_id}"
                        )
            statement = change.get("statement")
            normalized_statement = " ".join(str(statement or "").split())
            if not normalized_statement:
                errors.append(f"{state_label}.statement must be a non-empty string")
            elif normalized_statement not in normalized_what_happened:
                errors.append(
                    f"{state_label}.statement must appear verbatim in what_happened"
                )
        basis_ids = {str(value) for value in occurrence.get("basis_ids") or []}
        if not basis_ids:
            errors.append(f"{label}.basis_ids must not be empty")
        for basis_id in basis_ids:
            if basis_id in protected_change_ids:
                errors.append(f"{label}.basis_ids cannot cite a protected change: {basis_id}")
            elif (
                basis_id not in allowed_basis_ids
                and basis_id not in open_contact_by_thread
            ):
                errors.append(f"{label}.basis_ids uses unavailable basis: {basis_id}")

        effect_kind = occurrence.get("effect_kind")
        if effect_kind not in {"realize_development", "advance_open_thread", "self_contained"}:
            errors.append(f"{label}.effect_kind is invalid")
        elif effect_kind == "realize_development":
            development_ids = {str(value) for value in occurrence.get("development_ids") or []}
            if not development_ids:
                errors.append(f"{label}.realize_development must name its development")
        elif effect_kind == "advance_open_thread":
            thread_id = str(occurrence.get("thread_id") or "")
            if not thread_id:
                errors.append(f"{label}.advance_open_thread needs thread_id")
            elif (
                thread_id not in open_thread_ids
                and thread_id not in open_contact_by_thread
            ):
                errors.append(f"{label}.advance_open_thread must use an open thread")
            if not occurrence.get("continues_from"):
                errors.append(f"{label}.advance_open_thread must cite prior contact")
        elif effect_kind == "self_contained":
            if not basis_ids & self_contained_basis_ids:
                errors.append(
                    f"{label}.self_contained must cite a permitted local basis"
                )

        outcome = occurrence.get("assistant_outcome")
        if not require_exact_fields(outcome, ASSISTANT_OUTCOME_FIELDS, f"{label}.assistant_outcome", errors):
            outcome = {}
        kind = outcome.get("kind")
        requested = outcome.get("requested_result")
        if kind not in {"none", "conversation", "external_action"}:
            errors.append(f"{label}.assistant_outcome.kind is invalid")
        elif kind == "none" and requested is not None:
            errors.append(f"{label}.assistant_outcome none must have null requested_result")
        elif kind != "none" and not nonempty_string(requested):
            errors.append(f"{label}.assistant_outcome needs requested_result")
        operations = occurrence.get("app_operations")
        if not isinstance(operations, list):
            errors.append(f"{label}.app_operations must be a list")
            operations = []
        if kind == "external_action" and not operations:
            errors.append(f"{label}.external_action requires an app operation")
        elif kind != "external_action" and operations:
            errors.append(
                f"{label}.app_operations are allowed only for external_action"
            )
        declared_item_sources = {
            str(value)
            for value in occurrence.get("uses_items_created_by_contact_ids") or []
        }
        for source_contact_id in declared_item_sources:
            source_position = operation_contact_positions.get(source_contact_id)
            if source_position is None or source_position >= index:
                errors.append(
                    f"{label}.uses_items_created_by_contact_ids must name an earlier "
                    f"contact in this week: {source_contact_id}"
                )
        for operation_index, operation in enumerate(operations):
            operation_label = f"{label}.app_operations[{operation_index}]"
            if not require_exact_fields(
                operation, OPERATION_FIELDS, operation_label, errors
            ):
                continue
            tool = operation.get("tool")
            args = operation.get("args")
            if tool not in tools:
                errors.append(f"{operation_label} uses unknown tool: {tool}")
                continue
            if not isinstance(args, dict):
                errors.append(f"{operation_label}.args must be an object")
                continue
            schema = tools[str(tool)]
            unknown_args = sorted(set(args) - set(schema.get("arguments") or []))
            missing_args = sorted(
                set(schema.get("required_arguments") or []) - set(args)
            )
            if unknown_args:
                errors.append(f"{operation_label} has unknown args: {unknown_args}")
            if missing_args:
                errors.append(f"{operation_label} lacks required args: {missing_args}")
            errors.extend(
                validate_operation_result_references(
                    args,
                    label=operation_label,
                    current_contact_id=contact_id,
                    current_operation_index=operation_index,
                    contact_positions=operation_contact_positions,
                    operation_counts=operation_counts,
                )
            )
            existing_id_argument = EXISTING_APP_ITEM_ID_ARGUMENT_BY_TOOL.get(
                str(tool)
            )
            supplied_id = args.get(existing_id_argument) if existing_id_argument else None
            if (
                existing_id_argument
                and nonempty_string(supplied_id)
                and str(supplied_id) not in supplied_app_record_ids
            ):
                errors.append(
                    f"{operation_label}.args.{existing_id_argument} must copy an ID "
                    "from available_app_records or earlier_accepted_app_items"
                )
            operation_item_sources = set(_direct_operation_result_contact_ids(args))
            undeclared_item_sources = operation_item_sources - declared_item_sources
            if undeclared_item_sources:
                errors.append(
                    f"{operation_label} uses undeclared same-week item source(s): "
                    f"{sorted(undeclared_item_sources)}"
                )
            validate_concrete_choice_arguments(
                operation_label=operation_label,
                args=args,
                schema=schema,
                errors=errors,
            )
        source_material = occurrence.get("source_material")
        if source_material is not None:
            if require_exact_fields(source_material, SOURCE_MATERIAL_FIELDS, f"{label}.source_material", errors):
                if not all(nonempty_string(source_material.get(field)) for field in SOURCE_MATERIAL_FIELDS):
                    errors.append(f"{label}.source_material fields must be non-empty strings")

        used_fact_references: set[int | str] = set()
        for reference in occurrence.get("uses_fact_ids") or []:
            fact_id = fact_reference(reference)
            if fact_id is None:
                errors.append(f"{label}.uses_fact_ids contains an invalid fact reference")
            elif isinstance(fact_id, int) and fact_id not in known_fact_ids:
                errors.append(f"{label}.uses_fact_ids contains unknown fact {reference}")
            elif fact_id not in available_fact_references:
                errors.append(f"{label}.uses_fact_ids contains stale fact {reference}")
            else:
                used_fact_references.add(fact_id)
        durable_facts = occurrence.get("durable_facts")
        if not isinstance(durable_facts, list):
            errors.append(f"{label}.durable_facts must be a list")
            durable_facts = []
        if effect_kind == "self_contained" and durable_facts:
            errors.append(f"{label}.self_contained cannot establish durable facts")
        if effect_kind == "advance_open_thread" and durable_facts:
            errors.append(f"{label}.advance_open_thread cannot establish durable facts")
        declared = {
            key: fact
            for development_id in occurrence.get("development_ids") or []
            for key, fact in expected_facts.get(str(development_id), {}).items()
        }
        if effect_kind == "realize_development" and durable_facts and not declared:
            errors.append(
                f"{label} cannot establish durable facts before its development "
                "finish window"
            )
        seen_fact_keys: set[str] = set()
        superseded_fact_references: set[int | str] = set()
        occurrence_development_ids = {
            str(value) for value in occurrence.get("development_ids") or []
        }
        references_replaced_by_same_development = {
            reference
            for development_id in occurrence_development_ids
            for reference in superseded_fact_references_by_development.get(
                development_id, set()
            )
        }
        for fact_index, fact in enumerate(durable_facts):
            fact_label = f"{label}.durable_facts[{fact_index}]"
            if not require_exact_fields(fact, PLANNED_FACT_FIELDS, fact_label, errors):
                continue
            key = str(fact.get("key") or "")
            if not key or key in seen_fact_keys:
                errors.append(f"{fact_label}.key must be unique and non-empty")
            elif key in durable_fact_keys_in_window:
                errors.append(f"durable fact key appears more than once in window: {key}")
            else:
                durable_fact_keys_in_window.add(key)
            seen_fact_keys.add(key)
            is_exact_declared_fact = fact == declared.get(key)
            if (
                effect_kind == "realize_development"
                and declared
                and not is_exact_declared_fact
            ):
                errors.append(f"{fact_label} must copy a declared fact from a cited development")
            elif key in required_declared_fact_counts and is_exact_declared_fact:
                required_declared_fact_counts[key] += 1
            for raw_reference in fact.get("supersedes") or []:
                reference = fact_reference(raw_reference)
                if reference is None:
                    errors.append(f"{fact_label}.supersedes has an invalid reference")
                elif (
                    reference not in available_fact_references
                    and reference not in references_replaced_by_same_development
                ):
                    errors.append(
                        f"{fact_label}.supersedes has a stale or future reference: "
                        f"{raw_reference}"
                    )
                else:
                    superseded_fact_references.add(reference)
            for subject in fact.get("subjects") or []:
                if str(subject) not in allowed_entity_ids:
                    errors.append(f"{fact_label} uses unavailable entity: {subject}")
            if key:
                fact_subjects_by_reference[key] = {
                    str(subject)
                    for subject in fact.get("subjects") or []
                    if nonempty_string(subject)
                }
        available_fact_references.difference_update(superseded_fact_references)
        available_fact_references.update(seen_fact_keys)
        for development_id in occurrence_development_ids:
            superseded_fact_references_by_development.setdefault(
                development_id, set()
            ).update(superseded_fact_references)
        thread_after = occurrence.get("thread_after")
        if require_exact_fields(thread_after, THREAD_AFTER_FIELDS, f"{label}.thread_after", errors):
            is_open = thread_after.get("is_open")
            remains = thread_after.get("what_remains")
            if not isinstance(is_open, bool):
                errors.append(f"{label}.thread_after.is_open must be a boolean")
            elif is_open and not nonempty_string(remains):
                errors.append(f"{label}.thread_after needs what_remains when open")
            elif not is_open and remains is not None:
                errors.append(f"{label}.thread_after.what_remains must be null when closed")
            if (
                (is_open is True or occurrence.get("continues_from"))
                and not nonempty_string(occurrence.get("thread_id"))
            ):
                errors.append(
                    f"{label}.thread_id must be non-empty while work is unfinished"
                )
        thread_id = str(occurrence.get("thread_id") or "")
        if thread_id and thread_id in closed_thread_ids:
            errors.append(
                f"{label} reuses thread {thread_id} after an earlier occurrence closed it"
            )
        if (
            isinstance(thread_after, dict)
            and thread_after.get("is_open") is False
            and thread_id in open_thread_current_states_by_id
        ):
            state_changes_by_key = {
                str(change.get("key") or ""): change
                for change in current_state_changes
                if isinstance(change, dict)
            }
            for supplied_state in open_thread_current_states_by_id[thread_id]:
                state_key = str(supplied_state.get("key") or "")
                replacement = state_changes_by_key.get(state_key)
                if replacement is None:
                    errors.append(
                        f"{label} closes thread {thread_id} without updating current-state key {state_key}"
                    )
                    continue
                if replacement.get("subject_ids") != supplied_state.get("subject_ids"):
                    errors.append(
                        f"{label} must keep subject_ids for current-state key {state_key} when closing thread {thread_id}"
                    )
                if " ".join(str(replacement.get("statement") or "").split()) == " ".join(
                    str(supplied_state.get("statement") or "").split()
                ):
                    errors.append(
                        f"{label} must replace the statement for current-state key {state_key} when closing thread {thread_id}"
                    )
        for reference in occurrence.get("continues_from") or []:
            if str(reference) not in prior_contacts:
                errors.append(f"{label}.continues_from has a non-prior reference: {reference}")
        mismatched_thread_ids = sorted(
            {
                open_thread_id_by_reference[str(reference)]
                for reference in occurrence.get("continues_from") or []
                if str(reference) in open_thread_id_by_reference
                and open_thread_id_by_reference[str(reference)] != thread_id
            }
        )
        if mismatched_thread_ids:
            errors.append(
                f"{label} directly continues unfinished thread(s) "
                f"{mismatched_thread_ids} but uses thread_id {thread_id}"
            )
        prior_open_contact_id = open_contact_by_thread.get(thread_id)
        if (
            thread_id
            and prior_open_contact_id is not None
            and prior_open_contact_id not in {
                str(reference) for reference in occurrence.get("continues_from") or []
            }
        ):
            errors.append(
                f"{label} continues same-week thread {thread_id} without "
                f"referencing open contact {prior_open_contact_id}"
            )
        basis_subject_ids = set(primary_subject_ids)
        for basis_id in basis_ids:
            basis_subject_ids.update(subjects_by_basis.get(basis_id, set()))
        for development_id in occurrence.get("development_ids") or []:
            basis_subject_ids.update(subjects_by_basis.get(str(development_id), set()))
        for fact_id in used_fact_references:
            basis_subject_ids.update(fact_subjects_by_reference.get(fact_id, set()))
        for entity_id in occurrence.get("subject_ids") or []:
            if str(entity_id) not in allowed_entity_ids:
                errors.append(f"{label}.subject_ids uses unknown entity: {entity_id}")
            elif subjects_by_basis and str(entity_id) not in basis_subject_ids:
                errors.append(
                    f"{label}.subject_ids uses entity outside its cited basis: {entity_id}"
                )
        for development_id in occurrence.get("development_ids") or []:
            development_id = str(development_id)
            if development_id not in allowed_development_ids:
                errors.append(f"{label} uses a development outside this week: {development_id}")
            if (
                local_when is not None
                and development_id in development_start_dates
                and local_when.date() < development_start_dates[development_id]
            ):
                errors.append(
                    f"{label} cites development {development_id} before its accepted "
                    f"start date {development_start_dates[development_id].isoformat()}"
                )
            if development_id in development_counts:
                development_counts[development_id] += 1
        if occurrence.get("anchor_item_ids"):
            errors.append(f"{label}.anchor_item_ids must be empty")
        if isinstance(thread_after, dict) and isinstance(
            thread_after.get("is_open"), bool
        ) and thread_id:
            if thread_after["is_open"]:
                open_contact_by_thread[thread_id] = contact_id
                open_thread_id_by_reference[contact_id] = thread_id
            else:
                open_contact_by_thread.pop(thread_id, None)
                closed_thread_ids.add(thread_id)
                open_thread_id_by_reference = {
                    reference: value
                    for reference, value in open_thread_id_by_reference.items()
                    if value != thread_id
                }
        prior_contacts.add(contact_id)

    if any(not value for value in contact_ids) or len(contact_ids) != len(set(contact_ids)):
        errors.append("occurrences must have unique non-empty contact_id values")
    collisions = sorted(set(contact_ids) & (existing_references | existing_session_ids))
    if collisions:
        errors.append(f"contact IDs already exist in accepted history: {collisions}")
    missing = sorted(value for value, count in development_counts.items() if count == 0)
    if missing:
        errors.append(f"plan does not complete required developments: {missing}")
    missing_declared_fact_keys = sorted(
        key for key, count in required_declared_fact_counts.items() if count == 0
    )
    if missing_declared_fact_keys:
        errors.append(
            "required developments are missing declared durable facts: "
            + ", ".join(missing_declared_fact_keys)
        )
    repeated_declared_fact_keys = sorted(
        key for key, count in required_declared_fact_counts.items() if count > 1
    )
    if repeated_declared_fact_keys:
        errors.append(
            "required developments establish declared durable facts more than once: "
            + ", ".join(repeated_declared_fact_keys)
        )
    return list(dict.fromkeys(errors))


def validate_writer_prose_response(
    response: dict[str, Any], *, start: date, end: date, occurrence_plan: dict[str, Any]
) -> list[str]:
    """Validate Call 2's one-to-one realization of fixed occurrences."""
    return _validate_writer_prose_response_v2(
        response, start=start, end=end, occurrence_plan=occurrence_plan
    )


def _validate_writer_prose_response_v2(
    response: dict[str, Any], *, start: date, end: date, occurrence_plan: dict[str, Any]
) -> list[str]:
    """The writer may change wording and implement only authorized actions."""
    errors: list[str] = []
    if not isinstance(response, dict):
        return ["writer response must be an object containing plan"]
    if visible_fields(response) != {"plan"}:
        errors.append("writer response must contain exactly plan")
    plan = response.get("plan")
    if not require_exact_fields(plan, WRITER_PLAN_FIELDS, "plan", errors):
        return errors
    if plan.get("period_start") != start.isoformat():
        errors.append("plan.period_start does not match start-date")
    if plan.get("period_end") != end.isoformat():
        errors.append("plan.period_end does not match end-date")
    occurrences = occurrence_plan.get("occurrences")
    sessions = plan.get("sessions")
    if not isinstance(occurrences, list) or not isinstance(sessions, list):
        errors.append("plan.sessions and accepted occurrences must be lists")
        return errors
    if len(sessions) != len(occurrences):
        errors.append(
            "plan.sessions must correspond one-to-one with accepted occurrences: "
            f"expected {len(occurrences)}, got {len(sessions)}"
        )
        return errors
    for index, (session, occurrence) in enumerate(zip(sessions, occurrences)):
        if not isinstance(session, dict) or not isinstance(occurrence, dict):
            continue
        label = f"sessions[{index}]"
        if not require_exact_fields(session, WRITER_SESSION_FIELDS, label, errors):
            continue
        if session.get("contact_id") != occurrence.get("contact_id"):
            errors.append(f"{label}.contact_id must match the accepted occurrence in order")
        messages_before_request = session.get("messages_before_request")
        if not string_list(messages_before_request):
            errors.append(
                f"{label}.messages_before_request must be a list of non-empty strings"
            )
            messages_before_request = []
        request = session.get("message_with_request")
        outcome_kind = (occurrence.get("assistant_outcome") or {}).get("kind")
        if outcome_kind == "none":
            if request is not None:
                errors.append(f"{label}.message_with_request must be null for an update")
            if not messages_before_request:
                errors.append(
                    f"{label}.messages_before_request must contain an update when no request is planned"
                )
        elif not nonempty_string(request):
            errors.append(
                f"{label}.message_with_request must be a non-empty string when a request is planned"
            )
        messages = [*messages_before_request]
        if isinstance(request, str):
            messages.append(request)
        for message_index, message in enumerate(messages):
            if "app_operations_placeholder" in message.casefold():
                errors.append(
                    f"{label}.messages[{message_index}] contains the forbidden "
                    "internal marker app_operations_placeholder"
                )
        conflicting_fact_ids = session.get("conflicting_fact_ids")
        if not isinstance(conflicting_fact_ids, list) or any(
            type(fact_id) is not int for fact_id in conflicting_fact_ids
        ) or len(conflicting_fact_ids) != len(set(conflicting_fact_ids)):
            errors.append(f"{label}.conflicting_fact_ids must be a unique list of integers")
            conflicting_fact_ids = []
        if conflicting_fact_ids and (
            occurrence.get("effect_kind") != "self_contained"
            or occurrence.get("development_ids")
            or occurrence.get("durable_facts")
            or occurrence.get("app_operations")
        ):
            errors.append(
                f"{label}.conflicting_fact_ids may be non-empty only for an ordinary "
                "self-contained contact with no development IDs, durable facts, or app operation"
            )
        elif conflicting_fact_ids:
            errors.append(
                f"{label} reports that the fixed contact conflicts with current facts: "
                + ", ".join(str(fact_id) for fact_id in conflicting_fact_ids)
            )
    return list(dict.fromkeys(errors))


def construct_writer_plan(
    writer_response: dict[str, Any],
    *,
    occurrence_plan: dict[str, Any],
    required_entities: list[dict[str, Any]],
) -> dict[str, Any]:
    """Combine fixed occurrence identity with Call 2's realized session state."""
    writer_plan = writer_response["plan"]
    occurrences = occurrence_plan["occurrences"]
    contact_by_development: dict[str, str] = {}
    for occurrence in occurrences:
        for development_id in occurrence.get("development_ids") or []:
            contact_by_development.setdefault(
                str(development_id), str(occurrence["contact_id"])
            )
    new_entities = [
        {
            "id": entity["id"],
            "name": entity["name"],
            "kind": entity["kind"],
            "role": entity["role"],
            "reason": entity["role"],
            "introduced_in_contact_id": contact_by_development[
                str(entity["introduced_in_event_id"])
            ],
        }
        for entity in required_entities
    ]
    sessions = []
    for occurrence, writer_session in zip(occurrences, writer_plan["sessions"]):
        contact_id = str(occurrence["contact_id"])
        sessions.append(
            {
                "contact_id": contact_id,
                "narrative_date": copy.deepcopy(occurrence["narrative_date"]),
                "thread_id": copy.deepcopy(occurrence["thread_id"]),
                "continues_from": copy.deepcopy(occurrence["continues_from"]),
                "subject_ids": copy.deepcopy(occurrence["subject_ids"]),
                "development_ids": copy.deepcopy(occurrence["development_ids"]),
                "anchor_item_ids": copy.deepcopy(occurrence["anchor_item_ids"]),
                "purpose": copy.deepcopy(
                    occurrence["assistant_outcome"].get("requested_result")
                    or occurrence["what_happened"]
                ),
                "messages": [
                    *copy.deepcopy(writer_session["messages_before_request"]),
                    *(
                        [copy.deepcopy(writer_session["message_with_request"])]
                        if isinstance(writer_session["message_with_request"], str)
                        else []
                    ),
                ],
                "uses_facts": copy.deepcopy(occurrence["uses_fact_ids"]),
                "new_facts": [
                    {**copy.deepcopy(fact), "evidence_contact_ids": [contact_id]}
                    for fact in occurrence["durable_facts"]
                ],
                "app_operations": copy.deepcopy(occurrence["app_operations"]),
                "thread_open_after_contact": copy.deepcopy(
                    occurrence["thread_after"]["is_open"]
                ),
                "what_remains_after_contact": copy.deepcopy(
                    occurrence["thread_after"]["what_remains"]
                ),
            }
        )
    return {
        "period_start": occurrence_plan["period_start"],
        "period_end": occurrence_plan["period_end"],
        "new_entities": new_entities,
        "sessions": sessions,
    }


def validate_history_window_response(
    response: dict[str, Any],
    *,
    start: date,
    end: date,
    timezone_name: str,
    facts: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    tools: dict[str, dict[str, Any]],
    existing_session_ids: set[str],
    existing_references: set[str],
    required_development_ids: set[str],
    allowed_development_ids: set[str],
    required_entities: list[dict[str, Any]],
    required_facts: dict[str, dict[str, Any]],
    occurrence_plan: dict[str, Any],
) -> list[str]:
    tools = window_tools(tools)
    errors: list[str] = []
    if not isinstance(response, dict):
        return ["response must be an object containing plan"]
    if visible_fields(response) != {"plan"}:
        errors.append("response must contain exactly plan")
    plan = response.get("plan")
    if not require_exact_fields(plan, FINAL_PLAN_FIELDS, "plan", errors):
        return errors
    if plan.get("period_start") != start.isoformat():
        errors.append("plan.period_start does not match start-date")
    if plan.get("period_end") != end.isoformat():
        errors.append("plan.period_end does not match end-date")

    sessions = plan.get("sessions")
    new_entities = plan.get("new_entities")
    if not isinstance(sessions, list) or not sessions:
        errors.append("plan.sessions must be a non-empty list")
        sessions = []
    if not isinstance(new_entities, list):
        errors.append("plan.new_entities must be a list")
        new_entities = []
    contact_ids: list[str] = []
    for index, session in enumerate(sessions):
        label = f"sessions[{index}]"
        if require_exact_fields(session, SESSION_FIELDS, label, errors):
            contact_ids.append(str(session.get("contact_id") or ""))
    if any(not value for value in contact_ids) or len(contact_ids) != len(set(contact_ids)):
        errors.append("sessions must have unique non-empty contact_id values")
    collisions = sorted(set(contact_ids) & (existing_references | existing_session_ids))
    if collisions:
        errors.append(f"contact IDs already exist in accepted history: {collisions}")
    contact_positions = {value: index for index, value in enumerate(contact_ids)}
    operation_contact_positions, operation_counts = _operation_reference_context(sessions)
    required_entity_by_id = {
        str(row.get("id") or ""): row for row in required_entities if isinstance(row, dict)
    }
    actual_entity_by_id: dict[str, dict[str, Any]] = {}
    existing_entity_ids = {
        str(row.get("id")) for row in entities if nonempty_string(row.get("id"))
    }
    for index, entity in enumerate(new_entities):
        label = f"new_entities[{index}]"
        if not require_exact_fields(entity, ENTITY_FIELDS, label, errors):
            continue
        if not all(nonempty_string(entity.get(field)) for field in ENTITY_FIELDS):
            errors.append(f"{label} fields must all be non-empty strings")
            continue
        entity_id = str(entity["id"])
        if entity_id in existing_entity_ids or entity_id in actual_entity_by_id:
            errors.append(f"{label}.id is already known or duplicated: {entity_id}")
        actual_entity_by_id[entity_id] = entity
        intro = str(entity["introduced_in_contact_id"])
        if intro not in contact_positions:
            errors.append(f"{label} names an unknown introduction contact")
    if set(actual_entity_by_id) != set(required_entity_by_id):
        errors.append(
            "plan.new_entities must contain exactly the required new entity IDs: "
            f"{sorted(required_entity_by_id)}"
        )
    for entity_id in set(actual_entity_by_id) & set(required_entity_by_id):
        actual = actual_entity_by_id[entity_id]
        required = required_entity_by_id[entity_id]
        for field in ("name", "kind", "role"):
            if actual.get(field) != required.get(field):
                errors.append(f"new entity {entity_id} changes accepted {field}")
        if actual.get("reason") != required.get("role"):
            errors.append(
                f"new entity {entity_id} reason must preserve its accepted role"
            )

    introduced_at = {
        entity_id: contact_positions.get(str(row.get("introduced_in_contact_id")), -1)
        for entity_id, row in actual_entity_by_id.items()
    }
    known_fact_ids = {
        int(row["id"])
        for row in facts
        if isinstance(row.get("id"), int) and not isinstance(row.get("id"), bool)
    }
    current_facts: set[int | str] = set(current_existing_fact_ids(facts))
    superseded_fact_references_by_development: dict[str, set[int | str]] = {}
    inherited_source_origins: dict[int | str, list[tuple[str, str]]] = {}
    for row in facts:
        fact_id = fact_reference(row.get("id")) if isinstance(row, dict) else None
        if not isinstance(fact_id, int):
            continue
        sources = row.get("source_session_ids") or []
        if not isinstance(sources, list) or not all(nonempty_string(value) for value in sources):
            continue
        inherited_source_origins[fact_id] = [
            ("accepted_session", str(value)) for value in sources
        ]
    planned_fact_keys: set[str] = set()
    found_facts: dict[str, tuple[dict[str, Any], str]] = {}
    development_counts = {value: 0 for value in required_development_ids}
    previous_time: datetime | None = None
    prior_contacts = set(existing_references)

    for index, session in enumerate(sessions):
        if not isinstance(session, dict):
            continue
        label = f"sessions[{index}]"
        contact_id = str(session.get("contact_id") or "")
        local_date: date | None = None
        try:
            when = datetime.fromisoformat(str(session.get("narrative_date") or ""))
            if when.tzinfo is None:
                raise ValueError
            local_when = when.astimezone(ZoneInfo(timezone_name))
            local_date = local_when.date()
            expected_offset = local_when.replace(tzinfo=None).replace(
                tzinfo=ZoneInfo(timezone_name)
            ).utcoffset()
            if when.utcoffset() != expected_offset:
                errors.append(f"{label}.narrative_date offset does not match {timezone_name}")
            if not start <= local_when.date() <= end:
                errors.append(f"{label}.narrative_date is outside the requested dates")
            if previous_time is not None and when <= previous_time:
                errors.append("sessions must be strictly chronological")
            previous_time = when
        except ValueError:
            errors.append(f"{label}.narrative_date must be an offset-aware ISO datetime")

        if not nonempty_string(session.get("purpose")):
            errors.append(f"{label}.purpose must be a non-empty string")
        for field in (
            "continues_from",
            "subject_ids",
            "development_ids",
            "anchor_item_ids",
        ):
            if not string_list(session.get(field)):
                errors.append(f"{label}.{field} must be a unique list of strings")
        messages = session.get("messages")
        if not string_list(messages, allow_empty=False):
            errors.append(f"{label}.messages must be a non-empty list of strings")
            messages = []
        for reference in session.get("continues_from") or []:
            if str(reference) not in prior_contacts:
                errors.append(f"{label}.continues_from has a non-prior reference: {reference}")

        for entity_id in session.get("subject_ids") or []:
            if entity_id in existing_entity_ids:
                continue
            if entity_id not in actual_entity_by_id or introduced_at[entity_id] > index:
                errors.append(f"{label}.subject_ids uses unavailable entity: {entity_id}")

        for development_id in session.get("development_ids") or []:
            if development_id not in allowed_development_ids:
                errors.append(f"{label} uses a development outside this window: {development_id}")
            if development_id in development_counts:
                development_counts[development_id] += 1
        if session.get("anchor_item_ids"):
            errors.append(f"{label}.anchor_item_ids must be empty")

        uses = session.get("uses_facts")
        if not isinstance(uses, list) or len(uses) != len(set(map(str, uses or []))):
            errors.append(f"{label}.uses_facts must be a unique list")
            uses = []
        for raw_reference in uses:
            reference = fact_reference(raw_reference)
            if reference is None:
                errors.append(f"{label}.uses_facts contains an invalid reference")
            elif isinstance(reference, int) and reference not in known_fact_ids:
                errors.append(f"{label}.uses_facts contains unknown fact {reference}")
            elif reference not in current_facts:
                errors.append(f"{label}.uses_facts contains stale or future fact {reference}")

        new_facts = session.get("new_facts")
        if not isinstance(new_facts, list):
            errors.append(f"{label}.new_facts must be a list")
            new_facts = []
        facts_current_before_session = set(current_facts)
        session_development_ids = {
            str(value) for value in session.get("development_ids") or []
        }
        references_replaced_by_same_development = {
            reference
            for development_id in session_development_ids
            for reference in superseded_fact_references_by_development.get(
                development_id, set()
            )
        }
        facts_superseded_in_session: set[int | str] = set()
        fact_keys_added_in_session: set[str] = set()
        for fact_index, fact in enumerate(new_facts):
            fact_label = f"{label}.new_facts[{fact_index}]"
            if not require_exact_fields(fact, FACT_FIELDS, fact_label, errors):
                continue
            key = str(fact.get("key") or "")
            if not key or key.isdigit() or key in planned_fact_keys:
                errors.append(f"{fact_label}.key must be a unique non-numeric string")
                continue
            declaration = required_facts.get(key)
            if declaration is not None:
                for field in ("statement", "applies_when", "subjects", "supersedes"):
                    if fact.get(field) != declaration.get(field):
                        errors.append(f"{fact_label}.{field} changes the accepted fact declaration")
                if "development_ids" in declaration:
                    missing_fact_developments = sorted(
                        set(declaration["development_ids"])
                        - set(session.get("development_ids") or [])
                    )
                    if missing_fact_developments:
                        errors.append(
                            f"{fact_label} omits accepted development_ids: "
                            f"{missing_fact_developments}"
                        )
                found_facts[key] = (fact, contact_id)
            if not nonempty_string(fact.get("statement")) or not nonempty_string(
                fact.get("applies_when")
            ):
                errors.append(f"{fact_label} needs statement and applies_when")
            if not string_list(fact.get("subjects"), allow_empty=False):
                errors.append(f"{fact_label}.subjects must be a non-empty unique string list")
            for entity_id in fact.get("subjects") or []:
                if entity_id not in existing_entity_ids and (
                    entity_id not in actual_entity_by_id or introduced_at[entity_id] > index
                ):
                    errors.append(f"{fact_label} uses unavailable entity: {entity_id}")
            evidence = fact.get("evidence_contact_ids")
            valid_evidence_contact_ids: list[str] = []
            if not string_list(evidence, allow_empty=False):
                errors.append(f"{fact_label}.evidence_contact_ids must be non-empty")
            else:
                for evidence_id in evidence:
                    if evidence_id not in contact_positions or contact_positions[evidence_id] > index:
                        errors.append(f"{fact_label} has future or unknown evidence: {evidence_id}")
                    else:
                        valid_evidence_contact_ids.append(str(evidence_id))
            supersedes = fact.get("supersedes")
            if not isinstance(supersedes, list) or len(supersedes) != len(
                set(map(str, supersedes or []))
            ):
                errors.append(f"{fact_label}.supersedes must be a unique list")
                supersedes = []
            resolved: list[int | str] = []
            for raw_reference in supersedes:
                reference = fact_reference(raw_reference)
                if reference is None:
                    errors.append(f"{fact_label}.supersedes has an invalid reference")
                elif isinstance(reference, int) and reference not in known_fact_ids:
                    errors.append(f"{fact_label}.supersedes has unknown fact {reference}")
                elif (
                    reference not in facts_current_before_session
                    and reference not in references_replaced_by_same_development
                ):
                    errors.append(f"{fact_label}.supersedes has stale or future fact {reference}")
                else:
                    resolved.append(reference)
            if resolved and not valid_evidence_contact_ids:
                errors.append(
                    f"{fact_label} replacement needs at least one current-window evidence contact"
                )
            for reference in resolved:
                for origin_kind, origin_id in inherited_source_origins.get(reference, []):
                    if origin_kind == "accepted_session" and origin_id not in existing_session_ids:
                        errors.append(
                            f"{fact_label} inherits missing accepted-history source session: "
                            f"{origin_id}"
                        )
                    elif (
                        origin_kind == "window_contact"
                        and (
                            origin_id not in contact_positions
                            or contact_positions[origin_id] > index
                        )
                    ):
                        errors.append(
                            f"{fact_label} inherits source from a future or unknown window contact: "
                            f"{origin_id}"
                        )
            facts_superseded_in_session.update(resolved)
            planned_fact_keys.add(key)
            fact_keys_added_in_session.add(key)
            inherited_source_origins[key] = [
                *(origin for reference in resolved for origin in inherited_source_origins.get(reference, [])),
                *(("window_contact", evidence_id) for evidence_id in valid_evidence_contact_ids),
            ]

        current_facts.difference_update(facts_superseded_in_session)
        current_facts.update(fact_keys_added_in_session)
        for development_id in session_development_ids:
            superseded_fact_references_by_development.setdefault(
                development_id, set()
            ).update(facts_superseded_in_session)

        is_open = session.get("thread_open_after_contact")
        remains = session.get("what_remains_after_contact")
        if not isinstance(is_open, bool):
            errors.append(f"{label}.thread_open_after_contact must be a boolean")
        elif is_open and not nonempty_string(remains):
            errors.append(f"{label}.what_remains_after_contact must explain open work")
        elif not is_open and remains is not None:
            errors.append(f"{label}.what_remains_after_contact must be null when closed")
        if (
            (is_open is True or session.get("continues_from"))
            and not nonempty_string(session.get("thread_id"))
        ):
            errors.append(
                f"{label}.thread_id must be non-empty while work is unfinished"
            )

        operations = session.get("app_operations")
        if not isinstance(operations, list):
            errors.append(f"{label}.app_operations must be a list")
            operations = []
        for operation_index, operation in enumerate(operations):
            operation_label = f"{label}.app_operations[{operation_index}]"
            if not require_exact_fields(
                operation, OPERATION_FIELDS, operation_label, errors
            ):
                continue
            tool = operation.get("tool")
            args = operation.get("args")
            if tool not in tools:
                errors.append(f"{operation_label} uses unknown tool: {tool}")
                continue
            if not isinstance(args, dict):
                errors.append(f"{operation_label}.args must be an object")
                continue
            schema = tools[str(tool)]
            unknown_args = sorted(set(args) - set(schema.get("arguments") or []))
            missing_args = sorted(set(schema.get("required_arguments") or []) - set(args))
            if unknown_args:
                errors.append(f"{operation_label} has unknown args: {unknown_args}")
            if missing_args:
                errors.append(f"{operation_label} lacks required args: {missing_args}")
            errors.extend(
                validate_operation_result_references(
                    args,
                    label=operation_label,
                    current_contact_id=contact_id,
                    current_operation_index=operation_index,
                    contact_positions=operation_contact_positions,
                    operation_counts=operation_counts,
                )
            )
            validate_concrete_choice_arguments(
                operation_label=operation_label,
                args=args,
                schema=schema,
                errors=errors,
            )
        prior_contacts.add(contact_id)

    missing_developments = sorted(
        value for value, count in development_counts.items() if count == 0
    )
    if missing_developments:
        errors.append(f"plan does not complete required developments: {missing_developments}")
    if required_facts and set(found_facts) != set(required_facts):
        errors.append(
            "plan.new_facts must contain exactly the required quarter fact keys: "
            f"{sorted(required_facts)}"
        )
    return list(dict.fromkeys(errors))


def assign_window_session_ids(
    plan: dict[str, Any],
    *,
    persona: str,
    timezone_name: str,
    existing_session_ids: set[str],
) -> dict[str, Any]:
    assigned = copy.deepcopy(plan)
    used = set(existing_session_ids)
    next_number_by_date: dict[str, int] = {}
    zone = ZoneInfo(timezone_name)
    for session in assigned["sessions"]:
        when = datetime.fromisoformat(str(session["narrative_date"]))
        local_date = when.astimezone(zone).date().strftime("%Y_%m_%d")
        number = next_number_by_date.get(local_date, 1)
        while True:
            candidate = f"extension_{persona}_{local_date}_{number:03d}"
            number += 1
            if candidate not in used:
                break
        session["session_id"] = candidate
        used.add(candidate)
        next_number_by_date[local_date] = number
    return assigned


def update_unfinished_threads(
    existing: list[dict[str, Any]], plan: dict[str, Any]
) -> list[dict[str, Any]]:
    """Keep only the latest open state for each thread ID."""
    unkeyed: list[dict[str, Any]] = []
    keyed: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in existing:
        thread_id = str(row.get("thread_id") or "") if isinstance(row, dict) else ""
        if not thread_id:
            unkeyed.append(copy.deepcopy(row))
            continue
        if thread_id in order:
            order.remove(thread_id)
        order.append(thread_id)
        keyed[thread_id] = copy.deepcopy(row)
    for session in plan["sessions"]:
        thread_id = str(session["thread_id"])
        continued_references = {
            str(reference) for reference in session.get("continues_from") or []
        }
        continued_thread_ids = {
            existing_thread_id
            for existing_thread_id, row in keyed.items()
            if continued_references
            & {
                str(row.get("interaction_id") or ""),
                str(row.get("session_id") or ""),
            }
        }
        for continued_thread_id in continued_thread_ids:
            if continued_thread_id in order:
                order.remove(continued_thread_id)
            keyed.pop(continued_thread_id, None)
        if thread_id in order:
            order.remove(thread_id)
        keyed.pop(thread_id, None)
        if session["thread_open_after_contact"]:
            order.append(thread_id)
            keyed[thread_id] = {
                "interaction_id": session["contact_id"],
                "session_id": session["session_id"],
                "date": str(session["narrative_date"])[:10],
                "thread_id": thread_id,
                "event_ids": list(session["development_ids"]),
                "subjects": list(session["subject_ids"]),
                "contact_summary": session["purpose"],
                "thread_open_after_contact": True,
                "what_remains_after_contact": session["what_remains_after_contact"],
                "unfinished_status": {"source": "history_window"},
            }
    return [*unkeyed, *[keyed[thread_id] for thread_id in order]]


def materialize_window_state(
    *,
    starting_state: dict[str, Any],
    plan: dict[str, Any],
    projected_app_state: dict[str, Any],
    inherited_source_fact_ids_by_key: dict[str, list[int]] | None = None,
    accepted_contact_details_by_contact_id: dict[str, str] | None = None,
    current_state_changes_by_contact_id: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    accepted_contact_details_by_contact_id = (
        accepted_contact_details_by_contact_id or {}
    )
    current_state_changes_by_contact_id = current_state_changes_by_contact_id or {}
    compatibility_plan = {
        "new_entities": plan["new_entities"],
        "contacts": plan["sessions"],
    }
    entities, added_entities = materialize_entities(
        starting_state["entities"], compatibility_plan
    )
    facts, added_facts, _ = materialize_facts(
        starting_state["facts"],
        compatibility_plan,
        accepted_history=starting_state["history"],
        inherited_source_fact_ids_by_key=inherited_source_fact_ids_by_key,
    )
    added_history = [
        {
            "id": session["session_id"],
            "narrative_date": session["narrative_date"],
            "messages": copy.deepcopy(session["messages"]),
        }
        for session in plan["sessions"]
    ]
    added_purposes = [
        {
            "session_id": session["session_id"],
            "narrative_date": session["narrative_date"],
            "purpose": session["purpose"],
            "accepted_contact_details": (
                accepted_contact_details_by_contact_id.get(session["contact_id"])
                or session["purpose"]
            ),
            "event_ids": list(session["development_ids"]),
            "planned_interaction_id": session["contact_id"],
            "anchor_item_ids": list(session["anchor_item_ids"]),
            "thread_id": session["thread_id"],
            "subject_ids": list(session["subject_ids"]),
            "current_state_changes": copy.deepcopy(
                current_state_changes_by_contact_id.get(session["contact_id"], [])
            ),
        }
        for session in plan["sessions"]
    ]
    state = {
        "history": [*copy.deepcopy(starting_state["history"]), *added_history],
        "session_purposes": [
            *copy.deepcopy(starting_state["session_purposes"]),
            *added_purposes,
        ],
        "entities": entities,
        "facts": facts,
        "app_state": copy.deepcopy(projected_app_state),
        "unfinished_threads": update_unfinished_threads(
            starting_state.get("unfinished_threads") or [], plan
        ),
    }
    return state, added_entities, added_facts


def emit_candidate_checkpoint(
    *,
    path: Path,
    run_output: Path,
    save_kwargs: dict[str, Any],
    save: Callable[..., dict[str, Any]] = save_checkpoint,
    validate: Callable[[Path], str] = validated_checkpoint_identity,
) -> Path:
    expected = run_output / "candidate_checkpoint"
    if path != expected:
        raise ValueError("candidate checkpoint must be inside this run output")
    if path.exists():
        raise CompletedOutputError(f"candidate checkpoint already exists: {path}")
    pending = run_output / "candidate_checkpoint.pending"
    if pending.exists():
        if not pending.is_dir():
            raise ValueError(f"pending checkpoint path is not a directory: {pending}")
        shutil.rmtree(pending)
    try:
        save(path=pending, **save_kwargs)
        validate(pending)
        pending.rename(path)
    except Exception:
        if pending.is_dir():
            shutil.rmtree(pending)
        raise
    return path


def _operation_result_identity(
    row: dict[str, Any], *, index: int
) -> tuple[str, str, str, str]:
    if not isinstance(row, dict):
        raise ValueError(f"operation result {index} must be an object")
    session_id = str(row.get("session_id") or "").strip()
    operation = row.get("operation")
    tool = (
        str(operation.get("tool") or "").strip()
        if isinstance(operation, dict)
        else ""
    )
    if not session_id or not tool:
        raise ValueError(
            f"operation result {index} must contain session_id and operation.tool"
        )
    replay_epoch_ms = row.get("replay_epoch_ms")
    if replay_epoch_ms is None:
        return ("legacy-row", session_id, tool, data_hash(row))
    if isinstance(replay_epoch_ms, bool) or not isinstance(replay_epoch_ms, int):
        raise ValueError(f"operation result {index} replay_epoch_ms must be an integer")
    return ("replay", session_id, tool, str(replay_epoch_ms))


def cumulative_operation_results(
    starting_rows: list[dict[str, Any]], current_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Append current operation rows while rejecting duplicate replay identities."""
    if not isinstance(starting_rows, list) or not isinstance(current_rows, list):
        raise ValueError("operation results must be lists")
    cumulative = [*copy.deepcopy(starting_rows), *copy.deepcopy(current_rows)]
    seen: dict[tuple[str, str, str, str], int] = {}
    for index, row in enumerate(cumulative):
        identity = _operation_result_identity(row, index=index)
        if identity in seen:
            raise ValueError(
                "duplicate operation result identity at rows "
                f"{seen[identity]} and {index}: {identity[1:]}"
            )
        seen[identity] = index
    return cumulative


def write_window_operation_results(
    *,
    run_output: Path,
    starting_rows: list[dict[str, Any]],
    current_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep the run artifact local and return cumulative rows for a v2 checkpoint."""
    cumulative = cumulative_operation_results(starting_rows, current_rows)
    dump_json(run_output / "operation_results.json", current_rows)
    return cumulative


def system_hash(persona: str) -> str:
    """Fingerprint every canonical contract that can change window output."""
    paths = [
        ROOT / "construction" / "build_history_window.py",
        ROOT / "construction" / "checkpoints.py",
        ROOT / "construction" / "quarter_state.py",
        ROOT / "construction" / "construction_io.py",
        ROOT / "construction" / "runtime_inputs.py",
        ROOT / "construction" / "runtime_model_calls.py",
        ROOT / "construction" / "runtime_operations.py",
        ROOT / "construction" / "long_history_prompts.py",
        ROOT / "construction" / "model_input_text.py",
        ROOT / "construction" / "llm.py",
        ROOT / "construction" / "execute_mock_tool.py",
        ROOT / "mock_mcp" / "server.py",
        ROOT / "mock_mcp" / "manifests" / f"{persona}.yaml",
        ROOT / 'mock_mcp' / 'tool_contracts.yaml',
    ]
    return data_hash(
        {
            "files": {
                str(path.relative_to(ROOT)): file_hash(path) for path in paths
            },
            "prompts": {
                "story": HISTORY_WINDOW_STORY_SYSTEM,
                "planner": HISTORY_WINDOW_PLANNING_SYSTEM,
                "contact_correction": HISTORY_WINDOW_CONTACT_CORRECTION_SYSTEM,
                "writer": HISTORY_WINDOW_WRITING_SYSTEM,
            },
        }
    )


def rendered_sessions(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "contact_id": row["contact_id"],
            "session_id": row["session_id"],
            "narrative_date": row["narrative_date"],
            "messages": copy.deepcopy(row["messages"]),
        }
        for row in plan["sessions"]
    ]


def mark_stage(output_dir: Path, stage: str) -> None:
    dump_json(output_dir / "work" / "stage.json", {"stage": stage})


def decode_api_app_operations(
    operations: Any, *, label: str
) -> list[dict[str, Any]]:
    """Decode strict API operation objects into the internal operation shape."""
    if not isinstance(operations, list):
        raise ValueError(f"{label} must be a list")
    converted: list[dict[str, Any]] = []
    for operation_index, operation in enumerate(operations):
        operation_label = f"{label}[{operation_index}]"
        if not isinstance(operation, dict) or set(operation) != {"tool", "args_json"}:
            raise ValueError(
                f"{operation_label} must contain exactly tool and args_json"
            )
        tool = operation.get("tool")
        raw_args = operation.get("args_json")
        if (
            isinstance(tool, str)
            and isinstance(raw_args, str)
            and not tool.strip()
            and not raw_args.strip()
        ):
            continue
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError(f"{operation_label}.tool must be a non-empty string")
        if not isinstance(raw_args, str):
            raise ValueError(f"{operation_label}.args_json must be a string")
        try:
            args = json.loads(raw_args, strict=False)
        except json.JSONDecodeError as exc:
            try:
                prefix, end = json.JSONDecoder(strict=False).raw_decode(raw_args)
            except json.JSONDecodeError:
                prefix, end = None, 0
            if isinstance(prefix, dict) and raw_args[end:].strip() in {"]", "],"}:
                args = prefix
            else:
                try:
                    args = json.loads(raw_args.rstrip() + "}", strict=False)
                except json.JSONDecodeError:
                    raise ValueError(
                        f"{operation_label}.args_json must contain valid JSON"
                    ) from exc
        if not isinstance(args, dict):
            raise ValueError(
                f"{operation_label}.args_json must decode to an object"
            )
        converted.append({"tool": operation.get("tool"), "args": args})
    return converted


def normalize_writer_messages(response: dict[str, Any]) -> dict[str, Any]:
    """Remove blank setup messages without altering the required request field."""
    decoded = copy.deepcopy(response)
    plan = decoded.get("plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("sessions"), list):
        return decoded
    for session_index, session in enumerate(plan["sessions"]):
        if not isinstance(session, dict):
            continue
        messages = session.get("messages_before_request")
        if isinstance(messages, list):
            session["messages_before_request"] = [
                message
                for message in messages
                if not (isinstance(message, str) and not message.strip())
            ]
    return decoded


def normalize_writer_contact_order(
    response: dict[str, Any], contact_ids: list[str]
) -> dict[str, Any]:
    """Convert the keyed API response into the ordered internal session list."""
    normalized = copy.deepcopy(response)
    plan = normalized.get("plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("sessions"), dict):
        raise ValueError("writer response plan.sessions must be an object keyed by contact_id")
    keyed_sessions = plan["sessions"]
    expected = list(contact_ids)
    if set(keyed_sessions) != set(expected) or len(keyed_sessions) != len(expected):
        raise ValueError(
            "writer response must contain exactly the planned contact IDs"
        )
    plan["sessions"] = [
        {
            "contact_id": contact_id,
            **copy.deepcopy(keyed_sessions[contact_id]),
        }
        for contact_id in expected
    ]
    return normalized


def decode_planner_app_operations(response: dict[str, Any]) -> dict[str, Any]:
    """Normalize planner fact references and restore Python-owned metadata."""
    decoded = copy.deepcopy(response)
    plan = decoded.get("plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("occurrences"), list):
        return decoded
    original_key_by_normalized_key: dict[str, str] = {}
    open_contact_by_thread: dict[str, str] = {}
    open_thread_id_by_reference: dict[str, str] = {}
    for index, occurrence in enumerate(plan["occurrences"]):
        if not isinstance(occurrence, dict):
            continue
        occurrence["app_operations"] = decode_api_app_operations(
            occurrence.get("app_operations"),
            label=f"occurrences[{index}].app_operations",
        )
        current_state_changes = occurrence.get("current_state_changes")
        if isinstance(current_state_changes, list):
            for _state_index, change in enumerate(current_state_changes):
                if not isinstance(change, dict):
                    continue
                original_key = change.get("key")
                normalized_key = normalize_current_state_change_key(original_key)
                prior_original_key = original_key_by_normalized_key.get(normalized_key)
                if (
                    prior_original_key is not None
                    and prior_original_key != original_key
                ):
                    raise ValueError(
                        "current state change key collision in planner response: "
                        f"{prior_original_key!r} and {original_key!r} both normalize "
                        f"to {normalized_key!r}"
                    )
                original_key_by_normalized_key[normalized_key] = original_key
                change["key"] = normalized_key
        if "anchor_item_ids" in occurrence:
            raise ValueError(
                f"occurrences[{index}] must not contain model-owned anchor_item_ids"
            )
        if "uses_fact_ids" in occurrence:
            raise ValueError(
                f"occurrences[{index}] must use split planner fact-reference fields"
            )
        current_fact_ids = occurrence.pop("uses_current_fact_ids", None)
        new_fact_keys = occurrence.pop("uses_new_fact_keys", None)
        if not isinstance(current_fact_ids, list) or not all(
            type(fact_id) is int for fact_id in current_fact_ids
        ):
            raise ValueError(
                f"occurrences[{index}].uses_current_fact_ids must be a list of integers"
            )
        if not isinstance(new_fact_keys, list) or not all(
            nonempty_string(fact_key) for fact_key in new_fact_keys
        ):
            raise ValueError(
                f"occurrences[{index}].uses_new_fact_keys must be a list of strings"
            )
        occurrence["uses_fact_ids"] = [*current_fact_ids, *new_fact_keys]
        occurrence["anchor_item_ids"] = []
        thread_id = occurrence.get("thread_id")
        continues_from = occurrence.get("continues_from")
        thread_after = occurrence.get("thread_after")
        contact_id = occurrence.get("contact_id")
        continued_thread_ids = {
            open_thread_id_by_reference[str(reference)]
            for reference in continues_from or []
            if str(reference) in open_thread_id_by_reference
        }
        if len(continued_thread_ids) == 1:
            thread_id = next(iter(continued_thread_ids))
            occurrence["thread_id"] = thread_id
        if (
            not nonempty_string(thread_id)
            and nonempty_string(contact_id)
            and isinstance(continues_from, list)
            and not continues_from
            and occurrence.get("effect_kind") != "advance_open_thread"
            and isinstance(thread_after, dict)
            and thread_after.get("is_open") is True
        ):
            thread_id = f"thread_{contact_id}"
            occurrence["thread_id"] = thread_id
        if nonempty_string(thread_id) and isinstance(continues_from, list):
            prior_contact_id = open_contact_by_thread.get(thread_id)
            if prior_contact_id is not None and prior_contact_id not in continues_from:
                continues_from.append(prior_contact_id)
        if (
            nonempty_string(thread_id)
            and nonempty_string(contact_id)
            and isinstance(thread_after, dict)
        ):
            if thread_after.get("is_open") is True:
                open_contact_by_thread[thread_id] = contact_id
                open_thread_id_by_reference[contact_id] = thread_id
            elif thread_after.get("is_open") is False:
                open_contact_by_thread.pop(thread_id, None)
                open_thread_id_by_reference = {
                    reference: value
                    for reference, value in open_thread_id_by_reference.items()
                    if value != thread_id
                }
    return decoded


def resolve_superseded_fact_references(
    response: dict[str, Any],
) -> dict[str, Any]:
    """Replace stale references with one unambiguous earlier replacement."""
    normalized = copy.deepcopy(response)
    plan = normalized.get("plan")
    occurrences = plan.get("occurrences") if isinstance(plan, dict) else None
    if not isinstance(occurrences, list):
        return normalized

    replacements_by_reference: dict[int | str, set[str]] = {}

    def current_reference(reference: int | str) -> int | str:
        original = reference
        current = reference
        visited: set[int | str] = set()
        while current in replacements_by_reference:
            replacements = replacements_by_reference[current]
            if len(replacements) != 1 or current in visited:
                return original
            visited.add(current)
            current = next(iter(replacements))
        return current

    for occurrence in occurrences:
        if not isinstance(occurrence, dict):
            continue
        references = occurrence.get("uses_fact_ids")
        if isinstance(references, list):
            resolved: list[int | str] = []
            seen: set[int | str] = set()
            for reference in references:
                if type(reference) is not int and not isinstance(reference, str):
                    resolved_reference = reference
                else:
                    resolved_reference = current_reference(reference)
                if resolved_reference not in seen:
                    resolved.append(resolved_reference)
                    seen.add(resolved_reference)
            occurrence["uses_fact_ids"] = resolved

        for fact in occurrence.get("durable_facts") or []:
            if not isinstance(fact, dict) or not nonempty_string(fact.get("key")):
                continue
            key = str(fact["key"])
            for reference in (
                fact_reference(value) for value in fact.get("supersedes") or []
            ):
                if reference is not None:
                    replacements_by_reference.setdefault(reference, set()).add(key)
    return normalized


def restore_code_owned_period(
    response: dict[str, Any], *, start: date, end: date
) -> dict[str, Any]:
    """Restore the exact requested dates after decoding a model response."""
    decoded = copy.deepcopy(response)
    plan = decoded.get("plan")
    if isinstance(plan, dict):
        plan["period_start"] = start.isoformat()
        plan["period_end"] = end.isoformat()
    return decoded


def restore_existing_state_subject_ids(
    response: dict[str, Any], current_ordinary_states: list[dict[str, Any]]
) -> dict[str, Any]:
    """Keep the accepted subjects when the planner updates an existing state."""
    normalized = copy.deepcopy(response)
    subjects_by_key = {
        normalize_current_state_change_key(state.get("key")): copy.deepcopy(
            state.get("subject_ids") or []
        )
        for state in current_ordinary_states
        if isinstance(state, dict) and nonempty_string(state.get("key"))
    }
    occurrences = normalized.get("plan", {}).get("occurrences")
    if not isinstance(occurrences, list):
        return normalized
    for occurrence in occurrences:
        if not isinstance(occurrence, dict):
            continue
        for change in occurrence.get("current_state_changes") or []:
            if not isinstance(change, dict):
                continue
            key = normalize_current_state_change_key(change.get("key"))
            if key in subjects_by_key:
                change["subject_ids"] = copy.deepcopy(subjects_by_key[key])
    return normalized


def restore_named_existing_document_ids(
    response: dict[str, Any], available_app_records: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Use the real ID when a contact names one existing document exactly."""
    normalized = copy.deepcopy(response)
    documents = available_app_records.get("docs")
    if not isinstance(documents, list):
        return normalized, []
    documents = [
        document
        for document in documents
        if isinstance(document, dict)
        and nonempty_string(document.get("id"))
        and nonempty_string(document.get("title"))
    ]
    valid_ids = {str(document["id"]) for document in documents}
    corrections: list[dict[str, str]] = []
    occurrences = normalized.get("plan", {}).get("occurrences")
    if not isinstance(occurrences, list):
        return normalized, corrections

    for occurrence in occurrences:
        if not isinstance(occurrence, dict):
            continue
        outcome = occurrence.get("assistant_outcome")
        requested_result = (
            outcome.get("requested_result") if isinstance(outcome, dict) else ""
        )
        contact_text = "\n".join(
            str(value)
            for value in (occurrence.get("what_happened"), requested_result)
            if nonempty_string(value)
        )
        named_documents = [
            document
            for document in documents
            if f"`{document['title']}`" in contact_text
        ]
        if len(named_documents) != 1:
            continue
        actual = named_documents[0]
        actual_id = str(actual["id"])
        for operation in occurrence.get("app_operations") or []:
            if not isinstance(operation, dict) or operation.get("tool") != "update_doc":
                continue
            args = operation.get("args")
            if not isinstance(args, dict):
                continue
            supplied_id = args.get("doc_id")
            if not nonempty_string(supplied_id) or supplied_id in valid_ids:
                continue
            args["doc_id"] = actual_id
            corrections.append(
                {
                    "contact_id": str(occurrence.get("contact_id") or ""),
                    "tool": "update_doc",
                    "document_title": str(actual["title"]),
                    "supplied_doc_id": str(supplied_id),
                    "actual_doc_id": actual_id,
                }
            )
    return normalized, corrections


def normalize_planner_occurrence_order(response: dict[str, Any]) -> dict[str, Any]:
    """Order valid planner occurrences by timestamp without changing their contents."""
    normalized = copy.deepcopy(response)
    plan = normalized.get("plan")
    occurrences = plan.get("occurrences") if isinstance(plan, dict) else None
    if not isinstance(occurrences, list):
        return normalized

    parsed: list[datetime] = []
    for occurrence in occurrences:
        if not isinstance(occurrence, dict):
            return normalized
        try:
            timestamp = datetime.fromisoformat(str(occurrence.get("narrative_date") or ""))
        except ValueError:
            return normalized
        if timestamp.tzinfo is None:
            return normalized
        parsed.append(timestamp)

    plan["occurrences"] = [
        occurrence
        for _, occurrence in sorted(
            zip(parsed, occurrences), key=lambda item: item[0]
        )
    ]
    return normalized


def include_current_state_statements_in_writer_context(
    response: dict[str, Any],
) -> dict[str, Any]:
    """Give the writer the exact text of every continuing-state change."""
    normalized = copy.deepcopy(response)
    plan = normalized.get("plan")
    occurrences = plan.get("occurrences") if isinstance(plan, dict) else None
    if not isinstance(occurrences, list):
        return normalized

    for occurrence in occurrences:
        if not isinstance(occurrence, dict):
            continue
        what_happened = str(occurrence.get("what_happened") or "").strip()
        normalized_what_happened = " ".join(what_happened.split())
        changes = occurrence.get("current_state_changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            statement = " ".join(str(change.get("statement") or "").split())
            if statement and statement not in normalized_what_happened:
                what_happened = f"{what_happened}\n\n{statement}".strip()
                normalized_what_happened = " ".join(what_happened.split())
        occurrence["what_happened"] = what_happened
    return normalized


def missing_closing_state_updates(
    response: dict[str, Any],
    open_thread_current_states_by_id: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Return only closed-thread states omitted from the planner response."""
    missing: list[dict[str, Any]] = []
    for occurrence in response.get("plan", {}).get("occurrences") or []:
        if not isinstance(occurrence, dict):
            continue
        thread_id = str(occurrence.get("thread_id") or "")
        thread_after = occurrence.get("thread_after")
        if not thread_id or not isinstance(thread_after, dict) or thread_after.get("is_open") is not False:
            continue
        existing_keys = {
            str(change.get("key") or "")
            for change in occurrence.get("current_state_changes") or []
            if isinstance(change, dict)
        }
        omitted = [
            copy.deepcopy(state)
            for state in open_thread_current_states_by_id.get(thread_id, [])
            if str(state.get("key") or "") not in existing_keys
        ]
        if omitted:
            missing.append(
                {
                    "contact_id": str(occurrence.get("contact_id") or ""),
                    "what_happened": str(occurrence.get("what_happened") or ""),
                    "durable_facts": copy.deepcopy(occurrence.get("durable_facts") or []),
                    "states_that_must_be_replaced": omitted,
                }
            )
    return missing


def state_correction_response_schema(
    missing: list[dict[str, Any]],
) -> dict[str, Any]:
    contact_ids = [str(row["contact_id"]) for row in missing]
    return _strict_object(
        {
            "contacts": {
                "type": "array",
                "items": _strict_object(
                    {
                        "contact_id": {"type": "string", "enum": contact_ids},
                        "current_state_changes": {
                            "type": "array",
                            "items": CURRENT_STATE_CHANGE_SCHEMA,
                        },
                    }
                ),
                "minItems": len(contact_ids),
                "maxItems": len(contact_ids),
            }
        }
    )


def apply_missing_state_corrections(
    response: dict[str, Any],
    correction: dict[str, Any],
    missing: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge a narrowly scoped correction after checking every returned key."""
    normalized = copy.deepcopy(response)
    required_by_contact = {
        str(row["contact_id"]): {
            str(state["key"]): state
            for state in row["states_that_must_be_replaced"]
        }
        for row in missing
    }
    returned = correction.get("contacts")
    if not isinstance(returned, list):
        raise ValueError("state correction must return contacts")
    returned_by_contact = {
        str(row.get("contact_id") or ""): row
        for row in returned
        if isinstance(row, dict)
    }
    if set(returned_by_contact) != set(required_by_contact):
        raise ValueError("state correction must return every affected contact exactly once")

    occurrences = {
        str(row.get("contact_id") or ""): row
        for row in normalized.get("plan", {}).get("occurrences") or []
        if isinstance(row, dict)
    }
    for contact_id, required_states in required_by_contact.items():
        changes = returned_by_contact[contact_id].get("current_state_changes")
        if not isinstance(changes, list):
            raise ValueError(f"state correction for {contact_id} must return changes")
        changes_by_key = {
            str(change.get("key") or ""): change
            for change in changes
            if isinstance(change, dict)
        }
        if set(changes_by_key) != set(required_states):
            raise ValueError(
                f"state correction for {contact_id} must return only the missing keys"
            )
        for key, change in changes_by_key.items():
            if change.get("subject_ids") != required_states[key].get("subject_ids"):
                raise ValueError(
                    f"state correction for {contact_id} changed subject_ids for {key}"
                )
        occurrences[contact_id].setdefault("current_state_changes", []).extend(
            copy.deepcopy(changes)
        )
    return normalized


def complete_model_stage(
    *,
    output_dir: Path,
    stage: str,
    system: str,
    payload: dict[str, Any],
    client: AzureJsonClient,
    start: date,
    end: date,
) -> dict[str, Any]:
    if stage not in {"story", "planner", "writer"}:
        raise ValueError(f"unknown model stage: {stage}")
    if stage == "story":
        response_schema = story_response_schema(payload)
    elif stage == "planner":
        response_schema = enrichment_response_schema(payload)
    else:
        contacts = payload.get("contacts")
        if isinstance(contacts, dict):
            response_schema = writer_api_response_schema(
                [str(contact_id) for contact_id in contacts]
            )
        else:
            response_schema = WRITER_API_RESPONSE_SCHEMA
    (output_dir / f"{stage}_system.txt").write_text(system)
    dump_json(output_dir / f"{stage}_request.json", payload)
    rendered_input = (
        render_story_planner_input(payload)
        if stage == "story"
        else render_model_input(payload)
    )
    (output_dir / f"{stage}_input.txt").write_text(rendered_input)
    response = cached_client_complete(
        output_dir / "work" / f"{stage}_response_cache.json",
        system,
        rendered_input,
        client,
        response_schema=response_schema,
        response_schema_name=f"history_window_{stage}",
    )
    if stage == "story":
        dump_json(output_dir / "story_response.json", response)
        response = normalize_planner_occurrence_order(response)
    elif stage == "planner":
        dump_json(output_dir / "planner_enrichment_response.json", response)
    else:
        contacts = payload.get("contacts")
        if not isinstance(contacts, dict):
            raise ValueError("writer payload must contain contacts keyed by contact_id")
        response = normalize_writer_contact_order(
            response, [str(contact_id) for contact_id in contacts]
        )
        response = normalize_writer_messages(response)
    if stage == "writer":
        response = restore_code_owned_period(response, start=start, end=end)
        dump_json(output_dir / f"{stage}_response.json", response)
    return response


def _load_reviewed_story_response(
    output_dir: Path,
    *,
    start: date,
    end: date,
    starting_checkpoint_sha256: str,
) -> dict[str, Any] | None:
    """Load a story that was explicitly paused for review instead of regenerating it."""
    manifest_path = output_dir / "manifest.json"
    response_path = output_dir / "story_response.json"
    if not manifest_path.is_file() or not response_path.is_file():
        return None

    manifest = load_json(manifest_path)
    if manifest.get("status") != "story_only":
        return None
    expected = {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "starting_checkpoint_sha256": starting_checkpoint_sha256,
    }
    for field, value in expected.items():
        if manifest.get(field) != value:
            raise ValueError(
                f"reviewed story {field} does not match this run: "
                f"expected {value!r}, got {manifest.get(field)!r}"
            )

    response = load_json(response_path)
    occurrences = response.get("plan", {}).get("occurrences")
    if not isinstance(occurrences, list):
        raise ValueError("reviewed story_response.json must contain plan.occurrences")
    return normalize_planner_occurrence_order(response)


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.confirm_paid_calls:
        raise ValueError("refusing model calls without --confirm-paid-calls")
    prepare_window_output(args.out)
    validate_tool_python(args.tool_python)
    set_run_provenance_environment(args.out)

    config = yaml.safe_load(args.config.read_text()) or {}
    checkpoint, starting_state = load_checkpoint(args.resume_from_checkpoint)
    operation_results_path = args.resume_from_checkpoint / "operation_results.json"
    starting_state["operation_results"] = (
        load_json(operation_results_path) if operation_results_path.is_file() else []
    )
    if not isinstance(starting_state["operation_results"], list):
        raise ValueError("checkpoint operation_results.json must contain a list")
    cumulative_operation_results(starting_state["operation_results"], [])
    validate_resume_checkpoint_for_config(
        config=config,
        checkpoint=checkpoint,
        checkpoint_dir=args.resume_from_checkpoint,
    )
    validate_window_dates(config, checkpoint, args.start_date, args.end_date)
    spec = yaml.safe_load(resolve_path(config["spec"]).read_text()) or {}
    timezone_name = str(spec["narrative_timezone"])
    tools = window_tools(exact_persona_tools(str(config["persona"])))

    planner_payload, validation_context = build_planner_payload(
        config=config,
        checkpoint=checkpoint,
        state=starting_state,
        start=args.start_date,
        end=args.end_date,
        tools=tools,
    )
    story_payload = build_story_payload(planner_payload)
    prior_manifest_path = args.out / "manifest.json"
    prior_manifest = (
        load_json(prior_manifest_path) if prior_manifest_path.is_file() else {}
    )
    story_response = None
    if prior_manifest.get("status") == "story_only":
        story_response = _load_reviewed_story_response(
            args.out,
            start=args.start_date,
            end=args.end_date,
            starting_checkpoint_sha256=validated_checkpoint_identity(
                args.resume_from_checkpoint
            ),
        )
    if story_response is None:
        story_client = AzureJsonClient(
            model=PLANNER_MODEL, reasoning_effort=PLANNER_REASONING_EFFORT
        )
        mark_stage(args.out, "story_call")
        story_response = complete_model_stage(
            output_dir=args.out,
            stage="story",
            system=HISTORY_WINDOW_STORY_SYSTEM,
            payload=story_payload,
            client=story_client,
            start=args.start_date,
            end=args.end_date,
        )
    else:
        mark_stage(args.out, "story_complete")
    story_response = assign_story_contact_ids(
        story_response,
        persona=str(config["persona"]),
        timezone_name=timezone_name,
    )
    story_response = add_code_owned_story_fields(story_response)
    story_errors = story_contact_count_errors(
        story_response, start=args.start_date, end=args.end_date
    )
    dump_json(
        args.out / "story_validation.json",
        {"passed": not story_errors, "errors": story_errors},
    )
    if story_errors:
        raise ValueError(
            "history-window story response is invalid: " + "; ".join(story_errors)
        )

    if getattr(args, "stop_after_story", False):
        mark_stage(args.out, "story_complete")
        occurrences = story_response.get("plan", {}).get("occurrences")
        manifest = {
            "status": "story_only",
            "persona": str(config["persona"]),
            "period_start": args.start_date.isoformat(),
            "period_end": args.end_date.isoformat(),
            "starting_checkpoint": str(args.resume_from_checkpoint),
            "starting_checkpoint_sha256": validated_checkpoint_identity(
                args.resume_from_checkpoint
            ),
            "models": {
                "story": {
                    "name": PLANNER_MODEL,
                    "reasoning_effort": PLANNER_REASONING_EFFORT,
                }
            },
            "model_calls": 1,
            "llm_review_performed": False,
            "generation_stages": ["story_planning"],
            "sessions": len(occurrences) if isinstance(occurrences, list) else 0,
            "required_development_ids": sorted(
                validation_context["required_development_ids"]
            ),
            "call_ledger": str(args.out / "call_ledger.jsonl"),
            "candidate_checkpoint": None,
        }
        dump_json(args.out / "manifest.json", manifest)
        return manifest

    enrichment_payload = build_enrichment_payload(planner_payload, story_response)
    planner_client = AzureJsonClient(
        model=PLANNER_MODEL, reasoning_effort=PLANNER_REASONING_EFFORT
    )
    mark_stage(args.out, "planner_call")
    enrichment_response = complete_model_stage(
        output_dir=args.out,
        stage="planner",
        system=HISTORY_WINDOW_PLANNING_SYSTEM,
        payload=enrichment_payload,
        client=planner_client,
        start=args.start_date,
        end=args.end_date,
    )
    contact_correction_performed = False
    execution_problems = planner_execution_problems(
        story_response,
        enrichment_response,
        available_app_records=planner_payload["available_app_records"],
        tools=tools,
    )
    if execution_problems:
        contact_correction_performed = True
        blocked_contact_ids = [row["contact_id"] for row in execution_problems]
        story_occurrences_by_id = {
            str(row.get("contact_id") or ""): row
            for row in story_response["plan"]["occurrences"]
        }
        enrichment_by_contact_id = {
            str(row.get("contact_id") or ""): row
            for row in enrichment_response.get("contacts") or []
            if isinstance(row, dict)
        }
        current_facts_by_id = {
            int(row["id"]): row
            for row in enrichment_payload.get("current_facts") or []
            if isinstance(row, dict)
            and isinstance(row.get("id"), int)
            and not isinstance(row.get("id"), bool)
        }
        correction_payload = {
            "persona": str(config["persona"]),
            "requested_dates": copy.deepcopy(planner_payload["requested_dates"]),
            "contacts": [
                {
                    "contact": copy.deepcopy(story_occurrences_by_id[row["contact_id"]]),
                    "execution_problem": row["reason"],
                    "problem_kind": row.get("problem_kind", "impossible"),
                    "accepted_facts_cited_by_contact": [
                        copy.deepcopy(current_facts_by_id[fact_id])
                        for fact_id in enrichment_by_contact_id.get(
                            row["contact_id"], {}
                        ).get("uses_current_fact_ids") or []
                        if fact_id in current_facts_by_id
                    ],
                }
                for row in execution_problems
            ],
            "accepted_outcome_and_contact_evidence": correction_evidence_for_contact(
                planner_payload,
                {
                    "subject_ids": sorted({
                        str(subject)
                        for contact_id in blocked_contact_ids
                        for subject in story_occurrences_by_id[contact_id].get("subject_ids") or []
                    })
                },
            ),
            "other_fixed_contacts_this_week": [
                copy.deepcopy(contact)
                for contact_id, contact in story_occurrences_by_id.items()
                if contact_id not in blocked_contact_ids
            ],
            "supported_external_actions": copy.deepcopy(
                story_payload["supported_external_actions"]
            ),
        }
        correction_input = render_model_input(correction_payload)
        (args.out / "contact_correction_system.txt").write_text(
            HISTORY_WINDOW_CONTACT_CORRECTION_SYSTEM
        )
        dump_json(args.out / "contact_correction_request.json", correction_payload)
        (args.out / "contact_correction_input.txt").write_text(correction_input)
        mark_stage(args.out, "contact_correction_call")
        correction_response = cached_client_complete(
            args.out / "work" / "contact_correction_response_cache.json",
            HISTORY_WINDOW_CONTACT_CORRECTION_SYSTEM,
            correction_input,
            planner_client,
            response_schema=contact_correction_response_schema(blocked_contact_ids),
            response_schema_name="history_window_contact_correction",
        )
        dump_json(args.out / "contact_correction_response.json", correction_response)
        story_response = apply_story_contact_corrections(
            story_response,
            correction_response,
            blocked_contact_ids,
            problem_kinds={
                row["contact_id"]: str(row.get("problem_kind") or "impossible")
                for row in execution_problems
            },
        )
        dump_json(args.out / "corrected_story_response.json", story_response)

        blocked_contact_id_set = set(blocked_contact_ids)
        corrected_story_subset = {
            "plan": {
                "occurrences": [
                    copy.deepcopy(occurrence)
                    for occurrence in story_response["plan"]["occurrences"]
                    if str(occurrence.get("contact_id") or "")
                    in blocked_contact_id_set
                ]
            }
        }
        corrected_enrichment_payload = build_enrichment_payload(
            planner_payload,
            corrected_story_subset,
            other_fixed_contacts_this_week=[
                copy.deepcopy(occurrence)
                for occurrence in story_response["plan"]["occurrences"]
                if str(occurrence.get("contact_id") or "")
                not in blocked_contact_id_set
            ],
        )
        corrected_enrichment_input = render_model_input(corrected_enrichment_payload)
        (args.out / "corrected_planner_system.txt").write_text(
            HISTORY_WINDOW_PLANNING_SYSTEM
        )
        dump_json(
            args.out / "corrected_planner_request.json",
            corrected_enrichment_payload,
        )
        (args.out / "corrected_planner_input.txt").write_text(
            corrected_enrichment_input
        )
        mark_stage(args.out, "corrected_planner_call")
        corrected_enrichment_response = cached_client_complete(
            args.out / "work" / "corrected_planner_response_cache.json",
            HISTORY_WINDOW_PLANNING_SYSTEM,
            corrected_enrichment_input,
            planner_client,
            response_schema=enrichment_response_schema(corrected_enrichment_payload),
            response_schema_name="history_window_corrected_planner",
        )
        dump_json(
            args.out / "corrected_planner_enrichment_response.json",
            corrected_enrichment_response,
        )
        remaining_execution_problems = planner_execution_problems(
            corrected_story_subset,
            corrected_enrichment_response,
            available_app_records=planner_payload["available_app_records"],
            tools=tools,
        )
        if remaining_execution_problems:
            raise ValueError(
                "one contact remained impossible after its single correction: "
                + "; ".join(
                    f"{row['contact_id']}: {row['reason']}"
                    for row in remaining_execution_problems
                )
            )
        enrichment_response = replace_enrichment_contacts(
            enrichment_response,
            corrected_enrichment_response,
            blocked_contact_ids,
        )
    planner_response = merge_story_and_enrichment(story_response, enrichment_response)
    planner_response = normalize_planner_occurrence_order(planner_response)
    dump_json(args.out / "planner_response.json", planner_response)
    planner_response = decode_planner_app_operations(planner_response)
    planner_response, record_id_corrections = restore_named_existing_document_ids(
        planner_response, planner_payload["available_app_records"]
    )
    if record_id_corrections:
        dump_json(
            args.out / "record_id_corrections.json",
            {"corrections": record_id_corrections},
        )
    planner_response = resolve_superseded_fact_references(planner_response)
    planner_response = restore_code_owned_period(
        planner_response, start=args.start_date, end=args.end_date
    )
    planner_response = restore_existing_state_subject_ids(
        planner_response, planner_payload["current_ordinary_states"]
    )
    planner_response = add_known_world_basis_for_visible_subjects(
        planner_response,
        facts=starting_state["facts"],
        subjects_by_basis=validation_context["subjects_by_basis"],
        primary_subject_ids=validation_context["primary_subject_ids"],
    )
    missing_state_updates = missing_closing_state_updates(
        planner_response,
        validation_context.get("open_thread_current_states_by_id", {}),
    )
    state_correction_performed = bool(missing_state_updates)
    if missing_state_updates:
        correction_payload = {"contacts": missing_state_updates}
        correction_input = render_model_input(correction_payload)
        (args.out / "state_correction_system.txt").write_text(
            HISTORY_WINDOW_STATE_CORRECTION_SYSTEM
        )
        dump_json(args.out / "state_correction_request.json", correction_payload)
        (args.out / "state_correction_input.txt").write_text(correction_input)
        mark_stage(args.out, "planner_state_correction_call")
        correction_response = cached_client_complete(
            args.out / "work" / "state_correction_response_cache.json",
            HISTORY_WINDOW_STATE_CORRECTION_SYSTEM,
            correction_input,
            planner_client,
            response_schema=state_correction_response_schema(missing_state_updates),
            response_schema_name="history_window_state_correction",
        )
        dump_json(args.out / "state_correction_response.json", correction_response)
        planner_response = apply_missing_state_corrections(
            planner_response,
            correction_response,
            missing_state_updates,
        )
    planner_response = include_current_state_statements_in_writer_context(planner_response)
    dump_json(args.out / "planner_plan.json", planner_response["plan"])
    planner_errors = validate_history_window_plan_response(
        planner_response,
        start=args.start_date,
        end=args.end_date,
        timezone_name=timezone_name,
        entities=starting_state["entities"],
        existing_session_ids={str(row.get("id") or "") for row in starting_state["history"]},
        existing_references=set(
            planner_payload["valid_existing_continues_from_ids"]
        ),
        required_development_ids=validation_context["required_development_ids"],
        allowed_development_ids=validation_context["allowed_development_ids"],
        required_entities=validation_context["candidate_entities"],
        facts=starting_state["facts"],
        tools=tools,
        self_contained_basis_ids=validation_context["self_contained_basis_ids"],
        open_thread_ids=validation_context["open_thread_ids"],
        protected_change_ids=validation_context["protected_change_ids"],
        facts_by_development=validation_context["facts_by_development"],
        subjects_by_basis=validation_context["subjects_by_basis"],
        primary_subject_ids=validation_context["primary_subject_ids"],
        unfinished_thread_ids_by_reference=validation_context.get(
            "unfinished_thread_ids_by_reference", {}
        ),
        open_thread_current_states_by_id=validation_context.get(
            "open_thread_current_states_by_id", {}
        ),
        development_start_dates=validation_context.get("development_start_dates", {}),
        available_app_records=planner_payload["available_app_records"],
        earlier_accepted_app_items=planner_payload.get(
            "earlier_accepted_app_items"
        ),
    )
    planner_errors.extend(
        calendar_update_context_errors(
            planner_response,
            current_app_records=planner_payload.get("_exact_app_records") or {},
            planner_app_records=enrichment_payload.get("available_app_records") or {},
        )
    )
    selected_entities, entity_errors = select_new_entities_for_week(
        planner_response, validation_context["candidate_entities"]
    )
    planner_errors.extend(entity_errors)
    planner_validation = {"passed": not planner_errors, "errors": planner_errors}
    dump_json(args.out / "planner_validation.json", planner_validation)
    validation: dict[str, Any] = {
        "planner": planner_validation,
        "writer": {"passed": False, "errors": ["not run"]},
        "operation_replay": {"passed": False, "errors": ["not run"]},
        "candidate_checkpoint": {"passed": False, "errors": ["not emitted"]},
    }
    dump_json(args.out / "validation.json", validation)
    if planner_errors:
        raise ValueError(
            "history-window planner response is invalid: "
            + "; ".join(planner_errors)
        )

    if getattr(args, "stop_after_planner", False):
        mark_stage(args.out, "planner_complete")
        manifest = {
            "status": "planner_only",
            "persona": str(config["persona"]),
            "period_start": args.start_date.isoformat(),
            "period_end": args.end_date.isoformat(),
            "starting_checkpoint": str(args.resume_from_checkpoint),
            "starting_checkpoint_sha256": validated_checkpoint_identity(
                args.resume_from_checkpoint
            ),
            "models": {
                "story": {
                    "name": PLANNER_MODEL,
                    "reasoning_effort": PLANNER_REASONING_EFFORT,
                },
                "planner": {
                    "name": PLANNER_MODEL,
                    "reasoning_effort": PLANNER_REASONING_EFFORT,
                }
            },
            "model_calls": (
                2
                + int(state_correction_performed)
                + (2 if contact_correction_performed else 0)
            ),
            "llm_review_performed": False,
            "generation_stages": [
                "story_planning",
                "occurrence_enrichment",
                *(
                    ["unexecutable_contact_correction", "corrected_occurrence_enrichment"]
                    if contact_correction_performed
                    else []
                ),
                *(["missing_state_correction"] if state_correction_performed else []),
            ],
            "required_development_ids": sorted(
                validation_context["required_development_ids"]
            ),
            "call_ledger": str(args.out / "call_ledger.jsonl"),
            "candidate_checkpoint": None,
        }
        dump_json(args.out / "manifest.json", manifest)
        return manifest

    existing_session_ids = {
        str(row.get("id") or "") for row in starting_state["history"]
    }
    operation_plan = assign_window_session_ids(
        {
            "sessions": [
                {
                    "contact_id": str(occurrence["contact_id"]),
                    "narrative_date": copy.deepcopy(occurrence["narrative_date"]),
                    "app_operations": copy.deepcopy(occurrence["app_operations"]),
                }
                for occurrence in planner_response["plan"]["occurrences"]
            ]
        },
        persona=str(config["persona"]),
        timezone_name=timezone_name,
        existing_session_ids=existing_session_ids,
    )
    mark_stage(args.out, "operation_replay")
    try:
        projected_state, operation_results, app_changes = project_app_operations(
            persona=str(config["persona"]),
            plan={"contacts": operation_plan["sessions"]},
            starting_state=starting_state["app_state"],
            work_dir=args.out / "work" / "operation_replay",
            tool_python=args.tool_python,
        )
    except Exception as error:
        validation["operation_replay"] = {
            "passed": False,
            "errors": [f"{type(error).__name__}: {error}"],
        }
        dump_json(args.out / "validation.json", validation)
        raise
    validation["operation_replay"] = {"passed": True, "errors": []}
    dump_json(args.out / "validation.json", validation)

    assigned_session_by_contact_id = {
        str(session["contact_id"]): session
        for session in operation_plan["sessions"]
    }
    for occurrence in planner_response["plan"]["occurrences"]:
        assigned_session = assigned_session_by_contact_id[str(occurrence["contact_id"])]
        occurrence["app_operations"] = copy.deepcopy(
            assigned_session["app_operations"]
        )

    writer_payload = build_writer_payload(
        planner_response=planner_response,
        config=config,
        state=starting_state,
        start=args.start_date,
        end=args.end_date,
        timezone_name=timezone_name,
        required_entities=selected_entities,
        primary_subject_ids=validation_context["primary_subject_ids"],
        same_week_operation_results=operation_results,
    )
    writer_client = AzureJsonClient(
        model=WRITER_MODEL, reasoning_effort=WRITER_REASONING_EFFORT
    )
    mark_stage(args.out, "writer_call")
    writer_response = complete_model_stage(
        output_dir=args.out,
        stage="writer",
        system=HISTORY_WINDOW_WRITING_SYSTEM,
        payload=writer_payload,
        client=writer_client,
        start=args.start_date,
        end=args.end_date,
    )
    writer_contract_errors = validate_writer_prose_response(
        writer_response,
        start=args.start_date,
        end=args.end_date,
        occurrence_plan=planner_response["plan"],
    )
    if writer_contract_errors:
        writer_validation = {"passed": False, "errors": writer_contract_errors}
        dump_json(args.out / "writer_validation.json", writer_validation)
        validation["writer"] = writer_validation
        dump_json(args.out / "validation.json", validation)
        raise ValueError(
            "history-window writer response is invalid: "
            + "; ".join(writer_contract_errors)
        )
    writer_plan = construct_writer_plan(
        writer_response,
        occurrence_plan=planner_response["plan"],
        required_entities=selected_entities,
    )
    writer_errors = validate_history_window_response(
        {"plan": writer_plan},
        start=args.start_date,
        end=args.end_date,
        timezone_name=timezone_name,
        facts=starting_state["facts"],
        entities=starting_state["entities"],
        tools=tools,
        existing_session_ids={str(row.get("id") or "") for row in starting_state["history"]},
        existing_references=set(
            planner_payload["valid_existing_continues_from_ids"]
        ),
        occurrence_plan=planner_response["plan"],
        required_development_ids=validation_context["required_development_ids"],
        allowed_development_ids=validation_context["allowed_development_ids"],
        required_entities=selected_entities,
        required_facts=validation_context["required_facts"],
    )
    inherited_source_fact_ids_by_key: dict[str, list[int]] = {}
    writer_validation = {"passed": not writer_errors, "errors": writer_errors}
    dump_json(args.out / "writer_validation.json", writer_validation)
    validation["writer"] = writer_validation
    dump_json(args.out / "validation.json", validation)
    if writer_errors:
        raise ValueError(
            "history-window writer response is invalid: " + "; ".join(writer_errors)
        )

    plan = copy.deepcopy(writer_plan)
    for session in plan["sessions"]:
        assigned_session = assigned_session_by_contact_id.get(
            str(session.get("contact_id") or "")
        )
        if assigned_session is None:
            raise ValueError(
                "writer plan contains a contact absent from the validated planner plan"
            )
        if session.get("narrative_date") != assigned_session.get("narrative_date"):
            raise ValueError(
                "writer plan changed a planner-owned contact narrative_date"
            )
        session["session_id"] = str(assigned_session["session_id"])
    dump_json(args.out / "plan.json", plan)
    rendered = rendered_sessions(plan)
    dump_json(args.out / "rendered_sessions.json", {"sessions": rendered})

    checkpoint_operation_results = write_window_operation_results(
        run_output=args.out,
        starting_rows=starting_state["operation_results"],
        current_rows=operation_results,
    )
    dump_json(args.out / "app_state_changes.json", app_changes)

    mark_stage(args.out, "state_assembly")
    assembled_state, new_entities, new_facts = materialize_window_state(
        starting_state=starting_state,
        plan=plan,
        projected_app_state=projected_state,
        inherited_source_fact_ids_by_key=inherited_source_fact_ids_by_key,
        accepted_contact_details_by_contact_id={
            str(session["contact_id"]): accepted_contact_details_for_rendered_session(
                session
            )
            for session in rendered
        },
        current_state_changes_by_contact_id={
            str(occurrence["contact_id"]): copy.deepcopy(
                occurrence["current_state_changes"]
            )
            for occurrence in planner_response["plan"]["occurrences"]
        },
    )
    dump_json(args.out / "new_entities.json", new_entities)
    dump_json(args.out / "new_facts.json", new_facts)

    covered_events = list(
        dict.fromkeys(
            [
                *[str(value) for value in checkpoint.get("covered_quarter_event_ids") or []],
                *sorted(validation_context["required_development_ids"]),
            ]
        )
    )
    mark_stage(args.out, "candidate_checkpoint")
    candidate = emit_candidate_checkpoint(
        path=args.out / "candidate_checkpoint",
        run_output=args.out,
        save_kwargs={
            "persona": str(config["persona"]),
            "week": {
                "week_id": f"{args.start_date.isoformat()}_to_{args.end_date.isoformat()}",
                "start_date": args.start_date.isoformat(),
                "end_date": args.end_date.isoformat(),
            },
            "previous_hash": validated_checkpoint_identity(args.resume_from_checkpoint),
            "inputs": {
                "config": file_hash(args.config),
                "quarter_plan.json": file_hash(resolve_path(config["quarter_plan"])),
                "overview.json": file_hash(resolve_path(config["overview"])),
                "story_request.json": file_hash(args.out / "story_request.json"),
                "story_response.json": file_hash(args.out / "story_response.json"),
                "planner_request.json": file_hash(args.out / "planner_request.json"),
                "planner_response.json": file_hash(args.out / "planner_response.json"),
                "writer_request.json": file_hash(args.out / "writer_request.json"),
                "writer_response.json": file_hash(args.out / "writer_response.json"),
                "plan.json": file_hash(args.out / "plan.json"),
                **(
                    {
                        "contact_correction_request.json": file_hash(
                            args.out / "contact_correction_request.json"
                        ),
                        "contact_correction_response.json": file_hash(
                            args.out / "contact_correction_response.json"
                        ),
                        "corrected_story_response.json": file_hash(
                            args.out / "corrected_story_response.json"
                        ),
                        "corrected_planner_request.json": file_hash(
                            args.out / "corrected_planner_request.json"
                        ),
                        "corrected_planner_enrichment_response.json": file_hash(
                            args.out / "corrected_planner_enrichment_response.json"
                        ),
                    }
                    if contact_correction_performed
                    else {}
                ),
            },
            "state": assembled_state,
            "operation_results": checkpoint_operation_results,
            "covered_events": covered_events,
            "system_sha256": system_hash(str(config["persona"])),
            "revalidated_against_current_state": True,
            "progress": {"construction_path": "two_stage_weekly_planner"},
            "identity_version": 2,
        },
    )
    candidate_identity = validated_checkpoint_identity(candidate)
    validation["candidate_checkpoint"] = {
        "passed": True,
        "errors": [],
        "sha256": candidate_identity,
    }
    dump_json(args.out / "validation.json", validation)

    manifest = {
        "status": "passed",
        "persona": str(config["persona"]),
        "period_start": args.start_date.isoformat(),
        "period_end": args.end_date.isoformat(),
        "starting_checkpoint": str(args.resume_from_checkpoint),
        "starting_checkpoint_sha256": validated_checkpoint_identity(
            args.resume_from_checkpoint
        ),
        "models": {
            "story": {
                "name": PLANNER_MODEL,
                "reasoning_effort": PLANNER_REASONING_EFFORT,
            },
            "planner": {
                "name": PLANNER_MODEL,
                "reasoning_effort": PLANNER_REASONING_EFFORT,
            },
            "writer": {
                "name": WRITER_MODEL,
                "reasoning_effort": WRITER_REASONING_EFFORT,
            },
        },
        "model_calls": (
            3
            + int(state_correction_performed)
            + (2 if contact_correction_performed else 0)
        ),
        "llm_review_performed": False,
        "generation_stages": [
            "story_planning",
            "occurrence_enrichment",
            *(
                ["unexecutable_contact_correction", "corrected_occurrence_enrichment"]
                if contact_correction_performed
                else []
            ),
            *(["missing_state_correction"] if state_correction_performed else []),
            "session_writing",
        ],
        "sessions": len(plan["sessions"]),
        "new_entities": len(new_entities),
        "new_facts": len(new_facts),
        "app_operations": len(operation_results),
        "required_development_ids": sorted(
            validation_context["required_development_ids"]
        ),
        "call_ledger": str(args.out / "call_ledger.jsonl"),
        "candidate_checkpoint": str(candidate),
        "candidate_checkpoint_sha256": candidate_identity,
    }
    dump_json(args.out / "manifest.json", manifest)
    mark_stage(args.out, "complete")
    return manifest


def run(args: argparse.Namespace) -> dict[str, Any]:
    try:
        return _run(args)
    except CompletedOutputError:
        raise
    except Exception as exc:
        if args.out.is_dir():
            stage_path = args.out / "work" / "stage.json"
            stage = (
                str(load_json(stage_path).get("stage") or "unknown")
                if stage_path.is_file()
                else "unknown"
            )
            dump_json(
                args.out / "manifest.json",
                {
                    "status": "failed",
                    "stage": stage,
                    "error": f"{type(exc).__name__}: {exc}",
                    "candidate_checkpoint_emitted": (
                        args.out / "candidate_checkpoint"
                    ).exists(),
                },
            )
        raise


if __name__ == "__main__":
    raise SystemExit(
        "build_history_window is internal; run python -m construction.quarter_runner"
    )
