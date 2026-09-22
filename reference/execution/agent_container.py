"""Run an agent without mounting controller files or inheriting its environment."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
import shutil
import socketserver
import subprocess
import tarfile
import tempfile
import threading
from uuid import uuid4


def image_backend(image):
    if isinstance(image, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        return "local-docker"
    if isinstance(image, str) and re.fullmatch(r"im-[A-Za-z0-9]+", image):
        return "modal-sandbox"
    raise ValueError("Use an immutable Docker sha256 image ID or Modal im- image ID")


class AppBridge(socketserver.ThreadingUnixStreamServer):
    """Forward the agent's MCP bytes to the controller-owned stdio server."""

    daemon_threads = True
    block_on_close = False

    def __init__(self, path: Path, command: list[str], env: dict[str, str]):
        self.command, self.env = command, env
        self.processes = set()
        self.lock = threading.Lock()
        super().__init__(str(path), AppConnection)
        os.chmod(path, 0o666)

    def stop(self):
        self.shutdown()
        with self.lock:
            processes = list(self.processes)
        for process in processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self.server_close()


class AppConnection(socketserver.BaseRequestHandler):
    def handle(self):
        process = subprocess.Popen(
            self.server.command, env=self.server.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=None,
        )
        with self.server.lock:
            self.server.processes.add(process)

        def send():
            try:
                while data := process.stdout.read1(65536):
                    self.request.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    self.request.shutdown(1)
                except OSError:
                    pass

        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        try:
            while data := self.request.recv(65536):
                process.stdin.write(data)
                process.stdin.flush()
        except OSError:
            pass
        finally:
            process.stdin.close()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            sender.join(timeout=5)
            process.stdout.close()
            with self.server.lock:
                self.server.processes.discard(process)


def extract_files(archive, destination: Path, *, max_bytes=2 * 1024**3):
    """Accept regular output files, never links, devices, or escaping paths."""
    total = 0
    with tarfile.open(fileobj=archive, mode="r|*") as stream:
        for item in stream:
            path = PurePosixPath(item.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Agent output contains an unsafe path")
            if not (item.isdir() or item.isfile()):
                raise ValueError("Agent output contains a link or special file")
            total += item.size
            if total > max_bytes:
                raise ValueError("Agent output exceeds the size limit")
            target = destination.joinpath(*path.parts)
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(item) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)


class AgentContainer:
    """One local Docker container. The sole host mount is the MCP socket."""

    def __init__(self, image: str, payload: Path, command: list[str],
                 environment: dict[str, str], app_command: list[str],
                 app_environment: dict[str, str], *, network="bridge"):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("Use a resolved Docker image ID, not a mutable tag")
        if os.environ.get("DOCKER_HOST") or os.environ.get("DOCKER_CONTEXT"):
            raise ValueError("The agent boundary requires the local default Docker daemon")
        if network not in {"bridge", "none"}:
            raise ValueError("Host and shared container networking are not supported")
        self.image, self.payload, self.command = image, payload, command
        self.environment = dict(environment)
        self.app_command, self.app_environment = app_command, app_environment
        self.network = network
        self.output_valid = False
        self.name = "dolphinbench-agent-" + uuid4().hex

    def run(self, output: Path, timeout=None):
        # No profiles, repository paths, Docker socket, or host environment mounts.
        with tempfile.TemporaryDirectory(prefix="db-app-") as raw:
            socket = Path(raw) / "apps.sock"
            bridge = AppBridge(socket, self.app_command, self.app_environment)
            thread = threading.Thread(target=bridge.serve_forever, daemon=True)
            thread.start()
            created = False
            try:
                args = ["docker", "--context", "default", "create", "--name", self.name,
                        "--network", self.network, "--cap-drop=ALL",
                        "--security-opt=no-new-privileges", "--pids-limit=512",
                        "--memory=8g", "--cpus=2", "--workdir=/payload/work",
                        "--mount", f"type=bind,source={socket},target=/run/apps.sock,readonly"]
                for name, value in self.environment.items():
                    if not name or "=" in name or "\x00" in name + value:
                        raise ValueError("Invalid agent environment entry")
                    args += ["--env", f"{name}={value}"]
                args += ["--entrypoint", self.command[0], self.image, *self.command[1:]]
                subprocess.run(args, check=True, capture_output=True, text=True)
                created = True
                subprocess.run(["docker", "--context", "default", "cp", str(self.payload) + "/.", self.name + ":/payload"],
                               check=True, capture_output=True)
                try:
                    result = subprocess.run(["docker", "--context", "default", "start", "--attach", self.name],
                                            capture_output=True, text=True, timeout=timeout)
                except subprocess.TimeoutExpired:
                    subprocess.run(["docker", "--context", "default", "kill", self.name], capture_output=True)
                    raise
                finally:
                    # Preserve output even after an interrupted agent turn.
                    copied = subprocess.Popen(["docker", "--context", "default", "cp", self.name + ":/payload/output/.", "-"],
                                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    try:
                        extract_files(copied.stdout, output)
                        if copied.wait(timeout=60):
                            raise RuntimeError("Cannot preserve the agent's output")
                        self.output_valid = True
                    finally:
                        copied.stdout.close()
                        if copied.poll() is None:
                            copied.kill()
                            copied.wait()
                return result
            finally:
                if created:
                    subprocess.run(["docker", "--context", "default", "rm", "--force", self.name], capture_output=True)
                bridge.stop()
                thread.join(timeout=5)
