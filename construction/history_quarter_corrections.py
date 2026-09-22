"""Local correction replay for a failed whole-quarter history review.

This module is internal to ``construction.quarter_runner``.  It deliberately
does not call either weekly model stage: saved weekly plans are accepted source
generation provenance, while corrected sessions are replayed locally.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from construction import build_history_window
from construction.checkpoints import (
    dump_json,
    file_hash,
    load_checkpoint,
    load_json,
    load_yaml,
    validated_checkpoint_identity,
)
from construction.construction_io import resolve_path
from construction.runtime_inputs import exact_persona_tools
from construction.runtime_operations import validate_operation_result_references


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain an object")
    return value


def _source_plan(window_dir: Path) -> dict[str, Any]:
    path = window_dir / "plan.json"
    if not path.is_file():
        raise ValueError(f"source window is missing plan.json: {window_dir}")
    document = _require_object(load_json(path), f"source plan {path}")
    plan = document.get("plan") if isinstance(document.get("plan"), dict) else document
    if not isinstance(plan.get("sessions"), list) or not isinstance(plan.get("new_entities"), list):
        raise ValueError(f"source plan has no complete sessions/new_entities shape: {path}")
    return copy.deepcopy(plan)


def _accepted_generation_dir(window_dir: Path) -> Path:
    preserved = window_dir / "accepted_source_generation"
    return preserved if preserved.is_dir() else window_dir


def _source_occurrences(window_dir: Path) -> dict[str, dict[str, Any]]:
    path = _accepted_generation_dir(window_dir) / "planner_plan.json"
    if not path.is_file():
        raise ValueError(f"source window is missing planner_plan.json: {window_dir}")
    document = _require_object(load_json(path), f"source planner plan {path}")
    occurrences = document.get("occurrences")
    if not isinstance(occurrences, list):
        raise ValueError(f"source planner plan occurrences must be a list: {path}")
    return {
        str(row.get("contact_id")): copy.deepcopy(row)
        for row in occurrences
        if isinstance(row, dict) and isinstance(row.get("contact_id"), str)
    }


def _source_session_purposes(window_dir: Path) -> dict[str, dict[str, Any]]:
    """Read the text and temporary state actually accepted by the source run."""
    candidate = window_dir / "candidate_checkpoint"
    if not candidate.is_dir():
        return {}
    _, state = load_checkpoint(candidate)
    purposes = state.get("session_purposes")
    if not isinstance(purposes, list):
        raise ValueError(
            f"source candidate checkpoint has no session_purposes list: {candidate}"
        )
    return {
        str(row.get("session_id")): copy.deepcopy(row)
        for row in purposes
        if isinstance(row, dict) and isinstance(row.get("session_id"), str)
    }


def _source_operation_epochs(window_dir: Path) -> list[dict[str, Any]]:
    path = window_dir / "operation_results.json"
    if not path.is_file():
        raise ValueError(f"source window is missing operation_results.json: {window_dir}")
    rows = load_json(path)
    if not isinstance(rows, list):
        raise ValueError(f"source operation_results.json must contain a list: {path}")
    return copy.deepcopy(rows)


def _correction_replay_epoch_mapping(
    *,
    source_windows: list[dict[str, Any]],
    source_plans: list[dict[str, Any]],
    corrected_plans: list[dict[str, Any]],
) -> dict[tuple[str, int], int]:
    """Keep retained operation IDs stable while corrections remove other actions."""
    source_counts: dict[str, int] = {}
    source_epochs: dict[tuple[str, int], int] = {}
    used_epochs: set[int] = set()
    for source_row, plan in zip(source_windows, source_plans):
        for session in plan["sessions"]:
            session_id = str(session["session_id"])
            source_counts[session_id] = len(session.get("app_operations") or [])
        for row_index, row in enumerate(_source_operation_epochs(source_row["source_window"])):
            if not isinstance(row, dict):
                raise ValueError(f"source operation result {row_index} must be an object")
            session_id = row.get("session_id")
            operation_index = row.get("operation_index")
            replay_epoch_ms = row.get("replay_epoch_ms")
            if (
                not isinstance(session_id, str)
                or not session_id
                or isinstance(operation_index, bool)
                or not isinstance(operation_index, int)
                or operation_index < 0
                or isinstance(replay_epoch_ms, bool)
                or not isinstance(replay_epoch_ms, int)
            ):
                raise ValueError(
                    f"source operation result {row_index} lacks a valid session_id, "
                    "operation_index, or replay_epoch_ms"
                )
            key = (session_id, operation_index)
            if key in source_epochs:
                raise ValueError(
                    f"source operation results repeat session {session_id!r} operation {operation_index}"
                )
            if replay_epoch_ms in used_epochs:
                raise ValueError(
                    f"source operation results reuse replay_epoch_ms {replay_epoch_ms}"
                )
            source_epochs[key] = replay_epoch_ms
            used_epochs.add(replay_epoch_ms)

    mapping: dict[tuple[str, int], int] = {}
    for plan in corrected_plans:
        for session in plan["sessions"]:
            session_id = str(session["session_id"])
            source_count = source_counts.get(session_id)
            if source_count is None:
                raise ValueError(f"corrected session is missing from source plan: {session_id}")
            narrative_time = datetime.fromisoformat(str(session["narrative_date"]))
            if narrative_time.tzinfo is None:
                raise ValueError(
                    "correction replay requires offset-aware narrative datetimes"
                )
            narrative_epoch_ms = int(narrative_time.timestamp() * 1000)
            previous_epoch_ms: int | None = None
            for operation_index, _operation in enumerate(session.get("app_operations") or []):
                key = (session_id, operation_index)
                if operation_index < source_count:
                    replay_epoch_ms = source_epochs.get(key)
                    if replay_epoch_ms is None:
                        raise ValueError(
                            "source operation results are missing replay_epoch_ms for "
                            f"session {session_id!r} operation {operation_index}"
                        )
                else:
                    replay_epoch_ms = max(
                        narrative_epoch_ms,
                        (previous_epoch_ms + 1)
                        if previous_epoch_ms is not None
                        else narrative_epoch_ms,
                    )
                    while replay_epoch_ms in used_epochs:
                        replay_epoch_ms += 1
                    used_epochs.add(replay_epoch_ms)
                mapping[key] = replay_epoch_ms
                previous_epoch_ms = replay_epoch_ms
    return mapping


def _copy_source_provenance(source_window: Path, output_window: Path) -> None:
    provenance = output_window / "accepted_source_generation"
    provenance.mkdir(parents=True)
    source_generation = _accepted_generation_dir(source_window)
    for name in (
        "planner_system.txt",
        "planner_request.json",
        "planner_input.txt",
        "planner_response.json",
        "planner_plan.json",
        "planner_validation.json",
        "writer_system.txt",
        "writer_request.json",
        "writer_input.txt",
        "writer_response.json",
        "writer_validation.json",
        "call_ledger.jsonl",
        "manifest.json",
    ):
        source = source_generation / name
        if source.is_file():
            shutil.copy2(source, provenance / name)


def _copy_root_provenance(source: Path, output: Path) -> None:
    provenance = output / "accepted_source_generation"
    provenance.mkdir(parents=True)
    for name in (
        "manifest.json",
        "history_quarter_review_request.json",
        "history_quarter_review_system.txt",
        "history_quarter_review_input.txt",
        "history_quarter_review_response.json",
        "history_quarter_review_validation.json",
    ):
        path = source / name
        if path.is_file():
            shutil.copy2(path, provenance / name)


def _reference_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        result: set[str] = set()
        for child in value:
            result.update(_reference_values(child))
        return result
    if isinstance(value, dict):
        result: set[str] = set()
        for child in value.values():
            result.update(_reference_values(child))
        return result
    return set()


def _session_references(session: dict[str, Any]) -> set[str]:
    references = set()
    for field in ("continues_from", "uses_facts"):
        references.update(_reference_values(session.get(field)))
    for fact in session.get("new_facts") or []:
        if isinstance(fact, dict):
            references.update(_reference_values(fact.get("evidence_contact_ids")))
            references.update(_reference_values(fact.get("supersedes")))
    for operation in session.get("app_operations") or []:
        if isinstance(operation, dict):
            references.update(_reference_values(operation.get("args")))
    return references


def _returned_record_ids(result: Any) -> set[str]:
    if not isinstance(result, dict) or result.get("error") or result.get("ok") is False:
        return set()
    record_ids: set[str] = set()
    for path in build_history_window.APP_ITEM_RESULT_ID_PATHS:
        value: Any = result
        for key in path:
            if not isinstance(value, dict) or key not in value:
                break
            value = value[key]
        else:
            if isinstance(value, str) and value:
                record_ids.add(value)
    return record_ids


def _validate_retained_record_dependencies(
    *,
    source_windows: list[dict[str, Any]],
    source_plans: list[dict[str, Any]],
    corrected_plans: list[dict[str, Any]],
) -> None:
    """Reject corrections that remove a source-created record still used later."""
    source_positions: dict[str, int] = {}
    source_operation_counts: dict[str, int] = {}
    for plan in source_plans:
        for session in plan["sessions"]:
            session_id = str(session["session_id"])
            source_positions[session_id] = len(source_positions)
            source_operation_counts[session_id] = len(session.get("app_operations") or [])

    corrected_operations = {
        (str(session["session_id"]), operation_index): operation
        for plan in corrected_plans
        for session in plan["sessions"]
        for operation_index, operation in enumerate(session.get("app_operations") or [])
        if isinstance(operation, dict)
    }
    retained_operations = [
        (source_positions[str(session["session_id"])], operation_index, session, operation)
        for plan in corrected_plans
        for session in plan["sessions"]
        for operation_index, operation in enumerate(session.get("app_operations") or [])
        if isinstance(operation, dict)
        and operation_index < source_operation_counts[str(session["session_id"])]
    ]
    for source_row in source_windows:
        for row in _source_operation_epochs(source_row["source_window"]):
            if not isinstance(row, dict):
                continue
            session_id = row.get("session_id")
            operation_index = row.get("operation_index")
            operation = row.get("operation")
            if (
                not isinstance(session_id, str)
                or session_id not in source_positions
                or isinstance(operation_index, bool)
                or not isinstance(operation_index, int)
                or operation_index < 0
                or operation_index >= source_operation_counts[session_id]
                or not isinstance(operation, dict)
            ):
                continue
            tool = operation.get("tool")
            if not isinstance(tool, str) or not (
                tool.startswith("create_") or tool == "add_crm_row"
            ):
                continue
            corrected_operation = corrected_operations.get((session_id, operation_index))
            if (
                isinstance(corrected_operation, dict)
                and corrected_operation.get("tool") == tool
            ):
                continue
            creator_position = (source_positions[session_id], operation_index)
            for record_id in sorted(_returned_record_ids(row.get("result"))):
                for session_position, later_index, later_session, later_operation in retained_operations:
                    if (session_position, later_index) <= creator_position:
                        continue
                    if record_id not in _reference_values(later_operation.get("args")):
                        continue
                    raise ValueError(
                        f"cannot change source session {session_id!r}: created record ID "
                        f"{record_id!r} is still referenced by retained later session "
                        f"{str(later_session['session_id'])!r} tool "
                        f"{later_operation.get('tool')!r}"
                    )


def _validate_operations(sessions: list[dict[str, Any]], *, persona: str) -> None:
    tools = build_history_window.window_tools(exact_persona_tools(persona))
    contact_ids = [str(session.get("contact_id") or "") for session in sessions]
    if not all(contact_ids) or len(contact_ids) != len(set(contact_ids)):
        raise ValueError("corrected sessions must retain unique non-empty contact IDs")
    positions = {contact_id: index for index, contact_id in enumerate(contact_ids)}
    counts = {
        contact_id: len(session.get("app_operations") or [])
        for contact_id, session in zip(contact_ids, sessions)
    }
    errors: list[str] = []
    for session_index, session in enumerate(sessions):
        operations = session.get("app_operations")
        if not isinstance(operations, list):
            errors.append(f"sessions[{session_index}].app_operations must be a list")
            continue
        for operation_index, operation in enumerate(operations):
            label = f"sessions[{session_index}].app_operations[{operation_index}]"
            if not isinstance(operation, dict) or set(operation) != {"tool", "args"}:
                errors.append(f"{label} must contain exactly tool and args")
                continue
            tool = operation.get("tool")
            args = operation.get("args")
            if tool not in tools:
                errors.append(f"{label} uses unknown tool: {tool}")
                continue
            if not isinstance(args, dict):
                errors.append(f"{label}.args must be an object")
                continue
            schema = tools[str(tool)]
            unknown = sorted(set(args) - set(schema.get("arguments") or []))
            missing = sorted(set(schema.get("required_arguments") or []) - set(args))
            if unknown:
                errors.append(f"{label} has unknown args: {unknown}")
            if missing:
                errors.append(f"{label} lacks required args: {missing}")
            errors.extend(
                validate_operation_result_references(
                    args,
                    label=label,
                    current_contact_id=str(session["contact_id"]),
                    current_operation_index=operation_index,
                    contact_positions=positions,
                    operation_counts=counts,
                )
            )
            build_history_window.validate_concrete_choice_arguments(
                operation_label=label, args=args, schema=schema, errors=errors
            )
    if errors:
        raise ValueError("malformed corrected app operations: " + "; ".join(dict.fromkeys(errors)))


def _load_corrections(path: Path) -> tuple[dict[str, dict[str, Any]], set[str], dict[str, Any]]:
    try:
        document = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError("approved-history-corrections must be valid JSON") from exc
    if not isinstance(document, dict) or set(document) != {"replacements", "removals"}:
        raise ValueError("approved-history-corrections must contain exactly replacements and removals")
    replacements = document["replacements"]
    removals = document["removals"]
    if not isinstance(replacements, list) or not isinstance(removals, list):
        raise ValueError("approved-history-corrections replacements and removals must be lists")
    replacement_by_id: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(replacements):
        required_fields = {"session_id", "messages", "purpose", "app_operations"}
        allowed_fields = {
            *required_fields,
            "current_state_changes",
            "new_facts",
            "what_remains_after_contact",
        }
        if not isinstance(row, dict) or not required_fields <= set(row) or set(row) - allowed_fields:
            raise ValueError(
                f"replacements[{index}] must contain session_id, messages, purpose, app_operations, and optional current_state_changes and new_facts"
            )
        session_id = row.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError(f"replacements[{index}].session_id must be a non-empty string")
        if session_id in replacement_by_id:
            raise ValueError(f"approved-history-corrections has duplicate session ID: {session_id}")
        if not isinstance(row["messages"], list) or not row["messages"] or not all(
            isinstance(message, str) and message.strip() for message in row["messages"]
        ):
            raise ValueError(f"replacements[{index}].messages must be a non-empty list of strings")
        if not isinstance(row["purpose"], str) or not row["purpose"].strip():
            raise ValueError(f"replacements[{index}].purpose must be a non-empty string")
        if not isinstance(row["app_operations"], list):
            raise ValueError(f"replacements[{index}].app_operations must be a list")
        if "new_facts" in row and not isinstance(row["new_facts"], list):
            raise ValueError(f"replacements[{index}].new_facts must be a list")
        if "current_state_changes" in row:
            changes = row["current_state_changes"]
            if not isinstance(changes, list):
                raise ValueError(f"replacements[{index}].current_state_changes must be a list")
            seen_keys: set[str] = set()
            for change_index, change in enumerate(changes):
                label = f"replacements[{index}].current_state_changes[{change_index}]"
                if not isinstance(change, dict) or set(change) != build_history_window.CURRENT_STATE_CHANGE_FIELDS:
                    raise ValueError(f"{label} must contain exactly key, subject_ids, statement")
                key = str(change.get("key") or "")
                if not build_history_window.CURRENT_STATE_CHANGE_KEY_RE.fullmatch(key):
                    raise ValueError(f"{label}.key must be a stable lowercase underscore identifier")
                if key in seen_keys:
                    raise ValueError(f"{label}.key appears more than once in this replacement")
                seen_keys.add(key)
                subject_ids = change.get("subject_ids")
                if (
                    not isinstance(subject_ids, list)
                    or not subject_ids
                    or any(not isinstance(subject, str) or not subject.strip() for subject in subject_ids)
                    or len(subject_ids) != len(set(subject_ids))
                ):
                    raise ValueError(f"{label}.subject_ids must be a non-empty unique list of strings")
                if not isinstance(change.get("statement"), str) or not change["statement"].strip():
                    raise ValueError(f"{label}.statement must be a non-empty string")
        if "what_remains_after_contact" in row and (
            not isinstance(row["what_remains_after_contact"], str)
            or not row["what_remains_after_contact"].strip()
        ):
            raise ValueError(
                f"replacements[{index}].what_remains_after_contact must be a non-empty string"
            )
        replacement_by_id[session_id] = copy.deepcopy(row)
    removal_ids: set[str] = set()
    for index, session_id in enumerate(removals):
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError(f"removals[{index}] must be a non-empty session ID")
        if session_id in removal_ids:
            raise ValueError(f"approved-history-corrections has duplicate session ID: {session_id}")
        removal_ids.add(session_id)
    overlap = sorted(set(replacement_by_id) & removal_ids)
    if overlap:
        raise ValueError(f"approved-history-corrections replaces and removes the same session IDs: {overlap}")
    return replacement_by_id, removal_ids, document


def _validate_replacement_new_facts(
    replacements: dict[str, dict[str, Any]],
    *,
    source_sessions: list[dict[str, Any]],
    quarter_plan: dict[str, Any],
) -> None:
    """Allow statement-only corrections unless the accepted quarter plan defines the fact."""
    source_by_id = {
        str(session["session_id"]): session
        for session in source_sessions
    }
    plan = quarter_plan.get("plan") if isinstance(quarter_plan.get("plan"), dict) else quarter_plan
    planned_statements: dict[str, str] = {}
    for event in plan.get("events") or []:
        if not isinstance(event, dict):
            continue
        for fact in event.get("facts_to_establish") or []:
            if not isinstance(fact, dict):
                continue
            key = fact.get("key")
            statement = fact.get("statement")
            if not isinstance(key, str) or not key or not isinstance(statement, str) or not statement:
                continue
            previous = planned_statements.setdefault(key, statement)
            if previous != statement:
                raise ValueError(
                    f"accepted quarter plan defines conflicting statements for fact {key!r}"
                )
    protected_fields = (
        "key",
        "applies_when",
        "subjects",
        "supersedes",
        "evidence_contact_ids",
    )
    for session_id, replacement in replacements.items():
        if "new_facts" not in replacement:
            continue
        source_facts = source_by_id[session_id].get("new_facts")
        if not isinstance(source_facts, list):
            raise ValueError(
                f"source session {session_id} new_facts must be a list"
            )
        replacement_facts = replacement["new_facts"]
        if len(replacement_facts) != len(source_facts):
            raise ValueError(
                f"replacement {session_id} new_facts must preserve the source fact count"
            )
        for fact_index, (source_fact, replacement_fact) in enumerate(
            zip(source_facts, replacement_facts)
        ):
            label = f"replacement {session_id} new_facts[{fact_index}]"
            if not isinstance(source_fact, dict) or set(source_fact) != build_history_window.FACT_FIELDS:
                raise ValueError(
                    f"source session {session_id} new_facts[{fact_index}] does not have the canonical fact shape"
                )
            if (
                not isinstance(replacement_fact, dict)
                or set(replacement_fact) != build_history_window.FACT_FIELDS
            ):
                raise ValueError(
                    f"{label} must contain exactly key, statement, applies_when, subjects, supersedes, and evidence_contact_ids"
                )
            statement = replacement_fact["statement"]
            if not isinstance(statement, str) or not statement.strip():
                raise ValueError(f"{label}.statement must be a non-empty string")
            planned_statement = planned_statements.get(str(replacement_fact["key"]))
            if planned_statement is not None and statement != planned_statement:
                raise ValueError(
                    f"{label}.statement must exactly match the accepted quarter plan"
                )
            for field in protected_fields:
                source_value = json.dumps(
                    source_fact[field],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                replacement_value = json.dumps(
                    replacement_fact[field],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if replacement_value != source_value:
                    raise ValueError(
                        f"{label}.{field} must exactly match the source fact at the same position"
                    )


def _validate_replacement_current_state_changes(
    replacements: dict[str, dict[str, Any]],
    *,
    known_entity_ids: set[str],
) -> None:
    """Check corrected ordinary states against the rewritten user messages."""
    for session_id, replacement in replacements.items():
        normalized_messages = " ".join(
            "\n".join(str(message) for message in replacement["messages"]).split()
        )
        for index, change in enumerate(replacement.get("current_state_changes") or []):
            label = f"replacement {session_id} current_state_changes[{index}]"
            unknown = sorted(
                str(subject)
                for subject in change["subject_ids"]
                if str(subject) not in known_entity_ids
            )
            if unknown:
                raise ValueError(f"{label}.subject_ids uses unknown entity IDs: {unknown}")
            statement = " ".join(str(change["statement"]).split())
            if statement not in normalized_messages:
                raise ValueError(
                    f"{label}.statement must appear verbatim in the corrected messages"
                )


def _validate_removals(
    sessions: list[dict[str, Any]],
    *,
    removal_ids: set[str],
    replacements: dict[str, dict[str, Any]],
    new_entities: list[dict[str, Any]],
    starting_state: dict[str, Any],
) -> None:
    by_id = {str(session.get("session_id") or ""): session for session in sessions}
    unknown = sorted(removal_ids - set(by_id))
    if unknown:
        raise ValueError(f"approved-history-corrections names unknown or outside-quarter session IDs: {unknown}")
    contact_by_id = {session_id: str(session.get("contact_id") or "") for session_id, session in by_id.items()}
    removed_contacts = {contact_by_id[session_id] for session_id in removal_ids}
    removed_threads = {
        str(by_id[session_id].get("thread_id") or "")
        for session_id in removal_ids
        if str(by_id[session_id].get("thread_id") or "")
    }
    introduced_contacts = {
        str(entity.get("introduced_in_contact_id") or "")
        for entity in new_entities
        if isinstance(entity, dict)
    }
    thread_members = {
        thread_id: {
            session_id
            for session_id, session in by_id.items()
            if str(session.get("thread_id") or "") == thread_id
        }
        for thread_id in removed_threads
    }
    state_change_keys_by_session_id: dict[str, set[str]] = {}
    state_change_session_ids_by_key: dict[str, set[str]] = {}
    replacement_state_change_session_ids_by_key: dict[str, set[str]] = {}
    session_position = {session_id: index for index, session_id in enumerate(by_id)}
    for session_id, session in by_id.items():
        occurrence = session.get("_correction_occurrence") or {}
        replacement = replacements.get(session_id)
        replacement_supplies_state = (
            replacement is not None and "current_state_changes" in replacement
        )
        changes = (
            replacement["current_state_changes"]
            if replacement_supplies_state
            else occurrence.get("current_state_changes")
            if isinstance(occurrence, dict)
            else []
        )
        if changes is None:
            changes = []
        if not isinstance(changes, list):
            raise ValueError(
                f"cannot remove session {session_id}: current_state_changes must be a list"
            )
        keys: set[str] = set()
        for change in changes:
            if not isinstance(change, dict) or not str(change.get("key") or "").strip():
                raise ValueError(
                    f"cannot remove session {session_id}: current_state_changes must use non-empty keys"
                )
            keys.add(str(change["key"]).strip())
        state_change_keys_by_session_id[session_id] = keys
        for key in keys:
            state_change_session_ids_by_key.setdefault(key, set()).add(session_id)
            if replacement_supplies_state:
                replacement_state_change_session_ids_by_key.setdefault(
                    key, set()
                ).add(session_id)
    starting_state_keys = {
        str(row["key"])
        for row in build_history_window.current_ordinary_states_from_session_purposes(
            starting_state
        )
    }
    for session_id in removal_ids:
        session = by_id[session_id]
        contact_id = contact_by_id[session_id]
        if session.get("development_ids"):
            raise ValueError(
                f"cannot remove session {session_id}: it covers an accepted quarter development"
            )
        has_durable_state = bool(session.get("new_facts")) or contact_id in introduced_contacts
        if has_durable_state:
            raise ValueError(f"cannot remove session {session_id}: it establishes a durable fact or introduces an entity")
        thread_id = str(session.get("thread_id") or "")
        opens_or_continues = bool(session.get("thread_open_after_contact")) or bool(session.get("continues_from"))
        if opens_or_continues:
            if not thread_id or thread_members.get(thread_id, set()) - removal_ids:
                raise ValueError(f"cannot remove session {session_id}: partial-thread removal is not allowed")
            for thread_session_id in thread_members[thread_id]:
                thread_session = by_id[thread_session_id]
                thread_contact = contact_by_id[thread_session_id]
                if thread_session.get("new_facts") or thread_contact in introduced_contacts:
                    raise ValueError(f"cannot remove thread {thread_id}: it establishes a durable fact or introduces an entity")
        state_keys = state_change_keys_by_session_id[session_id]
        if state_keys:
            if thread_id and thread_members.get(thread_id, set()) - removal_ids:
                raise ValueError(
                    f"cannot remove session {session_id}: current_state_changes require complete-thread removal"
                )
            for key in sorted(state_keys):
                if key in starting_state_keys:
                    raise ValueError(
                        f"cannot remove session {session_id}: current-state key {key} existed before this quarter"
                    )
                retained_changers = state_change_session_ids_by_key[key] - removal_ids
                latest_retained_changer = (
                    max(retained_changers, key=session_position.__getitem__)
                    if retained_changers
                    else None
                )
                replacement_owns_final_state = latest_retained_changer in (
                    replacement_state_change_session_ids_by_key.get(key) or set()
                )
                if (
                    not thread_id
                    and retained_changers
                    and not replacement_owns_final_state
                ):
                    raise ValueError(
                        f"cannot remove session {session_id}: current_state_changes require complete-thread removal or an explicit retained replacement for key {key}"
                    )
                if retained_changers and not replacement_owns_final_state:
                    raise ValueError(
                        f"cannot remove session {session_id}: retained sessions change current-state key {key}"
                    )
    removed_references = removal_ids | removed_contacts | removed_threads
    for index, session in enumerate(sessions):
        if str(session.get("session_id") or "") in removal_ids:
            continue
        references = _session_references(session)
        if str(session.get("thread_id") or "") in removed_threads:
            references.add(str(session["thread_id"]))
        overlap = sorted(value for value in references & removed_references if value)
        if overlap:
            raise ValueError(
                f"cannot remove sessions: retained session {session.get('session_id') or index} references {overlap}"
            )


def _supported_generation_record(manifest: dict[str, Any], model_calls: Any) -> bool:
    """Accept legacy call counts or the current recorded generation stages."""
    if isinstance(model_calls, bool) or not isinstance(model_calls, int):
        return False
    stages = manifest.get("generation_stages")
    if stages is None:
        return model_calls in {2, 3}
    if not isinstance(stages, list) or not all(isinstance(stage, str) for stage in stages):
        return False

    expected = ["story_planning", "occurrence_enrichment"]
    if "unexecutable_contact_correction" in stages or "corrected_occurrence_enrichment" in stages:
        expected.extend(
            ["unexecutable_contact_correction", "corrected_occurrence_enrichment"]
        )
    if "missing_state_correction" in stages:
        expected.append("missing_state_correction")
    expected.append("session_writing")
    return stages == expected and model_calls == len(expected)


def _authenticate_source(
    *,
    source: Path,
    expected_window_config: dict[str, Any],
    windows: list[tuple[date, date]],
    starting_checkpoint: Path,
    starting_identity: str,
    persona: str,
) -> tuple[list[dict[str, Any]], Path, str, int]:
    manifest_path = source / "manifest.json"
    if not source.is_dir() or not manifest_path.is_file():
        raise ValueError("source-failed-quarter must be a failed quarter-run directory")
    manifest = _require_object(load_json(manifest_path), "source quarter manifest")
    if manifest.get("status") != "failed" or manifest.get("stage") != "history_quarter_review":
        raise ValueError("source-failed-quarter must have failed at history_quarter_review")
    if (source / "final_checkpoint").exists():
        raise ValueError("source-failed-quarter must not contain final_checkpoint")
    config_path = source / "window_config.yaml"
    if not config_path.is_file() or load_yaml(config_path) != expected_window_config:
        raise ValueError("source-failed-quarter window config does not match current config")
    records = manifest.get("windows")
    if not isinstance(records, list) or len(records) != len(windows):
        raise ValueError("source-failed-quarter must record every configured window")
    checkpoint_path = starting_checkpoint
    checkpoint_identity = starting_identity
    authenticated: list[dict[str, Any]] = []
    accepted_generation_calls = 0
    for index, (window_start, window_end) in enumerate(windows):
        record = records[index]
        if not isinstance(record, dict):
            raise ValueError("source-failed-quarter window record must be an object")
        window_dir = source / "windows" / f"{window_start.isoformat()}_to_{window_end.isoformat()}"
        window_manifest_path = window_dir / "manifest.json"
        if not window_manifest_path.is_file():
            raise ValueError(f"source-failed-quarter window is missing manifest: {window_dir}")
        window_manifest = _require_object(load_json(window_manifest_path), "source window manifest")
        if record.get("status") != "passed" or window_manifest.get("status") != "passed":
            raise ValueError("source-failed-quarter requires every window to have passed")
        if record.get("start_date") != window_start.isoformat() or record.get("end_date") != window_end.isoformat():
            raise ValueError("source-failed-quarter window order does not match current config")
        if window_manifest.get("persona") != persona or window_manifest.get("period_start") != window_start.isoformat() or window_manifest.get("period_end") != window_end.isoformat():
            raise ValueError("source-failed-quarter window manifest does not match current config")
        if window_manifest.get("starting_checkpoint_sha256") != checkpoint_identity:
            raise ValueError("source-failed-quarter checkpoint chain is not authenticated")
        source_generation_calls = window_manifest.get("model_calls")
        generation_calls_are_preserved = (
            window_manifest.get("mode") == "history_quarter_correction"
            and source_generation_calls == 0
            and isinstance(
                window_manifest.get("accepted_source_generation_model_calls"), int
            )
            and window_manifest.get("accepted_source_generation_model_calls")
            == record.get("accepted_source_generation_model_calls")
        )
        if generation_calls_are_preserved:
            source_generation_calls = window_manifest[
                "accepted_source_generation_model_calls"
            ]
        generation_manifest = window_manifest
        if generation_calls_are_preserved:
            preserved_manifest_path = _accepted_generation_dir(window_dir) / "manifest.json"
            if not preserved_manifest_path.is_file():
                raise ValueError("source-failed-quarter correction window is missing accepted generation provenance")
            preserved_manifest = _require_object(
                load_json(preserved_manifest_path), "accepted source generation manifest"
            )
            generation_manifest = preserved_manifest
            if (
                preserved_manifest.get("status") != "passed"
                or preserved_manifest.get("persona") != persona
                or preserved_manifest.get("period_start") != window_start.isoformat()
                or preserved_manifest.get("period_end") != window_end.isoformat()
                or preserved_manifest.get("model_calls") != source_generation_calls
            ):
                raise ValueError("accepted source generation provenance does not describe the authenticated window")
        if not _supported_generation_record(
            generation_manifest, source_generation_calls
        ):
            raise ValueError(
                "source-failed-quarter window has an unsupported generation-call record"
            )
        candidate = window_dir / "candidate_checkpoint"
        candidate_identity = validated_checkpoint_identity(candidate)
        if window_manifest.get("candidate_checkpoint_sha256") != candidate_identity:
            raise ValueError("source-failed-quarter candidate checkpoint identity does not match manifest")
        for field in (
            "starting_checkpoint_sha256",
            "candidate_checkpoint_sha256",
            "sessions",
            "new_facts",
            "app_operations",
            "model_calls",
        ):
            if record.get(field) != window_manifest.get(field):
                raise ValueError(
                    f"source-failed-quarter root and window manifests disagree on {field}"
                )
        metadata, _ = load_checkpoint(candidate)
        if metadata.get("previous_checkpoint_sha256") != checkpoint_identity:
            raise ValueError("source-failed-quarter checkpoint chain does not match")
        if str(metadata.get("accepted_through") or "") != window_end.isoformat():
            raise ValueError("source-failed-quarter checkpoint accepted_through does not match window")
        authenticated.append({
            "source_window": window_dir,
            "source_record": copy.deepcopy(record),
            "accepted_source_generation_model_calls": source_generation_calls,
        })
        accepted_generation_calls += source_generation_calls
        checkpoint_path = candidate
        checkpoint_identity = candidate_identity
    return authenticated, checkpoint_path, checkpoint_identity, accepted_generation_calls


def _ledger_summary(ledger: Path) -> dict[str, int]:
    summary = {"paid_calls": 0, "cache_hits": 0}
    with ledger.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                raise ValueError(f"call ledger row {line_number} is blank")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"call ledger row {line_number} is not valid JSON") from exc
            if not isinstance(row, dict) or not isinstance(row.get("cache_hit"), bool):
                raise ValueError(f"call ledger row {line_number} cache_hit must be a boolean")
            summary["cache_hits" if row["cache_hit"] else "paid_calls"] += 1
    summary["ledger_entries"] = summary["paid_calls"] + summary["cache_hits"]
    return summary


def run(
    *,
    args: Any,
    validated: dict[str, Any],
    config: dict[str, Any],
    expected_window_config: dict[str, Any],
    windows: list[tuple[date, date]],
    review_api: Any,
) -> dict[str, Any]:
    source = Path(args.source_failed_quarter).resolve()
    corrections_path = Path(args.approved_history_corrections).resolve()
    output = Path(args.out).resolve()
    if output.exists():
        raise ValueError("correction --out must be a new directory")
    if not corrections_path.is_file():
        raise ValueError("approved-history-corrections is not a file")
    tool_python = resolve_path(args.tool_python)
    build_history_window.validate_tool_python(tool_python)
    paths = validated["paths"]
    source_windows, _, _, accepted_generation_calls = _authenticate_source(
        source=source,
        expected_window_config=expected_window_config,
        windows=windows,
        starting_checkpoint=paths["starting_checkpoint"],
        starting_identity=validated["starting_checkpoint_identity"],
        persona=config["persona"],
    )
    replacements, removals, _ = _load_corrections(corrections_path)

    source_sessions: list[dict[str, Any]] = []
    source_entities: list[dict[str, Any]] = []
    plans_by_window: list[dict[str, Any]] = []
    source_purposes = _source_session_purposes(source_windows[-1]["source_window"])
    for source_row in source_windows:
        plan = _source_plan(source_row["source_window"])
        occurrences = _source_occurrences(source_row["source_window"])
        for session in plan["sessions"]:
            if not isinstance(session, dict) or not isinstance(session.get("session_id"), str):
                raise ValueError("source plan sessions must have session IDs")
            occurrence = copy.deepcopy(
                occurrences.get(str(session.get("contact_id")), {})
            )
            accepted_purpose = source_purposes.get(str(session["session_id"]))
            if accepted_purpose is not None:
                occurrence["current_state_changes"] = copy.deepcopy(
                    accepted_purpose.get("current_state_changes") or []
                )
            session["_correction_occurrence"] = occurrence
            source_sessions.append(session)
        source_entities.extend(plan["new_entities"])
        plans_by_window.append(plan)
    source_ids = [str(session["session_id"]) for session in source_sessions]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("source-failed-quarter has duplicate session IDs")
    unknown_replacements = sorted(set(replacements) - set(source_ids))
    if unknown_replacements:
        raise ValueError(f"approved-history-corrections names unknown or outside-quarter session IDs: {unknown_replacements}")
    _validate_replacement_new_facts(
        replacements,
        source_sessions=source_sessions,
        quarter_plan=_require_object(
            load_json(paths["quarter_plan"]), "accepted quarter plan"
        ),
    )
    _, starting_state = load_checkpoint(paths["starting_checkpoint"])
    _validate_replacement_current_state_changes(
        replacements,
        known_entity_ids={
            str(entity.get("id"))
            for entity in [*(starting_state.get("entities") or []), *source_entities]
            if isinstance(entity, dict) and str(entity.get("id") or "").strip()
        },
    )
    _validate_removals(
        source_sessions,
        removal_ids=removals,
        replacements=replacements,
        new_entities=source_entities,
        starting_state=starting_state,
    )
    source_plans = copy.deepcopy(plans_by_window)

    for plan in plans_by_window:
        retained: list[dict[str, Any]] = []
        for session in plan["sessions"]:
            session_id = str(session["session_id"])
            if session_id in removals:
                continue
            corrected = copy.deepcopy(session)
            corrected.pop("_correction_occurrence", None)
            replacement = replacements.get(session_id)
            if replacement is not None:
                for field in ("messages", "purpose", "app_operations"):
                    corrected[field] = copy.deepcopy(replacement[field])
                if "what_remains_after_contact" in replacement:
                    if corrected.get("thread_open_after_contact") is not True:
                        raise ValueError(
                            "what_remains_after_contact can change only on an open thread: "
                            + session_id
                        )
                    corrected["what_remains_after_contact"] = replacement[
                        "what_remains_after_contact"
                    ]
                if "new_facts" in replacement:
                    corrected["new_facts"] = copy.deepcopy(replacement["new_facts"])
            retained.append(corrected)
        plan["sessions"] = retained
        introduced_contacts = {str(session.get("contact_id") or "") for session in retained}
        plan["new_entities"] = [
            entity for entity in plan["new_entities"]
            if str(entity.get("introduced_in_contact_id") or "") in introduced_contacts
        ]
        _validate_operations(retained, persona=config["persona"])
    _validate_retained_record_dependencies(
        source_windows=source_windows,
        source_plans=source_plans,
        corrected_plans=plans_by_window,
    )
    replay_epoch_ms_by_operation = _correction_replay_epoch_mapping(
        source_windows=source_windows,
        source_plans=source_plans,
        corrected_plans=plans_by_window,
    )

    output.mkdir(parents=True)
    _copy_root_provenance(source, output)
    shutil.copy2(corrections_path, output / "approved_history_corrections.json")
    dump_json(output / "correction_provenance.json", {
        "mode": "history_quarter_correction",
        "source_failed_quarter": str(source),
        "approved_history_corrections": str(corrections_path),
        "accepted_source_generation_calls": accepted_generation_calls,
        "fresh_model_calls": 1,
    })
    root_ledger = output / "call_ledger.jsonl"
    root_ledger.touch()
    window_config_path = output / "window_config.yaml"
    window_config_path.write_text(yaml.safe_dump(expected_window_config, sort_keys=False, allow_unicode=True))

    checkpoint_path = paths["starting_checkpoint"]
    checkpoint_identity = validated["starting_checkpoint_identity"]
    records: list[dict[str, Any]] = []
    totals = {"sessions": 0, "new_facts": 0, "app_operations": 0, "model_calls": 0}
    stage = "local_correction_replay"
    try:
        for index, ((window_start, window_end), source_row, plan) in enumerate(
            zip(windows, source_windows, plans_by_window), start=1
        ):
            stage = f"window_{index:02d}_{window_start}_to_{window_end}"
            source_window = source_row["source_window"]
            output_window = output / "windows" / source_window.name
            _copy_source_provenance(source_window, output_window)
            occurrences = _source_occurrences(source_window)
            dump_json(output_window / "plan.json", plan)
            rendered = build_history_window.rendered_sessions(plan)
            dump_json(output_window / "rendered_sessions.json", {"sessions": rendered})
            projected_state, operation_results, app_changes = build_history_window.project_app_operations(
                persona=config["persona"],
                plan={"contacts": plan["sessions"]},
                starting_state=(load_checkpoint(checkpoint_path)[1]["app_state"]),
                work_dir=output_window / "work" / "operation_replay",
                tool_python=tool_python,
                replay_epoch_ms_by_operation=replay_epoch_ms_by_operation,
            )
            dump_json(output_window / "plan.json", plan)
            metadata, starting_state = load_checkpoint(checkpoint_path)
            operation_path = checkpoint_path / "operation_results.json"
            starting_operations = load_json(operation_path) if operation_path.is_file() else []
            if not isinstance(starting_operations, list):
                raise ValueError("checkpoint operation_results.json must contain a list")
            checkpoint_operations = build_history_window.write_window_operation_results(
                run_output=output_window,
                starting_rows=starting_operations,
                current_rows=operation_results,
            )
            current_state_changes = {
                str(session["contact_id"]): copy.deepcopy(
                    replacements.get(str(session.get("session_id")), {}).get(
                        "current_state_changes",
                        source_purposes.get(str(session.get("session_id")), {}).get(
                            "current_state_changes",
                            (occurrences.get(str(session.get("contact_id"))) or {}).get(
                                "current_state_changes"
                            ) or [],
                        ),
                    )
                )
                for session in plan["sessions"]
            }
            details = {
                str(session["contact_id"]): (
                    build_history_window.accepted_contact_details_for_rendered_session(
                        session
                    )
                )
                for session in plan["sessions"]
            }
            assembled, new_entities, new_facts = build_history_window.materialize_window_state(
                starting_state=starting_state,
                plan=plan,
                projected_app_state=projected_state,
                accepted_contact_details_by_contact_id=details,
                current_state_changes_by_contact_id=current_state_changes,
            )
            dump_json(output_window / "app_state_changes.json", app_changes)
            dump_json(output_window / "new_entities.json", new_entities)
            dump_json(output_window / "new_facts.json", new_facts)
            candidate = output_window / "candidate_checkpoint"
            build_history_window.emit_candidate_checkpoint(
                path=candidate,
                run_output=output_window,
                save_kwargs={
                    "persona": config["persona"],
                    "week": {"week_id": f"{window_start}_to_{window_end}", "start_date": window_start.isoformat(), "end_date": window_end.isoformat()},
                    "previous_hash": checkpoint_identity,
                    "inputs": {
                        "correction_provenance.json": file_hash(output / "correction_provenance.json"),
                        "approved_history_corrections.json": file_hash(output / "approved_history_corrections.json"),
                        "plan.json": file_hash(output_window / "plan.json"),
                    },
                    "state": assembled,
                    "operation_results": checkpoint_operations,
                    "covered_events": list(
                        dict.fromkeys(
                            [
                                *[str(value) for value in metadata.get("covered_quarter_event_ids") or []],
                                *[
                                    str(development_id)
                                    for session in plan["sessions"]
                                    for development_id in session.get("development_ids") or []
                                ],
                            ]
                        )
                    ),
                    "system_sha256": build_history_window.system_hash(config["persona"]),
                    "revalidated_against_current_state": True,
                    "progress": {"construction_path": "history_quarter_correction_replay"},
                    "identity_version": 2,
                },
            )
            candidate_identity = validated_checkpoint_identity(candidate)
            record = {
                "status": "passed", "mode": "history_quarter_correction",
                "persona": config["persona"], "period_start": window_start.isoformat(),
                "period_end": window_end.isoformat(), "start_date": window_start.isoformat(),
                "end_date": window_end.isoformat(),
                "output": str(output_window), "starting_checkpoint_sha256": checkpoint_identity,
                "candidate_checkpoint_sha256": candidate_identity,
                "sessions": len(plan["sessions"]), "new_facts": len(new_facts),
                "app_operations": len(operation_results), "model_calls": 0,
                "accepted_source_generation_model_calls": source_row[
                    "accepted_source_generation_model_calls"
                ],
                "source_window": str(source_window),
            }
            dump_json(output_window / "manifest.json", record)
            records.append(record)
            for field in ("sessions", "new_facts", "app_operations", "model_calls"):
                totals[field] += record[field]
            checkpoint_path, checkpoint_identity = candidate, candidate_identity

        stage = "history_quarter_review"
        quarter_plan = load_json(paths["quarter_plan"])
        review_request = review_api.build_review_request(
            config=config, quarter_plan=quarter_plan, starting_checkpoint=paths["starting_checkpoint"],
            final_checkpoint=checkpoint_path, window_records=records,
        )
        os.environ["DOLPHINBENCH_CALL_LEDGER"] = str(root_ledger)
        os.environ["DOLPHINBENCH_CONSTRUCTION_RUN_ID"] = output.name
        review = review_api.run_review(
            output_dir=output, request=review_request,
            rendered_input=review_api.render_review_input(review_request),
        )
        totals["model_calls"] = 1
        ledger_summary = _ledger_summary(root_ledger)
        if not review["validation"]["valid"]:
            raise ValueError("quarter history review response was invalid: " + "; ".join(review["validation"]["errors"]))
        if not review["validation"]["passed"]:
            raise ValueError(f"quarter history review found {review['validation']['finding_count']} concrete defect(s)")
        stage = "final_checkpoint"
        final = output / "final_checkpoint"
        pending = output / f".final_checkpoint.pending-{uuid.uuid4().hex}"
        shutil.copytree(checkpoint_path, pending)
        pending.replace(final)
        manifest = {
            "status": "passed", "mode": "history_quarter_correction", "persona": config["persona"],
            "quarter_id": config["quarter_id"], "quarter_start": validated["quarter_start"].isoformat(),
            "quarter_end": validated["quarter_end"].isoformat(), "source_failed_quarter": str(source),
            "approved_history_corrections": str(corrections_path), "window_config": str(window_config_path),
            "windows": records, "totals": totals,
            "model_calls": {
                "expected_fresh": 1,
                "accepted_source_generation_calls": accepted_generation_calls,
                "accepted_stage_calls": 1,
                **ledger_summary,
                "actual": ledger_summary["paid_calls"],
            },
            "call_ledger": str(root_ledger), "quarter_review": review,
            "final_checkpoint": str(final), "final_checkpoint_identity": validated_checkpoint_identity(final),
        }
        dump_json(output / "manifest.json", manifest)
        return manifest
    except Exception as exc:
        ledger_summary = _ledger_summary(root_ledger)
        dump_json(output / "manifest.json", {
            "status": "failed", "mode": "history_quarter_correction", "stage": stage,
            "error": f"{type(exc).__name__}: {exc}", "windows": records, "totals": totals,
            "source_failed_quarter": str(source), "approved_history_corrections": str(corrections_path),
            "model_calls": {
                "expected_fresh": 1,
                "accepted_source_generation_calls": accepted_generation_calls,
                "accepted_stage_calls": totals["model_calls"],
                **ledger_summary,
                "actual": ledger_summary["paid_calls"],
            },
        })
        raise
