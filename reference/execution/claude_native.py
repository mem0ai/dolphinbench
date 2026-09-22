"""Reference claude native implementation."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


from harness.claude_driver import ClaudeResult, run_claude
from reference.runtimes import claude_code as native

try:
    import modal
except ModuleNotFoundError:  # Local planning and tests do not require Modal.
    modal = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = ROOT / ("registry/personas/morgan/life_sim.yaml" if native.PERSONA == "morgan"
                        else f"construction/v2/{native.PERSONA}/release/500k_final/life_sim.yaml")
REMOTE_ROOT = Path("/home/ubuntu/.hermes/benchmark/hbme")
REMOTE_CORPUS = REMOTE_ROOT / DEFAULT_CORPUS.relative_to(ROOT)
REMOTE_CLAUDE = Path("/home/ubuntu/.local/bin/claude")
VOLUME_NAME = ("dolphinbench-claude-native-seed-v1" if native.PERSONA == "morgan"
               else f"dolphinbench-claude-{native.PERSONA}-native-seed-v1")
VOLUME_ROOT = Path("/mnt/dolphinbench-claude-native-seed")
SECRET_NAME = "dolphinbench-evaluation"
MODEL = "claude-sonnet-5"
PROGRESS_DIRECTORY = "progress"
FINAL_DIRECTORY = "final"
_VERSION = re.compile(r"\b(\d+\.\d+\.\d+)\b")


class NativeSeedIngestionError(RuntimeError):
    """Raised when the durable native-memory seed cannot safely continue."""


class PaidCallApprovalRequired(NativeSeedIngestionError):
    """Raised when a remote seed is requested before explicit approval."""


@dataclass(frozen=True)
class VolumePaths:
    root: Path

    @property
    def progress(self) -> Path:
        return self.root / PROGRESS_DIRECTORY

    @property
    def receipt(self) -> Path:
        return self.progress / "receipt.json"

    @property
    def memory(self) -> Path:
        return self.progress / native.MEMORY_ROOT

    @property
    def inflight(self) -> Path:
        return self.progress / "inflight.json"

    @property
    def final(self) -> Path:
        return self.root / FINAL_DIRECTORY

    @property
    def final_receipt(self) -> Path:
        return self.final / "receipt.json"

    @property
    def final_archive(self) -> Path:
        return self.final / "native-memory.tar.gz"


@dataclass(frozen=True)
class NativeSeedPlan:
    corpus_path: Path
    corpus_sha256: str
    source_count: int
    source_ids_sha256: str
    model: str
    claude_code_version: str
    volume_name: str
    volume_paths: VolumePaths

    def as_dict(self) -> dict[str, Any]:
        return {
            "persona": native.PERSONA,
            "corpus": str(self.corpus_path),
            "corpus_sha256": self.corpus_sha256,
            "source_count": self.source_count,
            "source_ids_sha256": self.source_ids_sha256,
            "model": self.model,
            "claude_code_version": self.claude_code_version,
            "volume": self.volume_name,
            "progress_receipt": str(self.volume_paths.receipt),
            "progress_auto_memory": str(self.volume_paths.memory),
            "inflight_marker": str(self.volume_paths.inflight),
            "final_receipt": str(self.volume_paths.final_receipt),
            "final_archive": str(self.volume_paths.final_archive),
            "remote_command_requires": "--confirm-paid-calls",
        }


def build_plan(
    *, corpus_path: Path = DEFAULT_CORPUS, volume_root: Path = VOLUME_ROOT,
    model: str = MODEL,
) -> NativeSeedPlan:
    """Build the local-only immutable seed contract."""
    messages, corpus_sha256 = native.load_morgan_source_messages(corpus_path)
    if model != MODEL:
        raise NativeSeedIngestionError(f"Claude native seed model must be {MODEL}")
    return NativeSeedPlan(
        corpus_path=corpus_path.resolve(),
        corpus_sha256=corpus_sha256,
        source_count=len(messages),
        source_ids_sha256=native._source_ids_sha256(messages),
        model=model,
        claude_code_version=native.REQUIRED_CLAUDE_CODE_VERSION,
        volume_name=VOLUME_NAME,
        volume_paths=VolumePaths(volume_root.resolve()),
    )


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    native._atomic_json(path, value)


def _copy_memory(source_config: Path, destination_memory: Path) -> None:
    source_memory = native.native_memory_dir(source_config)
    if not source_memory.is_dir():
        raise NativeSeedIngestionError("Claude completed a source without creating auto-memory")
    staging = destination_memory.with_name(f".{destination_memory.name}.staging")
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(source_memory, staging)
    shutil.rmtree(destination_memory, ignore_errors=True)
    shutil.move(str(staging), str(destination_memory))


def _copy_receipt(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    shutil.copyfile(source, temporary)
    os.replace(temporary, destination)


def _inflight_payload(source: native.SourceMessage) -> dict[str, str]:
    return {"source_id": source.source_id, "content_sha256": source.content_sha256}


def _require_no_ambiguous_inflight(paths: VolumePaths) -> None:
    if paths.inflight.exists():
        raise NativeSeedIngestionError(
            "the previous Claude call may have completed but was not checkpointed; "
            "refusing to replay that source"
        )


def _assert_empty_or_known_volume(paths: VolumePaths) -> None:
    if not paths.root.exists():
        return
    known = {PROGRESS_DIRECTORY, FINAL_DIRECTORY, "diagnostics"}
    unexpected = [path.name for path in paths.root.iterdir() if path.name not in known]
    if unexpected:
        raise NativeSeedIngestionError(
            "Claude native seed Volume has unknown files: " + ", ".join(sorted(unexpected))
        )
    if paths.final.exists():
        names = {path.name for path in paths.final.iterdir()}
        complete = {paths.final_receipt.name, paths.final_archive.name}
        if names == complete:
            if paths.inflight.exists():
                raise NativeSeedIngestionError("completed seed Volume also has an in-flight marker")
            return
        if names:
            raise NativeSeedIngestionError(
                "Claude native seed Volume has a partial final publication; preserve it for diagnosis"
            )
        raise NativeSeedIngestionError(
            "Claude native seed Volume has an empty final directory; preserve it for diagnosis"
        )


def _restore_or_initialize(
    *, plan: NativeSeedPlan, paths: VolumePaths, runtime_root: Path,
    recovery: Mapping[str, Any] | None = None, commit: Callable[[], None] = lambda: None,
) -> tuple[Path, Path, bool]:
    """Return clean runtime config and receipt paths without making a Claude call."""
    _assert_empty_or_known_volume(paths)
    if recovery is not None:
        from reference.runtimes.claude_recovery import recovery_completed
        from reference.runtimes.claude_recovery import tree_sha256
        from reference.runtimes.claude_recovery import validate_interrupted_source
        if recovery.get("action") not in {"retry", "continue"} or recovery.get("provider") != "builtin":
            raise NativeSeedIngestionError("Unsupported native recovery adjudication")
        saved_receipt = paths.final_receipt if paths.final_receipt.exists() else paths.receipt
        receipt = json.loads(saved_receipt.read_text())
        native.pending_source_messages(corpus_path=plan.corpus_path, receipt_path=saved_receipt)
        if any(recovery_completed(receipt, record, recovery)
               for record in receipt.get("recovery_adjudications", [])):
            recovery = None
    if recovery is not None:
        messages, _ = native.load_morgan_source_messages(plan.corpus_path)
        record = validate_interrupted_source(
            receipt_path=paths.receipt, marker_path=paths.inflight,
            messages=messages, recovery=recovery,
        )
        native.pending_source_messages(corpus_path=plan.corpus_path, receipt_path=paths.receipt)
        if tree_sha256(paths.memory) != recovery["memory_sha256"]:
            raise NativeSeedIngestionError("Native memory differs from the inspected recovery state")
        if recovery["action"] == "continue":
            _native_continuation_transcript(paths, recovery)
        receipt = json.loads(paths.receipt.read_text())
        receipt.setdefault("recovery_adjudications", []).append(record)
        _write_json(paths.receipt, receipt)
        commit()
        paths.inflight.unlink()
        commit()
    _require_no_ambiguous_inflight(paths)
    runtime_root.mkdir(parents=True, exist_ok=True)
    config_dir = runtime_root / "claude-config"
    receipt_path = runtime_root / "receipt.json"
    shutil.rmtree(config_dir, ignore_errors=True)
    receipt_path.unlink(missing_ok=True)
    if paths.final_receipt.exists():
        native.validate_completed_seed(paths.final_receipt)
        return config_dir, paths.final_receipt, True
    if paths.receipt.exists():
        _copy_receipt(paths.receipt, receipt_path)
        pending = native.pending_source_messages(corpus_path=plan.corpus_path, receipt_path=receipt_path)
        if len(pending) < plan.source_count and not paths.memory.is_dir():
            raise NativeSeedIngestionError("progress receipt has completed sources but no saved auto-memory")
        if paths.memory.is_dir():
            shutil.copytree(paths.memory, native.native_memory_dir(config_dir))
        if recovery is not None and recovery["action"] == "continue":
            transcript = _native_continuation_transcript(paths, recovery)
            destination = config_dir / transcript["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(transcript["text"])
        native.relocate_receipt_config_dir(receipt_path=receipt_path, config_dir=config_dir)
        return config_dir, receipt_path, False
    if paths.progress.exists() and any(paths.progress.iterdir()):
        raise NativeSeedIngestionError("progress directory has no receipt; refusing to initialize over unknown state")
    native.initialize_receipt(
        corpus_path=plan.corpus_path, receipt_path=receipt_path,
        config_dir=config_dir, project_dir=native.FIXED_PROJECT_DIR, model=plan.model,
    )
    return config_dir, receipt_path, False


def _checkpoint_before_call(
    *, paths: VolumePaths, source: native.SourceMessage, commit: Callable[[], None],
) -> None:
    paths.progress.mkdir(parents=True, exist_ok=True)
    _write_json(paths.inflight, _inflight_payload(source))
    commit()


def _checkpoint_after_call(
    *, plan: NativeSeedPlan, paths: VolumePaths, config_dir: Path,
    receipt_path: Path, source: native.SourceMessage, telemetry: Mapping[str, Any],
    commit: Callable[[], None],
) -> None:
    _copy_memory(config_dir, paths.memory)
    native.record_completed_source(
        corpus_path=plan.corpus_path, receipt_path=receipt_path, source_id=source.source_id,
        telemetry=telemetry,
    )
    value = json.loads(receipt_path.read_text())
    value.pop("pause", None)
    _write_json(receipt_path, value)
    _copy_receipt(receipt_path, paths.receipt)
    paths.inflight.unlink(missing_ok=True)
    commit()


def _checkpoint_failed_call(
    *, plan: NativeSeedPlan, paths: VolumePaths, receipt_path: Path,
    source: native.SourceMessage, telemetry: Mapping[str, Any], config_dir: Path,
    pause: Mapping[str, Any] | None,
    commit: Callable[[], None],
) -> None:
    native.record_source_attempt(
        corpus_path=plan.corpus_path, receipt_path=receipt_path, source_id=source.source_id,
        telemetry=telemetry,
    )
    native.native_memory_dir(config_dir).mkdir(parents=True, exist_ok=True)
    _copy_memory(config_dir, paths.memory)
    if pause is not None:
        value = json.loads(receipt_path.read_text())
        value["pause"] = dict(pause)
        _write_json(receipt_path, value)
    _copy_receipt(receipt_path, paths.receipt)
    commit()


def _safe_attempt_telemetry(
    *, source: native.SourceMessage, result: ClaudeResult, elapsed_seconds: float, outcome: str,
) -> dict[str, Any]:
    usage = result.token_usage if isinstance(result.token_usage, Mapping) else {}
    safe_usage = {
        str(name): value for name, value in usage.items()
        if isinstance(name, str) and isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    }
    detected_model = result.model if isinstance(result.model, str) and len(result.model) <= 256 else None
    status_code = result.status_code if isinstance(result.status_code, int) and not isinstance(result.status_code, bool) else None
    return {
        "content_sha256": source.content_sha256,
        "elapsed_seconds": round(max(0.0, elapsed_seconds), 6),
        "detected_model": detected_model,
        "token_usage": safe_usage,
        "status_code": status_code,
        "outcome": outcome,
    }


def _save_failure_evidence(*, paths: VolumePaths, source: native.SourceMessage,
                           result: ClaudeResult, config_dir: Path, token: str,
                           commit: Callable[[], None], controller_error: str | None = None) -> None:
    transcripts = []
    projects = config_dir / "projects"
    for path in projects.rglob("*.jsonl"):
        if ((result.session_id is None or path.name == str(result.session_id) + ".jsonl") and not path.is_symlink()
                and projects.resolve() in path.resolve().parents):
            item = {"path": str(path.relative_to(config_dir))}
            try:
                item["text"] = path.read_text(errors="replace")
            except OSError as exc:
                item["read_error"] = str(exc)
            transcripts.append(item)
    evidence = {**_inflight_payload(source), "result": asdict(result), "transcripts": transcripts,
                "controller_error": controller_error}
    # Diagnostics are controller-only and never enter the native-memory archive.
    encoded = json.dumps(evidence).replace(json.dumps(token)[1:-1], "[REDACTED]") if token else json.dumps(evidence)
    _write_json(paths.root / "diagnostics" / (uuid.uuid4().hex + ".json"), json.loads(encoded))
    commit()


def _publish_final(
    *, plan: NativeSeedPlan, paths: VolumePaths, config_dir: Path,
    receipt_path: Path, commit: Callable[[], None],
) -> dict[str, Any]:
    if paths.final.exists():
        raise NativeSeedIngestionError("refusing to overwrite an existing or partial final publication")
    paths.final.mkdir(parents=True)
    staging = paths.final / ".staging"
    staging.mkdir()
    temporary_archive = config_dir.parent / "native-memory.tar.gz"
    receipt = native.finalize_receipt(
        receipt_path=receipt_path,
        archive_path=temporary_archive,
        published_archive_reference=paths.final_archive.name,
    )
    shutil.copyfile(temporary_archive, staging / paths.final_archive.name)
    _copy_receipt(receipt_path, staging / paths.final_receipt.name)
    # First commit a complete staged pair. A later crash is diagnosable and is
    # never treated as a finished archive that can be silently overwritten.
    commit()
    os.replace(staging / paths.final_archive.name, paths.final_archive)
    os.replace(staging / paths.final_receipt.name, paths.final_receipt)
    staging.rmdir()
    native.validate_completed_seed(paths.final_receipt)
    shutil.rmtree(paths.progress, ignore_errors=True)
    commit()
    return receipt


def _native_continuation_transcript(paths: VolumePaths, recovery: Mapping[str, Any]) -> dict:
    from reference.runtimes.claude_recovery import file_sha256
    relative = Path(recovery["diagnostic_path"])
    if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "diagnostics":
        raise NativeSeedIngestionError("Invalid native recovery diagnostic path")
    path = paths.root / relative
    if file_sha256(path) != recovery["diagnostic_sha256"]:
        raise NativeSeedIngestionError("Native recovery diagnostic changed")
    evidence = json.loads(path.read_text())
    result = evidence["result"]
    if (evidence["source_id"] != recovery["source_id"]
            or evidence["content_sha256"] != recovery["content_sha256"]
            or result["status_code"] != 429 or result["ok"]
            or result["session_id"] != recovery["session_id"]):
        raise NativeSeedIngestionError("Native recovery is not the inspected quota interruption")
    transcripts = evidence["transcripts"]
    if len(transcripts) != 1:
        raise NativeSeedIngestionError("Native continuation requires one saved transcript")
    transcript = transcripts[0]
    relative = Path(transcript["path"])
    if (relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "projects"
            or relative.name != recovery["session_id"] + ".jsonl"):
        raise NativeSeedIngestionError("Invalid native continuation transcript path")
    return transcript


def run_seed(
    *, plan: NativeSeedPlan, volume_root: Path, runtime_root: Path,
    commit: Callable[[], None], invoke: Callable[..., ClaudeResult] = run_claude,
    environment: Mapping[str, str] | None = None,
    recovery: Mapping[str, Any] | None = None,
    run_budget_seconds: float = 22 * 3600,
) -> dict[str, Any]:
    """Run or resume the seed. The caller owns approval and the Modal lifetime."""
    if run_budget_seconds <= 0:
        raise ValueError("run_budget_seconds must be positive")
    deadline = time.monotonic() + run_budget_seconds
    paths = VolumePaths(volume_root.resolve())
    if recovery is not None and recovery.get("action") == "continue":
        saved_config = Path(json.loads(paths.receipt.read_text())["config_dir"])
        if (str(saved_config) != recovery["config_dir"] or saved_config.name != "claude-config"
                or saved_config.parent.parent != Path("/tmp")
                or not saved_config.parent.name.startswith("dolphinbench-claude-native-")):
            raise NativeSeedIngestionError("Native continuation must retain its original config path")
        runtime_root = saved_config.parent
    config_dir, receipt_path, completed = _restore_or_initialize(
        plan=plan, paths=paths, runtime_root=runtime_root,
        recovery=recovery, commit=commit,
    )
    if completed:
        receipt = native.validate_completed_seed(receipt_path)
        return {"status": "completed", "completed_sources": plan.source_count, "receipt": receipt}
    native._verify_claude_code_version([str(REMOTE_CLAUDE)])
    explicit_env = {"CLAUDE_CODE_OAUTH_TOKEN": str((environment or {}).get("CLAUDE_CODE_OAUTH_TOKEN") or "")}
    if not explicit_env["CLAUDE_CODE_OAUTH_TOKEN"]:
        raise NativeSeedIngestionError("CLAUDE_CODE_OAUTH_TOKEN is required in the Modal secret")
    identity = {}
    if invoke is run_claude:
        # The controller reads the corpus; Claude can read only its own files.
        runtime_root.chmod(0o711)
        paths.root.chmod(0o700)
        config_dir.chmod(0o700)
        explicit_env["HOME"] = "/home/claude-native"
        for path in (config_dir, *config_dir.rglob("*"), Path(native.FIXED_PROJECT_DIR)):
            os.chown(path, 2001, 2001, follow_symlinks=False)
        identity = {"run_as_user": 2001, "run_as_group": 2001}
    while True:
        pending = native.pending_source_messages(corpus_path=plan.corpus_path, receipt_path=receipt_path)
        if not pending:
            receipt = _publish_final(
                plan=plan, paths=paths, config_dir=config_dir,
                receipt_path=receipt_path, commit=commit,
            )
            return {"status": "completed", "completed_sources": plan.source_count, "receipt": receipt}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"status": "in_progress", "completed_sources": plan.source_count - len(pending)}
        source = pending[0]
        continuing = bool(recovery and recovery.get("action") == "continue"
                          and recovery["source_id"] == source.source_id)
        _checkpoint_before_call(paths=paths, source=source, commit=commit)
        started = time.monotonic()
        try:
            result = invoke(
                ("<system-reminder>Continue the interrupted response to the last user request.</system-reminder>"
                 if continuing else source.content),
                narrative_time=None if continuing else source.timestamp,
                **({"recovery_session_id": recovery["session_id"]} if continuing else {}),
                timeout=remaining,
                claude_command=[str(REMOTE_CLAUDE)],
                env=explicit_env,
                claude_config_dir=config_dir,
                mcp_config_path=None,
                allowed_mcp_tools=(),
                cwd=native.FIXED_PROJECT_DIR,
                model=plan.model,
                native_memory_mode="seed",
                native_memory_dir=native.native_memory_dir(config_dir),
                persist_session=True,
                **identity,
            )
        except Exception:
            result = ClaudeResult(ok=False, session_id=None, response_text="", error=traceback.format_exc())
        elapsed_seconds = time.monotonic() - started
        if not result.ok:
            _save_failure_evidence(paths=paths, source=source, result=result, config_dir=config_dir,
                                   token=explicit_env["CLAUDE_CODE_OAUTH_TOKEN"], commit=commit)
            rate_limited = result.status_code == 429
            pause = {
                "reason": "claude_usage_limit", "status_code": 429,
                "reset_at": result.rate_limit_reset_at,
                "source_id": source.source_id, "content_sha256": source.content_sha256,
                "requires_recovery_review": True,
            } if rate_limited else None
            telemetry = _safe_attempt_telemetry(
                source=source,
                result=result,
                elapsed_seconds=elapsed_seconds,
                outcome="rate_limited" if rate_limited else "ambiguous_failure",
            )
            _checkpoint_failed_call(
                plan=plan, paths=paths, receipt_path=receipt_path, source=source,
                telemetry=telemetry, config_dir=config_dir, pause=pause, commit=commit,
            )
            if rate_limited:
                return {
                    "status": "rate_limited",
                    "completed_sources": plan.source_count - len(pending),
                    "next_source_id": source.source_id,
                    "pause": pause,
                }
            raise NativeSeedIngestionError(
                f"Claude did not complete {source.source_id}; the in-flight marker remains"
            )
        telemetry = _safe_attempt_telemetry(
            source=source, result=result, elapsed_seconds=elapsed_seconds, outcome="completed",
        )
        try:
            _checkpoint_after_call(
                plan=plan, paths=paths, config_dir=config_dir,
                receipt_path=receipt_path, source=source, telemetry=telemetry, commit=commit,
            )
        except Exception:
            _save_failure_evidence(paths=paths, source=source, result=result, config_dir=config_dir,
                                   token=explicit_env["CLAUDE_CODE_OAUTH_TOKEN"], commit=commit,
                                   controller_error=traceback.format_exc())
            raise
        shutil.rmtree(config_dir / "projects", ignore_errors=True)


def check_remote_auth(
    *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Check only the Claude CLI version and auth mode; never return credentials."""
    version_result = runner(
        [str(REMOTE_CLAUDE), "--version"], text=True, capture_output=True, timeout=30, check=False,
    )
    version_match = _VERSION.search(version_result.stdout or "")
    version = version_match.group(1) if version_result.returncode == 0 and version_match else None
    auth_result = runner(
        [str(REMOTE_CLAUDE), "auth", "status"], text=True, capture_output=True, timeout=30, check=False,
    )
    try:
        auth = json.loads(auth_result.stdout) if auth_result.returncode == 0 else {}
    except json.JSONDecodeError:
        auth = {}
    if not isinstance(auth, dict):
        auth = {}
    result = {
        "version": version,
        "loggedIn": auth.get("loggedIn"),
        "authMethod": auth.get("authMethod"),
        "apiProvider": auth.get("apiProvider"),
    }
    if result != {
        "version": native.REQUIRED_CLAUDE_CODE_VERSION,
        "loggedIn": True,
        "authMethod": "oauth_token",
        "apiProvider": "firstParty",
    }:
        raise NativeSeedIngestionError("Claude native seed image has the wrong version or OAuth authentication mode")
    return result


if modal is not None:
    from reference.execution.images import claude_binary

    app = modal.App("dolphinbench-claude-native-ingestion")
    seed_volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
    evaluation_secret = modal.Secret.from_name(
        SECRET_NAME, required_keys=[native.TOKEN_KEY],
    )
    image = (
        modal.Image.debian_slim(python_version="3.11")
        .pip_install("pyyaml")
        .add_local_file(str(DEFAULT_CORPUS), str(REMOTE_CORPUS), copy=True)
        .add_local_file(str(claude_binary()), str(REMOTE_CLAUDE), copy=True)
        .add_local_dir(str(ROOT / "harness"), str(REMOTE_ROOT / "harness"), copy=True,
                       ignore=["test_*.py", "**/__pycache__/**", "**/*.pyc"])
        .add_local_dir(str(ROOT / "reference"), str(REMOTE_ROOT / "reference"), copy=True,
                       ignore=["tests/**", "**/__pycache__/**", "**/*.pyc"])
        .run_commands(f"chmod 0755 {REMOTE_CLAUDE}", f"mkdir -p {REMOTE_ROOT}/harness {REMOTE_ROOT}/reference",
                      "useradd --uid 2001 --user-group --create-home claude-native",
                      f"chmod 0600 {REMOTE_CORPUS}; mkdir -p {native.FIXED_PROJECT_DIR}; chown 2001:2001 {native.FIXED_PROJECT_DIR}")
        .env({native.PERSONA_ENV: native.PERSONA, native.ACCOUNT_ENV: native.ACCOUNT,
              "PYTHONPATH": str(REMOTE_ROOT), "HOME": "/home/ubuntu", "PATH": "/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin"})
    )

    @app.function(
        image=image, cpu=2, memory=4096, timeout=86_400, max_containers=1,
        secrets=[evaluation_secret], volumes={str(VOLUME_ROOT): seed_volume},
    )
    def auth_check() -> dict[str, Any]:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ[native.TOKEN_KEY]
        return check_remote_auth()

    @app.function(
        image=image, cpu=2, memory=4096, timeout=86_400, max_containers=1,
        nonpreemptible=True,
        secrets=[evaluation_secret], volumes={str(VOLUME_ROOT): seed_volume},
    )
    def ingest(confirm_paid_calls: bool = False, recovery: dict | None = None) -> dict[str, Any]:
        if not confirm_paid_calls:
            raise PaidCallApprovalRequired("Claude native seed requires confirm_paid_calls=True")
        plan = build_plan(corpus_path=REMOTE_CORPUS, volume_root=VOLUME_ROOT)
        with tempfile.TemporaryDirectory(prefix="dolphinbench-claude-native-") as raw:
            return run_seed(
                plan=plan,
                volume_root=VOLUME_ROOT,
                runtime_root=Path(raw),
                commit=seed_volume.commit,
                environment={"CLAUDE_CODE_OAUTH_TOKEN": os.environ.get(native.TOKEN_KEY, "")},
                recovery=recovery,
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan",))
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build_plan(corpus_path=args.corpus).as_dict(), indent=2, sort_keys=True))
    except (NativeSeedIngestionError, native.NativeMemoryError) as exc:
        parser.error(str(exc))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
