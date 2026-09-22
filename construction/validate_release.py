#!/usr/bin/env python3
"""Validate release-level DolphinBench corpus invariants without running the oracle."""

from __future__ import annotations

import sys
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from construction.checkpoints import validated_checkpoint_identity  # noqa: E402
from registry.facts import FactRegistryError, load_facts, load_sessions  # noqa: E402
from harness.dataset import load_test, spec_sha256  # noqa: E402
PERSONAS = ("morgan", "alex", "riley")
def load_yaml(path: Path) -> Any:
    return yaml.load(path.read_text(), Loader=yaml.CSafeLoader) or {}


def user_message_texts(session: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    for msg in session.get("messages") or []:
        if isinstance(msg, str):
            parts.append(msg)
        elif isinstance(msg, dict):
            role = msg.get("role") or "user"
            if role == "user":
                parts.append(msg.get("content") or msg.get("text") or "")
    return [p for p in parts if p]


def token_count(texts: list[str]) -> int:
    try:
        import tiktoken
    except ImportError as exc:  # pragma: no cover - release env should have it.
        raise SystemExit("tiktoken is required for release validation") from exc
    enc = tiktoken.get_encoding("o200k_base")
    return sum(len(enc.encode(t)) for t in texts)


def fail(msg: str) -> None:
    raise SystemExit(f"release validation failed: {msg}")


def load_release(persona: str, *, root: Path = ROOT) -> dict[str, Any]:
    """Resolve published tests against their authenticated accepted checkpoint."""
    if persona not in PERSONAS:
        raise ValueError(f"unknown release persona: {persona}")
    manifest_path = root / "authoring" / "release_manifests" / f"{persona}.json"
    manifest = json.loads(manifest_path.read_text())
    config = load_yaml(root / "authoring" / "configs" / f"{persona}.yaml")
    checkpoint = (root / config["checkpoint"]).resolve()
    checkpoint.relative_to(root.resolve())
    if (manifest.get("persona") != persona or config.get("persona") != persona
            or manifest.get("published") is not True or manifest.get("test_count") != 200
            or manifest.get("test_ids") != list(range(1, 201))
            or manifest.get("evaluation_date") != config.get("evaluation_date")):
        raise ValueError(f"{persona}: inconsistent published release metadata")
    identity = validated_checkpoint_identity(checkpoint)
    if identity != manifest.get("checkpoint_identity"):
        raise ValueError(f"{persona}: checkpoint differs from the published release")
    metadata = json.loads((checkpoint / "checkpoint.json").read_text())
    budget = metadata.get("session_budget", {})
    if (metadata.get("identity_version") != 2
            or budget.get("selection") != "first complete session at or above target"
            or metadata.get("progress", {}).get("construction_path") != "deterministic_release_cutoff"
            or budget.get("actual_tokens_o200k_base") != metadata.get("history_tokens_o200k_base")
            or not isinstance(budget.get("target_tokens_o200k_base"), int)
            or budget["target_tokens_o200k_base"] <= 0
            or budget["actual_tokens_o200k_base"] < budget["target_tokens_o200k_base"]):
        raise ValueError(f"{persona}: checkpoint is not an authenticated release cutoff")
    expected = {f"{i:03d}" for i in range(1, 201)}
    hashes = manifest.get("published_sha256", manifest.get("candidate_sha256", {}))
    paths = sorted((root / "tests" / persona).glob("*.yaml"))
    if set(hashes) != expected or {p.stem for p in paths} != expected:
        raise ValueError(f"{persona}: release must contain exactly 200 bound tests")
    tests = []
    for path in paths:
        if hashlib.sha256(path.read_bytes()).hexdigest() != hashes[path.stem]:
            raise ValueError(f"{persona}/{path.name}: published test hash mismatch")
        spec = load_test(path)
        if "evaluated_spec_sha256" in manifest:
            if spec_sha256(spec) != manifest["evaluated_spec_sha256"].get(path.stem):
                raise ValueError(f"{persona}/{path.name}: evaluated input mismatch")
        tests.append(spec)
    facts_path = checkpoint / "facts.yaml"
    source_facts = None
    if registry := manifest.get("fact_registry"):
        facts_path = manifest_path.with_suffix("") / "facts.yaml"
        source_path = facts_path.with_name("source_facts.json")
        for path, digest in ((facts_path, registry["sha256"]),
                             (source_path, registry["source_facts_sha256"])):
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError(f"{persona}: published fact evidence hash mismatch: {path.name}")
        source_facts = json.loads(source_path.read_text())
    facts = {row["id"]: row for row in load_yaml(facts_path)["facts"]}
    if source_facts is not None:
        sessions = {str(row["id"]): row for row in load_sessions(persona, checkpoint=checkpoint)}
        if len(source_facts) != len(facts) or {row["id"] for row in source_facts} != set(facts):
            raise ValueError(f"{persona}: published fact evidence coverage differs")
        for row in source_facts:
            fact = facts[row["id"]]
            if (fact["statement"] != row["statement"].strip()
                    or fact["source_session_ids"] != list(dict.fromkeys(s["session_id"] for s in row["sources"]))):
                raise ValueError(f"{persona}: fact {row['id']} differs from source evidence")
            for ref in row["sources"]:
                session = sessions[ref["session_id"]]
                index = ref["message_index"]
                if (type(index) is not int or not 0 <= index < len(session["messages"])
                        or session["narrative_date"] != ref["narrative_date"]):
                    raise ValueError(f"{persona}: fact {row['id']} has invalid message evidence")
    return {"persona": persona, "manifest": manifest, "checkpoint": checkpoint,
            "metadata": metadata, "tests": tests,
            "facts": facts, "facts_path": facts_path, "source_facts": source_facts,
            "sessions": load_sessions(persona, checkpoint=checkpoint)}


def validate_persona(persona: str, *, release: dict[str, Any] | None = None) -> tuple[int, int, int]:
    try:
        release = release or load_release(persona)
    except (ValueError, OSError, KeyError, FactRegistryError) as exc:
        fail(str(exc))
    sessions = release["sessions"]
    session_id_list = [str(s.get("id")) for s in sessions]
    session_ids = set(session_id_list)
    if len(session_id_list) != len(session_ids):
        fail(f"{persona}: session ids are not unique")
    narrative_dates = [str(s.get("narrative_date") or "") for s in sessions]
    if narrative_dates != sorted(narrative_dates):
        fail(f"{persona}: sessions are not ordered by narrative_date")

    facts = release["facts"]
    for fact_id, fact in facts.items():
        referenced = [
            *(fact.get("source_session_ids") or []),
            *(fact.get("related_history_session_ids") or []),
        ]
        missing = [session_id for session_id in referenced if session_id not in session_ids]
        if missing:
            fail(f"{persona}: fact {fact_id} references unknown sessions {missing}")

    texts = [text for session in sessions for text in user_message_texts(session)]
    tokens = token_count(texts)
    expected_tokens = release["metadata"]["history_tokens_o200k_base"]
    if tokens != expected_tokens:
        fail(f"{persona}: expected {expected_tokens} tokens, found {tokens}")

    check_count = 0
    llm_count = 0
    deterministic_count = 0
    for idx, data in enumerate(release["tests"], start=1):
        expected_id = f"{idx:03d}"
        path = Path(f"{expected_id}.yaml")
        raw_id = data.get("id")
        try:
            numeric_id = int(raw_id)
        except (TypeError, ValueError):
            fail(f"{persona}/{path.name}: id field is {raw_id!r}")
        if isinstance(raw_id, bool) or numeric_id != idx:
            fail(f"{persona}/{path.name}: id field is {data.get('id')!r}")
        if not str(data.get("test") or "").strip():
            fail(f"{persona}/{path.name}: missing test query")

        if "seeds" in data:
            fail(f"{persona}/{path.name}: legacy seeds field is forbidden")
        references = data.get("load_bearing_facts") or []
        if not references:
            fail(f"{persona}/{path.name}: missing load_bearing_facts")
        if not all(isinstance(fact_id, int) and not isinstance(fact_id, bool) for fact_id in references):
            fail(f"{persona}/{path.name}: fact references must be numeric ids")
        unknown = [fact_id for fact_id in references if fact_id not in facts]
        if unknown:
            fail(f"{persona}/{path.name}: unknown fact ids {unknown}")
        assertions = ((data.get("grade") or {}).get("config") or {}).get("assertions") or []
        if not assertions:
            fail(f"{persona}/{path.name}: missing grade assertions")
        for assertion in assertions:
            check_count += 1
            if assertion.get("type") == "field_llm_judge":
                llm_count += 1
            else:
                deterministic_count += 1

    return check_count, llm_count, deterministic_count


def main() -> None:
    total = llm = deterministic = 0
    sessions = tokens = 0
    for persona in PERSONAS:
        try:
            release = load_release(persona)
        except (ValueError, OSError, KeyError) as exc:
            fail(str(exc))
        c, l, d = validate_persona(persona, release=release)
        sessions += len(release["sessions"])
        tokens += release["metadata"]["history_tokens_o200k_base"]
        total += c
        llm += l
        deterministic += d

    print(
        "release validation passed: "
        f"{len(PERSONAS) * 200} tests, {sessions:,} sessions, {tokens:,} user-message tokens, "
        f"{total:,} checks ({llm:,} model-judged, {deterministic:,} deterministic), "
        "published test hashes and accepted checkpoints verified"
    )


if __name__ == "__main__":
    main()
