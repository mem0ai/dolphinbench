"""Review a new test and exercise its grading before running any assistants."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from authoring.context import dump_json
from authoring.final_trace_review import (
    FinalTraceDecision,
    FinalTraceReviewIssue,
    CurrentFinalTraceReviewResponse,
    _validate_audit_matches_request,
    _validate_issue_citations,
    build_test_review_request,
)
from authoring.prompts import REQUEST_QUALITY_REVIEW_RULES, TEST_REQUIREMENTS_RULES
from construction.runtime_model_calls import cached_client_complete
from graders.mechanical import grade_tool_trace


PREFLIGHT_SYSTEM = """Check one proposed test before any assistant executions.

Your job is to establish that the approved work was preserved, the test is executable and history-dependent, and its grading measures the necessary results fairly. You do not certify assistant performance. There are no execution traces yet; do not invent any.

Compare the final user_request with planned_work.requested_work and every planned work item. Necessary details must survive in the request or be supplied by selected source messages. Check the exact sources and full dates. Compare each planned required result with the request and sources: flag unsupported or unnecessary planned requirements as planning errors, not instructions for silently changing the approved idea.

Check the actual starting_app_data, not unselected baseline records. Check required records, recipients, identifiers, and other non-memory inputs are available. Determine whether the request or readable state actually reveals the remembered answer. authoring_evidence contains the writer's claims; check them rather than treating them as proof.

Audit every grading assertion for necessity, distinctness, completeness, and acceptance of ordinary correct alternatives. Do not expand a fact into more required details than the current work needs. Preserve every condition that changes correctness. Make sure the remembered content is checked on the right action and target. Reject checks that can be satisfied by unrelated actions or a mere body mention of a recipient. The selected tool contracts cover expected calls, not the full agent-tool inventory.

Fill the same audit used in final review: every planned-result index, grading-assertion index, fact ID, and selected tool exactly once. List the final requested actions and the checks proving they occurred. Supply an ordinary correct and a genuinely incorrect example for each assertion. A rejection must list all concrete problems and the step that owns each one; execution is not a possible owner before execution.

For user_request_requirements, give each row a grading_role and grading_reason. Use remembered_result for a history-dependent result (including a remembered search argument), target_identity for an identifier binding it to the right action target, final_effect for a requested completed action, and ordinary_detail for ordinary request details or supporting reads. The first three need checks; a result check may also prove its final effect. Ordinary details remain ungraded. Include every final effect and necessary target. Explain in each planned result's why_required_for_work what would be wrong with the requested work if that result were omitted or changed, separately from source support.

Interpret request-derived arguments using the full natural request and supplied context, not literal equality between prose and serialized values. Check whether those arguments need grading at all. For each defect, compare the approved result with the writer's request and checks to identify the first responsible step. Test genuinely different correct completions: preserve maximums and flexible choices rather than one reference schedule, and assess information across the complete action without requiring subsection repetition unless the subsection must stand alone.

Every issue must quote exact request words or cite an exact source passage. Only for a user_request issue about changed or omitted approved work, you may instead quote a continuous excerpt of planned_work.requested_work or a planned work item's work in planned_work_quote. That quote establishes the writing error; it does not make an otherwise unsupported grading requirement valid. Keep planned_work_quote empty for other issues.

If you reject the test, return reference_tool_calls_json as "[]" and grading_probes as []. Omit only these executable fixtures. The audit.grading_assertions entries must still include nonempty normal_correct_value and incorrect_or_incomplete_value examples for every assertion, including assertions you reject.

If you accept the test, provide one ordinary complete correct set of tool calls in reference_tool_calls_json, as JSON text: [{"tool":"tool_name","args":{"argument":"value"}}]. Use only expected tools and valid arguments. This is a grading fixture, not an execution and not evidence of certification. It must satisfy every check.

Also provide one grading_probes item for every assertion, using assertion_index and a reference_call_index. For a check with path args or args.<field>, correct_value_json is another normal correct value for that path, and incorrect_value_json is a plausible incomplete, contradictory, negated, or wrong-target value that must fail that check. Both are JSON-encoded values, not descriptions. The program substitutes that value into the chosen reference call, retaining its other arguments and the other calls. For checks without a path, set reference_call_index to -1 and encode complete replacement tool-call lists in both value fields. Each correct alternative must remain a valid way to complete the work. Do not make the incorrect example fail only because of malformed arguments or an unrelated omission. In particular, an incomplete body that repeats related vocabulary must still fail when it omits a necessary condition.

For check_version 2, inspect the declared comparison and generated criterion, not an assumed fallback. All checks sharing an action_id must hold on the same reference call. Separate actions need separate calls. Each probe's correct replacement must pass the complete grading set and each incorrect replacement must fail it. The program also combines two incomplete calls for an action as a negative example; splitting the correct recipient and content across different messages must fail.

Return only the supplied JSON shape.""" + "\n\n" + REQUEST_QUALITY_REVIEW_RULES + "\n\n" + TEST_REQUIREMENTS_RULES


class PreflightIssue(FinalTraceReviewIssue):
    planned_work_quote: StrictStr


class GradingProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_index: StrictInt
    reference_call_index: StrictInt
    correct_value_json: StrictStr
    incorrect_value_json: StrictStr


class PreflightResponse(CurrentFinalTraceReviewResponse):
    issues: list[PreflightIssue]
    reference_tool_calls_json: StrictStr
    grading_probes: list[GradingProbe]


def _has_current_grading_scope(response: dict[str, Any]) -> bool:
    """Older raw reports remain evidence, but cannot replace a current review."""
    audit = response.get("audit") or {}
    request_rows = audit.get("user_request_requirements") or []
    planned_rows = audit.get("planned_required_results") or []
    return bool(request_rows and planned_rows) and all(
        row.get("grading_role") and row.get("grading_reason") for row in request_rows
    ) and all(row.get("why_required_for_work") for row in planned_rows)


def _hash_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _tool_calls(value: Any, request: dict[str, Any], *, executable: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("grading examples must encode a list of tool calls")
    contracts = request["selected_tool_contracts"]
    for call in value:
        if not isinstance(call, dict) or set(call) != {"tool", "args"}:
            raise ValueError("each grading-example call must contain only tool and args")
        if call["tool"] not in contracts or not isinstance(call["args"], dict):
            raise ValueError("grading example has an unknown tool or invalid arguments")
        contract = contracts[call["tool"]]
        if set(call["args"]) - set(contract["arguments"]):
            raise ValueError("grading example names an unknown tool argument")
        if executable and set(contract.get("required_arguments") or []) - set(call["args"]):
            raise ValueError("a correct grading example is missing required tool arguments")
    return value


def _probe_calls(
    reference: list[dict[str, Any]], probe: GradingProbe, assertion: dict[str, Any],
    value_json: str, request: dict[str, Any], *, executable: bool,
) -> list[dict[str, Any]]:
    value = json.loads(value_json)
    path = assertion.get("path")
    if not path:
        if probe.reference_call_index != -1:
            raise ValueError("whole-trace grading probes must use reference_call_index -1")
        return _tool_calls(value, request, executable=executable)
    if not 0 <= probe.reference_call_index < len(reference):
        raise ValueError("grading probe names an unknown reference call")
    calls = copy.deepcopy(reference)
    call = calls[probe.reference_call_index]
    if call["tool"] != assertion["tool"]:
        raise ValueError("grading probe must change a call to the assertion's tool")
    parts = str(path).split(".")
    if parts[0] != "args":
        raise ValueError("grading probe path must begin with args")
    target: Any = call
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    if isinstance(target, list):
        target[int(parts[-1])] = value
    else:
        target[parts[-1]] = value
    return _tool_calls(calls, request, executable=executable)


def _grading_cases(response: PreflightResponse, request: dict[str, Any]) -> list[tuple[str, int, list[dict[str, Any]], bool]]:
    """Validate every example before treating the review as reusable evidence."""
    assertions = request["grading_checks"]
    indexes = [probe.assertion_index for probe in response.grading_probes]
    if sorted(indexes) != list(range(len(assertions))):
        raise ValueError("preflight must provide one grading probe per assertion exactly once")
    reference = _tool_calls(json.loads(response.reference_tool_calls_json), request, executable=True)
    cases = []
    for probe in response.grading_probes:
        index = probe.assertion_index
        assertion = assertions[index]
        cases.append((f"{index:03d}_reference", index, reference, True))
        for label, value, expected in (
            ("equivalent", probe.correct_value_json, True),
            ("incorrect", probe.incorrect_value_json, False),
        ):
            calls = _probe_calls(reference, probe, assertion, value, request, executable=expected)
            if not expected and calls == reference:
                raise ValueError("an incorrect grading example must differ from the correct reference")
            cases.append((f"{index:03d}_{label}", index, calls, expected))
    if request.get("check_version") == 2:
        cases.insert(0, ("complete_reference", -1, reference, True))
        groups: dict[str, list[GradingProbe]] = {}
        for probe in response.grading_probes:
            action = assertions[probe.assertion_index].get("action_id")
            if action and probe.reference_call_index >= 0:
                groups.setdefault(action, []).append(probe)
        for group_index, probes in enumerate(groups.values()):
            if len(probes) < 2:
                continue
            first, second = probes[:2]
            if len({probe.reference_call_index for probe in probes}) != 1:
                raise ValueError("checks for one action must probe the same reference call")
            left = _probe_calls(reference, first, assertions[first.assertion_index],
                                first.incorrect_value_json, request, executable=False)
            right = _probe_calls(reference, second, assertions[second.assertion_index],
                                 second.incorrect_value_json, request, executable=False)
            call_index = first.reference_call_index
            split = [call for index, call in enumerate(reference) if index != call_index]
            split.extend((left[call_index], right[call_index]))
            cases.append((f"split_action_{group_index:03d}", -1, split, False))
    return cases


def check_grading_examples(
    *, response: PreflightResponse, request: dict[str, Any], out: Path,
    grader: Callable[..., dict[str, Any]] = grade_tool_trace,
) -> dict[str, Any]:
    """Exercise the actual grader, saving each case for unchanged-case reuse."""
    from graders import llm_judge

    assertions = request["grading_checks"]
    cases = _grading_cases(response, request)
    root = Path(__file__).resolve().parents[1]
    grader_identity = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("graders/mechanical.py", "graders/llm_judge.py", "graders/explicit.py", "authoring/preflight.py")
    }
    judge_identity = {name: getattr(llm_judge, name) for name in (
        "BACKEND", "DEFAULT_MAX_TOKENS", "OPENAI_DEFAULT_MODEL", "OPENAI_DEFAULT_ENDPOINT",
        "AZURE_ENDPOINT", "AZURE_DEPLOYMENT", "AZURE_API_VERSION", "JUDGE_REASONING_EFFORT",
    )}
    results = []
    for name, index, calls, expected in cases:
        config = {"assertions": assertions if request.get("check_version") == 2 else [assertions[index]],
                  "today": request["evaluation_date"], "raise_on_judge_error": True}
        if request.get("check_version") == 2:
            config["check_version"] = 2
        identity = {
            "assertion": assertions[index] if index >= 0 else None, "grading_config": config, "tool_calls": calls,
            "request": request["user_request"], "date": request["evaluation_date"],
            "expected_pass": expected, "grader_code": grader_identity, "judge_config": judge_identity,
        }
        path = out / f"{name}.json"
        saved = json.loads(path.read_text()) if path.is_file() else None
        if saved is not None and saved.get("input_sha256") == _hash_json(identity):
            result = saved
        else:
            grade = grader(calls, config, test_message=request["user_request"])
            if not isinstance(grade.get("passed"), bool):
                raise ValueError("grading example returned no Boolean verdict")
            result = {
                "case": name, "assertion_index": index,
                "input_sha256": _hash_json(identity), "inputs": identity,
                "expected_pass": expected, "grade": grade,
                "matches_expected": grade["passed"] == expected,
            }
            dump_json(path, result)
        results.append(result)
    summary = {"passed": all(row["matches_expected"] for row in results), "cases": results}
    dump_json(out / "result.json", summary)
    return summary


def execute_preflight(
    *, candidate_path: Path, planned_task: Any, context: Any,
    authoring_evidence: dict[str, Any], out: Path, client: Any,
    confirm_paid_calls: bool = False,
    grader: Callable[..., dict[str, Any]] = grade_tool_trace,
    saved_response: dict[str, Any] | None = None,
    saved_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not confirm_paid_calls:
        raise ValueError("refusing preflight review and grading calls without explicit confirmation")
    request = build_test_review_request(
        candidate_path=candidate_path, planned_task=planned_task, context=context
    )
    request["authoring_evidence"] = authoring_evidence
    dump_json(out / "request.json", request)
    (out / "system.txt").write_text(PREFLIGHT_SYSTEM + "\n")
    schema = PreflightResponse.model_json_schema()
    dump_json(out / "schema.json", schema)
    reuse_review = (saved_response is not None and saved_request == request
                    and _has_current_grading_scope(saved_response))
    raw = copy.deepcopy(saved_response) if reuse_review else cached_client_complete(
        out / "work" / "response_cache.json", PREFLIGHT_SYSTEM, request, client,
        response_schema=schema, response_schema_name="dolphinbench_test_preflight",
    )
    response = PreflightResponse.model_validate({key: value for key, value in raw.items() if not key.startswith("_")})
    if response.test_id != request["test_id"]:
        raise ValueError("preflight returned the wrong test ID")
    _validate_audit_matches_request(response, request, require_grading_scope=True)
    source_issues = []
    approved_work = [planned_task.requested_work or planned_task.task_description]
    approved_work.extend(item.work for item in planned_task.work_items)
    for issue in response.issues:
        if issue.owning_step == "execution":
            raise ValueError("preflight cannot report an execution failure before execution")
        if issue.planned_work_quote:
            if issue.owning_step != "user_request" or not any(
                issue.planned_work_quote in text for text in approved_work
            ):
                raise ValueError("preflight planned-work quote does not establish a request-writing error")
        else:
            source_issues.append(issue)
    _validate_issue_citations(response.model_copy(update={"issues": source_issues}), request)
    decision = FinalTraceDecision.model_validate({
        "test_id": response.test_id, "accept": response.accept,
        "issues": [issue.model_dump(exclude={"planned_work_quote"}) for issue in response.issues],
        "audit": response.audit.model_dump(mode="json"),
    })
    if decision.accept:
        _grading_cases(response, request)
    elif json.loads(response.reference_tool_calls_json) != [] or response.grading_probes:
        raise ValueError("a rejected preflight must not contain grading examples")
    dump_json(out / "response.json", raw)
    dump_json(out / "decision.json", decision.model_dump(mode="json"))
    result = {
        "candidate_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
        "decision": decision.model_dump(mode="json"),
        "passed": False,
    }
    if decision.accept:
        checks = check_grading_examples(response=response, request=request, out=out / "grading_examples", grader=grader)
        result["grading_examples"] = str(out / "grading_examples" / "result.json")
        result["passed"] = checks["passed"]
        if not checks["passed"]:
            result["reason"] = "the grading disagrees with the reviewer's correct or incorrect examples"
    dump_json(out / "result.json", result)
    return result
