"""Offline tests for exact saved-work repairs and immutable source runs."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from authoring.complete_test import parse_writer_response, source_records
from authoring.context import dump_json
from authoring.exact_repairs import object_sha256
from authoring.progress import load_progress
from authoring import test_four_stage_authoring as fixture
from authoring.test_four_stage_authoring import ScriptedClient, approving_review, writer


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def execution_issue() -> dict:
    return {"decision": "reject", "examples": None, "issues": [{
        "part": "execution", "location": "/executions/0/tool_calls/0/args/body",
        "problem": "The assistant omitted a required result in the completed action.",
        "required_change": "Retry the unchanged candidate and include the required result.",
        "evidence": [{
            "location": "/executions/0/tool_calls/0/args/body", "quote": fixture.GOOD,
        }],
        "counterexample": None, "missing_check": None,
    }]}


class ExactRepairTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.FourStageAuthoringTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def permission(self, edits, *, source="create", additions=None, accepted=False):
        f = self.f
        progress, _ = load_progress(
            source_run=f.root / source, test_id=1, config_path=f.config_path,
            plan_path=f.plan, config=f.config, context=f.context, tasks=[f.idea],
        )
        row = {
            "kind": "exact_test_repair", "test_id": 1,
            "source_response_sha256": sha(Path(progress["authoring_response"])),
            "source_candidate_sha256": sha(Path(progress["candidate"])) if progress.get("candidate") else None,
            "source_gate_sha256": sha(Path(progress["gate"])) if progress.get("gate") else None,
            "specific_problem": "Apply only the approved exact repair.", "edits": edits,
            "fact_source_additions": additions or [],
        }
        if accepted:
            batch = f.root / source / "accepted_batch"
            row["replaces_accepted_batch"] = {
                "directory": str(batch), "planning_batch_sha256": sha(batch / "planning_batch.json"),
                "provenance_sha256": sha(batch / "provenance.json"),
            }
        path = f.root / "exact.json"
        dump_json(path, {"version": 4, "corrections": [row]})
        return path

    def accepted_with_redundant_check(self):
        raw = writer()
        raw["test"]["checks"].append({
            "assertion": {"check_id": "sent", "type": "tool_called", "tool": "send_email", "action_id": "email"},
            "evidence_ids": [], "why_required": "Proves sending, already proven by content.",
        })
        raw["quality_audit"]["final_actions"][0]["check_ids"].append("sent")
        raw["quality_audit"]["checks"].append({
            "check_id": "sent", "roles": ["final_action"],
            "why_necessary": "It claims to prove the send action.",
            "why_not_duplicate": "The reviewer must verify this claim against the other checks.",
        })
        self.f.create_batch(ScriptedClient(raw, approving_review(before=True), approving_review(before=False)))
        checks = parse_writer_response(raw).model_dump(mode="json")["test"]["checks"]
        return [{"field": "checks", "previous": checks, "replacement": checks[:-1]}]

    def test_accepted_grading_revision_reuses_agents_and_archives_batch_reference(self):
        edits = self.accepted_with_redundant_check()
        permission = self.permission(edits, accepted=True)
        source = self.f.root / "create"
        before = {str(p): p.read_bytes() for p in source.rglob("*") if p.is_file()}
        certifier = Mock(side_effect=AssertionError("must reuse saved executions"))
        client = ScriptedClient(approving_review(before=True), approving_review(before=False))
        result = self.f.resume_batch("create", client, corrections=permission, certifier=certifier)
        self.assertEqual(result["final_review_accepted"], 1)
        self.assertEqual(result["final_review_results"][0]["model_corrections_used"], 0)
        certifier.assert_not_called()
        self.assertEqual(len(client.calls), 2)
        state = json.loads((self.f.root / "persona_state.json").read_text())
        self.assertEqual(len(state["batches"]), 1)
        self.assertEqual(state["batches"][0]["directory"], result["accepted_batch"])
        self.assertEqual(state["superseded_batches"][0]["batch"]["directory"], str(source / "accepted_batch"))
        self.assertEqual(before, {str(p): p.read_bytes() for p in source.rglob("*") if p.is_file()})

    def test_accepted_repair_requires_exact_batch_binding_before_calls(self):
        permission = self.permission(self.accepted_with_redundant_check())
        client = ScriptedClient()
        with self.assertRaisesRegex(ValueError, "accepted test requires"):
            self.f.resume_batch("create", client, corrections=permission)
        self.assertEqual(client.calls, [])

    def test_failed_accepted_revision_keeps_old_batch_and_does_not_rewrite(self):
        permission = self.permission(self.accepted_with_redundant_check(), accepted=True)
        state_path = self.f.root / "persona_state.json"
        before = state_path.read_bytes()
        client = ScriptedClient(approving_review(before=True), self.f.check_issue())
        result = self.f.resume_batch("create", client, corrections=permission)
        self.assertEqual(result["final_review_accepted"], 0)
        self.assertEqual(result["final_review_results"][0]["status"], "repair_pending")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(state_path.read_bytes(), before)

    def test_interrupted_accepted_revision_retains_its_replacement_authorization(self):
        permission = self.permission(self.accepted_with_redundant_check(), accepted=True)
        first = self.f.resume_batch("create", ScriptedClient(approving_review(before=True)), corrections=permission)
        self.assertEqual(first["final_review_accepted"], 0)
        certifier = Mock(side_effect=AssertionError("must not rerun agents"))
        second = self.f.resume_batch("resume", ScriptedClient(approving_review(before=False)),
                                     name="second", certifier=certifier)
        self.assertEqual(second["final_review_accepted"], 1)
        state = json.loads((self.f.root / "persona_state.json").read_text())
        self.assertEqual(len(state["batches"]), 1)
        self.assertEqual(state["batches"][0]["directory"], second["accepted_batch"])

    def test_changed_before_value_fails_before_model_calls(self):
        edits = self.accepted_with_redundant_check()
        edits[0]["previous"][0]["why_required"] = "not the original value"
        permission = self.permission(edits, accepted=True)
        client = ScriptedClient()
        with self.assertRaisesRegex(ValueError, "does not match previous"):
            self.f.resume_batch("create", client, corrections=permission)
        self.assertFalse((self.f.root / "resume").exists())
        self.assertEqual(client.calls, [])

    def test_exact_request_repair_runs_new_agents_without_writer_call(self):
        raw = writer()
        raw["test"]["request"] = "Send an investor update with our funding context."
        raw["test"]["checks"][1]["assertion"]["path"] = "body"
        self.f.create_batch(ScriptedClient(raw))
        old = parse_writer_response(raw).model_dump(mode="json")["test"]
        new = parse_writer_response(writer()).model_dump(mode="json")["test"]
        edits = [{"field": field, "previous": old[field], "replacement": new[field]}
                 for field in ("request", "checks")]
        permission = self.permission(edits)
        certifier = Mock(wraps=self.f.certify)
        client = ScriptedClient(approving_review(before=True), approving_review(before=False))
        result = self.f.resume_batch("create", client, corrections=permission, certifier=certifier)
        self.assertEqual(result["final_review_accepted"], 1)
        self.assertEqual(result["final_review_results"][0]["model_corrections_used"], 0)
        certifier.assert_called_once()
        self.assertEqual(len(client.calls), 2)

    def test_exact_repair_routes_execution_only_failure_to_one_unchanged_retry(self):
        f = self.f
        raw = writer()
        raw["test"]["request"] = "Send an investor update with our funding context."
        raw["test"]["checks"][1]["assertion"]["path"] = "body"
        f.create_batch(ScriptedClient(raw))
        old = parse_writer_response(raw).model_dump(mode="json")["test"]
        new = parse_writer_response(writer()).model_dump(mode="json")["test"]
        permission = self.permission([
            {"field": field, "previous": old[field], "replacement": new[field]}
            for field in ("request", "checks")
        ])
        certifier = Mock(wraps=f.certify)
        client = ScriptedClient(
            approving_review(before=True), execution_issue(), approving_review(before=False),
        )

        result = f.resume_batch(
            "create", client, corrections=permission, certifier=certifier,
        )

        self.assertEqual(result["final_review_accepted"], 1, result)
        final = result["final_review_results"][0]
        self.assertEqual(final["status"], "accepted_after_final_review_correction")
        self.assertEqual(final["execution_reruns_used"], 1)
        certifier.assert_called()
        self.assertEqual(certifier.call_count, 2)
        self.assertEqual(len(client.calls), 3)

    def test_saved_exact_repair_execution_failure_resumes_into_retry(self):
        f = self.f
        raw = writer()
        raw["test"]["request"] = "Send an investor update with our funding context."
        raw["test"]["checks"][1]["assertion"]["path"] = "body"
        f.create_batch(ScriptedClient(raw))
        old = parse_writer_response(raw).model_dump(mode="json")["test"]
        new = parse_writer_response(writer()).model_dump(mode="json")["test"]
        permission = self.permission([
            {"field": field, "previous": old[field], "replacement": new[field]}
            for field in ("request", "checks")
        ])
        first_certifier = Mock(wraps=f.certify)
        with patch(
            "authoring.create_tests._unified_final_review_correction",
            return_value=(None, None, {"status": "repair_pending", "model_correction_started": False}),
        ):
            first = f.resume_batch(
                "create", ScriptedClient(approving_review(before=True), execution_issue()),
                corrections=permission, certifier=first_certifier,
            )
        self.assertEqual(first["final_review_results"][0]["status"], "repair_pending")
        self.assertEqual(first["final_review_results"][0]["execution_reruns_used"], 0)
        first_certifier.assert_called_once()

        def changed_retry(*args, **kwargs):
            result = f.certify(*args, **kwargs)
            result["shots"][0]["response_text"] = "Sent after the unchanged retry."
            return result

        retry_certifier = Mock(side_effect=changed_retry)
        second = f.resume_batch(
            "resume", ScriptedClient(approving_review(before=False)),
            name="second", certifier=retry_certifier,
        )

        self.assertEqual(second["final_review_accepted"], 1, second)
        self.assertEqual(second["final_review_results"][0]["execution_reruns_used"], 1, second)
        retry_certifier.assert_called_once()

    def test_grading_repair_before_first_certification_needs_no_writer(self):
        f = self.f
        revised = writer()
        revised["test"]["checks"][1]["assertion"]["criterion"] += " Equivalent wording is allowed."
        original_certifier = Mock(side_effect=AssertionError("preflight must stop before execution"))
        f.create_batch(ScriptedClient(writer(), f.check_issue(), revised, f.check_issue()),
                       certifier=original_certifier)
        checks = parse_writer_response(revised).model_dump(mode="json")["test"]["checks"]
        after = copy.deepcopy(checks)
        after[1]["assertion"]["criterion"] += " Preserve the funding meaning."
        permission = self.permission([{"field": "checks", "previous": checks, "replacement": after}])
        before = {str(p): p.read_bytes() for p in (f.root / "create").rglob("*") if p.is_file()}
        certifier = Mock(wraps=f.certify)
        client = ScriptedClient(approving_review(before=True), approving_review(before=False))
        result = f.resume_batch("create", client, corrections=permission, certifier=certifier)
        self.assertEqual(result["final_review_accepted"], 1, result)
        self.assertEqual(result["final_review_results"][0]["model_corrections_used"], 1)
        certifier.assert_called_once()
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(before, {str(p): p.read_bytes() for p in (f.root / "create").rglob("*") if p.is_file()})

    def test_missing_top_level_gate_does_not_authorize_repeating_saved_execution(self):
        f = self.f
        f.create_batch(
            ScriptedClient(writer(), approving_review(before=True)),
            force_final_review=True,
        )
        path = f.root / "create/final_trace_reviews/001/result.json"
        progress = json.loads(path.read_text())
        progress.pop("gate")
        dump_json(path, progress)
        checks = parse_writer_response(writer()).model_dump(mode="json")["test"]["checks"]
        after = copy.deepcopy(checks)
        after[1]["assertion"]["criterion"] += " Equivalent wording is allowed."
        permission = self.permission([{"field": "checks", "previous": checks, "replacement": after}])
        client = ScriptedClient()
        with self.assertRaisesRegex(ValueError, "cannot discard authenticated saved executions"):
            f.resume_batch("create", client, corrections=permission)
        self.assertEqual(client.calls, [])
        self.assertFalse((f.root / "resume").exists())

    def legacy_empty_scope_failure(self, *, completed_writer=False, nonempty_scope=False, reservation_count=1):
        """Reproduce the archived routing bug, only in the temporary fixture."""
        f = self.f
        result = f.create_batch(ScriptedClient(
            writer(), approving_review(before=True), f.mixed_check_execution_issue(),
            {"status": "cannot_write", "test": None, "problem": "No writable fields.",
             "quality_audit": None},
        ), force_final_review=True)
        proposal = f.root / "create/authoring/part_01/proposals/001"
        request_path = proposal / "final_review_correction_authoring_request.json"
        request = json.loads(request_path.read_text())
        request["allowed_correction_fields"] = ["test.checks"] if nonempty_scope else []
        dump_json(request_path, request)
        progress = result["final_review_results"][0]
        self.assertEqual(progress["status"], "validation_pending")
        progress["execution_reruns_used"] = reservation_count
        if reservation_count == 0:
            progress["attempt_name"] = "final_review_correction"
        progress["artifact_hashes"][str(request_path.resolve())] = sha(request_path)
        if completed_writer:
            failed_path = Path(progress["authoring_response"])
            written = writer()
            written["test"]["checks"][1]["assertion"]["criterion"] += " Equivalent wording is allowed."
            dump_json(failed_path, written)
            progress["artifact_hashes"][str(failed_path.resolve())] = sha(failed_path)
        progress_path = f.root / "create/final_trace_reviews/001/result.json"
        dump_json(progress_path, progress)
        checks = parse_writer_response(writer()).model_dump(mode="json")["test"]["checks"]
        after = copy.deepcopy(checks)
        after[1]["assertion"]["criterion"] += " Equivalent wording is allowed."
        permission = self.permission([{"field": "checks", "previous": checks, "replacement": after}])
        data = json.loads(permission.read_text())
        data["corrections"][0].update(
            source_response_sha256=sha(proposal / "initial_authoring_response.json"),
            failed_correction_recovery={
                "progress_sha256": sha(progress_path),
                "failed_response_sha256": sha(Path(progress["authoring_response"])),
                "failed_request_sha256": sha(request_path),
                "review_response_sha256": sha(progress_path.parent / "response.json"),
            },
        )
        dump_json(permission, data)
        return permission

    def test_failed_correction_recovery_completes_only_reserved_retry(self):
        permission = self.legacy_empty_scope_failure()
        f = self.f
        before = {str(p): p.read_bytes() for p in (f.root / "create").rglob("*") if p.is_file()}
        client = ScriptedClient(approving_review(before=True), approving_review(before=False))
        certifier = Mock(wraps=f.certify)
        result = f.resume_batch("create", client, corrections=permission, certifier=certifier)
        self.assertEqual(result["final_review_accepted"], 1, result)
        final = result["final_review_results"][0]
        self.assertEqual((final["model_corrections_used"], final["execution_reruns_used"]), (1, 1))
        certifier.assert_called_once()
        self.assertEqual(len(client.calls), 2)
        retry = f.root / "resume/authoring/part_01/proposals/001/reserved_execution_retry.json"
        self.assertEqual(json.loads(retry.read_text())["status"], "completed")
        self.assertEqual(before, {str(p): p.read_bytes() for p in (f.root / "create").rglob("*") if p.is_file()})

    def test_failed_correction_recovery_rejects_unbound_permissions_before_calls(self):
        permission = self.legacy_empty_scope_failure()
        initial = json.loads(permission.read_text())
        for key in ("progress_sha256", "failed_response_sha256", "failed_request_sha256", "review_response_sha256"):
            with self.subTest(key=key):
                value = copy.deepcopy(initial)
                value["corrections"][0]["failed_correction_recovery"][key] = "wrong"
                dump_json(permission, value)
                client = ScriptedClient()
                with self.assertRaisesRegex(ValueError, "unmatched artifact binding"):
                    self.f.resume_batch("create", client, corrections=permission)
                self.assertFalse((self.f.root / "resume").exists())
                self.assertEqual(client.calls, [])

    def test_failed_correction_recovery_requires_the_specific_recovery_permission(self):
        permission = self.legacy_empty_scope_failure()
        value = json.loads(permission.read_text())
        value["corrections"][0].pop("failed_correction_recovery")
        dump_json(permission, value)
        with self.assertRaisesRegex(ValueError, "does not match the saved response"):
            self.f.resume_batch("create", ScriptedClient(), corrections=permission)

    def test_recovery_cannot_roll_back_a_written_correction(self):
        permission = self.legacy_empty_scope_failure(completed_writer=True)
        with self.assertRaisesRegex(ValueError, "cannot roll back a written correction"):
            self.f.resume_batch("create", ScriptedClient(), corrections=permission)

    def test_recovery_does_not_bypass_a_legitimate_failed_writer_correction(self):
        permission = self.legacy_empty_scope_failure(nonempty_scope=True)
        with self.assertRaisesRegex(ValueError, "empty-scope correction"):
            self.f.resume_batch("create", ScriptedClient(), corrections=permission)

    def test_recovery_cannot_create_a_new_execution_reservation(self):
        permission = self.legacy_empty_scope_failure(reservation_count=0)
        with self.assertRaisesRegex(ValueError, "unfinished reserved retry"):
            self.f.resume_batch("create", ScriptedClient(), corrections=permission)

    def test_recovery_rejects_evidence_of_an_already_advanced_retry(self):
        permission = self.legacy_empty_scope_failure()
        path = self.f.root / "create/authoring/part_01/proposals/001/final_review_correction_gate.json"
        dump_json(path, {"result": {"valid": False}})
        with self.assertRaisesRegex(ValueError, "already advanced"):
            self.f.resume_batch("create", ScriptedClient(), corrections=permission)

    def test_recovered_preflight_interruption_keeps_one_reserved_retry(self):
        permission = self.legacy_empty_scope_failure()
        f = self.f
        first = f.resume_batch("create", ScriptedClient(), corrections=permission,
                               certifier=Mock(side_effect=AssertionError("preflight is pending")))
        self.assertEqual(first["final_review_accepted"], 0, first)
        progress, _ = load_progress(source_run=f.root / "resume", test_id=1, config_path=f.config_path,
                                    plan_path=f.plan, config=f.config, context=f.context, tasks=[f.idea])
        self.assertEqual(progress["execution_reruns_used"], 1)
        certifier = Mock(wraps=f.certify)
        second = f.resume_batch("resume", ScriptedClient(
            approving_review(before=True), approving_review(before=False),
        ), name="second", certifier=certifier)
        self.assertEqual(second["final_review_accepted"], 1, second)
        certifier.assert_called_once()
        self.assertEqual(second["final_review_results"][0]["execution_reruns_used"], 1)

    def test_recovered_final_review_interruption_does_not_repeat_execution(self):
        permission = self.legacy_empty_scope_failure()
        f = self.f
        first = f.resume_batch("create", ScriptedClient(approving_review(before=True)), corrections=permission)
        self.assertEqual(first["final_review_accepted"], 0, first)
        certifier = Mock(side_effect=AssertionError("the reserved retry is complete"))
        second = f.resume_batch("resume", ScriptedClient(approving_review(before=False)),
                                name="second", certifier=certifier)
        self.assertEqual(second["final_review_accepted"], 1, second)
        certifier.assert_not_called()
        self.assertEqual(second["final_review_results"][0]["execution_reruns_used"], 1)

    def test_started_reserved_retry_cannot_be_repeated(self):
        from authoring.exact_repairs import certify_reserved_retry

        f = self.f
        path = f.root / "retry.json"
        candidate = f.root / "candidate.yaml"
        candidate.write_text(yaml.safe_dump(f.candidate()))
        dump_json(path, {"status": "prepared"})
        certifier = Mock(side_effect=RuntimeError("connection interrupted"))
        for error in (RuntimeError, ValueError):
            with self.assertRaises(error):
                certify_reserved_retry(path=path, certifier=certifier, persona="morgan",
                                       candidate_path=candidate, checkpoint=f.context.checkpoint_path,
                                       confirm_paid_calls=True)
        certifier.assert_called_once()
        self.assertEqual(json.loads(path.read_text())["status"], "started")

    def test_completed_reserved_retry_is_cached_only_for_matching_inputs(self):
        from authoring.exact_repairs import certify_reserved_retry

        f = self.f
        path = f.root / "retry.json"
        candidate = f.root / "candidate.yaml"
        candidate.write_text(yaml.safe_dump(f.candidate()))
        dump_json(path, {"status": "prepared"})
        certifier = Mock(return_value={"valid": False, "shots": []})
        for _ in range(2):
            self.assertEqual(certify_reserved_retry(
                path=path, certifier=certifier, persona="morgan", candidate_path=candidate,
                checkpoint=f.context.checkpoint_path, confirm_paid_calls=True,
            ), {"valid": False, "shots": []})
        certifier.assert_called_once()
        with self.assertRaisesRegex(ValueError, "already started"):
            certify_reserved_retry(path=path, certifier=certifier, persona="different",
                                   candidate_path=candidate, checkpoint=f.context.checkpoint_path,
                                   confirm_paid_calls=True)

    def test_exact_repair_does_not_reset_an_exhausted_writer_allowance(self):
        self.f.exact_criterion_correction()
        source = self.f.root / "resume/authoring/part_01/proposals/001/initial_authoring_response.json"
        old = parse_writer_response(json.loads(source.read_text())).model_dump(mode="json")["test"]
        new_request = old["request"] + " Send it today."
        permission = self.permission([{"field": "request", "previous": old["request"],
                                       "replacement": new_request}], source="resume")
        client = ScriptedClient(approving_review(before=True), approving_review(before=False))
        result = self.f.resume_batch("resume", client, corrections=permission, name="exact")
        self.assertEqual(result["final_review_accepted"], 1)
        self.assertEqual(result["final_review_results"][0]["model_corrections_used"], 1)
        self.assertEqual(len(client.calls), 2)

    def test_accepted_revision_cannot_change_the_request(self):
        self.accepted_with_redundant_check()
        old = writer()["test"]["request"]
        permission = self.permission([{"field": "request", "previous": old,
                                       "replacement": old + " Send it today."}], accepted=True)
        with self.assertRaisesRegex(ValueError, "only its grading checks"):
            self.f.resume_batch("create", ScriptedClient(), corrections=permission)

    def test_budget_override_cannot_replay_completed_work(self):
        self.accepted_with_redundant_check()
        path = self.f.root / "budget.json"
        dump_json(path, {"version": 4, "corrections": [{
            "kind": "writer_input_budget", "test_id": 1, "source_budget_sha256": "irrelevant",
            "writer_max_input_tokens": 200000, "specific_problem": "Not an input-pending draft.",
        }]})
        with self.assertRaisesRegex(ValueError, "accepted test requires"):
            self.f.resume_batch("create", ScriptedClient(), corrections=path)

    def test_budget_override_preserves_config_and_complete_payload(self):
        self.f.config = self.f.config.model_copy(update={"writer_max_input_tokens": 1})
        self.f.config_path.write_text(yaml.safe_dump(self.f.config.model_dump(mode="json")))
        self.f.create_batch(ScriptedClient())
        budget = self.f.root / "create/authoring/part_01/proposals/001/initial_input_budget.json"
        path = self.f.root / "budget.json"
        dump_json(path, {"version": 4, "corrections": [{
            "kind": "writer_input_budget", "test_id": 1, "source_budget_sha256": sha(budget),
            "writer_max_input_tokens": 100000, "specific_problem": "The complete input exceeded the old limit.",
        }]})
        before = self.f.config_path.read_bytes()
        client = ScriptedClient(writer(), approving_review(before=True), approving_review(before=False))
        result = self.f.resume_batch("create", client, corrections=path)
        self.assertEqual(result["final_review_accepted"], 1)
        self.assertEqual(self.f.config_path.read_bytes(), before)
        payload = json.loads((self.f.root / "resume/authoring/part_01/proposals/001/initial_authoring_request.json").read_text())
        self.assertEqual(payload, self.f.payload)

    def test_source_overlay_is_used_by_certification_and_survives_review_resume(self):
        f = self.f
        session = {**f.context.history[0], "id": "s2"}
        f.context.history.append(session)
        f.context.sessions_by_id["s2"] = session
        f.create_batch(
            ScriptedClient(writer(), approving_review(before=True)),
            force_final_review=True,
        )
        old = parse_writer_response(writer()).model_dump(mode="json")["test"]["evidence"]
        new = copy.deepcopy(old)
        new[0].update(source_session_id="s2", message_id="s2:message:0")
        altered = copy.deepcopy(f.context)
        altered.facts_by_id[1]["source_session_ids"].append("s2")
        additions = [{
            "fact_id": 1, "previous_fact_sha256": object_sha256(f.context.facts_by_id[1]),
            "added_source_session_ids": ["s2"],
            "added_messages_sha256": object_sha256([r for r in source_records(altered, {1}) if r["source_session_id"] == "s2"]),
        }]
        permission = self.permission([{"field": "evidence", "previous": old, "replacement": new}], additions=additions)
        certifier = Mock(wraps=f.certify)
        result = f.resume_batch("create", ScriptedClient(approving_review(before=True)),
                                corrections=permission, certifier=certifier)
        self.assertEqual(result["final_review_accepted"], 0)
        self.assertEqual(f.context.facts_by_id[1]["source_session_ids"], ["s1"])
        self.assertEqual([r["source_session_id"] for r in certifier.call_args.kwargs["oracle_input"]["source_messages"]], ["s1", "s2"])
        certifier.reset_mock()
        final = f.resume_batch("resume", ScriptedClient(approving_review(before=False)), name="second", certifier=certifier)
        self.assertEqual(final["final_review_accepted"], 1)
        certifier.assert_not_called()

    def test_source_overlay_rejects_unknown_sessions_and_changed_messages(self):
        from authoring.exact_repairs import FactSourceAddition, add_fact_sources
        context = self.f.context
        row = {"fact_id": 1, "previous_fact_sha256": object_sha256(context.facts_by_id[1]),
               "added_source_session_ids": ["missing"], "added_messages_sha256": "wrong"}
        with self.assertRaisesRegex(ValueError, "existing checkpoint sessions"):
            add_fact_sources(context, self.f.idea, [FactSourceAddition.model_validate(row)])
        session = {**context.history[0], "id": "s2"}
        context.history.append(session)
        context.sessions_by_id["s2"] = session
        row["added_source_session_ids"] = ["s2"]
        with self.assertRaisesRegex(ValueError, "full original messages"):
            add_fact_sources(context, self.f.idea, [FactSourceAddition.model_validate(row)])
        self.assertEqual(context.facts_by_id[1]["source_session_ids"], ["s1"])


if __name__ == "__main__":
    unittest.main()
