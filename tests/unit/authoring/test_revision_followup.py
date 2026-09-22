import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from authoring.revision_execution import run_once
from authoring.revision_followup import (
    actual_grading, authenticate_outcome, binding, completed_step, inherited_preflight,
    load_followup, run_followup, seed_evaluation_caches,
)
from authoring.revision_review import grading_identity
from tests.unit.authoring.revision_fixture import RevisionFixture


class FollowupTest(unittest.TestCase):
    def setUp(self):
        self.fixture = RevisionFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.run_workflow()
        self.out = self.fixture.out
        self.record = {**json.loads((self.out / "outcome.json").read_text()),
                       "output_dir": str(self.out)}
        self.request = copy.deepcopy(self.fixture.request)
        self.request["legacy_execution_note"] = None

    def test_inherit_completed_preflight_without_grader_or_reviewer_calls(self):
        evidence = inherited_preflight(self.record, self.request)
        self.assertTrue(evidence["grading_examples"]["passed"])
        self.fixture.client.complete.reset_mock()
        self.fixture.client.complete.side_effect = [{"decision": "approve", "issues": [], "examples": None}]
        self.fixture.grader.reset_mock()
        for _ in range(2):
            result = self.fixture.run_workflow(
                out=self.out / "retry", request=self.request, saved_gate=None,
                oracle_input={"version": 4}, inherited_preflight=evidence)
            self.assertTrue(result["accepted"], result)
        self.fixture.certifier.assert_called_once()
        self.fixture.client.complete.assert_called_once()
        self.fixture.grader.assert_not_called()

    def test_changed_request_cannot_inherit_preflight(self):
        self.request["user_request"] += " changed"
        with self.assertRaisesRegex(ValueError, "preflight inputs changed"):
            inherited_preflight(self.record, self.request)

    def test_changed_grading_cannot_inherit_preflight(self):
        with patch("authoring.revision_followup.grading_identity", return_value={
                "code": {"graders/mechanical.py": "changed"}, "judge": {}}):
            with self.assertRaisesRegex(ValueError, "grader, settings, or inputs differ"):
                inherited_preflight(self.record, self.request)

    def test_added_source_context_cannot_inherit_preflight(self):
        self.request["review_context"] = {"original_messages": [{"text": "new source"}]}
        with self.assertRaisesRegex(ValueError, "preflight inputs changed"):
            inherited_preflight(self.record, self.request)

    def test_uncertain_preflight_result_is_not_replayed(self):
        path = self.out / "examples" / "reference" / "step.json"
        state = json.loads(path.read_text())
        state["status"] = "started"
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, "authenticated completed step"):
            inherited_preflight(self.record, self.request)

    def test_accepted_candidate_review_is_authenticated(self):
        request = authenticate_outcome(self.record, self.fixture.fixture.candidate,
                                       binding(self.fixture.candidate)["sha256"])
        self.assertEqual(request["user_request"], self.request["user_request"])

    def test_changed_outcome_is_not_adopted(self):
        self.record["writer_calls"] = 8
        with self.assertRaisesRegex(ValueError, "bound decision"):
            authenticate_outcome(self.record, self.fixture.fixture.candidate,
                                 binding(self.fixture.candidate)["sha256"])

    def test_changed_candidate_is_not_adopted(self):
        candidate = copy.deepcopy(self.fixture.fixture.candidate)
        candidate["test"] = "changed"
        with self.assertRaisesRegex(ValueError, "bind the candidate"):
            authenticate_outcome(self.record, candidate, binding(self.fixture.candidate)["sha256"])

    def test_changed_result_hash_is_rejected(self):
        path = self.out / "preflight" / "model" / "result.json"
        path.write_text("{}")
        with self.assertRaisesRegex(ValueError, "source changed"):
            completed_step(path.parent)

    def test_output_directory_is_bound_to_retry_allowance(self):
        approval_path = self.out / "approval.json"
        approval_path.write_text(json.dumps({"version": 3, "kind": "exact_release_revision_followup",
                                            "approved": True, "output_dir": str(self.out)}))
        with self.assertRaisesRegex(ValueError, "one bound output directory"):
            load_followup(approval_path=approval_path, config_path=self.out / "unused",
                          out=self.out / "another")

    def test_no_paid_approval_stops_before_loading(self):
        with patch("authoring.revision_followup.load_followup") as load:
            with self.assertRaisesRegex(ValueError, "paid-call approval"):
                run_followup(config_path=self.out, approval_path=self.out, out=self.out)
            load.assert_not_called()

    def test_preserved_evaluation_grade_is_copied_without_calls(self):
        source = {"source": {"path": "original", "sha256": "original"}, "rows": {"001": {"answer": "saved"}}}
        identity = {"candidate": self.fixture.fixture.candidate, "source": source["source"],
                    "source_answer": source["rows"]["001"], "grading": actual_grading(grading_identity())}
        origin = self.out / "evaluation" / "builtin" / "grades" / "001"
        run_once(directory=origin, identity=identity, action=lambda: {"passed": True})
        loaded = {"inputs": {"candidates": {"001": self.fixture.fixture.candidate}, "gates": {"001": {}}},
                  "inherited": {"001": {}}, "approval": {"parent_decision": {"path": str(self.out / "manifest.json")}}}
        seed_evaluation_caches(loaded=loaded, sources={"builtin": source}, out=self.out / "followup")
        target = self.out / "followup" / "evaluation" / "builtin" / "grades" / "001"
        self.assertEqual(completed_step(origin), completed_step(target))
        loaded["inputs"]["candidates"]["001"] = {"different": True}
        with self.assertRaisesRegex(ValueError, "grade inputs differ"):
            seed_evaluation_caches(loaded=loaded, sources={"builtin": source}, out=self.out / "followup")


if __name__ == "__main__":
    unittest.main()
