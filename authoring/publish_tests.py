"""Validate and publish exactly one complete persona test set."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Sequence

import yaml

from authoring.context import (
    ROOT,
    dump_json,
    load_authoring_tasks,
    load_checkpoint_context,
    load_config,
)
from authoring.propose import reset_persona_state_after_publish
from harness.task_schema import TestSpec


ACCEPTED_STATUSES = {
    "accepted_clean_certification",
    "accepted_initial_final_review",
    "accepted_after_final_review_correction",
    "accepted_after_continuation_review",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ValueError(f"required file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _test_id(value: object, *, path: Path) -> int:
    try:
        test_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"candidate has an invalid test ID: {path}") from exc
    if test_id <= 0:
        raise ValueError(f"candidate test ID must be positive: {path}")
    return test_id


def collect_accepted_tests(
    *,
    config_path: Path,
    accepted_batch_dirs: Sequence[Path],
    expected_count: int = 200,
    release_map_path: Path | None = None,
) -> tuple[Any, dict[int, Path], dict[str, Any]]:
    """Validate accepted batches without changing the public tests."""
    if expected_count <= 0:
        raise ValueError("expected_count must be positive")
    if not accepted_batch_dirs:
        raise ValueError("at least one --accepted-batch is required")

    config = load_config(config_path)
    context = load_checkpoint_context(config)
    candidates: dict[int, Path] = {}
    batch_summaries: list[dict[str, Any]] = []

    for directory in accepted_batch_dirs:
        directory = directory.resolve()
        plan_path = directory / "planning_batch.json"
        candidate_dir = directory / "candidates"
        provenance_path = directory / "provenance.json"
        if not candidate_dir.is_dir():
            raise ValueError(f"accepted batch has no candidates directory: {directory}")

        if _load_json(plan_path).get("kind") == "exact_release_revision":
            if release_map_path is not None:
                raise ValueError("release numbering is supported for ordinary accepted creation batches only")
            from authoring.revision_batch import collect_revision_batch
            revised = collect_revision_batch(directory=directory, context=context, config=config)
            if set(revised).intersection(candidates):
                raise ValueError("duplicate accepted test IDs across release batches")
            candidates.update(revised)
            batch_summaries.append({"directory": str(directory), "planning_batch_sha256": _sha256(plan_path),
                                    "provenance_sha256": _sha256(provenance_path),
                                    "accepted_test_ids": sorted(revised)})
            continue

        tasks = load_authoring_tasks(
            context=context,
            config=config,
            path=plan_path,
        )

        provenance = _load_json(provenance_path)
        from authoring.release_mapping import validate_mapped_batch
        validate_mapped_batch(directory=directory, plan=_load_json(plan_path), provenance=provenance)
        required_provenance_keys = {
            "checkpoint_identity",
            "accepted_test_ids",
            "source_manifest",
            "results",
        }
        allowed_provenance_keys = required_provenance_keys | {"resumed_from"}
        if not required_provenance_keys.issubset(provenance) or not set(
            provenance
        ).issubset(allowed_provenance_keys):
            raise ValueError(f"accepted batch has invalid provenance shape: {directory}")
        if "resumed_from" in provenance and not isinstance(
            provenance["resumed_from"], dict
        ):
            raise ValueError(
                f"accepted batch has invalid resumed_from provenance: {directory}"
            )
        if provenance.get("checkpoint_identity") != context.checkpoint_identity:
            raise ValueError(f"accepted batch checkpoint does not match config: {directory}")
        accepted_ids = provenance.get("accepted_test_ids")
        source_manifest = provenance.get("source_manifest")
        results = provenance.get("results")
        if (
            not isinstance(accepted_ids, list)
            or not isinstance(source_manifest, str)
            or not source_manifest.strip()
            or not isinstance(results, list)
        ):
            raise ValueError(f"accepted batch provenance has invalid IDs, source manifest, or results: {directory}")
        if len(accepted_ids) != len(tasks) or len(results) != len(tasks):
            raise ValueError(f"accepted batch plan, IDs, and results differ in length: {directory}")
        if len(set(accepted_ids)) != len(accepted_ids):
            raise ValueError(f"accepted batch contains duplicate IDs: {directory}")

        results_by_id: dict[int, dict[str, Any]] = {}
        for result in results:
            if not isinstance(result, dict) or set(result) != {
                "test_id",
                "status",
                "candidate",
                "candidate_sha256",
            }:
                raise ValueError(f"accepted batch has a malformed result: {directory}")
            test_id = _test_id(result.get("test_id"), path=provenance_path)
            if test_id in results_by_id:
                raise ValueError(f"accepted batch has duplicate result {test_id}: {directory}")
            if result.get("status") not in ACCEPTED_STATUSES:
                raise ValueError(
                    f"test {test_id} lacks accepted certification evidence: {directory}"
                )
            if not isinstance(result.get("candidate"), str) or not result["candidate"].strip():
                raise ValueError(f"test {test_id} has no accepted candidate path: {directory}")
            candidate_path = Path(result["candidate"])
            if not candidate_path.is_file():
                raise ValueError(f"test {test_id} accepted candidate does not exist: {candidate_path}")
            candidate_sha256 = result.get("candidate_sha256")
            if not isinstance(candidate_sha256, str) or not candidate_sha256.strip():
                raise ValueError(f"test {test_id} has no accepted candidate hash: {directory}")
            if _sha256(candidate_path) != candidate_sha256:
                raise ValueError(f"test {test_id} candidate changed after review: {candidate_path}")
            results_by_id[test_id] = result

        normalized_ids = [_test_id(value, path=provenance_path) for value in accepted_ids]
        if set(normalized_ids) != set(results_by_id):
            raise ValueError(f"accepted IDs and reviewed results do not match: {directory}")

        candidate_paths = sorted(candidate_dir.glob("*.yaml"))
        if len(candidate_paths) != len(tasks):
            raise ValueError(f"accepted batch plan and candidate count differ: {directory}")
        paths_by_id: dict[int, Path] = {}
        specs_by_id: dict[int, TestSpec] = {}
        for path in candidate_paths:
            raw_candidate = yaml.safe_load(path.read_text()) or {}
            spec = TestSpec.model_validate(raw_candidate)
            test_id = _test_id(spec.id, path=path)
            if test_id in paths_by_id:
                raise ValueError(f"accepted batch contains duplicate candidate {test_id}")
            if path.stem != f"{test_id:03d}":
                raise ValueError(f"candidate filename and ID differ: {path}")
            paths_by_id[test_id] = path
            specs_by_id[test_id] = spec
        if set(normalized_ids) != set(paths_by_id):
            raise ValueError(f"accepted IDs and candidate files do not match: {directory}")

        for test_id, task in zip(normalized_ids, tasks, strict=True):
            if test_id in candidates:
                raise ValueError(f"test ID {test_id} appears in more than one accepted batch")
            spec = specs_by_id[test_id]
            if spec.narrative_anchor_date != config.evaluation_date:
                raise ValueError(f"test {test_id} uses the wrong evaluation date")
            if list(spec.load_bearing_facts) != list(task.fact_ids):
                raise ValueError(f"test {test_id} facts do not match its accepted plan")
            if list(spec.expected_tool_calls) != list(task.expected_tools):
                raise ValueError(f"test {test_id} tools do not match its accepted plan")
            candidates[test_id] = paths_by_id[test_id]
            if _sha256(candidates[test_id]) != results_by_id[test_id]["candidate_sha256"]:
                raise ValueError(f"test {test_id} batch copy differs from its accepted candidate")

        batch_summaries.append(
            {
                "directory": str(directory),
                "planning_batch_sha256": _sha256(plan_path),
                "provenance_sha256": _sha256(provenance_path),
                "accepted_test_ids": normalized_ids,
            }
        )

    expected_ids = set(range(1, expected_count + 1))
    actual_ids = set(candidates)
    if release_map_path is None and actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise ValueError(
            f"accepted tests must be exactly 1 through {expected_count}; "
            f"missing={missing}, extra={extra}"
        )

    manifest = {
        "persona": config.persona,
        "checkpoint_identity": context.checkpoint_identity,
        "evaluation_date": config.evaluation_date,
        "test_count": len(candidates),
        "test_ids": sorted(candidates),
        "batches": batch_summaries,
        "candidate_sha256": {
            f"{test_id:03d}": _sha256(candidates[test_id])
            for test_id in sorted(candidates)
        },
    }
    if release_map_path is not None:
        from authoring.release_mapping import binding, mapped_bytes, validate_map
        mapping = validate_map(path=release_map_path, candidates=candidates, identity=manifest,
                               expected_count=expected_count)
        candidates = {row["release_id"]: candidates[row["candidate_id"]] for row in mapping["tests"]}
        manifest.update(release_mapping=mapping, release_mapping_binding=binding(release_map_path),
                        test_count=expected_count, test_ids=sorted(candidates),
                        candidate_sha256={f"{i:03d}": hashlib.sha256(mapped_bytes(p, i)).hexdigest()
                                          for i, p in sorted(candidates.items())})
    return config, candidates, manifest


def publish_tests(
    *,
    config_path: Path,
    accepted_batch_dirs: Sequence[Path],
    expected_count: int = 200,
    confirm_publish: bool = False,
    release_map_path: Path | None = None,
    approved_release_map_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate all tests and optionally replace the persona's public test set."""
    config, candidates, manifest = collect_accepted_tests(
        config_path=config_path,
        accepted_batch_dirs=accepted_batch_dirs,
        expected_count=expected_count,
        release_map_path=release_map_path,
    )
    destination = ROOT / "tests" / config.persona
    manifest_path = ROOT / "authoring" / "release_manifests" / f"{config.persona}.json"
    manifest.update(
        {
            "destination": str(destination),
            "published": bool(confirm_publish),
        }
    )
    if not confirm_publish:
        return manifest
    if release_map_path is not None and not (
            approved_release_map_sha256 == _sha256(release_map_path)
            == manifest["release_mapping_binding"]["sha256"]):
        raise ValueError("publication requires the explicitly approved release-map SHA-256")

    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{config.persona}_tests_", dir=destination.parent)
    )
    backup = destination.parent / f".{config.persona}_tests_previous_{os.getpid()}"
    try:
        if release_map_path is not None:
            from authoring.release_mapping import write_mapped_batches
            mapped_dir = ROOT / "authoring" / "release_batches" / config.persona / (
                approved_release_map_sha256 + "_" + uuid.uuid4().hex[:12])
            source_batches = manifest["batches"]
            manifest["batches"] = write_mapped_batches(directory=mapped_dir, manifest=manifest, candidates=candidates)
            manifest["source_batches"] = source_batches
            candidates = {int(path.stem): path for batch in manifest["batches"]
                          for path in (Path(batch["directory"]) / "candidates").glob("*.yaml")}
        for test_id, source in sorted(candidates.items()):
            target = stage / f"{test_id:03d}.yaml"
            shutil.copy2(source, target)
            if _sha256(target) != manifest["candidate_sha256"][f"{test_id:03d}"]:
                raise ValueError("candidate changed during publication")
        staged = sorted(stage.glob("*.yaml"))
        if len(staged) != expected_count:
            raise RuntimeError("staged public test count changed during publication")
        if backup.exists():
            raise RuntimeError(f"publication backup already exists: {backup}")
        if destination.exists():
            destination.rename(backup)
        stage.rename(destination)
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        if not destination.exists() and backup.exists():
            backup.rename(destination)
        if stage.exists():
            shutil.rmtree(stage)
        raise

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    dump_json(manifest_path, manifest)
    reset_persona_state_after_publish(
        config=config, release_manifest_path=manifest_path
    )
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--accepted-batch", type=Path, action="append", required=True)
    parser.add_argument("--confirm-publish", action="store_true")
    parser.add_argument("--release-map", type=Path)
    parser.add_argument("--approved-release-map-sha256")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = publish_tests(
        config_path=args.config,
        accepted_batch_dirs=args.accepted_batch,
        expected_count=200,
        confirm_publish=args.confirm_publish,
        release_map_path=args.release_map,
        approved_release_map_sha256=args.approved_release_map_sha256,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
