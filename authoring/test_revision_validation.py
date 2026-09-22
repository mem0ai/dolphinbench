import json
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from authoring import test_release_revision
from authoring.create_tests import main
from authoring.revision_inputs import load_release_validation_inputs
from authoring.revision_validation import execute_release_validation, prepare_release_validation


class RevisionValidationTest(unittest.TestCase):
    sha = staticmethod(test_release_revision.ReleaseRevisionTest.sha)
    save_proposal = test_release_revision.ReleaseRevisionTest.save_proposal
    prepare = test_release_revision.ReleaseRevisionTest.prepare

    def setUp(self):
        test_release_revision.ReleaseRevisionTest.setUp(self)
        self.proposal["tests"][0]["reason"] = "Remove an unsupported grading requirement."
        self.save_proposal()
        self.prepare()
        checkpoint = self.root / "checkpoint"
        checkpoint.mkdir()
        (checkpoint / "life_sim.yaml").write_text("original history")
        self.context = SimpleNamespace(persona="test", checkpoint_identity="checkpoint", checkpoint_path=checkpoint)
        trace_path = self.root / "trace.json"
        trace_path.write_text(json.dumps({
            "current_history_sha256": self.sha(checkpoint / "life_sim.yaml"),
            "approved_proposal": {"sha256": self.sha(self.proposal_path)},
            "certification_note": "Missing original settings.", "certification": {"001": {}},
        }))
        self.approval = {"version": 2, "kind": "exact_release_revision",
            "revision": {"path": str(self.out / "revision.json"), "sha256": self.sha(self.out / "revision.json")},
            "saved_run_trace": {"path": str(trace_path), "sha256": self.sha(trace_path)},
            "legacy_reuse_approval": {"approved": True, "test_ids": ["001"], "limitation": "Missing original settings."}}
        self.approval_path = self.root / "approval.json"
        self.save_approval()

    def save_approval(self):
        self.approval_path.write_text(json.dumps(self.approval))

    def load(self):
        return load_release_validation_inputs(root=self.root, approval_path=self.approval_path,
                                             config_path=self.config, context=self.context)

    def test_verifies_all_200_files_before_loading_saved_executions(self):
        with patch("authoring.revision_inputs.authenticate_legacy_execution", return_value={}) as authenticate:
            inputs = self.load()
            self.assertEqual(len(inputs["candidates"]), 200)
            authenticate.assert_called_once()
            (self.out / "tests" / "200.yaml").write_text("changed")
            with self.assertRaisesRegex(ValueError, "source changed"):
                self.load()
            self.assertEqual(authenticate.call_count, 1)

    def test_missing_legacy_approval_stops_before_execution_reuse(self):
        self.approval["legacy_reuse_approval"]["approved"] = False
        self.save_approval()
        with patch("authoring.revision_inputs.authenticate_legacy_execution") as authenticate:
            with self.assertRaisesRegex(ValueError, "explicit approval"):
                self.load()
            authenticate.assert_not_called()

    def test_changed_source_config_is_rejected(self):
        (self.out / "source_config.yaml").write_text("changed")
        with self.assertRaisesRegex(ValueError, "config differs"):
            self.load()

    def test_preparation_is_local_and_does_not_accept(self):
        output = self.root / "validation"
        request = {"required_example_check_ids": ["body"], "required_split_action_ids": []}
        with (
            patch("authoring.revision_validation.load_config", return_value=SimpleNamespace()),
            patch("authoring.revision_validation.load_checkpoint_context", return_value=self.context),
            patch("authoring.revision_inputs.authenticate_legacy_execution", return_value={}),
            patch("authoring.revision_validation.build_revision_review_request", return_value=request),
        ):
            result = prepare_release_validation(config_path=self.config, approval_path=self.approval_path,
                                                out=output, root=self.root)
            self.assertEqual(result["paid_calls_made"], 0)
            self.assertFalse(result["accepted"])
            self.assertEqual(result["planned_work"]["saved_certification_answers_to_regrade"], 4)
            self.assertEqual(result["planned_work"]["new_certification_attempts"], 0)
            self.assertEqual(result["planned_work"]["grading_example_sets"], 3)
            self.assertFalse((output / "accepted_batch").exists())
            with self.assertRaisesRegex(ValueError, "fresh output"):
                prepare_release_validation(config_path=self.config, approval_path=self.approval_path,
                                           out=output, root=self.root)

    def test_public_command_routes_local_preparation(self):
        with (
            patch("sys.argv", ["create_tests", "--config", str(self.config), "--out", "unused",
                               "--approved-revisions", str(self.approval_path), "--prepare-only"]),
            patch("authoring.revision_validation.prepare_release_validation", return_value={
                "status": "paid_approval_pending", "planned_work": {}}) as prepare,
            patch("authoring.create_tests.create_approved_revisions") as paid,
        ):
            main()
            prepare.assert_called_once()
            paid.assert_not_called()

    def test_public_command_routes_approved_version_two_execution(self):
        with (
            patch("sys.argv", ["create_tests", "--config", str(self.config), "--out", "unused",
                               "--approved-revisions", str(self.approval_path), "--confirm-paid-calls"]),
            patch("authoring.create_tests.create_approved_revisions") as paid,
            patch("authoring.revision_validation.execute_release_validation", return_value={
                "status": "validated", "accepted_count": 26}) as execute,
        ):
            main()
            execute.assert_called_once()
            self.assertTrue(execute.call_args.kwargs["confirm_paid_calls"])
            paid.assert_not_called()

    def test_review_recovery_never_reenters_an_unselected_accepted_test(self):
        with patch("authoring.revision_inputs.authenticate_legacy_execution", return_value={}):
            inputs = self.load()
        inputs["edits"]["002"] = inputs["edits"]["001"]
        inputs["gates"]["002"] = {}
        output = self.root / "recovery"
        for test_id, accepted, stage in (("001", False, "preflight"), ("002", True, "complete")):
            path = output / "tests" / test_id / "outcome.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "test_id": int(test_id), "accepted": accepted, "stage": stage,
                "status": "accepted_initial_final_review" if accepted else "pending",
                "candidate_sha256": self.sha(self.out / "tests" / f"{test_id}.yaml"),
            }))
        accepted_path = output / "tests" / "002" / "outcome.json"
        (output / "manifest.json").write_text(json.dumps({"results": {
            test_id: json.loads((output / "tests" / test_id / "outcome.json").read_text())
            for test_id in ("001", "002")}}))
        original = accepted_path.read_bytes()
        config = SimpleNamespace(persona="test", evaluation_date="2026-01-01", designer_model="test")

        def pending_review(**kwargs):
            target = kwargs["out"]
            (target / "preflight" / "model").mkdir(parents=True)
            result = {"accepted": False, "status": "pending", "stage": "preflight",
                      "candidate_sha256": self.sha(kwargs["candidate_path"])}
            (target / "outcome.json").write_text(json.dumps(result))
            (target / "preflight" / "model" / "step.json").write_text(json.dumps({
                "inputs": {"request": kwargs["request"]}}))
            return result

        with (
            patch("authoring.revision_validation.load_config", return_value=config),
            patch("authoring.revision_validation.load_checkpoint_context", return_value=self.context),
            patch("authoring.revision_validation.load_release_validation_inputs", return_value=inputs),
            patch("authoring.revision_inputs.load_review_context", return_value={
                "001": {"original_messages": [], "review_scope": "approved_delta_v1"}}),
            patch("authoring.revision_review_recovery.bind_review_recovery", return_value={"bound": True}),
            patch("authoring.revision_validation.build_revision_review_request", return_value={}),
            patch("construction.llm.AzureJsonClient"),
            patch("authoring.certify.verify_gate_python"),
            patch("harness.environment.call_ledger", return_value=nullcontext()),
            patch("authoring.revision_evaluation.load_evaluation_answers", return_value={}),
            patch("authoring.revision_evaluation.regrade_evaluation_answers", return_value={}),
            patch("authoring.revision_workflow.validate_revision_test", side_effect=pending_review) as validate,
        ):
            for _ in range(2):
                result = execute_release_validation(
                    config_path=self.config, approval_path=self.approval_path, out=output,
                    root=self.root, review_context_path=self.root / "context.json", confirm_paid_calls=True)
        validate.assert_called_once()
        self.assertIn("harness/environment.py", validate.call_args.kwargs["execution_identity"]["code"])
        self.assertEqual(validate.call_args.kwargs["candidate_path"].stem, "001")
        self.assertIn("not a new version-4 test", validate.call_args.kwargs["review_system"])
        self.assertEqual(accepted_path.read_bytes(), original)
        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(result["results"]["002"]["output_dir"], str(accepted_path.parent.resolve()))
