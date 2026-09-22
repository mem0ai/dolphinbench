"""Focused, no-network tests for official Claude plugin wiring."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import queue
import shlex
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from harness.claude_plugin_setup import configure_hindsight_evaluation, configure_plugin
from harness.hindsight_readonly_mcp import READ_TOOLS, filter_tool_list, permits_message
from reference.memory import proxy


class ClaudePluginSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.project.mkdir()
        self.source = self.root / "official"
        self.source.mkdir()
        self.settings = {
            "namespace": "diagnostic-morgan-7",
            "api_url": "https://memory.example.test/",
            "api_key": "test-provider-key",
            "mcp_url": "https://mcp.example.test/mcp/",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _plugin(self, relative: str) -> Path:
        root = self.source / relative
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text('{"name":"test"}\n')
        return root

    def _hindsight(self) -> Path:
        root = self.source / "hindsight-integrations" / "coding-agents"
        root.mkdir(parents=True)
        (root / "package.json").write_text('{"name":"@vectorize-io/hindsight-coding-agents"}\n')
        for name in (
            "claude-sessionstart-hook.js", "claude-hook.js", "claude-stop-hook.js", "mcp-server.js",
        ):
            path = root / "dist" / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("// built official artifact\n")
        return root

    def test_builtin_has_no_external_wiring(self) -> None:
        self.assertEqual(
            configure_plugin("builtin", self.home, self.project, self.source, self.settings),
            {"env": {}, "plugin_dirs": [], "settings": {}},
        )

    def test_mem0_uses_the_official_claude_plugin_and_scoped_environment(self) -> None:
        root = self._plugin("integrations/claude-code-plugin")
        result = configure_plugin("mem0", self.home, self.project, self.source, self.settings)
        self.assertEqual(result["plugin_dirs"], [str(root.resolve())])
        self.assertEqual(result["settings"], {})
        self.assertEqual(result["env"], {
            "MEM0_API_KEY": "test-provider-key",
            "MEM0_USER_ID": "diagnostic-morgan-7",
            "MEM0_API_URL": "https://memory.example.test",
        })

    def test_honcho_uses_the_built_plugin_and_v3_endpoint(self) -> None:
        root = self._plugin("plugins/honcho/.stage")
        (root / "dist").mkdir()
        (root / "dist" / "mcp-server.js").write_text("// built official artifact\n")
        result = configure_plugin("honcho", self.home, self.project, self.source, self.settings)
        self.assertEqual(result["plugin_dirs"], [str(root.resolve())])
        self.assertEqual(result["env"], {
            "HONCHO_API_KEY": "test-provider-key",
            "HONCHO_ENDPOINT": "https://memory.example.test/v3",
            "HONCHO_WORKSPACE": "diagnostic-morgan-7",
            "HONCHO_PEER_NAME": "diagnostic-morgan-7",
            "HONCHO_AI_PEER": "claude",
        })

    def test_hindsight_writes_only_the_isolated_home_and_installs_official_wiring(self) -> None:
        root = self._hindsight()
        result = configure_plugin("hindsight", self.home, self.project, self.source, self.settings)
        config = json.loads((self.home / ".hindsight" / "coding-agent.json").read_text())
        self.assertEqual(config, {
            "apiUrl": "https://memory.example.test",
            "apiToken": "test-provider-key",
            "bankId": "diagnostic-morgan-7",
            "autoSeed": False,
            "codebaseSurvey": False,
            "autoUpdate": False,
        })
        self.assertFalse((self.project / ".hindsight").exists())
        self.assertEqual(result["plugin_dirs"], [])
        hooks = result["settings"]["hooks"]
        self.assertEqual(hooks["SessionStart"][0]["hooks"][0]["command"], f'node "{root / "dist/claude-sessionstart-hook.js"}"')
        self.assertEqual(hooks["UserPromptSubmit"][0]["hooks"][0]["timeout"], 30)
        self.assertEqual(hooks["Stop"][0]["hooks"][0]["timeout"], 60)
        self.assertEqual(result["mcp_servers"], {
            "hindsight": {
                "command": "node",
                "args": [str(root / "dist/mcp-server.js")],
                "env": {"HINDSIGHT_MCP_HARNESS": "claude-code"},
            }
        })

    def test_supermemory_uses_the_official_plugin_custom_api_and_mcp(self) -> None:
        root = self._plugin("plugin")
        result = configure_plugin("supermemory", self.home, self.project, self.source, self.settings)
        self.assertEqual(result["plugin_dirs"], [str(root.resolve())])
        self.assertEqual(result["env"], {
            "SUPERMEMORY_CC_API_KEY": "test-provider-key",
            "SUPERMEMORY_API_URL": "https://memory.example.test",
            "SUPERMEMORY_MCP_URL": "https://mcp.example.test/mcp",
            "SUPERMEMORY_REPO_TAG": "diagnostic-morgan-7",
        })

    def test_hindsight_evaluation_copies_official_skill_and_only_keeps_read_hooks(self):
        root = self._hindsight()
        (root / "skill/references").mkdir(parents=True)
        (root / "skill/SKILL.md").write_text("Official instructions\n")
        (root / "skill/references/config.md").write_text("Official reference\n")
        original = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        result = configure_hindsight_evaluation(self.home, self.project, root, self.settings)
        copied = self.home / ".claude/skills/hindsight-coding-agent"
        for path in (root / "skill").rglob("*"):
            if path.is_file():
                self.assertEqual((copied / path.relative_to(root / "skill")).read_bytes(), path.read_bytes())
        self.assertEqual(original, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()})
        self.assertEqual(set(result["settings"]["hooks"]), {"SessionStart", "UserPromptSubmit"})
        self.assertEqual(result["env"]["CLAUDE_CONFIG_DIR"], str(self.home / ".claude"))
        self.assertTrue(result["mcp_servers"]["hindsight"]["args"][0].endswith("hindsight_readonly_mcp.py"))
        config = json.loads((self.home / ".hindsight/coding-agent.json").read_text())
        self.assertNotIn("reflectTimeoutMs", config)
        self.assertNotIn("reflectToolTimeoutMs", config)
        with self.assertRaisesRegex(ValueError, "fresh profile"):
            configure_hindsight_evaluation(self.home, self.project, root, self.settings)

    def test_hindsight_evaluation_refuses_missing_skill_or_redirected_destination(self):
        root = self._hindsight()
        with self.assertRaisesRegex(ValueError, "SKILL.md"):
            configure_hindsight_evaluation(self.home, self.project, root, self.settings)
        (root / "skill").mkdir()
        (root / "skill/SKILL.md").write_text("Official instructions\n")
        self.home.mkdir()
        (self.home / ".claude").symlink_to(self.project, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            configure_hindsight_evaluation(self.home, self.project, root, self.settings)
        self.assertEqual(list(self.project.iterdir()), [])

    def test_rejects_unknown_provider_and_incomplete_official_build(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported"):
            configure_plugin("other", self.home, self.project, self.source, self.settings)
        with self.assertRaisesRegex(ValueError, "official Claude plugin build"):
            configure_plugin("mem0", self.home, self.project, self.source, self.settings)
        self._plugin("integrations/claude-code-plugin")
        invalid = dict(self.settings)
        invalid.pop("api_key")
        with self.assertRaisesRegex(ValueError, "api_key"):
            configure_plugin("mem0", self.home, self.project, self.source, invalid)

    def test_sessionend_compatibility_only_changes_copied_manifest(self):
        root = self._plugin("plugin")
        (root / "hooks").mkdir()
        capture = root / "hooks/capture.js"
        capture.write_text("// official capture\n")
        lib = root / "hooks/lib"
        lib.mkdir()
        api = lib / "api.js"
        api.write_text("const REQUEST_TIMEOUT_MS = 3000;\n")
        hooks = {"SessionStart": [], "Stop": [{"hooks": [{"type": "command",
                 "command": 'node "${CLAUDE_PLUGIN_ROOT}/hooks/capture.js"', "async": True, "timeout": 30}]}]}
        manifest = root / "hooks/hooks.json"
        manifest.write_text(json.dumps({"hooks": hooks}))
        original = manifest.read_bytes()
        settings = {**self.settings, "capture_session_end": True}
        for _ in range(2):
            setup = configure_plugin("supermemory", self.home, self.project, root, settings)
            copied = Path(setup["plugin_dirs"][0])
            result = json.loads((copied / "hooks/hooks.json").read_text())["hooks"]
            self.assertNotIn("Stop", result)
            self.assertNotIn("async", result["SessionEnd"][0]["hooks"][0])
            self.assertEqual(45, result["SessionEnd"][0]["hooks"][0]["timeout"])
            self.assertEqual(result["SessionStart"], [])
            self.assertEqual((copied / "hooks/capture.js").read_bytes(), capture.read_bytes())
            self.assertIn("REQUEST_TIMEOUT_MS = 30000", (copied / "hooks/lib/api.js").read_text())
            self.assertEqual(manifest.read_bytes(), original)
            self.assertEqual("const REQUEST_TIMEOUT_MS = 3000;\n", api.read_text())


class HindsightReadOnlyMcpTests(unittest.TestCase):
    def test_only_official_read_tools_and_protocol_setup_are_forwarded(self):
        for name in READ_TOOLS:
            self.assertTrue(permits_message({"method": "tools/call", "params": {"name": name}}))
        for name in ("hindsight_capture_initiative", "hindsight_ingest_document",
                     "hindsight_diagnose", "unknown"):
            self.assertFalse(permits_message({"method": "tools/call", "params": {"name": name}}))
        for method in ("initialize", "ping", "tools/list", "notifications/initialized"):
            self.assertTrue(permits_message({"method": method}))
        for method in ("resources/read", "prompts/get", "unknown"):
            self.assertFalse(permits_message({"method": method}))

    def test_tool_definitions_and_read_results_are_unchanged(self):
        definitions = [{"name": name, "description": "official", "inputSchema": {
            "type": "object", "properties": {"query": {"type": "string"}},
        }} for name in sorted(READ_TOOLS)]
        message = {"jsonrpc": "2.0", "id": 2, "result": {
            "tools": copy.deepcopy(definitions) + [{"name": "hindsight_capture_initiative"}],
        }}
        self.assertEqual(filter_tool_list(message)["result"]["tools"], definitions)
        result = {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "original"}]}}
        self.assertEqual(filter_tool_list(copy.deepcopy(result)), result)

    @unittest.skipUnless(os.environ.get("HINDSIGHT_TEST_OFFICIAL_SOURCE"), "optional local official build")
    def test_official_mcp_reads_through_proxy_and_cannot_write(self):
        requests = []

        class Backend(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append(("GET", self.path))
                if self.path.endswith("/tree"):
                    value = {"roots": [{"id": "kp-test", "kind": "page", "name": "Test"}]}
                elif "/search?" in self.path:
                    value = {"results": [{"id": "kp-test", "name": "Test", "snippet": "fixture"}]}
                else:
                    value = {"id": "kp-test", "content": "fixture"}
                self.reply(value)

            def do_POST(self):
                requests.append(("POST", self.path))
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.reply({"text": "fixture reflection"})

            def reply(self, value):
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        boundary = ThreadingHTTPServer(("127.0.0.1", 0), proxy.ReadOnlyHandler)
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (backend, boundary)]
        for thread in threads:
            thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp, patch.object(proxy, "UPSTREAM_PORT", backend.server_port), \
                    patch.dict(os.environ, {"HINDSIGHT_API_KEY": "fixture", "DOLPHINBENCH_HINDSIGHT_BANK_ID": "test"}):
                home = Path(temp) / "home"
                project = Path(temp) / "project"
                project.mkdir()
                result = configure_hindsight_evaluation(home, project, Path(os.environ["HINDSIGHT_TEST_OFFICIAL_SOURCE"]), {
                    "api_url": f"http://127.0.0.1:{boundary.server_port}", "api_key": "fixture", "namespace": "test",
                })
                mcp = result["mcp_servers"]["hindsight"]
                env = {"PATH": os.environ["PATH"], "TMPDIR": temp,
                       **result["env"], **mcp["env"]}
                process = subprocess.Popen([mcp["command"], *mcp["args"]], cwd=project, env=env,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                messages = queue.Queue()
                reader = threading.Thread(target=lambda: [messages.put(json.loads(line)) for line in process.stdout], daemon=True)
                reader.start()

                def call(index, method, params):
                    process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": index, "method": method, "params": params}) + "\n")
                    process.stdin.flush()
                    response = messages.get(timeout=15)
                    self.assertEqual(response.get("id"), index, response)
                    return response

                try:
                    self.assertIn("result", call(1, "initialize", {
                        "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"},
                    }))
                    roster = call(2, "tools/list", {})["result"]["tools"]
                    self.assertEqual({tool["name"] for tool in roster}, READ_TOOLS)
                    for index, (name, args) in enumerate((
                        ("hindsight_list_knowledge_pages", {}),
                        ("hindsight_search_knowledge_pages", {"query": "exact & question"}),
                        ("hindsight_read_knowledge_page", {"page_id": "kp-test"}),
                        ("hindsight_reflect", {"query": "why"}),
                    ), 3):
                        response = call(index, "tools/call", {"name": name, "arguments": args})
                        self.assertNotIn("error", response)
                        self.assertFalse(response["result"].get("isError"), response)
                    before = list(requests)
                    self.assertIn("error", call(7, "tools/call", {"name": "hindsight_capture_initiative", "arguments": {}}))
                    self.assertEqual(requests, before)
                    self.assertEqual(requests, [
                        ("GET", "/v1/default/banks/test/knowledge-base/tree"),
                        ("GET", "/v1/default/banks/test/knowledge-base/search?q=exact%20%26%20question&limit=3"),
                        ("GET", "/v1/default/banks/test/knowledge-base/pages/kp-test"),
                        ("POST", "/v1/default/banks/test/reflect"),
                    ])
                    for event, entries in result["settings"]["hooks"].items():
                        hook = subprocess.run(shlex.split(entries[0]["hooks"][0]["command"]),
                                              cwd=project, env=env, input=json.dumps({
                                                  "session_id": "fixture", "cwd": str(project),
                                                  "prompt": "why", "hook_event_name": event,
                                              }), text=True, capture_output=True, timeout=15)
                        self.assertEqual(hook.returncode, 0, hook.stderr)
                        self.assertIn("Hindsight", hook.stdout, (event, hook.stderr, requests))
                    self.assertTrue(all(method == "GET" or path.endswith("/reflect")
                                        for method, path in requests))
                finally:
                    process.stdin.close()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    reader.join(timeout=5)
                    process.stdout.close()
                    process.stderr.close()
        finally:
            for server in (boundary, backend):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join()


if __name__ == "__main__":
    unittest.main()
