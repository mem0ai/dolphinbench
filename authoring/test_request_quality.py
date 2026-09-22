"""Local review-contract tests, not claims about a model's writing judgment."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from authoring.final_trace_review import CurrentFinalTraceReviewResponse, FinalTraceDecision, FinalTraceReviewResponse, _validate_issue_citations
from authoring.preflight import PreflightResponse
from authoring import test_final_trace_review_evidence as evidence_fixture


def assessment(explanation: str, *, passes: bool = True) -> dict:
    return {"passes": passes, "explanation": explanation, "issue_indexes": [] if passes else [0]}


def current_response() -> dict:
    raw = evidence_fixture.FinalTraceReviewEvidenceTests._response()
    raw["audit"]["request_quality"] = {
        "reader_and_goal": "Morgan needs an investor email with current funding context.",
        "work_is_plausible": assessment("The investor needs an update on the company's funding."),
        "scope_is_necessary": assessment("Funding context serves the investor update; no unrelated work is added."),
        "request_is_natural": assessment("Send the investor update asks for work without listing the funding values."),
    }
    # Use one content check that also proves the final action occurred.
    raw["audit"]["grading_assertions"] = [raw["audit"]["grading_assertions"][1]]
    row = raw["audit"]["grading_assertions"][0]
    row["assertion_index"] = 0
    row["result_quality"] = assessment("Funding content is necessary to the update, and this is the only check.")
    for key in ("user_request_requirements", "planned_required_results"):
        raw["audit"][key][0]["assertion_indexes"] = [0]
    return raw


class RequestQualityTests(unittest.TestCase):
    def test_new_reviews_require_quality_but_historical_records_remain_readable(self):
        old = evidence_fixture.FinalTraceReviewEvidenceTests._response()
        FinalTraceReviewResponse.model_validate(old)
        FinalTraceDecision.model_validate(old)
        with self.assertRaises(ValidationError):
            CurrentFinalTraceReviewResponse.model_validate(old)
        response = CurrentFinalTraceReviewResponse.model_validate(current_response())
        saved = response.model_dump(mode="json")
        self.assertEqual(FinalTraceDecision.model_validate(saved).audit.model_dump(mode="json"), saved["audit"])

    def test_both_live_schemas_require_quality_and_each_check_justification(self):
        for model in (CurrentFinalTraceReviewResponse, PreflightResponse):
            schema = model.model_json_schema()
            self.assertEqual(schema["properties"]["audit"]["$ref"], "#/$defs/CurrentFinalTraceAudit")
            self.assertIn("request_quality", schema["$defs"]["CurrentFinalTraceAudit"]["required"])
            self.assertIn("result_quality", schema["$defs"]["CurrentGradingAssertionAudit"]["required"])
            for definition in schema["$defs"].values():
                if definition.get("type") == "object":
                    self.assertEqual(set(definition["required"]), set(definition["properties"]))
                    self.assertFalse(definition["additionalProperties"])
        raw = current_response()
        del raw["audit"]["grading_assertions"][0]["result_quality"]
        with self.assertRaises(ValidationError):
            CurrentFinalTraceReviewResponse.model_validate(raw)

    def rejected(self, field="scope_is_necessary", owner="planning"):
        raw = current_response()
        raw["accept"] = False
        raw["audit"]["request_quality"][field] = assessment("The task adds screening-policy reporting to a closeout count.", passes=False)
        raw["issues"] = [{
            "owning_step": owner,
            "specific_problem": "The approved task adds screening-policy reporting without explaining why this closeout needs it.",
            "required_correction": "Require a newly approved idea with a justified scope; do not silently remove approved work.",
            "request_requirement_quote": "Also state the current front-door screening requirements and return behavior",
            "source_citations": [],
        }]
        return raw

    def test_alex_002_scope_failure_routes_to_planning_not_a_writer_rewrite(self):
        fixture = json.loads((Path(__file__).parent / "testdata" / "alex_002_request_quality.json").read_text())
        raw = self.rejected()
        raw["test_id"] = 2
        response = CurrentFinalTraceReviewResponse.model_validate(raw)
        _validate_issue_citations(response, {"user_request": fixture["user_request"]})
        decision = FinalTraceDecision.model_validate(response.model_dump(mode="json"))
        self.assertFalse(decision.accept)
        self.assertEqual(decision.part_to_correct, "planning")
        self.assertIn("screening requirements", fixture["work_items"][1]["work"])
        self.assertIn("Nadia remains responsible", fixture["work_items"][3]["work"])

    def test_failed_assessment_cannot_be_accepted_or_routed_to_execution(self):
        for field in ("work_is_plausible", "scope_is_necessary", "request_is_natural"):
            raw = self.rejected(field)
            raw["accept"] = True
            raw["issues"] = []
            with self.subTest(field=field), self.assertRaises(ValidationError):
                CurrentFinalTraceReviewResponse.model_validate(raw)
        with self.assertRaisesRegex(ValidationError, "owns the failure"):
            CurrentFinalTraceReviewResponse.model_validate(self.rejected(owner="execution"))

    def test_failed_assessment_requires_a_real_issue_and_explanation(self):
        for indexes in ([], [-1], [0, 0], [8]):
            raw = self.rejected()
            raw["audit"]["request_quality"]["scope_is_necessary"]["issue_indexes"] = indexes
            with self.subTest(indexes=indexes), self.assertRaises(ValidationError):
                CurrentFinalTraceReviewResponse.model_validate(raw)
        for field in ("work_is_plausible", "scope_is_necessary", "request_is_natural"):
            raw = current_response()
            raw["audit"]["request_quality"][field]["explanation"] = " "
            with self.subTest(field=field), self.assertRaises(ValidationError):
                CurrentFinalTraceReviewResponse.model_validate(raw)

    def test_saved_current_report_cannot_bypass_quality_through_legacy_reader(self):
        raw = self.rejected()
        raw["accept"] = True
        raw["issues"] = []
        with self.assertRaisesRegex(ValidationError, "cannot fail a quality assessment"):
            FinalTraceReviewResponse.model_validate(raw)

    def test_writer_wording_failure_routes_to_request_correction(self):
        raw = self.rejected("request_is_natural", "user_request")
        raw["issues"][0]["specific_problem"] = "The writer added an evaluator-style checklist to a focused approved task."
        raw["issues"][0]["required_correction"] = "Rewrite the request in the user's language without changing approved work."
        response = CurrentFinalTraceReviewResponse.model_validate(raw)
        self.assertEqual(FinalTraceDecision.model_validate(response.model_dump()).part_to_correct, "user_request")

    def test_unnecessary_or_duplicate_grading_check_blocks_acceptance(self):
        raw = current_response()
        row = raw["audit"]["grading_assertions"][0]
        row["result_quality"] = assessment("Another assertion already proves the same funding content.", passes=False)
        raw["accept"] = False
        raw["issues"] = self.rejected(owner="grading_checks")["issues"]
        response = CurrentFinalTraceReviewResponse.model_validate(raw)
        self.assertEqual(FinalTraceDecision.model_validate(response.model_dump()).part_to_correct, "grading_checks")
        raw["accept"] = True
        raw["issues"] = []
        with self.assertRaises(ValidationError):
            CurrentFinalTraceReviewResponse.model_validate(raw)

    def test_valid_quality_reports_have_no_vocabulary_or_task_size_filter(self):
        cases = (
            "An investor email needs the remembered $18M Northstar-led Series B as body content.",
            "An engineering handoff needs metrics-router rollback authority and required evidence fields.",
            "A work trip requires both a flight and a hotel within the remembered combined spending limit.",
            "A dinner order needs the remembered restaurant and dish as tool arguments.",
        )
        for goal in cases:
            raw = current_response()
            quality = raw["audit"]["request_quality"]
            quality["reader_and_goal"] = goal
            quality["work_is_plausible"] = assessment(goal)
            quality["scope_is_necessary"] = assessment(goal)
            with self.subTest(goal=goal):
                self.assertTrue(CurrentFinalTraceReviewResponse.model_validate(raw).accept)

if __name__ == "__main__":
    unittest.main()
