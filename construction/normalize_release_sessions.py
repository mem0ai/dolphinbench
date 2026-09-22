"""Make a local single-message copy of the authenticated three-persona release."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from harness.submission import link_ingestion
from harness.dataset import load_test

from construction.validate_release import PERSONAS, ROOT, load_release, load_yaml, validate_persona


REFERENCE_FIELDS = ("source_session_ids", "related_history_session_ids")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize(release: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Split source sessions, retaining all source text and fact evidence."""
    sessions = []
    mapping = []
    expanded: dict[str, list[str]] = {}
    for source in release["sessions"]:
        source_id = str(source["id"])
        if source_id in expanded:
            raise ValueError(f"Duplicate source session: {source_id}")
        messages = source.get("messages")
        if not isinstance(messages, list) or not messages or not all(
            isinstance(message, str) and message.strip() for message in messages
        ):
            raise ValueError(f"{source_id}: expected non-empty user-message strings")
        if not source.get("narrative_date"):
            raise ValueError(f"{source_id}: missing narrative date")
        expanded[source_id] = []
        for index, message in enumerate(messages):
            session_id = f"{len(sessions) + 1:06d}"
            session = copy.deepcopy(source)
            session.update(id=session_id, messages=[message])
            sessions.append(session)
            expanded[source_id].append(session_id)
            mapping.append({"session_id": session_id, "original_session_id": source_id,
                            "original_message_index": index})

    facts = copy.deepcopy(release["facts"])
    exact = {row["id"]: row for row in release.get("source_facts") or []}
    for fact in facts.values():
        for field in REFERENCE_FIELDS:
            if field in fact:
                try:
                    if field == "source_session_ids" and fact["id"] in exact:
                        fact[field] = list(dict.fromkeys(
                            expanded[ref["session_id"]][ref["message_index"]]
                            for ref in exact[fact["id"]]["sources"]))
                    else:
                        fact[field] = [new_id for old_id in fact[field]
                                       for new_id in expanded[str(old_id)]]
                except KeyError as exc:
                    raise ValueError(f"Fact {fact['id']}: unknown source session {exc}") from exc
    result = {**release, "sessions": sessions, "facts": facts}
    verify_equivalence(release, result, mapping)
    return result, mapping


def verify_equivalence(original: dict[str, Any], converted: dict[str, Any],
                       mapping: list[dict[str, Any]]) -> None:
    """Compare complete messages and every fact's evidence, not just counts."""
    expected = [(str(s["id"]), i, s, text) for s in original["sessions"]
                for i, text in enumerate(s["messages"])]
    sessions = converted["sessions"]
    if len(sessions) != len(expected) or len(mapping) != len(expected):
        raise ValueError("Session conversion changed message coverage")
    expanded: dict[str, list[str]] = {}
    for number, ((source_id, index, source, text), session, link) in enumerate(
        zip(expected, sessions, mapping), start=1
    ):
        new_id = f"{number:06d}"
        if link != {"session_id": new_id, "original_session_id": source_id,
                    "original_message_index": index}:
            raise ValueError(f"Incorrect source mapping for session {new_id}")
        expected_session = {**source, "id": new_id, "messages": [text]}
        if session != expected_session:
            raise ValueError(f"Session {new_id}: text, date, order, or metadata changed")
        expanded.setdefault(source_id, []).append(new_id)

    if original["facts"].keys() != converted["facts"].keys():
        raise ValueError("Fact IDs changed")
    exact = {row["id"]: row for row in original.get("source_facts") or []}
    for fact_id, old in original["facts"].items():
        new = converted["facts"][fact_id]
        if {k: v for k, v in old.items() if k not in REFERENCE_FIELDS} != {
            k: v for k, v in new.items() if k not in REFERENCE_FIELDS
        }:
            raise ValueError(f"Fact {fact_id}: content changed")
        for field in REFERENCE_FIELDS:
            if (field in old) != (field in new):
                raise ValueError(f"Fact {fact_id}: {field} was added or removed")
            expected_ids = [sid for old_id in old.get(field, [])
                            for sid in expanded[str(old_id)]]
            if field == "source_session_ids" and fact_id in exact:
                expected_ids = list(dict.fromkeys(
                    expanded[ref["session_id"]][ref["message_index"]]
                    for ref in exact[fact_id]["sources"]))
            if new.get(field, []) != expected_ids:
                raise ValueError(f"Fact {fact_id}: {field} changed evidence coverage")
    if original["tests"] != converted["tests"]:
        raise ValueError("Test content changed")


def _files(root: Path = ROOT) -> set[str]:
    return {"session_map.json"} | {
        f"registry/personas/{persona}/{name}.yaml"
        for persona in PERSONAS for name in ("life_sim", "facts")
    } | {f"tests/{persona}/{i:03d}.yaml" for persona in PERSONAS for i in range(1, 201)} | {
        f"tests/{persona}/state.json" for persona in PERSONAS
        if (root / "tests" / persona / "state.json").is_file()
    }


def _source_metadata(release: dict[str, Any], root: Path) -> dict[str, Any]:
    persona = release["persona"]
    return {
        "checkpoint_identity": release["manifest"]["checkpoint_identity"],
        "release_sha256": sha256(root / "authoring/release_manifests" / f"{persona}.json"),
        "original_sessions": len(release["sessions"]),
        "sessions": sum(len(row["messages"]) for row in release["sessions"]),
        "user_message_tokens": release["metadata"]["history_tokens_o200k_base"],
        "tests": len(release["tests"]),
    }


def prepare(output: Path, *, root: Path = ROOT) -> dict[str, Any]:
    output = output.resolve()
    if output.exists():
        raise ValueError("Output must not exist; preserve previous releases")
    originals = {persona: load_release(persona, root=root) for persona in PERSONAS}
    payloads = {}
    maps = {}
    sources = {}
    for persona, original in originals.items():
        validate_persona(persona, release=original)
        converted, maps[persona] = normalize(original)
        sources[persona] = _source_metadata(original, root)
        history = load_yaml(original["checkpoint"] / "life_sim.yaml")
        history["sessions"] = converted["sessions"]
        facts = load_yaml(original.get("facts_path", original["checkpoint"] / "facts.yaml"))
        facts["facts"] = list(converted["facts"].values())
        for name, value in (("life_sim", history), ("facts", facts)):
            payloads[f"registry/personas/{persona}/{name}.yaml"] = yaml.safe_dump(
                value, sort_keys=False, allow_unicode=True
            ).encode("utf-8")
        hashes = original["manifest"].get("published_sha256", original["manifest"]["candidate_sha256"])
        for test_id, expected_sha in hashes.items():
            path = root / "tests" / persona / f"{test_id}.yaml"
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != expected_sha:
                raise ValueError(f"Published test changed during conversion: {persona}/{test_id}")
            payloads[f"tests/{persona}/{test_id}.yaml"] = content
        shared_state = root / "tests" / persona / "state.json"
        if shared_state.is_file():
            payloads[f"tests/{persona}/state.json"] = shared_state.read_bytes()
    payloads["session_map.json"] = (json.dumps(maps, indent=2) + "\n").encode("utf-8")
    if set(payloads) != _files(root):
        raise ValueError("Incomplete release output")
    output.mkdir(parents=True)
    for relative, content in payloads.items():
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest = {"format": "single_message_release", "sources": sources,
                "files": {name: hashlib.sha256(content).hexdigest()
                          for name, content in payloads.items()}}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    verify(output, root=root)
    return manifest


def verify(output: Path, *, root: Path = ROOT) -> dict[str, dict[str, Any]]:
    """Reload the copy and verify it against the still-authenticated originals."""
    manifest = json.loads((output / "manifest.json").read_text())
    expected_files = _files(root)
    actual_files = {str(path.relative_to(output)) for path in output.rglob("*") if path.is_file()}
    if (manifest.get("format") != "single_message_release"
            or set(manifest.get("files", {})) != expected_files
            or actual_files != expected_files | {"manifest.json"}):
        raise ValueError("Unexpected or missing release files")
    for name, digest in manifest["files"].items():
        if sha256(output / name) != digest:
            raise ValueError(f"Converted file changed: {name}")
    maps = json.loads((output / "session_map.json").read_text())
    if set(maps) != set(PERSONAS) or set(manifest["sources"]) != set(PERSONAS):
        raise ValueError("Release must cover all three personas")
    converted = {}
    for persona in PERSONAS:
        original = load_release(persona, root=root)
        if manifest["sources"][persona] != _source_metadata(original, root):
            raise ValueError(f"{persona}: original release changed")
        history = load_yaml(output / "registry/personas" / persona / "life_sim.yaml")
        facts = load_yaml(output / "registry/personas" / persona / "facts.yaml")
        for field, payload, name in (("sessions", history, "life_sim"), ("facts", facts, "facts")):
            source_path = (original.get("facts_path", original["checkpoint"] / "facts.yaml")
                           if name == "facts" else original["checkpoint"] / f"{name}.yaml")
            source_payload = load_yaml(source_path)
            if {k: v for k, v in payload.items() if k != field} != {
                k: v for k, v in source_payload.items() if k != field
            }:
                raise ValueError(f"{persona}: {name} metadata changed")
        fact_rows = facts.get("facts", [])
        if len({row["id"] for row in fact_rows}) != len(fact_rows):
            raise ValueError(f"{persona}: duplicate fact IDs")
        hashes = original["manifest"].get("published_sha256", original["manifest"]["candidate_sha256"])
        for test_id, digest in hashes.items():
            path = output / "tests" / persona / f"{test_id}.yaml"
            if sha256(path) != digest:
                raise ValueError(f"{persona}/{test_id}: test bytes changed")
        tests = [load_test(path) for path in sorted((output / "tests" / persona).glob("*.yaml"))]
        result = {**original, "sessions": history["sessions"],
                  "facts": {row["id"]: row for row in fact_rows}, "tests": tests}
        verify_equivalence(original, result, maps[persona])
        validate_persona(persona, release=result)
        converted[persona] = result
    return converted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--check-ingestion", action="append", default=[], metavar="PERSONA=FILE",
                        help="Check saved Hermes seed_calls against the new session IDs; no provider calls")
    args = parser.parse_args()
    if args.verify_only:
        releases = verify(args.out)
        counts = {persona: len(release["sessions"]) for persona, release in releases.items()}
    else:
        manifest = prepare(args.out)
        counts = {persona: row["sessions"] for persona, row in manifest["sources"].items()}
    ingestion = []
    maps = json.loads((args.out / "session_map.json").read_text())
    for item in args.check_ingestion:
        persona, separator, filename = item.partition("=")
        if not separator or persona not in PERSONAS:
            parser.error("--check-ingestion must be PERSONA=FILE")
        records = json.loads(Path(filename).read_text())["seed_calls"]
        links = link_ingestion(maps[persona], records)
        ingestion.append({"persona": persona, "file": filename, "matched_sessions": len(links)})
    print(json.dumps({"single_message_sessions": counts, "unchanged_tests": 600,
                      "verified": True, "ingestion_checks": ingestion,
                      "output": str(args.out.resolve())}, indent=2))


if __name__ == "__main__":
    main()
