"""Read the selected Claude plugin ingestion traces from retained files and Modal."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
from urllib.parse import urlsplit
import zipfile

import modal

from build_public_run_records import artifact_name, system_path, write_compact_json
from export_public_ingestion import sources


NATIVE_MEMORY_VOLUMES = {
    "morgan": "dolphinbench-claude-native-seed-v1",
    "alex": "dolphinbench-claude-alex-native-seed-v1",
    "riley": "dolphinbench-claude-riley-native-seed-v1",
}


async def native_memory_files(persona: str, expected_sha256: str) -> list[dict]:
    volume = modal.Volume.from_name(NATIVE_MEMORY_VOLUMES[persona])
    payload = b"".join([part async for part in volume.read_file.aio("final/native-memory.tar.gz")])
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError(f"Native-memory archive mismatch for {persona}")
    files = []
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive:
            if not member.isfile() or not member.name.startswith("auto-memory/"):
                continue
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"Cannot read native-memory file: {member.name}")
            files.append({"file": member.name.removeprefix("auto-memory/"),
                          "content": source.read().decode("utf-8")})
    return files


def visible_events(stdout: str) -> list[dict]:
    messages = []
    seen = set()
    for line in stdout.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") not in ("user", "assistant"):
            continue
        if event.get("uuid") and event["uuid"] in seen:
            continue
        seen.add(event.get("uuid"))
        raw = event.get("message") or {}
        content = raw.get("content")
        if isinstance(content, list):
            content = [item for item in content if item.get("type") not in ("thinking", "redacted_thinking", "reasoning")]
        if not content:
            continue
        message = {"role": raw.get("role", event["type"]), "content": content}
        if event.get("parent_tool_use_id"):
            message["parent_tool_use_id"] = event["parent_tool_use_id"]
        messages.append(message)
    return messages


async def export(args) -> None:
    report = json.loads((args.root / "website/content/official-results.json").read_text())
    usage_honcho = json.loads(gzip.decompress((args.usage_root / "claude-agent-usage.json.gz").read_bytes()))["personas"]
    usage_mem0 = json.loads(gzip.decompress((args.usage_root / "claude-mem0-agent-usage.json.gz").read_bytes()))
    archives = {}
    semaphore = asyncio.Semaphore(24)
    args.download_cache.mkdir(parents=True, exist_ok=True)

    async def fetch(entry: dict) -> tuple[dict, dict]:
        reference = entry.get("source_ref") or entry["source"]
        cached = args.download_cache / f"{entry['sha256']}.json"
        if cached.is_file():
            raw = cached.read_bytes()
        elif reference.startswith("modal-volume://"):
            url = urlsplit(reference)
            volume = modal.Volume.from_name(url.netloc)
            if url.fragment:
                key = f"{url.netloc}{url.path}"
                if key not in archives:
                    async def read_archive():
                        raw = b"".join([part async for part in volume.read_file.aio(url.path)])
                        archive = tarfile.open(fileobj=io.BytesIO(raw))
                        return {member.name: archive.extractfile(member).read() for member in archive
                                if member.isfile() and member.name.startswith("traces/") and member.name.endswith(".json")}
                    archives[key] = asyncio.create_task(read_archive())
                contents = await archives[key]
                raw = contents[url.fragment]
            else:
                async with semaphore:
                    raw = b"".join([part async for part in volume.read_file.aio(url.path)])
        else:
            raw = Path(reference).read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError(f"Trace hash mismatch: {entry['source_id']}")
        if not cached.is_file():
            cached.write_bytes(raw)
        return entry, json.loads(raw)

    for row in report["configurations"]:
        if row["harness"]["id"] != "claude-code":
            continue
        if args.only and row["id"] not in args.only:
            continue
        memory = row["memory"]["id"]
        if memory == "builtin":
            archive = args.root / "website/public/leaderboard/cost-records" / artifact_name(
                row["evidence"]["configuration"]["url"])
            with zipfile.ZipFile(archive) as records:
                for persona in ("morgan", "alex", "riley"):
                    payload = records.read(f"{persona}/ingestion-receipt.json")
                    receipt = json.loads(payload)
                    original = sources(args.root, persona)
                    sessions = []
                    for source in original:
                        attempts = receipt["source_attempts"].get(source["source_id"], [])
                        sessions.append({**source, "attempts": attempts})
                    missing = [item["session_id"] for item in sessions if not item["attempts"]]
                    document = {
                        "configuration_id": row["id"], "persona": persona,
                        "record_type": "completed_native_memory_ingestion",
                        "expected_messages": len(original),
                        "messages_with_saved_conversations": len(original) - len(missing),
                        "missing_conversation_ids": missing,
                        "completion": {key: receipt.get(key) for key in (
                            "status", "completed", "memory_provider", "model", "corpus", "snapshot",
                            "ingestion_metrics", "recovery_adjudications")},
                        "receipt_sha256": hashlib.sha256(payload).hexdigest(), "sessions": sessions,
                        "memory_at_end": await native_memory_files(persona, receipt["snapshot"]["sha256"]),
                    }
                    target = args.output / system_path(row) / persona / "ingestion.json"
                    write_compact_json(target, document)
                    print(f"{row['id']}/{persona}: {len(original) - len(missing)}/{len(original)} conversations, {target.stat().st_size:,} bytes", flush=True)
            continue
        for persona in ("morgan", "alex", "riley"):
            entries = usage_honcho[persona]["ingestion"] if memory == "honcho" else usage_mem0[persona]
            original = {item["source_id"]: item for item in sources(args.root, persona)}
            records = await asyncio.gather(*(fetch(entry) for entry in entries))
            sessions = []
            covered = set()
            for entry, trace in records:
                source = original[entry["source_id"]]
                if source["content_sha256"] != trace["content_sha256"]:
                    raise ValueError(f"Source content mismatch: {entry['source_id']}")
                result = trace["result"]
                sessions.append({**source, "agent_session_id": result.get("session_id"),
                                 "trace_sha256": entry["sha256"],
                                 "trace_source": entry.get("source_ref") or entry["source"],
                                 "messages": visible_events(result.get("stdout") or ""),
                                 "response": result.get("response_text"),
                                 "token_usage": result.get("token_usage"), "ok": result.get("ok")})
                covered.add(source["session_id"])
            sessions.sort(key=lambda value: value["session_id"])
            missing = [value["session_id"] for value in original.values() if value["session_id"] not in covered]
            document = {"configuration_id": row["id"], "persona": persona,
                        "expected_messages": len(original), "messages_with_saved_conversations": len(covered),
                        "missing_conversation_ids": missing, "sessions": sessions}
            target = args.output / system_path(row) / persona / "ingestion.json"
            write_compact_json(target, document)
            print(f"{row['id']}/{persona}: {len(covered)}/{len(original)} conversations, {target.stat().st_size:,} bytes", flush=True)
            archives.clear()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--usage-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--download-cache", type=Path, required=True)
    parser.add_argument("--only", action="append", default=[])
    asyncio.run(export(parser.parse_args()))


if __name__ == "__main__":
    main()
