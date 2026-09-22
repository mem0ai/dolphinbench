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


GOOD = "The company raised an $18M Series B led by Northstar."
BAD = "The company is growing."


class ScriptedClient:
    model = "offline"

    def __init__(self, *responses: dict):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, system, payload, *, response_schema, response_schema_name):
        if not self.responses:
            raise AssertionError("unexpected extra model stage")
        response = copy.deepcopy(self.responses.pop(0))
        if response_schema_name == "dolphinbench_test_review_v4":
            def strip_evidence_quotes(value):
                if isinstance(value, dict):
                    if "location" in value:
                        value.pop("quote", None)
                    for child in value.values():
                        strip_evidence_quotes(child)
                elif isinstance(value, list):
                    for child in value:
                        strip_evidence_quotes(child)
            strip_evidence_quotes(response)
        Draft202012Validator(response_schema).validate(response)
        self.calls.append({"system": system, "payload": payload, "schema": response_schema_name})
        return response


def idea() -> ApprovedIdea:
    return ApprovedIdea(
        authoring_version=4, idea_id=1,
        situation="An investor wants a company update.",
        work="Send the investor an email with current funding context.",
        fact_ids=[1], expected_tools=["send_email"],
        history_needed="History supplies the funding round, amount, and lead investor.",
        likely_without_history="The assistant asks for the funding information.",
    )


def writer() -> dict:
    return {
        "status": "written", "problem": "", "quality_audit": {
            "reader_and_goal": "Morgan needs to send an investor current funding context.",
            "why_request_is_natural": "It is a direct delegation of a normal investor update.",
            "why_scope_is_coherent": "The recipient and funding context serve one email.",
            "non_memory_inputs": [{
                "input": "The investor email address.",
                "source_location": "/user_request",
                "why_needed": "The email tool needs a delivery target.",
            }],
            "memory_results": [{
                "result": "The email communicates the current funding round, amount, and lead investor.",
                "fact_ids": [1],
                "check_ids": ["funding"],
                "why_necessary": "The approved investor update explicitly needs current funding context.",
            }],
            "why_request_and_state_do_not_reveal_memory": (
                "The request asks for funding context without stating its values, and no records are selected."
            ),
            "final_actions": [{
                "result": "Send the investor update.",
                "tool": "send_email",
                "action_id": "email",
                "check_ids": ["recipient", "funding"],
            }],
            "checks": [{
                "check_id": "recipient",
                "roles": ["target", "final_action"],
                "why_necessary": "It binds the remembered content to the requested investor.",
                "why_not_duplicate": "No other check verifies the delivery target.",
            }, {
                "check_id": "funding",
                "roles": ["remembered_result", "final_action"],
                "why_necessary": "It verifies the remembered funding context in the sent email.",
                "why_not_duplicate": "No other check verifies the funding content.",
            }],
        }, "test": {
            "request": "Send an investor update to pat@example.com with our current funding context.",
            "existing_records": [], "new_records": [], "expected_tools": ["send_email"],
            "evidence": [{"evidence_id": "funding", "fact_ids": [1], "source_session_id": "s1",
                          "message_id": "s1:message:0", "quote": GOOD}],
            "checks": [
                {"assertion": {"check_id": "recipient", "action_id": "email", "type": "field_equals",
                               "tool": "send_email", "path": "args.to", "value_json": '"pat@example.com"'},
                 "evidence_ids": [], "why_required": "The funding information must reach the intended investor."},
                {"assertion": {"check_id": "funding", "action_id": "email", "type": "field_llm_judge",
                               "tool": "send_email", "path": "args.body",
                               "criterion": "Communicates an $18M Series B led by Northstar."},
                 "evidence_ids": ["funding"], "why_required": "The investor needs the current funding context."},
            ],
        },
    }


def calls(body=GOOD, to="pat@example.com") -> list[dict]:
    return [{"tool": "send_email", "args": {"to": to, "subject": "Company update", "body": body}}]


def approving_review(*, before: bool) -> dict:
    return {
        "decision": "approve", "issues": [],
        "examples": {
            "correct_calls": [{"tool": row["tool"], "args_json": json.dumps(row["args"])} for row in calls()],
            "incorrect_calls": [{"tool": row["tool"], "args_json": json.dumps(row["args"])} for row in calls(BAD)],
            "expected_failed_check_ids": ["funding"],
        } if before else None,
    }


class CreationFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = AuthoringConfig(
            version=1, persona="morgan", checkpoint=str(self.root / "checkpoint"),
            evaluation_date="2026-09-14", planner_model="offline", designer_model="offline",
            query_model="offline", persona_state=str(self.root / "persona_state.json"),
        )
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(yaml.safe_dump(self.config.model_dump(mode="json")))
        session = {"id": "s1", "narrative_date": "2025-05-01T15:30:00-07:00", "message": GOOD}
        fact = {"id": 1, "statement": GOOD, "source_session_ids": ["s1"], "subjects": ["company:scaffold"],
                "applies_when": "current funding context", "supersedes": []}
        tools = {
            "send_email": {"arguments": ["to", "subject", "body", "cc"], "required_arguments": ["to", "subject", "body"],
                           "state_effect": {"reads_state_keys": [], "writes_state_keys": ["emails"]}},
            "get_document": {"arguments": ["id"], "required_arguments": ["id"],
                             "state_effect": {"reads_state_keys": ["documents", "attachments"], "writes_state_keys": []}},
            "create_document": {"arguments": ["title", "body"], "required_arguments": ["title", "body"],
                                "state_effect": {"reads_state_keys": [], "writes_state_keys": ["documents"]}},
            "list_emails": {"arguments": [], "required_arguments": [],
                            "state_effect": {"reads_state_keys": ["emails"], "writes_state_keys": []}},
        }
        state = {"emails": [{"id": "old", "body": GOOD}],
                 "documents": {"d1": {"title": "Brief", "attachment_id": "a1"}},
                 "attachments": [{"id": "a1", "body": "The full attachment."}]}
        self.context = SimpleNamespace(
            persona="morgan", checkpoint_identity="checkpoint", checkpoint_path=self.root / "checkpoint",
            history=[session], facts=[fact], facts_by_id={1: fact}, sessions_by_id={"s1": session},
            active_fact_ids={1}, app_state=state, readable_state=copy.deepcopy(state), tools=tools,
        )
        dump_json(self.root / "persona_state.json", {
            "version": 1, "persona": "morgan", "checkpoint_identity": "checkpoint",
            "evaluation_date": self.config.evaluation_date, "batches": [], "approved_plans": [], "rejected_test_files": [],
        })
        self.idea = idea()
        self.plan = self.root / "plan.json"
        dump_json(self.plan, CandidateTaskBatch(
            checkpoint_identity="checkpoint", persona="morgan", evaluation_date=self.config.evaluation_date,
            tasks=[self.idea],
        ).model_dump(mode="json"))
        self.payload = writer_payload(config=self.config, context=self.context, idea=self.idea)
        self.addCleanup(patch.stopall)
        patch("graders.llm_judge.judge_field_value", side_effect=self.judge).start()

    @staticmethod
    def judge(value, criterion, **kwargs):
        # The test double checks wiring, not real-model semantic accuracy.
        return {"ok": all(word in str(value) for word in ("$18M", "Series B", "Northstar")), "reason": "offline fixture"}

    def candidate(self, raw=None):
        response = parse_writer_response(raw or writer())
        return assemble_written_test(response=response, idea=self.idea, context=self.context,
                                     evaluation_date=self.config.evaluation_date, test_id=1, payload=self.payload)

    def review_request(self, raw=None):
        from authoring.final_trace_review import build_test_review_request
        from authoring.pipeline import write_candidate
        response = parse_writer_response(raw or writer())
        path = self.root / "candidate.yaml"
        write_candidate(path, self.candidate(raw))
        request = build_test_review_request(candidate_path=path, planned_task=self.idea, context=self.context)
        request["authoring_evidence"] = authoring_evidence(response)
        return request

    def certify(self, persona, candidate_path, *, checkpoint, confirm_paid_calls, oracle_input):
        self.assertTrue(confirm_paid_calls)
        candidate = yaml.safe_load(candidate_path.read_text())
        shots = []
        inputs = {}
        for index, with_memory in enumerate((True, True, False, False)):
            actual = calls(GOOD if with_memory else BAD)
            grade = grade_tool_trace(actual, candidate["grade"]["config"], candidate["test"])
            initial = {
                "messages": [{"role": "system", "content": build_system_prompt(
                    {**candidate, "_oracle_input": oracle_input}, persona, with_memory=with_memory)},
                    {"role": "user", "content": candidate["test"]}],
                "tools": [], "starting_state_sha256": hashlib.sha256(json.dumps(
                    candidate["mock_state"], sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            }
            key = str(with_memory)
            inputs[key] = initial
            shots.append({"attempt_id": str(index), "with_memory": with_memory,
                          "passed": grade["passed"], "grade": grade, "tool_calls": actual,
                          "tool_results": [{"call_index": 0, **actual[0], "result": {"sent": True}}],
                          "agent_input_id": key, "error": None, "oracle_errors": [], "response_text": "Sent."})
        return {"valid": True, "verdict": "valid", "g1_pass_count": 2, "g2_pass_count": 0,
                "shots": shots, "agent_inputs": inputs}

    def create_batch(
        self, client, *, certifier=None, name="create", force_final_review=False
    ):
        from authoring.create_tests import create_tests
        from authoring.run import run_authoring

        def local_worker(**kwargs):
            run_dir = kwargs["out"] / "authoring" / "part_01"
            run_authoring(config_path=kwargs["config_path"], plan_path=kwargs["plan_path"], out=run_dir,
                          limit=None, start_id=kwargs["start_id"], test_ids=kwargs["test_ids"],
                          design_client=client, query_client=client, certifier=certifier or self.certify,
                          confirm_paid_calls=True)
            return [run_dir]

        review_policy = (
            patch(
                "authoring.create_tests._clean_certification_evidence",
                return_value=(False, {"outcome": "test_forced_exception", "reasons": []}),
            )
            if force_final_review
            else nullcontext()
        )
        with (patch("authoring.create_tests.load_checkpoint_context", return_value=self.context),
              patch("authoring.run.load_checkpoint_context", return_value=self.context),
              patch("authoring.create_tests._run_authoring_parts", side_effect=local_worker),
              patch("authoring.create_tests.AzureJsonClient", return_value=client),
              patch("authoring.create_tests.certify_candidate", side_effect=certifier or self.certify),
              review_policy):
            return create_tests(config_path=self.config_path, plan_path=self.plan, out=self.root / name,
                                start_id=1, concurrency=1, confirm_paid_calls=True)

    def resume_batch(
        self, source, client, *, certifier=None, name="resume", corrections=None,
        stop_after_preflight=False, force_final_review=False,
    ):
        from authoring.create_tests import resume_new_test_run
        review_policy = (
            patch(
                "authoring.create_tests._clean_certification_evidence",
                return_value=(False, {"outcome": "test_forced_exception", "reasons": []}),
            )
            if force_final_review
            else nullcontext()
        )
        with (patch("authoring.create_tests.load_checkpoint_context", return_value=self.context),
              patch("authoring.create_tests.AzureJsonClient", return_value=client),
              review_policy):
            return resume_new_test_run(
                config_path=self.config_path, plan_path=self.plan, source_run=self.root / source,
                test_ids=[1], out=self.root / name, concurrency=1, confirm_paid_calls=True,
                certifier=certifier or self.certify,
                approved_corrections_path=corrections,
                stop_after_preflight=stop_after_preflight,
            )

    def pending_draft_correction(self):
        raw = writer()
        raw["test"]["checks"][1]["assertion"]["path"] = "body"
        result = self.create_batch(ScriptedClient(raw))
        self.assertEqual(result["pending_count"], 1)
        source = self.root / "create/authoring/part_01/proposals/001/initial_authoring_response.json"
        path = self.root / "approved_corrections.json"
        dump_json(path, {"version": 4, "corrections": [{
            "test_id": 1, "source_response_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "allowed_correction_fields": ["test.request", "test.checks"],
            "protected_check_ids": ["recipient"],
            "specific_problem": "The content check uses a bare argument path.",
            "required_correction": "Use args.body. Preserve the existing request and other checks.",
        }]})
        return path

    def exact_criterion_correction(self):
        permission = self.pending_draft_correction()
        result = self.resume_batch("create", ScriptedClient(
            writer(), approving_review(before=True), self.check_issue(),
        ), corrections=permission, force_final_review=True)
        self.assertEqual(result["final_review_accepted"], 0)
        record = result["final_review_results"][0]
        self.assertEqual(record["model_corrections_used"], 1)
        response = Path(record["authoring_response"])
        gate = Path(record["gate"])
        original = parse_writer_response(json.loads(response.read_text())).test.checks[1].assertion["criterion"]
        path = self.root / "exact_criterion.json"
        dump_json(path, {"version": 4, "corrections": [{
            "test_id": 1, "source_response_sha256": hashlib.sha256(response.read_bytes()).hexdigest(),
            "source_gate_sha256": hashlib.sha256(gate.read_bytes()).hexdigest(),
            "criterion_replacements": [{"check_id": "funding", "previous_criterion": original,
                                        "criterion": original + " Equivalent wording is allowed."}],
        }]})
        return path

    @staticmethod
    def check_issue():
        return {"decision": "reject", "examples": None, "issues": [{
            "part": "checks", "location": "/grading_checks/1", "problem": "The criterion needs an explicit allowance for equivalent wording.",
            "required_change": "Allow equivalent wording without changing the necessary funding meaning.",
            "evidence": [{"location": "/grading_checks/1/criterion", "quote": "Communicates an $18M Series B led by Northstar."}],
            "counterexample": {"check_id": "funding", "predicted_pass": False, "required_pass": True,
                               "calls": [{"tool": "send_email", "args_json": json.dumps(calls("Northstar led our Series B of eighteen million dollars.")[0]["args"])}]},
            "missing_check": None,
        }]}

    def mixed_check_execution_issue(self):
        review = self.check_issue()
        review["issues"].append({
            "part": "execution", "location": "/executions/0/tool_calls/0",
            "problem": "The assistant made an execution mistake requiring one retry.",
            "required_change": "Retry without changing the request or state.",
            "evidence": [{"location": "/user_request", "quote": writer()["test"]["request"]}],
            "counterexample": None, "missing_check": None,
        })
        return review

    def missing_issue(self, role="remembered_result", action="email"):
        return {"decision": "reject", "examples": None, "issues": [{
            "part": "checks", "location": "/grading_checks", "problem": "A required result is missing.",
            "required_change": "Add only the missing check.",
            "evidence": [{"location": "/user_request", "quote": writer()["test"]["request"]}],
            "counterexample": None,
            "missing_check": {"role": role, "tool": "send_email", "action_id": action,
                              "source_evidence": [], "existing_check_ids": []},
        }]}
