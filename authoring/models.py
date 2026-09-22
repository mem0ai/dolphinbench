"""Strict internal records used while authoring DolphinBench tests."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


class ExistingRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_key: StrictStr
    record_key: StrictStr
    match: dict[str, Any]
    remove_fields: list[StrictStr]


class NewRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_key: StrictStr
    record_key: StrictStr
    record: dict[str, Any]


class GradingCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A check may verify a necessary part of the current request or starting
    # state. Those checks have no historical fact ID. Separate validation
    # still requires every selected fact to have a fact-backed check.
    fact_ids: list[StrictInt]
    assertion: dict[str, Any]

    @field_validator("fact_ids")
    @classmethod
    def _unique_fact_ids(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)) or any(fact_id <= 0 for fact_id in value):
            raise ValueError("fact_ids must contain unique positive integers")
        return value


class RequiredResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: StrictStr
    # Legacy saved plans may contain request-only results with no fact IDs.
    # Current authoring prompts and validation allow only remembered results
    # here; final actions are derived from the tool contracts instead.
    fact_ids: list[StrictInt]
    # New two-stage plans bind each fact-backed result to one immutable item of
    # the requested work. Legacy plans did not carry this mapping.
    work_item_id: StrictStr | None = None

    @field_validator("fact_ids")
    @classmethod
    def _unique_positive_fact_ids(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)) or any(fact_id <= 0 for fact_id in value):
            raise ValueError("fact_ids must contain unique positive integers")
        return value

    @field_validator("result")
    @classmethod
    def _result_is_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("result must not be empty")
        return value.strip()

    @field_validator("work_item_id")
    @classmethod
    def _work_item_id_is_not_empty_when_present(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("work_item_id must not be empty when present")
        return value.strip()


class RequestedResultChecks(BaseModel):
    """The assertions that prove one material result from the planned work."""

    model_config = ConfigDict(extra="forbid")

    requested_result: StrictStr
    assertion_indexes: list[StrictInt] = Field(default_factory=list)
    check_ids: list[StrictStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_reference_format(self) -> "RequestedResultChecks":
        if bool(self.assertion_indexes) == bool(self.check_ids):
            raise ValueError("result mapping needs either check IDs or saved assertion indexes")
        if len(self.check_ids) != len(set(self.check_ids)) or any(not item.strip() for item in self.check_ids):
            raise ValueError("check IDs must be unique nonempty names")
        return self

    @field_validator("requested_result")
    @classmethod
    def _requested_result_is_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("requested_result must not be empty")
        return value.strip()

    @field_validator("assertion_indexes")
    @classmethod
    def _assertion_indexes_are_unique_and_non_negative(
        cls, value: list[int]
    ) -> list[int]:
        if len(value) != len(set(value)) or any(index < 0 for index in value):
            raise ValueError(
                "assertion_indexes must contain unique non-negative integers"
            )
        return value


class AssertionExamples(BaseModel):
    """Examples that demonstrate one assertion's intended boundary."""

    model_config = ConfigDict(extra="forbid")

    assertion_index: StrictInt = Field(ge=0)
    correct_example: StrictStr
    incorrect_or_negated_example: StrictStr

    @field_validator("correct_example", "incorrect_or_negated_example")
    @classmethod
    def _example_is_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("assertion examples must not be empty")
        return value.strip()


class GradingDesignResponse(BaseModel):
    """Grading designed after the exact blind request is available."""

    model_config = ConfigDict(extra="forbid")

    grading_checks: list[GradingCheck] = Field(min_length=1)
    answers_that_must_not_appear: list[StrictStr]
    with_history_expected_result: StrictStr
    without_history_expected_result: StrictStr
    without_history_failed_assertion_index: StrictInt = Field(ge=0)
    requested_result_checks: list[RequestedResultChecks] = Field(min_length=1)
    assertion_examples: list[AssertionExamples] = Field(min_length=1)

    @field_validator("answers_that_must_not_appear")
    @classmethod
    def _answers_are_nonempty_and_unique(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError(
                "answers_that_must_not_appear must contain unique non-empty values"
            )
        return normalized

    @field_validator(
        "with_history_expected_result", "without_history_expected_result"
    )
    @classmethod
    def _expected_result_is_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("expected result must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def _evidence_matches_the_assertions(self) -> "GradingDesignResponse":
        indexes = set(range(len(self.grading_checks)))
        mapped_results = [item.requested_result for item in self.requested_result_checks]
        if len(mapped_results) != len(set(mapped_results)):
            raise ValueError("requested_result_checks must not repeat a requested result")
        for item in self.requested_result_checks:
            unknown = set(item.assertion_indexes) - indexes
            if unknown:
                raise ValueError(
                    "requested_result_checks names unknown assertion indexes "
                    f"{sorted(unknown)}"
                )
        example_indexes = [item.assertion_index for item in self.assertion_examples]
        if set(example_indexes) != indexes or len(example_indexes) != len(indexes):
            raise ValueError(
                "assertion_examples must contain exactly one entry for every assertion"
            )
        if self.without_history_failed_assertion_index not in indexes:
            raise ValueError(
                "without_history_failed_assertion_index is not an assertion index"
            )
        return self


class DesignResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["design", "reject"]
    rejection_reason: StrictStr
    source_message_meaning: StrictStr
    reason_it_applies_on_test_date: StrictStr
    present_situation: StrictStr
    requested_work: StrictStr
    missing_information: StrictStr
    information_the_request_must_not_reveal: StrictStr
    existing_records: list[ExistingRecord]
    new_records: list[NewRecord]
    required_results: list[RequiredResult] = Field(default_factory=list)
    expected_tool_calls: list[StrictStr]
    # These fields remain available for saved legacy designs and the existing
    # candidate builder. New design calls leave them empty; the later grading
    # call supplies them after the exact request exists.
    grading_checks: list[GradingCheck] = Field(default_factory=list)
    check_version: Literal[1, 2] = 1
    answers_that_must_not_appear: list[StrictStr] = Field(default_factory=list)
    with_history_result: StrictStr = ""
    without_history_result: StrictStr = ""
    without_memory_failed_check: StrictInt = -1

    @field_validator("expected_tool_calls")
    @classmethod
    def _unique_tool_names(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("expected_tool_calls contains duplicates")
        return value

    @field_validator(
        "source_message_meaning",
        "reason_it_applies_on_test_date",
        "present_situation",
        "requested_work",
        "missing_information",
        "information_the_request_must_not_reveal",
    )
    @classmethod
    def _strip_explanations(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _design_and_rejection_fields_are_consistent(self) -> "DesignResponse":
        design_text = {
            "source_message_meaning": self.source_message_meaning,
            "reason_it_applies_on_test_date": self.reason_it_applies_on_test_date,
            "present_situation": self.present_situation,
            "requested_work": self.requested_work,
            "missing_information": self.missing_information,
            "information_the_request_must_not_reveal": (
                self.information_the_request_must_not_reveal
            ),
        }
        if self.outcome == "design":
            missing = [name for name, value in design_text.items() if not value]
            if missing:
                raise ValueError(
                    "design response is missing " + ", ".join(missing)
                )
        elif not self.rejection_reason.strip():
            raise ValueError("rejected design must give a rejection reason")
        return self

    @field_validator("answers_that_must_not_appear")
    @classmethod
    def _answers_are_nonempty_and_unique(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError(
                "answers_that_must_not_appear must contain unique non-empty values"
            )
        return normalized

    def blind_request_handoff(self) -> dict[str, str]:
        """Return the only fact-free information the request writer may receive."""
        handoff = {
            "present_situation": self.present_situation,
            "requested_work": self.requested_work,
            "missing_information": self.missing_information,
            "information_the_request_must_not_reveal": (
                self.information_the_request_must_not_reveal
            ),
        }
        missing = [name for name, value in handoff.items() if not value.strip()]
        if missing:
            raise ValueError(
                "new request handoff is missing " + ", ".join(missing)
            )
        return handoff

    def with_grading(self, grading: GradingDesignResponse) -> "DesignResponse":
        """Attach grading created from the final blind request."""
        if self.outcome != "design":
            raise ValueError("cannot add grading to a rejected design")
        return self.model_copy(
            update={
                "grading_checks": grading.grading_checks,
                "answers_that_must_not_appear": grading.answers_that_must_not_appear,
                "with_history_result": grading.with_history_expected_result,
                "without_history_result": grading.without_history_expected_result,
                "without_memory_failed_check": grading.without_history_failed_assertion_index,
            }
        )


class BlindQueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: StrictStr

    @field_validator("query")
    @classmethod
    def _query_is_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be empty")
        return value.strip()


class HistoryDependentResult(BaseModel):
    """Evidence for one result that the current request does not supply."""

    model_config = ConfigDict(extra="forbid")

    fact_id: StrictInt = Field(gt=0)
    source_session_id: StrictStr
    supporting_source_text: StrictStr
    present_request_states: StrictStr
    history_only_supplies: StrictStr
    why_request_alone_does_not_determine_result: StrictStr
    hidden_values: list[StrictStr] = Field(min_length=1)

    @field_validator(
        "source_session_id",
        "supporting_source_text",
        "present_request_states",
        "history_only_supplies",
        "why_request_alone_does_not_determine_result",
    )
    @classmethod
    def _text_is_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("history-result text must not be empty")
        return value.strip()

    @field_validator("hidden_values")
    @classmethod
    def _hidden_values_are_unique(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("hidden_values must contain unique non-empty values")
        return normalized


class AuthoredExistingRecord(BaseModel):
    """An existing app record encoded without an open-ended JSON schema."""

    model_config = ConfigDict(extra="forbid")

    state_key: StrictStr
    record_key: StrictStr
    match_json: StrictStr
    remove_fields: list[StrictStr]

    def to_existing_record(self) -> ExistingRecord:
        match = json.loads(self.match_json)
        if not isinstance(match, dict):
            raise ValueError("match_json must contain one JSON object")
        return ExistingRecord(
            state_key=self.state_key,
            record_key=self.record_key,
            match=match,
            remove_fields=self.remove_fields,
        )


class AuthoredNewRecord(BaseModel):
    """A new app record encoded without an open-ended JSON schema."""

    model_config = ConfigDict(extra="forbid")

    state_key: StrictStr
    record_key: StrictStr
    record_json: StrictStr

    def to_new_record(self) -> NewRecord:
        record = json.loads(self.record_json)
        if not isinstance(record, dict):
            raise ValueError("record_json must contain one JSON object")
        return NewRecord(
            state_key=self.state_key,
            record_key=self.record_key,
            record=record,
        )


class AuthoredDesignResponse(BaseModel):
    """The design fields owned by the one-call authoring model."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["design", "reject"]
    rejection_reason: StrictStr
    source_message_meaning: StrictStr
    reason_it_applies_on_test_date: StrictStr
    existing_records: list[AuthoredExistingRecord]
    new_records: list[AuthoredNewRecord]
    required_results: list[RequiredResult]
    expected_tool_calls: list[StrictStr]

    def to_design_response(self, *, query: str) -> DesignResponse:
        request_text = query.strip() if self.outcome == "design" else ""
        return DesignResponse(
            outcome=self.outcome,
            rejection_reason=self.rejection_reason,
            source_message_meaning=self.source_message_meaning,
            reason_it_applies_on_test_date=self.reason_it_applies_on_test_date,
            present_situation=request_text,
            requested_work=request_text,
            missing_information=(
                "Information supplied only by the cited earlier messages."
                if request_text
                else ""
            ),
            information_the_request_must_not_reveal=(
                "The answer supplied only by the cited earlier messages."
                if request_text
                else ""
            ),
            existing_records=[item.to_existing_record() for item in self.existing_records],
            new_records=[item.to_new_record() for item in self.new_records],
            required_results=self.required_results,
            expected_tool_calls=self.expected_tool_calls,
        )


class ExpectedToolArgument(BaseModel):
    """A sourced argument value with an explicit comparison, not a name heuristic."""

    model_config = ConfigDict(extra="forbid")

    check_id: StrictStr = ""
    action_id: StrictStr = ""

    tool: StrictStr
    argument: StrictStr
    value_json: StrictStr
    comparison: Literal[
        "exact", "number", "date", "instant", "list_includes",
        "recipient_identity", "email_delivery",
    ] | None = None
    request_requirement_quote: StrictStr = ""
    history_fact_ids: list[StrictInt] = Field(default_factory=list)

    @field_validator("tool", "argument")
    @classmethod
    def _argument_text_is_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tool argument fields must not be empty")
        return value.strip()

    @field_validator("history_fact_ids")
    @classmethod
    def _history_fact_ids_are_unique(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)) or any(item <= 0 for item in value):
            raise ValueError("history_fact_ids must contain unique positive integers")
        return value

    @model_validator(mode="after")
    def _argument_has_a_requirement_source(self) -> "ExpectedToolArgument":
        if not self.request_requirement_quote.strip() and not self.history_fact_ids:
            raise ValueError(
                "an expected tool argument needs a request quote or a history fact"
            )
        return self

    def value(self) -> Any:
        return json.loads(self.value_json)


class ExpectedEmptyToolArgument(BaseModel):
    """One optional tool argument that an explicit requirement leaves empty."""

    model_config = ConfigDict(extra="forbid")

    check_id: StrictStr = ""
    action_id: StrictStr = ""

    tool: StrictStr
    argument: StrictStr
    request_requirement_quote: StrictStr = ""
    history_fact_ids: list[StrictInt] = Field(default_factory=list)

    @field_validator("tool", "argument")
    @classmethod
    def _argument_text_is_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("empty tool argument fields must not be empty")
        return value.strip()

    @field_validator("history_fact_ids")
    @classmethod
    def _history_fact_ids_are_unique(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)) or any(item <= 0 for item in value):
            raise ValueError("history_fact_ids must contain unique positive integers")
        return value

    @model_validator(mode="after")
    def _argument_has_a_requirement_source(self) -> "ExpectedEmptyToolArgument":
        if not self.request_requirement_quote.strip() and not self.history_fact_ids:
            raise ValueError(
                "an empty tool argument needs a request quote or a history fact"
            )
        return self


class ExpectedToolCount(BaseModel):
    """An exact action count explicitly required by the current request."""

    model_config = ConfigDict(extra="forbid")

    tool: StrictStr
    count: StrictInt = Field(gt=0)
    request_requirement_quote: StrictStr

    @field_validator("tool", "request_requirement_quote")
    @classmethod
    def _count_text_is_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tool count fields must not be empty")
        return value.strip()


class ProseRequirement(BaseModel):
    """One prose requirement that needs an LLM judge rather than exact equality."""

    model_config = ConfigDict(extra="forbid")

    check_id: StrictStr = ""
    action_id: StrictStr = ""

    tool: StrictStr
    path: StrictStr
    criterion: StrictStr
    request_requirement_quote: StrictStr = ""
    history_fact_ids: list[StrictInt] = Field(default_factory=list)

    @field_validator("tool", "path", "criterion")
    @classmethod
    def _prose_text_is_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prose requirement fields must not be empty")
        return value.strip()

    @field_validator("history_fact_ids")
    @classmethod
    def _prose_history_fact_ids_are_unique(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)) or any(item <= 0 for item in value):
            raise ValueError("history_fact_ids must contain unique positive integers")
        return value

    @model_validator(mode="after")
    def _prose_has_a_requirement_source(self) -> "ProseRequirement":
        if not self.request_requirement_quote.strip() and not self.history_fact_ids:
            raise ValueError(
                "a prose requirement needs a request quote or a history fact"
            )
        return self


class AuthoredTestResponse(BaseModel):
    """The one-model-call response used for new DolphinBench test creation."""

    model_config = ConfigDict(extra="forbid")

    # Version 1 remains readable only for authenticated saved authoring work.
    schema_version: Literal[1, 2, 3] = 1
    design: AuthoredDesignResponse
    query: StrictStr = ""
    history_dependent_results: list[HistoryDependentResult] = Field(default_factory=list)
    request_requirement_quotes: list[StrictStr] = Field(default_factory=list)
    expected_arguments: list[ExpectedToolArgument] = Field(default_factory=list)
    expected_empty_arguments: list[ExpectedEmptyToolArgument] = Field(default_factory=list)
    expected_tool_counts: list[ExpectedToolCount] = Field(default_factory=list)
    prose_requirements: list[ProseRequirement] = Field(default_factory=list)
    requested_result_checks: list[RequestedResultChecks] = Field(default_factory=list)
    with_history_expected_result: StrictStr = ""
    without_history_expected_result: StrictStr = ""

    @model_validator(mode="after")
    def _design_or_rejection_has_the_right_fields(self) -> "AuthoredTestResponse":
        if self.design.outcome == "reject":
            return self
        if not self.query.strip():
            raise ValueError("an authored test needs a user request")
        if not self.history_dependent_results:
            raise ValueError("an authored test needs history-dependent results")
        if not self.with_history_expected_result.strip():
            raise ValueError("an authored test needs a with-history expected result")
        if not self.without_history_expected_result.strip():
            raise ValueError("an authored test needs a without-history expected result")
        if self.schema_version >= 2:
            if any(item.comparison is None for item in self.expected_arguments):
                raise ValueError("new expected arguments must state their comparison")
            if not self.requested_result_checks:
                raise ValueError("new authoring must map the approved results to grading checks")
        if self.schema_version == 3:
            checks = [*self.expected_arguments, *self.expected_empty_arguments, *self.prose_requirements]
            ids = [check.check_id for check in checks]
            if len(ids) != len(set(ids)) or any(not name.strip() or name.startswith("program:") for name in ids):
                raise ValueError("new checks need unique nonempty check IDs")
            if any(not check.action_id.strip() for check in checks):
                raise ValueError("new checks must identify their action")
            if any(item.comparison in {"recipient_identity", "email_delivery"} for item in self.expected_arguments):
                raise ValueError("contact meaning requires a generated LLM rubric; literal addresses use exact checks")
            if any(not row.check_ids or row.assertion_indexes for row in self.requested_result_checks):
                raise ValueError("new result mappings use check IDs, not assertion positions")
        return self

    @field_validator("request_requirement_quotes")
    @classmethod
    def _request_requirement_quotes_are_unique(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError(
                "request_requirement_quotes must contain unique non-empty excerpts"
            )
        return normalized


class MemoryChange(BaseModel):
    """One required result that earlier user messages supply."""

    model_config = ConfigDict(extra="forbid")

    fact_id: StrictInt = Field(gt=0)
    part_of_result: StrictStr
    required_result: StrictStr
    source_session_id: StrictStr
    exact_supporting_source_text: StrictStr
    why_source_supports_required_result: StrictStr
    why_fact_still_applies_on_evaluation_date: StrictStr

    @field_validator(
        "part_of_result",
        "required_result",
        "source_session_id",
        "exact_supporting_source_text",
        "why_source_supports_required_result",
        "why_fact_still_applies_on_evaluation_date",
    )
    @classmethod
    def _required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("memory-change text must not be empty")
        return value.strip()


class PlannedWorkItem(BaseModel):
    """One numbered, immutable part of the work selected in the first call."""

    model_config = ConfigDict(extra="forbid")

    work_item_id: StrictStr
    work: StrictStr

    @field_validator("work_item_id", "work")
    @classmethod
    def _text_is_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("work item text must not be empty")
        return value.strip()


class SituationProposal(BaseModel):
    """The first-call proposal for one possible test."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: StrictInt = Field(gt=0)
    new_situation: StrictStr
    requested_work: StrictStr
    work_items: list[PlannedWorkItem] = Field(min_length=1)
    candidate_fact_ids: list[StrictInt] = Field(min_length=1)
    tool_names: list[StrictStr] = Field(min_length=1)
    without_history: StrictStr
    why_listed_tools_can_complete_requested_work: StrictStr

    @field_validator("new_situation", "requested_work", "without_history", "why_listed_tools_can_complete_requested_work")
    @classmethod
    def _required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("situation proposal text must not be empty")
        return value.strip()

    @field_validator("candidate_fact_ids")
    @classmethod
    def _candidate_fact_ids_are_unique(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)) or any(fact_id <= 0 for fact_id in value):
            raise ValueError("candidate_fact_ids must contain unique positive integers")
        return value

    @field_validator("tool_names")
    @classmethod
    def _tool_names_are_unique(cls, value: list[str]) -> list[str]:
        normalized = [tool.strip() for tool in value]
        if any(not tool for tool in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("tool_names must contain unique non-empty names")
        return normalized

    @model_validator(mode="after")
    def _work_item_ids_follow_the_proposal_number(self) -> "SituationProposal":
        expected = [f"{self.proposal_id}.{index}" for index in range(1, len(self.work_items) + 1)]
        actual = [item.work_item_id for item in self.work_items]
        if actual != expected:
            raise ValueError(
                "work_item_id values must be numbered from the proposal ID: "
                f"expected {expected}, got {actual}"
            )
        return self


class SituationProposalBatch(BaseModel):
    """The first planning response, intentionally without evidence or results."""

    model_config = ConfigDict(extra="forbid")

    checkpoint_identity: StrictStr
    persona: StrictStr
    evaluation_date: StrictStr
    proposals: list[SituationProposal] = Field(min_length=1, max_length=25)

    @field_validator("checkpoint_identity", "persona", "evaluation_date")
    @classmethod
    def _identity_text_is_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("situation proposal batch identity fields must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def _proposal_ids_are_stable_numbers(self) -> "SituationProposalBatch":
        expected = list(range(1, len(self.proposals) + 1))
        actual = [proposal.proposal_id for proposal in self.proposals]
        if actual != expected:
            raise ValueError(
                f"proposal_id values must be consecutive numbers: expected {expected}, got {actual}"
            )
        return self


class RememberedResultEvidence(BaseModel):
    """One cited result attached to, but unable to alter, a first-call work item."""

    model_config = ConfigDict(extra="forbid")

    work_item_id: StrictStr
    exact_work_item_text: StrictStr
    fact_id: StrictInt = Field(gt=0)
    required_result: StrictStr
    source_session_id: StrictStr
    exact_supporting_source_text: StrictStr
    why_source_supports_required_result: StrictStr
    # Empty only when reading evidence produced before necessity was explicit.
    why_required_for_work: StrictStr = ""
    why_fact_still_applies_on_evaluation_date: StrictStr

    @field_validator(
        "work_item_id",
        "exact_work_item_text",
        "required_result",
        "source_session_id",
        "exact_supporting_source_text",
        "why_source_supports_required_result",
        "why_fact_still_applies_on_evaluation_date",
    )
    @classmethod
    def _required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("remembered-result evidence text must not be empty")
        return value.strip()


class EvidenceDecision(BaseModel):
    """The second-call decision for one immutable first-call proposal."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: StrictInt = Field(gt=0)
    outcome: Literal["accept", "reject"]
    rejection_reason: StrictStr
    remembered_results: list[RememberedResultEvidence]

    @field_validator("rejection_reason")
    @classmethod
    def _strip_rejection_reason(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _acceptance_and_rejection_have_the_right_contents(self) -> "EvidenceDecision":
        if self.outcome == "accept":
            if self.rejection_reason:
                raise ValueError("an accepted evidence decision must not have a rejection reason")
            if not self.remembered_results:
                raise ValueError("an accepted evidence decision needs remembered results")
        elif not self.rejection_reason:
            raise ValueError("a rejected evidence decision needs a rejection reason")
        elif self.remembered_results:
            raise ValueError("a rejected evidence decision must not include remembered results")
        return self


class EvidenceDecisionBatch(BaseModel):
    """The fact-and-source-grounded second planning response."""

    model_config = ConfigDict(extra="forbid")

    checkpoint_identity: StrictStr
    persona: StrictStr
    evaluation_date: StrictStr
    decisions: list[EvidenceDecision] = Field(min_length=1, max_length=25)

    @field_validator("checkpoint_identity", "persona", "evaluation_date")
    @classmethod
    def _identity_text_is_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence decision batch identity fields must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def _decision_ids_are_unique(self) -> "EvidenceDecisionBatch":
        decision_ids = [decision.proposal_id for decision in self.decisions]
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("evidence decisions must not repeat proposal_id values")
        return self


class TestProposal(BaseModel):
    """A test idea before exact messages, records, arguments, and checks."""

    model_config = ConfigDict(extra="forbid")

    fact_ids: list[StrictInt] = Field(min_length=1)
    new_situation: StrictStr
    requested_work: StrictStr
    tool_names: list[StrictStr] = Field(min_length=1)
    memory_changes: list[MemoryChange] = Field(min_length=1)
    without_history: StrictStr
    why_listed_tools_can_complete_requested_work: StrictStr

    @field_validator("fact_ids")
    @classmethod
    def _unique_positive_fact_ids(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)) or any(fact_id <= 0 for fact_id in value):
            raise ValueError("fact_ids must contain unique positive integers")
        return value

    @field_validator("tool_names")
    @classmethod
    def _unique_tool_names(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("tool_names must contain unique non-empty names")
        return normalized

    @field_validator(
        "new_situation",
        "requested_work",
        "without_history",
        "why_listed_tools_can_complete_requested_work",
    )
    @classmethod
    def _required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("proposal text must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def _every_fact_changes_a_result(self) -> "TestProposal":
        changed = {change.fact_id for change in self.memory_changes}
        if changed != set(self.fact_ids):
            raise ValueError(
                "memory_changes must contain at least one item for every and only "
                "the selected fact IDs"
            )
        results = [
            (change.fact_id, change.part_of_result, change.required_result)
            for change in self.memory_changes
        ]
        if len(results) != len(set(results)):
            raise ValueError("memory_changes must not repeat the same required result")
        return self

    def to_planned_task(self) -> "PlannedTask":
        task_description = (
            f"New situation: {self.new_situation}\n"
            f"Work the user wants completed: {self.requested_work}"
        )
        return PlannedTask(
            task_description=task_description,
            fact_ids=list(self.fact_ids),
            required_results=[
                RequiredResult(
                    result=(
                        f"In {change.part_of_result}, {change.required_result}"
                    ),
                    fact_ids=[change.fact_id],
                )
                for change in self.memory_changes
            ],
            expected_tools=list(self.tool_names),
            action_without_memory=self.without_history,
            why_memory_changes_result="; ".join(
                f"Fact {change.fact_id} changes {change.part_of_result}: "
                f"{change.required_result}"
                for change in self.memory_changes
            ),
            fact_application_explanations_on_evaluation_date=[
                FactApplicationExplanationOnEvaluationDate(
                    fact_id=change.fact_id,
                    required_result=(
                        f"In {change.part_of_result}, {change.required_result}"
                    ),
                    source_session_id=change.source_session_id,
                    exact_supporting_source_text=change.exact_supporting_source_text,
                    why_source_supports_required_result=(
                        change.why_source_supports_required_result
                    ),
                    why_fact_still_applies_on_evaluation_date=(
                        change.why_fact_still_applies_on_evaluation_date
                    ),
                )
                for change in self.memory_changes
            ],
            why_listed_tools_can_complete_requested_work=(
                self.why_listed_tools_can_complete_requested_work
            ),
        )


MIN_PROPOSAL_COUNT = 1
MAX_PROPOSAL_COUNT = 25
MAX_CANDIDATE_TASKS = 250


def validate_proposal_count(count: int) -> int:
    """Require a supported number of proposals for one planning batch."""
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or not MIN_PROPOSAL_COUNT <= count <= MAX_PROPOSAL_COUNT
    ):
        raise ValueError(
            f"proposal count must be an integer from {MIN_PROPOSAL_COUNT} through "
            f"{MAX_PROPOSAL_COUNT}"
        )
    return count


class ProposalBatch(BaseModel):
    """A reusable planner response containing one planner shard."""

    model_config = ConfigDict(extra="forbid")

    checkpoint_identity: StrictStr
    persona: StrictStr
    evaluation_date: StrictStr
    proposals: list[TestProposal] = Field(
        min_length=MIN_PROPOSAL_COUNT,
        max_length=MAX_PROPOSAL_COUNT,
    )

    @field_validator("checkpoint_identity", "persona", "evaluation_date")
    @classmethod
    def _identity_text_is_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("proposal batch identity fields must not be empty")
        return value.strip()


def proposal_batch_schema(count: int) -> dict[str, Any]:
    """Return the legacy one-call planner schema for old saved artifacts."""
    count = validate_proposal_count(count)
    schema = ProposalBatch.model_json_schema()
    proposals = schema["properties"]["proposals"]
    proposals["minItems"] = count
    proposals["maxItems"] = count
    return schema


def situation_proposal_batch_schema(count: int) -> dict[str, Any]:
    """Return the first-call schema for exactly ``count`` situation proposals."""
    count = validate_proposal_count(count)
    schema = SituationProposalBatch.model_json_schema()
    proposals = schema["properties"]["proposals"]
    proposals["minItems"] = count
    proposals["maxItems"] = count
    return schema


def evidence_decision_batch_schema(count: int) -> dict[str, Any]:
    """Return the second-call schema for one decision per situation proposal."""
    count = validate_proposal_count(count)
    schema = EvidenceDecisionBatch.model_json_schema()
    evidence = schema["$defs"]["RememberedResultEvidence"]
    evidence["required"] = list(evidence["properties"])
    evidence["properties"]["why_required_for_work"].pop("default", None)
    evidence["properties"]["why_required_for_work"]["minLength"] = 1
    decisions = schema["properties"]["decisions"]
    decisions["minItems"] = count
    decisions["maxItems"] = count
    return schema


class TestIdea(BaseModel):
    """Work proposed for approval, without a second planned answer key."""

    model_config = ConfigDict(extra="forbid")

    situation: StrictStr
    work: StrictStr
    fact_ids: list[StrictInt] = Field(min_length=1)
    expected_tools: list[StrictStr] = Field(min_length=1)
    history_needed: StrictStr
    likely_without_history: StrictStr

    @field_validator("situation", "work", "history_needed", "likely_without_history")
    @classmethod
    def _nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("idea text must not be empty")
        return value

    @field_validator("fact_ids")
    @classmethod
    def _positive_unique_facts(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value) or any(item <= 0 for item in value):
            raise ValueError("idea fact IDs must be unique positive integers")
        return value

    @field_validator("expected_tools")
    @classmethod
    def _unique_tools(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not item.strip() for item in value):
            raise ValueError("idea tools must be unique nonempty names")
        return value


class IdeaResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ideas: list[TestIdea] = Field(max_length=MAX_PROPOSAL_COUNT)
    shortfall_reason: StrictStr


class ApprovedIdea(TestIdea):
    """The program assigns identity before the human accepts a candidate plan."""

    authoring_version: Literal[4]
    idea_id: StrictInt = Field(gt=0)


class CandidateTaskBatch(BaseModel):
    """A checkpoint-bound set of tasks ready for detailed authoring."""

    model_config = ConfigDict(extra="forbid")

    checkpoint_identity: StrictStr
    persona: StrictStr
    evaluation_date: StrictStr
    # An evidence call may reject every proposal. That is a completed planning
    # run with zero tasks, not a malformed candidate plan.
    tasks: list["PlannedTask | ApprovedIdea"] = Field(
        default_factory=list,
        max_length=MAX_CANDIDATE_TASKS,
    )

    @model_validator(mode="after")
    def _one_authoring_generation(self) -> "CandidateTaskBatch":
        ideas = [task for task in self.tasks if isinstance(task, ApprovedIdea)]
        if ideas and len(ideas) != len(self.tasks):
            raise ValueError("a batch cannot mix old plans and version-4 ideas")
        if len({idea.idea_id for idea in ideas}) != len(ideas):
            raise ValueError("a batch cannot repeat an idea ID")
        return self

    @field_validator("checkpoint_identity", "persona", "evaluation_date")
    @classmethod
    def _identity_text_is_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("candidate task batch identity fields must not be empty")
        return value.strip()


class FactApplicationExplanationOnEvaluationDate(BaseModel):
    """Planner evidence for one fact-backed result."""

    model_config = ConfigDict(extra="forbid")

    fact_id: StrictInt = Field(gt=0)
    why_fact_still_applies_on_evaluation_date: StrictStr
    # These fields are empty only in plans accepted before the planner began
    # preserving source evidence. New plans always populate all four fields.
    required_result: StrictStr = ""
    source_session_id: StrictStr = ""
    exact_supporting_source_text: StrictStr = ""
    why_source_supports_required_result: StrictStr = ""

    @field_validator("why_fact_still_applies_on_evaluation_date")
    @classmethod
    def _required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("fact application explanation must not be empty")
        return value.strip()

    @field_validator(
        "required_result",
        "source_session_id",
        "exact_supporting_source_text",
        "why_source_supports_required_result",
    )
    @classmethod
    def _optional_evidence_text(cls, value: str) -> str:
        return value.strip()


class PlannedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # New plans preserve these fields separately. task_description is retained
    # only so already accepted one-call plans can still be read.
    task_description: StrictStr | None = None
    new_situation: StrictStr | None = None
    requested_work: StrictStr | None = None
    work_items: list[PlannedWorkItem] = Field(default_factory=list)
    fact_ids: list[StrictInt] = Field(min_length=1)
    required_results: list[RequiredResult] = Field(min_length=1)
    expected_tools: list[StrictStr] = Field(min_length=1)
    action_without_memory: StrictStr
    why_memory_changes_result: StrictStr
    fact_application_explanations_on_evaluation_date: list[
        FactApplicationExplanationOnEvaluationDate
    ] = Field(min_length=1)
    why_listed_tools_can_complete_requested_work: StrictStr

    @field_validator("fact_ids")
    @classmethod
    def _unique_positive_fact_ids(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)) or any(fact_id <= 0 for fact_id in value):
            raise ValueError("fact_ids must contain unique positive integers")
        return value

    @field_validator("task_description", "new_situation", "requested_work")
    @classmethod
    def _optional_text_is_not_empty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("planned task text must not be empty")
        return value.strip()

    @field_validator(
        "why_memory_changes_result", "why_listed_tools_can_complete_requested_work"
    )
    @classmethod
    def _required_text_is_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("planned task text must not be empty")
        return value.strip()

    @field_validator("expected_tools")
    @classmethod
    def _unique_tool_names(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(not tool.strip() for tool in value):
            raise ValueError("expected_tools must contain unique non-empty tool names")
        return value

    @model_validator(mode="after")
    def _new_or_legacy_task_has_a_complete_shape(self) -> "PlannedTask":
        has_new_shape = (
            self.new_situation is not None
            or self.requested_work is not None
            or bool(self.work_items)
        )
        if has_new_shape:
            if self.task_description is not None:
                raise ValueError(
                    "new planned tasks must use new_situation and requested_work, not task_description"
                )
            if self.new_situation is None or self.requested_work is None or not self.work_items:
                raise ValueError(
                    "new planned tasks need new_situation, requested_work, and work_items"
                )
            work_item_ids = {item.work_item_id for item in self.work_items}
            if len(work_item_ids) != len(self.work_items):
                raise ValueError("new planned tasks must not repeat work_item_id values")
            missing_work_items = [
                result.result
                for result in self.required_results
                if result.work_item_id not in work_item_ids
            ]
            if missing_work_items:
                raise ValueError(
                    "every new planned required result must reference an existing work_item_id"
                )
        elif self.task_description is None:
            raise ValueError("legacy planned tasks need task_description")

        selected = set(self.fact_ids)
        covered = {
            fact_id
            for result in self.required_results
            for fact_id in result.fact_ids
        }
        if covered != selected:
            raise ValueError(
                "every and only planned fact_ids must support a required result; "
                f"facts={sorted(selected)}, supporting={sorted(covered)}"
            )
        explained = {
            explanation.fact_id
            for explanation in self.fact_application_explanations_on_evaluation_date
        }
        if explained != selected:
            raise ValueError(
                "fact_application_explanations_on_evaluation_date must cover every "
                "and only planned fact_ids; "
                f"facts={sorted(selected)}, explained={sorted(explained)}"
            )
        return self


class AuthoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    persona: StrictStr
    checkpoint: StrictStr
    evaluation_date: StrictStr
    planner_model: StrictStr
    designer_model: StrictStr
    query_model: StrictStr
    persona_state: StrictStr | None = None
    rejected_fact_ids: list[StrictInt] = Field(default_factory=list)
    writer_max_input_tokens: StrictInt = Field(default=100000, gt=0)
    max_model_corrections: StrictInt = Field(default=1, ge=1, le=3)

    @field_validator("rejected_fact_ids")
    @classmethod
    def _unique_positive_target_fact_ids(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)) or any(fact_id <= 0 for fact_id in value):
            raise ValueError("target fact IDs must contain unique positive integers")
        return value
