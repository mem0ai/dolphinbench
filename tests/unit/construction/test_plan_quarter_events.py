from __future__ import annotations

import argparse
import copy
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from construction.checkpoints import save_checkpoint
from construction.long_history_prompts import (
    QUARTER_EVENT_EVENT_REPAIR_SYSTEM,
    QUARTER_EVENT_FACT_FINALIZATION_SYSTEM,
    QUARTER_EVENT_FACT_REPAIR_SYSTEM,
    QUARTER_EVENT_QUALITY_REVIEW_SYSTEM,
    QUARTER_EVENT_STORY_SYSTEM,
)
from construction.plan_quarter_events import (
    build_canonical_plan,
    correction_review_scope,
    approved_event_id_renames,
    canonical_plan_for_context,
    normalize_candidate_plan,
    derive_replacement_accounting,
    event_repair_payload,
    expand_fact_repair_scope,
    fact_repair_payload,
    failed_event_review_ids,
    failed_fact_only_event_ids,
    failed_fact_event_ids,
    reconstruct_consolidated_candidate,
    load_or_complete_stage,
    load_latest_full_quality_review_request,
    load_failed_candidate_findings,
    merge_event_repair,
    merge_failed_candidate_findings,
    merge_fact_repair,
    previous_calendar_month_sessions,
    primary_subject_ids_for_persona,
    quality_review_payload,
    render_quarter_stage_request,
    render_quarter_quality_review_request,
    merge_quality_reviews,
    merge_targeted_quality_review,
    normalize_story_event_order,
    normalize_legacy_passing_review_reasons,
    restrict_quality_review,
    run,
    run_failed_candidate_correction,
    rename_event_ids_in_finalization_response,
    validate_event_fact_annotations,
    validate_event_repair_response,
    validate_fact_replacements,
    validate_replacement_accounting,
    validate_plan,
    validate_quality_review,
    targeted_quality_review_payload,
    validate_story,
)


class QuarterEventPlanningTest(unittest.TestCase):
    def test_story_events_are_normalized_by_end_date_then_start_date(self) -> None:
        story = self._story(second_event=True)
        first, second = story["story"]["events"]
        first["start_date"] = "2024-06-02"
        first["end_date"] = "2024-06-03"
        second["start_date"] = "2024-06-01"
        second["end_date"] = "2024-06-03"

        normalized = normalize_story_event_order(story)

        self.assertEqual(
            [event["id"] for event in normalized["story"]["events"]],
            [second["id"], first["id"]],
        )
        self.assertEqual(story["story"]["events"], [first, second])

    def test_legacy_passing_review_reasons_are_cleared_without_changing_failures(
        self,
    ) -> None:
        review = {
            "passed": False,
            "plan_review": {"passed": True, "reason": "The plan is coherent."},
            "event_reviews": [
                {"event_id": "one", "passed": True, "reason": "This event works."},
                {"event_id": "two", "passed": False, "reason": "This event is vague."},
            ],
            "fact_reviews": [],
            "replacement_reviews": [],
        }

        normalized = normalize_legacy_passing_review_reasons(review)

        self.assertEqual(normalized["plan_review"]["reason"], "")
        self.assertEqual(normalized["event_reviews"][0]["reason"], "")
        self.assertEqual(
            normalized["event_reviews"][1]["reason"], "This event is vague."
        )
        self.assertEqual(review["plan_review"]["reason"], "The plan is coherent.")

    def test_failed_candidate_reconstruction_and_repair_targets(self) -> None:
        story = self._story()
        finalization = self._finalization_response(story)
        canonical, errors = build_canonical_plan(story, finalization)
        self.assertEqual(errors, [])
        assert canonical is not None
        accounting = derive_replacement_accounting(finalization, canonical)
        rebuilt_story, rebuilt_facts, rebuilt_accounting, rebuilt_plan = (
            reconstruct_consolidated_candidate(
                canonical["plan"],
                {
                    "proposed_plan": canonical["plan"],
                    "replacement_accounting": accounting,
                },
            )
        )
        self.assertEqual(rebuilt_plan, canonical)
        self.assertEqual(build_canonical_plan(rebuilt_story, rebuilt_facts), (canonical, []))
        self.assertEqual(rebuilt_accounting, accounting)

        review = self._passing_review(canonical)
        review["passed"] = False
        review["event_reviews"][0].update(passed=False, reason="Fix the event.")
        self.assertEqual(failed_event_review_ids(review), {"event-1"})
        self.assertEqual(failed_fact_only_event_ids(review), set())
        review["fact_reviews"][0].update(passed=False, reason="Fix the fact.")
        self.assertEqual(failed_fact_only_event_ids(review), {"event-1"})

    def test_primary_subject_uses_the_literal_persona_id(self) -> None:
        self.assertEqual(
            primary_subject_ids_for_persona(
                "riley", [{"id": "riley", "name": "Riley Tanaka"}]
            ),
            {"riley"},
        )
        with self.assertRaisesRegex(ValueError, "riley"):
            primary_subject_ids_for_persona(
                "riley", [{"id": "person:riley", "name": "Riley Tanaka"}]
            )

    def _story_event(
        self, event_id: str = "event-1", start_date: str = "2024-04-10"
    ) -> dict:
        return {
            "id": event_id,
            "start_date": start_date,
            "end_date": start_date,
            "subjects": ["morgan"],
            "builds_on_event_ids": [] if event_id == "event-1" else ["event-1"],
            "what_happens": "Morgan assigns the forecast to Sarah.",
            "lasting_outcome": "Sarah owns the forecast.",
        }

    def _story(self, *, second_event: bool = False) -> dict:
        events = [self._story_event()]
        if second_event:
            events.append(self._story_event("event-2", "2024-05-10"))
        return {
            "story": {
                "period_start": "2024-04-01",
                "period_end": "2024-06-30",
                "new_entities": [],
                "protected_changes": [],
                "events": events,
            }
        }

    def _story_validation_arguments(self) -> dict:
        return {
            "period_start": date(2024, 4, 1),
            "period_end": date(2024, 6, 30),
            "starting_entities": [{"id": "morgan"}],
            "starting_facts": [],
            "overview": {},
            "previous_plan": {"events": []},
        }

    def _event(self) -> dict:
        return {
            **self._story_event(),
            "relevant_existing_fact_ids": [7],
            "facts_to_establish": [
                {
                    "key": "forecast_owner",
                    "statement": "Sarah owns the forecast.",
                    "applies_when": "After April 10",
                    "subjects": ["morgan"],
                    "supersedes": [5],
                    "supersedes_fact_keys": [],
                }
            ],
        }

    def _finalized_plan(self, story: dict) -> dict:
        story_body = story["story"]
        events = []
        for story_event in story_body["events"]:
            event = copy.deepcopy(story_event)
            if event["id"] == "event-1":
                event.update(
                    {
                        "relevant_existing_fact_ids": [7],
                        "facts_to_establish": [
                            {
                                "key": "forecast_owner",
                                "statement": (
                                    "Sarah owns the Q2 forecast, and the Friday update "
                                    "remains required."
                                ),
                                "applies_when": "After April 10",
                                "subjects": ["morgan"],
                                "supersedes": [5],
                                "supersedes_fact_keys": [],
                            }
                        ],
                    }
                )
            else:
                event.update(
                    {
                        "relevant_existing_fact_ids": [],
                        "facts_to_establish": [],
                    }
                )
            events.append(event)
        return {
            "plan": {
                "period_start": story_body["period_start"],
                "period_end": story_body["period_end"],
                "new_entities": copy.deepcopy(story_body["new_entities"]),
                "protected_changes": copy.deepcopy(
                    story_body.get("protected_changes") or []
                ),
                "events": events,
            }
        }

    def _finalization_response(self, story: dict) -> dict:
        return {
            "event_fact_annotations": [
                {
                    "event_id": "event-1",
                    "relevant_existing_fact_ids": [7],
                    "facts_to_establish": [
                        {
                            "key": "forecast_owner",
                            "statement": (
                                "Sarah owns the forecast. "
                                "The Friday update remains required."
                            ),
                            "applies_when": "After April 10",
                            "subjects": ["morgan"],
                        }
                    ],
                }
            ],
            "fact_replacements": [
                {
                    "event_id": "event-1",
                    "replaced_fact": {"source": "starting_checkpoint", "fact_id": 5},
                    "replacement_fact_numbers": [1],
                    "still_current_information": [
                        "The Friday update remains required."
                    ],
                    "changed_or_ended_information": [
                        "Morgan no longer owns the forecast; Sarah owns it."
                    ],
                }
            ],
        }

    def _derived_accounting(self, story: dict, response: dict) -> list[dict]:
        canonical, errors = build_canonical_plan(story, response)
        self.assertEqual(errors, [])
        assert canonical is not None
        return derive_replacement_accounting(response, canonical)

    def _passing_review(self, response: dict) -> dict:
        events = response["plan"]["events"]
        return {
            "passed": True,
            "plan_review": {"passed": True, "reason": ""},
            "event_reviews": [
                {"event_id": event["id"], "passed": True, "reason": ""}
                for event in events
            ],
            "fact_reviews": [
                {
                    "event_id": "event-1",
                    "fact_key": "forecast_owner",
                    "passed": True,
                    "reason": "",
                }
            ],
            "replacement_reviews": [
                {
                    "event_id": "event-1",
                    "fact_key": "forecast_owner",
                    "replaced_fact_ref": "id:5",
                    "passed": True,
                    "reason": "",
                }
            ],
        }

    def test_quality_review_payload_contains_all_and_only_current_facts(self) -> None:
        response = {"plan": {"new_entities": [], "events": [self._event()]}}
        facts = [
            {"id": 3, "statement": "Retired owner", "applies_when": "Before March"},
            {
                "id": 5,
                "statement": "Old owner",
                "applies_when": "Before April",
                "supersedes": [3],
            },
            {"id": 7, "statement": "Forecast exists", "applies_when": "Always"},
            {"id": 9, "statement": "Unrelated", "applies_when": "Always"},
        ]

        accounting = [
            {
                "event_id": "event-1",
                "replaced_fact_ref": "id:5",
                "replacement_fact_keys": ["forecast_owner"],
                "still_current_information": ["The Friday update remains required."],
                "changed_or_ended_information": ["Morgan no longer owns the forecast."],
            }
        ]
        payload = quality_review_payload(
            response,
            facts,
            replacement_accounting=accounting,
            primary_subject_ids={"morgan"},
        )

        self.assertEqual(
            [fact["id"] for fact in payload["current_fact_registry"]], [5, 7, 9]
        )
        self.assertNotIn("referenced_existing_facts", payload)
        self.assertNotIn("fact_checks", payload)
        self.assertNotIn("fixed_story", payload)
        self.assertEqual(
            [event["id"] for event in payload["proposed_plan"]["events"]],
            ["event-1"],
        )
        self.assertEqual(payload["replacement_accounting"], accounting)

    def test_quality_review_renderer_orders_events_and_includes_fact_replacement_content(
        self,
    ) -> None:
        early_event = {
            **self._event(),
            "id": "early-event",
            "start_date": "2024-04-10",
            "end_date": "2024-04-10",
            "facts_to_establish": [
                {
                    "key": "early_forecast_rule",
                    "statement": "The forecast uses the April review rule.",
                    "applies_when": "After April 10.",
                    "subjects": ["morgan"],
                    "supersedes": [],
                    "supersedes_fact_keys": [],
                }
            ],
        }
        late_event = {
            **self._event(),
            "id": "late-event",
            "start_date": "2024-06-20",
            "end_date": "2024-06-20",
            "builds_on_event_ids": ["early-event"],
            "relevant_existing_fact_ids": [5],
            "facts_to_establish": [
                {
                    "key": "late_forecast_rule",
                    "statement": "The forecast uses Sarah's final review rule.",
                    "applies_when": "After June 20.",
                    "subjects": ["morgan"],
                    "supersedes": [5],
                    "supersedes_fact_keys": ["early_forecast_rule"],
                }
            ],
        }
        payload = {
            "proposed_plan": {
                "new_entities": [{"id": "person:sarah", "introduced_in_event_id": "late-event"}],
                "events": [late_event, early_event],
            },
            "current_fact_registry": [
                {
                    "id": 5,
                    "statement": "Morgan owns the forecast and sends the Friday update.",
                    "applies_when": "Until the owner changes.",
                    "subjects": ["morgan"],
                }
            ],
            "replacement_accounting": [
                {
                    "event_id": "late-event",
                    "replaced_fact_ref": "id:5",
                    "replacement_fact_keys": ["late_forecast_rule"],
                    "still_current_information": ["The Friday update remains required."],
                    "changed_or_ended_information": ["Morgan no longer owns the forecast."],
                }
            ],
            "event_local_continuity_evidence": [
                {
                    "event_id": "late-event",
                    "relevant_existing_fact_ids": [5],
                    "source_session_ids": ["session-5"],
                }
            ],
            "multi_year_overview": {"next_year": "The forecast process will expand."},
            "completed_previous_quarter_plan": {
                "period_start": "2024-01-01",
                "period_end": "2024-03-31",
                "protected_changes": [],
                "new_entities": [],
                "events": [
                    {**late_event, "id": "previous-late", "start_date": "2024-03-20"},
                    {**early_event, "id": "previous-early", "start_date": "2024-01-10"},
                ],
            },
        }

        rendered = render_quarter_quality_review_request(payload)

        timeline = rendered.index("Proposed-quarter timeline")
        self.assertLess(rendered.index("Event early-event", timeline), rendered.index("Event late-event", timeline))
        self.assertLess(rendered.index("Event ID: previous-early"), rendered.index("Event ID: previous-late"))
        self.assertIn("Fact ID 5:", rendered)
        self.assertIn("Morgan owns the forecast and sends the Friday update.", rendered)
        self.assertIn("Earlier event IDs it continues:\n  - early-event", rendered)
        self.assertIn("The forecast uses Sarah's final review rule.", rendered)
        self.assertIn("Old fact reference: id:5", rendered)
        self.assertIn("The Friday update remains required.", rendered)
        self.assertIn("Morgan no longer owns the forecast.", rendered)
        self.assertIn("person:sarah", rendered)
        self.assertIn("The forecast process will expand.", rendered)

    def test_quality_review_call_uses_rendered_text_and_keeps_json_provenance(self) -> None:
        payload = quality_review_payload(
            {"plan": {"new_entities": [], "events": [self._event()]}},
            [
                {"id": 5, "statement": "Old owner", "applies_when": "Before April"},
                {"id": 7, "statement": "Forecast exists", "applies_when": "Always"},
            ],
            replacement_accounting=[],
            primary_subject_ids={"morgan"},
        )
        calls: list[tuple[str, str, Path]] = []

        class FakeClient:
            def complete(self, system: str, user_input: str, *, call_path: Path) -> dict:
                calls.append((system, user_input, call_path))
                return {"passed": True}

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            response, called = load_or_complete_stage(
                FakeClient(),
                output_dir=output_dir,
                name="quality_review",
                system="review system",
                payload=payload,
            )

            self.assertTrue(called)
            self.assertEqual(response, {"passed": True})
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], render_quarter_quality_review_request(payload))
            self.assertEqual(
                json.loads((output_dir / "quality_review_request.json").read_text()),
                payload,
            )
            self.assertEqual(
                (output_dir / "quality_review_user_input.txt").read_text(), calls[0][1]
            )

    def test_all_quarter_stage_calls_use_saved_plain_text_input(self) -> None:
        stage_payloads = {
            "story": {"period_start": "2025-10-01", "named_subjects": ["riley"]},
            "fact_finalization": {"fixed_story": {"events": []}, "current_fact_registry": []},
            "event_repair": {"failed_events": [], "accepted_current_facts": []},
            "fact_repair": {"event_ids_to_repair": ["event-1"], "fixed_story_events": []},
            "quality_review": {"proposed_plan": {"events": [], "new_entities": []}},
        }
        calls: list[tuple[str, str, Path]] = []

        class FakeClient:
            def complete(self, system: str, user_input: str, *, call_path: Path) -> dict:
                calls.append((system, user_input, call_path))
                return {"stage": system}

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            for name, payload in stage_payloads.items():
                response, called = load_or_complete_stage(
                    FakeClient(),
                    output_dir=output_dir,
                    name=name,
                    system=f"{name} system",
                    payload=payload,
                )
                self.assertTrue(called)
                self.assertEqual(response, {"stage": f"{name} system"})
                self.assertEqual(
                    (output_dir / f"{name}_user_input.txt").read_text(),
                    render_quarter_stage_request(name, payload),
                )
                self.assertEqual(
                    json.loads((output_dir / f"{name}_request.json").read_text()), payload
                )

            self.assertEqual(len(calls), len(stage_payloads))
            self.assertTrue(all(isinstance(user_input, str) for _, user_input, _ in calls))

            for name, payload in stage_payloads.items():
                response, called = load_or_complete_stage(
                    FakeClient(),
                    output_dir=output_dir,
                    name=name,
                    system=f"{name} system",
                    payload=payload,
                )
                self.assertFalse(called)
                self.assertEqual(response, {"stage": f"{name} system"})

            (output_dir / "story_user_input.txt").unlink()
            with self.assertRaisesRegex(ValueError, "story: a saved response requires"):
                load_or_complete_stage(
                    FakeClient(),
                    output_dir=output_dir,
                    name="story",
                    system="story system",
                    payload=stage_payloads["story"],
                )

    def test_current_fact_rendering_distinguishes_current_and_replaced_ids(self) -> None:
        rendered = render_quarter_stage_request(
            "fact_finalization",
            {
                "fixed_story": {"events": []},
                "current_fact_registry": [
                    {
                        "id": 220,
                        "construction_key": "standing_framework_method",
                        "statement": "The standing framework remains active.",
                        "supersedes": [193, 202],
                    }
                ],
            },
        )

        self.assertIn("Current fact ID 220:", rendered)
        self.assertIn(
            "Old fact IDs already replaced; do not cite them:\n      - 193\n      - 202",
            rendered,
        )
        self.assertNotIn("Item 1:", rendered)

    def test_current_session_state_is_visible_to_story_and_review_calls(self) -> None:
        state = {
            "key": "kibo_registration_renewal_2027",
            "subject_ids": ["riley", "kibo"],
            "statement": "Kibo's registration is current through June 15, 2028.",
            "source_session_id": "extension_riley_2027_06_30_002",
            "date": "2027-06-30",
        }
        story_text = render_quarter_stage_request(
            "story", {"current_ordinary_states": [state]}
        )
        review_text = render_quarter_stage_request(
            "quality_review",
            {
                "proposed_plan": {"events": [], "new_entities": []},
                "current_ordinary_states": [state],
            },
        )

        for rendered in (story_text, review_text):
            self.assertIn(
                "Current conditions established by accepted sessions", rendered
            )
            self.assertIn("Kibo's registration is current through June 15, 2028.", rendered)
            self.assertIn("extension_riley_2027_06_30_002", rendered)

    def test_repair_inputs_use_the_required_top_level_section_order(self) -> None:
        event_repair_payload = {
            "existing_entities": "entities",
            "earlier_same_quarter_events": "earlier events",
            "accepted_current_facts": "current facts",
            "failed_events": "failed events",
            "failed_event_review_reasons": "review reasons",
            "quarter_date_bounds": "dates",
        }
        fact_repair_payload = {
            "fact_keys_from_other_events": "other keys",
            "later_events_that_depend_on_repaired_facts": "later events",
            "earlier_same_quarter_changes_to_starting_facts": "earlier changes",
            "relevant_earlier_quarter_facts": "earlier facts",
            "exact_starting_facts_used_or_replaced": "current facts",
            "fact_replacement_rows_to_repair": "replacement rows",
            "event_fact_annotations_to_repair": "annotations",
            "fixed_story_events": "story events",
            "review_failures": "review failures",
            "event_ids_to_repair": "event ids",
        }

        expected_event_sections = [
            "Quarter date bounds:",
            "Failed event review reasons:",
            "Failed events:",
            "Accepted current facts:",
            "Earlier same quarter events:",
            "Existing entities:",
        ]
        expected_fact_sections = [
            "Event ids to repair:",
            "Review failures:",
            "Exact starting facts used or replaced:",
            "Relevant earlier quarter facts:",
            "Fixed story events:",
            "Event fact annotations to repair:",
            "Fact replacement rows to repair:",
            "Earlier same quarter changes to starting facts:",
            "Later events that depend on repaired facts:",
            "Fact keys from other events:",
        ]
        for name, payload, sections in (
            ("event_repair", event_repair_payload, expected_event_sections),
            ("fact_repair", fact_repair_payload, expected_fact_sections),
        ):
            rendered = render_quarter_stage_request(name, payload)
            self.assertEqual([rendered.index(section) for section in sections], sorted(
                rendered.index(section) for section in sections
            ))

    def test_quality_review_evidence_uses_only_fact_and_session_references(self) -> None:
        event = self._event()
        event["subjects"] = ["morgan", "project:forecast"]
        response = {"plan": {"new_entities": [], "events": [event]}}
        matching_fact = {
            "id": 7,
            "statement": "Morgan already uses the forecast workbook.",
            "applies_when": "planning the forecast",
            "subjects": ["morgan", "project:forecast"],
            "source_session_ids": ["matching-session"],
        }
        unrelated_fact = {
            "id": 9,
            "statement": "A different team uses another tool.",
            "applies_when": "elsewhere",
            "subjects": ["morgan", "organization:unrelated"],
            "source_session_ids": ["unrelated-session"],
        }
        payload = quality_review_payload(
            response,
            [matching_fact, unrelated_fact],
            replacement_accounting=[],
            primary_subject_ids={"morgan"},
            continuity_context={
                "recent_accepted_session_purposes": [
                    {
                        "session_id": "matching-session",
                        "subject_ids": ["morgan", "project:forecast"],
                        "purpose": "Discuss the workbook.",
                        "accepted_contact_details": "The workbook is in use.",
                    },
                    {
                        "session_id": "unrelated-session",
                        "subject_ids": [
                            "morgan",
                            "organization:unrelated",
                        ],
                        "purpose": "Discuss another tool.",
                    },
                ],
                "accepted_user_messages_from_previous_month": [
                    {
                        "id": "matching-session",
                        "narrative_date": "2024-03-28T09:00:00-08:00",
                        "messages": ["The workbook has 12 active rows."],
                    },
                    {
                        "id": "unrelated-session",
                        "narrative_date": "2024-03-29T09:00:00-08:00",
                        "messages": ["The other tool has 99 rows."],
                    },
                ],
            },
        )

        evidence = payload["event_local_continuity_evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(
            evidence[0],
            {
                "event_id": "event-1",
                "relevant_existing_fact_ids": [7],
                "source_session_ids": ["matching-session"],
            },
        )
        self.assertEqual(
            [row["id"] for row in payload["continuity_context"][
                "accepted_user_messages_from_previous_month"
            ]],
            ["matching-session"],
        )
        self.assertNotIn("matching_current_facts", evidence[0])
        self.assertNotIn("matching_recent_accepted_contacts", evidence[0])
        self.assertIn(
            "A merely related fact, the overview, or a similar subject does not supply missing evidence",
            " ".join(QUARTER_EVENT_QUALITY_REVIEW_SYSTEM.split()),
        )
        self.assertIn(
            "If an event says an earlier rule or agreement remains unchanged, and the new fact does not replace the earlier fact, do not require the new fact to copy it",
            " ".join(QUARTER_EVENT_QUALITY_REVIEW_SYSTEM.split()),
        )

    def test_quality_review_evidence_rejects_unknown_current_fact_ids(self) -> None:
        event = self._event()
        event["relevant_existing_fact_ids"] = [8]
        response = {"plan": {"new_entities": [], "events": [event]}}
        with self.assertRaisesRegex(ValueError, r"unknown current facts \[8\]"):
            quality_review_payload(
                response,
                [{"id": 7, "statement": "Fact 7."}],
                replacement_accounting=[],
                primary_subject_ids={"morgan"},
            )

    def test_correction_review_payload_contains_only_corrected_event_evidence(self) -> None:
        story = self._story(second_event=True)
        finalized = self._finalization_response(story)
        finalized["event_fact_annotations"].append(
            {
                "event_id": "event-2",
                "relevant_existing_fact_ids": [7],
                "facts_to_establish": [],
            }
        )
        canonical, errors = build_canonical_plan(story, finalized)
        self.assertEqual(errors, [])
        assert canonical is not None
        payload = quality_review_payload(
            canonical,
            [
                {
                    "id": 5,
                    "statement": "Morgan owns the forecast.",
                    "applies_when": "Before April 10",
                    "source_session_ids": ["old-session"],
                },
                {
                    "id": 7,
                    "statement": "The forecast exists.",
                    "applies_when": "Always",
                    "source_session_ids": ["shared-session"],
                },
            ],
            replacement_accounting=self._derived_accounting(story, finalized),
            primary_subject_ids={"morgan"},
            continuity_context={
                "recent_accepted_session_purposes": [{"session_id": "shared-session"}],
                "accepted_user_messages_from_previous_month": [
                    {"id": "shared-session", "messages": ["Relevant"]},
                    {"id": "unrelated-session", "messages": ["Unrelated"]},
                ],
            },
            event_ids={"event-1"},
        )
        self.assertEqual(
            [event["id"] for event in payload["proposed_plan"]["events"]],
            ["event-1"],
        )
        self.assertEqual(
            [fact["id"] for fact in payload["current_fact_registry"]], [5, 7]
        )
        self.assertEqual(
            [row["event_id"] for row in payload["replacement_accounting"]],
            ["event-1"],
        )
        self.assertEqual(
            [row["id"] for row in payload["continuity_context"][
                "accepted_user_messages_from_previous_month"
            ]],
            ["shared-session"],
        )

    def test_correction_review_payload_includes_recursive_earlier_events_only_as_context(self) -> None:
        def event(event_id: str, start_date: str) -> dict:
            return {
                **self._story_event(event_id, start_date),
                "relevant_existing_fact_ids": [7],
                "facts_to_establish": [],
            }

        event_one = event("event-1", "2024-04-10")
        event_two = event("event-2", "2024-05-10")
        event_three = event("event-3", "2024-06-10")
        event_three["builds_on_event_ids"] = ["event-2"]
        unrelated = event("unrelated", "2024-06-15")
        unrelated["builds_on_event_ids"] = []
        payload = quality_review_payload(
            {
                "plan": {
                    "new_entities": [],
                    "events": [event_one, event_two, event_three, unrelated],
                }
            },
            [{"id": 7, "statement": "The forecast exists."}],
            replacement_accounting=[],
            primary_subject_ids={"morgan"},
            event_ids={"event-3"},
        )

        self.assertEqual(
            [event["id"] for event in payload["proposed_plan"]["events"]],
            ["event-3"],
        )
        self.assertEqual(
            [
                event["id"]
                for event in payload[
                    "earlier_events_used_by_the_events_being_reviewed"
                ]
            ],
            ["event-1", "event-2"],
        )
        self.assertNotIn(
            "unrelated",
            {
                event["id"]
                for event in payload[
                    "earlier_events_used_by_the_events_being_reviewed"
                ]
            },
        )

    def test_quality_review_rows_merge_back_into_the_complete_review(self) -> None:
        initial = {
            "passed": False,
            "plan_review": {"passed": True, "reason": ""},
            "event_reviews": [
                {"event_id": "event-1", "passed": False, "reason": "old"},
                {"event_id": "event-2", "passed": True, "reason": ""},
            ],
            "fact_reviews": [
                {
                    "event_id": "event-1",
                    "fact_key": "old-fact",
                    "passed": False,
                    "reason": "old",
                },
                {
                    "event_id": "event-2",
                    "fact_key": "unchanged-fact",
                    "passed": True,
                    "reason": "unchanged",
                }
            ],
            "replacement_reviews": [],
        }
        correction = {
            "passed": True,
            "plan_review": {"passed": True, "reason": ""},
            "event_reviews": [{"event_id": "event-1", "passed": True, "reason": ""}],
            "fact_reviews": [
                {
                    "event_id": "event-1",
                    "fact_key": "new-fact",
                    "passed": True,
                    "reason": "fixed",
                }
            ],
            "replacement_reviews": [],
        }
        merged = merge_quality_reviews(initial, correction, {"event-1"})
        self.assertTrue(merged["passed"])
        self.assertEqual([row["event_id"] for row in merged["event_reviews"]], ["event-1", "event-2"])
        self.assertEqual(
            [(row["event_id"], row["fact_key"]) for row in merged["fact_reviews"]],
            [("event-1", "new-fact"), ("event-2", "unchanged-fact")],
        )
        self.assertEqual(merged["fact_reviews"][1]["reason"], "unchanged")

    def test_narrow_correction_keeps_unchanged_review_rows(self) -> None:
        source_story = self._story(second_event=True)
        source_response = self._finalization_response(source_story)
        source_plan, errors = build_canonical_plan(source_story, source_response)
        self.assertEqual(errors, [])
        assert source_plan is not None

        corrected_plan = copy.deepcopy(source_plan)
        corrected_plan["plan"]["events"][0]["facts_to_establish"][0][
            "applies_when"
        ] = "After April 11"
        source_accounting = derive_replacement_accounting(source_response, source_plan)
        corrected_accounting = copy.deepcopy(source_accounting)
        scope = correction_review_scope(
            source_plan, corrected_plan, source_accounting, corrected_accounting
        )
        source_review = self._passing_review(source_plan)
        correction_review = {
            "passed": False,
            "plan_review": {"passed": True, "reason": ""},
            "event_reviews": [],
            "fact_reviews": [
                {
                    "event_id": "event-1",
                    "fact_key": "forecast_owner",
                    "passed": False,
                    "reason": "The corrected fact is still unsupported.",
                }
            ],
            "replacement_reviews": [
                {
                    "event_id": "event-1",
                    "fact_key": "forecast_owner",
                    "replaced_fact_ref": "id:5",
                    "passed": True,
                    "reason": "",
                }
            ],
        }
        merged = merge_targeted_quality_review(
            source_review, correction_review, scope
        )

        self.assertEqual(scope["event_ids"], [])
        self.assertEqual(
            scope["fact_keys"],
            [{"event_id": "event-1", "fact_key": "forecast_owner"}],
        )
        self.assertEqual(scope["review_fact_keys"], scope["fact_keys"])
        self.assertTrue(merged["event_reviews"][0]["passed"])
        self.assertFalse(merged["fact_reviews"][0]["passed"])
        self.assertFalse(merged["passed"])

    def test_narrow_review_validates_only_changed_fact_and_replacement_rows(self) -> None:
        source_story = self._story(second_event=True)
        source_response = self._finalization_response(source_story)
        source_plan, errors = build_canonical_plan(source_story, source_response)
        self.assertEqual(errors, [])
        assert source_plan is not None
        corrected_plan = copy.deepcopy(source_plan)
        corrected_plan["plan"]["events"][0]["facts_to_establish"][0][
            "applies_when"
        ] = "After April 11"
        accounting = derive_replacement_accounting(source_response, source_plan)
        scope = correction_review_scope(source_plan, corrected_plan, accounting, accounting)
        payload = targeted_quality_review_payload(
            corrected_plan,
            [{"id": 7, "statement": "The forecast exists."}],
            replacement_accounting=accounting,
            primary_subject_ids={"morgan"},
            scope=scope,
        )
        review = {
            "passed": False,
            "plan_review": {"passed": True, "reason": ""},
            "event_reviews": [],
            "fact_reviews": [
                {
                    "event_id": "event-1",
                    "fact_key": "forecast_owner",
                    "passed": False,
                    "reason": "The corrected fact is still unsupported.",
                }
            ],
            "replacement_reviews": [
                {
                    "event_id": "event-1",
                    "fact_key": "forecast_owner",
                    "replaced_fact_ref": "id:5",
                    "passed": True,
                    "reason": "",
                }
            ],
        }
        self.assertEqual(
            validate_quality_review(
                review,
                set(scope["event_ids"]),
                payload,
                expected_fact_rows={("event-1", "forecast_owner")},
                expected_replacement_rows={("event-1", "forecast_owner", "id:5")},
            ),
            [],
        )

    def test_narrow_correction_does_not_ask_reviewer_about_removed_fact_keys(self) -> None:
        source_story = self._story()
        source_response = self._finalization_response(source_story)
        source_plan, errors = build_canonical_plan(source_story, source_response)
        self.assertEqual(errors, [])
        assert source_plan is not None

        corrected_plan = copy.deepcopy(source_plan)
        corrected_fact = corrected_plan["plan"]["events"][0]["facts_to_establish"][0]
        corrected_fact["key"] = "new_forecast_owner"
        corrected_fact["supersedes"] = []
        source_accounting = derive_replacement_accounting(source_response, source_plan)
        scope = correction_review_scope(
            source_plan, corrected_plan, source_accounting, []
        )

        self.assertEqual(
            scope["fact_keys"],
            [
                {"event_id": "event-1", "fact_key": "forecast_owner"},
                {"event_id": "event-1", "fact_key": "new_forecast_owner"},
            ],
        )
        self.assertEqual(
            scope["review_fact_keys"],
            [{"event_id": "event-1", "fact_key": "new_forecast_owner"}],
        )
        self.assertEqual(scope["review_replacement_keys"], [])

    def test_repaired_whole_plan_verdict_replaces_the_old_failure(self) -> None:
        initial = {
            "passed": False,
            "plan_review": {"passed": False, "reason": "old"},
            "event_reviews": [
                {"event_id": "event-1", "passed": False, "reason": "old"}
            ],
            "fact_reviews": [],
            "replacement_reviews": [],
        }
        correction = {
            "passed": True,
            "plan_review": {"passed": True, "reason": ""},
            "event_reviews": [
                {"event_id": "event-1", "passed": True, "reason": ""}
            ],
            "fact_reviews": [],
            "replacement_reviews": [],
        }

        merged = merge_quality_reviews(initial, correction, {"event-1"})

        self.assertTrue(merged["passed"])
        self.assertEqual(merged["plan_review"], correction["plan_review"])

    def test_restrict_quality_review_discards_saved_rows_for_other_events(self) -> None:
        review = self._passing_review(self._finalized_plan(self._story(second_event=True)))
        restricted = restrict_quality_review(review, {"event-1"})
        self.assertEqual(
            [row["event_id"] for row in restricted["event_reviews"]], ["event-1"]
        )

    def test_deterministic_validation_rejects_unknown_or_obsolete_fact_links(self) -> None:
        response = {
            "plan": {
                "period_start": "2024-04-01",
                "period_end": "2024-06-30",
                "new_entities": [],
                "protected_changes": [],
                "events": [self._event()],
            }
        }
        arguments = {
            "period_start": date(2024, 4, 1),
            "period_end": date(2024, 6, 30),
            "starting_entities": [{"id": "morgan"}],
            "starting_facts": [
                {"id": 3, "construction_key": "retired_owner"},
                {"id": 5, "construction_key": "old_owner", "supersedes": [3]},
                {"id": 7, "construction_key": "forecast_exists"},
            ],
            "overview": {},
            "previous_plan": {"events": []},
        }

        self.assertEqual(validate_plan(response, **arguments), [])

        response["plan"]["events"][0]["facts_to_establish"][0]["supersedes"] = [99]
        self.assertTrue(
            any(
                "supersedes unknown facts" in error
                for error in validate_plan(response, **arguments)
            )
        )
        response["plan"]["events"][0]["facts_to_establish"][0]["supersedes"] = [3]
        self.assertTrue(
            any(
                "supersedes obsolete facts" in error
                for error in validate_plan(response, **arguments)
            )
        )
        response["plan"]["events"][0]["facts_to_establish"][0]["supersedes"] = [5]
        response["plan"]["events"][0]["relevant_existing_fact_ids"] = [3]
        self.assertTrue(
            any(
                "references obsolete starting facts" in error
                for error in validate_plan(response, **arguments)
            )
        )

    def test_replacement_review_payload_keeps_old_fact_as_comparison_only(self) -> None:
        event = self._event()
        event["what_happens"] = "Morgan assigns the forecast to Sarah."
        event["lasting_outcome"] = "Sarah owns the forecast."
        event["facts_to_establish"][0]["statement"] = (
            "Sarah owns the forecast and the Friday update remains required."
        )
        response = {"plan": {"new_entities": [], "events": [event]}}
        payload = quality_review_payload(
            response,
            [
                {
                    "id": 5,
                    "statement": "Morgan owns the forecast and the Friday update remains required.",
                    "applies_when": "Before April 10",
                },
                {"id": 7, "statement": "Forecast exists", "applies_when": "Always"},
            ],
            replacement_accounting=[],
            primary_subject_ids={"morgan"},
        )

        checked_event = payload["proposed_plan"]["events"][0]
        self.assertEqual(checked_event["what_happens"], event["what_happens"])
        self.assertEqual(checked_event["lasting_outcome"], event["lasting_outcome"])
        self.assertEqual(
            checked_event["facts_to_establish"][0]["statement"],
            "Sarah owns the forecast and the Friday update remains required.",
        )
        self.assertEqual(
            payload["current_fact_registry"][0]["statement"],
            "Morgan owns the forecast and the Friday update remains required.",
        )
        self.assertNotIn("referenced_existing_facts", payload)
        self.assertNotIn("fact_checks", payload)

    def test_event_repair_can_add_entity_and_story_validation_accepts_it(self) -> None:
        story = self._story()
        replacement = copy.deepcopy(story["story"]["events"][0])
        replacement["subjects"].append("account:acme")
        new_entity = {
            "id": "account:acme",
            "name": "Acme account",
            "kind": "account",
            "role": "The customer account that the repaired event must identify.",
            "introduced_in_event_id": "event-1",
        }
        repair = {"new_entities": [new_entity], "events": [replacement]}

        self.assertEqual(
            validate_event_repair_response(
                repair, story, {"event-1"}, known_entity_ids={"morgan"}
            ),
            [],
        )
        merged, errors = merge_event_repair(
            story_response=story,
            repair_response=repair,
            failed_event_ids={"event-1"},
            known_entity_ids={"morgan"},
        )
        self.assertEqual(errors, [])
        assert merged is not None
        self.assertEqual(merged["story"]["new_entities"], [new_entity])
        self.assertEqual(validate_story(merged, **self._story_validation_arguments()), [])

    def test_event_repair_new_entity_can_continue_into_later_repaired_event(self) -> None:
        story = self._story(second_event=True)
        replacements = copy.deepcopy(story["story"]["events"])
        for event in replacements:
            event["subjects"].append("account:acme")
        new_entity = {
            "id": "account:acme",
            "name": "Acme account",
            "kind": "account",
            "role": "The customer account used by both repaired events.",
            "introduced_in_event_id": "event-1",
        }
        repair = {"new_entities": [new_entity], "events": replacements}

        self.assertEqual(
            validate_event_repair_response(
                repair,
                story,
                {"event-1", "event-2"},
                known_entity_ids={"morgan"},
            ),
            [],
        )
        merged, errors = merge_event_repair(
            story_response=story,
            repair_response=repair,
            failed_event_ids={"event-1", "event-2"},
            known_entity_ids={"morgan"},
        )
        self.assertEqual(errors, [])
        assert merged is not None
        self.assertEqual(validate_story(merged, **self._story_validation_arguments()), [])

    def test_event_repair_rejects_invalid_entity_introduction(self) -> None:
        story = self._story()
        replacement = copy.deepcopy(story["story"]["events"][0])
        entity = {
            "id": "person:decorative",
            "name": "Decorative person",
            "kind": "person",
            "role": "Unneeded background name.",
            "introduced_in_event_id": "event-1",
        }
        errors = validate_event_repair_response(
            {"new_entities": [entity], "events": [replacement]},
            story,
            {"event-1"},
            known_entity_ids={"morgan"},
        )
        self.assertTrue(
            any(
                "introduction event must include the new entity in subjects" in error
                for error in errors
            ),
            errors,
        )

        entity["introduced_in_event_id"] = "missing-event"
        replacement["subjects"].append(entity["id"])
        errors = validate_event_repair_response(
            {"new_entities": [entity], "events": [replacement]},
            story,
            {"event-1"},
            known_entity_ids={"morgan"},
        )
        self.assertTrue(
            any("introduction event must be one repaired event" in error for error in errors),
            errors,
        )

    def test_quality_review_must_assess_known_events(self) -> None:
        errors = validate_quality_review(
            {
                "passed": False,
                "plan_review": {"passed": False, "reason": "The plan is not credible."},
                "event_reviews": [
                    {"event_id": "unknown", "passed": False, "reason": "Not credible."}
                ],
                "fact_reviews": [],
                "replacement_reviews": [],
            },
            {"event-1"},
        )

        self.assertTrue(any("every event exactly once" in error for error in errors))

    def test_event_review_failures_are_sent_to_both_repair_layers(self) -> None:
        review = {
            "event_reviews": [
                {"event_id": "event-1", "passed": False, "reason": "Dates conflict."},
                {"event_id": "event-2", "passed": True, "reason": ""},
            ],
            "fact_reviews": [
                {
                    "event_id": "event-2",
                    "fact_key": "fact-2",
                    "passed": False,
                    "reason": "Fact is unsupported.",
                }
            ],
            "replacement_reviews": [],
        }

        self.assertEqual(failed_event_review_ids(review), {"event-1"})
        self.assertEqual(failed_fact_event_ids(review), {"event-1", "event-2"})

    def test_failed_candidate_findings_use_review_row_shapes_and_merge(self) -> None:
        story = self._story()
        finalized = self._finalization_response(story)
        canonical, errors = build_canonical_plan(story, finalized)
        self.assertEqual(errors, [])
        assert canonical is not None
        findings_document = {
            "findings": [
                {"event_id": "event-1", "reason": "The event needs its exact date."},
                {
                    "event_id": "event-1",
                    "fact_key": "forecast_owner",
                    "reason": "The fact must preserve the Friday update.",
                },
                {
                    "event_id": "event-1",
                    "fact_key": "forecast_owner",
                    "replaced_fact_ref": "id:5",
                    "reason": "The replacement must retain the still-current rule.",
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "findings.json"
            path.write_text(json.dumps(findings_document))
            findings, errors = load_failed_candidate_findings(path, canonical)

        self.assertEqual(errors, [])
        merged = merge_failed_candidate_findings(self._passing_review(canonical), findings)
        self.assertFalse(merged["passed"])
        self.assertFalse(merged["event_reviews"][0]["passed"])
        self.assertFalse(merged["fact_reviews"][0]["passed"])
        self.assertFalse(merged["replacement_reviews"][0]["passed"])

    def test_failed_candidate_findings_reject_invalid_targets(self) -> None:
        story = self._story()
        finalized = self._finalization_response(story)
        canonical, errors = build_canonical_plan(story, finalized)
        self.assertEqual(errors, [])
        assert canonical is not None
        invalid_documents = [
            {},
            {"findings": []},
            {"findings": [{"event_id": "missing", "reason": "No."}]},
            {
                "findings": [
                    {
                        "event_id": "event-1",
                        "fact_key": "missing",
                        "reason": "No.",
                    }
                ]
            },
            {
                "findings": [
                    {
                        "event_id": "event-1",
                        "fact_key": "forecast_owner",
                        "replaced_fact_ref": "id:999",
                        "reason": "No.",
                    }
                ]
            },
            {"findings": [{"event_id": "event-1", "reason": ""}]},
            {"findings": [{"event_id": "event-1", "extra": "No."}]},
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for index, document in enumerate(invalid_documents):
                path = root / f"invalid-{index}.json"
                path.write_text(json.dumps(document))
                _, finding_errors = load_failed_candidate_findings(path, canonical)
                self.assertTrue(finding_errors, document)

    def test_failed_candidate_event_id_rename_updates_references_and_facts(self) -> None:
        story = self._story(second_event=True)
        story["story"]["new_entities"] = [
            {
                "id": "project:forecast",
                "name": "Forecast",
                "kind": "project",
                "role": "The forecast work Morgan assigned.",
                "introduced_in_event_id": "event-1",
            }
        ]
        story["story"]["events"][0]["subjects"].append("project:forecast")
        finalized = self._finalization_response(story)
        canonical, errors = build_canonical_plan(story, finalized)
        self.assertEqual(errors, [])
        assert canonical is not None
        findings_document = {
            "findings": [
                {
                    "event_id": "event-1",
                    "replacement_event_id": "event-1-remains-open",
                    "reason": "The cycle remains open, so the event ID must say that.",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "findings.json"
            path.write_text(json.dumps(findings_document))
            findings, errors = load_failed_candidate_findings(path, canonical)

        self.assertEqual(errors, [])
        renames = approved_event_id_renames(findings)
        self.assertEqual(renames, {"event-1": "event-1-remains-open"})
        repaired_event = copy.deepcopy(story["story"]["events"][0])
        repaired_event["id"] = "event-1-remains-open"
        merged_story, merge_errors = merge_event_repair(
            story_response=story,
            repair_response={"new_entities": [], "events": [repaired_event]},
            failed_event_ids={"event-1"},
            known_entity_ids={"morgan", "project:forecast"},
            approved_event_id_renames=renames,
        )
        self.assertEqual(merge_errors, [])
        assert merged_story is not None
        self.assertEqual(
            [event["id"] for event in merged_story["story"]["events"]],
            ["event-1-remains-open", "event-2"],
        )
        self.assertEqual(
            merged_story["story"]["events"][1]["builds_on_event_ids"],
            ["event-1-remains-open"],
        )
        self.assertEqual(
            merged_story["story"]["new_entities"][0]["introduced_in_event_id"],
            "event-1-remains-open",
        )
        renamed_finalization = rename_event_ids_in_finalization_response(finalized, renames)
        self.assertEqual(
            renamed_finalization["event_fact_annotations"][0]["event_id"],
            "event-1-remains-open",
        )

    def test_event_repair_payload_contains_only_failed_events_and_ancestors(self) -> None:
        story = self._story(second_event=True)
        story["story"]["events"][1]["builds_on_event_ids"] = ["event-1"]
        finalized = self._finalization_response(story)
        canonical, errors = build_canonical_plan(story, finalized)
        self.assertEqual(errors, [])
        assert canonical is not None
        review = {
            "event_reviews": [
                {"event_id": "event-2", "passed": False, "reason": "The dates conflict."}
            ]
        }
        payload = event_repair_payload(
            failed_event_ids={"event-2"},
            story_response=story,
            canonical_plan=canonical,
            current_facts=[
                {"id": 7, "statement": "The forecast exists.", "source_session_ids": []},
                {"id": 8, "statement": "Unrelated.", "source_session_ids": []},
            ],
            quality_review=review,
            known_entities=[{"id": "morgan", "name": "Morgan", "kind": "person"}],
            period_start=date(2024, 4, 1),
            first_event_date=date(2024, 4, 1),
            period_end=date(2024, 6, 30),
        )

        self.assertEqual([event["id"] for event in payload["failed_events"]], ["event-2"])
        self.assertEqual(
            [event["id"] for event in payload["earlier_same_quarter_events"]], ["event-1"]
        )
        self.assertEqual(payload["failed_event_review_reasons"][0]["event_id"], "event-2")
        self.assertEqual(
            [fact["id"] for fact in payload["accepted_current_facts"]], [7, 8]
        )
        self.assertEqual(
            payload["existing_entities"],
            [{"id": "morgan", "name": "Morgan", "kind": "person"}],
        )

    def test_event_repair_replaces_only_requested_events_and_preserves_others(self) -> None:
        story = self._story(second_event=True)
        original = copy.deepcopy(story)
        replacement = copy.deepcopy(story["story"]["events"][0])
        replacement["what_happens"] = "Morgan assigns the forecast to Sarah after a review."
        merged, errors = merge_event_repair(
            story_response=story,
            repair_response={"new_entities": [], "events": [replacement]},
            failed_event_ids={"event-1"},
            known_entity_ids={"morgan"},
        )

        self.assertEqual(errors, [])
        assert merged is not None
        self.assertEqual(
            merged["story"]["events"][1], original["story"]["events"][1]
        )
        self.assertEqual(
            merged["story"]["events"][0]["what_happens"],
            "Morgan assigns the forecast to Sarah after a review.",
        )

    def test_event_repair_sorts_replacement_that_moves_earlier(self) -> None:
        story = self._story(second_event=True)
        story["story"]["events"][0]["start_date"] = "2024-05-10"
        story["story"]["events"][0]["end_date"] = "2024-05-10"
        original_unchanged_event = copy.deepcopy(story["story"]["events"][1])
        replacement = copy.deepcopy(story["story"]["events"][0])
        replacement["start_date"] = "2024-04-10"
        replacement["end_date"] = "2024-04-10"

        merged, errors = merge_event_repair(
            story_response=story,
            repair_response={"new_entities": [], "events": [replacement]},
            failed_event_ids={"event-1"},
            known_entity_ids={"morgan"},
        )

        self.assertEqual(errors, [])
        assert merged is not None
        self.assertEqual(
            [event["id"] for event in merged["story"]["events"]],
            ["event-1", "event-2"],
        )
        self.assertEqual(merged["story"]["events"][1], original_unchanged_event)

    def test_event_repair_rejects_invalid_schema_and_missing_or_extra_ids(self) -> None:
        story = self._story(second_event=True)
        replacement = copy.deepcopy(story["story"]["events"][0])
        replacement["unexpected"] = "not allowed"
        errors = validate_event_repair_response(
            {"new_entities": [], "events": [replacement]}, story, {"event-1"}, known_entity_ids={"morgan"}
        )
        self.assertTrue(any("exactly" in error for error in errors), errors)

        errors = validate_event_repair_response(
            {"new_entities": [], "events": []}, story, {"event-1"}, known_entity_ids={"morgan"}
        )
        self.assertTrue(any("every failed event exactly once" in error for error in errors))

        errors = validate_event_repair_response(
            {"new_entities": [], "events": [copy.deepcopy(story["story"]["events"][1])]},
            story,
            {"event-1"},
            known_entity_ids={"morgan"},
        )
        self.assertTrue(any("only a failed event" in error for error in errors))

    def test_merged_event_repair_is_revalidated_as_a_complete_story(self) -> None:
        story = self._story()
        replacement = copy.deepcopy(story["story"]["events"][0])
        replacement["start_date"] = "2024-07-01"
        replacement["end_date"] = "2024-07-01"
        merged, errors = merge_event_repair(
            story_response=story,
            repair_response={"new_entities": [], "events": [replacement]},
            failed_event_ids={"event-1"},
            known_entity_ids={"morgan"},
        )
        self.assertEqual(errors, [])
        assert merged is not None
        self.assertTrue(
            any(
                "dates must fall between" in error
                for error in validate_story(merged, **self._story_validation_arguments())
            )
        )

    def test_canonical_plan_requires_the_five_active_fields(self) -> None:
        plan = self._finalized_plan(self._story())
        arguments = self._story_validation_arguments()
        arguments["starting_facts"] = [
            {
                "id": 5,
                "construction_key": "old_owner",
                "statement": "Morgan owns the Q2 forecast.",
                "subjects": ["morgan"],
                "supersedes": [],
            },
            {
                "id": 7,
                "construction_key": "forecast_exists",
                "statement": "The Q2 forecast exists.",
                "subjects": ["morgan"],
                "supersedes": [],
            },
        ]

        self.assertEqual(validate_plan(plan, **arguments), [])

        missing = copy.deepcopy(plan)
        missing["plan"].pop("protected_changes")
        errors = validate_plan(missing, **arguments)
        self.assertTrue(any("exactly" in error for error in errors), errors)

        extra = copy.deepcopy(plan)
        extra["plan"]["retired_field"] = []
        errors = validate_plan(extra, **arguments)
        self.assertTrue(
            any("exactly" in error for error in errors),
            errors,
        )

    def test_previous_plan_context_drops_retired_weekly_fields(self) -> None:
        context = canonical_plan_for_context(
            {
                "plan": {
                    "period_start": "2025-10-01",
                    "period_end": "2025-12-31",
                    "weekly_briefs": [{"active_situations": ["retired"]}],
                    "new_entities": [],
                    "events": [],
                }
            }
        )

        self.assertEqual(
            set(context),
            {"period_start", "period_end", "protected_changes", "new_entities", "events"},
        )
        self.assertNotIn("weekly_briefs", context)
        self.assertNotIn("active_situations", context)

    def test_candidate_plan_normalizer_accepts_direct_and_legacy_wrapped_shapes(self) -> None:
        plan = {
            "period_start": "2025-10-01",
            "period_end": "2025-12-31",
            "protected_changes": [],
            "new_entities": [],
            "events": [],
        }
        self.assertEqual(normalize_candidate_plan(plan), {"plan": plan})
        self.assertEqual(normalize_candidate_plan({"plan": plan}), {"plan": plan})

    def test_candidate_plan_normalizer_rejects_missing_or_extra_fields(self) -> None:
        plan = {
            "period_start": "2025-10-01",
            "period_end": "2025-12-31",
            "protected_changes": [],
            "new_entities": [],
            "events": [],
        }
        missing = dict(plan)
        del missing["events"]
        with self.assertRaises(ValueError):
            normalize_candidate_plan(missing)
        with self.assertRaises(ValueError):
            normalize_candidate_plan({**plan, "retired_field": []})

    def test_story_rejects_event_outside_the_requested_period(self) -> None:
        raw_story = self._story()
        raw_story["story"]["events"][0]["start_date"] = "2024-07-01"
        raw_story["story"]["events"][0]["end_date"] = "2024-07-01"

        errors = validate_story(raw_story, **self._story_validation_arguments())
        self.assertTrue(any("dates must fall between" in error for error in errors), errors)

    def test_story_rejects_event_already_covered_by_starting_checkpoint(self) -> None:
        arguments = self._story_validation_arguments()
        arguments["first_event_date"] = date(2024, 6, 1)

        errors = validate_story(self._story(), **arguments)

        self.assertTrue(
            any("2024-06-01 and 2024-06-30" in error for error in errors),
            errors,
        )

    def test_recent_history_uses_month_before_first_new_event(self) -> None:
        history = [
            {
                "id": "march",
                "narrative_date": "2024-03-20T09:00:00-07:00",
                "messages": ["March"],
            },
            {
                "id": "may",
                "narrative_date": "2024-05-20T09:00:00-07:00",
                "messages": ["May"],
            },
        ]

        self.assertEqual(
            previous_calendar_month_sessions(history, date(2024, 6, 1)),
            [
                {
                    "id": "may",
                    "narrative_date": "2024-05-20T09:00:00-07:00",
                    "messages": ["May"],
                }
            ],
        )

    def test_quality_review_requires_each_fact_and_replacement(self) -> None:
        payload = quality_review_payload(
            {"plan": {"new_entities": [], "events": [self._event()]}},
            [
                {"id": 5, "statement": "Old owner", "applies_when": "Before April"},
                {"id": 7, "statement": "Forecast exists", "applies_when": "Always"},
            ],
            replacement_accounting=[],
            primary_subject_ids={"morgan"},
        )
        review = {
            "passed": True,
            "plan_review": {"passed": True, "reason": ""},
            "event_reviews": [{"event_id": "event-1", "passed": True, "reason": ""}],
            "fact_reviews": [
                {
                    "event_id": "event-1",
                    "fact_key": "forecast_owner",
                    "passed": True,
                    "reason": "",
                }
            ],
            "replacement_reviews": [
                {
                    "event_id": "event-1",
                    "fact_key": "forecast_owner",
                    "replaced_fact_ref": "id:5",
                    "passed": True,
                    "reason": "",
                }
            ],
        }

        self.assertEqual(validate_quality_review(review, {"event-1"}, payload), [])
        review["replacement_reviews"] = []
        self.assertTrue(
            any(
                "replacement_reviews" in error
                for error in validate_quality_review(review, {"event-1"}, payload)
            )
        )

    def test_quality_review_requires_the_whole_plan_result(self) -> None:
        response = {"plan": {"new_entities": [], "events": [self._event()]}}
        payload = quality_review_payload(
            response,
            [
                {"id": 5, "statement": "Old owner", "applies_when": "Before April"},
                {"id": 7, "statement": "Forecast exists", "applies_when": "Always"},
            ],
            replacement_accounting=[],
            primary_subject_ids={"morgan"},
        )
        review = self._passing_review(response)
        del review["plan_review"]

        self.assertIn(
            "quality review must contain exactly ['event_reviews', 'fact_reviews', 'passed', 'plan_review', 'replacement_reviews']",
            validate_quality_review(review, {"event-1"}, payload),
        )

    def test_whole_plan_failure_rejects_the_candidate(self) -> None:
        raw_story = self._story()
        finalized = self._finalization_response(raw_story)
        canonical, errors = build_canonical_plan(raw_story, finalized)
        self.assertEqual(errors, [])
        assert canonical is not None
        review = self._passing_review(canonical)
        review["passed"] = False
        review["plan_review"] = {
            "passed": False,
            "reason": "The events repeat the same outcome under different wording.",
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            args, _ = self._run_arguments(Path(temporary_directory))
            responses = [raw_story, finalized, review]

            class OrderedFakeClient:
                def __init__(self, **options: str) -> None:
                    del options

                def complete(
                    self, system: str, payload: dict, *, call_path: Path
                ) -> dict:
                    del system, payload, call_path
                    return copy.deepcopy(responses.pop(0))

            with patch("construction.plan_quarter_events.AzureJsonClient", OrderedFakeClient):
                validation = run(args)

        self.assertFalse(validation["passed"])
        self.assertIn(
            "a failing plan review must identify an event, fact, or replacement that causes the problem",
            validation["errors"],
        )

    def test_run_separates_story_fact_finalization_and_review(self) -> None:
        raw_story = self._story(second_event=True)
        story = raw_story
        finalized = self._finalization_response(story)
        canonical, errors = build_canonical_plan(story, finalized)
        self.assertEqual(errors, [])
        assert canonical is not None
        review = self._passing_review(canonical)
        clients: list[dict[str, str]] = []
        calls: list[tuple[str, str, Path]] = []

        class FakeClient:
            def __init__(self, **options: str) -> None:
                clients.append(options)

            def complete(
                self, system: str, payload: str, *, call_path: Path
            ) -> dict:
                calls.append((system, payload, call_path))
                return copy.deepcopy([raw_story, finalized, review][len(calls) - 1])

        with tempfile.TemporaryDirectory() as temporary_directory:
            args, output = self._run_arguments(Path(temporary_directory))
            with patch("construction.plan_quarter_events.AzureJsonClient", FakeClient):
                validation = run(args)

            self.assertEqual(
                clients, [{"model": "gpt-5.6-sol", "reasoning_effort": "high"}]
            )
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[0][0], QUARTER_EVENT_STORY_SYSTEM)
            self.assertIsInstance(calls[0][1], str)
            self.assertIn("First date for new events: 2024-04-01", calls[0][1])
            self.assertEqual(calls[1][0], QUARTER_EVENT_FACT_FINALIZATION_SYSTEM)
            self.assertEqual(calls[2][0], QUARTER_EVENT_QUALITY_REVIEW_SYSTEM)
            self.assertNotIn(
                QUARTER_EVENT_EVENT_REPAIR_SYSTEM,
                [system for system, _, _ in calls],
            )
            self.assertNotIn("App state at period start", calls[0][1])
            self.assertNotIn("Fact registry with current status", calls[0][1])
            self.assertIn("Current fact registry:", calls[0][1])
            self.assertIn("Current fact ID 5:", calls[0][1])
            self.assertIn("Fixed story:", calls[1][1])
            self.assertIn("Current fact registry:", calls[1][1])
            self.assertEqual(
                [
                    event["id"]
                    for event in json.loads(
                        (output / "quality_review_request.json").read_text()
                    )["proposed_plan"]["events"]
                ],
                ["event-1", "event-2"],
            )
            review_payload = json.loads(
                (output / "quality_review_request.json").read_text()
            )
            self.assertEqual(review_payload["multi_year_overview"], {})
            self.assertEqual(
                review_payload["completed_previous_quarter_plan"],
                {
                    "period_start": None,
                    "period_end": None,
                    "protected_changes": [],
                    "new_entities": [],
                    "events": [],
                },
            )
            self.assertIn("continuity_context", review_payload)
            self.assertEqual(
                [fact["id"] for fact in review_payload["current_fact_registry"]],
                [5, 7],
            )
            self.assertEqual(
                review_payload["replacement_accounting"],
                self._derived_accounting(story, finalized),
            )
            self.assertNotIn("fact_checks", review_payload)
            self.assertNotIn("fixed_story", review_payload)
            self.assertTrue(validation["passed"])
            self.assertEqual(validation["inputs"]["accepted_through"], "2024-03-31")
            self.assertEqual(
                validation["inputs"]["first_date_for_new_events"], "2024-04-01"
            )
            self.assertEqual(
                json.loads((output / "fact_finalization_response.json").read_text()),
                finalized,
            )
            self.assertEqual(
                json.loads((output / "story_response.json").read_text()), raw_story
            )
            self.assertEqual(
                json.loads((output / "candidate_quarter_plan.json").read_text()),
                canonical["plan"],
            )
            accounting = {"replacement_accounting": self._derived_accounting(story, finalized)}
            self.assertEqual(
                json.loads((output / "candidate_replacement_accounting.json").read_text()),
                accounting,
            )
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {
                    "candidate_quarter_plan.json",
                    "candidate_replacement_accounting.json",
                    "consolidated_fact_finalization_response.json",
                    "consolidated_quality_review_response.json",
                    "consolidated_story_response.json",
                    "fact_finalization_request.json",
                    "fact_finalization_response.json",
                    "fact_finalization_system.txt",
                    "fact_finalization_user_input.txt",
                    "quality_review_request.json",
                    "quality_review_response.json",
                    "quality_review_system.txt",
                    "quality_review_user_input.txt",
                    "story_request.json",
                    "story_response.json",
                    "story_system.txt",
                    "story_user_input.txt",
                    "validation.json",
                },
            )

    def test_incomplete_run_reuses_saved_responses_and_calls_only_review(self) -> None:
        raw_story = self._story(second_event=True)
        finalized = self._finalization_response(raw_story)
        canonical, errors = build_canonical_plan(raw_story, finalized)
        self.assertEqual(errors, [])
        assert canonical is not None
        review = self._passing_review(canonical)
        first_calls: list[str] = []

        class InterruptingClient:
            def __init__(self, **_: str) -> None:
                pass

            def complete(
                self, system: str, payload: dict, *, call_path: Path
            ) -> dict:
                del payload, call_path
                first_calls.append(system)
                if len(first_calls) == 1:
                    return copy.deepcopy(raw_story)
                if len(first_calls) == 2:
                    return copy.deepcopy(finalized)
                raise RuntimeError("stop before quality review response")

        with tempfile.TemporaryDirectory() as temporary_directory:
            args, output = self._run_arguments(Path(temporary_directory))
            with patch("construction.plan_quarter_events.AzureJsonClient", InterruptingClient):
                with self.assertRaisesRegex(RuntimeError, "stop before quality review"):
                    run(args)

            (output / "quality_review_request.json").write_text(
                json.dumps({"old": "unexecuted request"}, indent=2) + "\n"
            )
            (output / "quality_review_system.txt").write_text("old unexecuted prompt")
            resumed_calls: list[str] = []
            resumed_payloads: list[str] = []

            class ResumeClient:
                def __init__(self, **_: str) -> None:
                    pass

                def complete(
                    self, system: str, payload: str, *, call_path: Path
                ) -> dict:
                    del call_path
                    resumed_calls.append(system)
                    resumed_payloads.append(copy.deepcopy(payload))
                    return copy.deepcopy(review)

            with patch("construction.plan_quarter_events.AzureJsonClient", ResumeClient):
                validation = run(args)

            self.assertTrue(validation["passed"], validation["errors"])
            self.assertEqual(first_calls, [
                QUARTER_EVENT_STORY_SYSTEM,
                QUARTER_EVENT_FACT_FINALIZATION_SYSTEM,
                QUARTER_EVENT_QUALITY_REVIEW_SYSTEM,
            ])
            self.assertEqual(resumed_calls, [QUARTER_EVENT_QUALITY_REVIEW_SYSTEM])
            self.assertEqual(validation["model_calls"], 1)
            self.assertEqual(
                (output / "quality_review_user_input.txt").read_text(),
                resumed_payloads[0],
            )
            self.assertEqual(
                (output / "quality_review_system.txt").read_text(),
                QUARTER_EVENT_QUALITY_REVIEW_SYSTEM,
            )
            self.assertEqual(
                json.loads((output / "story_response.json").read_text()), raw_story
            )
            self.assertEqual(
                json.loads((output / "fact_finalization_response.json").read_text()),
                finalized,
            )

    def test_incomplete_run_rejects_mismatched_saved_request(self) -> None:
        raw_story = self._story()
        finalized = self._finalization_response(raw_story)
        calls: list[str] = []

        class InterruptingClient:
            def __init__(self, **_: str) -> None:
                pass

            def complete(
                self, system: str, payload: dict, *, call_path: Path
            ) -> dict:
                del payload, call_path
                calls.append(system)
                if len(calls) == 1:
                    return copy.deepcopy(raw_story)
                if len(calls) == 2:
                    return copy.deepcopy(finalized)
                raise RuntimeError("stop before quality review response")

        with tempfile.TemporaryDirectory() as temporary_directory:
            args, output = self._run_arguments(Path(temporary_directory))
            with patch("construction.plan_quarter_events.AzureJsonClient", InterruptingClient):
                with self.assertRaises(RuntimeError):
                    run(args)

            saved_request = json.loads((output / "story_request.json").read_text())
            saved_request["period_start"] = "2024-04-02"
            (output / "story_request.json").write_text(
                json.dumps(saved_request, indent=2) + "\n"
            )
            resumed_calls: list[str] = []

            class NoCallClient:
                def __init__(self, **_: str) -> None:
                    pass

                def complete(
                    self, system: str, payload: dict, *, call_path: Path
                ) -> dict:
                    del system, payload, call_path
                    resumed_calls.append("called")
                    raise AssertionError("mismatched input must fail before a call")

            with patch("construction.plan_quarter_events.AzureJsonClient", NoCallClient):
                with self.assertRaisesRegex(ValueError, "story: saved request"):
                    run(args)
            self.assertEqual(resumed_calls, [])

    def test_run_plans_only_uncovered_part_of_quarter(self) -> None:
        raw_story = self._story()
        raw_story["story"]["events"][0]["start_date"] = "2024-06-10"
        raw_story["story"]["events"][0]["end_date"] = "2024-06-10"
        finalized = self._finalization_response(raw_story)
        canonical, errors = build_canonical_plan(raw_story, finalized)
        self.assertEqual(errors, [])
        assert canonical is not None
        review = self._passing_review(canonical)
        responses = [raw_story, finalized, review]
        calls: list[tuple[str, str]] = []

        class FakeClient:
            def __init__(self, **_: str) -> None:
                pass

            def complete(
                self, system: str, payload: str, *, call_path: Path
            ) -> dict:
                calls.append((system, payload))
                return copy.deepcopy(responses[len(calls) - 1])

        history = [
            {
                "id": "march",
                "narrative_date": "2024-03-20T09:00:00-07:00",
                "messages": ["March accepted history."],
            },
            {
                "id": "may",
                "narrative_date": "2024-05-20T09:00:00-07:00",
                "messages": ["May accepted history."],
            },
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            args, _ = self._run_arguments(
                Path(temporary_directory),
                accepted_through="2024-05-31",
                history=history,
            )
            with patch("construction.plan_quarter_events.AzureJsonClient", FakeClient):
                validation = run(args)

        self.assertTrue(validation["passed"], validation["errors"])
        self.assertIn("Period start: 2024-04-01", calls[0][1])
        self.assertIn("Period end: 2024-06-30", calls[0][1])
        self.assertIn("First date for new events: 2024-06-01", calls[0][1])
        self.assertIn("Id: may", calls[0][1])
        self.assertEqual(validation["inputs"]["accepted_through"], "2024-05-31")
        self.assertEqual(
            validation["inputs"]["first_date_for_new_events"], "2024-06-01"
        )

    def test_failed_fact_rows_are_repaired_once_and_whole_plan_is_reviewed_again(
        self,
    ) -> None:
        raw_story = self._story(second_event=True)
        story = self._story(second_event=True)
        finalized = self._finalization_response(story)
        original_canonical, errors = build_canonical_plan(story, finalized)
        self.assertEqual(errors, [])
        assert original_canonical is not None

        failed_review = self._passing_review(original_canonical)
        failed_review["passed"] = False
        failed_review["fact_reviews"][0].update(
            passed=False, reason="Remove the unsupported detail."
        )
        failed_review["replacement_reviews"][0].update(
            passed=False, reason="Preserve every still-current clause from the old fact."
        )
        repair_response = {
            "event_fact_annotations": [
                {
                    "event_id": "event-1",
                    "relevant_existing_fact_ids": [7],
                    "facts_to_establish": copy.deepcopy(
                        finalized["event_fact_annotations"][0]["facts_to_establish"]
                    ),
                }
            ],
            "fact_replacements": copy.deepcopy(finalized["fact_replacements"]),
        }
        merged_repair_response, errors = merge_fact_repair(
            story_response=story,
            original_response=finalized,
            repair_response=repair_response,
            failed_event_ids={"event-1"},
        )
        self.assertEqual(errors, [])
        assert merged_repair_response is not None
        repaired_canonical, errors = build_canonical_plan(
            story, merged_repair_response
        )
        self.assertEqual(errors, [])
        assert repaired_canonical is not None
        final_review = self._passing_review(repaired_canonical)
        responses = [
            raw_story,
            finalized,
            failed_review,
            repair_response,
            final_review,
        ]
        calls: list[tuple[str, str]] = []

        class FakeClient:
            def __init__(self, **_: str) -> None:
                pass

            def complete(
                self, system: str, payload: str, *, call_path: Path
            ) -> dict:
                calls.append((system, payload))
                return copy.deepcopy(responses.pop(0))

        with tempfile.TemporaryDirectory() as temporary_directory:
            args, output = self._run_arguments(Path(temporary_directory))
            with patch("construction.plan_quarter_events.AzureJsonClient", FakeClient):
                validation = run(args)

            self.assertTrue(validation["passed"], validation["errors"])
            self.assertEqual(validation["model_calls"], 5)
            self.assertTrue(validation["fact_repair_attempted"])
            self.assertEqual(validation["initial_quality_review"], failed_review)
            self.assertEqual(
                [system for system, _ in calls],
                [
                    QUARTER_EVENT_STORY_SYSTEM,
                    QUARTER_EVENT_FACT_FINALIZATION_SYSTEM,
                    QUARTER_EVENT_QUALITY_REVIEW_SYSTEM,
                    QUARTER_EVENT_FACT_REPAIR_SYSTEM,
                    QUARTER_EVENT_QUALITY_REVIEW_SYSTEM,
                ],
            )
            self.assertTrue(all(isinstance(payload, str) for _, payload in calls))
            repair_payload = json.loads((output / "fact_repair_request.json").read_text())
            self.assertEqual(
                [event["id"] for event in repair_payload["fixed_story_events"]],
                ["event-1"],
            )
            self.assertEqual(
                [row["event_id"] for row in repair_payload["event_fact_annotations_to_repair"]],
                ["event-1"],
            )
            self.assertEqual(
                [
                    fact["id"]
                    for fact in repair_payload[
                        "exact_starting_facts_used_or_replaced"
                    ]
                ],
                [5, 7],
            )
            self.assertEqual(
                [
                    event["id"]
                    for event in json.loads(
                        (output / "repair_review" / "quality_review_request.json").read_text()
                    )["proposed_plan"]["events"]
                ],
                ["event-1"],
            )
            self.assertEqual(
                json.loads((output / "quality_review_request.json").read_text())[
                    "replacement_accounting"
                ],
                self._derived_accounting(story, finalized),
            )
            self.assertEqual(
                json.loads(
                    (output / "repair_review" / "quality_review_request.json").read_text()
                )["replacement_accounting"],
                self._derived_accounting(story, merged_repair_response),
            )
            self.assertTrue((output / "fact_repair_response.json").is_file())
            self.assertTrue(
                (output / "repair_review" / "quality_review_response.json").is_file()
            )
            self.assertTrue((output / "candidate_quarter_plan.json").is_file())
            self.assertFalse((output / "accepted_quarter_plan.json").exists())

    def test_failed_event_is_repaired_before_fact_repair_for_the_same_union(self) -> None:
        raw_story = self._story(second_event=True)
        story = self._story(second_event=True)
        finalized = self._finalization_response(story)
        original_canonical, errors = build_canonical_plan(story, finalized)
        self.assertEqual(errors, [])
        assert original_canonical is not None

        failed_review = self._passing_review(original_canonical)
        failed_review["passed"] = False
        failed_review["event_reviews"][0].update(
            passed=False, reason="The dates and move description conflict."
        )
        repaired_story = copy.deepcopy(story)
        repaired_story["story"]["events"][0]["what_happens"] = (
            "Morgan assigns the forecast to Sarah after the review."
        )
        event_repair_response = {
            "new_entities": [],
            "events": [repaired_story["story"]["events"][0]],
        }
        fact_repair_response = {
            "event_fact_annotations": [
                {
                    "event_id": "event-1",
                    "relevant_existing_fact_ids": [7],
                    "facts_to_establish": copy.deepcopy(
                        finalized["event_fact_annotations"][0]["facts_to_establish"]
                    ),
                }
            ],
            "fact_replacements": copy.deepcopy(finalized["fact_replacements"]),
        }
        merged_fact_response, errors = merge_fact_repair(
            story_response=repaired_story,
            original_response=finalized,
            repair_response=fact_repair_response,
            failed_event_ids={"event-1"},
        )
        self.assertEqual(errors, [])
        assert merged_fact_response is not None
        repaired_canonical, errors = build_canonical_plan(
            repaired_story, merged_fact_response
        )
        self.assertEqual(errors, [])
        assert repaired_canonical is not None
        final_review = self._passing_review(repaired_canonical)
        responses = [
            raw_story,
            finalized,
            failed_review,
            event_repair_response,
            fact_repair_response,
            final_review,
        ]
        calls: list[tuple[str, str, Path]] = []

        class FakeClient:
            def __init__(self, **_: str) -> None:
                pass

            def complete(
                self, system: str, payload: str, *, call_path: Path
            ) -> dict:
                calls.append((system, payload, call_path))
                return copy.deepcopy(responses.pop(0))

        with tempfile.TemporaryDirectory() as temporary_directory:
            args, output = self._run_arguments(Path(temporary_directory))
            with patch("construction.plan_quarter_events.AzureJsonClient", FakeClient):
                validation = run(args)
            event_repair_files_exist = (
                (output / "event_repair_request.json").is_file()
                and (output / "event_repair" / "fact_repair_response.json").is_file()
                and (
                    output
                    / "event_repair"
                    / "repair_review"
                    / "quality_review_response.json"
                ).is_file()
            )

        self.assertTrue(validation["passed"], validation["errors"])
        self.assertEqual(validation["model_calls"], 6)
        self.assertEqual(
            [system for system, _, _ in calls],
            [
                QUARTER_EVENT_STORY_SYSTEM,
                QUARTER_EVENT_FACT_FINALIZATION_SYSTEM,
                QUARTER_EVENT_QUALITY_REVIEW_SYSTEM,
                QUARTER_EVENT_EVENT_REPAIR_SYSTEM,
                QUARTER_EVENT_FACT_REPAIR_SYSTEM,
                QUARTER_EVENT_QUALITY_REVIEW_SYSTEM,
            ],
        )
        self.assertTrue(all(isinstance(payload, str) for _, payload, _ in calls))
        self.assertIn("Failed events:", calls[3][1])
        self.assertIn("Id: event-1", calls[3][1])
        self.assertIn("Fixed story events:", calls[4][1])
        self.assertIn("Morgan assigns the forecast to Sarah after the review.", calls[4][1])
        self.assertTrue(event_repair_files_exist)

    def test_failed_review_resumes_with_one_final_targeted_correction(self) -> None:
        raw_story = self._story()
        story = self._story()
        finalized = self._finalization_response(story)
        canonical, errors = build_canonical_plan(story, finalized)
        self.assertEqual(errors, [])
        assert canonical is not None
        failed_review = self._passing_review(canonical)
        failed_review["passed"] = False
        failed_review["fact_reviews"][0].update(
            passed=False, reason="The fact still contains unsupported information."
        )
        responses = [
            raw_story,
            finalized,
            failed_review,
            {
                "event_fact_annotations": [
                    {
                        "event_id": "event-1",
                        "relevant_existing_fact_ids": [7],
                        "facts_to_establish": copy.deepcopy(
                            finalized["event_fact_annotations"][0]["facts_to_establish"]
                        ),
                    }
                ],
                "fact_replacements": copy.deepcopy(finalized["fact_replacements"]),
            },
            copy.deepcopy(failed_review),
        ]
        calls: list[str] = []

        class FakeClient:
            def __init__(self, **_: str) -> None:
                pass

            def complete(
                self, system: str, payload: dict, *, call_path: Path
            ) -> dict:
                calls.append(system)
                return copy.deepcopy(responses.pop(0))

        with tempfile.TemporaryDirectory() as temporary_directory:
            args, output = self._run_arguments(Path(temporary_directory))
            with patch("construction.plan_quarter_events.AzureJsonClient", FakeClient):
                validation = run(args)

            self.assertFalse(validation["passed"])
            self.assertEqual(validation["model_calls"], 5)
            self.assertEqual(calls.count(QUARTER_EVENT_FACT_REPAIR_SYSTEM), 1)
            self.assertTrue(
                any("quality review after fact repair" in error for error in validation["errors"]),
                validation["errors"],
            )
            self.assertFalse((output / "accepted_quarter_plan.json").exists())

            calls.clear()
            responses.extend(
                [
                    {
                        "event_fact_annotations": [
                            {
                                "event_id": "event-1",
                                "relevant_existing_fact_ids": [7],
                                "facts_to_establish": [
                                    {
                                        **copy.deepcopy(
                                            finalized["event_fact_annotations"][0][
                                                "facts_to_establish"
                                            ][0]
                                        ),
                                        "applies_when": "After the corrected April 10 assignment",
                                    }
                                ],
                            }
                        ],
                        "fact_replacements": copy.deepcopy(
                            finalized["fact_replacements"]
                        ),
                    },
                    {
                        "passed": True,
                        "plan_review": {"passed": True, "reason": ""},
                        "event_reviews": [],
                        "fact_reviews": [
                            {
                                "event_id": "event-1",
                                "fact_key": "forecast_owner",
                                "passed": True,
                                "reason": "",
                            }
                        ],
                        "replacement_reviews": [
                            {
                                "event_id": "event-1",
                                "fact_key": "forecast_owner",
                                "replaced_fact_ref": "id:5",
                                "passed": True,
                                "reason": "",
                            }
                        ],
                    },
                ]
            )
            with patch("construction.plan_quarter_events.AzureJsonClient", FakeClient):
                validation = run(args)

            self.assertTrue(validation["passed"], validation["errors"])
            self.assertEqual(validation["model_calls"], 2)
            self.assertEqual(
                calls,
                [
                    QUARTER_EVENT_FACT_REPAIR_SYSTEM,
                    QUARTER_EVENT_QUALITY_REVIEW_SYSTEM,
                ],
            )
            self.assertTrue(
                (output / "final_repair" / "fact_repair_response.json").is_file()
            )
            self.assertFalse((output / "final_repair" / "event_repair_response.json").exists())

    def test_failed_candidate_correction_uses_initial_failed_event_for_stale_plan_failure(
        self,
    ) -> None:
        story = self._story(second_event=True)
        finalized = self._finalization_response(story)
        canonical, errors = build_canonical_plan(story, finalized)
        self.assertEqual(errors, [])
        assert canonical is not None
        accounting = derive_replacement_accounting(finalized, canonical)

        saved_review = self._passing_review(canonical)
        saved_review["passed"] = False
        saved_review["plan_review"] = {
            "passed": False,
            "reason": "The original event needed correction.",
        }
        initial_review = self._passing_review(canonical)
        initial_review["passed"] = False
        initial_review["event_reviews"][0].update(
            passed=False, reason="The event conflicts with the accepted history."
        )

        event_repair_response = {
            "new_entities": [],
            "events": [copy.deepcopy(story["story"]["events"][0])],
        }
        event_repair_response["events"][0]["what_happens"] = (
            "Morgan assigns the forecast to Sarah and keeps the Friday update."
        )
        responses = [
            event_repair_response,
            copy.deepcopy(finalized),
            {
                "passed": True,
                "plan_review": {"passed": True, "reason": ""},
                "event_reviews": [
                    {"event_id": "event-1", "passed": True, "reason": ""}
                ],
                "fact_reviews": [],
                "replacement_reviews": [],
            },
        ]
        calls: list[tuple[str, dict]] = []

        class FakeClient:
            def complete(
                self, system: str, payload: dict, *, call_path: Path
            ) -> dict:
                del call_path
                calls.append((system, payload))
                return copy.deepcopy(responses.pop(0))

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "failed-candidate"
            output = root / "correction"
            source.mkdir()
            for filename, document in (
                ("candidate_quarter_plan.json", canonical["plan"]),
                ("consolidated_story_response.json", story),
                ("consolidated_fact_finalization_response.json", finalized),
                (
                    "candidate_replacement_accounting.json",
                    {"replacement_accounting": accounting},
                ),
                (
                    "validation.json",
                    {
                        "passed": False,
                        "quality_review": saved_review,
                        "initial_quality_review": initial_review,
                    },
                ),
            ):
                (source / filename).write_text(json.dumps(document, indent=2) + "\n")
            findings_path = root / "approved-findings.json"
            findings_path.write_text(
                json.dumps(
                    {
                        "findings": [
                            {
                                "event_id": "event-1",
                                "fact_key": "forecast_owner",
                                "replaced_fact_ref": "id:5",
                                "reason": "Keep the Friday update in the replacement record.",
                            }
                        ]
                    },
                    indent=2,
                )
                + "\n"
            )

            validation = run_failed_candidate_correction(
                client=FakeClient(),
                output_dir=output,
                source_candidate_dir=source,
                approved_findings_path=findings_path,
                starting_facts=[
                    {
                        "id": 5,
                        "construction_key": "old_owner",
                        "statement": "Morgan owns the forecast.",
                        "subjects": ["morgan"],
                    },
                    {
                        "id": 7,
                        "construction_key": "forecast_exists",
                        "statement": "The forecast exists.",
                        "subjects": ["morgan"],
                    },
                ],
                current_facts=[
                    {"id": 5, "statement": "Morgan owns the forecast."},
                    {"id": 7, "statement": "The forecast exists."},
                ],
                entities=[{"id": "morgan", "name": "Morgan", "kind": "person"}],
                primary_subject_ids={"morgan"},
                overview={},
                previous_plan={"events": []},
                recent_session_purposes=[],
                recent_history=[],
                current_ordinary_states=[],
                period_start=date(2024, 4, 1),
                first_event_date=date(2024, 4, 1),
                period_end=date(2024, 6, 30),
                checkpoint_identity="checkpoint",
            )
            self.assertTrue((output / "approved_correction_findings.json").is_file())
            saved_findings_sha256 = json.loads(
                (output / "failed_candidate_source.json").read_text()
            )["approved_findings"]["sha256"]

        self.assertTrue(validation["passed"], validation["errors"])
        self.assertEqual(validation["model_calls"], 3)
        self.assertEqual(
            [system for system, _ in calls],
            [
                QUARTER_EVENT_EVENT_REPAIR_SYSTEM,
                QUARTER_EVENT_FACT_REPAIR_SYSTEM,
                QUARTER_EVENT_QUALITY_REVIEW_SYSTEM,
            ],
        )
        self.assertIn("Failed events:", calls[0][1])
        self.assertIn("Id: event-1", calls[0][1])
        self.assertIn("Fixed story events:", calls[1][1])
        self.assertIn("Keep the Friday update", calls[1][1])
        self.assertIn("Event event-1", calls[2][1])
        self.assertNotIn("Event event-2", calls[2][1])
        self.assertEqual(validation["initial_quality_review"], saved_review)
        self.assertEqual(
            validation["inputs"]["approved_findings_sha256"],
            saved_findings_sha256,
        )

    def test_invalid_story_stops_before_fact_finalization_or_review(self) -> None:
        story = {
            "story": {
                "period_start": "2024-01-01",
                "period_end": "2024-06-30",
                "new_entities": [],
                "events": [],
            }
        }
        calls: list[str] = []

        class FakeClient:
            def __init__(self, **_: str) -> None:
                pass

            def complete(
                self, system: str, payload: dict, *, call_path: Path
            ) -> dict:
                calls.append(system)
                return copy.deepcopy(story)

        with tempfile.TemporaryDirectory() as temporary_directory:
            args, output = self._run_arguments(Path(temporary_directory))
            with patch("construction.plan_quarter_events.AzureJsonClient", FakeClient):
                validation = run(args)

            self.assertEqual(calls, [QUARTER_EVENT_STORY_SYSTEM])
            self.assertFalse(validation["passed"])
            self.assertFalse((output / "fact_finalization_request.json").exists())
            self.assertFalse((output / "quality_review_request.json").exists())
            self.assertFalse((output / "accepted_quarter_plan.json").exists())

    def test_omitted_annotation_keeps_fixed_event_with_empty_lists(self) -> None:
        story = self._story(second_event=True)
        finalized = self._finalization_response(story)
        canonical, errors = build_canonical_plan(story, finalized)
        self.assertEqual(errors, [])
        self.assertEqual(
            canonical["plan"]["events"][1]["id"], "event-2"
        )
        self.assertEqual(
            canonical["plan"]["events"][1]["what_happens"],
            story["story"]["events"][1]["what_happens"],
        )
        self.assertEqual(canonical["plan"]["events"][1]["relevant_existing_fact_ids"], [])
        self.assertEqual(canonical["plan"]["events"][1]["facts_to_establish"], [])

    def test_unknown_and_duplicate_annotation_ids_fail(self) -> None:
        story = self._story(second_event=True)
        response = self._finalization_response(story)
        response["event_fact_annotations"].append(
            copy.deepcopy(response["event_fact_annotations"][0])
        )
        response["event_fact_annotations"][-1]["event_id"] = "missing"
        errors = validate_event_fact_annotations(response, story)
        self.assertTrue(any("unknown event" in error for error in errors))
        response["event_fact_annotations"][-1]["event_id"] = "event-1"
        errors = validate_event_fact_annotations(response, story)
        self.assertTrue(any("duplicate event" in error for error in errors))

    def test_finalizer_raw_response_has_no_plan_and_canonical_files_are_plan_only(self) -> None:
        story = self._story()
        finalized = self._finalization_response(story)
        canonical, errors = build_canonical_plan(story, finalized)
        self.assertEqual(errors, [])
        self.assertNotIn("plan", finalized)
        self.assertEqual(set(finalized), {"event_fact_annotations", "fact_replacements"})
        self.assertEqual(set(canonical), {"plan"})
        self.assertEqual(
            set(canonical["plan"]),
            {"period_start", "period_end", "new_entities", "protected_changes", "events"},
        )

    def test_run_uses_built_plan_for_review_and_artifacts(self) -> None:
        raw_story = self._story()
        story = self._story()
        finalized = self._finalization_response(story)
        canonical, errors = build_canonical_plan(story, finalized)
        self.assertEqual(errors, [])
        review = self._passing_review(canonical)
        responses = [raw_story, finalized, review]

        class FakeClient:
            def __init__(self, **_: str) -> None:
                pass

            def complete(
                self, system: str, payload: dict, *, call_path: Path
            ) -> dict:
                return copy.deepcopy(responses.pop(0))

        with tempfile.TemporaryDirectory() as temporary_directory:
            args, _ = self._run_arguments(Path(temporary_directory))
            with patch("construction.plan_quarter_events.AzureJsonClient", FakeClient):
                validation = run(args)

        self.assertTrue(validation["passed"])

    def test_passing_run_writes_only_candidate_outputs(self) -> None:
        raw_story = self._story()
        story = self._story()
        finalized = self._finalization_response(story)
        canonical, errors = build_canonical_plan(story, finalized)
        self.assertEqual(errors, [])
        responses = [raw_story, finalized, self._passing_review(canonical)]

        class FakeClient:
            def __init__(self, **_: str) -> None:
                pass

            def complete(
                self, system: str, payload: dict, *, call_path: Path
            ) -> dict:
                return copy.deepcopy(responses.pop(0))

        with tempfile.TemporaryDirectory() as temporary_directory:
            args, output = self._run_arguments(Path(temporary_directory))
            with patch("construction.plan_quarter_events.AzureJsonClient", FakeClient):
                validation = run(args)

            self.assertTrue(validation["passed"])
            self.assertTrue((output / "candidate_quarter_plan.json").is_file())
            self.assertTrue((output / "candidate_replacement_accounting.json").is_file())
            self.assertFalse((output / "accepted_quarter_plan.json").exists())
            self.assertFalse((output / "accepted_replacement_accounting.json").exists())

    def test_joint_replacement_accounting_keeps_the_explicit_owner_and_allows_siblings(
        self,
    ) -> None:
        """One event may split one retired fact into several replacement facts."""
        story = self._story()
        response = self._finalization_response(story)
        response["event_fact_annotations"][0]["facts_to_establish"].append(
            {
                "key": "friday_update_requirement",
                "statement": "The Friday update remains required.",
                "applies_when": "When the forecast is updated.",
                "subjects": ["morgan"],
            }
        )
        response["fact_replacements"][0]["replacement_fact_numbers"] = [1, 2]
        canonical, errors = build_canonical_plan(story, response)
        self.assertEqual(errors, [])
        assert canonical is not None
        self.assertEqual(
            [fact["supersedes"] for fact in canonical["plan"]["events"][0]["facts_to_establish"]],
            [[5], [5]],
        )
        accounting = derive_replacement_accounting(response, canonical)
        self.assertEqual(
            accounting,
            [
                {
                    "event_id": "event-1",
                    "replaced_fact_ref": "id:5",
                    "replacement_fact_keys": [
                        "forecast_owner",
                        "friday_update_requirement",
                    ],
                    "still_current_information": [
                        "The Friday update remains required."
                    ],
                    "changed_or_ended_information": [
                        "Morgan no longer owns the forecast; Sarah owns it."
                    ],
                }
            ],
        )
        self.assertEqual(validate_replacement_accounting(accounting, canonical_plan=canonical), [])
        return

    @staticmethod
    def _add_other_event_key_to_accounting(response: dict) -> None:
        response["event_fact_annotations"].append(
            {
                "event_id": "event-2",
                "relevant_existing_fact_ids": [],
                "facts_to_establish": [
                    {
                        "key": "other_event_fact",
                        "statement": "A different event establishes this fact.",
                        "applies_when": "After the later event.",
                        "subjects": ["morgan"],
                        "supersedes": [],
                        "supersedes_fact_keys": [],
                    }
                ],
            }
        )
        response["replacement_accounting"][0]["replacement_fact_keys"] = [
            "forecast_owner",
            "other_event_fact",
        ]

    def test_finalization_accounting_must_cover_each_replaced_fact_once(self) -> None:
        story = self._story(second_event=True)
        response = self._finalization_response(story)
        response["event_fact_annotations"].append(
            {
                "event_id": "event-2",
                "relevant_existing_fact_ids": [],
                "facts_to_establish": [
                    {
                        "key": "later_forecast_owner",
                        "statement": "Sarah remains the forecast owner.",
                        "applies_when": "After May 10",
                        "subjects": ["morgan"],
                    }
                ],
            }
        )
        cases = {
            "bad source shape": lambda value: value["fact_replacements"][0].update(
                replaced_fact={"source": "starting_checkpoint", "fact_id": 5, "extra": True}
            ),
            "unknown event": lambda value: value["fact_replacements"][0].update(event_id="missing"),
            "forward event": lambda value: value["fact_replacements"][0].update(
                event_id="event-1",
                replaced_fact={"source": "this_quarter", "event_id": "event-2", "fact_number": 1},
            ),
            "zero index": lambda value: value["fact_replacements"][0].update(
                event_id="event-2",
                replaced_fact={"source": "this_quarter", "event_id": "event-1", "fact_number": 0},
            ),
            "source index range": lambda value: value["fact_replacements"][0].update(
                event_id="event-2",
                replaced_fact={"source": "this_quarter", "event_id": "event-1", "fact_number": 2},
            ),
            "replacement index range": lambda value: value["fact_replacements"][0].update(replacement_fact_numbers=[2]),
            "duplicate row": lambda value: value["fact_replacements"].append(
                copy.deepcopy(value["fact_replacements"][0])
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                candidate = copy.deepcopy(response)
                mutate(candidate)
                self.assertTrue(validate_fact_replacements(candidate, story), label)
        response["event_fact_annotations"][0]["facts_to_establish"].append(
            {
                "key": "second_replacement_fact",
                "statement": "The forecast update remains required.",
                "applies_when": "After April 10",
                "subjects": ["morgan"],
            }
        )
        response["fact_replacements"][0]["replacement_fact_numbers"] = [1, 1]
        self.assertTrue(validate_fact_replacements(response, story))
        return

    def test_replacement_accounting_omission_fails(self) -> None:
        story = self._story()
        response = self._finalization_response(story)
        response["event_fact_annotations"][0]["facts_to_establish"][0]["statement"] = "Sarah owns the forecast."
        canonical, errors = build_canonical_plan(story, response)
        self.assertEqual(errors, [])
        assert canonical is not None
        self.assertTrue(
            any(
                "must be copied from a named replacement fact statement" in error
                for error in validate_replacement_accounting(
                    derive_replacement_accounting(response, canonical), canonical_plan=canonical
                )
            )
        )
        return

    def test_replacement_accounting_exact_carried_text_passes(self) -> None:
        story = self._story()
        response = self._finalization_response(story)
        canonical, errors = build_canonical_plan(story, response)
        self.assertEqual(errors, [])
        assert canonical is not None
        self.assertEqual(
            validate_replacement_accounting(
                derive_replacement_accounting(response, canonical), canonical_plan=canonical
            ),
            [],
        )
        return

    def test_replacement_accounting_whitespace_normalization_passes(self) -> None:
        story = self._story()
        response = self._finalization_response(story)
        response["event_fact_annotations"][0]["facts_to_establish"][0]["statement"] = (
            "Sarah owns the forecast.\nThe Friday   update remains required."
        )
        response["fact_replacements"][0]["still_current_information"] = [
            "The Friday\nupdate remains   required."
        ]
        canonical, errors = build_canonical_plan(story, response)
        self.assertEqual(errors, [])
        assert canonical is not None
        self.assertEqual(
            validate_replacement_accounting(
                derive_replacement_accounting(response, canonical), canonical_plan=canonical
            ),
            [],
        )
        return

    def test_fact_subjects_may_extend_beyond_event_but_must_be_known(self) -> None:
        response = {
            "plan": {
                "period_start": "2024-04-01",
                "period_end": "2024-06-30",
                "new_entities": [],
                "protected_changes": [],
                "events": [self._event()],
            }
        }
        fact = response["plan"]["events"][0]["facts_to_establish"][0]
        fact["subjects"] = [
            "morgan",
            "person:jake",
        ]
        fact["supersedes"] = []
        arguments = {
            "period_start": date(2024, 4, 1),
            "period_end": date(2024, 6, 30),
            "starting_entities": [
                {"id": "morgan"},
                {"id": "person:jake"},
                {"id": "person:priya"},
            ],
            "starting_facts": [
                {
                    "id": 5,
                    "construction_key": "old_owner",
                    "subjects": ["person:jake"],
                },
                {"id": 7, "construction_key": "forecast_exists"},
            ],
            "overview": {},
            "previous_plan": {"events": []},
        }

        self.assertEqual(validate_plan(response, **arguments), [])
        fact["subjects"].append("person:unknown")
        errors = validate_plan(response, **arguments)
        self.assertTrue(
            any("fact has unknown subjects" in error for error in errors),
            errors,
        )

    def test_preserved_information_may_reach_a_joint_sibling_replacement(self) -> None:
        story = self._story(second_event=True)
        response = self._finalization_response(story)
        response["event_fact_annotations"].append(
            {
                "event_id": "event-2",
                "relevant_existing_fact_ids": [],
                "facts_to_establish": [
                    {
                        "key": "later_forecast_rule",
                        "statement": "Sarah owns the forecast after the May review.",
                        "applies_when": "After May 10",
                        "subjects": ["morgan"],
                    }
                ],
            }
        )
        response["fact_replacements"].extend(
            [
                {
                    "event_id": "event-2",
                    "replaced_fact": {
                        "source": "this_quarter", "event_id": "event-1", "fact_number": 1
                    },
                    "replacement_fact_numbers": [1],
                    "still_current_information": [],
                    "changed_or_ended_information": ["The April assignment is replaced after the May review."],
                },
                {
                    "event_id": "event-2",
                    "replaced_fact": {"source": "starting_checkpoint", "fact_id": 7},
                    "replacement_fact_numbers": [1],
                    "still_current_information": [],
                    "changed_or_ended_information": ["The earlier forecast record is replaced after the May review."],
                },
            ]
        )
        canonical, errors = build_canonical_plan(story, response)
        self.assertEqual(errors, [])
        assert canonical is not None
        later_fact = canonical["plan"]["events"][1]["facts_to_establish"][0]
        self.assertEqual(later_fact["supersedes"], [7])
        self.assertEqual(later_fact["supersedes_fact_keys"], ["forecast_owner"])
        self.assertEqual(
            [row["replaced_fact_ref"] for row in derive_replacement_accounting(response, canonical)],
            ["id:5", "key:forecast_owner", "id:7"],
        )
        return

    def test_riley_aug18_reference_resolves_without_repeating_the_key(self) -> None:
        story = self._story(second_event=True)
        story["story"]["events"][0]["id"] = "aug18_helio_start_beta"
        story["story"]["events"][1]["id"] = "sep24_helio_followup"
        response = {
            "event_fact_annotations": [
                {
                    "event_id": "aug18_helio_start_beta",
                    "relevant_existing_fact_ids": [],
                    "facts_to_establish": [
                        {
                            "key": "aug18_helio_start_beta_capped_support_metric",
                            "statement": "Helio beta support remains capped at the agreed metric.",
                            "applies_when": "During the Helio beta.",
                            "subjects": ["morgan"],
                        }
                    ],
                },
                {
                    "event_id": "sep24_helio_followup",
                    "relevant_existing_fact_ids": [],
                    "facts_to_establish": [
                        {
                            "key": "sep24_helio_support_metric",
                            "statement": "Helio support uses the September metric.",
                            "applies_when": "After September 24.",
                            "subjects": ["morgan"],
                        }
                    ],
                },
            ],
            "fact_replacements": [
                {
                    "event_id": "sep24_helio_followup",
                    "replaced_fact": {
                        "source": "this_quarter",
                        "event_id": "aug18_helio_start_beta",
                        "fact_number": 1,
                    },
                    "replacement_fact_numbers": [1],
                    "still_current_information": [],
                    "changed_or_ended_information": ["The August capped support metric ends."],
                }
            ],
        }
        self.assertNotIn(
            "aug18_helio_start_beta_capped_support_metric",
            json.dumps(response["fact_replacements"]),
        )
        canonical, errors = build_canonical_plan(story, response)
        self.assertEqual(errors, [])
        assert canonical is not None
        self.assertEqual(
            canonical["plan"]["events"][1]["facts_to_establish"][0]["supersedes_fact_keys"],
            ["aug18_helio_start_beta_capped_support_metric"],
        )
        self.assertEqual(
            derive_replacement_accounting(response, canonical)[0]["replaced_fact_ref"],
            "key:aug18_helio_start_beta_capped_support_metric",
        )

    def test_fact_repair_merge_uses_repaired_ids_and_preserves_other_events(self) -> None:
        story = self._story(second_event=True)
        original = self._finalization_response(story)
        original["event_fact_annotations"].append(
            {
                "event_id": "event-2",
                "relevant_existing_fact_ids": [],
                "facts_to_establish": [],
            }
        )
        repair = {
            "event_fact_annotations": [
                {
                    "event_id": "event-1",
                    "relevant_existing_fact_ids": [9],
                    "facts_to_establish": [
                        {
                            "key": "repaired_forecast_owner",
                            "statement": "Sarah owns the forecast.",
                            "applies_when": "After April 10",
                            "subjects": ["morgan"],
                        }
                    ],
                }
            ],
            "fact_replacements": copy.deepcopy(original["fact_replacements"]),
        }
        merged, errors = merge_fact_repair(
            story_response=story,
            original_response=original,
            repair_response=repair,
            failed_event_ids={"event-1"},
        )
        self.assertEqual(errors, [])
        self.assertEqual(set(merged or {}), {"event_fact_annotations", "fact_replacements"})
        self.assertEqual(
            (merged or {})["event_fact_annotations"][0]["relevant_existing_fact_ids"],
            [9],
        )
        self.assertEqual(
            (merged or {})["event_fact_annotations"][1],
            original["event_fact_annotations"][1],
        )
        self.assertEqual(
            (merged or {})["event_fact_annotations"][0]["facts_to_establish"][0]["key"],
            "repaired_forecast_owner",
        )
        self.assertEqual(
            (merged or {})["fact_replacements"][0]["replaced_fact"],
            {"source": "starting_checkpoint", "fact_id": 5},
        )

    def test_fact_repair_merge_accepts_event_with_no_original_fact_row(self) -> None:
        story = self._story(second_event=True)
        original = self._finalization_response(story)
        repair = {
            "event_fact_annotations": [
                {
                    "event_id": "event-2",
                    "relevant_existing_fact_ids": [],
                    "facts_to_establish": [],
                }
            ],
            "fact_replacements": [],
        }

        merged, errors = merge_fact_repair(
            story_response=story,
            original_response=original,
            repair_response=repair,
            failed_event_ids={"event-2"},
        )

        self.assertEqual(errors, [])
        self.assertIn(
            {
                "event_id": "event-2",
                "relevant_existing_fact_ids": [],
                "facts_to_establish": [],
            },
            (merged or {})["event_fact_annotations"],
        )

    def test_fact_repair_merge_rejects_old_two_field_annotation_rows(self) -> None:
        story = self._story()
        original = self._finalization_response(story)
        repair = {
            "event_fact_annotations": [
                {
                    "event_id": "event-1",
                    "facts_to_establish": [],
                }
            ],
            "fact_replacements": [],
        }
        merged, errors = merge_fact_repair(
            story_response=story,
            original_response=original,
            repair_response=repair,
            failed_event_ids={"event-1"},
        )
        self.assertIsNone(merged)
        self.assertTrue(
            any("relevant_existing_fact_ids" in error for error in errors),
            errors,
        )

    def test_fact_repair_payload_includes_only_directly_used_current_facts(self) -> None:
        story = self._story()
        story["story"]["events"][0]["subjects"] = ["morgan", "project:forecast"]
        original = self._finalization_response(story)
        canonical, errors = build_canonical_plan(story, original)
        self.assertEqual(errors, [])
        assert canonical is not None
        current_facts = [
            {
                "id": 7,
                "statement": "Morgan uses the forecast workbook.",
                "subjects": ["morgan", "project:forecast"],
            },
            {
                "id": 8,
                "statement": "Morgan's forecast has a Friday update.",
                "subjects": ["morgan"],
            },
            {
                "id": 9,
                "statement": "A different project uses another workbook.",
                "subjects": ["morgan", "project:other"],
            },
        ]
        payload = fact_repair_payload(
            failed_event_ids={"event-1"},
            story_response=story,
            final_response=original,
            canonical_plan=canonical,
            current_facts=current_facts,
            quality_review={
                "event_reviews": [],
                "fact_reviews": [{"event_id": "event-1", "passed": False}],
                "replacement_reviews": [],
            },
            primary_subject_ids={"morgan"},
        )
        self.assertEqual(
            [
                fact["id"]
                for fact in payload[
                    "exact_starting_facts_used_or_replaced"
                ]
            ],
            [7],
        )

    def test_fact_repair_payload_includes_fact_from_earlier_builds_on_event(self) -> None:
        earlier_event = self._story_event("event-1", "2024-04-10")
        repaired_event = self._story_event("event-2", "2024-05-10")
        story = {"story": {"events": [earlier_event, repaired_event]}}
        earlier_fact = {
            "key": "forecast_exists",
            "statement": "The forecast exists.",
            "applies_when": "After April 10",
            "subjects": ["morgan"],
            "supersedes": [],
            "supersedes_fact_keys": [],
        }
        canonical = {
            "plan": {
                "events": [
                    {
                        **earlier_event,
                        "relevant_existing_fact_ids": [],
                        "facts_to_establish": [earlier_fact],
                    },
                    {
                        **repaired_event,
                        "relevant_existing_fact_ids": [],
                        "facts_to_establish": [],
                    },
                ]
            }
        }
        payload = fact_repair_payload(
            failed_event_ids={"event-2"},
            story_response=story,
            final_response={
                "event_fact_annotations": [],
                "fact_replacements": [],
            },
            canonical_plan=canonical,
            current_facts=[],
            quality_review={
                "event_reviews": [],
                "fact_reviews": [],
                "replacement_reviews": [],
            },
            primary_subject_ids={"morgan"},
        )

        self.assertEqual(
            payload["relevant_earlier_quarter_facts"],
            [{"event_id": "event-1", "fact_number": 1, "fact": earlier_fact}],
        )

    def test_fact_repair_scope_includes_later_transitive_replacement_events(self) -> None:
        jan7 = "alex_q1_2025_01_07_north_pier_accepts_recording_terms"
        jan10 = "alex_q1_2025_01_10_north_pier_recording_followup"
        jan11 = "alex_q1_2025_01_11_north_pier_recording_handoff"
        story = {
            "story": {
                "events": [
                    self._story_event(jan7, "2025-01-07"),
                    self._story_event(jan10, "2025-01-10"),
                    self._story_event(jan11, "2025-01-11"),
                ]
            }
        }
        jan7_facts = [
            {"key": "north_pier_recording_scope", "statement": "North Pier may record.", "subjects": ["morgan"]},
            {"key": "north_pier_recording_terms", "statement": "North Pier accepted the recording terms.", "subjects": ["morgan"]},
        ]
        jan10_facts = [
            {"key": "north_pier_recording_followup", "statement": "North Pier has the follow-up terms.", "subjects": ["morgan"]}
        ]
        jan11_facts = [
            {"key": "north_pier_recording_handoff", "statement": "North Pier has the final handoff terms.", "subjects": ["morgan"]}
        ]
        canonical = {
            "plan": {
                "events": [
                    {**story["story"]["events"][0], "relevant_existing_fact_ids": [], "facts_to_establish": jan7_facts},
                    {**story["story"]["events"][1], "relevant_existing_fact_ids": [], "facts_to_establish": jan10_facts},
                    {**story["story"]["events"][2], "relevant_existing_fact_ids": [], "facts_to_establish": jan11_facts},
                ]
            }
        }
        jan10_row = {
            "event_id": jan10,
            "replaced_fact": {"source": "this_quarter", "event_id": jan7, "fact_number": 2},
            "replacement_fact_numbers": [1],
            "still_current_information": [],
            "changed_or_ended_information": ["The original recording terms no longer govern."],
        }
        jan11_row = {
            "event_id": jan11,
            "replaced_fact": {"source": "this_quarter", "event_id": jan10, "fact_number": 1},
            "replacement_fact_numbers": [1],
            "still_current_information": [],
            "changed_or_ended_information": ["The follow-up terms no longer govern."],
        }
        finalization = {
            "event_fact_annotations": [
                {"event_id": jan7, "relevant_existing_fact_ids": [], "facts_to_establish": jan7_facts},
                {"event_id": jan10, "relevant_existing_fact_ids": [], "facts_to_establish": jan10_facts},
                {"event_id": jan11, "relevant_existing_fact_ids": [], "facts_to_establish": jan11_facts},
            ],
            "fact_replacements": [jan10_row, jan11_row],
        }
        scope = expand_fact_repair_scope(
            failed_event_ids={jan7},
            story_response=story,
            final_response=finalization,
        )
        self.assertEqual(scope, {jan7, jan10, jan11})
        payload = fact_repair_payload(
            failed_event_ids=scope,
            story_response=story,
            final_response=finalization,
            canonical_plan=canonical,
            current_facts=[],
            quality_review={"event_reviews": [], "fact_reviews": [], "replacement_reviews": []},
            primary_subject_ids={"morgan"},
        )
        self.assertEqual(payload["event_ids_to_repair"], [jan7, jan10, jan11])
        self.assertEqual(
            payload["later_events_that_depend_on_repaired_facts"],
            [jan10_row, jan11_row],
        )

    def test_fact_repair_payload_shows_earlier_same_quarter_change_to_starting_fact(self) -> None:
        jan10 = "riley_2025q1_jan10_ankle_referral"
        mar21 = "riley_2025q1_mar21_continuous_run_return"
        story = {
            "story": {
                "events": [
                    self._story_event(jan10, "2025-01-10"),
                    self._story_event(mar21, "2025-03-21"),
                ]
            }
        }
        jan10_facts = [
            {"key": "ankle_recovery_running_rule", "statement": "Riley runs only after the ankle referral clears it.", "subjects": ["morgan"]}
        ]
        mar21_facts = [
            {"key": "continuous_run_duration_rule", "statement": "Riley runs continuously for the approved duration and frequency.", "subjects": ["morgan"]}
        ]
        canonical = {
            "plan": {
                "events": [
                    {**story["story"]["events"][0], "relevant_existing_fact_ids": [], "facts_to_establish": jan10_facts},
                    {**story["story"]["events"][1], "relevant_existing_fact_ids": [285], "facts_to_establish": mar21_facts},
                ]
            }
        }
        jan10_row = {
            "event_id": jan10,
            "replaced_fact": {"source": "starting_checkpoint", "fact_id": 285},
            "replacement_fact_numbers": [1],
            "still_current_information": [],
            "changed_or_ended_information": ["The earlier running rule no longer governs."],
        }
        mar21_row = {
            "event_id": mar21,
            "replaced_fact": {"source": "starting_checkpoint", "fact_id": 285},
            "replacement_fact_numbers": [1],
            "still_current_information": [],
            "changed_or_ended_information": ["The ankle limitation no longer governs."],
        }
        finalization = {
            "event_fact_annotations": [
                {"event_id": jan10, "relevant_existing_fact_ids": [], "facts_to_establish": jan10_facts},
                {"event_id": mar21, "relevant_existing_fact_ids": [285], "facts_to_establish": mar21_facts},
            ],
            "fact_replacements": [jan10_row, mar21_row],
        }
        payload = fact_repair_payload(
            failed_event_ids={mar21},
            story_response=story,
            final_response=finalization,
            canonical_plan=canonical,
            current_facts=[{"id": 285, "statement": "Riley has an earlier running rule.", "subjects": ["morgan"]}],
            quality_review={"event_reviews": [], "fact_reviews": [], "replacement_reviews": []},
            primary_subject_ids={"morgan"},
        )
        self.assertEqual(
            payload["earlier_same_quarter_changes_to_starting_facts"],
            [
                {
                    "event_id": jan10,
                    "facts_to_establish": jan10_facts,
                    "replacement_accounting_rows": [
                        {
                            "event_id": jan10,
                            "replaced_fact_ref": "id:285",
                            "replacement_fact_keys": ["ankle_recovery_running_rule"],
                            "still_current_information": [],
                            "changed_or_ended_information": ["The earlier running rule no longer governs."],
                        }
                    ],
                }
            ],
        )

    def test_derived_plan_stays_compatible_with_accepted_plan_schema(self) -> None:
        story = self._story()
        response = self._finalization_response(story)
        canonical, errors = build_canonical_plan(story, response)
        self.assertEqual(errors, [])
        assert canonical is not None
        arguments = self._story_validation_arguments()
        arguments["starting_facts"] = [
            {"id": 5, "construction_key": "old_forecast_owner"},
            {"id": 7, "construction_key": "forecast_exists"},
        ]
        self.assertEqual(validate_plan(canonical, **arguments), [])
        self.assertEqual(
            set(canonical["plan"]["events"][0]["facts_to_establish"][0]),
            {
                "key",
                "statement",
                "applies_when",
                "subjects",
                "supersedes",
                "supersedes_fact_keys",
            },
        )

    def _run_arguments(
        self,
        root: Path,
        *,
        accepted_through: str = "2024-03-31",
        history: list[dict] | None = None,
    ) -> tuple[argparse.Namespace, Path]:
        history = history or []
        checkpoint = root / "checkpoint"
        save_checkpoint(
            path=checkpoint,
            persona="morgan",
            week={"week_id": "q1", "end_date": accepted_through},
            previous_hash="previous",
            inputs={},
            state={
                "history": history,
                "session_purposes": [
                    {
                        "session_id": str(session["id"]),
                        "narrative_date": str(session["narrative_date"]),
                        "purpose": "Accepted context.",
                    }
                    for session in history
                ],
                "entities": [{"id": "morgan"}],
                "facts": [
                    {
                        "id": 3,
                        "construction_key": "retired_forecast_owner",
                        "statement": "Alex owned the forecast.",
                        "applies_when": "Before April 1",
                    },
                    {
                        "id": 5,
                        "construction_key": "old_forecast_owner",
                        "statement": "Morgan owns the forecast and the Friday update remains required.",
                        "applies_when": "Before April 10",
                        "source_session_ids": ["old-forecast-session"],
                        "supersedes": [3],
                    },
                    {
                        "id": 7,
                        "construction_key": "forecast_exists",
                        "statement": "The Q2 forecast exists.",
                        "applies_when": "Always",
                    },
                ],
                "app_state": {"private_app_value": "schedule-layer-only"},
                "unfinished_threads": [],
            },
            operation_results=[],
            covered_events=[],
            system_sha256="system",
            identity_version=2,
        )
        overview = root / "overview.json"
        overview.write_text(json.dumps({"overview": {}}))
        previous = root / "previous.json"
        previous.write_text(json.dumps({"plan": {"events": []}}))
        persona_sheet = root / "persona.md"
        persona_sheet.write_text("Morgan")
        generator_context = root / "context.md"
        generator_context.write_text("Context")
        output = root / "output"
        return (
            argparse.Namespace(
                persona="morgan",
                persona_sheet=persona_sheet,
                generator_context=generator_context,
                overview=overview,
                starting_checkpoint=checkpoint,
                previous_quarter_plan=previous,
                period_start="2024-04-01",
                period_end="2024-06-30",
                output=output,
            ),
            output,
        )


if __name__ == "__main__":
    unittest.main()
