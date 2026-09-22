import json
import hashlib
import shutil
import unittest
from unittest.mock import patch

from authoring.finalize_batch import finalize_accepted, write_completed_batch, write_repair_batches
from authoring.test_staged_creation import StagedCreationTests
from authoring.test_four_stage_authoring import writer, approving_review


class FinalizeBatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = StagedCreationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture._run_stages([writer()], [approving_review(before=True)])
        self.source = self.fixture.root / "staged"
        (self.source / "manifest.json").unlink()

    def finalize(self):
        return finalize_accepted(config_path=self.fixture.config_path, plan_path=self.fixture.plan,
            source_run=self.source, out=self.fixture.root / "finalized", test_ids=[1])

    def test_packages_only_completed_clean_evidence_without_model_calls(self):
        with (patch("authoring.finalize_batch.load_checkpoint_context", return_value=self.fixture.context),
              patch("authoring.propose.load_persona_state", return_value={"approved_plan_paths": [self.fixture.plan], "accepted_batch_dirs": []}),
              patch("authoring.propose.record_accepted_batch") as register,
              patch("construction.llm.AzureJsonClient.complete", side_effect=AssertionError("no provider calls"))):
            manifest = self.finalize()
        self.assertEqual(manifest["final_review_accepted"], 1)
        self.assertEqual(manifest["provider_calls"], 0)
        register.assert_called_once()

    def test_pending_work_cannot_be_finalized(self):
        path = self.source / "progress/001.json"
        progress = json.loads(path.read_text())
        progress["status"] = "review_pending"
        path.write_text(json.dumps(progress))
        with (patch("authoring.finalize_batch.load_checkpoint_context", return_value=self.fixture.context),
              patch("authoring.propose.load_persona_state", return_value={"approved_plan_paths": [self.fixture.plan], "accepted_batch_dirs": []}),
              patch("authoring.propose.record_accepted_batch") as register):
            with self.assertRaisesRegex(ValueError, "no completed acceptance"):
                self.finalize()
        register.assert_not_called()

    def test_unchanged_retry_inherits_authenticated_original_preflight(self):
        from pathlib import Path
        path = self.source / "progress/001.json"
        progress = json.loads(path.read_text())
        original_gate = Path(progress["gate"])
        predecessor_path = original_gate.parent / "progress.json"
        predecessor = json.loads(predecessor_path.read_text())
        predecessor["gate"] = str(original_gate)
        predecessor["artifact_hashes"][str(original_gate)] = hashlib.sha256(original_gate.read_bytes()).hexdigest()
        predecessor_path.write_text(json.dumps(predecessor))
        progress["artifact_hashes"].pop(str(predecessor_path), None)
        retry_gate = original_gate.with_name("execution_retry_gate.json")
        shutil.copy2(original_gate, retry_gate)
        progress["gate"] = str(retry_gate)
        progress["artifact_hashes"][str(retry_gate)] = hashlib.sha256(retry_gate.read_bytes()).hexdigest()
        progress["artifact_hashes"] = {key: value for key, value in progress["artifact_hashes"].items()
                                       if "_preflight/" not in key}
        path.write_text(json.dumps(progress))
        with (patch("authoring.finalize_batch.load_checkpoint_context", return_value=self.fixture.context),
              patch("authoring.propose.load_persona_state", return_value={"approved_plan_paths": [self.fixture.plan], "accepted_batch_dirs": []}),
              patch("authoring.propose.record_accepted_batch")):
            manifest = self.finalize()
        self.assertEqual(manifest["final_review_accepted"], 1)
        self.assertTrue(manifest["final_review_results"][0]["local_finalization_evidence"]["inherited_preflight"].endswith("initial_preflight"))


    def test_writer_preserves_candidates_and_separates_all_outcomes(self):
        root = self.fixture.root
        accepted = {}
        statuses = ("accepted_clean_certification", "accepted_initial_final_review",
                    "accepted_after_final_review_correction", "accepted_after_same_facts_redesign")
        for test_id in (1, 3, 5, 6):
            path = root / f"source-{test_id}.yaml"
            path.write_bytes(f"id: {test_id}\ntest: unchanged\n".encode())
            accepted[test_id] = path
        original = {i: p.read_bytes() for i, p in accepted.items()}
        tasks = {i: self.fixture.idea.model_copy(update={"idea_id": i}) for i in range(1, 7)}
        reviewed = [{"id": i, "status": status} for i, status in zip(accepted, statuses)]
        authored = [{"id": 2, "status": "replacement_required", "reason": "Unsupported request."},
                    {"id": 4, "status": "review_pending", "reason": "Incomplete evidence."}]
        for resumed in (None, {"authenticated_files": {"original.json": "unchanged-hash"}}):
            with self.subTest(resumed=resumed):
                out = root / ("completed-resume" if resumed else "completed-new")
                paths, results = write_completed_batch(
                    out=out, plan_path=self.fixture.plan, accepted=accepted,
                    tasks_by_id=tasks,
                    checkpoint_identity=self.fixture.context.checkpoint_identity,
                    persona=self.fixture.context.persona, evaluation_date=self.fixture.config.evaluation_date,
                    authoring_results=authored, final_review_results=reviewed, resumed_from=resumed,
                )
                batch = out / "accepted_batch"
                self.assertEqual(paths, [str(batch / "candidates" / f"{i:03d}.yaml") for i in accepted])
                for row, status in zip(results, statuses):
                    self.assertEqual(row["status"], status)
                    self.assertEqual(row["candidate_sha256"], hashlib.sha256(original[row["test_id"]]).hexdigest())
                    self.assertEqual((batch / "candidates" / f'{row["test_id"]:03d}.yaml').read_bytes(),
                                     original[row["test_id"]])
                plan = json.loads((batch / "planning_batch.json").read_text())
                self.assertEqual(plan["tasks"], [tasks[i].model_dump(mode="json") for i in accepted])
                provenance = json.loads((batch / "provenance.json").read_text())
                expected = {"checkpoint_identity": self.fixture.context.checkpoint_identity,
                            "accepted_test_ids": list(accepted), "source_manifest": str(out / "manifest.json"),
                            "results": results}
                if resumed is not None:
                    expected["resumed_from"] = resumed
                self.assertEqual(provenance, expected)
                rejected = json.loads((out / "rejected_tests.json").read_text())["rejected_tests"]
                self.assertEqual([row["test_id"] for row in rejected], [2])
                self.assertEqual(rejected[0]["failure_reason"], "Unsupported request.")
                self.assertEqual(json.loads((out / "pending_tests.json").read_text())["pending_tests"], [authored[1]])
                self.assertEqual({i: p.read_bytes() for i, p in accepted.items()}, original)

    def test_writer_rejects_unaccepted_completion_status(self):
        path = self.fixture.root / "candidate.yaml"
        path.write_text("id: 1\n")
        with self.assertRaisesRegex(ValueError, "invalid completion status"):
            write_completed_batch(
                out=self.fixture.root / "invalid-status", plan_path=self.fixture.plan,
                tasks_by_id={1: self.fixture.idea}, accepted={1: path},
                checkpoint_identity=self.fixture.context.checkpoint_identity,
                persona=self.fixture.context.persona, evaluation_date=self.fixture.config.evaluation_date,
                authoring_results=[], final_review_results=[{"id": 1, "status": "review_pending"}],
            )

    def test_repair_batches_preserve_grouping_and_append_without_changing_prior_files(self):
        root = self.fixture.root
        out = root / "repairs"
        tasks = {i: self.fixture.idea.model_copy(update={"idea_id": i}) for i in range(1, 15)}
        accepted = {}
        for i in reversed(tasks):
            accepted[i] = root / f"repair-{i}.yaml"
            accepted[i].write_bytes(f"id: {i}\ntest: unchanged\n".encode())
        statuses = {i: "accepted_after_final_review_correction" for i in tasks}

        def write(ids, preserve_existing=False):
            return write_repair_batches(
                out=out, context=self.fixture.context, config=self.fixture.config,
                tasks_by_id=tasks, accepted={i: accepted[i] for i in ids},
                accepted_statuses=statuses, source_manifest=self.source / "manifest.json",
                preserve_existing=preserve_existing,
            )

        batches = write(range(12, 0, -1))
        self.assertEqual([p.name for p in batches], ["part_01", "part_02"])
        for batch, ids in zip(batches, [list(range(1, 11)), [11, 12]]):
            provenance = json.loads((batch / "provenance.json").read_text())
            self.assertEqual(provenance["accepted_test_ids"], ids)
            self.assertEqual(provenance["source_manifest"], str(self.source / "manifest.json"))
            self.assertEqual(json.loads((batch / "planning_batch.json").read_text())["tasks"],
                             [tasks[i].model_dump(mode="json") for i in ids])
            for row in provenance["results"]:
                i = row["test_id"]
                self.assertEqual(row["status"], statuses[i])
                self.assertEqual(row["candidate_sha256"], hashlib.sha256(accepted[i].read_bytes()).hexdigest())
                self.assertEqual((batch / "candidates" / f"{i:03d}.yaml").read_bytes(), accepted[i].read_bytes())
        self.assertEqual(write([], preserve_existing=True), batches)
        for i, name in [(13, "continuation_01"), (14, "continuation_02")]:
            before = {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}
            batches = write([i], preserve_existing=True)
            self.assertEqual(batches[-1].name, name)
            self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(write([]), [])
        self.assertFalse((out / "accepted_batches").exists())
        for i, path in accepted.items():
            self.assertEqual(path.read_bytes(), f"id: {i}\ntest: unchanged\n".encode())
