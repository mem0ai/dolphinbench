"""Local checks for reference commands; external execution is mocked."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import contextlib
import io
import unittest

from reference import __main__ as cli, ingest
from reference.memory import proxy


class EntrypointTests(unittest.TestCase):
    def test_ingest_executes_the_history_and_preserves_resume(self):
        with patch("reference.evaluate.launch_matrix", return_value=0) as launch:
            self.assertEqual(ingest.main([
                "--manifest", "run.json", "--resume", "--confirm-paid-calls",
            ]), 0)
        launch.assert_called_once_with(
            Path("run.json"), 1, (), resume=True, seed_only=True,
        )

    def test_paid_commands_require_confirmation_before_execution(self):
        for arguments, target in (
            (["ingest", "--manifest", "run.json"], "reference.evaluate.launch_matrix"),
            (["evaluate", "--manifest", "run.json"], "reference.evaluate.main"),
            (["modal", "launch", "--suite", "suite.json"], "reference.execution.modal.main"),
            (["modal", "resume", "--state", "state.json"], "reference.execution.modal.main"),
        ):
            with self.subTest(command=arguments), patch(target) as execute, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cli.main(arguments)
                execute.assert_not_called()

    def test_evaluate_uses_the_existing_test_only_runner(self):
        with patch("reference.evaluate.main", return_value=0) as evaluate:
            cli.main(["evaluate", "--manifest", "tests.json", "--confirm-paid-calls"])
        evaluate.assert_called_once_with(["test-only", "--manifest", "tests.json"])

    def test_subcommand_help_reaches_the_subcommand(self):
        with patch("reference.evaluate.main", return_value=0) as evaluate:
            cli.main(["prepare-ingestion", "--help"])
        evaluate.assert_called_once_with(["prepare", "--help"])

    def test_each_proxy_starts_its_own_service(self):
        for service, function in (
            ("hindsight", "main"), ("supermemory", "supermemory_main"), ("openai", "openai_main"),
        ):
            with self.subTest(service=service), patch.object(proxy, function) as start:
                proxy.serve([service])
                start.assert_called_once_with()
