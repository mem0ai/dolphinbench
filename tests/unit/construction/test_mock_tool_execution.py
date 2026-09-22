from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from construction.runtime_inputs import exact_persona_tools, readable_app_state


ROOT = Path(__file__).resolve().parents[3]


class MockToolExecutionTest(unittest.TestCase):
    def run_tool(
        self,
        *,
        state: dict,
        tool: str,
        tool_args: dict,
        persona: str = "morgan",
        session_id: str = "",
        narrative_datetime: str = "",
        replay_epoch_ms: int | None = None,
    ) -> tuple[dict, dict, list[dict]]:
        with TemporaryDirectory() as directory:
            temp_dir = Path(directory)
            state_path = temp_dir / "state.json"
            log_path = temp_dir / "calls.jsonl"
            state_path.write_text(json.dumps(state))
            fake_mcp = temp_dir / "mcp" / "server"
            fake_mcp.mkdir(parents=True)
            (temp_dir / "mcp" / "__init__.py").write_text("")
            (fake_mcp / "__init__.py").write_text("")
            (fake_mcp / "fastmcp.py").write_text(
                "class FastMCP:\n"
                "    def __init__(self, *args, **kwargs):\n"
                "        pass\n"
                "    def tool(self):\n"
                "        return lambda function: function\n"
            )
            command = [
                sys.executable,
                "-m",
                "construction.execute_mock_tool",
                "--persona",
                persona,
                "--state",
                str(state_path),
                "--log",
                str(log_path),
                "--tool",
                tool,
                "--args",
                json.dumps(tool_args),
                "--session-id",
                session_id,
                "--narrative-datetime",
                narrative_datetime,
            ]
            if replay_epoch_ms is not None:
                command.extend(["--replay-epoch-ms", str(replay_epoch_ms)])
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.pathsep.join(
                [str(temp_dir), str(ROOT), environment.get("PYTHONPATH", "")]
            )
            completed = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            saved_state = json.loads(state_path.read_text())
            logs = [json.loads(line) for line in log_path.read_text().splitlines()]
            return result, saved_state, logs

    def test_replay_clock_reproduces_generated_id(self) -> None:
        result, saved_state, _ = self.run_tool(
            state={"sent_emails": []},
            tool="send_email",
            tool_args={
                "to": "devon@example.com",
                "subject": "Status",
                "body": "The update is ready.",
            },
            session_id="session_1",
            narrative_datetime="2023-05-05T09:27:00-07:00",
            replay_epoch_ms=1784911047881,
        )

        self.assertEqual(result["id"], "sent_1784911047881")
        self.assertEqual(saved_state["sent_emails"][0]["id"], "sent_1784911047881")

    def test_slack_message_mutates_the_contract_state_key(self) -> None:
        contract = exact_persona_tools("morgan")["send_slack_message"]
        declared_keys = contract["state_effect"]["writes_state_keys"]
        self.assertEqual(declared_keys, ["slack_log"])

        result, saved_state, _ = self.run_tool(
            state={"slack_log": []},
            tool="send_slack_message",
            tool_args={
                "channel": "#eng-team",
                "message": "The contract test passed.",
            },
            session_id="contract_test",
            narrative_datetime="2026-08-25T12:00:00Z",
            replay_epoch_ms=1787659200000,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(len(saved_state[declared_keys[0]]), 1)
        self.assertEqual(saved_state[declared_keys[0]][0]["channel"], "#eng-team")

    def test_missing_riley_analytics_records_return_not_found(self) -> None:
        cases = [
            ("query_event_funnel", {"funnel_name": "missing"}, {"funnels": []}),
            (
                "query_retention_cohort",
                {"cohort_id": "missing"},
                {"retention_cohorts": []},
            ),
            (
                "get_churn_metrics",
                {"tier": "mid_seg", "asof_date": "2026-02-28"},
                {"churn_metrics": {}},
            ),
            (
                "get_arr_segments",
                {"asof_date": "2026-02-28"},
                {"arr_segments": []},
            ),
            (
                "query_experiment_results",
                {"experiment_id": "missing"},
                {"experiment_results": []},
            ),
        ]

        for tool, tool_args, state in cases:
            with self.subTest(tool=tool):
                result, _, _ = self.run_tool(
                    state=state,
                    tool=tool,
                    tool_args=tool_args,
                    persona="riley",
                )
                self.assertEqual(result["error"], "NOT_FOUND")

    def test_mrr_exact_date_does_not_fall_back_to_another_snapshot(self) -> None:
        result, _, _ = self.run_tool(
            state={
                "mrr_snapshots": [
                    {"asof_date": "2026-01-31", "total_mrr": 4000000.0}
                ]
            },
            tool="get_mrr_metrics",
            tool_args={"asof_date": "2026-02-28"},
            persona="riley",
        )

        self.assertEqual(result["error"], "NOT_FOUND")

    def test_deploy_service_records_completed(self) -> None:
        result, saved_state, _ = self.run_tool(
            state={"deploys": []},
            tool="deploy_service",
            tool_args={
                "service": "metrics-router",
                "version": "v1.42.0",
                "env": "staging",
            },
            persona="alex",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(saved_state["deploys"][0]["status"], "completed")

    def test_update_doc_replaces_the_existing_body(self) -> None:
        result, saved_state, _ = self.run_tool(
            state={
                "docs": [
                    {
                        "id": "doc_123",
                        "title": "Working note",
                        "body": "Old body",
                        "folder": "Growth",
                    }
                ]
            },
            tool="update_doc",
            tool_args={"doc_id": "doc_123", "body": "Final body"},
            persona="riley",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(saved_state["docs"][0]["body"], "Final body")
        self.assertEqual(saved_state["docs"][0]["title"], "Working note")
        self.assertEqual(saved_state["docs"][0]["folder"], "Growth")

    def test_update_doc_appends_only_new_text_with_clean_spacing(self) -> None:
        contract = exact_persona_tools("riley")["update_doc"]
        self.assertIn("mode", contract["arguments"])
        self.assertNotIn("mode", contract["required_arguments"])

        result, saved_state, _ = self.run_tool(
            state={
                "docs": [
                    {
                        "id": "doc_123",
                        "title": "Working note",
                        "body": "Existing body\n\n",
                        "folder": "Growth",
                    }
                ]
            },
            tool="update_doc",
            tool_args={
                "doc_id": "doc_123",
                "body": "\nNew section",
                "mode": "append",
            },
            persona="riley",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            saved_state["docs"][0]["body"], "Existing body\n\nNew section"
        )

    def test_update_doc_rejects_unknown_mode(self) -> None:
        state = {"docs": [{"id": "doc_123", "body": "Existing body"}]}
        result, saved_state, _ = self.run_tool(
            state=state,
            tool="update_doc",
            tool_args={"doc_id": "doc_123", "body": "New body", "mode": "merge"},
            persona="riley",
        )

        self.assertEqual(result["error"], "INVALID_UPDATE_MODE")
        self.assertEqual(saved_state, state)

    def test_review_pr_rejects_merged_and_closed_pull_requests(self) -> None:
        cases = [
            ("merged", "approve"),
            ("closed", "comment"),
        ]
        for status, decision in cases:
            with self.subTest(status=status, decision=decision):
                state = {
                    "prs": [{"id": "metrics-router#412", "status": status}],
                    "pr_reviews": [],
                }
                result, saved_state, _ = self.run_tool(
                    state=state,
                    tool="review_pr",
                    tool_args={
                        "pr_id": "metrics-router#412",
                        "decision": decision,
                        "body": "Review body",
                    },
                    persona="alex",
                )

                self.assertEqual(result["error"], "PR_NOT_OPEN")
                self.assertIn(status, result["instruction"])
                self.assertEqual(saved_state, state)

    def test_calendar_event_requires_valid_positive_duration(self) -> None:
        contract = exact_persona_tools("riley")["create_calendar_event"]
        self.assertIn(
            "end must be later than start.",
            contract["state_effect"]["hard_requirements"],
        )
        state = {"calendar": []}
        invalid_cases = [
            (
                "malformed",
                "2026-04-17T15:00:00-07:00",
                "not-a-datetime+00:00",
                "INVALID_DATETIME",
            ),
            (
                "equal",
                "2026-04-17T15:00:00-07:00",
                "2026-04-17T15:00:00-07:00",
                "INVALID_EVENT_RANGE",
            ),
            (
                "reversed",
                "2026-04-17T16:00:00-07:00",
                "2026-04-17T15:00:00-07:00",
                "INVALID_EVENT_RANGE",
            ),
        ]
        for name, start, end, expected_error in invalid_cases:
            with self.subTest(name=name):
                result, saved_state, _ = self.run_tool(
                    state=state,
                    tool="create_calendar_event",
                    tool_args={"title": "Calendar guard", "start": start, "end": end},
                    persona="riley",
                )
                self.assertEqual(result["error"], expected_error)
                self.assertEqual(saved_state, state)

        result, saved_state, _ = self.run_tool(
            state=state,
            tool="create_calendar_event",
            tool_args={
                "title": "Calendar guard",
                "start": "2026-04-17T15:00:00-07:00",
                "end": "2026-04-17T15:30:00-07:00",
            },
            persona="riley",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(saved_state["calendar"]), 1)

    def test_calendar_event_rejects_exact_time_with_shared_attendee(self) -> None:
        state = {
            "calendar": [
                {
                    "id": "evt_existing",
                    "title": "Existing event",
                    "start": "2026-04-17T15:00:00-07:00",
                    "end": "2026-04-17T15:30:00-07:00",
                    "attendees": ["riley", "owen"],
                }
            ]
        }
        result, saved_state, _ = self.run_tool(
            state=state,
            tool="create_calendar_event",
            tool_args={
                "title": "Conflicting event",
                "start": "2026-04-17T15:00:00-07:00",
                "end": "2026-04-17T15:30:00-07:00",
                "attendees": ["riley", "sam"],
            },
            persona="riley",
        )

        self.assertEqual(result["error"], "CALENDAR_EVENT_CONFLICT")
        self.assertIn("evt_existing", result["instruction"])
        self.assertEqual(saved_state, state)

    def test_calendar_event_allows_partial_or_unshared_same_time_events(self) -> None:
        state = {
            "calendar": [
                {
                    "id": "evt_existing",
                    "title": "Existing event",
                    "start": "2026-04-17T15:00:00-07:00",
                    "end": "2026-04-17T15:30:00-07:00",
                    "attendees": ["riley"],
                }
            ]
        }
        for title, start, end, attendees in (
            (
                "Partial overlap",
                "2026-04-17T15:15:00-07:00",
                "2026-04-17T15:45:00-07:00",
                ["riley"],
            ),
            (
                "No shared attendee",
                "2026-04-17T15:00:00-07:00",
                "2026-04-17T15:30:00-07:00",
                ["owen"],
            ),
        ):
            with self.subTest(title=title):
                result, saved_state, _ = self.run_tool(
                    state=state,
                    tool="create_calendar_event",
                    tool_args={
                        "title": title,
                        "start": start,
                        "end": end,
                        "attendees": attendees,
                    },
                    persona="riley",
                )

                self.assertTrue(result["ok"])
                self.assertEqual(len(saved_state["calendar"]), 2)

    def test_alex_and_riley_expose_send_sms(self) -> None:
        for persona in ("alex", "riley"):
            with self.subTest(persona=persona):
                contract = exact_persona_tools(persona)["send_sms"]
                self.assertEqual(
                    contract["state_effect"]["writes_state_keys"], ["sms_log"]
                )

    def test_list_calendar_events_returns_only_stable_fields(self) -> None:
        contract = exact_persona_tools("riley")["list_calendar_events"]
        self.assertIn(
            "only id, title, start, end, and attendees, plus body and location when present",
            " ".join(contract["description"].split()),
        )
        self.assertEqual(
            contract["state_effect"]["returns"],
            "Events on the requested ISO date with only id, title, start, end, and attendees, plus body and location when present.",
        )
        state = {
            "calendar": [
                {
                    "id": "evt_current",
                    "title": "Current event",
                    "start": "2026-04-17T09:00:00-07:00",
                    "end": "2026-04-17T09:30:00-07:00",
                    "attendees": ["riley", "owen"],
                    "body": "Keep this accepted body.",
                    "location": "Room 3",
                    "notes": "must not be visible",
                    "recurring": "weekly:friday",
                },
                {
                    "event_id": "evt_legacy",
                    "title": "Legacy event",
                    "start": "2026-04-17T10:00:00-07:00",
                    "end": "2026-04-17T10:30:00-07:00",
                    "attendees": ["alex", "hema"],
                    "private": True,
                },
                {
                    "id": "evt_spanning",
                    "title": "Primary on-call",
                    "start": "2026-04-16T09:00:00-07:00",
                    "end": "2026-04-20T09:00:00-07:00",
                    "attendees": ["riley"],
                    "body": "Keep the handoff note.",
                },
                {
                    "id": "evt_other_day",
                    "title": "Other day",
                    "start": "2026-04-18T09:00:00-07:00",
                    "end": "2026-04-18T09:30:00-07:00",
                    "attendees": [],
                },
            ]
        }

        result, saved_state, _ = self.run_tool(
            state=state,
            tool="list_calendar_events",
            tool_args={"date": "2026-04-17"},
            persona="riley",
        )

        self.assertEqual(
            result,
            [
                {
                    "id": "evt_current",
                    "title": "Current event",
                    "start": "2026-04-17T09:00:00-07:00",
                    "end": "2026-04-17T09:30:00-07:00",
                    "attendees": ["riley", "owen"],
                    "body": "Keep this accepted body.",
                    "location": "Room 3",
                },
                {
                    "id": "evt_legacy",
                    "title": "Legacy event",
                    "start": "2026-04-17T10:00:00-07:00",
                    "end": "2026-04-17T10:30:00-07:00",
                    "attendees": ["alex", "hema"],
                },
                {
                    "id": "evt_spanning",
                    "title": "Primary on-call",
                    "start": "2026-04-16T09:00:00-07:00",
                    "end": "2026-04-20T09:00:00-07:00",
                    "attendees": ["riley"],
                    "body": "Keep the handoff note.",
                },
            ],
        )
        self.assertEqual(saved_state, state)

    def test_send_email_contract_states_new_email_and_no_thread_support(self) -> None:
        tool = exact_persona_tools("alex")["send_email"]
        contract = tool["state_effect"]

        self.assertIn(
            "This tool sends body text only and cannot attach files.",
            contract["notes"],
        )
        description = " ".join(tool["description"].split())
        self.assertIn("creates a new email only", description)
        self.assertIn("no thread ID, message ID, or reply capability", description)

    def test_doc_comment_uses_existing_document_ids_and_allows_standalone_threads(self) -> None:
        contract = exact_persona_tools("alex")["post_doc_comment"]
        self.assertEqual(contract["state_effect"]["reads_state_keys"], ["docs"])
        self.assertTrue(contract["state_effect"]["empty_result_is_valid"])
        self.assertNotIn("does_not_require_lookup", contract["state_effect"])
        state = {
            "docs": [{"id": "doc_1", "title": "Lantern pilot-preparation agreement"}],
            "doc_comments": [],
        }
        for doc_id in ("Lantern pilot-preparation agreement", "lantern-pilot-preparation-agreement"):
            with self.subTest(doc_id=doc_id):
                result, saved_state, _ = self.run_tool(
                    state=state,
                    tool="post_doc_comment",
                    tool_args={"doc_id": doc_id, "body": "Comment"},
                    persona="alex",
                )
                self.assertEqual(result["error"], "INVALID_DOCUMENT_ID")
                self.assertIn("doc_1", result["instruction"])
                self.assertEqual(saved_state, state)

        result, saved_state, _ = self.run_tool(
            state=state,
            tool="post_doc_comment",
            tool_args={"doc_id": "doc_1", "body": "Comment"},
            persona="alex",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(saved_state["doc_comments"][0]["doc_id"], "doc_1")

        result, saved_state, _ = self.run_tool(
            state=state,
            tool="post_doc_comment",
            tool_args={"doc_id": "lantern-pilot-ops-thread", "body": "Comment"},
            persona="alex",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            saved_state["doc_comments"][0]["doc_id"], "lantern-pilot-ops-thread"
        )

    def test_discord_message_mutates_the_contract_state_key(self) -> None:
        contract = exact_persona_tools("morgan")["send_discord_message"]
        declared_keys = contract["state_effect"]["writes_state_keys"]
        self.assertEqual(declared_keys, ["discord_log"])

        result, saved_state, _ = self.run_tool(
            state={"discord_log": []},
            tool="send_discord_message",
            tool_args={
                "channel": "#eng-team",
                "message": "The Discord contract test passed.",
            },
            session_id="contract_test_discord",
            narrative_datetime="2026-08-25T12:00:00Z",
            replay_epoch_ms=1787659200001,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(len(saved_state[declared_keys[0]]), 1)
        self.assertEqual(saved_state[declared_keys[0]][0]["channel"], "#eng-team")

    def test_project_status_reads_exact_record_from_state(self) -> None:
        state = {
            "projects": {
                "mercury": {
                    "project": "Project Mercury",
                    "status": "in beta",
                    "progress": "82%",
                    "working_channel": "#mercury-live",
                },
                "atlas": {
                    "project": "Project Atlas",
                    "status": "paused",
                },
            }
        }

        result, _, _ = self.run_tool(
            state=state,
            tool="check_project_status",
            tool_args={"project_name": "Project Mercury"},
        )

        self.assertEqual(result, state["projects"]["mercury"])
        self.assertNotIn("platform", result)

    def test_project_status_does_not_fuzzy_match(self) -> None:
        state = {
            "projects": {
                "mercury": {
                    "project": "Project Mercury",
                    "status": "in beta",
                }
            }
        }

        result, _, _ = self.run_tool(
            state=state,
            tool="check_project_status",
            tool_args={"project_name": "Merc"},
        )

        self.assertEqual(result["status"], "unknown")
        self.assertIn("No project with that exact name", result["error"])
        self.assertNotIn("working_channel", result)
        self.assertNotIn("platform", result)

    def test_project_status_rollup_uses_state_records(self) -> None:
        projects = {
            "mercury": {"project": "Project Mercury", "status": "in beta"},
            "atlas": {"project": "Project Atlas", "status": "paused"},
        }

        result, _, _ = self.run_tool(
            state={"projects": projects},
            tool="check_project_status",
            tool_args={"project_name": "roll-up"},
        )

        self.assertEqual(result["workstreams"], list(projects.values()))
        self.assertEqual(result["summary"], "2 project records available.")

    def test_execution_uses_explicit_session_and_narrative_time(self) -> None:
        narrative_datetime = "2023-05-08T09:15:00-07:00"

        result, state, logs = self.run_tool(
            state={"sent_emails": []},
            tool="send_email",
            tool_args={
                "to": "team@example.com",
                "subject": "Status",
                "body": "Current status attached.",
            },
            session_id="extension_morgan_2023_05_05_013",
            narrative_datetime=narrative_datetime,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(state["sent_emails"][0]["sent_at"], narrative_datetime)
        self.assertEqual(logs[0]["ts"], narrative_datetime)
        self.assertEqual(
            logs[0]["session_id"], "extension_morgan_2023_05_05_013"
        )

    def test_crm_notes_contract_matches_replacement_behavior(self) -> None:
        description = exact_persona_tools("morgan")["update_crm_row"]["description"]
        self.assertIn("complete replacement value", description)

        _, state, _ = self.run_tool(
            state={"crm": [{"id": "crm_1", "name": "Evergreen", "notes": "Old"}]},
            tool="update_crm_row",
            tool_args={"name": "Evergreen", "notes": "New"},
        )

        self.assertEqual(state["crm"][0]["notes"], "New")

    def test_runbook_body_replaces_the_existing_body(self) -> None:
        _, state, _ = self.run_tool(
            state={
                "runbooks": [
                    {
                        "id": "rb_oncall",
                        "title": "On-call handoff",
                        "body": "The old complete runbook body.",
                    }
                ]
            },
            tool="update_runbook_entry",
            tool_args={
                "entry_id": "rb_oncall",
                "body": "The new complete runbook body.",
            },
            persona="alex",
        )

        self.assertEqual(
            state["runbooks"][0]["body"], "The new complete runbook body."
        )

    def test_runbook_update_can_replace_structured_owners(self) -> None:
        _, state, _ = self.run_tool(
            state={
                "runbooks": [
                    {
                        "id": "rb_metrics_router",
                        "owner": "alex",
                        "backup": "wes",
                        "body": "Old ownership.",
                    }
                ]
            },
            tool="update_runbook_entry",
            tool_args={
                "entry_id": "rb_metrics_router",
                "owner": "wes",
                "backup": "alex",
                "body": "Wes is primary; Alex is backup.",
            },
            persona="alex",
        )

        self.assertEqual(state["runbooks"][0]["owner"], "wes")
        self.assertEqual(state["runbooks"][0]["backup"], "alex")

    def test_deck_contract_exposes_existing_deck_identity_to_planner(self) -> None:
        deck_edits = [
            {
                "deck": "Q3_Operating_Review_Sep24",
                "slide": 3,
                "change": "Evergreen evidence update.",
            }
        ]

        visible = readable_app_state(
            exact_persona_tools("morgan"),
            {"deck_edits": deck_edits, "private_state": ["not visible"]},
        )

        self.assertEqual(visible, {"deck_edits": deck_edits})


if __name__ == "__main__":
    unittest.main()
