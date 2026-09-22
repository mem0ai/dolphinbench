from __future__ import annotations

import os
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness import oracle
from harness.mining.preview import prod_grade_passed, prod_grade_result


class OracleCheckpointPromptTest(unittest.TestCase):
    def test_new_oracle_saves_initial_inputs_continuation_and_indexed_results(self) -> None:
        request = "Send the update. "
        candidate = {
            "test": request, "narrative_anchor_date": "2026-09-14",
            "_oracle_input": {
                "version": 4,
                "system": oracle.NORMAL_ASSISTANT_PROMPT.format(evaluation_date="2026-09-14"),
                "source_messages": [{"date": "2026-01-02T09:00:00-08:00", "text": "Original funding context."}],
            },
        }
        tool = {"type": "function", "function": {"name": "send_email", "parameters": {"type": "object"}}}
        responses = [
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="Sending.", tool_calls=[SimpleNamespace(
                    id="call1", function=SimpleNamespace(name="send_email", arguments='{"body":"Funding update"}')
                )],
            ))]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Sent.", tool_calls=[]))]),
        ]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text("{}")
            with (
                patch.object(oracle, "MOCK_STATE_PATH", state),
                patch.object(oracle, "MOCK_LOG_PATH", Path(directory) / "calls.jsonl"),
                patch.object(oracle, "_import_mock_server", return_value=object()),
                patch.object(oracle, "_openai_tool_specs", return_value=[tool]),
                patch.object(oracle, "_resolve_client_and_model", return_value=(object(), "offline")),
                patch.object(oracle, "_create_with_retry", side_effect=responses),
                patch.object(oracle, "_call_tool", return_value={"sent": True}),
            ):
                result = oracle.run_oracle(candidate, "morgan", with_memory=True)
        self.assertEqual(result["agent_input"]["tools"], [tool])
        self.assertEqual(len(result["agent_input"]["messages"]), 2)
        self.assertEqual(result["agent_input"]["messages"][1], {"role": "user", "content": request})
        self.assertIn("[2026-01-02T09:00:00-08:00] Original funding context.",
                      result["agent_input"]["messages"][0]["content"])
        self.assertEqual(result["agent_input"]["starting_state_sha256"],
                         hashlib.sha256(json.dumps({}, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
        self.assertEqual(result["tool_results"], [{
            "call_index": 0, "tool": "send_email", "args": {"body": "Funding update"}, "result": {"sent": True},
        }])
        self.assertEqual([row["role"] for row in result["messages"]],
                         ["system", "user", "assistant", "tool", "assistant"])
        self.assertEqual(result["errors"], [])

    def test_production_grader_returns_full_check_details(self) -> None:
        candidate = {
            "grade": {
                "type": "tool_trace",
                "config": {
                    "assertions": [
                        {
                            "type": "field_equals",
                            "tool": "send_email",
                            "path": "args.to",
                            "value": "person@example.com",
                        }
                    ]
                },
            }
        }
        oracle_result = {
            "tool_trace": [
                {
                    "tool": "send_email",
                    "args": {"to": "person@example.com"},
                    "result": {"ok": True},
                }
            ]
        }

        result = prod_grade_result(candidate, oracle_result)

        self.assertIsNotNone(result)
        self.assertTrue(result["passed"])
        self.assertEqual(result["details"][0]["observed_values"][0]["value"], "person@example.com")
        self.assertTrue(prod_grade_passed(candidate, oracle_result))

    def test_azure_endpoint_is_accepted_as_the_oracle_base_url(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AZURE_OPENAI_API_KEY": "test-key",
                "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com/",
            },
            clear=True,
        ):
            key, base_url = oracle._load_azure_creds()

        self.assertEqual(key, "test-key")
        self.assertEqual(base_url, "https://example.openai.azure.com/openai/v1/")

    def test_prompt_uses_explicit_checkpoint_for_facts_and_sessions(self) -> None:
        checkpoint = Path("/tmp/authenticated-checkpoint")
        sessions = [
            {
                "id": "later",
                "narrative_date": "2026-01-02T09:00:00-08:00",
                "messages": ["Later context."],
            },
            {
                "id": "earlier",
                "narrative_date": "2026-01-01T09:00:00-08:00",
                "messages": ["Earlier context."],
            },
        ]
        with (
            patch(
                "harness.oracle.source_session_ids_for_test",
                return_value=["later", "earlier"],
            ) as source_ids,
            patch("harness.oracle.load_sessions", return_value=sessions) as load_sessions,
        ):
            prompt = oracle.build_system_prompt(
                {"load_bearing_facts": [1]}, "morgan", checkpoint=checkpoint
            )

        source_ids.assert_called_once_with(
            {"load_bearing_facts": [1]},
            "morgan",
            root=oracle.DOLPHINBENCH_ROOT,
            checkpoint=checkpoint,
        )
        load_sessions.assert_called_once_with(
            "morgan", root=oracle.DOLPHINBENCH_ROOT, checkpoint=checkpoint
        )
        self.assertLess(prompt.index("Earlier context."), prompt.index("Later context."))

    def test_environment_checkpoint_bridges_the_subprocess_boundary(self) -> None:
        checkpoint = "/tmp/authenticated-checkpoint"
        with (
            patch.dict(os.environ, {"DOLPHINBENCH_CHECKPOINT": checkpoint}),
            patch("harness.oracle.source_session_ids_for_test", return_value=[]) as source_ids,
            patch("harness.oracle.load_sessions", return_value=[]),
        ):
            oracle.build_system_prompt({"load_bearing_facts": [1]}, "morgan")

        source_ids.assert_called_once_with(
            {"load_bearing_facts": [1]},
            "morgan",
            root=oracle.DOLPHINBENCH_ROOT,
            checkpoint=checkpoint,
        )


if __name__ == "__main__":
    unittest.main()
