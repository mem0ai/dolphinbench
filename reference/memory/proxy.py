"""Authenticated memory read proxies and model API compatibility."""

from __future__ import annotations

import hmac
import http.client
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, quote, unquote, urlsplit
from urllib.parse import urlsplit
import json
import time
import urllib.error
import urllib.request


LISTEN_PORT = 8889
UPSTREAM_PORT = 8888
MAX_REQUEST_BYTES = 1024 * 1024
HOP_HEADERS = frozenset({
    "connection", "content-length", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
})


def permits_request(method: str, target: str, bank_id: str) -> bool:
    if not bank_id:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in target):
        return False
    try:
        url = urlsplit(target)
    except ValueError:
        return False
    if url.scheme or url.netloc or url.fragment or "#" in target:
        return False
    prefix = f"/v1/default/banks/{quote(bank_id, safe='')}/"
    if method == "GET":
        if target in {"/health", "/version", prefix + "knowledge-base/tree"}:
            return True
        pages = prefix + "knowledge-base/pages/"
        if url.path.startswith(pages) and "?" not in target:
            page_id = unquote(url.path[len(pages):])
            return bool(page_id) and page_id not in {".", ".."} and not any(
                char in page_id for char in "/\\%"
            ) and all(ord(char) >= 32 and ord(char) != 127 for char in page_id)
        if url.path == prefix + "knowledge-base/search":
            try:
                pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
            except ValueError:
                return False
            params = dict(pairs)
            return (
                len(params) == len(pairs)
                and "q" in params
                and set(params) <= {"q", "limit"}
                and ("limit" not in params or (
                    params["limit"].isascii() and params["limit"].isdigit()
                    and bool(params["limit"].lstrip("0"))
                ))
            )
        return False
    if "?" in target:
        return False
    return method == "POST" and target in {
        prefix + "memories/recall", prefix + "reflect",
    }


class ReadOnlyHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self) -> None:
        key = os.environ.get("HINDSIGHT_API_KEY", "")
        if not key or not hmac.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {key}",
        ):
            self._reply(401, b'{"error":"Unauthorized"}')
            return
        if not permits_request(
            self.command, self.path, os.environ.get("DOLPHINBENCH_HINDSIGHT_BANK_ID", ""),
        ):
            self._reply(405, b'{"error":"Only retrieval is enabled for this frozen bank"}')
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            length = int(lengths[0]) if len(lengths) == 1 else 0
            if len(lengths) > 1 or self.headers.get("Transfer-Encoding") or not 0 <= length <= MAX_REQUEST_BYTES:
                raise ValueError("invalid request size")
        except ValueError:
            self._reply(400, b'{"error":"Invalid request length"}')
            return
        body = self.rfile.read(length) if length else None
        headers = {"Authorization": f"Bearer {key}", "Accept": self.headers.get("Accept", "application/json")}
        if content_type := self.headers.get("Content-Type"):
            headers["Content-Type"] = content_type
        connection = http.client.HTTPConnection("127.0.0.1", UPSTREAM_PORT, timeout=600)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read()
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in HOP_HEADERS:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (OSError, http.client.HTTPException):
            self._reply(502, b'{"error":"Hindsight is unavailable"}')
        finally:
            connection.close()

    do_GET = _forward
    do_POST = _forward
    do_PUT = _forward
    do_PATCH = _forward
    do_DELETE = _forward
    do_HEAD = _forward
    do_OPTIONS = _forward


def main() -> None:
    if not os.environ.get("HINDSIGHT_API_KEY") or not os.environ.get("DOLPHINBENCH_HINDSIGHT_BANK_ID"):
        raise SystemExit("Hindsight evaluation requires a credential and the frozen bank ID")
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), ReadOnlyHandler).serve_forever()


SUPERMEMORY_LISTEN_PORT = 6768
UPSTREAM_HOST = "127.0.0.1"
SUPERMEMORY_UPSTREAM_PORT = 6767
EXTERNAL_KEY_ENV = "SUPERMEMORY_PROXY_API_KEY"
UPSTREAM_KEY_ENV = "SUPERMEMORY_UPSTREAM_API_KEY"
READ_POST_PATHS = frozenset({"/v3/documents/list", "/v3/search", "/v4/search"})


def _bearer(headers: object) -> str:
    value = getattr(headers, "get")("Authorization", "")
    prefix = "Bearer "
    return value[len(prefix):].strip() if value.startswith(prefix) else ""


class ReadOnlyProxyHandler(BaseHTTPRequestHandler):
    server_version = "DolphinBenchSupermemoryProxy/1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = os.environ.get(EXTERNAL_KEY_ENV, "")
        supplied = _bearer(self.headers)
        return bool(expected and supplied and hmac.compare_digest(expected, supplied))

    def _forward(self) -> None:
        path = urlsplit(self.path).path
        if self.command == "GET" and path == "/health":
            pass
        elif self.command == "POST" and path in READ_POST_PATHS:
            if not self._authorized():
                self._reply(401, b'{"error":"Unauthorized"}')
                return
        else:
            self._reply(405, b'{"error":"This endpoint permits retrieval only"}')
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else None
        headers = {
            "Authorization": f"Bearer {os.environ[UPSTREAM_KEY_ENV]}",
            "Accept": self.headers.get("Accept", "application/json"),
        }
        if content_type := self.headers.get("Content-Type"):
            headers["Content-Type"] = content_type
        connection = http.client.HTTPConnection(UPSTREAM_HOST, SUPERMEMORY_UPSTREAM_PORT, timeout=180)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in {
                    "connection",
                    "content-length",
                    "keep-alive",
                    "proxy-authenticate",
                    "proxy-authorization",
                    "te",
                    "trailers",
                    "transfer-encoding",
                    "upgrade",
                }:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)
        except OSError:
            self._reply(502, b'{"error":"Supermemory is unavailable"}')
        finally:
            connection.close()

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_PUT(self) -> None:
        self._forward()

    def do_PATCH(self) -> None:
        self._forward()

    def do_DELETE(self) -> None:
        self._forward()


def supermemory_main() -> None:
    if not os.environ.get(EXTERNAL_KEY_ENV) or not os.environ.get(UPSTREAM_KEY_ENV):
        raise SystemExit("both proxy bearer keys are required")
    ThreadingHTTPServer(("0.0.0.0", SUPERMEMORY_LISTEN_PORT), ReadOnlyProxyHandler).serve_forever()


LISTEN_HOST = "127.0.0.1"
OPENAI_LISTEN_PORT = 6766


def record_usage(status: int, body: bytes) -> None:
    """Opt-in numeric usage evidence; never persist requests, replies, or credentials."""
    path = os.environ.get("DOLPHINBENCH_OPENAI_USAGE_LOG")
    if not path:
        return
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        payload = {}
    usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
    row = {"unix_seconds": time.time(), "status": status, "usage": {
        key: value for key, value in usage.items()
        if key in {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"}
        and isinstance(value, int) and not isinstance(value, bool)
    }} if isinstance(usage, dict) else {"unix_seconds": time.time(), "status": status, "usage": {}}
    with open(path, "a") as handle:
        handle.write(json.dumps(row) + "\n")


def translate_request(body: bytes) -> bytes:
    """Rename the legacy output-token field and leave everything else unchanged."""

    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(value, dict) or "max_tokens" not in value:
        return body
    if "max_completion_tokens" in value:
        raise ValueError("request contains both max_tokens and max_completion_tokens")
    value["max_completion_tokens"] = value.pop("max_tokens")
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


class ProxyHandler(BaseHTTPRequestHandler):
    """Forward OpenAI API traffic to the configured Azure endpoint."""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = translate_request(self.rfile.read(length))
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        upstream_base = os.environ["AZURE_OPENAI_UPSTREAM_BASE_URL"].rstrip("/")
        path = self.path.removeprefix("/v1")
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in {"host", "content-length", "connection"}
        }
        request = urllib.request.Request(
            upstream_base + path,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                self._send_upstream(response.status, response.headers, response.read())
        except urllib.error.HTTPError as error:
            self._send_upstream(error.code, error.headers, error.read())

    def _send_upstream(self, status: int, headers: object, body: bytes) -> None:
        record_usage(status, body)
        self.send_response(status)
        for name, value in headers.items():
            if name.lower() not in {"content-length", "transfer-encoding", "connection"}:
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def openai_main() -> None:
    if not os.environ.get("AZURE_OPENAI_UPSTREAM_BASE_URL", "").startswith("https://"):
        raise RuntimeError("AZURE_OPENAI_UPSTREAM_BASE_URL must be an https URL")
    ThreadingHTTPServer((LISTEN_HOST, OPENAI_LISTEN_PORT), ProxyHandler).serve_forever()


def serve(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=("hindsight", "supermemory", "openai"))
    args = parser.parse_args(argv)
    {"hindsight": main, "supermemory": supermemory_main, "openai": openai_main}[args.service]()


if __name__ == "__main__":
    serve()
