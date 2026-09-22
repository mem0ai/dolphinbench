"""Local preparation of exact approved release edits; never accepts or runs tests."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from authoring.context import ROOT, dump_json, load_checkpoint_context, load_config
from graders.explicit import validate_checks
from harness.task_schema import TestSpec
from harness.dataset import load_test


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bound(path: Path, digest: str) -> bytes:
    data = path.read_bytes()
    if _sha(data) != digest:
        raise ValueError(f"revision input hash mismatch: {path}")
    return data


def replay_edits(original: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """Replay the approval's ordered operations without interpreting criterion prose."""
    revised = copy.deepcopy(original)
    if row["owner"] not in {"grading", "request_writing"}:
        raise ValueError("release revisions permit only grading or request corrections")
    if not row["edits_in_order"]:
        raise ValueError("release revision has no edits")
    for edit in row["edits_in_order"]:
        location = edit["path"]
        operation = edit.get("operation")
        parent: Any
        key: Any
        if location == "test" and row["owner"] == "request_writing":
            parent, key = revised, "test"
            if operation is not None:
                raise ValueError("request edits cannot specify a grading operation")
        elif location == "grade.config":
            parent, key = revised["grade"], "config"
            if operation != "bind_target_and_content_same_call":
                raise ValueError("whole-config edits require an explicit same-call repair")
        else:
            match = re.fullmatch(r"grade\.config\.assertions\[(\d+)\](\.criterion)?", location)
            if match is None:
                raise ValueError(f"release revision field is not permitted: {location}")
            parent = revised["grade"]["config"]["assertions"]
            key = int(match[1])
            if match[2]:
                parent, key = parent[key], "criterion"
                if operation is not None:
                    raise ValueError("criterion edits cannot specify a list operation")
            elif operation != "remove_duplicate_check" or edit["after"] is not None:
                raise ValueError("whole-check edits may only remove an approved duplicate")
        if parent[key] != edit["before"] or edit["before"] == edit["after"]:
            raise ValueError(f"release revision before-value mismatch or no-op: {location}")
        if operation == "remove_duplicate_check":
            del parent[key]
        else:
            parent[key] = copy.deepcopy(edit["after"])
    before_inputs, after_inputs = copy.deepcopy(original), copy.deepcopy(revised)
    before_inputs.pop("grade")
    after_inputs.pop("grade")
    if row["owner"] == "request_writing":
        if before_inputs.pop("test") == after_inputs.pop("test"):
            raise ValueError("a request correction must change the request")
    if before_inputs != after_inputs:
        raise ValueError("release revision changed non-approved agent inputs")
    return revised


def prepare_release_revision(
    *, config_path: Path, proposal_path: Path, approved_sha256: str,
    out: Path, root: Path = ROOT,
) -> dict[str, Any]:
    """Stage a separate 200-file candidate and retain immutable source bindings."""
    out = out.resolve()
    if out.exists():
        raise ValueError("release revision preparation requires a fresh output directory")
    proposal_bytes = _bound(proposal_path, approved_sha256)
    proposal = json.loads(proposal_bytes)
    config = load_config(config_path)
    context = load_checkpoint_context(config)
    tests_dir = (root / "tests" / config.persona).resolve()
    release_path = root / "authoring" / "release_manifests" / f"{config.persona}.json"
    release_bytes = release_path.read_bytes()
    release = json.loads(release_bytes)
    expected = {f"{i:03d}" for i in range(1, 201)}
    original_hashes = proposal["published_tests_before_sha256"]
    if (set(original_hashes) != expected
            or release.get("published_sha256", release.get("candidate_sha256")) != original_hashes
            or release.get("test_count") != 200
            or release.get("test_ids") != list(range(1, 201))
            or release.get("published") is not True
            or release.get("persona") != config.persona
            or release.get("checkpoint_identity") != context.checkpoint_identity
            or release.get("evaluation_date") != config.evaluation_date):
        raise ValueError("approved originals do not match the current complete frozen release")
    if {p.name for p in tests_dir.glob("*.yaml")} != {f"{i}.yaml" for i in expected}:
        raise ValueError("published test directory must contain exactly the 200 approved files")
    originals = {i: _bound(tests_dir / f"{i}.yaml", original_hashes[i]) for i in sorted(expected)}
    candidates = dict(originals)
    changed: dict[str, dict[str, Any]] = {}
    input_bindings = {str(config_path.resolve()): _sha(config_path.read_bytes()),
                      str(release_path.resolve()): _sha(release_bytes),
                      str(proposal_path.resolve()): approved_sha256}
    shared_state = tests_dir / "state.json"
    shared_bytes = shared_state.read_bytes() if shared_state.is_file() else None
    if shared_bytes is not None:
        input_bindings[str(shared_state)] = _sha(shared_bytes)
    for row in proposal["tests"]:
        test_id = row["test_id"]
        if test_id not in expected or test_id in changed:
            raise ValueError("revision IDs must be distinct members of the published release")
        original_path = (root / row["original_path"]).resolve()
        if (original_path != tests_dir / f"{test_id}.yaml"
                or row["original_sha256"] != original_hashes[test_id]):
            raise ValueError("revision original is not its bound published test")
        proposed_path = (root / row["proposed_path"]).resolve()
        proposed_bytes = _bound(proposed_path, row["proposed_sha256"])
        input_bindings[str(proposed_path)] = row["proposed_sha256"]
        for name, digest in row["evidence_sha256"].items():
            path = (root / name).resolve()
            _bound(path, digest)
            input_bindings[str(path)] = digest
        original, proposed = load_test(original_path), yaml.safe_load(proposed_bytes)
        if replay_edits(original, row) != proposed:
            raise ValueError(f"unapproved changes in proposed test {test_id}")
        spec = TestSpec.model_validate(proposed)
        if str(spec.id).zfill(3) != test_id:
            raise ValueError("proposed test identity differs from the approved release")
        grading = proposed["grade"]["config"]
        if grading.get("check_version") == 2:
            validate_checks(grading["assertions"])
        candidates[test_id] = proposed_bytes
        changed[test_id] = {
            "owner": row["owner"], "original_sha256": original_hashes[test_id],
            "candidate_sha256": _sha(proposed_bytes),
            "status": "preflight_pending", "accepted": False,
            "certification": ("new_execution_after_paid_approval" if row["owner"] == "request_writing"
                              else "saved_execution_authentication_pending"),
            "additional_writer_calls_allowed": 0,
        }
    request_count = sum(row["owner"] == "request_writing" for row in changed.values())
    counts = {"changed_tests": len(changed), "request_changes": request_count,
              "grading_only_tests": len(changed) - request_count,
              "unchanged_tests": 200 - len(changed)}
    if not changed or any(proposal["counts"].get(k) != v for k, v in counts.items()):
        raise ValueError("approved revision counts do not match its exact edit set")
    protected = [tests_dir, release_path.parent, proposal_path.resolve().parent]
    if any(out == p or out.is_relative_to(p) or p.is_relative_to(out) for p in protected):
        raise ValueError("revision output overlaps protected source paths")
    manifest = {
        "version": 1, "kind": "prepared_exact_release_revision", "status": "validation_pending",
        "persona": config.persona, "checkpoint_identity": context.checkpoint_identity,
        "evaluation_date": config.evaluation_date, "approved_proposal_sha256": approved_sha256,
        "source_bindings": input_bindings, "original_test_sha256": original_hashes,
        "candidate_test_sha256": {i: _sha(data) for i, data in candidates.items()},
        "counts": counts, "changed_tests": changed, "accepted": False,
        "paid_calls_made": 0, "publication_authorized": False,
        "evaluation_launch_authorized": False,
    }
    # Finish every validation before writing; these are candidates, never an accepted batch.
    (out / "tests").mkdir(parents=True)
    (out / "source_tests").mkdir()
    if shared_bytes is not None:
        for folder in (out / "tests", out / "source_tests"):
            (folder / "state.json").write_bytes(shared_bytes)
    for test_id, data in candidates.items():
        (out / "tests" / f"{test_id}.yaml").write_bytes(data)
        (out / "source_tests" / f"{test_id}.yaml").write_bytes(originals[test_id])
    (out / "source_release.json").write_bytes(release_bytes)
    (out / "approved_proposal.json").write_bytes(proposal_bytes)
    (out / "source_config.yaml").write_bytes(config_path.read_bytes())
    dump_json(out / "revision.json", manifest)
    return manifest
