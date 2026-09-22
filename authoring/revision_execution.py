"""Hash-bound saved-output reuse and at-most-once steps for exact revisions."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

import yaml

from authoring.exact_repairs import object_sha256
from harness.durable_json import atomic_json as _atomic_json
from harness.test_spec_schema import TestSpec


def read_bound(path: Path, expected_sha256: str) -> bytes:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError(f"revision source changed: {path}")
    return data


def agent_inputs(candidate: dict[str, Any]) -> dict[str, Any]:
    value = TestSpec.model_validate(candidate).model_dump(mode="json")
    value["id"] = int(value["id"])
    value.pop("grade")
    return value


def load_reusable_gate(
    *, original: dict[str, Any], candidate: dict[str, Any],
    source_candidate: Path, source_candidate_sha256: str,
    source_gate: Path, source_gate_sha256: str,
) -> dict[str, Any]:
    previous = yaml.safe_load(read_bound(source_candidate, source_candidate_sha256))
    if agent_inputs(previous) != agent_inputs(original) or agent_inputs(candidate) != agent_inputs(original):
        raise ValueError("saved certification cannot be reused after changing agent inputs")
    gate = json.loads(read_bound(source_gate, source_gate_sha256))
    if gate.get("candidate_sha256") != source_candidate_sha256:
        raise ValueError("saved certification gate belongs to a different candidate")
    result = gate.get("result") or {}
    shots = result.get("shots")
    if not isinstance(shots, list) or len(shots) != 4:
        raise ValueError("saved certification needs exactly four attempts")
    flags = [shot.get("with_memory") for shot in shots if isinstance(shot, dict)]
    if (sum(flag is True for flag in flags) != 2
            or sum(flag is False for flag in flags) != 2):
        raise ValueError("saved certification needs two attempts per memory condition")
    for shot in shots:
        if (shot.get("error") or shot.get("oracle_errors")
                or not isinstance(shot.get("tool_calls"), list)
                or not isinstance(shot.get("response_text"), str)):
            raise ValueError("saved certification contains incomplete or unsuccessful execution")
    return copy.deepcopy(gate)


def run_once(
    *, directory: Path, identity: dict[str, Any], action: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Reuse complete work; never replay a failed or uncertain paid operation."""
    directory.mkdir(parents=True, exist_ok=True)
    input_hash = object_sha256(identity)
    state_path, result_path = directory / "step.json", directory / "result.json"
    with (directory / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if state_path.is_file():
            state = json.loads(state_path.read_text())
            if state.get("input_sha256") != input_hash or state.get("inputs") != identity:
                raise ValueError("revision step inputs changed; refusing cached work or another call")
            if state.get("status") != "completed":
                raise ValueError("revision step previously started; inspect it before authorizing any repeat")
            return json.loads(read_bound(result_path, state["result_sha256"]))
        if result_path.exists():
            raise ValueError("revision step has an unbound result")
        state = {"input_sha256": input_hash, "inputs": identity, "status": "started"}
        _atomic_json(state_path, state)
        try:
            result = action()
            if not isinstance(result, dict):
                raise ValueError("revision operation did not return a result object")
            _atomic_json(result_path, result)
        except Exception as exc:
            _atomic_json(state_path, {**state, "status": "failed_or_uncertain",
                                      "error_type": type(exc).__name__})
            raise
        _atomic_json(state_path, {**state, "status": "completed",
                                  "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest()})
        return result


def grade_saved_attempts(
    *, candidate: dict[str, Any], gate: dict[str, Any], out: Path,
    grading_identity: dict[str, Any], grader: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Grade each original answer once, retaining its unmodified execution evidence."""
    result = copy.deepcopy(gate["result"])
    original_shots = copy.deepcopy(result["shots"])
    for index, shot in enumerate(result["shots"]):
        identity = {"kind": "exact_revision_certification_grade", "candidate": candidate,
                    "source_shot": original_shots[index], "grading": grading_identity}
        grade = run_once(
            directory=out / f"shot_{index + 1}", identity=identity,
            action=lambda shot=shot: grader(
                shot["tool_calls"], {**candidate["grade"]["config"], "raise_on_judge_error": True},
                test_message=candidate["test"],
            ),
        )
        if type(grade.get("passed")) is not bool or grade.get("diagnostic"):
            raise ValueError("revision grading returned an incomplete or diagnostic result")
        shot["grade"], shot["passed"] = grade, grade["passed"]
    result["g1_pass_count"] = sum(s["passed"] for s in result["shots"] if s["with_memory"])
    result["g2_pass_count"] = sum(s["passed"] for s in result["shots"] if not s["with_memory"])
    result["valid"] = result["g1_pass_count"] == 2 and result["g2_pass_count"] == 0
    result["verdict"] = "valid" if result["valid"] else "invalid"
    return {"result": result, "original_execution_evidence": original_shots,
            "reused_executions": True, "new_agent_executions": 0}
