import copy
import unittest
from unittest.mock import Mock, patch

from authoring import test_revision_review as fixtures
from authoring.revision_evaluation import regrade_evaluation_answers, grading_identity


class RevisionEvaluationTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RevisionReviewTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        originals = {f"{i:03d}": {**copy.deepcopy(self.fixture.original), "id": f"{i:03d}"}
                     for i in range(1, 201)}
        candidates = copy.deepcopy(originals)
        candidates["001"] = self.fixture.candidate
        candidates["002"]["test"] = "Revised request"
        self.inputs = {"originals": originals, "candidates": candidates,
                       "gates": {"001": {}}, "edits": {"001": {}, "002": {}}}
        self.sources = {}
        for provider in ("builtin", "mem0", "honcho"):
            rows = {test_id: {"test_id": test_id, "query": original["test"], "effective_tool_calls": [],
                             "passed": False, "grade": {"passed": False}, "driver_ok": True,
                             "token_usage": {"input_tokens": 200}, "latency_seconds": 2.5, "cost_usd": 0.1}
                    for test_id, original in originals.items()}
            self.sources[provider] = {"rows": rows, "source": {"path": provider, "sha256": provider},
                                      "original_summary": {"passes": 0}}
        self.grader = Mock(side_effect=lambda calls, config, **kwargs: {
            "passed": True, "details": [{"assertion": a, "ok": True} for a in config["assertions"]]})

    def run_regrade(self):
        return regrade_evaluation_answers(inputs=self.inputs, sources=self.sources,
                                         out=self.fixture.out / "evaluation", grader=self.grader)

    def test_regrades_only_changed_checks_and_reuses_completed_work(self):
        import json
        for _ in range(2):
            result = self.run_regrade()
            self.assertEqual(result["new_agent_executions"], 0)
            for provider, row in result["providers"].items():
                self.assertEqual(row["completed_count"], 199)
                self.assertEqual(row["preserved_test_count"], 198)
                self.assertEqual(row["pending_new_request_test_ids"], ["002"])
                self.assertEqual(row["completed_passes"], 1)
                self.assertEqual(row["original_passes_on_completed_tests"], 0)
                self.assertEqual(row["pass_delta_on_completed_tests"], 1)
                stored = json.loads((self.fixture.out / "evaluation" / provider / "regraded_results.json").read_text())
                first = stored["test_results"][0]
                for key in ("token_usage", "latency_seconds", "cost_usd", "effective_tool_calls"):
                    self.assertEqual(first[key], self.sources[provider]["rows"]["001"][key])
                self.assertEqual(stored["test_results"][1], self.sources[provider]["rows"]["003"])
        self.assertEqual(self.grader.call_count, 3)

    def test_changed_request_cannot_be_regraded_as_an_old_answer(self):
        self.inputs["gates"]["002"] = {}
        with self.assertRaisesRegex(ValueError, "changed request"):
            self.run_regrade()
        self.grader.assert_not_called()

    def test_incomplete_grades_are_not_results(self):
        self.grader.side_effect = lambda *args, **kwargs: {"passed": True}
        with self.assertRaisesRegex(ValueError, "incomplete or inconsistent"):
            self.run_regrade()

    def test_different_provider_source_cannot_reuse_a_grade(self):
        self.run_regrade()
        self.sources["builtin"]["source"]["sha256"] = "changed"
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            self.run_regrade()

    def test_pending_corrections_are_omitted_until_validated(self):
        first = regrade_evaluation_answers(inputs=self.inputs, sources=self.sources,
                                          out=self.fixture.out / "evaluation", grader=self.grader,
                                          validated_test_ids={"002"})
        self.grader.assert_not_called()
        self.assertEqual(first["pending_validation_test_ids"], ["001"])
        self.assertEqual(first["pending_saved_answers"], 3)
        for provider in first["providers"].values():
            self.assertEqual(provider["completed_count"], 198)
            self.assertEqual(provider["pending_revision_test_ids"], ["001"])
        second = self.run_regrade()
        self.assertEqual(second["saved_answers_regraded"], 3)
        self.assertEqual(second["pending_saved_answers"], 0)
        self.assertEqual(self.grader.call_count, 3)

    def test_review_code_changes_preserve_grades_but_grader_changes_do_not(self):
        self.run_regrade()
        identity = grading_identity()
        identity["code"]["authoring/revision_review.py"] = "review-only change"
        with patch("authoring.revision_evaluation.grading_identity", return_value=copy.deepcopy(identity)):
            self.run_regrade()
        self.assertEqual(self.grader.call_count, 3)
        for name in ("graders/mechanical.py", "harness/environment.py"):
            with self.subTest(name=name):
                changed = copy.deepcopy(identity)
                self.assertIn(name, changed["code"])
                changed["code"][name] = "grader dependency change"
                with patch("authoring.revision_evaluation.grading_identity", return_value=changed):
                    with self.assertRaisesRegex(ValueError, "inputs changed"):
                        self.run_regrade()
        self.assertEqual(self.grader.call_count, 3)


if __name__ == "__main__":
    unittest.main()
