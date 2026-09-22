"""Checkpoint-backed inputs for canonical DolphinBench test authoring."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from authoring.models import (
    ApprovedIdea,
    AuthoringConfig,
    CandidateTaskBatch,
    PlannedTask,
)
from construction.checkpoints import (
    load_checkpoint,
    validated_checkpoint_identity,
)
from construction.runtime_inputs import (
    current_fact_ids,
    exact_persona_tools,
    readable_app_state,
)


ROOT = Path(__file__).resolve().parents[1]


def resolve_repo_path(value: str, *, root: Path = ROOT) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def load_config(path: Path) -> AuthoringConfig:
    return AuthoringConfig.model_validate(yaml.safe_load(path.read_text()) or {})


@dataclass(frozen=True)
class CheckpointContext:
    persona: str
    checkpoint_path: Path
    checkpoint_identity: str
    metadata: dict[str, Any]
    history: list[dict[str, Any]]
    facts: list[dict[str, Any]]
    entities: list[dict[str, Any]]
    app_state: dict[str, Any]
    sessions_by_id: dict[str, dict[str, Any]]
    facts_by_id: dict[int, dict[str, Any]]
    active_fact_ids: set[int]
    tools: dict[str, dict[str, Any]]
    readable_state: dict[str, Any]
    evidence_packets: dict[tuple[int, ...], dict[str, Any]] | None = None


def validate_release_metadata(metadata: dict[str, Any]) -> None:
    budget = metadata.get("session_budget") or {}
    if metadata.get("identity_version") != 2:
        raise ValueError("test authoring requires an identity-version-2 release checkpoint")
    if (metadata.get("progress") or {}).get("construction_path") != "deterministic_release_cutoff":
        raise ValueError("test authoring requires a finalized release checkpoint")
    if budget.get("selection") != "first complete session at or above target":
        raise ValueError("release checkpoint has the wrong token-cutoff contract")
    target = budget.get("target_tokens_o200k_base")
    actual = budget.get("actual_tokens_o200k_base")
    if (
        not isinstance(target, int)
        or isinstance(target, bool)
        or target <= 0
        or not isinstance(actual, int)
        or isinstance(actual, bool)
        or actual < target
        or actual != metadata.get("history_tokens_o200k_base")
    ):
        raise ValueError("release checkpoint has inconsistent token-budget metadata")


def load_checkpoint_context(config: AuthoringConfig) -> CheckpointContext:
    checkpoint_path = resolve_repo_path(config.checkpoint)
    identity = validated_checkpoint_identity(checkpoint_path)
    metadata, state = load_checkpoint(checkpoint_path)
    validate_release_metadata(metadata)
    facts_document = yaml.safe_load((checkpoint_path / "facts.yaml").read_text()) or {}
    entities_document = yaml.safe_load((checkpoint_path / "entities.yaml").read_text()) or {}
    for label, document in (
        ("facts.yaml", facts_document),
        ("entities.yaml", entities_document),
    ):
        if str(document.get("persona") or "") != config.persona:
            raise ValueError(f"{label} persona does not match {config.persona}")
    history = state["history"]
    try:
        evaluation_date = date.fromisoformat(config.evaluation_date)
    except ValueError as exc:
        raise ValueError("evaluation_date must be an ISO date") from exc
    try:
        final_history_date = max(
            _narrative_date(row["narrative_date"], session_id=str(row["id"]))
            for row in history
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("checkpoint history contains an invalid narrative_date") from exc
    if evaluation_date <= final_history_date:
        raise ValueError(
            f"evaluation_date must be after the final history date {final_history_date}"
        )
    facts = state["facts"]
    sessions_by_id = {str(row["id"]): row for row in history}
    if len(sessions_by_id) != len(history):
        raise ValueError("checkpoint contains duplicate session IDs")
    facts_by_id = {int(row["id"]): row for row in facts}
    if len(facts_by_id) != len(facts):
        raise ValueError("checkpoint contains duplicate fact IDs")
    for fact_id, fact in facts_by_id.items():
        source_ids = [str(value) for value in fact.get("source_session_ids") or []]
        missing = [value for value in source_ids if value not in sessions_by_id]
        if not source_ids or missing:
            raise ValueError(f"fact {fact_id} has missing source sessions: {missing}")
    tools = exact_persona_tools(config.persona)
    return CheckpointContext(
        persona=config.persona,
        checkpoint_path=checkpoint_path,
        checkpoint_identity=identity,
        metadata=metadata,
        history=history,
        facts=facts,
        entities=state["entities"],
        app_state=state["app_state"],
        sessions_by_id=sessions_by_id,
        facts_by_id=facts_by_id,
        active_fact_ids=current_fact_ids(facts),
        tools=tools,
        readable_state=readable_app_state(tools, state["app_state"]),
    )


def load_authoring_tasks(
    *, context: CheckpointContext, config: AuthoringConfig, path: Path
) -> list[PlannedTask]:
    """Load the one supported checkpoint-bound authoring task batch."""
    raw = json.loads(path.read_text())
    batch = CandidateTaskBatch.model_validate(raw)
    if batch.persona != context.persona:
        raise ValueError("candidate task batch persona does not match checkpoint persona")
    if batch.checkpoint_identity != context.checkpoint_identity:
        raise ValueError(
            "candidate task batch checkpoint identity does not match checkpoint"
        )
    if batch.evaluation_date != config.evaluation_date:
        raise ValueError("candidate task batch evaluation date does not match config")
    _validate_tasks(context=context, config=config, tasks=batch.tasks)
    return batch.tasks


def _validate_tasks(
    *, context: CheckpointContext, config: AuthoringConfig, tasks: list[PlannedTask]
) -> None:
    for index, task in enumerate(tasks, start=1):
        rejected = [
            fact_id for fact_id in task.fact_ids if fact_id in config.rejected_fact_ids
        ]
        missing = [
            fact_id for fact_id in task.fact_ids if fact_id not in context.facts_by_id
        ]
        stale = [
            fact_id for fact_id in task.fact_ids if fact_id not in context.active_fact_ids
        ]
        unknown_tools = [
            tool for tool in task.expected_tools if tool not in context.tools
        ]
        if rejected:
            raise ValueError(
                f"planned task {index} references configured rejected facts {rejected}"
            )
        if missing:
            raise ValueError(f"planned task {index} references missing facts {missing}")
        if stale:
            raise ValueError(f"planned task {index} references superseded facts {stale}")
        if unknown_tools:
            raise ValueError(
                f"planned task {index} names unknown tools {unknown_tools}"
            )
        if isinstance(task, ApprovedIdea):
            continue
        evidence_rows = task.fact_application_explanations_on_evaluation_date
        evidence_is_present = [
            bool(
                row.required_result
                or row.source_session_id
                or row.exact_supporting_source_text
                or row.why_source_supports_required_result
            )
            for row in evidence_rows
        ]
        # Plans accepted before planner evidence existed remain readable. New
        # planner output always fills every evidence field and is validated here.
        if any(evidence_is_present):
            if not all(evidence_is_present):
                raise ValueError(
                    f"planned task {index} has incomplete planner evidence mappings"
                )
            evidence_keys = [
                (row.fact_id, row.required_result) for row in evidence_rows
            ]
            if len(evidence_keys) != len(set(evidence_keys)):
                raise ValueError(
                    f"planned task {index} repeats planner evidence for a result"
                )
            expected_evidence_keys: list[tuple[int, str]] = []
            for result in task.required_results:
                if len(result.fact_ids) != 1:
                    raise ValueError(
                        f"planned task {index} has planner evidence but a required "
                        "result does not name exactly one fact"
                )
                fact_id = result.fact_ids[0]
                expected_evidence_keys.append((fact_id, result.result))
            if len(expected_evidence_keys) != len(set(expected_evidence_keys)):
                raise ValueError(
                    f"planned task {index} repeats a fact-backed required result"
                )
            if set(expected_evidence_keys) != set(evidence_keys):
                raise ValueError(
                    f"planned task {index} planner evidence does not cover every "
                    "fact-backed required result"
                )
            for evidence in evidence_rows:
                fact_id = evidence.fact_id
                source_ids = {
                    str(value)
                    for value in context.facts_by_id[fact_id].get(
                        "source_session_ids"
                    )
                    or []
                }
                if evidence.source_session_id not in source_ids:
                    raise ValueError(
                        f"planned task {index} planner evidence cites a session that "
                        f"does not establish fact {fact_id}"
                    )
                source_text = _source_user_message_text(
                    context.sessions_by_id[evidence.source_session_id]
                )
                if evidence.exact_supporting_source_text not in source_text:
                    raise ValueError(
                        f"planned task {index} planner evidence quote does not match "
                        f"session {evidence.source_session_id}"
                    )


def _fact_source_sessions(
    context: CheckpointContext, fact_id: int
) -> list[dict[str, Any]]:
    wanted = {
        str(value)
        for value in context.facts_by_id[fact_id].get("source_session_ids") or []
    }
    return [copy.deepcopy(row) for row in context.history if str(row["id"]) in wanted]


def _narrative_date(value: Any, *, session_id: str) -> date:
    """Parse one session date, rejecting ambiguous or malformed source dates."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"session {session_id} has no narrative_date")
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ValueError(f"session {session_id} has an invalid narrative_date") from exc


def _source_user_message_text(session: dict[str, Any]) -> str:
    """Return the exact user-message text stored for one history session."""
    parts: list[str] = []
    message = session.get("message")
    if isinstance(message, str):
        parts.append(message)
    messages = session.get("messages")
    if isinstance(messages, list):
        parts.extend(value for value in messages if isinstance(value, str))
    text = "\n".join(parts)
    if not text:
        raise ValueError(f"session {session.get('id')!r} has no user-message text")
    return text


def planning_fact_rows(
    *, context: CheckpointContext, config: AuthoringConfig
) -> list[dict[str, Any]]:
    """Return the active facts that the planner may use or must compare."""
    source_ids_by_fact: dict[int, list[str]] = {}
    earliest_date_by_fact: dict[int, date] = {}
    latest_date_by_fact: dict[int, date] = {}
    eligible_facts: list[dict[str, Any]] = []

    for fact in context.facts:
        fact_id = int(fact["id"])
        if fact_id not in context.active_fact_ids or fact_id in config.rejected_fact_ids:
            continue
        source_session_ids = [
            str(value) for value in fact.get("source_session_ids") or []
        ]
        source_dates = [
            _narrative_date(
                context.sessions_by_id[session_id]["narrative_date"],
                session_id=session_id,
            )
            for session_id in source_session_ids
        ]
        if not source_dates:
            raise ValueError(f"fact {fact_id} has no source session dates")
        source_ids_by_fact[fact_id] = source_session_ids
        earliest_date_by_fact[fact_id] = min(source_dates)
        latest_date_by_fact[fact_id] = max(source_dates)
        eligible_facts.append(fact)

    rows: list[dict[str, Any]] = []
    for fact in eligible_facts:
        fact_id = int(fact["id"])
        rows.append(
            {
                "id": fact_id,
                "statement": str(fact.get("statement") or ""),
                "applies_when": str(fact.get("applies_when") or ""),
                "subjects": [str(value) for value in fact.get("subjects") or []],
                "source_session_ids": source_ids_by_fact[fact_id],
                "earliest_source_date": earliest_date_by_fact[fact_id].isoformat(),
                "latest_source_date": latest_date_by_fact[fact_id].isoformat(),
                "supersedes": [
                    int(value) for value in fact.get("supersedes") or []
                ],
            }
        )
    return rows


def planning_fact_subject_index(
    *, fact_rows: list[dict[str, Any]], persona: str
) -> dict[str, list[int]]:
    """List facts by concrete subject without treating the persona as a topic."""
    persona_subject = f"person:{persona.lower()}"
    persona_subject_prefix = persona_subject + "_"
    rows_by_id = {int(row["id"]): row for row in fact_rows}
    fact_ids_by_subject: dict[str, list[int]] = {}
    for row in fact_rows:
        fact_id = int(row["id"])
        for value in row.get("subjects") or []:
            subject = str(value).strip()
            if (
                not subject
                or subject == persona_subject
                or subject.startswith(persona_subject_prefix)
            ):
                continue
            fact_ids_by_subject.setdefault(subject, []).append(fact_id)
    for fact_ids in fact_ids_by_subject.values():
        fact_ids.sort(
            key=lambda fact_id: (
                str(rows_by_id[fact_id]["latest_source_date"]),
                fact_id,
            )
        )
    return dict(sorted(fact_ids_by_subject.items()))


def planning_source_sessions(
    *, context: CheckpointContext, fact_rows: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Return one exact source record per session used by planner-facing facts."""
    source_ids = {
        str(session_id)
        for fact in fact_rows
        for session_id in fact["source_session_ids"]
    }
    sessions: list[dict[str, str]] = []
    for session_id in sorted(
        source_ids,
        key=lambda value: (
            _narrative_date(
                context.sessions_by_id[value]["narrative_date"], session_id=value
            ),
            value,
        ),
    ):
        session = context.sessions_by_id[session_id]
        sessions.append(
            {
                "source_session_id": session_id,
                "source_date": str(session["narrative_date"]),
                "exact_user_message_text": _source_user_message_text(session),
            }
        )
    return sessions



def _one_line(value: str) -> str:
    """Keep planner-facing prose compact without changing its content."""
    return " ".join(value.split())


def _tool_doc_section(description: str, heading: str) -> str:
    """Extract one compact section from a recovered tool docstring."""
    _, marker, remainder = description.partition(f"\n\n{heading}:\n")
    return _one_line(remainder.partition("\n\n")[0]) if marker else ""


def _planner_action_description(tool: dict[str, Any]) -> str:
    """Summarize the recovered signature and declared contract without inference."""
    description = str(tool.get("description") or "")
    effect = tool.get("state_effect") or {}
    if not isinstance(effect, dict):
        raise ValueError("tool has invalid state_effect")

    required = [str(value) for value in tool.get("required_arguments") or []]
    arguments = [str(value) for value in tool.get("arguments") or []]
    accepts = (
        f"arguments {', '.join(arguments) or 'none'}; "
        f"required arguments {', '.join(required) or 'none declared'}"
    )
    policies = effect.get("args_policy") or {}
    if policies:
        accepts += "; " + "; ".join(
            f"{key}: {_one_line(str(value))}" for key, value in sorted(policies.items())
        )
    requirements = [
        _one_line(str(value)) for value in effect.get("hard_requirements") or []
    ]
    if requirements:
        accepts += "; requires " + "; ".join(requirements)

    returns = _one_line(
        str(effect.get("returns") or _tool_doc_section(description, "Returns") or "not specified")
    )
    reads = ", ".join(str(value) for value in effect.get("reads_state_keys") or []) or "none"
    writes = ", ".join(str(value) for value in effect.get("writes_state_keys") or []) or "none"

    direct_use_notes: list[str] = []
    limits: list[str] = []
    for key in sorted(effect):
        if key.startswith("does_not_"):
            value = effect[key]
            words = key.removeprefix("does_not_").replace("_", " ")
            if key == "does_not_require_lookup" and value is True:
                direct_use_notes.append("No prior lookup is required")
            elif isinstance(value, list):
                limits.append(
                    f"Does not {words}: {', '.join(str(item) for item in value)}"
                )
            elif value is True:
                limits.append(f"Does not {words}")
            elif value:
                limits.append(f"Does not {words}: {_one_line(str(value))}")
    limits.extend(_one_line(str(value)) for value in effect.get("notes") or [])
    if effect.get("visibility"):
        limits.append(_one_line(str(effect["visibility"])))
    cannot = "; ".join(limits) if limits else "no explicit limitation in contract"

    purpose = _one_line(description.partition("\n\nArgs:")[0])
    direct_use = (
        f" {'; '.join(direct_use_notes)}." if direct_use_notes else ""
    )
    return (
        f"Does: {purpose or 'not specified'}. Accepts: {accepts}. "
        f"Returns: {returns}. Reads: {reads}. Writes: {writes}. Cannot: {cannot}."
        f"{direct_use}"
    )


def planner_action_inventory(
    tools: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    """Describe actions with compact, exact recovered contract details."""
    return [
        {
            "tool_name": tool_name,
            "description": _planner_action_description(tools[tool_name]),
        }
        for tool_name in sorted(tools)
    ]



def _supersession_chain_ids(
    context: CheckpointContext, fact_ids: list[int]
) -> set[int]:
    """Return all facts explicitly connected by a supersedes link."""
    chain = set(fact_ids)
    changed = True
    while changed:
        changed = False
        for fact in context.facts:
            fact_id = int(fact["id"])
            supersedes = {
                int(value)
                for value in fact.get("supersedes") or []
                if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
            }
            if fact_id in chain or supersedes.intersection(chain):
                expanded = chain | supersedes | {fact_id}
                if expanded != chain:
                    chain = expanded
                    changed = True
    return chain


def _related_supersession_chain_facts(
    context: CheckpointContext, fact_ids: list[int]
) -> list[dict[str, Any]]:
    """Return only facts explicitly connected to selected facts by supersession."""
    selected = set(fact_ids)
    related_ids = _supersession_chain_ids(context, fact_ids) - selected
    return [
        copy.deepcopy(fact)
        for fact in context.facts
        if int(fact["id"]) in related_ids
    ]


def design_payload(
    *,
    config: AuthoringConfig,
    context: CheckpointContext,
    planned_task: PlannedTask,
) -> dict[str, Any]:
    if isinstance(planned_task, ApprovedIdea):
        from authoring.complete_test import writer_payload
        return writer_payload(config=config, context=context, idea=planned_task)
    selected_tools = set(planned_task.expected_tools)
    relevant_state_keys = {
        str(state_key)
        for tool_name in selected_tools
        for field in ("reads_state_keys", "writes_state_keys")
        for state_key in context.tools[tool_name].get("state_effect", {}).get(field, [])
    }
    tools = {
        name: copy.deepcopy(tool)
        for name, tool in context.tools.items()
        if name in selected_tools
        or relevant_state_keys.intersection(
            str(state_key)
            for state_key in tool.get("state_effect", {}).get("reads_state_keys", [])
        )
    }
    readable_state = {
        key: copy.deepcopy(value)
        for key, value in context.readable_state.items()
        if key in relevant_state_keys
    }
    return {
        "authoring_schema_version": 3,
        "persona": context.persona,
        "evaluation_date": config.evaluation_date,
        "planned_task": planned_task.model_dump(mode="python"),
        "selected_fact_evidence": [
            {
                "fact_id": fact_id,
                "official_fact_text": str(
                    context.facts_by_id[fact_id].get("statement") or ""
                ),
                "applies_when": str(
                    context.facts_by_id[fact_id].get("applies_when") or ""
                ),
                "source_session_ids": [
                    str(value)
                    for value in context.facts_by_id[fact_id].get(
                        "source_session_ids"
                    )
                    or []
                ],
                "supersedes": [
                    int(value)
                    for value in context.facts_by_id[fact_id].get("supersedes") or []
                ],
                "exact_source_sessions": _fact_source_sessions(context, fact_id),
            }
            for fact_id in planned_task.fact_ids
        ],
        "current_facts_that_may_change_the_meaning": _related_supersession_chain_facts(
            context, planned_task.fact_ids
        ),
        "tools": tools,
        "current_readable_app_state": readable_state,
    }


def corpus_summary(context: CheckpointContext) -> dict[str, Any]:
    return {
        "checkpoint_identity": context.checkpoint_identity,
        "history_tokens_o200k_base": context.metadata["history_tokens_o200k_base"],
        "sessions": len(context.history),
        "all_facts": len(context.facts),
        "current_facts": len(context.active_fact_ids),
    }


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
