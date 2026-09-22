"""Local MCP wiring tests with scripted responses, never a model provider."""

import json
import tempfile
import unittest
from pathlib import Path

from examples.harness_template import BenchmarkHarness
from examples.mcp_connection import connect_apps, inspect_apps
from examples.offline_adapter import OfflineAdapter
from harness.adapter import Interaction, InteractionRecord
from harness.runner import Runner
from harness.submission import check_messages


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_discovery_for_each_persona(self):
        for persona, count in (("morgan", 39), ("alex", 27), ("riley", 30)):
            result = await inspect_apps(persona)
            self.assertEqual(len(result["tools"]), count)
            self.assertEqual(result["model_calls"], 0)
            self.assertEqual(result["tool_calls"], 0)

    async def test_template_passes_real_tools_and_routes_one_scripted_action(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = Runner({}, root, OfflineAdapter({}, root), identity={"fixture": True})
            config = runner._apps("tests", "morgan", "fixture", {"mock_state": {}, "tools": ["list_inbox"]})
            request = Interaction("morgan", "tests", "fixture", "Check inbox", "2026-09-14", config, root)
            parent = self

            class ScriptedHarness(BenchmarkHarness):
                async def run_agent(self, actual, tools, call_app):
                    parent.assertIs(actual, request)
                    parent.assertEqual([tool.name for tool in tools], ["list_inbox"])
                    result = await call_app("list_inbox", {})
                    parent.assertFalse(result.isError)
                    return InteractionRecord({"model": "offline-contract-fixture"}, [
                        {"role": "user", "content": actual.dated_message},
                        {"role": "assistant", "tool_calls": [{"id": "one", "name": "list_inbox", "arguments": {}}],
                         "usage": {"input_tokens": 0, "output_tokens": 0}},
                        {"role": "tool", "tool_call_id": "one", "content": result.model_dump(mode="json", by_alias=True)},
                        {"role": "assistant", "content": "Scripted fixture response.",
                         "usage": {"input_tokens": 0, "output_tokens": 0}},
                    ])

            evidence = await ScriptedHarness({}, root)._run(request)
            calls = check_messages(evidence.messages, "scripted fixture")
            self.assertEqual(calls[0]["tool"], "list_inbox")
            log = Path(config["env"]["DOLPHINBENCH_LOG_PATH"])
            self.assertEqual(len(log.read_text().splitlines()), 1)
            self.assertEqual(json.loads(log.read_text())["tool"], "list_inbox")

    async def test_connection_can_close_on_error_and_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = Runner({}, root, OfflineAdapter({}, root), identity={"fixture": True})
            config = runner._apps("tests", "morgan", "fixture", {"mock_state": {}, "tools": ["list_inbox"]})
            handled = False
            try:
                async with connect_apps(config):
                    raise RuntimeError("agent failed")
            except* RuntimeError:
                handled = True
            self.assertTrue(handled)
            async with connect_apps(config) as session:
                self.assertEqual([tool.name for tool in (await session.list_tools()).tools], ["list_inbox"])

    async def test_template_cannot_claim_a_completed_integration(self):
        example = BenchmarkHarness({}, Path("/unused"))
        for function, args in ((example.identity, ()), (example.freeze, ("morgan",)),
                               (example.verify_checkpoint, ("morgan", {})),
                               (example.total_cost_usd, ("ingestion",))):
            with self.assertRaises(NotImplementedError):
                function(*args)
        with self.assertRaises(NotImplementedError):
            await example.run_agent(None, [], None)


if __name__ == "__main__":
    unittest.main()
