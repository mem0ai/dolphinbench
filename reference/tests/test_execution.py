"""Local checks for reference execution; external execution is mocked."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import hashlib
import json
import os
import runpy
import subprocess
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace

from reference import evaluate as matrix
from reference.execution import claude as suite, modal as modal_suite, worker
from reference.execution import images as packaging


class ImageRecipe:
    """Record image operations without building or uploading an image."""
    def __init__(self, operations=()):
        self.operations = operations

    def __getattr__(self, name):
        def record(*args, **kwargs):
            return ImageRecipe((*self.operations, (name, args, kwargs)))
        return record


class WorkerPackagingTests(unittest.TestCase):
    def test_volume_commit_retries_and_propagates_exhaustion(self):
        with patch.object(worker.results_volume, "commit", side_effect=[OSError("transient"), None]) as commit, \
                patch.object(worker.time, "sleep") as sleep:
            worker._commit_results_volume()
            self.assertEqual(commit.call_count, 2)
            sleep.assert_called_once_with(1)
        with patch.object(worker.results_volume, "commit", side_effect=OSError("unavailable")) as commit, \
                patch.object(worker.time, "sleep") as sleep:
            with self.assertRaisesRegex(OSError, "unavailable"):
                worker._commit_results_volume()
            self.assertEqual(commit.call_count, 6)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4, 8, 16])

    def test_checkpoint_pruning_keeps_current_and_fallback_before_saving(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, name in enumerate(("checkpoint-stale", "checkpoint-fallback", "checkpoint-current")):
                path = root / name
                path.mkdir()
                (path / "memory").write_text(name)
                os.utime(path, ns=(index + 1, index + 1))
            (root / "ingestion-checkpoint.json").write_text(json.dumps({"directory": "checkpoint-current"}))
            with patch.object(worker, "_commit_results_volume", side_effect=OSError("storage unavailable")):
                with self.assertRaisesRegex(OSError, "storage unavailable"):
                    worker._save_ingestion_checkpoint({}, root, root / "output")
            self.assertFalse((root / "checkpoint-stale").exists())
            for name in ("checkpoint-fallback", "checkpoint-current"):
                self.assertEqual((root / name / "memory").read_text(), name)
            self.assertEqual(json.loads((root / "ingestion-checkpoint.json").read_text()),
                             {"directory": "checkpoint-current"})

    def test_claude_app_check_uses_the_real_mcp_server_without_hermes(self):
        with patch.object(worker, "BENCH_ROOT", Path(__file__).resolve().parents[2]), \
                patch.object(worker, "MOCK_MCP_PYTHON", Path(sys.executable)):
            result = worker._app_tool_smoke("claude")
        self.assertTrue(result["ok"], result)
        self.assertGreater(result["registered_tool_count"], 0)

    def test_runtime_packages_do_not_require_the_other_installation(self):
        for runtime in ("hermes", "claude"):
            with self.subTest(runtime=runtime), patch.object(worker, "base_image", ImageRecipe()), \
                    patch.object(worker, "hermes_source", return_value=Path("/selected/hermes")) as hermes, \
                    patch.object(worker, "claude_binary", return_value=Path("/selected/claude")) as claude:
                recipe = worker.runtime_image(runtime)
                copies = [(name, args[:2]) for name, args, _ in recipe.operations
                          if name in {"add_local_file", "add_local_dir"}]
                if runtime == "hermes":
                    claude.assert_not_called()
                    hermes.assert_called_once()
                    self.assertEqual(copies, [("add_local_dir", ("/selected/hermes", str(worker.HERMES_ROOT)))])
                else:
                    hermes.assert_not_called()
                    claude.assert_called_once()
                    self.assertEqual(copies, [("add_local_file", ("/selected/claude", "/home/ubuntu/.local/bin/claude"))])

    def test_selected_plugin_is_copied_once(self):
        with patch.object(packaging.modal.Image, "debian_slim", return_value=ImageRecipe()):
            bases = runpy.run_path(packaging.__file__)
        for provider in suite.workers:
            with self.subTest(provider=provider), patch.object(packaging, "claude_binary", return_value=Path("/selected/claude")):
                base = bases["private_network_image"] if provider == "honcho" else bases["base_image"]
                recipe = suite._attach_runtime(base, provider)
                destinations = [args[1] for name, args, _ in recipe.operations
                                if name in {"add_local_dir", "add_local_file"}]
                for candidate, path in packaging._PLUGIN_SOURCES.items():
                    self.assertEqual(destinations.count(str(path)), int(candidate == provider))
                self.assertEqual(destinations.count("/usr/local/bin/claude"), 1)
                self.assertEqual(destinations.count("/opt/dolphinbench/harness"), 1)
                plugin_copy = next(options for name, args, options in recipe.operations
                                   if name == "add_local_dir" and args[1] == str(packaging._PLUGIN_SOURCES[provider]))
                self.assertEqual("**/node_modules/**" in plugin_copy["ignore"],
                                 provider not in {"mem0", "honcho"})
                self.assertEqual(destinations.count(str(packaging.REMOTE_SOURCES / "SOURCE_MANIFEST.md")),
                                 int(provider in {"mem0", "honcho"}))

    def test_remote_worker_import_needs_neither_local_installation(self):
        code = """
from modal_proto import api_pb2
from modal._runtime.user_code_imports import import_single_function_service
from reference.execution import worker
for name in ('run_prepared_job', 'run_private_network_job', 'read_job_status'):
    definition = api_pb2.Function(module_name=worker.__name__, implementation_name=name,
                                  function_name='hermes_' + name, app_name=worker.APP_NAME)
    service = import_single_function_service(definition, None)
    assert service._user_defined_callable is getattr(worker, name)
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                                env={**os.environ, "PATH": "", "DOLPHINBENCH_HERMES_SOURCE": "/missing/hermes",
                                     "DOLPHINBENCH_CLAUDE_BIN": "/missing/claude"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_registration_is_lazy_and_keeps_runtime_functions_separate(self):
        # Use the actual SDK registration, but never enter app.run().
        with patch.object(worker, "runtime_image", return_value=worker.base_image) as image:
            _, functions = worker.create_worker_app()
            image.assert_not_called()
            self.assertEqual(set(functions), {"status"})
            _, functions = worker.create_worker_app(frozenset({"hermes", "claude"}))
            self.assertEqual({call.args[0] for call in image.call_args_list}, {"hermes", "claude"})
            self.assertIsNot(functions["hermes", "run_prepared_job"], functions["claude", "run_prepared_job"])
            self.assertIn(("hermes", "run_ingestion_job"), functions)
            self.assertNotIn(("claude", "run_ingestion_job"), functions)

    def test_manifest_runtime_selects_the_worker_without_changing_job_arguments(self):
        received = []
        bridge = object.__new__(modal_suite._RealModalBridge)
        bridge.openrouter_workers = {}
        def handle(runtime, name):
            def spawn(**kwargs):
                received.append((runtime, name, kwargs))
                return SimpleNamespace(object_id="fixture-call")
            return SimpleNamespace(spawn=spawn)
        bridge.functions = {(runtime, name): handle(runtime, name)
                            for runtime in ("hermes", "claude")
                            for name in ("run_prepared_job", "run_private_network_job", "run_modal_memory_job")}
        arguments = {"bundle_name": "inputs.tar.gz", "job_id": "fixture", "action": "test-only"}
        for runtime in ("hermes", "claude"):
            for provider, name in (("builtin", "run_prepared_job"), ("mem0", "run_prepared_job"),
                                   ("honcho", "run_private_network_job"), ("hindsight", "run_modal_memory_job"),
                                   ("supermemory", "run_modal_memory_job")):
                bridge.spawn(memory_provider=provider, agent_runtime=runtime, **arguments)
                self.assertEqual(received[-1], (runtime, name, arguments))

    def test_installation_inputs_are_checked_locally(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {}, clear=True):
            root = Path(raw)
            with self.assertRaisesRegex(RuntimeError, "DOLPHINBENCH_HERMES_SOURCE"):
                packaging.hermes_source()
            os.environ["DOLPHINBENCH_HERMES_SOURCE"] = str(root)
            with self.assertRaisesRegex(RuntimeError, "pyproject.toml"):
                packaging.hermes_source()
            (root / "pyproject.toml").touch()
            (root / "hermes_cli").mkdir()
            (root / "hermes_cli/main.py").touch()
            self.assertEqual(packaging.hermes_source(), root)
            binary = root / "claude"
            os.environ["DOLPHINBENCH_CLAUDE_BIN"] = str(binary)
            with self.assertRaisesRegex(RuntimeError, "missing or not executable"):
                packaging.claude_binary()
            binary.touch()
            binary.chmod(0o755)
            for version in ("2.1.259", "2.1.260", "12.1.259"):
                with patch.object(packaging.subprocess, "run", return_value=SimpleNamespace(
                    stdout=f"{version} (Claude Code)", returncode=0,
                )) as check:
                    if version == "2.1.259":
                        self.assertEqual(packaging.claude_binary(), binary)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "requires version"):
                            packaging.claude_binary()
                    self.assertEqual(check.call_args.args[0], [str(binary), "--version"])


class FakeModal:
    def __init__(self) -> None:
        self.uploads: list[tuple[Path, str]] = []
        self.profile_uploads: list[tuple[Path, str]] = []
        self.calls: dict[str, dict[str, object]] = {}
        self.spawned: list[dict[str, object]] = []
        self.files: dict[tuple[str, str], bytes] = {}

    def upload_bundle(self, local_path: Path, remote_name: str) -> None:
        self.uploads.append((local_path, remote_name))

    def upload_profile(self, local_path: Path, remote_name: str) -> None:
        self.profile_uploads.append((local_path, remote_name))

    def spawn(self, **kwargs: object) -> str:
        call_id = f"fc-{len(self.spawned) + 1}"
        self.spawned.append(dict(kwargs))
        self.calls[call_id] = {"status": "running", "job_id": kwargs["job_id"]}
        return call_id

    def poll(self, call_id: str, _job_id: str) -> dict[str, object]:
        return dict(self.calls[call_id])

    def read_result_file(self, job_id: str, relative_path: str) -> bytes:
        return self.files[(job_id, relative_path)]


class ModalEvalSuiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        for directory in ("harness", "graders", "mock_mcp", "reference", "construction"):
            path = self.root / directory
            path.mkdir(parents=True)
            (path / "runtime.py").write_text("# runtime\n")
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.suite_path = self.root / "suite.json"
        self.state_path = self.root / "modal.state.json"
        self.bundle_dir = self.root / "bundles"
        self.fake = FakeModal()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _manifest(
        self,
        name: str,
        configuration: str = "builtin",
        test_ids: list[str] | None = None,
    ) -> Path:
        output = self.runs / name
        hashed_input = self.root / "harness" / "runtime.py"
        path = self.root / f"{name}.json"
        path.write_text(json.dumps({
            "manifest_version": matrix.MANIFEST_VERSION,
            "phase": "prepared_offline",
            "root": str(self.root),
            "output_root": str(output),
            "agent_runtime": "hermes",
            "agent": {"provider": "azure-foundry", "model": "gpt-5.6-luna"},
            "judge": {"backend": "azure", "deployment": "gpt-5.6-sol"},
            "evaluation_scope": {
                "kind": "test_only",
                "test_ids": test_ids or [f"{index:03d}" for index in range(1, 6)],
            },
            "jobs": [{"id": f"{name}-matrix", "agent_runtime": "hermes", "configuration": configuration}],
            "hashes": {
                "files": {
                    str(hashed_input): hashlib.sha256(hashed_input.read_bytes()).hexdigest(),
                },
                "source_trees": {},
            },
            "launch": {},
        }))
        return path

    def _write_suite(
        self,
        jobs: int = 3,
        configuration: str = "builtin",
        purpose: str = "integration_smoke",
        test_ids: list[str] | None = None,
    ) -> None:
        test_ids = test_ids or [f"{index:03d}" for index in range(1, 6)]
        entries = []
        for index in range(jobs):
            manifest = self._manifest(
                f"job-{index + 1}",
                configuration=configuration,
                test_ids=test_ids,
            )
            entries.append({
                "id": f"job-{index + 1}",
                "manifest": manifest.name,
                "action": "test-only",
                "resources": [
                    "agent:hermes",
                    f"memory:{configuration}",
                    "provider:azure-foundry",
                    "judge:azure",
                    "azure-luna",
                    "azure-sol-judge",
                ],
            })
        self.suite_path.write_text(json.dumps({
            "suite_version": 1,
            "created_by": "reference.plan",
            "purpose": purpose,
            "test_ids": test_ids,
            "published_test_sha256": {
                test_id: "a" * 64 for test_id in test_ids
            },
            "global_limit": 4,
            "resource_limits": {
                "agent:hermes": 4,
                f"memory:{configuration}": 4,
                "provider:azure-foundry": 4,
                "judge:azure": 4,
                "azure-luna": 3,
                "azure-sol-judge": 4,
            },
            "azure_luna": {"clean_interval_seconds": 60, "retry_backoff_seconds": 5},
            "jobs": entries,
        }))

    def test_rejects_provider_mismatch_before_any_worker_is_spawned(self) -> None:
        self._write_suite(jobs=1, configuration="builtin")
        manifest_path = self.root / "job-1.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["jobs"][0]["configuration"] = "mem0"
        manifest_path.write_text(json.dumps(manifest))

        with self.assertRaisesRegex(
            modal_suite.ModalSuiteError,
            "memory:mem0",
        ):
            modal_suite.launch(
                self.suite_path,
                state_path=self.state_path,
                bundle_dir=self.bundle_dir,
                bridge=self.fake,
            )

        self.assertEqual([], self.fake.spawned)

    def test_launch_builds_one_bundle_and_starts_every_allowed_luna_job(self) -> None:
        self._write_suite()

        state = modal_suite.launch(
            self.suite_path,
            state_path=self.state_path,
            bundle_dir=self.bundle_dir,
            bridge=self.fake,
        )

        self.assertEqual(1, len(self.fake.uploads))
        self.assertEqual(3, len(self.fake.spawned))
        expected_test_ids = [f"{index:03d}" for index in range(1, 6)]
        self.assertTrue(all(
            call["test_ids"] == expected_test_ids
            for call in self.fake.spawned
        ))
        self.assertTrue(all(
            job["test_ids"] == expected_test_ids
            for job in state["jobs"].values()
        ))
        self.assertEqual(32, len(state["launch_id"]))
        remote_job_ids = {
            job["remote_job_id"]
            for job in state["jobs"].values()
        }
        self.assertEqual(3, len(remote_job_ids))
        self.assertTrue(all(
            call["job_id"] == state["jobs"][logical_id]["remote_job_id"]
            for logical_id, call in zip(state["jobs"], self.fake.spawned)
        ))
        self.assertTrue(all(job["status"] == "running" for job in state["jobs"].values()))
        self.assertEqual(3, state["luna"]["current_concurrency"])
        archive = self.fake.uploads[0][0]
        with tarfile.open(archive, "r:gz") as bundle:
            names = set(bundle.getnames())
            rewritten = json.load(bundle.extractfile("workspace/manifests/job-1.json"))
            extracted = Path(self.temporary.name) / "extracted"
            bundle.extractall(extracted)
        self.assertIn("workspace/bundle.json", names)
        self.assertIn("workspace/manifests/job-1.json", names)
        self.assertIn("workspace/repo/harness/runtime.py", names)
        self.assertEqual(
            "/tmp/dolphinbench-job/workspace/repo",
            rewritten["root"],
        )
        self.assertEqual(
            "/tmp/dolphinbench-job/workspace/output",
            rewritten["output_root"],
        )
        extracted_manifest = extracted / "workspace" / "manifests" / "job-1.json"
        remote_root = Path("/tmp/dolphinbench-job")
        local_root = extracted
        portable = json.loads(extracted_manifest.read_text().replace(
            str(remote_root), str(local_root),
        ))
        extracted_manifest.write_text(json.dumps(portable))
        matrix.verify_manifest_hashes(matrix.load_manifest(extracted_manifest))

    def test_resume_never_restarts_a_completed_job(self) -> None:
        self._write_suite()
        state = modal_suite.launch(
            self.suite_path,
            state_path=self.state_path,
            bundle_dir=self.bundle_dir,
            bridge=self.fake,
        )
        first_call = state["jobs"]["job-1"]["modal_call_id"]
        self.fake.calls[first_call] = {
            "status": "completed",
            "job_id": "job-1",
            "returncode": 0,
        }

        modal_suite.launch(
            self.suite_path,
            state_path=self.state_path,
            bundle_dir=self.bundle_dir,
            bridge=self.fake,
            resume=True,
        )
        saved = json.loads(self.state_path.read_text())

        self.assertEqual("completed", saved["jobs"]["job-1"]["status"])
        self.assertEqual(3, len(self.fake.spawned))
        self.assertEqual(
            saved["jobs"]["job-3"]["remote_job_id"],
            self.fake.spawned[-1]["job_id"],
        )

    def test_resume_uses_saved_code_and_only_retries_named_failed_job(self) -> None:
        self._write_suite()
        state = modal_suite.launch(self.suite_path, state_path=self.state_path,
                                  bundle_dir=self.bundle_dir, bridge=self.fake)
        for job_id, entry in state["jobs"].items():
            self.fake.calls[entry["modal_call_id"]] = {
                "status": "failed" if job_id == "job-2" else "completed",
                "returncode": None if job_id == "job-2" else 0,
            }
        (self.root / "harness" / "runtime.py").write_text("# later unrelated development\n")
        resumed = modal_suite.launch(
            self.suite_path, state_path=self.state_path, bundle_dir=self.bundle_dir,
            bridge=self.fake, resume=True, retry_failed_jobs=("job-2",),
        )
        self.assertEqual(4, len(self.fake.spawned))
        self.assertEqual("resume", self.fake.spawned[-1]["action"])
        self.assertEqual(state["jobs"]["job-2"]["remote_job_id"], self.fake.spawned[-1]["job_id"])
        self.assertEqual(state["bundle"], resumed["bundle"])
        self.assertEqual(1, len(self.fake.uploads))
        self.assertEqual("completed", resumed["jobs"]["job-1"]["status"])
        self.assertEqual("completed", resumed["jobs"]["job-3"]["status"])

    def test_resume_rejects_changed_saved_archive(self) -> None:
        self._write_suite(jobs=1)
        state = modal_suite.launch(self.suite_path, state_path=self.state_path,
                                  bundle_dir=self.bundle_dir, bridge=self.fake)
        Path(state["bundle"]["path"]).write_bytes(b"different archive")
        with self.assertRaisesRegex(modal_suite.ModalSuiteError, "archive is missing or changed"):
            modal_suite.launch(self.suite_path, state_path=self.state_path,
                               bundle_dir=self.bundle_dir, bridge=self.fake, resume=True)
        self.assertEqual(1, len(self.fake.spawned))

    def test_download_verifies_the_remote_file_hashes(self) -> None:
        self._write_suite(jobs=1)
        state = modal_suite.launch(
            self.suite_path,
            state_path=self.state_path,
            bundle_dir=self.bundle_dir,
            bridge=self.fake,
        )
        call_id = state["jobs"]["job-1"]["modal_call_id"]
        remote_job_id = state["jobs"]["job-1"]["remote_job_id"]
        content = b'{"score": 1}\n'
        digest = __import__("hashlib").sha256(content).hexdigest()
        self.fake.calls[call_id] = {"status": "completed", "job_id": "job-1", "returncode": 0}
        self.fake.files[(remote_job_id, "metadata.json")] = json.dumps({
            "status": "completed",
            "returncode": 0,
            "files": [{"path": "output/results.json", "bytes": len(content), "sha256": digest}],
        }).encode()
        self.fake.files[(remote_job_id, "output/results.json")] = content

        copied = modal_suite.download(
            self.suite_path,
            state_path=self.state_path,
            destination=self.root / "download",
            bridge=self.fake,
        )

        self.assertEqual(2, len(copied))
        self.assertEqual(content, (self.root / "download" / "job-1" / "output" / "results.json").read_bytes())


class ClaudeLaunchTests(unittest.TestCase):
    def test_worker_deserializes_without_either_launcher_module_name(self):
        from modal._serialization import serialize
        with tempfile.TemporaryDirectory() as raw:
            for provider, worker in suite.workers.items():
                payload = Path(raw) / (provider + ".pickle")
                payload.write_bytes(serialize(worker.get_raw_f()))
                code = """
import importlib.abc, pickle, sys
class NoLauncher(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'modal_claude_ingestion', 'reference.execution.claude'}:
            raise ImportError('The launcher is not installed on a worker')
sys.meta_path.insert(0, NoLauncher())
with open(sys.argv[1], 'rb') as stream:
    worker = pickle.load(stream)
assert worker.__module__ == 'reference.runtimes.claude_history'
"""
                with self.subTest(provider=provider):
                    result = subprocess.run([sys.executable, "-c", code, str(payload)],
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(0, result.returncode, result.stderr)

    def test_partial_launch_preserves_submitted_ids(self):
        recorded = []
        def failed(plan):
            raise RuntimeError("Modal rejected submission")
        functions = {p: (lambda plan: "saved-id") for p in suite.planning.PROVIDERS}
        functions["honcho"] = failed
        with self.assertRaises(RuntimeError):
            suite.submit_all({}, functions, lambda calls: recorded.append(dict(calls)))
        self.assertEqual(set(recorded[-1]), {"builtin", "mem0"})


class ModalEvalWorkerTests(unittest.TestCase):
    def test_completed_job_with_matching_inputs_returns_without_rerunning(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inputs = root / "inputs"
            results = root / "results"
            inputs.mkdir()
            (inputs / "bundle.tar.gz").write_bytes(b"fixture")
            result_root = results / "job"
            result_root.mkdir(parents=True)
            identity = worker._job_input_identity(
                bundle_name="bundle.tar.gz", manifest_relative_path="workspace/manifest.json",
                test_ids=["test-1"], profile_archive_name="profile.tar.gz",
                native_memory_archive_name=None,
            )
            worker._write_json(result_root / "metadata.json", {
                "job_id": "job", "input_identity": identity, "status": "completed",
                "returncode": 0, "error": None, "rate_limited": False,
                "subscription_waiting": False,
            })
            with patch.object(worker, "INPUT_ROOT", inputs),\
                 patch.object(worker, "RESULTS_ROOT", results),\
                 patch.object(worker.subprocess, "Popen", side_effect=AssertionError("must not run")):
                result = worker._run_prepared_job(
                    bundle_name="bundle.tar.gz", job_id="job",
                    manifest_relative_path="workspace/manifest.json", action="resume",
                    test_ids=["test-1"], profile_archive_name="profile.tar.gz",
                    memory_connection="none",
                )
            self.assertEqual({"ok": True, "status": "completed", "returncode": 0}, {
                key: result[key] for key in ("ok", "status", "returncode")
            })

    def test_existing_result_rejects_mismatched_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inputs = root / "inputs"
            results = root / "results"
            inputs.mkdir()
            (inputs / "bundle.tar.gz").write_bytes(b"fixture")
            result_root = results / "job"
            result_root.mkdir(parents=True)
            worker._write_json(result_root / "metadata.json", {
                "job_id": "job", "bundle_name": "bundle.tar.gz",
                "manifest_relative_path": "workspace/manifest.json", "test_ids": ["test-1"],
                "profile_archive_name": None, "native_memory_archive_name": None,
                "action": "test-only", "status": "failed",
            })
            with patch.object(worker, "INPUT_ROOT", inputs),\
                 patch.object(worker, "RESULTS_ROOT", results):
                with self.assertRaisesRegex(RuntimeError, "inputs do not match"):
                    worker._run_prepared_job(
                        bundle_name="bundle.tar.gz", job_id="job",
                        manifest_relative_path="workspace/manifest.json", action="resume",
                        test_ids=["different"], memory_connection="none",
                    )
