"""Focused tests for the fact-free design-to-request handoff."""

from __future__ import annotations

import unittest

from authoring.models import BlindQueryResponse, DesignResponse
from authoring.pipeline import (
    AuthoringValidationError, _validate_query_contract, _validate_request_handoff,
    assemble_candidate, validate_design,
)
from authoring.run import _blind_query_request


class StructuredRequestHandoffTests(unittest.TestCase):
    def test_query_contract_allows_canonical_new_work_and_newer_instruction(self) -> None:
        queries = (
            "Please send an investor update with our latest wins and funding context.",
            "Please send a first-contact external email to the partner about the launch update.",
            "Please send the external update today without a CC and sign it Morgan Chen.",
        )

        for query in queries:
            with self.subTest(query=query):
                candidate = self._assemble_candidate(query)
                self.assertEqual(candidate["test"], query)

    def test_query_contract_rejects_standalone_recall_and_memory_pointers(self) -> None:
        rejected = (
            "What funding round did we raise?",
            "Please use MY USUAL external signature.",
            "Please use my normal outbound signature.",
            "Write this in my preferred style.",
            "As we discussed, send the update.",
            "You know how I like it, so draft the note.",
            "Use what I told you for the announcement.",
            "Use the details already established in the slide.",
            "Draft this from the context available to you.",
            "Draft this based on the background context.",
        )

        for query in rejected:
            with self.subTest(query=query):
                with self.assertRaises(AuthoringValidationError):
                    self._assemble_candidate(query)

    def test_query_contract_rejects_blank_required_details_narrowly(self) -> None:
        with self.assertRaisesRegex(AuthoringValidationError, "blank/TBD"):
            _validate_query_contract(
                query="Please leave the required owner and boundary details TBD."
            )
        _validate_query_contract(
            query="Please update the CRM row with the owner and boundary details."
        )
        _validate_query_contract(
            query="Please do not leave the owner or boundary blank."
        )

    def test_query_contract_allows_ordinary_time_and_normal_wording(self) -> None:
        _validate_query_contract(
            query="Compare the previous quarter with the current quarter."
        )
        _validate_query_contract(
            query="Tell the team that normal support hours resume earlier on Friday."
        )

    def test_answer_values_are_rejected_from_all_request_handoff_fields(self) -> None:
        answer = "Approval exists to return the draft to staging."
        for field_name in (
            "present_situation",
            "requested_work",
            "missing_information",
            "information_the_request_must_not_reveal",
        ):
            with self.subTest(field_name=field_name):
                design = self._design(answers_that_must_not_appear=[answer])
                leaked = design.model_copy(update={field_name: answer})
                with self.assertRaisesRegex(AuthoringValidationError, field_name):
                    _validate_request_handoff(leaked)

    def _assemble_candidate(self, query: str) -> dict[str, object]:
        design = self._design(answers_that_must_not_appear=["$18M"])
        return assemble_candidate(
            context=self._context(), planned_task=self._planned_task(),
            evaluation_date="2026-09-14", test_id="1", design=design,
            blind_query=BlindQueryResponse(query=query),
        )

    def test_query_preserves_the_supplied_meeting_date(self):
        design = self._design(
            present_situation="The operating-read meeting is September 18, 2026.",
            requested_work="Please prepare the follow-up note for that meeting.",
        )
        for date in ("September 18, 2026", "Sept. 18, 2026"):
            with self.subTest(date=date):
                _validate_query_contract(query=f"Please prepare the {date} follow-up note.", design=design)
        for date in ("September 14, 2026", "Sept 14, 2026", "Sept. 14, 2026", "2026-09-14"):
            with self.subTest(date=date), self.assertRaisesRegex(AuthoringValidationError, date):
                _validate_query_contract(query=f"Please prepare the {date} follow-up note.", design=design)

    def test_blind_writer_receives_exactly_the_four_fact_free_fields(self) -> None:
        design = self._design()
        _validate_request_handoff(design)

        self.assertEqual(
            _blind_query_request(design),
            {
                "present_situation": "The release note is due today.",
                "requested_work": "Please prepare the release note.",
                "missing_information": (
                    "Whether the draft is allowed back in staging."
                ),
                "information_the_request_must_not_reveal": (
                    "Whether the draft is allowed back in staging."
                ),
            },
        )

    def test_new_request_handoff_requires_all_four_fields(self) -> None:
        payload = self._design().model_dump(mode="json")
        del payload["missing_information"]

        with self.assertRaisesRegex(ValueError, "missing_information"):
            DesignResponse.model_validate(payload)

    def test_omission_fields_cannot_repeat_an_answer_value(self) -> None:
        design = self._design(
            information_the_request_must_not_reveal=(
                "Approval exists to return the draft to staging."
            )
        )

        problems = validate_design(self._context(), self._planned_task(), design)
        self.assertTrue(any("answer-bearing values" in problem for problem in problems))

    @staticmethod
    def _design(**overrides: object) -> DesignResponse:
        payload: dict[str, object] = {
            "outcome": "design",
            "rejection_reason": "",
            "source_message_meaning": "Morgan gave a standing release instruction.",
            "reason_it_applies_on_test_date": "No later message changed it.",
            "present_situation": "The release note is due today.",
            "requested_work": "Please prepare the release note.",
            "missing_information": (
                "Whether the draft is allowed back in staging."
            ),
            "information_the_request_must_not_reveal": (
                "Whether the draft is allowed back in staging."
            ),
            "existing_records": [],
            "new_records": [],
            "expected_tool_calls": ["send_email"],
            "grading_checks": [
                {
                    "fact_ids": [1],
                    "assertion": {
                        "type": "field_equals",
                        "tool": "send_email",
                        "path": "args.to",
                        "value": "editor@example.com",
                    },
                }
            ],
            "answers_that_must_not_appear": [
                "Approval exists to return the draft to staging."
            ],
            "with_history_result": "Prepare the release note with the right status.",
            "without_history_result": "The status would be unknown.",
            "without_memory_failed_check": 0,
        }
        payload.update(overrides)
        return DesignResponse.model_validate(payload)

    @staticmethod
    def _context() -> object:
        from types import SimpleNamespace

        return SimpleNamespace(
            tools={"send_email": {"arguments": ["to"], "state_effect": {}}}
        )

    @staticmethod
    def _planned_task() -> object:
        from authoring.models import PlannedTask, RequiredResult

        return PlannedTask(
            task_description="Prepare a release note.",
            fact_ids=[1],
            required_results=[
                RequiredResult(result="Use the remembered release status.", fact_ids=[1])
            ],
            expected_tools=["send_email"],
            action_without_memory="The assistant would not know the release status.",
            why_memory_changes_result="The status changes the release note.",
            fact_application_explanations_on_evaluation_date=[
                {
                    "fact_id": 1,
                    "why_fact_still_applies_on_evaluation_date": "No later message changed it.",
                }
            ],
            why_listed_tools_can_complete_requested_work=(
                "send_email can send the release note."
            ),
        )


if __name__ == "__main__":
    unittest.main()
