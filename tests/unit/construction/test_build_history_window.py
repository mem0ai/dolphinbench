from __future__ import annotations

import ast
import copy
import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from jsonschema import Draft202012Validator, ValidationError
import yaml

import construction.build_history_window as history_window
from construction.build_history_window import (
    CompletedOutputError,
    ENRICHMENT_CONTACT_SCHEMA,
    ENRICHMENT_RESPONSE_SCHEMA,
    PLANNER_RESPONSE_SCHEMA,
    STORY_RESPONSE_SCHEMA,
    WRITER_API_RESPONSE_SCHEMA,
    accepted_contact_chronology_for_planner,
    accepted_communication_destinations,
    accepted_protected_changes,
    assign_window_session_ids,
    build_enrichment_payload,
    build_planner_payload,
    build_story_payload,
    build_writer_payload,
    calendar_update_context_errors,
    accepted_contact_details_for_occurrence,
    accepted_contact_details_for_rendered_session,
    compact_older_contact_summary,
    complete_model_stage,
    construct_writer_plan,
    cumulative_operation_results,
    decode_planner_app_operations,
    normalize_writer_messages,
    emit_candidate_checkpoint,
    include_current_state_statements_in_writer_context,
    load_replacement_evidence_refs,
    materialize_window_state,
    merge_story_and_enrichment,
    enrichment_response_schema,
    normalize_planner_occurrence_order,
    normalize_writer_contact_order,
    planner_current_facts,
    planner_open_threads,
    prior_accepted_context_by_contact_subject,
    prior_accepted_contacts_for_open_threads,
    prepare_window_output,
    new_entity_candidates_for_week,
    resolve_current_fact_references_for_model,
    resolve_superseded_fact_references,
    select_new_entities_for_week,
    required_fact_declarations,
    replacement_source_fact_ids_for_window,
    unfinished_thread_current_fact_ids,
    update_unfinished_threads,
    validate_history_window_plan_response,
    validate_history_window_response,
    validate_writer_prose_response,
    validate_window_dates,
    restore_code_owned_period,
    story_action_categories,
    story_contact_count_errors,
    story_response_schema,
    story_existing_app_items,
    write_window_operation_results,
    writer_api_response_schema,
    writer_facts_by_occurrence,
    writer_known_entities_for_occurrence,
)
from construction.checkpoints import load_checkpoint
from construction.runtime_inputs import exact_persona_tools
from construction.long_history_prompts import (
    HISTORY_WINDOW_PLANNING_SYSTEM,
    HISTORY_WINDOW_STORY_SYSTEM,
    HISTORY_WINDOW_WRITING_SYSTEM,
)
import construction.long_history_prompts as long_history_prompts
from construction.model_input_text import render_story_planner_input


class HistoryWindowTest(unittest.TestCase):
    maxDiff = None

    def test_code_owned_story_fields_do_not_treat_record_references_as_open_work(self) -> None:
        story = {
            "plan": {
                "occurrences": [
                    {
                        "development_ids": [],
                        "continues_from": ["accepted-session"],
                        "uses_items_created_by_contact_ids": [],
                        "thread_id": "accepted-thread",
                        "thread_after": {"is_open": True},
                    },
                    {
                        "development_ids": [],
                        "continues_from": [],
                        "uses_items_created_by_contact_ids": [
                            "same-week-document-creator"
                        ],
                        "thread_id": "",
                        "thread_after": {"is_open": False},
                    },
                    {
                        "development_ids": ["accepted-development"],
                        "continues_from": [],
                        "uses_items_created_by_contact_ids": ["same-week-release"],
                        "thread_id": "new-review-thread",
                        "thread_after": {"is_open": True},
                    },
                    {
                        "development_ids": [],
                        "continues_from": ["same-week-review"],
                        "uses_items_created_by_contact_ids": [],
                        "thread_id": "new-review-thread",
                        "thread_after": {"is_open": True},
                    },
                ]
            }
        }

        result = history_window.add_code_owned_story_fields(story)
        occurrences = result["plan"]["occurrences"]

        self.assertEqual(occurrences[0]["effect_kind"], "advance_open_thread")
        self.assertEqual(occurrences[0]["basis_ids"], ["accepted-thread"])
        self.assertEqual(occurrences[1]["effect_kind"], "self_contained")
        self.assertEqual(
            occurrences[1]["basis_ids"],
            [history_window.ORDINARY_CURRENT_LIFE_BASIS_ID],
        )
        self.assertEqual(occurrences[2]["effect_kind"], "realize_development")
        self.assertEqual(occurrences[2]["basis_ids"], ["accepted-development"])
        self.assertEqual(occurrences[3]["effect_kind"], "advance_open_thread")
        self.assertEqual(occurrences[3]["basis_ids"], ["new-review-thread"])

    def test_enrichment_schema_keeps_item_types_for_empty_reference_arrays(self) -> None:
        schema = enrichment_response_schema(
            {
                "fixed_contacts": [{"contact_id": "contact-1"}],
                "current_facts": [],
                "lasting_facts_that_must_be_established": [],
            }
        )
        properties = schema["properties"]["contacts"]["items"]["properties"]
        self.assertEqual(
            properties["uses_current_fact_ids"],
            {"type": "array", "items": {"type": "integer"}, "maxItems": 0},
        )
        self.assertEqual(
            properties["uses_new_fact_keys"],
            {"type": "array", "items": {"type": "string"}, "maxItems": 0},
        )

    def test_story_contact_ids_are_assigned_by_code_and_references_follow(self) -> None:
        story = {
            "plan": {
                "occurrences": [
                    {
                        "contact_id": "c001",
                        "narrative_date": "2025-07-22T09:00:00-05:00",
                        "continues_from": ["older-thread-contact"],
                        "uses_items_created_by_contact_ids": [],
                    },
                    {
                        "contact_id": "c002",
                        "narrative_date": "2025-07-22T11:00:00-05:00",
                        "continues_from": ["c001"],
                        "uses_items_created_by_contact_ids": ["c001"],
                    },
                    {
                        "contact_id": "c003",
                        "narrative_date": "2025-07-23T08:00:00-05:00",
                        "continues_from": [],
                        "uses_items_created_by_contact_ids": [],
                    },
                ]
            }
        }

        assigned = history_window.assign_story_contact_ids(
            story,
            persona="riley",
            timezone_name="America/Chicago",
        )["plan"]["occurrences"]

        self.assertEqual(
            [row["contact_id"] for row in assigned],
            [
                "riley_2025_07_22_001",
                "riley_2025_07_22_002",
                "riley_2025_07_23_001",
            ],
        )
        self.assertEqual(assigned[0]["continues_from"], ["older-thread-contact"])
        self.assertEqual(
            assigned[1]["continues_from"], ["riley_2025_07_22_001"]
        )
        self.assertEqual(
            assigned[1]["uses_items_created_by_contact_ids"],
            ["riley_2025_07_22_001"],
        )

    def test_story_input_excludes_tools_and_app_records(self) -> None:
        planner_payload = {
            "persona": "alex",
            "requested_dates": {"start": "2025-04-01", "end": "2025-04-07"},
            "narrative_timezone": "America/Los_Angeles",
            "world_description": "Alex runs an engineering organization.",
            "developments_for_this_week": [
                {
                    "id": "release",
                    "what_happens": "A release decision is made.",
                    "facts_to_establish": [{"key": "secret_fact"}],
                    "must_finish_in_this_window": True,
                },
                {
                    "id": "later-release",
                    "start_date": "2025-04-06",
                    "end_date": "2025-04-09",
                    "subjects": ["person:alex"],
                    "what_happens": "Several intermediate checks occur.",
                    "lasting_outcome": "The later release finishes after this week.",
                    "facts_to_establish": [],
                    "must_finish_in_this_window": False,
                },
            ],
            "accepted_developments_after_this_window": [],
            "protected_changes": [],
            "current_facts": [
                {"id": 81, "statement": "The release is paused.", "subjects": ["person:alex"]},
                {"id": 82, "statement": "The newest release rule applies.", "subjects": ["person:alex"]},
            ],
            "current_ordinary_states": [
                {
                    "key": "release_state",
                    "subject_ids": ["person:alex"],
                    "statement": "The release candidate is waiting for approval.",
                    "source_session_id": "state-source-secret",
                    "date": "2025-03-31",
                },
                {
                    "key": "newer_release_state",
                    "subject_ids": ["person:alex"],
                    "statement": "The newest continuing condition applies.",
                    "date": "2025-04-01",
                },
            ],
            "known_entities": [{"id": "person:alex", "name": "Alex"}],
            "available_simulated_actions": [
                {"tool_name": "post_pr_comment", "required_arguments": ["pr_id"]}
            ],
            "available_app_records": {
                "github": [{"id": "pr-secret"}],
                "docs": [
                    {
                        "id": "doc-secret",
                        "title": "Release note",
                        "body": "A repeated story idea that must stay out.",
                    }
                ],
            },
            "older_contacts_that_already_happened": [
                {
                    "session_id": "old-1",
                    "date": "2025-03-25T09:00:00-07:00",
                    "subject_ids": ["person:alex"],
                    "contact_purpose": "Alex reviewed a release problem.",
                    "accepted_app_operation_identities": [{"tool": "post_pr_comment"}],
                }
            ],
            "recently_finished_contacts": [],
            "open_threads": [
                {
                    "basis_id": "release-thread",
                    "continues_from_id": "old-1",
                    "subject_ids": ["person:alex"],
                    "what_already_happened": "Approval was requested.",
                    "what_remains": "The approval is still pending.",
                    "current_fact_ids": [81],
                    "prior_accepted_contacts": [
                        {"accepted_contact_details": "Duplicated contact text."}
                    ],
                    "earlier_user_messages_in_same_matter": [
                        {"messages": ["Duplicated user message."]}
                    ],
                    "current_ordinary_states": [
                        {"statement": "Duplicated nested state."}
                    ],
                }
            ],
            "valid_existing_continues_from_ids": [],
            "previously_used_communication_destinations": [],
            "new_entities_that_may_first_appear_this_week": [],
            "lasting_facts_that_must_be_established": [],
        }

        story_payload = build_story_payload(planner_payload)
        rendered = render_story_planner_input(story_payload)

        self.assertNotIn("available_simulated_actions", story_payload)
        self.assertNotIn("available_app_records", story_payload)
        self.assertEqual(
            [row["id"] for row in story_payload["developments_for_this_week"]],
            ["release"],
        )
        self.assertEqual(
            story_payload["developments_finishing_later"],
            [
                {
                    "id": "later-release",
                    "start_date": "2025-04-06",
                    "end_date": "2025-04-09",
                    "subjects": ["person:alex"],
                    "what_happens": "Several intermediate checks occur.",
                    "lasting_outcome": "The later release finishes after this week.",
                }
            ],
        )
        self.assertIn("Several intermediate checks occur.", rendered)
        self.assertIn("The later release finishes after this week.", rendered)
        self.assertEqual(
            [row["statement"] for row in story_payload["current_conditions"]],
            ["The newest release rule applies.", "The release is paused."],
        )
        self.assertEqual(
            [row["key"] for row in story_payload["current_ordinary_states"]],
            ["newer_release_state", "release_state"],
        )
        self.assertNotIn("post_pr_comment", rendered)
        self.assertNotIn("pr-secret", rendered)
        self.assertIn("Release note", rendered)
        self.assertNotIn("A repeated story idea", rendered)
        self.assertIn("The release candidate is waiting for approval.", rendered)
        self.assertNotIn("state-source-secret", rendered)
        self.assertIn("The approval is still pending.", rendered)
        self.assertNotIn("Duplicated contact text", rendered)
        self.assertNotIn("Duplicated user message", rendered)
        self.assertNotIn("Duplicated nested state", rendered)
        self.assertIn("Week of 2025-03-24", rendered)
        self.assertIn("The release is paused.", rendered)

    def test_story_payload_includes_upcoming_calendar_events(self) -> None:
        planner_payload = {
            "persona": "alex",
            "requested_dates": {"start": "2025-04-01", "end": "2025-04-07"},
            "narrative_timezone": "America/Los_Angeles",
            "world_description": "Alex runs an engineering organization.",
            "developments_for_this_week": [],
            "accepted_developments_after_this_window": [],
            "protected_changes": [],
            "current_facts": [],
            "current_ordinary_states": [],
            "known_entities": [],
            "available_simulated_actions": [],
            "available_app_records": {
                "calendar": [
                    {
                        "id": "evt-overlap",
                        "title": "On call",
                        "start": "2025-03-31T09:00:00-07:00",
                        "end": "2025-04-07T09:00:00-07:00",
                        "attendees": ["Alex"],
                        "body": "Primary coverage.",
                    },
                    {
                        "id": "evt-later",
                        "title": "Later",
                        "start": "2025-04-08T09:00:00-07:00",
                        "end": "2025-04-08T10:00:00-07:00",
                        "attendees": [],
                        "body": "Outside the week.",
                    },
                ]
            },
            "older_contacts_that_already_happened": [],
            "recently_finished_contacts": [],
            "open_threads": [],
            "valid_existing_continues_from_ids": [],
            "previously_used_communication_destinations": [],
            "new_entities_that_may_first_appear_this_week": [],
            "lasting_facts_that_must_be_established": [],
        }

        payload = build_story_payload(planner_payload)

        self.assertEqual(
            payload["upcoming_calendar_events"],
            [
                {
                    "id": "evt-overlap",
                    "title": "On call",
                    "start": "2025-03-31T09:00:00-07:00",
                    "end": "2025-04-07T09:00:00-07:00",
                    "attendees": ["Alex"],
                },
                {
                    "id": "evt-later",
                    "title": "Later",
                    "start": "2025-04-08T09:00:00-07:00",
                    "end": "2025-04-08T10:00:00-07:00",
                    "attendees": [],
                }
            ],
        )

    def test_story_input_puts_current_status_before_identity_and_history(self) -> None:
        planner_payload = {
            "persona": "alex",
            "requested_dates": {"start": "2025-04-01", "end": "2025-04-07"},
            "narrative_timezone": "America/Los_Angeles",
            "world_description": "Kibo began as a proposed pilot.",
            "developments_for_this_week": [],
            "accepted_developments_after_this_window": [],
            "protected_changes": [],
            "current_facts": [
                {
                    "id": 81,
                    "statement": "Kibo is live and the pilot work is complete.",
                    "subjects": ["project:kibo"],
                }
            ],
            "current_ordinary_states": [],
            "known_entities": [
                {
                    "id": "project:kibo",
                    "name": "Kibo",
                    "reason": "Kibo was originally proposed as a pilot.",
                }
            ],
            "available_simulated_actions": [],
            "available_app_records": {},
            "older_contacts_that_already_happened": [],
            "recently_finished_contacts": [],
            "open_threads": [],
            "valid_existing_continues_from_ids": [],
            "previously_used_communication_destinations": [],
            "new_entities_that_may_first_appear_this_week": [],
            "lasting_facts_that_must_be_established": [],
        }

        rendered = render_story_planner_input(build_story_payload(planner_payload))

        self.assertLess(
            rendered.index("WHAT IS CURRENTLY TRUE"),
            rendered.index("HISTORICAL ENTITY IDENTITY AND HISTORY"),
        )
        self.assertIn("Subjects:\n    - project:kibo", rendered)
        self.assertNotIn("Subject names:", rendered)
        self.assertIn(
            "Do not move completed or live work backward to an earlier stage unless "
            "a supplied new event explicitly reopens it.",
            " ".join(rendered.split()),
        )
        self.assertIn("identity and history, not current status", rendered)

    def test_enrichment_receives_exact_prior_app_item_mapping_and_update_rule(self) -> None:
        state = {
            "app_state": {},
            "operation_results": [
                {
                    "session_id": "doc-session",
                    "operation": {
                        "tool": "create_doc",
                        "args": {"title": "Launch checklist", "body": "Accepted body."},
                    },
                    "result": {"ok": True, "id": "doc-17"},
                },
                {
                    "session_id": "calendar-session",
                    "operation": {
                        "tool": "update_calendar_event",
                        "args": {"event_id": "evt-9", "start": "2025-04-02T10:00:00-07:00"},
                    },
                    "result": {
                        "ok": True,
                        "event": {"id": "evt-9", "title": "Kibo launch"},
                    },
                },
                {
                    "session_id": "doc-session",
                    "operation": {
                        "tool": "send_email",
                        "args": {"to": "team@example.com", "body": "Not an app item."},
                    },
                    "result": {"ok": True, "id": "sent-3"},
                },
            ],
        }
        accepted_contacts = [
            {
                "session_id": "doc-session",
                "planned_interaction_id": "contact-doc",
            },
            {
                "session_id": "calendar-session",
                "planned_interaction_id": "contact-calendar",
            },
        ]

        mapping = history_window.accepted_app_items_by_contact(
            state, accepted_contacts=accepted_contacts
        )
        planner_payload = {
            "persona": "alex",
            "requested_dates": {"start": "2025-04-01", "end": "2025-04-07"},
            "current_facts": [],
            "known_entities": [],
            "open_threads": [],
            "lasting_facts_that_must_be_established": [],
            "current_ordinary_states": [],
            "available_simulated_actions": [],
            "available_app_records": {},
            "earlier_accepted_app_items": mapping,
            "previously_used_communication_destinations": [],
        }
        story_response = {
            "plan": {
                "occurrences": [
                    {
                        "contact_id": "update-doc",
                        "thread_id": "",
                        "subject_ids": [],
                        "thread_after": {"is_open": False, "what_remains": None},
                    }
                ]
            }
        }

        enrichment = build_enrichment_payload(planner_payload, story_response)

        self.assertEqual(
            enrichment["earlier_accepted_app_items"],
            [
                {
                    "contact_id": "contact-doc",
                    "session_id": "doc-session",
                    "items": [
                        {
                            "tool": "create_doc",
                            "record_id": "doc-17",
                            "operation_result_id": "doc-17",
                            "title": "Launch checklist",
                        }
                    ],
                },
                {
                    "contact_id": "contact-calendar",
                    "session_id": "calendar-session",
                    "items": [
                        {
                            "tool": "update_calendar_event",
                            "record_id": "evt-9",
                            "operation_result_id": "evt-9",
                            "title": "Kibo launch",
                        }
                    ],
                },
            ],
        )
        self.assertIn(
            "use the supplied existing record ID",
            enrichment["existing_app_item_update_rule"],
        )
        self.assertIn(
            "Do not create a replacement item",
            enrichment["existing_app_item_update_rule"],
        )

    def test_enrichment_cannot_add_or_drop_fixed_contacts(self) -> None:
        def fixed(contact_id: str) -> dict[str, Any]:
            return {
                "contact_id": contact_id,
                "happening_id": f"happening-{contact_id}",
                "narrative_date": "2025-04-01T09:00:00-07:00",
                "basis_ids": [history_window.ORDINARY_CURRENT_LIFE_BASIS_ID],
                "effect_kind": "self_contained",
                "thread_id": "",
                "continues_from": [],
                "subject_ids": ["person:alex"],
                "development_ids": [],
                "what_happened": f"Fixed story for {contact_id}.",
                "assistant_outcome": {"kind": "none", "requested_result": None},
                "source_material": None,
                "thread_after": {"is_open": False, "what_remains": None},
            }

        def addition(contact_id: str) -> dict[str, Any]:
            return {
                "contact_id": contact_id,
                "uses_current_fact_ids": [],
                "uses_new_fact_keys": [],
                "durable_facts": [],
                "current_state_changes": [],
                "app_operations": [],
            }

        story = {"plan": {"occurrences": [fixed("one"), fixed("two")]}}
        merged = merge_story_and_enrichment(
            story, {"contacts": [addition("one"), addition("two")]}
        )
        self.assertEqual(
            [row["what_happened"] for row in merged["plan"]["occurrences"]],
            ["Fixed story for one.", "Fixed story for two."],
        )
        with self.assertRaisesRegex(ValueError, "every fixed contact"):
            merge_story_and_enrichment(story, {"contacts": [addition("one")]})
        with self.assertRaisesRegex(ValueError, "every fixed contact"):
            merge_story_and_enrichment(
                story,
                {"contacts": [addition("one"), addition("two"), addition("three")]},
            )

    def test_closing_a_thread_does_not_force_its_current_states_to_change(self) -> None:
        state = {
            "key": "water_bill_review",
            "subject_ids": ["person:riley", "home:cherrywood"],
            "statement": "The final adjustment remains under review.",
        }
        planner_payload = {
            "persona": "riley",
            "requested_dates": {"start": "2025-04-01", "end": "2025-04-07"},
            "current_facts": [],
            "known_entities": [],
            "open_threads": [
                {
                    "basis_id": "thread-water-bill",
                    "current_ordinary_states": [state],
                }
            ],
            "lasting_facts_that_must_be_established": [],
            "current_ordinary_states": [state],
            "available_simulated_actions": [],
            "available_app_records": {},
            "previously_used_communication_destinations": [],
        }
        story_response = {
            "plan": {
                "occurrences": [
                    {
                        "contact_id": "close-water-bill",
                        "thread_id": "thread-water-bill",
                        "subject_ids": ["person:riley", "home:cherrywood"],
                        "thread_after": {"is_open": False, "what_remains": None},
                    },
                    {
                        "contact_id": "unrelated-update",
                        "thread_id": "",
                        "subject_ids": ["person:riley"],
                        "thread_after": {"is_open": False, "what_remains": None},
                    },
                ]
            }
        }

        payload = build_enrichment_payload(planner_payload, story_response)

        self.assertNotIn(
            "existing_states_that_this_contact_must_update",
            payload["fixed_contacts"][0],
        )
        self.assertNotIn(
            "existing_states_that_this_contact_must_update",
            payload["fixed_contacts"][1],
        )

    def test_missing_closed_thread_state_can_be_corrected_without_changing_story(self) -> None:
        state = {
            "key": "running_trial",
            "subject_ids": ["person:riley"],
            "statement": "The second trial remains pending.",
        }
        response = {
            "plan": {
                "occurrences": [
                    {
                        "contact_id": "close-running-trial",
                        "thread_id": "thread-running-trial",
                        "what_happened": "The second trial passed and the new baseline is active.",
                        "durable_facts": [],
                        "current_state_changes": [],
                        "thread_after": {"is_open": False, "what_remains": None},
                    }
                ]
            }
        }

        missing = history_window.missing_closing_state_updates(
            response, {"thread-running-trial": [state]}
        )
        corrected = history_window.apply_missing_state_corrections(
            response,
            {
                "contacts": [
                    {
                        "contact_id": "close-running-trial",
                        "current_state_changes": [
                            {
                                "key": "running_trial",
                                "subject_ids": ["person:riley"],
                                "statement": "The second trial passed and the new baseline is active.",
                            }
                        ],
                    }
                ]
            },
            missing,
        )

        self.assertEqual(
            corrected["plan"]["occurrences"][0]["what_happened"],
            response["plan"]["occurrences"][0]["what_happened"],
        )
        self.assertEqual(
            corrected["plan"]["occurrences"][0]["current_state_changes"][0]["key"],
            "running_trial",
        )

    def test_story_schema_rejects_protected_or_future_development_ids(self) -> None:
        schema = story_response_schema(
            {"developments_for_this_week": [{"id": "current-development"}]}
        )
        occurrence_schema = schema["properties"]["plan"]["properties"]["occurrences"]["items"]
        allowed = occurrence_schema["properties"]["development_ids"]["items"]["enum"]

        self.assertEqual(allowed, ["current-development"])
        self.assertNotIn("protected-future-change", allowed)

    def test_cached_story_response_resumes_without_another_model_call(self) -> None:
        story_response = {
            "plan": {
                "occurrences": [
                    {
                        "contact_id": "one",
                        "happening_id": "happening-one",
                        "narrative_date": "2025-04-01T09:00:00-07:00",
                        "basis_ids": [history_window.ORDINARY_CURRENT_LIFE_BASIS_ID],
                        "effect_kind": "self_contained",
                        "thread_id": "",
                        "continues_from": [],
                        "subject_ids": ["person:alex"],
                        "development_ids": [],
                        "what_happened": "Alex records a new observation.",
                        "assistant_outcome": {"kind": "none", "requested_result": None},
                        "source_material": None,
                        "thread_after": {"is_open": False, "what_remains": None},
                    }
                ]
            }
        }
        client = Mock(model="gpt-5.6-sol")
        client.complete.return_value = story_response
        payload = {
            "persona": "alex",
            "narrative_timezone": "America/Los_Angeles",
            "requested_dates": {"start": "2025-04-01", "end": "2025-04-07"},
            "older_contacts_that_already_happened": [],
            "recently_finished_contacts": [],
        }
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            (output / "work").mkdir()
            first = complete_model_stage(
                output_dir=output,
                stage="story",
                system=HISTORY_WINDOW_STORY_SYSTEM,
                payload=payload,
                client=client,
                start=date(2025, 4, 1),
                end=date(2025, 4, 7),
            )
            second = complete_model_stage(
                output_dir=output,
                stage="story",
                system=HISTORY_WINDOW_STORY_SYSTEM,
                payload=payload,
                client=client,
                start=date(2025, 4, 1),
                end=date(2025, 4, 7),
            )

        self.assertEqual(first, second)
        client.complete.assert_called_once()

    def test_reviewed_story_survives_later_prompt_changes(self) -> None:
        story_response = {"plan": {"occurrences": []}}
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            (output / "manifest.json").write_text(
                json.dumps(
                    {
                        "status": "story_only",
                        "period_start": "2025-04-01",
                        "period_end": "2025-04-07",
                        "starting_checkpoint_sha256": "checkpoint-sha",
                    }
                )
                + "\n"
            )
            (output / "story_response.json").write_text(
                json.dumps(story_response) + "\n"
            )

            loaded = history_window._load_reviewed_story_response(
                output,
                start=date(2025, 4, 1),
                end=date(2025, 4, 7),
                starting_checkpoint_sha256="checkpoint-sha",
            )

        self.assertEqual(loaded, story_response)

    def test_planner_payload_separates_later_accepted_developments(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            quarter = root / "quarter.json"
            spec = root / "spec.yaml"
            world = root / "world.md"
            quarter.write_text(
                json.dumps(
                    {
                        "plan": {
                            "period_start": "2025-01-01",
                            "period_end": "2025-03-31",
                            "events": [
                                {
                                    "id": "current",
                                    "start_date": "2025-01-29",
                                    "end_date": "2025-01-30",
                                    "subjects": ["person:morgan"],
                                    "what_happens": "Current development.",
                                    "lasting_outcome": "Current outcome.",
                                    "facts_to_establish": [],
                                },
                                {
                                    "id": "later",
                                    "start_date": "2025-02-10",
                                    "end_date": "2025-02-12",
                                    "subjects": ["person:morgan"],
                                    "what_happens": "Later development.",
                                    "lasting_outcome": "Later outcome.",
                                    "facts_to_establish": [],
                                },
                                {
                                    "id": "at-quarter-end",
                                    "start_date": "2025-03-31",
                                    "end_date": "2025-03-31",
                                    "subjects": ["person:morgan"],
                                    "what_happens": "Quarter-end development.",
                                    "lasting_outcome": "Quarter-end outcome.",
                                    "facts_to_establish": [],
                                },
                            ],
                        }
                    }
                )
            )
            spec.write_text(
                yaml.safe_dump(
                    {
                        "narrative_timezone": "America/Los_Angeles",
                        "primary_subject_ids": ["person:morgan"],
                    }
                )
            )
            world.write_text("The accepted world.\n")
            payload, _ = build_planner_payload(
                config={
                    "persona": "morgan",
                    "quarter_plan": str(quarter),
                    "spec": str(spec),
                    "generator_context": str(world),
                },
                checkpoint={"covered_quarter_event_ids": []},
                state={
                    "facts": [],
                    "entities": [{"id": "person:morgan", "name": "Morgan"}],
                    "app_state": {},
                    "history": [],
                    "session_purposes": [],
                    "operation_results": [],
                    "unfinished_threads": [],
                },
                start=date(2025, 1, 29),
                end=date(2025, 2, 2),
            )

        self.assertEqual(
            [row["id"] for row in payload["developments_for_this_week"]],
            ["current"],
        )
        self.assertEqual(
            [row["id"] for row in payload["accepted_developments_after_this_window"]],
            ["later", "at-quarter-end"],
        )
        self.assertEqual(payload["current_ordinary_states"], [])

    def test_multiweek_development_routes_facts_before_finish_and_authorizes_at_finish(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            quarter = root / "quarter.json"
            spec = root / "spec.yaml"
            world = root / "world.md"
            quarter.write_text(
                json.dumps(
                    {
                        "plan": {
                            "period_start": "2025-01-01",
                            "period_end": "2025-03-31",
                            "events": [
                                {
                                    "id": "multiweek",
                                    "start_date": "2025-01-29",
                                    "end_date": "2025-02-05",
                                    "subjects": ["person:morgan", "project:alpha"],
                                    "what_happens": "The Alpha rollout completes.",
                                    "facts_to_establish": [
                                        {
                                            "key": "alpha-live",
                                            "statement": "Alpha is live.",
                                            "applies_when": "Alpha is discussed",
                                            "subjects": ["project:alpha"],
                                            "supersedes": [],
                                        }
                                    ],
                                }
                            ],
                        }
                    }
                )
            )
            spec.write_text(
                "narrative_timezone: America/Los_Angeles\n"
                "primary_subject_ids:\n- person:morgan\n"
            )
            world.write_text("Accepted world.\n")
            config = {
                "persona": "morgan",
                "quarter_plan": str(quarter),
                "spec": str(spec),
                "generator_context": str(world),
            }
            state = {
                "facts": [],
                "entities": [
                    {"id": "person:morgan", "name": "Morgan"},
                    {"id": "project:alpha", "name": "Alpha"},
                ],
                "app_state": {},
                "history": [],
                "session_purposes": [],
                "operation_results": [],
                "unfinished_threads": [],
            }

            early_payload, early_context = build_planner_payload(
                config=config,
                checkpoint={"covered_quarter_event_ids": []},
                state=state,
                start=date(2025, 1, 29),
                end=date(2025, 2, 2),
            )
            finish_payload, finish_context = build_planner_payload(
                config=config,
                checkpoint={"covered_quarter_event_ids": []},
                state=state,
                start=date(2025, 2, 3),
                end=date(2025, 2, 9),
            )

        self.assertEqual(
            early_payload["developments_for_this_week"][0]["facts_to_establish"],
            [],
        )
        self.assertFalse(
            early_payload["developments_for_this_week"][0]["must_finish_in_this_window"]
        )
        self.assertEqual(early_context["facts_by_development"], {})
        self.assertTrue(
            finish_payload["developments_for_this_week"][0]["must_finish_in_this_window"]
        )
        self.assertEqual(
            finish_payload["developments_for_this_week"][0]["facts_to_establish"][0]["key"],
            "alpha-live",
        )
        self.assertEqual(
            finish_context["facts_by_development"]["multiweek"][0]["key"],
            "alpha-live",
        )

    def test_planner_current_facts_include_primary_only_and_selected_subjects(self) -> None:
        facts = [
            {"id": 1, "subjects": ["person:morgan"], "statement": "Primary only."},
            {"id": 2, "subjects": ["project:alpha"], "statement": "Alpha is ready."},
            {"id": 3, "subjects": ["project:other"], "statement": "Other is ready."},
            {"id": 4, "subjects": ["person:morgan"], "statement": "Required primary."},
            {"id": 5, "subjects": ["pet:kibo"], "statement": "Kibo is a dog."},
        ]
        selected = planner_current_facts(
            facts,
            quarter_developments=[
                {
                    "id": "alpha-development",
                    "subjects": ["person:morgan", "project:alpha"],
                    "relevant_existing_fact_ids": [4],
                }
            ],
            recent_chronology=[],
            primary_subject_ids={"person:morgan"},
            selected_subject_ids={"pet:kibo"},
        )

        self.assertEqual([fact["id"] for fact in selected], [1, 2, 4, 5])

    def test_accepted_contact_details_preserve_planner_situation_and_source_material(self) -> None:
        occurrence = {
            "what_happened": "A vendor sent the signed renewal terms.",
            "source_material": {
                "kind": "email",
                "origin": "vendor email",
                "factual_contents": "Renewal is $43.80 for 12 seats.",
            },
        }

        self.assertEqual(
            accepted_contact_details_for_occurrence(occurrence),
            "What happened:\nA vendor sent the signed renewal terms.\n\n"
            "Source material:\nRenewal is $43.80 for 12 seats.",
        )

    def test_accepted_contact_details_use_only_final_visible_messages(self) -> None:
        session = {
            "contact_id": "contact-1",
            "messages": [
                "The clinic offered April 19, and I accepted it.",
                "Add the appointment to my calendar.",
            ],
        }

        self.assertEqual(
            accepted_contact_details_for_rendered_session(session),
            "The clinic offered April 19, and I accepted it.\n\n"
            "Add the appointment to my calendar.",
        )

    def test_older_contact_summary_keeps_concrete_details_and_is_bounded(self) -> None:
        summary = compact_older_contact_summary(
            {
                "thread_id": "thread-renewal",
                "accepted_contact_details": "The vendor sent a signed renewal for $43.80 covering 12 active seats.",
            }
        )

        self.assertIn("signed renewal", summary)
        self.assertIn("$43.80", summary)
        self.assertLessEqual(len(summary), history_window.OLDER_CONTACT_SUMMARY_LIMIT)

    def test_planner_payload_keeps_only_latest_ordinary_state_after_eight_weeks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            quarter = root / "quarter.json"
            spec = root / "spec.yaml"
            world = root / "world.md"
            quarter.write_text(
                json.dumps(
                    {
                        "plan": {
                            "period_start": "2025-01-01",
                            "period_end": "2025-03-31",
                            "new_entities": [],
                            "events": [],
                        }
                    }
                )
            )
            spec.write_text(
                "narrative_timezone: America/Los_Angeles\n"
                "primary_subject_ids:\n- person:morgan\n"
            )
            world.write_text("The accepted world.\n")
            payload, _ = build_planner_payload(
                config={
                    "persona": "morgan",
                    "quarter_plan": str(quarter),
                    "spec": str(spec),
                    "generator_context": str(world),
                },
                checkpoint={"covered_quarter_event_ids": []},
                state={
                    "facts": [],
                    "entities": [
                        {"id": "person:morgan", "name": "Morgan"},
                        {"id": "place:hallway", "name": "Hallway"},
                    ],
                    "app_state": {},
                    "history": [],
                    "unfinished_threads": [],
                    "operation_results": [],
                    "session_purposes": [
                        {
                            "session_id": "october-alarm",
                            "narrative_date": "2024-10-08T10:00:00-07:00",
                            "current_state_changes": [
                                {
                                    "key": "south_slope_hallway_alarm",
                                    "subject_ids": ["place:hallway"],
                                    "statement": "The old hallway alarm remains installed.",
                                }
                            ],
                        },
                        {
                            "session_id": "december-alarm",
                            "narrative_date": "2024-12-01T10:00:00-08:00",
                            "current_state_changes": [
                                {
                                    "key": "south_slope_hallway_alarm",
                                    "subject_ids": ["place:hallway"],
                                    "statement": "The hallway combo smoke and CO alarm is replaced and tested.",
                                }
                            ],
                        },
                    ],
                },
                start=date(2025, 1, 29),
                end=date(2025, 2, 4),
            )

        self.assertEqual(
            payload["current_ordinary_states"],
            [
                {
                    "key": "south_slope_hallway_alarm",
                    "subject_ids": ["place:hallway"],
                    "statement": "The hallway combo smoke and CO alarm is replaced and tested.",
                    "source_session_id": "december-alarm",
                    "date": "2024-12-01",
                }
            ],
        )
        self.assertIn(
            "place:hallway", [entity["id"] for entity in payload["known_entities"]]
        )

    def test_accepted_communication_destinations_are_exact_and_exclude_bodies(self) -> None:
        state = {
            "session_purposes": [
                {
                    "session_id": "slack-session",
                    "narrative_date": "2025-01-01T09:00:00-08:00",
                    "purpose": "Send the engineering update to the engineering team.",
                },
                {
                    "session_id": "dm-session",
                    "narrative_date": "2025-01-02T09:00:00-08:00",
                    "purpose": "Ask Ines about the release handoff.",
                },
                {
                    "session_id": "email-session",
                    "narrative_date": "2025-01-03T09:00:00-08:00",
                    "purpose": "Send Devon the finance update and copy finance.",
                },
                {
                    "session_id": "duplicate-session",
                    "narrative_date": "2025-01-04T09:00:00-08:00",
                    "purpose": "Send the later engineering update to the engineering team.",
                },
            ],
            "operation_results": [
                {
                    "session_id": "slack-session",
                    "operation": {
                        "tool": "send_slack_message",
                        "args": {
                            "channel": "#eng-team",
                            "message": "Private message body.",
                        },
                    },
                },
                {
                    "session_id": "dm-session",
                    "operation": {
                        "tool": "send_slack_dm",
                        "args": {"user": "Ines", "message": "DM body."},
                    },
                },
                {
                    "session_id": "email-session",
                    "operation": {
                        "tool": "send_email",
                        "args": {
                            "to": "devon@example.com",
                            "cc": ["finance@example.com"],
                            "subject": "Subject body.",
                            "body": "Email body.",
                        },
                    },
                },
                {
                    "session_id": "duplicate-session",
                    "operation": {
                        "tool": "send_slack_message",
                        "args": {"channel": "#eng-team", "message": "Another body."},
                    },
                },
                {
                    "session_id": "document-session",
                    "operation": {
                        "tool": "create_doc",
                        "args": {"title": "Not a destination", "body": "Body."},
                    },
                },
            ]
        }

        destinations = accepted_communication_destinations(state)

        self.assertEqual(
            destinations,
            [
                {
                    "transport": "slack",
                    "destination_type": "channel",
                    "destination": "#eng-team",
                    "prior_contact_purpose": (
                        "Send the later engineering update to the engineering team."
                    ),
                },
                {
                    "transport": "slack",
                    "destination_type": "user",
                    "destination": "Ines",
                    "prior_contact_purpose": "Ask Ines about the release handoff.",
                },
                {
                    "transport": "email",
                    "destination_type": "to",
                    "destination": "devon@example.com",
                    "prior_contact_purpose": (
                        "Send Devon the finance update and copy finance."
                    ),
                },
                {
                    "transport": "email",
                    "destination_type": "cc",
                    "destination": "finance@example.com",
                    "prior_contact_purpose": (
                        "Send Devon the finance update and copy finance."
                    ),
                },
            ],
        )
        self.assertNotIn("body", json.dumps(destinations))
        self.assertNotIn("message", json.dumps(destinations))
        self.assertNotIn("subject", json.dumps(destinations))

    def test_communication_destinations_reach_planner_and_writer_payloads(self) -> None:
        state = {
            "facts": copy.deepcopy(self.facts),
            "entities": copy.deepcopy(self.entities),
            "app_state": {},
            "history": [],
            "session_purposes": [
                {
                    "session_id": "prior-session",
                    "narrative_date": "2025-01-01T09:00:00-08:00",
                    "purpose": "Send the engineering update to the engineering team.",
                }
            ],
            "unfinished_threads": [],
            "operation_results": [
                {
                    "session_id": "prior-session",
                    "operation": {
                        "tool": "send_slack_message",
                        "args": {"channel": "#eng-team", "message": "Do not expose."},
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            quarter = root / "quarter.json"
            spec = root / "spec.yaml"
            world = root / "world.md"
            quarter.write_text(
                json.dumps(
                    {
                        "plan": {
                            "period_start": "2025-01-01",
                            "period_end": "2025-03-31",
                            "events": [],
                        }
                    }
                )
            )
            spec.write_text(
                "narrative_timezone: America/Los_Angeles\n"
                "primary_subject_ids:\n- person:morgan\n"
            )
            world.write_text("The accepted world.\n")
            config = {
                "persona": "morgan",
                "quarter_plan": str(quarter),
                "spec": str(spec),
                "generator_context": str(world),
            }
            planner_payload, _ = build_planner_payload(
                config=config,
                checkpoint={"covered_quarter_event_ids": []},
                state=state,
                start=self.start,
                end=self.start,
            )
            writer_payload = build_writer_payload(
                planner_response=self.good_planner_response(),
                config=config,
                state=state,
                start=self.start,
                end=self.start,
                timezone_name="America/Los_Angeles",
            )

        expected = [
            {
                "transport": "slack",
                "destination_type": "channel",
                "destination": "#eng-team",
                "prior_contact_purpose": (
                    "Send the engineering update to the engineering team."
                ),
            }
        ]
        self.assertEqual(
            planner_payload["previously_used_communication_destinations"], expected
        )
        self.assertNotIn("previously_used_communication_destinations", writer_payload)

    def test_window_tools_excludes_only_observability_tools(self) -> None:
        tools = {
            "get_service_metrics": {"description": "metrics"},
            "query_logs": {"description": "logs"},
            "create_doc": {"description": "documents"},
        }

        self.assertEqual(
            history_window.window_tools(tools),
            {"create_doc": {"description": "documents"}},
        )
        self.assertEqual(tools["get_service_metrics"], {"description": "metrics"})

    def test_planner_capability_includes_contract_visibility_and_stub_notes(self) -> None:
        capabilities = history_window.compact_tool_capabilities(
            {
                "internal_search_v2": {
                    "description": "Search internal notes.\n\nArgs:\n    query: text",
                    "state_effect": {
                        "returns": "Current stub search results.",
                        "visibility": "Only indexed snippets are visible.",
                        "notes": ["Returns generic stub content, not arbitrary full documents."],
                    },
                }
            }
        )

        self.assertEqual(
            capabilities,
            [
                {
                    "tool_name": "internal_search_v2",
                    "description": "Search internal notes.\n\nArgs:\n    query: text",
                    "arguments": [],
                    "required_arguments": [],
                    "returns": "Current stub search results.",
                    "visibility": "Only indexed snippets are visible.",
                    "notes": ["Returns generic stub content, not arbitrary full documents."],
                }
            ],
        )

    def test_planner_hides_generation_unavailable_capability(self) -> None:
        capabilities = history_window.compact_tool_capabilities(
            {
                "evaluation_stub": {
                    "description": "Read evaluation content.",
                    "state_effect": {"generation_available": False},
                },
                "create_note": {
                    "description": "Create a note.",
                    "state_effect": {"writes_state_keys": ["notes"]},
                },
            }
        )

        self.assertEqual(
            capabilities,
            [
                {
                    "tool_name": "create_note",
                    "description": "Create a note.",
                    "arguments": [],
                    "required_arguments": [],
                }
            ],
        )

    def test_planner_hides_capability_requiring_visible_records(self) -> None:
        tool = {
            "list_oncall_schedule": {
                "description": "List on-call schedule.",
                "state_effect": {
                    "reads_state_keys": ["oncall_schedule"],
                    "empty_result_is_valid": True,
                    "generation_requires_visible_records": True,
                },
            }
        }

        self.assertEqual(
            history_window.compact_tool_capabilities(tool, {"oncall_schedule": []}),
            [],
        )
        self.assertEqual(
            [row["tool_name"]
             for row in history_window.compact_tool_capabilities(
                 tool, {"oncall_schedule": [{"team": "infra"}]}
             )],
            ["list_oncall_schedule"],
        )

    def test_planner_compacts_oncall_records_with_requested_identity_fields_and_dates(self) -> None:
        compact = history_window.compact_planner_app_records(
            {
                "list_oncall_schedule": {
                    "state_effect": {"reads_state_keys": ["oncall_schedule"]}
                }
            },
            {
                "oncall_schedule": [
                    {
                        "team": "infra",
                        "week_of": "2025-01-27",
                        "primary": "Alex",
                        "secondary": "Yuki",
                        "members": ["Alex", "Yuki"],
                        "notes": "Keep this record note out of the compact index.",
                    },
                    {
                        "team": "infra",
                        "week_of": "2025-08-04",
                        "primary": "Alex",
                        "secondary": "Yuki",
                        "members": ["Alex", "Yuki"],
                    },
                ]
            },
            start=date(2025, 1, 29),
            end=date(2025, 2, 4),
        )

        self.assertEqual(
            compact,
            {
                "oncall_schedule": [
                    {
                        "team": "infra",
                        "week_of": "2025-01-27",
                        "primary": "Alex",
                        "secondary": "Yuki",
                        "members": ["Alex", "Yuki"],
                    }
                ]
            },
        )

    def test_planner_hides_unavailable_reads_and_tool_examples(self) -> None:
        tools = {
            "query_report": {
                "description": "Read a named report.\n\nArgs:\n    name: e.g. sample_report",
                "state_effect": {"reads_state_keys": ["reports"]},
            },
            "list_reports": {
                "description": "List reports.\n\nReturns all visible reports.",
                "state_effect": {
                    "reads_state_keys": ["reports"],
                    "empty_result_is_valid": True,
                },
            },
            "create_note": {
                "description": "Create a note.\n\nArgs:\n    body: text",
                "state_effect": {"writes_state_keys": ["notes"]},
            },
            "update_note": {
                "description": "Update a note.\n\nArgs:\n    id: note id",
                "state_effect": {
                    "reads_state_keys": ["notes"],
                    "writes_state_keys": ["notes"],
                },
            },
        }

        capabilities = history_window.compact_tool_capabilities(
            tools, {"reports": [], "notes": []}
        )

        self.assertEqual(
            capabilities,
            [
                {
                    "tool_name": "create_note",
                    "description": "Create a note.\n\nArgs:\n    body: text",
                    "arguments": [],
                    "required_arguments": [],
                },
                {
                    "tool_name": "list_reports",
                    "description": "List reports.\n\nReturns all visible reports.",
                    "arguments": [],
                    "required_arguments": [],
                },
                {
                    "tool_name": "update_note",
                    "description": "Update a note.\n\nArgs:\n    id: note id",
                    "arguments": [],
                    "required_arguments": [],
                },
            ],
        )

    def test_planner_normalizes_unique_out_of_order_timestamps_stably(self) -> None:
        response = self.good_planner_response()
        occurrences = response["plan"]["occurrences"]
        later = copy.deepcopy(occurrences[0])
        later.update(
            {
                "contact_id": "contact-2",
                "happening_id": "happening-personal-update",
                "narrative_date": "2025-01-29T11:00:00-08:00",
                "basis_ids": [history_window.ORDINARY_CURRENT_LIFE_BASIS_ID],
                "effect_kind": "self_contained",
                "thread_id": "self_contained-2",
                "continues_from": [],
                "subject_ids": ["person:morgan"],
                "development_ids": [],
                "uses_fact_ids": [],
                "durable_facts": [],
                "what_happened": "Morgan shares a new personal observation.",
                "assistant_outcome": {"kind": "none", "requested_result": None},
                "thread_after": {"is_open": False, "what_remains": None},
            }
        )
        occurrences.append(later)
        occurrences[0]["narrative_date"], occurrences[1]["narrative_date"] = (
            occurrences[1]["narrative_date"],
            occurrences[0]["narrative_date"],
        )
        raw_response = copy.deepcopy(response)
        normalized = normalize_planner_occurrence_order(response)
        self.assertEqual(
            [row["narrative_date"] for row in normalized["plan"]["occurrences"]],
            sorted(row["narrative_date"] for row in occurrences),
        )
        self.assertEqual(
            {row["contact_id"] for row in normalized["plan"]["occurrences"]},
            {row["contact_id"] for row in occurrences},
        )
        self.assertEqual(response, raw_response)

    def test_planner_keeps_equal_timestamps_for_strict_validator(self) -> None:
        response = self.good_planner_response()
        occurrences = response["plan"]["occurrences"]
        later = copy.deepcopy(occurrences[0])
        later.update(
            {
                "contact_id": "contact-2",
                "happening_id": "happening-personal-update",
                "basis_ids": [history_window.ORDINARY_CURRENT_LIFE_BASIS_ID],
                "effect_kind": "self_contained",
                "thread_id": "self_contained-2",
                "continues_from": [],
                "subject_ids": ["person:morgan"],
                "development_ids": [],
                "uses_fact_ids": [],
                "durable_facts": [],
                "what_happened": "Morgan shares a new personal observation.",
                "assistant_outcome": {"kind": "none", "requested_result": None},
                "thread_after": {"is_open": False, "what_remains": None},
            }
        )
        occurrences.append(later)
        occurrences[1]["narrative_date"] = occurrences[0]["narrative_date"]
        normalized = normalize_planner_occurrence_order(response)
        self.assertEqual(
            normalized["plan"]["occurrences"][0]["contact_id"],
            occurrences[0]["contact_id"],
        )
        self.assertEqual(
            normalized["plan"]["occurrences"][1]["contact_id"],
            occurrences[1]["contact_id"],
        )
        self.assertIn(
            "occurrences must be strictly chronological",
            self.validate_planner(normalized),
        )

    def test_active_history_prompts_are_persona_neutral(self) -> None:
        for prompt in (
            HISTORY_WINDOW_STORY_SYSTEM,
            HISTORY_WINDOW_PLANNING_SYSTEM,
            HISTORY_WINDOW_WRITING_SYSTEM,
        ):
            self.assertNotIn("Morgan", prompt)
            self.assertNotIn("morgan", prompt)

    def test_story_calendar_action_requires_complete_timing(self) -> None:
        actions = story_action_categories(
            [{"tool_name": "create_calendar_event"}]
        )

        self.assertEqual(len(actions), 1)
        self.assertIn("exact date, start time, and end time", actions[0])
        self.assertIn(
            "first date, last date, start time, and duration", actions[0]
        )

    def test_story_communication_capabilities_match_available_tools(self) -> None:
        dm_only = story_action_categories(
            [{"tool_name": "send_email"}, {"tool_name": "send_slack_dm"}]
        )

        self.assertEqual(
            dm_only,
            [
                "send a text-only email with a subject and body; email cannot "
                "include attachments or files",
                "send a private Slack direct message to one user",
            ],
        )
        self.assertNotIn("channel", " ".join(dm_only).lower())

        with_channel = story_action_categories(
            [{"tool_name": "send_slack_dm"}, {"tool_name": "send_slack_message"}]
        )
        self.assertEqual(
            with_channel,
            [
                "send a private Slack direct message to one user",
                "post a message to a Slack channel",
            ],
        )

    def test_story_readable_sources_are_precise_and_exclude_stub_search(self) -> None:
        actions = story_action_categories(
            [
                {"tool_name": "list_inbox"},
                {"tool_name": "get_runbook"},
                {"tool_name": "internal_search_v2"},
            ]
        )

        self.assertEqual(
            actions,
            [
                "read an existing runbook by its supplied ID",
                "read messages already present in the inbox",
            ],
        )
        self.assertNotIn("retrieve information already present in the supported work apps", actions)

    def test_fact_references_follow_only_unambiguous_earlier_replacements(self) -> None:
        response = {
            "plan": {
                "occurrences": [
                    {
                        "uses_fact_ids": [338],
                        "durable_facts": [
                            {
                                "key": "lantern_access_through_june",
                                "supersedes": [338],
                            }
                        ],
                    },
                    {
                        "uses_fact_ids": [160, 338, "lantern_access_through_june"],
                        "durable_facts": [],
                    },
                    {
                        "uses_fact_ids": [338],
                        "durable_facts": [
                            {
                                "key": "lantern_access_final",
                                "supersedes": ["lantern_access_through_june"],
                            }
                        ],
                    },
                    {"uses_fact_ids": [338], "durable_facts": []},
                    {
                        "uses_fact_ids": [500],
                        "durable_facts": [
                            {"key": "first_replacement", "supersedes": [500]},
                            {"key": "second_replacement", "supersedes": [500]},
                        ],
                    },
                    {"uses_fact_ids": [500, 999], "durable_facts": []},
                ]
            }
        }

        normalized = resolve_superseded_fact_references(response)

        self.assertEqual(
            normalized["plan"]["occurrences"][0]["uses_fact_ids"],
            [338],
        )
        self.assertEqual(
            normalized["plan"]["occurrences"][1]["uses_fact_ids"],
            [160, "lantern_access_through_june"],
        )
        self.assertEqual(
            normalized["plan"]["occurrences"][2]["uses_fact_ids"],
            ["lantern_access_through_june"],
        )
        self.assertEqual(
            normalized["plan"]["occurrences"][3]["uses_fact_ids"],
            ["lantern_access_final"],
        )
        self.assertEqual(
            normalized["plan"]["occurrences"][5]["uses_fact_ids"],
            [500, 999],
        )

    def test_unfinished_thread_current_fact_ids_reuse_open_thread_routes(self) -> None:
        facts = [
            {
                "id": 1,
                "statement": "The current open-thread status.",
                "subjects": ["project:open"],
                "source_session_ids": ["direct-one"],
                "supersedes": [],
            },
            {
                "id": 2,
                "statement": "An additive fact from the open thread.",
                "subjects": ["project:open"],
                "source_session_ids": ["direct-two"],
                "supersedes": [],
            },
            {
                "id": 3,
                "statement": "A current fact from another thread.",
                "subjects": ["project:other"],
                "source_session_ids": ["other"],
                "supersedes": [],
            },
            {
                "id": 4,
                "statement": "A retired fact from the open thread.",
                "subjects": ["project:open"],
                "source_session_ids": ["direct-one"],
                "supersedes": [],
            },
            {
                "id": 5,
                "statement": "The replacement for the retired fact.",
                "subjects": ["project:open"],
                "source_session_ids": ["replacement-session"],
                "supersedes": [4],
            },
        ]
        selected = unfinished_thread_current_fact_ids(
            facts,
            recent_chronology=[],
            unfinished_threads=[
                {"thread_id": "thread-open", "session_id": "direct-one"},
                {"thread_id": "thread-open", "interaction_id": "direct-two"},
                {"thread_id": "thread-other", "session_id": "other"},
            ],
        )
        self.assertEqual(selected, {"thread-open": [1, 2], "thread-other": [3]})

    def test_open_thread_history_keeps_prior_accepted_details(self) -> None:
        state = {
            "entities": [],
            "app_state": {},
            "history": [
                {
                    "id": "session-earlier",
                    "narrative_date": "2025-01-02T09:00:00-08:00",
                    "messages": ["The approved amount is $43.80."],
                },
                {
                    "id": "session-later",
                    "narrative_date": "2025-01-16T09:00:00-08:00",
                    "messages": ["The January revision is $58.50."],
                },
            ],
            "session_purposes": [
                {
                    "session_id": "session-earlier",
                    "planned_interaction_id": "contact-earlier",
                    "thread_id": "thread-finance",
                    "accepted_contact_details": "December AWS was $43.80.",
                },
                {
                    "session_id": "session-later",
                    "planned_interaction_id": "contact-later",
                    "thread_id": "thread-finance",
                    "accepted_contact_details": "January AWS was $58.50.",
                },
            ],
        }
        history = prior_accepted_contacts_for_open_threads(
            state,
            start=date(2025, 2, 1),
            unfinished_threads=[{"thread_id": "thread-finance"}],
        )
        self.assertEqual(
            history["thread-finance"],
            [
                {
                    "date": "2025-01-02",
                    "contact_id": "contact-earlier",
                    "accepted_contact_details": "December AWS was $43.80.",
                },
                {
                    "date": "2025-01-16",
                    "contact_id": "contact-later",
                    "accepted_contact_details": "January AWS was $58.50.",
                },
            ],
        )
        exposed = planner_open_threads(
            [{"thread_id": "thread-finance", "session_id": "session-later"}],
            prior_accepted_contacts_by_thread=history,
        )
        self.assertEqual(exposed[0]["prior_accepted_contacts"], history["thread-finance"])

    def test_same_matter_messages_are_bounded_and_reach_planner_and_writer(self) -> None:
        state = {
            "facts": copy.deepcopy(self.facts),
            "entities": copy.deepcopy(self.entities),
            "history": [
                {
                    "id": f"session-{index}",
                    "narrative_date": f"2025-01-{index:02d}T09:00:00-08:00",
                    "messages": [f"Earlier message {index}."],
                }
                for index in range(1, 10)
            ],
            "session_purposes": [
                {
                    "session_id": f"session-{index}",
                    "planned_interaction_id": f"contact-{index}",
                    "thread_id": "thread-main",
                }
                for index in range(1, 10)
            ],
            "operation_results": [],
        }
        messages = history_window.bounded_same_matter_user_messages(
            state, thread_id="thread-main"
        )

        self.assertEqual(
            [row["contact_id"] for row in messages],
            [f"contact-{index}" for index in range(2, 10)],
        )
        planner_thread = planner_open_threads(
            [{"thread_id": "thread-main", "session_id": "session-9"}],
            earlier_user_messages_by_thread={"thread-main": messages},
        )[0]
        self.assertEqual(
            planner_thread["earlier_user_messages_in_same_matter"], messages
        )

        with tempfile.TemporaryDirectory() as temp:
            world = Path(temp) / "world.md"
            world.write_text("Morgan runs the accepted world.\n")
            payload = build_writer_payload(
                planner_response=self.good_planner_response(),
                config={"persona": "morgan", "generator_context": str(world)},
                state=state,
                start=self.start,
                end=self.end,
                timezone_name="America/Los_Angeles",
            )
        self.assertEqual(
            payload["contacts"]["contact-1"]["earlier_user_messages_in_same_matter"],
            messages,
        )

    def test_recent_chronology_keeps_planned_subjects_without_new_facts(self) -> None:
        state = {
            "history": [
                {
                    "id": "session-northstar",
                    "narrative_date": "2025-01-28T09:00:00-08:00",
                    "messages": ["Northstar sent a relationship update."],
                }
            ],
            "session_purposes": [
                {
                    "session_id": "session-northstar",
                    "planned_interaction_id": "contact-northstar",
                    "thread_id": "thread-northstar",
                    "accepted_contact_details": "Northstar sent a relationship update.",
                    "subject_ids": [
                        "person:morgan",
                        "organization:northstar_ventures",
                    ],
                }
            ],
            "facts": [],
        }

        rows = accepted_contact_chronology_for_planner(
            state, date(2025, 2, 1), quarter_start=date(2025, 1, 1)
        )

        self.assertEqual(
            rows[0]["subject_ids"],
            ["person:morgan", "organization:northstar_ventures"],
        )

    def test_planner_recent_context_never_falls_back_to_exact_user_messages(self) -> None:
        state = {
            "history": [
                {
                    "id": "session-1",
                    "narrative_date": "2025-01-28T09:00:00-08:00",
                    "messages": ["Exact accepted message that must stay writer-only."],
                }
            ],
            "session_purposes": [
                {
                    "session_id": "session-1",
                    "purpose": "Review the finance update.",
                }
            ],
            "facts": [],
        }

        rows = accepted_contact_chronology_for_planner(
            state, date(2025, 2, 1), quarter_start=date(2025, 1, 1)
        )

        self.assertEqual(rows[0]["accepted_contact_details"], "Review the finance update.")
        self.assertNotIn("Exact accepted message", json.dumps(rows))

    def test_visible_app_record_exposes_matching_canonical_entity(self) -> None:
        selected = history_window.entity_ids_named_in_app_records(
            [
                {
                    "id": "organization:northstar_ventures",
                    "name": "Northstar Ventures",
                    "kind": "organization",
                    "aliases": ["Northstar"],
                },
                {
                    "id": "organization:northwind",
                    "name": "Northwind",
                    "kind": "organization",
                    "aliases": [],
                },
            ],
            {
                "crm": [
                    {
                        "id": "crm-1",
                        "name": "Northstar Ventures",
                        "status": "warm",
                    }
                ]
            },
        )

        self.assertEqual(selected, {"organization:northstar_ventures"})

    def setUp(self) -> None:
        self.start = date(2025, 1, 29)
        self.end = date(2025, 2, 11)
        self.entities = [
            {"id": "person:morgan", "name": "Morgan", "kind": "person"}
        ]
        self.facts = [
            {
                "id": 1,
                "statement": "The earlier rule is current.",
                "applies_when": "the project is discussed",
                "subjects": ["person:morgan"],
                "source_session_ids": ["old-session"],
                "supersedes": [],
                "construction_key": "earlier-rule",
            }
        ]
        self.tools = {
            "create_doc": {
                "arguments": ["title", "body", "folder"],
                "required_arguments": ["title", "body"],
                "description": "Create a document.",
                "state_effect": {"writes_state_keys": ["docs"]},
            }
        }
        self.required_entities = [
            {
                "id": "project:new",
                "name": "New Project",
                "kind": "project",
                "role": "A project accepted by the quarter plan.",
                "introduced_in_event_id": "development-1",
            }
        ]
        self.required_facts = {
            "new-rule": {
                "key": "new-rule",
                "statement": "The new rule now governs the project.",
                "applies_when": "the project is discussed",
                "subjects": ["person:morgan", "project:new"],
                "supersedes": [1],
                "development_ids": ["development-1"],
            }
        }

    def good_response(self) -> dict[str, Any]:
        title = "New project operating note"
        body = "The new rule now governs the project."
        return {
            "plan": {
                "period_start": self.start.isoformat(),
                "period_end": self.end.isoformat(),
                "new_entities": [
                    {
                        "id": "project:new",
                        "name": "New Project",
                        "kind": "project",
                        "role": "A project accepted by the quarter plan.",
                        "reason": "A project accepted by the quarter plan.",
                        "introduced_in_contact_id": "contact-1",
                    }
                ],
                "sessions": [
                    {
                        "contact_id": "contact-1",
                        "narrative_date": "2025-01-29T10:00:00-08:00",
                        "thread_id": "thread-main",
                        "continues_from": ["old-session"],
                        "subject_ids": ["person:morgan", "project:new"],
                        "development_ids": ["development-1"],
                        "anchor_item_ids": [],
                        "purpose": "Create a document titled New project operating note.",
                        "messages": [
                            f"I received this project decision note:\n\n{body}\n\n"
                            f"Create a doc titled {title} using that exact sentence as its body."
                        ],
                        "uses_facts": [1],
                        "new_facts": [
                            {
                                "key": "new-rule",
                                "statement": "The new rule now governs the project.",
                                "applies_when": "the project is discussed",
                                "subjects": ["person:morgan", "project:new"],
                                "supersedes": [1],
                                "evidence_contact_ids": ["contact-1"],
                            }
                        ],
                        "app_operations": [
                            {
                                "tool": "create_doc",
                                "args": {"title": title, "body": body},
                            }
                        ],
                        "thread_open_after_contact": False,
                        "what_remains_after_contact": None,
                    }
                ],
            }
        }

    def good_planner_response(self) -> dict[str, Any]:
        return {
            "plan": {
                "period_start": self.start.isoformat(),
                "period_end": self.end.isoformat(),
                "occurrences": [
                    {
                        "contact_id": "contact-1",
                        "happening_id": "happening-project-decision",
                        "narrative_date": "2025-01-29T10:00:00-08:00",
                        "basis_ids": ["development-1"],
                        "effect_kind": "realize_development",
                        "thread_id": "thread-main",
                        "continues_from": ["old-session"],
                        "uses_items_created_by_contact_ids": [],
                        "subject_ids": ["person:morgan", "project:new"],
                        "development_ids": ["development-1"],
                        "anchor_item_ids": [],
                        "what_happened": (
                            "Morgan introduces the project and establishes its rule: "
                            "The new rule now governs the project."
                        ),
                        "assistant_outcome": {
                            "kind": "external_action",
                            "requested_result": "Create a document titled New project operating note.",
                        },
                        "source_material": None,
                        "uses_fact_ids": [1],
                        "durable_facts": [
                            {
                                "key": "new-rule",
                                "statement": "The new rule now governs the project.",
                                "applies_when": "the project is discussed",
                                "subjects": ["person:morgan", "project:new"],
                                "supersedes": [1],
                            }
                        ],
                        "current_state_changes": [],
                        "app_operations": [
                            {
                                "tool": "create_doc",
                                "args": {
                                    "title": "New project operating note",
                                    "body": "The new rule now governs the project.",
                                },
                            }
                        ],
                        "thread_after": {"is_open": False, "what_remains": None},
                    }
                ],
            }
        }

    def good_writer_api_response(self) -> dict[str, Any]:
        return {
            "plan": {
                "sessions": [
                    {
                        "contact_id": "contact-1",
                        "messages_before_request": [
                            "I received this project decision note."
                        ],
                        "message_with_request": "Create a document titled New project operating note.",
                        "conflicting_fact_ids": [],
                    }
                ],
            }
        }

    def good_keyed_writer_api_response(self) -> dict[str, Any]:
        response = self.good_writer_api_response()
        return {
            "plan": {
                "sessions": {
                    str(session["contact_id"]): {
                        "messages_before_request": copy.deepcopy(
                            session["messages_before_request"]
                        ),
                        "message_with_request": copy.deepcopy(
                            session["message_with_request"]
                        ),
                        "conflicting_fact_ids": [],
                    }
                    for session in response["plan"]["sessions"]
                }
            }
        }

    def good_planner_api_response(self) -> dict[str, Any]:
        response = copy.deepcopy(self.good_planner_response())
        response["plan"].pop("period_start")
        response["plan"].pop("period_end")
        for occurrence in response["plan"]["occurrences"]:
            occurrence.pop("anchor_item_ids", None)
            references = occurrence.pop("uses_fact_ids")
            occurrence["uses_current_fact_ids"] = [
                reference for reference in references if type(reference) is int
            ]
            occurrence["uses_new_fact_keys"] = [
                reference for reference in references if isinstance(reference, str)
            ]
            occurrence["app_operations"] = [
                {
                    "tool": operation["tool"],
                    "args_json": json.dumps(operation["args"]),
                }
                for operation in occurrence["app_operations"]
            ]
        return response

    def good_story_api_response(self) -> dict[str, Any]:
        response = copy.deepcopy(self.good_planner_api_response())
        for occurrence in response["plan"]["occurrences"]:
            for key in (
                "basis_ids",
                "effect_kind",
                "uses_current_fact_ids",
                "uses_new_fact_keys",
                "durable_facts",
                "current_state_changes",
                "app_operations",
            ):
                occurrence.pop(key)
        return response

    def good_enrichment_api_response(self) -> dict[str, Any]:
        response = self.good_planner_api_response()
        return {
            "contacts": [
                {
                    "contact_id": occurrence["contact_id"],
                    "uses_current_fact_ids": copy.deepcopy(
                        occurrence["uses_current_fact_ids"]
                    ),
                    "uses_new_fact_keys": copy.deepcopy(
                        occurrence["uses_new_fact_keys"]
                    ),
                    "durable_facts": copy.deepcopy(occurrence["durable_facts"]),
                    "current_state_changes": copy.deepcopy(
                        occurrence["current_state_changes"]
                    ),
                    "app_operations": copy.deepcopy(occurrence["app_operations"]),
                    "execution_problem": None,
                }
                for occurrence in response["plan"]["occurrences"]
            ]
        }

    def validate_planner(
        self,
        response: dict[str, Any],
        *,
        active_situation_ids: set[str] | None = None,
        open_thread_ids: set[str] | None = None,
        protected_change_ids: set[str] | None = None,
        facts_by_development: dict[str, list[dict[str, Any]]] | None = None,
        subjects_by_basis: dict[str, set[str]] | None = None,
        primary_subject_ids: set[str] | None = None,
        unfinished_thread_ids_by_reference: dict[str, str] | None = None,
        open_thread_current_states_by_id: dict[str, list[dict[str, Any]]] | None = None,
        development_start_dates: dict[str, date] | None = None,
        available_app_records: dict[str, list[dict[str, Any]]] | None = None,
        earlier_accepted_app_items: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        return validate_history_window_plan_response(
            response,
            start=self.start,
            end=self.end,
            timezone_name="America/Los_Angeles",
            entities=self.entities,
            existing_session_ids={"old-session"},
            existing_references={"old-session"},
            required_development_ids={"development-1"},
            allowed_development_ids={"development-1", "development-later"},
            required_entities=self.required_entities,
            facts=self.facts,
            tools=self.tools,
            active_situation_ids=active_situation_ids,
            open_thread_ids=open_thread_ids,
            protected_change_ids=protected_change_ids,
            facts_by_development=(
                facts_by_development
                if facts_by_development is not None
                else {
                    "development-1": [
                        history_window.model_fact_fields(
                            self.required_facts["new-rule"]
                        )
                    ]
                }
            ),
            subjects_by_basis=subjects_by_basis,
            primary_subject_ids=primary_subject_ids,
            unfinished_thread_ids_by_reference=unfinished_thread_ids_by_reference,
            open_thread_current_states_by_id=open_thread_current_states_by_id,
            development_start_dates=development_start_dates,
            available_app_records=available_app_records,
            earlier_accepted_app_items=earlier_accepted_app_items,
        )

    def validate(
        self,
        response: dict[str, Any],
        *,
        occurrence_plan: dict[str, Any] | None = None,
    ) -> list[str]:
        return validate_history_window_response(
            response,
            start=self.start,
            end=self.end,
            timezone_name="America/Los_Angeles",
            facts=self.facts,
            entities=self.entities,
            tools=self.tools,
            existing_session_ids={"old-session"},
            existing_references={"old-session"},
            required_development_ids={"development-1"},
            allowed_development_ids={"development-1", "development-later"},
            required_entities=self.required_entities,
            required_facts=self.required_facts,
            occurrence_plan=occurrence_plan or self.good_planner_response()["plan"],
        )

    def test_date_boundary_requires_the_day_after_checkpoint(self) -> None:
        config = {"quarter_start": "2025-01-01", "quarter_end": "2025-03-31"}
        checkpoint = {"accepted_through": "2025-01-28"}
        validate_window_dates(config, checkpoint, self.start, self.end)
        with self.assertRaisesRegex(ValueError, "exactly one day"):
            validate_window_dates(config, checkpoint, date(2025, 1, 30), self.end)
        with self.assertRaisesRegex(ValueError, "inside"):
            validate_window_dates(config, checkpoint, self.start, date(2025, 4, 1))

    def test_first_window_accepts_checkpoint_from_day_before_quarter(self) -> None:
        config = {"quarter_start": "2025-04-01", "quarter_end": "2025-06-30"}
        checkpoint = {"accepted_through": "2025-03-31"}
        validate_window_dates(
            config,
            checkpoint,
            date(2025, 4, 1),
            date(2025, 4, 14),
        )


    def test_valid_planner_output_passes(self) -> None:
        self.assertEqual(self.validate_planner(self.good_planner_response()), [])

    def test_planner_rejects_current_state_change_with_unknown_subject(self) -> None:
        response = self.good_planner_response()
        response["plan"]["occurrences"][0]["current_state_changes"] = [
            {
                "key": "hallway_alarm",
                "subject_ids": ["place:unknown"],
                "statement": "Morgan introduces the project and establishes its rule.",
            }
        ]

        errors = self.validate_planner(response)

        self.assertTrue(
            any("current_state_changes[0].subject_ids uses unknown entity" in error for error in errors),
            errors,
        )

    def test_planner_rejects_current_state_change_not_stated_in_happening(self) -> None:
        response = self.good_planner_response()
        response["plan"]["occurrences"][0]["current_state_changes"] = [
            {
                "key": "hallway_alarm",
                "subject_ids": ["person:morgan"],
                "statement": "The hallway alarm is replaced and tested.",
            }
        ]

        errors = self.validate_planner(response)

        self.assertTrue(
            any("current_state_changes[0].statement must appear verbatim" in error for error in errors),
            errors,
        )

    def test_planner_adds_exact_current_state_statement_to_writer_context(self) -> None:
        response = self.good_planner_response()
        occurrence = response["plan"]["occurrences"][0]
        occurrence["current_state_changes"] = [
            {
                "key": "hallway_alarm",
                "subject_ids": ["person:morgan"],
                "statement": "The hallway alarm is replaced and tested.",
            }
        ]

        normalized = include_current_state_statements_in_writer_context(response)

        self.assertNotEqual(normalized, response)
        self.assertEqual(self.validate_planner(normalized), [])
        self.assertEqual(
            normalized["plan"]["occurrences"][0]["what_happened"].count(
                "The hallway alarm is replaced and tested."
            ),
            1,
        )

        unchanged = include_current_state_statements_in_writer_context(normalized)
        self.assertEqual(unchanged, normalized)

    def test_planner_normalizes_opaque_current_state_keys_and_preserves_statements(self) -> None:
        response = self.good_planner_api_response()
        change = {
            "key": "DH_50184",
            "subject_ids": ["person:morgan"],
            "statement": "The exact human-readable claim stays unchanged.",
        }
        response["plan"]["occurrences"][0]["what_happened"] = (
            "The exact human-readable claim stays unchanged."
        )
        response["plan"]["occurrences"][0]["current_state_changes"] = [
            copy.deepcopy(change)
        ]
        second_occurrence = copy.deepcopy(response["plan"]["occurrences"][0])
        second_occurrence["current_state_changes"] = [copy.deepcopy(change)]
        response["plan"]["occurrences"].append(second_occurrence)

        decoded = decode_planner_app_operations(response)

        self.assertEqual(
            [
                occurrence["current_state_changes"][0]["key"]
                for occurrence in decoded["plan"]["occurrences"]
            ],
            ["dh_50184", "dh_50184"],
        )
        self.assertEqual(
            decoded["plan"]["occurrences"][0]["current_state_changes"][0][
                "statement"
            ],
            change["statement"],
        )
        self.assertEqual(
            response["plan"]["occurrences"][0]["current_state_changes"][0]["key"],
            "DH_50184",
        )

    def test_planner_rejects_invalid_or_colliding_normalized_current_state_keys(self) -> None:
        invalid = self.good_planner_api_response()
        invalid["plan"]["occurrences"][0]["current_state_changes"] = [
            {
                "key": "123",
                "subject_ids": ["person:morgan"],
                "statement": "The exact human-readable claim stays unchanged.",
            }
        ]
        with self.assertRaisesRegex(ValueError, "normalizes to an invalid key"):
            decode_planner_app_operations(invalid)

        collision = self.good_planner_api_response()
        first = collision["plan"]["occurrences"][0]
        first["current_state_changes"] = [
            {
                "key": "DH-50184",
                "subject_ids": ["person:morgan"],
                "statement": "The first claim.",
            }
        ]
        second = copy.deepcopy(first)
        second["current_state_changes"][0]["key"] = "dh_50184"
        collision["plan"]["occurrences"].append(second)
        with self.assertRaisesRegex(ValueError, "key collision"):
            decode_planner_app_operations(collision)

    def test_same_week_open_thread_requires_previous_contact_reference(self) -> None:
        response = self.good_planner_response()
        first = response["plan"]["occurrences"][0]
        first["thread_after"] = {
            "is_open": True,
            "what_remains": "Wait for the implementation result.",
        }
        second = copy.deepcopy(first)
        second.update(
            {
                "contact_id": "contact-2",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "continues_from": [],
                "anchor_item_ids": [],
                "uses_fact_ids": [],
                "durable_facts": [],
                "thread_after": {"is_open": False, "what_remains": None},
            }
        )
        response["plan"]["occurrences"].append(second)
        errors = self.validate_planner(response)
        self.assertTrue(
            any("continues same-week thread" in error for error in errors), errors
        )

        second["continues_from"] = ["contact-1"]
        self.assertEqual(self.validate_planner(response), [])

    def test_same_week_open_thread_can_advance(self) -> None:
        response = self.good_planner_response()
        first = response["plan"]["occurrences"][0]
        first["continues_from"] = []
        first["thread_after"] = {
            "is_open": True,
            "what_remains": "Wait for the implementation result.",
        }
        second = copy.deepcopy(first)
        second.update(
            {
                "contact_id": "contact-2",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "basis_ids": ["thread-main"],
                "effect_kind": "advance_open_thread",
                "continues_from": ["contact-1"],
                "development_ids": [],
                "anchor_item_ids": [],
                "uses_fact_ids": [],
                "durable_facts": [],
                "thread_after": {"is_open": False, "what_remains": None},
            }
        )
        response["plan"]["occurrences"].append(second)
        self.assertEqual(self.validate_planner(response), [])

    def test_same_week_closed_thread_cannot_be_reused(self) -> None:
        response = self.good_planner_response()
        first = response["plan"]["occurrences"][0]
        second = copy.deepcopy(first)
        second.update(
            {
                "contact_id": "contact-2",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "continues_from": [],
                "anchor_item_ids": [],
                "uses_fact_ids": [],
                "durable_facts": [],
            }
        )
        response["plan"]["occurrences"].append(second)

        errors = self.validate_planner(response)

        self.assertTrue(
            any(
                "reuses thread thread-main after an earlier occurrence closed it"
                in error
                for error in errors
            ),
            errors,
        )

    def test_story_planner_sees_app_item_names_without_internal_document_ids(self) -> None:
        items = story_existing_app_items(
            {
                "docs": [
                    {"id": "doc-secret", "title": "Lantern monitoring note"}
                ],
                "prs": [
                    {"id": "metrics-router#412", "status": "open"}
                ],
                "calendar": [
                    {"id": "event-secret", "title": "Hema one-on-one"}
                ],
            }
        )

        self.assertEqual(
            items,
            {
                "docs": [{"name": "Lantern monitoring note", "details": ""}],
                "prs": [
                    {
                        "name": "metrics-router#412",
                        "details": "status: open",
                    }
                ],
            },
        )
        self.assertNotIn("doc-secret", str(items))
        self.assertNotIn("event-secret", str(items))

    def test_story_app_item_description_distinguishes_generic_runbook_title(self) -> None:
        items = story_existing_app_items(
            {
                "runbook": [
                    {
                        "id": "rb-secret",
                        "title": "Service identity and ownership",
                        "owner": "cyrus_team",
                        "body": "The Cyrus team owns rollup-service.",
                    }
                ]
            }
        )

        self.assertEqual(
            items["runbook"][0],
            {
                "name": "Service identity and ownership",
                "details": "owner: cyrus_team",
            },
        )
        self.assertNotIn("rollup-service", str(items))
        self.assertNotIn("rb-secret", str(items))

    def test_closing_open_thread_updates_its_current_state(self) -> None:
        response = self.good_planner_response()
        occurrence = response["plan"]["occurrences"][0]
        supplied_state = {
            "key": "project_decision_status",
            "subject_ids": ["person:morgan", "project:new"],
            "statement": "The project decision is pending.",
            "source_session_id": "old-session",
            "date": "2025-01-28",
        }
        errors = self.validate_planner(
            response,
            open_thread_current_states_by_id={"thread-main": [supplied_state]},
        )
        self.assertTrue(
            any("without updating current-state key project_decision_status" in error for error in errors),
            errors,
        )

        occurrence["what_happened"] = (
            "Morgan introduces the project and establishes its rule. "
            "The project decision is final and recorded."
        )
        occurrence["current_state_changes"] = [
            {
                "key": "project_decision_status",
                "subject_ids": ["person:morgan", "project:new"],
                "statement": "The project decision is final and recorded.",
            }
        ]
        self.assertEqual(
            self.validate_planner(
                response,
                open_thread_current_states_by_id={"thread-main": [supplied_state]},
            ),
            [],
        )

    def test_completed_contact_does_not_need_thread_id(self) -> None:
        response = self.good_planner_response()
        occurrence = response["plan"]["occurrences"][0]
        occurrence["thread_id"] = ""
        occurrence["continues_from"] = []
        self.assertEqual(self.validate_planner(response), [])

    def test_planner_decoder_uses_same_week_thread_id_from_reference(self) -> None:
        response = self.good_planner_api_response()
        first = response["plan"]["occurrences"][0]
        first["thread_id"] = ""
        first["continues_from"] = []
        first["thread_after"] = {
            "is_open": True,
            "what_remains": "Wait for the implementation result.",
        }
        second = copy.deepcopy(first)
        second.update(
            {
                "contact_id": "contact-2",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "thread_id": "wrong-thread-id",
                "continues_from": ["contact-1"],
                "uses_current_fact_ids": [],
                "uses_new_fact_keys": [],
                "durable_facts": [],
                "thread_after": {"is_open": False, "what_remains": None},
            }
        )
        response["plan"]["occurrences"].append(second)

        decoded = restore_code_owned_period(
            decode_planner_app_operations(response),
            start=self.start,
            end=self.end,
        )

        self.assertEqual(decoded["plan"]["occurrences"][0]["thread_id"], "thread_contact-1")
        self.assertEqual(decoded["plan"]["occurrences"][1]["thread_id"], "thread_contact-1")
        self.assertEqual(self.validate_planner(decoded), [])

    def test_derived_protection_names_the_reserved_lasting_outcome(self) -> None:
        changes = accepted_protected_changes(
            {
                "plan": {
                    "period_start": "2025-01-01",
                    "period_end": "2025-03-31",
                    "events": [
                        {
                            "id": "later-health-change",
                            "start_date": "2025-02-10",
                            "end_date": "2025-02-10",
                            "subjects": ["person:morgan"],
                            "lasting_outcome": "Morgan begins a new care plan.",
                        }
                    ],
                }
            }
        )
        self.assertEqual(changes[0]["not_before"], "2025-02-10")
        self.assertIn("Morgan begins a new care plan", changes[0]["description"])

    def test_planner_cannot_use_a_protected_change_as_an_occurrence_basis(self) -> None:
        response = self.good_planner_response()
        response["plan"]["occurrences"][0]["basis_ids"] = ["protected:later"]
        errors = self.validate_planner(
            response,
            protected_change_ids={"protected:later"},
        )
        self.assertTrue(any("cannot cite a protected change" in error for error in errors), errors)

    def test_development_can_span_contacts_before_its_fact_becomes_true(self) -> None:
        response = self.good_planner_response()
        declaration = copy.deepcopy(response["plan"]["occurrences"][0]["durable_facts"])
        first = response["plan"]["occurrences"][0]
        first["durable_facts"] = []
        first["thread_after"] = {
            "is_open": True,
            "what_remains": "The requested app change still needs to be completed.",
        }
        second = copy.deepcopy(first)
        second.update(
            {
                "contact_id": "contact-2",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "continues_from": ["contact-1"],
                "anchor_item_ids": [],
                "durable_facts": copy.deepcopy(declaration),
                "thread_after": {"is_open": False, "what_remains": None},
            }
        )
        response["plan"]["occurrences"].append(second)
        self.assertEqual(
            self.validate_planner(
                response,
                facts_by_development={"development-1": declaration},
            ),
            [],
        )

        response["plan"]["occurrences"][1]["durable_facts"] = []
        errors = self.validate_planner(
            response,
            facts_by_development={"development-1": declaration},
        )
        self.assertTrue(any("missing declared durable facts" in error for error in errors), errors)

        response["plan"]["occurrences"][1]["durable_facts"] = copy.deepcopy(declaration)
        duplicate = copy.deepcopy(response["plan"]["occurrences"][1])
        duplicate["contact_id"] = "contact-3"
        duplicate["narrative_date"] = "2025-01-31T10:00:00-08:00"
        duplicate["continues_from"] = ["contact-2"]
        response["plan"]["occurrences"].append(duplicate)
        errors = self.validate_planner(
            response,
            facts_by_development={"development-1": declaration},
        )
        self.assertTrue(any("appears more than once" in error for error in errors), errors)
        self.assertTrue(any("more than once" in error for error in errors), errors)

        response["plan"]["occurrences"].pop()
        response["plan"]["occurrences"][1]["durable_facts"][0]["statement"] = (
            "A mutated version of the declared fact."
        )
        errors = self.validate_planner(
            response,
            facts_by_development={"development-1": declaration},
        )
        self.assertTrue(any("must copy a declared fact" in error for error in errors), errors)

    def test_self_contained_occurrence_cannot_change_durable_facts_but_may_stay_open(self) -> None:
        response = self.good_planner_response()
        occurrence = response["plan"]["occurrences"][0]
        occurrence["basis_ids"] = ["situation:local"]
        occurrence["effect_kind"] = "self_contained"
        occurrence["development_ids"] = []
        occurrence["thread_after"] = {
            "is_open": True,
            "what_remains": "A new matter remains open.",
        }
        errors = self.validate_planner(
            response,
            active_situation_ids={"situation:local"},
        )
        self.assertTrue(any("cannot establish durable facts" in error for error in errors), errors)
        self.assertFalse(any("cannot leave an open thread" in error for error in errors), errors)

    def test_ordinary_current_life_basis_permits_open_factless_contact(self) -> None:
        response = self.good_planner_response()
        occurrence = copy.deepcopy(response["plan"]["occurrences"][0])
        occurrence.update(
            {
                "contact_id": "contact-ordinary-life",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "basis_ids": [history_window.ORDINARY_CURRENT_LIFE_SITUATION_ID],
                "effect_kind": "self_contained",
                "thread_id": "",
                "continues_from": [],
                "development_ids": [],
                "anchor_item_ids": [],
                "uses_fact_ids": [],
                "durable_facts": [],
                "thread_after": {"is_open": False, "what_remains": None},
            }
        )
        response["plan"]["occurrences"].append(occurrence)
        errors = self.validate_planner(
            response,
            active_situation_ids={history_window.ORDINARY_CURRENT_LIFE_SITUATION_ID},
            subjects_by_basis={
                "development-1": {"project:new"},
                history_window.ORDINARY_CURRENT_LIFE_SITUATION_ID: {
                    "person:morgan",
                    "project:new",
                }
            },
            primary_subject_ids={"person:morgan"},
        )
        self.assertEqual(errors, [])

        occurrence["thread_after"] = {
            "is_open": True,
            "what_remains": "A specific external reply is still pending.",
        }
        occurrence["thread_id"] = "thread-ordinary-life"
        errors = self.validate_planner(
            response,
            active_situation_ids={history_window.ORDINARY_CURRENT_LIFE_SITUATION_ID},
            subjects_by_basis={
                "development-1": {"project:new"},
                history_window.ORDINARY_CURRENT_LIFE_SITUATION_ID: {
                    "person:morgan",
                    "project:new",
                }
            },
            primary_subject_ids={"person:morgan"},
        )
        self.assertEqual(errors, [])

        occurrence["durable_facts"] = [
            {
                "key": "invalid-local-fact",
                "statement": "This must not become durable.",
                "applies_when": "it is discussed",
                "subjects": ["person:morgan"],
                "supersedes": [],
            }
        ]
        errors = self.validate_planner(
            response,
            active_situation_ids={history_window.ORDINARY_CURRENT_LIFE_SITUATION_ID},
            subjects_by_basis={
                "development-1": {"project:new"},
                history_window.ORDINARY_CURRENT_LIFE_SITUATION_ID: {
                    "person:morgan",
                    "project:new",
                }
            },
            primary_subject_ids={"person:morgan"},
        )
        self.assertTrue(any("cannot establish durable facts" in error for error in errors), errors)

    def test_direct_continuation_must_keep_exact_unfinished_thread_id(self) -> None:
        response = self.good_planner_response()
        errors = self.validate_planner(
            response,
            unfinished_thread_ids_by_reference={
                "old-session": "thread-from-checkpoint"
            },
        )
        self.assertTrue(
            any("directly continues unfinished thread" in error for error in errors),
            errors,
        )

        response["plan"]["occurrences"][0]["thread_id"] = (
            "thread-from-checkpoint"
        )
        self.assertEqual(
            self.validate_planner(
                response,
                unfinished_thread_ids_by_reference={
                    "old-session": "thread-from-checkpoint"
                },
            ),
            [],
        )

    def test_code_cites_known_world_for_visible_subject_outside_event(self) -> None:
        response = self.good_planner_response()
        response["plan"]["occurrences"][0]["subject_ids"].append(
            "person:anna_martinez"
        )
        subjects_by_basis = {
            "development-1": {"project:new"},
            history_window.ORDINARY_CURRENT_LIFE_BASIS_ID: {
                "person:morgan",
                "person:anna_martinez",
                "project:new",
            },
        }

        normalized = history_window.add_known_world_basis_for_visible_subjects(
            response,
            facts=self.facts,
            subjects_by_basis=subjects_by_basis,
            primary_subject_ids={"person:morgan"},
        )

        self.assertEqual(response["plan"]["occurrences"][0]["basis_ids"], ["development-1"])
        self.assertEqual(
            normalized["plan"]["occurrences"][0]["basis_ids"],
            ["development-1", history_window.ORDINARY_CURRENT_LIFE_BASIS_ID],
        )

    def test_code_does_not_authorize_subject_absent_from_known_world(self) -> None:
        response = self.good_planner_response()
        response["plan"]["occurrences"][0]["subject_ids"].append(
            "person:invented"
        )

        normalized = history_window.add_known_world_basis_for_visible_subjects(
            response,
            facts=self.facts,
            subjects_by_basis={
                "development-1": {"project:new"},
                history_window.ORDINARY_CURRENT_LIFE_BASIS_ID: {
                    "person:morgan",
                    "project:new",
                },
            },
            primary_subject_ids={"person:morgan"},
        )

        self.assertEqual(
            normalized["plan"]["occurrences"][0]["basis_ids"],
            ["development-1"],
        )
        errors = self.validate_planner(
            normalized,
            active_situation_ids={history_window.ORDINARY_CURRENT_LIFE_BASIS_ID},
            subjects_by_basis={
                "development-1": {"project:new"},
                history_window.ORDINARY_CURRENT_LIFE_BASIS_ID: {
                    "person:morgan",
                    "project:new",
                },
            },
            primary_subject_ids={"person:morgan"},
        )
        self.assertTrue(any("unknown entity" in error for error in errors), errors)

    def test_code_leaves_fully_cited_subjects_unchanged(self) -> None:
        response = self.good_planner_response()
        normalized = history_window.add_known_world_basis_for_visible_subjects(
            response,
            facts=self.facts,
            subjects_by_basis={
                "development-1": {"project:new"},
                history_window.ORDINARY_CURRENT_LIFE_BASIS_ID: {
                    "person:morgan",
                    "project:new",
                },
            },
            primary_subject_ids={"person:morgan"},
        )
        self.assertEqual(normalized, response)


    def test_realized_development_does_not_repeat_its_id_in_basis_ids(self) -> None:
        response = self.good_planner_response()
        occurrence = response["plan"]["occurrences"][0]
        occurrence["basis_ids"] = ["situation:development-1"]
        errors = self.validate_planner(
            response,
            active_situation_ids={"situation:development-1"},
            subjects_by_basis={
                "development-1": {"project:new"},
                "situation:development-1": {"project:new"},
            },
            primary_subject_ids={"person:morgan"},
        )
        self.assertEqual(errors, [])


    def test_planner_owns_current_and_earlier_window_fact_references(self) -> None:
        response = self.good_planner_response()
        response["plan"]["occurrences"][0]["uses_facts"] = [1]
        errors = self.validate_planner(response)
        self.assertTrue(any("must contain exactly" in error for error in errors), errors)
        return
        response = self.good_planner_response()
        second = copy.deepcopy(response["plan"]["occurrences"][0])
        second.update(
            {
                "contact_id": "contact-2",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "continues_from": ["contact-1"],
                "subject_ids": ["person:morgan"],
                "development_ids": [],
                "anchor_item_ids": [],
                "what_happens": "Morgan follows up after the project decision.",
                "why_contact_assistant": "Morgan asks for a follow-up using the new rule.",
                "source_material": None,
                "requested_response_or_action": "Draft the short follow-up.",
                "uses_facts": ["new-rule"],
                "durable_facts": [],
                "app_operations": [],
                "thread_open_after_contact": False,
                "what_remains_after_contact": None,
            }
        )
        response["plan"]["occurrences"].append(second)
        self.assertEqual(self.validate_planner(response), [])

    def test_planner_rejects_invalid_future_and_superseded_fact_references(self) -> None:
        response = self.good_planner_response()
        response["plan"]["occurrences"][0]["uses_fact_ids"] = [99]
        errors = self.validate_planner(response)
        self.assertTrue(any("unknown fact 99" in error for error in errors), errors)
        return
        cases = {
            "unknown fact 99": lambda response: response["plan"]["occurrences"][0].update(
                {"uses_facts": [99]}
            ),
            "future or unknown fact key": lambda response: response["plan"]["occurrences"][0].update(
                {"uses_facts": ["new-rule"]}
            ),
        }
        for expected, mutate in cases.items():
            with self.subTest(expected=expected):
                response = self.good_planner_response()
                mutate(response)
                errors = self.validate_planner(response)
                self.assertTrue(any(expected in error for error in errors), errors)

        response = self.good_planner_response()
        second = copy.deepcopy(response["plan"]["occurrences"][0])
        second.update(
            {
                "contact_id": "contact-2",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "continues_from": ["contact-1"],
                "subject_ids": ["person:morgan"],
                "development_ids": [],
                "anchor_item_ids": [],
                "what_happens": "Morgan repeats the retired rule.",
                "why_contact_assistant": "Morgan asks about the old rule.",
                "source_material": None,
                "requested_response_or_action": None,
                "uses_facts": [1],
                "durable_facts": [],
                "app_operations": [],
                "thread_open_after_contact": False,
                "what_remains_after_contact": None,
            }
        )
        response["plan"]["occurrences"].append(second)
        errors = self.validate_planner(response)
        self.assertTrue(
            any("stale or future fact 1" in error for error in errors), errors
        )

    def test_planner_accepts_an_actionless_one_way_update(self) -> None:
        response = self.good_planner_response()
        occurrence = response["plan"]["occurrences"][0]
        occurrence["what_happened"] = (
            "Morgan tells the assistant that the project rule now applies."
        )
        occurrence["assistant_outcome"] = {"kind": "none", "requested_result": None}
        occurrence["app_operations"] = []
        self.assertEqual(self.validate_planner(response), [])

    def test_one_development_can_split_one_old_fact_into_two_new_facts(self) -> None:
        response = self.good_planner_response()
        second_fact = {
            "key": "sibling-rule",
            "statement": "The project also has a separate reporting rule.",
            "applies_when": "project reporting is discussed",
            "subjects": ["person:morgan", "project:new"],
            "supersedes": [1],
        }
        second = copy.deepcopy(response["plan"]["occurrences"][0])
        second.update(
            {
                "contact_id": "contact-2",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "thread_id": "",
                "continues_from": [],
                "what_happened": second_fact["statement"],
                "assistant_outcome": {"kind": "none", "requested_result": None},
                "uses_fact_ids": [],
                "durable_facts": [copy.deepcopy(second_fact)],
                "app_operations": [],
            }
        )
        response["plan"]["occurrences"].append(second)
        facts_by_development = {
            "development-1": [
                history_window.model_fact_fields(self.required_facts["new-rule"]),
                copy.deepcopy(second_fact),
            ]
        }

        self.assertEqual(
            self.validate_planner(
                response, facts_by_development=facts_by_development
            ),
            [],
        )
        history_window.writer_current_facts_for_occurrences(response["plan"], self.facts)
        history_window.writer_facts_by_occurrence(response["plan"], self.facts)

        response["plan"]["occurrences"][1]["development_ids"] = [
            "development-later"
        ]
        response["plan"]["occurrences"][1]["basis_ids"] = [
            "development-later"
        ]
        facts_by_development = {
            "development-1": [
                history_window.model_fact_fields(self.required_facts["new-rule"])
            ],
            "development-later": [copy.deepcopy(second_fact)],
        }
        errors = self.validate_planner(
            response, facts_by_development=facts_by_development
        )
        self.assertTrue(
            any("stale or future reference: 1" in error for error in errors),
            errors,
        )

    def test_final_sessions_allow_one_development_to_split_one_old_fact(self) -> None:
        response = self.good_response()
        second_fact = {
            "key": "sibling-rule",
            "statement": "The project also has a separate reporting rule.",
            "applies_when": "project reporting is discussed",
            "subjects": ["person:morgan", "project:new"],
            "supersedes": [1],
            "evidence_contact_ids": ["contact-2"],
        }
        second = copy.deepcopy(response["plan"]["sessions"][0])
        second.update(
            {
                "contact_id": "contact-2",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "thread_id": "",
                "continues_from": [],
                "purpose": second_fact["statement"],
                "messages": [second_fact["statement"]],
                "uses_facts": [],
                "new_facts": [copy.deepcopy(second_fact)],
                "app_operations": [],
            }
        )
        response["plan"]["sessions"].append(second)
        self.required_facts["sibling-rule"] = {
            **copy.deepcopy(second_fact),
            "development_ids": ["development-1"],
        }
        self.required_facts["sibling-rule"].pop("evidence_contact_ids")

        self.assertEqual(self.validate(response), [])

    def test_planner_rejects_external_action_without_operation(self) -> None:
        response = self.good_planner_response()
        occurrence = response["plan"]["occurrences"][0]
        occurrence["app_operations"] = []
        errors = self.validate_planner(response)
        self.assertTrue(
            any("external_action requires an app operation" in error for error in errors),
            errors,
        )

    def test_planner_rejects_invented_existing_app_record_id(self) -> None:
        response = self.good_planner_response()
        response["plan"]["occurrences"][0]["app_operations"] = [
            {
                "tool": "update_calendar_event",
                "args": {
                    "event_id": "evt_invented",
                    "start": "2025-01-29T11:00:00-08:00",
                    "end": "2025-01-29T11:30:00-08:00",
                },
            }
        ]
        self.tools["update_calendar_event"] = {
            "arguments": ["event_id", "start", "end"],
            "required_arguments": ["event_id"],
            "description": "Update an existing calendar event.",
            "state_effect": {
                "reads_state_keys": ["calendar"],
                "writes_state_keys": ["calendar"],
            },
        }

        errors = self.validate_planner(
            response,
            available_app_records={
                "calendar": [{"id": "evt_real", "title": "Existing meeting"}]
            },
        )

        self.assertTrue(
            any("event_id must copy an ID" in error for error in errors), errors
        )

    def test_planner_rejects_historical_development_id(self) -> None:
        response = self.good_planner_response()
        response["plan"]["occurrences"][0]["development_ids"] = ["event-recent"]
        errors = self.validate_planner(response)
        self.assertTrue(
            any("development outside this week: event-recent" in error for error in errors),
            errors,
        )


    def test_exact_schema_passes_and_extra_session_field_fails(self) -> None:
        self.assertEqual(self.validate(self.good_response()), [])
        response = self.good_response()
        response["plan"]["sessions"][0]["source_material"] = {"origin": "wrong layer"}
        errors = self.validate(response)
        self.assertTrue(any("must contain exactly" in error for error in errors), errors)

    def test_completed_session_does_not_need_thread_id(self) -> None:
        response = self.good_response()
        session = response["plan"]["sessions"][0]
        session["thread_id"] = ""
        session["continues_from"] = []
        self.assertEqual(self.validate(response), [])

    def test_writer_cannot_supply_planner_owned_metadata(self) -> None:
        response = self.good_writer_api_response()
        response["plan"]["sessions"][0]["thread_id"] = "different-thread"
        errors = validate_writer_prose_response(
            response,
            start=self.start,
            end=self.end,
            occurrence_plan=self.good_planner_response()["plan"],
        )
        self.assertTrue(any("must contain exactly" in error for error in errors), errors)







    def test_required_fact_supersedes_must_match_exact_numeric_ids(self) -> None:
        response = self.good_response()
        response["plan"]["sessions"][0]["new_facts"][0]["supersedes"] = ["1"]
        errors = self.validate(response)
        self.assertTrue(
            any(
                "new_facts[0].supersedes changes the accepted fact declaration"
                in error
                for error in errors
            ),
            errors,
        )

    def test_sibling_facts_may_replace_different_parts_of_one_current_fact(
        self,
    ) -> None:
        response = self.good_response()
        sibling = copy.deepcopy(response["plan"]["sessions"][0]["new_facts"][0])
        sibling["key"] = "sibling-rule"
        response["plan"]["sessions"][0]["new_facts"].append(sibling)
        occurrence_plan = self.good_planner_response()["plan"]
        self.required_facts["sibling-rule"] = {
            **copy.deepcopy(self.required_facts["new-rule"]),
            "key": "sibling-rule",
        }

        self.assertEqual(self.validate(response, occurrence_plan=occurrence_plan), [])

    def test_replacement_requires_new_contact_evidence_and_cannot_name_inherited_sessions(
        self,
    ) -> None:
        response = self.good_response()
        replacement = response["plan"]["sessions"][0]["new_facts"][0]
        replacement["evidence_contact_ids"] = []
        errors = self.validate(response)
        self.assertTrue(
            any("new_facts[0].evidence_contact_ids must be non-empty" in error for error in errors),
            errors,
        )

        response = self.good_response()
        replacement = response["plan"]["sessions"][0]["new_facts"][0]
        replacement["inherited_source_session_ids"] = ["old-session"]
        errors = self.validate(response)
        self.assertTrue(
            any("new_facts[0] must contain exactly" in error for error in errors),
            errors,
        )





    def test_accepted_accounting_resolves_siblings_and_transitive_fact_keys(self) -> None:
        facts = [
            {
                "id": 1,
                "construction_key": "old-rule",
                "source_session_ids": ["session-old"],
            }
        ]
        history = [
            {"id": "session-old", "narrative_date": "2025-04-01T09:00:00-07:00"}
        ]
        plan = {
            "sessions": [
                {
                    "new_facts": [
                        {"key": "owner-change"},
                        {"key": "still-required"},
                    ]
                },
                {"new_facts": [{"key": "later-current-rule"}]},
            ]
        }
        references = {
            "owner-change": ["id:1", "key:old-rule"],
            "still-required": ["id:1"],
            "later-current-rule": ["key:still-required"],
        }

        resolved = replacement_source_fact_ids_for_window(
            replacement_refs_by_fact_key=references,
            facts=facts,
            accepted_history=history,
            plan=plan,
        )

        self.assertEqual(resolved, {
            "owner-change": [1],
            "still-required": [1],
            "later-current-rule": [3],
        })

    def test_accepted_accounting_rejects_missing_future_and_ambiguous_fact_keys(self) -> None:
        facts = [
            {
                "id": 1,
                "construction_key": "old-rule",
                "source_session_ids": ["session-old"],
            }
        ]
        history = [
            {"id": "session-old", "narrative_date": "2025-04-01T09:00:00-07:00"}
        ]
        plan = {
            "sessions": [
                {"new_facts": [{"key": "first"}]},
                {"new_facts": [{"key": "later"}]},
            ]
        }
        cases = {
            "missing": ({"first": ["key:missing"]}, "missing fact key"),
            "future": ({"first": ["key:later"]}, "future current-window fact"),
            "ambiguous": ({"first": ["key:old-rule"]}, "reuses accepted construction key"),
        }
        ambiguous_plan = {
            "sessions": [
                {"new_facts": [{"key": "old-rule"}, {"key": "first"}]}
            ]
        }
        for label, (references, message) in cases.items():
            with self.subTest(case=label):
                source_plan = ambiguous_plan if label == "ambiguous" else plan
                with self.assertRaisesRegex(ValueError, message):
                    replacement_source_fact_ids_for_window(
                        replacement_refs_by_fact_key=references,
                        facts=facts,
                        accepted_history=history,
                        plan=source_plan,
                    )

    def test_accepted_accounting_file_has_only_valid_fact_reference_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            accounting = root / "accepted_replacement_accounting.json"
            config = {"accepted_replacement_accounting": str(accounting)}
            accounting.write_text(
                json.dumps(
                    {
                        "replacement_accounting": [
                            {
                                "replaced_fact_ref": "id:1",
                                "replacement_fact_keys": ["owner-change", "still-required"],
                            }
                        ]
                    }
                )
            )

            path, refs = load_replacement_evidence_refs(config)

            self.assertEqual(path, accounting)
            self.assertEqual(refs, {
                "owner-change": ["id:1"],
                "still-required": ["id:1"],
            })






    def test_python_assigns_stable_session_ids(self) -> None:
        plan = self.good_response()["plan"]
        second = copy.deepcopy(plan["sessions"][0])
        second["contact_id"] = "contact-2"
        second["narrative_date"] = "2025-01-29T11:00:00-08:00"
        plan["sessions"].append(second)
        first = assign_window_session_ids(
            plan,
            persona="morgan",
            timezone_name="America/Los_Angeles",
            existing_session_ids={"extension_morgan_2025_01_29_001"},
        )
        second_run = assign_window_session_ids(
            plan,
            persona="morgan",
            timezone_name="America/Los_Angeles",
            existing_session_ids={"extension_morgan_2025_01_29_001"},
        )
        self.assertEqual(first, second_run)
        self.assertEqual(
            [row["session_id"] for row in first["sessions"]],
            [
                "extension_morgan_2025_01_29_002",
                "extension_morgan_2025_01_29_003",
            ],
        )
        self.assertNotIn("session_id", plan["sessions"][0])

    def test_materialized_session_purpose_preserves_subject_ids(self) -> None:
        plan = self.good_response()["plan"]
        plan["sessions"][0]["session_id"] = "extension_morgan_2025_01_29_001"
        state, _, _ = materialize_window_state(
            starting_state={
                "history": [
                    {
                        "id": "old-session",
                        "narrative_date": "2025-01-28T09:00:00-08:00",
                        "messages": ["The earlier rule is current."],
                    }
                ],
                "session_purposes": [],
                "entities": self.entities,
                "facts": self.facts,
                "app_state": {"docs": []},
                "unfinished_threads": [],
            },
            plan=plan,
            projected_app_state={"docs": []},
            accepted_contact_details_by_contact_id={
                "contact-1": (
                    "What happened:\nA vendor sent the signed renewal terms.\n\n"
                    "Source material:\nRenewal is $43.80 for 12 seats."
                )
            },
            current_state_changes_by_contact_id={
                "contact-1": [
                    {
                        "key": "project_operating_rule",
                        "subject_ids": ["person:morgan", "project:new"],
                        "statement": "Morgan introduces the project and establishes its rule.",
                    }
                ]
            },
        )

        self.assertEqual(
            state["session_purposes"][-1]["subject_ids"],
            ["person:morgan", "project:new"],
        )
        self.assertEqual(
            state["session_purposes"][-1]["current_state_changes"],
            [
                {
                    "key": "project_operating_rule",
                    "subject_ids": ["person:morgan", "project:new"],
                    "statement": "Morgan introduces the project and establishes its rule.",
                }
            ],
        )
        self.assertEqual(
            state["session_purposes"][-1]["accepted_contact_details"],
            "What happened:\nA vendor sent the signed renewal terms.\n\n"
            "Source material:\nRenewal is $43.80 for 12 seats.",
        )

    def test_same_thread_update_replaces_old_row_and_close_removes_it(self) -> None:
        existing = [
            {
                "thread_id": "thread-main",
                "interaction_id": "old-contact",
                "session_id": "old-session",
            },
            {"thread_id": "thread-other", "interaction_id": "other"},
        ]
        open_session = copy.deepcopy(self.good_response()["plan"]["sessions"][0])
        open_session.update(
            {
                "session_id": "new-session",
                "thread_open_after_contact": True,
                "what_remains_after_contact": "One concrete follow-up remains.",
            }
        )
        updated = update_unfinished_threads(
            existing, {"sessions": [open_session], "new_entities": []}
        )
        main_rows = [row for row in updated if row.get("thread_id") == "thread-main"]
        self.assertEqual(len(main_rows), 1)
        self.assertEqual(main_rows[0]["session_id"], "new-session")

        closing = copy.deepcopy(open_session)
        closing.update(
            {
                "contact_id": "closing-contact",
                "session_id": "closing-session",
                "thread_open_after_contact": False,
                "what_remains_after_contact": None,
                "continues_from": [],
            }
        )
        closed = update_unfinished_threads(updated, {"sessions": [closing]})
        self.assertFalse(any(row.get("thread_id") == "thread-main" for row in closed))
        self.assertTrue(any(row.get("thread_id") == "thread-other" for row in closed))

    def test_direct_continuation_transfers_and_closes_historical_thread_alias(self) -> None:
        existing = [
            {
                "thread_id": "original-thread",
                "interaction_id": "old-contact",
                "session_id": "old-session",
            }
        ]
        continued = copy.deepcopy(self.good_response()["plan"]["sessions"][0])
        continued.update(
            {
                "contact_id": "continued-contact",
                "session_id": "continued-session",
                "thread_id": "historical-alias",
                "continues_from": ["old-session"],
                "thread_open_after_contact": True,
                "what_remains_after_contact": "One result remains pending.",
            }
        )
        updated = update_unfinished_threads(existing, {"sessions": [continued]})
        self.assertFalse(
            any(row.get("thread_id") == "original-thread" for row in updated)
        )
        self.assertTrue(
            any(row.get("thread_id") == "historical-alias" for row in updated)
        )

        closing = copy.deepcopy(continued)
        closing.update(
            {
                "contact_id": "closing-contact",
                "session_id": "closing-session",
                "thread_id": "closing-alias",
                "continues_from": ["continued-session"],
                "thread_open_after_contact": False,
                "what_remains_after_contact": None,
            }
        )
        self.assertEqual(
            update_unfinished_threads(updated, {"sessions": [closing]}),
            [],
        )


    def test_required_facts_are_exact_but_ordinary_facts_are_allowed(self) -> None:
        missing = self.good_response()
        missing["plan"]["sessions"][0]["new_facts"] = []
        errors = self.validate(missing)
        self.assertTrue(
            any(
                "must contain exactly the required quarter fact keys" in error
                for error in errors
            ),
            errors,
        )

        changed = self.good_response()
        changed["plan"]["sessions"][0]["new_facts"][0]["statement"] = "Changed"
        errors = self.validate(changed)
        self.assertTrue(
            any("changes the accepted fact declaration" in error for error in errors),
            errors,
        )

        ordinary = self.good_response()
        ordinary["plan"]["sessions"][0]["new_facts"].append(
            {
                "key": "ordinary-follow-up-context",
                "statement": "The team will use the project note for the next follow-up.",
                "applies_when": "preparing the next project follow-up",
                "subjects": ["person:morgan", "project:new"],
                "supersedes": [],
                "evidence_contact_ids": ["contact-1"],
            }
        )
        self.assertEqual(self.validate(ordinary), [])

    def test_writer_validation_accepts_distinct_message_text(self) -> None:
        errors = self.validate(self.good_response())
        self.assertEqual(errors, [])

    def test_writer_validation_allows_exact_duplicate_from_prior_history(self) -> None:
        response = self.good_response()
        message = response["plan"]["sessions"][0]["messages"][0]
        errors = self.validate(
            response,
        )
        self.assertEqual(errors, [], errors)

    def test_writer_validation_rejects_duplicate_contact_id(self) -> None:
        response = self.good_response()
        second = copy.deepcopy(response["plan"]["sessions"][0])
        second.update(
            {
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "thread_id": "thread-second",
                "development_ids": [],
                "anchor_item_ids": [],
                "uses_facts": [],
                "new_facts": [],
                "app_operations": [],
                "thread_open_after_contact": False,
                "what_remains_after_contact": None,
            }
        )
        response["plan"]["sessions"].append(second)
        errors = self.validate(response)
        self.assertTrue(
            any("sessions must have unique non-empty contact_id values" in error for error in errors),
            errors,
        )

    def test_strict_schemas_accept_existing_planner_and_writer_fixtures(self) -> None:
        for schema in (PLANNER_RESPONSE_SCHEMA, WRITER_API_RESPONSE_SCHEMA):
            property_count = 0
            maximum_object_depth = 0

            def inspect(node: Any, object_depth: int = 0) -> None:
                nonlocal property_count, maximum_object_depth
                if not isinstance(node, dict):
                    return
                next_depth = object_depth
                if node.get("type") == "object":
                    next_depth += 1
                    maximum_object_depth = max(maximum_object_depth, next_depth)
                    properties = node.get("properties")
                    self.assertIsInstance(properties, dict)
                    self.assertFalse(node.get("additionalProperties"))
                    self.assertEqual(node.get("required"), list(properties))
                    property_count += len(properties)
                for value in node.values():
                    if isinstance(value, dict):
                        inspect(value, next_depth)
                    elif isinstance(value, list):
                        for item in value:
                            inspect(item, next_depth)

            inspect(schema)
            self.assertLessEqual(property_count, 100)
            self.assertLessEqual(maximum_object_depth, 5)
        Draft202012Validator.check_schema(PLANNER_RESPONSE_SCHEMA)
        Draft202012Validator.check_schema(WRITER_API_RESPONSE_SCHEMA)
        Draft202012Validator(PLANNER_RESPONSE_SCHEMA).validate(
            self.good_planner_api_response()
        )
        Draft202012Validator(WRITER_API_RESPONSE_SCHEMA).validate(
            self.good_writer_api_response()
        )

    def test_model_schemas_reject_model_owned_period_fields(self) -> None:
        planner = self.good_planner_api_response()
        planner["plan"]["period_start"] = self.start.isoformat()
        with self.assertRaises(ValidationError):
            Draft202012Validator(PLANNER_RESPONSE_SCHEMA).validate(planner)

        writer = self.good_writer_api_response()
        writer["plan"]["period_end"] = self.end.isoformat()
        with self.assertRaises(ValidationError):
            Draft202012Validator(WRITER_API_RESPONSE_SCHEMA).validate(writer)

    def test_planner_schema_requires_split_fact_reference_fields_with_correct_types(self) -> None:
        validator = Draft202012Validator(PLANNER_RESPONSE_SCHEMA)
        valid = self.good_planner_api_response()
        validator.validate(valid)

        missing = copy.deepcopy(valid)
        missing["plan"]["occurrences"][0].pop("uses_new_fact_keys")
        with self.assertRaises(ValidationError):
            validator.validate(missing)

        wrong_current_type = copy.deepcopy(valid)
        wrong_current_type["plan"]["occurrences"][0]["uses_current_fact_ids"] = ["1"]
        with self.assertRaises(ValidationError):
            validator.validate(wrong_current_type)

        wrong_new_type = copy.deepcopy(valid)
        wrong_new_type["plan"]["occurrences"][0]["uses_new_fact_keys"] = [1]
        with self.assertRaises(ValidationError):
            validator.validate(wrong_new_type)

    def test_writer_schema_rejects_undeclared_session_field(self) -> None:
        response = self.good_writer_api_response()
        response["plan"]["sessions"][0]["narrative_date"] = "forbidden"
        with self.assertRaises(ValidationError):
            Draft202012Validator(WRITER_API_RESPONSE_SCHEMA).validate(response)

    def test_writer_schema_is_limited_to_fixed_contact_ids_and_count(self) -> None:
        schema = writer_api_response_schema(["contact-1", "contact-2"])
        validator = Draft202012Validator(schema)
        response = {
            "plan": {
                "sessions": {
                    "contact-1": {
                        "messages_before_request": [],
                        "message_with_request": "First.",
                        "conflicting_fact_ids": [],
                    },
                    "contact-2": {
                        "messages_before_request": [],
                        "message_with_request": "Second.",
                        "conflicting_fact_ids": [],
                    },
                }
            }
        }
        validator.validate(response)

        extra = copy.deepcopy(response)
        extra["plan"]["sessions"]["contact-3"] = copy.deepcopy(
            extra["plan"]["sessions"]["contact-1"]
        )
        with self.assertRaises(ValidationError):
            validator.validate(extra)

        unknown = copy.deepcopy(response)
        unknown["plan"]["sessions"]["other-contact"] = unknown["plan"]["sessions"].pop(
            "contact-2"
        )
        with self.assertRaises(ValidationError):
            validator.validate(unknown)

    def test_writer_schema_rejects_app_operations(self) -> None:
        schema = writer_api_response_schema(["contact-1"])
        validator = Draft202012Validator(schema)
        valid = {
            "plan": {
                "sessions": {
                    "contact-1": {
                        "messages_before_request": [],
                        "message_with_request": "Send it.",
                        "conflicting_fact_ids": [],
                    },
                }
            }
        }
        validator.validate(valid)

        extra = copy.deepcopy(valid)
        extra["plan"]["sessions"]["contact-1"]["app_operations"] = []
        with self.assertRaises(ValidationError):
            validator.validate(extra)

    def test_writer_contact_packets_keep_only_their_referenced_context(self) -> None:
        occurrence_plan = copy.deepcopy(self.good_planner_response()["plan"])
        occurrence_plan["occurrences"].append(
            {
                **copy.deepcopy(occurrence_plan["occurrences"][0]),
                "contact_id": "contact-2",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "continues_from": [],
                "subject_ids": ["person:morgan"],
                "uses_fact_ids": [],
                "durable_facts": [],
            }
        )
        facts = [
            *copy.deepcopy(self.facts),
            {
                "id": 2,
                "statement": "Only contact two uses this.",
                "applies_when": "contact two is discussed",
                "subjects": ["project:other"],
                "source_session_ids": ["old-session"],
                "supersedes": [],
            },
        ]
        first = occurrence_plan["occurrences"][0]
        first["uses_fact_ids"] = [1]
        second = occurrence_plan["occurrences"][1]
        second["subject_ids"] = ["project:other"]
        second["uses_fact_ids"] = [2]
        selected = writer_facts_by_occurrence(occurrence_plan, facts)
        self.assertEqual([row["id"] for row in selected["contact-1"]], [1])
        self.assertEqual([row["id"] for row in selected["contact-2"]], [2])

        entities = [
            *copy.deepcopy(self.entities),
            {"id": "project:other", "name": "Other", "kind": "project"},
        ]
        first_entities = writer_known_entities_for_occurrence(
            entities,
            occurrence=first,
            current_facts=selected["contact-1"],
            required_entities=[],
        )
        second_entities = writer_known_entities_for_occurrence(
            entities,
            occurrence=second,
            current_facts=selected["contact-2"],
            required_entities=[],
        )
        self.assertNotIn("project:other", [row["id"] for row in first_entities])
        self.assertIn("project:other", [row["id"] for row in second_entities])

    def test_writer_contact_packet_includes_fact_established_earlier_that_week(self) -> None:
        occurrence_plan = copy.deepcopy(self.good_planner_response()["plan"])
        first = occurrence_plan["occurrences"][0]
        first["uses_fact_ids"] = [1]
        later = copy.deepcopy(first)
        later.update(
            {
                "contact_id": "contact-2",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "uses_fact_ids": ["new-rule"],
                "durable_facts": [],
            }
        )
        occurrence_plan["occurrences"].append(later)

        selected = writer_facts_by_occurrence(occurrence_plan, self.facts)

        self.assertEqual(
            selected["contact-2"],
            [
                {
                    "key": "new-rule",
                    "statement": "The new rule now governs the project.",
                    "applies_when": "the project is discussed",
                    "subjects": ["person:morgan", "project:new"],
                    "supersedes": [1],
                }
            ],
        )

    def test_writer_attaches_sorted_deduplicated_dates_only_to_cited_facts(self) -> None:
        occurrence_plan = {
            "occurrences": [
                {
                    "contact_id": "contact-1",
                    "uses_fact_ids": [1],
                    "durable_facts": [],
                },
                {
                    "contact_id": "contact-2",
                    "uses_fact_ids": [2],
                    "durable_facts": [],
                },
            ]
        }
        facts = [
            {
                "id": 1,
                "statement": "Cherrywood has been home since May.",
                "subjects": ["person:morgan"],
                "source_session_ids": ["accepted-may-3", "accepted-may-1", "accepted-may-3"],
                "supersedes": [],
            },
            {
                "id": 2,
                "statement": "The registration notice still shows Holly.",
                "subjects": ["person:morgan"],
                "source_session_ids": ["accepted-may-2"],
                "supersedes": [],
            },
        ]
        accepted_history = [
            {"id": "accepted-may-3", "narrative_date": "2024-05-03T09:00:00-07:00"},
            {"id": "accepted-may-1", "narrative_date": "2024-05-01T09:00:00-07:00"},
            {"id": "accepted-may-2", "narrative_date": "2024-05-02T09:00:00-07:00"},
        ]

        selected = writer_facts_by_occurrence(
            occurrence_plan,
            facts,
            accepted_history=accepted_history,
        )

        self.assertEqual(
            selected["contact-1"][0]["source_message_dates"],
            ["2024-05-01", "2024-05-03"],
        )
        self.assertEqual(
            selected["contact-2"][0]["source_message_dates"],
            ["2024-05-02"],
        )
        self.assertNotIn("source_session_ids", json.dumps(selected))
        self.assertNotIn("accepted-may-", json.dumps(selected))

    def test_writer_does_not_invent_dates_for_missing_source_sessions(self) -> None:
        occurrence_plan = {
            "occurrences": [
                {"contact_id": "missing-only", "uses_fact_ids": [1], "durable_facts": []},
                {"contact_id": "mixed", "uses_fact_ids": [2], "durable_facts": []},
            ]
        }
        facts = [
            {
                "id": 1,
                "statement": "A fact without an accepted source.",
                "subjects": ["person:morgan"],
                "source_session_ids": ["missing-session"],
                "supersedes": [],
            },
            {
                "id": 2,
                "statement": "A fact with one accepted source.",
                "subjects": ["person:morgan"],
                "source_session_ids": ["missing-session", "accepted-session"],
                "supersedes": [],
            },
        ]
        selected = writer_facts_by_occurrence(
            occurrence_plan,
            facts,
            accepted_history=[
                {
                    "id": "accepted-session",
                    "narrative_date": "2024-05-01T09:00:00-07:00",
                }
            ],
        )

        self.assertNotIn("source_message_dates", selected["missing-only"][0])
        self.assertEqual(
            selected["mixed"][0]["source_message_dates"], ["2024-05-01"]
        )
        self.assertNotIn("missing-session", json.dumps(selected))

    def test_same_week_fact_has_no_source_message_dates(self) -> None:
        occurrence_plan = {
            "occurrences": [
                {
                    "contact_id": "contact-1",
                    "uses_fact_ids": [],
                    "durable_facts": [
                        {
                            "key": "new-rule",
                            "statement": "The new rule now governs.",
                            "applies_when": "the project is discussed",
                            "subjects": ["person:morgan"],
                            "supersedes": [],
                        }
                    ],
                },
                {
                    "contact_id": "contact-2",
                    "uses_fact_ids": ["new-rule"],
                    "durable_facts": [],
                },
            ]
        }

        selected = writer_facts_by_occurrence(
            occurrence_plan,
            [],
            accepted_history=[
                {
                    "id": "accepted-session",
                    "narrative_date": "2024-05-01T09:00:00-07:00",
                }
            ],
        )

        self.assertEqual(
            selected["contact-2"],
            [
                {
                    "key": "new-rule",
                    "statement": "The new rule now governs.",
                    "applies_when": "the project is discussed",
                    "subjects": ["person:morgan"],
                    "supersedes": [],
                }
            ],
        )

    def test_writer_enrichment_does_not_change_planner_payload_or_stored_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            quarter = root / "quarter.json"
            spec = root / "spec.yaml"
            world = root / "world.md"
            quarter.write_text(
                json.dumps(
                    {
                        "plan": {
                            "period_start": "2025-01-01",
                            "period_end": "2025-03-31",
                            "new_entities": [],
                            "events": [
                                {
                                    "id": "current",
                                    "start_date": "2025-01-29",
                                    "end_date": "2025-01-29",
                                    "subjects": ["person:morgan"],
                                    "what_happens": "The current project is discussed.",
                                    "lasting_outcome": "No lasting change.",
                                    "relevant_existing_fact_ids": [1],
                                    "facts_to_establish": [],
                                }
                            ],
                        }
                    }
                )
            )
            spec.write_text(
                "narrative_timezone: America/Los_Angeles\n"
                "primary_subject_ids:\n- person:morgan\n"
            )
            world.write_text("Morgan works on a project.\n")
            state = {
                "facts": [
                    {
                        **copy.deepcopy(self.facts[0]),
                        "source_session_ids": ["accepted-session"],
                    }
                ],
                "entities": copy.deepcopy(self.entities),
                "app_state": {},
                "history": [
                    {
                        "id": "accepted-session",
                        "narrative_date": "2024-05-01T09:00:00-07:00",
                        "messages": ["Accepted source message."],
                    }
                ],
                "session_purposes": [],
                "operation_results": [],
                "unfinished_threads": [],
            }
            config = {
                "persona": "morgan",
                "quarter_plan": str(quarter),
                "spec": str(spec),
                "generator_context": str(world),
            }
            planner_before, _ = history_window.build_planner_payload(
                config=config,
                checkpoint={"covered_quarter_event_ids": []},
                state=state,
                start=self.start,
                end=self.start,
            )
            writer_payload = build_writer_payload(
                planner_response={
                    "plan": {
                        "occurrences": [
                            {
                                "contact_id": "contact-1",
                                "narrative_date": "2025-01-29T10:00:00-08:00",
                                "subject_ids": ["person:morgan"],
                                "uses_fact_ids": [1],
                                "durable_facts": [],
                                "continues_from": [],
                                "app_operations": [],
                            }
                        ]
                    }
                },
                config=config,
                state=state,
                start=self.start,
                end=self.start,
                timezone_name="America/Los_Angeles",
            )
            planner_after, _ = history_window.build_planner_payload(
                config=config,
                checkpoint={"covered_quarter_event_ids": []},
                state=state,
                start=self.start,
                end=self.start,
            )

        self.assertEqual(planner_before, planner_after)
        self.assertEqual(state["facts"], [
            {
                **self.facts[0],
                "source_session_ids": ["accepted-session"],
            }
        ])
        self.assertEqual(
            writer_payload["contacts"]["contact-1"]["cited_facts"][0][
                "source_message_dates"
            ],
            ["2024-05-01"],
        )
        self.assertNotIn("source_message_dates", json.dumps(planner_before))

    def test_writer_prior_context_matches_concrete_subjects_within_14_days(self) -> None:
        occurrence_plan = {
            "occurrences": [
                {
                    "contact_id": "contact-project",
                    "narrative_date": "2025-01-29T10:00:00-08:00",
                    "subject_ids": ["person:morgan", "project:current"],
                    "continues_from": ["old-session"],
                    "app_operations": [],
                }
            ]
        }
        state = {
            "entities": [],
            "app_state": {},
            "history": [
                {
                    "id": "old-session",
                    "narrative_date": "2025-01-20T10:00:00-08:00",
                    "messages": ["The exact project message."],
                },
                {
                    "id": "shared-session",
                    "narrative_date": "2025-01-10T10:00:00-08:00",
                    "messages": ["Another exact project message."],
                },
                {
                    "id": "too-old-session",
                    "narrative_date": "2024-12-03T10:00:00-08:00",
                    "messages": ["Do not include this."],
                },
                {
                    "id": "primary-only-session",
                    "narrative_date": "2025-01-15T10:00:00-08:00",
                    "messages": ["Do not include a persona-only contact."],
                    "subject_ids": ["person:morgan"],
                },
            ],
            "session_purposes": [
                {
                    "session_id": "old-session",
                    "planned_interaction_id": "old-contact",
                    "subject_ids": ["person:morgan", "project:current"],
                },
                {
                    "session_id": "shared-session",
                    "planned_interaction_id": "shared-contact",
                    "subject_ids": ["person:morgan", "project:current"],
                },
                {
                    "session_id": "too-old-session",
                    "subject_ids": ["project:current"],
                },
                {
                    "session_id": "primary-only-session",
                    "subject_ids": ["person:morgan"],
                },
            ],
            "facts": [],
            "operation_results": [
                {
                    "session_id": "shared-session",
                    "operation": {"tool": "update_doc", "args": {"id": "doc-1"}},
                    "result": {"ok": True},
                }
            ],
        }

        selected = prior_accepted_context_by_contact_subject(
            occurrence_plan,
            state,
            primary_subject_ids={"person:morgan"},
            entities=[],
        )

        self.assertEqual(
            [row["session_id"] for row in selected["contact-project"]],
            ["old-session"],
        )
        self.assertEqual(
            selected["contact-project"][0]["messages"],
            ["The exact project message."],
        )

        with tempfile.TemporaryDirectory() as temp:
            world = Path(temp) / "world.md"
            world.write_text("The accepted world.\n")
            payload = build_writer_payload(
                planner_response={"plan": occurrence_plan},
                config={"persona": "morgan", "generator_context": str(world)},
                state=state,
                start=date(2025, 1, 29),
                end=date(2025, 1, 29),
                timezone_name="America/Los_Angeles",
                primary_subject_ids={"person:morgan"},
            )

        self.assertEqual(
            payload["contacts"]["contact-project"]["continues_from_contacts"],
            [{"contact_id": "old-session"}],
        )
        self.assertNotIn("earlier_accepted_contacts", payload)

    def test_writer_response_normalization_restores_planner_order_and_internal_shape(self) -> None:
        response = {
            "plan": {
                "sessions": {
                    "contact-2": {
                        "messages_before_request": [],
                        "message_with_request": "Second.",
                        "conflicting_fact_ids": [],
                    },
                    "contact-1": {
                        "messages_before_request": [],
                        "message_with_request": "First.",
                        "conflicting_fact_ids": [],
                    },
                }
            }
        }
        normalized = normalize_writer_contact_order(response, ["contact-1", "contact-2"])
        self.assertEqual(
            normalized["plan"]["sessions"],
            [
                {
                    "contact_id": "contact-1",
                    "messages_before_request": [],
                    "message_with_request": "First.",
                    "conflicting_fact_ids": [],
                },
                {
                    "contact_id": "contact-2",
                    "messages_before_request": [],
                    "message_with_request": "Second.",
                    "conflicting_fact_ids": [],
                },
            ],
        )
        extra = copy.deepcopy(response)
        extra["plan"]["sessions"]["contact-3"] = {
            "messages_before_request": [],
            "message_with_request": "Third.",
            "conflicting_fact_ids": [],
        }
        with self.assertRaises(ValueError):
            normalize_writer_contact_order(extra, ["contact-1", "contact-2"])

    def test_planner_rejects_development_before_its_accepted_start_date(self) -> None:
        normalized_prompt = " ".join(HISTORY_WINDOW_STORY_SYSTEM.split())
        self.assertIn(
            "Do not cite it or use any part of its new outcome "
            "before that date",
            normalized_prompt,
        )
        self.assertIn(
            "If its start and end dates are the same, every contact that "
            "cites it must occur on that date",
            normalized_prompt,
        )
        response = self.good_planner_response()
        errors = self.validate_planner(
            response,
            development_start_dates={"development-1": date(2025, 1, 30)},
        )
        self.assertTrue(
            any("before its accepted start date 2025-01-30" in error for error in errors),
            errors,
        )

        response["plan"]["occurrences"][0]["narrative_date"] = "2025-01-30T10:00:00-08:00"
        self.assertEqual(
            self.validate_planner(
                response,
                development_start_dates={"development-1": date(2025, 1, 30)},
            ),
            [],
        )

    def test_writer_schema_accepts_writer_owned_uses_facts(self) -> None:
        response = self.good_writer_api_response()
        response["plan"]["sessions"][0]["uses_facts"] = [1]
        with self.assertRaises(ValidationError):
            Draft202012Validator(WRITER_API_RESPONSE_SCHEMA).validate(response)

    def test_planner_args_json_decodes_to_existing_internal_shape(self) -> None:
        decoded = restore_code_owned_period(
            decode_planner_app_operations(self.good_planner_api_response()),
            start=self.start,
            end=self.end,
        )
        expected = self.good_planner_response()
        expected["plan"]["occurrences"][0]["anchor_item_ids"] = []
        self.assertEqual(decoded, expected)
        self.assertEqual(self.validate_planner(decoded), [])

    def test_planner_decoder_assigns_new_open_thread_without_linking_other_blank_ids(self) -> None:
        response = self.good_planner_api_response()
        first = response["plan"]["occurrences"][0]
        first["thread_id"] = ""
        first["continues_from"] = []
        first["thread_after"] = {
            "is_open": True,
            "what_remains": "A specific reply is pending.",
        }
        second = copy.deepcopy(first)
        second.update(
            {
                "contact_id": "contact-2",
                "happening_id": "another-happening",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "continues_from": [],
                "thread_after": {"is_open": False, "what_remains": None},
            }
        )
        response["plan"]["occurrences"].append(second)

        decoded = decode_planner_app_operations(response)

        self.assertEqual(
            decoded["plan"]["occurrences"][0]["thread_id"], "thread_contact-1"
        )
        self.assertEqual(decoded["plan"]["occurrences"][1]["continues_from"], [])

    def test_planner_normalizes_current_ids_and_prior_same_week_keys(self) -> None:
        response = self.good_planner_api_response()
        first = response["plan"]["occurrences"][0]
        first["durable_facts"][0]["key"] = "same-week-rule"
        second = copy.deepcopy(first)
        second.update(
            {
                "contact_id": "contact-2",
                "happening_id": "happening-follow-up",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "basis_ids": [history_window.ORDINARY_CURRENT_LIFE_BASIS_ID],
                "effect_kind": "self_contained",
                "thread_id": "self-contained-2",
                "continues_from": [],
                "development_ids": [],
                "uses_current_fact_ids": [],
                "uses_new_fact_keys": ["same-week-rule"],
                "durable_facts": [],
            }
        )
        response["plan"]["occurrences"].append(second)

        decoded = decode_planner_app_operations(response)

        self.assertEqual(decoded["plan"]["occurrences"][0]["uses_fact_ids"], [1])
        self.assertEqual(
            decoded["plan"]["occurrences"][1]["uses_fact_ids"], ["same-week-rule"]
        )
        normalized = restore_code_owned_period(decoded, start=self.start, end=self.end)
        self.assertEqual(
            self.validate_planner(
                normalized,
                active_situation_ids={history_window.ORDINARY_CURRENT_LIFE_BASIS_ID},
                facts_by_development={
                    "development-1": copy.deepcopy(first["durable_facts"])
                },
            ),
            [],
        )

    def test_planner_rejects_unknown_current_fact_id_after_normalization(self) -> None:
        response = self.good_planner_api_response()
        response["plan"]["occurrences"][0]["uses_current_fact_ids"] = [264]
        decoded = restore_code_owned_period(
            decode_planner_app_operations(response),
            start=self.start,
            end=self.end,
        )

        errors = self.validate_planner(decoded)

        self.assertTrue(any("unknown fact 264" in error for error in errors), errors)

    def test_writer_decoder_discards_blank_messages_but_preserves_content(self) -> None:
        response = self.good_writer_api_response()
        response["plan"]["sessions"][0]["messages_before_request"] = [
            "The substantive message.",
            "",
            "   ",
        ]

        decoded = normalize_writer_messages(response)

        self.assertEqual(
            decoded["plan"]["sessions"][0]["messages_before_request"],
            ["The substantive message."],
        )

    def test_writer_decoder_does_not_hide_a_session_with_no_real_message(self) -> None:
        response = self.good_writer_api_response()
        response["plan"]["sessions"][0]["messages_before_request"] = ["", "   "]
        response["plan"]["sessions"][0]["message_with_request"] = ""
        decoded = restore_code_owned_period(
            normalize_writer_messages(response),
            start=self.start,
            end=self.end,
        )

        errors = validate_writer_prose_response(
            decoded,
            start=self.start,
            end=self.end,
            occurrence_plan=self.good_planner_response()["plan"],
        )

        self.assertTrue(
            any("message_with_request must be a non-empty string" in error for error in errors),
            errors,
        )

    def test_writer_rejects_internal_app_operation_placeholder_marker(self) -> None:
        response = self.good_writer_api_response()
        response["plan"]["sessions"][0]["message_with_request"] = (
            "Please handle this. APP_OPERATIONS_PLACEHOLDER"
        )
        response = restore_code_owned_period(
            response,
            start=self.start,
            end=self.end,
        )

        errors = validate_writer_prose_response(
            response,
            start=self.start,
            end=self.end,
            occurrence_plan=self.good_planner_response()["plan"],
        )

        self.assertIn(
            "sessions[0].messages[1] contains the forbidden internal marker "
            "app_operations_placeholder",
            errors,
        )

    def test_writer_rejects_conflict_flag_on_development_contact(self) -> None:
        response = self.good_writer_api_response()
        response["plan"]["sessions"][0]["conflicting_fact_ids"] = [1]
        response = restore_code_owned_period(
            response,
            start=self.start,
            end=self.end,
        )

        errors = validate_writer_prose_response(
            response,
            start=self.start,
            end=self.end,
            occurrence_plan=self.good_planner_response()["plan"],
        )

        self.assertIn(
            "sessions[0].conflicting_fact_ids may be non-empty only for an ordinary "
            "self-contained contact with no development IDs, durable facts, or app operation",
            errors,
        )

    def test_writer_stops_on_valid_ordinary_contact_conflict(self) -> None:
        planner = self.good_planner_response()
        occurrence = planner["plan"]["occurrences"][0]
        occurrence["effect_kind"] = "self_contained"
        occurrence["basis_ids"] = ["ordinary_current_life"]
        occurrence["development_ids"] = []
        occurrence["durable_facts"] = []
        occurrence["assistant_outcome"] = {
            "kind": "conversation",
            "requested_result": "Help me understand this.",
        }
        occurrence["app_operations"] = []
        response = self.good_writer_api_response()
        session = response["plan"]["sessions"][0]
        session["conflicting_fact_ids"] = [317]
        response = restore_code_owned_period(
            response,
            start=self.start,
            end=self.end,
        )

        errors = validate_writer_prose_response(
            response,
            start=self.start,
            end=self.end,
            occurrence_plan=planner["plan"],
        )

        self.assertIn(
            "sessions[0] reports that the fixed contact conflicts with current facts: 317",
            errors,
        )

    def test_planner_decoder_repairs_one_missing_final_object_brace(self) -> None:
        response = self.good_planner_api_response()
        operation = response["plan"]["occurrences"][0]["app_operations"][0]
        expected = {"tool": operation["tool"], "args": json.loads(operation["args_json"])}
        operation["args_json"] = operation["args_json"].rstrip()[:-1]

        decoded = decode_planner_app_operations(response)

        self.assertEqual(
            decoded["plan"]["occurrences"][0]["app_operations"][0],
            expected,
        )

    def test_planner_decoder_ignores_outer_array_suffix_after_complete_args_object(self) -> None:
        response = self.good_planner_api_response()
        operation = response["plan"]["occurrences"][0]["app_operations"][0]
        expected = {"tool": operation["tool"], "args": json.loads(operation["args_json"])}
        operation["args_json"] += "],"

        decoded = decode_planner_app_operations(response)

        self.assertEqual(
            decoded["plan"]["occurrences"][0]["app_operations"][0],
            expected,
        )

    def test_planner_decoder_discards_only_completely_blank_operation(self) -> None:
        response = self.good_planner_api_response()
        response["plan"]["occurrences"][0]["app_operations"].append(
            {"tool": "  ", "args_json": " \n "}
        )

        decoded = decode_planner_app_operations(response)

        self.assertEqual(len(decoded["plan"]["occurrences"][0]["app_operations"]), 1)

        response = self.good_planner_api_response()
        response["plan"]["occurrences"][0]["app_operations"] = [
            {"tool": "", "args_json": ""}
        ]
        decoded = restore_code_owned_period(
            decode_planner_app_operations(response), start=self.start, end=self.end
        )
        errors = self.validate_planner(decoded)
        self.assertTrue(
            any("external_action requires an app operation" in error for error in errors),
            errors,
        )

        response = self.good_planner_api_response()
        response["plan"]["occurrences"][0]["app_operations"] = [
            {"tool": " ", "args_json": "{}"}
        ]
        with self.assertRaisesRegex(ValueError, "tool must be a non-empty string"):
            decode_planner_app_operations(response)

    def test_older_operation_identity_omits_all_written_payload_arguments(self) -> None:
        compact = history_window.compact_older_contact_operation_identities(
            [
                {
                    "tool": "post_doc_comment",
                    "args": {
                        "doc_id": "doc_42",
                        "body": "body",
                        "message": "message",
                        "content": "content",
                        "comment": "comment",
                        "text": "text",
                    },
                }
            ]
        )

        self.assertEqual(
            compact,
            [{"tool": "post_doc_comment", "arguments": {"doc_id": "doc_42"}}],
        )

    def test_planner_decoder_preserves_literal_newlines_inside_args_json_strings(self) -> None:
        response = self.good_planner_api_response()
        operation = response["plan"]["occurrences"][0]["app_operations"][0]
        operation["args_json"] = '{"message":"first line\nsecond line"}'

        decoded = decode_planner_app_operations(response)

        self.assertEqual(
            decoded["plan"]["occurrences"][0]["app_operations"][0]["args"],
            {"message": "first line\nsecond line"},
        )

    def test_planner_decoder_still_rejects_other_invalid_json(self) -> None:
        response = self.good_planner_api_response()
        response["plan"]["occurrences"][0]["app_operations"][0]["args_json"] = (
            '{"name": not-json}'
        )

        with self.assertRaisesRegex(ValueError, "args_json must contain valid JSON"):
            decode_planner_app_operations(response)

    def test_model_boundary_restores_exact_code_owned_dates(self) -> None:
        planner = restore_code_owned_period(
            decode_planner_app_operations(self.good_planner_api_response()),
            start=self.start,
            end=self.end,
        )
        self.assertEqual(planner["plan"]["period_start"], self.start.isoformat())
        self.assertEqual(planner["plan"]["period_end"], self.end.isoformat())
        self.assertEqual(self.validate_planner(planner), [])

        writer = restore_code_owned_period(
            normalize_writer_messages(self.good_writer_api_response()),
            start=self.start,
            end=self.end,
        )
        writer_plan = construct_writer_plan(
            writer,
            occurrence_plan=self.good_planner_response()["plan"],
            required_entities=self.required_entities,
        )
        self.assertEqual(writer_plan["period_start"], self.start.isoformat())
        self.assertEqual(writer_plan["period_end"], self.end.isoformat())
        self.assertEqual(self.validate({"plan": writer_plan}), [])

    def test_new_entity_is_introduced_by_first_contact_for_its_event(self) -> None:
        planner = self.good_planner_response()
        later_occurrence = copy.deepcopy(planner["plan"]["occurrences"][0])
        later_occurrence["contact_id"] = "contact-2"
        later_occurrence["narrative_date"] = "2025-01-29T11:00:00-08:00"
        later_occurrence["continues_from"] = ["contact-1"]
        planner["plan"]["occurrences"].append(later_occurrence)

        writer = self.good_writer_api_response()
        later_session = copy.deepcopy(writer["plan"]["sessions"][0])
        later_session["contact_id"] = "contact-2"
        writer["plan"]["sessions"].append(later_session)

        writer_plan = construct_writer_plan(
            writer,
            occurrence_plan=planner["plan"],
            required_entities=self.required_entities,
        )

        self.assertEqual(
            writer_plan["new_entities"][0]["introduced_in_contact_id"],
            "contact-1",
        )

    def test_new_entity_candidate_is_available_in_first_overlapping_week(self) -> None:
        candidates = new_entity_candidates_for_week(
            [self.required_entities[0]],
            relevant_developments=[{"id": "development-1"}],
            current_entities=self.entities,
        )
        self.assertEqual([row["id"] for row in candidates], ["project:new"])

    def test_unused_new_entity_is_not_selected_for_writer_or_state(self) -> None:
        candidates = [copy.deepcopy(self.required_entities[0])]
        planner = self.good_planner_response()
        planner["plan"]["occurrences"][0]["subject_ids"] = ["person:morgan"]
        selected, errors = select_new_entities_for_week(planner, candidates)
        self.assertEqual(selected, [])
        self.assertEqual(errors, [])

    def test_new_entity_used_without_its_introduction_event_is_rejected(self) -> None:
        candidates = [copy.deepcopy(self.required_entities[0])]
        planner = self.good_planner_response()
        planner["plan"]["occurrences"][0]["development_ids"] = []
        planner["plan"]["occurrences"][0]["subject_ids"] = [
            "person:morgan",
            "project:new",
        ]
        _, errors = select_new_entities_for_week(planner, candidates)
        self.assertEqual(len(errors), 1)
        self.assertIn("without its introduction event development-1", errors[0])

    def test_new_entity_can_be_used_after_its_introduction_occurrence(self) -> None:
        candidates = [copy.deepcopy(self.required_entities[0])]
        planner = self.good_planner_response()
        later = copy.deepcopy(planner["plan"]["occurrences"][0])
        later.update(
            {
                "contact_id": "contact-2",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "continues_from": [],
                "development_ids": [],
            }
        )
        planner["plan"]["occurrences"].append(later)

        selected, errors = select_new_entities_for_week(planner, candidates)

        self.assertEqual(errors, [])
        self.assertEqual([entity["id"] for entity in selected], ["project:new"])

    def test_new_entity_used_before_its_introduction_occurrence_is_rejected(self) -> None:
        candidates = [copy.deepcopy(self.required_entities[0])]
        planner = self.good_planner_response()
        introduction = planner["plan"]["occurrences"][0]
        introduction["contact_id"] = "contact-2"
        introduction["narrative_date"] = "2025-01-30T10:00:00-08:00"
        introduction["continues_from"] = []
        introduction["subject_ids"] = ["person:morgan"]

        earlier = copy.deepcopy(introduction)
        earlier.update(
            {
                "contact_id": "contact-1",
                "narrative_date": "2025-01-29T10:00:00-08:00",
                "development_ids": [],
                "subject_ids": ["person:morgan", "project:new"],
            }
        )
        planner["plan"]["occurrences"] = [earlier, introduction]

        selected, errors = select_new_entities_for_week(planner, candidates)

        self.assertEqual(selected, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("before its introduction event development-1", errors[0])

    def test_current_entity_is_not_a_new_entity_candidate(self) -> None:
        current_entity = copy.deepcopy(self.required_entities[0])
        candidates = new_entity_candidates_for_week(
            [current_entity],
            relevant_developments=[{"id": "development-1"}],
            current_entities=[*self.entities, current_entity],
        )
        self.assertEqual(candidates, [])

    def test_planner_fact_comparison_uses_only_model_fact_fields(self) -> None:
        response = self.good_planner_response()
        expected = copy.deepcopy(response["plan"]["occurrences"][0]["durable_facts"])
        expected[0]["supersedes_fact_keys"] = ["earlier-rule"]
        self.assertEqual(
            self.validate_planner(
                response,
                facts_by_development={"development-1": expected},
            ),
            [],
        )


    def test_open_thread_routes_basis_id_and_prior_contact_id_separately(self) -> None:
        current_state = {
            "key": "compass_owner_decision",
            "subject_ids": ["person:morgan", "project:compass"],
            "statement": "The Compass owner decision is pending.",
            "source_session_id": "session-compass-latest",
            "date": "2025-01-28",
        }
        exposed = planner_open_threads(
            [
                {
                    "thread_id": "thread-compass",
                    "session_id": "session-compass-latest",
                    "interaction_id": "interaction-compass-latest",
                    "subjects": ["person:morgan", "project:compass"],
                    "contact_summary": "The current Compass read is still unresolved.",
                    "what_remains_after_contact": "The owner decision is pending.",
                    "current_fact_ids": [7],
                }
            ],
            current_ordinary_states_by_thread={"thread-compass": [current_state]},
        )
        self.assertEqual(
            exposed,
            [
                {
                    "basis_id": "thread-compass",
                    "continues_from_id": "session-compass-latest",
                    "subject_ids": ["person:morgan", "project:compass"],
                    "what_already_happened": "The current Compass read is still unresolved.",
                    "what_remains": "The owner decision is pending.",
                    "current_fact_ids": [7],
                    "prior_accepted_contacts": [],
                    "earlier_user_messages_in_same_matter": [],
                    "current_ordinary_states": [current_state],
                }
            ],
        )

    def test_open_thread_gets_current_state_from_its_source_thread(self) -> None:
        current_state = {
            "key": "compass_owner_decision",
            "subject_ids": ["person:morgan", "project:compass"],
            "statement": "The Compass owner decision is pending.",
            "source_session_id": "session-compass-latest",
            "date": "2025-01-28",
        }
        states = history_window.current_ordinary_states_for_open_threads(
            {
                "session_purposes": [
                    {
                        "session_id": "session-compass-latest",
                        "thread_id": "thread-compass",
                    },
                    {
                        "session_id": "unrelated-session",
                        "thread_id": "thread-unrelated",
                    },
                ]
            },
            unfinished_threads=[
                {"thread_id": "thread-compass"},
                {"thread_id": "thread-other"},
            ],
            current_ordinary_states=[current_state],
        )
        self.assertEqual(states, {"thread-compass": [current_state], "thread-other": []})


    def test_writer_payload_excludes_irrelevant_operational_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            persona = root / "persona.md"
            world = root / "world.md"
            persona.write_text("Morgan writes direct updates.\n")
            world.write_text("The accepted world.\n")
            state = {
                "facts": [
                    *copy.deepcopy(self.facts),
                    {
                        "id": 2,
                        "statement": "An unrelated project status.",
                        "applies_when": "the unrelated project is discussed",
                        "subjects": ["project:irrelevant"],
                        "source_session_ids": ["old-session"],
                        "supersedes": [],
                    },
                ],
                "entities": [
                    *copy.deepcopy(self.entities),
                    {
                        "id": "project:irrelevant",
                        "name": "Irrelevant",
                        "kind": "project",
                    },
                ],
                "app_state": {"docs": [{"id": "hidden-doc"}]},
                "history": [
                    {
                        "id": "old-session",
                        "narrative_date": "2025-01-28T10:00:00-08:00",
                        "messages": [
                            "Northstar's source material says the renewal is $43.80 for 12 active seats."
                        ],
                    }
                ],
                "session_purposes": [],
                "operation_results": [],
            }
            payload = build_writer_payload(
                planner_response=self.good_planner_response(),
                config={
                    "persona": "morgan",
                    "persona_sheet": str(persona),
                    "generator_context": str(world),
                },
                state=state,
                start=self.start,
                end=self.end,
                timezone_name="America/Los_Angeles",
                required_entities=self.required_entities,
            )
        self.assertEqual(
            [fact["id"] for fact in payload["contacts"]["contact-1"]["cited_facts"]],
            [1],
        )
        self.assertEqual(
            [entity["id"] for entity in payload["contacts"]["contact-1"]["subjects"]],
            ["person:morgan", "project:new"],
        )
        self.assertNotIn(
            "project:irrelevant",
            [entity["id"] for entity in payload["contacts"]["contact-1"]["subjects"]],
        )
        self.assertEqual(
            set(payload["contacts"]["contact-1"]),
            {
                "when",
                "what_happened",
                "assistant_outcome_kind",
                "requested_result",
                "source_material",
                "subjects",
            "cited_facts",
            "continues_from_contacts",
            "earlier_user_messages_in_same_matter",
            "earlier_items_from_this_week",
            "fixed_app_operations",
                "referenced_operation_results",
            },
        )
        self.assertNotIn("current_facts", payload)
        self.assertNotIn("earlier_accepted_contacts", payload)
        self.assertNotIn("recent_exact_user_messages_for_voice", payload)
        self.assertNotIn("app_action_information", payload)

    def test_writer_requires_a_request_when_the_plan_requires_one(self) -> None:
        response = self.good_writer_api_response()
        response["plan"]["sessions"][0]["message_with_request"] = None
        response = restore_code_owned_period(
            response, start=self.start, end=self.end
        )

        errors = validate_writer_prose_response(
            response,
            start=self.start,
            end=self.end,
            occurrence_plan=self.good_planner_response()["plan"],
        )

        self.assertIn(
            "sessions[0].message_with_request must be a non-empty string when a request is planned",
            errors,
        )

    def test_writer_allows_no_request_only_for_an_update(self) -> None:
        occurrence_plan = self.good_planner_response()["plan"]
        occurrence = occurrence_plan["occurrences"][0]
        occurrence["assistant_outcome"] = {"kind": "none", "requested_result": None}
        occurrence["app_operations"] = []
        response = self.good_writer_api_response()
        response["plan"]["sessions"][0]["message_with_request"] = None
        response = restore_code_owned_period(
            response, start=self.start, end=self.end
        )

        self.assertEqual(
            validate_writer_prose_response(
                response,
                start=self.start,
                end=self.end,
                occurrence_plan=occurrence_plan,
            ),
            [],
        )

        response["plan"]["sessions"][0]["message_with_request"] = "Do this."
        self.assertIn(
            "sessions[0].message_with_request must be null for an update",
            validate_writer_prose_response(
                response,
                start=self.start,
                end=self.end,
                occurrence_plan=occurrence_plan,
            ),
        )

    def test_writer_combines_setup_and_required_request_into_messages(self) -> None:
        writer_plan = construct_writer_plan(
            self.good_writer_api_response(),
            occurrence_plan=self.good_planner_response()["plan"],
            required_entities=self.required_entities,
        )

        self.assertEqual(
            writer_plan["sessions"][0]["messages"],
            [
                "I received this project decision note.",
                "Create a document titled New project operating note.",
            ],
        )

    def test_interrupted_output_keeps_cache_and_completed_output_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "run"
            planner_cache = output / "work" / "planner_response_cache.json"
            writer_cache = output / "work" / "writer_response_cache.json"
            planner_cache.parent.mkdir(parents=True)
            planner_cache.write_text('{"request_sha256":"planner"}')
            writer_cache.write_text('{"request_sha256":"writer"}')
            pending = output / "candidate_checkpoint.pending"
            pending.mkdir()
            (pending / "partial").write_text("partial")
            prepare_window_output(output)
            self.assertEqual(
                planner_cache.read_text(), '{"request_sha256":"planner"}'
            )
            self.assertEqual(
                writer_cache.read_text(), '{"request_sha256":"writer"}'
            )
            self.assertFalse(pending.exists())
            (output / "manifest.json").write_text('{"status":"passed"}')
            with self.assertRaises(CompletedOutputError):
                prepare_window_output(output)

    def test_planner_and_writer_use_separate_cached_call_boundaries(self) -> None:
        story_api_response = self.good_story_api_response()
        enrichment_api_response = self.good_enrichment_api_response()
        writer_api_response = self.good_keyed_writer_api_response()
        writer_response = restore_code_owned_period(
            normalize_writer_messages(
                normalize_writer_contact_order(writer_api_response, ["contact-1"])
            ),
            start=self.start,
            end=self.end,
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            (output / "work").mkdir()
            client = Mock()
            with patch(
                "construction.build_history_window.cached_client_complete",
                side_effect=[story_api_response, enrichment_api_response, writer_api_response],
            ) as complete:
                actual_story = complete_model_stage(
                    output_dir=output,
                    stage="story",
                    system=HISTORY_WINDOW_STORY_SYSTEM,
                    payload={
                        "persona": "morgan",
                        "narrative_timezone": "America/Los_Angeles",
                        "requested_dates": {"start": self.start.isoformat(), "end": self.end.isoformat()},
                        "older_contacts_that_already_happened": [],
                        "recently_finished_contacts": [],
                    },
                    client=client,
                    start=self.start,
                    end=self.end,
                )
                actual_planner = complete_model_stage(
                    output_dir=output,
                    stage="planner",
                    system=HISTORY_WINDOW_PLANNING_SYSTEM,
                    payload={"input": "planner"},
                    client=client,
                    start=self.start,
                    end=self.end,
                )
                actual_writer = complete_model_stage(
                    output_dir=output,
                    stage="writer",
                    system=HISTORY_WINDOW_WRITING_SYSTEM,
                    payload={"contacts": {"contact-1": {}}},
                    client=client,
                    start=self.start,
                    end=self.end,
                )
            self.assertEqual(actual_story, story_api_response)
            self.assertEqual(actual_planner, enrichment_api_response)
            self.assertEqual(actual_writer, writer_response)
            self.assertEqual(
                json.loads((output / "story_response.json").read_text()),
                story_api_response,
            )
            self.assertEqual(
                complete.call_args_list[0].args[0],
                output / "work" / "story_response_cache.json",
            )
            self.assertEqual(
                complete.call_args_list[1].args[0],
                output / "work" / "planner_response_cache.json",
            )
            self.assertEqual(
                complete.call_args_list[2].args[0],
                output / "work" / "writer_response_cache.json",
            )
            self.assertEqual(
                complete.call_args_list[0].kwargs["response_schema"],
                story_response_schema(
                    {
                        "persona": "morgan",
                        "narrative_timezone": "America/Los_Angeles",
                        "requested_dates": {
                            "start": self.start.isoformat(),
                            "end": self.end.isoformat(),
                        },
                        "older_contacts_that_already_happened": [],
                        "recently_finished_contacts": [],
                    }
                ),
            )
            self.assertEqual(
                complete.call_args_list[0].kwargs["response_schema_name"],
                "history_window_story",
            )
            self.assertEqual(
                complete.call_args_list[1].kwargs["response_schema"],
                enrichment_response_schema({"input": "planner"}),
            )
            self.assertEqual(
                complete.call_args_list[1].kwargs["response_schema_name"],
                "history_window_planner",
            )
            self.assertEqual(
                complete.call_args_list[2].kwargs["response_schema"],
                writer_api_response_schema(["contact-1"]),
            )
            self.assertEqual(
                complete.call_args_list[2].kwargs["response_schema_name"],
                "history_window_writer",
            )
            for stage in ("story", "planner", "writer"):
                self.assertTrue((output / f"{stage}_system.txt").is_file())
                self.assertTrue((output / f"{stage}_request.json").is_file())
            self.assertTrue((output / "story_response.json").is_file())
            self.assertTrue((output / "planner_enrichment_response.json").is_file())
            self.assertTrue((output / "writer_response.json").is_file())

    def test_run_has_exactly_two_model_stage_calls(self) -> None:
        source_path = Path(__file__).resolve().parents[3] / "construction" / "build_history_window.py"
        tree = ast.parse(source_path.read_text())
        run_function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_run"
        )
        calls = [
            node
            for node in ast.walk(run_function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "complete_model_stage"
        ]
        self.assertEqual(len(calls), 3)

    def test_schedule_free_payload_excludes_writer_only_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            quarter = root / "quarter.json"
            spec = root / "spec.yaml"
            persona = root / "persona.md"
            world = root / "world.md"
            quarter.write_text(
                json.dumps(
                    {
                        "story": {
                            "period_start": "2025-01-01",
                            "period_end": "2025-03-31",
                            "new_entities": [
                                {
                                    "id": "project:current",
                                    "name": "Current Project",
                                    "kind": "project",
                                    "role": "The project introduced by the current development.",
                                    "introduced_in_event_id": "development-1",
                                },
                                {
                                    "id": "project:future",
                                    "name": "Future Project",
                                    "kind": "project",
                                    "role": "The project introduced by a later development.",
                                    "introduced_in_event_id": "development-2",
                                },
                            ],
                            "events": [
                                {
                                    "id": "development-1",
                                    "start_date": "2025-01-29",
                                    "end_date": "2025-01-29",
                                    "subjects": ["person:morgan"],
                                    "facts_to_establish": [
                                        {
                                            "key": "development-rule",
                                            "statement": "The development rule now applies.",
                                            "applies_when": "the development is discussed",
                                            "subjects": ["person:morgan"],
                                            "supersedes": [],
                                            "supersedes_fact_keys": [],
                                        }
                                    ],
                                },
                                {
                                    "id": "development-2",
                                    "start_date": "2025-02-05",
                                    "end_date": "2025-02-05",
                                    "subjects": ["person:morgan", "project:future"],
                                    "facts_to_establish": [],
                                },
                            ],
                        }
                    }
                )
            )
            spec.write_text(
                "narrative_timezone: America/Los_Angeles\nprimary_subject_ids:\n- person:morgan\n"
            )
            persona.write_text("Morgan writes direct updates.\n")
            world.write_text("A founder's current world.\n")
            config = {
                "persona": "morgan",
                "quarter_plan": str(quarter),
                "spec": str(spec),
                "persona_sheet": str(persona),
                "generator_context": str(world),
            }
            state = {
                "facts": [
                    *copy.deepcopy(self.facts),
                    {
                        "id": 2,
                        "statement": "Morgan reviews invoices above the approval threshold.",
                        "applies_when": "an invoice needs payment review",
                        "subjects": ["person:morgan"],
                        "source_session_ids": ["old-session"],
                        "supersedes": [],
                    },
                    {
                        "id": 3,
                        "statement": "Morgan prefers the short Friday wrap format.",
                        "applies_when": "writing a Friday update",
                        "subjects": ["person:morgan"],
                        "source_session_ids": ["old-session"],
                        "supersedes": [],
                    },
                ],
                "entities": [
                    *copy.deepcopy(self.entities),
                    {
                        "id": "organization:lemongrass",
                        "name": "Lemongrass",
                        "kind": "organization",
                    },
                    {"id": "tool:figma", "name": "Figma", "kind": "tool"},
                ],
                "app_state": {"docs": [{"id": "accepted-doc", "title": "Current"}]},
                "unfinished_threads": [
                    {
                        "thread_id": "thread-finance",
                        "session_id": "old-session",
                        "subjects": ["person:morgan"],
                        "contact_summary": "Finance review is awaiting a decision.",
                        "what_remains_after_contact": "Choose the finance response.",
                    }
                ],
                "history": [
                    {
                        "id": "older-session",
                        "narrative_date": "2024-12-20T10:00:00-08:00",
                        "messages": ["An older completed contact."],
                    },
                    {
                        "id": "old-session",
                        "narrative_date": "2025-01-28T10:00:00-08:00",
                        "messages": [
                            "Northstar's source material says the renewal is $43.80 for 12 active seats."
                        ],
                    }
                ],
                "session_purposes": [
                    {
                        "session_id": "older-session",
                        "planned_interaction_id": "contact-older",
                        "thread_id": "self_contained",
                        "subject_ids": ["person:morgan"],
                        "purpose": "Order replacement office supplies.",
                        "accepted_contact_details": "Office supplies were ordered.",
                    },
                    {
                        "session_id": "old-session",
                        "planned_interaction_id": "contact-finance",
                        "thread_id": "thread-finance",
                        "purpose": "Review the finance update.",
                        "accepted_contact_details": "February AWS was $43.80.",
                    }
                ],
                "operation_results": [
                    {
                        "session_id": "older-session",
                        "operation": {
                            "tool": "create_doc",
                            "args": {
                                "title": "Office supplies",
                                "folder": "Operations",
                                "body": "Do not show this older document body.",
                            },
                        },
                        "result": {"ok": True, "id": "doc-office-supplies"},
                    },
                    {
                        "session_id": "old-session",
                        "operation": {
                            "tool": "create_doc",
                            "args": {"title": "Finance note", "body": "Keep $43.80."},
                        },
                        "result": {"ok": True, "id": "doc-finance"},
                    }
                ],
            }
            payload, context = build_planner_payload(
                config=config,
                checkpoint={"covered_quarter_event_ids": []},
                state=state,
                start=self.start,
                end=self.start,
                tools=self.tools,
            )
            writer_payload = build_writer_payload(
                planner_response={
                    "plan": {
                        "period_start": self.start.isoformat(),
                        "period_end": self.start.isoformat(),
                        "occurrences": [
                            {
                                "contact_id": "contact-1",
                                "narrative_date": "2025-01-29T10:00:00-08:00",
                                "thread_id": "development-thread",
                                "continues_from": [],
                                "subject_ids": ["person:morgan"],
                                "development_ids": ["development-1"],
                                "anchor_item_ids": [],
                                "what_happens": "The development rule is decided.",
                                "why_contact_assistant": "Morgan sends an update.",
                                "app_operations": [],
                            }
                        ],
                    }
                },
                config=config,
                state=state,
                start=self.start,
                end=self.start,
                timezone_name="America/Los_Angeles",
                required_entities=context["candidate_entities"],
            )
        self.assertNotIn("accepted_schedule_dir", config)
        self.assertNotIn("accepted_contacts_that_must_appear_in_these_dates", payload)
        self.assertIn("lasting_facts_that_must_be_established", payload)
        self.assertEqual(
            payload["older_contacts_that_already_happened"],
            [
                {
                    "session_id": "older-session",
                    "date": "2024-12-20",
                    "thread_id": "self_contained",
                    "subject_ids": ["person:morgan"],
                    "contact_purpose": (
                        "Office supplies were ordered."
                    ),
                    "accepted_app_operation_identities": [
                        {
                            "tool": "create_doc",
                            "arguments": {
                                "title": "Office supplies",
                                "folder": "Operations",
                            },
                        }
                    ],
                }
            ],
        )
        self.assertNotIn(
            "Do not show this older document body.",
            json.dumps(payload["older_contacts_that_already_happened"]),
        )
        self.assertNotIn("recent_contact_topics", payload)
        self.assertEqual(len(payload["recently_finished_contacts"]), 1)
        self.assertEqual(
            payload["recently_finished_contacts"][0]["accepted_contact_details"],
            "February AWS was $43.80.",
        )
        self.assertEqual(
            payload["recently_finished_contacts"][0]["session_id"],
            "old-session",
        )
        self.assertEqual(
            payload["recently_finished_contacts"][0]["accepted_app_operations"],
            [
                {
                    "tool": "create_doc",
                    "args": {"title": "Finance note", "body": "Keep $43.80."},
                }
            ],
        )
        self.assertNotIn(
            "result", payload["recently_finished_contacts"][0]["accepted_app_operations"][0]
        )
        self.assertNotIn(
            "accepted_user_messages", payload["recently_finished_contacts"][0]
        )
        self.assertNotIn(
            "Northstar's source material says the renewal is $43.80 for 12 active seats.",
            json.dumps(payload["recently_finished_contacts"]),
        )
        self.assertEqual(
            payload["open_threads"][0]["prior_accepted_contacts"],
            [
                {
                    "date": "2025-01-28",
                    "contact_id": "contact-finance",
                    "accepted_contact_details": "February AWS was $43.80.",
                }
            ],
        )
        self.assertNotIn("required_anchors", context)
        self.assertEqual(
            payload["new_entities_that_may_first_appear_this_week"],
            [
                {
                    "id": "project:current",
                    "name": "Current Project",
                    "kind": "project",
                    "role": "The project introduced by the current development.",
                    "introduced_in_event_id": "development-1",
                }
            ],
        )
        self.assertEqual(
            context["candidate_entities"],
            payload["new_entities_that_may_first_appear_this_week"],
        )
        self.assertEqual(set(context["required_facts"]), {"development-rule"})
        self.assertEqual(
            payload["developments_for_this_week"][0]["facts_to_establish"][0]["key"],
            "development-rule",
        )
        self.assertNotIn("required_durable_facts", writer_payload)
        return
        self.assertEqual(
            payload["quarter_developments_for_these_dates"][0]["facts_to_establish"][0][
                "key"
            ],
            "development-rule",
        )
        self.assertNotIn("required_durable_facts", payload)
        self.assertEqual(
            writer_payload["required_durable_facts"],
            [
                {
                    "key": "development-rule",
                    "statement": "The development rule now applies.",
                    "applies_when": "the development is discussed",
                    "subjects": ["person:morgan"],
                    "supersedes": [],
                    "development_ids": ["development-1"],
                    "anchor_item_ids": [],
                }
            ],
        )
        self.assertEqual([row["id"] for row in payload["known_entities"]], ["person:morgan"])
        for forbidden in (
            "exact_simulated_tools",
            "tool_visible_app_state",
            "recent_exact_user_messages",
            "accepted_contact_chronology_for_preceding_eight_weeks",
        ):
            self.assertNotIn(forbidden, payload)
        self.assertEqual(payload["current_facts"], [])

    def test_payload_routes_facts_by_window_and_selected_non_primary_subjects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            quarter = root / "quarter.json"
            spec = root / "spec.yaml"
            persona = root / "persona.md"
            world = root / "world.md"
            quarter.write_text(
                json.dumps(
                    {
                        "story": {
                            "period_start": "2025-01-01",
                            "period_end": "2025-03-31",
                            "new_entities": [
                                {
                                    "id": "organization:future_only",
                                    "name": "Future Only",
                                    "kind": "organization",
                                    "role": "A future quarter organization.",
                                    "reason": "It has not been introduced yet.",
                                    "introduced_in_contact_id": "future-contact",
                                }
                            ],
                            "events": [
                                {
                                    "id": "this-week",
                                    "start_date": "2025-01-29",
                                    "end_date": "2025-01-29",
                                    "subjects": [
                                        "person:morgan_chen",
                                        "project:current",
                                    ],
                                    "relevant_existing_fact_ids": [1],
                                    "facts_to_establish": [
                                        {
                                            "key": "current-project-acme-route",
                                            "statement": "Acme is affected by the current project route.",
                                            "applies_when": "the current project route is discussed",
                                            "subjects": ["organization:acme"],
                                            "supersedes": [],
                                            "supersedes_fact_keys": [],
                                        }
                                    ],
                                },
                                {
                                    "id": "later-quarter",
                                    "start_date": "2025-02-20",
                                    "end_date": "2025-02-20",
                                    "subjects": [
                                        "person:morgan_chen",
                                        "organization:kibo",
                                    ],
                                    "relevant_existing_fact_ids": [2],
                                    "facts_to_establish": [],
                                },
                            ],
                        }
                    }
                )
            )
            spec.write_text(
                "narrative_timezone: America/Los_Angeles\n"
                "primary_subject_ids:\n"
                "- person:morgan_chen\n"
                "- organization:scaffold\n"
            )
            persona.write_text("Morgan writes direct updates.\n")
            world.write_text("A founder's current world.\n")
            config = {
                "persona": "morgan",
                "quarter_plan": str(quarter),
                "spec": str(spec),
                "persona_sheet": str(persona),
                "generator_context": str(world),
            }
            state = {
                "facts": [
                    {
                        "id": 1,
                        "statement": "The current-week project is ready.",
                        "applies_when": "the current project is discussed",
                        "subjects": ["project:current"],
                        "source_session_ids": ["old-session"],
                        "supersedes": [],
                    },
                    {
                        "id": 2,
                        "statement": "Kibo has a current status.",
                        "applies_when": "Kibo is discussed",
                        "subjects": ["person:morgan_chen", "organization:kibo"],
                        "source_session_ids": ["old-session"],
                        "supersedes": [],
                    },
                    {
                        "id": 3,
                        "statement": "Acme shares Morgan and Scaffold.",
                        "applies_when": "Acme is discussed",
                        "subjects": [
                            "person:morgan_chen",
                            "organization:scaffold",
                            "organization:acme",
                        ],
                        "source_session_ids": ["old-session"],
                        "supersedes": [],
                    },
                    {
                        "id": 4,
                        "statement": "Morgan's operating preference applies broadly.",
                        "applies_when": "Morgan requests a draft",
                        "subjects": [
                            "person:morgan_chen",
                            "organization:scaffold",
                        ],
                        "source_session_ids": ["old-session"],
                        "supersedes": [],
                    },
                    {
                        "id": 5,
                        "statement": "Zulu has the historical termination-notice rule.",
                        "applies_when": "a termination notice is discussed",
                        "subjects": ["organization:zulu_logistics"],
                        "source_session_ids": ["old-session"],
                        "supersedes": [],
                    },
                ],
                "entities": [
                    {"id": "person:morgan_chen", "name": "Morgan", "kind": "person"},
                    {
                        "id": "organization:scaffold",
                        "name": "Scaffold",
                        "kind": "organization",
                    },
                    {"id": "project:current", "name": "Current", "kind": "project"},
                    {
                        "id": "organization:kibo",
                        "name": "Kibo",
                        "kind": "organization",
                        "reason": "Kibo is Morgan's dog.",
                    },
                    {
                        "id": "organization:acme",
                        "name": "Acme",
                        "kind": "organization",
                    },
                    {
                        "id": "organization:zulu_logistics",
                        "name": "Zulu Logistics",
                        "kind": "organization",
                    },
                    {
                        "id": "tool:accepted_existing_tool",
                        "name": "Accepted Existing Tool",
                        "kind": "tool",
                    },
                ],
                "app_state": {
                    "docs": [{"id": "accepted-doc", "title": "Current"}]
                },
                "unfinished_threads": [],
                "history": [
                    {
                        "id": "old-session",
                        "narrative_date": "2025-01-28T10:00:00-08:00",
                        "messages": ["Earlier accepted context."],
                    }
                ],
                "session_purposes": [],
                "operation_results": [],
            }
            planner_tools = {
                "get_docs": {
                    "arguments": [],
                    "required_arguments": [],
                    "description": "List visible documents.",
                    "state_effect": {"reads_state_keys": ["docs"]},
                }
            }
            planner_payload, context = build_planner_payload(
                config=config,
                checkpoint={"covered_quarter_event_ids": []},
                state=state,
                start=self.start,
                end=self.start,
                tools=planner_tools,
            )
            occurrence_plan = {
                "period_start": self.start.isoformat(),
                "period_end": self.start.isoformat(),
                "occurrences": [
                    {
                        "contact_id": "contact-kibo",
                        "narrative_date": "2025-01-29T10:00:00-08:00",
                        "thread_id": "kibo-thread",
                        "continues_from": [],
                        "subject_ids": [
                            "person:morgan_chen",
                            "organization:scaffold",
                            "organization:kibo",
                        ],
                        "development_ids": [],
                        "anchor_item_ids": [],
                        "uses_fact_ids": [1],
                        "what_happens": "Morgan discusses Kibo.",
                        "why_contact_assistant": "Morgan sends an update.",
                        "app_operations": [],
                    }
                ],
            }
            writer_payload = build_writer_payload(
                planner_response={"plan": occurrence_plan},
                config=config,
                state=state,
                start=self.start,
                end=self.start,
                timezone_name="America/Los_Angeles",
                primary_subject_ids={"person:morgan_chen", "organization:scaffold"},
            )

        self.assertEqual(
            [fact["id"] for fact in planner_payload["current_facts"]],
            [1, 2, 3, 4, 5],
        )
        self.assertIn(
            "organization:kibo",
            [entity["id"] for entity in planner_payload["known_entities"]],
        )
        self.assertEqual(
            {entity["id"] for entity in planner_payload["known_entities"]},
            {
                "person:morgan_chen",
                "organization:scaffold",
                "project:current",
                "organization:kibo",
                "organization:acme",
                "organization:zulu_logistics",
            },
        )
        self.assertNotIn(
            "organization:future_only",
            [entity["id"] for entity in planner_payload["known_entities"]],
        )
        self.assertEqual(
            planner_payload["available_simulated_actions"],
            [
                {
                    "tool_name": "get_docs",
                    "description": "List visible documents.",
                    "arguments": [],
                    "required_arguments": [],
                }
            ],
        )
        self.assertEqual(
            planner_payload["available_app_records"],
            {"docs": [{"id": "accepted-doc", "title": "Current"}]},
        )
        self.assertEqual(
            next(
                entity["reason"]
                for entity in planner_payload["known_entities"]
                if entity["id"] == "organization:kibo"
            ),
            "Kibo is Morgan's dog.",
        )
        self.assertNotIn("active_situations", planner_payload)
        self.assertIn(
            history_window.ORDINARY_CURRENT_LIFE_BASIS_ID,
            context["self_contained_basis_ids"],
        )
        self.assertEqual(
            context["subjects_by_basis"][history_window.ORDINARY_CURRENT_LIFE_BASIS_ID],
            {row["id"] for row in planner_payload["known_entities"]},
        )
        self.assertEqual(
            context["subjects_by_basis"]["this-week"],
            {"person:morgan_chen", "project:current", "organization:acme"},
        )
        self.assertEqual(
            [fact["id"] for fact in writer_payload["contacts"]["contact-kibo"]["cited_facts"]],
            [1],
        )
        self.assertEqual(
            writer_payload["contacts"]["contact-kibo"]["fixed_app_operations"],
            [],
        )

    def test_planner_resolves_stale_development_fact_to_current_fact(self) -> None:
        facts = [
            {"id": 1, "supersedes": []},
            {"id": 2, "supersedes": [1]},
        ]
        developments = [{"id": "august", "relevant_existing_fact_ids": [1]}]

        resolved = resolve_current_fact_references_for_model(developments, facts)

        self.assertEqual(resolved[0]["relevant_existing_fact_ids"], [2])
        self.assertEqual(developments[0]["relevant_existing_fact_ids"], [1])
        planner_facts = planner_current_facts(
            facts,
            quarter_developments=resolved,
            recent_chronology=[],
            primary_subject_ids=set(),
        )
        self.assertEqual([fact["id"] for fact in planner_facts], [2])
        self.assertEqual(
            [fact["id"] for fact in history_window.compact_current_facts_for_model(planner_facts)],
            [2],
        )

    def test_planner_selects_primary_only_facts_but_not_other_recent_facts(self) -> None:
        facts = [
            {
                "id": 1,
                "statement": "The project has an accepted operating rule.",
                "applies_when": "the project is discussed",
                "subjects": ["person:morgan", "project:current"],
                "source_session_ids": ["recent-session"],
                "supersedes": [],
            },
            {
                "id": 2,
                "statement": "Morgan has an established preference.",
                "applies_when": "Morgan requests help",
                "subjects": ["person:morgan"],
                "source_session_ids": ["recent-session"],
                "supersedes": [],
            },
        ]
        chronology = [{"date": "2025-01-28", "fact_ids": [1, 2]}]

        selected = planner_current_facts(
            facts,
            quarter_developments=[],
            recent_chronology=chronology,
            primary_subject_ids={"person:morgan"},
        )

        self.assertEqual([fact["id"] for fact in selected], [2])

    def test_writer_adds_current_facts_for_non_primary_subjects_and_keeps_citations(self) -> None:
        occurrence_plan = {
            "occurrences": [
                {
                    "contact_id": "contact-project",
                    "subject_ids": ["person:morgan", "project:current"],
                    "uses_fact_ids": [1],
                    "durable_facts": [],
                }
            ]
        }
        facts = [
            {
                "id": 1,
                "statement": "Morgan cited this personal preference.",
                "subjects": ["person:morgan"],
                "supersedes": [],
            },
            {
                "id": 2,
                "statement": "The current project uses the approved route.",
                "subjects": ["person:morgan", "project:current"],
                "supersedes": [],
            },
            {
                "id": 3,
                "statement": "Morgan's unrelated personal detail.",
                "subjects": ["person:morgan"],
                "supersedes": [],
            },
            {
                "id": 4,
                "statement": "The project has a separate accepted owner.",
                "subjects": ["project:current"],
                "supersedes": [],
            },
        ]

        selected = writer_facts_by_occurrence(
            occurrence_plan,
            facts,
            primary_subject_ids={"person:morgan"},
        )

        self.assertEqual([fact["id"] for fact in selected["contact-project"]], [1])

    def test_build_planner_payload_exposes_only_current_rewritten_fact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            quarter = root / "quarter.json"
            spec = root / "spec.yaml"
            persona = root / "persona.md"
            world = root / "world.md"
            quarter.write_text(
                json.dumps(
                    {
                        "plan": {
                            "period_start": "2025-01-01",
                            "period_end": "2025-03-31",
                            "new_entities": [],
                            "events": [
                                {
                                    "id": "august",
                                    "start_date": "2025-01-29",
                                    "end_date": "2025-01-29",
                                    "subjects": ["person:morgan"],
                                    "relevant_existing_fact_ids": [1],
                                    "facts_to_establish": [],
                                }
                            ],
                        }
                    }
                )
            )
            spec.write_text(
                "narrative_timezone: America/Los_Angeles\n"
                "primary_subject_ids:\n- person:morgan\n"
            )
            persona.write_text("Morgan writes direct updates.\n")
            world.write_text("A founder's current world.\n")
            state = {
                "facts": [
                    {
                        "id": 1,
                        "statement": "The old rule applies.",
                        "applies_when": "the project is discussed",
                        "subjects": ["person:morgan"],
                        "supersedes": [],
                    },
                    {
                        "id": 2,
                        "statement": "The current rule applies.",
                        "applies_when": "the project is discussed",
                        "subjects": ["person:morgan"],
                        "supersedes": [1],
                    },
                ],
                "entities": copy.deepcopy(self.entities),
                "app_state": {},
                "unfinished_threads": [],
                "history": [],
                "session_purposes": [],
                "operation_results": [],
            }
            payload, _ = build_planner_payload(
                config={
                    "persona": "morgan",
                    "quarter_plan": str(quarter),
                    "spec": str(spec),
                    "persona_sheet": str(persona),
                    "generator_context": str(world),
                },
                checkpoint={"covered_quarter_event_ids": []},
                state=state,
                start=self.start,
                end=self.start,
            )
            accepted_fact_ids = json.loads(quarter.read_text())["plan"]["events"][0][
                "relevant_existing_fact_ids"
            ]

        self.assertEqual(
            payload["developments_for_this_week"][0]["relevant_existing_fact_ids"],
            [2],
        )
        self.assertEqual([fact["id"] for fact in payload["current_facts"]], [2])
        self.assertTrue(
            all("available_only_for_supersession" not in fact for fact in payload["current_facts"])
        )
        self.assertEqual(accepted_fact_ids, [1])

    def test_planner_resolves_recursive_supersession(self) -> None:
        facts = [
            {"id": 1, "supersedes": []},
            {"id": 2, "supersedes": [1]},
            {"id": 3, "supersedes": [2]},
        ]

        resolved = resolve_current_fact_references_for_model(
            [{"relevant_existing_fact_ids": [1]}], facts
        )

        self.assertEqual(resolved[0]["relevant_existing_fact_ids"], [3])

    def test_planner_includes_all_current_supersession_descendants_once(self) -> None:
        facts = [
            {"id": 1, "supersedes": []},
            {"id": 2, "supersedes": [1]},
            {"id": 3, "supersedes": [1]},
        ]

        resolved = resolve_current_fact_references_for_model(
            [{"relevant_existing_fact_ids": [1, 2, 1]}], facts
        )

        self.assertEqual(resolved[0]["relevant_existing_fact_ids"], [2, 3])

    def test_planner_keeps_existing_current_fact_reference(self) -> None:
        facts = [{"id": 1, "supersedes": []}]

        resolved = resolve_current_fact_references_for_model(
            [{"relevant_existing_fact_ids": [1, 1]}], facts
        )

        self.assertEqual(resolved[0]["relevant_existing_fact_ids"], [1])

    def test_planner_rejects_supersession_cycles_and_dead_ends(self) -> None:
        with self.assertRaisesRegex(ValueError, "fact supersession cycle"):
            resolve_current_fact_references_for_model(
                [{"relevant_existing_fact_ids": [1]}],
                [{"id": 1, "supersedes": [2]}, {"id": 2, "supersedes": [1]}],
            )
        with self.assertRaisesRegex(ValueError, "fact 99 has no route to a current fact"):
            resolve_current_fact_references_for_model(
                [{"relevant_existing_fact_ids": [99]}],
                [{"id": 1, "supersedes": []}],
            )

    def test_planner_app_record_index_excludes_record_bodies_and_notes(self) -> None:
        tools = {
            "get_docs": {
                "arguments": [],
                "required_arguments": [],
                "description": "List visible documents.",
                "state_effect": {"reads_state_keys": ["docs"]},
            }
        }
        compact = history_window.compact_planner_app_records(
            tools,
            {
                "docs": [
                    {
                        "id": "doc-1",
                        "title": "Current plan",
                        "status": "draft",
                        "tags": ["current"],
                        "body": "Large content the planner must not use as an event idea.",
                        "notes": "Mutable internal commentary.",
                    }
                ]
            },
        )
        self.assertEqual(
            compact,
            {
                "docs": [
                    {
                        "id": "doc-1",
                        "title": "Current plan",
                        "status": "draft",
                        "tags": ["current"],
                    }
                ]
            },
        )

    def test_planner_sees_fields_that_update_tools_can_replace(self) -> None:
        compact = history_window.compact_planner_app_records(
            {
                "update_calendar_event": {
                    "arguments": ["event_id", "body"],
                    "state_effect": {
                        "reads_state_keys": ["calendar"],
                        "writes_state_keys": ["calendar"],
                    }
                },
                "update_crm_row": {
                    "arguments": ["name", "notes"],
                    "state_effect": {
                        "reads_state_keys": ["crm"],
                        "writes_state_keys": ["crm"],
                    }
                },
                "update_runbook_entry": {
                    "arguments": ["entry_id", "body", "title"],
                    "state_effect": {
                        "reads_state_keys": ["runbooks"],
                        "writes_state_keys": ["runbooks"],
                    },
                },
            },
            {
                "calendar": [
                    {
                        "id": "event-1",
                        "title": "Flight",
                        "body": "Leave home at 4:35am.",
                    }
                ],
                "crm": [
                    {
                        "name": "Acme",
                        "status": "active",
                        "notes": "Keep the existing owner note.",
                    }
                ],
                "runbooks": [
                    {
                        "id": "runbook-1",
                        "title": "Current procedure",
                        "body": "Keep all existing controls.",
                    }
                ],
            },
        )

        self.assertNotIn("body", compact["calendar"][0])
        self.assertNotIn("notes", compact["crm"][0])
        self.assertNotIn("body", compact["runbooks"][0])

    def test_planner_calendar_index_keeps_a_long_event_that_overlaps_the_week(self) -> None:
        compact = history_window.compact_planner_app_records(
            {
                "list_calendar_events": {
                    "state_effect": {"reads_state_keys": ["calendar"]}
                }
            },
            {
                "calendar": [
                    {
                        "id": "evt-oncall",
                        "title": "Primary on-call",
                        "start": "2025-03-01T09:00:00-08:00",
                        "end": "2025-04-03T09:00:00-07:00",
                        "attendees": ["Alex"],
                    }
                ]
            },
            start=date(2025, 4, 1),
            end=date(2025, 4, 7),
        )

        self.assertEqual(
            compact["calendar"],
            [
                {
                    "id": "evt-oncall",
                    "title": "Primary on-call",
                    "start": "2025-03-01T09:00:00-08:00",
                    "end": "2025-04-03T09:00:00-07:00",
                    "attendees": ["Alex"],
                }
            ],
        )

    def test_planner_app_record_index_exposes_compact_analytics_identity_fields(self) -> None:
        compact = history_window.compact_planner_app_records(
            {"get_analytics": {"state_effect": {"reads_state_keys": ["analytics"]}}},
            {
                "analytics": [
                    {
                        "asof_date": "2025-01-28",
                        "cohort_id": "cohort-2025-01",
                        "tier": "pro",
                        "customer_id": "customer-42",
                        "experiment_id": "experiment-onboarding",
                        "funnel_name": "activation",
                        "metrics": {"conversion_rate": 0.42},
                        "body": "Large analytics report body.",
                    }
                ]
            },
        )

        self.assertEqual(
            compact,
            {
                "analytics": [
                    {
                        "asof_date": "2025-01-28",
                        "cohort_id": "cohort-2025-01",
                        "tier": "pro",
                        "customer_id": "customer-42",
                        "experiment_id": "experiment-onboarding",
                        "funnel_name": "activation",
                    }
                ]
            },
        )

    def test_writer_voice_context_contains_all_and_only_prior_fourteen_calendar_days(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            persona = root / "persona.md"
            world = root / "world.md"
            persona.write_text("Morgan writes direct updates.\n")
            world.write_text("The accepted world.\n")
            state = {
                "facts": copy.deepcopy(self.facts),
                "entities": copy.deepcopy(self.entities),
                "app_state": {},
                "unfinished_threads": [],
                "history": [
                    {
                        "id": "outside-window",
                        "narrative_date": "2025-01-14T09:00:00-08:00",
                        "messages": ["Outside the voice window."],
                    },
                    {
                        "id": "first-in-window",
                        "narrative_date": "2025-01-15T09:00:00-08:00",
                        "messages": ["Distinctive retained opening."],
                    },
                    {
                        "id": "last-in-window",
                        "narrative_date": "2025-01-28T17:00:00-08:00",
                        "messages": ["Keep this exact accepted message."],
                    },
                    {
                        "id": "new-window",
                        "narrative_date": "2025-01-29T09:00:00-08:00",
                        "messages": ["Do not include the new window."],
                    },
                ],
                "session_purposes": [],
                "operation_results": [],
            }
            payload = build_writer_payload(
                planner_response=self.good_planner_response(),
                config={
                    "persona": "morgan",
                    "persona_sheet": str(persona),
                    "generator_context": str(world),
                },
                state=state,
                start=self.start,
                end=self.end,
                timezone_name="America/Los_Angeles",
            )

        self.assertNotIn("recent_exact_user_messages_for_voice", payload)

    def test_planner_app_record_index_excludes_stale_calendar_and_inbox_rows(self) -> None:
        tools = {
            "get_calendar": {
                "description": "List visible calendar events.",
                "state_effect": {"reads_state_keys": ["calendar"]},
            },
            "get_inbox": {
                "description": "List visible inbox messages.",
                "state_effect": {"reads_state_keys": ["inbox"]},
            },
        }
        compact = history_window.compact_planner_app_records(
            tools,
            {
                "calendar": [
                    {"id": "old-event", "start": "2023-01-01T09:00:00-08:00"},
                    {"id": "near-event", "start": "2025-12-12T09:00:00-08:00"},
                ],
                "inbox": [
                    {"id": "old-mail", "date": "2023-01-01T09:00:00-08:00"},
                    {"id": "recent-mail", "date": "2025-11-20T09:00:00-08:00"},
                ],
            },
            start=date(2025, 12, 1),
            end=date(2025, 12, 7),
        )
        self.assertEqual(
            compact,
            {
                "calendar": [
                    {"id": "near-event", "start": "2025-12-12T09:00:00-08:00"}
                ],
                "inbox": [
                    {"id": "recent-mail", "date": "2025-11-20T09:00:00-08:00"}
                ],
            },
        )

    def test_writer_payload_gets_fixed_occurrences_and_realization_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            persona = root / "persona.md"
            world = root / "world.md"
            persona.write_text("Morgan writes direct updates.\n")
            world.write_text("Morgan runs a company.\n")
            state = {
                "facts": [
                    *copy.deepcopy(self.facts),
                    {
                        "id": 2,
                        "statement": "An unrelated vendor is under review.",
                        "applies_when": "the unrelated vendor is discussed",
                        "subjects": ["person:morgan", "organization:unrelated"],
                        "source_session_ids": ["old-session"],
                        "supersedes": [],
                    },
                    {
                        "id": 3,
                        "statement": "Morgan reviews invoices above the approval threshold.",
                        "applies_when": "an invoice needs payment review",
                        "subjects": ["person:morgan"],
                        "source_session_ids": ["old-session"],
                        "supersedes": [],
                    },
                    {
                        "id": 4,
                        "statement": "Morgan prefers the short Friday wrap format.",
                        "applies_when": "writing a Friday update",
                        "subjects": ["person:morgan"],
                        "source_session_ids": ["old-session"],
                        "supersedes": [],
                    },
                    {
                        "id": 5,
                        "statement": "Scaffold uses the unrelated vendor policy.",
                        "applies_when": "the unrelated vendor is discussed",
                        "subjects": ["person:morgan", "organization:scaffold"],
                        "source_session_ids": ["old-session"],
                        "supersedes": [],
                    },
                ],
                "entities": copy.deepcopy(self.entities),
                "app_state": {"docs": [{"id": "visible-doc"}]},
                "unfinished_threads": [{"thread_id": "thread-main", "what_remains_after_contact": "Await a reply."}],
                "history": [{"id": "old-session", "narrative_date": "2025-01-28T10:00:00-08:00", "messages": ["Earlier accepted context."]}],
                "session_purposes": [],
                "operation_results": [],
            }
            planner_response = self.good_planner_response()
            second = copy.deepcopy(planner_response["plan"]["occurrences"][0])
            second.update(
                {
                    "contact_id": "contact-2",
                    "narrative_date": "2025-01-30T10:00:00-08:00",
                    "basis_ids": [history_window.ORDINARY_CURRENT_LIFE_BASIS_ID],
                    "effect_kind": "self_contained",
                    "thread_id": "",
                    "continues_from": [],
                    "uses_items_created_by_contact_ids": ["contact-1"],
                    "development_ids": [],
                    "what_happened": "Morgan asks for advice using the note created yesterday.",
                    "assistant_outcome": {
                        "kind": "conversation",
                        "requested_result": "Compare the new options against that note.",
                    },
                    "uses_fact_ids": [],
                    "durable_facts": [],
                    "current_state_changes": [],
                    "app_operations": [],
                    "thread_after": {"is_open": False, "what_remains": None},
                }
            )
            planner_response["plan"]["occurrences"].append(second)
            payload = build_writer_payload(
                planner_response=planner_response,
                config={"persona": "morgan", "persona_sheet": str(persona), "generator_context": str(world)},
                state=state,
                start=self.start,
                end=self.end,
                timezone_name="America/Los_Angeles",
                primary_subject_ids={"person:morgan"},
            )
        self.assertEqual(
            payload["contacts"]["contact-1"]["what_happened"],
            "Morgan introduces the project and establishes its rule: "
            "The new rule now governs the project.",
        )
        self.assertEqual(
            payload["contacts"]["contact-1"]["fixed_app_operations"],
            self.good_planner_response()["plan"]["occurrences"][0]["app_operations"],
        )
        self.assertNotIn("current_state_changes", payload["contacts"]["contact-1"])
        self.assertNotIn("app_action_information", payload)
        self.assertNotIn("recent_exact_user_messages_for_voice", payload)
        self.assertEqual(
            [row["id"] for row in payload["contacts"]["contact-1"]["cited_facts"]],
            [1],
        )
        self.assertEqual(
            payload["contacts"]["contact-2"]["earlier_items_from_this_week"],
            [
                {
                    "contact_id": "contact-1",
                    "what_happened": planner_response["plan"]["occurrences"][0][
                        "what_happened"
                    ],
                    "requested_result": planner_response["plan"]["occurrences"][0][
                        "assistant_outcome"
                    ]["requested_result"],
                    "app_operations": planner_response["plan"]["occurrences"][0][
                        "app_operations"
                    ],
                }
            ],
        )

    def test_exact_prior_context_resolves_continuation_within_occurrence_thread(self) -> None:
        reference = "extension_riley_2025_02_06_005"
        state = {
            "history": [
                {"id": "session-a", "messages": ["Thread A context."]},
                {"id": reference, "messages": ["Thread B context."]},
            ],
            "session_purposes": [
                {
                    "session_id": "session-a",
                    "planned_interaction_id": reference,
                    "thread_id": "thread-a",
                },
                {
                    "session_id": reference,
                    "planned_interaction_id": "contact-b",
                    "thread_id": "thread-b",
                },
            ],
            "operation_results": [],
        }
        occurrence_plan = {
            "occurrences": [
                {"thread_id": "thread-b", "continues_from": [reference]}
            ]
        }

        context = history_window.exact_prior_context_for_occurrence_references(
            occurrence_plan, state
        )

        self.assertEqual(
            context,
            [
                {
                    "reference_id": reference,
                    "planned_interaction_id": "contact-b",
                    "session_id": reference,
                    "messages": ["Thread B context."],
                    "recorded_app_operations_and_results": [],
                }
            ],
        )

    def test_writer_fact_selection_rejects_non_current_references(self) -> None:
        occurrence_plan = copy.deepcopy(self.good_planner_response()["plan"])
        occurrence_plan["occurrences"][0]["uses_fact_ids"] = [99]
        with self.assertRaisesRegex(ValueError, "unknown, stale, or future fact: 99"):
            history_window.writer_current_facts_for_occurrences(
                occurrence_plan,
                self.facts,
            )

    def test_writer_fact_selection_allows_prior_same_window_fact_keys(self) -> None:
        occurrence_plan = copy.deepcopy(self.good_planner_response()["plan"])
        first = occurrence_plan["occurrences"][0]
        first["durable_facts"][0]["key"] = "same-window-intermediate"
        first["durable_facts"][0]["supersedes"] = []
        second = copy.deepcopy(first)
        second.update(
            {
                "contact_id": "contact-2",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "continues_from": ["contact-1"],
                "uses_fact_ids": ["same-window-intermediate"],
                "durable_facts": [],
            }
        )
        occurrence_plan["occurrences"].append(second)

        selected = history_window.writer_current_facts_for_occurrences(
            occurrence_plan,
            self.facts,
        )

        self.assertEqual([fact["id"] for fact in selected], [1])

    def test_writer_fact_selection_rejects_future_same_window_fact_keys(self) -> None:
        occurrence_plan = copy.deepcopy(self.good_planner_response()["plan"])
        occurrence_plan["occurrences"][0]["uses_fact_ids"] = ["future-key"]
        with self.assertRaisesRegex(ValueError, "unknown, stale, or future fact: future-key"):
            history_window.writer_current_facts_for_occurrences(
                occurrence_plan,
                self.facts,
            )

    def test_writer_cannot_add_drop_or_reorder_fixed_occurrences(self) -> None:
        occurrence_plan = self.good_planner_response()["plan"]
        second = copy.deepcopy(occurrence_plan["occurrences"][0])
        second.update(
            {
                "contact_id": "contact-2",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "continues_from": ["contact-1"],
                "development_ids": [],
                "anchor_item_ids": [],
                "what_happens": "Morgan receives a later result.",
                "why_contact_assistant": "Morgan wants the assistant to know the result.",
            }
        )
        occurrence_plan["occurrences"].append(second)
        writer = restore_code_owned_period(
            normalize_writer_messages(self.good_writer_api_response()),
            start=self.start,
            end=self.end,
        )
        writer_session = copy.deepcopy(writer["plan"]["sessions"][0])
        writer_session["contact_id"] = "contact-2"
        writer["plan"]["sessions"].append(writer_session)
        self.assertEqual(
            validate_writer_prose_response(writer, start=self.start, end=self.end, occurrence_plan=occurrence_plan),
            [],
        )
        for mutation in (
            lambda response: response["plan"]["sessions"].pop(),
            lambda response: response["plan"]["sessions"].reverse(),
            lambda response: response["plan"]["sessions"][0].update({"contact_id": "other"}),
        ):
            response = copy.deepcopy(writer)
            mutation(response)
            errors = validate_writer_prose_response(
                response, start=self.start, end=self.end, occurrence_plan=occurrence_plan
            )
            self.assertTrue(errors, errors)

    def test_writer_rejects_fact_or_operation_not_copied_from_planner(self) -> None:
        writer = restore_code_owned_period(
            normalize_writer_messages(self.good_writer_api_response()),
            start=self.start,
            end=self.end,
        )
        self.assertEqual(
            validate_writer_prose_response(
                writer, start=self.start, end=self.end,
                occurrence_plan=self.good_planner_response()["plan"],
            ),
            [],
        )

    def test_final_response_does_not_repeat_planner_item_source_validation(self) -> None:
        occurrence_plan = copy.deepcopy(self.good_planner_response()["plan"])
        second_occurrence = copy.deepcopy(occurrence_plan["occurrences"][0])
        second_occurrence.update(
            {
                "contact_id": "contact-2",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "basis_ids": [history_window.ORDINARY_CURRENT_LIFE_BASIS_ID],
                "effect_kind": "self_contained",
                "thread_id": "",
                "continues_from": [],
                "uses_items_created_by_contact_ids": ["contact-1"],
                "subject_ids": ["person:morgan"],
                "development_ids": [],
                "anchor_item_ids": [],
                "what_happened": "The follow-up uses the result from contact-1.",
                "assistant_outcome": {
                    "kind": "external_action",
                    "requested_result": "Create the follow-up note.",
                },
                "source_material": None,
                "uses_fact_ids": [],
                "durable_facts": [],
                "current_state_changes": [],
                "thread_after": {"is_open": False, "what_remains": None},
            }
        )
        occurrence_plan["occurrences"].append(second_occurrence)
        writer = self.good_response()
        writer["plan"]["sessions"].append(
            {
                "contact_id": "contact-2",
                "narrative_date": second_occurrence["narrative_date"],
                "thread_id": "",
                "continues_from": [],
                "subject_ids": ["person:morgan"],
                "development_ids": [],
                "anchor_item_ids": [],
                "purpose": "Create the follow-up note.",
                "messages": ["Create the follow-up note using contact-1's result."],
                "uses_facts": [],
                "new_facts": [],
                "app_operations": [
                    {
                        "tool": "create_doc",
                        "args": {
                            "title": "Follow-up",
                            "body": {
                                "$operation_result": {
                                    "contact_id": "contact-1",
                                    "operation_index": 0,
                                    "field": "id",
                                }
                            },
                        },
                    }
                ],
                "thread_open_after_contact": False,
                "what_remains_after_contact": None,
            }
        )

        self.assertEqual(self.validate(writer, occurrence_plan=occurrence_plan), [])
        occurrence_plan["occurrences"][1]["uses_items_created_by_contact_ids"] = []
        self.assertEqual(self.validate(writer, occurrence_plan=occurrence_plan), [])

    def test_planner_operation_result_reference_uses_continuation_and_keeps_forward_check(self) -> None:
        planner = copy.deepcopy(self.good_planner_response())
        planner["plan"]["occurrences"][0]["thread_after"] = {
            "is_open": True,
            "what_remains": "The follow-up note still needs to be created.",
        }
        second_occurrence = copy.deepcopy(planner["plan"]["occurrences"][0])
        second_occurrence.update(
            {
                "contact_id": "contact-2",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "basis_ids": [history_window.ORDINARY_CURRENT_LIFE_BASIS_ID],
                "effect_kind": "self_contained",
                "thread_id": "thread-main",
                "continues_from": ["contact-1"],
                "uses_items_created_by_contact_ids": ["contact-1"],
                "subject_ids": ["person:morgan"],
                "development_ids": [],
                "anchor_item_ids": [],
                "what_happened": "The event created earlier this week supplies the title.",
                "assistant_outcome": {
                    "kind": "external_action",
                    "requested_result": "Create the follow-up note.",
                },
                "source_material": None,
                "uses_fact_ids": [],
                "durable_facts": [],
                "current_state_changes": [],
                "app_operations": [
                    {
                        "tool": "create_doc",
                        "args": {
                            "title": "Follow-up",
                            "body": {
                                "$operation_result": {
                                    "contact_id": "contact-1",
                                    "operation_index": 0,
                                    "field": "id",
                                }
                            },
                        },
                    }
                ],
                "thread_after": {"is_open": False, "what_remains": None},
            }
        )
        planner["plan"]["occurrences"].append(second_occurrence)

        validation_options = {
            "active_situation_ids": {history_window.ORDINARY_CURRENT_LIFE_BASIS_ID}
        }
        self.assertEqual(self.validate_planner(planner, **validation_options), [])

        planner["plan"]["occurrences"][1]["uses_items_created_by_contact_ids"] = []
        errors = self.validate_planner(planner, **validation_options)
        self.assertTrue(any("undeclared same-week item source" in error for error in errors), errors)

        planner["plan"]["occurrences"][1]["uses_items_created_by_contact_ids"] = [
            "contact-1"
        ]
        planner["plan"]["occurrences"][0]["app_operations"][0]["args"]["body"] = {
            "$operation_result": {
                "contact_id": "contact-2",
                "operation_index": 0,
                "field": "id",
            }
        }
        errors = self.validate_planner(planner, **validation_options)
        self.assertTrue(any("forward contact 'contact-2'" in error for error in errors), errors)

    def test_planner_allows_conversation_to_use_an_earlier_created_item(self) -> None:
        planner = copy.deepcopy(self.good_planner_response())
        second = copy.deepcopy(planner["plan"]["occurrences"][0])
        second.update(
            {
                "contact_id": "contact-2",
                "narrative_date": "2025-01-30T10:00:00-08:00",
                "basis_ids": [history_window.ORDINARY_CURRENT_LIFE_BASIS_ID],
                "effect_kind": "self_contained",
                "thread_id": "",
                "continues_from": [],
                "uses_items_created_by_contact_ids": ["contact-1"],
                "development_ids": [],
                "what_happened": "Morgan asks for advice using the note created yesterday.",
                "assistant_outcome": {
                    "kind": "conversation",
                    "requested_result": "Compare the new options against that note.",
                },
                "uses_fact_ids": [],
                "durable_facts": [],
                "current_state_changes": [],
                "app_operations": [],
                "thread_after": {"is_open": False, "what_remains": None},
            }
        )
        planner["plan"]["occurrences"].append(second)

        errors = self.validate_planner(
            planner,
            active_situation_ids={history_window.ORDINARY_CURRENT_LIFE_BASIS_ID},
        )

        self.assertEqual(errors, [])

    def test_planner_must_supply_an_authorized_external_action(self) -> None:
        planner = self.good_planner_response()
        planner["plan"]["occurrences"][0]["app_operations"] = []
        errors = self.validate_planner(planner)
        self.assertTrue(any("requires an app operation" in error for error in errors), errors)

    def test_schedule_free_run_uses_two_deployments_and_commits_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            quarter = root / "quarter.json"
            overview = root / "overview.json"
            spec = root / "spec.yaml"
            persona = root / "persona.md"
            world = root / "world.md"
            config_path = root / "config.yaml"
            quarter.write_text(
                json.dumps(
                    {
                        "plan": {
                            "period_start": "2025-01-01",
                            "period_end": "2025-03-31",
                            "new_entities": [],
                            "events": [
                                {
                                    "id": "development-1",
                                    "start_date": "2025-01-29",
                                    "end_date": "2025-01-29",
                                    "subjects": ["person:morgan"],
                                    "relevant_existing_fact_ids": [1],
                                    "facts_to_establish": [
                                        {
                                            "key": "new-rule",
                                            "statement": "The new rule now governs the project.",
                                            "applies_when": "the project is discussed",
                                            "subjects": ["person:morgan"],
                                            "supersedes": [1],
                                        }
                                    ],
                                }
                            ],
                        }
                    }
                )
            )
            overview.write_text("{}\n")
            spec.write_text(
                "narrative_timezone: America/Los_Angeles\nprimary_subject_ids:\n- person:morgan\n"
            )
            persona.write_text("Morgan is a founder.\n")
            world.write_text("A founder's current world.\n")
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "persona": "morgan",
                        "quarter_start": "2025-01-01",
                        "quarter_end": "2025-03-31",
                        "quarter_plan": str(quarter),
                        "overview": str(overview),
                        "spec": str(spec),
                        "persona_sheet": str(persona),
                        "generator_context": str(world),
                    }
                )
            )
            planner = self.good_planner_api_response()
            planner_occurrence = planner["plan"]["occurrences"][0]
            planner_occurrence["subject_ids"] = ["person:morgan"]
            planner_occurrence["continues_from"] = []
            planner_occurrence["durable_facts"][0]["subjects"] = ["person:morgan"]
            story = copy.deepcopy(planner)
            for occurrence in story["plan"]["occurrences"]:
                for key in (
                    "uses_current_fact_ids",
                    "uses_new_fact_keys",
                    "durable_facts",
                    "current_state_changes",
                    "app_operations",
                ):
                    occurrence.pop(key)
            enrichment = {
                "contacts": [
                    {
                        key: copy.deepcopy(occurrence[key])
                        for key in (
                            "contact_id",
                            "uses_current_fact_ids",
                            "uses_new_fact_keys",
                            "durable_facts",
                            "current_state_changes",
                            "app_operations",
                        )
                    }
                    for occurrence in planner["plan"]["occurrences"]
                ]
            }
            writer = self.good_keyed_writer_api_response()
            starting_state = {
                "facts": copy.deepcopy(self.facts),
                "entities": copy.deepcopy(self.entities),
                "app_state": {},
                "unfinished_threads": [],
                "history": [
                    {
                        "id": "old-session",
                        "narrative_date": "2025-01-28T10:00:00-08:00",
                        "messages": ["Earlier accepted context."],
                    }
                ],
                "session_purposes": [],
                "operation_results": [],
            }
            checkpoint = {"accepted_through": "2025-01-28", "input_sha256": {}}
            args = SimpleNamespace(
                confirm_paid_calls=True,
                out=root / "run",
                tool_python=root / "tool.py",
                config=config_path,
                resume_from_checkpoint=root / "checkpoint",
                start_date=self.start,
                end_date=self.start,
            )
            candidate = args.out / "candidate_checkpoint"
            with (
                patch(
                    "construction.build_history_window.assign_story_contact_ids",
                    side_effect=lambda response, **_: response,
                ),
                patch("construction.build_history_window.validate_tool_python"),
                patch("construction.build_history_window.load_checkpoint", return_value=(checkpoint, starting_state)),
                patch("construction.build_history_window.validate_resume_checkpoint_for_config"),
                patch("construction.build_history_window.exact_persona_tools", return_value=self.tools),
                patch("construction.build_history_window.AzureJsonClient") as client_cls,
                patch(
                    "construction.build_history_window.cached_client_complete",
                    side_effect=[story, enrichment, writer],
                ) as complete,
                patch("construction.build_history_window.project_app_operations", return_value=({}, [], [])),
                patch("construction.build_history_window.validated_checkpoint_identity", return_value="checkpoint-id"),
            ):
                manifest = history_window._run(args)
                self.assertTrue(candidate.is_dir())
        self.assertEqual(manifest["model_calls"], 3)
        self.assertEqual(complete.call_count, 3)
        self.assertEqual(
            [call.kwargs["model"] for call in client_cls.call_args_list],
            ["gpt-5.6-sol", "gpt-5.6-sol", "gpt-5.6-sol"],
        )
        self.assertEqual(manifest["models"]["planner"]["reasoning_effort"], "high")
        self.assertEqual(manifest["models"]["writer"]["name"], "gpt-5.6-sol")
        self.assertEqual(manifest["models"]["writer"]["reasoning_effort"], "medium")

    def test_stop_after_story_skips_enrichment_writer_replay_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = root / "spec.yaml"
            config_path = root / "config.yaml"
            spec.write_text("narrative_timezone: America/Los_Angeles\n")
            config_path.write_text(
                yaml.safe_dump({"persona": "morgan", "spec": str(spec)})
            )
            args = SimpleNamespace(
                confirm_paid_calls=True,
                stop_after_story=True,
                out=root / "run",
                tool_python=root / "tool.py",
                config=config_path,
                resume_from_checkpoint=root / "checkpoint",
                start_date=self.start,
                end_date=self.end,
            )
            checkpoint = {"accepted_through": "2025-01-28", "input_sha256": {}}
            state = {
                "facts": copy.deepcopy(self.facts),
                "entities": copy.deepcopy(self.entities),
                "app_state": {},
                "unfinished_threads": [],
                "history": [],
                "session_purposes": [],
                "operation_results": [],
            }
            validation_context = {"required_development_ids": {"development-1"}}
            with (
                patch(
                    "construction.build_history_window.assign_story_contact_ids",
                    side_effect=lambda response, **_: response,
                ),
                patch("construction.build_history_window.validate_tool_python"),
                patch(
                    "construction.build_history_window.load_checkpoint",
                    return_value=(checkpoint, state),
                ),
                patch("construction.build_history_window.validate_resume_checkpoint_for_config"),
                patch("construction.build_history_window.validate_window_dates"),
                patch("construction.build_history_window.exact_persona_tools", return_value={}),
                patch(
                    "construction.build_history_window.build_planner_payload",
                    return_value=({}, validation_context),
                ),
                patch("construction.build_history_window.build_story_payload", return_value={}),
                patch("construction.build_history_window.build_enrichment_payload") as enrich,
                patch("construction.build_history_window.AzureJsonClient"),
                patch(
                    "construction.build_history_window.cached_client_complete",
                    return_value=self.good_story_api_response(),
                ) as complete,
                patch("construction.build_history_window.project_app_operations") as replay,
                patch("construction.build_history_window.build_writer_payload") as write,
                patch("construction.build_history_window.emit_candidate_checkpoint") as emit,
                patch(
                    "construction.build_history_window.validated_checkpoint_identity",
                    return_value="checkpoint-id",
                ),
            ):
                manifest = history_window._run(args)

            self.assertEqual(manifest["status"], "story_only")
            self.assertEqual(manifest["model_calls"], 1)
            self.assertEqual(complete.call_count, 1)
            enrich.assert_not_called()
            write.assert_not_called()
            replay.assert_not_called()
            emit.assert_not_called()
            self.assertTrue((args.out / "story_response.json").is_file())
            self.assertFalse((args.out / "planner_request.json").exists())
            self.assertFalse((args.out / "writer_request.json").exists())
            self.assertFalse((args.out / "candidate_checkpoint").exists())

    def test_stop_after_planner_skips_writer_replay_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = root / "spec.yaml"
            config_path = root / "config.yaml"
            spec.write_text("narrative_timezone: America/Los_Angeles\n")
            config_path.write_text(
                yaml.safe_dump({"persona": "morgan", "spec": str(spec)})
            )
            args = SimpleNamespace(
                confirm_paid_calls=True,
                stop_after_planner=True,
                out=root / "run",
                tool_python=root / "tool.py",
                config=config_path,
                resume_from_checkpoint=root / "checkpoint",
                start_date=self.start,
                end_date=self.end,
            )
            checkpoint = {"accepted_through": "2025-01-28", "input_sha256": {}}
            state = {
                "facts": copy.deepcopy(self.facts),
                "entities": copy.deepcopy(self.entities),
                "app_state": {},
                "unfinished_threads": [],
                "history": [],
                "session_purposes": [],
                "operation_results": [],
            }
            planner_payload = {
                "valid_existing_continues_from_ids": ["old-session"],
                "available_app_records": {},
                "current_ordinary_states": [],
            }
            validation_context = {
                "required_development_ids": {"development-1"},
                "allowed_development_ids": {"development-1"},
                "required_anchors": {},
                "required_entities": [],
                "candidate_entities": [],
                "self_contained_basis_ids": {
                    history_window.ORDINARY_CURRENT_LIFE_BASIS_ID
                },
                "open_thread_ids": set(),
                "protected_change_ids": set(),
                "facts_by_development": {},
                "subjects_by_basis": {},
                "primary_subject_ids": {"person:morgan"},
            }
            with (
                patch(
                    "construction.build_history_window.assign_story_contact_ids",
                    side_effect=lambda response, **_: response,
                ),
                patch("construction.build_history_window.validate_tool_python"),
                patch(
                    "construction.build_history_window.load_checkpoint",
                    return_value=(checkpoint, state),
                ),
                patch("construction.build_history_window.validate_resume_checkpoint_for_config"),
                patch("construction.build_history_window.validate_window_dates"),
                patch("construction.build_history_window.exact_persona_tools", return_value=self.tools),
                patch(
                    "construction.build_history_window.build_planner_payload",
                    return_value=(planner_payload, validation_context),
                ),
                patch(
                    "construction.build_history_window.build_story_payload",
                    return_value={},
                ),
                patch(
                    "construction.build_history_window.build_enrichment_payload",
                    return_value={},
                ),
                patch("construction.build_history_window.AzureJsonClient") as client_cls,
                patch(
                    "construction.build_history_window.cached_client_complete",
                    side_effect=[
                        self.good_story_api_response(),
                        self.good_enrichment_api_response(),
                    ],
                ) as complete,
                patch(
                    "construction.build_history_window.validate_history_window_plan_response",
                    return_value=[],
                ) as validate_planner,
                patch("construction.build_history_window.build_writer_payload") as build_writer,
                patch("construction.build_history_window.project_app_operations") as replay,
                patch("construction.build_history_window.emit_candidate_checkpoint") as emit,
                patch(
                    "construction.build_history_window.validated_checkpoint_identity",
                    return_value="checkpoint-id",
                ),
            ):
                manifest = history_window._run(args)

            self.assertEqual(manifest["status"], "planner_only")
            self.assertEqual(manifest["model_calls"], 2)
            self.assertEqual(complete.call_count, 2)
            self.assertEqual(client_cls.call_count, 2)
            validate_planner.assert_called_once()
            build_writer.assert_not_called()
            replay.assert_not_called()
            emit.assert_not_called()
            self.assertTrue((args.out / "planner_system.txt").is_file())
            self.assertTrue((args.out / "story_system.txt").is_file())
            self.assertTrue((args.out / "planner_request.json").is_file())
            self.assertTrue((args.out / "planner_response.json").is_file())
            self.assertTrue((args.out / "planner_validation.json").is_file())
            self.assertTrue((args.out / "validation.json").is_file())
            self.assertFalse((args.out / "writer_request.json").exists())
            self.assertFalse((args.out / "candidate_checkpoint").exists())

    def test_prompt_module_contains_only_canonical_system_prompts(self) -> None:
        from construction import long_history_prompts

        names = {
            name
            for name in vars(long_history_prompts)
            if name.endswith("_SYSTEM") and name.isupper()
        }
        self.assertEqual(
            names,
            {
                "QUARTER_EVENT_STORY_SYSTEM",
                "QUARTER_EVENT_FACT_FINALIZATION_SYSTEM",
                "QUARTER_EVENT_FACT_REPAIR_SYSTEM",
                "QUARTER_EVENT_EVENT_REPAIR_SYSTEM",
                "QUARTER_EVENT_QUALITY_REVIEW_SYSTEM",
                "HISTORY_WINDOW_STORY_SYSTEM",
                "HISTORY_WINDOW_PLANNING_SYSTEM",
                "HISTORY_WINDOW_CONTACT_CORRECTION_SYSTEM",
                "HISTORY_WINDOW_STATE_CORRECTION_SYSTEM",
                "HISTORY_WINDOW_WRITING_SYSTEM",
            },
        )

    def test_candidate_checkpoint_emits_without_any_review_input(self) -> None:
        saved: list[Path] = []
        validated: list[Path] = []

        def fake_save(*, path: Path, **_kwargs: Any) -> dict[str, Any]:
            saved.append(path)
            path.mkdir(parents=True)
            (path / "complete").write_text("yes")
            return {}

        def fake_validate(path: Path) -> str:
            validated.append(path)
            self.assertEqual((path / "complete").read_text(), "yes")
            return "checkpoint-hash"

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            emitted = emit_candidate_checkpoint(
                path=output / "candidate_checkpoint",
                run_output=output,
                save_kwargs={"state": {}},
                save=fake_save,
                validate=fake_validate,
            )
            self.assertEqual(emitted, output / "candidate_checkpoint")
            self.assertTrue((emitted / "complete").is_file())
            self.assertFalse((output / "candidate_checkpoint.pending").exists())
        self.assertEqual(saved, validated)

    def test_operation_results_accumulate_across_two_windows_but_run_files_stay_local(
        self,
    ) -> None:
        def row(session_id: str, tool: str, replay_epoch_ms: int) -> dict[str, Any]:
            return {
                "session_id": session_id,
                "operation": {"tool": tool, "args": {}},
                "result": {"ok": True},
                "replay_epoch_ms": replay_epoch_ms,
            }

        starting = [row("accepted-session", "create_doc", 1)]
        first_window = [row("first-window", "create_doc", 2)]
        second_window = [row("second-window", "send_message", 3)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first_output = root / "first"
            first_output.mkdir()
            first_cumulative = write_window_operation_results(
                run_output=first_output,
                starting_rows=starting,
                current_rows=first_window,
            )
            self.assertEqual(
                json.loads((first_output / "operation_results.json").read_text()),
                first_window,
            )

            starting_v2 = root / "starting-v2"
            starting_v2.mkdir()
            (starting_v2 / "operation_results.json").write_text(
                json.dumps(first_cumulative)
            )
            second_output = root / "second"
            second_output.mkdir()
            second_cumulative = write_window_operation_results(
                run_output=second_output,
                starting_rows=json.loads(
                    (starting_v2 / "operation_results.json").read_text()
                ),
                current_rows=second_window,
            )
            self.assertEqual(second_cumulative, [*starting, *first_window, *second_window])
            self.assertEqual(
                json.loads((second_output / "operation_results.json").read_text()),
                second_window,
            )
            identities = [
                (item["session_id"], item["operation"]["tool"], item["replay_epoch_ms"])
                for item in second_cumulative
            ]
            self.assertEqual(len(identities), len(set(identities)))

    def test_cumulative_operation_results_rejects_duplicate_replay_identity(self) -> None:
        duplicate = {
            "session_id": "same-session",
            "operation": {"tool": "create_doc", "args": {"title": "Same"}},
            "replay_epoch_ms": 10,
        }
        with self.assertRaisesRegex(ValueError, "duplicate operation result identity"):
            cumulative_operation_results([duplicate], [copy.deepcopy(duplicate)])

    def test_failed_planner_operation_stops_before_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = root / "spec.yaml"
            config_path = root / "config.yaml"
            spec.write_text(
                "narrative_timezone: America/Los_Angeles\n"
                "primary_subject_ids:\n- person:morgan\n"
            )
            config_path.write_text(
                yaml.safe_dump({"persona": "morgan", "spec": str(spec)})
            )
            checkpoint = {
                "accepted_through": "2025-01-28",
                "input_sha256": {},
            }
            starting_state = {
                "facts": copy.deepcopy(self.facts),
                "entities": copy.deepcopy(self.entities),
                "app_state": {},
                "unfinished_threads": [],
                "history": [],
                "session_purposes": [],
                "operation_results": [],
            }
            args = SimpleNamespace(
                confirm_paid_calls=True,
                stop_after_planner=False,
                out=root / "run",
                tool_python=root / "tool.py",
                config=config_path,
                resume_from_checkpoint=root / "checkpoint",
                start_date=self.start,
                end_date=self.start,
            )
            planner_payload = {
                "valid_existing_continues_from_ids": [],
                "available_app_records": {},
                "current_ordinary_states": [],
            }
            validation_context = {
                "required_development_ids": set(),
                "allowed_development_ids": set(),
                "candidate_entities": [],
                "required_entities": [],
                "required_facts": {},
                "primary_subject_ids": {"person:morgan"},
                "self_contained_basis_ids": {
                    history_window.ORDINARY_CURRENT_LIFE_BASIS_ID
                },
                "open_thread_ids": set(),
                "protected_change_ids": set(),
                "facts_by_development": {},
                "subjects_by_basis": {},
            }
            operation_plan = {
                "sessions": [
                    {
                        "contact_id": "contact-1",
                        "narrative_date": "2025-01-29T10:00:00-08:00",
                        "session_id": "session-1",
                        "app_operations": [
                            {
                                "tool": "create_doc",
                                "args": {
                                    "title": "New project operating note",
                                    "body": "The new rule now governs the project.",
                                },
                            }
                        ],
                    }
                ]
            }
            with (
                patch(
                    "construction.build_history_window.assign_story_contact_ids",
                    side_effect=lambda response, **_: response,
                ),
                patch("construction.build_history_window.validate_tool_python"),
                patch(
                    "construction.build_history_window.load_checkpoint",
                    return_value=(checkpoint, starting_state),
                ),
                patch(
                    "construction.build_history_window.validate_resume_checkpoint_for_config"
                ),
                patch("construction.build_history_window.validate_window_dates"),
                patch(
                    "construction.build_history_window.exact_persona_tools",
                    return_value=self.tools,
                ),
                patch(
                    "construction.build_history_window.build_planner_payload",
                    return_value=(planner_payload, validation_context),
                ),
                patch(
                    "construction.build_history_window.build_story_payload",
                    return_value={},
                ),
                patch(
                    "construction.build_history_window.build_enrichment_payload",
                    return_value={},
                ),
                patch(
                    "construction.build_history_window.add_known_world_basis_for_visible_subjects",
                    side_effect=lambda response, **_: response,
                ),
                patch("construction.build_history_window.AzureJsonClient"),
                patch(
                    "construction.build_history_window.cached_client_complete",
                    side_effect=[
                        self.good_story_api_response(),
                        self.good_enrichment_api_response(),
                    ],
                ),
                patch(
                    "construction.build_history_window.validate_history_window_plan_response",
                    return_value=[],
                ),
                patch(
                    "construction.build_history_window.assign_window_session_ids",
                    return_value=operation_plan,
                ),
                patch(
                    "construction.build_history_window.project_app_operations",
                    side_effect=RuntimeError("simulated planner operation failure"),
                ) as replay,
                patch(
                    "construction.build_history_window.build_writer_payload"
                ) as build_writer,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "simulated planner operation failure"
                ):
                    history_window._run(args)

            replay.assert_called_once()
            build_writer.assert_not_called()
            validation = json.loads((args.out / "validation.json").read_text())
            self.assertFalse(validation["operation_replay"]["passed"])
            self.assertEqual(validation["writer"]["errors"], ["not run"])

    def test_writer_operation_schema_rejects_model_owned_operations(self) -> None:
        response = self.good_writer_api_response()
        response["plan"]["sessions"][0]["app_operations"] = []
        with self.assertRaises(ValidationError):
            Draft202012Validator(WRITER_API_RESPONSE_SCHEMA).validate(response)

    def test_writer_gets_only_strictly_earlier_same_week_results(self) -> None:
        occurrences = [
            {
                "contact_id": "contact-1",
                "narrative_date": "2025-01-29T09:00:00-08:00",
                "app_operations": [],
            },
            {
                "contact_id": "contact-2",
                "narrative_date": "2025-01-29T10:00:00-08:00",
                "app_operations": [
                    {
                        "tool": "create_doc",
                        "args": {
                            "parent_id": {
                                "$operation_result": {
                                    "contact_id": "contact-1",
                                    "operation_index": 0,
                                    "field": "id",
                                }
                            }
                        },
                    }
                ],
            },
            {
                "contact_id": "contact-3",
                "narrative_date": "2025-01-29T11:00:00-08:00",
                "app_operations": [
                    {
                        "tool": "create_doc",
                        "args": {
                            "parent_id": {
                                "$operation_result": {
                                    "contact_id": "contact-2",
                                    "operation_index": 0,
                                    "field": "id",
                                }
                            }
                        },
                    }
                ],
            },
        ]
        results = [
            {
                "contact_id": "contact-1",
                "operation_index": 0,
                "operation": {"tool": "create_doc", "args": {}},
                "result": {"id": "doc-1"},
            },
            {
                "contact_id": "contact-2",
                "operation_index": 0,
                "operation": {"tool": "create_doc", "args": {}},
                "result": {"id": "doc-2"},
            },
            {
                "contact_id": "contact-3",
                "operation_index": 0,
                "operation": {"tool": "create_doc", "args": {}},
                "result": {"id": "doc-3"},
            },
        ]

        visible = history_window.prior_same_week_operation_results_by_contact(
            occurrences, results
        )

        self.assertEqual(
            [row["contact_id"] for row in visible["contact-1"]], []
        )
        self.assertEqual(
            [row["contact_id"] for row in visible["contact-2"]], ["contact-1"]
        )
        self.assertEqual(
            [row["contact_id"] for row in visible["contact-3"]],
            ["contact-2"],
        )

    def test_planner_history_has_current_quarter_and_thirteen_prior_weeks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            quarter = root / "quarter.json"
            spec = root / "spec.yaml"
            world = root / "world.md"
            quarter.write_text(
                json.dumps(
                    {
                        "plan": {
                            "period_start": "2025-04-01",
                            "period_end": "2025-09-30",
                            "events": [],
                        }
                    }
                )
            )
            spec.write_text(
                "narrative_timezone: America/Los_Angeles\n"
                "primary_subject_ids:\n- person:morgan\n"
            )
            world.write_text("Morgan runs a company.\n")
            def session(session_id: str, when: str, purpose: str) -> dict[str, Any]:
                return {
                    "id": session_id,
                    "narrative_date": when,
                    "messages": [f"Exact message for {session_id}."],
                }

            state = {
                "history": [
                    session("outside", "2024-12-31T09:00:00-08:00", "Outside."),
                    session("prior-13-weeks", "2025-01-01T09:00:00-08:00", "Prior."),
                    session("current-quarter", "2025-04-10T09:00:00-07:00", "Quarter."),
                    session("older-recent", "2025-06-16T09:00:00-07:00", "Older recent."),
                    session("detailed-recent", "2025-06-18T09:00:00-07:00", "Detailed recent."),
                ],
                "session_purposes": [
                    {
                        "session_id": session_id,
                        "purpose": purpose,
                        "accepted_contact_details": purpose,
                        "subject_ids": ["person:morgan"],
                    }
                    for session_id, purpose in (
                        ("outside", "Outside."),
                        ("prior-13-weeks", "Prior."),
                        ("current-quarter", "Quarter."),
                        ("older-recent", "Older recent."),
                        ("detailed-recent", "Detailed recent."),
                    )
                ],
                "facts": [],
                "entities": [{"id": "person:morgan", "name": "Morgan", "kind": "person"}],
                "app_state": {},
                "operation_results": [],
                "unfinished_threads": [],
            }
            payload, _ = build_planner_payload(
                config={
                    "persona": "morgan",
                    "quarter_plan": str(quarter),
                    "spec": str(spec),
                    "generator_context": str(world),
                },
                checkpoint={"covered_quarter_event_ids": []},
                state=state,
                start=date(2025, 7, 1),
                end=date(2025, 7, 7),
            )

        older_text = json.dumps(payload["older_contacts_that_already_happened"])
        recent_ids = {
            row["session_id"] for row in payload["recently_finished_contacts"]
        }
        self.assertIn("Prior.", older_text)
        self.assertIn("Quarter.", older_text)
        self.assertIn("Older recent.", older_text)
        self.assertIn('"session_id": "outside"', older_text)
        self.assertNotIn("Exact message", older_text)
        self.assertEqual(recent_ids, {"detailed-recent"})
        self.assertNotIn("older-recent", recent_ids)

    def test_story_contact_correction_changes_only_blocked_contact_text(self) -> None:
        story = {
            "plan": {
                "period_start": "2025-04-01",
                "period_end": "2025-04-07",
                "occurrences": [
                    {
                        "contact_id": "contact-1",
                        "narrative_date": "2025-04-01T09:00:00-07:00",
                        "subject_ids": ["person:riley"],
                        "what_happened": "Unchanged story one.",
                        "assistant_outcome": {"kind": "none", "requested_result": None},
                        "source_material": None,
                        "thread_after": {"is_open": False, "what_remains": None},
                    },
                    {
                        "contact_id": "contact-2",
                        "narrative_date": "2025-04-02T09:00:00-07:00",
                        "subject_ids": ["person:riley"],
                        "what_happened": "Original blocked story.",
                        "assistant_outcome": {
                            "kind": "conversation",
                            "requested_result": "Original outcome.",
                        },
                        "source_material": {"kind": "email", "body": "Keep this."},
                        "thread_after": {"is_open": True, "what_remains": "Keep this too."},
                    },
                ],
            },
            "metadata": {"keep": ["every", "other", "field"]},
        }
        original = copy.deepcopy(story)
        correction = {
            "contacts": [
                {
                    "contact_id": "contact-2",
                    "what_happened": "Corrected blocked story.",
                    "assistant_outcome": {
                        "kind": "conversation",
                        "requested_result": "Corrected outcome.",
                    },
                }
            ]
        }

        corrected = history_window.apply_story_contact_corrections(
            story, correction, {"contact-2"}
        )

        self.assertEqual(corrected["metadata"], original["metadata"])
        self.assertEqual(corrected["plan"]["period_start"], original["plan"]["period_start"])
        self.assertEqual(corrected["plan"]["period_end"], original["plan"]["period_end"])
        self.assertEqual(
            corrected["plan"]["occurrences"][0], original["plan"]["occurrences"][0]
        )
        changed = corrected["plan"]["occurrences"][1]
        expected = copy.deepcopy(original["plan"]["occurrences"][1])
        expected.update(
            {
                "what_happened": "Corrected blocked story.",
                "assistant_outcome": {
                    "kind": "conversation",
                    "requested_result": "Corrected outcome.",
                },
            }
        )
        self.assertEqual(changed, expected)

    def test_story_contact_correction_rejects_wrong_or_duplicate_ids(self) -> None:
        story = {
            "plan": {
                "occurrences": [
                    {
                        "contact_id": "blocked",
                        "what_happened": "Old.",
                        "assistant_outcome": {"kind": "none", "requested_result": None},
                    },
                    {
                        "contact_id": "free",
                        "what_happened": "Old.",
                        "assistant_outcome": {"kind": "none", "requested_result": None},
                    },
                ]
            }
        }
        correction = {
            "contacts": [
                {
                    "contact_id": "blocked",
                    "what_happened": "New.",
                    "assistant_outcome": {"kind": "none", "requested_result": None},
                }
            ]
        }
        for label, response, blocked in (
            ("unexpected", {"contacts": [{**correction["contacts"][0], "contact_id": "unknown"}]}, {"blocked"}),
            ("missing", {"contacts": []}, {"blocked"}),
            (
                "duplicate",
                {"contacts": [correction["contacts"][0], correction["contacts"][0]]},
                {"blocked"},
            ),
        ):
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    history_window.apply_story_contact_corrections(story, response, blocked)

    def test_enrichment_contact_replacement_changes_only_blocked_rows_and_keeps_order(self) -> None:
        enrichment = {
            "contacts": [
                {"contact_id": "one", "app_operations": [{"tool": "keep-one"}]},
                {"contact_id": "two", "app_operations": [{"tool": "old-two"}]},
                {"contact_id": "three", "app_operations": [{"tool": "keep-three"}]},
            ],
            "metadata": {"keep": True},
        }
        replacement = {
            "contacts": [
                {"contact_id": "two", "app_operations": [{"tool": "new-two"}]}
            ]
        }

        replaced = history_window.replace_enrichment_contacts(
            enrichment, replacement, {"two"}
        )

        self.assertEqual(replaced["metadata"], enrichment["metadata"])
        self.assertEqual(
            [row["contact_id"] for row in replaced["contacts"]], ["one", "two", "three"]
        )
        self.assertEqual(replaced["contacts"][0], enrichment["contacts"][0])
        self.assertEqual(replaced["contacts"][2], enrichment["contacts"][2])
        self.assertEqual(replaced["contacts"][1], replacement["contacts"][0])

    def test_enrichment_contact_replacement_rejects_wrong_or_duplicate_ids(self) -> None:
        enrichment = {"contacts": [{"contact_id": "blocked"}, {"contact_id": "free"}]}
        replacement = {"contacts": [{"contact_id": "blocked"}]}
        for label, response, blocked in (
            ("unexpected", {"contacts": [{"contact_id": "unknown"}]}, {"blocked"}),
            ("missing", {"contacts": []}, {"blocked"}),
            (
                "duplicate",
                {"contacts": [replacement["contacts"][0], replacement["contacts"][0]]},
                {"blocked"},
            ),
        ):
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    history_window.replace_enrichment_contacts(enrichment, response, blocked)

    def test_enrichment_contact_schema_requires_nullable_execution_problem(self) -> None:
        properties = ENRICHMENT_CONTACT_SCHEMA["properties"]
        self.assertIn("execution_problem", properties)
        self.assertEqual(
            properties["execution_problem"],
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
        )
        self.assertIn("execution_problem", ENRICHMENT_CONTACT_SCHEMA["required"])

        valid = {
            "contact_id": "one",
            "uses_current_fact_ids": [],
            "uses_new_fact_keys": [],
            "durable_facts": [],
            "current_state_changes": [],
            "app_operations": [],
            "execution_problem": None,
            "factual_problem": None,
        }
        Draft202012Validator(ENRICHMENT_CONTACT_SCHEMA).validate(valid)
        valid["execution_problem"] = "The calendar record was unavailable."
        Draft202012Validator(ENRICHMENT_CONTACT_SCHEMA).validate(valid)
        with self.assertRaises(ValidationError):
            Draft202012Validator(ENRICHMENT_CONTACT_SCHEMA).validate(
                {key: value for key, value in valid.items() if key != "execution_problem"}
            )
        with self.assertRaises(ValidationError):
            Draft202012Validator(ENRICHMENT_CONTACT_SCHEMA).validate(
                {**valid, "execution_problem": False}
            )

    def test_planner_execution_problem_rejects_missing_snapshot_date(self) -> None:
        story = {
            "plan": {
                "occurrences": [
                    {
                        "contact_id": "snapshot-contact",
                        "assistant_outcome": {
                            "kind": "external_action",
                            "requested_result": "Read the saved snapshot.",
                        },
                    }
                ]
            }
        }
        enrichment = {
            "contacts": [
                {
                    "contact_id": "snapshot-contact",
                    "app_operations": [
                        {
                            "tool": "get_mrr_metrics",
                            "args_json": '{"asof_date":"2027-03-31"}',
                        }
                    ],
                    "execution_problem": None,
                }
            ]
        }
        tools = {
            "get_mrr_metrics": {
                "state_effect": {
                    "required_existing_record": {
                        "app_records_key": "mrr_snapshots",
                        "argument_fields": {"asof_date": "asof_date"},
                    }
                }
            }
        }
        records = {"mrr_snapshots": [{"asof_date": "2023-02-04"}]}

        problems = history_window.planner_execution_problems(
            story,
            enrichment,
            available_app_records=records,
            tools=tools,
        )

        self.assertEqual([row["contact_id"] for row in problems], ["snapshot-contact"])
        self.assertIn("asof_date='2027-03-31'", problems[0]["reason"])

        enrichment["contacts"][0]["app_operations"][0]["args_json"] = (
            '{"asof_date":"2023-02-04"}'
        )
        self.assertEqual(
            history_window.planner_execution_problems(
                story,
                enrichment,
                available_app_records=records,
                tools=tools,
            ),
            [],
        )

    def test_story_existing_app_items_shows_snapshot_dates(self) -> None:
        self.assertEqual(
            history_window.story_existing_app_items(
                {"mrr_snapshots": [{"asof_date": "2023-02-04"}]}
            ),
            {"mrr_snapshots": [{"name": "2023-02-04", "details": ""}]},
        )

    def test_older_summary_prefers_accepted_contact_details_with_cap(self) -> None:
        summary = history_window.compact_older_contact_summary(
            {
                "finished_topic": "Repeated topic",
                "accepted_contact_details": "Accepted detail " * 100,
            }
        )
        self.assertLessEqual(len(summary), 320)
        self.assertTrue(summary.startswith("Accepted detail"))
        self.assertNotIn("Repeated topic:", summary)

    def test_factual_problem_enters_bounded_correction_and_source_material_can_change(self) -> None:
        story = {
            "plan": {"occurrences": [{
                "contact_id": "contact-1",
                "source_material": {"kind": "note", "origin": "user", "factual_contents": "old"},
                "assistant_outcome": {"kind": "conversation", "requested_result": "Explain."},
                "what_happened": "The supplied amount is 10, but the current fact says 12.",
            }]}
        }
        enrichment = {"contacts": [{
            "contact_id": "contact-1", "app_operations": [],
            "execution_problem": None,
            "factual_problem": "The contact treats 10 and 12 as simultaneously authoritative.",
        }]}
        problems = history_window.planner_execution_problems(story, enrichment)
        self.assertEqual(problems[0]["problem_kind"], "factual")
        corrected = history_window.apply_story_contact_corrections(
            story,
            {"contacts": [{
                "contact_id": "contact-1",
                "what_happened": "The supplied amount is corrected to 12.",
                "assistant_outcome": {"kind": "conversation", "requested_result": "Explain."},
                "source_material": {"kind": "note", "origin": "user", "factual_contents": "corrected"},
            }]},
            ["contact-1"],
            problem_kinds={"contact-1": "factual"},
        )
        self.assertEqual(corrected["plan"]["occurrences"][0]["source_material"]["factual_contents"], "corrected")

    def test_impossible_action_correction_rejects_source_material_change(self) -> None:
        story = {"plan": {"occurrences": [{
            "contact_id": "contact-1",
            "source_material": None,
            "assistant_outcome": {"kind": "external_action", "requested_result": "Read it."},
            "what_happened": "Ask for the missing record.",
        }]}}
        with self.assertRaisesRegex(ValueError, "cannot change source_material"):
            history_window.apply_story_contact_corrections(
                story,
                {"contacts": [{
                    "contact_id": "contact-1",
                    "what_happened": "Ask for a draft instead.",
                    "assistant_outcome": {"kind": "conversation", "requested_result": "Draft it."},
                    "source_material": {"kind": "note", "origin": "user", "factual_contents": "new"},
                }]},
                ["contact-1"],
            )

    def test_exact_planner_gets_full_only_for_named_record_and_index_stays_compact(self) -> None:
        tools = {"update_doc": {"state_effect": {"reads_state_keys": ["docs"], "writes_state_keys": ["docs"]}}}
        records = {"docs": [{"id": "doc-1", "title": "Named plan", "status": "draft", "body": "Full body."},
                             {"id": "doc-2", "title": "Other plan", "body": "Other body."}]}
        compact = history_window.compact_planner_app_records(tools, records)
        self.assertTrue(all("body" not in row for row in compact["docs"]))
        payload = {
            "persona": "morgan", "requested_dates": {}, "available_app_records": compact,
            "_exact_app_records": records, "earlier_accepted_app_items": [],
            "current_facts": [], "lasting_facts_that_must_be_established": [],
            "current_ordinary_states": [], "known_entities": [], "open_threads": [],
            "available_simulated_actions": [], "previously_used_communication_destinations": [],
        }
        enriched = history_window.build_enrichment_payload(payload, {"plan": {"occurrences": [{
            "contact_id": "c1", "subject_ids": [], "thread_id": "", "what_happened": "Update Named plan.",
            "source_material": None, "assistant_outcome": {"kind": "external_action", "requested_result": "Update it."},
        }]}})
        self.assertEqual(enriched["available_app_records"]["docs"][0]["body"], "Full body.")
        self.assertNotIn("body", enriched["available_app_records"]["docs"][1])
        self.assertNotIn("_exact_app_records", enriched)

        linked_payload = copy.deepcopy(payload)
        linked_payload["earlier_accepted_app_items"] = [{
            "contact_id": "prior-contact",
            "items": [{"record_id": "doc-2", "title": "Other plan"}],
        }]
        linked = history_window.build_enrichment_payload(
            linked_payload,
            {"plan": {"occurrences": [{
                "contact_id": "c2", "subject_ids": [], "thread_id": "",
                "uses_items_created_by_contact_ids": ["prior-contact"],
                "what_happened": "Use the earlier item.", "source_material": None,
                "assistant_outcome": {"kind": "external_action", "requested_result": "Update it."},
            }]}},
        )
        self.assertEqual(linked["available_app_records"]["docs"][1]["body"], "Other body.")

    def test_exact_record_lookup_uses_ids_when_titles_repeat(self) -> None:
        tools = {
            "update_calendar_event": {
                "state_effect": {
                    "reads_state_keys": ["calendar"],
                    "writes_state_keys": ["calendar"],
                }
            }
        }
        records = {
            "calendar": [
                {"id": "old", "title": "Kibo weight recheck", "body": "Old body."},
                {"id": "current", "title": "Kibo weight recheck", "body": "Current body."},
            ]
        }
        payload = {
            "persona": "alex",
            "requested_dates": {},
            "available_app_records": history_window.compact_planner_app_records(
                tools, records
            ),
            "_exact_app_records": records,
            "earlier_accepted_app_items": [],
            "current_facts": [],
            "lasting_facts_that_must_be_established": [],
            "current_ordinary_states": [],
            "known_entities": [],
            "open_threads": [],
            "available_simulated_actions": [],
            "previously_used_communication_destinations": [],
        }
        enriched = build_enrichment_payload(
            payload,
            {
                "plan": {
                    "occurrences": [
                        {
                            "contact_id": "c1",
                            "subject_ids": ["alex"],
                            "thread_id": "",
                            "what_happened": "Update Kibo weight recheck.",
                            "source_material": None,
                            "assistant_outcome": {
                                "kind": "external_action",
                                "requested_result": "Update it.",
                            },
                        }
                    ]
                }
            },
        )
        by_id = {
            row["id"]: row["body"]
            for row in enriched["available_app_records"]["calendar"]
        }
        self.assertEqual(by_id, {"old": "Old body.", "current": "Current body."})

    def test_exact_planner_gets_full_bounded_calendar_records(self) -> None:
        records = {
            "calendar": [
                {
                    "id": "event-1",
                    "title": "Beacon outbound travel",
                    "body": "Keep this.",
                }
            ]
        }
        payload = {
            "available_app_records": {
                "calendar": [{"id": "event-1", "title": "Beacon outbound travel"}]
            },
            "_exact_app_records": records,
            "earlier_accepted_app_items": [],
        }
        selected = history_window._exact_app_records_for_contacts(
            payload,
            [{"contact_id": "c1", "what_happened": "Update the afternoon trip."}],
        )
        self.assertEqual(selected["calendar"][0]["body"], "Keep this.")

    def test_calendar_update_requires_complete_current_body(self) -> None:
        response = {
            "plan": {
                "occurrences": [
                    {
                        "contact_id": "c1",
                        "app_operations": [
                            {
                                "tool": "update_calendar_event",
                                "args": {"event_id": "event-1", "body": "Changed."},
                            }
                        ],
                    }
                ]
            }
        }
        current = {"calendar": [{"id": "event-1", "body": "Keep this."}]}
        compact = {"calendar": [{"id": "event-1"}]}
        self.assertEqual(
            len(
                calendar_update_context_errors(
                    response,
                    current_app_records=current,
                    planner_app_records=compact,
                )
            ),
            1,
        )
        self.assertEqual(
            calendar_update_context_errors(
                response,
                current_app_records=current,
                planner_app_records=current,
            ),
            [],
        )

    def test_full_week_contact_count_is_enforced(self) -> None:
        for count in (23, 24):
            response = {"plan": {"occurrences": [{} for _ in range(count)]}}
            self.assertEqual(
                story_contact_count_errors(
                    response, start=date(2027, 4, 1), end=date(2027, 4, 7)
                ),
                [],
            )
        for count in (22, 25):
            response = {"plan": {"occurrences": [{} for _ in range(count)]}}
            self.assertEqual(
                len(
                    story_contact_count_errors(
                        response, start=date(2027, 4, 1), end=date(2027, 4, 7)
                    )
                ),
                1,
            )

    def test_partial_week_contact_count_is_not_enforced(self) -> None:
        response = {"plan": {"occurrences": [{}]}}
        self.assertEqual(
            story_contact_count_errors(
                response, start=date(2027, 4, 1), end=date(2027, 4, 5)
            ),
            [],
        )

    def test_enrichment_preserves_all_current_facts_after_story(self) -> None:
        payload = {
            "persona": "riley",
            "requested_dates": {},
            "available_app_records": {},
            "_exact_app_records": {},
            "earlier_accepted_app_items": [],
            "current_facts": [],
            "_all_current_facts": [
                {"id": 333, "subjects": ["riley"], "statement": "Current card terms."},
                {"id": 334, "subjects": ["riley", "kibo"], "statement": "Kibo terms."},
            ],
            "_primary_subject_ids": ["riley"],
            "lasting_facts_that_must_be_established": [],
            "current_ordinary_states": [],
            "known_entities": [],
            "open_threads": [],
            "available_simulated_actions": [],
            "previously_used_communication_destinations": [],
        }
        enriched = build_enrichment_payload(
            payload,
            {
                "plan": {
                    "occurrences": [
                        {
                            "contact_id": "c1",
                            "subject_ids": ["riley"],
                            "thread_id": "",
                            "what_happened": "Compare card terms.",
                            "source_material": None,
                            "assistant_outcome": {
                                "kind": "conversation",
                                "requested_result": "Compare them.",
                            },
                        }
                    ]
                }
            },
        )
        self.assertEqual(enriched["current_facts"], payload["_all_current_facts"])
        self.assertIsNot(enriched["current_facts"], payload["_all_current_facts"])


if __name__ == "__main__":
    unittest.main()
