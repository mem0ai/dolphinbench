#!/usr/bin/env python3
"""
DolphinBench Life Simulation runner.

Seeds memory through a sequence of realistic sessions (the "life simulation"),
then runs test queries against the accumulated memory state. NO per-test wipe —
the whole point is testing retrieval from a cluttered, realistic memory store.

Usage:
  python3 run_simulation.py --sim simulation/life_sim.yaml \
      --provider builtin --out results/sim_builtin.json --verbose
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

DOLPHINBENCH_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(DOLPHINBENCH_ROOT))

from harness.environment import get_setting

# Load .env
_ENV_FILE = Path.home() / ".hermes" / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        _k, _v = _k.strip(), _v.strip().strip('"').strip("'")
        if _k and _v and _k not in os.environ:
            os.environ[_k] = _v

from graders.mechanical import grade_regex, grade_tool_trace, grade_hybrid, load_tool_calls
from graders.llm_judge import grade_llm_judge
from harness.costing import (
    UnknownModelError,
    compute_call_cost,
    load_pricing,
    pricing_version,
)
from harness.hermes_driver import (
    hermes_profiles_dir, run_hermes,
    _SEED_RECEIPT_ENV,
    _failure_details,
    _retry_delay,
    _seed_message_was_delivered,
    _seed_delivery_state,
    _validate_hermes_runtime_tools,
    _fallback_session_calls_for_grading,
)
from harness.claude_driver import run_claude

# Example drivers (shipped under examples/ so external users can copy them).
# Imported lazily-tolerant: if someone clones just the harness without the
# examples dir, the Hermes providers still work.
try:
    from examples.dummy_driver import run_dummy  # noqa: E402
except ImportError:
    run_dummy = None  # type: ignore[assignment]
try:
    from examples.openai_direct_driver import run_openai_direct  # noqa: E402
except ImportError:
    run_openai_direct = None  # type: ignore[assignment]
from harness.memory_capture import (
    diff_memory_dumps,
    snapshot_for_provider,
)


def load_test(test_id: str) -> dict[str, Any]:
    """Load one numeric test for the active persona."""
    persona = get_setting("DOLPHINBENCH_PERSONA")
    if not persona:
        raise RuntimeError("DOLPHINBENCH_PERSONA must be set before loading tests")
    bare_id = str(test_id).zfill(3)
    directory = Path(os.environ.get("DOLPHINBENCH_TESTS_DIR") or DOLPHINBENCH_ROOT / "tests" / persona)
    path = directory / f"{bare_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"test {bare_id} not found for {persona}")
    from harness.dataset import load_test as load_test_file
    return load_test_file(path)


def _test_narrative_time(spec: dict[str, Any]) -> str:
    """Return the test's own narrative anchor rather than a global clock."""
    value = spec.get("narrative_anchor_date")
    if value is None:
        raise RuntimeError("test is missing narrative_anchor_date")
    return str(value)


def _wait_for_honcho(expected_seed_messages: int) -> dict[str, Any]:
    """Wait until the configured Honcho workspace has consumed seed work.

    Honcho's queue endpoint is workspace-scoped. It does not expose a stable
    identifier for each message submitted through the Hermes plugin, so this
    function can prove that this isolated workspace is idle, but cannot map a
    queue item back to an individual seed message. The result records that
    limit rather than pretending the API provides more evidence than it does.
    """
    base_url = os.environ.get("HONCHO_BASE_URL", "").strip()
    workspace = os.environ.get("HONCHO_WORKSPACE_ID", "").strip()
    if not workspace:
        raise RuntimeError("HONCHO_WORKSPACE_ID is required for the seed barrier")

    from honcho import Honcho

    client_args: dict[str, Any] = {
        "api_key": os.environ.get("HONCHO_API_KEY") or None,
        "workspace_id": workspace,
        "timeout": 300,
    }
    if base_url:
        client_args["base_url"] = base_url
    client = Honcho(**client_args)
    deadline = time.monotonic() + float(
        os.environ.get("DOLPHINBENCH_HONCHO_QUEUE_TIMEOUT", "7200")
    )
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status = client.queue_status()
        last = status.model_dump()
        total = int(last.get("total_work_units", 0))
        pending = int(last.get("pending_work_units", 0))
        active = int(last.get("in_progress_work_units", 0))
        completed = int(last.get("completed_work_units", 0))
        if total > 0 and pending == 0 and active == 0 and completed == total:
            return {
                **last,
                "submitted_seed_messages": expected_seed_messages,
                "completion_evidence": "workspace_queue_drained",
                "completion_evidence_limit": (
                    "Honcho queue status is scoped to the isolated workspace but "
                    "does not identify individual submitted messages."
                ),
            }
        time.sleep(2)
    raise TimeoutError(f"Honcho seed queue did not finish: {last}")


def _safe_snapshot(
    provider: str,
    profile: str | None,
) -> dict:
    """snapshot_for_provider but never raises and always returns a dict.

    The benchmark loop should never abort because of memory capture, so
    we wrap defensively here on top of memory_capture's own try/except.
    """
    try:
        return snapshot_for_provider(provider, profile)
    except Exception as exc:  # noqa: BLE001
        return {"snapshot_at": "", "error": f"snapshot_failed: {exc}"}


def _memory_dump_path(out_path: Path, suffix: str) -> Path:
    """Derive a sibling path: results/sim_x.json + 'seed_end_memory'
    becomes results/sim_x.seed_end_memory.json.
    """
    return out_path.with_suffix("." + suffix + ".json")


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON to `path` atomically via a tmp-file + rename.

    Prevents partial files from being observed if the process is killed
    mid-write. Parent dir is created on demand.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, path)


def _load_partial_results(
    out_path: Path,
    simulation_name: str | None,
    provider: str,
) -> dict[str, Any]:
    """Load an unfinished run without altering any completed work."""
    if not out_path.exists():
        raise RuntimeError(f"cannot resume: results file does not exist: {out_path}")
    try:
        results = json.loads(out_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot resume: invalid results file {out_path}: {exc}") from exc
    if not isinstance(results, dict):
        raise RuntimeError(f"cannot resume: results file is not an object: {out_path}")
    partial_seed_run = (
        results.get("run_mode") == "seed_only"
        and (results.get("summary") or {}).get("seed_complete") is False
    )
    if results.get("ended") and not partial_seed_run:
        raise RuntimeError("cannot resume a completed run; create a new output path")
    if results.get("provider") != provider:
        raise RuntimeError(
            f"cannot resume: results provider is {results.get('provider')!r}, not {provider!r}"
        )
    if results.get("simulation") != simulation_name:
        raise RuntimeError("cannot resume: results file belongs to another simulation")
    for key in ("seed_sessions", "seed_calls", "test_results"):
        if not isinstance(results.get(key, []), list):
            raise RuntimeError(f"cannot resume: {key} is not a list")
        results.setdefault(key, [])
    if partial_seed_run:
        # Older bounded ingestion runs incorrectly marked each group as final.
        previous_end = results.pop("ended", None)
        if previous_end:
            results.setdefault("last_seed_group_ended_at", previous_end)
        results.pop("phase1_ended_at", None)
    results["resumed_at"] = datetime.now(timezone.utc).isoformat()
    return results


def _seed_key(session_id: str, message_index: int) -> tuple[str, int]:
    return (str(session_id), int(message_index))


def _seed_item_id(session_id: str, message_index: int) -> str:
    """Return the stable name for one submitted life-simulation message."""
    return f"{session_id}:{int(message_index)}"


def _expected_seed_items(sim: dict[str, Any]) -> dict[str, tuple[str, int]]:
    """Map every life-simulation message to its stable seed item name."""
    expected: dict[str, tuple[str, int]] = {}
    for session in sim.get("sessions", []):
        session_id = str(session.get("id", ""))
        if not session_id:
            raise RuntimeError("life simulation contains a seed session without an id")
        for message_index, _message in enumerate(session.get("messages", [])):
            item_id = _seed_item_id(session_id, message_index)
            if item_id in expected:
                raise RuntimeError(f"life simulation repeats seed item id: {item_id}")
            expected[item_id] = _seed_key(session_id, message_index)
    return expected


def _seed_receipt_path(out_path: Path, provider: str) -> Path | None:
    if provider not in _SEED_RECEIPT_ENV:
        return None
    return out_path.with_suffix(f".{provider}_seed_receipts.jsonl")


def _can_resume_without_results(out_path: Path, provider: str) -> bool:
    """Only receipt-backed providers can resume a pre-results crash."""
    receipt_path = _seed_receipt_path(out_path, provider)
    return receipt_path is not None and receipt_path.exists()


def _prepare_seed_receipt_file(receipt_path: Path, *, resuming: bool) -> bool:
    """Create an empty file for a fresh run without changing a resumed run."""
    if resuming:
        return receipt_path.exists()
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text("")
    return True


def _read_seed_receipts(receipt_path: Path) -> list[dict[str, Any]]:
    """Read complete JSONL receipt records without accepting malformed lines."""
    if not receipt_path.exists():
        return []
    try:
        lines = receipt_path.read_text().splitlines()
    except OSError as exc:
        raise RuntimeError(f"cannot read seed receipt file {receipt_path}: {exc}") from exc
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"invalid JSON seed receipt at {receipt_path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(record, dict):
            raise RuntimeError(
                f"seed receipt at {receipt_path}:{line_number} is not a JSON object"
            )
        records.append(record)
    return records


def _validate_seed_receipts(
    receipt_path: Path,
    provider: str,
    expected_items: dict[str, tuple[str, int]],
    *,
    require_all: bool,
) -> dict[str, Any]:
    """Validate automatic provider receipts and return their recovered seed keys.

    Explicit conclude receipts are intentionally ignored. They are a different
    provider operation and cannot prove that a life-simulation message synced.
    """
    automatic_ids: list[str] = []
    for record in _read_seed_receipts(receipt_path):
        if record.get("operation_kind") != "automatic_sync_turn":
            continue
        if record.get("provider") != provider:
            raise RuntimeError(
                f"unexpected automatic seed receipt provider in {receipt_path}: "
                f"{record.get('provider')!r}"
            )
        item_id = record.get("seed_item_id")
        if not isinstance(item_id, str) or not item_id:
            raise RuntimeError(
                f"automatic {provider} seed receipt is missing seed_item_id: {record}"
            )
        automatic_ids.append(item_id)

    counts: dict[str, int] = {}
    for item_id in automatic_ids:
        counts[item_id] = counts.get(item_id, 0) + 1
    duplicates = sorted(item_id for item_id, count in counts.items() if count != 1)
    extras = sorted(item_id for item_id in counts if item_id not in expected_items)
    missing = sorted(item_id for item_id in expected_items if item_id not in counts)
    if duplicates:
        raise RuntimeError(
            f"duplicate automatic {provider} seed receipts: {duplicates[:10]}"
        )
    if extras:
        raise RuntimeError(
            f"unexpected automatic {provider} seed receipts: {extras[:10]}"
        )
    if require_all and missing:
        raise RuntimeError(
            f"missing automatic {provider} seed receipts: {missing[:10]}"
        )
    return {
        "path": str(receipt_path),
        "provider": provider,
        "automatic_receipt_count": len(automatic_ids),
        "expected_seed_item_count": len(expected_items),
        "completed_seed_keys": {expected_items[item_id] for item_id in counts},
    }


def _receipt_resume_seed_calls(
    results: dict[str, Any],
    expected_items: dict[str, tuple[str, int]],
    completed_seed_keys: set[tuple[str, int]],
) -> None:
    """Record receipt-only recovery so the next resume remains self-describing."""
    existing: dict[tuple[str, int], dict[str, Any]] = {}
    for call in results.get("seed_calls", []):
        key = _seed_key(str(call.get("session_id")), int(call.get("message_index", -1)))
        existing[key] = call
    item_ids_by_key = {key: item_id for item_id, key in expected_items.items()}
    for seed_key in sorted(completed_seed_keys):
        if seed_key in existing:
            was_already_recorded = (
                existing[seed_key].get("seed_delivery_state") == "delivered"
                or existing[seed_key].get("seed_message_delivered") is True
            )
            existing[seed_key]["seed_item_id"] = item_ids_by_key[seed_key]
            existing[seed_key]["seed_message_delivered"] = True
            existing[seed_key]["seed_delivery_state"] = "delivered"
            existing[seed_key]["matching_seed_receipt"] = True
            if not was_already_recorded:
                existing[seed_key]["recovered_from_seed_receipt"] = True
            continue
        results["seed_calls"].append({
            "session_id": seed_key[0],
            "message_index": seed_key[1],
            "seed_item_id": item_ids_by_key[seed_key],
            "driver_ok": None,
            "seed_message_delivered": True,
            "seed_delivery_state": "delivered",
            "matching_seed_receipt": True,
            "recovered_from_seed_receipt": True,
            "cost_unavailable_reason": "runner_stopped_before_call_accounting_was_saved",
        })


def _receipt_ambiguous_seed_keys(
    results: dict[str, Any],
    completed_seed_keys: set[tuple[str, int]],
) -> set[tuple[str, int]]:
    """Find previous provider calls that lack both a receipt and a safe failure."""
    ambiguous: set[tuple[str, int]] = set()
    for call in results.get("seed_calls", []):
        key = _seed_key(str(call.get("session_id")), int(call.get("message_index", -1)))
        if key in completed_seed_keys:
            continue
        if call.get("seed_delivery_state") != "known_not_delivered":
            ambiguous.add(key)
    return ambiguous


def _completed_seed_keys(results: dict[str, Any]) -> set[tuple[str, int]]:
    return {
        _seed_key(str(call.get("session_id")), int(call.get("message_index", -1)))
        for call in results.get("seed_calls", [])
        if call.get("seed_delivery_state") == "delivered"
        or call.get("seed_message_delivered") is True
    }


def _ambiguous_seed_keys(results: dict[str, Any]) -> set[tuple[str, int]]:
    """Find incomplete messages that a prior process may have submitted."""
    completed = _completed_seed_keys(results)
    ambiguous: set[tuple[str, int]] = set()
    for call in results.get("seed_calls", []):
        key = _seed_key(str(call.get("session_id")), int(call.get("message_index", -1)))
        if key in completed:
            continue
        # Older partial files did not record delivery state. Treat them as
        # uncertain rather than replaying a possibly accepted message.
        if call.get("seed_delivery_state") != "known_not_delivered":
            ambiguous.add(key)
    return ambiguous


def _completed_test_ids(results: dict[str, Any]) -> set[str]:
    """Completed test entries are durable outcomes and must not be rerun."""
    return {str(test.get("test_id")) for test in results.get("test_results", [])}


HERMES_PROFILES_DIR = hermes_profiles_dir()


def _copy_hermes_profile(src_profile: str, dst_profile: str) -> None:
    """Copy a Hermes profile including seeded sessions and memory files.

    Used for Phase 2 isolation: each test gets the same post-seeding profile
    state, so native session_search remains available but cannot see earlier
    benchmark test queries.
    """
    src = HERMES_PROFILES_DIR / src_profile
    dst = HERMES_PROFILES_DIR / dst_profile
    if not src.exists():
        raise FileNotFoundError(f"Hermes profile not found: {src}")
    if dst.exists():
        shutil.rmtree(dst)
    ignore = shutil.ignore_patterns("logs", "workspace", "__pycache__")
    shutil.copytree(src, dst, ignore=ignore)


def _remove_hermes_profile(profile: str) -> None:
    dst = HERMES_PROFILES_DIR / profile
    if dst.exists():
        shutil.rmtree(dst)


def _archive_hermes_session(
    session_path: str | None,
    session_id: str | None,
    trace_dir: Path,
    filename: str,
) -> str | None:
    """Save one Hermes interaction without copying the growing profile database."""
    if not session_path:
        return None
    source = Path(session_path)
    if not source.is_file():
        return None
    trace_dir.mkdir(parents=True, exist_ok=True)
    destination = trace_dir / filename

    if source.name != "state.db":
        shutil.copy2(source, destination)
        return str(destination.resolve())
    if not session_id:
        return None

    connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    try:
        session = connection.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,),
        ).fetchone()
        if session is None:
            return None
        messages = connection.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,),
        ).fetchall()
        usage = connection.execute(
            "SELECT * FROM session_model_usage WHERE session_id = ? "
            "ORDER BY model, billing_provider, billing_base_url, billing_mode, task",
            (session_id,),
        ).fetchall()
        prompt = None
        prompt_hash = session["system_prompt_hash"]
        if prompt_hash:
            prompt = connection.execute(
                "SELECT * FROM system_prompts WHERE hash = ?", (prompt_hash,),
            ).fetchone()
    finally:
        connection.close()

    payload = {
        "format": "dolphinbench_hermes_session_trace_v1",
        "source_database": str(source.resolve()),
        "session": dict(session),
        "system_prompt": dict(prompt) if prompt is not None else None,
        "messages": [dict(message) for message in messages],
        "model_usage": [dict(row) for row in usage],
    }
    recording = source.parent / "submission-traces" / f"{session_id}.jsonl"
    if recording.is_file():
        from harness.submission_capture import read_recording
        try:
            payload["submission"] = read_recording(recording)
        except (ValueError, KeyError, TypeError) as exc:
            payload["submission_error"] = str(exc)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    os.replace(temporary, destination)
    return str(destination.resolve())


def _honcho_plugin_errors(profile: str) -> list[str]:
    """Return Honcho plugin failures that make a run's memory state unsafe."""
    log_path = HERMES_PROFILES_DIR / profile / "logs" / "agent.log"
    if not log_path.is_file():
        return []
    return [
        line.strip()
        for line in log_path.read_text(errors="replace").splitlines()
        if "ERROR plugins.memory.honcho" in line
    ]


def _require_healthy_honcho(profile: str, phase: str) -> None:
    errors = _honcho_plugin_errors(profile)
    if errors:
        sample = "\n".join(errors[-5:])
        raise RuntimeError(
            f"Honcho {phase} produced {len(errors)} provider error(s); "
            f"the run cannot be scored. Last errors:\n{sample}"
        )


def _wait_for_mem0_event_log(
    event_log_path: Path,
    expected_user_id: str | None = None,
) -> dict[str, Any]:
    """Wait for Mem0 Platform extraction events recorded during seeding.

    The Mem0 plugin writes one JSON line per add()/conclude() call when
    DOLPHINBENCH_MEM0_EVENT_LOG is set. Waiting once here keeps Phase 1 fast while
    still ensuring Phase 2 starts only after platform-side extraction has
    completed.
    """
    if not event_log_path.exists():
        raise RuntimeError(f"Mem0 seed event log is missing: {event_log_path}")

    event_ids: list[str] = []
    rows: list[dict[str, Any]] = []
    for line in event_log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        event_id = row.get("event_id")
        if event_id and event_id not in event_ids:
            event_ids.append(event_id)
            rows.append(row)

    if not event_ids:
        raise RuntimeError("Mem0 seeding produced no completion events")
    wrong_scope = [
        row for row in rows
        if (expected_user_id and row.get("user_id") != expected_user_id)
        or row.get("agent_id") is not None
    ]
    if wrong_scope:
        raise RuntimeError(
            "Mem0 seed event log contains event(s) outside this run's memory scope"
        )

    import urllib.request

    api_key = os.environ.get("MEM0_API_KEY", "")
    if not api_key:
        raise RuntimeError("MEM0_API_KEY is required for the seed event barrier")
    mem0_user_id = hashlib.md5(api_key.encode()).hexdigest()
    host = os.environ.get("MEM0_API_HOST", "https://api.mem0.ai").rstrip("/")

    def read_event(event_id: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{host}/v1/event/{event_id}/",
            headers={
                "Authorization": f"Token {api_key}",
                "Mem0-User-ID": mem0_user_id,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read().decode("utf-8"))

    timeout_s = float(os.environ.get("DOLPHINBENCH_MEM0_EVENT_BARRIER_TIMEOUT", "2400"))
    poll_s = float(os.environ.get("DOLPHINBENCH_MEM0_EVENT_POLL", "1.5"))
    deadline = time.monotonic() + timeout_s
    pending = set(event_ids)
    failed: dict[str, Any] = {}
    last_status: dict[str, str] = {}

    while pending:
        for event_id in list(pending):
            try:
                event = read_event(event_id)
                status = (
                    event.get("status")
                    or (event.get("event") or {}).get("status")
                    or (event.get("data") or {}).get("status")
                    or ""
                )
                normalized = str(status or "").lower()
                last_status[event_id] = normalized
                if normalized in {"succeeded", "success", "completed", "complete", "done"}:
                    pending.remove(event_id)
                elif normalized in {"failed", "failure", "error", "errored"}:
                    failed[event_id] = event
                    pending.remove(event_id)
            except Exception as exc:  # noqa: BLE001
                last_status[event_id] = f"poll_error:{exc}"

        if not pending:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Timed out waiting for Mem0 seed events: "
                f"{len(pending)}/{len(event_ids)} still pending; "
                f"sample={list(pending)[:5]}"
            )
        time.sleep(poll_s)

    if failed:
        raise RuntimeError(
            f"Mem0 seed event failures: {list(failed)[:5]} "
            f"({len(failed)} total)"
        )

    return {
        "event_count": len(event_ids),
        "completed": len(event_ids),
        "event_ids": event_ids,
        "completion_evidence": "every uniquely logged platform event completed",
        "completion_evidence_limit": (
            "The plugin records platform write events, not a one-to-one seed-message "
            "identifier; one agent turn may create zero or several write events."
        ),
        "last_status_sample": dict(list(last_status.items())[:5]),
    }


def _snapshot_dir_for_run(out_path: Path, run_id: str) -> Path:
    """Where per-test memory snapshots land for this run.

    Lives under `results/memory_snapshots/<run_id>/` so the snapshots
    are grouped by run and easy to sweep / archive independently of the
    top-level results JSON. `run_id` is derived from the output file
    stem so two runs writing to different result files don't collide.
    """
    return out_path.parent / "memory_snapshots" / run_id


def _per_test_snapshot_path(
    snap_dir: Path,
    test_index: int,
    source_id: str,
    phase: str,
) -> Path:
    """Filename: test_<NNN>_<source_id>_<phase>.json.

    `test_index` is 1-based (first test = 001) and zero-padded so
    directory listings sort naturally. `phase` is "pre" or "post".
    `source_id` is lightly sanitized (path separators stripped) so an
    unusual test id can't escape the snapshot dir.
    """
    safe_src = str(source_id).replace("/", "_").replace("\\", "_")
    return snap_dir / f"test_{test_index:03d}_{safe_src}_{phase}.json"

SEED_PER_ATTEMPT_TIMEOUT_SEC = 180
HERMES_SUBPROCESS_TIMEOUT_SEC = None


def _hermes_isolation_base(profile: str | None, out_path: Path) -> str:
    """Build a bounded profile prefix unique to the absolute result location."""
    identity = f"{profile}-{out_path.resolve()}" if profile else str(out_path.resolve())
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", identity)
    digest = hashlib.sha256(identity.encode()).hexdigest()[:8]
    # Leave room for `-seed` or `-tNNN` under Hermes' 64-character limit.
    return f"{slug[:43]}-{digest}"


def _require_hermes_test_safety(
    provider: str,
    read_only_test_memory: bool,
    isolate_hermes_test_sessions: bool,
) -> None:
    """Refuse a Hermes run that could let one evaluation affect another."""
    if provider not in HERMES_PROVIDERS:
        return
    missing_protocol: list[str] = []
    if not read_only_test_memory:
        missing_protocol.append("read-only Phase 2 memory")
    if not isolate_hermes_test_sessions:
        missing_protocol.append("a fresh copy of seeded sessions per test")
    if missing_protocol:
        raise SystemExit(
            "FATAL: independent DolphinBench tests require "
            + " and ".join(missing_protocol)
            + ". These protections cannot be disabled for Hermes runs."
        )


_AGENT_INFERENCE_COST_SCOPE = (
    "Outer-agent token inference plus local Honcho reasoning tokens when its "
    "trace is available; embeddings and hosted-provider fees are separate or unavailable."
)


PROVIDER_TO_PROFILE = {
    "builtin": "dolphinbench-builtin",
    "builtin_dolphinbench": "dolphinbench-builtin",  # DolphinBench-clean isolated clone; per-persona memory wipe
    "honcho": "dolphinbench-honcho",
    "mem0": "dolphinbench-mem0",
    "hindsight": "dolphinbench-hindsight",
    "supermemory": "dolphinbench-supermemory",
    "dummy": None,   # no profile / no memory — see examples/dummy_driver.py
    "openai_direct": None,  # see examples/openai_direct_driver.py
}

# Providers that use the hermes driver
HERMES_PROVIDERS = {
    "builtin", "builtin_dolphinbench", "honcho", "mem0", "hindsight", "supermemory",
}
# Simple no-memory baseline / example providers wired from examples/.
EXAMPLE_PROVIDERS = {"dummy", "openai_direct"}

# Persona-specific paths are resolved at runtime.
TESTS_DIR = DOLPHINBENCH_ROOT / "tests"

MOCK_STATE_PATH = Path(get_setting("DOLPHINBENCH_STATE_PATH", DOLPHINBENCH_ROOT / "mock_mcp" / "state.json"))
MOCK_LOG_PATH = Path(get_setting("DOLPHINBENCH_LOG_PATH", DOLPHINBENCH_ROOT / "mock_mcp" / "calls.jsonl"))
# Baseline state path is now persona-namespaced — resolved at run time
# from registry/personas.yaml. The module-level constant is kept only
# as a fallback for legacy callers; the canonical lookup is
# `resolve_persona_paths(persona_id).baseline_state`.
BASELINE_STATE = DOLPHINBENCH_ROOT / "mock_mcp" / "state" / "morgan_baseline.json"


# ────────────────────────────────────────────────────────────────────────────
# Persona resolution
# ────────────────────────────────────────────────────────────────────────────
# As of the persona-namespace refactor, every persona-bound artifact lives
# under registry/personas/<id>/, tests/<id>/, mock_mcp/state/<id>_baseline.json
# and mock_mcp/manifests/<id>.yaml. The harness resolves these through
# registry/personas.yaml — no hardcoded persona branches anywhere.


class _PersonaPaths:
    """Resolved on-disk paths for a single persona."""

    def __init__(self, persona_id: str, entry: dict) -> None:
        self.id = persona_id
        self.name = entry.get("name", persona_id)
        self.persona_dir = DOLPHINBENCH_ROOT / "registry" / "personas" / persona_id
        self.life_sim = self.persona_dir / "life_sim.yaml"
        self.tests_dir = DOLPHINBENCH_ROOT / "tests" / persona_id
        manifest_name = entry.get("mock_manifest", f"{persona_id}.yaml")
        self.mock_manifest = DOLPHINBENCH_ROOT / "mock_mcp" / "manifests" / manifest_name
        # baseline_state path is recorded in the manifest, but we also
        # expose the conventional location here so callers don't need
        # to parse the manifest for trivial uses.
        self.baseline_state = DOLPHINBENCH_ROOT / "mock_mcp" / "state" / f"{persona_id}_baseline.json"
        self.results_dir = DOLPHINBENCH_ROOT / "results" / persona_id


def resolve_persona_paths(persona_id: str | None) -> _PersonaPaths:
    """Look up a persona by id in registry/personas.yaml.

    `persona_id` falls back to env var DOLPHINBENCH_PERSONA when None. With
    neither set, raises SystemExit with a clear message — there is no
    silent default after the persona-namespace refactor.
    """
    pid = persona_id or get_setting("DOLPHINBENCH_PERSONA")
    if not pid:
        raise SystemExit(
            "DolphinBench persona is required: pass --persona <id> or set "
            "DOLPHINBENCH_PERSONA=<id>. Available personas live in "
            "registry/personas.yaml."
        )
    personas_path = DOLPHINBENCH_ROOT / "registry" / "personas.yaml"
    if not personas_path.exists():
        raise SystemExit(f"registry/personas.yaml not found at {personas_path}")
    data = yaml.safe_load(personas_path.read_text()) or {}
    for entry in data.get("personas", []) or []:
        if entry.get("id") == pid:
            return _PersonaPaths(pid, entry)
    raise SystemExit(
        f"Persona '{pid}' not found in {personas_path}. "
        f"Known ids: {[e.get('id') for e in data.get('personas', [])]}"
    )


def _guard_hermes_profile_tools(profile: str, provider: str) -> None:
    """Require the container boundary and the selected memory integration."""
    if provider not in HERMES_PROVIDERS:
        return
    from reference.execution.agent_container import image_backend
    try:
        image_backend(os.environ.get("DOLPHINBENCH_AGENT_IMAGE", ""))
    except ValueError as exc:
        raise SystemExit("Set DOLPHINBENCH_AGENT_IMAGE to the isolated Hermes image ID before running") from exc
    required = {"dolphinbench-apps", "memory", "session_search"}
    provider_toolset = {
        "builtin": "memory",
        "builtin_dolphinbench": "memory",
        "honcho": "honcho",
        "mem0": "mem0",
        "hindsight": "hindsight",
        "supermemory": "supermemory",
    }.get(provider)
    if provider_toolset and provider_toolset != "memory":
        required.add(provider_toolset)

    explicit = os.environ.get("DOLPHINBENCH_HERMES_TOOLSETS", "")
    if explicit:
        requested = {x.strip() for x in explicit.split(",") if x.strip()}
        missing = sorted(required - requested)
        if missing:
            raise SystemExit(
                "FATAL: DOLPHINBENCH_HERMES_TOOLSETS is missing required fair-run "
                f"toolsets {missing}. Use "
                "DOLPHINBENCH_HERMES_TOOLSETS=dolphinbench-apps,memory,session_search,<provider>."
            )
        return

    cfg_path = HERMES_PROFILES_DIR / profile / "config.yaml"
    if not cfg_path.exists():
        return

    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    configured: set[str] = set()
    top_level = cfg.get("toolsets")
    if isinstance(top_level, list):
        configured.update(str(x) for x in top_level)
    platform_toolsets = cfg.get("platform_toolsets") or {}
    if isinstance(platform_toolsets, dict):
        for val in platform_toolsets.values():
            if isinstance(val, list):
                configured.update(str(x) for x in val)

    missing = sorted(required - configured)
    if missing:
        raise SystemExit(
            f"FATAL: Hermes profile '{profile}' is missing required fair-run "
            f"toolsets {missing}. It must expose DolphinBench mock tools plus native "
            "Hermes memory/search and the selected memory provider."
        )

# Per-test tool-scoping config. The mock MCP reads this at startup/reload
# and registers only the listed tools.
#   file absent                        → all tools active (default)
#   {"active_tools": []}               → zero tools registered
#   {"active_tools": ["a", "b", ...]}  → exactly those tools registered
MOCK_TOOL_CONFIG = Path(get_setting(
    "DOLPHINBENCH_TOOL_CONFIG_PATH",
    "/tmp/dolphinbench_mock_config.json",
))


def reset_mock_mcp(baseline: Path | None = None):
    """Copy the persona baseline into the live mock state and truncate the log.

    The caller passes the persona's baseline_state path so the same
    function works across personas. The legacy module-level BASELINE_STATE
    constant is the fallback for callers that haven't been threaded yet.
    """
    src = baseline or BASELINE_STATE
    if src.exists():
        shutil.copy(src, MOCK_STATE_PATH)
    MOCK_LOG_PATH.write_text("")


def _write_mock_tool_config(tools: list | None) -> None:
    """Write the per-test tool scoping config before MCP reload.

    Semantics (matters — ``[]`` vs ``None`` are NOT the same):

    - ``tools is None`` → no scoping requested. Clear any previous
      config file so the mock falls back to registering ALL tools.
      This is the default for tests that omit ``tools:`` in YAML.
    - ``tools == []`` → scope explicitly empty. Write a config with
      an empty ``active_tools`` list so the mock registers NO tools.
      Use this for tests meant to run with zero MCP tools available.
    - ``tools == [names...]`` → register exactly this scope.

    Each entry in a non-empty list is expected to be a string (the name
    of an existing mock tool), or a dict with a ``name`` key.
    """
    if tools is None:
        # Unset / default — clear any previous config so the mock
        # defaults to registering everything.
        if MOCK_TOOL_CONFIG.exists():
            try:
                MOCK_TOOL_CONFIG.unlink()
            except Exception:
                pass
        return

    # Coerce to list of strings (future: also accept dicts for custom tools).
    # For an empty input list, `active` remains [] and we write an explicit
    # empty-scope config — the mock server treats an empty active_tools
    # set as "register nothing" (distinct from the missing-file default).
    active = [t if isinstance(t, str) else t.get("name", "") for t in tools]
    active = [t for t in active if t]
    MOCK_TOOL_CONFIG.write_text(json.dumps({"active_tools": active}))


_EXTERNAL_AGENT_RUNTIMES = {"claude"}
_EXTERNAL_MEMORY_PROVIDERS = {"mem0", "honcho", "hindsight", "supermemory"}
_CLAUDE_NATIVE_MEMORY_PROVIDER = "claude_native_memory"


def _load_exact_memory_identity(path_value: str | None, provider: str) -> dict[str, str]:
    """Load the explicitly supplied, already-seeded retrieval identity."""
    if provider not in _EXTERNAL_MEMORY_PROVIDERS | {_CLAUDE_NATIVE_MEMORY_PROVIDER}:
        raise RuntimeError(
            "unsupported memory provider for an isolated Claude Code test run"
        )
    if not path_value:
        raise RuntimeError("--memory-provider-identity-path is required for Claude Code")
    path = Path(path_value)
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read memory provider identity: {path}: {exc}") from exc
    identity = payload.get("provider_identity", payload) if isinstance(payload, dict) else None
    gateway_config = payload.get("gateway_config", {}) if isinstance(payload, dict) else {}
    if not isinstance(identity, dict):
        raise RuntimeError("memory provider identity must be a JSON object")
    if not isinstance(gateway_config, dict):
        raise RuntimeError("memory gateway configuration must be a JSON object")
    if provider == _CLAUDE_NATIVE_MEMORY_PROVIDER:
        if identity.get("kind") != "claude_native_auto_memory":
            raise RuntimeError("Claude native-memory identity must describe a frozen Claude auto-memory snapshot")
        config_dir = gateway_config.get("claude_native_memory_seed_dir")
        if not isinstance(config_dir, str) or not Path(config_dir).is_dir():
            raise RuntimeError("Claude native-memory seed directory is missing or unreadable")
        return {"seed_config_dir": str(Path(config_dir).resolve())}
    if provider == "mem0":
        user_id = identity.get("user_id") or identity.get("userId")
        if not isinstance(user_id, str) or not user_id:
            raise RuntimeError("Mem0 identity must contain a non-empty user_id")
        return {"user_id": user_id}
    if provider == "honcho":
        workspace = identity.get("workspace") or identity.get("workspace_id")
        peer = identity.get("peer_name") or identity.get("peer")
        if not isinstance(workspace, str) or not workspace or not isinstance(peer, str) or not peer:
            raise RuntimeError("Honcho identity must contain non-empty workspace and peer_name")
        base_url = gateway_config.get("honcho_base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            raise RuntimeError(
                "Honcho gateway configuration must contain the completed profile's base URL"
            )
        return {"workspace": workspace, "peer_name": peer, "base_url": base_url.strip()}
    if provider == "hindsight":
        bank_id = identity.get("bank_id")
        if not isinstance(bank_id, str) or not bank_id:
            raise RuntimeError("Hindsight identity must contain a non-empty bank_id")
        mode = gateway_config.get("hindsight_mode")
        base_url = gateway_config.get("hindsight_base_url")
        budget = gateway_config.get("hindsight_budget", "mid")
        if mode != "local_external" or not isinstance(base_url, str) or not base_url.strip():
            raise RuntimeError(
                "Hindsight gateway configuration must contain the completed self-hosted service URL"
            )
        return {
            "bank_id": bank_id,
            "mode": mode,
            "base_url": base_url.strip(),
            "budget": str(budget).strip() or "mid",
        }
    container_tag = identity.get("container_tag")
    if not isinstance(container_tag, str) or not container_tag:
        raise RuntimeError("Supermemory identity must contain a non-empty container_tag")
    base_url = gateway_config.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        raise RuntimeError(
            "Supermemory gateway configuration must contain the completed self-hosted service URL"
        )
    return {
        "container_tag": container_tag,
        "base_url": base_url.strip().rstrip("/"),
    }


def _external_mcp_config(
    *,
    persona: _PersonaPaths,
    state_path: Path,
    log_path: Path,
    tool_config_path: Path,
    run_id: str,
    provider: str,
    identity: dict[str, str],
    memory_gateway_python: str,
    mock_mcp_python: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Build the complete per-test MCP configuration for Claude Code.

    The runtime uses this configuration instead of an operator config or a
    Hermes profile.
    """
    manifest = yaml.safe_load(persona.mock_manifest.read_text()) or {}
    app_tools = [str(tool) for tool in manifest.get("tools") or []]
    if not app_tools or len(app_tools) != len(set(app_tools)):
        raise RuntimeError(f"{persona.id}: mock tool manifest has no exact tool list")
    app_env = {
        "DOLPHINBENCH_PERSONA": persona.id,
        "DOLPHINBENCH_MOCK_MANIFEST": str(persona.mock_manifest),
        "DOLPHINBENCH_STATE_PATH": str(state_path),
        "DOLPHINBENCH_LOG_PATH": str(log_path),
        "DOLPHINBENCH_TOOL_CONFIG_PATH": str(tool_config_path),
        "DOLPHINBENCH_RUN_ID": run_id,
    }
    mcp_servers = {
        "dolphinbench_apps": {
            "command": mock_mcp_python or sys.executable,
            "args": [str(DOLPHINBENCH_ROOT / "mock_mcp" / "server.py")],
            "env": app_env,
        },
    }
    allowed_tools = [
        *(f"mcp__dolphinbench_apps__{tool}" for tool in app_tools),
    ]
    if provider != _CLAUDE_NATIVE_MEMORY_PROVIDER:
        raise RuntimeError(
            "This retained runner does not execute Claude external-memory evaluations."
        )
    claude_config = {"mcpServers": mcp_servers}
    return claude_config, allowed_tools


def _external_runtime_environment(runtime: str) -> dict[str, str]:
    """Pass memory credentials plus the selected subscription credential."""
    allowed = {
        key: value
        for key, value in os.environ.items()
        if key.startswith(("MEM0_", "HONCHO_", "HINDSIGHT_", "SUPERMEMORY_"))
    }
    if runtime == "claude":
        token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "Claude subscription runs require CLAUDE_CODE_OAUTH_TOKEN"
            )
        allowed["CLAUDE_CODE_OAUTH_TOKEN"] = token
    # Do not inherit a global Claude Code configuration root.
    allowed.pop("CLAUDE_CONFIG_DIR", None)
    return allowed


def _run_external_agent_runtime(
    args: argparse.Namespace,
    persona: _PersonaPaths,
    sim: dict[str, Any],
    only_ids: set[str] | None,
) -> int:
    """Run Phase 2 only through an isolated Claude Code process."""
    runtime = args.agent_runtime
    if runtime not in _EXTERNAL_AGENT_RUNTIMES:
        raise RuntimeError(f"unsupported agent runtime: {runtime}")
    if not args.skip_seeding or args.seed_only or args.resume:
        raise RuntimeError(
            f"{runtime} runs are test-only: pass --skip-seeding and do not use --seed-only or --resume"
        )
    provider = args.provider
    if provider in _EXTERNAL_MEMORY_PROVIDERS:
        raise RuntimeError(
            "This retained runner does not execute Claude external-memory evaluations."
        )
    if provider == _CLAUDE_NATIVE_MEMORY_PROVIDER and runtime != "claude":
        raise RuntimeError("Claude native auto-memory is available only to Claude Code")
    identity = _load_exact_memory_identity(args.memory_provider_identity_path, provider)
    runtime_root = Path(args.runtime_config_dir or "")
    if not args.runtime_config_dir:
        raise RuntimeError("--runtime-config-dir is required for Claude Code")
    gateway_python = Path(args.memory_gateway_python or "")
    if provider != _CLAUDE_NATIVE_MEMORY_PROVIDER and (
        not args.memory_gateway_python or not gateway_python.is_file()
    ):
        raise RuntimeError(
            "--memory-gateway-python must name the provider environment's Python executable"
        )
    if runtime_root.exists():
        raise RuntimeError(f"runtime config directory already exists: {runtime_root}")
    runtime_root.mkdir(parents=True)

    out_path = Path(args.out)
    if out_path.exists():
        raise RuntimeError(f"results file already exists: {out_path}")
    results: dict[str, Any] = {
        "simulation": sim.get("name"),
        "provider": provider,
        "agent_runtime": runtime,
        "memory_provider_identity": identity,
        "run_mode": "test_only_external_runtime",
        "read_only_test_memory": True,
        "seed_sessions": [],
        "seed_calls": [],
        "test_results": [],
        "started": datetime.now(timezone.utc).isoformat(),
    }
    selected = [
        test for test in sim.get("test_queries", [])
        if not only_ids or test.get("id") in only_ids
    ]
    if not selected:
        raise RuntimeError("no test queries selected")

    background_grader = _BackgroundGrader(results, out_path)

    for index, query_meta in enumerate(selected, start=1):
        test_id = str(query_meta["id"])
        source_id = str(query_meta.get("source", test_id))
        spec = load_test(source_id)
        query = spec.get("test") or query_meta.get("query")
        if not isinstance(query, str) or not query.strip():
            raise RuntimeError(f"{test_id}: test has no query text")
        test_root = runtime_root / f"test_{index:03d}_{source_id}"
        state_path = test_root / "state.json"
        log_path = test_root / "calls.jsonl"
        tool_config_path = test_root / "tool_config.json"
        test_root.mkdir(parents=True)
        native_memory_dir = None
        if provider == _CLAUDE_NATIVE_MEMORY_PROVIDER:
            seed_config_dir = Path(identity["seed_config_dir"])
            test_config_dir = test_root / "claude-config"
            shutil.copytree(seed_config_dir, test_config_dir)
            native_memory_dir = test_config_dir.resolve() / "auto-memory"
            if not native_memory_dir.is_dir():
                raise RuntimeError("Claude native-memory copy has no auto-memory directory")
            native_project_dir = Path("/tmp/dolphinbench-claude-native-memory-project")
            native_project_dir.mkdir(parents=True, exist_ok=True)
            if any(native_project_dir.iterdir()):
                raise RuntimeError("Claude native-memory project directory must remain empty")
        state = spec.get("mock_state")
        if state is not None:
            state_path.write_text(json.dumps(state))
        else:
            shutil.copy(persona.baseline_state, state_path)
        log_path.write_text("")
        tool_scope = spec.get("tools")
        if tool_scope is not None:
            _write_external_mock_tool_config(tool_config_path, tool_scope)
        claude_config, allowed_tools = _external_mcp_config(
            persona=persona,
            state_path=state_path,
            log_path=log_path,
            tool_config_path=tool_config_path,
            run_id=f"{args.runtime_run_id or out_path.stem}-{test_id}",
            provider=provider,
            identity=identity,
            memory_gateway_python=str(gateway_python),
            mock_mcp_python=getattr(args, "mock_mcp_python", None),
        )
        config_path = test_root / "claude_mcp.json"
        config_path.write_text(json.dumps(claude_config, indent=2, sort_keys=True) + "\n")
        narrative_time = _test_narrative_time(spec)
        started = time.monotonic()
        env = _external_runtime_environment(runtime)
        result = run_claude(
            query,
            narrative_time=narrative_time,
            timeout=args.runtime_timeout,
            claude_config_dir=test_root / "claude-config",
            mcp_config_path=config_path,
            allowed_mcp_tools=allowed_tools,
            cwd=str(native_project_dir) if native_memory_dir else test_root,
            model=args.model,
            env=env,
            native_memory_mode="read_only" if native_memory_dir else None,
            native_memory_dir=native_memory_dir,
        )
        latency_seconds = time.monotonic() - started
        raw_calls = load_tool_calls(log_path)
        effective_calls = raw_calls or _fallback_session_calls_for_grading(result.tool_calls)
        entry: dict[str, Any] = {
            "test_id": test_id,
            "source_id": source_id,
            "query": query,
            "response": result.response_text,
            "session_id": result.session_id,
            "mcp_tool_calls": raw_calls,
            "session_tool_calls": result.tool_calls,
            "available_tools": allowed_tools,
            "effective_tool_calls": effective_calls,
            "latency_seconds": latency_seconds,
            "driver_ok": bool(result.ok),
            "token_usage": result.token_usage or {},
            "model": result.model or args.model,
            "runtime_config_path": str(config_path),
        }
        if result.error:
            entry["driver_error"] = result.error
        trace_path = getattr(result, "raw_trace_path", None) or getattr(result, "trace_path", None)
        if trace_path:
            entry["trace_path"] = trace_path
        entry["grading_status"] = "pending"
        results["test_results"].append(entry)
        _atomic_write_json(out_path, results)
        background_grader.submit(entry, spec)
        background_grader.collect(wait=False)

    background_grader.finish()
    results["ended"] = datetime.now(timezone.utc).isoformat()
    passed = sum(bool(entry.get("passed")) for entry in results["test_results"])
    results["summary"] = {
        "seed_complete": True,
        "tests_completed": len(results["test_results"]),
        "tests_passed": passed,
        "test_accuracy": passed / len(results["test_results"]),
        "model_id": next((entry.get("model") for entry in results["test_results"] if entry.get("model")), args.model),
    }
    _atomic_write_json(out_path, results)
    return 0


def _write_external_mock_tool_config(path: Path, tools: list) -> None:
    active = [tool if isinstance(tool, str) else tool.get("name", "") for tool in tools]
    active = [tool for tool in active if isinstance(tool, str) and tool]
    path.write_text(json.dumps({"active_tools": active}))


# Costing uses these defaults only until the driver reports its actual model.
DEFAULT_MODEL_IDS = {
    "builtin": os.environ.get("HERMES_MODEL_ID"),
    "honcho": os.environ.get("HERMES_MODEL_ID"),
    "mem0": os.environ.get("HERMES_MODEL_ID"),
    "dummy": "dummy-v0",
    "openai_direct": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
}


def _run_example_provider(
    provider: str,
    message: str,
    narrative_time: str | None,
    timeout: int,
):
    """Dispatch a message to one of the example providers (dummy, openai_direct).

    Returns whatever the example driver returned (a dataclass conforming
    to the driver contract). Raises if the driver module isn't importable
    — which only happens when someone deleted the examples/ directory.
    """
    if provider == "dummy":
        if run_dummy is None:
            raise RuntimeError(
                "examples/dummy_driver.py not importable — "
                "ensure DolphinBench/examples/ is present."
            )
        return run_dummy(message, narrative_time=narrative_time, timeout=timeout)
    if provider == "openai_direct":
        if run_openai_direct is None:
            raise RuntimeError(
                "examples/openai_direct_driver.py not importable — "
                "ensure DolphinBench/examples/ is present."
            )
        return run_openai_direct(message, narrative_time=narrative_time, timeout=timeout)
    raise ValueError(f"unknown example provider: {provider}")


def _normalize_usage(token_usage: dict) -> dict:
    """Map driver token_usage keys to costing-module expected keys.

    Accept both short and explicit token field names. Cache fields pass
    through unchanged when present.
    """
    if not token_usage:
        return {}
    return {
        "input_tokens": token_usage.get("input_tokens", token_usage.get("input")),
        "output_tokens": token_usage.get("output_tokens", token_usage.get("output")),
        "cache_creation_input_tokens": token_usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": token_usage.get("cache_read_input_tokens"),
    }


def _parse_iso_to_epoch(iso_ts: str) -> float:
    """Parse ISO-8601 (with or without timezone) to a UTC epoch float.

    Used to bucket cost-ledger entries (which carry ``time.time()`` floats)
    against the harness's phase-boundary timestamps.
    """
    return datetime.fromisoformat(
        iso_ts.replace("Z", "+00:00")
    ).timestamp()


def _read_cost_ledger(
    ledger_path: Path,
    phase1_start: str | None,
    phase2_start: str | None,
    run_end: str | None,
    pricing: dict,
) -> dict:
    """Read the structured per-call cost ledger and aggregate by phase + model.

    Entries contain the model, token usage, cache fields, and a ``ts`` epoch.
    Ignore entries outside this run. Missing records or unknown prices are
    reported as unavailable, not as zero spend.
    """
    out = {
        "phase1_cost": 0.0,
        "phase1_calls": 0,
        "phase2_cost": 0.0,
        "phase2_calls": 0,
        "total_cost": 0.0,
        "total_calls": 0,
        "by_model": {},
        "unknown_models": [],
        "cost_unavailable_calls": 0,
        "ledger_path": str(ledger_path),
    }
    if not ledger_path.exists() or not phase1_start:
        return out
    try:
        from harness.costing import UnknownModelError, compute_call_cost
    except Exception:
        return out

    p1_start = _parse_iso_to_epoch(phase1_start)
    p2_start = _parse_iso_to_epoch(phase2_start) if phase2_start else None
    end = _parse_iso_to_epoch(run_end) if run_end else None

    unknown: set[str] = set()
    with ledger_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = float(entry.get("ts") or 0)
            if ts < p1_start or (end is not None and ts > end):
                continue
            model = entry.get("model") or ""
            usage = {
                "input_tokens": int(entry.get("input_tokens") or 0),
                "output_tokens": int(entry.get("output_tokens") or 0),
                "cache_read_input_tokens": int(entry.get("cache_read_input_tokens") or 0),
                "cache_creation_input_tokens": int(entry.get("cache_creation_input_tokens") or 0),
            }
            try:
                cost = compute_call_cost(usage, model, pricing)
            except UnknownModelError:
                cost = None
                unknown.add(model)

            phase = "phase1" if (p2_start is None or ts < p2_start) else "phase2"
            if cost is None:
                out["cost_unavailable_calls"] += 1
            else:
                out[f"{phase}_cost"] += cost
            out[f"{phase}_calls"] += 1
            if cost is not None:
                out["total_cost"] += cost
            out["total_calls"] += 1
            mb = out["by_model"].setdefault(model, {
                "calls": 0, "cost": 0.0,
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            })
            mb["calls"] += 1
            if cost is None:
                mb["cost"] = None
            elif mb["cost"] is not None:
                mb["cost"] += cost
            for k in ("input_tokens", "output_tokens",
                      "cache_read_input_tokens", "cache_creation_input_tokens"):
                mb[k] += usage[k]

    out["unknown_models"] = sorted(unknown)
    if unknown:
        out["phase1_cost"] = None
        out["phase2_cost"] = None
        out["total_cost"] = None
    return out


def _read_honcho_reasoning_trace(
    trace_path: Path,
    started_at: str | None,
    ended_at: str | None,
    pricing: dict[str, Any] | None,
) -> dict[str, Any]:
    """Read local Honcho reasoning calls recorded during this benchmark run."""
    unavailable = {
        "available": False,
        "reason": "trace file or pricing snapshot unavailable",
        "embedding_fees": "unavailable",
        "platform_fees": "not_applicable_for_local_honcho",
    }
    if not trace_path.exists() or not started_at or not ended_at or pricing is None:
        return unavailable
    start = _parse_iso_to_epoch(started_at)
    end = _parse_iso_to_epoch(ended_at)
    calls: list[dict[str, Any]] = []
    unknown_models: set[str] = set()
    total_cost = 0.0
    try:
        lines = trace_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {**unavailable, "reason": f"could not read trace: {exc}"}
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        timestamp = float(entry.get("timestamp") or 0)
        if timestamp < start or timestamp > end:
            continue
        model = str(entry.get("model") or "")
        usage = {
            "input_tokens": int((entry.get("input") or {}).get("tokens") or 0),
            "output_tokens": int((entry.get("output") or {}).get("tokens") or 0),
        }
        call: dict[str, Any] = {
            "task_type": entry.get("task_type"),
            "model": model,
            **usage,
        }
        try:
            call["cost_usd"] = compute_call_cost(usage, model, pricing)
            total_cost += call["cost_usd"]
        except UnknownModelError:
            call["cost_usd"] = None
            call["cost_unavailable_reason"] = f"model_id_not_in_pricing_snapshot: {model}"
            unknown_models.add(model)
        calls.append(call)
    return {
        "available": True,
        "trace_path": str(trace_path),
        "calls": calls,
        "call_count": len(calls),
        "input_tokens": sum(call["input_tokens"] for call in calls),
        "output_tokens": sum(call["output_tokens"] for call in calls),
        "cost_usd": None if unknown_models else total_cost,
        "unknown_models": sorted(unknown_models),
        "embedding_fees": "unavailable",
        "platform_fees": "not_applicable_for_local_honcho",
    }


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (no numpy)."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = pct / 100.0 * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def grade(spec: dict, response_text: str, tool_calls: list[dict],
          test_message: str) -> dict:
    grade_cfg = spec.get("grade", {})
    gtype = grade_cfg.get("type")
    cfg = grade_cfg.get("config", {})
    if gtype == "regex":
        return grade_regex(response_text, cfg)
    if gtype == "tool_trace":
        return grade_tool_trace(tool_calls, cfg, test_message=test_message)
    if gtype == "hybrid":
        return grade_hybrid(response_text, tool_calls, cfg,
                            test_message=test_message)
    if gtype == "llm_judge":
        return grade_llm_judge(response_text, test_message, cfg, tool_calls=tool_calls)
    return {"grader_type": "unknown", "passed": False, "score": 0.0,
            "details": [], "error": f"unknown: {gtype}"}


class _BackgroundGrader:
    """Grade saved agent outputs without blocking the next agent call."""

    def __init__(self, results: dict[str, Any], out_path: Path):
        workers = int(os.environ.get("DOLPHINBENCH_GRADING_WORKERS", "2"))
        if workers < 1:
            raise RuntimeError("DOLPHINBENCH_GRADING_WORKERS must be at least 1")
        self._results = results
        self._out_path = out_path
        self._executor = ThreadPoolExecutor(max_workers=workers)
        self._pending: dict[Future, dict[str, Any]] = {}

    def submit(self, entry: dict[str, Any], spec: dict[str, Any]) -> None:
        if entry.get("grade") is not None:
            return
        entry["grading_status"] = "pending"
        entry.pop("grading_error", None)
        future = self._executor.submit(
            grade,
            spec,
            str(entry.get("response") or ""),
            list(entry.get("effective_tool_calls") or []),
            str(entry.get("query") or ""),
        )
        self._pending[future] = entry

    def submit_saved_outputs(self) -> None:
        """Resume grading outputs saved before a prior interruption."""
        for entry in self._results.get("test_results", []):
            if entry.get("grade") is not None:
                continue
            source_id = str(entry.get("source_id") or entry.get("test_id"))
            self.submit(entry, load_test(source_id))

    def collect(self, *, wait: bool) -> list[dict[str, Any]]:
        futures = (
            list(as_completed(list(self._pending)))
            if wait
            else [future for future in list(self._pending) if future.done()]
        )
        completed: list[dict[str, Any]] = []
        for future in futures:
            entry = self._pending.pop(future)
            try:
                grade_result = future.result()
            except Exception as exc:
                entry["grading_status"] = "failed"
                entry["grading_error"] = str(exc)
                _atomic_write_json(self._out_path, self._results)
                self._executor.shutdown(wait=False, cancel_futures=True)
                raise
            entry["grade"] = grade_result
            entry["passed"] = bool(grade_result.get("passed"))
            entry.pop("grading_status", None)
            entry.pop("grading_error", None)
            completed.append(entry)
            _atomic_write_json(self._out_path, self._results)
        return completed

    def finish(self) -> list[dict[str, Any]]:
        completed = self.collect(wait=True)
        self._executor.shutdown(wait=True)
        return completed


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="DolphinBench Life Simulation runner")
    ap.add_argument("--persona", default=get_setting("DOLPHINBENCH_PERSONA"),
                    help="Persona id (e.g. 'morgan'). Required: pass --persona "
                         "or set DOLPHINBENCH_PERSONA. Resolves life_sim, tests dir, "
                         "and mock manifest under registry/personas/<id>/.")
    ap.add_argument("--sim", default=None,
                    help="Path to life_sim.yaml. Defaults to the persona's "
                         "registry/personas/<id>/life_sim.yaml.")
    ap.add_argument("--provider", required=True,
                    choices=[*PROVIDER_TO_PROFILE.keys(), _CLAUDE_NATIVE_MEMORY_PROVIDER])
    ap.add_argument(
        "--agent-runtime", choices=("hermes", "claude"), default="hermes",
        help="Agent runtime. Claude Code uses test-only execution with a completed ingestion.",
    )
    ap.add_argument(
        "--runtime-config-dir",
        help="Fresh per-job directory for Claude Code runtime configuration and test state.",
    )
    ap.add_argument(
        "--memory-provider-identity-path",
        help="JSON file with the exact already-seeded Mem0 or Honcho identity for Claude Code.",
    )
    ap.add_argument(
        "--memory-gateway-python",
        help="Python executable containing the selected memory provider SDK and MCP server SDK.",
    )
    ap.add_argument(
        "--mock-mcp-python",
        help="Python executable containing the simulated app MCP server dependencies.",
    )
    ap.add_argument(
        "--runtime-run-id",
        help="Run label recorded in mock app calls for Claude Code.",
    )
    ap.add_argument(
        "--runtime-timeout", type=int, default=600,
        help="Per-test Claude Code subprocess timeout in seconds.",
    )
    ap.add_argument("--out", required=True, help="Output results JSON")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--skip-seeding", action="store_true",
                    help="Skip the seeding phase (reuse existing memory state)")
    ap.add_argument(
        "--seed-only",
        action="store_true",
        help="Complete or resume seeding, save the seeded profile, and exit before tests",
    )
    ap.add_argument(
        "--max-new-seed-items",
        type=int,
        default=None,
        help="For a seed-only run, stop after this many newly completed source messages. "
             "Use --resume to continue from the recorded receipts.",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="Resume an unfinished result file. Completed seed messages and tests "
             "are kept; an uncertain seed delivery stops the run for inspection.",
    )
    ap.add_argument(
        "--read-only-test-memory",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Disable external memory-provider writes during Phase 2. Native memory "
             "writes are confined to the disposable profile created for each test "
             "(required for Hermes DolphinBench runs; enabled by default).",
    )
    ap.add_argument(
        "--isolate-hermes-test-sessions",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Copy the seeded Hermes profile for every Phase 2 test so native "
             "session_search sees seed history but never prior tests (required "
             "for Hermes DolphinBench runs; enabled by default).",
    )
    ap.add_argument("--only", default="",
                    help="Comma-separated test_ids to run (e.g. sim_P2,sim_T5). "
                         "If omitted, all test_queries run.")
    ap.add_argument("--capture-memory", dest="capture_memory",
                    action="store_true", default=True,
                    help="Capture memory snapshots/deltas (default on).")
    ap.add_argument("--no-capture-memory", dest="capture_memory",
                    action="store_false",
                    help="Disable memory snapshot capture.")
    ap.add_argument("--model", dest="model", default=None,
                    help="Override the model passed to hermes via -m (hermes "
                         "providers only). Also seeds the recorded model_id "
                         "used for cost accounting before the first call. "
                         "When unset, the harness uses the model the driver "
                         "reports from its saved session, so the results file "
                         "always reflects whatever the agent actually ran on.")
    return ap


def _validate_seed_corpus(sim: dict) -> None:
    """Pre-flight check: every seed message must be a string.

    A free-form bare-string YAML scalar with embedded ``"`` and a
    ``: `` colon-space sequence parses as a 1-key mapping (dict)
    instead of a string — and the bug only surfaces 19 minutes into
    Phase-1 when the harness calls ``msg.strip()``. Catching it here
    instead saves the run.

    Walks every ``sim['sessions'][].messages`` entry. Aggregates all
    bad rows so a single run lists them all rather than one-at-a-time.
    Raises SystemExit(1) on any failure with a precise location.
    """
    bad: list[tuple[str, int, str, str]] = []
    for s in sim.get("sessions") or []:
        sid = s.get("id", "?") if isinstance(s, dict) else "?"
        msgs = s.get("messages", []) if isinstance(s, dict) else []
        if not isinstance(msgs, list):
            bad.append((sid, -1, type(msgs).__name__, repr(msgs)[:80]))
            continue
        for i, m in enumerate(msgs):
            if not isinstance(m, str):
                bad.append((sid, i, type(m).__name__, repr(m)[:120]))
    if bad:
        lines = [
            f"Seed corpus validation failed: {len(bad)} message(s) are not strings.",
            "",
            "Common cause: a bare-string YAML scalar containing embedded "
            "double quotes AND a ':<space>' sequence — PyYAML interprets "
            "that as a mapping, not a string. Wrap the message in explicit "
            'double quotes (escape internal " as \\") or use a literal '
            "block scalar (|- ...).",
            "",
            "Bad rows:",
        ]
        for sid, idx, tname, sample in bad:
            where = f"messages (whole field is {tname})" if idx < 0 else f"messages[{idx}] (got {tname})"
            lines.append(f"  · seed {sid}: {where}")
            lines.append(f"      sample: {sample}")
        raise SystemExit("\n".join(lines))


def run_simulation(args: argparse.Namespace) -> int:
    runtime = getattr(args, "agent_runtime", "hermes")
    if runtime != "hermes" and runtime not in _EXTERNAL_AGENT_RUNTIMES:
        raise ValueError(f"unsupported agent runtime: {runtime}")
    only_ids = {x.strip() for x in args.only.split(",") if x.strip()} if args.only else None

    # Resolve persona FIRST so every downstream path (life_sim, tests,
    # mock manifest, baseline state) lookups through one source of truth.
    persona = resolve_persona_paths(getattr(args, "persona", None))
    # Stamp the persona id into env so child subprocesses (mock MCP
    # server and Hermes driver) see it without us having to thread it
    # through every IPC boundary.
    os.environ["DOLPHINBENCH_PERSONA"] = persona.id
    # Tell the mock MCP server where to find its manifest. The server
    # reads DOLPHINBENCH_MOCK_MANIFEST on boot and registers only the tools
    # listed there for this persona.
    os.environ["DOLPHINBENCH_MOCK_MANIFEST"] = str(persona.mock_manifest)
    print(f"[persona] id={persona.id} life_sim={persona.life_sim}", flush=True)

    sim_path = Path(args.sim) if args.sim else persona.life_sim
    sim = yaml.safe_load(sim_path.read_text())
    _validate_seed_corpus(sim)
    if runtime in _EXTERNAL_AGENT_RUNTIMES:
        return _run_external_agent_runtime(args, persona, sim, only_ids)
    profile = os.environ.get("DOLPHINBENCH_HERMES_PROFILE") or PROVIDER_TO_PROFILE[args.provider]
    # GUARD: the hermes profile's mock MCP server must run as THE SAME persona
    # as this benchmark run -- a stale --persona in the profile silently swaps
    # the whole tool surface (tools missing -> rigged failures). Fail loudly.
    if profile:
        _pcfg = HERMES_PROFILES_DIR / profile / "config.yaml"
        if _pcfg.exists():
            import yaml as _yaml
            _profile_cfg = _yaml.safe_load(_pcfg.read_text()) or {}
            _mock = (_profile_cfg.get("mcp_servers") or {}).get("dolphinbench-apps")
            if not _mock:
                raise SystemExit(
                    f"FATAL: profile '{profile}' has no dolphinbench-apps MCP server. "
                    "This run would not expose DolphinBench's simulated tools."
                )
            _margs = _mock.get("args") or []
            _menv = _mock.get("env") or {}
            _required_mcp_env = [
                "DOLPHINBENCH_PERSONA",
                "DOLPHINBENCH_STATE_PATH",
                "DOLPHINBENCH_LOG_PATH",
                "DOLPHINBENCH_TOOL_CONFIG_PATH",
            ]
            _missing_env = [k for k in _required_mcp_env if not get_setting(k, environ=_menv)]
            if _missing_env:
                raise SystemExit(
                    f"FATAL: profile '{profile}' dolphinbench-apps server is missing "
                    f"explicit env keys {_missing_env}. Create an isolated per-run "
                    "profile before running."
                )
            _mpersona = get_setting("DOLPHINBENCH_PERSONA", environ=_menv) or (
                _margs[_margs.index("--persona") + 1] if "--persona" in _margs else None)
            if _mpersona and _mpersona != args.persona:
                raise SystemExit(
                    f"FATAL: profile '{profile}' mock server is configured for persona "
                    f"'{_mpersona}' but this run is --persona {args.persona}. Fix the "
                    f"profile's mcp_servers config before running.")
            _expected_env = {
                "DOLPHINBENCH_STATE_PATH": str(MOCK_STATE_PATH),
                "DOLPHINBENCH_LOG_PATH": str(MOCK_LOG_PATH),
                "DOLPHINBENCH_TOOL_CONFIG_PATH": str(MOCK_TOOL_CONFIG),
            }
            _mismatched_env = {
                k: {"profile": get_setting(k, environ=_menv), "runner": v}
                for k, v in _expected_env.items()
                if str(get_setting(k, environ=_menv)) != v
            }
            if _mismatched_env:
                raise SystemExit(
                    f"FATAL: profile '{profile}' mock server paths do not match "
                    f"this runner process: {_mismatched_env}. Use one isolated "
                    "profile and matching env per benchmark run."
                )
        _guard_hermes_profile_tools(profile, args.provider)
    provider = args.provider
    seed_only = bool(getattr(args, "seed_only", False))
    if seed_only and args.skip_seeding:
        raise RuntimeError("--seed-only and --skip-seeding cannot be used together")
    max_new_seed_items = getattr(args, "max_new_seed_items", None)
    if max_new_seed_items is not None:
        if not seed_only:
            raise RuntimeError("--max-new-seed-items requires --seed-only")
        if provider not in _SEED_RECEIPT_ENV:
            raise RuntimeError(
                "--max-new-seed-items requires a provider with per-message receipts"
            )
        if max_new_seed_items < 1:
            raise RuntimeError("--max-new-seed-items must be at least 1")

    _require_hermes_test_safety(
        provider,
        args.read_only_test_memory,
        args.isolate_hermes_test_sessions,
    )

    # Bind out_path early so Phase 1 progressive writes can flush partial
    # seed_sessions to disk as they happen. Originally bound below the
    # seed loop, which meant a Phase-1 crash left zero recoverable data.
    out_path = Path(args.out)
    trace_root = out_path.parent / "traces"
    resuming = bool(getattr(args, "resume", False))
    if out_path.exists() and not resuming:
        raise RuntimeError(
            f"results file already exists: {out_path}. Use --resume only for an "
            "unfinished run, or choose a new output path."
        )
    prior_results = (
        _load_partial_results(out_path, sim.get("name"), args.provider)
        if resuming and out_path.exists() else None
    )
    if resuming and prior_results is None and not _can_resume_without_results(
        out_path, args.provider,
    ):
        raise RuntimeError(
            "cannot resume without a results file or provider seed receipt file: "
            f"{out_path}"
        )


    # Load pricing snapshot for cost instrumentation. Failure here is
    # non-fatal: tests still run; cost_usd will be null with reason logged.
    pricing_snapshot: dict | None = None
    pricing_ver: str | None = None
    pricing_load_error: str | None = None
    try:
        pricing_snapshot = load_pricing()
        pricing_ver = pricing_version()
        print(f"Loaded pricing snapshot: {pricing_ver}", flush=True)
    except Exception as exc:
        pricing_load_error = str(exc)
        print(f"  ! Pricing snapshot unavailable: {exc}", flush=True)

    # Pre-run best-guess model id: CLI flag > per-provider env var override >
    # pending (None — will be filled in by the first driver response). When
    # the harness pre-guess is None we defer to whatever the driver reports
    # and write that into results["model_id"] after the first call so the
    # file always reflects the model that actually answered.
    model_id = args.model or DEFAULT_MODEL_IDS.get(provider)
    hermes_model_override = args.model if provider in HERMES_PROVIDERS else None

    results: dict[str, Any] = prior_results or {
        "simulation": sim.get("name"),
        "provider": provider,
        "profile": profile,
        "model_id": model_id,
        "pricing_version": pricing_ver,
        "started": datetime.now(timezone.utc).isoformat(),
        "seed_sessions": [],
        "seed_calls": [],
        "test_results": [],
    }
    if resuming:
        if results.get("profile") != profile:
            raise RuntimeError(
                f"cannot resume: results profile is {results.get('profile')!r}, "
                f"not {profile!r}"
            )
        results.setdefault("resume_count", 0)
        results["resume_count"] += 1

    expected_seed_items = _expected_seed_items(sim)
    receipt_path = _seed_receipt_path(out_path, provider)
    receipt_completed_seed_keys: set[tuple[str, int]] = set()
    if receipt_path is not None:
        os.environ[_SEED_RECEIPT_ENV[provider]] = str(receipt_path)
        os.environ["HERMES_MEMORY_SYNC_DRAIN_TIMEOUT_SECONDS"] = "6000"
        receipt_file_exists = _prepare_seed_receipt_file(
            receipt_path, resuming=resuming,
        )
        if resuming and prior_results is None and not receipt_file_exists:
            raise RuntimeError(
                "cannot resume provider seeding without either a results file or "
                f"a seed receipt file: {receipt_path}"
            )
        receipt_status = _validate_seed_receipts(
            receipt_path, provider, expected_seed_items, require_all=False,
        )
        receipt_completed_seed_keys = receipt_status["completed_seed_keys"]
        _receipt_resume_seed_calls(
            results, expected_seed_items, receipt_completed_seed_keys,
        )
        results["seed_receipt_path"] = str(receipt_path)

    # ── Phase 1: Seeding ──
    # Keep this run's cost ledger separate from any previous run.
    ledger_path = Path(os.environ.get("DOLPHINBENCH_COST_LEDGER_PATH",
                                       "/tmp/dolphinbench_cost_ledger.jsonl"))
    if not resuming:
        try:
            ledger_path.write_text("")
        except OSError:
            pass
    results["cost_ledger_path"] = str(ledger_path)
    results.setdefault("phase1_started_at", datetime.now(timezone.utc).isoformat())
    mem0_event_log_path = out_path.with_suffix(".mem0_events.jsonl")
    if provider == "mem0" and "DOLPHINBENCH_MEM0_EVENT_LOG" not in os.environ:
        prior_event_log = (
            prior_results.get("mem0_event_log_path") if prior_results else None
        )
        os.environ["DOLPHINBENCH_MEM0_EVENT_LOG"] = str(
            prior_event_log or mem0_event_log_path
        )
    if provider == "mem0" and not resuming:
        try:
            Path(os.environ["DOLPHINBENCH_MEM0_EVENT_LOG"]).write_text("")
        except OSError:
            pass
        results["mem0_event_log_path"] = os.environ.get("DOLPHINBENCH_MEM0_EVENT_LOG")
    if receipt_path is not None:
        completed_seed_keys = set(receipt_completed_seed_keys)
        ambiguous_seed_keys = (
            _receipt_ambiguous_seed_keys(results, completed_seed_keys)
            if resuming else set()
        )
    else:
        completed_seed_keys = _completed_seed_keys(results)
        ambiguous_seed_keys = _ambiguous_seed_keys(results) if resuming else set()
    if ambiguous_seed_keys:
        sample = sorted(ambiguous_seed_keys)[:5]
        raise RuntimeError(
            "cannot resume because the previous process may have delivered seed "
            f"message(s) without recording success: {sample}. Do not replay them."
        )
    if not args.skip_seeding:
        new_seed_items = 0
        reached_seed_limit = False
        if resuming:
            # Save recovered receipt evidence once, before starting another turn.
            _atomic_write_json(out_path, results)
        print(f"\n{'='*60}", flush=True)
        print(f"PHASE 1 — Seeding ({len(sim.get('sessions', []))} sessions)",
              flush=True)
        print(f"{'='*60}\n", flush=True)

        # A resumed run must keep the already seeded provider state intact.
        if not resuming:
            if provider in HERMES_PROVIDERS:
                reset_mock_mcp(persona.baseline_state)

        for sess_spec in sim.get("sessions", []):
            session_progress_changed = False
            sess_id = sess_spec.get("id", "?")
            label = sess_spec.get("label", sess_id)
            messages = sess_spec.get("messages", [])
            narrative_date = sess_spec.get("narrative_date")

            tag = f" @ {narrative_date}" if narrative_date else ""
            print(f"[{label}]{tag} {len(messages)} message(s)", flush=True)


            for i, msg in enumerate(messages):
                seed_key = _seed_key(str(sess_id), i)
                seed_item_id = _seed_item_id(str(sess_id), i)
                if seed_key in completed_seed_keys:
                    continue
                if args.verbose:
                    print(f"  msg {i+1}/{len(messages)}: "
                          f"{msg.strip()[:60]}...", flush=True)

                def _do_seed_call() -> tuple[Any, float]:
                    # Provider plugins inherit this stable logical message name
                    # and write it only after automatic sync succeeds.
                    os.environ["DOLPHINBENCH_SEED_ITEM_ID"] = seed_item_id
                    started_at = time.monotonic()
                    if provider in EXAMPLE_PROVIDERS:
                        # Seeding a no-memory baseline is a no-op semantically
                        # (dummy/openai_direct don't persist anything), but we
                        # still invoke the driver so call-count / cost numbers
                        # are comparable across providers.
                        r = _run_example_provider(
                            provider, msg.strip(),
                            narrative_time=narrative_date,
                            timeout=SEED_PER_ATTEMPT_TIMEOUT_SEC,
                        )
                        return r, time.monotonic() - started_at
                    r = run_hermes(profile, msg.strip(),
                                   timeout=HERMES_SUBPROCESS_TIMEOUT_SEC,
                                   model=hermes_model_override,
                                   narrative_time=narrative_date)
                    if r.session_id or getattr(r, "available_tools", None):
                        _validate_hermes_runtime_tools(
                            r, "seed", str(sess_id), args.provider)
                    return r, time.monotonic() - started_at

                def _matching_seed_receipt() -> bool:
                    if receipt_path is None:
                        return False
                    current = _validate_seed_receipts(
                        receipt_path, provider, expected_seed_items, require_all=False,
                    )
                    return seed_key in current["completed_seed_keys"]

                first_result, first_latency = _do_seed_call()
                attempt_results = [
                    (first_result, first_latency, _matching_seed_receipt())
                ]
                r, _, matching_seed_receipt = attempt_results[-1]
                ok, error = r.ok, r.error
                delivery_state = _seed_delivery_state(
                    provider, r, matching_seed_receipt=matching_seed_receipt,
                )
                retry_count = int(os.environ.get("DOLPHINBENCH_SEED_RETRIES", "3"))
                for attempt_idx in range(1, retry_count + 1):
                    details = _failure_details(r)
                    if (
                        delivery_state != "known_not_delivered"
                        or not details.transient
                    ):
                        break
                    backoff = _retry_delay(details, attempt_idx)
                    print(f"    seed retry {attempt_idx}/{retry_count} after "
                          f"{backoff:.1f}s (last error: {error})", flush=True)
                    time.sleep(backoff)
                    retry_result, retry_latency = _do_seed_call()
                    attempt_results.append(
                        (retry_result, retry_latency, _matching_seed_receipt())
                    )
                    r, _, matching_seed_receipt = attempt_results[-1]
                    ok, error = r.ok, r.error
                    delivery_state = _seed_delivery_state(
                        provider, r, matching_seed_receipt=matching_seed_receipt,
                    )

                for attempt_number, (attempt_result, attempt_latency, attempt_receipt) in enumerate(
                    attempt_results, start=1,
                ):
                    usage = getattr(attempt_result, "token_usage", {}) or {}
                    normalized_usage = _normalize_usage(usage)
                    attempt_model = (
                        getattr(attempt_result, "model", None)
                        or (usage.get("model") if isinstance(usage, dict) else None)
                        or model_id
                    )
                    attempt_cost: float | None = None
                    cost_reason: str | None = None
                    if pricing_snapshot is None:
                        cost_reason = (
                            f"pricing_snapshot_unavailable: {pricing_load_error}"
                        )
                    elif not usage:
                        cost_reason = "driver_did_not_expose_token_usage"
                    elif normalized_usage.get("input_tokens") in (None, 0) and \
                            normalized_usage.get("output_tokens") in (None, 0):
                        cost_reason = "token_usage_block_empty"
                    else:
                        try:
                            attempt_cost = compute_call_cost(
                                normalized_usage, attempt_model, pricing_snapshot,
                            )
                        except UnknownModelError:
                            cost_reason = (
                                f"model_id_not_in_pricing_snapshot: {attempt_model}"
                            )
                        except Exception as exc:
                            cost_reason = f"cost_computation_error: {exc}"

                    call_entry = {
                        "session_id": sess_id,
                        "message_index": i,
                        "seed_item_id": seed_item_id,
                        "attempt": attempt_number,
                        "driver_ok": bool(getattr(attempt_result, "ok", False)),
                        "driver_error": getattr(attempt_result, "error", None),
                        "seed_message_delivered": _seed_message_was_delivered(
                            provider, attempt_result,
                            matching_seed_receipt=attempt_receipt,
                        ),
                        "seed_delivery_state": _seed_delivery_state(
                            provider, attempt_result,
                            matching_seed_receipt=attempt_receipt,
                        ),
                        "matching_seed_receipt": attempt_receipt if receipt_path else None,
                        "transport_failure": _failure_details(attempt_result).__dict__,
                        "hermes_session_id": getattr(attempt_result, "session_id", None),
                        "model": attempt_model,
                        "token_usage": usage,
                        "cost_usd": attempt_cost,
                        "latency_seconds": attempt_latency,
                    }
                    if provider in HERMES_PROVIDERS:
                        trace_path = _archive_hermes_session(
                            getattr(attempt_result, "session_path", None),
                            getattr(attempt_result, "session_id", None),
                            trace_root / "seed",
                            f"{len(results['seed_calls']) + 1:04d}_"
                            f"{sess_id}_m{i + 1}_a{attempt_number}.json",
                        )
                        if trace_path:
                            call_entry["trace_path"] = trace_path
                    if cost_reason:
                        call_entry["cost_unavailable_reason"] = cost_reason
                    if call_entry["seed_delivery_state"] != "delivered":
                        call_entry["driver_stdout"] = getattr(attempt_result, "stdout", "")
                        call_entry["driver_stderr"] = getattr(attempt_result, "stderr", "")
                    results["seed_calls"].append(call_entry)

                if delivery_state != "delivered":
                    _atomic_write_json(out_path, results)
                    if delivery_state == "ambiguous":
                        raise RuntimeError(
                            f"seed delivery is ambiguous for {sess_id} message {i + 1}; "
                            "the message was not retried to avoid duplicate ingestion"
                        )
                    raise RuntimeError(
                        f"seed failed for {sess_id} message {i + 1}: {error}"
                    )
                completed_seed_keys.add(seed_key)
                new_seed_items += 1
                session_progress_changed = True
                time.sleep(0.5)
                if max_new_seed_items is not None and new_seed_items >= max_new_seed_items:
                    reached_seed_limit = True
                    break

            session_complete = all(
                _seed_key(str(sess_id), message_index) in completed_seed_keys
                for message_index in range(len(messages))
            )
            if session_complete and not any(
                entry.get("id") == sess_id for entry in results["seed_sessions"]
            ):
                results["seed_sessions"].append({
                    "id": sess_id,
                    "label": label,
                    "narrative_date": narrative_date,
                    "message_count": len(messages),
                })
                session_progress_changed = True
            # Revisited completed sessions have no new progress to save.
            if session_progress_changed:
                _atomic_write_json(out_path, results)
            if reached_seed_limit:
                break
    else:
        print("Skipping seeding phase (--skip-seeding)", flush=True)

    if receipt_path is not None and not args.skip_seeding:
        seed_complete = len(completed_seed_keys) == len(expected_seed_items)
        final_receipts = _validate_seed_receipts(
            receipt_path,
            provider,
            expected_seed_items,
            require_all=seed_complete,
        )
        results["seed_receipts"] = {
            "path": final_receipts["path"],
            "provider": final_receipts["provider"],
            "automatic_receipt_count": final_receipts["automatic_receipt_count"],
            "expected_seed_item_count": final_receipts["expected_seed_item_count"],
        }
        completed_seed_keys = set(final_receipts["completed_seed_keys"])
        _atomic_write_json(out_path, results)
        print(
            f"Verified {provider} automatic seed receipts: "
            f"{len(completed_seed_keys)}/{len(expected_seed_items)}",
            flush=True,
        )
    else:
        seed_complete = True

    if provider == "mem0" and not args.skip_seeding:
        event_log = Path(os.environ.get("DOLPHINBENCH_MEM0_EVENT_LOG", ""))
        print(f"Waiting for Mem0 seed events from {event_log}", flush=True)
        barrier_started = datetime.now(timezone.utc).isoformat()
        barrier = _wait_for_mem0_event_log(
            event_log,
            expected_user_id=os.environ.get("MEM0_USER_ID"),
        )
        barrier["started_at"] = barrier_started
        barrier["ended_at"] = datetime.now(timezone.utc).isoformat()
        results["mem0_event_barrier"] = barrier
        _atomic_write_json(out_path, results)
        print(
            "Mem0 seed event barrier complete: "
            f"{barrier.get('completed', 0)}/{barrier.get('event_count', 0)}",
            flush=True,
        )

    if provider == "honcho" and not args.skip_seeding:
        _require_healthy_honcho(profile, "seeding")
        print("Waiting for Honcho to finish processing the seed corpus", flush=True)
        results["honcho_seed_queue"] = _wait_for_honcho(
            expected_seed_messages=len(expected_seed_items),
        )
        _atomic_write_json(out_path, results)

    # ── Memory capture: end-of-seeding snapshot ──
    # (out_path was bound earlier to enable Phase-1 progressive writes)

    # Per-test snapshot directory. Derived from the output file stem so
    # concurrent runs writing to different result files don't collide.
    # Only created lazily on first write so a --no-capture-memory run
    # leaves no trace here.
    run_id = out_path.stem
    per_test_snap_dir = _snapshot_dir_for_run(out_path, run_id)
    results["memory_snapshots_dir"] = (
        str(per_test_snap_dir) if args.capture_memory else None
    )

    if args.capture_memory:
        seed_snap = _safe_snapshot(provider, profile)
        try:
            seed_dump_path = _memory_dump_path(out_path, "seed_end_memory")
            seed_dump_path.parent.mkdir(parents=True, exist_ok=True)
            seed_dump_path.write_text(
                json.dumps(seed_snap, indent=2, default=str))
            print(f"  ↳ seed-end memory snapshot → {seed_dump_path}",
                  flush=True)
        except Exception as exc:
            print(f"  ! seed-end snapshot write failed: {exc}", flush=True)

    hermes_seed_profile: str | None = None
    hermes_isolation_base = _hermes_isolation_base(profile, out_path)
    if provider in HERMES_PROVIDERS and args.isolate_hermes_test_sessions and not seed_only:
        if not profile:
            raise RuntimeError("Hermes test isolation requires a profile name")
        hermes_seed_profile = f"{hermes_isolation_base}-seed"
        _copy_hermes_profile(profile, hermes_seed_profile)
        results["hermes_test_session_isolation"] = {
            "enabled": True,
            "seed_profile": hermes_seed_profile,
            "keep_test_profiles": os.environ.get(
                "DOLPHINBENCH_KEEP_HERMES_TEST_PROFILES", "0"
            ).lower() in {"1", "true", "yes", "on"},
        }
        _atomic_write_json(out_path, results)
        print(
            "Hermes Phase 2 session isolation enabled "
            f"(seed profile: {hermes_seed_profile})",
            flush=True,
        )
    else:
        results["hermes_test_session_isolation"] = {"enabled": False}

    if seed_only:
        ended_at = datetime.now(timezone.utc).isoformat()
        results["last_seed_group_ended_at"] = ended_at
        if seed_complete:
            results["phase1_ended_at"] = ended_at
            results["ended"] = ended_at
        results["run_mode"] = "seed_only"
        seed_costs = [
            call.get("cost_usd") for call in results.get("seed_calls", [])
        ]
        costs_complete = all(cost is not None for cost in seed_costs)
        results["summary"] = {
            "seed_complete": seed_complete,
            "seed_items_completed": len(completed_seed_keys),
            "seed_items_expected": len(expected_seed_items),
            "seed_sessions": len(results.get("seed_sessions", [])),
            "seed_calls": len(results.get("seed_calls", [])),
            "total_cost_usd_seed_calls": (
                sum(float(cost) for cost in seed_costs) if costs_complete else None
            ),
            "seed_cost_unavailable_count": sum(
                cost is None for cost in seed_costs
            ),
            "pricing_version": pricing_ver,
            "model_id": model_id,
        }
        try:
            results["cost_breakdown"] = _read_cost_ledger(
                ledger_path=ledger_path,
                phase1_start=results.get("phase1_started_at"),
                phase2_start=None,
                run_end=ended_at,
                pricing=pricing_snapshot or {},
            )
        except Exception as exc:
            results["cost_breakdown"] = {"error": str(exc)}
        if provider == "honcho":
            trace_path = Path(os.environ.get(
                "DOLPHINBENCH_HONCHO_REASONING_TRACE",
                "/home/ubuntu/services/honcho/runtime/reasoning.jsonl",
            ))
            results["honcho_internal_reasoning"] = _read_honcho_reasoning_trace(
                trace_path,
                results.get("started"),
                ended_at,
                pricing_snapshot,
            )
        _atomic_write_json(out_path, results)
        ingestion_status = "Ingestion complete" if seed_complete else (
            f"Ingestion saved: {len(completed_seed_keys)}/{len(expected_seed_items)} messages"
        )
        print(f"{ingestion_status}; no tests were run. Results: {out_path}", flush=True)
        return 0

    # ── Phase 2: Test queries ──
    results.setdefault("phase1_ended_at", datetime.now(timezone.utc).isoformat())
    results.setdefault("phase2_started_at", results["phase1_ended_at"])
    if args.read_only_test_memory:
        # Phase 1 is the only external memory-ingestion phase. Keep Hermes's
        # `memory` toolset enabled because current Hermes uses it as the master
        # switch for both built-in memory context and external memory providers.
        # Provider-specific read-only modes hide their write tools. Any native
        # write is confined to the disposable profile cloned for this test.
        os.environ["DOLPHINBENCH_MEMORY_READ_ONLY"] = "1"
        os.environ["DOLPHINBENCH_MEM0_READ_ONLY"] = "1"
        os.environ["DOLPHINBENCH_HONCHO_READ_ONLY"] = "1"
        os.environ["DOLPHINBENCH_NATIVE_MEMORY_READ_ONLY"] = "1"
        os.environ["DOLPHINBENCH_SINGLE_TURN"] = "1"
        results["read_only_test_memory"] = True
        print(
            "Phase 2 external memory writes disabled; native memory state isolated "
            "per test (--read-only-test-memory)",
            flush=True,
        )
    else:
        results["read_only_test_memory"] = False
    print(f"\n{'='*60}", flush=True)
    print(f"PHASE 2 — Test queries ({len(sim.get('test_queries', []))} tests)",
          flush=True)
    print(f"{'='*60}\n", flush=True)
    selected_tests = [
        tq for tq in sim.get("test_queries", [])
        if not only_ids or tq.get("id") in only_ids
    ]
    completed_test_ids = _completed_test_ids(results)

    # Reset mock MCP state for test phase (clean tool state, keep memory)
    if provider in HERMES_PROVIDERS:
        reset_mock_mcp(persona.baseline_state)

    # 1-based index over tests actually executed (skipped tests don't bump
    # it). Used to name per-test snapshot files so listings sort naturally.
    test_exec_index = 0

    background_grader = _BackgroundGrader(results, out_path)
    background_grader.submit_saved_outputs()

    def _record_completed_grades(entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            grade_result = entry["grade"]
            status = "PASS" if entry["passed"] else "FAIL"
            print(
                f"  [{entry['test_id']}] → {status} "
                f"({grade_result.get('score', 0.0):.2f})",
                flush=True,
            )

    for tq in sim.get("test_queries", []):
        test_id = tq["id"]
        # Clean DolphinBench sims use only a numeric `id` and resolve
        # tests/<persona>/<id>.yaml. `source` is kept as a legacy override for
        # older generated sims whose public id differed from the filename stem.
        source_id = tq.get("source", test_id)

        if only_ids and test_id not in only_ids:
            continue
        if test_id in completed_test_ids:
            print(f"[{test_id}] SKIP — already completed in saved results", flush=True)
            continue

        test_exec_index += 1

        # Single source of truth: query text lives in tests/<source_id>.yaml
        # under the `test:` field (co-located with the grader that's
        # written against it). life_sim.yaml's test_queries entries only carry
        # ordering metadata. Fall back to an inline `query:` field in life_sim
        # for backwards compat only.
        try:
            query_spec = load_test(source_id)
            query = query_spec.get("test")
            if query is None:
                # Test YAML uses the multi-query form — not supported by
                # this harness path; fall back to life_sim's query.
                query = tq.get("query")
        except FileNotFoundError:
            query = tq.get("query")
        if query is None:
            print(f"[{test_id}] SKIP — no query found in tests/{source_id}*.yaml "
                  f"or life_sim test_queries entry", flush=True)
            continue

        print(f"[{test_id}] (from {source_id})", flush=True)
        try:
            pre_spec = load_test(source_id)
        except FileNotFoundError:
            pre_spec = {"grade": {"type": "llm_judge", "config": {
                "rubric": [{"id": "manual", "criterion": "Manual review needed"}],
            }}}
        test_narrative_time = _test_narrative_time(pre_spec)
        tool_scope = pre_spec.get("tools")

        def _prepare_test_attempt(attempt_number: int) -> tuple[str | None, str | None]:
            """Create a new isolated test profile and clean simulated app state."""
            attempt_profile = profile
            attempt_clone: str | None = None
            if hermes_seed_profile:
                attempt_clone = (
                    f"{hermes_isolation_base}-t{test_exec_index:03d}-a{attempt_number:02d}"
                )
                _copy_hermes_profile(hermes_seed_profile, attempt_clone)
                attempt_profile = attempt_clone
            MOCK_LOG_PATH.write_text("")
            spec_state = pre_spec.get("mock_state")
            if spec_state is not None:
                MOCK_STATE_PATH.write_text(json.dumps(spec_state))
            elif persona.baseline_state.exists():
                shutil.copy(persona.baseline_state, MOCK_STATE_PATH)
            _write_mock_tool_config(tool_scope)
            return attempt_profile, attempt_clone

        def _run_test_call(active_profile: str | None) -> tuple[Any, list[dict], list[dict]]:
            if provider in EXAMPLE_PROVIDERS:
                result = _run_example_provider(
                    provider, query, narrative_time=test_narrative_time, timeout=300,
                )
                calls = load_tool_calls(MOCK_LOG_PATH)
            else:
                result = run_hermes(
                    active_profile, query, timeout=HERMES_SUBPROCESS_TIMEOUT_SEC,
                    model=hermes_model_override, narrative_time=test_narrative_time,
                )
                if result.session_id or getattr(result, "available_tools", None):
                    _validate_hermes_runtime_tools(result, "test", str(test_id), args.provider)
                if provider == "honcho":
                    _require_healthy_honcho(active_profile, f"test {source_id}")
                calls = load_tool_calls(MOCK_LOG_PATH)
            return result, calls, calls or _fallback_session_calls_for_grading(result.tool_calls)

        test_attempts: list[dict[str, Any]] = []
        retry_count = int(os.environ.get("DOLPHINBENCH_TEST_RETRIES", "3"))
        active_profile: str | None = None
        test_isolation_profile: str | None = None
        r: Any = None
        mcp_calls: list[dict] = []
        effective_calls: list[dict] = []
        latency_seconds = 0.0
        pre_snap: dict[str, Any] | None = None
        for attempt_number in range(1, retry_count + 2):
            active_profile, test_isolation_profile = _prepare_test_attempt(attempt_number)
            diagnostic_path = None
            if provider == "honcho" and args.read_only_test_memory:
                diagnostic_path = trace_root / "memory_diagnostics" / f"{source_id}_attempt_{attempt_number}.jsonl"
                diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
                os.environ["DOLPHINBENCH_MEMORY_DIAGNOSTICS_PATH"] = str(diagnostic_path.resolve())
            attempt_pre_snap = (
                _safe_snapshot(provider, active_profile)
                if args.capture_memory else None
            )
            started_at = time.monotonic()
            try:
                r, mcp_calls, effective_calls = _run_test_call(active_profile)
            finally:
                if diagnostic_path is not None:
                    os.environ.pop("DOLPHINBENCH_MEMORY_DIAGNOSTICS_PATH", None)
            attempt_latency = time.monotonic() - started_at
            latency_seconds += attempt_latency
            details = _failure_details(r)
            test_attempts.append({
                "attempt": attempt_number,
                "driver_ok": bool(getattr(r, "ok", False)),
                "driver_error": getattr(r, "error", None),
                "latency_seconds": attempt_latency,
                "transport_failure": details.__dict__,
                "memory_diagnostics_path": str(diagnostic_path.resolve()) if diagnostic_path else None,
            })
            if bool(getattr(r, "ok", False)) or not details.transient:
                pre_snap = attempt_pre_snap
                break
            if test_isolation_profile:
                _remove_hermes_profile(test_isolation_profile)
                test_isolation_profile = None
            if attempt_number > retry_count:
                raise RuntimeError(
                    f"test {test_id} exhausted {retry_count + 1} transport attempts: "
                    f"{getattr(r, 'error', None)}"
                )
            delay = _retry_delay(details, attempt_number)
            print(
                f"  transient transport failure; retrying {attempt_number}/{retry_count} "
                f"after {delay:.1f}s", flush=True,
            )
            time.sleep(delay)

        # Preserve the pre-test snapshot for comparison with this attempt's
        # post-test memory files, without changing the frozen input profile.
        pre_snapshot_path: Path | None = None
        if args.capture_memory and pre_snap is not None:
            pre_snapshot_path = _per_test_snapshot_path(
                per_test_snap_dir, test_exec_index, source_id, "pre",
            )
            try:
                _atomic_write_json(pre_snapshot_path, pre_snap)
            except Exception as exc:
                print(f"  ! pre-test snapshot write failed: {exc}", flush=True)
                pre_snapshot_path = None

        test_entry = {
            "test_id": test_id,
            "source_id": source_id,
            "query": query,
            "response": r.response_text,
            "session_id": r.session_id,
            "mcp_tool_calls": mcp_calls,
            "session_tool_calls": r.tool_calls,
            "available_tools": getattr(r, "available_tools", []),
            "effective_tool_calls": effective_calls,
            "attempts": test_attempts,
            "latency_seconds": latency_seconds,
        }
        if test_isolation_profile:
            test_entry["hermes_test_profile"] = test_isolation_profile
        # Distinguish transport failures from a successfully returned empty answer.
        driver_error = getattr(r, "error", None)
        if driver_error:
            test_entry["driver_error"] = driver_error
        test_entry["driver_ok"] = bool(getattr(r, "ok", True))
        # Add provider-specific fields
        if provider in HERMES_PROVIDERS:
            test_entry["session_path"] = r.session_path
            trace_path = _archive_hermes_session(
                r.session_path,
                r.session_id,
                trace_root / "test",
                f"{test_exec_index:03d}_{source_id}.json",
            )
            if trace_path:
                test_entry["trace_path"] = trace_path
        token_usage_raw = getattr(r, "token_usage", {}) or {}
        if token_usage_raw:
            test_entry["token_usage"] = token_usage_raw

        # Post-test memory snapshot + delta vs pre-test snapshot.
        if args.capture_memory and pre_snap is not None:
            post_snap = _safe_snapshot(provider, active_profile)
            try:
                delta = diff_memory_dumps(pre_snap, post_snap, provider)
            except Exception as exc:
                delta = {"error": f"diff_failed: {exc}"}
            test_entry["memory_delta"] = delta

            # Persist the post-test snapshot alongside the pre-test one.
            # Path is recorded on the test entry so the debug viewer can
            # load it without re-deriving the naming convention.
            post_snapshot_path = _per_test_snapshot_path(
                per_test_snap_dir, test_exec_index, source_id, "post",
            )
            try:
                _atomic_write_json(post_snapshot_path, post_snap)
            except Exception as exc:
                print(f"  ! post-test snapshot write failed: {exc}", flush=True)
                post_snapshot_path = None

            if pre_snapshot_path is not None:
                test_entry["pre_memory_snapshot_path"] = str(pre_snapshot_path)
            if post_snapshot_path is not None:
                test_entry["post_memory_snapshot_path"] = str(post_snapshot_path)

        # Per-test cost computation (spec §8.2.4). Falls back to null
        # with a reason if usage data missing or model unknown — never
        # fails the run (per task constraints).
        cost_usd: float | None = None
        cost_unavailable_reason: str | None = None
        normalized = _normalize_usage(token_usage_raw)
        # Prefer the recorded model over the configured fallback.
        driver_model = (
            getattr(r, "model", None)
            or (token_usage_raw.get("model") if isinstance(token_usage_raw, dict) else None)
        )
        effective_model_id = driver_model or model_id
        if driver_model and results.get("model_id") != driver_model:
            if results.get("model_id") in (None, model_id):
                results["model_id"] = driver_model
                model_id = driver_model
        if pricing_snapshot is None:
            cost_unavailable_reason = (
                f"pricing_snapshot_unavailable: {pricing_load_error}"
            )
        elif not token_usage_raw:
            cost_unavailable_reason = "driver_did_not_expose_token_usage"
        elif normalized.get("input_tokens") in (None, 0) and \
                normalized.get("output_tokens") in (None, 0):
            cost_unavailable_reason = "token_usage_block_empty"
        else:
            try:
                cost_usd = compute_call_cost(normalized, effective_model_id, pricing_snapshot)
            except UnknownModelError:
                cost_unavailable_reason = (
                    f"model_id_not_in_pricing_snapshot: {effective_model_id}"
                )
            except Exception as exc:
                cost_unavailable_reason = f"cost_computation_error: {exc}"

        test_entry["cost_usd"] = cost_usd
        test_entry["pricing_version"] = pricing_ver
        if cost_unavailable_reason:
            test_entry["cost_unavailable_reason"] = cost_unavailable_reason

        results["test_results"].append(test_entry)
        test_entry["grading_status"] = "pending"
        # Save the agent output before grading starts. A resumed run grades
        # this output instead of asking the agent to perform the test again.
        _atomic_write_json(out_path, results)
        background_grader.submit(test_entry, pre_spec)
        _record_completed_grades(background_grader.collect(wait=False))
        if (
            test_isolation_profile
            and os.environ.get("DOLPHINBENCH_KEEP_HERMES_TEST_PROFILES", "0").lower()
            not in {"1", "true", "yes", "on"}
        ):
            _remove_hermes_profile(test_isolation_profile)


    _record_completed_grades(background_grader.finish())

    # Clear the per-test tool scope so the mock defaults to full set
    # when we're done (and on any future unrelated invocation).
    _write_mock_tool_config(None)

    if provider == "honcho":
        _require_healthy_honcho(profile, "test phase")

    results["ended"] = datetime.now(timezone.utc).isoformat()

    # Aggregate recorded costs using the same prices as per-test accounting.
    try:
        results["cost_breakdown"] = _read_cost_ledger(
            ledger_path=ledger_path,
            phase1_start=results.get("phase1_started_at"),
            phase2_start=results.get("phase2_started_at"),
            run_end=results["ended"],
            pricing=pricing_snapshot or {},
        )
    except Exception as exc:
        print(f"  ! cost_breakdown computation failed: {exc}", flush=True)
        results["cost_breakdown"] = {"error": str(exc)}
    if provider == "honcho":
        trace_path = Path(os.environ.get(
            "DOLPHINBENCH_HONCHO_REASONING_TRACE",
            "/home/ubuntu/services/honcho/runtime/reasoning.jsonl",
        ))
        results["honcho_internal_reasoning"] = _read_honcho_reasoning_trace(
            trace_path,
            results.get("started"),
            results["ended"],
            pricing_snapshot,
        )
    else:
        results["provider_fees"] = {
            "embedding_fees": "unavailable",
            "platform_fees": "unavailable" if provider == "mem0" else "not_applicable",
        }

    # ── Memory capture: end-of-run snapshot ──
    if args.capture_memory:
        final_snap = _safe_snapshot(provider, profile)
        try:
            final_dump_path = _memory_dump_path(out_path, "final_memory")
            final_dump_path.parent.mkdir(parents=True, exist_ok=True)
            final_dump_path.write_text(
                json.dumps(final_snap, indent=2, default=str))
            print(f"  ↳ final memory snapshot → {final_dump_path}", flush=True)
        except Exception as exc:
            print(f"  ! final snapshot write failed: {exc}", flush=True)

    # Summary
    passes = sum(1 for t in results["test_results"] if t["passed"])
    total = len(results["test_results"])

    # Cost aggregates. Tests where cost couldn't be computed are excluded
    # from sums/means but counted via cost_unavailable_count for transparency.
    costs = [t["cost_usd"] for t in results["test_results"]
             if t.get("cost_usd") is not None]
    cost_unavailable = total - len(costs)
    total_cost = sum(costs) if costs else 0.0
    mean_cost = (total_cost / len(costs)) if costs else None
    median_cost = _percentile(costs, 50) if costs else None

    # Latency aggregates (already captured per test).
    latencies = [t["latency_seconds"] for t in results["test_results"]
                 if t.get("latency_seconds") is not None]
    median_latency = _percentile(latencies, 50) if latencies else None
    p95_latency = _percentile(latencies, 95) if latencies else None
    total_latency = sum(latencies) if latencies else 0.0

    seed_costs = [
        call["cost_usd"] for call in results.get("seed_calls", [])
        if call.get("cost_usd") is not None
    ]
    seed_cost_unavailable = len(results.get("seed_calls", [])) - len(seed_costs)
    seed_cost = sum(seed_costs)
    # The base profile contains ingestion sessions while each test runs in a
    # separate copied profile. Summing captured seed calls and per-test calls
    # captures agent inference across both phases; the base profile DB does not.
    headline_cost = (
        seed_cost + total_cost
        if seed_cost_unavailable == 0 and cost_unavailable == 0
        else None
    )
    honcho_reasoning_cost = (
        (results.get("honcho_internal_reasoning") or {}).get("cost_usd")
        if provider == "honcho" else 0.0
    )
    total_with_honcho_reasoning = (
        headline_cost + honcho_reasoning_cost
        if headline_cost is not None and honcho_reasoning_cost is not None
        else None
    )

    results["summary"] = {
        "passes": passes,
        "total": total,
        "pass_rate": passes / total if total else 0,
        "total_cost_usd": headline_cost,
        "total_cost_usd_including_honcho_reasoning": total_with_honcho_reasoning,
        "honcho_internal_reasoning_cost_usd": honcho_reasoning_cost,
        "total_cost_usd_seed_calls": seed_cost,
        "total_cost_usd_test_calls": total_cost,
        "cost_calculation_method": (
            "Sum each recorded API request after pricing that request's "
            "API-reported token usage separately."
        ),
        "mean_cost_usd": mean_cost,
        "median_cost_usd": median_cost,
        "cost_unavailable_count": cost_unavailable,
        "seed_cost_unavailable_count": seed_cost_unavailable,
        "agent_inference_cost_scope": _AGENT_INFERENCE_COST_SCOPE,
        "total_latency_seconds": total_latency,
        "median_latency_seconds": median_latency,
        "p95_latency_seconds": p95_latency,
        "pricing_version": pricing_ver,
        "model_id": model_id,
    }

    # Write results (out_path already initialized above for memory dumps).
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, default=str))

    print(f"\n{'='*60}", flush=True)
    print(f"Results: {out_path}", flush=True)
    if total:
        print(f"  {passes}/{total} passed ({100*passes/total:.0f}%)", flush=True)
    else:
        print("  (no tests selected -- seed-only run)", flush=True)
    if headline_cost is not None:
        print(
            f"  Total agent inference cost (ingestion + tests): "
            f"${headline_cost:.4f} [seed ${seed_cost:.4f} + "
            f"tests ${total_cost:.4f}]",
            flush=True,
        )
    else:
        print(
            "  Total agent inference cost unavailable: "
            f"{seed_cost_unavailable} seed call(s) and "
            f"{cost_unavailable} test call(s) lack cost data",
            flush=True,
        )
    print(f"  Cost scope: {_AGENT_INFERENCE_COST_SCOPE}", flush=True)
    if costs:
        mean_str = f"${mean_cost:.4f}" if mean_cost is not None else "n/a"
        median_str = f"${median_cost:.4f}" if median_cost is not None else "n/a"
        print(f"  Cost: total ${total_cost:.4f} | mean {mean_str} | "
              f"median {median_str} (over {len(costs)}/{total} tests; "
              f"pricing {pricing_ver})", flush=True)
        if cost_unavailable:
            print(f"  Cost unavailable for {cost_unavailable} test(s) — "
                  f"see cost_unavailable_reason field", flush=True)
    else:
        print(f"  Cost: unavailable for all tests "
              f"(pricing_version={pricing_ver})", flush=True)
    if latencies:
        med_l = f"{median_latency:.2f}s" if median_latency is not None else "n/a"
        p95_l = f"{p95_latency:.2f}s" if p95_latency is not None else "n/a"
        print(f"  Latency (wall-clock): total {total_latency:.1f}s | "
              f"median {med_l} | p95 {p95_l}", flush=True)

    cb = results.get("cost_breakdown") or {}
    if cb.get("total_calls"):
        def _cost_text(value: Any) -> str:
            return f"${value:.4f}" if value is not None else "unavailable"
        print(f"  Cost ledger (per-call): "
              f"{_cost_text(cb.get('total_cost'))} total over {cb['total_calls']} calls "
              f"[P1: {_cost_text(cb.get('phase1_cost'))}/{cb['phase1_calls']} calls | "
              f"P2: {_cost_text(cb.get('phase2_cost'))}/{cb['phase2_calls']} calls]",
              flush=True)
        if cb.get("by_model"):
            for m, info in sorted(cb["by_model"].items(),
                                   key=lambda kv: -(kv[1]["cost"] or -1.0)):
                print(f"    {m}: {_cost_text(info['cost'])} "
                      f"({info['calls']} calls, "
                      f"in={info['input_tokens']:,} "
                      f"out={info['output_tokens']:,} "
                      f"cache_r={info['cache_read_input_tokens']:,})",
                      flush=True)

    print(f"{'='*60}", flush=True)

    # Per-test summary
    print(f"\n{'test_id':<12} {'source':<8} {'result':<12}")
    print("-" * 35)
    for t in results["test_results"]:
        v = "PASS" if t["passed"] else "FAIL"
        s = t["grade"].get("score", 0)
        print(f"{t['test_id']:<12} {t['source_id']:<8} {v} ({s:.2f})")

    return 0


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    return run_simulation(args)


if __name__ == "__main__":
    sys.exit(main())
