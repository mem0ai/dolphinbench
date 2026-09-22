"""Project the hash-verified release into the website; never modify its sources."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from harness.dataset import load_test
PERSONAS = ("morgan", "alex", "riley")
PROFILES = {
    "morgan": {"role": "Founder & CEO", "organization": "Scaffold", "accent": "#315efb",
               "summary": "Company operations, fundraising, product decisions, and everyday personal requests."},
    "alex": {"role": "Infrastructure engineer", "organization": "Sphere", "accent": "#087a55",
             "summary": "Infrastructure migrations, incident response, team coordination, and life outside work."},
    "riley": {"role": "Growth & product operator", "organization": "Helio", "accent": "#af4a6c",
              "summary": "Product experiments, customer retention, campaign decisions, and personal commitments."},
}


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def project(root: Path) -> tuple[dict, dict[str, bytes]]:
    manifest_bytes = (root / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("format") != "single_message_release":
        raise ValueError("The website requires the accepted single-message release")
    # Authenticate the complete release before producing any output, including
    # the session map, not just the files a particular route happens to display.
    verified = {}
    for relative, expected in manifest["files"].items():
        path = (root / relative).resolve()
        path.relative_to(root.resolve())
        raw = path.read_bytes()
        if sha(raw) != expected:
            raise ValueError(f"Release hash mismatch: {relative}")
        verified[relative] = raw
    registry = yaml.safe_load((root / "registry/personas.yaml").read_text())
    names = {row["id"]: row["name"] for row in registry["personas"]}
    output: dict[str, bytes] = {}
    summaries = []

    def emit(name: str, value: object) -> None:
        output[name] = (json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                                   allow_nan=False) + "\n").encode()

    for persona in PERSONAS:
        base = f"registry/personas/{persona}"
        source = manifest["sources"][persona]
        sessions = yaml.load(verified[f"{base}/life_sim.yaml"], Loader=yaml.CSafeLoader)["sessions"]
        raw_facts = yaml.load(verified[f"{base}/facts.yaml"], Loader=yaml.CSafeLoader)["facts"]
        if len(sessions) != source["sessions"]:
            raise ValueError(f"{persona}: history count mismatch")
        history = []
        for position, row in enumerate(sessions, start=1):
            if (str(row["id"]) != f"{position:06d}" or len(row["messages"]) != 1
                    or not isinstance(row["messages"][0], str)):
                raise ValueError(f"{persona}: invalid single-message session")
            history.append({"id": str(row["id"]), "date": row["narrative_date"],
                            "content": row["messages"][0], "source_test_ids": [], "context_test_ids": []})
        by_session = {row["id"]: row for row in history}
        facts = {}
        for row in raw_facts:
            fact_id = row["id"]
            if fact_id in facts:
                raise ValueError(f"{persona}: duplicate fact {fact_id}")
            fact = {"id": fact_id, "statement": row["statement"], "applies_when": row.get("applies_when", ""),
                    "source_session_ids": [str(value) for value in row["source_session_ids"]],
                    "related_history_session_ids": [str(value) for value in row.get("related_history_session_ids", [])]}
            for field in ("source_session_ids", "related_history_session_ids"):
                if any(session_id not in by_session for session_id in fact[field]):
                    raise ValueError(f"{persona}: unresolved {field} for fact {fact_id}")
            facts[fact_id] = fact
        tests = []
        source_links = defaultdict(set)
        context_links = defaultdict(set)
        expected_files = {f"{i:03d}.yaml" for i in range(1, 201)}
        if {path.name for path in (root / "tests" / persona).glob("*.yaml")} != expected_files:
            raise ValueError(f"{persona}: expected exactly 200 test files")
        for position in range(1, 201):
            test_id = f"{position:03d}"
            relative = f"tests/{persona}/{test_id}.yaml"
            raw = verified[relative]
            spec = load_test(root / relative, read_bytes=lambda path: verified[
                str(path.resolve().relative_to(root.resolve()))])
            # Downloads are self-contained, even when the repository shares state.
            download = (yaml.dump(spec, Dumper=yaml.CSafeDumper, sort_keys=False,
                                  allow_unicode=True).encode()
                        if "mock_state_base" in yaml.load(raw, Loader=yaml.CSafeLoader) else raw)
            if str(spec["id"]).zfill(3) != test_id or not spec["load_bearing_facts"]:
                raise ValueError(f"{persona}: invalid test {test_id}")
            for fact_id in spec["load_bearing_facts"]:
                if fact_id not in facts:
                    raise ValueError(f"{persona}/{test_id}: unknown fact {fact_id}")
                for session_id in facts[fact_id]["source_session_ids"]:
                    source_links[session_id].add(test_id)
                for session_id in facts[fact_id]["related_history_session_ids"]:
                    context_links[session_id].add(test_id)
            tests.append({"id": test_id, "date": spec["narrative_anchor_date"], "request": spec["test"],
                          "fact_ids": spec["load_bearing_facts"], "tools": spec.get("expected_tool_calls", []),
                          "grade": spec["grade"], "sha256": sha(download)})
            emit(f"tests/{persona}/{test_id}-state.json", spec.get("mock_state", {}))
            output[relative] = download
        for row in history:
            row["source_test_ids"] = sorted(source_links[row["id"]])
            row["context_test_ids"] = sorted(context_links[row["id"]] - source_links[row["id"]])
        months = Counter(row["date"][:7] for row in history)
        summary = {"id": persona, "name": names[persona], **PROFILES[persona],
                   "profile_source": f"{base}/persona_sheet.md", "start": history[0]["date"],
                   "end": history[-1]["date"], "evaluation_date": tests[0]["date"],
                   "messages": len(history), "facts": len(facts), "tests": len(tests),
                   "tokens": source["user_message_tokens"],
                   "tools": sorted({tool for test in tests for tool in test["tools"]}),
                   "months": [{"month": month, "messages": count} for month, count in sorted(months.items())],
                   "checkpoint_sha256": source["checkpoint_identity"],
                   "history_sha256": manifest["files"][f"{base}/life_sim.yaml"],
                   "facts_sha256": manifest["files"][f"{base}/facts.yaml"]}
        summaries.append(summary)
        emit(f"{persona}.json", {"tests": tests, "facts": facts})
        emit(f"{persona}-history.json", history)
    index = {"schema_version": 2, "release_sha256": sha(manifest_bytes), "personas": summaries,
             "messages": sum(row["messages"] for row in summaries), "tests": sum(row["tests"] for row in summaries),
             "tokens": sum(row["tokens"] for row in summaries)}
    emit("index.json", index)
    return index, output


def build(root: Path, out: Path) -> dict:
    index, files = project(root)
    for relative, raw in files.items():
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(raw)
        temporary.replace(target)
    return index


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=HERE.parent)
    parser.add_argument("--out", type=Path, default=HERE / "public/data")
    args = parser.parse_args()
    index = build(args.root.resolve(), args.out.resolve())
    print(f"Verified website data: {len(index['personas'])} personas, {index['tests']} tests, {index['messages']} messages")
