"""Render structured construction inputs as deterministic plain text."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any


def _label(value: str) -> str:
    words = re.sub(r"[_-]+", " ", value).strip()
    return words[:1].upper() + words[1:]


def _scalar(value: Any) -> str:
    if value is None:
        return "None"
    if value is True:
        return "True"
    if value is False:
        return "False"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _append_value(lines: list[str], value: Any, *, indent: int) -> None:
    prefix = " " * indent
    if isinstance(value, dict):
        if not value:
            lines.append(f"{prefix}None")
            return
        for key, child in value.items():
            label = _label(str(key))
            if isinstance(child, (dict, list)):
                lines.append(f"{prefix}{label}:")
                _append_value(lines, child, indent=indent + 2)
            else:
                text = _scalar(child)
                if "\n" in text:
                    lines.append(f"{prefix}{label}:")
                    lines.extend(f"{prefix}  {part}" for part in text.splitlines())
                else:
                    lines.append(f"{prefix}{label}: {text}")
        return

    if isinstance(value, list):
        if not value:
            lines.append(f"{prefix}None")
            return
        for index, child in enumerate(value, start=1):
            if isinstance(child, (dict, list)):
                lines.append(f"{prefix}Item {index}:")
                _append_value(lines, child, indent=indent + 2)
            else:
                text = _scalar(child)
                if "\n" in text:
                    lines.append(f"{prefix}-")
                    lines.extend(f"{prefix}  {part}" for part in text.splitlines())
                else:
                    lines.append(f"{prefix}- {text}")
        return

    lines.append(f"{prefix}{_scalar(value)}")


def render_model_input(payload: dict[str, Any]) -> str:
    """Render top-level input sections in their supplied order."""
    if not isinstance(payload, dict):
        raise ValueError("model input must be an object")
    lines: list[str] = []
    for key, value in payload.items():
        if lines:
            lines.append("")
        labels = {
            "developments_for_this_week": "ACCEPTED EVENTS DURING THESE DATES, INCLUDING THEIR LATER OUTCOMES",
            "accepted_developments_after_this_window": "ACCEPTED OUTCOMES LATER IN THIS QUARTER",
            "previous_accepted_contacts_with_shared_subject_ids": "EARLIER CONTACTS ABOUT THE SAME PEOPLE OR SUBJECTS",
            "other_fixed_contacts_this_week": "OTHER FIXED CONTACTS THIS WEEK (READ ONLY)",
            "accepted_outcome_and_contact_evidence": "ACCEPTED OUTCOME AND CONTACT EVIDENCE FOR THIS CORRECTION",
        }
        lines.append(f"# {labels.get(str(key), _label(str(key)))}")
        _append_value(lines, value, indent=0)
    return "\n".join(lines).rstrip() + "\n"


def _contact_date(row: dict[str, Any]) -> date:
    raw = str(row.get("date") or row.get("narrative_date") or "")
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return date.fromisoformat(raw[:10])


def _week_label(value: date) -> str:
    start = value - timedelta(days=value.weekday())
    return f"Week of {start.isoformat()}"


def _render_chronological_contacts(
    lines: list[str],
    rows: list[dict[str, Any]],
    *,
    detail_field: str,
    person_name: str,
) -> None:
    current_week = ""
    for row in sorted(rows, key=lambda item: (_contact_date(item), str(item.get("session_id") or ""))):
        week = _week_label(_contact_date(row))
        if week != current_week:
            if current_week:
                lines.append("")
            lines.append(week)
            current_week = week
        when = str(row.get("date") or row.get("narrative_date") or "")
        detail = " ".join(str(row.get(detail_field) or "").split())
        subjects = ", ".join(str(value) for value in row.get("subject_ids") or [])
        suffix = f" Subjects: {subjects}." if subjects else ""
        lines.append(
            f"- On {when}, {person_name} already contacted the assistant about: "
            f"{detail}{suffix}"
        )


def render_story_planner_input(payload: dict[str, Any]) -> str:
    """Render the story planner's small, chronological input without JSON-shaped rows."""
    if not isinstance(payload, dict):
        raise ValueError("story planner input must be an object")

    labels = {
        "requested_dates": "DATES TO PLAN",
        "world_description": "PERSON AND ESTABLISHED WORLD",
        "developments_for_this_week": "CHANGES THAT MUST FINISH DURING THESE DATES",
        "developments_finishing_later": "CHANGES ALREADY UNDERWAY THAT FINISH LATER",
        "accepted_developments_after_this_window": "LATER ACCEPTED CHANGES THAT THIS WEEK MUST NOT CONTRADICT",
        "current_conditions": "WHAT IS CURRENTLY TRUE",
        "current_ordinary_states": "OTHER CONTINUING CONDITIONS",
        "known_entities": "KNOWN PEOPLE, PROJECTS, COMPANIES, PRODUCTS, AND PLACES",
        "new_entities_that_may_first_appear_this_week": "NEW NAMES ALLOWED BY THIS WEEK'S ACCEPTED CHANGES",
        "open_threads": "THINGS THAT WERE STILL UNRESOLVED BEFORE THIS WEEK",
        "protected_changes": "CHANGES THAT MUST NOT HAPPEN YET",
        "supported_external_actions": "SUPPORTED EXTERNAL ACTIONS",
        "existing_app_items": "EXISTING APP ITEMS",
        "upcoming_calendar_events": "UPCOMING CALENDAR EVENTS",
    }
    omitted = {
        "persona",
        "narrative_timezone",
        "older_contacts_that_already_happened",
        "recently_finished_contacts",
    }
    section_order = (
        "requested_dates",
        "developments_for_this_week",
        "developments_finishing_later",
        "open_threads",
        "accepted_developments_after_this_window",
        "protected_changes",
        "current_status_precedence",
        "current_conditions",
        "current_ordinary_states",
        "historical_identity_and_world_context",
        "historical_entity_identity_and_history",
        "new_entities_that_may_first_appear_this_week",
        "supported_external_actions",
        "existing_app_items",
        "upcoming_calendar_events",
    )
    lines = [
        f"PERSONA: {payload.get('persona', '')}",
        f"TIME ZONE: {payload.get('narrative_timezone', '')}",
    ]
    ordered_keys = [key for key in section_order if key in payload]
    ordered_keys.extend(
        key for key in payload if key not in omitted and key not in section_order
    )
    for key in ordered_keys:
        value = payload[key]
        if key in omitted:
            continue
        lines.extend(["", labels.get(key, _label(str(key)).upper())])
        if key == "open_threads":
            lines.append(
                "Use this section only to avoid contradicting earlier history. It is "
                "not a list of work for this week. Do not invent a reply, corrected "
                "draft, revised code change, completed repair, acceptance, or result "
                "because an item appears here. It is normal for these items to remain "
                "unchanged."
            )
        _append_value(lines, value, indent=0)

    lines.extend(
        [
            "",
            "WHAT ALREADY HAPPENED",
            "Each item below describes a contact that already happened. Do not reuse "
            "or paraphrase its situation, source material, question, or result. "
            "Continue only an unresolved matter listed above, and only when genuinely "
            "new information arrives.",
        ]
    )
    person_name = _label(str(payload.get("persona") or "this person"))
    _render_chronological_contacts(
        lines,
        list(payload.get("older_contacts_that_already_happened") or []),
        detail_field="contact_purpose",
        person_name=person_name,
    )
    _render_chronological_contacts(
        lines,
        list(payload.get("recently_finished_contacts") or []),
        detail_field="accepted_contact_details",
        person_name=person_name,
    )
    lines.extend(
        [
            "",
            "Now plan only the requested dates. Keep the accepted changes and "
            "current conditions above true. Use earlier contacts only to avoid "
            "repeating them.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"
