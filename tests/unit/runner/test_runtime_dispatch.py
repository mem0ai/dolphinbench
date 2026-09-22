from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from harness import run_simulation as runner


class RuntimeDispatchTests(unittest.TestCase):
    def test_unknown_runtime_is_not_reinterpreted_as_hermes(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported agent runtime"):
            runner.run_simulation(SimpleNamespace(agent_runtime="removed-runtime"))

    def setUp(self) -> None:
        environment = patch.dict(runner.os.environ, {})
        environment.start()
        self.addCleanup(environment.stop)

    def test_native_apps_use_the_selected_mcp_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            persona, _, _ = self._fixture(root)
            config, tools = runner._external_mcp_config(
                persona=persona, state_path=root / "state.json",
                log_path=root / "calls.jsonl", tool_config_path=root / "tools.json",
                run_id="fixture", provider="claude_native_memory", identity={},
                memory_gateway_python=sys.executable,
                mock_mcp_python="/opt/dolphinbench-mock-mcp/bin/python",
            )
            self.assertEqual(
                config["mcpServers"]["dolphinbench_apps"]["command"],
                "/opt/dolphinbench-mock-mcp/bin/python",
            )
            self.assertIn("mcp__dolphinbench_apps__send_email", tools)

    def _fixture(self, root: Path) -> tuple[SimpleNamespace, Path, Path]:
        manifest = root / "mock_manifest.yaml"
        manifest.write_text(yaml.safe_dump({"tools": ["send_email", "update_crm_row"]}))
        baseline = root / "baseline.json"
        baseline.write_text(json.dumps({"inbox": [], "crm": []}))
        sim = root / "sim.yaml"
        sim.write_text(yaml.safe_dump({
            "name": "local fixture",
            "sessions": [],
            "test_queries": [{"id": "001"}, {"id": "002"}],
        }))
        return SimpleNamespace(
            id="morgan", life_sim=sim, mock_manifest=manifest, baseline_state=baseline,
        ), sim, baseline

    def _args(self, root: Path, sim: Path, identity: Path, *, runtime: str = "claude") -> SimpleNamespace:
        return SimpleNamespace(
            persona="morgan", sim=str(sim), provider="mem0", out=str(root / "results.json"),
            verbose=False, skip_seeding=True, seed_only=False, resume=False,
            read_only_test_memory=True, isolate_hermes_test_sessions=True, only="",
            capture_memory=False, model="fake-model",
            agent_runtime=runtime, runtime_config_dir=str(root / "runtime"),
            memory_provider_identity_path=str(identity), runtime_run_id="fixture", runtime_timeout=10,
            memory_gateway_python=sys.executable,
        )

    @staticmethod
    def _result() -> SimpleNamespace:
        return SimpleNamespace(
            ok=True, response_text="done", session_id="fresh", tool_calls=[], token_usage={"input_tokens": 3},
            model="fake-model", error=None, raw_trace_path=None, trace_path=None,
        )

    def test_claude_refuses_removed_custom_search_before_running_agent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            persona, sim, _ = self._fixture(root)
            identity = root / "identity.json"
            identity.write_text(json.dumps({"userId": "seeded-user"}))
            args = self._args(root, sim, identity)
            specs = {
                "001": {"test": "send first", "narrative_anchor_date": "2026-01-01", "tools": ["send_email"]},
                "002": {"test": "send second", "narrative_anchor_date": "2026-01-02", "tools": ["update_crm_row"]},
            }
            calls: list[dict] = []

            def fake_claude(message: str, **kwargs):
                calls.append(kwargs)
                return self._result()

            with patch.object(runner, "resolve_persona_paths", return_value=persona), \
                    patch.object(runner, "load_test", side_effect=lambda test_id: specs[test_id]), \
                    patch.object(runner, "grade", return_value={"passed": True, "score": 1.0}), \
                    patch.object(runner, "run_claude", side_effect=fake_claude), \
                    patch.object(runner, "run_hermes", side_effect=AssertionError("must not seed or run Hermes")), \
                    patch.dict(runner.os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "test-token"}):
                with self.assertRaisesRegex(RuntimeError, "does not execute Claude external-memory"):
                    runner.run_simulation(args)
            self.assertEqual(calls, [])
            self.assertFalse((root / "runtime").exists())


    def test_external_memory_gateways_keep_hindsight_and_supermemory_hard_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            hindsight_identity = root / "hindsight.json"
            hindsight_identity.write_text(json.dumps({
                "provider_identity": {"bank_id": "seeded-bank"},
                "gateway_config": {
                    "hindsight_mode": "local_external",
                    "hindsight_base_url": "http://127.0.0.1:8888",
                    "hindsight_budget": "mid",
                },
            }))
            supermemory_identity = root / "supermemory.json"
            supermemory_identity.write_text(
                json.dumps({
                    "provider_identity": {"container_tag": "seeded-container"},
                    "gateway_config": {"base_url": "http://supermemory.local"},
                })
            )
            self.assertEqual(
                runner._load_exact_memory_identity(hindsight_identity, "hindsight"),
                {
                    "bank_id": "seeded-bank",
                    "mode": "local_external",
                    "base_url": "http://127.0.0.1:8888",
                    "budget": "mid",
                },
            )
            self.assertEqual(
                runner._load_exact_memory_identity(supermemory_identity, "supermemory"),
                {
                    "container_tag": "seeded-container",
                    "base_url": "http://supermemory.local",
                },
            )

    def test_disabled_external_runtime_creates_no_test_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            persona, sim, _ = self._fixture(root)
            identity = root / "identity.json"
            identity.write_text(json.dumps({"user_id": "seeded-user"}))
            args = self._args(root, sim, identity)
            spec = {"test": "send", "narrative_anchor_date": "2026-01-01"}
            with patch.object(runner, "resolve_persona_paths", return_value=persona), \
                    patch.object(runner, "load_test", return_value=spec), \
                    patch.object(runner, "grade", return_value={"passed": True}), \
                    patch.object(runner, "run_claude", return_value=self._result()), \
                    patch.object(runner, "run_hermes", side_effect=AssertionError("provider seeding is forbidden")), \
                    patch.dict(runner.os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "test-token"}):
                with self.assertRaisesRegex(RuntimeError, "does not execute Claude external-memory"):
                    runner.run_simulation(args)
            self.assertFalse((root / "runtime").exists())
            self.assertFalse((root / "results.json").exists())

    def test_external_runtime_refuses_seeding_before_creating_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            persona, sim, _ = self._fixture(root)
            identity = root / "identity.json"
            identity.write_text(json.dumps({"user_id": "seeded-user"}))
            args = self._args(root, sim, identity)
            args.skip_seeding = False
            with patch.object(runner, "resolve_persona_paths", return_value=persona):
                with self.assertRaisesRegex(RuntimeError, "test-only"):
                    runner.run_simulation(args)
            self.assertFalse((root / "runtime").exists())


if __name__ == "__main__":
    unittest.main()
