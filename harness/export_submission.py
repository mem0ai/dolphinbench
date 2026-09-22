"""Export completed local records to the approved two-file submission ZIP."""

from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path

from harness.costing import load_pricing
from harness.submission import (
    SubmissionError, _shared, _hoist_system_prompt, record_settings, clean_message, check_total_cost, load_json, read_release, read_zip, validate, write_zip,
)
from harness.submission import link_ingestion

PERSONAS = ("morgan", "alex", "riley")


def _read_trace(record: dict, results_path: Path) -> dict:
    if isinstance(record.get("submission"), dict):
        return record["submission"]
    value = record.get("trace_path")
    if not isinstance(value, str):
        raise SubmissionError(f"{results_path.name}: execution has no saved trace")
    path = Path(value)
    candidates = [path if path.is_absolute() else results_path.parent / path]
    # Downloads retain the traces/ subtree, even when the worker used an absolute path.
    if "traces" in path.parts:
        suffix = Path(*path.parts[path.parts.index("traces"):])
        candidates.append(results_path.parent / suffix)
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise SubmissionError(f"{results_path.name}: saved trace is not available locally: {value}")
    trace = load_json(path.read_bytes())
    if trace.get("submission_error"):
        raise SubmissionError(f"{path.name}: {trace['submission_error']}")
    payload = trace.get("submission")
    if not isinstance(payload, dict):
        raise SubmissionError(
            f"{path.name}: trace lacks recorded per-response usage and settings. "
            "Older aggregate usage cannot be turned into a complete submission."
        )
    return payload


def _duration(record: dict, *, test: bool) -> float:
    latest = record
    if test and record.get("attempts"):
        latest = record["attempts"][-1]
    duration = latest.get("latency_seconds")
    if type(duration) not in (int, float):
        raise SubmissionError("Latest execution has no measured latency_seconds")
    return duration * 1000


def build_documents(ingestion_paths: dict[str, Path], test_paths: dict[str, Path],
                    maps: dict) -> tuple[dict, dict]:
    if set(ingestion_paths) != set(PERSONAS) or set(test_paths) != set(PERSONAS):
        raise SubmissionError("Select ingestion and test results for Morgan, Alex, and Riley")
    sessions = []
    tests = []
    ingestion_settings = test_settings = judge_settings = None
    costs = {"ingestion": [], "tests": []}
    for persona in PERSONAS:
        path = ingestion_paths[persona]
        source = load_json(path.read_bytes())
        costs["ingestion"].append(check_total_cost(source.get("total_cost_usd"), str(path)))
        if "sessions" in source and "settings" in source:
            incoming = copy.deepcopy(source["sessions"])
            ingestion_settings = record_settings(ingestion_settings, source["settings"], incoming, "ingestion settings")
            sessions.extend(incoming)
        else:
            records = source.get("seed_calls")
            if not isinstance(records, list):
                raise SubmissionError(f"{path}: no ingestion records")
            if persona not in maps:
                raise SubmissionError("Exporting older ingestion records requires the original session-ID mapping")
            links = link_ingestion(maps[persona], records)
            for session_id, position in sorted(links.items()):
                record = records[position]
                trace = _read_trace(record, path)
                row = {"persona": persona, "session_id": session_id,
                                 "duration_ms": _duration(record, test=False),
                                 "messages": [clean_message(m) for m in trace["messages"]]}
                ingestion_settings = record_settings(ingestion_settings, trace["settings"], [row], "ingestion settings")
                sessions.append(row)

        path = test_paths[persona]
        source = load_json(path.read_bytes())
        costs["tests"].append(check_total_cost(source.get("total_cost_usd"), str(path)))
        if "tests" in source and "settings" in source:
            incoming = copy.deepcopy(source["tests"])
            test_settings = record_settings(test_settings, source["settings"], incoming, "test settings")
            judge_settings = _shared(judge_settings, source["judge_settings"], "judge settings")
            tests.extend(incoming)
            continue
        records = source.get("test_results")
        if not isinstance(records, list):
            raise SubmissionError(f"{path}: no test results")
        latest = {}
        for record in records:
            test_id = record.get("source_id") or record.get("test_id")
            if not isinstance(test_id, (str, int)) or isinstance(test_id, bool):
                raise SubmissionError(f"{path}: invalid test ID")
            latest[str(test_id).zfill(3)] = record
        for test_id, record in sorted(latest.items()):
            trace = _read_trace(record, path)
            grade = record.get("grade")
            if not isinstance(grade, dict) or not isinstance(grade.get("details"), list):
                raise SubmissionError(f"{persona}/{test_id}: missing completed grading")
            checks = []
            for index, detail in enumerate(grade["details"]):
                check = {"check": index, "passed": detail["ok"]}
                if detail.get("judge_messages"):
                    judge_settings = _shared(judge_settings, detail["judge_settings"], "judge settings")
                    check["messages"] = [clean_message(m) for m in detail["judge_messages"]]
                checks.append(check)
            row = {"persona": persona, "test_id": test_id,
                          "duration_ms": _duration(record, test=True),
                          "messages": [clean_message(m) for m in trace["messages"]], "grading": checks}
            calls = record.get("effective_tool_calls")
            if isinstance(calls, list):
                row["app_calls"] = [{key: call[key] for key in ("tool", "args", "result") if key in call}
                                    for call in calls]
            test_settings = record_settings(test_settings, trace["settings"], [row], "test settings")
            tests.append(row)
    if ingestion_settings is None or test_settings is None:
        raise SubmissionError("Missing execution settings")
    # No semantic calls are needed when every relevant action is absent. The
    # benchmark's fixed judge still identifies the grading configuration.
    judge_settings = judge_settings or {"model": "gpt-5.6-sol", "reasoning_effort": "medium"}
    _hoist_system_prompt(ingestion_settings, sessions)
    _hoist_system_prompt(test_settings, tests)
    _hoist_system_prompt(judge_settings, [check for test in tests for check in test["grading"]
                                        if check.get("messages")])
    return ({"settings": ingestion_settings, "sessions": sessions,
             "total_cost_usd": math.fsum(costs["ingestion"])},
            {"settings": test_settings, "judge_settings": judge_settings, "tests": tests,
             "total_cost_usd": math.fsum(costs["tests"])})


def _paths(values: list[str]) -> dict[str, Path]:
    paths = {}
    for value in values:
        persona, separator, filename = value.partition("=")
        if not separator or persona not in PERSONAS or persona in paths or not filename:
            raise SubmissionError("Sources must use PERSONA=FILE once for each persona")
        paths[persona] = Path(filename)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True, help="Verified single-message release directory")
    parser.add_argument("--ingestion", action="append", default=[], metavar="PERSONA=FILE")
    parser.add_argument("--tests", action="append", default=[], metavar="PERSONA=FILE")
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--out", type=Path, help="Write a new submission ZIP")
    output.add_argument("--check", type=Path, help="Validate an existing ZIP without model calls")
    args = parser.parse_args()
    try:
        if args.check and (args.ingestion or args.tests):
            raise SubmissionError("--check reads the ZIP, not separate source files")
        ingestion_paths = _paths(args.ingestion)
        test_paths = _paths(args.tests)
        if args.out and (set(ingestion_paths) != set(PERSONAS) or set(test_paths) != set(PERSONAS)):
            raise SubmissionError("Select --ingestion and --tests files for all three personas")
        releases = read_release(args.release)
        pricing = load_pricing()
        if args.check:
            summary = validate(*read_zip(args.check), releases, pricing=pricing)
        else:
            mapping = args.release / "session_map.json"
            maps = load_json(mapping.read_bytes()) if mapping.is_file() else {}
            ingestion, tests = build_documents(ingestion_paths, test_paths, maps)
            summary = write_zip(args.out, ingestion, tests, releases, pricing=pricing)
        print(json.dumps(summary, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
