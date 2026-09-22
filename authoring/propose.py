"""Choose a small exact number of new DolphinBench test ideas from the canonical checkpoint."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Sequence

import yaml

from authoring.context import (
    CheckpointContext,
    _validate_tasks,
    dump_json,
    load_authoring_tasks,
    load_checkpoint_context,
    load_config,
    planner_action_inventory,
    planning_fact_subject_index,
    planning_fact_rows,
    planning_source_sessions,
)
from authoring.models import (
    ApprovedIdea,
    AuthoringConfig,
    CandidateTaskBatch,
    EvidenceDecisionBatch,
    IdeaResponse,
    ProposalBatch,
    RequiredResult,
    SituationProposalBatch,
    evidence_decision_batch_schema,
    proposal_batch_schema,
    situation_proposal_batch_schema,
    validate_proposal_count,
)
from authoring.prompts import IDEA_PLANNER_SYSTEM, evidence_planner_system, test_idea_planner_system
from authoring.input_rendering import render_evidence_input, render_planner_input
from construction.llm import AzureJsonClient
from construction.runtime_model_calls import cached_client_complete
from harness.environment import call_ledger as _call_ledger
from harness.task_schema import TestSpec
from harness.dataset import load_test


REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_PLANNER_SHARDS = 10
MAX_PLANNER_CONCURRENCY = 20


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_rejected_tests(paths: Sequence[Path]) -> list[dict[str, Any]]:
    """Load every prior rejected idea so the planner does not repeat it."""
    required_keys = {
        "test_id",
        "candidate_plan",
        "failure_reason",
        "candidate_plan_path",
        "failure_manifest_path",
    }
    normalized: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen_paths:
            raise ValueError(f"rejected tests file was supplied more than once: {path}")
        seen_paths.add(resolved)
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError as exc:
            raise ValueError(f"rejected tests file does not exist: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"rejected tests file is not valid JSON: {path}") from exc
        if not isinstance(raw, dict) or set(raw) != {"rejected_tests"}:
            raise ValueError("rejected tests file must contain only a rejected_tests array")
        rows = raw["rejected_tests"]
        if not isinstance(rows, list):
            raise ValueError("rejected_tests must be an array")

        seen_ids: set[int] = set()
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict) or set(row) != required_keys:
                raise ValueError(
                    f"rejected_tests[{index}] in {path} must contain exactly "
                    f"{sorted(required_keys)}"
                )
            test_id = row["test_id"]
            candidate_plan = row["candidate_plan"]
            text_values = {
                key: row[key]
                for key in (
                    "failure_reason",
                    "candidate_plan_path",
                    "failure_manifest_path",
                )
            }
            if (
                not isinstance(test_id, int)
                or isinstance(test_id, bool)
                or test_id <= 0
                or not isinstance(candidate_plan, dict)
                or not candidate_plan
                or any(
                    not isinstance(value, str) or not value.strip()
                    for value in text_values.values()
                )
            ):
                raise ValueError(
                    f"rejected_tests[{index}] in {path} has invalid test_id, "
                    "candidate_plan, or text"
                )
            if test_id in seen_ids:
                raise ValueError(f"rejected_tests in {path} contains duplicate test_id {test_id}")
            seen_ids.add(test_id)
            normalized.append(
                {
                    "test_id": test_id,
                    "candidate_plan": candidate_plan,
                    **{key: value.strip() for key, value in text_values.items()},
                }
            )
    return normalized


def _accepted_test_summaries(
    *,
    config: AuthoringConfig,
    context: CheckpointContext,
    accepted_batch_dirs: Sequence[Path],
    excluded_test_ids: Sequence[int] = (),
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    excluded = set(excluded_test_ids)
    for directory in accepted_batch_dirs:
        plan_path = directory / "planning_batch.json"
        candidate_dir = directory / "candidates"
        provenance_path = directory / "provenance.json"
        if not plan_path.is_file() or not candidate_dir.is_dir() or not provenance_path.is_file():
            raise ValueError(
                f"accepted batch needs planning_batch.json, candidates/, and provenance.json: {directory}"
            )
        plan_raw = json.loads(plan_path.read_text())
        if plan_raw.get("kind") == "exact_release_revision":
            from authoring.revision_batch import collect_revision_batch
            revised = collect_revision_batch(directory=directory, context=context, config=config)
            for test_id, path in sorted(revised.items()):
                if test_id in seen_ids:
                    raise ValueError(f"duplicate accepted test ID {test_id}")
                seen_ids.add(test_id)
                if test_id not in excluded:
                    candidate = TestSpec.model_validate(yaml.safe_load(path.read_text()))
                    summaries.append(_published_test_summary(candidate, test_id))
            continue
        batch = CandidateTaskBatch.model_validate(plan_raw)
        if batch.persona != context.persona:
            raise ValueError("candidate task batch persona does not match checkpoint persona")
        if batch.checkpoint_identity != context.checkpoint_identity:
            raise ValueError("candidate task batch checkpoint identity does not match checkpoint")
        if batch.evaluation_date != config.evaluation_date:
            raise ValueError("candidate task batch evaluation date does not match config")
        tasks = batch.tasks
        provenance = json.loads(provenance_path.read_text())
        from authoring.release_mapping import validate_mapped_batch
        validate_mapped_batch(directory=directory, plan=plan_raw, provenance=provenance)
        required_provenance_keys = {
            "checkpoint_identity",
            "accepted_test_ids",
            "source_manifest",
            "results",
        }
        allowed_provenance_keys = required_provenance_keys | {"resumed_from"}
        if (
            not isinstance(provenance, dict)
            or not required_provenance_keys.issubset(provenance)
            or not set(provenance).issubset(allowed_provenance_keys)
        ):
            raise ValueError(f"accepted batch has invalid provenance: {directory}")
        if "resumed_from" in provenance and not isinstance(
            provenance["resumed_from"], dict
        ):
            raise ValueError(
                f"accepted batch has invalid resumed_from provenance: {directory}"
            )
        if provenance["checkpoint_identity"] != context.checkpoint_identity:
            raise ValueError(f"accepted batch checkpoint does not match current checkpoint: {directory}")
        if not isinstance(provenance["source_manifest"], str) or not provenance["source_manifest"].strip():
            raise ValueError(f"accepted batch has no source manifest path: {directory}")
        accepted_ids = provenance["accepted_test_ids"]
        results = provenance["results"]
        if (
            not isinstance(accepted_ids, list)
            or not isinstance(results, list)
            or len(accepted_ids) != len(tasks)
            or len(results) != len(tasks)
            or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in accepted_ids)
            or len(accepted_ids) != len(set(accepted_ids))
        ):
            raise ValueError(f"accepted batch provenance does not cover its tasks: {directory}")
        results_by_id: dict[int, dict[str, Any]] = {}
        for result in results:
            if not isinstance(result, dict) or set(result) != {
                "test_id",
                "status",
                "candidate",
                "candidate_sha256",
            }:
                raise ValueError(f"accepted batch has malformed provenance result: {directory}")
            test_id = result["test_id"]
            if (
                not isinstance(test_id, int)
                or isinstance(test_id, bool)
                or test_id <= 0
                or result.get("status") not in {
                    "accepted_clean_certification",
                    "accepted_initial_final_review",
                    "accepted_after_final_review_correction",
                    "accepted_after_continuation_review",
                }
                or not isinstance(result.get("candidate"), str)
                or not result["candidate"].strip()
                or not isinstance(result.get("candidate_sha256"), str)
                or not result["candidate_sha256"].strip()
                or test_id in results_by_id
            ):
                raise ValueError(f"accepted batch has invalid provenance result: {directory}")
            results_by_id[test_id] = result
        if set(accepted_ids) != set(results_by_id):
            raise ValueError(f"accepted batch provenance IDs and results disagree: {directory}")
        _validate_tasks(
            context=context,
            config=config,
            tasks=[
                task
                for task_index, task in enumerate(tasks)
                if accepted_ids[task_index] not in excluded
            ],
        )
        paths = sorted(candidate_dir.glob("*.yaml"))
        if len(paths) != len(tasks):
            raise ValueError(
                f"accepted batch has {len(tasks)} planned tasks but {len(paths)} candidates: "
                f"{directory}"
            )
        for task_index, (task, path) in enumerate(zip(tasks, paths, strict=True)):
            candidate = TestSpec.model_validate(yaml.safe_load(path.read_text()) or {})
            test_id = int(candidate.id)
            if test_id in seen_ids:
                raise ValueError(f"duplicate accepted test ID {test_id}")
            if list(candidate.load_bearing_facts) != task.fact_ids:
                raise ValueError(
                    f"candidate {test_id} fact IDs do not match its accepted plan"
                )
            if test_id != accepted_ids[task_index]:
                raise ValueError(f"accepted batch task order does not match provenance: {directory}")
            if _sha256(path) != results_by_id[test_id]["candidate_sha256"]:
                raise ValueError(f"accepted candidate hash does not match provenance: {path}")
            seen_ids.add(test_id)
            if test_id in excluded:
                continue
            summaries.append(
                {
                    "test_id": test_id,
                    "fact_ids": list(task.fact_ids),
                    "requested_work": candidate.test,
                    "tool_names": list(candidate.expected_tool_calls),
                    "memory_dependent_results": [
                        result.result for result in task.required_results
                    ] if not isinstance(task, ApprovedIdea) else [task.history_needed],
                }
            )
    return sorted(summaries, key=lambda row: int(row["test_id"]))


def _default_release_manifest_path(config: AuthoringConfig) -> Path:
    return REPO_ROOT / "authoring" / "release_manifests" / f"{config.persona}.json"


def _published_test_summary(candidate: TestSpec, test_id: int) -> dict[str, Any]:
    assertions = (candidate.grade.config or {}).get("assertions")
    results = []
    if isinstance(assertions, list):
        results = [
            assertion["criterion"]
            for assertion in assertions
            if isinstance(assertion, dict)
            and isinstance(assertion.get("criterion"), str)
            and assertion["criterion"].strip()
        ]
    return {
        "test_id": test_id,
        "fact_ids": list(candidate.load_bearing_facts),
        "requested_work": candidate.test,
        "tool_names": list(candidate.expected_tool_calls),
        "memory_dependent_results": results,
    }


def _published_test_summaries(
    *,
    config: AuthoringConfig,
    context: CheckpointContext,
    replace_test_ids: Sequence[int],
    manifest_path: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load the published tests and verify every release hash before replacement planning."""
    ids = list(replace_test_ids)
    if len(ids) != len(set(ids)) or any(
        not isinstance(test_id, int) or isinstance(test_id, bool) or test_id <= 0
        for test_id in ids
    ):
        raise ValueError("replacement test IDs must be unique positive integers")

    manifest_path = (manifest_path or _default_release_manifest_path(config)).resolve()
    if not manifest_path.is_file():
        raise ValueError(f"published release manifest is missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"published release manifest is not valid JSON: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("published release manifest must be a JSON object")

    for key, expected in (
        ("persona", config.persona),
        ("checkpoint_identity", context.checkpoint_identity),
        ("evaluation_date", config.evaluation_date),
    ):
        if manifest.get(key) != expected:
            raise ValueError(f"published release manifest has wrong {key}")
    if manifest.get("published") is not True:
        raise ValueError("published release manifest is not marked published")

    test_count = manifest.get("test_count")
    test_ids = manifest.get("test_ids")
    hashes = manifest.get("published_sha256", manifest.get("candidate_sha256"))
    destination = manifest.get("destination")
    if (
        not isinstance(test_count, int)
        or isinstance(test_count, bool)
        or test_count <= 0
        or test_ids != list(range(1, test_count + 1))
        or not isinstance(hashes, dict)
        or not isinstance(destination, str)
        or not destination.strip()
    ):
        raise ValueError("published release manifest has invalid test IDs, hashes, or destination")
    expected_hash_keys = {f"{test_id:03d}" for test_id in test_ids}
    if set(hashes) != expected_hash_keys or any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in hashes.values()
    ):
        raise ValueError("published release manifest does not contain one SHA-256 hash per test")

    # Published hashes identify the files; an old checkout location is provenance.
    if Path(destination).parts[-2:] != ("tests", config.persona):
        raise ValueError("published release manifest points to a non-canonical tests directory")
    tests_dir = (REPO_ROOT / "tests" / config.persona).resolve()
    paths = sorted(tests_dir.glob("*.yaml")) if tests_dir.is_dir() else []
    expected_names = {f"{test_id:03d}.yaml" for test_id in test_ids}
    actual_names = {path.name for path in paths}
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise ValueError(
            "published test files do not match the release manifest: "
            f"missing={missing}, extra={extra}"
        )

    summaries = []
    for path in paths:
        name = path.stem
        test_id = int(name)
        if _sha256(path) != hashes[name]:
            raise ValueError(f"published test {name} changed after release")
        try:
            candidate = TestSpec.model_validate(load_test(path))
        except (OSError, yaml.YAMLError, ValueError) as exc:
            raise ValueError(f"published test {name} is invalid: {path}") from exc
        if int(candidate.id) != test_id:
            raise ValueError(f"published test {name} contains a duplicate or wrong test ID")
        summaries.append(_published_test_summary(candidate, test_id))

    if any(test_id not in test_ids for test_id in ids):
        raise ValueError(
            "replacement test IDs must name published tests: "
            f"{sorted(set(ids) - set(test_ids))}"
        )
    excluded = set(ids)
    return (
        [summary for summary in summaries if summary["test_id"] not in excluded],
        {
            "path": str(manifest_path),
            "sha256": _sha256(manifest_path),
            "test_count": test_count,
            "excluded_test_ids": sorted(ids),
        },
    )


def _merge_test_summaries(
    published: Sequence[dict[str, Any]],
    accepted: Sequence[dict[str, Any]],
    *,
    replace_test_ids: Sequence[int],
) -> list[dict[str, Any]]:
    summaries = list(published)
    seen_ids = {int(row["test_id"]) for row in summaries}
    replacement_ids = set(replace_test_ids)
    for row in accepted:
        test_id = int(row["test_id"])
        if test_id in seen_ids:
            raise ValueError(f"duplicate accepted test ID {test_id}")
        if replacement_ids and test_id not in replacement_ids:
            raise ValueError(
                f"accepted replacement test ID {test_id} was not named for replacement"
            )
        summaries.append(row)
        seen_ids.add(test_id)
    return sorted(summaries, key=lambda row: int(row["test_id"]))


def _approved_idea_summaries(
    *,
    config: AuthoringConfig,
    context: CheckpointContext,
    approved_plan_paths: Sequence[Path],
) -> list[dict[str, Any]]:
    """Load ideas approved by the user but not yet created as tests."""
    summaries: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    seen_tasks: set[str] = set()
    for path in approved_plan_paths:
        resolved = path.resolve()
        if resolved in seen_paths:
            raise ValueError(f"approved plan was supplied more than once: {path}")
        seen_paths.add(resolved)
        plan_tasks: set[str] = set()
        for task in load_authoring_tasks(context=context, config=config, path=path):
            identity = json.dumps(task.model_dump(mode="json"), sort_keys=True)
            if identity in plan_tasks:
                raise ValueError(f"approved plan contains the same idea more than once: {path}")
            plan_tasks.add(identity)
            if identity in seen_tasks:
                # A retry plan may repeat work from an earlier approval record.
                continue
            seen_tasks.add(identity)
            summaries.append(
                {
                    "fact_ids": list(task.fact_ids),
                    "requested_work": task.work if isinstance(task, ApprovedIdea) else task.requested_work or task.task_description or "",
                    "tool_names": list(task.expected_tools),
                    "memory_dependent_results": [
                        result.result for result in task.required_results
                    ] if not isinstance(task, ApprovedIdea) else [task.history_needed],
                }
            )
    return summaries


def _legacy_proposal_request(
    *,
    config: AuthoringConfig,
    context: CheckpointContext,
    accepted_batch_dirs: Sequence[Path],
    approved_plan_paths: Sequence[Path] = (),
    rejected_tests_paths: Sequence[Path] = (),
    replace_test_ids: Sequence[int] = (),
    count: int = 10,
) -> dict[str, Any]:
    count = validate_proposal_count(count)
    accepted_batch_tests = _accepted_test_summaries(
        config=config,
        context=context,
        accepted_batch_dirs=accepted_batch_dirs,
        excluded_test_ids=replace_test_ids,
    )
    published_tests: list[dict[str, Any]] = []
    if replace_test_ids:
        published_tests, _ = _published_test_summaries(
            config=config,
            context=context,
            replace_test_ids=replace_test_ids,
        )
    accepted_tests = _merge_test_summaries(
        published_tests,
        accepted_batch_tests,
        replace_test_ids=replace_test_ids,
    )
    approved_ideas = _approved_idea_summaries(
        config=config,
        context=context,
        approved_plan_paths=approved_plan_paths,
    )
    accepted_fact_ids = {
        int(fact_id)
        for test in accepted_tests
        for fact_id in test["fact_ids"]
    }
    approved_fact_ids = {
        int(fact_id)
        for idea in approved_ideas
        for fact_id in idea["fact_ids"]
    }
    overlap = sorted(accepted_fact_ids & approved_fact_ids)
    if overlap:
        raise ValueError(
            "approved ideas use facts already covered by accepted tests: "
            f"{overlap}"
        )
    all_fact_rows = planning_fact_rows(context=context, config=config)
    unavailable_fact_ids = accepted_fact_ids | approved_fact_ids
    available_facts = [
        fact for fact in all_fact_rows if int(fact["id"]) not in unavailable_fact_ids
    ]
    available_fact_ids = {int(fact["id"]) for fact in available_facts}
    all_facts_by_id = {int(fact["id"]): fact for fact in all_fact_rows}
    facts_about_each_subject = planning_fact_subject_index(
        fact_rows=all_fact_rows, persona=config.persona
    )
    later_context_fact_ids: set[int] = set()
    for fact in available_facts:
        fact_id = int(fact["id"])
        for subject in fact["subjects"]:
            for other_fact_id in facts_about_each_subject.get(str(subject), []):
                if (
                    other_fact_id not in available_fact_ids
                    and all_facts_by_id[other_fact_id]["latest_source_date"]
                    > fact["latest_source_date"]
                ):
                    later_context_fact_ids.add(other_fact_id)
    later_active_facts = [
        fact for fact in all_fact_rows if int(fact["id"]) in later_context_fact_ids
    ]
    planner_fact_rows = [*available_facts, *later_active_facts]
    rejected_tests = load_rejected_tests(rejected_tests_paths)
    rejected_ids = {int(row["test_id"]) for row in rejected_tests}
    accepted_ids = {int(row["test_id"]) for row in accepted_tests}
    if rejected_ids & accepted_ids:
        raise ValueError(
            "rejected tests overlap accepted tests: "
            f"{sorted(rejected_ids & accepted_ids)}"
        )
    return {
        "checkpoint": {
            "checkpoint_identity": context.checkpoint_identity,
            "persona": context.persona,
            "evaluation_date": config.evaluation_date,
            "proposals_to_return": count,
        },
        "facts": available_facts,
        "later_active_facts": later_active_facts,
        "fact_ids_by_subject": planning_fact_subject_index(
            fact_rows=planner_fact_rows, persona=config.persona
        ),
        "source_sessions": planning_source_sessions(
            context=context, fact_rows=available_facts
        ),
        "actions": planner_action_inventory(context.tools),
        "accepted_tests": accepted_tests,
        "approved_ideas": approved_ideas,
        "rejected_tests": rejected_tests,
    }


def _validate_planner_evidence(
    *, response: ProposalBatch, request: dict[str, Any]
) -> None:
    """Reject a plan whose claimed source evidence is not exact and selectable."""
    facts_by_id = {int(fact["id"]): fact for fact in request["facts"]}
    source_text_by_id = {
        str(session["source_session_id"]): str(session["exact_user_message_text"])
        for session in request["source_sessions"]
    }
    for proposal_index, proposal in enumerate(response.proposals, start=1):
        selected_fact_ids = set(proposal.fact_ids)
        changed_fact_ids = {change.fact_id for change in proposal.memory_changes}
        if changed_fact_ids != selected_fact_ids:
            raise ValueError(
                f"proposal {proposal_index} evidence must contain at least one "
                "mapping for every and only the selected facts"
            )
        result_keys = [
            (change.fact_id, change.part_of_result, change.required_result)
            for change in proposal.memory_changes
        ]
        if len(result_keys) != len(set(result_keys)):
            raise ValueError(
                f"proposal {proposal_index} repeats the same fact-backed result"
            )
        for change in proposal.memory_changes:
            fact_id = change.fact_id
            source_ids = {
                str(value)
                for value in facts_by_id[fact_id]["source_session_ids"]
            }
            if change.source_session_id not in source_ids:
                raise ValueError(
                    f"proposal {proposal_index} cites session "
                    f"{change.source_session_id!r} for fact {fact_id}, but that "
                    "session does not establish the selected fact"
                )
            source_text = source_text_by_id.get(change.source_session_id)
            if source_text is None:
                raise ValueError(
                    f"proposal {proposal_index} cites source session "
                    f"{change.source_session_id!r} that was not supplied to the planner"
                )
            quote = change.exact_supporting_source_text
            if len(quote) < 3 or quote not in source_text:
                raise ValueError(
                    f"proposal {proposal_index} evidence for fact {fact_id} is not an "
                    "exact passage from its cited source session"
                )


def _legacy_prepare_proposals(
    *,
    config_path: Path,
    accepted_batch_dirs: Sequence[Path],
    approved_plan_paths: Sequence[Path] = (),
    rejected_tests_paths: Sequence[Path] = (),
    replace_test_ids: Sequence[int] = (),
    out: Path,
    count: int = 10,
) -> dict[str, Any]:
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"output directory is not empty: {out}")
    config = load_config(config_path)
    context = load_checkpoint_context(config)
    request = _legacy_proposal_request(
        config=config,
        context=context,
        accepted_batch_dirs=accepted_batch_dirs,
        approved_plan_paths=approved_plan_paths,
        rejected_tests_paths=rejected_tests_paths,
        replace_test_ids=replace_test_ids,
        count=count,
    )
    published_release = None
    if replace_test_ids:
        _, published_release = _published_test_summaries(
            config=config,
            context=context,
            replace_test_ids=replace_test_ids,
        )
    count = validate_proposal_count(count)
    system = test_idea_planner_system(count)
    schema = proposal_batch_schema(count)
    out.mkdir(parents=True, exist_ok=True)
    (out / "planner_system.txt").write_text(system + "\n")
    dump_json(out / "planner_request.json", request)
    dump_json(out / "planner_schema.json", schema)
    manifest = {
        "config_path": str(config_path.resolve()),
        "config_sha256": _sha256(config_path),
        "checkpoint_identity": context.checkpoint_identity,
        "planner_model": config.planner_model,
        "accepted_test_count": len(request["accepted_tests"]),
        "published_release": published_release,
        "approved_plan_files": [
            {"path": str(path.resolve()), "sha256": _sha256(path)}
            for path in approved_plan_paths
        ],
        "approved_idea_count": len(request["approved_ideas"]),
        "rejected_test_files": [
            {"path": str(path.resolve()), "sha256": _sha256(path)}
            for path in rejected_tests_paths
        ],
        "rejected_test_count": len(request["rejected_tests"]),
        "available_fact_count": len(request["facts"]),
        "proposal_count": count,
        "model_calls_made": 0,
    }
    dump_json(out / "manifest.json", manifest)
    return manifest


def _legacy_execute_proposals(
    *,
    config_path: Path,
    accepted_batch_dirs: Sequence[Path],
    approved_plan_paths: Sequence[Path] = (),
    rejected_tests_paths: Sequence[Path] = (),
    replace_test_ids: Sequence[int] = (),
    out: Path,
    client: AzureJsonClient,
    confirm_paid_calls: bool = False,
    count: int = 10,
) -> CandidateTaskBatch:
    if not confirm_paid_calls:
        raise ValueError("refusing planner model call without explicit confirmation")
    config = load_config(config_path)
    context = load_checkpoint_context(config)
    count = validate_proposal_count(count)
    expected_request = _legacy_proposal_request(
        config=config,
        context=context,
        accepted_batch_dirs=accepted_batch_dirs,
        approved_plan_paths=approved_plan_paths,
        rejected_tests_paths=rejected_tests_paths,
        replace_test_ids=replace_test_ids,
        count=count,
    )
    request = json.loads((out / "planner_request.json").read_text())
    if request != expected_request:
        raise ValueError("prepared planner request no longer matches its inputs")
    manifest = json.loads((out / "manifest.json").read_text())
    expected_approved_plans_identity = {
        "approved_plan_files": [
            {"path": str(path.resolve()), "sha256": _sha256(path)}
            for path in approved_plan_paths
        ],
        "approved_idea_count": len(expected_request["approved_ideas"]),
    }
    if any(
        manifest.get(key) != value
        for key, value in expected_approved_plans_identity.items()
    ):
        raise ValueError("prepared approved plan identity no longer matches its inputs")
    expected_rejected_tests_identity = {
        "rejected_test_files": [
            {"path": str(path.resolve()), "sha256": _sha256(path)}
            for path in rejected_tests_paths
        ],
        "rejected_test_count": len(expected_request["rejected_tests"]),
    }
    if any(
        manifest.get(key) != value
        for key, value in expected_rejected_tests_identity.items()
    ):
        raise ValueError("prepared rejected tests identity no longer matches its inputs")
    expected_published_release = None
    if replace_test_ids:
        _, expected_published_release = _published_test_summaries(
            config=config,
            context=context,
            replace_test_ids=replace_test_ids,
        )
    if manifest.get("published_release") != expected_published_release:
        raise ValueError("prepared published release identity no longer matches its inputs")
    if manifest.get("proposal_count") != count:
        raise ValueError("prepared proposal count no longer matches its inputs")
    schema = proposal_batch_schema(count)
    if json.loads((out / "planner_schema.json").read_text()) != schema:
        raise ValueError("prepared planner schema no longer matches the code")
    system = test_idea_planner_system(count)
    if (out / "planner_system.txt").read_text() != system + "\n":
        raise ValueError("prepared planner instructions no longer match the code")
    if client.model != config.planner_model:
        raise ValueError("planner client model does not match the config")
    with _call_ledger(out / "call_ledger.jsonl"):
        raw = cached_client_complete(
            out / "work" / "planner_response_cache.json",
            system,
            request,
            client,
            response_schema=schema,
            response_schema_name=f"dolphinbench_{count}_test_proposals",
        )
    response = ProposalBatch.model_validate(
        {key: value for key, value in raw.items() if not key.startswith("_")}
    )
    if len(response.proposals) != count:
        raise ValueError(
            f"planner response must contain exactly {count} proposals, got "
            f"{len(response.proposals)}"
        )
    checkpoint = request["checkpoint"]
    if (
        response.checkpoint_identity != checkpoint["checkpoint_identity"]
        or response.persona != checkpoint["persona"]
        or response.evaluation_date != checkpoint["evaluation_date"]
    ):
        raise ValueError("planner response does not match the checkpoint identity")
    available_facts = {int(row["id"]) for row in request["facts"]}
    available_tools = {str(row["tool_name"]) for row in request["actions"]}
    for index, proposal in enumerate(response.proposals, start=1):
        missing_facts = sorted(set(proposal.fact_ids) - available_facts)
        missing_tools = sorted(set(proposal.tool_names) - available_tools)
        if missing_facts or missing_tools:
            raise ValueError(
                f"proposal {index} references unavailable facts {missing_facts} "
                f"or tools {missing_tools}"
            )
    _validate_planner_evidence(response=response, request=request)
    batch = CandidateTaskBatch(
        checkpoint_identity=context.checkpoint_identity,
        persona=context.persona,
        evaluation_date=config.evaluation_date,
        tasks=[proposal.to_planned_task() for proposal in response.proposals],
    )
    if len(batch.tasks) != count:
        raise ValueError(
            f"candidate task batch must contain exactly {count} tasks, got "
            f"{len(batch.tasks)}"
        )
    dump_json(out / "planner_raw_response.json", raw)
    dump_json(out / "proposals.json", response.model_dump(mode="json"))
    dump_json(out / "candidate_plan.json", batch.model_dump(mode="json"))
    manifest.update(
        {
            "model_calls_made": 1,
            "planner_usage": raw.get("_usage") or {},
            "candidate_plan": str((out / "candidate_plan.json").resolve()),
        }
    )
    dump_json(out / "manifest.json", manifest)
    return batch


def _one_line(value: str, *, limit: int = 280) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _task_work_summary(task: Any) -> str:
    if isinstance(task, ApprovedIdea):
        return _one_line(task.work)
    return _one_line(str(task.requested_work or task.task_description or ""))


def _compact_prior_work(
    *,
    accepted: Sequence[dict[str, Any]],
    approved: Sequence[dict[str, Any]],
    rejected: Sequence[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    compact_accepted = [
        {
            "test_id": int(row["test_id"]),
            "fact_ids": sorted(int(value) for value in row["fact_ids"]),
            "work": _one_line(str(row["requested_work"])),
            "tools": sorted(str(value) for value in row["tool_names"]),
        }
        for row in accepted
    ]
    compact_approved = [
        {
            "fact_ids": sorted(int(value) for value in row["fact_ids"]),
            "work": _one_line(str(row["requested_work"])),
            "tools": sorted(str(value) for value in row["tool_names"]),
        }
        for row in approved
    ]
    compact_rejected: list[dict[str, Any]] = []
    for row in rejected:
        plan = row["candidate_plan"]
        work = ""
        facts: list[int] = []
        if isinstance(plan, dict):
            work = str(plan.get("work") or plan.get("requested_work") or plan.get("task_description") or "")
            raw_facts = plan.get("fact_ids") or []
            if isinstance(raw_facts, list):
                facts = sorted(
                    int(value)
                    for value in raw_facts
                    if isinstance(value, int) and not isinstance(value, bool)
                )
        compact_rejected.append(
            {
                "test_id": int(row["test_id"]),
                "fact_ids": facts,
                "work": _one_line(work or "Earlier rejected idea."),
                "reason": _one_line(str(row["failure_reason"]), limit=220),
            }
        )
    return {
        "accepted": compact_accepted,
        "approved": compact_approved,
        "rejected": compact_rejected,
    }


def _state_path(config: AuthoringConfig, persona_state_path: Path | None) -> Path:
    if persona_state_path is not None:
        return persona_state_path.resolve()
    configured_state = getattr(config, "persona_state", None)
    if configured_state is not None:
        path = Path(configured_state).expanduser()
        return path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()
    return _default_release_manifest_path(config).resolve()


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ValueError(f"{label} is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def _release_manifest_from_state(
    *, state_path: Path, state: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    entry = state.get("release_manifest")
    if entry is None:
        return state_path, state
    if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
        raise ValueError("persona state release_manifest must contain path and sha256")
    release_path = _state_entry_path(
        state_path=state_path, entry=entry, key="path"
    )
    if not release_path.is_file() or entry["sha256"] != _sha256(release_path):
        raise ValueError("persona state release manifest is missing or changed")
    return release_path, _load_json_object(
        release_path, label="persona state release manifest"
    )


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _append_state_file(
    *, state_path: Path, state: dict[str, Any], key: str, path: Path
) -> bool:
    rows = state.setdefault(key, [])
    if not isinstance(rows, list):
        raise ValueError(f"persona state {key} must be an array")
    resolved = path.resolve()
    entry = {"path": str(resolved), "sha256": _sha256(resolved)}
    for existing in rows:
        if not isinstance(existing, dict):
            raise ValueError(f"persona state {key} entries must be objects")
        existing_path = _state_entry_path(
            state_path=state_path, entry=existing, key="path"
        )
        if existing_path == resolved:
            if existing != entry:
                raise ValueError(f"persona state {key} file changed after it was recorded")
            return False
    rows.append(entry)
    return True


def _accepted_batch_entry(directory: Path) -> dict[str, str]:
    resolved = directory.resolve()
    planning_batch = resolved / "planning_batch.json"
    provenance = resolved / "provenance.json"
    if not planning_batch.is_file() or not provenance.is_file():
        raise ValueError(f"accepted batch is incomplete: {resolved}")
    return {
        "directory": str(resolved),
        "planning_batch_sha256": _sha256(planning_batch),
        "provenance_sha256": _sha256(provenance),
    }


def _append_state_batch(
    *, state_path: Path, state: dict[str, Any], directory: Path
) -> bool:
    rows = state.setdefault("batches", [])
    if not isinstance(rows, list):
        raise ValueError("persona state batches must be an array")
    entry = _accepted_batch_entry(directory)
    resolved = Path(entry["directory"])
    for existing in rows:
        if not isinstance(existing, dict):
            raise ValueError("persona state batch entries must be objects")
        existing_path = _state_entry_path(
            state_path=state_path, entry=existing, key="directory"
        )
        if existing_path == resolved:
            if existing != entry:
                raise ValueError(
                    "persona state accepted batch changed after it was recorded"
                )
            return False
    rows.append(entry)
    return True


def record_approved_plan(
    *, config: AuthoringConfig, context: CheckpointContext, plan_path: Path
) -> None:
    """Record an explicitly approved plan before its authoring calls begin."""
    if getattr(config, "persona_state", None) is None:
        return
    state_path = _state_path(config, None)
    state = _load_json_object(state_path, label="persona state")
    load_persona_state(config=config, context=context, persona_state_path=state_path)
    if _append_state_file(
        state_path=state_path, state=state, key="approved_plans", path=plan_path
    ):
        _write_json_atomic(state_path, state)


def validate_working_batch_replacement(
    *, config: AuthoringConfig, context: CheckpointContext, binding: dict[str, Any],
) -> None:
    """Fail before paid work if the approved batch is no longer active."""
    if getattr(config, "persona_state", None) is None:
        raise ValueError("accepted revisions require an authenticated persona state")
    state_path = _state_path(config, None)
    state = _load_json_object(state_path, label="persona state")
    load_persona_state(config=config, context=context, persona_state_path=state_path)
    directory = Path(binding["directory"]).resolve()
    if _accepted_batch_entry(directory) != binding:
        raise ValueError("accepted repair source batch changed")
    entries = [row for row in state.get("batches", []) if _state_entry_path(
        state_path=state_path, entry=row, key="directory",
    ) == directory]
    if entries != [binding]:
        raise ValueError("the approved accepted batch is no longer an active working batch")


def record_accepted_batch(
    *, config: AuthoringConfig, context: CheckpointContext, directory: Path,
    replaces_accepted_batch: dict[str, Any] | None = None,
) -> None:
    """Register one completed accepted batch for subsequent planning calls."""
    if getattr(config, "persona_state", None) is None:
        return
    resolved = directory.resolve()
    state_path = _state_path(config, None)
    state = _load_json_object(state_path, label="persona state")
    loaded = load_persona_state(
        config=config, context=context, persona_state_path=state_path
    )
    accepted_batch_dirs = list(loaded["accepted_batch_dirs"])
    already_recorded = resolved in accepted_batch_dirs
    if replaces_accepted_batch is not None and not already_recorded:
        old = Path(replaces_accepted_batch["directory"]).resolve()
        if _accepted_batch_entry(old) != replaces_accepted_batch:
            raise ValueError("accepted repair source batch changed")
        entries = [row for row in state.get("batches", []) if _state_entry_path(
            state_path=state_path, entry=row, key="directory",
        ) == old]
        if len(entries) != 1 or entries[0] != replaces_accepted_batch:
            raise ValueError("only an authenticated working batch may be replaced")
        before = _load_json_object(old / "provenance.json", label="accepted provenance")
        after = _load_json_object(resolved / "provenance.json", label="accepted provenance")
        if not set(before["accepted_test_ids"]).issubset(after["accepted_test_ids"]):
            raise ValueError("accepted repair may not remove a previously accepted test")
        if (after.get("resumed_from") or {}).get("replaces_accepted_batch") != replaces_accepted_batch:
            raise ValueError("accepted replacement has no matching repair provenance")
        accepted_batch_dirs.remove(old)
        state["batches"].remove(entries[0])
        state.setdefault("superseded_batches", []).append({
            "batch": entries[0], "replaced_by": _accepted_batch_entry(resolved),
        })
    if resolved not in accepted_batch_dirs:
        accepted_batch_dirs.append(resolved)
    _state_test_summaries(
        config=config,
        context=context,
        state={**loaded, "accepted_batch_dirs": accepted_batch_dirs},
    )
    if already_recorded:
        return
    if _append_state_batch(
        state_path=state_path, state=state, directory=resolved
    ):
        _write_json_atomic(state_path, state)


def record_rejected_tests(
    *, config: AuthoringConfig, context: CheckpointContext, rejected_path: Path
) -> None:
    """Record rejected ideas from one completed authoring run."""
    if getattr(config, "persona_state", None) is None:
        return
    rows = _load_json_object(rejected_path, label="rejected tests").get(
        "rejected_tests"
    )
    if not isinstance(rows, list):
        raise ValueError("rejected tests file must contain a rejected_tests array")
    if not rows:
        return
    state_path = _state_path(config, None)
    state = _load_json_object(state_path, label="persona state")
    load_persona_state(config=config, context=context, persona_state_path=state_path)
    if _append_state_file(
        state_path=state_path,
        state=state,
        key="rejected_test_files",
        path=rejected_path,
    ):
        _write_json_atomic(state_path, state)


def reset_persona_state_after_publish(
    *, config: AuthoringConfig, release_manifest_path: Path
) -> None:
    """Point working state at a newly published release and clear completed work."""
    if getattr(config, "persona_state", None) is None:
        return
    state_path = _state_path(config, None)
    release_path = release_manifest_path.resolve()
    state = {
        "version": 1,
        "persona": config.persona,
        "checkpoint_identity": _load_json_object(
            release_path, label="release manifest"
        )["checkpoint_identity"],
        "evaluation_date": config.evaluation_date,
        "release_manifest": {
            "path": os.path.relpath(release_path, state_path.parent),
            "sha256": _sha256(release_path),
        },
        "batches": [],
        "approved_plans": [],
        "rejected_test_files": [],
    }
    _write_json_atomic(state_path, state)


def _state_entry_path(*, state_path: Path, entry: dict[str, Any], key: str) -> Path:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"persona state entry needs a non-empty {key}")
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (state_path.parent / path).resolve()


def _verified_state_paths(
    *, state_path: Path, entries: Any, key: str, label: str
) -> list[Path]:
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ValueError(f"persona state {label} must be an array")
    paths: list[Path] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"persona state {label} entries must be objects")
        path = _state_entry_path(state_path=state_path, entry=entry, key=key)
        digest = entry.get("sha256")
        if not path.is_file():
            raise ValueError(f"persona state {label} file is missing: {path}")
        if not isinstance(digest, str) or digest != _sha256(path):
            raise ValueError(f"persona state {label} hash does not match: {path}")
        paths.append(path)
    if len(paths) != len(set(paths)):
        raise ValueError(f"persona state repeats a {label} path")
    return paths


def load_persona_state(
    *,
    config: AuthoringConfig,
    context: CheckpointContext,
    persona_state_path: Path | None = None,
) -> dict[str, Any]:
    """Load prior work from the single manifest-style persona state input."""
    path = _state_path(config, persona_state_path)
    if not path.is_file():
        raise ValueError(f"persona state is missing: {path}")
    raw = _load_json_object(path, label="persona state")
    release_path, release = _release_manifest_from_state(state_path=path, state=raw)
    for key, expected in (
        ("persona", context.persona),
        ("checkpoint_identity", context.checkpoint_identity),
        ("evaluation_date", config.evaluation_date),
    ):
        if raw.get(key) != expected:
            raise ValueError(f"persona state has wrong {key}")

    release_batches = release.get("batches")
    if not isinstance(release_batches, list):
        raise ValueError("persona state must contain the release-manifest batches array")
    published_tests: list[dict[str, Any]] = []
    if release.get("published") is True:
        published_tests, _ = _published_test_summaries(
            config=config, context=context, replace_test_ids=[],
            manifest_path=release_path,
        )
        release_batches = []
    working_batches: Any = []
    if release_path != path:
        working_batches = raw.get("batches", [])
        if not isinstance(working_batches, list):
            raise ValueError("persona state batches must be an array")
    accepted_batch_dirs: list[Path] = []
    batch_entries = [
        (release_path, entry) for entry in release_batches
    ] + [
        (path, entry) for entry in working_batches
    ]
    for entry_state_path, entry in batch_entries:
        if not isinstance(entry, dict):
            raise ValueError("persona state batch entries must be objects")
        directory = _state_entry_path(
            state_path=entry_state_path, entry=entry, key="directory"
        )
        planning_hash = entry.get("planning_batch_sha256")
        provenance_hash = entry.get("provenance_sha256")
        plan = directory / "planning_batch.json"
        provenance = directory / "provenance.json"
        if not plan.is_file() or not provenance.is_file():
            raise ValueError(f"persona state batch is incomplete: {directory}")
        if (
            not isinstance(planning_hash, str)
            or planning_hash != _sha256(plan)
            or not isinstance(provenance_hash, str)
            or provenance_hash != _sha256(provenance)
        ):
            raise ValueError(f"persona state batch hash does not match: {directory}")
        accepted_batch_dirs.append(directory)
    if len(accepted_batch_dirs) != len(set(accepted_batch_dirs)):
        raise ValueError("persona state repeats an accepted batch directory")

    return {
        "path": path,
        "sha256": _sha256(path),
        "accepted_batch_dirs": accepted_batch_dirs,
        "published_tests": published_tests,
        "approved_plan_paths": _verified_state_paths(
            state_path=path,
            entries=raw.get("approved_plans"),
            key="path",
            label="approved plans",
        ),
        "rejected_tests_paths": _verified_state_paths(
            state_path=path,
            entries=raw.get("rejected_test_files"),
            key="path",
            label="rejected tests",
        ),
    }


def _state_test_summaries(
    *, config: AuthoringConfig, context: CheckpointContext,
    state: dict[str, Any], excluded_test_ids: Sequence[int] = (),
) -> list[dict[str, Any]]:
    accepted = _accepted_test_summaries(
        config=config, context=context,
        accepted_batch_dirs=state["accepted_batch_dirs"],
        excluded_test_ids=excluded_test_ids,
    )
    excluded = set(excluded_test_ids)
    published = [
        row for row in state.get("published_tests", [])
        if int(row["test_id"]) not in excluded
    ]
    return _merge_test_summaries(published, accepted, replace_test_ids=[])


def proposal_request(
    *,
    config: AuthoringConfig,
    context: CheckpointContext,
    persona_state_path: Path | None = None,
    replace_test_ids: Sequence[int] = (),
    count: int = 10,
) -> dict[str, Any]:
    """Build the request for the first planning call."""
    count = validate_proposal_count(count)
    replacement_ids = set(replace_test_ids)
    if len(replacement_ids) != len(replace_test_ids) or any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in replacement_ids
    ):
        raise ValueError("replacement test IDs must be unique positive integers")
    state = load_persona_state(
        config=config, context=context, persona_state_path=persona_state_path
    )
    accepted = _state_test_summaries(
        config=config,
        context=context,
        state=state,
        excluded_test_ids=replace_test_ids,
    )
    approved = _approved_idea_summaries(
        config=config,
        context=context,
        approved_plan_paths=state["approved_plan_paths"],
    )
    rejected = load_rejected_tests(state["rejected_tests_paths"])
    fact_rows = planning_fact_rows(context=context, config=config)
    used_by_test_ids: dict[int, list[int]] = {}
    for test in accepted:
        for fact_id in test["fact_ids"]:
            used_by_test_ids.setdefault(int(fact_id), []).append(int(test["test_id"]))
    return {
        "checkpoint": {
            "checkpoint_identity": context.checkpoint_identity,
            "persona": context.persona,
            "evaluation_date": config.evaluation_date,
            "proposals_to_return": count,
        },
        "current_facts": [
            {
                "id": int(row["id"]),
                "statement": str(row["statement"]),
                "applies_when": str(row["applies_when"]),
                "subjects": list(row["subjects"]),
                "latest_source_date": str(row["latest_source_date"]),
                "supersedes": list(row["supersedes"]),
                "used_by_test_ids": sorted(used_by_test_ids.get(int(row["id"]), [])),
            }
            for row in fact_rows
        ],
        "tools": planner_action_inventory(context.tools),
        "prior_work": _compact_prior_work(
            accepted=accepted, approved=approved, rejected=rejected
        ),
    }


def _planning_fact_shards(
    *, request: dict[str, Any], persona: str, shard_count: int
) -> list[list[int]]:
    """Partition selectable facts without splitting replacement chains."""
    if not 1 <= shard_count <= MAX_PLANNER_SHARDS:
        raise ValueError(
            f"planner shards must be between one and {MAX_PLANNER_SHARDS}"
        )
    rows = request["current_facts"]
    fact_ids = [int(row["id"]) for row in rows]
    if not fact_ids:
        raise ValueError("planning requires at least one current fact")
    if shard_count == 1:
        return [fact_ids]

    parent = {fact_id: fact_id for fact_id in fact_ids}

    def find(fact_id: int) -> int:
        while parent[fact_id] != fact_id:
            parent[fact_id] = parent[parent[fact_id]]
            fact_id = parent[fact_id]
        return fact_id

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    available = set(fact_ids)
    for row in rows:
        fact_id = int(row["id"])
        for replaced in row.get("supersedes") or []:
            replaced_id = int(replaced)
            if replaced_id in available:
                union(fact_id, replaced_id)

    components: dict[int, list[int]] = {}
    for fact_id in fact_ids:
        components.setdefault(find(fact_id), []).append(fact_id)
    groups = [sorted(group) for group in components.values()]
    groups.sort(key=lambda group: (-len(group), group[0]))
    if len(groups) < shard_count:
        raise ValueError(
            f"cannot make {shard_count} nonempty related-fact shards from "
            f"{len(groups)} fact groups"
        )

    shards: list[list[int]] = [[] for _ in range(shard_count)]
    for group in groups:
        index = min(range(shard_count), key=lambda value: (len(shards[value]), value))
        shards[index].extend(group)
    return [sorted(shard) for shard in shards]


def _sharded_proposal_requests(
    *, request: dict[str, Any], persona: str, shard_count: int
) -> list[dict[str, Any]]:
    fact_ids_by_shard = _planning_fact_shards(
        request=request, persona=persona, shard_count=shard_count
    )
    facts_by_id = {int(row["id"]): row for row in request["current_facts"]}
    persona_subject = f"person:{persona.lower()}"
    persona_prefix = persona_subject + "_"

    def concrete_subjects(row: dict[str, Any]) -> set[str]:
        return {
            str(value).strip()
            for value in row.get("subjects") or []
            if str(value).strip()
            and str(value).strip().lower() != persona.lower()
            and str(value).strip() != persona_subject
            and not str(value).strip().startswith(persona_prefix)
        }

    subjects_by_id = {
        fact_id: concrete_subjects(row) for fact_id, row in facts_by_id.items()
    }
    requests: list[dict[str, Any]] = []
    for index, fact_ids in enumerate(fact_ids_by_shard, start=1):
        shard_request = copy.deepcopy(request)
        assigned = set(fact_ids)
        assigned_subjects = {
            subject for fact_id in fact_ids for subject in subjects_by_id[fact_id]
        }
        context_ids = sorted(
            fact_id
            for fact_id, subjects in subjects_by_id.items()
            if fact_id not in assigned
            and subjects & assigned_subjects
            and any(
                str(facts_by_id[fact_id].get("latest_source_date") or "")
                > str(facts_by_id[assigned_id].get("latest_source_date") or "")
                and bool(subjects & subjects_by_id[assigned_id])
                for assigned_id in assigned
            )
        )
        shard_request["current_facts"] = [
            {
                **facts_by_id[fact_id],
                "available_for_new_idea": fact_id in assigned,
            }
            for fact_id in [*fact_ids, *context_ids]
        ]
        shard_request["planning_shard"] = {
            "index": index,
            "count": shard_count,
            "assigned_fact_ids": fact_ids,
            "related_context_fact_ids": context_ids,
        }
        requests.append(shard_request)
    return requests


def _validate_situation_response(
    *, response: SituationProposalBatch, request: dict[str, Any], count: int
) -> None:
    checkpoint = request["checkpoint"]
    if (
        response.checkpoint_identity != checkpoint["checkpoint_identity"]
        or response.persona != checkpoint["persona"]
        or response.evaluation_date != checkpoint["evaluation_date"]
        or len(response.proposals) != count
    ):
        raise ValueError("situation response does not match the prepared checkpoint or count")
    facts = {int(row["id"]) for row in request["current_facts"]}
    tools = {str(row["tool_name"]) for row in request["tools"]}
    for proposal in response.proposals:
        missing_facts = sorted(set(proposal.candidate_fact_ids) - facts)
        missing_tools = sorted(set(proposal.tool_names) - tools)
        if missing_facts or missing_tools:
            raise ValueError(
                f"proposal {proposal.proposal_id} references unavailable facts {missing_facts} "
                f"or tools {missing_tools}"
            )


def evidence_request(
    *,
    config: AuthoringConfig,
    context: CheckpointContext,
    frozen_proposal: SituationProposalBatch,
) -> dict[str, Any]:
    """Build the source-only second-call input from the validated first response."""
    all_rows = planning_fact_rows(context=context, config=config)
    rows_by_id = {int(row["id"]): row for row in all_rows}
    subject_index = planning_fact_subject_index(fact_rows=all_rows, persona=config.persona)
    selected_facts: list[dict[str, Any]] = []
    later_facts: list[dict[str, Any]] = []
    for proposal in frozen_proposal.proposals:
        selected_ids = set(proposal.candidate_fact_ids)
        for fact_id in proposal.candidate_fact_ids:
            row = rows_by_id[fact_id]
            selected_facts.append(
                {
                    "proposal_id": proposal.proposal_id,
                    "fact_id": fact_id,
                    "statement": str(row["statement"]),
                    "applies_when": str(row["applies_when"]),
                    "subjects": list(row["subjects"]),
                    "source_session_ids": list(row["source_session_ids"]),
                    "exact_source_messages": planning_source_sessions(
                        context=context, fact_rows=[row]
                    ),
                }
            )
            related_ids: set[int] = set()
            for subject in row["subjects"]:
                for other_id in subject_index.get(str(subject), []):
                    other = rows_by_id[other_id]
                    if (
                        other_id not in selected_ids
                        and str(other["latest_source_date"]) > str(row["latest_source_date"])
                    ):
                        related_ids.add(other_id)
            for other_id in sorted(related_ids):
                other = rows_by_id[other_id]
                later_facts.append(
                    {
                        "proposal_id": proposal.proposal_id,
                        "fact_id": other_id,
                        "statement": str(other["statement"]),
                        "applies_when": str(other["applies_when"]),
                        "subjects": list(other["subjects"]),
                        "latest_source_date": str(other["latest_source_date"]),
                        "supersedes": list(other["supersedes"]),
                    }
                )
    return {
        "checkpoint": {
            "checkpoint_identity": frozen_proposal.checkpoint_identity,
            "persona": frozen_proposal.persona,
            "evaluation_date": frozen_proposal.evaluation_date,
            "proposals_to_return": len(frozen_proposal.proposals),
        },
        "frozen_proposal": frozen_proposal.model_dump(mode="json"),
        "necessity_evidence_version": 2,
        "all_current_facts": [
            {
                "id": int(row["id"]),
                "statement": str(row["statement"]),
                "applies_when": str(row["applies_when"]),
                "subjects": list(row["subjects"]),
                "latest_source_date": str(row["latest_source_date"]),
                "supersedes": list(row["supersedes"]),
            }
            for row in all_rows
        ],
        "selected_facts": selected_facts,
        "later_facts_about_same_subjects": later_facts,
    }


def _validate_evidence_response(
    *,
    response: EvidenceDecisionBatch,
    frozen_proposal: SituationProposalBatch,
    request: dict[str, Any],
) -> EvidenceDecisionBatch:
    if (
        response.checkpoint_identity != frozen_proposal.checkpoint_identity
        or response.persona != frozen_proposal.persona
        or response.evaluation_date != frozen_proposal.evaluation_date
    ):
        raise ValueError("evidence response does not match the frozen checkpoint identity")
    proposals = {proposal.proposal_id: proposal for proposal in frozen_proposal.proposals}
    if {decision.proposal_id for decision in response.decisions} != set(proposals):
        raise ValueError("evidence response must decide every and only frozen proposal IDs")
    facts_by_proposal: dict[int, dict[int, dict[str, Any]]] = {}
    for fact in request["selected_facts"]:
        facts_by_proposal.setdefault(int(fact["proposal_id"]), {})[int(fact["fact_id"])] = fact
    validated_decisions = []
    for decision in response.decisions:
        if decision.outcome != "accept":
            validated_decisions.append(decision)
            continue
        try:
            if request.get("necessity_evidence_version") == 2 and any(
                not result.why_required_for_work.strip()
                for result in decision.remembered_results
            ):
                raise ValueError(
                    "each remembered result must explain why the requested work needs it"
                )
            proposal = proposals[decision.proposal_id]
            allowed_facts = facts_by_proposal.get(decision.proposal_id)
            if allowed_facts is None:
                raise ValueError("proposal has no supplied selected facts")
            allowed_items = {
                item.work_item_id: item.work for item in proposal.work_items
            }
            result_facts = {result.fact_id for result in decision.remembered_results}
            if result_facts != set(proposal.candidate_fact_ids):
                raise ValueError(
                    f"accepted proposal {proposal.proposal_id} must attach every and only its candidate facts"
                )
            seen: set[tuple[int, str, str]] = set()
            for result in decision.remembered_results:
                if result.work_item_id not in allowed_items:
                    raise ValueError(
                        f"remembered result references missing work_item_id {result.work_item_id}"
                    )
                if result.exact_work_item_text != allowed_items[result.work_item_id]:
                    raise ValueError(
                        "remembered result does not copy its frozen work item text exactly"
                    )
                fact = allowed_facts.get(result.fact_id)
                if fact is None:
                    raise ValueError(
                        f"remembered result cites unavailable fact {result.fact_id}"
                    )
                if result.source_session_id not in set(fact["source_session_ids"]):
                    raise ValueError(
                        f"source session {result.source_session_id!r} does not establish fact {result.fact_id}"
                    )
                source_text = next(
                    (
                        str(source["exact_user_message_text"])
                        for source in fact["exact_source_messages"]
                        if source["source_session_id"] == result.source_session_id
                    ),
                    None,
                )
                if (
                    source_text is None
                    or len(result.exact_supporting_source_text) < 3
                    or result.exact_supporting_source_text not in source_text
                ):
                    raise ValueError(
                        f"evidence quote for fact {result.fact_id} is not an exact supplied source passage"
                    )
                key = (result.fact_id, result.work_item_id, result.required_result)
                if key in seen:
                    raise ValueError(
                        "evidence response repeats the same remembered result"
                    )
                seen.add(key)
        except ValueError as exc:
            validated_decisions.append(
                decision.__class__(
                    proposal_id=decision.proposal_id,
                    outcome="reject",
                    rejection_reason=f"Local evidence validation failed: {exc}",
                    remembered_results=[],
                )
            )
        else:
            validated_decisions.append(decision)
    return response.model_copy(update={"decisions": validated_decisions})


def _candidate_batch(
    *, frozen_proposal: SituationProposalBatch, response: EvidenceDecisionBatch
) -> CandidateTaskBatch:
    decisions = {decision.proposal_id: decision for decision in response.decisions}
    tasks = []
    for proposal in frozen_proposal.proposals:
        decision = decisions[proposal.proposal_id]
        if decision.outcome != "accept":
            continue
        results = decision.remembered_results
        tasks.append(
            {
                "new_situation": proposal.new_situation,
                "requested_work": proposal.requested_work,
                "work_items": [item.model_dump(mode="json") for item in proposal.work_items],
                "fact_ids": list(proposal.candidate_fact_ids),
                "required_results": [
                    RequiredResult(
                        result=result.required_result,
                        fact_ids=[result.fact_id],
                        work_item_id=result.work_item_id,
                    ).model_dump(mode="json", exclude_none=True)
                    for result in results
                ],
                "expected_tools": list(proposal.tool_names),
                "action_without_memory": proposal.without_history,
                "why_memory_changes_result": "; ".join(
                    result.why_required_for_work for result in results if result.why_required_for_work
                ) or "The cited earlier facts change the listed work items.",
                "fact_application_explanations_on_evaluation_date": [
                    {
                        "fact_id": result.fact_id,
                        "required_result": result.required_result,
                        "source_session_id": result.source_session_id,
                        "exact_supporting_source_text": result.exact_supporting_source_text,
                        "why_source_supports_required_result": result.why_source_supports_required_result,
                        "why_fact_still_applies_on_evaluation_date": result.why_fact_still_applies_on_evaluation_date,
                    }
                    for result in results
                ],
                "why_listed_tools_can_complete_requested_work": proposal.why_listed_tools_can_complete_requested_work,
            }
        )
    return CandidateTaskBatch(
        checkpoint_identity=frozen_proposal.checkpoint_identity,
        persona=frozen_proposal.persona,
        evaluation_date=frozen_proposal.evaluation_date,
        tasks=tasks,
    )


def _rejected_planning_ideas(
    *,
    frozen_proposal: SituationProposalBatch,
    response: EvidenceDecisionBatch,
    out: Path,
    replace_test_ids: Sequence[int],
) -> list[dict[str, Any]]:
    """Turn rejected evidence decisions into the existing prior-work record."""
    decisions = {decision.proposal_id: decision for decision in response.decisions}
    replacement_ids = list(replace_test_ids)
    rejected: list[dict[str, Any]] = []
    for proposal in frozen_proposal.proposals:
        decision = decisions[proposal.proposal_id]
        if decision.outcome != "reject":
            continue
        test_id = (
            replacement_ids[proposal.proposal_id - 1]
            if proposal.proposal_id <= len(replacement_ids)
            else proposal.proposal_id
        )
        rejected.append(
            {
                "test_id": test_id,
                "candidate_plan": proposal.model_dump(mode="json"),
                "failure_reason": decision.rejection_reason,
                "candidate_plan_path": str((out / "frozen_proposal.json").resolve()),
                "failure_manifest_path": str((out / "manifest.json").resolve()),
            }
        )
    return rejected


def _idea_request(request: dict[str, Any]) -> dict[str, Any]:
    """Keep checkpoint identity in program records, not model-generated output."""
    rendered = {
        "requested_count": request["checkpoint"]["proposals_to_return"],
        "evaluation_date": request["checkpoint"]["evaluation_date"],
        "current_facts": request["current_facts"],
        "tools": request["tools"],
        "previous_work": request["prior_work"],
    }
    if "planning_shard" in request:
        rendered["planning_shard"] = request["planning_shard"]
    return rendered


def _idea_response_schema(count: int) -> dict[str, Any]:
    from authoring.complete_test import strict_schema
    schema = IdeaResponse.model_json_schema()
    schema["properties"]["ideas"]["maxItems"] = validate_proposal_count(count)
    return strict_schema(schema)


def _planner_artifact_dir(out: Path, *, shard_index: int, shard_count: int) -> Path:
    return out if shard_count == 1 else out / "shards" / f"{shard_index:02d}"


def prepare_proposals(
    *,
    config_path: Path,
    out: Path,
    persona_state_path: Path | None = None,
    replace_test_ids: Sequence[int] = (),
    count: int = 10,
    shards: int = 1,
    concurrency: int = 1,
) -> dict[str, Any]:
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"output directory is not empty: {out}")
    config = load_config(config_path)
    context = load_checkpoint_context(config)
    count = validate_proposal_count(count)
    if not 1 <= shards <= MAX_PLANNER_SHARDS:
        raise ValueError(f"planner shards must be between one and {MAX_PLANNER_SHARDS}")
    if not 1 <= concurrency <= MAX_PLANNER_CONCURRENCY:
        raise ValueError(
            f"planner concurrency must be between one and {MAX_PLANNER_CONCURRENCY}"
        )
    state = load_persona_state(
        config=config, context=context, persona_state_path=persona_state_path
    )
    request = proposal_request(
        config=config, context=context, persona_state_path=state["path"],
        replace_test_ids=replace_test_ids, count=count,
    )
    requests = _sharded_proposal_requests(
        request=request, persona=context.persona, shard_count=shards
    )
    out.mkdir(parents=True, exist_ok=True)
    shard_records = []
    for index, shard_request in enumerate(requests, start=1):
        directory = _planner_artifact_dir(
            out, shard_index=index, shard_count=shards
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "planner_system.txt").write_text(IDEA_PLANNER_SYSTEM + "\n")
        dump_json(directory / "planner_request.json", _idea_request(shard_request))
        dump_json(directory / "planner_schema.json", _idea_response_schema(count))
        shard_records.append(
            {
                "index": index,
                "fact_ids": shard_request["planning_shard"]["assigned_fact_ids"],
                "status": "prepared",
                "cache": str(
                    (directory / "work" / "planner_response_cache.json").relative_to(out)
                ),
            }
        )
    manifest = {
        "authoring_version": 4,
        "config_path": str(config_path.resolve()),
        "config_sha256": _sha256(config_path),
        "checkpoint_identity": context.checkpoint_identity,
        "persona": context.persona,
        "evaluation_date": config.evaluation_date,
        "persona_state": {"path": str(state["path"]), "sha256": state["sha256"]},
        "planner_model": config.planner_model,
        "ideas_per_shard": count,
        "planner_shard_count": shards,
        "planner_concurrency": concurrency,
        "maximum_proposal_count": count * shards,
        "current_fact_count": len(request["current_facts"]),
        "prior_work_counts": {key: len(value) for key, value in request["prior_work"].items()},
        "model_calls_made": 0,
        "planner_shards": shard_records,
        "status": "prepared",
    }
    if shards == 1:
        manifest["proposal_count"] = count
        manifest["calls"] = {
            "planner": {
                "status": "prepared",
                "cache": "work/planner_response_cache.json",
            }
        }
    dump_json(out / "manifest.json", manifest)
    return manifest


def execute_proposals(
    *,
    config_path: Path,
    out: Path,
    client: AzureJsonClient,
    confirm_paid_calls: bool = False,
    persona_state_path: Path | None = None,
    replace_test_ids: Sequence[int] = (),
    count: int = 10,
    shards: int = 1,
    concurrency: int = 1,
) -> CandidateTaskBatch:
    if not confirm_paid_calls:
        raise ValueError("refusing planner model calls without explicit confirmation")
    config = load_config(config_path)
    context = load_checkpoint_context(config)
    count = validate_proposal_count(count)
    if not 1 <= shards <= MAX_PLANNER_SHARDS:
        raise ValueError(f"planner shards must be between one and {MAX_PLANNER_SHARDS}")
    if not 1 <= concurrency <= MAX_PLANNER_CONCURRENCY:
        raise ValueError(
            f"planner concurrency must be between one and {MAX_PLANNER_CONCURRENCY}"
        )
    manifest = json.loads((out / "manifest.json").read_text())
    if manifest.get("authoring_version") != 4:
        raise ValueError("old prepared planning runs are records; prepare a fresh version-4 plan")
    request = proposal_request(
        config=config, context=context, persona_state_path=persona_state_path,
        replace_test_ids=replace_test_ids, count=count,
    )
    requests = _sharded_proposal_requests(
        request=request, persona=context.persona, shard_count=shards
    )
    state = load_persona_state(
        config=config, context=context, persona_state_path=persona_state_path
    )
    if manifest.get("persona_state") != {
        "path": str(state["path"]), "sha256": state["sha256"],
    }:
        raise ValueError("prepared planning request no longer matches its inputs")
    if (manifest.get("config_sha256") != _sha256(config_path)
            or manifest.get("checkpoint_identity") != context.checkpoint_identity
            or client.model != config.planner_model):
        raise ValueError("prepared planner identity or client no longer matches the config")
    if (
        manifest.get("ideas_per_shard") != count
        or manifest.get("planner_shard_count") != shards
        or manifest.get("planner_concurrency") != concurrency
    ):
        raise ValueError("prepared planner count, shards, or concurrency changed")
    schema = _idea_response_schema(count)
    prepared_requests: list[dict[str, Any]] = []
    for index, shard_request in enumerate(requests, start=1):
        directory = _planner_artifact_dir(
            out, shard_index=index, shard_count=shards
        )
        expected = _idea_request(shard_request)
        prepared = json.loads((directory / "planner_request.json").read_text())
        if prepared != expected:
            raise ValueError(f"prepared planner shard {index} no longer matches its inputs")
        if (
            (directory / "planner_system.txt").read_text()
            != IDEA_PLANNER_SYSTEM + "\n"
            or json.loads((directory / "planner_schema.json").read_text()) != schema
        ):
            raise ValueError(
                f"prepared planner shard {index} instructions or schema changed"
            )
        prepared_requests.append(prepared)

    def execute_shard(index: int) -> dict[str, Any]:
        directory = _planner_artifact_dir(
            out, shard_index=index, shard_count=shards
        )
        try:
            raw = cached_client_complete(
                directory / "work" / "planner_response_cache.json",
                IDEA_PLANNER_SYSTEM,
                prepared_requests[index - 1],
                client,
                response_schema=schema,
                response_schema_name=f"dolphinbench_test_ideas_v4_shard_{index:02d}",
            )
        except Exception as exc:
            result = {
                "index": index,
                "status": "failed",
                "failure_stage": "planner_call",
                "reason": f"{type(exc).__name__}: {exc}",
                "tasks": [],
            }
            dump_json(
                directory / "result.json",
                {key: value for key, value in result.items() if key != "tasks"},
            )
            return result
        dump_json(directory / "planner_raw_response.json", raw)
        try:
            response = IdeaResponse.model_validate(
                {key: value for key, value in raw.items() if not key.startswith("_")}
            )
            if len(response.ideas) > count:
                raise ValueError("planner returned more ideas than requested")
            if (len(response.ideas) < count) != bool(response.shortfall_reason.strip()):
                raise ValueError("planner must explain a shortfall, and only a shortfall")
            allowed_facts = {
                int(row["id"])
                for row in prepared_requests[index - 1]["current_facts"]
                if row.get("available_for_new_idea") is True
            }
            tasks = [
                ApprovedIdea(
                    authoring_version=4,
                    idea_id=(index - 1) * count + local_index,
                    **idea.model_dump(),
                )
                for local_index, idea in enumerate(response.ideas, start=1)
            ]
            for task in tasks:
                missing = sorted(set(task.fact_ids) - allowed_facts)
                if missing:
                    raise ValueError(
                        f"planner shard {index} used facts outside its assignment: {missing}"
                    )
            _validate_tasks(context=context, config=config, tasks=tasks)
            dump_json(directory / "proposals.json", response.model_dump(mode="json"))
            result = {
                "index": index,
                "status": "completed",
                "tasks": tasks,
                "shortfall_reason": response.shortfall_reason,
                "usage": raw.get("_usage") or {},
            }
            dump_json(
                directory / "result.json",
                {
                    **{key: value for key, value in result.items() if key != "tasks"},
                    "idea_ids": [task.idea_id for task in tasks],
                },
            )
            return result
        except Exception as exc:
            result = {
                "index": index,
                "status": "failed",
                "failure_stage": "planner_validation",
                "reason": f"{type(exc).__name__}: {exc}",
                "tasks": [],
                "usage": raw.get("_usage") or {},
            }
            dump_json(
                directory / "result.json",
                {key: value for key, value in result.items() if key != "tasks"},
            )
            return result

    results: list[dict[str, Any]] = []
    with _call_ledger(out / "call_ledger.jsonl"):
        with ThreadPoolExecutor(max_workers=min(concurrency, shards)) as executor:
            futures = {
                executor.submit(execute_shard, index): index
                for index in range(1, shards + 1)
            }
            for future in as_completed(futures):
                results.append(future.result())
    results.sort(key=lambda row: int(row["index"]))
    failed = [row for row in results if row["status"] == "failed"]
    if shards == 1 and failed:
        failed_row = failed[0]
        manifest.update(
            status="failed",
            failure_stage=failed_row["failure_stage"],
            failure_reason=failed_row["reason"],
            model_calls_made=1,
        )
        if failed_row["failure_stage"] == "planner_validation":
            manifest["calls"] = {
                "planner": {
                    "status": "completed",
                    "cache": "work/planner_response_cache.json",
                    "usage": failed_row.get("usage") or {},
                }
            }
        else:
            manifest["calls"] = {
                "planner": {
                    "status": "failed",
                    "cache": "work/planner_response_cache.json",
                }
            }
        dump_json(out / "manifest.json", manifest)
        raise ValueError(failed_row["reason"])

    tasks: list[ApprovedIdea] = []
    duplicate_ideas: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for row in results:
        for task in row["tasks"]:
            identity = json.dumps(
                task.model_dump(mode="json", exclude={"idea_id"}), sort_keys=True
            )
            if identity in seen:
                duplicate_ideas.append(
                    {"idea_id": task.idea_id, "duplicates_idea_id": seen[identity]}
                )
                continue
            seen[identity] = task.idea_id
            tasks.append(task)
    batch = CandidateTaskBatch(
        checkpoint_identity=context.checkpoint_identity,
        persona=context.persona,
        evaluation_date=config.evaluation_date,
        tasks=tasks,
    )
    dump_json(
        out / "proposals.json",
        {
            "shards": [
                {
                    "index": row["index"],
                    "status": row["status"],
                    "shortfall_reason": row.get("shortfall_reason", ""),
                    **(
                        {
                            "failure_stage": row["failure_stage"],
                            "reason": row["reason"],
                        }
                        if row["status"] == "failed"
                        else {}
                    ),
                }
                for row in results
            ],
            "ideas": [task.model_dump(mode="json") for task in tasks],
            "duplicate_ideas": duplicate_ideas,
        },
    )
    dump_json(out / "candidate_plan.json", batch.model_dump(mode="json"))
    manifest.update(
        status="awaiting_human_acceptance" if tasks else "failed",
        candidate_plan=str((out / "candidate_plan.json").resolve()),
        proposed_ideas=len(batch.tasks),
        model_calls_made=shards,
        failed_shards=[
            {
                "index": row["index"],
                "failure_stage": row["failure_stage"],
                "reason": row["reason"],
            }
            for row in failed
        ],
        duplicate_ideas=duplicate_ideas,
        planner_shards=[
            {
                "index": row["index"],
                "fact_ids": requests[int(row["index"]) - 1]["planning_shard"]["assigned_fact_ids"],
                "status": row["status"],
                "cache": str(
                    (
                        _planner_artifact_dir(
                            out,
                            shard_index=int(row["index"]),
                            shard_count=shards,
                        )
                        / "work"
                        / "planner_response_cache.json"
                    ).relative_to(out)
                ),
                **({"usage": row.get("usage") or {}} if row["status"] == "completed" else {}),
                **(
                    {
                        "failure_stage": row["failure_stage"],
                        "reason": row["reason"],
                    }
                    if row["status"] == "failed"
                    else {}
                ),
            }
            for row in results
        ],
    )
    if shards == 1:
        row = results[0]
        manifest["calls"] = {
            "planner": {
                "status": "completed",
                "cache": "work/planner_response_cache.json",
                "usage": row.get("usage") or {},
            }
        }
        manifest["shortfall_reason"] = row.get("shortfall_reason", "")
    manifest.pop("failure_stage", None)
    manifest.pop("failure_reason", None)
    dump_json(out / "manifest.json", manifest)
    return batch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--persona-state", type=Path)
    parser.add_argument("--replace-test-id", type=int, action="append", default=[])
    parser.add_argument(
        "--count",
        type=int,
        choices=range(1, 26),
        default=25,
        help="ideas requested from each planner shard",
    )
    parser.add_argument(
        "--shards",
        type=int,
        choices=range(1, MAX_PLANNER_SHARDS + 1),
        default=1,
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        choices=range(1, MAX_PLANNER_CONCURRENCY + 1),
        default=10,
        help="maximum simultaneous planner calls",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--confirm-paid-calls", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not (args.out / "manifest.json").is_file():
        prepare_proposals(
            config_path=args.config,
            persona_state_path=args.persona_state,
            replace_test_ids=args.replace_test_id,
            out=args.out,
            count=args.count,
            shards=args.shards,
            concurrency=args.concurrency,
        )
    if not args.confirm_paid_calls:
        print(json.dumps({"out": str(args.out), "prepared": True}, indent=2))
        return
    config = load_config(args.config)
    batch = execute_proposals(
        config_path=args.config,
        persona_state_path=args.persona_state,
        replace_test_ids=args.replace_test_id,
        out=args.out,
        client=AzureJsonClient(model=config.planner_model, reasoning_effort="high"),
        confirm_paid_calls=True,
        count=args.count,
        shards=args.shards,
        concurrency=args.concurrency,
    )
    print(json.dumps({"out": str(args.out), "proposals": len(batch.tasks)}, indent=2))


if __name__ == "__main__":
    main()
