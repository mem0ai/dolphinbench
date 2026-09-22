"""New authoring format through compilation, example grading, and correction."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from authoring.create_tests import _corrected_grading_candidate, _grading_correction_response_schema
from authoring.models import AuthoredTestResponse, BlindQueryResponse
from authoring.pipeline import assemble_candidate, write_candidate
from authoring.preflight import GradingProbe, check_grading_examples
from authoring.run import _authoring_response_schema, _completed_design_from_authoring, _validate_authoring_response
from tests.unit.authoring import test_grading_rules
from tests.unit.authoring import test_unified_authoring
from graders.mechanical import grade_tool_trace


class ExplicitAuthoringTests(unittest.TestCase):
    def test_multiple_passages_for_one_fact_are_all_validated(self):
        row = copy.deepcopy(self.raw["history_dependent_results"][0])
        row.update(source_session_id="session-2", supporting_source_text="Northstar remains the lead investor.")
        self.context.sessions_by_id["session-2"] = {"message": row["supporting_source_text"]}
        self.context.facts_by_id[1]["source_session_ids"].append("session-2")
        self.raw["history_dependent_results"].append(row)
        self.validated_response()
        row["supporting_source_text"] = "A fabricated quote."
        with self.assertRaisesRegex(ValueError, "source quote"):
            self.validated_response()

    def test_missing_or_unselected_evidence_still_fails(self):
        original = copy.deepcopy(self.raw["history_dependent_results"])
        for rows in ([], [dict(original[0], fact_id=2)]):
            with self.subTest(rows=rows):
                self.raw["history_dependent_results"] = rows
                with self.assertRaisesRegex(ValueError, "every and only selected fact|needs history-dependent results"):
                    self.validated_response()

    def test_unaffiliated_or_missing_source_still_fails(self):
        row = self.raw["history_dependent_results"][0]
        row["source_session_id"] = "not-a-source"
        with self.assertRaisesRegex(ValueError, "does not support"):
            self.validated_response()
        self.context.facts_by_id[1]["source_session_ids"].append("not-a-source")
        with self.assertRaisesRegex(ValueError, "missing source session"):
            self.validated_response()

    def test_independent_errors_are_reported_before_compilation(self):
        self.raw["history_dependent_results"][0]["supporting_source_text"] = "Fabricated source."
        self.raw["expected_arguments"][0]["argument"] = "unknown_field"
        self.raw["prose_requirements"][0]["request_requirement_quote"] = "Absent from request"
        with patch("authoring.run._completed_design_from_authoring") as compile_checks:
            with self.assertRaises(ValueError) as caught:
                self.validated_response()
        for text in ("source quote", "unknown field", "request quote"):
            self.assertIn(text, str(caught.exception))
        self.assertIn("history_dependent_results", caught.exception.correction_fields)
        self.assertIn("expected_arguments", caught.exception.correction_fields)
        compile_checks.assert_not_called()

    def test_natural_request_arguments_reach_review_without_literal_conversion(self):
        self.raw["query"] += " Schedule Q1 2028 on January 18 from 2:00 to 3:30 PM Eastern with me."
        self.context.tools["create_calendar_event"] = {
            "arguments": ["start", "end", "attendees"],
            "state_effect": {"writes_state_keys": ["calendar"]},
        }
        self.raw["design"]["expected_tool_calls"].append("create_calendar_event")
        for argument, value, comparison in (
            ("start", "2028-01-18T14:00:00-05:00", "instant"),
            ("end", "2028-01-18T15:30:00-05:00", "instant"),
            ("attendees", ["Morgan"], "list_includes"),
        ):
            self.raw["expected_arguments"].append({
                "tool": "create_calendar_event", "argument": argument,
                "value_json": json.dumps(value), "comparison": comparison,
                "check_id": argument, "action_id": "event",
                "request_requirement_quote": "January 18 from 2:00 to 3:30 PM Eastern with me",
                "history_fact_ids": [],
            })
        response = self.validated_response()
        design = _completed_design_from_authoring(response=response, context=self.context, planned_task=self.task)
        check = next(c.assertion for c in design.grading_checks if c.assertion["check_id"] == "end")
        with patch("graders.llm_judge.judge_field_value", side_effect=AssertionError("no fallback")):
            for instant, passes in (("2028-01-18T20:30:00Z", True), ("2028-01-18T21:30:00Z", False)):
                result = grade_tool_trace([{"tool": "create_calendar_event", "args": {"end": instant}}],
                                          {"check_version": 2, "assertions": [check]})
                self.assertEqual(result["passed"], passes)

    def test_redundant_request_declaration_does_not_require_a_read_check(self):
        self.raw["query"] += " Check my calendar first."
        self.raw["request_requirement_quotes"].append("Check my calendar first.")
        self.validated_response()
        self.raw["request_requirement_quotes"] = []
        self.validated_response()
        self.raw["expected_arguments"][0]["request_requirement_quote"] = "A fabricated request."
        with self.assertRaisesRegex(ValueError, "request quote"):
            self.validated_response()

    def validated_response(self):
        response = AuthoredTestResponse.model_validate(self.raw)
        _validate_authoring_response(response=response, context=self.context, planned_task=self.task)
        return response

    def setUp(self):
        fixture = test_unified_authoring.UnifiedAuthoringTests()
        self.context, self.task, self.raw = fixture._context(), fixture._task(), fixture._raw_response()
        self.raw["schema_version"] = 3
        self.raw["query"] = "Send sam@example.com an investor update on our recent wins and funding."
        self.raw["request_requirement_quotes"] = ["sam@example.com"]
        self.raw["expected_arguments"][0].update(
            comparison="exact", check_id="recipient", action_id="investor_email",
        )
        self.raw["prose_requirements"][0].update(
            check_id="funding", action_id="investor_email", request_requirement_quote="",
        )
        self.raw["requested_result_checks"] = [{
            "requested_result": self.task.required_results[0].result, "check_ids": ["funding"],
        }]

    def candidate(self):
        response = self.validated_response()
        design = _completed_design_from_authoring(response=response, context=self.context, planned_task=self.task)
        return assemble_candidate(
            context=self.context, planned_task=self.task, evaluation_date="2026-09-14",
            test_id="1", design=design, blind_query=BlindQueryResponse(query=response.query),
        )

    def test_new_schema_exposes_only_named_checks_and_declared_methods(self):
        schema = _authoring_response_schema(3)
        self.assertEqual(schema["properties"]["schema_version"]["const"], 3)
        mapping = schema["$defs"]["RequestedResultChecks"]["properties"]
        self.assertIn("check_ids", mapping)
        self.assertNotIn("assertion_indexes", mapping)
        arguments = schema["$defs"]["ExpectedToolArgument"]
        self.assertIn("action_id", arguments["required"])
        self.assertEqual(arguments["properties"]["comparison"]["enum"],
                         ["exact", "number", "date", "instant", "list_includes"])
        repair = json.dumps(_grading_correction_response_schema(2))
        self.assertIn("action_id", repair)
        self.assertNotIn('"recipient_matches"', repair)
        self.assertNotIn('"email_recipients_received"', repair)

    def test_complete_investor_email_and_split_calls(self):
        candidate = self.candidate()
        config = candidate["grade"]["config"]
        self.assertEqual(config["check_version"], 2)
        self.assertEqual([check["check_id"] for check in config["assertions"]], ["recipient", "funding"])
        complete = {"tool": "send_email", "args": {"to": "sam@example.com", "body": "Funding context"}}
        with patch("graders.llm_judge.judge_field_value", return_value={"ok": True, "reason": "Equivalent meaning"}) as judge:
            self.assertTrue(grade_tool_trace([complete], config, candidate["test"])["passed"])
            self.assertEqual(judge.call_args.args[1], self.raw["prose_requirements"][0]["criterion"])
        split = [copy.deepcopy(complete), copy.deepcopy(complete)]
        split[0]["args"]["body"] = "No funding context"
        split[1]["args"]["to"] = "other@example.com"
        with patch("graders.llm_judge.judge_field_value", side_effect=[
            {"ok": False, "reason": "Missing funding"}, {"ok": True, "reason": "Complete funding"},
        ]):
            grade = grade_tool_trace(split, config, candidate["test"])
        self.assertFalse(grade["passed"])
        self.assertEqual(grade["details"][1]["call_evaluations"][0]["reason"], "Missing funding")

    def test_examples_grade_full_calls_and_resume_without_rejudging(self):
        candidate = self.candidate()
        checks = candidate["grade"]["config"]["assertions"]
        reference = [{"tool": "send_email", "args": {"to": "sam@example.com", "body": "Complete funding"}}]
        response = SimpleNamespace(reference_tool_calls_json=json.dumps(reference), grading_probes=[
            GradingProbe(assertion_index=0, reference_call_index=0,
                         correct_value_json='"sam@example.com"', incorrect_value_json='"other@example.com"'),
            GradingProbe(assertion_index=1, reference_call_index=0,
                         correct_value_json='"Equivalent funding"', incorrect_value_json='"Missing funding"'),
        ])
        request = {"check_version": 2, "grading_checks": checks, "user_request": candidate["test"],
                   "evaluation_date": "2026-09-14", "selected_tool_contracts": self.context.tools}
        # Semantic decisions are fixtures. This tests wiring, not model quality.
        def judge(value, criterion, **kwargs):
            return {"ok": value != "Missing funding", "reason": "Fixture semantic decision"}
        with tempfile.TemporaryDirectory() as directory, patch("graders.llm_judge.judge_field_value", side_effect=judge):
            result = check_grading_examples(response=response, request=request, out=Path(directory))
            self.assertTrue(result["passed"])
            self.assertTrue(any(row["case"].startswith("split_action") for row in result["cases"]))
            with patch("graders.llm_judge.judge_field_value", side_effect=AssertionError("must reuse")):
                self.assertEqual(check_grading_examples(response=response, request=request, out=Path(directory)), result)

    def test_correction_preserves_format_target_and_request(self):
        candidate = self.candidate()
        replacement = copy.deepcopy(candidate["grade"]["config"]["assertions"][1])
        replacement["criterion"] += " An omitted or negated funding claim fails."
        correction = {"operations": [{"operation": "replace", "issue_indexes": [0],
                                      "assertion_index": 1, "assertion": replacement}],
                      "expected_no_history_failed_check_index": 1}
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory) / "source.yaml", Path(directory) / "corrected.yaml"
            write_candidate(source, candidate)
            original = source.read_bytes()
            _corrected_grading_candidate(source=source, destination=destination,
                                        decision=test_grading_rules.GradingRuleTests._grading_decision(), correction=correction)
            updated = yaml.safe_load(destination.read_text())
            expected = copy.deepcopy(candidate)
            expected["grade"]["config"]["assertions"][1] = replacement
            self.assertEqual(updated, expected)
            self.assertEqual(source.read_bytes(), original)

    def test_unknown_mapping_or_hybrid_comparison_is_rejected(self):
        self.raw["requested_result_checks"][0]["check_ids"] = ["missing"]
        with self.assertRaisesRegex(ValueError, "unknown check"):
            self.candidate()
        self.raw["expected_arguments"][0]["comparison"] = "email_delivery"
        with self.assertRaises(ValueError):
            AuthoredTestResponse.model_validate(self.raw)

    def test_different_ids_cannot_disguise_duplicate_checks_on_one_action(self):
        duplicate = copy.deepcopy(self.raw["prose_requirements"][0])
        duplicate["check_id"] = "funding_again"
        self.raw["prose_requirements"].append(duplicate)
        self.raw["requested_result_checks"][0]["check_ids"].append("funding_again")
        with self.assertRaisesRegex(ValueError, "same grading assertion"):
            self.candidate()
