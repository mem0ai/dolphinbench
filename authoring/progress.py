"""Authenticated per-test progress shared by writing, review, and resume."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from authoring.context import dump_json


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_progress(
    *, path: Path, result: dict[str, Any], inputs_path: Path,
    artifact_paths: list[Path],
) -> dict[str, Any]:
    """Snapshot completed files, never a mutable ledger or another test's work."""
    saved = copy.deepcopy(result)
    if inputs_path.is_file():
        saved.update(
            progress_version=1,
            authoring_inputs=str(inputs_path.resolve()),
            authoring_inputs_sha256=_sha256(inputs_path),
            artifact_hashes={
                str(artifact.resolve()): _sha256(artifact)
                for artifact in artifact_paths
                if artifact.is_file() and artifact.resolve() != path.resolve()
            },
        )
    dump_json(path, saved)
    return saved


def authoring_artifacts(directory: Path, attempt_name: str) -> list[Path]:
    files = list(directory.glob(f"{attempt_name}_*"))
    files.append(directory / "reserved_execution_retry.json")
    files.extend((directory / f"{attempt_name}_preflight").rglob("*.json"))
    files.append(directory / "work" / f"{attempt_name}_authoring_response_cache.json")
    request = directory / f"{attempt_name}_authoring_request.json"
    if request.is_file():
        bound = json.loads(request.read_text()).get("saved_execution_gate") or {}
        if isinstance(bound.get("file"), str) and Path(bound["file"]).name == bound["file"]:
            files.append(directory / bound["file"])
    return [path for path in files if path.is_file()]


def cached_authoring_response(files: dict[str, str], attempt_name: str) -> tuple[dict[str, Any], Path] | None:
    """Recover a completed response when interruption preceded response.json."""
    from construction.runtime_model_calls import request_hash

    for value in files:
        cache = Path(value)
        if cache.name != f"{attempt_name}_authoring_response_cache.json":
            continue
        directory = cache.parent.parent
        request = directory / f"{attempt_name}_authoring_request.json"
        system = directory / f"{attempt_name}_authoring_system.txt"
        schema = directory / f"{attempt_name}_authoring_schema.json"
        if not all(str(path.resolve()) in files for path in (request, system, schema)):
            raise ValueError("saved authoring cache has no authenticated request, system, or schema")
        model_request = directory / f"{attempt_name}_authoring_request.md"
        payload = json.loads(request.read_text())
        if model_request.is_file():
            if str(model_request.resolve()) not in files:
                raise ValueError("saved authoring cache has no authenticated rendered request")
            payload = model_request.read_text(encoding="utf-8")
        identity = request_hash(system.read_text().removesuffix("\n"), payload,
                                **json.loads(schema.read_text()))
        saved = json.loads(cache.read_text())
        if saved.get("request_sha256") != identity or not isinstance(saved.get("response"), dict):
            raise ValueError("saved authoring cache does not match its authenticated request")
        return saved["response"], directory
    return None


def load_progress(
    *, source_run: Path, test_id: int, config_path: Path, plan_path: Path,
    config: Any, context: Any, tasks: list[Any],
    record_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, str]] | None:
    """Load the furthest saved stage after checking its plan and every saved file."""
    source_run = source_run.resolve()
    review_dir = source_run / "final_trace_reviews" / f"{test_id:03d}"
    paths = [review_dir / "result.json", review_dir / "progress.json", source_run / "progress" / f"{test_id:03d}.json"]
    for directory in sorted((source_run / "authoring").glob(f"part_*/proposals/{test_id:03d}")):
        paths.extend((directory / "status.json", directory / "progress.json"))
    if record_path is not None:
        if record_path.resolve() not in {path.resolve() for path in paths}:
            raise ValueError("requested progress record is not a canonical record for this test")
        paths = [record_path]
    records = []
    for path in paths:
        if not path.is_file():
            continue
        record = json.loads(path.read_text())
        if record.get("progress_version") == 1:
            records.append((path, record))
    if not records:
        return None

    def rank(entry: tuple[Path, dict[str, Any]]) -> tuple[int, int, int]:
        path, record = entry
        status = str(record.get("status") or "")
        explicit_stages = {
            "authoring_pending": 10, "correction_pending": 10,
            "validation_pending": 20, "local_validation": 20,
            "preflight_pending": 30, "certification_pending": 40,
            "review_required": 50, "review_pending": 50,
        }
        stage = explicit_stages.get(status) or (
            60 if status.startswith(("accepted_", "replacement_"))
            else 50 if "final_review" in record or "initial_final_review" in record or "review" in status
            else 40 if "certif" in status or status.startswith("certified_")
            else 30 if "preflight" in status
            else 20 if "validation" in status else 10
        )
        generation = int(record.get("model_corrections_used") or 0) + int(record.get("execution_reruns_used") or 0)
        # Stage queues own the durable correction count and terminal decision.
        # A writer's intermediate status must not outrank that same generation.
        if record.get("workflow") in {"staged", "production"} and path == source_run / "progress" / f"{test_id:03d}.json":
            stage = 100
        terminal = int(path.name in {"status.json", "result.json"})
        return generation, stage, terminal + (2 if stage < 50 and record.get("attempt_name") else 0)

    path, record = max(records, key=rank)
    if record.get("id") != test_id:
        raise ValueError("saved progress has a different test ID")
    inputs_path = Path(record["authoring_inputs"]).resolve()
    if not inputs_path.is_relative_to(source_run):
        raise ValueError("saved authoring inputs point outside the source run")
    if not inputs_path.is_file() or _sha256(inputs_path) != record["authoring_inputs_sha256"]:
        raise ValueError("saved authoring inputs changed")
    inputs = json.loads(inputs_path.read_text())
    correction_limit = 1
    if inputs.get("runtime_policy") is not None:
        from authoring.runtime_policy import CreationPolicy
        correction_limit = CreationPolicy.model_validate(inputs["runtime_policy"]).max_model_corrections
    for key in ("model_corrections_used", "execution_reruns_used"):
        count = record.get(key, 0)
        limit = correction_limit if key == "model_corrections_used" else 1
        if type(count) is not int or not 0 <= count <= limit:
            raise ValueError(f"saved progress has an invalid {key}")
    production_count = record.get("production_corrections_used", 0)
    if type(production_count) is not int or not 0 <= production_count <= min(1, record.get("model_corrections_used", 0)):
        raise ValueError("saved progress has an invalid production_corrections_used")
    if (
        inputs.get("checkpoint_identity") != context.checkpoint_identity
        or inputs.get("persona") != config.persona
        or inputs.get("evaluation_date") != config.evaluation_date
        or Path(str(inputs.get("config_path") or "")).resolve() != config_path.resolve()
        or inputs.get("config_sha256") != _sha256(config_path)
        or Path(str(inputs.get("plan_path") or "")).resolve() != plan_path.resolve()
        or inputs.get("plan_sha256") != _sha256(plan_path)
    ):
        raise ValueError("saved progress does not match the current approved inputs")
    ids = inputs.get("selected_test_ids") or []
    positions = inputs.get("selected_plan_positions") or []
    if (
        len(ids) != len(set(ids)) or len(ids) != len(positions) or ids.count(test_id) != 1
        or any(not isinstance(position, int) or isinstance(position, bool) or not 1 <= position <= len(tasks) for position in positions)
    ):
        raise ValueError("saved progress has invalid plan positions")
    position = positions[ids.index(test_id)]
    task = tasks[position - 1]
    if record.get("fact_ids", task.fact_ids) != task.fact_ids:
        raise ValueError("saved progress refers to different planned facts")
    authenticated = {
        str(path.resolve()): _sha256(path),
        str(inputs_path): _sha256(inputs_path),
        str(config_path.resolve()): _sha256(config_path),
        str(plan_path.resolve()): _sha256(plan_path),
    }
    artifacts = record.get("artifact_hashes")
    if not isinstance(artifacts, dict):
        raise ValueError("saved progress has no artifact hashes")
    for value, expected in artifacts.items():
        artifact = Path(value).resolve()
        if not artifact.is_relative_to(source_run):
            raise ValueError("saved progress artifact points outside the source run")
        if not artifact.is_file() or _sha256(artifact) != expected:
            raise ValueError(f"saved progress artifact changed: {artifact}")
        authenticated[str(artifact)] = expected
    for key in ("candidate", "gate", "authoring_response"):
        if record.get(key):
            artifact = Path(record[key]).resolve()
            if str(artifact) not in authenticated:
                raise ValueError(f"saved progress {key} is not authenticated")
    return {**record, "plan_position": position}, authenticated
