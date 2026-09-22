"""Focused checks for the one-call new-test authoring path."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from authoring.final_trace_review import FinalTraceIssue, _validate_issue_citations
from authoring.models import AuthoredTestResponse, PlannedTask, RequiredResult
from authoring.pipeline import AuthoringValidationError, build_mock_state
from authoring.prompts import AUTHOR_TEST_SYSTEM
from authoring.run import (
    GRADING_AUTHORING_FIELDS,
    _author_new_test_attempt,
    _assertion_for_expected_argument,
    _completed_design_from_authoring,
    _correction_fields_for_issues,
    _validate_correction_scope,
    _strict_response_schema,
    _validate_authoring_response,
)


class UnifiedAuthoringTests(unittest.TestCase):
    def test_authoring_prompt_is_persona_independent(self) -> None:
        self.assertNotIn("message Morgan would send", AUTHOR_TEST_SYSTEM)

    def test_azure_schema_requires_every_declared_field(self) -> None:
        schema = _strict_response_schema(AuthoredTestResponse.model_json_schema())
        self.assertNotIn("answers_that_must_not_appear", schema["properties"])

        def check(value: object) -> None:
            if isinstance(value, dict):
                properties = value.get("properties")
                if isinstance(properties, dict):
                    self.assertEqual(set(value.get("required") or []), set(properties))
                    self.assertFalse(value.get("additionalProperties", True))
                self.assertNotIn("default", value)
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        check(schema)

    def _context(self) -> object:
        session = {
            "id": "session-1",
            "message": "We closed an $18M Series B led by Northstar.",
        }
        return SimpleNamespace(
            persona="morgan",
            checkpoint_path=Path("/tmp/checkpoint"),
            sessions_by_id={"session-1": session},
            facts_by_id={
                1: {
                    "id": 1,
                    "statement": "Morgan closed an $18M Series B led by Northstar.",
                    "source_session_ids": ["session-1"],
                }
            },
            tools={
                "send_email": {
                    "arguments": ["to", "cc", "body"],
                    "state_effect": {"writes_state_keys": ["sent_emails"]},
                }
            },
            readable_state={},
            app_state={},
        )

    def _task(self) -> PlannedTask:
        return PlannedTask(
            task_description="Send an investor update.",
            fact_ids=[1],
            required_results=[
                RequiredResult(
                    result="Include the Series B funding context.", fact_ids=[1]
                )
            ],
            expected_tools=["send_email"],
            action_without_memory="The update would omit the funding context.",
            why_memory_changes_result="The funding context changes the email body.",
            fact_application_explanations_on_evaluation_date=[
                {
                    "fact_id": 1,
                    "why_fact_still_applies_on_evaluation_date": "No later fact replaces it.",
                }
            ],
            why_listed_tools_can_complete_requested_work=(
                "send_email accepts the recipient and body."
            ),
        )

    def _raw_response(self) -> dict[str, object]:
        return {
            "design": {
                "outcome": "design",
                "rejection_reason": "",
                "source_message_meaning": "Morgan reported the funding round.",
                "reason_it_applies_on_test_date": "No later fact replaces it.",
                "existing_records": [],
                "new_records": [],
                "required_results": [
                    {
                        "result": "Include the Series B funding context.",
                        "fact_ids": [1],
                    }
                ],
                "expected_tool_calls": ["send_email"],
            },
            "query": "Please send a concise investor update to sam@example.com.",
            "history_dependent_results": [
                {
                    "fact_id": 1,
                    "source_session_id": "session-1",
                    "supporting_source_text": "closed an $18M Series B led by Northstar",
                    "present_request_states": "Send a concise investor update.",
                    "history_only_supplies": "The funding round amount and lead investor.",
                    "why_request_alone_does_not_determine_result": "The request does not name the funding round.",
                    "hidden_values": ["$18M", "Northstar"],
                }
            ],
            "request_requirement_quotes": ["sam@example.com", "investor update"],
            "expected_arguments": [
                {
                    "tool": "send_email",
                    "argument": "to",
                    "value_json": "\"sam@example.com\"",
                    "request_requirement_quote": "sam@example.com",
                    "history_fact_ids": [],
                }
            ],
            "expected_tool_counts": [],
            "prose_requirements": [
                {
                    "tool": "send_email",
                    "path": "body",
                    "criterion": "Does the email state that the company closed an $18M Series B led by Northstar?",
                    "request_requirement_quote": "investor update",
                    "history_fact_ids": [1],
                }
            ],
            "with_history_expected_result": "Send the update with the funding context.",
            "without_history_expected_result": "Send the update without the funding context.",
        }

    def _response(self) -> AuthoredTestResponse:
        return AuthoredTestResponse.model_validate(self._raw_response())

    def test_rejects_a_source_quote_that_does_not_match(self) -> None:
        raw = self._raw_response()
        raw["history_dependent_results"][0]["supporting_source_text"] = "raised a Series C"  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "source quote"):
            _validate_authoring_response(
                response=AuthoredTestResponse.model_validate(raw),
                context=self._context(),
                planned_task=self._task(),
            )

    def test_request_argument_mismatch_routes_only_to_grading(self) -> None:
        raw = self._raw_response()
        raw["expected_arguments"][0]["value_json"] = '"someone-else@example.com"'
        with self.assertRaisesRegex(ValueError, "must quote the required value") as caught:
            _validate_authoring_response(
                response=AuthoredTestResponse.model_validate(raw),
                context=self._context(), planned_task=self._task(),
            )
        self.assertEqual(caught.exception.correction_fields, GRADING_AUTHORING_FIELDS)

    def test_accepts_an_exact_multiline_quote_from_a_message(self) -> None:
        source = (
            "Morgan's update:\n\n"
            '"We closed an $18M Series B led by Northstar. The wire cleared."'
        )
        context = self._context()
        context.sessions_by_id["session-1"] = {
            "id": "session-1",
            "messages": [source],
        }
        raw = self._raw_response()
        raw["history_dependent_results"][0]["supporting_source_text"] = source  # type: ignore[index]

        _validate_authoring_response(
            response=AuthoredTestResponse.model_validate(raw),
            context=context,
            planned_task=self._task(),
        )

    def test_rejects_a_request_that_reveals_the_answer(self) -> None:
        raw = self._raw_response()
        raw["query"] = "Please send an investor update mentioning our $18M Series B."
        with self.assertRaisesRegex(ValueError, "reveals the history-only value"):
            _validate_authoring_response(
                response=AuthoredTestResponse.model_validate(raw),
                context=self._context(),
                planned_task=self._task(),
            )

    def _context_with_baseline_document(self) -> object:
        context = self._context()
        context.tools["read_doc"] = {
            "arguments": ["id"],
            "state_effect": {"reads_state_keys": ["docs"]},
        }
        context.app_state = {
            "docs": [
                {"id": "funding", "body": "We closed an $18M Series B led by Northstar."},
                {"id": "target", "body": "Investor update draft; add the funding context."},
            ]
        }
        context.readable_state = context.app_state
        return context

    def test_unselected_baseline_answer_does_not_leak_into_new_email(self) -> None:
        context = self._context_with_baseline_document()
        response = self._response()
        _validate_authoring_response(response=response, context=context, planned_task=self._task())
        state = build_mock_state(context, response.design.to_design_response(query=response.query))
        self.assertEqual(state, {})
        self.assertIn("Northstar", context.readable_state["docs"][0]["body"])

    def test_family_call_answer_in_optional_old_event_is_not_automatically_visible(self) -> None:
        source = "The first-Sunday family call is from 6:00 to 6:30 PM Eastern."
        context = self._context()
        context.sessions_by_id["session-1"]["message"] = source
        context.facts_by_id[1]["statement"] = source
        context.tools = {
            "create_calendar_event": {
                "arguments": ["title", "start_time", "end_time"],
                "state_effect": {"writes_state_keys": ["calendar_events"]},
            },
            "read_calendar": {
                "arguments": [],
                "state_effect": {"reads_state_keys": ["calendar_events"]},
            },
        }
        context.app_state = {"calendar_events": [{
            "id": "family-2027-11-07", "body": source,
            "start_time": "2027-11-07T18:00:00-05:00",
            "end_time": "2027-11-07T18:30:00-05:00",
        }]}
        context.readable_state = context.app_state
        raw = self._raw_response()
        raw["query"] = "Create the January 2028 first-Sunday family call on my calendar."
        raw["design"].update({  # type: ignore[union-attr]
            "source_message_meaning": source,
            "required_results": [{"result": "Use the family-call time.", "fact_ids": [1]}],
            "expected_tool_calls": ["create_calendar_event"],
        })
        raw["history_dependent_results"][0].update({  # type: ignore[index]
            "supporting_source_text": source,
            "present_request_states": raw["query"],
            "history_only_supplies": "The family call's start and end times.",
            "why_request_alone_does_not_determine_result": "The request does not give the time.",
            "hidden_values": ["6:00", "6:30"],
        })
        raw["request_requirement_quotes"] = []
        raw["expected_arguments"] = [{
            "tool": "create_calendar_event", "argument": "start_time",
            "value_json": '"2028-01-02T18:00:00-05:00"',
            "request_requirement_quote": "", "history_fact_ids": [1],
        }]
        raw["prose_requirements"] = []
        task = self._task().model_copy(update={
            "task_description": raw["query"],
            "expected_tools": ["create_calendar_event"],
            "required_results": [RequiredResult(result="Use the family-call time.", fact_ids=[1])],
        })
        response = AuthoredTestResponse.model_validate(raw)
        _validate_authoring_response(response=response, context=context, planned_task=task)
        self.assertEqual(build_mock_state(context, response.design.to_design_response(query=response.query)), {})
        raw["design"]["existing_records"] = [{  # type: ignore[index]
            "state_key": "calendar_events", "record_key": "",
            "match_json": '{"id":"family-2027-11-07"}', "remove_fields": [],
        }]
        with self.assertRaisesRegex(ValueError, "reveals the history-only value"):
            _validate_authoring_response(
                response=AuthoredTestResponse.model_validate(raw), context=context, planned_task=task,
            )

    def test_selected_baseline_answer_is_still_rejected(self) -> None:
        raw = self._raw_response()
        raw["design"]["existing_records"] = [{  # type: ignore[index]
            "state_key": "docs", "record_key": "", "match_json": '{"id":"funding"}',
            "remove_fields": [],
        }]
        with self.assertRaisesRegex(ValueError, "reveals the history-only value"):
            _validate_authoring_response(
                response=AuthoredTestResponse.model_validate(raw),
                context=self._context_with_baseline_document(), planned_task=self._task(),
            )

    def test_new_record_revealing_the_answer_is_still_rejected(self) -> None:
        raw = self._raw_response()
        raw["design"]["new_records"] = [{  # type: ignore[index]
            "state_key": "docs", "record_key": "",
            "record_json": '{"id":"new","body":"Northstar led our $18M Series B."}',
        }]
        with self.assertRaisesRegex(ValueError, "reveals the history-only value"):
            _validate_authoring_response(
                response=AuthoredTestResponse.model_validate(raw),
                context=self._context_with_baseline_document(), planned_task=self._task(),
            )

    def test_selected_neutral_target_is_preserved_without_other_baseline_records(self) -> None:
        raw = self._raw_response()
        raw["design"]["existing_records"] = [{  # type: ignore[index]
            "state_key": "docs", "record_key": "", "match_json": '{"id":"target"}',
            "remove_fields": [],
        }]
        response = AuthoredTestResponse.model_validate(raw)
        context = self._context_with_baseline_document()
        _validate_authoring_response(response=response, context=context, planned_task=self._task())
        state = build_mock_state(context, response.design.to_design_response(query=response.query))
        self.assertEqual(state, {"docs": [context.app_state["docs"][1]]})
        self.assertIsNot(state["docs"][0], context.app_state["docs"][1])

    def test_nonexistent_selected_target_is_not_silently_omitted(self) -> None:
        raw = self._raw_response()
        raw["design"]["existing_records"] = [{  # type: ignore[index]
            "state_key": "docs", "record_key": "", "match_json": '{"id":"missing"}',
            "remove_fields": [],
        }]
        with self.assertRaisesRegex(ValueError, "matched 0 records"):
            _validate_authoring_response(
                response=AuthoredTestResponse.model_validate(raw),
                context=self._context_with_baseline_document(), planned_task=self._task(),
            )

    def test_code_uses_result_checks_and_never_infers_count_or_no_cc(self) -> None:
        response = self._response()
        _validate_authoring_response(
            response=response, context=self._context(), planned_task=self._task()
        )
        design = _completed_design_from_authoring(
            response=response, planned_task=self._task(), context=self._context()
        )
        assertions = [check.assertion for check in design.grading_checks]
        self.assertFalse(any(item["type"] == "tool_call_count" for item in assertions))
        self.assertIn(
            {
                "type": "recipient_matches",
                "tool": "send_email",
                "path": "args.to",
                "value": "sam@example.com",
            },
            assertions,
        )
        self.assertTrue(any(item.get("path") == "args.body" for item in assertions))
        self.assertFalse(any(item.get("path") == "args.cc" for item in assertions))
        self.assertFalse(any(item["type"].startswith("tool_not") for item in assertions))
        self.assertEqual(design.without_memory_failed_check, 1)
        self.assertEqual(design.answers_that_must_not_appear, ["$18M", "Northstar"])

    def test_explicit_recipient_comparison(self) -> None:
        self.assertEqual(
            _assertion_for_expected_argument(
                tool="send_slack_dm",
                argument="user",
                value="Priya Nair",
                history_fact_ids=[],
                comparison="recipient_identity",
            ),
            {
                "type": "recipient_matches",
                "tool": "send_slack_dm",
                "path": "args.user",
                "value": "Priya Nair",
            },
        )

    def test_test_3_accepts_natural_language_date_for_iso_date_argument(self) -> None:
        raw = self._raw_response()
        raw["query"] = (
            "Please send a concise investor update to sam@example.com for "
            "September 15, 2026."
        )
        raw["request_requirement_quotes"] = [
            "sam@example.com",
            "investor update",
            "September 15, 2026",
        ]
        raw["expected_arguments"].append(  # type: ignore[union-attr]
            {
                "tool": "send_email",
                "argument": "date",
                "value_json": '"2026-09-15"',
                "request_requirement_quote": "September 15, 2026",
                "history_fact_ids": [],
            }
        )
        context = self._context()
        context.tools["send_email"]["arguments"].append("date")
        response = AuthoredTestResponse.model_validate(raw)

        _validate_authoring_response(
            response=response, context=context, planned_task=self._task()
        )
        design = _completed_design_from_authoring(
            response=response, planned_task=self._task(), context=context
        )
        self.assertIn(
            {
                "type": "date_on",
                "tool": "send_email",
                "path": "args.date",
                "value": "2026-09-15",
            },
            [check.assertion for check in design.grading_checks],
        )

    def test_test_3_accepts_each_date_from_two_date_request_quote(self) -> None:
        raw = self._raw_response()
        raw["query"] = (
            "Please review my calendar for Tuesday afternoon, September 15, 2026, "
            "and Friday, September 18, 2026."
        )
        raw["request_requirement_quotes"] = [
            "review my calendar",
            "Please review my calendar for Tuesday afternoon, September 15, 2026, and Friday, September 18, 2026",
        ]
        raw["expected_arguments"] = [
            {
                "tool": "send_email",
                "argument": "date",
                "value_json": '"2026-09-15"',
                "request_requirement_quote": (
                    "Please review my calendar for Tuesday afternoon, September 15, "
                    "2026, and Friday, September 18, 2026"
                ),
                "history_fact_ids": [],
            },
            {
                "tool": "send_email",
                "argument": "date",
                "value_json": '"2026-09-18"',
                "request_requirement_quote": (
                    "Please review my calendar for Tuesday afternoon, September 15, "
                    "2026, and Friday, September 18, 2026"
                ),
                "history_fact_ids": [],
            },
        ]
        raw["prose_requirements"][0]["request_requirement_quote"] = (  # type: ignore[index]
            "review my calendar"
        )
        context = self._context()
        context.tools["send_email"]["arguments"].append("date")
        response = AuthoredTestResponse.model_validate(raw)

        _validate_authoring_response(
            response=response, context=context, planned_task=self._task()
        )

    def test_non_date_mechanical_fields_still_require_exact_values(self) -> None:
        self.assertEqual(
            _assertion_for_expected_argument(
                tool="create_calendar_event",
                argument="start",
                value="2026-09-15T09:00:00-07:00",
                history_fact_ids=[],
            )["type"],
            "field_equals",
        )
        self.assertEqual(
            _assertion_for_expected_argument(
                tool="create_calendar_event",
                argument="end",
                value="2026-09-15T10:00:00-07:00",
                history_fact_ids=[],
            )["type"],
            "field_equals",
        )

    def test_code_adds_an_explicitly_requested_tool_count(self) -> None:
        raw = self._raw_response()
        raw["query"] = "Please send one investor update to sam@example.com."
        raw["expected_tool_counts"] = [
            {
                "tool": "send_email",
                "count": 1,
                "request_requirement_quote": "send one investor update",
            }
        ]
        raw["request_requirement_quotes"] = [
            "sam@example.com",
            "investor update",
            "send one investor update",
        ]
        response = AuthoredTestResponse.model_validate(raw)
        _validate_authoring_response(
            response=response, context=self._context(), planned_task=self._task()
        )
        design = _completed_design_from_authoring(
            response=response, planned_task=self._task(), context=self._context()
        )
        self.assertIn(
            {"type": "tool_call_count", "tool": "send_email", "count": 1},
            [check.assertion for check in design.grading_checks],
        )

    def test_code_adds_an_explicit_empty_cc_check(self) -> None:
        raw = self._raw_response()
        raw["query"] = (
            "Please send an investor update only to sam@example.com."
        )
        raw["request_requirement_quotes"] = [
            "only to sam@example.com",
            "investor update",
        ]
        raw["expected_arguments"][0]["request_requirement_quote"] = (  # type: ignore[index]
            "only to sam@example.com"
        )
        raw["expected_empty_arguments"] = [
            {
                "tool": "send_email",
                "argument": "cc",
                "request_requirement_quote": "only to sam@example.com",
                "history_fact_ids": [],
            }
        ]
        response = AuthoredTestResponse.model_validate(raw)
        _validate_authoring_response(
            response=response, context=self._context(), planned_task=self._task()
        )
        design = _completed_design_from_authoring(
            response=response, planned_task=self._task(), context=self._context()
        )
        self.assertIn(
            {
                "type": "field_absent_or_empty",
                "tool": "send_email",
                "path": "args.cc",
            },
            [check.assertion for check in design.grading_checks],
        )

    def test_rejects_a_declared_request_requirement_without_a_check(self) -> None:
        raw = self._raw_response()
        raw["request_requirement_quotes"].append("concise")  # type: ignore[union-attr]
        with self.assertRaisesRegex(ValueError, "request requirements and request-backed"):
            _validate_authoring_response(
                response=AuthoredTestResponse.model_validate(raw),
                context=self._context(),
                planned_task=self._task(),
            )

    def test_code_adds_tool_called_only_when_no_other_check_proves_the_call(self) -> None:
        raw = self._raw_response()
        raw["expected_arguments"] = []
        raw["prose_requirements"][0]["tool"] = "send_email"  # type: ignore[index]
        response = AuthoredTestResponse.model_validate(raw)
        design = _completed_design_from_authoring(
            response=response, planned_task=self._task(), context=self._context()
        )
        assertions = [check.assertion for check in design.grading_checks]
        self.assertFalse(any(item["type"] == "tool_called" for item in assertions))

    def test_code_grades_a_final_action_but_not_a_supporting_read(self) -> None:
        raw = self._raw_response()
        raw["query"] = "Search my notes and create a scratchpad."
        raw["design"]["expected_tool_calls"] = ["vector_search", "create_doc"]  # type: ignore[index]
        raw["request_requirement_quotes"] = []
        raw["expected_arguments"] = []
        raw["prose_requirements"] = [
            {
                "tool": "vector_search",
                "path": "namespace",
                "criterion": "Does the namespace identify the remembered notes index?",
                "request_requirement_quote": "",
                "history_fact_ids": [1],
            }
        ]
        context = self._context()
        context.tools = {
            "vector_search": {
                "arguments": ["namespace"],
                "state_effect": {"reads_state_keys": ["vectors"]},
            },
            "create_doc": {
                "arguments": ["title", "body"],
                "state_effect": {"writes_state_keys": ["docs"]},
            },
        }
        task = self._task().model_copy(
            update={"expected_tools": ["vector_search", "create_doc"]}
        )
        response = AuthoredTestResponse.model_validate(raw)
        _validate_authoring_response(response=response, context=context, planned_task=task)
        design = _completed_design_from_authoring(
            response=response, planned_task=task, context=context
        )
        assertions = [check.assertion for check in design.grading_checks]
        self.assertIn({"type": "tool_called", "tool": "create_doc"}, assertions)
        self.assertNotIn({"type": "tool_called", "tool": "vector_search"}, assertions)

    def test_rejects_selected_fact_that_no_grading_requirement_uses(self) -> None:
        raw = self._raw_response()
        raw["prose_requirements"][0]["history_fact_ids"] = []  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "grade every and only selected fact"):
            _validate_authoring_response(
                response=AuthoredTestResponse.model_validate(raw),
                context=self._context(),
                planned_task=self._task(),
            )

    def test_reviewer_cannot_add_an_uncited_requirement(self) -> None:
        issue = FinalTraceIssue(
            owning_step="grading_checks",
            specific_problem="The test does not forbid a CC.",
            required_correction="Require no CC recipients.",
        )
        with self.assertRaisesRegex(ValueError, "must cite exact request wording or exact source text"):
            _validate_issue_citations(
                SimpleNamespace(issues=[issue]),
                {
                    "user_request": "Send the investor update.",
                    "source_evidence": [
                        {"source_sessions": [self._context().sessions_by_id["session-1"]]}
                    ],
                },
            )

    def test_reviewer_accepts_decorative_quotes_around_an_exact_excerpt(self) -> None:
        issue = FinalTraceIssue(
            owning_step="user_request",
            specific_problem="The request reveals the answer.",
            required_correction="Remove the answer.",
            request_requirement_quote='"Send the investor update."',
        )
        _validate_issue_citations(
            SimpleNamespace(issues=[issue]),
            {"user_request": "Send the investor update.", "source_evidence": []},
        )

    def test_reviewer_accepts_one_exact_request_excerpt(self) -> None:
        issue = FinalTraceIssue(
            owning_step="grading_checks",
            specific_problem="Two requested parts are not graded.",
            required_correction="Grade both parts.",
            request_requirement_quote=(
                "Greg Shipman, Acme’s product lead, asked for help; please send "
                "Sarah an internal email."
            ),
        )
        _validate_issue_citations(
            SimpleNamespace(issues=[issue]),
            {
                "user_request": (
                    "Greg Shipman, Acme’s product lead, asked for help; please send "
                    "Sarah an internal email."
                ),
                "source_evidence": [],
            },
        )

    def test_new_creation_makes_one_authoring_call(self) -> None:
        response = self._raw_response()
        config = SimpleNamespace(evaluation_date="2026-09-14", persona="morgan")
        with tempfile.TemporaryDirectory() as temp:
            with patch("authoring.run._model_call", return_value=response) as call:
                candidate, result = _author_new_test_attempt(
                    directory=Path(temp),
                    attempt_name="initial",
                    system="author once",
                    payload={"accepted_idea": "send an investor update"},
                    planned_task=self._task(),
                    context=self._context(),
                    config=config,
                    test_id=1,
                    client=SimpleNamespace(),
                    certifier=lambda *_args, **_kwargs: {"valid": True},
                )
        self.assertIsNotNone(candidate)
        self.assertEqual(result["stage"], "accepted")
        self.assertEqual(call.call_count, 1)


class RequirementPreservationTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = UnifiedAuthoringTests()
        self.context = fixture._context()
        self.task = fixture._task()
        self.raw = fixture._raw_response()
        self.raw["schema_version"] = 2
        self.raw["expected_arguments"][0]["comparison"] = "email_delivery"
        self.raw["requested_result_checks"] = [{
            "requested_result": self.task.required_results[0].result,
            "assertion_indexes": [1],
        }]

    def validate(self) -> AuthoredTestResponse:
        response = AuthoredTestResponse.model_validate(self.raw)
        _validate_authoring_response(response=response, context=self.context, planned_task=self.task)
        return response

    def test_required_content_in_the_same_email_tool_remains_valid(self) -> None:
        response = self.validate()
        design = _completed_design_from_authoring(response=response, context=self.context, planned_task=self.task)
        self.assertEqual(design.grading_checks[1].assertion["type"], "field_llm_judge")
        self.assertEqual(design.grading_checks[1].fact_ids, [1])

    def test_writer_cannot_redefine_the_approved_result(self) -> None:
        self.raw["design"]["required_results"][0]["result"] = "Also include the historical board meeting date."
        with self.assertRaisesRegex(ValueError, "approved required results unchanged"):
            self.validate()

    def test_unmapped_extra_grading_check_is_rejected(self) -> None:
        self.raw["prose_requirements"].append({
            "tool": "send_email", "path": "body", "criterion": "Does the email give the board meeting date?",
            "history_fact_ids": [1], "request_requirement_quote": "",
        })
        with self.assertRaisesRegex(ValueError, "every history-dependent check"):
            self.validate()

    def test_duplicate_grading_check_is_rejected(self) -> None:
        self.raw["prose_requirements"].append(copy.deepcopy(self.raw["prose_requirements"][0]))
        self.raw["requested_result_checks"][0]["assertion_indexes"].append(2)
        with self.assertRaisesRegex(ValueError, "same grading assertion"):
            self.validate()

    def test_unknown_check_index_is_rejected(self) -> None:
        self.raw["requested_result_checks"][0]["assertion_indexes"] = [99]
        with self.assertRaisesRegex(ValueError, "unknown check"):
            self.validate()

    def test_topic_match_is_left_for_meaning_review_not_declared_a_leak(self) -> None:
        self.raw["history_dependent_results"][0]["hidden_values"].append("investor update")
        response = self.validate()
        design = _completed_design_from_authoring(response=response, context=self.context, planned_task=self.task)
        self.assertEqual(design.answers_that_must_not_appear, [])
        self.assertEqual(self.context.facts_by_id[1]["source_session_ids"], ["session-1"])

    def test_grading_correction_cannot_change_request_or_state(self) -> None:
        fields = _correction_fields_for_issues([{"owning_step": "grading_checks"}])
        for mutation in ("query", "state", "source"):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(self.raw)
                if mutation == "query":
                    changed["query"] += " Include a new requirement."
                elif mutation == "state":
                    changed["design"]["new_records"] = [{
                        "state_key": "docs", "record_key": "id", "record_json": '{"id":"new"}',
                    }]
                else:
                    changed["history_dependent_results"][0]["supporting_source_text"] = "a different source"
                with self.assertRaisesRegex(ValueError, "unrelated field"):
                    _validate_correction_scope(
                        previous=self.raw, current=AuthoredTestResponse.model_validate(changed), allowed_fields=fields,
                    )

    def test_grading_correction_can_change_only_grading(self) -> None:
        changed = copy.deepcopy(self.raw)
        changed["prose_requirements"][0]["criterion"] += " Missing or negated funding must fail."
        _validate_correction_scope(
            previous=self.raw, current=AuthoredTestResponse.model_validate(changed),
            allowed_fields=_correction_fields_for_issues([{"owning_step": "grading_checks"}]),
        )


class NewRecordStateTests(unittest.TestCase):
    def test_record_keys_follow_the_collection_shape(self):
        cases = (
            ("inbox", [], "email-1", {"id": "email-1", "subject": "Neutral incoming email"},
             [{"id": "email-1", "subject": "Neutral incoming email"}], None),
            ("crm", [], "crm-other", {"id": "crm-1", "name": "Neutral contact"}, None, "exactly matches"),
            ("docs", {}, "doc-1", {"id": "different-id", "title": "Neutral doc"},
             {"doc-1": {"id": "different-id", "title": "Neutral doc"}}, None),
            ("docs", {}, "", {"id": "doc-1", "title": "Neutral doc"}, None, "requires record_key"),
        )
        for collection, initial, key, record, expected, error in cases:
            with self.subTest(collection=collection, key=key):
                context = SimpleNamespace(
                    tools={"read": {"state_effect": {"reads_state_keys": [collection]}, "arguments": []}},
                    readable_state={collection: initial}, app_state={collection: initial},
                )
                design = SimpleNamespace(existing_records=[], new_records=[
                    SimpleNamespace(state_key=collection, record_key=key, record=record),
                ])
                if error:
                    with self.assertRaisesRegex(AuthoringValidationError, error):
                        build_mock_state(context, design)
                else:
                    self.assertEqual(build_mock_state(context, design)[collection], expected)


if __name__ == "__main__":
    unittest.main()
