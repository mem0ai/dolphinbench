"""Configuration compatibility without model calls or persistent state."""

import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.environment import call_ledger, get_setting, load_environment, openai_config_path


class EnvironmentTests(unittest.TestCase):
    def test_environment_file_fills_only_missing_values_without_interpretation(self):
        parent = {"EXISTING": "parent", "EMPTY": ""}
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, parent, clear=True):
            path = Path(raw) / ".env"
            path.write_text(
                "# comment\n\ninvalid line\nEXISTING=file\nEMPTY=file\n"
                " ADDED = first=part \nADDED=second\nQUOTED='literal'\n"
            )
            self.assertEqual(load_environment(path), {
                **parent, "ADDED": "first=part", "QUOTED": "'literal'",
            })
            self.assertEqual(dict(os.environ), parent)

    def test_missing_environment_file_returns_an_independent_copy(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {"VALUE": "parent"}, clear=True):
            values = load_environment(Path(raw) / "missing")
            self.assertEqual(values, {"VALUE": "parent"})
            values["VALUE"] = "changed"
            self.assertEqual(os.environ["VALUE"], "parent")

    def test_canonical_value_wins_including_empty_values(self):
        for value in ("current", ""):
            with self.subTest(value=value):
                self.assertEqual(get_setting("DOLPHINBENCH_PERSONA", environ={
                    "DOLPHINBENCH_PERSONA": value, "ENACT_PERSONA": "legacy",
                }), value)

    def test_legacy_defaults_and_explicit_mapping(self):
        with patch.dict(os.environ, {"DOLPHINBENCH_PERSONA": "parent"}, clear=True):
            self.assertEqual(get_setting("DOLPHINBENCH_PERSONA", environ={
                "ENACT_PERSONA": "profile",
            }), "profile")
            self.assertEqual(get_setting("DOLPHINBENCH_PERSONA", 42, environ={}), 42)
            self.assertIsNone(get_setting("DOLPHINBENCH_PERSONA", environ={}))
            self.assertEqual(get_setting("DOLPHINBENCH_PERSONA"), "parent")

    def test_config_path_prefers_new_location_and_accepts_old_location(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {}, clear=True):
            home = Path(raw)
            with patch("harness.environment.Path.home", return_value=home):
                new = home / ".dolphinbench/config.yaml"
                old = home / ".enact/config.yaml"
                self.assertEqual(openai_config_path(), new)
                old.parent.mkdir()
                old.touch()
                self.assertEqual(openai_config_path(), old)
                new.parent.mkdir()
                new.touch()
                self.assertEqual(openai_config_path(), new)

    def test_explicit_config_path_accepts_both_prefixes(self):
        with patch.dict(os.environ, {"ENACT_OPENAI_CONFIG": "/legacy.yaml"}, clear=True):
            self.assertEqual(openai_config_path(), Path("/legacy.yaml"))
            os.environ["DOLPHINBENCH_OPENAI_CONFIG"] = "/current.yaml"
            self.assertEqual(openai_config_path(), Path("/current.yaml"))

    def test_nested_call_ledger_restores_legacy_parent(self):
        original = {"ENACT_CALL_LEDGER": "/parent.jsonl", "ENACT_CONSTRUCTION_RUN_ID": "parent"}
        with patch.dict(os.environ, original, clear=True):
            with call_ledger(Path("/outer/calls.jsonl")):
                self.assertEqual(get_setting("DOLPHINBENCH_CALL_LEDGER"), "/outer/calls.jsonl")
                with self.assertRaisesRegex(ValueError, "fixture"):
                    with call_ledger(Path("/inner/calls.jsonl")):
                        self.assertEqual(get_setting("DOLPHINBENCH_CONSTRUCTION_RUN_ID"), "inner")
                        raise ValueError("fixture")
                self.assertEqual(get_setting("DOLPHINBENCH_CALL_LEDGER"), "/outer/calls.jsonl")
            self.assertEqual(dict(os.environ), original)
            self.assertEqual(get_setting("DOLPHINBENCH_CALL_LEDGER"), "/parent.jsonl")

    def test_checkpoint_defaults_preserve_legacy_orchestrator_settings(self):
        from construction.checkpoints import set_run_provenance_environment

        with patch.dict(os.environ, {
            "ENACT_CALL_LEDGER": "/parent.jsonl", "ENACT_CONSTRUCTION_RUN_ID": "parent",
        }, clear=True):
            set_run_provenance_environment(Path("/child"))
            self.assertEqual(get_setting("DOLPHINBENCH_CALL_LEDGER"), "/parent.jsonl")
            self.assertEqual(get_setting("DOLPHINBENCH_CONSTRUCTION_RUN_ID"), "parent")

    def test_mock_server_loads_isolated_paths_with_either_prefix(self):
        server_path = Path(__file__).resolve().parents[1] / "mock_mcp/server.py"
        for prefix in ("DOLPHINBENCH_", "ENACT_"):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as raw:
                state = Path(raw) / "state.json"
                log = Path(raw) / "calls.jsonl"
                env = {
                    prefix + "PERSONA": "morgan",
                    prefix + "STATE_PATH": str(state),
                    prefix + "LOG_PATH": str(log),
                    prefix + "TOOL_CONFIG_PATH": str(Path(raw) / "tools.json"),
                    prefix + "RUN_ID": "fixture-run",
                }
                with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", [str(server_path)]):
                    server = runpy.run_path(str(server_path))
                self.assertEqual(server["STATE_PATH"], state)
                self.assertEqual(server["LOG_PATH"], log)
                self.assertEqual(server["RUN_ID"], "fixture-run")
                self.assertTrue(server["_MANIFEST_TOOLS"])
                self.assertFalse(state.exists())
                self.assertFalse(log.exists())
