from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from authoring.publish_tests import collect_accepted_tests, publish_tests


def _candidate(test_id: int) -> dict:
    return {
        "id": f"{test_id:03d}",
        "narrative_anchor_date": "2026-09-14",
        "test": f"Please complete task {test_id}.",
        "load_bearing_facts": [test_id],
        "expected_tool_calls": ["send_email"],
        "grade": {
            "type": "tool_trace",
            "config": {
                "assertions": [
                    {
                        "type": "field_equals",
                        "tool": "send_email",
                        "path": "args.to",
                        "value": f"person{test_id}@example.com",
                    }
                ]
            },
        },
        "mock_state": {},
    }


class PublishTestsTest(unittest.TestCase):
    def _batch(self, root: Path) -> Path:
        batch = root / "accepted_batch"
        candidates = batch / "candidates"
        candidates.mkdir(parents=True)
        (batch / "planning_batch.json").write_text("{}\n")
        results = []
        for test_id in (1, 2):
            (candidates / f"{test_id:03d}.yaml").write_text(
                yaml.safe_dump(_candidate(test_id), sort_keys=False)
            )
            results.append(
                {
                    "test_id": test_id,
                    "status": "accepted_initial_final_review",
                    "candidate": str(candidates / f"{test_id:03d}.yaml"),
                    "candidate_sha256": hashlib.sha256(
                        (candidates / f"{test_id:03d}.yaml").read_bytes()
                    ).hexdigest(),
                }
            )
        (batch / "provenance.json").write_text(
            json.dumps(
                {
                    "checkpoint_identity": "checkpoint-1",
                    "accepted_test_ids": [1, 2],
                    "source_manifest": str(root / "manifest.json"),
                    "results": results,
                }
            )
            + "\n"
        )
        return batch

    def _patches(self):
        config = SimpleNamespace(persona="alex", evaluation_date="2026-09-14")
        context = SimpleNamespace(checkpoint_identity="checkpoint-1")
        tasks = [
            SimpleNamespace(fact_ids=[1], expected_tools=["send_email"]),
            SimpleNamespace(fact_ids=[2], expected_tools=["send_email"]),
        ]
        return (
            patch("authoring.publish_tests.load_config", return_value=config),
            patch("authoring.publish_tests.load_checkpoint_context", return_value=context),
            patch(
                "authoring.publish_tests.load_authoring_tasks",
                return_value=tasks,
            ),
        )

    def test_collect_requires_exact_reviewed_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch = self._batch(root)
            config_patch, context_patch, plan_patch = self._patches()
            with config_patch, context_patch, plan_patch:
                _config, candidates, manifest = collect_accepted_tests(
                    config_path=root / "config.yaml",
                    accepted_batch_dirs=[batch],
                    expected_count=2,
                )
            self.assertEqual(sorted(candidates), [1, 2])
            self.assertEqual(manifest["checkpoint_identity"], "checkpoint-1")
            self.assertEqual(manifest["test_count"], 2)

    def test_collect_accepts_resumed_test_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch = self._batch(root)
            provenance_path = batch / "provenance.json"
            provenance = json.loads(provenance_path.read_text())
            provenance["resumed_from"] = {
                "source_run": str(root / "source_run"),
                "source_manifest_sha256": "a" * 64,
            }
            provenance_path.write_text(json.dumps(provenance) + "\n")
            config_patch, context_patch, plan_patch = self._patches()
            with config_patch, context_patch, plan_patch:
                _config, candidates, manifest = collect_accepted_tests(
                    config_path=root / "config.yaml",
                    accepted_batch_dirs=[batch],
                    expected_count=2,
                )
            self.assertEqual(sorted(candidates), [1, 2])
            self.assertEqual(manifest["test_count"], 2)

    def test_collect_accepts_continuation_review_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch = self._batch(root)
            provenance_path = batch / "provenance.json"
            provenance = json.loads(provenance_path.read_text())
            provenance["results"][0]["status"] = "accepted_after_continuation_review"
            provenance_path.write_text(json.dumps(provenance) + "\n")
            config_patch, context_patch, plan_patch = self._patches()
            with config_patch, context_patch, plan_patch:
                _config, candidates, manifest = collect_accepted_tests(
                    config_path=root / "config.yaml",
                    accepted_batch_dirs=[batch],
                    expected_count=2,
                )
            self.assertEqual(sorted(candidates), [1, 2])
            self.assertEqual(manifest["test_count"], 2)

    def test_collect_rejects_non_object_resumed_test_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch = self._batch(root)
            provenance_path = batch / "provenance.json"
            provenance = json.loads(provenance_path.read_text())
            provenance["resumed_from"] = ["not", "an", "object"]
            provenance_path.write_text(json.dumps(provenance) + "\n")
            config_patch, context_patch, plan_patch = self._patches()
            with self.assertRaisesRegex(ValueError, "invalid resumed_from provenance"):
                with config_patch, context_patch, plan_patch:
                    collect_accepted_tests(
                        config_path=root / "config.yaml",
                        accepted_batch_dirs=[batch],
                        expected_count=2,
                    )

    def test_publish_replaces_public_directory_only_after_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch = self._batch(root)
            public = root / "tests" / "alex"
            public.mkdir(parents=True)
            (public / "old.yaml").write_text("old: true\n")
            config_patch, context_patch, plan_patch = self._patches()
            with (
                config_patch,
                context_patch,
                plan_patch,
                patch("authoring.publish_tests.ROOT", root),
            ):
                preview = publish_tests(
                    config_path=root / "config.yaml",
                    accepted_batch_dirs=[batch],
                    expected_count=2,
                    confirm_publish=False,
                )
                self.assertFalse(preview["published"])
                self.assertTrue((public / "old.yaml").is_file())

                result = publish_tests(
                    config_path=root / "config.yaml",
                    accepted_batch_dirs=[batch],
                    expected_count=2,
                    confirm_publish=True,
                )
            self.assertTrue(result["published"])
            self.assertEqual(
                sorted(path.name for path in public.glob("*.yaml")),
                ["001.yaml", "002.yaml"],
            )
            self.assertFalse((public / "old.yaml").exists())
            self.assertTrue((root / "authoring" / "release_manifests" / "alex.json").is_file())

    def test_collect_rejects_missing_candidate_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch = self._batch(root)
            provenance = json.loads((batch / "provenance.json").read_text())
            for result in provenance["results"]:
                result.pop("candidate_sha256")
            (batch / "provenance.json").write_text(json.dumps(provenance) + "\n")
            config_patch, context_patch, plan_patch = self._patches()
            with self.assertRaisesRegex(ValueError, "malformed result"):
                with config_patch, context_patch, plan_patch:
                    collect_accepted_tests(
                        config_path=root / "config.yaml",
                        accepted_batch_dirs=[batch],
                        expected_count=2,
                    )

    def test_collect_rejects_changed_candidate_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch = self._batch(root)
            candidate = batch / "candidates" / "001.yaml"
            candidate.write_text(candidate.read_text() + "\n")
            config_patch, context_patch, plan_patch = self._patches()
            with self.assertRaisesRegex(ValueError, "candidate changed after review"):
                with config_patch, context_patch, plan_patch:
                    collect_accepted_tests(
                        config_path=root / "config.yaml",
                        accepted_batch_dirs=[batch],
                        expected_count=2,
                    )

    def test_collect_accepts_numeric_candidate_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch = self._batch(root)
            candidate = batch / "candidates" / "001.yaml"
            data = yaml.safe_load(candidate.read_text())
            data["id"] = 1
            candidate.write_text(yaml.safe_dump(data, sort_keys=False))
            provenance = json.loads((batch / "provenance.json").read_text())
            provenance["results"][0]["candidate_sha256"] = hashlib.sha256(
                candidate.read_bytes()
            ).hexdigest()
            (batch / "provenance.json").write_text(json.dumps(provenance) + "\n")
            config_patch, context_patch, plan_patch = self._patches()
            with config_patch, context_patch, plan_patch:
                _config, candidates, _manifest = collect_accepted_tests(
                    config_path=root / "config.yaml",
                    accepted_batch_dirs=[batch],
                    expected_count=2,
                )
            self.assertEqual(sorted(candidates), [1, 2])


if __name__ == "__main__":
    unittest.main()
