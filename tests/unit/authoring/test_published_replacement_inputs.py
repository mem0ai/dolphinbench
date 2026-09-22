from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from authoring import propose as propose_module
from authoring.context import load_checkpoint_context, load_config
from authoring.propose import (
    _merge_test_summaries,
    _published_test_summaries,
    _state_test_summaries,
    load_persona_state,
    proposal_request,
    reset_persona_state_after_publish,
)


ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "authoring" / "configs" / "morgan.yaml"


class PublishedReplacementInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(CONFIG_PATH)
        cls.context = load_checkpoint_context(cls.config)

    def _fixture(self) -> tuple[Path, dict[str, str]]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        manifest_dir = root / "authoring" / "release_manifests"
        tests_dir = root / "tests" / "morgan"
        manifest_dir.mkdir(parents=True)
        tests_dir.mkdir(parents=True)
        hashes: dict[str, str] = {}
        for test_id in (1, 2):
            path = tests_dir / f"{test_id:03d}.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "id": f"{test_id:03d}",
                        "narrative_anchor_date": self.config.evaluation_date,
                        "test": f"Do work {test_id}.",
                        "load_bearing_facts": [test_id],
                        "expected_tool_calls": ["send_email"],
                        "grade": {
                            "type": "tool_trace",
                            "config": {
                                "assertions": [
                                    {
                                        "type": "tool_called",
                                        "tool": "send_email",
                                        "criterion": f"Do work {test_id}.",
                                    }
                                ]
                            },
                        },
                        "mock_state": {},
                    },
                    sort_keys=False,
                )
            )
            hashes[f"{test_id:03d}"] = hashlib.sha256(path.read_bytes()).hexdigest()
        (manifest_dir / "morgan.json").write_text(
            json.dumps(
                {
                    "persona": "morgan",
                    "checkpoint_identity": self.context.checkpoint_identity,
                    "evaluation_date": self.config.evaluation_date,
                    "test_count": 2,
                    "test_ids": [1, 2],
                    "batches": [],
                    "candidate_sha256": hashes,
                    "destination": str(tests_dir),
                    "published": True,
                }
            )
        )
        return root, hashes

    def test_loads_published_tests_and_excludes_named_replacements(self) -> None:
        root, _ = self._fixture()
        with patch.object(propose_module, "REPO_ROOT", root):
            summaries, release = _published_test_summaries(
                config=self.config,
                context=self.context,
                replace_test_ids=[1],
            )
        self.assertEqual([row["test_id"] for row in summaries], [2])
        self.assertEqual(release["excluded_test_ids"], [1])
        self.assertEqual(release["test_count"], 2)

    def test_rejects_a_changed_published_file(self) -> None:
        root, _ = self._fixture()
        (root / "tests" / "morgan" / "001.yaml").write_text("changed\n")
        with patch.object(propose_module, "REPO_ROOT", root), self.assertRaisesRegex(
            ValueError, "changed after release"
        ):
            _published_test_summaries(
                config=self.config,
                context=self.context,
                replace_test_ids=[1],
            )

    def test_shared_state_loads_for_planning_and_review(self) -> None:
        from authoring.create_tests import _normalized_test
        root, hashes = self._fixture()
        folder = root / "tests/morgan"
        state = folder / "state.json"
        state.write_text(json.dumps({"inbox": [{"id": "shared"}]}))
        path = folder / "001.yaml"
        compact = yaml.safe_load(path.read_text())
        compact["mock_state_base"] = {"path": "state.json",
            "sha256": hashlib.sha256(state.read_bytes()).hexdigest()}
        path.write_text(yaml.safe_dump(compact))
        hashes["001"] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = root / "authoring/release_manifests/morgan.json"
        release = json.loads(manifest.read_text())
        release["published_sha256"] = hashes
        manifest.write_text(json.dumps(release))
        with patch.object(propose_module, "REPO_ROOT", root):
            summaries, _ = _published_test_summaries(
                config=self.config, context=self.context, replace_test_ids=[])
        self.assertEqual(len(summaries), 2)
        self.assertEqual(json.loads(_normalized_test(path))["mock_state"], json.loads(state.read_text()))
        state.write_text("{}")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            _normalized_test(path)

    def test_relocated_release_uses_local_hash_bound_tests_not_historical_batches(self) -> None:
        root, _ = self._fixture()
        manifest = root / "authoring/release_manifests/morgan.json"
        release = json.loads(manifest.read_text())
        release["destination"] = "/old-checkout/tests/morgan"
        release["batches"] = [{"directory": "/unavailable-historical-batch"}]
        manifest.write_text(json.dumps(release))
        before = manifest.read_bytes()
        state = root / "state.json"
        state.write_text(json.dumps({
            "persona": self.context.persona,
            "checkpoint_identity": self.context.checkpoint_identity,
            "evaluation_date": self.config.evaluation_date,
            "release_manifest": {
                "path": "authoring/release_manifests/morgan.json",
                "sha256": hashlib.sha256(before).hexdigest(),
            },
            "batches": [],
        }))
        with patch.object(propose_module, "REPO_ROOT", root):
            loaded = load_persona_state(
                config=self.config, context=self.context, persona_state_path=state,
            )
            self.assertEqual(loaded["accepted_batch_dirs"], [])
            self.assertEqual([row["test_id"] for row in loaded["published_tests"]], [1, 2])
            self.assertEqual(manifest.read_bytes(), before)
            manifest.write_bytes(before + b"\n")
            with self.assertRaisesRegex(ValueError, "release manifest is missing or changed"):
                load_persona_state(
                    config=self.config, context=self.context, persona_state_path=state,
                )

    def test_publication_writes_a_relative_hash_bound_release_reference(self) -> None:
        root, _ = self._fixture()
        manifest = root / "authoring/release_manifests/morgan.json"
        state = root / "authoring/persona_states/morgan.json"
        config = SimpleNamespace(
            persona=self.config.persona, evaluation_date=self.config.evaluation_date,
            persona_state=str(state),
        )
        before = manifest.read_bytes()
        with patch.object(propose_module, "REPO_ROOT", root):
            reset_persona_state_after_publish(config=config, release_manifest_path=manifest)
            reference = json.loads(state.read_text())["release_manifest"]
            self.assertEqual(reference["path"], "../release_manifests/morgan.json")
            self.assertEqual(reference["sha256"], hashlib.sha256(before).hexdigest())
            loaded = load_persona_state(config=config, context=self.context)
        self.assertEqual([row["test_id"] for row in loaded["published_tests"]], [1, 2])
        self.assertEqual(manifest.read_bytes(), before)

    def test_published_release_does_not_waive_missing_working_batch(self) -> None:
        root, _ = self._fixture()
        manifest = root / "authoring/release_manifests/morgan.json"
        state = root / "state.json"
        state.write_text(json.dumps({
            "persona": self.context.persona,
            "checkpoint_identity": self.context.checkpoint_identity,
            "evaluation_date": self.config.evaluation_date,
            "release_manifest": {
                "path": str(manifest),
                "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            },
            "batches": [{"directory": "missing-working-batch"}],
        }))
        with patch.object(propose_module, "REPO_ROOT", root):
            with self.assertRaisesRegex(ValueError, "batch is incomplete"):
                load_persona_state(
                    config=self.config, context=self.context, persona_state_path=state,
                )

    def test_working_tests_merge_with_published_tests_without_reusing_ids(self) -> None:
        state = {"accepted_batch_dirs": [], "published_tests": [{"test_id": 1}]}
        with patch.object(propose_module, "_accepted_test_summaries", return_value=[{"test_id": 2}]):
            merged = _state_test_summaries(config=self.config, context=self.context, state=state)
        self.assertEqual([row["test_id"] for row in merged], [1, 2])
        with patch.object(propose_module, "_accepted_test_summaries", return_value=[{"test_id": 1}]):
            with self.assertRaisesRegex(ValueError, "duplicate accepted test ID 1"):
                _state_test_summaries(config=self.config, context=self.context, state=state)

    def test_rejects_noncanonical_destination_even_with_matching_hashes(self) -> None:
        root, _ = self._fixture()
        manifest = root / "authoring/release_manifests/morgan.json"
        release = json.loads(manifest.read_text())
        release["destination"] = str(root / "tests/alex")
        manifest.write_text(json.dumps(release))
        with patch.object(propose_module, "REPO_ROOT", root):
            with self.assertRaisesRegex(ValueError, "non-canonical tests directory"):
                _published_test_summaries(
                    config=self.config, context=self.context, replace_test_ids=[],
                )

    def test_rejects_a_missing_published_file(self) -> None:
        root, _ = self._fixture()
        (root / "tests" / "morgan" / "002.yaml").unlink()
        with patch.object(propose_module, "REPO_ROOT", root), self.assertRaisesRegex(
            ValueError, "missing"
        ):
            _published_test_summaries(
                config=self.config,
                context=self.context,
                replace_test_ids=[1],
            )

    def test_rejects_duplicate_ids_when_merging_replacements(self) -> None:
        published = [{"test_id": 2}]
        accepted = [{"test_id": 2}]
        with self.assertRaisesRegex(ValueError, "duplicate accepted test ID 2"):
            _merge_test_summaries(published, accepted, replace_test_ids=[2])

    def test_rejects_duplicate_replacement_ids(self) -> None:
        root, _ = self._fixture()
        with patch.object(propose_module, "REPO_ROOT", root), self.assertRaisesRegex(
            ValueError, "unique positive"
        ):
            _published_test_summaries(
                config=self.config,
                context=self.context,
                replace_test_ids=[1, 1],
            )

    def test_release_identity_stays_out_of_the_planner_request(self) -> None:
        root, _ = self._fixture()
        with patch.object(propose_module, "REPO_ROOT", root):
            request = proposal_request(
                config=self.config,
                context=self.context,
                persona_state_path=root / "authoring" / "release_manifests" / "morgan.json",
                replace_test_ids=[1],
            )
        self.assertNotIn("published_release", request)
        self.assertEqual(
            [row["test_id"] for row in request["prior_work"]["accepted"]], [2]
        )


if __name__ == "__main__":
    unittest.main()
