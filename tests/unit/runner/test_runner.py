"""Offline runner lifecycle and characterization of the existing Hermes boundary."""

from __future__ import annotations

import copy
import builtins
import io
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import redirect_stdout
from unittest.mock import patch

import yaml

from examples.offline_adapter import OfflineAdapter
from harness import durable_json
from harness.adapter import Interaction, InteractionRecord, load_adapter
from harness.adapters.hermes import HermesAdapter, READ_ONLY, TOOLSETS
from harness.runner import Runner, main, run_lock
from harness.submission import SubmissionError, read_zip, validate
from tests.unit.submissions.test_submission import fixture
from harness.dataset import load_test


class SharedAppStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / "state.json"
        self.base.write_text(json.dumps({"inbox": [{"id": "original"}],
                                        "docs": {"keep": "original"}, "sent": []}))
        self.spec = {"id": "001", "mock_state_base": {"path": "state.json",
                     "sha256": hashlib.sha256(self.base.read_bytes()).hexdigest()},
                     "mock_state": {"inbox": [], "docs": {"replacement": "only"}, "new": None}}
        self.path = self.root / "001.yaml"
        self.path.write_text(yaml.safe_dump(self.spec))

    def test_complete_collection_replacement_and_independent_loads(self):
        loaded = load_test(self.path)
        self.assertEqual(loaded, {"id": "001", "mock_state": {
            "inbox": [], "docs": {"replacement": "only"}, "sent": [], "new": None}})
        loaded["mock_state"]["sent"].append("side effect")
        self.assertEqual(load_test(self.path)["mock_state"]["sent"], [])
        self.spec["mock_state"] = {}
        self.path.write_text(yaml.safe_dump(self.spec))
        self.assertEqual(load_test(self.path)["mock_state"], json.loads(self.base.read_bytes()))

    def test_changed_or_missing_state_is_rejected(self):
        self.base.write_text("{}")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            load_test(self.path)
        self.base.unlink()
        with self.assertRaises(FileNotFoundError):
            load_test(self.path)

    def test_external_paths_and_symlinks_are_rejected(self):
        for path in ("../state.json", str(self.base.resolve())):
            self.spec["mock_state_base"]["path"] = path
            self.path.write_text(yaml.safe_dump(self.spec))
            with self.assertRaises(ValueError):
                load_test(self.path)
        self.spec["mock_state_base"]["path"] = "state.json"
        self.path.write_text(yaml.safe_dump(self.spec))
        self.base.unlink()
        self.base.symlink_to(self.root.parent / "outside-state.json")
        with self.assertRaises(ValueError):
            load_test(self.path)

    def test_inline_tests_remain_unchanged(self):
        self.spec.pop("mock_state_base")
        self.path.write_text(yaml.safe_dump(self.spec))
        self.assertEqual(load_test(self.path), self.spec)

    def test_reference_runner_uses_the_same_expanded_input(self):
        from harness.run_simulation import load_test as reference_load_test
        with patch.dict(os.environ, {"DOLPHINBENCH_PERSONA": "morgan",
                                    "DOLPHINBENCH_TESTS_DIR": str(self.root)}):
            self.assertEqual(reference_load_test("001"), load_test(self.path))


class PublicImportTests(unittest.TestCase):
    def test_public_runner_and_reference_do_not_import_internal_scripts(self):
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; import harness.runner; import harness.export_submission; import harness.adapters.hermes_reference; "
             "assert 'harness.run_simulation' not in sys.modules; "
             "assert not any(name.split('.')[0] in {'scripts', 'reference', 'construction', 'authoring'} "
             "for name in sys.modules)"],
            cwd=Path(__file__).resolve().parents[3], capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class StarterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.root)

    def test_init_creates_matching_files_without_loading_agent_or_release(self):
        from harness.runner import ROOT
        with patch("harness.runner.load_adapter", side_effect=AssertionError("Loaded agent")), \
                patch("harness.runner.read_release", side_effect=AssertionError("Loaded dataset")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("Network call")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["init"]), 0)
        config = yaml.safe_load(Path("run.yaml").read_text())
        self.assertEqual(config["adapter"], "my_harness:BenchmarkHarness")
        self.assertEqual((self.root / config["release"]).resolve(), ROOT)
        self.assertEqual(config["output"], "tmp/my-agent")
        self.assertEqual(Path("my_harness.py").read_bytes(), (ROOT / "examples/harness_template.py").read_bytes())
        with patch.object(sys, "path", [str(self.root), *sys.path]):
            try:
                adapter = load_adapter(config["adapter"], config["options"], self.root / config["output"])
                self.assertTrue(callable(adapter.run_interaction))
                with self.assertRaisesRegex(NotImplementedError, "Implement identity"):
                    adapter.identity()
            finally:
                sys.modules.pop("my_harness", None)

    def test_init_preserves_existing_files_and_symlinks(self):
        for name in ("run.yaml", "my_harness.py"):
            for symlink in (False, True):
                with self.subTest(name=name, symlink=symlink):
                    path = self.root / name
                    if symlink:
                        path.symlink_to(self.root / "missing")
                    else:
                        path.write_text("existing work")
                    with self.assertRaises(SystemExit), patch("sys.stderr", io.StringIO()):
                        main(["init"])
                    self.assertEqual(list(self.root.iterdir()), [path])
                    if symlink:
                        self.assertTrue(path.is_symlink())
                    else:
                        self.assertEqual(path.read_text(), "existing work")
                    path.unlink()

    def test_init_cleans_up_if_config_cannot_be_created(self):
        with self.assertRaises(SystemExit), patch("sys.stderr", io.StringIO()):
            main(["init", "--config", "missing/run.yaml"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_default_config_and_unimplemented_method_error(self):
        config = {"release": ".", "output": "run", "adapter": "custom:factory", "options": {}}
        Path("run.yaml").write_text(yaml.safe_dump(config))
        adapter = SimpleNamespace(identity=lambda: {"version": "test"}, total_cost_usd=lambda phase: 0)
        with patch("harness.runner.read_release", return_value={}), \
                patch("harness.runner.load_adapter", return_value=adapter), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["prepare"]), 0)
        def missing_identity():
            raise NotImplementedError("Implement identity()")

        adapter.identity = missing_identity
        with patch("harness.runner.read_release", return_value={}), \
                patch("harness.runner.load_adapter", return_value=adapter), patch("sys.stderr", io.StringIO()) as error:
            with self.assertRaises(SystemExit):
                main(["prepare"])
        self.assertIn("Implement identity()", error.getvalue())
        self.assertNotIn("Traceback", error.getvalue())


class DurableRecordTests(unittest.TestCase):
    def test_existing_record_formats_are_preserved(self):
        value = {"label": "\u00e9", "n": 1}
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            compact = root / "nested" / "compact.json"
            durable_json.save_json(compact, value)
            self.assertEqual(compact.read_bytes(), b'{"label": "\\u00e9", "n": 1}')
            pretty = root / "pretty.json"
            durable_json.atomic_json(pretty, value)
            self.assertEqual(pretty.read_bytes(), '{\n  "label": "\u00e9",\n  "n": 1\n}\n'.encode())

    def test_failed_write_preserves_previous_record_and_removes_temporary_files(self):
        for write in (durable_json.save_json, durable_json.atomic_json):
            for operation in ("fsync", "replace"):
                with self.subTest(writer=write.__name__, operation=operation), tempfile.TemporaryDirectory() as raw:
                    path = Path(raw) / "record.json"
                    path.write_bytes(b'{"previous": true}')
                    with patch.object(durable_json.os, operation, side_effect=OSError("failed write")):
                        with self.assertRaisesRegex(OSError, "failed write"):
                            write(path, {"replacement": True})
                    with self.assertRaises(ValueError):
                        write(path, {"invalid": float("nan")})
                    self.assertEqual(path.read_bytes(), b'{"previous": true}')
                    self.assertEqual(list(path.parent.iterdir()), [path])


class RunnerTests(unittest.TestCase):
    def test_cost_failure_resumes_without_repeating_conversations_or_grades(self):
        with patch.object(self.adapter, "total_cost_usd", side_effect=ValueError("Accounting pending")):
            with self.assertRaisesRegex(ValueError, "Accounting pending"):
                self.runner.ingest()
        self.assertEqual(len(self.calls), 3)
        self.assertFalse((self.root / "costs/ingestion.json").exists())
        with patch.object(self.adapter, "total_cost_usd", return_value=12) as cost:
            self.runner.ingest()
            self.runner.ingest()
            cost.assert_called_once_with("ingestion")
        self.assertEqual(len(self.calls), 3)
        with patch.object(self.adapter, "total_cost_usd", side_effect=ValueError("Accounting pending")):
            with self.assertRaisesRegex(ValueError, "Accounting pending"):
                self.runner.evaluate()
        self.assertEqual(len(self.calls), 603)
        with self.assertRaisesRegex(ValueError, "Missing tests cost"):
            self.runner.package(self.root / "incomplete.zip")
        with patch.object(self.adapter, "total_cost_usd", return_value=4) as cost, \
                patch.object(self.runner, "_grade", side_effect=AssertionError("Regraded")):
            self.runner.evaluate()
            self.runner.evaluate()
            cost.assert_called_once_with("tests")
        self.assertEqual(len(self.calls), 603)
        self.runner.adapter = None
        report = self.runner.package(self.root / "complete.zip")
        self.assertEqual(report["total_cost_usd"], 16)

    def test_invalid_cost_is_not_saved(self):
        for value in (None, -1, True, float('nan'), float('inf')):
            with patch.object(self.adapter, "total_cost_usd", return_value=value):
                with self.assertRaises(SubmissionError):
                    self.runner._collect_cost("ingestion")
            self.assertFalse((self.root / "costs/ingestion.json").exists())

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.release, self.ingestion, self.tests = fixture()
        self.network = patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network call"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.calls = []
        parent = self

        class FixtureAdapter(OfflineAdapter):
            def run_interaction(self, request):
                parent.calls.append(request)
                records = parent.ingestion["sessions"] if request.phase == "ingestion" else parent.tests["tests"]
                key = "session_id" if request.phase == "ingestion" else "test_id"
                row = next(r for r in records if r["persona"] == request.persona and r[key] == request.interaction_id)
                messages = copy.deepcopy(row["messages"])
                messages[0]["content"] = request.dated_message
                return InteractionRecord({"model": "fixture-model"}, messages)

        self.adapter = FixtureAdapter({}, self.root)
        self.runner = Runner(self.release, self.root, self.adapter, identity={"fixture": True})

    def test_full_round_trip_resume_and_grade_recovery(self):
        self.runner.ingest()
        self.assertEqual(len(self.calls), 3)
        with patch.object(self.runner, "_grade", side_effect=ValueError("judge interrupted")):
            with self.assertRaisesRegex(ValueError, "judge interrupted"):
                self.runner.evaluate()
        self.assertEqual(len(self.calls), 4)
        self.runner.evaluate()
        self.assertEqual(len(self.calls), 603)
        self.runner.ingest()
        self.runner.evaluate()
        self.assertEqual(len(self.calls), 603)
        output = self.root / "submission.zip"
        summary = self.runner.package(output)
        self.assertEqual(summary["passes"], 600)
        self.assertEqual(validate(*read_zip(output), self.release), summary)
        for request in self.calls:
            self.assertTrue(request.fresh_session)
            self.assertFalse(hasattr(request, "grade"))
            self.assertFalse(hasattr(request, "facts"))

    def test_uncertain_interaction_is_not_replayed(self):
        with patch.object(self.adapter, "run_interaction", side_effect=TimeoutError("unknown delivery")) as call:
            with self.assertRaises(TimeoutError):
                self.runner.ingest()
            with self.assertRaisesRegex(ValueError, "Uncertain interaction"):
                self.runner.ingest()
            self.assertEqual(call.call_count, 1)

    def test_invalid_evidence_is_retained_but_never_replayed(self):
        malformed = InteractionRecord({"model": "fixture"}, [{"role": "assistant", "content": "missing usage"}])
        with patch.object(self.adapter, "run_interaction", return_value=malformed) as call:
            with self.assertRaises(SubmissionError):
                self.runner.ingest()
            self.assertTrue((self.root / "ingestion/morgan/000001.returned").exists())
            with self.assertRaisesRegex(ValueError, "Uncertain interaction"):
                self.runner.ingest()
            self.assertEqual(call.call_count, 1)

    def test_identity_and_lock_reject_changed_or_concurrent_runs(self):
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            Runner(self.release, self.root, self.adapter, identity={"fixture": False})
        changed = copy.deepcopy(self.release)
        changed["morgan"]["sessions"][0]["messages"] = ["changed"]
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            Runner(changed, self.root, self.adapter, identity={"fixture": True})
        with run_lock(self.root):
            with self.assertRaisesRegex(ValueError, "Another runner"):
                with run_lock(self.root):
                    self.fail("Lock was acquired twice")

    def test_evaluation_requires_all_checkpoints(self):
        with self.assertRaisesRegex(ValueError, "Complete ingestion first"):
            self.runner.evaluate()
        self.assertFalse(self.calls)

    def test_changed_judge_configuration_rejected_before_paid_execution(self):
        with patch("graders.llm_judge.AZURE_DEPLOYMENT", "different-judge"):
            with self.assertRaisesRegex(ValueError, "fixed Azure"):
                Runner(self.release, self.root / "paid", self.adapter, identity={}, allow_paid=True)
        self.assertFalse(self.calls)

    def test_app_scope_and_state_match_legacy_semantics(self):
        for scope in (None, [], ["send_email"], [{"name": "send_email"}]):
            spec = {"tools": scope, "mock_state": {"marker": "initial"}}
            apps = self.runner._apps("tests", "morgan", "001", spec)
            env = apps["env"]
            state = Path(env["DOLPHINBENCH_STATE_PATH"])
            self.assertEqual(json.loads(state.read_text()), spec["mock_state"])
            tools = Path(env["DOLPHINBENCH_TOOL_CONFIG_PATH"])
            if scope is None:
                self.assertFalse(tools.exists())
            else:
                self.assertEqual(json.loads(tools.read_text()), {"active_tools": [] if not scope else ["send_email"]})
            state.write_text('{"marker": "mutated"}')
        self.runner._apps("tests", "morgan", "001", spec)
        self.assertEqual(json.loads(state.read_text()), spec["mock_state"])

    def test_published_runtime_uses_exact_isolated_tools_and_directory(self):
        runtime = {"manifest": {"persona": "morgan", "tools": ["send_email"]},
                   "tests": {"001": {"tool_config": {"active_tools": ["send_email"]}, "workspace": "w"}},
                   "workspaces": {"w": {"users": [], "channels": []}}}
        self.release["morgan"]["runtime"] = runtime
        apps = self.runner._apps("tests", "morgan", "001", {"mock_state": {"emails": []}})
        self.assertTrue(apps["args"][0].endswith("mock_mcp/repair_server.py"))
        env = apps["env"]
        self.assertEqual(json.loads(Path(env["DOLPHINBENCH_TOOL_CONFIG_PATH"]).read_text()),
                         runtime["tests"]["001"]["tool_config"])
        self.assertEqual(json.loads(Path(env["DOLPHINBENCH_WORKSPACE_DIRECTORY"]).read_text()),
                         runtime["workspaces"]["w"])
        self.assertEqual(yaml.safe_load(Path(env["DOLPHINBENCH_MOCK_MANIFEST"]).read_text()), runtime["manifest"])
        ingestion = self.runner._apps("ingestion", "morgan", "000001", {"mock_state": {}})
        self.assertTrue(ingestion["args"][0].endswith("mock_mcp/server.py"))
        self.assertNotIn("DOLPHINBENCH_WORKSPACE_DIRECTORY", ingestion["env"])

    def test_grader_receives_original_request_not_dated_agent_input(self):
        self.runner.ingest()
        spec = self.release["morgan"]["tests"][0]
        evidence = self.runner._execute("tests", "morgan", spec)
        from graders.mechanical import grade_tool_trace
        with patch("harness.runner.grade_tool_trace", wraps=grade_tool_trace) as grade:
            self.runner._grade(spec, evidence)
        self.assertEqual(grade.call_args.kwargs["test_message"], spec["test"])

    def test_dynamic_adapter_and_paid_gate_before_import(self):
        adapter = load_adapter("examples.offline_adapter:OfflineAdapter", {}, self.root)
        self.assertIsInstance(adapter, OfflineAdapter)
        config = self.root / "run.yaml"
        config.write_text(yaml.safe_dump({"adapter": "unknown:factory", "release": ".", "output": "out", "options": {}}))
        with patch("harness.runner.load_adapter", side_effect=AssertionError("Imported before approval")):
            with self.assertRaises(SystemExit):
                main(["ingest", "--config", str(config)])

    def test_prepare_constructs_trusted_adapter_without_execution(self):
        config = self.root / "prepare.yaml"
        config.write_text(yaml.safe_dump({"release": ".", "output": "prepared",
            "adapter": "trusted:factory", "options": {}}))
        with patch("harness.runner.read_release", return_value=self.release), \
                patch("harness.runner.load_adapter", return_value=self.adapter), \
                patch.object(self.adapter, "run_interaction", side_effect=AssertionError("Executed")) as run, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["prepare", "--config", str(config)]), 0)
        self.assertEqual(json.loads(output.getvalue())["model_calls"], 0)
        run.assert_not_called()
        self.assertTrue((self.root / "prepared/run.json").exists())

    def test_semantic_grading_without_approval_never_calls_provider(self):
        self.runner.ingest()
        spec = copy.deepcopy(self.release["morgan"]["tests"][0])
        evidence = self.runner._execute("tests", "morgan", spec)
        spec["grade"]["config"]["assertions"] = [{
            "type": "field_llm_judge", "tool": "place_order", "path": "args.restaurant",
            "criterion": "Names Blue Bottle.",
        }]
        with patch("graders.llm_judge._urlopen_json", side_effect=AssertionError("Provider called")) as provider:
            with self.assertRaisesRegex(ValueError, "Missing judge conversation"):
                self.runner._grade(spec, evidence)
        provider.assert_not_called()

    def test_cached_completed_record_with_leftover_marker_is_not_replayed(self):
        self.runner.ingest()
        marker = self.root / "ingestion/morgan/000001.inflight"
        marker.write_text("{}")
        self.runner._execute("ingestion", "morgan", self.release["morgan"]["sessions"][0])
        self.assertEqual(len(self.calls), 3)
        self.assertFalse(marker.exists())

    def test_changed_frozen_checkpoint_stops_all_evaluation(self):
        self.runner.ingest()
        (self.root / "checkpoints/riley.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "checkpoint changed"):
            self.runner.evaluate()
        self.assertEqual(len(self.calls), 3)

    def test_app_scope_matches_existing_runner(self):
        from harness import run_simulation as legacy
        legacy_path = self.root / "legacy-tools.json"
        for scope in (None, [], ["send_email"], [{"name": "send_email"}]):
            with patch.object(legacy, "MOCK_TOOL_CONFIG", legacy_path):
                legacy._write_mock_tool_config(scope)
            apps = self.runner._apps("tests", "morgan", "001", {"tools": scope, "mock_state": {}})
            actual = Path(apps["env"]["DOLPHINBENCH_TOOL_CONFIG_PATH"])
            self.assertEqual(actual.exists(), legacy_path.exists())
            if actual.exists():
                self.assertEqual(json.loads(actual.read_text()), json.loads(legacy_path.read_text()))

    def test_package_does_not_import_or_construct_adapter(self):
        self.runner.ingest()
        self.runner.evaluate()
        self.runner.adapter = None
        self.assertEqual(self.runner.package(self.root / "submission.zip")["passes"], 600)

    def test_three_cli_stages_with_external_factory_and_no_network(self):
        from harness.runner import ROOT
        config = self.root / "cli.yaml"
        output = self.root / "cli-output"
        config.write_text(yaml.safe_dump({"release": str(ROOT), "output": str(output),
            "adapter": "examples.offline_adapter:OfflineAdapter", "options": {}}))
        with patch("harness.runner.read_release", return_value=self.release), \
                patch("harness.runner.os.fsync"), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["ingest", "--config", str(config)]), 0)
            self.assertEqual(main(["evaluate", "--config", str(config)]), 0)
            with patch("harness.runner.load_adapter", side_effect=AssertionError("Packaging loaded an adapter")):
                self.assertEqual(main(["package", "--config", str(config)]), 0)
        self.assertEqual(validate(*read_zip(output / "submission.zip"), self.release)["tests"], 600)


class HermesBoundaryTests(unittest.TestCase):
    def test_preserves_driver_inputs_and_isolates_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = root / "template"
            template.mkdir()
            config = {"model": {"default": "pinned-model"}, "memory": {"provider": "builtin", "flush_min_turns": 6},
                      "toolsets": TOOLSETS, "agent": {"personalities": {"default": "unchanged instructions"}},
                      "mcp_servers": {"dolphinbench-apps": {"command": "old", "args": [],
                                                             "tools": {"resources": False, "prompts": False}}}}
            (template / "config.yaml").write_text(yaml.safe_dump(config))
            binary = root / "hermes"
            binary.touch()
            adapter = HermesAdapter({"profiles": {p: str(template) for p in ("morgan", "alex", "riley")},
                                     "binary": str(binary)}, root / "run")
            apps = {"command": "python", "args": ["server.py"], "env": {"DOLPHINBENCH_PERSONA": "morgan"}}
            result = SimpleNamespace(ok=True, session_id="abc", error=None)
            captured = []
            original_import = builtins.__import__

            def public_import(name, *args, **kwargs):
                if name == "harness.run_simulation" or name.split(".")[0] in {
                    "scripts", "reference", "construction", "authoring",
                }:
                    raise AssertionError(f"Participant execution imported {name}")
                return original_import(name, *args, **kwargs)

            def driver(*args, **kwargs):
                captured.append((args, kwargs, {key: os.environ.get(key) for key in READ_ONLY}))
                result.available_tools = ["mcp_dolphinbench_apps_send_email", "session_search"]
                if args[0] == "morgan":
                    result.available_tools.append("memory")
                recording = adapter.profiles / args[0] / "submission-traces/abc.jsonl"
                recording.parent.mkdir(parents=True, exist_ok=True)
                recording.write_text("{\"fixture\": true}\n")
                import sqlite3
                with sqlite3.connect(adapter.profiles / args[0] / "state.db") as connection:
                    connection.execute("CREATE TABLE IF NOT EXISTS sessions (estimated_cost_usd REAL)")
                    connection.execute("INSERT INTO sessions VALUES (0.25)")
                if args[0] != "morgan":
                    (adapter.profiles / args[0] / "test-only.txt").write_text("isolated change")
                return result

            with patch("harness.adapters.hermes.hermes_driver.run_hermes", side_effect=driver), \
                    patch("builtins.__import__", side_effect=public_import), \
                    patch("harness.adapters.hermes.read_recording", return_value={"settings": {"model": "pinned-model"}, "messages": []}):
                request = Interaction("morgan", "ingestion", "000001", "original message", "2023-01-01", apps, root)
                adapter.run_interaction(request)
                checkpoint = adapter.freeze("morgan")
                adapter.verify_checkpoint("morgan", checkpoint)
                test = Interaction("morgan", "tests", "001", "original test", "2026-09-14", apps, root)
                adapter.run_interaction(test)
                self.assertEqual(adapter.total_cost_usd("ingestion"), .25)
                self.assertEqual(adapter.total_cost_usd("tests"), .25)
                self.assertFalse((adapter.profiles / "morgan-test-001-a01").exists())
                self.assertTrue((adapter.home / "test-traces/morgan/001.jsonl").exists())
                adapter.verify_checkpoint("morgan", checkpoint)
                with self.assertRaisesRegex(ValueError, "refusing to replay"):
                    adapter.run_interaction(test)
            self.assertEqual(captured[0][:2], (("morgan", "original message"),
                {"timeout": None, "model": None, "narrative_time": "2023-01-01"}))
            self.assertEqual(captured[1][:2], (("morgan-test-001-a01", "original test"),
                {"timeout": None, "model": None, "narrative_time": "2026-09-14"}))
            self.assertEqual(set(captured[0][2].values()), {"0"})
            self.assertEqual(set(captured[1][2].values()), {"1"})
            resulting_config = yaml.safe_load((adapter.profiles / "morgan/config.yaml").read_text())
            for key in ("model", "memory", "agent", "toolsets"):
                self.assertEqual(resulting_config[key], config[key])
            self.assertEqual(yaml.safe_load((template / "config.yaml").read_text()), config)


if __name__ == "__main__":
    unittest.main()
