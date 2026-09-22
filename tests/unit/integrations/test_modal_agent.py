import io
import json
from pathlib import Path
import socket
import subprocess
import tarfile
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from reference.execution.agent_bridge import RemoteBridge
from reference.execution.hermes_agent import agent_environment, route_memory_endpoints
from reference.execution.modal_agent import ModalAgentContainer


class ModalAgentTests(unittest.TestCase):
    def test_agent_gets_only_its_model_and_memory_credentials(self):
        env = {"OPENAI_API_KEY": "agent", "CUSTOM_API_KEY": "agent",
               "AZURE_FOUNDRY_API_KEY": "agent", "AZURE_OPENAI_API_KEY": "judge",
               "MEM0_API_KEY": "selected", "SUPERMEMORY_API_KEY": "unrelated",
               "MODAL_TOKEN_SECRET": "controller"}
        selected = agent_environment({"model": {"provider": "azure-foundry"}, "memory": {"provider": "mem0"}}, env)
        self.assertEqual(selected, {key: env[key] for key in ("OPENAI_API_KEY", "CUSTOM_API_KEY", "AZURE_FOUNDRY_API_KEY", "MEM0_API_KEY")})

    def test_memory_urls_keep_paths_and_share_only_the_same_origin(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = {"honcho": {"baseUrl": "http://100.64.0.2:8000/v3"},
                      "hindsight": {"api_url": "http://localhost:8888"}}
            env = {"HONCHO_BASE_URL": "http://100.64.0.2:8000/v3"}
            (root / "honcho.json").write_text(json.dumps({"baseUrl": env["HONCHO_BASE_URL"]}))
            endpoints, ports = route_memory_endpoints(config, env, root)
            self.assertEqual(len(endpoints), 2)
            self.assertEqual(env["HONCHO_BASE_URL"], "http://127.0.0.1:19000/v3")
            self.assertEqual(config["honcho"]["baseUrl"], env["HONCHO_BASE_URL"])
            self.assertEqual(json.loads((root / "honcho.json").read_text())["baseUrl"], env["HONCHO_BASE_URL"])
            self.assertEqual(ports, {"memory-0": 19000, "memory-1": 19001})

    def test_bridge_requires_token_and_forwards_only_fixed_memory_origin(self):
        seen = []
        class Memory(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers["Authorization"], self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
            def log_message(self, *_args):
                pass
        memory = ThreadingHTTPServer(("127.0.0.1", 0), Memory)
        bridge = RemoteBridge([], {}, {"memory-0": f"http://127.0.0.1:{memory.server_port}"})
        for server in (memory, bridge):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for token, route, expected in (("wrong", "memory-0", False), (bridge.token, "unknown", False), (bridge.token, "memory-0", True)):
                with socket.create_connection(("127.0.0.1", bridge.server_address[1]), timeout=5) as client:
                    client.sendall((json.dumps({"token": token, "route": route}) + "\n").encode())
                    client.sendall(b"POST /v1/write?q=1 HTTP/1.1\r\nHost: ignored\r\nAuthorization: Bearer fixture\r\nContent-Length: 4\r\n\r\nbody")
                    try:
                        response = client.makefile("rb").read()
                    except ConnectionResetError:
                        response = b""
                    self.assertEqual(b"200 OK" in response, expected)
            self.assertEqual(seen, [("/v1/write?q=1", "Bearer fixture", b"body")])
        finally:
            bridge.stop()
            memory.shutdown()
            memory.server_close()

    def test_modal_export_and_cleanup_on_success_and_timeout(self):
        for exit_code in (0, -1):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                payload = root / "payload"
                payload.mkdir()
                output = root / "output"
                output.mkdir()
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w") as archive:
                    item = tarfile.TarInfo("receipt.json")
                    item.size = 2
                    archive.addfile(item, io.BytesIO(b"{}"))
                sandbox = MagicMock()
                def execute(*args, **kwargs):
                    return SimpleNamespace(wait=lambda: exit_code if args[0] == "python" else 0,
                                           stdout=io.StringIO("saved"), stderr=io.StringIO(""))
                sandbox.exec.side_effect = execute
                sandbox.filesystem.copy_to_local.side_effect = lambda remote, path: path.write_bytes(data.getvalue())
                @contextmanager
                def forward(_port):
                    yield SimpleNamespace(tls_socket=("fixture.modal.host", 443))
                with patch("reference.execution.modal_agent.modal.forward", side_effect=forward), \
                        patch("reference.execution.modal_agent.modal.Image.from_id"), \
                        patch("reference.execution.modal_agent.modal.App.lookup"), \
                        patch("reference.execution.modal_agent.modal.Sandbox.create", return_value=sandbox) as create:
                    runner = ModalAgentContainer("im-fixture", payload, ["hermes"], {"AGENT_KEY": "fixture"}, [], {})
                    if exit_code == -1:
                        with self.assertRaises(subprocess.TimeoutExpired):
                            runner.run(output, timeout=1)
                    else:
                        self.assertEqual(runner.run(output, timeout=1).returncode, 0)
                    self.assertNotIn("volumes", create.call_args.kwargs)
                    self.assertNotIn("secrets", create.call_args.kwargs)
                    sandbox.terminate.assert_called_once()
                    self.assertEqual((output / "receipt.json").read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
