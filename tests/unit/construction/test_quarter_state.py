from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from construction.quarter_state import (
    materialize_entities,
    materialize_facts,
    project_app_operations,
)
from construction.runtime_operations import validate_operation_result_references


class QuarterStateTest(unittest.TestCase):
    def test_deterministic_replay_produces_identical_ids_and_state(self) -> None:
        plan = {
            "contacts": [
                {
                    "session_id": "extension_morgan_2025_02_03_001",
                    "narrative_date": "2025-02-03T10:00:00-08:00",
                    "app_operations": [
                        {
                            "tool": "create_calendar_event",
                            "args": {
                                "title": "Deterministic meeting",
                                "start": "2025-02-03T10:00:00-08:00",
                                "end": "2025-02-03T10:30:00-08:00",
                                "attendees": [],
                            },
                        },
                        {
                            "tool": "create_calendar_event",
                            "args": {
                                "title": "Second deterministic meeting",
                                "start": "2025-02-03T11:00:00-08:00",
                                "end": "2025-02-03T11:30:00-08:00",
                                "attendees": [],
                            },
                        },
                    ],
                }
            ]
        }
        replay_inputs: list[int] = []

        def fake_request(batch: list[dict]) -> tuple[str, int, str]:
            state_path = Path(batch[0]["state_path"])
            state = json.loads(state_path.read_text())
            rows = []
            for replay_index, item in enumerate(batch):
                replay_ms = int(
                    datetime.fromisoformat(item["narrative_datetime"]).timestamp()
                    * 1000
                ) + replay_index
                event_id = f"evt_{replay_ms}"
                state.setdefault("calendar", []).append({"id": event_id})
                rows.append(
                    {
                        "contact_id": item["contact_id"],
                        "operation_index": item["operation_index"],
                        "session_id": item["session_id"],
                        "operation": {"tool": item["tool"], "args": item["args"]},
                        "exit_code": 0,
                        "result": {"ok": True, "id": event_id},
                        "stdout": json.dumps({"ok": True, "id": event_id}),
                        "stderr": "",
                        "replay_epoch_ms": replay_ms,
                    }
                )
            state_path.write_text(json.dumps(state))
            replay_inputs.extend(row["replay_epoch_ms"] for row in rows)
            return json.dumps(rows), 0, ""

        with tempfile.TemporaryDirectory() as temp, patch(
            "construction.runtime_operations._mock_tool_process",
            return_value=SimpleNamespace(request=fake_request),
        ):
            root = Path(temp)
            first_state, first_results, first_changes = project_app_operations(
                persona="morgan",
                plan=plan,
                starting_state={"calendar": []},
                work_dir=root / "first",
                tool_python=Path("python"),
            )
            second_state, second_results, second_changes = project_app_operations(
                persona="morgan",
                plan=plan,
                starting_state={"calendar": []},
                work_dir=root / "second",
                tool_python=Path("python"),
            )

        self.assertEqual(first_state, second_state)
        self.assertEqual(first_changes, second_changes)
        self.assertEqual(
            [row["result"]["id"] for row in first_results],
            [row["result"]["id"] for row in second_results],
        )
        self.assertEqual(
            [row["replay_epoch_ms"] for row in first_results],
            [row["replay_epoch_ms"] for row in second_results],
        )
        self.assertEqual(replay_inputs[:2], replay_inputs[2:])
        self.assertEqual(replay_inputs[1], replay_inputs[0] + 1)

    def test_same_week_result_reference_resolves_and_records_concrete_args(self) -> None:
        plan = {
            "contacts": [
                {
                    "contact_id": "create-contact",
                    "session_id": "extension_morgan_2025_02_03_001",
                    "narrative_date": "2025-02-03T10:00:00-08:00",
                    "app_operations": [
                        {
                            "tool": "create_calendar_event",
                            "args": {
                                "title": "Deterministic meeting",
                                "start": "2025-02-03T10:00:00-08:00",
                                "end": "2025-02-03T10:30:00-08:00",
                                "attendees": [],
                            },
                        }
                    ],
                },
                {
                    "contact_id": "update-contact",
                    "session_id": "extension_morgan_2025_02_04_001",
                    "narrative_date": "2025-02-04T10:00:00-08:00",
                    "app_operations": [
                        {
                            "tool": "update_calendar_event",
                            "args": {
                                "event_id": {
                                    "$operation_result": {
                                        "contact_id": "create-contact",
                                        "operation_index": 0,
                                        "field": "id",
                                    }
                                },
                                "start": "2025-02-03T11:00:00-08:00",
                                "end": "2025-02-03T11:30:00-08:00",
                            },
                        }
                    ],
                },
            ]
        }

        def fake_request(batch: list[dict]) -> tuple[str, int, str]:
            state_path = Path(batch[0]["state_path"])
            state = json.loads(state_path.read_text())
            rows = []
            prior_result_id = None
            for replay_index, item in enumerate(batch):
                tool = item["tool"]
                args = item["args"]
                if isinstance(args.get("event_id"), dict):
                    args = dict(args)
                    args["event_id"] = prior_result_id
                replay_ms = int(
                    datetime.fromisoformat(item["narrative_datetime"]).timestamp()
                    * 1000
                ) + replay_index
                if tool == "create_calendar_event":
                    event_id = f"evt_{replay_ms}"
                    state.setdefault("calendar", []).append({"id": event_id, **args})
                    result = {"ok": True, "id": event_id}
                    prior_result_id = event_id
                else:
                    event = next(
                        row for row in state["calendar"] if row["id"] == args["event_id"]
                    )
                    event.update({key: value for key, value in args.items() if key != "event_id"})
                    result = {"ok": True, "id": event["id"]}
                rows.append(
                    {
                        "contact_id": item["contact_id"],
                        "operation_index": item["operation_index"],
                        "session_id": item["session_id"],
                        "operation": {"tool": tool, "args": args},
                        "exit_code": 0,
                        "result": result,
                        "stdout": json.dumps(result),
                        "stderr": "",
                        "replay_epoch_ms": replay_ms,
                    }
                )
            state_path.write_text(json.dumps(state))
            return json.dumps(rows), 0, ""

        with tempfile.TemporaryDirectory() as temp, patch(
            "construction.runtime_operations._mock_tool_process",
            return_value=SimpleNamespace(request=fake_request),
        ):
            root = Path(temp)
            state, results, _changes = project_app_operations(
                persona="morgan",
                plan=plan,
                starting_state={"calendar": []},
                work_dir=root / "replay",
                tool_python=Path("python"),
            )
            logged_event_id = json.loads(
                (root / "replay" / "operation_results.json").read_text()
            )[1]["operation"]["args"]["event_id"]

        event_id = results[0]["result"]["id"]
        self.assertEqual(results[1]["operation"]["args"]["event_id"], event_id)
        self.assertNotIsInstance(results[1]["operation"]["args"]["event_id"], dict)
        self.assertEqual(
            plan["contacts"][1]["app_operations"][0]["args"]["event_id"],
            event_id,
        )
        self.assertEqual(logged_event_id, event_id)
        self.assertEqual(state["calendar"][0]["id"], event_id)
        self.assertEqual(state["calendar"][0]["start"], "2025-02-03T11:00:00-08:00")

    def test_explicit_replay_epoch_mapping_preserves_retained_generated_id(self) -> None:
        removed_epoch = 1738602000000
        retained_epoch = 1738605600001
        update_epoch = 1738609200002
        plan = {
            "contacts": [
                {
                    "contact_id": "retained-create",
                    "session_id": "retained-create-session",
                    "narrative_date": "2025-02-01T09:00:00-08:00",
                    "app_operations": [{
                        "tool": "create_calendar_event",
                        "args": {
                            "title": "Retained event",
                            "start": "2025-02-01T09:00:00-08:00",
                            "end": "2025-02-01T09:30:00-08:00",
                            "attendees": [],
                        },
                    }],
                },
                {
                    "contact_id": "retained-update",
                    "session_id": "retained-update-session",
                    "narrative_date": "2025-02-01T10:00:00-08:00",
                    "app_operations": [{
                        "tool": "update_calendar_event",
                        "args": {
                            "event_id": f"evt_{retained_epoch}",
                            "start": "2025-02-01T10:00:00-08:00",
                            "end": "2025-02-01T10:30:00-08:00",
                        },
                    }],
                },
            ]
        }

        def fake_request(batch: list[dict]) -> tuple[str, int, str]:
            state_path = Path(batch[0]["state_path"])
            state = json.loads(state_path.read_text())
            rows = []
            for item in batch:
                tool = item["tool"]
                args = item["args"]
                replay_ms = item["replay_epoch_ms"]
                if tool == "create_calendar_event":
                    event_id = f"evt_{replay_ms}"
                    state.setdefault("calendar", []).append({"id": event_id, **args})
                    result = {"ok": True, "id": event_id}
                    returncode = 0
                else:
                    event = next(
                        (row for row in state.get("calendar", []) if row["id"] == args["event_id"]),
                        None,
                    )
                    if event is None:
                        result = {"error": "calendar event not found"}
                        returncode = 1
                    else:
                        event.update({key: value for key, value in args.items() if key != "event_id"})
                        result = {"ok": True, "id": event["id"]}
                        returncode = 0
                rows.append(
                    {
                        "contact_id": item["contact_id"],
                        "operation_index": item["operation_index"],
                        "session_id": item["session_id"],
                        "operation": {"tool": tool, "args": args},
                        "exit_code": returncode,
                        "result": result,
                        "stdout": json.dumps(result),
                        "stderr": "",
                        "replay_epoch_ms": replay_ms,
                    }
                )
                if returncode != 0:
                    break
            state_path.write_text(json.dumps(state))
            return json.dumps(rows), returncode, ""

        with tempfile.TemporaryDirectory() as temp, patch(
            "construction.runtime_operations._mock_tool_process",
            return_value=SimpleNamespace(request=fake_request),
        ):
            _state, results, _changes = project_app_operations(
                persona="morgan",
                plan=plan,
                starting_state={"calendar": []},
                work_dir=Path(temp) / "replay",
                tool_python=Path("python"),
                replay_epoch_ms_by_operation={
                    ("retained-create-session", 0): retained_epoch,
                    ("retained-update-session", 0): update_epoch,
                },
            )

        self.assertEqual(results[0]["result"]["id"], f"evt_{retained_epoch}")
        self.assertEqual(results[1]["result"]["id"], f"evt_{retained_epoch}")
        self.assertEqual(
            [row["replay_epoch_ms"] for row in results], [retained_epoch, update_epoch]
        )

    def test_replay_fails_when_retained_operation_uses_removed_generated_record(self) -> None:
        removed_epoch = 1738602000000
        plan = {
            "contacts": [{
                "contact_id": "retained-update",
                "session_id": "retained-update-session",
                "narrative_date": "2025-02-01T10:00:00-08:00",
                "app_operations": [{
                    "tool": "update_calendar_event",
                    "args": {
                        "event_id": f"evt_{removed_epoch}",
                        "start": "2025-02-01T10:00:00-08:00",
                        "end": "2025-02-01T10:30:00-08:00",
                    },
                }],
            }]
        }

        def fake_request(batch: list[dict]) -> tuple[str, int, str]:
            item = batch[0]
            return (
                json.dumps([{
                    "contact_id": item["contact_id"],
                    "operation_index": item["operation_index"],
                    "session_id": item["session_id"],
                    "operation": {"tool": item["tool"], "args": item["args"]},
                    "exit_code": 1,
                    "result": {"error": "calendar event not found"},
                    "stdout": json.dumps({"error": "calendar event not found"}),
                    "stderr": "",
                    "replay_epoch_ms": item["replay_epoch_ms"],
                }]),
                1,
                "",
            )

        with tempfile.TemporaryDirectory() as temp, patch(
            "construction.runtime_operations._mock_tool_process",
            return_value=SimpleNamespace(request=fake_request),
        ):
            with self.assertRaisesRegex(ValueError, "calendar event not found"):
                project_app_operations(
                    persona="morgan",
                    plan=plan,
                    starting_state={"calendar": []},
                    work_dir=Path(temp) / "replay",
                    tool_python=Path("python"),
                    replay_epoch_ms_by_operation={
                        ("retained-update-session", 0): 1738605600000,
                    },
                )

    def test_project_app_operations_uses_existing_epoch_calculation_without_mapping(self) -> None:
        plan = {
            "contacts": [{
                "session_id": "extension_morgan_2025_02_03_001",
                "narrative_date": "2025-02-03T10:00:00-08:00",
                "app_operations": [{
                    "tool": "create_calendar_event",
                    "args": {
                        "title": "Unmapped meeting",
                        "start": "2025-02-03T10:00:00-08:00",
                        "end": "2025-02-03T10:30:00-08:00",
                        "attendees": [],
                    },
                }],
            }]
        }
        captured: dict[str, object] = {}

        def executor(**kwargs: object) -> list[dict[str, object]]:
            captured.update(kwargs)
            return []

        with tempfile.TemporaryDirectory() as temp:
            project_app_operations(
                persona="morgan",
                plan=plan,
                starting_state={"calendar": []},
                work_dir=Path(temp) / "replay",
                tool_python=Path("python"),
                executor=executor,
            )

        self.assertIsNone(captured["replay_epoch_ms_by_operation"])
        self.assertTrue(captured["deterministic_replay"])

    def test_operation_result_reference_validation_rejects_invalid_links(self) -> None:
        context = {
            "label": "sessions[1].app_operations[0]",
            "current_contact_id": "current",
            "current_operation_index": 1,
            "contact_positions": {"earlier": 0, "current": 1, "later": 2},
            "operation_counts": {"earlier": 1, "current": 2, "later": 1},
        }
        valid_payload = {
            "contact_id": "earlier",
            "operation_index": 0,
            "field": "id",
        }
        cases = {
            "malformed": {"$operation_result": {"contact_id": "earlier"}},
            "nested": {"wrapper": {"$operation_result": valid_payload}},
            "forward": {
                "$operation_result": {
                    "contact_id": "current",
                    "operation_index": 1,
                    "field": "id",
                }
            },
            "missing_contact": {
                "$operation_result": {
                    "contact_id": "missing",
                    "operation_index": 0,
                    "field": "id",
                }
            },
            "missing_operation": {
                "$operation_result": {
                    "contact_id": "earlier",
                    "operation_index": 1,
                    "field": "id",
                }
            },
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                errors = validate_operation_result_references(
                    {"event_id": value}, **context
                )
                self.assertTrue(errors)

    def test_fact_and_entity_materialization_resolves_contact_references(self) -> None:
        plan = {
            "new_entities": [
                {
                    "id": "project:new",
                    "name": "New Project",
                    "kind": "project",
                    "reason": "It begins in this contact.",
                    "introduced_in_contact_id": "contact-one",
                }
            ],
            "contacts": [
                {
                    "contact_id": "contact-one",
                    "session_id": "session-one",
                    "new_facts": [
                        {
                            "key": "new-rule",
                            "statement": "The new rule applies.",
                            "applies_when": "the project is discussed",
                            "subjects": ["project:new"],
                            "supersedes": [1],
                            "evidence_contact_ids": ["contact-one"],
                        }
                    ],
                }
            ],
        }
        entities, added_entities = materialize_entities([], plan)
        facts, added_facts, keys = materialize_facts(
            [{"id": 1, "statement": "The old rule applies."}], plan
        )

        self.assertEqual(entities, added_entities)
        self.assertEqual(entities[0]["introduced_in"], "session-one")
        self.assertEqual(keys, {"new-rule": 2})
        self.assertEqual(added_facts[0]["source_session_ids"], ["session-one"])
        self.assertEqual(added_facts[0]["supersedes"], [1])
        self.assertEqual(facts[-1], added_facts[0])

    def test_fact_materialization_rejects_key_from_starting_facts(self) -> None:
        plan = {
            "contacts": [
                {
                    "contact_id": "new-contact",
                    "session_id": "session-new",
                    "new_facts": [{"key": "existing-rule"}],
                }
            ]
        }

        with self.assertRaisesRegex(
            ValueError, "fact key existing-rule already exists in starting facts"
        ):
            materialize_facts(
                [{"id": 1, "construction_key": "existing-rule"}],
                plan,
            )

    def test_fact_materialization_rejects_duplicate_plan_keys(self) -> None:
        plan = {
            "contacts": [
                {
                    "contact_id": "first-contact",
                    "session_id": "session-first",
                    "new_facts": [{"key": "repeated-rule"}],
                },
                {
                    "contact_id": "second-contact",
                    "session_id": "session-second",
                    "new_facts": [{"key": "repeated-rule"}],
                },
            ]
        }

        with self.assertRaisesRegex(
            ValueError, "fact key repeated-rule appears more than once in the current plan"
        ):
            materialize_facts([], plan)

    def test_replacement_inherits_interleaved_old_sources_in_history_order(self) -> None:
        existing = [
            {
                "id": 1,
                "statement": "The first old rule remains partly current.",
                "source_session_ids": ["session-b", "session-d"],
            },
            {
                "id": 2,
                "statement": "The second old rule remains partly current.",
                "source_session_ids": ["session-a", "session-c"],
            },
        ]
        accepted_history = [
            {"id": "session-a", "narrative_date": "2025-04-01T09:00:00-07:00"},
            {"id": "session-b", "narrative_date": "2025-04-02T09:00:00-07:00"},
            {"id": "session-c", "narrative_date": "2025-04-03T09:00:00-07:00"},
            {"id": "session-d", "narrative_date": "2025-04-04T09:00:00-07:00"},
            {"id": "session-new", "narrative_date": "2025-04-05T09:00:00-07:00"},
        ]
        plan = {
            "new_entities": [],
            "contacts": [
                {
                    "contact_id": "new-contact",
                    "session_id": "session-new",
                    "new_facts": [
                        {
                            "key": "combined-current-rule",
                            "statement": "The changed rule keeps both unchanged clauses.",
                            "applies_when": "the work is discussed",
                            "subjects": ["person:morgan"],
                            "supersedes": [2, 1],
                            "evidence_contact_ids": ["new-contact"],
                        }
                    ],
                }
            ],
        }

        _, added, _ = materialize_facts(
            existing,
            plan,
            accepted_history=accepted_history,
        )

        self.assertEqual(
            added[0]["source_session_ids"],
            ["session-a", "session-b", "session-c", "session-d", "session-new"],
        )

    def test_same_contact_sibling_replacements_can_share_one_old_fact(self) -> None:
        existing = [
            {
                "id": 1,
                "statement": "The old combined rule applies.",
                "source_session_ids": ["session-old"],
            }
        ]
        plan = {
            "new_entities": [],
            "contacts": [
                {
                    "contact_id": "new-contact",
                    "session_id": "session-new",
                    "new_facts": [
                        {
                            "key": "replacement-a",
                            "statement": "The first replacement rule applies.",
                            "applies_when": "the first issue is discussed",
                            "subjects": ["person:morgan"],
                            "supersedes": [1],
                            "evidence_contact_ids": ["new-contact"],
                        },
                        {
                            "key": "replacement-b",
                            "statement": "The second replacement rule applies.",
                            "applies_when": "the second issue is discussed",
                            "subjects": ["person:morgan"],
                            "supersedes": [1],
                            "evidence_contact_ids": ["new-contact"],
                        },
                    ],
                }
            ],
        }

        _, added, _ = materialize_facts(
            existing,
            plan,
            accepted_history=[
                {"id": "session-old"},
                {"id": "session-new"},
            ],
        )

        self.assertEqual(len(added), 2)
        self.assertEqual(
            [fact["source_session_ids"] for fact in added],
            [
                ["session-old", "session-new"],
                ["session-old", "session-new"],
            ],
        )

    def test_same_development_can_replace_one_old_fact_across_two_contacts(self) -> None:
        existing = [
            {
                "id": 1,
                "statement": "The old combined rule applies.",
                "source_session_ids": ["session-old"],
            }
        ]
        plan = {
            "new_entities": [],
            "contacts": [
                {
                    "contact_id": "first-contact",
                    "session_id": "session-first",
                    "development_ids": ["accepted-change"],
                    "new_facts": [
                        {
                            "key": "replacement-a",
                            "statement": "The first replacement rule applies.",
                            "applies_when": "the first issue is discussed",
                            "subjects": ["person:morgan"],
                            "supersedes": [1],
                            "evidence_contact_ids": ["first-contact"],
                        }
                    ],
                },
                {
                    "contact_id": "second-contact",
                    "session_id": "session-second",
                    "development_ids": ["accepted-change"],
                    "new_facts": [
                        {
                            "key": "replacement-b",
                            "statement": "The second replacement rule applies.",
                            "applies_when": "the second issue is discussed",
                            "subjects": ["person:morgan"],
                            "supersedes": [1],
                            "evidence_contact_ids": ["second-contact"],
                        }
                    ],
                },
            ],
        }

        _, added, _ = materialize_facts(
            existing,
            plan,
            accepted_history=[
                {"id": "session-old"},
                {"id": "session-first"},
                {"id": "session-second"},
            ],
        )

        self.assertEqual(len(added), 2)
        self.assertEqual(
            [fact["source_session_ids"] for fact in added],
            [
                ["session-old", "session-first"],
                ["session-old", "session-second"],
            ],
        )

    def test_same_window_replacement_inherits_the_full_prior_source_chain(self) -> None:
        existing = [
            {
                "id": 1,
                "statement": "Morgan owns the original operating rule.",
                "source_session_ids": ["session-old"],
            }
        ]
        accepted_history = [
            {"id": "session-old", "narrative_date": "2025-04-01T09:00:00-07:00"},
            {"id": "session-first", "narrative_date": "2025-04-02T09:00:00-07:00"},
            {"id": "session-second", "narrative_date": "2025-04-03T09:00:00-07:00"},
        ]
        plan = {
            "new_entities": [],
            "contacts": [
                {
                    "contact_id": "first-contact",
                    "session_id": "session-first",
                    "new_facts": [
                        {
                            "key": "interim-rule",
                            "statement": "Sarah now owns the operating rule.",
                            "applies_when": "the work is discussed",
                            "subjects": ["person:morgan"],
                            "supersedes": [1],
                            "evidence_contact_ids": ["first-contact"],
                        }
                    ],
                },
                {
                    "contact_id": "second-contact",
                    "session_id": "session-second",
                    "new_facts": [
                        {
                            "key": "current-rule",
                            "statement": "Sarah owns the final operating rule.",
                            "applies_when": "the work is discussed",
                            "subjects": ["person:morgan"],
                            "supersedes": ["interim-rule"],
                            "evidence_contact_ids": ["second-contact"],
                        }
                    ],
                },
            ],
        }

        _, added, keys = materialize_facts(
            existing,
            plan,
            accepted_history=accepted_history,
        )

        self.assertEqual(keys, {"interim-rule": 2, "current-rule": 3})
        self.assertEqual(added[0]["source_session_ids"], ["session-old", "session-first"])
        self.assertEqual(
            added[1]["source_session_ids"],
            ["session-old", "session-first", "session-second"],
        )

    def test_sidecar_sibling_inherits_old_evidence_without_retiring_the_old_fact(
        self,
    ) -> None:
        existing = [
            {
                "id": 1,
                "statement": "Morgan owns the forecast and sends Friday updates.",
                "source_session_ids": ["session-old"],
            }
        ]
        plan = {
            "new_entities": [],
            "contacts": [
                {
                    "contact_id": "owner-contact",
                    "session_id": "session-owner",
                    "new_facts": [
                        {
                            "key": "forecast-owner",
                            "statement": "Sarah now owns the forecast.",
                            "applies_when": "when the forecast is discussed",
                            "subjects": ["person:morgan"],
                            "supersedes": [1],
                            "evidence_contact_ids": ["owner-contact"],
                        }
                    ],
                },
                {
                    "contact_id": "update-contact",
                    "session_id": "session-update",
                    "new_facts": [
                        {
                            "key": "friday-update",
                            "statement": "The Friday update remains required.",
                            "applies_when": "when the forecast is updated",
                            "subjects": ["person:morgan"],
                            "supersedes": [],
                            "evidence_contact_ids": ["update-contact"],
                        }
                    ],
                },
            ],
        }
        accepted_history = [
            {"id": "session-old", "narrative_date": "2025-04-01T09:00:00-07:00"}
        ]

        facts, added, _ = materialize_facts(
            existing,
            plan,
            accepted_history=accepted_history,
            inherited_source_fact_ids_by_key={
                "forecast-owner": [1],
                "friday-update": [1],
            },
        )

        self.assertEqual(added[0]["supersedes"], [1])
        self.assertEqual(added[1]["supersedes"], [])
        self.assertEqual(
            added[0]["source_session_ids"], ["session-old", "session-owner"]
        )
        self.assertEqual(
            added[1]["source_session_ids"], ["session-old", "session-update"]
        )
        self.assertEqual(facts[-1], added[1])

    def test_sidecar_sources_are_deduplicated_and_globally_chronological(self) -> None:
        existing = [
            {"id": 1, "statement": "First", "source_session_ids": ["session-b", "session-d"]},
            {"id": 2, "statement": "Second", "source_session_ids": ["session-a", "session-c", "session-d"]},
        ]
        accepted_history = [
            {"id": "session-a", "narrative_date": "2025-04-01T09:00:00-07:00"},
            {"id": "session-b", "narrative_date": "2025-04-02T09:00:00-07:00"},
            {"id": "session-c", "narrative_date": "2025-04-03T09:00:00-07:00"},
            {"id": "session-d", "narrative_date": "2025-04-04T09:00:00-07:00"},
        ]
        plan = {
            "new_entities": [],
            "contacts": [
                {
                    "contact_id": "contact-new",
                    "session_id": "session-new",
                    "new_facts": [
                        {
                            "key": "combined",
                            "statement": "The current rule keeps the separate clauses.",
                            "applies_when": "when the work is discussed",
                            "subjects": ["person:morgan"],
                            "supersedes": [],
                            "evidence_contact_ids": ["contact-new"],
                        }
                    ],
                }
            ],
        }

        _, added, _ = materialize_facts(
            existing,
            plan,
            accepted_history=accepted_history,
            inherited_source_fact_ids_by_key={"combined": [2, 1, 2]},
        )

        self.assertEqual(
            added[0]["source_session_ids"],
            ["session-a", "session-b", "session-c", "session-d", "session-new"],
        )



if __name__ == "__main__":
    unittest.main()
