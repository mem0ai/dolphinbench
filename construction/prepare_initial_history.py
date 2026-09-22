"""Prepare a staged repair of a short existing history without model calls."""

from __future__ import annotations

import argparse
import ast
import copy
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import tiktoken
import yaml

from construction.checkpoints import (
    data_hash,
    save_checkpoint,
    validated_checkpoint_identity,
)
from construction.construction_io import file_hash
from construction.runtime_operations import execute_operations, operation_errors


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_PERSONAS = {"alex", "riley"}
_ENTITY_KINDS = [
    "person", "pet", "organization", "team", "project", "product", "service",
    "tool", "document", "channel", "place", "routine", "event", "account", "other",
]
_APP_EFFECT_CATEGORIES = {
    "incoming_record",
    "reported_change",
    "requested_action",
    "unsupported_request",
}
_REQUEST_DECISIONS = {"supported_action", "unsupported_request", "no_app_action"}
_APPROVED_CORRECTIONS = Path("corrections") / "approved_text_corrections.json"
_ENTITY_CORRECTIONS = Path("corrections") / "approved_entity_inventory_corrections.json"
_FACT_SUBJECT_CORRECTIONS = Path("corrections") / "approved_fact_subject_corrections.json"
_FACT_SUBJECT_SOURCE_RESPONSE = "responses/existing_fact_subjects.json"


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text()) or {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _write_yaml(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False))


def _source_paths(persona: str) -> dict[str, Path]:
    if persona not in ALLOWED_PERSONAS:
        raise ValueError("persona must be alex or riley")
    base = ROOT / "registry" / "personas" / persona
    return {
        "life_sim": base / "life_sim.yaml",
        "facts": base / "facts.yaml",
        "persona_sheet": base / "persona_sheet.md",
    }


def _sessions(document: dict[str, Any], *, label: str) -> list[dict[str, Any]]:
    sessions = document.get("sessions")
    if not isinstance(sessions, list) or not all(isinstance(row, dict) for row in sessions):
        raise ValueError(f"{label} must contain a sessions list")
    return sessions


def _facts(document: dict[str, Any], *, label: str) -> list[dict[str, Any]]:
    facts = document.get("facts")
    if not isinstance(facts, list) or not all(isinstance(row, dict) for row in facts):
        raise ValueError(f"{label} must contain a facts list")
    return facts


def _parse_datetime(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO datetime string")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{label} is not a valid ISO datetime: {value!r}") from error


def _validate_source_history(sessions: list[dict[str, Any]], facts: list[dict[str, Any]]) -> None:
    ids: list[str] = []
    previous: datetime | None = None
    for index, session in enumerate(sessions):
        session_id = session.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(f"session {index} is missing a non-empty id")
        for field in ("label", "session_type"):
            if not isinstance(session.get(field), str) or not session[field]:
                raise ValueError(f"session {session_id} is missing a non-empty {field}")
        messages = session.get("messages")
        if not isinstance(messages, list) or not messages or not all(isinstance(message, str) and message for message in messages):
            raise ValueError(f"session {session_id} must contain non-empty message strings")
        current = _parse_datetime(session.get("narrative_date"), label=f"session {session_id} narrative_date")
        if previous is not None and current < previous:
            raise ValueError(f"sessions are not chronological at {session_id}")
        previous = current
        ids.append(session_id)
    if len(ids) != len(set(ids)):
        raise ValueError("session IDs must be unique")
    fact_ids = [fact.get("id") for fact in facts]
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in fact_ids) or len(fact_ids) != len(set(fact_ids)):
        raise ValueError("source fact IDs must be unique positive integers")
    known_ids = set(ids)
    for fact in facts:
        for field in ("source_session_ids", "related_history_session_ids"):
            values = fact.get(field) or []
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ValueError(f"fact {fact.get('id')} {field} must be a list of session IDs")
            if missing := sorted(set(values) - known_ids):
                raise ValueError(f"fact {fact.get('id')} {field} has unknown sessions: {missing}")


def _source_data(persona: str) -> tuple[dict[str, Path], list[dict[str, Any]], list[dict[str, Any]]]:
    paths = _source_paths(persona)
    if missing := [str(path) for path in paths.values() if not path.is_file()]:
        raise ValueError("required source files are missing: " + ", ".join(missing))
    sessions = _sessions(_load_yaml(paths["life_sim"]), label=str(paths["life_sim"]))
    facts = _facts(_load_yaml(paths["facts"]), label=str(paths["facts"]))
    _validate_source_history(sessions, facts)
    return paths, sessions, facts


def _approved_corrections(run: Path, persona: str) -> dict[str, Any]:
    path = run / _APPROVED_CORRECTIONS
    if not path.is_file():
        return {"message_corrections": [], "fact_statement_corrections": []}
    value = _load_json(path)
    expected_keys = {"version", "persona", "message_corrections", "fact_statement_corrections"}
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError(f"approved corrections have invalid top-level fields: {path}")
    if value["version"] != 1 or value["persona"] != persona:
        raise ValueError(f"approved corrections have the wrong version or persona: {path}")
    for field in ("message_corrections", "fact_statement_corrections"):
        if not isinstance(value[field], list):
            raise ValueError(f"approved corrections {field} must be a list")
    return value


def _apply_fact_statement_corrections(
    run: Path, persona: str, facts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows = _approved_corrections(run, persona)["fact_statement_corrections"]
    corrected = copy.deepcopy(facts)
    by_id = {int(fact["id"]): fact for fact in corrected}
    seen: set[int] = set()
    expected_keys = {"fact_id", "exact_statement", "replacement_statement", "reason"}
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected_keys:
            raise ValueError("approved fact corrections have invalid fields")
        fact_id = row["fact_id"]
        if isinstance(fact_id, bool) or not isinstance(fact_id, int) or fact_id not in by_id:
            raise ValueError(f"approved fact correction has unknown fact ID: {fact_id!r}")
        if fact_id in seen:
            raise ValueError(f"approved fact correction duplicates fact {fact_id}")
        exact, replacement, reason = row["exact_statement"], row["replacement_statement"], row["reason"]
        if not all(isinstance(text, str) and text for text in (exact, replacement, reason)) or exact == replacement:
            raise ValueError(f"approved fact correction {fact_id} has invalid text")
        if by_id[fact_id].get("statement") != exact:
            raise ValueError(f"approved fact correction does not match fact {fact_id}")
        by_id[fact_id]["statement"] = replacement
        seen.add(fact_id)
    return corrected


def _apply_message_corrections(
    run: Path, persona: str, sessions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows = _approved_corrections(run, persona)["message_corrections"]
    corrected = copy.deepcopy(sessions)
    by_id = {str(session["id"]): session for session in corrected}
    seen: set[tuple[str, int]] = set()
    expected_keys = {"session_id", "message_index", "exact_text", "replacement_text", "reason"}
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected_keys:
            raise ValueError("approved message corrections have invalid fields")
        session_id, index = row["session_id"], row["message_index"]
        if (
            not isinstance(session_id, str)
            or session_id not in by_id
            or isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < len(by_id[session_id]["messages"])
        ):
            raise ValueError("approved message correction has an invalid session or message index")
        if (key := (session_id, index)) in seen:
            raise ValueError(f"approved message correction duplicates {session_id}[{index}]")
        exact, replacement, reason = row["exact_text"], row["replacement_text"], row["reason"]
        if not all(isinstance(text, str) and text for text in (exact, replacement, reason)) or exact == replacement:
            raise ValueError(f"approved message correction {session_id}[{index}] has invalid text")
        if by_id[session_id]["messages"][index] != exact:
            raise ValueError(f"approved message correction does not match {session_id}[{index}]")
        by_id[session_id]["messages"][index] = replacement
        seen.add(key)
    return corrected


def _history_tokens(sessions: list[dict[str, Any]]) -> int:
    encoder = tiktoken.get_encoding("o200k_base")
    return sum(len(encoder.encode(message)) for session in sessions for message in session["messages"])


def _manifest(persona: str, paths: dict[str, Path], sessions: list[dict[str, Any]], facts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "persona": persona,
        "source_paths": {name: str(path.relative_to(ROOT)) for name, path in paths.items()},
        "source_sha256": {name: file_hash(path) for name, path in paths.items()},
        "session_count": len(sessions),
        "fact_count": len(facts),
        "user_message_tokens_o200k_base": _history_tokens(sessions),
        "original_first_date": sessions[0]["narrative_date"],
        "original_last_date": sessions[-1]["narrative_date"],
    }


def _linked_fact_ids(session_id: str, facts: list[dict[str, Any]]) -> list[int]:
    return [int(fact["id"]) for fact in facts if session_id in (fact.get("source_session_ids") or []) or session_id in (fact.get("related_history_session_ids") or [])]


def _session_view(session: dict[str, Any], facts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": session["id"], "narrative_date": session["narrative_date"],
        "label": session["label"], "session_type": session["session_type"],
        "messages": session["messages"], "linked_existing_fact_ids": _linked_fact_ids(str(session["id"]), facts),
    }


def _request(stage: str, system: str, response_schema: dict[str, Any], input_value: dict[str, Any]) -> dict[str, Any]:
    return {"stage": stage, "model": "gpt-5.5", "system": system, "response_schema": response_schema, "input": input_value}


def _problem_schema(session_ids: list[str]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["problems", "messages_checked"], "properties": {"problems": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["session_id", "message_index", "exact_text", "evidence_session_ids", "what_is_wrong", "information_that_must_remain"],
        "properties": {
            "session_id": {"enum": session_ids}, "message_index": {"type": "integer", "minimum": 0},
            "exact_text": {"type": "string", "minLength": 1},
            "evidence_session_ids": {"type": "array", "minItems": 1, "items": {"enum": session_ids}},
            "what_is_wrong": {"type": "string", "minLength": 1},
            "information_that_must_remain": {"type": "string", "minLength": 1},
        },
    }}, "messages_checked": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["session_id", "message_index"],
        "properties": {"session_id": {"enum": session_ids}, "message_index": {"type": "integer", "minimum": 0}},
    }}}}


def _rewrite_schema(session_ids: list[str]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["sessions"], "properties": {"sessions": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["session_id", "messages"],
        "properties": {"session_id": {"enum": session_ids}, "messages": {"type": "array", "items": {"type": "string", "minLength": 1}}},
    }}}}


def _entity_schema(session_ids: list[str]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["entities"], "properties": {"entities": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "name", "kind", "aliases", "introduced_in", "reason"],
        "properties": {
            "id": {"type": "string", "minLength": 1}, "name": {"type": "string", "minLength": 1},
            "kind": {"enum": _ENTITY_KINDS}, "aliases": {"type": "array", "items": {"type": "string"}},
            "introduced_in": {"enum": session_ids}, "reason": {"type": "string", "minLength": 1},
        },
    }}}}


def _fact_subject_schema(fact_ids: list[int], entity_ids: list[str]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["fact_subjects"], "properties": {"fact_subjects": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["fact_id", "subjects"],
        "properties": {"fact_id": {"enum": fact_ids}, "subjects": {"type": "array", "minItems": 1, "items": {"enum": entity_ids}}},
    }}}}


def _new_fact_schema(*, session_ids: list[str], entity_ids: list[str]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["facts"], "properties": {"facts": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["id", "statement", "applies_when", "source_session_ids", "related_history_session_ids", "subjects", "supersedes"],
        "properties": {
            "id": {"type": "integer", "minimum": 1}, "statement": {"type": "string", "minLength": 1}, "applies_when": {"type": "string"},
            "source_session_ids": {"type": "array", "minItems": 1, "items": {"enum": session_ids}},
            "related_history_session_ids": {"type": "array", "items": {"enum": session_ids}},
            "subjects": {"type": "array", "minItems": 1, "items": {"enum": entity_ids}},
            "supersedes": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        },
    }}}}


def _app_effect_schema(*, session_ids: list[str], tool_names: list[str]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["message_effects"], "properties": {"message_effects": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["session_id", "message_index", "effects"],
        "properties": {
            "session_id": {"enum": session_ids},
            "message_index": {"type": "integer", "minimum": 0},
            "effects": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["category", "evidence_quote", "reason", "tool"],
                "properties": {
                    "category": {"enum": sorted(_APP_EFFECT_CATEGORIES)},
                    "evidence_quote": {"type": "string", "minLength": 1},
                    "reason": {"type": "string", "minLength": 1},
                    "tool": {"enum": [*tool_names, None]},
                },
            }},
        },
    }}}}


MESSAGE_PROBLEMS_SYSTEM = """Read the supplied history and list only message problems that the supplied history proves. Return only JSON that matches the schema. It is valid to return an empty problems list.

Inspect every supplied message once. Include every session/message pair in messages_checked, including clean messages. messages_checked uses zero-based message_index. problems contains only defective messages.

List a message only when at least one of these is true:
- It contradicts information established earlier and does not say that the situation changed.
- The user refers to themself by name or as another person when they mean "I".
- It talks about writing or generating a dataset instead of speaking as the user.
- It is so unclear or exaggerated that a normal reader cannot understand what the user means.
- A person, company, product, project, place, or other name appears repeatedly even though the history never explains what it is or how it relates to the user.
- A message speaks as if a later event or outcome has already happened. Legitimate future plans and deadlines are not problems.

Do not rewrite any message. Do not flag a normal request or a personal writing style. If a message clearly says that something changed, do not flag the new information merely because it differs from an earlier session.

Copy exact_text from the supplied message without changing it. message_index starts at zero. evidence_session_ids must contain the session IDs that prove the problem. When the problem is visible in the message itself, include that message's own session ID. Use only supplied session IDs."""

REWRITE_MESSAGES_SYSTEM = """Rewrite only the messages with accepted problems. Return only JSON that matches the schema.

Each item in targets contains:
- target_session: the original session whose message needs repair;
- accepted_problems: the exact problems to fix and the information that must remain;
- previous_two_sessions and next_two_sessions: nearby conversation context;
- evidence_sessions: the sessions that prove each problem;
- linked_protected_existing_facts: accepted facts that the rewrite must not change.

Return one row for every target_session and no other session. Keep the same session_id, message count, and message order. Copy every unflagged message exactly. Rewrite each flagged message so it reads as the user speaking to their assistant at that session's date.

Follow each accepted problem literally. Preserve every item in information_that_must_remain, every protected fact, and the meaning of any request or action. If a problem identifies knowledge of a later outcome, remove that later knowledge and write only what the user could know at the session's date. A real future plan or deadline may remain. If a problem identifies a mistaken name or other contradiction, apply the explicit correction stated in that problem.

Do not invent an event, fact, person, request, action, or explanation. Do not mention datasets, generation, rewriting, or repairs. Do not change text merely to impose a different writing style."""

ENTITY_INVENTORY_SYSTEM = """Identify the people and things that later facts and new sessions must refer to consistently. Return only JSON that matches the schema.

Include a person, pet, organization, team, project, product, service, tool, document, channel, place, routine, event, account, or other identifiable thing when either of these is true:
- it appears more than once and later sessions need to recognize it as the same thing;
- a protected fact needs it in order to say who or what that fact is about.

Read every protected fact before finishing. The returned entities must be sufficient to describe the subjects of every protected fact. A planned document or event still counts when a protected fact is about whether it exists, has started, or will happen.

Do not combine two things when they can differ in owner, time period, or state. For example, one person's standing practice and a later company policy are separate entities even when they concern the same activity.

introduced_in must be the earliest supplied session that names or clearly identifies the entity. A session that plans or announces something comes before the session where it happens, so use the earlier planning session. The persona sheet may establish the user's identity, but it is not a conversation session and cannot be used as introduced_in.

Do not create an entity for an incidental unnamed place, channel, tool, or event that appears once and is not needed by a protected fact. When the source never gives something a proper name but it must remain identifiable, use a plain descriptive name without presenting it as an official name. Put only names or phrases actually used in the supplied persona sheet, sessions, or facts in aliases. Do not use pronouns such as I, me, he, she, or they as aliases.

Use one short, stable lowercase ID for each entity. Do not create separate entities for two names that clearly refer to the same thing.

Do not write facts, rewrites, app actions, or app records."""

EXISTING_FACT_SUBJECTS_SYSTEM = """For each protected fact, identify the people and things that fact is actually about. Return only JSON that matches the schema.

Return one row for every protected fact. Keep its numeric fact ID unchanged. Choose only IDs from accepted_entities.

Read the complete fact, including applies_when, source_session_ids, and related_history_session_ids. Use those session IDs to find and read the complete messages in referenced_sessions before choosing the entities.

Select an entity when the fact describes that entity's state, preference, ownership, responsibility, relationship, schedule, history, or change over time. A relationship fact normally needs every person or organization in that relationship. A fact about a named project, service, document, rule, routine, or event should include that thing when the fact directly describes it.

Use the smallest complete set of entities. Do not select an entity merely because it appears in a source or related-history session, supplied evidence for the fact, was present when someone recorded the fact, or marks a date before or after the fact. Do not include a meeting, channel, document, policy, project, or person unless the fact itself describes something about it. applies_when explains when the fact is valid; an entity mentioned only as a time boundary in applies_when is not automatically a subject.

Do not use an entity that was introduced later to classify an earlier fact unless the earlier fact explicitly plans or describes that same future entity.

Every fact must have at least one entity. If removing an entity would leave the fact with the same subject and meaning, leave that entity out.

Do not add, remove, combine, split, or rewrite facts. Do not rewrite entities, messages, app actions, or app records."""

MISSING_FACTS_SYSTEM = """Read the complete conversation history in chronological order. Identify information that later sessions must know in order to avoid contradicting this history. Return only JSON that matches the schema.

The supplied protected_existing_facts are already accepted. Do not repeat, rewrite, delete, or alter them. Add a fact only when the history clearly establishes something that is not already expressed by those accepted facts. Examples include a lasting preference, relationship, responsibility, decision, plan, recurring practice, or the current state of a person, project, product, document, or account. Do not turn a passing observation or a completed one-time action into a fact unless it changes what remains true afterward.

Use only names, quantities, technical terms, and relationships stated by the supplied sessions or accepted facts. Do not add a cause merely because one event happened after another. If an accepted fact already states part of a sentence, return only the information that is still missing.

Each returned fact must state one truth that could change without necessarily changing another returned fact. Use the next numeric IDs after the protected facts, in order. source_session_ids must directly establish the fact. related_history_session_ids may identify earlier or later supplied sessions needed to understand it. subjects must contain only the accepted entities that the fact is actually about.

When a later session changes an earlier fact about the same property of the same subject, return both facts in chronological order and put the earlier fact's ID in the later fact's supersedes list. Use supersedes only when the earlier fact would otherwise give the wrong current answer. A true dated event or past state remains historically true and is not superseded merely because something happened later.

Do not combine a lasting fact with a temporary emotion, impression, or passing mood. Return only the part that future sessions need in order to remain consistent.

Do not repair messages, invent information, describe app operations, or create app records."""

IDENTIFY_APP_EFFECTS_SYSTEM = """Read every supplied user message in chronological order and identify only app-related effects that the message itself establishes. Return only JSON that matches the schema.

Return one message_effects row for every supplied session/message pair, in the same order as the supplied sessions and messages. Use zero-based message_index. Use an empty effects list when the message has no app-related effect.

An effect must be one of these four categories:
- incoming_record: The message clearly refers to an email, calendar event, pull request, runbook entry, or other record that needs to exist for the assistant to read or act on it. tool must name the supplied tool through which that record would be available. This category only identifies that a record is needed; it does not create the record.
- reported_change: The message says that an app-visible change already happened. tool must name a supplied tool that performs that kind of app change. This category records an observed result; it does not claim that the assistant performed it.
- requested_action: The user asks the assistant to take an app action that one supplied tool can perform. tool must name that supplied tool. Do not include an action merely because the user describes a plan, asks for advice, asks for drafting in chat, or says that someone else will do it.
- unsupported_request: The user asks for an external action, but no supplied tool can perform it or the message lacks information that no supplied read tool could provide. Set tool to null for this category.

Every effect must copy evidence_quote exactly from the one supplied message named by its row. reason must be a short plain-English explanation of why the message belongs in that category. For incoming_record, reported_change, and requested_action, name only a tool listed in supplied_tools. Set tool to null for unsupported_request.

Do not invent app records, record IDs, record fields, tool arguments, tool calls, completed actions, dates, recipients, or state. Do not rewrite messages, facts, or entities. This stage only classifies what the existing history says and stops before record creation or replay."""


def prepare(*, persona: str, out: Path) -> None:
    """Hash immutable inputs and write only the first message-problem request."""
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"refusing to overwrite non-empty preparation directory: {out}")
    paths, sessions, facts = _source_data(persona)
    _write_json(out / "source_manifest.json", _manifest(persona, paths, sessions, facts))
    _write_json(out / "requests" / "01_find_message_problems.json", _request(
        "find_message_problems", MESSAGE_PROBLEMS_SYSTEM, _problem_schema([str(session["id"]) for session in sessions]),
        {"persona": persona, "persona_sheet": paths["persona_sheet"].read_text(), "protected_existing_facts": facts,
         "sessions": [_session_view(session, facts) for session in sessions]},
    ))


def _validate_schema(value: Any, schema: dict[str, Any], *, location: str = "$") -> None:
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{location}: value is not allowed by the response schema")
    wanted = schema.get("type")
    kinds = {"object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
             "integer": isinstance(value, int) and not isinstance(value, bool), "number": isinstance(value, (int, float)) and not isinstance(value, bool)}
    if wanted is not None and not any(kinds.get(kind, False) for kind in (wanted if isinstance(wanted, list) else [wanted])):
        raise ValueError(f"{location}: wrong type for the response schema")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        raise ValueError(f"{location}: string is shorter than the response schema permits")
    if isinstance(value, int) and not isinstance(value, bool) and value < schema.get("minimum", value):
        raise ValueError(f"{location}: integer is below the response schema minimum")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"{location}: array is shorter than the response schema permits")
        for index, item in enumerate(value):
            if "items" in schema:
                _validate_schema(item, schema["items"], location=f"{location}[{index}]")
    if isinstance(value, dict):
        if missing := [key for key in schema.get("required", []) if key not in value]:
            raise ValueError(f"{location}: missing required response fields: {missing}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and (extras := sorted(set(value) - set(properties))):
            raise ValueError(f"{location}: unexpected response fields: {extras}")
        for key, item in value.items():
            if key in properties:
                _validate_schema(item, properties[key], location=f"{location}.{key}")


def _verify_manifest(persona: str, run: Path, paths: dict[str, Path]) -> dict[str, Any]:
    manifest_path = run / "source_manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"missing source manifest: {manifest_path}")
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("persona") != persona:
        raise ValueError("source manifest persona does not match")
    if manifest.get("source_sha256") != {name: file_hash(path) for name, path in paths.items()}:
        raise ValueError("source files changed after preparation; prepare a new run")
    return manifest


def _accepted_response(run: Path, name: str, stage: str) -> dict[str, Any]:
    accepted_path, request_path = run / "accepted" / name, run / "requests" / name
    if not accepted_path.is_file():
        raise ValueError(f"missing accepted file: {accepted_path}")
    if not request_path.is_file():
        raise ValueError(f"accepted file has no saved request: {accepted_path}")
    request, response = _load_json(request_path), _load_json(accepted_path)
    if not isinstance(request, dict) or request.get("stage") != stage or not isinstance(request.get("response_schema"), dict):
        raise ValueError(f"saved request has the wrong stage or schema: {request_path}")
    if not isinstance(response, dict):
        raise ValueError(f"accepted file must contain a JSON object: {accepted_path}")
    _validate_schema(response, request["response_schema"])
    return response


def _accepted_stage(name: str) -> str | None:
    fixed = {"01_find_message_problems.json": "find_message_problems", "02_rewrite_messages.json": "rewrite_messages",
             "03_entity_inventory.json": "entity_inventory", "04_existing_fact_subjects.json": "existing_fact_subjects",
             "05_missing_facts.json": "missing_facts", "06_identify_app_effects.json": "identify_app_effects"}
    return fixed.get(name)


def _validate_accepted_directory(run: Path) -> None:
    accepted = run / "accepted"
    if not accepted.exists():
        return
    if not accepted.is_dir():
        raise ValueError(f"accepted path is not a directory: {accepted}")
    for path in accepted.iterdir():
        if not path.is_file() or path.suffix != ".json" or (stage := _accepted_stage(path.name)) is None:
            raise ValueError(f"accepted contains an unknown staged record: {path}")
        _accepted_response(run, path.name, stage)


def _validate_problems(value: dict[str, Any], sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    problems = value.get("problems")
    if not isinstance(problems, list):
        raise ValueError("message problems must contain a problems list")
    by_id = {str(session["id"]): session for session in sessions}
    found: set[tuple[str, int]] = set()
    for problem in problems:
        session_id, index = problem.get("session_id"), problem.get("message_index")
        if not isinstance(session_id, str) or session_id not in by_id or isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(by_id[session_id]["messages"]):
            raise ValueError("message problem has an invalid session or message index")
        if problem.get("exact_text") != by_id[session_id]["messages"][index]:
            raise ValueError(f"message problem exact_text does not match {session_id}[{index}]")
        evidence = problem.get("evidence_session_ids")
        if not isinstance(evidence, list) or not evidence or len(evidence) != len(set(evidence)) or set(evidence) - set(by_id):
            raise ValueError("message problem has invalid evidence session IDs")
        if (key := (session_id, index)) in found:
            raise ValueError(f"message problem duplicates {session_id}[{index}]")
        found.add(key)
    if "messages_checked" in value:
        checked_rows = value["messages_checked"]
        if not isinstance(checked_rows, list):
            raise ValueError("messages_checked must be a list")
        expected = {(str(session["id"]), index) for session in sessions for index, _ in enumerate(session["messages"])}
        checked: set[tuple[str, int]] = set()
        for row in checked_rows:
            if not isinstance(row, dict) or set(row) != {"session_id", "message_index"}:
                raise ValueError("messages_checked rows must contain only session_id and message_index")
            session_id, index = row["session_id"], row["message_index"]
            if not isinstance(session_id, str) or session_id not in by_id or isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(by_id[session_id]["messages"]):
                raise ValueError("messages_checked has an invalid session or message index")
            if (key := (session_id, index)) in checked:
                raise ValueError(f"messages_checked duplicates {session_id}[{index}]")
            checked.add(key)
        if checked != expected:
            raise ValueError("messages_checked must contain every source session/message pair exactly once")
    return problems


def _rewrite_input(persona: str, sheet: str, problems: list[dict[str, Any]], sessions: list[dict[str, Any]], facts: list[dict[str, Any]]) -> dict[str, Any]:
    by_id, positions = {str(session["id"]): session for session in sessions}, {str(session["id"]): index for index, session in enumerate(sessions)}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for problem in problems:
        grouped.setdefault(str(problem["session_id"]), []).append(problem)
    targets = []
    for session in sessions:
        session_id = str(session["id"])
        if session_id not in grouped:
            continue
        index = positions[session_id]
        evidence_ids = sorted({item for problem in grouped[session_id] for item in problem["evidence_session_ids"]}, key=positions.__getitem__)
        relevant = {session_id, *evidence_ids}
        targets.append({
            "accepted_problems": grouped[session_id], "target_session": _session_view(session, facts),
            "previous_two_sessions": [_session_view(item, facts) for item in sessions[max(0, index - 2):index]],
            "next_two_sessions": [_session_view(item, facts) for item in sessions[index + 1:index + 3]],
            "evidence_sessions": [_session_view(by_id[item], facts) for item in evidence_ids],
            "linked_protected_existing_facts": [fact for fact in facts if relevant.intersection(fact.get("source_session_ids") or []) or relevant.intersection(fact.get("related_history_session_ids") or [])],
        })
    return {"persona": persona, "persona_sheet": sheet, "targets": targets}


def _validate_rewrites(value: dict[str, Any], problems: list[dict[str, Any]], sessions: list[dict[str, Any]]) -> dict[str, list[str]]:
    rows = value.get("sessions")
    if not isinstance(rows, list):
        raise ValueError("message rewrites must contain a sessions list")
    source = {str(session["id"]): session for session in sessions}
    flagged: dict[str, set[int]] = {}
    for problem in problems:
        flagged.setdefault(str(problem["session_id"]), set()).add(int(problem["message_index"]))
    result: dict[str, list[str]] = {}
    for row in rows:
        session_id, messages = row.get("session_id"), row.get("messages")
        if session_id in result or session_id not in flagged:
            raise ValueError("message rewrites must contain every flagged session exactly once and no others")
        if not isinstance(messages, list) or len(messages) != len(source[session_id]["messages"]) or not all(isinstance(message, str) and message for message in messages):
            raise ValueError(f"message rewrite for {session_id} changes the required message list")
        if any(message != source[session_id]["messages"][index] for index, message in enumerate(messages) if index not in flagged[session_id]):
            raise ValueError(f"message rewrite for {session_id} changes unflagged content")
        result[session_id] = messages
    if set(result) != set(flagged) or len(rows) != len(flagged):
        raise ValueError("message rewrites must contain every flagged session exactly once and no others")
    return result


def _load_repaired_sessions(
    run: Path,
    persona: str,
    source_document: dict[str, Any],
    source_sessions: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    problems = _validate_problems(_accepted_response(run, "01_find_message_problems.json", "find_message_problems"), source_sessions)
    rewrites = _validate_rewrites(_accepted_response(run, "02_rewrite_messages.json", "rewrite_messages"), problems, source_sessions)
    repaired_document = copy.deepcopy(source_document)
    repaired = _sessions(repaired_document, label="source life_sim.yaml")
    for session in repaired:
        if messages := rewrites.get(str(session["id"])):
            session["messages"] = messages
    repaired = _apply_message_corrections(run, persona, repaired)
    repaired_document["sessions"] = repaired
    path = run / "repaired" / "life_sim.yaml"
    if path.exists():
        if _load_yaml(path) != repaired_document:
            raise ValueError("repaired life_sim.yaml does not match the approved message rewrites")
    else:
        _write_yaml(path, repaired_document)
    if _load_yaml(path) != repaired_document:
        raise ValueError("repaired life_sim.yaml does not match the approved message rewrites")
    _validate_source_history(repaired, facts)
    for source, current in zip(source_sessions, repaired):
        if source["id"] != current["id"] or source["narrative_date"] != current["narrative_date"]:
            raise ValueError("repaired sessions must preserve source IDs, order, and narrative dates")
    return repaired


def _validate_entities(value: dict[str, Any], session_ids: set[str]) -> list[dict[str, Any]]:
    entities, found = value.get("entities"), set()
    if not isinstance(entities, list):
        raise ValueError("entity inventory must contain an entities list")
    for entity in entities:
        entity_id = entity.get("id")
        if not isinstance(entity_id, str) or not entity_id or entity_id in found or entity.get("introduced_in") not in session_ids:
            raise ValueError("accepted entities must have unique IDs and session introductions")
        found.add(entity_id)
    return entities


def _apply_entity_inventory_corrections(
    run: Path, persona: str, entities: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    path = run / _ENTITY_CORRECTIONS
    if not path.is_file():
        return copy.deepcopy(entities)
    value = _load_json(path)
    expected_top = {
        "version", "persona", "introduced_in_corrections", "alias_removals",
        "entity_removals", "entity_additions",
    }
    if not isinstance(value, dict) or set(value) != expected_top:
        raise ValueError(f"approved entity corrections have invalid top-level fields: {path}")
    if value["version"] != 1 or value["persona"] != persona:
        raise ValueError(f"approved entity corrections have the wrong version or persona: {path}")
    for field in expected_top - {"version", "persona"}:
        if not isinstance(value[field], list):
            raise ValueError(f"approved entity corrections {field} must be a list")

    corrected = copy.deepcopy(entities)
    by_id = {str(entity["id"]): entity for entity in corrected}
    seen_updates: set[str] = set()
    for row in value["introduced_in_corrections"]:
        if not isinstance(row, dict) or set(row) != {"entity_id", "exact_introduced_in", "replacement_introduced_in", "reason"}:
            raise ValueError("approved introduced_in corrections have invalid fields")
        entity_id = row["entity_id"]
        if entity_id not in by_id or entity_id in seen_updates:
            raise ValueError(f"approved introduced_in correction has unknown or duplicate entity: {entity_id!r}")
        if by_id[entity_id].get("introduced_in") != row["exact_introduced_in"]:
            raise ValueError(f"approved introduced_in correction does not match {entity_id}")
        if not isinstance(row["replacement_introduced_in"], str) or not isinstance(row["reason"], str) or not row["reason"]:
            raise ValueError(f"approved introduced_in correction has invalid text: {entity_id}")
        by_id[entity_id]["introduced_in"] = row["replacement_introduced_in"]
        seen_updates.add(entity_id)

    seen_aliases: set[str] = set()
    for row in value["alias_removals"]:
        if not isinstance(row, dict) or set(row) != {"entity_id", "aliases", "reason"}:
            raise ValueError("approved alias removals have invalid fields")
        entity_id, aliases = row["entity_id"], row["aliases"]
        if entity_id not in by_id or entity_id in seen_aliases:
            raise ValueError(f"approved alias removal has unknown or duplicate entity: {entity_id!r}")
        if not isinstance(aliases, list) or not aliases or len(aliases) != len(set(aliases)) or not all(isinstance(alias, str) and alias for alias in aliases):
            raise ValueError(f"approved alias removal has invalid aliases: {entity_id}")
        if missing := [alias for alias in aliases if alias not in by_id[entity_id].get("aliases", [])]:
            raise ValueError(f"approved alias removal does not match {entity_id}: {missing}")
        if not isinstance(row["reason"], str) or not row["reason"]:
            raise ValueError(f"approved alias removal has no reason: {entity_id}")
        by_id[entity_id]["aliases"] = [alias for alias in by_id[entity_id]["aliases"] if alias not in aliases]
        seen_aliases.add(entity_id)

    removed: set[str] = set()
    for row in value["entity_removals"]:
        if not isinstance(row, dict) or set(row) != {"entity_id", "exact_name", "reason"}:
            raise ValueError("approved entity removals have invalid fields")
        entity_id = row["entity_id"]
        if entity_id not in by_id or entity_id in removed or by_id[entity_id].get("name") != row["exact_name"]:
            raise ValueError(f"approved entity removal does not match {entity_id!r}")
        if not isinstance(row["reason"], str) or not row["reason"]:
            raise ValueError(f"approved entity removal has no reason: {entity_id}")
        removed.add(entity_id)
    corrected = [entity for entity in corrected if entity["id"] not in removed]

    existing_ids = {str(entity["id"]) for entity in corrected}
    for row in value["entity_additions"]:
        if not isinstance(row, dict) or set(row) != {"entity", "reason"} or not isinstance(row["entity"], dict):
            raise ValueError("approved entity additions have invalid fields")
        entity = copy.deepcopy(row["entity"])
        if entity.get("id") in existing_ids or not isinstance(row["reason"], str) or not row["reason"]:
            raise ValueError(f"approved entity addition is duplicate or has no reason: {entity.get('id')!r}")
        corrected.append(entity)
        existing_ids.add(str(entity.get("id")))
    return corrected


def _phrase_occurs(phrase: str, text: str) -> bool:
    folded = phrase.casefold()
    left = r"(?<![a-z0-9_])" if folded[0].isalnum() else ""
    right = r"(?![a-z0-9_])" if folded[-1].isalnum() else ""
    if folded.startswith("#"):
        right = r"(?![a-z0-9_-])"
    return re.search(left + re.escape(folded) + right, text.casefold()) is not None


def _validate_entity_source_grounding(
    entities: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    persona_sheet: str,
) -> None:
    positions = {str(session["id"]): index for index, session in enumerate(sessions)}
    session_text = {
        str(session["id"]): str(session.get("label", "")) + "\n" + "\n".join(session["messages"])
        for session in sessions
    }
    all_text = "\n".join([
        persona_sheet,
        *session_text.values(),
        *[
            str(fact.get("statement", "")) + "\n" + str(fact.get("applies_when", ""))
            for fact in facts
        ],
    ])
    for entity in entities:
        entity_id = str(entity["id"])
        aliases = entity.get("aliases")
        if not isinstance(aliases, list) or len(aliases) != len(set(aliases)) or not all(isinstance(alias, str) and alias for alias in aliases):
            raise ValueError(f"entity {entity_id} aliases must be unique non-empty strings")
        if unsupported := [alias for alias in aliases if not _phrase_occurs(alias, all_text)]:
            raise ValueError(f"entity {entity_id} has aliases absent from the supplied history and facts: {unsupported}")
        introduced = str(entity["introduced_in"])
        earlier = [
            session_id
            for session_id, text in session_text.items()
            if positions[session_id] < positions[introduced]
            and any(_phrase_occurs(alias, text) for alias in aliases)
        ]
        if earlier:
            raise ValueError(f"entity {entity_id} has an exact alias before introduced_in: {earlier[0]}")


def _protected_fact_input(facts: list[dict[str, Any]], sessions: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {str(session["id"]): session for session in sessions}
    referenced = {session_id for fact in facts for field in ("source_session_ids", "related_history_session_ids") for session_id in (fact.get(field) or [])}
    return {"protected_existing_facts": facts, "referenced_sessions": [_session_view(by_id[session_id], facts) for session_id in by_id if session_id in referenced]}


def _validate_fact_subjects(value: dict[str, Any], facts: list[dict[str, Any]], entity_ids: set[str]) -> dict[int, list[str]]:
    rows, expected, result = value.get("fact_subjects"), {int(fact["id"]) for fact in facts}, {}
    if not isinstance(rows, list):
        raise ValueError("fact subjects must contain a fact_subjects list")
    for row in rows:
        fact_id, subjects = row.get("fact_id"), row.get("subjects")
        if fact_id in result or fact_id not in expected or not isinstance(subjects, list) or not subjects or len(subjects) != len(set(subjects)) or not all(isinstance(item, str) and item in entity_ids for item in subjects):
            raise ValueError("fact subjects must cover protected facts with unique known entities")
        result[fact_id] = subjects
    if set(result) != expected or len(rows) != len(expected):
        raise ValueError("fact-subject rows must cover every protected fact exactly once")
    return result


def _apply_fact_subject_corrections(
    run: Path,
    persona: str,
    subjects: dict[int, list[str]],
    entity_ids: set[str],
) -> dict[int, list[str]]:
    """Apply exact, run-local replacements to an already validated raw mapping."""
    path = run / _FACT_SUBJECT_CORRECTIONS
    if not path.is_file():
        return copy.deepcopy(subjects)

    value = _load_json(path)
    expected_top = {"version", "persona", "source_response", "corrections"}
    if not isinstance(value, dict) or set(value) != expected_top:
        raise ValueError(f"approved fact-subject corrections have invalid top-level fields: {path}")
    if value["version"] != 1 or value["persona"] != persona:
        raise ValueError(f"approved fact-subject corrections have the wrong version or persona: {path}")
    if value["source_response"] != _FACT_SUBJECT_SOURCE_RESPONSE:
        raise ValueError(f"approved fact-subject corrections have the wrong source response: {path}")
    rows = value["corrections"]
    if not isinstance(rows, list):
        raise ValueError(f"approved fact-subject corrections must contain a corrections list: {path}")

    corrected = copy.deepcopy(subjects)
    seen: set[int] = set()
    expected_keys = {"fact_id", "exact_subjects", "replacement_subjects", "reason"}
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected_keys:
            raise ValueError("approved fact-subject correction has invalid fields")
        fact_id = row["fact_id"]
        if isinstance(fact_id, bool) or not isinstance(fact_id, int) or fact_id not in subjects:
            raise ValueError(f"approved fact-subject correction has unknown fact ID: {fact_id!r}")
        if fact_id in seen:
            raise ValueError(f"approved fact-subject correction duplicates fact {fact_id}")

        exact_subjects = row["exact_subjects"]
        replacement_subjects = row["replacement_subjects"]
        reason = row["reason"]
        if exact_subjects != subjects[fact_id]:
            raise ValueError(f"approved fact-subject correction does not match fact {fact_id}")
        if (
            not isinstance(replacement_subjects, list)
            or not replacement_subjects
            or not all(isinstance(entity_id, str) for entity_id in replacement_subjects)
            or len(replacement_subjects) != len(set(replacement_subjects))
            or not all(entity_id in entity_ids for entity_id in replacement_subjects)
        ):
            raise ValueError(f"approved fact-subject correction has invalid replacement subjects for fact {fact_id}")
        if not isinstance(reason, str) or not reason:
            raise ValueError(f"approved fact-subject correction has no reason for fact {fact_id}")
        corrected[fact_id] = copy.deepcopy(replacement_subjects)
        seen.add(fact_id)
    return corrected


def _validate_new_facts(value: dict[str, Any], *, session_ids: set[str], entity_ids: set[str], protected_ids: set[int]) -> list[dict[str, Any]]:
    facts = value.get("facts")
    if not isinstance(facts, list):
        raise ValueError("missing-facts response must contain a facts list")
    allowed_supersedes = set(protected_ids)
    expected_start, returned = max(protected_ids, default=0) + 1, []
    for fact in facts:
        fact_id = fact.get("id")
        if isinstance(fact_id, bool) or not isinstance(fact_id, int) or fact_id in protected_ids:
            raise ValueError("new fact IDs must be new integers")
        returned.append(fact_id)
        if not isinstance(fact.get("statement"), str) or not fact["statement"].strip() or not isinstance(fact.get("applies_when"), str):
            raise ValueError(f"new fact {fact_id} has invalid text")
        for field, required in (("source_session_ids", True), ("related_history_session_ids", False)):
            refs = fact.get(field)
            if not isinstance(refs, list) or (required and not refs) or len(refs) != len(set(refs)) or not all(isinstance(item, str) and item in session_ids for item in refs):
                raise ValueError(f"new fact {fact_id} has invalid {field}")
        subjects, supersedes = fact.get("subjects"), fact.get("supersedes")
        if not isinstance(subjects, list) or not subjects or len(subjects) != len(set(subjects)) or not all(isinstance(item, str) and item in entity_ids for item in subjects):
            raise ValueError(f"new fact {fact_id} has unresolved subjects")
        if not isinstance(supersedes, list) or len(supersedes) != len(set(supersedes)) or not all(isinstance(item, int) and not isinstance(item, bool) for item in supersedes) or set(supersedes) - allowed_supersedes:
            raise ValueError(f"new fact {fact_id} has unresolved supersedes")
        allowed_supersedes.add(fact_id)
    if returned != list(range(expected_start, expected_start + len(facts))):
        raise ValueError(f"new fact IDs must be contiguous from {expected_start}; got {returned}")
    return facts


def _manifest_tools(persona: str) -> list[dict[str, Any]]:
    """Return every manifest-listed server tool with its literal Python signature."""
    manifest_path = ROOT / "mock_mcp" / "manifests" / f"{persona}.yaml"
    server_path = ROOT / "mock_mcp" / "server.py"
    contracts_path = ROOT / 'mock_mcp' / 'tool_contracts.yaml'
    if not manifest_path.is_file() or not server_path.is_file() or not contracts_path.is_file():
        raise ValueError("mock tool manifest, server, or contracts file is missing")
    manifest = _load_yaml(manifest_path)
    if manifest.get("persona_id") != persona:
        raise ValueError("mock tool manifest persona does not match")
    names = manifest.get("tools")
    if not isinstance(names, list) or not names or not all(isinstance(name, str) and name for name in names) or len(names) != len(set(names)):
        raise ValueError("mock tool manifest must list unique non-empty tool names")
    contracts_document = _load_yaml(contracts_path)
    contracts = contracts_document.get("tools")
    if not isinstance(contracts, dict):
        raise ValueError("tool contracts must contain a tools object")
    tree = ast.parse(server_path.read_text())
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = [name for name in names if name not in functions]
    if missing:
        raise ValueError("manifest-listed tools have no server function: " + ", ".join(missing))
    result: list[dict[str, Any]] = []
    for name in names:
        node = functions[name]
        if node.args.vararg is not None or node.args.kwarg is not None:
            raise ValueError(f"manifest-listed tool has unsupported variadic arguments: {name}")
        positional = [*node.args.posonlyargs, *node.args.args]
        defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
        arguments: dict[str, dict[str, Any]] = {}
        for argument, default in zip(positional, defaults, strict=True):
            arguments[argument.arg] = {
                "required": default is None,
                "default": None if default is None else ast.unparse(default),
                "annotation": ast.unparse(argument.annotation) if argument.annotation else None,
            }
        for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
            arguments[argument.arg] = {
                "required": default is None,
                "default": None if default is None else ast.unparse(default),
                "annotation": ast.unparse(argument.annotation) if argument.annotation else None,
            }
        contract = contracts.get(name, {})
        if not isinstance(contract, dict):
            raise ValueError(f"tool contract for {name} must be an object")
        result.append({"name": name, "arguments": arguments, "contract": contract})
    return result


def _accepted_facts(
    facts: list[dict[str, Any]],
    subjects: dict[int, list[str]],
    missing_facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Combine protected facts with their accepted subjects and new accepted facts."""
    enriched: list[dict[str, Any]] = []
    for fact in facts:
        fact_id = int(fact["id"])
        if fact_id not in subjects:
            raise ValueError(f"accepted subjects are missing protected fact {fact_id}")
        row = copy.deepcopy(fact)
        row["subjects"] = subjects[fact_id]
        enriched.append(row)
    combined = [*enriched, *copy.deepcopy(missing_facts)]
    ids = [int(fact["id"]) for fact in combined]
    if len(ids) != len(set(ids)):
        raise ValueError("accepted facts have duplicate IDs")
    return combined


def _validate_app_effects(
    value: dict[str, Any],
    sessions: list[dict[str, Any]],
    tool_names: set[str],
) -> None:
    rows = value.get("message_effects")
    if not isinstance(rows, list):
        raise ValueError("app-effects response must contain a message_effects list")
    expected = [
        (str(session["id"]), message_index)
        for session in sessions
        for message_index, _ in enumerate(session["messages"])
    ]
    observed = [(row.get("session_id"), row.get("message_index")) for row in rows]
    if observed != expected:
        raise ValueError("message_effects must contain every repaired session/message pair exactly once in chronological order")
    by_pair = {
        (str(session["id"]), message_index): message
        for session in sessions
        for message_index, message in enumerate(session["messages"])
    }
    for row in rows:
        session_id, message_index = str(row["session_id"]), int(row["message_index"])
        message = by_pair[(session_id, message_index)]
        effects = row.get("effects")
        if not isinstance(effects, list):
            raise ValueError(f"message_effects {session_id}[{message_index}] must contain an effects list")
        for effect in effects:
            category = effect.get("category")
            if category not in _APP_EFFECT_CATEGORIES:
                raise ValueError(f"message effect has an invalid category in {session_id}[{message_index}]")
            quote = effect.get("evidence_quote")
            if not isinstance(quote, str) or not quote or quote not in message:
                raise ValueError(f"message effect must use an exact evidence quote from {session_id}[{message_index}]")
            tool = effect.get("tool")
            if category == "unsupported_request":
                if tool is not None:
                    raise ValueError(f"unsupported_request must not name a tool in {session_id}[{message_index}]")
            elif not isinstance(tool, str) or tool not in tool_names:
                raise ValueError(f"message effect must name an allowed manifest tool in {session_id}[{message_index}]")


def _accepted_metadata(
    run: Path,
    persona: str,
    persona_sheet: str,
    sessions: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, list[str]]]:
    entity_path = run / "accepted" / "03_entity_inventory.json"
    if not entity_path.is_file():
        return [], {}
    session_ids = {str(session["id"]) for session in sessions}
    entities = _validate_entities(_accepted_response(run, entity_path.name, "entity_inventory"), session_ids)
    entities = _apply_entity_inventory_corrections(run, persona, entities)
    entities = _validate_entities({"entities": entities}, session_ids)
    _validate_entity_source_grounding(entities, sessions, facts, persona_sheet)
    subjects_path = run / "accepted" / "04_existing_fact_subjects.json"
    if not subjects_path.is_file():
        return entities, {}
    entity_ids = {str(entity["id"]) for entity in entities}
    subjects = _validate_fact_subjects(_accepted_response(run, subjects_path.name, "existing_fact_subjects"), facts, entity_ids)
    subjects = _apply_fact_subject_corrections(run, persona, subjects, entity_ids)
    return entities, subjects


def _waiting(request: Path) -> dict[str, Any]:
    return {"status": "waiting", "request": str(request)}


def advance(*, persona: str, run: Path) -> dict[str, Any]:
    """Validate accepted stages and write exactly one next request."""
    paths, source_sessions, facts = _source_data(persona)
    _verify_manifest(persona, run, paths)
    facts = _apply_fact_statement_corrections(run, persona, facts)
    _validate_accepted_directory(run)
    accepted, requests = run / "accepted", run / "requests"
    first = requests / "01_find_message_problems.json"
    if not (accepted / first.name).is_file():
        if not first.is_file():
            raise ValueError("missing initial message-problem request; run prepare first")
        return _waiting(first)
    problems = _validate_problems(_accepted_response(run, first.name, "find_message_problems"), source_sessions)
    rewrite = requests / "02_rewrite_messages.json"
    if not (accepted / rewrite.name).is_file():
        if rewrite.is_file():
            return _waiting(rewrite)
        flagged = [str(session["id"]) for session in source_sessions if any(problem["session_id"] == session["id"] for problem in problems)]
        _write_json(rewrite, _request("rewrite_messages", REWRITE_MESSAGES_SYSTEM, _rewrite_schema(flagged), _rewrite_input(persona, paths["persona_sheet"].read_text(), problems, source_sessions, facts)))
        return {"status": "request_written", "request": str(rewrite)}
    sessions = _load_repaired_sessions(run, persona, _load_yaml(paths["life_sim"]), source_sessions, facts)
    entity = requests / "03_entity_inventory.json"
    if not (accepted / entity.name).is_file():
        if entity.is_file():
            return _waiting(entity)
        _write_json(entity, _request("entity_inventory", ENTITY_INVENTORY_SYSTEM, _entity_schema([str(session["id"]) for session in sessions]), {"persona": persona, "persona_sheet": paths["persona_sheet"].read_text(), "protected_existing_facts": facts, "sessions": [_session_view(session, facts) for session in sessions]}))
        return {"status": "request_written", "request": str(entity)}
    entities, subjects = _accepted_metadata(run, persona, paths["persona_sheet"].read_text(), sessions, facts)
    subjects_request = requests / "04_existing_fact_subjects.json"
    if not (accepted / subjects_request.name).is_file():
        if subjects_request.is_file():
            return _waiting(subjects_request)
        _write_json(subjects_request, _request("existing_fact_subjects", EXISTING_FACT_SUBJECTS_SYSTEM, _fact_subject_schema([int(fact["id"]) for fact in facts], [str(entity["id"]) for entity in entities]), {"accepted_entities": entities, **_protected_fact_input(facts, sessions)}))
        return {"status": "request_written", "request": str(subjects_request)}
    missing_facts = requests / "05_missing_facts.json"
    if (accepted / missing_facts.name).is_file():
        new_facts = _validate_new_facts(
            _accepted_response(run, missing_facts.name, "missing_facts"),
            session_ids={str(session["id"]) for session in sessions},
            entity_ids={str(entity["id"]) for entity in entities},
            protected_ids={int(fact["id"]) for fact in facts},
        )
        accepted_facts = _accepted_facts(facts, subjects, new_facts)
        app_effects = requests / "06_identify_app_effects.json"
        tools = _manifest_tools(persona)
        tool_names = [str(tool["name"]) for tool in tools]
        if (accepted / app_effects.name).is_file():
            _validate_app_effects(
                _accepted_response(run, app_effects.name, "identify_app_effects"),
                sessions,
                set(tool_names),
            )
            return {"status": "app_effects_complete"}
        if app_effects.is_file():
            return _waiting(app_effects)
        _write_json(app_effects, _request(
            "identify_app_effects",
            IDENTIFY_APP_EFFECTS_SYSTEM,
            _app_effect_schema(session_ids=[str(session["id"]) for session in sessions], tool_names=tool_names),
            {
                "persona": persona,
                "persona_sheet": paths["persona_sheet"].read_text(),
                "accepted_facts": accepted_facts,
                "sessions": [_session_view(session, accepted_facts) for session in sessions],
                "supplied_tools": tools,
            },
        ))
        return {"status": "request_written", "request": str(app_effects)}
    if missing_facts.exists():
        return _waiting(missing_facts)
    session_ids = [str(session["id"]) for session in sessions]
    _write_json(missing_facts, _request(
        "missing_facts",
        MISSING_FACTS_SYSTEM,
        _new_fact_schema(session_ids=session_ids, entity_ids=[str(entity["id"]) for entity in entities]),
        {
            "accepted_entities": entities,
            "protected_existing_facts": [
                {"protected_fact": fact, "subjects": subjects[int(fact["id"])]}
                for fact in facts
            ],
            "sessions": [_session_view(session, facts) for session in sessions],
        },
    ))
    return {"status": "request_written", "request": str(missing_facts)}


def finalize(*, persona: str, run: Path) -> None:
    """Disabled: staged initial-history preparation does not create a checkpoint."""
    del persona, run
    raise ValueError("finalize is disabled: staged initial-history preparation does not create a checkpoint")


def _load_request_decisions(
    *,
    persona: str,
    run: Path,
    path: Path,
    sessions: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    document = _load_json(path)
    if not isinstance(document, dict) or document.get("persona") != persona:
        raise ValueError("request decisions must be an object for the selected persona")
    input_sha256 = document.get("input_sha256")
    if not isinstance(input_sha256, dict) or not input_sha256:
        raise ValueError("request decisions must record their input file hashes")
    for relative_path, expected_hash in input_sha256.items():
        input_path = ROOT / str(relative_path)
        if not input_path.is_file() or file_hash(input_path) != expected_hash:
            raise ValueError(f"request-decision input changed: {relative_path}")

    decisions = document.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("request decisions must contain a non-empty decisions list")
    if document.get("decision_count") != len(decisions):
        raise ValueError("request decision_count does not match decisions")

    session_order = {str(session["id"]): index for index, session in enumerate(sessions)}
    message_by_pair = {
        (str(session["id"]), message_index): message
        for session in sessions
        for message_index, message in enumerate(session["messages"])
    }
    enabled_tools = {str(tool["name"]) for tool in _manifest_tools(persona)}
    observed: set[tuple[str, int]] = set()
    previous_order = -1
    supported_plans: list[dict[str, Any]] = []
    for decision in decisions:
        if not isinstance(decision, dict):
            raise ValueError("each request decision must be an object")
        session_id = str(decision.get("session_id") or "")
        message_index = decision.get("message_index")
        pair = (session_id, message_index)
        if pair not in message_by_pair or pair in observed:
            raise ValueError(f"request decision has an unknown or duplicate message: {pair}")
        observed.add(pair)
        current_order = session_order[session_id]
        if current_order <= previous_order:
            raise ValueError("request decisions must be in chronological session order")
        previous_order = current_order

        evidence = decision.get("evidence_quote")
        if not isinstance(evidence, str) or not evidence or evidence not in message_by_pair[pair]:
            raise ValueError(f"request decision must quote its message exactly: {session_id}")
        category = decision.get("decision")
        if category not in _REQUEST_DECISIONS:
            raise ValueError(f"invalid request decision for {session_id}: {category}")
        reason = decision.get("reason")
        if not isinstance(reason, str) or not reason:
            raise ValueError(f"request decision needs a reason: {session_id}")
        operations = decision.get("operations")
        if not isinstance(operations, list):
            raise ValueError(f"request decision operations must be a list: {session_id}")
        if category != "supported_action" and operations:
            raise ValueError(f"only supported actions may contain operations: {session_id}")
        if category == "supported_action":
            if not operations:
                raise ValueError(f"supported action has no operations: {session_id}")
            for operation in operations:
                if (
                    not isinstance(operation, dict)
                    or set(operation) != {"tool", "args"}
                    or operation.get("tool") not in enabled_tools
                    or not isinstance(operation.get("args"), dict)
                ):
                    raise ValueError(f"supported action has an invalid operation: {session_id}")
            source_session = sessions[current_order]
            supported_plans.append(
                {
                    "session_id": session_id,
                    "narrative_date": source_session["narrative_date"],
                    "app_operations": copy.deepcopy(operations),
                }
            )
    return document, supported_plans


def _add_required_records(app_state: dict[str, Any], records: Any) -> None:
    if not isinstance(records, list):
        raise ValueError("records_to_add_before_replay must be a list")
    for item in records:
        if not isinstance(item, dict) or set(item) != {"state_key", "record"}:
            raise ValueError("each pre-existing record must contain state_key and record")
        state_key = item["state_key"]
        record = item["record"]
        if not isinstance(state_key, str) or not isinstance(record, dict):
            raise ValueError("pre-existing record fields have invalid types")
        rows = app_state.setdefault(state_key, [])
        if not isinstance(rows, list):
            raise ValueError(f"app-state field is not a list: {state_key}")
        record_id = record.get("id") or record.get("event_id")
        if record_id and any(
            (row.get("id") or row.get("event_id")) == record_id
            for row in rows
            if isinstance(row, dict)
        ):
            raise ValueError(f"pre-existing record already exists: {record_id}")
        rows.append(copy.deepcopy(record))


def build_candidate_checkpoint(
    *,
    persona: str,
    run: Path,
    decisions_path: Path,
    baseline_path: Path,
    out: Path,
    tool_python: Path,
) -> dict[str, Any]:
    """Build, replay, and authenticate an initial candidate without promoting it."""
    run = run.resolve()
    decisions_path = decisions_path.resolve()
    baseline_path = baseline_path.resolve()
    out = out.resolve()
    tool_python = tool_python if tool_python.is_absolute() else (ROOT / tool_python)
    if out.name.endswith("_final"):
        raise ValueError("build-candidate cannot write a final checkpoint")
    if out.exists():
        raise ValueError(f"candidate output already exists: {out}")
    paths, source_sessions, protected_facts = _source_data(persona)
    _verify_manifest(persona, run, paths)
    protected_facts = _apply_fact_statement_corrections(run, persona, protected_facts)
    sessions = _load_repaired_sessions(
        run, persona, _load_yaml(paths["life_sim"]), source_sessions, protected_facts
    )
    entities, subjects = _accepted_metadata(
        run, persona, paths["persona_sheet"].read_text(), sessions, protected_facts
    )
    if not entities or len(subjects) != len(protected_facts):
        raise ValueError("accepted entities and protected-fact subjects are incomplete")
    new_facts = _validate_new_facts(
        _accepted_response(run, "05_missing_facts.json", "missing_facts"),
        session_ids={str(session["id"]) for session in sessions},
        entity_ids={str(entity["id"]) for entity in entities},
        protected_ids={int(fact["id"]) for fact in protected_facts},
    )
    facts = _accepted_facts(protected_facts, subjects, new_facts)
    decisions, supported_plans = _load_request_decisions(
        persona=persona,
        run=run,
        path=decisions_path,
        sessions=sessions,
    )
    if not baseline_path.is_file():
        raise ValueError(f"baseline state is missing: {baseline_path}")
    baseline = _load_json(baseline_path)
    if not isinstance(baseline, dict):
        raise ValueError("baseline app state must contain an object")
    app_state = copy.deepcopy(baseline)
    _add_required_records(app_state, decisions.get("records_to_add_before_replay"))

    work = out.with_name(out.name + ".build")
    pending = out.with_name(out.name + ".pending")
    for path in (work, pending):
        if path.exists():
            if not path.is_dir():
                raise ValueError(f"temporary output path is not a directory: {path}")
            shutil.rmtree(path)
    work.mkdir(parents=True)
    try:
        state_path = work / "app_state.json"
        log_path = work / "tool_calls.jsonl"
        _write_json(state_path, app_state)
        operation_results = execute_operations(
            persona=persona,
            plans=supported_plans,
            state_path=state_path,
            log_path=log_path,
            tool_python=tool_python,
            deterministic_replay=True,
        )
        if errors := operation_errors(operation_results):
            raise ValueError("initial app replay failed: " + "; ".join(errors))
        if len(operation_results) != sum(
            len(plan["app_operations"]) for plan in supported_plans
        ):
            raise ValueError("initial app replay stopped before all operations completed")
        app_state = _load_json(state_path)
        session_purposes = [
            {
                "session_id": str(session["id"]),
                "narrative_date": str(session["narrative_date"]),
                "purpose": " ".join(str(session["label"]).split()),
            }
            for session in sessions
        ]
        input_files = {
            str((run / "source_manifest.json").relative_to(ROOT)): file_hash(run / "source_manifest.json"),
            str((run / "repaired" / "life_sim.yaml").relative_to(ROOT)): file_hash(run / "repaired" / "life_sim.yaml"),
            str((run / "accepted" / "03_entity_inventory.json").relative_to(ROOT)): file_hash(run / "accepted" / "03_entity_inventory.json"),
            str((run / "accepted" / "04_existing_fact_subjects.json").relative_to(ROOT)): file_hash(run / "accepted" / "04_existing_fact_subjects.json"),
            str((run / "accepted" / "05_missing_facts.json").relative_to(ROOT)): file_hash(run / "accepted" / "05_missing_facts.json"),
            str(decisions_path.relative_to(ROOT)): file_hash(decisions_path),
            str(baseline_path.relative_to(ROOT)): file_hash(baseline_path),
        }
        system_sha256 = data_hash(
            {
                str(path.relative_to(ROOT)): file_hash(path)
                for path in (
                    ROOT / "construction" / "prepare_initial_history.py",
                    ROOT / "construction" / "runtime_operations.py",
                    ROOT / "construction" / "execute_mock_tool.py",
                    ROOT / "mock_mcp" / "server.py",
                )
            }
        )
        state = {
            "history": copy.deepcopy(sessions),
            "session_purposes": session_purposes,
            "entities": copy.deepcopy(entities),
            "facts": facts,
            "app_state": app_state,
            "unfinished_threads": [],
        }
        metadata = save_checkpoint(
            path=pending,
            persona=persona,
            week={
                "week_id": f"{persona}_initial_history",
                "end_date": str(sessions[-1]["narrative_date"]).split("T", 1)[0],
            },
            previous_hash="initial-source:" + data_hash(input_files),
            inputs=input_files,
            state=state,
            operation_results=operation_results,
            covered_events=[],
            system_sha256=system_sha256,
            revalidated_against_current_state=True,
            progress={"initial_history_repair_run": str(run.relative_to(ROOT))},
            identity_version=2,
        )
        identity = validated_checkpoint_identity(pending)
        pending.rename(out)
        counts = {
            decision: sum(row["decision"] == decision for row in decisions["decisions"])
            for decision in sorted(_REQUEST_DECISIONS)
        }
        summary = {
            "status": "candidate_checkpoint_built",
            "checkpoint": str(out),
            "checkpoint_identity": identity,
            "sessions": len(sessions),
            "entities": len(entities),
            "facts": len(facts),
            "request_decisions": counts,
            "tool_operations_replayed": len(operation_results),
            "history_tokens_o200k_base": metadata["history_tokens_o200k_base"],
        }
        _write_json(run / "review" / "initial_checkpoint_summary.json", summary)
        return summary
    finally:
        if work.is_dir():
            shutil.rmtree(work)
        if pending.is_dir():
            shutil.rmtree(pending)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, path_name in (("prepare", "--out"), ("advance", "--run")):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--persona", required=True, choices=sorted(ALLOWED_PERSONAS))
        command_parser.add_argument(path_name, required=True, type=Path)
    candidate = subparsers.add_parser("build-candidate")
    candidate.add_argument("--persona", required=True, choices=sorted(ALLOWED_PERSONAS))
    candidate.add_argument("--run", required=True, type=Path)
    candidate.add_argument("--decisions", required=True, type=Path)
    candidate.add_argument("--baseline", required=True, type=Path)
    candidate.add_argument("--out", required=True, type=Path)
    candidate.add_argument("--tool-python", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        prepare(persona=args.persona, out=args.out)
    elif args.command == "advance":
        print(json.dumps(advance(persona=args.persona, run=args.run)))
    else:
        print(json.dumps(build_candidate_checkpoint(
            persona=args.persona,
            run=args.run,
            decisions_path=args.decisions,
            baseline_path=args.baseline,
            out=args.out,
            tool_python=args.tool_python,
        )))


if __name__ == "__main__":
    main()
