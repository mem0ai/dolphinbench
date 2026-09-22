from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import subprocess
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from harness.hermes_driver import (
    _failure_details,
    _find_matching_state_session,
    _parse_state_session,
    _terminal_transport_metadata,
    _tool_names_from_request_dumps,
    _transport_metadata,
    hermes_profiles_dir,
    run_hermes,
)


class DriverTransportTests(unittest.TestCase):
    def test_turn_status_controls_success_without_discarding_evidence(self) -> None:
        cases = [
            ({"completed": True}, 0, None, True),
            ({"completed": True}, -6, None, True),
            ({"completed": False, "partial": True,
              "error": "Model generated invalid tool call: terminal"}, 0, None, False),
            ({"completed": True, "partial": True}, 0, None, False),
            (None, 0, None, False),
            (None, 0, subprocess.TimeoutExpired("hermes", 5), False),
        ]
        for legacy in (False, True):
            for status, code, failure, expected in cases:
                with self.subTest(legacy=legacy, status=status, code=code, failure=failure):
                    def run(*args, **kwargs):
                        if failure:
                            raise failure
                        if status is not None:
                            Path(kwargs["env"]["DOLPHINBENCH_TURN_STATUS_PATH"]).write_text(json.dumps(status))
                        return SimpleNamespace(stdout="", stderr="", returncode=code)

                    parsed = ("s1", "saved response", [{"tool": "example"}], {"input_tokens": 12}, "model", ["example"])
                    with patch("harness.hermes_driver._snapshot_sessions", return_value={}), \
                            patch("harness.hermes_driver._snapshot_state_sessions", return_value=set()), \
                            patch("harness.hermes_driver._snapshot_request_dumps", return_value={}), \
                            patch("harness.hermes_driver._find_matching_session", return_value=Path("session.json") if legacy else None), \
                            patch("harness.hermes_driver._find_matching_state_session", return_value="s1"), \
                            patch("harness.hermes_driver._parse_session_json", return_value=parsed), \
                            patch("harness.hermes_driver._parse_state_session", return_value=parsed), \
                            patch("harness.hermes_driver._read_real_usage", return_value=None), \
                            patch("harness.hermes_driver._tool_names_from_request_dumps", return_value=[]), \
                            patch("harness.hermes_driver._read_log_since", return_value=""), \
                            patch("reference.execution.hermes_agent.run_cli", side_effect=run):
                        result = run_hermes("test-profile", "message", timeout=5)
                    self.assertEqual(result.ok, expected)
                    self.assertEqual(result.response_text, "saved response")
                    self.assertEqual(result.token_usage, {"input_tokens": 12})
                    self.assertEqual(result.tool_calls, [{"tool": "example"}])
                    if not expected:
                        self.assertTrue(result.error)
                    if status and status.get("error"):
                        self.assertEqual(result.error, status["error"])

    def test_turn_observer_preserves_return_value(self) -> None:
        from harness.hermes_observed_cli import observe_turns

        value = {"completed": False, "partial": True, "error": "invalid tool", "messages": ["private"]}

        class Agent:
            def run_conversation(self, message):
                return value

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "status.json"
            observe_turns(Agent, path)
            self.assertIs(Agent().run_conversation("message"), value)
            self.assertEqual(json.loads(path.read_text()), {
                "completed": False, "partial": True, "error": "invalid tool",
            })

    def test_status_observer_waits_for_cli_agent_import(self) -> None:
        from harness.hermes_observed_cli import TurnStatusFinder
        from importlib.machinery import ModuleSpec
        from unittest.mock import Mock

        loader = Mock()
        spec = ModuleSpec("run_agent", loader)
        finder = TurnStatusFinder(Path("unused-status.json"))
        with patch("importlib.machinery.PathFinder.find_spec", return_value=spec), \
                patch("harness.hermes_observed_cli.observe_turns") as observe:
            self.assertIsNone(finder.find_spec("unrelated"))
            result = finder.find_spec("run_agent")
            loader.exec_module.assert_not_called()
            observe.assert_not_called()
            module = SimpleNamespace(AIAgent=object())
            result.loader.exec_module(module)
            loader.exec_module.assert_called_once_with(module)
            observe.assert_called_once_with(module.AIAgent, Path("unused-status.json"))

    def test_transport_failure_preserves_http_retry_guidance(self) -> None:
        headers = Message()
        headers["Retry-After"] = "13"
        result = _failure_details(HTTPError(
            "http://example.test/chat", 429, "rate limited", headers, None,
        ))
        self.assertTrue(result.transient)
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.retry_after_seconds, 13)

    def test_named_profiles_follow_hermes_home(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.home", return_value=Path("/test/home")):
            self.assertEqual(Path("/test/home/.hermes/profiles"), hermes_profiles_dir())
        for home in ("/test/hermes", "/test/hermes/profiles/current"):
            with self.subTest(home=home), patch.dict(os.environ, {"HERMES_HOME": home}):
                self.assertEqual(Path("/test/hermes/profiles"), hermes_profiles_dir())


    def test_hermes_extracts_cli_transport_metadata(self) -> None:
        self.assertEqual(
            _transport_metadata("HTTP 503 service unavailable; Retry-After: 9"),
            (503, 9.0),
        )

    def test_hermes_extracts_azure_rate_limit_without_numeric_status(self) -> None:
        message = (
            "API call failed after 3 retries. Your requests to gpt-5.6-luna "
            "in westus have exceeded rate limit."
        )
        self.assertEqual(_terminal_transport_metadata(message), (429, None))

    def test_hermes_preserves_terminal_rate_limit_for_created_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "agent.log"
            log.write_text("")

            def failed_run(*_args, **_kwargs):
                self.assertIsNone(_kwargs["timeout"])
                log.write_text(
                    "API call failed after 8 retries. Your requests to "
                    "gpt-5.6-luna in westus have exceeded rate limit.\n"
                )
                return SimpleNamespace(stdout="", stderr="", returncode=0)

            with patch("harness.hermes_driver._agent_log_path", return_value=log), \
                    patch("harness.hermes_driver._snapshot_sessions", return_value={}), \
                    patch("harness.hermes_driver._snapshot_state_sessions", return_value=set()), \
                    patch("harness.hermes_driver._snapshot_request_dumps", return_value={}), \
                    patch("harness.hermes_driver._find_matching_session", return_value=None), \
                    patch("harness.hermes_driver._find_matching_state_session", return_value="s1"), \
                    patch("harness.hermes_driver._parse_state_session", return_value=(
                        "s1", "", [], {}, "gpt-5.6-luna", [],
                    )), \
                    patch("harness.hermes_driver._state_db", return_value=Path(raw) / "state.db"), \
                    patch("reference.execution.hermes_agent.run_cli", side_effect=failed_run):
                result = run_hermes("profile", "message")

            self.assertFalse(result.ok)
            self.assertEqual(result.status_code, 429)
            self.assertEqual(result.error, "HTTP 429: model deployment rate limit exceeded")

    def test_current_hermes_state_db_is_parsed_without_session_json(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            db = Path(raw) / "state.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, "
                "parent_session_id TEXT, source TEXT, started_at REAL);"
                "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
                "role TEXT, content TEXT, tool_calls TEXT, active INTEGER);"
            )
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                ("s-new", "gpt-5.6-luna", None, "cli", 2.0),
            )
            conn.executemany(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (1, "s-new", "user", "exact request", None, 1),
                    (2, "s-new", "assistant", "", json.dumps([{
                        "function": {"name": "memory", "arguments": "{\"operations\": []}"},
                    }]), 1),
                    (3, "s-new", "assistant", "Done.", None, 1),
                ],
            )
            conn.commit()
            conn.close()
            with patch("harness.hermes_driver._state_db", return_value=db), patch(
                "harness.hermes_driver._read_real_usage",
                return_value={"model": "gpt-5.6-luna", "input_tokens": 12},
            ):
                self.assertEqual(
                    _find_matching_state_session("profile", set(), "exact request"),
                    "s-new",
                )
                parsed = _parse_state_session(
                    "profile", "s-new", ["memory", "session_search"],
                )
            self.assertEqual(parsed[1], "Done.")
            self.assertEqual(parsed[2], [{"tool": "memory", "args": {"operations": []}}])
            self.assertEqual(parsed[3]["input_tokens"], 12)

    def test_runtime_tool_surface_combines_request_and_discovered_mock_tools(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            profile_dir = Path(raw)
            sessions = profile_dir / "sessions"
            cache = profile_dir / "cache"
            sessions.mkdir()
            cache.mkdir()
            (sessions / "request_dump_s1_1.json").write_text(json.dumps({
                "session_id": "s1",
                "request": {"body": {"tools": [
                    {"type": "function", "name": "mem0_search"},
                    {"type": "function", "name": "session_search"},
                ]}},
            }))
            (cache / "mcp_schema_cache.json").write_text(json.dumps({
                "dolphinbench-apps": {"tools": [{"name": "send_email"}]},
            }))
            with patch("harness.hermes_driver._sessions_dir", return_value=sessions), patch(
                "pathlib.Path.home", return_value=profile_dir,
            ):
                expected_cache = (
                    profile_dir / ".hermes" / "profiles" / "profile"
                    / "cache" / "mcp_schema_cache.json"
                )
                expected_cache.parent.mkdir(parents=True, exist_ok=True)
                expected_cache.write_text((cache / "mcp_schema_cache.json").read_text())
                tools = _tool_names_from_request_dumps("profile", {}, "s1")
            self.assertEqual(
                tools,
                ["mcp__dolphinbench_apps__send_email", "mem0_search", "session_search"],
            )

    def test_runtime_tool_surface_includes_explicitly_listed_deferred_tool(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            profile_dir = Path(raw)
            sessions = profile_dir / "sessions"
            sessions.mkdir()
            (sessions / "request_dump_s1_1.json").write_text(json.dumps({
                "session_id": "s1",
                "request": {"body": {"tools": [{
                    "type": "function",
                    "function": {
                        "name": "tool_search",
                        "description": (
                            "Search additional tools.\n\n"
                            "Deferred tool catalog (call schemas with tool_describe):\n"
                            "session_search tools (1):\n"
                            "- session_search: Recall past conversations."
                        ),
                        "parameters": {"type": "object", "properties": {}},
                    },
                }]}},
            }))
            with patch("harness.hermes_driver._sessions_dir", return_value=sessions), patch(
                "pathlib.Path.home", return_value=profile_dir,
            ):
                tools = _tool_names_from_request_dumps("profile", {}, "s1")
            self.assertEqual(tools, ["session_search", "tool_search"])

    def test_runtime_tool_surface_does_not_assume_bridge_contains_session_search(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            profile_dir = Path(raw)
            sessions = profile_dir / "sessions"
            sessions.mkdir()
            (sessions / "request_dump_s1_1.json").write_text(json.dumps({
                "session_id": "s1",
                "request": {"body": {"tools": [{
                    "type": "function",
                    "function": {
                        "name": "tool_search",
                        "description": "Search additional tools.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }]}},
            }))
            with patch("harness.hermes_driver._sessions_dir", return_value=sessions), patch(
                "pathlib.Path.home", return_value=profile_dir,
            ):
                tools = _tool_names_from_request_dumps("profile", {}, "s1")
            self.assertEqual(tools, ["tool_search"])


if __name__ == "__main__":
    unittest.main()
