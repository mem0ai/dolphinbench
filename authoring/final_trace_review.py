"""Inspect why one four-run-certified DolphinBench candidate passed."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from authoring.context import CheckpointContext, dump_json
from authoring.models import PlannedTask
from authoring.prompts import REQUEST_QUALITY_REVIEW_RULES, TEST_REQUIREMENTS_RULES
from construction.llm import AzureJsonClient
from construction.runtime_model_calls import cached_client_complete
from harness.test_spec_schema import TestSpec


FINAL_TRACE_REVIEW_SYSTEM = """Inspect one candidate test and its four saved executions.

The candidate may have passed or failed its four-run certification. Read certification_result and every execution. Accept only a candidate that passed twice with the cited source messages and failed twice without those messages. When certification failed, identify whether the exact problem began in the grading checks, user request, test design, starting app data, or agent execution. Do not assume that every failed run requires redesigning the test.

Accept the test only when all of these statements are true:
1. The user asks for realistic new work, not a standalone question about an old message.
2. The request and readable app state do not reveal the remembered answer.
3. The exact source messages support the required result on the evaluation date. Later information does not replace it.
4. History changes a necessary observable result. This may be a tool choice, any tool argument, or required content, tone, framing, or scope inside the same tool call.
5. Both runs without history fail because the earlier information is absent, not because of a tool error, malformed state, or unrelated model failure.
6. Every correct way to complete the user's request must pass every grading check. Consider another normal correct answer before accepting the test.
7. An exact comparison is valid only when the application requires that specific stored value. Names, business names, places, explanations, and other prose must accept different wording that means the same thing. A regular expression must not force one spelling or sentence. "Blue Bottle Coffee" must pass a check that requires Blue Bottle.
8. A check on one tool argument may require only information that belongs in that argument. A message sent to @sarah does not need to repeat "Sarah" in its body.
9. Check only information from the earlier messages that the assistant needs for this request. Do not require unrelated details from the same fact or source message. A question about whether a temporary logging query is an ongoing cost must not require unrelated customer-usage or vendor-cost figures.
10. Every grading check must measure one necessary result without measuring the same result twice. Check every result whose correct value, content, or choice depends on the earlier messages. Also prove that every requested final action occurred when it changes app state or has an external effect. A result check on that action already proves it occurred; otherwise require one tool-used check. Do not require a separate check for a supporting read unless a remembered result determines what it must retrieve or how it must be performed. Do not check an ordinary detail supplied by the new request or current app state unless it is needed to prove that a remembered result was applied to the right recipient, existing record, or action target. A request-supplied label or title for a newly created document or record is an ordinary detail; do not grade it when checks on the create action and its required content already prove the requested creation. To verify fact coverage, compare planned_work.required_results, source_evidence, and grading_checks. Published grading assertions do not contain fact IDs, so do not require a fact ID inside an assertion. Each selected fact must still change at least one required result, and the grading checks must test that result. Reject redundant checks, including a negative check when a positive check already proves the same choice. Use an exact call count only when duplicates would make the action wrong. Use separate checks for separate remembered results.
11. Compare each required remembered detail with both with-history traces. Do not rely only on the numerical gate or a single trace.
If an execution omits a required detail but the grading checks marked that execution as passing, choose `grading_checks`: the checks failed to measure the required result. Choose `execution` only when the checks correctly reject the output and a fresh agent run is needed.
12. Reject a phrase that asks the assistant to recover an unstated answer from unseen history, such as "my usual", "my normal", "my preferred", "as we discussed", "use what I told you", "already established", "from the context available to you", or "based on the background context". Do not reject ordinary uses of words such as "normal", "previous", and "earlier" when they identify real present-day work.
13. Reject stale temporal use. Compare the fact's full date and stated scope with the evaluation date; a fact limited to August or September 2025 does not apply merely because the evaluation date is before September 18, 2026.
14. Reject a request that tells the assistant to omit graded content, leave a required value, owner, or boundary blank, use TBD, or wait for later confirmation. Keeping an answer out of the user's request means the user does not state it; the assistant must still determine and include it in the completed work.
15. Read each field_llm_judge question with the user's request and the complete arguments of the same action being checked. Use that context to understand names, pronouns, abbreviations, and what the action is for; do not use another action as evidence. Accept different wording that communicates the same required meaning. Do not require a message to repeat its recipient's name. Fail when the completed action omits, contradicts, or leaves unclear a required meaning. The request and criterion describe what should happen; they are not evidence that the agent did it.
16. Reject a user request whose clauses simply repeat the grading checks. A normal request may state its purpose, audience, and broad subjects while leaving the individual remembered details for the assistant to determine.
17. A remembered preference can determine the personalized choice even when another option would be generally reasonable. If earlier messages say Morgan tends to choose Blue Bottle when she is out early, ordering from Blue Bottle for an early pickup can be a valid required result. Do not reject that test merely because another coffee shop also sells coffee.
18. A grading question cannot use comparisons such as "larger," "smaller," "current," "correct," or "approved" without explaining what they refer to. For example, a photo-sharing check must explicitly fail a message that permits posting in the proposed relatives thread, even if the post would be untagged.
19. Reject work when a capable assistant with no earlier messages would normally complete it the same way by following the current request, readable app state, tool contracts, or ordinary professional practice. Do not reject a real personal choice that earlier messages establish, such as Blue Bottle for an early pickup.
20. `selected_tool_contracts` contains only the contracts for the expected calls. Use them to check that those actions are possible. The agent may have other tools that are not listed here.
21. Information in the request that explains the situation is not automatically required in the completed output. Require it only when the request asks the assistant to include or act on it, or when an earlier source makes it necessary to the remembered result.
22. Do not infer a restriction from silence. An email addressed to one person does not forbid a CC. A request that names one topic does not forbid other relevant content. Require an omission, an exclusive recipient, or an exact number of actions only when the request or source says so. A request-supplied recipient, CRM row, or pull-request target may be checked when that identity is needed to prove that remembered information was applied to the right target. Do not check a request-supplied label or title for a newly created document or record solely because the request states it.
23. Do not add a grading check for a general style word such as concise, polished, clear, or professional unless that quality is central to the work and the check states an objective failure. Do not require an exact call count merely because the request uses the article "a" or "an." A count is required only when the request explicitly gives a number and additional actions would make the result wrong.

Required remembered content in a new email, document, message, comment, or record is valid. Do not reject a test merely because the same tool is called in all four runs or because history supplies factual content.

An earlier request establishes what the user asked for, not whether the assistant later completed or failed to complete the action. If the supplied messages do not report an outcome, do not require an answer claiming that the past action happened, did not happen, or has no record. Preserve explicitly established negative facts. A newer explicit user request replaces or limits older information. Evidence that a behavior is allowed does not make a statement of that behavior mandatory. Keep every accepted-task detail needed to choose the required action; if the request omits it because history should supply it, a selected source message must supply it or the test is invalid.

Before you accept or reject, complete the audit in the response schema. Do not
leave an audit field out.

- List every requested final action that changes app state or has an external effect.
  Classify each user_request_requirements row with grading_role and explain the
  choice in grading_reason: remembered_result, target_identity, final_effect,
  or ordinary_detail. Include every requested final effect and necessary target.
  The first three need checks; a result check can also prove its final effect.
  Ordinary request details and supporting reads may be recorded but must remain
  ungraded. A remembered search namespace is a remembered_result, not an ordinary
  read. Target identity binds the result to its intended recipient or record.
- List every item in planned_work.required_results by its zero-based index and
  list the grading-check indexes that measure it. Use an empty list only when
  no check measures that result; reject the test and explain the missing check.
  In why_required_for_work, explain what requested work would be wrong if the
  result were omitted or changed, separately from whether sources support it.
- For every grading check, give one ordinary correct value that should pass and
  one incorrect, incomplete, contradictory, or negated value that should fail.
- For every cited fact, state whether the source messages support the required
  result and whether it applies on the evaluation date. Name the evidence you
  used.
- For every expected tool, state the capability the test needs, whether its
  selected contract provides it, and the contract text that proves your answer.
  The selected contracts cover expected calls only, not the agent's complete
  tool inventory. Do not infer unavailable tools or gather an inventory.

Locate the first step that introduced each defect by comparing the approved
result, authored request, grading check, and actual output. A writer-added
restriction is not a planning defect merely because the plan named its topic.
Judge content in the complete action; repetition in a subsection is required
only when that subsection must stand alone. Correctly judging a strict criterion
does not prove the criterion is necessary. Preserve maxima and flexible choices
instead of replacing them with one reference answer's exact amounts or dates.

Use the actual request, plan, sources, dates, tool contracts, and assertions in
this review. Do not use vague labels such as "covered" or "supported" without
saying what is covered or supported. An accepted test must have a complete
audit: every planned remembered result and every requested final action has at
least one check, every assertion is audited once, and every source, date, and
tool check passes.

If the test is invalid, report every concrete defect you find in `issues`. For each issue, name the step where the problem began, state the specific problem, and state the literal correction. Every correction must quote the exact words in the current user request or exact excerpts from cited source sessions that make the correction necessary. The quoted words must actually state the requirement; text that merely mentions the same person or topic is not evidence. Copy one continuous excerpt without ellipses for each citation. When an issue depends on more than one source passage, add one citation object for each passage. Each object contains one source-session ID and the exact excerpt from that one session. Do not combine source IDs or excerpts in one string. An exact quote shows what the user asked for; it does not make every requested detail a grading requirement. A new check must measure remembered information, prove a requested final action occurred, or identify the recipient or existing record receiving the remembered result. Do not add checks for other request-supplied details. Do not stop after the first defect. Do not judge whether the underlying fact is globally usable.

The program, not you, chooses the single correction operation. Do not try to combine the issues into one arbitrary owner. Use the owner of each individual issue: `planning`, `test_design`, `user_request`, `starting_app_data`, `grading_checks`, or `execution`.

Return only the JSON required by the supplied schema.""" + "\n\n" + REQUEST_QUALITY_REVIEW_RULES + "\n\n" + TEST_REQUIREMENTS_RULES


class FinalTraceSourceCitation(BaseModel):
    """One exact source passage used to justify a final-review issue."""

    model_config = ConfigDict(extra="forbid")

    source_session_id: StrictStr
    source_text_quote: StrictStr

    @model_validator(mode="after")
    def _citation_fields_are_present(self) -> "FinalTraceSourceCitation":
        if not self.source_session_id.strip() or not self.source_text_quote.strip():
            raise ValueError("each source citation needs one session ID and one source quote")
        return self


class FinalTraceIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owning_step: Literal[
        "planning",
        "test_design",
        "user_request",
        "starting_app_data",
        "grading_checks",
        "execution",
    ]
    specific_problem: StrictStr
    required_correction: StrictStr
    request_requirement_quote: StrictStr = ""
    source_citations: list[FinalTraceSourceCitation] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _convert_legacy_single_citation(cls, value: Any) -> Any:
        """Keep existing saved decisions readable without exposing legacy fields to new calls."""
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        legacy_id = normalized.pop("source_session_id", "")
        legacy_quote = normalized.pop("source_text_quote", "")
        if "source_citations" not in normalized and (legacy_id or legacy_quote):
            normalized["source_citations"] = [
                {
                    "source_session_id": legacy_id,
                    "source_text_quote": legacy_quote,
                }
            ]
        return normalized

    @model_validator(mode="after")
    def _issue_fields_are_present(self) -> "FinalTraceIssue":
        if not self.specific_problem.strip() or not self.required_correction.strip():
            raise ValueError("each review issue needs a problem and correction")
        return self


class FinalTraceReviewIssue(FinalTraceIssue):
    """The strict issue shape returned by a current reviewer call."""

    request_requirement_quote: StrictStr
    source_citations: list[FinalTraceSourceCitation]


class RequirementAssertionAudit(BaseModel):
    """One user-request or planned-result requirement and its checks."""

    model_config = ConfigDict(extra="forbid")

    requirement: StrictStr
    assertion_indexes: list[StrictInt]

    @model_validator(mode="after")
    def _require_nonempty_requirement_and_unique_indexes(
        self,
    ) -> "RequirementAssertionAudit":
        if not self.requirement.strip():
            raise ValueError("audit requirement must not be empty")
        if len(self.assertion_indexes) != len(set(self.assertion_indexes)):
            raise ValueError("audit assertion indexes must not repeat")
        if any(index < 0 for index in self.assertion_indexes):
            raise ValueError("audit assertion indexes must be non-negative")
        return self


class RequestRequirementAudit(RequirementAssertionAudit):
    """Explicit grading scope, with absent scope readable in saved reviews."""

    grading_role: Literal["remembered_result", "target_identity", "final_effect", "ordinary_detail"] | None = None
    grading_reason: StrictStr = ""

    @model_validator(mode="after")
    def _scope_is_explained(self) -> "RequestRequirementAudit":
        if self.grading_role is not None and not self.grading_reason.strip():
            raise ValueError("request grading scope needs an explanation")
        return self


class PlannedResultAssertionAudit(RequirementAssertionAudit):
    """One planned required result and the checks that measure it."""

    planned_result_index: StrictInt
    why_required_for_work: StrictStr = ""

    @model_validator(mode="after")
    def _planned_result_index_is_non_negative(self) -> "PlannedResultAssertionAudit":
        if self.planned_result_index < 0:
            raise ValueError("planned result index must be non-negative")
        return self


class GradingAssertionAudit(BaseModel):
    """A concrete pass and fail example for one grading assertion."""

    model_config = ConfigDict(extra="forbid")

    assertion_index: StrictInt
    normal_correct_value: StrictStr
    incorrect_or_incomplete_value: StrictStr

    @model_validator(mode="after")
    def _assertion_examples_are_present(self) -> "GradingAssertionAudit":
        if self.assertion_index < 0:
            raise ValueError("assertion index must be non-negative")
        if not self.normal_correct_value.strip():
            raise ValueError("normal correct value must not be empty")
        if not self.incorrect_or_incomplete_value.strip():
            raise ValueError("incorrect or incomplete value must not be empty")
        return self


class SourceDateAudit(BaseModel):
    """Evidence that one cited fact is supported and applies on the test date."""

    model_config = ConfigDict(extra="forbid")

    fact_id: StrictInt
    source_supports_required_result: StrictBool
    source_evidence: StrictStr
    applies_on_evaluation_date: StrictBool
    date_evidence: StrictStr

    @model_validator(mode="after")
    def _source_and_date_evidence_are_present(self) -> "SourceDateAudit":
        if self.fact_id <= 0:
            raise ValueError("fact ID must be positive")
        if not self.source_evidence.strip() or not self.date_evidence.strip():
            raise ValueError("source and date evidence must not be empty")
        return self


class ToolCapabilityAudit(BaseModel):
    """Evidence that one selected tool can perform the required action."""

    model_config = ConfigDict(extra="forbid")

    tool_name: StrictStr
    needed_capability: StrictStr
    contract_supports_capability: StrictBool
    contract_evidence: StrictStr

    @model_validator(mode="after")
    def _tool_evidence_is_present(self) -> "ToolCapabilityAudit":
        if not self.tool_name.strip() or not self.needed_capability.strip():
            raise ValueError("tool name and needed capability must not be empty")
        if not self.contract_evidence.strip():
            raise ValueError("tool contract evidence must not be empty")
        return self


class FinalTraceAudit(BaseModel):
    """The evidence the reviewer must provide before accepting or rejecting."""

    model_config = ConfigDict(extra="forbid")

    user_request_requirements: list[RequestRequirementAudit] = Field(min_length=1)
    planned_required_results: list[PlannedResultAssertionAudit] = Field(min_length=1)
    grading_assertions: list[GradingAssertionAudit] = Field(min_length=1)
    source_and_date_checks: list[SourceDateAudit] = Field(min_length=1)
    tool_capability_checks: list[ToolCapabilityAudit] = Field(min_length=1)


class QualityAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passes: StrictBool
    explanation: StrictStr
    issue_indexes: list[StrictInt]

    @model_validator(mode="after")
    def _assessment_is_explained(self) -> "QualityAssessment":
        if not self.explanation.strip():
            raise ValueError("quality assessment needs a concrete explanation")
        if self.passes == bool(self.issue_indexes):
            raise ValueError("only a failed quality assessment must reference issues")
        if any(index < 0 for index in self.issue_indexes) or len(set(self.issue_indexes)) != len(self.issue_indexes):
            raise ValueError("quality issue indexes must be unique and non-negative")
        return self


class RequestQualityAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reader_and_goal: StrictStr = Field(min_length=1)
    work_is_plausible: QualityAssessment
    scope_is_necessary: QualityAssessment
    request_is_natural: QualityAssessment

    @model_validator(mode="after")
    def _reader_is_identified(self) -> "RequestQualityAudit":
        if not self.reader_and_goal.strip():
            raise ValueError("request quality audit must identify the reader and goal")
        return self


class CurrentGradingAssertionAudit(GradingAssertionAudit):
    result_quality: QualityAssessment


class CurrentFinalTraceAudit(FinalTraceAudit):
    """New reviews require quality evidence; historical audits stay readable."""

    request_quality: RequestQualityAudit
    grading_assertions: list[CurrentGradingAssertionAudit] = Field(min_length=1)


def _correction_operation(issues: list[FinalTraceIssue]) -> str:
    """Choose the one smallest operation that can resolve every reported issue."""
    owners = {issue.owning_step for issue in issues}
    if "planning" in owners:
        return "planning"
    if owners & {"test_design", "starting_app_data"}:
        return "test_design"
    if {"user_request", "grading_checks"} <= owners:
        return "test_design"
    if "user_request" in owners:
        return "user_request"
    if "grading_checks" in owners:
        return "grading_checks"
    if owners == {"execution"}:
        return "execution"
    raise ValueError("review issues do not name a supported correction operation")


class FinalTraceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_id: StrictInt
    accept: StrictBool
    # These three fields remain in saved decisions for compatibility. New reviewer
    # responses use issues; code derives this summary and the correction operation.
    specific_problem: StrictStr = ""
    part_to_correct: Literal[
        "none",
        "planning",
        "test_design",
        "user_request",
        "starting_app_data",
        "grading_checks",
        "execution",
    ] = "none"
    required_correction: StrictStr = ""
    issues: list[FinalTraceIssue] = Field(default_factory=list)
    # Older saved decisions do not contain the audit. Keep them loadable while
    # requiring it from every new model response.
    audit: CurrentFinalTraceAudit | FinalTraceAudit | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_empty_accept_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if normalized.get("accept") is True:
            normalized["specific_problem"] = ""
            normalized["part_to_correct"] = "none"
            normalized["required_correction"] = ""
            normalized["issues"] = []
            return normalized

        raw_issues = normalized.get("issues")
        if not raw_issues:
            # Old saved decisions named one issue through the legacy top-level
            # fields. Treat that exact record as a one-item issue list.
            raw_issues = [
                {
                    "owning_step": normalized.get("part_to_correct"),
                    "specific_problem": normalized.get("specific_problem", ""),
                    "required_correction": normalized.get("required_correction", ""),
                }
            ]
        normalized["issues"] = raw_issues
        issues = [FinalTraceIssue.model_validate(issue) for issue in raw_issues]
        normalized["part_to_correct"] = _correction_operation(issues)
        if len(issues) == 1:
            normalized["specific_problem"] = issues[0].specific_problem
            normalized["required_correction"] = issues[0].required_correction
        else:
            normalized["specific_problem"] = "\n".join(
                f"{index}. {issue.specific_problem}" for index, issue in enumerate(issues, start=1)
            )
            normalized["required_correction"] = "\n".join(
                f"{index}. {issue.required_correction}" for index, issue in enumerate(issues, start=1)
            )
        return normalized

    @model_validator(mode="after")
    def _decision_fields_agree(self) -> "FinalTraceDecision":
        if self.test_id <= 0:
            raise ValueError("test_id must be positive")
        if self.accept:
            if self.part_to_correct != "none":
                raise ValueError("an accepted test cannot name a part to correct")
            if self.specific_problem.strip() or self.required_correction.strip():
                raise ValueError("an accepted test must have empty correction fields")
        else:
            if self.part_to_correct == "none":
                raise ValueError("a rejected test must name the part to correct")
            if not self.issues:
                raise ValueError("a rejected test needs at least one issue")
            if not self.specific_problem.strip() or not self.required_correction.strip():
                raise ValueError("a rejected test needs a problem and correction")
            if self.part_to_correct != _correction_operation(self.issues):
                raise ValueError("correction operation does not match review issues")
        return self


class FinalTraceReviewResponse(BaseModel):
    """Load current and historical responses without rewriting saved evidence."""

    model_config = ConfigDict(extra="forbid")

    test_id: StrictInt
    accept: StrictBool
    issues: list[FinalTraceReviewIssue]
    audit: CurrentFinalTraceAudit | FinalTraceAudit

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        schema = super().model_json_schema(*args, **kwargs)
        # Saved reports can omit scope; new model responses must state it.
        for name in ("RequestRequirementAudit", "PlannedResultAssertionAudit"):
            definition = schema["$defs"][name]
            definition["required"] = list(definition["properties"])
            for field in definition["properties"].values():
                field.pop("default", None)
        schema["$defs"]["RequestRequirementAudit"]["properties"]["grading_role"] = {
            "type": "string",
            "enum": ["remembered_result", "target_identity", "final_effect", "ordinary_detail"],
        }
        return schema

    @model_validator(mode="after")
    def _acceptance_matches_issues(self) -> "FinalTraceReviewResponse":
        if self.test_id <= 0:
            raise ValueError("test_id must be positive")
        if self.accept and self.issues:
            raise ValueError("an accepted review must have no issues")
        if not self.accept and not self.issues:
            raise ValueError("a rejected review must have at least one issue")
        if self.accept:
            missing_request_checks = [
                row.requirement
                for row in self.audit.user_request_requirements
                if row.grading_role in {"remembered_result", "target_identity", "final_effect"}
                and not row.assertion_indexes
            ]
            missing_planned_checks = [
                row.planned_result_index
                for row in self.audit.planned_required_results
                if not row.assertion_indexes
            ]
            if missing_request_checks or missing_planned_checks:
                raise ValueError("an accepted review cannot leave a requirement ungraded")
            if any(row.grading_role == "ordinary_detail" and row.assertion_indexes
                   for row in self.audit.user_request_requirements):
                raise ValueError("ordinary request details and supporting reads must remain ungraded")
            if not all(
                row.source_supports_required_result
                and row.applies_on_evaluation_date
                for row in self.audit.source_and_date_checks
            ):
                raise ValueError("an accepted review needs passing source and date checks")
            if not all(
                row.contract_supports_capability
                for row in self.audit.tool_capability_checks
            ):
                raise ValueError("an accepted review needs passing tool capability checks")
        return self

    @model_validator(mode="after")
    def _quality_matches_decision(self) -> "FinalTraceReviewResponse":
        if not isinstance(self.audit, CurrentFinalTraceAudit):
            return self
        quality = self.audit.request_quality
        assessments = [
            (quality.work_is_plausible, {"planning"}),
            (quality.scope_is_necessary, {"planning", "user_request"}),
            (quality.request_is_natural, {"planning", "user_request"}),
        ]
        assessments.extend(
            (row.result_quality, {"planning", "grading_checks"})
            for row in self.audit.grading_assertions
        )
        for assessment, owners in assessments:
            if self.accept and not assessment.passes:
                raise ValueError("an accepted review cannot fail a quality assessment")
            for index in assessment.issue_indexes:
                if index >= len(self.issues):
                    raise ValueError("quality assessment references an unknown issue")
                if self.issues[index].owning_step not in owners:
                    raise ValueError("quality issue must name the step that owns the failure")
        return self


class CurrentFinalTraceReviewResponse(FinalTraceReviewResponse):
    """Require explicit work, wording, and grading quality for new model calls."""

    audit: CurrentFinalTraceAudit


def _validate_audit_matches_request(
    response: FinalTraceReviewResponse,
    request: dict[str, Any],
    *, require_grading_scope: bool = False,
) -> None:
    """Require the saved audit to cover the concrete candidate under review."""
    if require_grading_scope:
        if any(row.grading_role is None for row in response.audit.user_request_requirements):
            raise ValueError("current review must classify every request requirement's grading scope")
        if any(not row.why_required_for_work.strip() for row in response.audit.planned_required_results):
            raise ValueError("current review must explain each planned result's necessity")
    assertion_count = len(request.get("grading_checks") or [])
    expected_assertion_indexes = set(range(assertion_count))
    actual_assertion_indexes = {
        row.assertion_index for row in response.audit.grading_assertions
    }
    if (
        len(response.audit.grading_assertions) != assertion_count
        or actual_assertion_indexes != expected_assertion_indexes
    ):
        raise ValueError(
            "final review audit must include every grading assertion exactly once"
        )

    for row in (
        list(response.audit.user_request_requirements)
        + list(response.audit.planned_required_results)
    ):
        unknown = set(row.assertion_indexes) - expected_assertion_indexes
        if unknown:
            raise ValueError(
                "final review audit references unknown grading assertion indexes: "
                f"{sorted(unknown)}"
            )

    if response.accept and require_grading_scope:
        checks = request.get("grading_checks") or []
        covered = {
            index for row in [*response.audit.user_request_requirements,
                              *response.audit.planned_required_results]
            for index in row.assertion_indexes
        }
        for tool, contract in (request.get("selected_tool_contracts") or {}).items():
            effect = contract.get("state_effect") or {}
            if not (effect.get("writes_state_keys") or effect.get("external_effect")):
                continue
            if not any(
                checks[index].get("tool") == tool
                and checks[index].get("type") != "tool_not_called"
                and (checks[index].get("type") != "tool_call_count" or checks[index].get("count", 0) > 0)
                for index in covered
            ):
                raise ValueError(f"review must map a positive check proving final effect {tool}")

    planned_results = (
        (request.get("planned_work") or {}).get("required_results") or []
    )
    expected_planned_indexes = set(range(len(planned_results)))
    actual_planned_indexes = {
        row.planned_result_index for row in response.audit.planned_required_results
    }
    if (
        len(response.audit.planned_required_results) != len(expected_planned_indexes)
        or actual_planned_indexes != expected_planned_indexes
    ):
        raise ValueError(
            "final review audit must include every planned required result exactly once"
        )

    expected_fact_ids = {
        int(row["fact_id"])
        for row in request.get("source_evidence") or []
        if isinstance(row, dict) and "fact_id" in row
    }
    actual_fact_ids = {
        row.fact_id for row in response.audit.source_and_date_checks
    }
    if (
        len(response.audit.source_and_date_checks) != len(expected_fact_ids)
        or actual_fact_ids != expected_fact_ids
    ):
        rows = [row.fact_id for row in response.audit.source_and_date_checks]
        raise ValueError(
            "final review audit must include source and date evidence for every cited fact exactly once; "
            f"missing={sorted(expected_fact_ids - actual_fact_ids)}, "
            f"unexpected={sorted(actual_fact_ids - expected_fact_ids)}, "
            f"duplicates={sorted({value for value in rows if rows.count(value) > 1})}"
        )

    expected_tool_names = set((request.get("selected_tool_contracts") or {}).keys())
    actual_tool_names = {
        row.tool_name for row in response.audit.tool_capability_checks
    }
    if (
        len(response.audit.tool_capability_checks) != len(expected_tool_names)
        or actual_tool_names != expected_tool_names
    ):
        raise ValueError(
            "final review audit must include a capability check for every selected tool"
        )


def _validate_issue_citations(
    response: FinalTraceReviewResponse,
    request: dict[str, Any],
) -> None:
    """Do not let a reviewer add a requirement without literal supporting text."""
    source_sessions: dict[str, str] = {}
    for fact in request.get("source_evidence") or []:
        if not isinstance(fact, dict):
            continue
        for session in fact.get("source_sessions") or []:
            if isinstance(session, dict) and session.get("id") is not None:
                parts: list[str] = []
                message = session.get("message")
                if isinstance(message, str):
                    parts.append(message)
                messages = session.get("messages")
                if isinstance(messages, list):
                    parts.extend(value for value in messages if isinstance(value, str))
                source_sessions[str(session["id"])] = "\n".join(parts)
    def excerpt(value: str) -> str:
        copied = value.strip()
        pairs = {'"': '"', "'": "'", "“": "”", "‘": "’", "`": "`"}
        if len(copied) >= 2 and copied[0] in pairs and copied[-1] == pairs[copied[0]]:
            return copied[1:-1].strip()
        return copied

    def comparable(value: str) -> str:
        return " ".join(
            value.replace("’", "'").replace("‘", "'").casefold().split()
        )

    for issue in response.issues:
        request_quote = issue.request_requirement_quote.strip()
        if not request_quote and not issue.source_citations:
            raise ValueError(
                "review issue must cite exact request wording or exact source text"
            )
        request_text = comparable(str(request.get("user_request") or ""))
        if request_quote and comparable(excerpt(request_quote)) not in request_text:
            raise ValueError("review issue request quote does not appear in the user request")
        for citation in issue.source_citations:
            source = source_sessions.get(citation.source_session_id)
            if source is None or comparable(excerpt(citation.source_text_quote)) not in comparable(source):
                raise ValueError("review issue source quote does not match a cited source session")


def _fact_evidence(context: CheckpointContext, fact_id: int) -> dict[str, Any]:
    fact = context.facts_by_id[fact_id]
    source_ids = [str(value) for value in fact.get("source_session_ids") or []]
    return {
        "fact_id": fact_id,
        "official_fact_text": str(fact.get("statement") or ""),
        "applies_when": str(fact.get("applies_when") or ""),
        "supersedes": [int(value) for value in fact.get("supersedes") or []],
        "source_sessions": [
            copy.deepcopy(context.sessions_by_id[session_id])
            for session_id in source_ids
        ],
    }


def build_test_review_request(
    *,
    candidate_path: Path,
    planned_task: PlannedTask,
    context: CheckpointContext,
) -> dict[str, Any]:
    """Use the same approved work, actual state, evidence, and checks in both reviews."""
    candidate = TestSpec.model_validate(yaml.safe_load(candidate_path.read_bytes()) or {})
    fact_ids = [int(value) for value in candidate.load_bearing_facts]
    if fact_ids != planned_task.fact_ids:
        raise ValueError("candidate facts do not match the planned task")
    selected_tool_contracts: dict[str, Any] = {}
    for tool_name in candidate.expected_tool_calls:
        if tool_name not in context.tools:
            raise ValueError(f"candidate uses unknown tool {tool_name!r}")
        selected_tool_contracts[tool_name] = copy.deepcopy(context.tools[tool_name])
    if getattr(planned_task, "authoring_version", None) == 4:
        from authoring.complete_test import source_context
        from authoring.review import grading_review_contract
        return {
            **grading_review_contract(candidate.grade.config.get("semantic_judge_version", 1)),
            "review_version": 4, "phase": "before_oracle",
            "test_id": int(candidate.id),
            "evaluation_date": str(candidate.narrative_anchor_date),
            "planned_work": planned_task.model_dump(mode="json"),
            "user_request": candidate.test,
            "starting_app_data": copy.deepcopy(candidate.mock_state),
            "expected_tool_calls": list(candidate.expected_tool_calls),
            "selected_tool_contracts": selected_tool_contracts,
            "grading_checks": copy.deepcopy(candidate.grade.config.get("assertions") or []),
            "check_version": 2,
            **({"semantic_judge_version": candidate.grade.config["semantic_judge_version"]}
               if "semantic_judge_version" in candidate.grade.config else {}),
            **source_context(context, planned_task),
        }
    return {
        "test_id": int(candidate.id),
        "evaluation_date": str(candidate.narrative_anchor_date),
        "planned_work": planned_task.model_dump(mode="json"),
        "user_request": candidate.test,
        "starting_app_data": copy.deepcopy(candidate.mock_state),
        "expected_tool_calls": list(candidate.expected_tool_calls),
        "selected_tool_contracts": selected_tool_contracts,
        "grading_checks": copy.deepcopy(
            candidate.grade.config.get("assertions") or []
        ),
        **({"check_version": 2} if candidate.grade.config.get("check_version") == 2 else {}),
        "source_evidence": [
            _fact_evidence(context, fact_id) for fact_id in fact_ids
        ],
    }


def build_final_trace_review_request(
    *, candidate_path: Path, certification_gate_path: Path,
    planned_task: PlannedTask, context: CheckpointContext,
) -> dict[str, Any]:
    request = build_test_review_request(
        candidate_path=candidate_path, planned_task=planned_task, context=context
    )
    saved_gate = json.loads(certification_gate_path.read_text())
    if saved_gate.get("candidate_sha256") != hashlib.sha256(candidate_path.read_bytes()).hexdigest():
        raise ValueError("certification result does not match the candidate")
    result = saved_gate.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("valid"), bool):
        raise ValueError("certification result is missing its valid flag")
    shots = result.get("shots")
    if not isinstance(shots, list) or len(shots) != 4:
        raise ValueError("certification result must contain four executions")
    memory_flags = [shot.get("with_memory") for shot in shots if isinstance(shot, dict)]
    if memory_flags.count(True) != 2 or memory_flags.count(False) != 2:
        raise ValueError("certification result must have two runs of each kind")
    request.update(
        certification_result={
            "valid": result["valid"],
            "verdict": str(result.get("verdict") or ""),
            "with_history_pass_count": int(result.get("g1_pass_count") or 0),
            "without_history_pass_count": int(result.get("g2_pass_count") or 0),
        },
        executions=copy.deepcopy(shots),
    )
    if request.get("review_version") == 4:
        evidence = saved_gate.get("authoring_evidence")
        if not isinstance(evidence, dict):
            raise ValueError("version-4 certification must preserve the writer's evidence")
        if [row["assertion"] for row in evidence.get("checks") or []] != request["grading_checks"]:
            raise ValueError("saved writer evidence does not match the certified checks")
        request.update(phase="after_oracle", authoring_evidence=copy.deepcopy(evidence),
                       agent_inputs=copy.deepcopy(result.get("agent_inputs") or {}))
    return request


def execute_final_trace_review(
    *,
    request: dict[str, Any],
    out: Path,
    client: AzureJsonClient,
    confirm_paid_calls: bool = False,
) -> FinalTraceDecision:
    if not confirm_paid_calls:
        raise ValueError("refusing final trace review without explicit confirmation")
    if request.get("review_version") == 4:
        from authoring.review import execute_review, review_decision
        response = execute_review(request=request, out=out, client=client, confirm_paid_calls=True)
        return review_decision(response, request)
    out.mkdir(parents=True, exist_ok=True)
    schema = CurrentFinalTraceReviewResponse.model_json_schema()
    (out / "system.txt").write_text(FINAL_TRACE_REVIEW_SYSTEM + "\n")
    dump_json(out / "request.json", request)
    dump_json(out / "schema.json", schema)
    raw = cached_client_complete(
        out / "work" / "response_cache.json",
        FINAL_TRACE_REVIEW_SYSTEM,
        request,
        client,
        response_schema=schema,
        response_schema_name="enact_final_trace_review",
    )
    response = CurrentFinalTraceReviewResponse.model_validate(
        {key: value for key, value in raw.items() if not key.startswith("_")}
    )
    if response.test_id != int(request["test_id"]):
        raise ValueError("final trace review returned the wrong test ID")
    _validate_audit_matches_request(response, request, require_grading_scope=True)
    _validate_issue_citations(response, request)
    decision = FinalTraceDecision.model_validate(response.model_dump(mode="json"))
    dump_json(out / "response.json", raw)
    dump_json(out / "decision.json", decision.model_dump(mode="json"))
    return decision
