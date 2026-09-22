"""Certification and Hermes must use the same explicit starting records."""

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness import run_simulation as runner
from harness.mining import preview


class TestStartingState(unittest.TestCase):
    def test_empty_nonempty_and_missing_states_match_certification(self):
        for supplied in [{}, {"invoices": []}, None]:
            with self.subTest(state=supplied), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / "state").mkdir()
                baseline = root / "state/morgan_baseline.json"
                baseline.write_text(json.dumps({"invoices": [{"vendor_contact": "answer@example.test"}]}))
                state, log = root / "live.json", root / "calls.jsonl"
                with patch.object(preview, "_MOCK_DIR", root), patch.object(preview, "_MOCK_STATE", state), patch.object(preview, "_MOCK_LOG", log):
                    preview._reset_mock_state("morgan", supplied)
                expected = json.loads(state.read_text())
                if supplied is not None:
                    self.assertNotIn("answer@example.test", state.read_text())
                state.write_text('{"stale": true}')
                sim = root / "sim.yaml"
                sim.write_text("name: test-state\nsessions: []\ntest_queries:\n  - id: '001'\n")
                profiles = root / "profiles"
                (profiles / "eval").mkdir(parents=True)
                persona = SimpleNamespace(id="morgan", life_sim=sim, baseline_state=baseline,
                                          mock_manifest=root / "manifest.yaml")
                spec = {"test": "Send a message.", "narrative_anchor_date": "2026-09-14",
                        "grade": {"type": "regex", "config": {}}}
                if supplied is not None:
                    spec["mock_state"] = supplied
                observed = []

                def fake_run(*args, **kwargs):
                    observed.append(json.loads(state.read_text()))
                    return SimpleNamespace(ok=True, error=None, status_code=None,
                        retry_after_seconds=None, session_id="local-test", session_path=None,
                        available_tools=["mcp_dolphinbench_apps_send_email", "session_search"],
                        tool_calls=[], response_text="done", token_usage={}, model=None)

                args = runner.build_arg_parser().parse_args([
                    "--persona", "morgan", "--sim", str(sim), "--provider", "builtin",
                    "--out", str(root / "out.json"), "--skip-seeding",
                ])
                with ExitStack() as stack:
                    for name, value in {
                        "HERMES_PROFILES_DIR": profiles, "MOCK_STATE_PATH": state,
                        "MOCK_LOG_PATH": log, "MOCK_TOOL_CONFIG": root / "tools.json",
                    }.items():
                        stack.enter_context(patch.object(runner, name, value))
                    stack.enter_context(patch.object(runner, "resolve_persona_paths", return_value=persona))
                    stack.enter_context(patch.object(runner, "load_test", return_value=spec))
                    stack.enter_context(patch.object(runner, "run_hermes", side_effect=fake_run))
                    stack.enter_context(patch.object(runner, "grade", return_value={"passed": True, "score": 1.0}))
                    stack.enter_context(patch.object(runner, "reset_mock_mcp"))
                    stack.enter_context(patch("harness.hermes_driver._is_dolphinbench_mock_tool_name", return_value=True))
                    stack.enter_context(patch.dict(os.environ, {
                        "DOLPHINBENCH_AGENT_IMAGE": "sha256:" + "a" * 64,
                        "DOLPHINBENCH_HERMES_PROFILE": "eval",
                        "DOLPHINBENCH_HERMES_TOOLSETS": "dolphinbench-apps,memory,session_search",
                        "DOLPHINBENCH_COST_LEDGER_PATH": str(root / "ledger.jsonl"),
                    }))
                    self.assertEqual(runner.run_simulation(args), 0)
                self.assertEqual(observed, [expected])


if __name__ == "__main__":
    unittest.main()
