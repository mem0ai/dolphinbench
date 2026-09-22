"""Local checks for reference ingestion; external execution is mocked."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import tempfile
import unittest

from harness.claude_driver import ClaudeResult
from reference.execution import claude_native as native_ingestion, hermes as hermes_ingestion
from reference.runtimes import claude_code as claude_ingestion, claude_code as native
from reference.runtimes.claude_code import SourceMessage


def _message(source_id: str, text: str) -> SourceMessage:
    import hashlib
    return SourceMessage(source_id, "2024-01-01T00:00:00Z", text, hashlib.sha256(text.encode()).hexdigest())


class ClaudePluginIngestionTests(unittest.TestCase):
    def _config(self, root: Path, *, model: str = "claude-sonnet-5") -> claude_ingestion.PluginIngestionConfig:
        source = root / "official-plugin"
        source.mkdir(exist_ok=True)
        return claude_ingestion.PluginIngestionConfig(
            provider="mem0", model=model, home=root / "home", project=root / "project",
            claude_config_dir=root / "config", plugin_source=source,
            plugin_identity={"name": "mem0", "sha256": "a" * 64},
            plugin_settings={"namespace": "test", "api_url": "https://example.test", "api_key": "secret"},
            environment={"CLAUDE_CODE_OAUTH_TOKEN": "test-subscription"},
        )

    def test_runs_only_bounded_missing_messages_with_timestamp_prefix_and_fresh_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self._config(root)
            messages = [_message("one", "first"), _message("two", "second")]
            receipt = root / "state" / "receipt.json"
            traces = root / "traces"
            claude_ingestion.initialize_plugin_receipt(receipt_path=receipt, config=config, messages=messages)
            calls: list[tuple[str, dict]] = []
            def call(message: str, **kwargs: object) -> ClaudeResult:
                calls.append((message, kwargs))
                return ClaudeResult(ok=True, session_id="fresh", response_text="ordinary answer")
            setup = {"env": {}, "plugin_dirs": [str(config.plugin_source)], "settings": {}}
            with patch.object(claude_ingestion, "configure_plugin", return_value=setup):
                result = claude_ingestion.ingest(receipt_path=receipt, trace_dir=traces, config=config,
                                          messages=messages, max_messages=1, run_call=call)
            self.assertEqual("in_progress", result["status"])
            self.assertEqual(["first"], [item[0] for item in calls])
            self.assertEqual("2024-01-01T00:00:00Z", calls[0][1]["narrative_time"])
            self.assertTrue(calls[0][1]["persist_session"])
            self.assertIsNone(calls[0][1]["timeout"])
            self.assertEqual("seed", calls[0][1]["native_memory_mode"])
            self.assertEqual(["Read", "Write", "Edit", "Bash", "Skill", "ToolSearch"], calls[0][1]["builtin_tools"])
            self.assertEqual(["mcp__plugin_mem0_mem0__*"], list(calls[0][1]["allowed_mcp_tools"]))
            self.assertEqual(1, len(list(traces.glob("00000-*.json"))))

    def test_resume_rejects_changed_configuration_or_history(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config, messages = self._config(root), [_message("one", "first")]
            receipt = root / "receipt.json"
            claude_ingestion.initialize_plugin_receipt(receipt_path=receipt, config=config, messages=messages)
            cases = [
                (replace(config, model="different-model"), messages),
                (replace(config, plugin_identity={"name": "mem0", "sha256": "b" * 64}), messages),
                (config, [_message("one", "changed")]),
                (replace(config, plugin_settings={**config.plugin_settings, "namespace": "another-store"}), messages),
                (config, [replace(messages[0], timestamp="2025-01-01T00:00:00Z")]),
            ]
            for changed, sources in cases:
                with self.subTest(config=changed, messages=sources), self.assertRaisesRegex(
                    claude_ingestion.ClaudePluginIngestionError, "configuration differs"
                ):
                    claude_ingestion.ingest(receipt_path=receipt, trace_dir=root / "traces", config=changed,
                                     messages=sources, max_messages=1,
                                     run_call=lambda *a, **k: self.fail("changed inputs reached the model"))

    def test_completed_turn_is_not_sent_twice_and_trace_redacts_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config, messages = self._config(root), [_message("one", "first")]
            receipt = root / "receipt.json"
            claude_ingestion.initialize_plugin_receipt(receipt_path=receipt, config=config, messages=messages)
            setup = {"env": {}, "plugin_dirs": [], "settings": {}}
            calls = []
            def invoke(*args, **kwargs):
                calls.append(args)
                return ClaudeResult(ok=True, session_id="x", response_text="secret test-subscription")
            with patch.object(claude_ingestion, "configure_plugin", return_value=setup):
                for _ in range(2):
                    claude_ingestion.ingest(receipt_path=receipt, trace_dir=root / "traces", config=config,
                                     messages=messages, max_messages=1, run_call=invoke)
            self.assertEqual(len(calls), 1)
            trace = next((root / "traces").glob("*.json")).read_text()
            self.assertNotIn("test-subscription", trace)
            self.assertNotIn("secret", trace)


class ModalMemoryIngestionTests(unittest.TestCase):
    def test_continuous_group_loop_records_progress_without_restarting_a_service(self) -> None:
        group_receipts = iter([
            {
                "provider": "hindsight",
                "ingestion_path": "hermes_official_memory_plugin",
                "completed_source_ids": ["one"],
                "status": "paused",
            },
            {
                "provider": "hindsight",
                "ingestion_path": "hermes_official_memory_plugin",
                "completed_source_ids": ["one", "two"],
                "status": "paused",
            },
            {
                "provider": "hindsight",
                "ingestion_path": "hermes_official_memory_plugin",
                "completed_source_ids": ["one", "two", "three"],
                "status": "completed",
            },
        ])
        events: list[str] = []

        def run_group() -> dict:
            events.append("run")
            return next(group_receipts)

        receipt, groups, paused = hermes_ingestion._run_continuous_groups(
            provider="hindsight",
            source_count=3,
            run_group=run_group,
            save_progress=lambda: events.append("save"),
        )

        self.assertEqual(["run", "save", "run", "save", "run"], events)
        self.assertEqual(3, groups)
        self.assertEqual("completed", receipt["status"])
        self.assertFalse(paused)

    def test_continuous_group_loop_rejects_a_group_that_made_no_progress(self) -> None:
        receipt = {
            "provider": "hindsight",
            "ingestion_path": "hermes_official_memory_plugin",
            "completed_source_ids": [],
            "status": "paused",
        }
        with self.assertRaisesRegex(
            hermes_ingestion.IngestionPreparationError, "made no progress",
        ):
            hermes_ingestion._run_continuous_groups(
                provider="hindsight",
                source_count=3,
                run_group=lambda: receipt,
                save_progress=lambda: None,
            )

    def test_continuous_group_loop_accepts_completed_supermemory_plugin_receipt(self) -> None:
        receipt = {
            "provider": "supermemory",
            "ingestion_path": "hermes_official_memory_plugin",
            "completed_source_ids": ["one"],
            "status": "completed",
        }
        actual, groups, paused = hermes_ingestion._run_continuous_groups(
            provider="supermemory",
            source_count=1,
            run_group=lambda: receipt,
            save_progress=lambda: self.fail("completed ingestion must not save partial progress"),
        )
        self.assertIs(receipt, actual)
        self.assertEqual(1, groups)
        self.assertFalse(paused)


ROOT = Path(__file__).resolve().parents[3]


CORPUS = ROOT / "registry/personas/morgan/life_sim.yaml"


class ModalClaudeNativeIngestionTests(unittest.TestCase):
    def test_success_checkpoint_persists_memory_and_receipt_in_source_order(self) -> None:
        plan = native_ingestion.build_plan(corpus_path=CORPUS)
        sources, _ = native.load_morgan_source_messages(CORPUS)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = native_ingestion.VolumePaths(root / "volume")
            config = root / "runtime" / "claude-config"
            receipt = root / "runtime" / "receipt.json"
            project = root / "project"
            with patch.object(native, "FIXED_PROJECT_DIR", str(project)):
                native.initialize_receipt(
                    corpus_path=CORPUS, receipt_path=receipt, config_dir=config,
                    project_dir=str(project), model=plan.model,
                )
                memory = native.native_memory_dir(config)
                memory.mkdir(parents=True)
                (memory / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
                native_ingestion._checkpoint_before_call(paths=paths, source=sources[0], commit=lambda: None)
                native_ingestion._checkpoint_after_call(
                    plan=plan, paths=paths, config_dir=config, receipt_path=receipt,
                    source=sources[0],
                    telemetry={
                        "content_sha256": sources[0].content_sha256,
                        "elapsed_seconds": 1.25,
                        "detected_model": "claude-sonnet-5",
                        "token_usage": {"input_tokens": 12, "output_tokens": 3},
                        "status_code": 200,
                        "outcome": "completed",
                    },
                    commit=lambda: None,
                )
                pending = native.pending_source_messages(corpus_path=CORPUS, receipt_path=paths.receipt)
            self.assertFalse(paths.inflight.exists())
            self.assertEqual("# Memory\n", (paths.memory / "MEMORY.md").read_text())
            self.assertEqual(3399, len(pending))
            self.assertEqual(sources[1].source_id, pending[0].source_id)
            receipt_value = __import__("json").loads(paths.receipt.read_text())
            self.assertEqual(sources[0].content_sha256, receipt_value["source_attempts"][sources[0].source_id][-1]["content_sha256"])
