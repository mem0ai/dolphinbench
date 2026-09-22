"""Deterministic state operations shared by canonical construction stages."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable

from construction.checkpoints import dump_json, load_json
from construction.runtime_operations import execute_operations, operation_errors


def visible_fields(value: dict[str, Any]) -> set[str]:
    if not isinstance(value, dict):
        return set()
    return {str(key) for key in value if not str(key).startswith("_")}


def require_exact_fields(
    value: Any, expected: set[str], label: str, errors: list[str]
) -> bool:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return False
    actual = visible_fields(value)
    if actual != expected:
        errors.append(
            f"{label} must contain exactly {sorted(expected)}; got {sorted(actual)}"
        )
        return False
    return True


def nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def string_list(value: Any, *, allow_empty: bool = True) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(nonempty_string(item) for item in value)
        and len(value) == len(set(value))
    )


def current_existing_fact_ids(facts: list[dict[str, Any]]) -> set[int]:
    superseded = {
        int(reference)
        for fact in facts
        for reference in fact.get("supersedes") or []
        if isinstance(reference, int) and not isinstance(reference, bool)
    }
    return {
        int(fact["id"])
        for fact in facts
        if isinstance(fact.get("id"), int)
        and not isinstance(fact.get("id"), bool)
        and int(fact["id"]) not in superseded
    }


def fact_reference(value: Any) -> int | str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if nonempty_string(value):
        return str(value)
    return None


def changed_app_state(
    before: dict[str, Any], after: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return exact changed values without copying complete app stores."""
    missing = object()
    changes: list[dict[str, Any]] = []

    def walk(left: Any, right: Any, path: list[str | int]) -> None:
        if left is not missing and right is not missing and left == right:
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                walk(left.get(key, missing), right.get(key, missing), [*path, key])
            return
        if isinstance(left, list) and isinstance(right, list):
            for index in range(max(len(left), len(right))):
                walk(
                    left[index] if index < len(left) else missing,
                    right[index] if index < len(right) else missing,
                    [*path, index],
                )
            return
        if left is missing:
            changes.append({"path": path, "change": "added", "after": right})
        elif right is missing:
            changes.append({"path": path, "change": "removed", "before": left})
        else:
            changes.append(
                {"path": path, "change": "changed", "before": left, "after": right}
            )

    walk(before, after, [])
    return changes


def project_app_operations(
    *,
    persona: str,
    plan: dict[str, Any],
    starting_state: dict[str, Any],
    work_dir: Path,
    tool_python: Path,
    executor: Callable[..., list[dict[str, Any]]] = execute_operations,
    replay_epoch_ms_by_operation: dict[tuple[str, int], int] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Replay declared operations in order with deterministic result identifiers."""
    work_dir.mkdir(parents=True, exist_ok=True)
    before = copy.deepcopy(starting_state)
    state_path = work_dir / "projected_app_state.json"
    log_path = work_dir / "projected_tool_calls.jsonl"
    log_path.unlink(missing_ok=True)
    dump_json(work_dir / "app_state_before.json", before)
    dump_json(state_path, before)
    ordered = [
        {
            "contact_id": str(contact.get("contact_id") or contact["session_id"]),
            "session_id": contact["session_id"],
            "narrative_date": contact["narrative_date"],
            "app_operations": copy.deepcopy(contact["app_operations"]),
        }
        for contact in plan["contacts"]
    ]
    results = executor(
        persona=persona,
        plans=ordered,
        state_path=state_path,
        log_path=log_path,
        tool_python=tool_python,
        deterministic_replay=True,
        replay_epoch_ms_by_operation=replay_epoch_ms_by_operation,
    )
    contacts_by_id = {
        str(contact.get("contact_id") or contact["session_id"]): contact
        for contact in plan["contacts"]
    }
    for row in results:
        contact = contacts_by_id.get(str(row.get("contact_id") or ""))
        operation_index = row.get("operation_index")
        if (
            contact is not None
            and isinstance(operation_index, int)
            and not isinstance(operation_index, bool)
            and 0 <= operation_index < len(contact["app_operations"])
        ):
            contact["app_operations"][operation_index] = copy.deepcopy(
                row["operation"]
            )
    errors = operation_errors(results)
    dump_json(work_dir / "operation_results.json", results)
    if errors:
        dump_json(
            work_dir / "operation_validation.json",
            {"passed": False, "errors": errors},
        )
        raise ValueError("planned app operation failed: " + "; ".join(errors))
    after = load_json(state_path)
    changes = changed_app_state(before, after)
    dump_json(work_dir / "app_state_changes.json", changes)
    dump_json(
        work_dir / "operation_validation.json", {"passed": True, "errors": []}
    )
    return after, results, changes


def materialize_entities(
    existing: list[dict[str, Any]], plan: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    result = copy.deepcopy(existing)
    session_by_contact = {
        str(contact["contact_id"]): str(contact["session_id"])
        for contact in plan["contacts"]
    }
    added: list[dict[str, Any]] = []
    for planned in plan["new_entities"]:
        entity = {
            "id": str(planned["id"]),
            "name": str(planned["name"]),
            "kind": str(planned["kind"]),
            "aliases": [],
            "introduced_in": session_by_contact[
                str(planned["introduced_in_contact_id"])
            ],
            "reason": str(planned["reason"]),
        }
        result.append(entity)
        added.append(entity)
    return result, added


def materialize_facts(
    existing: list[dict[str, Any]],
    plan: dict[str, Any],
    *,
    accepted_history: list[dict[str, Any]] | None = None,
    inherited_source_fact_ids_by_key: dict[str, list[int]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Materialize facts and carry evidence forward through replacements.

    The model supplies only the contacts that establish the changed information.
    For a replacement, prior source sessions are derived from the facts it retires
    and from the accepted replacement-accounting sidecar. They are never accepted
    from model output.
    """
    result = copy.deepcopy(existing)
    existing_keys = {
        str(row["construction_key"])
        for row in existing
        if isinstance(row, dict) and row.get("construction_key") is not None
    }
    planned_keys: set[str] = set()
    for contact in plan["contacts"]:
        for fact in contact["new_facts"]:
            fact_key = str(fact["key"])
            if fact_key in existing_keys:
                raise ValueError(
                    f"fact key {fact_key} already exists in starting facts"
                )
            if fact_key in planned_keys:
                raise ValueError(
                    f"fact key {fact_key} appears more than once in the current plan"
                )
            planned_keys.add(fact_key)

    next_id = max((int(row["id"]) for row in existing), default=0) + 1
    key_to_id: dict[str, int] = {}
    for contact in plan["contacts"]:
        for fact in contact["new_facts"]:
            key_to_id[str(fact["key"])] = next_id
            next_id += 1
    session_by_contact = {
        str(contact["contact_id"]): str(contact["session_id"])
        for contact in plan["contacts"]
    }
    contact_order = {
        str(contact["contact_id"]): index
        for index, contact in enumerate(plan["contacts"])
    }
    accepted_source_session_ids = [
        str(row["id"])
        for row in accepted_history or []
        if isinstance(row, dict) and nonempty_string(row.get("id"))
    ]
    if len(accepted_source_session_ids) != len(set(accepted_source_session_ids)):
        raise ValueError("accepted source session IDs must be unique and chronological")
    source_session_order = {
        session_id: index
        for index, session_id in enumerate(accepted_source_session_ids)
    }
    source_session_order.update(
        {
            session_by_contact[contact_id]: len(accepted_source_session_ids) + index
            for contact_id, index in contact_order.items()
        }
    )
    facts_by_id = {
        int(row["id"]): row
        for row in result
        if isinstance(row, dict)
        and isinstance(row.get("id"), int)
        and not isinstance(row.get("id"), bool)
    }
    current_fact_ids = current_existing_fact_ids(result)
    superseded_fact_ids_by_development: dict[str, set[int]] = {}
    available_source_session_ids = set(accepted_source_session_ids)
    inherited_source_fact_ids_by_key = inherited_source_fact_ids_by_key or {}
    if not isinstance(inherited_source_fact_ids_by_key, dict):
        raise ValueError("inherited source fact IDs must be a mapping by fact key")

    def source_sessions_for(fact_id: int) -> list[str]:
        source_fact = facts_by_id.get(fact_id)
        if source_fact is None:
            raise ValueError(f"replacement references unknown fact {fact_id}")
        sources = source_fact.get("source_session_ids") or []
        if not isinstance(sources, list) or not all(nonempty_string(value) for value in sources):
            raise ValueError(
                f"replacement source fact {fact_id} has invalid source_session_ids"
            )
        return [str(value) for value in sources]

    added: list[dict[str, Any]] = []
    for contact_index, contact in enumerate(plan["contacts"]):
        contact_id = str(contact["contact_id"])
        session_id = session_by_contact[contact_id]
        available_source_session_ids.add(session_id)
        facts_current_before_contact = set(current_fact_ids)
        contact_development_ids = {
            str(value) for value in contact.get("development_ids") or []
        }
        fact_ids_replaced_by_same_development = {
            fact_id
            for development_id in contact_development_ids
            for fact_id in superseded_fact_ids_by_development.get(
                development_id, set()
            )
        }
        facts_superseded_in_contact: set[int] = set()
        fact_ids_added_in_contact: set[int] = set()
        for planned in contact["new_facts"]:
            fact_key = str(planned["key"])
            supersedes = [
                int(reference)
                if isinstance(reference, int)
                else key_to_id[str(reference)]
                for reference in planned["supersedes"]
            ]
            if len(supersedes) != len(set(supersedes)):
                raise ValueError(
                    f"fact {planned['key']} has duplicate supersedes references"
            )
            for fact_id in supersedes:
                if (
                    fact_id not in facts_current_before_contact
                    and fact_id not in fact_ids_replaced_by_same_development
                ):
                    raise ValueError(
                        f"fact {planned['key']} supersedes stale or unknown fact {fact_id}"
                    )

            sidecar_source_fact_ids = inherited_source_fact_ids_by_key.get(fact_key, [])
            if not isinstance(sidecar_source_fact_ids, list) or any(
                isinstance(fact_id, bool) or not isinstance(fact_id, int)
                for fact_id in sidecar_source_fact_ids
            ):
                raise ValueError(
                    f"fact {fact_key} has invalid inherited source fact IDs"
                )
            inherited_fact_ids = list(
                dict.fromkeys([*supersedes, *sidecar_source_fact_ids])
            )
            for fact_id in sidecar_source_fact_ids:
                if fact_id not in facts_by_id:
                    raise ValueError(
                        f"fact {fact_key} inherits evidence from source fact "
                        f"{fact_id} that is missing or has not been materialized yet"
                    )

            inherited_source_session_ids: list[str] = []
            for fact_id in inherited_fact_ids:
                inherited_source_session_ids.extend(source_sessions_for(fact_id))
            for source_session_id in inherited_source_session_ids:
                if (
                    source_session_order
                    and source_session_id not in available_source_session_ids
                ):
                    raise ValueError(
                        "replacement inherits source session that is not in accepted "
                        f"history or an earlier materialized session: {source_session_id}"
                    )

            evidence_contact_ids = planned.get("evidence_contact_ids") or []
            if (
                not isinstance(evidence_contact_ids, list)
                or not evidence_contact_ids
                or not all(nonempty_string(value) for value in evidence_contact_ids)
                or len(evidence_contact_ids) != len(set(evidence_contact_ids))
            ):
                raise ValueError(
                    f"fact {planned['key']} must cite unique non-empty evidence contacts"
                )
            future_evidence = [
                str(value)
                for value in evidence_contact_ids
                if str(value) not in contact_order
                or contact_order[str(value)] > contact_index
            ]
            if future_evidence:
                raise ValueError(
                    f"fact {planned['key']} cites future or unknown evidence contacts: "
                    f"{future_evidence}"
                )
            try:
                new_source_session_ids = [
                    session_by_contact[str(evidence_contact_id)]
                    for evidence_contact_id in sorted(
                        evidence_contact_ids,
                        key=lambda value: contact_order[str(value)],
                    )
                ]
            except KeyError as error:
                raise ValueError(
                    f"fact {planned['key']} cites unknown evidence contact {error.args[0]}"
                ) from error
            raw_source_session_ids = [
                *inherited_source_session_ids,
                *new_source_session_ids,
            ]
            unique_source_session_ids = list(dict.fromkeys(raw_source_session_ids))
            try:
                source_session_ids = sorted(
                    unique_source_session_ids,
                    key=lambda value: source_session_order[value],
                )
            except KeyError as error:
                raise ValueError(
                    "replacement source session is not in accepted history or the "
                    f"current window: {error.args[0]}"
                ) from error
            if len(source_session_ids) != len(set(source_session_ids)):
                raise ValueError(f"fact {planned['key']} has duplicate source sessions")

            fact = {
                "id": key_to_id[fact_key],
                "statement": str(planned["statement"]),
                "applies_when": str(planned["applies_when"]),
                "source_session_ids": source_session_ids,
                "subjects": list(planned["subjects"]),
                "supersedes": supersedes,
                "construction_key": fact_key,
            }
            result.append(fact)
            added.append(fact)
            facts_by_id[int(fact["id"])] = fact
            facts_superseded_in_contact.update(supersedes)
            fact_ids_added_in_contact.add(int(fact["id"]))
        current_fact_ids.difference_update(facts_superseded_in_contact)
        current_fact_ids.update(fact_ids_added_in_contact)
        for development_id in contact_development_ids:
            superseded_fact_ids_by_development.setdefault(
                development_id, set()
            ).update(facts_superseded_in_contact)
    return result, added, key_to_id
