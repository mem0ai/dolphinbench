from __future__ import annotations

import os
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from construction.construction_io import file_hash, resolve_path
from construction.checkpoints import (
    load_checkpoint,
    save_checkpoint,
    set_run_provenance_environment,
    validate_resume_checkpoint_for_config,
    validated_checkpoint_identity,
)


class CheckpointTest(unittest.TestCase):
    def state(self) -> dict:
        return {
            "history": [
                {
                    "id": "session-one",
                    "narrative_date": "2025-01-01T09:00:00-08:00",
                    "messages": ["The accepted state is ready."],
                }
            ],
            "session_purposes": [
                {
                    "session_id": "session-one",
                    "purpose": "Record the accepted state.",
                }
            ],
            "entities": [{"id": "person:morgan", "name": "Morgan"}],
            "facts": [{"id": 1, "statement": "The accepted state is ready."}],
            "app_state": {"docs": [{"id": "doc-one", "title": "Accepted"}]},
            "unfinished_threads": [
                {"thread_id": "thread-one", "what_remains_after_contact": "Follow up."}
            ],
        }

    def save(self, path: Path, *, identity_version: int) -> dict:
        return save_checkpoint(
            path=path,
            persona="morgan",
            week={"week_id": "window-one", "end_date": "2025-01-07"},
            previous_hash="previous",
            inputs={"quarter_plan.json": "plan-hash"},
            state=self.state(),
            operation_results=[
                {
                    "session_id": "session-one",
                    "operation": {"tool": "create_doc", "args": {"title": "Accepted"}},
                    "replay_epoch_ms": 1,
                }
            ],
            covered_events=["event-one"],
            system_sha256="system-hash",
            identity_version=identity_version,
        )

    def test_checkpoint_round_trip_and_payload_tamper_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = Path(temp) / "checkpoint"
            metadata = self.save(checkpoint, identity_version=2)
            identity = validated_checkpoint_identity(checkpoint)
            loaded_metadata, loaded_state = load_checkpoint(checkpoint)

            self.assertEqual(len(identity), 64)
            self.assertEqual(loaded_metadata, metadata)
            self.assertEqual(loaded_state, self.state())

            facts_path = checkpoint / "facts.yaml"
            facts_path.write_text(facts_path.read_text().replace("accepted state", "other state"))
            with self.assertRaisesRegex(ValueError, "facts.yaml"):
                load_checkpoint(checkpoint)

    def test_v1_and_v2_preserve_operation_result_identity_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            v1 = root / "v1"
            v1_metadata = self.save(v1, identity_version=1)
            v1_identity = validated_checkpoint_identity(v1)
            self.assertNotIn("identity_version", v1_metadata)
            self.assertNotIn(
                "operation_results.json", v1_metadata["state_payload_sha256"]
            )
            (v1 / "operation_results.json").write_text("[]\n")
            self.assertEqual(validated_checkpoint_identity(v1), v1_identity)

            v2 = root / "v2"
            v2_metadata = self.save(v2, identity_version=2)
            self.assertEqual(v2_metadata["identity_version"], 2)
            self.assertIn(
                "operation_results.json", v2_metadata["state_payload_sha256"]
            )
            (v2 / "operation_results.json").write_text("[]\n")
            with self.assertRaisesRegex(ValueError, "operation_results.json"):
                validated_checkpoint_identity(v2)

    def test_inherited_root_provenance_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=False):
            output = Path(temp) / "window"
            os.environ["DOLPHINBENCH_CALL_LEDGER"] = "/root/run/calls.jsonl"
            os.environ["DOLPHINBENCH_CONSTRUCTION_RUN_ID"] = "root-quarter-run"

            set_run_provenance_environment(output)

            self.assertEqual(os.environ["DOLPHINBENCH_CALL_LEDGER"], "/root/run/calls.jsonl")
            self.assertEqual(
                os.environ["DOLPHINBENCH_CONSTRUCTION_RUN_ID"], "root-quarter-run"
            )

    def test_direct_run_gets_local_provenance_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=False):
            output = Path(temp) / "window"
            os.environ.pop("DOLPHINBENCH_CALL_LEDGER", None)
            os.environ.pop("DOLPHINBENCH_CONSTRUCTION_RUN_ID", None)

            set_run_provenance_environment(output)

            self.assertEqual(
                os.environ["DOLPHINBENCH_CALL_LEDGER"], str(output / "call_ledger.jsonl")
            )
            self.assertEqual(os.environ["DOLPHINBENCH_CONSTRUCTION_RUN_ID"], "window")

    def test_checkpoint_date_accepts_previous_day_and_dates_inside_quarter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {
                "persona": "morgan",
                "quarter_start": "2025-04-01",
                "quarter_end": "2025-06-30",
                "quarter_plan": str(root / "plan.json"),
                "overview": str(root / "overview.json"),
            }
            for label, accepted_through in (
                ("initial", "2025-03-31"),
                ("inside", "2025-04-14"),
            ):
                checkpoint = root / label
                save_checkpoint(
                    path=checkpoint,
                    persona="morgan",
                    week={"week_id": label, "end_date": accepted_through},
                    previous_hash="previous",
                    inputs={},
                    state=self.state(),
                    operation_results=[],
                    covered_events=[],
                    system_sha256="system",
                    identity_version=2,
                )
                metadata, _ = load_checkpoint(checkpoint)
                validate_resume_checkpoint_for_config(
                    config=config,
                    checkpoint=metadata,
                    checkpoint_dir=checkpoint,
                )

            for label, accepted_through in (
                ("early", "2025-03-30"),
                ("late", "2025-07-01"),
            ):
                checkpoint = root / label
                save_checkpoint(
                    path=checkpoint,
                    persona="morgan",
                    week={"week_id": label, "end_date": accepted_through},
                    previous_hash="previous",
                    inputs={},
                    state=self.state(),
                    operation_results=[],
                    covered_events=[],
                    system_sha256="system",
                    identity_version=2,
                )
                metadata, _ = load_checkpoint(checkpoint)
                with self.assertRaisesRegex(ValueError, "outside the configured quarter"):
                    validate_resume_checkpoint_for_config(
                        config=config,
                        checkpoint=metadata,
                        checkpoint_dir=checkpoint,
                    )

    def test_previous_quarter_checkpoint_does_not_require_new_quarter_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {
                "persona": "morgan",
                "quarter_start": "2025-04-01",
                "quarter_end": "2025-06-30",
                "quarter_plan": str(root / "plan.json"),
                "overview": str(root / "overview.json"),
            }
            checkpoint = root / "previous-quarter"
            save_checkpoint(
                path=checkpoint,
                persona="morgan",
                week={"week_id": "previous", "end_date": "2025-03-31"},
                previous_hash="previous",
                inputs={"quarter_plan.json": "previous-quarter-hash"},
                state=self.state(),
                operation_results=[],
                covered_events=[],
                system_sha256="system",
                identity_version=2,
            )
            metadata, _ = load_checkpoint(checkpoint)

            validate_resume_checkpoint_for_config(
                config=config,
                checkpoint=metadata,
                checkpoint_dir=checkpoint,
            )

    def test_current_quarter_checkpoint_still_requires_matching_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "plan.json").write_text("{}\n")
            (root / "overview.json").write_text("{}\n")
            config = {
                "persona": "morgan",
                "quarter_start": "2025-04-01",
                "quarter_end": "2025-06-30",
                "quarter_plan": str(root / "plan.json"),
                "overview": str(root / "overview.json"),
            }
            checkpoint = root / "current-quarter"
            save_checkpoint(
                path=checkpoint,
                persona="morgan",
                week={"week_id": "current", "end_date": "2025-04-14"},
                previous_hash="previous",
                inputs={"quarter_plan.json": "wrong-hash"},
                state=self.state(),
                operation_results=[],
                covered_events=[],
                system_sha256="system",
                identity_version=2,
            )
            metadata, _ = load_checkpoint(checkpoint)

            with self.assertRaisesRegex(ValueError, "quarter_plan.json hash"):
                validate_resume_checkpoint_for_config(
                    config=config,
                    checkpoint=metadata,
                    checkpoint_dir=checkpoint,
                )


class ConstructionIoTest(unittest.TestCase):
    def test_resolve_path_keeps_absolute_paths(self) -> None:
        path = Path("/tmp/example.json")

        self.assertEqual(resolve_path(path), path)

    def test_resolve_path_anchors_relative_paths_at_repository_root(self) -> None:
        with patch(
            "construction.construction_io.REPOSITORY_ROOT", Path("/repo")
        ):
            self.assertEqual(resolve_path("data/example.json"), Path("/repo/data/example.json"))

    def test_file_hash_returns_sha256_of_file_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "example.txt"
            payload = b"quarter plan\n"
            path.write_bytes(payload)

            self.assertEqual(file_hash(path), hashlib.sha256(payload).hexdigest())


if __name__ == "__main__":
    unittest.main()
