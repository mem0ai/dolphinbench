"""Local correction of a pending production review misrecorded as rejection."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from authoring.context import dump_json, load_authoring_tasks, load_checkpoint_context, load_config
from authoring.production_review import ProductionReview
from authoring.progress import save_progress


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reconcile_pending_reviews(*, config_path: Path, plan_path: Path, source_run: Path,
                              out: Path, test_ids: list[int]) -> dict[str, Any]:
    from authoring.create_tests import _copy_authenticated_tree, _progress_resume_inputs, _verify_saved_resume_files
    from authoring.propose import _state_test_summaries, _state_entry_path, _state_path, _write_json_atomic, load_persona_state

    source_run, out = source_run.resolve(), out.resolve()
    if out.exists() or out.is_relative_to(source_run):
        raise ValueError("status reconciliation requires a fresh directory outside its source")
    if not test_ids or len(set(test_ids)) != len(test_ids) or any(type(i) is not int or i <= 0 for i in test_ids):
        raise ValueError("status reconciliation requires distinct test IDs")
    config = load_config(config_path)
    context = load_checkpoint_context(config)
    state = load_persona_state(config=config, context=context)
    if plan_path.resolve() not in state["approved_plan_paths"]:
        raise ValueError("status reconciliation requires the original approved plan")
    accepted = _state_test_summaries(config=config, context=context, state=state)
    if set(test_ids).intersection(int(row["test_id"]) for row in accepted):
        raise ValueError("status reconciliation cannot select a test accepted in another batch")
    tasks = load_authoring_tasks(context=context, config=config, path=plan_path)
    verified = _progress_resume_inputs(config_path=config_path, plan_path=plan_path, source_run=source_run,
        test_ids=test_ids, config=config, context=context, tasks=tasks, allow_approved_correction=True)
    if verified is None:
        raise ValueError("status reconciliation requires authenticated production progress")
    records, _, identity = verified
    decisions = {}
    for test_id, record in records.items():
        progress = record["progress"]
        if progress.get("workflow") != "production" or progress["status"] != "replacement_required" or record["was_accepted"]:
            raise ValueError("only a misclassified unaccepted production rejection can be reconciled")
        directory = source_run / "final_trace_reviews" / f"{test_id:03d}" / progress["attempt_name"]
        decision_path, request_path = directory / "decision.json", directory / "request.json"
        if any(str(p) not in record["authenticated_files"] for p in (decision_path, request_path)):
            raise ValueError("status reconciliation lacks its authenticated review")
        saved = json.loads(decision_path.read_text())
        decision = ProductionReview.model_validate({k: v for k, v in saved.items() if k != "request_sha256"})
        request = json.loads(request_path.read_text())
        if decision.decision != "pending" or saved["request_sha256"] != _hash(request_path) or request["test_id"] != test_id:
            raise ValueError("the saved review does not establish this pending decision")
        decisions[test_id] = decision

    state_path = _state_path(config, None)
    state_hash = _hash(state_path)
    original_state = json.loads(state_path.read_text())
    rejected_path = source_run / "rejected_tests.json"
    original_rejected = json.loads(rejected_path.read_text())
    identity["authenticated_files"][str(rejected_path)] = _hash(rejected_path)
    rejected_ids = {row["test_id"] for row in original_rejected["rejected_tests"]}
    if not set(test_ids).issubset(rejected_ids):
        raise ValueError("the source rejection record does not contain every selected test")
    entries = original_state.get("rejected_test_files", [])
    positions = [i for i, entry in enumerate(entries)
                 if _state_entry_path(state_path=state_path, entry=entry, key="path") == rejected_path]
    if len(positions) != 1 or entries[positions[0]]["sha256"] != _hash(rejected_path):
        raise ValueError("the source rejection file is not the current authenticated planning record")

    out.mkdir(parents=True)
    _copy_authenticated_tree(source_run, out, identity["authenticated_files"], model_caches=True)
    inputs = out / "authoring/part_01/run_inputs.json"
    old_inputs = json.loads(Path(records[test_ids[0]]["progress"]["authoring_inputs"]).read_text())
    dump_json(inputs, {**old_inputs, "selected_test_ids": test_ids,
                      "selected_plan_positions": [records[i]["progress"]["plan_position"] for i in test_ids]})
    authored = []
    for test_id, record in records.items():
        row = copy.deepcopy(record["progress"])
        row.update(status="review_pending", source_status="replacement_required",
                   reason="; ".join(issue.problem for issue in decisions[test_id].issues),
                   next_action="preserve_pending_review_without_new_calls")
        for key in ("candidate", "gate", "authoring_response"):
            if row.get(key):
                row[key] = str(out / Path(row[key]).relative_to(source_run))
        proposal = out / "authoring/part_01/proposals" / f"{test_id:03d}"
        review = out / "final_trace_reviews" / f"{test_id:03d}"
        artifacts = [*proposal.rglob("*"), *review.rglob("*"), out / "runtime_policy.json"]
        authored.append(save_progress(path=out / "progress" / f"{test_id:03d}.json", result=row,
                                      inputs_path=inputs, artifact_paths=[p for p in artifacts if p.is_file()]))
    dump_json(out / "rejected_tests.json", {"rejected_tests": [row for row in original_rejected["rejected_tests"]
                                                               if row["test_id"] not in decisions]})
    dump_json(out / "pending_tests.json", {"pending_tests": authored})
    dump_json(out / "persona_state_before.json", original_state)
    manifest = {"status": "review_pending", "status_reconciliation": True, "provider_calls": 0,
                "test_ids": test_ids, "candidate_count": len(test_ids), "final_review_accepted": 0,
                "checkpoint_identity": context.checkpoint_identity, "plan_path": str(plan_path.resolve()),
                "resume_new_test_run": identity, "authoring_results": authored, "final_review_results": [],
                "accepted_batch": None, "accepted_candidates": [], "promoted": False}
    dump_json(out / "manifest.json", manifest)
    _verify_saved_resume_files(identity)
    if _hash(state_path) != state_hash:
        raise ValueError("persona state changed during local status reconciliation")
    updated = copy.deepcopy(original_state)
    updated["rejected_test_files"][positions[0]] = {
        "path": str(out / "rejected_tests.json"), "sha256": _hash(out / "rejected_tests.json")}
    _write_json_atomic(state_path, updated)
    return manifest
