"""Model selection, independent stores, and scoped credentials."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness.claude_driver import _subscription_environment
from reference import evaluate, plan


class ConfigurationTests(unittest.TestCase):
    def test_claude_test_files_preserve_the_completed_memory_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "run"
            job = {
                "agent_runtime": "claude", "run_dir": str(root),
                "ledger_path": str(root / "costs.jsonl"),
                "provider_identity": {"kind": "claude_native_auto_memory"},
                "gateway_config": {"claude_native_memory_seed_dir": str(root / "memory")},
            }
            evaluate._write_test_only_runtime_files(job)
            self.assertEqual(json.loads((root / "memory_provider_identity.json").read_text()), {
                "provider_identity": job["provider_identity"],
                "gateway_config": job["gateway_config"],
            })

    def test_paper_model_selection_accepts_all_three_harness_model_pairs(self):
        runtimes = [
            {"runtime": "hermes", "model_family": family, "model": model,
             "reasoning_effort": "high", "context_length": context,
             "agent_base_url": endpoint, "agent_api_key_env": key}
            for family, model, context, endpoint, key in (
                ("luna", "gpt-5.6-luna", 1050000, "https://azure.example/openai/v1", "AZURE_OPENAI_API_KEY"),
                ("minimax", "minimax/minimax-m3", 1048576, "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
            )
        ]
        runtimes.append({"runtime": "claude", "model_family": "sonnet",
                         "model": "claude-sonnet-5", "reasoning_effort": None})
        actual = plan._runtime_configs(runtimes, purpose="final_results")
        self.assertEqual([(row["runtime"], row["model"]) for row in actual],
                         [(row["runtime"], row["model"]) for row in runtimes])

    def test_hermes_models_and_personas_use_separate_stores_and_correct_profiles(self):
        # Worker identity is selected at import time, so each configuration needs a fresh process.
        code = """
import json
import os
import tempfile
from pathlib import Path
import yaml
from reference.execution import hermes
os.environ.update(AZURE_OPENAI_API_KEY='unused',
                  AZURE_OPENAI_ENDPOINT='https://example.openai.azure.com',
                  OPENROUTER_API_KEY='unused')
rows = []
for provider in ('hindsight', 'supermemory'):
    plan = hermes.build_plan(provider=provider, mode='final')
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        profile, env = hermes._prepare_hermes_seed_profile(provider, root, plan)
        config = yaml.safe_load((root / 'hermes-home/profiles' / profile / 'config.yaml').read_text())
        assert hermes._prepare_hermes_seed_profile(provider, root, plan)[0] == profile
        assert config['mcp_servers']['dolphinbench-apps']['command'] == str(hermes.REMOTE_MOCK_MCP_PYTHON)
        rows.append({'plan': plan.as_dict(), 'model': config['model']})
print(json.dumps(rows))
"""
        volumes = set()
        for agent, model, provider, endpoint, api_mode, context in (
            ("luna", "gpt-5.6-luna", "azure-foundry",
             "https://example.openai.azure.com/openai/v1", "codex_responses", 1050000),
            ("minimax-m3", "minimax/minimax-m3", "openrouter",
             "https://openrouter.ai/api/v1", "chat_completions", 1048576),
        ):
            for persona in ("morgan", "alex"):
                with self.subTest(agent=agent, persona=persona):
                    result = subprocess.run(
                        [sys.executable, "-c", code], check=True, capture_output=True, text=True,
                        env={**os.environ, "DOLPHINBENCH_MEMORY_INGESTION_PERSONA": persona,
                             "DOLPHINBENCH_MEMORY_INGESTION_AGENT": agent},
                    )
                    rows = json.loads(result.stdout)
                    self.assertEqual(len(rows), 2)
                    for row in rows:
                        self.assertGreater(row["plan"]["source_count"], 0)
                        self.assertEqual(row["plan"]["model"], model)
                        self.assertEqual(row["model"], {
                            "provider": provider, "default": model, "base_url": endpoint,
                            "api_mode": api_mode, "context_length": context,
                        })
                        volume = row["plan"]["snapshot_volume"]
                        self.assertNotIn(volume, volumes)
                        volumes.add(volume)
        self.assertEqual(len(volumes), 8)

    def test_claude_conditions_use_separate_files_and_stores(self):
        jobs = [plan.condition(provider, "fixture-run") for provider in plan.PROVIDERS]
        self.assertEqual({job["provider"] for job in jobs},
                         {"builtin", "mem0", "honcho", "hindsight", "supermemory"})
        for field in ("container_name", "volume_name", "namespace", "uid",
                      "home", "project", "config_dir", "auto_memory"):
            with self.subTest(field=field):
                self.assertEqual(len({job[field] for job in jobs}), 5)
        self.assertTrue(all(not job["reuse_hermes_store"] for job in jobs))

    def test_only_selected_credentials_reach_claude(self):
        secrets = {
            "CLAUDE_CODE_OAUTH_TOKEN": "subscription", "AZURE_OPENAI_API_KEY": "azure",
            "MEM0_API_KEY": "mem0", "HONCHO_API_KEY": "honcho",
            "SUPERMEMORY_API_KEY": "supermemory", "TS_AUTHKEY": "tailscale",
        }
        for provider in plan.PROVIDERS:
            with self.subTest(provider=provider):
                env = plan.child_environment(provider, "/home/isolated", {}, secrets)
                self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "subscription")
                self.assertFalse(set(secrets) - {"CLAUDE_CODE_OAUTH_TOKEN"} & set(env))
                child = _subscription_environment(env, inherit=False)
                self.assertNotIn("AZURE_OPENAI_API_KEY", child)
                self.assertNotIn("ANTHROPIC_API_KEY", child)
