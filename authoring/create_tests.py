"""Create and review one accepted DolphinBench test batch."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from authoring.context import (
    ROOT,
    _validate_tasks,
    design_payload,
    dump_json,
    load_authoring_tasks,
    load_checkpoint_context,
    load_config,
)
from authoring.finalize_batch import write_completed_batch, write_repair_batches
from authoring.final_trace_review import (
    FinalTraceDecision,
    FinalTraceReviewResponse,
    _validate_audit_matches_request,
    _validate_issue_citations,
    build_final_trace_review_request,
    execute_final_trace_review,
)
from authoring.models import (
    ApprovedIdea,
    AuthoredTestResponse,
    BlindQueryResponse,
    DesignResponse,
    PlannedTask,
    TestProposal,
)
from authoring.prompts import AUTHOR_TEST_SYSTEM, DESIGN_SYSTEM, REDESIGN_SYSTEM, REAUTHOR_TEST_SYSTEM
from authoring.pipeline import (
    AuthoringValidationError,
    assemble_candidate,
    validate_design,
    write_candidate,
)
from authoring.propose import (
    record_accepted_batch,
    record_approved_plan,
    record_rejected_tests,
    validate_working_batch_replacement,
)
from authoring.progress import authoring_artifacts, cached_authoring_response, load_progress, save_progress
from authoring.run import (
    _author_new_test_attempt,
    _attempt,
    _certify_cached,
    _completed_design_from_authoring,
    _create_one_test,
    _correction_fields_for_issues,
    _parse_design,
    _validate_authoring_response,
    certify_candidate,
    parse_test_ids,
)
from construction.llm import AzureJsonClient
from construction.runtime_model_calls import cached_client_complete, request_hash
from harness.test_spec_schema import TestSpec
from harness.dataset import load_test
from authoring.certify import verify_gate_python


def _groups(count: int, concurrency: int) -> list[list[int]]:
    groups = [[] for _ in range(min(count, concurrency))]
    for index, position in enumerate(range(1, count + 1)):
        groups[index % len(groups)].append(position)
    return [group for group in groups if group]


def _candidate_path(run_dir: Path, value: str) -> Path:
    path = Path(value)
    if path.is_file():
        return path.resolve()
    candidate = (run_dir / path).resolve()
    if candidate.is_file():
        return candidate
    raise ValueError(f"candidate path does not exist: {value}")


def _normalized_test(path: Path) -> str:
    value = load_test(path)
    if not isinstance(value, dict):
        raise ValueError(f"test is not a YAML object: {path}")
    spec = TestSpec.model_validate(value)
    normalized = spec.model_dump(mode="json")
    normalized["id"] = int(spec.id)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


GRADING_CHECK_CORRECTION_SYSTEM = """Change only the grading checks that the reviewer identified.

The user request, facts, tools, starting app data, and four saved executions are fixed. The
input includes the current assertion list and the reviewer's grading-check issues. Each issue
has an issue_index. Return operations, not a new assertion list.

For every reviewer issue whose owning_step is grading_checks, return one or more operations
that fix that issue. Each operation must name all addressed issue_indexes and one original assertion_index.
When multiple issues concern the same assertion, combine their fixes into one operation and list every addressed issue index.
Use delete to remove that existing assertion. Use replace to change that existing assertion.
Use add to insert a new assertion before that original index; use the list length to append.
Never recreate, reorder, or modify an assertion that the reviewer did not ask you to change.
Do not use the same assertion_index in more than one operation.

Write the smallest fair change that fixes the reviewer issues. A new check is allowed only when
the reviewer says an existing check misses a required result. Keep separate required results in
separate checks. Use deterministic checks for exact values and field_llm_judge only for prose
that can be correct in different words. A field_llm_judge must say exactly what the one named
field must communicate and what incomplete or contradictory meaning fails.

Do not infer an exact number of tool calls from an article such as "a" or "an", singular wording,
or ordinary phrasing. Add tool_call_count only when the request or source explicitly gives an
exact number and additional calls would make the work wrong. Do not add checks for optional
arguments or restrictions that the request and source do not state.

For check_version 2, preserve check_id and action_id on unchanged results. All checks for one
action must pass on the same call. Choose a declared mechanical comparison or a generated
field_llm_judge criterion; mechanical failure never falls back to a language model.

Every replacement or added assertion must include all fields required by the
schema. Use null only for fields that assertion type does not use. Return the zero-based index,
in the final assertion list after applying your operations, that an assistant without the earlier
messages should fail. Do not change any other part of the test. Return only the JSON required by
the schema."""


SAME_FACTS_REPLAN_SYSTEM = """Choose one new piece of realistic work using exactly the supplied facts.

The previous test plan was rejected. Read the complete source messages, the facts, the
evaluation date, and the real tool descriptions in original_input. Use every selected fact
ID and no other fact IDs. Choose work that a person could naturally ask an assistant to do
on the evaluation date. The earlier information must change a necessary part of the
completed work, but the new situation and requested work must not reveal that information.

For every fact, explain why it still applies on the evaluation date. For every named tool,
explain how its actual arguments and behavior can complete the proposed work. Never say
only that a fact applies or that a tool is listed.

When the work sends something to a person, include the exact email address, phone number,
handle, or record ID in the new situation unless remembering that identifier is itself what
the test measures. The test must not fail merely because an assistant without history
cannot locate the recipient.

Do not write the final user message, application records, tool arguments, or grading checks.
Do not repeat the rejected plan unless the failure says only its wording was unclear.
Return only the JSON required by the supplied schema."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_yaml_object(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text()) or {}
    if not isinstance(value, dict):
        raise ValueError(f"expected YAML object: {path}")
    return load_test(path) if "mock_state_base" in value else value


def _normalized_test_object(path: Path) -> dict[str, Any]:
    value = TestSpec.model_validate(_load_yaml_object(path)).model_dump(mode="json")
    value["id"] = int(value["id"])
    return value


def _resolve_manifest_path(*, manifest_path: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"approved revisions {label} path must be a non-empty string")
    path = Path(value)
    return (path if path.is_absolute() else manifest_path.parent / path).resolve()


def _load_approved_revision_inputs(
    *,
    manifest_path: Path,
    config: Any,
    context: Any,
) -> tuple[Path, dict[int, Path], dict[int, PlannedTask], dict[str, Any]]:
    """Authenticate exact revision YAMLs against one approved task batch."""
    raw = json.loads(manifest_path.read_text())
    if not isinstance(raw, dict) or set(raw) != {"version", "approved_plan", "candidates"}:
        raise ValueError(
            "approved revisions must contain exactly version, approved_plan, and candidates"
        )
    if raw.get("version") != 1:
        raise ValueError("approved revisions version must be 1")

    plan_path = _resolve_manifest_path(
        manifest_path=manifest_path, value=raw.get("approved_plan"), label="approved_plan"
    )
    if not plan_path.is_file():
        raise ValueError(f"approved revisions plan does not exist: {plan_path}")
    plan_hash = _sha256(plan_path)
    tasks = load_authoring_tasks(context=context, config=config, path=plan_path)
    if any(isinstance(task, ApprovedIdea) for task in tasks):
        raise ValueError("approved revisions support historical plans only; version-4 ideas require complete test writing and review")
    if not 1 <= len(tasks) <= 10:
        raise ValueError("approved revisions plan must contain one through ten tasks")

    candidates = raw.get("candidates")
    if not isinstance(candidates, dict) or not candidates:
        raise ValueError("approved revisions candidates must be a non-empty ID mapping")
    if len(candidates) != len(tasks):
        raise ValueError("approved revisions candidates must map one-to-one to approved plan tasks")

    candidate_ids: list[int] = []
    entries: dict[int, dict[str, Any]] = {}
    for key, entry in candidates.items():
        if not isinstance(key, str) or not key.isdigit():
            raise ValueError("approved revisions candidate keys must be zero-padded test IDs")
        test_id = int(key)
        if key != f"{test_id:03d}" or test_id <= 0:
            raise ValueError(f"approved revisions candidate key is not a three-digit ID: {key!r}")
        if test_id in entries:
            raise ValueError(f"approved revisions repeats test {test_id:03d}")
        required_fields = {"path", "sha256", "published_sha256"}
        saved_fields = {"saved_run", "saved_run_sha256"}
        if not isinstance(entry, dict) or set(entry) not in (
            required_fields, required_fields | saved_fields,
            required_fields | saved_fields | {"review_only"},
        ):
            raise ValueError(
                f"approved revisions entry {test_id:03d} needs path, sha256, published_sha256, "
                "and optionally saved_run, saved_run_sha256, and review_only"
            )
        if "review_only" in entry and not isinstance(entry["review_only"], bool):
            raise ValueError("review_only must be true or false")
        for field in ("sha256", "published_sha256"):
            value = entry[field]
            if not isinstance(value, str) or len(value) != 64:
                raise ValueError(f"approved revisions {field} is invalid for {test_id:03d}")
        candidate_path = _resolve_manifest_path(
            manifest_path=manifest_path, value=entry["path"], label=f"candidate {test_id:03d}"
        )
        if not candidate_path.is_file() or _sha256(candidate_path) != entry["sha256"]:
            raise ValueError(f"approved revision candidate changed or is missing for {test_id:03d}")

        published_path = ROOT / "tests" / context.persona / f"{test_id:03d}.yaml"
        if not published_path.is_file() or _sha256(published_path) != entry["published_sha256"]:
            raise ValueError(f"current published test changed or is missing for {test_id:03d}")
        candidate = TestSpec.model_validate(_load_yaml_object(candidate_path))
        if int(candidate.id) != test_id:
            raise ValueError(f"approved revision candidate has the wrong ID for {test_id:03d}")
        if candidate.narrative_anchor_date != config.evaluation_date:
            raise ValueError(f"approved revision has the wrong evaluation date for {test_id:03d}")
        candidate_ids.append(test_id)
        entries[test_id] = {
            "candidate": candidate_path,
            "published": published_path,
            "spec": candidate,
            "input": copy.deepcopy(entry),
        }

    candidate_ids.sort()
    tasks_by_id = dict(zip(candidate_ids, tasks, strict=True))
    paths_by_id: dict[int, Path] = {}
    for test_id in candidate_ids:
        candidate = entries[test_id]["spec"]
        task = tasks_by_id[test_id]
        if list(candidate.load_bearing_facts) != list(task.fact_ids):
            raise ValueError(f"approved revision facts do not match its plan for {test_id:03d}")
        if list(candidate.expected_tool_calls) != list(task.expected_tools):
            raise ValueError(f"approved revision tools do not match its plan for {test_id:03d}")
        if len(candidate.expected_tool_calls) != len(set(candidate.expected_tool_calls)):
            raise ValueError(f"approved revision repeats an expected tool for {test_id:03d}")
        unknown_tools = [tool for tool in candidate.expected_tool_calls if tool not in context.tools]
        if unknown_tools:
            raise ValueError(f"approved revision names unknown tools for {test_id:03d}: {unknown_tools}")
        assertions = candidate.grade.config.get("assertions")
        if not isinstance(assertions, list):
            raise ValueError(f"approved revision has no grading assertions for {test_id:03d}")
        _validate_grading_assertions(assertions)
        for index, assertion in enumerate(assertions):
            tool = assertion.get("tool")
            if tool not in context.tools:
                raise ValueError(f"approved revision check {index} names unknown tool for {test_id:03d}")
            path = assertion.get("path") or assertion.get("arg_path")
            if isinstance(path, str) and path.startswith("args."):
                argument = path.split(".", 1)[1].split(".", 1)[0]
                if argument not in context.tools[tool]["arguments"]:
                    raise ValueError(
                        f"approved revision check {index} names unknown argument for {test_id:03d}"
                    )
        paths_by_id[test_id] = entries[test_id]["candidate"]
        entries[test_id]["saved_execution"] = _approved_revision_saved_execution(
            test_id=test_id,
            entry=entries[test_id]["input"],
            manifest_path=manifest_path,
            candidate=entries[test_id]["candidate"],
            context=context,
            config=config,
        )

    identity = {
        "approved_revisions": str(manifest_path.resolve()),
        "approved_revisions_sha256": _sha256(manifest_path),
        "approved_plan": str(plan_path),
        "approved_plan_sha256": plan_hash,
        "candidate_ids": candidate_ids,
        "candidates": {
            f"{test_id:03d}": {
                **entries[test_id]["input"],
                "path": str(entries[test_id]["candidate"]),
                "published_path": str(entries[test_id]["published"]),
                "saved_execution": entries[test_id]["saved_execution"],
            }
            for test_id in candidate_ids
        },
    }
    return plan_path, paths_by_id, tasks_by_id, identity


def _approved_revision_saved_execution(
    *, test_id: int, entry: dict[str, Any], manifest_path: Path,
    candidate: Path, context: Any, config: Any,
) -> dict[str, str] | None:
    """Reuse agent outputs only when the agent's inputs have not changed."""
    if "saved_run" not in entry:
        return None
    source_manifest_path = _resolve_manifest_path(
        manifest_path=manifest_path, value=entry["saved_run"], label="saved_run"
    )
    if not source_manifest_path.is_file() or _sha256(source_manifest_path) != entry["saved_run_sha256"]:
        raise ValueError(f"saved run changed or is missing for {test_id:03d}")
    source = json.loads(source_manifest_path.read_text())
    if any(source.get(key) != value for key, value in (
        ("checkpoint_identity", context.checkpoint_identity),
        ("persona", context.persona),
        ("evaluation_date", config.evaluation_date),
    )):
        raise ValueError(f"saved run has different history or evaluation date for {test_id:03d}")
    rows = [row for row in source.get("authoring_results", []) if row.get("id") == test_id]
    if len(rows) != 1:
        raise ValueError(f"saved run must contain one result for {test_id:03d}")
    row = rows[0]
    old_candidate = _candidate_path(source_manifest_path.parent, str(row.get("candidate") or ""))
    old_gate = _candidate_path(source_manifest_path.parent, str(row.get("gate") or ""))
    saved = _authenticated_saved_gate(test_id=test_id, candidate=old_candidate, gate=old_gate)
    if saved["result"] != row.get("certification"):
        raise ValueError(f"saved execution no longer matches its run for {test_id:03d}")
    if saved["result"].get("verdict") == "infra_error" or any(
        shot.get("error") or shot.get("oracle_errors")
        or not isinstance(shot.get("passed"), bool)
        for shot in saved["result"]["shots"]
    ):
        raise ValueError(f"saved execution has unresolved errors for {test_id:03d}")
    before, after = _normalized_test_object(old_candidate), _normalized_test_object(candidate)
    if entry.get("review_only") and before != after:
        raise ValueError(f"review-only reuse requires an unchanged test for {test_id:03d}")
    before.pop("grade")
    after.pop("grade")
    if before != after:
        raise ValueError(
            f"cannot reuse executions for {test_id:03d}: request, sources, state, or other agent inputs changed"
        )
    return {"candidate": str(old_candidate), "gate": str(old_gate), "run": str(source_manifest_path)}


def _final_review_artifacts(*, review_dir: Path, decision: FinalTraceDecision) -> dict[str, Any]:
    """Record the existing review files and supported model-call metadata."""
    request_path = review_dir / "request.json"
    response_path = review_dir / "response.json"
    cache_path = review_dir / "work" / "response_cache.json"
    decision_path = review_dir / "decision.json"
    raw_response: dict[str, Any] | None = None
    if response_path.is_file():
        raw_response = json.loads(response_path.read_text())
    elif cache_path.is_file():
        cached = json.loads(cache_path.read_text())
        if isinstance(cached.get("response"), dict):
            raw_response = cached["response"]
    usage = raw_response.get("_usage") if isinstance(raw_response, dict) else None
    return {
        "request": str(request_path.resolve()) if request_path.is_file() else None,
        "raw_response": str(response_path.resolve()) if response_path.is_file() else None,
        "response_cache": str(cache_path.resolve()) if cache_path.is_file() else None,
        "decision": str(decision_path.resolve()) if decision_path.is_file() else None,
        "model_call_metadata": {
            "response_id": raw_response.get("_response_id") if raw_response else None,
            "usage": copy.deepcopy(usage) if isinstance(usage, dict) else {},
            "reasoning_tokens": usage.get("reasoning_tokens", 0)
            if isinstance(usage, dict)
            else 0,
        },
        "review_reason": (
            "accepted"
            if decision.accept
            else decision.specific_problem.strip() or "final trace review rejected the candidate"
        ),
    }


def create_approved_revisions(
    *,
    config_path: Path,
    approved_revisions_path: Path,
    out: Path,
    concurrency: int,
    confirm_paid_calls: bool = False,
    certifier: Any = certify_candidate,
    reviewer: Any = None,
    regrader: Any = None,
) -> dict[str, Any]:
    """Certify exact human-approved replacement YAMLs without authoring calls."""
    if not confirm_paid_calls:
        raise ValueError("refusing approved-revision certification without explicit confirmation")
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"approved-revision output directory is not empty: {out}")
    if not 1 <= concurrency <= 4:
        raise ValueError("concurrency must be between one and four")
    config = load_config(config_path)
    context = load_checkpoint_context(config)
    approved_revisions_path = approved_revisions_path.resolve()
    if not approved_revisions_path.is_file():
        raise ValueError(f"approved revisions manifest does not exist: {approved_revisions_path}")
    plan_path, source_candidates, tasks_by_id, identity = _load_approved_revision_inputs(
        manifest_path=approved_revisions_path,
        config=config,
        context=context,
    )
    verify_gate_python()
    reviewer = reviewer or _review_one
    regrader = regrader or _default_regrader
    record_approved_plan(config=config, context=context, plan_path=plan_path)
    out.mkdir(parents=True, exist_ok=True)
    dump_json(out / "approved_revisions_inputs.json", {**identity, "checkpoint_identity": context.checkpoint_identity})

    previous_ledger = os.environ.get("DOLPHINBENCH_CALL_LEDGER")
    previous_run_id = os.environ.get("DOLPHINBENCH_CONSTRUCTION_RUN_ID")
    ledger = out / "final_trace_reviews" / "call_ledger.jsonl"
    os.environ["DOLPHINBENCH_CALL_LEDGER"] = str(ledger)
    os.environ["DOLPHINBENCH_CONSTRUCTION_RUN_ID"] = out.name
    prepared: dict[int, tuple[Path, Path]] = {}
    for test_id in sorted(source_candidates):
        proposal_dir = out / "authoring" / "part_01" / "proposals" / f"{test_id:03d}"
        proposal_dir.mkdir(parents=True, exist_ok=True)
        candidate = proposal_dir / "approved_candidate.yaml"
        shutil.copy2(source_candidates[test_id], candidate)
        prepared[test_id] = (candidate, proposal_dir / "approved_gate.json")

    authoring_results_by_id: dict[int, dict[str, Any]] = {}
    final_review_results: list[dict[str, Any]] = []
    accepted: dict[int, Path] = {}
    try:
        def certify_one(test_id: int) -> dict[str, Any]:
            candidate, gate = prepared[test_id]
            authoring_result: dict[str, Any] = {
                "id": test_id,
                "fact_ids": list(tasks_by_id[test_id].fact_ids),
                "authoring_mode": "approved_revision",
                "candidate": str(candidate.resolve()),
                "gate": str(gate.resolve()),
            }
            try:
                saved_execution = identity["candidates"][f"{test_id:03d}"]["saved_execution"]
                if saved_execution:
                    old_candidate = Path(saved_execution["candidate"])
                    old_gate = Path(saved_execution["gate"])
                    if identity["candidates"][f"{test_id:03d}"].get("review_only"):
                        wrapped = json.loads(old_gate.read_text())
                        wrapped["candidate_sha256"] = _sha256(candidate)
                    else:
                        wrapped = _regraded_gate(
                            candidate_path=candidate, saved_gate_path=old_gate, regrader=regrader
                        )
                    dump_json(gate, wrapped)
                    certification = wrapped["result"]
                    authoring_result["reused_execution"] = saved_execution
                else:
                    certification = _certify_cached(
                        persona=config.persona,
                        checkpoint=context.checkpoint_path,
                        candidate_path=candidate,
                        result_path=gate,
                        certifier=certifier,
                    )
                _authenticated_saved_gate(test_id=test_id, candidate=candidate, gate=gate)
                authoring_result["certification"] = copy.deepcopy(certification)
                authoring_result["certification_valid"] = bool(certification.get("valid"))
                authoring_result["status"] = "certified_approved_revision" if certification.get("valid") else "certification_failed"
            except Exception as exc:
                authoring_result.update(
                    status="certification_failed",
                    certification_valid=False,
                    certification_error=f"{type(exc).__name__}: {exc}",
                )
            return authoring_result

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(certify_one, test_id): test_id for test_id in prepared}
            for future in as_completed(futures):
                test_id = futures[future]
                authoring_results_by_id[test_id] = future.result()

        def review_one(test_id: int) -> dict[str, Any]:
            candidate, gate = prepared[test_id]
            review_dir = out / "final_trace_reviews" / f"{test_id:03d}"
            authoring_result = authoring_results_by_id[test_id]
            try:
                decision = reviewer(
                    test_id=test_id,
                    candidate=candidate,
                    gate=gate,
                    planned_task=tasks_by_id[test_id],
                    context=context,
                    out=out / "final_trace_reviews",
                    model=config.designer_model,
                )
                if decision.accept and not authoring_result["certification_valid"]:
                    raise ValueError(
                        f"reviewer accepted test {test_id:03d} even though certification failed"
                    )
                evidence = _final_review_artifacts(review_dir=review_dir, decision=decision)
                if reviewer is _review_one and not evidence["raw_response"]:
                    raise ValueError("existing final trace reviewer produced no raw response")
                return {
                    "id": test_id,
                    "initial_final_review": decision.model_dump(mode="json"),
                    "final_review_evidence": evidence,
                    "status": "accepted_initial_final_review" if decision.accept else "replacement_required",
                }
            except Exception as exc:
                return {
                    "id": test_id,
                    "status": "replacement_required",
                    "reason": f"final trace review failed: {type(exc).__name__}: {exc}",
                }

        review_ids = [test_id for test_id, (_candidate, gate) in prepared.items() if gate.is_file()]
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(review_one, test_id): test_id for test_id in review_ids}
            reviewed_by_id = {
                futures[future]: future.result() for future in as_completed(futures)
            }
        for test_id in sorted(prepared):
            authoring_result = authoring_results_by_id[test_id]
            if test_id not in reviewed_by_id:
                final_review_results.append(
                    {
                        "id": test_id,
                        "status": "replacement_required",
                        "reason": "certification did not produce a saved four-run gate",
                    }
                )
                continue
            review_result = reviewed_by_id[test_id]
            final_review_results.append(review_result)
            if review_result["status"] == "accepted_initial_final_review":
                accepted[test_id] = prepared[test_id][0]
    finally:
        if previous_ledger is None:
            os.environ.pop("DOLPHINBENCH_CALL_LEDGER", None)
        else:
            os.environ["DOLPHINBENCH_CALL_LEDGER"] = previous_ledger
        if previous_run_id is None:
            os.environ.pop("DOLPHINBENCH_CONSTRUCTION_RUN_ID", None)
        else:
            os.environ["DOLPHINBENCH_CONSTRUCTION_RUN_ID"] = previous_run_id

    manifest: dict[str, Any] = {
        "checkpoint_identity": context.checkpoint_identity,
        "persona": context.persona,
        "evaluation_date": config.evaluation_date,
        "approved_revisions_inputs": str((out / "approved_revisions_inputs.json").resolve()),
        "plan_path": str(plan_path.resolve()),
        "test_ids": sorted(source_candidates),
        "candidate_count": len(source_candidates),
        "concurrency": concurrency,
        "authoring_calls": 0,
        "authoring_results": [authoring_results_by_id[test_id] for test_id in sorted(prepared)],
        "certification_results": [authoring_results_by_id[test_id] for test_id in sorted(prepared)],
        "final_review_results": final_review_results,
        "final_review_accepted": len(accepted),
        "promoted": False,
    }
    dump_json(out / "manifest.json", manifest)
    accepted_candidates, _ = write_completed_batch(
        out=out,
        checkpoint_identity=context.checkpoint_identity,
        persona=context.persona, evaluation_date=config.evaluation_date,
        plan_path=plan_path,
        tasks_by_id=tasks_by_id,
        accepted=accepted,
        authoring_results=[authoring_results_by_id[test_id] for test_id in sorted(prepared)],
        final_review_results=final_review_results,
    )
    manifest["accepted_candidates"] = accepted_candidates
    manifest["accepted_batch"] = str((out / "accepted_batch").resolve()) if accepted else None
    manifest["rejected_tests"] = str((out / "rejected_tests.json").resolve())
    dump_json(out / "manifest.json", manifest)
    record_rejected_tests(config=config, context=context, rejected_path=out / "rejected_tests.json")
    return manifest


def _four_shot_gate_matches_candidate(*, gate: Path, candidate: Path) -> bool:
    """Return whether a saved gate is the accepted four-execution certification."""
    try:
        saved_gate = json.loads(gate.read_text())
        result = saved_gate.get("result")
        shots = result.get("shots") if isinstance(result, dict) else None
        return (
            saved_gate.get("candidate_sha256") == _sha256(candidate)
            and isinstance(result, dict)
            and result.get("valid") is True
            and isinstance(shots, list)
            and len(shots) == 4
            and sum(shot.get("with_memory") is True for shot in shots if isinstance(shot, dict)) == 2
            and sum(shot.get("with_memory") is False for shot in shots if isinstance(shot, dict)) == 2
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _release_manifest_binding(
    *,
    test_id: int,
    published_path: Path,
    context: Any,
    config: Any,
) -> tuple[str, PlannedTask, dict[str, str]] | None:
    """Load the accepted candidate/task and source review record for a published test."""
    canonical_tests_dir = (ROOT / "tests" / context.persona).resolve()
    if published_path.resolve().parent != canonical_tests_dir:
        return None
    manifest_path = ROOT / "authoring" / "release_manifests" / f"{context.persona}.json"
    if not manifest_path.is_file():
        return None

    release = json.loads(manifest_path.read_text())
    if (
        release.get("persona") != context.persona
        or release.get("checkpoint_identity") != context.checkpoint_identity
        or release.get("evaluation_date") != config.evaluation_date
    ):
        raise ValueError("published-test release manifest does not match the active checkpoint")
    batches = release.get("batches")
    if not isinstance(batches, list):
        raise ValueError("published-test release manifest has no batches")
    matching_batches = [
        batch
        for batch in batches
        if isinstance(batch, dict) and test_id in (batch.get("accepted_test_ids") or [])
    ]
    if not matching_batches:
        return None
    if len(matching_batches) != 1:
        raise ValueError(f"published test {test_id:03d} appears in multiple release batches")
    batch_entry = matching_batches[0]
    required_batch_fields = {
        "directory",
        "planning_batch_sha256",
        "provenance_sha256",
        "accepted_test_ids",
    }
    if not required_batch_fields.issubset(batch_entry):
        raise ValueError(f"release manifest entry is incomplete for published test {test_id:03d}")
    batch_dir = Path(str(batch_entry["directory"])).resolve()
    plan_path = batch_dir / "planning_batch.json"
    provenance_path = batch_dir / "provenance.json"
    if (
        not plan_path.is_file()
        or not provenance_path.is_file()
        or _sha256(plan_path) != batch_entry["planning_batch_sha256"]
        or _sha256(provenance_path) != batch_entry["provenance_sha256"]
    ):
        raise ValueError(f"release batch files changed for published test {test_id:03d}")

    tasks = load_authoring_tasks(context=context, config=config, path=plan_path)
    accepted_ids = batch_entry["accepted_test_ids"]
    if (
        not isinstance(accepted_ids, list)
        or len(accepted_ids) != len(tasks)
        or len(accepted_ids) != len(set(accepted_ids))
        or test_id not in accepted_ids
    ):
        raise ValueError(f"release batch task mapping is invalid for published test {test_id:03d}")
    task_position = accepted_ids.index(test_id)
    accepted_task = tasks[task_position]

    provenance = json.loads(provenance_path.read_text())
    if provenance.get("checkpoint_identity") != context.checkpoint_identity:
        raise ValueError(f"accepted batch checkpoint does not match for published test {test_id:03d}")
    source_manifest_value = provenance.get("source_manifest")
    results = provenance.get("results")
    if not isinstance(source_manifest_value, str) or not isinstance(results, list):
        raise ValueError(f"accepted batch provenance is incomplete for published test {test_id:03d}")
    matching_results = [
        result
        for result in results
        if isinstance(result, dict) and result.get("test_id") == test_id
    ]
    if len(matching_results) != 1:
        raise ValueError(f"accepted batch provenance has no unique result for published test {test_id:03d}")
    result = matching_results[0]
    if result.get("status") not in {
        "accepted_clean_certification",
        "accepted_initial_final_review",
        "accepted_after_final_review_correction",
    }:
        raise ValueError(f"accepted batch provenance is not accepted for published test {test_id:03d}")
    accepted_candidate = (batch_dir / "candidates" / f"{test_id:03d}.yaml").resolve()
    if (
        Path(str(result.get("candidate") or "")).resolve() != accepted_candidate
        or not accepted_candidate.is_file()
        or result.get("candidate_sha256") != _sha256(accepted_candidate)
        or release.get("published_sha256", {}).get(f"{test_id:03d}", result.get("candidate_sha256"))
        != _sha256(published_path)
        or ("published_sha256" in release and load_test(accepted_candidate) != load_test(published_path))
    ):
        raise ValueError(f"accepted candidate provenance does not match published test {test_id:03d}")
    accepted_spec = TestSpec.model_validate(_load_yaml_object(accepted_candidate))
    if (
        int(accepted_spec.id) != test_id
        or list(accepted_spec.load_bearing_facts) != list(accepted_task.fact_ids)
        or list(accepted_spec.expected_tool_calls) != list(accepted_task.expected_tools)
    ):
        raise ValueError(f"accepted candidate does not match its plan for published test {test_id:03d}")

    source_manifest_path = Path(source_manifest_value).resolve()
    if not source_manifest_path.is_file():
        raise ValueError(f"accepted batch source manifest is missing for published test {test_id:03d}")
    source_manifest = json.loads(source_manifest_path.read_text())
    if (
        source_manifest.get("checkpoint_identity") != context.checkpoint_identity
        or (
            source_manifest.get("persona") is not None
            and source_manifest.get("persona") != context.persona
        )
    ):
        raise ValueError(f"accepted batch source manifest does not match for published test {test_id:03d}")
    review_dir_value = source_manifest.get("review_dir")
    review_inputs_sha256 = source_manifest.get("review_inputs_sha256")
    if not isinstance(review_dir_value, str) or not isinstance(review_inputs_sha256, str):
        return result["candidate_sha256"], accepted_task, {}
    review_inputs_path = Path(review_dir_value).resolve() / "review_inputs.json"
    if not review_inputs_path.is_file() or _sha256(review_inputs_path) != review_inputs_sha256:
        raise ValueError(f"accepted batch review provenance changed for published test {test_id:03d}")
    source_evidence = (json.loads(review_inputs_path.read_text()).get("evidence") or {}).get(
        f"{test_id:03d}"
    )
    if not isinstance(source_evidence, dict) or not all(
        isinstance(source_evidence.get(key), str)
        and Path(source_evidence[key]).is_file()
        for key in ("candidate", "gate", "plan", "published")
    ):
        raise ValueError(f"accepted batch has no complete source record for published test {test_id:03d}")
    if Path(source_evidence["published"]).resolve() != published_path.resolve():
        raise ValueError(f"accepted batch source record points to a different published test {test_id:03d}")
    return result["candidate_sha256"], accepted_task, {
        key: str(value) for key, value in source_evidence.items() if isinstance(value, str)
    }


def _targeted_repair_source(
    *,
    test_id: int,
    candidate: Path,
    result: dict[str, Any],
    repair_root: Path,
    evidence_root: Path,
) -> dict[str, Path] | None:
    """Authenticate the two targeted corrections made before repair_inputs existed."""
    targeted_path = repair_root / "targeted_results.json"
    promotion_path = repair_root / "promotion_manifest.json"
    if not targeted_path.is_file() or not promotion_path.is_file():
        return None
    targeted = json.loads(targeted_path.read_text())
    promotion = json.loads(promotion_path.read_text())
    saved = targeted.get(str(test_id))
    if (
        not isinstance(saved, dict)
        or saved.get("result") != result
        or Path(str(saved.get("candidate") or "")).resolve() != candidate.resolve()
        or test_id not in [int(value) for value in promotion.get("replaced_test_ids") or []]
        or Path(str(promotion.get("targeted_results") or "")).resolve()
        != targeted_path.resolve()
    ):
        return None

    action = result.get("action")
    sources: list[dict[str, Path]] = []
    for review_inputs_path in evidence_root.glob("**/review_inputs.json"):
        review_inputs = json.loads(review_inputs_path.read_text())
        raw = (review_inputs.get("evidence") or {}).get(f"{test_id:03d}")
        if not isinstance(raw, dict) or not all(
            isinstance(raw.get(key), str) for key in ("candidate", "gate", "plan")
        ):
            continue
        paths = {key: Path(raw[key]).resolve() for key in ("candidate", "gate", "plan")}
        if not all(path.is_file() for path in paths.values()):
            continue
        source_gate = json.loads(paths["gate"].read_text())
        if source_gate.get("candidate_sha256") != _sha256(paths["candidate"]):
            continue
        before = _normalized_test_object(paths["candidate"])
        after = _normalized_test_object(candidate)
        if action == "corrected_grading_checks":
            before["grade"]["config"].pop("assertions", None)
            after["grade"]["config"].pop("assertions", None)
        elif action == "rewrote_user_request":
            before.pop("test", None)
            after.pop("test", None)
        else:
            continue
        if before == after:
            sources.append({f"source_{key}": path for key, path in paths.items()})
    return sources[0] if len(sources) == 1 else None


def _write_yaml(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False))


def _saved_review_record(
    *,
    test_id: int,
    published_path: Path,
    evidence_root: Path,
    context: Any,
    config: Any,
) -> tuple[dict[str, Any], PlannedTask]:
    expected = _normalized_test(published_path)
    release_binding = _release_manifest_binding(
        test_id=test_id,
        published_path=published_path,
        context=context,
        config=config,
    )
    preferred_candidate_sha256 = release_binding[0] if release_binding else None
    preferred_task = release_binding[1] if release_binding else None
    if release_binding is not None:
        _, accepted_task, source_evidence = release_binding
        source_candidate_value = source_evidence.get("candidate")
        if source_candidate_value is not None and (
            _normalized_test(Path(source_candidate_value).resolve()) == expected
        ):
            record, task = _planned_task_from_review_evidence(
                test_id=test_id,
                evidence=source_evidence,
                context=context,
                config=config,
            )
            if (
                task != accepted_task
                or not _four_shot_gate_matches_candidate(
                    gate=record["gate"], candidate=record["candidate"]
                )
            ):
                raise ValueError(
                    f"release source record does not match accepted four-shot gate for "
                    f"published test {test_id:03d}"
                )
            return record, task

    matches: list[tuple[Path, Path, Path, PlannedTask, dict[str, Path]]] = []
    repair_matches: list[tuple[Path, Path, Path, PlannedTask, dict[str, Path]]] = []
    pattern = f"**/proposals/{test_id:03d}/*candidate.yaml"
    for candidate in evidence_root.glob(pattern):
        try:
            if (
                _normalized_test(candidate) != expected
                or (
                    preferred_candidate_sha256 is not None
                    and _sha256(candidate) != preferred_candidate_sha256
                )
            ):
                continue
        except (OSError, TypeError, ValueError, yaml.YAMLError):
            continue
        prefix = candidate.name[: -len("candidate.yaml")]
        gate = candidate.with_name(prefix + "gate.json")
        run_dir = candidate.parents[2]
        run_inputs = run_dir / "run_inputs.json"
        if not gate.is_file() or not run_inputs.is_file():
            continue
        metadata = json.loads(run_inputs.read_text())
        selected_test_ids = [int(value) for value in metadata.get("selected_test_ids") or []]
        selected_positions_value = metadata.get("selected_plan_positions")
        selected_positions = (
            [int(value) for value in selected_positions_value]
            if selected_positions_value is not None
            else None
        )
        if selected_test_ids:
            if selected_positions is None or len(selected_test_ids) != len(selected_positions):
                continue
            positions = [
                selected_positions[index]
                for index, value in enumerate(selected_test_ids)
                if value == test_id
            ]
        else:
            start_id = metadata.get("start_id")
            limit = metadata.get("limit")
            if start_id is None or (selected_positions is None and limit is None):
                continue
            position = test_id - int(start_id) + 1
            if selected_positions is not None:
                positions = [position] if position in selected_positions else []
            else:
                positions = [position] if 1 <= position <= int(limit) else []
        if len(positions) != 1:
            continue
        plan_path = Path(str(metadata.get("plan_path") or ""))
        if not plan_path.is_file():
            continue
        position = positions[0]
        legacy_plan = json.loads(plan_path.read_text())
        tasks = legacy_plan.get("tasks")
        if not isinstance(tasks, list) or not 1 <= position <= len(tasks):
            continue
        if (
            legacy_plan.get("persona") != context.persona
            or legacy_plan.get("evaluation_date") != config.evaluation_date
        ):
            continue
        historical_task = dict(legacy_plan["tasks"][position - 1])
        historical_task.setdefault("action_without_memory", "")
        historical_task.setdefault(
            "fact_application_explanations_on_evaluation_date",
            [
                {
                    "fact_id": fact_id,
                    "why_fact_still_applies_on_evaluation_date": historical_task[
                        "why_memory_changes_result"
                    ],
                }
                for fact_id in historical_task["fact_ids"]
            ],
        )
        historical_task.setdefault(
            "why_listed_tools_can_complete_requested_work",
            "The saved plan lists these tools: "
            + ", ".join(historical_task["expected_tools"])
            + ".",
        )
        planned_task = PlannedTask.model_validate(historical_task)
        _validate_tasks(context=context, config=config, tasks=[planned_task])
        if preferred_task is not None and planned_task != preferred_task:
            continue
        if not _four_shot_gate_matches_candidate(gate=gate, candidate=candidate):
            continue
        matches.append(
            (candidate.resolve(), gate.resolve(), plan_path.resolve(), planned_task, {})
        )

    repair_pattern = f"**/repair/{test_id:03d}/*candidate.yaml"
    for candidate in evidence_root.glob(repair_pattern):
        try:
            if (
                _normalized_test(candidate) != expected
                or (
                    preferred_candidate_sha256 is not None
                    and _sha256(candidate) != preferred_candidate_sha256
                )
            ):
                continue
        except (OSError, TypeError, ValueError, yaml.YAMLError):
            continue
        result_path = candidate.parent / "result.json"
        if not result_path.is_file():
            continue
        result = json.loads(result_path.read_text())
        result_candidate = result.get("candidate")
        if (
            result.get("status") != "accepted"
            or not isinstance(result_candidate, str)
            or Path(result_candidate).resolve() != candidate.resolve()
        ):
            continue
        raw_task = result.get("planned_task")
        if not isinstance(raw_task, dict):
            continue
        planned_task = PlannedTask.model_validate(raw_task)
        _validate_tasks(context=context, config=config, tasks=[planned_task])

        prefix = candidate.name[: -len("candidate.yaml")]
        gate = candidate.with_name(prefix + "gate.json")
        if not gate.is_file():
            continue
        if preferred_task is not None and planned_task != preferred_task:
            continue
        if not _four_shot_gate_matches_candidate(gate=gate, candidate=candidate):
            continue

        repair_root = candidate.parents[2]
        repair_inputs_path = repair_root / "repair_inputs.json"
        source_paths: dict[str, Path] | None = None
        if repair_inputs_path.is_file():
            repair_inputs = json.loads(repair_inputs_path.read_text())
            review_dir_value = repair_inputs.get("review_dir")
            expected_review_inputs_hash = repair_inputs.get("review_inputs_sha256")
            if not isinstance(review_dir_value, str) or not isinstance(
                expected_review_inputs_hash, str
            ):
                continue
            previous_review_inputs_path = Path(review_dir_value) / "review_inputs.json"
            if (
                not previous_review_inputs_path.is_file()
                or _sha256(previous_review_inputs_path) != expected_review_inputs_hash
            ):
                continue
            previous_review_inputs = json.loads(previous_review_inputs_path.read_text())
            previous_evidence = (previous_review_inputs.get("evidence") or {}).get(
                f"{test_id:03d}"
            )
            if not isinstance(previous_evidence, dict) or not all(
                isinstance(previous_evidence.get(key), str)
                for key in ("candidate", "gate", "plan")
            ):
                continue
            source_paths = {
                f"source_{key}": Path(previous_evidence[key]).resolve()
                for key in ("candidate", "gate", "plan")
            }
            if not all(path.is_file() for path in source_paths.values()):
                continue
        else:
            source_paths = _targeted_repair_source(
                test_id=test_id,
                candidate=candidate,
                result=result,
                repair_root=repair_root,
                evidence_root=evidence_root,
            )
            if source_paths is None:
                continue
        repair_matches.append(
            (
                candidate.resolve(),
                gate.resolve(),
                result_path.resolve(),
                planned_task,
                source_paths,
            )
        )
    if repair_matches:
        matches = repair_matches
    if len(matches) != 1:
        if release_binding is not None:
            raise ValueError(
                f"release provenance could not resolve exactly one saved candidate, "
                f"plan, and four-shot gate for published test {test_id:03d}; "
                f"found {len(matches)}"
            )
        raise ValueError(
            f"published test {test_id:03d} must match exactly one saved candidate, gate, and plan; "
            f"found {len(matches)}"
        )
    candidate, gate, plan_path, planned_task, source_paths = matches[0]
    record = {
        "candidate": candidate,
        "gate": gate,
        "plan": plan_path,
        "published": published_path.resolve(),
    }
    record.update(source_paths)
    return record, planned_task


def review_published_tests(
    *,
    config_path: Path,
    tests_dir: Path,
    evidence_root: Path,
    out: Path,
    concurrency: int,
    expected_count: int = 200,
    confirm_paid_calls: bool = False,
    reviewer: Any = None,
    selected_test_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Review an existing published set without changing it."""
    if not confirm_paid_calls:
        raise ValueError("refusing published-test review without explicit confirmation")
    if not 1 <= concurrency <= 4:
        raise ValueError("concurrency must be between one and four")
    if expected_count <= 0:
        raise ValueError("expected_count must be positive")
    config = load_config(config_path)
    context = load_checkpoint_context(config)
    reviewer = reviewer or _review_one
    published = sorted(tests_dir.glob("*.yaml"))
    if len(published) != expected_count:
        raise ValueError(
            f"published-test review expected {expected_count} YAML files, found {len(published)}"
        )
    paths_by_id: dict[int, Path] = {}
    for path in published:
        spec = TestSpec.model_validate(load_test(path))
        test_id = int(spec.id)
        if test_id in paths_by_id:
            raise ValueError(f"duplicate published test ID: {test_id}")
        paths_by_id[test_id] = path
    expected_ids = set(range(1, expected_count + 1))
    if set(paths_by_id) != expected_ids:
        raise ValueError("published tests must use every ID from 1 through expected_count")
    selected_ids = _selected_published_test_ids(
        selected_test_ids, expected_ids, option_name="--test-ids"
    )

    records: dict[int, dict[str, Any]] = {}
    tasks: dict[int, PlannedTask] = {}
    for test_id in selected_ids:
        path = paths_by_id[test_id]
        record, task = _saved_review_record(
            test_id=test_id,
            published_path=path,
            evidence_root=evidence_root,
            context=context,
            config=config,
        )
        records[test_id] = record
        tasks[test_id] = task

    identity = {
        "checkpoint_identity": context.checkpoint_identity,
        "persona": context.persona,
        "model": config.designer_model,
        "tests_dir": str(tests_dir.resolve()),
        "evidence_root": str(evidence_root.resolve()),
        "selected_test_ids": selected_ids,
        "test_sha256": {
            f"{test_id:03d}": hashlib.sha256(path.read_bytes()).hexdigest()
            for test_id, path in sorted(paths_by_id.items())
        },
        "evidence": {
            f"{test_id:03d}": {
                key: str(value) for key, value in record.items()
            }
            for test_id, record in sorted(records.items())
        },
    }
    identity_path = out / "review_inputs.json"
    if out.exists() and any(out.iterdir()):
        if not identity_path.is_file() or json.loads(identity_path.read_text()) != identity:
            raise ValueError(f"published-review output directory belongs to different inputs: {out}")
    out.mkdir(parents=True, exist_ok=True)
    dump_json(identity_path, identity)

    ledger = out / "final_trace_reviews" / "call_ledger.jsonl"
    previous_ledger = os.environ.get("DOLPHINBENCH_CALL_LEDGER")
    previous_run_id = os.environ.get("DOLPHINBENCH_CONSTRUCTION_RUN_ID")
    os.environ["DOLPHINBENCH_CALL_LEDGER"] = str(ledger)
    os.environ["DOLPHINBENCH_CONSTRUCTION_RUN_ID"] = out.name
    decisions: dict[int, FinalTraceDecision] = {}
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(
                    reviewer,
                    test_id=test_id,
                    candidate=record["candidate"],
                    gate=record["gate"],
                    planned_task=tasks[test_id],
                    context=context,
                    out=out / "final_trace_reviews",
                    model=config.designer_model,
                ): test_id
                for test_id, record in records.items()
            }
            for future in as_completed(futures):
                decisions[futures[future]] = future.result()
    finally:
        if previous_ledger is None:
            os.environ.pop("DOLPHINBENCH_CALL_LEDGER", None)
        else:
            os.environ["DOLPHINBENCH_CALL_LEDGER"] = previous_ledger
        if previous_run_id is None:
            os.environ.pop("DOLPHINBENCH_CONSTRUCTION_RUN_ID", None)
        else:
            os.environ["DOLPHINBENCH_CONSTRUCTION_RUN_ID"] = previous_run_id

    manifest = {
        "checkpoint_identity": context.checkpoint_identity,
        "persona": context.persona,
        "model": config.designer_model,
        "test_count": len(decisions),
        "published_test_count": len(paths_by_id),
        "selected_test_ids": selected_ids,
        "accepted": sum(decision.accept for decision in decisions.values()),
        "rejected": sum(not decision.accept for decision in decisions.values()),
        "decisions": {
            f"{test_id:03d}": decision.model_dump(mode="json")
            for test_id, decision in sorted(decisions.items())
        },
        "published_tests_changed": False,
    }
    dump_json(out / "manifest.json", manifest)
    return manifest


def _reviewed_test_inputs(
    *, tests_dir: Path, review_dir: Path, expected_count: int
) -> tuple[
    dict[int, Path],
    dict[int, FinalTraceDecision],
    dict[int, dict[str, str]],
    list[int],
]:
    """Load the exact published set and the completed review decisions."""
    inputs_path = review_dir / "review_inputs.json"
    manifest_path = review_dir / "manifest.json"
    if not inputs_path.is_file() or not manifest_path.is_file():
        raise ValueError("published-test repair requires review_inputs.json and manifest.json")
    review_inputs = json.loads(inputs_path.read_text())
    review_manifest = json.loads(manifest_path.read_text())
    expected_hashes = review_inputs.get("test_sha256")
    if not isinstance(expected_hashes, dict):
        raise ValueError("published-test review has no test hashes")
    paths_by_id: dict[int, Path] = {}
    for path in sorted(tests_dir.glob("*.yaml")):
        spec = TestSpec.model_validate(_load_yaml_object(path))
        test_id = int(spec.id)
        if test_id in paths_by_id:
            raise ValueError(f"duplicate published test ID: {test_id}")
        paths_by_id[test_id] = path
    expected_ids = set(range(1, expected_count + 1))
    if set(paths_by_id) != expected_ids:
        raise ValueError("published tests must use every ID from 1 through expected_count")
    selected_ids = _selected_published_test_ids(
        review_inputs.get("selected_test_ids"),
        expected_ids,
        option_name="selected_test_ids",
    )
    manifest_selected_ids = review_manifest.get("selected_test_ids")
    if manifest_selected_ids is not None and _selected_published_test_ids(
        manifest_selected_ids, expected_ids, option_name="manifest.selected_test_ids"
    ) != selected_ids:
        raise ValueError("published-test review selection differs between its two manifests")

    # This happens before any output directory is created. A repair must never
    # operate on a published test that changed after its review.
    for test_id, path in sorted(paths_by_id.items()):
        expected = expected_hashes.get(f"{test_id:03d}")
        if not isinstance(expected, str) or _sha256(path) != expected:
            raise ValueError(f"published test {test_id:03d} changed after its review")

    raw_decisions = review_manifest.get("decisions")
    if not isinstance(raw_decisions, dict):
        raise ValueError("published-test review has no decisions")
    decisions: dict[int, FinalTraceDecision] = {}
    for test_id in selected_ids:
        raw = raw_decisions.get(f"{test_id:03d}")
        if not isinstance(raw, dict):
            raise ValueError(f"published-test review has no decision for {test_id:03d}")
        decision = FinalTraceDecision.model_validate(raw)
        if decision.test_id != test_id:
            raise ValueError(f"published-test review has wrong decision ID for {test_id:03d}")
        decisions[test_id] = decision
    evidence = review_inputs.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("published-test review has no evidence paths")
    evidence_by_id: dict[int, dict[str, str]] = {}
    for test_id in selected_ids:
        raw = evidence.get(f"{test_id:03d}")
        if not isinstance(raw, dict) or not all(
            isinstance(raw.get(key), str) for key in ("candidate", "gate", "plan", "published")
        ):
            raise ValueError(f"published-test review has incomplete evidence for {test_id:03d}")
        evidence_by_id[test_id] = {key: str(raw[key]) for key in raw}
    return paths_by_id, decisions, evidence_by_id, selected_ids


def _selected_published_test_ids(
    values: Any, expected_ids: set[int], *, option_name: str
) -> list[int]:
    """Validate a review selection without weakening full-set validation."""
    if values is None:
        return sorted(expected_ids)
    if not isinstance(values, list) or any(
        not isinstance(value, int) or isinstance(value, bool) for value in values
    ):
        raise ValueError(f"{option_name} must be a list of integer test IDs")
    if not values:
        raise ValueError(f"{option_name} must contain at least one test ID")
    if len(values) != len(set(values)):
        raise ValueError(f"{option_name} contains duplicate test IDs")
    unknown = sorted(set(values) - expected_ids)
    if unknown:
        raise ValueError(f"{option_name} contains unknown test IDs: {unknown}")
    return sorted(values)


def _planned_task_from_review_evidence(
    *,
    test_id: int,
    evidence: dict[str, str],
    context: Any,
    config: Any,
) -> tuple[dict[str, Any], PlannedTask]:
    """Recover the saved plan and authoring files used for one reviewed test."""
    candidate = Path(evidence["candidate"])
    gate = Path(evidence["gate"])
    plan_path = Path(evidence["plan"])
    if not candidate.is_file() or not gate.is_file() or not plan_path.is_file():
        raise ValueError(f"published-test review evidence is missing for {test_id:03d}")
    saved_gate = json.loads(gate.read_text())
    if saved_gate.get("candidate_sha256") != _sha256(candidate):
        raise ValueError(f"saved certification does not match candidate for {test_id:03d}")
    raw_plan = json.loads(plan_path.read_text())
    repaired_task = raw_plan.get("planned_task")
    if isinstance(repaired_task, dict):
        task = PlannedTask.model_validate(repaired_task)
        _validate_tasks(context=context, config=config, tasks=[task])
        source_candidate_value = evidence.get("source_candidate")
        if isinstance(source_candidate_value, str):
            source_candidate = Path(source_candidate_value)
            if not source_candidate.is_file():
                raise ValueError(
                    f"saved repair evidence is missing its original candidate for {test_id:03d}"
                )
            run_dir = source_candidate.parents[2]
            first_pass = source_candidate.name.startswith("initial_")
        else:
            run_dir_value = evidence.get("run_dir")
            if not isinstance(run_dir_value, str) or not Path(run_dir_value).is_dir():
                raise ValueError(
                    f"saved repair evidence has no authoring directory for {test_id:03d}"
                )
            run_dir = Path(run_dir_value)
            first_pass = str(evidence.get("first_pass")).lower() == "true"
        return (
            {
                "candidate": candidate,
                "gate": gate,
                "plan": plan_path,
                "published": Path(evidence["published"]),
                "run_dir": run_dir,
                "first_pass": first_pass,
            },
            task,
        )
    run_dir = candidate.parents[2]
    run_inputs_path = run_dir / "run_inputs.json"
    if not run_inputs_path.is_file():
        raise ValueError(f"saved authoring inputs are missing for {test_id:03d}")
    run_inputs = json.loads(run_inputs_path.read_text())
    selected_ids = [int(value) for value in run_inputs.get("selected_test_ids") or []]
    selected_positions_value = run_inputs.get("selected_plan_positions")
    selected_positions = (
        [int(value) for value in selected_positions_value]
        if isinstance(selected_positions_value, list)
        else None
    )
    if selected_ids:
        if selected_positions is None or len(selected_ids) != len(selected_positions):
            raise ValueError(f"saved authoring inputs cannot locate {test_id:03d}")
        positions = [
            selected_positions[index]
            for index, selected_id in enumerate(selected_ids)
            if selected_id == test_id
        ]
    else:
        start_id = run_inputs.get("start_id")
        limit = run_inputs.get("limit")
        if start_id is None or (selected_positions is None and limit is None):
            raise ValueError(f"saved authoring inputs cannot locate {test_id:03d}")
        position = test_id - int(start_id) + 1
        if selected_positions is not None:
            positions = [position] if position in selected_positions else []
        else:
            positions = [position] if 1 <= position <= int(limit) else []
    if len(positions) != 1:
        raise ValueError(f"saved authoring inputs have no unique plan position for {test_id:03d}")
    tasks = raw_plan.get("tasks")
    position = positions[0]
    if not isinstance(tasks, list) or not 1 <= position <= len(tasks):
        raise ValueError(f"saved plan has no task for {test_id:03d}")
    raw_task = dict(tasks[position - 1])
    raw_task.setdefault("action_without_memory", "")
    raw_task.setdefault(
        "fact_application_explanations_on_evaluation_date",
        [
            {
                "fact_id": fact_id,
                "why_fact_still_applies_on_evaluation_date": raw_task[
                    "why_memory_changes_result"
                ],
            }
            for fact_id in raw_task["fact_ids"]
        ],
    )
    raw_task.setdefault(
        "why_listed_tools_can_complete_requested_work",
        "The saved plan lists these tools: " + ", ".join(raw_task["expected_tools"]) + ".",
    )
    task = PlannedTask.model_validate(raw_task)
    _validate_tasks(context=context, config=config, tasks=[task])
    attempt_name = "redesign" if candidate.name.startswith("redesign_") else "initial"
    return (
        {
            "candidate": candidate,
            "gate": gate,
            "plan": plan_path,
            "published": Path(evidence["published"]),
            "run_dir": run_dir,
            "first_pass": attempt_name == "initial",
        },
        task,
    )


def _regraded_gate(
    *, candidate_path: Path, saved_gate_path: Path, regrader: Any
) -> dict[str, Any]:
    """Apply revised checks to saved agent output without running the agent again."""
    candidate = _load_yaml_object(candidate_path)
    saved = json.loads(saved_gate_path.read_text())
    result = copy.deepcopy(saved.get("result"))
    if not isinstance(result, dict) or not isinstance(result.get("shots"), list):
        raise ValueError("saved certification result has no executions")
    shots = result["shots"]
    if len(shots) != 4:
        raise ValueError("saved certification result must have four executions")
    for shot in shots:
        if not isinstance(shot, dict):
            raise ValueError("saved certification execution is malformed")
        oracle_result = {
            "tool_trace": copy.deepcopy(shot.get("tool_calls") or []),
            "tool_calls": copy.deepcopy(shot.get("tool_calls") or []),
            "response_text": str(shot.get("response_text") or ""),
        }
        grade = regrader(candidate, oracle_result)
        if not isinstance(grade, dict) or not isinstance(grade.get("passed"), bool):
            raise ValueError("saved execution cannot be graded locally")
        # The corrected candidate has different assertions. Never retain the old
        # assertion details beside the new pass/fail result.
        shot["grade"] = copy.deepcopy(grade)
        shot["passed"] = grade["passed"]
    with_memory = [shot for shot in shots if shot.get("with_memory") is True]
    without_memory = [shot for shot in shots if shot.get("with_memory") is False]
    if len(with_memory) != 2 or len(without_memory) != 2:
        raise ValueError("saved certification executions have invalid memory labels")
    result["g1_pass_count"] = sum(shot["passed"] is True for shot in with_memory)
    result["g2_pass_count"] = sum(shot["passed"] is True for shot in without_memory)
    result["valid"] = result["g1_pass_count"] == 2 and result["g2_pass_count"] == 0
    result["verdict"] = (
        "valid"
        if result["valid"]
        else "unsolvable_with_memory"
        if result["g1_pass_count"] != 2
        else "leaks_without_memory"
    )
    return {"candidate_sha256": _sha256(candidate_path), "result": result}


def _default_regrader(
    candidate: dict[str, Any], oracle_result: dict[str, Any]
) -> dict[str, Any] | None:
    from harness.mining.preview import prod_grade_result

    return prod_grade_result(candidate, oracle_result)


def _grade_correction_request(
    *, candidate_path: Path, review_request_path: Path, decision: FinalTraceDecision
) -> dict[str, Any]:
    review_request = json.loads(review_request_path.read_text())
    assertion_schema = _grading_correction_response_schema(
        _load_yaml_object(candidate_path)["grade"]["config"].get("check_version", 1)
    )
    grading_issues = [
        {
            "issue_index": index,
            "specific_problem": issue.specific_problem,
            "required_correction": issue.required_correction,
            "request_requirement_quote": issue.request_requirement_quote,
            "source_citations": [
                citation.model_dump(mode="json")
                for citation in issue.source_citations
            ],
        }
        for index, issue in enumerate(decision.issues)
        if issue.owning_step == "grading_checks"
    ]
    return {
        "current_test": _load_yaml_object(candidate_path),
        "current_assertions": _load_yaml_object(candidate_path)["grade"]["config"][
            "assertions"
        ],
        "planned_work": review_request.get("planned_work") or {},
        "final_user_request": review_request.get("user_request") or "",
        "starting_app_data": review_request.get("starting_app_data") or {},
        "selected_tool_contracts": review_request.get("selected_tool_contracts") or {},
        "source_evidence": review_request.get("source_evidence") or [],
        "executions": review_request.get("executions") or [],
        "supported_assertion_shapes": assertion_schema["properties"]["operations"],
        "reviewer_grading_issues": grading_issues,
        "reviewer_decision": decision.model_dump(mode="json"),
    }


def _grading_assertion_response_variant(
    assertion_types: list[str],
    *,
    path: bool = False,
    value: bool = False,
    values: bool = False,
    pattern: bool = False,
    criterion: bool = False,
    count: bool = False,
) -> dict[str, Any]:
    """Build one Azure-strict assertion variant with explicit nulls for unused fields."""
    properties: dict[str, Any] = {
        "type": {"type": "string", "enum": assertion_types},
        "tool": {"type": "string"},
        "path": {"type": "string"} if path else {"type": "null"},
        "value": (
            {
                "anyOf": [
                    {"type": "string"},
                    {"type": "number"},
                    {"type": "boolean"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            }
            if value
            else {"type": "null"}
        ),
        "pattern": (
            {"type": "string"}
            if pattern
            else {"type": "null"}
        ),
        "criterion": (
            {"type": "string"}
            if criterion
            else {"type": "null"}
        ),
        "count": (
            {"type": "integer"}
            if count
            else {"type": "null"}
        ),
    }
    if values:
        properties["values"] = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _grading_correction_response_schema(check_version: int = 1) -> dict[str, Any]:
    """Return the strict response schema for one grading-correction call."""
    assertion_variants = [
        _grading_assertion_response_variant(
            ["tool_called", "tool_not_called"]
        ),
        _grading_assertion_response_variant(
            ["tool_call_count"], count=True
        ),
        _grading_assertion_response_variant(
            [
                "field_equals",
                "field_lte",
                "field_gte",
                "field_eq_number",
                "date_on",
                "datetime_absolute_eq",
                "date_on_or_after",
                "date_on_or_before",
                "time_of_day_gte",
                "time_of_day_lte",
            ],
            path=True,
            value=True,
        ),
        _grading_assertion_response_variant(
            [
                "field_regex",
                "field_not_regex",
                "tool_called_with_arg_regex",
                "tool_not_called_with_arg_regex",
            ],
            path=True,
            pattern=True,
        ),
        _grading_assertion_response_variant(
            ["tool_called_with_value"], pattern=True
        ),
        _grading_assertion_response_variant(
            ["field_llm_judge"], path=True, criterion=True
        ),
        _grading_assertion_response_variant(
            ["recipient_matches"], path=True, value=True
        ),
        _grading_assertion_response_variant(
            ["field_list_includes", "email_recipients_received"], path=True, values=True
        ),
        _grading_assertion_response_variant(
            ["field_absent_or_empty"], path=True
        ),
    ]
    if check_version == 2:
        from graders.explicit import CHECK_TYPES
        current = []
        for variant in assertion_variants:
            props = variant["properties"]
            props["type"]["enum"] = [kind for kind in props["type"]["enum"] if kind in CHECK_TYPES]
            if not props["type"]["enum"]:
                continue
            props.update(check_id={"type": "string"}, action_id={"type": "string"})
            variant["required"] = list(props)
            current.append(variant)
        assertion_variants = current
    def operation_schema(
        operation: str, *, assertion: dict[str, Any]
    ) -> dict[str, Any]:
        properties = {
            "operation": {"type": "string", "enum": [operation]},
            "issue_indexes": {"type": "array", "items": {"type": "integer"}},
            "assertion_index": {"type": "integer"},
            "assertion": assertion,
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        }

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["operations", "expected_no_history_failed_check_index"],
        "properties": {
            "operations": {
                "type": "array",
                "items": {
                    "anyOf": [
                        operation_schema("delete", assertion={"type": "null"}),
                        operation_schema(
                            "replace", assertion={"anyOf": assertion_variants}
                        ),
                        operation_schema(
                            "add", assertion={"anyOf": assertion_variants}
                        ),
                    ]
                },
            },
            "expected_no_history_failed_check_index": {
                "type": "integer",
            },
        },
    }


def _default_grading_corrector(*, request: dict[str, Any], out: Path, client: Any) -> dict[str, Any]:
    schema = _grading_correction_response_schema(
        request.get("current_test", {}).get("grade", {}).get("config", {}).get("check_version", 1)
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "grading_correction_system.txt").write_text(GRADING_CHECK_CORRECTION_SYSTEM + "\n")
    dump_json(out / "grading_correction_request.json", request)
    dump_json(out / "grading_correction_schema.json", schema)
    raw = cached_client_complete(
        out / "work" / "grading_correction_response_cache.json",
        GRADING_CHECK_CORRECTION_SYSTEM,
        request,
        client,
        response_schema=schema,
        response_schema_name="enact_grading_check_correction",
    )
    dump_json(out / "grading_correction_response.json", raw)
    return {key: value for key, value in raw.items() if not key.startswith("_")}


def _validate_grading_assertions(assertions: list[dict[str, Any]]) -> None:
    field_types = {
        "field_list_includes",
        "datetime_absolute_eq",
        "field_equals",
        "field_lte",
        "field_gte",
        "field_eq_number",
        "date_on",
        "date_on_or_after",
        "date_on_or_before",
        "time_of_day_gte",
        "time_of_day_lte",
        "field_regex",
        "field_not_regex",
        "tool_called_with_arg_regex",
        "tool_not_called_with_arg_regex",
        "field_llm_judge",
        "recipient_matches",
        "field_absent_or_empty",
    }
    regex_types = {
        "field_regex",
        "field_not_regex",
        "tool_called_with_arg_regex",
        "tool_called_with_value",
        "tool_not_called_with_arg_regex",
    }
    value_types = {
        "datetime_absolute_eq",
        "field_equals",
        "field_lte",
        "field_gte",
        "field_eq_number",
        "date_on",
        "date_on_or_after",
        "date_on_or_before",
        "time_of_day_gte",
        "time_of_day_lte",
        "recipient_matches",
    }
    for index, assertion in enumerate(assertions):
        assertion_type = assertion.get("type")
        tool = assertion.get("tool")
        if not isinstance(assertion_type, str) or not assertion_type.strip():
            raise ValueError(f"grading assertion {index} has no type")
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError(f"grading assertion {index} has no tool")
        if assertion_type in field_types and (
            not isinstance(assertion.get("path"), str)
            or not assertion["path"].strip()
        ):
            raise ValueError(f"grading assertion {index} has no path")
        if assertion_type in regex_types and (
            not isinstance(assertion.get("pattern"), str)
            or not assertion["pattern"].strip()
        ):
            raise ValueError(f"grading assertion {index} has no pattern")
        if assertion_type == "field_llm_judge" and (
            not isinstance(assertion.get("criterion"), str)
            or not assertion["criterion"].strip()
        ):
            raise ValueError(f"grading assertion {index} has no criterion")
        if assertion_type in value_types and assertion.get("value") is None:
            raise ValueError(f"grading assertion {index} has no value")
        if assertion_type in {"field_list_includes", "email_recipients_received"}:
            values = assertion.get("values")
            if not isinstance(values, list) or not values or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ValueError(f"grading assertion {index} needs string values")
        if assertion_type == "tool_call_count":
            count = assertion.get("count")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError(
                    f"grading assertion {index} needs a non-negative integer count"
                )


def _corrected_grading_candidate(
    *,
    source: Path,
    destination: Path,
    correction: dict[str, Any],
    decision: FinalTraceDecision,
) -> int:
    """Apply a reviewer-bound grading patch without changing untouched assertions."""
    if set(correction) != {"operations", "expected_no_history_failed_check_index"}:
        raise ValueError("grading correction may change only named assertion operations and failed-check index")
    operations = correction.get("operations")
    failed_index = correction.get("expected_no_history_failed_check_index")
    if not isinstance(operations, list) or not operations:
        raise ValueError("grading correction must return at least one assertion operation")
    if not isinstance(failed_index, int) or isinstance(failed_index, bool):
        raise ValueError("grading correction must return an integer failed-check index")
    before = _load_yaml_object(source)
    grade = before.get("grade")
    if not isinstance(grade, dict) or not isinstance(grade.get("config"), dict):
        raise ValueError("published test has no grading assertion configuration")
    original_assertions = grade["config"].get("assertions")
    if not isinstance(original_assertions, list) or not all(
        isinstance(item, dict) for item in original_assertions
    ):
        raise ValueError("published test has an invalid grading assertion list")

    grading_issue_indexes = {
        index
        for index, issue in enumerate(decision.issues)
        if issue.owning_step == "grading_checks"
    }
    if not grading_issue_indexes:
        raise ValueError("grading correction has no reviewer grading-check issue")

    deletes: set[int] = set()
    replacements: dict[int, dict[str, Any]] = {}
    additions: dict[int, dict[str, Any]] = {}
    operation_indexes: set[int] = set()
    issue_indexes: set[int] = set()
    for operation_index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise ValueError(f"grading correction operation {operation_index} is not an object")
        if set(operation) not in (
            {"operation", "issue_index", "assertion_index", "assertion"},
            {"operation", "issue_indexes", "assertion_index", "assertion"},
        ):
            raise ValueError(f"grading correction operation {operation_index} has unexpected fields")
        kind = operation.get("operation")
        addressed_issues = operation.get("issue_indexes", [operation.get("issue_index")])
        assertion_index = operation.get("assertion_index")
        assertion = operation.get("assertion")
        if kind not in {"delete", "replace", "add"}:
            raise ValueError(f"grading correction operation {operation_index} has invalid operation")
        if (
            not isinstance(addressed_issues, list)
            or not addressed_issues
            or any(
                not isinstance(index, int) or isinstance(index, bool)
                or index not in grading_issue_indexes
                for index in addressed_issues
            )
            or len(set(addressed_issues)) != len(addressed_issues)
        ):
            raise ValueError(
                f"grading correction operation {operation_index} does not reference a reviewer grading-check issue"
            )
        if not isinstance(assertion_index, int) or isinstance(assertion_index, bool):
            raise ValueError(f"grading correction operation {operation_index} has invalid assertion index")
        maximum_index = len(original_assertions) if kind == "add" else len(original_assertions) - 1
        if not 0 <= assertion_index <= maximum_index:
            raise ValueError(f"grading correction operation {operation_index} has assertion index out of range")
        if assertion_index in operation_indexes:
            raise ValueError(f"grading correction has conflicting operations for assertion index {assertion_index}")
        operation_indexes.add(assertion_index)
        issue_indexes.update(addressed_issues)
        if kind == "delete":
            if assertion is not None:
                raise ValueError(f"grading correction delete {operation_index} must use null assertion")
            deletes.add(assertion_index)
        else:
            if not isinstance(assertion, dict):
                raise ValueError(f"grading correction {kind} {operation_index} must include an assertion")
            _validate_grading_assertions([assertion])
            if kind == "replace":
                replacements[assertion_index] = assertion
            else:
                additions[assertion_index] = assertion
    if issue_indexes != grading_issue_indexes:
        raise ValueError("grading correction must address every reviewer grading-check issue")

    assertions: list[dict[str, Any]] = []
    for index, assertion in enumerate(original_assertions):
        if index in additions:
            assertions.append(copy.deepcopy(additions[index]))
        if index in deletes:
            continue
        assertions.append(copy.deepcopy(replacements.get(index, assertion)))
    if len(original_assertions) in additions:
        assertions.append(copy.deepcopy(additions[len(original_assertions)]))
    _validate_grading_assertions(assertions)
    if not assertions or not 0 <= failed_index < len(assertions):
        raise ValueError("grading correction has an invalid failed-check index")

    after = copy.deepcopy(before)
    after["grade"]["config"]["assertions"] = assertions
    if after["grade"]["config"].get("check_version") == 2:
        from graders.explicit import validate_checks
        # Strict response schemas use nulls for unused fields; the stored checks
        # contain only declared operands, plus their stable IDs.
        assertions = [{key: value for key, value in check.items()
                       if value is not None or (key == "value" and check["type"] == "field_equals")}
                      for check in assertions]
        validate_checks(assertions)
        after["grade"]["config"]["assertions"] = assertions
    TestSpec.model_validate(after)
    before_without_assertions = copy.deepcopy(before)
    after_without_assertions = copy.deepcopy(after)
    del before_without_assertions["grade"]["config"]["assertions"]
    del after_without_assertions["grade"]["config"]["assertions"]
    if before_without_assertions != after_without_assertions:
        raise ValueError("grading correction changed a field other than assertions")
    _write_yaml(destination, after)
    return failed_index


def _redesign_from_same_facts(
    *,
    test_id: int,
    record: dict[str, Any],
    planned_task: PlannedTask,
    context: Any,
    config: Any,
    work_dir: Path,
    failure: dict[str, Any],
    design_client: AzureJsonClient,
    query_client: AzureJsonClient,
    certifier: Any,
) -> tuple[Path | None, Path | None, dict[str, Any]]:
    try:
        _design, previous_design, previous_query = _load_saved_authoring_artifacts(
            record=record,
            planned_task=planned_task,
            context=context,
            test_id=test_id,
        )
    except (FileNotFoundError, ValueError):
        previous_design = {}
        previous_query = {}
    return _attempt(
        directory=work_dir,
        attempt_name="same_facts_redesign",
        design_system=DESIGN_SYSTEM + "\n\n" + REDESIGN_SYSTEM,
        design_request={
            "original_input": design_payload(
                config=config,
                context=context,
                planned_task=planned_task,
            ),
            "previous_design": previous_design,
            "previous_query": previous_query,
            "failure_to_fix": failure,
        },
        planned_task=planned_task,
        context=context,
        config=config,
        test_id=test_id,
        design_client=design_client,
        query_client=query_client,
        certifier=certifier,
    )


def _replan_from_same_facts(
    *,
    original_task: PlannedTask,
    context: Any,
    config: Any,
    work_dir: Path,
    failure: dict[str, Any],
) -> PlannedTask:
    request = {
        "original_input": design_payload(
            config=config,
            context=context,
            planned_task=original_task,
        ),
        "rejected_plan": original_task.model_dump(mode="json"),
        "failure_to_fix": failure,
    }
    schema = TestProposal.model_json_schema()
    raw = cached_client_complete(
        work_dir / "work" / "same_facts_replan_response_cache.json",
        SAME_FACTS_REPLAN_SYSTEM,
        request,
        AzureJsonClient(model=config.planner_model, reasoning_effort="high"),
        response_schema=schema,
        response_schema_name="enact_same_facts_replan",
    )
    dump_json(work_dir / "same_facts_replan_request.json", request)
    dump_json(work_dir / "same_facts_replan_response.json", raw)
    proposal = TestProposal.model_validate(
        {key: value for key, value in raw.items() if not key.startswith("_")}
    )
    if set(proposal.fact_ids) != set(original_task.fact_ids):
        raise ValueError("same-facts replanning changed the selected fact IDs")
    task = proposal.to_planned_task()
    _validate_tasks(context=context, config=config, tasks=[task])
    return task


def _complete_same_facts_design(
    *,
    test_id: int,
    record: dict[str, Any],
    planned_task: PlannedTask,
    context: Any,
    config: Any,
    work_dir: Path,
    failure: dict[str, Any],
    design_client: AzureJsonClient,
    query_client: AzureJsonClient,
    certifier: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any], Path | None, Path | None]:
    candidate, first = _redesign_from_same_facts(
        test_id=test_id,
        record=record,
        planned_task=planned_task,
        context=context,
        config=config,
        work_dir=work_dir,
        failure=failure,
        design_client=design_client,
        query_client=query_client,
        certifier=certifier,
    )
    if candidate is not None:
        return (
            candidate,
            {"initial": first, "correction": None},
            work_dir / "same_facts_redesign_candidate.yaml",
            work_dir / "same_facts_redesign_gate.json",
        )
    if first.get("stage") == "proposal_rejected":
        return None, {"initial": first, "correction": None}, None, None

    second_request = {
        "original_input": design_payload(
            config=config,
            context=context,
            planned_task=planned_task,
        ),
        "previous_design": first.get("design") or {},
        "previous_query": first.get("query") or {},
        "failure_to_fix": {
            "stage": first.get("stage"),
            "reason": first.get("reason"),
            "gate": first.get("gate") or {},
        },
    }
    candidate, second = _attempt(
        directory=work_dir,
        attempt_name="same_facts_design_correction",
        design_system=DESIGN_SYSTEM + "\n\n" + REDESIGN_SYSTEM,
        design_request=second_request,
        planned_task=planned_task,
        context=context,
        config=config,
        test_id=test_id,
        design_client=design_client,
        query_client=query_client,
        certifier=certifier,
    )
    return (
        candidate,
        {"initial": first, "correction": second},
        work_dir / "same_facts_design_correction_candidate.yaml"
        if candidate is not None
        else None,
        work_dir / "same_facts_design_correction_gate.json"
        if candidate is not None
        else None,
    )


def _correct_same_facts_final_review(
    *,
    test_id: int,
    decision: FinalTraceDecision,
    candidate: Path,
    gate: Path,
    planned_task: PlannedTask,
    context: Any,
    config: Any,
    work_dir: Path,
    authoring_attempt: dict[str, Any],
    design_client: AzureJsonClient,
    query_client: AzureJsonClient,
    grading_corrector: Any,
    regrader: Any,
    certifier: Any,
) -> tuple[Path | None, Path | None, dict[str, Any]]:
    feedback = _final_review_feedback(decision)
    if decision.part_to_correct == "grading_checks":
        correction = grading_corrector(
            request=_grade_correction_request(
                candidate_path=candidate,
                review_request_path=(
                    work_dir
                    / "final_trace_review"
                    / f"{test_id:03d}"
                    / "request.json"
                ),
                decision=decision,
            ),
            out=work_dir / "same_facts_final_grading_correction",
            client=design_client,
        )
        corrected = work_dir / "same_facts_final_correction_candidate.yaml"
        _corrected_grading_candidate(
            source=candidate,
            destination=corrected,
            correction=correction,
            decision=decision,
        )
        corrected_regrade = work_dir / "same_facts_final_correction_regrade.json"
        corrected_gate = work_dir / "same_facts_final_correction_gate.json"
        regraded = _regraded_gate(
            candidate_path=corrected,
            saved_gate_path=gate,
            regrader=regrader,
        )
        dump_json(corrected_regrade, regraded)
        if _requires_execution_rerun(decision) or regraded["result"]["g1_pass_count"] != 2:
            gate_result = _certify_cached(
                persona=config.persona,
                checkpoint=context.checkpoint_path,
                candidate_path=corrected,
                result_path=corrected_gate,
                certifier=certifier,
            )
            if not gate_result.get("valid"):
                return None, None, {"stage": "certification", "result": gate_result}
        elif regraded["result"]["g2_pass_count"] != 0:
            return None, None, {
                "stage": "certification",
                "reason": "saved no-history execution passes corrected checks",
            }
        else:
            corrected_gate = corrected_regrade
        return corrected, corrected_gate, {"stage": "corrected_grading_checks"}

    latest_attempt = authoring_attempt.get("correction") or authoring_attempt.get("initial") or {}
    if decision.part_to_correct == "user_request":
        design = DesignResponse.model_validate(latest_attempt.get("design") or {})
        candidate_data, attempt = _attempt(
            directory=work_dir,
            attempt_name="same_facts_final_request_correction",
            design_system="",
            design_request={},
            planned_task=planned_task,
            context=context,
            config=config,
            test_id=test_id,
            design_client=design_client,
            query_client=query_client,
            certifier=certifier,
            reuse_design=design,
            query_final_review_feedback=feedback,
        )
        return (
            work_dir / "same_facts_final_request_correction_candidate.yaml"
            if candidate_data is not None
            else None,
            work_dir / "same_facts_final_request_correction_gate.json"
            if candidate_data is not None
            else None,
            attempt,
        )

    if decision.part_to_correct in {"test_design", "starting_app_data"}:
        candidate_data, attempt = _attempt(
            directory=work_dir,
            attempt_name="same_facts_final_design_correction",
            design_system=DESIGN_SYSTEM + "\n\n" + REDESIGN_SYSTEM,
            design_request={
                "original_input": design_payload(
                    config=config,
                    context=context,
                    planned_task=planned_task,
                ),
                "previous_design": latest_attempt.get("design") or {},
                "previous_query": latest_attempt.get("query") or {},
                "failure_to_fix": decision.model_dump(mode="json"),
            },
            planned_task=planned_task,
            context=context,
            config=config,
            test_id=test_id,
            design_client=design_client,
            query_client=query_client,
            certifier=certifier,
        )
        return (
            work_dir / "same_facts_final_design_correction_candidate.yaml"
            if candidate_data is not None
            else None,
            work_dir / "same_facts_final_design_correction_gate.json"
            if candidate_data is not None
            else None,
            attempt,
        )

    if decision.part_to_correct == "execution":
        corrected = work_dir / "same_facts_final_execution_candidate.yaml"
        shutil.copyfile(candidate, corrected)
        corrected_gate = work_dir / "same_facts_final_execution_gate.json"
        gate_result = _certify_cached(
            persona=config.persona,
            checkpoint=context.checkpoint_path,
            candidate_path=corrected,
            result_path=corrected_gate,
            certifier=certifier,
        )
        return (
            corrected if gate_result.get("valid") else None,
            corrected_gate if gate_result.get("valid") else None,
            {"stage": "certification", "result": gate_result},
        )

    return None, None, {
        "stage": "planning",
        "reason": "the replanned task still failed planning review",
    }


_CONTINUATION_REVIEW_STAGES = (
    "continuation_final_review",
    "same_facts_final_correction_review",
    "same_facts_redesign_final_review",
    "targeted_final_review",
    "final_review",
    "initial_review",
)


def _json_sha256(value: Any) -> str:
    """Hash a saved JSON value without depending on filesystem timestamps."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _approved_grading_replacements(
    *, change: dict[str, Any], test_id: int
) -> list[dict[str, Any]]:
    """Return exact assertion edits; a null replacement explicitly deletes it."""
    required = {
        "assertion_index",
        "expected_assertion_sha256",
        "replacement_assertion",
    }
    if set(change) == required:
        replacements = [change]
    elif set(change) == {"assertion_replacements"}:
        replacements = change["assertion_replacements"]
        if not isinstance(replacements, list) or not replacements:
            raise ValueError(
                f"approved grading correction for {test_id:03d} must contain "
                "at least one assertion replacement"
            )
    else:
        raise ValueError(
            f"approved grading correction for {test_id:03d} must contain one exact "
            "assertion replacement or an assertion_replacements list"
        )

    indexes: set[int] = set()
    for replacement in replacements:
        if not isinstance(replacement, dict) or set(replacement) != required:
            raise ValueError(
                f"each approved grading replacement for {test_id:03d} must contain "
                f"exactly {sorted(required)}"
            )
        index = replacement["assertion_index"]
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError(
                f"approved correction for {test_id:03d} has an invalid assertion index"
            )
        if index in indexes:
            raise ValueError(
                f"approved correction for {test_id:03d} repeats assertion index {index}"
            )
        indexes.add(index)
        if replacement["replacement_assertion"] is None:
            continue
        if not isinstance(replacement["replacement_assertion"], dict):
            raise ValueError(
                f"approved correction for {test_id:03d} has no replacement assertion"
            )
        _validate_grading_assertions([replacement["replacement_assertion"]])
    return replacements


def _load_approved_corrections(
    *,
    path: Path,
    expected_test_ids: set[int],
    out: Path,
    source_result_paths: dict[int, Path] | None = None,
    source_root: Path | None = None,
) -> dict[int, dict[str, Any]]:
    """Load exact human-approved changes and authenticate every source file."""
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict) or set(raw) != {"corrections"}:
        raise ValueError("approved corrections must contain only a corrections list")
    rows = raw["corrections"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("approved corrections must contain at least one correction")
    corrections: dict[int, dict[str, Any]] = {}
    required = {
        "test_id",
        "source_result_sha256",
        "part_to_correct",
        "specific_problem",
        "required_correction",
        "source_candidate",
        "source_candidate_sha256",
        "source_gate",
        "source_gate_sha256",
        "change",
    }
    for row in rows:
        if not isinstance(row, dict) or set(row) != required:
            raise ValueError(f"approved correction must contain exactly {sorted(required)}")
        test_id = row["test_id"]
        if not isinstance(test_id, int) or isinstance(test_id, bool) or test_id <= 0:
            raise ValueError("approved correction has an invalid test ID")
        if test_id in corrections:
            raise ValueError(f"approved correction repeats test {test_id:03d}")
        if row["part_to_correct"] not in {"grading_checks", "user_request", "test_design"}:
            raise ValueError(
                f"approved correction for {test_id:03d} must change grading_checks, "
                "user_request, or test_design"
            )
        for key in ("specific_problem", "required_correction"):
            if not isinstance(row[key], str) or not row[key].strip():
                raise ValueError(f"approved correction for {test_id:03d} has no {key}")

        result_path = (
            source_result_paths[test_id]
            if source_result_paths is not None
            else out / "repair" / f"{test_id:03d}" / "result.json"
        )
        if not result_path.is_file() or _sha256(result_path) != row["source_result_sha256"]:
            raise ValueError(f"approved correction source result changed for {test_id:03d}")
        authenticated = copy.deepcopy(row)
        for name in ("source_candidate", "source_gate"):
            source_path = Path(row[name])
            if not source_path.is_absolute():
                source_path = (path.parent / source_path).resolve()
            expected_hash = row[f"{name}_sha256"]
            if not source_path.is_file() or _sha256(source_path) != expected_hash:
                raise ValueError(f"approved correction {name} changed for {test_id:03d}")
            if source_root is not None and not source_path.is_relative_to(source_root):
                raise ValueError(
                    f"approved correction {name} for {test_id:03d} is outside the source run"
                )
            authenticated[name] = str(source_path)
        _authenticated_saved_gate(
            test_id=test_id,
            candidate=Path(authenticated["source_candidate"]),
            gate=Path(authenticated["source_gate"]),
        )

        change = row["change"]
        if not isinstance(change, dict):
            raise ValueError(f"approved correction for {test_id:03d} has no exact change")
        if row["part_to_correct"] == "grading_checks":
            replacements = _approved_grading_replacements(
                change=change, test_id=test_id
            )
            candidate_value = _load_yaml_object(Path(authenticated["source_candidate"]))
            assertions = candidate_value.get("grade", {}).get("config", {}).get("assertions")
            for replacement in replacements:
                index = replacement["assertion_index"]
                if not isinstance(assertions, list) or not 0 <= index < len(assertions):
                    raise ValueError(
                        f"approved grading correction for {test_id:03d} has an invalid assertion index"
                    )
                if _json_sha256(assertions[index]) != replacement[
                    "expected_assertion_sha256"
                ]:
                    raise ValueError(
                        f"approved grading correction source assertion changed for {test_id:03d}"
                    )
        elif row["part_to_correct"] == "user_request":
            if set(change) != {"replacement_request"}:
                raise ValueError(
                    f"approved request correction for {test_id:03d} must contain only replacement_request"
                )
            if not isinstance(change["replacement_request"], str) or not change[
                "replacement_request"
            ].strip():
                raise ValueError(f"approved correction for {test_id:03d} has no replacement request")
        else:
            if set(change) != {"redesign_instruction"}:
                raise ValueError(
                    f"approved design correction for {test_id:03d} must contain only redesign_instruction"
                )
            if not isinstance(change["redesign_instruction"], str) or not change[
                "redesign_instruction"
            ].strip():
                raise ValueError(f"approved correction for {test_id:03d} has no redesign instruction")
        corrections[test_id] = authenticated
    if set(corrections) != expected_test_ids:
        raise ValueError(
            "approved correction IDs must exactly match --test-ids: "
            f"expected {sorted(expected_test_ids)}, found {sorted(corrections)}"
        )
    return corrections


def _apply_approved_correction(
    *, source: Path, destination: Path, correction: dict[str, Any]
) -> None:
    """Apply only the literal field replacement recorded in an approval file."""
    before = _load_yaml_object(source)
    after = copy.deepcopy(before)
    change = correction["change"]
    if correction["part_to_correct"] == "grading_checks":
        assertions = after.get("grade", {}).get("config", {}).get("assertions")
        if not isinstance(assertions, list):
            raise ValueError("approved grading correction source has no assertions")
        replacements = _approved_grading_replacements(
            change=change, test_id=int(correction.get("test_id") or 0)
        )
        for replacement in sorted(replacements, key=lambda row: row["assertion_index"], reverse=True):
            index = replacement["assertion_index"]
            if not 0 <= index < len(assertions):
                raise ValueError("approved grading correction assertion index is out of range")
            if _json_sha256(assertions[index]) != replacement[
                "expected_assertion_sha256"
            ]:
                raise ValueError("approved grading correction source assertion changed")
            if replacement["replacement_assertion"] is None:
                del assertions[index]
            else:
                assertions[index] = copy.deepcopy(replacement["replacement_assertion"])
        _validate_grading_assertions(assertions)
        if after["grade"]["config"].get("check_version") == 2:
            from graders.explicit import validate_checks
            validate_checks(assertions)
    elif correction["part_to_correct"] == "user_request":
        after["test"] = change["replacement_request"].strip()
    else:
        raise ValueError("approved test-design corrections must run through test design")
    TestSpec.model_validate(after)
    _write_yaml(destination, after)


def _authenticated_saved_gate(
    *, test_id: int, candidate: Path, gate: Path
) -> dict[str, Any]:
    """Return one saved four-shot gate only when it belongs to this candidate."""
    if not candidate.is_file() or not gate.is_file():
        raise ValueError(f"saved continuation evidence is missing for {test_id:03d}")
    saved = json.loads(gate.read_text())
    if saved.get("candidate_sha256") != _sha256(candidate):
        raise ValueError(f"saved continuation gate does not match candidate for {test_id:03d}")
    result = saved.get("result")
    shots = result.get("shots") if isinstance(result, dict) else None
    if not isinstance(shots, list) or len(shots) != 4:
        raise ValueError(f"saved continuation gate has no four-shot result for {test_id:03d}")
    with_memory = [shot for shot in shots if isinstance(shot, dict) and shot.get("with_memory") is True]
    without_memory = [shot for shot in shots if isinstance(shot, dict) and shot.get("with_memory") is False]
    if len(with_memory) != 2 or len(without_memory) != 2:
        raise ValueError(f"saved continuation gate has invalid memory labels for {test_id:03d}")
    return saved


def _saved_failure_decision(result: dict[str, Any]) -> tuple[str, FinalTraceDecision] | None:
    """Find the latest explicit reviewer decision recorded in one repair result."""
    for stage in _CONTINUATION_REVIEW_STAGES:
        raw = result.get(stage)
        if not isinstance(raw, dict):
            continue
        decision = FinalTraceDecision.model_validate(raw)
        if not decision.accept:
            return stage, decision
    return None


def _saved_continuation_local_failure(result: dict[str, Any]) -> dict[str, Any] | None:
    """Read the structured local validation failure from the latest continuation."""
    saved = result.get("continuation_failure")
    if isinstance(saved, dict):
        if (
            saved.get("kind") == "local_validation"
            and isinstance(saved.get("part_to_correct"), str)
            and isinstance(saved.get("reason"), str)
            and saved["reason"].strip()
        ):
            return saved
        raise ValueError("saved continuation failure is malformed")

    # Compatibility for continuations written before continuation_failure was
    # recorded explicitly. These fields are named saved outputs, not inferred
    # from timestamps or directory contents.
    continuation = result.get("continuation")
    attempt = result.get("attempt")
    if (
        isinstance(continuation, dict)
        and isinstance(attempt, dict)
        and attempt.get("stage") == "local_validation"
        and isinstance(attempt.get("reason"), str)
        and attempt["reason"].strip()
        and isinstance(continuation.get("changed_step"), str)
    ):
        return {
            "kind": "local_validation",
            "part_to_correct": continuation["changed_step"],
            "reason": attempt["reason"],
            "source_attempt": attempt,
        }
    return None


def _latest_saved_candidate_and_gate(
    *,
    test_id: int,
    result: dict[str, Any],
    work_dir: Path,
    fallback: dict[str, Any],
    require_review_request: bool = True,
) -> tuple[Path, Path, Path]:
    """Resolve a saved reviewer stage to its exact candidate, gate, and request file."""
    found = _saved_failure_decision(result)
    if found is None:
        raise ValueError(f"saved repair has no reviewer decision for {test_id:03d}")
    stage, _decision = found
    if stage == "continuation_final_review":
        continuation = result.get("continuation")
        if not isinstance(continuation, dict):
            raise ValueError(f"saved continuation has no evidence paths for {test_id:03d}")
        candidate_value = continuation.get("candidate")
        gate_value = continuation.get("gate")
        request_value = continuation.get("review_request")
        if (
            not isinstance(candidate_value, str)
            or not isinstance(gate_value, str)
            or (require_review_request and not isinstance(request_value, str))
        ):
            raise ValueError(f"saved continuation evidence is incomplete for {test_id:03d}")
        candidate = Path(candidate_value)
        gate = Path(gate_value)
        request = (
            Path(request_value)
            if isinstance(request_value, str)
            else work_dir / "missing_old_review_request.json"
        )
    elif stage == "same_facts_final_correction_review":
        prior = result.get("targeted_final_review") or result.get(
            "same_facts_redesign_final_review"
        )
        part = prior.get("part_to_correct") if isinstance(prior, dict) else None
        filenames = {
            "grading_checks": (
                "same_facts_final_correction_candidate.yaml",
                "same_facts_final_correction_gate.json",
            ),
            "execution": (
                "same_facts_final_execution_candidate.yaml",
                "same_facts_final_execution_gate.json",
            ),
            "user_request": (
                "same_facts_final_request_correction_candidate.yaml",
                "same_facts_final_request_correction_gate.json",
            ),
            "test_design": (
                "same_facts_final_design_correction_candidate.yaml",
                "same_facts_final_design_correction_gate.json",
            ),
            "starting_app_data": (
                "same_facts_final_design_correction_candidate.yaml",
                "same_facts_final_design_correction_gate.json",
            ),
        }.get(part)
        if filenames is None:
            raise ValueError(
                f"saved final correction has no explicit evidence mapping for {test_id:03d}"
            )
        candidate = work_dir / filenames[0]
        gate = work_dir / filenames[1]
        request = work_dir / "same_facts_final_correction_review" / f"{test_id:03d}" / "request.json"
    elif stage == "same_facts_redesign_final_review":
        redesign = result.get("same_facts_redesign")
        correction = redesign.get("correction") if isinstance(redesign, dict) else None
        if isinstance(correction, dict) and (work_dir / "same_facts_design_correction_candidate.yaml").is_file():
            candidate = work_dir / "same_facts_design_correction_candidate.yaml"
            gate = work_dir / "same_facts_design_correction_gate.json"
        else:
            candidate = work_dir / "same_facts_redesign_candidate.yaml"
            gate = work_dir / "same_facts_redesign_gate.json"
        request = work_dir / "same_facts_redesign_final_review" / f"{test_id:03d}" / "request.json"
    elif stage == "targeted_final_review":
        action = result.get("targeted_action") or result.get("action")
        candidates = {
            "corrected_grading_checks": ("grading_checks_candidate.yaml", "grading_checks_gate.json"),
            "rewrote_user_request": ("user_request_candidate.yaml", "user_request_gate.json"),
            "reran_certification": ("execution_candidate.yaml", "execution_gate.json"),
        }
        filenames = candidates.get(action)
        if filenames is None:
            raise ValueError(f"saved targeted review has no explicit evidence mapping for {test_id:03d}")
        candidate = work_dir / filenames[0]
        gate = work_dir / filenames[1]
        request = work_dir / "final_trace_review" / f"{test_id:03d}" / "request.json"
    elif stage == "final_review":
        candidate = Path(fallback["candidate"])
        gate = Path(fallback["gate"])
        request = work_dir / "final_trace_review" / f"{test_id:03d}" / "request.json"
    else:
        candidate = Path(fallback["candidate"])
        gate = Path(fallback["gate"])
        request = work_dir / "initial_review_request.json"
    _authenticated_saved_gate(test_id=test_id, candidate=candidate, gate=gate)
    if require_review_request and not request.is_file():
        raise ValueError(f"saved reviewer request is missing for {test_id:03d}")
    return candidate, gate, request


def _snapshot_previous_repair_result(*, result_path: Path, saved: dict[str, Any]) -> Path:
    """Preserve a repair result once before writing its continuation result."""
    digest = _sha256(result_path)
    snapshot = result_path.parent / "previous_results" / f"{digest}.json"
    if snapshot.is_file():
        if _sha256(snapshot) != digest:
            raise ValueError(f"previous repair snapshot changed: {snapshot}")
        return snapshot
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(result_path.read_bytes())
    if _sha256(snapshot) != digest:
        raise ValueError(f"could not preserve previous repair result: {result_path}")
    return snapshot


def _continuation_inputs_identity(
    *,
    test_id: int,
    result_path: Path,
    snapshot: Path,
    failure_hash: str,
    planned_task: PlannedTask,
    source_candidate: Path,
    source_gate: Path,
    correction_operation: str,
) -> dict[str, Any]:
    """Bind one continuation directory to the exact saved work it continues."""
    source_result_sha256 = _sha256(result_path)
    source_snapshot_sha256 = _sha256(snapshot)
    planned_task_data = planned_task.model_dump(mode="json")
    if source_result_sha256 != source_snapshot_sha256:
        raise ValueError(f"source result changed before continuation for {test_id:03d}")
    return {
        "test_id": test_id,
        "source_result": str(result_path.resolve()),
        "source_result_sha256": source_result_sha256,
        "source_snapshot": str(snapshot.resolve()),
        "source_snapshot_sha256": source_snapshot_sha256,
        "source_failure_sha256": failure_hash,
        "planned_task": planned_task_data,
        "planned_task_sha256": _json_sha256(planned_task_data),
        "source_candidate": str(source_candidate.resolve()),
        "source_candidate_sha256": _sha256(source_candidate),
        "source_gate": str(source_gate.resolve()),
        "source_gate_sha256": _sha256(source_gate),
        "correction_operation": correction_operation,
    }


def _legacy_grading_validation_exception(
    *, test_id: int, result_path: Path, source_result_sha256: str
) -> dict[str, Any]:
    """Return the one saved local grading-validation failure for this result."""
    matches: list[dict[str, Any]] = []
    for evidence_path in sorted(
        (result_path.parent / "continuation_exceptions").glob("*.json")
    ):
        evidence = json.loads(evidence_path.read_text())
        traceback_text = evidence.get("traceback")
        if (
            evidence_path.stem == _json_sha256(evidence)
            and evidence.get("test_id") == test_id
            and Path(str(evidence.get("source_result") or "")).resolve()
            == result_path.resolve()
            and evidence.get("source_result_sha256") == source_result_sha256
            and evidence.get("exception_type") == "ValueError"
            and isinstance(traceback_text, str)
            and "in _corrected_grading_candidate" in traceback_text
            and "in _validate_grading_assertions" in traceback_text
        ):
            matches.append(evidence)
    if len(matches) != 1:
        raise ValueError(
            f"legacy continuation has no unique saved grading-validation failure for "
            f"{test_id:03d}"
        )
    return matches[0]


def _authenticate_legacy_grading_partial(
    *,
    test_id: int,
    continuation_dir: Path,
    identity: dict[str, Any],
    source_review_request: Path | None,
) -> None:
    """Authenticate the files left before an old grading correction wrote a candidate."""
    required_files = {
        "grading_checks/grading_correction_request.json",
        "grading_checks/grading_correction_schema.json",
        "grading_checks/grading_correction_response.json",
        "grading_checks/work/grading_correction_response_cache.json",
    }
    allowed_files = required_files | {
        "grading_checks/grading_correction_system.txt",
    }
    allowed_directories = {"grading_checks", "grading_checks/work"}
    files: set[str] = set()
    directories: set[str] = set()
    for path in continuation_dir.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"legacy continuation contains a symlink for {test_id:03d}")
        relative = path.relative_to(continuation_dir).as_posix()
        if path.is_dir():
            directories.add(relative)
        elif path.is_file():
            files.add(relative)
        else:
            raise ValueError(f"legacy continuation contains an unknown entry for {test_id:03d}")
    if not required_files.issubset(files) or not files.issubset(allowed_files):
        raise ValueError(
            f"legacy continuation contains completed, missing, or unknown work for {test_id:03d}"
        )
    if not directories.issubset(allowed_directories):
        raise ValueError(f"legacy continuation contains an unknown directory for {test_id:03d}")

    candidate = Path(str(identity.get("source_candidate") or ""))
    gate = Path(str(identity.get("source_gate") or ""))
    planned_task = identity.get("planned_task")
    if (
        not candidate.is_file()
        or _sha256(candidate) != identity.get("source_candidate_sha256")
        or not gate.is_file()
        or _sha256(gate) != identity.get("source_gate_sha256")
        or not isinstance(planned_task, dict)
        or _json_sha256(planned_task) != identity.get("planned_task_sha256")
    ):
        raise ValueError(f"legacy continuation source evidence changed for {test_id:03d}")
    saved_gate = _authenticated_saved_gate(
        test_id=test_id, candidate=candidate, gate=gate
    )
    correction_request = json.loads(
        (continuation_dir / "grading_checks" / "grading_correction_request.json").read_text()
    )
    if (
        correction_request.get("current_test") != _load_yaml_object(candidate)
        or correction_request.get("executions") != saved_gate["result"]["shots"]
        or _json_sha256(correction_request.get("reviewer_decision"))
        != identity.get("source_failure_sha256")
    ):
        raise ValueError(f"legacy grading request does not match its source for {test_id:03d}")
    if source_review_request is None or not source_review_request.is_file():
        raise ValueError(f"legacy continuation has no source review request for {test_id:03d}")
    review_request = json.loads(source_review_request.read_text())
    if review_request.get("planned_work") != planned_task:
        raise ValueError(f"legacy continuation planned task changed for {test_id:03d}")


def _prepare_continuation_directory(
    *,
    test_id: int,
    continuation_dir: Path,
    identity: dict[str, Any],
    result_path: Path,
    saved_result: dict[str, Any],
    source_review_request: Path | None = None,
) -> None:
    """Create or authenticate one continuation directory before using it."""
    inputs_path = continuation_dir / "continuation_inputs.json"
    if not continuation_dir.exists():
        continuation_dir.mkdir(parents=True, exist_ok=False)
        dump_json(inputs_path, identity)
        return
    if not continuation_dir.is_dir():
        raise ValueError(f"continuation path is not a directory for {test_id:03d}")
    if inputs_path.is_file():
        existing = json.loads(inputs_path.read_text())
        if existing != identity:
            raise ValueError(f"continuation inputs changed for {test_id:03d}")
        return

    if identity.get("correction_operation") != "grading_checks":
        raise ValueError(f"legacy continuation is not a grading correction for {test_id:03d}")
    if continuation_dir.name != identity.get("source_failure_sha256"):
        raise ValueError(f"legacy continuation has the wrong failure hash for {test_id:03d}")
    if (
        _sha256(result_path) != identity.get("source_result_sha256")
        or identity.get("source_result_sha256")
        != identity.get("source_snapshot_sha256")
    ):
        raise ValueError(f"legacy continuation source result changed for {test_id:03d}")
    if isinstance(saved_result.get("continuation"), dict):
        raise ValueError(f"legacy continuation already has a saved result for {test_id:03d}")
    _legacy_grading_validation_exception(
        test_id=test_id,
        result_path=result_path,
        source_result_sha256=str(identity["source_result_sha256"]),
    )
    _authenticate_legacy_grading_partial(
        test_id=test_id,
        continuation_dir=continuation_dir,
        identity=identity,
        source_review_request=source_review_request,
    )
    dump_json(inputs_path, identity)


def _save_continuation_exception(*, test_id: int, result_path: Path, exc: Exception) -> Path:
    """Save a continuation crash without changing the prior unresolved result."""
    if not result_path.is_file():
        raise ValueError(f"selected continuation has no saved repair result for {test_id:03d}")
    source_result_sha256 = _sha256(result_path)
    evidence = {
        "test_id": test_id,
        "source_result": str(result_path.resolve()),
        "source_result_sha256": source_result_sha256,
        "exception_type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
    }
    evidence_path = (
        result_path.parent
        / "continuation_exceptions"
        / f"{_json_sha256(evidence)}.json"
    )
    if evidence_path.is_file():
        if json.loads(evidence_path.read_text()) != evidence:
            raise ValueError(f"continuation exception evidence changed for {test_id:03d}")
        return evidence_path
    dump_json(evidence_path, evidence)
    return evidence_path


def _saved_design_for_continuation(
    *,
    result: dict[str, Any],
    record: dict[str, Any],
    planned_task: PlannedTask,
    context: Any,
    test_id: int,
) -> DesignResponse:
    """Use the design that produced the latest saved candidate when it is recorded."""
    local_failure = _saved_continuation_local_failure(result)
    local_attempt = local_failure.get("source_attempt") if isinstance(local_failure, dict) else None
    if not isinstance(local_attempt, dict):
        local_attempt = result.get("attempt")
    if isinstance(local_attempt, dict) and isinstance(local_attempt.get("design"), dict):
        return DesignResponse.model_validate(local_attempt["design"])
    redesign = result.get("same_facts_redesign")
    if isinstance(redesign, dict):
        latest = redesign.get("correction") or redesign.get("initial")
        if isinstance(latest, dict) and isinstance(latest.get("design"), dict):
            return DesignResponse.model_validate(latest["design"])
    design, _raw_design, _raw_query = _load_saved_authoring_artifacts(
        record=record,
        planned_task=planned_task,
        context=context,
        test_id=test_id,
    )
    return design


def _continue_unresolved_published_test(
    *,
    test_id: int,
    published_path: Path,
    evidence: dict[str, str],
    review_dir: Path,
    out: Path,
    context: Any,
    config: Any,
    grading_corrector: Any,
    regrader: Any,
    certifier: Any,
    reviewer: Any,
    approved_correction: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], PlannedTask | None, Path | None, str | None]:
    """Continue one unresolved repair from its latest explicit reviewer decision."""
    result_path = out / "repair" / f"{test_id:03d}" / "result.json"
    if not result_path.is_file():
        raise ValueError(f"selected continuation has no saved repair result for {test_id:03d}")
    saved = json.loads(result_path.read_text())
    if saved.get("id") != test_id:
        raise ValueError(f"repair result has wrong ID for {test_id:03d}")
    if saved.get("status") == "accepted":
        raise ValueError(f"selected continuation is already accepted for {test_id:03d}")
    if approved_correction is not None:
        if approved_correction["test_id"] != test_id:
            raise ValueError(f"approved correction has wrong ID for {test_id:03d}")
        if _sha256(result_path) != approved_correction["source_result_sha256"]:
            raise ValueError(f"approved correction source result changed for {test_id:03d}")
    local_failure = None if approved_correction is not None else _saved_continuation_local_failure(saved)
    requires_execution_rerun = False
    refresh_old_decision = False
    refreshed_review_evidence: dict[str, Any] | None = None
    found = None if approved_correction is not None else _saved_failure_decision(saved)
    if approved_correction is None and local_failure is None and found is None:
        reason = (
            "the saved repair has no reviewer decision; certification or planning failed before "
            "review, so this continuation will not guess which step to change"
        )
        snapshot = _snapshot_previous_repair_result(result_path=result_path, saved=saved)
        unresolved = {
            "id": test_id,
            "initial_review": saved.get("initial_review"),
            "status": "unresolved_without_reviewer_decision",
            "previous_result": str(snapshot.resolve()),
            "continuation_reason": reason,
            "planned_task": saved.get("planned_task"),
        }
        dump_json(result_path, unresolved)
        return (
            unresolved,
            PlannedTask.model_validate(saved["planned_task"])
            if isinstance(saved.get("planned_task"), dict)
            else None,
            None,
            None,
        )
    if approved_correction is not None:
        stage = "human_approved_correction"
        part_to_correct = str(approved_correction["part_to_correct"])
        failure = {
            "stage": stage,
            "part_to_correct": part_to_correct,
            "specific_problem": str(approved_correction["specific_problem"]),
            "required_correction": str(approved_correction["required_correction"]),
            "approved_change": copy.deepcopy(approved_correction["change"]),
        }
    elif local_failure is not None:
        stage = "continuation_local_validation"
        part_to_correct = str(local_failure["part_to_correct"])
        failure = {
            "stage": "local_validation",
            "part_to_correct": part_to_correct,
            "reason": str(local_failure["reason"]),
        }
        previous_reviewer = saved.get("continuation", {}).get("source_failure") if isinstance(saved.get("continuation"), dict) else None
        if isinstance(previous_reviewer, dict):
            failure["previous_reviewer_decision"] = previous_reviewer
    else:
        assert found is not None
        stage, decision = found
        raw_decision = saved.get(stage)
        refresh_old_decision = (
            isinstance(raw_decision, dict) and "issues" not in raw_decision
        )
        part_to_correct = decision.part_to_correct
        failure = decision.model_dump(mode="json")
        requires_execution_rerun = _requires_execution_rerun(decision)

    routing_details: dict[str, Any] = {}
    work_dir = result_path.parent
    source_candidate: Path | None = None
    source_gate: Path | None = None
    source_review_request: Path | None = None
    if approved_correction is not None:
        source_candidate = Path(approved_correction["source_candidate"])
        source_gate = Path(approved_correction["source_gate"])
        _authenticated_saved_gate(
            test_id=test_id, candidate=source_candidate, gate=source_gate
        )
        routing_details = {
            "approved_correction": copy.deepcopy(approved_correction),
        }
    record, source_task = _planned_task_from_review_evidence(
        test_id=test_id,
        evidence=evidence,
        context=context,
        config=config,
    )
    saved_task = saved.get("planned_task")
    planned_task = PlannedTask.model_validate(saved_task) if isinstance(saved_task, dict) else source_task
    _validate_tasks(context=context, config=config, tasks=[planned_task])

    if refresh_old_decision:
        source_candidate, source_gate, _old_review_request = _latest_saved_candidate_and_gate(
            test_id=test_id,
            result=saved,
            work_dir=work_dir,
            fallback=record,
            require_review_request=False,
        )
        source_result_sha256 = _sha256(result_path)
        refreshed_review_dir = (
            work_dir / "old_decision_review" / source_result_sha256
        )
        refreshed = reviewer(
            test_id=test_id,
            candidate=source_candidate,
            gate=source_gate,
            planned_task=planned_task,
            context=context,
            out=refreshed_review_dir,
            model=config.designer_model,
        )
        refreshed_request = (
            refreshed_review_dir / f"{test_id:03d}" / "request.json"
        )
        if not refreshed_request.is_file():
            raise ValueError(
                f"refreshed reviewer request is missing for {test_id:03d}"
            )
        refreshed_review_evidence = {
            "source_stage": stage,
            "source_decision": copy.deepcopy(saved[stage]),
            "source_result_sha256": source_result_sha256,
            "candidate": str(source_candidate.resolve()),
            "candidate_sha256": _sha256(source_candidate),
            "gate": str(source_gate.resolve()),
            "gate_sha256": _sha256(source_gate),
            "planned_task": planned_task.model_dump(mode="json"),
            "review_request": str(refreshed_request.resolve()),
            "decision": refreshed.model_dump(mode="json"),
        }
        snapshot = _snapshot_previous_repair_result(
            result_path=result_path, saved=saved
        )
        if refreshed.accept:
            accepted = {
                "id": test_id,
                "initial_review": saved.get("initial_review"),
                "action": "accepted_after_old_decision_review",
                "status": "accepted",
                "planned_task": planned_task.model_dump(mode="json"),
                "previous_result": str(snapshot.resolve()),
                "continuation": {
                    "source_result_sha256": _sha256(snapshot),
                    "source_failure_stage": stage,
                    "source_failure": copy.deepcopy(saved[stage]),
                    "source_failure_sha256": _json_sha256(saved[stage]),
                    "changed_step": "none",
                    "candidate": str(source_candidate.resolve()),
                    "gate": str(source_gate.resolve()),
                    "review_request": str(refreshed_request.resolve()),
                    "refreshed_final_review": refreshed_review_evidence,
                },
                "candidate": str(source_candidate.resolve()),
                "accepted_status": "accepted_after_continuation_review",
            }
            dump_json(result_path, accepted)
            return (
                accepted,
                planned_task,
                source_candidate,
                "accepted_after_continuation_review",
            )

        stage = "refreshed_final_review"
        decision = refreshed
        part_to_correct = decision.part_to_correct
        failure = decision.model_dump(mode="json")
        requires_execution_rerun = _requires_execution_rerun(decision)
        source_review_request = refreshed_request

    failure_hash = _json_sha256(failure)
    prior_continuation = saved.get("continuation")
    if (
        isinstance(prior_continuation, dict)
        and prior_continuation.get("source_failure_sha256") == failure_hash
    ):
        raise ValueError(
            f"refusing to repeat the same continuation failure for {test_id:03d}"
        )

    if source_candidate is None:
        source_candidate, source_gate, latest_review_request = _latest_saved_candidate_and_gate(
            test_id=test_id,
            result=saved,
            work_dir=work_dir,
            fallback=record,
            require_review_request=part_to_correct == "grading_checks",
        )
        if source_review_request is None and latest_review_request.is_file():
            source_review_request = latest_review_request
    assert source_candidate is not None and source_gate is not None
    snapshot = _snapshot_previous_repair_result(result_path=result_path, saved=saved)
    continuation_dir = work_dir / "continuations" / failure_hash
    continuation_identity = _continuation_inputs_identity(
        test_id=test_id,
        result_path=result_path,
        snapshot=snapshot,
        failure_hash=failure_hash,
        planned_task=planned_task,
        source_candidate=source_candidate,
        source_gate=source_gate,
        correction_operation=part_to_correct,
    )
    _prepare_continuation_directory(
        test_id=test_id,
        continuation_dir=continuation_dir,
        identity=continuation_identity,
        result_path=result_path,
        saved_result=saved,
        source_review_request=source_review_request,
    )
    needs_model_clients = approved_correction is None or part_to_correct in {
        "starting_app_data",
        "test_design",
        "planning",
    }
    design_client = (
        AzureJsonClient(model=config.designer_model, reasoning_effort="high")
        if needs_model_clients
        else None
    )
    query_client = (
        AzureJsonClient(model=config.query_model, reasoning_effort="high")
        if needs_model_clients
        else None
    )
    action = ""
    candidate: Path | None = None
    gate: Path | None = None
    task_for_review = planned_task
    details: dict[str, Any] = {}
    details.update(routing_details)

    if part_to_correct == "grading_checks":
        assert source_candidate is not None and source_gate is not None
        action = "corrected_grading_checks"
        candidate = continuation_dir / "grading_checks_candidate.yaml"
        if approved_correction is not None:
            _apply_approved_correction(
                source=source_candidate,
                destination=candidate,
                correction=approved_correction,
            )
        else:
            assert source_review_request is not None
            grading_decision = failure.get("previous_reviewer_decision") or failure
            grading_decision_model = FinalTraceDecision.model_validate(grading_decision)
            correction = grading_corrector(
                request=_grade_correction_request(
                    candidate_path=source_candidate,
                    review_request_path=source_review_request,
                    decision=grading_decision_model,
                ),
                out=continuation_dir / "grading_checks",
                client=design_client,
            )
            details["expected_no_history_failed_check_index"] = _corrected_grading_candidate(
                source=source_candidate,
                destination=candidate,
                correction=correction,
                decision=grading_decision_model,
            )
        regrade_path = continuation_dir / "grading_checks_regrade.json"
        regraded = _regraded_gate(
            candidate_path=candidate, saved_gate_path=source_gate, regrader=regrader
        )
        dump_json(regrade_path, regraded)
        regraded_result = regraded["result"]
        if requires_execution_rerun or regraded_result["g1_pass_count"] != 2:
            details["certification_rerun"] = True
            gate = continuation_dir / "grading_checks_gate.json"
            gate_result = _certify_cached(
                persona=config.persona,
                checkpoint=context.checkpoint_path,
                candidate_path=candidate,
                result_path=gate,
                certifier=certifier,
            )
            if not gate_result.get("valid"):
                candidate = None
        elif regraded_result["g2_pass_count"] != 0:
            gate = regrade_path
            candidate = None
            details["certification_rerun"] = False
            details["reason"] = "saved no-history executions pass corrected checks"
        else:
            gate = regrade_path
            details["certification_rerun"] = False
    elif part_to_correct == "execution":
        assert source_candidate is not None
        action = "reran_certification"
        candidate = continuation_dir / "execution_candidate.yaml"
        shutil.copyfile(source_candidate, candidate)
        gate = continuation_dir / "execution_gate.json"
        gate_result = _certify_cached(
            persona=config.persona,
            checkpoint=context.checkpoint_path,
            candidate_path=candidate,
            result_path=gate,
            certifier=certifier,
        )
        details["certification"] = gate_result
        if not gate_result.get("valid"):
            candidate = None
    elif part_to_correct == "user_request":
        action = "rewrote_user_request"
        if approved_correction is not None:
            assert source_candidate is not None
            candidate = continuation_dir / "continuation_user_request_candidate.yaml"
            _apply_approved_correction(
                source=source_candidate,
                destination=candidate,
                correction=approved_correction,
            )
            gate = continuation_dir / "continuation_user_request_gate.json"
            gate_result = _certify_cached(
                persona=config.persona,
                checkpoint=context.checkpoint_path,
                candidate_path=candidate,
                result_path=gate,
                certifier=certifier,
            )
            details["certification"] = gate_result
            if not gate_result.get("valid"):
                candidate = None
        else:
            design = _saved_design_for_continuation(
                result=saved,
                record=record,
                planned_task=planned_task,
                context=context,
                test_id=test_id,
            )
            candidate_data, attempt = _attempt(
                directory=continuation_dir,
                attempt_name="continuation_user_request",
                design_system="",
                design_request={},
                planned_task=planned_task,
                context=context,
                config=config,
                test_id=test_id,
                design_client=design_client,
                query_client=query_client,
                certifier=certifier,
                reuse_design=design,
                query_final_review_feedback={
                    "specific_problem": str(
                        failure.get("specific_problem") or failure.get("reason") or ""
                    ),
                    "required_correction": str(
                        failure.get("required_correction") or failure.get("reason") or ""
                    ),
                },
            )
            details["attempt"] = attempt
            candidate = continuation_dir / "continuation_user_request_candidate.yaml" if candidate_data is not None else None
            gate = continuation_dir / "continuation_user_request_gate.json" if candidate is not None else None
    elif part_to_correct in {"starting_app_data", "test_design"}:
        action = "redesigned_same_facts"
        assert design_client is not None and query_client is not None
        candidate_data, attempt = _redesign_from_same_facts(
            test_id=test_id,
            record=record,
            planned_task=planned_task,
            context=context,
            config=config,
            work_dir=continuation_dir,
            failure=failure,
            design_client=design_client,
            query_client=query_client,
            certifier=certifier,
        )
        details["attempt"] = attempt
        candidate = continuation_dir / "same_facts_redesign_candidate.yaml" if candidate_data is not None else None
        gate = continuation_dir / "same_facts_redesign_gate.json" if candidate is not None else None
    elif part_to_correct == "planning":
        action = "replanned_same_facts"
        task_for_review = _replan_from_same_facts(
            original_task=planned_task,
            context=context,
            config=config,
            work_dir=continuation_dir,
            failure=failure,
        )
        candidate_data, attempt, candidate, gate = _complete_same_facts_design(
            test_id=test_id,
            record=record,
            planned_task=task_for_review,
            context=context,
            config=config,
            work_dir=continuation_dir,
            failure=failure,
            design_client=design_client,
            query_client=query_client,
            certifier=certifier,
        )
        details["attempt"] = attempt
    else:
        raise ValueError(
            f"saved continuation assigned unsupported correction step {part_to_correct!r} "
            f"for {test_id:03d}"
        )

    result: dict[str, Any] = {
        "id": test_id,
        "initial_review": saved.get("initial_review"),
        "issue_owners": [
            str(issue.get("owning_step"))
            for issue in failure.get("issues", [])
            if isinstance(issue, dict) and issue.get("owning_step")
        ],
        "execution_rerun_required": requires_execution_rerun,
        "action": action,
        "status": "unresolved_after_continuation",
        "planned_task": task_for_review.model_dump(mode="json"),
        "previous_result": str(snapshot.resolve()),
        "continuation": {
            "source_result_sha256": _sha256(snapshot),
            "source_failure_stage": stage,
            "source_failure": failure,
            "source_failure_sha256": failure_hash,
            "changed_step": part_to_correct,
            "candidate": str(candidate.resolve()) if candidate is not None else None,
            "gate": str(gate.resolve()) if gate is not None else None,
            "review_request": (
                str(source_review_request.resolve())
                if source_review_request is not None
                else None
            ),
        },
        **details,
    }
    if refreshed_review_evidence is not None:
        result["continuation"]["refreshed_final_review"] = refreshed_review_evidence
    if candidate is None or gate is None or not gate.is_file():
        attempt = details.get("attempt")
        if isinstance(attempt, dict) and attempt.get("stage") == "local_validation":
            result["continuation_failure"] = {
                "kind": "local_validation",
                "part_to_correct": part_to_correct,
                "reason": str(attempt.get("reason") or "local validation failed"),
                "source_attempt": attempt,
            }
        result["continuation_reason"] = details.get(
            "reason", "the selected correction did not produce a valid four-shot certification"
        )
        dump_json(result_path, result)
        return result, task_for_review, None, None

    final_review_dir = continuation_dir / "final_trace_review"
    final = reviewer(
        test_id=test_id,
        candidate=candidate,
        gate=gate,
        planned_task=task_for_review,
        context=context,
        out=final_review_dir,
        model=config.designer_model,
    )
    result["continuation_final_review"] = final.model_dump(mode="json")
    result["continuation"]["review_request"] = str(
        (final_review_dir / f"{test_id:03d}" / "request.json").resolve()
    )
    if final.accept:
        result.update(
            status="accepted",
            candidate=str(candidate.resolve()),
            accepted_status="accepted_after_continuation_review",
        )
        dump_json(result_path, result)
        return result, task_for_review, candidate, "accepted_after_continuation_review"
    dump_json(result_path, result)
    return result, task_for_review, None, None


def _repair_rejected_published_test(
    *,
    test_id: int,
    published_path: Path,
    decision: FinalTraceDecision,
    evidence: dict[str, str],
    review_dir: Path,
    out: Path,
    context: Any,
    config: Any,
    replacement_task: PlannedTask | None,
    grading_corrector: Any,
    regrader: Any,
    certifier: Any,
    reviewer: Any,
    force_same_facts_redesign: bool = False,
) -> tuple[dict[str, Any], PlannedTask | None, Path | None, str | None]:
    """Repair one rejected test using only its own output directory."""
    if decision.accept:
        raise ValueError(f"test {test_id:03d} is accepted and does not need repair")
    result_path = out / "repair" / f"{test_id:03d}" / "result.json"
    previous_failed_result: dict[str, Any] | None = None
    if result_path.is_file():
        saved = json.loads(result_path.read_text())
        if saved.get("id") != test_id:
            raise ValueError(f"repair result has wrong ID for {test_id:03d}")
        saved_task = saved.get("planned_task")
        task = PlannedTask.model_validate(saved_task) if isinstance(saved_task, dict) else None
        candidate: Path | None = None
        status: str | None = None
        candidate_path = saved.get("candidate")
        if saved.get("status") == "accepted" and isinstance(candidate_path, str):
            candidate = Path(candidate_path)
            if not candidate.is_file():
                raise ValueError(f"saved repaired candidate is missing for {test_id:03d}")
            status = str(
                saved.get("accepted_status") or "accepted_after_final_review_correction"
            )
        if candidate is not None or not force_same_facts_redesign:
            return saved, task, candidate, status
        previous_failed_result = saved

    work_dir = result_path.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    if previous_failed_result is not None:
        dump_json(work_dir / "previous_failed_result.json", previous_failed_result)

    record, task = _planned_task_from_review_evidence(
        test_id=test_id,
        evidence=evidence,
        context=context,
        config=config,
    )
    if replacement_task is not None:
        task_for_review = replacement_task
    elif previous_failed_result is not None or decision.part_to_correct in {
        "planning",
        "test_design",
    }:
        task_for_review = _replan_from_same_facts(
            original_task=task,
            context=context,
            config=config,
            work_dir=work_dir,
            failure=previous_failed_result or decision.model_dump(mode="json"),
        )
    else:
        task_for_review = task
    result: dict[str, Any] = {
        "id": test_id,
        "initial_review": decision.model_dump(mode="json"),
        "issue_owners": _review_issue_owners(decision),
        "execution_rerun_required": _requires_execution_rerun(decision),
        "action": "",
        "status": "",
        "planned_task": task_for_review.model_dump(mode="json"),
    }
    if previous_failed_result is not None:
        result["previous_failed_repair"] = str(
            (work_dir / "previous_failed_result.json").resolve()
        )
    candidate: Path | None = None
    gate: Path | None = None
    design_client = AzureJsonClient(model=config.designer_model, reasoning_effort="high")
    query_client = AzureJsonClient(model=config.query_model, reasoning_effort="high")
    used_same_facts_redesign = False

    if previous_failed_result is not None:
        used_same_facts_redesign = True
        result["action"] = "redesigned_from_same_facts"
        candidate_data, attempt, candidate, gate = _complete_same_facts_design(
            test_id=test_id,
            record=record,
            planned_task=task_for_review,
            context=context,
            config=config,
            work_dir=work_dir,
            failure=previous_failed_result,
            design_client=design_client,
            query_client=query_client,
            certifier=certifier,
        )
        result["same_facts_redesign"] = attempt
    elif decision.part_to_correct == "grading_checks":
        result["action"] = "corrected_grading_checks"
        try:
            correction = grading_corrector(
                request=_grade_correction_request(
                    candidate_path=published_path,
                    review_request_path=(
                        review_dir
                        / "final_trace_reviews"
                        / f"{test_id:03d}"
                        / "request.json"
                    ),
                    decision=decision,
                ),
                out=work_dir,
                client=design_client,
            )
            candidate = work_dir / "grading_checks_candidate.yaml"
            result["expected_no_history_failed_check_index"] = _corrected_grading_candidate(
                source=published_path,
                destination=candidate,
                correction=correction,
                decision=decision,
            )
            regraded = _regraded_gate(
                candidate_path=candidate,
                saved_gate_path=record["gate"],
                regrader=regrader,
            )
            regrade_path = work_dir / "grading_checks_regrade.json"
            dump_json(regrade_path, regraded)
            regraded_result = regraded["result"]
            if _requires_execution_rerun(decision) or regraded_result["g1_pass_count"] != 2:
                result["certification_rerun"] = True
                gate = work_dir / "grading_checks_gate.json"
                gate_result = _certify_cached(
                    persona=config.persona,
                    checkpoint=context.checkpoint_path,
                    candidate_path=candidate,
                    result_path=gate,
                    certifier=certifier,
                )
                if not gate_result.get("valid"):
                    candidate = None
            elif regraded_result["g2_pass_count"] != 0:
                result["certification_rerun"] = False
                result["regrade_problem"] = "saved no-history execution passes corrected checks"
                candidate = None
                gate = regrade_path
            else:
                result["certification_rerun"] = False
                gate = regrade_path
        except (
            AttributeError,
            ValidationError,
            AuthoringValidationError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            result["targeted_attempt"] = {
                "stage": "local_validation",
                "reason": f"{type(exc).__name__}: {exc}",
            }
            candidate = None
            gate = None
    elif decision.part_to_correct == "user_request":
        result["action"] = "rewrote_user_request"
        design, _raw_design, _raw_query = _load_saved_authoring_artifacts(
            record=record,
            planned_task=task,
            context=context,
            test_id=test_id,
        )
        candidate_data, attempt = _attempt(
            directory=work_dir,
            attempt_name="user_request",
            design_system="",
            design_request={},
            planned_task=task,
            context=context,
            config=config,
            test_id=test_id,
            design_client=design_client,
            query_client=query_client,
            certifier=certifier,
            reuse_design=design,
            query_final_review_feedback=_final_review_feedback(decision),
        )
        result["attempt"] = attempt
        candidate = (
            work_dir / "user_request_candidate.yaml"
            if candidate_data is not None
            else None
        )
        gate = work_dir / "user_request_gate.json" if candidate is not None else None
    elif decision.part_to_correct == "execution":
        result["action"] = "reran_certification"
        candidate = work_dir / "execution_candidate.yaml"
        candidate.write_text(yaml.safe_dump(load_test(published_path), sort_keys=False, allow_unicode=True))
        gate = work_dir / "execution_gate.json"
        gate_result = _certify_cached(
            persona=config.persona,
            checkpoint=context.checkpoint_path,
            candidate_path=candidate,
            result_path=gate,
            certifier=certifier,
        )
        if not gate_result.get("valid"):
            candidate = None
    elif decision.part_to_correct in {"planning", "test_design"} and replacement_task is None:
        used_same_facts_redesign = True
        result["action"] = "redesigned_from_same_facts"
        candidate_data, attempt, candidate, gate = _complete_same_facts_design(
            test_id=test_id,
            record=record,
            planned_task=task_for_review,
            context=context,
            config=config,
            work_dir=work_dir,
            failure=decision.model_dump(mode="json"),
            design_client=design_client,
            query_client=query_client,
            certifier=certifier,
        )
        result["same_facts_redesign"] = attempt
    else:
        used_same_facts_redesign = True
        result["action"] = "created_from_accepted_plan"
        candidate_data, attempt = _attempt(
            directory=work_dir,
            attempt_name="replacement",
            design_system=DESIGN_SYSTEM,
            design_request=design_payload(
                config=config,
                context=context,
                planned_task=task_for_review,
            ),
            planned_task=task_for_review,
            context=context,
            config=config,
            test_id=test_id,
            design_client=design_client,
            query_client=query_client,
            certifier=certifier,
        )
        result["attempt"] = attempt
        candidate = (
            work_dir / "replacement_candidate.yaml"
            if candidate_data is not None
            else None
        )
        gate = work_dir / "replacement_gate.json" if candidate is not None else None

    accepted_status: str | None = None
    if candidate is not None and gate is not None and gate.is_file():
        final = reviewer(
            test_id=test_id,
            candidate=candidate,
            gate=gate,
            planned_task=task_for_review,
            context=context,
            out=work_dir / "final_trace_review",
            model=config.designer_model,
        )
        result["final_review"] = final.model_dump(mode="json")
        if final.accept:
            accepted_status = "accepted_after_final_review_correction"
            result.update(
                status="accepted",
                candidate=str(candidate.resolve()),
                accepted_status=accepted_status,
            )
            dump_json(result_path, result)
            return result, task_for_review, candidate, accepted_status
        result["targeted_final_review"] = result.pop("final_review")
        candidate = None
        gate = None
    if candidate is None or gate is None or not gate.is_file():
        result["status"] = "unresolved_after_one_correction"
    dump_json(result_path, result)
    return result, task_for_review, candidate, accepted_status


def repair_published_tests(
    *,
    config_path: Path,
    tests_dir: Path,
    review_dir: Path,
    evidence_root: Path,
    out: Path,
    concurrency: int,
    confirm_paid_calls: bool = False,
    replacement_plan: Path | None = None,
    expected_count: int = 200,
    grading_corrector: Any = None,
    regrader: Any = None,
    certifier: Any = None,
    reviewer: Any = None,
    continuation_test_ids: list[int] | None = None,
    approved_corrections_path: Path | None = None,
) -> dict[str, Any]:
    """Repair only tests rejected by a completed published-test review."""
    if not confirm_paid_calls:
        raise ValueError("refusing published-test repair without explicit confirmation")
    if not 1 <= concurrency <= 4:
        raise ValueError("concurrency must be between one and four")
    if not evidence_root.is_dir():
        raise ValueError(f"evidence root does not exist: {evidence_root}")
    config = load_config(config_path)
    context = load_checkpoint_context(config)
    paths_by_id, decisions, evidence_by_id, selected_ids = _reviewed_test_inputs(
        tests_dir=tests_dir, review_dir=review_dir, expected_count=expected_count
    )
    replacement_ids = [
        test_id
        for test_id, decision in sorted(decisions.items())
        if not decision.accept and decision.part_to_correct in {"planning", "test_design"}
    ]
    replacement_tasks: dict[int, PlannedTask] = {}
    if replacement_plan is not None:
        tasks = load_authoring_tasks(context=context, config=config, path=replacement_plan)
        if len(tasks) != len(replacement_ids):
            raise ValueError(
                "replacement plan must contain one accepted task for every planning or test-design replacement"
            )
        replacement_tasks = dict(zip(replacement_ids, tasks, strict=True))

    identity = {
        "checkpoint_identity": context.checkpoint_identity,
        "persona": context.persona,
        "tests_dir": str(tests_dir.resolve()),
        "review_dir": str(review_dir.resolve()),
        "evidence_root": str(evidence_root.resolve()),
        "selected_test_ids": selected_ids,
        "review_manifest_sha256": _sha256(review_dir / "manifest.json"),
        "review_inputs_sha256": _sha256(review_dir / "review_inputs.json"),
        "replacement_plan": str(replacement_plan.resolve()) if replacement_plan else None,
        "replacement_plan_sha256": _sha256(replacement_plan) if replacement_plan else None,
    }
    inputs_path = out / "repair_inputs.json"
    if out.exists() and any(out.iterdir()):
        if not inputs_path.is_file() or json.loads(inputs_path.read_text()) != identity:
            raise ValueError(f"published-test repair output directory belongs to different inputs: {out}")
    out.mkdir(parents=True, exist_ok=True)
    dump_json(inputs_path, identity)

    grading_corrector = grading_corrector or _default_grading_corrector
    regrader = regrader or _default_regrader
    certifier = certifier or certify_candidate
    reviewer = reviewer or _review_one
    tasks_by_id: dict[int, PlannedTask] = {}
    accepted: dict[int, Path] = {}
    accepted_statuses: dict[int, str] = {}
    results_by_id: dict[int, dict[str, Any]] = {}

    for test_id in sorted(
        test_id for test_id in selected_ids if decisions[test_id].accept
    ):
        result_path = out / "repair" / f"{test_id:03d}" / "result.json"
        if result_path.is_file():
            saved = json.loads(result_path.read_text())
            if saved.get("id") != test_id:
                raise ValueError(f"repair result has wrong ID for {test_id:03d}")
            saved_task = saved.get("planned_task")
            if isinstance(saved_task, dict):
                task = PlannedTask.model_validate(saved_task)
            else:
                _task_record, task = _planned_task_from_review_evidence(
                    test_id=test_id,
                    evidence=evidence_by_id[test_id],
                    context=context,
                    config=config,
                )
            tasks_by_id[test_id] = task
            if saved.get("status") != "accepted":
                raise ValueError(f"saved accepted-test result is invalid for {test_id:03d}")
            accepted[test_id] = paths_by_id[test_id]
            accepted_statuses[test_id] = "accepted_initial_final_review"
            results_by_id[test_id] = saved
            continue

        result_path.parent.mkdir(parents=True, exist_ok=True)
        decision = decisions[test_id]
        _record, task = _planned_task_from_review_evidence(
            test_id=test_id,
            evidence=evidence_by_id[test_id],
            context=context,
            config=config,
        )
        tasks_by_id[test_id] = task
        result: dict[str, Any] = {
            "id": test_id,
            "initial_review": decision.model_dump(mode="json"),
            "action": "",
            "status": "",
            "planned_task": task.model_dump(mode="json"),
        }
        result.update(
            action="copied_accepted",
            status="accepted",
            accepted_status="accepted_initial_final_review",
            candidate=str(paths_by_id[test_id].resolve()),
        )
        accepted[test_id] = paths_by_id[test_id]
        accepted_statuses[test_id] = "accepted_initial_final_review"
        dump_json(result_path, result)
        results_by_id[test_id] = result

    rejected_ids = [test_id for test_id in selected_ids if not decisions[test_id].accept]
    continuation_ids = set(continuation_test_ids or [])
    unknown_continuation_ids = sorted(continuation_ids - set(rejected_ids))
    if unknown_continuation_ids:
        raise ValueError(
            "--test-ids may contain only tests rejected by the completed review: "
            f"{unknown_continuation_ids}"
        )
    continuation_mode = continuation_test_ids is not None
    if continuation_mode:
        for test_id in sorted(continuation_ids):
            saved_path = out / "repair" / f"{test_id:03d}" / "result.json"
            if not saved_path.is_file():
                raise ValueError(
                    f"selected continuation has no saved unresolved result for {test_id:03d}"
                )
            saved = json.loads(saved_path.read_text())
            if saved.get("id") != test_id or saved.get("status") == "accepted":
                raise ValueError(
                    f"selected continuation is not an unresolved repair result for {test_id:03d}"
                )
    if approved_corrections_path is not None and not continuation_mode:
        raise ValueError("--approved-corrections requires --test-ids in repair mode")
    approved_corrections = (
        _load_approved_corrections(
            path=approved_corrections_path,
            expected_test_ids=continuation_ids,
            out=out,
        )
        if approved_corrections_path is not None
        else {}
    )

    existing_accepted: dict[int, Path] = {}
    newly_accepted: dict[int, Path] = {}
    if continuation_mode:
        for test_id in rejected_ids:
            saved_path = out / "repair" / f"{test_id:03d}" / "result.json"
            if not saved_path.is_file():
                if test_id in continuation_ids:
                    raise ValueError(
                        f"selected continuation has no saved repair result for {test_id:03d}"
                    )
                continue
            saved = json.loads(saved_path.read_text())
            if saved.get("id") != test_id:
                raise ValueError(f"repair result has wrong ID for {test_id:03d}")
            saved_task = saved.get("planned_task")
            if isinstance(saved_task, dict):
                tasks_by_id[test_id] = PlannedTask.model_validate(saved_task)
            if saved.get("status") == "accepted":
                candidate_value = saved.get("candidate")
                if not isinstance(candidate_value, str) or not Path(candidate_value).is_file():
                    raise ValueError(f"saved repaired candidate is missing for {test_id:03d}")
                existing_accepted[test_id] = Path(candidate_value)
                accepted_statuses[test_id] = str(
                    saved.get("accepted_status")
                    or "accepted_after_final_review_correction"
                )
                results_by_id[test_id] = saved

    continuation_exceptions: list[Path] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {}
        for test_id in rejected_ids:
            if continuation_mode and test_id not in continuation_ids:
                result_path = out / "repair" / f"{test_id:03d}" / "result.json"
                if result_path.is_file():
                    results_by_id[test_id] = json.loads(result_path.read_text())
                continue
            if continuation_mode:
                future = executor.submit(
                    _continue_unresolved_published_test,
                    test_id=test_id,
                    published_path=paths_by_id[test_id],
                    evidence=evidence_by_id[test_id],
                    review_dir=review_dir,
                    out=out,
                    context=context,
                    config=config,
                    grading_corrector=grading_corrector,
                    regrader=regrader,
                    certifier=certifier,
                    reviewer=reviewer,
                    approved_correction=approved_corrections.get(test_id),
                )
            else:
                future = executor.submit(
                    _repair_rejected_published_test,
                    test_id=test_id,
                    published_path=paths_by_id[test_id],
                    decision=decisions[test_id],
                    evidence=evidence_by_id[test_id],
                    review_dir=review_dir,
                    out=out,
                    context=context,
                    config=config,
                    replacement_task=replacement_tasks.get(test_id),
                    grading_corrector=grading_corrector,
                    regrader=regrader,
                    certifier=certifier,
                    reviewer=reviewer,
                )
            futures[future] = test_id
        for future in as_completed(futures):
            test_id = futures[future]
            try:
                result, task, candidate, accepted_status = future.result()
            except Exception as exc:
                if continuation_mode:
                    saved_path = out / "repair" / f"{test_id:03d}" / "result.json"
                    continuation_exceptions.append(
                        _save_continuation_exception(
                            test_id=test_id, result_path=saved_path, exc=exc
                        )
                    )
                    continue
                _record, task = _planned_task_from_review_evidence(
                    test_id=test_id,
                    evidence=evidence_by_id[test_id],
                    context=context,
                    config=config,
                )
                result = {
                    "id": test_id,
                    "initial_review": decisions[test_id].model_dump(mode="json"),
                    "action": "repair_failed_local_validation",
                    "status": "unresolved_after_redesign",
                    "error": f"{type(exc).__name__}: {exc}",
                    "planned_task": task.model_dump(mode="json"),
                }
                dump_json(
                    out / "repair" / f"{test_id:03d}" / "result.json",
                    result,
                )
                candidate = None
                accepted_status = None
            results_by_id[test_id] = result
            if task is not None:
                tasks_by_id[test_id] = task
            if candidate is not None:
                accepted[test_id] = candidate
                accepted_statuses[test_id] = str(accepted_status)
                if continuation_mode:
                    newly_accepted[test_id] = candidate

    if continuation_exceptions:
        paths = ", ".join(str(path.resolve()) for path in continuation_exceptions)
        raise RuntimeError(
            "selected published-test continuations failed locally; saved exception evidence: "
            + paths
        )

    if continuation_mode:
        accepted.update(existing_accepted)

    results = [results_by_id[test_id] for test_id in sorted(results_by_id)]

    repair_manifest_path = out / "repair_manifest.json"
    batch_dirs = write_repair_batches(
        out=out,
        context=context,
        config=config,
        tasks_by_id=tasks_by_id,
        accepted=newly_accepted if continuation_mode else accepted,
        accepted_statuses=accepted_statuses,
        source_manifest=repair_manifest_path,
        preserve_existing=continuation_mode,
    )
    unresolved = []
    for row in results:
        if row["status"] not in {
            "replacement_required",
            "awaiting_replacement",
            "unresolved_after_redesign",
            "unresolved_after_one_correction",
            "unresolved_after_continuation",
            "unresolved_without_reviewer_decision",
        }:
            continue
        test_id = int(row["id"])
        planned_task = row.get("planned_task")
        if not isinstance(planned_task, dict) or not planned_task:
            raise ValueError(f"unresolved test {test_id:03d} has no planned task")
        detail = (
            row.get("same_facts_redesign_final_review")
            or row.get("same_facts_redesign")
            or row.get("final_review")
            or row.get("attempt")
            or row.get("error")
            or row.get("action")
            or row["status"]
        )
        if isinstance(detail, dict):
            failure_reason = next(
                (
                    str(detail[key]).strip()
                    for key in ("specific_problem", "reason", "rejection_reason", "stage")
                    if isinstance(detail.get(key), str) and detail[key].strip()
                ),
                row["status"],
            )
        else:
            failure_reason = str(detail).strip() or row["status"]
        unresolved.append(
            {
                "test_id": test_id,
                "candidate_plan": planned_task,
                "failure_reason": failure_reason,
                "candidate_plan_path": str(
                    (out / "repair" / f"{test_id:03d}" / "result.json").resolve()
                ),
                "failure_manifest_path": str(repair_manifest_path.resolve()),
            }
        )
    dump_json(out / "unresolved_tests.json", {"rejected_tests": unresolved})
    manifest = {
        **identity,
        "test_count": expected_count,
        "selected_test_ids": selected_ids,
        "continuation_test_ids": sorted(continuation_ids) if continuation_mode else None,
        "approved_corrections": (
            str(approved_corrections_path.resolve())
            if approved_corrections_path is not None
            else None
        ),
        "approved_corrections_sha256": (
            _sha256(approved_corrections_path)
            if approved_corrections_path is not None
            else None
        ),
        "accepted": len(accepted),
        "awaiting_replacement": sum(row["status"] == "awaiting_replacement" for row in results),
        "replacement_required": sum(row["status"] == "replacement_required" for row in results),
        "unresolved": len(unresolved),
        "accepted_batches": [str(path.resolve()) for path in batch_dirs],
        "unresolved_tests_path": str((out / "unresolved_tests.json").resolve()),
        "results": sorted(results, key=lambda row: int(row["id"])),
    }
    dump_json(repair_manifest_path, manifest)
    return manifest


def _run_authoring_parts(
    *,
    config_path: Path,
    plan_path: Path,
    out: Path,
    start_id: int | None,
    test_ids: list[int] | None,
    count: int,
    concurrency: int,
) -> list[Path]:
    commands: list[tuple[list[str], Path]] = []
    for index, positions in enumerate(_groups(count, concurrency), start=1):
        run_dir = out / "authoring" / f"part_{index:02d}"
        command = [
            sys.executable,
            "-m",
            "authoring.run",
            "--config",
            str(config_path),
            "--plan",
            str(plan_path),
            "--out",
            str(run_dir),
            "--only",
            ",".join(str(position) for position in positions),
            "--confirm-paid-calls",
        ]
        if test_ids is not None:
            command.extend(["--test-ids", ",".join(str(test_id) for test_id in test_ids)])
        else:
            command.extend(["--start-id", str(start_id)])
        commands.append((command, run_dir))
    processes = [(command, run_dir, subprocess.Popen(command)) for command, run_dir in commands]
    for command, run_dir, process in processes:
        return_code = process.wait()
        if return_code != 0:
            dump_json(run_dir / "worker_error.json", {
                "return_code": return_code, "command": command,
                "reason": "the authoring worker stopped before finishing its selected tests",
            })
    return [run_dir for _command, run_dir in commands]


def _reviewable_outputs(
    *, run_dirs: list[Path], expected_test_ids: list[int]
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    reviewable: dict[int, dict[str, Any]] = {}
    all_results: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {"results": []}
        rows = list(manifest["results"])
        saved_ids = {row["id"] for row in rows}
        for progress_path in sorted((run_dir / "proposals").glob("*/progress.json")):
            progress = json.loads(progress_path.read_text())
            if progress["id"] not in saved_ids:
                if progress["status"] == "local_validation":
                    progress["status"] = "validation_pending"
                rows.append(progress)
                saved_ids.add(progress["id"])
        for result in rows:
            all_results.append(result)
            status = str(result.get("status") or "")
            if status != "review_required" and not status.startswith("certified_"):
                continue
            test_id = int(result["id"])
            attempt = (
                "redesign"
                if status == "certified_redesign"
                else "correction"
                if status == "certified_correction"
                else "initial"
            )
            proposal_dir = run_dir / "proposals" / f"{test_id:03d}"
            candidate_value = result.get("candidate")
            if not isinstance(candidate_value, str):
                raise ValueError(f"reviewable test {test_id} has no candidate path")
            gate_value = result.get("gate")
            gate = (
                _candidate_path(run_dir, gate_value)
                if isinstance(gate_value, str)
                else proposal_dir / f"{attempt}_gate.json"
            )
            corrections_used = result.get("model_corrections_used")
            if corrections_used is None:
                corrections_used = int(attempt != "initial")
            if corrections_used not in (0, 1):
                raise ValueError(f"test {test_id} has an invalid correction count")
            if corrections_used:
                attempt = "correction" if result.get("authoring_mode") == "unified" else "redesign"
            reviewable[test_id] = {
                "candidate": _candidate_path(run_dir, candidate_value),
                "gate": gate,
                "run_dir": run_dir,
                "first_pass": attempt == "initial",
                "certification_valid": status.startswith("certified_"),
                "authoring_mode": str(result.get("authoring_mode") or "legacy"),
                "model_corrections_used": corrections_used,
                "authoring_response": result.get("authoring_response"),
            }
    expected = set(expected_test_ids)
    actual_results = {int(result["id"]) for result in all_results}
    if actual_results - expected or len(actual_results) != len(all_results):
        raise ValueError(
            f"authoring results contain duplicate or unplanned tests: "
            f"expected={sorted(expected)}, actual={sorted(actual_results)}"
        )
    for test_id in sorted(expected - actual_results):
        root = run_dirs[0].parents[1]
        pending = save_progress(
            path=root / "progress" / f"{test_id:03d}.json",
            result={
                "id": test_id, "status": "authoring_pending", "model_corrections_used": 0,
                "reason": "the authoring worker stopped before saving this test",
            },
            inputs_path=root / "run_inputs.json", artifact_paths=[],
        )
        all_results.append(pending)
    return reviewable, sorted(all_results, key=lambda row: int(row["id"]))


def _review_one(
    *,
    test_id: int,
    candidate: Path,
    gate: Path,
    planned_task: Any,
    context: Any,
    out: Path,
    model: str,
) -> FinalTraceDecision:
    request = build_final_trace_review_request(
        candidate_path=candidate,
        certification_gate_path=gate,
        planned_task=planned_task,
        context=context,
    )
    return execute_final_trace_review(
        request=request,
        out=out / f"{test_id:03d}",
        client=AzureJsonClient(model=model, reasoning_effort="high"),
        confirm_paid_calls=True,
    )


def _attempt_name(record: dict[str, Any]) -> str:
    return "initial" if record["first_pass"] else "redesign"


def _load_saved_authoring_artifacts(
    *,
    record: dict[str, Any],
    planned_task: PlannedTask,
    context: Any,
    test_id: int,
) -> tuple[DesignResponse, dict[str, Any], dict[str, Any]]:
    """Load the exact validated design and request that passed the four-shot gate."""
    proposal_dir = record["run_dir"] / "proposals" / f"{test_id:03d}"
    attempt_name = _attempt_name(record)
    raw_design = json.loads((proposal_dir / f"{attempt_name}_design_response.json").read_text())
    if "source_message_meaning" not in raw_design:
        description = planned_task.task_description.strip()
        marker = "\nWork the user wants completed:"
        if description.startswith("New situation:") and marker in description:
            present_situation, requested_work = description.split(marker, 1)
            present_situation = present_situation.removeprefix("New situation:").strip()
            requested_work = requested_work.strip()
        else:
            present_situation = str(raw_design.get("task_description") or description).strip()
            requested_work = present_situation
        raw_design = {
            "outcome": raw_design["outcome"],
            "rejection_reason": str(raw_design.get("skip_reason") or ""),
            "source_message_meaning": str(
                raw_design["what_the_source_messages_establish"]
            ),
            "reason_it_applies_on_test_date": str(
                raw_design["why_this_can_affect_an_action_on_the_test_date"]
            ),
            "present_situation": present_situation,
            "requested_work": requested_work,
            "missing_information": (
                "Earlier messages contain information needed to complete this work."
            ),
            "information_the_request_must_not_reveal": (
                "Do not state or hint at the information from those earlier messages."
            ),
            "existing_records": raw_design.get("existing_records") or [],
            "new_records": raw_design.get("new_records") or [],
            "expected_tool_calls": raw_design.get("expected_tool_calls") or [],
            "grading_checks": raw_design.get("grading_checks") or [],
            "answers_that_must_not_appear": [],
            "with_history_result": str(raw_design.get("with_memory_behavior") or ""),
            "without_history_result": str(
                raw_design.get("without_memory_behavior") or ""
            ),
            "without_memory_failed_check": raw_design["without_memory_failed_check"],
        }
    design = _parse_design(
        context=context,
        planned_task=planned_task,
        response=raw_design,
    )
    raw_query = json.loads(
        (proposal_dir / f"{attempt_name}_blind_query_response.json").read_text()
    )
    BlindQueryResponse.model_validate(
        {key: value for key, value in raw_query.items() if not key.startswith("_")}
    )
    return design, raw_design, raw_query


def _final_review_feedback(decision: FinalTraceDecision) -> dict[str, str]:
    return {
        "specific_problem": decision.specific_problem,
        "required_correction": decision.required_correction,
    }


def _requires_execution_rerun(decision: FinalTraceDecision) -> bool:
    """Return whether the reviewer found an execution problem as well as another problem."""
    return any(issue.owning_step == "execution" for issue in decision.issues)


def _review_issue_owners(decision: FinalTraceDecision) -> list[str]:
    """Return all reviewer owners in saved order, including mixed failures."""
    return list(dict.fromkeys(issue.owning_step for issue in decision.issues))


def _complete_test_final_review_correction(
    *, decision: FinalTraceDecision, record: dict[str, Any], planned_task: ApprovedIdea,
    context: Any, config: Any, test_id: int, review_dir: Path, client: Any,
    certifier: Any, regrader: Any = _default_regrader,
) -> tuple[Path | None, Path | None, dict[str, Any]]:
    from authoring.complete_test import correction_scope, parse_writer_response
    from authoring.review import ReviewResponse

    correction = {"initial_final_review": decision.model_dump(mode="json"),
                  "part_to_correct": decision.part_to_correct, "status": "replacement_required"}
    if decision.part_to_correct == "planning":
        correction["reason"] = "the approved work needs a newly approved idea"
        dump_json(review_dir / "correction.json", correction)
        return None, None, correction
    proposal = record["run_dir"] / "proposals" / f"{test_id:03d}"
    if decision.part_to_correct == "execution":
        candidate = proposal / "final_review_execution_rerun_candidate.yaml"
        shutil.copyfile(record["candidate"], candidate)
        saved_gate = json.loads(Path(record["gate"]).read_text())
        gate = proposal / "final_review_execution_rerun_gate.json"
        result = _certify_cached(
            persona=config.persona, checkpoint=context.checkpoint_path, candidate_path=candidate,
            result_path=gate, certifier=certifier, authoring_evidence=saved_gate["authoring_evidence"],
            oracle_input=saved_gate["oracle_input"],
        )
        correction.update(action="reran_the_same_candidate", certification=result,
                          status="candidate_certified" if result.get("valid") else "replacement_required")
        if not result.get("valid"):
            correction.update(failed_candidate=str(candidate), failed_gate=str(gate))
        dump_json(review_dir / "correction.json", correction)
        return (candidate if result.get("valid") else None), gate, correction

    response_path = Path(record["authoring_response"]) if record.get("authoring_response") else (
        proposal / (Path(record["gate"]).name.removesuffix("_gate.json") + "_authoring_response.json")
    )
    previous = json.loads(response_path.read_text())
    review = ReviewResponse.model_validate(
        json.loads((review_dir / "review.json").read_text())
    )
    scope = correction_scope([row.model_dump(mode="json") for row in review.issues], parse_writer_response(previous))
    if not scope["allowed_correction_fields"]:
        correction.update(status="correction_pending", reason="review grants no writable test fields",
                          model_correction_started=False)
        dump_json(review_dir / "correction.json", correction)
        return None, None, correction
    payload = {
        "original_authoring_input": design_payload(config=config, context=context, planned_task=planned_task),
        "previous_authoring": previous, **scope,
        "failure_to_fix": review.model_dump(mode="json"),
    }
    checks_only = all(row.part == "checks" for row in review.issues)
    prior_gate = None
    if checks_only:
        prior_gate = proposal / "final_review_correction_prior_gate.json"
        if prior_gate.is_file() and _sha256(prior_gate) != _sha256(Path(record["gate"])):
            raise ValueError("a stopped grading correction refers to different saved executions")
        shutil.copyfile(record["gate"], prior_gate)
        payload["saved_execution_gate"] = {"file": prior_gate.name, "sha256": _sha256(prior_gate)}
    candidate_data, attempt = _author_new_test_attempt(
        directory=proposal, attempt_name="final_review_correction", system="",
        payload=payload, planned_task=planned_task, context=context, config=config, test_id=test_id,
        client=client, certifier=certifier, model_corrections_used=1,
        regrade_saved_gate=prior_gate, regrader=regrader,
    )
    candidate = proposal / "final_review_correction_candidate.yaml"
    gate = proposal / "final_review_correction_gate.json"
    correction.update(
        action="corrected_only_the_assigned_fields", attempt=attempt,
        status="candidate_certified" if candidate_data is not None else (
            attempt["stage"] if attempt["stage"].endswith("_pending") else "replacement_required"
        ),
    )
    if candidate_data is None and attempt.get("stage") == "certification":
        correction.update(failed_candidate=str(candidate), failed_gate=str(gate))
    dump_json(review_dir / "correction.json", correction)
    return (candidate if candidate_data is not None else None), (gate if gate.is_file() else None), correction


def _unified_final_review_correction(
    *,
    decision: FinalTraceDecision,
    record: dict[str, Any],
    planned_task: PlannedTask,
    context: Any,
    config: Any,
    test_id: int,
    review_dir: Path,
    client: AzureJsonClient,
    certifier: Any,
    regrader: Any = _default_regrader,
) -> tuple[Path | None, Path | None, dict[str, Any]]:
    """Use one complete re-authoring call for one rejected new-test candidate."""
    if isinstance(planned_task, ApprovedIdea):
        return _complete_test_final_review_correction(
            decision=decision, record=record, planned_task=planned_task, context=context,
            config=config, test_id=test_id, review_dir=review_dir, client=client,
            certifier=certifier, regrader=regrader,
        )
    correction: dict[str, Any] = {
        "initial_final_review": decision.model_dump(mode="json"),
        "part_to_correct": decision.part_to_correct,
        "status": "replacement_required",
    }
    if decision.part_to_correct == "planning":
        correction["reason"] = "the accepted idea needs a new human-approved replacement"
        dump_json(review_dir / "correction.json", correction)
        return None, None, correction
    proposal_dir = record["run_dir"] / "proposals" / f"{test_id:03d}"
    if decision.part_to_correct == "execution":
        candidate = proposal_dir / "final_review_execution_rerun_candidate.yaml"
        shutil.copyfile(record["candidate"], candidate)
        gate = proposal_dir / "final_review_execution_rerun_gate.json"
        result = _certify_cached(
            persona=config.persona,
            checkpoint=context.checkpoint_path,
            candidate_path=candidate,
            result_path=gate,
            certifier=certifier,
        )
        correction.update(
            action="reran_the_same_candidate",
            certification=result,
            status="candidate_certified" if result.get("valid") else "replacement_required",
        )
        dump_json(review_dir / "correction.json", correction)
        return (candidate if result.get("valid") else None), gate, correction

    response_path = Path(record["authoring_response"]) if record.get("authoring_response") else (
        proposal_dir / (Path(record["gate"]).name.removesuffix("_gate.json") + "_authoring_response.json")
    )
    if not response_path.is_file():
        raise ValueError("a scoped correction needs the saved complete authoring response")
    previous_authoring = json.loads(response_path.read_text())
    payload = {
        "original_authoring_input": design_payload(
            config=config, context=context, planned_task=planned_task
        ),
        "previous_authoring": previous_authoring,
        "allowed_correction_fields": _correction_fields_for_issues(
            [issue.model_dump(mode="json") for issue in decision.issues]
        ),
        "failure_to_fix": decision.model_dump(mode="json"),
    }
    candidate_data, attempt = _author_new_test_attempt(
        directory=proposal_dir,
        attempt_name="final_review_correction",
        system=AUTHOR_TEST_SYSTEM + "\n\n" + REAUTHOR_TEST_SYSTEM,
        payload=payload,
        planned_task=planned_task,
        context=context,
        config=config,
        test_id=test_id,
        client=client,
        certifier=certifier,
    )
    candidate = (
        proposal_dir / "final_review_correction_candidate.yaml"
        if candidate_data is not None
        else None
    )
    gate = (
        proposal_dir / "final_review_correction_gate.json"
        if candidate_data is not None
        else None
    )
    correction.update(
        action="corrected_only_the_assigned_fields",
        attempt=attempt,
        status="candidate_certified" if candidate is not None else (
            attempt["stage"] if attempt["stage"].endswith("_pending") else "replacement_required"
        ),
    )
    if candidate is None and attempt.get("stage") == "certification":
        correction["failed_candidate"] = str(proposal_dir / "final_review_correction_candidate.yaml")
        correction["failed_gate"] = str(proposal_dir / "final_review_correction_gate.json")
    dump_json(review_dir / "correction.json", correction)
    return candidate, gate, correction


def _correct_after_final_review(
    *,
    decision: FinalTraceDecision,
    record: dict[str, Any],
    planned_task: PlannedTask,
    context: Any,
    config: Any,
    test_id: int,
    review_dir: Path,
    design_client: AzureJsonClient,
    query_client: AzureJsonClient,
    certifier: Any,
    grading_corrector: Any,
    regrader: Any,
) -> tuple[Path | None, Path | None, dict[str, Any]]:
    """Run the one permitted correction after a rejected final trace review."""
    feedback = _final_review_feedback(decision)
    correction = {
        "initial_final_review": decision.model_dump(mode="json"),
        "part_to_correct": decision.part_to_correct,
        "specific_problem": decision.specific_problem,
        "required_correction": decision.required_correction,
        "issue_owners": _review_issue_owners(decision),
        "execution_rerun_required": _requires_execution_rerun(decision),
    }
    if decision.part_to_correct == "planning":
        correction["status"] = "replacement_required"
        correction["reason"] = "final review assigned the failure to a step that cannot be corrected here"
        dump_json(review_dir / "correction.json", correction)
        return None, None, correction

    candidate: Path | None = None
    gate_path: Path | None = None
    try:
        proposal_dir = record["run_dir"] / "proposals" / f"{test_id:03d}"
        candidate_path = proposal_dir / "final_review_correction_candidate.yaml"
        gate_path = proposal_dir / "final_review_correction_gate.json"
        if decision.part_to_correct == "grading_checks":
            correction_data = grading_corrector(
                request=_grade_correction_request(
                    candidate_path=record["candidate"],
                    review_request_path=review_dir / "request.json",
                    decision=decision,
                ),
                out=review_dir / "grading_checks",
                client=design_client,
            )
            correction["expected_no_history_failed_check_index"] = _corrected_grading_candidate(
                source=record["candidate"],
                destination=candidate_path,
                correction=correction_data,
                decision=decision,
            )
            regraded = _regraded_gate(
                candidate_path=candidate_path,
                saved_gate_path=record["gate"],
                regrader=regrader,
            )
            regrade_path = proposal_dir / "final_review_correction_regrade.json"
            dump_json(regrade_path, regraded)
            if _requires_execution_rerun(decision):
                correction["certification_rerun"] = True
                gate_result = _certify_cached(
                    persona=config.persona,
                    checkpoint=context.checkpoint_path,
                    candidate_path=candidate_path,
                    result_path=gate_path,
                    certifier=certifier,
                )
                candidate = candidate_path if gate_result.get("valid") else None
            elif regraded["result"]["g1_pass_count"] == 2 and regraded["result"]["g2_pass_count"] == 0:
                correction["certification_rerun"] = False
                candidate = candidate_path
                gate_path = regrade_path
            else:
                correction["certification_rerun"] = False
                correction["reason"] = "the saved executions do not pass the corrected four-run gate"
                candidate = None
                gate_path = regrade_path
            correction["status"] = "candidate_certified" if candidate is not None else "replacement_required"
        elif decision.part_to_correct == "execution":
            shutil.copyfile(record["candidate"], candidate_path)
            gate_result = _certify_cached(
                persona=config.persona,
                checkpoint=context.checkpoint_path,
                candidate_path=candidate_path,
                result_path=gate_path,
                certifier=certifier,
            )
            correction["certification"] = gate_result
            candidate = candidate_path if gate_result.get("valid") else None
            correction["status"] = "candidate_certified" if candidate is not None else "replacement_required"
        elif decision.part_to_correct == "user_request":
            design, previous_design, previous_query = _load_saved_authoring_artifacts(
                record=record,
                planned_task=planned_task,
                context=context,
                test_id=test_id,
            )
            generated_candidate, attempt = _attempt(
                directory=proposal_dir,
                attempt_name="final_review_correction",
                design_system="",
                design_request={},
                planned_task=planned_task,
                context=context,
                config=config,
                test_id=test_id,
                design_client=design_client,
                query_client=query_client,
                certifier=certifier,
                reuse_design=design,
                query_final_review_feedback=feedback,
            )
            candidate = candidate_path if generated_candidate is not None else None
        else:
            design, previous_design, previous_query = _load_saved_authoring_artifacts(
                record=record,
                planned_task=planned_task,
                context=context,
                test_id=test_id,
            )
            redesign_request = {
                "original_input": design_payload(
                    config=config, context=context, planned_task=planned_task
                ),
                "previous_design": previous_design,
                "previous_query": previous_query,
                "final_review_feedback": {
                    "part_to_correct": decision.part_to_correct,
                    **feedback,
                },
            }
            generated_candidate, attempt = _attempt(
                directory=proposal_dir,
                attempt_name="final_review_correction",
                design_system=(
                    DESIGN_SYSTEM + "\n\n" + REDESIGN_SYSTEM
                ),
                design_request=redesign_request,
                planned_task=planned_task,
                context=context,
                config=config,
                test_id=test_id,
                design_client=design_client,
                query_client=query_client,
                certifier=certifier,
            )
            candidate = candidate_path if generated_candidate is not None else None
        if decision.part_to_correct in {"user_request", "test_design"}:
            correction["attempt"] = attempt
            correction["status"] = "candidate_certified" if candidate is not None else "replacement_required"
        correction["candidate"] = str(candidate_path.resolve()) if candidate is not None else None
    except (ValueError, KeyError, TypeError, OSError) as exc:
        candidate = None
        correction["status"] = "correction_pending" if record.get("authoring_mode") == "unified" else "replacement_required"
        correction["attempt"] = {
            "stage": "local_validation",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    evidence_path = gate_path if candidate is not None else None
    if candidate is None and gate_path is not None and gate_path.is_file() and candidate_path.is_file():
        try:
            _authenticated_saved_gate(test_id=test_id, candidate=candidate_path, gate=gate_path)
        except (ValueError, TypeError, KeyError):
            pass
        else:
            correction["failed_candidate"] = str(candidate_path)
            correction["failed_gate"] = str(gate_path)
    correction["certification_evidence"] = (
        str(evidence_path.resolve()) if evidence_path is not None else None
    )
    dump_json(review_dir / "correction.json", correction)
    return candidate, evidence_path, correction


def _same_facts_redesign_after_final_review(
    *,
    test_id: int,
    record: dict[str, Any],
    planned_task: PlannedTask,
    context: Any,
    config: Any,
    review_dir: Path,
    failure: dict[str, Any],
    trigger: str,
    design_client: AzureJsonClient,
    query_client: AzureJsonClient,
    certifier: Any,
    reviewer: Any,
) -> tuple[Path | None, dict[str, Any]]:
    """Make the one permitted complete redesign after final-review recovery fails."""
    redesign_dir = review_dir / "same_facts_redesign"
    candidate_data, attempt = _redesign_from_same_facts(
        test_id=test_id,
        record=record,
        planned_task=planned_task,
        context=context,
        config=config,
        work_dir=redesign_dir,
        failure=failure,
        design_client=design_client,
        query_client=query_client,
        certifier=certifier,
    )
    evidence: dict[str, Any] = {
        "trigger": trigger,
        "failure": copy.deepcopy(failure),
        "attempt": attempt,
        "candidate": None,
        "gate": None,
        "status": "unresolved_after_same_facts_redesign",
    }
    if candidate_data is None:
        dump_json(redesign_dir / "recovery.json", evidence)
        return None, evidence

    candidate = redesign_dir / "same_facts_redesign_candidate.yaml"
    gate = redesign_dir / "same_facts_redesign_gate.json"
    evidence["candidate"] = str(candidate.resolve())
    evidence["gate"] = str(gate.resolve())
    final = reviewer(
        test_id=test_id,
        candidate=candidate,
        gate=gate,
        planned_task=planned_task,
        context=context,
        out=review_dir / "same_facts_redesign_final_review",
        model=config.designer_model,
    )
    evidence["final_review"] = final.model_dump(mode="json")
    if final.accept:
        evidence["status"] = "accepted_after_same_facts_redesign"
        dump_json(redesign_dir / "recovery.json", evidence)
        return candidate, evidence
    dump_json(redesign_dir / "recovery.json", evidence)
    return None, evidence


def _save_review_progress(
    *, path: Path, result: dict[str, Any], record: dict[str, Any]
) -> dict[str, Any]:
    files = [file for file in path.parent.rglob("*.json")
             if file not in {path.parent / "result.json", path.parent / "progress.json"}]
    for row in (record, result):
        files.extend(Path(row[key]) for key in ("candidate", "gate", "authoring_response") if row.get(key))
    run_dir = record.get("run_dir")
    inputs_path = Path(run_dir) / "run_inputs.json" if run_dir is not None else path.parent / "no_authoring_inputs"
    corrected_response = (Path(run_dir) / "proposals" / f"{result['id']:03d}" /
                          "final_review_correction_authoring_response.json") if run_dir is not None else None
    if corrected_response is not None and corrected_response.is_file():
        result["authoring_response"] = str(corrected_response)
        files.extend(authoring_artifacts(corrected_response.parent, "final_review_correction"))
    elif record.get("authoring_response"):
        result["authoring_response"] = str(record["authoring_response"])
    if run_dir is not None:
        files.append(Path(run_dir) / "proposals" / f"{result['id']:03d}" / "reserved_execution_retry.json")
    return save_progress(path=path, result=result, inputs_path=inputs_path, artifact_paths=files)


def _clean_certification_evidence(
    *,
    candidate: Path,
    gate: Path,
    planned_task: PlannedTask,
    preflight_gate: Path | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Prove that a version-4 test needs no post-execution model review."""
    evidence: dict[str, Any] = {
        "policy_version": 1,
        "outcome": "final_review_required",
        "reasons": [],
    }
    reasons: list[str] = evidence["reasons"]
    if not isinstance(planned_task, ApprovedIdea):
        reasons.append("only new version-4 tests may skip final model review")
        return False, evidence
    try:
        spec = TestSpec.model_validate(yaml.safe_load(candidate.read_text()) or {})
        assertions = copy.deepcopy(spec.grade.config.get("assertions") or [])
        assertion_ids = [str(row["check_id"]) for row in assertions]
        if not assertion_ids or len(assertion_ids) != len(set(assertion_ids)):
            reasons.append("candidate grading checks are missing or duplicated")

        saved_gate = json.loads(gate.read_text())
        candidate_sha256 = _sha256(candidate)
        evidence["candidate_sha256"] = candidate_sha256
        if saved_gate.get("candidate_sha256") != candidate_sha256:
            reasons.append("certification does not match the candidate hash")

        result = saved_gate.get("result")
        if not isinstance(result, dict):
            reasons.append("certification result is missing")
            return False, evidence
        if (
            result.get("valid") is not True
            or result.get("verdict") != "valid"
            or result.get("g1_pass_count") != 2
            or result.get("g2_pass_count") != 0
        ):
            reasons.append("the four-run result is not two history passes and two no-memory failures")

        authoring_evidence = saved_gate.get("authoring_evidence")
        if not isinstance(authoring_evidence, dict):
            reasons.append("certification is missing version-4 authoring evidence")
            return False, evidence
        quality_audit = authoring_evidence.get("quality_audit")
        audit_checks = quality_audit.get("checks") if isinstance(quality_audit, dict) else None
        if not isinstance(audit_checks, list):
            reasons.append("writer quality audit is missing its check roles")
            return False, evidence
        remembered_check_ids = {
            str(row.get("check_id"))
            for row in audit_checks
            if isinstance(row, dict)
            and isinstance(row.get("roles"), list)
            and "remembered_result" in row["roles"]
        }
        evidence["remembered_check_ids"] = sorted(remembered_check_ids)
        if not remembered_check_ids:
            reasons.append("no grading check is tied to a remembered result")
        if not remembered_check_ids.issubset(set(assertion_ids)):
            reasons.append("writer audit names an unknown remembered-result check")
        saved_checks = authoring_evidence.get("checks")
        if (
            not isinstance(saved_checks, list)
            or [row.get("assertion") for row in saved_checks if isinstance(row, dict)]
            != assertions
        ):
            reasons.append("writer evidence does not match the candidate checks")

        preflight_source = preflight_gate or gate
        attempt_name = preflight_source.stem.removesuffix("_gate")
        preflight_dir = preflight_source.parent / f"{attempt_name}_preflight"
        decision_path = preflight_dir / "decision.json"
        request_path = preflight_dir / "request.json"
        result_path = preflight_dir / "result.json"
        examples_path = preflight_dir / "grading_examples" / "result.json"
        preflight_request: dict[str, Any] | None = None
        if not all(
            path.is_file()
            for path in (decision_path, request_path, result_path, examples_path)
        ):
            reasons.append("approved preflight or grading examples are missing")
        else:
            decision = json.loads(decision_path.read_text())
            preflight_request = json.loads(request_path.read_text())
            preflight_result = json.loads(result_path.read_text())
            examples = json.loads(examples_path.read_text())
            if decision.get("accept") is not True:
                reasons.append("preflight did not approve the candidate")
            if (
                preflight_result.get("candidate_sha256") != candidate_sha256
                or preflight_result.get("decision") != decision
                or preflight_result.get("passed") is not True
            ):
                reasons.append("preflight result does not authenticate this candidate and decision")
            if (
                preflight_request.get("test_id") != int(spec.id)
                or decision.get("test_id") != int(spec.id)
                or preflight_request.get("planned_work")
                != planned_task.model_dump(mode="json")
                or preflight_request.get("user_request") != spec.test
                or preflight_request.get("starting_app_data") != spec.mock_state
                or preflight_request.get("expected_tool_calls")
                != spec.expected_tool_calls
                or preflight_request.get("authoring_evidence") != authoring_evidence
            ):
                reasons.append("preflight request does not match the candidate, plan, or writer evidence")
            if preflight_request.get("grading_checks") != assertions:
                reasons.append("preflight checks do not match the candidate")
            if preflight_request.get("semantic_judge_version", 1) != spec.grade.config.get("semantic_judge_version", 1):
                reasons.append("preflight semantic judging differs from the candidate")
            if examples.get("passed") is not True:
                reasons.append("preflight grading examples did not both behave correctly")

        shots = result.get("shots")
        if not isinstance(shots, list) or len(shots) != 4:
            reasons.append("certification does not contain exactly four executions")
            return False, evidence
        history_shots = [shot for shot in shots if isinstance(shot, dict) and shot.get("with_memory") is True]
        no_memory_shots = [shot for shot in shots if isinstance(shot, dict) and shot.get("with_memory") is False]
        if len(history_shots) != 2 or len(no_memory_shots) != 2:
            reasons.append("certification does not contain two executions of each kind")
        attempt_ids = [shot.get("attempt_id") for shot in shots if isinstance(shot, dict)]
        if (
            len(attempt_ids) != 4
            or any(not isinstance(value, str) or not value for value in attempt_ids)
            or len(set(attempt_ids)) != 4
        ):
            reasons.append("certification execution IDs are missing or duplicated")
        if preflight_request is not None:
            from authoring.review import validate_certification

            try:
                validate_certification(
                    {
                        **preflight_request,
                        "certification_result": {
                            "valid": result.get("valid"),
                            "with_history_pass_count": result.get("g1_pass_count"),
                            "without_history_pass_count": result.get("g2_pass_count"),
                        },
                        "executions": shots,
                        "agent_inputs": result.get("agent_inputs") or {},
                    }
                )
            except ValueError as exc:
                reasons.append(f"certification record validation failed: {exc}")

        failed_remembered_by_attempt: list[list[str]] = []
        for shot in shots:
            if not isinstance(shot, dict):
                reasons.append("certification contains a malformed execution")
                continue
            if shot.get("error") or shot.get("oracle_errors"):
                reasons.append(
                    f"execution {shot.get('attempt_id', '?')} contains an error"
                )
            details = (shot.get("grade") or {}).get("details")
            if not isinstance(details, list):
                reasons.append(
                    f"execution {shot.get('attempt_id', '?')} has no complete check results"
                )
                continue
            detail_by_id = {
                str(detail.get("name")): detail
                for detail in details
                if isinstance(detail, dict)
            }
            if (
                len(details) != len(assertion_ids)
                or len(detail_by_id) != len(details)
                or set(detail_by_id) != set(assertion_ids)
            ):
                reasons.append(
                    f"execution {shot.get('attempt_id', '?')} does not grade every check exactly once"
                )
                continue
            if shot.get("with_memory") is True:
                if shot.get("passed") is not True or any(
                    detail.get("ok") is not True for detail in detail_by_id.values()
                ):
                    reasons.append(
                        f"history execution {shot.get('attempt_id', '?')} did not pass every check"
                    )
            else:
                failed_remembered = sorted(
                    check_id
                    for check_id in remembered_check_ids
                    if detail_by_id[check_id].get("ok") is False
                )
                failed_remembered_by_attempt.append(failed_remembered)
                if shot.get("passed") is not False or not failed_remembered:
                    reasons.append(
                        f"no-memory execution {shot.get('attempt_id', '?')} did not fail a remembered-result check"
                    )
        evidence["no_memory_failed_remembered_check_ids"] = failed_remembered_by_attempt
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, yaml.YAMLError) as exc:
        reasons.append(f"clean-result validation failed: {type(exc).__name__}: {exc}")

    if reasons:
        return False, evidence
    evidence["outcome"] = "accepted_without_final_model_review"
    return True, evidence


def _review_four_run_candidates(
    *,
    reviewable: dict[int, dict[str, Any]],
    tasks_by_id: dict[int, PlannedTask],
    context: Any,
    config: Any,
    out: Path,
    concurrency: int,
    design_client: AzureJsonClient,
    query_client: AzureJsonClient,
    certifier: Any,
    grading_corrector: Any = _default_grading_corrector,
    regrader: Any = _default_regrader,
    reviewer: Any = _review_one,
) -> tuple[dict[int, Path], list[dict[str, Any]]]:
    """Finish and save each test independently; report process errors as pending."""
    def finish_one(test_id: int) -> tuple[Path | None, dict[str, Any]]:
        record = reviewable[test_id]
        review_dir = out / "final_trace_reviews" / f"{test_id:03d}"
        try:
            candidate, result = _review_four_run_candidate(
                test_id=test_id, record=record, planned_task=tasks_by_id[test_id],
                context=context, config=config, out=out,
                design_client=design_client, query_client=query_client,
                certifier=certifier, grading_corrector=grading_corrector,
                regrader=regrader, reviewer=reviewer,
            )
        except Exception as exc:
            # A malformed report or transport failure is not evidence against
            # the approved idea. Preserve the latest completed step for resume.
            progress_path = review_dir / "progress.json"
            progress = json.loads(progress_path.read_text()) if progress_path.is_file() else {}
            candidate = None
            result = {
                **progress,
                "id": test_id,
                "model_corrections_used": progress.get(
                    "model_corrections_used", record.get("model_corrections_used", int(not record["first_pass"]))
                ),
                "execution_reruns_used": progress.get("execution_reruns_used", record.get("execution_reruns_used", 0)),
                "status": "correction_pending" if progress.get("status") == "correction_pending" else "review_pending",
                "reason": f"{type(exc).__name__}: {exc}",
                "candidate": progress.get("candidate", str(record["candidate"])),
                "gate": progress.get("gate", str(record["gate"])),
            }
        result = _save_review_progress(path=review_dir / "result.json", result=result, record=record)
        return candidate, result

    accepted: dict[int, Path] = {}
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(finish_one, test_id): test_id for test_id in reviewable
        }
        for future in as_completed(futures):
            candidate, result = future.result()
            results.append(result)
            if candidate is not None:
                accepted[futures[future]] = candidate
    return accepted, sorted(results, key=lambda row: row["id"])


def _review_four_run_candidate(
    *, test_id: int, record: dict[str, Any], planned_task: PlannedTask,
    context: Any, config: Any, out: Path, design_client: AzureJsonClient,
    query_client: AzureJsonClient, certifier: Any, grading_corrector: Any,
    regrader: Any, reviewer: Any,
) -> tuple[Path | None, dict[str, Any]]:
    """Accept clean evidence locally; review only exceptions and corrections."""
    review_dir = out / "final_trace_reviews" / f"{test_id:03d}"
    clean, clean_evidence = _clean_certification_evidence(
        candidate=record["candidate"],
        gate=record["gate"],
        planned_task=planned_task,
    )
    if clean:
        return record["candidate"], {
            "id": test_id,
            "status": "accepted_clean_certification",
            "final_model_review": "not_required",
            "clean_certification": clean_evidence,
            "candidate": str(record["candidate"]),
            "gate": str(record["gate"]),
            "candidate_sha256": _sha256(record["candidate"]),
            "model_corrections_used": record.get(
                "model_corrections_used", int(not record["first_pass"])
            ),
            "execution_reruns_used": record.get("execution_reruns_used", 0),
        }
    initial = reviewer(
        test_id=test_id, candidate=record["candidate"], gate=record["gate"],
        planned_task=planned_task, context=context,
        out=out / "final_trace_reviews", model=config.designer_model,
    )
    result: dict[str, Any] = {
        "id": test_id,
        "clean_certification": clean_evidence,
        "final_model_review": "required",
        "initial_final_review": initial.model_dump(mode="json"),
        "candidate": str(record["candidate"]),
        "gate": str(record["gate"]),
        "model_corrections_used": record.get("model_corrections_used", int(not record["first_pass"])),
        "execution_reruns_used": record.get("execution_reruns_used", 0),
    }
    _save_review_progress(path=review_dir / "progress.json", result=result, record=record)
    if initial.accept:
        if record.get("certification_valid") is False:
            raise ValueError(
                f"reviewer accepted test {test_id} even though its four-run certification failed"
            )
        result["status"] = "accepted_initial_final_review"
        result["candidate_sha256"] = _sha256(record["candidate"])
        return record["candidate"], result

    if (
        (record.get("test_overrides") or {}).get("exact_repair")
        and initial.part_to_correct != "execution"
    ):
        result["status"] = "repair_pending"
        return None, result
    if initial.part_to_correct == "planning":
        result["status"] = "replacement_required"
        return None, result
    if result["model_corrections_used"] and initial.part_to_correct != "execution":
        result["status"] = "replacement_required_after_one_correction"
        return None, result

    result.update(status="correction_pending", pending_operation=initial.part_to_correct)
    needs_execution = initial.part_to_correct == "execution" or any(issue.owning_step == "execution" for issue in initial.issues)
    if needs_execution:
        if record.get("execution_reruns_used", 0):
            result["status"] = "replacement_required_after_one_correction"
            return None, result
    if initial.part_to_correct != "execution":
        result["model_corrections_used"] = 1
    _save_review_progress(path=review_dir / "progress.json", result=result, record=record)

    def correction_certifier(*args: Any, **kwargs: Any) -> Any:
        # Reserve the retry only when execution starts, not while writing or
        # reviewing the corrected checks. Persist before the external call.
        if needs_execution:
            result["execution_reruns_used"] = 1
            _save_review_progress(path=review_dir / "progress.json", result=result, record=record)
        return certifier(*args, **kwargs)

    if isinstance(planned_task, ApprovedIdea) or (record.get("authoring_mode") == "unified" and initial.part_to_correct not in {
        "grading_checks", "execution",
    }):
        candidate, corrected_gate, correction = _unified_final_review_correction(
            decision=initial, record=record, planned_task=planned_task,
            context=context, config=config, test_id=test_id,
            review_dir=review_dir, client=design_client, certifier=correction_certifier, regrader=regrader,
        )
    else:
        candidate, corrected_gate, correction = _correct_after_final_review(
            decision=initial, record=record, planned_task=planned_task,
            context=context, config=config, test_id=test_id,
            review_dir=review_dir, design_client=design_client,
            query_client=query_client, certifier=correction_certifier,
            grading_corrector=grading_corrector, regrader=regrader,
        )
    result["correction"] = correction
    if initial.part_to_correct != "execution":
        result["model_corrections_used"] = (
            record.get("model_corrections_used", int(not record["first_pass"]))
            if correction.get("model_correction_started") is False else 1
        )
    if candidate is None or corrected_gate is None:
        if correction.get("failed_candidate") and correction.get("failed_gate"):
            candidate = Path(correction["failed_candidate"])
            corrected_gate = Path(correction["failed_gate"])
        else:
            result["status"] = correction["status"] if str(correction.get("status") or "").endswith("_pending") else "replacement_required_after_one_correction"
            return None, result

    result.update(candidate=str(candidate), gate=str(corrected_gate), candidate_sha256=_sha256(candidate))
    corrected_clean, corrected_clean_evidence = _clean_certification_evidence(
        candidate=candidate,
        gate=corrected_gate,
        planned_task=planned_task,
        preflight_gate=(
            Path(record["gate"])
            if _sha256(candidate) == _sha256(Path(record["candidate"]))
            else None
        ),
    )
    result["corrected_clean_certification"] = corrected_clean_evidence
    if corrected_clean:
        result["status"] = "accepted_after_final_review_correction"
        result["corrected_final_model_review"] = "not_required"
        return candidate, result
    result.update(status="review_pending", pending_operation="review")
    _save_review_progress(path=review_dir / "progress.json", result=result, record=record)
    second = reviewer(
        test_id=test_id, candidate=candidate, gate=corrected_gate,
        planned_task=planned_task, context=context,
        out=review_dir / "correction_final_review", model=config.designer_model,
    )
    result["corrected_final_review"] = second.model_dump(mode="json")
    correction["corrected_final_review"] = second.model_dump(mode="json")
    dump_json(review_dir / "correction.json", correction)
    if second.accept:
        if not json.loads(corrected_gate.read_text()).get("result", {}).get("valid"):
            raise ValueError("reviewer accepted a corrected test whose four-run certification failed")
        result["status"] = "accepted_after_final_review_correction"
        return candidate, result
    result["status"] = "replacement_required_after_one_correction"
    return None, result


def _unique_saved_result(rows: list[Any], test_id: int, label: str) -> dict[str, Any]:
    matches = [row for row in rows if isinstance(row, dict) and row.get("id") == test_id]
    if len(matches) != 1:
        raise ValueError(f"the source run has no unique {label} for {test_id:03d}")
    return matches[0]


def _saved_new_test_resume_inputs(
    *,
    config_path: Path,
    plan_path: Path,
    source_run: Path,
    test_ids: list[int],
    config: Any,
    context: Any,
    tasks: list[PlannedTask],
) -> tuple[dict[int, dict[str, Any]], dict[int, PlannedTask], dict[str, Any]]:
    """Authenticate the unchanged inputs from one completed new-test run."""
    source_run = source_run.resolve()
    source_manifest_path = source_run / "manifest.json"
    if not source_manifest_path.is_file():
        raise ValueError("the source new-test run has no manifest.json")
    source_manifest = json.loads(source_manifest_path.read_text())
    source_ids = source_manifest.get("test_ids")
    if not isinstance(source_ids, list) or not all(
        isinstance(value, int) and not isinstance(value, bool) for value in source_ids
    ):
        raise ValueError("the source new-test run has invalid test IDs")
    if len(source_ids) != len(set(source_ids)) or len(source_ids) != len(tasks):
        raise ValueError("the source new-test run does not match its plan")
    if source_manifest.get("candidate_count") != len(tasks):
        raise ValueError("the source new-test run has an invalid candidate count")
    if Path(str(source_manifest.get("plan_path") or "")).resolve() != plan_path.resolve():
        raise ValueError("the supplied plan is not the source run's plan")
    if source_manifest.get("checkpoint_identity") != context.checkpoint_identity:
        raise ValueError("the source run does not match the active checkpoint")
    if not test_ids or len(test_ids) != len(set(test_ids)) or any(
        not isinstance(test_id, int) or isinstance(test_id, bool) or test_id <= 0
        for test_id in test_ids
    ):
        raise ValueError("--test-ids must contain unique positive integers")
    if not set(test_ids).issubset(source_ids):
        raise ValueError("--test-ids contains a test that is not in the source run")

    tasks_by_id = dict(zip(source_ids, tasks, strict=True))
    authoring_rows = source_manifest.get("authoring_results")
    review_rows = source_manifest.get("final_review_results")
    if not isinstance(authoring_rows, list) or not isinstance(review_rows, list):
        raise ValueError("the source run has no authoring or final-review results")

    records: dict[int, dict[str, Any]] = {}
    authenticated_files: dict[str, str] = {
        str(source_manifest_path): _sha256(source_manifest_path),
        str(config_path.resolve()): _sha256(config_path),
        str(plan_path.resolve()): _sha256(plan_path),
    }
    part_manifests: dict[Path, dict[str, Any]] = {}
    for test_id in test_ids:
        authoring_row = _unique_saved_result(authoring_rows, test_id, "authoring result")
        final_row = _unique_saved_result(review_rows, test_id, "final-review result")
        if str(final_row.get("status") or "").startswith("accepted_"):
            raise ValueError(f"test {test_id:03d} was already accepted")
        raw_decision = final_row.get("initial_final_review")
        if not isinstance(raw_decision, dict):
            raise ValueError(f"test {test_id:03d} has no saved initial final review")
        decision = FinalTraceDecision.model_validate(raw_decision)
        if decision.accept or decision.part_to_correct != "grading_checks":
            raise ValueError(
                f"test {test_id:03d} did not stop after a grading-check final review"
            )

        candidate_value = authoring_row.get("candidate")
        gate_value = authoring_row.get("gate")
        if not isinstance(candidate_value, str) or not isinstance(gate_value, str):
            raise ValueError(f"test {test_id:03d} has no saved candidate and gate")
        candidate = _candidate_path(source_run, candidate_value)
        gate = _candidate_path(source_run, gate_value)
        if not candidate.is_relative_to(source_run) or not gate.is_relative_to(source_run):
            raise ValueError(f"test {test_id:03d} points outside the source run")
        _authenticated_saved_gate(test_id=test_id, candidate=candidate, gate=gate)

        review_dir = source_run / "final_trace_reviews" / f"{test_id:03d}"
        decision_path = review_dir / "decision.json"
        request_path = review_dir / "request.json"
        if not decision_path.is_file() or not request_path.is_file():
            raise ValueError(f"test {test_id:03d} has incomplete saved final-review evidence")
        saved_decision = FinalTraceDecision.model_validate_json(decision_path.read_text())
        if saved_decision.model_dump(mode="json") != decision.model_dump(mode="json"):
            raise ValueError(f"test {test_id:03d} final-review decision changed")
        expected_request = build_final_trace_review_request(
            candidate_path=candidate,
            certification_gate_path=gate,
            planned_task=tasks_by_id[test_id],
            context=context,
        )
        if json.loads(request_path.read_text()) != expected_request:
            raise ValueError(f"test {test_id:03d} final-review request changed")

        part_manifest_path = candidate.parents[2] / "manifest.json"
        if not part_manifest_path.is_file() or not part_manifest_path.is_relative_to(source_run):
            raise ValueError(f"test {test_id:03d} has no source authoring manifest")
        if part_manifest_path not in part_manifests:
            part_manifest = json.loads(part_manifest_path.read_text())
            if (
                part_manifest.get("checkpoint_identity") != context.checkpoint_identity
                or part_manifest.get("persona") != context.persona
                or part_manifest.get("evaluation_date") != config.evaluation_date
                or Path(str(part_manifest.get("config_path") or "")).resolve()
                != config_path.resolve()
                or part_manifest.get("config_sha256") != _sha256(config_path)
                or Path(str(part_manifest.get("plan_path") or "")).resolve()
                != plan_path.resolve()
                or part_manifest.get("plan_sha256") != _sha256(plan_path)
            ):
                raise ValueError("the source authoring manifest does not match current inputs")
            selected_ids = part_manifest.get("selected_test_ids")
            selected_positions = part_manifest.get("selected_plan_positions")
            if (
                not isinstance(selected_ids, list)
                or len(selected_ids) != len(set(selected_ids))
                or not isinstance(selected_positions, list)
                or len(selected_positions) != len(selected_ids)
            ):
                raise ValueError("the source authoring manifest has invalid selected test IDs")
            part_manifests[part_manifest_path] = part_manifest
            authenticated_files[str(part_manifest_path)] = _sha256(part_manifest_path)
        part_manifest = part_manifests[part_manifest_path]
        if test_id not in part_manifest["selected_test_ids"]:
            raise ValueError(f"test {test_id:03d} was not selected by its source authoring run")
        part_row = _unique_saved_result(part_manifest.get("results") or [], test_id, "part result")
        if (
            _candidate_path(source_run, str(part_row.get("candidate") or "")) != candidate
            or _candidate_path(source_run, str(part_row.get("gate") or "")) != gate
        ):
            raise ValueError(f"test {test_id:03d} does not match its source authoring result")

        for path in (candidate, gate, decision_path, request_path):
            authenticated_files[str(path)] = _sha256(path)
        records[test_id] = {
            "candidate": candidate,
            "gate": gate,
            "decision": decision,
            "request": request_path,
            "run_dir": None,
            "first_pass": True,
            "certification_valid": bool(
                json.loads(gate.read_text()).get("result", {}).get("valid")
            ),
            "authoring_mode": str(authoring_row.get("authoring_mode") or "legacy"),
            "source_authoring_result": copy.deepcopy(authoring_row),
        }

    identity = {
        "source_run": str(source_run),
        "source_manifest_sha256": _sha256(source_manifest_path),
        "checkpoint_identity": context.checkpoint_identity,
        "config_path": str(config_path.resolve()),
        "config_sha256": _sha256(config_path),
        "plan_path": str(plan_path.resolve()),
        "plan_sha256": _sha256(plan_path),
        "selected_test_ids": list(test_ids),
        "authenticated_files": authenticated_files,
    }
    return records, {test_id: tasks_by_id[test_id] for test_id in test_ids}, identity


def _approved_new_test_resume_inputs(
    *,
    config_path: Path,
    plan_path: Path,
    source_run: Path,
    corrections_path: Path,
    test_ids: list[int],
    config: Any,
    context: Any,
    tasks: list[PlannedTask],
    out: Path,
) -> tuple[dict[int, dict[str, Any]], dict[int, PlannedTask], dict[str, Any]]:
    """Authenticate one saved corrected candidate for an approved local patch."""
    if any(isinstance(task, ApprovedIdea) for task in tasks):
        raise ValueError("approved corrections support historical plans only; resume version-4 tests through their saved review")
    source_run = source_run.resolve()
    source_manifest_path = source_run / "manifest.json"
    if not source_manifest_path.is_file():
        raise ValueError("the approved-correction source run has no manifest.json")
    source_manifest = json.loads(source_manifest_path.read_text())
    source_ids = source_manifest.get("test_ids")
    if (
        not isinstance(source_ids, list)
        or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in source_ids)
        or len(source_ids) != len(set(source_ids))
        or not set(test_ids).issubset(source_ids)
    ):
        raise ValueError("the approved-correction source run has different test IDs")
    if source_manifest.get("candidate_count") != len(source_ids):
        raise ValueError("the approved-correction source run has an invalid candidate count")
    if Path(str(source_manifest.get("plan_path") or "")).resolve() != plan_path.resolve():
        raise ValueError("the approved-correction source run has a different plan")
    if source_manifest.get("checkpoint_identity") != context.checkpoint_identity:
        raise ValueError("the approved-correction source run has a different checkpoint")
    if not test_ids or len(test_ids) != len(set(test_ids)):
        raise ValueError("approved-correction test IDs must be unique")
    if len(tasks) != len(source_ids):
        raise ValueError("approved-correction test IDs do not match the plan")

    corrections = _load_approved_corrections(
        path=corrections_path,
        expected_test_ids=set(test_ids),
        out=out,
        source_result_paths={test_id: source_manifest_path for test_id in test_ids},
        source_root=source_run,
    )
    authoring_rows = source_manifest.get("authoring_results")
    review_rows = source_manifest.get("final_review_results")
    if not isinstance(authoring_rows, list) or not isinstance(review_rows, list):
        raise ValueError("the approved-correction source run has no saved results")

    all_tasks_by_id = dict(zip(source_ids, tasks, strict=True))
    tasks_by_id = {test_id: all_tasks_by_id[test_id] for test_id in test_ids}
    authenticated_files = {
        str(source_manifest_path): _sha256(source_manifest_path),
        str(corrections_path.resolve()): _sha256(corrections_path),
        str(config_path.resolve()): _sha256(config_path),
        str(plan_path.resolve()): _sha256(plan_path),
    }
    records: dict[int, dict[str, Any]] = {}
    for test_id in test_ids:
        authoring_row = _unique_saved_result(authoring_rows, test_id, "authoring result")
        matching_review_rows = [
            row
            for row in review_rows
            if isinstance(row, dict) and row.get("id") == test_id
        ]
        if len(matching_review_rows) > 1:
            raise ValueError(
                f"the source run has more than one final-review result for {test_id:03d}"
            )
        if matching_review_rows and str(
            matching_review_rows[0].get("status") or ""
        ).startswith("accepted_"):
            raise ValueError(f"test {test_id:03d} was already accepted")
        correction = corrections[test_id]
        candidate = Path(correction["source_candidate"]).resolve()
        gate = Path(correction["source_gate"]).resolve()
        candidate_value = _load_yaml_object(candidate)
        if str(candidate_value.get("id") or "") not in {str(test_id), f"{test_id:03d}"}:
            raise ValueError(f"approved candidate has the wrong test ID for {test_id:03d}")
        TestSpec.model_validate(candidate_value)
        _authenticated_saved_gate(test_id=test_id, candidate=candidate, gate=gate)
        if authoring_row.get("id") != test_id:
            raise ValueError(f"the source authoring result has the wrong ID for {test_id:03d}")
        authenticated_files.update(
            {
                str(candidate): _sha256(candidate),
                str(gate): _sha256(gate),
            }
        )
        records[test_id] = {
            "candidate": candidate,
            "gate": gate,
            "decision": None,
            "request": None,
            "run_dir": None,
            "first_pass": False,
            "certification_valid": False,
            "authoring_mode": str(authoring_row.get("authoring_mode") or "unified"),
            "approved_correction": correction,
        }

    authenticated_files[str(source_manifest_path)] = _sha256(source_manifest_path)
    identity = {
        "source_run": str(source_run),
        "source_kind": "approved_grading_correction",
        "source_manifest_sha256": _sha256(source_manifest_path),
        "checkpoint_identity": context.checkpoint_identity,
        "config_path": str(config_path.resolve()),
        "config_sha256": _sha256(config_path),
        "plan_path": str(plan_path.resolve()),
        "plan_sha256": _sha256(plan_path),
        "approved_corrections": str(corrections_path.resolve()),
        "approved_corrections_sha256": _sha256(corrections_path),
        "selected_test_ids": list(test_ids),
        "authenticated_files": authenticated_files,
    }
    return records, tasks_by_id, identity


def _authenticated_part_manifest(
    *,
    source_run: Path,
    config_path: Path,
    plan_path: Path,
    config: Any,
    context: Any,
    test_id: int | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Locate and authenticate the unique part containing the selected test."""
    matches = []
    for path in sorted((source_run / "authoring").glob("part_*/manifest.json")):
        manifest = json.loads(path.read_text())
        if test_id is None or test_id in (manifest.get("selected_test_ids") or []):
            matches.append((path, manifest))
    if len(matches) != 1:
        raise ValueError("the source run has no unique authoring part manifest for this test")
    part_manifest_path, manifest = matches[0]
    if (
        manifest.get("checkpoint_identity") != context.checkpoint_identity
        or manifest.get("persona") != context.persona
        or manifest.get("evaluation_date") != config.evaluation_date
        or Path(str(manifest.get("config_path") or "")).resolve()
        != config_path.resolve()
        or manifest.get("config_sha256") != _sha256(config_path)
        or Path(str(manifest.get("plan_path") or "")).resolve()
        != plan_path.resolve()
        or manifest.get("plan_sha256") != _sha256(plan_path)
    ):
        raise ValueError("the source authoring manifest does not match current inputs")
    return part_manifest_path, manifest


def _part_tasks_by_test_id(
    *, part_manifest: dict[str, Any], tasks: list[PlannedTask]
) -> dict[int, PlannedTask]:
    selected_ids = part_manifest.get("selected_test_ids")
    selected_positions = part_manifest.get("selected_plan_positions")
    if (
        not isinstance(selected_ids, list)
        or not isinstance(selected_positions, list)
        or len(selected_ids) != len(selected_positions)
        or len(selected_ids) != len(set(selected_ids))
        or any(
            not isinstance(test_id, int) or isinstance(test_id, bool)
            for test_id in selected_ids
        )
        or any(
            not isinstance(position, int)
            or isinstance(position, bool)
            or not 1 <= position <= len(tasks)
            for position in selected_positions
        )
    ):
        raise ValueError("the source authoring manifest has invalid selected test IDs")
    return {
        test_id: tasks[position - 1]
        for test_id, position in zip(selected_ids, selected_positions, strict=True)
    }


def _source_part_result(
    *, part_manifest: dict[str, Any], test_id: int
) -> dict[str, Any]:
    rows = [
        row
        for row in part_manifest.get("results") or []
        if isinstance(row, dict) and row.get("id") == test_id
    ]
    if len(rows) != 1:
        raise ValueError(f"the source authoring manifest has no unique result for {test_id:03d}")
    return rows[0]


def _incomplete_final_review_resume_inputs(
    *,
    config_path: Path,
    plan_path: Path,
    source_run: Path,
    test_ids: list[int],
    config: Any,
    context: Any,
    tasks: list[PlannedTask],
) -> tuple[dict[int, dict[str, Any]], dict[int, PlannedTask], dict[str, Any]]:
    """Authenticate an unfinished historical run and any completed acceptance."""
    if (source_run / "manifest.json").exists():
        raise ValueError("the source run has a completed manifest")
    if len(test_ids) != 1:
        raise ValueError("an incomplete final-review resume accepts exactly one test ID")
    part_manifest_path, part_manifest = _authenticated_part_manifest(
        source_run=source_run,
        config_path=config_path,
        plan_path=plan_path,
        config=config,
        context=context,
        test_id=test_ids[0],
    )
    tasks_by_id = _part_tasks_by_test_id(part_manifest=part_manifest, tasks=tasks)
    test_id = test_ids[0]
    if test_id not in tasks_by_id:
        raise ValueError("--test-ids is not present in the source authoring part")
    row = _source_part_result(part_manifest=part_manifest, test_id=test_id)
    if row.get("status") != "review_required" and not str(row.get("status") or "").startswith("certified_"):
        raise ValueError("the incomplete source did not stop before final review")
    candidate = _candidate_path(source_run, str(row.get("candidate") or ""))
    gate = _candidate_path(source_run, str(row.get("gate") or ""))
    _authenticated_saved_gate(test_id=test_id, candidate=candidate, gate=gate)
    review_dir = source_run / "final_trace_reviews" / f"{test_id:03d}"
    request_path = review_dir / "request.json"
    raw_response_path = review_dir / "work" / "response_cache.json"
    if not request_path.is_file() or not raw_response_path.is_file():
        raise ValueError("the incomplete source has no saved final-review request and raw response")
    expected_request = build_final_trace_review_request(
        candidate_path=candidate,
        certification_gate_path=gate,
        planned_task=tasks_by_id[test_id],
        context=context,
    )
    if json.loads(request_path.read_text()) != expected_request:
        raise ValueError("the saved final-review request does not match current inputs")
    authenticated_files = {
        str(part_manifest_path): _sha256(part_manifest_path),
        str(config_path.resolve()): _sha256(config_path),
        str(plan_path.resolve()): _sha256(plan_path),
        str(candidate): _sha256(candidate),
        str(gate): _sha256(gate),
        str(request_path): _sha256(request_path),
        str(raw_response_path): _sha256(raw_response_path),
    }
    decision = None
    decision_path = review_dir / "decision.json"
    if decision_path.is_file():
        saved_decision = FinalTraceDecision.model_validate(json.loads(decision_path.read_text()))
        if saved_decision.accept:
            evidence = [decision_path, review_dir / "response.json", review_dir / "system.txt", review_dir / "schema.json"]
            if not all(path.is_file() for path in evidence):
                raise ValueError("saved acceptance is missing its review evidence")
            cache = json.loads(raw_response_path.read_text())
            fingerprint = request_hash(
                (review_dir / "system.txt").read_text().removesuffix("\n"), expected_request,
                response_schema=json.loads((review_dir / "schema.json").read_text()),
                response_schema_name="enact_final_trace_review",
            )
            raw = json.loads((review_dir / "response.json").read_text())
            if cache.get("request_sha256") != fingerprint or cache.get("response") != raw:
                raise ValueError("saved acceptance does not match its review cache")
            response = FinalTraceReviewResponse.model_validate(
                {key: value for key, value in raw.items() if not key.startswith("_")}
            )
            _validate_audit_matches_request(response, expected_request)
            _validate_issue_citations(response, expected_request)
            if response.test_id != test_id or saved_decision != FinalTraceDecision.model_validate(response.model_dump(mode="json")):
                raise ValueError("saved acceptance does not match its validated review")
            if not expected_request["certification_result"]["valid"]:
                raise ValueError("saved acceptance has no passing four-run certification")
            decision = saved_decision
            authenticated_files.update({str(path): _sha256(path) for path in evidence})
    return (
        {
            test_id: {
                "candidate": candidate,
                "gate": gate,
                "decision": decision,
                "request": request_path,
                "raw_reviewer_response": raw_response_path,
                "run_dir": None,
                "first_pass": row.get("status") != "certified_correction",
                "model_corrections_used": row.get("model_corrections_used", int(row.get("status") == "certified_correction")),
                "certification_valid": bool(
                    json.loads(gate.read_text()).get("result", {}).get("valid")
                ),
                "authoring_mode": str(row.get("authoring_mode") or "unified"),
                "resume_kind": "reuse_accepting_final_review" if decision is not None else "rerun_final_review",
            }
        },
        {test_id: tasks_by_id[test_id]},
        {
            "source_run": str(source_run),
            "source_kind": "incomplete_final_review",
            "checkpoint_identity": context.checkpoint_identity,
            "config_path": str(config_path.resolve()),
            "config_sha256": _sha256(config_path),
            "plan_path": str(plan_path.resolve()),
            "plan_sha256": _sha256(plan_path),
            "selected_test_ids": list(test_ids),
            "authenticated_files": authenticated_files,
        },
    )


def _local_validation_resume_inputs(
    *,
    config_path: Path,
    plan_path: Path,
    source_run: Path,
    test_ids: list[int],
    config: Any,
    context: Any,
    tasks: list[PlannedTask],
) -> tuple[dict[int, dict[str, Any]], dict[int, PlannedTask], dict[str, Any]] | None:
    """Recover one unchanged authoring response blocked only by a shared local validator."""
    source_manifest_path = source_run / "manifest.json"
    if not source_manifest_path.is_file() or len(test_ids) != 1:
        return None
    source_manifest = json.loads(source_manifest_path.read_text())
    if (
        source_manifest.get("checkpoint_identity") != context.checkpoint_identity
        or Path(str(source_manifest.get("plan_path") or "")).resolve()
        != plan_path.resolve()
    ):
        return None
    part_manifest_path, part_manifest = _authenticated_part_manifest(
        source_run=source_run,
        config_path=config_path,
        plan_path=plan_path,
        config=config,
        context=context,
        test_id=test_ids[0],
    )
    tasks_by_id = _part_tasks_by_test_id(part_manifest=part_manifest, tasks=tasks)
    test_id = test_ids[0]
    if test_id not in tasks_by_id:
        return None
    row = _source_part_result(part_manifest=part_manifest, test_id=test_id)
    failure = row.get("first_failure")
    if (
        row.get("status") != "replacement_required"
        or not isinstance(failure, dict)
        or failure.get("stage") != "local_validation"
        or "is prose, not a mechanical field" not in str(failure.get("reason") or "")
    ):
        return None
    response_path = (
        part_manifest_path.parent
        / "proposals"
        / f"{test_id:03d}"
        / "initial_authoring_response.json"
    )
    if not response_path.is_file():
        raise ValueError("the local-validation source has no saved initial authoring response")
    raw = json.loads(response_path.read_text())
    saved_response = failure.get("authoring")
    without_metadata = {key: value for key, value in raw.items() if not key.startswith("_")}
    if without_metadata != saved_response:
        raise ValueError("the saved initial authoring response does not match its failure record")
    # This validates the exact old response under the corrected shared validator.
    authored = AuthoredTestResponse.model_validate(without_metadata)
    _validate_authoring_response(
        response=authored, context=context, planned_task=tasks_by_id[test_id]
    )
    authenticated_files = {
        str(source_manifest_path): _sha256(source_manifest_path),
        str(part_manifest_path): _sha256(part_manifest_path),
        str(config_path.resolve()): _sha256(config_path),
        str(plan_path.resolve()): _sha256(plan_path),
        str(response_path): _sha256(response_path),
    }
    return (
        {
            test_id: {
                "candidate": None,
                "gate": None,
                "decision": None,
                "request": None,
                "saved_authoring_response": response_path,
                "run_dir": None,
                "first_pass": True,
                "certification_valid": False,
                "authoring_mode": "unified",
                "resume_kind": "rerun_candidate_after_shared_validator_fix",
            }
        },
        {test_id: tasks_by_id[test_id]},
        {
            "source_run": str(source_run),
            "source_kind": "shared_local_validator_fix",
            "checkpoint_identity": context.checkpoint_identity,
            "config_path": str(config_path.resolve()),
            "config_sha256": _sha256(config_path),
            "plan_path": str(plan_path.resolve()),
            "plan_sha256": _sha256(plan_path),
            "selected_test_ids": list(test_ids),
            "authenticated_files": authenticated_files,
        },
    )


def _materialize_saved_authored_candidate(
    *, record: dict[str, Any], planned_task: PlannedTask, context: Any, config: Any, test_id: int
) -> Path:
    """Write the exact saved authoring response as a fresh candidate after a shared fix."""
    response_path = Path(record["saved_authoring_response"])
    raw = json.loads(response_path.read_text())
    authored = AuthoredTestResponse.model_validate(
        {key: value for key, value in raw.items() if not key.startswith("_")}
    )
    _validate_authoring_response(response=authored, context=context, planned_task=planned_task)
    design = _completed_design_from_authoring(
        response=authored, planned_task=planned_task, context=context
    )
    problems = validate_design(context, planned_task, design)
    if problems:
        raise AuthoringValidationError("; ".join(problems))
    candidate = assemble_candidate(
        context=context,
        planned_task=planned_task,
        evaluation_date=config.evaluation_date,
        test_id=str(test_id),
        design=design,
        blind_query=BlindQueryResponse(query=authored.query),
    )
    path = record["run_dir"] / "proposals" / f"{test_id:03d}" / "initial_candidate.yaml"
    write_candidate(path, candidate)
    return path


def _verify_saved_resume_files(identity: dict[str, Any]) -> None:
    """Reject source input changed while the bounded resume was running."""
    for raw_path, expected_hash in identity["authenticated_files"].items():
        path = Path(raw_path)
        if not path.is_file() or _sha256(path) != expected_hash:
            raise ValueError(f"authenticated resume input changed: {path}")


def _progress_resume_inputs(
    *, config_path: Path, plan_path: Path, source_run: Path, test_ids: list[int],
    config: Any, context: Any, tasks: list[PlannedTask], allow_approved_correction: bool = False,
) -> tuple[dict[int, dict[str, Any]], dict[int, PlannedTask], dict[str, Any]] | None:
    loaded = {
        test_id: load_progress(
            source_run=source_run, test_id=test_id, config_path=config_path,
            plan_path=plan_path, config=config, context=context, tasks=tasks,
        )
        for test_id in test_ids
    }
    if not any(loaded.values()):
        return None
    if not all(loaded.values()):
        raise ValueError("resume current and historical progress records separately")
    source_manifest_path = source_run / "manifest.json"
    source_manifest = json.loads(source_manifest_path.read_text()) if source_manifest_path.is_file() else {}
    completed_ids = {
        row["id"] for row in source_manifest.get("final_review_results") or []
        if str(row.get("status") or "").startswith("accepted_")
    } if source_manifest.get("accepted_batch") else set()
    if completed_ids.intersection(test_ids) and not allow_approved_correction:
        raise ValueError("an accepted batch already contains one of the selected tests")
    records = {}
    selected_tasks = {}
    authenticated: dict[str, str] = {}
    for test_id, saved in loaded.items():
        assert saved is not None
        progress, files = saved
        authenticated.update(files)
        task = tasks[progress["plan_position"] - 1]
        selected_tasks[test_id] = task
        from authoring.exact_repairs import effective_context
        saved_inputs = json.loads(Path(progress["authoring_inputs"]).read_text())
        overrides = saved_inputs.get("test_overrides", {}).get(str(test_id), {})
        test_context = effective_context(context, task, overrides)
        if str(progress["status"]).startswith("replacement_") and not allow_approved_correction:
            raise ValueError("a rejected test needs an explicitly approved correction or replacement idea")
        candidate = Path(progress["candidate"]).resolve() if progress.get("candidate") else None
        gate = Path(progress["gate"]).resolve() if progress.get("gate") else None
        request_path = None
        decision = None
        if candidate is not None and gate is not None:
            _authenticated_saved_gate(test_id=test_id, candidate=candidate, gate=gate)
            expected_request = build_final_trace_review_request(
                candidate_path=candidate, certification_gate_path=gate,
                planned_task=task, context=test_context,
            )
            for value in files:
                path = Path(value)
                if path.name == "request.json" and json.loads(path.read_text()) == expected_request:
                    request_path = path
                    break
            raw_decision = progress.get("corrected_final_review") or progress.get("initial_final_review")
            if request_path is not None and isinstance(raw_decision, dict):
                parsed = FinalTraceDecision.model_validate(raw_decision)
                if parsed.test_id != test_id:
                    raise ValueError("saved final review has a different test ID")
                if parsed.accept and expected_request["certification_result"]["valid"]:
                    decision = parsed
        records[test_id] = {
            "resume_kind": "saved_stage", "progress": progress,
            "candidate": candidate, "gate": gate, "request": request_path,
            "decision": decision, "run_dir": None,
            "first_pass": progress.get("model_corrections_used", 0) == 0,
            "model_corrections_used": progress.get("model_corrections_used", 0),
            "execution_reruns_used": progress.get("execution_reruns_used", 0),
            "authoring_mode": "unified", "authenticated_files": files,
            "test_overrides": overrides,
            "was_accepted": test_id in completed_ids,
        }
    if source_manifest_path.is_file():
        authenticated[str(source_manifest_path)] = _sha256(source_manifest_path)
    identity = {
        "source_run": str(source_run), "source_kind": "saved_stage",
        "checkpoint_identity": context.checkpoint_identity,
        "config_path": str(config_path.resolve()), "config_sha256": _sha256(config_path),
        "plan_path": str(plan_path.resolve()), "plan_sha256": _sha256(plan_path),
        "selected_test_ids": test_ids, "authenticated_files": authenticated,
    }
    for record in records.values():
        binding = ((record.get("test_overrides") or {}).get("exact_repair") or {}).get("replaces_accepted_batch")
        if binding is not None:
            validate_working_batch_replacement(config=config, context=context, binding=binding)
            if identity.get("replaces_accepted_batch", binding) != binding:
                raise ValueError("resume accepted revisions from one batch at a time")
            identity["replaces_accepted_batch"] = binding
            for filename, key in (("planning_batch.json", "planning_batch_sha256"), ("provenance.json", "provenance_sha256")):
                identity["authenticated_files"][str(Path(binding["directory"]) / filename)] = binding[key]
    return records, selected_tasks, identity


def _copy_authenticated_tree(source: Path, destination: Path, files: dict[str, str], *, model_caches: bool = False) -> None:
    source = source.resolve()
    for value in files:
        path = Path(value)
        if path.is_relative_to(source) and path.name not in {"progress.json", "status.json"}:
            if not model_caches and path.name.endswith("response_cache.json"):
                continue
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _attach_approved_v4_corrections(
    *, path: Path, records: dict[int, dict[str, Any]], identity: dict[str, Any],
    context: Any = None, tasks_by_id: dict[int, Any] | None = None, config: Any = None,
) -> None:
    """Bind an approved writer correction or exact criterion edit to saved work."""
    from authoring.complete_test import ApprovedCriterionCorrection, ApprovedWriterCorrection, parse_writer_response
    from authoring.exact_repairs import (
        ApprovedExactRepair, ApprovedInputBudget, apply_exact_edits, bind_input_budget,
        effective_context, grade_only, has_saved_executions, recover_failed_correction_source,
    )

    value = json.loads(path.read_text())
    if not isinstance(value, dict) or set(value) != {"version", "corrections"} or value["version"] != 4:
        raise ValueError("version-4 writer corrections require version and corrections")
    if not isinstance(value["corrections"], list):
        raise ValueError("writer corrections must be a list")
    rows = [
        (ApprovedExactRepair if isinstance(row, dict) and row.get("kind") == "exact_test_repair"
         else ApprovedInputBudget if isinstance(row, dict) and row.get("kind") == "writer_input_budget"
         else ApprovedCriterionCorrection if isinstance(row, dict) and "criterion_replacements" in row
         else ApprovedWriterCorrection).model_validate(row)
        for row in value["corrections"]
    ]
    if len(rows) != len(records) or {row.test_id for row in rows} != set(records):
        raise ValueError("writer corrections must name each selected test exactly once")
    for row in rows:
        record = records[row.test_id]
        progress = record["progress"]
        if record.get("was_accepted") and not (
            isinstance(row, ApprovedExactRepair) and row.replaces_accepted_batch is not None
        ):
            raise ValueError("an accepted test requires an exact grading repair and batch binding")
        if isinstance(row, ApprovedInputBudget):
            bind_input_budget(row, record)
            continue
        source = Path(progress.get("authoring_response") or "").resolve()
        if isinstance(row, ApprovedExactRepair) and row.failed_correction_recovery is not None:
            source = recover_failed_correction_source(row, record)
        if (str(source) not in record["authenticated_files"] or not source.is_file()
                or _sha256(source) != row.source_response_sha256):
            raise ValueError("approved writer correction does not match the saved response")
        previous = parse_writer_response(json.loads(source.read_text()))
        if previous.test is None:
            raise ValueError("an approved writer correction requires a written draft")
        if isinstance(row, ApprovedExactRepair):
            from authoring.complete_test import assemble_written_test, validate_written_test, writer_payload
            for key in ("candidate", "gate"):
                expected = getattr(row, f"source_{key}_sha256")
                actual = _sha256(record[key]) if record.get(key) else None
                if expected != actual:
                    raise ValueError(f"exact repair does not match the saved {key}")
            revised = apply_exact_edits(json.loads(source.read_text()), row)
            if row.replaces_accepted_batch is not None:
                binding = row.replaces_accepted_batch.model_dump(mode="json")
                validate_working_batch_replacement(config=config, context=context, binding=binding)
                directory = Path(binding["directory"]).resolve()
                source_manifest = json.loads((Path(identity["source_run"]) / "manifest.json").read_text())
                if not record.get("was_accepted") or directory != Path(source_manifest.get("accepted_batch") or "").resolve():
                    raise ValueError("accepted repair does not name its completed source batch")
                for filename, key in (("planning_batch.json", "planning_batch_sha256"), ("provenance.json", "provenance_sha256")):
                    if _sha256(directory / filename) != binding[key]:
                        raise ValueError("accepted repair source batch changed")
                    identity["authenticated_files"][str(directory / filename)] = binding[key]
                ids = set(json.loads((directory / "provenance.json").read_text())["accepted_test_ids"])
                revised_ids = {other.test_id for other in rows if isinstance(other, ApprovedExactRepair)
                               and other.replaces_accepted_batch == row.replaces_accepted_batch}
                if ids != revised_ids:
                    raise ValueError("revise a completed batch only when every accepted entry is explicitly selected")
                identity["replaces_accepted_batch"] = binding
            additions = [item.model_dump(mode="json") for item in row.fact_source_additions]
            overrides = copy.deepcopy(record.get("test_overrides") or {})
            overrides.setdefault("fact_source_additions", []).extend(additions)
            test_context = effective_context(context, tasks_by_id[row.test_id], overrides)
            if row.failed_correction_recovery is not None:
                original_candidate = assemble_written_test(
                    response=previous, idea=tasks_by_id[row.test_id], context=test_context,
                    evaluation_date=config.evaluation_date, test_id=row.test_id,
                    payload=writer_payload(config=config, context=test_context, idea=tasks_by_id[row.test_id]),
                )
                if original_candidate != yaml.safe_load(Path(record["candidate"]).read_text()):
                    raise ValueError("recovered writer response does not reconstruct the bound candidate")
                record["recovered_authoring_response"] = str(source)
            validate_written_test(
                response=revised, idea=tasks_by_id[row.test_id], context=test_context,
                payload=writer_payload(config=config, context=test_context, idea=tasks_by_id[row.test_id]),
            )
            if grade_only(row) and record.get("gate") is None and has_saved_executions(record):
                raise ValueError("a grading-only repair cannot discard authenticated saved executions")
            overrides["exact_repair"] = row.model_dump(mode="json")
            record["test_overrides"] = overrides
            record["approved_exact_repair"] = row.model_dump(mode="json")
            continue
        if isinstance(row, ApprovedCriterionCorrection):
            decision = progress.get("corrected_final_review") or progress.get("initial_final_review") or {}
            if (record["model_corrections_used"] != 1 or record["gate"] is None
                    or _sha256(record["gate"]) != row.source_gate_sha256
                    or decision.get("part_to_correct") != "grading_checks"):
                raise ValueError("an exact criterion edit needs one used correction, a matching gate, and grading-only review")
            by_id = {check.assertion.get("check_id"): check.assertion for check in previous.test.checks}
            ids = [item.check_id for item in row.criterion_replacements]
            if len(set(ids)) != len(ids):
                raise ValueError("an exact criterion edit repeats a check ID")
            for item in row.criterion_replacements:
                assertion = by_id.get(item.check_id) or {}
                if (assertion.get("type") != "field_llm_judge"
                        or assertion.get("criterion") != item.previous_criterion
                        or not item.criterion.strip() or item.criterion == item.previous_criterion):
                    raise ValueError("an exact criterion edit does not match a changed semantic check")
            record["approved_criterion_correction"] = row.model_dump(mode="json")
            continue
        if (progress["status"] != "validation_pending" or record["model_corrections_used"]
                or record["gate"] is not None):
            raise ValueError("an approved writer correction needs an uncorrected validation-pending draft")
        known = {check.assertion.get("check_id") for check in previous.test.checks}
        if (len(set(row.protected_check_ids)) != len(row.protected_check_ids)
                or set(row.protected_check_ids) - known):
            raise ValueError("approved writer correction names invalid protected checks")
        if len(set(row.allowed_correction_fields)) != len(row.allowed_correction_fields):
            raise ValueError("approved writer correction repeats a field permission")
        if not row.specific_problem.strip() or not row.required_correction.strip():
            raise ValueError("approved writer correction needs a concrete problem and correction")
        record["approved_writer_correction"] = row.model_dump(mode="json")
    identity["authenticated_files"][str(path.resolve())] = _sha256(path)
    identity["approved_v4_corrections"] = value


def _continue_saved_stage(
    *, test_id: int, record: dict[str, Any], task: PlannedTask, config: Any,
    context: Any, out: Path, certifier: Any, reviewer: Any,
    grading_corrector: Any, regrader: Any,
) -> tuple[Path | None, dict[str, Any], dict[str, Any] | None]:
    """Continue the recorded step, using the normal writing and review functions."""
    from authoring.exact_repairs import (
        ApprovedExactRepair, apply_exact_edits, certify_reserved_retry, effective_context, grade_only,
    )

    overrides = record.get("test_overrides") or {}
    has_exact_repair = bool(overrides.get("exact_repair"))
    context = effective_context(context, task, overrides)
    if "writer_max_input_tokens" in overrides:
        config = config.model_copy(update={"writer_max_input_tokens": overrides["writer_max_input_tokens"]})
    progress = record["progress"]
    proposal = record["run_dir"] / "proposals" / f"{test_id:03d}"
    review_dir = out / "final_trace_reviews" / f"{test_id:03d}"
    files = record["authenticated_files"]
    exact_repair = ApprovedExactRepair.model_validate(record["approved_exact_repair"]) if record.get("approved_exact_repair") else None
    source_response = record.get("recovered_authoring_response") or progress.get("authoring_response")
    response_path = Path(source_response) if source_response else None
    if response_path is not None and not record.get("approved_writer_correction"):
        if exact_repair is None:
            _copy_authenticated_tree(response_path.parent, proposal, files)
        record["authoring_response"] = proposal / ("initial_authoring_response.json" if exact_repair else response_path.name)
    for key in ("candidate", "gate"):
        if record.get(key) is not None:
            name = f"source_{key}{Path(record[key]).suffix}" if exact_repair else Path(record[key]).name
            destination = proposal / name
            shutil.copy2(record[key], destination)
            record[key] = destination
    if record.get("request") is not None:
        review_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(record["request"], review_dir / "request.json")
    client = AzureJsonClient(model=config.designer_model, reasoning_effort="high")
    status = progress["status"]
    exact_edit = record.get("approved_criterion_correction")
    retry_path = proposal / "reserved_execution_retry.json"
    if exact_repair is not None and exact_repair.failed_correction_recovery is not None:
        if retry_path.exists():
            raise ValueError("a new recovery cannot replace an existing reserved retry")
        dump_json(retry_path, {
            "version": 1, "status": "prepared",
            "recovery": exact_repair.failed_correction_recovery.model_dump(mode="json"),
            "source_response_sha256": exact_repair.source_response_sha256,
            "source_candidate_sha256": exact_repair.source_candidate_sha256,
            "source_gate_sha256": exact_repair.source_gate_sha256,
        })
    prior_gate = None
    if exact_edit is not None or (
        exact_repair is not None and grade_only(exact_repair) and record.get("gate") is not None
        and exact_repair.failed_correction_recovery is None
    ):
        prior_gate = proposal / "approved_criterion_prior_gate.json"
        shutil.copy2(record["gate"], prior_gate)
        record.update(candidate=None, gate=None, decision=None)
    if exact_repair is not None:
        record.update(candidate=None, gate=None, decision=None)
        status = "validation_pending"
    initial_review = progress.get("initial_final_review") or {}
    needs_execution_retry = initial_review.get("part_to_correct") == "execution" or any(
        issue.get("owning_step") == "execution" for issue in initial_review.get("issues", [])
    )
    if (
        status == "repair_pending"
        and has_exact_repair
        and initial_review.get("part_to_correct") == "execution"
        and not record["execution_reruns_used"]
    ):
        status = "correction_pending"

    def resume_certifier(*args: Any, **kwargs: Any) -> Any:
        if retry_path.is_file():
            repair_permission = overrides.get("exact_repair") or {}
            retry = json.loads(retry_path.read_text())
            if (not repair_permission.get("failed_correction_recovery")
                    or retry.get("version") != 1
                    or retry.get("recovery") != repair_permission["failed_correction_recovery"]
                    or any(retry.get(key) != repair_permission.get(key) for key in (
                        "source_response_sha256", "source_candidate_sha256", "source_gate_sha256",
                    ))
                    or record["model_corrections_used"] != 1 or record["execution_reruns_used"] != 1):
                raise ValueError("reserved retry has no authenticated recovery permission")
            return certify_reserved_retry(
                path=retry_path, certifier=certifier, persona=args[0], candidate_path=args[1], **kwargs,
            )
        if needs_execution_retry:
            record["execution_reruns_used"] = 1
            _save_review_progress(
                path=review_dir / "progress.json",
                result={**progress, "execution_reruns_used": 1,
                        "model_corrections_used": record["model_corrections_used"]}, record=record,
            )
        return certifier(*args, **kwargs)

    if status == "correction_pending":
        decision = FinalTraceDecision.model_validate(progress["initial_final_review"])
        if record.get("request") is None:
            raise ValueError("a stopped correction needs its authenticated review request")
        source_review = Path(record["request"]).parent
        _copy_authenticated_tree(source_review, review_dir, files, model_caches=True)
        previous_corrections = record["model_corrections_used"]
        if decision.part_to_correct != "execution":
            record["model_corrections_used"] = 1
            _save_review_progress(
                path=review_dir / "progress.json",
                result={**progress, "model_corrections_used": 1}, record=record,
            )
        if not isinstance(task, ApprovedIdea) and decision.part_to_correct in {"grading_checks", "execution"}:
            candidate, gate, correction = _correct_after_final_review(
                decision=decision, record=record, planned_task=task, context=context,
                config=config, test_id=test_id, review_dir=review_dir,
                design_client=client, query_client=client, certifier=resume_certifier,
                grading_corrector=grading_corrector, regrader=regrader,
            )
        else:
            candidate, gate, correction = _unified_final_review_correction(
                decision=decision, record=record, planned_task=task, context=context,
                config=config, test_id=test_id, review_dir=review_dir, client=client,
                certifier=resume_certifier, regrader=regrader,
            )
        if correction.get("model_correction_started") is False:
            record["model_corrections_used"] = previous_corrections
        candidate = candidate or (Path(correction["failed_candidate"]) if correction.get("failed_candidate") else None)
        gate = gate or (Path(correction["failed_gate"]) if correction.get("failed_gate") else None)
        if candidate is None or gate is None:
            result = {
                "id": test_id, "status": correction["status"], "correction": correction,
                "initial_final_review": decision.model_dump(mode="json"),
                "pending_operation": decision.part_to_correct,
                "candidate": str(record["candidate"]), "gate": str(record["gate"]),
                "model_corrections_used": record["model_corrections_used"],
                "execution_reruns_used": record["execution_reruns_used"],
            }
            _save_review_progress(path=review_dir / "result.json", result=result, record=record)
            return None, result, result
        record.update(candidate=candidate, gate=gate, decision=None)
    if record.get("gate") is not None:
        saved = _authenticated_saved_gate(test_id=test_id, candidate=record["candidate"], gate=record["gate"])
        record["certification_valid"] = saved["result"]["valid"]
        authoring = {
            "id": test_id, "status": "reused_saved_authoring",
            "fact_ids": list(task.fact_ids), "candidate": str(record["candidate"]),
            "gate": str(record["gate"]), "model_corrections_used": record["model_corrections_used"],
        }
    else:
        raw = json.loads(response_path.read_text()) if response_path is not None else None
        payload = design_payload(config=config, context=context, planned_task=task)
        old_attempt = progress.get("attempt_name", "initial")
        source_proposal = response_path.parent if response_path is not None else None
        if raw is None:
            cached = cached_authoring_response(files, old_attempt)
            if cached is not None:
                raw, source_proposal = cached
                _copy_authenticated_tree(source_proposal, proposal, files)
        if source_proposal is None:
            source_proposal = next(
                (Path(value).parent for value in files
                 if Path(value).name == f"{old_attempt}_authoring_request.json"), None,
            )
        if source_proposal is not None:
            old_request = source_proposal / f"{old_attempt}_authoring_request.json"
            if str(old_request.resolve()) in files:
                payload = json.loads(old_request.read_text())
        prior_response = prior_request = None
        if source_proposal is not None:
            preflight_dir = source_proposal / f"{old_attempt}_preflight"
            source_response = preflight_dir / "response.json"
            source_request = preflight_dir / "request.json"
            if all(str(path.resolve()) in files for path in (source_response, source_request)):
                prior_response = json.loads(source_response.read_text())
                prior_request = json.loads(source_request.read_text())
                _copy_authenticated_tree(preflight_dir / "grading_examples", proposal / "initial_preflight" / "grading_examples", files)
        approved = record.get("approved_writer_correction")
        if exact_repair is not None:
            from authoring.complete_test import encode_writer_response
            revised = apply_exact_edits(raw, exact_repair)
            payload = {
                "original_authoring_input": design_payload(config=config, context=context, planned_task=task),
                "approved_exact_repair": exact_repair.model_dump(mode="json"),
            }
            if prior_gate is not None:
                payload["saved_execution_gate"] = {"file": prior_gate.name, "sha256": _sha256(prior_gate)}
            raw = encode_writer_response(revised)
            prior_response = prior_request = None
        if exact_edit is not None:
            from authoring.complete_test import encode_writer_response, parse_writer_response
            previous = parse_writer_response(raw)
            revised = previous.model_copy(deep=True)
            edits = {item["check_id"]: item for item in exact_edit["criterion_replacements"]}
            for check in revised.test.checks:
                if check.assertion["check_id"] in edits:
                    check.assertion["criterion"] = edits[check.assertion["check_id"]]["criterion"]
            revised.quality_audit = None
            payload = {
                "original_authoring_input": payload.get("original_authoring_input", payload),
                "previous_authoring": raw,
                "allowed_correction_fields": ["test.checks"],
                "protected_check_ids": [check.assertion["check_id"] for check in previous.test.checks
                                        if check.assertion["check_id"] not in edits],
                "approved_criterion_correction": exact_edit,
                "saved_execution_gate": {"file": prior_gate.name, "sha256": _sha256(prior_gate)},
            }
            raw = encode_writer_response(revised)
            prior_response = prior_request = None
        if approved is not None:
            if raw is None:
                raise ValueError("approved writer correction lost its saved draft")
            payload = {
                "original_authoring_input": payload,
                "previous_authoring": raw,
                "allowed_correction_fields": approved["allowed_correction_fields"],
                "protected_check_ids": approved["protected_check_ids"],
                "failure_to_fix": {
                    "specific_problem": approved["specific_problem"],
                    "required_correction": approved["required_correction"],
                },
            }
            raw = prior_response = prior_request = None
            record["model_corrections_used"] = 1
        needs_execution_retry = needs_execution_retry or any(
            issue.get("part") == "execution"
            for issue in (payload.get("failure_to_fix") or {}).get("issues", [])
        )
        authoring = _create_one_test(
            directory=proposal, accepted_dir=out / "candidates", payload=payload,
            planned_task=task, context=context, config=config, test_id=test_id,
            client=client, certifier=resume_certifier, model_corrections_used=record["model_corrections_used"],
            reuse_authoring=raw, reuse_preflight_response=prior_response,
            reuse_preflight_request=prior_request,
            allow_model_correction=not bool(overrides.get("exact_repair")) and not record.get("stop_after_preflight", False),
            regrader=regrader,
            stop_after_preflight=record.get("stop_after_preflight", False),
        )
        attempt = authoring["attempt_name"]
        authored_path = proposal / f"{attempt}_authoring_response.json"
        if authored_path.is_file():
            authoring["authoring_response"] = str(authored_path)
        authoring["execution_reruns_used"] = record["execution_reruns_used"]
        artifacts = authoring_artifacts(proposal, attempt)
        artifacts.extend(Path(authoring[key]) for key in ("candidate", "gate") if authoring.get(key))
        authoring = save_progress(
            path=proposal / "status.json", result=authoring,
            inputs_path=record["run_dir"] / "run_inputs.json", artifact_paths=artifacts,
        )
        if authoring["status"] != "review_required" and not authoring["status"].startswith("certified_"):
            return None, authoring, None
        record.update(
            candidate=Path(authoring["candidate"]), gate=Path(authoring["gate"]),
            authoring_response=authoring.get("authoring_response"),
            model_corrections_used=authoring["model_corrections_used"],
            first_pass=authoring["model_corrections_used"] == 0,
            certification_valid=authoring["status"].startswith("certified_"),
        )
    saved_decision = record.get("decision")
    def review_or_reuse(**kwargs: Any) -> FinalTraceDecision:
        nonlocal saved_decision
        if saved_decision is not None:
            decision, saved_decision = saved_decision, None
            return decision
        return reviewer(**kwargs)
    accepted, results = _review_four_run_candidates(
        reviewable={test_id: record}, tasks_by_id={test_id: task}, context=context,
        config=config, out=out, concurrency=1, design_client=client, query_client=client,
        certifier=certifier, grading_corrector=grading_corrector, regrader=regrader,
        reviewer=review_or_reuse,
    )
    return accepted.get(test_id), authoring, results[0]


def resume_new_test_run(
    *,
    config_path: Path,
    plan_path: Path,
    source_run: Path,
    out: Path,
    test_ids: list[int],
    concurrency: int,
    confirm_paid_calls: bool = False,
    grading_corrector: Any = _default_grading_corrector,
    regrader: Any = _default_regrader,
    certifier: Any = certify_candidate,
    reviewer: Any = _review_one,
    approved_corrections_path: Path | None = None,
    stop_after_preflight: bool = False,
    runtime_policy_path: Path | None = None,
) -> dict[str, Any]:
    """Resume one saved new-test run without rerunning authoring or Hermes."""
    if runtime_policy_path is not None:
        if approved_corrections_path is not None or stop_after_preflight:
            raise ValueError("staged creation cannot combine exact corrections or stop-after-preflight")
        from authoring.staged_creation import resume_staged
        return resume_staged(config_path=config_path, plan_path=plan_path, source_run=source_run,
                             out=out, test_ids=test_ids, policy_path=runtime_policy_path,
                             confirm_paid_calls=confirm_paid_calls)
    if not confirm_paid_calls:
        raise ValueError("refusing new-test resume calls without explicit confirmation")
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"new-test resume output directory is not empty: {out}")
    if not 1 <= concurrency <= 40:
        raise ValueError("concurrency must be between one and forty")
    source_run = source_run.resolve()
    if not test_ids or len(test_ids) != len(set(test_ids)) or any(
        not isinstance(test_id, int) or isinstance(test_id, bool) or test_id <= 0 for test_id in test_ids
    ):
        raise ValueError("resume test IDs must be unique positive integers")
    out_resolved = out.resolve()
    if out_resolved == source_run or out_resolved.is_relative_to(source_run):
        raise ValueError("the resume output must be a fresh directory outside the source run")

    config = load_config(config_path)
    context = load_checkpoint_context(config)
    tasks = load_authoring_tasks(context=context, config=config, path=plan_path)
    current_progress = _progress_resume_inputs(
        config_path=config_path, plan_path=plan_path, source_run=source_run,
        test_ids=list(test_ids), config=config, context=context, tasks=tasks,
        allow_approved_correction=approved_corrections_path is not None,
    ) if approved_corrections_path is None or all(isinstance(task, ApprovedIdea) for task in tasks) else None
    if current_progress is not None:
        records, tasks_by_id, source_identity = current_progress
        if approved_corrections_path is not None:
            _attach_approved_v4_corrections(
                path=approved_corrections_path, records=records, identity=source_identity,
                context=context, tasks_by_id=tasks_by_id, config=config,
            )
    elif approved_corrections_path is not None:
        records, tasks_by_id, source_identity = _approved_new_test_resume_inputs(
            config_path=config_path,
            plan_path=plan_path,
            source_run=source_run,
            corrections_path=approved_corrections_path,
            test_ids=list(test_ids),
            config=config,
            context=context,
            tasks=tasks,
            out=out,
        )
    elif not (source_run / "manifest.json").is_file():
        records, tasks_by_id, source_identity = _incomplete_final_review_resume_inputs(
            config_path=config_path,
            plan_path=plan_path,
            source_run=source_run,
            test_ids=list(test_ids),
            config=config,
            context=context,
            tasks=tasks,
        )
    else:
        local_resume = _local_validation_resume_inputs(
            config_path=config_path,
            plan_path=plan_path,
            source_run=source_run,
            test_ids=list(test_ids),
            config=config,
            context=context,
            tasks=tasks,
        )
        if local_resume is None:
            records, tasks_by_id, source_identity = _saved_new_test_resume_inputs(
                config_path=config_path,
                plan_path=plan_path,
                source_run=source_run,
                test_ids=list(test_ids),
                config=config,
                context=context,
                tasks=tasks,
            )
        else:
            records, tasks_by_id, source_identity = local_resume

    if stop_after_preflight:
        if current_progress is None or approved_corrections_path is not None:
            raise ValueError("--stop-after-preflight requires a saved version-4 draft without new corrections")
        for test_id, record in records.items():
            if (not isinstance(tasks_by_id[test_id], ApprovedIdea) or record.get("gate") is not None
                    or not record.get("progress", {}).get("authoring_response")):
                raise ValueError("--stop-after-preflight requires a written draft with no certification")
            record["stop_after_preflight"] = True
        source_identity["stop_after_preflight"] = True
    out.mkdir(parents=True, exist_ok=True)
    dump_json(out / "resume_inputs.json", source_identity)
    if current_progress is not None:
        dump_json(out / "authoring" / "part_01" / "run_inputs.json", {
            "config_path": str(config_path.resolve()), "config_sha256": _sha256(config_path),
            "plan_path": str(plan_path.resolve()), "plan_sha256": _sha256(plan_path),
            "checkpoint_identity": context.checkpoint_identity, "persona": config.persona,
            "evaluation_date": config.evaluation_date,
            "selected_test_ids": list(test_ids),
            "selected_plan_positions": [records[test_id]["progress"]["plan_position"] for test_id in test_ids],
            "test_overrides": {str(test_id): records[test_id].get("test_overrides") or {} for test_id in test_ids},
        })
    ledger = out / "final_trace_reviews" / "call_ledger.jsonl"
    previous_ledger = os.environ.get("DOLPHINBENCH_CALL_LEDGER")
    previous_run_id = os.environ.get("DOLPHINBENCH_CONSTRUCTION_RUN_ID")
    os.environ["DOLPHINBENCH_CALL_LEDGER"] = str(ledger)
    os.environ["DOLPHINBENCH_CONSTRUCTION_RUN_ID"] = out.name
    accepted: dict[int, Path] = {}
    final_review_results: list[dict[str, Any]] = []
    authoring_results: list[dict[str, Any]] = []

    def resume_one(test_id: int) -> tuple[dict[int, Path], list[dict[str, Any]], list[dict[str, Any]]]:
        accepted: dict[int, Path] = {}
        authoring_results: list[dict[str, Any]] = []
        final_review_results: list[dict[str, Any]] = []
        record = records[test_id]
        proposal_dir = out / "authoring" / "part_01" / "proposals" / f"{test_id:03d}"
        proposal_dir.mkdir(parents=True, exist_ok=True)
        record["run_dir"] = out / "authoring" / "part_01"
        review_dir = out / "final_trace_reviews" / f"{test_id:03d}"
        review_dir.mkdir(parents=True, exist_ok=True)
        if record.get("resume_kind") == "saved_stage":
            try:
                candidate, authoring, final = _continue_saved_stage(
                    test_id=test_id, record=record, task=tasks_by_id[test_id],
                    config=config, context=context, out=out, certifier=certifier,
                    reviewer=reviewer, grading_corrector=grading_corrector, regrader=regrader,
                )
            except Exception as exc:
                candidate = None
                authoring = {
                    "id": test_id, "status": "resume_pending", "reason": f"{type(exc).__name__}: {exc}",
                    "model_corrections_used": record["model_corrections_used"],
                    "execution_reruns_used": record["execution_reruns_used"],
                }
                final = None
                dump_json(proposal_dir / "resume_error.json", authoring)
            authoring_results.append(authoring)
            if final is not None:
                final_review_results.append(final)
            if candidate is not None:
                accepted[test_id] = candidate
            return accepted, authoring_results, final_review_results
        approved_correction = record.get("approved_correction")
        if approved_correction is not None:
            candidate = proposal_dir / "approved_grading_correction_candidate.yaml"
            _apply_approved_correction(
                source=record["candidate"],
                destination=candidate,
                correction=approved_correction,
            )
            regrade_path = proposal_dir / "approved_grading_correction_regrade.json"
            regraded = _regraded_gate(
                candidate_path=candidate,
                saved_gate_path=record["gate"],
                regrader=regrader,
            )
            dump_json(regrade_path, regraded)
            result: dict[str, Any] = {
                "id": test_id,
                "source_run": str(source_run),
                "approved_correction": copy.deepcopy(approved_correction),
                "regrade": str(regrade_path.resolve()),
            }
            regraded_result = regraded["result"]
            if (
                regraded_result.get("g1_pass_count") != 2
                or regraded_result.get("g2_pass_count") != 0
            ):
                result["status"] = "unresolved_after_approved_correction"
            else:
                final = reviewer(
                    test_id=test_id,
                    candidate=candidate,
                    gate=regrade_path,
                    planned_task=tasks_by_id[test_id],
                    context=context,
                    out=review_dir,
                    model=config.designer_model,
                )
                result["final_review"] = final.model_dump(mode="json")
                if final.accept:
                    accepted[test_id] = candidate
                    result["status"] = "accepted_after_final_review_correction"
                else:
                    result["status"] = "unresolved_after_approved_correction"
            final_review_results.append(result)
            authoring_results.append(
                {
                    "id": test_id,
                    "fact_ids": list(tasks_by_id[test_id].fact_ids),
                    "status": "reused_saved_authoring",
                    "candidate": str(candidate.resolve()),
                    "gate": str(regrade_path.resolve()),
                    "source_run": str(source_run),
                }
            )
            return accepted, authoring_results, final_review_results
        if record.get("resume_kind") == "rerun_candidate_after_shared_validator_fix":
            candidate = _materialize_saved_authored_candidate(
                record=record,
                planned_task=tasks_by_id[test_id],
                context=context,
                config=config,
                test_id=test_id,
            )
            gate = proposal_dir / "initial_gate.json"
            gate_result = _certify_cached(
                persona=config.persona,
                checkpoint=context.checkpoint_path,
                candidate_path=candidate,
                result_path=gate,
                certifier=certifier,
            )
            record["candidate"] = candidate
            record["gate"] = gate
            record["certification_valid"] = bool(gate_result.get("valid"))
        if record.get("request") is not None:
            shutil.copy2(record["request"], review_dir / "request.json")
        decision = record.get("decision")
        result: dict[str, Any] = {
            "id": test_id,
            "source_run": str(source_run),
        }
        if decision is None:
            decision = reviewer(
                test_id=test_id,
                candidate=record["candidate"],
                gate=record["gate"],
                planned_task=tasks_by_id[test_id],
                context=context,
                out=review_dir / "initial_final_review",
                model=config.designer_model,
            )
            result["initial_final_review"] = decision.model_dump(mode="json")
        else:
            result["initial_final_review"] = decision.model_dump(mode="json")
        if decision.accept:
            if record.get("certification_valid") is False:
                raise ValueError(
                    f"reviewer accepted test {test_id:03d} even though certification failed"
                )
            accepted[test_id] = record["candidate"]
            result["status"] = "accepted_initial_final_review"
            final_review_results.append(result)
            authoring_results.append(
                {
                    "id": test_id,
                    "fact_ids": list(tasks_by_id[test_id].fact_ids),
                    "status": "reused_saved_authoring",
                    "candidate": str(record["candidate"]),
                    "gate": str(record["gate"]),
                    "source_run": str(source_run),
                }
            )
            return accepted, authoring_results, final_review_results
        if record.get("authoring_mode") == "unified" and decision.part_to_correct not in {
            "grading_checks",
            "execution",
        }:
            candidate, corrected_gate, correction = _unified_final_review_correction(
                decision=decision,
                record=record,
                planned_task=tasks_by_id[test_id],
                context=context,
                config=config,
                test_id=test_id,
                review_dir=review_dir,
                client=AzureJsonClient(model=config.designer_model, reasoning_effort="high"),
                certifier=certifier,
            )
        else:
            candidate, corrected_gate, correction = _correct_after_final_review(
                decision=decision,
                record=record,
                planned_task=tasks_by_id[test_id],
                context=context,
                config=config,
                test_id=test_id,
                review_dir=review_dir,
                design_client=AzureJsonClient(
                    model=config.designer_model, reasoning_effort="high"
                ),
                query_client=AzureJsonClient(
                    model=config.query_model, reasoning_effort="high"
                ),
                certifier=certifier,
                grading_corrector=grading_corrector,
                regrader=regrader,
            )
        result["correction"] = correction
        if candidate is None or corrected_gate is None:
            result["status"] = "replacement_required_after_one_correction"
        else:
            second = reviewer(
                test_id=test_id,
                candidate=candidate,
                gate=corrected_gate,
                planned_task=tasks_by_id[test_id],
                context=context,
                out=review_dir / "correction_final_review",
                model=config.designer_model,
            )
            result["corrected_final_review"] = second.model_dump(mode="json")
            correction["corrected_final_review"] = second.model_dump(mode="json")
            dump_json(review_dir / "correction.json", correction)
            if second.accept:
                accepted[test_id] = candidate
                result["status"] = "accepted_after_final_review_correction"
            else:
                result["status"] = "replacement_required_after_one_correction"
        final_review_results.append(result)
        authoring_results.append(
            {
                "id": test_id,
                "fact_ids": list(tasks_by_id[test_id].fact_ids),
                "status": "reused_saved_authoring",
                "candidate": str(record["candidate"]),
                "gate": str(record["gate"]),
                "source_run": str(source_run),
            }
        )
        return accepted, authoring_results, final_review_results

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(resume_one, test_id): test_id for test_id in test_ids}
            for future in as_completed(futures):
                completed, authored, reviewed = future.result()
                accepted.update(completed)
                authoring_results.extend(authored)
                final_review_results.extend(reviewed)
        authoring_results.sort(key=lambda row: row["id"])
        final_review_results.sort(key=lambda row: row["id"])
        _verify_saved_resume_files(source_identity)
    finally:
        if previous_ledger is None:
            os.environ.pop("DOLPHINBENCH_CALL_LEDGER", None)
        else:
            os.environ["DOLPHINBENCH_CALL_LEDGER"] = previous_ledger
        if previous_run_id is None:
            os.environ.pop("DOLPHINBENCH_CONSTRUCTION_RUN_ID", None)
        else:
            os.environ["DOLPHINBENCH_CONSTRUCTION_RUN_ID"] = previous_run_id

    manifest = {
        "checkpoint_identity": context.checkpoint_identity,
        "plan_path": str(plan_path.resolve()),
        "test_ids": list(test_ids),
        "candidate_count": len(test_ids),
        "concurrency": concurrency,
        "resume_new_test_run": source_identity,
        "approved_corrections": (
            str(approved_corrections_path.resolve())
            if approved_corrections_path is not None
            else None
        ),
        "approved_corrections_sha256": (
            _sha256(approved_corrections_path)
            if approved_corrections_path is not None
            else None
        ),
        "final_review_accepted": len(accepted),
        "authoring_results": authoring_results,
        "final_review_results": final_review_results,
        "promoted": False,
    }
    dump_json(out / "manifest.json", manifest)
    accepted_candidates, _ = write_completed_batch(
        out=out,
        resumed_from=source_identity,
        checkpoint_identity=context.checkpoint_identity,
        persona=context.persona, evaluation_date=config.evaluation_date,
        plan_path=plan_path,
        tasks_by_id=tasks_by_id,
        accepted=accepted,
        authoring_results=authoring_results,
        final_review_results=final_review_results,
    )
    manifest["accepted_candidates"] = accepted_candidates
    manifest["accepted_batch"] = str((out / "accepted_batch").resolve()) if accepted else None
    manifest["rejected_tests"] = str((out / "rejected_tests.json").resolve())
    dump_json(out / "manifest.json", manifest)
    if manifest["accepted_batch"] is not None:
        replacement = source_identity.get("replaces_accepted_batch")
        if replacement is not None:
            prior_ids = set(json.loads((Path(replacement["directory"]) / "provenance.json").read_text())["accepted_test_ids"])
            if not prior_ids.issubset(accepted):
                replacement = None
        record_accepted_batch(
            config=config,
            context=context,
            directory=Path(manifest["accepted_batch"]),
            **({"replaces_accepted_batch": replacement} if replacement is not None else {}),
        )
    return manifest


def create_tests(
    *,
    config_path: Path,
    plan_path: Path,
    out: Path,
    start_id: int | None,
    test_ids: list[int] | None = None,
    concurrency: int,
    confirm_paid_calls: bool = False,
    runtime_policy_path: Path | None = None,
) -> dict[str, Any]:
    if not confirm_paid_calls:
        raise ValueError("refusing test-creation calls without explicit confirmation")
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"test-creation output directory is not empty: {out}")
    if not 1 <= concurrency <= 40:
        raise ValueError("concurrency must be between one and forty")
    config = load_config(config_path)
    context = load_checkpoint_context(config)
    tasks = load_authoring_tasks(context=context, config=config, path=plan_path)
    if any(not isinstance(task, ApprovedIdea) for task in tasks):
        raise ValueError("new test creation requires a version-4 approved idea; old plans remain records for their existing work")
    if not 1 <= len(tasks) <= 250:
        raise ValueError("test creation requires one through 250 planned tasks")
    if (start_id is None) == (test_ids is None):
        raise ValueError("provide exactly one of --start-id or --test-ids")
    if test_ids is not None:
        test_ids = list(test_ids)
        if len(test_ids) != len(tasks):
            raise ValueError("--test-ids must contain exactly one ID for every task in the plan")
        if any(not isinstance(test_id, int) or isinstance(test_id, bool) for test_id in test_ids):
            raise ValueError("--test-ids must contain integers")
        if any(test_id <= 0 for test_id in test_ids):
            raise ValueError("--test-ids must be positive")
        if len(test_ids) != len(set(test_ids)):
            raise ValueError("--test-ids must be unique")
        resolved_test_ids = test_ids
    else:
        assert start_id is not None
        if start_id <= 0:
            raise ValueError("start_id must be positive")
        resolved_test_ids = list(range(start_id, start_id + len(tasks)))
    tasks_by_id = dict(zip(resolved_test_ids, tasks, strict=True))
    if runtime_policy_path is not None:
        from authoring.staged_creation import resume_staged
        return resume_staged(config_path=config_path, plan_path=plan_path, source_run=None,
                             out=out, test_ids=resolved_test_ids, policy_path=runtime_policy_path,
                             confirm_paid_calls=confirm_paid_calls)
    record_approved_plan(config=config, context=context, plan_path=plan_path)
    out.mkdir(parents=True, exist_ok=True)
    dump_json(out / "run_inputs.json", {
        "config_path": str(config_path.resolve()), "config_sha256": _sha256(config_path),
        "plan_path": str(plan_path.resolve()), "plan_sha256": _sha256(plan_path),
        "checkpoint_identity": context.checkpoint_identity, "persona": config.persona,
        "evaluation_date": config.evaluation_date,
        "selected_test_ids": resolved_test_ids,
        "selected_plan_positions": list(range(1, len(tasks) + 1)),
    })
    run_dirs = _run_authoring_parts(
        config_path=config_path,
        plan_path=plan_path,
        out=out,
        start_id=start_id,
        test_ids=test_ids,
        count=len(tasks),
        concurrency=concurrency,
    )
    reviewable, authoring_results = _reviewable_outputs(
        run_dirs=run_dirs, expected_test_ids=resolved_test_ids
    )
    ledger = out / "final_trace_reviews" / "call_ledger.jsonl"
    previous_ledger = os.environ.get("DOLPHINBENCH_CALL_LEDGER")
    previous_run_id = os.environ.get("DOLPHINBENCH_CONSTRUCTION_RUN_ID")
    os.environ["DOLPHINBENCH_CALL_LEDGER"] = str(ledger)
    os.environ["DOLPHINBENCH_CONSTRUCTION_RUN_ID"] = out.name
    try:
        accepted, final_review_results = _review_four_run_candidates(
            reviewable=reviewable,
            tasks_by_id=tasks_by_id,
            context=context,
            config=config,
            out=out,
            concurrency=concurrency,
            design_client=AzureJsonClient(
                model=config.designer_model, reasoning_effort="high"
            ),
            query_client=AzureJsonClient(
                model=config.query_model, reasoning_effort="high"
            ),
            certifier=certify_candidate,
        )
    finally:
        if previous_ledger is None:
            os.environ.pop("DOLPHINBENCH_CALL_LEDGER", None)
        else:
            os.environ["DOLPHINBENCH_CALL_LEDGER"] = previous_ledger
        if previous_run_id is None:
            os.environ.pop("DOLPHINBENCH_CONSTRUCTION_RUN_ID", None)
        else:
            os.environ["DOLPHINBENCH_CONSTRUCTION_RUN_ID"] = previous_run_id

    manifest = {
        "checkpoint_identity": context.checkpoint_identity,
        "plan_path": str(plan_path.resolve()),
        "start_id": start_id,
        "test_ids": resolved_test_ids,
        "candidate_count": len(tasks),
        "concurrency": concurrency,
        "first_pass_certified": sum(
            1
            for record in reviewable.values()
            if record["first_pass"] and record.get("certification_valid") is not False
        ),
        "certified_after_one_design_correction": sum(
            1
            for record in reviewable.values()
            if not record["first_pass"] and record.get("certification_valid") is not False
        ),
        "failed_certifications_reviewed": sum(
            1
            for result in final_review_results
            if reviewable.get(result["id"], {}).get("certification_valid") is False
            and result.get("initial_final_review")
        ),
        "accepted_clean_certification": sum(
            1
            for result in final_review_results
            if result["status"] == "accepted_clean_certification"
        ),
        "accepted_initial_final_review": sum(
            1
            for result in final_review_results
            if result["status"] == "accepted_initial_final_review"
        ),
        "accepted_after_final_review_correction": sum(
            1
            for result in final_review_results
            if result["status"] == "accepted_after_final_review_correction"
        ),
        "accepted_after_same_facts_redesign": sum(
            1
            for result in final_review_results
            if result["status"] == "accepted_after_same_facts_redesign"
        ),
        "replacement_required_after_final_review": sum(
            1
            for result in final_review_results
            if result["status"] == "replacement_required"
        ),
        "unresolved_after_same_facts_redesign": sum(
            1
            for result in final_review_results
            if result["status"] == "unresolved_after_same_facts_redesign"
        ),
        "final_review_accepted": len(accepted),
        "authoring_results": authoring_results,
        "final_review_results": final_review_results,
        "promoted": False,
    }
    dump_json(out / "manifest.json", manifest)
    accepted_candidates, _ = write_completed_batch(
        out=out,
        checkpoint_identity=context.checkpoint_identity,
        persona=context.persona, evaluation_date=config.evaluation_date,
        plan_path=plan_path,
        tasks_by_id=tasks_by_id,
        accepted=accepted,
        authoring_results=authoring_results,
        final_review_results=final_review_results,
    )
    manifest["accepted_candidates"] = accepted_candidates
    manifest["accepted_batch"] = str((out / "accepted_batch").resolve()) if accepted else None
    manifest["rejected_tests"] = str((out / "rejected_tests.json").resolve())
    manifest["pending_tests"] = str((out / "pending_tests.json").resolve())
    manifest["pending_count"] = len(json.loads((out / "pending_tests.json").read_text())["pending_tests"])
    dump_json(out / "manifest.json", manifest)
    if manifest["accepted_batch"] is not None:
        record_accepted_batch(
            config=config,
            context=context,
            directory=Path(manifest["accepted_batch"]),
        )
    record_rejected_tests(
        config=config,
        context=context,
        rejected_path=out / "rejected_tests.json",
    )
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--prepare-release-revision", type=Path,
                        help="Locally stage an exact approved release proposal; no paid calls or acceptance.")
    parser.add_argument("--approved-proposal-sha256",
                        help="SHA-256 of the explicitly approved release-revision proposal.")
    parser.add_argument("--revision-review-context", type=Path,
                        help="Supply original checkpoint messages to a pending exact-revision review.")
    parser.add_argument("--prepare-only", action="store_true",
                        help="Prepare version-2 exact release validation locally; no paid calls or acceptance.")
    parser.add_argument(
        "--approved-revisions",
        type=Path,
        help=(
            "Certify exact hash-bound replacement YAMLs from an approved-revisions "
            "manifest for a historical plan without authoring calls."
        ),
    )
    parser.add_argument("--out", type=Path, required=True)
    ids = parser.add_mutually_exclusive_group()
    ids.add_argument("--start-id", type=int)
    ids.add_argument(
        "--test-ids",
        type=parse_test_ids,
        help=(
            "For --repair-published-tests, continue only these existing unresolved "
            "repair results from their latest saved reviewer decision."
        ),
    )
    parser.add_argument("--review-published-tests", type=Path)
    parser.add_argument("--repair-published-tests", type=Path)
    parser.add_argument(
        "--resume-new-test-run",
        type=Path,
        help=(
            "Resume selected unfinished tests from an unpublished run using saved "
            "progress. Requires --plan, --test-ids, and a fresh --out directory."
        ),
    )
    parser.add_argument("--review-dir", type=Path)
    parser.add_argument("--replacement-plan", type=Path)
    parser.add_argument(
        "--approved-corrections",
        type=Path,
        help=(
            "Apply hash-bound human-approved corrections: version-4 pending draft "
            "request/check corrections or exact criterion edits on resume, "
            "or historical repair corrections."
        ),
    )
    parser.add_argument("--evidence-root", type=Path, default=ROOT / "tmp" / "authoring")
    parser.add_argument("--expected-count", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--confirm-paid-calls", action="store_true")
    parser.add_argument("--runtime-policy", type=Path,
                        help="explicit staged-creation policy for unfinished version-4 work")
    parser.add_argument("--finalize-accepted-only", action="store_true",
                        help="Locally package completed clean acceptances from an interrupted run; no provider calls.")
    parser.add_argument("--reconcile-review-statuses", action="store_true",
                        help="Locally restore authenticated pending reviews misrecorded as rejected ideas; no provider calls.")
    parser.add_argument("--stop-after-preflight", action="store_true",
                        help="Resume a written version-4 draft through review/examples only; no writer or certification calls.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if getattr(args, "reconcile_review_statuses", False):
        if (args.resume_new_test_run is None or args.plan is None or args.test_ids is None
                or args.confirm_paid_calls or args.finalize_accepted_only or args.stop_after_preflight
                or args.runtime_policy is not None or args.prepare_only
                or any(getattr(args, name, None) is not None for name in (
                    "prepare_release_revision", "approved_revisions", "approved_corrections", "start_id",
                    "review_published_tests", "repair_published_tests", "review_dir", "replacement_plan",
                    "approved_proposal_sha256", "revision_review_context"))):
            raise ValueError("local status reconciliation requires plan, source run and test IDs, and forbids paid or other modes")
        from authoring.reconcile_review_status import reconcile_pending_reviews
        result = reconcile_pending_reviews(config_path=args.config, plan_path=args.plan,
            source_run=args.resume_new_test_run, out=args.out, test_ids=args.test_ids)
        print(json.dumps({"out": str(args.out), "pending": len(result["test_ids"]), "provider_calls": 0}, indent=2))
        return
    if getattr(args, "finalize_accepted_only", False):
        if (args.resume_new_test_run is None or args.plan is None or args.test_ids is None
                or args.confirm_paid_calls or args.stop_after_preflight or args.runtime_policy is not None
                or args.prepare_only or any(getattr(args, name, None) is not None for name in (
                    "prepare_release_revision", "approved_revisions", "approved_corrections", "start_id",
                    "review_published_tests", "repair_published_tests", "review_dir", "replacement_plan"))):
            raise ValueError("local finalization requires plan, source run and test IDs, and forbids paid or other modes")
        from authoring.finalize_batch import finalize_accepted
        result = finalize_accepted(config_path=args.config, plan_path=args.plan, source_run=args.resume_new_test_run,
                                   out=args.out, test_ids=args.test_ids)
        print(json.dumps({"out": str(args.out), "accepted": result["final_review_accepted"], "provider_calls": 0}, indent=2))
        return
    if args.runtime_policy is not None and args.resume_new_test_run is None and (
            args.plan is None or args.prepare_only or args.stop_after_preflight or any(
                value is not None for value in (args.prepare_release_revision, args.approved_revisions,
                    args.approved_corrections, args.review_published_tests, args.repair_published_tests,
                    args.review_dir, args.replacement_plan))):
        raise ValueError("--runtime-policy requires fresh version-4 creation or an unfinished version-4 resume")
    if args.revision_review_context is not None and (args.approved_revisions is None or args.prepare_only):
        raise ValueError("--revision-review-context requires exact-revision execution")
    if args.prepare_release_revision is not None:
        if (not args.approved_proposal_sha256 or args.confirm_paid_calls or args.stop_after_preflight or args.prepare_only
                or any(value is not None for value in (
                    args.plan, args.approved_revisions, args.start_id, args.test_ids,
                    args.review_published_tests, args.repair_published_tests,
                    args.resume_new_test_run, args.review_dir, args.replacement_plan,
                    args.approved_corrections,
                ))):
            raise ValueError("release preparation requires its approval hash and forbids paid/other modes")
        from authoring.release_revision import prepare_release_revision
        manifest = prepare_release_revision(
            config_path=args.config, proposal_path=args.prepare_release_revision,
            approved_sha256=args.approved_proposal_sha256, out=args.out,
        )
        print(json.dumps({"out": str(args.out), "status": manifest["status"],
                          **manifest["counts"]}, indent=2))
        return
    if args.approved_proposal_sha256 is not None:
        raise ValueError("--approved-proposal-sha256 requires --prepare-release-revision")
    if args.prepare_only and (args.approved_revisions is None or args.confirm_paid_calls):
        raise ValueError("--prepare-only requires --approved-revisions and forbids paid-call confirmation")
    if args.stop_after_preflight and args.resume_new_test_run is None:
        raise ValueError("--stop-after-preflight requires --resume-new-test-run")
    if args.approved_revisions is not None:
        if any(
            value is not None
            for value in (
                args.plan,
                args.start_id,
                args.test_ids,
                args.review_published_tests,
                args.repair_published_tests,
                args.resume_new_test_run,
                args.review_dir,
                args.replacement_plan,
                args.approved_corrections,
            )
        ):
            raise ValueError(
                "--approved-revisions cannot be combined with another create_tests mode"
            )
        approval = json.loads(args.approved_revisions.read_text())
        if approval.get("version") == 3 and approval.get("kind") == "exact_release_revision_followup":
            if args.revision_review_context is not None:
                raise ValueError("follow-up review context is inherited from its bound parent")
            from authoring.revision_followup import run_followup
            manifest = run_followup(
                config_path=args.config, approval_path=args.approved_revisions, out=args.out,
                prepare_only=args.prepare_only, confirm_paid_calls=args.confirm_paid_calls,
                concurrency=args.concurrency)
            print(json.dumps({"out": str(args.out), "status": manifest["status"],
                              "accepted": manifest["accepted_count"]}, indent=2))
            return
        if args.prepare_only:
            from authoring.revision_validation import prepare_release_validation
            manifest = prepare_release_validation(
                config_path=args.config, approval_path=args.approved_revisions, out=args.out)
            print(json.dumps({"out": str(args.out), "status": manifest["status"],
                              **manifest["planned_work"]}, indent=2))
            return
        if approval.get("version") == 2:
            from authoring.revision_validation import execute_release_validation
            manifest = execute_release_validation(
                config_path=args.config, approval_path=args.approved_revisions, out=args.out,
                concurrency=args.concurrency, confirm_paid_calls=args.confirm_paid_calls,
                review_context_path=args.revision_review_context)
            print(json.dumps({"out": str(args.out), "status": manifest["status"],
                              "accepted": manifest["accepted_count"]}, indent=2))
            return
        if args.revision_review_context is not None:
            raise ValueError("review context requires a version-2 exact release revision")
        manifest = create_approved_revisions(
            config_path=args.config,
            approved_revisions_path=args.approved_revisions,
            out=args.out,
            concurrency=args.concurrency,
            confirm_paid_calls=args.confirm_paid_calls,
        )
        print(
            json.dumps(
                {
                    "out": str(args.out),
                    "certified": sum(
                        bool(row.get("certification_valid"))
                        for row in manifest["certification_results"]
                    ),
                    "accepted": manifest["final_review_accepted"],
                },
                indent=2,
            )
        )
        return
    if args.resume_new_test_run is not None:
        if args.review_published_tests is not None or args.repair_published_tests is not None:
            raise ValueError(
                "--resume-new-test-run cannot be combined with published-test modes"
            )
        if args.plan is None or args.test_ids is None or args.start_id is not None:
            raise ValueError(
                "--resume-new-test-run requires --plan and --test-ids and forbids --start-id"
            )
        if args.review_dir is not None or args.replacement_plan is not None:
            raise ValueError(
                "--resume-new-test-run cannot use published-test repair inputs"
            )
        manifest = resume_new_test_run(
            config_path=args.config,
            plan_path=args.plan,
            source_run=args.resume_new_test_run,
            out=args.out,
            test_ids=args.test_ids,
            concurrency=args.concurrency,
            confirm_paid_calls=args.confirm_paid_calls,
            approved_corrections_path=args.approved_corrections,
            stop_after_preflight=args.stop_after_preflight,
            runtime_policy_path=args.runtime_policy,
        )
        print(
            json.dumps(
                {
                    "out": str(args.out),
                    "accepted": manifest["final_review_accepted"],
                    "unresolved": len(args.test_ids) - manifest["final_review_accepted"],
                },
                indent=2,
            )
        )
        return
    if args.repair_published_tests is not None:
        if args.plan is not None or args.start_id is not None:
            raise ValueError(
                "--repair-published-tests cannot be combined with --plan or --start-id"
            )
        if args.review_dir is None:
            raise ValueError("--repair-published-tests requires --review-dir")
        if not args.confirm_paid_calls:
            raise ValueError("refusing published-test repair without explicit confirmation")
        verify_gate_python()
        manifest = repair_published_tests(
            config_path=args.config,
            tests_dir=args.repair_published_tests,
            review_dir=args.review_dir,
            evidence_root=args.evidence_root,
            out=args.out,
            concurrency=args.concurrency,
            replacement_plan=args.replacement_plan,
            expected_count=args.expected_count,
            confirm_paid_calls=args.confirm_paid_calls,
            continuation_test_ids=args.test_ids,
            approved_corrections_path=args.approved_corrections,
        )
        print(
            json.dumps(
                {
                    "out": str(args.out),
                    "accepted": manifest["accepted"],
                    "unresolved": manifest["unresolved"],
                },
                indent=2,
            )
        )
        return
    if args.review_published_tests is not None:
        if args.approved_corrections is not None:
            raise ValueError("--approved-corrections is available only in repair mode")
        if args.plan is not None or args.start_id is not None:
            raise ValueError(
                "--review-published-tests cannot be combined with --plan or --start-id"
            )
        manifest = review_published_tests(
            config_path=args.config,
            tests_dir=args.review_published_tests,
            evidence_root=args.evidence_root,
            out=args.out,
            concurrency=args.concurrency,
            expected_count=args.expected_count,
            confirm_paid_calls=args.confirm_paid_calls,
            selected_test_ids=args.test_ids,
        )
        print(
            json.dumps(
                {
                    "out": str(args.out),
                    "reviewed": manifest["test_count"],
                    "accepted": manifest["accepted"],
                    "rejected": manifest["rejected"],
                },
                indent=2,
            )
        )
        return
    if args.approved_corrections is not None:
        raise ValueError("--approved-corrections is available only in repair mode")
    if args.plan is None or (args.start_id is None) == (args.test_ids is None):
        raise ValueError(
            "test creation requires --plan and exactly one of --start-id or --test-ids"
        )
    manifest = create_tests(
        config_path=args.config,
        plan_path=args.plan,
        out=args.out,
        start_id=args.start_id,
        test_ids=args.test_ids,
        concurrency=args.concurrency,
        confirm_paid_calls=args.confirm_paid_calls,
        runtime_policy_path=args.runtime_policy,
    )
    summary = {"out": str(args.out), "accepted": manifest["final_review_accepted"]}
    if "certified_after_one_design_correction" in manifest:
        summary["certified"] = manifest["certified_after_one_design_correction"]
    else:
        summary["unresolved"] = manifest["candidate_count"] - manifest["final_review_accepted"]
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
