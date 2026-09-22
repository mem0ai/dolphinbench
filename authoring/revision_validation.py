"""Prepare bounded exact-release validation through the public authoring command."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from authoring.context import ROOT, dump_json, load_checkpoint_context, load_config
from authoring.revision_inputs import load_release_validation_inputs
from authoring.revision_review import build_revision_review_request, grading_identity


def prepare_release_validation(
    *, config_path: Path, approval_path: Path, out: Path, root: Path = ROOT,
) -> dict[str, Any]:
    if out.exists():
        raise ValueError("release validation preparation requires a fresh output directory")
    config = load_config(config_path)
    context = load_checkpoint_context(config)
    inputs = load_release_validation_inputs(
        root=root, approval_path=approval_path, config_path=config_path, context=context)
    requests = {}
    for test_id, edit in sorted(inputs["edits"].items()):
        requests[test_id] = build_revision_review_request(
            original=inputs["originals"][test_id], candidate=inputs["candidates"][test_id],
            approved_edit=edit, context=context,
            legacy_execution_note=inputs["approval"]["legacy_reuse_approval"]
            if test_id in inputs["gates"] else None,
        )
    request_ids = sorted(i for i in requests if i not in inputs["gates"])
    grading_ids = sorted(inputs["gates"])
    example_count = sum(1 + 2 * len(r["required_example_check_ids"])
                        + len(r["required_split_action_ids"]) for r in requests.values())
    manifest = {
        "version": 1, "kind": "exact_release_validation_plan", "status": "paid_approval_pending",
        "approval": {"path": str(approval_path.resolve()),
                     "sha256": hashlib.sha256(approval_path.read_bytes()).hexdigest()},
        "checkpoint_identity": context.checkpoint_identity,
        "grading_identity": grading_identity(), "grading_only_test_ids": grading_ids,
        "changed_request_test_ids": request_ids, "review_requests": {},
        "planned_work": {
            "preflight_reviews": len(requests), "grading_example_sets": example_count,
            "saved_certification_answers_to_regrade": 4 * len(grading_ids),
            "new_certification_attempts": 4 * len(request_ids),
            "final_reviews_max": len(requests),
            "writer_calls": 0, "additional_corrections": 0,
        },
        "legacy_execution_limitation": inputs["approval"]["legacy_reuse_approval"]["limitation"],
        "cost_note": "Use existing usage records; an upfront cost calculation is not required.",
        "paid_calls_made": 0, "accepted": False, "publication_authorized": False,
        "evaluation_launch_authorized": False,
    }
    out.mkdir(parents=True)
    for test_id, request in requests.items():
        path = out / "review_requests" / f"{test_id}.json"
        dump_json(path, request)
        manifest["review_requests"][test_id] = {
            "path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    dump_json(out / "validation_plan.json", manifest)
    return manifest


def execute_release_validation(
    *, config_path: Path, approval_path: Path, out: Path, concurrency: int = 4,
    confirm_paid_calls: bool = False, root: Path = ROOT, review_context_path: Path | None = None,
) -> dict[str, Any]:
    if not confirm_paid_calls:
        raise ValueError("release validation requires explicit paid-call approval")
    if not 1 <= concurrency <= 4:
        raise ValueError("revision concurrency must be between one and four")
    from authoring.complete_test import certification_input
    from harness.environment import call_ledger as _call_ledger
    from authoring.revision_execution import _atomic_json, run_once
    from authoring.revision_workflow import validate_revision_test
    from authoring.revision_inputs import load_review_context
    from authoring.revision_review import REVISION_REVIEW_SYSTEM
    from authoring.revision_review_recovery import (
        REVIEW_SCOPE_INSTRUCTIONS, FINAL_REVIEW_CONTEXT_INSTRUCTIONS,
        bind_review_recovery, recover_final_review,
    )
    from construction.llm import AzureJsonClient
    from graders.mechanical import grade_tool_trace
    from authoring.certify import certify_candidate, verify_gate_python

    config = load_config(config_path)
    context = load_checkpoint_context(config)
    inputs = load_release_validation_inputs(
        root=root, approval_path=approval_path, config_path=config_path, context=context)
    review_context = load_review_context(path=review_context_path, approval_path=approval_path,
                                        context=context, changed_ids=set(inputs["edits"]))
    verify_gate_python()
    execution = {
        "checkpoint_identity": context.checkpoint_identity,
        "oracle_model": "gpt-5.4", "oracle_max_turns": 10,
        "code": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
            "harness/oracle.py", "harness/mining/preview.py", "harness/environment.py", "authoring/_gate_shot.py",
            "authoring/certify.py", "construction/llm.py", "authoring/revision_workflow.py",
        )},
    }
    identity = {"approval_sha256": hashlib.sha256(approval_path.read_bytes()).hexdigest(),
                "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
                "execution": execution, "grading": grading_identity()}
    recovery = None
    previous_results = {}
    previous_dirs = {}
    selected = set(review_context)
    if review_context:
        previous_results = json.loads((out / "manifest.json").read_text())["results"]
        if set(previous_results) != set(inputs["edits"]):
            raise ValueError("review recovery requires the complete prior validation manifest")
        recovery = bind_review_recovery(out=out, identity=identity, context_path=review_context_path)
        for test_id, recorded in previous_results.items():
            previous_out = Path(recorded.get("output_dir", out / "tests" / test_id)).resolve()
            if not previous_out.is_relative_to((out / "tests").resolve()):
                raise ValueError("previous review output is outside this validation run")
            previous = json.loads((previous_out / "outcome.json").read_text())
            if previous != {key: value for key, value in recorded.items() if key != "output_dir"}:
                raise ValueError("previous review outcome differs from its saved manifest")
            previous_dirs[test_id] = previous_out
            if test_id not in selected:
                continue
            saved_context = None
            for path in (previous_out / "final_review_request.json",
                         previous_out / "preflight" / "model" / "step.json"):
                if path.is_file():
                    saved = json.loads(path.read_text())
                    saved_context = saved.get("review_context", saved.get("inputs", {}).get("request", {}).get("review_context"))
                    if saved_context is not None:
                        break
            if saved_context == review_context[test_id]:
                selected.remove(test_id)
                continue
            if previous.get("accepted") or previous.get("stage") != review_context[test_id].get("review_stage", "preflight"):
                raise ValueError("review context must identify the exact pending review stage")
    else:
        run_once(directory=out / "inputs", identity=identity, action=lambda: identity)
    results = {}

    def validate_one(test_id: str) -> dict[str, Any]:
        candidate = inputs["candidates"][test_id]
        test_out = previous_dirs.get(test_id, out / "tests" / test_id)
        if review_context and test_id not in selected:
            previous = json.loads((test_out / "outcome.json").read_text())
            digest = hashlib.sha256((inputs["stage"] / "tests" / f"{test_id}.yaml").read_bytes()).hexdigest()
            if previous.get("candidate_sha256") != digest:
                raise ValueError("preserved revision result belongs to a different candidate")
            return {**previous, "output_dir": str(test_out.resolve())}
        request = build_revision_review_request(
            original=inputs["originals"][test_id], candidate=candidate,
            approved_edit=inputs["edits"][test_id], context=context,
            legacy_execution_note=inputs["approval"]["legacy_reuse_approval"]
            if test_id in inputs["gates"] else None)
        review_system = None
        previous_out = test_out
        if test_id in selected:
            request["review_context"] = review_context[test_id]
            if review_context[test_id].get("review_scope") == "approved_delta_v1":
                review_system = REVISION_REVIEW_SYSTEM + REVIEW_SCOPE_INSTRUCTIONS
            suffix = hashlib.sha256(json.dumps({"context": review_context[test_id],
                "previous_out": str(previous_out.resolve())}, sort_keys=True).encode()).hexdigest()[:16]
            test_out = out / "tests" / f"{test_id}_context_{suffix}"
        if test_id in selected and review_context[test_id].get("review_stage") == "final_review":
            result = recover_final_review(
                candidate_path=inputs["stage"] / "tests" / f"{test_id}.yaml",
                previous_out=previous_out, out=test_out, review_context=review_context[test_id],
                client=AzureJsonClient(config.designer_model, reasoning_effort="high"),
                system=(review_system or REVISION_REVIEW_SYSTEM) + FINAL_REVIEW_CONTEXT_INSTRUCTIONS,
                confirm_paid_calls=True)
            return {**result, "output_dir": str(test_out.resolve())}
        result = validate_revision_test(
            candidate_path=inputs["stage"] / "tests" / f"{test_id}.yaml",
            request=request, saved_gate=inputs["gates"].get(test_id),
            oracle_input=None if test_id in inputs["gates"] else certification_input(
                context, SimpleNamespace(fact_ids=candidate["load_bearing_facts"]), config.evaluation_date),
            out=test_out, checkpoint=context.checkpoint_path, persona=config.persona,
            client=AzureJsonClient(config.designer_model, reasoning_effort="high"),
            grader=grade_tool_trace, certifier=certify_candidate,
            execution_identity=execution, confirm_paid_calls=True, review_system=review_system)
        return {**result, "output_dir": str(test_out.resolve())}

    with _call_ledger(out / "call_ledger.jsonl"):
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(validate_one, test_id): test_id for test_id in sorted(inputs["edits"])}
            for future in as_completed(futures):
                test_id = futures[future]
                try:
                    results[test_id] = future.result()
                except Exception as exc:
                    results[test_id] = {"test_id": int(test_id), "accepted": False,
                                        "status": "pending", "error": f"{type(exc).__name__}: {exc}"}
                _atomic_json(out / "progress.json", results)
                print(json.dumps({"test_id": test_id, "status": results[test_id]["status"],
                                  "completed": len(results), "total": len(futures)}), flush=True)
    manifest = {
        "kind": "exact_release_validation", "version": 1,
        "status": "validated" if all(row["accepted"] for row in results.values()) else "validation_pending",
        "inputs": identity, "revision": inputs["approval"]["revision"],
        "approved_revisions": {"path": str(approval_path.resolve()), "sha256": identity["approval_sha256"]},
        "results": results, "accepted_count": sum(row["accepted"] for row in results.values()),
        "preserved_test_count": 200 - len(results), "published": False,
        "review_recovery": recovery,
    }
    _atomic_json(out / "manifest.json", manifest)
    if manifest["status"] == "validated":
        from authoring.revision_batch import write_revision_batch
        manifest["accepted_batch"] = str(write_revision_batch(inputs=inputs, manifest=manifest, out=out).resolve())
        _atomic_json(out / "manifest.json", manifest)
    if any(row["accepted"] for row in results.values()):
        from authoring.revision_evaluation import load_evaluation_answers, regrade_evaluation_answers
        sources = load_evaluation_answers(inputs=inputs, root=root)
        with _call_ledger(out / "call_ledger.jsonl"):
            manifest["evaluation_regrading"] = regrade_evaluation_answers(
                inputs=inputs, sources=sources, out=out / "evaluation",
                validated_test_ids={test_id for test_id, row in results.items() if row["accepted"]})
        _atomic_json(out / "manifest.json", manifest)
    return manifest
