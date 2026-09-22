"""Offline coverage for the planner, complete writer, and short review workflow."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml
from jsonschema import Draft202012Validator

from authoring.complete_test import (
    WriterResponse, assemble_written_test, authoring_evidence, certification_input,
    correction_scope, encode_writer_response, parse_writer_response, source_context,
    validate_writer_correction, writer_payload, writer_response_schema,
)
from authoring.context import dump_json, load_authoring_tasks
from authoring.models import ApprovedIdea, AuthoringConfig, CandidateTaskBatch
from authoring.pipeline import AuthoringValidationError
from authoring.review import (
    ReviewPendingError, ReviewResponse, check_review_examples, execute_review,
    review_decision, review_response_schema, validate_review,
)
from graders.explicit import grade_tool_trace
from harness.oracle import build_system_prompt


from tests.unit.authoring.creation_fixture import GOOD, BAD, ScriptedClient, idea, writer, calls, approving_review


from tests.unit.authoring import creation_fixture


class FourStageAuthoringTests(creation_fixture.CreationFixture):


    def test_transport_round_trip_and_schema(self):
        schema = writer_response_schema()
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(writer())
        self.assertEqual(encode_writer_response(parse_writer_response(writer())), writer())
        self.assertEqual(self.candidate()["grade"]["config"]["assertions"],
                         [row.assertion for row in parse_writer_response(writer()).test.checks])

    def test_current_writer_requires_audit_but_historical_response_remains_readable(self):
        raw = writer()
        del raw["quality_audit"]
        self.assertIsNone(parse_writer_response(raw).quality_audit)
        with self.assertRaisesRegex(ValueError, "current written response needs quality_audit"):
            parse_writer_response(raw, require_quality_audit=True)

    def test_quality_audit_is_internal_and_has_no_request_length_validator(self):
        raw = writer()
        raw["test"]["request"] = " ".join(["Please"] + ["include the necessary context"] * 30)
        response = parse_writer_response(raw, require_quality_audit=True)
        candidate = self.candidate(raw)
        self.assertNotIn("quality_audit", candidate)
        self.assertIn("quality_audit", authoring_evidence(response))

    def test_quality_audit_rejects_broken_structural_bindings(self):
        changes = (
            ("missing input source", lambda raw: raw["quality_audit"]["non_memory_inputs"][0].update(
                source_location="/starting_app_data/emails/0"
            )),
            ("unselected fact", lambda raw: raw["quality_audit"]["memory_results"][0].update(
                fact_ids=[2]
            )),
            ("unknown check", lambda raw: raw["quality_audit"]["checks"][0].update(
                check_id="missing"
            )),
            ("wrong action", lambda raw: raw["quality_audit"]["final_actions"][0].update(
                action_id="other"
            )),
        )
        for label, change in changes:
            raw = writer()
            change(raw)
            with self.subTest(label=label), self.assertRaises(AuthoringValidationError):
                self.candidate(raw)

    def test_quality_audit_leaves_extra_action_classification_to_reviewer(self):
        audit_idea = self.idea.model_copy(update={
            "expected_tools": ["send_email", "list_emails"],
        })
        payload = writer_payload(config=self.config, context=self.context, idea=audit_idea)
        raw = writer()
        raw["test"]["expected_tools"].append("list_emails")
        raw["test"]["checks"].append({
            "assertion": {
                "check_id": "lookup",
                "action_id": "lookup",
                "type": "tool_called",
                "tool": "list_emails",
            },
            "evidence_ids": [],
            "why_required": "The request requires a supporting inbox read.",
        })
        raw["quality_audit"]["final_actions"].append({
            "result": "Read the inbox before sending.",
            "tool": "list_emails",
            "action_id": "lookup",
            "check_ids": ["lookup"],
        })
        raw["quality_audit"]["checks"].append({
            "check_id": "lookup",
            "roles": ["final_action"],
            "why_necessary": "It claims the supporting read is a final action.",
            "why_not_duplicate": "No other check covers the read.",
        })

        response = parse_writer_response(raw)
        candidate = assemble_written_test(
            response=response,
            idea=audit_idea,
            context=self.context,
            evaluation_date=self.config.evaluation_date,
            test_id=1,
            payload=payload,
        )

        self.assertEqual(candidate["id"], 1)

    def test_flexible_values_preserve_nested_types(self):
        raw = writer()
        assertion = raw["test"]["checks"][0]["assertion"]
        assertion["value_json"] = '{"enabled":true,"amount":3,"options":[null,{"x":"y"}]}'
        parsed = parse_writer_response(raw)
        self.assertEqual(parsed.test.checks[0].assertion["value"],
                         {"enabled": True, "amount": 3, "options": [None, {"x": "y"}]})

    def test_bound_writer_schema_prevents_unavailable_tools_and_scoped_counts(self):
        schema = writer_response_schema(payload=self.payload)
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        validator.validate(writer())
        raw = writer()
        raw["test"]["checks"][0]["assertion"]["tool"] = "merge_pr"
        self.assertFalse(validator.is_valid(raw))
        raw = writer()
        raw["test"]["checks"][0]["assertion"] = {
            "check_id": "no_extra_call", "type": "tool_not_called", "tool": "send_email",
            "action_id": "email",
        }
        self.assertFalse(validator.is_valid(raw))
        raw["test"]["checks"][0]["assertion"]["action_id"] = ""
        self.assertTrue(validator.is_valid(raw))

    def test_bound_writer_schema_preserves_fact_message_relationships(self):
        payload = copy.deepcopy(self.payload)
        payload["sources"].append({
            "source_session_id": "related", "message_id": "related:message:0",
            "fact_ids": [532], "text": GOOD, "date": "2026-01-01",
        })
        original = copy.deepcopy(payload)
        validator = Draft202012Validator(writer_response_schema(payload=payload))
        validator.validate(writer())
        raw = writer()
        raw["test"]["evidence"][0].update(
            source_session_id="related", message_id="related:message:0",
        )
        self.assertFalse(validator.is_valid(raw))
        raw["test"]["evidence"][0]["fact_ids"] = [532]
        self.assertFalse(validator.is_valid(raw))
        self.assertEqual(payload, original)
        self.assertTrue(Draft202012Validator(writer_response_schema()).is_valid(raw))

    def test_malformed_transport_is_not_repaired(self):
        for encoded in ('{"x":1,"x":2}', '{"x":NaN}', "not JSON"):
            raw = writer()
            raw["test"]["checks"][0]["assertion"]["value_json"] = encoded
            with self.subTest(encoded=encoded), self.assertRaises(ValueError):
                parse_writer_response(raw)

    def test_write_only_collection_is_not_supplied_or_visible(self):
        self.assertEqual(self.payload["app_records"], {})
        self.assertEqual(self.payload["app_record_shapes"]["emails"], "list")
        self.assertEqual(self.candidate()["mock_state"], {})

    def test_read_collections_and_dependencies_are_complete(self):
        task = self.idea.model_copy(update={"expected_tools": ["get_document", "create_document"]})
        payload = writer_payload(config=self.config, context=self.context, idea=task)
        self.assertEqual(payload["app_records"]["documents"], self.context.app_state["documents"])
        self.assertEqual(payload["app_records"]["attachments"], self.context.app_state["attachments"])

    def test_get_pr_supplies_both_complete_read_collections(self):
        from construction.runtime_inputs import exact_persona_tools, readable_app_state

        self.context.tools = exact_persona_tools("alex")
        self.context.app_state = {
            "prs": [{"id": "service#1", "body": "First complete record"}],
            "open_prs": [{"id": "service#2", "diff": "Second complete record"}],
            "pr_reviews": [{"id": "old-review"}],
        }
        self.context.readable_state = readable_app_state(
            self.context.tools, self.context.app_state,
        )
        task = self.idea.model_copy(update={"expected_tools": ["get_pr", "review_pr"]})
        payload = writer_payload(config=self.config, context=self.context, idea=task)
        self.assertEqual(payload["tools"]["get_pr"]["state_effect"]["reads_state_keys"],
                         ["prs", "open_prs"])
        self.assertEqual(payload["app_records"], {
            key: self.context.app_state[key] for key in ("prs", "open_prs")
        })
        self.assertEqual(payload["app_record_shapes"], {
            "prs": "list", "open_prs": "list", "pr_reviews": "list",
        })

    def test_dictionary_and_list_record_transport(self):
        raw = writer()
        raw["test"]["new_records"] = [{"state_key": "emails", "record_key": "", "record_json": '{"id":"neutral","body":"Meeting invitation"}'}]
        self.assertEqual(self.candidate(raw)["mock_state"]["emails"][0]["id"], "neutral")
        raw["test"]["new_records"] = [{"state_key": "documents", "record_key": "new-doc", "record_json": '{"title":"Neutral brief"}'}]
        self.assertEqual(self.candidate(raw)["mock_state"]["documents"]["new-doc"]["title"], "Neutral brief")

    def test_unsupplied_record_selection_fails(self):
        raw = writer()
        raw["test"]["existing_records"] = [{"state_key": "emails", "record_key": "", "match_json": '{"id":"old"}'}]
        with self.assertRaisesRegex(AuthoringValidationError, "unsupplied"):
            self.candidate(raw)

    def test_sources_appear_once_with_full_original_dates(self):
        second = {**copy.deepcopy(self.context.facts[0]), "id": 2, "statement": "The same source establishes another fact."}
        self.context.facts.append(second)
        self.context.facts_by_id[2] = second
        rows = source_context(self.context, self.idea)["sources"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fact_ids"], [1, 2])
        self.assertEqual(rows[0]["date"], "2025-05-01T15:30:00-07:00")

    def test_multiple_passages_for_one_fact_are_allowed(self):
        raw = writer()
        raw["test"]["evidence"].append({**raw["test"]["evidence"][0], "evidence_id": "lead", "quote": "led by Northstar"})
        raw["test"]["checks"][1]["evidence_ids"].append("lead")
        self.candidate(raw)

    def test_bare_persona_is_not_a_topic_but_topics_and_updates_are_retained(self):
        self.context.facts[0]["subjects"] = ["morgan", "sphere"]
        for index, subjects, supersedes in (
            (2, ["morgan", "dinner"], []),
            (3, ["morgan", "sphere"], []),
            (4, ["morgan"], [1]),
        ):
            session = {"id": f"s{index}", "narrative_date": f"2026-01-0{index}T09:00:00-08:00",
                       "messages": [f"Complete original message {index}."]}
            fact = {**copy.deepcopy(self.context.facts[0]), "id": index,
                    "subjects": subjects, "supersedes": supersedes, "source_session_ids": [session["id"]]}
            self.context.history.append(session)
            self.context.sessions_by_id[session["id"]] = session
            self.context.facts.append(fact)
            self.context.facts_by_id[index] = fact
        result = source_context(self.context, self.idea)
        self.assertEqual([row["id"] for row in result["related_updates"]], [3, 4])
        self.assertEqual([row["source_session_id"] for row in result["sources"]], ["s1", "s3", "s4"])
        self.assertEqual(result["sources"][-1]["text"], "Complete original message 4.")

    def test_validation_collects_independent_errors(self):
        raw = writer()
        raw["test"]["evidence"][0]["quote"] = "an invented funding round"
        raw["test"]["checks"][0]["assertion"]["path"] = "args.nonexistent"
        with self.assertRaises(AuthoringValidationError) as error:
            self.candidate(raw)
        self.assertIn("exact passage", str(error.exception))
        self.assertIn("unknown argument", str(error.exception))

    def test_same_action_cannot_be_split_across_emails(self):
        candidate = self.candidate()
        actual = calls(BAD) + calls(GOOD, "someoneelse@example.com")
        result = grade_tool_trace(actual, candidate["grade"]["config"], candidate["test"])
        self.assertFalse(result["passed"])

    def test_two_complete_examples_use_actual_grader_and_cache(self):
        response = ReviewResponse.model_validate(approving_review(before=True))
        request = self.review_request()
        grader = Mock(side_effect=grade_tool_trace)
        for _ in range(2):
            result = check_review_examples(response=response, request=request, out=self.root / "examples", grader=grader)
            self.assertTrue(result["passed"])
        self.assertEqual(grader.call_count, 2)

    def test_unrelated_failure_does_not_count_as_named_negative(self):
        raw = approving_review(before=True)
        raw["examples"]["incorrect_calls"] = [{"tool": "send_email", "args_json": json.dumps(calls(GOOD, "wrong@example.com")[0]["args"])}]
        result = check_review_examples(response=ReviewResponse.model_validate(raw), request=self.review_request(), out=self.root / "examples")
        self.assertFalse(result["passed"])

    def test_grader_service_error_does_not_pass_negative_example(self):
        with self.assertRaisesRegex(RuntimeError, "offline error"):
            check_review_examples(response=ReviewResponse.model_validate(approving_review(before=True)),
                                  request=self.review_request(), out=self.root / "examples",
                                  grader=Mock(side_effect=RuntimeError("offline error")))

    def test_review_quote_must_match_its_location(self):
        raw = {"decision": "reject", "examples": None, "issues": [{
            "part": "request", "location": "/user_request", "problem": "Unsupported work.",
            "required_change": "Fix the request.", "evidence": [{"location": "/user_request", "quote": "not supplied"}],
            "counterexample": None, "missing_check": None,
        }]}
        with self.assertRaisesRegex(ValueError, "quote does not match"):
            validate_review(ReviewResponse.model_validate(raw), self.review_request())

    def test_provider_review_schema_omits_redundant_quote(self):
        schema = review_response_schema()
        evidence = schema["$defs"]["ReviewEvidence"]
        self.assertEqual(evidence["required"], ["location"])
        self.assertNotIn("quote", evidence["properties"])

    def test_execute_review_materializes_exact_pointed_evidence(self):
        raw = {"decision": "reject", "examples": None, "issues": [{
            "part": "request", "location": "/user_request", "problem": "Unsupported work.",
            "required_change": "Fix the request.", "evidence": [{"location": "/user_request"}],
            "counterexample": None, "missing_check": None,
        }]}
        out = self.root / "review"
        response = execute_review(
            request=self.review_request(), out=out, client=ScriptedClient(raw),
            confirm_paid_calls=True,
        )
        expected = self.review_request()["user_request"]
        self.assertEqual(response.issues[0].evidence[0].quote, expected)
        self.assertNotIn("quote", json.loads((out / "response.json").read_text())["issues"][0]["evidence"][0])
        self.assertEqual(json.loads((out / "review.json").read_text())["issues"][0]["evidence"][0]["quote"], expected)

    def test_pending_review_is_not_a_rejection(self):
        raw = {"decision": "pending", "examples": None, "issues": [{
            "part": "system", "location": "/missing", "problem": "The tool result is missing.",
            "required_change": "Recover the missing result.", "evidence": [],
            "counterexample": None, "missing_check": None,
        }]}
        with self.assertRaises(ReviewPendingError):
            execute_review(request=self.review_request(), out=self.root / "review", client=ScriptedClient(raw), confirm_paid_calls=True)
        self.assertTrue((self.root / "review" / "response.json").is_file())

    def test_check_correction_preserves_unaffected_checks_and_request(self):
        previous = parse_writer_response(writer())
        scope = correction_scope([{"part": "checks", "location": "/grading_checks/1"}], previous)
        current = previous.model_copy(deep=True)
        current.test.checks[1].assertion["criterion"] += " Equivalent wording is allowed."
        validate_writer_correction(previous, current, scope)
        current.test.checks[0].assertion["value"] = "wrong@example.com"
        with self.assertRaisesRegex(AuthoringValidationError, "unaffected"):
            validate_writer_correction(previous, current, scope)

    def test_request_correction_does_not_implicitly_allow_all_checks(self):
        previous = parse_writer_response(writer())
        scope = correction_scope([{"part": "request", "location": "/user_request"}], previous)
        self.assertEqual(scope["allowed_correction_fields"], ["test.request"])

    def test_mixed_execution_issue_preserves_only_named_check_permissions(self):
        previous = parse_writer_response(writer())
        issues = [{"part": "checks", "location": "/grading_checks/1/criterion"},
                  {"part": "checks", "location": "/grading_checks"},
                  {"part": "execution", "location": "/executions/0/tool_calls/0"}]
        scope = correction_scope(issues, previous)
        self.assertEqual(scope["allowed_correction_fields"], ["test.checks"])
        self.assertEqual(scope["protected_check_ids"], ["recipient"])
        for blocker in ("planning", "system"):
            with self.subTest(blocker=blocker):
                self.assertEqual(correction_scope(issues + [{"part": blocker}], previous)
                                 ["allowed_correction_fields"], [])
        self.assertEqual(correction_scope([issues[-1]], previous)["allowed_correction_fields"], [])

    def test_input_budget_stops_before_any_model_call(self):
        from authoring.run import _create_one_test
        client = ScriptedClient()
        result = _create_one_test(
            directory=self.root / "run" / "proposals" / "001", accepted_dir=self.root / "candidates",
            payload=self.payload, planned_task=self.idea, context=self.context,
            config=self.config.model_copy(update={"writer_max_input_tokens": 1}),
            test_id=1, client=client, certifier=Mock(side_effect=AssertionError("unexpected certification")),
        )
        self.assertEqual(result["status"], "input_pending")
        self.assertFalse(client.calls)

    def test_ordinary_oracle_prompt_keeps_history_out_of_no_history(self):
        candidate = self.candidate()
        candidate["_oracle_input"] = certification_input(self.context, self.idea, self.config.evaluation_date)
        with_history = build_system_prompt(candidate, "morgan", with_memory=True)
        without = build_system_prompt(candidate, "morgan", with_memory=False)
        self.assertIn(GOOD, with_history)
        self.assertNotIn(GOOD, without)
        self.assertIn("2025-05-01T15:30:00-07:00", with_history)
        self.assertNotIn("CLOSEST TOOL", with_history)
        self.assertNotIn("READING IS NOT ACTING", with_history)
        self.assertEqual(with_history.split("\n\nEarlier user messages:")[0], without)

    def test_single_planning_call_assigns_ids_and_stops_for_approval(self):
        from authoring.propose import execute_proposals, prepare_proposals
        response = {"ideas": [self.idea.model_dump(exclude={"authoring_version", "idea_id"})], "shortfall_reason": ""}
        client = ScriptedClient(response)
        out = self.root / "planning"
        with patch("authoring.propose.load_checkpoint_context", return_value=self.context):
            prepare_proposals(config_path=self.config_path, out=out, count=1)
            batch = execute_proposals(config_path=self.config_path, out=out, count=1, client=client, confirm_paid_calls=True)
        self.assertEqual(len(client.calls), 1)
        self.assertIsInstance(batch.tasks[0], ApprovedIdea)
        self.assertEqual(batch.tasks[0].idea_id, 1)
        self.assertNotIn("sources", client.calls[0]["payload"])
        self.assertFalse((out / "evidence_request.json").exists())
        self.assertEqual(json.loads((out / "manifest.json").read_text())["status"], "awaiting_human_acceptance")

    def test_replacement_facts_stay_together_in_balanced_planner_shards(self):
        from authoring.propose import _planning_fact_shards, _sharded_proposal_requests

        request = {
            "current_facts": [
                {"id": 1, "subjects": ["project:a"], "supersedes": [], "latest_source_date": "2025-01-01"},
                {"id": 2, "subjects": ["project:a"], "supersedes": [], "latest_source_date": "2025-02-01"},
                {"id": 3, "subjects": ["project:b"], "supersedes": [], "latest_source_date": "2025-01-01"},
                {"id": 4, "subjects": ["project:c"], "supersedes": [5], "latest_source_date": "2025-02-01"},
                {"id": 5, "subjects": ["project:d"], "supersedes": [], "latest_source_date": "2025-01-01"},
            ]
        }
        shards = _planning_fact_shards(
            request=request, persona="morgan", shard_count=3
        )
        self.assertEqual(sorted(value for shard in shards for value in shard), [1, 2, 3, 4, 5])
        self.assertEqual(
            sum(len(set(left) & set(right)) for left in shards for right in shards if left is not right),
            0,
        )
        self.assertTrue(any({4, 5}.issubset(shard) for shard in map(set, shards)))
        requests = _sharded_proposal_requests(
            request=request, persona="morgan", shard_count=3
        )
        rows_for_one = next(
            shard["current_facts"]
            for shard in requests
            if 1 in shard["planning_shard"]["assigned_fact_ids"]
        )
        row_two = next(row for row in rows_for_one if row["id"] == 2)
        self.assertFalse(row_two["available_for_new_idea"])

    def test_parallel_planner_shards_merge_with_stable_global_ids(self):
        from authoring.propose import execute_proposals, prepare_proposals

        session = {
            "id": "s2",
            "narrative_date": "2025-06-01T12:00:00-07:00",
            "message": "Use the North Pier office for vendor meetings.",
        }
        fact = {
            "id": 2,
            "statement": session["message"],
            "source_session_ids": ["s2"],
            "subjects": ["place:north_pier"],
            "applies_when": "vendor meetings",
            "supersedes": [],
        }
        context = SimpleNamespace(**vars(self.context))
        context.history = [*self.context.history, session]
        context.facts = [*self.context.facts, fact]
        context.facts_by_id = {**self.context.facts_by_id, 2: fact}
        context.sessions_by_id = {**self.context.sessions_by_id, "s2": session}
        context.active_fact_ids = {1, 2}
        out = self.root / "sharded_planning"
        calls_seen: list[int] = []

        def complete(_cache, _system, payload, _client, **_kwargs):
            fact_id = int(payload["current_facts"][0]["id"])
            calls_seen.append(fact_id)
            return {
                "ideas": [
                    {
                        "situation": f"Situation for fact {fact_id}.",
                        "work": f"Send the update supported by fact {fact_id}.",
                        "fact_ids": [fact_id],
                        "expected_tools": ["send_email"],
                        "history_needed": f"Fact {fact_id} supplies required content.",
                        "likely_without_history": "The assistant omits that content.",
                    }
                ],
                "shortfall_reason": "",
            }

        with (
            patch("authoring.propose.load_checkpoint_context", return_value=context),
            patch("authoring.propose.cached_client_complete", side_effect=complete),
        ):
            prepare_proposals(
                config_path=self.config_path,
                out=out,
                count=1,
                shards=2,
                concurrency=2,
            )
            batch = execute_proposals(
                config_path=self.config_path,
                out=out,
                count=1,
                shards=2,
                concurrency=2,
                client=SimpleNamespace(model="offline"),
                confirm_paid_calls=True,
            )

        self.assertEqual(sorted(calls_seen), [1, 2])
        self.assertEqual([task.idea_id for task in batch.tasks], [1, 2])
        self.assertEqual(
            sorted(task.fact_ids[0] for task in batch.tasks),
            [1, 2],
        )
        self.assertTrue((out / "shards" / "01" / "planner_raw_response.json").is_file())
        self.assertTrue((out / "shards" / "02" / "planner_raw_response.json").is_file())
        self.assertEqual(
            json.loads((out / "manifest.json").read_text())["status"],
            "awaiting_human_acceptance",
        )

    def test_parallel_planner_preserves_valid_shards_when_one_call_fails(self):
        from authoring.propose import execute_proposals, prepare_proposals

        session = {
            "id": "s2",
            "narrative_date": "2025-06-01T12:00:00-07:00",
            "message": "Use the North Pier office for vendor meetings.",
        }
        fact = {
            "id": 2,
            "statement": session["message"],
            "source_session_ids": ["s2"],
            "subjects": ["place:north_pier"],
            "applies_when": "vendor meetings",
            "supersedes": [],
        }
        context = SimpleNamespace(**vars(self.context))
        context.history = [*self.context.history, session]
        context.facts = [*self.context.facts, fact]
        context.facts_by_id = {**self.context.facts_by_id, 2: fact}
        context.sessions_by_id = {**self.context.sessions_by_id, "s2": session}
        context.active_fact_ids = {1, 2}
        out = self.root / "partially_failed_sharded_planning"

        def complete(_cache, _system, payload, _client, **_kwargs):
            fact_id = int(
                next(
                    row
                    for row in payload["current_facts"]
                    if row["available_for_new_idea"] is True
                )["id"]
            )
            if fact_id == 2:
                raise RuntimeError("temporary provider failure")
            idea = self.idea.model_dump(exclude={"authoring_version", "idea_id"})
            idea.update(
                situation=f"Situation for fact {fact_id}.",
                work=f"Send the update supported by fact {fact_id}.",
                fact_ids=[fact_id],
                history_needed=f"Fact {fact_id} supplies required content.",
                likely_without_history="The assistant omits that content.",
            )
            return {"ideas": [idea], "shortfall_reason": ""}

        with (
            patch("authoring.propose.load_checkpoint_context", return_value=context),
            patch("authoring.propose.cached_client_complete", side_effect=complete),
        ):
            prepare_proposals(
                config_path=self.config_path,
                out=out,
                count=1,
                shards=2,
                concurrency=2,
            )
            batch = execute_proposals(
                config_path=self.config_path,
                out=out,
                count=1,
                shards=2,
                concurrency=2,
                client=SimpleNamespace(model="offline"),
                confirm_paid_calls=True,
            )

        self.assertEqual([task.idea_id for task in batch.tasks], [1])
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "awaiting_human_acceptance")
        self.assertEqual(
            manifest["failed_shards"],
            [
                {
                    "index": 2,
                    "failure_stage": "planner_call",
                    "reason": "RuntimeError: temporary provider failure",
                }
            ],
        )
        self.assertTrue((out / "candidate_plan.json").is_file())
        self.assertTrue((out / "shards" / "01" / "proposals.json").is_file())
        self.assertTrue((out / "shards" / "02" / "result.json").is_file())

    def test_public_creation_accepts_batch_and_next_planner_reads_it(self):
        from authoring.propose import proposal_request
        client = ScriptedClient(writer(), approving_review(before=True))
        manifest = self.create_batch(client)
        self.assertEqual(manifest["final_review_accepted"], 1, manifest)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(
            manifest["final_review_results"][0]["status"],
            "accepted_clean_certification",
        )
        next_request = proposal_request(config=self.config, context=self.context, count=1)
        self.assertEqual(len(next_request["prior_work"]["accepted"]), 1)
        self.assertEqual(next_request["current_facts"][0]["used_by_test_ids"], [1])
        self.assertEqual(load_authoring_tasks(context=self.context, config=self.config,
                                             path=Path(manifest["accepted_batch"]) / "planning_batch.json"), [self.idea])

    def test_clean_certification_requires_each_no_memory_run_to_fail_memory_check(self):
        from authoring.create_tests import _clean_certification_evidence

        manifest = self.create_batch(
            ScriptedClient(writer(), approving_review(before=True))
        )
        completed = manifest["final_review_results"][0]
        candidate = Path(completed["candidate"])
        gate = Path(completed["gate"])
        clean, evidence = _clean_certification_evidence(
            candidate=candidate, gate=gate, planned_task=self.idea
        )
        self.assertTrue(clean, evidence)

        altered = json.loads(gate.read_text())
        no_memory = next(
            shot
            for shot in altered["result"]["shots"]
            if shot["with_memory"] is False
        )
        funding = next(
            detail for detail in no_memory["grade"]["details"]
            if detail["name"] == "funding"
        )
        funding["ok"] = True
        altered_gate = self.root / "no_memory_failed_unrelated_check.json"
        dump_json(altered_gate, altered)
        clean, evidence = _clean_certification_evidence(
            candidate=candidate, gate=altered_gate, planned_task=self.idea
        )
        self.assertFalse(clean)
        self.assertTrue(
            any("did not fail a remembered-result check" in reason for reason in evidence["reasons"]),
            evidence,
        )

    def test_clean_certification_routes_execution_errors_to_final_review(self):
        from authoring.create_tests import _clean_certification_evidence

        manifest = self.create_batch(
            ScriptedClient(writer(), approving_review(before=True))
        )
        completed = manifest["final_review_results"][0]
        candidate = Path(completed["candidate"])
        gate = Path(completed["gate"])
        altered = json.loads(gate.read_text())
        altered["result"]["shots"][2]["error"] = "provider timeout"
        altered_gate = self.root / "errored_gate.json"
        dump_json(altered_gate, altered)

        clean, evidence = _clean_certification_evidence(
            candidate=candidate, gate=altered_gate, planned_task=self.idea
        )

        self.assertFalse(clean)
        self.assertTrue(
            any("contains an error" in reason for reason in evidence["reasons"]),
            evidence,
        )

    def test_clean_certification_authenticates_preflight_and_unique_grade_rows(self):
        from authoring.create_tests import _clean_certification_evidence

        manifest = self.create_batch(
            ScriptedClient(writer(), approving_review(before=True))
        )
        completed = manifest["final_review_results"][0]
        candidate = Path(completed["candidate"])
        gate = Path(completed["gate"])
        preflight_result_path = gate.parent / "initial_preflight" / "result.json"
        original_preflight = preflight_result_path.read_text()
        altered_preflight = json.loads(original_preflight)
        altered_preflight["candidate_sha256"] = "0" * 64
        dump_json(preflight_result_path, altered_preflight)
        clean, evidence = _clean_certification_evidence(
            candidate=candidate, gate=gate, planned_task=self.idea
        )
        self.assertFalse(clean)
        self.assertTrue(
            any("does not authenticate" in reason for reason in evidence["reasons"]),
            evidence,
        )
        preflight_result_path.write_text(original_preflight)

        original_gate = gate.read_text()
        altered_gate = json.loads(original_gate)
        details = altered_gate["result"]["shots"][0]["grade"]["details"]
        details.append(copy.deepcopy(details[0]))
        dump_json(gate, altered_gate)
        clean, evidence = _clean_certification_evidence(
            candidate=candidate, gate=gate, planned_task=self.idea
        )
        self.assertFalse(clean)
        self.assertTrue(
            any("exactly once" in reason for reason in evidence["reasons"]),
            evidence,
        )
        gate.write_text(original_gate)

    def test_historical_manual_correction_modes_do_not_consume_new_ideas(self):
        from authoring.create_tests import _approved_new_test_resume_inputs, _load_approved_revision_inputs
        manifest = self.root / "revisions.json"
        dump_json(manifest, {"version": 1, "approved_plan": str(self.plan), "candidates": {}})
        with self.assertRaisesRegex(ValueError, "historical plans only"):
            _load_approved_revision_inputs(manifest_path=manifest, config=self.config, context=self.context)
        with self.assertRaisesRegex(ValueError, "historical plans only"):
            _approved_new_test_resume_inputs(
                config_path=self.config_path, plan_path=self.plan, source_run=self.root / "missing",
                corrections_path=self.root / "corrections.json", test_ids=[1], config=self.config,
                context=self.context, tasks=[self.idea], out=self.root / "resume",
            )

    def test_grading_only_correction_reuses_all_four_executions(self):
        revised = writer()
        revised["test"]["checks"][1]["assertion"]["criterion"] += " Equivalent wording is allowed."
        client = ScriptedClient(writer(), approving_review(before=True), self.check_issue(),
                                revised, approving_review(before=True), approving_review(before=False))
        certifier = Mock(side_effect=self.certify)
        manifest = self.create_batch(client, certifier=certifier, force_final_review=True)
        self.assertEqual(manifest["final_review_accepted"], 1, manifest)
        self.assertEqual(certifier.call_count, 1)
        result = manifest["final_review_results"][0]
        self.assertEqual(result["model_corrections_used"], 1)
        self.assertEqual(result["execution_reruns_used"], 0)
        self.assertEqual(len(client.calls), 6)


    def test_mixed_correction_edits_checks_then_retries_execution_once(self):
        revised = writer()
        revised["test"]["checks"][1]["assertion"]["criterion"] += " Equivalent wording is allowed."
        client = ScriptedClient(writer(), approving_review(before=True), self.mixed_check_execution_issue(),
                                revised, approving_review(before=True), approving_review(before=False))
        certifier = Mock(side_effect=self.certify)
        manifest = self.create_batch(client, certifier=certifier, force_final_review=True)
        self.assertEqual(manifest["final_review_accepted"], 1, manifest)
        self.assertEqual(certifier.call_count, 2)
        payload = client.calls[3]["payload"]
        self.assertEqual(payload["allowed_correction_fields"], ["test.checks"])
        self.assertEqual(payload["protected_check_ids"], ["recipient"])
        result = manifest["final_review_results"][0]
        self.assertEqual(result["model_corrections_used"], 1)
        self.assertEqual(result["execution_reruns_used"], 1)

    def test_mixed_correction_stopped_before_execution_does_not_spend_retry(self):
        client = ScriptedClient(writer(), approving_review(before=True), self.mixed_check_execution_issue(),
                                {"status": "cannot_write", "problem": "No valid correction.",
                                 "test": None, "quality_audit": None})
        certifier = Mock(side_effect=self.certify)
        manifest = self.create_batch(client, certifier=certifier, force_final_review=True)
        self.assertEqual(manifest["final_review_accepted"], 0, manifest)
        self.assertEqual(certifier.call_count, 1)
        result = manifest["final_review_results"][0]
        self.assertEqual(result["model_corrections_used"], 1)
        self.assertEqual(result["execution_reruns_used"], 0)

    def test_empty_correction_scope_never_calls_writer(self):
        client = ScriptedClient(writer(), approving_review(before=True), self.check_issue())
        with patch("authoring.complete_test.correction_scope", return_value={
            "allowed_correction_fields": [], "protected_check_ids": [],
        }):
            manifest = self.create_batch(client, force_final_review=True)
        self.assertEqual(len(client.calls), 3)
        result = manifest["final_review_results"][0]
        self.assertEqual(result["status"], "correction_pending")
        self.assertEqual(result["model_corrections_used"], 0)
        self.assertEqual(result["execution_reruns_used"], 0)
        revised = writer()
        revised["test"]["checks"][1]["assertion"]["criterion"] += " Equivalent wording is allowed."
        resumed = self.resume_batch("create", ScriptedClient(
            revised, approving_review(before=True), approving_review(before=False),
        ), certifier=Mock(side_effect=AssertionError("must reuse saved executions")))
        self.assertEqual(resumed["final_review_accepted"], 1, resumed)
        self.assertEqual(resumed["final_review_results"][0]["model_corrections_used"], 1)

    def test_interrupted_preflight_resumes_without_rewriting(self):
        first_client = ScriptedClient(writer())
        manifest = self.create_batch(first_client)
        self.assertEqual(manifest["pending_count"], 1, manifest)
        before = {str(path): path.read_bytes() for path in (self.root / "create").rglob("*") if path.is_file()}
        client = ScriptedClient(approving_review(before=True))
        result = self.resume_batch("create", client)
        self.assertEqual(result["final_review_accepted"], 1, result)
        self.assertEqual(len(client.calls), 1)
        for path, content in before.items():
            self.assertEqual(Path(path).read_bytes(), content)

    def test_approved_pending_draft_correction_uses_one_scoped_writer_call(self):
        path = self.pending_draft_correction()
        before = {str(p): p.read_bytes() for p in (self.root / "create").rglob("*") if p.is_file()}
        client = ScriptedClient(writer(), approving_review(before=True))
        result = self.resume_batch("create", client, corrections=path)
        self.assertEqual(result["final_review_accepted"], 1, result)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("previous_authoring", client.calls[0]["payload"])
        self.assertEqual(result["final_review_results"][0]["model_corrections_used"], 1)
        for name, content in before.items():
            self.assertEqual(Path(name).read_bytes(), content)

    def test_approved_pending_correction_rejects_changed_binding_before_calls(self):
        path = self.pending_draft_correction()
        value = json.loads(path.read_text())
        value["corrections"][0]["source_response_sha256"] = "wrong"
        dump_json(path, value)
        client = ScriptedClient()
        with self.assertRaisesRegex(ValueError, "does not match the saved response"):
            self.resume_batch("create", client, corrections=path)
        self.assertEqual(client.calls, [])

    def test_interrupted_approved_writer_correction_keeps_scope_on_resume(self):
        path = self.pending_draft_correction()
        result = self.resume_batch("create", ScriptedClient(), corrections=path)
        self.assertEqual(result["authoring_results"][0]["status"], "authoring_pending", result)
        client = ScriptedClient(writer(), approving_review(before=True))
        result = self.resume_batch("resume", client, name="continued")
        self.assertEqual(result["final_review_accepted"], 1, result)
        self.assertIn("previous_authoring", client.calls[0]["payload"])
        self.assertEqual(client.calls[0]["payload"]["protected_check_ids"], ["recipient"])
        self.assertEqual(result["final_review_results"][0]["model_corrections_used"], 1)

    def test_pending_writer_correction_cannot_change_source_evidence(self):
        path = self.pending_draft_correction()
        revised = writer()
        revised["test"]["evidence"][0]["quote"] = "Series B"
        result = self.resume_batch("create", ScriptedClient(revised), corrections=path)
        self.assertEqual(result["authoring_results"][0]["status"], "validation_pending", result)
        self.assertEqual(result["final_review_accepted"], 0)

    def test_exact_criterion_edit_reuses_executions_without_a_writer_call(self):
        path = self.exact_criterion_correction()
        before = {str(p): p.read_bytes() for p in (self.root / "resume").rglob("*") if p.is_file()}
        client = ScriptedClient(approving_review(before=True), approving_review(before=False))
        certifier = Mock(side_effect=AssertionError("must reuse saved executions"))
        result = self.resume_batch("resume", client, corrections=path, name="regraded", certifier=certifier)
        self.assertEqual(result["final_review_accepted"], 1, result)
        self.assertEqual(len(client.calls), 2)
        certifier.assert_not_called()
        self.assertEqual(result["final_review_results"][0]["model_corrections_used"], 1)
        for name, content in before.items():
            self.assertEqual(Path(name).read_bytes(), content)

    def test_exact_criterion_edit_rejects_changed_gate_before_calls(self):
        path = self.exact_criterion_correction()
        value = json.loads(path.read_text())
        value["corrections"][0]["source_gate_sha256"] = "wrong"
        dump_json(path, value)
        client = ScriptedClient()
        with self.assertRaisesRegex(ValueError, "matching gate"):
            self.resume_batch("resume", client, corrections=path, name="regraded")
        self.assertEqual(client.calls, [])

    def test_exact_criterion_edit_resume_preserves_saved_attempts(self):
        path = self.exact_criterion_correction()
        certifier = Mock(side_effect=AssertionError("must reuse saved executions"))
        first = self.resume_batch("resume", ScriptedClient(), corrections=path, name="regraded", certifier=certifier)
        self.assertEqual(first["authoring_results"][0]["status"], "preflight_pending")
        client = ScriptedClient(approving_review(before=True), approving_review(before=False))
        result = self.resume_batch("regraded", client, name="regraded_resumed", certifier=certifier)
        self.assertEqual(result["final_review_accepted"], 1, result)
        self.assertEqual(len(client.calls), 2)
        certifier.assert_not_called()

    def test_failed_execution_retry_still_receives_final_review(self):
        issue = {"decision": "reject", "examples": None, "issues": [{
            "part": "execution", "location": "/executions/0/passed",
            "problem": "The assistant omitted the required funding information.",
            "required_change": "Retry the unchanged test once.",
            "evidence": [{"location": "/executions/0/passed", "quote": "false"}],
            "counterexample": None, "missing_check": None,
        }]}

        def failing_certifier(*args, **kwargs):
            result = self.certify(*args, **kwargs)
            candidate = yaml.safe_load(args[1].read_text())
            shot = result["shots"][0]
            shot["tool_calls"] = calls(BAD)
            shot["tool_results"] = [{"call_index": 0, **shot["tool_calls"][0], "result": {"sent": True}}]
            shot["grade"] = grade_tool_trace(shot["tool_calls"], candidate["grade"]["config"], candidate["test"])
            shot["passed"] = False
            result.update(valid=False, verdict="with-history failure", g1_pass_count=1)
            return result

        certifier = Mock(side_effect=failing_certifier)
        client = ScriptedClient(writer(), approving_review(before=True), issue, issue)
        manifest = self.create_batch(client, certifier=certifier)
        self.assertEqual(manifest["final_review_accepted"], 0, manifest)
        self.assertEqual(certifier.call_count, 2)
        self.assertEqual(len(client.calls), 4)
        first, second = (call.args[1].read_bytes() for call in certifier.call_args_list)
        self.assertEqual(first, second)
        self.assertEqual(client.calls[-1]["payload"]["phase"], "after_oracle")
        self.assertFalse(client.calls[-1]["payload"]["certification_result"]["valid"])

    def test_clean_execution_retry_reuses_preflight_without_second_review(self):
        issue = {"decision": "reject", "examples": None, "issues": [{
            "part": "execution", "location": "/executions/0/passed",
            "problem": "The assistant omitted the required funding information.",
            "required_change": "Retry the unchanged test once.",
            "evidence": [{"location": "/executions/0/passed", "quote": "false"}],
            "counterexample": None, "missing_check": None,
        }]}

        def failed_first_certification(*args, **kwargs):
            result = self.certify(*args, **kwargs)
            candidate = yaml.safe_load(args[1].read_text())
            shot = result["shots"][0]
            shot["tool_calls"] = calls(BAD)
            shot["tool_results"] = [
                {"call_index": 0, **shot["tool_calls"][0], "result": {"sent": True}}
            ]
            shot["grade"] = grade_tool_trace(
                shot["tool_calls"], candidate["grade"]["config"], candidate["test"]
            )
            shot["passed"] = False
            result.update(valid=False, verdict="with-history failure", g1_pass_count=1)
            return result

        certifier = Mock(side_effect=[
            failed_first_certification,
            self.certify,
        ])

        def run_certifier(*args, **kwargs):
            implementation = certifier(*args, **kwargs)
            return implementation(*args, **kwargs)

        client = ScriptedClient(writer(), approving_review(before=True), issue)
        manifest = self.create_batch(client, certifier=run_certifier)

        self.assertEqual(manifest["final_review_accepted"], 1, manifest)
        self.assertEqual(certifier.call_count, 2)
        self.assertEqual(len(client.calls), 3)
        result = manifest["final_review_results"][0]
        self.assertEqual(result["status"], "accepted_after_final_review_correction")
        self.assertEqual(result["corrected_final_model_review"], "not_required")

    def test_interrupted_grading_correction_resumes_without_new_agent_attempts(self):
        revised = writer()
        revised["test"]["checks"][1]["assertion"]["criterion"] += " Equivalent wording is allowed."
        first = ScriptedClient(writer(), approving_review(before=True), self.check_issue(), revised)
        certifier = Mock(side_effect=self.certify)
        manifest = self.create_batch(first, certifier=certifier, force_final_review=True)
        self.assertEqual(manifest["pending_count"], 1, manifest)
        self.assertEqual(certifier.call_count, 1)
        next_client = ScriptedClient(approving_review(before=True), approving_review(before=False))
        resumed = self.resume_batch("create", next_client, certifier=Mock(side_effect=AssertionError("must reuse saved executions")))
        self.assertEqual(resumed["final_review_accepted"], 1, resumed)
        self.assertEqual(resumed["final_review_results"][0]["model_corrections_used"], 1)
        self.assertEqual(len(next_client.calls), 1)

    def test_interrupted_mixed_correction_preserves_retry_count_on_resume(self):
        revised = writer()
        revised["test"]["checks"][1]["assertion"]["criterion"] += " Equivalent wording is allowed."
        manifest = self.create_batch(ScriptedClient(
            writer(), approving_review(before=True), self.mixed_check_execution_issue(), revised,
        ), force_final_review=True)
        self.assertEqual(manifest["pending_count"], 1, manifest)
        self.assertEqual(manifest["final_review_results"][0]["execution_reruns_used"], 0)
        certifier = Mock(side_effect=self.certify)
        resumed = self.resume_batch("create", ScriptedClient(
            approving_review(before=True), approving_review(before=False),
        ), certifier=certifier)
        self.assertEqual(resumed["final_review_accepted"], 1, resumed)
        self.assertEqual(certifier.call_count, 1)
        self.assertEqual(resumed["final_review_results"][0]["execution_reruns_used"], 1)
        self.assertEqual(resumed["final_review_results"][0]["model_corrections_used"], 1)

    def test_model_correction_cannot_change_an_unrelated_check(self):
        revised = writer()
        revised["test"]["checks"][0]["assertion"]["value_json"] = '"other@example.com"'
        client = ScriptedClient(writer(), approving_review(before=True), self.check_issue(), revised)
        manifest = self.create_batch(client, force_final_review=True)
        self.assertEqual(manifest["final_review_accepted"], 0)
        self.assertEqual(manifest["pending_count"], 1)
        failure = manifest["final_review_results"][0]["correction"]["attempt"]
        self.assertIn("unaffected", failure["reason"])

    def test_accepting_final_review_cannot_hide_missing_execution_evidence(self):
        from authoring.final_trace_review import build_final_trace_review_request
        from authoring.pipeline import write_candidate
        candidate_path = self.root / "test.yaml"
        write_candidate(candidate_path, self.candidate())
        gate = self.certify("morgan", candidate_path, checkpoint=self.context.checkpoint_path,
                            confirm_paid_calls=True, oracle_input=certification_input(self.context, self.idea, self.config.evaluation_date))
        gate["shots"][0].pop("tool_results")
        path = self.root / "gate.json"
        dump_json(path, {"candidate_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
                         "authoring_evidence": authoring_evidence(parse_writer_response(writer())), "result": gate})
        request = build_final_trace_review_request(candidate_path=candidate_path, certification_gate_path=path,
                                                  planned_task=self.idea, context=self.context)
        with self.assertRaisesRegex(ValueError, "tool results"):
            validate_review(ReviewResponse.model_validate(approving_review(before=False)), request)

    def test_accepting_final_review_cannot_count_an_error_as_no_history_failure(self):
        from authoring.review import validate_certification
        request = self.review_request()
        request.update(phase="after_oracle", certification_result={"valid": True}, executions=[
            {"with_memory": True, "error": "provider failed"}, {"with_memory": True},
            {"with_memory": False}, {"with_memory": False},
        ])
        with self.assertRaisesRegex(ValueError, "execution error"):
            validate_certification(request)

    def test_six_canonical_examples_have_explicit_review_instructions(self):
        from authoring.prompts import COMPLETE_TEST_WRITER_SYSTEM, IDEA_PLANNER_SYSTEM, TEST_REVIEW_SYSTEM
        for prompt in (IDEA_PLANNER_SYSTEM, COMPLETE_TEST_WRITER_SYSTEM, TEST_REVIEW_SYSTEM):
            self.assertIn("content", prompt)
            self.assertIn("newer", prompt)
        self.assertIn("standalone recall", IDEA_PLANNER_SYSTEM)
        self.assertIn("required remembered information unstated", COMPLETE_TEST_WRITER_SYSTEM)
        self.assertIn("does not globally invalidate its facts", TEST_REVIEW_SYSTEM)
        self.assertIn("same tool call", TEST_REVIEW_SYSTEM)
        # Prompt coverage is not evidence of real-model judgment quality.
        candidate = self.candidate()
        self.assertTrue(grade_tool_trace(calls(GOOD), candidate["grade"]["config"], candidate["test"])["passed"])
        self.assertFalse(grade_tool_trace(calls(BAD), candidate["grade"]["config"], candidate["test"])["passed"])

    def test_review_receives_exact_judge_semantics(self):
        from graders.llm_judge import FIELD_JUDGE_SYSTEM_PROMPT
        request = self.review_request()
        self.assertEqual(request["review_contract_version"], 1)
        self.assertEqual(request["grading_semantics"]["field_judge_system"], FIELD_JUDGE_SYSTEM_PROMPT)
        self.assertIn("complete arguments of the SAME call", request["grading_semantics"]["field_judge_inputs"])

    def test_prose_only_check_rejection_stays_pending(self):
        raw = self.check_issue()
        raw["issues"][0]["counterexample"] = None
        with self.assertRaisesRegex(ReviewPendingError, "exactly one"):
            execute_review(request=self.review_request(), out=self.root / "review", client=ScriptedClient(raw), confirm_paid_calls=True)
        self.assertTrue((self.root / "review/response.json").exists())
        self.assertFalse((self.root / "review/decision.json").exists())

    def test_reproduced_check_defect_is_retained_and_cached(self):
        from authoring.review import check_rejection_examples
        response = ReviewResponse.model_validate(self.check_issue())
        request = self.review_request()
        validate_review(response, request)
        grader = Mock(side_effect=lambda c, config, test_message: grade_tool_trace(c, config, test_message))
        for _ in range(2):
            check_rejection_examples(response=response, request=request, out=self.root / "rejection", grader=grader)
        self.assertEqual(grader.call_count, 1)
        saved = json.loads((self.root / "rejection/issue_0.json").read_text())
        self.assertTrue(saved["claim_reproduced"])
        request["user_request"] += " Send it today."
        check_rejection_examples(response=response, request=request, out=self.root / "rejection", grader=grader)
        self.assertEqual(grader.call_count, 2)

    def test_cc_claim_is_checked_against_full_same_call(self):
        from authoring.review import check_rejection_examples
        request = self.review_request()
        request["grading_checks"][0] = {"check_id": "recipient", "type": "field_llm_judge", "tool": "send_email",
                                        "action_id": "email", "path": "args.to", "criterion": "Pat must be in To or CC."}
        raw = self.check_issue()
        issue = raw["issues"][0]
        issue.update(location="/grading_checks/0", evidence=[{"location": "/grading_checks/0/path", "quote": "args.to"}])
        args = {**calls()[0]["args"], "to": "assistant@example.com", "cc": ["pat@example.com"]}
        issue["counterexample"].update(check_id="recipient", calls=[{"tool": "send_email", "args_json": json.dumps(args)}])
        response = ReviewResponse.model_validate(raw)
        def contextual_judge(value, criterion, **kwargs):
            if criterion == "Pat must be in To or CC.":
                context = json.loads(kwargs["context"])
                return {"ok": "pat@example.com" in context["arguments"].get("cc", []), "reason": "same-call CC"}
            return self.judge(value, criterion, **kwargs)
        validate_review(response, request)
        with patch("graders.llm_judge.judge_field_value", side_effect=contextual_judge):
            with self.assertRaisesRegex(ReviewPendingError, "disagrees"):
                check_rejection_examples(response=response, request=request, out=self.root / "cc")
        self.assertFalse(json.loads((self.root / "cc/issue_0.json").read_text())["claim_reproduced"])

    def test_sibling_failure_does_not_prove_counterexample(self):
        from authoring.review import check_rejection_examples
        raw = self.check_issue()
        raw["issues"][0]["counterexample"]["calls"] = [{"tool": "send_email", "args_json": json.dumps(calls(GOOD, "wrong@example.com")[0]["args"])}]
        with self.assertRaisesRegex(ReviewPendingError, "disagrees"):
            check_rejection_examples(response=ReviewResponse.model_validate(raw), request=self.review_request(), out=self.root / "sibling")

    def test_counterexample_transport_error_is_not_a_defect(self):
        from authoring.review import check_rejection_examples
        with self.assertRaisesRegex(RuntimeError, "offline service error"):
            check_rejection_examples(response=ReviewResponse.model_validate(self.check_issue()), request=self.review_request(),
                                     out=self.root / "error", grader=Mock(side_effect=RuntimeError("offline service error")))
        self.assertFalse((self.root / "error/issue_0.json").exists())


    def test_request_only_content_cannot_be_memory_requirement(self):
        with self.assertRaisesRegex(ReviewPendingError, "current-request quote alone"):
            validate_review(ReviewResponse.model_validate(self.missing_issue()), self.review_request())

    def test_extra_email_content_is_not_another_final_action(self):
        with self.assertRaisesRegex(ReviewPendingError, "already prove this action"):
            validate_review(ReviewResponse.model_validate(self.missing_issue("final_action")), self.review_request())

    def test_missing_source_content_and_target_are_allowed(self):
        request = self.review_request()
        raw = self.missing_issue()
        raw["issues"][0]["missing_check"]["source_evidence"] = [{"location": "/sources/0/text", "quote": GOOD}]
        validate_review(ReviewResponse.model_validate(raw), request)
        raw = self.missing_issue("target")
        raw["issues"][0]["missing_check"]["existing_check_ids"] = ["funding"]
        validate_review(ReviewResponse.model_validate(raw), request)
        raw["issues"][0]["missing_check"]["existing_check_ids"] = ["recipient"]
        with self.assertRaisesRegex(ReviewPendingError, "remembered result"):
            validate_review(ReviewResponse.model_validate(raw), request)

    def test_distinct_same_tool_action_can_be_missing(self):
        request = self.review_request()
        request["user_request"] += " Also send a separate email to the board."
        raw = self.missing_issue("final_action", "board_email")
        raw["issues"][0]["evidence"] = [{"location": "/user_request", "quote": "Also send a separate email to the board."}]
        validate_review(ReviewResponse.model_validate(raw), request)

    def test_preflight_only_resume_never_writes_or_certifies(self):
        self.create_batch(ScriptedClient(writer()))
        source = self.root / "create/authoring/part_01/proposals/001/initial_candidate.yaml"
        before = source.read_bytes()
        client = ScriptedClient(approving_review(before=True))
        certifier = Mock(side_effect=AssertionError("certification not authorized"))
        result = self.resume_batch("create", client, certifier=certifier, stop_after_preflight=True)
        self.assertEqual(result["final_review_accepted"], 0)
        self.assertEqual(len(client.calls), 1)
        certifier.assert_not_called()
        self.assertEqual(source.read_bytes(), before)
        proposal = self.root / "resume/authoring/part_01/proposals/001"
        status = json.loads((proposal / "status.json").read_text())
        self.assertEqual(status["status"], "certification_pending")
        self.assertEqual(status["model_corrections_used"], 0)
        self.assertFalse((proposal / "initial_gate.json").exists())
        next_client = ScriptedClient(approving_review(before=False))
        resumed = self.resume_batch("resume", next_client, name="finish")
        self.assertEqual(resumed["final_review_accepted"], 1, resumed)
        self.assertEqual(len(next_client.calls), 0)

    def test_preflight_only_rejection_cannot_start_correction(self):
        self.create_batch(ScriptedClient(writer()))
        client = ScriptedClient(self.check_issue())
        certifier = Mock(side_effect=AssertionError("certification not authorized"))
        result = self.resume_batch("create", client, certifier=certifier, stop_after_preflight=True)
        self.assertEqual(result["final_review_accepted"], 0)
        self.assertEqual(len(client.calls), 1)
        certifier.assert_not_called()
        proposal = self.root / "resume/authoring/part_01/proposals/001"
        status = json.loads((proposal / "status.json").read_text())
        self.assertEqual(status["model_corrections_used"], 0)
        self.assertEqual(status["status"], "repair_pending")
        self.assertFalse((proposal / "correction_authoring_request.json").exists())

    def test_resume_copies_authenticated_examples_from_relative_source(self):
        from authoring.create_tests import _copy_authenticated_tree
        source = self.root / "source/grading_examples"
        saved = source / "correct.json"
        dump_json(saved, {"input_sha256": "same-input", "grade": {"passed": True}})
        dump_json(source / "unbound.json", {"untrusted": True})
        destination = self.root / "resumed/grading_examples"
        _copy_authenticated_tree(
            Path(os.path.relpath(source)), destination,
            {str(saved.resolve()): hashlib.sha256(saved.read_bytes()).hexdigest()},
        )
        self.assertTrue((destination / "correct.json").is_file())
        self.assertEqual(saved.read_bytes(), (destination / "correct.json").read_bytes())
        self.assertFalse((destination / "unbound.json").exists())
