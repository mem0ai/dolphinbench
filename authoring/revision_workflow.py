"""Internal, at-most-once stage ordering for exact release validation."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any, Callable

import yaml

from authoring.revision_execution import _atomic_json, grade_saved_attempts, read_bound, run_once
from authoring.revision_review import (
    RevisionReviewResponse, check_revision_examples, execute_revision_review,
    grading_identity, validate_revision_certification,
)


def validate_revision_test(
    *, candidate_path: Path, request: dict[str, Any], saved_gate: dict[str, Any] | None,
    oracle_input: dict[str, Any] | None, out: Path, checkpoint: Path, persona: str,
    client: Any, grader: Callable[..., dict[str, Any]], certifier: Callable[..., dict[str, Any]],
    execution_identity: dict[str, Any], confirm_paid_calls: bool = False,
    review_system: str | None = None,
    inherited_preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one bound correction; callers own input authentication and paid budgets."""
    if not confirm_paid_calls:
        raise ValueError("revision execution requires explicit paid-call approval")
    raw = candidate_path.read_bytes()
    candidate_hash = hashlib.sha256(raw).hexdigest()
    candidate = yaml.safe_load(raw)
    if (candidate["test"] != request["user_request"]
            or candidate["mock_state"] != request["starting_app_data"]
            or candidate["grade"]["config"] != request["exact_grading_config"]
            or int(candidate["id"]) != request["test_id"]
            or request["phase"] != "before_oracle"):
        raise ValueError("revision review inputs differ from the candidate")
    if (saved_gate is None) != (request.get("legacy_execution_note") is None):
        raise ValueError("saved execution reuse must have the authenticated legacy approval")
    if saved_gate is None and (oracle_input is None or oracle_input.get("version") != 4):
        raise ValueError("new revision attempts require complete version-4 certification inputs")
    if saved_gate is not None and oracle_input is not None:
        raise ValueError("a grading-only revision cannot request new assistant inputs")
    identity = {
        "candidate_sha256": candidate_hash, "review_request": request,
        "saved_gate": saved_gate, "oracle_input": oracle_input,
        "checkpoint": str(checkpoint.resolve()), "persona": persona,
        "grading": grading_identity(), "execution": execution_identity,
        "review_system": review_system,
        **({"inherited_preflight": inherited_preflight} if inherited_preflight else {}),
        "reviewer": {"model": client.model, "reasoning_effort": client.reasoning_effort,
                     "max_output_tokens": getattr(client, "max_output_tokens", None)},
    }
    # Bind the whole workflow before consulting any independently cached stage.
    run_once(directory=out / "inputs", identity=identity, action=lambda: {"bound": True})
    outcome: dict[str, Any] = {
        "test_id": request["test_id"], "candidate_sha256": candidate_hash,
        "status": "pending", "stage": "preflight", "accepted": False,
        "additional_corrections": 0, "writer_calls": 0,
    }
    try:
        preflight = (RevisionReviewResponse.model_validate(inherited_preflight["preflight"])
                     if inherited_preflight else execute_revision_review(
                         request=request, out=out / "preflight", client=client, confirm_paid_calls=True,
                         system_override=review_system))
        outcome["preflight"] = preflight.model_dump(mode="json")
        if preflight.decision != "approve":
            return _save_outcome(out, outcome)
        outcome["stage"] = "grading_examples"
        examples = (inherited_preflight["grading_examples"] if inherited_preflight else
                    check_revision_examples(
                        response=preflight, request=request, out=out / "examples", grader=grader))
        outcome["grading_examples"] = examples
        if examples["passed"] is not True:
            return _save_outcome(out, outcome)
        outcome["stage"] = "certification"
        if saved_gate is not None:
            gate = grade_saved_attempts(
                candidate=candidate, gate=saved_gate, out=out / "saved_answer_grades",
                grading_identity=identity["grading"], grader=grader)
        else:
            def certify() -> dict[str, Any]:
                read_bound(candidate_path, candidate_hash)
                return certifier(
                    persona, candidate_path, checkpoint=checkpoint,
                    test_id=f"{request['test_id']:03d}", confirm_paid_calls=True,
                    oracle_input=oracle_input, max_technical_retries=0,
                )

            result = run_once(directory=out / "new_certification", identity=identity, action=certify)
            gate = {"result": result, "reused_executions": False, "new_agent_executions": 4}
        gate["candidate_sha256"] = candidate_hash
        _atomic_json(out / "gate.json", gate)
        result = gate["result"]
        outcome["reused_executions"] = gate["reused_executions"]
        outcome["new_agent_executions"] = gate["new_agent_executions"]
        outcome["stage"] = "final_review"
        final_request = copy.deepcopy(request)
        final_request.update(
            phase="after_oracle", executions=result.get("shots", []),
            agent_inputs=result.get("agent_inputs", {}),
            certification_result={
                "valid": result.get("valid"),
                "with_history_pass_count": result.get("g1_pass_count"),
                "without_history_pass_count": result.get("g2_pass_count"),
            },
        )
        _atomic_json(out / "final_review_request.json", final_request)
        final: RevisionReviewResponse = execute_revision_review(
            request=final_request, out=out / "final_review", client=client, confirm_paid_calls=True,
            system_override=review_system)
        outcome["final_review"] = final.model_dump(mode="json")
        if final.decision == "approve":
            validate_revision_certification(final_request)
            read_bound(candidate_path, candidate_hash)
            outcome.update(status="accepted_initial_final_review", stage="complete", accepted=True)
    except Exception as exc:
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    return _save_outcome(out, outcome)


def _save_outcome(out: Path, outcome: dict[str, Any]) -> dict[str, Any]:
    _atomic_json(out / "outcome.json", outcome)
    return outcome
