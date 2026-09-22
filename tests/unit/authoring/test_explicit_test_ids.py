from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from authoring import run as run_module
from authoring import create_tests as create_tests_module


class ExplicitTestIdTests(unittest.TestCase):
    def test_authoring_uses_the_id_assigned_to_each_plan_position(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            plan_path = root / "plan.json"
            config_path.write_text("config\n")
            plan_path.write_text("plan\n")
            tasks = [SimpleNamespace(fact_ids=[index]) for index in (1, 2, 3)]
            config = SimpleNamespace(persona="morgan", evaluation_date="2026-09-14")
            context = SimpleNamespace(
                checkpoint_identity="checkpoint-id",
                checkpoint_path=root / "checkpoint",
            )

            def write_candidate(path: Path, _candidate: object) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("id: test\n")

            with (
                patch.object(run_module, "load_config", return_value=config),
                patch.object(run_module, "load_checkpoint_context", return_value=context),
                patch.object(
                    run_module,
                    "load_authoring_tasks",
                    return_value=tasks,
                ),
                patch.object(run_module, "design_payload", return_value={}),
                patch.object(
                    run_module,
                    "_author_new_test_attempt",
                    return_value=({"candidate": True}, {"stage": "accepted"}),
                ) as attempt,
                patch.object(run_module, "write_candidate", side_effect=write_candidate),
            ):
                manifest = run_module.run_authoring(
                    config_path=config_path,
                    plan_path=plan_path,
                    out=root / "run",
                    limit=None,
                    only=[1, 3],
                    start_id=None,
                    test_ids=[121, 127, 130],
                    design_client=SimpleNamespace(),
                    query_client=SimpleNamespace(),
                    confirm_paid_calls=True,
                )

            self.assertEqual([call.kwargs["test_id"] for call in attempt.call_args_list], [121, 130])
            self.assertEqual([result["id"] for result in manifest["results"]], [121, 130])
            self.assertEqual(manifest["selected_test_ids"], [121, 130])

    def test_create_tests_passes_the_complete_id_list_to_every_parallel_part(self) -> None:
        test_ids = [121, 122, 123, 124, 125, 127, 130]
        commands: list[list[str]] = []

        class Process:
            def __init__(self, command: list[str]) -> None:
                commands.append(command)

            def wait(self) -> int:
                return 0

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(create_tests_module.subprocess, "Popen", side_effect=Process),
        ):
            create_tests_module._run_authoring_parts(
                config_path=Path("config.yaml"),
                plan_path=Path("plan.json"),
                out=Path(directory),
                start_id=None,
                test_ids=test_ids,
                count=7,
                concurrency=4,
            )

        self.assertEqual(len(commands), 4)
        for command in commands:
            index = command.index("--test-ids")
            self.assertEqual(command[index + 1], "121,122,123,124,125,127,130")

    def test_failed_certification_stops_for_trace_review_before_redesign(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            plan_path = root / "plan.json"
            config_path.write_text("config\n")
            plan_path.write_text("plan\n")
            task = SimpleNamespace(fact_ids=[745])
            config = SimpleNamespace(persona="morgan", evaluation_date="2026-09-14")
            context = SimpleNamespace(
                checkpoint_identity="checkpoint-id",
                checkpoint_path=root / "checkpoint",
            )

            def failed_attempt(**kwargs: object) -> tuple[None, dict[str, object]]:
                proposal_dir = Path(kwargs["directory"])
                proposal_dir.mkdir(parents=True, exist_ok=True)
                (proposal_dir / "initial_candidate.yaml").write_text("id: 16\n")
                (proposal_dir / "initial_gate.json").write_text(
                    json.dumps({"result": {"valid": False}})
                )
                return None, {
                    "stage": "certification",
                    "reason": "unsolvable_with_memory",
                }

            with (
                patch.object(run_module, "load_config", return_value=config),
                patch.object(run_module, "load_checkpoint_context", return_value=context),
                patch.object(run_module, "load_authoring_tasks", return_value=[task]),
                patch.object(run_module, "design_payload", return_value={}),
                patch.object(run_module, "_author_new_test_attempt", side_effect=failed_attempt) as attempt,
            ):
                manifest = run_module.run_authoring(
                    config_path=config_path,
                    plan_path=plan_path,
                    out=root / "run",
                    limit=None,
                    only=None,
                    start_id=None,
                    test_ids=[16],
                    design_client=SimpleNamespace(),
                    query_client=SimpleNamespace(),
                    confirm_paid_calls=True,
                )

            self.assertEqual(attempt.call_count, 1)
            self.assertEqual(manifest["results"][0]["status"], "review_required")
            self.assertIn("first_failure", manifest["results"][0])

    def test_failed_certification_is_returned_to_the_public_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "part_01"
            proposal_dir = run_dir / "proposals" / "016"
            proposal_dir.mkdir(parents=True)
            candidate = proposal_dir / "initial_candidate.yaml"
            gate = proposal_dir / "initial_gate.json"
            candidate.write_text("id: 16\n")
            gate.write_text("{}\n")
            (run_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "id": 16,
                                "status": "review_required",
                                "candidate": str(candidate),
                                "gate": str(gate),
                            }
                        ]
                    }
                )
            )

            reviewable, results = create_tests_module._reviewable_outputs(
                run_dirs=[run_dir], expected_test_ids=[16]
            )

            self.assertEqual(results[0]["status"], "review_required")
            self.assertFalse(reviewable[16]["certification_valid"])
            self.assertEqual(reviewable[16]["candidate"], candidate.resolve())
            self.assertEqual(reviewable[16]["gate"], gate.resolve())


if __name__ == "__main__":
    unittest.main()
