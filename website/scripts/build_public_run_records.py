"""Build readable official-run records from the approved result archives."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import io
import json
import math
import statistics
import tarfile
import zipfile
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

import yaml


PERSONAS = ("morgan", "alex", "riley")
PERSONA_NAMES = {name: name.title() for name in PERSONAS}
ROOT = Path(__file__).resolve().parents[2]
MEMORY_TOOLS = ("memory", "session_search", "honcho_", "hindsight", "supermemory", "mem0")


def read_json(path: Path) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as source:
        return json.load(source)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def write_compact_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")


def compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def write_record_arrays_json(path: Path, header: dict,
                             arrays: list[tuple[str, list[dict], bool]]) -> None:
    """Write readable JSON without expanding every nested value across many lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["{"]
    for name, value in header.items():
        lines.append(f"  {json.dumps(name)}: {compact_json(value)},")
    for array_index, (key, records, fields_on_separate_lines) in enumerate(arrays):
        lines.append(f"  {json.dumps(key)}: [")
        for index, record in enumerate(records):
            comma = "," if index + 1 < len(records) else ""
            if not fields_on_separate_lines:
                lines.append(f"    {compact_json(record)}{comma}")
                continue
            lines.append("    {")
            items = list(record.items())
            for field_index, (name, value) in enumerate(items):
                field_comma = "," if field_index + 1 < len(items) else ""
                lines.append(f"      {json.dumps(name)}: {compact_json(value)}{field_comma}")
            lines.append(f"    }}{comma}")
        array_comma = "," if array_index + 1 < len(arrays) else ""
        lines.append(f"  ]{array_comma}")
    lines.append("}")
    path.write_text("\n".join(lines) + "\n")


def write_record_array_json(path: Path, header: dict, key: str, records: list[dict],
                            *, fields_on_separate_lines: bool = False) -> None:
    write_record_arrays_json(path, header, [(key, records, fields_on_separate_lines)])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact_name(url: str) -> str:
    return Path(urlsplit(url).path.rstrip("/")).name


def system_path(row: dict) -> Path:
    model = {
        "gpt-5.6-luna-high": "gpt-5.6-luna",
        "minimax-m3-high": "minimax-m3",
        "claude-sonnet-5": "claude-sonnet-5",
    }[row["model"]["id"]]
    return Path(row["harness"]["id"]) / model / row["memory"]["id"]


def result_path(cache: Path, row: dict, persona: str) -> Path:
    return cache / artifact_name(row["personas"][persona]["source"]["url"])


def verify_artifact(path: Path, expected: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    actual = sha256(path)
    if actual != expected:
        raise ValueError(f"{path}: expected {expected}, found {actual}")


def fmt_cost(value: float | None) -> str:
    return "Not recorded" if value is None else f"${value:.6f}"


def fmt_latency(value: float | None) -> str:
    return "Not recorded" if value is None else f"{value:.3f} s"


def test_cost(record: dict) -> float | None:
    for key in ("cost_usd", "agent_cost_usd"):
        value = record.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value):
            return float(value)
    return None


def memory_from_system(messages: list[dict]) -> str | None:
    sections = []
    for message in messages:
        if message.get("role") != "system" or not isinstance(message.get("content"), str):
            continue
        text = message["content"]
        for heading in ("MEMORY (your personal notes)", "USER PROFILE (who the user is)", "# Honcho Memory"):
            start = text.find(heading)
            if start < 0:
                continue
            start = text.find("\n", start) + 1
            if start <= 0:
                continue
            next_line = text.find("\n", start)
            if next_line >= 0 and set(text[start:next_line].strip()) <= {"═"}:
                start = next_line + 1
            end_candidates = [position for marker in ("\n════════", "\nConversation started:", "\n# ")
                              if (position := text.find(marker, start + len(heading))) >= 0]
            end = min(end_candidates) if end_candidates else len(text)
            value = text[start:end].strip()
            if value and value not in sections:
                sections.append(value)
    return "\n\n".join(sections) or None


def hermes_events(payload: bytes) -> tuple[list[dict], str | None]:
    trace = json.loads(payload)
    submission = trace.get("submission") or trace
    messages = submission.get("messages") or trace.get("messages") or []
    calls: dict[str, str] = {}
    events = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            continue
        content = message.get("content")
        if role in ("assistant", "user") and content:
            events.append({"kind": role, "content": content})
        tool_calls = message.get("tool_calls") or []
        if isinstance(tool_calls, str):
            try:
                tool_calls = json.loads(tool_calls)
            except json.JSONDecodeError as exc:
                raise ValueError("Malformed tool calls in trace") from exc
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or call
            name = function.get("name", "unknown")
            arguments = function.get("arguments")
            if name == "tool_call" and isinstance(arguments, dict) and isinstance(arguments.get("name"), str):
                name = arguments["name"]
                arguments = arguments.get("arguments")
            call_id = call.get("id", "")
            calls[call_id] = name
            events.append({"kind": "call", "name": name, "arguments": arguments})
        if role == "tool":
            events.append({"kind": "result", "name": calls.get(message.get("tool_call_id", ""), "tool"),
                           "content": content})
    return events, memory_from_system(messages)


def claude_events(payload: bytes) -> tuple[list[dict], str | None]:
    events = []
    calls: dict[str, str] = {}
    memory = []
    seen = set()
    for raw in payload.decode("utf-8").splitlines():
        if not raw.strip():
            continue
        event = json.loads(raw)
        if event.get("uuid"):
            if event["uuid"] in seen:
                continue
            seen.add(event["uuid"])
        if event.get("type") == "system" and event.get("subtype") == "hook_response":
            output = event.get("output") or event.get("stdout")
            if event.get("hook_event") == "SessionStart" and isinstance(output, str) and output.strip():
                memory.append(output.strip())
            continue
        if event.get("type") == "assistant":
            for part in (event.get("message") or {}).get("content") or []:
                if part.get("type") == "text" and part.get("text"):
                    events.append({"kind": "assistant", "content": part["text"]})
                elif part.get("type") in {"tool_use", "server_tool_use"}:
                    name = part.get("name", "unknown")
                    calls[part.get("id", "")] = name
                    events.append({"kind": "call", "name": name, "arguments": part.get("input")})
            continue
        if event.get("type") == "user":
            for part in (event.get("message") or {}).get("content") or []:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    content = part.get("content")
                    events.append({"kind": "result", "name": calls.get(part.get("tool_use_id", ""), "tool"),
                                   "content": content})
    return events, "\n\n".join(memory) or None


def clean_memory_calls(events: list[dict]) -> list[dict]:
    steps = []
    for event in events:
        if event["kind"] in {"user", "assistant"} or not any(
                token in event["name"].lower() for token in MEMORY_TOOLS):
            continue
        if event["kind"] == "call":
            steps.append({"type": "call", "tool": event["name"],
                          "input": clean_recorded_value(event.get("arguments"))})
        elif event["kind"] == "result":
            steps.append({"type": "result", "tool": event["name"],
                          "output": clean_recorded_value(event.get("content"))})
    return steps


def clean_recorded_value(value: object) -> object:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return clean_recorded_value(json.loads(stripped))
            except json.JSONDecodeError:
                pass
        return value
    if isinstance(value, list):
        return [clean_recorded_value(item) for item in value]
    if isinstance(value, dict):
        return {key: clean_recorded_value(item) for key, item in value.items()}
    return value


def clean_app_calls(record: dict) -> list[dict]:
    cleaned = []
    for call in record.get("effective_tool_calls") or record.get("mcp_tool_calls") or []:
        call_input = clean_recorded_value(call.get("args"))
        value = {"tool": call.get("tool"), "input": call_input}
        if "result" in call:
            output = clean_recorded_value(call["result"])
            value["output"] = output
        cleaned.append(value)
    return cleaned


def clean_grading(grade: dict) -> dict:
    checks = []
    for detail in grade.get("details") or []:
        assertion = {key: value for key, value in (detail.get("assertion") or {}).items()
                     if key not in {"action_id", "check_id"}}
        check = {"passed": bool(detail.get("ok")), "requirement": assertion}
        if detail.get("observed_values"):
            values = [value.get("value") if isinstance(value, dict) and "value" in value else value
                      for value in detail["observed_values"]]
            check["observed"] = values[0] if len(values) == 1 else values
        if detail.get("reason") and detail["reason"] != "comparison passed":
            check["reason"] = detail["reason"]
        checks.append(check)
    return {"checks": checks}


def clean_test(record: dict, events: list[dict], memory: str | None) -> dict:
    answer = str(record.get("response") or "").strip()
    value = {
        "id": str(record.get("test_id") or record.get("source_id")).zfill(3),
        "passed": bool(record.get("passed", (record.get("grade") or {}).get("passed"))),
        "request": str(record.get("query") or "").strip(),
    }
    if memory:
        value["memory_at_start"] = memory.strip()
    memory_calls = clean_memory_calls(events)
    if memory_calls:
        value["memory_calls"] = memory_calls
    app_calls = clean_app_calls(record)
    if app_calls:
        value["tool_calls"] = app_calls
    value["answer"] = answer
    value["grading"] = clean_grading(record.get("grade") or {})
    if isinstance(record.get("latency_seconds"), (int, float)):
        value["latency_seconds"] = round(float(record["latency_seconds"]), 3)
    cost = test_cost(record)
    if cost is not None:
        value["cost_usd"] = round(cost, 6)
    return value


def expected_trace_name(record: dict, persona: str, *, claude: bool) -> list[str]:
    path = str(record.get("trace_path") or "")
    if claude:
        if "/tests/" not in path:
            return []
        run, tests = path.rsplit("/tests/", 1)
        run_name = Path(run).name
        return [f"{persona}/runs/{run_name}/tests/{tests}", f"{persona}/tests/{tests}"]
    if "/traces/" not in path:
        return []
    if "/parallel/" in path:
        return [f"{persona}/parallel/{path.split('/parallel/', 1)[1]}"]
    suffix = path.split("/traces/", 1)[1]
    return [f"{persona}/traces/{suffix}", f"traces/{suffix}"]


def match_trace(name: str, expected: dict[str, tuple[str, dict]]) -> tuple[str, dict] | None:
    for candidate in (name, name.lstrip("./")):
        if candidate in expected:
            return expected[candidate]
    return None


def process_tar_stream(source, expected: dict[str, tuple[str, dict]], selected: dict[str, dict[str, dict]],
                       *, claude: bool, nested_persona: str | None = None) -> set[tuple[str, str]]:
    found = set()
    with tarfile.open(fileobj=source, mode="r|*") as records:
        for member in records:
            if not member.isfile():
                continue
            if nested_persona is None and member.name.endswith(".tar.gz"):
                persona = member.name.split("/", 1)[0]
                nested = records.extractfile(member)
                if nested is not None:
                    found |= process_tar_stream(nested, expected, selected,
                                                claude=claude, nested_persona=persona)
                continue
            matched = match_trace(member.name, expected)
            if matched is None and nested_persona:
                matched = next((value for key, value in expected.items()
                                if value[0] == nested_persona and member.name.endswith(key.split("/", 1)[-1])), None)
            if matched is None:
                continue
            persona, record = matched
            test_id = str(record.get("test_id") or record.get("source_id")).zfill(3)
            key = (persona, test_id)
            if key in found:
                raise ValueError(f"duplicate selected trace for {persona}/{test_id}")
            extracted = records.extractfile(member)
            if extracted is None:
                raise ValueError(f"cannot read {member.name}")
            payload = extracted.read()
            if claude:
                ids = {event.get("session_id") for line in payload.splitlines() if line.strip()
                       if (event := json.loads(line)).get("session_id")}
                if record.get("session_id") and record["session_id"] not in ids:
                    raise ValueError(f"Selected session is absent from {member.name}")
            else:
                trace = json.loads(payload)
                actual_id = (trace.get("session") or {}).get("id")
                if actual_id and record.get("session_id") and actual_id != record["session_id"]:
                    raise ValueError(f"Selected session disagrees with {member.name}")
            events, memory = claude_events(payload) if claude else hermes_events(payload)
            selected[persona][test_id] = clean_test(record, events, memory)
            found.add(key)
    return found


def load_ingestion(zip_file: zipfile.ZipFile, persona: str, local_path: Path | None, row: dict) -> dict:
    names = set(zip_file.namelist())
    if local_path and local_path.exists():
        if local_path.is_file():
            raw = read_json(local_path)
        else:
            raw = read_json(local_path / "summary.json")
            raw["sessions"] = []
            for name in raw.pop("parts"):
                raw["sessions"].extend(read_json(local_path / name)["sessions"])
        if raw.get("configuration_id") != row["id"] or raw.get("persona") != persona:
            raise ValueError(f"Ingestion belongs to a different run: {local_path}")
        return raw
    for name in (f"{persona}/ingestion.json.gz", f"{persona}/ingestion-receipt.json"):
        if name in names:
            payload = zip_file.read(name)
            if name.endswith(".gz"):
                payload = gzip.decompress(payload)
            return {"configuration_id": row["id"], "persona": persona,
                    "record_type": "ingestion_run" if name.endswith(".gz") else "completion_receipt",
                    "record": json.loads(payload)}
    manifest_name = f"{persona}/launch_manifest.json"
    if manifest_name not in names:
        raise ValueError(f"{row['id']}: no ingestion record for {persona}")
    manifest = json.loads(zip_file.read(manifest_name))
    selected = manifest.get("source_seed_artifacts") or {}
    persona_input = (manifest.get("persona_inputs") or {}).get(persona)
    return {"configuration_id": row["id"], "persona": persona,
            "record_type": "selected_frozen_ingestion",
            "record": {"run_id": manifest.get("run_id"), "prepared_at": manifest.get("prepared_at"),
                       "source_seed_artifacts": selected, "persona_input": persona_input}}


def text_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(part.get("text")) for part in value
                          if isinstance(part, dict) and part.get("type") == "text" and part.get("text"))
    return ""


def dated_message(text: str, date: str) -> str:
    prefix = f"[{date}]"
    return text[len(prefix):].strip() if text.startswith(prefix) else text.strip()


def attempt_matches_date(attempt: dict, date: str) -> bool:
    return any(message.get("role") == "user" and isinstance(message.get("content"), str)
               and message["content"].startswith(f"[{date}]") for message in attempt.get("messages") or [])


def accepted_attempt(session: dict) -> dict:
    attempts = session.get("attempts") or []
    dated = [attempt for attempt in attempts if attempt_matches_date(attempt, session.get("date", ""))]
    candidates = dated or attempts
    complete = [attempt for attempt in candidates if attempt.get("messages") or attempt.get("outcome") == "completed"]
    return (complete or candidates or [{}])[-1]


@cache
def canonical_messages(persona: str) -> tuple[str, ...]:
    source = ROOT / "registry" / "personas" / persona / "life_sim.yaml"
    document = yaml.safe_load(source.read_text())
    return tuple(str(session["messages"][0]).strip() for session in document["sessions"])


def compact_ingestion(clean: dict, persona: str) -> dict:
    source_messages = canonical_messages(persona)
    messages = clean["messages"]
    if len(messages) != len(source_messages):
        raise ValueError(
            f"{persona}: ingestion has {len(messages)} messages; source history has {len(source_messages)}"
        )
    responses = []
    for number, (message, source_message) in enumerate(zip(messages, source_messages), 1):
        record = {"message_number": number}
        if str(message.get("message") or "").strip() != source_message:
            record["message"] = str(message.get("message") or "").strip()
        if message.get("response"):
            record["response"] = message["response"]
        if len(record) > 1:
            responses.append(record)
    return {
        "persona": PERSONA_NAMES[persona],
        "source_history": f"registry/personas/{persona}/life_sim.yaml",
        "messages_processed": len(messages),
        "responses": responses,
    }


def clean_native_ingestion(document: dict, persona: str) -> dict:
    processing = []
    for message_number, session in enumerate(document.get("sessions") or [], 1):
        attempts = session.get("attempts") or []
        for attempt_number, attempt in enumerate(attempts, 1):
            record = {
                "message_number": message_number,
                "outcome": attempt.get("outcome"),
                "elapsed_seconds": attempt.get("elapsed_seconds"),
                "token_usage": attempt.get("token_usage") or {},
            }
            if len(attempts) > 1:
                record["attempt"] = attempt_number
            if attempt.get("status_code") is not None:
                record["status_code"] = attempt["status_code"]
            processing.append(record)
    return {
        "persona": PERSONA_NAMES[persona],
        "source_history": f"registry/personas/{persona}/life_sim.yaml",
        "messages_processed": document.get("expected_messages", len(document.get("sessions") or [])),
        "message_processing": processing,
        "memory_at_end": document.get("memory_at_end") or [],
    }


def clean_ingestion(document: dict, persona: str) -> dict:
    if document.get("record_type") == "completed_native_memory_ingestion":
        return clean_native_ingestion(document, persona)
    cleaned = []
    for session in document.get("sessions") or []:
        attempt = accepted_attempt(session)
        messages = session.get("messages") or attempt.get("messages") or []
        source = str(session.get("content") or "")
        if not source:
            source = next((dated_message(str(message.get("content") or ""), str(session.get("date") or ""))
                           for message in messages if message.get("role") == "user"
                           and isinstance(message.get("content"), str)), "")
        response = str(session.get("response") or "")
        if not response:
            response = next((text_content(message.get("content")).strip() for message in reversed(messages)
                             if message.get("role") == "assistant" and text_content(message.get("content")).strip()), "")
        item = {"date": session.get("date"), "message": source.strip()}
        if response:
            item["response"] = response
        cleaned.append(item)
    return compact_ingestion({"messages": cleaned}, persona)


def compact_tests(persona: str, tests: list[dict]) -> tuple[dict, list[dict]]:
    header = {"persona": PERSONA_NAMES[persona]}
    recorded = [test.get("memory_at_start") for test in tests if test.get("memory_at_start")]
    unique = set(recorded)
    if len(unique) == 1:
        header["memory_at_start"] = recorded[0]
        missing = []
        for test in tests:
            if test.pop("memory_at_start", None) is None:
                missing.append(test["id"])
        if missing:
            header["memory_at_start_not_recorded_for"] = missing
    return header, tests


def persona_metrics(result: dict) -> dict:
    tests = result["test_results"]
    costs = [test_cost(test) for test in tests]
    latencies = [test.get("latency_seconds") for test in tests]
    return {
        "passes": sum(bool(test.get("passed", (test.get("grade") or {}).get("passed"))) for test in tests),
        "tests": len(tests),
        "agent_test_cost_usd": math.fsum(costs) if all(value is not None for value in costs) else None,
        "median_latency_seconds": statistics.median(latencies) if all(isinstance(value, (int, float)) for value in latencies) else None,
    }


def persona_costs(cost: dict, results: dict) -> dict:
    breakdown = {}
    for persona in PERSONAS:
        agent = memory = None
        test_total = persona_metrics(results[persona])["agent_test_cost_usd"]
        if "agent_personas" in cost:
            totals = cost["agent_personas"][persona]["totals"]
            agent = math.fsum(totals.get(key, 0) for key in (
                "ingestion_main_session_tier_bound_usd", "ingestion_aux_session_tier_bound_usd",
                "evaluation_main_recorded_usd", "evaluation_aux_session_tier_bound_usd",
                "proposed_missing_seed_session_cost_usd", "proposed_additional_retry_cost_usd"))
        elif "personas" in cost and "agent_usd" in cost["personas"][persona]:
            values = cost["personas"][persona]
            agent = values["agent_usd"]
            memory = values.get("memory_usd", values.get("memory_ingestion_usd", 0) + values.get("memory_evaluation_usd", 0))
        elif "agent_calculation" in cost:
            values = cost["agent_calculation"]["personas"][persona]
            agent = next((values[key] for key in ("agent_total_usd", "recorded_agent_usd",
                         "comparable_total_1h_with_helper_allowance_usd") if key in values), None)
        elif "calculation" in cost:
            calculation = cost["calculation"]
            ingestion = calculation["ingestion"][persona]
            if "main_1h_usd" in ingestion:
                agent = ingestion["main_1h_usd"] + test_total + calculation["ingestion_helper_estimate"][f"{persona}_usd"]
            else:
                agent = ingestion["main_usd"] + ingestion["auxiliary_usd"] + test_total
                agent += calculation["evaluation"]["auxiliary_usd_by_persona"][persona]
                agent += calculation.get("missing_usage_estimate", {}).get(f"{persona}_usd", 0)
                if persona == "riley" and "riley_prior_ingestion_attempts" in calculation:
                    prior = calculation["riley_prior_ingestion_attempts"]
                    agent += prior["main_usd"] + prior["auxiliary_usd"]
            if "backend_ingestion" in calculation:
                memory = calculation["backend_ingestion"]["personas"][persona]["cost_usd"]
        if cost["cost"]["memory_usd"] == 0:
            memory = 0
        elif "backend" in cost["cost"]:
            memory = next(value["backend_ingestion_usd"] for value in cost["cost"]["backend"]["personas"]
                          if value["persona"] == persona)
        elif "memory_calculation" in cost and "personas" in cost["memory_calculation"]:
            values = cost["memory_calculation"]["personas"][persona]
            if "cost_usd" in values:
                memory = values["cost_usd"]
            elif "estimated_accounted_backend_usd" in values:
                memory = values["estimated_accounted_backend_usd"]
            elif "candidate_ingestion_backend_usd" in values:
                memory = values["candidate_ingestion_backend_usd"] + values["candidate_evaluation_backend_usd"]
        breakdown[persona] = {"agent_usd": agent, "memory_usd": memory,
                              "total_usd": agent + memory if agent is not None and memory is not None else None}
    for key in ("agent_usd", "memory_usd", "total_usd"):
        values = [row[key] for row in breakdown.values()]
        if all(value is not None for value in values):
            if not math.isclose(math.fsum(values), cost["cost"][key], abs_tol=1e-7):
                raise ValueError(f"Persona {key} disagrees with published total: {values} vs {cost['cost'][key]}")
    return breakdown


def build_system(row: dict, cache: Path, cost_root: Path, output: Path,
                 ingestion_paths: dict[tuple[str, str], Path]) -> dict:
    destination = output / system_path(row)
    destination.mkdir(parents=True, exist_ok=True)
    archive = row["evidence"]["grades"]
    archive_path = cache / artifact_name(archive["url"])
    verify_artifact(archive_path, archive["sha256"])
    source_path = cache / artifact_name(row["evidence"]["source"]["url"])
    verify_artifact(source_path, row["evidence"]["source"]["sha256"])
    read_json(source_path)
    config_zip_path = cost_root / artifact_name(row["evidence"]["configuration"]["url"])
    verify_artifact(config_zip_path, row["evidence"]["configuration"]["sha256"])

    results = {}
    expected = {}
    ingestion_counts = {}
    with zipfile.ZipFile(config_zip_path) as config_zip:
        cost = json.loads(config_zip.read("cost-calculation.json"))
        settings = {}
        for persona in PERSONAS:
            name = f"{persona}/configuration.json"
            if name in config_zip.namelist():
                settings[persona] = json.loads(config_zip.read(name))
            else:
                manifest = json.loads(config_zip.read(f"{persona}/launch_manifest.json"))
                settings[persona] = {key: manifest[key] for key in (
                    "run_id", "agent", "agent_runtime", "configurations", "judge", "pricing", "evaluation_scope",
                    "runtime_provenance", "source_seed_artifacts") if key in manifest}
            path = result_path(cache, row, persona)
            verify_artifact(path, row["personas"][persona]["source"]["sha256"])
            results[persona] = read_json(path)
            tests = results[persona].get("test_results") or []
            if len(tests) != 200:
                raise ValueError(f"{row['id']}/{persona}: expected 200 tests")
            for record in tests:
                test_id = str(record.get("test_id") or record.get("source_id")).zfill(3)
                for name in expected_trace_name(record, persona, claude=row["harness"]["id"] == "claude-code"):
                    expected[name] = (persona, record)
            ingestion = load_ingestion(config_zip, persona, ingestion_paths.get((row["id"], persona)), row)
            clean = clean_ingestion(ingestion, persona)
            ingestion_counts[persona] = clean["messages_processed"]
            if "message_processing" in clean:
                processing = clean.pop("message_processing")
                memory = clean.pop("memory_at_end")
                write_record_arrays_json(
                    destination / persona / "ingestion.json", clean,
                    [("message_processing", processing, False), ("memory_at_end", memory, False)],
                )
            else:
                responses = clean.pop("responses")
                write_record_array_json(destination / persona / "ingestion.json", clean, "responses", responses)

    first_settings = settings[PERSONAS[0]]
    if isinstance(first_settings.get("agent"), dict):
        agent = first_settings["agent"]
        runtime = {
            "provider": agent.get("provider"),
            "reasoning_effort": agent.get("reasoning_effort"),
            "context_length": agent.get("context_length"),
            "max_turns": agent.get("max_turns"),
        }
    else:
        runtime = {key: first_settings.get(key) for key in (
            "reasoning_effort", "memory_writes", "native_memory", "tools")}
    configuration = {
        "harness": row["harness"]["name"],
        "model": row["model"]["name"],
        "memory": row["memory"]["name"],
        "runtime": {key: value for key, value in runtime.items() if value is not None},
        "personas": [PERSONA_NAMES[persona] for persona in PERSONAS],
        "tests_per_persona": 200,
    }
    write_json(destination / "configuration.json", configuration)

    selected = {persona: {} for persona in PERSONAS}
    with archive_path.open("rb") as raw:
        found = process_tar_stream(raw, expected, selected,
                                   claude=row["harness"]["id"] == "claude-code")
    wanted = {(persona, f"{index:03d}") for persona in PERSONAS for index in range(1, 201)}
    if found != wanted:
        missing = sorted(wanted - found)
        raise ValueError(f"{row['id']}: missing {len(missing)} selected traces: {missing[:10]}")

    persona_rows = {}
    subtotals = persona_costs(cost, results)
    clean_cost = {
        "total_usd": round(float(row["total_cost_usd"]), 6),
        "agent_usd": round(float(cost["cost"]["agent_usd"]), 6),
        "memory_usd": round(float(cost["cost"]["memory_usd"]), 6),
        "personas": {},
    }
    for persona in PERSONAS:
        metrics = persona_metrics(results[persona])
        persona_rows[persona] = metrics
        test_header, tests = compact_tests(
            persona, [selected[persona][f"{index:03d}"] for index in range(1, 201)]
        )
        write_record_array_json(destination / persona / "tests.json", test_header, "tests", tests,
                                fields_on_separate_lines=True)
        clean_cost["personas"][persona] = {
            key: (round(float(value), 6) if value is not None else None)
            for key, value in subtotals[persona].items()
        }
        readme = [
            f"# {PERSONA_NAMES[persona]}", "",
            f"{metrics['passes']} of 200 tests passed. Median latency was {fmt_latency(metrics['median_latency_seconds'])}.", "",
            f"- [Ingestion evidence: {ingestion_counts[persona]:,} messages](ingestion.json)",
            "- [Complete test records](tests.json)",
            "- [Cost](../cost.json)", "",
            "## Tests", "", "| Test | Result | Latency | Agent call cost |", "| --- | --- | ---: | ---: |",
        ]
        for test in sorted(results[persona]["test_results"], key=lambda value: str(value["test_id"])):
            test_id = str(test["test_id"]).zfill(3)
            readme.append(f"| {test_id} | {'Pass' if test['passed'] else 'Fail'} | {fmt_latency(test.get('latency_seconds'))} | {fmt_cost(test_cost(test))} |")
        (destination / persona / "README.md").write_text("\n".join(readme))

    write_json(destination / "cost.json", clean_cost)

    title = f"{row['harness']['name']} + {row['model']['name']} + {row['memory']['name']}"
    readme = [
        f"# {title}", "",
        f"This agent passed {row['passes']} of 600 tests. Its complete run cost ${row['total_cost_usd']:.2f}, and its median task latency was {row['median_latency_seconds']:.1f} seconds.", "",
        f"Agent model: ${cost['cost']['agent_usd']:.2f}. Memory processing: ${cost['cost']['memory_usd']:.2f}.", "",
        "| Persona | Passed | Test agent cost | Median latency | Runs |", "| --- | ---: | ---: | ---: | --- |",
    ]
    for persona in PERSONAS:
        metrics = persona_rows[persona]
        readme.append(f"| {PERSONA_NAMES[persona]} | {metrics['passes']} / 200 | {fmt_cost(metrics['agent_test_cost_usd'])} | {fmt_latency(metrics['median_latency_seconds'])} | [View]({persona}/) |")
    readme.extend(("", "- [Configuration](configuration.json)", "- [Cost](cost.json)", ""))
    (destination / "README.md").write_text("\n".join(readme))
    return {"row": row, "path": system_path(row), "personas": persona_rows}


def ingestion_overrides(values: list[str]) -> dict[tuple[str, str], Path]:
    result = {}
    for value in values:
        key, separator, path = value.partition("=")
        configuration, separator2, persona = key.rpartition(":")
        if not separator or not separator2 or persona not in PERSONAS:
            raise ValueError("--ingestion must use CONFIGURATION_ID:PERSONA=FILE")
        result[(configuration, persona)] = Path(path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--evidence-cache", type=Path, required=True)
    parser.add_argument("--cost-records", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ingestion", action="append", default=[])
    parser.add_argument("--ingestion-root", type=Path)
    parser.add_argument("--update-links", action="store_true")
    args = parser.parse_args()
    report = read_json(args.results)
    for row in report["configurations"]:
        row["release_sha256"] = report.get("release_sha256")
    overrides = ingestion_overrides(args.ingestion)
    if args.ingestion_root:
        for row in report["configurations"]:
            for persona in PERSONAS:
                path = args.ingestion_root / system_path(row) / persona / "ingestion.json"
                if path.is_file():
                    overrides.setdefault((row["id"], persona), path)
                else:
                    folder = path.with_suffix("")
                    if (folder / "summary.json").is_file():
                        overrides.setdefault((row["id"], persona), folder)
    built = []
    for row in report["configurations"]:
        built.append(build_system(row, args.evidence_cache, args.cost_records, args.output, overrides))
        print(f"{row['id']}: 600 selected tests written", flush=True)
    lines = ["# Official run records", "",
             "These are the runs behind the official DolphinBench leaderboard. Each agent includes its configuration, cost, ingestion evidence, test responses, tool calls, and grades.", "",
             "| Harness | Model | Memory | Accuracy | Total cost | Median latency | Runs |",
             "| --- | --- | --- | ---: | ---: | ---: | --- |"]
    built.sort(key=lambda item: item["row"]["harness"]["id"] != "hermes")
    for item in built:
        row = item["row"]
        lines.append(f"| {row['harness']['name']} | {row['model']['name']} | {row['memory']['name']} | {row['passes']} / 600 | ${row['total_cost_usd']:.2f} | {row['median_latency_seconds']:.1f} s | [View]({item['path'].as_posix()}/) |")
    lines.append("")
    (args.output / "README.md").write_text("\n".join(lines))
    if args.update_links:
        linked = read_json(args.results)
        for row in linked["configurations"]:
            row["runs_url"] = f"https://github.com/mem0ai/dolphinbench/tree/main/results/{system_path(row).as_posix()}"
        write_json(args.results, linked)
    print(json.dumps({"systems": len(built), "personas": len(built) * 3, "tests": len(built) * 600}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
