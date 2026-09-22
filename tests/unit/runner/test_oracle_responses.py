import copy
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harness import oracle
from harness.oracle_responses import complete_turn, function_tools


def response(items, status="completed"):
    return SimpleNamespace(id="resp_fixture", created_at=1, status=status, incomplete_details=None,
        output=[SimpleNamespace(model_dump=lambda _item=item, **kwargs: copy.deepcopy(_item)) for item in items],
        usage=SimpleNamespace(model_dump=lambda: {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}))


class ResponsesTests(unittest.TestCase):
    def test_staged_responses_records_only_safe_capacity_headers(self):
        client = Mock()
        reply = response([{"type": "message", "content": [{"type": "output_text", "text": "Done."}]}])
        client.with_raw_response.responses.create.return_value = SimpleNamespace(
            headers={"x-ratelimit-limit-tokens": "1000000", "set-cookie": "secret"}, parse=lambda: reply)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "DOLPHINBENCH_AUTHORING_PROVIDER_CAPACITY": directory, "DOLPHINBENCH_AUTHORING_PROVIDER_SLOTS": "1",
                "DOLPHINBENCH_AUTHORING_TRANSPORT_LEDGER": directory + "/ledger.jsonl"}):
            normalized, _ = complete_turn(client=client, model="fixture", inputs=[], tools=[], reasoning_effort="medium")
            import json
            ledger = json.loads((Path(directory) / "ledger.jsonl").read_text())
        self.assertEqual(normalized.choices[0].message.content, "Done.")
        self.assertEqual(ledger["rate_limit_headers"], {"x-ratelimit-limit-tokens": "1000000"})
        client.responses.create.assert_not_called()

    def test_tool_turn_preserves_reasoning_and_function_output(self):
        candidate = {"test": "Send the update.", "narrative_anchor_date": "2026-09-14",
            "_oracle_input": {"version": 4,
                "system": oracle.NORMAL_ASSISTANT_PROMPT.format(evaluation_date="2026-09-14"), "source_messages": [],
                "execution_settings": {"model": "gpt-5.6-sol", "reasoning_effort": "medium",
                                       "timeout": 120, "max_retries": 0, "api": "responses"}}}
        tool = {"type": "function", "function": {"name": "send_email", "parameters": {"type": "object"}}}
        items = [{"type": "reasoning", "id": "r1", "summary": [], "encrypted_content": "opaque"},
                 {"type": "function_call", "id": "fc1", "call_id": "call1", "name": "send_email",
                  "arguments": '{"body":"Update"}', "status": "completed"}]
        saved = []
        replies = iter([response(items), response([{"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": "Sent."}]}])])
        client = Mock()
        client.with_options.return_value = client
        def create(**kwargs):
            saved.append(copy.deepcopy(kwargs))
            return next(replies)
        client.responses.create.side_effect = create
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(oracle, "MOCK_STATE_PATH", Path(directory) / "state.json"),
            patch.object(oracle, "MOCK_LOG_PATH", Path(directory) / "calls.jsonl"),
            patch.object(oracle, "_import_mock_server", return_value=object()),
            patch.object(oracle, "_openai_tool_specs", return_value=[tool]),
            patch.object(oracle, "_resolve_client_and_model", return_value=(client, "gpt-5.6-sol")),
            patch.object(oracle, "_call_tool", return_value={"sent": True}),
        ):
            result = oracle.run_oracle(candidate, "morgan", model="gpt-5.6-sol", with_memory=True)
        self.assertEqual(result["errors"], [])
        self.assertEqual(saved[1]["input"][2:4], items)
        self.assertEqual(saved[1]["input"][4]["type"], "function_call_output")
        self.assertEqual(saved[1]["input"][4]["call_id"], "call1")
        self.assertFalse(saved[0]["store"])
        self.assertFalse(function_tools([tool])[0]["strict"])
        self.assertEqual(result["provider_continuation"][0]["response_items"], items)

    def test_incomplete_and_empty_outputs_remain_errors(self):
        for reply in (response([], "incomplete"), response([])):
            client = Mock()
            client.responses.create.return_value = reply
            with self.assertRaises(RuntimeError):
                complete_turn(client=client, model="offline", inputs=[], tools=[], reasoning_effort="medium")

    def test_refusal_is_preserved_as_assistant_text(self):
        client = Mock()
        client.responses.create.return_value = response([{"type": "message", "content": [
            {"type": "refusal", "refusal": "Cannot do that."}]}])
        normalized, _ = complete_turn(client=client, model="offline", inputs=[], tools=[], reasoning_effort="medium")
        self.assertEqual(normalized.choices[0].message.content, "Cannot do that.")
