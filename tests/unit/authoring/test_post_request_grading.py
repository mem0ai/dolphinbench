"""Tests for grading that is designed after the exact user request exists."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

from pydantic import ValidationError

from authoring.models import (
    BlindQueryResponse,
    DesignResponse,
    GradingDesignResponse,
    PlannedTask,
    RequiredResult,
)
from authoring.run import _attempt, _grading_design_request


class PostRequestGradingTests(unittest.TestCase):
    def test_grading_receives_the_final_request_and_saves_its_own_evidence(self) -> None:
        planned_task = PlannedTask(
            task_description="Send an investor update.",
            fact_ids=[1],
            required_results=[
                RequiredResult(
                    result="Include the earlier funding amount in the email body.",
                    fact_ids=[1],
                )
            ],
            expected_tools=["send_email"],
            action_without_memory="The email would omit the funding amount.",
            why_memory_changes_result="The funding amount changes the email body.",
            fact_application_explanations_on_evaluation_date=[
                {
                    "fact_id": 1,
                    "why_fact_still_applies_on_evaluation_date": "No later message changes it.",
                }
            ],
            why_listed_tools_can_complete_requested_work=(
                "send_email can send the requested update."
            ),
        )
        context = SimpleNamespace(
            tools={
                "send_email": {
                    "arguments": ["to", "body"],
                    "state_effect": {"reads_state_keys": []},
                }
            },
            readable_state={},
            app_state={},
            checkpoint_path=Path("checkpoint"),
        )
        config = SimpleNamespace(
            persona="morgan", evaluation_date="2026-09-14"
        )
        design_response = {
            "outcome": "design",
            "rejection_reason": "",
            "source_message_meaning": "Morgan said the Series B was $18M.",
            "reason_it_applies_on_test_date": "No later source changes the amount.",
            "present_situation": "An investor update is due today.",
            "requested_work": "Send the investor update with our latest wins and funding context.",
            "missing_information": "The funding context to include.",
            "information_the_request_must_not_reveal": "The funding amount.",
            "existing_records": [],
            "new_records": [],
            "required_results": [
                {
                    "result": "Include the earlier funding amount in the email body.",
                    "fact_ids": [1],
                }
            ],
            "expected_tool_calls": ["send_email"],
        }
        final_query = "Please send the investor update with our latest wins and funding context."
        grading_response = {
            "grading_checks": [
                {
                    "fact_ids": [1],
                    "assertion": {
                        "type": "field_equals",
                        "tool": "send_email",
                        "path": "args.body",
                        "value": "We raised an $18M Series B.",
                    },
                }
            ],
            "answers_that_must_not_appear": ["$18M"],
            "with_history_expected_result": "Send the investor update with the funding amount.",
            "without_history_expected_result": "Send the update without the funding amount.",
            "without_history_failed_assertion_index": 0,
            "requested_result_checks": [
                {
                    "requested_result": "Include the earlier funding amount in the email body.",
                    "assertion_indexes": [0],
                }
            ],
            "assertion_examples": [
                {
                    "assertion_index": 0,
                    "correct_example": "The email says we raised an $18M Series B.",
                    "incorrect_or_negated_example": "The email leaves out the funding amount.",
                }
            ],
        }
        calls: list[tuple[str, dict[str, object]]] = []

        def complete(
            _cache: Path,
            system: str,
            payload: dict[str, object],
            _client: object,
            *,
            response_schema: dict[str, object] | None = None,
            response_schema_name: str | None = None,
        ) -> dict[str, object]:
            self.assertIsNone(response_schema)
            self.assertIsNone(response_schema_name)
            calls.append((system, payload))
            if "Turn one proposed test" in system:
                return design_response
            if "Write one natural request" in system:
                return {"query": final_query}
            if "Write the smallest fair set of grading checks" in system:
                return grading_response
            self.fail(f"unexpected model system: {system[:80]}")

        design_request = {
            "selected_fact_evidence": [{"id": 1, "messages": ["Morgan: $18M Series B"]}],
            "current_facts_that_may_change_the_meaning": [],
            "tools": context.tools,
        }
        with tempfile.TemporaryDirectory() as temp, patch(
            "authoring.run.cached_client_complete", side_effect=complete
        ):
            candidate, result = _attempt(
                directory=Path(temp),
                attempt_name="initial",
                design_system="Turn one proposed test into an executable test, or reject it with one exact reason.",
                design_request=design_request,
                planned_task=planned_task,
                context=context,
                config=config,
                test_id=1,
                design_client=SimpleNamespace(),
                query_client=SimpleNamespace(),
                certifier=lambda *_args, **_kwargs: {"valid": True},
            )

            self.assertIsNotNone(candidate)
            self.assertEqual(result["stage"], "accepted")
            self.assertNotIn("grading_checks", design_response)
            grading_payload = calls[2][1]
            self.assertEqual(grading_payload["final_user_request"], final_query)
            self.assertEqual(
                grading_payload["planned_required_results"], design_response["required_results"]
            )
            self.assertEqual(grading_payload["selected_fact_evidence"], design_request["selected_fact_evidence"])
            self.assertTrue(grading_payload["supported_assertion_shapes"])
            self.assertEqual(candidate["test"], final_query)
            self.assertEqual(
                candidate["grade"]["config"]["assertions"],
                [grading_response["grading_checks"][0]["assertion"]],
            )
            self.assertEqual(result["grading"], grading_response)
            self.assertEqual(
                json.loads((Path(temp) / "initial_design_response.json").read_text()),
                design_response,
            )
            self.assertEqual(
                json.loads((Path(temp) / "initial_grading_response.json").read_text()),
                grading_response,
            )

    def test_grading_evidence_requires_one_example_for_every_assertion(self) -> None:
        payload = {
            "grading_checks": [
                {
                    "fact_ids": [1],
                    "assertion": {"type": "tool_called", "tool": "send_email"},
                }
            ],
            "answers_that_must_not_appear": ["$18M"],
            "with_history_expected_result": "Send the update with funding context.",
            "without_history_expected_result": "Send it without funding context.",
            "without_history_failed_assertion_index": 0,
            "requested_result_checks": [
                {"requested_result": "Include funding context.", "assertion_indexes": [0]}
            ],
            "assertion_examples": [],
        }
        with self.assertRaisesRegex(ValidationError, "assertion_examples"):
            GradingDesignResponse.model_validate(payload)

    def test_request_correction_rebuilds_complete_grading_input(self) -> None:
        planned_task = PlannedTask(
            task_description="Send an investor update.",
            fact_ids=[1],
            required_results=[
                RequiredResult(
                    result="Include the earlier funding amount in the email body.",
                    fact_ids=[1],
                )
            ],
            expected_tools=["send_email"],
            action_without_memory="The email would omit the funding amount.",
            why_memory_changes_result="The funding amount changes the email body.",
            fact_application_explanations_on_evaluation_date=[
                {
                    "fact_id": 1,
                    "why_fact_still_applies_on_evaluation_date": "No later message changes it.",
                }
            ],
            why_listed_tools_can_complete_requested_work="send_email can send the update.",
        )
        design = DesignResponse.model_validate(
            {
                "outcome": "design",
                "rejection_reason": "",
                "source_message_meaning": "Morgan said the Series B was $18M.",
                "reason_it_applies_on_test_date": "No later source changes the amount.",
                "present_situation": "An investor update is due today.",
                "requested_work": "Send the investor update with funding context.",
                "missing_information": "The funding context to include.",
                "information_the_request_must_not_reveal": "The funding amount.",
                "existing_records": [],
                "new_records": [],
                "required_results": [
                    {
                        "result": "Include the earlier funding amount in the email body.",
                        "fact_ids": [1],
                    }
                ],
                "expected_tool_calls": ["send_email"],
            }
        )
        context = SimpleNamespace(
            tools={
                "send_email": {
                    "arguments": ["to", "body"],
                    "state_effect": {"reads_state_keys": []},
                }
            },
            readable_state={},
            app_state={},
        )
        rebuilt_input = {
            "selected_fact_evidence": [{"id": 1, "messages": ["Morgan: $18M Series B"]}],
            "current_facts_that_may_change_the_meaning": [{"id": 2}],
            "tools": context.tools,
        }

        with patch("authoring.run.design_payload", return_value=rebuilt_input) as payload:
            request = _grading_design_request(
                design=design,
                blind_query=BlindQueryResponse(
                    query="Please send the investor update with our latest wins and funding context."
                ),
                planned_task=planned_task,
                context=context,
                config=SimpleNamespace(),
                design_request={},
            )

        payload.assert_called_once_with(
            config=ANY, context=context, planned_task=planned_task
        )
        self.assertEqual(request["selected_fact_evidence"], rebuilt_input["selected_fact_evidence"])
        self.assertEqual(
            request["current_facts_that_may_change_the_meaning"],
            rebuilt_input["current_facts_that_may_change_the_meaning"],
        )
        self.assertEqual(request["tools"], context.tools)
        self.assertEqual(request["current_state_for_this_test"], {})


if __name__ == "__main__":
    unittest.main()
