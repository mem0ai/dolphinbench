"""Execute approved initial-history request envelopes with resumable caches."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import jsonschema

from construction.llm import AzureJsonClient
from construction.runtime_model_calls import cached_client_complete


REQUEST_KEYS = {"stage", "model", "system", "response_schema", "input"}
SAFE_STAGE = re.compile(r"^[A-Za-z0-9_]+$")


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _request_path(run: Path, filename: str) -> Path:
    if not isinstance(filename, str):
        raise ValueError("request must be a plain JSON filename")
    request_dir = run / "requests"
    candidate = Path(filename)
    if (
        not filename
        or candidate.is_absolute()
        or candidate.name != filename
        or "\\" in filename
        or candidate.suffix != ".json"
    ):
        raise ValueError("request must be a plain JSON filename under run/requests")
    path = request_dir / filename
    if path.resolve().parent != request_dir.resolve():
        raise ValueError("request must be a plain JSON filename under run/requests")
    if not path.is_file():
        raise ValueError(f"missing prepared request: {path}")
    return path


def _validate_request(run: Path, filename: str) -> tuple[Path, dict[str, Any]]:
    path = _request_path(run, filename)
    request = _load_object(path)
    if set(request) != REQUEST_KEYS:
        raise ValueError(f"invalid request envelope for {filename}")

    stage = request["stage"]
    if not isinstance(stage, str) or not stage:
        raise ValueError(f"invalid stage in request envelope for {filename}")
    if SAFE_STAGE.fullmatch(stage) is None:
        raise ValueError(f"unsafe stage in request envelope for {filename}")
    if not isinstance(request["model"], str):
        raise ValueError(f"{filename} model must be a string")
    if not isinstance(request["system"], str):
        raise ValueError(f"{filename} system must be a string")
    if not isinstance(request["response_schema"], dict):
        raise ValueError(f"{filename} response_schema must be an object")
    if not isinstance(request["input"], dict):
        raise ValueError(f"{filename} input must be an object")
    try:
        jsonschema.Draft202012Validator.check_schema(request["response_schema"])
    except jsonschema.exceptions.SchemaError as exc:
        raise ValueError(f"{filename} response_schema is invalid") from exc
    return path, request


def _execute_one(run: Path, request: dict[str, Any]) -> dict[str, Any]:
    stage = request["stage"]
    cache_path = run / "cache" / f"{stage}.json"
    response = cached_client_complete(
        cache_path,
        request["system"],
        request["input"],
        AzureJsonClient(model=request["model"], reasoning_effort="high"),
        response_schema=request["response_schema"],
        response_schema_name=stage,
    )
    if not isinstance(response, dict):
        raise ValueError(f"response for {stage} must be an object")
    body = {
        key: value for key, value in response.items() if key not in {"_usage", "_response_id"}
    }
    jsonschema.Draft202012Validator(request["response_schema"]).validate(body)
    response_path = run / "responses" / f"{stage}.json"
    response_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    receipt_path = run / "receipts" / f"{stage}.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(
            {
                "stage": stage,
                "model": request["model"],
                "cache_path": str(cache_path),
                "response_id": response.get("_response_id"),
                "usage": response.get("_usage") or {},
                "validated": True,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"stage": stage, "response": str(response_path), "validated": True}


def execute(*, run: Path, requests: list[str], confirmed: bool) -> list[dict[str, Any]]:
    if not confirmed:
        raise ValueError("paid calls require --confirm-paid-calls")
    if len(requests) != len(set(requests)):
        raise ValueError("each request may be supplied only once")

    prepared: list[dict[str, Any]] = []
    stages: set[str] = set()
    for filename in requests:
        _, request = _validate_request(run, filename)
        stage = request["stage"]
        if stage in stages:
            raise ValueError("each stage may be requested only once")
        stages.add(stage)
        prepared.append(request)

    return [_execute_one(run, request) for request in prepared]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument(
        "--request",
        action="append",
        required=True,
        help="prepared JSON filename under <run>/requests; repeat for sequential execution",
    )
    parser.add_argument("--confirm-paid-calls", action="store_true")
    args = parser.parse_args(argv)
    print(
        json.dumps(
            execute(run=args.run, requests=args.request, confirmed=args.confirm_paid_calls),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
