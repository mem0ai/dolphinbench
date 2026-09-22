"""Read-only stdio boundary around the unchanged official Hindsight MCP server."""

from __future__ import annotations

import json
import subprocess
import sys
import threading


READ_TOOLS = frozenset({
    "hindsight_search_knowledge_pages", "hindsight_list_knowledge_pages",
    "hindsight_read_knowledge_page", "hindsight_reflect",
})


def permits_message(message: dict) -> bool:
    method = message.get("method")
    if method == "tools/call":
        return (message.get("params") or {}).get("name") in READ_TOOLS
    return method in {
        "initialize", "ping", "tools/list", "notifications/initialized",
        "notifications/cancelled",
    }


def filter_tool_list(message: dict) -> dict:
    result = message.get("result")
    if isinstance(result, dict) and "tools" in result:
        result["tools"] = [tool for tool in result["tools"] if tool.get("name") in READ_TOOLS]
    return message


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Expected the official dist/mcp-server.js path")
    child = subprocess.Popen(
        ["node", sys.argv[1]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1,
    )
    lock = threading.Lock()
    pending_lists = set()

    def emit(message: dict) -> None:
        with lock:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    def receive() -> None:
        for line in child.stdout:
            message = json.loads(line)
            # Only tools/list responses are changed; retrieval results are untouched.
            with lock:
                is_list = message.get("id") in pending_lists
                pending_lists.discard(message.get("id"))
            emit(filter_tool_list(message) if is_list else message)

    reader = threading.Thread(target=receive, daemon=True)
    reader.start()
    try:
        for line in sys.stdin:
            message = json.loads(line)
            if not permits_message(message):
                if "id" in message:
                    emit({"jsonrpc": "2.0", "id": message["id"], "error": {
                        "code": -32601, "message": "Only Hindsight retrieval is enabled",
                    }})
                continue
            if message.get("method") == "tools/list":
                with lock:
                    pending_lists.add(message["id"])
            child.stdin.write(line)
            child.stdin.flush()
    finally:
        child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait(timeout=5)
        reader.join(timeout=5)
        child.stdout.close()


if __name__ == "__main__":
    main()
