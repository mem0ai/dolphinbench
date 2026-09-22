"""Run one Hermes turn in a Modal Sandbox without controller files or secrets."""

import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import threading

import modal

from reference.execution.agent_bridge import RemoteBridge
from reference.execution.agent_container import extract_files


class ModalAgentContainer:
    def __init__(self, image, payload, command, environment, app_command, app_environment,
                 *, endpoints=None, ports=None):
        self.image, self.payload, self.command = image, payload, command
        self.environment = environment
        self.app_command, self.app_environment = app_command, app_environment
        self.endpoints, self.ports = endpoints or {}, ports or {}
        self.output_valid = False

    def run(self, output: Path, timeout=None):
        bridge = RemoteBridge(self.app_command, self.app_environment, self.endpoints)
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        sandbox = None
        try:
            with modal.forward(bridge.server_address[1]) as tunnel:
                (self.payload / "bridge.json").write_text(json.dumps({
                    "address": tunnel.tls_socket, "token": bridge.token, "ports": self.ports,
                }))
                sandbox = modal.Sandbox.create(
                    "sleep", "86400", image=modal.Image.from_id(self.image),
                    app=modal.App.lookup("dolphinbench-agent-runtime", create_if_missing=True),
                    timeout=86400, cpu=2, memory=8192,
                )
                # Neither benchmark volumes nor Modal/provider secrets are mounted.
                with tempfile.TemporaryDirectory(prefix="db-upload-") as raw:
                    archive = Path(raw) / "input.tar"
                    with tarfile.open(archive, mode="w") as files:
                        files.add(self.payload, arcname="payload")
                    sandbox.filesystem.copy_from_local(archive, "/tmp/input.tar")
                unpack = sandbox.exec("tar", "xf", "/tmp/input.tar", "-C", "/")
                if unpack.wait() != 0:
                    raise RuntimeError("Cannot upload the agent profile to the Modal Sandbox")
                sandbox.exec("rm", "/tmp/input.tar").wait()
                try:
                    process = sandbox.exec(
                        "python", "/payload/agent_bridge_client.py", *self.command,
                        env=self.environment, workdir="/payload/work",
                        timeout=timeout or 85800,
                    )
                    stdout, stderr = [], []
                    readers = [threading.Thread(target=lambda stream=s, result=r: result.append(stream.read()))
                               for s, r in ((process.stdout, stdout), (process.stderr, stderr))]
                    for reader in readers:
                        reader.start()
                    try:
                        code = process.wait()
                    finally:
                        for reader in readers:
                            reader.join()
                    if code in {-1, 124}:
                        raise subprocess.TimeoutExpired(self.command, timeout or 85800,
                                                        output="".join(stdout), stderr="".join(stderr))
                    return subprocess.CompletedProcess(self.command, code, "".join(stdout), "".join(stderr))
                finally:
                    packed = sandbox.exec("tar", "cf", "/tmp/output.tar", "-C", "/payload/output", ".")
                    if packed.wait() != 0:
                        raise RuntimeError("Cannot preserve the agent's Modal output")
                    with tempfile.TemporaryDirectory(prefix="db-download-") as raw:
                        archive = Path(raw) / "output.tar"
                        sandbox.filesystem.copy_to_local("/tmp/output.tar", archive)
                        with archive.open("rb") as stream:
                            extract_files(stream, output)
                        self.output_valid = True
        finally:
            try:
                if sandbox is not None:
                    sandbox.terminate()
            finally:
                bridge.stop()
                thread.join(timeout=5)
