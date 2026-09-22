"""Built-in-memory adapter using the unchanged Hermes subprocess driver.

This first integration deliberately refuses external memory providers: their
completion receipts and service checkpoint lifecycles need separate adapters.
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

import yaml

from harness import hermes_driver
from harness.adapter import InteractionRecord
from harness.submission_capture import read_recording
from harness.submission import check_total_cost, load_json
from harness.durable_json import save_json


TOOLSETS = ["hermes-cli", "dolphinbench-apps", "memory", "session_search"]
READ_ONLY = ["DOLPHINBENCH_MEMORY_READ_ONLY", "DOLPHINBENCH_MEM0_READ_ONLY",
             "DOLPHINBENCH_HONCHO_READ_ONLY", "DOLPHINBENCH_NATIVE_MEMORY_READ_ONLY",
             "DOLPHINBENCH_SINGLE_TURN"]


@contextmanager
def environment(values):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def tree_identity(path):
    result = {}
    for file in sorted(path.rglob("*")):
        if file.is_symlink():
            raise ValueError(f"Profile symlinks are not supported: {file}")
        if file.is_file():
            result[str(file.relative_to(path))] = hashlib.sha256(file.read_bytes()).hexdigest()
    return result


def copy_profile(source, target):
    # Same excluded directories as run_simulation._copy_hermes_profile.
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("logs", "workspace", "__pycache__"))


def profile_cost(directory, *, empty_ok=False):
    path = directory / "state.db"
    if not path.exists():
        if empty_ok:
            return 0.0
        raise ValueError(f"Missing Hermes cost records: {path}")
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # The per-model table includes auxiliary calls; never sum both tables.
        table = "session_model_usage" if "session_model_usage" in tables else "sessions"
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        fields = [name for name in ("actual_cost_usd", "estimated_cost_usd", "cost_status") if name in columns]
        if "estimated_cost_usd" not in fields and "actual_cost_usd" not in fields:
            raise ValueError(f"Hermes database has no recorded costs: {path}")
        rows = list(connection.execute(f"SELECT {', '.join(fields)} FROM {table}"))
    if not rows and not empty_ok:
        raise ValueError(f"Empty Hermes cost records: {path}")
    costs = []
    for row in rows:
        fields = dict(row)
        if fields.get("cost_status") in {"unknown", "unavailable", "partial"}:
            raise ValueError(f"Incomplete Hermes cost records: {path}")
        cost = fields.get("actual_cost_usd")
        if cost is None:
            cost = fields.get("estimated_cost_usd")
        costs.append(check_total_cost(cost, str(path)))
    return check_total_cost(math.fsum(costs), str(path))


class HermesAdapter:
    def __init__(self, options, work_dir):
        self.home = work_dir / "hermes"
        self.profiles = self.home / "profiles"
        self.generated_templates = None
        self.reference = None
        if "reference" in options:
            from harness.adapters.hermes_reference import prepare_reference
            self.reference = prepare_reference(options, self.home)
            self.binary = self.reference["binary"]
            self.app_python = self.reference["app_python"]
            self.generated_templates = self.reference["templates"]
            self.templates = {}
            return
        if set(options) != {"profiles", "binary"} or set(options["profiles"]) != {"morgan", "alex", "riley"}:
            raise ValueError("Hermes options require binary and three empty persona profiles")
        self.templates = {p: Path(path).resolve() for p, path in options["profiles"].items()}
        self.binary = str(Path(options["binary"]).resolve())
        if not Path(self.binary).is_file():
            raise ValueError("Hermes binary does not exist")
        for persona, template in self.templates.items():
            tree_identity(template)
            cfg = yaml.safe_load((template / "config.yaml").read_text())
            if cfg.get("memory", {}).get("provider") != "builtin":
                raise ValueError("This adapter currently supports only Hermes built-in memory")
            if set(cfg.get("toolsets", [])) != set(TOOLSETS):
                raise ValueError("Template must preserve the exact benchmark toolsets")
            for toolsets in cfg.get("platform_toolsets", {}).values():
                if set(toolsets) != set(TOOLSETS):
                    raise ValueError("Template platform toolsets differ from benchmark tools")
            if set(cfg.get("mcp_servers", {})) != {"dolphinbench-apps"}:
                raise ValueError("Template must contain only the benchmark app server")
            if (template / "state.db").exists() or any((template / "sessions").glob("*")):
                raise ValueError("Use an empty profile template, not an existing ingestion")
            if any(p.is_file() and p.stat().st_size for p in (template / "memories").rglob("*")):
                raise ValueError("Template already contains built-in memories")

    def identity(self):
        templates = ({p: {name: hashlib.sha256(content.encode()).hexdigest() for name, content in files.items()}
                      for p, files in self.generated_templates.items()} if self.generated_templates is not None else
                     {p: tree_identity(path) for p, path in self.templates.items()})
        return {"binary_sha256": hashlib.sha256(Path(self.binary).read_bytes()).hexdigest(),
                "agent_image": os.environ.get("DOLPHINBENCH_AGENT_IMAGE"),
                "reference": self.reference["identity"] if self.reference else None,
                "source_root": self.reference["source"] if self.reference else None,
                "retry_environment": {name: os.environ.get(name, default) for name, default in (
                    ("DOLPHINBENCH_SEED_RETRIES", "3"), ("DOLPHINBENCH_TEST_RETRIES", "3"),
                    ("DOLPHINBENCH_RETRY_BASE_SECONDS", "5"), ("DOLPHINBENCH_RETRY_MAX_SECONDS", "60"))},
                "templates": templates}

    def _initialize(self, persona):
        self.profiles.mkdir(parents=True, exist_ok=True)
        destination = self.profiles / persona
        if not destination.exists():
            temporary = self.profiles / f"{persona}.initializing"
            if temporary.exists():
                raise ValueError("Interrupted profile initialization requires inspection")
            if self.generated_templates is not None:
                temporary.mkdir()
                for name, content in self.generated_templates[persona].items():
                    (temporary / name).write_text(content)
            else:
                copy_profile(self.templates[persona], temporary)
            temporary.rename(destination)

    def freeze(self, persona):
        source = self.profiles / persona
        destination = self.profiles / f"{persona}-frozen"
        if not destination.exists():
            temporary = self.profiles / f"{persona}.freezing"
            if temporary.exists():
                raise ValueError("Interrupted memory freeze requires inspection")
            copy_profile(source, temporary)
            temporary.rename(destination)
        return {"files": tree_identity(destination)}

    def verify_checkpoint(self, persona, checkpoint):
        directory = self.profiles / f"{persona}-frozen"
        if not directory.is_dir() or checkpoint != {"files": tree_identity(directory)}:
            raise ValueError(f"Frozen Hermes memory changed: {persona}")

    def total_cost_usd(self, phase):
        if phase not in {"ingestion", "tests"}:
            raise ValueError(f"Unknown phase: {phase}")
        costs = []
        journals = sorted((self.home / "attempts" / phase).glob("*/*.json"))
        if not journals:
            raise ValueError(f"Missing Hermes attempts for {phase}")
        if phase == "ingestion":
            # Fresh persona profiles contain all ingestion attempts and late auxiliary work.
            return check_total_cost(math.fsum(profile_cost(self.profiles / persona)
                for persona in sorted({journal.parent.name for journal in journals})), phase)
        for journal in journals:
            for attempt in load_json(journal.read_bytes()):
                path = self.home / "costs" / phase / journal.parent.name / f"{journal.stem}-{attempt['attempt']}.json"
                if not path.exists():
                    raise ValueError(f"Missing Hermes attempt cost: {path}; completed interactions must not be replayed")
                record = load_json(path.read_bytes())
                if record.get("error"):
                    before = check_total_cost(record.get("before_usd"), str(path))
                    after = profile_cost(Path(record["profile"]))
                    record = {"before_usd": before, "after_usd": after,
                              "total_cost_usd": check_total_cost(after - before, str(path))}
                    save_json(path, record)
                costs.append(check_total_cost(record.get("total_cost_usd"), str(path)))
        return check_total_cost(math.fsum(costs), phase)

    def run_interaction(self, request):
        from harness.hermes_driver import _failure_details, _retry_delay, _seed_delivery_state

        testing = request.phase == "tests"
        variable = "DOLPHINBENCH_TEST_RETRIES" if testing else "DOLPHINBENCH_SEED_RETRIES"
        retries = int(os.environ.get(variable, "3"))
        if retries < 0:
            raise ValueError("Retry count must be nonnegative")
        journal = self.home / "attempts" / request.phase / request.persona / f"{request.interaction_id}.json"
        if journal.exists():
            raise ValueError("Saved interaction attempts already exist; refusing to replay")
        state_path = request.apps["env"].get("DOLPHINBENCH_STATE_PATH")
        initial_state = Path(state_path).read_bytes() if testing and state_path else None
        attempts = []
        for number in range(1, retries + 2):
            if number > 1 and initial_state is not None:
                Path(state_path).write_bytes(initial_state)
                Path(request.apps["env"]["DOLPHINBENCH_LOG_PATH"]).write_text("")
            result, recording, duration = self._attempt(request, number)
            failure = _failure_details(result)
            delivery = None if testing else _seed_delivery_state("builtin", result)
            attempts.append({"attempt": number, "driver_ok": result.ok, "driver_error": result.error,
                             "latency_seconds": duration / 1000, "transport_failure": failure.__dict__,
                             "seed_delivery_state": delivery, "session_id": result.session_id,
                             "recording": str(recording) if recording.is_file() else None})
            save_json(journal, attempts)
            retry = failure.transient and not result.ok and (testing or delivery == "known_not_delivered")
            if not retry or number > retries:
                break
            time.sleep(_retry_delay(failure, number))
        if not testing and delivery != "delivered":
            raise RuntimeError(f"Hermes ingestion {delivery}: {result.error}")
        if not result.ok and not recording.is_file():
            raise RuntimeError(f"Hermes interaction has no complete recorded response: {result.error}")
        trace = read_recording(recording)
        if testing:
            # Archive before dropping the large successful disposable profile.
            archive = self.home / "test-traces" / request.persona / f"{request.interaction_id}.jsonl"
            archive.parent.mkdir(parents=True, exist_ok=True)
            os.link(recording, archive)
            with archive.open("rb") as saved:
                os.fsync(saved.fileno())
            parent = os.open(archive.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
            attempts[-1]["recording"] = str(archive)
            save_json(journal, attempts)
            cost_path = self.home / "costs" / request.phase / request.persona / f"{request.interaction_id}-{number}.json"
            if cost_path.exists() and not load_json(cost_path.read_bytes()).get("error"):
                shutil.rmtree(recording.parent.parent)
        else:
            time.sleep(0.5)
        from graders.mechanical import load_tool_calls
        from harness.hermes_driver import _fallback_session_calls_for_grading
        log = request.apps["env"].get("DOLPHINBENCH_LOG_PATH")
        calls = load_tool_calls(Path(log)) if log else []
        if not calls:
            calls = _fallback_session_calls_for_grading(getattr(result, "tool_calls", []))
        actions = [{key: call[key] for key in ("tool", "args", "result") if key in call} for call in calls]
        return InteractionRecord(trace["settings"], trace["messages"], duration, attempts, app_calls=actions)

    def _attempt(self, request, number):
        self._initialize(request.persona)
        profile = request.persona
        if request.phase == "tests":
            profile = f"{request.persona}-test-{request.interaction_id}-a{number:02d}"
            destination = self.profiles / profile
            archive = self.home / "test-traces" / request.persona / f"{request.interaction_id}.jsonl"
            if destination.exists() or archive.exists():
                raise ValueError("Test profile already exists; refusing to replay")
            copy_profile(self.profiles / f"{request.persona}-frozen", destination)
        directory = self.profiles / profile
        config_path = directory / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        apps = config["mcp_servers"]["dolphinbench-apps"]
        apps.update(request.apps)
        config_path.write_text(yaml.safe_dump(config, sort_keys=False))
        values = {
            **request.apps["env"], "HERMES_HOME": str(self.home),
            "DOLPHINBENCH_CAPTURE_SUBMISSION": "1",
            "DOLPHINBENCH_HERMES_TOOLSETS": ",".join(TOOLSETS),
            **{key: "1" if request.phase == "tests" else "0" for key in READ_ONLY},
        }
        previous_binary = hermes_driver.HERMES_BIN
        cost_path = self.home / "costs" / request.phase / request.persona / f"{request.interaction_id}-{number}.json"
        try:
            cost_before = profile_cost(directory, empty_ok=True)
            cost_error = None
        except (ValueError, sqlite3.Error) as exc:
            cost_before, cost_error = None, str(exc)
        if self.reference:
            values["PYTHONPATH"] = self.reference["source"]
        try:
            hermes_driver.HERMES_BIN = self.binary
            started = time.monotonic()
            with environment(values):
                result = hermes_driver.run_hermes(profile, request.message,
                    timeout=None, model=None, narrative_time=request.narrative_time)
                from harness.hermes_driver import _validate_hermes_runtime_tools
                if result.session_id or getattr(result, "available_tools", None):
                    _validate_hermes_runtime_tools(result, "seed" if request.phase == "ingestion" else "test",
                                                  request.interaction_id, "builtin")
            duration = (time.monotonic() - started) * 1000
            try:
                if cost_error:
                    raise ValueError(cost_error)
                cost_after = profile_cost(directory)
                cost = check_total_cost(cost_after - cost_before, str(cost_path))
                save_json(cost_path, {"before_usd": cost_before, "after_usd": cost_after,
                                      "total_cost_usd": cost})
            except (ValueError, sqlite3.Error) as exc:
                # Keep the profile and completed response if accounting is incomplete.
                save_json(cost_path, {"before_usd": cost_before, "profile": str(directory), "error": str(exc)})
            recording = directory / "submission-traces" / f"{result.session_id}.jsonl"
            return result, recording, duration
        finally:
            hermes_driver.HERMES_BIN = previous_binary
