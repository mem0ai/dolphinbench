"""Local checks for reference recovery; external execution is mocked."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest

import yaml

from harness.claude_driver import ClaudeResult
from reference.execution import claude_native as native_ingestion, hermes as hermes_ingestion, worker
from reference.memory import services
from reference.runtimes import claude_code as loop, claude_code as native, claude_recovery as gates, claude_recovery as recovery
from reference.runtimes.claude_code import SourceMessage
from reference.runtimes.claude_history import RecoveryStore, RecoveryStoreError, run_history


class RecoveryStoreTests(unittest.TestCase):
    def test_mem0_live_wal_database_restores_without_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home, controller, _, recovery = self._paths(root)
            database = home / RecoveryStore._MEM0_DATABASE
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA wal_autocheckpoint=0")
                connection.execute("CREATE TABLE events (value TEXT)")
                connection.execute("INSERT INTO events VALUES ('saved turn')")
                connection.commit()
                store = RecoveryStore(recovery, lambda: None)
                store.save(home, controller, None)
                store.restore(root / "restored-home", root / "restored-controller", None)
                restored = root / "restored-home" / RecoveryStore._MEM0_DATABASE
                self.assertFalse(Path(str(restored) + "-wal").exists())
                self.assertFalse(Path(str(restored) + "-shm").exists())
                with sqlite3.connect(restored) as saved:
                    self.assertEqual([("saved turn",)], saved.execute("SELECT value FROM events").fetchall())
                    self.assertEqual(("ok",), saved.execute("PRAGMA quick_check").fetchone())
            finally:
                connection.close()

    def _paths(self, root: Path) -> tuple[Path, Path, Path, Path]:
        home = root / "source-home"
        controller = root / "source-controller"
        database = root / "source-database.archive"
        store = root / "recovery"
        home.mkdir()
        controller.mkdir()
        return home, controller, database, store

    def test_save_and_restore_position_home_and_database_together(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home, controller, database, recovery = self._paths(root)
            (home / "memory.txt").write_text("position 17\n")
            (controller / "receipt.json").write_text('{"position": 17}\n')
            database.write_bytes(b"consistent mock database\x00\x01")
            commits: list[str] = []
            store = RecoveryStore(recovery, lambda: commits.append("commit"))
            saved = store.save(home, controller, database)

            restored_home = root / "restored-home"
            restored_controller = root / "restored-controller"
            restored_database = root / "restored-database.archive"
            restored = store.restore(restored_home, restored_controller, restored_database)

            self.assertEqual(["commit", "commit", "commit"], commits)
            self.assertTrue(saved["database_present"])
            self.assertEqual(saved, restored)
            self.assertEqual("position 17\n", (restored_home / "memory.txt").read_text())
            self.assertEqual('{"position": 17}\n', (restored_controller / "receipt.json").read_text())
            self.assertEqual(database.read_bytes(), restored_database.read_bytes())

    def test_failed_save_keeps_the_previous_generation_selected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home, controller, _, recovery = self._paths(root)
            (home / "memory").write_text("first")
            (controller / "receipt").write_text("first")
            calls = 0

            def commit() -> None:
                nonlocal calls
                calls += 1
                if calls == 5:
                    raise OSError("simulated Volume failure")

            store = RecoveryStore(recovery, commit)
            store.save(home, controller, None)
            (home / "memory").write_text("second")
            with self.assertRaisesRegex(OSError, "simulated"):
                store.save(home, controller, None)
            restored_home = root / "restored-home"
            store.restore(restored_home, root / "restored-controller", None)
            self.assertEqual("first", (restored_home / "memory").read_text())

    def test_corruption_is_rejected_without_target_changes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home, controller, _, recovery = self._paths(root)
            (home / "memory").write_text("saved")
            (controller / "receipt").write_text("saved")
            store = RecoveryStore(recovery, lambda: None)
            store.save(home, controller, None)
            current = (recovery / "CURRENT").read_text().strip()
            archive = recovery / "generations" / current / "home.tar.gz"
            archive.write_bytes(b"corrupt")
            target_home = root / "target-home"
            target_controller = root / "target-controller"
            with self.assertRaisesRegex(RecoveryStoreError, "hash mismatch"):
                store.restore(target_home, target_controller, None)
            self.assertFalse(target_home.exists())
            self.assertFalse(target_controller.exists())

    def test_unsafe_tar_path_is_rejected_before_targets_are_created(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home, controller, _, recovery = self._paths(root)
            (home / "memory").write_text("saved")
            (controller / "receipt").write_text("saved")
            store = RecoveryStore(recovery, lambda: None)
            store.save(home, controller, None)
            current = (recovery / "CURRENT").read_text().strip()
            generation = recovery / "generations" / current
            malicious = io.BytesIO()
            with tarfile.open(fileobj=malicious, mode="w:gz") as archive:
                info = tarfile.TarInfo("../escape")
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
            (generation / "home.tar.gz").write_bytes(malicious.getvalue())
            manifest = __import__("json").loads((generation / "manifest.json").read_text())
            manifest["files"]["home.tar.gz"] = store._sha256(generation / "home.tar.gz")
            (generation / "manifest.json").write_text(__import__("json").dumps(manifest))
            with self.assertRaisesRegex(RecoveryStoreError, "unsafe path"):
                store.restore(root / "target-home", root / "target-controller", None)
            self.assertFalse((root / "target-home").exists())


class FakeService:
    def __init__(self, provider):
        self.provider = provider
        self.messages = []
        self.stops = 0
        self.running = False

    def start(self, saved):
        if saved:
            self.messages = json.loads(saved.read_text())
        self.running = True

    def backup(self, destination):
        if self.provider in {"mem0", "honcho"}:
            return None
        if self.provider == "supermemory":
            assert not self.running
        destination.write_text(json.dumps(self.messages))
        return destination

    def stop(self):
        self.running = False
        self.stops += 1


class RuntimeRecoveryTests(unittest.TestCase):
    def config(self, root, provider):
        source = root / "plugin"
        source.mkdir(exist_ok=True)
        return loop.PluginIngestionConfig(
            provider=provider, model="claude-sonnet-5", home=root / "home",
            project=root / "home/project", claude_config_dir=root / "home/.claude",
            plugin_source=source, plugin_identity={"version": "tested"},
            plugin_settings={"namespace": "new-claude-store", "api_url": "http://localhost", "api_key": "key-value"},
            environment={"CLAUDE_CODE_OAUTH_TOKEN": "oauth-value"},
        )

    def messages(self):
        return [SourceMessage(str(i), "2023-01-01T00:00:00Z", text, hashlib.sha256(text.encode()).hexdigest())
                for i, text in enumerate(("first", "second"))]

    def test_restores_matching_database_and_home_then_only_sends_missing_message(self):
        for provider in ("mem0", "honcho", "hindsight", "supermemory"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                config = self.config(root, provider)
                controller = root / "controller/state"
                service = FakeService(provider)
                calls = []
                def invoke(text, **kwargs):
                    calls.append(text)
                    service.messages.append(text)
                    (config.home / "memory").write_text("|".join(service.messages))
                    return ClaudeResult(ok=True, session_id=text, response_text="Saved")
                setup = {"env": {}, "plugin_dirs": [], "settings": {}}
                original = loop.ingest
                def one(**kwargs):
                    kwargs["max_messages"] = 1
                    return original(**kwargs)
                arguments = dict(config=config, messages=self.messages(), controller=controller,
                                 volume_root=root / "volume", commit=lambda: None, run_call=invoke)
                with patch.object(loop, "configure_plugin", return_value=setup), patch.object(loop, "ingest", side_effect=one):
                    run_history(**arguments, service=service)
                self.assertEqual(calls, ["first"])
                self.assertEqual(service.stops, 1)
                shutil.rmtree(config.home)
                shutil.rmtree(controller)
                (controller.parent / "database.backup").unlink(missing_ok=True)
                if provider in {"hindsight", "supermemory"}:
                    service = FakeService(provider)
                with patch.object(loop, "configure_plugin", return_value=setup):
                    result = run_history(**arguments, service=service)
                self.assertEqual(calls, ["first", "second"])
                self.assertEqual(service.messages, ["first", "second"])
                self.assertEqual((config.home / "memory").read_text(), "first|second")
                self.assertEqual(result["completed_messages"], 2)

    def test_ambiguous_hosted_write_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self.config(root, "mem0")
            service = FakeService("mem0")
            controller = root / "controller/state"
            def fail(text, **kwargs):
                service.messages.append(text)
                raise ConnectionError("Response was lost")
            setup = {"env": {}, "plugin_dirs": [], "settings": {}}
            args = dict(config=config, messages=self.messages(), controller=controller,
                        volume_root=root / "volume", commit=lambda: None, service=service)
            with patch.object(loop, "configure_plugin", return_value=setup):
                with self.assertRaises(loop.ClaudePluginIngestionError):
                    run_history(**args, run_call=fail)
            shutil.rmtree(config.home)
            shutil.rmtree(controller)
            with self.assertRaisesRegex(RuntimeError, "may already have reached"):
                run_history(**args, run_call=fail)
            self.assertEqual(service.messages, ["first"])


class IngestionCheckpointTests(unittest.TestCase):
    def test_handoff_stops_only_after_acknowledged_delivery(self) -> None:
        for state, save_fails in (("delivered", False), ("delivered", True), ("ambiguous", False)):
            with self.subTest(state=state, save_fails=save_fails), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                runner = root / "runner.py"
                runner.write_text(
                    "def _atomic_write_json(path, payload): pass\n"
                    "def main():\n"
                    f"    _atomic_write_json('results.json', {{'seed_calls':[{{'seed_delivery_state':{state!r}}}]}})\n"
                    "    print('next source')\n"
                    "    return 0\n"
                )
                code = (
                    "from reference import ingest as b\n"
                    "def acknowledge(*args):\n"
                    + ("    raise RuntimeError('save failed')\n" if save_fails else "    pass\n")
                    + "b.wait_for_checkpoint=acknowledge\n"
                    + f"b.run({str(runner)!r}, {str(root)!r}, handoff_seconds=0)\n"
                )
                result = subprocess.run([sys.executable, "-c", code, "--seed-only"], capture_output=True, text=True)
                self.assertEqual((root / "handoff.json").exists(), state == "delivered" and not save_fails)
                self.assertEqual("next source" in result.stdout, state == "ambiguous")
                self.assertEqual(result.returncode == 0, not save_fails)

    def test_failed_copy_or_commit_preserves_previous_save(self) -> None:
        for failure in ("copy", "commit"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                output = root / "working"
                output.mkdir()
                (output / "progress.json").write_text('{"completed":1}')
                results = root / "results"
                results.mkdir()
                with patch.object(worker.results_volume, "commit"):
                    first = worker._save_ingestion_checkpoint({"jobs": []}, results, output)
                (output / "progress.json").write_text('{"completed":2}')
                if failure == "copy":
                    with patch.object(
                        worker.shutil, "copytree", side_effect=OSError("temporary failure"),
                    ) as failing, patch.object(worker.results_volume, "commit"),\
                         patch.object(worker.time, "sleep"):
                        with self.assertRaises(OSError):
                            worker._save_ingestion_checkpoint({"jobs": []}, results, output)
                else:
                    with patch.object(
                        worker.results_volume, "commit", side_effect=OSError("temporary failure"),
                    ) as failing, patch.object(worker.time, "sleep"):
                        with self.assertRaises(OSError):
                            worker._save_ingestion_checkpoint({"jobs": []}, results, output)
                self.assertEqual(3 if failure == "copy" else 6, failing.call_count)
                self.assertEqual(first, worker._ingestion_resume_root(results))
                self.assertEqual('{"completed":1}', (first / "output/progress.json").read_text())
                self.assertEqual([first], list(results.glob("checkpoint-*")))


def _corpus(path: Path, message_count: int = 3) -> Path:
    sessions = []
    for index in range(message_count):
        sessions.append({
            "id": f"session-{index}",
            "narrative_date": f"2024-01-{index + 1:02d}",
            "messages": [f"message {index}"],
        })
    path.write_text(yaml.safe_dump({"sessions": sessions}, sort_keys=False), encoding="utf-8")
    return path


class ModalMemoryIngestionTests(unittest.TestCase):
    def test_shutdown_failure_preserves_original_error_and_refuses_checkpoint(self) -> None:
        original = RuntimeError("original processing failure")
        shutdown = RuntimeError("shutdown timeout")
        with (patch.object(hermes_ingestion, "_stop_supermemory_service", side_effect=shutdown),
              patch.object(hermes_ingestion, "_record_supermemory_failure", return_value={}) as record):
            clean = hermes_ingestion._preserve_supermemory_failure(
                process=SimpleNamespace(poll=lambda: None), error=original,
                receipt_path=Path("/receipt"), runtime_root=Path("/runtime"),
                commit=lambda: None,
            )
        self.assertFalse(clean)
        self.assertIs(original, record.call_args.kwargs["error"])
        self.assertIs(shutdown, record.call_args.kwargs["shutdown_error"])
        self.assertEqual(None, record.call_args.kwargs["shutdown_diagnostics"]["returncode_before_shutdown"])

    def _final_plan(self, provider: str, corpus: Path) -> hermes_ingestion.IngestionPlan:
        digest = hashlib.sha256(corpus.read_bytes()).hexdigest()
        return hermes_ingestion.IngestionPlan(
            provider=provider,
            mode="final",
            identity=hermes_ingestion.identity_for(provider, "final"),
            corpus_path=corpus.resolve(),
            corpus_sha256=digest,
            source_count=3,
            source_checkpoint="checkpoint",
            snapshot_volume="test-volume",
            archive_path="snapshot.tar.gz",
            checksum_path="snapshot.sha256",
            receipt_path="ingestion-receipt.json",
            bridge_contract_error=None,
        )

    def _hindsight_partial_receipt(self, plan: hermes_ingestion.IngestionPlan) -> dict:
        documents, _ = hermes_ingestion.ingest_hindsight.load_source_documents(
            plan.corpus_path,
            persona="morgan",
            source_checkpoint=plan.source_checkpoint,
        )
        receipt = hermes_ingestion.ingest_hindsight._new_state(
            bank_id=plan.identity.name,
            persona="morgan",
            corpus_path=plan.corpus_path,
            corpus_sha256=plan.corpus_sha256,
            source_checkpoint=plan.source_checkpoint,
            batch_size=2,
            documents=documents,
        )
        receipt["status"] = "paused"
        receipt["batches"][0]["status"] = "completed"
        receipt["batches"][0]["completed_document_ids"] = list(receipt["batches"][0]["document_ids"])
        return receipt

    def test_final_checkpoint_persists_a_completed_group_and_restores_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            corpus = _corpus(root / "life_sim.yaml")
            plan = self._final_plan("hindsight", corpus)
            snapshot = services.ProviderSnapshot("hindsight", "0.9.2", "test", root / "volume")
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            (runtime / "state").write_text("after first group", encoding="utf-8")
            receipt_path = root / "receipt.json"
            receipt_path.write_text(json.dumps(self._hindsight_partial_receipt(plan)), encoding="utf-8")

            with patch.object(hermes_ingestion, "_provider_snapshot", return_value=snapshot):
                saved = hermes_ingestion.save_in_progress_checkpoint(
                    provider="hindsight",
                    runtime_root=runtime,
                    receipt_path=receipt_path,
                    plan=plan,
                )
                self.assertTrue((snapshot.volume_root / "in-progress" / "current.json").is_file())
                self.assertTrue(Path(saved["path"]).is_dir())
                (runtime / "state").write_text("wrong", encoding="utf-8")
                receipt_path.unlink()
                self.assertTrue(hermes_ingestion.restore_in_progress_checkpoint(
                    provider="hindsight",
                    runtime_root=runtime,
                    receipt_path=receipt_path,
                    plan=plan,
                ))

            self.assertEqual("after first group", (runtime / "state").read_text(encoding="utf-8"))
            self.assertEqual("paused", json.loads(receipt_path.read_text(encoding="utf-8"))["status"])


ROOT = Path(__file__).resolve().parents[3]


CORPUS = ROOT / "registry/personas/morgan/life_sim.yaml"


class ModalClaudeNativeIngestionTests(unittest.TestCase):
    def test_approved_native_retry_preserves_memory_without_fabricating_completion(self) -> None:
        import json
        from reference.runtimes.claude_recovery import file_sha256
        from reference.runtimes.claude_recovery import tree_sha256
        plan = native_ingestion.build_plan(corpus_path=CORPUS)
        source = native.load_morgan_source_messages(CORPUS)[0][0]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = native_ingestion.VolumePaths(root / "volume")
            project = root / "project"
            with patch.object(native, "FIXED_PROJECT_DIR", str(project)):
                native.initialize_receipt(corpus_path=CORPUS, receipt_path=paths.receipt,
                                          config_dir=root / "original", project_dir=str(project), model=plan.model)
                paths.memory.mkdir(parents=True)
                (paths.memory / "MEMORY.md").write_text("Original memory")
                native_ingestion._checkpoint_before_call(paths=paths, source=source, commit=lambda: None)
                spec = {"provider": "builtin", "action": "retry", "completed_count": 0,
                        "source_id": source.source_id, "content_sha256": source.content_sha256,
                        "receipt_sha256": file_sha256(paths.receipt), "marker_sha256": file_sha256(paths.inflight),
                        "memory_sha256": tree_sha256(paths.memory)}
                commits = []
                config, receipt, done = native_ingestion._restore_or_initialize(
                    plan=plan, paths=paths, runtime_root=root / "restored", recovery=spec,
                    commit=lambda: commits.append(True),
                )
                self.assertFalse(done)
                self.assertEqual({}, json.loads(receipt.read_text())["completed"])
                self.assertEqual("Original memory", (native.native_memory_dir(config) / "MEMORY.md").read_text())
                self.assertEqual(1, len(json.loads(receipt.read_text())["recovery_adjudications"]))
                self.assertEqual(2, len(commits))
                self.assertFalse(paths.inflight.exists())
                native_ingestion._checkpoint_before_call(paths=paths, source=source, commit=lambda: None)
                with self.assertRaisesRegex(ValueError, "receipt differs"):
                    native_ingestion._restore_or_initialize(plan=plan, paths=paths, runtime_root=root / "another", recovery=spec)
                native_ingestion._checkpoint_after_call(
                    plan=plan, paths=paths, config_dir=config, receipt_path=receipt,
                    source=source, telemetry=native_ingestion._safe_attempt_telemetry(
                        source=source, result=ClaudeResult(ok=True, session_id="recovered", response_text="Done"),
                        elapsed_seconds=1, outcome="completed",
                    ), commit=lambda: None,
                )
                _, restored, _ = native_ingestion._restore_or_initialize(
                    plan=plan, paths=paths, runtime_root=root / "after-completion", recovery=spec,
                )
                self.assertIn(source.source_id, json.loads(restored.read_text())["completed"])
                next_source = native.load_morgan_source_messages(CORPUS)[0][1]
                native_ingestion._checkpoint_before_call(paths=paths, source=next_source, commit=lambda: None)
                with self.assertRaises(native_ingestion.NativeSeedIngestionError):
                    native_ingestion._restore_or_initialize(
                        plan=plan, paths=paths, runtime_root=root / "new-interruption", recovery=spec,
                    )


class RecoveryTests(unittest.TestCase):
    def test_mem0_reconciles_proven_writes_without_sending_them(self):
        for remote_ok in (True, False):
            with self.subTest(remote_ok=remote_ok), tempfile.TemporaryDirectory() as raw:
                home = Path(raw)
                db = home / ".claude/plugins/data/mem0-inline/evidence.sqlite3"
                db.parent.mkdir(parents=True)
                inputs = [{"role": "user", "content": "Original"},
                          {"role": "assistant", "content": "Saved reply"}]
                digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
                with sqlite3.connect(db) as connection:
                    connection.execute("CREATE TABLE flushes(packet_id TEXT, session_id TEXT, status TEXT, semantic_event_id TEXT, error TEXT)")
                    connection.execute("CREATE TABLE events(flush_id TEXT, kind TEXT, payload_json TEXT)")
                    connection.executemany("INSERT INTO flushes VALUES (?,?,?,?,?)", [
                        ("queued", "session-a", "semantic-queued", '["event"]', None),
                        ("prepared", "session-b", "prepared", None, None),
                    ])
                    connection.executemany("INSERT INTO events VALUES (?,?,?)", [
                        (key, "assistant_stop", json.dumps({"transcript_messages": inputs}))
                        for key in ("queued", "prepared")
                    ])
                spec = {"namespace": "user", "flushes": {
                    "queued": {"session_id": "session-a", "status": "semantic-queued",
                               "semantic_event_id": '["event"]', "event_id": "event", "input_sha256": digest},
                    "prepared": {"session_id": "session-b", "status": "prepared",
                                 "semantic_event_id": None, "memory_id": "memory", "input_sha256": digest},
                }}

                def read(url, **kwargs):
                    if "/event/" in url:
                        return {"status": "SUCCEEDED", "payload": {
                            "run_id": "session-a", "user_id": "user", "messages": inputs}}
                    return [{"event": "ADD", "session_id": "session-b", "user_id": "user",
                             "input": inputs if remote_ok else []}]

                with patch.object(recovery, "read_json", side_effect=read) as reads:
                    if remote_ok:
                        self.assertEqual(2, len(recovery.reconcile_mem0(home, spec, "secret")))
                    else:
                        with self.assertRaisesRegex(ValueError, "stored input"):
                            recovery.reconcile_mem0(home, spec, "secret")
                    self.assertEqual(2, reads.call_count)
                with sqlite3.connect(db) as connection:
                    statuses = [row[0] for row in connection.execute("SELECT status FROM flushes")]
                self.assertEqual(["semantic-succeeded"] * 2 if remote_ok else ["semantic-queued", "prepared"], statuses)


class HindsightGateTests(unittest.TestCase):
    def spec(self):
        return {"session_id": "session", "database_evidence": {
            "bank_id": "bank", "document_ids": ["conversation:old"], "failed_operation_ids": ["failed"],
        }}

    def state(self):
        return {"bank_id": "bank", "document_ids": ["conversation:old"], "target_document": None,
                "target_operations": [], "noncompleted_operations": [{
                    "operation_id": "failed", "operation_type": "consolidation", "status": "failed",
                    "error_message": 'duplicate key value violates unique constraint "observation_history_pkey"',
                }]}

    def test_recovered_document_requires_exact_once_prompt_answer_and_completed_extraction(self):
        source = SourceMessage("id", "2023-01-01", "original", "hash")
        turns = [{"role": "user", "content": "[2023-01-01] original"},
                 {"role": "assistant", "content": "quota notice"}, {"role": "assistant", "content": "answer"}]
        state = {**self.state(), "noncompleted_operations": [],
                 "document_ids": ["conversation:old", "conversation:session"],
                 "target_document": {"id": "conversation:session", "original_text": "\n".join(map(json.dumps, turns))},
                 "target_operations": [{"operation_id": "retain", "status": "completed"}]}
        result = {"response_text": "answer", "session_id": "session"}
        self.assertTrue(gates.validate_recovered_document(state, source, result, self.spec())["verified"])
        for key, value in (("target_document", None), ("target_operations", []),
                           ("document_ids", ["conversation:session"]), ("noncompleted_operations", [{}])):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                gates.validate_recovered_document({**state, key: value}, source, result, self.spec())
        for bad_turns in (turns + turns[:1], turns[:2], turns[1:]):
            with self.assertRaises(RuntimeError):
                gates.validate_recovered_document({**state, "target_document": {
                    "id": "conversation:session", "original_text": "\n".join(map(json.dumps, bad_turns))}},
                    source, result, self.spec())
