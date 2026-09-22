"""Review and executable examples for human-approved legacy release corrections."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from authoring.complete_test import source_records, strict_schema
from authoring.prompts import TEST_REVIEW_SYSTEM
from authoring.review import (
    ExampleCall, ReviewExamples, ReviewIssue, ReviewResponse, _example_calls,
    grading_review_contract, materialize_review_quotes, validate_review,
    validate_certification,
)
from authoring.revision_execution import run_once
from graders.mechanical import grade_tool_trace


REVISION_REVIEW_SYSTEM = TEST_REVIEW_SYSTEM + """

This is an exact human-approved revision of an existing legacy release, not new
test authoring. planned_work contains the original request and the exact approved
edits; inspect their source support. Do not invent an additional repair. Report
any failure and stop. Historical checks without action_id retain independent-call
grading; only declared action_id groups use same-call grading.

Before certification, provide one complete correct_calls reference and exactly
one variant for every required_example_check_ids entry. Each variant contains
complete correct_calls with an ordinary equivalent completion and complete
incorrect_calls that omit, contradict, or misapply the result at that named
check. The named check itself must reject the negative, not merely a sibling.
For every required_split_action_ids entry, provide a split_actions example:
separate individually incomplete calls collectively satisfy all the action's
checks, but no individual call satisfies the whole action. Keep valid arguments.
After certification, return examples as null. Original saved executions remain
historical evidence; do not pretend missing original settings were reconstructed.
"""


class RevisionVariant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check_id: StrictStr
    correct_calls: list[ExampleCall] = Field(min_length=1)
    incorrect_calls: list[ExampleCall] = Field(min_length=1)


class SplitActionExample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: StrictStr
    calls: list[ExampleCall] = Field(min_length=2)


class RevisionExamples(BaseModel):
    model_config = ConfigDict(extra="forbid")
    correct_calls: list[ExampleCall] = Field(min_length=1)
    variants: list[RevisionVariant] = Field(min_length=1)
    split_actions: list[SplitActionExample]


class RevisionReviewResponse(ReviewResponse):
    examples: RevisionExamples | None


def _comparison(check: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in check.items()
            if key not in {"check_id", "action_id", "name"} and value is not None}


def build_revision_review_request(
    *, original: dict[str, Any], candidate: dict[str, Any], approved_edit: dict[str, Any], context: Any,
    legacy_execution_note: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks = copy.deepcopy(candidate["grade"]["config"]["assertions"])
    old_checks = [_comparison(check) for check in original["grade"]["config"]["assertions"]]
    focus = []
    for index, check in enumerate(checks):
        check.setdefault("check_id", f"legacy_{int(candidate['id']):03d}_{index:03d}")
        if _comparison(check) not in old_checks or candidate["test"] != original["test"]:
            focus.append(check["check_id"])
    if not focus:
        raise ValueError("revision must identify a changed check or request to validate")
    version = candidate["grade"]["config"].get("check_version", 1)
    split_actions = sorted({c["action_id"] for c in checks if c.get("action_id")}) if any(
        edit.get("operation") == "bind_target_and_content_same_call"
        for edit in approved_edit["edits_in_order"]
    ) else []
    semantics = grading_review_contract()["grading_semantics"]
    if version == 1:
        semantics["action_matching"] = "Historical version-1 checks independently select matching calls; no implicit action grouping."
    return {
        "review_contract_version": "exact_release_revision_1", "phase": "before_oracle",
        "test_id": int(candidate["id"]), "evaluation_date": candidate["narrative_anchor_date"],
        "planned_work": {"original_published_request": original["test"],
                         "selected_fact_ids": candidate["load_bearing_facts"],
                         "approved_exact_edits": copy.deepcopy(approved_edit["edits_in_order"]),
                         "approved_correction_reason": approved_edit["reason"]},
        "user_request": candidate["test"], "starting_app_data": copy.deepcopy(candidate["mock_state"]),
        "selected_tool_contracts": {tool: copy.deepcopy(context.tools[tool])
                                    for tool in candidate["expected_tool_calls"]},
        "sources": source_records(context, set(candidate["load_bearing_facts"])),
        "grading_checks": checks, "check_version": version,
        "exact_grading_config": copy.deepcopy(candidate["grade"]["config"]),
        "grading_semantics": semantics, "required_example_check_ids": focus,
        "required_split_action_ids": split_actions, "additional_corrections_allowed": 0,
        "legacy_execution_note": copy.deepcopy(legacy_execution_note),
    }


def grading_identity() -> dict[str, Any]:
    from graders import llm_judge
    root = Path(__file__).resolve().parents[1]
    return {
        "code": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in (
            "graders/mechanical.py", "graders/explicit.py", "graders/llm_judge.py",
            "authoring/revision_review.py", "authoring/revision_execution.py",
            "harness/paid_budget.py", "harness/durable_json.py", "harness/environment.py",
        )},
        "judge": {name: getattr(llm_judge, name) for name in (
            "BACKEND", "DEFAULT_MAX_TOKENS", "OPENAI_DEFAULT_MODEL", "OPENAI_DEFAULT_ENDPOINT",
            "AZURE_ENDPOINT", "AZURE_DEPLOYMENT", "AZURE_API_VERSION", "JUDGE_REASONING_EFFORT",
        )},
    }


def _named_detail(grade: dict[str, Any], request: dict[str, Any], check_id: str) -> dict[str, Any]:
    index = next(i for i, c in enumerate(request["grading_checks"]) if c["check_id"] == check_id)
    details = grade.get("details")
    if not isinstance(details, list) or len(details) != len(request["grading_checks"]):
        raise ValueError("revision example grading omitted check results")
    detail = details[index]
    if type(detail.get("ok")) is not bool:
        raise ValueError("revision example has no Boolean check result")
    if request["check_version"] == 2 and detail.get("name") != check_id:
        raise ValueError("revision example returned a different check identity")
    return detail


def _comparison_passed(detail: dict[str, Any]) -> bool:
    comparisons = detail.get("call_evaluations")
    if comparisons:
        if any(type(row.get("ok")) is not bool for row in comparisons):
            raise ValueError("revision example has incomplete per-call comparisons")
        return any(row["ok"] for row in comparisons)
    return detail["ok"]


def check_revision_examples(
    *, response: RevisionReviewResponse, request: dict[str, Any], out: Path,
    grader: Callable[..., dict[str, Any]] = grade_tool_trace,
) -> dict[str, Any]:
    examples = response.examples
    if response.decision != "approve" or examples is None:
        raise ValueError("revision examples require an approving preflight")
    actual_ids = [v.check_id for v in examples.variants]
    if sorted(actual_ids) != sorted(request["required_example_check_ids"]):
        raise ValueError("revision examples must cover every changed check exactly once")
    if sorted(v.action_id for v in examples.split_actions) != request["required_split_action_ids"]:
        raise ValueError("revision examples must cover every new same-call action")
    identity = grading_identity()

    def grade(name: str, calls: list[ExampleCall]) -> dict[str, Any]:
        tool_calls = _example_calls(calls, request)
        inputs = {"tool_calls": tool_calls, "grading_config": request["exact_grading_config"],
                  "request": request["user_request"], "grading": identity}
        result = run_once(directory=out / name, identity=inputs, action=lambda: grader(
            tool_calls, {**request["exact_grading_config"], "raise_on_judge_error": True},
            test_message=request["user_request"],
        ))
        if type(result.get("passed")) is not bool or result.get("diagnostic"):
            raise ValueError("revision example has incomplete or diagnostic grading")
        return result

    results = [{"case": "reference", "matches_expected": grade("reference", examples.correct_calls)["passed"]}]
    for index, variant in enumerate(examples.variants):
        if variant.correct_calls == variant.incorrect_calls:
            raise ValueError("revision example negative must differ from its correct alternative")
        correct = grade(f"variant_{index}_correct", variant.correct_calls)
        incorrect = grade(f"variant_{index}_incorrect", variant.incorrect_calls)
        named = _named_detail(incorrect, request, variant.check_id)
        results.append({"case": variant.check_id, "matches_expected": (
            correct["passed"] and not incorrect["passed"] and not _comparison_passed(named)
        )})
    for index, example in enumerate(examples.split_actions):
        split = grade(f"split_{index}", example.calls)
        action_ids = [c["check_id"] for c in request["grading_checks"] if c.get("action_id") == example.action_id]
        individual = [_comparison_passed(_named_detail(split, request, check_id)) for check_id in action_ids]
        results.append({"case": f"split:{example.action_id}",
                        "matches_expected": bool(action_ids) and all(individual) and not split["passed"]})
    return {"passed": all(row["matches_expected"] for row in results), "cases": results}


def execute_revision_review(
    *, request: dict[str, Any], out: Path, client: Any, confirm_paid_calls: bool = False,
    system_override: str | None = None,
) -> RevisionReviewResponse:
    if not confirm_paid_calls:
        raise ValueError("revision review requires explicit paid-call approval")
    schema = strict_schema(RevisionReviewResponse.model_json_schema())
    evidence_schema = schema["$defs"]["ReviewEvidence"]
    evidence_schema["properties"].pop("quote")
    evidence_schema["required"].remove("quote")
    phase_bound = (request["phase"] == "before_oracle"
                   and request.get("review_context", {}).get("review_format") == "phase_bound_v1")
    if phase_bound:
        from authoring.revision_review_recovery import phase_bound_preflight_schema
        schema = phase_bound_preflight_schema(schema)
    system = REVISION_REVIEW_SYSTEM if system_override is None else system_override
    identity = {"system": system, "request": request, "schema": schema,
                "model": client.model, "reasoning_effort": client.reasoning_effort,
                "max_output_tokens": getattr(client, "max_output_tokens", None)}
    raw = run_once(directory=out / "model", identity=identity, action=lambda: client.complete(
        system, request, response_schema=schema,
        response_schema_name="exact_release_revision_review",
        call_path=out / "model" / "result.json",
    ))
    payload = raw["review"] if phase_bound else raw
    normalized = materialize_review_quotes({k: v for k, v in payload.items() if not k.startswith("_")}, request)
    response = RevisionReviewResponse.model_validate(normalized)
    common = ReviewResponse(decision=response.decision, issues=response.issues, examples=(
        ReviewExamples(correct_calls=response.examples.correct_calls,
                       incorrect_calls=response.examples.variants[0].incorrect_calls,
                       expected_failed_check_ids=[response.examples.variants[0].check_id])
        if response.examples is not None else None
    ))
    if request["phase"] == "after_oracle" and response.decision == "approve":
        if response.examples is not None:
            raise ValueError("final revision review must not supply examples")
        validate_revision_certification(request)
    else:
        validate_review(common, request)
    # A rejection stops this exact repair; it never grants another edit or run.
    return response


def validate_revision_certification(request: dict[str, Any]) -> None:
    """Keep full-input validation for new runs; apply only the recorded legacy exception."""
    exact = {**request, "grading_checks": request["exact_grading_config"]["assertions"]}
    legacy = request.get("legacy_execution_note")
    if legacy is None:
        validate_certification(exact)
        return
    if (legacy.get("approved") is not True
            or f"{int(request['test_id']):03d}" not in legacy.get("test_ids", [])
            or not legacy.get("limitation")):
        raise ValueError("legacy certification has no explicit recorded exception")
    result, shots = request["certification_result"], request["executions"]
    if (result.get("valid") is not True or len(shots) != 4
            or sum(s.get("with_memory") is True for s in shots) != 2
            or sum(s.get("with_memory") is False for s in shots) != 2
            or result.get("with_history_pass_count") != 2
            or result.get("without_history_pass_count") != 0):
        raise ValueError("legacy approval requires two history passes and two no-history failures")
    assertions = exact["grading_checks"]
    for shot in shots:
        grade = shot.get("grade") or {}
        details = grade.get("details")
        if (shot.get("error") or shot.get("oracle_errors") or grade.get("diagnostic")
                or type(shot.get("passed")) is not bool
                or shot["passed"] is not shot["with_memory"]
                or grade.get("passed") is not shot["passed"]
                or not isinstance(details, list) or len(details) != len(assertions)):
            raise ValueError("legacy certification has an error or incomplete grading")
        for detail, assertion in zip(details, assertions, strict=True):
            if detail.get("assertion") != assertion or type(detail.get("ok")) is not bool:
                raise ValueError("legacy certification results differ from the exact revised checks")
        if grade["passed"] is not all(d["ok"] for d in details):
            raise ValueError("legacy certification grade contradicts its check results")
