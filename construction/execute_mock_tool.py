"""Execute simulated app operations against an isolated state file."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import sys
import traceback


def _configure_environment(
    *,
    persona: str,
    state: str,
    log: str,
    session_id: str,
    narrative_datetime: str,
) -> None:
    os.environ["DOLPHINBENCH_PERSONA"] = persona
    os.environ["DOLPHINBENCH_STATE_PATH"] = state
    os.environ["DOLPHINBENCH_LOG_PATH"] = log
    os.environ["DOLPHINBENCH_RUN_ID"] = "history-extension"
    os.environ["DOLPHINBENCH_SESSION_ID"] = session_id
    os.environ["DOLPHINBENCH_NARRATIVE_DATETIME"] = narrative_datetime


def _parse_result(stdout: str):
    try:
        return json.loads(stdout.strip())
    except json.JSONDecodeError:
        return None


def _exception_text(error: BaseException) -> str:
    if isinstance(error, SystemExit):
        return str(error)
    return traceback.format_exc().strip()


def _run_batch(args, operations: list[dict], server=None) -> int:
    from construction.runtime_operations import _resolve_operation_result_references

    if server is None:
        _configure_environment(
            persona=args.persona,
            state=args.state,
            log=args.log,
            session_id="",
            narrative_datetime="",
        )
        server = importlib.import_module("mock_mcp.server")
        server.STATE_PATH = Path(args.state)
        server.LOG_PATH = Path(args.log)
        server.RUN_ID = os.environ["DOLPHINBENCH_RUN_ID"]
        server.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not hasattr(server, "_DOLPHINBENCH_ORIGINAL_TIME"):
        server._DOLPHINBENCH_ORIGINAL_TIME = server.time.time
    original_time = server._DOLPHINBENCH_ORIGINAL_TIME
    prior_results = {}
    replay_operation_index = 0
    rows = []

    for item in operations:
        contact_id = str(item.get("contact_id") or "")
        operation_index = item["operation_index"]
        session_id = str(item["session_id"])
        narrative_datetime = str(item["narrative_datetime"])
        _configure_environment(
            persona=args.persona,
            state=str(item.get("state_path") or args.state),
            log=str(item.get("log_path") or args.log),
            session_id=session_id,
            narrative_datetime=narrative_datetime,
        )
        server.STATE_PATH = Path(os.environ["DOLPHINBENCH_STATE_PATH"])
        server.LOG_PATH = Path(os.environ["DOLPHINBENCH_LOG_PATH"])
        server.RUN_ID = os.environ["DOLPHINBENCH_RUN_ID"]
        server.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        operation = {
            "tool": str(item["tool"]),
            "args": item["args"],
        }
        invoked = False
        try:
            if item.get("deterministic_replay"):
                if item.get("replay_epoch_ms_provided"):
                    replay_epoch_ms = item.get("replay_epoch_ms")
                    if (
                        isinstance(replay_epoch_ms, bool)
                        or not isinstance(replay_epoch_ms, int)
                    ):
                        raise ValueError(
                            "deterministic operation replay is missing replay_epoch_ms for "
                            f"session {session_id!r} operation {operation_index}"
                        )
                else:
                    from datetime import datetime

                    narrative_time = datetime.fromisoformat(narrative_datetime)
                    if narrative_time.tzinfo is None:
                        raise ValueError(
                            "deterministic operation replay requires offset-aware narrative datetimes"
                        )
                    replay_epoch_ms = (
                        int(narrative_time.timestamp() * 1000) + replay_operation_index
                    )
                server.time.time = lambda epoch_ms=replay_epoch_ms: epoch_ms / 1000
            else:
                replay_epoch_ms = None
                server.time.time = original_time

            operation["args"] = _resolve_operation_result_references(
                operation["args"],
                label=f"{contact_id} app_operations[{operation_index}]",
                prior_results=prior_results,
            )
            if operation["tool"] not in server._MANIFEST_TOOLS:
                raise SystemExit(
                    f"tool is not available to {args.persona}: {operation['tool']}"
                )
            function = getattr(server, operation["tool"], None)
            if not callable(function):
                raise SystemExit(f"unknown simulated tool: {operation['tool']}")
            invoked = True
            result_stdout = str(function(**operation["args"]))
            parsed = _parse_result(result_stdout)
            row = {
                "contact_id": contact_id,
                "operation_index": operation_index,
                "session_id": session_id,
                "operation": operation,
                "exit_code": 0,
                "result": parsed,
                "stdout": result_stdout,
                "stderr": "",
            }
            if replay_epoch_ms is not None:
                row["replay_epoch_ms"] = replay_epoch_ms
            rows.append(row)
            replay_operation_index += 1
            if isinstance(parsed, dict) and parsed.get("error"):
                break
            prior_results[(contact_id, operation_index)] = row
        except BaseException as error:
            rows.append(
                {
                    "contact_id": contact_id,
                    "operation_index": operation_index,
                    "session_id": session_id,
                    "operation": operation,
                    "exit_code": 1,
                    "result": None,
                    "stdout": "",
                    "stderr": _exception_text(error),
                    "configuration_error": (
                        str(error)
                        if not invoked and isinstance(error, ValueError)
                        else None
                    ),
                }
            )
            print(json.dumps(rows, ensure_ascii=False), flush=True)
            server.time.time = original_time
            return 1

    server.time.time = original_time
    print(json.dumps(rows, ensure_ascii=False), flush=True)
    return 0


def _run_batch_stream(args) -> int:
    _configure_environment(
        persona=args.persona,
        state=args.state,
        log=args.log,
        session_id="",
        narrative_datetime="",
    )
    server = importlib.import_module("mock_mcp.server")
    for line in sys.stdin:
        if not line.strip():
            continue
        operations = json.loads(line)
        if not isinstance(operations, list):
            raise ValueError("batch input must be a JSON list")
        _run_batch(args, operations, server=server)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--tool")
    parser.add_argument("--args")
    parser.add_argument("--batch", action="store_true")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--narrative-datetime", default="")
    parser.add_argument("--replay-epoch-ms", type=int)
    args = parser.parse_args()

    if args.batch:
        if args.tool is not None or args.args is not None:
            parser.error("--batch cannot be combined with --tool or --args")
        raise SystemExit(_run_batch_stream(args))

    if args.tool is None or args.args is None:
        parser.error("--tool and --args are required unless --batch is used")
    _configure_environment(
        persona=args.persona,
        state=args.state,
        log=args.log,
        session_id=args.session_id,
        narrative_datetime=args.narrative_datetime,
    )
    server = importlib.import_module("mock_mcp.server")
    if args.replay_epoch_ms is not None:
        server.time.time = lambda: args.replay_epoch_ms / 1000
    if args.tool not in server._MANIFEST_TOOLS:
        raise SystemExit(f"tool is not available to {args.persona}: {args.tool}")
    function = getattr(server, args.tool, None)
    if not callable(function):
        raise SystemExit(f"unknown simulated tool: {args.tool}")
    result = function(**json.loads(args.args))
    print(result)


if __name__ == "__main__":
    main()
