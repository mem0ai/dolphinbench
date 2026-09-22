"""One source-grounded review of completed four-attempt production candidates."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, StrictStr, model_validator

from authoring.complete_test import WriterResponse, strict_schema
from authoring.context import dump_json
from authoring.review import validate_certification
from construction.runtime_model_calls import cached_client_complete


class ProductionIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part: Literal["planning", "request", "starting_state", "checks", "evidence", "system"]
    check_ids: list[StrictStr]
    source_message_ids: list[StrictStr]
    problem: StrictStr
    required_change: StrictStr


class ProductionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "correct", "reject", "pending"]
    issues: list[ProductionIssue]

    @model_validator(mode="after")
    def consistent(self) -> "ProductionReview":
        if (self.decision == "approve") != (not self.issues):
            raise ValueError("approval needs no issues; other decisions need a concrete issue")
        if any(not issue.problem.strip() or not issue.required_change.strip() for issue in self.issues):
            raise ValueError("each issue needs a problem and required change")
        return self


def validate_survivor(request: dict[str, Any]) -> dict[str, Any]:
    """Validate the real executions, without a synthetic preflight requirement."""
    validate_certification(request)
    shots = request["executions"]
    ids = [shot.get("attempt_id") for shot in shots]
    if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != 4:
        raise ValueError("four distinct completed attempt IDs are required")
    evidence = request["authoring_evidence"]
    memory_checks = {row["assertion"]["check_id"] for row in evidence["checks"] if row["evidence_ids"]}
    if not memory_checks:
        raise ValueError("no check is linked to a remembered result")
    failed = []
    for shot in shots:
        if shot["with_memory"]:
            continue
        actual_failures = []
        for detail in shot["grade"]["details"]:
            if detail["name"] not in memory_checks or detail["ok"]:
                continue
            evaluations = detail.get("call_evaluations", [])
            # An unrelated check failing on the same action is not a memory failure.
            if not evaluations or not any(row.get("ok") is True for row in evaluations):
                actual_failures.append(detail["name"])
        if not actual_failures:
            raise ValueError("a no-history attempt must actually fail a remembered-result check")
        failed.append(actual_failures)
    return {"policy_version": 2, "remembered_check_ids": sorted(memory_checks),
            "no_memory_failed_remembered_check_ids": failed}


def model_request(request: dict[str, Any]) -> dict[str, Any]:
    """Avoid repeating source histories, checks, and input state in four traces."""
    result = {key: copy.deepcopy(request[key]) for key in (
        "test_id", "evaluation_date", "planned_work", "user_request", "starting_app_data",
        "selected_tool_contracts", "grading_checks", "grading_semantics", "sources",
        "selected_facts", "related_updates", "related_fact_catalog", "certification_result",
    ) if key in request}
    result["authoring_evidence"] = {key: copy.deepcopy(request["authoring_evidence"][key])
                                    for key in ("evidence", "checks")}
    result["executions"] = []
    for shot in request["executions"]:
        result["executions"].append({
            **{key: copy.deepcopy(shot.get(key)) for key in (
                "attempt_id", "with_memory", "tool_calls", "tool_results", "response_text", "passed")},
            "check_results": [{key: copy.deepcopy(detail.get(key)) for key in
                               ("name", "ok", "reason", "matched_call_index", "call_evaluations")}
                              for detail in shot["grade"]["details"]],
        })
        for detail in result["executions"][-1]["check_results"]:
            for evaluation in detail.get("call_evaluations") or []:
                evaluation.pop("value", None)
    return result


def execute_production_review(*, request: dict[str, Any], out: Path, client: Any,
                              inspect_failed_grading: bool = False) -> ProductionReview:
    from authoring.prompts import _prompt_text
    if inspect_failed_grading:
        validate_certification(request, require_success=False)
    else:
        validate_survivor(request)
    system = _prompt_text("reviewer_production.md")
    if inspect_failed_grading:
        system = system[system.index("Approve only when"):]
        system = _prompt_text("reviewer_grading_recovery.md") + "\n\n" + system
    payload = model_request(request)
    schema = strict_schema(ProductionReview.model_json_schema())
    out.mkdir(parents=True, exist_ok=True)
    dump_json(out / "request.json", request)
    dump_json(out / "model_request.json", payload)
    dump_json(out / "schema.json", schema)
    (out / "system.txt").write_text(system + "\n")
    raw = cached_client_complete(out / "work" / "response_cache.json", system, payload, client,
                                 response_schema=schema, response_schema_name="dolphinbench_production_review")
    dump_json(out / "response.json", raw)
    response = ProductionReview.model_validate({k: v for k, v in raw.items() if not k.startswith("_")})
    checks = {row["check_id"] for row in request["grading_checks"]}
    sources = {row["message_id"] for row in request["sources"]}
    for issue in response.issues:
        if set(issue.check_ids) - checks or set(issue.source_message_ids) - sources:
            raise ValueError("review refers to an unavailable check or source message")
    dump_json(out / "decision.json", {**response.model_dump(mode="json"),
              "request_sha256": hashlib.sha256((out / "request.json").read_bytes()).hexdigest()})
    return response


def dependent_scope(response: WriterResponse, *, parts: set[str], check_ids: set[str]) -> dict[str, Any]:
    if response.test is None or parts.intersection({"planning", "system"}):
        return {"allowed_correction_fields": []}
    fields = set()
    if "request" in parts:
        fields.add("test.request")
    if "starting_state" in parts:
        fields.update(("test.existing_records", "test.new_records"))
    if parts.intersection({"checks", "evidence"}):
        fields.update(("test.checks", "test.evidence"))
    checks = response.test.checks
    known = {row.assertion["check_id"] for row in checks}
    if check_ids - known:
        raise ValueError("correction names unavailable checks")
    protected = [row for row in checks if row.assertion["check_id"] not in check_ids]
    return {"allowed_correction_fields": sorted(fields),
            "protected_check_ids": [row.assertion["check_id"] for row in protected],
            "protected_evidence_ids": sorted({eid for row in protected for eid in row.evidence_ids}),
            "dependent_evidence_correction": True}
