"""Recovery and provider completion checks for interrupted Claude ingestion."""

from __future__ import annotations

import hashlib
import fnmatch
import json
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Mapping
import http.client
import os
import time
import urllib.error
import urllib.parse
import asyncio
from typing import Any


HONCHO_CONTINUATION = (
    "<system-reminder>Continue the interrupted response to the last user request."
    "</system-reminder>"
)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Recovery memory tree must not contain symlinks")
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode() + b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def validate_interrupted_source(
    *, receipt_path: Path, marker_path: Path, messages: list,
    recovery: Mapping[str, Any],
) -> dict[str, Any]:
    if file_sha256(receipt_path) != recovery["receipt_sha256"]:
        raise ValueError("Recovery receipt differs from the inspected checkpoint")
    if file_sha256(marker_path) != recovery["marker_sha256"]:
        raise ValueError("Recovery marker differs from the inspected interruption")
    receipt = json.loads(receipt_path.read_text())
    marker = json.loads(marker_path.read_text())
    completed = receipt["completed"]
    if len(completed) != recovery["completed_count"]:
        raise ValueError("Recovery completed count differs from the inspected checkpoint")
    pending = [source for source in messages if source.source_id not in completed]
    if not pending:
        raise ValueError("Recovery has no pending source")
    source = pending[0]
    expected = {"source_id": source.source_id, "content_sha256": source.content_sha256}
    if marker != expected or any(recovery.get(key) != value for key, value in expected.items()):
        raise ValueError("Recovery must address exactly the next source and original content")
    return {"adjudication": dict(recovery), "original_marker": marker}


def recovery_completed(receipt: Mapping[str, Any], record: Mapping[str, Any],
                       recovery: Mapping[str, Any]) -> bool:
    """Recognize the same applied recovery only after its source completed."""
    source_id = recovery.get("source_id")
    expected = recovery.get("content_sha256")
    return bool(
        source_id and expected
        and record.get("adjudication") == dict(recovery)
        and record.get("original_marker") == {"source_id": source_id, "content_sha256": expected}
        and receipt.get("completed", {}).get(source_id, {}).get("content_sha256") == expected
    )


def read_json(url: str, *, key: str = "") -> Any:
    headers = {"Authorization": f"Token {key}"} if key else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        return json.load(response)


def mem0_extraction_inputs(home: Path, plugin_source: Path, packet_id: str) -> list[dict]:
    """Rebuild the exact payload using the installed plugin's pure builders."""
    code = r'''
import json,sqlite3,sys
sys.path.insert(0,sys.argv[1])
import memory_core as core
with sqlite3.connect("file:"+sys.argv[2]+"?mode=ro",uri=True) as connection:
 connection.row_factory=sqlite3.Row
 flush=connection.execute("SELECT * FROM flushes WHERE packet_id=?",(sys.argv[3],)).fetchone()
 scope=connection.execute("SELECT * FROM session_scopes WHERE session_id=?",(flush["session_id"],)).fetchone()
 repo=core.RepoContext(cwd=scope["root"],root=scope["root"],identity=scope["repo_id"],app_id=scope["app_id"],branch=scope["branch"],head_sha=scope["head_sha"],directory=scope["directory"])
 events=[{"id":row["id"],"created_at":row["created_at"],"kind":row["kind"],"payload":json.loads(row["payload_json"])} for row in connection.execute("SELECT * FROM events WHERE flush_id=? ORDER BY id",(sys.argv[3],))]
 _,structured=core.build_episode(repo,flush["session_id"],flush["packet_id"],events)
 batches=[batch for batch in core.extraction_message_batches(core.build_extraction_messages(structured)) if batch]
 if len(batches)!=1: raise ValueError("Recovery requires one inspected extraction batch")
 print(json.dumps(batches[0]))
'''
    database = home / ".claude/plugins/data/mem0-inline/evidence.sqlite3"
    result = subprocess.run([sys.executable, "-c", code, str(plugin_source / "core"), str(database), packet_id],
                            text=True, capture_output=True, check=True, timeout=30)
    return json.loads(result.stdout)


def reconcile_mem0(home: Path, recovery: Mapping[str, Any], key: str,
                   plugin_source: Path | None = None) -> list[dict]:
    """Reconcile only locally stale ledger rows whose remote writes are proven."""
    database = home / ".claude/plugins/data/mem0-inline/evidence.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Saved Mem0 SQLite state failed integrity_check")
        rows = {row["packet_id"]: dict(row) for row in connection.execute(
            "SELECT * FROM flushes WHERE status != 'semantic-succeeded'"
        )}
        if set(rows) != set(recovery["flushes"]):
            raise ValueError("Unresolved Mem0 flushes differ from the inspected ledger")
        if connection.execute("SELECT COUNT(*) FROM events WHERE flush_id IS NULL").fetchone()[0]:
            raise ValueError("Saved Mem0 state contains unreviewed unflushed events")
        evidence = []
        for packet_id, expected in recovery["flushes"].items():
            row = rows[packet_id]
            if any(row.get(k) != expected[k] for k in ("session_id", "status", "semantic_event_id")):
                raise ValueError("Mem0 flush row differs from its adjudication")
            input_format = expected.get("input_format", "transcript")
            if input_format == "official-plugin" and plugin_source is not None:
                original_inputs = mem0_extraction_inputs(home, plugin_source, packet_id)
            elif input_format == "transcript":
                stops = connection.execute(
                    "SELECT payload_json FROM events WHERE flush_id=? AND kind='assistant_stop'",
                    (packet_id,),
                ).fetchall()
                if len(stops) != 1:
                    raise ValueError("Mem0 recovery requires the inspected single completed exchange")
                original_inputs = json.loads(stops[0][0])["transcript_messages"]
            else:
                raise ValueError("Unsupported Mem0 recovery input format")
            original_hash = hashlib.sha256(json.dumps(original_inputs, sort_keys=True).encode()).hexdigest()
            if original_hash != expected["input_sha256"]:
                raise ValueError("Mem0 local input differs from the inspected original turn")
            if expected.get("event_id"):
                data = read_json(f"https://api.mem0.ai/v1/event/{expected['event_id']}/", key=key)
                if data.get("status") != "SUCCEEDED":
                    raise ValueError("Mem0 extraction is not confirmed successful")
                payload = data["payload"]
                if payload.get("run_id") != row["session_id"] or payload.get("user_id") != recovery["namespace"]:
                    raise ValueError("Mem0 event belongs to a different session or user")
                inputs = payload["messages"]
            else:
                memory_id = expected["memory_id"]
                data = read_json(f"https://api.mem0.ai/v1/memories/{memory_id}/history/", key=key)
                matches = [item for item in data if item.get("event") == "ADD"
                           and item.get("session_id") == row["session_id"]
                           and item.get("user_id") == recovery["namespace"]]
                if len(matches) != 1:
                    raise ValueError("Mem0 memory history does not prove the inspected write")
                inputs = matches[0]["input"]
            input_hash = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
            if input_hash != expected["input_sha256"]:
                raise ValueError("Mem0 stored input differs from the inspected original turn")
            evidence.append({"packet_id": packet_id, "input_sha256": input_hash,
                             "event_id": expected.get("event_id"), "memory_id": expected.get("memory_id")})
        for packet_id in rows:
            connection.execute("UPDATE flushes SET status='semantic-succeeded', error='' WHERE packet_id=?", (packet_id,))
    return evidence


def verify_mem0_completed_turn(config: Any, source: Any, recovery: Mapping[str, Any]) -> list[dict]:
    """Acknowledge an approved completed turn without repeating any provider write."""
    transcript = config.home / recovery["transcript_relative_path"]
    if file_sha256(transcript) != recovery["transcript_sha256"]:
        raise ValueError("Mem0 transcript differs from the inspected turn")
    database = config.home / ".claude/plugins/data/mem0-inline/evidence.sqlite3"
    with sqlite3.connect("file:" + str(database) + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Mem0 ledger failed integrity_check")
        if connection.execute("SELECT COUNT(*) FROM flushes WHERE status != 'semantic-succeeded'").fetchone()[0]:
            raise ValueError("Mem0 has unresolved flushes")
        if connection.execute("SELECT COUNT(*) FROM events WHERE flush_id IS NULL").fetchone()[0]:
            raise ValueError("Mem0 has unflushed events")
        rows = connection.execute("SELECT * FROM flushes WHERE session_id=?", (recovery["session_id"],)).fetchall()
        if len(rows) != 1 or rows[0]["packet_id"] != recovery["packet_id"]:
            raise ValueError("Mem0 session does not match the inspected flush")
        stops = connection.execute("SELECT payload_json FROM events WHERE flush_id=? AND kind='assistant_stop'",
                                   (recovery["packet_id"],)).fetchall()
        if len(stops) != 1:
            raise ValueError("Mem0 requires one saved final response")
        stop = json.loads(stops[0][0])
        exchange = stop["transcript_messages"]
        if (len(exchange) != 2 or exchange[0] != {"role": "user", "content": f"[{source.timestamp}] {source.content}"}
                or not stop["text"].strip() or exchange[1] != {"role": "assistant", "content": "Main Claude response:\n" + stop["text"]}):
            raise ValueError("Mem0 exchange differs from the original source and final answer")
        events = [json.loads(line) for line in transcript.read_text().splitlines() if line.strip()]
        final = next((event for event in events if event.get("uuid") == stop["transcript_leaf_uuid"]), {})
        text = "".join(part.get("text", "") for part in final.get("message", {}).get("content", []) if part.get("type") == "text")
        if final.get("sessionId") != recovery["session_id"] or final.get("type") != "assistant" or text != stop["text"]:
            raise ValueError("Mem0 final answer does not match the saved Claude transcript")
        event_ids = json.loads(rows[0]["semantic_event_id"])
        if event_ids != [recovery["event_id"]]:
            raise ValueError("Mem0 event differs from the inspected successful write")
    inputs = mem0_extraction_inputs(config.home, config.plugin_source, recovery["packet_id"])
    remote = read_json(f"https://api.mem0.ai/v1/event/{recovery['event_id']}/", key=config.plugin_settings["api_key"])
    payload = remote.get("payload", {})
    if (remote.get("status") != "SUCCEEDED" or payload.get("run_id") != recovery["session_id"]
            or payload.get("user_id") != config.plugin_settings["namespace"] or payload.get("messages") != inputs):
        raise ValueError("Mem0 provider event does not prove the exact completed write")
    return [{"event_id": recovery["event_id"], "packet_id": recovery["packet_id"],
             "input_sha256": hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()}]


def prepare_plugin_recovery(
    *, config: Any, messages: list, controller: Path, volume_root: Path,
    restored: Mapping[str, Any] | None, recovery: Mapping[str, Any], commit: Any,
) -> bool:
    provider = config.provider
    actions = {"hindsight": {"retry", "acknowledge", "continue", "finish"}, "honcho": {"continue"}, "mem0": {"reconcile", "continue", "acknowledge", "retry"}, "supermemory": {"continue"}}
    if recovery.get("provider") != provider or recovery.get("action") not in actions.get(provider, set()):
        raise ValueError("Unsupported provider recovery adjudication")
    if provider == "mem0" and recovery["action"] == "retry" and recovery.get("approved_duplicate_write_risk") is not True:
        raise ValueError("Mem0 retry requires explicit duplicate-write risk approval")
    receipt = controller / "receipt.json"
    applied = controller / "recovery-adjudication.json"
    if applied.exists() and recovery_completed(
        json.loads(receipt.read_text()), json.loads(applied.read_text()), recovery,
    ):
        return False
    if not restored or restored["generation"] != recovery["generation"] or restored["files"] != recovery["generation_files"]:
        raise ValueError("Recovery generation differs from the inspected snapshot")
    marker = controller / "in-flight.json"
    if provider == "mem0" and recovery["action"] == "reconcile":
        if file_sha256(receipt) != recovery["receipt_sha256"] or file_sha256(marker) != recovery["marker_sha256"]:
            raise ValueError("Mem0 checkpoint differs from the inspected successful turn")
        record = {"adjudication": dict(recovery), "original_marker": json.loads(marker.read_text())}
    else:
        record = validate_interrupted_source(receipt_path=receipt, marker_path=marker,
                                             messages=messages, recovery=recovery)
    if recovery["action"] in {"continue", "finish"}:
        transcript = config.home / recovery["transcript_relative_path"]
        if file_sha256(transcript) != recovery["transcript_sha256"]:
            raise ValueError("Interrupted transcript differs from the inspected session")
        if provider == "hindsight":
            from reference.runtimes.claude_recovery import validate_hindsight_continuation as validate_continuation
            source = next(source for source in messages if source.source_id == recovery["source_id"])
            validate_continuation(config, controller, source, recovery)
        elif provider == "supermemory":
            from reference.runtimes.claude_recovery import validate_supermemory_continuation as validate_continuation
            source = next(source for source in messages if source.source_id == recovery["source_id"])
            validate_continuation(config, controller, source, recovery)
    if provider == "honcho":
        source = next(source for source in messages if source.source_id == recovery["source_id"])
        expected_text = f"[{source.timestamp}] {source.content}"
        remote = read_json(config.plugin_settings["api_url"].rstrip("/") + recovery["message_api_path"])
        if (remote.get("content") != expected_text
                or remote.get("metadata", {}).get("instance_id") != recovery["session_id"]):
            raise ValueError("Honcho stored prompt does not match the interrupted Claude session")
    if provider == "mem0":
        if recovery["action"] == "acknowledge":
            source = next(source for source in messages if source.source_id == recovery["source_id"])
            record["verified_writes"] = verify_mem0_completed_turn(config, source, recovery)
        else:
            record["verified_writes"] = reconcile_mem0(config.home, recovery, config.plugin_settings["api_key"], config.plugin_source)
    if recovery["action"] == "acknowledge" and provider == "hindsight":
        _verify_hindsight_completed_turn(controller, recovery)
    evidence_dir = volume_root / "recovery-evidence" / recovery["generation"]
    claim = evidence_dir / "claim.json"
    if claim.exists():
        raise ValueError("This recovery was already attempted; inspect it before another retry")
    shutil.copytree(volume_root / "saved/generations" / recovery["generation"], evidence_dir, dirs_exist_ok=True)
    claim.write_text(json.dumps(record, indent=2) + "\n")
    commit()
    (controller / "recovery-adjudication.json").write_text(json.dumps(record, indent=2) + "\n")
    if recovery["action"] == "acknowledge":
        from reference.runtimes.claude_code import _atomic_json
        state = json.loads(receipt.read_text())
        state["completed"][recovery["source_id"]] = {"content_sha256": recovery["content_sha256"]}
        if len(state["completed"]) == len(messages):
            state["status"] = "completed"
        _atomic_json(receipt, state)
    if provider != "mem0" or recovery["action"] in {"continue", "acknowledge", "retry"}:
        marker.unlink()
    return True


def _verify_hindsight_completed_turn(controller: Path, recovery: Mapping[str, Any]) -> None:
    """Recheck the saved trace against an explicitly inspected database backup."""
    from harness.claude_driver import _parse_trace, _rejected_unavailable_tools
    trace = controller / recovery["trace_relative_path"]
    if file_sha256(trace) != recovery["trace_sha256"]:
        raise ValueError("Hindsight completed trace differs from the inspected turn")
    evidence = recovery["database_evidence"]
    if (evidence["database_sha256"] != recovery["generation_files"]["database.archive"]
            or evidence["document_id"] != "conversation:" + recovery["session_id"]
            or not evidence["completed_operation_ids"]
            or evidence["exact_user_and_assistant_verified"] is not True):
        raise ValueError("Hindsight database evidence is not bound to this completed turn")
    saved = json.loads(trace.read_text())
    if any(saved.get(key) != recovery[key] for key in ("source_id", "content_sha256")):
        raise ValueError("Hindsight trace belongs to a different source")
    result = saved["result"]
    session, response, calls, _, _, error, complete = _parse_trace(result["stdout"])
    rejected = _rejected_unavailable_tools(result["stdout"])
    allowed = {"Read", "Write", "Edit", "Bash", "Skill", "ToolSearch"}
    if (session != recovery["session_id"] or not complete or error or not response.strip()
            or response != result["response_text"] or not rejected
            or result["error"] != "Claude used tools outside the allowed MCP surface: " + ", ".join(sorted(rejected))
            or any(call["tool"] not in allowed | rejected
                   and not fnmatch.fnmatchcase(call["tool"], "mcp__hindsight__*") for call in calls)):
        raise ValueError("Hindsight trace does not prove the inspected completed response")


from reference.runtimes import claude_code as loop


def validate_supermemory_continuation(config, controller: Path, source, recovery: dict) -> None:
    from harness.claude_driver import _transport_metadata
    from reference.runtimes.claude_recovery import file_sha256

    if (not config.plugin_settings.get("capture_session_end")
            or recovery.get("action") != "continue"
            or not recovery.get("generation_files", {}).get("database.archive")):
        raise ValueError("Supermemory continuation requires its saved SessionEnd database")
    trace_path = controller / recovery["trace_relative_path"]
    if file_sha256(trace_path) != recovery["trace_sha256"]:
        raise ValueError("Supermemory interrupted trace changed")
    trace = json.loads(trace_path.read_text())
    result = trace["result"]
    session = recovery["session_id"]
    if (trace.get("source_id") != source.source_id or trace.get("content_sha256") != source.content_sha256
            or result.get("session_id") != session or result.get("ok") is not False
            or result.get("tool_calls") != recovery["inspected_tool_calls"]
            or _transport_metadata(result.get("stdout", ""))[0] != 429):
        raise ValueError("Supermemory trace is not the inspected quota interruption")
    transcript_path = config.home / recovery["transcript_relative_path"]
    if file_sha256(transcript_path) != recovery["transcript_sha256"]:
        raise ValueError("Supermemory interrupted transcript changed")
    events = [json.loads(line) for line in transcript_path.read_text().splitlines() if line.strip()]
    prompts = [e["message"]["content"] for e in events if e.get("type") == "user"
               and isinstance(e.get("message", {}).get("content"), str)]
    expected_prompts = [f"[{source.timestamp}] {source.content}"] + recovery.get(
        "approved_string_continuation_prompts", []
    )
    if prompts != expected_prompts:
        raise ValueError("Supermemory continuation must retain exactly the original source")
    captures = controller / "supermemory-captures.json"
    ack = config.home / ".supermemory-claude/statusline/statusline-state" / hashlib.sha256(session.encode()).hexdigest() / "capture.json"
    tracker = config.home / ".supermemory-claude/trackers" / (session + ".txt")
    for name, path in (("captures", captures), ("capture_ack", ack), ("tracker", tracker)):
        if file_sha256(path) != recovery["capture_files_sha256"][name]:
            raise ValueError("Supermemory partial-capture evidence changed")
    records = json.loads(captures.read_text())
    value = json.loads(ack.read_text())
    capture_count = recovery.get("capture_count", 1)
    if (not isinstance(capture_count, int) or isinstance(capture_count, bool) or capture_count < 1
            or source.source_id in records or len(records) != recovery["completed_count"]
            or value.get("status") != "saved" or value.get("count") != capture_count
            or tracker.read_text().strip() not in {e.get("uuid") for e in events}):
        raise ValueError("Supermemory partial capture differs from the inspected capture")


def validate_retrieval_payload(payload: dict, *, namespace: str, cwd: str,
                               facts=("scaffold", "devon", "q3")) -> None:
    """Prove retrieval from both pilot sources, not perfect recall of every fact."""
    events = []
    for line in payload.get("stdout", "").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    session = payload.get("session_id")
    init = [row for row in events if row.get("subtype") == "init" and row.get("session_id") == session]
    if (payload.get("ok") is not True or payload.get("tool_calls") or len(init) != 1
            or init[0].get("tools") != [] or init[0].get("mcp_servers") != [] or init[0].get("cwd") != cwd):
        raise RuntimeError("Retrieval probe did not use the isolated tool-free session")
    context = ""
    for row in events:
        if row.get("subtype") != "hook_response" or row.get("session_id") != session or row.get("exit_code") != 0:
            continue
        try:
            output = json.loads(row.get("stdout", ""))
        except json.JSONDecodeError:
            continue
        context += output.get("hookSpecificOutput", {}).get("additionalContext", "")
    if namespace not in context or not all(word in context.lower() for word in facts):
        raise RuntimeError("Official read-time context does not establish both pilot sources")
    if not all(word in payload.get("response_text", "").lower() for word in facts):
        raise RuntimeError("Fresh-session answer did not retrieve facts from both pilot sources")


class CaptureGate:
    def __init__(self, config, controller: Path, deadline: float):
        self.config = config
        self.controller = controller
        self.deadline = deadline
        self.path = controller / "supermemory-captures.json"
        self.records = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.continuation = None

    def begin_continuation(self, source, recovery: dict) -> None:
        session = recovery["session_id"]
        deadline = min(self.deadline, time.monotonic() + 900)
        last_status = "not read"
        task_checked = False
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Supermemory processing did not finish for session {session}; "
                    f"last document status: {last_status}"
                )
            try:
                document = self.document(session, deadline=deadline)
            except TimeoutError as exc:
                raise TimeoutError(
                    f"Supermemory document read timed out while waiting for processing of {session}; "
                    f"last document status: {last_status}"
                ) from exc
            except urllib.error.HTTPError as exc:
                if exc.code != 404 or time.monotonic() >= deadline:
                    raise
                time.sleep(2)
                continue
            status = document.get("status", "").upper()
            last_status = status
            if status == "DONE":
                break
            if status == "FAILED" or time.monotonic() >= deadline:
                raise RuntimeError("Supermemory interrupted capture is not fully processed")
            if not task_checked:
                try:
                    self._require_interrupted_task(deadline)
                except RuntimeError:
                    # The task may finish and remove its saved input during the check.
                    if self.document(session, deadline=deadline).get("status", "").upper() != "DONE":
                        raise
                task_checked = True
            time.sleep(2)
        if document["content"].count(f"[{source.timestamp}] {source.content}") != 1:
            raise RuntimeError("Supermemory partial document lacks the exact original source")
        self.continuation = {"source_id": source.source_id, "session_id": session,
                             "document_id": document["id"], "content": document["content"],
                             "capture_count": recovery.get("capture_count", 1)}
        loop._atomic_json(self.controller / "supermemory-continuation-before.json", self.continuation)

    def _require_interrupted_task(
        self, deadline: float, root: Path = Path("/tmp/dolphinbench-supermemory"),
    ) -> None:
        inputs = list((root / "step-data/workflow-input").glob("*.bin"))
        if len(inputs) != 1:
            raise RuntimeError(
                "Supermemory interrupted processing needs recovery: "
                f"expected one saved task, found {len(inputs)}"
            )
        engine = json.loads((root / "runtime/rivet/engine.json").read_text())
        port = engine["guard"]["port"]
        query = urllib.parse.urlencode({"rvt-namespace": "default", "rvt-method": "get",
                                       "rvt-key": inputs[0].stem})
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/gateway/contentIngestion/action/getState?{query}",
            data=json.dumps({"args": []}).encode(),
            headers={"Content-Type": "application/json", "x-rivet-encoding": "json"},
        )
        remaining = min(20, deadline - time.monotonic())
        if remaining <= 0:
            raise RuntimeError("Supermemory processing check reached its deadline")
        try:
            with urllib.request.urlopen(request, timeout=remaining) as response:
                state = json.load(response)
        except (TimeoutError, ConnectionError, http.client.HTTPException, urllib.error.URLError) as exc:
            raise RuntimeError(
                "Supermemory document is readable but its saved processing task did not respond; "
                "recover the task before resuming Claude"
            ) from exc
        output = state.get("output") if isinstance(state, dict) else None
        if not isinstance(output, dict) or not output.get("status") or output["status"] == "failed":
            raise RuntimeError("Supermemory saved processing task did not return a healthy state")

    def _read_json(self, request, *, deadline: float | None = None):
        # Only read-only requests use this helper; never retry a capture here.
        deadline = min(self.deadline, deadline if deadline is not None else time.monotonic() + 900)
        attempt = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Supermemory read did not finish within its retry window")
            try:
                with urllib.request.urlopen(request, timeout=min(30, remaining)) as response:
                    return json.load(response)
            except (TimeoutError, ConnectionError, http.client.RemoteDisconnected,
                    urllib.error.URLError) as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    if exc.code not in {408, 429, 500, 502, 503, 504}:
                        raise
                elif isinstance(exc, urllib.error.URLError):
                    if not isinstance(exc.reason, (TimeoutError, ConnectionError)):
                        raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                attempt += 1
                print(json.dumps({"supermemory_read_retry": attempt,
                                  "error_type": type(exc).__name__}), flush=True)
                time.sleep(min(2 * attempt, 10, remaining))

    def document(self, session: str, *, deadline: float | None = None) -> dict:
        settings = self.config.plugin_settings
        request = urllib.request.Request(
            settings["api_url"].rstrip("/") + "/v3/documents/" + urllib.parse.quote(session, safe=""),
            headers={"Authorization": "Bearer " + settings["api_key"]},
        )
        value = self._read_json(request, deadline=deadline)
        if value.get("customId") != session or not value.get("id") or not value.get("content"):
            raise RuntimeError("Supermemory exact session document is absent or mismatched")
        return value

    def _wait_for_document(self, session: str, label: str) -> dict:
        deadline = min(self.deadline, time.monotonic() + 900)
        while True:
            document = self.document(session, deadline=deadline)
            status = document.get("status", "").upper()
            if status == "DONE":
                return document
            if status == "FAILED" or time.monotonic() >= deadline:
                raise RuntimeError(f"Supermemory {label} did not finish processing: {status}")
            time.sleep(2)

    def preflight(self, _source=None) -> None:
        """Check the SessionStart profile read before sending a new message."""
        settings = self.config.plugin_settings
        body = json.dumps({
            "containerTag": settings["namespace"],
            "q": self.config.project.name,
        }).encode("utf-8")
        request = urllib.request.Request(
            settings["api_url"].rstrip("/") + "/v4/profile",
            data=body,
            headers={
                "Authorization": "Bearer " + settings["api_key"],
                "Content-Type": "application/json",
                "x-sm-source": "claude-code",
            },
            method="POST",
        )
        value = self._read_json(request)
        if not isinstance(value, dict):
            raise RuntimeError("Supermemory profile preflight returned an invalid response")

    def verify_turn(self, source, result) -> None:
        result = loop._trace_payload(result)
        session = result.get("session_id")
        if not isinstance(session, str) or not session:
            raise RuntimeError("Capture needs a successful Claude session ID")
        state_file = (self.config.home / ".supermemory-claude/statusline/statusline-state"
                      / hashlib.sha256(session.encode()).hexdigest() / "capture.json")
        try:
            ack = json.loads(state_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            ack = {}
        continued = self.continuation if self.continuation and self.continuation["source_id"] == source.source_id else None
        expected_count = continued["capture_count"] + 1 if continued else 1
        if ack.get("status") == "saved" and ack.get("count") != expected_count:
            raise RuntimeError("Official Supermemory capture did not acknowledge this fresh session")
        document = self._wait_for_document(session, "capture")
        if continued:
            if (session != continued["session_id"] or document["id"] != continued["document_id"]
                    or not document["content"].startswith(continued["content"])
                    or document["content"].count(f"[{source.timestamp}] {source.content}") != 1):
                raise RuntimeError("Supermemory continuation replaced or duplicated the original capture")
        content = document["content"]
        answer = result.get("response_text", "").strip()
        source_text = f"[{source.timestamp}] {source.content}"
        if content.count(source_text) != 1 or not answer or answer not in content:
            raise RuntimeError("Captured document does not contain the original dated source and final answer")
        if ack.get("status") != "saved":
            ack = {**ack, "status": "reconciled_from_finished_document",
                   "expected_count": expected_count}
        self.records[source.source_id] = {
            "source_sha256": source.content_sha256, "session_id": session,
            "document_id": document["id"], "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "capture_ack": ack,
        }
        loop._atomic_json(self.path, self.records)
        if len(self.records) <= 2 or len(self.records) % 100 == 0:
            print(json.dumps({"supermemory_verified_captures": len(self.records),
                              "source_id": source.source_id}), flush=True)

    def _check(self, record: dict, *, deadline: float | None = None) -> dict:
        document = self.document(record["session_id"], deadline=deadline)
        if (document["id"] != record["document_id"] or
                hashlib.sha256(document["content"].encode()).hexdigest() != record["content_sha256"]):
            raise RuntimeError("Supermemory restored document differs from its capture receipt")
        return document

    def verify_saved(self, state: dict) -> None:
        if set(self.records) != set(state["completed"]):
            raise RuntimeError("Supermemory capture coverage differs from completed sources")
        for source_id, record in self.records.items():
            if record["source_sha256"] != state["completed"][source_id]["content_sha256"]:
                raise RuntimeError("Supermemory capture source hash changed")
            self._check(record)

    def wait_ready(self) -> None:
        deadline = min(self.deadline, time.monotonic() + 900)
        for record in self.records.values():
            while True:
                status = self._check(record, deadline=deadline).get("status", "").upper()
                if status == "DONE":
                    break
                if status == "FAILED" or time.monotonic() >= deadline:
                    raise RuntimeError(f"Supermemory pilot processing did not complete: {status}")
                time.sleep(2)

    def verify_fresh_retrieval(self) -> None:
        # Only the official read-time hooks are loaded. No MCP, capture hook,
        # native memory, transcript, or filesystem tool can supply the answer.
        config = self.config
        home = config.home.parent / (config.home.name + "-retrieval-probe")
        if home.exists():
            raise RuntimeError("Fresh retrieval probe already exists; inspect before repeating")
        project = home / "project"
        project.mkdir(parents=True)
        source = home / "plugin"
        shutil.copytree(config.plugin_source, source)
        (source / ".mcp.json").unlink(missing_ok=True)
        manifest = source / "hooks/hooks.json"
        value = json.loads(manifest.read_text())
        value["hooks"] = {key: item for key, item in value["hooks"].items()
                          if key in {"SessionStart", "UserPromptSubmit"}}
        manifest.write_text(json.dumps(value) + "\n")
        setup = loop.configure_plugin("supermemory", home, project, source,
                                      {**config.plugin_settings, "capture_session_end": False})
        config_dir = home / ".claude"
        config_dir.mkdir()
        for path in (home, *home.rglob("*")):
            os.chown(path, config.run_as_user, config.run_as_group, follow_symlinks=False)
        result = loop.run_claude(
            config.plugin_settings.get("retrieval_probe_prompt",
                "What company did I start, who co-founded it with me, and what timing did I mention "
                "for fundraising and the main push?") + " Answer only from Supermemory's supplied context.",
            claude_command=config.claude_command, model=config.model, cwd=project,
            claude_config_dir=config_dir, builtin_tools=[], allowed_mcp_tools=[],
            plugin_directories=setup["plugin_dirs"], settings={"autoMemoryEnabled": False},
            env=loop.child_environment("supermemory", str(home), setup["env"], config.environment),
            timeout=min(300, self.deadline - time.monotonic()),
            run_as_user=config.run_as_user, run_as_group=config.run_as_group,
        )
        payload = loop._trace_payload(result)
        loop._atomic_json(self.controller / "supermemory-retrieval-probe.json", loop.redact_credentials(
            payload, loop.credential_values(config.environment, config.plugin_settings)))
        validate_retrieval_payload(payload, namespace=config.plugin_settings["namespace"], cwd=str(project),
            facts=config.plugin_settings.get("retrieval_probe_terms", ("scaffold", "devon", "q3")))

    def finish_saved_probe(self, recovery: dict, restored: dict, state: dict) -> None:
        if (not restored or restored["generation"] != recovery.get("generation")
                or restored["files"] != recovery.get("generation_files")
                or len(state["completed"]) != 2 or recovery.get("completed_count") != 2
                or (self.controller / "in-flight.json").exists()):
            raise RuntimeError("Pilot recovery differs from the inspected two-message checkpoint")
        for name in ("receipt.json", "supermemory-captures.json", "supermemory-retrieval-probe.json"):
            if hashlib.sha256((self.controller / name).read_bytes()).hexdigest() != recovery.get("files_sha256", {}).get(name):
                raise RuntimeError("Pilot recovery evidence differs from the inspected files")
        self.verify_saved(state)
        self.wait_ready()
        payload = json.loads((self.controller / "supermemory-retrieval-probe.json").read_text())
        if payload.get("session_id") in {row["session_id"] for row in self.records.values()}:
            raise RuntimeError("Retrieval probe reused an ingestion session")
        home = self.config.home
        validate_retrieval_payload(payload, namespace=self.config.plugin_settings["namespace"],
                                   cwd=str(home.parent / (home.name + "-retrieval-probe") / "project"),
                                   facts=self.config.plugin_settings.get("retrieval_probe_terms", ("scaffold", "devon", "q3")))


from reference.memory.hindsight import is_sequence_failure
from reference.memory.hindsight import wait_for_clean_operations
from reference.memory.hindsight import HindsightApi


def validate_hindsight_continuation(config: Any, controller: Path, source: Any, recovery: dict) -> None:
    from harness.claude_driver import _parse_trace, _transport_metadata
    from reference.runtimes.claude_recovery import HONCHO_CONTINUATION
    from reference.runtimes.claude_recovery import file_sha256

    evidence = recovery["database_evidence"]
    if (evidence.get("database_sha256") != recovery["generation_files"].get("database.archive")
            or evidence.get("bank_id") != config.plugin_settings["namespace"]
            or evidence.get("document_id") != "conversation:" + recovery["session_id"]
            or (evidence.get("document_absent") is not True
                and not (recovery["action"] == "finish"
                         and evidence.get("document_absent") is False
                         and evidence.get("queued_content_sha256")))
            or (recovery["action"] == "continue" and evidence.get("session_operations_absent") is not True)
            or (recovery["action"] == "finish" and not evidence.get("queued_operation_ids"))
            or evidence.get("prior_completed_documents_verified") != recovery["completed_count"]
            or not isinstance(evidence.get("document_ids"), list)
            or not isinstance(evidence.get("failed_operation_ids"), list)):
        raise ValueError("Hindsight continuation lacks source-bound database evidence")
    trace_path = controller / recovery["trace_relative_path"]
    if file_sha256(trace_path) != recovery["trace_sha256"]:
        raise ValueError("Hindsight interrupted trace differs from its inspection")
    trace = json.loads(trace_path.read_text())
    result = trace["result"]
    if (trace.get("source_id") != source.source_id
            or trace.get("content_sha256") != source.content_sha256
            or result.get("session_id") != recovery["session_id"]
            or result.get("tool_calls") != recovery["inspected_tool_calls"]):
        raise ValueError("Hindsight trace does not match the inspected turn")
    if recovery["action"] == "continue":
        if result.get("ok") is not False or _transport_metadata(result.get("stdout", ""))[0] != 429:
            raise ValueError("Hindsight trace does not match the inspected quota interruption")
    else:
        session, response, _, _, _, error, complete = _parse_trace(result.get("stdout", ""))
        if (result.get("ok") is not True or not complete or error or result.get("error")
                or session != recovery["session_id"] or response != result.get("response_text")
                or not response.strip() or _transport_metadata(result.get("stdout", ""))[0] == 429):
            raise ValueError("Hindsight saved turn is not a verified successful Claude answer")
    events = [json.loads(line) for line in
              (config.home / recovery["transcript_relative_path"]).read_text().splitlines()]
    prompts = [event["message"]["content"] for event in events
               if event.get("type") == "user" and isinstance(event.get("message", {}).get("content"), str)]
    expected_prompts = [f"[{source.timestamp}] {source.content}"]
    prior_continuations = recovery.get("prior_continuations", 0)
    if type(prior_continuations) is not int or not 0 <= prior_continuations <= 10:
        raise ValueError("Invalid inspected prior continuation count")
    expected_prompts.extend([HONCHO_CONTINUATION] * prior_continuations)
    if recovery["action"] == "finish":
        expected_prompts.append(HONCHO_CONTINUATION)
    if prompts != expected_prompts:
        raise ValueError("Hindsight saved session does not contain exactly the original source")
    if recovery["action"] == "finish":
        answers = [block["text"] for event in events if event.get("type") == "assistant"
                   for block in event.get("message", {}).get("content", [])
                   if isinstance(block, dict) and block.get("type") == "text"]
        if not answers or answers[-1] != result["response_text"]:
            raise ValueError("Hindsight saved transcript lacks the verified final answer")


def probe_database(bank_id: str, session_id: str) -> dict:
    from reference.memory import services as services
    result = subprocess.run(
        ["/app/api/.venv/bin/python", "-m", "reference.runtimes.claude_recovery", bank_id, session_id],
        env=services._hindsight_environment(), user=1000, group=1000,
        capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(result.stdout)


def validate_restored_state(state: dict, recovery: dict) -> None:
    evidence = recovery["database_evidence"]
    if recovery.get("action") == "finish":
        previous = set(evidence["document_ids"])
        actual = set(state["document_ids"])
        if (state["bank_id"] != evidence["bank_id"] or not previous <= actual
                or actual - previous - {evidence["document_id"]}):
            raise RuntimeError("Restored Hindsight documents differ from the inspected checkpoint")
        for row in state["noncompleted_operations"]:
            if (row["status"] not in {"pending", "processing"}
                    or row["operation_type"] not in {"batch_retain", "retain", "consolidation"}
                    or (row["operation_type"] != "consolidation"
                        and row["operation_id"] not in evidence["queued_operation_ids"])):
                raise RuntimeError("Hindsight queued work differs from the inspected saved answer")
        return
    if (state["bank_id"] != evidence["bank_id"]
            or state["document_ids"] != sorted(evidence["document_ids"])
            or state["target_document"] is not None or state["target_operations"]):
        raise RuntimeError("Restored Hindsight documents differ from the inspected checkpoint")
    rows = state["noncompleted_operations"]
    pending = {row["operation_id"]: row["operation_type"] for row in evidence.get("pending_operations", [])}
    remaining = []
    for row in rows:
        if (row["operation_id"] in pending and row["status"] in {"pending", "processing"}
                and row["operation_type"] == pending[row["operation_id"]]):
            continue
        remaining.append(row)
    rows = remaining
    if (sorted(row["operation_id"] for row in rows) != sorted(evidence["failed_operation_ids"])
            or any(row["status"] != "failed" or not is_sequence_failure(row) for row in rows)):
        raise RuntimeError("Restored Hindsight failures differ from the approved counter repairs")


def validate_recovered_document(state: dict, source: Any, result: dict, recovery: dict) -> dict:
    document = state["target_document"]
    if not document or state["noncompleted_operations"]:
        raise RuntimeError("Recovered Hindsight conversation or clean operations are missing")
    turns = [json.loads(line) for line in document["original_text"].splitlines() if line.strip()]
    original = f"[{source.timestamp}] {source.content}"
    users = [turn.get("content") for turn in turns if turn.get("role") == "user"]
    answers = [turn.get("content", "") for turn in turns if turn.get("role") == "assistant"]
    expected_ids = set(recovery["database_evidence"]["document_ids"]) | {"conversation:" + recovery["session_id"]}
    if (set(state["document_ids"]) != expected_ids
            or users.count(original) != 1 or users != [original]
            or not answers or answers[-1].strip() != result["response_text"].strip()
            or not result["response_text"].strip()
            or result.get("session_id") != recovery["session_id"]):
        raise RuntimeError("Recovered Hindsight document does not contain the exact original turn")
    operations = state["target_operations"]
    if not operations or any(row["status"] != "completed" for row in operations):
        raise RuntimeError("Recovered Hindsight extraction is not completed")
    return {"document_id": document["id"], "content_sha256": hashlib.sha256(document["original_text"].encode()).hexdigest(),
            "operation_ids": [row["operation_id"] for row in operations], "verified": True}


class RecoveryGate:
    def __init__(self, config: Any, recovery: dict, deadline: float):
        self.config, self.recovery, self.deadline = config, recovery, deadline
        self.bank = config.plugin_settings["namespace"]
        self.api = HindsightApi(config.plugin_settings["api_url"], config.plugin_settings["api_key"])
        self.evidence: dict = {}

    def drain(self, allowed_retry_ids: set[str]) -> dict:
        return wait_for_clean_operations(
            self.api, self.bank, timeout_seconds=min(6000, max(0, self.deadline - time.monotonic())),
            allowed_retry_ids=allowed_retry_ids,
        )

    def before_claude(self) -> None:
        state = probe_database(self.bank, self.recovery["session_id"])
        validate_restored_state(state, self.recovery)
        self.evidence["operation_recovery"] = self.drain(set(self.recovery["database_evidence"]["failed_operation_ids"]))
        print(json.dumps({"hindsight_recovery_stage": "database_clean", **self.evidence}), flush=True)

    def verify_turn(self, source: Any, result: Any) -> None:
        from reference.runtimes.claude_code import _trace_payload
        self.drain(set())
        state = probe_database(self.bank, self.recovery["session_id"])
        if self.recovery.get("action") == "finish":
            document = state["target_document"]
            expected = self.recovery["database_evidence"]["queued_content_sha256"]
            if (not document or state["noncompleted_operations"]
                    or hashlib.sha256(document["original_text"].encode()).hexdigest() != expected):
                raise RuntimeError("Completed queued document differs from the inspected partial transcript")
            self.flush_saved_transcript()
            self.drain(set())
            state = probe_database(self.bank, self.recovery["session_id"])
        self.evidence["recovered_turn"] = validate_recovered_document(state, source, _trace_payload(result), self.recovery)

    def flush_saved_transcript(self) -> None:
        from harness.claude_plugin_setup import _hindsight_root, configure_plugin
        from reference.plan import child_environment
        from reference.runtimes.claude_recovery import file_sha256
        transcript = self.config.home / self.recovery["transcript_relative_path"]
        if file_sha256(transcript) != self.recovery["transcript_sha256"]:
            raise RuntimeError("Hindsight transcript changed before the inspected hook flush")
        setup = configure_plugin("hindsight", self.config.home, self.config.project,
                                 self.config.plugin_source, dict(self.config.plugin_settings))
        environment = child_environment("hindsight", str(self.config.home.resolve()),
                                        setup["env"], self.config.environment or {})
        hook = _hindsight_root(self.config.plugin_source) / "dist/claude-stop-hook.js"
        subprocess.run(
            ["node", str(hook)], input=json.dumps({"session_id": self.recovery["session_id"],
             "transcript_path": str(transcript), "cwd": str(self.config.project)}),
            env=environment, cwd=self.config.project, user=self.config.run_as_user,
            group=self.config.run_as_group or self.config.run_as_user,
            capture_output=True, text=True, timeout=min(60, max(1, self.deadline - time.monotonic())), check=True,
        )
        self.evidence["official_stop_hook_flushed"] = True


async def _probe(bank: str, session: str) -> dict:
    import asyncpg
    from hindsight_api.pg0 import resolve_database_url
    connection = await asyncpg.connect(await resolve_database_url("pg0"), timeout=30)
    try:
        async with connection.transaction(readonly=True):
            documents = await connection.fetch("SELECT id FROM public.documents WHERE bank_id=$1 ORDER BY id", bank)
            document = await connection.fetchrow(
                "SELECT id,original_text FROM public.documents WHERE bank_id=$1 AND id=$2", bank, "conversation:" + session)
            rows = await connection.fetch(
                "SELECT operation_id::text,operation_type,status,error_message FROM public.async_operations "
                "WHERE bank_id=$1 AND status != 'completed' ORDER BY operation_id", bank)
            operations = await connection.fetch(
                "SELECT operation_id::text,operation_type,status FROM public.async_operations WHERE bank_id=$1 "
                "AND (strpos(coalesce(task_payload::text,''),$2)>0 OR strpos(coalesce(result_metadata::text,''),$2)>0)",
                bank, session)
        return {"bank_id": bank, "document_ids": sorted(row["id"] for row in documents),
                "target_document": dict(document) if document else None,
                "noncompleted_operations": [{**dict(row), "error_message": (row["error_message"] or "").split("\n")[0]} for row in rows],
                "target_operations": [dict(row) for row in operations]}
    finally:
        await connection.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_probe(sys.argv[1], sys.argv[2]))))
