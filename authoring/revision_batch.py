"""Accepted exact-release batches without fabricating a new planning response."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import yaml

from authoring.revision_execution import read_bound
from authoring.revision_review import validate_revision_certification
from harness.durable_json import atomic_json
from harness.test_spec_schema import TestSpec


def _binding(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def write_revision_batch(*, inputs: dict[str, Any], manifest: dict[str, Any], out: Path) -> Path:
    directory = out / "accepted_batch"
    if (directory / "provenance.json").is_file():
        return directory
    if manifest["status"] != "validated" or set(manifest["results"]) != set(inputs["edits"]):
        raise ValueError("an exact release batch requires all approved corrections to pass")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "candidates").mkdir(exist_ok=True)
    if ((directory / "validation_decision.json").exists()
            and json.loads((directory / "validation_decision.json").read_text()) != manifest):
        raise ValueError("unfinished accepted batch belongs to a different validation decision")
    atomic_json(directory / "validation_decision.json", manifest)
    plan = {
        "kind": "exact_release_revision", "version": 1,
        "persona": inputs["revision"]["persona"],
        "checkpoint_identity": inputs["revision"]["checkpoint_identity"],
        "evaluation_date": inputs["revision"]["evaluation_date"],
        "revision": inputs["approval"]["revision"],
        "validation_decision": _binding(directory / "validation_decision.json"),
        "inherited_release": _binding(inputs["stage"] / "source_release.json"),
    }
    results = []
    for test_id in sorted(inputs["candidates"]):
        source = inputs["stage"] / "tests" / f"{test_id}.yaml"
        read_bound(source, inputs["revision"]["candidate_test_sha256"][test_id])
        target = directory / "candidates" / source.name
        shutil.copyfile(source, target)
        results.append({"test_id": int(test_id), "status": "accepted_initial_final_review",
                        "candidate": str(target.resolve()), "candidate_sha256": _binding(target)["sha256"]})
    atomic_json(directory / "planning_batch.json", plan)
    atomic_json(directory / "provenance.json", {
        "checkpoint_identity": plan["checkpoint_identity"], "accepted_test_ids": list(range(1, 201)),
        "source_manifest": str((out / "manifest.json").resolve()), "results": results,
        "resumed_from": {"inherited_release": plan["inherited_release"],
                         "inherited_test_ids": sorted(set(inputs["candidates"]) - set(inputs["edits"])),
                         "revised_test_ids": sorted(inputs["edits"])},
    })
    return directory


def collect_revision_batch(*, directory: Path, context: Any, config: Any) -> dict[int, Path]:
    plan = json.loads((directory / "planning_batch.json").read_text())
    if plan.get("kind") != "exact_release_revision" or plan.get("version") not in (1, 2):
        raise ValueError("unsupported exact release batch")
    for key, expected in (("persona", context.persona), ("checkpoint_identity", context.checkpoint_identity),
                          ("evaluation_date", config.evaluation_date)):
        if plan.get(key) != expected:
            raise ValueError(f"exact release batch has different {key}")
    revision = json.loads(read_bound(Path(plan["revision"]["path"]), plan["revision"]["sha256"]))
    if any(revision.get(key) != plan[key] for key in ("persona", "checkpoint_identity", "evaluation_date")):
        raise ValueError("exact release revision identity differs from its batch")
    inherited = json.loads(read_bound(Path(plan["inherited_release"]["path"]), plan["inherited_release"]["sha256"]))
    decision = json.loads(read_bound(Path(plan["validation_decision"]["path"]), plan["validation_decision"]["sha256"]))
    replacements = {}
    if plan["version"] == 2:
        approval = json.loads(read_bound(Path(plan["release_mapping"]["path"]), plan["release_mapping"]["sha256"]))
        if approval.get("approved") is not True or not approval.get("approval_record"):
            raise ValueError("release mapping requires explicit approval")
        replacements = approval.get("replacements")
        if not isinstance(replacements, dict) or not replacements:
            raise ValueError("release mapping must name its replacement candidates")
        if not set(replacements).issubset(revision["changed_tests"]):
            raise ValueError("release mapping may replace only pending revised tests")
        if any(decision["results"][i].get("accepted") for i in replacements):
            raise ValueError("release mapping may not replace an accepted revision")
    if inherited.get("candidate_sha256") != revision["original_test_sha256"]:
        raise ValueError("inherited acceptance does not cover the original published release")
    if (decision.get("status") not in ({"validated", "validation_pending"} if replacements else {"validated"})
            or set(decision["results"]) != set(revision["changed_tests"])
            or decision.get("revision") != plan["revision"]):
        raise ValueError("exact release validation does not cover every correction")
    provenance = json.loads((directory / "provenance.json").read_text())
    if provenance.get("accepted_test_ids") != list(range(1, 201)) or len(provenance.get("results", [])) != 200:
        raise ValueError("exact release provenance must cover exactly 200 tests")
    recorded = {f"{int(row['test_id']):03d}": row for row in provenance["results"]}
    if len(recorded) != 200 or set(recorded) != set(revision["candidate_test_sha256"]):
        raise ValueError("exact release candidate identities differ from provenance")
    paths = sorted((directory / "candidates").glob("*.yaml"))
    if {path.stem for path in paths} != set(recorded):
        raise ValueError("exact release has missing or unexpected candidates")
    candidates = {}
    for path in paths:
        test_id = path.stem
        replacement = replacements.get(test_id)
        digest = replacement["release_sha256"] if replacement else revision["candidate_test_sha256"][test_id]
        spec = TestSpec.model_validate(yaml.safe_load(read_bound(path, digest)))
        if (int(spec.id) != int(test_id) or spec.narrative_anchor_date != config.evaluation_date
                or recorded[test_id].get("candidate_sha256") != digest
                or recorded[test_id].get("status") != ("accepted_clean_certification" if replacement else "accepted_initial_final_review")):
            raise ValueError("exact release candidate differs from accepted provenance")
        if replacement:
            _validate_clean_replacement(replacement, spec, context=context, config=config)
        elif test_id in decision["results"]:
            outcome = decision["results"][test_id]
            if not outcome.get("accepted") or outcome.get("candidate_sha256") != digest:
                raise ValueError("revised test did not pass validation")
            test_out = Path(outcome["output_dir"])
            final_request = json.loads((test_out / "final_review_request.json").read_text())
            step = json.loads((test_out / "final_review" / "model" / "step.json").read_text())
            response = json.loads(read_bound(test_out / "final_review" / "model" / "result.json", step["result_sha256"]))
            if (step.get("status") != "completed" or step["inputs"]["request"] != final_request
                    or response.get("decision") != "approve" or response.get("issues")
                    or final_request["user_request"] != spec.test
                    or final_request["exact_grading_config"] != spec.grade.config
                    or final_request["starting_app_data"] != spec.mock_state):
                raise ValueError("revised test does not match its accepting final review")
            validate_revision_certification(final_request)
        elif digest != revision["original_test_sha256"][test_id]:
            raise ValueError("an inherited test changed without revised acceptance")
        candidates[int(test_id)] = path
    return candidates


def _validate_clean_replacement(record: dict[str, Any], spec: TestSpec, *, context: Any, config: Any) -> None:
    from authoring.context import load_authoring_tasks
    from authoring.create_tests import _clean_certification_evidence

    def source(key: str) -> Path:
        binding = record[key]
        path = Path(binding["path"])
        read_bound(path, binding["sha256"])
        return path

    original_path = source("candidate")
    original = yaml.safe_load(original_path.read_text())
    if str(original["id"]) != str(record["candidate_id"]):
        raise ValueError("release mapping candidate ID changed")
    mapped = dict(original, id=spec.id)
    if TestSpec.model_validate(mapped) != spec:
        raise ValueError("release mapping changed more than the candidate ID")
    tasks = load_authoring_tasks(context=context, config=config, path=source("planning_batch"))
    if len(tasks) != 1 or list(tasks[0].fact_ids) != list(spec.load_bearing_facts):
        raise ValueError("release mapping does not match its accepted plan")
    provenance = json.loads(source("provenance").read_text())
    if (provenance.get("accepted_test_ids") != [record["candidate_id"]]
            or len(provenance.get("results", [])) != 1
            or provenance["results"][0].get("candidate_sha256") != record["candidate"]["sha256"]
            or provenance["results"][0].get("status") != "accepted_clean_certification"):
        raise ValueError("release mapping lacks matching accepted provenance")
    clean, evidence = _clean_certification_evidence(
        candidate=original_path, gate=source("gate"), planned_task=tasks[0],
        preflight_gate=source("preflight_gate"),
    )
    if not clean:
        raise ValueError(f"release mapping lacks clean certification: {evidence['reasons']}")
