"""Generate one quarter's dated events from the accepted long-range story plan."""

from __future__ import annotations

import argparse
import copy
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from construction.construction_io import file_hash, resolve_path
from construction.checkpoints import load_checkpoint, validated_checkpoint_identity
from construction.build_history_window import (
    current_ordinary_states_from_session_purposes,
)
from construction.runtime_inputs import current_fact_ids
from construction.llm import AzureJsonClient
from construction.long_history_prompts import (
    QUARTER_EVENT_EVENT_REPAIR_SYSTEM,
    QUARTER_EVENT_FACT_FINALIZATION_SYSTEM,
    QUARTER_EVENT_FACT_REPAIR_SYSTEM,
    QUARTER_EVENT_QUALITY_REVIEW_SYSTEM,
    QUARTER_EVENT_STORY_SYSTEM,
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def dump_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def current_starting_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = current_fact_ids(facts)
    return [dict(fact) for fact in facts if int(fact["id"]) in ids]


def primary_subject_ids_for_persona(
    persona: str, entities: list[dict[str, Any]]
) -> set[str]:
    """Return the explicitly invoked persona ID after checking the checkpoint."""
    primary_subject_ids = {str(persona)}
    known_entity_ids = {
        str(entity.get("id"))
        for entity in entities
        if isinstance(entity, dict) and isinstance(entity.get("id"), str)
    }
    if not primary_subject_ids <= known_entity_ids:
        raise ValueError(
            "starting checkpoint is missing the primary subject IDs for the persona: "
            + ", ".join(sorted(primary_subject_ids - known_entity_ids))
        )
    return primary_subject_ids


STORY_EVENT_FIELDS = {
    "id",
    "start_date",
    "end_date",
    "subjects",
    "builds_on_event_ids",
    "what_happens",
    "lasting_outcome",
}
NEW_ENTITY_FIELDS = {
    "id",
    "name",
    "kind",
    "role",
    "introduced_in_event_id",
}
PROTECTED_CHANGE_FIELDS = {"id", "subject_ids", "not_before", "description"}
CANONICAL_PLAN_FIELDS = {
    "period_start",
    "period_end",
    "protected_changes",
    "new_entities",
    "events",
}
ANNOTATION_FACT_FIELDS = {"key", "statement", "applies_when", "subjects"}
FACT_REPLACEMENT_FIELDS = {
    "event_id",
    "replaced_fact",
    "replacement_fact_numbers",
    "still_current_information",
    "changed_or_ended_information",
}


def canonical_plan_for_context(document: dict[str, Any]) -> dict[str, Any]:
    """Expose only active quarter-plan fields to the next planner."""
    source = document.get("plan") if isinstance(document.get("plan"), dict) else document
    if not isinstance(source, dict):
        return {}
    return {
        "period_start": source.get("period_start"),
        "period_end": source.get("period_end"),
        "protected_changes": copy.deepcopy(source.get("protected_changes") or []),
        "new_entities": copy.deepcopy(source.get("new_entities") or []),
        "events": copy.deepcopy(source.get("events") or []),
    }


def event_repair_entities(
    starting_entities: list[dict[str, Any]], canonical_plan: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return complete entity records available to an event repair."""
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    sources = [
        starting_entities,
        (canonical_plan.get("plan") or {}).get("new_entities") or [],
    ]
    for source in sources:
        for entity in source:
            if not isinstance(entity, dict) or not isinstance(entity.get("id"), str):
                continue
            entity_id = entity["id"]
            if entity_id in seen_ids:
                continue
            seen_ids.add(entity_id)
            records.append(copy.deepcopy(entity))
    return records


def normalize_candidate_plan(document: dict[str, Any]) -> dict[str, Any]:
    """Normalize direct and legacy wrapped candidates for internal consumers."""
    if not isinstance(document, dict):
        raise ValueError("candidate quarter plan must be an object")
    if set(document) == {"plan"} and isinstance(document.get("plan"), dict):
        plan = document["plan"]
    elif "plan" in document:
        raise ValueError("candidate quarter plan must contain exactly the canonical plan fields")
    else:
        plan = document
    if set(plan) != CANONICAL_PLAN_FIELDS:
        raise ValueError(
            "candidate quarter plan must contain exactly "
            f"{sorted(CANONICAL_PLAN_FIELDS)}"
        )
    return {"plan": copy.deepcopy(plan)}


def _story_from_canonical_plan(canonical_plan: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the story response without fact annotations."""
    plan = canonical_plan["plan"]
    return {
        "story": {
            "period_start": plan["period_start"],
            "period_end": plan["period_end"],
            "new_entities": copy.deepcopy(plan["new_entities"]),
            "protected_changes": copy.deepcopy(plan["protected_changes"]),
            "events": [
                {
                    field: copy.deepcopy(event[field])
                    for field in STORY_EVENT_FIELDS
                    if field in event
                }
                for event in plan["events"]
            ],
        }
    }


def _fact_response_from_canonical_plan(
    canonical_plan: dict[str, Any],
    replacement_accounting: list[dict[str, Any]],
) -> dict[str, Any]:
    """Rebuild the fact-finalization response from the canonical plan."""
    plan = canonical_plan["plan"]
    events = plan["events"]
    event_by_id = {str(event["id"]): event for event in events}
    annotations: list[dict[str, Any]] = []
    for event in events:
        annotations.append(
            {
                "event_id": event["id"],
                "relevant_existing_fact_ids": copy.deepcopy(
                    event.get("relevant_existing_fact_ids") or []
                ),
                "facts_to_establish": [
                    {
                        field: copy.deepcopy(fact[field])
                        for field in ANNOTATION_FACT_FIELDS
                        if field in fact
                    }
                    for fact in event.get("facts_to_establish") or []
                ],
            }
        )

    fact_numbers_by_event_and_key = {
        (str(event["id"]), str(fact["key"])): index
        for event in events
        for index, fact in enumerate(event.get("facts_to_establish") or [], start=1)
    }
    fact_replacements: list[dict[str, Any]] = []
    for index, row in enumerate(replacement_accounting):
        event_id = str(row.get("event_id"))
        if event_id not in event_by_id:
            raise ValueError(
                f"replacement accounting row {index} names unknown event {event_id}"
            )
        replaced_ref = str(row.get("replaced_fact_ref") or "")
        if replaced_ref.startswith("id:"):
            try:
                replaced_fact: dict[str, Any] = {
                    "source": "starting_checkpoint",
                    "fact_id": int(replaced_ref[3:]),
                }
            except ValueError as exc:
                raise ValueError(
                    f"replacement accounting row {index} has invalid fact ID"
                ) from exc
        elif replaced_ref.startswith("key:"):
            source_key = replaced_ref[4:]
            source_matches = [
                (str(event["id"]), number)
                for event in events
                for number, fact in enumerate(
                    event.get("facts_to_establish") or [], start=1
                )
                if str(fact.get("key")) == source_key
            ]
            if len(source_matches) != 1:
                raise ValueError(
                    f"replacement accounting row {index} cannot resolve {replaced_ref}"
                )
            source_event_id, source_fact_number = source_matches[0]
            replaced_fact = {
                "source": "this_quarter",
                "event_id": source_event_id,
                "fact_number": source_fact_number,
            }
        else:
            raise ValueError(
                f"replacement accounting row {index} must use id: or key:"
            )

        replacement_numbers: list[int] = []
        for key in row.get("replacement_fact_keys") or []:
            lookup = (event_id, str(key))
            if lookup not in fact_numbers_by_event_and_key:
                raise ValueError(
                    f"replacement accounting row {index} cannot resolve replacement fact {key}"
                )
            replacement_numbers.append(fact_numbers_by_event_and_key[lookup])
        fact_replacements.append(
            {
                "event_id": event_id,
                "replaced_fact": replaced_fact,
                "replacement_fact_numbers": replacement_numbers,
                "still_current_information": copy.deepcopy(
                    row.get("still_current_information") or []
                ),
                "changed_or_ended_information": copy.deepcopy(
                    row.get("changed_or_ended_information") or []
                ),
            }
        )
    return {
        "event_fact_annotations": annotations,
        "fact_replacements": fact_replacements,
    }


def _current_accounting_for_plan(
    replacement_accounting: list[dict[str, Any]],
    canonical_plan: dict[str, Any],
) -> list[dict[str, Any]]:
    """Keep accounting text that still matches the saved candidate facts."""
    plan = canonical_plan["plan"]
    statements = {
        (str(event["id"]), str(fact["key"])): str(fact.get("statement") or "")
        for event in plan.get("events") or []
        for fact in event.get("facts_to_establish") or []
    }
    result: list[dict[str, Any]] = []
    for index, row in enumerate(replacement_accounting):
        updated = copy.deepcopy(row)
        event_id = str(updated.get("event_id"))
        keys = [str(key) for key in updated.get("replacement_fact_keys") or []]
        named_statements = [
            statements[(event_id, key)]
            for key in keys
            if (event_id, key) in statements and statements[(event_id, key)].strip()
        ]
        if not isinstance(updated.get("still_current_information"), list):
            raise ValueError(
                f"replacement accounting row {index} still_current_information must be a list"
            )
        normalized_named = " ".join(" ".join(named_statements).split())
        retained = [
            value
            for value in updated["still_current_information"]
            if isinstance(value, str)
            and " ".join(value.split()) in normalized_named
        ]
        if updated["still_current_information"] and not retained:
            retained = named_statements
        updated["still_current_information"] = retained
        result.append(updated)
    return result


def reconstruct_consolidated_candidate(
    candidate_document: dict[str, Any],
    full_quality_review_request: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Rebuild missing current responses from an older failed candidate.

    The saved candidate plan remains authoritative. The review request supplies
    replacement-accounting explanations, which are reconciled to the saved
    candidate facts before the response is rebuilt.
    """
    canonical_plan = normalize_candidate_plan(candidate_document)
    proposed = full_quality_review_request.get("proposed_plan")
    if not isinstance(proposed, dict):
        raise ValueError("full quality review request is missing proposed_plan")
    candidate_plan = canonical_plan["plan"]
    proposed_events = proposed.get("events")
    if not isinstance(proposed_events, list) or [
        str(event.get("id")) for event in proposed_events if isinstance(event, dict)
    ] != [str(event["id"]) for event in candidate_plan["events"]]:
        raise ValueError(
            "full quality review request does not cover every saved candidate event"
        )
    accounting = full_quality_review_request.get("replacement_accounting")
    if not isinstance(accounting, list):
        raise ValueError("full quality review request is missing replacement_accounting")
    accounting = _current_accounting_for_plan(accounting, canonical_plan)
    story_response = _story_from_canonical_plan(canonical_plan)
    fact_response = _fact_response_from_canonical_plan(canonical_plan, accounting)
    rebuilt_plan, errors = build_canonical_plan(story_response, fact_response)
    if errors or rebuilt_plan != canonical_plan:
        raise ValueError(
            "reconstructed responses do not reproduce candidate_quarter_plan.json: "
            + "; ".join(errors)
        )
    accounting_errors = validate_replacement_accounting(
        accounting, canonical_plan=canonical_plan
    )
    if accounting_errors:
        raise ValueError(
            "reconstructed replacement accounting is invalid: "
            + "; ".join(accounting_errors)
        )
    return story_response, fact_response, accounting, canonical_plan


def load_candidate_state_for_correction(
    candidate_dir: Path,
    candidate_document: dict[str, Any],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any],
    list[Path],
]:
    """Load the saved current candidate, falling back for older runs."""
    names = (
        "consolidated_story_response.json",
        "consolidated_fact_finalization_response.json",
        "candidate_replacement_accounting.json",
    )
    paths = [candidate_dir / name for name in names]
    existing = [path.is_file() for path in paths]
    if any(existing):
        if not all(existing):
            raise ValueError("failed candidate has an incomplete consolidated state")
        story_response = normalize_story_event_order(load_json(paths[0]))
        fact_response = load_json(paths[1])
        accounting_document = load_json(paths[2])
        accounting = accounting_document.get("replacement_accounting")
        if not isinstance(accounting, list):
            raise ValueError(
                "candidate_replacement_accounting.json is missing replacement_accounting"
            )
        canonical_plan, errors = build_canonical_plan(story_response, fact_response)
        expected_plan = normalize_candidate_plan(candidate_document)
        if errors or canonical_plan != expected_plan:
            raise ValueError(
                "saved consolidated responses do not reproduce candidate_quarter_plan.json: "
                + "; ".join(errors)
            )
        accounting_errors = validate_replacement_accounting(
            accounting, canonical_plan=canonical_plan
        )
        if accounting_errors:
            raise ValueError(
                "saved consolidated replacement accounting is invalid: "
                + "; ".join(accounting_errors)
            )
        return story_response, fact_response, accounting, canonical_plan, paths

    raw_story_path = candidate_dir / "story_response.json"
    raw_fact_path = candidate_dir / "fact_finalization_response.json"
    if raw_story_path.is_file() and raw_fact_path.is_file():
        story_response = normalize_story_event_order(load_json(raw_story_path))
        fact_response = load_json(raw_fact_path)
        canonical_plan, errors = build_canonical_plan(story_response, fact_response)
        expected_plan = normalize_candidate_plan(candidate_document)
        if errors or canonical_plan != expected_plan:
            raise ValueError(
                "saved story and fact responses do not reproduce "
                "candidate_quarter_plan.json: " + "; ".join(errors)
            )
        accounting = derive_replacement_accounting(fact_response, canonical_plan)
        accounting_errors = validate_replacement_accounting(
            accounting, canonical_plan=canonical_plan
        )
        if accounting_errors:
            raise ValueError(
                "saved replacement accounting is invalid: "
                + "; ".join(accounting_errors)
            )
        return (
            story_response,
            fact_response,
            accounting,
            canonical_plan,
            [raw_story_path, raw_fact_path],
        )

    canonical_plan = normalize_candidate_plan(candidate_document)
    review_request_path, review_request = load_latest_full_quality_review_request(
        candidate_dir, canonical_plan
    )
    story_response, fact_response, accounting, canonical_plan = (
        reconstruct_consolidated_candidate(candidate_document, review_request)
    )
    return (
        story_response,
        fact_response,
        accounting,
        canonical_plan,
        [review_request_path],
    )


def load_latest_full_quality_review_request(
    candidate_dir: Path, canonical_plan: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    """Find the newest saved review request that covers the complete plan."""
    plan = canonical_plan["plan"]
    expected_ids = [str(event["id"]) for event in plan.get("events") or []]
    candidates: list[tuple[tuple[int, int, str], Path, dict[str, Any]]] = []
    for path in candidate_dir.rglob("quality_review_request.json"):
        document = load_json(path)
        if not isinstance(document, dict):
            continue
        if "earlier_events_used_by_the_events_being_reviewed" in document:
            continue
        proposed = document.get("proposed_plan")
        if not isinstance(proposed, dict) or not isinstance(proposed.get("events"), list):
            continue
        ids = [
            str(event.get("id"))
            for event in proposed["events"]
            if isinstance(event, dict)
        ]
        if ids != expected_ids or not isinstance(
            document.get("replacement_accounting"), list
        ):
            continue
        proposed_plan = {
            "new_entities": proposed.get("new_entities") or [],
            "events": proposed.get("events") or [],
        }
        saved_plan = {
            "new_entities": plan.get("new_entities") or [],
            "events": plan.get("events") or [],
        }
        exact_plan = int(proposed_plan == saved_plan)
        score = (exact_plan, len(path.relative_to(candidate_dir).parts), str(path))
        candidates.append((score, path, document))
    if not candidates:
        raise ValueError(
            "failed candidate has no full quality_review_request.json covering its saved plan"
        )
    _, path, document = max(candidates, key=lambda item: item[0])
    return path, document


def failed_fact_only_event_ids(review: dict[str, Any]) -> set[str]:
    """Return events whose fact or replacement rows failed, excluding event-only failures."""
    return {
        str(row["event_id"])
        for field in ("fact_reviews", "replacement_reviews")
        for row in review.get(field) or []
        if isinstance(row, dict) and row.get("passed") is False
    }


def deterministic_fact_failure_review(
    validation_errors: list[Any], canonical_plan: dict[str, Any]
) -> dict[str, Any] | None:
    """Represent pre-review fact validation errors for the existing repair step."""
    events = (canonical_plan.get("plan") or {}).get("events") or []
    event_ids = {str(event["id"]) for event in events}
    reasons_by_event: dict[str, list[str]] = {}
    for value in validation_errors:
        if not isinstance(value, str) or ":" not in value:
            return None
        event_id = value.split(":", 1)[0].strip()
        if event_id not in event_ids:
            return None
        reasons_by_event.setdefault(event_id, []).append(value)
    if not reasons_by_event:
        return None
    if any(
        not event.get("facts_to_establish")
        for event in events
        if str(event["id"]) in reasons_by_event
    ):
        return None

    review = {
        "passed": False,
        "plan_review": {"passed": True, "reason": ""},
        "event_reviews": [
            {"event_id": str(event["id"]), "passed": True, "reason": ""}
            for event in events
        ],
        "fact_reviews": [],
        "replacement_reviews": [],
    }
    for event in events:
        event_id = str(event["id"])
        reason = "; ".join(reasons_by_event.get(event_id, []))
        for fact in event.get("facts_to_establish") or []:
            review["fact_reviews"].append(
                {
                    "event_id": event_id,
                    "fact_key": str(fact["key"]),
                    "passed": not bool(reason),
                    "reason": reason,
                }
            )
            for fact_id in fact.get("supersedes") or []:
                review["replacement_reviews"].append(
                    {
                        "event_id": event_id,
                        "fact_key": str(fact["key"]),
                        "replaced_fact_ref": f"id:{int(fact_id)}",
                        "passed": not bool(reason),
                        "reason": reason,
                    }
                )
            for fact_key in fact.get("supersedes_fact_keys") or []:
                review["replacement_reviews"].append(
                    {
                        "event_id": event_id,
                        "fact_key": str(fact["key"]),
                        "replaced_fact_ref": f"key:{fact_key}",
                        "passed": not bool(reason),
                        "reason": reason,
                    }
                )
    return review


def expand_fact_repair_scope(
    *,
    failed_event_ids: set[str],
    story_response: dict[str, Any],
    final_response: dict[str, Any],
) -> set[str]:
    """Include later events whose replacement rows point at repaired facts."""
    story_events = (story_response.get("story") or {}).get("events") or []
    event_positions = {
        str(event.get("id")): index
        for index, event in enumerate(story_events)
        if isinstance(event, dict) and isinstance(event.get("id"), str)
    }
    scope = set(failed_event_ids).intersection(event_positions)
    changed = True
    while changed:
        changed = False
        for row in final_response.get("fact_replacements") or []:
            if not isinstance(row, dict):
                continue
            event_id = str(row.get("event_id") or "")
            replaced_fact = row.get("replaced_fact")
            if (
                event_id not in event_positions
                or not isinstance(replaced_fact, dict)
                or replaced_fact.get("source") != "this_quarter"
            ):
                continue
            source_event_id = str(replaced_fact.get("event_id") or "")
            if (
                source_event_id in scope
                and event_positions[source_event_id] < event_positions[event_id]
                and event_id not in scope
            ):
                scope.add(event_id)
                changed = True
    return scope


def write_consolidated_candidate_state(
    output_dir: Path,
    *,
    story_response: dict[str, Any],
    fact_finalization_response: dict[str, Any],
    replacement_accounting: list[dict[str, Any]],
    canonical_plan: dict[str, Any],
    quality_review: dict[str, Any],
) -> dict[str, str]:
    """Save the current complete plan state separately from raw call records."""
    files = {
        "consolidated_story_response.json": story_response,
        "consolidated_fact_finalization_response.json": fact_finalization_response,
        "candidate_replacement_accounting.json": {
            "replacement_accounting": replacement_accounting
        },
        "candidate_quarter_plan.json": canonical_plan["plan"],
        "consolidated_quality_review_response.json": quality_review,
    }
    for name, value in files.items():
        dump_json(output_dir / name, value)
    return {name: file_hash(output_dir / name) for name in files}


def story_as_empty_fact_plan(story_response: dict[str, Any]) -> dict[str, Any]:
    story = story_response.get("story") or {}
    events = story.get("events")
    if not isinstance(events, list):
        events = []
    return {
        "plan": {
            "period_start": story.get("period_start"),
            "period_end": story.get("period_end"),
            "new_entities": story.get("new_entities"),
            "protected_changes": copy.deepcopy(story.get("protected_changes") or []),
            "events": [
                {
                    **event,
                    "relevant_existing_fact_ids": [],
                    "facts_to_establish": [],
                }
                for event in events
                if isinstance(event, dict)
            ],
        }
    }


def normalize_story_event_order(response: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with valid story events in the required date order."""
    normalized = copy.deepcopy(response)
    story = normalized.get("story")
    if not isinstance(story, dict):
        return normalized
    events = story.get("events")
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        return normalized
    try:
        for event in events:
            date.fromisoformat(str(event["start_date"]))
            date.fromisoformat(str(event["end_date"]))
    except (KeyError, ValueError):
        return normalized
    story["events"] = sorted(
        events,
        key=lambda event: (str(event["end_date"]), str(event["start_date"])),
    )
    return normalized


def validate_story(
    response: dict[str, Any],
    *,
    period_start: date,
    period_end: date,
    first_event_date: date | None = None,
    starting_entities: list[dict[str, Any]],
    starting_facts: list[dict[str, Any]],
    overview: dict[str, Any],
    previous_plan: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    first_event_date = first_event_date or period_start
    response_fields = {key for key in response if not key.startswith("_")}
    if response_fields != {"story"}:
        errors.append("story response must contain exactly story")
    story = response.get("story")
    if not isinstance(story, dict):
        errors.append("story response must contain a story object")
        return errors
    expected_story_fields = {
        "period_start", "period_end", "new_entities", "events", "protected_changes"
    }
    if set(story) != expected_story_fields:
        errors.append(
            f"story must contain exactly {sorted(expected_story_fields)}"
        )
        return errors
    events = story.get("events")
    if not isinstance(events, list):
        errors.append("story events must be a list")
        return errors
    for event_index, event in enumerate(events):
        if not isinstance(event, dict) or set(event) != STORY_EVENT_FIELDS:
            errors.append(
                f"story events[{event_index}] must contain exactly "
                f"{sorted(STORY_EVENT_FIELDS)}"
            )
            continue
        try:
            event_start = date.fromisoformat(str(event["start_date"]))
            event_end = date.fromisoformat(str(event["end_date"]))
        except ValueError:
            errors.append(f"story events[{event_index}] has invalid dates")
            continue
        if (
            event_start < first_event_date
            or event_end > period_end
            or event_end < event_start
        ):
            errors.append(
                f"story events[{event_index}] dates must fall between "
                f"{first_event_date.isoformat()} and {period_end.isoformat()}"
            )
    protected_changes = story.get("protected_changes", [])
    if not isinstance(protected_changes, list):
        errors.append("story protected_changes must be a list")
    else:
        for index, change in enumerate(protected_changes):
            if not isinstance(change, dict) or set(change) != PROTECTED_CHANGE_FIELDS:
                errors.append(
                    f"story protected_changes[{index}] must contain exactly "
                    f"{sorted(PROTECTED_CHANGE_FIELDS)}"
                )
    if errors:
        return errors
    return validate_plan(
        story_as_empty_fact_plan(response),
        period_start=period_start,
        period_end=period_end,
        first_event_date=first_event_date,
        starting_entities=starting_entities,
        starting_facts=starting_facts,
        overview=overview,
        previous_plan=previous_plan,
    )


def validate_event_fact_annotations(
    response: dict[str, Any], story_response: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    response_fields = {key for key in response if not key.startswith("_")}
    expected_response_fields = {"event_fact_annotations", "fact_replacements"}
    if response_fields != expected_response_fields:
        errors.append(
            "fact finalization response must contain exactly "
            f"{sorted(expected_response_fields)}"
        )

    story = story_response.get("story")
    story_events = story.get("events") if isinstance(story, dict) else None
    if not isinstance(story_events, list):
        return errors + ["fixed story events must be a list"]
    known_event_ids = {
        str(event.get("id"))
        for event in story_events
        if isinstance(event, dict) and isinstance(event.get("id"), str)
    }
    annotations = response.get("event_fact_annotations")
    if not isinstance(annotations, list):
        return errors + ["event_fact_annotations must be a list"]

    seen: set[str] = set()
    expected_row_fields = {
        "event_id",
        "relevant_existing_fact_ids",
        "facts_to_establish",
    }
    for index, row in enumerate(annotations):
        if not isinstance(row, dict) or set(row) != expected_row_fields:
            errors.append(
                f"event_fact_annotations[{index}] must contain exactly "
                f"{sorted(expected_row_fields)}"
            )
            continue
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            errors.append(f"event_fact_annotations[{index}] event_id must be a nonempty string")
            continue
        if event_id not in known_event_ids:
            errors.append(f"event_fact_annotations[{index}] names unknown event {event_id}")
        if event_id in seen:
            errors.append(f"event_fact_annotations contains duplicate event {event_id}")
        seen.add(event_id)
        fact_ids = row.get("relevant_existing_fact_ids")
        if not isinstance(fact_ids, list):
            errors.append(
                f"event_fact_annotations[{index}] relevant_existing_fact_ids must be a list"
            )
        elif any(isinstance(value, bool) or not isinstance(value, int) for value in fact_ids):
            errors.append(
                f"event_fact_annotations[{index}] relevant_existing_fact_ids must contain integer IDs"
            )
        facts = row.get("facts_to_establish")
        if not isinstance(facts, list):
            errors.append(
                f"event_fact_annotations[{index}] facts_to_establish must be a list"
            )
            continue
        for fact_index, fact in enumerate(facts):
            if not isinstance(fact, dict) or set(fact) != ANNOTATION_FACT_FIELDS:
                errors.append(
                    f"event_fact_annotations[{index}] facts_to_establish[{fact_index}] "
                    f"must contain exactly {sorted(ANNOTATION_FACT_FIELDS)}"
                )
    return errors


def validate_fact_replacements(
    response: dict[str, Any], story_response: dict[str, Any]
) -> list[str]:
    """Validate model-authored replacement references before deriving plan fields."""
    errors: list[str] = []
    rows = response.get("fact_replacements")
    if not isinstance(rows, list):
        return ["fact_replacements must be a list"]
    story = story_response.get("story")
    story_events = story.get("events") if isinstance(story, dict) else None
    if not isinstance(story_events, list):
        return ["fixed story events must be a list"]
    event_positions = {
        str(event.get("id")): index
        for index, event in enumerate(story_events)
        if isinstance(event, dict) and isinstance(event.get("id"), str)
    }
    facts_by_event = {
        str(row.get("event_id")): row.get("facts_to_establish")
        for row in response.get("event_fact_annotations") or []
        if isinstance(row, dict) and isinstance(row.get("event_id"), str)
    }
    seen_rows: set[tuple[str, str]] = set()
    seen_old_facts: set[str] = set()
    for index, row in enumerate(rows):
        prefix = f"fact_replacements[{index}]"
        if not isinstance(row, dict) or set(row) != FACT_REPLACEMENT_FIELDS:
            errors.append(f"{prefix} must contain exactly {sorted(FACT_REPLACEMENT_FIELDS)}")
            continue
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or event_id not in event_positions:
            errors.append(f"{prefix} event_id must name a fixed story event")
            continue
        replacement_facts = facts_by_event.get(event_id)
        if not isinstance(replacement_facts, list):
            errors.append(f"{prefix} event_id must have an annotation row")
            replacement_facts = []

        replaced_fact = row.get("replaced_fact")
        reference: str | None = None
        if not isinstance(replaced_fact, dict):
            errors.append(f"{prefix} replaced_fact must be an object")
        elif replaced_fact.get("source") == "starting_checkpoint":
            if set(replaced_fact) != {"source", "fact_id"}:
                errors.append(f"{prefix} starting_checkpoint reference has invalid shape")
            elif isinstance(replaced_fact.get("fact_id"), bool) or not isinstance(
                replaced_fact.get("fact_id"), int
            ) or int(replaced_fact["fact_id"]) <= 0:
                errors.append(f"{prefix} starting_checkpoint fact_id must be a positive integer")
            else:
                reference = f"id:{replaced_fact['fact_id']}"
        elif replaced_fact.get("source") == "this_quarter":
            if set(replaced_fact) != {"source", "event_id", "fact_number"}:
                errors.append(f"{prefix} this_quarter reference has invalid shape")
            else:
                source_event_id = replaced_fact.get("event_id")
                fact_number = replaced_fact.get("fact_number")
                if not isinstance(source_event_id, str) or source_event_id not in event_positions:
                    errors.append(f"{prefix} this_quarter event_id must name a fixed story event")
                elif event_positions[source_event_id] >= event_positions[event_id]:
                    errors.append(f"{prefix} this_quarter event_id must be earlier than {event_id}")
                elif isinstance(fact_number, bool) or not isinstance(fact_number, int) or fact_number < 1:
                    errors.append(f"{prefix} this_quarter fact_number must be a 1-based integer")
                else:
                    source_facts = facts_by_event.get(source_event_id)
                    if not isinstance(source_facts, list) or fact_number > len(source_facts):
                        errors.append(f"{prefix} this_quarter fact_number is out of range")
                    else:
                        source_fact = source_facts[fact_number - 1]
                        key = source_fact.get("key") if isinstance(source_fact, dict) else None
                        if not isinstance(key, str) or not key:
                            errors.append(f"{prefix} this_quarter source fact must have a nonempty key")
                        else:
                            reference = f"key:{key}"
        else:
            errors.append(f"{prefix} replaced_fact source must be starting_checkpoint or this_quarter")

        replacement_numbers = row.get("replacement_fact_numbers")
        if not isinstance(replacement_numbers, list) or not replacement_numbers:
            errors.append(f"{prefix} replacement_fact_numbers must be a nonempty list")
        elif any(
            isinstance(number, bool) or not isinstance(number, int) or number < 1
            for number in replacement_numbers
        ):
            errors.append(f"{prefix} replacement_fact_numbers must contain 1-based integers")
        else:
            if len(replacement_numbers) != len(set(replacement_numbers)):
                errors.append(f"{prefix} replacement_fact_numbers must not contain duplicates")
            if any(number > len(replacement_facts) for number in replacement_numbers):
                errors.append(f"{prefix} replacement_fact_numbers must belong to event {event_id}")

        for field, required in (
            ("still_current_information", False),
            ("changed_or_ended_information", True),
        ):
            value = row.get(field)
            if not isinstance(value, list) or (required and not value):
                errors.append(f"{prefix} {field} must be {'a nonempty ' if required else 'a '}list")
            elif any(not isinstance(item, str) or not item.strip() for item in value):
                errors.append(f"{prefix} {field} must contain nonempty strings")

        if reference is not None:
            row_key = (event_id, reference)
            if row_key in seen_rows:
                errors.append(f"{prefix} duplicates a replacement row for {reference}")
            if reference in seen_old_facts:
                errors.append(f"{prefix} duplicates replacement of {reference}")
            seen_rows.add(row_key)
            seen_old_facts.add(reference)
    return errors


def build_canonical_plan(
    story_response: dict[str, Any], annotation_response: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[str]]:
    """Copy event structure from the story and attach validated annotations."""
    errors = validate_event_fact_annotations(annotation_response, story_response)
    errors.extend(validate_fact_replacements(annotation_response, story_response))
    story = story_response.get("story")
    if not isinstance(story, dict) or not isinstance(story.get("events"), list):
        return None, errors
    if errors:
        return None, errors
    annotations = {
        str(row["event_id"]): row
        for row in annotation_response["event_fact_annotations"]
    }
    events = []
    event_by_id: dict[str, dict[str, Any]] = {}
    for story_event in story["events"]:
        event = dict(story_event)
        annotation = annotations.get(str(story_event["id"]), {})
        event["relevant_existing_fact_ids"] = list(
            annotation.get("relevant_existing_fact_ids") or []
        )
        event["facts_to_establish"] = [
            {
                **fact,
                "supersedes": [],
                "supersedes_fact_keys": [],
            }
            for fact in annotation.get("facts_to_establish") or []
        ]
        events.append(event)
        event_by_id[str(event["id"])] = event
    for row in annotation_response["fact_replacements"]:
        event = event_by_id[str(row["event_id"])]
        targets = [
            event["facts_to_establish"][number - 1]
            for number in row["replacement_fact_numbers"]
        ]
        replaced_fact = row["replaced_fact"]
        if replaced_fact["source"] == "starting_checkpoint":
            for fact in targets:
                fact["supersedes"].append(replaced_fact["fact_id"])
        else:
            source_event = event_by_id[replaced_fact["event_id"]]
            source_fact = source_event["facts_to_establish"][replaced_fact["fact_number"] - 1]
            for fact in targets:
                fact["supersedes_fact_keys"].append(source_fact["key"])
    return {
        "plan": {
            "period_start": story["period_start"],
            "period_end": story["period_end"],
            "new_entities": story["new_entities"],
            "protected_changes": copy.deepcopy(story.get("protected_changes") or []),
            "events": events,
        }
    }, []


def derive_replacement_accounting(
    response: dict[str, Any], canonical_plan: dict[str, Any]
) -> list[dict[str, Any]]:
    """Create the legacy review artifact from the one authoritative model relation."""
    plan = canonical_plan["plan"]
    events = {str(event["id"]): event for event in plan["events"]}
    rows: list[dict[str, Any]] = []
    for replacement in response["fact_replacements"]:
        event = events[str(replacement["event_id"])]
        targets = [
            event["facts_to_establish"][number - 1]["key"]
            for number in replacement["replacement_fact_numbers"]
        ]
        old_fact = replacement["replaced_fact"]
        if old_fact["source"] == "starting_checkpoint":
            reference = f"id:{old_fact['fact_id']}"
        else:
            source_event = events[old_fact["event_id"]]
            reference = f"key:{source_event['facts_to_establish'][old_fact['fact_number'] - 1]['key']}"
        rows.append(
            {
                "event_id": replacement["event_id"],
                "replaced_fact_ref": reference,
                "replacement_fact_keys": targets,
                "still_current_information": list(replacement["still_current_information"]),
                "changed_or_ended_information": list(replacement["changed_or_ended_information"]),
            }
        )
    return rows


def validate_replacement_accounting(
    replacement_accounting: list[dict[str, Any]],
    *,
    canonical_plan: dict[str, Any],
) -> list[str]:
    """Validate replacement-accounting structure without judging semantics."""
    errors: list[str] = []
    plan = canonical_plan
    if isinstance(plan, dict) and isinstance(plan.get("plan"), dict):
        plan = plan["plan"]
    if not isinstance(plan, dict):
        errors.append("canonical plan must be an object")
        return errors
    rows = replacement_accounting

    # ``expected`` records the facts that explicitly retire an older fact.  An
    # accounting row may also name other facts from that same event when the
    # replacement is deliberately split across several facts.
    expected: dict[tuple[str, str], set[str]] = {}
    event_fact_keys: dict[str, set[str]] = {}
    event_fact_statements: dict[str, dict[str, str]] = {}
    events = plan.get("events")
    if not isinstance(events, list):
        return errors
    for event_index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id:
            continue
        facts = event.get("facts_to_establish")
        if not isinstance(facts, list):
            continue
        for fact_index, fact in enumerate(facts):
            if not isinstance(fact, dict):
                continue
            fact_key = fact.get("key")
            if not isinstance(fact_key, str) or not fact_key:
                continue
            event_fact_keys.setdefault(event_id, set()).add(fact_key)
            statement = fact.get("statement")
            if isinstance(statement, str):
                event_fact_statements.setdefault(event_id, {})[fact_key] = statement
            references = (
                ("supersedes", "id"),
                ("supersedes_fact_keys", "key"),
            )
            for field, prefix in references:
                values = fact.get(field)
                if not isinstance(values, list):
                    errors.append(
                        f"{event_id}/{fact_key}: {field} must be a list for "
                        "replacement accounting"
                    )
                    continue
                for reference in values:
                    if prefix == "id":
                        if isinstance(reference, bool) or not isinstance(reference, int):
                            errors.append(
                                f"{event_id}/{fact_key}: supersedes references "
                                "must be integer fact IDs"
                            )
                            continue
                    elif not isinstance(reference, str) or not reference:
                        errors.append(
                            f"{event_id}/{fact_key}: supersedes_fact_keys references "
                            "must be nonempty strings"
                        )
                        continue
                    pair = (event_id, f"{prefix}:{reference}")
                    fact_keys = expected.setdefault(pair, set())
                    if fact_key in fact_keys:
                        errors.append(
                            f"{event_id}/{fact_key}: duplicate replacement reference "
                            f"{pair[1]}"
                        )
                    else:
                        fact_keys.add(fact_key)

    actual_pairs: list[tuple[str, str]] = []
    expected_row_fields = {
        "event_id",
        "replaced_fact_ref",
        "replacement_fact_keys",
        "still_current_information",
        "changed_or_ended_information",
    }
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != expected_row_fields:
            errors.append(
                f"replacement_accounting[{index}] must contain exactly "
                f"{sorted(expected_row_fields)}"
            )
            continue
        event_id = row.get("event_id")
        replaced_fact_ref = row.get("replaced_fact_ref")
        if not isinstance(event_id, str) or not event_id:
            errors.append(f"replacement_accounting[{index}] event_id must be a nonempty string")
            continue
        if not isinstance(replaced_fact_ref, str) or not replaced_fact_ref:
            errors.append(
                f"replacement_accounting[{index}] replaced_fact_ref must be a nonempty string"
            )
            continue
        if not (replaced_fact_ref.startswith("id:") or replaced_fact_ref.startswith("key:")):
            errors.append(
                f"replacement_accounting[{index}] replaced_fact_ref must use id: or key:"
            )
        pair = (event_id, replaced_fact_ref)
        actual_pairs.append(pair)
        if pair not in expected:
            errors.append(
                f"replacement_accounting[{index}] replaced_fact_ref is not an explicit "
                f"replacement reference in event {event_id}"
            )

        replacement_fact_keys = row.get("replacement_fact_keys")
        named_replacement_text = ""
        if not isinstance(replacement_fact_keys, list) or not replacement_fact_keys:
            errors.append(
                f"replacement_accounting[{index}] replacement_fact_keys must be a nonempty list"
            )
        elif any(
            not isinstance(value, str) or not value.strip()
            for value in replacement_fact_keys
        ):
            errors.append(
                f"replacement_accounting[{index}] replacement_fact_keys must contain nonempty strings"
            )
        else:
            replacement_key_set = set(replacement_fact_keys)
            if len(replacement_key_set) != len(replacement_fact_keys):
                errors.append(
                    f"replacement_accounting[{index}] replacement_fact_keys must not contain duplicates"
                )
            unknown_keys = replacement_key_set - event_fact_keys.get(event_id, set())
            if unknown_keys:
                errors.append(
                    f"replacement_accounting[{index}] replacement_fact_keys must name facts "
                    f"from event {event_id}: {sorted(unknown_keys)}"
                )
            missing_owners = expected.get(pair, set()) - replacement_key_set
            if missing_owners:
                errors.append(
                    f"replacement_accounting[{index}] replacement_fact_keys must include every "
                    f"fact that explicitly supersedes {replaced_fact_ref}: {sorted(missing_owners)}"
                )
            named_replacement_text = " ".join(
                event_fact_statements.get(event_id, {}).get(key, "")
                for key in replacement_fact_keys
            )

        for field, allow_empty in (
            ("still_current_information", True),
            ("changed_or_ended_information", False),
        ):
            values = row.get(field)
            if not isinstance(values, list) or (not allow_empty and not values):
                errors.append(
                    f"replacement_accounting[{index}] {field} must be "
                    f"{'a list' if allow_empty else 'a nonempty list'}"
                )
            elif any(not isinstance(value, str) or not value.strip() for value in values):
                errors.append(
                    f"replacement_accounting[{index}] {field} must contain nonempty strings"
                )
            elif field == "still_current_information":
                normalized_replacement_text = " ".join(named_replacement_text.split())
                for item in values:
                    normalized_item = " ".join(item.split())
                    if normalized_item not in normalized_replacement_text:
                        errors.append(
                            f"replacement_accounting[{index}] still_current_information "
                            "must be copied from a named replacement fact statement: "
                            f"{item!r}"
                        )

    if len(actual_pairs) != len(set(actual_pairs)):
        errors.append("replacement_accounting must not contain duplicate event/fact rows")
    if set(actual_pairs) != set(expected):
        errors.append(
            "replacement_accounting must contain exactly one row for every "
            "explicit supersedes reference"
        )
    return errors


def validate_finalization_response(
    response: dict[str, Any],
    canonical_plan: dict[str, Any],
    replacement_accounting: list[dict[str, Any]],
    *,
    period_start: date,
    period_end: date,
    first_event_date: date | None = None,
    starting_entities: list[dict[str, Any]],
    starting_facts: list[dict[str, Any]],
    overview: dict[str, Any],
    previous_plan: dict[str, Any],
) -> list[str]:
    errors = validate_replacement_accounting(
        replacement_accounting, canonical_plan=canonical_plan
    )
    errors.extend(
        validate_plan(
            canonical_plan,
            period_start=period_start,
            period_end=period_end,
            first_event_date=first_event_date,
            starting_entities=starting_entities,
            starting_facts=starting_facts,
            overview=overview,
            previous_plan=previous_plan,
        )
    )
    return errors


def quality_review_payload(
    response: dict[str, Any],
    starting_facts: list[dict[str, Any]],
    *,
    replacement_accounting: list[dict[str, Any]],
    primary_subject_ids: set[str],
    overview: dict[str, Any] | None = None,
    previous_plan: dict[str, Any] | None = None,
    continuity_context: dict[str, Any] | None = None,
    current_ordinary_states: list[dict[str, Any]] | None = None,
    event_ids: set[str] | None = None,
) -> dict[str, Any]:
    plan = response.get("plan") or {}
    events = [
        event
        for event in plan.get("events") or []
        if isinstance(event, dict)
        and (event_ids is None or str(event.get("id")) in event_ids)
    ]
    current_facts = current_starting_facts(starting_facts)
    selected_fact_ids = {
        int(fact_id)
        for event in events
        for fact_id in event.get("relevant_existing_fact_ids") or []
    }
    selected_fact_ids.update(
        int(fact_id)
        for event in events
        for fact in event.get("facts_to_establish") or []
        for fact_id in fact.get("supersedes") or []
    )
    selected_fact_ids.update(
        int(str(row["replaced_fact_ref"])[3:])
        for row in replacement_accounting
        if str(row.get("replaced_fact_ref", "")).startswith("id:")
        and str(row.get("event_id")) in {str(event.get("id")) for event in events}
    )
    if event_ids is not None:
        current_facts = [
            fact for fact in current_facts if int(fact["id"]) in selected_fact_ids
        ]
    event_local_continuity_evidence = event_local_review_evidence(
        events,
        current_facts=current_facts,
        continuity_context=continuity_context,
        primary_subject_ids=primary_subject_ids,
    )
    payload = {
        "proposed_plan": {
            "new_entities": [
                entity
                for entity in plan.get("new_entities") or []
                if event_ids is None
                or str(entity.get("introduced_in_event_id")) in event_ids
            ],
            "events": events,
        },
        "current_fact_registry": current_facts,
        "replacement_accounting": [
            row
            for row in replacement_accounting
            if event_ids is None or str(row.get("event_id")) in event_ids
        ],
        "event_local_continuity_evidence": event_local_continuity_evidence,
        "current_ordinary_states": copy.deepcopy(current_ordinary_states or []),
    }
    if event_ids is not None:
        payload["earlier_events_used_by_the_events_being_reviewed"] = (
            earlier_events_used_by_selected_events(
                plan.get("events") or [],
                {str(event_id) for event_id in event_ids},
            )
        )
    if overview is not None:
        payload["multi_year_overview"] = overview
    if previous_plan is not None:
        payload["completed_previous_quarter_plan"] = previous_plan
    if continuity_context is not None:
        source_session_ids = {
            session_id
            for row in event_local_continuity_evidence
            for session_id in row["source_session_ids"]
        }
        payload["continuity_context"] = copy.deepcopy(continuity_context)
        payload["continuity_context"]["accepted_user_messages_from_previous_month"] = [
            row
            for row in continuity_context.get(
                "accepted_user_messages_from_previous_month", []
            )
            if str(row.get("id")) in source_session_ids
        ]
    return payload


def correction_review_scope(
    source_plan: dict[str, Any],
    corrected_plan: dict[str, Any],
    source_accounting: list[dict[str, Any]],
    corrected_accounting: list[dict[str, Any]],
) -> dict[str, Any]:
    """Find the event, fact, and replacement objects changed by a correction."""
    source = source_plan.get("plan") or {}
    corrected = corrected_plan.get("plan") or {}
    source_events = {
        str(event.get("id")): event
        for event in source.get("events") or []
        if isinstance(event, dict) and event.get("id") is not None
    }
    corrected_events = {
        str(event.get("id")): event
        for event in corrected.get("events") or []
        if isinstance(event, dict) and event.get("id") is not None
    }

    def event_body(event: dict[str, Any]) -> dict[str, Any]:
        return {
            key: copy.deepcopy(value)
            for key, value in event.items()
            if key not in {"facts_to_establish"}
        }

    event_ids = {
        event_id
        for event_id in set(source_events) | set(corrected_events)
        if event_body(source_events.get(event_id, {}))
        != event_body(corrected_events.get(event_id, {}))
    }

    def facts_by_key(plan_events: dict[str, dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        return {
            (event_id, str(fact.get("key"))): copy.deepcopy(fact)
            for event_id, event in plan_events.items()
            for fact in event.get("facts_to_establish") or []
            if isinstance(fact, dict) and fact.get("key") is not None
        }

    source_facts = facts_by_key(source_events)
    corrected_facts = facts_by_key(corrected_events)
    fact_keys = {
        key
        for key in set(source_facts) | set(corrected_facts)
        if source_facts.get(key) != corrected_facts.get(key)
    }

    def replacement_entries(
        accounting: list[dict[str, Any]],
    ) -> dict[tuple[str, str, str], dict[str, Any]]:
        return {
            (
                str(row.get("event_id") or ""),
                str(fact_key),
                str(row.get("replaced_fact_ref") or ""),
            ): row
            for row in accounting
            if isinstance(row, dict)
            for fact_key in row.get("replacement_fact_keys") or []
        }

    source_replacements = replacement_entries(source_accounting)
    corrected_replacements = replacement_entries(corrected_accounting)
    replacement_keys = {
        key
        for key in set(source_replacements) | set(corrected_replacements)
        if source_replacements.get(key) != corrected_replacements.get(key)
    }

    # A fact can change the meaning of a later replacement even when that later
    # row's own text is unchanged. Include rows that name the changed fact.
    changed_fact_names = {fact_key for _, fact_key in fact_keys}
    for key, row in {**source_replacements, **corrected_replacements}.items():
        replacement_facts = {
            str(value) for value in row.get("replacement_fact_keys") or []
        }
        replaced_ref = str(row.get("replaced_fact_ref") or "")
        if replacement_facts & changed_fact_names or replaced_ref in {
            f"key:{fact_key}" for fact_key in changed_fact_names
        }:
            replacement_keys.add(key)

    fact_keys_for_event = {event_id for event_id, _ in fact_keys}
    replacement_event_ids = {event_id for event_id, _, _ in replacement_keys}
    context_event_ids = event_ids | fact_keys_for_event | replacement_event_ids
    review_fact_keys = fact_keys.intersection(corrected_facts)
    review_replacement_keys = replacement_keys.intersection(corrected_replacements)
    return {
        "event_ids": sorted(event_ids),
        "fact_keys": sorted(
            [{"event_id": event_id, "fact_key": fact_key} for event_id, fact_key in fact_keys],
            key=lambda row: (row["event_id"], row["fact_key"]),
        ),
        "review_fact_keys": sorted(
            [
                {"event_id": event_id, "fact_key": fact_key}
                for event_id, fact_key in review_fact_keys
            ],
            key=lambda row: (row["event_id"], row["fact_key"]),
        ),
        "replacement_keys": sorted(
            [
                {
                    "event_id": event_id,
                    "fact_key": fact_key,
                    "replaced_fact_ref": replaced_fact_ref,
                }
                for event_id, fact_key, replaced_fact_ref in replacement_keys
            ],
            key=lambda row: (row["event_id"], row["fact_key"], row["replaced_fact_ref"]),
        ),
        "review_replacement_keys": sorted(
            [
                {
                    "event_id": event_id,
                    "fact_key": fact_key,
                    "replaced_fact_ref": replaced_fact_ref,
                }
                for event_id, fact_key, replaced_fact_ref in review_replacement_keys
            ],
            key=lambda row: (row["event_id"], row["fact_key"], row["replaced_fact_ref"]),
        ),
        "context_event_ids": sorted(context_event_ids),
        "plan_changed": (
            {
                key: source.get(key)
                for key in ("period_start", "period_end", "protected_changes", "new_entities")
            }
            != {
                key: corrected.get(key)
                for key in ("period_start", "period_end", "protected_changes", "new_entities")
            }
        ),
    }


def targeted_quality_review_payload(
    response: dict[str, Any],
    starting_facts: list[dict[str, Any]],
    *,
    replacement_accounting: list[dict[str, Any]],
    primary_subject_ids: set[str],
    scope: dict[str, Any],
    overview: dict[str, Any] | None = None,
    previous_plan: dict[str, Any] | None = None,
    continuity_context: dict[str, Any] | None = None,
    current_ordinary_states: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a review input containing only objects changed by a correction."""
    context_event_ids = set(scope["context_event_ids"])
    payload = quality_review_payload(
        response,
        starting_facts,
        replacement_accounting=replacement_accounting,
        primary_subject_ids=primary_subject_ids,
        overview=overview,
        previous_plan=previous_plan,
        continuity_context=continuity_context,
        current_ordinary_states=current_ordinary_states,
        event_ids=context_event_ids,
    )
    fact_keys = {
        (str(row["event_id"]), str(row["fact_key"]))
        for row in scope["review_fact_keys"]
    }
    event_ids = set(scope["event_ids"])
    for event in payload["proposed_plan"]["events"]:
        event_id = str(event.get("id"))
        if event_id not in event_ids:
            event["facts_to_establish"] = [
                fact
                for fact in event.get("facts_to_establish") or []
                if (event_id, str(fact.get("key"))) in fact_keys
            ]
    replacement_keys = {
        (
            str(row["event_id"]),
            str(row["fact_key"]),
            str(row["replaced_fact_ref"]),
        )
        for row in scope["review_replacement_keys"]
    }
    payload["replacement_accounting"] = [
        row
        for row in payload["replacement_accounting"]
        if any(
            (
                str(row.get("event_id") or ""),
                str(fact_key),
                str(row.get("replaced_fact_ref") or ""),
            ) in replacement_keys
            for fact_key in row.get("replacement_fact_keys") or []
        )
    ]
    payload["correction_review_scope"] = {
        "event_ids": list(scope["event_ids"]),
        "fact_reviews": copy.deepcopy(scope["review_fact_keys"]),
        "replacement_reviews": copy.deepcopy(scope["review_replacement_keys"]),
    }
    if scope.get("prior_plan_review_reason"):
        payload["correction_review_scope"]["prior_plan_review_reason"] = str(
            scope["prior_plan_review_reason"]
        )
    return payload


def _plain_label(value: str) -> str:
    return value.replace("_", " ").capitalize()


def _plain_scalar(value: Any) -> str:
    if value is None:
        return "none"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _render_plain_value(lines: list[str], label: str, value: Any, indent: str = "") -> None:
    """Render a JSON value without converting the review input back to JSON."""
    prefix = f"{indent}{label}:"
    if isinstance(value, dict):
        if not value:
            lines.append(f"{prefix} none")
            return
        lines.append(prefix)
        for key in sorted(value, key=str):
            item = value[key]
            _render_plain_value(lines, _plain_label(str(key)), item, indent + "  ")
        return
    if isinstance(value, list):
        if not value:
            lines.append(f"{prefix} none")
            return
        lines.append(prefix)
        for index, item in enumerate(value, start=1):
            if isinstance(item, (dict, list)):
                lines.append(f"{indent}  Item {index}:")
                _render_plain_value(lines, "Value", item, indent + "    ")
            else:
                lines.append(f"{indent}  - {_plain_scalar(item)}")
        return
    lines.append(f"{prefix} {_plain_scalar(value)}")


def _render_current_facts(lines: list[str], label: str, facts: list[Any]) -> None:
    lines.append(f"{label}:")
    if not facts:
        lines.append("  none")
        return
    for index, fact in enumerate(facts, start=1):
        if not isinstance(fact, dict) or "id" not in fact:
            lines.append(f"  Item {index}:")
            _render_plain_value(lines, "Value", fact, "    ")
            continue
        lines.append(f"  Current fact ID {fact['id']}:")
        for key in sorted(fact, key=str):
            if key == "id":
                continue
            if key == "supersedes":
                field_label = "Old fact IDs already replaced; do not cite them"
            elif key == "supersedes_fact_keys":
                field_label = "Old fact keys already replaced; do not cite them"
            else:
                field_label = _plain_label(str(key))
            _render_plain_value(lines, field_label, fact[key], "    ")


def _render_current_ordinary_states(
    lines: list[str], states: list[dict[str, Any]]
) -> None:
    lines.append("Current conditions established by accepted sessions:")
    if not states:
        lines.append("  none")
        return
    for state in states:
        lines.append(
            "  - "
            + " | ".join(
                (
                    f"Date: {state.get('date')}",
                    f"Subjects: {', '.join(map(str, state.get('subject_ids') or []))}",
                    f"Condition: {state.get('statement')}",
                    f"Source session: {state.get('source_session_id')}",
                    f"State key: {state.get('key')}",
                )
            )
        )


def render_quarter_stage_request(name: str, payload: dict[str, Any]) -> str:
    """Render each quarter-planning payload as stable labeled plain text."""
    if name == "quality_review":
        return render_quarter_quality_review_request(payload)

    stage_sections = {
        "story": (
            "persona",
            "persona_sheet",
            "generator_context",
            "period_start",
            "period_end",
            "first_date_for_new_events",
            "multi_year_overview",
            "recent_accepted_session_purposes",
            "accepted_user_messages_from_previous_month",
            "entity_registry",
            "current_fact_registry",
            "current_ordinary_states",
            "completed_previous_quarter_plan",
        ),
        "fact_finalization": (
            "fixed_story",
            "current_fact_registry",
            "current_ordinary_states",
        ),
        "event_repair": (
            "quarter_date_bounds",
            "failed_event_review_reasons",
            "failed_events",
            "accepted_current_facts",
            "current_ordinary_states",
            "earlier_same_quarter_events",
            "existing_entities",
        ),
        "fact_repair": (
            "event_ids_to_repair",
            "review_failures",
            "exact_starting_facts_used_or_replaced",
            "current_ordinary_states",
            "relevant_earlier_quarter_facts",
            "fixed_story_events",
            "event_fact_annotations_to_repair",
            "fact_replacement_rows_to_repair",
            "earlier_same_quarter_changes_to_starting_facts",
            "later_events_that_depend_on_repaired_facts",
            "fact_keys_from_other_events",
        ),
    }
    titles = {
        "story": "Quarter Story Input",
        "fact_finalization": "Quarter Fact Annotation Input",
        "event_repair": "Quarter Event Repair Input",
        "fact_repair": "Quarter Fact Repair Input",
    }
    try:
        section_order = stage_sections[name]
        title = titles[name]
    except KeyError as exc:
        raise ValueError(f"unsupported quarter-planning stage: {name}") from exc

    lines = [title, ""]
    ordered_keys = [key for key in section_order if key in payload]
    ordered_keys.extend(sorted(set(payload) - set(ordered_keys), key=str))
    for key in ordered_keys:
        if key in {
            "current_fact_registry",
            "accepted_current_facts",
            "exact_starting_facts_used_or_replaced",
        } and isinstance(payload[key], list):
            _render_current_facts(lines, _plain_label(str(key)), payload[key])
        elif key == "current_ordinary_states" and isinstance(payload[key], list):
            _render_current_ordinary_states(lines, payload[key])
        else:
            _render_plain_value(lines, _plain_label(str(key)), payload[key])
    return "\n".join(lines).rstrip() + "\n"


def _render_record(
    lines: list[str], record: dict[str, Any], *, skip: set[str] | None = None, indent: str = ""
) -> None:
    for key in sorted(record, key=str):
        value = record[key]
        if skip is None or key not in skip:
            _render_plain_value(lines, _plain_label(str(key)), value, indent)


def _render_fact(lines: list[str], fact: dict[str, Any], *, indent: str = "") -> None:
    """Render only the fact fields needed to judge meaning and current scope."""
    for key in (
        "id",
        "construction_key",
        "key",
        "statement",
        "applies_when",
        "subjects",
        "supersedes",
        "supersedes_fact_keys",
    ):
        if key in fact:
            _render_plain_value(lines, _plain_label(key), fact[key], indent)


def _event_sort_key(event: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(event.get("start_date") or ""),
        str(event.get("end_date") or ""),
        str(event.get("id") or ""),
    )


def _render_event(
    lines: list[str],
    event: dict[str, Any],
    *,
    facts_by_id: dict[str, dict[str, Any]],
    facts_by_key: dict[str, dict[str, Any]],
    evidence_by_event_id: dict[str, dict[str, Any]],
    accounting_by_event_id: dict[str, list[dict[str, Any]]],
) -> None:
    event_id = str(event.get("id") or "")
    lines.append(f"Event {event_id}")
    lines.append(
        f"Dates: {_plain_scalar(event.get('start_date'))} to {_plain_scalar(event.get('end_date'))}"
    )
    _render_plain_value(lines, "Subjects", event.get("subjects", []))
    _render_plain_value(
        lines, "Earlier event IDs it continues", event.get("builds_on_event_ids", [])
    )
    _render_plain_value(lines, "What happens", event.get("what_happens"))
    _render_plain_value(lines, "Lasting outcome", event.get("lasting_outcome"))
    _render_record(
        lines,
        event,
        skip={
            "id",
            "start_date",
            "end_date",
            "subjects",
            "builds_on_event_ids",
            "what_happens",
            "lasting_outcome",
            "relevant_existing_fact_ids",
            "facts_to_establish",
        },
    )

    lines.append("Relevant existing facts:")
    relevant_fact_ids = event.get("relevant_existing_fact_ids") or []
    if not relevant_fact_ids:
        lines.append("  none")
    for fact_id in relevant_fact_ids:
        lines.append(f"  Fact ID {fact_id}:")
        fact = facts_by_id.get(str(fact_id))
        if fact is None:
            lines.append("    Not present in the supplied current fact registry.")
        else:
            _render_fact(lines, fact, indent="    ")

    evidence = evidence_by_event_id.get(event_id)
    if evidence is not None:
        lines.append("Continuity evidence:")
        _render_record(lines, evidence, indent="  ")

    lines.append("Facts it creates:")
    facts_to_establish = event.get("facts_to_establish") or []
    if not facts_to_establish:
        lines.append("  none")
    for fact_number, fact in enumerate(facts_to_establish, start=1):
        if not isinstance(fact, dict):
            _render_plain_value(lines, f"Fact {fact_number}", fact, "  ")
            continue
        lines.append(f"  Fact {fact_number}:")
        _render_fact(lines, fact, indent="    ")

    lines.append("Replacement accounting:")
    accounting_rows = accounting_by_event_id.get(event_id, [])
    if not accounting_rows:
        lines.append("  none")
    for row in accounting_rows:
        lines.append("  Replacement:")
        replaced_ref = str(row.get("replaced_fact_ref") or "")
        lines.append(f"    Old fact reference: {replaced_ref}")
        old_fact = (
            facts_by_id.get(replaced_ref[3:])
            if replaced_ref.startswith("id:")
            else facts_by_key.get(replaced_ref[4:])
            if replaced_ref.startswith("key:")
            else None
        )
        if old_fact is not None:
            lines.append("    Old fact:")
            _render_fact(lines, old_fact, indent="      ")
        _render_record(lines, row, skip={"event_id", "replaced_fact_ref"}, indent="    ")
    lines.append("")


def render_quarter_quality_review_request(payload: dict[str, Any]) -> str:
    """Render the structured review request as deterministic chronological plain text."""
    lines = ["Quarter Quality Review", ""]
    correction_scope = payload.get("correction_review_scope")
    if isinstance(correction_scope, dict):
        lines.extend(
            [
                "Correction review scope",
                "Review only these changed objects:",
                "- Events: " + ", ".join(map(str, correction_scope.get("event_ids") or [])),
                "- Facts: " + ", ".join(
                    f"{row.get('event_id')} / {row.get('fact_key')}"
                    for row in correction_scope.get("fact_reviews") or []
                ),
                "- Replacements: " + ", ".join(
                    f"{row.get('event_id')} / {row.get('fact_key')} / {row.get('replaced_fact_ref')}"
                    for row in correction_scope.get("replacement_reviews") or []
                ),
                *(
                    [
                        "Earlier whole-plan problem to reconsider:",
                        str(correction_scope["prior_plan_review_reason"]),
                    ]
                    if correction_scope.get("prior_plan_review_reason")
                    else []
                ),
                "Return rows only for those objects. Do not reassess unchanged objects.",
                "",
            ]
        )
    proposed_plan = payload.get("proposed_plan") or {}
    current_facts = payload.get("current_fact_registry") or []
    facts_by_id = {
        str(fact.get("id")): fact
        for fact in current_facts
        if isinstance(fact, dict) and fact.get("id") is not None
    }
    review_events = [
        event for event in proposed_plan.get("events") or [] if isinstance(event, dict)
    ]
    earlier_events = [
        event
        for event in payload.get("earlier_events_used_by_the_events_being_reviewed") or []
        if isinstance(event, dict)
    ]
    facts_by_key = {
        str(fact.get("key")): fact
        for event in [*review_events, *earlier_events]
        for fact in event.get("facts_to_establish") or []
        if isinstance(fact, dict) and fact.get("key") is not None
    }
    evidence_by_event_id = {
        str(row.get("event_id")): row
        for row in payload.get("event_local_continuity_evidence") or []
        if isinstance(row, dict) and row.get("event_id") is not None
    }
    accounting_by_event_id: dict[str, list[dict[str, Any]]] = {}
    for row in payload.get("replacement_accounting") or []:
        if isinstance(row, dict):
            accounting_by_event_id.setdefault(str(row.get("event_id") or ""), []).append(row)

    directly_used_fact_ids = {
        str(fact_id)
        for event in review_events
        for fact_id in event.get("relevant_existing_fact_ids") or []
    }
    directly_used_fact_ids.update(
        str(fact_id)
        for event in review_events
        for fact in event.get("facts_to_establish") or []
        if isinstance(fact, dict)
        for fact_id in fact.get("supersedes") or []
    )

    lines.append("Current facts used or replaced by the proposed quarter")
    if not current_facts:
        lines.append("None supplied.")
    for fact in current_facts:
        if isinstance(fact, dict) and str(fact.get("id")) in directly_used_fact_ids:
            lines.append(f"Fact ID {fact.get('id')}:")
            _render_fact(lines, fact, indent="  ")
        elif not isinstance(fact, dict):
            _render_plain_value(lines, "Fact", fact)
    lines.append("")

    other_current_facts = [
        fact
        for fact in current_facts
        if isinstance(fact, dict) and str(fact.get("id")) not in directly_used_fact_ids
    ]
    lines.append("Other current facts about the same subjects")
    if not other_current_facts:
        lines.append("None supplied.")
    for fact in other_current_facts:
        lines.append(
            f"- Fact ID {fact.get('id')}: {fact.get('statement')} "
            f"| Applies when: {fact.get('applies_when')}"
        )
    lines.append("")

    _render_current_ordinary_states(
        lines, payload.get("current_ordinary_states") or []
    )
    lines.append("")

    previous_plan = payload.get("completed_previous_quarter_plan")
    if previous_plan is not None:
        lines.append("Previous-quarter chronology")
        if isinstance(previous_plan, dict):
            _render_record(lines, previous_plan, skip={"events"})
            previous_events = [
                event for event in previous_plan.get("events") or [] if isinstance(event, dict)
            ]
            if not previous_events:
                lines.append("Events: none")
            for event in sorted(previous_events, key=_event_sort_key):
                lines.append(
                    "- "
                    + " | ".join(
                        (
                            f"Dates: {event.get('start_date')} to {event.get('end_date')}",
                            f"Event ID: {event.get('id')}",
                            f"What happens: {event.get('what_happens')}",
                            f"Lasting outcome: {event.get('lasting_outcome')}",
                        )
                    )
                )
        else:
            _render_plain_value(lines, "Completed previous-quarter plan", previous_plan)
        lines.append("")

    if "multi_year_overview" in payload:
        lines.append("Long-range overview")
        _render_plain_value(lines, "Overview", payload["multi_year_overview"])
        lines.append("")

    if "continuity_context" in payload:
        lines.append("Recent accepted continuity")
        continuity = payload["continuity_context"]
        if isinstance(continuity, dict):
            purposes = continuity.get("recent_accepted_session_purposes") or []
            lines.append("Recent session purposes:")
            if not purposes:
                lines.append("  none")
            for row in purposes:
                if not isinstance(row, dict):
                    _render_plain_value(lines, "Purpose", row, "  ")
                    continue
                lines.append(
                    "  - "
                    + " | ".join(
                        (
                            f"Date: {row.get('narrative_date')}",
                            f"Session ID: {row.get('session_id')}",
                            f"Purpose: {row.get('purpose')}",
                            "Subjects: "
                            + ", ".join(map(str, row.get("subject_ids") or [])),
                        )
                    )
                )
            messages = continuity.get("accepted_user_messages_from_previous_month") or []
            lines.append("Accepted messages cited as evidence:")
            if not messages:
                lines.append("  none")
            for message in messages:
                if isinstance(message, dict):
                    lines.append(f"  Session {message.get('id') or message.get('session_id')}:")
                    _render_record(lines, message, indent="    ")
                else:
                    _render_plain_value(lines, "Message", message, "  ")
        else:
            _render_plain_value(lines, "Continuity", continuity)
        lines.append("")

    lines.append("New entities introduced in this quarter")
    _render_plain_value(lines, "Entities", proposed_plan.get("new_entities", []))
    lines.append("")

    if earlier_events:
        lines.append("Earlier proposed events supporting this review")
        for event in sorted(earlier_events, key=_event_sort_key):
            _render_event(
                lines,
                event,
                facts_by_id=facts_by_id,
                facts_by_key=facts_by_key,
                evidence_by_event_id=evidence_by_event_id,
                accounting_by_event_id=accounting_by_event_id,
            )

    lines.append("Proposed-quarter timeline")
    for event in sorted(review_events, key=_event_sort_key):
        _render_event(
            lines,
            event,
            facts_by_id=facts_by_id,
            facts_by_key=facts_by_key,
            evidence_by_event_id=evidence_by_event_id,
            accounting_by_event_id=accounting_by_event_id,
        )
    if not review_events:
        lines.append("No proposed events.")

    handled_fields = {
        "proposed_plan",
        "current_fact_registry",
        "replacement_accounting",
        "event_local_continuity_evidence",
        "earlier_events_used_by_the_events_being_reviewed",
        "completed_previous_quarter_plan",
        "multi_year_overview",
        "continuity_context",
    }
    for key, value in payload.items():
        if key not in handled_fields:
            lines.append("")
            _render_plain_value(lines, _plain_label(str(key)), value)
    return "\n".join(lines).rstrip() + "\n"


def earlier_events_used_by_selected_events(
    events: list[dict[str, Any]], selected_event_ids: set[str]
) -> list[dict[str, Any]]:
    """Return only the recursive proposed-event ancestors of selected events."""
    events_by_id = {
        str(event.get("id")): event
        for event in events
        if isinstance(event, dict) and event.get("id") is not None
    }
    earlier_ids: set[str] = set()
    to_visit = list(selected_event_ids)
    while to_visit:
        event_id = to_visit.pop()
        event = events_by_id.get(event_id)
        if event is None:
            continue
        for earlier_id in event.get("builds_on_event_ids") or []:
            earlier_id = str(earlier_id)
            if earlier_id in events_by_id and earlier_id not in earlier_ids:
                earlier_ids.add(earlier_id)
                to_visit.append(earlier_id)
    return [
        event
        for event in events
        if isinstance(event, dict)
        and str(event.get("id")) in earlier_ids
        and str(event.get("id")) not in selected_event_ids
    ]


def event_local_review_evidence(
    events: list[dict[str, Any]],
    *,
    current_facts: list[dict[str, Any]],
    continuity_context: dict[str, Any] | None,
    primary_subject_ids: set[str],
) -> list[dict[str, Any]]:
    """Give the reviewer IDs that point to the shared evidence already in the payload."""
    del continuity_context, primary_subject_ids
    current_fact_ids = {int(fact["id"]) for fact in current_facts}
    evidence: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("id") or "")
        try:
            relevant_fact_ids = [
                int(fact_id) for fact_id in event.get("relevant_existing_fact_ids") or []
            ]
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{event_id}: relevant_existing_fact_ids must contain numeric IDs"
            ) from exc
        unknown_fact_ids = sorted(set(relevant_fact_ids) - current_fact_ids)
        if unknown_fact_ids:
            raise ValueError(
                f"{event_id}: references unknown current facts {unknown_fact_ids}"
            )
        facts_by_id = {int(fact["id"]): fact for fact in current_facts}
        source_session_ids: list[str] = []
        for fact_id in relevant_fact_ids:
            for session_id in facts_by_id[fact_id].get("source_session_ids") or []:
                if isinstance(session_id, str) and session_id not in source_session_ids:
                    source_session_ids.append(session_id)
        evidence.append(
            {
                "event_id": event_id,
                "relevant_existing_fact_ids": relevant_fact_ids,
                "source_session_ids": source_session_ids,
            }
        )
    return evidence


def load_or_complete_stage(
    client: AzureJsonClient,
    *,
    output_dir: Path,
    name: str,
    system: str,
    payload: dict[str, Any],
    allow_saved_response_mismatch: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Reuse a completed stage or replace an unexecuted draft before calling it."""
    request_path = output_dir / f"{name}_request.json"
    system_path = output_dir / f"{name}_system.txt"
    response_path = output_dir / f"{name}_response.json"
    rendered_user_input = render_quarter_stage_request(name, payload)
    user_input_path = output_dir / f"{name}_user_input.txt"
    request_exists = request_path.exists()
    system_exists = system_path.exists()
    response_exists = response_path.exists()
    if response_exists:
        if not request_exists or not system_exists or not user_input_path.exists():
            raise ValueError(
                f"{name}: a saved response requires its request, system prompt, and user input"
            )
        if not allow_saved_response_mismatch and load_json(request_path) != payload:
            raise ValueError(f"{name}: saved request does not match current inputs")
        if not allow_saved_response_mismatch and system_path.read_text() != system:
            raise ValueError(f"{name}: saved system prompt does not match current prompt")
        if user_input_path.read_text() != rendered_user_input:
            raise ValueError(f"{name}: saved rendered user input does not match current inputs")
        response = load_json(response_path)
        if not isinstance(response, dict):
            raise ValueError(f"{name}: saved response must be a JSON object")
        return response, False

    if request_exists != system_exists:
        raise ValueError(
            f"{name}: an unexecuted stage requires both its request and system prompt"
        )

    # Without a response, the saved pair was never executed. Replace it so a
    # retry cannot send stale inputs, then execute only this stage.
    dump_json(request_path, payload)
    system_path.write_text(system)
    user_input_path.write_text(rendered_user_input)

    response = client.complete(
        system,
        rendered_user_input,
        call_path=response_path,
    )
    dump_json(response_path, response)
    return response, True


def complete_quarter_quality_review(
    client: AzureJsonClient,
    payload: dict[str, Any],
    *,
    output_dir: Path,
) -> dict[str, Any]:
    request_path = output_dir / "quality_review_request.json"
    system_path = output_dir / "quality_review_system.txt"
    response_path = output_dir / "quality_review_response.json"
    dump_json(request_path, payload)
    system_path.write_text(QUARTER_EVENT_QUALITY_REVIEW_SYSTEM)
    rendered_user_input = render_quarter_quality_review_request(payload)
    (output_dir / "quality_review_user_input.txt").write_text(rendered_user_input)
    response = client.complete(
        QUARTER_EVENT_QUALITY_REVIEW_SYSTEM,
        rendered_user_input,
        call_path=response_path,
    )
    dump_json(response_path, response)
    return response


def validate_quality_review(
    review: dict[str, Any],
    event_ids: set[str],
    review_payload: dict[str, Any] | None = None,
    *,
    expected_fact_rows: set[tuple[str, str]] | None = None,
    expected_replacement_rows: set[tuple[str, str, str]] | None = None,
) -> list[str]:
    errors: list[str] = []
    fields = {key for key in review if not key.startswith("_")}
    expected_fields = {
        "passed",
        "plan_review",
        "event_reviews",
        "fact_reviews",
        "replacement_reviews",
    }
    if fields != expected_fields:
        errors.append(f"quality review must contain exactly {sorted(expected_fields)}")
        return errors
    if not isinstance(review.get("passed"), bool):
        errors.append("quality review passed must be a boolean")
    failed = False
    plan_review = review.get("plan_review")
    plan_passed: bool | None = None
    if not isinstance(plan_review, dict) or set(plan_review) != {"passed", "reason"}:
        errors.append("quality review plan_review must contain exactly passed and reason")
    else:
        plan_passed = plan_review.get("passed")
        plan_reason = plan_review.get("reason")
        if not isinstance(plan_passed, bool):
            errors.append("quality review plan_review passed must be a boolean")
        elif plan_passed:
            if not isinstance(plan_reason, str) or plan_reason.strip():
                errors.append("passing quality review plan_review must have an empty reason")
        elif not isinstance(plan_reason, str) or not plan_reason.strip():
            errors.append("failing quality review plan_review must give a reason")
        if isinstance(plan_passed, bool):
            failed = not plan_passed
    rows = review.get("event_reviews")
    if not isinstance(rows, list):
        errors.append("quality review event_reviews must be a list")
        return errors
    reviewed_ids: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"event_id", "passed", "reason"}:
            errors.append(
                f"quality review row {index} must contain exactly event_id, passed, and reason"
            )
            continue
        event_id = str(row.get("event_id") or "")
        reviewed_ids.append(event_id)
        row_passed = row.get("passed")
        reason = row.get("reason")
        if not isinstance(row_passed, bool):
            errors.append(f"quality review row {index} passed must be a boolean")
            continue
        if not isinstance(reason, str):
            errors.append(f"quality review row {index} reason must be a string")
            continue
        if row_passed and reason.strip():
            errors.append(f"passing quality review row {index} must have an empty reason")
        if not row_passed and not reason.strip():
            errors.append(f"failing quality review row {index} must give a reason")
        failed = failed or not row_passed
    if len(reviewed_ids) != len(set(reviewed_ids)):
        errors.append("quality review must assess every event exactly once")
    if set(reviewed_ids) != event_ids or len(reviewed_ids) != len(event_ids):
        errors.append("quality review must assess every event exactly once")
    if expected_fact_rows is None:
        expected_fact_rows = {
            (str(event["id"]), str(fact["key"]))
            for event in ((review_payload or {}).get("proposed_plan") or {}).get("events") or []
            for fact in event.get("facts_to_establish") or []
        }
    if expected_replacement_rows is None:
        expected_replacement_rows = {
            (str(event["id"]), str(fact["key"]), f"id:{int(fact_id)}")
            for event in ((review_payload or {}).get("proposed_plan") or {}).get("events") or []
            for fact in event.get("facts_to_establish") or []
            for fact_id in fact.get("supersedes") or []
        }
        expected_replacement_rows.update(
            (str(event["id"]), str(fact["key"]), f"key:{fact_key}")
            for event in ((review_payload or {}).get("proposed_plan") or {}).get("events") or []
            for fact in event.get("facts_to_establish") or []
            for fact_key in fact.get("supersedes_fact_keys") or []
        )

    def validate_detail_rows(
        field: str,
        expected_keys: set[tuple[str, ...]],
        key_fields: tuple[str, ...],
    ) -> None:
        nonlocal failed
        detail_rows = review.get(field)
        if not isinstance(detail_rows, list):
            errors.append(f"quality review {field} must be a list")
            return
        actual_keys: list[tuple[str, ...]] = []
        expected_row_fields = {*key_fields, "passed", "reason"}
        for index, row in enumerate(detail_rows):
            if not isinstance(row, dict) or set(row) != expected_row_fields:
                errors.append(
                    f"quality review {field} row {index} must contain exactly "
                    f"{sorted(expected_row_fields)}"
                )
                continue
            key = tuple(str(row[name]) for name in key_fields)
            actual_keys.append(key)
            row_passed = row.get("passed")
            reason = row.get("reason")
            if not isinstance(row_passed, bool):
                errors.append(f"quality review {field} row {index} passed must be boolean")
                continue
            if not isinstance(reason, str):
                errors.append(f"quality review {field} row {index} reason must be a string")
            elif row_passed and reason.strip():
                errors.append(
                    f"passing quality review {field} row {index} must have an empty reason"
                )
            elif not row_passed and not reason.strip():
                errors.append(
                    f"failing quality review {field} row {index} must give a reason"
                )
            failed = failed or not row_passed
        if len(actual_keys) != len(set(actual_keys)) or set(actual_keys) != expected_keys:
            errors.append(f"quality review must assess every expected {field} row exactly once")

    validate_detail_rows(
        "fact_reviews", expected_fact_rows, ("event_id", "fact_key")
    )
    validate_detail_rows(
        "replacement_reviews",
        expected_replacement_rows,
        ("event_id", "fact_key", "replaced_fact_ref"),
    )
    if plan_passed is False and not any(
        isinstance(row, dict) and row.get("passed") is False
        for field in ("event_reviews", "fact_reviews", "replacement_reviews")
        for row in review.get(field) or []
    ):
        errors.append(
            "a failing plan review must identify an event, fact, or replacement that causes the problem"
        )
    if review.get("passed") is not (not failed):
        errors.append("quality review overall passed does not match its detailed rows")
    return errors


def normalize_legacy_passing_review_reasons(
    review: dict[str, Any],
) -> dict[str, Any]:
    """Adapt saved reviews from before passing explanations became empty."""
    normalized = copy.deepcopy(review)
    plan_review = normalized.get("plan_review")
    if isinstance(plan_review, dict) and plan_review.get("passed") is True:
        plan_review["reason"] = ""
    for field in ("event_reviews", "fact_reviews", "replacement_reviews"):
        rows = normalized.get(field)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("passed") is True:
                row["reason"] = ""
    return normalized


def mark_missing_quality_review_rows_failed(
    review: dict[str, Any], review_payload: dict[str, Any]
) -> dict[str, Any]:
    """Treat an omitted required review row as a failure, never as a pass."""
    completed = copy.deepcopy(review)
    proposed_events = (review_payload.get("proposed_plan") or {}).get("events") or []
    reason = "The reviewer omitted this required row."

    rows = completed.get("event_reviews")
    if isinstance(rows, list):
        present = {
            str(row.get("event_id")) for row in rows if isinstance(row, dict)
        }
        rows.extend(
            {
                "event_id": str(event["id"]),
                "passed": False,
                "reason": reason,
            }
            for event in proposed_events
            if str(event["id"]) not in present
        )

    fact_rows = completed.get("fact_reviews")
    if isinstance(fact_rows, list):
        present = {
            (str(row.get("event_id")), str(row.get("fact_key")))
            for row in fact_rows
            if isinstance(row, dict)
        }
        for event in proposed_events:
            for fact in event.get("facts_to_establish") or []:
                key = (str(event["id"]), str(fact["key"]))
                if key not in present:
                    fact_rows.append(
                        {
                            "event_id": key[0],
                            "fact_key": key[1],
                            "passed": False,
                            "reason": reason,
                        }
                    )

    replacement_rows = completed.get("replacement_reviews")
    if isinstance(replacement_rows, list):
        present = {
            (
                str(row.get("event_id")),
                str(row.get("fact_key")),
                str(row.get("replaced_fact_ref")),
            )
            for row in replacement_rows
            if isinstance(row, dict)
        }
        expected: list[tuple[str, str, str]] = []
        for event in proposed_events:
            for fact in event.get("facts_to_establish") or []:
                expected.extend(
                    (str(event["id"]), str(fact["key"]), f"id:{int(fact_id)}")
                    for fact_id in fact.get("supersedes") or []
                )
                expected.extend(
                    (str(event["id"]), str(fact["key"]), f"key:{fact_key}")
                    for fact_key in fact.get("supersedes_fact_keys") or []
                )
        replacement_rows.extend(
            {
                "event_id": event_id,
                "fact_key": fact_key,
                "replaced_fact_ref": replaced_fact_ref,
                "passed": False,
                "reason": reason,
            }
            for event_id, fact_key, replaced_fact_ref in expected
            if (event_id, fact_key, replaced_fact_ref) not in present
        )

    if any(
        isinstance(row, dict)
        and row.get("passed") is False
        and row.get("reason") == reason
        for field in ("event_reviews", "fact_reviews", "replacement_reviews")
        for row in completed.get(field) or []
    ):
        completed["passed"] = False
    return completed


def failed_event_review_ids(review: dict[str, Any]) -> set[str]:
    return {
        str(row["event_id"])
        for row in review.get("event_reviews") or []
        if isinstance(row, dict) and row.get("passed") is False
    }


def failed_fact_event_ids(review: dict[str, Any]) -> set[str]:
    return {
        str(row["event_id"])
        for field in ("event_reviews", "fact_reviews", "replacement_reviews")
        for row in review.get(field) or []
        if isinstance(row, dict) and row.get("passed") is False
    }


def merge_quality_reviews(
    initial_review: dict[str, Any],
    correction_review: dict[str, Any],
    corrected_event_ids: set[str],
) -> dict[str, Any]:
    """Replace corrected event rows and any repaired whole-plan verdict."""
    merged = copy.deepcopy(initial_review)
    review_fields = ("event_reviews", "fact_reviews", "replacement_reviews")
    for field in review_fields:
        original_rows = list(merged.get(field) or [])
        correction_rows = [
            copy.deepcopy(row)
            for row in correction_review.get(field) or []
            if isinstance(row, dict) and str(row.get("event_id")) in corrected_event_ids
        ]
        first_removed = next(
            (
                index
                for index, row in enumerate(original_rows)
                if isinstance(row, dict)
                and str(row.get("event_id")) in corrected_event_ids
            ),
            None,
        )
        rows: list[dict[str, Any]] = []
        inserted = False
        for row in original_rows:
            if isinstance(row, dict) and str(row.get("event_id")) in corrected_event_ids:
                if not inserted and first_removed is not None:
                    rows.extend(correction_rows)
                    inserted = True
                continue
            rows.append(row)
        if correction_rows and not inserted:
            rows.extend(correction_rows)
        merged[field] = rows
    plan_review = initial_review.get("plan_review")
    if isinstance(plan_review, dict) and plan_review.get("passed") is False:
        plan_review = correction_review.get("plan_review")
    merged["plan_review"] = copy.deepcopy(plan_review)
    merged["passed"] = all(
        isinstance(row, dict) and row.get("passed") is True
        for field in review_fields
        for row in merged.get(field) or []
    ) and isinstance(merged["plan_review"], dict) and merged["plan_review"].get("passed") is True
    return merged


def merge_targeted_quality_review(
    source_review: dict[str, Any],
    correction_review: dict[str, Any],
    scope: dict[str, Any],
) -> dict[str, Any]:
    """Keep saved results outside the objects changed by a correction."""
    target_keys = {
        "event_reviews": {
            (str(row),) for row in scope.get("event_ids") or []
        },
        "fact_reviews": {
            (str(row["event_id"]), str(row["fact_key"]))
            for row in scope.get("fact_keys") or []
        },
        "replacement_reviews": {
            (
                str(row["event_id"]),
                str(row["fact_key"]),
                str(row["replaced_fact_ref"]),
            )
            for row in scope.get("replacement_keys") or []
        },
    }
    key_fields = {
        "event_reviews": ("event_id",),
        "fact_reviews": ("event_id", "fact_key"),
        "replacement_reviews": ("event_id", "fact_key", "replaced_fact_ref"),
    }
    merged = copy.deepcopy(source_review)
    for field, fields in key_fields.items():
        def row_key(row: dict[str, Any]) -> tuple[str, ...]:
            return tuple(str(row.get(name) or "") for name in fields)

        replacement_rows = {
            row_key(row): copy.deepcopy(row)
            for row in correction_review.get(field) or []
            if isinstance(row, dict)
        }
        saved_rows = list(source_review.get(field) or [])
        first_target_index = next(
            (
                index
                for index, row in enumerate(saved_rows)
                if isinstance(row, dict) and row_key(row) in target_keys[field]
            ),
            len(saved_rows),
        )
        rows = [
            row
            for row in saved_rows
            if not isinstance(row, dict) or row_key(row) not in target_keys[field]
        ]
        insert_at = min(first_target_index, len(rows))
        rows[insert_at:insert_at] = list(replacement_rows.values())
        merged[field] = rows

    if scope.get("plan_changed") or scope.get("prior_plan_review_reason"):
        merged["plan_review"] = copy.deepcopy(correction_review["plan_review"])
    else:
        merged["plan_review"] = copy.deepcopy(source_review["plan_review"])
    merged["passed"] = (
        isinstance(merged["plan_review"], dict)
        and merged["plan_review"].get("passed") is True
        and all(
            isinstance(row, dict) and row.get("passed") is True
            for field in ("event_reviews", "fact_reviews", "replacement_reviews")
            for row in merged[field]
        )
    )
    return merged


def restrict_quality_review(
    review: dict[str, Any], event_ids: set[str]
) -> dict[str, Any]:
    """Read a saved review as a review of only the requested events."""
    restricted = {
        field: [
            copy.deepcopy(row)
            for row in review.get(field) or []
            if isinstance(row, dict) and str(row.get("event_id")) in event_ids
        ]
        for field in ("event_reviews", "fact_reviews", "replacement_reviews")
    }
    restricted["plan_review"] = copy.deepcopy(review.get("plan_review"))
    restricted["passed"] = all(
        row.get("passed") is True
        for field in ("event_reviews", "fact_reviews", "replacement_reviews")
        for row in restricted[field]
    ) and isinstance(restricted["plan_review"], dict) and restricted["plan_review"].get("passed") is True
    return restricted


def fact_repair_payload(
    *,
    failed_event_ids: set[str],
    story_response: dict[str, Any],
    final_response: dict[str, Any],
    canonical_plan: dict[str, Any],
    current_facts: list[dict[str, Any]],
    quality_review: dict[str, Any],
    primary_subject_ids: set[str],
    current_ordinary_states: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    story_events = (story_response.get("story") or {}).get("events") or []
    plan_events = (canonical_plan.get("plan") or {}).get("events") or []
    event_ids_to_repair = set(failed_event_ids)
    events_to_repair = [
        event for event in plan_events if str(event.get("id")) in failed_event_ids
    ]
    directly_used_starting_fact_ids = {
        int(value)
        for event in events_to_repair
        for value in event.get("relevant_existing_fact_ids") or []
        if isinstance(value, int) and not isinstance(value, bool)
    }
    replacement_rows_to_repair = [
        row
        for row in final_response.get("fact_replacements") or []
        if isinstance(row, dict) and str(row.get("event_id")) in failed_event_ids
    ]
    directly_used_starting_fact_ids.update(
        int(row["replaced_fact"]["fact_id"])
        for row in replacement_rows_to_repair
        if isinstance(row.get("replaced_fact"), dict)
        and row["replaced_fact"].get("source") == "starting_checkpoint"
        and isinstance(row["replaced_fact"].get("fact_id"), int)
        and not isinstance(row["replaced_fact"].get("fact_id"), bool)
    )
    event_positions = {
        str(event.get("id")): index
        for index, event in enumerate(story_events)
        if isinstance(event, dict) and isinstance(event.get("id"), str)
    }
    replaced_quarter_sources = {
        (
            str(row["replaced_fact"].get("event_id")),
            row["replaced_fact"].get("fact_number"),
        )
        for row in replacement_rows_to_repair
        if isinstance(row.get("replaced_fact"), dict)
        and row["replaced_fact"].get("source") == "this_quarter"
    }
    referenced_earlier_event_ids = {
        str(earlier_event_id)
        for event in events_to_repair
        for earlier_event_id in event.get("builds_on_event_ids") or []
        if str(earlier_event_id) in event_positions
        and str(event.get("id")) in event_positions
        and event_positions[str(earlier_event_id)]
        < event_positions.get(str(event.get("id")), -1)
    }
    relevant_fact_keys = set(replaced_quarter_sources)
    relevant_fact_keys.update(
        (str(event.get("id")), fact_number)
        for event in plan_events
        if str(event.get("id")) in referenced_earlier_event_ids
        for fact_number, _ in enumerate(event.get("facts_to_establish") or [], start=1)
    )
    earlier_quarter_facts = [
        {
            "event_id": str(event["id"]),
            "fact_number": fact_number,
            "fact": fact,
        }
        for event in plan_events
        for fact_number, fact in enumerate(event.get("facts_to_establish") or [], start=1)
        if (str(event.get("id")), fact_number) in relevant_fact_keys
    ]
    annotations_to_repair = [
        row
        for row in final_response.get("event_fact_annotations") or []
        if str(row.get("event_id")) in failed_event_ids
    ]
    later_dependent_replacement_rows = [
        copy.deepcopy(row)
        for row in final_response.get("fact_replacements") or []
        if isinstance(row, dict)
        and str(row.get("event_id")) in event_ids_to_repair
        and isinstance(row.get("replaced_fact"), dict)
        and row["replaced_fact"].get("source") == "this_quarter"
        and str(row["replaced_fact"].get("event_id")) in event_ids_to_repair
        and event_positions.get(str(row["replaced_fact"].get("event_id")), -1)
        < event_positions.get(str(row.get("event_id")), -1)
    ]
    replacement_accounting = derive_replacement_accounting(
        final_response, canonical_plan
    )
    earlier_same_quarter_changes = []
    starting_fact_references = {
        f"id:{fact_id}" for fact_id in directly_used_starting_fact_ids
    }
    for event in plan_events:
        event_id = str(event.get("id") or "")
        if event_id not in event_positions:
            continue
        matching_rows = [
            copy.deepcopy(row)
            for row in replacement_accounting
            if row.get("event_id") == event_id
            and str(row.get("replaced_fact_ref")) in starting_fact_references
            and any(
                event_positions[event_id] < event_positions[repair_event_id]
                for repair_event_id in event_ids_to_repair
                if repair_event_id in event_positions
            )
        ]
        if matching_rows:
            earlier_same_quarter_changes.append(
                {
                    "event_id": event_id,
                    "facts_to_establish": copy.deepcopy(
                        event.get("facts_to_establish") or []
                    ),
                    "replacement_accounting_rows": matching_rows,
                }
            )
    review_failures = {
        field: [
            row
            for row in quality_review.get(field) or []
            if isinstance(row, dict)
            and row.get("passed") is False
            and str(row.get("event_id")) in failed_event_ids
        ]
        for field in ("event_reviews", "fact_reviews", "replacement_reviews")
    }
    return {
        "event_ids_to_repair": sorted(event_ids_to_repair),
        "fixed_story_events": [
            event
            for event in story_events
            if str(event.get("id")) in failed_event_ids
        ],
        "event_fact_annotations_to_repair": annotations_to_repair,
        "fact_replacement_rows_to_repair": replacement_rows_to_repair,
        "later_events_that_depend_on_repaired_facts": later_dependent_replacement_rows,
        "earlier_same_quarter_changes_to_starting_facts": earlier_same_quarter_changes,
        "review_failures": review_failures,
        "exact_starting_facts_used_or_replaced": [
            {
                key: copy.deepcopy(fact[key])
                for key in (
                    "id",
                    "construction_key",
                    "statement",
                    "applies_when",
                    "subjects",
                    "supersedes",
                )
                if key in fact
            }
            for fact in current_facts
            if int(fact["id"]) in directly_used_starting_fact_ids
        ],
        "current_ordinary_states": copy.deepcopy(current_ordinary_states or []),
        "relevant_earlier_quarter_facts": earlier_quarter_facts,
        "fact_keys_from_other_events": [
            str(fact["key"])
            for event in plan_events
            if str(event.get("id")) not in event_ids_to_repair
            for fact in event.get("facts_to_establish") or []
        ],
    }


def event_repair_payload(
    *,
    failed_event_ids: set[str],
    approved_event_id_renames: dict[str, str] | None = None,
    story_response: dict[str, Any],
    canonical_plan: dict[str, Any],
    current_facts: list[dict[str, Any]],
    quality_review: dict[str, Any],
    known_entities: list[dict[str, Any]],
    period_start: date,
    first_event_date: date,
    period_end: date,
    current_ordinary_states: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the request needed to repair failed story events without changing canon."""
    story_events = (story_response.get("story") or {}).get("events") or []
    existing_entities = event_repair_entities(known_entities, {})
    return {
        "quarter_date_bounds": {
            "period_start": period_start.isoformat(),
            "first_date_for_new_events": first_event_date.isoformat(),
            "period_end": period_end.isoformat(),
        },
        "existing_entities": existing_entities,
        "failed_events": [
            copy.deepcopy(event)
            for event in story_events
            if str(event.get("id")) in failed_event_ids
        ],
        "failed_event_review_reasons": [
            copy.deepcopy(row)
            for row in quality_review.get("event_reviews") or []
            if isinstance(row, dict)
            and row.get("passed") is False
            and str(row.get("event_id")) in failed_event_ids
        ],
        "approved_event_id_renames": [
            {
                "event_id": event_id,
                "replacement_event_id": replacement_event_id,
            }
            for event_id, replacement_event_id in sorted(
                (approved_event_id_renames or {}).items()
            )
        ],
        "accepted_current_facts": copy.deepcopy(current_facts),
        "current_ordinary_states": copy.deepcopy(current_ordinary_states or []),
        "earlier_same_quarter_events": earlier_events_used_by_selected_events(
            story_events, failed_event_ids
        ),
    }


def validate_event_repair_response(
    response: dict[str, Any],
    story_response: dict[str, Any],
    failed_event_ids: set[str],
    *,
    known_entity_ids: set[str] | None = None,
    approved_event_id_renames: dict[str, str] | None = None,
) -> list[str]:
    errors: list[str] = []
    if {key for key in response if not key.startswith("_")} != {"new_entities", "events"}:
        errors.append("event repair response must contain exactly new_entities and events")
    new_entities = response.get("new_entities")
    if not isinstance(new_entities, list):
        errors.append("event repair new_entities must be a list")
        new_entities = []
    replacements = response.get("events")
    if not isinstance(replacements, list):
        return errors + ["event repair events must be a list"]

    original_events = (story_response.get("story") or {}).get("events") or []
    approved_event_id_renames = approved_event_id_renames or {}
    renamed_ids = {value: key for key, value in approved_event_id_renames.items()}
    original_ids = {
        str(event.get("id"))
        for event in original_events
        if isinstance(event, dict) and isinstance(event.get("id"), str)
    }
    replacement_ids: list[str] = []
    repair_events_by_id: dict[str, dict[str, Any]] = {}
    for index, event in enumerate(replacements):
        prefix = f"event repair events[{index}]"
        if not isinstance(event, dict) or set(event) != STORY_EVENT_FIELDS:
            errors.append(f"{prefix} must contain exactly {sorted(STORY_EVENT_FIELDS)}")
            continue
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id:
            errors.append(f"{prefix} id must be a nonempty string")
            continue
        replacement_ids.append(event_id)
        repair_events_by_id[event_id] = event
        original_event_id = renamed_ids.get(event_id, event_id)
        if original_event_id not in original_ids:
            errors.append(f"{prefix} names an event that is not in the story: {event_id}")
        if original_event_id not in failed_event_ids:
            errors.append(f"{prefix} may replace only a failed event: {event_id}")
        for field in ("start_date", "end_date", "what_happens", "lasting_outcome"):
            if not isinstance(event.get(field), str) or not event[field].strip():
                errors.append(f"{prefix} {field} must be a nonempty string")
        for field in ("subjects", "builds_on_event_ids"):
            if not isinstance(event.get(field), list) or any(
                not isinstance(value, str) or not value
                for value in event.get(field) or []
            ):
                errors.append(f"{prefix} {field} must be a list of nonempty strings")
        if known_entity_ids is not None:
            unknown_subjects = sorted(
                set(event.get("subjects") or [])
                - (known_entity_ids | {
                    str(entity.get("id"))
                    for entity in new_entities
                    if isinstance(entity, dict) and isinstance(entity.get("id"), str)
                })
            )
            if unknown_subjects:
                errors.append(f"{prefix} has unknown subjects {unknown_subjects}")
    if len(replacement_ids) != len(set(replacement_ids)):
        errors.append("event repair must not return duplicate event IDs")
    expected_replacement_ids = {
        approved_event_id_renames.get(event_id, event_id)
        for event_id in failed_event_ids
    }
    if (
        set(replacement_ids) != expected_replacement_ids
        or len(replacement_ids) != len(expected_replacement_ids)
    ):
        errors.append("event repair must return every failed event exactly once")

    new_entity_ids: set[str] = set()
    for index, entity in enumerate(new_entities):
        prefix = f"event repair new_entities[{index}]"
        if not isinstance(entity, dict) or set(entity) != NEW_ENTITY_FIELDS:
            errors.append(f"{prefix} must contain exactly {sorted(NEW_ENTITY_FIELDS)}")
            continue
        for field in NEW_ENTITY_FIELDS:
            if not isinstance(entity[field], str) or not entity[field].strip():
                errors.append(f"{prefix} {field} must be a nonempty string")
        entity_id = entity["id"]
        if entity_id in new_entity_ids:
            errors.append(f"{prefix} repeats entity id {entity_id}")
        if known_entity_ids is not None and entity_id in known_entity_ids:
            errors.append(f"{prefix} reuses an existing entity id {entity_id}")
        new_entity_ids.add(entity_id)
        introduction_event_id = entity["introduced_in_event_id"]
        event = repair_events_by_id.get(introduction_event_id)
        if event is None:
            errors.append(f"{prefix} introduction event must be one repaired event")
        elif entity_id not in set(event.get("subjects") or []):
            errors.append(
                f"{prefix} introduction event must include the new entity in subjects"
            )
    return errors


def merge_event_repair(
    *,
    story_response: dict[str, Any],
    repair_response: dict[str, Any],
    failed_event_ids: set[str],
    known_entity_ids: set[str] | None = None,
    approved_event_id_renames: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors = validate_event_repair_response(
        repair_response,
        story_response,
        failed_event_ids,
        known_entity_ids=known_entity_ids,
        approved_event_id_renames=approved_event_id_renames,
    )
    if errors:
        return None, errors
    merged = copy.deepcopy(story_response)
    events = (merged.get("story") or {}).get("events") or []
    approved_event_id_renames = approved_event_id_renames or {}
    renamed_ids = {value: key for key, value in approved_event_id_renames.items()}
    replacements = {
        renamed_ids.get(str(event["id"]), str(event["id"])): copy.deepcopy(event)
        for event in repair_response["events"]
    }
    for index, event in enumerate(events):
        event_id = str(event.get("id"))
        if event_id in replacements:
            events[index] = replacements[event_id]
    for event in events:
        event["builds_on_event_ids"] = [
            approved_event_id_renames.get(str(event_id), str(event_id))
            for event_id in event.get("builds_on_event_ids") or []
        ]
    for entity in (merged.get("story") or {}).get("new_entities") or []:
        if isinstance(entity, dict):
            introduction_event_id = str(entity.get("introduced_in_event_id") or "")
            entity["introduced_in_event_id"] = approved_event_id_renames.get(
                introduction_event_id, introduction_event_id
            )
    events.sort(
        key=lambda event: (
            date.fromisoformat(str(event["end_date"])),
            date.fromisoformat(str(event["start_date"])),
            str(event["id"]),
        )
    )
    merged["story"]["events"] = events
    merged["story"]["new_entities"] = [
        *(merged["story"].get("new_entities") or []),
        *copy.deepcopy(repair_response.get("new_entities") or []),
    ]
    return merged, []


def run_final_targeted_correction(
    *,
    client: AzureJsonClient,
    output_dir: Path,
    current_story_response: dict[str, Any],
    current_final_response: dict[str, Any],
    current_plan: dict[str, Any],
    current_accounting: list[dict[str, Any]],
    current_review: dict[str, Any],
    current_full_review: dict[str, Any],
    current_facts: list[dict[str, Any]],
    starting_facts: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    primary_subject_ids: set[str],
    overview: dict[str, Any],
    previous_plan: dict[str, Any],
    recent_session_purposes: list[dict[str, Any]],
    recent_history: list[dict[str, Any]],
    current_ordinary_states: list[dict[str, Any]],
    period_start: date,
    first_event_date: date,
    period_end: date,
    repair_dir_name: str = "final_repair",
    fact_event_ids: set[str] | None = None,
    approved_event_id_renames: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Use one final correction pass on the failures in a saved correction review."""
    approved_event_id_renames = approved_event_id_renames or {}
    event_ids = failed_event_review_ids(current_review)
    fact_event_ids = (
        failed_fact_event_ids(current_review)
        if fact_event_ids is None
        else set(fact_event_ids)
    )
    corrected_event_ids = event_ids | fact_event_ids
    if not corrected_event_ids:
        return {
            "story_response": current_story_response,
            "final_response": current_final_response,
            "canonical_plan": current_plan,
            "accounting": current_accounting,
            "quality_review": current_full_review,
            "errors": [],
            "model_calls": 0,
        }

    repair_dir = output_dir / repair_dir_name
    repair_dir.mkdir(exist_ok=True)
    story_response = copy.deepcopy(current_story_response)
    final_response = copy.deepcopy(current_final_response)
    canonical_plan = copy.deepcopy(current_plan)
    accounting = copy.deepcopy(current_accounting)
    review_base_plan = copy.deepcopy(current_plan)
    review_base_accounting = copy.deepcopy(current_accounting)
    errors: list[str] = []
    model_calls = 0

    if event_ids:
        existing_entities = event_repair_entities(entities, canonical_plan)
        payload = event_repair_payload(
            failed_event_ids=event_ids,
            approved_event_id_renames=approved_event_id_renames,
            story_response=story_response,
            canonical_plan=canonical_plan,
            current_facts=current_facts,
            quality_review=current_review,
            known_entities=existing_entities,
            period_start=period_start,
            first_event_date=first_event_date,
            period_end=period_end,
            current_ordinary_states=current_ordinary_states,
        )
        response, called = load_or_complete_stage(
            client,
            output_dir=repair_dir,
            name="event_repair",
            system=QUARTER_EVENT_EVENT_REPAIR_SYSTEM,
            payload=payload,
        )
        model_calls += int(called)
        story_response, repair_errors = merge_event_repair(
            story_response=story_response,
            repair_response=response,
            failed_event_ids=event_ids,
            known_entity_ids={str(entity["id"]) for entity in existing_entities},
            approved_event_id_renames=approved_event_id_renames,
        )
        errors.extend(repair_errors)
        if not errors and story_response is not None:
            errors.extend(
                validate_story(
                    story_response,
                    period_start=period_start,
                    period_end=period_end,
                    first_event_date=first_event_date,
                    starting_entities=entities,
                    starting_facts=starting_facts,
                    overview=overview,
                    previous_plan=previous_plan,
                )
            )
        if not errors and story_response is not None:
            final_response = rename_event_ids_in_finalization_response(
                final_response, approved_event_id_renames
            )
            current_review = rename_quality_review_event_ids(
                current_review, approved_event_id_renames
            )
            current_full_review = rename_quality_review_event_ids(
                current_full_review, approved_event_id_renames
            )
            corrected_event_ids = {
                approved_event_id_renames.get(event_id, event_id)
                for event_id in corrected_event_ids
            }
            canonical_plan, annotation_errors = build_canonical_plan(
                story_response, final_response
            )
            errors.extend(annotation_errors)
            if not annotation_errors and canonical_plan is not None:
                accounting = derive_replacement_accounting(
                    final_response, canonical_plan
                )

    if not errors and corrected_event_ids:
        fact_repair_event_ids = expand_fact_repair_scope(
            failed_event_ids=corrected_event_ids,
            story_response=story_response,
            final_response=final_response,
        )
        payload = fact_repair_payload(
            failed_event_ids=fact_repair_event_ids,
            story_response=story_response,
            final_response=final_response,
            canonical_plan=canonical_plan,
            current_facts=current_facts,
            quality_review=current_review,
            primary_subject_ids=primary_subject_ids,
            current_ordinary_states=current_ordinary_states,
        )
        response, called = load_or_complete_stage(
            client,
            output_dir=repair_dir,
            name="fact_repair",
            system=QUARTER_EVENT_FACT_REPAIR_SYSTEM,
            payload=payload,
        )
        model_calls += int(called)
        merged_response, repair_errors = merge_fact_repair(
            story_response=story_response,
            original_response=final_response,
            repair_response=response,
            failed_event_ids=fact_repair_event_ids,
        )
        errors.extend(repair_errors)
        if not errors and merged_response is not None:
            repaired_plan, annotation_errors = build_canonical_plan(
                story_response, merged_response
            )
            errors.extend(annotation_errors)
            if not annotation_errors and repaired_plan is not None:
                repaired_accounting = derive_replacement_accounting(
                    merged_response, repaired_plan
                )
                errors.extend(
                    validate_finalization_response(
                        merged_response,
                        repaired_plan,
                        repaired_accounting,
                        period_start=period_start,
                        period_end=period_end,
                        first_event_date=first_event_date,
                        starting_entities=entities,
                        starting_facts=starting_facts,
                        overview=overview,
                        previous_plan=previous_plan,
                    )
                )
                if not errors:
                    final_response = merged_response
                    canonical_plan = repaired_plan
                    accounting = repaired_accounting

    if not errors:
        review_scope = correction_review_scope(
            review_base_plan,
            canonical_plan,
            review_base_accounting,
            accounting,
        )
        prior_plan_review = current_full_review.get("plan_review")
        if (
            isinstance(prior_plan_review, dict)
            and prior_plan_review.get("passed") is False
        ):
            review_scope["prior_plan_review_reason"] = str(
                prior_plan_review.get("reason") or ""
            )
        if review_scope["plan_changed"]:
            errors.append(
                "targeted correction changed plan-level objects; a complete review is required"
            )
        review_payload = targeted_quality_review_payload(
            canonical_plan,
            current_facts,
            replacement_accounting=accounting,
            primary_subject_ids=primary_subject_ids,
            scope=review_scope,
            overview=overview,
            previous_plan=previous_plan,
            continuity_context={
                "recent_accepted_session_purposes": recent_session_purposes,
                "accepted_user_messages_from_previous_month": recent_history,
            },
            current_ordinary_states=current_ordinary_states,
        )
        review_dir = repair_dir / "repair_review"
        review_dir.mkdir(exist_ok=True)
        response, called = load_or_complete_stage(
            client,
            output_dir=review_dir,
            name="quality_review",
            system=QUARTER_EVENT_QUALITY_REVIEW_SYSTEM,
            payload=review_payload,
        )
        model_calls += int(called)
        errors.extend(
            validate_quality_review(
                response,
                set(review_scope["event_ids"]),
                review_payload,
                expected_fact_rows={
                    (str(row["event_id"]), str(row["fact_key"]))
                    for row in review_scope["review_fact_keys"]
                },
                expected_replacement_rows={
                    (
                        str(row["event_id"]),
                        str(row["fact_key"]),
                        str(row["replaced_fact_ref"]),
                    )
                    for row in review_scope["review_replacement_keys"]
                },
            )
        )
        if not errors:
            merged_review = merge_targeted_quality_review(
                current_full_review,
                response,
                review_scope,
            )
            if not merged_review["passed"]:
                if merged_review["plan_review"]["passed"] is False:
                    errors.append(
                        "quality review after final correction: "
                        + str(merged_review["plan_review"]["reason"])
                    )
                errors.extend(
                    "quality review after final correction: " + str(row["reason"])
                    for field in (
                        "event_reviews",
                        "fact_reviews",
                        "replacement_reviews",
                    )
                    for row in merged_review[field]
                    if row["passed"] is False
                )
            return {
                "story_response": story_response,
                "final_response": final_response,
                "canonical_plan": canonical_plan,
                "accounting": accounting,
                "quality_review": merged_review,
                "errors": errors,
                "model_calls": model_calls,
            }

    return {
        "story_response": story_response,
        "final_response": final_response,
        "canonical_plan": canonical_plan,
        "accounting": accounting,
        "quality_review": current_full_review,
        "errors": errors,
        "model_calls": model_calls,
    }


def human_review_failure_review(findings: list[dict[str, str]]) -> dict[str, Any]:
    """Turn an explicit human finding into the existing fact-repair input shape."""
    event_ids: list[str] = []
    fact_reviews: list[dict[str, Any]] = []
    replacement_reviews: list[dict[str, Any]] = []
    for finding in findings:
        event_id = finding["event_id"]
        fact_key = finding["fact_key"]
        replaced_fact_ref = finding["replaced_fact_ref"]
        if event_id not in event_ids:
            event_ids.append(event_id)
        fact_reviews.append(
            {
                "event_id": event_id,
                "fact_key": fact_key,
                "passed": False,
                "reason": finding["reason"],
            }
        )
        replacement_reviews.append(
            {
                "event_id": event_id,
                "fact_key": fact_key,
                "replaced_fact_ref": replaced_fact_ref,
                "passed": False,
                "reason": finding["reason"],
            }
        )
    return {
        "passed": False,
        "event_reviews": [
            {
                "event_id": event_id,
                "passed": False,
                "reason": next(
                    finding["reason"]
                    for finding in findings
                    if finding["event_id"] == event_id
                ),
            }
            for event_id in event_ids
        ],
        "fact_reviews": fact_reviews,
        "replacement_reviews": replacement_reviews,
    }


def load_human_review_findings(
    path: Path, canonical_plan: dict[str, Any]
) -> tuple[list[dict[str, str]], list[str]]:
    document = load_json(path)
    if set(document) != {"findings"} or not isinstance(document.get("findings"), list):
        return [], ["human review findings must contain exactly a findings list"]
    events = {
        str(event["id"]): event
        for event in (canonical_plan.get("plan") or {}).get("events") or []
        if isinstance(event, dict)
    }
    findings: list[dict[str, str]] = []
    errors: list[str] = []
    expected_fields = {"event_id", "fact_key", "replaced_fact_ref", "reason"}
    for index, finding in enumerate(document["findings"]):
        if not isinstance(finding, dict) or set(finding) != expected_fields:
            errors.append(
                f"human review finding {index} must contain exactly "
                f"{sorted(expected_fields)}"
            )
            continue
        normalized = {key: str(finding[key]) for key in expected_fields}
        event = events.get(normalized["event_id"])
        if event is None:
            errors.append(f"human review finding {index} names an unknown event")
            continue
        fact = next(
            (
                fact
                for fact in event.get("facts_to_establish") or []
                if str(fact.get("key")) == normalized["fact_key"]
            ),
            None,
        )
        if fact is None:
            errors.append(f"human review finding {index} names an unknown fact")
            continue
        valid_refs = {
            *(f"id:{int(fact_id)}" for fact_id in fact.get("supersedes") or []),
            *(f"key:{fact_key}" for fact_key in fact.get("supersedes_fact_keys") or []),
        }
        if normalized["replaced_fact_ref"] not in valid_refs:
            errors.append(
                f"human review finding {index} names a replacement not present in the fact"
            )
            continue
        if not normalized["reason"].strip():
            errors.append(f"human review finding {index} must give a reason")
            continue
        findings.append(normalized)
    if not findings and not errors:
        errors.append("human review findings must contain at least one finding")
    return findings, errors


def load_failed_candidate_findings(
    path: Path, canonical_plan: dict[str, Any]
) -> tuple[list[dict[str, str]], list[str]]:
    """Load approved failed-candidate findings in the supported review-row shapes."""
    document = load_json(path)
    if set(document) != {"findings"} or not isinstance(document.get("findings"), list):
        return [], ["approved findings must contain exactly a findings list"]
    events = {
        str(event["id"]): event
        for event in (canonical_plan.get("plan") or {}).get("events") or []
        if isinstance(event, dict)
    }
    findings: list[dict[str, str]] = []
    errors: list[str] = []
    allowed_fields = (
        {"event_id", "reason"},
        {"event_id", "replacement_event_id", "reason"},
        {"event_id", "fact_key", "reason"},
        {"event_id", "fact_key", "replaced_fact_ref", "reason"},
    )
    seen: set[tuple[str, ...]] = set()
    for index, finding in enumerate(document["findings"]):
        if not isinstance(finding, dict) or set(finding) not in allowed_fields:
            errors.append(
                "approved finding "
                f"{index} must contain exactly one of {sorted(allowed_fields[0])}, "
                f"{sorted(allowed_fields[1])}, or {sorted(allowed_fields[2])}"
                f", or {sorted(allowed_fields[3])}"
            )
            continue
        if any(not isinstance(value, str) for value in finding.values()):
            errors.append(f"approved finding {index} values must be strings")
            continue
        normalized = {key: value.strip() for key, value in finding.items()}
        if not normalized["event_id"]:
            errors.append(f"approved finding {index} must name an event")
            continue
        if not normalized["reason"]:
            errors.append(f"approved finding {index} must give a reason")
            continue
        event = events.get(normalized["event_id"])
        if event is None:
            errors.append(f"approved finding {index} names an unknown event")
            continue
        if "replacement_event_id" in normalized:
            replacement_event_id = normalized["replacement_event_id"]
            if not replacement_event_id:
                errors.append(f"approved finding {index} must name the replacement event")
                continue
            if replacement_event_id == normalized["event_id"]:
                errors.append(f"approved finding {index} must change the event ID")
                continue
            if replacement_event_id in events:
                errors.append(
                    f"approved finding {index} reuses an existing event ID"
                )
                continue
        if "fact_key" in normalized:
            if not normalized["fact_key"]:
                errors.append(f"approved finding {index} must name a fact")
                continue
            fact = next(
                (
                    fact
                    for fact in event.get("facts_to_establish") or []
                    if str(fact.get("key")) == normalized["fact_key"]
                ),
                None,
            )
            if fact is None:
                errors.append(f"approved finding {index} names an unknown fact")
                continue
            if "replaced_fact_ref" in normalized:
                if not normalized["replaced_fact_ref"]:
                    errors.append(f"approved finding {index} must name a replacement")
                    continue
                valid_refs = {
                    *(f"id:{int(fact_id)}" for fact_id in fact.get("supersedes") or []),
                    *(
                        f"key:{fact_key}"
                        for fact_key in fact.get("supersedes_fact_keys") or []
                    ),
                }
                if normalized["replaced_fact_ref"] not in valid_refs:
                    errors.append(
                        f"approved finding {index} names a replacement not present in the fact"
                    )
                    continue
        if "replacement_event_id" in normalized:
            target = ("event", normalized["event_id"])
        elif "replaced_fact_ref" in normalized:
            target = (
                "replacement",
                normalized["event_id"],
                normalized["fact_key"],
                normalized["replaced_fact_ref"],
            )
        elif "fact_key" in normalized:
            target = ("fact", normalized["event_id"], normalized["fact_key"])
        else:
            target = ("event", normalized["event_id"])
        if target in seen:
            errors.append(f"approved finding {index} repeats an earlier target")
            continue
        seen.add(target)
        findings.append(normalized)
    if not findings and not errors:
        errors.append("approved findings must contain at least one finding")
    replacement_ids = [
        finding["replacement_event_id"]
        for finding in findings
        if "replacement_event_id" in finding
    ]
    if len(replacement_ids) != len(set(replacement_ids)):
        errors.append("approved findings must not reuse a replacement event ID")
    return findings, errors


def merge_failed_candidate_findings(
    review: dict[str, Any], findings: list[dict[str, str]]
) -> dict[str, Any]:
    """Mark the approved review rows failed while preserving saved automatic failures."""
    merged = copy.deepcopy(review)
    for finding in findings:
        if "replacement_event_id" in finding:
            field, keys = "event_reviews", ("event_id",)
        elif "replaced_fact_ref" in finding:
            field, keys = (
                "replacement_reviews",
                ("event_id", "fact_key", "replaced_fact_ref"),
            )
        elif "fact_key" in finding:
            field, keys = "fact_reviews", ("event_id", "fact_key")
        else:
            field, keys = "event_reviews", ("event_id",)
        target = tuple(finding[key] for key in keys)
        row = next(
            (
                row
                for row in merged.get(field) or []
                if isinstance(row, dict)
                and tuple(str(row.get(key) or "") for key in keys) == target
            ),
            None,
        )
        if row is None:
            raise ValueError("approved finding does not match a saved quality review row")
        old_reason = str(row.get("reason") or "").strip()
        row["passed"] = False
        row["reason"] = (
            finding["reason"]
            if not old_reason or old_reason == finding["reason"]
            else old_reason + "\n\nApproved finding: " + finding["reason"]
        )
    merged["passed"] = False
    return merged


def approved_event_id_renames(findings: list[dict[str, str]]) -> dict[str, str]:
    return {
        finding["event_id"]: finding["replacement_event_id"]
        for finding in findings
        if "replacement_event_id" in finding
    }


def rename_event_ids_in_finalization_response(
    response: dict[str, Any], event_id_renames: dict[str, str]
) -> dict[str, Any]:
    """Keep fact annotations and same-quarter replacement references aligned."""
    renamed = copy.deepcopy(response)
    for annotation in renamed.get("event_fact_annotations") or []:
        if isinstance(annotation, dict):
            event_id = str(annotation.get("event_id") or "")
            annotation["event_id"] = event_id_renames.get(event_id, event_id)
    for replacement in renamed.get("fact_replacements") or []:
        if not isinstance(replacement, dict):
            continue
        event_id = str(replacement.get("event_id") or "")
        replacement["event_id"] = event_id_renames.get(event_id, event_id)
        replaced_fact = replacement.get("replaced_fact")
        if (
            isinstance(replaced_fact, dict)
            and replaced_fact.get("source") == "this_quarter"
        ):
            earlier_event_id = str(replaced_fact.get("event_id") or "")
            replaced_fact["event_id"] = event_id_renames.get(
                earlier_event_id, earlier_event_id
            )
    return renamed


def rename_quality_review_event_ids(
    review: dict[str, Any], event_id_renames: dict[str, str]
) -> dict[str, Any]:
    renamed = copy.deepcopy(review)
    for field in ("event_reviews", "fact_reviews", "replacement_reviews"):
        for row in renamed.get(field) or []:
            if isinstance(row, dict):
                event_id = str(row.get("event_id") or "")
                row["event_id"] = event_id_renames.get(event_id, event_id)
    return renamed


def run_human_review_correction(
    *,
    client: AzureJsonClient,
    output_dir: Path,
    source_candidate_dir: Path,
    human_review_findings_path: Path,
    source_plan: dict[str, Any],
    story_response: dict[str, Any],
    final_response: dict[str, Any],
    current_facts: list[dict[str, Any]],
    primary_subject_ids: set[str],
    overview: dict[str, Any],
    previous_plan: dict[str, Any],
    recent_session_purposes: list[dict[str, Any]],
    recent_history: list[dict[str, Any]],
    current_ordinary_states: list[dict[str, Any]],
    period_start: date,
    period_end: date,
    first_event_date: date,
    entities: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    checkpoint_identity: str,
) -> dict[str, Any]:
    findings, finding_errors = load_human_review_findings(
        human_review_findings_path, source_plan
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    if finding_errors:
        return {"passed": False, "errors": finding_errors, "model_calls": 0}
    failure_review = human_review_failure_review(findings)
    failed_event_ids = {finding["event_id"] for finding in findings}
    fact_repair_event_ids = expand_fact_repair_scope(
        failed_event_ids=failed_event_ids,
        story_response=story_response,
        final_response=final_response,
    )
    repair_payload = fact_repair_payload(
        failed_event_ids=fact_repair_event_ids,
        story_response=story_response,
        final_response=final_response,
        canonical_plan=source_plan,
        current_facts=current_facts,
        quality_review=failure_review,
        primary_subject_ids=primary_subject_ids,
        current_ordinary_states=current_ordinary_states,
    )
    repair_response, repair_called = load_or_complete_stage(
        client,
        output_dir=output_dir,
        name="fact_repair",
        system=QUARTER_EVENT_FACT_REPAIR_SYSTEM,
        payload=repair_payload,
    )
    merged_response, errors = merge_fact_repair(
        story_response=story_response,
        original_response=final_response,
        repair_response=repair_response,
        failed_event_ids=fact_repair_event_ids,
    )
    model_calls = int(repair_called)
    if errors or merged_response is None:
        return {"passed": False, "errors": errors, "model_calls": model_calls}
    corrected_plan, annotation_errors = build_canonical_plan(story_response, merged_response)
    errors.extend(annotation_errors)
    if corrected_plan is None or annotation_errors:
        return {"passed": False, "errors": errors, "model_calls": model_calls}
    corrected_accounting = derive_replacement_accounting(merged_response, corrected_plan)
    errors.extend(
        validate_finalization_response(
            merged_response,
            corrected_plan,
            corrected_accounting,
            period_start=period_start,
            period_end=period_end,
            first_event_date=first_event_date,
            starting_entities=entities,
            starting_facts=facts,
            overview=overview,
            previous_plan=previous_plan,
        )
    )
    if errors:
        return {"passed": False, "errors": errors, "model_calls": model_calls}
    source_accounting = derive_replacement_accounting(final_response, source_plan)
    review_scope = correction_review_scope(
        source_plan,
        corrected_plan,
        source_accounting,
        corrected_accounting,
    )
    if review_scope["plan_changed"]:
        errors.append("narrow correction changed plan-level objects; use a new full review")
    review_payload = targeted_quality_review_payload(
        corrected_plan,
        current_facts,
        replacement_accounting=corrected_accounting,
        primary_subject_ids=primary_subject_ids,
        scope=review_scope,
        overview=overview,
        previous_plan=previous_plan,
        continuity_context={
            "recent_accepted_session_purposes": recent_session_purposes,
            "accepted_user_messages_from_previous_month": recent_history,
        },
        current_ordinary_states=current_ordinary_states,
    )
    review_dir = output_dir / "human_review_repair"
    review_dir.mkdir(exist_ok=True)
    review, review_called = load_or_complete_stage(
        client,
        output_dir=review_dir,
        name="quality_review",
        system=QUARTER_EVENT_QUALITY_REVIEW_SYSTEM,
        payload=review_payload,
    )
    model_calls += int(review_called)
    errors.extend(
        validate_quality_review(
            review,
            set(review_scope["event_ids"]),
            review_payload,
            expected_fact_rows={
                (str(row["event_id"]), str(row["fact_key"]))
                for row in review_scope["review_fact_keys"]
            },
            expected_replacement_rows={
                (
                    str(row["event_id"]),
                    str(row["fact_key"]),
                    str(row["replaced_fact_ref"]),
                )
                for row in review_scope["review_replacement_keys"]
            },
        )
    )
    source_review_payload = quality_review_payload(
        source_plan,
        current_facts,
        replacement_accounting=derive_replacement_accounting(final_response, source_plan),
        primary_subject_ids=primary_subject_ids,
        overview=overview,
        previous_plan=previous_plan,
        continuity_context={
            "recent_accepted_session_purposes": recent_session_purposes,
            "accepted_user_messages_from_previous_month": recent_history,
        },
        current_ordinary_states=current_ordinary_states,
    )
    source_review_path = source_candidate_dir / "quality_review_response.json"
    if not source_review_path.is_file():
        source_review_path = source_candidate_dir / "consolidated_quality_review_response.json"
    if not source_review_path.is_file():
        errors.append("source candidate has no saved quality review response")
        source_review = {}
    else:
        source_review = load_json(source_review_path)
    source_review_errors = validate_quality_review(
        source_review,
        {str(event["id"]) for event in (source_plan.get("plan") or {}).get("events") or []},
        source_review_payload,
    )
    errors.extend(source_review_errors)
    merged_review = merge_targeted_quality_review(
        source_review,
        review,
        review_scope,
    )
    if merged_review["plan_review"]["passed"] is False:
        errors.append("human review correction: " + str(merged_review["plan_review"]["reason"]))
    errors.extend(
        "human review correction: " + str(row["reason"])
        for field in ("event_reviews", "fact_reviews", "replacement_reviews")
        for row in merged_review[field]
        if row["passed"] is False
    )
    dump_json(output_dir / "candidate_quarter_plan.json", corrected_plan["plan"])
    if not errors and merged_review["passed"]:
        dump_json(
            output_dir / "candidate_replacement_accounting.json",
            {"replacement_accounting": corrected_accounting},
        )
    validation = {
        "passed": not errors and merged_review["passed"],
        "errors": errors,
        "quality_review": merged_review,
        "initial_quality_review": source_review,
        "fact_repair_attempted": True,
        "model_calls": model_calls,
        "counts": {
            "events": len((corrected_plan.get("plan") or {}).get("events") or []),
            "new_entities": len((corrected_plan.get("plan") or {}).get("new_entities") or []),
            "new_facts": sum(
                len(event.get("facts_to_establish") or [])
                for event in (corrected_plan.get("plan") or {}).get("events") or []
                if isinstance(event, dict)
            ),
        },
        "inputs": {
            "starting_checkpoint_sha256": checkpoint_identity,
            "source_candidate": str(source_candidate_dir),
            "human_review_findings_sha256": file_hash(human_review_findings_path),
        },
        "candidate_quarter_plan_sha256": file_hash(output_dir / "candidate_quarter_plan.json"),
    }
    dump_json(output_dir / "validation.json", validation)
    return validation


def merge_fact_repair(
    *,
    story_response: dict[str, Any],
    original_response: dict[str, Any],
    repair_response: dict[str, Any],
    failed_event_ids: set[str],
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    fields = {key for key in repair_response if not key.startswith("_")}
    expected_fields = {"event_fact_annotations", "fact_replacements"}
    if fields != expected_fields:
        return None, [
            f"fact repair response must contain exactly {sorted(expected_fields)}"
        ]
    repair_annotations = repair_response.get("event_fact_annotations")
    repair_replacements = repair_response.get("fact_replacements")
    if not isinstance(repair_annotations, list):
        errors.append("fact repair event_fact_annotations must be a list")
        repair_annotations = []
    if not isinstance(repair_replacements, list):
        errors.append("fact repair fact_replacements must be a list")
        repair_replacements = []
    expected_annotation_fields = {
        "event_id",
        "relevant_existing_fact_ids",
        "facts_to_establish",
    }
    for index, row in enumerate(repair_annotations):
        if not isinstance(row, dict) or set(row) != expected_annotation_fields:
            errors.append(
                f"fact repair event_fact_annotations[{index}] must contain exactly "
                f"{sorted(expected_annotation_fields)}"
            )
            continue
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            errors.append(
                f"fact repair event_fact_annotations[{index}] event_id must be a nonempty string"
            )
        facts = row.get("facts_to_establish")
        if not isinstance(facts, list):
            errors.append(
                f"fact repair event_fact_annotations[{index}] facts_to_establish must be a list"
            )
        relevant_ids = row.get("relevant_existing_fact_ids")
        if (
            not isinstance(relevant_ids, list)
            or any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in relevant_ids
            )
            or len(relevant_ids) != len(set(relevant_ids))
        ):
            errors.append(
                f"fact repair event_fact_annotations[{index}] "
                "relevant_existing_fact_ids must be a list of unique integers"
            )
    repair_ids = [
        str(row.get("event_id"))
        for row in repair_annotations
        if isinstance(row, dict)
    ]
    if len(repair_ids) != len(set(repair_ids)) or set(repair_ids) != failed_event_ids:
        errors.append("fact repair must return every failed event exactly once")
    if any(
        not isinstance(row, dict)
        or str(row.get("event_id")) not in failed_event_ids
        for row in repair_replacements
    ):
        errors.append("fact repair replacements may contain only failed events")
    if errors:
        return None, errors

    repair_by_id = {str(row["event_id"]): row for row in repair_annotations}
    original_by_id = {
        str(row["event_id"]): row
        for row in original_response.get("event_fact_annotations") or []
    }
    missing_original_ids = failed_event_ids - set(original_by_id)
    for event_id in missing_original_ids:
        original_by_id[event_id] = {
            "event_id": event_id,
            "relevant_existing_fact_ids": [],
            "facts_to_establish": [],
        }
    story_event_ids = [
        str(event["id"])
        for event in (story_response.get("story") or {}).get("events") or []
    ]
    merged_annotations = [
        (
            {
                "event_id": event_id,
                "relevant_existing_fact_ids": copy.deepcopy(
                    repair_by_id[event_id]["relevant_existing_fact_ids"]
                ),
                "facts_to_establish": copy.deepcopy(
                    repair_by_id[event_id]["facts_to_establish"]
                ),
            }
            if event_id in failed_event_ids
            else original_by_id[event_id]
        )
        for event_id in story_event_ids
        if event_id in repair_by_id or event_id in original_by_id
    ]
    merged_replacements = [
        row
        for row in original_response.get("fact_replacements") or []
        if str(row.get("event_id")) not in failed_event_ids
    ] + list(repair_replacements)
    return {
        "event_fact_annotations": merged_annotations,
        "fact_replacements": merged_replacements,
    }, []


def run_failed_candidate_correction(
    *,
    client: AzureJsonClient,
    output_dir: Path,
    source_candidate_dir: Path,
    approved_findings_path: Path | None = None,
    starting_facts: list[dict[str, Any]],
    current_facts: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    primary_subject_ids: set[str],
    overview: dict[str, Any],
    previous_plan: dict[str, Any],
    recent_session_purposes: list[dict[str, Any]],
    recent_history: list[dict[str, Any]],
    current_ordinary_states: list[dict[str, Any]],
    period_start: date,
    first_event_date: date,
    period_end: date,
    checkpoint_identity: str,
) -> dict[str, Any]:
    """Correct a failed candidate using saved failures and approved findings."""
    source_candidate_dir = source_candidate_dir.resolve()
    output_dir = output_dir.resolve()
    if source_candidate_dir == output_dir:
        raise ValueError("failed candidate correction must write to a new output directory")
    source_validation_path = source_candidate_dir / "validation.json"
    source_plan_path = source_candidate_dir / "candidate_quarter_plan.json"
    if not source_validation_path.is_file() or not source_plan_path.is_file():
        raise ValueError(
            "failed candidate must contain validation.json and candidate_quarter_plan.json"
        )
    source_validation = load_json(source_validation_path)
    if source_validation.get("passed") is not False:
        raise ValueError("source candidate must be a failed candidate")
    source_plan_document = load_json(source_plan_path)
    (
        reconstructed_story,
        reconstructed_finalization,
        reconstructed_accounting,
        reconstructed_plan,
        source_state_paths,
    ) = load_candidate_state_for_correction(
        source_candidate_dir, source_plan_document
    )

    deterministic_review = deterministic_fact_failure_review(
        source_validation.get("errors") or [], reconstructed_plan
    )
    review_plan = reconstructed_plan
    if deterministic_review is not None:
        review_plan = copy.deepcopy(reconstructed_plan)
        valid_fact_ids = current_fact_ids(current_facts)
        for event in (review_plan.get("plan") or {}).get("events") or []:
            if isinstance(event, dict):
                event["relevant_existing_fact_ids"] = [
                    fact_id
                    for fact_id in event.get("relevant_existing_fact_ids") or []
                    if fact_id in valid_fact_ids
                ]
    review_payload = quality_review_payload(
        review_plan,
        current_facts,
        replacement_accounting=reconstructed_accounting,
        primary_subject_ids=primary_subject_ids,
        overview=overview,
        previous_plan=previous_plan,
        continuity_context={
            "recent_accepted_session_purposes": recent_session_purposes,
            "accepted_user_messages_from_previous_month": recent_history,
        },
        current_ordinary_states=current_ordinary_states,
    )
    review: dict[str, Any] | None = None
    consolidated_review_path = (
        source_candidate_dir / "consolidated_quality_review_response.json"
    )
    if consolidated_review_path.is_file():
        review = load_json(consolidated_review_path)
    else:
        try:
            latest_review_request_path, _ = load_latest_full_quality_review_request(
                source_candidate_dir, reconstructed_plan
            )
            latest_review_response_path = latest_review_request_path.with_name(
                "quality_review_response.json"
            )
            if latest_review_response_path.is_file():
                review = load_json(latest_review_response_path)
        except ValueError:
            pass
    if not isinstance(review, dict):
        review = source_validation.get("quality_review")
    if not isinstance(review, dict):
        response_path = source_candidate_dir / "quality_review_response.json"
        if response_path.is_file():
            review = load_json(response_path)
        else:
            review = deterministic_review
            if review is None:
                raise ValueError(
                    "failed candidate has no saved full quality review response "
                    "or repairable fact validation error"
                )
    review = mark_missing_quality_review_rows_failed(review, review_payload)
    review = normalize_legacy_passing_review_reasons(review)
    correction_review = review
    saved_failed_event_ids = failed_event_review_ids(review)
    saved_failed_fact_event_ids = failed_fact_only_event_ids(review)
    if (
        isinstance(review.get("plan_review"), dict)
        and review["plan_review"].get("passed") is False
        and not (saved_failed_event_ids | saved_failed_fact_event_ids)
    ):
        initial_review = source_validation.get("initial_quality_review")
        if not isinstance(initial_review, dict):
            raise ValueError(
                "failed candidate has a stale plan failure but no saved initial quality review"
            )
        initial_review = normalize_legacy_passing_review_reasons(initial_review)
        initial_failed_event_rows = [
            copy.deepcopy(row)
            for row in initial_review.get("event_reviews") or []
            if isinstance(row, dict) and row.get("passed") is False
        ]
        initial_failed_event_ids = {
            str(row["event_id"])
            for row in initial_failed_event_rows
            if isinstance(row.get("event_id"), str) and row["event_id"]
        }
        if not initial_failed_event_ids:
            raise ValueError(
                "failed candidate has a stale plan failure but no failed event in its initial quality review"
            )
        current_event_ids = {
            str(event["id"])
            for event in (reconstructed_plan.get("plan") or {}).get("events") or []
        }
        unknown_event_ids = initial_failed_event_ids - current_event_ids
        if unknown_event_ids:
            raise ValueError(
                "initial quality review names unknown failed events: "
                + ", ".join(sorted(unknown_event_ids))
            )
        initial_rows_by_id = {
            str(row["event_id"]): row for row in initial_failed_event_rows
        }
        correction_review = copy.deepcopy(review)
        correction_review["event_reviews"] = [
            copy.deepcopy(initial_rows_by_id.get(str(row.get("event_id")), row))
            for row in review.get("event_reviews") or []
        ]
        correction_review["passed"] = False
    review_errors = validate_quality_review(
        correction_review,
        {
            str(event["id"])
            for event in (reconstructed_plan.get("plan") or {}).get("events") or []
        },
        review_payload,
    )
    if review_errors:
        raise ValueError(
            "saved failed review does not match the reconstructed candidate: "
            + "; ".join(review_errors)
        )
    approved_findings: list[dict[str, str]] = []
    if approved_findings_path is not None:
        approved_findings_path = approved_findings_path.resolve()
        approved_findings, finding_errors = load_failed_candidate_findings(
            approved_findings_path, reconstructed_plan
        )
        if finding_errors:
            raise ValueError("; ".join(finding_errors))
        correction_review = merge_failed_candidate_findings(
            correction_review, approved_findings
        )
    if not (failed_event_review_ids(correction_review) | failed_fact_only_event_ids(correction_review)):
        raise ValueError("saved failed review has no event, fact, or replacement row to correct")

    source_record = {
        "version": 1,
        "mode": "failed_candidate_correction",
        "source_candidate": str(source_candidate_dir),
        "source_candidate_plan_sha256": file_hash(source_plan_path),
        "source_validation_sha256": file_hash(source_validation_path),
        "source_state_files": {
            str(path.relative_to(source_candidate_dir)): file_hash(path)
            for path in source_state_paths
        },
    }
    if approved_findings_path is not None:
        source_record["approved_findings"] = {
            "source_path": str(approved_findings_path),
            "sha256": file_hash(approved_findings_path),
            "copy": "approved_correction_findings.json",
        }
    output_dir.mkdir(parents=True, exist_ok=True)
    source_record_path = output_dir / "failed_candidate_source.json"
    provenance_findings_path = output_dir / "approved_correction_findings.json"
    if source_record_path.exists():
        if load_json(source_record_path) != source_record:
            raise ValueError("output directory is tied to a different failed candidate")
        if approved_findings_path is not None and (
            not provenance_findings_path.is_file()
            or file_hash(provenance_findings_path)
            != source_record["approved_findings"]["sha256"]
        ):
            raise ValueError("saved approved findings do not match correction provenance")
    else:
        dump_json(source_record_path, source_record)
        if approved_findings_path is not None:
            provenance_findings_path.write_bytes(approved_findings_path.read_bytes())
            if (
                file_hash(provenance_findings_path)
                != source_record["approved_findings"]["sha256"]
            ):
                raise ValueError("could not save approved findings for correction provenance")
    existing_validation_path = output_dir / "validation.json"
    if existing_validation_path.exists():
        existing_validation = load_json(existing_validation_path)
        if (
            existing_validation.get("correction_mode") == "failed_candidate"
            and existing_validation.get("passed") is True
        ):
            return existing_validation

    correction = run_final_targeted_correction(
        client=client,
        output_dir=output_dir,
        current_story_response=reconstructed_story,
        current_final_response=reconstructed_finalization,
        current_plan=reconstructed_plan,
        current_accounting=reconstructed_accounting,
        current_review=correction_review,
        current_full_review=review,
        current_facts=current_facts,
        starting_facts=starting_facts,
        entities=entities,
        primary_subject_ids=primary_subject_ids,
        overview=overview,
        previous_plan=previous_plan,
        recent_session_purposes=recent_session_purposes,
        recent_history=recent_history,
        current_ordinary_states=current_ordinary_states,
        period_start=period_start,
        first_event_date=first_event_date,
        period_end=period_end,
        repair_dir_name="correction",
        fact_event_ids=failed_fact_only_event_ids(correction_review),
        approved_event_id_renames=approved_event_id_renames(approved_findings),
    )
    errors = list(correction["errors"])
    consolidated_hashes = write_consolidated_candidate_state(
        output_dir,
        story_response=correction["story_response"],
        fact_finalization_response=correction["final_response"],
        replacement_accounting=correction["accounting"],
        canonical_plan=correction["canonical_plan"],
        quality_review=correction["quality_review"],
    )
    corrected_plan = correction["canonical_plan"].get("plan") or {}
    validation = {
        "passed": not errors,
        "errors": errors,
        "correction_mode": "failed_candidate",
        "quality_review": correction["quality_review"],
        "initial_quality_review": review,
        "fact_repair_attempted": bool(failed_fact_only_event_ids(correction_review)),
        "model_calls": correction["model_calls"],
        "counts": {
            "events": len(corrected_plan.get("events") or []),
            "new_entities": len(corrected_plan.get("new_entities") or []),
            "new_facts": sum(
                len(event.get("facts_to_establish") or [])
                for event in corrected_plan.get("events") or []
                if isinstance(event, dict)
            ),
        },
        "inputs": {
            "starting_checkpoint_sha256": checkpoint_identity,
            "source_candidate": str(source_candidate_dir),
            "source_state_files": [
                str(path.relative_to(source_candidate_dir))
                for path in source_state_paths
            ],
            "approved_findings_sha256": (
                file_hash(approved_findings_path)
                if approved_findings_path is not None
                else None
            ),
        },
        "consolidated_artifact_sha256": consolidated_hashes,
    }
    dump_json(output_dir / "validation.json", validation)
    return validation


def previous_calendar_month_sessions(
    history: list[dict[str, Any]], first_event_date: date
) -> list[dict[str, Any]]:
    current_month_start = first_event_date.replace(day=1)
    previous_month_end = current_month_start - timedelta(days=1)
    previous_month_start = previous_month_end.replace(day=1)
    return [
        {
            "id": str(row["id"]),
            "narrative_date": str(row["narrative_date"]),
            "messages": list(row.get("messages") or []),
        }
        for row in history
        if previous_month_start
        <= date.fromisoformat(str(row["narrative_date"])[:10])
        <= previous_month_end
    ]


def validate_plan(
    response: dict[str, Any],
    *,
    period_start: date,
    period_end: date,
    first_event_date: date | None = None,
    starting_entities: list[dict[str, Any]],
    starting_facts: list[dict[str, Any]],
    overview: dict[str, Any],
    previous_plan: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    first_event_date = first_event_date or period_start
    response_fields = {key for key in response if not key.startswith("_")}
    if response_fields != {"plan"}:
        errors.append("response must contain exactly plan")
    plan = response.get("plan")
    if not isinstance(plan, dict):
        errors.append("response must contain a plan object")
        return errors
    if set(plan) != CANONICAL_PLAN_FIELDS:
        errors.append(f"plan must contain exactly {sorted(CANONICAL_PLAN_FIELDS)}")
    if plan.get("period_start") != period_start.isoformat():
        errors.append("plan period_start does not match the requested quarter")
    if plan.get("period_end") != period_end.isoformat():
        errors.append("plan period_end does not match the requested quarter")

    new_entities = plan.get("new_entities")
    events = plan.get("events")
    if not isinstance(new_entities, list):
        errors.append("plan new_entities must be a list")
        new_entities = []
    if not isinstance(events, list):
        errors.append("plan events must be a list")
        events = []

    protected_changes = plan.get("protected_changes")
    if not isinstance(protected_changes, list):
        errors.append("plan protected_changes must be a list")
    for index, change in enumerate(
        protected_changes if isinstance(protected_changes, list) else []
    ):
        if not isinstance(change, dict) or set(change) != PROTECTED_CHANGE_FIELDS:
            errors.append(
                f"protected_changes[{index}] must contain exactly "
                f"{sorted(PROTECTED_CHANGE_FIELDS)}"
            )

    starting_entity_ids = {str(row.get("id")) for row in starting_entities}
    new_entity_ids: set[str] = set()
    introduction_events: dict[str, str] = {}
    for index, entity in enumerate(new_entities):
        if not isinstance(entity, dict):
            errors.append(f"new_entities[{index}] must be an object")
            continue
        if set(entity) != NEW_ENTITY_FIELDS:
            errors.append(
                f"new_entities[{index}] must contain exactly {sorted(NEW_ENTITY_FIELDS)}"
            )
            continue
        for field in NEW_ENTITY_FIELDS:
            if not isinstance(entity[field], str) or not entity[field].strip():
                errors.append(f"new_entities[{index}] {field} must be a nonempty string")
        entity_id = str(entity["id"])
        if entity_id in starting_entity_ids or entity_id in new_entity_ids:
            errors.append(f"duplicate or already-established entity id: {entity_id}")
        new_entity_ids.add(entity_id)
        introduction_events[entity_id] = str(entity["introduced_in_event_id"])

    allowed_entity_ids = starting_entity_ids | new_entity_ids
    starting_fact_ids = {int(row["id"]) for row in starting_facts}
    current_starting_fact_ids = current_fact_ids(starting_facts)
    event_ids: set[str] = set()
    fact_keys: set[str] = {
        str(row["construction_key"])
        for row in starting_facts
        if row.get("construction_key")
    }
    previous_event_ids = {
        str(row.get("id"))
        for row in previous_plan.get("events") or []
        if isinstance(row, dict)
    }
    prior_event_ids: set[str] = set(previous_event_ids)
    prior_fact_keys = set(fact_keys)
    last_event_date: tuple[str, str] | None = None

    event_by_id = {
        str(row.get("id")): row for row in events if isinstance(row, dict)
    }
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            errors.append(f"events[{index}] must be an object")
            continue
        expected = {
            "id",
            "start_date",
            "end_date",
            "subjects",
            "builds_on_event_ids",
            "relevant_existing_fact_ids",
            "what_happens",
            "lasting_outcome",
            "facts_to_establish",
        }
        if set(event) != expected:
            errors.append(f"events[{index}] must contain exactly {sorted(expected)}")
            continue
        event_id = str(event["id"])
        if event_id in event_ids:
            errors.append(f"duplicate event id: {event_id}")
        event_ids.add(event_id)
        try:
            start = date.fromisoformat(str(event["start_date"]))
            end = date.fromisoformat(str(event["end_date"]))
        except ValueError:
            errors.append(f"{event_id}: invalid event date")
            continue
        if start < first_event_date or end > period_end or end < start:
            errors.append(
                f"{event_id}: dates must fall between "
                f"{first_event_date.isoformat()} and {period_end.isoformat()}"
            )
        ordering = (end.isoformat(), start.isoformat())
        if last_event_date is not None and ordering < last_event_date:
            errors.append(f"{event_id}: events are not ordered by completion date")
        last_event_date = ordering

        subjects = {str(value) for value in event.get("subjects") or []}
        unknown_subjects = sorted(subjects - allowed_entity_ids)
        if unknown_subjects:
            errors.append(f"{event_id}: unknown subjects {unknown_subjects}")
        unknown_dependencies = sorted(
            set(map(str, event.get("builds_on_event_ids") or [])) - prior_event_ids
        )
        if unknown_dependencies:
            errors.append(f"{event_id}: builds on non-earlier events {unknown_dependencies}")
        referenced_fact_ids = set(event.get("relevant_existing_fact_ids") or [])
        unknown_facts = sorted(referenced_fact_ids - starting_fact_ids)
        obsolete_facts = sorted(
            referenced_fact_ids & (starting_fact_ids - current_starting_fact_ids)
        )
        if unknown_facts:
            errors.append(
                f"{event_id}: references unknown starting facts {unknown_facts}"
            )
        if obsolete_facts:
            errors.append(
                f"{event_id}: references obsolete starting facts {obsolete_facts}"
            )

        event_fact_keys: set[str] = set()
        facts = event.get("facts_to_establish")
        if not isinstance(facts, list):
            errors.append(f"{event_id}: facts_to_establish must be a list")
            facts = []
        for fact_index, fact in enumerate(facts):
            if not isinstance(fact, dict):
                errors.append(f"{event_id}: fact {fact_index} must be an object")
                continue
            fact_expected = {
                "key",
                "statement",
                "applies_when",
                "subjects",
                "supersedes",
                "supersedes_fact_keys",
            }
            if set(fact) != fact_expected:
                errors.append(
                    f"{event_id}: fact {fact_index} must contain exactly {sorted(fact_expected)}"
                )
                continue
            key = str(fact["key"])
            if key in fact_keys or key in event_fact_keys:
                errors.append(f"{event_id}: duplicate fact key {key}")
            event_fact_keys.add(key)
            fact_subjects = {str(value) for value in fact.get("subjects") or []}
            unknown_fact_subjects = sorted(fact_subjects - allowed_entity_ids)
            if unknown_fact_subjects:
                errors.append(
                    f"{event_id}/{key}: fact has unknown subjects "
                    f"{unknown_fact_subjects}"
                )
            replacement_ids = set(fact.get("supersedes") or [])
            replacement_keys = set(map(str, fact.get("supersedes_fact_keys") or []))
            unknown_replacements = sorted(replacement_ids - starting_fact_ids)
            obsolete_replacements = sorted(
                replacement_ids & (starting_fact_ids - current_starting_fact_ids)
            )
            if unknown_replacements:
                errors.append(
                    f"{event_id}/{key}: supersedes unknown facts "
                    f"{unknown_replacements}"
                )
            if obsolete_replacements:
                errors.append(
                    f"{event_id}/{key}: supersedes obsolete facts "
                    f"{obsolete_replacements}"
                )
            unknown_key_replacements = sorted(
                replacement_keys - prior_fact_keys
            )
            if unknown_key_replacements:
                errors.append(
                    f"{event_id}/{key}: supersedes facts not established earlier "
                    f"{unknown_key_replacements}"
                )
        fact_keys.update(event_fact_keys)
        prior_fact_keys.update(event_fact_keys)
        prior_event_ids.add(event_id)

    for entity_id, introduction_event_id in introduction_events.items():
        event = event_by_id.get(introduction_event_id)
        if event is None:
            errors.append(f"{entity_id}: introduction event does not exist")
        elif entity_id not in set(map(str, event.get("subjects") or [])):
            errors.append(f"{entity_id}: introduction event does not include the entity")
        first_reference = next(
            (
                str(event["id"])
                for event in events
                if isinstance(event, dict)
                and entity_id in set(map(str, event.get("subjects") or []))
            ),
            None,
        )
        if first_reference != introduction_event_id:
            errors.append(
                f"{entity_id}: first referenced in {first_reference}, not its introduction event"
            )

    return errors


def run(args: argparse.Namespace) -> dict[str, Any]:
    starting_checkpoint = resolve_path(str(args.starting_checkpoint))
    overview_path = resolve_path(str(args.overview))
    previous_plan_path = resolve_path(str(args.previous_quarter_plan))
    persona_sheet_path = resolve_path(str(args.persona_sheet))
    generator_context_path = resolve_path(str(args.generator_context))
    output_dir = resolve_path(str(args.output))
    period_start = date.fromisoformat(args.period_start)
    period_end = date.fromisoformat(args.period_end)

    checkpoint_identity = validated_checkpoint_identity(starting_checkpoint)
    checkpoint, starting_state = load_checkpoint(starting_checkpoint)
    accepted_through = date.fromisoformat(str(checkpoint["accepted_through"]))
    if not period_start - timedelta(days=1) <= accepted_through < period_end:
        raise ValueError(
            "starting checkpoint accepted_through must be between "
            f"{(period_start - timedelta(days=1)).isoformat()} and "
            f"{(period_end - timedelta(days=1)).isoformat()}"
        )
    first_event_date = max(period_start, accepted_through + timedelta(days=1))
    entities = starting_state["entities"]
    primary_subject_ids = primary_subject_ids_for_persona(args.persona, entities)
    facts = starting_state["facts"]
    recent_session_purposes = starting_state["session_purposes"][-80:]
    history = starting_state["history"]
    recent_history = previous_calendar_month_sessions(history, first_event_date)
    overview_document = load_json(overview_path)
    overview = (
        overview_document["overview"]
        if isinstance(overview_document.get("overview"), dict)
        else overview_document
    )
    previous_plan_document = load_json(previous_plan_path)
    previous_plan = canonical_plan_for_context(previous_plan_document)
    current_facts = current_starting_facts(facts)
    current_ordinary_states = current_ordinary_states_from_session_purposes(
        starting_state
    )
    source_candidate_arg = getattr(args, "source_candidate", None)
    human_review_findings_arg = getattr(args, "human_review_findings", None)
    failed_source_with_findings = False
    if source_candidate_arg and human_review_findings_arg:
        source_validation_path = (
            resolve_path(str(source_candidate_arg)) / "validation.json"
        )
        if source_validation_path.is_file():
            failed_source_with_findings = (
                load_json(source_validation_path).get("passed") is False
            )
    correction_source_mode = bool(source_candidate_arg) and (
        not bool(human_review_findings_arg) or failed_source_with_findings
    )

    story_payload = {
        "persona": args.persona,
        "persona_sheet": persona_sheet_path.read_text(),
        "generator_context": generator_context_path.read_text(),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "first_date_for_new_events": first_event_date.isoformat(),
        "multi_year_overview": overview,
        "recent_accepted_session_purposes": recent_session_purposes,
        "accepted_user_messages_from_previous_month": recent_history,
        "entity_registry": entities,
        "current_fact_registry": current_facts,
        "current_ordinary_states": current_ordinary_states,
        "completed_previous_quarter_plan": previous_plan,
    }
    prior_failed_validation: dict[str, Any] | None = None
    if output_dir.exists():
        if not output_dir.is_dir():
            raise ValueError(f"output path is not a directory: {output_dir}")
        if correction_source_mode:
            if (output_dir / "validation.json").exists():
                prior_failed_validation = load_json(output_dir / "validation.json")
        elif not any(
            (output_dir / filename).exists()
            for filename in ("story_request.json", "story_system.txt", "story_response.json")
        ):
            raise ValueError(
                "existing output directory has no saved story request, prompt, or response"
            )
        validation_path = output_dir / "validation.json"
        if correction_source_mode:
            resuming_failed_candidate = False
        else:
            if validation_path.exists() and load_json(validation_path).get("passed") is True:
                raise ValueError("output directory already contains a completed plan")
            if (output_dir / "candidate_quarter_plan.json").exists() and not validation_path.exists():
                raise ValueError(
                    "candidate quarter plan without validation is not a resumable run"
                )
            resuming_failed_candidate = (
                (output_dir / "candidate_quarter_plan.json").exists()
                and validation_path.exists()
                and load_json(validation_path).get("passed") is False
            )
            if resuming_failed_candidate:
                prior_failed_validation = load_json(validation_path)
    else:
        output_dir.mkdir(parents=True)
        resuming_failed_candidate = False

    client = AzureJsonClient(model="gpt-5.6-sol", reasoning_effort="high")
    if human_review_findings_arg and not source_candidate_arg:
        raise ValueError(
            "human_review_findings requires source_candidate"
        )
    if source_candidate_arg and human_review_findings_arg:
        source_candidate_dir = resolve_path(str(source_candidate_arg))
        findings_path = resolve_path(str(human_review_findings_arg))
        if source_candidate_dir == output_dir:
            raise ValueError("human review correction must write to a new output directory")
        source_validation_path = source_candidate_dir / "validation.json"
        if not source_validation_path.is_file():
            raise ValueError("source candidate is missing validation.json")
        source_validation = load_json(source_validation_path)
        if source_validation.get("passed") is False:
            return run_failed_candidate_correction(
                client=client,
                output_dir=output_dir,
                source_candidate_dir=source_candidate_dir,
                approved_findings_path=findings_path,
                starting_facts=facts,
                current_facts=current_facts,
                entities=entities,
                primary_subject_ids=primary_subject_ids,
                overview=overview,
                previous_plan=previous_plan,
                recent_session_purposes=recent_session_purposes,
                recent_history=recent_history,
                current_ordinary_states=current_ordinary_states,
                period_start=period_start,
                first_event_date=first_event_date,
                period_end=period_end,
                checkpoint_identity=checkpoint_identity,
            )
        if source_validation.get("passed") is not True:
            raise ValueError("source candidate validation must say whether it passed")
        source_plan = normalize_candidate_plan(
            load_json(source_candidate_dir / "candidate_quarter_plan.json")
        )
        source_story_response = load_json(source_candidate_dir / "story_response.json")
        source_final_response = load_json(
            source_candidate_dir / "fact_finalization_response.json"
        )
        return run_human_review_correction(
            client=client,
            output_dir=output_dir,
            source_candidate_dir=source_candidate_dir,
            human_review_findings_path=findings_path,
            source_plan=source_plan,
            story_response=source_story_response,
            final_response=source_final_response,
            current_facts=current_facts,
            primary_subject_ids=primary_subject_ids,
            overview=overview,
            previous_plan=previous_plan,
            recent_session_purposes=recent_session_purposes,
            recent_history=recent_history,
            current_ordinary_states=current_ordinary_states,
            period_start=period_start,
            period_end=period_end,
            first_event_date=first_event_date,
            entities=entities,
            facts=facts,
            checkpoint_identity=checkpoint_identity,
        )
    if source_candidate_arg:
        return run_failed_candidate_correction(
            client=client,
            output_dir=output_dir,
            source_candidate_dir=resolve_path(str(source_candidate_arg)),
            starting_facts=facts,
            current_facts=current_facts,
            entities=entities,
            primary_subject_ids=primary_subject_ids,
            overview=overview,
            previous_plan=previous_plan,
            recent_session_purposes=recent_session_purposes,
            recent_history=recent_history,
            current_ordinary_states=current_ordinary_states,
            period_start=period_start,
            first_event_date=first_event_date,
            period_end=period_end,
            checkpoint_identity=checkpoint_identity,
        )
    story_response, story_called = load_or_complete_stage(
        client,
        output_dir=output_dir,
        name="story",
        system=QUARTER_EVENT_STORY_SYSTEM,
        payload=story_payload,
    )
    normalized_story_response = normalize_story_event_order(story_response)
    story_errors = validate_story(
        normalized_story_response,
        period_start=period_start,
        period_end=period_end,
        first_event_date=first_event_date,
        starting_entities=entities,
        starting_facts=facts,
        overview=overview,
        previous_plan=previous_plan,
    )
    canonical_story_response = normalized_story_response if not story_errors else None
    final_response: dict[str, Any] | None = None
    canonical_final_response: dict[str, Any] | None = None
    derived_replacement_accounting: list[dict[str, Any]] | None = None
    quality_review: dict[str, Any] | None = None
    initial_quality_review: dict[str, Any] | None = None
    repair_attempted = False
    model_calls = int(story_called)
    errors = list(story_errors)
    if not errors and canonical_story_response is not None:
        finalization_payload = {
            "fixed_story": canonical_story_response["story"],
            "current_fact_registry": current_facts,
            "current_ordinary_states": current_ordinary_states,
        }
        final_response, fact_finalization_called = load_or_complete_stage(
            client,
            output_dir=output_dir,
            name="fact_finalization",
            system=QUARTER_EVENT_FACT_FINALIZATION_SYSTEM,
            payload=finalization_payload,
        )
        model_calls += int(fact_finalization_called)
        canonical_final_response, annotation_errors = build_canonical_plan(
            canonical_story_response, final_response
        )
        errors.extend(annotation_errors)
        if not annotation_errors and canonical_final_response is not None:
            derived_replacement_accounting = derive_replacement_accounting(
                final_response, canonical_final_response
            )
            errors.extend(
                validate_finalization_response(
                    final_response,
                    canonical_final_response,
                    derived_replacement_accounting,
                    period_start=period_start,
                    period_end=period_end,
                    first_event_date=first_event_date,
                    starting_entities=entities,
                    starting_facts=facts,
                    overview=overview,
                    previous_plan=previous_plan,
                )
            )
    if not errors and canonical_final_response is not None:
        review_payload = quality_review_payload(
            canonical_final_response,
            current_facts,
            replacement_accounting=derived_replacement_accounting or [],
            primary_subject_ids=primary_subject_ids,
            overview=overview,
            previous_plan=previous_plan,
            continuity_context={
                "recent_accepted_session_purposes": recent_session_purposes,
                "accepted_user_messages_from_previous_month": recent_history,
            },
            current_ordinary_states=current_ordinary_states,
        )
        quality_review, quality_review_called = load_or_complete_stage(
            client,
            output_dir=output_dir,
            name="quality_review",
            system=QUARTER_EVENT_QUALITY_REVIEW_SYSTEM,
            payload=review_payload,
            allow_saved_response_mismatch=resuming_failed_candidate,
        )
        model_calls += int(quality_review_called)
        review_errors = validate_quality_review(
            quality_review,
            {
                str(event["id"])
                for event in (canonical_final_response.get("plan") or {}).get("events") or []
            },
            review_payload,
        )
        if review_errors:
            errors.extend(review_errors)
        elif quality_review["passed"] is False:
            initial_quality_review = quality_review
            event_repair_ids = failed_event_review_ids(quality_review)
            repair_event_ids = failed_fact_event_ids(quality_review)
            repair_stage_dir = output_dir
            if event_repair_ids:
                repair_attempted = True
                existing_entities = event_repair_entities(entities, canonical_final_response)
                event_repair_payload_value = event_repair_payload(
                    failed_event_ids=event_repair_ids,
                    story_response=canonical_story_response,
                    canonical_plan=canonical_final_response,
                    current_facts=current_facts,
                    quality_review=quality_review,
                    known_entities=existing_entities,
                    period_start=period_start,
                    first_event_date=first_event_date,
                    period_end=period_end,
                    current_ordinary_states=current_ordinary_states,
                )
                event_repair_response, event_repair_called = load_or_complete_stage(
                    client,
                    output_dir=output_dir,
                    name="event_repair",
                    system=QUARTER_EVENT_EVENT_REPAIR_SYSTEM,
                    payload=event_repair_payload_value,
                    allow_saved_response_mismatch=resuming_failed_candidate,
                )
                model_calls += int(event_repair_called)
                merged_story_response, event_repair_errors = merge_event_repair(
                    story_response=canonical_story_response,
                    repair_response=event_repair_response,
                    failed_event_ids=event_repair_ids,
                    known_entity_ids={
                        str(entity["id"])
                        for entity in event_repair_payload_value["existing_entities"]
                    },
                )
                errors.extend(event_repair_errors)
                if not event_repair_errors and merged_story_response is not None:
                    errors.extend(
                        validate_story(
                            merged_story_response,
                            period_start=period_start,
                            period_end=period_end,
                            first_event_date=first_event_date,
                            starting_entities=entities,
                            starting_facts=facts,
                            overview=overview,
                            previous_plan=previous_plan,
                        )
                    )
                if not errors and merged_story_response is not None:
                    canonical_story_response = merged_story_response
                    canonical_final_response, annotation_errors = build_canonical_plan(
                        canonical_story_response, final_response
                    )
                    errors.extend(annotation_errors)
                    if not annotation_errors and canonical_final_response is not None:
                        derived_replacement_accounting = derive_replacement_accounting(
                            final_response, canonical_final_response
                        )
                    repair_stage_dir = output_dir / "event_repair"
                    repair_stage_dir.mkdir(exist_ok=True)

            if not errors and repair_event_ids:
                repair_attempted = True
                repair_event_ids = expand_fact_repair_scope(
                    failed_event_ids=repair_event_ids,
                    story_response=canonical_story_response,
                    final_response=final_response,
                )
                repair_payload = fact_repair_payload(
                    failed_event_ids=repair_event_ids,
                    story_response=canonical_story_response,
                    final_response=final_response,
                    canonical_plan=canonical_final_response,
                    current_facts=current_facts,
                    quality_review=quality_review,
                    primary_subject_ids=primary_subject_ids,
                    current_ordinary_states=current_ordinary_states,
                )
                repair_response, repair_called = load_or_complete_stage(
                    client,
                    output_dir=repair_stage_dir,
                    name="fact_repair",
                    system=QUARTER_EVENT_FACT_REPAIR_SYSTEM,
                    payload=repair_payload,
                    allow_saved_response_mismatch=resuming_failed_candidate,
                )
                model_calls += int(repair_called)
                merged_response, repair_errors = merge_fact_repair(
                    story_response=canonical_story_response,
                    original_response=final_response,
                    repair_response=repair_response,
                    failed_event_ids=repair_event_ids,
                )
                errors.extend(repair_errors)
                if not repair_errors and merged_response is not None:
                    repaired_plan, annotation_errors = build_canonical_plan(
                        canonical_story_response, merged_response
                    )
                    errors.extend(annotation_errors)
                    if not annotation_errors and repaired_plan is not None:
                        repaired_accounting = derive_replacement_accounting(
                            merged_response, repaired_plan
                        )
                        errors.extend(
                            validate_finalization_response(
                                merged_response,
                                repaired_plan,
                                repaired_accounting,
                                period_start=period_start,
                                period_end=period_end,
                                first_event_date=first_event_date,
                                starting_entities=entities,
                                starting_facts=facts,
                                overview=overview,
                                previous_plan=previous_plan,
                            )
                        )
                    if not errors and repaired_plan is not None:
                        final_response = merged_response
                        canonical_final_response = repaired_plan
                        derived_replacement_accounting = repaired_accounting
                        repaired_review_payload = quality_review_payload(
                            canonical_final_response,
                            current_facts,
                            replacement_accounting=derived_replacement_accounting,
                            primary_subject_ids=primary_subject_ids,
                            overview=overview,
                            previous_plan=previous_plan,
                            continuity_context={
                                "recent_accepted_session_purposes": recent_session_purposes,
                                "accepted_user_messages_from_previous_month": recent_history,
                            },
                            current_ordinary_states=current_ordinary_states,
                            event_ids=repair_event_ids,
                        )
                        repair_review_dir = repair_stage_dir / "repair_review"
                        repair_review_dir.mkdir(exist_ok=True)
                        correction_review, repair_review_called = load_or_complete_stage(
                            client,
                            output_dir=repair_review_dir,
                            name="quality_review",
                            system=QUARTER_EVENT_QUALITY_REVIEW_SYSTEM,
                            payload=repaired_review_payload,
                            allow_saved_response_mismatch=resuming_failed_candidate,
                        )
                        model_calls += int(repair_review_called)
                        saved_correction_review = correction_review
                        correction_review = restrict_quality_review(
                            saved_correction_review, repair_event_ids
                        )
                        repaired_review_errors = validate_quality_review(
                            correction_review,
                            repair_event_ids,
                            repaired_review_payload,
                        )
                        errors.extend(repaired_review_errors)
                        if not repaired_review_errors:
                            quality_review = merge_quality_reviews(
                                initial_quality_review,
                                correction_review,
                                repair_event_ids,
                            )
                            if resuming_failed_candidate and not errors:
                                final_correction = run_final_targeted_correction(
                                    client=client,
                                    output_dir=output_dir,
                                    current_story_response=canonical_story_response,
                                    current_final_response=merged_response,
                                    current_plan=repaired_plan,
                                    current_accounting=repaired_accounting,
                                    current_review=saved_correction_review,
                                    current_full_review=quality_review,
                                    current_facts=current_facts,
                                    starting_facts=facts,
                                    entities=entities,
                                    primary_subject_ids=primary_subject_ids,
                                    overview=overview,
                                    previous_plan=previous_plan,
                                    recent_session_purposes=recent_session_purposes,
                                    recent_history=recent_history,
                                    current_ordinary_states=current_ordinary_states,
                                    period_start=period_start,
                                    first_event_date=first_event_date,
                                    period_end=period_end,
                                )
                                model_calls += int(final_correction["model_calls"])
                                canonical_story_response = final_correction["story_response"]
                                final_response = final_correction["final_response"]
                                canonical_final_response = final_correction["canonical_plan"]
                                derived_replacement_accounting = final_correction["accounting"]
                                quality_review = final_correction["quality_review"]
                                errors.extend(final_correction["errors"])
                            if not quality_review["passed"] and not errors:
                                if quality_review["plan_review"]["passed"] is False:
                                    errors.append(
                                        "quality review after fact repair: "
                                        + str(quality_review["plan_review"]["reason"])
                                    )
                                errors.extend(
                                    "quality review after fact repair: " + str(row["reason"])
                                    for field in (
                                        "event_reviews",
                                        "fact_reviews",
                                        "replacement_reviews",
                                    )
                                    for row in quality_review[field]
                                    if row["passed"] is False
                                )
            else:
                if quality_review["plan_review"]["passed"] is False:
                    errors.append(
                        "quality review: " + str(quality_review["plan_review"]["reason"])
                    )
                errors.extend(
                    "quality review: " + str(row["reason"])
                    for field in (
                        "event_reviews",
                        "fact_reviews",
                        "replacement_reviews",
                    )
                    for row in quality_review[field]
                    if row["passed"] is False
                )
    if canonical_final_response is not None:
        dump_json(
            output_dir / "candidate_quarter_plan.json",
            canonical_final_response["plan"],
        )
    replacement_accounting = (
        {"replacement_accounting": derived_replacement_accounting}
        if derived_replacement_accounting is not None
        else None
    )
    if not errors and quality_review is not None and quality_review.get("passed") is True:
        dump_json(
            output_dir / "candidate_replacement_accounting.json",
            replacement_accounting,
        )
    consolidated_artifact_hashes: dict[str, str] = {}
    if (
        canonical_story_response is not None
        and final_response is not None
        and canonical_final_response is not None
        and derived_replacement_accounting is not None
        and quality_review is not None
    ):
        consolidated_artifact_hashes = write_consolidated_candidate_state(
            output_dir,
            story_response=canonical_story_response,
            fact_finalization_response=final_response,
            replacement_accounting=derived_replacement_accounting,
            canonical_plan=canonical_final_response,
            quality_review=quality_review,
        )
    final_plan = (
        canonical_final_response.get("plan")
        if canonical_final_response is not None
        and isinstance(canonical_final_response.get("plan"), dict)
        else {}
    )
    validation = {
        "passed": not errors,
        "errors": errors,
        "story_validation": {"passed": not story_errors, "errors": story_errors},
        "quality_review": quality_review,
        "initial_quality_review": initial_quality_review,
        "fact_repair_attempted": repair_attempted,
        "model_calls": model_calls,
        "counts": {
            "events": len(final_plan.get("events") or []),
            "new_entities": len(
                final_plan.get("new_entities") or []
            ),
            "new_facts": sum(
                len(event.get("facts_to_establish") or [])
                for event in final_plan.get("events") or []
                if isinstance(event, dict)
            ),
        },
        "inputs": {
            "accepted_through": accepted_through.isoformat(),
            "first_date_for_new_events": first_event_date.isoformat(),
            "overview_sha256": file_hash(overview_path),
            "starting_checkpoint_sha256": checkpoint_identity,
            "previous_quarter_plan_sha256": file_hash(previous_plan_path),
            "generator_context_sha256": file_hash(generator_context_path),
        },
        "candidate_quarter_plan_sha256": (
            file_hash(output_dir / "candidate_quarter_plan.json")
            if canonical_final_response is not None
            else None
        ),
        "candidate_replacement_accounting_sha256": (
            file_hash(output_dir / "candidate_replacement_accounting.json")
            if (output_dir / "candidate_replacement_accounting.json").exists()
            else None
        ),
        "consolidated_artifact_sha256": consolidated_artifact_hashes,
    }
    dump_json(output_dir / "validation.json", validation)
    return validation


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", required=True)
    parser.add_argument("--persona-sheet", type=Path, required=True)
    parser.add_argument("--generator-context", type=Path, required=True)
    parser.add_argument("--overview", type=Path, required=True)
    parser.add_argument("--starting-checkpoint", type=Path, required=True)
    parser.add_argument("--previous-quarter-plan", type=Path, required=True)
    parser.add_argument("--period-start", required=True)
    parser.add_argument("--period-end", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--source-candidate",
        type=Path,
        help="candidate to correct into a new output",
    )
    parser.add_argument(
        "--human-review-findings",
        type=Path,
        help="approved findings for a passed or failed source candidate",
    )
    parser.add_argument("--confirm-paid-call", action="store_true")
    args = parser.parse_args()
    if not args.confirm_paid_call:
        parser.error("this command makes a paid model call; pass --confirm-paid-call")
    validation = run(args)
    print(json.dumps(validation, indent=2))
    if not validation["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
