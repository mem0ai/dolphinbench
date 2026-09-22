import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from authoring.context import dump_json
from authoring.publish_tests import collect_accepted_tests, publish_tests
from authoring.release_mapping import binding, validate_mapped_batch
from authoring.test_publish_tests import _candidate


class ReleaseMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.batch = self.root / "source"
        (self.batch / "candidates").mkdir(parents=True)
        self.config = SimpleNamespace(persona="alex", evaluation_date="2026-09-14")
        self.context = SimpleNamespace(persona="alex", checkpoint_identity="checkpoint-1")
        self.identity = {"persona": "alex", "checkpoint_identity": "checkpoint-1", "evaluation_date": "2026-09-14"}
        self.tasks = [SimpleNamespace(fact_ids=[i], expected_tools=["send_email"], required_results=[]) for i in (238, 241)]
        dump_json(self.batch / "planning_batch.json", {**self.identity, "tasks": [{"fact_ids": [i]} for i in (238, 241)]})
        results = []
        for i in (238, 241):
            path = self.batch / "candidates" / f"{i}.yaml"
            path.write_text(yaml.safe_dump(_candidate(i), sort_keys=False))
            results.append({"test_id": i, "status": "accepted_clean_certification", "candidate": str(path),
                            "candidate_sha256": binding(path)["sha256"]})
        dump_json(self.batch / "provenance.json", {"checkpoint_identity": "checkpoint-1", "accepted_test_ids": [238, 241],
                  "source_manifest": str(self.root / "original_manifest.json"), "results": results})
        self.mapping = {"version": 1, **self.identity, "tests": [
            {"release_id": ordinal, "candidate_id": row["test_id"], "candidate_sha256": row["candidate_sha256"]}
            for ordinal, row in enumerate(results, 1)]}
        self.map_path = self.root / "mapping.json"
        dump_json(self.map_path, self.mapping)
        for target, value in (("load_config", self.config), ("load_checkpoint_context", self.context),
                              ("load_authoring_tasks", self.tasks), ("ROOT", self.root)):
            mock = patch("authoring.publish_tests." + target, **({"new": value} if target == "ROOT" else {"return_value": value}))
            mock.start()
            self.addCleanup(mock.stop)

    def publish(self, **kwargs):
        return publish_tests(config_path=self.root / "config.yaml", accepted_batch_dirs=[self.batch], expected_count=2,
                             release_map_path=self.map_path, **kwargs)

    def test_preview_and_publish_keep_sources_and_planner_release_ids(self):
        before = {p: p.read_bytes() for p in self.batch.rglob("*") if p.is_file()}
        preview = self.publish()
        self.assertEqual(preview["test_ids"], [1, 2])
        self.assertFalse((self.root / "tests").exists())
        with self.assertRaisesRegex(ValueError, "explicitly approved"):
            self.publish(confirm_publish=True)
        result = self.publish(confirm_publish=True, approved_release_map_sha256=binding(self.map_path)["sha256"])
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        mapped = Path(result["batches"][0]["directory"])
        provenance = json.loads((mapped / "provenance.json").read_text())
        plan = json.loads((mapped / "planning_batch.json").read_text())
        validate_mapped_batch(directory=mapped, plan=plan, provenance=provenance)
        for release_id, source_id in ((1, 238), (2, 241)):
            expected = _candidate(source_id)
            expected["id"] = f"{release_id:03d}"
            self.assertEqual(yaml.safe_load((self.root / "tests/alex" / f"{release_id:03d}.yaml").read_text()), expected)
        from authoring.propose import _accepted_test_summaries
        with (patch("authoring.propose.CandidateTaskBatch.model_validate", return_value=SimpleNamespace(**self.identity, tasks=self.tasks)),
              patch("authoring.propose._validate_tasks")):
            summaries = _accepted_test_summaries(config=self.config, context=self.context, accepted_batch_dirs=[mapped], excluded_test_ids=[1])
        self.assertEqual([row["test_id"] for row in summaries], [2])

    def test_duplicate_positions_or_source_hash_changes_are_rejected(self):
        for key, value in (("release_id", 1), ("candidate_id", 238), ("candidate_sha256", "0" * 64)):
            changed = copy.deepcopy(self.mapping)
            changed["tests"][1][key] = value
            dump_json(self.map_path, changed)
            with self.assertRaises(ValueError):
                self.publish()

    def test_mapped_content_change_is_not_inherited_acceptance(self):
        result = self.publish(confirm_publish=True, approved_release_map_sha256=binding(self.map_path)["sha256"])
        mapped = Path(result["batches"][0]["directory"])
        path = mapped / "candidates/001.yaml"
        candidate = yaml.safe_load(path.read_text())
        candidate["test"] = "Changed work."
        path.write_text(yaml.safe_dump(candidate))
        with self.assertRaisesRegex(ValueError, "changed accepted content"):
            validate_mapped_batch(directory=mapped, plan=json.loads((mapped / "planning_batch.json").read_text()),
                                  provenance=json.loads((mapped / "provenance.json").read_text()))

    def test_unmapped_sparse_ids_are_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "exactly 1 through"):
            collect_accepted_tests(config_path=self.root / "config.yaml", accepted_batch_dirs=[self.batch], expected_count=2)
