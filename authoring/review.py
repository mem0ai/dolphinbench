"""One short review contract before and after certification; no public command."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator

from authoring.complete_test import decode_json, strict_schema
from authoring.context import dump_json
from authoring.prompts import TEST_REVIEW_SYSTEM
from construction.runtime_model_calls import cached_client_complete
from graders.mechanical import grade_tool_trace


class ReviewEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location: StrictStr
    quote: StrictStr


class ExampleCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: StrictStr
    args_json: StrictStr


class GradingCounterexample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calls: list[ExampleCall] = Field(min_length=1)
    check_id: StrictStr
    predicted_pass: StrictBool
    required_pass: StrictBool


class MissingCheckBasis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["remembered_result", "final_action", "target"]
    tool: StrictStr
    action_id: StrictStr
    source_evidence: list[ReviewEvidence]
    existing_check_ids: list[StrictStr]


class ReviewIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part: Literal["planning", "request", "starting_state", "checks", "execution", "system"]
    location: StrictStr
    problem: StrictStr
    evidence: list[ReviewEvidence]
    required_change: StrictStr
    counterexample: GradingCounterexample | None = None
    missing_check: MissingCheckBasis | None = None


class ReviewExamples(BaseModel):
    model_config = ConfigDict(extra="forbid")

    correct_calls: list[ExampleCall] = Field(min_length=1)
    incorrect_calls: list[ExampleCall]
    expected_failed_check_ids: list[StrictStr] = Field(min_length=1)


class ReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject", "pending"]
    issues: list[ReviewIssue]
    examples: ReviewExamples | None

    @model_validator(mode="after")
    def _decision_matches_issues(self) -> "ReviewResponse":
        if (self.decision == "approve") != (not self.issues):
            raise ValueError("approve needs no issues; reject and pending need issues")
        if self.decision != "approve" and self.examples is not None:
            raise ValueError("only an approving review can supply examples")
        for issue in self.issues:
            if not all(text.strip() for text in (issue.location, issue.problem, issue.required_change)):
                raise ValueError("each issue needs a location, problem, and required change")
        return self


class ReviewPendingError(ValueError):
    """A review or grader disagreement is not an idea rejection."""


def pointer_value(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ValueError(f"review location must be a JSON Pointer: {pointer!r}")
    for encoded in pointer.split("/")[1:]:
        part = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise ValueError(f"review location is not supplied: {pointer!r}")
    return value


def validate_review(response: ReviewResponse, request: dict[str, Any]) -> None:
    before = request["phase"] == "before_oracle"
    if (response.examples is not None) != (before and response.decision == "approve"):
        raise ValueError("examples are required only for approval before the oracle")
    for issue in response.issues:
        if before and issue.part == "execution":
            raise ValueError("execution cannot own a failure before the oracle")
        if issue.part == "execution":
            segments = issue.location.split("/")
            if len(segments) >= 4 and segments[1] == "executions" and segments[3] == "grade":
                raise ReviewPendingError(
                    "a grading-result location cannot own an assistant execution correction; "
                    "locate incorrect assistant work at its call or response, an incorrect "
                    "criterion at grading_checks, or a misapplied judge result as system/pending"
                )
        if not issue.evidence and response.decision != "pending":
            raise ValueError("a rejection needs supplied evidence; missing evidence is pending")
        if issue.part == "system" and response.decision != "pending":
            raise ValueError("a system error must remain pending")
        if response.decision != "pending":
            pointer_value(request, issue.location)
        for evidence in issue.evidence:
            value = pointer_value(request, evidence.location)
            if not evidence.quote.strip():
                raise ValueError("review evidence must quote supplied material")
            if isinstance(value, str):
                matches = evidence.quote in value
            else:
                matches = decode_json(evidence.quote, evidence.location) == value
            if not matches:
                raise ValueError(f"review quote does not match {evidence.location}")
        if request.get("review_contract_version") == 1 and response.decision == "reject":
            _validate_grading_issue(issue, request)
    if not before and response.decision == "approve":
        validate_certification(request)


def _validate_grading_issue(issue: ReviewIssue, request: dict[str, Any]) -> None:
    if issue.part != "checks":
        if issue.counterexample is not None or issue.missing_check is not None:
            raise ReviewPendingError("grading evidence belongs only to a checks issue")
        return
    if (issue.counterexample is None) == (issue.missing_check is None):
        raise ReviewPendingError("a checks rejection needs exactly one counterexample or missing-check basis")
    checks = {row["check_id"]: row for row in request["grading_checks"]}
    if issue.counterexample is not None:
        example = issue.counterexample
        check = checks.get(example.check_id)
        if check is None or not any(
            issue.location == f"/grading_checks/{index}" or issue.location.startswith(f"/grading_checks/{index}/")
            or issue.location == f"/authoring_evidence/checks/{index}" or issue.location.startswith(f"/authoring_evidence/checks/{index}/")
            for index, row in enumerate(request["grading_checks"]) if row["check_id"] == example.check_id
        ):
            raise ReviewPendingError("counterexample must identify the existing check at the issue location")
        if example.predicted_pass == example.required_pass:
            raise ReviewPendingError("a grading defect must predict a result different from the required result")
        _example_calls(example.calls, request)
        return
    basis = issue.missing_check
    if issue.location != "/grading_checks" or basis.tool not in request["selected_tool_contracts"] or not basis.action_id.strip():
        raise ReviewPendingError("a missing check needs the check-list location, a supplied tool, and an action ID")
    if len(set(basis.existing_check_ids)) != len(basis.existing_check_ids) or set(basis.existing_check_ids) - checks.keys():
        raise ReviewPendingError("missing-check basis names unknown or duplicate checks")
    for evidence in basis.source_evidence:
        if not evidence.location.startswith("/sources/") or not evidence.location.endswith("/text"):
            raise ReviewPendingError("remembered requirements must cite original source messages")
        value = pointer_value(request, evidence.location)
        if not evidence.quote.strip() or evidence.quote not in value:
            raise ReviewPendingError("missing-check source quote does not match")
    action_checks = {key for key, row in checks.items()
                     if row.get("action_id") == basis.action_id and row["tool"] == basis.tool}
    if basis.role == "remembered_result":
        if not basis.source_evidence:
            raise ReviewPendingError("a current-request quote alone cannot establish a remembered requirement")
    elif basis.role == "target":
        memory_checks = {row["assertion"]["check_id"] for row in request["authoring_evidence"]["checks"] if row["evidence_ids"]}
        if not set(basis.existing_check_ids).intersection(action_checks & memory_checks):
            raise ReviewPendingError("a target check must identify the same action receiving a remembered result")
    else:
        if basis.action_id in {row.get("action_id") for row in checks.values()}:
            raise ReviewPendingError("checks already prove this action; extra content is not a missing final action")
        effect = request["selected_tool_contracts"][basis.tool].get("state_effect") or {}
        if not effect.get("writes_state_keys"):
            raise ReviewPendingError("a missing final action must name a tool with a supplied state-changing effect")
    if basis.role in {"target", "final_action"} and not any(e.location == "/user_request" for e in issue.evidence):
        raise ReviewPendingError("a requested action or target needs current-request evidence")


def grading_review_contract(semantic_judge_version: int = 1) -> dict[str, Any]:
    from graders.llm_judge import ACTION_JUDGE_SYSTEM_PROMPT, FIELD_JUDGE_SYSTEM_PROMPT

    return {
        "review_contract_version": 1,
        "grading_semantics": {
            "field_judge_system": ACTION_JUDGE_SYSTEM_PROMPT if semantic_judge_version == 2 else FIELD_JUDGE_SYSTEM_PROMPT,
            "field_judge_inputs": ["extracted field value", "criterion", "current request", "complete arguments of the SAME call", "date"],
            "missing_path": "A missing field path fails before semantic judging; an existing field does not hide other call arguments.",
            "action_matching": "All checks sharing an action_id must pass on one call. Different action_ids require distinct calls.",
            "scope": "Grade remembered results, their targets, and proof of final actions. Extra request-supplied content in the same email is not another action.",
        },
    }


def validate_certification(request: dict[str, Any], *, require_success: bool = True) -> None:
    result = request["certification_result"]
    shots = request["executions"]
    if (require_success and not result["valid"]) or len(shots) != 4:
        raise ValueError("approval requires successful four-attempt certification")
    flags = [shot.get("with_memory") for shot in shots]
    if any(type(flag) is not bool for flag in flags) or flags.count(True) != 2 or flags.count(False) != 2:
        raise ValueError("certification needs two attempts in each condition")
    assertions = request["grading_checks"]
    for shot in shots:
        if shot.get("error") or shot.get("oracle_errors") or type(shot.get("passed")) is not bool:
            raise ValueError("an execution error is not a no-history failure")
        grade = shot.get("grade")
        if not isinstance(grade, dict) or grade.get("passed") is not shot["passed"]:
            raise ValueError("execution and grading verdicts disagree")
        details = grade.get("details")
        if not isinstance(details, list) or len(details) != len(assertions):
            raise ValueError("certification must preserve every check result")
        for detail, assertion in zip(details, assertions, strict=True):
            if detail.get("assertion") != assertion or type(detail.get("ok")) is not bool:
                raise ValueError("certification details do not match the candidate checks")
        if grade["passed"] is not all(row["ok"] for row in details):
            raise ValueError("overall grading verdict disagrees with its check results")
        agent_input = request.get("agent_inputs", {}).get(shot.get("agent_input_id"))
        if not isinstance(agent_input, dict) or not isinstance(agent_input.get("tools"), list):
            raise ValueError("certification is missing the exact assistant inputs")
        messages = agent_input.get("messages")
        if (not isinstance(messages, list) or len(messages) != 2
                or messages[1] != {"role": "user", "content": request["user_request"]}):
            raise ValueError("saved assistant request differs from the test")
        state_hash = hashlib.sha256(json.dumps(
            request["starting_app_data"], sort_keys=True, ensure_ascii=False
        ).encode()).hexdigest()
        if agent_input.get("starting_state_sha256") != state_hash:
            raise ValueError("saved assistant state differs from the test")
        results = shot.get("tool_results")
        calls = shot.get("tool_calls")
        if not isinstance(results, list) or not isinstance(calls, list) or len(results) != len(calls):
            raise ValueError("certification is missing tool results")
        for index, (call, observed) in enumerate(zip(calls, results, strict=True)):
            if (observed.get("call_index") != index or "result" not in observed
                    or any(call.get(key) != observed.get(key) for key in ("tool", "args"))):
                raise ValueError("saved tool results do not match the executed calls")
        if require_success and shot["passed"] is not shot["with_memory"]:
            raise ValueError("both history attempts must pass and both no-history attempts must fail")
    counts = [sum(shot["passed"] for shot in shots if shot["with_memory"] is flag) for flag in (True, False)]
    if counts != [result["with_history_pass_count"], result["without_history_pass_count"]]:
        raise ValueError("certification counts do not match the four attempts")
    if result["valid"] is not (counts == [2, 0]):
        raise ValueError("certification valid flag disagrees with the four attempts")
    if not require_success:
        attempt_ids = [shot.get("attempt_id") for shot in shots]
        if any(not isinstance(value, str) or not value for value in attempt_ids) or len(set(attempt_ids)) != 4:
            raise ValueError("four distinct completed attempt IDs are required")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _example_calls(rows: list[ExampleCall], request: dict[str, Any]) -> list[dict[str, Any]]:
    from authoring.preflight import _tool_calls
    calls = [
        {"tool": row.tool, "args": decode_json(row.args_json, f"example call {index}.args_json")}
        for index, row in enumerate(rows)
    ]
    return _tool_calls(calls, request, executable=True)


def _grading_identity(request: dict[str, Any]) -> dict[str, Any]:
    from graders import llm_judge
    root = Path(__file__).resolve().parents[1]
    return {
        "grading_config": {"check_version": 2, "assertions": request["grading_checks"],
                           "today": request["evaluation_date"], "raise_on_judge_error": True,
                           **({"semantic_judge_version": request["semantic_judge_version"]}
                              if "semantic_judge_version" in request else {})},
        "request": request["user_request"],
        "grader_code": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("graders/mechanical.py", "graders/explicit.py", "graders/llm_judge.py", "authoring/review.py")
        },
        "judge_config": {name: getattr(llm_judge, name) for name in (
            "BACKEND", "DEFAULT_MAX_TOKENS", "OPENAI_DEFAULT_MODEL", "OPENAI_DEFAULT_ENDPOINT",
            "AZURE_ENDPOINT", "AZURE_DEPLOYMENT", "AZURE_API_VERSION", "JUDGE_REASONING_EFFORT",
        )},
    }


def check_rejection_examples(*, response: ReviewResponse, request: dict[str, Any], out: Path,
                             grader: Callable[..., dict[str, Any]] = grade_tool_trace) -> None:
    for index, issue in enumerate(response.issues):
        example = issue.counterexample
        if example is None:
            continue
        calls = _example_calls(example.calls, request)
        identity = _grading_identity(request)
        inputs = {**identity, "tool_calls": calls}
        path = out / f"issue_{index}.json"
        saved = json.loads(path.read_text()) if path.is_file() else None
        grade = saved["grade"] if saved and saved.get("input_sha256") == _hash(inputs) else grader(
            calls, identity["grading_config"], test_message=request["user_request"])
        if type(grade.get("passed")) is not bool:
            raise ReviewPendingError("counterexample grading returned no Boolean verdict")
        detail = next((row for row in grade.get("details", []) if row.get("name") == example.check_id), None)
        if detail is None or type(detail.get("ok")) is not bool:
            raise ReviewPendingError("counterexample grading omitted the named check")
        # A sibling's same-action failure must not substantiate this claim.
        comparisons = detail.get("call_evaluations") or []
        if any(type(row.get("ok")) is not bool for row in comparisons):
            raise ReviewPendingError("counterexample has incomplete call evaluations")
        observed = any(row["ok"] for row in comparisons) if comparisons else detail["ok"]
        matches = observed is example.predicted_pass
        dump_json(path, {"input_sha256": _hash(inputs), "inputs": inputs, "grade": grade,
                         "predicted_pass": example.predicted_pass, "required_pass": example.required_pass,
                         "observed_check_pass": observed, "claim_reproduced": matches})
        if not matches:
            raise ReviewPendingError(f"reviewer counterexample disagrees with actual grader for {example.check_id}")


def check_review_examples(*, response: ReviewResponse, request: dict[str, Any], out: Path,
                          grader: Callable[..., dict[str, Any]] = grade_tool_trace) -> dict[str, Any]:
    examples = response.examples
    if examples is None:
        raise ValueError("an approving preflight review needs examples")
    correct = _example_calls(examples.correct_calls, request)
    incorrect = _example_calls(examples.incorrect_calls, request)
    if correct == incorrect:
        raise ValueError("the incorrect example must differ from the correct example")
    expected_ids = examples.expected_failed_check_ids
    memory_ids = {
        row["assertion"]["check_id"] for row in request["authoring_evidence"]["checks"]
        if row["evidence_ids"]
    }
    if len(set(expected_ids)) != len(expected_ids) or set(expected_ids) - memory_ids:
        raise ValueError("incorrect examples must name distinct memory-dependent check IDs")
    identity = _grading_identity(request)
    results = []
    for name, calls, expected in (("correct", correct, True), ("incorrect", incorrect, False)):
        inputs = {**identity, "tool_calls": calls}
        path = out / f"{name}.json"
        saved = json.loads(path.read_text()) if path.is_file() else None
        if saved is not None and saved.get("input_sha256") == _hash(inputs):
            grade = saved["grade"]
        else:
            grade = grader(calls, identity["grading_config"], test_message=request["user_request"])
            if type(grade.get("passed")) is not bool:
                raise ReviewPendingError("example grading returned no Boolean verdict")
            dump_json(path, {"input_sha256": _hash(inputs), "inputs": inputs, "grade": grade})
        matches = grade.get("passed") is expected
        if not expected:
            by_id = {row.get("name"): row for row in grade.get("details") or []}
            for check_id in expected_ids:
                detail = by_id.get(check_id) or {}
                # Same-action failure marks every grouped check false. Ensure
                # the named remembered check itself rejects the example too.
                matches = matches and detail.get("ok") is False and not any(
                    row.get("ok") is True for row in detail.get("call_evaluations") or []
                )
        results.append({"case": name, "expected_pass": expected, "grade": grade, "matches_expected": matches})
    result = {"passed": all(row["matches_expected"] for row in results), "cases": results,
              "expected_failed_check_ids": expected_ids}
    dump_json(out / "result.json", result)
    return result


def review_decision(response: ReviewResponse, request: dict[str, Any]) -> Any:
    from authoring.final_trace_review import FinalTraceDecision

    if response.decision == "pending":
        raise ReviewPendingError("; ".join(issue.problem for issue in response.issues))
    owners = {"request": "user_request", "starting_state": "starting_app_data", "checks": "grading_checks"}
    return FinalTraceDecision(
        test_id=request["test_id"], accept=response.decision == "approve",
        issues=[{
            "owning_step": owners.get(issue.part, issue.part),
            "specific_problem": f"{issue.location}: {issue.problem}",
            "required_correction": issue.required_change,
            "request_requirement_quote": next((item.quote for item in issue.evidence if item.location == "/user_request"), ""),
            "source_citations": [{
                "source_session_id": request["sources"][int(item.location.split("/")[2])]["source_session_id"],
                "source_text_quote": item.quote,
            } for item in issue.evidence if item.location.startswith("/sources/") and item.location.endswith("/text")],
        } for issue in response.issues],
    )


def review_response_schema() -> dict[str, Any]:
    """Return the provider shape; evidence quotes are derived from locations."""
    schema = strict_schema(ReviewResponse.model_json_schema())
    evidence = schema["$defs"]["ReviewEvidence"]
    evidence["properties"].pop("quote")
    evidence["required"].remove("quote")
    return schema


def materialize_review_quotes(raw: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Copy authoritative evidence values into the normalized review object."""
    normalized = copy.deepcopy(raw)

    def materialize(rows: Any) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            location = row.get("location")
            value = pointer_value(request, location) if isinstance(location, str) else None
            row["quote"] = value if isinstance(value, str) else json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )

    for issue in normalized.get("issues", []):
        if not isinstance(issue, dict):
            continue
        materialize(issue.get("evidence"))
        missing = issue.get("missing_check")
        if isinstance(missing, dict):
            materialize(missing.get("source_evidence"))
    return normalized


def execute_review(*, request: dict[str, Any], out: Path, client: Any,
                   confirm_paid_calls: bool = False,
                   saved_response: dict[str, Any] | None = None,
                   saved_request: dict[str, Any] | None = None) -> ReviewResponse:
    if not confirm_paid_calls:
        raise ValueError("refusing review model calls without explicit confirmation")
    schema = review_response_schema()
    out.mkdir(parents=True, exist_ok=True)
    dump_json(out / "request.json", request)
    dump_json(out / "schema.json", schema)
    (out / "system.txt").write_text(TEST_REVIEW_SYSTEM + "\n")
    # The reusable response binds the actual request, schema, and prompt, not
    # just an earlier approval Boolean.
    identity = _hash({"request": request, "schema": schema, "system": TEST_REVIEW_SYSTEM})
    reuse = saved_response is not None and saved_request == request and saved_response.get("_review_identity") == identity
    raw = copy.deepcopy(saved_response) if reuse else cached_client_complete(
        out / "work" / "response_cache.json", TEST_REVIEW_SYSTEM, request, client,
        response_schema=schema, response_schema_name="dolphinbench_test_review_v4",
    )
    raw["_review_identity"] = identity
    dump_json(out / "response.json", raw)
    normalized = materialize_review_quotes(
        {key: value for key, value in raw.items() if not key.startswith("_")}, request,
    )
    response = ReviewResponse.model_validate(normalized)
    validate_review(response, request)
    if response.decision == "reject" and request.get("review_contract_version") == 1:
        check_rejection_examples(response=response, request=request, out=out / "rejection_examples")
    dump_json(out / "review.json", response.model_dump(mode="json"))
    decision = review_decision(response, request)
    dump_json(out / "decision.json", decision.model_dump(mode="json"))
    return response
