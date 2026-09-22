from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from authoring import certify


class CertificationCheckpointTest(unittest.TestCase):
    def test_verify_gate_python_rejects_interpreter_that_cannot_import_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "broken-python"
            executable.write_text("#!/bin/sh\nexit 1\n")
            executable.chmod(0o755)
            with patch.dict(os.environ, {"DOLPHINBENCH_GATE_PYTHON": str(executable)}):
                with self.assertRaisesRegex(RuntimeError, "cannot import mcp"):
                    certify.verify_gate_python()

    def test_candidate_uses_exact_four_shot_order_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = root / "candidate.yaml"
            candidate.write_text("id: candidate-1\ntest: remember this\nmock_state: {items: []}\n")
            checkpoint = root / "checkpoint"
            calls: list[tuple[Path, str, bool, Path, int, Path | None]] = []

            def fake_run_shot(*args, **kwargs):
                calls.append((*args, kwargs.get("checkpoint")))
                return object()

            def fake_parse_shot(_process, with_memory):
                return {"with_memory": with_memory, "passed": with_memory}

            with (
                patch("authoring.certify.run_shot", side_effect=fake_run_shot),
                patch("authoring.certify.parse_shot", side_effect=fake_parse_shot),
            ):
                result = certify.certify_candidate(
                    "morgan",
                    candidate,
                    checkpoint=checkpoint,
                    confirm_paid_calls=True,
                )

        self.assertEqual([call[2] for call in calls], [True, True, False, False])
        self.assertEqual([call[4] for call in calls], [0, 1, 2, 3])
        self.assertTrue(all(call[5] == checkpoint for call in calls))
        self.assertEqual(len({call[0] for call in calls}), 1)
        self.assertEqual(result["verdict"], "valid")

    def test_run_shot_passes_checkpoint_to_isolated_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checkpoint = root / "checkpoint"
            with patch("authoring.certify.subprocess.Popen", return_value=object()) as popen:
                certify.run_shot(
                    root / "candidate.json",
                    "morgan",
                    True,
                    root,
                    0,
                    checkpoint=checkpoint,
                )

        args, kwargs = popen.call_args
        self.assertEqual(args[0][-1], str(checkpoint))
        self.assertEqual(kwargs["env"]["DOLPHINBENCH_CHECKPOINT"], str(checkpoint))
        self.assertEqual(kwargs["env"]["DOLPHINBENCH_STATE_PATH"], str(root / "state_0.json"))

    def test_default_shot_does_not_inherit_a_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with (
                patch.dict(os.environ, {"DOLPHINBENCH_CHECKPOINT": "/stale/checkpoint"}),
                patch("authoring.certify.subprocess.Popen", return_value=object()) as popen,
            ):
                certify.run_shot(root / "candidate.json", "morgan", False, root, 1)

        args, kwargs = popen.call_args
        self.assertEqual(len(args[0]), 5)
        self.assertNotIn("DOLPHINBENCH_CHECKPOINT", kwargs["env"])

    def test_legacy_persona_test_path_is_unchanged(self) -> None:
        with patch("authoring.certify.certify_candidate", return_value={}) as certify_candidate:
            certify.certify_test("morgan", "019", confirm_paid_calls=True)

        certify_candidate.assert_called_once_with(
            "morgan",
            certify.ROOT / "tests" / "morgan" / "019.yaml",
            checkpoint=None,
            test_id="019",
            confirm_paid_calls=True,
        )

    def test_candidate_cli_plumbs_checkpoint_without_running_a_shot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = root / "candidate.json"
            candidate.write_text(json.dumps({"id": "candidate-2"}))
            checkpoint = root / "checkpoint"
            output = root / "result.json"
            expected = {
                "test_id": "candidate-2",
                "verdict": "valid",
                "valid": True,
                "g1_pass_count": 2,
                "g2_pass_count": 0,
                "shots": [],
            }
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "certify.py",
                        "--persona",
                        "morgan",
                        "--candidate",
                        str(candidate),
                        "--checkpoint",
                        str(checkpoint),
                        "--out",
                        str(output),
                        "--confirm-paid-calls",
                    ],
                ),
                patch("authoring.certify.certify_candidate", return_value=expected) as certify_candidate,
            ):
                self.assertEqual(certify.main(), 0)

            certify_candidate.assert_called_once_with(
                "morgan",
                candidate.resolve(),
                checkpoint=checkpoint,
                confirm_paid_calls=True,
            )
            self.assertEqual(json.loads(output.read_text())["results"], [expected])

    def test_certifier_does_not_treat_tool_errors_as_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / "candidate.yaml"
            candidate.write_text("id: 1\ntest: do it\n")

            def fake_parse(_process, with_memory):
                return {
                    "with_memory": with_memory,
                    "passed": False,
                    "error": None,
                    "oracle_errors": ["turn 1 update_item: mock tool rejected input"],
                }

            with (
                patch("authoring.certify.run_shot", return_value=object()),
                patch("authoring.certify.parse_shot", side_effect=fake_parse),
            ):
                result = certify.certify_candidate(
                    "morgan", candidate, confirm_paid_calls=True
                )

        self.assertEqual(result["verdict"], "unsolvable_with_memory")

    def test_certifier_retries_only_failed_infrastructure_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / "candidate.yaml"
            candidate.write_text("id: 1\ntest: do it\n")
            run_indexes: list[int] = []
            parsed = iter(
                [
                    {"with_memory": True, "passed": None, "error": "provider failed"},
                    {"with_memory": True, "passed": True, "error": None},
                    {"with_memory": False, "passed": False, "error": None},
                    {"with_memory": False, "passed": False, "error": None},
                    {"with_memory": True, "passed": True, "error": None},
                ]
            )

            def fake_run(*args, **_kwargs):
                run_indexes.append(args[4])
                return object()

            with (
                patch("authoring.certify.run_shot", side_effect=fake_run),
                patch(
                    "authoring.certify.parse_shot",
                    side_effect=lambda _process, _with_memory: next(parsed),
                ),
            ):
                result = certify.certify_candidate(
                    "morgan", candidate, confirm_paid_calls=True
                )

        self.assertEqual(run_indexes, [0, 1, 2, 3, 4])
        self.assertEqual(result["verdict"], "valid")
        self.assertEqual(result["technical_retries_used"], 1)
        self.assertEqual(result["technical_retry_attempts"][0]["slot"], 1)
        self.assertEqual(
            result["technical_retry_attempts"][0]["shot"]["error"],
            "provider failed",
        )

    def test_certifier_stops_after_two_technical_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / "candidate.yaml"
            candidate.write_text("id: 1\ntest: do it\n")
            parsed = iter(
                [
                    {"with_memory": True, "passed": None, "error": "timeout 1"},
                    {"with_memory": True, "passed": True, "error": None},
                    {"with_memory": False, "passed": False, "error": None},
                    {"with_memory": False, "passed": False, "error": None},
                    {"with_memory": True, "passed": None, "error": "timeout 2"},
                    {"with_memory": True, "passed": None, "error": "timeout 3"},
                ]
            )
            with (
                patch("authoring.certify.run_shot", return_value=object()) as run,
                patch(
                    "authoring.certify.parse_shot",
                    side_effect=lambda _process, _with_memory: next(parsed),
                ),
            ):
                result = certify.certify_candidate(
                    "morgan", candidate, confirm_paid_calls=True
                )

        self.assertEqual(run.call_count, 6)
        self.assertEqual(result["verdict"], "infra_error")
        self.assertEqual(result["technical_retries_used"], 2)

    def test_certifier_requires_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / "candidate.yaml"
            candidate.write_text("id: 1\ntest: do it\n")
            with self.assertRaisesRegex(ValueError, "explicit confirmation"):
                certify.certify_candidate("morgan", candidate)

    def test_bounded_revision_can_forbid_technical_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / "candidate.yaml"
            candidate.write_text("id: 1\ntest: do it\n")
            with (
                patch("authoring.certify.run_shot", return_value=object()) as run,
                patch("authoring.certify.parse_shot", side_effect=lambda _p, memory: {
                    "with_memory": memory, "passed": None, "error": "timeout"}),
            ):
                result = certify.certify_candidate(
                    "morgan", candidate, confirm_paid_calls=True, max_technical_retries=0)
            self.assertEqual(run.call_count, 4)
            self.assertEqual(result["verdict"], "infra_error")
            self.assertEqual(result["technical_retries_used"], 0)

    def test_invalid_retry_limit_stops_before_reading_or_running(self) -> None:
        for limit in (-1, 3, True, 0.5):
            with self.subTest(limit=limit), patch("authoring.certify.run_shot") as run:
                with self.assertRaisesRegex(ValueError, "retry limit"):
                    certify.certify_candidate("morgan", Path("missing.yaml"),
                        confirm_paid_calls=True, max_technical_retries=limit)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
