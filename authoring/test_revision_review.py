import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jsonschema import ValidationError, validate

from authoring.revision_review_recovery import phase_bound_preflight_schema
from authoring.revision_review import (
    RevisionReviewResponse, build_revision_review_request, check_revision_examples,
    execute_revision_review, validate_revision_certification,
)


class RevisionReviewTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.out = Path(temp.name)
        self.original = {"id": "001", "narrative_anchor_date": "2026-09-14", "test": "Email Anna",
                         "load_bearing_facts": [1], "expected_tool_calls": ["send_email"], "mock_state": {},
                         "grade": {"type": "tool_trace", "config": {"assertions": [{
                             "type": "field_llm_judge", "tool": "send_email", "path": "args.body",
                             "criterion": "before"}]}}}
        self.candidate = copy.deepcopy(self.original)
        self.candidate["grade"]["config"]["assertions"][0]["criterion"] = "after"
        self.context = SimpleNamespace(tools={"send_email": {
            "arguments": ["to", "subject", "body"], "required_arguments": ["to", "subject", "body"]}},
            facts_by_id={1: {"source_session_ids": ["s1"]}},
            history=[{"id": "s1", "narrative_date": "2026-09-01", "messages": ["The original history"]}])
        self.edit = {"reason": "approved exact criterion repair", "edits_in_order": [{
            "path": "grade.config.assertions[0].criterion", "before": "before", "after": "after"}]}
        self.request = build_revision_review_request(original=self.original, candidate=self.candidate,
                                                     approved_edit=self.edit, context=self.context)

    def call(self, body):
        return {"tool": "send_email", "args_json": json.dumps({"to": "Anna", "subject": "Update", "body": body})}

    def response(self):
        return RevisionReviewResponse.model_validate({"decision": "approve", "issues": [], "examples": {
            "correct_calls": [self.call("right")], "variants": [{"check_id": "legacy_001_000",
            "correct_calls": [self.call("equivalent")], "incorrect_calls": [self.call("wrong")]}],
            "split_actions": []}})

    def grade(self, passed, detail=None):
        return {"passed": passed, "diagnostic": [], "details": [{"ok": passed if detail is None else detail}]}

    def final_request(self):
        request = copy.deepcopy(self.request)
        request.update(phase="after_oracle", legacy_execution_note={
            "approved": True, "test_ids": ["001"], "limitation": "Missing old initial transcripts."},
            certification_result={"valid": True, "with_history_pass_count": 2, "without_history_pass_count": 0})
        assertion = request["exact_grading_config"]["assertions"][0]
        request["executions"] = [{"with_memory": flag, "passed": flag,
            "grade": {"passed": flag, "details": [{"assertion": assertion, "ok": flag}]}}
            for flag in [True, True, False, False]]
        return request

    def test_legacy_final_review_accepts_only_named_transcript_exception(self):
        validate_revision_certification(self.final_request())

    def test_new_runs_still_require_full_transcript_evidence(self):
        request = self.final_request()
        request["legacy_execution_note"] = None
        with self.assertRaisesRegex(ValueError, "exact assistant inputs"):
            validate_revision_certification(request)

    def test_legacy_exception_does_not_allow_missing_or_contradictory_grades(self):
        for change in ({"grade": None}, {"passed": False}, {"error": "timeout"}):
            with self.subTest(change=change):
                request = self.final_request()
                request["executions"][0].update(change)
                with self.assertRaisesRegex(ValueError, "error or incomplete grading"):
                    validate_revision_certification(request)

    def test_legacy_exception_cannot_cover_another_test(self):
        request = self.final_request()
        request["legacy_execution_note"]["test_ids"] = ["002"]
        with self.assertRaisesRegex(ValueError, "explicit recorded exception"):
            validate_revision_certification(request)

    def test_review_does_not_migrate_legacy_grading(self):
        self.assertEqual(self.request["exact_grading_config"], self.candidate["grade"]["config"])
        self.assertEqual(self.request["check_version"], 1)
        self.assertNotIn("action_id", self.request["grading_checks"][0])
        self.assertEqual(self.request["sources"][0]["text"], "The original history")

    def test_metadata_only_conversion_does_not_mark_unchanged_criteria_as_changed(self):
        self.candidate["grade"]["config"]["assertions"][0].update(check_id="stable", action_id="action")
        self.original["grade"]["config"]["assertions"][0]["criterion"] = "after"
        with self.assertRaisesRegex(ValueError, "changed check or request"):
            build_revision_review_request(original=self.original, candidate=self.candidate,
                                          approved_edit=self.edit, context=self.context)

    def test_examples_grade_exact_config_and_resume_without_calls(self):
        grader = Mock(side_effect=[self.grade(True), self.grade(True), self.grade(False)])
        for _ in range(2):
            result = check_revision_examples(response=self.response(), request=self.request, out=self.out, grader=grader)
            self.assertTrue(result["passed"])
        self.assertEqual(grader.call_count, 3)
        self.assertNotIn("check_version", grader.call_args.args[1])

    def test_sibling_failure_does_not_validate_negative(self):
        grader = Mock(side_effect=[self.grade(True), self.grade(True), self.grade(False, detail=True)])
        result = check_revision_examples(response=self.response(), request=self.request, out=self.out, grader=grader)
        self.assertFalse(result["passed"])

    def test_omitted_changed_check_stops_before_grading(self):
        self.request["required_example_check_ids"].append("missing")
        grader = Mock()
        with self.assertRaisesRegex(ValueError, "every changed check"):
            check_revision_examples(response=self.response(), request=self.request, out=self.out, grader=grader)
        grader.assert_not_called()

    def test_technical_failure_never_counts_as_negative(self):
        grader = Mock(side_effect=[self.grade(True), self.grade(True), RuntimeError("transport")])
        with self.assertRaises(RuntimeError):
            check_revision_examples(response=self.response(), request=self.request, out=self.out, grader=grader)
        with self.assertRaisesRegex(ValueError, "previously started"):
            check_revision_examples(response=self.response(), request=self.request, out=self.out, grader=grader)
        self.assertEqual(grader.call_count, 3)

    def test_review_needs_paid_approval(self):
        client = Mock()
        with self.assertRaisesRegex(ValueError, "paid-call approval"):
            execute_revision_review(request=self.request, out=self.out, client=client)
        client.complete.assert_not_called()

    def test_completed_review_is_reused(self):
        client = SimpleNamespace(model="test-model", reasoning_effort="medium", max_output_tokens=1000,
                                 complete=Mock(return_value=self.response().model_dump(mode="json")))
        for _ in range(2):
            result = execute_revision_review(request=self.request, out=self.out, client=client, confirm_paid_calls=True)
            self.assertEqual(result.decision, "approve")
        client.complete.assert_called_once()

    def split_request_and_response(self, *, incomplete_content=False):
        self.original["grade"]["config"]["assertions"] = [{
            "type": "field_equals", "tool": "send_email", "path": "args.body", "value": "right"}]
        self.candidate["grade"]["config"] = {"check_version": 2, "assertions": [
            {**self.original["grade"]["config"]["assertions"][0], "check_id": "body", "action_id": "mail"},
            {"type": "field_equals", "tool": "send_email", "path": "args.to", "value": "Anna",
             "check_id": "target", "action_id": "mail"},
        ]}
        self.edit["edits_in_order"] = [{"operation": "bind_target_and_content_same_call"}]
        request = build_revision_review_request(original=self.original, candidate=self.candidate,
                                               approved_edit=self.edit, context=self.context)
        wrong_target = self.call("right")
        args = json.loads(wrong_target["args_json"])
        args["to"] = "Bob"
        wrong_target["args_json"] = json.dumps(args)
        split_wrong = copy.deepcopy(wrong_target)
        if incomplete_content:
            args["body"] = "wrong"
            split_wrong["args_json"] = json.dumps(args)
        response = RevisionReviewResponse.model_validate({"decision": "approve", "issues": [], "examples": {
            "correct_calls": [self.call("right")],
            "variants": [{"check_id": "target", "correct_calls": [self.call("right")],
                          "incorrect_calls": [wrong_target]}],
            "split_actions": [{"action_id": "mail", "calls": [split_wrong, self.call("wrong")]}],
        }})
        return request, response

    def test_actual_same_action_grader_rejects_split_target_and_content(self):
        request, response = self.split_request_and_response()
        result = check_revision_examples(response=response, request=request, out=self.out)
        self.assertTrue(result["passed"])
        self.assertEqual(request["required_example_check_ids"], ["target"])

    def test_an_ordinary_omission_does_not_prove_a_split_action_example(self):
        request, response = self.split_request_and_response(incomplete_content=True)
        result = check_revision_examples(response=response, request=request, out=self.out)
        self.assertFalse(result["passed"])


class RevisionReviewSchemaTest(unittest.TestCase):
    def setUp(self):
        self.schema = phase_bound_preflight_schema(RevisionReviewResponse.model_json_schema())

    def test_approval_without_examples_is_not_representable(self):
        with self.assertRaises(ValidationError):
            validate({"review": {"decision": "approve", "issues": [], "examples": None}}, self.schema)

    def test_pending_can_omit_examples(self):
        validate({"review": {"decision": "pending", "issues": [], "examples": None}}, self.schema)

    def test_approval_with_complete_examples_remains_valid(self):
        fixture = RevisionReviewTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        response = fixture.response().model_dump(mode="json")
        validate({"review": response}, self.schema)
        response["decision"] = "pending"
        with self.assertRaises(ValidationError):
            validate({"review": response}, self.schema)


if __name__ == "__main__":
    unittest.main()
