"""Bound amendments and execution retries within the public revision workflow."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from authoring.context import ROOT, load_checkpoint_context, load_config
from authoring.exact_repairs import object_sha256
from authoring.release_revision import replay_edits
from authoring.revision_execution import read_bound, run_once
from authoring.revision_inputs import load_release_validation_inputs
from authoring.revision_review import grading_identity, validate_revision_certification
from harness.durable_json import atomic_json
from harness.task_schema import TestSpec


def binding(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def bound_json(record: dict[str, str], root: Path = ROOT) -> dict[str, Any]:
    return json.loads(read_bound(root / record["path"], record["sha256"]))


def completed_step(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    step = json.loads((directory / "step.json").read_text())
    if step.get("status") != "completed" or step.get("input_sha256") != object_sha256(step["inputs"]):
        raise ValueError("follow-up requires an authenticated completed step")
    return step["inputs"], json.loads(read_bound(directory / "result.json", step["result_sha256"]))


def actual_grading(identity: dict[str, Any]) -> dict[str, Any]:
    return {"judge": identity["judge"], "code": {
        key: value for key, value in identity["code"].items()
        if key.startswith("graders/") or key == "harness/environment.py"}}


def authenticate_outcome(record: dict[str, Any], candidate: dict[str, Any], digest: str) -> dict[str, Any]:
    directory = Path(record["output_dir"])
    outcome = json.loads((directory / "outcome.json").read_text())
    if outcome != {k: v for k, v in record.items() if k != "output_dir"}:
        raise ValueError("follow-up outcome differs from its bound decision")
    request = json.loads((directory / "final_review_request.json").read_text())
    step, response = completed_step(directory / "final_review" / "model")
    from authoring.review import materialize_review_quotes
    response = materialize_review_quotes({k: v for k, v in response.items() if not k.startswith("_")}, request)
    if (outcome["candidate_sha256"] != digest or step["request"] != request
            or request["test_id"] != int(candidate["id"])
            or request["user_request"] != candidate["test"]
            or request["starting_app_data"] != candidate["mock_state"]
            or request["exact_grading_config"] != candidate["grade"]["config"]
            or any(response.get(k) != outcome["final_review"].get(k)
                   for k in ("decision", "issues", "examples"))):
        raise ValueError("follow-up final review does not bind the candidate and outcome")
    if outcome["accepted"]:
        if response["decision"] != "approve" or response["issues"]:
            raise ValueError("inherited accepted outcome lacks an accepting review")
        validate_revision_certification(request)
    return request


def inherited_preflight(record: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    directory = Path(record["output_dir"])
    seen = set()
    while not (directory / "preflight" / "model" / "step.json").exists():
        if directory in seen:
            raise ValueError("cyclic preflight provenance")
        seen.add(directory)
        outcome = json.loads((directory / "outcome.json").read_text())
        recovery = outcome["review_only_recovery"]
        previous = Path(recovery["previous_out"])
        for name, digest in recovery["source_sha256"].items():
            read_bound(previous / name, digest)
        directory = previous
    step, raw = completed_step(directory / "preflight" / "model")
    before = step["request"]
    # A retry supplies full current assistant inputs, not the old transcript exception.
    comparable = copy.deepcopy(request)
    comparable["legacy_execution_note"] = before.get("legacy_execution_note")
    if not before.get("review_context") and not comparable.get("review_context", {}).get("original_messages"):
        comparable.pop("review_context", None)
    if before != comparable:
        raise ValueError("execution retry preflight inputs changed")
    response = {k: v for k, v in raw.get("review", raw).items() if not k.startswith("_")}
    if response.get("decision") != "approve" or response.get("issues"):
        raise ValueError("execution retry requires an approved preflight")
    from authoring.review import _example_calls
    from authoring.revision_review import RevisionReviewResponse, _named_detail, _comparison_passed

    review = RevisionReviewResponse.model_validate(response)
    examples = review.examples
    if (examples is None or sorted(v.check_id for v in examples.variants) != before["required_example_check_ids"]
            or sorted(v.action_id for v in examples.split_actions) != before["required_split_action_ids"]):
        raise ValueError("inherited examples do not cover the required checks")

    def grade(name: str, calls: Any) -> dict[str, Any]:
        saved, result = completed_step(directory / "examples" / name)
        expected = {"tool_calls": _example_calls(calls, before), "grading_config": before["exact_grading_config"],
                    "request": before["user_request"], "grading": actual_grading(grading_identity())}
        saved = {**saved, "grading": actual_grading(saved["grading"])}
        if saved != expected or type(result.get("passed")) is not bool or result.get("diagnostic"):
            raise ValueError("inherited example grader, settings, or inputs differ")
        return result

    cases = [{"case": "reference", "matches_expected": grade("reference", examples.correct_calls)["passed"]}]
    for index, variant in enumerate(examples.variants):
        correct = grade(f"variant_{index}_correct", variant.correct_calls)
        incorrect = grade(f"variant_{index}_incorrect", variant.incorrect_calls)
        cases.append({"case": variant.check_id, "matches_expected": bool(
            variant.correct_calls != variant.incorrect_calls and correct["passed"] and not incorrect["passed"]
            and not _comparison_passed(_named_detail(incorrect, before, variant.check_id)))})
    for index, variant in enumerate(examples.split_actions):
        result = grade(f"split_{index}", variant.calls)
        checks = [c["check_id"] for c in before["grading_checks"] if c.get("action_id") == variant.action_id]
        cases.append({"case": f"split:{variant.action_id}", "matches_expected": bool(
            checks and not result["passed"] and all(
                _comparison_passed(_named_detail(result, before, c)) for c in checks))})
    examples = {"passed": all(c["matches_expected"] for c in cases), "cases": cases}
    if not examples["passed"]:
        raise ValueError("execution retry example grades did not pass")
    return {"preflight": review.model_dump(mode="json"),
            "grading_examples": examples, "source": binding(directory / "preflight" / "model" / "step.json")}


def load_followup(*, approval_path: Path, config_path: Path, out: Path,
                  root: Path = ROOT) -> dict[str, Any]:
    approval = json.loads(approval_path.read_text())
    if (approval.get("version") != 3 or approval.get("kind") != "exact_release_revision_followup"
            or approval.get("approved") is not True
            or (root / approval["output_dir"]).resolve() != out.resolve()):
        raise ValueError("follow-up needs explicit approval and its one bound output directory")
    read_bound(root / approval["proposal"]["path"], approval["proposal"]["sha256"])
    parent_path = root / approval["parent_approval"]["path"]
    read_bound(parent_path, approval["parent_approval"]["sha256"])
    config = load_config(config_path)
    context = load_checkpoint_context(config)
    inputs = load_release_validation_inputs(root=root, approval_path=parent_path,
                                           config_path=config_path, context=context)
    parent = bound_json(approval["parent_decision"], root)
    if (parent["approved_revisions"]["sha256"] != approval["parent_approval"]["sha256"]
            or parent["inputs"]["config_sha256"] != binding(config_path)["sha256"]
            or actual_grading(parent["inputs"]["grading"]) != actual_grading(grading_identity())
            or set(parent["results"]) != set(inputs["edits"])):
        raise ValueError("follow-up parent inputs, grading, or selected revisions differ")
    for name, digest in parent["inputs"]["execution"]["code"].items():
        if name != "authoring/revision_workflow.py":
            read_bound(root / name, digest)
    selected = {r["test_id"]: r for r in approval["tests"]}
    pending = {i for i, r in parent["results"].items() if not r["accepted"]}
    if len(selected) != len(approval["tests"]) or set(selected) != pending:
        raise ValueError("follow-up must name exactly the pending tests and preserve accepted work")
    requests = {}
    for i, record in parent["results"].items():
        requests[i] = authenticate_outcome(record, inputs["candidates"][i],
                                           inputs["revision"]["candidate_test_sha256"][i])
    inherited = {i: r for i, r in parent["results"].items() if i not in selected}
    original_stage = inputs["stage"]
    requests_to_run, preflights = {}, {}
    fresh_ids = []
    from authoring.revision_review import build_revision_review_request
    for i, amendment in selected.items():
        old = inputs["candidates"][i]
        mode = amendment["mode"]
        if mode == "execution_retry":
            if amendment["edits_in_order"] or amendment.get("retry_allowance") != 1:
                raise ValueError("execution retry must be unchanged and allowed exactly once")
            candidate = old
            request = copy.deepcopy(requests[i])
            for key in ("executions", "agent_inputs", "certification_result"):
                request.pop(key, None)
            request.update(phase="before_oracle", legacy_execution_note=None)
            preflights[i] = inherited_preflight(parent["results"][i], request)
        elif mode in {"grading", "request_writing"}:
            amendment = {**amendment, "owner": mode}
            candidate = replay_edits(old, amendment)
            TestSpec.model_validate(candidate)
            edit = inputs["edits"][i]
            edit["edits_in_order"].extend(copy.deepcopy(amendment["edits_in_order"]))
            edit["reason"] += " Follow-up: " + amendment["reason"]
            edit["owner"] = mode
            request = build_revision_review_request(
                original=inputs["originals"][i], candidate=candidate, approved_edit=edit, context=context,
                legacy_execution_note=inputs["approval"]["legacy_reuse_approval"] if mode == "grading" else None)
            request["review_context"] = copy.deepcopy(requests[i].get("review_context", {}))
            request["review_context"]["review_format"] = "phase_bound_v1"
        else:
            raise ValueError("unsupported follow-up owner")
        if mode != "grading":
            fresh_ids.append(i)
        if mode == "request_writing":
            inputs["gates"].pop(i)
        inputs["candidates"][i] = candidate
        requests_to_run[i] = request
    if sorted(fresh_ids) != approval["fresh_certification_test_ids"]:
        raise ValueError("fresh certification IDs differ from the approved allowance")
    return {"approval": approval, "inputs": inputs, "config": config, "context": context,
            "parent": parent, "inherited": inherited, "selected": selected,
            "requests": requests_to_run, "preflights": preflights, "fresh_ids": fresh_ids,
            "original_stage": original_stage}


def stage_followup(*, loaded: dict[str, Any], approval_path: Path, out: Path) -> None:
    inputs = loaded["inputs"]
    original_stage = loaded["original_stage"]
    stage = out / "stage"
    identity = {"approval": binding(approval_path), "parent": loaded["approval"]["parent_decision"]}

    def stage_files() -> dict[str, Any]:
        stage.mkdir(parents=True, exist_ok=True)
        shutil.copytree(original_stage / "source_tests", stage / "source_tests")
        shutil.copyfile(original_stage / "source_release.json", stage / "source_release.json")
        (stage / "tests").mkdir()
        revision = copy.deepcopy(inputs["revision"])
        for i, candidate in inputs["candidates"].items():
            source = original_stage / "tests" / f"{i}.yaml"
            data = source.read_bytes()
            if yaml.safe_load(data) != candidate:
                data = yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True).encode()
            (stage / "tests" / f"{i}.yaml").write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            revision["candidate_test_sha256"][i] = digest
            if i in revision["changed_tests"]:
                revision["changed_tests"][i]["candidate_sha256"] = digest
                revision["changed_tests"][i]["owner"] = inputs["edits"][i]["owner"]
        revision["followup_approval"] = binding(approval_path)
        request_count = sum(r["owner"] == "request_writing" for r in inputs["edits"].values())
        revision["counts"].update(request_changes=request_count,
                                  grading_only_tests=len(inputs["edits"]) - request_count)
        atomic_json(stage / "revision.json", revision)
        return {"revision": binding(stage / "revision.json")}

    staged = run_once(directory=out / "staging", identity=identity, action=stage_files)
    inputs["revision"] = bound_json(staged["revision"])
    for i, digest in inputs["revision"]["candidate_test_sha256"].items():
        read_bound(stage / "tests" / f"{i}.yaml", digest)
    inputs["stage"] = stage
    inputs["approval"] = {**inputs["approval"], "revision": staged["revision"]}


def seed_evaluation_caches(*, loaded: dict[str, Any], sources: dict[str, Any], out: Path) -> None:
    inputs = loaded["inputs"]
    source_out = Path(loaded["approval"]["parent_decision"]["path"]).resolve().parent / "evaluation"
    for provider, source in sources.items():
        for i in sorted(set(loaded["inherited"]) & set(inputs["gates"])):
            origin = source_out / provider / "grades" / i
            identity, _ = completed_step(origin)
            expected = {"candidate": inputs["candidates"][i], "source": source["source"],
                        "source_answer": source["rows"][i], "grading": actual_grading(grading_identity())}
            if identity != expected:
                raise ValueError("inherited evaluation grade inputs differ")
            target = out / "evaluation" / provider / "grades" / i
            if not target.exists():
                shutil.copytree(origin, target)
            elif completed_step(target) != completed_step(origin):
                raise ValueError("inherited evaluation cache changed")


def run_followup(*, config_path: Path, approval_path: Path, out: Path,
                 prepare_only: bool = False, confirm_paid_calls: bool = False,
                 concurrency: int = 4, root: Path = ROOT) -> dict[str, Any]:
    if (prepare_only and confirm_paid_calls) or (not prepare_only and not confirm_paid_calls):
        raise ValueError("follow-up execution requires explicit paid-call approval")
    if not 1 <= concurrency <= 4:
        raise ValueError("follow-up concurrency must be between one and four")
    loaded = load_followup(approval_path=approval_path, config_path=config_path, out=out, root=root)
    stage_followup(loaded=loaded, approval_path=approval_path, out=out)
    inputs, context, config = loaded["inputs"], loaded["context"], loaded["config"]
    plan = {"status": "validation_pending", "accepted_count": len(loaded["inherited"]),
            "selected_test_ids": sorted(loaded["selected"]), "publication_authorized": False,
            "evaluation_launch_authorized": False, "planned_work": {
                "new_certification_attempts": 4 * len(loaded["fresh_ids"]),
                "saved_certification_answers_to_regrade": 4 * (len(loaded["selected"]) - len(loaded["fresh_ids"])),
                "inherited_accepted_tests": len(loaded["inherited"]), "writer_calls": 0}}
    atomic_json(out / "validation_plan.json", plan)
    if prepare_only:
        return plan
    from authoring.complete_test import certification_input
    from harness.environment import call_ledger as _call_ledger
    from authoring.revision_batch import write_revision_batch
    from authoring.revision_evaluation import load_evaluation_answers, regrade_evaluation_answers
    from authoring.revision_review import REVISION_REVIEW_SYSTEM
    from authoring.revision_review_recovery import REVIEW_SCOPE_INSTRUCTIONS, FINAL_REVIEW_CONTEXT_INSTRUCTIONS
    from authoring.revision_workflow import validate_revision_test
    from construction.llm import AzureJsonClient
    from graders.mechanical import grade_tool_trace
    from authoring.certify import certify_candidate, verify_gate_python

    verify_gate_python()
    sources = load_evaluation_answers(inputs=inputs, root=root)
    seed_evaluation_caches(loaded=loaded, sources=sources, out=out)
    identity = {"approval": binding(approval_path), "grading": grading_identity(),
                "checkpoint_identity": context.checkpoint_identity,
                "code": {name: binding(ROOT / name)["sha256"] for name in (
                    "authoring/revision_followup.py", "authoring/revision_workflow.py")}}
    run_once(directory=out / "inputs", identity=identity, action=lambda: identity)
    results = copy.deepcopy(loaded["inherited"])

    def validate_one(i: str) -> dict[str, Any]:
        fresh = i in loaded["fresh_ids"]
        test_out = out / "tests" / i
        result = validate_revision_test(
            candidate_path=inputs["stage"] / "tests" / f"{i}.yaml", request=loaded["requests"][i],
            saved_gate=None if fresh else inputs["gates"][i],
            oracle_input=certification_input(context, SimpleNamespace(
                fact_ids=inputs["candidates"][i]["load_bearing_facts"]), config.evaluation_date) if fresh else None,
            out=test_out, checkpoint=context.checkpoint_path, persona=config.persona,
            client=AzureJsonClient(config.designer_model, reasoning_effort="high"), grader=grade_tool_trace,
            certifier=certify_candidate, execution_identity=identity, confirm_paid_calls=True,
            review_system=REVISION_REVIEW_SYSTEM + REVIEW_SCOPE_INSTRUCTIONS + FINAL_REVIEW_CONTEXT_INSTRUCTIONS,
            inherited_preflight=loaded["preflights"].get(i))
        return {**result, "output_dir": str(test_out.resolve())}

    with _call_ledger(out / "call_ledger.jsonl"):
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(validate_one, i): i for i in sorted(loaded["selected"])}
            for future in as_completed(futures):
                i = futures[future]
                try:
                    results[i] = future.result()
                except Exception as exc:
                    results[i] = {"test_id": int(i), "accepted": False, "status": "pending",
                                  "error": f"{type(exc).__name__}: {exc}"}
                atomic_json(out / "progress.json", results)
                print(json.dumps({"test_id": i, "accepted": results[i]["accepted"],
                                  "completed": len(results), "total": len(inputs["edits"])}), flush=True)
    manifest = {"kind": "exact_release_validation", "version": 1,
                "status": "validated" if all(r["accepted"] for r in results.values()) else "validation_pending",
                "inputs": identity, "revision": inputs["approval"]["revision"],
                "approved_revisions": binding(approval_path), "results": results,
                "accepted_count": sum(r["accepted"] for r in results.values()),
                "preserved_test_count": 200 - len(results), "published": False,
                "inherited_decision": loaded["approval"]["parent_decision"]}
    atomic_json(out / "manifest.json", manifest)
    if manifest["status"] == "validated":
        manifest["accepted_batch"] = str(write_revision_batch(inputs=inputs, manifest=manifest, out=out).resolve())
        atomic_json(out / "manifest.json", manifest)
    with _call_ledger(out / "call_ledger.jsonl"):
        manifest["evaluation_regrading"] = regrade_evaluation_answers(
            inputs=inputs, sources=sources, out=out / "evaluation",
            validated_test_ids={i for i, r in results.items() if r["accepted"]})
    atomic_json(out / "manifest.json", manifest)
    return manifest
