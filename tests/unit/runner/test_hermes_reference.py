"""Offline retry, source-pin, and generated-profile regression checks."""

import copy
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from harness.adapter import Interaction
from harness.adapters.hermes import HermesAdapter, profile_cost
from harness.adapters.hermes_reference import SPEC_PATH, prepare_reference, verify_dependencies, verify_source


class RetryTests(unittest.TestCase):
    def test_profile_cost_counts_auxiliary_usage_without_double_counting_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(profile_cost(root, empty_ok=True), 0)
            with self.assertRaisesRegex(ValueError, "Missing Hermes cost"):
                profile_cost(root)
            with sqlite3.connect(root / "state.db") as connection:
                connection.execute("CREATE TABLE sessions (estimated_cost_usd REAL)")
                connection.execute("INSERT INTO sessions VALUES (10)")
            self.assertEqual(profile_cost(root), 10)
            with sqlite3.connect(root / "state.db") as connection:
                connection.execute("CREATE TABLE session_model_usage (task TEXT, estimated_cost_usd REAL, cost_status TEXT)")
                connection.executemany("INSERT INTO session_model_usage VALUES (?, ?, ?)",
                                       [("main", 10, "estimated"), ("auxiliary", 2, "estimated")])
            self.assertEqual(profile_cost(root), 12)
            with sqlite3.connect(root / "state.db") as connection:
                connection.execute("UPDATE session_model_usage SET estimated_cost_usd = NULL WHERE task = 'auxiliary'")
            with self.assertRaises(ValueError):
                profile_cost(root)

    def test_seed_retries_only_known_undelivered_transient_failures(self):
        for error, expected in (("HTTP 429", 4), ("connection refused", 4),
                                ("timeout", 1), ("HTTP 503", 1), ("HTTP 400", 1)):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temporary:
                adapter = object.__new__(HermesAdapter)
                adapter.home = Path(temporary)
                result = SimpleNamespace(ok=False, error=error, session_id=None)
                request = Interaction("morgan", "ingestion", "000001", "message", "2023-01-01",
                                      {"env": {}}, adapter.home)
                with patch.object(adapter, "_attempt", return_value=(result, adapter.home / "missing", 125)) as attempt, \
                        patch("harness.adapters.hermes.time.sleep") as sleep, patch.dict(os.environ, {
                            "DOLPHINBENCH_SEED_RETRIES": "3", "DOLPHINBENCH_RETRY_BASE_SECONDS": "5",
                            "DOLPHINBENCH_RETRY_MAX_SECONDS": "60"}):
                    with self.assertRaises(RuntimeError):
                        adapter.run_interaction(request)
                    self.assertEqual(attempt.call_count, expected)
                    self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 10, 20][:expected - 1])
                    journal = adapter.home / "attempts/ingestion/morgan/000001.json"
                    self.assertEqual(len(json.loads(journal.read_text())), expected)
                    with self.assertRaisesRegex(ValueError, "refusing to replay"):
                        adapter.run_interaction(request)

    def test_test_retry_resets_apps_preserves_attempts_and_last_duration(self):
        with tempfile.TemporaryDirectory() as temporary:
            adapter = object.__new__(HermesAdapter)
            adapter.home = Path(temporary)
            state = adapter.home / "state.json"
            log = adapter.home / "calls.jsonl"
            state.write_text('{"initial": true}')
            log.write_text("")
            request = Interaction("morgan", "tests", "001", "message", "2026-09-14",
                {"env": {"DOLPHINBENCH_STATE_PATH": str(state), "DOLPHINBENCH_LOG_PATH": str(log)}}, adapter.home)

            def attempt(request, number):
                self.assertEqual(json.loads(state.read_text()), {"initial": True})
                self.assertEqual(log.read_text(), "")
                state.write_text('{"mutated": true}')
                recording = adapter.home / f"profile-{number}/submission-traces/session.jsonl"
                recording.parent.mkdir(parents=True)
                recording.write_text("fixture")
                result = SimpleNamespace(ok=number == 2, error="HTTP 503" if number == 1 else None,
                                         session_id=f"s{number}", tool_calls=[])
                if number == 1:
                    log.write_text("old failed call")
                cost = adapter.home / "costs/tests/morgan" / f"001-{number}.json"
                cost.parent.mkdir(parents=True, exist_ok=True)
                cost.write_text(json.dumps({"total_cost_usd": number * .25}))
                return result, recording, number * 100

            with patch.object(adapter, "_attempt", side_effect=attempt), \
                    patch("harness.adapters.hermes.time.sleep"), \
                    patch("harness.adapters.hermes.read_recording", return_value={"settings": {"model": "fixture"}, "messages": []}):
                record = adapter.run_interaction(request)
            self.assertEqual(record.duration_ms, 200)
            self.assertEqual(len(record.attempts), 2)
            self.assertTrue(Path(record.attempts[0]["recording"]).exists())
            self.assertTrue(Path(record.attempts[1]["recording"]).exists())
            self.assertFalse((adapter.home / "profile-2").exists())
            self.assertEqual(record.app_calls, [])
            self.assertEqual(adapter.total_cost_usd("tests"), .75)


class ReferenceTests(unittest.TestCase):
    def test_generated_profile_matches_recorded_recipe_without_creating_profiles(self):
        options = {"reference": "hermes-builtin-luna", "source_env": "TEST_SOURCE", "python_env": "TEST_PYTHON",
                   "app_python_env": "TEST_APPS", "base_url_env": "TEST_BASE"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            python = root / "python"
            python.touch()
            (root / "hermes").write_text(f"#!{python}\n")
            home = root / "run"
            with patch.dict(os.environ, {"TEST_SOURCE": temporary, "TEST_PYTHON": str(python),
                    "TEST_APPS": str(python), "TEST_BASE": "https://example.invalid/openai/v1"}), \
                    patch("harness.adapters.hermes_reference.verify_source", return_value={}), \
                    patch("harness.adapters.hermes_reference.interpreter_identity", return_value={}), \
                    patch("harness.adapters.hermes_reference.verify_dependencies"):
                reference = prepare_reference(options, home)
            self.assertFalse(home.exists())
            self.assertEqual(set(reference["templates"]), {"morgan", "alex", "riley"})
            config = yaml.safe_load(reference["templates"]["morgan"]["config.yaml"])
            self.assertEqual(config["model"]["default"], "gpt-5.6-luna")
            self.assertEqual(config["model"]["context_length"], 1050000)
            self.assertEqual(config["agent"]["max_turns"], 60)
            self.assertEqual(config["agent"]["api_max_retries"], 8)
            self.assertTrue(config["agent"]["retry_rate_limits_until_success"])
            self.assertEqual(config["memory"]["flush_min_turns"], 6)
            self.assertIn("Morgan Chen", reference["templates"]["morgan"]["SOUL.md"])

    def test_source_tampering_is_rejected_before_git_or_runtime_start(self):
        spec = json.loads(SPEC_PATH.read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "uv.lock").write_text("tampered")
            with patch("harness.adapters.hermes_reference.subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, "uv.lock differs"):
                    verify_source(root, spec)
                run.assert_not_called()

    def test_dependency_versions_and_python_are_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text('[project]\ndependencies = ["sample>=1"]\n')
            (root / "uv.lock").write_text('[[package]]\nname = "sample"\nversion = "1.0"\n')
            spec = {"python_minor": [3, 11], "extras": [], "app_packages": {"mcp": "1.29.0"}}
            runtime = {"python": [3, 11, 15], "packages": {"sample": "1.0"}}
            apps = {"python": [3, 11, 15], "packages": {"mcp": "1.29.0"}}
            verify_dependencies(root, spec, runtime, apps)
            changed = copy.deepcopy(runtime)
            changed["packages"]["sample"] = "2.0"
            with self.assertRaisesRegex(ValueError, "not in the supplied lock"):
                verify_dependencies(root, spec, changed, apps)
            apps["packages"]["mcp"] = "1.30.0"
            with self.assertRaisesRegex(ValueError, "App environment requires"):
                verify_dependencies(root, spec, runtime, apps)
            runtime["python"] = [3, 12, 3]
            with self.assertRaisesRegex(ValueError, "Python 3.11"):
                verify_dependencies(root, spec, runtime, apps)


if __name__ == "__main__":
    unittest.main()
