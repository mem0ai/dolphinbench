"""Offline regressions for the approved staged workflow and evidence closure."""

import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from authoring.context import CheckpointContext, dump_json
from authoring.evidence_packets import prepare_evidence
from authoring.complete_test import certification_input, writer_payload
from authoring.review import ReviewPendingError, ReviewResponse, validate_review
from authoring.runtime_policy import CreationPolicy
from tests.unit.authoring.creation_fixture import CreationFixture, ScriptedClient, approving_review, writer


from tests.unit.authoring import staged_fixture


class StagedCreationTests(staged_fixture.StagedFixture):
    def test_protocol_recovery_requires_four_empty_failed_attempts_and_identical_input(self):
        from authoring.staged_creation import _protocol_recovery
        old = {"source_messages": [{"text": "Original"}], "execution_settings": {
            "model": "gpt-5.6-sol", "reasoning_effort": "medium", "timeout": 120, "max_retries": 0}}
        expected = copy.deepcopy(old)
        expected["execution_settings"]["api"] = "responses"
        shot = {"tool_calls": [], "response_text": "", "usage": {"prompt_tokens": 0}, "oracle_errors": [
            "Function tools with reasoning_effort are not supported in /v1/chat/completions"]}
        gate = {"oracle_input": old, "result": {"shots": [copy.deepcopy(shot) for _ in range(4)]}}
        self.assertTrue(_protocol_recovery(gate, expected))
        for field, value in (("response_text", "Sent."), ("tool_calls", [{"tool": "send"}]),
                             ("usage", {"prompt_tokens": 1}), ("oracle_errors", ["429"])):
            changed = copy.deepcopy(gate)
            changed["result"]["shots"][0][field] = value
            self.assertFalse(_protocol_recovery(changed, expected))
        expected["source_messages"] = []
        self.assertFalse(_protocol_recovery(gate, expected))


    def test_dependency_selection_retains_replacements_and_related_original_messages(self):
        history = copy.deepcopy(self.context.history)
        facts = copy.deepcopy(self.context.facts)
        for i, statement in [(2, "Newer instruction: use the Series C instead."),
                             (3, "An unrelated team offsite."), (4, "The investor uses Pat as a short name.")]:
            history.append({"id": f"s{i}", "narrative_date": f"2025-06-0{i}T10:00:00Z", "message": statement})
            facts.append({"id": i, "statement": statement, "source_session_ids": [f"s{i}"],
                          "subjects": ["company:scaffold"], "supersedes": [1] if i == 2 else []})
        facts[0]["related_history_session_ids"] = ["s4"]
        context = replace(self.context, facts=facts, history=history,
                          facts_by_id={f["id"]: f for f in facts}, sessions_by_id={s["id"]: s for s in history})
        client = ScriptedClient({"additional_fact_ids": [], "rationale": "Mandatory replacement and identity links suffice."})
        prepared = prepare_evidence(context=context, idea=self.idea, evaluation_date=self.config.evaluation_date,
                                    out=self.root / "dependencies", client=client)
        payload = writer_payload(config=self.config, context=prepared, idea=self.idea)
        ids = {s["source_session_id"] for s in payload["sources"]}
        self.assertEqual(ids, {"s1", "s2", "s4"})
        self.assertNotIn("related_fact_catalog", payload)
        packet = prepared.evidence_packets[(1,)]
        self.assertEqual({f["id"] for f in packet["related_fact_catalog"]}, {1, 2, 3, 4})
        oracle = certification_input(prepared, self.idea, self.config.evaluation_date)
        self.assertEqual([s["text"] for s in oracle["source_messages"]], [s["text"] for s in payload["sources"]])
        self.assertTrue(all(set(s) == {"source_session_id", "message_id", "date", "text"} for s in oracle["source_messages"]))
        self.assertNotIn("related_history_session_ids", self.context.facts[0])

    def test_grader_application_error_cannot_trigger_an_execution_rerun(self):
        response = ReviewResponse.model_validate({"decision": "reject", "examples": None, "issues": [{
            "part": "execution", "location": "/executions/0/grade/details/0", "problem": "The judge misapplied the criterion.",
            "evidence": [], "required_change": "Regrade the existing call."}]})
        with self.assertRaisesRegex(ReviewPendingError, "grading-result location"):
            validate_review(response, {"phase": "after_oracle"})

    def test_audit_can_reference_a_declared_empty_read_but_not_an_invented_record(self):
        from authoring.complete_test import _validate_quality_audit, parse_writer_response
        raw = writer()
        raw["test"]["expected_tools"].append("list_calendar_events")
        raw["quality_audit"]["non_memory_inputs"].append({
            "input": "The visible calendar is empty.", "source_location": "/starting_app_data/calendar",
            "why_needed": "The requested conflict check can return an empty result."})
        payload = copy.deepcopy(self.payload)
        payload["app_record_shapes"] = {"calendar": "list"}
        payload["tools"]["list_calendar_events"] = {"state_effect": {
            "reads_state_keys": ["calendar"], "empty_result_is_valid": True}}
        def errors():
            return _validate_quality_audit(response=parse_writer_response(raw), idea=self.idea,
                                           context=self.context, payload=payload)
        self.assertEqual(errors(), [])
        for location in ("/starting_app_data/calendar/0", "/starting_app_data/missing"):
            raw["quality_audit"]["non_memory_inputs"][-1]["source_location"] = location
            self.assertTrue(any("unavailable state" in error for error in errors()))
        raw["quality_audit"]["non_memory_inputs"][-1]["source_location"] = "/starting_app_data/calendar"
        payload["tools"]["list_calendar_events"]["state_effect"]["empty_result_is_valid"] = False
        self.assertTrue(any("unavailable state" in error for error in errors()))

    def test_stages_keep_clean_acceptance_and_register_a_normal_batch(self):
        result, writer_client, review_client, registered = self._run_stages([writer()], [approving_review(before=True)])
        self.assertEqual(result["final_review_accepted"], 1, result["authoring_results"])
        self.assertEqual(len(writer_client.calls), 1)
        self.assertEqual(len(review_client.calls), 1)
        self.assertTrue(result["final_review_results"][0]["status"].startswith("accepted_"))
        registered.assert_called_once()
        events = json.loads((self.root / "staged" / "stage_events.json").read_text())
        self.assertEqual([e["stage"] for e in events], ["preparation", "writing", "preflight", "certification", "review"])

    def test_three_scoped_corrections_keep_the_checks_and_count(self):
        drafts = []
        for prefix in ("Send", "Please send", "Email", "Please email"):
            draft = writer()
            draft["test"]["request"] = prefix + " an investor update to pat@example.com with our current funding context."
            drafts.append(draft)
        rejection = {"decision": "reject", "examples": None, "issues": [{
            "part": "request", "location": "/user_request", "problem": "Use the supplied direct request wording.",
            "evidence": [{"location": "/user_request"}], "required_change": "Correct only the request wording.",
            "counterexample": None, "missing_check": None}]}
        result, authors, reviews, _ = self._run_stages(drafts, [rejection, rejection, rejection, approving_review(before=True)])
        self.assertEqual(result["final_review_accepted"], 1, result["authoring_results"])
        self.assertEqual(result["authoring_results"][0]["model_corrections_used"], 3)
        self.assertEqual(len(authors.calls), 4)
        self.assertEqual(len(reviews.calls), 4)

    def test_repeated_candidate_stops_without_more_review_or_execution(self):
        rejection = {"decision": "reject", "examples": None, "issues": [{
            "part": "request", "location": "/user_request", "problem": "The request needs a scoped correction.",
            "evidence": [{"location": "/user_request"}], "required_change": "Correct the request.",
            "counterexample": None, "missing_check": None}]}
        result, authors, reviews, _ = self._run_stages([writer(), writer()], [rejection])
        self.assertEqual(result["final_review_accepted"], 0)
        self.assertEqual(result["authoring_results"][0]["status"], "correction_pending")
        self.assertEqual(len(authors.calls), 2)
        self.assertEqual(len(reviews.calls), 1)

    def test_preflight_preserves_the_writer_cache_identity(self):
        from authoring.progress import cached_authoring_response
        self._run_stages([writer()], [approving_review(before=True)])
        progress = json.loads((self.root / "staged/progress/001.json").read_text())
        recovered = cached_authoring_response(progress["artifact_hashes"], "initial")
        self.assertEqual(recovered[0]["test"]["request"], writer()["test"]["request"])

    def test_resume_uses_completed_response_without_consulting_interruption_cache(self):
        from authoring.staged_creation import resume_staged
        self._run_stages([writer()], [approving_review(before=True)])
        source = self.root / "staged"
        (source / "manifest.json").unlink()
        path = source / "progress/001.json"
        progress = json.loads(path.read_text())
        progress["status"] = "certification_pending"
        progress["gate"] = None
        path.write_text(json.dumps(progress))
        no_calls = ScriptedClient()
        def certifier(*args, **kwargs):
            kwargs.pop("max_technical_retries", None)
            return self.fixture.certify(*args, **kwargs)
        with (patch("authoring.staged_creation.load_checkpoint_context", return_value=self.context),
              patch("authoring.propose.load_persona_state", return_value={"approved_plan_paths": [self.plan], "accepted_batch_dirs": []}),
              patch("authoring.staged_creation.cached_authoring_response", side_effect=AssertionError("completed response is authoritative")),
              patch.object(CreationPolicy, "client", return_value=no_calls),
              patch("authoring.staged_creation.certify_candidate", side_effect=certifier),
              patch("authoring.propose.record_accepted_batch")):
            result = resume_staged(config_path=self.config_path, plan_path=self.plan, source_run=source,
                                  out=self.root / "resumed", test_ids=[1], policy_path=self.root / "policy.json",
                                  confirm_paid_calls=True)
        self.assertEqual(result["final_review_accepted"], 1, result["authoring_results"])
        self.assertEqual(no_calls.calls, [])


class BatchedSemanticChecksTests(unittest.TestCase):
    def test_action_batch_preserves_distinct_checks_and_same_call_assignment(self):
        from graders.explicit import grade_tool_trace
        checks = [{"type": "field_llm_judge", "check_id": key, "action_id": "email", "tool": "send_email",
                   "path": "args." + key, "criterion": "Required " + key} for key in ("to", "body")]
        calls = [{"tool": "send_email", "args": {"to": "correct", "body": "wrong"}},
                 {"tool": "send_email", "args": {"to": "wrong", "body": "correct"}}]

        def batch(*, call, fields, **kwargs):
            return {f["check_id"]: {"ok": f["value"] == "correct", "reason": "fixture"} for f in fields}

        with patch("graders.llm_judge.judge_action_fields", side_effect=batch) as judge:
            result = grade_tool_trace(calls, {"check_version": 2, "semantic_judge_version": 2, "assertions": checks})
        self.assertFalse(result["passed"])
        self.assertEqual(judge.call_count, 2)
        self.assertTrue(result["details"][0]["call_evaluations"][0]["ok"])
        self.assertTrue(result["details"][1]["call_evaluations"][1]["ok"])

    def test_malformed_batch_is_pending_not_a_negative_grade(self):
        from graders.llm_judge import judge_action_fields
        with patch("graders.llm_judge._call_judge", return_value={"results": [{"check_id": "a", "passed": "false", "reason": "bad type"}]}):
            with self.assertRaisesRegex(ValueError, "malformed"):
                judge_action_fields(call={"tool": "send_email", "args": {}},
                                    fields=[{"check_id": "a", "value": "x", "criterion": "x"}], request="send", today="2028-01-01")
