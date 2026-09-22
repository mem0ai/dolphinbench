import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from tests.unit.authoring import test_release_revision as fixtures
from authoring.revision_batch import collect_revision_batch, write_revision_batch


class RevisionBatchTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReleaseRevisionTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        revision = self.fixture.prepare()
        self.out = self.fixture.root / "validation"
        test_out = self.out / "tests" / "001"
        model = test_out / "final_review" / "model"
        model.mkdir(parents=True)
        candidate = yaml.safe_load(self.fixture.candidate.read_text())
        assertion = candidate["grade"]["config"]["assertions"][0]
        request = {"test_id": 1, "user_request": candidate["test"], "starting_app_data": {},
                   "exact_grading_config": candidate["grade"]["config"], "grading_checks": [assertion],
                   "legacy_execution_note": {"approved": True, "test_ids": ["001"], "limitation": "old records"},
                   "certification_result": {"valid": True, "with_history_pass_count": 2,
                                            "without_history_pass_count": 0},
                   "executions": [{"passed": flag, "with_memory": flag,
                       "grade": {"passed": flag, "details": [{"assertion": assertion, "ok": flag}]}}
                       for flag in [True, True, False, False]]}
        (test_out / "final_review_request.json").write_text(json.dumps(request))
        (model / "result.json").write_text(json.dumps({"decision": "approve", "issues": []}))
        (model / "step.json").write_text(json.dumps({"status": "completed", "inputs": {"request": request},
            "result_sha256": self.fixture.sha(model / "result.json")}))
        binding = {"path": str(self.fixture.out / "revision.json"),
                   "sha256": self.fixture.sha(self.fixture.out / "revision.json")}
        self.inputs = {"revision": revision, "stage": self.fixture.out,
                       "approval": {"revision": binding}, "edits": {"001": {}},
                       "candidates": {f"{i:03d}": {} for i in range(1, 201)}}
        self.manifest = {"status": "validated", "revision": binding,
                         "results": {"001": {"accepted": True, "candidate_sha256": self.fixture.sha(self.fixture.candidate),
                                             "output_dir": str(test_out)}}}
        self.directory = write_revision_batch(inputs=self.inputs, manifest=self.manifest, out=self.out)
        self.context = SimpleNamespace(persona="test", checkpoint_identity="checkpoint")
        self.config = SimpleNamespace(persona="test", evaluation_date="2026-09-14")

    def collect(self):
        return collect_revision_batch(directory=self.directory, context=self.context, config=self.config)

    def test_inherits_199_acceptances_and_checks_the_one_revised_acceptance(self):
        self.assertEqual(len(self.collect()), 200)
        for i in range(2, 201):
            self.assertEqual((self.directory / "candidates" / f"{i:03d}.yaml").read_bytes(),
                             (self.fixture.tests / f"{i:03d}.yaml").read_bytes())

    def test_changed_accepted_candidate_is_rejected(self):
        (self.directory / "candidates" / "200.yaml").write_text("changed")
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.collect()

    def test_changed_final_review_request_is_rejected(self):
        path = self.out / "tests" / "001" / "final_review_request.json"
        request = json.loads(path.read_text())
        request["user_request"] = "changed"
        path.write_text(json.dumps(request))
        with self.assertRaisesRegex(ValueError, "accepting final review"):
            self.collect()

    def test_existing_publication_command_accepts_the_batch_without_publishing(self):
        from authoring.publish_tests import collect_accepted_tests
        with patch("authoring.publish_tests.load_config", return_value=self.config), \
                patch("authoring.publish_tests.load_checkpoint_context", return_value=self.context):
            _, candidates, _ = collect_accepted_tests(config_path=self.fixture.config,
                                                     accepted_batch_dirs=[self.directory])
        self.assertEqual(len(candidates), 200)
        self.assertEqual(self.fixture.sha(self.fixture.tests / "001.yaml"),
                         self.fixture.proposal["published_tests_before_sha256"]["001"])

    def test_proposal_reads_the_batch_without_inventing_planner_evidence(self):
        from authoring.propose import _accepted_test_summaries
        summaries = _accepted_test_summaries(config=self.config, context=self.context,
                                             accepted_batch_dirs=[self.directory])
        self.assertEqual(len(summaries), 200)
        self.assertEqual(summaries[0]["memory_dependent_results"], ["revised"])

    def test_release_mapping_requires_explicit_approval(self):
        path = self.directory / "mapping.json"
        path.write_text(json.dumps({"approved": False, "replacements": {"001": {}}}))
        plan_path = self.directory / "planning_batch.json"
        plan = json.loads(plan_path.read_text())
        plan.update(version=2, release_mapping={"path": str(path), "sha256": self.fixture.sha(path)})
        plan_path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, "explicit approval"):
            self.collect()

    def test_release_mapping_preserves_already_accepted_revision(self):
        path = self.directory / "mapping.json"
        path.write_text(json.dumps({"approved": True, "approval_record": "approved",
                                   "replacements": {"001": {}}}))
        plan_path = self.directory / "planning_batch.json"
        plan = json.loads(plan_path.read_text())
        plan.update(version=2, release_mapping={"path": str(path), "sha256": self.fixture.sha(path)})
        plan_path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, "accepted revision"):
            self.collect()


if __name__ == "__main__":
    unittest.main()
