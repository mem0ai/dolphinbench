"""Export retained ingestion conversations from the profiles selected for official runs."""

from __future__ import annotations

import argparse
import collections
import functools
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import zipfile

import yaml

from build_public_run_records import artifact_name, sha256, system_path, write_compact_json


@functools.cache
def sources(root: Path, persona: str) -> list[dict]:
    history = yaml.safe_load((root / f"registry/personas/{persona}/life_sim.yaml").read_text())["sessions"]
    mapping = json.loads((root / "session_map.json").read_text())[persona]
    result = []
    for session, original in zip(history, mapping, strict=True):
        text = session["messages"][0].strip()
        result.append({"session_id": session["id"], "original_session_id": original["original_session_id"],
                       "original_message_index": original["original_message_index"],
                       "source_id": f"dolphinbench-{persona}-{original['original_session_id']}-{original['original_message_index']}",
                       "date": session["narrative_date"], "content": text,
                       "content_sha256": hashlib.sha256(text.encode()).hexdigest()})
    return result


def without_date(text: str) -> str:
    return re.sub(r"^\[\d{4}-\d{2}-\d{2}[^\]]*\]\s*", "", text).strip()


def dated_key(text: str) -> tuple[str | None, str]:
    match = re.match(r"^\[([^\]]+)\]\s*(.*)$", text, flags=re.DOTALL)
    return (match.group(1), match.group(2).strip()) if match else (None, text.strip())


def saved_session_attempts(profile_path: Path, wanted: set[tuple[str, str]]) -> dict[tuple[str, str], list[dict]]:
    """Read conversations absent from state.db from the profile's saved session logs."""
    found = collections.defaultdict(list)
    for path in (profile_path / "sessions").glob("*.jsonl"):
        messages = []
        for line in path.read_text(errors="replace").splitlines():
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if raw.get("role") not in ("user", "assistant", "tool"):
                continue
            message = {key: raw[key] for key in ("role", "content", "tool_name", "tool_calls", "tool_call_id")
                       if raw.get(key) is not None}
            if message:
                messages.append(message)
        starts = [(index, dated_key(message["content"])) for index, message in enumerate(messages)
                  if message.get("role") == "user" and isinstance(message.get("content"), str)
                  and dated_key(message["content"]) in wanted]
        for position, (start, source) in enumerate(starts):
            next_users = [index for index in range(start + 1, len(messages)) if messages[index].get("role") == "user"]
            end = next_users[0] if next_users else len(messages)
            segment = messages[start:end]
            if not segment:
                continue
            signature = json.dumps(segment, sort_keys=True, ensure_ascii=False)
            if any(item.get("_signature") == signature for item in found[source]):
                continue
            found[source].append({"session_id": f"{path.stem}:{position + 1}",
                                  "source_file": str(path), "messages": segment,
                                  "_signature": signature})
    for path in (profile_path / "sessions").glob("enact-*.json"):
        try:
            document = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        messages = [{key: raw[key] for key in ("role", "content", "tool_name", "tool_calls", "tool_call_id")
                     if raw.get(key) is not None}
                    for raw in document.get("messages", [])
                    if raw.get("role") in ("user", "assistant", "tool")]
        source = next((dated_key(message["content"]) for message in messages
                       if message.get("role") == "user" and isinstance(message.get("content"), str)
                       and dated_key(message["content"]) in wanted), None)
        if source is None:
            continue
        signature = json.dumps(messages, sort_keys=True, ensure_ascii=False)
        if any(item.get("_signature") == signature for item in found[source]):
            continue
        found[source].append({"session_id": path.stem.removeprefix("enact-"),
                              "source_file": str(path), "messages": messages,
                              "_signature": signature})
    for path in (profile_path / "submission-traces").glob("*.jsonl"):
        final = None
        for line in path.read_text(errors="replace").splitlines():
            try:
                final = json.loads(line)
            except json.JSONDecodeError:
                continue
        if not final:
            continue
        raw_messages = final.get("messages") or []
        assistant = final.get("assistant")
        if isinstance(assistant, dict):
            raw_messages = [*raw_messages, assistant]
        messages = [{key: raw[key] for key in ("role", "content", "tool_name", "tool_calls", "tool_call_id")
                     if raw.get(key) is not None}
                    for raw in raw_messages if raw.get("role") in ("user", "assistant", "tool")]
        source = None
        for message in messages:
            if message.get("role") != "user" or not isinstance(message.get("content"), str):
                continue
            date, content = dated_key(message["content"].split("\n\n<memory-context>", 1)[0])
            if (date, content) in wanted:
                source = (date, content)
                break
        if source is None:
            continue
        signature = json.dumps(messages, sort_keys=True, ensure_ascii=False)
        if any(item.get("_signature") == signature for item in found[source]):
            continue
        found[source].append({"session_id": path.stem, "source_file": str(path),
                              "messages": messages, "_signature": signature})
    for attempts in found.values():
        for attempt in attempts:
            attempt.pop("_signature", None)
    return found


def export_hermes(row: dict, persona: str, manifest: dict, root: Path, profiles: Path,
                  result_files: dict[str, Path]) -> dict:
    job = manifest["source_seed_artifacts"]["jobs"][f"{row['memory']['id']}-{persona}"]
    profile = job.get("source_seed_profile") or job["profile"]
    database = profiles / profile / "state.db"
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    by_session = collections.defaultdict(list)
    for raw in connection.execute("SELECT session_id,role,content,tool_calls,tool_call_id FROM messages ORDER BY id"):
        message = {key: raw[key] for key in ("role", "content", "tool_calls", "tool_call_id") if raw[key] is not None}
        if message.get("tool_calls"):
            message["tool_calls"] = json.loads(message["tool_calls"])
        else:
            message.pop("tool_calls", None)
        by_session[raw["session_id"]].append(message)
    session_columns = {value[1] for value in connection.execute("PRAGMA table_info(sessions)")}
    fields = [key for key in ("id", "model", "started_at", "ended_at", "input_tokens", "output_tokens",
                              "cache_read_tokens", "cache_write_tokens", "estimated_cost_usd") if key in session_columns]
    metadata = {raw["id"]: dict(raw) for raw in connection.execute("SELECT " + ",".join(fields) + " FROM sessions")}
    connection.close()
    matches = collections.defaultdict(list)
    for identifier, messages in by_session.items():
        for message in messages:
            if message["role"] == "user" and isinstance(message.get("content"), str):
                matches[dated_key(message["content"])].append(identifier)
    wanted = {(source["date"], source["content"]) for source in sources(root, persona)
              if not matches.get((source["date"], source["content"]))}
    saved = saved_session_attempts(profiles / profile, wanted) if wanted else {}
    calls = {}
    for recorded_path, expected_hash in job.get("result_hashes", {}).items():
        path = Path(recorded_path)
        if not path.is_file() and expected_hash in result_files:
            path = result_files[expected_hash]
        if not path.is_file():
            continue
        if sha256(path) != expected_hash:
            raise ValueError(f"Result hash mismatch: {path}")
        document = json.loads(path.read_text())
        for call in document.get("seed_calls", []):
            calls[(str(call.get("session_id")), int(call.get("message_index", 0)))] = {
                "call": call, "path": recorded_path, "sha256": expected_hash}
    conversations = []
    missing = []
    for source in sources(root, persona):
        source_key = (source["date"], source["content"].strip())
        identifiers = list(dict.fromkeys(matches.get(source_key, [])))
        item = {key: value for key, value in source.items() if key != "content"}
        item["attempts"] = []
        for identifier in identifiers:
            item["attempts"].append({"session_id": identifier, "usage": metadata.get(identifier, {}),
                                     "messages": by_session[identifier]})
        item["attempts"].extend(saved.get(source_key, []))
        if not item["attempts"]:
            record = calls.get((str(source["original_session_id"]), int(source["original_message_index"])))
            if record:
                call = record["call"]
                output = call.get("driver_stdout") or ""
                item["attempts"].append({
                    "session_id": call.get("hermes_session_id"), "source_file": record["path"],
                    "source_file_sha256": record["sha256"], "usage": call.get("token_usage"),
                    "latency_seconds": call.get("latency_seconds"), "cost_usd": call.get("cost_usd"),
                    "messages": [{"role": "user", "content": source["content"]},
                                 {"role": "assistant", "content": output}],
                })
        if not item["attempts"]:
            missing.append(source["session_id"])
        conversations.append(item)
    return {"configuration_id": row["id"], "persona": persona, "profile": profile,
            "source_database_sha256": sha256(database),
            "source_history": f"registry/personas/{persona}/life_sim.yaml",
            "expected_messages": len(conversations), "messages_with_saved_conversations": len(conversations) - len(missing),
            "missing_conversation_ids": missing, "sessions": conversations}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", action="append", default=[], help="Configuration ID to export")
    parser.add_argument("--persona", action="append", choices=("morgan", "alex", "riley"), default=[])
    parser.add_argument("--result-file", action="append", default=[], help="SHA256=PATH")
    args = parser.parse_args()
    result_files = {}
    for value in args.result_file:
        expected, separator, path = value.partition("=")
        if not separator:
            raise ValueError("--result-file must use SHA256=PATH")
        result_files[expected] = Path(path)
    report = json.loads((args.root / "website/content/official-results.json").read_text())
    for row in report["configurations"]:
        if row["harness"]["id"] != "hermes":
            continue
        if args.only and row["id"] not in args.only:
            continue
        archive = args.root / "website/public/leaderboard/cost-records" / artifact_name(row["evidence"]["configuration"]["url"])
        with zipfile.ZipFile(archive) as records:
            for persona in (args.persona or ("morgan", "alex", "riley")):
                manifest = json.loads(records.read(f"{persona}/launch_manifest.json"))
                document = export_hermes(row, persona, manifest, args.root, args.profiles, result_files)
                target = args.output / system_path(row) / persona / "ingestion.json"
                write_compact_json(target, document)
                print(f"{row['id']}/{persona}: {document['messages_with_saved_conversations']}/{document['expected_messages']} conversations, {target.stat().st_size:,} bytes", flush=True)


if __name__ == "__main__":
    main()
