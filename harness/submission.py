"""The two-file submission format and its local, no-model validation."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import stat
import statistics
import tempfile
import zipfile
import zlib
from collections import Counter
from pathlib import Path
from typing import Any

from harness.costing import compute_call_cost


FILES = {"ingestion.json", "tests.json"}
MAX_JSON_BYTES = 512 * 1024 * 1024
MAX_TOTAL_JSON_BYTES = 2 * MAX_JSON_BYTES
# Allow both maximum-size JSON files plus compression and ZIP metadata overhead.
MAX_ZIP_BYTES = MAX_TOTAL_JSON_BYTES + 2 * 1024 * 1024
MAX_ZIP_METADATA_BYTES = 512 * 1024
ZIP_READ_BYTES = 64 * 1024
SETTINGS_FIELDS = {"model", "reasoning_effort", "temperature", "top_p", "max_tokens",
                   "max_completion_tokens", "max_output_tokens", "context_length", "response_format", "system_prompt",
                   "tools", "stop", "seed", "parallel_tool_calls"}
MESSAGE_FIELDS = {"role", "content", "tool_calls", "tool_call_id", "usage", "duration_ms", "settings"}


class SubmissionError(ValueError):
    pass


def _shared(current: dict | None, incoming: dict, label: str) -> dict:
    check_settings(incoming, label)
    if current is not None and current != incoming:
        raise SubmissionError(f"{label}: settings differ across the selected records")
    return copy.deepcopy(incoming)


def record_settings(current: dict | None, incoming: dict, records: list[dict], label: str) -> dict:
    """Use phase defaults, retaining a complete snapshot when a response differs."""
    check_settings(incoming, label)
    if current is None:
        return copy.deepcopy(incoming)
    if current != incoming:
        for row in records:
            for message in row["messages"]:
                if message["role"] == "assistant" and "settings" not in message:
                    message["settings"] = copy.deepcopy(incoming)
    return current


def _hoist_system_prompt(settings: dict, records: list[dict]) -> None:
    if not records:
        return
    if any("settings" in m for row in records for m in row["messages"]):
        return
    prompts = [row["messages"][0] for row in records if row.get("messages")]
    if len(prompts) != len(records) or any(m.get("role") != "system" for m in prompts):
        return
    content = prompts[0].get("content")
    if (not isinstance(content, str) or any(m.get("content") != content for m in prompts)
            or ("system_prompt" in settings and settings["system_prompt"] != content)):
        return
    settings["system_prompt"] = content
    for record in records:
        record["messages"].pop(0)


def link_ingestion(mapping: list[dict[str, Any]], records: list[dict[str, Any]],
                   *, require_complete: bool = True) -> dict[str, int]:
    """Locate each normalized session's latest saved Hermes seed record."""
    lookup = {(row["original_session_id"], row["original_message_index"]): row["session_id"]
              for row in mapping}
    if len(lookup) != len(mapping) or len(set(lookup.values())) != len(mapping):
        raise ValueError("Ingestion mapping is not one-to-one")
    links = {}
    for position, record in enumerate(records):
        index = record.get("message_index")
        if type(index) is not int:
            raise ValueError(f"Ingestion record {position}: invalid message index")
        key = (str(record.get("session_id")), index)
        if key not in lookup:
            raise ValueError(f"Ingestion record {position}: unknown source {key}")
        if (record.get("seed_item_id") is not None
                and record["seed_item_id"] != f"{key[0]}:{key[1]}"):
            raise ValueError(f"Ingestion record {position}: conflicting source IDs")
        links[lookup[key]] = position
    if require_complete and set(links) != set(lookup.values()):
        raise ValueError("Ingestion records do not cover every source message")
    return links


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SubmissionError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(content: str | bytes | bytearray) -> Any:
    def invalid(value):
        raise SubmissionError(f"Non-finite JSON number: {value}")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            invalid(value)
        return number

    try:
        value = json.loads(content, object_pairs_hook=_object, parse_constant=invalid,
                           parse_float=finite_float)
        # Bound nesting independently of the Python version's parser limit.
        stack = [iter((value,))]
        while stack:
            try:
                item = next(stack[-1])
            except StopIteration:
                stack.pop()
                continue
            if isinstance(item, (dict, list)):
                if len(stack) > 128:
                    raise SubmissionError('JSON nesting exceeds 128 levels')
                stack.append(iter(item.values() if isinstance(item, dict) else item))
        return value
    except SubmissionError:
        raise
    except RecursionError as exc:
        raise SubmissionError("JSON nesting exceeds the parser limit") from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise SubmissionError(f"Invalid JSON: {exc}") from exc


def _fields(value: Any, required: set[str], optional: set[str], label: str) -> None:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise SubmissionError(f"{label}: expected {sorted(required)}, optionally {sorted(optional)}")


def _duration(value: Any, label: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise SubmissionError(f"{label}: duration_ms must be a finite nonnegative number")


def check_settings(settings: Any, label: str) -> None:
    _fields(settings, {"model"}, SETTINGS_FIELDS - {"model"}, label)
    if not isinstance(settings["model"], str) or not settings["model"].strip():
        raise SubmissionError(f"{label}: model must be a non-empty string")
    if "system_prompt" in settings and not isinstance(settings["system_prompt"], str):
        raise SubmissionError(f"{label}: system_prompt must be a string")
    if "tools" in settings and not isinstance(settings["tools"], list):
        raise SubmissionError(f"{label}: tools must be a list")
    if "reasoning_effort" in settings and not isinstance(settings["reasoning_effort"], str):
        raise SubmissionError(f"{label}: reasoning_effort must be a string")
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens", "context_length"):
        if key in settings and (type(settings[key]) is not int or settings[key] < 1):
            raise SubmissionError(f"{label}: {key} must be a positive integer")
    for key in ("temperature", "top_p"):
        if key in settings and (type(settings[key]) not in (int, float)
                                or not math.isfinite(settings[key]) or settings[key] < 0):
            raise SubmissionError(f"{label}: {key} must be a finite nonnegative number")


def token_counts(usage: Any) -> dict[str, int]:
    """Read returned usage without counting cached input or reasoning twice."""
    if not isinstance(usage, dict) or not usage:
        raise SubmissionError("Missing per-response token usage; aggregate or estimated usage cannot replace it")
    if any(key in usage for key in ("_source", "estimated", "estimated_tokens")):
        raise SubmissionError("Usage must come from the model response, not a transcript estimate")

    def number(value, label):
        if type(value) is not int or value < 0:
            raise SubmissionError(f"Usage {label} must be a nonnegative integer")
        return value

    def details(name):
        value = usage.get(name) or {}
        if not isinstance(value, dict):
            raise SubmissionError(f"Usage {name} must be an object")
        return value

    canonical = "cache_read_tokens" in usage or "cache_write_tokens" in usage
    if canonical:
        fresh = number(usage.get("input_tokens"), "input_tokens")
        output = number(usage.get("output_tokens"), "output_tokens")
        cached = number(usage.get("cache_read_tokens", 0), "cache_read_tokens")
        written = number(usage.get("cache_write_tokens", 0), "cache_write_tokens")
        reasoning = number(usage.get("reasoning_tokens", 0), "reasoning_tokens")
        if "prompt_tokens" in usage and number(usage["prompt_tokens"], "prompt_tokens") != fresh + cached + written:
            raise SubmissionError("Usage prompt_tokens disagrees with input and cache buckets")
    elif "prompt_tokens" in usage:
        prompt = number(usage["prompt_tokens"], "prompt_tokens")
        output = number(usage.get("completion_tokens"), "completion_tokens")
        cached = number(details("prompt_tokens_details").get("cached_tokens", 0), "cached_tokens")
        written = 0
        reasoning = number(details("completion_tokens_details").get("reasoning_tokens", 0), "reasoning_tokens")
        fresh = prompt - cached
    else:
        prompt = number(usage.get("input_tokens"), "input_tokens")
        output = number(usage.get("output_tokens"), "output_tokens")
        reasoning = number(details("output_tokens_details").get("reasoning_tokens", 0), "reasoning_tokens")
        if "input_tokens_details" in usage:
            cached = number(details("input_tokens_details").get("cached_tokens", 0), "cached_tokens")
            written = 0
            fresh = prompt - cached
        else:
            fresh = prompt
            cached = number(usage.get("cache_read_input_tokens", 0), "cache_read_input_tokens")
            written = number(usage.get("cache_creation_input_tokens", 0), "cache_creation_input_tokens")
    if fresh < 0 or reasoning > output:
        raise SubmissionError("Usage cache or reasoning tokens exceed their containing bucket")
    total = fresh + cached + written + output
    if "total_tokens" in usage and number(usage["total_tokens"], "total_tokens") != total:
        raise SubmissionError("Usage total_tokens disagrees with input and output")
    return {"input_tokens": fresh, "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": written, "output_tokens": output,
            "reasoning_tokens": reasoning}


def clean_message(message: dict) -> dict:
    """Convert a saved chat message to the approved message representation."""
    result = {key: copy.deepcopy(value) for key, value in message.items()
              if key in MESSAGE_FIELDS and value is not None}
    tool_calls = result.get("tool_calls")
    if isinstance(tool_calls, str):
        tool_calls = load_json(tool_calls)
    if tool_calls:
        normalized = []
        for call in tool_calls:
            function = call.get("function", call)
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                arguments = load_json(arguments)
            normalized.append({"id": call.get("id") or call.get("call_id"),
                               "name": function.get("name"), "arguments": arguments})
        result["tool_calls"] = normalized
    else:
        result.pop("tool_calls", None)
    return result


def check_messages(messages: Any, label: str) -> list[dict]:
    if not isinstance(messages, list) or not messages:
        raise SubmissionError(f"{label}: missing conversation")
    pending = {}
    seen = set()
    tools = []
    for position, message in enumerate(messages):
        where = f"{label}, message {position}"
        _fields(message, {"role"}, MESSAGE_FIELDS - {"role"}, where)
        role = message["role"]
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise SubmissionError(f"{where}: unknown message role")
        if "duration_ms" in message:
            _duration(message["duration_ms"], where)
        if "content" not in message and not message.get("tool_calls"):
            raise SubmissionError(f"{where}: missing content or tool calls")
        if role == "assistant":
            token_counts(message.get("usage"))
            if "settings" in message:
                check_settings(message["settings"], f"{where} settings")
        elif "usage" in message:
            raise SubmissionError(f"{where}: usage belongs to an assistant response")
        if role != "assistant" and "settings" in message:
            raise SubmissionError(f"{where}: settings belong to an assistant response")
        if role != "assistant" and "tool_calls" in message:
            raise SubmissionError(f"{where}: only assistants request tools")
        if role != "tool" and "tool_call_id" in message:
            raise SubmissionError(f"{where}: only tool results have tool_call_id")
        if role in {"assistant", "user"} and pending:
            raise SubmissionError(f"{where}: a preceding tool call has no result")
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list):
            raise SubmissionError(f"{where}: tool_calls must be a list")
        for call in calls:
            _fields(call, {"id", "name", "arguments"}, set(), where)
            if (not isinstance(call["id"], str) or not call["id"] or call["id"] in seen
                    or not isinstance(call["name"], str) or not call["name"]
                    or not isinstance(call["arguments"], dict)):
                raise SubmissionError(f"{where}: invalid or duplicate tool call")
            pending[call["id"]] = call
            seen.add(call["id"])
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise SubmissionError(f"{where}: tool result has no matching call")
            call = pending.pop(call_id)
            name, arguments = call["name"], call["arguments"]
            if name == "tool_call" and isinstance(arguments.get("name"), str):
                name, arguments = arguments["name"], arguments.get("arguments", {})
            for prefix in ("mcp__dolphinbench_apps__", "mcp_dolphinbench_apps_"):
                if name.startswith(prefix):
                    name = name[len(prefix):]
                    break
            content = message.get("content")
            if isinstance(content, str):
                try:
                    content = load_json(content)
                except SubmissionError:
                    pass
            tools.append({"tool": name, "args": arguments, "result": content})
    if pending:
        raise SubmissionError(f"{label}: tool calls without results")
    if messages[-1]["role"] != "assistant" or messages[-1].get("tool_calls"):
        raise SubmissionError(f"{label}: missing final assistant response")
    return tools


def check_total_cost(value: Any, label: str) -> float:
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise SubmissionError(f"{label}: total_cost_usd must be a finite, nonnegative number from the harness integration")
    return value


def _phase_stats(records: list[dict], settings: dict, pricing: dict | None) -> dict:
    totals = {key: 0 for key in ("input_tokens", "cache_read_input_tokens",
                                "cache_creation_input_tokens", "output_tokens", "reasoning_tokens")}
    cost = 0.0
    unknown = 0
    responses = 0
    for record in records:
        for message in record["messages"]:
            if message["role"] != "assistant":
                continue
            counts = token_counts(message["usage"])
            for key, value in counts.items():
                totals[key] += value
            responses += 1
            model = message.get("settings", settings)["model"]
            entry = (pricing or {}).get("models", {}).get(model)
            rates = ("input_cost_per_token", "output_cost_per_token")
            if (not isinstance(entry, dict) or any(
                type(entry.get(key)) not in (int, float) or not math.isfinite(entry[key]) or entry[key] < 0
                for key in rates
            )):
                unknown += 1
            else:
                cost += compute_call_cost(counts, model, pricing)
    durations = [row["duration_ms"] for row in records if "duration_ms" in row]
    return {"records": len(records), "model_responses": responses, "tokens": totals,
            "total_duration_ms": sum(durations) if len(durations) == len(records) else None,
            "median_duration_ms": statistics.median(durations) if durations else None,
            "estimated_model_cost_usd": None if unknown else cost,
            "unpriced_model_responses": unknown}


def grading_calls(row: dict, label: str) -> list[dict]:
    """Keep model-visible tool text separate from the benchmark server's result."""
    transcript = check_messages(row["messages"], label)
    if "app_calls" not in row:
        return transcript
    calls = row["app_calls"]
    if not isinstance(calls, list):
        raise SubmissionError(f"{label}: app_calls must be a list")
    def key(call):
        return json.dumps([call["tool"], call["args"]], sort_keys=True, allow_nan=False)
    available = Counter(key(call) for call in transcript)
    for call in calls:
        _fields(call, {"tool", "args"}, {"result"}, f"{label} app call")
        if not isinstance(call["tool"], str) or not isinstance(call["args"], dict) or available[key(call)] < 1:
            raise SubmissionError(f"{label}: app call has no matching conversation tool call")
        available[key(call)] -= 1
    return calls


def validate(ingestion: dict, tests: dict, releases: dict, *, pricing: dict | None = None) -> dict:
    _fields(ingestion, {"settings", "sessions", "total_cost_usd"}, set(), "ingestion.json")
    _fields(tests, {"settings", "judge_settings", "tests", "total_cost_usd"}, set(), "tests.json")
    ingestion_cost = check_total_cost(ingestion["total_cost_usd"], "ingestion.json")
    test_cost = check_total_cost(tests["total_cost_usd"], "tests.json")
    total_cost = check_total_cost(ingestion_cost + test_cost, "submission")
    check_settings(ingestion["settings"], "ingestion settings")
    check_settings(tests["settings"], "test settings")
    check_settings(tests["judge_settings"], "judge settings")
    if (tests["judge_settings"]["model"] != "gpt-5.6-sol"
            or tests["judge_settings"].get("reasoning_effort") != "medium"):
        raise SubmissionError("Judge settings must use gpt-5.6-sol with medium reasoning")
    from graders.llm_judge import DEFAULT_MAX_TOKENS
    fixed_judge = {"model": "gpt-5.6-sol", "reasoning_effort": "medium",
                   "max_completion_tokens": DEFAULT_MAX_TOKENS, "response_format": {"type": "json_object"}}
    if (set(tests["judge_settings"]) - set(fixed_judge) - {"system_prompt"}
            or any(tests["judge_settings"].get(key, value) != value for key, value in fixed_judge.items())):
        raise SubmissionError("Judge settings differ from the benchmark's fixed configuration")
    expected_history = {(p, s["id"]): s for p, release in releases.items() for s in release["sessions"]}
    expected_tests = {(p, str(t["id"]).zfill(3)): t for p, release in releases.items() for t in release["tests"]}
    if set(releases) != {"morgan", "alex", "riley"} or len(expected_tests) != 600:
        raise SubmissionError("Validation requires the full three-persona, 600-test release")
    passed = {persona: 0 for persona in releases}
    judge_records = []
    for phase, rows, expected, id_field in (
        ("ingestion", ingestion["sessions"], expected_history, "session_id"),
        ("tests", tests["tests"], expected_tests, "test_id"),
    ):
        if not isinstance(rows, list):
            raise SubmissionError(f"{phase}: records must be a list")
        seen = set()
        by_persona = {p: [] for p in releases}
        for row in rows:
            required = {"persona", id_field, "duration_ms", "messages"}
            if phase == "tests":
                required.add("grading")
            _fields(row, required, {"app_calls"}, phase)
            if not isinstance(row["persona"], str) or not isinstance(row[id_field], str):
                raise SubmissionError(f"{phase}: persona and {id_field} must be strings")
            key = (row["persona"], row[id_field])
            label = f"{phase}/{key[0]}/{key[1]}"
            if key not in expected or key in seen:
                raise SubmissionError(f"{label}: unexpected or duplicate record")
            seen.add(key)
            by_persona[key[0]].append(key[1])
            _duration(row["duration_ms"], label)
            actions = grading_calls(row, label)
            users = [m["content"] for m in row["messages"] if m["role"] == "user"]
            source = expected[key]
            if phase == "ingestion":
                dated_message = f"[{source['narrative_date']}] {source['messages'][0].strip()}"
                if users != [dated_message]:
                    raise SubmissionError(f"{label}: user message differs from the released history")
            else:
                dated_request = f"[{source['narrative_anchor_date']}] {source['test'].strip()}"
                if users not in ([source["test"]], [dated_request]):
                    raise SubmissionError(f"{label}: user request differs from the published test")
                grading = row["grading"]
                assertions = source["grade"]["config"]["assertions"]
                if not isinstance(grading, list) or len(grading) != len(assertions):
                    raise SubmissionError(f"{label}: grading must cover every published check")
                judge_messages = []
                for i, check in enumerate(grading):
                    _fields(check, {"check", "passed"}, {"messages"}, f"{label} grading")
                    if type(check["check"]) is not int or check["check"] != i or type(check["passed"]) is not bool:
                        raise SubmissionError(f"{label}: invalid check number or verdict")
                    if "messages" in check:
                        check_messages(check["messages"], f"{label} judge {i}")
                        if any("settings" in message and message["settings"] != tests["judge_settings"]
                               for message in check["messages"]):
                            raise SubmissionError(f"{label}: judge response overrides the fixed settings")
                        judge_messages.extend(check["messages"])
                        judge_record = {"messages": check["messages"]}
                        answers = [m for m in check["messages"] if m["role"] == "assistant"]
                        if all("duration_ms" in m for m in answers):
                            judge_record["duration_ms"] = sum(m["duration_ms"] for m in answers)
                        judge_records.append(judge_record)
                config = copy.deepcopy(source["grade"]["config"])
                config["raise_on_judge_error"] = True
                try:
                    from graders.judge_recording import replay_judge
                    from graders.mechanical import grade_tool_trace
                    with replay_judge(judge_messages, tests["judge_settings"]):
                        result = grade_tool_trace(actions, config, test_message=source["test"])
                except (ValueError, KeyError, TypeError) as exc:
                    raise SubmissionError(f"{label}: {exc}") from exc
                if [check["passed"] for check in grading] != [check["ok"] for check in result["details"]]:
                    raise SubmissionError(f"{label}: supplied grades disagree with the recorded actions and judge responses")
                passed[key[0]] += int(result["passed"])
        if seen != set(expected):
            missing = sorted(set(expected) - seen)
            raise SubmissionError(f"{phase}: missing {len(missing)} records; first missing: {missing[:3]}")
        if phase == "ingestion":
            for persona, ids in by_persona.items():
                if ids != [s["id"] for s in releases[persona]["sessions"]]:
                    raise SubmissionError(f"{persona}: ingestion sessions are out of order")
    return {"passes": sum(passed.values()), "tests": 600, "passes_by_persona": passed,
            "total_cost_usd": total_cost,
            "ingestion": {**_phase_stats(ingestion["sessions"], ingestion["settings"], pricing),
                          "total_cost_usd": ingestion_cost},
            "execution": {**_phase_stats(tests["tests"], tests["settings"], pricing),
                          "total_cost_usd": test_cost},
            "grading": _phase_stats(judge_records, tests["judge_settings"], pricing)}


def read_release(directory: Path) -> dict:
    """Load the distributed release without requiring construction checkpoints."""
    import yaml
    from harness.dataset import load_test

    manifest = load_json((directory / "manifest.json").read_bytes())
    if manifest.get("format") != "single_message_release":
        raise SubmissionError("Expected a single-message release directory")
    for relative, expected_sha in manifest["files"].items():
        path = (directory / relative).resolve()
        path.relative_to(directory.resolve())
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha:
            raise SubmissionError(f"Released file changed: {relative}")
    releases = {}
    for persona in ("morgan", "alex", "riley"):
        expected = {f"{i:03d}.yaml" for i in range(1, 201)}
        paths = sorted((directory / "tests" / persona).glob("*.yaml"))
        if {path.name for path in paths} != expected:
            raise SubmissionError(f"{persona}: release must contain exactly 200 tests")
        for path in [*paths, *(directory / "registry/personas" / persona / name
                              for name in ("life_sim.yaml", "facts.yaml"))]:
            expected_sha = manifest["files"].get(str(path.relative_to(directory)))
            if expected_sha != hashlib.sha256(path.read_bytes()).hexdigest():
                raise SubmissionError(f"Released file changed: {path.relative_to(directory)}")
        # Read the authenticated bytes rather than a prior loader cache.
        sessions = yaml.safe_load((directory / "registry/personas" / persona / "life_sim.yaml").read_text())["sessions"]
        facts_payload = yaml.safe_load((directory / "registry/personas" / persona / "facts.yaml").read_text())
        from registry.facts import _parse_fact_registry
        facts = _parse_fact_registry(facts_payload, persona=persona,
            path=directory / "registry/personas" / persona / "facts.yaml", allowed_versions={1, 2}, normalize_session_ids=True)
        if (len(sessions) != manifest["sources"][persona]["sessions"]
                or [s.get("id") for s in sessions] != [f"{i:06d}" for i in range(1, len(sessions) + 1)]
                or any(not isinstance(s.get("messages"), list) or len(s["messages"]) != 1
                       or not isinstance(s["messages"][0], str) or not s["messages"][0].strip()
                       or not isinstance(s.get("narrative_date"), str) for s in sessions)):
            raise SubmissionError(f"{persona}: invalid single-message history")
        ids = {s["id"] for s in sessions}
        for fact in facts.values():
            if any(sid not in ids for field in ("source_session_ids", "related_history_session_ids")
                   for sid in fact.get(field, [])):
                raise SubmissionError(f"{persona}: fact references a missing session")
        def authenticated_bytes(path):
            relative = str(path.resolve().relative_to(directory.resolve()))
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != manifest["files"].get(relative):
                raise SubmissionError(f"Released file changed or unlisted: {relative}")
            return raw

        tests = [load_test(path, read_bytes=authenticated_bytes) for path in paths]
        for i, test in enumerate(tests, start=1):
            if str(test.get("id")).zfill(3) != f"{i:03d}":
                raise SubmissionError(f"{persona}: test IDs do not match their filenames")
            if not test.get("load_bearing_facts") or any(fid not in facts for fid in test["load_bearing_facts"]):
                raise SubmissionError(f"{persona}: test references a missing fact")
        releases[persona] = {"sessions": sessions, "facts": facts, "tests": tests}
        runtime_path = f"mock_mcp/runtime/{persona}.json"
        if runtime_path in manifest["files"]:
            runtime = load_json((directory / runtime_path).read_bytes())
            if set(runtime["tests"]) != {f"{i:03d}" for i in range(1, 201)}:
                raise SubmissionError(f"{persona}: incomplete app runtime")
            for entry in runtime["tests"].values():
                if entry["workspace"] not in runtime["workspaces"]:
                    raise SubmissionError(f"{persona}: missing app directory")
            releases[persona]["runtime"] = runtime
    return releases


class _ZipReader(io.BufferedReader):
    """Bound metadata allocation before ZipFile can enumerate untrusted entries."""

    reading_directory = True

    def read(self, size=-1):
        if self.reading_directory:
            if size < 0:
                size = os.fstat(self.fileno()).st_size - self.tell()
            if size > MAX_ZIP_METADATA_BYTES:
                raise SubmissionError("ZIP metadata exceeds the size limit")
        return super().read(size)


def read_zip(path: Path) -> tuple[dict, dict]:
    """Read only the two JSON documents, never extract files or execute their contents.

    A hosted caller must also bound the validator process's memory and runtime;
    decoded JSON objects can occupy more memory than their uncompressed bytes.
    """
    try:
        with _ZipReader(io.FileIO(path, "r")) as stream:
            if os.fstat(stream.fileno()).st_size > MAX_ZIP_BYTES:
                raise SubmissionError("Submission ZIP exceeds the size limit")
            with zipfile.ZipFile(stream) as archive:
                stream.reading_directory = False
                entries = archive.infolist()
                if (len(entries) != 2 or {entry.orig_filename for entry in entries} != FILES):
                    raise SubmissionError("ZIP must contain only ingestion.json and tests.json at its root")
                for entry in entries:
                    mode = stat.S_IFMT(entry.external_attr >> 16)
                    if (entry.external_attr & 0x10
                            or (entry.create_system == 3 and mode not in (0, stat.S_IFREG))):
                        raise SubmissionError("ZIP entries must be regular files, not links or directories")
                    if entry.flag_bits & (1 | 0x40 | 0x2000) or entry.file_size > MAX_JSON_BYTES:
                        raise SubmissionError("Encrypted or oversized submission file")
                    if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                        raise SubmissionError("ZIP must use stored or Deflate compression")
                if sum(entry.file_size for entry in entries) > MAX_TOTAL_JSON_BYTES:
                    raise SubmissionError("Combined JSON size exceeds the size limit")
                values = {}
                total = 0
                for entry in entries:
                    content = bytearray()
                    with archive.open(entry) as source:
                        while chunk := source.read(ZIP_READ_BYTES):
                            total += len(chunk)
                            if len(content) + len(chunk) > MAX_JSON_BYTES or total > MAX_TOTAL_JSON_BYTES:
                                raise SubmissionError("Decompressed JSON exceeds the size limit")
                            content.extend(chunk)
                    if len(content) != entry.file_size:
                        raise SubmissionError("Decompressed JSON size differs from the ZIP directory")
                    values[entry.filename] = load_json(content)
                return values["ingestion.json"], values["tests.json"]
    except (OSError, zipfile.BadZipFile, RuntimeError, EOFError, UnicodeError, zlib.error) as exc:
        raise SubmissionError(f"Cannot read submission ZIP: {exc}") from exc


def write_zip(path: Path, ingestion: dict, tests: dict, releases: dict,
              *, pricing: dict | None = None) -> dict:
    if path.exists():
        raise SubmissionError("Output already exists; choose a new ZIP path")
    summary = validate(ingestion, tests, releases, pricing=pricing)
    payloads = {name: json.dumps(value, ensure_ascii=False, allow_nan=False,
                                separators=(",", ":")).encode("utf-8")
                for name, value in (("ingestion.json", ingestion), ("tests.json", tests))}
    secrets = [value.encode("utf-8") for key, value in os.environ.items()
               if len(value) >= 16 and key.endswith(("API_KEY", "TOKEN", "PASSWORD", "SECRET"))]
    for name, content in payloads.items():
        if len(content) > MAX_JSON_BYTES:
            raise SubmissionError(f"{name}: exceeds the JSON size limit")
        if any(secret in content for secret in secrets):
            raise SubmissionError(f"{name}: contains a configured credential; nothing exported")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as directory:
        temporary = Path(directory) / "submission.zip"
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in payloads.items():
                archive.writestr(name, content)
        reloaded = read_zip(temporary)
        if reloaded != (ingestion, tests):
            raise SubmissionError("Submission changed during ZIP round-trip")
        # Publish without replacing an existing archive, including a racing writer.
        os.link(temporary, path)
    return summary
