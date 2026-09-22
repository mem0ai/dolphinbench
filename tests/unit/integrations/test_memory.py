"""Local checks for reference memory; external execution is mocked."""

from __future__ import annotations

from email.message import Message
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import http.client
import io
import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
import unittest

import yaml

from reference import artifacts as snapshot
from reference.memory import honcho as verifier, proxy
from reference.memory.hindsight import HindsightApi, IngestionError
from reference.memory.supermemory import IngestionError as SupermemoryIngestionError, PendingDocumentsError, verify_complete
from reference.runtimes import claude_code as native, claude_recovery as recovery


class HindsightReadProxyTests(unittest.TestCase):
    def test_only_original_retrieval_endpoints_on_frozen_bank_are_allowed(self):
        for method, path in (
            ("GET", "/health"), ("GET", "/version"),
            ("POST", "/v1/default/banks/morgan/memories/recall"),
            ("POST", "/v1/default/banks/morgan/reflect"),
        ):
            self.assertTrue(proxy.permits_request(method, path, "morgan"))
        for method, path in (
            ("POST", "/v1/default/banks/other/reflect"),
            ("POST", "/v1/default/banks/morgan/memories"),
            ("POST", "/v1/default/banks/morgan/reflect?bank_id=other"),
            ("DELETE", "/v1/default/banks/morgan"),
            ("PUT", "/v1/default/banks/morgan"),
            ("PATCH", "/v1/default/banks/morgan/config"),
            ("POST", "/v1/default/banks/morgan/../other/reflect"),
            ("GET", "/docs"), ("GET", "http://other/health"),
        ):
            self.assertFalse(proxy.permits_request(method, path, "morgan"))
        self.assertFalse(proxy.permits_request("GET", "/health", ""))

    def test_write_denied_before_upstream_and_read_body_is_unchanged(self):
        # The local server is only a test fixture; no Hindsight process is started.
        server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.ReadOnlyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        real_connection = http.client.HTTPConnection
        try:
            with patch.dict(os.environ, {"HINDSIGHT_API_KEY": "test-key", "DOLPHINBENCH_HINDSIGHT_BANK_ID": "morgan"}),\
                    patch.object(proxy.http.client, "HTTPConnection") as upstream:
                for path, token, expected in (
                    ("/v1/default/banks/morgan/memories", "test-key", 405),
                    ("/v1/default/banks/other/reflect", "test-key", 405),
                    ("/v1/default/banks/morgan/reflect", "wrong", 401),
                ):
                    connection = real_connection("127.0.0.1", server.server_port)
                    connection.request("POST", path, body=b"{}", headers={"Authorization": f"Bearer {token}"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, expected)
                    response.read()
                    connection.close()
                upstream.assert_not_called()
                response = Mock(status=200)
                response.read.return_value = b'{"text":"original answer"}'
                response.getheaders.return_value = [("Content-Type", "application/json")]
                upstream.return_value.getresponse.return_value = response
                body = b'{"query":"original question","budget":"mid"}'
                connection = real_connection("127.0.0.1", server.server_port)
                connection.request("POST", "/v1/default/banks/morgan/reflect", body=body, headers={"Authorization": "Bearer test-key"})
                self.assertEqual(connection.getresponse().read(), response.read.return_value)
                self.assertEqual(upstream.return_value.request.call_args.kwargs["body"], body)
                connection.close()
                for path in (
                    "/v1/default/banks/morgan/knowledge-base/tree",
                    "/v1/default/banks/morgan/knowledge-base/pages/kp-123",
                    "/v1/default/banks/morgan/knowledge-base/search?q=an%20exact%26query&limit=3",
                ):
                    connection = real_connection("127.0.0.1", server.server_port)
                    connection.request("GET", path, headers={"Authorization": "Bearer test-key"})
                    self.assertEqual(connection.getresponse().read(), response.read.return_value)
                    self.assertEqual(upstream.return_value.request.call_args.args, ("GET", path))
                    connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class SupermemoryReadProxyTests(unittest.TestCase):
    def _handler(self, method: str, path: str, authorization: str = ""):
        handler = object.__new__(proxy.ReadOnlyProxyHandler)
        handler.command = method
        handler.path = path
        headers = Message()
        if authorization:
            headers["Authorization"] = authorization
        handler.headers = headers
        handler.responses = []
        handler._reply = lambda status, body: handler.responses.append((status, body))
        return handler

    def test_external_key_must_match(self) -> None:
        with patch.dict(os.environ, {proxy.EXTERNAL_KEY_ENV: "expected"}, clear=True):
            self.assertFalse(self._handler("POST", "/v4/search")._authorized())
            self.assertFalse(self._handler("POST", "/v4/search", "Bearer wrong")._authorized())
            self.assertTrue(self._handler("POST", "/v4/search", "Bearer expected")._authorized())

    def test_only_retrieval_posts_are_allowed(self) -> None:
        handler = self._handler("POST", "/v3/documents")
        handler._forward()
        self.assertEqual(405, handler.responses[0][0])

        handler = self._handler("DELETE", "/v3/documents/example")
        handler._forward()
        self.assertEqual(405, handler.responses[0][0])


class HindsightIngestionTests(unittest.TestCase):
    def test_operation_listing_pages_and_retry_use_supported_paths(self):
        api = HindsightApi("http://example.invalid", "key")
        calls: list[tuple[str, str]] = []

        def request(method: str, path: str):
            calls.append((method, path))
            if method == "POST":
                return {"success": True}
            if "offset=0" in path:
                return {
                    "operations": [{"operation_id": "one"}],
                    "total": 2,
                }
            return {
                "operations": [{"operation_id": "two"}],
                "total": 2,
            }

        api._request = request
        self.assertEqual(
            ["one", "two"],
            [
                operation["operation_id"]
                for operation in api.list_operations(
                    "bank/name", status="failed", operation_type="consolidation",
                    page_size=1,
                )
            ],
        )
        self.assertEqual({"success": True}, api.retry_operation("bank/name", "op/id"))
        self.assertEqual(
            ("POST", "/v1/default/banks/bank%2Fname/operations/op%2Fid/retry"),
            calls[-1],
        )

    def test_operation_listing_fails_after_bounded_inconsistent_reads(self):
        api = HindsightApi(
            "http://example.invalid",
            "key",
            sleep_fn=lambda _seconds: None,
        )
        api._request = lambda _method, _path: {"operations": [], "total": 1}

        with self.assertRaisesRegex(IngestionError, "coherent snapshot after 3 attempts"):
            api.list_operations("bank", status="processing", consistency_attempts=3)


class SupermemoryCompletionTests(unittest.TestCase):
    def verify(self, **changes):
        document = {
            "id": "provider-1", "source_id": "source-1", "status": "done",
            "content_sha256": "a" * 64, "container_tags": ["fixture"],
            **changes,
        }
        store = Mock(spec=["list_documents"])
        store.list_documents.return_value = [document]
        return verify_complete(
            store, container_tag="fixture", expected_ids={"source-1"},
            expected_content_sha256={"source-1": "a" * 64},
        )

    def test_only_completed_matching_content_is_accepted(self):
        result = self.verify()
        self.assertEqual(result["source_ids"], ["source-1"])
        self.assertTrue(result["content_verified"])
        for changes in (
            {"source_id": "other"}, {"container_tags": ["other"]},
            {"content_sha256": "b" * 64},
        ):
            with self.subTest(changes=changes), self.assertRaises(SupermemoryIngestionError):
                self.verify(**changes)

    def test_pending_and_failed_documents_are_not_complete(self):
        for states, error in (
            (("failed", "error", "deleted", "cancelled"), SupermemoryIngestionError),
            (("unknown", "queued", "extracting", "chunking", "embedding", "indexing"), PendingDocumentsError),
        ):
            for state in states:
                with self.subTest(state=state), self.assertRaises(error):
                    self.verify(status=state)


class HonchoLocalVerificationTests(unittest.TestCase):
    def _fixture(self, root: Path) -> dict[str, Path]:
        profile = root / "profiles" / "morgan-honcho"
        profile.mkdir(parents=True)
        identity = {
            "workspace": "workspace-morgan",
            "peerName": "peer-morgan",
            "aiPeer": "hermes",
        }
        (profile / "honcho.json").write_text(json.dumps({
            **identity,
            "hosts": {"hermes": {"enabled": True, **identity}},
        }) + "\n", encoding="utf-8")

        simulation = root / "simulation.yaml"
        simulation.write_text(
            "sessions:\n"
            "  - id: session-1\n"
            "    narrative_date: '2026-08-01T09:00:00-07:00'\n"
            "    messages:\n" + "".join("      - text\n" for _ in range(verifier.EXPECTED_SOURCE_COUNT)),
            encoding="utf-8",
        )
        manifest = root / "seed_manifest.json"
        manifest.write_text(json.dumps({"jobs": [{
            "configuration": "honcho",
            "profile": profile.name,
            "sim_path": str(simulation),
        }]}) + "\n", encoding="utf-8")

        receipts = root / "seed_receipts.jsonl"
        with receipts.open("w", encoding="utf-8") as stream:
            for index in range(verifier.EXPECTED_SOURCE_COUNT):
                stream.write(json.dumps({
                    "provider": "honcho",
                    "receipt_type": "memory_seed_operation",
                    "operation_kind": "automatic_sync_turn",
                    "seed_item_id": f"session-1:{index}",
                    "peer": identity["peerName"],
                    "assistant_peer": identity["aiPeer"],
                    "user_chunk_count": 1,
                    "assistant_chunk_count": 1,
                }) + "\n")
        return {
            "profile": profile,
            "manifest": manifest,
            "receipts": receipts,
            "report": root / "verification.json",
        }

    @staticmethod
    def _evidence(*, failed: int = 1, unresolved: int = 0, mismatched_content: bool = False) -> dict[str, object]:
        contents = ["[2026-08-01T09:00:00-07:00] text"] * verifier.EXPECTED_SOURCE_COUNT
        if mismatched_content:
            contents[-1] = "[2026-08-01T09:00:00-07:00] changed"
        return {
            "workspace_count": 1,
            "profile_peer_count": 1,
            "profile_peer_message_count": verifier.EXPECTED_SOURCE_COUNT,
            "profile_peer_distinct_message_count": verifier.EXPECTED_SOURCE_COUNT,
            "profile_peer_contents": contents,
            "pending_queue_item_count": 0,
            "failed_queue_item_count": failed,
            "superseded_failed_queue_item_count": failed - unresolved,
            "unresolved_failed_queue_item_count": unresolved,
            "last_failed_queue_item_id": 27 if failed else None,
            "last_failed_task_type": "summary" if failed else None,
        }

    def _runner(self, evidence: dict[str, object], calls: list[tuple[list[str], dict[str, object]]]):
        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, json.dumps(evidence) + "\n", "")
        return run

    def test_writes_read_only_report_for_completed_local_seed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            calls: list[tuple[list[str], dict[str, object]]] = []
            with patch("reference.memory.honcho.shutil.which", return_value="/usr/bin/docker"):
                report = verifier.verify_honcho_service(
                    profile=fixture["profile"],
                    seed_manifest=fixture["manifest"],
                    seed_receipts=fixture["receipts"],
                    service_url="http://honcho.tailnet.ts.net:8000",
                    database_container="honcho-database-1",
                    output_path=fixture["report"],
                    runner=self._runner(self._evidence(), calls),
                )
            self.assertEqual(report["workspace"], "workspace-morgan")
            self.assertEqual(report["source_receipts"]["unique_source_id_count"], 3400)
            self.assertEqual(report["messages"]["count"], 3400)
            self.assertTrue(report["processing"]["historical_failures_superseded"])
            self.assertIn("no HTTP request was made", report["service_endpoint"]["evidence"])
            self.assertTrue(report["messages"]["exact_content_match"])
            self.assertEqual(report["messages"]["expected_content_sha256"], report["messages"]["observed_content_sha256"])
            self.assertNotIn("[2026-08-01T09:00:00-07:00] text", json.dumps(report))
            self.assertEqual(len(calls), 1)
            command, kwargs = calls[0]
            self.assertEqual(command[:7], ["docker", "exec", "-i", "honcho-database-1", "psql", "-U", "postgres"])
            self.assertNotIn("--command", command)
            self.assertEqual(command[command.index("--set") + 1], "ON_ERROR_STOP=1")
            self.assertIn("workspace_name=workspace-morgan", command)
            self.assertIn("peer_name=peer-morgan", command)
            self.assertEqual(kwargs["input"].strip().upper()[:4], "WITH")
            query = str(kwargs["input"]).upper()
            self.assertIn("JSON_AGG(MESSAGES.CONTENT ORDER BY MESSAGES.ID)", query)
            self.assertIn("JSONB_EACH", query)
            self.assertIn("SAVED_SUMMARY.VALUE->>'MESSAGE_ID'", query)
            for keyword in ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP"):
                self.assertNotIn(keyword, query)
            self.assertEqual(json.loads(fixture["report"].read_text()), report)

    def test_rejects_mismatched_ordered_message_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            calls: list[tuple[list[str], dict[str, object]]] = []
            with patch("reference.memory.honcho.shutil.which", return_value="/usr/bin/docker"):
                with self.assertRaisesRegex(ValueError, "message content does not match"):
                    verifier.verify_honcho_service(
                        profile=fixture["profile"],
                        seed_manifest=fixture["manifest"],
                        seed_receipts=fixture["receipts"],
                        service_url="http://honcho.tailnet.ts.net:8000",
                        database_container="honcho-database-1",
                        output_path=fixture["report"],
                        runner=self._runner(self._evidence(mismatched_content=True), calls),
                    )
            self.assertEqual(len(calls), 1)


class RecoveryTests(unittest.TestCase):
    def test_mem0_acknowledgment_requires_exact_successful_provider_event(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            transcript = home / "transcript.jsonl"
            transcript.write_text(json.dumps({"uuid": "leaf", "sessionId": "session", "type": "assistant",
                                              "message": {"content": [{"type": "text", "text": "Saved"}]}}))
            database = home / ".claude/plugins/data/mem0-inline/evidence.sqlite3"
            database.parent.mkdir(parents=True)
            stop = {"text": "Saved", "transcript_leaf_uuid": "leaf", "transcript_messages": [
                {"role": "user", "content": "[date] Original"},
                {"role": "assistant", "content": "Main Claude response:\nSaved"}]}
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE flushes(packet_id TEXT,session_id TEXT,status TEXT,semantic_event_id TEXT)")
                connection.execute("CREATE TABLE events(flush_id TEXT,kind TEXT,payload_json TEXT)")
                connection.execute("INSERT INTO flushes VALUES ('packet','session','semantic-succeeded',?)", (json.dumps(["event"]),))
                connection.execute("INSERT INTO events VALUES ('packet','assistant_stop',?)", (json.dumps(stop),))
            config = SimpleNamespace(home=home, plugin_source=home, plugin_settings={"api_key": "key", "namespace": "user"})
            source = SimpleNamespace(timestamp="date", content="Original")
            spec = {"transcript_relative_path": "transcript.jsonl", "transcript_sha256": recovery.file_sha256(transcript),
                    "session_id": "session", "packet_id": "packet", "event_id": "event"}
            inputs = [{"role": "user", "content": "Official extraction input"}]
            remote = {"status": "SUCCEEDED", "payload": {"run_id": "session", "user_id": "user", "messages": inputs}}
            with patch.object(recovery, "mem0_extraction_inputs", return_value=inputs),\
                 patch.object(recovery, "read_json", return_value=remote) as read:
                self.assertEqual(recovery.verify_mem0_completed_turn(config, source, spec)[0]["event_id"], "event")
                for field, wrong in (("run_id", "other"), ("user_id", "other"), ("messages", [])):
                    read.return_value = {**remote, "payload": {**remote["payload"], field: wrong}}
                    with self.subTest(field=field), self.assertRaises(ValueError):
                        recovery.verify_mem0_completed_turn(config, source, spec)
                read.return_value = {**remote, "status": "PENDING"}
                with self.assertRaises(ValueError):
                    recovery.verify_mem0_completed_turn(config, source, spec)
                with self.assertRaises(ValueError):
                    recovery.verify_mem0_completed_turn(config, source, {**spec, "transcript_sha256": "wrong"})
                with self.assertRaises(ValueError):
                    recovery.verify_mem0_completed_turn(config, SimpleNamespace(timestamp="date", content="other"), spec)

    def test_hindsight_acknowledgment_requires_matching_trace_and_database_evidence(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            name = "hindsight_search_knowledge_pages"
            events = [
                {"type": "system", "subtype": "init", "tools": ["Write"], "session_id": "session"},
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "t", "name": name, "input": {}}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "t", "is_error": True,
                     "content": f"<tool_use_error>Error: No such tool available: {name}</tool_use_error>"}]}},
                {"type": "result", "subtype": "success", "is_error": False, "result": "Saved"},
            ]
            trace = root / "trace.json"
            trace.write_text(json.dumps({"source_id": "source", "content_sha256": "content", "result": {
                "stdout": "\n".join(json.dumps(e) for e in events), "response_text": "Saved",
                "error": "Claude used tools outside the allowed MCP surface: " + name}}))
            spec = {"trace_relative_path": "trace.json", "trace_sha256": recovery.file_sha256(trace),
                    "source_id": "source", "content_sha256": "content", "session_id": "session",
                    "generation_files": {"database.archive": "database"},
                    "database_evidence": {"database_sha256": "database", "document_id": "conversation:session",
                                          "completed_operation_ids": ["operation"], "exact_user_and_assistant_verified": True}}
            recovery._verify_hindsight_completed_turn(root, spec)
            for field, value in (("trace_sha256", "other"), ("session_id", "other"), ("source_id", "other")):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    recovery._verify_hindsight_completed_turn(root, {**spec, field: value})
            for field, value in (("database_sha256", "other"), ("completed_operation_ids", []),
                                 ("exact_user_and_assistant_verified", False)):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    recovery._verify_hindsight_completed_turn(root, {**spec, "database_evidence": {**spec["database_evidence"], field: value}})


class ClaudeNativeMemoryTests(unittest.TestCase):
    def test_snapshot_contains_only_native_memory_and_no_credentials_or_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = root / "config"
            memory = native.native_memory_dir(config)
            memory.mkdir(parents=True)
            (memory / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            (memory / "topic.md").write_text("A remembered detail.\n", encoding="utf-8")
            (config / "auth.json").write_text('{"secret":"must not archive"}\n', encoding="utf-8")
            (config / "sessions").mkdir()
            (config / "sessions" / "history.jsonl").write_text("forbidden\n", encoding="utf-8")
            archive = root / "native-memory.tar.gz"
            snapshot = native.create_snapshot(config_dir=config, archive_path=archive)
            self.assertEqual(native.sha256_file(archive), snapshot["sha256"])
            names = native.validate_snapshot(archive)
            self.assertTrue(all("auth.json" not in name for name in names))
            self.assertTrue(all("session" not in name.lower() for name in names))


class CompletedSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        checkpoint = self.root / "checkpoint"
        checkpoint.mkdir()
        (checkpoint / "life_sim.yaml").write_text(yaml.safe_dump({
            "sessions": [{"id": "s1", "messages": ["hello"]}]}))
        self.receipt = {"ingestion_path": "hermes_official_memory_plugin", "provider": "hindsight",
            "persona": "morgan", "identity": "bank", "status": "completed", "source_count": 1,
            "completed_source_ids": ["s1:0"], "corpus_sha256": snapshot._sha(checkpoint / "life_sim.yaml")}
        config = {"memory": {"provider": "hindsight"}, "hindsight": {"bank_id": "bank", "auto_recall": True},
                  "toolsets": ["memory", "session_search", "hindsight", "dolphinbench-apps"]}
        self.files = {"ingestion-receipt.json": json.dumps(self.receipt),
            "hermes-seed-results.json": json.dumps({"phase1_ended_at": "done", "profile": "actual",
                "summary": {"seed_complete": True}, "seed_sessions": [{"id": "s1"}], "seed_calls": [{}]}),
            "hermes-seed-results.hindsight_seed_receipts.jsonl": json.dumps({"seed_item_id": "s1:0",
                "document_id": "s1:0", "bank_id": "bank", "provider": "hindsight", "operation_kind": "automatic_sync_turn"}),
            "hermes-home/profiles/actual/config.yaml": yaml.safe_dump(config),
            "hermes-home/profiles/actual/hindsight/config.json": json.dumps(config["hindsight"]),
            "hermes-home/profiles/actual/auth.json": "secret",
            "hermes-home/profiles/actual/memories/MEMORY.md": "actual ingested memory"}
        self.spec = {"archive": self.root / "snapshot.tar.gz", "checksum": self.root / "snapshot.sha256",
            "receipt": self.root / "receipt.json", "checkpoint_dir": checkpoint,
            "service_url": "https://example.test"}

    def pack(self):
        self.spec["receipt"].write_text(json.dumps(self.receipt))
        with tarfile.open(self.spec["archive"], "w:gz") as archive:
            for name, value in self.files.items():
                data = value.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        self.spec["checksum"].write_text(snapshot._sha(self.spec["archive"]))

    def run_adapter(self):
        with patch.object(snapshot.matrix, "HERMES_HOME", self.root / "hermes"), patch.object(
            snapshot, "_database_check", return_value={"checksums_passed": True}
        ):
            return snapshot.materialize(self.spec, output=self.root / "output", persona="morgan", tests={"ids": ["001"]})

    def test_actual_profile_and_identity_preserved_without_credentials(self):
        self.pack()
        result = self.run_adapter()
        manifest = json.loads(result["manifest"].read_text())
        job = manifest["jobs"][0]
        profile = self.root / "hermes/profiles" / job["profile"]
        self.assertFalse((profile / "auth.json").exists())
        self.assertEqual((profile / "memories/MEMORY.md").read_text(), "actual ingested memory")
        self.assertEqual(job["provider_identity"]["bank_id"], "bank")
        self.assertEqual(yaml.safe_load((profile / "config.yaml").read_text())["hindsight"],
                         {"bank_id": "bank", "auto_recall": True, "api_url": "https://example.test"})

    def test_changed_archive_rejected(self):
        self.pack()
        self.spec["checksum"].write_text("0" * 64)
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.run_adapter()


class CompletedSupermemorySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        checkpoint = self.root / "checkpoint"
        checkpoint.mkdir()
        (checkpoint / "life_sim.yaml").write_text(yaml.safe_dump({
            "sessions": [{"id": "s1", "messages": ["hello"]}]}))
        self.receipt = {
            "ingestion_path": "hermes_official_memory_plugin",
            "provider": "supermemory",
            "persona": "morgan",
            "identity": "final-tag",
            "status": "completed",
            "source_count": 1,
            "completed_source_ids": ["s1:0"],
            "corpus_sha256": snapshot._sha(checkpoint / "life_sim.yaml"),
            "provider_processing": {
                "status": "completed_with_document_failure",
                "pending_documents": 0,
                "approval": "approved",
                "approved_failed_documents": [{"id": "known-failure"}],
            },
        }
        config = {
            "memory": {"provider": "supermemory"},
            "supermemory": {
                "container_tag": "final-tag",
                "base_url": "http://127.0.0.1:6767",
                "auto_recall": True,
                "auto_capture": True,
            },
            "toolsets": ["memory", "session_search", "supermemory", "dolphinbench-apps"],
        }
        self.files = {
            "ingestion-receipt.json": json.dumps(self.receipt),
            "hermes-seed-results.json": json.dumps({
                "phase1_ended_at": "done",
                "profile": "actual",
                "summary": {"seed_complete": True},
                "seed_sessions": [{"id": "s1"}],
                "seed_calls": [{}],
            }),
            "hermes-seed-results.supermemory_seed_receipts.jsonl": json.dumps({
                "seed_item_id": "s1:0",
                "conversation_id": "s1:0",
                "container_tag": "final_tag",
                "provider": "supermemory",
                "operation_kind": "automatic_sync_turn",
                "receipt_type": "memory_seed_operation",
                "hermes_session_id": "session-1",
            }),
            "hermes-home/profiles/actual/config.yaml": yaml.safe_dump(config),
            "hermes-home/profiles/actual/supermemory.json": json.dumps(config["supermemory"]),
            "hermes-home/profiles/actual/auth.json": "secret",
            "hermes-home/profiles/actual/memories/MEMORY.md": "actual ingested memory",
        }
        self.spec = {
            "archive": self.root / "snapshot.tar.gz",
            "checksum": self.root / "snapshot.sha256",
            "receipt": self.root / "receipt.json",
            "checkpoint_dir": checkpoint,
            "service_url": "https://supermemory.example.test",
        }

    def pack(self):
        self.spec["receipt"].write_text(json.dumps(self.receipt))
        with tarfile.open(self.spec["archive"], "w:gz") as archive:
            for name, value in self.files.items():
                data = value.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        self.spec["checksum"].write_text(snapshot._sha(self.spec["archive"]))

    def run_adapter(self):
        with patch.object(snapshot.matrix, "HERMES_HOME", self.root / "hermes"):
            return snapshot.materialize(
                self.spec,
                output=self.root / "output",
                persona="morgan",
                tests={"ids": ["001"]},
            )

    def test_profile_identity_and_read_service_are_preserved(self):
        self.pack()
        result = self.run_adapter()
        manifest = json.loads(result["manifest"].read_text())
        job = manifest["jobs"][0]
        profile = self.root / "hermes/profiles" / job["profile"]
        self.assertFalse((profile / "auth.json").exists())
        self.assertEqual((profile / "memories/MEMORY.md").read_text(), "actual ingested memory")
        self.assertEqual(job["provider_identity"]["container_tag"], "final-tag")
        self.assertEqual(
            yaml.safe_load((profile / "config.yaml").read_text())["supermemory"]["base_url"],
            "https://supermemory.example.test",
        )
        self.assertEqual(
            json.loads((profile / "supermemory.json").read_text())["base_url"],
            "https://supermemory.example.test",
        )

    def test_unfinished_provider_processing_is_rejected(self):
        self.receipt["provider_processing"] = {
            "status": "processing",
            "pending_documents": 1,
        }
        self.files["ingestion-receipt.json"] = json.dumps(self.receipt)
        self.pack()
        with self.assertRaisesRegex(ValueError, "source checkpoint"):
            self.run_adapter()
