"""Explicit no-model Modal check; creates temporary compute, never real stores."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import modal


ROOT = Path("/repo") if Path("/repo/mock_mcp").is_dir() else Path(__file__).resolve().parents[2]
app = modal.App("dolphinbench-agent-boundary-check")
image = (modal.Image.debian_slim(python_version="3.11")
         .pip_install("mcp==1.29.0", "pyyaml==6.0.3", "requests")
         .add_local_dir(str(ROOT / "reference"), "/repo/reference", copy=True,
                        ignore=["**/__pycache__/**", "**/*.pyc"])
         .add_local_dir(str(ROOT / "mock_mcp"), "/repo/mock_mcp", copy=True)
         .add_local_file(str(ROOT / "harness/__init__.py"), "/repo/harness/__init__.py", copy=True)
         .add_local_file(str(ROOT / "harness/environment.py"), "/repo/harness/environment.py", copy=True)
         .env({"PYTHONPATH": "/repo"}))


def run_check(agent_image: str):
    from reference.execution.modal_agent import ModalAgentContainer

    class Memory(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/memory/roundtrip?q=1"
            assert self.headers["Authorization"] == "Bearer fixture-only"
            body = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    memory = ThreadingHTTPServer(("127.0.0.1", 0), Memory)
    threading.Thread(target=memory.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            hidden = root / "controller-only-grades.txt"
            hidden.write_text("must not reach the agent")
            payload = root / "payload"
            (payload / "work").mkdir(parents=True)
            (payload / "output").mkdir()
            shutil.copyfile("/repo/reference/execution/agent_bridge_client.py", payload / "agent_bridge_client.py")
            state = root / "apps.json"
            state.write_text('{"docs": []}')
            script = '''import asyncio,json,os,pathlib,subprocess,urllib.request
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
assert not pathlib.Path(HIDDEN).exists()
assert not pathlib.Path('/repo/mock_mcp').exists()
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not any(k in os.environ for k in ('CONTROLLER_SECRET','MODAL_TOKEN_ID','MODAL_TOKEN_SECRET'))
assert subprocess.check_output(['sh','-c','printf terminal-works']).decode()=='terminal-works'
assert b'usage' in subprocess.check_output(['hermes','--help'],stderr=subprocess.STDOUT).lower()
request=urllib.request.Request('http://127.0.0.1:19000/memory/roundtrip?q=1',data=b'fixture-body',headers={'Authorization':'Bearer fixture-only'})
assert urllib.request.urlopen(request).read()==b'fixture-body'
async def check():
 async with stdio_client(StdioServerParameters(command='python',args=['/payload/agent_bridge_client.py','mcp'])) as (r,w):
  async with ClientSession(r,w) as client:
   await client.initialize()
   result=await client.call_tool('create_doc',{'title':'isolated-modal-test','body':'hello'})
   assert not result.isError
asyncio.run(check())
pathlib.Path('/payload/output/proof.json').write_text(json.dumps({'terminal':True,'hermes_cli':True,'memory_roundtrip':True,'apps':True,'controller_files_absent':True,'controller_credentials_absent':True}))
'''.replace("HIDDEN", repr(str(hidden)))
            (payload / "check.py").write_text(script)
            output = root / "returned"
            output.mkdir()
            runner = ModalAgentContainer(agent_image, payload, ["python", "/payload/check.py"], {},
                [sys.executable, "/repo/mock_mcp/server.py"],
                {**os.environ, "CONTROLLER_SECRET": "fixture-secret", "DOLPHINBENCH_PERSONA": "morgan",
                 "DOLPHINBENCH_STATE_PATH": str(state), "DOLPHINBENCH_LOG_PATH": str(root / "calls.jsonl")},
                endpoints={"memory-0": f"http://127.0.0.1:{memory.server_port}"}, ports={"memory-0": 19000})
            result = runner.run(output, timeout=120)
            if result.returncode:
                raise RuntimeError(f"Sandbox check failed: {result.stdout}\n{result.stderr}")
            assert "isolated-modal-test" in state.read_text()
            proof = json.loads((output / "proof.json").read_text())
            interrupted = root / "interrupted"
            interrupted.mkdir()
            runner = ModalAgentContainer(agent_image, payload,
                ["python", "-c", "from pathlib import Path; import time; Path('/payload/output/partial.txt').write_text('saved'); time.sleep(30)"],
                {}, [sys.executable, "/repo/mock_mcp/server.py"], dict(os.environ))
            try:
                runner.run(interrupted, timeout=3)
            except subprocess.TimeoutExpired:
                assert runner.output_valid
                assert (interrupted / "partial.txt").read_text() == "saved"
                proof["timeout_output_saved"] = True
            else:
                raise AssertionError("The timed-out command must not count as a completed turn")
            return proof
    finally:
        memory.shutdown()
        memory.server_close()


@app.function(image=image, timeout=600, cpu=1, memory=2048)
def check(agent_image: str):
    # Production launches reference.evaluate and the driver as subprocesses.
    result = subprocess.run([sys.executable, "/repo/reference/tests/modal_agent_smoke.py", "--child", agent_image],
                            capture_output=True, text=True, timeout=480)
    if result.returncode:
        raise RuntimeError(f"Controller subprocess check failed: {result.stdout}\n{result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1])


@app.local_entrypoint()
def main(agent_image: str):
    print(json.dumps(check.remote(agent_image), sort_keys=True))


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--child":
    print(json.dumps(run_check(sys.argv[2]), sort_keys=True))
