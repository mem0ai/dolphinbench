"""Agent-side MCP connection and loopback forwards for selected memory services."""

import json
import os
from pathlib import Path
import socket
import socketserver
import ssl
import subprocess
import sys
import threading


CONFIG = Path("/payload/bridge.json")


def connect(route):
    config = json.loads(CONFIG.read_text())
    host, port = config["address"]
    raw = socket.create_connection((host, port), timeout=30)
    try:
        stream = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    except BaseException:
        raw.close()
        raise
    stream.sendall((json.dumps({"token": config["token"], "route": route}) + "\n").encode())
    stream.settimeout(None)
    return stream


def mcp():
    with connect("apps") as connection:
        def send():
            try:
                while data := os.read(sys.stdin.fileno(), 65536):
                    connection.sendall(data)
                connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        threading.Thread(target=send, daemon=True).start()
        while data := connection.recv(65536):
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()


class MemoryForward(socketserver.BaseRequestHandler):
    def handle(self):
        with connect(self.server.route) as upstream:
            def send():
                try:
                    while data := self.request.recv(65536):
                        upstream.sendall(data)
                except OSError:
                    pass
            thread = threading.Thread(target=send, daemon=True)
            thread.start()
            try:
                while data := upstream.recv(65536):
                    self.request.sendall(data)
            except OSError:
                pass
            finally:
                self.request.shutdown(socket.SHUT_RDWR)
                thread.join(timeout=5)


def main():
    if sys.argv[1] == "mcp":
        mcp()
        return
    servers = []
    try:
        for route, port in json.loads(CONFIG.read_text())["ports"].items():
            server = socketserver.ThreadingTCPServer(("127.0.0.1", port), MemoryForward)
            server.daemon_threads = True
            server.route = route
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
        raise SystemExit(subprocess.call(sys.argv[1:]))
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
