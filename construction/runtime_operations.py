"""Deterministic simulated-app operations used by the canonical quarter runtime."""

from __future__ import annotations

import copy
import atexit
import json
import os
from harness.environment import get_setting
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

OPERATION_RESULT_REFERENCE_KEY = "$operation_result"
OPERATION_RESULT_REFERENCE_FIELDS = {
    "contact_id",
    "operation_index",
    "field",
}


def _reference_shape(value: Any) -> tuple[str, int, str] | None:
    if not isinstance(value, dict) or set(value) != {OPERATION_RESULT_REFERENCE_KEY}:
        return None
    payload = value[OPERATION_RESULT_REFERENCE_KEY]
    if (
        not isinstance(payload, dict)
        or set(payload) != OPERATION_RESULT_REFERENCE_FIELDS
    ):
        return None
    contact_id = payload.get("contact_id")
    operation_index = payload.get("operation_index")
    field = payload.get("field")
    if not isinstance(contact_id, str) or not contact_id.strip():
        return None
    if (
        isinstance(operation_index, bool)
        or not isinstance(operation_index, int)
        or operation_index < 0
    ):
        return None
    if not isinstance(field, str) or not field.strip():
        return None
    return contact_id, operation_index, field


def validate_operation_result_references(
    args: dict[str, Any],
    *,
    label: str,
    current_contact_id: str,
    current_operation_index: int,
    contact_positions: dict[str, int],
    operation_counts: dict[str, int],
) -> list[str]:
    """Validate direct argument references against the ordered weekly plan."""
    errors: list[str] = []

    def visit(value: Any, path: str, *, direct_argument_value: bool) -> None:
        if isinstance(value, dict):
            if OPERATION_RESULT_REFERENCE_KEY in value:
                if not direct_argument_value:
                    errors.append(f"{path} contains a nested operation-result reference")
                    return
                shape = _reference_shape(value)
                if shape is None:
                    errors.append(
                        f"{path} must be exactly a valid $operation_result object"
                    )
                    return
                contact_id, referenced_index, _field = shape
                current_position = contact_positions.get(current_contact_id)
                referenced_position = contact_positions.get(contact_id)
                if referenced_position is None:
                    errors.append(f"{path} references missing contact {contact_id!r}")
                    return
                if current_position is None or referenced_position > current_position:
                    errors.append(f"{path} references a forward contact {contact_id!r}")
                    return
                if (
                    referenced_position == current_position
                    and referenced_index >= current_operation_index
                ):
                    errors.append(
                        f"{path} references a forward operation {referenced_index} "
                        f"in contact {contact_id!r}"
                    )
                    return
                if referenced_index >= operation_counts.get(contact_id, 0):
                    errors.append(
                        f"{path} references missing operation {referenced_index} "
                        f"in contact {contact_id!r}"
                    )
                return
            for key, item in value.items():
                visit(item, f"{path}.{key}", direct_argument_value=False)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]", direct_argument_value=False)

    for argument_name, value in args.items():
        visit(value, f"{label}.args.{argument_name}", direct_argument_value=True)
    return list(dict.fromkeys(errors))


def _resolve_operation_result_references(
    args: dict[str, Any],
    *,
    label: str,
    prior_results: dict[tuple[str, int], dict[str, Any]],
) -> dict[str, Any]:
    """Resolve only validated direct argument references before tool execution."""

    def resolve(value: Any, path: str, *, direct_argument_value: bool) -> Any:
        if isinstance(value, dict):
            if OPERATION_RESULT_REFERENCE_KEY in value:
                if not direct_argument_value:
                    raise ValueError(f"{path} contains a nested operation-result reference")
                shape = _reference_shape(value)
                if shape is None:
                    raise ValueError(f"{path} must be exactly a valid $operation_result object")
                contact_id, operation_index, field = shape
                row = prior_results.get((contact_id, operation_index))
                if row is None:
                    raise ValueError(
                        f"{path} references missing earlier operation "
                        f"{operation_index} in contact {contact_id!r}"
                    )
                result = row.get("result")
                if not isinstance(result, dict) or field not in result:
                    raise ValueError(
                        f"{path} references operation {operation_index} in contact "
                        f"{contact_id!r}, but result field {field!r} is missing"
                    )
                return copy.deepcopy(result[field])
            return {
                key: resolve(item, f"{path}.{key}", direct_argument_value=False)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                resolve(item, f"{path}[{index}]", direct_argument_value=False)
                for index, item in enumerate(value)
            ]
        return copy.deepcopy(value)

    return {
        key: resolve(value, f"{label}.args.{key}", direct_argument_value=True)
        for key, value in args.items()
    }


class _MockToolProcess:
    """Keep the isolated mock-tool interpreter alive for one construction run."""

    def __init__(self, command: list[str]) -> None:
        self.process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def request(self, batch: list[dict[str, Any]]) -> tuple[str, int, str]:
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("mock-tool process pipes are unavailable")
        self.process.stdin.write(json.dumps(batch, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        response = self.process.stdout.readline()
        if response:
            return response, self.process.poll() or 0, ""
        stderr = ""
        if self.process.stderr is not None:
            stderr = self.process.stderr.read()
        return "", self.process.poll() or 1, stderr

    def close(self) -> None:
        if self.process.poll() is None and self.process.stdin is not None:
            self.process.stdin.close()
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


_MOCK_TOOL_PROCESSES: dict[tuple[str, str, str], _MockToolProcess] = {}


def _close_mock_tool_processes() -> None:
    for process in _MOCK_TOOL_PROCESSES.values():
        process.close()
    _MOCK_TOOL_PROCESSES.clear()


atexit.register(_close_mock_tool_processes)


def _mock_tool_process(
    *, persona: str, state_path: Path, log_path: Path, tool_python: Path
) -> _MockToolProcess:
    run_id = get_setting("DOLPHINBENCH_CONSTRUCTION_RUN_ID", "")
    key = (str(tool_python.resolve()), persona, run_id)
    process = _MOCK_TOOL_PROCESSES.get(key)
    if process is not None and process.process.poll() is not None:
        process.close()
        del _MOCK_TOOL_PROCESSES[key]
        process = None
    if process is None:
        process = _MockToolProcess(
            [
                str(tool_python),
                "-m",
                "construction.execute_mock_tool",
                "--persona",
                persona,
                "--state",
                str(state_path),
                "--log",
                str(log_path),
                "--batch",
            ]
        )
        _MOCK_TOOL_PROCESSES[key] = process
    return process


def execute_operations(
    *,
    persona: str,
    plans: list[dict[str, Any]],
    state_path: Path,
    log_path: Path,
    tool_python: Path,
    deterministic_replay: bool = False,
    replay_epoch_ms_by_operation: dict[tuple[str, int], int] | None = None,
) -> list[dict[str, Any]]:
    batch: list[dict[str, Any]] = []
    for session in plans:
        for local_operation_index, operation in enumerate(
            session.get("app_operations") or []
        ):
            contact_id = str(session.get("contact_id") or "")
            operation = copy.deepcopy(operation)
            mapping_key = (str(session["session_id"]), local_operation_index)
            batch_operation = {
                "contact_id": contact_id,
                "operation_index": local_operation_index,
                "session_id": str(session["session_id"]),
                "narrative_datetime": str(session["narrative_date"]),
                "state_path": str(state_path),
                "log_path": str(log_path),
                "tool": str(operation["tool"]),
                "args": operation["args"],
                "deterministic_replay": deterministic_replay,
            }
            if deterministic_replay and replay_epoch_ms_by_operation is not None:
                batch_operation["replay_epoch_ms_provided"] = True
                batch_operation["replay_epoch_ms"] = replay_epoch_ms_by_operation.get(
                    mapping_key
                )
            batch.append(batch_operation)

    if not batch:
        return []

    process = _mock_tool_process(
        persona=persona,
        state_path=state_path,
        log_path=log_path,
        tool_python=tool_python,
    )
    completed_stdout, completed_returncode, completed_stderr = process.request(batch)
    payload: Any = None
    try:
        payload = json.loads(completed_stdout.strip())
    except json.JSONDecodeError:
        pass

    if not isinstance(payload, list):
        first = batch[0]
        return [
            {
                "contact_id": first["contact_id"],
                "operation_index": first["operation_index"],
                "session_id": first["session_id"],
                "operation": {
                    "tool": first["tool"],
                    "args": first["args"],
                },
                "exit_code": completed_returncode,
                "result": None,
                "stdout": completed_stdout.strip(),
                "stderr": completed_stderr.strip(),
            }
        ]

    results: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        configuration_error = item.get("configuration_error")
        if configuration_error:
            raise ValueError(str(configuration_error))
        row = {
            "contact_id": item["contact_id"],
            "operation_index": item["operation_index"],
            "session_id": item["session_id"],
            "operation": item["operation"],
            "exit_code": item.get("exit_code", completed_returncode),
            "result": item.get("result"),
            "stdout": item.get("stdout", ""),
            "stderr": item.get("stderr", "") or "",
        }
        replay_epoch_ms = item.get("replay_epoch_ms")
        if replay_epoch_ms is not None:
            row["replay_epoch_ms"] = replay_epoch_ms
        results.append(row)
        if row["exit_code"] != 0 or (
            isinstance(row["result"], dict) and row["result"].get("error")
        ):
            return results
    return results


def validate_tool_python(tool_python: Path) -> None:
    """Fail before generation if app operations cannot load the mock tool server."""
    completed = subprocess.run(
        [str(tool_python), "-c", "from mcp.server.fastmcp import FastMCP"],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{tool_python} cannot import the repository's FastMCP server API; "
            "install requirements.txt in that environment or pass --tool-python"
        )


def operation_errors(results: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for row in results:
        result = row.get("result")
        failed = row["exit_code"] != 0 or (
            isinstance(result, dict)
            and (result.get("error") or result.get("ok") is False)
        )
        if failed:
            errors.append(
                f"{row['session_id']} {row['operation']['tool']} failed: "
                f"{row['stderr'] or row['stdout']}"
            )
            continue
        if isinstance(result, dict) and result.get("status") == "unknown":
            errors.append(
                f"{row['session_id']} {row['operation']['tool']} returned no matching record: "
                f"{row['stdout']}"
            )
    return errors
