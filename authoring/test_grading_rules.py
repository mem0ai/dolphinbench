"""Regression coverage for the canonical authoring grading rules."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml
from jsonschema import Draft202012Validator

from authoring import create_tests as create_tests_module
from authoring.create_tests import (
    _corrected_grading_candidate,
    _default_grading_corrector,
    _grading_correction_response_schema,
    _validate_grading_assertions,
)
from authoring.final_trace_review import FinalTraceDecision
from authoring.final_trace_review import build_final_trace_review_request
from authoring.models import (
    DesignResponse,
    FactApplicationExplanationOnEvaluationDate,
    GradingCheck,
    PlannedTask,
    RequiredResult,
)
from authoring.pipeline import validate_design
from authoring.run import _assertion_for_expected_argument
from graders.mechanical import grade_tool_trace


class GradingRuleTests(unittest.TestCase):
    @staticmethod
    def _correction_assertion(
        assertion_type: str,
        *,
        path: object = None,
        value: object = None,
        pattern: object = None,
        criterion: object = None,
        count: object = None,
    ) -> dict[str, object]:
        return {
            "type": assertion_type,
            "tool": "update_crm",
            "path": path,
            "value": value,
            "pattern": pattern,
            "criterion": criterion,
            "count": count,
        }

    @staticmethod
    def _grading_decision(*, issue_count: int = 1) -> FinalTraceDecision:
        return FinalTraceDecision.model_validate(
            {
                "test_id": 1,
                "accept": False,
                "issues": [
                    {
                        "owning_step": "grading_checks",
                        "specific_problem": f"Check {index} is wrong.",
                        "required_correction": f"Correct check {index}.",
                        "request_requirement_quote": "Send the update.",
                        "source_session_id": "",
                        "source_text_quote": "",
                    }
                    for index in range(issue_count)
                ],
            }
        )

    @staticmethod
    def _candidate_with_assertions(assertions: list[dict[str, object]]) -> dict[str, object]:
        return {
            "id": "001",
            "narrative_anchor_date": "2026-09-14",
            "test": "Update the named CRM record.",
            "load_bearing_facts": [1],
            "expected_tool_calls": ["update_crm"],
            "grade": {"type": "tool_trace", "config": {"assertions": assertions}},
            "mock_state": {},
        }

    def _context(self) -> object:
        return SimpleNamespace(
            tools={
                "update_crm": {
                    "arguments": ["name", "status", "owner"],
                    "state_effect": {"writes_state_keys": ["crm"]},
                    "description": "Updates one CRM contact.",
                },
                "post_pr_comment": {
                    "arguments": ["pr_id", "body"],
                    "state_effect": {"writes_state_keys": ["pr_comments"]},
                    "description": "Posts one pull-request comment.",
                },
                "list_inbox": {
                    "arguments": ["query"],
                    "state_effect": {"reads_state_keys": ["inbox"]},
                    "description": "Reads matching inbox messages.",
                },
            }
        )

    @staticmethod
    def _planned_task(*, expected_tools: list[str] | None = None) -> PlannedTask:
        return PlannedTask(
            task_description="Update the named CRM record.",
            fact_ids=[1],
            required_results=[
                RequiredResult(
                    result="Use the remembered account name.", fact_ids=[1]
                )
            ],
            expected_tools=expected_tools or ["update_crm"],
            action_without_memory="The assistant would not know the account name.",
            why_memory_changes_result="The account name changes the record updated.",
            fact_application_explanations_on_evaluation_date=[
                FactApplicationExplanationOnEvaluationDate(
                    fact_id=1,
                    why_fact_still_applies_on_evaluation_date="No later message changes it.",
                )
            ],
            why_listed_tools_can_complete_requested_work=(
                "update_crm accepts the name and status for the record."
            ),
        )

    @staticmethod
    def _design(
        grading_checks: list[dict[str, object]], *, expected_tools: list[str] | None = None
    ) -> DesignResponse:
        return DesignResponse.model_validate(
            {
                "outcome": "design",
                "rejection_reason": "",
                "source_message_meaning": "Morgan named the account to update.",
                "reason_it_applies_on_test_date": "No later message changed it.",
                "present_situation": "The CRM record needs its status updated.",
                "requested_work": "Update the CRM record and set its status to active.",
                "missing_information": "Which account record needs the update.",
                "information_the_request_must_not_reveal": "The account name.",
                "existing_records": [],
                "new_records": [],
                "expected_tool_calls": expected_tools or ["update_crm"],
                "grading_checks": grading_checks,
                "answers_that_must_not_appear": ["Northstar Ventures"],
                "with_history_result": "Update Northstar Ventures to active.",
                "without_history_result": "The assistant cannot identify the account.",
                "without_memory_failed_check": 0,
            }
        )

    def test_check_without_a_fact_id_is_valid_for_requested_state(self) -> None:
        check = GradingCheck(
            fact_ids=[],
            assertion={
                "type": "field_equals",
                "tool": "update_crm",
                "path": "args.status",
                "value": "active",
            },
        )
        self.assertEqual(check.fact_ids, [])

    def test_request_only_result_is_valid_when_selected_fact_is_covered(self) -> None:
        task = PlannedTask(
            task_description="Send one update.",
            fact_ids=[1],
            required_results=[
                RequiredResult(
                    result="Include the remembered account name.", fact_ids=[1]
                ),
                RequiredResult(
                    result="Send exactly one message as requested.", fact_ids=[]
                ),
            ],
            expected_tools=["update_crm"],
            action_without_memory="The assistant would not know the account name.",
            why_memory_changes_result="The account name changes the record updated.",
            fact_application_explanations_on_evaluation_date=[
                FactApplicationExplanationOnEvaluationDate(
                    fact_id=1,
                    why_fact_still_applies_on_evaluation_date="No later message changes it.",
                )
            ],
            why_listed_tools_can_complete_requested_work=(
                "update_crm can update the requested record."
            ),
        )
        self.assertEqual(task.required_results[1].fact_ids, [])

    def test_request_only_result_does_not_cover_a_selected_fact(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "every and only planned fact_ids must support a required result"
        ):
            PlannedTask(
                task_description="Send one update.",
                fact_ids=[1],
                required_results=[
                    RequiredResult(
                        result="Send exactly one message as requested.", fact_ids=[]
                    )
                ],
                expected_tools=["update_crm"],
                action_without_memory="The assistant would not know the account name.",
                why_memory_changes_result="The account name changes the record updated.",
                fact_application_explanations_on_evaluation_date=[
                    FactApplicationExplanationOnEvaluationDate(
                        fact_id=1,
                        why_fact_still_applies_on_evaluation_date=(
                            "No later message changes it."
                        ),
                    )
                ],
                why_listed_tools_can_complete_requested_work=(
                    "update_crm can update the requested record."
                ),
            )

        design = self._design(
            [
                {
                    "fact_ids": [1],
                    "assertion": {
                        "type": "field_equals",
                        "tool": "update_crm",
                        "path": "args.name",
                        "value": "Northstar Ventures",
                    },
                },
                {
                    "fact_ids": [],
                    "assertion": {
                        "type": "field_equals",
                        "tool": "update_crm",
                        "path": "args.status",
                        "value": "active",
                    },
                },
            ]
        )
        self.assertEqual(
            validate_design(self._context(), self._planned_task(), design), []
        )

    def test_selected_fact_cannot_be_covered_only_by_present_request_checks(self) -> None:
        design = self._design(
            [
                {
                    "fact_ids": [],
                    "assertion": {
                        "type": "field_equals",
                        "tool": "update_crm",
                        "path": "args.status",
                        "value": "active",
                    },
                }
            ]
        )
        problems = validate_design(self._context(), self._planned_task(), design)
        self.assertTrue(
            any("memory-dependent grading checks must cover" in problem for problem in problems)
        )

    def test_every_final_action_needs_positive_grading_evidence(self) -> None:
        design = self._design(
            [
                {
                    "fact_ids": [1],
                    "assertion": {
                        "type": "field_equals",
                        "tool": "update_crm",
                        "path": "args.name",
                        "value": "Northstar Ventures",
                    },
                }
            ],
            expected_tools=["update_crm", "post_pr_comment"],
        )
        problems = validate_design(
            self._context(),
            self._planned_task(expected_tools=["update_crm", "post_pr_comment"]),
            design,
        )
        self.assertIn(
            "final action tools have no non-negative grading evidence for: ['post_pr_comment']",
            problems,
        )

    def test_supporting_read_does_not_need_a_separate_check(self) -> None:
        design = self._design(
            [
                {
                    "fact_ids": [1],
                    "assertion": {
                        "type": "field_equals",
                        "tool": "update_crm",
                        "path": "args.name",
                        "value": "Northstar Ventures",
                    },
                }
            ],
            expected_tools=["list_inbox", "update_crm"],
        )
        problems = validate_design(
            self._context(),
            self._planned_task(expected_tools=["list_inbox", "update_crm"]),
            design,
        )
        self.assertEqual(problems, [])

    def test_negative_assertion_does_not_prove_an_expected_call_happened(self) -> None:
        design = self._design(
            [
                {
                    "fact_ids": [1],
                    "assertion": {
                        "type": "tool_not_called",
                        "tool": "update_crm",
                    },
                }
            ]
        )
        problems = validate_design(self._context(), self._planned_task(), design)
        self.assertIn(
            "final action tools have no non-negative grading evidence for: ['update_crm']",
            problems,
        )

    def test_zero_exact_count_does_not_prove_an_expected_call_happened(self) -> None:
        design = self._design(
            [
                {
                    "fact_ids": [1],
                    "assertion": {
                        "type": "tool_call_count",
                        "tool": "update_crm",
                        "count": 0,
                    },
                }
            ]
        )
        problems = validate_design(self._context(), self._planned_task(), design)
        self.assertIn(
            "final action tools have no non-negative grading evidence for: ['update_crm']",
            problems,
        )

    def test_exact_count_is_supported_only_with_a_real_count(self) -> None:
        _validate_grading_assertions(
            [{"type": "tool_call_count", "tool": "post_pr_comment", "count": 1}]
        )
        with self.assertRaisesRegex(ValueError, "non-negative integer count"):
            _validate_grading_assertions(
                [{"type": "tool_call_count", "tool": "post_pr_comment", "count": True}]
            )
        with self.assertRaisesRegex(ValueError, "non-negative integer count"):
            _validate_grading_assertions(
                [{"type": "tool_call_count", "tool": "post_pr_comment", "count": -1}]
            )

    def test_pull_request_identity_uses_an_explicit_comparison(self) -> None:
        from authoring.run import _assertion_for_expected_argument

        assertion = _assertion_for_expected_argument(
            tool="post_pr_comment", argument="pr", value="example/repo#4",
            history_fact_ids=[], comparison="exact",
        )
        self.assertEqual(assertion["type"], "field_equals")
        self.assertEqual(assertion["path"], "args.pr")

    def test_grading_correction_schema_requires_every_variant_property(self) -> None:
        schema = _grading_correction_response_schema()
        Draft202012Validator.check_schema(schema)
        encoded_schema = json.dumps(schema)
        self.assertNotIn('"minLength"', encoded_schema)
        self.assertNotIn('"minimum"', encoded_schema)
        self.assertEqual(
            set(schema["required"]), set(schema["properties"])
        )
        variants = schema["properties"]["operations"]["items"]["anyOf"]
        self.assertEqual(len(variants), 3)
        for variant in variants:
            self.assertEqual(
                set(variant["required"]), set(variant["properties"])
            )
            self.assertFalse(variant["additionalProperties"])

    def test_grading_correction_schema_accepts_each_valid_variant(self) -> None:
        schema = _grading_correction_response_schema()
        validator = Draft202012Validator(schema)
        response = {
            "operations": [
                {
                    "operation": "delete",
                    "issue_indexes": [0],
                    "assertion_index": 0,
                    "assertion": None,
                },
                {
                    "operation": "replace",
                    "issue_indexes": [1],
                    "assertion_index": 1,
                    "assertion": self._correction_assertion(
                        "field_regex", path="args.body", pattern="launch"
                    ),
                },
                {
                    "operation": "add",
                    "issue_indexes": [1],
                    "assertion_index": 2,
                    "assertion": self._correction_assertion(
                        "field_llm_judge",
                        path="args.body",
                        criterion="The body states the launch date.",
                    ),
                },
            ],
            "expected_no_history_failed_check_index": 0,
        }
        self.assertEqual(list(validator.iter_errors(response)), [])

    def test_grading_correction_schema_rejects_null_type_specific_fields(self) -> None:
        validator = Draft202012Validator(_grading_correction_response_schema())

        invalid_assertions = [
            self._correction_assertion(
                "field_regex", path="args.body", pattern=None
            ),
            self._correction_assertion(
                "field_llm_judge", path="args.body", criterion=None
            ),
            self._correction_assertion(
                "field_equals", path="args.status", value=None
            ),
            self._correction_assertion(
                "date_on", path="args.date", value=None
            ),
            self._correction_assertion(
                "time_of_day_gte", path="args.start", value=None
            ),
            self._correction_assertion(
                "field_lte", path="args.amount", value=None
            ),
            self._correction_assertion("tool_call_count", count=None),
            self._correction_assertion(
                "field_equals", path=None, value="active"
            ),
            self._correction_assertion(
                "field_llm_judge", path=None, criterion="Required content is present."
            ),
        ]
        for assertion in invalid_assertions:
            with self.subTest(assertion=assertion):
                response = {
                    "operations": [
                        {
                            "operation": "replace",
                            "issue_indexes": [0],
                            "assertion_index": 0,
                            "assertion": assertion,
                        }
                    ],
                    "expected_no_history_failed_check_index": 0,
                }
                self.assertFalse(validator.is_valid(response))

    def test_schema_leaves_content_constraints_to_local_validation(self) -> None:
        validator = Draft202012Validator(_grading_correction_response_schema())
        structurally_valid = [
            self._correction_assertion(
                "field_regex", path="", pattern="launch"
            ),
            self._correction_assertion(
                "field_regex", path="args.body", pattern=""
            ),
            self._correction_assertion(
                "field_llm_judge", path="args.body", criterion=""
            ),
            self._correction_assertion("tool_call_count", count=-1),
        ]
        for assertion in structurally_valid:
            with self.subTest(assertion=assertion):
                response = {
                    "operations": [
                        {
                            "operation": "replace",
                            "issue_indexes": [0],
                            "assertion_index": 0,
                            "assertion": assertion,
                        }
                    ],
                    "expected_no_history_failed_check_index": -1,
                }
                self.assertTrue(validator.is_valid(response))

        with self.assertRaisesRegex(ValueError, "has no path"):
            _validate_grading_assertions([structurally_valid[0]])
        with self.assertRaisesRegex(ValueError, "has no pattern"):
            _validate_grading_assertions([structurally_valid[1]])
        with self.assertRaisesRegex(ValueError, "has no criterion"):
            _validate_grading_assertions([structurally_valid[2]])
        with self.assertRaisesRegex(ValueError, "non-negative integer count"):
            _validate_grading_assertions([structurally_valid[3]])

    def test_grading_patch_changes_only_named_assertions(self) -> None:
        original = [
            self._correction_assertion("tool_called"),
            self._correction_assertion(
                "field_regex", path="args.body", pattern="old owner"
            ),
            self._correction_assertion(
                "field_regex", path="args.body", pattern="keep this exact"
            ),
            self._correction_assertion(
                "field_regex", path="args.body", pattern="remove this"
            ),
        ]
        replacement = self._correction_assertion(
            "field_llm_judge",
            path="args.body",
            criterion="The body names the correct owner.",
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.yaml"
            destination = Path(directory) / "candidate.yaml"
            source.write_text(yaml.safe_dump(self._candidate_with_assertions(original), sort_keys=False))
            failed_index = _corrected_grading_candidate(
                source=source,
                destination=destination,
                decision=self._grading_decision(issue_count=2),
                correction={
                    "operations": [
                        {
                            "operation": "replace",
                            "issue_index": 0,
                            "assertion_index": 1,
                            "assertion": replacement,
                        },
                        {
                            "operation": "delete",
                            "issue_index": 1,
                            "assertion_index": 3,
                            "assertion": None,
                        },
                    ],
                    "expected_no_history_failed_check_index": 1,
                },
            )
            after = yaml.safe_load(destination.read_text())["grade"]["config"]["assertions"]

        self.assertEqual(failed_index, 1)
        self.assertEqual(after, [original[0], replacement, original[2]])
        self.assertEqual(after[0], original[0])
        self.assertEqual(after[2], original[2])

    def test_grading_patch_cannot_add_unrequested_tool_call_count(self) -> None:
        original = [
            self._correction_assertion("tool_called"),
            self._correction_assertion(
                "field_regex", path="args.body", pattern="owner"
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.yaml"
            destination = Path(directory) / "candidate.yaml"
            source.write_text(yaml.safe_dump(self._candidate_with_assertions(original), sort_keys=False))
            _corrected_grading_candidate(
                source=source,
                destination=destination,
                decision=self._grading_decision(),
                correction={
                    "operations": [
                        {
                            "operation": "replace",
                            "issue_index": 0,
                            "assertion_index": 1,
                            "assertion": self._correction_assertion(
                                "field_llm_judge",
                                path="args.body",
                                criterion="The body names the required owner.",
                            ),
                        }
                    ],
                    "expected_no_history_failed_check_index": 1,
                },
            )
            after = yaml.safe_load(destination.read_text())["grade"]["config"]["assertions"]

        self.assertFalse(any(item["type"] == "tool_call_count" for item in after))
        self.assertEqual(after[0], original[0])

    def test_grading_patch_rejects_invalid_or_conflicting_operations(self) -> None:
        original = [self._correction_assertion("tool_called")]
        cases = [
            (
                "does not reference a reviewer grading-check issue",
                [
                    {
                        "operation": "delete",
                        "issue_index": 1,
                        "assertion_index": 0,
                        "assertion": None,
                    }
                ],
            ),
            (
                "assertion index out of range",
                [
                    {
                        "operation": "delete",
                        "issue_index": 0,
                        "assertion_index": 1,
                        "assertion": None,
                    }
                ],
            ),
            (
                "conflicting operations",
                [
                    {
                        "operation": "delete",
                        "issue_index": 0,
                        "assertion_index": 0,
                        "assertion": None,
                    },
                    {
                        "operation": "replace",
                        "issue_index": 0,
                        "assertion_index": 0,
                        "assertion": self._correction_assertion("tool_not_called"),
                    },
                ],
            ),
            (
                "has no pattern",
                [
                    {
                        "operation": "replace",
                        "issue_index": 0,
                        "assertion_index": 0,
                        "assertion": self._correction_assertion(
                            "field_regex", path="args.body", pattern=""
                        ),
                    }
                ],
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.yaml"
            source.write_text(yaml.safe_dump(self._candidate_with_assertions(original), sort_keys=False))
            for message, operations in cases:
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    _corrected_grading_candidate(
                        source=source,
                        destination=Path(directory) / "candidate.yaml",
                        decision=self._grading_decision(),
                        correction={
                            "operations": operations,
                            "expected_no_history_failed_check_index": 0,
                        },
                    )

    def test_one_grading_edit_can_address_multiple_issues(self) -> None:
        original = [self._correction_assertion("tool_called")]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.yaml"
            destination = Path(directory) / "candidate.yaml"
            source.write_text(yaml.safe_dump(self._candidate_with_assertions(original)))
            for indexes in ([], [0, 0], [0, 2], [True], "0", [[0]]):
                with self.subTest(indexes=indexes), self.assertRaises(ValueError):
                    _corrected_grading_candidate(
                        source=source, destination=destination,
                        decision=self._grading_decision(issue_count=2),
                        correction={"operations": [{
                            "operation": "replace", "issue_indexes": indexes,
                            "assertion_index": 0, "assertion": original[0],
                        }], "expected_no_history_failed_check_index": 0},
                    )
            _corrected_grading_candidate(
                source=source, destination=destination,
                decision=self._grading_decision(issue_count=2),
                correction={"operations": [{
                    "operation": "replace", "issue_indexes": [0, 1],
                    "assertion_index": 0, "assertion": original[0],
                }], "expected_no_history_failed_check_index": 0},
            )
            self.assertEqual(yaml.safe_load(destination.read_text())["test"],
                             self._candidate_with_assertions(original)["test"])

    def test_grading_patch_must_address_every_reviewer_issue(self) -> None:
        original = [self._correction_assertion("tool_called")]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.yaml"
            source.write_text(yaml.safe_dump(self._candidate_with_assertions(original), sort_keys=False))
            with self.assertRaisesRegex(ValueError, "must address every reviewer grading-check issue"):
                _corrected_grading_candidate(
                    source=source,
                    destination=Path(directory) / "candidate.yaml",
                    decision=self._grading_decision(issue_count=2),
                    correction={
                        "operations": [
                            {
                                "operation": "delete",
                                "issue_index": 0,
                                "assertion_index": 0,
                                "assertion": None,
                            }
                        ],
                        "expected_no_history_failed_check_index": 0,
                    },
                )

    def test_default_grading_corrector_sends_the_strict_schema(self) -> None:
        response = {
            "operations": [
                {
                    "operation": "delete",
                    "issue_indexes": [0],
                    "assertion_index": 0,
                    "assertion": None,
                }
            ],
            "expected_no_history_failed_check_index": 0,
        }

        def complete(*_args: object, **kwargs: object) -> dict[str, object]:
            schema = kwargs["response_schema"]
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(response)
            return response

        with tempfile.TemporaryDirectory() as directory, patch.object(
            create_tests_module,
            "cached_client_complete",
            side_effect=complete,
        ) as client_complete:
            result = _default_grading_corrector(
                request={"current_test": {}},
                out=Path(directory),
                client=SimpleNamespace(),
            )

        self.assertEqual(result, response)
        schema = client_complete.call_args.kwargs["response_schema"]
        self.assertEqual(
            set(schema["required"]), set(schema["properties"])
        )

    def test_final_review_receives_only_the_selected_tool_contracts(self) -> None:
        planned_task = self._planned_task()
        candidate = {
            "id": 1,
            "narrative_anchor_date": "2026-09-14",
            "test": "Update the CRM record.",
            "load_bearing_facts": [1],
            "expected_tool_calls": ["update_crm"],
            "grade": {"type": "tool_trace", "config": {"assertions": []}},
            "mock_state": {},
        }
        context = SimpleNamespace(
            tools={
                "update_crm": {
                    "arguments": ["name", "status", "owner"],
                    "state_effect": {},
                },
                "post_pr_comment": {"arguments": ["pr_id", "body"], "state_effect": {}},
            },
            facts_by_id={
                1: {
                    "statement": "The account is Northstar Ventures.",
                    "applies_when": "Current until replaced.",
                    "source_session_ids": ["session-1"],
                    "supersedes": [],
                }
            },
            sessions_by_id={"session-1": {"session_id": "session-1", "messages": []}},
        )
        shots = [
            {"with_memory": True},
            {"with_memory": True},
            {"with_memory": False},
            {"with_memory": False},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate_path = root / "candidate.yaml"
            candidate_path.write_text(yaml.safe_dump(candidate, sort_keys=False))
            gate_path = root / "gate.json"
            gate_path.write_text(
                json.dumps(
                    {
                        "candidate_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
                        "result": {
                            "valid": False,
                            "verdict": "unsolvable_with_memory",
                            "g1_pass_count": 1,
                            "g2_pass_count": 0,
                            "shots": shots,
                        },
                    }
                )
            )
            request = build_final_trace_review_request(
                candidate_path=candidate_path,
                certification_gate_path=gate_path,
                planned_task=planned_task,
                context=context,
            )
        self.assertEqual(list(request["selected_tool_contracts"]), ["update_crm"])
        self.assertEqual(
            request["selected_tool_contracts"]["update_crm"]["arguments"],
            ["name", "status", "owner"],
        )
        self.assertEqual(
            request["certification_result"],
            {
                "valid": False,
                "verdict": "unsolvable_with_memory",
                "with_history_pass_count": 1,
                "without_history_pass_count": 0,
            },
        )


class ArgumentComparisonTests(unittest.TestCase):
    def grade(self, assertion: dict, arguments: dict) -> bool:
        return grade_tool_trace(
            [{"tool": assertion["tool"], "args": arguments}], {"assertions": [assertion]},
        )["passed"]

    def test_service_identity_is_checked_in_the_service_argument(self) -> None:
        assertion = _assertion_for_expected_argument(
            tool="create_runbook_entry", argument="service", value="metrics-router",
            history_fact_ids=[], comparison="exact",
        )
        self.assertEqual(assertion, {
            "type": "field_equals", "tool": "create_runbook_entry",
            "path": "args.service", "value": "metrics-router",
        })
        self.assertTrue(self.grade(assertion, {"service": "metrics-router"}))
        self.assertFalse(self.grade(assertion, {"service": "another-service", "body": "metrics-router"}))

    def test_attendee_order_and_unrestricted_extras_do_not_fail(self) -> None:
        assertion = _assertion_for_expected_argument(
            tool="create_calendar_event", argument="attendees", value=["Devika", "Anya", "Mom"],
            history_fact_ids=[1], comparison="list_includes",
        )
        self.assertTrue(self.grade(assertion, {"attendees": ["Mom", "Anya", "Devika"]}))
        self.assertTrue(self.grade(assertion, {"attendees": ["Mom", "Anya", "Devika", "Alex"]}))
        self.assertFalse(self.grade(assertion, {"attendees": ["Mom", "Anya"]}))

    def test_equivalent_timezone_offsets_pass_but_wrong_or_naive_times_fail(self) -> None:
        for argument in ("start", "end"):
            with self.subTest(argument=argument):
                assertion = _assertion_for_expected_argument(
                    tool="create_calendar_event", argument=argument, value="2028-01-02T18:00:00-05:00",
                    history_fact_ids=[1], comparison="instant",
                )
                self.assertEqual(assertion["type"], "datetime_absolute_eq")
                for value in ("2028-01-02T18:00:00-05:00", "2028-01-02T23:00:00Z", "2028-01-02T23:00:00+00:00"):
                    self.assertTrue(self.grade(assertion, {argument: value}))
                for value in ("2028-01-02T18:00:00Z", "2028-01-02T18:00:00", "not a timestamp"):
                    self.assertFalse(self.grade(assertion, {argument: value}))

    def test_delivery_accepts_to_cc_split_and_rejects_body_mentions(self) -> None:
        assertion = _assertion_for_expected_argument(
            tool="send_email", argument="to", value=["Benefits", "Payroll"],
            history_fact_ids=[], comparison="email_delivery",
        )
        with patch("graders.llm_judge.judge_field_value", return_value={"ok": False, "reason": "missing recipient"}) as judge:
            self.assertTrue(self.grade(assertion, {"to": "Benefits", "cc": ["Payroll"]}))
            self.assertTrue(self.grade(assertion, {"to": "Payroll", "cc": ["Benefits", "Another person"]}))
            self.assertFalse(self.grade(assertion, {"to": "Benefits", "body": "Payroll is mentioned here."}))
            self.assertNotIn("body", judge.call_args.args[0])

    def test_judge_transport_error_cannot_count_as_a_successful_negative_probe(self) -> None:
        assertion = {"type": "field_llm_judge", "tool": "send_email", "path": "args.body", "criterion": "Does it include the required amount?"}
        with patch("graders.llm_judge._call_judge", side_effect=TimeoutError("provider timeout")):
            with self.assertRaises(TimeoutError):
                grade_tool_trace(
                    [{"tool": "send_email", "args": {"body": "wrong amount"}}],
                    {"assertions": [assertion], "raise_on_judge_error": True},
                )


if __name__ == "__main__":
    unittest.main()
