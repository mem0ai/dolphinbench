import copy
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import yaml

from tests.unit.authoring import test_revision_review as fixtures
from authoring.revision_workflow import validate_revision_test


from tests.unit.authoring import revision_fixture


class RevisionWorkflowTest(revision_fixture.RevisionFixture):


    def test_saved_answers_are_regraded_once_and_never_reexecuted(self):
        for _ in range(2):
            result = self.run_workflow()
            self.assertTrue(result["accepted"], result)
            self.assertEqual(result["new_agent_executions"], 0)
        self.assertEqual(self.grader.call_count, 7)
        self.assertEqual(self.client.complete.call_count, 2)
        self.certifier.assert_not_called()
        gate = json.loads((self.out / "gate.json").read_text())
        self.assertEqual(gate["original_execution_evidence"], self.shots)

    def test_changed_request_certifies_once_without_technical_retries(self):
        self.request["legacy_execution_note"] = None
        for _ in range(2):
            result = self.run_workflow(saved_gate=None, oracle_input={"version": 4})
            self.assertTrue(result["accepted"], result)
            self.assertEqual(result["new_agent_executions"], 4)
        self.certifier.assert_called_once()
        self.assertEqual(self.certifier.call_args.kwargs["max_technical_retries"], 0)
        self.assertEqual(self.grader.call_count, 3)

    def test_example_disagreement_stops_before_certification(self):
        self.grader.side_effect = lambda *args, **kwargs: {"passed": False, "details": [{"ok": False}]}
        result = self.run_workflow()
        self.assertEqual(result["stage"], "grading_examples")
        self.assertFalse(result["accepted"])
        self.certifier.assert_not_called()
        self.client.complete.assert_called_once()

    def test_missing_new_execution_inputs_cannot_use_legacy_exception(self):
        self.request["legacy_execution_note"] = None
        with self.assertRaisesRegex(ValueError, "version-4 certification inputs"):
            self.run_workflow(saved_gate=None)
        self.client.complete.assert_not_called()

    def test_failed_certification_is_not_repeated(self):
        self.request["legacy_execution_note"] = None
        self.certifier.side_effect = RuntimeError("transport")
        for _ in range(2):
            result = self.run_workflow(saved_gate=None, oracle_input={"version": 4})
            self.assertEqual(result["stage"], "certification")
            self.assertFalse(result["accepted"])
        self.certifier.assert_called_once()

    def test_incomplete_fresh_evidence_cannot_be_accepted(self):
        self.request["legacy_execution_note"] = None
        self.certifier.side_effect = None
        self.certifier.return_value = {"shots": [], "valid": True, "g1_pass_count": 2, "g2_pass_count": 0}
        result = self.run_workflow(saved_gate=None, oracle_input={"version": 4})
        self.assertFalse(result["accepted"])
        self.assertIn("four-attempt certification", result["error"])

    def test_changed_runtime_is_rejected_before_reusing_or_making_calls(self):
        self.run_workflow()
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            self.run_workflow(execution_identity={"runtime": "changed"})
        self.assertEqual(self.client.complete.call_count, 2)

    def test_review_override_is_bound_and_used_in_both_reviews(self):
        result = self.run_workflow(review_system="Bound review-only recovery")
        self.assertTrue(result["accepted"])
        self.assertEqual([call.args[0] for call in self.client.complete.call_args_list],
                         ["Bound review-only recovery"] * 2)
        for stage in ("preflight", "final_review"):
            step = json.loads((self.out / stage / "model" / "step.json").read_text())
            self.assertEqual(step["inputs"]["system"], "Bound review-only recovery")
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            self.run_workflow(review_system="Different review")
        self.assertEqual(self.client.complete.call_count, 2)

    def test_phase_bound_preflight_keeps_raw_wrapper_and_normal_final_review(self):
        self.request["review_context"] = {"review_format": "phase_bound_v1"}
        self.client.complete.side_effect = [
            {"review": self.fixture.response().model_dump(mode="json")},
            {"decision": "approve", "issues": [], "examples": None},
        ]
        result = self.run_workflow()
        self.assertTrue(result["accepted"], result)
        raw = json.loads((self.out / "preflight" / "model" / "result.json").read_text())
        self.assertEqual(raw["review"]["decision"], "approve")
        calls = self.client.complete.call_args_list
        self.assertIn("review", calls[0].kwargs["response_schema"]["properties"])
        self.assertNotIn("review", calls[1].kwargs["response_schema"]["properties"])

    def test_no_paid_approval_stops_before_any_stage(self):
        with self.assertRaisesRegex(ValueError, "paid-call approval"):
            self.run_workflow(confirm_paid_calls=False)
        self.client.complete.assert_not_called()
        self.grader.assert_not_called()
        self.certifier.assert_not_called()


if __name__ == "__main__":
    unittest.main()
