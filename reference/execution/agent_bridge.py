"""Authenticated access to one turn's apps and fixed memory endpoints."""

import hmac
from http.server import BaseHTTPRequestHandler
import json
import secrets
import socketserver
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

import requests

from reference.execution.agent_container import AppBridge, AppConnection


class RemoteBridge(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, command, env, endpoints):
        self.command, self.env = command, env
        self.endpoints = endpoints
        self.token = secrets.token_urlsafe(32)
        self.processes = set()
        self.lock = threading.Lock()
        super().__init__(("0.0.0.0", 0), BridgeConnection)

    stop = AppBridge.stop


class BridgeConnection(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(15)
        try:
            line = bytearray()
            while len(line) < 4096:
                byte = self.request.recv(1)
                if not byte:
                    return
                if byte == b"\n":
                    break
                line.extend(byte)
            else:
                return
            request = json.loads(line)
            token = request.get("token", "")
            if not isinstance(token, str) or not hmac.compare_digest(token, self.server.token):
                return
            route = request.get("route")
            self.request.settimeout(None)
            if route == "apps":
                AppConnection(self.request, self.client_address, self.server)
            elif route in self.server.endpoints:
                MemoryConnection(self.request, self.client_address,
                                 SimpleNamespace(endpoint=self.server.endpoints[route]))
        except (OSError, ValueError, TypeError, AttributeError):
            return


class MemoryConnection(BaseHTTPRequestHandler):
    """Forward HTTP to a fixed origin; never accept a destination from the agent."""

    protocol_version = "HTTP/1.1"
    HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                   "te", "trailer", "transfer-encoding", "upgrade", "host"}

    def log_message(self, *_args):
        pass

    def forward(self):
        self.close_connection = True
        if not self.path.startswith("/") or urlsplit(self.path).netloc:
            self.send_error(400, "Only origin-relative paths are supported")
            return
        if self.headers.get("Transfer-Encoding"):
            self.send_error(411, "Content-Length is required")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if not 0 <= length <= 64 * 1024**2:
            self.send_error(413)
            return
        headers = {k: v for k, v in self.headers.items() if k.lower() not in self.HOP_HEADERS}
        try:
            # The controller retains its existing private-network proxy settings.
            with requests.request(self.command, self.server.endpoint + self.path,
                                  headers=headers, data=self.rfile.read(length),
                                  allow_redirects=False, stream=True, timeout=(30, 7200)) as response:
                self.send_response(response.status_code)
                for key, value in response.headers.items():
                    if key.lower() not in self.HOP_HEADERS:
                        self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    for chunk in response.raw.stream(65536, decode_content=False):
                        self.wfile.write(chunk)
        except requests.RequestException:
            self.send_error(502, "Memory endpoint request failed")

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = forward
