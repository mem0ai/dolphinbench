"""Local checks for reference evaluation; external execution is mocked."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import json
import subprocess
import tempfile
import unittest

import yaml

from reference import evaluate as matrix
from reference import plan
from reference.execution import worker
from reference.runtimes import claude_code as native
from reference.runtimes.hermes import profile_config_hashes


class EvalMatrixPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.hermes = Path(self.temp.name) / "hermes"
        self.honcho = Path(self.temp.name) / "honcho"
        self._make_fixture()
        self.environment = {
            "DOLPHINBENCH_AGENT_IMAGE": "sha256:" + "0" * 64,
            "EVAL_KEY": "not-recorded-agent-secret",
            "AZURE_OPENAI_ENDPOINT": "https://judge.example.invalid",
            "AZURE_OPENAI_API_KEY": "not-recorded-judge-secret",
            "DOLPHINBENCH_HONCHO_SOURCE": str(self.honcho),
            "HINDSIGHT_API_KEY": "not-recorded-hindsight-secret",
            "SUPERMEMORY_API_KEY": "not-recorded-supermemory-secret",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def _make_fixture(self) -> None:
        checkpoint = self.root / "construction" / "v2" / "morgan" / "release" / "500k_final" / "checkpoint"
        checkpoint.mkdir(parents=True)
        (checkpoint / "life_sim.yaml").write_text(yaml.safe_dump({"sessions": [
            {"id": "s1", "narrative_date": "2026-01-01T09:00:00+00:00", "messages": ["Remember this."]},
        ]}, sort_keys=False))
        (checkpoint / "app_state.json").write_text("{}")
        (checkpoint / "facts.yaml").write_text("facts: []\n")
        (checkpoint / "checkpoint.json").write_text(json.dumps({"sessions": 1, "accepted_through": "2026-01-01"}))
        for index in range(1, 201):
            self._write(
                f"tests/morgan/{index:03d}.yaml",
                yaml.safe_dump({
                    "id": f"{index:03d}",
                    "narrative_anchor_date": "2026-01-02",
                    "test": "Do the requested work.",
                }, sort_keys=False),
            )
        self._write(
            "authoring/release_manifests/morgan.json",
            json.dumps({
                "persona": "morgan",
                "checkpoint_identity": "checkpoint-id",
                "evaluation_date": "2026-01-02",
                "test_count": 200,
            }),
        )
        self._write("registry/personas.yaml", "personas:\n  - id: morgan\n    name: Morgan Chen\n")
        self._write(
            "mock_mcp/manifests/morgan.yaml",
            "persona_id: morgan\ntools:\n  - send_email\n",
        )
        self._write("mock_mcp/state/morgan_baseline.json", "{}")
        self._write("mock_mcp/server.py", "# mock\n")
        for relative in (
            "harness/benchmark_soul.md",
            "harness/environment.py",
            "harness/hermes_driver.py",
            "harness/claude_driver.py",
            "harness/memory_capture.py",
            "harness/costing.py",
            "graders/mechanical.py",
            "graders/llm_judge.py",
            "reference/runtimes/hermes.py",
            "harness/adapters/hermes_profile.py",
            "reference/evaluate.py",
        ):
            self._write(relative, "# fixture\n")
        self._write(
            "harness/run_simulation.py",
            "def _test_narrative_time(spec):\n"
            "    return spec.get(\"narrative_anchor_date\")\n"
            "test_narrative_time = _test_narrative_time(pre_spec)\n"
            "run(narrative_time=test_narrative_time)\n",
        )
        self._write(
            "pricing/latest.json",
            json.dumps({"snapshot": {"snapshot_date": "2026-01-01", "models": {
                "fixture-model": {"max_input_tokens": 100000, "input_cost_per_token": 1, "output_cost_per_token": 2},
            }}}),
        )
        (self.hermes / "plugins" / "memory").mkdir(parents=True)
        (self.hermes / "agent.py").write_text("# hermes\n")
        (self.hermes / "plugins" / "memory" / "plugin.py").write_text("# plugin\n")
        (self.hermes / "plugins" / "memory" / "mem0").mkdir()
        (self.hermes / "plugins" / "memory" / "mem0" / "plugin.py").write_text("# mem0\n")
        for provider in ("hindsight", "supermemory"):
            plugin = self.hermes / "plugins" / "memory" / provider
            plugin.mkdir()
            (plugin / "plugin.py").write_text(f"# {provider}\n")
        (self.honcho / "src").mkdir(parents=True)
        (self.honcho / "src" / "service.py").write_text("# honcho\n")
        (self.honcho / "docker-compose.dolphinbench.yml").write_text(
            "# Pinned upstream Honcho commit: 1234567890abcdef\n"
            "x-honcho-dolphinbench-environment:\n"
            "  EMBEDDING_MODEL_CONFIG__TRANSPORT: openai\n"
            "  EMBEDDING_MODEL_CONFIG__MODEL: openai/text-embedding-3-small\n"
            "  DERIVER_MODEL_CONFIG__TRANSPORT: openai\n"
            "  DERIVER_MODEL_CONFIG__MODEL: gpt-5.6-luna\n"
            "  DERIVER_MODEL_CONFIG__THINKING_EFFORT: medium\n"
            "  SUMMARY_MODEL_CONFIG__TRANSPORT: openai\n"
            "  SUMMARY_MODEL_CONFIG__MODEL: gpt-5.6-luna\n"
            "  SUMMARY_MODEL_CONFIG__THINKING_EFFORT: medium\n"
            "  DREAM_DEDUCTION_MODEL_CONFIG__TRANSPORT: openai\n"
            "  DREAM_DEDUCTION_MODEL_CONFIG__MODEL: gpt-5.6-luna\n"
            "  DREAM_DEDUCTION_MODEL_CONFIG__THINKING_EFFORT: none\n"
            "  DREAM_INDUCTION_MODEL_CONFIG__TRANSPORT: openai\n"
            "  DREAM_INDUCTION_MODEL_CONFIG__MODEL: gpt-5.6-luna\n"
            "  DREAM_INDUCTION_MODEL_CONFIG__THINKING_EFFORT: none\n"
            "  DIALECTIC_LEVELS__minimal__MODEL_CONFIG__TRANSPORT: openai\n"
            "  DIALECTIC_LEVELS__minimal__MODEL_CONFIG__MODEL: gpt-5.6-luna\n"
            "  DIALECTIC_LEVELS__minimal__MODEL_CONFIG__THINKING_EFFORT: none\n"
            "  DIALECTIC_LEVELS__low__MODEL_CONFIG__TRANSPORT: openai\n"
            "  DIALECTIC_LEVELS__low__MODEL_CONFIG__MODEL: gpt-5.6-luna\n"
            "  DIALECTIC_LEVELS__low__MODEL_CONFIG__THINKING_EFFORT: none\n"
            "  DIALECTIC_LEVELS__medium__MODEL_CONFIG__TRANSPORT: openai\n"
            "  DIALECTIC_LEVELS__medium__MODEL_CONFIG__MODEL: gpt-5.6-luna\n"
            "  DIALECTIC_LEVELS__medium__MODEL_CONFIG__THINKING_EFFORT: none\n"
            "  DIALECTIC_LEVELS__high__MODEL_CONFIG__TRANSPORT: openai\n"
            "  DIALECTIC_LEVELS__high__MODEL_CONFIG__MODEL: gpt-5.6-luna\n"
            "  DIALECTIC_LEVELS__high__MODEL_CONFIG__THINKING_EFFORT: none\n"
            "  DIALECTIC_LEVELS__max__MODEL_CONFIG__TRANSPORT: openai\n"
            "  DIALECTIC_LEVELS__max__MODEL_CONFIG__MODEL: gpt-5.6-luna\n"
            "  DIALECTIC_LEVELS__max__MODEL_CONFIG__THINKING_EFFORT: none\n"
        )

    def _prepare(
        self,
        *,
        context_length: int = 100000,
        configurations: tuple[str, ...] = matrix.HERMES_MEMORY_CONFIGURATIONS,
        reasoning_effort: str = "medium",
        agent_runtime: str = "hermes",
    ) -> Path:
        with patch.object(
            matrix,
            "validated_checkpoint_identity",
            return_value="checkpoint-id",
        ):
            return matrix.prepare_matrix(
                output_root=self.root / "runs" / "unique",
                personas=("morgan",),
                configurations=configurations,
                checkpoints={},
                model="fixture-model",
                agent_context_length=context_length,
                agent_base_url="https://example.invalid/v1",
                agent_api_key_env="EVAL_KEY",
                agent_reasoning_effort=reasoning_effort,
                agent_runtime=agent_runtime,
                root=self.root,
                hermes_source=self.hermes,
                env=self.environment,
            )

    def _completed_seed(
        self,
        *,
        configurations: tuple[str, ...] = ("builtin",),
        output_name: str = "unique",
        model: str = "fixture-model",
    ) -> tuple[Path, Path, Path]:
        with patch.object(matrix, "validated_checkpoint_identity", return_value="checkpoint-id"):
            seed_manifest_path = matrix.prepare_matrix(
                output_root=self.root / "runs" / output_name,
                personas=("morgan",),
                configurations=configurations,
                checkpoints={},
                model=model,
                agent_context_length=100000,
                agent_base_url="https://example.invalid/v1",
                agent_api_key_env="EVAL_KEY",
                agent_reasoning_effort="medium",
                agent_runtime="hermes",
                root=self.root,
                hermes_source=self.hermes,
                env=self.environment,
            )
        seed_manifest = json.loads(seed_manifest_path.read_text())
        home = self.root / "hermes-home"
        profiles = home / "profiles"
        state_jobs = []
        profile_hashes = {}
        for job in seed_manifest["jobs"]:
            profile_dir = profiles / job["profile"]
            profile_dir.mkdir(parents=True, exist_ok=True)
            (profile_dir / "config.yaml").write_text("model: fixture\n")
            if job["configuration"] == "honcho":
                (profile_dir / "honcho.json").write_text(json.dumps({
                    "baseUrl": "http://127.0.0.1:8000",
                }))
            if job["configuration"] == "hindsight":
                hindsight_dir = profile_dir / "hindsight"
                hindsight_dir.mkdir()
                (hindsight_dir / "config.json").write_text(json.dumps({
                    "mode": "local_external",
                    "api_url": "http://127.0.0.1:8888",
                    "budget": "mid",
                    "bank_id": job["provider_identity"]["bank_id"],
                }))
            if job["configuration"] == "supermemory":
                (profile_dir / "supermemory.json").write_text(json.dumps({
                    "base_url": "https://example--supermemory.modal.run",
                    "container_tag": job["provider_identity"]["container_tag"],
                }))
            profile_hashes[job["id"]] = profile_config_hashes(profile_dir)
            run_dir = Path(job["run_dir"])
            result_path = run_dir / "results_phase_01.json"
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(json.dumps({
                "seed_sessions": [{"id": "s1"}],
                "seed_calls": [],
                "test_results": [],
                "phase1_ended_at": "2026-01-01T00:01:00+00:00",
            }))
            state_jobs.append({
                "id": job["id"],
                "status": "completed",
                "result_path": str(result_path),
                "requested_test_ids": [],
            })
        seed_state_path = Path(seed_manifest["output_root"]) / "launch_state.json"
        matrix._write_json(seed_state_path, {
            "manifest": str(seed_manifest_path.resolve()),
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-01T00:01:00+00:00",
            "profiles": profile_hashes,
            "phases": [{
                "index": 1,
                "operation": "seed_only",
                "started_at": "2026-01-01T00:00:00+00:00",
                "ended_at": "2026-01-01T00:01:00+00:00",
                "failure_count": 0,
                "requested_test_ids": [],
                "jobs": state_jobs,
            }],
        })
        return seed_manifest_path, seed_state_path, home

    def _prepare_test_only(self, *, configurations: tuple[str, ...] = ("builtin",)) -> tuple[Path, Path, Path]:
        seed_manifest_path, seed_state_path, home = self._completed_seed(configurations=configurations)
        with patch.object(matrix, "HERMES_HOME", home),\
             patch.object(matrix, "validated_checkpoint_identity", return_value="checkpoint-id"):
            test_manifest_path = matrix.prepare_test_only(
                seed_manifest_path=seed_manifest_path,
                seed_state_path=seed_state_path,
                output_root=self.root / "runs" / "tests-only",
                configurations=configurations,
                test_ids=("001",),
                root=self.root,
                hermes_source=self.hermes,
                env=self.environment,
            )
        return test_manifest_path, seed_manifest_path, home

    def test_prepare_test_only_reuses_completed_profile_and_new_result_paths(self) -> None:
        test_manifest_path, seed_manifest_path, home = self._prepare_test_only()
        test_manifest = json.loads(test_manifest_path.read_text())
        seed_manifest = json.loads(seed_manifest_path.read_text())
        test_job = test_manifest["jobs"][0]
        seed_job = seed_manifest["jobs"][0]
        self.assertEqual(test_manifest["evaluation_scope"]["kind"], "test_only")
        self.assertEqual(test_manifest["evaluation_scope"]["test_ids"], ["001"])
        self.assertEqual(test_job["profile"], seed_job["profile"])
        self.assertEqual(test_job["provider_identity"], seed_job["provider_identity"])
        self.assertEqual(test_job["state_path"], seed_job["state_path"])
        self.assertEqual(test_job["log_path"], seed_job["log_path"])
        self.assertEqual(test_job["tool_config_path"], seed_job["tool_config_path"])
        self.assertNotEqual(test_job["result_path"], seed_job["result_path"])
        self.assertTrue(test_job["result_path"].startswith(str(self.root / "runs" / "tests-only")))
        self.assertTrue((home / "profiles" / seed_job["profile"] / "config.yaml").is_file())
        self.assertEqual(
            test_manifest["launch"]["profile_creation"],
            "reused_from_completed_seed",
        )
        self.assertEqual(
            test_manifest["source_seed_artifacts"]["manifest"],
            str(seed_manifest_path),
        )
        self.assertEqual(test_manifest["judge"]["deployment"], "gpt-5.6-sol")
        self.assertEqual(
            test_manifest["launch"]["hermes_bin"],
            str(self.hermes / "venv" / "bin" / "hermes"),
        )
        self.assertEqual(
            test_manifest["launch"]["hermes_python"],
            str(self.hermes / "venv" / "bin" / "python"),
        )

    def test_prepare_test_only_rejects_incomplete_seed(self) -> None:
        seed_manifest_path, seed_state_path, home = self._completed_seed()
        state = json.loads(seed_state_path.read_text())
        result_path = Path(state["phases"][0]["jobs"][0]["result_path"])
        result_path.write_text(json.dumps({"seed_sessions": []}))
        with patch.object(matrix, "HERMES_HOME", home):
            with self.assertRaisesRegex(matrix.PreparationError, "ingestion is incomplete"):
                matrix.prepare_test_only(
                    seed_manifest_path=seed_manifest_path,
                    seed_state_path=seed_state_path,
                    output_root=self.root / "runs" / "bad-tests-only",
                    configurations=("builtin",),
                    test_ids=("001",),
                    root=self.root,
                    hermes_source=self.hermes,
                    env=self.environment,
                )
        self.assertFalse((self.root / "runs" / "bad-tests-only").exists())

    def test_test_only_uses_skip_seeding_and_never_creates_profile(self) -> None:
        test_manifest_path, _seed_manifest_path, home = self._prepare_test_only()
        commands: list[list[str]] = []
        with patch.object(matrix, "HERMES_HOME", home),\
             patch.object(matrix, "load_environment", return_value=self.environment),\
             patch.object(matrix, "verify_manifest_hashes"),\
             patch(
                 "reference.runtimes.hermes.create_profile_from_manifest",
                 side_effect=AssertionError("profile creation"),
             ),\
             patch.object(matrix.subprocess, "run", side_effect=self._runner_stub(commands)):
            self.assertEqual(
                matrix.launch_matrix(test_manifest_path, 1, ("001",), resume=False),
                0,
            )
        self.assertIn("--skip-seeding", commands[0])
        self.assertNotIn("--seed-only", commands[0])
        self.assertIn("--only", commands[0])

    def test_test_only_rejects_changed_tests_or_shared_runtime_code(self) -> None:
        test_manifest_path, _seed_manifest_path, _home = self._prepare_test_only()
        for relative in ("tests/morgan/001.yaml", "harness/environment.py"):
            path = self.root / relative
            original = path.read_bytes()
            with self.subTest(path=relative), patch.object(matrix, "_git_revision", return_value=None), \
                    patch.object(matrix.subprocess, "run") as execute:
                try:
                    path.write_text("changed: true\n")
                    with self.assertRaisesRegex(matrix.ManifestDriftError, "changed"):
                        matrix.launch_matrix(test_manifest_path, 1, ("001",), resume=False)
                    execute.assert_not_called()
                finally:
                    path.write_bytes(original)

    def test_prepare_rejects_test_date_that_differs_from_accepted_release(self) -> None:
        path = self.root / "tests" / "morgan" / "001.yaml"
        test = yaml.safe_load(path.read_text())
        test["narrative_anchor_date"] = "2026-01-03"
        path.write_text(yaml.safe_dump(test, sort_keys=False))
        with self.assertRaisesRegex(matrix.PreparationError, "accepted evaluation date"):
            self._prepare()
        self.assertFalse((self.root / "runs" / "unique").exists())

    def test_test_phases_reject_duplicate_or_reused_ids(self) -> None:
        self.assertEqual(matrix._parse_test_ids("1,010,200"), ("001", "010", "200"))
        with self.assertRaisesRegex(matrix.PreparationError, "duplicates"):
            matrix._parse_test_ids("001,1")

    def test_hermes_planning_keeps_model_specific_ingestions(self):
        pricing_path = self.root / "pricing/latest.json"
        pricing = json.loads(pricing_path.read_text())
        models = (("luna", "gpt-5.6-luna"), ("minimax", "minimax/minimax-m3"))
        for _, model in models:
            pricing["snapshot"]["models"][model] = pricing["snapshot"]["models"]["fixture-model"]
        pricing_path.write_text(json.dumps(pricing))
        runtimes = []
        for family, model in models:
            manifest, state, home = self._completed_seed(
                configurations=plan.MEMORY_PROVIDERS, output_name=family, model=model,
            )
            runtimes.append({"runtime": "hermes", "model_family": family, "model": model,
                "context_length": 100000, "reasoning_effort": "medium",
                "agent_base_url": "https://example.invalid/v1", "agent_api_key_env": "EVAL_KEY",
                "completed_ingestions": {provider: {"manifest": str(manifest), "state": str(state)}
                                          for provider in plan.MEMORY_PROVIDERS}})
        config = {"purpose": "integration_smoke", "persona": "morgan", "runtimes": runtimes,
            "hermes_source": str(self.hermes), "output_root": str(self.root / "plan"),
            "published_tests": {"directory": str(self.root / "tests/morgan"), "ids": ["001"],
                                "sha256": {"001": matrix._sha256(self.root / "tests/morgan/001.yaml")}},
            "resource_limits": {"global_limit": 1, "azure-luna": 2, "azure-sol-judge": 1,
                "judge:azure": 1, "agent:hermes": 1, "provider:custom": 1,
                **{"memory:" + provider: 1 for provider in plan.MEMORY_PROVIDERS}}}
        config_path = self._write("plan.json", json.dumps(config))
        with patch.object(plan, "ROOT", self.root), patch.object(matrix, "HERMES_HOME", home), \
                patch.object(plan, "load_environment", return_value=self.environment), \
                patch.object(matrix, "validated_checkpoint_identity", return_value="checkpoint-id"):
            result = plan.build_plan(config_path)
            saved = json.loads(result["plan"].read_text())
            self.assertEqual(result["ready_count"], 10)
            for row in saved["ready_cells"]:
                source = next(item for item in runtimes if item["model"] == row["model"])
                expected = source["completed_ingestions"][row["memory_provider"]]["manifest"]
                self.assertEqual(row["source_seed_manifest"], expected)
            for change, error in (("reuse", "another configuration uses this ingestion"),
                                  ("model", "ingestion and evaluation models differ")):
                rejected = json.loads(json.dumps(config))
                rejected["output_root"] = str(self.root / "rejected" / change)
                if change == "reuse":
                    rejected["runtimes"][1]["completed_ingestions"] = runtimes[0]["completed_ingestions"]
                else:
                    rejected["runtimes"][1]["model"] = "fixture-model"
                config_path.write_text(json.dumps(rejected))
                with self.subTest(change=change), self.assertRaisesRegex(plan.PlanError, error):
                    plan.build_plan(config_path)

    def _runner_stub(self, commands: list[list[str]]):
        def run(command, **_kwargs):
            commands.append(list(command))
            out = Path(command[command.index("--out") + 1])
            ids = command[command.index("--only") + 1].split(",")
            existing = json.loads(out.read_text()) if out.exists() else {}
            existing["seed_sessions"] = existing.get("seed_sessions") or [{"id": "s1"}]
            existing["seed_calls"] = existing.get("seed_calls") or []
            existing["test_results"] = [{"source_id": test_id} for test_id in ids]
            out.write_text(json.dumps(existing))
            return subprocess.CompletedProcess(command, 0)
        return run


class ClaudeNativeMemoryTests(unittest.TestCase):
    def test_each_test_restores_an_independent_copy_of_the_frozen_memory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_config = root / "source-config"
            memory = native.native_memory_dir(source_config)
            memory.mkdir(parents=True)
            (memory / "MEMORY.md").write_text("# Memory\noriginal\n", encoding="utf-8")
            archive = root / "native-memory.tar.gz"
            native.create_snapshot(config_dir=source_config, archive_path=archive)

            first = root / "first-config"
            second = root / "second-config"
            first_memory = native.restore_snapshot(archive_path=archive, config_dir=first)
            second_memory = native.restore_snapshot(archive_path=archive, config_dir=second)
            (first_memory / "MEMORY.md").write_text("changed\n", encoding="utf-8")
            self.assertEqual("# Memory\noriginal\n", (second_memory / "MEMORY.md").read_text())
            self.assertEqual("# Memory\noriginal\n", native.restore_snapshot(
                archive_path=archive, config_dir=root / "third-config",
            ).joinpath("MEMORY.md").read_text())


class ModalEvalWorkerTests(unittest.TestCase):
    def test_configures_only_the_worker_copy_for_isolated_simulated_apps(self) -> None:
        from reference.runtimes.hermes import profile_config_hashes

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            profile = home / ".hermes" / "profiles" / "completed-morgan"
            profile.mkdir(parents=True)
            old_config = {
                "model": {"default": "gpt-5.6-luna"},
                "memory": {"provider": "mem0"},
                "mcp_servers": {
                    "dolphinbench-apps": {
                        "command": "/old/python",
                        "args": ["/old/mock_mcp/server.py"],
                        "env": {
                            "ENACT_PERSONA": "morgan",
                            "ENACT_STATE_PATH": "/old/state.json",
                        },
                    },
                },
            }
            profile.joinpath("config.yaml").write_text(yaml.safe_dump(old_config, sort_keys=False))
            profile.joinpath("state.db").write_bytes(b"frozen history")
            workspace = root / "workspace"
            manifest = {
                "root": str(workspace / "repo"),
                "launch": {"mock_mcp_python": "/usr/local/bin/python"},
                "jobs": [{
                    "profile": profile.name,
                    "persona": "morgan",
                    "run_id": "dolphinbench-test-morgan",
                    "state_path": str(workspace / "output" / "state.json"),
                    "log_path": str(workspace / "output" / "calls.jsonl"),
                    "tool_config_path": str(workspace / "output" / "tool_config.json"),
                }],
            }
            manifest_path = root / "manifest.json"

            with patch.object(worker.Path, "home", return_value=home):
                worker._configure_restored_profile_for_worker(manifest_path, manifest)

            self.assertTrue((workspace / "output").is_dir())
            self.assertFalse((workspace / "output" / "state.json").exists())
            updated = yaml.safe_load(profile.joinpath("config.yaml").read_text())
            app_server = updated["mcp_servers"]["dolphinbench-apps"]
            self.assertEqual("/usr/local/bin/python", app_server["command"])
            self.assertEqual([str(workspace / "repo" / "mock_mcp" / "server.py")], app_server["args"])
            self.assertEqual({
                "ENACT_PERSONA": "morgan",
                "ENACT_MOCK_MANIFEST": str(workspace / "repo" / "mock_mcp" / "manifests" / "morgan.yaml"),
                "ENACT_STATE_PATH": str(workspace / "output" / "state.json"),
                "ENACT_LOG_PATH": str(workspace / "output" / "calls.jsonl"),
                "ENACT_TOOL_CONFIG_PATH": str(workspace / "output" / "tool_config.json"),
                "ENACT_RUN_ID": "dolphinbench-test-morgan",
            }, app_server["env"])
            self.assertEqual({"default": "gpt-5.6-luna"}, updated["model"])
            self.assertEqual({"provider": "mem0"}, updated["memory"])
            self.assertEqual(b"frozen history", profile.joinpath("state.db").read_bytes())
            expected_hashes = profile_config_hashes(profile)
            self.assertEqual(expected_hashes, manifest["jobs"][0]["seed_profile_config_hashes"])
            saved_job = json.loads(manifest_path.read_text())["jobs"][0]
            self.assertEqual(expected_hashes, saved_job["seed_profile_config_hashes"])
