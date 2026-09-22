from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.claude_driver import run_claude, _rejected_unavailable_tools


_FAKE_CLAUDE = r'''
import json
import os
import sys
import time

capture = os.environ.get("FAKE_CAPTURE")
if capture:
    with open(capture, "w") as target:
        json.dump({
            "argv": sys.argv[1:],
            "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
            "inherited": os.environ.get("FAKE_INHERITED"),
            "overlay": os.environ.get("FAKE_OVERLAY"),
            "claude_oauth": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
            "api_key": os.environ.get("ANTHROPIC_API_KEY"),
            "auth_token": os.environ.get("ANTHROPIC_AUTH_TOKEN"),
            "aws_key": os.environ.get("AWS_ACCESS_KEY_ID"),
            "vertex_project": os.environ.get("VERTEXAI_PROJECT"),
            "foundry_endpoint": os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL"),
            "azure_endpoint": os.environ.get("AZURE_OPENAI_ENDPOINT"),
        }, target)

mode = os.environ.get("FAKE_MODE", "stream")
if mode == "timeout":
    time.sleep(5)
elif mode == "transport":
    print("HTTP 429 rate limited; Retry-After: 7", file=sys.stderr)
    raise SystemExit(9)
elif mode == "single_json":
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "single result", "session_id": "single-session",
        "model": "claude-test-single", "usage": {"input_tokens": 2, "output_tokens": 3},
    }))
elif mode == "unexpected_tool":
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "mcp__other__delete", "input": {"id": "x"}},
    ]}}))
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done"}))
else:
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "trace-session", "model": "claude-test"}))
    print(json.dumps({"type": "assistant", "message": {"model": "claude-test", "content": [
        {"type": "tool_use", "id": "tool-1", "name": "mcp__dolphinbench_apps__send_email", "input": {"to": "a@example.test"}},
        {"type": "text", "text": "Sent it."},
    ]}}))
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
        "result": "Final response", "session_id": "trace-session",
        "usage": {"input_tokens": 11, "output_tokens": 5,
                  "cache_read_input_tokens": 2}}))
'''


class ClaudeDriverTests(unittest.TestCase):
    def test_structured_quota_rejection_preserves_reset_and_partial_tools(self):
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "read-1", "name": "Read", "input": {"file_path": "MEMORY.md"}}]}},
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "rateLimitType": "five_hour", "resetsAt": 1788830400}},
            {"type": "result", "subtype": "success", "is_error": True,
             "api_error_status": 429, "result": "You've hit your session limit"},
        ]
        with patch("harness.claude_driver.subprocess.run", return_value=subprocess.CompletedProcess(
            [], 0, "\n".join(json.dumps(e) for e in events), "",
        )):
            result = self._run(builtin_tools=["Read"])
        self.assertFalse(result.ok)
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.rate_limit_reset_at, 1788830400)
        self.assertEqual(result.tool_calls[0]["tool"], "Read")

    def test_quota_metadata_ignores_warnings_and_does_not_mask_other_errors(self):
        from harness.claude_driver import _transport_metadata
        warning = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed_warning", "resetsAt": 1788830400}}
        success = {"type": "result", "is_error": False, "result": "HTTP 429 rate limited"}
        self.assertEqual(_transport_metadata(json.dumps(warning)), (None, None, None))
        rejected = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": 1788830400}}
        self.assertEqual(_transport_metadata(json.dumps(rejected)), (429, None, 1788830400))
        self.assertEqual(_transport_metadata(json.dumps([rejected, success])), (None, None, None))
        other = {"type": "result", "is_error": True, "api_error_status": 503}
        self.assertEqual(_transport_metadata(json.dumps([rejected, other])), (503, None, None))
        self.assertEqual(_transport_metadata(json.dumps(other)), (503, None, None))
        for bad in (None, True, -1, "1788830400"):
            rejected["rate_limit_info"]["resetsAt"] = bad
            self.assertEqual(_transport_metadata(json.dumps(rejected)), (429, None, None))

    def test_seed_unavailable_attempt_is_not_unauthorized_execution(self):
        name = "hindsight_search_knowledge_pages"
        events = [
            {"type": "system", "subtype": "init", "tools": ["Write"]},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": name, "input": {}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                 "content": f"<tool_use_error>Error: No such tool available: {name}</tool_use_error>"}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "Saved"},
        ]
        def run(mode):
            stdout = "\n".join(json.dumps(event) for event in events)
            with patch("harness.claude_driver.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout, "")):
                return self._run(native_memory_mode=mode, native_memory_dir=self.config_dir / "auto-memory")
        self.assertTrue(run("seed").ok)
        self.assertEqual(run("seed").tool_calls[0]["tool"], name)
        self.assertFalse(run(None).ok)
        events[-1]["is_error"] = True
        self.assertFalse(run("seed").ok)
        events[-1]["is_error"] = False
        events[0]["tools"].append(name)
        self.assertFalse(run("seed").ok)
        events[0]["tools"].remove(name)
        result = events[2]["message"]["content"][0]
        for changes in ({"is_error": False}, {"content": "Permission denied"}, {"tool_use_id": "other"}):
            original = dict(result)
            result.update(changes)
            self.assertFalse(run("seed").ok)
            result.clear()
            result.update(original)
        events.pop(2)
        self.assertFalse(run("seed").ok)
        self.assertEqual(_rejected_unavailable_tools("{}"), set())

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = self.root / "fake_claude.py"
        self.fake.write_text(_FAKE_CLAUDE)
        self.config_dir = self.root / "isolated-claude"
        self.mcp_config = self.root / "mcp.json"
        self.mcp_config.write_text(json.dumps({"mcpServers": {"dolphinbench_apps": {}}}))
        self.allowed = ["mcp__dolphinbench_apps__send_email"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, **kwargs):
        mcp_config_path = kwargs.pop("mcp_config_path", self.mcp_config)
        allowed_mcp_tools = kwargs.pop("allowed_mcp_tools", self.allowed)
        return run_claude(
            "send the email",
            claude_command=[sys.executable, str(self.fake)],
            claude_config_dir=self.config_dir,
            mcp_config_path=mcp_config_path,
            allowed_mcp_tools=allowed_mcp_tools,
            **kwargs,
        )

    def test_runs_with_isolated_strict_mcp_surface_and_parses_jsonl(self) -> None:
        capture = self.root / "capture.json"
        with patch.dict(os.environ, {"FAKE_INHERITED": "kept"}):
            result = self._run(
                session_id="do-not-resume-this",
                narrative_time="2026-09-02T12:00:00Z",
                env={
                    "FAKE_CAPTURE": str(capture),
                    "FAKE_OVERLAY": "applied",
                    "CLAUDE_CONFIG_DIR": "/ambient/config",
                },
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.session_id, "trace-session")
        self.assertEqual(result.response_text, "Final response")
        self.assertEqual(result.tool_calls, [{
            "tool": "mcp__dolphinbench_apps__send_email", "args": {"to": "a@example.test"},
        }])
        self.assertEqual(result.token_usage["input_tokens"], 11)
        self.assertEqual(result.model, "claude-test")
        self.assertEqual(result.available_tools, self.allowed)

        invocation = json.loads(capture.read_text())
        argv = invocation["argv"]
        self.assertIn("--print", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertEqual(argv[argv.index("--mcp-config") + 1], str(self.mcp_config.resolve()))
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], self.allowed[0])
        generated_session = argv[argv.index("--session-id") + 1]
        self.assertNotEqual(generated_session, "do-not-resume-this")
        self.assertEqual(invocation["config_dir"], str(self.config_dir.resolve()))
        self.assertEqual(invocation["inherited"], "kept")
        self.assertEqual(invocation["overlay"], "applied")
        self.assertIn("[2026-09-02T12:00:00Z] send the email", argv)

    def test_parses_single_json_result(self) -> None:
        result = self._run(env={"FAKE_MODE": "single_json"})
        self.assertTrue(result.ok)
        self.assertEqual(result.session_id, "single-session")
        self.assertEqual(result.response_text, "single result")
        self.assertEqual(result.model, "claude-test-single")
        self.assertEqual(result.token_usage, {"input_tokens": 2, "output_tokens": 3})

    def test_explicit_ingestion_recovery_resumes_only_the_named_session(self) -> None:
        capture = self.root / "resume.json"
        session = "d254feb8-1d1b-4605-8581-a0985e5a7b0f"
        result = self._run(
            recovery_session_id=session, persist_session=True,
            native_memory_mode="seed", native_memory_dir=self.config_dir / "auto-memory",
            env={"FAKE_CAPTURE": str(capture)},
        )
        self.assertTrue(result.ok)
        argv = json.loads(capture.read_text())["argv"]
        self.assertEqual(session, argv[argv.index("--resume") + 1])
        self.assertNotIn("--session-id", argv)
        self.assertNotIn("--fork-session", argv)
        self.assertFalse(self._run(recovery_session_id=session).ok)

    def test_official_plugin_can_load_without_replacing_its_mcp_configuration(self) -> None:
        capture = self.root / "capture.json"
        plugin = self.root / "plugin"
        plugin.mkdir()
        result = self._run(
            mcp_config_path=None,
            plugin_directories=[plugin],
            persist_session=True,
            settings={"autoMemoryEnabled": True},
            env={"FAKE_CAPTURE": str(capture)},
        )
        self.assertTrue(result.ok)
        argv = json.loads(capture.read_text())["argv"]
        self.assertNotIn("--no-session-persistence", argv)
        self.assertNotIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--plugin-dir") + 1], str(plugin))
        self.assertTrue(json.loads(Path(argv[argv.index("--settings") + 1]).read_text())["autoMemoryEnabled"])

    def test_isolated_environment_does_not_inherit_other_provider_credentials(self) -> None:
        from harness.claude_driver import _subscription_environment
        with patch.dict(os.environ, {"MEM0_API_KEY": "another-condition", "HONCHO_API_KEY": "other"}):
            env = _subscription_environment({"CLAUDE_CODE_OAUTH_TOKEN": "selected"}, inherit=False)
        self.assertNotIn("MEM0_API_KEY", env)
        self.assertNotIn("HONCHO_API_KEY", env)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "selected")

    def test_child_uses_explicit_subscription_auth_and_removes_provider_credentials(self) -> None:
        capture = self.root / "auth-capture.json"
        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "ambient-api-key",
            "AWS_ACCESS_KEY_ID": "ambient-aws-key",
            "VERTEXAI_PROJECT": "ambient-project",
            "PATH": os.environ["PATH"],
        }):
            result = self._run(env={
                "FAKE_CAPTURE": str(capture),
                "CLAUDE_CODE_OAUTH_TOKEN": "subscription-token",
                "ANTHROPIC_API_KEY": "overlay-api-key",
                "ANTHROPIC_AUTH_TOKEN": "overlay-auth-token",
                "ANTHROPIC_FOUNDRY_BASE_URL": "https://foundry.invalid",
                "AZURE_OPENAI_ENDPOINT": "https://azure.invalid",
            })

        self.assertTrue(result.ok)
        invocation = json.loads(capture.read_text())
        self.assertEqual(invocation["claude_oauth"], "subscription-token")
        self.assertIsNone(invocation["api_key"])
        self.assertIsNone(invocation["auth_token"])
        self.assertIsNone(invocation["aws_key"])
        self.assertIsNone(invocation["vertex_project"])
        self.assertIsNone(invocation["foundry_endpoint"])
        self.assertIsNone(invocation["azure_endpoint"])

    def test_ambient_claude_oauth_token_is_not_used_without_explicit_env(self) -> None:
        capture = self.root / "ambient-auth-capture.json"
        with patch.dict(os.environ, {
            "CLAUDE_CODE_OAUTH_TOKEN": "ambient-token-must-not-pass",
        }):
            result = self._run(env={"FAKE_CAPTURE": str(capture)})

        self.assertTrue(result.ok)
        invocation = json.loads(capture.read_text())
        self.assertIsNone(invocation["claude_oauth"])

    def test_rejects_trace_tools_outside_the_exact_allowlist(self) -> None:
        result = self._run(env={"FAKE_MODE": "unexpected_tool"})
        self.assertFalse(result.ok)
        self.assertIn("outside the allowed MCP surface", result.error or "")
        self.assertEqual(result.tool_calls[0]["tool"], "mcp__other__delete")

    def test_surfaces_timeout_without_a_real_claude_call(self) -> None:
        result = self._run(env={"FAKE_MODE": "timeout"}, timeout=0.01)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "timeout after 0.01s")

    def test_timeout_keeps_partial_byte_output(self) -> None:
        with patch("harness.claude_driver.subprocess.run", side_effect=subprocess.TimeoutExpired(
                "claude", 10, output=b"partial stdout", stderr=b"partial stderr")):
            result = self._run(timeout=10)
        self.assertFalse(result.ok)
        self.assertEqual(result.stdout, "partial stdout")
        self.assertEqual(result.stderr, "partial stderr")

    def test_surfaces_cli_transport_failure(self) -> None:
        result = self._run(env={"FAKE_MODE": "transport"})
        self.assertFalse(result.ok)
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.retry_after_seconds, 7.0)
        self.assertIn("Claude exited with status 9", result.error or "")

    def test_requires_explicit_isolated_configuration(self) -> None:
        result = run_claude("hello")
        self.assertFalse(result.ok)
        self.assertIn("claude_config_dir is required", result.error or "")

    def test_native_seed_allows_only_the_configured_auto_memory_directory(self) -> None:
        capture = self.root / "native-seed.json"
        memory = self.config_dir / "auto-memory"
        result = self._run(
            mcp_config_path=None,
            allowed_mcp_tools=(),
            native_memory_mode="seed",
            native_memory_dir=memory,
            env={"FAKE_CAPTURE": str(capture), "FAKE_MODE": "single_json"},
        )
        self.assertTrue(result.ok)
        invocation = json.loads(capture.read_text())
        argv = invocation["argv"]
        self.assertEqual("Read,Write,Edit", argv[argv.index("--tools") + 1])
        self.assertNotIn("--mcp-config", argv)
        allowed = argv[argv.index("--allowedTools") + 1]
        self.assertEqual(
            ",".join([
                f"Read({memory.resolve()}/**)",
                f"Write({memory.resolve()}/**)",
                f"Edit({memory.resolve()}/**)",
            ]),
            allowed,
        )
        settings = Path(argv[argv.index("--settings") + 1])
        self.assertEqual(
            {"autoMemoryDirectory": str(memory.resolve())},
            json.loads(settings.read_text()),
        )

    def test_native_evaluation_cannot_write_memory_or_use_unscoped_files(self) -> None:
        capture = self.root / "native-eval.json"
        memory = self.config_dir / "auto-memory"
        memory.mkdir(parents=True)
        result = self._run(
            native_memory_mode="read_only",
            native_memory_dir=memory,
            env={"FAKE_CAPTURE": str(capture)},
        )
        self.assertTrue(result.ok)
        argv = json.loads(capture.read_text())["argv"]
        self.assertEqual("Read", argv[argv.index("--tools") + 1])
        allowed = argv[argv.index("--allowedTools") + 1]
        self.assertIn(self.allowed[0], allowed)
        self.assertIn(f"Read({memory.resolve()}/**)", allowed)
        self.assertNotIn("Write(", allowed)
        self.assertNotIn("Edit(", allowed)
        self.assertNotIn("Bash", allowed)
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        hook = settings["hooks"]["PreToolUse"][0]
        self.assertEqual(hook["matcher"], "Read")
        self.assertIn("claude_native_read_guard.py", hook["hooks"][0]["command"])
        self.assertIn(str(memory.resolve()), hook["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main()
