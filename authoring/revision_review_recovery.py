"""Bind a narrow review repair without changing a completed run's inputs."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from authoring.revision_execution import _atomic_json, read_bound, run_once


REVIEW_SCOPE_INSTRUCTIONS = """
Scope of this review-only recovery: validate the exact approved delta against
the original authenticated test. This is not a new version-4 test or a migration.
The original published request and unchanged checks keep their recorded meaning
and independent-call behavior. Do not demand new target checks or shared action
groups merely because an unchanged legacy test lacks them. Where the approved
edits explicitly change target binding or action grouping, validate those edits
fully. Preserve all unchanged requirements when constructing grading examples.

Validate every changed requirement against the full dated original sources,
including review_context.original_messages when present. Human approval and a
correction explanation are not source evidence. Supplemental review messages
are not added to the historical assistant inputs. Report a missing prerequisite
in those assistant inputs, an unsupported changed requirement, answer leakage,
or a conflict with newer instructions; do not use scope to excuse these defects.
Required remembered content is valid even in the same tool call. Standalone
recall is not realistic work. A task-writing defect is not a global fact defect.

After certification, inspect the complete saved calls and grading results against
the unchanged requirements and the approved changed checks. A real assistant
mistake or a misapplied grade stays an issue; do not soften requirements, invent
evidence, or approve merely because this is a bounded revision. In particular,
an unapproved migration request is different from a concrete failure of a check
that is actually present. Before certification, an approval must include all
required complete executable examples. After certification, examples are null.
"""


FINAL_REVIEW_CONTEXT_INSTRUCTIONS = """
Evaluate the complete tool call, not a quoted fragment in isolation. Equivalent
meaning can be established by surrounding sentences or arguments; do not require
the same noun or adjective to be repeated in every sentence. Conversely, context
does not excuse missing meaning or an explicit contradictory permission.
Do not infer an unstated exclusive approval authority or mandatory named owner
from a source that specifies only an internal review route. Verify whether the
actual complete routing instruction satisfies the source-specified boundary.
Keep the necessary meaning intact, and report any remaining concrete defect.
"""


def phase_bound_preflight_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Keep the union nested: approval needs examples, other decisions forbid them."""
    approve = copy.deepcopy({key: value for key, value in schema.items() if key != "$defs"})
    decline = copy.deepcopy(approve)
    approve["properties"]["decision"] = {"type": "string", "enum": ["approve"]}
    approve["properties"]["examples"] = {"$ref": "#/$defs/RevisionExamples"}
    decline["properties"]["decision"] = {"type": "string", "enum": ["reject", "pending"]}
    decline["properties"]["examples"] = {"type": "null"}
    return {"type": "object", "properties": {"review": {"anyOf": [approve, decline]}},
            "required": ["review"], "additionalProperties": False, "$defs": copy.deepcopy(schema["$defs"])}


def recover_final_review(*, candidate_path: Path, previous_out: Path, out: Path,
                         review_context: dict[str, Any], client: Any,
                         system: str, confirm_paid_calls: bool = False) -> dict[str, Any]:
    """Revisit only a pending review; no grader or certifier is called here."""
    import yaml
    from authoring.exact_repairs import object_sha256
    from authoring.revision_review import execute_revision_review, grading_identity, validate_revision_certification

    if not confirm_paid_calls:
        raise ValueError("final review recovery requires explicit paid-call approval")
    previous = json.loads((previous_out / "outcome.json").read_text())
    if previous.get("accepted") or previous.get("stage") != "final_review":
        raise ValueError("final review recovery requires a pending final review")
    candidate = yaml.safe_load(read_bound(candidate_path, previous["candidate_sha256"]))
    request = json.loads((previous_out / "final_review_request.json").read_text())
    step = json.loads((previous_out / "final_review" / "model" / "step.json").read_text())
    response = json.loads(read_bound(previous_out / "final_review" / "model" / "result.json",
                                    step["result_sha256"]))
    if (step.get("status") != "completed" or step["inputs"]["request"] != request
            or step["input_sha256"] != object_sha256(step["inputs"])
            or response.get("decision") == "approve"
            or request["user_request"] != candidate["test"]
            or request["starting_app_data"] != candidate["mock_state"]
            or request["exact_grading_config"] != candidate["grade"]["config"]
            or request["test_id"] != int(candidate["id"])
            or previous.get("grading_examples", {}).get("passed") is not True
            or previous.get("preflight", {}).get("decision") != "approve"):
        raise ValueError("pending final review does not match authenticated candidate and evidence")
    validate_revision_certification(request)
    paths = ("outcome.json", "final_review_request.json", "final_review/model/step.json", "gate.json")
    origins = {name: hashlib.sha256((previous_out / name).read_bytes()).hexdigest() for name in paths}
    request["review_context"] = review_context
    identity = {
        "kind": "final_review_only_recovery", "previous_out": str(previous_out.resolve()),
        "source_sha256": origins, "candidate_sha256": previous["candidate_sha256"],
        "request": request, "system": system, "grading": grading_identity(),
        "reviewer": {"model": client.model, "reasoning_effort": client.reasoning_effort,
                     "max_output_tokens": getattr(client, "max_output_tokens", None)},
    }
    run_once(directory=out / "inputs", identity=identity, action=lambda: {"bound": True})
    _atomic_json(out / "final_review_request.json", request)
    outcome = {**previous, "review_only_recovery": identity,
               "new_recovery_agent_executions": 0, "new_recovery_grades": 0}
    outcome.pop("error", None)
    try:
        final = execute_revision_review(request=request, out=out / "final_review", client=client,
                                        confirm_paid_calls=True, system_override=system)
        outcome["final_review"] = final.model_dump(mode="json")
        if final.decision == "approve":
            validate_revision_certification(request)
            read_bound(candidate_path, previous["candidate_sha256"])
            outcome.update(accepted=True, status="accepted_initial_final_review", stage="complete")
    except Exception as exc:
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    _atomic_json(out / "outcome.json", outcome)
    return outcome


def bind_review_recovery(*, out: Path, identity: dict[str, Any],
                         context_path: Path) -> dict[str, Any]:
    step_path = out / "inputs" / "step.json"
    step = json.loads(step_path.read_text())
    if step.get("status") != "completed":
        raise ValueError("review recovery requires completed original input authentication")
    original = json.loads(read_bound(out / "inputs" / "result.json", step["result_sha256"]))
    if step.get("inputs") != original:
        raise ValueError("review recovery original inputs do not match their authenticated result")
    comparable = copy.deepcopy(identity)
    for section, name in (("execution", "authoring/revision_workflow.py"),
                          ("grading", "authoring/revision_review.py")):
        comparable[section]["code"][name] = original[section]["code"][name]
    if comparable != original:
        raise ValueError("review-only recovery cannot change candidates, execution, grading, or settings")
    binding = {
        "kind": "exact_revision_review_recovery",
        "original_step_sha256": hashlib.sha256(step_path.read_bytes()).hexdigest(),
        "review_context_sha256": hashlib.sha256(context_path.read_bytes()).hexdigest(),
        "current_inputs": identity,
    }
    digest = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    run_once(directory=out / "review_recovery_inputs" / digest, identity=binding,
             action=lambda: binding)
    return binding
