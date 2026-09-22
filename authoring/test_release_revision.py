from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from authoring.release_revision import prepare_release_revision, replay_edits
from authoring.create_tests import main
from harness.dataset import load_test


class ReleaseRevisionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.yaml"
        self.config.write_text("persona: test\n")
        self.tests = self.root / "tests" / "test"
        self.tests.mkdir(parents=True)
        hashes = {}
        for i in range(1, 201):
            value = {"id": i, "narrative_anchor_date": "2026-09-14", "test": "Send the update.",
                     "load_bearing_facts": [1], "expected_tool_calls": ["send_email"],
                     "mock_state": {}, "grade": {"type": "explicit", "config": {
                         "assertions": [{"type": "field_llm_judge", "tool": "send_email",
                                         "path": "args.body", "criterion": "original"}]}}}
            path = self.tests / f"{i:03d}.yaml"
            path.write_text(yaml.safe_dump(value))
            hashes[f"{i:03d}"] = self.sha(path)
        self.release = self.root / "authoring" / "release_manifests" / "test.json"
        self.release.parent.mkdir(parents=True)
        self.release.write_text(json.dumps({"candidate_sha256": hashes, "test_count": 200,
                                           "test_ids": list(range(1, 201)), "published": True,
                                           "persona": "test", "checkpoint_identity": "checkpoint",
                                           "evaluation_date": "2026-09-14"}))
        self.proposal_path = self.root / "proposal" / "proposal.json"
        self.proposal_path.parent.mkdir()
        self.candidate = self.proposal_path.parent / "001.yaml"
        self.original = yaml.safe_load((self.tests / "001.yaml").read_text())
        proposed = copy.deepcopy(self.original)
        proposed["grade"]["config"]["assertions"][0]["criterion"] = "revised"
        self.candidate.write_text(yaml.safe_dump(proposed))
        self.proposal = {"tests": [{"test_id": "001", "owner": "grading",
            "original_path": "tests/test/001.yaml", "original_sha256": hashes["001"],
            "proposed_path": "proposal/001.yaml", "proposed_sha256": self.sha(self.candidate),
            "evidence_sha256": {}, "edits_in_order": [{
                "path": "grade.config.assertions[0].criterion", "before": "original", "after": "revised"}]}],
            "published_tests_before_sha256": hashes,
            "counts": {"changed_tests": 1, "request_changes": 0, "grading_only_tests": 1,
                       "unchanged_tests": 199}}
        self.out = self.root / "staged"
        self.save_proposal()
        self.config_patch = patch("authoring.release_revision.load_config", return_value=SimpleNamespace(
            persona="test", evaluation_date="2026-09-14"))
        self.context_patch = patch("authoring.release_revision.load_checkpoint_context",
                                   return_value=SimpleNamespace(checkpoint_identity="checkpoint"))
        self.config_patch.start()
        self.context_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.addCleanup(self.context_patch.stop)

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def save_proposal(self):
        self.proposal_path.write_text(json.dumps(self.proposal))

    def prepare(self, **overrides):
        args = dict(config_path=self.config, proposal_path=self.proposal_path,
                    approved_sha256=self.sha(self.proposal_path), out=self.out, root=self.root)
        args.update(overrides)
        return prepare_release_revision(**args)

    def test_stages_only_exact_changes_and_preserves_release(self):
        before = self.release.read_bytes()
        result = self.prepare()
        self.assertFalse(result["accepted"])
        self.assertEqual(result["paid_calls_made"], 0)
        self.assertEqual(len(list((self.out / "tests").glob("*.yaml"))), 200)
        self.assertEqual(self.release.read_bytes(), before)
        self.assertEqual(self.sha(self.tests / "001.yaml"), self.proposal["tests"][0]["original_sha256"])
        self.assertEqual(self.sha(self.out / "tests" / "001.yaml"), self.sha(self.candidate))
        self.assertEqual(self.sha(self.out / "tests" / "002.yaml"), self.sha(self.tests / "002.yaml"))
        self.assertFalse((self.out / "accepted_batch").exists())
        self.assertEqual(result["changed_tests"]["001"]["certification"],
                         "saved_execution_authentication_pending")

    def test_changed_approval_hash_fails_before_output(self):
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.prepare(approved_sha256="0" * 64)
        self.assertFalse(self.out.exists())

    def test_shared_state_survives_revision_staging(self):
        state = self.tests / "state.json"
        state.write_text(json.dumps({"inbox": [{"id": "preserved"}]}))
        release = json.loads(self.release.read_text())
        published = dict(release["candidate_sha256"])
        for test_id in ("001", "002"):
            path = self.tests / f"{test_id}.yaml"
            spec = yaml.safe_load(path.read_text())
            spec["mock_state_base"] = {"path": "state.json", "sha256": self.sha(state)}
            path.write_text(yaml.safe_dump(spec))
            published[test_id] = self.sha(path)
        release["published_sha256"] = published
        self.release.write_text(json.dumps(release))
        proposed = yaml.safe_load(self.candidate.read_text())
        proposed["mock_state"] = json.loads(state.read_text())
        self.candidate.write_text(yaml.safe_dump(proposed))
        self.proposal["published_tests_before_sha256"] = published
        self.proposal["tests"][0].update(original_sha256=published["001"],
                                          proposed_sha256=self.sha(self.candidate))
        self.save_proposal()
        result = self.prepare()
        self.assertEqual(result["source_bindings"][str(state)], self.sha(state))
        for folder in ("source_tests", "tests"):
            self.assertEqual((self.out / folder / "state.json").read_bytes(), state.read_bytes())
            self.assertEqual(load_test(self.out / folder / "002.yaml"), load_test(self.tests / "002.yaml"))
        self.assertEqual(load_test(self.out / "tests/001.yaml"), proposed)

    def test_changed_unaffected_published_file_fails(self):
        (self.tests / "200.yaml").write_text("changed")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_unlisted_candidate_edit_fails_even_with_new_hash(self):
        candidate = yaml.safe_load(self.candidate.read_text())
        candidate["mock_state"] = {"answer": "leak"}
        self.candidate.write_text(yaml.safe_dump(candidate))
        self.proposal["tests"][0]["proposed_sha256"] = self.sha(self.candidate)
        self.save_proposal()
        with self.assertRaisesRegex(ValueError, "unapproved changes"):
            self.prepare()

    def test_disallowed_field_fails(self):
        row = self.proposal["tests"][0]
        row["edits_in_order"] = [{"path": "mock_state", "before": {}, "after": {"answer": "leak"}}]
        with self.assertRaisesRegex(ValueError, "not permitted"):
            replay_edits(self.original, row)

    def test_grading_edit_cannot_change_request(self):
        row = self.proposal["tests"][0]
        row["edits_in_order"] = [{"path": "test", "before": self.original["test"], "after": "Changed"}]
        with self.assertRaisesRegex(ValueError, "not permitted"):
            replay_edits(self.original, row)

    def test_request_change_requires_new_execution(self):
        row = self.proposal["tests"][0]
        row["owner"] = "request_writing"
        row["edits_in_order"].append({"path": "test", "before": self.original["test"], "after": "Clarified"})
        candidate = yaml.safe_load(self.candidate.read_text())
        candidate["test"] = "Clarified"
        self.candidate.write_text(yaml.safe_dump(candidate))
        row["proposed_sha256"] = self.sha(self.candidate)
        self.proposal["counts"].update(request_changes=1, grading_only_tests=0)
        self.save_proposal()
        result = self.prepare()
        self.assertEqual(result["changed_tests"]["001"]["certification"], "new_execution_after_paid_approval")

    def test_duplicate_ids_fail(self):
        self.proposal["tests"].append(copy.deepcopy(self.proposal["tests"][0]))
        self.save_proposal()
        with self.assertRaisesRegex(ValueError, "distinct"):
            self.prepare()

    def test_staging_cannot_overwrite_existing_output(self):
        self.prepare()
        before = (self.out / "revision.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "fresh output"):
            self.prepare()
        self.assertEqual(before, (self.out / "revision.json").read_bytes())

    def test_changed_checkpoint_fails(self):
        with patch("authoring.release_revision.load_checkpoint_context", return_value=SimpleNamespace(
                checkpoint_identity="different")):
            with self.assertRaisesRegex(ValueError, "frozen release"):
                self.prepare()

    def test_before_value_must_match(self):
        self.proposal["tests"][0]["edits_in_order"][0]["before"] = "not the original"
        self.save_proposal()
        with self.assertRaisesRegex(ValueError, "before-value mismatch"):
            self.prepare()

    def test_changed_evidence_fails(self):
        evidence = self.root / "evidence.json"
        evidence.write_text("original")
        self.proposal["tests"][0]["evidence_sha256"] = {"evidence.json": self.sha(evidence)}
        self.save_proposal()
        evidence.write_text("changed")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.prepare()

    def test_output_cannot_be_created_inside_published_directory(self):
        with self.assertRaisesRegex(ValueError, "overlaps protected"):
            self.prepare(out=self.tests / "revision")
        self.assertFalse((self.tests / "revision").exists())


class ReleaseRevisionCLITest(unittest.TestCase):
    def test_paid_and_other_modes_are_rejected_before_preparation(self):
        for extra in (["--confirm-paid-calls"], ["--plan", "old.json"],
                      ["--test-ids", "1"], ["--approved-revisions", "old.json"]):
            with self.subTest(extra=extra), patch("sys.argv", [
                    "create_tests", "--config", "config.yaml", "--out", "out",
                    "--prepare-release-revision", "proposal.json", "--approved-proposal-sha256", "hash",
                    *extra]):
                with patch("authoring.release_revision.prepare_release_revision") as prepare:
                    with self.assertRaisesRegex(ValueError, "forbids paid/other modes"):
                        main()
                    prepare.assert_not_called()

    def test_approval_hash_cannot_be_used_in_another_mode(self):
        with patch("sys.argv", ["create_tests", "--config", "config.yaml", "--out", "out",
                                "--approved-proposal-sha256", "hash"]):
            with self.assertRaisesRegex(ValueError, "requires --prepare-release-revision"):
                main()


if __name__ == "__main__":
    unittest.main()
