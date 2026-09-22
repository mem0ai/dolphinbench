"""Version-4 writer records and deterministic conversion, used by authoring.run."""

from __future__ import annotations

import copy
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from authoring.models import ApprovedIdea, ExistingRecord, NewRecord
from authoring.pipeline import AuthoringValidationError, build_mock_state, tool_has_final_effect
from graders.explicit import CHECK_TYPES, validate_checks
from harness.task_schema import TestSpec


class SelectedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_key: StrictStr
    record_key: StrictStr
    match: dict[str, Any]


class SourceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: StrictStr
    fact_ids: list[StrictInt] = Field(min_length=1)
    source_session_id: StrictStr
    message_id: StrictStr
    quote: StrictStr


class WrittenCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion: dict[str, Any]
    evidence_ids: list[StrictStr]
    why_required: StrictStr


class WrittenTest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: StrictStr
    existing_records: list[SelectedRecord]
    new_records: list[NewRecord]
    expected_tools: list[StrictStr] = Field(min_length=1)
    evidence: list[SourceEvidence] = Field(min_length=1)
    checks: list[WrittenCheck] = Field(min_length=1)


class AuditedNonMemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: StrictStr
    source_location: StrictStr = Field(
        pattern=r"^/(user_request|starting_app_data/.+)$"
    )
    why_needed: StrictStr


class AuditedMemoryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: StrictStr
    fact_ids: list[StrictInt] = Field(min_length=1)
    check_ids: list[StrictStr] = Field(min_length=1)
    why_necessary: StrictStr


class AuditedFinalAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: StrictStr
    tool: StrictStr
    action_id: StrictStr
    check_ids: list[StrictStr] = Field(min_length=1)


class AuditedCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: StrictStr
    roles: list[Literal["remembered_result", "final_action", "target"]] = Field(min_length=1)
    why_necessary: StrictStr
    why_not_duplicate: StrictStr


class WriterQualityAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reader_and_goal: StrictStr
    why_request_is_natural: StrictStr
    why_scope_is_coherent: StrictStr
    non_memory_inputs: list[AuditedNonMemoryInput]
    memory_results: list[AuditedMemoryResult] = Field(min_length=1)
    why_request_and_state_do_not_reveal_memory: StrictStr
    final_actions: list[AuditedFinalAction]
    checks: list[AuditedCheck] = Field(min_length=1)


class WriterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["written", "cannot_write"]
    problem: StrictStr
    test: WrittenTest | None
    # Optional in the Python model so authenticated historical responses remain
    # readable. Current provider calls require it through parse_writer_response.
    quality_audit: WriterQualityAudit | None = None

    @model_validator(mode="after")
    def _status_matches_test(self) -> "WriterResponse":
        if self.status == "written":
            if self.test is None or self.problem:
                raise ValueError("written needs a test and an empty problem")
        elif self.test is not None or not self.problem.strip() or self.quality_audit is not None:
            raise ValueError("cannot_write needs a concrete problem, test null, and quality_audit null")
        return self


class ApprovedWriterCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_id: StrictInt = Field(gt=0)
    source_response_sha256: StrictStr
    allowed_correction_fields: list[Literal["test.request", "test.checks"]] = Field(min_length=1)
    protected_check_ids: list[StrictStr]
    specific_problem: StrictStr = Field(min_length=1)
    required_correction: StrictStr = Field(min_length=1)


class CriterionReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: StrictStr = Field(min_length=1)
    previous_criterion: StrictStr = Field(min_length=1)
    criterion: StrictStr = Field(min_length=1)


class ApprovedCriterionCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_id: StrictInt = Field(gt=0)
    source_response_sha256: StrictStr
    source_gate_sha256: StrictStr
    criterion_replacements: list[CriterionReplacement] = Field(min_length=1)


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Use the repository's existing explicit-field provider schema convention."""
    result = copy.deepcopy(schema)

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            if isinstance(node.get("properties"), dict):
                node["required"] = list(node["properties"])
            if node.get("type") == "object":
                node["additionalProperties"] = False
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(result)
    return result


def _encoded_field(schema: dict[str, Any], field: str) -> None:
    properties = schema["properties"]
    properties.pop(field)
    properties[field + "_json"] = {"type": "string"}


def assertion_response_schema(*, tools: list[str] | None = None) -> dict[str, Any]:
    variants = []
    for kind in sorted(CHECK_TYPES):
        fields: dict[str, Any] = {
            "check_id": {"type": "string"},
            "type": {"type": "string", "const": kind},
            "tool": {"type": "string"},
            "action_id": {"type": "string"},
        }
        if tools is not None:
            fields["tool"]["enum"] = tools
            if kind in {"tool_not_called", "tool_call_count"}:
                fields["action_id"]["const"] = ""
        if not kind.startswith("tool_"):
            fields["path"] = {"type": "string"}
        if kind == "field_llm_judge":
            fields["criterion"] = {"type": "string"}
        elif kind == "field_regex":
            fields["pattern"] = {"type": "string"}
        elif kind == "field_list_includes":
            fields["values_json"] = {"type": "string"}
        elif kind == "tool_call_count":
            fields["count"] = {"type": "integer"}
        elif kind not in {"tool_called", "tool_not_called", "field_absent_or_empty"}:
            fields["value_json"] = {"type": "string"}
        variants.append({"type": "object", "properties": fields})
    return {"anyOf": variants}


def writer_response_schema(*, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    schema = WriterResponse.model_json_schema()
    definitions = schema["$defs"]
    _encoded_field(definitions["SelectedRecord"], "match")
    _encoded_field(definitions["NewRecord"], "record")
    tools = sorted(payload["tools"]) if payload is not None else None
    definitions["WrittenCheck"]["properties"]["assertion"] = assertion_response_schema(tools=tools)
    if payload is not None:
        definitions["WrittenTest"]["properties"]["expected_tools"]["items"]["enum"] = tools
        selected = set(payload["approved_idea"]["fact_ids"])
        definitions["AuditedMemoryResult"]["properties"]["fact_ids"]["items"]["enum"] = sorted(selected)
        definitions["AuditedFinalAction"]["properties"]["tool"]["enum"] = tools
        bindings: dict[tuple[str, tuple[int, ...]], list[str]] = {}
        for source in payload["sources"]:
            fact_ids = tuple(sorted(selected if payload.get("workflow") == "production"
                                    else selected.intersection(source["fact_ids"])))
            if fact_ids:
                bindings.setdefault((source["source_session_id"], fact_ids), []).append(source["message_id"])
        if not bindings:
            raise ValueError("writer has no source messages linked to selected facts")
        # Bind citations structurally, while retaining all related messages in the input.
        evidence = []
        for (session_id, fact_ids), message_ids in sorted(bindings.items()):
            variant = copy.deepcopy(definitions["SourceEvidence"])
            properties = variant["properties"]
            properties["fact_ids"]["items"]["enum"] = list(fact_ids)
            properties["source_session_id"]["const"] = session_id
            properties["message_id"]["enum"] = message_ids
            evidence.append(variant)
        definitions["SourceEvidence"] = {"anyOf": evidence}
    return strict_schema(schema)


def decode_json(value: Any, location: str) -> Any:
    if not isinstance(value, str):
        raise ValueError(f"{location} must be JSON-encoded text")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise ValueError(f"{location} repeats JSON key {key!r}")
            result[key] = item
        return result

    def constant(token: str) -> None:
        raise ValueError(f"{location} contains non-JSON number {token}")

    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{location} contains invalid JSON: {exc.msg}") from exc


def _decode_field(row: dict[str, Any], name: str, location: str) -> None:
    encoded = name + "_json"
    if name in row or encoded not in row:
        raise ValueError(f"{location} needs only {encoded} in the provider response")
    row[name] = decode_json(row.pop(encoded), f"{location}.{encoded}")


def parse_writer_response(
    raw: dict[str, Any], *, require_quality_audit: bool = False
) -> WriterResponse:
    value = copy.deepcopy({key: item for key, item in raw.items() if not key.startswith("_")})
    test = value.get("test")
    if isinstance(test, dict):
        for field, name in (("existing_records", "match"), ("new_records", "record")):
            for index, row in enumerate(test.get(field) or []):
                if not isinstance(row, dict):
                    raise ValueError(f"test.{field}.{index} must be an object")
                _decode_field(row, name, f"test.{field}.{index}")
        for index, row in enumerate(test.get("checks") or []):
            assertion = row.get("assertion") if isinstance(row, dict) else None
            if not isinstance(assertion, dict):
                raise ValueError(f"test.checks.{index}.assertion must be an object")
            for name in ("value", "values"):
                if name in assertion or name + "_json" in assertion:
                    _decode_field(assertion, name, f"test.checks.{index}.assertion")
    response = WriterResponse.model_validate(value)
    if require_quality_audit and response.test is not None and response.quality_audit is None:
        raise ValueError("a current written response needs quality_audit")
    return response


def encode_writer_response(response: WriterResponse) -> dict[str, Any]:
    """Encode flexible values for a model correction without rewriting the test."""
    value = response.model_dump(mode="json")
    test = value["test"]
    if test is not None:
        for field, name in (("existing_records", "match"), ("new_records", "record")):
            for row in test[field]:
                row[name + "_json"] = json.dumps(row.pop(name), ensure_ascii=False, allow_nan=False)
        for row in test["checks"]:
            for name in ("value", "values"):
                if name in row["assertion"]:
                    row["assertion"][name + "_json"] = json.dumps(
                        row["assertion"].pop(name), ensure_ascii=False, allow_nan=False
                    )
    return value


def source_records(context: Any, fact_ids: set[int]) -> list[dict[str, Any]]:
    """Preserve each original message once, with a program-assigned stable ID."""
    facts_by_session: dict[str, list[int]] = {}
    for fact_id in sorted(fact_ids):
        for session_id in context.facts_by_id[fact_id].get("source_session_ids") or []:
            facts_by_session.setdefault(str(session_id), []).append(fact_id)
    records = []
    for session in context.history:
        session_id = str(session["id"])
        if session_id not in facts_by_session:
            continue
        messages = []
        if isinstance(session.get("message"), str):
            messages.append(session["message"])
        messages.extend(item for item in session.get("messages") or [] if isinstance(item, str))
        if not messages:
            raise ValueError(f"source session {session_id} has no original user messages")
        for index, message in enumerate(messages):
            records.append({
                "source_session_id": session_id,
                "message_id": f"{session_id}:message:{index}",
                "date": str(session["narrative_date"]),
                "text": message,
                "fact_ids": sorted(set(facts_by_session[session_id])),
            })
    return records


def source_context(context: Any, idea: ApprovedIdea) -> dict[str, Any]:
    packets = getattr(context, "evidence_packets", None)
    if packets is not None and tuple(sorted(idea.fact_ids)) in packets:
        return copy.deepcopy(packets[tuple(sorted(idea.fact_ids))])
    from authoring.context import _supersession_chain_ids, planning_fact_subject_index

    rows = [
        {**fact, "latest_source_date": max(
            str(context.sessions_by_id[str(sid)]["narrative_date"])
            for sid in fact.get("source_session_ids") or []
        )}
        for fact in context.facts
    ]
    by_subject = planning_fact_subject_index(fact_rows=rows, persona=context.persona)
    related_ids = _supersession_chain_ids(context, idea.fact_ids)
    for subject, ids in by_subject.items():
        # Some checkpoints use a bare persona name instead of person:<name>.
        if subject.casefold() == context.persona.casefold():
            continue
        if set(ids).intersection(idea.fact_ids):
            related_ids.update(ids)
    related_ids.difference_update(idea.fact_ids)
    return {
        "selected_facts": [copy.deepcopy(context.facts_by_id[fact_id]) for fact_id in idea.fact_ids],
        "sources": source_records(context, set(idea.fact_ids) | related_ids),
        "related_updates": [copy.deepcopy(context.facts_by_id[fact_id]) for fact_id in sorted(related_ids)],
    }


def writer_payload(*, config: Any, context: Any, idea: ApprovedIdea) -> dict[str, Any]:
    reads = {
        key for tool in idea.expected_tools
        for key in (context.tools[tool].get("state_effect") or {}).get("reads_state_keys") or []
    }
    writes = {
        key for tool in idea.expected_tools
        for key in (context.tools[tool].get("state_effect") or {}).get("writes_state_keys") or []
    }
    # Keep complete collections for declared reads. A write destination alone
    # does not make its old records necessary; no prose-based filtering occurs.
    tools = {
        name: copy.deepcopy(tool) for name, tool in context.tools.items()
        if name in idea.expected_tools or reads.intersection(
            (tool.get("state_effect") or {}).get("reads_state_keys") or []
        )
    }
    evidence = source_context(context, idea)
    # The independent reviewer checks the broader dependency catalog. Writing
    # uses the selected complete evidence, not a second full selection pass.
    evidence.pop("related_fact_catalog", None)
    return {
        "authoring_schema_version": 4,
        "persona": context.persona,
        "evaluation_date": config.evaluation_date,
        "approved_idea": idea.model_dump(mode="json"),
        **evidence,
        "tools": tools,
        "app_records": {key: copy.deepcopy(context.readable_state[key]) for key in sorted(reads) if key in context.readable_state},
        "app_record_shapes": {
            key: "list" if isinstance(value, list) else "dictionary"
            for key, value in context.app_state.items()
            if key in reads | writes and isinstance(value, (dict, list))
        },
        "check_types": assertion_response_schema(),
    }


def _validate_quality_audit(
    *, response: WriterResponse, idea: ApprovedIdea, context: Any, payload: dict[str, Any]
) -> list[str]:
    audit = response.quality_audit
    test = response.test
    if audit is None or test is None:
        return []

    errors: list[str] = []

    def empty_text_locations(value: Any, location: str) -> None:
        if isinstance(value, str):
            if not value.strip():
                errors.append(f"{location} must not be blank")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                empty_text_locations(child, f"{location}.{index}")
        elif isinstance(value, dict):
            for key, child in value.items():
                empty_text_locations(child, f"{location}.{key}")

    empty_text_locations(audit.model_dump(mode="json"), "quality_audit")

    starting_state: dict[str, Any] | None = None
    for index, row in enumerate(audit.non_memory_inputs):
        if row.source_location == "/user_request":
            continue
        if starting_state is None:
            try:
                starting_state = written_state(context, test)
                # Missing collections are empty for reads that explicitly allow it.
                for tool in test.expected_tools:
                    effect = payload.get("tools", {}).get(tool, {}).get("state_effect") or {}
                    if effect.get("empty_result_is_valid") is True:
                        for key in effect.get("reads_state_keys", []):
                            shape = payload.get("app_record_shapes", {}).get(key)
                            if shape in {"list", "dict"}:
                                starting_state.setdefault(key, [] if shape == "list" else {})
            except (AuthoringValidationError, TypeError, ValueError):
                errors.append(
                    f"quality_audit.non_memory_inputs.{index}.source_location cannot be "
                    "checked because the selected state is invalid"
                )
                continue
        value: Any = starting_state
        found = True
        for encoded in row.source_location.split("/")[2:]:
            part = encoded.replace("~1", "/").replace("~0", "~")
            if isinstance(value, dict) and part in value:
                value = value[part]
            elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            else:
                found = False
                break
        if not found:
            errors.append(
                f"quality_audit.non_memory_inputs.{index}.source_location names unavailable state"
            )

    checks = {row.assertion.get("check_id"): row for row in test.checks}
    check_ids = [row.assertion.get("check_id") for row in test.checks]
    evidence = {row.evidence_id: set(row.fact_ids) for row in test.evidence}
    facts_by_check = {
        check_id: set().union(*(evidence.get(evidence_id, set()) for evidence_id in row.evidence_ids))
        for check_id, row in checks.items()
    }

    selected = set(idea.fact_ids)
    mapped_facts: set[int] = set()
    mapped_memory_checks: set[str] = set()
    for index, row in enumerate(audit.memory_results):
        name = f"quality_audit.memory_results.{index}"
        row_facts = set(row.fact_ids)
        row_checks = set(row.check_ids)
        if len(row_facts) != len(row.fact_ids):
            errors.append(f"{name}.fact_ids contains duplicates")
        if row_facts - selected:
            errors.append(f"{name}.fact_ids names unselected facts {sorted(row_facts - selected)}")
        if len(row_checks) != len(row.check_ids):
            errors.append(f"{name}.check_ids contains duplicates")
        if row_checks - checks.keys():
            errors.append(f"{name}.check_ids names unknown checks {sorted(row_checks - checks.keys())}")
        for check_id in row_checks & checks.keys():
            if not facts_by_check[check_id].intersection(row_facts):
                errors.append(f"{name} maps {check_id!r} without linked source evidence")
        supported = set().union(*(facts_by_check.get(check_id, set()) for check_id in row_checks))
        if row_facts - supported:
            errors.append(f"{name} does not bind facts {sorted(row_facts - supported)} to its checks")
        mapped_facts.update(row_facts)
        mapped_memory_checks.update(row_checks)
    if mapped_facts != selected:
        errors.append(
            "quality_audit.memory_results must cover every selected fact: "
            f"selected={sorted(selected)}, covered={sorted(mapped_facts)}"
        )
    evidence_check_ids = {check_id for check_id, fact_ids in facts_by_check.items() if fact_ids}
    if mapped_memory_checks != evidence_check_ids:
        errors.append(
            "quality_audit.memory_results must name exactly the evidence-backed checks: "
            f"checks={sorted(evidence_check_ids)}, mapped={sorted(mapped_memory_checks)}"
        )

    final_effect_tools = {
        tool for tool in test.expected_tools if tool_has_final_effect(context, tool)
    }
    action_ids: set[str] = set()
    action_check_ids: set[str] = set()
    audited_effect_tools: set[str] = set()
    for index, row in enumerate(audit.final_actions):
        name = f"quality_audit.final_actions.{index}"
        if row.action_id in action_ids:
            errors.append(f"{name}.action_id repeats {row.action_id!r}")
        action_ids.add(row.action_id)
        if row.tool not in test.expected_tools or row.tool not in payload["tools"]:
            errors.append(f"{name}.tool names an unavailable or unexpected tool {row.tool!r}")
        if len(set(row.check_ids)) != len(row.check_ids):
            errors.append(f"{name}.check_ids contains duplicates")
        for check_id in row.check_ids:
            check = checks.get(check_id)
            if check is None:
                errors.append(f"{name}.check_ids names unknown check {check_id!r}")
            elif (
                check.assertion.get("tool") != row.tool
                or check.assertion.get("action_id") != row.action_id
            ):
                errors.append(f"{name} does not match check {check_id!r}'s tool and action_id")
            else:
                action_check_ids.add(check_id)
        audited_effect_tools.add(row.tool)
    if not final_effect_tools.issubset(audited_effect_tools):
        errors.append(
            "quality_audit.final_actions must cover every final-effect tool: "
            f"tools={sorted(final_effect_tools)}, covered={sorted(audited_effect_tools)}"
        )

    audited_checks: dict[str, AuditedCheck] = {}
    for index, row in enumerate(audit.checks):
        name = f"quality_audit.checks.{index}"
        if row.check_id in audited_checks:
            errors.append(f"{name}.check_id repeats {row.check_id!r}")
        audited_checks[row.check_id] = row
        if len(set(row.roles)) != len(row.roles):
            errors.append(f"{name}.roles contains duplicates")
    if set(audited_checks) != set(check_ids):
        errors.append(
            "quality_audit.checks must cover every written check exactly: "
            f"checks={sorted(check_ids)}, covered={sorted(audited_checks)}"
        )
    for check_id in mapped_memory_checks & audited_checks.keys():
        if "remembered_result" not in audited_checks[check_id].roles:
            errors.append(f"quality_audit.checks for {check_id!r} omits remembered_result")
    for check_id in action_check_ids & audited_checks.keys():
        if "final_action" not in audited_checks[check_id].roles:
            errors.append(f"quality_audit.checks for {check_id!r} omits final_action")
    return errors


def validate_written_test(*, response: WriterResponse, idea: ApprovedIdea,
                          context: Any, payload: dict[str, Any]) -> None:
    if response.test is None:
        return
    test = response.test
    errors: list[str] = []
    production = payload.get("workflow") == "production"
    correction_fields: set[str] = set()
    selected = set(idea.fact_ids)
    sources = {row["message_id"]: row for row in payload["sources"]}
    evidence: dict[str, SourceEvidence] = {}
    covered: set[int] = set()
    if not test.request.strip():
        errors.append("test.request must not be empty")
    if len(set(test.expected_tools)) != len(test.expected_tools):
        errors.append("test.expected_tools contains duplicates")
    unknown_tools = set(test.expected_tools) - set(payload["tools"])
    if unknown_tools:
        errors.append(f"test.expected_tools names unavailable tools {sorted(unknown_tools)}")
    missing_tools = set(idea.expected_tools) - set(test.expected_tools)
    if missing_tools:
        errors.append(f"test.expected_tools omits approved tools {sorted(missing_tools)}")
    if errors:
        correction_fields.update(("test.request", "test.expected_tools"))
    before_evidence = len(errors)
    for index, row in enumerate(test.evidence):
        name = f"test.evidence.{index}"
        if not row.evidence_id.strip() or row.evidence_id in evidence:
            errors.append(f"{name} has an empty or repeated evidence ID")
        evidence[row.evidence_id] = row
        if len(set(row.fact_ids)) != len(row.fact_ids) or set(row.fact_ids) - selected:
            errors.append(f"{name} cites repeated or unselected facts")
        source = sources.get(row.message_id)
        if source is None or source["source_session_id"] != row.source_session_id:
            errors.append(f"{name} does not identify a supplied source message")
        elif not production and set(row.fact_ids) - set(source["fact_ids"]):
            errors.append(f"{name} cites a message not linked to its facts")
        elif not row.quote.strip() or (not production and row.quote not in source["text"]):
            errors.append(f"{name}.quote is not an exact passage of that message")
    if len(errors) != before_evidence:
        correction_fields.add("test.evidence")
    before_checks = len(errors)
    assertions = [row.assertion for row in test.checks]
    try:
        validate_checks(assertions)
    except (ValueError, TypeError) as exc:
        errors.append(f"test.checks: {exc}")
    positive_tools: set[str] = set()
    used_evidence: set[str] = set()
    for index, row in enumerate(test.checks):
        name = f"test.checks.{index}"
        if not row.why_required.strip():
            errors.append(f"{name}.why_required must explain necessity")
        if len(set(row.evidence_ids)) != len(row.evidence_ids):
            errors.append(f"{name} repeats evidence IDs")
        for evidence_id in row.evidence_ids:
            used_evidence.add(evidence_id)
            if evidence_id not in evidence:
                errors.append(f"{name} references missing evidence {evidence_id!r}")
            else:
                covered.update(evidence[evidence_id].fact_ids)
        assertion = row.assertion
        tool = assertion.get("tool")
        if tool not in test.expected_tools or tool not in payload["tools"]:
            errors.append(f"{name} names an unavailable or unexpected tool {tool!r}")
            continue
        path = assertion.get("path")
        if isinstance(path, str) and path.startswith("args."):
            argument = path.split(".")[1]
            if argument not in payload["tools"][tool]["arguments"]:
                errors.append(f"{name} names an unknown argument {argument!r}")
        if assertion.get("type") != "tool_not_called" and not (
            assertion.get("type") == "tool_call_count" and assertion.get("count") == 0
        ):
            positive_tools.add(tool)
    if covered != selected:
        errors.append(f"checks must cover every selected fact: selected={sorted(selected)}, covered={sorted(covered)}")
    if not production and set(evidence) - used_evidence:
        errors.append(f"test.evidence has unused entries {sorted(set(evidence) - used_evidence)}")
    missing_effects = {tool for tool in test.expected_tools if tool_has_final_effect(context, tool)} - positive_tools
    if missing_effects:
        errors.append(f"requested final effects have no positive checks: {sorted(missing_effects)}")
    if len(errors) != before_checks:
        correction_fields.add("test.checks")
    before_records = len(errors)
    for index, record in enumerate(test.existing_records):
        if record.state_key not in payload["app_records"]:
            errors.append(f"test.existing_records.{index} selects an unsupplied collection")
    if len(errors) != before_records:
        correction_fields.add("test.existing_records")
    if not production:
        errors.extend(_validate_quality_audit(
            response=response, idea=idea, context=context, payload=payload
        ))
    if errors:
        raise AuthoringValidationError("; ".join(errors), correction_fields=tuple(sorted(correction_fields)))
    # This validates selectors and state shapes without interpreting the task.
    written_state(context, test)


def written_state(context: Any, test: WrittenTest) -> dict[str, Any]:
    from types import SimpleNamespace

    selections = [ExistingRecord(**row.model_dump(), remove_fields=[]) for row in test.existing_records]
    keys = [json.dumps(row.model_dump(mode="json"), sort_keys=True) for row in test.existing_records]
    if len(set(keys)) != len(keys):
        raise AuthoringValidationError("existing records repeat a selector")
    return build_mock_state(context, SimpleNamespace(existing_records=selections, new_records=test.new_records))


def assemble_written_test(*, response: WriterResponse, idea: ApprovedIdea, context: Any,
                          evaluation_date: str, test_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    validate_written_test(response=response, idea=idea, context=context, payload=payload)
    test = response.test
    if test is None:
        raise AuthoringValidationError("cannot assemble a cannot_write response")
    candidate = {
        "id": test_id, "narrative_anchor_date": evaluation_date,
        "test": test.request, "load_bearing_facts": list(idea.fact_ids),
        "expected_tool_calls": list(test.expected_tools),
        "mock_state": written_state(context, test),
        "grade": {"type": "tool_trace", "config": {
            "check_version": 2, "today": evaluation_date,
            **({"semantic_judge_version": 2} if payload.get("semantic_judge_version") == 2 else {}),
            "assertions": [copy.deepcopy(row.assertion) for row in test.checks],
        }},
    }
    if payload.get("workflow") == "production":
        from harness.oracle import _persona_display_name
        candidate["test"] = f"I'm {_persona_display_name(context.persona)}.\n\n{test.request}"
    return TestSpec.model_validate(candidate).model_dump(mode="python")


def writer_input_tokens(system: str, payload: dict[str, Any], schema: dict[str, Any]) -> int:
    import tiktoken
    serialized = system + "\n" + json.dumps(payload, ensure_ascii=False) + "\n" + json.dumps(schema)
    return len(tiktoken.get_encoding("o200k_base").encode(serialized))


def correction_scope(issues: list[dict[str, Any]], response: WriterResponse) -> dict[str, Any]:
    """Name writable parts and protect every check not identified by the review."""
    if response.test is None or any(row["part"] in {"planning", "system"} for row in issues):
        return {"allowed_correction_fields": [], "protected_check_ids": []}
    fields: set[str] = set()
    affected_checks: set[str] = set()
    for issue in issues:
        part = issue["part"]
        if part == "request":
            fields.add("test.request")
        elif part == "starting_state":
            fields.update(("test.existing_records", "test.new_records"))
        elif part == "checks":
            fields.add("test.checks")
            pieces = issue["location"].split("/")
            if len(pieces) >= 3 and pieces[1] == "grading_checks" and pieces[2].isdigit():
                index = int(pieces[2])
                if index >= len(response.test.checks):
                    raise ValueError("review correction names an unavailable check")
                affected_checks.add(response.test.checks[index].assertion["check_id"])
            elif len(pieces) >= 4 and pieces[1:3] == ["authoring_evidence", "checks"] and pieces[3].isdigit():
                index = int(pieces[3])
                if index >= len(response.test.checks):
                    raise ValueError("review correction names unavailable check evidence")
                affected_checks.add(response.test.checks[index].assertion["check_id"])
            elif issue["location"] != "/grading_checks":
                raise ValueError("a check correction must name its check, or /grading_checks for an addition")
    return {
        "allowed_correction_fields": sorted(fields),
        "protected_check_ids": [row.assertion["check_id"] for row in response.test.checks
                                if row.assertion["check_id"] not in affected_checks],
    }


def validate_writer_correction(previous: WriterResponse, current: WriterResponse,
                               scope: dict[str, Any]) -> None:
    fields = scope.get("allowed_correction_fields") or []
    supported = {"test.request", "test.existing_records", "test.new_records", "test.checks"}
    if scope.get("dependent_evidence_correction") is True:
        supported.update(("test.evidence", "test.expected_tools"))
    if not fields or set(fields) - supported:
        raise AuthoringValidationError("writer correction has no valid field permission")
    before = previous.model_dump(mode="json")
    after = current.model_dump(mode="json")
    if previous.test is None or current.test is None:
        raise AuthoringValidationError("a scoped correction must preserve the written test")
    if "test.checks" in fields:
        protected = scope.get("protected_check_ids")
        if not isinstance(protected, list):
            raise AuthoringValidationError("a grading correction must identify protected checks")
        old = [row for row in before["test"]["checks"] if row["assertion"]["check_id"] in protected]
        new = [row for row in after["test"]["checks"] if row["assertion"]["check_id"] in protected]
        if old != new:
            raise AuthoringValidationError("correction changed or removed an unaffected check")
    if "test.evidence" in fields and scope.get("protected_evidence_ids"):
        protected_evidence = set(scope["protected_evidence_ids"])
        old = [row for row in before["test"]["evidence"] if row["evidence_id"] in protected_evidence]
        new = [row for row in after["test"]["evidence"] if row["evidence_id"] in protected_evidence]
        if old != new:
            raise AuthoringValidationError("correction changed evidence used by an unaffected check")
    # The audit is internal reasoning about the complete current response. It
    # may be refreshed after an allowed test change and never enters a candidate.
    before.pop("quality_audit", None)
    after.pop("quality_audit", None)
    for field in fields:
        name = field.removeprefix("test.")
        before["test"].pop(name)
        after["test"].pop(name)
    if before != after:
        raise AuthoringValidationError("correction changed a field outside its assigned parts")


def authoring_evidence(response: WriterResponse) -> dict[str, Any]:
    if response.test is None:
        raise ValueError("a written test is required")
    result = {"evidence": [row.model_dump(mode="json") for row in response.test.evidence],
              "checks": [row.model_dump(mode="json") for row in response.test.checks]}
    if response.quality_audit is not None:
        result["quality_audit"] = response.quality_audit.model_dump(mode="json")
    return result


def certification_input(context: Any, idea: ApprovedIdea, evaluation_date: str) -> dict[str, Any]:
    from harness.oracle import NORMAL_ASSISTANT_PROMPT
    packets = getattr(context, "evidence_packets", None)
    sources = (packets[tuple(sorted(idea.fact_ids))]["sources"]
               if packets is not None and tuple(sorted(idea.fact_ids)) in packets
               else source_records(context, set(idea.fact_ids)))
    return {
        "version": 4, "system": NORMAL_ASSISTANT_PROMPT.format(evaluation_date=evaluation_date),
        "source_messages": [
            {key: row[key] for key in ("source_session_id", "message_id", "date", "text")}
            for row in sources
        ],
    }
