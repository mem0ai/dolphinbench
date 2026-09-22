from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from construction.checkpoints import save_checkpoint
from registry.facts import (
    FactRegistryError,
    load_fact_registry,
    load_facts,
    load_sessions,
    source_session_ids_for_test,
)


class FactCheckpointAdapterTest(unittest.TestCase):
    def _checkpoint(self, root: Path) -> Path:
        checkpoint = root / "checkpoint"
        save_checkpoint(
            path=checkpoint,
            persona="morgan",
            week={"week_id": "adapter-test", "end_date": "2026-01-01"},
            previous_hash="previous",
            inputs={},
            state={
                "history": [
                    {
                        "id": 101,
                        "narrative_date": "2026-01-01T09:00:00-08:00",
                        "messages": ["Checkpoint-only source session."],
                    }
                ],
                "session_purposes": [{"session_id": 101, "purpose": "Test source."}],
                "entities": [{"id": "person:morgan", "name": "Morgan"}],
                "facts": [
                    {
                        "id": 1,
                        "statement": "The checkpoint fact is authoritative.",
                        "applies_when": "Adapter tests.",
                        "source_session_ids": [101],
                        "subjects": ["person:morgan"],
                        "supersedes": [],
                    }
                ],
                "app_state": {},
                "unfinished_threads": [],
            },
            operation_results=[],
            covered_events=[],
            system_sha256="test-system",
            identity_version=2,
        )
        return checkpoint

    def test_default_loader_remains_the_registry_loader(self) -> None:
        test = {"load_bearing_facts": [1]}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOLPHINBENCH_CHECKPOINT", None)
            self.assertEqual(load_facts("morgan"), load_fact_registry("morgan"))
            self.assertEqual(
                source_session_ids_for_test(test, "morgan"),
                load_fact_registry("morgan")[1]["source_session_ids"],
            )

    def test_authenticated_checkpoint_loads_facts_and_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = self._checkpoint(Path(temp))

            facts = load_facts("morgan", checkpoint=checkpoint)
            sessions = load_sessions("morgan", checkpoint=checkpoint)

            self.assertEqual(facts[1]["source_session_ids"], ["101"])
            self.assertEqual(sessions[0]["id"], 101)
            self.assertEqual(
                source_session_ids_for_test(
                    {"load_bearing_facts": [1]}, "morgan", checkpoint=checkpoint
                ),
                ["101"],
            )

    def test_checkpoint_rejects_persona_mismatch_and_payload_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = self._checkpoint(Path(temp))

            with self.assertRaisesRegex(FactRegistryError, "persona does not match"):
                load_facts("alex", checkpoint=checkpoint)

            history = checkpoint / "life_sim.yaml"
            history.write_text(history.read_text() + "# tampered\n")
            with self.assertRaisesRegex(FactRegistryError, "authentication failed"):
                load_sessions("morgan", checkpoint=checkpoint)


if __name__ == "__main__":
    unittest.main()
