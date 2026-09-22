"""Exact human-approved edits and per-test overrides for authenticated resumes."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from authoring.complete_test import WriterResponse, parse_writer_response


class ExactFieldEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: Literal["request", "existing_records", "new_records", "evidence", "checks"]
    previous: Any
    replacement: Any


class FactSourceAddition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: StrictInt = Field(gt=0)
    previous_fact_sha256: StrictStr
    added_source_session_ids: list[StrictStr] = Field(min_length=1)
    added_messages_sha256: StrictStr


class AcceptedBatchBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    directory: StrictStr
    planning_batch_sha256: StrictStr
    provenance_sha256: StrictStr


class FailedCorrectionRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    progress_sha256: StrictStr
    failed_response_sha256: StrictStr
    failed_request_sha256: StrictStr
    review_response_sha256: StrictStr


class ApprovedExactRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["exact_test_repair"]
    test_id: StrictInt = Field(gt=0)
    source_response_sha256: StrictStr
    source_candidate_sha256: StrictStr | None
    source_gate_sha256: StrictStr | None
    specific_problem: StrictStr = Field(min_length=1)
    edits: list[ExactFieldEdit] = Field(min_length=1)
    fact_source_additions: list[FactSourceAddition] = Field(default_factory=list)
    replaces_accepted_batch: AcceptedBatchBinding | None = None
    failed_correction_recovery: FailedCorrectionRecovery | None = None


class ApprovedInputBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["writer_input_budget"]
    test_id: StrictInt = Field(gt=0)
    source_budget_sha256: StrictStr
    writer_max_input_tokens: StrictInt = Field(gt=0)
    specific_problem: StrictStr = Field(min_length=1)


def object_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def add_fact_sources(context: Any, task: Any, additions: list[FactSourceAddition]) -> Any:
    """Overlay only provenance; the authenticated checkpoint remains untouched."""
    if not additions:
        return context
    from authoring.complete_test import source_records

    revised = copy.deepcopy(context)
    seen: set[int] = set()
    for addition in additions:
        if addition.fact_id in seen or addition.fact_id not in task.fact_ids:
            raise ValueError("source additions must name distinct selected facts")
        seen.add(addition.fact_id)
        fact = revised.facts_by_id[addition.fact_id]
        if object_sha256(fact) != addition.previous_fact_sha256:
            raise ValueError("source addition does not match the original fact")
        ids = addition.added_source_session_ids
        if (len(set(ids)) != len(ids) or set(ids).intersection(fact.get("source_session_ids") or [])
                or set(ids) - set(revised.sessions_by_id)):
            raise ValueError("source additions must name new, existing checkpoint sessions")
        fact["source_session_ids"] = list(fact.get("source_session_ids") or []) + ids
        messages = [row for row in source_records(revised, {addition.fact_id})
                    if row["source_session_id"] in ids]
        if object_sha256(messages) != addition.added_messages_sha256:
            raise ValueError("source addition does not match the full original messages")
    return revised


def apply_exact_edits(raw: dict[str, Any], repair: ApprovedExactRepair) -> WriterResponse:
    previous = parse_writer_response(raw)
    if previous.test is None:
        raise ValueError("an exact repair requires a saved written draft")
    value = previous.model_dump(mode="json")
    fields = [edit.field for edit in repair.edits]
    if len(set(fields)) != len(fields):
        raise ValueError("an exact repair repeats a field")
    for edit in repair.edits:
        if value["test"][edit.field] != edit.previous:
            raise ValueError(f"exact repair does not match previous test.{edit.field}")
        if edit.previous == edit.replacement:
            raise ValueError("an exact repair must change each named field")
        value["test"][edit.field] = copy.deepcopy(edit.replacement)
    # An exact human edit has no writer call that can truthfully refresh this
    # internal reasoning. Review the repaired test directly instead.
    value["quality_audit"] = None
    revised = WriterResponse.model_validate(value)
    if repair.replaces_accepted_batch is not None and (
        set(fields) != {"checks"} or repair.fact_source_additions
    ):
        raise ValueError("an accepted-test repair may change only its grading checks")
    return revised


def grade_only(repair: ApprovedExactRepair) -> bool:
    return not repair.fact_source_additions and all(
        edit.field in {"checks", "evidence"} for edit in repair.edits
    )


def has_saved_executions(record: dict[str, Any]) -> bool:
    """A missing top-level gate does not erase an interrupted regrade's inputs."""
    for name in record["authenticated_files"]:
        path = Path(name)
        if path.suffix != ".json":
            continue
        value = json.loads(path.read_text())
        if (isinstance(value, dict) and "candidate_sha256" in value
                and isinstance(value.get("result"), dict) and value["result"].get("shots")):
            return True
    return False


def recover_failed_correction_source(repair: ApprovedExactRepair, record: dict[str, Any]) -> Path:
    """Recover only the written input to the recorded empty-scope correction."""
    from authoring.review import ReviewResponse, review_decision, validate_review

    recovery = repair.failed_correction_recovery
    if recovery is None:
        raise ValueError("failed-correction recovery needs explicit hash bindings")
    progress = record["progress"]
    files = record["authenticated_files"]

    def bound(digest: str, suffix: str) -> Path:
        paths = [Path(name) for name, expected in files.items()
                 if expected == digest and Path(name).name.endswith(suffix)]
        if len(paths) != 1 or hashlib.sha256(paths[0].read_bytes()).hexdigest() != digest:
            raise ValueError("failed-correction recovery has an unmatched artifact binding")
        return paths[0]

    saved_progress = json.loads(bound(recovery.progress_sha256, ".json").read_text())
    if saved_progress != {key: value for key, value in progress.items() if key != "plan_position"}:
        raise ValueError("failed-correction recovery does not bind the selected progress")
    if (record.get("was_accepted") or repair.replaces_accepted_batch is not None
            or progress.get("status") != "validation_pending"
            or record.get("model_corrections_used") != 1 or record.get("execution_reruns_used") != 1
            or not record.get("candidate") or not record.get("gate")
            or progress.get("corrected_final_review") or repair.fact_source_additions
            or any(edit.field != "checks" for edit in repair.edits)):
        raise ValueError("recovery requires an unfinished reserved retry and an exact grading repair")
    failed = bound(recovery.failed_response_sha256, "_authoring_response.json")
    if failed.resolve() != Path(progress.get("authoring_response") or "").resolve():
        raise ValueError("recovery must bind the latest failed writer response")
    if parse_writer_response(json.loads(failed.read_text())).status != "cannot_write":
        raise ValueError("recovery cannot roll back a written correction")
    request_path = bound(recovery.failed_request_sha256, "_authoring_request.json")
    if request_path != failed.with_name(failed.name.replace("_response.json", "_request.json")):
        raise ValueError("recovery binds a different writer request")
    request = json.loads(request_path.read_text())
    source = bound(repair.source_response_sha256, "_authoring_response.json")
    previous = parse_writer_response(json.loads(source.read_text()))
    if (request.get("allowed_correction_fields") != [] or previous.test is None
            or parse_writer_response(request.get("previous_authoring") or {}) != previous):
        raise ValueError("recovery requires the written input to an empty-scope correction")
    review_path = bound(recovery.review_response_sha256, "response.json")
    if not record.get("request") or review_path.parent != Path(record["request"]).parent:
        raise ValueError("recovery needs the candidate's authenticated final review")
    normalized_review_path = review_path.with_name("review.json")
    normalized_key = str(normalized_review_path.resolve())
    if (
        normalized_key not in files
        or not normalized_review_path.is_file()
        or hashlib.sha256(normalized_review_path.read_bytes()).hexdigest() != files[normalized_key]
    ):
        raise ValueError("recovery needs the authenticated normalized final review")
    review = ReviewResponse.model_validate(json.loads(normalized_review_path.read_text()))
    review_request = json.loads(Path(record["request"]).read_text())
    validate_review(review, review_request)
    if (review.decision != "reject" or {issue.part for issue in review.issues} != {"checks", "execution"}
            or request.get("failure_to_fix") != review.model_dump(mode="json")
            or review_decision(review, review_request).model_dump(mode="json") != progress.get("initial_final_review")):
        raise ValueError("recovery requires the recorded mixed grading and execution decision")
    attempt = (progress.get("correction") or {}).get("attempt") or {}
    if attempt.get("stage") != "validation_pending":
        raise ValueError("recovery cannot replay a correction that reached execution")
    if any(path.is_file() for pattern in (
        "final_review_*candidate.yaml", "final_review_*gate.json", "reserved_execution_retry.json",
    ) for path in failed.parent.glob(pattern)):
        raise ValueError("recovery found evidence that the reserved retry already advanced")
    return source


def certify_reserved_retry(
    *, path: Path, certifier: Any, persona: str, candidate_path: Path,
    checkpoint: Path, confirm_paid_calls: bool, oracle_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Complete one unused reservation; a saved completion is a cache, not a retry."""
    from authoring.context import dump_json

    if not confirm_paid_calls:
        raise ValueError("reserved certification requires explicit paid-call confirmation")
    saved = json.loads(path.read_text())
    identity = {
        "persona": persona, "candidate_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
        "checkpoint": str(checkpoint.resolve()), "oracle_input_sha256": object_sha256(oracle_input),
    }
    if saved.get("status") == "completed" and saved.get("identity") == identity:
        return saved["result"]
    if saved.get("status") != "prepared":
        raise ValueError("reserved retry already started; reuse its matching completion or diagnose the interruption")
    saved.update(status="started", identity=identity)
    dump_json(path, saved)
    result = certifier(
        persona, candidate_path, checkpoint=checkpoint, confirm_paid_calls=True,
        **({"oracle_input": oracle_input} if oracle_input is not None else {}),
    )
    saved.update(status="completed", result=result)
    dump_json(path, saved)
    return result


def bind_input_budget(row: ApprovedInputBudget, record: dict[str, Any]) -> None:
    progress = record["progress"]
    if (progress["status"] != "input_pending" or progress.get("authoring_response")
            or record.get("candidate") or record.get("gate")):
        raise ValueError("a budget override requires a draft stopped before its writer call")
    paths = [Path(path) for path, digest in record["authenticated_files"].items()
             if Path(path).name.endswith("_input_budget.json") and digest == row.source_budget_sha256]
    if len(paths) != 1:
        raise ValueError("budget override does not match the saved input budget")
    budget = json.loads(paths[0].read_text())
    if (row.writer_max_input_tokens <= budget["maximum"]
            or row.writer_max_input_tokens < budget["serialized_input_tokens"]):
        raise ValueError("budget override must cover the complete saved input")
    record.setdefault("test_overrides", {})["writer_max_input_tokens"] = row.writer_max_input_tokens


def effective_context(context: Any, task: Any, overrides: dict[str, Any]) -> Any:
    return add_fact_sources(context, task, [
        FactSourceAddition.model_validate(row) for row in overrides.get("fact_source_additions", [])
    ])
