"""Present authoring inputs by purpose without dropping or rewriting their data."""

from __future__ import annotations

import json
from typing import Any


Section = tuple[str, dict[str, Any]]


def _sections(payload: dict[str, Any], layout: list[tuple[str, tuple[str, ...]]]) -> list[Section]:
    remaining = dict(payload)
    sections = []
    for title, keys in layout:
        fields = {key: remaining.pop(key) for key in keys if key in remaining}
        if fields:
            sections.append((title, fields))
    if remaining:
        sections.append(("Additional supplied information", remaining))
    return sections


def _render(sections: list[Section]) -> str:
    return "\n\n".join(
        f"## {title}\n\n```json\n{json.dumps(fields, ensure_ascii=False, sort_keys=True, indent=2)}\n```"
        for title, fields in sections
    ) + "\n"


def render_planner_input(payload: dict[str, Any]) -> str:
    return _render(_sections(payload, [
        ("Person, date, and requested number of ideas", ("checkpoint",)),
        ("Prior work", ("prior_work",)),
        ("Available actions", ("tools",)),
        ("All current facts", ("current_facts",)),
    ]))


def render_evidence_input(payload: dict[str, Any]) -> str:
    return _render(_sections(payload, [
        ("Person, date, and requested number of decisions", ("checkpoint",)),
        ("Fixed proposals", ("frozen_proposal",)),
        ("Selected facts and exact source messages", ("selected_facts",)),
        ("Later facts about the same subjects", ("later_facts_about_same_subjects",)),
        ("All current facts", ("all_current_facts",)),
    ]))


def _authoring_sections(payload: dict[str, Any]) -> list[Section]:
    remaining = dict(payload)
    person = {key: remaining.pop(key) for key in ("persona", "evaluation_date") if key in remaining}
    plan = remaining.pop("planned_task", None)
    sections = []
    if isinstance(plan, dict):
        plan = dict(plan)
        brief = {key: plan.pop(key) for key in ("new_situation", "requested_work", "work_items") if key in plan}
        if brief:
            person["planned_task"] = brief
        elif not plan:
            person["planned_task"] = {}
        if person:
            sections.append(("Person and present work", person))
        if plan:
            sections.append(("Approved results: correctness information, not request wording", {"planned_task": plan}))
    else:
        if "planned_task" in payload:
            person["planned_task"] = plan
        if person:
            sections.append(("Person and present work", person))
    sections.extend(_sections(remaining, [
        ("Source evidence and later information", ("selected_fact_evidence", "current_facts_that_may_change_the_meaning")),
        ("Available tools and record catalog", ("tools", "current_readable_app_state")),
        ("Response version", ("authoring_schema_version",)),
    ]))
    return sections


def render_authoring_input(payload: dict[str, Any]) -> str:
    original = payload.get("original_authoring_input")
    if not isinstance(original, dict):
        return _render(_authoring_sections(payload))
    sections = [
        (f"Original input: {title}", {"original_authoring_input": fields})
        for title, fields in _authoring_sections(original)
    ]
    if not sections:
        sections.append(("Original input", {"original_authoring_input": original}))
    sections.extend(_sections(
        {key: value for key, value in payload.items() if key != "original_authoring_input"},
        [
            ("Saved authoring response", ("previous_authoring",)),
            ("Allowed correction fields", ("allowed_correction_fields",)),
            ("Failure to correct", ("failure_to_fix",)),
        ],
    ))
    return _render(sections)
