"""Package authenticated completed acceptances without any provider calls."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from authoring.context import dump_json, load_authoring_tasks, load_checkpoint_context, load_config
from authoring.models import ApprovedIdea, CandidateTaskBatch, PlannedTask


def _failure_reason(
    *, authoring_result: dict[str, Any] | None, final_review_result: dict[str, Any] | None
) -> str:
    def text_at(value: Any, *path: str) -> str:
        for key in path:
            if not isinstance(value, dict):
                return ""
            value = value.get(key)
        return value.strip() if isinstance(value, str) else ""

    generic = {
        "proposal_rejected",
        "replacement_required",
        "replacement_required_after_one_correction",
    }
    if final_review_result is not None:
        for path in (
            ("corrected_final_review", "specific_problem"),
            ("correction", "reason"),
            ("initial_final_review", "specific_problem"),
        ):
            value = text_at(final_review_result, *path)
            if value and value not in generic:
                return value
    if authoring_result is not None:
        for path in (
            ("proposal_rejected", "reason"),
            ("redesign_failure", "preflight", "decision", "specific_problem"),
            ("redesign_failure", "reason"),
            ("first_failure", "preflight", "decision", "specific_problem"),
            ("first_failure", "reason"),
            ("preflight", "decision", "specific_problem"),
            ("reason",),
            ("stage",),
            ("status",),
        ):
            value = text_at(authoring_result, *path)
            if value and value not in generic:
                return value
    return "The candidate did not pass certification and final trace review."


def _write_accepted_batch(
    *, directory: Path, checkpoint_identity: str, persona: str, evaluation_date: str,
    tasks_by_id: dict[int, PlannedTask | ApprovedIdea], accepted: dict[int, Path],
    statuses: dict[int, str], source_manifest: Path,
    resumed_from: dict[str, Any] | None = None, preserve_metadata: bool = True,
) -> list[dict[str, Any]]:
    candidates_dir = directory / "candidates"
    candidates_dir.mkdir(parents=True, exist_ok=preserve_metadata)
    copy_candidate = shutil.copy2 if preserve_metadata else shutil.copyfile
    ids = sorted(accepted)
    results = []
    for test_id in ids:
        destination = candidates_dir / f"{test_id:03d}.yaml"
        copy_candidate(accepted[test_id], destination)
        results.append({
            "test_id": test_id, "status": statuses[test_id],
            "candidate": str(destination.resolve()),
            "candidate_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        })
    batch = CandidateTaskBatch(
        checkpoint_identity=checkpoint_identity, persona=persona,
        evaluation_date=evaluation_date, tasks=[tasks_by_id[i] for i in ids],
    )
    dump_json(directory / "planning_batch.json", batch.model_dump(mode="json"))
    provenance = {
        "checkpoint_identity": checkpoint_identity, "accepted_test_ids": ids,
        "source_manifest": str(source_manifest.resolve()), "results": results,
    }
    if resumed_from is not None:
        provenance["resumed_from"] = resumed_from
    dump_json(directory / "provenance.json", provenance)
    return results


def write_repair_batches(
    *, out: Path, context: Any, config: Any, tasks_by_id: dict[int, PlannedTask],
    accepted: dict[int, Path], accepted_statuses: dict[int, str],
    source_manifest: Path, preserve_existing: bool = False,
) -> list[Path]:
    """Package repairs in groups of ten, retaining prior batches on continuation."""
    batches_root = out / "accepted_batches"
    if batches_root.exists() and not preserve_existing:
        shutil.rmtree(batches_root)
    batch_dirs = (
        sorted(path for path in batches_root.glob("*") if path.is_dir())
        if preserve_existing else []
    )
    prefix = "continuation" if preserve_existing else "part"
    next_number = 1
    while (batches_root / f"{prefix}_{next_number:02d}").exists():
        next_number += 1
    for batch_number, start in enumerate(range(0, len(accepted), 10), start=1):
        ids = sorted(accepted)[start : start + 10]
        batch_dir = batches_root / f"{prefix}_{next_number + batch_number - 1:02d}"
        _write_accepted_batch(
            directory=batch_dir, checkpoint_identity=context.checkpoint_identity,
            persona=context.persona, evaluation_date=config.evaluation_date,
            tasks_by_id=tasks_by_id, accepted={i: accepted[i] for i in ids},
            statuses=accepted_statuses, source_manifest=source_manifest,
            preserve_metadata=False,
        )
        batch_dirs.append(batch_dir)
    return batch_dirs


def write_completed_batch(
    *,
    out: Path,
    plan_path: Path,
    checkpoint_identity: str,
    persona: str,
    evaluation_date: str,
    tasks_by_id: dict[int, PlannedTask | ApprovedIdea],
    accepted: dict[int, Path],
    authoring_results: list[dict[str, Any]],
    final_review_results: list[dict[str, Any]],
    resumed_from: dict[str, Any] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Write accepted tests for the next planner and failures for replacement planning."""
    manifest_path = (out / "manifest.json").resolve()
    final_by_id = {int(row["id"]): row for row in final_review_results}
    authoring_by_id = {int(row["id"]): row for row in authoring_results}
    accepted_ids = sorted(accepted)
    accepted_candidate_paths: list[str] = []
    provenance_results: list[dict[str, Any]] = []
    if accepted_ids:
        for test_id in accepted_ids:
            status = str(final_by_id[test_id]["status"])
            if status not in {
                "accepted_clean_certification",
                "accepted_initial_final_review",
                "accepted_after_final_review_correction",
                "accepted_after_same_facts_redesign",
            }:
                raise ValueError(f"accepted test {test_id} has invalid completion status")

    rejected_tests = []
    pending_tests = []
    for test_id in sorted(tasks_by_id):
        if test_id in accepted:
            continue
        final_result = final_by_id.get(test_id) or {}
        authoring_result = authoring_by_id.get(test_id) or {}
        pending = final_result if str(final_result.get("status") or "").endswith("_pending") else authoring_result
        if str(pending.get("status") or "").endswith("_pending"):
            pending_tests.append(copy.deepcopy(pending))
            continue
        rejected_tests.append(
            {
                "test_id": test_id,
                "candidate_plan": tasks_by_id[test_id].model_dump(mode="json"),
                "failure_reason": _failure_reason(
                    authoring_result=authoring_by_id.get(test_id),
                    final_review_result=final_by_id.get(test_id),
                ),
                "candidate_plan_path": str(plan_path.resolve()),
                "failure_manifest_path": str(manifest_path),
            }
        )
    dump_json(out / "rejected_tests.json", {"rejected_tests": rejected_tests})
    dump_json(out / "pending_tests.json", {"pending_tests": pending_tests})
    if accepted_ids:
        provenance_results = _write_accepted_batch(
            directory=out / "accepted_batch", checkpoint_identity=checkpoint_identity,
            persona=persona, evaluation_date=evaluation_date, tasks_by_id=tasks_by_id,
            accepted=accepted, statuses={i: str(final_by_id[i]["status"]) for i in accepted_ids},
            source_manifest=manifest_path, resumed_from=resumed_from,
        )
        accepted_candidate_paths = [row["candidate"] for row in provenance_results]
    return accepted_candidate_paths, provenance_results


def finalize_accepted(*, config_path: Path, plan_path: Path, source_run: Path,
                      out: Path, test_ids: list[int]) -> dict:
    from authoring import create_tests as api
    from authoring.propose import _state_test_summaries, load_persona_state, record_accepted_batch

    config = load_config(config_path)
    context = load_checkpoint_context(config)
    tasks = load_authoring_tasks(context=context, config=config, path=plan_path)
    source_run, out = source_run.resolve(), out.resolve()
    if out.exists() or out == source_run or out.is_relative_to(source_run):
        raise ValueError("finalization needs a fresh output directory outside its source run")
    if not test_ids or len(test_ids) != len(set(test_ids)):
        raise ValueError("finalization requires distinct completed test IDs")
    state = load_persona_state(config=config, context=context)
    if plan_path.resolve() not in state["approved_plan_paths"]:
        raise ValueError("finalization requires the registered approved plan")
    previous = _state_test_summaries(config=config, context=context, state=state)
    if set(test_ids).intersection(row["test_id"] for row in previous):
        raise ValueError("a selected acceptance is already registered")
    verified = api._progress_resume_inputs(config_path=config_path, plan_path=plan_path, source_run=source_run,
        test_ids=test_ids, config=config, context=context, tasks=tasks)
    if verified is None:
        raise ValueError("finalization requires authenticated per-test completion records")
    records, selected, identity = verified
    accepted, completed = {}, []
    for test_id, record in records.items():
        progress = record["progress"]
        if progress["status"] not in {"accepted_clean_certification", "accepted_initial_final_review",
                                      "accepted_after_final_review_correction"}:
            raise ValueError(f"test {test_id} has no completed acceptance")
        candidate, gate = record.get("candidate"), record.get("gate")
        if candidate is None or gate is None or progress.get("candidate_sha256") != api._sha256(candidate):
            raise ValueError(f"test {test_id} has no matching accepted candidate and gate")
        clean, evidence = api._clean_certification_evidence(candidate=candidate, gate=gate, planned_task=selected[test_id])
        if not clean:
            from authoring.progress import load_progress
            for path in (source_run / "authoring").glob(f"part_*/proposals/{test_id:03d}/progress.json"):
                previous = load_progress(source_run=source_run, test_id=test_id, config_path=config_path,
                    plan_path=plan_path, config=config, context=context, tasks=tasks, record_path=path)
                if previous is None:
                    continue
                old, files = previous
                old_candidate = str(Path(old.get("candidate") or "").resolve())
                old_gate = str(Path(old.get("gate") or "").resolve())
                if (old.get("authoring_inputs_sha256") != progress["authoring_inputs_sha256"]
                        or record["authenticated_files"].get(old_candidate) != api._sha256(candidate)
                        or old_gate not in record["authenticated_files"]
                        or files.get(old_gate) != record["authenticated_files"][old_gate]):
                    continue
                if any(key in record["authenticated_files"] and record["authenticated_files"][key] != digest
                       for key, digest in files.items()):
                    raise ValueError("completed acceptance and predecessor progress disagree")
                record["authenticated_files"].update(files)
                identity["authenticated_files"].update(files)
            # An unchanged execution retry can inherit its original preflight.
            for value in record["authenticated_files"]:
                path = Path(value)
                if path.name != "result.json" or not path.parent.name.endswith("_preflight"):
                    continue
                required = [path.parent / name for name in (
                    "decision.json", "request.json", "result.json", "grading_examples/result.json")]
                if not all(str(p.resolve()) in record["authenticated_files"] for p in required):
                    continue
                preflight_gate = path.parent.with_name(path.parent.name.removesuffix("_preflight") + "_gate.json")
                clean, evidence = api._clean_certification_evidence(candidate=candidate, gate=gate,
                    planned_task=selected[test_id], preflight_gate=preflight_gate)
                if clean:
                    evidence["inherited_preflight"] = str(path.parent)
                    break
        if not clean:
            raise ValueError(f"test {test_id} needs review; local finalization cannot change acceptance: {evidence['reasons']}")
        saved = progress.get("corrected_clean_certification") or progress.get("clean_certification") or {}
        if (saved.get("outcome") != "accepted_without_final_model_review" or saved.get("reasons")
                or saved.get("candidate_sha256") != api._sha256(candidate)):
            raise ValueError(f"test {test_id} has no authenticated completed clean decision")
        accepted[test_id] = candidate
        completed.append({**progress, "local_finalization_evidence": evidence})
    api._verify_saved_resume_files(identity)
    out.mkdir(parents=True)
    manifest = {"persona": config.persona, "checkpoint_identity": context.checkpoint_identity,
                "plan_path": str(plan_path.resolve()), "test_ids": test_ids, "candidate_count": len(test_ids),
                "final_review_accepted": len(accepted), "authoring_results": completed,
                "final_review_results": completed, "resume_new_test_run": identity,
                "local_only": True, "provider_calls": 0, "promoted": False}
    dump_json(out / "manifest.json", manifest)
    candidates, _ = write_completed_batch(
        out=out, plan_path=plan_path, tasks_by_id=selected, accepted=accepted,
        checkpoint_identity=context.checkpoint_identity, persona=config.persona,
        evaluation_date=config.evaluation_date, resumed_from=identity,
        authoring_results=completed, final_review_results=completed,
    )
    batch = out / "accepted_batch"
    manifest.update(accepted_candidates=candidates, accepted_batch=str(batch),
                    rejected_tests=str(out / "rejected_tests.json"))
    dump_json(out / "manifest.json", manifest)
    api._verify_saved_resume_files(identity)
    record_accepted_batch(config=config, context=context, directory=batch)
    return manifest
