"""Reference modal implementation."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


from reference import evaluate as matrix
from reference import evaluate as local_suite


WORKSPACE = "workspace"
REMOTE_WORK_ROOT = Path("/tmp/dolphinbench-job")
REMOTE_WORKSPACE = REMOTE_WORK_ROOT / WORKSPACE
HERMES_ROOT = Path("/home/ubuntu/.hermes/hermes-agent")
BUNDLE_VERSION = 1
STATE_VERSION = 1
FINAL_EVALUATION_SUITE_VERSION = 1
LUNA_RESOURCE = "azure-luna"
SOL_JUDGE_RESOURCE = "azure-sol-judge"
MEMORY_RESOURCE_PREFIX = "memory:"
PRIVATE_NETWORK_MEMORY_PROVIDERS = frozenset({"honcho"})
MODAL_MEMORY_PROVIDERS = frozenset({"hindsight", "supermemory"})
MEMORY_PROVIDERS = frozenset({
    "builtin", "mem0", *PRIVATE_NETWORK_MEMORY_PROVIDERS, *MODAL_MEMORY_PROVIDERS,
})
WAITING = frozenset({"waiting_rate_limit", "waiting_subscription", "waiting_worker_timeout"})
TERMINAL = frozenset({"completed", "failed"})
SECRET_NAMES = frozenset({".env", "auth.json", "credentials.json", "tokens.json"})
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ModalSuiteError(RuntimeError):
    """Raised when local Modal orchestration input or state is invalid."""


class ModalBridge(Protocol):
    """Small boundary around Modal; tests provide an in-memory implementation."""

    def upload_bundle(self, local_path: Path, remote_name: str) -> None: ...

    def upload_profile(self, local_path: Path, remote_name: str) -> None: ...

    def upload_native_memory(self, local_path: Path, remote_name: str) -> None: ...

    def spawn(
        self,
        *,
        bundle_name: str,
        job_id: str,
        manifest_relative_path: str,
        action: str,
        test_ids: list[str],
        profile_archive_name: str | None,
        native_memory_archive_name: str | None,
        memory_provider: str,
        agent_provider: str,
        agent_runtime: str,
    ) -> str: ...

    def poll(self, call_id: str, job_id: str) -> dict[str, Any]: ...

    def read_result_file(self, job_id: str, relative_path: str) -> bytes: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ModalSuiteError(f"{label} does not exist: {path}")
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ModalSuiteError(f"{label} is not valid JSON: {path}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ModalSuiteError(f"{label} must be a JSON object: {path}")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ModalSuiteError(f"{label} must be an integer greater than zero")
    return value


def _is_luna_consumer(manifest: dict[str, Any]) -> bool:
    agent = manifest.get("agent") or {}
    if (
        agent.get("provider") == "azure-foundry"
        and agent.get("model") == "gpt-5.6-luna"
    ):
        return True
    if not any(job.get("configuration") == "honcho" for job in manifest.get("jobs") or []):
        return False
    internal_models = (
        ((manifest.get("runtime_provenance") or {}).get("honcho") or {})
        .get("internal_models")
        or {}
    )
    return any(model == "gpt-5.6-luna" for model in internal_models.values())


def _memory_provider_for_job(job: local_suite.SuiteJob) -> str:
    """Read the one memory provider recorded in the suite job's resource list."""
    providers = [
        resource.removeprefix(MEMORY_RESOURCE_PREFIX)
        for resource in job.resources
        if resource.startswith(MEMORY_RESOURCE_PREFIX)
    ]
    if len(providers) != 1 or providers[0] not in MEMORY_PROVIDERS:
        raise ModalSuiteError(
            f"{job.id}: suite job must contain exactly one supported memory:<provider> resource"
        )
    return providers[0]


def _agent_provider_for_job(job: local_suite.SuiteJob) -> str:
    providers = [
        resource.removeprefix("provider:")
        for resource in job.resources
        if resource.startswith("provider:")
    ]
    if len(providers) != 1 or not providers[0]:
        raise ModalSuiteError(
            f"{job.id}: suite job must contain exactly one provider:<name> resource"
        )
    return providers[0]


def _uses_sol_judge(manifest: dict[str, Any]) -> bool:
    judge = manifest.get("judge") or {}
    return judge.get("backend") == "azure" and judge.get("deployment") == "gpt-5.6-sol"


def _load_and_validate_suite(suite_path: Path, *, verify_sources: bool = True) -> tuple[local_suite.Suite, dict[str, Any], dict[str, dict[str, Any]]]:
    """Validate every input before building a bundle or contacting Modal."""
    try:
        suite = local_suite.load_suite(suite_path)
    except local_suite.SuiteError as exc:
        raise ModalSuiteError(str(exc)) from exc
    raw = _load_json(suite.path, "suite file")
    if raw.get("suite_version") != FINAL_EVALUATION_SUITE_VERSION:
        raise ModalSuiteError("suite was not created by the current final-evaluation planner")
    if raw.get("created_by") != "reference.plan":
        raise ModalSuiteError("suite was not created by reference.plan")
    purpose = raw.get("purpose")
    test_ids = raw.get("test_ids")
    expected_ids = (
        [f"{index:03d}" for index in range(1, 201)]
        if purpose in {"partial_final_results", "final_results"}
        else test_ids if purpose == "integration_smoke" and isinstance(test_ids, list) and 1 <= len(test_ids) <= 10
        else None
    )
    if test_ids != expected_ids:
        raise ModalSuiteError(
            "suite must contain between 1 and 10 tests for integration_smoke, or tests "
            "001 through 200 for partial_final_results or final_results"
        )
    test_hashes = raw.get("published_test_sha256")
    if not isinstance(test_hashes, dict) or set(test_hashes) != set(test_ids):
        raise ModalSuiteError("suite must record one published test hash for every test")
    if any(
        not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
        for digest in test_hashes.values()
    ):
        raise ModalSuiteError("suite contains an invalid published test hash")
    manifests: dict[str, dict[str, Any]] = {}
    for job in suite.jobs:
        if job.action != "test-only" or job.test_ids:
            raise ModalSuiteError(f"{job.id}: final evaluation suites run tests only")
        memory_provider = _memory_provider_for_job(job)
        try:
            manifest = matrix.load_manifest(job.manifest_path)
            if verify_sources:
                matrix.verify_manifest_hashes(manifest)
        except (matrix.PreparationError, matrix.ManifestDriftError) as exc:
            raise ModalSuiteError(f"{job.id}: {exc}") from exc
        if len(manifest.get("jobs") or []) != 1:
            raise ModalSuiteError(f"{job.id}: Modal suites require one matrix job per manifest")
        if manifest["jobs"][0].get("configuration") != memory_provider:
            raise ModalSuiteError(
                f"{job.id}: memory resource does not match the prepared manifest"
            )
        if (manifest.get("agent") or {}).get("provider") != _agent_provider_for_job(job):
            raise ModalSuiteError(
                f"{job.id}: agent provider resource does not match the prepared manifest"
            )
        scope = manifest.get("evaluation_scope") or {}
        if scope.get("kind") != "test_only" or scope.get("test_ids") != test_ids:
            raise ModalSuiteError(f"{job.id}: manifest test list does not match the suite")
        if _is_luna_consumer(manifest) and LUNA_RESOURCE not in job.resources:
            raise ModalSuiteError(
                f"{job.id}: Luna agent or Honcho Luna work must reserve {LUNA_RESOURCE!r}"
            )
        if _uses_sol_judge(manifest) and SOL_JUDGE_RESOURCE not in job.resources:
            raise ModalSuiteError(
                f"{job.id}: Sol grading must reserve {SOL_JUDGE_RESOURCE!r} separately from Luna"
            )
        manifests[job.id] = manifest
    if any(_is_luna_consumer(item) for item in manifests.values()):
        if LUNA_RESOURCE not in suite.resource_limits:
            raise ModalSuiteError(f"suite has Luna consumers but no {LUNA_RESOURCE!r} limit")
    return suite, raw, manifests


def _luna_policy(raw_suite: dict[str, Any], suite: local_suite.Suite) -> dict[str, Any]:
    supplied = raw_suite.get("azure_luna") or {}
    if not isinstance(supplied, dict):
        raise ModalSuiteError("azure_luna must be a JSON object when present")
    has_luna = LUNA_RESOURCE in suite.resource_limits
    clean_interval = supplied.get("clean_interval_seconds", 900)
    retry_backoff = supplied.get("retry_backoff_seconds", 60)
    if not has_luna:
        if supplied:
            raise ModalSuiteError("azure_luna settings require at least one Luna-consuming job")
        return {"enabled": False}
    if "initial_concurrency" in supplied:
        raise ModalSuiteError(
            "azure_luna.initial_concurrency is not supported; set the azure-luna resource limit"
        )
    clean_interval = _positive_int(clean_interval, "azure_luna.clean_interval_seconds")
    retry_backoff = _positive_int(retry_backoff, "azure_luna.retry_backoff_seconds")
    concurrency = suite.resource_limits[LUNA_RESOURCE]
    return {
        "enabled": True,
        "initial_concurrency": concurrency,
        "current_concurrency": concurrency,
        "maximum_concurrency": concurrency,
        "clean_interval_seconds": clean_interval,
        "retry_backoff_seconds": retry_backoff,
        "last_429_at": None,
        "admissions_paused": False,
    }


def _inside(path: Path, root: Path) -> Path | None:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return None


def _contains_secret_path(path: Path) -> bool:
    return any(part.lower() in SECRET_NAMES for part in path.parts)


def _copy_file(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise ModalSuiteError(f"required bundle input is missing: {source}")
    if _contains_secret_path(source):
        raise ModalSuiteError(
            f"prepared manifest includes a secret-bearing file path: {source}; rebuild the manifest without it"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if _sha256(destination) != _sha256(source):
            raise ModalSuiteError(f"two bundle inputs map to different content: {destination}")
        return
    shutil.copy2(source, destination)


def _copy_runtime_support(root: Path, destination: Path) -> None:
    """Copy small Python runtime packages, never broad history or local stores."""
    for directory_name in ("harness", "graders", "mock_mcp", "reference"):
        source = root / directory_name
        if not source.is_dir():
            raise ModalSuiteError(f"required runtime directory is missing: {source}")
        for path in source.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                _copy_file(path, destination / directory_name / path.relative_to(source))
    construction = root / "construction"
    for path in construction.glob("*.py"):
        _copy_file(path, destination / "construction" / path.name)


def _path_mapper(manifest: dict[str, Any], job_id: str) -> tuple[dict[Path, Path], dict[Path, Path]]:
    root = Path(str(manifest["root"])).resolve()
    output_root = Path(str(manifest["output_root"])).resolve()
    bases: dict[Path, Path] = {
        output_root: REMOTE_WORKSPACE / "output",
        root: REMOTE_WORKSPACE / "repo",
    }
    external_roots: dict[Path, Path] = {}
    source_trees = (manifest.get("hashes") or {}).get("source_trees") or {}
    for index, (name, source_tree) in enumerate(sorted(source_trees.items()), start=1):
        source = Path(str(source_tree.get("root") or "")).resolve()
        if not source.is_dir():
            raise ModalSuiteError(f"{job_id}: source tree is missing: {source}")
        if _inside(source, root) is None:
            external_roots[source] = REMOTE_WORKSPACE / "external" / f"{index:02d}-{name}"
    bases.update(external_roots)
    return bases, external_roots


def _map_path(path: Path, bases: dict[Path, Path]) -> Path:
    path = path.resolve()
    candidates = sorted(bases, key=lambda item: len(str(item)), reverse=True)
    for source in candidates:
        relative = _inside(path, source)
        if relative is not None:
            return bases[source] / relative
    digest = hashlib.sha256(str(path).encode()).hexdigest()[:16]
    return REMOTE_WORKSPACE / "external" / "files" / digest / path.name


def _bundle_member_path(remote_path: Path) -> Path:
    """Return the archive path for one absolute path used by the worker."""
    try:
        return remote_path.relative_to(REMOTE_WORK_ROOT)
    except ValueError as exc:
        raise ModalSuiteError(
            f"rewritten path is outside the worker directory: {remote_path}"
        ) from exc


def _rewrite_paths(value: Any, bases: dict[Path, Path]) -> Any:
    if isinstance(value, str):
        candidate = Path(value)
        if candidate.is_absolute():
            return str(_map_path(candidate, bases))
        return value
    if isinstance(value, list):
        return [_rewrite_paths(item, bases) for item in value]
    if isinstance(value, dict):
        rewritten: dict[Any, Any] = {}
        for key, item in value.items():
            if key == "fixed_project_dir":
                # This is a runtime identity, not a bundled input file.
                rewritten[key] = item
                continue
            new_key = str(_map_path(Path(key), bases)) if isinstance(key, str) and Path(key).is_absolute() else key
            rewritten[new_key] = _rewrite_paths(item, bases)
        return rewritten
    return value


def _refresh_relocated_source_tree_hashes(manifest: dict[str, Any]) -> None:
    """Recompute tree hashes after absolute source paths move into the bundle."""
    source_trees = (manifest.get("hashes") or {}).get("source_trees") or {}
    for tree in source_trees.values():
        files = tree.get("files") or {}
        digest = hashlib.sha256()
        for name, value in sorted(files.items()):
            digest.update(name.encode())
            digest.update(b"\0")
            digest.update(value.encode())
            digest.update(b"\0")
        tree["sha256"] = digest.hexdigest()


def _copy_manifest_inputs(
    manifest: dict[str, Any],
    bundle_root: Path,
    bases: dict[Path, Path],
) -> None:
    local_only_seed_evidence = {
        Path(path).resolve()
        for job in manifest.get("jobs") or []
        for path in job.get("seed_result_paths") or []
    }
    files = (manifest.get("hashes") or {}).get("files") or {}
    source_trees = (manifest.get("hashes") or {}).get("source_trees") or {}
    for raw_path in files:
        source = Path(raw_path)
        if source.resolve() in local_only_seed_evidence:
            continue
        remote_path = _map_path(source, bases)
        _copy_file(source, bundle_root / _bundle_member_path(remote_path))
    for tree in source_trees.values():
        source_root = Path(str(tree.get("root") or "")).resolve()
        remote_root = _map_path(source_root, bases)
        for raw_path in (tree.get("files") or {}):
            source = Path(raw_path).resolve()
            try:
                relative = source.relative_to(source_root)
            except ValueError:
                remote_path = _map_path(source, bases)
            else:
                remote_path = remote_root / relative
            _copy_file(source, bundle_root / _bundle_member_path(remote_path))


def _preserve_relocated_source_tree_layouts(
    original: dict[str, Any],
    rewritten: dict[str, Any],
    bases: dict[Path, Path],
) -> None:
    """Keep every recorded tree complete when recorded trees overlap."""
    original_trees = (original.get("hashes") or {}).get("source_trees") or {}
    rewritten_trees = (rewritten.get("hashes") or {}).get("source_trees") or {}
    for name, original_tree in original_trees.items():
        source_root = Path(str(original_tree.get("root") or "")).resolve()
        remote_root = _map_path(source_root, bases)
        rewritten_tree = rewritten_trees[name]
        rewritten_tree["root"] = str(remote_root)
        rewritten_files: dict[str, str] = {}
        for raw_path, digest in (original_tree.get("files") or {}).items():
            source = Path(raw_path).resolve()
            try:
                remote_path = remote_root / source.relative_to(source_root)
            except ValueError:
                remote_path = _map_path(source, bases)
            rewritten_files[str(remote_path)] = digest
        rewritten_tree["files"] = rewritten_files
        rewritten_tree["relocated_for_bundle"] = True


def build_bundle(
    suite: local_suite.Suite,
    manifests: dict[str, dict[str, Any]],
    destination_dir: Path,
) -> dict[str, Any]:
    """Create one deterministic, self-contained archive for every suite job."""
    destination_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dolphinbench-modal-bundle-") as temporary:
        root = Path(temporary) / WORKSPACE
        bundle_repo = root / "repo"
        first_manifest = next(iter(manifests.values()))
        _copy_runtime_support(Path(str(first_manifest["root"])), bundle_repo)
        rewritten_paths: dict[str, str] = {}
        for job in suite.jobs:
            manifest = manifests[job.id]
            bases, _external = _path_mapper(manifest, job.id)
            source_root = Path(str(manifest["root"])).resolve()
            if source_root != Path(str(first_manifest["root"])).resolve():
                raise ModalSuiteError("all Modal suite manifests must use the same benchmark repository")
            _copy_manifest_inputs(manifest, Path(temporary), bases)
            rewritten = _rewrite_paths(manifest, bases)
            local_only_seed_evidence = {
                Path(path).resolve()
                for matrix_job in manifest.get("jobs") or []
                for path in matrix_job.get("seed_result_paths") or []
            }
            rewritten_files = (rewritten.get("hashes") or {}).get("files") or {}
            for source in local_only_seed_evidence:
                rewritten_files.pop(str(_map_path(source, bases)), None)
            for matrix_job in rewritten.get("jobs") or []:
                matrix_job["seed_result_paths"] = []
            rewritten["bundle_exclusions"] = {
                "seed_result_files": "verified locally before launch; not read by test-only workers"
            }
            _preserve_relocated_source_tree_layouts(manifest, rewritten, bases)
            _refresh_relocated_source_tree_hashes(rewritten)
            launch = rewritten.get("launch") or {}
            # The image packages these executables. They are not copied from a
            # local virtualenv, which could be host-specific or contain secrets.
            launch["hermes_bin"] = str(HERMES_ROOT / "venv" / "bin" / "hermes")
            launch["hermes_python"] = str(HERMES_ROOT / "venv" / "bin" / "python")
            launch["mock_mcp_python"] = sys.executable
            rewritten["launch"] = launch
            relative = Path(WORKSPACE) / "manifests" / f"{job.id}.json"
            target = Path(temporary) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(rewritten, indent=2, sort_keys=True) + "\n")
            rewritten_paths[job.id] = str(relative)
        metadata = {
            "bundle_version": BUNDLE_VERSION,
            "suite_sha256": suite.digest,
            "manifests": rewritten_paths,
        }
        metadata_path = root / "bundle.json"
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        archive_unhashed = destination_dir / f"{suite.path.stem}-{suite.digest[:16]}.tar.gz"
        with tarfile.open(archive_unhashed, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            archive.add(root, arcname=WORKSPACE, recursive=True)
        digest = _sha256(archive_unhashed)
        archive = destination_dir / f"dolphinbench-{suite.digest[:16]}-{digest[:16]}.tar.gz"
        os.replace(archive_unhashed, archive)
    return {
        "path": str(archive.resolve()),
        "sha256": digest,
        "name": archive.name,
        "manifest_relative_paths": rewritten_paths,
    }


def _profile_archive_filter(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(member.name).parts
    if any(part.lower() in SECRET_NAMES for part in parts):
        return None
    if member.issym() or member.islnk():
        raise ModalSuiteError(f"Hermes profile contains a link: {member.name}")
    member.uid = 0
    member.gid = 0
    member.uname = ""
    member.gname = ""
    return member


def build_profile_archives(
    suite: local_suite.Suite,
    manifests: dict[str, dict[str, Any]],
    destination_dir: Path,
    *,
    profiles_root: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Archive each completed Hermes profile once without credential files."""
    from reference.runtimes.hermes import profile_config_hashes

    profiles_root = profiles_root or (Path.home() / ".hermes" / "profiles")
    destination_dir.mkdir(parents=True, exist_ok=True)
    by_profile: dict[str, dict[str, Any]] = {}
    by_job: dict[str, dict[str, Any]] = {}
    for suite_job in suite.jobs:
        manifest = manifests[suite_job.id]
        matrix_jobs = manifest.get("jobs") or []
        if len(matrix_jobs) != 1:
            raise ModalSuiteError(f"{suite_job.id}: expected one matrix job")
        matrix_job = matrix_jobs[0]
        profile = matrix_job.get("profile")
        if not isinstance(profile, str) or not profile:
            continue
        if not SAFE_NAME.fullmatch(profile):
            raise ModalSuiteError(f"{suite_job.id}: invalid Hermes profile name: {profile!r}")
        source = profiles_root / profile
        if not source.is_dir():
            raise ModalSuiteError(f"{suite_job.id}: completed Hermes profile is missing: {source}")
        expected_hashes = matrix_job.get("seed_profile_config_hashes")
        if not isinstance(expected_hashes, dict):
            raise ModalSuiteError(
                f"{suite_job.id}: manifest has no completed profile configuration hashes"
            )
        actual_hashes = profile_config_hashes(source)
        if not matrix._profile_hashes_match(expected_hashes, actual_hashes):
            raise ModalSuiteError(
                f"{suite_job.id}: completed Hermes profile configuration changed after preparation"
            )
        existing = by_profile.get(profile)
        if existing is None:
            temporary = destination_dir / f".{profile}.{uuid.uuid4().hex}.tar.gz"
            try:
                with tarfile.open(temporary, "w:gz", format=tarfile.PAX_FORMAT) as archive:
                    archive.add(
                        source,
                        arcname=profile,
                        recursive=True,
                        filter=_profile_archive_filter,
                    )
                digest = _sha256(temporary)
                target = destination_dir / f"profile-{profile}-{digest[:16]}.tar.gz"
                if target.exists():
                    if _sha256(target) != digest:
                        raise ModalSuiteError(f"profile archive name collision: {target}")
                    temporary.unlink()
                else:
                    os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            existing = {
                "profile": profile,
                "path": str(target.resolve()),
                "name": target.name,
                "sha256": digest,
                "bytes": target.stat().st_size,
                "config_hashes": actual_hashes,
            }
            by_profile[profile] = existing
        by_job[suite_job.id] = existing
    return by_job


def build_native_memory_archives(
    suite: local_suite.Suite,
    manifests: dict[str, dict[str, Any]],
    destination_dir: Path,
) -> dict[str, dict[str, Any]]:
    """Copy each verified Claude native-memory archive without repacking it."""
    from reference.runtimes import claude_code as native

    destination_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, dict[str, Any]] = {}
    for suite_job in suite.jobs:
        jobs = manifests[suite_job.id].get("jobs") or []
        if len(jobs) != 1:
            raise ModalSuiteError(f"{suite_job.id}: expected one matrix job")
        job = jobs[0]
        if job.get("provider") != "claude_native_memory":
            continue
        native_input = job.get("native_memory")
        if not isinstance(native_input, dict):
            raise ModalSuiteError(f"{suite_job.id}: Claude native-memory job has no archive metadata")
        source = Path(str(native_input.get("archive_path") or "")).resolve()
        expected = native_input.get("archive_sha256")
        if not source.is_file() or not isinstance(expected, str) or _sha256(source) != expected:
            raise ModalSuiteError(f"{suite_job.id}: Claude native-memory archive is missing or changed")
        native.validate_snapshot(source)
        target = destination_dir / f"claude-native-{expected[:16]}.tar.gz"
        if target.exists() and _sha256(target) != expected:
            raise ModalSuiteError(f"{suite_job.id}: Claude native-memory archive name collision")
        if not target.exists():
            shutil.copyfile(source, target)
        result[suite_job.id] = {
            "path": str(target.resolve()),
            "name": target.name,
            "sha256": expected,
            "bytes": target.stat().st_size,
        }
    return result


class _RealModalBridge:
    def __init__(self, *, agent_providers: frozenset[str] = frozenset(),
                 runtimes: frozenset[str] = frozenset()) -> None:
        try:
            import modal
            from reference.execution import worker as worker
        except ImportError as exc:  # pragma: no cover - depends on the operator environment.
            raise ModalSuiteError(
                "Modal is not installed for this Python. Use the interpreter behind the modal CLI."
            ) from exc
        self.modal = modal
        self.worker = worker
        self.app, self.functions = worker.create_worker_app(runtimes)
        self.input_volume = modal.Volume.from_name(worker.INPUT_VOLUME_NAME, create_if_missing=True)
        self.results_volume = modal.Volume.from_name(worker.RESULTS_VOLUME_NAME, create_if_missing=True)
        self.openrouter_workers: dict[str, Any] = {}
        if "openrouter" in agent_providers:
            self.openrouter_workers = {
                "check": self.functions["openrouter_check"],
                "ordinary": self.functions["hermes", "run_openrouter_prepared_job"],
                "private": self.functions["hermes", "run_openrouter_private_network_job"],
                "modal": self.functions["hermes", "run_openrouter_modal_memory_job"],
            }
        self._app_context: Any | None = None

    def __enter__(self) -> "_RealModalBridge":
        self._app_context = self.app.run(detach=True)
        self._app_context.__enter__()
        if getattr(self, "openrouter_workers", {}):
            present = self.openrouter_workers["check"].remote()
            required = {"OPENROUTER_API_KEY", "MINIMAX_OPENROUTER_API_KEY"}
            if not isinstance(present, dict) or not all(present.get(key) for key in required):
                raise ModalSuiteError(
                    "OpenRouter evaluation worker did not receive both required key names"
                )
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        if self._app_context is None:
            return None
        try:
            return self._app_context.__exit__(exc_type, exc, traceback)
        finally:
            self._app_context = None

    def upload_bundle(self, local_path: Path, remote_name: str) -> None:
        async def upload() -> None:
            async with self.input_volume.batch_upload() as batch:
                batch.put_file(local_path, remote_name)
        asyncio.run(upload())

    def upload_profile(self, local_path: Path, remote_name: str) -> None:
        self.upload_bundle(local_path, remote_name)

    def upload_native_memory(self, local_path: Path, remote_name: str) -> None:
        try:
            self.upload_bundle(local_path, remote_name)
        except FileExistsError:
            remote_hash = hashlib.sha256()
            for chunk in self.input_volume.read_file(remote_name):
                remote_hash.update(chunk)
            if remote_hash.hexdigest() != hashlib.sha256(local_path.read_bytes()).hexdigest():
                raise ModalSuiteError(f"existing native memory archive differs: {remote_name}")

    def spawn(
        self,
        *,
        memory_provider: str,
        agent_provider: str = "azure-foundry",
        agent_runtime: str = "hermes",
        **kwargs: Any,
    ) -> str:
        if agent_provider == "openrouter":
            if agent_runtime != "hermes":
                raise ModalSuiteError("OpenRouter workers require a Hermes manifest")
            if not self.openrouter_workers:
                raise ModalSuiteError("OpenRouter workers were not registered for this suite")
            if memory_provider in PRIVATE_NETWORK_MEMORY_PROVIDERS:
                function = self.openrouter_workers["private"]
            elif memory_provider in MODAL_MEMORY_PROVIDERS:
                function = self.openrouter_workers["modal"]
            elif memory_provider in {"builtin", "mem0"}:
                function = self.openrouter_workers["ordinary"]
            else:
                raise ModalSuiteError(
                    f"unsupported memory provider for OpenRouter worker: {memory_provider}"
                )
        elif kwargs.get("action") in {"seed", "resume-seed"} and memory_provider in {"builtin", "mem0", "honcho"}:
            name = "run_private_ingestion_job" if memory_provider == "honcho" else "run_ingestion_job"
            function = self.functions[agent_runtime, name]
        elif memory_provider in PRIVATE_NETWORK_MEMORY_PROVIDERS:
            function = self.functions[agent_runtime, "run_private_network_job"]
        elif memory_provider in MODAL_MEMORY_PROVIDERS:
            function = self.functions[agent_runtime, "run_modal_memory_job"]
        elif memory_provider in {"builtin", "mem0"}:
            function = self.functions[agent_runtime, "run_prepared_job"]
        else:
            raise ModalSuiteError(f"unsupported memory provider for Modal worker: {memory_provider}")
        call = function.spawn(**kwargs)
        return str(call.object_id)

    def poll(self, call_id: str, job_id: str) -> dict[str, Any]:
        status = self.functions["status"].remote(job_id)
        try:
            result = self.modal.FunctionCall.from_id(call_id).get(timeout=0)
        except TimeoutError:
            return status
        except Exception as exc:
            # A completed Modal call can raise when the worker itself exceeded
            # its 24-hour ceiling. The durable worker status tells the
            # scheduler whether this was an ordinary failure or a resumable
            # timeout.
            return {
                "status": "running",
                "worker_ended": True,
                "worker_error": str(exc),
            }
        if isinstance(result, dict):
            return {**status, **result}
        return status

    def read_result_file(self, job_id: str, relative_path: str) -> bytes:
        remote_path = f"{job_id}/{relative_path}"
        return b"".join(self.results_volume.read_file(remote_path))


def _initial_state(
    suite: local_suite.Suite,
    bundle: dict[str, Any],
    luna: dict[str, Any],
    profile_archives: dict[str, dict[str, Any]],
    native_memory_archives: dict[str, dict[str, Any]],
    test_ids: list[str],
) -> dict[str, Any]:
    launch_id = uuid.uuid4().hex
    return {
        "version": STATE_VERSION,
        "launch_id": launch_id,
        "suite_path": str(suite.path),
        "suite_sha256": suite.digest,
        "bundle": bundle,
        "profile_archives": profile_archives,
        "native_memory_archives": native_memory_archives,
        "luna": luna,
        "status": "ready",
        "started_at": _now(),
        "updated_at": _now(),
        "jobs": {
            job.id: {
                "status": "pending",
                "remote_job_id": f"{launch_id}-{job.id}",
                "attempts": 0,
                "manifest": str(job.manifest_path),
                "manifest_relative_path": bundle["manifest_relative_paths"][job.id],
                "action": job.action,
                "resources": list(job.resources),
                "test_ids": list(test_ids),
                "profile_archive_name": (
                    profile_archives.get(job.id) or {}
                ).get("name"),
                "native_memory_archive_name": (
                    native_memory_archives.get(job.id) or {}
                ).get("name"),
            }
            for job in suite.jobs
        },
    }


def _load_state(path: Path, suite: local_suite.Suite) -> dict[str, Any]:
    state = _load_json(path, "Modal suite state")
    if state.get("version") != STATE_VERSION:
        raise ModalSuiteError(f"unsupported Modal suite state version: {path}")
    if state.get("suite_sha256") != suite.digest:
        raise ModalSuiteError("suite file changed after the Modal state was created; use a new state path")
    if set((state.get("jobs") or {})) != {job.id for job in suite.jobs}:
        raise ModalSuiteError("Modal suite state jobs do not match the suite file")
    return state


def _verify_saved_inputs(state: dict[str, Any], raw: dict[str, Any], manifests: dict[str, dict[str, Any]]) -> None:
    """Continue the saved run even when unrelated development files change."""
    bundle = state["bundle"]
    archive = Path(bundle["path"])
    if not archive.is_file() or _sha256(archive) != bundle["sha256"]:
        raise ModalSuiteError("the saved evaluation archive is missing or changed")
    if archive.name != bundle["name"]:
        raise ModalSuiteError("the saved evaluation archive name changed")
    with tarfile.open(archive, "r:gz") as saved:
        metadata = json.load(saved.extractfile("workspace/bundle.json"))
    if metadata["suite_sha256"] != state["suite_sha256"]:
        raise ModalSuiteError("the saved archive belongs to a different evaluation")
    for job_id, entry in state["jobs"].items():
        if entry["manifest_relative_path"] != metadata["manifests"][job_id]:
            raise ModalSuiteError(f"{job_id}: saved manifest path changed")
        if entry["test_ids"] != raw["test_ids"]:
            raise ModalSuiteError(f"{job_id}: saved test list changed")
    # Test edits require a new run, unlike edits to code outside the saved archive.
    checked: set[Path] = set()
    for manifest in manifests.values():
        root = Path(manifest["root"]) / "tests"
        for name, digest in (manifest.get("hashes") or {}).get("files", {}).items():
            path = Path(name)
            if path in checked or _inside(path, root) is None:
                continue
            checked.add(path)
            if raw["published_test_sha256"].get(path.stem) != digest or not path.is_file() or _sha256(path) != digest:
                raise ModalSuiteError(f"published test changed: {path}; prepare a new evaluation")


def _save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    _atomic_json(path, state)


def _recovery_action(action: str) -> str:
    if action == "seed":
        return "resume-seed"
    if action in {"launch", "test-only"}:
        return "resume"
    return action


def _occupied_resources(state: dict[str, Any]) -> dict[str, int]:
    occupied: dict[str, int] = {}
    for job in state["jobs"].values():
        if job.get("status") != "running":
            continue
        for resource in job.get("resources") or []:
            occupied[resource] = occupied.get(resource, 0) + 1
    return occupied


def _refresh_luna_capacity(state: dict[str, Any], now: float) -> None:
    luna = state.get("luna") or {}
    if not luna.get("enabled"):
        return
    last_429 = luna.get("last_429_unix")
    if isinstance(last_429, (int, float)):
        if now - last_429 < luna["clean_interval_seconds"]:
            luna["admissions_paused"] = True
            return
        luna["admissions_paused"] = False
def _admissible(job: local_suite.SuiteJob, state: dict[str, Any], suite: local_suite.Suite) -> bool:
    occupied = _occupied_resources(state)
    running_jobs = sum(
        entry.get("status") == "running"
        for entry in state["jobs"].values()
    )
    if running_jobs >= suite.global_limit:
        return False
    if any(occupied.get(resource, 0) >= suite.resource_limits[resource] for resource in job.resources):
        return False
    luna = state.get("luna") or {}
    if LUNA_RESOURCE in job.resources:
        if luna.get("admissions_paused"):
            return False
        if occupied.get(LUNA_RESOURCE, 0) >= luna.get("current_concurrency", 0):
            return False
    return True


def _apply_remote_status(
    state: dict[str, Any],
    job_id: str,
    remote: dict[str, Any],
    now: float,
) -> None:
    entry = state["jobs"][job_id]
    remote_status = str(remote.get("status") or "")
    if remote.get("rate_limited"):
        luna = state.get("luna") or {}
        if luna.get("enabled"):
            luna["last_429_unix"] = now
            luna["last_429_at"] = _now()
            luna["admissions_paused"] = True
    if remote_status in {"running", "not_started", ""}:
        if remote.get("worker_ended"):
            error = str(remote.get("worker_error") or "")
            if "timeout" in error.lower() or "timed out" in error.lower():
                entry["status"] = "waiting_worker_timeout"
                entry["remote_error"] = error
                entry["ended_at"] = _now()
            else:
                entry["status"] = "failed"
                entry["remote_error"] = error
                entry["ended_at"] = _now()
        return
    if remote_status in {
        "completed", "failed", "waiting_rate_limit", "waiting_subscription", "waiting_worker_timeout",
    }:
        entry["status"] = remote_status
        entry["returncode"] = remote.get("returncode")
        entry["remote_error"] = remote.get("error")
        entry["ended_at"] = _now()
        if remote_status == "waiting_rate_limit":
            entry["retry_not_before_unix"] = now + int((state.get("luna") or {}).get("retry_backoff_seconds", 60))


def refresh_status(
    suite: local_suite.Suite,
    state: dict[str, Any],
    bridge: ModalBridge,
    *,
    now: float | None = None,
) -> None:
    current = time.time() if now is None else now
    for job in suite.jobs:
        entry = state["jobs"][job.id]
        if entry.get("status") != "running":
            continue
        call_id = entry.get("modal_call_id")
        if not isinstance(call_id, str) or not call_id:
            entry["status"] = "failed"
            entry["remote_error"] = "running job has no Modal function call id"
            continue
        remote_job_id = str(entry.get("remote_job_id") or job.id)
        _apply_remote_status(
            state,
            job.id,
            bridge.poll(call_id, remote_job_id),
            current,
        )
    _refresh_luna_capacity(state, current)
    statuses = {entry.get("status") for entry in state["jobs"].values()}
    state["status"] = "completed" if statuses == {"completed"} else "active"


def _begin_waiting_jobs(state: dict[str, Any], *, resume_subscriptions: bool, now: float) -> None:
    for entry in state["jobs"].values():
        status = entry.get("status")
        if status == "waiting_rate_limit" and now >= float(entry.get("retry_not_before_unix", 0)):
            entry["status"] = "pending"
            entry["action"] = _recovery_action(str(entry["action"]))
        elif status == "waiting_subscription" and resume_subscriptions:
            entry["status"] = "pending"
            entry["action"] = _recovery_action(str(entry["action"]))
        elif status == "waiting_worker_timeout":
            entry["status"] = "pending"
            entry["action"] = _recovery_action(str(entry["action"]))


def admit_jobs(
    suite: local_suite.Suite,
    state: dict[str, Any],
    bridge: ModalBridge,
    state_path: Path,
) -> None:
    for job in suite.jobs:
        entry = state["jobs"][job.id]
        if entry.get("status") != "pending" or not _admissible(job, state, suite):
            continue
        action = str(entry["action"])
        # A remote submit has no local transaction. Save an explicit uncertain
        # state before submitting so an interrupted local process cannot
        # silently submit the same ingestion a second time.
        entry.update({
            "status": "starting",
            "last_action": action,
        })
        _save_state(state_path, state)
        try:
            call_id = bridge.spawn(
                bundle_name=state["bundle"]["name"],
                job_id=str(entry.get("remote_job_id") or job.id),
                manifest_relative_path=entry["manifest_relative_path"],
                action=action,
                test_ids=list(entry["test_ids"]),
                profile_archive_name=entry.get("profile_archive_name"),
                native_memory_archive_name=entry.get("native_memory_archive_name"),
                memory_provider=_memory_provider_for_job(job),
                agent_provider=_agent_provider_for_job(job),
                agent_runtime=matrix._manifest_agent_runtime(matrix.load_manifest(job.manifest_path)),
            )
        except Exception as exc:
            entry.update({
                "status": "needs_inspection",
                "remote_error": f"Modal submission outcome is unknown: {exc}",
                "ended_at": _now(),
            })
            _save_state(state_path, state)
            continue
        entry.update({
            "status": "running",
            "modal_call_id": call_id,
            "attempts": int(entry.get("attempts", 0)) + 1,
            "started_at": _now(),
        })
        _save_state(state_path, state)


def launch(
    suite_path: Path,
    *,
    state_path: Path,
    bundle_dir: Path,
    bridge: ModalBridge | None = None,
    resume: bool = False,
    resume_subscriptions: bool = False,
    retry_failed_jobs: tuple[str, ...] = (),
) -> dict[str, Any]:
    if bridge is None:
        suite, _raw, _manifests = _load_and_validate_suite(
            suite_path,
            verify_sources=not resume,
        )
        agent_providers = frozenset(_agent_provider_for_job(job) for job in suite.jobs)
        runtimes = frozenset(matrix._manifest_agent_runtime(manifest) for manifest in _manifests.values())
        from reference.execution.agent_container import image_backend
        for manifest in _manifests.values():
            if matrix._manifest_agent_runtime(manifest) == "hermes":
                image = manifest.get("runtime_provenance", {}).get("agent_container", {}).get("image")
                if not image or image_backend(image) != "modal-sandbox":
                    raise ModalSuiteError("Prepare the Hermes job with a Modal agent image ID (im-...), not a local Docker image")
        with _RealModalBridge(agent_providers=agent_providers, runtimes=runtimes) as real_bridge:
            return launch(
                suite_path,
                state_path=state_path,
                bundle_dir=bundle_dir,
                bridge=real_bridge,
                resume=resume,
                resume_subscriptions=resume_subscriptions,
                retry_failed_jobs=retry_failed_jobs,
            )
    suite, raw, manifests = _load_and_validate_suite(suite_path, verify_sources=not resume)
    if state_path.exists():
        if not resume:
            raise ModalSuiteError(f"state already exists: {state_path}; use resume")
        state = _load_state(state_path, suite)
        _verify_saved_inputs(state, raw, manifests)
    else:
        if resume:
            raise ModalSuiteError(f"cannot resume because state does not exist: {state_path}")
        bundle = build_bundle(suite, manifests, bundle_dir)
        profile_archives = build_profile_archives(
            suite, manifests, bundle_dir / "profiles",
        )
        native_memory_archives = build_native_memory_archives(
            suite, manifests, bundle_dir / "claude-native-memory",
        )
        bridge.upload_bundle(Path(bundle["path"]), bundle["name"])
        uploaded: set[str] = set()
        for profile in profile_archives.values():
            if profile["name"] in uploaded:
                continue
            bridge.upload_profile(Path(profile["path"]), profile["name"])
            uploaded.add(profile["name"])
        uploaded_native: set[str] = set()
        for native_memory in native_memory_archives.values():
            if native_memory["name"] in uploaded_native:
                continue
            bridge.upload_native_memory(Path(native_memory["path"]), native_memory["name"])
            uploaded_native.add(native_memory["name"])
        state = _initial_state(
            suite,
            bundle,
            _luna_policy(raw, suite),
            profile_archives,
            native_memory_archives,
            list(raw["test_ids"]),
        )
    refresh_status(suite, state, bridge)
    if resume:
        _begin_waiting_jobs(state, resume_subscriptions=resume_subscriptions, now=time.time())
        for job_id in retry_failed_jobs:
            if job_id not in state["jobs"]:
                raise ModalSuiteError(f"unknown failed job: {job_id}")
            entry = state["jobs"][job_id]
            if entry["status"] == "failed":
                entry["status"] = "pending"
                entry["action"] = _recovery_action(str(entry["action"]))
    admit_jobs(suite, state, bridge, state_path)
    _save_state(state_path, state)
    return state


def status(suite_path: Path, *, state_path: Path, bridge: ModalBridge | None = None) -> dict[str, Any]:
    if bridge is None:
        with _RealModalBridge() as real_bridge:
            return status(suite_path, state_path=state_path, bridge=real_bridge)
    suite, _raw, _manifests = _load_and_validate_suite(suite_path, verify_sources=False)
    state = _load_state(state_path, suite)
    refresh_status(suite, state, bridge)
    _save_state(state_path, state)
    return state


def download(suite_path: Path, *, state_path: Path, destination: Path, bridge: ModalBridge | None = None) -> list[Path]:
    if bridge is None:
        with _RealModalBridge() as real_bridge:
            return download(
                suite_path,
                state_path=state_path,
                destination=destination,
                bridge=real_bridge,
            )
    suite, _raw, _manifests = _load_and_validate_suite(suite_path, verify_sources=False)
    state = _load_state(state_path, suite)
    refresh_status(suite, state, bridge)
    _save_state(state_path, state)
    copied: list[Path] = []
    for job in suite.jobs:
        entry = state["jobs"][job.id]
        if entry.get("status") != "completed":
            continue
        remote_job_id = str(entry.get("remote_job_id") or job.id)
        metadata_raw = bridge.read_result_file(remote_job_id, "metadata.json")
        metadata = json.loads(metadata_raw)
        if metadata.get("status") != "completed" or metadata.get("returncode") != 0:
            raise ModalSuiteError(f"{job.id}: completed state has invalid remote metadata")
        job_root = destination / job.id
        job_root.mkdir(parents=True, exist_ok=True)
        (job_root / "metadata.json").write_bytes(metadata_raw)
        copied.append(job_root / "metadata.json")
        for expected in metadata.get("files") or []:
            relative = expected.get("path")
            if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ModalSuiteError(f"{job.id}: remote result has an unsafe file name")
            content = bridge.read_result_file(remote_job_id, relative)
            target = job_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            if len(content) != expected.get("bytes") or hashlib.sha256(content).hexdigest() != expected.get("sha256"):
                raise ModalSuiteError(f"{job.id}: downloaded file did not match the remote result: {relative}")
            copied.append(target)
    return copied


def _summary(state: dict[str, Any]) -> dict[str, Any]:
    jobs = state.get("jobs") or {}
    counts: dict[str, int] = {}
    for entry in jobs.values():
        name = str(entry.get("status") or "unknown")
        counts[name] = counts.get(name, 0) + 1
    return {
        "state": state.get("status"),
        "job_counts": counts,
        "luna": state.get("luna"),
        "jobs": {
            job_id: {
                key: entry.get(key)
                for key in ("status", "attempts", "last_action", "returncode", "remote_error", "started_at", "ended_at")
                if key in entry
            }
            for job_id, entry in jobs.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("launch", "resume", "status", "download"):
        command = commands.add_parser(name)
        command.add_argument("--suite", type=Path, required=True)
        command.add_argument("--state", type=Path, required=True)
    launch_parser = commands.choices["launch"]
    launch_parser.add_argument("--bundle-dir", type=Path, required=True)
    resume_parser = commands.choices["resume"]
    resume_parser.add_argument("--bundle-dir", type=Path, required=True)
    resume_parser.add_argument("--resume-subscriptions", action="store_true")
    resume_parser.add_argument("--retry-failed-job", action="append", default=[], help="Resume this inspected failed job from its saved results")
    download_parser = commands.choices["download"]
    download_parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "launch":
            state = launch(args.suite, state_path=args.state, bundle_dir=args.bundle_dir)
            print(json.dumps(_summary(state), indent=2, sort_keys=True))
            return 0
        if args.command == "resume":
            state = launch(
                args.suite,
                state_path=args.state,
                bundle_dir=args.bundle_dir,
                resume=True,
                resume_subscriptions=args.resume_subscriptions,
                retry_failed_jobs=tuple(args.retry_failed_job),
            )
            print(json.dumps(_summary(state), indent=2, sort_keys=True))
            return 0
        if args.command == "status":
            print(json.dumps(_summary(status(args.suite, state_path=args.state)), indent=2, sort_keys=True))
            return 0
        copied = download(args.suite, state_path=args.state, destination=args.destination)
        print(json.dumps({"downloaded_files": [str(path) for path in copied]}, indent=2))
        return 0
    except ModalSuiteError as exc:
        parser.error(str(exc))
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
