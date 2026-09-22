"""Authenticate exact release revisions and explicitly approved legacy evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from harness.dataset import load_test

from authoring.final_trace_review import _fact_evidence
from authoring.release_revision import replay_edits
from authoring.revision_execution import load_reusable_gate, read_bound


def _record(root: Path, binding: dict[str, Any]) -> dict[str, Any]:
    return json.loads(read_bound(root / binding["path"], binding["sha256"]))


def _outputs(shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: shot.get(key) for key in (
        "with_memory", "tool_calls", "response_text", "error", "oracle_errors",
    )} for shot in shots]


def authenticate_legacy_execution(
    *, root: Path, original: dict[str, Any], candidate: dict[str, Any],
    evidence: dict[str, Any], context: Any,
) -> dict[str, Any]:
    """Verify retained task inputs, without claiming full-transcript recovery."""
    gate = load_reusable_gate(
        original=original, candidate=candidate,
        source_candidate=root / evidence["candidate"]["path"],
        source_candidate_sha256=evidence["candidate"]["sha256"],
        source_gate=root / evidence["gate"]["path"],
        source_gate_sha256=evidence["gate"]["sha256"],
    )
    request = _record(root, evidence["linked_review_request"])
    if (request.get("user_request") != original["test"]
            or request.get("starting_app_data") != original["mock_state"]
            or request.get("evaluation_date") != original["narrative_anchor_date"]
            or _outputs(request.get("executions", [])) != _outputs(gate["result"]["shots"])):
        raise ValueError("legacy review does not bind the original task and saved outputs")
    facts = request.get("source_evidence", [])
    if (len(facts) != len(original["load_bearing_facts"])
            or {f["fact_id"] for f in facts} != set(original["load_bearing_facts"])):
        raise ValueError("legacy review selected facts differ")
    for fact in facts:
        if fact.get("source_sessions") != _fact_evidence(context, fact["fact_id"])["source_sessions"]:
            raise ValueError("legacy execution source messages differ from the current complete sources")
    contracts = request.get("selected_tool_contracts", {})
    if set(contracts) != set(original["expected_tool_calls"]):
        raise ValueError("legacy execution tools differ")
    differences = []
    for tool, old in contracts.items():
        current = context.tools[tool]
        for key in ("arguments", "required_arguments"):
            if old.get(key) != current.get(key):
                raise ValueError("legacy execution tool arguments differ")
        for key in sorted(set(old) | set(current)):
            if old.get(key) != current.get(key):
                differences.append({"tool": tool, "field": key,
                                    "recorded": old.get(key), "current": current.get(key)})
    if differences != evidence["selected_tool_contract_differences_from_current"]:
        raise ValueError("legacy tool differences changed since the approved evidence record")

    authenticated_checkpoint = False
    for chain in evidence.get("checkpoint_correction_chains", []):
        binding = chain["input_record"]
        record = _record(root, binding)
        identity = record.get("checkpoint_identity")
        if not identity or identity != binding["checkpoint_identity"]:
            raise ValueError("legacy checkpoint input binding differs")
        seen = set()
        for correction_binding in chain["history_unchanged_correction_chain"]:
            correction = _record(root, correction_binding)
            if (identity in seen or correction.get("old_checkpoint_identity") != identity
                    or correction.get("history_changed") is not False
                    or set(correction.get("corrected_fact_ids", [])).intersection(original["load_bearing_facts"])):
                raise ValueError("legacy checkpoint correction changes the execution inputs")
            seen.add(identity)
            identity = correction["new_checkpoint_identity"]
        if identity != context.checkpoint_identity:
            raise ValueError("legacy checkpoint correction does not reach the current checkpoint")
        authenticated_checkpoint = True
    if "current_checkpoint_run_manifest" in evidence:
        manifest = _record(root, evidence["current_checkpoint_run_manifest"])
        results = [row for row in manifest["certification_results"]
                   if int(row["id"]) == int(original["id"])]
        if (manifest.get("checkpoint_identity") != context.checkpoint_identity
                or len(results) != 1 or results[0].get("certification") != gate["result"]):
            raise ValueError("legacy current-checkpoint run does not bind these attempts")
        authenticated_checkpoint = True
    if not authenticated_checkpoint:
        raise ValueError("legacy execution has no authenticated checkpoint record")
    return gate


def load_release_validation_inputs(
    *, root: Path, approval_path: Path, config_path: Path, context: Any,
) -> dict[str, Any]:
    approval = json.loads(approval_path.read_bytes())
    if (set(approval) != {"version", "kind", "revision", "saved_run_trace", "legacy_reuse_approval"}
            or approval["version"] != 2 or approval["kind"] != "exact_release_revision"):
        raise ValueError("expected a version-2 exact release revision approval")
    revision = _record(root, approval["revision"])
    stage = (root / approval["revision"]["path"]).resolve().parent
    if (revision.get("kind") != "prepared_exact_release_revision"
            or revision.get("checkpoint_identity") != context.checkpoint_identity
            or revision.get("persona") != context.persona):
        raise ValueError("prepared revision belongs to another checkpoint or persona")
    for name, digest in revision["source_bindings"].items():
        read_bound(Path(name), digest)
    config_bytes = config_path.read_bytes()
    if config_bytes != (stage / "source_config.yaml").read_bytes():
        raise ValueError("release validation config differs from the staged config")
    proposal = json.loads(read_bound(stage / "approved_proposal.json", revision["approved_proposal_sha256"]))
    originals, candidates = {}, {}
    expected = {f"{i:03d}" for i in range(1, 201)}
    if (set(revision["original_test_sha256"]) != expected
            or set(revision["candidate_test_sha256"]) != expected
            or proposal["published_tests_before_sha256"] != revision["original_test_sha256"]):
        raise ValueError("release validation requires the complete bound 200-test set")
    for folder in (stage / "source_tests", stage / "tests", root / "tests" / context.persona):
        if {p.name for p in folder.glob("*.yaml")} != {f"{i}.yaml" for i in expected}:
            raise ValueError("release validation test directory changed")
    for test_id in sorted(expected):
        digest = revision["original_test_sha256"][test_id]
        read_bound(stage / "source_tests" / f"{test_id}.yaml", digest)
        read_bound(root / "tests" / context.persona / f"{test_id}.yaml", digest)
        originals[test_id] = load_test(stage / "source_tests" / f"{test_id}.yaml")
        candidate_path = stage / "tests" / f"{test_id}.yaml"
        read_bound(candidate_path, revision["candidate_test_sha256"][test_id])
        candidates[test_id] = load_test(candidate_path)
    edits = {row["test_id"]: row for row in proposal["tests"]}
    if len(edits) != len(proposal["tests"]) or set(edits) != set(revision["changed_tests"]):
        raise ValueError("release validation changed-test list differs")
    for test_id in sorted(expected):
        wanted = replay_edits(originals[test_id], edits[test_id]) if test_id in edits else originals[test_id]
        if candidates[test_id] != wanted:
            raise ValueError("release validation candidate contains unapproved edits")
    trace = _record(root, approval["saved_run_trace"])
    history_hash = hashlib.sha256((context.checkpoint_path / "life_sim.yaml").read_bytes()).hexdigest()
    if (trace["current_history_sha256"] != history_hash
            or trace["approved_proposal"]["sha256"] != revision["approved_proposal_sha256"]):
        raise ValueError("saved-run trace belongs to different release inputs")
    grading_ids = sorted(i for i, row in edits.items() if row["owner"] == "grading")
    legacy = approval["legacy_reuse_approval"]
    if (set(legacy) != {"approved", "test_ids", "limitation"}
            or legacy["approved"] is not True or legacy["test_ids"] != grading_ids
            or legacy["limitation"] != trace["certification_note"]
            or set(trace["certification"]) != set(grading_ids)):
        raise ValueError("legacy reuse requires explicit approval of the exact IDs and recorded limitation")
    gates = {i: authenticate_legacy_execution(
        root=root, original=originals[i], candidate=candidates[i],
        evidence=trace["certification"][i], context=context,
    ) for i in grading_ids}
    return {"approval": approval, "revision": revision, "stage": stage,
            "originals": originals, "candidates": candidates, "edits": edits,
            "trace": trace, "gates": gates}


def load_review_context(*, path: Path | None, approval_path: Path, context: Any,
                        changed_ids: set[str]) -> dict[str, Any]:
    if path is None:
        return {}
    raw = json.loads(path.read_text())
    if (set(raw) != {"version", "approval_sha256", "tests"} or raw["version"] != 1
            or raw["approval_sha256"] != hashlib.sha256(approval_path.read_bytes()).hexdigest()
            or not isinstance(raw["tests"], dict) or not set(raw["tests"]).issubset(changed_ids)):
        raise ValueError("review context must bind the approved exact revisions and selected IDs")
    sessions = {str(row["id"]): row for row in context.history}
    result = {}
    for test_id, entry in raw["tests"].items():
        if (not isinstance(entry, dict) or "source_session_ids" not in entry
                or set(entry) - {"source_session_ids", "review_scope", "review_stage", "review_format"}
                or entry.get("review_scope") not in (None, "approved_delta_v1")
                or entry.get("review_stage") not in (None, "preflight", "final_review")
                or entry.get("review_format") not in (None, "phase_bound_v1")):
            raise ValueError("review context may supply only existing original source sessions")
        ids = entry["source_session_ids"]
        if (not isinstance(ids, list) or any(not isinstance(value, str) for value in ids)
                or (not ids and entry.get("review_scope") != "approved_delta_v1")
                or len(ids) != len(set(ids)) or not set(ids).issubset(sessions)):
            raise ValueError("review context refers to missing or repeated source sessions")
        messages = []
        for session_id in ids:
            session = sessions[session_id]
            original = ([session["message"]] if isinstance(session.get("message"), str) else [])
            original += [s for s in session.get("messages", []) if isinstance(s, str)]
            if not original:
                raise ValueError("review context session has no original user messages")
            messages.extend({"source_session_id": session_id, "message_id": f"{session_id}:message:{i}",
                             "date": session["narrative_date"], "text": text}
                            for i, text in enumerate(original))
        result[test_id] = {"purpose": "Review context only; not added to assistant certification inputs.",
                           "original_messages": messages}
        for key in ("review_scope", "review_stage", "review_format"):
            if entry.get(key):
                result[test_id][key] = entry[key]
    return result
