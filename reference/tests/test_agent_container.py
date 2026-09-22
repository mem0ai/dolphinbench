import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from reference.execution.agent_container import AgentContainer, extract_files
from reference.execution.hermes_agent import copy_regular_tree, run_cli
from harness.hermes_driver import _validate_hermes_runtime_tools
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]


class ContainerBoundaryTests(unittest.TestCase):
    def test_new_hermes_modal_launch_stops_before_creating_workers(self):
        from reference.execution import modal
        with patch.object(modal, "_load_and_validate_suite", return_value=(SimpleNamespace(jobs=[]), {}, {"job": {}})), \
                patch.object(modal.matrix, "_manifest_agent_runtime", return_value="hermes"), \
                patch.object(modal, "_RealModalBridge") as bridge:
            with self.assertRaisesRegex(modal.ModalSuiteError, "Modal agent image"):
                modal.launch(Path("suite.json"), state_path=Path("state.json"), bundle_dir=Path("bundle"))
            bridge.assert_not_called()

    def test_launcher_stages_only_runtime_and_restores_outputs_not_config(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            profile = root / "morgan"
            profile.mkdir()
            original_config = 'mcp_servers:\n  dolphinbench-apps:\n    command: python\n    args: [controller-server.py]\n'
            (profile / "config.yaml").write_text(original_config)
            (profile / "state.db").write_bytes(b"before")
            (profile / "state.db-wal").write_bytes(b"old journal")
            (profile / ".env").write_text("CONTROLLER_SECRET=hidden")
            receipt = root / "receipt.json"
            receipt.write_text("before")
            environment = {
                "DOLPHINBENCH_AGENT_IMAGE": "sha256:" + "0" * 64,
                "OPENAI_API_KEY": "agent-key",
                "CONTROLLER_SECRET": "hidden",
                "DOLPHINBENCH_TURN_STATUS_PATH": str(receipt),
            }

            def container(image, payload, command, env, apps, app_env):
                self.assertEqual(env["OPENAI_API_KEY"], "agent-key")
                self.assertNotIn("CONTROLLER_SECRET", env)
                self.assertFalse((payload / "output/home/profiles/morgan/.env").exists())
                self.assertEqual(apps, ["python", "controller-server.py"])
                self.assertIn("/payload/app_client.py", (payload / "output/home/profiles/morgan/config.yaml").read_text())
                self.assertFalse((payload / "runtime/graders").exists())
                self.assertEqual(command[-2:], ["--query", "hello"])

                def run(output, timeout):
                    shutil.copytree(payload / "output", output, dirs_exist_ok=True)
                    returned = output / "home/profiles/morgan"
                    (returned / "config.yaml").write_text("untrusted returned config")
                    (returned / "state.db").write_bytes(b"after")
                    (returned / "state.db-wal").unlink()
                    (output / "files/DOLPHINBENCH_TURN_STATUS_PATH").write_text("after")
                    raise subprocess.TimeoutExpired(command, timeout)

                return SimpleNamespace(run=run, output_valid=True)

            with patch("reference.execution.hermes_agent.AgentContainer", side_effect=container):
                with self.assertRaises(subprocess.TimeoutExpired):
                    run_cli(["hermes", "-p", "morgan", "--query", "hello"],
                            profile="morgan", profile_path=profile, env=environment, timeout=1)
            self.assertEqual((profile / "config.yaml").read_text(), original_config)
            self.assertEqual((profile / "state.db").read_bytes(), b"after")
            self.assertFalse((profile / "state.db-wal").exists())
            self.assertEqual(receipt.read_text(), "after")

    def test_rejects_links_escape_paths_and_oversized_output(self):
        for name, kind, size in (("../secret", tarfile.REGTYPE, 0),
                                 ("/secret", tarfile.REGTYPE, 0),
                                 ("link", tarfile.SYMTYPE, 0),
                                 ("link", tarfile.LNKTYPE, 0),
                                 ("device", tarfile.CHRTYPE, 0),
                                 ("large", tarfile.REGTYPE, 16)):
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as raw:
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w") as archive:
                    item = tarfile.TarInfo(name)
                    item.type, item.size, item.linkname = kind, size, "/outside"
                    archive.addfile(item, io.BytesIO(b"x" * size))
                data.seek(0)
                with self.assertRaises(ValueError):
                    extract_files(data, Path(raw), max_bytes=8)

    def test_profile_copy_excludes_credentials_and_rejects_links(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            (source / ".env").write_text("SECRET=hidden")
            (source / "memory.txt").write_text("intended memory")
            copy_regular_tree(source, root / "copy")
            self.assertFalse((root / "copy/.env").exists())
            self.assertEqual((root / "copy/memory.txt").read_text(), "intended memory")
            (source / "link").symlink_to(root)
            with self.assertRaises(ValueError):
                copy_regular_tree(source, root / "second")

    def test_general_tools_are_allowed_but_test_memory_writes_are_not(self):
        tools = ["mcp__dolphinbench_apps__send_email", "session_search", "mem0_search",
                 "terminal", "read_file", "browser_navigate", "delegate_task"]
        with patch("harness.hermes_driver._is_dolphinbench_mock_tool_name", side_effect=lambda name: name.startswith("mcp__")):
            _validate_hermes_runtime_tools(SimpleNamespace(available_tools=tools), "test", "001", "mem0")
            for write in ("mem0_add", "save_memory", "mem0_delete"):
                with self.subTest(write=write), self.assertRaises(SystemExit):
                    _validate_hermes_runtime_tools(SimpleNamespace(available_tools=tools + [write]), "test", "001", "mem0")
            _validate_hermes_runtime_tools(SimpleNamespace(available_tools=tools + ["memory", "mem0_add"]), "seed", "001", "mem0")

    @unittest.skipUnless(os.environ.get("DOLPHINBENCH_CONTAINER_TEST_IMAGE"), "set an existing Python/MCP image ID for the local container test")
    def test_real_container_can_call_apps_but_cannot_read_controller(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            hidden = root / "hidden-grading-data.txt"
            hidden.write_text("controller-only")
            payload = root / "payload"
            (payload / "work").mkdir(parents=True)
            (payload / "output").mkdir()
            shutil.copyfile(ROOT / "reference/execution/app_client.py", payload / "app_client.py")
            state = root / "state.json"
            state.write_text(json.dumps({"docs": []}))
            script = '''import asyncio,json,os,pathlib,subprocess
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
assert not pathlib.Path(HIDDEN).exists()
assert not pathlib.Path('/var/run/docker.sock').exists()
assert 'CONTROLLER_SECRET' not in os.environ
assert subprocess.check_output(['sh','-c','printf real-terminal']).decode()=='real-terminal'
async def check():
 async with stdio_client(StdioServerParameters(command='python',args=['/payload/app_client.py'])) as (r,w):
  async with ClientSession(r,w) as client:
   await client.initialize()
   tools=await client.list_tools()
   assert 'create_doc' in [t.name for t in tools.tools]
   result=await client.call_tool('create_doc',{'title':'boundary-test','body':'hello'})
   assert not result.isError
   pathlib.Path('/payload/output/proof.json').write_text(json.dumps({'tools':len(tools.tools),'isolated':True}))
asyncio.run(check())
'''.replace("HIDDEN", repr(str(hidden)))
            (payload / "check.py").write_text(script)
            env = {**os.environ, "PYTHONPATH": str(ROOT), "DOLPHINBENCH_PERSONA": "morgan",
                   "DOLPHINBENCH_STATE_PATH": str(state), "DOLPHINBENCH_LOG_PATH": str(root / "calls.jsonl"),
                   "CONTROLLER_SECRET": "must-not-cross"}
            output = root / "returned"
            output.mkdir()
            container = AgentContainer(os.environ["DOLPHINBENCH_CONTAINER_TEST_IMAGE"], payload,
                                       ["python", "/payload/check.py"], {},
                                       [sys.executable, str(ROOT / "mock_mcp/server.py")], env,
                                       network="none")
            result = container.run(output, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads((output / "proof.json").read_text())["isolated"])
            self.assertIn("boundary-test", state.read_text())
            remaining = subprocess.run(["docker", "ps", "-a", "--filter", "name=" + container.name, "--format", "{{.Names}}"], capture_output=True, text=True, check=True)
            self.assertEqual(remaining.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
