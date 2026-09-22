"""Saved-attempt grading recovery must never authorize assistant execution."""

import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from authoring.context import dump_json
from authoring.runtime_policy import CreationPolicy
from authoring.staged_creation import resume_staged
from authoring.test_four_stage_authoring import ScriptedClient, writer
from authoring.test_staged_creation import StagedCreationTests


class GradingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = StagedCreationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def source(self):
        def failed(result):
            result.update(valid=False, g1_pass_count=0, verdict="unsolvable_with_memory")
            for shot in result["shots"][:2]:
                shot["passed"] = shot["grade"]["passed"] = False
                for detail in shot["grade"]["details"]:
                    detail["ok"] = False
                    for evaluation in detail["call_evaluations"]:
                        evaluation["ok"] = False
            return result
        draft = writer()
        draft["quality_audit"] = None
        self.fixture._run_stages([draft], [], workflow="production", transform_gate=failed)
        policy = json.loads((self.root / "policy.json").read_text())
        policy["grading_only_recovery"] = True
        dump_json(self.root / "recovery_policy.json", policy)
        self.source_run = self.root / "staged"
        self.hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in self.source_run.rglob("*") if p.is_file()}

    def resume(self, reviews, drafts=(), *, source=None, name="recovered"):
        author = ScriptedClient(*drafts)
        reviewer = ScriptedClient(*reviews)
        with (patch("authoring.staged_creation.load_checkpoint_context", return_value=self.fixture.context),
              patch("authoring.propose.load_persona_state", return_value={
                  "approved_plan_paths": [self.fixture.plan], "accepted_batch_dirs": []}),
              patch.object(CreationPolicy, "client", side_effect=lambda model, writing=False: author if writing else reviewer),
              patch("authoring.staged_creation.certify_candidate", side_effect=AssertionError("no new oracle")) as execute,
              patch("authoring.run._certify_cached", side_effect=AssertionError("no execution fallback")) as cached,
              patch("authoring.propose.record_accepted_batch") as registered):
            result = resume_staged(config_path=self.fixture.config_path, plan_path=self.fixture.plan,
                                   source_run=source or self.source_run, out=self.root / name, test_ids=[1],
                                   policy_path=self.root / "recovery_policy.json", confirm_paid_calls=True)
        execute.assert_not_called()
        cached.assert_not_called()
        for path, digest in self.hashes.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        return result, author, reviewer, registered

    def issue(self, part="checks"):
        return {"decision": "correct", "issues": [{"part": part, "check_ids": ["funding"],
                "source_message_ids": ["s1:message:0"], "problem": "The funding check excludes equivalent wording.",
                "required_change": "Require the same funding meaning without a fixed formulation."}]}

    def correction(self):
        draft = writer()
        draft["quality_audit"] = None
        draft["test"]["checks"][1]["assertion"]["criterion"] = "Communicates the Series B, $18M and Northstar; accept equivalent wording."
        return draft

    def test_correction_regrades_four_saved_attempts_and_preserves_source(self):
        self.source()
        from authoring.create_tests import _default_regrader
        with patch("authoring.create_tests._default_regrader", wraps=_default_regrader) as grade:
            result, author, reviewer, registered = self.resume(
                [self.issue(), {"decision": "approve", "issues": []}], [self.correction()])
        self.assertEqual(result["final_review_accepted"], 1, result["authoring_results"])
        self.assertEqual(grade.call_count, 4)
        self.assertEqual(len(author.calls), 1)
        self.assertEqual(len(reviewer.calls), 2)
        self.assertIn("may fail", reviewer.calls[0]["system"])
        self.assertEqual(result["authoring_results"][0]["production_corrections_used"], 1)
        registered.assert_called_once()
        old = json.loads(next(self.source_run.rglob("initial_gate.json")).read_text())["result"]
        new = json.loads(next((self.root / "recovered").rglob("correction_1_gate.json")).read_text())["result"]
        self.assertEqual(old["agent_inputs"], new["agent_inputs"])
        for before, after in zip(old["shots"], new["shots"]):
            self.assertEqual({k: v for k, v in before.items() if k not in {"grade", "passed"}},
                             {k: v for k, v in after.items() if k not in {"grade", "passed"}})

    def test_failed_attempts_cannot_be_approved_unchanged(self):
        self.source()
        result, author, _, registered = self.resume([{"decision": "approve", "issues": []}])
        self.assertEqual(result["final_review_accepted"], 0)
        self.assertEqual(author.calls, [])
        registered.assert_not_called()

    def test_request_or_state_issue_cannot_trigger_a_writer(self):
        self.source()
        result, author, _, _ = self.resume([self.issue("request")])
        self.assertEqual(result["final_review_accepted"], 0)
        self.assertEqual(author.calls, [])
        self.assertEqual(result["authoring_results"][0]["next_action"], "requires_change_outside_approved_grading_scope")

    def test_correction_cannot_change_request(self):
        self.source()
        draft = self.correction()
        draft["test"]["request"] = "A changed request."
        result, _, _, _ = self.resume([self.issue()], [draft])
        self.assertEqual(result["final_review_accepted"], 0)
        self.assertIn("pending", result["authoring_results"][0]["status"])

    def test_no_history_success_after_correction_cannot_be_accepted(self):
        self.source()
        from authoring.create_tests import _default_regrader

        def all_pass(candidate, answer):
            result = _default_regrader(candidate, answer)
            result["passed"] = True
            for detail in result["details"]:
                detail["ok"] = True
                for evaluation in detail["call_evaluations"]:
                    evaluation["ok"] = True
            return result

        with patch("authoring.create_tests._default_regrader", side_effect=all_pass):
            result, _, reviews, _ = self.resume([self.issue()], [self.correction()])
        self.assertEqual(result["final_review_accepted"], 0)
        self.assertEqual(len(reviews.calls), 1)
        self.assertEqual(result["authoring_results"][0]["status"], "certification_pending")

    def test_missing_saved_gate_cannot_trigger_an_execution_fallback(self):
        self.source()
        path = self.source_run / "progress/001.json"
        progress = json.loads(path.read_text())
        progress["gate"] = None
        dump_json(path, progress)
        self.hashes[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        result, authors, reviews, _ = self.resume([])
        self.assertEqual(result["final_review_accepted"], 0)
        self.assertEqual(authors.calls, [])
        self.assertEqual(reviews.calls, [])
        self.assertIn("saved candidate and four attempts", result["authoring_results"][0]["reason"])

    def test_one_correction_limit_survives_resume(self):
        self.source()
        result, _, _, _ = self.resume([self.issue(), self.issue()], [self.correction()])
        self.assertEqual(result["final_review_accepted"], 0)
        result, author, _, _ = self.resume([self.issue()], source=self.root / "recovered", name="again")
        self.assertEqual(result["final_review_accepted"], 0)
        self.assertEqual(result["authoring_results"][0]["production_corrections_used"], 1)
        self.assertEqual(author.calls, [])

    def test_failed_evidence_validation_still_rejects_errors_and_bad_counts(self):
        self.source()
        result, _, reviewer, _ = self.resume([self.issue("system")])
        request_path = self.root / "recovered/final_trace_reviews/001/initial/request.json"
        request = json.loads(request_path.read_text())
        from authoring.review import validate_certification
        validate_certification(request, require_success=False)
        for field, value in (("error", "transport failed"), ("attempt_id", "1")):
            changed = copy.deepcopy(request)
            changed["executions"][0][field] = value
            with self.assertRaises(ValueError):
                validate_certification(changed, require_success=False)
        request["certification_result"]["with_history_pass_count"] = 2
        with self.assertRaises(ValueError):
            validate_certification(request, require_success=False)


if __name__ == "__main__":
    unittest.main()
