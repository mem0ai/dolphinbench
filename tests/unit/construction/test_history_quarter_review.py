from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from construction.checkpoints import save_checkpoint
from construction.history_quarter_review import REVIEW_MODEL, build_review_request, render_review_input, run_review, validate_review_response
from construction.runtime_inputs import exact_persona_tools


class FakeClient:
    model = REVIEW_MODEL


class HistoryQuarterReviewTest(unittest.TestCase):
    def make_checkpoints(
        self,
        root: Path,
        *,
        include_starting_records: bool = False,
        include_read_result: bool = False,
    ) -> tuple[Path, Path, list[dict[str, str]]]:
        starting = root / "starting_checkpoint"
        final = root / "final_checkpoint"
        old_session = {
            "id": "old-1",
            "narrative_date": "2025-03-29T09:00:00-07:00",
            "messages": ["The earlier accepted message."],
        }
        new_session = {
            "id": "generated-1",
            "narrative_date": "2025-04-02T10:00:00-07:00",
            "messages": ["Please send the approved note to Devon."],
        }
        starting_state = {
            "history": [old_session],
            "session_purposes": [
                {
                    "session_id": "old-1",
                    "narrative_date": old_session["narrative_date"],
                    "purpose": "Earlier accepted purpose.",
                }
            ],
            "entities": [
                {"id": "person:morgan", "name": "Morgan"},
                {"id": "person:devon", "name": "Devon"},
            ],
            "facts": [
                {
                    "id": 1,
                    "statement": "Morgan works at Scaffold.",
                    "applies_when": "Always.",
                    "source_session_ids": ["old-1"],
                    "subjects": ["person:morgan"],
                    "supersedes": [],
                    "unused_review_field": "omit me",
                }
            ],
            "app_state": {"calendar": []},
            "unfinished_threads": [],
        }
        if include_starting_records:
            starting_state["app_state"] = {
                "calendar": [
                    {
                        "id": "event-1",
                        "title": "Approved note review",
                        "start": "2025-04-03T10:00:00-07:00",
                        "end": "2025-04-03T10:30:00-07:00",
                    },
                    {
                        "id": "event-unrelated",
                        "title": "Unrelated event",
                    },
                ],
                "docs": [
                    {
                        "id": "doc-1",
                        "title": "Devon note",
                        "body": "Original note body.",
                    },
                    {
                        "id": "doc-unrelated",
                        "title": "Unrelated document",
                        "body": "Should not be shown.",
                    },
                ],
                "deploys": [
                    {
                        "deploy_id": "deploy-1",
                        "service": "api",
                        "version": "v1",
                        "status": "completed",
                    }
                ],
            }
        final_state = {
            **starting_state,
            "history": [old_session, new_session],
            "session_purposes": [
                *starting_state["session_purposes"],
                {
                    "session_id": "generated-1",
                    "narrative_date": new_session["narrative_date"],
                    "purpose": "Send the approved note to Devon.",
                    "subject_ids": ["person:morgan", "person:devon"],
                },
            ],
            "facts": [
                *starting_state["facts"],
                {
                    "id": 2,
                    "construction_key": "devon_note_approved",
                    "statement": "Morgan approved the note for Devon.",
                    "applies_when": "After April 2, 2025.",
                    "source_session_ids": ["generated-1"],
                    "subjects": ["person:morgan", "person:devon"],
                    "supersedes": [],
                },
            ],
        }
        operation_results = [
            {
                "session_id": "generated-1",
                "operation": {
                    "tool": "update_runbook_entry",
                    "args": {"entry_id": "rb-devon", "body": "Approved."},
                },
                "result": {
                    "status": "completed",
                    "ok": True,
                    "id": "runbook-result",
                    "url": "https://example.test/runbook-result",
                    "discarded": "omit me",
                },
                "replay_epoch_ms": 1,
            }
        ]
        if include_starting_records:
            operation_results.extend(
                [
                    {
                        "session_id": "generated-1",
                        "operation": {
                            "tool": "update_doc",
                            "args": {
                                "doc_id": "doc-1",
                                "body": "First addition.",
                                "mode": "append",
                            },
                        },
                        "result": {"ok": True},
                        "replay_epoch_ms": 2,
                    },
                    {
                        "session_id": "generated-1",
                        "operation": {
                            "tool": "update_doc",
                            "args": {
                                "doc_id": "doc-1",
                                "body": "Second addition.",
                                "mode": "append",
                            },
                        },
                        "result": {"ok": True},
                        "replay_epoch_ms": 3,
                    },
                    {
                        "session_id": "generated-1",
                        "operation": {
                            "tool": "update_calendar_event",
                            "args": {
                                "event_id": "event-1",
                                "title": "Approved note review moved",
                            },
                        },
                        "result": {"ok": True},
                        "replay_epoch_ms": 4,
                    },
                    {
                        "session_id": "generated-1",
                        "operation": {
                            "tool": "rollback_deploy",
                            "args": {
                                "service": "api",
                                "deploy_id": "deploy-1",
                            },
                        },
                        "result": {"ok": True},
                        "replay_epoch_ms": 5,
                    },
                ]
            )
        if include_read_result:
            operation_results.append(
                {
                    "session_id": "generated-1",
                    "operation": {
                        "tool": "list_inbox",
                        "args": {"folder": "inbox", "limit": 100},
                    },
                    "result": [
                        {"id": "email-1", "subject": "Keep this read result"}
                    ],
                    "replay_epoch_ms": 6,
                }
            )
        save_checkpoint(
            path=starting,
            persona="alex",
            week={"week_id": "before-quarter", "end_date": "2025-03-31"},
            previous_hash="previous",
            inputs={},
            state=starting_state,
            operation_results=[],
            covered_events=[],
            system_sha256="system",
            identity_version=2,
        )
        save_checkpoint(
            path=final,
            persona="alex",
            week={"week_id": "2025_q2", "end_date": "2025-06-30"},
            previous_hash="previous",
            inputs={},
            state=final_state,
            operation_results=operation_results,
            covered_events=[],
            system_sha256="system",
            identity_version=2,
        )
        window_output = root / "window"
        window_output.mkdir()
        (window_output / "plan.json").write_text(
            json.dumps(
                {
                    "period_start": "2025-04-01",
                    "period_end": "2025-04-07",
                    "sessions": [
                        {
                            "session_id": "generated-1",
                            "subject_ids": ["person:morgan", "person:devon"],
                            "purpose": "Send the approved note to Devon.",
                            "new_facts": [{"key": "devon_note_approved"}],
                        }
                    ],
                }
            )
            + "\n"
        )
        records = [
            {
                "start_date": "2025-04-01",
                "end_date": "2025-04-07",
                "output": str(window_output),
            }
        ]
        return starting, final, records

    def request(self, root: Path) -> dict:
        starting, final, records = self.make_checkpoints(root)
        return self.build_request(starting, final, records)

    def build_request(
        self,
        starting: Path,
        final: Path,
        records: list[dict[str, str]],
    ) -> dict:
        return build_review_request(
            config={
                "persona": "alex",
                "quarter_id": "2025_q2",
                "quarter_start": "2025-04-01",
                "quarter_end": "2025-06-30",
            },
            quarter_plan={
                "plan": {
                    "period_start": "2025-04-01",
                    "period_end": "2025-06-30",
                    "protected_changes": [],
                    "events": [{"id": "event-1", "description": "Send the note."}],
                }
            },
            starting_checkpoint=starting,
            final_checkpoint=final,
            window_records=records,
        )

    def test_build_request_includes_referenced_starting_records_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            starting, final, records = self.make_checkpoints(
                root, include_starting_records=True
            )
            request = self.build_request(starting, final, records)
            app_records = request["starting_app_records"]

            self.assertEqual(
                [(row["state_collection"], row["record_id"]) for row in app_records],
                [("calendar", "event-1"), ("docs", "doc-1")],
            )
            self.assertEqual(
                sum(row["record_id"] == "doc-1" for row in app_records), 1
            )
            self.assertEqual(
                sum(row["record_id"] == "event-1" for row in app_records), 1
            )
            self.assertTrue(all("referenced_by" not in row for row in app_records))
            rendered = render_review_input(request)
            section = "STARTING APP RECORDS DIRECTLY MODIFIED BY GENERATED OPERATIONS"
            self.assertLess(rendered.index(section), rendered.index("GENERATED WEEK"))
            self.assertIn("Original note body.", rendered)
            self.assertIn("Approved note review", rendered)
            self.assertNotIn("doc-unrelated", rendered)
            self.assertNotIn("event-unrelated", rendered)
            self.assertNotIn(
                "deploy-1", {row["record_id"] for row in app_records}
            )

    def test_rendered_input_is_chronological_and_contains_required_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            request = self.request(Path(temp))
            rendered = render_review_input(request)
            self.assertLess(rendered.index("old-1"), rendered.index("generated-1"))
            self.assertIn("Morgan works at Scaffold.", rendered)
            self.assertIn("The earlier accepted message.", rendered)
            self.assertIn("Please send the approved note to Devon.", rendered)
            self.assertIn("person:devon", rendered)
            self.assertIn("Morgan approved the note for Devon.", rendered)
            self.assertIn("update_runbook_entry", rendered)
            self.assertIn("creating it if it does not already exist", rendered)
            self.assertIn("required_arguments:", rendered)
            self.assertIn("writes_state_keys:", rendered)
            self.assertIn(
                "GENERATED WEEK 2025-04-01_to_2025-04-07 (2025-04-01 through 2025-04-07)",
                rendered,
            )
            self.assertNotIn("app_state:", rendered)
            generated = request["generated_week_sections"][0]["sessions"][0]
            self.assertEqual(
                set(generated),
                {
                    "session_id",
                    "narrative_date",
                    "purpose",
                    "messages",
                    "facts_established_by_this_session",
                    "app_operations",
                },
            )
            self.assertEqual(
                generated["facts_established_by_this_session"],
                [
                    {
                        "id": 2,
                        "construction_key": "devon_note_approved",
                        "statement": "Morgan approved the note for Devon.",
                    }
                ],
            )
            self.assertNotIn("fact_ids_and_statements", generated)
            self.assertEqual(
                request["starting_current_facts"],
                [
                    {
                        "id": 1,
                        "statement": "Morgan works at Scaffold.",
                        "applies_when": "Always.",
                        "subjects": ["person:morgan"],
                        "supersedes": [],
                    }
                ],
            )
            self.assertEqual(
                request["accepted_quarter_plan"],
                {
                    "plan": {
                        "period_start": "2025-04-01",
                        "period_end": "2025-06-30",
                        "protected_changes": [],
                        "events": [{"id": "event-1", "description": "Send the note."}],
                    }
                },
            )
            self.assertEqual(
                generated["app_operations"][0]["result"],
                {
                    "status": "completed",
                    "ok": True,
                    "id": "runbook-result",
                    "url": "https://example.test/runbook-result",
                },
            )
            self.assertEqual(
                request["used_tool_contracts"],
                [
                    {
                        "tool": "update_runbook_entry",
                        **exact_persona_tools("alex")["update_runbook_entry"],
                    }
                ],
            )
            self.assertNotIn("send_slack_dm", rendered)

    def test_read_operation_results_are_preserved_in_full(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            starting, final, records = self.make_checkpoints(
                root, include_read_result=True
            )

            request = self.build_request(starting, final, records)
            operations = request["generated_week_sections"][0]["sessions"][0][
                "app_operations"
            ]
            read_operation = next(
                row for row in operations if row["operation"]["tool"] == "list_inbox"
            )
            self.assertEqual(
                read_operation["result"],
                [{"id": "email-1", "subject": "Keep this read result"}],
            )

    def test_rendered_input_shows_empty_operation_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            request = self.request(Path(temp))
            operation = request["generated_week_sections"][0]["sessions"][0][
                "app_operations"
            ][0]

            operation["result"] = []
            self.assertRegex(render_review_input(request), r"result:\n\s+\[\]")

            operation["result"] = {}
            self.assertRegex(render_review_input(request), r"result:\n\s+\{\}")

    def test_run_review_saves_request_prompt_input_response_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = self.request(root)
            rendered = render_review_input(request)
            response = {
                "passed": True,
                "findings": [],
                "week_results": [
                    {"week_id": "2025-04-01_to_2025-04-07", "passed": True}
                ],
                "cross_quarter_findings": [],
            }
            with patch(
                "construction.history_quarter_review.cached_client_complete",
                return_value=response,
            ) as complete:
                result = run_review(
                    output_dir=root / "review",
                    request=request,
                    rendered_input=rendered,
                    client=FakeClient(),
                )
            review_dir = root / "review"
            for name in (
                "history_quarter_review_request.json",
                "history_quarter_review_system.txt",
                "history_quarter_review_input.txt",
                "history_quarter_review_response.json",
                "history_quarter_review_validation.json",
            ):
                self.assertTrue((review_dir / name).is_file(), name)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(complete.call_args.args[2], rendered)

    def test_validation_requires_findings_to_name_generated_session(self) -> None:
        passed = validate_review_response(
            {
                "passed": True,
                "findings": [],
                "week_results": [
                    {"week_id": "2025-04-01_to_2025-04-07", "passed": True}
                ],
                "cross_quarter_findings": [],
            },
            generated_session_ids={"generated-1"},
            generated_session_ids_by_week={"2025-04-01_to_2025-04-07": {"generated-1"}},
            expected_week_ids=["2025-04-01_to_2025-04-07"],
        )
        self.assertEqual(passed["valid"], True)
        failed = validate_review_response(
            {
                "passed": False,
                "findings": [
                    {"session_ids": ["generated-1"], "problem": "The message is contradictory."}
                ],
                "week_results": [
                    {"week_id": "2025-04-01_to_2025-04-07", "passed": False}
                ],
                "cross_quarter_findings": [],
            },
            generated_session_ids={"generated-1"},
            generated_session_ids_by_week={"2025-04-01_to_2025-04-07": {"generated-1"}},
            expected_week_ids=["2025-04-01_to_2025-04-07"],
        )
        self.assertEqual(failed, {"valid": True, "passed": False, "errors": [], "finding_count": 1})

    def test_validation_rejects_omitted_or_duplicate_week_results(self) -> None:
        expected_week_ids = ["2025-04-01_to_2025-04-07", "2025-04-08_to_2025-04-14"]
        common = {"passed": True, "findings": [], "cross_quarter_findings": []}
        omitted = validate_review_response(
            {
                **common,
                "week_results": [{"week_id": expected_week_ids[0], "passed": True}],
            },
            generated_session_ids={"generated-1"},
            generated_session_ids_by_week={
                expected_week_ids[0]: {"generated-1"}, expected_week_ids[1]: set()
            },
            expected_week_ids=expected_week_ids,
        )
        self.assertFalse(omitted["valid"])
        self.assertTrue(
            any(error.startswith("week_results omits week sections") for error in omitted["errors"])
        )

        duplicated = validate_review_response(
            {
                **common,
                "week_results": [
                    {"week_id": expected_week_ids[0], "passed": True},
                    {"week_id": expected_week_ids[0], "passed": True},
                    {"week_id": expected_week_ids[1], "passed": True},
                ],
            },
            generated_session_ids={"generated-1"},
            generated_session_ids_by_week={
                expected_week_ids[0]: {"generated-1"}, expected_week_ids[1]: set()
            },
            expected_week_ids=expected_week_ids,
        )
        self.assertFalse(duplicated["valid"])
        self.assertTrue(
            any(error.startswith("week_results duplicates week sections") for error in duplicated["errors"])
        )

    def test_validation_rejects_week_result_finding_mismatches(self) -> None:
        week_id = "2025-04-01_to_2025-04-07"
        common = {
            "passed": False,
            "findings": [
                {"session_ids": ["generated-1"], "problem": "The message is contradictory."}
            ],
            "cross_quarter_findings": [],
        }
        marked_passed = validate_review_response(
            {**common, "week_results": [{"week_id": week_id, "passed": True}]},
            generated_session_ids={"generated-1"},
            generated_session_ids_by_week={week_id: {"generated-1"}},
            expected_week_ids=[week_id],
        )
        self.assertFalse(marked_passed["valid"])
        self.assertTrue(
            any("must be false to match top-level findings" in error for error in marked_passed["errors"])
        )

        marked_failed = validate_review_response(
            {
                "passed": True,
                "findings": [],
                "week_results": [{"week_id": week_id, "passed": False}],
                "cross_quarter_findings": [],
            },
            generated_session_ids={"generated-1"},
            generated_session_ids_by_week={week_id: {"generated-1"}},
            expected_week_ids=[week_id],
        )
        self.assertFalse(marked_failed["valid"])
        self.assertTrue(
            any("must be true to match top-level findings" in error for error in marked_failed["errors"])
        )


if __name__ == "__main__":
    unittest.main()
