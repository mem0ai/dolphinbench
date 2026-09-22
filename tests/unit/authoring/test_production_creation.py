"""Production-mode coverage without model or provider calls."""

import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from authoring.complete_test import assemble_written_test, parse_writer_response, validate_writer_correction, writer_response_schema
from authoring.pipeline import AuthoringValidationError
from authoring.production_review import dependent_scope, model_request
from tests.unit.authoring.staged_fixture import StagedFixture
from tests.unit.authoring.creation_fixture import writer


class ProductionCreationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = StagedFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def draft(self):
        raw = writer()
        raw["quality_audit"] = None
        return raw

    def test_public_summary_does_not_report_acceptances_as_certification_count(self):
        from authoring.create_tests import main
        output = io.StringIO()
        with (patch("sys.argv", ["create_tests", "--config", str(self.fixture.config_path),
                                "--plan", str(self.fixture.plan), "--start-id", "301",
                                "--out", str(self.fixture.root / "new"),
                                "--runtime-policy", str(self.fixture.root / "policy.json"),
                                "--confirm-paid-calls"]),
              patch("authoring.create_tests.create_tests", return_value={
                  "final_review_accepted": 3, "candidate_count": 5}), redirect_stdout(output)):
            main()
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["accepted"], 3)
        self.assertEqual(summary["unresolved"], 2)
        self.assertNotIn("certified", summary)

    def test_fresh_public_creation_uses_the_same_pipeline_and_authenticates_progress(self):
        result, authors, reviews, registered = self.fixture._run_stages(
            [self.draft()], [{"decision": "approve", "issues": []}], workflow="production", fresh=True)
        self.assertEqual(result["final_review_accepted"], 1, result["authoring_results"])
        self.assertEqual(result["test_ids"], [301])
        self.assertEqual(len(authors.calls), 1)
        self.assertEqual(len(reviews.calls), 1)
        registered.assert_called_once()
        from authoring.progress import load_progress
        loaded = load_progress(source_run=self.fixture.root / "staged", test_id=301,
                               config_path=self.fixture.config_path, plan_path=self.fixture.plan,
                               config=self.fixture.config, context=self.fixture.context, tasks=[self.fixture.idea])
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded[0]["plan_position"], 1)

    def test_production_schema_allows_related_messages_but_not_unselected_facts(self):
        payload = copy.deepcopy(self.fixture.payload)
        payload["sources"].append({**payload["sources"][0], "source_session_id": "later",
                                   "message_id": "later:message:0", "fact_ids": [999]})
        def bindings(value):
            return writer_response_schema(payload=value)["$defs"]["SourceEvidence"]["anyOf"]
        self.assertFalse(any(v["properties"]["source_session_id"].get("const") == "later"
                             for v in bindings(payload)))
        variants = bindings({**payload, "workflow": "production"})
        self.assertTrue(any(v["properties"]["message_id"]["enum"] == ["later:message:0"] for v in variants))
        self.assertTrue(all(v["properties"]["fact_ids"]["items"]["enum"] == self.fixture.idea.fact_ids
                            for v in variants))

    def test_model_request_removes_only_duplicate_values_without_mutating_evidence(self):
        request = {"authoring_evidence": {"checks": [], "evidence": []}, "executions": [
            {"tool_calls": [{"tool": "send_email", "args": {"body": "Full content"}}],
             "grade": {"details": [{"name": "content", "ok": True, "call_evaluations": [
                 {"call_index": 0, "ok": True, "reason": "supported", "value": "Full content"}]}]}}]}
        compact = model_request(request)
        evaluation = compact["executions"][0]["check_results"][0]["call_evaluations"][0]
        self.assertEqual(evaluation, {"call_index": 0, "ok": True, "reason": "supported"})
        self.assertEqual(compact["executions"][0]["tool_calls"], request["executions"][0]["tool_calls"])
        self.assertIn("value", request["executions"][0]["grade"]["details"][0]["call_evaluations"][0])

    def test_complete_flow_has_one_review_and_no_preflight_examples(self):
        result, authors, reviews, registered = self.fixture._run_stages(
            [self.draft()], [{"decision": "approve", "issues": []}], workflow="production")
        self.assertEqual(result["final_review_accepted"], 1, result["authoring_results"])
        self.assertEqual(len(authors.calls), 1)
        self.assertEqual(len(reviews.calls), 1)
        registered.assert_called_once()
        events = json.loads((self.fixture.root / "staged/stage_events.json").read_text())
        self.assertEqual([row["stage"] for row in events], ["preparation", "writing", "certification", "review"])
        request = reviews.calls[0]["payload"]
        self.assertTrue(request["user_request"].startswith("I'm Morgan Chen."))
        self.assertNotIn("agent_inputs", request)
        self.assertNotIn("quality_audit", request["authoring_evidence"])
        self.assertEqual(len(request["executions"]), 4)
        self.assertTrue(all("tool_results" in shot for shot in request["executions"]))

    def test_failed_certification_gets_no_model_review_or_unbounded_reruns(self):
        def fail(gate):
            gate["valid"] = False
            gate["g1_pass_count"] = 1
            return gate
        result, authors, reviews, registered = self.fixture._run_stages(
            [self.draft()], [], workflow="production", transform_gate=fail)
        self.assertEqual(result["final_review_accepted"], 0)
        self.assertEqual(len(authors.calls), 1)
        self.assertEqual(reviews.calls, [])
        registered.assert_not_called()
        self.assertEqual(result["authoring_results"][0]["status"], "certification_pending")

    def test_paraphrased_support_is_not_rejected_as_a_failed_quote_match(self):
        raw = self.draft()
        raw["test"]["evidence"][0]["quote"] = "Northstar led the eighteen-million-dollar Series B."
        response = parse_writer_response(raw)
        arguments = dict(response=response, idea=self.fixture.idea, context=self.fixture.context,
                         evaluation_date=self.fixture.config.evaluation_date, test_id=1)
        with self.assertRaises(AuthoringValidationError):
            assemble_written_test(**arguments, payload=self.fixture.payload)
        candidate = assemble_written_test(**arguments, payload={**self.fixture.payload, "workflow": "production"})
        self.assertTrue(candidate["test"].startswith("I'm Morgan Chen."))
        raw["test"]["evidence"][0]["message_id"] = "invented"
        with self.assertRaises(AuthoringValidationError):
            assemble_written_test(**{**arguments, "response": parse_writer_response(raw)},
                                  payload={**self.fixture.payload, "workflow": "production"})

    def test_dependent_evidence_can_change_but_unrelated_checks_cannot(self):
        raw = self.draft()
        previous = parse_writer_response(raw)
        scope = dependent_scope(previous, parts={"checks"}, check_ids={"funding"})
        current = copy.deepcopy(raw)
        current["test"]["evidence"][0]["quote"] = "Faithful shorter supporting explanation."
        validate_writer_correction(previous, parse_writer_response(current), scope)
        current["test"]["checks"][0]["why_required"] = "Changed unaffected recipient check."
        with self.assertRaisesRegex(AuthoringValidationError, "unaffected check"):
            validate_writer_correction(previous, parse_writer_response(current), scope)

    def test_review_rejection_is_not_overridden_by_passing_grades(self):
        issue = {"part": "planning", "check_ids": [], "source_message_ids": ["s1:message:0"],
                 "problem": "The approved task is standalone recall rather than useful work.",
                 "required_change": "Approve different present-day work."}
        result, _, reviews, registered = self.fixture._run_stages(
            [self.draft()], [{"decision": "reject", "issues": [issue]}], workflow="production")
        self.assertEqual(result["final_review_accepted"], 0)
        self.assertEqual(len(reviews.calls), 1)
        registered.assert_not_called()

    def test_pending_review_cannot_become_an_idea_rejection_from_its_issue_category(self):
        issue = {"part": "planning", "check_ids": [], "source_message_ids": [],
                 "problem": "The saved evidence is inconclusive.", "required_change": "Keep this test pending."}
        result, _, _, registered = self.fixture._run_stages(
            [self.draft()], [{"decision": "pending", "issues": [issue]}], workflow="production")
        self.assertEqual(result["authoring_results"][0]["status"], "review_pending")
        self.assertEqual(json.loads((self.fixture.root / "staged/rejected_tests.json").read_text()), {"rejected_tests": []})
        registered.assert_not_called()

    def test_production_allows_only_one_targeted_correction(self):
        issue = {"part": "request", "check_ids": [], "source_message_ids": [],
                 "problem": "The request wording gives away the answer.", "required_change": "Remove the answer hint."}
        corrected = self.draft()
        corrected["test"]["request"] = "Email pat@example.com an investor update covering our funding."
        result, authors, reviews, _ = self.fixture._run_stages(
            [self.draft(), corrected], [{"decision": "correct", "issues": [issue]}] * 2,
            workflow="production")
        self.assertEqual(len(authors.calls), 2)
        self.assertEqual(len(reviews.calls), 2)
        self.assertEqual(result["authoring_results"][0]["production_corrections_used"], 1)
        self.assertEqual(result["authoring_results"][0]["status"], "correction_pending")
