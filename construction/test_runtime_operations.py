from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from construction.runtime_operations import (
    _resolve_operation_result_references,
    execute_operations,
)


ROOT = Path(__file__).resolve().parents[1]


def _execute_individually(
    *,
    persona: str,
    plans: list[dict],
    state_path: Path,
    log_path: Path,
    tool_python: Path,
) -> list[dict]:
    """Reference the pre-batch protocol: one child process per operation."""
    results = []
    prior_results = {}
    replay_operation_index = 0
    for session in plans:
        for local_operation_index, source_operation in enumerate(
            session.get("app_operations") or []
        ):
            contact_id = str(session.get("contact_id") or "")
            operation = copy.deepcopy(source_operation)
            operation["args"] = _resolve_operation_result_references(
                operation["args"],
                label=f"{contact_id} app_operations[{local_operation_index}]",
                prior_results=prior_results,
            )
            replay_epoch_ms = (
                int(datetime.fromisoformat(str(session["narrative_date"])).timestamp() * 1000)
                + replay_operation_index
            )
            completed = subprocess.run(
                [
                    str(tool_python),
                    "-m",
                    "construction.execute_mock_tool",
                    "--persona",
                    persona,
                    "--state",
                    str(state_path),
                    "--log",
                    str(log_path),
                    "--session-id",
                    str(session["session_id"]),
                    "--narrative-datetime",
                    str(session["narrative_date"]),
                    "--tool",
                    str(operation["tool"]),
                    "--args",
                    json.dumps(operation["args"], ensure_ascii=False),
                    "--replay-epoch-ms",
                    str(replay_epoch_ms),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            try:
                parsed = json.loads(completed.stdout.strip())
            except json.JSONDecodeError:
                parsed = None
            row = {
                "contact_id": contact_id,
                "operation_index": local_operation_index,
                "session_id": session["session_id"],
                "operation": operation,
                "exit_code": completed.returncode,
                "result": parsed,
                "stdout": completed.stdout.strip(),
                "stderr": completed.stderr.strip(),
                "replay_epoch_ms": replay_epoch_ms,
            }
            results.append(row)
            replay_operation_index += 1
            if completed.returncode != 0 or (
                isinstance(parsed, dict) and parsed.get("error")
            ):
                return results
            prior_results[(contact_id, local_operation_index)] = row
    return results


class RuntimeOperationsBatchTest(unittest.TestCase):
    def test_batch_matches_individual_semantics_and_reduces_processes(self) -> None:
        plans = [
            {
                "contact_id": "create-contact",
                "session_id": "session-create",
                "narrative_date": "2025-02-03T10:00:00-08:00",
                "app_operations": [
                    {
                        "tool": "create_calendar_event",
                        "args": {
                            "title": "Parity meeting",
                            "start": "2025-02-03T10:00:00-08:00",
                            "end": "2025-02-03T10:30:00-08:00",
                            "attendees": [],
                        },
                    }
                ],
            },
            {
                "contact_id": "update-contact",
                "session_id": "session-update",
                "narrative_date": "2025-02-04T11:00:00-08:00",
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
                            "start": "2025-02-04T11:30:00-08:00",
                            "end": "2025-02-04T12:00:00-08:00",
                        },
                    }
                ],
            },
            {
                "contact_id": "message-contact",
                "session_id": "session-message",
                "narrative_date": "2025-02-05T12:00:00-08:00",
                "app_operations": [
                    {
                        "tool": "send_email",
                        "args": {
                            "to": "devon@example.com",
                            "subject": "Parity",
                            "body": "The parity run completed.",
                        },
                    }
                ],
            },
            {
                "contact_id": "failure-contact",
                "session_id": "session-failure",
                "narrative_date": "2025-02-06T13:00:00-08:00",
                "app_operations": [
                    {
                        "tool": "create_calendar_event",
                        "args": {
                            "title": "Invalid event",
                            "start": "2025-02-06T13:00:00",
                            "end": "2025-02-06T13:30:00",
                            "attendees": [],
                        },
                    }
                ],
            },
            {
                "contact_id": "after-failure-contact",
                "session_id": "session-after-failure",
                "narrative_date": "2025-02-07T14:00:00-08:00",
                "app_operations": [
                    {
                        "tool": "send_email",
                        "args": {
                            "to": "should-not-send@example.com",
                            "subject": "Should not run",
                            "body": "This operation must be skipped.",
                        },
                    }
                ],
            },
        ]
        starting_state = {"calendar": [], "sent_emails": []}
        tool_python = Path(sys.executable)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_state = root / "legacy-state.json"
            legacy_log = root / "legacy-log.jsonl"
            batch_state_first = root / "batch-state-first.json"
            batch_state_second = root / "batch-state-second.json"
            batch_log_first = root / "batch-log-first.jsonl"
            batch_log_second = root / "batch-log-second.jsonl"
            legacy_state.write_text(json.dumps(starting_state))
            batch_state_first.write_text(json.dumps(starting_state))

            legacy_rows = _execute_individually(
                persona="morgan",
                plans=plans[:3],
                state_path=legacy_state,
                log_path=legacy_log,
                tool_python=tool_python,
            )
            legacy_rows.extend(
                _execute_individually(
                    persona="morgan",
                    plans=plans[3:],
                    state_path=legacy_state,
                    log_path=legacy_log,
                    tool_python=tool_python,
                )
            )
            with patch(
                "construction.runtime_operations.subprocess.Popen",
                wraps=subprocess.Popen,
            ) as batch_process:
                batch_rows = execute_operations(
                    persona="morgan",
                    plans=plans[:3],
                    state_path=batch_state_first,
                    log_path=batch_log_first,
                    tool_python=tool_python,
                    deterministic_replay=True,
                )
                batch_state_second.write_text(batch_state_first.read_text())
                batch_rows.extend(
                    execute_operations(
                        persona="morgan",
                        plans=plans[3:],
                        state_path=batch_state_second,
                        log_path=batch_log_second,
                        tool_python=tool_python,
                        deterministic_replay=True,
                    )
                )

            self.assertEqual(batch_process.call_count, 1)
            self.assertEqual(len(legacy_rows), 4)
            self.assertEqual(batch_rows, legacy_rows)
            self.assertEqual(
                json.loads(batch_state_second.read_text()),
                json.loads(legacy_state.read_text()),
            )
            self.assertEqual(
                batch_log_first.read_text() + batch_log_second.read_text(),
                legacy_log.read_text(),
            )
            self.assertEqual(
                [row["session_id"] for row in batch_rows],
                [
                    "session-create",
                    "session-update",
                    "session-message",
                    "session-failure",
                ],
            )
            self.assertNotIn(
                "should-not-send@example.com",
                batch_log_first.read_text() + batch_log_second.read_text(),
            )


if __name__ == "__main__":
    unittest.main()
