"""Checkpointed stage queues behind authoring.create_tests --runtime-policy."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from authoring.complete_test import (
    authoring_evidence, certification_input, correction_scope, parse_writer_response, writer_payload,
)
from authoring.context import dump_json, load_authoring_tasks, load_checkpoint_context, load_config
from authoring.evidence_packets import prepare_evidence
from authoring.finalize_batch import write_completed_batch
from authoring.models import ApprovedIdea
from authoring.progress import authoring_artifacts, cached_authoring_response, save_progress
from authoring.runtime_policy import CreationPolicy, is_technical_error
from authoring.certify import certify_candidate, verify_gate_python


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _protocol_recovery(gate: dict, oracle_input: dict) -> bool:
    old = copy.deepcopy(gate.get("oracle_input", {}))
    settings = old.get("execution_settings", {})
    if settings.get("api", "chat_completions") != "chat_completions":
        return False
    settings["api"] = "responses"
    if old != oracle_input:
        return False
    shots = gate.get("result", {}).get("shots", [])
    return len(shots) == 4 and all(
        not shot.get("tool_calls") and not shot.get("response_text") and not shot.get("continuation_messages")
        and not any((shot.get("usage") or {}).values())
        and any("Function tools with reasoning_effort are not supported" in e
                and "/v1/chat/completions" in e for e in shot.get("oracle_errors", []))
        for shot in shots)


@dataclass
class Work:
    test_id: int
    task: ApprovedIdea
    context: Any
    directory: Path
    record: dict[str, Any]
    payload: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] | None = None
    corrections: int = 0
    production_corrections: int = 0
    execution_reruns: int = 0
    candidate: Path | None = None
    gate: Path | None = None
    prior_gate: Path | None = None
    attempt: str = "initial"
    seen_candidates: set[str] = field(default_factory=set)
    technical_attempts: dict[str, int] = field(default_factory=dict)
    final_result: dict[str, Any] | None = None
    status: str = "authoring_pending"


def resume_staged(*, config_path: Path, plan_path: Path, source_run: Path | None,
                  out: Path, test_ids: list[int], policy_path: Path,
                  confirm_paid_calls: bool) -> dict[str, Any]:
    from authoring import create_tests as api
    from authoring.final_trace_review import build_final_trace_review_request, execute_final_trace_review
    from authoring.propose import _state_test_summaries, load_persona_state, record_accepted_batch, record_rejected_tests, record_approved_plan
    from authoring.run import _author_complete_test_attempt, _certify_cached

    if not confirm_paid_calls:
        raise ValueError("staged creation requires explicit paid-call confirmation")
    policy = CreationPolicy.load(policy_path)
    if policy.grading_only_recovery and (policy.workflow != "production" or source_run is None):
        raise ValueError("grading-only recovery requires an authenticated saved production run")
    config = load_config(config_path)
    context = load_checkpoint_context(config)
    tasks = load_authoring_tasks(context=context, config=config, path=plan_path)
    state = load_persona_state(config=config, context=context)
    if source_run is not None and plan_path.resolve() not in state["approved_plan_paths"]:
        raise ValueError("staged creation requires a plan registered as explicitly approved")
    prior = _state_test_summaries(config=config, context=context, state=state)
    if set(test_ids).intersection(int(row["test_id"]) for row in prior):
        raise ValueError("staged creation cannot repeat a test already accepted in any working batch")
    source_run = source_run.resolve() if source_run is not None else None
    out = out.resolve()
    if out.exists() or (source_run is not None and (out == source_run or out.is_relative_to(source_run))):
        raise ValueError("staged output must be a fresh directory outside its source run")
    if not test_ids or len(set(test_ids)) != len(test_ids) or any(type(i) is not int or i <= 0 for i in test_ids):
        raise ValueError("staged creation needs distinct unfinished test IDs")
    if source_run is None:
        if policy.workflow != "production" or not 1 <= len(tasks) <= 250 or len(test_ids) != len(tasks):
            raise ValueError("fresh production creation requires one ID for each of one through 250 approved ideas")
        tasks_by_id = dict(zip(test_ids, tasks, strict=True))
        records = {test_id: {"progress": {"plan_position": position, "status": "input_pending"},
                            "authenticated_files": {}, "model_corrections_used": 0,
                            "execution_reruns_used": 0, "test_overrides": {}}
                   for position, test_id in enumerate(test_ids, 1)}
        source_identity = {"source_kind": "approved_plan", "config_path": str(config_path.resolve()),
                           "config_sha256": _hash(config_path), "plan_path": str(plan_path.resolve()),
                           "plan_sha256": _hash(plan_path), "checkpoint_identity": context.checkpoint_identity,
                           "selected_test_ids": test_ids, "authenticated_files": {
                               str(config_path.resolve()): _hash(config_path),
                               str(plan_path.resolve()): _hash(plan_path)}}
    else:
        verified = api._progress_resume_inputs(
            config_path=config_path, plan_path=plan_path, source_run=source_run,
            test_ids=test_ids, config=config, context=context, tasks=tasks,
        )
        if verified is None:
            raise ValueError("staged creation requires authenticated version-4 progress")
        records, tasks_by_id, source_identity = verified
    if any(not isinstance(task, ApprovedIdea) for task in tasks_by_id.values()):
        raise ValueError("staged creation supports only approved version-4 ideas")
    if any(record.get("test_overrides") for record in records.values()):
        raise ValueError("resume exact-repair permissions through their original workflow")
    verify_gate_python()
    if source_run is None:
        record_approved_plan(config=config, context=context, plan_path=plan_path)
    config = config.model_copy(update={"max_model_corrections": policy.max_model_corrections})
    out.mkdir(parents=True)
    policy_hash = _hash(policy_path)
    root = Path(__file__).resolve().parents[1]
    runtime_paths = [
        "authoring/production_review.py", "authoring/prompt_text/writer_production.md",
        "authoring/prompt_text/reviewer_production.md",
        "authoring/prompt_text/reviewer_grading_recovery.md",
        "authoring/staged_creation.py", "authoring/runtime_policy.py", "authoring/evidence_packets.py",
        "authoring/complete_test.py", "authoring/context.py", "authoring/models.py", "authoring/pipeline.py",
        "authoring/run.py", "authoring/review.py", "authoring/final_trace_review.py", "authoring/progress.py",
        "authoring/create_tests.py", "authoring/finalize_batch.py", "authoring/prompt_text/writer_v4.md", "authoring/prompt_text/reviewer_v4.md",
        "graders/explicit.py", "graders/mechanical.py", "graders/llm_judge.py",
        "harness/oracle.py", "harness/provider_capacity.py", "harness/mining/preview.py",
        "harness/oracle_responses.py", "harness/environment.py",
        "harness/paid_budget.py",
        "authoring/certify.py", "authoring/_gate_shot.py", "construction/llm.py", "construction/runtime_model_calls.py",
    ]
    runtime_hashes = {name: _hash(root / name) for name in runtime_paths}
    for name in runtime_paths:
        destination = out / "runtime_source" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, destination)
    inputs = out / "authoring" / "part_01" / "run_inputs.json"
    dump_json(inputs, {
        "config_path": str(config_path.resolve()), "config_sha256": _hash(config_path),
        "plan_path": str(plan_path.resolve()), "plan_sha256": _hash(plan_path),
        "checkpoint_identity": context.checkpoint_identity, "persona": config.persona,
        "evaluation_date": config.evaluation_date, "selected_test_ids": test_ids,
        "selected_plan_positions": [records[i]["progress"]["plan_position"] for i in test_ids],
        "runtime_policy": policy.model_dump(mode="json"), "runtime_policy_sha256": policy_hash,
        "runtime_source_sha256": runtime_hashes,
    })
    dump_json(out / "runtime_policy.json", policy.model_dump(mode="json"))
    dump_json(out / "resume_inputs.json", source_identity)
    old_env = {key: os.environ.get(key) for key in (
        "DOLPHINBENCH_CALL_LEDGER", "DOLPHINBENCH_CONSTRUCTION_RUN_ID", "DOLPHINBENCH_AUTHORING_PROVIDER_CAPACITY",
        "DOLPHINBENCH_AUTHORING_PROVIDER_SLOTS", "DOLPHINBENCH_AUTHORING_TRANSPORT_LEDGER",
        "DOLPHINBENCH_AUTHORING_PROVIDER_TPM", "DOLPHINBENCH_AUTHORING_PROVIDER_RPM",
        "DOLPHINBENCH_AUTHORING_PROVIDER_HEADER_FEEDBACK",
    )}
    os.environ["DOLPHINBENCH_CALL_LEDGER"] = str(out / "call_ledger.jsonl")
    os.environ["DOLPHINBENCH_CONSTRUCTION_RUN_ID"] = out.name
    os.environ["DOLPHINBENCH_AUTHORING_PROVIDER_CAPACITY"] = str(Path(policy.provider_capacity_directory).resolve())
    os.environ["DOLPHINBENCH_AUTHORING_PROVIDER_SLOTS"] = str(policy.provider_concurrency)
    os.environ["DOLPHINBENCH_AUTHORING_PROVIDER_TPM"] = str(policy.provider_tokens_per_minute)
    os.environ["DOLPHINBENCH_AUTHORING_PROVIDER_RPM"] = str(policy.provider_requests_per_minute)
    os.environ["DOLPHINBENCH_AUTHORING_PROVIDER_HEADER_FEEDBACK"] = "1" if policy.provider_header_feedback else "0"
    os.environ["DOLPHINBENCH_AUTHORING_TRANSPORT_LEDGER"] = str(out / "transport_ledger.jsonl")
    started = time.monotonic()
    work: dict[int, Work] = {}
    accepted: dict[int, Path] = {}
    events: list[dict[str, Any]] = []
    queues = {name: ThreadPoolExecutor(max_workers=getattr(policy, name + "_workers"))
              for name in ("preparation", "writing", "preflight", "certification", "review")}
    futures: dict[Any, tuple[str, Work, float]] = {}

    def save(w: Work, status: str, **extra: Any) -> None:
        w.status = status
        if w.payload:
            dump_json(w.directory / f"{w.attempt}_authoring_request.json", w.payload)
        artifacts = list(w.directory.rglob("*"))
        review_dir = out / "final_trace_reviews" / f"{w.test_id:03d}"
        artifacts.extend(review_dir.rglob("*"))
        artifacts.append(out / "runtime_policy.json")
        row = {"id": w.test_id, "fact_ids": w.task.fact_ids, "status": status,
               "workflow": policy.workflow,
               "candidate": str(w.candidate) if w.candidate else None,
               "gate": str(w.gate) if w.gate else None,
               "authoring_response": str(w.directory / f"{w.attempt}_authoring_response.json") if w.raw else None,
               "authoring_mode": "unified", "authoring_version": 4,
               "attempt_name": w.attempt, "model_corrections_used": w.corrections,
               "production_corrections_used": w.production_corrections,
               "execution_reruns_used": w.execution_reruns,
               "seen_candidate_sha256": sorted(w.seen_candidates),
               "technical_attempts": w.technical_attempts, **extra}
        save_progress(path=out / "progress" / f"{w.test_id:03d}.json", result=row,
                      inputs_path=inputs, artifact_paths=[p for p in artifacts if p.is_file()])

    def preparation(w: Work) -> tuple[str | None, dict]:
        files = w.record["authenticated_files"]
        progress = w.record["progress"]
        old_response = progress.get("authoring_response")
        recovered = None if old_response else cached_authoring_response(files, progress.get("attempt_name", "initial"))
        old_attempt = progress.get("attempt_name", "initial")
        saved_request = next((Path(p) for p in files if Path(p).name == f"{old_attempt}_authoring_request.json"), None)
        if old_response is None and recovered is None and saved_request is not None:
            payload = json.loads(saved_request.read_text())
            if payload.get("previous_authoring") or payload.get("evidence_selection"):
                artifacts = {p: digest for p, digest in files.items()
                             if Path(p).name not in {"progress.json", "status.json", "run_inputs.json"}}
                api._copy_authenticated_tree(saved_request.parent, w.directory, artifacts, model_caches=True)
                w.payload, w.attempt = payload, old_attempt
                original = payload.get("original_authoring_input", payload)
                if original.get("evidence_selection"):
                    packet_file = saved_request.parent / "evidence" / "packet.json"
                    if str(packet_file.resolve()) not in files:
                        raise ValueError("queued correction lost its authenticated source packet")
                    packet = json.loads(packet_file.read_text())["packet"]
                    w.context = replace(context, evidence_packets={tuple(sorted(w.task.fact_ids)): packet})
                if payload.get("saved_execution_gate"):
                    bound = payload["saved_execution_gate"]
                    w.prior_gate = w.directory / bound["file"]
                    if _hash(w.prior_gate) != bound["sha256"]:
                        raise ValueError("queued grading correction lost its saved executions")
                if policy.grading_only_recovery and w.prior_gate is None:
                    raise ValueError("grading-only recovery has no saved executions")
                return "writing", {}
        if old_response or recovered is not None:
            original_dir = Path(old_response).parent if old_response else recovered[1]
            artifacts = {p: digest for p, digest in files.items()
                         if Path(p).name not in {"progress.json", "status.json", "run_inputs.json"}}
            api._copy_authenticated_tree(original_dir, w.directory, artifacts, model_caches=True)
            old_attempt = progress.get("attempt_name", "initial")
            request_file = original_dir / f"{old_attempt}_authoring_request.json"
            if str(request_file.resolve()) not in files:
                raise ValueError("a completed writer response lacks its authenticated request")
            w.payload = json.loads(request_file.read_text())
            if w.payload.get("saved_execution_gate"):
                bound = w.payload["saved_execution_gate"]
                w.prior_gate = w.directory / bound["file"]
                if _hash(w.prior_gate) != bound["sha256"]:
                    raise ValueError("saved grading correction lost its execution binding")
            w.raw = json.loads(Path(old_response).read_text()) if old_response else recovered[0]
            w.attempt = old_attempt
            original = w.payload.get("original_authoring_input", w.payload)
            if original.get("evidence_selection"):
                packet_file = original_dir / "evidence" / "packet.json"
                if str(packet_file.resolve()) not in files:
                    raise ValueError("saved staged writing has no authenticated evidence packet")
                packet = json.loads(packet_file.read_text())["packet"]
                w.context = replace(context, evidence_packets={tuple(sorted(w.task.fact_ids)): packet})
            if policy.workflow == "production":
                if policy.grading_only_recovery:
                    if progress.get("workflow") != "production":
                        raise ValueError("grading-only recovery cannot migrate historical work")
                    if w.record.get("gate") and w.record.get("candidate"):
                        w.candidate = w.directory / Path(w.record["candidate"]).name
                        w.gate = w.directory / Path(w.record["gate"]).name
                        for key in ("candidate", "gate"):
                            shutil.copy2(w.record[key], getattr(w, key))
                        expected = certification_input(w.context, w.task, config.evaluation_date)
                        expected["execution_settings"] = policy.oracle_settings()
                        if json.loads(w.gate.read_text()).get("oracle_input") != expected:
                            raise ValueError("grading-only recovery cannot change history or agent settings")
                        return "review", {}
                    if w.prior_gate is not None:
                        return "preflight", {}
                    raise ValueError("grading-only recovery requires a saved candidate and four attempts")
                if progress.get("workflow") != "production":
                    w.attempt = old_attempt + "_production"
                    for suffix in ("request.json", "schema.json", "system.txt", "response.json"):
                        original_file = w.directory / f"{old_attempt}_authoring_{suffix}"
                        if original_file.is_file():
                            shutil.copy2(original_file, w.directory / f"{w.attempt}_authoring_{suffix}")
                if w.raw.get("status") == "cannot_write" and w.payload.get("previous_authoring"):
                    previous = w.payload["previous_authoring"]
                    issues = (w.payload.get("failure_to_fix") or {}).get("issues")
                    if previous.get("status") == "written" and issues:
                        w.raw = copy.deepcopy(previous)
                        return scoped_correction(w, issues, "Complete the saved correction with its required evidence updates.")
                if w.record.get("gate"):
                    w.gate = w.directory / Path(w.record["gate"]).name
                    shutil.copy2(w.record["gate"], w.gate)
                return "preflight", {}
            if w.record.get("gate"):
                w.candidate = w.directory / Path(w.record["candidate"]).name
                w.gate = w.directory / Path(w.record["gate"]).name
                for key in ("candidate", "gate"):
                    shutil.copy2(w.record[key], getattr(w, key))
                expected = certification_input(w.context, w.task, config.evaluation_date)
                expected["execution_settings"] = policy.oracle_settings()
                if _protocol_recovery(json.loads(w.gate.read_text()), expected):
                    w.attempt += "_protocol_recovery"
                    shutil.copytree(w.directory / f"{old_attempt}_preflight", w.directory / f"{w.attempt}_preflight")
                    shutil.copy2(w.directory / f"{old_attempt}_authoring_response.json",
                                 w.directory / f"{w.attempt}_authoring_response.json")
                    w.gate = None
                    return "certification", {}
                return "review", {}
            if w.record.get("candidate"):
                w.candidate = w.directory / Path(w.record["candidate"]).name
                shutil.copy2(w.record["candidate"], w.candidate)
                w.seen_candidates.add(_hash(w.candidate))
            return "preflight", {}
        if policy.grading_only_recovery:
            raise ValueError("grading-only recovery cannot create a new draft or source packet")
        if policy.prepare_evidence:
            w.context = prepare_evidence(context=context, idea=w.task,
                                         evaluation_date=config.evaluation_date,
                                         out=w.directory / "evidence", client=policy.client(config.designer_model),
                                         cache_root=Path(policy.evidence_cache_directory).resolve())
        w.payload = writer_payload(config=config, context=w.context, idea=w.task)
        if policy.workflow == "production":
            w.payload["workflow"] = "production"
        if policy.action_batched_judge:
            w.payload["semantic_judge_version"] = 2
        return "writing", {}

    def writing(w: Work) -> tuple[str | None, dict]:
        if policy.grading_only_recovery and (w.prior_gate is None or not w.payload.get("previous_authoring")):
            raise ValueError("grading-only recovery permits only a scoped correction with saved attempts")
        _, attempt = _author_complete_test_attempt(
            directory=w.directory, attempt_name=w.attempt, payload=w.payload,
            planned_task=w.task, context=w.context, config=config, test_id=w.test_id,
            client=policy.client(config.designer_model, writing=True), certifier=certify_candidate,
            model_corrections_used=w.corrections, stop_after_writing=True,
            validation_options={"workflow": policy.workflow},
        )
        response = w.directory / f"{w.attempt}_authoring_response.json"
        if response.is_file():
            w.raw = json.loads(response.read_text())
        if attempt.get("candidate"):
            w.candidate = Path(attempt["candidate"])
        if attempt["stage"] == "preflight_pending" and w.candidate:
            fingerprint = _hash(w.candidate)
            if fingerprint in w.seen_candidates:
                return None, {"status": "correction_pending", "reason": "correction repeated a previous candidate"}
            w.seen_candidates.add(fingerprint)
            return ("certification" if policy.workflow == "production" else "preflight"), {}
        return None, {"status": attempt["stage"], "reason": attempt.get("reason"), "failure": attempt}

    def scoped_correction(w: Work, issues: list[dict], reason: str) -> tuple[str | None, dict]:
        if policy.grading_only_recovery and any(issue["part"] not in {"checks", "evidence"} for issue in issues):
            return None, {"status": "correction_pending", "reason": reason,
                          "next_action": "requires_change_outside_approved_grading_scope"}
        if w.corrections >= policy.max_model_corrections:
            return None, {"status": "correction_pending", "reason": reason}
        if policy.workflow == "production" and w.production_corrections >= 1:
            return None, {"status": "correction_pending", "reason": reason}
        response = parse_writer_response(w.raw)
        if policy.workflow == "production":
            from authoring.production_review import dependent_scope
            parts = {issue["part"] for issue in issues}
            if all("check_ids" in issue for issue in issues):
                check_ids = {cid for issue in issues for cid in issue["check_ids"]}
            else:
                old_scope = correction_scope(issues, response)
                check_ids = {row.assertion["check_id"] for row in response.test.checks} - set(old_scope.get("protected_check_ids", []))
            scope = dependent_scope(response, parts=parts, check_ids=check_ids)
        else:
            scope = correction_scope(issues, response)
        if not scope.get("allowed_correction_fields"):
            return None, {"status": "correction_pending", "reason": "failure has no valid field correction scope"}
        original = w.payload.get("original_authoring_input", w.payload)
        if policy.workflow == "production":
            original = {**original, "workflow": "production"}
        w.payload = {"original_authoring_input": original, "previous_authoring": w.raw,
                     **scope, "failure_to_fix": {"issues": issues, "reason": reason}}
        if set(scope["allowed_correction_fields"]).issubset({"test.checks", "test.evidence"}) and w.gate:
            w.prior_gate = w.gate
            w.payload["saved_execution_gate"] = {"file": w.gate.name, "sha256": _hash(w.gate)}
        else:
            w.prior_gate = None
        w.corrections += 1
        if policy.workflow == "production":
            w.production_corrections += 1
        w.attempt = f"correction_{w.corrections}"
        w.raw = None
        w.candidate = w.gate = None
        return "writing", {}

    def preflight(w: Work) -> tuple[str | None, dict]:
        if policy.workflow == "production":
            saved_gate = w.gate
            _, attempt = _author_complete_test_attempt(
                directory=w.directory, attempt_name=w.attempt, payload=w.payload,
                planned_task=w.task, context=w.context, config=config, test_id=w.test_id,
                client=policy.client(config.designer_model), certifier=certify_candidate,
                model_corrections_used=w.corrections, reuse_authoring=w.raw, stop_after_writing=True,
                validation_options={"workflow": "production"},
            )
            if attempt.get("candidate") and attempt["stage"] == "preflight_pending":
                w.candidate = Path(attempt["candidate"])
                w.seen_candidates.add(_hash(w.candidate))
                expected = certification_input(w.context, w.task, config.evaluation_date)
                expected["execution_settings"] = policy.oracle_settings()
                if saved_gate:
                    gate = json.loads(saved_gate.read_text())
                    if gate.get("candidate_sha256") == _hash(w.candidate) and gate.get("oracle_input") == expected:
                        return "review", {}
                if policy.grading_only_recovery:
                    if w.prior_gate is not None:
                        return "certification", {}
                    raise ValueError("grading-only recovery cannot replace unmatched executions")
                w.gate = None
                w.prior_gate = None
                return "certification", {}
            return None, {"status": attempt["stage"], "reason": attempt.get("reason"), "failure": attempt}
        _, attempt = _author_complete_test_attempt(
            directory=w.directory, attempt_name=w.attempt, payload=w.payload,
            planned_task=w.task, context=w.context, config=config, test_id=w.test_id,
            client=policy.client(config.designer_model), certifier=certify_candidate,
            model_corrections_used=w.corrections, reuse_authoring=w.raw, stop_after_preflight=True,
        )
        if attempt.get("candidate"):
            w.candidate = Path(attempt["candidate"])
        if attempt["stage"] == "certification_pending":
            return "certification", {}
        if attempt["stage"] == "preflight_rejected":
            response = json.loads((w.directory / f"{w.attempt}_preflight" / "response.json").read_text())
            if any(row["part"] == "planning" for row in response["issues"]):
                return None, {"status": "replacement_required", "reason": attempt.get("reason")}
            return scoped_correction(w, response["issues"], attempt.get("reason", "preflight rejected"))
        return None, {"status": attempt["stage"], "reason": attempt.get("reason"), "failure": attempt}

    def certification(w: Work) -> tuple[str | None, dict]:
        oracle_input = certification_input(w.context, w.task, config.evaluation_date)
        oracle_input["execution_settings"] = policy.oracle_settings()
        evidence = authoring_evidence(parse_writer_response(w.raw))
        target = w.directory / f"{w.attempt}_gate.json"
        if w.prior_gate is not None:
            old = json.loads(w.prior_gate.read_text())
            if old.get("oracle_input") != oracle_input:
                raise ValueError("grading-only repair cannot change history or reference-agent settings")
            if policy.grading_only_recovery:
                import yaml
                previous_path = w.prior_gate.with_name(w.prior_gate.name.removesuffix("_gate.json") + "_candidate.yaml")
                previous = yaml.safe_load(previous_path.read_text())
                current = yaml.safe_load(w.candidate.read_text())
                if _hash(previous_path) != old.get("candidate_sha256"):
                    raise ValueError("saved execution candidate binding changed")
                previous.pop("grade", None)
                current.pop("grade", None)
                if previous != current:
                    raise ValueError("grading-only recovery changed a non-grading candidate field")
            gate = api._regraded_gate(candidate_path=w.candidate, saved_gate_path=w.prior_gate,
                                     regrader=api._default_regrader)
            gate.update(authoring_evidence=evidence, oracle_input=oracle_input)
            dump_json(target, gate)
        else:
            if policy.grading_only_recovery:
                raise ValueError("grading-only recovery cannot execute the assistant")
            def certifier(*args: Any, **kwargs: Any) -> dict:
                return certify_candidate(*args, **kwargs, max_technical_retries=policy.technical_retries)
            if w.test_id in policy.baseline_probe_ids and w.execution_reruns == 0 and w.corrections == 0:
                baseline = copy.deepcopy(oracle_input)
                baseline["execution_settings"]["model"] = "gpt-5.4"
                _certify_cached(persona=config.persona, checkpoint=context.checkpoint_path,
                                candidate_path=w.candidate, result_path=w.directory / "baseline_probe_gate.json",
                                certifier=certifier, oracle_input=baseline, authoring_evidence=evidence)
            _certify_cached(persona=config.persona, checkpoint=context.checkpoint_path,
                            candidate_path=w.candidate, result_path=target, certifier=certifier,
                            oracle_input=oracle_input, authoring_evidence=evidence)
        w.gate = target
        return "review", {}

    def review(w: Work) -> tuple[str | None, dict]:
        if policy.workflow == "production":
            from authoring.production_review import execute_production_review, validate_survivor
            from authoring.final_trace_review import FinalTraceDecision
            request = build_final_trace_review_request(candidate_path=w.candidate, certification_gate_path=w.gate,
                                                      planned_task=w.task, context=w.context)
            try:
                evidence = validate_survivor(request)
            except ValueError as exc:
                if not policy.grading_only_recovery or w.production_corrections >= 1 or w.corrections >= policy.max_model_corrections:
                    return None, {"status": "certification_pending", "reason": str(exc), "next_action": "park_failed_candidate"}
                evidence = None
            review_dir = out / "final_trace_reviews" / f"{w.test_id:03d}" / w.attempt
            decision = execute_production_review(request=request, out=review_dir,
                                                 client=policy.client(config.designer_model),
                                                 inspect_failed_grading=evidence is None)
            if decision.decision == "approve":
                if evidence is None:
                    return None, {"status": "review_pending", "reason": "failed saved attempts cannot be approved unchanged"}
                w.final_result = {"id": w.test_id, "status": "accepted_initial_final_review",
                    "candidate": str(w.candidate), "gate": str(w.gate), "candidate_sha256": _hash(w.candidate),
                    "initial_final_review": FinalTraceDecision(test_id=w.test_id, accept=True).model_dump(mode="json"),
                    "production_review": str(review_dir / "decision.json"), "certification_evidence": evidence,
                    "model_corrections_used": w.corrections, "production_corrections_used": w.production_corrections}
                return None, w.final_result
            issues = [issue.model_dump(mode="json") for issue in decision.issues]
            reason = "; ".join(issue.problem for issue in decision.issues)
            if decision.decision == "pending":
                return None, {"status": "review_pending", "reason": reason}
            if decision.decision == "reject" or any(issue.part == "planning" for issue in decision.issues):
                return None, {"status": "replacement_required", "reason": reason}
            if any(issue.part == "system" for issue in decision.issues):
                return None, {"status": "review_pending", "reason": reason}
            return scoped_correction(w, issues, reason)
        clean, evidence = api._clean_certification_evidence(candidate=w.candidate, gate=w.gate, planned_task=w.task)
        if clean:
            w.final_result = {"id": w.test_id, "status": "accepted_clean_certification",
                              "candidate": str(w.candidate), "gate": str(w.gate),
                              "candidate_sha256": _hash(w.candidate), "clean_certification": evidence,
                              "model_corrections_used": w.corrections, "execution_reruns_used": w.execution_reruns,
                              "final_model_review": "not_required"}
            return None, w.final_result
        request = build_final_trace_review_request(candidate_path=w.candidate, certification_gate_path=w.gate,
                                                  planned_task=w.task, context=w.context)
        review_dir = out / "final_trace_reviews" / f"{w.test_id:03d}" / w.attempt
        decision = execute_final_trace_review(request=request, out=review_dir,
                                             client=policy.client(config.designer_model), confirm_paid_calls=True)
        if decision.accept:
            w.final_result = {"id": w.test_id, "status": "accepted_initial_final_review",
                              "candidate": str(w.candidate), "gate": str(w.gate),
                              "candidate_sha256": _hash(w.candidate), "initial_final_review": decision.model_dump(mode="json")}
            return None, w.final_result
        raw = json.loads((review_dir / "response.json").read_text())
        parts = {issue["part"] for issue in raw["issues"]}
        if "planning" in parts:
            return None, {"status": "replacement_required", "reason": decision.specific_problem}
        if parts == {"execution"}:
            if w.execution_reruns:
                return None, {"status": "replacement_required", "reason": decision.specific_problem}
            w.execution_reruns = 1
            w.attempt += "_execution_retry"
            # The unchanged retry reuses the authenticated preflight, not the model review.
            previous = w.gate.stem.removesuffix("_gate")
            shutil.copytree(w.directory / f"{previous}_preflight", w.directory / f"{w.attempt}_preflight")
            shutil.copy2(w.directory / f"{previous}_authoring_response.json", w.directory / f"{w.attempt}_authoring_response.json")
            return "certification", {}
        return scoped_correction(w, raw["issues"], decision.specific_problem)

    stages = {"preparation": preparation, "writing": writing, "preflight": preflight,
              "certification": certification, "review": review}

    def submit(stage: str, w: Work) -> None:
        save(w, {"preparation": "authoring_pending", "writing": "authoring_pending",
                 "preflight": "preflight_pending", "certification": "certification_pending",
                 "review": "review_pending"}[stage])
        futures[queues[stage].submit(stages[stage], w)] = (stage, w, time.monotonic())

    try:
        for test_id, record in records.items():
            directory = inputs.parent / "proposals" / f"{test_id:03d}"
            directory.mkdir(parents=True)
            w = Work(test_id, tasks_by_id[test_id], context, directory, record,
                     corrections=record["model_corrections_used"], execution_reruns=record["execution_reruns_used"],
                     production_corrections=record["progress"].get("production_corrections_used", 0),
                     seen_candidates=set(record["progress"].get("seen_candidate_sha256", [])),
                     technical_attempts=dict(record["progress"].get("technical_attempts", {})))
            work[test_id] = w
            submit("preparation", w)
        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                stage, w, submitted = futures.pop(future)
                try:
                    next_stage, result = future.result()
                except Exception as exc:
                    next_stage, result = None, {"status": stage + "_pending", "reason": f"{type(exc).__name__}: {exc}",
                                                "technical_error": is_technical_error(exc)}
                technical = result.get("technical_error") or (result.get("failure") or {}).get("technical_error")
                retry_key = f"{w.attempt}:{stage}"
                if technical and w.technical_attempts.get(retry_key, 0) < policy.technical_retries:
                    w.technical_attempts[retry_key] = w.technical_attempts.get(retry_key, 0) + 1
                    next_stage = stage
                events.append({"test_id": w.test_id, "stage": "validation" if policy.workflow == "production" and stage == "preflight" else stage,
                               "elapsed_including_queue_seconds": round(time.monotonic() - submitted, 3),
                               "next_stage": next_stage, "result": result})
                dump_json(out / "stage_events.json", events)
                if next_stage is not None:
                    submit(next_stage, w)
                    continue
                result = copy.deepcopy(result)
                status = result.pop("status", stage + "_pending")
                # Technical failures are pending, never evidence against an idea.
                save(w, status, **{k: v for k, v in result.items() if k not in {"id", "candidate", "gate"}})
                if status.startswith("accepted_"):
                    accepted[w.test_id] = w.candidate
                elif not status.endswith("_pending") and status != "replacement_required":
                    save(w, "authoring_pending", reason=result.get("reason", status))
        api._verify_saved_resume_files(source_identity)
        if _hash(policy_path) != policy_hash or any(_hash(root / name) != digest for name, digest in runtime_hashes.items()):
            raise ValueError("staged runtime or policy changed during execution; completed evidence is preserved for inspection")
    finally:
        for queue in queues.values():
            queue.shutdown(wait=True)
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    authored = [json.loads((out / "progress" / f"{i:03d}.json").read_text()) for i in sorted(work)]
    reviewed = [w.final_result for w in work.values() if w.final_result is not None]
    manifest = {"checkpoint_identity": context.checkpoint_identity, "plan_path": str(plan_path.resolve()),
                "test_ids": test_ids, "candidate_count": len(test_ids), "resume_new_test_run": source_identity,
                "runtime_policy": policy.model_dump(mode="json"), "runtime_policy_sha256": policy_hash,
                "runtime_source_sha256": runtime_hashes,
                "final_review_accepted": len(accepted), "authoring_results": authored,
                "final_review_results": reviewed, "promoted": False,
                "elapsed_seconds": round(time.monotonic() - started, 3)}
    dump_json(out / "manifest.json", manifest)
    candidates, _ = write_completed_batch(
        out=out, plan_path=plan_path, tasks_by_id=tasks_by_id, accepted=accepted,
        checkpoint_identity=context.checkpoint_identity, persona=context.persona,
        evaluation_date=config.evaluation_date, resumed_from=source_identity,
        authoring_results=authored, final_review_results=reviewed,
    )
    manifest.update(accepted_candidates=candidates,
                    accepted_batch=str(out / "accepted_batch") if accepted else None,
                    rejected_tests=str(out / "rejected_tests.json"))
    dump_json(out / "manifest.json", manifest)
    if accepted:
        record_accepted_batch(config=config, context=context, directory=out / "accepted_batch")
    record_rejected_tests(config=config, context=context, rejected_path=out / "rejected_tests.json")
    return manifest
