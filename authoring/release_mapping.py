"""Local-only release numbering with immutable accepted source bindings."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from authoring.context import dump_json


def binding(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def read_binding(value: dict) -> Path:
    path = Path(value["path"])
    if binding(path) != value:
        raise ValueError("release mapping source changed")
    return path


def mapped_bytes(source: Path, release_id: int) -> bytes:
    data = yaml.safe_load(source.read_text())
    if int(data["id"]) == release_id:
        return source.read_bytes()
    data["id"] = f"{release_id:03d}"
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode()


def validate_map(*, path: Path, candidates: dict[int, Path], identity: dict,
                 expected_count: int) -> dict:
    raw = json.loads(path.read_text())
    if (not isinstance(raw, dict) or set(raw) != {
            "version", "persona", "checkpoint_identity", "evaluation_date", "tests"} or raw["version"] != 1):
        raise ValueError("release map has an invalid shape")
    for key in ("persona", "checkpoint_identity", "evaluation_date"):
        if raw[key] != identity[key]:
            raise ValueError(f"release map has a different {key}")
    rows = raw["tests"]
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise ValueError("release map must select exactly the required test count")
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"release_id", "candidate_id", "candidate_sha256"}
                or any(type(row[key]) is not int or row[key] <= 0 for key in ("release_id", "candidate_id"))):
            raise ValueError("release map contains an invalid entry")
        source = candidates.get(row["candidate_id"])
        if source is None or binding(source)["sha256"] != row["candidate_sha256"]:
            raise ValueError("release map does not name the exact accepted candidate")
    if ({row["release_id"] for row in rows} != set(range(1, expected_count + 1))
            or len({row["candidate_id"] for row in rows}) != expected_count):
        raise ValueError("release map must use distinct candidates and contiguous release positions")
    return raw


def write_mapped_batches(*, directory: Path, manifest: dict, candidates: dict[int, Path]) -> list[dict]:
    """Derive numbered copies only after publication is explicitly confirmed."""
    if directory.exists():
        raise ValueError("mapped release provenance directory already exists")
    directory.mkdir(parents=True)
    mapping = manifest["release_mapping"]
    by_source = {row["candidate_id"]: row for row in mapping["tests"]}
    summaries = []
    for index, batch in enumerate(manifest["batches"], 1):
        source = Path(batch["directory"])
        if (binding(source / "planning_batch.json")["sha256"] != batch["planning_batch_sha256"]
                or binding(source / "provenance.json")["sha256"] != batch["provenance_sha256"]):
            raise ValueError("accepted batch changed during publication")
        plan = json.loads((source / "planning_batch.json").read_text())
        provenance = json.loads((source / "provenance.json").read_text())
        selected = [(source_id, task) for source_id, task in zip(
            provenance["accepted_test_ids"], plan["tasks"], strict=True) if source_id in by_source]
        if not selected:
            continue
        selected.sort(key=lambda pair: by_source[pair[0]]["release_id"])
        target = directory / f"batch_{index:03d}"
        (target / "candidates").mkdir(parents=True)
        results, bindings = [], []
        source_results = {row["test_id"]: row for row in provenance["results"]}
        for source_id, task in selected:
            row = by_source[source_id]
            release_id = row["release_id"]
            original = candidates[release_id]
            if binding(original)["sha256"] != row["candidate_sha256"]:
                raise ValueError("accepted source changed during publication")
            candidate = target / "candidates" / f"{release_id:03d}.yaml"
            candidate.write_bytes(mapped_bytes(original, release_id))
            digest = binding(candidate)["sha256"]
            if digest != manifest["candidate_sha256"][f"{release_id:03d}"]:
                raise ValueError("mapped candidate changed during publication")
            results.append({"test_id": release_id, "status": source_results[source_id]["status"],
                            "candidate": str(candidate), "candidate_sha256": digest})
            bindings.append({**row, "source_candidate": binding(original)})
        mapped_plan = copy.deepcopy(plan)
        mapped_plan["tasks"] = [task for _, task in selected]
        dump_json(target / "planning_batch.json", mapped_plan)
        dump_json(target / "provenance.json", {
            "checkpoint_identity": plan["checkpoint_identity"], "accepted_test_ids": [row["test_id"] for row in results],
            "source_manifest": provenance["source_manifest"], "results": results,
            "resumed_from": {"release_mapping": {
                "source_plan": binding(source / "planning_batch.json"),
                "source_provenance": binding(source / "provenance.json"), "tests": bindings,
                "approved_mapping": manifest["release_mapping_binding"],
            }},
        })
        summaries.append({"directory": str(target), "planning_batch_sha256": binding(target / "planning_batch.json")["sha256"],
                          "provenance_sha256": binding(target / "provenance.json")["sha256"],
                          "accepted_test_ids": [row["test_id"] for row in results]})
    return summaries


def validate_mapped_batch(*, directory: Path, plan: dict, provenance: dict) -> None:
    if not isinstance(provenance, dict) or not isinstance(provenance.get("resumed_from", {}), dict):
        return
    record = (provenance.get("resumed_from") or {}).get("release_mapping")
    if record is None:
        return
    original_plan = json.loads(read_binding(record["source_plan"]).read_text())
    original_provenance = json.loads(read_binding(record["source_provenance"]).read_text())
    approved = json.loads(read_binding(record["approved_mapping"]).read_text())
    original_ids = original_provenance["accepted_test_ids"]
    original_results = {row["test_id"]: row for row in original_provenance["results"]}
    tasks = []
    if len(record["tests"]) != len(provenance["results"]):
        raise ValueError("mapped provenance has incomplete source coverage")
    for row, result in zip(record["tests"], provenance["results"], strict=True):
        selection = {key: row[key] for key in ("release_id", "candidate_id", "candidate_sha256")}
        if selection not in approved["tests"] or row["release_id"] != result["test_id"]:
            raise ValueError("mapped candidate does not match the approved release position")
        source = read_binding(row["source_candidate"])
        original = original_results[row["candidate_id"]]
        candidate = directory / "candidates" / f"{row['release_id']:03d}.yaml"
        if (original["candidate_sha256"] != row["candidate_sha256"]
                or row["source_candidate"]["sha256"] != row["candidate_sha256"]
                or original["status"] != result["status"]
                or int(yaml.safe_load(source.read_text())["id"]) != row["candidate_id"]
                or candidate.read_bytes() != mapped_bytes(source, row["release_id"])):
            raise ValueError("release mapping changed accepted content or evidence")
        tasks.append(original_plan["tasks"][original_ids.index(row["candidate_id"])])
    if dict(original_plan, tasks=tasks) != plan:
        raise ValueError("release mapping changed an accepted plan")
