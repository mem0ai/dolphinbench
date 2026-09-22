"""Evaluation and ingestion preparation."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from typing import Any, Mapping


import yaml

from harness.environment import load_environment
from reference import evaluate as matrix
from reference import evaluate as local_suite


ROOT = Path(__file__).resolve().parents[1]
MEMORY_PROVIDERS = ("builtin", "mem0", "honcho", "hindsight", "supermemory")
COMPLETED_SEED_PROVIDERS = MEMORY_PROVIDERS
RUNTIMES = ("hermes", "claude")
EXPECTED_RUNTIME_COUNTS = {"hermes": 2, "claude": 1}
EXPECTED_MODEL_FAMILIES = {
    "hermes": {"luna", "minimax"},
    "claude": {"sonnet"},
}
PURPOSES = ("integration_smoke", "partial_final_results", "final_results")
CURRENT_JUDGE = {
    "backend": "azure",
    "deployment": "gpt-5.6-sol",
    "reasoning_effort": "medium",
    "parallelism_per_test": 1,
    "concurrent_test_grades": 2,
}
RUNTIME_AUTH_SECRETS = {
    "claude": "dolphinbench-evaluation",
}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class PlanError(ValueError):
    """Raised when a final-evaluation plan is incomplete or inconsistent."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _path(value: Any, *, base: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{label} must be a non-empty path")
    candidate = Path(value).expanduser()
    return (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError(f"cannot read {label}: {path}: {exc}") from exc
    try:
        value = json.loads(raw) if path.suffix.lower() == ".json" else yaml.safe_load(raw)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise PlanError(f"{label} is not valid JSON or YAML: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PlanError(f"{label} must be a JSON/YAML object: {path}")
    return value


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{label} must be a non-empty string")
    return value.strip()


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PlanError(f"{label} must be an integer greater than zero")
    return value


def _test_ids(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise PlanError("published_tests.ids must be a non-empty list")
    normalized: list[str] = []
    for raw in value:
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise PlanError("published_tests.ids must contain strings or integers")
        try:
            number = int(str(raw))
        except ValueError as exc:
            raise PlanError(f"invalid published test ID: {raw!r}") from exc
        if number < 1 or number > 200:
            raise PlanError(f"published test ID must be between 1 and 200: {raw!r}")
        test_id = f"{number:03d}"
        if test_id in normalized:
            raise PlanError(f"duplicate published test ID: {test_id}")
        normalized.append(test_id)
    return normalized


def _published_tests(raw: Any, *, base: Path, purpose: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PlanError("published_tests must be an object")
    directory = _path(raw.get("directory"), base=base, label="published_tests.directory")
    if not directory.is_dir():
        raise PlanError(f"published test directory does not exist: {directory}")
    test_ids = _test_ids(raw.get("ids"))
    if purpose == "integration_smoke" and not 1 <= len(test_ids) <= 10:
        raise PlanError("an integration_smoke plan must contain between 1 and 10 published tests")
    if purpose in {"partial_final_results", "final_results"} and test_ids != [
        f"{index:03d}" for index in range(1, 201)
    ]:
        raise PlanError(
            f"a {purpose} plan must contain published tests 001 through 200 in order"
        )
    supplied = raw.get("sha256")
    if not isinstance(supplied, dict) or set(supplied) != set(test_ids):
        raise PlanError("published_tests.sha256 must contain exactly one hash per test ID")
    hashes: dict[str, str] = {}
    paths: dict[str, str] = {}
    for test_id in test_ids:
        expected = supplied[test_id]
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            raise PlanError(f"published_tests.sha256[{test_id}] is not a SHA-256 hash")
        path = directory / f"{test_id}.yaml"
        if not path.is_file():
            path = directory / f"{test_id}.yml"
        if not path.is_file():
            raise PlanError(f"published test file is missing: {test_id} in {directory}")
        actual = _sha256(path)
        if actual.lower() != expected.lower():
            raise PlanError(
                f"published test {test_id} changed: expected {expected}, found {actual}"
            )
        hashes[test_id] = actual
        paths[test_id] = str(path)
    return {"directory": str(directory), "ids": test_ids, "sha256": hashes, "paths": paths}


def _published_test_runtime(
    raw: Any,
    *,
    base: Path,
    tests: dict[str, Any],
    persona: str,
) -> dict[str, Any] | None:
    """Load the exact app runtime used to certify the published tests."""
    if not isinstance(raw, dict) or raw.get("runtime") is None:
        return None
    runtime = raw["runtime"]
    if not isinstance(runtime, dict):
        raise PlanError("published_tests.runtime must be an object")
    source_manifest_path = _path(
        runtime.get("source_manifest"),
        base=base,
        label="published_tests.runtime.source_manifest",
    )
    expected_manifest_hash = _required_string(
        runtime.get("source_manifest_sha256"),
        "published_tests.runtime.source_manifest_sha256",
    ).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_hash):
        raise PlanError("published_tests.runtime.source_manifest_sha256 is not a SHA-256 hash")
    actual_manifest_hash = _sha256(source_manifest_path)
    if actual_manifest_hash != expected_manifest_hash:
        raise PlanError(
            "published test runtime manifest changed: "
            f"expected {expected_manifest_hash}, found {actual_manifest_hash}"
        )
    source_manifest = _load_object(source_manifest_path, "published test runtime manifest")
    if source_manifest.get("status") != "assembled_candidate_all_200_certified":
        raise PlanError("published test runtime manifest is not the accepted certified batch")

    release_path = ROOT / "authoring" / "release_manifests" / f"{persona}.json"
    release = _load_object(release_path, f"{persona} release manifest")
    hashes = (release.get("published_sha256"), release.get("candidate_sha256"))
    if release.get("published") is not True or tests["sha256"] not in hashes:
        raise PlanError("published test hashes do not match the canonical release manifest")
    accepted_ids = [int(test_id) for test_id in tests["ids"]]
    accepted_batch = next(
        (
            batch for batch in release.get("batches") or []
            if isinstance(batch, dict)
            and batch.get("kind") == "approved_repaired_release"
            and batch.get("accepted_test_ids") == accepted_ids
        ),
        None,
    )
    if accepted_batch is None:
        raise PlanError("canonical release manifest has no accepted batch for these tests")
    provenance_path = Path(str(accepted_batch.get("directory") or "")) / "provenance.json"
    if (
        not provenance_path.is_file()
        or _sha256(provenance_path) != accepted_batch.get("provenance_sha256")
    ):
        raise PlanError("accepted publication provenance is missing or changed")
    provenance = _load_object(provenance_path, "accepted publication provenance")
    if (
        Path(str(provenance.get("source_manifest") or "")).resolve() != source_manifest_path
        or provenance.get("source_manifest_sha256") != actual_manifest_hash
    ):
        raise PlanError("accepted publication provenance names a different certified source")

    mock_mcp_root = _path(
        runtime.get("mock_mcp_root"),
        base=base,
        label="published_tests.runtime.mock_mcp_root",
    )
    if not mock_mcp_root.is_dir():
        raise PlanError(f"certified mock MCP directory does not exist: {mock_mcp_root}")
    implementation_hashes = source_manifest.get("implementation_sha256")
    if not isinstance(implementation_hashes, dict) or not implementation_hashes:
        raise PlanError("published test runtime manifest has no implementation hashes")
    for raw_path, expected in implementation_hashes.items():
        original = Path(raw_path).resolve()
        if original.is_relative_to(ROOT / "mock_mcp"):
            actual_path = mock_mcp_root / original.relative_to(ROOT / "mock_mcp")
        else:
            actual_path = original
        if not actual_path.is_file() or _sha256(actual_path) != expected:
            raise PlanError(f"certified implementation is missing or changed: {actual_path}")

    source_files = source_manifest.get("files")
    if not isinstance(source_files, dict):
        raise PlanError("published test runtime manifest has no file hashes")
    runtime_index_path = _path(
        runtime.get("runtime_index"),
        base=base,
        label="published_tests.runtime.runtime_index",
    )
    expected_index_hash = _required_string(
        runtime.get("runtime_index_sha256"),
        "published_tests.runtime.runtime_index_sha256",
    ).lower()
    if _sha256(runtime_index_path) != expected_index_hash:
        raise PlanError("published test runtime index is missing or changed")
    runtime_index = _load_object(runtime_index_path, "published test runtime index")
    if set(runtime_index) != set(tests["ids"]):
        raise PlanError("published test runtime index does not cover exactly the published tests")
    entries: dict[str, dict[str, Any]] = {}
    for test_id in tests["ids"]:
        source_path = source_manifest_path.parent / "tests" / f"{test_id}.json"
        expected_source_hash = source_files.get(f"tests/{test_id}.json")
        if not isinstance(expected_source_hash, str) or _sha256(source_path) != expected_source_hash:
            raise PlanError(f"certified source test is missing or changed: {test_id}")
        entry = runtime_index[test_id]
        if not isinstance(entry, dict) or entry.get("version") != 1:
            raise PlanError(f"certified source test {test_id} has no supported runtime")
        workspace = _path(entry.get("workspace"), base=base, label=f"{test_id}.workspace")
        tool_config = _path(entry.get("tool_config"), base=base, label=f"{test_id}.tool_config")
        for label, path, hash_key in (
            ("workspace", workspace, "workspace_sha256"),
            ("tool config", tool_config, "tool_config_sha256"),
        ):
            expected = entry.get(hash_key)
            if not isinstance(expected, str) or _sha256(path) != expected:
                raise PlanError(f"certified {label} is missing or changed for test {test_id}")
            try:
                relative = path.relative_to(source_manifest_path.parent).as_posix()
            except ValueError as exc:
                raise PlanError(f"certified {label} is outside its accepted batch for test {test_id}") from exc
            if source_files.get(relative) != expected:
                raise PlanError(f"certified {label} is not bound by the accepted manifest for test {test_id}")
        entries[test_id] = {
            "workspace": str(workspace),
            "workspace_sha256": entry["workspace_sha256"],
            "tool_config": str(tool_config),
            "tool_config_sha256": entry["tool_config_sha256"],
        }
    return {
        "version": 1,
        "source_manifest": str(source_manifest_path),
        "source_manifest_sha256": actual_manifest_hash,
        "mock_mcp_root": str(mock_mcp_root),
        "mock_mcp_entrypoint": str(mock_mcp_root / "repair_server.py"),
        "implementation_sha256": implementation_hashes,
        "tests": entries,
    }


def _selected_memory_providers(raw: Any, *, purpose: str) -> tuple[str, ...]:
    if raw is None:
        providers = MEMORY_PROVIDERS
    elif not isinstance(raw, list) or not raw:
        raise PlanError("memory_providers must be a non-empty list")
    else:
        providers = tuple(str(item).strip() for item in raw)
    if len(providers) != len(set(providers)):
        raise PlanError("memory_providers contains a duplicate")
    unknown = sorted(set(providers) - set(MEMORY_PROVIDERS))
    if unknown:
        raise PlanError("unsupported memory provider: " + ", ".join(unknown))
    if purpose == "final_results" and providers != MEMORY_PROVIDERS:
        raise PlanError("a final_results plan must use all five memory providers in canonical order")
    return tuple(providers)


def _seed_inputs(
    raw: Any,
    *,
    base: Path,
    providers: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict) or set(raw) != set(providers):
        raise PlanError(
            "completed_seeds must list exactly: " + ", ".join(providers)
        )
    result: dict[str, dict[str, Any]] = {}
    for provider in providers:
        item = raw[provider]
        if not isinstance(item, dict):
            raise PlanError(f"completed_seeds.{provider} must be an object")
        if provider == "builtin" and "hermes" in item:
            item = item["hermes"]
            if not isinstance(item, dict):
                raise PlanError("completed_seeds.builtin.hermes must be an object")
        if provider in {"hindsight", "supermemory"} and "snapshot" in item:
            spec = item["snapshot"]
            if not isinstance(spec, dict):
                raise PlanError(f"completed_seeds.{provider}.snapshot must be an object")
            paths = {key: _path(spec.get(key), base=base, label=f"snapshot.{key}")
                     for key in ("archive", "checksum", "receipt", "checkpoint_dir")}
            if any(not path.exists() for path in paths.values()):
                raise PlanError("completed snapshot inputs are missing")
            result[provider] = {"snapshot": {**paths, "service_url": _required_string(
                spec.get("service_url"), "snapshot.service_url")}}
            continue
        manifest = _path(item.get("manifest"), base=base, label=f"completed_seeds.{provider}.manifest")
        state = _path(item.get("state"), base=base, label=f"completed_seeds.{provider}.state")
        if not manifest.is_file() or not state.is_file():
            raise PlanError(
                f"completed_seeds.{provider} needs both an existing manifest and state file"
            )
        result[provider] = {"manifest": manifest, "state": state}
    return result


def _claude_builtin_receipt(raw: Any, *, base: Path) -> Path:
    if not isinstance(raw, dict) or not isinstance(raw.get("builtin"), dict):
        raise PlanError("completed_seeds.builtin must be an object")
    claude = raw["builtin"].get("claude")
    if not isinstance(claude, dict):
        raise PlanError(
            "completed_seeds.builtin.claude must name the completed Claude native-memory receipt"
        )
    receipt = _path(
        claude.get("receipt"), base=base,
        label="completed_seeds.builtin.claude.receipt",
    )
    if not receipt.is_file():
        raise PlanError("completed_seeds.builtin.claude.receipt does not exist")
    return receipt


def _runtime_configs(raw: Any, *, purpose: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise PlanError("runtimes must be a non-empty list")
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    allowed_effort = set(matrix.AGENT_REASONING_EFFORTS)
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise PlanError(f"runtimes[{index}] must be an object")
        runtime = _required_string(item.get("runtime"), f"runtimes[{index}].runtime")
        if runtime not in RUNTIMES:
            raise PlanError(f"runtimes[{index}].runtime must be one of {RUNTIMES}")
        model_family = _required_string(
            item.get("model_family"), f"runtimes[{index}].model_family"
        ).lower()
        if model_family not in EXPECTED_MODEL_FAMILIES[runtime]:
            raise PlanError(
                f"runtimes[{index}].model_family must be one of "
                f"{sorted(EXPECTED_MODEL_FAMILIES[runtime])} for {runtime}"
            )
        model_value = item.get("model")
        if not isinstance(model_value, str) or not model_value.strip():
            raise PlanError(
                f"runtimes[{index}].model is required; provide the exact outer model ID"
            )
        model = model_value.strip()
        context_length = item.get("context_length")
        api_mode = item.get("agent_api_mode", "codex_responses")
        if runtime == "hermes":
            context_length = _positive_int(
                context_length, f"runtimes[{index}].context_length"
            )
            api_mode = _required_string(api_mode, f"runtimes[{index}].agent_api_mode")
            if api_mode not in {"chat_completions", "codex_responses"}:
                raise PlanError(
                    f"runtimes[{index}].agent_api_mode must be chat_completions or codex_responses"
                )
        elif context_length is not None:
            raise PlanError(
                f"runtimes[{index}].context_length is only used by Hermes; omit it for {runtime}"
            )
        elif "agent_api_mode" in item:
            raise PlanError(
                f"runtimes[{index}].agent_api_mode is only used by Hermes; omit it for {runtime}"
            )
        else:
            api_mode = None
        base_url = item.get("agent_base_url")
        api_key_env = item.get("agent_api_key_env")
        if runtime != "hermes" and (base_url is not None or api_key_env is not None):
            raise PlanError(
                f"runtimes[{index}] may only set agent_base_url and agent_api_key_env for Hermes"
            )
        if (base_url is None) != (api_key_env is None):
            raise PlanError(
                f"runtimes[{index}] must set both agent_base_url and agent_api_key_env"
            )
        if base_url is not None:
            base_url = _required_string(base_url, f"runtimes[{index}].agent_base_url")
            api_key_env = _required_string(api_key_env, f"runtimes[{index}].agent_api_key_env")
        if runtime == "hermes" and (base_url is None or api_key_env is None):
            raise PlanError(
                f"runtimes[{index}] must provide the exact Hermes agent_base_url "
                "and agent_api_key_env"
            )
        reasoning = item.get("reasoning_effort")
        if reasoning is not None:
            reasoning = _required_string(reasoning, f"runtimes[{index}].reasoning_effort")
            if reasoning not in allowed_effort:
                raise PlanError(
                    f"runtimes[{index}].reasoning_effort must be one of {sorted(allowed_effort)}"
                )
        if runtime == "hermes" and reasoning is None:
            raise PlanError("Hermes requires an explicit reasoning_effort")
        if runtime == "claude" and reasoning is not None:
            raise PlanError(
                f"{runtime} does not accept a reasoning_effort in its current driver; use null"
            )
        if "auth_reference" in item:
            raise PlanError(
                f"runtimes[{index}].auth_reference is not used; {runtime} authentication "
                "comes from the fixed Modal secret recorded by the preparation command"
            )
        auth_secret = RUNTIME_AUTH_SECRETS.get(runtime)
        key = (runtime, model_family)
        if key in seen:
            raise PlanError(f"duplicate runtime model family: {runtime}/{model_family}")
        seen.add(key)
        result.append({
            "runtime": runtime,
            "model_family": model_family,
            "model": model,
            "reasoning_effort": reasoning,
            "context_length": context_length,
            "agent_api_mode": api_mode,
            "agent_base_url": base_url,
            "agent_api_key_env": api_key_env,
            "auth_secret": auth_secret,
            **({"completed_ingestions": item["completed_ingestions"]}
               if "completed_ingestions" in item else {}),
        })
    if purpose == "final_results":
        counts = {runtime: sum(item["runtime"] == runtime for item in result) for runtime in RUNTIMES}
        if counts != EXPECTED_RUNTIME_COUNTS:
            raise PlanError(
                "a final_results plan needs Hermes Luna, Hermes MiniMax, and Claude Code Sonnet"
            )
        families = {
            runtime: {item["model_family"] for item in result if item["runtime"] == runtime}
            for runtime in RUNTIMES
        }
        if families != EXPECTED_MODEL_FAMILIES:
            raise PlanError(
                "a final_results plan must name Luna and MiniMax for Hermes and Sonnet for Claude"
            )
    return result


def _completed_ingestions(config, runtimes, providers, *, base: Path):
    """Resolve ingestion inputs per harness/model, retaining single-model legacy inputs."""
    resolved = {}
    for runtime in runtimes:
        name = f"{runtime['runtime']}-{runtime['model_family']}"
        raw = runtime.get("completed_ingestions")
        if raw is None:
            legacy = config.get("completed_seeds")
            if runtime["runtime"] == "hermes":
                if sum(item["runtime"] == "hermes" for item in runtimes) != 1:
                    raise PlanError(f"{name}: set completed_ingestions on each runtime so models use their own ingestion")
                raw = legacy
            elif providers == ("builtin",):
                raw = {"builtin": {"receipt": str(_claude_builtin_receipt(legacy, base=base))}}
        if not isinstance(raw, dict) or set(raw) != set(providers):
            raise PlanError(f"{name}.completed_ingestions must list exactly: {', '.join(providers)}")
        if runtime["runtime"] == "hermes":
            resolved[name] = _seed_inputs(raw, base=base, providers=providers)
            continue
        entries = {}
        for provider, value in raw.items():
            if not isinstance(value, dict):
                raise PlanError(f"{name}/{provider}: ingestion must be an object")
            item = dict(value)
            item["receipt"] = _path(item.get("receipt"), base=base, label=f"{name}/{provider}.receipt")
            if not item["receipt"].is_file():
                raise PlanError(f"{name}/{provider}: missing receipt: {item['receipt']}")
            entries[provider] = item
        resolved[name] = entries
    return resolved


def _validate_credentials(runtimes: list[dict[str, Any]]) -> dict[str, str]:
    """Check credential names and values without contacting any service."""
    env = dict(load_environment(matrix.HERMES_HOME / ".env"))
    missing: list[str] = []
    for index, runtime in enumerate(runtimes, start=1):
        if runtime["runtime"] != "hermes":
            continue
        if runtime["agent_base_url"]:
            key_name = runtime["agent_api_key_env"]
            if not env.get(key_name):
                missing.append(f"runtimes[{index}].agent_api_key_env ({key_name})")
        else:
            for key in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY"):
                if not env.get(key):
                    missing.append(key)
    if env.get("DOLPHINBENCH_JUDGE_BACKEND", "azure").lower() != CURRENT_JUDGE["backend"]:
        raise PlanError("the judge must use the current Azure gpt-5.6-sol configuration")
    if env.get("DOLPHINBENCH_JUDGE_DEPLOYMENT", CURRENT_JUDGE["deployment"]) != CURRENT_JUDGE["deployment"]:
        raise PlanError("the judge must use the current Azure gpt-5.6-sol configuration")
    for key in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY"):
        if not env.get(key):
            missing.append(key)
    if missing:
        raise PlanError("missing credentials or endpoints: " + ", ".join(sorted(set(missing))))
    return env


def _source_job(seed_path: dict[str, Path], provider: str, persona: str) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = _load_object(seed_path["manifest"], f"{provider} seed manifest")
    state = _load_object(seed_path["state"], f"{provider} seed state")
    if manifest.get("manifest_version") != matrix.MANIFEST_VERSION:
        raise PlanError(f"{provider} seed manifest has an unsupported version")
    if manifest.get("phase") != "prepared_offline":
        raise PlanError(f"{provider} seed manifest is not an unlaunched prepared manifest")
    if state.get("manifest") != str(seed_path["manifest"].resolve()):
        raise PlanError(f"{provider} seed state belongs to a different manifest")
    if not state.get("phases"):
        raise PlanError(f"{provider} seed state has no recorded phases")
    if manifest.get("personas") != [persona]:
        raise PlanError(
            f"{provider} seed manifest must contain only persona {persona!r} for this plan"
        )
    jobs = manifest.get("jobs")
    if not isinstance(jobs, list):
        raise PlanError(f"{provider} seed manifest has no jobs list")
    job_id = f"{provider}-{persona}"
    matches = [job for job in jobs if isinstance(job, dict) and str(job.get("id")) == job_id]
    if len(matches) != 1:
        raise PlanError(f"{provider} seed manifest must contain exactly one job {job_id}")
    return manifest, matches[0]


def _slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_.-")
    return value or "model"


def _luna_job(manifest: dict[str, Any], memory: str) -> bool:
    agent = manifest.get("agent") or {}
    if agent.get("model") == "gpt-5.6-luna":
        return True
    if memory != "honcho":
        return False
    internal = ((manifest.get("runtime_provenance") or {}).get("honcho") or {}).get("internal_models") or {}
    return any(model == "gpt-5.6-luna" for model in internal.values())


def _job_resources(manifest: dict[str, Any], runtime: str, memory: str) -> list[str]:
    agent = manifest.get("agent") or {}
    judge = manifest.get("judge") or {}
    resources = [
        f"agent:{runtime}",
        f"memory:{memory}",
        f"provider:{agent.get('provider')}",
        f"judge:{judge.get('backend')}",
    ]
    if _luna_job(manifest, memory):
        resources.append("azure-luna")
    if judge.get("backend") == "azure" and judge.get("deployment") == "gpt-5.6-sol":
        resources.append("azure-sol-judge")
    return list(dict.fromkeys(resources))


def _validate_current_judge(manifest: dict[str, Any], runtime: str, memory: str) -> None:
    judge = manifest.get("judge")
    if not isinstance(judge, dict) or any(judge.get(key) != value for key, value in CURRENT_JUDGE.items()):
        raise PlanError(
            f"{runtime}/{memory}: prepared manifest must use the current Azure "
            "gpt-5.6-sol judge with medium reasoning"
        )


def _validate_resource_limits(raw: Any) -> tuple[int, dict[str, int]]:
    if not isinstance(raw, dict):
        raise PlanError("resource_limits must be an object")
    limits: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise PlanError("resource limit names must be non-empty strings")
        limits[key] = _positive_int(value, f"resource_limits.{key}")
    return _positive_int(raw.get("global_limit"), "resource_limits.global_limit"), {
        key: value for key, value in limits.items() if key != "global_limit"
    }


def build_plan(config_path: Path) -> dict[str, Any]:
    """Build manifests and a suite without contacting any provider."""
    config_path = config_path.resolve()
    config = _load_object(config_path, "plan configuration")
    base = config_path.parent
    persona = _required_string(config.get("persona"), "persona")
    purpose = _required_string(config.get("purpose"), "purpose")
    if purpose not in PURPOSES:
        raise PlanError(f"purpose must be one of {PURPOSES}")
    memory_providers = _selected_memory_providers(
        config.get("memory_providers"), purpose=purpose,
    )
    tests = _published_tests(config.get("published_tests"), base=base, purpose=purpose)
    test_runtime = _published_test_runtime(
        config.get("published_tests"), base=base, tests=tests, persona=persona,
    )
    runtimes = _runtime_configs(config.get("runtimes"), purpose=purpose)
    if any(item["runtime"] == "claude" for item in runtimes) and any(
        provider != "builtin" for provider in memory_providers
    ):
        raise PlanError(
            "This retained suite builder prepares Hermes and Claude Built-In evaluations; "
            "it does not launch Claude external-memory evaluations."
        )
    hermes_source = None
    if any(item["runtime"] == "hermes" for item in runtimes):
        hermes_source = _path(config.get("hermes_source"), base=base, label="hermes_source")
        if not hermes_source.is_dir():
            raise PlanError(f"hermes_source is not a directory: {hermes_source}")
    ingestions = _completed_ingestions(config, runtimes, memory_providers, base=base)
    evaluation_env = _validate_credentials(runtimes)
    evaluation_env["DOLPHINBENCH_JUDGE_PARALLELISM"] = "1"
    evaluation_env["DOLPHINBENCH_GRADING_WORKERS"] = "2"
    global_limit, resource_limits = _validate_resource_limits(config.get("resource_limits"))
    output_root = _path(config.get("output_root"), base=base, label="output_root")
    if output_root.exists():
        raise PlanError(f"output_root already exists; choose a new plan directory: {output_root}")
    output_root.mkdir(parents=True)

    unsupported: list[dict[str, Any]] = []
    supported: list[dict[str, Any]] = []
    for runtime_config in runtimes:
        runtime = runtime_config["runtime"]
        for memory in memory_providers:
            cell = {key: value for key, value in runtime_config.items() if key != "completed_ingestions"}
            cell["memory_provider"] = memory
            supported.append({**cell, "status": "ready"})

    generated: list[dict[str, Any]] = []
    ingestion_records = {}
    used_inputs = set()
    used_stores = set()
    ready_cells = list(supported)
    for cell in ready_cells:
        runtime = cell["runtime"]
        memory = cell["memory_provider"]
        name = f"{runtime}-{cell['model_family']}"
        seed_paths = ingestions[name][memory]
        if "snapshot" in seed_paths:
            from reference.artifacts import materialize
            seed_paths = materialize(seed_paths["snapshot"],
                output=output_root / "completed_ingestions" / name / memory,
                persona=persona, tests=tests)
        origin = seed_paths["manifest"] if runtime == "hermes" else seed_paths["receipt"]
        input_key = (memory, str(origin.resolve()))
        if input_key in used_inputs:
            raise PlanError(f"{name}/{memory}: another configuration uses this ingestion record")
        used_inputs.add(input_key)
        identity = None
        if runtime == "hermes":
            source_manifest, source_job = _source_job(seed_paths, memory, persona)
            if source_manifest.get("agent", {}).get("model") != cell["model"]:
                raise PlanError(f"{name}/{memory}: ingestion and evaluation models differ")
            identity = source_job.get("provider_identity")
            if not isinstance(identity, dict):
                raise PlanError(f"{name}/{memory}: completed ingestion has no provider_identity")
        target_root = output_root / "manifests" / (
            f"{runtime}__{_slug(cell['model'])}__"
            f"{cell['reasoning_effort'] or 'none'}__{memory}"
        )
        native_claude_builtin = runtime == "claude" and memory == "builtin"
        if runtime == "claude":
            manifest_path = matrix.prepare_claude_native_memory_test_only(
                seed_receipt_path=seed_paths["receipt"],
                output_root=target_root, test_ids=tuple(tests["ids"]), model=cell["model"],
                root=ROOT, env=dict(evaluation_env),
            )
        else:
            kwargs: dict[str, Any] = {
                "seed_manifest_path": seed_paths["manifest"],
                "seed_state_path": seed_paths["state"],
                "output_root": target_root,
                "configurations": (memory,),
                "test_ids": tuple(tests["ids"]),
                "root": ROOT,
                "hermes_source": hermes_source,
                "agent_runtime": runtime,
                "model": cell["model"],
                "env": {
                    **evaluation_env,
                    **(
                        {"DOLPHINBENCH_AGENT_API_MODE": cell["agent_api_mode"]}
                        if runtime == "hermes"
                        else {}
                    ),
                },
                "published_tests_directory": Path(tests["directory"]),
                "published_test_runtime": test_runtime,
            }
            if runtime == "hermes":
                kwargs.update({
                    "agent_base_url": cell["agent_base_url"],
                    "agent_api_key_env": cell["agent_api_key_env"],
                    "agent_context_length": cell["context_length"],
                    "agent_reasoning_effort": cell["reasoning_effort"],
                })
            manifest_path = matrix.prepare_test_only(**kwargs)
        manifest = matrix.load_manifest(manifest_path)
        matrix.verify_manifest_hashes(manifest)
        _validate_current_judge(manifest, runtime, memory)
        if len(manifest.get("jobs") or []) != 1:
            raise PlanError(f"{runtime}/{memory}: helper emitted more than one matrix job")
        job = manifest["jobs"][0]
        if job.get("agent_runtime") != runtime or job.get("configuration") != memory:
            raise PlanError(f"{runtime}/{memory}: helper emitted the wrong runtime or memory configuration")
        if runtime == "hermes" and job.get("provider_identity") != identity:
            raise PlanError(f"{runtime}/{memory}: prepared manifest changed the completed provider identity")
        if runtime == "claude":
            identity = job.get("provider_identity")
            if not isinstance(identity, dict):
                raise PlanError(f"{name}/{memory}: prepared manifest has no provider_identity")
            if native_claude_builtin and identity.get("kind") != "claude_native_auto_memory":
                raise PlanError("claude/builtin: helper emitted the wrong native-memory identity")
        if memory != "builtin":
            store = (memory, json.dumps(identity, sort_keys=True), json.dumps(job.get("gateway_config") or {}, sort_keys=True))
            if store in used_stores:
                raise PlanError(f"{name}/{memory}: another configuration uses the same memory store")
            used_stores.add(store)
        if manifest.get("agent", {}).get("model") != cell["model"]:
            raise PlanError(f"{runtime}/{memory}: prepared manifest has the wrong outer model")
        if runtime == "hermes" and manifest.get("agent", {}).get("reasoning_effort") != cell["reasoning_effort"]:
            raise PlanError(f"{runtime}/{memory}: prepared manifest has the wrong reasoning setting")
        resources = _job_resources(manifest, runtime, memory)
        missing_limits = sorted(set(resources) - set(resource_limits))
        if missing_limits:
            raise PlanError(
                f"{runtime}/{memory}: resource_limits is missing " + ", ".join(missing_limits)
            )
        suite_job_id = f"{runtime}-{_slug(cell['model'])}-{cell['reasoning_effort'] or 'none'}-{memory}"
        if not _ID.fullmatch(suite_job_id):
            raise PlanError(f"generated suite job ID is invalid: {suite_job_id}")
        relative_manifest = str(Path(manifest_path).resolve().relative_to(output_root))
        generated.append({
            "id": suite_job_id,
            "status": "ready",
            "runtime": runtime,
            "model": cell["model"],
            "reasoning_effort": cell["reasoning_effort"],
            "memory_provider": memory,
            "manifest": str(manifest_path.resolve()),
            "manifest_relative": relative_manifest,
            "resources": resources,
            "provider_identity": identity,
            "source_seed_manifest": str(seed_paths["manifest"]) if runtime == "hermes" else None,
            "source_seed_state": str(seed_paths["state"]) if runtime == "hermes" else None,
            "source_seed_receipt": str(seed_paths["receipt"]) if runtime == "claude" else None,
        })
        fields = ("manifest", "state") if runtime == "hermes" else ("receipt",)
        ingestion_records[suite_job_id] = {
            **{field: str(seed_paths[field]) for field in fields},
            **{field + "_sha256": _sha256(seed_paths[field]) for field in fields},
            "provider_identity": identity,
        }

    if purpose == "final_results" and len(supported) != 15:
        raise PlanError(f"the plan must contain exactly 15 launch configurations; found {len(supported)}")
    if not generated:
        raise PlanError("the plan has no ready supported configurations")
    if any("azure-luna" in item["resources"] for item in generated) and resource_limits.get("azure-luna", 0) < 2:
        raise PlanError("resource_limits.azure-luna must be at least 2 for Luna-consuming cells")

    suite = {
        "suite_version": 1,
        "created_by": "reference.plan",
        "purpose": purpose,
        "test_ids": tests["ids"],
        "published_test_sha256": tests["sha256"],
        "global_limit": global_limit,
        "resource_limits": resource_limits,
        "jobs": [
            {
                "id": item["id"],
                "manifest": item["manifest_relative"],
                "action": "test-only",
                "resources": item["resources"],
            }
            for item in generated
        ],
    }
    suite_path = output_root / "modal_suite.json"
    _write_json(suite_path, suite)
    try:
        validated_suite = local_suite.load_suite(suite_path)
    except local_suite.SuiteError as exc:
        raise PlanError(f"generated Modal suite failed local validation: {exc}") from exc
    if len(validated_suite.jobs) != len(generated):
        raise PlanError("generated Modal suite does not contain one job per prepared manifest")

    plan = {
        "plan_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "phase": "prepared_offline",
        "paid_calls_made": False,
        "purpose": purpose,
        "persona": persona,
        "memory_providers": list(memory_providers),
        "judge": dict(CURRENT_JUDGE),
        "published_tests": tests,
        "published_test_runtime": test_runtime,
        "completed_ingestions": ingestion_records,
        "runtime_configurations": runtimes,
        "supported_cells": supported,
        "ready_cells": generated,
        "unsupported_cells": unsupported,
        "modal_suite": str(suite_path),
        "global_limit": global_limit,
        "resource_limits": resource_limits,
    }
    plan_path = output_root / "final_evaluation_plan.json"
    _write_json(plan_path, plan)
    _write_json(output_root / "unsupported_cells.json", {"cells": unsupported})
    return {
        "plan": plan_path,
        "suite": suite_path,
        "unsupported": output_root / "unsupported_cells.json",
        "supported_count": len(supported),
        "ready_count": len(generated),
        "unsupported_count": len(unsupported),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="JSON or YAML plan input")
    args = parser.parse_args(argv)
    try:
        result = build_plan(args.config)
    except (PlanError, matrix.PreparationError, matrix.ManifestDriftError) as exc:
        parser.error(str(exc))
    print(json.dumps({key: str(value) if isinstance(value, Path) else value for key, value in result.items()}, indent=2, sort_keys=True))
    return 0


from reference.runtimes.claude_code import PERSONA
from reference.runtimes.claude_code import load_morgan_source_messages
from reference.runtimes.claude_code import _source_ids_sha256


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / ("construction/v2/morgan/release/500k_final/checkpoint/life_sim.yaml"
                 if PERSONA == "morgan" else f"construction/v2/{PERSONA}/release/500k_final/life_sim.yaml")
PROVIDERS = ("builtin", "mem0", "honcho", "hindsight", "supermemory")
SOURCES = Path("/tmp/dolphinbench-claude-five-sources")
PLUGIN_PATHS = {
    "mem0": SOURCES / "mem0/integrations/claude-code-plugin",
    "honcho": SOURCES / "honcho/plugins/honcho/.stage",
    "hindsight": SOURCES / "hindsight/hindsight-integrations/coding-agents",
    "supermemory": SOURCES / "supermemory-plugin/plugin",
}
PLUGIN_TOOLS = {
    "builtin": [],
    "mem0": ["mcp__plugin_mem0_mem0__*"],
    "honcho": ["mcp__plugin_honcho_honcho__*"],
    "hindsight": ["mcp__hindsight__*"],
    "supermemory": ["mcp__plugin_supermemory_supermemory__*"],
}


def source_hash(path: Path) -> str:
    """Record the installed files rather than trusting a version label alone."""
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if not item.is_file() or any(p in {".git", "__pycache__", "node_modules"} for p in item.relative_to(path).parts):
            continue
        digest.update(str(item.relative_to(path)).encode() + b"\0")
        digest.update(item.read_bytes())
    return digest.hexdigest()


def condition(provider: str, run_name: str) -> dict[str, Any]:
    if provider not in PROVIDERS or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,48}", run_name):
        raise ValueError("Use a known provider and a lowercase run name with letters, numbers, or hyphens")
    name = f"dolphinbench-claude-{run_name}-{provider}"
    home = f"/home/{name}"
    locations = {
        "builtin": "Claude files in its own container",
        "mem0": "Hosted Mem0, new Claude-only user and project",
        "honcho": "Existing private Honcho server, new Claude-only workspace and peers",
        "hindsight": "New Hindsight database in this condition's Modal container",
        "supermemory": "New Supermemory database and official MCP service in this condition's Modal container",
    }
    job = {
        "provider": provider,
        "container_name": name,
        "volume_name": name,
        "namespace": name,
        "uid": 2000 + PROVIDERS.index(provider) + 1,
        "home": home,
        "project": f"{home}/project",
        "config_dir": f"{home}/.claude",
        "auto_memory": f"{home}/.claude/auto-memory",
        "controller_files": "/root/dolphinbench-controller",
        "store_location": locations[provider],
        "allowed_memory_tools": PLUGIN_TOOLS[provider],
        "model": "claude-sonnet-5",
        "claude_version": "2.1.259",
        "cpu_request": 4 if provider == "supermemory" else 2 if provider == "hindsight" else 1,
        "memory_request_mib": {"hindsight": 6144, "supermemory": 8192}.get(provider, 2048),
        "gpu": False,
        "agent_call_timeout": None,
        "messages_at_once": 1,
        "reuse_hermes_store": False,
        "native_memory_enabled": True,
    }
    if provider == "builtin":
        job.update(
            volume_name=("dolphinbench-claude-native-seed-v1" if PERSONA == "morgan"
                         else f"dolphinbench-claude-{PERSONA}-native-seed-v1"),
            home="/home/claude-native",
            project="/tmp/dolphinbench-claude-native-memory-project",
            config_dir="/tmp/dolphinbench-claude-native-<run>/claude-config",
            auto_memory="/tmp/dolphinbench-claude-native-<run>/claude-config/auto-memory",
            cpu_request=2, memory_request_mib=4096,
        )
    return job


from reference.runtimes.claude_code import child_environment


def build_claude_ingestion_plan(run_name: str, corpus: Path = CORPUS, *, supermemory_sessionend: bool = False,
               capture_supermemory: bool = False) -> dict[str, Any]:
    messages, corpus_hash = load_morgan_source_messages(corpus)
    jobs = [condition(provider, run_name) for provider in PROVIDERS]
    if supermemory_sessionend:
        jobs = [job for job in jobs if job["provider"] == "supermemory"]
    if supermemory_sessionend or capture_supermemory:
        next(job for job in jobs if job["provider"] == "supermemory")["capture_session_end"] = True
        if PERSONA != "morgan":
            prompts = {
                "alex": ("Where did I have a cortado, and who was home for leftovers that evening?",
                         ["7th", "devika"]),
                "riley": ("What was my lake outing like, and what painting activity did I describe afterward?",
                          ["lake", "watercolor"]),
            }
            prompt, terms = prompts[PERSONA]
            next(job for job in jobs if job["provider"] == "supermemory").update(
                retrieval_probe_prompt=prompt, retrieval_probe_terms=terms)
    for job in jobs:
        source = PLUGIN_PATHS.get(job["provider"])
        if source is not None:
            if not source.is_dir():
                raise ValueError(f"Missing official plugin source: {source}")
            job["plugin_source"] = str(source)
            job["plugin_files_sha256"] = source_hash(source)
    return {
        "status": "awaiting_remote_verification_and_launch_approval",
        "run_name": run_name,
        "corpus": str(corpus.resolve()),
        "corpus_sha256": corpus_hash,
        "source_count": len(messages),
        "source_ids_sha256": _source_ids_sha256(messages),
        "message_format": "[<original ISO-8601 timestamp>] <original message text>",
        "first_timestamp": messages[0].timestamp,
        "last_timestamp": messages[-1].timestamp,
        "container_count": len(jobs),
        "start_all_conditions_together": not supermemory_sessionend,
        "maximum_total_modal_containers": 100,
        "runner_files_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "reference/execution/claude.py", "reference/runtimes/claude_history.py",
                "reference/runtimes/claude_code.py",
                "reference/execution/claude_native.py", "harness/claude_driver.py",
                "harness/claude_plugin_setup.py", "reference/memory/hindsight.py",
                "reference/runtimes/claude_recovery.py",
                "reference/execution/hermes.py",
            )
        },
        "conditions": jobs,
        "remaining_before_launch": [
            "Build and verify the exact Modal images, including actual database backup and restore commands",
            "Verify available Modal capacity and subscription authentication before approved launch",
        ],
    }


def claude_ingestion_main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = build_claude_ingestion_plan(args.run_name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise ValueError("Choose a new output path; the existing plan is unchanged")
    args.output.write_text(json.dumps(plan, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "messages_per_condition": plan["source_count"], "conditions": list(PROVIDERS), "launched": False}))

if __name__ == "__main__":
    raise SystemExit(main())
