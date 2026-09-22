import copy
import tempfile
import unittest
from pathlib import Path

from authoring.revision_execution import run_once
from tests.unit.authoring import revision_fixture as fixtures
from authoring.revision_review_recovery import bind_review_recovery, recover_final_review


class RevisionReviewRecoveryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.out = Path(temporary.name)
        self.context = self.out / "review_context.json"
        self.context.write_text("{}")
        self.original = {
            "approval_sha256": "approved", "config_sha256": "config",
            "execution": {"oracle_model": "fixed", "code": {
                "authoring/revision_workflow.py": "old-workflow", "harness/oracle.py": "oracle"}},
            "grading": {"judge": "fixed", "code": {
                "authoring/revision_review.py": "old-review", "graders/mechanical.py": "grader"}},
        }
        run_once(directory=self.out / "inputs", identity=self.original,
                 action=lambda: self.original)
        self.current = copy.deepcopy(self.original)
        self.current["execution"]["code"]["authoring/revision_workflow.py"] = "new-workflow"
        self.current["grading"]["code"]["authoring/revision_review.py"] = "new-review"

    def bind(self):
        return bind_review_recovery(out=self.out, identity=self.current, context_path=self.context)

    def test_allows_only_review_helpers_and_preserves_original_inputs(self):
        before = (self.out / "inputs" / "step.json").read_bytes()
        self.assertEqual(self.bind(), self.bind())
        self.assertEqual((self.out / "inputs" / "step.json").read_bytes(), before)
        self.assertEqual(len(list((self.out / "review_recovery_inputs").iterdir())), 1)

    def test_rejects_changed_approval_or_config(self):
        for key in ("approval_sha256", "config_sha256"):
            with self.subTest(key=key):
                self.current[key] = "changed"
                with self.assertRaisesRegex(ValueError, "cannot change"):
                    self.bind()
                self.current[key] = self.original[key]

    def test_rejects_changed_grader_or_oracle(self):
        for section, key in (("execution", "harness/oracle.py"),
                             ("grading", "graders/mechanical.py")):
            with self.subTest(section=section):
                self.current[section]["code"][key] = "changed"
                with self.assertRaisesRegex(ValueError, "cannot change"):
                    self.bind()
                self.current[section]["code"][key] = self.original[section]["code"][key]

    def test_rejects_changed_model_settings(self):
        self.current["grading"]["judge"] = "different"
        with self.assertRaisesRegex(ValueError, "cannot change"):
            self.bind()

    def test_binds_each_context_without_overwriting_earlier_context(self):
        first = self.bind()
        self.context.write_text('{"context": "changed"}')
        second = self.bind()
        self.assertNotEqual(first["review_context_sha256"], second["review_context_sha256"])
        self.assertEqual(len(list((self.out / "review_recovery_inputs").iterdir())), 2)


class FinalReviewRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RevisionFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.client.complete.side_effect = [
            self.fixture.fixture.response().model_dump(mode="json"),
            {"decision": "pending", "issues": [], "examples": None},
            {"decision": "approve", "issues": [], "examples": None},
        ]
        outcome = self.fixture.run_workflow()
        self.assertEqual(outcome["stage"], "final_review")
        self.assertFalse(outcome["accepted"])
        self.out = self.fixture.fixture.out / "final_recovery"

    def recover(self):
        return recover_final_review(candidate_path=self.fixture.candidate,
                                    previous_out=self.fixture.out, out=self.out,
                                    review_context={"original_messages": []}, client=self.fixture.client,
                                    system="Review complete calls", confirm_paid_calls=True)

    def test_recovers_only_review_without_execution_or_grading(self):
        old = (self.fixture.out / "outcome.json").read_bytes()
        grades = self.fixture.grader.call_count
        for _ in range(2):
            result = self.recover()
            self.assertTrue(result["accepted"], result)
            self.assertEqual(result["new_recovery_agent_executions"], 0)
            self.assertEqual(result["new_recovery_grades"], 0)
        self.assertEqual(self.fixture.client.complete.call_count, 3)
        self.assertEqual(self.fixture.grader.call_count, grades)
        self.fixture.certifier.assert_not_called()
        self.assertEqual((self.fixture.out / "outcome.json").read_bytes(), old)

    def test_changed_candidate_stops_before_another_review(self):
        self.fixture.candidate.write_text("changed")
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.recover()
        self.assertEqual(self.fixture.client.complete.call_count, 2)

    def test_changed_final_request_stops_before_another_review(self):
        import json
        path = self.fixture.out / "final_review_request.json"
        request = json.loads(path.read_text())
        request["sources"] = []
        path.write_text(json.dumps(request))
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.recover()
        self.assertEqual(self.fixture.client.complete.call_count, 2)


if __name__ == "__main__":
    unittest.main()
