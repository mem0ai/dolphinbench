import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import yaml

from authoring.revision_execution import grade_saved_attempts, load_reusable_gate, run_once


class RevisionExecutionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.candidate = {"id": "001", "narrative_anchor_date": "2026-09-14", "test": "Send the update",
                          "load_bearing_facts": [1], "expected_tool_calls": ["send_email"], "mock_state": {},
                          "grade": {"type": "tool_trace", "config": {"assertions": []}}}

    def bound_gate(self, shots=None):
        candidate_path = self.root / "candidate.yaml"
        candidate_path.write_text(yaml.safe_dump(self.candidate))
        candidate_hash = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        if shots is None:
            shots = [{"with_memory": flag, "tool_calls": [], "response_text": "answer", "error": None,
                      "oracle_errors": [], "passed": flag} for flag in [True, True, False, False]]
        gate_path = self.root / "gate.json"
        gate_path.write_text(json.dumps({"candidate_sha256": candidate_hash, "result": {"shots": shots}}))
        return dict(original=self.candidate, candidate=copy.deepcopy(self.candidate),
                    source_candidate=candidate_path, source_candidate_sha256=candidate_hash,
                    source_gate=gate_path, source_gate_sha256=hashlib.sha256(gate_path.read_bytes()).hexdigest())

    def test_grading_only_change_reuses_gate(self):
        args = self.bound_gate()
        args["candidate"]["grade"]["config"]["assertions"] = [{"type": "tool_called", "tool": "send_email"}]
        self.assertEqual(len(load_reusable_gate(**args)["result"]["shots"]), 4)

    def test_agent_input_changes_prevent_reuse(self):
        for field, replacement in [("test", "Changed"), ("mock_state", {"answer": "leak"}),
                                   ("load_bearing_facts", [2]), ("narrative_anchor_date", "2026-09-15")]:
            with self.subTest(field=field):
                args = self.bound_gate()
                args["candidate"][field] = replacement
                with self.assertRaisesRegex(ValueError, "changing agent inputs"):
                    load_reusable_gate(**args)

    def test_incomplete_gate_cannot_be_reused(self):
        args = self.bound_gate(shots=[])
        with self.assertRaisesRegex(ValueError, "exactly four"):
            load_reusable_gate(**args)

    def test_technical_error_cannot_be_reused(self):
        shots = [{"with_memory": f, "tool_calls": [], "response_text": "", "error": "timeout"}
                 for f in [True, True, False, False]]
        with self.assertRaisesRegex(ValueError, "incomplete"):
            load_reusable_gate(**self.bound_gate(shots))

    def test_changed_gate_hash_prevents_reuse(self):
        args = self.bound_gate()
        args["source_gate"].write_text("changed")
        with self.assertRaisesRegex(ValueError, "source changed"):
            load_reusable_gate(**args)

    def test_complete_operation_runs_once(self):
        action = Mock(return_value={"passed": False})
        for _ in range(2):
            self.assertEqual(run_once(directory=self.root, identity={"id": 1}, action=action), {"passed": False})
        action.assert_called_once()

    def test_changed_step_inputs_never_run_again(self):
        action = Mock(return_value={"passed": True})
        run_once(directory=self.root, identity={"id": 1}, action=action)
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            run_once(directory=self.root, identity={"id": 2}, action=action)
        action.assert_called_once()

    def test_unknown_started_operation_never_repeats(self):
        action = Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            run_once(directory=self.root, identity={"id": 1}, action=action)
        with self.assertRaisesRegex(ValueError, "previously started"):
            run_once(directory=self.root, identity={"id": 1}, action=action)
        action.assert_called_once()

    def test_failed_operation_never_repeats(self):
        action = Mock(side_effect=RuntimeError("failed"))
        with self.assertRaises(RuntimeError):
            run_once(directory=self.root, identity={"id": 1}, action=action)
        with self.assertRaisesRegex(ValueError, "previously started"):
            run_once(directory=self.root, identity={"id": 1}, action=action)
        action.assert_called_once()

    def test_changed_result_never_reuses_or_repeats(self):
        action = Mock(return_value={"passed": True})
        run_once(directory=self.root, identity={"id": 1}, action=action)
        (self.root / "result.json").write_text('{"passed": false}')
        with self.assertRaisesRegex(ValueError, "source changed"):
            run_once(directory=self.root, identity={"id": 1}, action=action)
        action.assert_called_once()

    def test_regrading_preserves_original_outputs_and_resumes(self):
        gate = load_reusable_gate(**self.bound_gate())
        grader = Mock(side_effect=[{"passed": p, "details": [], "diagnostic": []}
                                   for p in [True, True, False, False]])
        for _ in range(2):
            result = grade_saved_attempts(candidate=self.candidate, gate=gate, out=self.root / "grades",
                                         grading_identity={"version": 1}, grader=grader)
            self.assertTrue(result["result"]["valid"])
            self.assertEqual(result["new_agent_executions"], 0)
            self.assertEqual(result["original_execution_evidence"], gate["result"]["shots"])
        self.assertEqual(grader.call_count, 4)


if __name__ == "__main__":
    unittest.main()
