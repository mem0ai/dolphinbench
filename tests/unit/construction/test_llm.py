from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from construction.runtime_model_calls import cached_client_complete
from construction.model_input_text import render_model_input
from construction.llm import AzureJsonClient


class AzureJsonClientTest(unittest.TestCase):
    def test_direct_call_is_appended_to_construction_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "model_calls.jsonl"
            client = AzureJsonClient.__new__(AzureJsonClient)
            client.model = "gpt-test"
            client.reasoning_effort = "high"
            client.max_completion_tokens = None
            client.client = Mock()
            client.client.chat.completions.create.return_value = SimpleNamespace(
                id="response-1",
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
                usage=SimpleNamespace(
                    prompt_tokens=11,
                    completion_tokens=7,
                    total_tokens=18,
                    completion_tokens_details=SimpleNamespace(reasoning_tokens=3),
                ),
            )

            with patch.dict(
                os.environ,
                {
                    "DOLPHINBENCH_CALL_LEDGER": str(ledger),
                    "DOLPHINBENCH_CONSTRUCTION_RUN_ID": "run-1",
                },
                clear=False,
            ):
                result = client.complete(
                    "system",
                    {"input": "value"},
                    call_path=Path("response.json"),
                )

            row = json.loads(ledger.read_text())

        self.assertTrue(result["ok"])
        self.assertEqual(
            client.client.chat.completions.create.call_args.kwargs["response_format"],
            {"type": "json_object"},
        )
        self.assertEqual(row["cache_path"], "response.json")
        self.assertEqual(row["model"], "gpt-test")
        self.assertEqual(row["response_id"], "response-1")
        self.assertEqual(row["run_id"], "run-1")
        self.assertEqual(row["usage"]["total_tokens"], 18)

    def test_azure_client_uses_opt_in_strict_json_schema(self) -> None:
        client = AzureJsonClient.__new__(AzureJsonClient)
        client.model = "gpt-test"
        client.reasoning_effort = "high"
        client.max_completion_tokens = None
        client.client = Mock()
        client.client.chat.completions.create.return_value = SimpleNamespace(
            id="response-structured",
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
            usage=SimpleNamespace(
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
            ),
        )
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }

        result = client.complete(
            "system",
            {"input": "value"},
            response_schema=schema,
            response_schema_name="test_response",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            client.client.chat.completions.create.call_args.kwargs["response_format"],
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "test_response",
                    "strict": True,
                    "schema": schema,
                },
            },
        )

    def test_cached_client_legacy_call_receives_no_new_keywords(self) -> None:
        class LegacyClient:
            model = "legacy"

            def __init__(self) -> None:
                self.calls = 0

            def complete(self, system: str, payload: dict) -> dict:
                self.calls += 1
                return {"system": system, "payload": payload}

        with tempfile.TemporaryDirectory() as tmp:
            client = LegacyClient()
            with patch.dict(os.environ, {"DOLPHINBENCH_CALL_LEDGER": ""}, clear=False):
                result = cached_client_complete(
                    Path(tmp) / "response.json",
                    "legacy system",
                    {"value": 1},
                    client,
                )

        self.assertEqual(result["payload"], {"value": 1})
        self.assertEqual(client.calls, 1)

    def test_cached_client_forwards_schema_and_includes_it_in_cache_identity(self) -> None:
        class SchemaClient:
            model = "schema-client"

            def __init__(self) -> None:
                self.calls: list[dict] = []

            def complete(self, _system: str, _payload: dict, **kwargs: dict) -> dict:
                self.calls.append(kwargs)
                return {"call": len(self.calls)}

        schema = {
            "type": "object",
            "properties": {"call": {"type": "integer"}},
            "required": ["call"],
            "additionalProperties": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "response.json"
            client = SchemaClient()
            with patch.dict(os.environ, {"DOLPHINBENCH_CALL_LEDGER": ""}, clear=False):
                first = cached_client_complete(
                    path,
                    "system",
                    {"value": 1},
                    client,
                    response_schema=schema,
                    response_schema_name="schema_one",
                )
                cached = cached_client_complete(
                    path,
                    "system",
                    {"value": 1},
                    client,
                    response_schema=schema,
                    response_schema_name="schema_one",
                )
                changed_schema_name = cached_client_complete(
                    path,
                    "system",
                    {"value": 1},
                    client,
                    response_schema=schema,
                    response_schema_name="schema_two",
                )

        self.assertEqual(first, cached)
        self.assertEqual(changed_schema_name, {"call": 2})
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0]["response_schema"], schema)
        self.assertEqual(client.calls[0]["response_schema_name"], "schema_one")


class ModelInputTextTest(unittest.TestCase):
    def test_renders_ordered_labeled_plain_text(self) -> None:
        rendered = render_model_input(
            {
                "requested_dates": {"start": "2025-01-01", "end": "2025-01-07"},
                "current_facts": [
                    {
                        "id": 17,
                        "statement": "The agreed rate is 12.8 cents per kWh.",
                    }
                ],
                "notes": "First line.\nSecond line.",
            }
        )

        self.assertLess(rendered.index("# Requested dates"), rendered.index("# Current facts"))
        self.assertIn("Start: 2025-01-01", rendered)
        self.assertIn("Id: 17", rendered)
        self.assertIn("Statement: The agreed rate is 12.8 cents per kWh.", rendered)
        self.assertIn("First line.\nSecond line.", rendered)
        self.assertNotIn('{"requested_dates"', rendered)

    def test_rejects_non_object_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "model input must be an object"):
            render_model_input([])  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
