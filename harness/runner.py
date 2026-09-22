"""Resumable external-runner foundation. No provider calls without confirmation."""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import sys
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

import yaml

from graders.judge_recording import replay_judge
from graders.mechanical import grade_tool_trace
from harness.adapter import Interaction, load_adapter
from harness.durable_json import save_json as save
from harness.submission import (
    SubmissionError, check_messages, check_settings, check_total_cost, load_json, read_release, write_zip,
)
from harness.submission import _hoist_system_prompt, _shared, record_settings


ROOT = Path(__file__).resolve().parents[1]
JUDGE = {"model": "gpt-5.6-sol", "reasoning_effort": "medium"}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


@contextmanager
def run_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another runner owns this output directory") from exc
        yield


def initialize(config: Path):
    base = config.resolve().parent
    harness = Path.cwd() / "my_harness.py"
    files = {
        harness: (ROOT / "examples/harness_template.py").read_text(),
        config: yaml.safe_dump({
            "release": os.path.relpath(ROOT, base),
            "output": os.path.relpath(Path.cwd() / "tmp/my-agent", base),
            "adapter": "my_harness:BenchmarkHarness", "options": {},
        }, sort_keys=False),
    }
    for path in files:
        if os.path.lexists(path):
            raise ValueError(f"{path} already exists; init will not replace your files")
    created = []
    try:
        for path, content in files.items():
            with path.open("x") as stream:
                created.append(path)
                stream.write(content)
    except OSError:
        for path in created:
            path.unlink()
        raise
    print(f"Created {harness.name} and {config}.\n"
          "Implement the five TODO methods in my_harness.py, then run:\n"
          "python -m harness.runner prepare" + (f" --config {config}" if config != Path("run.yaml") else ""))


class Runner:
    def __init__(self, release: dict, directory: Path, adapter, *, identity: dict,
                 release_root: Path = ROOT, allow_paid: bool = False):
        self.release = release
        self.directory = directory
        self.adapter = adapter
        self.release_root = release_root
        self.allow_paid = allow_paid
        if adapter is not None and not callable(getattr(adapter, "total_cost_usd", None)):
            raise ValueError("Implement total_cost_usd(phase) in the harness integration before running")
        if allow_paid:
            from graders import llm_judge
            if (llm_judge.BACKEND, llm_judge.AZURE_DEPLOYMENT, llm_judge.JUDGE_REASONING_EFFORT) != (
                "azure", "gpt-5.6-sol", "medium"
            ):
                raise ValueError("Restore the fixed Azure gpt-5.6-sol, medium judge settings before execution")
        directory.mkdir(parents=True, exist_ok=True)
        fingerprint = {"version": 1, "release": digest(release), "configuration": identity}
        path = directory / "run.json"
        if path.exists():
            if load_json(path.read_bytes()) != fingerprint:
                raise ValueError("Run inputs changed; use a new output directory")
        else:
            if any(directory.glob("*/**/*.json")):
                raise ValueError("Existing evidence has no run identity")
            save(path, fingerprint)

    def _path(self, phase, persona, item_id):
        return self.directory / phase / persona / f"{item_id}.json"

    def _apps(self, phase, persona, item_id, spec):
        workspace = self.directory / "apps" / persona / ("ingestion" if phase == "ingestion" else item_id)
        workspace.mkdir(parents=True, exist_ok=True)
        state = workspace / "state.json"
        baseline = self.release_root / "mock_mcp/state" / f"{persona}_baseline.json"
        if phase == "tests" or not state.exists():
            value = spec.get("mock_state")
            if value is None:
                value = load_json(baseline.read_bytes())
            save(state, value)
        log = workspace / "calls.jsonl"
        log.write_text("")
        tool_config = workspace / "tools.json"
        scope = spec.get("tools") if phase == "tests" else None
        if scope is None:
            tool_config.unlink(missing_ok=True)
        else:
            save(tool_config, {"active_tools": [t if isinstance(t, str) else t["name"] for t in scope]})
        entrypoint = self.release_root / "mock_mcp/server.py"
        manifest_path = self.release_root / "mock_mcp/manifests" / f"{persona}.yaml"
        extra_env = {}
        runtime = self.release.get(persona, {}).get("runtime") if phase == "tests" else None
        if runtime is not None:
            entry = runtime["tests"][item_id]
            save(tool_config, entry["tool_config"])
            directory_path = workspace / "directory.json"
            save(directory_path, runtime["workspaces"][entry["workspace"]])
            manifest_path = workspace / "manifest.yaml"
            manifest_path.write_text(yaml.safe_dump(runtime["manifest"], sort_keys=False))
            entrypoint = self.release_root / "mock_mcp/repair_server.py"
            extra_env["DOLPHINBENCH_WORKSPACE_DIRECTORY"] = str(directory_path)
        return {
            "command": getattr(self.adapter, "app_python", sys.executable),
            "args": [str(entrypoint)],
            "env": {
                "DOLPHINBENCH_PERSONA": persona,
                "DOLPHINBENCH_MOCK_MANIFEST": str(manifest_path),
                "DOLPHINBENCH_STATE_PATH": str(state),
                "DOLPHINBENCH_LOG_PATH": str(log),
                "DOLPHINBENCH_TOOL_CONFIG_PATH": str(tool_config),
                "DOLPHINBENCH_RUN_ID": self.directory.name,
                **extra_env,
            },
        }

    def _execute(self, phase, persona, spec):
        item_id = str(spec["id"]).zfill(6 if phase == "ingestion" else 3)
        path = self._path(phase, persona, item_id)
        marker = path.with_suffix(".inflight")
        if path.exists():
            value = load_json(path.read_bytes())
            marker.unlink(missing_ok=True)
            return value
        if marker.exists():
            raise ValueError(f"Uncertain interaction {phase}/{persona}/{item_id}; reconcile before resuming")
        message = spec["messages"][0].strip() if phase == "ingestion" else spec["test"]
        date = spec["narrative_date"] if phase == "ingestion" else spec["narrative_anchor_date"]
        request = Interaction(persona, phase, item_id, message, date,
                              self._apps(phase, persona, item_id, spec), self.directory)
        save(marker, {"source_sha256": digest(spec)})
        started = time.monotonic()
        result = self.adapter.run_interaction(request)
        duration = (time.monotonic() - started) * 1000
        if result.duration_ms is not None:
            duration = result.duration_ms
        # Persist returned evidence even if validation fails. Never repeat a call
        # just because export validation or a controller write failed afterward.
        evidence = {"settings": result.settings, "messages": result.messages, "duration_ms": duration,
                    "attempts": result.attempts, "app_calls": result.app_calls}
        save(path.with_suffix(".returned"), evidence)
        check_settings(result.settings, f"{phase}/{persona}/{item_id}")
        check_messages(result.messages, f"{phase}/{persona}/{item_id}")
        users = [m["content"] for m in result.messages if m["role"] == "user"]
        if users != [request.dated_message]:
            raise SubmissionError("Adapter returned a different user message or conversation boundary")
        row = {"persona": persona, "session_id" if phase == "ingestion" else "test_id": item_id,
               "messages": result.messages, "duration_ms": duration}
        evidence = {"settings": result.settings, "row": row, "attempts": result.attempts}
        from graders.mechanical import load_tool_calls
        app_calls = load_tool_calls(Path(request.apps["env"]["DOLPHINBENCH_LOG_PATH"]))
        if result.app_calls is not None:
            row["app_calls"] = result.app_calls
        elif app_calls:
            row["app_calls"] = [{key: call[key] for key in ("tool", "args", "result") if key in call} for call in app_calls]
        save(path, evidence)
        marker.unlink()
        return evidence

    def ingest(self):
        for persona, release in self.release.items():
            checkpoint = self.directory / "checkpoints" / f"{persona}.json"
            if checkpoint.exists():
                self.adapter.verify_checkpoint(persona, load_json(checkpoint.read_bytes()))
                continue
            for spec in release["sessions"]:
                self._execute("ingestion", persona, spec)
            save(checkpoint, self.adapter.freeze(persona))
        self._collect_cost("ingestion")

    def _saved_cost(self, phase):
        path = self.directory / "costs" / f"{phase}.json"
        if not path.exists():
            stage = "ingest" if phase == "ingestion" else "evaluate"
            raise ValueError(f"Missing {phase} cost; resume {stage} to collect it without repeating completed interactions")
        return check_total_cost(load_json(path.read_bytes())["total_cost_usd"], phase)

    def _collect_cost(self, phase):
        path = self.directory / "costs" / f"{phase}.json"
        if path.exists():
            return self._saved_cost(phase)
        cost = check_total_cost(self.adapter.total_cost_usd(phase), phase)
        save(path, {"total_cost_usd": cost})
        return cost

    def _grade(self, spec, evidence):
        if spec["grade"]["type"] != "tool_trace":
            raise ValueError("The public submission format requires tool_trace grading")
        from harness.submission import grading_calls
        calls = grading_calls(evidence["row"], "saved execution")
        config = copy.deepcopy(spec["grade"]["config"])
        config["raise_on_judge_error"] = True
        guard = nullcontext() if self.allow_paid else replay_judge([], JUDGE)
        with guard:
            result = grade_tool_trace(calls, config, test_message=spec["test"])
        checks = []
        settings = None
        for index, detail in enumerate(result["details"]):
            check = {"check": index, "passed": detail["ok"]}
            if detail.get("judge_messages"):
                settings = _shared(settings, detail["judge_settings"], "judge settings")
                check["messages"] = detail["judge_messages"]
            checks.append(check)
        return {"checks": checks, "settings": settings or JUDGE}

    def evaluate(self):
        # Check every persona before starting any test.
        for persona in self.release:
            path = self.directory / "checkpoints" / f"{persona}.json"
            if not path.exists():
                raise ValueError(f"Complete ingestion first: {persona}")
            self.adapter.verify_checkpoint(persona, load_json(path.read_bytes()))
        self._saved_cost("ingestion")
        for persona, release in self.release.items():
            for spec in release["tests"]:
                evidence = self._execute("tests", persona, spec)
                grade_path = self._path("grades", persona, str(spec["id"]).zfill(3))
                if not grade_path.exists():
                    save(grade_path, self._grade(spec, evidence))
        self._collect_cost("tests")

    def package(self, output):
        for persona in self.release:
            if not (self.directory / "checkpoints" / f"{persona}.json").is_file():
                raise ValueError(f"Missing completed ingestion checkpoint: {persona}")
        settings = {"ingestion": None, "tests": None}
        rows = {"ingestion": [], "tests": []}
        judge_settings = None
        for phase in rows:
            for persona, release in self.release.items():
                for spec in release["sessions" if phase == "ingestion" else "tests"]:
                    item_id = str(spec["id"]).zfill(6 if phase == "ingestion" else 3)
                    evidence = load_json(self._path(phase, persona, item_id).read_bytes())
                    row = copy.deepcopy(evidence["row"])
                    settings[phase] = record_settings(settings[phase], evidence["settings"], [row], phase)
                    if phase == "tests":
                        grade = load_json(self._path("grades", persona, item_id).read_bytes())
                        # A no-call check carries only the fixed judge identity.
                        if grade["settings"] != JUDGE:
                            judge_settings = _shared(judge_settings, grade["settings"], "judge settings")
                        row["grading"] = grade["checks"]
                    rows[phase].append(row)
            _hoist_system_prompt(settings[phase], rows[phase])
        return write_zip(output, {"settings": settings["ingestion"], "sessions": rows["ingestion"],
                                  "total_cost_usd": self._saved_cost("ingestion")},
                         {"settings": settings["tests"], "judge_settings": judge_settings or JUDGE,
                          "total_cost_usd": self._saved_cost("tests"),
                          "tests": rows["tests"]}, self.release)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("init", "prepare", "ingest", "evaluate", "package"))
    parser.add_argument("--config", type=Path, default=Path("run.yaml"), help="Run configuration (default: run.yaml)")
    parser.add_argument("--confirm-paid-calls", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.stage == "init":
            initialize(args.config)
            return 0
        if not args.config.is_file():
            raise ValueError(f"{args.config} not found. Run python -m harness.runner init, or pass --config FILE")
        config = yaml.safe_load(args.config.read_text())
        if set(config) != {"release", "output", "adapter", "options"}:
            raise ValueError("Config requires exactly release, output, adapter, options")
        base = args.config.resolve().parent
        release_root = (base / config["release"]).resolve()
        output = (base / config["output"]).resolve()
        # Execution is gated before imports. Prepare trusts the adapter contract
        # that imports, construction, and identity checks are local-only.
        if (args.stage in {"ingest", "evaluate"} and config["adapter"] != "examples.offline_adapter:OfflineAdapter"
                and not args.confirm_paid_calls):
            raise ValueError("Agent execution requires --confirm-paid-calls")
        release = read_release(release_root)
        with run_lock(output):
            adapter = None
            source_files = [*sorted((ROOT / "harness").rglob("*.py")), *sorted((ROOT / "graders").rglob("*.py")),
                            *sorted((ROOT / "mock_mcp").rglob("*.py")), *sorted((ROOT / "examples").rglob("*.py")),
                            *sorted((ROOT / "examples/reference").glob("*")),
                            ROOT / "harness/benchmark_soul.md",
                            *sorted((release_root / "mock_mcp/manifests").glob("*.yaml")),
                            *sorted((release_root / "mock_mcp/state").glob("*_baseline.json"))]
            identity = {"config": config, "code": {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                                     for p in source_files if not p.name.startswith("test_")}}
            if args.stage == "package":
                identity["adapter"] = load_json((output / "run.json").read_bytes())["configuration"]["adapter"]
            else:
                previous = output / "run.json"
                if previous.exists():
                    old = load_json(previous.read_bytes())["configuration"]
                    if any(old[key] != identity[key] for key in ("config", "code")):
                        raise ValueError("Run inputs changed; refusing to load the adapter")
                adapter = load_adapter(config["adapter"], config["options"], output)
                identity["adapter"] = adapter.identity()
            runner = Runner(release, output, adapter, identity=identity, release_root=release_root,
                            allow_paid=args.confirm_paid_calls)
            if args.stage == "prepare":
                print(json.dumps({"prepared": str(output), "adapter": config["adapter"],
                                  "reference": identity["adapter"].get("reference"),
                                  "model_calls": 0}, indent=2))
            elif args.stage == "package":
                print(json.dumps(runner.package(output / "submission.zip"), indent=2))
            else:
                getattr(runner, args.stage)()
                print(f"{args.stage} complete: {output}")
        return 0
    except (ValueError, OSError, KeyError, TypeError, NotImplementedError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
