from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from construction.checkpoints import (
    load_checkpoint,
    save_checkpoint,
    validated_checkpoint_identity,
)
from construction.history_quarter_corrections import _correction_replay_epoch_mapping
from construction.quarter_runner import run


class HistoryQuarterCorrectionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tool_validation = patch(
            "construction.history_quarter_corrections.build_history_window.validate_tool_python"
        )
        self.tool_validation.start()

    def tearDown(self) -> None:
        self.tool_validation.stop()

    def state(self) -> dict:
        return {
            "history": [], "session_purposes": [],
            "entities": [{"id": "person:morgan", "name": "Morgan"}],
            "facts": [], "app_state": {}, "unfinished_threads": [],
        }

    def checkpoint(self, path: Path, *, end: str, state: dict | None = None, previous: str = "previous") -> None:
        save_checkpoint(
            path=path, persona="morgan", week={"week_id": end, "end_date": end},
            previous_hash=previous, inputs={}, state=state or self.state(),
            operation_results=[], covered_events=[], system_sha256="system", identity_version=2,
        )

    def session(self, session_id: str, contact_id: str, *, thread_id: str = "", open_thread: bool = False, continues: list[str] | None = None, fact: bool = False) -> dict:
        return {
            "contact_id": contact_id, "session_id": session_id,
            "narrative_date": "2025-06-24T09:00:00-07:00",
            "purpose": f"Purpose for {session_id}", "messages": [f"Message for {session_id}"],
            "continues_from": continues or [], "subject_ids": ["person:morgan"],
            "development_ids": [], "anchor_item_ids": [], "uses_facts": [],
            "new_facts": ([{
                "key": f"fact_{session_id}", "statement": "A durable term.",
                "applies_when": "current", "subjects": ["person:morgan"],
                "evidence_contact_ids": [contact_id], "supersedes": [],
            }] if fact else []),
            "app_operations": [], "thread_id": thread_id,
            "thread_open_after_contact": open_thread,
            "what_remains_after_contact": "Keep going" if open_thread else None,
        }

    def fixture(
        self,
        root: Path,
        sessions: list[dict],
        *,
        starting_state: dict | None = None,
        source_generation_calls: int = 2,
    ) -> tuple[dict, Path]:
        root.mkdir(parents=True, exist_ok=True)
        start = root / "starting_final"
        self.checkpoint(start, end="2025-06-23", state=starting_state)
        files = {name: root / filename for name, filename in {
            "spec": "spec.yaml", "persona_sheet": "persona.md", "generator_context": "context.md",
            "overview": "overview.json", "quarter_plan": "quarter_plan.json",
        }.items()}
        files["spec"].write_text("persona: morgan\nnarrative_timezone: America/Los_Angeles\n")
        files["persona_sheet"].write_text("Morgan\n")
        files["generator_context"].write_text("Context\n")
        files["overview"].write_text("{}\n")
        files["quarter_plan"].write_text(json.dumps({"plan": {"period_start": "2025-04-01", "period_end": "2025-06-30", "events": []}}))
        config = {
            "version": 1, "persona": "morgan", "quarter_id": "2025_q2",
            "quarter_start": "2025-04-01", "quarter_end": "2025-06-30",
            **{key: str(path) for key, path in files.items()}, "starting_checkpoint": str(start),
        }
        config_path = root / "config.yaml"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False))
        source = root / "failed_source"
        window = source / "windows" / "2025-06-24_to_2025-06-30"
        window.mkdir(parents=True)
        window_config = {
            "persona": "morgan", "quarter_start": "2025-04-01", "quarter_end": "2025-06-30",
            **{key: str(path.resolve()) for key, path in files.items()},
        }
        (source / "window_config.yaml").write_text(yaml.safe_dump(window_config, sort_keys=False))
        plan = {"period_start": "2025-06-24", "period_end": "2025-06-30", "new_entities": [], "sessions": sessions}
        (window / "plan.json").write_text(json.dumps(plan))
        (window / "planner_plan.json").write_text(json.dumps({"occurrences": [{"contact_id": row["contact_id"], "current_state_changes": []} for row in sessions]}))
        (window / "planner_request.json").write_text("{}\n")
        (window / "planner_response.json").write_text("{}\n")
        (window / "writer_request.json").write_text("{}\n")
        (window / "writer_response.json").write_text("{}\n")
        (window / "operation_results.json").write_text("[]\n")
        candidate = window / "candidate_checkpoint"
        self.checkpoint(candidate, end="2025-06-30", previous=validated_checkpoint_identity(start))
        identity = validated_checkpoint_identity(candidate)
        window_manifest = {
            "status": "passed", "persona": "morgan", "period_start": "2025-06-24", "period_end": "2025-06-30",
            "starting_checkpoint_sha256": validated_checkpoint_identity(start), "candidate_checkpoint_sha256": identity,
            "sessions": len(sessions), "new_facts": 0, "app_operations": 0,
            "model_calls": source_generation_calls,
        }
        (window / "manifest.json").write_text(json.dumps(window_manifest))
        source_manifest = {
            "status": "failed", "stage": "history_quarter_review", "windows": [{
                "start_date": "2025-06-24", "end_date": "2025-06-30", "output": str(window), "status": "passed",
                "starting_checkpoint_sha256": validated_checkpoint_identity(start),
                "candidate_checkpoint_sha256": identity, "sessions": len(sessions),
                "new_facts": 0, "app_operations": 0,
                "model_calls": source_generation_calls,
            }],
        }
        (source / "manifest.json").write_text(json.dumps(source_manifest))
        corrections = root / "corrections.json"
        corrections.write_text(json.dumps({"replacements": [], "removals": []}))
        args = {
            "config": config_path, "out": root / "corrected", "tool_python": Path(sys.executable),
            "confirm_paid_calls": True, "source_failed_quarter": source,
            "approved_history_corrections": corrections,
        }
        return args, source

    def set_current_state_changes(
        self,
        root: Path,
        changes_by_contact_id: dict[str, list[dict]],
    ) -> None:
        planner_plan = root / "failed_source" / "windows" / "2025-06-24_to_2025-06-30" / "planner_plan.json"
        document = json.loads(planner_plan.read_text())
        for occurrence in document["occurrences"]:
            occurrence["current_state_changes"] = copy.deepcopy(
                changes_by_contact_id.get(occurrence["contact_id"], [])
            )
        planner_plan.write_text(json.dumps(document))

    def current_state_change(self, key: str) -> dict:
        return {
            "key": key,
            "subject_ids": ["person:morgan"],
            "statement": f"Temporary state {key}.",
        }

    def correction_args(self, values: dict, root: Path, document: dict) -> argparse.Namespace:
        path = root / "corrections.json"
        path.write_text(json.dumps(document))
        values = {**values, "approved_history_corrections": path}
        return argparse.Namespace(**values)

    def passed_review(self, calls: list[Path]):
        def review(output_dir: Path, **_: object) -> dict:
            calls.append(output_dir)
            self.assertEqual(os.environ["DOLPHINBENCH_CALL_LEDGER"], str(output_dir / "call_ledger.jsonl"))
            self.assertEqual(os.environ["DOLPHINBENCH_CONSTRUCTION_RUN_ID"], output_dir.name)
            with (output_dir / "call_ledger.jsonl").open("a") as ledger:
                ledger.write('{"model":"gpt-5.6-sol","cache_hit":false}\n')
            return {"status": "passed", "validation": {"valid": True, "passed": True, "errors": [], "finding_count": 0}}
        return review

    def test_replacement_is_exact_and_source_is_unchanged_without_weekly_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            values, source = self.fixture(root, [self.session("s1", "c1")])
            before = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
            args = self.correction_args(values, root, {"replacements": [{
                "session_id": "s1", "messages": ["Corrected message"], "purpose": "Corrected purpose", "app_operations": [],
            }], "removals": []})
            calls: list[Path] = []
            with patch("construction.quarter_runner.history_quarter_review.run_review", side_effect=self.passed_review(calls)), patch(
                "construction.quarter_runner.build_history_window.run", side_effect=AssertionError("weekly model stage called")
            ):
                manifest = run(args)
            plan = json.loads((root / "corrected" / "windows" / "2025-06-24_to_2025-06-30" / "plan.json").read_text())
            self.assertEqual(plan["sessions"][0]["session_id"], "s1")
            self.assertEqual(plan["sessions"][0]["messages"], ["Corrected message"])
            self.assertEqual(plan["sessions"][0]["purpose"], "Corrected purpose")
            self.assertEqual(before, {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()})
            self.assertEqual(calls, [root / "corrected"])
            self.assertEqual(manifest["model_calls"], {
                "expected_fresh": 1, "accepted_source_generation_calls": 2,
                "accepted_stage_calls": 1, "paid_calls": 1, "cache_hits": 0,
                "ledger_entries": 1, "actual": 1,
            })
            window_manifest = json.loads((root / "corrected" / "windows" / "2025-06-24_to_2025-06-30" / "manifest.json").read_text())
            self.assertEqual(
                {key: window_manifest[key] for key in ("mode", "persona", "period_start", "period_end")},
                {"mode": "history_quarter_correction", "persona": "morgan", "period_start": "2025-06-24", "period_end": "2025-06-30"},
            )

    def test_replacement_may_correct_fact_statement_without_changing_numeric_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_session = self.session("s1", "c1", fact=True)
            values, _ = self.fixture(root, [source_session])

            legacy_values = {**values, "out": root / "legacy_replay"}
            with patch(
                "construction.quarter_runner.history_quarter_review.run_review",
                side_effect=self.passed_review([]),
            ):
                run(self.correction_args(legacy_values, root, {
                    "replacements": [],
                    "removals": [],
                }))
            _, legacy_state = load_checkpoint(
                root / "legacy_replay" / "final_checkpoint"
            )
            legacy_facts = legacy_state["facts"]

            corrected_fact = copy.deepcopy(source_session["new_facts"][0])
            corrected_fact["statement"] = "The corrected durable term."
            replacement = {
                "session_id": "s1",
                "messages": ["Corrected message for the durable term."],
                "purpose": "Record the corrected durable term.",
                "app_operations": [],
                "new_facts": [corrected_fact],
            }
            with patch(
                "construction.quarter_runner.history_quarter_review.run_review",
                side_effect=self.passed_review([]),
            ):
                run(self.correction_args(values, root, {
                    "replacements": [replacement],
                    "removals": [],
                }))

            _, corrected_state = load_checkpoint(
                root / "corrected" / "final_checkpoint"
            )
            corrected_facts = corrected_state["facts"]
            corrected_plan = json.loads(
                (
                    root
                    / "corrected"
                    / "windows"
                    / "2025-06-24_to_2025-06-30"
                    / "plan.json"
                ).read_text()
            )
            self.assertEqual(corrected_plan["sessions"][0]["new_facts"], [corrected_fact])
            self.assertEqual(
                [fact["id"] for fact in corrected_facts],
                [fact["id"] for fact in legacy_facts],
            )
            self.assertEqual(corrected_facts[0]["statement"], corrected_fact["statement"])

    def test_replacement_cannot_change_fact_statement_from_accepted_quarter_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_session = self.session("s1", "c1", fact=True)
            values, _ = self.fixture(root, [source_session])
            quarter_plan_path = Path(values["config"]).parent / "quarter_plan.json"
            quarter_plan_path.write_text(json.dumps({"plan": {
                "period_start": "2025-04-01",
                "period_end": "2025-06-30",
                "events": [{
                    "id": "planned_event",
                    "facts_to_establish": [{
                        "key": source_session["new_facts"][0]["key"],
                        "statement": source_session["new_facts"][0]["statement"],
                    }],
                }],
            }}))
            changed_fact = copy.deepcopy(source_session["new_facts"][0])
            changed_fact["statement"] = "A different durable term."
            replacement = {
                "session_id": "s1",
                "messages": ["Correction"],
                "purpose": "Correction",
                "app_operations": [],
                "new_facts": [changed_fact],
            }

            with self.assertRaisesRegex(
                ValueError,
                "statement must exactly match the accepted quarter plan",
            ):
                run(self.correction_args(values, root, {
                    "replacements": [replacement],
                    "removals": [],
                }))

    def test_replacement_fact_key_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_session = self.session("s1", "c1", fact=True)
            values, _ = self.fixture(root, [source_session])
            changed_fact = copy.deepcopy(source_session["new_facts"][0])
            changed_fact["key"] = "different_key"
            replacement = {
                "session_id": "s1", "messages": ["Correction"],
                "purpose": "Correction", "app_operations": [],
                "new_facts": [changed_fact],
            }
            with self.assertRaisesRegex(ValueError, r"new_facts\[0\]\.key must exactly match"):
                run(self.correction_args(values, root, {
                    "replacements": [replacement],
                    "removals": [],
                }))

    def test_replacement_fact_metadata_changes_are_rejected(self) -> None:
        changes = {
            "applies_when": "historical",
            "subjects": ["person:someone_else"],
            "supersedes": [1],
            "evidence_contact_ids": ["different_contact"],
        }
        for field, changed_value in changes.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source_session = self.session("s1", "c1", fact=True)
                values, _ = self.fixture(root, [source_session])
                changed_fact = copy.deepcopy(source_session["new_facts"][0])
                changed_fact[field] = changed_value
                replacement = {
                    "session_id": "s1", "messages": ["Correction"],
                    "purpose": "Correction", "app_operations": [],
                    "new_facts": [changed_fact],
                }
                with self.assertRaisesRegex(
                    ValueError,
                    rf"new_facts\[0\]\.{field} must exactly match",
                ):
                    run(self.correction_args(values, root, {
                        "replacements": [replacement],
                        "removals": [],
                    }))

    def test_replacement_fact_count_changes_are_rejected(self) -> None:
        for replacement_count in (0, 2):
            with self.subTest(replacement_count=replacement_count), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source_session = self.session("s1", "c1", fact=True)
                values, _ = self.fixture(root, [source_session])
                replacement = {
                    "session_id": "s1", "messages": ["Correction"],
                    "purpose": "Correction", "app_operations": [],
                    "new_facts": [
                        copy.deepcopy(source_session["new_facts"][0])
                        for _ in range(replacement_count)
                    ],
                }
                with self.assertRaisesRegex(ValueError, "must preserve the source fact count"):
                    run(self.correction_args(values, root, {
                        "replacements": [replacement],
                        "removals": [],
                    }))

    def test_replacement_fact_reordering_and_empty_statement_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_session = self.session("s1", "c1", fact=True)
            second_fact = copy.deepcopy(source_session["new_facts"][0])
            second_fact["key"] = "second_fact"
            second_fact["statement"] = "A second durable term."
            source_session["new_facts"].append(second_fact)
            values, _ = self.fixture(root, [source_session])
            reordered = {
                "session_id": "s1", "messages": ["Correction"],
                "purpose": "Correction", "app_operations": [],
                "new_facts": [second_fact, source_session["new_facts"][0]],
            }
            with self.assertRaisesRegex(ValueError, r"new_facts\[0\]\.key must exactly match"):
                run(self.correction_args(values, root, {
                    "replacements": [reordered],
                    "removals": [],
                }))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_session = self.session("s1", "c1", fact=True)
            values, _ = self.fixture(root, [source_session])
            empty_statement = copy.deepcopy(source_session["new_facts"][0])
            empty_statement["statement"] = "   "
            replacement = {
                "session_id": "s1", "messages": ["Correction"],
                "purpose": "Correction", "app_operations": [],
                "new_facts": [empty_statement],
            }
            with self.assertRaisesRegex(ValueError, r"new_facts\[0\]\.statement must be a non-empty string"):
                run(self.correction_args(values, root, {
                    "replacements": [replacement],
                    "removals": [],
                }))

    def test_current_three_call_source_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            values, _ = self.fixture(
                root,
                [self.session("s1", "c1")],
                source_generation_calls=3,
            )
            calls: list[Path] = []
            with patch(
                "construction.quarter_runner.history_quarter_review.run_review",
                side_effect=self.passed_review(calls),
            ):
                manifest = run(argparse.Namespace(**values))

            self.assertEqual(
                manifest["model_calls"]["accepted_source_generation_calls"], 3
            )
            window_manifest = json.loads(
                (
                    root
                    / "corrected"
                    / "windows"
                    / "2025-06-24_to_2025-06-30"
                    / "manifest.json"
                ).read_text()
            )
            self.assertEqual(
                window_manifest["accepted_source_generation_model_calls"], 3
            )

    def test_current_six_call_source_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            values, _ = self.fixture(
                root,
                [self.session("s1", "c1")],
                source_generation_calls=6,
            )
            window_manifest_path = (
                root
                / "failed_source"
                / "windows"
                / "2025-06-24_to_2025-06-30"
                / "manifest.json"
            )
            window_manifest = json.loads(window_manifest_path.read_text())
            window_manifest["generation_stages"] = [
                "story_planning",
                "occurrence_enrichment",
                "unexecutable_contact_correction",
                "corrected_occurrence_enrichment",
                "missing_state_correction",
                "session_writing",
            ]
            window_manifest_path.write_text(json.dumps(window_manifest))

            with patch(
                "construction.quarter_runner.history_quarter_review.run_review",
                side_effect=self.passed_review([]),
            ):
                manifest = run(argparse.Namespace(**values))

            self.assertEqual(
                manifest["model_calls"]["accepted_source_generation_calls"], 6
            )

    def test_correction_replay_uses_saved_epochs_and_allocates_new_operation_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            window = root / "source-window"
            window.mkdir()
            removed = self.session("removed-session", "removed-contact")
            removed["app_operations"] = [{"tool": "create_calendar_event", "args": {}}]
            retained = self.session("retained-session", "retained-contact")
            retained["app_operations"] = [{"tool": "create_calendar_event", "args": {}}]
            (window / "operation_results.json").write_text(json.dumps([
                {
                    "session_id": "removed-session",
                    "operation_index": 0,
                    "replay_epoch_ms": 1738602000000,
                },
                {
                    "session_id": "retained-session",
                    "operation_index": 0,
                    "replay_epoch_ms": 1738605600001,
                },
            ]))
            corrected_retained = copy.deepcopy(retained)
            corrected_retained["app_operations"][0] = (
                {"tool": "post_pr_comment", "args": {}}
            )
            corrected_retained["app_operations"].append(
                {"tool": "create_calendar_event", "args": {}}
            )

            mapping = _correction_replay_epoch_mapping(
                source_windows=[{"source_window": window}],
                source_plans=[{"sessions": [removed, retained]}],
                corrected_plans=[{"sessions": [corrected_retained]}],
            )

        self.assertEqual(mapping[("retained-session", 0)], 1738605600001)
        self.assertGreater(mapping[("retained-session", 1)], 1738605600001)
        self.assertNotIn(
            mapping[("retained-session", 1)], {1738602000000, 1738605600001}
        )

    def test_replacement_cannot_remove_created_event_used_by_later_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            event_id = "evt_1750780800000"
            create = self.session("s1", "c1")
            create["app_operations"] = [{
                "tool": "create_calendar_event",
                "args": {
                    "title": "Source event",
                    "start": "2025-06-24T09:00:00-07:00",
                    "end": "2025-06-24T09:30:00-07:00",
                    "attendees": [],
                },
            }]
            update = self.session("s2", "c2")
            update["app_operations"] = [{
                "tool": "update_calendar_event",
                "args": {
                    "event_id": event_id,
                    "start": "2025-06-24T10:00:00-07:00",
                    "end": "2025-06-24T10:30:00-07:00",
                },
            }]
            values, source = self.fixture(root, [create, update])
            operation_results = [
                {
                    "session_id": "s1", "operation_index": 0,
                    "operation": copy.deepcopy(create["app_operations"][0]),
                    "result": {"ok": True, "id": event_id},
                    "replay_epoch_ms": 1750780800000,
                },
                {
                    "session_id": "s2", "operation_index": 0,
                    "operation": copy.deepcopy(update["app_operations"][0]),
                    "result": {"ok": True, "id": event_id},
                    "replay_epoch_ms": 1750780800001,
                },
            ]
            (source / "windows" / "2025-06-24_to_2025-06-30" / "operation_results.json").write_text(
                json.dumps(operation_results)
            )
            replacement = {
                "session_id": "s1", "messages": ["Corrected message."],
                "purpose": "Corrected purpose.", "app_operations": [],
            }

            with self.assertRaisesRegex(
                ValueError,
                r"source session 's1'.*record ID 'evt_1750780800000'.*"
                r"session 's2' tool 'update_calendar_event'",
            ), patch(
                "construction.history_quarter_corrections.build_history_window.project_app_operations",
                side_effect=AssertionError("local replay launched"),
            ):
                run(self.correction_args(values, root, {
                    "replacements": [replacement], "removals": [],
                }))

    def test_replacement_preserves_created_event_at_same_position(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            event_id = "evt_1750780800000"
            create = self.session("s1", "c1")
            create["app_operations"] = [{
                "tool": "create_calendar_event",
                "args": {
                    "title": "Source event",
                    "start": "2025-06-24T09:00:00-07:00",
                    "end": "2025-06-24T09:30:00-07:00",
                    "attendees": [],
                },
            }]
            update = self.session("s2", "c2")
            update["app_operations"] = [{
                "tool": "update_calendar_event",
                "args": {
                    "event_id": event_id,
                    "start": "2025-06-24T10:00:00-07:00",
                    "end": "2025-06-24T10:30:00-07:00",
                },
            }]
            values, source = self.fixture(root, [create, update])
            (source / "windows" / "2025-06-24_to_2025-06-30" / "operation_results.json").write_text(
                json.dumps([
                    {
                        "session_id": "s1", "operation_index": 0,
                        "operation": copy.deepcopy(create["app_operations"][0]),
                        "result": {"ok": True, "id": event_id},
                        "replay_epoch_ms": 1750780800000,
                    },
                    {
                        "session_id": "s2", "operation_index": 0,
                        "operation": copy.deepcopy(update["app_operations"][0]),
                        "result": {"ok": True, "id": event_id},
                        "replay_epoch_ms": 1750780800001,
                    },
                ])
            )
            replacement = {
                "session_id": "s1", "messages": ["Corrected message."],
                "purpose": "Corrected purpose.",
                "app_operations": [{
                    "tool": "create_calendar_event",
                    "args": {
                        "title": "Corrected event",
                        "start": "2025-06-24T09:00:00-07:00",
                        "end": "2025-06-24T09:30:00-07:00",
                        "attendees": [],
                    },
                }],
            }

            def replay(**kwargs: object) -> tuple[dict, list[dict], list[dict]]:
                replay_epochs = kwargs["replay_epoch_ms_by_operation"]
                self.assertEqual(replay_epochs[("s1", 0)], 1750780800000)
                self.assertEqual(replay_epochs[("s2", 0)], 1750780800001)
                return (
                    {"calendar": [{
                        "id": event_id, "title": "Corrected event",
                        "start": "2025-06-24T10:00:00-07:00",
                        "end": "2025-06-24T10:30:00-07:00", "attendees": [],
                    }]},
                    [
                        {
                            "contact_id": "c1", "session_id": "s1",
                            "operation_index": 0,
                            "operation": replacement["app_operations"][0],
                            "result": {"ok": True, "id": event_id},
                            "exit_code": 0, "stdout": "", "stderr": "",
                            "replay_epoch_ms": 1750780800000,
                        },
                        {
                            "contact_id": "c2", "session_id": "s2",
                            "operation_index": 0,
                            "operation": update["app_operations"][0],
                            "result": {"ok": True, "id": event_id},
                            "exit_code": 0, "stdout": "", "stderr": "",
                            "replay_epoch_ms": 1750780800001,
                        },
                    ],
                    [],
                )

            with patch(
                "construction.quarter_runner.history_quarter_review.run_review",
                side_effect=self.passed_review([]),
            ), patch(
                "construction.history_quarter_corrections.build_history_window.project_app_operations",
                side_effect=replay,
            ):
                manifest = run(self.correction_args(values, root, {
                    "replacements": [replacement], "removals": [],
                }))
            self.assertEqual(manifest["status"], "passed")
            _, state = load_checkpoint(root / "corrected" / "final_checkpoint")
            self.assertEqual(state["app_state"]["calendar"][0]["id"], event_id)
            self.assertEqual(state["app_state"]["calendar"][0]["title"], "Corrected event")

    def test_correction_details_always_use_the_accepted_user_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sessions = [self.session("s1", "c1"), self.session("s2", "c2")]
            values, _ = self.fixture(root, sessions)
            planner_plan = root / "failed_source" / "windows" / "2025-06-24_to_2025-06-30" / "planner_plan.json"
            planner_plan.write_text(json.dumps({"occurrences": [
                {"contact_id": "c1", "what_happened": "Old replaced details.", "source_material": {"factual_contents": "Old source material."}},
                {"contact_id": "c2", "what_happened": "Unchanged occurrence details.", "source_material": {"factual_contents": "Unchanged source material."}},
            ]}))
            args = self.correction_args(values, root, {"replacements": [{
                "session_id": "s1", "messages": ["  Corrected first message.  ", "Corrected second message."],
                "purpose": "Corrected purpose", "app_operations": [],
            }], "removals": []})
            with patch("construction.quarter_runner.history_quarter_review.run_review", side_effect=self.passed_review([])):
                run(args)
            state = json.loads((root / "corrected" / "final_checkpoint" / "session_purposes.json").read_text())
            by_session_id = {row["session_id"]: row for row in state}
            self.assertEqual(by_session_id["s1"]["accepted_contact_details"], "Corrected first message.\n\nCorrected second message.")
            self.assertEqual(
                by_session_id["s2"]["accepted_contact_details"],
                "Message for s2",
            )

    def test_source_checkpoint_state_changes_survive_another_correction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            window = root / "window"
            accepted_change = self.current_state_change("home:fan")
            self.checkpoint(
                window / "candidate_checkpoint",
                end="2025-06-30",
                state={
                    **self.state(),
                    "session_purposes": [
                        {
                            "session_id": "s1",
                            "current_state_changes": [accepted_change],
                        }
                    ],
                },
            )

            from construction.history_quarter_corrections import _source_session_purposes

            purposes = _source_session_purposes(window)
            self.assertEqual(
                purposes["s1"]["current_state_changes"],
                [accepted_change],
            )

    def test_replacement_current_state_changes_are_validated_and_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            values, _ = self.fixture(root, [self.session("s1", "c1")])
            statement = "The disputed service charge was removed in full."
            replacement = {
                "session_id": "s1",
                "messages": [f"Property management confirmed that {statement}"],
                "purpose": "Record the resolved service charge.",
                "app_operations": [],
                "current_state_changes": [
                    {
                        "key": "service_charge_dispute",
                        "subject_ids": ["person:morgan"],
                        "statement": statement,
                    }
                ],
            }
            with patch(
                "construction.quarter_runner.history_quarter_review.run_review",
                side_effect=self.passed_review([]),
            ):
                run(self.correction_args(values, root, {"replacements": [replacement], "removals": []}))
            purposes = json.loads(
                (root / "corrected" / "final_checkpoint" / "session_purposes.json").read_text()
            )
            self.assertEqual(purposes[0]["current_state_changes"], replacement["current_state_changes"])

    def test_replacement_current_state_requires_known_subject_and_message_support(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            values, _ = self.fixture(root, [self.session("s1", "c1")])
            unsupported = {
                "session_id": "s1",
                "messages": ["Property management confirmed the result."],
                "purpose": "Record the result.",
                "app_operations": [],
                "current_state_changes": [
                    {
                        "key": "service_charge_dispute",
                        "subject_ids": ["person:unknown"],
                        "statement": "The disputed service charge was removed in full.",
                    }
                ],
            }
            with self.assertRaisesRegex(ValueError, "unknown entity IDs"):
                run(self.correction_args(values, root, {"replacements": [unsupported], "removals": []}))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            values, _ = self.fixture(root, [self.session("s1", "c1")])
            unsupported["current_state_changes"][0]["subject_ids"] = ["person:morgan"]
            with self.assertRaisesRegex(ValueError, "must appear verbatim in the corrected messages"):
                run(self.correction_args(values, root, {"replacements": [unsupported], "removals": []}))

    def test_failed_correction_can_be_corrected_again_without_weekly_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            values, source = self.fixture(root, [self.session("s1", "c1")])
            window = source / "windows" / "2025-06-24_to_2025-06-30"
            generation = window / "accepted_source_generation"
            generation.mkdir()
            original_manifest = json.loads((window / "manifest.json").read_text())
            for name in (
                "planner_plan.json", "planner_request.json", "planner_response.json",
                "writer_request.json", "writer_response.json",
            ):
                (window / name).replace(generation / name)
            (generation / "manifest.json").write_text(json.dumps(original_manifest))
            corrected_manifest = {
                **original_manifest,
                "mode": "history_quarter_correction",
                "model_calls": 0,
                "accepted_source_generation_model_calls": 2,
            }
            (window / "manifest.json").write_text(json.dumps(corrected_manifest))
            source_manifest = json.loads((source / "manifest.json").read_text())
            source_manifest["windows"][0].update({
                "model_calls": 0,
                "accepted_source_generation_model_calls": 2,
            })
            (source / "manifest.json").write_text(json.dumps(source_manifest))
            calls: list[Path] = []
            with patch(
                "construction.quarter_runner.history_quarter_review.run_review",
                side_effect=self.passed_review(calls),
            ), patch(
                "construction.quarter_runner.build_history_window.run",
                side_effect=AssertionError("weekly model stage called"),
            ):
                manifest = run(self.correction_args(values, root, {"replacements": [], "removals": []}))
            self.assertEqual(manifest["status"], "passed")
            self.assertTrue(
                (root / "corrected" / "windows" / "2025-06-24_to_2025-06-30"
                 / "accepted_source_generation" / "planner_plan.json").is_file()
            )

    def test_safe_removal_and_deterministic_checkpoint_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            values, _ = self.fixture(root, [self.session("s1", "c1"), self.session("s2", "c2")])
            document = {"replacements": [], "removals": ["s1"]}
            calls: list[Path] = []
            with patch("construction.quarter_runner.history_quarter_review.run_review", side_effect=self.passed_review(calls)):
                first = run(self.correction_args(values, root, document))
            first_identity = first["final_checkpoint_identity"]
            values["out"] = root / "corrected_again"
            with patch("construction.quarter_runner.history_quarter_review.run_review", side_effect=self.passed_review(calls)):
                second = run(self.correction_args(values, root, document))
            self.assertEqual(first_identity, second["final_checkpoint_identity"])
            plan = json.loads((root / "corrected" / "windows" / "2025-06-24_to_2025-06-30" / "plan.json").read_text())
            self.assertEqual([row["session_id"] for row in plan["sessions"]], ["s2"])
            self.assertEqual(calls, [root / "corrected", root / "corrected_again"])

    def test_partial_or_durable_removal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sessions = [
                self.session("s1", "c1", thread_id="t1", open_thread=True),
                self.session("s2", "c2", thread_id="t1", continues=["c1"]),
            ]
            values, _ = self.fixture(root, sessions)
            with self.assertRaisesRegex(ValueError, "partial-thread"):
                run(self.correction_args(values, root, {"replacements": [], "removals": ["s1"]}))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            values, _ = self.fixture(root, [self.session("s1", "c1", fact=True)])
            with self.assertRaisesRegex(ValueError, "durable fact"):
                run(self.correction_args(values, root, {"replacements": [], "removals": ["s1"]}))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = self.session("s1", "c1")
            session["development_ids"] = ["event:accepted"]
            values, _ = self.fixture(root, [session])
            with self.assertRaisesRegex(ValueError, "accepted quarter development"):
                run(self.correction_args(values, root, {"replacements": [], "removals": ["s1"]}))

    def test_atomic_complete_thread_removal_is_permitted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sessions = [
                self.session("s1", "c1", thread_id="t1", open_thread=True),
                self.session("s2", "c2", thread_id="t1", continues=["c1"]),
            ]
            values, _ = self.fixture(root, sessions)
            calls: list[Path] = []
            with patch("construction.quarter_runner.history_quarter_review.run_review", side_effect=self.passed_review(calls)):
                manifest = run(self.correction_args(values, root, {"replacements": [], "removals": ["s1", "s2"]}))
            self.assertEqual(manifest["totals"]["sessions"], 0)
            self.assertTrue((root / "corrected" / "final_checkpoint").is_dir())

    def test_complete_thread_removal_may_drop_new_temporary_current_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sessions = [
                self.session("s1", "c1", thread_id="t1", open_thread=True),
                self.session("s2", "c2", thread_id="t1", continues=["c1"]),
            ]
            values, _ = self.fixture(root, sessions)
            self.set_current_state_changes(root, {"c1": [self.current_state_change("home:fan")]})
            with patch("construction.quarter_runner.history_quarter_review.run_review", side_effect=self.passed_review([])):
                run(self.correction_args(values, root, {"replacements": [], "removals": ["s1", "s2"]}))
            state = json.loads((root / "corrected" / "final_checkpoint" / "session_purposes.json").read_text())
            self.assertEqual(state, [])

    def test_current_state_removal_rejects_partial_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sessions = [
                self.session("s1", "c1", thread_id="t1", open_thread=True),
                self.session("s2", "c2", thread_id="t1", continues=["c1"]),
            ]
            values, _ = self.fixture(root, sessions)
            self.set_current_state_changes(root, {"c1": [self.current_state_change("home:fan")]})
            with self.assertRaisesRegex(ValueError, "partial-thread removal"):
                run(self.correction_args(values, root, {"replacements": [], "removals": ["s1"]}))

    def test_standalone_current_state_removal_rejects_without_explicit_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sessions = [self.session("s0", "c0"), self.session("s1", "c1")]
            values, _ = self.fixture(root, sessions)
            self.set_current_state_changes(root, {
                "c0": [self.current_state_change("home:fan")],
                "c1": [self.current_state_change("home:fan")],
            })
            with self.assertRaisesRegex(ValueError, "current_state_changes require complete-thread removal"):
                run(self.correction_args(values, root, {"replacements": [], "removals": ["s1"]}))

    def test_all_standalone_changes_for_new_current_state_may_be_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sessions = [self.session("s0", "c0"), self.session("s1", "c1")]
            values, _ = self.fixture(root, sessions)
            self.set_current_state_changes(root, {
                "c0": [self.current_state_change("home:fan")],
                "c1": [self.current_state_change("home:fan")],
            })
            with patch(
                "construction.quarter_runner.history_quarter_review.run_review",
                side_effect=self.passed_review([]),
            ):
                manifest = run(self.correction_args(values, root, {
                    "replacements": [],
                    "removals": ["s0", "s1"],
                }))
            self.assertEqual(manifest["totals"]["sessions"], 0)
            purposes = json.loads(
                (root / "corrected" / "final_checkpoint" / "session_purposes.json").read_text()
            )
            self.assertEqual(purposes, [])

    def test_standalone_current_state_removal_accepts_explicit_retained_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sessions = [self.session("s0", "c0"), self.session("s1", "c1")]
            values, _ = self.fixture(root, sessions)
            self.set_current_state_changes(root, {
                "c0": [self.current_state_change("local_customer_csv_containment")],
                "c1": [self.current_state_change("local_customer_csv_containment")],
            })
            final_statement = "Local customer CSV files remain contained on the approved device."
            replacement = {
                "session_id": "s0",
                "messages": [final_statement],
                "purpose": "Record the final local CSV containment state.",
                "app_operations": [],
                "current_state_changes": [{
                    "key": "local_customer_csv_containment",
                    "subject_ids": ["person:morgan"],
                    "statement": final_statement,
                }],
            }
            with patch(
                "construction.quarter_runner.history_quarter_review.run_review",
                side_effect=self.passed_review([]),
            ):
                run(self.correction_args(values, root, {
                    "replacements": [replacement],
                    "removals": ["s1"],
                }))
            purposes = json.loads(
                (root / "corrected" / "final_checkpoint" / "session_purposes.json").read_text()
            )
            self.assertEqual(len(purposes), 1)
            self.assertEqual(purposes[0]["session_id"], "s0")
            self.assertEqual(
                purposes[0]["current_state_changes"],
                replacement["current_state_changes"],
            )

    def test_current_state_removal_rejects_key_changed_by_retained_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sessions = [
                self.session("s1", "c1", thread_id="t1", open_thread=True),
                self.session("s2", "c2", thread_id="t1", continues=["c1"]),
                self.session("s3", "c3"),
            ]
            values, _ = self.fixture(root, sessions)
            self.set_current_state_changes(root, {
                "c1": [self.current_state_change("home:fan")],
                "c3": [self.current_state_change("home:fan")],
            })
            replacement_without_state = {
                "session_id": "s3",
                "messages": ["Corrected retained message."],
                "purpose": "Corrected retained purpose.",
                "app_operations": [],
            }
            with self.assertRaisesRegex(ValueError, "retained sessions change current-state key home:fan"):
                run(self.correction_args(values, root, {
                    "replacements": [replacement_without_state],
                    "removals": ["s1", "s2"],
                }))

    def test_current_state_removal_accepts_final_state_owned_by_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sessions = [
                self.session("s0", "c0"),
                self.session("s3", "c3"),
                self.session("s1", "c1", thread_id="t1", open_thread=True),
                self.session("s2", "c2", thread_id="t1", continues=["c1"]),
            ]
            values, _ = self.fixture(root, sessions)
            self.set_current_state_changes(root, {
                "c0": [self.current_state_change("home_fan")],
                "c1": [self.current_state_change("home_fan")],
            })
            final_statement = "The retained history records the final fan state."
            replacement = {
                "session_id": "s3",
                "messages": [final_statement],
                "purpose": "Record the final fan state.",
                "app_operations": [],
                "current_state_changes": [{
                    "key": "home_fan",
                    "subject_ids": ["person:morgan"],
                    "statement": final_statement,
                }],
            }
            with patch(
                "construction.quarter_runner.history_quarter_review.run_review",
                side_effect=self.passed_review([]),
            ):
                run(self.correction_args(values, root, {
                    "replacements": [replacement],
                    "removals": ["s1", "s2"],
                }))
            purposes = json.loads(
                (root / "corrected" / "final_checkpoint" / "session_purposes.json").read_text()
            )
            by_session_id = {row["session_id"]: row for row in purposes}
            self.assertEqual(
                by_session_id["s3"]["current_state_changes"],
                replacement["current_state_changes"],
            )

    def test_current_state_removal_rejects_retained_reference_to_removed_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sessions = [
                self.session("s1", "c1", thread_id="t1", open_thread=True),
                self.session("s2", "c2", thread_id="t1", continues=["c1"]),
                self.session("s3", "c3", thread_id="t2", continues=["c2"]),
            ]
            values, _ = self.fixture(root, sessions)
            self.set_current_state_changes(root, {"c1": [self.current_state_change("home:fan")]})
            with self.assertRaisesRegex(ValueError, "retained session s3 references"):
                run(self.correction_args(values, root, {"replacements": [], "removals": ["s1", "s2"]}))

    def test_current_state_removal_rejects_key_present_at_quarter_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sessions = [
                self.session("s1", "c1", thread_id="t1", open_thread=True),
                self.session("s2", "c2", thread_id="t1", continues=["c1"]),
            ]
            start_state = self.state()
            start_state["session_purposes"] = [{
                "session_id": "before-quarter", "narrative_date": "2025-06-01T09:00:00-07:00",
                "current_state_changes": [self.current_state_change("home:fan")],
            }]
            values, _ = self.fixture(root, sessions, starting_state=start_state)
            self.set_current_state_changes(root, {"c1": [self.current_state_change("home:fan")]})
            with self.assertRaisesRegex(ValueError, "current-state key home:fan existed before this quarter"):
                run(self.correction_args(values, root, {"replacements": [], "removals": ["s1", "s2"]}))

    def test_malformed_corrections_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            values, _ = self.fixture(root, [self.session("s1", "c1")])
            malformed = {"replacements": [{
                "session_id": "s1", "messages": ["x"], "purpose": "x",
                "app_operations": [{"tool": "nope", "args": {}}],
            }], "removals": []}
            with self.assertRaisesRegex(ValueError, "malformed corrected app operations"):
                run(self.correction_args(values, root, malformed))
            with self.assertRaisesRegex(ValueError, "exactly replacements and removals"):
                run(self.correction_args(values, root, {"replacements": [], "removals": [], "extra": True}))

    def test_replay_failure_records_the_actual_window_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            values, _ = self.fixture(root, [self.session("s1", "c1")])
            with patch(
                "construction.history_quarter_corrections.build_history_window.project_app_operations",
                side_effect=RuntimeError("replay failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "replay failed"):
                    run(self.correction_args(values, root, {"replacements": [], "removals": []}))
            failed = json.loads((root / "corrected" / "manifest.json").read_text())
            self.assertEqual(failed["stage"], "window_01_2025-06-24_to_2025-06-30")
            self.assertEqual(failed["model_calls"]["actual"], 0)


if __name__ == "__main__":
    unittest.main()
