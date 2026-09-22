"""Focused tests for the visible final-review audit."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from tests.unit.authoring import test_final_review_correction
from authoring.preflight import PreflightResponse
from authoring.final_trace_review import (
    CurrentFinalTraceReviewResponse,
    FinalTraceDecision,
    FinalTraceReviewResponse,
    _validate_audit_matches_request,
    _validate_issue_citations,
)


class FinalTraceReviewEvidenceTests(unittest.TestCase):
    @staticmethod
    def _request() -> dict[str, object]:
        return {
            "grading_checks": [{"type": "tool_called"}, {"type": "field_regex"}],
            "planned_work": {
                "required_results": [
                    {"result": "Send the investor update."},
                ],
            },
            "source_evidence": [{"fact_id": 41}],
            "selected_tool_contracts": {"send_email": {"arguments": ["to", "body"]}},
        }

    @staticmethod
    def _response() -> dict[str, object]:
        return {
            "test_id": 121,
            "accept": True,
            "issues": [],
            "audit": {
                "user_request_requirements": [
                    {
                        "requirement": "Send an investor update.",
                        "assertion_indexes": [0, 1],
                    },
                ],
                "planned_required_results": [
                    {
                        "planned_result_index": 0,
                        "requirement": "Send the investor update.",
                        "assertion_indexes": [0, 1],
                    },
                ],
                "grading_assertions": [
                    {
                        "assertion_index": 0,
                        "normal_correct_value": "A send_email call is present.",
                        "incorrect_or_incomplete_value": "No send_email call is present.",
                    },
                    {
                        "assertion_index": 1,
                        "normal_correct_value": "The email includes the funding context.",
                        "incorrect_or_incomplete_value": "The email omits the funding context.",
                    },
                ],
                "source_and_date_checks": [
                    {
                        "fact_id": 41,
                        "source_supports_required_result": True,
                        "source_evidence": "The cited message states the funding context.",
                        "applies_on_evaluation_date": True,
                        "date_evidence": "No later message replaces the funding context.",
                    },
                ],
                "tool_capability_checks": [
                    {
                        "tool_name": "send_email",
                        "needed_capability": "Send a new investor email.",
                        "contract_supports_capability": True,
                        "contract_evidence": "The contract accepts to and body.",
                    },
                ],
            },
        }

    def test_response_schema_requires_a_visible_audit(self) -> None:
        schema = FinalTraceReviewResponse.model_json_schema()
        self.assertEqual(
            set(schema["properties"]), {"test_id", "accept", "issues", "audit"}
        )
        self.assertEqual(set(schema["required"]), set(schema["properties"]))

        raw = self._response()
        raw.pop("audit")
        with self.assertRaises(ValidationError):
            FinalTraceReviewResponse.model_validate(raw)

    def test_audit_must_cover_every_concrete_input(self) -> None:
        response = FinalTraceReviewResponse.model_validate(self._response())
        _validate_audit_matches_request(response, self._request())

        raw = self._response()
        raw["audit"]["grading_assertions"] = raw["audit"]["grading_assertions"][:1]
        response = FinalTraceReviewResponse.model_validate(raw)
        with self.assertRaisesRegex(ValueError, "every grading assertion exactly once"):
            _validate_audit_matches_request(response, self._request())

    def test_older_saved_decision_remains_loadable_without_an_audit(self) -> None:
        decision = FinalTraceDecision.model_validate(
            {
                "test_id": 121,
                "accept": False,
                "specific_problem": "The request reveals the answer.",
                "part_to_correct": "user_request",
                "required_correction": "Remove the answer.",
            }
        )
        self.assertIsNone(decision.audit)

    def _citation_issue(
        self, citations: list[dict[str, str]]
    ) -> dict[str, object]:
        return {
            "owning_step": "grading_checks",
            "specific_problem": "The check accepts the wrong routing.",
            "required_correction": "Reject routing to Rishi.",
            "request_requirement_quote": "Should Rishi be added?",
            "source_citations": citations,
        }

    def _citation_request(self) -> dict[str, object]:
        return {
            "user_request": "Should Rishi be added?",
            "source_evidence": [
                {
                    "source_sessions": [
                        {
                            "id": "atlas-handoff",
                            "messages": [
                                "Rishi is no longer the Atlas owner.\n\n"
                                "Leo owns the first technical read."
                            ],
                        },
                        {
                            "id": "atlas-scope",
                            "message": "Keep the Atlas work internal until the owner signs off.",
                        },
                    ]
                }
            ],
        }

    def test_one_source_citation_is_valid(self) -> None:
        raw = self._response()
        raw["accept"] = False
        raw["issues"] = [self._citation_issue([{
            "source_session_id": "atlas-handoff",
            "source_text_quote": "Rishi is no longer the Atlas owner.\n\nLeo owns the first technical read.",
        }])]
        response = FinalTraceReviewResponse.model_validate(raw)
        _validate_issue_citations(response, self._citation_request())

    def test_two_source_citations_are_valid(self) -> None:
        raw = self._response()
        raw["accept"] = False
        raw["issues"] = [self._citation_issue([
            {
                "source_session_id": "atlas-handoff",
                "source_text_quote": "Rishi is no longer the Atlas owner.",
            },
            {
                "source_session_id": "atlas-scope",
                "source_text_quote": "Keep the Atlas work internal until the owner signs off.",
            },
        ])]
        _validate_issue_citations(
            FinalTraceReviewResponse.model_validate(raw), self._citation_request()
        )

    def test_wrong_source_quote_is_rejected(self) -> None:
        raw = self._response()
        raw["accept"] = False
        raw["issues"] = [self._citation_issue([{
            "source_session_id": "atlas-handoff",
            "source_text_quote": "Keep the Atlas work internal until the owner signs off.",
        }])]
        with self.assertRaisesRegex(ValueError, "does not match"):
            _validate_issue_citations(
                FinalTraceReviewResponse.model_validate(raw), self._citation_request()
            )

    def test_wrong_source_id_is_rejected(self) -> None:
        raw = self._response()
        raw["accept"] = False
        raw["issues"] = [self._citation_issue([{
            "source_session_id": "not-a-source",
            "source_text_quote": "Rishi is no longer the Atlas owner.",
        }])]
        with self.assertRaisesRegex(ValueError, "does not match"):
            _validate_issue_citations(
                FinalTraceReviewResponse.model_validate(raw), self._citation_request()
            )

    def test_concatenated_source_id_is_rejected(self) -> None:
        raw = self._response()
        raw["accept"] = False
        raw["issues"] = [self._citation_issue([{
            "source_session_id": "atlas-handoff; atlas-scope",
            "source_text_quote": "Rishi is no longer the Atlas owner.",
        }])]
        with self.assertRaisesRegex(ValueError, "does not match"):
            _validate_issue_citations(
                FinalTraceReviewResponse.model_validate(raw), self._citation_request()
            )


class ReviewScopeTests(unittest.TestCase):
    def setUp(self):
        self.raw = {"test_id": 121, "accept": True, "issues": [],
                    "audit": test_final_review_correction.FinalReviewCorrectionTests._audit()}
        self.request = {
            "grading_checks": [{"type": "field_llm_judge", "tool": "send_email", "path": "args.body"}],
            "planned_work": {"required_results": [{"result": "Include the funding context."}]},
            "source_evidence": [{"fact_id": 1}],
            "selected_tool_contracts": {"send_email": {"state_effect": {"writes_state_keys": ["sent"]}}},
        }

    def validate(self):
        response = CurrentFinalTraceReviewResponse.model_validate(self.raw)
        _validate_audit_matches_request(response, self.request, require_grading_scope=True)
        return response

    def test_calendar_read_may_remain_ungraded(self):
        self.raw["audit"]["user_request_requirements"].append({
            "requirement": "Check the calendar before creating the reminders.",
            "grading_role": "ordinary_detail", "grading_reason": "An ordinary supporting read, not a remembered result.",
            "assertion_indexes": [],
        })
        self.validate()
        self.raw["audit"]["user_request_requirements"][-1]["assertion_indexes"] = [0]
        with self.assertRaisesRegex(ValueError, "remain ungraded"):
            self.validate()

    def test_required_scopes_and_planned_results_cannot_lose_checks(self):
        for role in ("remembered_result", "target_identity", "final_effect"):
            with self.subTest(role=role):
                row = self.raw["audit"]["user_request_requirements"][0]
                row.update(grading_role=role, assertion_indexes=[])
                with self.assertRaisesRegex(ValueError, "ungraded"):
                    self.validate()
        row["assertion_indexes"] = [0]
        self.raw["audit"]["planned_required_results"][0]["assertion_indexes"] = []
        with self.assertRaisesRegex(ValueError, "ungraded"):
            self.validate()

    def test_historical_unclassified_read_parses_but_needs_current_scope_review(self):
        # Riley 3's saved report has this shape. Loading is not acceptance.
        self.raw["audit"]["user_request_requirements"].append({
            "requirement": "Check Riley's calendar for the four reminder times before creating the reminders.",
            "assertion_indexes": [],
        })
        raw = dict(self.raw, reference_tool_calls_json="[]", grading_probes=[])
        PreflightResponse.model_validate(raw)
        with self.assertRaisesRegex(ValueError, "classify every request"):
            self.validate()

    def test_source_truth_and_current_dates_remain_required(self):
        row = self.raw["audit"]["source_and_date_checks"][0]
        for field in ("source_supports_required_result", "applies_on_evaluation_date"):
            with self.subTest(field=field):
                row[field] = False
                with self.assertRaisesRegex(ValueError, "source and date"):
                    self.validate()
                row[field] = True

    def test_negative_tool_check_cannot_prove_a_final_effect(self):
        self.request["grading_checks"][0]["type"] = "tool_not_called"
        with self.assertRaisesRegex(ValueError, "positive check proving final effect"):
            self.validate()

    def test_current_review_requires_necessity_separate_from_support(self):
        self.raw["audit"]["planned_required_results"][0]["why_required_for_work"] = ""
        with self.assertRaisesRegex(ValueError, "necessity"):
            self.validate()

    def test_scope_added_by_writer_is_not_forced_back_to_planning(self):
        self.raw["accept"] = False
        self.raw["issues"] = [{
            "owning_step": "user_request", "specific_problem": "Writer added an unrelated recap.",
            "required_correction": "Restore the approved scope.",
            "request_requirement_quote": "Include a full recap.", "source_citations": [],
        }]
        self.raw["audit"]["request_quality"]["scope_is_necessary"] = {
            "passes": False, "explanation": "The writer added work absent from the approved idea.", "issue_indexes": [0],
        }
        self.validate()

    def test_generated_review_schema_requires_scope_even_though_history_can_omit_it(self):
        schema = PreflightResponse.model_json_schema()
        row = schema["$defs"]["RequestRequirementAudit"]
        self.assertIn("grading_role", row["required"])
        self.assertEqual(row["properties"]["grading_role"]["type"], "string")


if __name__ == "__main__":
    unittest.main()
