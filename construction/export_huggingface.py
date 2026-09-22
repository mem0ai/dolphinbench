#!/usr/bin/env python3
"""Export the verified DolphinBench release for Hugging Face Datasets."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
PERSONAS = ("morgan", "alex", "riley")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _verified_files(root: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("format") != "single_message_release":
        raise ValueError("Expected the accepted single-message DolphinBench release")
    verified: dict[str, bytes] = {}
    for relative, expected in manifest["files"].items():
        path = (root / relative).resolve()
        path.relative_to(root.resolve())
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f"Release hash mismatch: {relative}")
        verified[relative] = raw
    return manifest, verified


def build_rows(root: Path = ROOT) -> dict[str, list[dict[str, Any]]]:
    manifest, verified = _verified_files(root)
    registry = yaml.safe_load((root / "registry/personas.yaml").read_text())
    names = {row["id"]: row["name"] for row in registry["personas"]}
    rows: dict[str, list[dict[str, Any]]] = {"messages": [], "facts": [], "tests": []}

    for persona in PERSONAS:
        base = f"registry/personas/{persona}"
        sessions = yaml.safe_load(verified[f"{base}/life_sim.yaml"])["sessions"]
        facts = yaml.safe_load(verified[f"{base}/facts.yaml"])["facts"]
        if len(sessions) != manifest["sources"][persona]["sessions"]:
            raise ValueError(f"{persona}: history count differs from manifest")

        message_ids: set[str] = set()
        for position, session in enumerate(sessions, start=1):
            message_id = str(session["id"])
            messages = session.get("messages") or []
            if message_id != f"{position:06d}" or len(messages) != 1 or not isinstance(messages[0], str):
                raise ValueError(f"{persona}: invalid released message {message_id}")
            message_ids.add(message_id)
            rows["messages"].append({
                "persona": persona,
                "persona_name": names[persona],
                "message_id": message_id,
                "date": str(session["narrative_date"]),
                "content": messages[0],
            })

        fact_ids: set[int] = set()
        for fact in facts:
            fact_id = fact["id"]
            source_ids = [str(value) for value in fact["source_session_ids"]]
            related_ids = [str(value) for value in fact.get("related_history_session_ids", [])]
            if fact_id in fact_ids or any(value not in message_ids for value in source_ids + related_ids):
                raise ValueError(f"{persona}: invalid fact {fact_id}")
            fact_ids.add(fact_id)
            rows["facts"].append({
                "persona": persona,
                "persona_name": names[persona],
                "fact_id": fact_id,
                "statement": fact["statement"],
                "applies_when": fact.get("applies_when", ""),
                "source_message_ids": source_ids,
                "related_message_ids": related_ids,
            })

        for position in range(1, 201):
            test_id = f"{position:03d}"
            relative = f"tests/{persona}/{test_id}.yaml"
            spec = yaml.safe_load(verified[relative])
            referenced_facts = spec.get("load_bearing_facts") or []
            if str(spec.get("id", "")).zfill(3) != test_id or any(value not in fact_ids for value in referenced_facts):
                raise ValueError(f"{persona}: invalid test {test_id}")
            rows["tests"].append({
                "persona": persona,
                "persona_name": names[persona],
                "test_id": test_id,
                "date": str(spec["narrative_anchor_date"]),
                "request": spec["test"],
                "required_fact_ids": referenced_facts,
                "expected_tools": spec.get("expected_tool_calls", []),
                "grading": _json(spec["grade"]),
                "app_state": _json(spec.get("mock_state", {})),
                "shared_app_state": _json(spec.get("mock_state_base")) if spec.get("mock_state_base") else "",
            })

    if len(rows["messages"]) != 13_539 or len(rows["tests"]) != 600:
        raise ValueError("Exported row counts differ from the published release")
    return rows


def dataset_card(rows: dict[str, list[dict[str, Any]]]) -> str:
    return f"""---
license: apache-2.0
language:
- en
pretty_name: DolphinBench
tags:
- benchmark
- synthetic
- agent-memory
- tool-use
- long-term-memory
- arxiv:2609.24971
configs:
- config_name: messages
  default: true
  data_files:
  - split: train
    path: messages/train.parquet
- config_name: facts
  data_files:
  - split: train
    path: facts/train.parquet
- config_name: tests
  data_files:
  - split: test
    path: tests/test.parquet
---

# DolphinBench

DolphinBench measures whether an agent can use long-term memory to take the right actions on a user's behalf. It contains three synthetic users, approximately 500,000 tokens of history per user, and 600 tool-using tests across their work and personal lives.

The benchmark grades the actions an agent takes through simulated apps, not just what it says it remembers.

## Load the dataset

```python
from datasets import load_dataset

messages = load_dataset("mem0ai/dolphinbench")
facts = load_dataset("mem0ai/dolphinbench", "facts")
tests = load_dataset("mem0ai/dolphinbench", "tests")
```

## Contents

| Configuration | Rows | Contents |
| --- | ---: | --- |
| `messages` | {len(rows['messages']):,} | Dated conversation history for each synthetic user. |
| `facts` | {len(rows['facts']):,} | Information established by the histories and the messages that establish it. |
| `tests` | {len(rows['tests']):,} | Task requests, required facts, expected tools, app state, and grading checks. |

The `grading`, `app_state`, and `shared_app_state` columns contain JSON. The complete benchmark runner, simulated apps, and release validation are maintained in the [DolphinBench GitHub repository](https://github.com/mem0ai/dolphinbench).

## Links

- [Website](https://dolphinbench.ai)
- [Paper](https://arxiv.org/abs/2609.24971)
- [GitHub](https://github.com/mem0ai/dolphinbench)

## Intended use

DolphinBench is evaluation data for comparing agent harnesses, models, and memory systems. Do not train on its histories, facts, tests, or grading criteria if you intend to report DolphinBench results.

All people, organizations, conversations, and app states in the dataset are synthetic.

## Citation

```bibtex
@article{{rathi2026dolphinbench,
  title={{DolphinBench: Mapping the Pareto Frontier of Agent Memory}},
  author={{Rathi, Soumil and Yadav, Deshraj and Singh, Taranjeet}},
  journal={{arXiv preprint arXiv:2609.24971}},
  year={{2026}}
}}
```
"""


def write_dataset(output: Path, rows: dict[str, list[dict[str, Any]]]) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise SystemExit("Install pyarrow to write the Hugging Face export") from exc

    schemas = {
        "messages": pa.schema([
            ("persona", pa.string()), ("persona_name", pa.string()), ("message_id", pa.string()),
            ("date", pa.string()), ("content", pa.string()),
        ]),
        "facts": pa.schema([
            ("persona", pa.string()), ("persona_name", pa.string()), ("fact_id", pa.int64()),
            ("statement", pa.string()), ("applies_when", pa.string()),
            ("source_message_ids", pa.list_(pa.string())), ("related_message_ids", pa.list_(pa.string())),
        ]),
        "tests": pa.schema([
            ("persona", pa.string()), ("persona_name", pa.string()), ("test_id", pa.string()),
            ("date", pa.string()), ("request", pa.string()), ("required_fact_ids", pa.list_(pa.int64())),
            ("expected_tools", pa.list_(pa.string())), ("grading", pa.string()),
            ("app_state", pa.string()), ("shared_app_state", pa.string()),
        ]),
    }
    paths = {"messages": output / "messages/train.parquet", "facts": output / "facts/train.parquet",
             "tests": output / "tests/test.parquet"}
    for name, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(rows[name], schema=schemas[name])
        pq.write_table(table, path, compression="zstd", write_page_index=True)
    (output / "README.md").write_text(dataset_card(rows))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = build_rows(args.root.resolve())
    write_dataset(args.output.resolve(), rows)
    print(f"Exported {len(rows['messages']):,} messages, {len(rows['facts']):,} facts, and {len(rows['tests']):,} tests")


if __name__ == "__main__":
    main()
