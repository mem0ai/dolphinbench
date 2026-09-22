#!/usr/bin/env python3
"""Run DolphinBench's four-shot source-session certification gate."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
SHOT = ROOT / 'authoring' / '_gate_shot.py'
MAX_TECHNICAL_RETRIES_PER_SHOT = 2


def gate_python() -> str:
    """Return the exact interpreter that certification subprocesses will use."""
    return os.environ.get("DOLPHINBENCH_GATE_PYTHON", sys.executable)


def verify_gate_python() -> str:
    """Verify the certification interpreter before a run creates paid work."""
    configured = os.environ.get("DOLPHINBENCH_GATE_PYTHON")
    executable = configured or sys.executable
    resolved = shutil.which(executable)
    if resolved is None:
        name = "DOLPHINBENCH_GATE_PYTHON" if configured else "the current Python"
        raise RuntimeError(
            f"{name} points to {executable!r}, but that executable does not exist or cannot run; "
            "use a Python executable that can run certification and import mcp"
        )
    try:
        result = subprocess.run(
            [resolved, "-c", "import mcp"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(
            f"the certification Python {resolved!r} could not run: {exc}; "
            "use a Python executable that can import mcp"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown import error"
        raise RuntimeError(
            f"the certification Python {resolved!r} cannot import mcp ({detail}); "
            "use a Python executable with mcp installed or set DOLPHINBENCH_GATE_PYTHON"
        )
    return resolved


def run_shot(
    candidate_path: Path,
    persona: str,
    with_memory: bool,
    work_dir: Path,
    index: int,
    checkpoint: Path | None = None,
) -> subprocess.Popen[str]:
    env = dict(
        os.environ,
        DOLPHINBENCH_PERSONA=persona,
        DOLPHINBENCH_STATE_PATH=str(work_dir / f"state_{index}.json"),
        DOLPHINBENCH_LOG_PATH=str(work_dir / f"calls_{index}.jsonl"),
    )
    if checkpoint is None:
        env.pop("DOLPHINBENCH_CHECKPOINT", None)
    else:
        env["DOLPHINBENCH_CHECKPOINT"] = str(checkpoint)
    return subprocess.Popen(
        [
            gate_python(),
            str(SHOT),
            str(candidate_path),
            persona,
            "1" if with_memory else "0",
            *([str(checkpoint)] if checkpoint is not None else []),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def parse_shot(process: subprocess.Popen[str], with_memory: bool) -> dict[str, Any]:
    try:
        stdout, stderr = process.communicate(timeout=900)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        return {"with_memory": with_memory, "passed": None, "error": "shot timed out"}
    line = next(
        (value for value in reversed(stdout.splitlines()) if value.strip().startswith("{")),
        None,
    )
    if not line:
        return {
            "with_memory": with_memory,
            "passed": None,
            "error": f"shot produced no result: {stderr[-500:]}",
        }
    result = json.loads(line)
    result["with_memory"] = with_memory
    return result


def _shot_has_infrastructure_error(shot: dict[str, Any]) -> bool:
    return bool(
        shot.get("error")
        or any(
            "LLM call failed" in str(error) or "reading mock log" in str(error)
            for error in shot.get("oracle_errors") or []
        )
    )


def _load_candidate(path: Path) -> dict[str, Any]:
    try:
        candidate = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load candidate {path}: {exc}") from exc
    if not isinstance(candidate, dict):
        raise ValueError(f"candidate {path} must contain an object")
    return candidate


def certify_candidate(
    persona: str,
    candidate_path: Path,
    *,
    checkpoint: Path | None = None,
    test_id: str | None = None,
    confirm_paid_calls: bool = False,
    oracle_input: dict[str, Any] | None = None,
    max_technical_retries: int = MAX_TECHNICAL_RETRIES_PER_SHOT,
) -> dict[str, Any]:
    if not confirm_paid_calls:
        raise ValueError("refusing certification calls without explicit confirmation")
    if type(max_technical_retries) is not int or not 0 <= max_technical_retries <= MAX_TECHNICAL_RETRIES_PER_SHOT:
        raise ValueError("technical retry limit must be an integer from zero through two")
    candidate_path = candidate_path.resolve()
    candidate = _load_candidate(candidate_path)
    if oracle_input is not None:
        if oracle_input.get("version") != 4:
            raise ValueError("unsupported certification input version")
        candidate["_oracle_input"] = copy.deepcopy(oracle_input)
    test_id = test_id or str(candidate.get("id") or candidate_path.stem)
    with tempfile.TemporaryDirectory(prefix=f"dolphinbench_gate_{persona}_{test_id}_") as tmp:
        work_dir = Path(tmp)
        shot_candidate_path = work_dir / "candidate.json"
        shot_candidate_path.write_text(json.dumps(candidate, default=str))
        conditions = [True, True, False, False]
        processes = [
            run_shot(
                shot_candidate_path,
                persona,
                with_memory,
                work_dir,
                index,
                checkpoint=checkpoint,
            )
            for index, with_memory in enumerate(conditions)
        ]
        shots = [
            parse_shot(process, with_memory)
            for process, with_memory in zip(processes, conditions)
        ]
        technical_retry_attempts: list[dict[str, Any]] = []
        for retry_number in range(1, max_technical_retries + 1):
            failed_slots = [
                index
                for index, shot in enumerate(shots)
                if _shot_has_infrastructure_error(shot)
            ]
            if not failed_slots:
                break
            retry_processes = [
                run_shot(
                    shot_candidate_path,
                    persona,
                    conditions[index],
                    work_dir,
                    retry_number * len(conditions) + index,
                    checkpoint=checkpoint,
                )
                for index in failed_slots
            ]
            retry_shots = [
                parse_shot(process, conditions[index])
                for process, index in zip(retry_processes, failed_slots, strict=True)
            ]
            for index, retry_shot in zip(failed_slots, retry_shots, strict=True):
                technical_retry_attempts.append(
                    {
                        "slot": index + 1,
                        "execution_number": retry_number,
                        "shot": shots[index],
                    }
                )
                shots[index] = retry_shot

    agent_inputs: dict[str, Any] = {}
    def normalize_shot(shot: dict[str, Any], *, attempt_id: str) -> None:
        shot["attempt_id"] = attempt_id
        agent_input = shot.pop("agent_input", None)
        if agent_input is not None:
            key = hashlib.sha256(json.dumps(agent_input, sort_keys=True).encode()).hexdigest()
            agent_inputs[key] = agent_input
            shot["agent_input_id"] = key
            messages = shot.get("messages")
            prefix = agent_input.get("messages") or []
            if isinstance(messages, list) and messages[:len(prefix)] == prefix:
                shot["continuation_messages"] = messages[len(prefix):]
                shot.pop("messages")

    for retry in technical_retry_attempts:
        normalize_shot(
            retry["shot"],
            attempt_id=(
                f"{test_id}:{retry['slot']}:technical-attempt-"
                f"{retry['execution_number']}"
            ),
        )
    for index, shot in enumerate(shots):
        shot["technical_attempt_number"] = 1 + sum(
            retry["slot"] == index + 1 for retry in technical_retry_attempts
        )
        normalize_shot(shot, attempt_id=f"{test_id}:{index + 1}")

    infra_errors = [
        shot
        for shot in shots
        if _shot_has_infrastructure_error(shot)
    ]
    with_memory = [shot for shot in shots if shot["with_memory"]]
    without_memory = [shot for shot in shots if not shot["with_memory"]]
    g1_pass_count = sum(shot.get("passed") is True for shot in with_memory)
    g2_pass_count = sum(shot.get("passed") is True for shot in without_memory)
    if infra_errors:
        verdict = "infra_error"
    elif g1_pass_count != 2:
        verdict = "unsolvable_with_memory"
    elif g2_pass_count:
        verdict = "leaks_without_memory"
    else:
        verdict = "valid"
    return {
        "test_id": test_id,
        "verdict": verdict,
        "valid": verdict == "valid",
        "g1_pass_count": g1_pass_count,
        "g2_pass_count": g2_pass_count,
        "shots": shots,
        "technical_retry_attempts": technical_retry_attempts,
        "technical_retries_used": len(technical_retry_attempts),
        "agent_inputs": agent_inputs,
    }


def certify_test(
    persona: str,
    test_id: str,
    *,
    checkpoint: Path | None = None,
    confirm_paid_calls: bool = False,
) -> dict[str, Any]:
    return certify_candidate(
        persona,
        ROOT / "tests" / persona / f"{test_id}.yaml",
        checkpoint=checkpoint,
        test_id=test_id,
        confirm_paid_calls=confirm_paid_calls,
    )


def write_results(path: Path, persona: str, results: dict[str, Any]) -> None:
    ordered = [results[key] for key in sorted(results)]
    summary: dict[str, int] = {}
    for result in ordered:
        verdict = result["verdict"]
        summary[verdict] = summary.get(verdict, 0) + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "persona": persona,
        "gate": "2/2 with source sessions and 0/2 without memory",
        "summary": summary,
        "results": ordered,
    }, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", required=True, choices=("morgan", "alex", "riley"))
    parser.add_argument("--only", help="comma-separated test ids; defaults to 001-200")
    parser.add_argument(
        "--candidate",
        type=Path,
        help="YAML or JSON test candidate to certify instead of tests/<persona>/<id>.yaml",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        help="authenticated checkpoint to use for facts and source sessions",
    )
    parser.add_argument("--out", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--confirm-paid-calls", action="store_true")
    args = parser.parse_args()

    if not args.confirm_paid_calls:
        parser.error("refusing certification calls without --confirm-paid-calls")

    if args.candidate is not None and args.only:
        parser.error("--candidate cannot be combined with --only")
    candidate_path = args.candidate.resolve() if args.candidate is not None else None
    if candidate_path is not None:
        candidate = _load_candidate(candidate_path)
        test_ids = [str(candidate.get("id") or candidate_path.stem)]
    else:
        test_ids = (
            [value.strip().zfill(3) for value in args.only.split(",") if value.strip()]
            if args.only
            else [f"{index:03d}" for index in range(1, 201)]
        )
    out_path = Path(args.out)
    results: dict[str, Any] = {}
    if args.resume and out_path.exists():
        previous = json.loads(out_path.read_text())
        if previous.get("persona") != args.persona:
            raise SystemExit("cannot resume a result file for another persona")
        results = {str(row["test_id"]): row for row in previous.get("results") or []}

    for test_id in test_ids:
        if test_id in results:
            continue
        result = (
            certify_candidate(
                args.persona,
                candidate_path,
                checkpoint=args.checkpoint,
                confirm_paid_calls=True,
            )
            if candidate_path is not None
            else certify_test(
                args.persona,
                test_id,
                checkpoint=args.checkpoint,
                confirm_paid_calls=True,
            )
        )
        results[test_id] = result
        write_results(out_path, args.persona, results)
        print(
            f"{args.persona}/{test_id}: {result['verdict']} "
            f"(with={result['g1_pass_count']}/2, no-memory={result['g2_pass_count']}/2)",
            flush=True,
        )

    write_results(out_path, args.persona, results)
    return 0 if all(results[test_id]["valid"] for test_id in test_ids) else 1


if __name__ == "__main__":
    raise SystemExit(main())
