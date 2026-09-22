from __future__ import annotations

import unittest

from construction.build_history_window import (
    build_enrichment_payload,
    correction_evidence_for_contact,
)
from construction.model_input_text import render_model_input, render_story_planner_input


class SharedWeeklyPlannerContextTest(unittest.TestCase):
    def test_completed_contact_and_future_outcome_survive_exact_and_correction_context(self) -> None:
        completed_contact = {
            "session_id": "session-completed",
            "date": "2027-04-01",
            "accepted_contact_details": "Completed the Beacon category close at 12 accounts.",
            "subject_ids": ["riley", "beacon"],
            "thread_id": "",
        }
        unrelated_contact = {
            "session_id": "session-unrelated",
            "date": "2027-04-02",
            "accepted_contact_details": "A separate matter about another project.",
            "subject_ids": ["riley", "unrelated_project"],
            "thread_id": "",
        }
        future_outcome = {
            "id": "beacon-final-close",
            "start_date": "2027-04-14",
            "end_date": "2027-04-14",
            "subjects": ["riley", "beacon"],
            "lasting_outcome": "Beacon closes at 18 accounts on April 14.",
        }
        payload = {
            "persona": "riley",
            "requested_dates": {"start": "2027-04-07", "end": "2027-04-13"},
            "developments_for_this_week": [{"id": "beacon-in-progress"}],
            "accepted_developments_after_this_window": [future_outcome],
            "accepted_contact_chronology": [completed_contact, unrelated_contact],
            "current_facts": [],
            "lasting_facts_that_must_be_established": [],
            "current_ordinary_states": [],
            "known_entities": [],
            "open_threads": [],
            "available_simulated_actions": [],
            "available_app_records": {},
            "_exact_app_records": {},
            "earlier_accepted_app_items": [],
            "previously_used_communication_destinations": [],
        }
        contact = {
            "contact_id": "riley_2027_04_07_001",
            "subject_ids": ["riley", "beacon"],
            "thread_id": "",
            "what_happened": "Review the Beacon count.",
            "assistant_outcome": {"kind": "conversation", "requested_result": "Review it."},
        }

        other_contact = {"contact_id": "riley_2027_04_08_001", "what_happened": "Close Beacon."}
        exact_context = build_enrichment_payload(
            payload,
            {"plan": {"occurrences": [contact]}},
            other_fixed_contacts_this_week=[other_contact],
        )
        correction_context = correction_evidence_for_contact(payload, contact)
        exact_text = render_model_input(exact_context)
        correction_text = render_model_input(
            {"accepted_outcome_and_contact_evidence": correction_context}
        )

        self.assertEqual(
            exact_context["previous_accepted_contacts_with_shared_subject_ids"],
            [completed_contact],
        )
        self.assertEqual(exact_context["accepted_developments_after_this_window"], [future_outcome])
        self.assertEqual(exact_context["other_fixed_contacts_this_week"], [other_contact])
        for text in (exact_text, correction_text):
            self.assertIn("Completed the Beacon category close at 12 accounts.", text)
            self.assertIn("Beacon closes at 18 accounts on April 14.", text)


class StoryInputTextTest(unittest.TestCase):
    def test_story_input_marks_prior_context_as_evidence_not_work(self) -> None:
        rendered = render_story_planner_input(
            {
                "persona": "alex",
                "narrative_timezone": "America/New_York",
                "open_threads": [{"what_remains": "A reply is still pending."}],
                "older_contacts_that_already_happened": [
                    {
                        "date": "2026-10-17",
                        "contact_purpose": "Compare the formal route with an informal footpath.",
                    }
                ],
                "recently_finished_contacts": [],
            }
        )

        self.assertIn(
            "THINGS THAT WERE STILL UNRESOLVED BEFORE THIS WEEK", rendered
        )
        self.assertIn("It is not a list of work for this week.", rendered)
        self.assertIn(
            "Do not reuse or paraphrase its situation, source material, question, or result.",
            rendered,
        )
        self.assertIn(
            "On 2026-10-17, Alex already contacted the assistant about: Compare the "
            "formal route with an informal footpath.",
            rendered,
        )
        self.assertTrue(
            rendered.rstrip().endswith(
                "Now plan only the requested dates. Keep the accepted changes and "
                "current conditions above true. Use earlier contacts only to avoid "
                "repeating them."
            )
        )

    def test_story_input_puts_this_weeks_work_before_historical_conditions(self) -> None:
        rendered = render_story_planner_input(
            {
                "persona": "riley",
                "narrative_timezone": "America/Chicago",
                "current_conditions": [{"statement": "An older condition."}],
                "open_threads": [{"what_remains": "A reply is pending."}],
                "developments_for_this_week": [
                    {"what_happens": "A required change happens."}
                ],
                "requested_dates": {"start": "2026-10-01", "end": "2026-10-07"},
                "older_contacts_that_already_happened": [],
                "recently_finished_contacts": [],
            }
        )

        self.assertLess(
            rendered.index("DATES TO PLAN"),
            rendered.index("CHANGES THAT MUST FINISH DURING THESE DATES"),
        )
        self.assertLess(
            rendered.index("CHANGES THAT MUST FINISH DURING THESE DATES"),
            rendered.index("THINGS THAT WERE STILL UNRESOLVED BEFORE THIS WEEK"),
        )
        self.assertLess(
            rendered.index("THINGS THAT WERE STILL UNRESOLVED BEFORE THIS WEEK"),
            rendered.index("WHAT IS CURRENTLY TRUE"),
        )


if __name__ == "__main__":
    unittest.main()
