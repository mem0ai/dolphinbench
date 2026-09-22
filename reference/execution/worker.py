"""Reference worker implementation."""

from __future__ import annotations

from contextlib import closing
import asyncio
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from uuid import uuid4
from hashlib import sha256
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


import modal
import yaml

from reference.execution.images import claude_binary, hermes_source


APP_NAME = "dolphinbench-evaluation"
BENCH_ROOT = Path("/home/ubuntu/.hermes/benchmark/hbme")
HERMES_ROOT = Path("/home/ubuntu/.hermes/hermes-agent")
MOCK_MCP_ROOT = Path("/opt/dolphinbench-mock-mcp")
MOCK_MCP_PYTHON = MOCK_MCP_ROOT / "bin" / "python"
INPUT_ROOT = Path("/mnt/dolphinbench-inputs")
RESULTS_ROOT = Path("/mnt/dolphinbench-results")
INPUT_VOLUME_NAME = "dolphinbench-evaluation-inputs"
RESULTS_VOLUME_NAME = "dolphinbench-evaluation-results"
SECRET_NAME = "dolphinbench-evaluation"
OPENROUTER_SECRET_NAME = "dolphinbench-openrouter"
MEMORY_SERVICE_SECRET_NAME = "dolphinbench-memory-services"
TAILSCALE_AUTH_KEY_ENV = "TS_AUTHKEY"
TAILSCALE_SOCKS_PROXY = "socks5h://localhost:1080"
LOCAL_PROXY_BYPASS = "127.0.0.1,localhost,::1"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ACTIONS = frozenset({"seed", "launch", "test-only", "resume", "resume-seed"})
_SNAPSHOT_INTERVAL_SECONDS = 300
_STATUS_COMMIT_INTERVAL_SECONDS = 60
_VOLUME_COMMIT_ATTEMPTS = 6
_PROFILE_SECRET_NAMES = frozenset({".env", "auth.json", "credentials.json", "tokens.json"})
_TAILSCALE_MEMORY_PROVIDERS = frozenset({"honcho"})
_MODAL_MEMORY_PROVIDERS = frozenset({"hindsight", "supermemory"})
_SELF_HOSTED_MEMORY_PROVIDERS = _TAILSCALE_MEMORY_PROVIDERS | _MODAL_MEMORY_PROVIDERS
_TAILSCALE_IPV4_RANGE = ip_network("100.64.0.0/10")
_MODAL_SERVICE_HOST_SUFFIXES = {
    "hindsight": (
        "--dolphinbench-memory-services-hindsight-service.modal.run",
        "--dolphinbench-hindsight-final.modal.run",
        "--dolphinbench-minimax-m3-morgan-hindsight.modal.run",
    ),
    "supermemory": "--dolphinbench-memory-services-supermemory-service.modal.run",
}

LOCAL_ROOT = Path(__file__).resolve().parents[2]
LOCAL_CLAUDE_CODE_VERSION = "2.1.259"
REQUIRED_CLAUDE_CODE_VERSION = "2.1.259"


def _require_local_file(path: Path) -> str:
    if modal.is_local() and not path.is_file():
        raise RuntimeError(f"required local file is missing: {path}")
    return str(path)


def _require_local_dir(path: Path) -> str:
    if modal.is_local() and not path.is_dir():
        raise RuntimeError(f"required local directory is missing: {path}")
    return str(path)


base_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates", "curl", "git", "tar")
    .pip_install_from_requirements(str(LOCAL_ROOT / "requirements.txt"))
    .pip_install_from_requirements(str(LOCAL_ROOT / "reference/requirements.txt"))
    .add_local_dir(
        _require_local_dir(LOCAL_ROOT / "harness"),
        str(BENCH_ROOT / "harness"),
        copy=True,
        ignore=["**/__pycache__/**", "**/*.pyc"],
    )
    .add_local_dir(
        _require_local_dir(LOCAL_ROOT / "mock_mcp"),
        str(BENCH_ROOT / "mock_mcp"),
        copy=True,
        ignore=["**/__pycache__/**", "**/*.pyc"],
    )
    .add_local_dir(
        _require_local_dir(LOCAL_ROOT / "graders"),
        str(BENCH_ROOT / "graders"),
        copy=True,
        ignore=["**/__pycache__/**", "**/*.pyc"],
    )
    .add_local_dir(
        _require_local_dir(LOCAL_ROOT / "reference"),
        str(BENCH_ROOT / "reference"),
        copy=True,
        ignore=["**/__pycache__/**", "**/*.pyc"],
    )
    .add_local_dir(
        _require_local_dir(LOCAL_ROOT / "pricing"),
        str(BENCH_ROOT / "pricing"),
        copy=True,
        ignore=["**/__pycache__/**", "**/*.pyc"],
    )
    .add_local_file(
        _require_local_file(LOCAL_ROOT / "construction" / "__init__.py"),
        str(BENCH_ROOT / "construction" / "__init__.py"),
        copy=True,
    )
    .add_local_file(
        _require_local_file(LOCAL_ROOT / "construction" / "checkpoints.py"),
        str(BENCH_ROOT / "construction" / "checkpoints.py"),
        copy=True,
    )
    .add_local_file(
        _require_local_file(LOCAL_ROOT / "construction" / "construction_io.py"),
        str(BENCH_ROOT / "construction" / "construction_io.py"),
        copy=True,
    )
    .run_commands(
        f"python -m venv '{MOCK_MCP_ROOT}'",
        f"'{MOCK_MCP_ROOT}/bin/pip' install mcp==1.29.0 pyyaml==6.0.3",
        f"mkdir -p '{BENCH_ROOT}'",
    )
    .env({
        "PATH": "/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": str(BENCH_ROOT), "HOME": "/home/ubuntu",
    })
)


def runtime_image(runtime: str) -> Any:
    """Package only the runtime selected by a prepared manifest."""
    if runtime == "hermes":
        return (
            base_image.add_local_dir(
                str(hermes_source()), str(HERMES_ROOT), copy=True,
                ignore=[".git/**", ".venv/**", "venv/**", "**/__pycache__/**", "**/*.pyc", "logs/**"],
            )
            .run_commands(
                f"python -m pip install -e '{HERMES_ROOT}[mcp,honcho]'",
                f"mkdir -p '{HERMES_ROOT}/venv/bin'",
                f"ln -sf /usr/local/bin/python '{HERMES_ROOT}/venv/bin/python'",
                f"ln -sf /usr/local/bin/python '{HERMES_ROOT}/venv/bin/python3'",
                f"ln -sf /usr/local/bin/hermes '{HERMES_ROOT}/venv/bin/hermes'",
            )
            .env({"PATH": f"/home/ubuntu/.local/bin:{HERMES_ROOT}/venv/bin:/usr/local/bin:/usr/bin:/bin",
                  "PYTHONPATH": os.pathsep.join((str(BENCH_ROOT), str(HERMES_ROOT)))})
        )
    if runtime == "claude":
        return (
            base_image.add_local_file(str(claude_binary()), "/home/ubuntu/.local/bin/claude", copy=True)
            .run_commands("chmod 0755 /home/ubuntu/.local/bin/claude", "mkdir -p /home/ubuntu/.claude")
        )
    raise ValueError(f"unsupported worker runtime: {runtime}")


def private_network_image(image: Any) -> Any:
    return (
        image
        .run_commands(
            "set -eu; archive=/tmp/tailscale_1.102.2_amd64.tgz; "
            "curl -fsSL https://pkgs.tailscale.com/stable/tailscale_1.102.2_amd64.tgz -o \"$archive\"; "
            "echo \"ad2cde12f8de95f7b93a1e0401e652291c603d42b9d60a33fb1741eb38ab04d8  $archive\" "
            "| sha256sum --check --strict; "
            "tar -xzf \"$archive\" -C /tmp; "
            "install -m 0755 /tmp/tailscale_1.102.2_amd64/tailscale /usr/local/bin/tailscale; "
            "install -m 0755 /tmp/tailscale_1.102.2_amd64/tailscaled /usr/local/bin/tailscaled; "
            "rm -rf \"$archive\" /tmp/tailscale_1.102.2_amd64",
        )
        .add_local_file(
            _require_local_file(LOCAL_ROOT / 'reference' / 'execution' / 'tailscale_entrypoint.sh'),
            "/root/tailscale-entrypoint.sh",
            copy=True,
        )
        .run_commands("chmod 0755 /root/tailscale-entrypoint.sh")
        .env({
            "ALL_PROXY": TAILSCALE_SOCKS_PROXY,
            "HTTP_PROXY": "http://localhost:1080",
            "HTTPS_PROXY": "http://localhost:1080",
            "NO_PROXY": LOCAL_PROXY_BYPASS,
            "no_proxy": LOCAL_PROXY_BYPASS,
        })
        .entrypoint(["/root/tailscale-entrypoint.sh"])
    )


app = modal.App(APP_NAME)
input_volume = modal.Volume.from_name(INPUT_VOLUME_NAME, create_if_missing=True)
results_volume = modal.Volume.from_name(RESULTS_VOLUME_NAME, create_if_missing=True)
evaluation_secret = modal.Secret.from_name(
    SECRET_NAME, required_keys=["AZURE_OPENAI_API_KEY"],
)
private_network_evaluation_secret = modal.Secret.from_name(
    SECRET_NAME,
    required_keys=["AZURE_OPENAI_API_KEY", TAILSCALE_AUTH_KEY_ENV],
)
memory_service_secret = modal.Secret.from_name(
    MEMORY_SERVICE_SECRET_NAME,
    required_keys=["HINDSIGHT_API_KEY", "SUPERMEMORY_API_KEY"],
)
openrouter_evaluation_secret = modal.Secret.from_name(
    OPENROUTER_SECRET_NAME,
    required_keys=["OPENROUTER_API_KEY", "MINIMAX_OPENROUTER_API_KEY"],
)




def _configure_agent_credentials(manifest: dict[str, Any]) -> None:
    agent = manifest.get("agent") or {}
    if agent.get("provider") != "openrouter":
        return
    target = str(agent.get("api_key_env") or "").strip()
    if not target:
        raise RuntimeError("OpenRouter manifest has no agent API key environment name")
    key = (
        os.environ.get(target, "").strip()
        or os.environ.get("OPENROUTER_API_KEY", "").strip()
        or os.environ.get("MINIMAX_OPENROUTER_API_KEY", "").strip()
    )
    if not key:
        raise RuntimeError("OpenRouter evaluation worker received no OpenRouter API key")
    os.environ[target] = key


def _profile_memory_service_url(profile: str, provider: str) -> str | None:
    """Read the fixed service URL from one restored Hermes profile."""
    root = Path.home() / ".hermes" / "profiles" / profile
    relative_paths = {
        "honcho": (Path("honcho.json"), ("baseUrl", "base_url")),
        "hindsight": (
            Path("hindsight") / "config.json",
            ("base_url", "baseUrl", "api_url"),
        ),
        "supermemory": (Path("supermemory.json"), ("base_url", "baseUrl")),
    }
    path, keys = relative_paths[provider]
    try:
        payload = json.loads((root / path).read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{provider} profile has invalid JSON at {root / path}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{provider} profile configuration at {root / path} must be a JSON object")
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _configured_memory_service_endpoints(
    manifest: dict[str, Any],
    providers: frozenset[str] = _SELF_HOSTED_MEMORY_PROVIDERS,
) -> list[tuple[str, str]]:
    """Return configured endpoints for the requested memory providers."""
    endpoints: list[tuple[str, str]] = []
    for job in manifest.get("jobs") or []:
        provider = job.get("configuration")
        if provider not in providers:
            continue
        if not isinstance(provider, str):
            continue
        gateway = job.get("gateway_config") or {}
        if not isinstance(gateway, dict):
            raise RuntimeError(f"{provider} job has invalid memory service configuration")
        keys = {
            "honcho": ("honcho_base_url", "base_url"),
            "hindsight": ("hindsight_base_url", "base_url"),
            "supermemory": ("supermemory_base_url", "base_url"),
        }[provider]
        endpoint = next(
            (
                value.strip()
                for key in keys
                if isinstance((value := gateway.get(key)), str) and value.strip()
            ),
            None,
        )
        if endpoint is None:
            profile = job.get("profile")
            if isinstance(profile, str) and profile:
                endpoint = _profile_memory_service_url(profile, provider)
        if endpoint is None:
            raise RuntimeError(
                f"{provider} job has no configured memory service endpoint"
            )
        endpoints.append((provider, endpoint))
    return endpoints


def _require_private_tailscale_endpoint(provider: str, endpoint: str) -> None:
    """Reject endpoints that would send a supposedly private request to the public internet."""
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(
            f"{provider} memory service endpoint must be a full http or https URL"
        )
    if parsed.username or parsed.password:
        raise RuntimeError(f"{provider} memory service endpoint must not contain credentials")
    host = parsed.hostname.rstrip(".").lower()
    try:
        private = ip_address(host) in _TAILSCALE_IPV4_RANGE
    except ValueError:
        private = host.endswith(".ts.net")
    if not private:
        raise RuntimeError(
            f"{provider} memory service endpoint must use a private Tailscale address"
        )


def _verify_tailscale_memory_service_endpoints(manifest: dict[str, Any]) -> list[str]:
    """Verify the required Honcho service through the worker's Tailscale proxy."""
    endpoints = _configured_memory_service_endpoints(
        manifest, _TAILSCALE_MEMORY_PROVIDERS,
    )
    if len(endpoints) != 1:
        raise RuntimeError("a Tailscale evaluation worker must contain exactly one Honcho job")
    verified: list[str] = []
    for provider, endpoint in endpoints:
        _require_private_tailscale_endpoint(provider, endpoint)
        result = _output([
            "curl",
            "--silent",
            "--show-error",
            "--location",
            "--proxy",
            TAILSCALE_SOCKS_PROXY,
            "--connect-timeout",
            "10",
            "--max-time",
            "20",
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}",
            endpoint,
        ])
        status = str(result.get("stdout") or "")
        if not result.get("ok") or status == "000":
            detail = str(result.get("stderr") or result.get("error") or "connection failed")
            raise RuntimeError(
                f"cannot reach the configured {provider} memory service through Tailscale: {detail}"
            )
        verified.append(endpoint)
    return verified


def _can_restart_unstarted_test(manifest: dict[str, Any], result_root: Path) -> bool:
    return (
        (manifest.get("evaluation_scope") or {}).get("kind") == "test_only"
        and not any((result_root / name).exists() for name in ("output", "stdout.log", "stderr.log"))
    )


def _require_modal_service_endpoint(provider: str, endpoint: str) -> None:
    """Allow only the HTTPS URLs created by the approved Modal deployment."""

    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError(f"{provider} memory service endpoint must be an https URL")
    if parsed.username or parsed.password:
        raise RuntimeError(f"{provider} memory service endpoint must not contain credentials")
    host = parsed.hostname.rstrip(".").lower()
    if not host.endswith(_MODAL_SERVICE_HOST_SUFFIXES[provider]):
        raise RuntimeError(
            f"{provider} memory service endpoint must name the approved Modal service"
        )


def _verify_modal_memory_service_endpoints(manifest: dict[str, Any]) -> list[str]:
    """Verify Hindsight or Supermemory directly from the evaluation worker."""

    endpoints = _configured_memory_service_endpoints(
        manifest, _MODAL_MEMORY_PROVIDERS,
    )
    if len(endpoints) != 1:
        raise RuntimeError(
            "a Modal memory evaluation worker must contain exactly one Hindsight or Supermemory job"
        )
    verified: list[str] = []
    for provider, endpoint in endpoints:
        _require_modal_service_endpoint(provider, endpoint)
        result = _output([
            "curl",
            "--silent",
            "--show-error",
            "--location",
            "--connect-timeout",
            "10",
            "--max-time",
            "180",
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}",
            endpoint.rstrip("/") + "/health",
        ], timeout_seconds=190)
        status = str(result.get("stdout") or "")
        if not result.get("ok") or status == "000":
            detail = str(result.get("stderr") or result.get("error") or "connection failed")
            raise RuntimeError(
                f"cannot reach the configured {provider} Modal memory service: {detail}"
            )
        verified.append(endpoint)
    return verified


def _output(command: list[str], *, timeout_seconds: int = 30) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except Exception as exc:
        return {"command": command, "ok": False, "error": str(exc)}
    return {
        "command": command,
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _app_tool_smoke(runtime: str = "hermes") -> dict[str, Any]:
    """Connect the real simulated-app server without calling a model."""
    with tempfile.TemporaryDirectory(prefix="dolphinbench-app-smoke-") as temporary:
        home = Path(temporary)
        config = {
            "mcp_servers": {
                "dolphinbench-apps": {
                    "command": str(MOCK_MCP_PYTHON),
                    "args": [str(BENCH_ROOT / "mock_mcp" / "server.py")],
                    "env": {
                        "ENACT_PERSONA": "morgan",
                        "ENACT_MOCK_MANIFEST": str(
                            BENCH_ROOT / "mock_mcp" / "manifests" / "morgan.yaml"
                        ),
                        "ENACT_STATE_PATH": str(home / "state.json"),
                        "ENACT_LOG_PATH": str(home / "calls.jsonl"),
                        "ENACT_RUN_ID": "modal-environment-smoke",
                    },
                }
            }
        }
        (home / "config.yaml").write_text(
            yaml.safe_dump(config, sort_keys=False),
            encoding="utf-8",
        )
        if runtime == "claude":
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            async def check_apps() -> dict[str, Any]:
                server = config["mcp_servers"]["dolphinbench-apps"]
                (home / "state.json").write_text("{}")
                parameters = StdioServerParameters(**server)
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        response = await session.list_tools()
                actual = {tool.name for tool in response.tools}
                expected = set(yaml.safe_load(Path(server["env"]["ENACT_MOCK_MANIFEST"]).read_text())["tools"])
                return {"ok": actual == expected, "app_tool_count": len(expected),
                        "registered_tool_count": len(actual), "missing_tools": sorted(expected - actual),
                        "unexpected_tools": sorted(actual - expected)}

            return asyncio.run(check_apps())
        check = """
import json
import logging
from pathlib import Path
import yaml
from hermes_cli.mcp_startup import set_mcp_server_filter, ensure_mcp_discovery_before_agent_build

set_mcp_server_filter('dolphinbench-apps')
ensure_mcp_discovery_before_agent_build(logger=logging.getLogger('smoke'), single_query=True)
from tools.mcp_tool import get_mcp_status, get_registered_mcp_server_names, mcp_prefixed_tool_name
from tools.registry import registry

status = get_mcp_status()
manifest = yaml.safe_load(Path(
    '/home/ubuntu/.hermes/benchmark/hbme/mock_mcp/manifests/morgan.yaml'
).read_text())
expected_app_tools = set(manifest['tools'])
registered_tools = set(registry.get_tool_names_for_toolset('mcp-dolphinbench-apps'))
expected_registered_tools = {
    mcp_prefixed_tool_name('dolphinbench-apps', name)
    for name in expected_app_tools
}
expected_registered_tools.update({
    mcp_prefixed_tool_name('dolphinbench-apps', name)
    for name in ('list_resources', 'read_resource', 'list_prompts', 'get_prompt')
})
missing_tools = sorted(expected_registered_tools - registered_tools)
unexpected_tools = sorted(registered_tools - expected_registered_tools)
result = {
    'ok': (
        not missing_tools
        and not unexpected_tools
        and 'dolphinbench-apps' in get_registered_mcp_server_names()
    ),
    'app_tool_count': len(expected_app_tools),
    'registered_tool_count': len(registered_tools),
    'missing_tools': missing_tools,
    'unexpected_tools': unexpected_tools,
    'status': status,
}
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if result['ok'] else 1)
"""
        env = os.environ.copy()
        env["HERMES_HOME"] = str(home)
        env.update(config["mcp_servers"]["dolphinbench-apps"]["env"])
        completed = subprocess.run(
            [sys.executable, "-c", check],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
            env=env,
        )
        server_log = home / "logs" / "mcp-stderr.log"
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
            "server_stderr": server_log.read_text(encoding="utf-8") if server_log.is_file() else "",
        }


def environment_smoke(runtime: str = "hermes") -> dict[str, Any]:
    """Verify the packaged runtime without contacting a model or memory service."""
    imports: dict[str, str] = {}
    for module_name in (
        "yaml", "pydantic", "mcp", "tiktoken", "mem0", "honcho",
        "hindsight_client", "supermemory",
    ):
        try:
            module = __import__(module_name)
            imports[module_name] = str(getattr(module, "__version__", "installed"))
        except Exception as exc:
            imports[module_name] = f"ERROR: {exc}"
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "benchmark_root_exists": BENCH_ROOT.is_dir(),
        "hermes_root_exists": HERMES_ROOT.is_dir(),
        "imports": imports,
        runtime: _output([runtime, "--version"]),
        "dolphinbench_apps": _app_tool_smoke(runtime),
    }


def private_network_smoke(endpoints: dict[str, str]) -> dict[str, Any]:
    """Confirm that one Tailscale worker can reach Honcho on this EC2 host."""

    expected = _TAILSCALE_MEMORY_PROVIDERS
    if set(endpoints) != expected:
        raise ValueError("endpoints must contain exactly honcho")
    jobs = []
    for provider in sorted(expected):
        jobs.append({
            "configuration": provider,
            "gateway_config": {
                f"{provider}_base_url": endpoints[provider],
            },
        })
    verified = _verify_tailscale_memory_service_endpoints({"jobs": jobs})
    return {"ok": True, "verified_endpoints": verified}


def _safe_bundle(bundle_name: str) -> Path:
    if not _SAFE_NAME.fullmatch(bundle_name):
        raise ValueError("bundle_name may contain only letters, numbers, '.', '_' and '-'")
    path = INPUT_ROOT / bundle_name
    if not path.is_file():
        raise FileNotFoundError(f"input bundle does not exist: {path}")
    return path


def _safe_input_archive(archive_name: str, *, label: str) -> Path:
    if not _SAFE_NAME.fullmatch(archive_name):
        raise ValueError(f"{label} archive name may contain only letters, numbers, '.', '_' and '-'")
    path = INPUT_ROOT / archive_name
    if not path.is_file():
        raise FileNotFoundError(f"{label} archive does not exist: {path}")
    return path


def _safe_profile_archive(archive_name: str) -> Path:
    return _safe_input_archive(archive_name, label="profile")


def _safe_extract(bundle: Path, destination: Path) -> None:
    destination = destination.resolve()
    with tarfile.open(bundle, "r:gz") as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if target != destination and destination not in target.parents:
                raise ValueError(f"bundle contains an unsafe path: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"bundle contains a link: {member.name}")
        archive.extractall(destination)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _safe_result_path(job_id: str, relative_path: str) -> Path:
    if not _SAFE_NAME.fullmatch(job_id):
        raise ValueError("job_id may contain only letters, numbers, '.', '_' and '-'")
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("result file path must stay inside the job result directory")
    return RESULTS_ROOT / job_id / relative


def _result_files(result_root: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(result_root.rglob("*")):
        if not path.is_file() or path.name in {"metadata.json", "status.json"}:
            continue
        digest = sha256(path.read_bytes()).hexdigest()
        files.append({
            "path": str(path.relative_to(result_root)),
            "bytes": path.stat().st_size,
            "sha256": digest,
        })
    return files


def _rate_limit_seen(*paths: Path) -> bool:
    markers = ("429", "rate limit", "rate-limit", "too many requests")
    for path in paths:
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="replace").lower()
        except OSError:
            continue
        if any(marker in text for marker in markers):
            return True
    return False


def _subscription_waiting_seen(*paths: Path) -> bool:
    markers = (
        "subscription limit",
        "subscription usage limit",
        "usage limit reached",
        "quota resets",
        "try again when your limit resets",
        "five-hour limit",
    )
    for path in paths:
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="replace").lower()
        except OSError:
            continue
        if any(marker in text for marker in markers):
            return True
    return False


def _persist_profile(manifest: dict[str, Any], result_root: Path) -> None:
    """Save the job's isolated Hermes profile so a later container can resume."""
    for job in manifest.get("jobs") or []:
        profile = job.get("profile")
        if not isinstance(profile, str) or not profile:
            continue
        source = Path.home() / ".hermes" / "profiles" / profile
        if source.is_dir():
            destination = result_root / "profiles" / profile
            shutil.copytree(source, destination, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("state.db", "state.db-journal",
                                                          "state.db-wal", "state.db-shm"))
            if (source / "state.db").is_file():
                with closing(sqlite3.connect((source / "state.db").as_uri() + "?mode=ro", uri=True)) as live:
                    with closing(sqlite3.connect(destination / "state.db")) as saved:
                        live.backup(saved)
                        if saved.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                            raise RuntimeError("saved Hermes database failed its integrity check")


def _restore_profile(manifest: dict[str, Any], result_root: Path) -> None:
    for job in manifest.get("jobs") or []:
        profile = job.get("profile")
        if not isinstance(profile, str) or not profile:
            continue
        source = result_root / "profiles" / profile
        if source.is_dir():
            destination = Path.home() / ".hermes" / "profiles" / profile
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination, dirs_exist_ok=True)


def _restore_input_profile(manifest: dict[str, Any], archive_name: str) -> None:
    """Restore one credential-free completed profile from the input volume."""
    from reference.runtimes.hermes import profile_config_hashes
    from reference.evaluate import _profile_hashes_match

    jobs = manifest.get("jobs") or []
    if len(jobs) != 1:
        raise RuntimeError("profile archives require a one-job manifest")
    job = jobs[0]
    profile = job.get("profile")
    if not isinstance(profile, str) or not profile:
        raise RuntimeError("profile archive was supplied for a job that has no Hermes profile")
    archive = _safe_profile_archive(archive_name)
    profiles_root = Path.home() / ".hermes" / "profiles"
    destination = profiles_root / profile
    if destination.exists():
        raise FileExistsError(f"input Hermes profile already exists: {destination}")
    staging = Path("/tmp/dolphinbench-profile")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    _safe_extract(archive, staging)
    children = list(staging.iterdir())
    if len(children) != 1 or children[0].name != profile or not children[0].is_dir():
        raise RuntimeError(f"profile archive does not contain exactly {profile!r}")
    if any(
        path.is_file() and path.name.lower() in _PROFILE_SECRET_NAMES
        for path in children[0].rglob("*")
    ):
        raise RuntimeError("profile archive contains credentials")
    expected_hashes = job.get("seed_profile_config_hashes")
    actual_hashes = profile_config_hashes(children[0])
    if not isinstance(expected_hashes, dict) or not _profile_hashes_match(
        expected_hashes, actual_hashes,
    ):
        raise RuntimeError("profile archive configuration does not match the prepared manifest")
    profiles_root.mkdir(parents=True, exist_ok=True)
    shutil.move(str(children[0]), str(destination))


def _configure_restored_profile_for_worker(
    manifest_path: Path, manifest: dict[str, Any],
) -> None:
    """Point the worker-local profile at this job's isolated simulated app files."""
    jobs = manifest.get("jobs") or []
    if len(jobs) != 1:
        raise RuntimeError("worker profile configuration requires a one-job manifest")
    job = jobs[0]
    profile = job.get("profile")
    if not isinstance(profile, str) or not profile:
        raise RuntimeError("worker profile configuration requires a Hermes profile")
    config_path = Path.home() / ".hermes" / "profiles" / profile / "config.yaml"
    try:
        config = yaml.safe_load(config_path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"cannot read restored Hermes profile configuration: {config_path}") from exc
    if not isinstance(config, dict):
        raise RuntimeError("restored Hermes profile configuration must be a YAML object")
    servers = config.get("mcp_servers")
    app_server = servers.get("dolphinbench-apps") if isinstance(servers, dict) else None
    if not isinstance(app_server, dict):
        raise RuntimeError("restored Hermes profile has no dolphinbench-apps server")
    root = Path(str(manifest.get("root") or ""))
    launch = manifest.get("launch") or {}
    mock_python = launch.get("mock_mcp_python")
    if not root.is_absolute() or not isinstance(mock_python, str) or not mock_python:
        raise RuntimeError("prepared manifest has incomplete simulated app runtime paths")
    persona = job.get("persona")
    run_id = job.get("run_id")
    required_job_paths = {
        "ENACT_STATE_PATH": job.get("state_path"),
        "ENACT_LOG_PATH": job.get("log_path"),
        "ENACT_TOOL_CONFIG_PATH": job.get("tool_config_path"),
    }
    if not isinstance(persona, str) or not persona or not isinstance(run_id, str) or not run_id:
        raise RuntimeError("prepared job has no persona or run ID")
    if any(not isinstance(value, str) or not Path(value).is_absolute() for value in required_job_paths.values()):
        raise RuntimeError("prepared job has incomplete simulated app file paths")
    # Completed profiles may retain an ingestion path absent from the test bundle.
    Path(required_job_paths["ENACT_STATE_PATH"]).parent.mkdir(parents=True, exist_ok=True)
    app_server["command"] = mock_python
    app_server["args"] = [str(root / "mock_mcp" / "server.py")]
    app_server["env"] = {
        "ENACT_PERSONA": persona,
        "ENACT_MOCK_MANIFEST": str(root / "mock_mcp" / "manifests" / f"{persona}.yaml"),
        **required_job_paths,
        "ENACT_RUN_ID": run_id,
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    from reference.runtimes.hermes import profile_config_hashes

    job["seed_profile_config_hashes"] = profile_config_hashes(config_path.parent)
    _write_json(manifest_path, manifest)


def _restore_native_memory_snapshot(manifest: dict[str, Any], archive_name: str) -> None:
    """Restore one immutable Claude native-memory archive for this worker."""
    from reference.runtimes import claude_code as native

    if LOCAL_CLAUDE_CODE_VERSION != REQUIRED_CLAUDE_CODE_VERSION:
        raise RuntimeError(
            "Claude native-memory evaluation requires Claude Code "
            f"{REQUIRED_CLAUDE_CODE_VERSION}; worker is pinned to {LOCAL_CLAUDE_CODE_VERSION}"
        )

    jobs = manifest.get("jobs") or []
    if len(jobs) != 1:
        raise RuntimeError("Claude native-memory archive requires a one-job manifest")
    job = jobs[0]
    if (
        job.get("agent_runtime") != "claude"
        or job.get("configuration") != "builtin"
        or job.get("provider") != "claude_native_memory"
    ):
        raise RuntimeError("native-memory archive was supplied for a non-Claude-built-in job")
    native_input = job.get("native_memory")
    if not isinstance(native_input, dict):
        raise RuntimeError("Claude built-in job has no native-memory metadata")
    archive = _safe_input_archive(archive_name, label="Claude native-memory")
    expected = native_input.get("archive_sha256")
    if not isinstance(expected, str) or sha256(archive.read_bytes()).hexdigest() != expected:
        raise RuntimeError("Claude native-memory archive does not match the prepared manifest")
    destination = Path("/tmp/dolphinbench-claude-native-seed")
    shutil.rmtree(destination, ignore_errors=True)
    native.restore_snapshot(archive_path=archive, config_dir=destination)
    identity = job.get("provider_identity")
    if not isinstance(identity, dict) or identity.get("kind") != "claude_native_auto_memory":
        raise RuntimeError("Claude built-in job has the wrong native-memory identity")
    identity["seed_config_dir"] = str(destination)
    gateway_config = job.setdefault("gateway_config", {})
    if not isinstance(gateway_config, dict):
        raise RuntimeError("Claude built-in job has invalid gateway configuration")
    gateway_config["claude_native_memory_seed_dir"] = str(destination)


def _commit_results_volume() -> None:
    """Retry transient Modal control-plane failures while publishing saved files."""
    for attempt in range(_VOLUME_COMMIT_ATTEMPTS):
        try:
            results_volume.commit()
            return
        except Exception:
            if attempt == _VOLUME_COMMIT_ATTEMPTS - 1:
                raise
            time.sleep(min(2 ** attempt, 30))


def _snapshot_resume_state(
    manifest: dict[str, Any], result_root: Path, output_root: Path,
) -> None:
    """Persist the latest safe matrix state before Modal can end the container."""
    if output_root.is_dir():
        shutil.copytree(output_root, result_root / "output", dirs_exist_ok=True)
    _persist_profile(manifest, result_root)
    _commit_results_volume()


def _ingestion_resume_root(result_root: Path) -> Path:
    if (result_root / "ingestion-recovery-required.json").exists():
        raise RuntimeError("interrupted ingestion requires inspection before resuming saved sources")
    pointer = result_root / "ingestion-checkpoint.json"
    if not pointer.exists():
        return result_root  # Previously saved runs predate separate checkpoints.
    name = json.loads(pointer.read_text())["directory"]
    if not isinstance(name, str) or not re.fullmatch(r"checkpoint-[0-9a-f]{32}", name):
        raise RuntimeError("invalid ingestion checkpoint directory")
    root = result_root / name
    receipt = json.loads((root / "checkpoint.json").read_text())
    for entry in receipt["files"]:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("invalid ingestion checkpoint file path")
        if sha256((root / relative).read_bytes()).hexdigest() != entry["sha256"]:
            raise RuntimeError(f"ingestion checkpoint file changed: {relative}")
    return root


def _save_ingestion_checkpoint(
    manifest: dict[str, Any], result_root: Path, output_root: Path,
) -> Path:
    """Called only before execution, at a held boundary, or after clean exit."""
    pointer = result_root / "ingestion-checkpoint.json"
    previous = json.loads(pointer.read_text()) if pointer.exists() else None
    checkpoints = sorted(
        (path for path in result_root.glob("checkpoint-*") if path.is_dir()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    current_name = (previous or {}).get("directory")
    fallback = next((path for path in checkpoints if path.name != current_name), None)
    stale = [
        path for path in checkpoints
        if path.name != current_name and path != fallback
    ]
    if stale:
        for path in stale:
            shutil.rmtree(path)
        _commit_results_volume()
    for attempt in range(3):
        destination = result_root / f"checkpoint-{uuid4().hex}"
        try:
            destination.mkdir()
            if output_root.is_dir():
                shutil.copytree(output_root, destination / "output")
            _persist_profile(manifest, destination)
            _write_json(destination / "checkpoint.json", {"files": _result_files(destination)})
            # Publish only after the entire new save is durable. A failed save
            # cannot overwrite the preceding database, memory, or progress.
            try:
                results_volume.commit()
                _write_json(pointer, {"directory": destination.name})
                results_volume.commit()
            except Exception as exc:
                raise OSError("could not commit ingestion checkpoint") from exc
        except (OSError, shutil.Error):
            if previous is None:
                pointer.unlink(missing_ok=True)
            else:
                _write_json(pointer, previous)
            # The pointer never names this candidate, so retaining a partial
            # copy only consumes space needed by the next attempt.
            shutil.rmtree(destination, ignore_errors=True)
            try:
                results_volume.commit()
            except Exception:
                # The next successful commit will also publish this deletion.
                pass
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
            continue
        keep = {destination.name, (previous or {}).get("directory")}
        removed_old = False
        for old in result_root.glob("checkpoint-*"):
            if old.is_dir() and old.name not in keep:
                shutil.rmtree(old, ignore_errors=True)
                removed_old = True
        if removed_old:
            results_volume.commit()
        return destination
    raise AssertionError("unreachable")


def _prune_completed_ingestion_saves(result_root: Path, saved: Path) -> None:
    for checkpoint in result_root.glob("checkpoint-*"):
        if checkpoint.is_dir() and checkpoint != saved:
            shutil.rmtree(checkpoint)
    for diagnostics in result_root.glob("failed-ingestion-*"):
        if diagnostics.is_dir():
            shutil.rmtree(diagnostics)


def _seed_checkpoint_runner(workspace: Path, runner: str, control: Path) -> Path:
    original = Path(runner).resolve()
    if workspace.resolve() not in original.parents or not original.is_file():
        raise ValueError("ingestion runner must be a file in the saved workspace")
    wrapper = workspace.parent / "run_ingestion_with_checkpoints.py"
    wrapper.write_text(
        "import runpy\n"
        f"run = runpy.run_path({str(BENCH_ROOT / 'reference' / 'ingest.py')!r})['run']\n"
        f"raise SystemExit(run({str(original)!r}, {str(control)!r}))\n",
        encoding="utf-8",
    )
    return wrapper


def _publish_completed_ingestion(saved: Path, result_root: Path) -> None:
    # This is only a download-layout copy of an already durable checkpoint.
    for attempt in range(3):
        try:
            for name in ("output", "profiles"):
                if (saved / name).is_dir():
                    shutil.copytree(saved / name, result_root / name, dirs_exist_ok=True)
            try:
                results_volume.commit()
            except Exception as exc:
                raise OSError("could not commit completed ingestion files") from exc
            break
        except (OSError, shutil.Error):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def _restore_bundled_hermes_source(workspace: Path) -> Path:
    """Copy the hash-bound Hermes source tree to a fresh executable location."""
    source = workspace / "external" / "01-hermes_code"
    destination = Path("/tmp/dolphinbench-hermes-source")
    if not source.is_dir():
        raise FileNotFoundError("input bundle has no saved Hermes source tree")
    shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(source, destination)
    if not (destination / "hermes_cli" / "main.py").is_file():
        raise RuntimeError("saved Hermes source tree has no console entry point")
    launcher = destination / "hermes"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "from hermes_cli.main import main\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


def _is_hermes_manifest(manifest: dict[str, Any]) -> bool:
    jobs = manifest.get("jobs") or []
    runtimes = {
        str(job.get("agent_runtime") or manifest.get("agent_runtime") or "hermes")
        for job in jobs
        if isinstance(job, dict)
    }
    return not runtimes or runtimes == {"hermes"}


def _test_only_runner(workspace: Path, runner: str) -> Path:
    """Keep ingestion disabled when the saved matrix runner requests --resume."""
    original = Path(runner).resolve()
    if workspace.resolve() not in original.parents or not original.is_file():
        raise ValueError("test runner must be a file in the saved workspace")
    wrapper = workspace.parent / "run_tests_only.py"
    wrapper.write_text(
        "import runpy, sys\n"
        f"sys.argv[0] = {str(original)!r}\n"
        "if '--skip-seeding' not in sys.argv:\n"
        "    sys.argv.append('--skip-seeding')\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n",
        encoding="utf-8",
    )
    return wrapper


def _job_input_identity(
    *,
    bundle_name: str,
    manifest_relative_path: str,
    test_ids: list[str] | None,
    profile_archive_name: str | None,
    native_memory_archive_name: str | None,
) -> dict[str, Any]:
    """Return the immutable inputs that must match before a job can resume."""
    return {
        "bundle_name": bundle_name,
        "manifest_relative_path": manifest_relative_path,
        "test_ids": test_ids or [],
        "profile_archive_name": profile_archive_name,
        "native_memory_archive_name": native_memory_archive_name,
    }


def _saved_input_identity(metadata: dict[str, Any]) -> dict[str, Any] | None:
    identity = metadata.get("input_identity")
    if isinstance(identity, dict):
        return identity
    # Metadata written before restart support still has these fields at top level.
    required = (
        "bundle_name", "manifest_relative_path", "test_ids",
        "profile_archive_name", "native_memory_archive_name",
    )
    if all(key in metadata for key in required):
        return {key: metadata[key] for key in required}
    return None


def _validate_saved_inputs(metadata_path: Path, expected: dict[str, Any]) -> dict[str, Any]:
    try:
        metadata = json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("existing result has unreadable metadata; cannot validate inputs") from exc
    if not isinstance(metadata, dict):
        raise RuntimeError("existing result has invalid metadata; cannot validate inputs")
    saved = _saved_input_identity(metadata)
    if saved is None:
        raise RuntimeError("existing result has no input identity; cannot validate inputs")
    if saved != expected:
        raise RuntimeError("existing result inputs do not match this job")
    return metadata


def _saved_result(metadata: dict[str, Any], result_root: Path) -> dict[str, Any]:
    return {
        "ok": True,
        "job_id": metadata["job_id"],
        "returncode": metadata.get("returncode"),
        "error": metadata.get("error"),
        "result_path": str(result_root),
        "status": "completed",
        "rate_limited": bool(metadata.get("rate_limited", False)),
        "subscription_waiting": bool(metadata.get("subscription_waiting", False)),
    }


def _run_prepared_job(
    *,
    bundle_name: str,
    job_id: str,
    manifest_relative_path: str,
    action: str,
    test_ids: list[str] | None = None,
    profile_archive_name: str | None = None,
    native_memory_archive_name: str | None = None,
    memory_connection: str,
) -> dict[str, Any]:
    """Run one immutable single-job manifest and preserve all output."""
    if not _SAFE_NAME.fullmatch(job_id):
        raise ValueError("job_id may contain only letters, numbers, '.', '_' and '-'")
    if action not in _ACTIONS:
        raise ValueError(f"unsupported matrix action: {action}")
    if Path(manifest_relative_path).is_absolute() or ".." in Path(manifest_relative_path).parts:
        raise ValueError("manifest_relative_path must stay inside the input bundle")

    bundle = _safe_bundle(bundle_name)
    input_identity = _job_input_identity(
        bundle_name=bundle_name,
        manifest_relative_path=manifest_relative_path,
        test_ids=test_ids,
        profile_archive_name=profile_archive_name,
        native_memory_archive_name=native_memory_archive_name,
    )
    work_root = Path("/tmp/dolphinbench-job")
    shutil.rmtree(work_root, ignore_errors=True)
    work_root.mkdir(parents=True)
    result_root = RESULTS_ROOT / job_id
    metadata_path = result_root / "metadata.json"
    execution_action = action
    if result_root.exists():
        if not metadata_path.is_file():
            raise RuntimeError("existing result has no metadata; cannot validate inputs")
        previous = _validate_saved_inputs(metadata_path, input_identity)
        if previous.get("status") == "completed":
            return _saved_result(previous, result_root)
        if action == "test-only" and previous.get("action") == "test-only":
            execution_action = "resume"
        elif action not in {"resume", "resume-seed"}:
            raise FileExistsError(f"result already exists: {result_root}")
    result_root.mkdir(parents=True, exist_ok=True)
    stdout_path = result_root / "stdout.log"
    stderr_path = result_root / "stderr.log"
    status_path = result_root / "status.json"
    started = time.time()
    returncode: int | None = None
    error: str | None = None
    manifest: dict[str, Any] | None = None
    rate_limited = False
    subscription_waiting = False
    prior_attempts = 0
    verified_memory_endpoints: list[str] = []
    ingestion = action in {"seed", "resume-seed"}
    control = work_root / "checkpoint-control"
    process: subprocess.Popen | None = None
    if metadata_path.is_file():
        try:
            prior_attempts = int(json.loads(metadata_path.read_text()).get("attempts", 0))
        except (ValueError, TypeError, json.JSONDecodeError):
            prior_attempts = 0
    # Save identity before unpacking or starting the subprocess so a replacement
    # worker can validate and recover from a hard container interruption.
    _write_json(metadata_path, {
        "job_id": job_id,
        "input_identity": input_identity,
        "bundle_name": bundle_name,
        "manifest_relative_path": manifest_relative_path,
        "action": action,
        "test_ids": test_ids or [],
        "profile_archive_name": profile_archive_name,
        "native_memory_archive_name": native_memory_archive_name,
        "attempts": prior_attempts,
        "status": "running",
    })
    results_volume.commit()
    _write_json(status_path, {
        "job_id": job_id,
        "status": "running",
        "action": action,
        "started_unix": started,
        "rate_limited": False,
        "subscription_waiting": False,
    })

    try:
        _safe_extract(bundle, work_root)
        workspace = work_root / "workspace"
        if not workspace.is_dir():
            raise FileNotFoundError("input bundle has no workspace directory")
        manifest_path = (work_root / manifest_relative_path).resolve()
        if work_root.resolve() not in manifest_path.parents or not manifest_path.is_file():
            raise FileNotFoundError(f"manifest is missing from bundle: {manifest_relative_path}")
        manifest = json.loads(manifest_path.read_text())
        agent_environment = {}
        if _is_hermes_manifest(manifest):
            from reference.execution.agent_container import image_backend
            agent_image = manifest.get("runtime_provenance", {}).get("agent_container", {}).get("image")
            if image_backend(agent_image) != "modal-sandbox":
                raise ValueError("Hermes Modal jobs require a recorded Modal agent image ID")
            agent_environment["DOLPHINBENCH_AGENT_IMAGE"] = agent_image
        _configure_agent_credentials(manifest)
        if ingestion and (len(manifest.get("jobs") or []) != 1 or not _is_hermes_manifest(manifest)):
            raise RuntimeError("ingestion checkpoints require one Hermes job")
        if native_memory_archive_name:
            if action not in {"test-only", "resume"}:
                raise ValueError("Claude native-memory archives are only valid for test-only work")
            _restore_native_memory_snapshot(manifest, native_memory_archive_name)
        launch = manifest.get("launch") or {}
        bundled_hermes: Path | None = None
        if action in {"test-only", "resume"} and _is_hermes_manifest(manifest):
            bundled_hermes = _restore_bundled_hermes_source(workspace)
        # The interpreter belongs to the immutable worker image. Test-only
        # work runs the hash-bound Hermes CLI copied from its input bundle.
        launch.update({
            "hermes_bin": str(
                bundled_hermes
                if bundled_hermes is not None
                else HERMES_ROOT / "venv" / "bin" / "hermes"
            ),
            "hermes_python": sys.executable if bundled_hermes is not None else str(
                HERMES_ROOT / "venv" / "bin" / "python"
            ),
            "mock_mcp_python": str(MOCK_MCP_PYTHON),
        })
        manifest["launch"] = launch
        if (manifest.get("evaluation_scope") or {}).get("kind") == "test_only":
            launch["canonical_runner"] = str(_test_only_runner(workspace, launch["canonical_runner"]))
        elif ingestion:
            control.mkdir()
            launch["canonical_runner"] = str(
                _seed_checkpoint_runner(workspace, launch["canonical_runner"], control)
            )
        _write_json(manifest_path, manifest)
        output_root = Path(str(manifest.get("output_root") or ""))
        if not output_root.is_absolute():
            raise ValueError("prepared manifest has no absolute output_root")
        if workspace.resolve() not in output_root.resolve().parents:
            raise ValueError("prepared manifest output_root must stay inside the immutable bundle workspace")
        if execution_action == "resume" and _can_restart_unstarted_test(manifest, result_root):
            execution_action = "test-only"
        if execution_action in {"resume", "resume-seed"}:
            resume_root = _ingestion_resume_root(result_root) if ingestion else result_root
            saved_output = resume_root / "output"
            if not saved_output.is_dir():
                raise FileNotFoundError("cannot resume: the prior job has no saved output directory")
            shutil.copytree(saved_output, output_root, dirs_exist_ok=True)
            if profile_archive_name:
                _restore_input_profile(manifest, profile_archive_name)
            else:
                _restore_profile(manifest, resume_root)
        elif profile_archive_name:
            _restore_input_profile(manifest, profile_archive_name)
        if profile_archive_name:
            if action not in {"test-only", "resume"}:
                raise ValueError("completed profile archives are only valid for test-only work")
            if not any(job.get("profile") for job in manifest.get("jobs") or []):
                raise ValueError("profile archive is not valid for a non-Hermes job")
            _configure_restored_profile_for_worker(manifest_path, manifest)
        if native_memory_archive_name and profile_archive_name:
            raise ValueError("a job may use either a Hermes profile archive or a Claude native-memory archive")
        if memory_connection == "tailscale":
            verified_memory_endpoints = _verify_tailscale_memory_service_endpoints(manifest)
        elif memory_connection == "modal":
            verified_memory_endpoints = _verify_modal_memory_service_endpoints(manifest)
        elif memory_connection != "none":
            raise ValueError(f"unsupported memory connection: {memory_connection}")
        if ingestion:
            _save_ingestion_checkpoint(manifest, result_root, output_root)
        elif not profile_archive_name:
            _snapshot_resume_state(manifest, result_root, output_root)
        elif output_root.is_dir():
            shutil.copytree(output_root, result_root / "output", dirs_exist_ok=True)
            _commit_results_volume()

        command = [
            sys.executable,
            "-m",
            "reference.evaluate",
            execution_action,
            "--manifest",
            str(manifest_path),
            "--concurrency",
            "1",
        ]
        # A fresh test-only manifest already contains its exact test IDs.
        # Only launch/resume accept a --test-ids command-line argument.
        if test_ids and execution_action in {"launch", "resume"}:
            command.extend(("--test-ids", ",".join(test_ids)))
        mode = "a" if execution_action in {"resume", "resume-seed"} else "w"
        last_snapshot = time.monotonic()
        last_status_commit = last_snapshot
        if ingestion:
            _write_json(result_root / "ingestion-recovery-required.json", {
                "reason": "execution may advance beyond the last saved checkpoint",
            })
            results_volume.commit()
        with stdout_path.open(mode) as stdout, stderr_path.open(mode) as stderr:
            process = subprocess.Popen(
                command,
                cwd=workspace / "repo",
                stdout=stdout,
                stderr=stderr,
                text=True,
                start_new_session=True,
                env={
                    **os.environ,
                    **agent_environment,
                    "PYTHONPATH": os.pathsep.join([
                        *([str(bundled_hermes.parent)] if bundled_hermes is not None else []),
                        str(workspace / "repo"),
                    ]),
                },
            )
            while process.poll() is None:
                rate_limited = rate_limited or _rate_limit_seen(stdout_path, stderr_path)
                subscription_waiting = subscription_waiting or _subscription_waiting_seen(
                    stdout_path, stderr_path,
                )
                _write_json(status_path, {
                    "job_id": job_id,
                    "status": "running",
                    "action": action,
                    "started_unix": started,
                    "rate_limited": rate_limited,
                    "subscription_waiting": subscription_waiting,
                })
                if ingestion:
                    request = control / "request"
                    ready = control / "ready"
                    if time.monotonic() - last_snapshot >= _SNAPSHOT_INTERVAL_SECONDS and not request.exists():
                        request.write_text(uuid4().hex)
                    if request.exists() and ready.exists() and ready.read_text() == request.read_text():
                        _save_ingestion_checkpoint(manifest, result_root, output_root)
                        request.unlink()
                        last_snapshot = time.monotonic()
                elif time.monotonic() - last_status_commit >= _STATUS_COMMIT_INTERVAL_SECONDS:
                    _commit_results_volume()
                    last_status_commit = time.monotonic()
                if not ingestion and time.monotonic() - last_snapshot >= _SNAPSHOT_INTERVAL_SECONDS:
                    if profile_archive_name:
                        if output_root.is_dir():
                            shutil.copytree(output_root, result_root / "output", dirs_exist_ok=True)
                        _commit_results_volume()
                    else:
                        _snapshot_resume_state(manifest, result_root, output_root)
                    last_snapshot = time.monotonic()
                    last_status_commit = last_snapshot
                time.sleep(5)
            returncode = process.returncode
        if output_root.exists():
            if ingestion and returncode == 0:
                saved = _save_ingestion_checkpoint(manifest, result_root, output_root)
                _publish_completed_ingestion(saved, result_root)
                _prune_completed_ingestion_saves(result_root, saved)
                (result_root / "ingestion-recovery-required.json").unlink()
                results_volume.commit()
            elif ingestion:
                # Failed/uncertain turns are evidence, not a replacement for the
                # last acknowledged checkpoint. Never resume them automatically.
                diagnostics = result_root / f"failed-ingestion-{uuid4().hex}"
                shutil.copytree(output_root, diagnostics / "output")
                _persist_profile(manifest, diagnostics)
            elif profile_archive_name:
                shutil.copytree(output_root, result_root / "output", dirs_exist_ok=True)
                _commit_results_volume()
            else:
                _snapshot_resume_state(manifest, result_root, output_root)
    except Exception as exc:
        error = str(exc)
        if ingestion and process is not None and process.poll() is None:
            (control / "error").write_text("checkpoint or worker failed")
            # A checkpoint waiter exits without starting another source.
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                import signal

                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            try:
                diagnostics = result_root / f"failed-ingestion-{uuid4().hex}"
                shutil.copytree(output_root, diagnostics / "output")
                _persist_profile(manifest, diagnostics)
            except Exception as save_exc:
                error += f"; failed to save stopped ingestion evidence: {save_exc}"
    finally:
        rate_limited = rate_limited or _rate_limit_seen(stdout_path, stderr_path)
        subscription_waiting = subscription_waiting or _subscription_waiting_seen(
            stdout_path, stderr_path,
        )
        if error is not None:
            final_status = "failed"
        elif returncode == 0:
            final_status = "completed"
        elif rate_limited:
            final_status = "waiting_rate_limit"
        elif subscription_waiting:
            final_status = "waiting_subscription"
        else:
            final_status = "failed"
        metadata = {
            "job_id": job_id,
            "input_identity": input_identity,
            "bundle_name": bundle_name,
            "manifest_relative_path": manifest_relative_path,
            "action": action,
            "execution_action": execution_action,
            "test_ids": test_ids or [],
            "profile_archive_name": profile_archive_name,
            "native_memory_archive_name": native_memory_archive_name,
            "started_unix": started,
            "ended_unix": time.time(),
            "returncode": returncode,
            "error": error,
            "attempts": prior_attempts + 1,
            "status": final_status,
            "rate_limited": rate_limited,
            "subscription_waiting": subscription_waiting,
            "verified_memory_endpoints": verified_memory_endpoints,
            "ingestion_checkpoint_version": 1 if ingestion else None,
            "files": _result_files(result_root),
        }
        _write_json(metadata_path, metadata)
        _write_json(status_path, {
            "job_id": job_id,
            "status": final_status,
            "action": action,
            "started_unix": started,
            "ended_unix": metadata["ended_unix"],
            "rate_limited": rate_limited,
            "subscription_waiting": subscription_waiting,
            "returncode": returncode,
            "error": error,
        })
        _commit_results_volume()

    return {
        "ok": final_status == "completed",
        "job_id": job_id,
        "returncode": returncode,
        "error": error,
        "result_path": str(result_root),
        "status": final_status,
        "rate_limited": rate_limited,
        "subscription_waiting": subscription_waiting,
    }


_WORKER_FUNCTION_OPTIONS = {
    "cpu": 2,
    "memory": 4096,
    "timeout": 86400,
    "retries": 0,
    "single_use_containers": True,
    "volumes": {
        str(INPUT_ROOT): input_volume.with_mount_options(read_only=True),
        str(RESULTS_ROOT): results_volume,
    },
}


def run_ingestion_job(**kwargs: Any) -> dict[str, Any]:
    """Run approved built-in or hosted ingestion without preemption."""
    if kwargs.get("action") not in {"seed", "resume-seed"}:
        raise ValueError("ingestion worker requires seed or resume-seed")
    return _run_prepared_job(**kwargs, memory_connection="none")


def run_private_ingestion_job(**kwargs: Any) -> dict[str, Any]:
    """Run approved Honcho ingestion without preemption."""
    if kwargs.get("action") not in {"seed", "resume-seed"}:
        raise ValueError("ingestion worker requires seed or resume-seed")
    return _run_prepared_job(**kwargs, memory_connection="tailscale")


def run_prepared_job(
    *,
    bundle_name: str,
    job_id: str,
    manifest_relative_path: str,
    action: str,
    test_ids: list[str] | None = None,
    profile_archive_name: str | None = None,
    native_memory_archive_name: str | None = None,
) -> dict[str, Any]:
    """Run built-in or hosted Mem0 evaluation work without a private network."""
    return _run_prepared_job(
        bundle_name=bundle_name,
        job_id=job_id,
        manifest_relative_path=manifest_relative_path,
        action=action,
        test_ids=test_ids,
        profile_archive_name=profile_archive_name,
        native_memory_archive_name=native_memory_archive_name,
        memory_connection="none",
    )


def run_private_network_job(
    *,
    bundle_name: str,
    job_id: str,
    manifest_relative_path: str,
    action: str,
    test_ids: list[str] | None = None,
    profile_archive_name: str | None = None,
    native_memory_archive_name: str | None = None,
) -> dict[str, Any]:
    """Run a self-hosted memory evaluation after joining the private network."""
    return _run_prepared_job(
        bundle_name=bundle_name,
        job_id=job_id,
        manifest_relative_path=manifest_relative_path,
        action=action,
        test_ids=test_ids,
        profile_archive_name=profile_archive_name,
        native_memory_archive_name=native_memory_archive_name,
        memory_connection="tailscale",
    )


def run_modal_memory_job(
    *,
    bundle_name: str,
    job_id: str,
    manifest_relative_path: str,
    action: str,
    test_ids: list[str] | None = None,
    profile_archive_name: str | None = None,
    native_memory_archive_name: str | None = None,
) -> dict[str, Any]:
    """Run Hindsight or Supermemory evaluation against its Modal service."""

    return _run_prepared_job(
        bundle_name=bundle_name,
        job_id=job_id,
        manifest_relative_path=manifest_relative_path,
        action=action,
        test_ids=test_ids,
        profile_archive_name=profile_archive_name,
        native_memory_archive_name=native_memory_archive_name,
        memory_connection="modal",
    )


def run_openrouter_prepared_job(**kwargs: Any) -> dict[str, Any]:
    return _run_prepared_job(**kwargs, memory_connection="none")


def run_openrouter_private_network_job(**kwargs: Any) -> dict[str, Any]:
    return _run_prepared_job(**kwargs, memory_connection="tailscale")


def run_openrouter_modal_memory_job(**kwargs: Any) -> dict[str, Any]:
    return _run_prepared_job(**kwargs, memory_connection="modal")


def _check_openrouter_environment() -> dict[str, bool]:
    return {
        "OPENROUTER_API_KEY": bool(os.environ.get("OPENROUTER_API_KEY", "").strip()),
        "MINIMAX_OPENROUTER_API_KEY": bool(
            os.environ.get("MINIMAX_OPENROUTER_API_KEY", "").strip()
        ),
    }


def read_job_status(job_id: str) -> dict[str, Any]:
    """Read a job's durable status without contacting a provider."""
    path = _safe_result_path(job_id, "status.json")
    if not path.is_file():
        return {"job_id": job_id, "status": "not_started"}
    return json.loads(path.read_text())



def create_worker_app(runtimes: frozenset[str] = frozenset()) -> tuple[Any, dict]:
    """Register selected runtime packages and a runtime-free status reader."""
    unknown = runtimes - {"hermes", "claude"}
    if unknown:
        raise ValueError(f"unsupported worker runtimes: {sorted(unknown)}")
    selected_app = modal.App(APP_NAME)
    functions = {}
    functions["status"] = selected_app.function(
        image=base_image, timeout=600,
        volumes={str(RESULTS_ROOT): results_volume.with_mount_options(read_only=True)},
    )(read_job_status)
    for runtime in sorted(runtimes):
        image = runtime_image(runtime)
        private = private_network_image(image)
        memory = image.pip_install("hindsight-client==0.6.1")
        definitions = [
            (run_prepared_job, image, [evaluation_secret], {}),
            (run_private_network_job, private, [private_network_evaluation_secret], {}),
            (run_modal_memory_job, memory, [evaluation_secret, memory_service_secret], {}),
        ]
        if runtime == "hermes":
            definitions += [
                (run_ingestion_job, image, [evaluation_secret], {"nonpreemptible": True}),
                (run_private_ingestion_job, private, [private_network_evaluation_secret], {"nonpreemptible": True}),
                (run_openrouter_prepared_job, image, [evaluation_secret, openrouter_evaluation_secret], {}),
                (run_openrouter_private_network_job, private, [private_network_evaluation_secret, openrouter_evaluation_secret], {}),
                (run_openrouter_modal_memory_job, memory, [evaluation_secret, memory_service_secret, openrouter_evaluation_secret], {}),
            ]
            functions["openrouter_check"] = selected_app.function(
                image=image, secrets=[evaluation_secret, openrouter_evaluation_secret], timeout=600,
            )(_check_openrouter_environment)
        for function, package, secrets, options in definitions:
            functions[runtime, function.__name__] = selected_app.function(
                name=f"{runtime}_{function.__name__}", image=package, secrets=secrets,
                **options, **_WORKER_FUNCTION_OPTIONS,
            )(function)
        functions[runtime, "smoke"] = selected_app.function(
            name=f"{runtime}_environment_smoke", image=image, cpu=1, memory=2048,
            timeout=600, single_use_containers=True, secrets=[evaluation_secret],
        )(environment_smoke)
        functions[runtime, "private_smoke"] = selected_app.function(
            name=f"{runtime}_private_network_smoke", image=private, cpu=1, memory=1024,
            timeout=600, single_use_containers=True, secrets=[private_network_evaluation_secret],
        )(private_network_smoke)
    return selected_app, functions


@app.local_entrypoint()
def main(smoke: bool = False, runtime: str = "hermes") -> None:
    """Run explicit operator checks; normal jobs use the suite controller."""
    if not smoke:
        raise SystemExit("pass --smoke to run the no-model environment check")
    selected_app, functions = create_worker_app(frozenset({runtime}))
    with selected_app.run():
        print(json.dumps(functions[runtime, "smoke"].remote(runtime), indent=2, sort_keys=True))


@app.local_entrypoint()
def smoke(runtime: str = "hermes") -> None:
    main(smoke=True, runtime=runtime)


@app.local_entrypoint()
def smoke_private(honcho_url: str, runtime: str = "hermes") -> None:
    """Run the approved network-only check without invoking an agent or model."""
    selected_app, functions = create_worker_app(frozenset({runtime}))
    with selected_app.run():
        result = functions[runtime, "private_smoke"].remote({"honcho": honcho_url})
    print(json.dumps(result, indent=2, sort_keys=True))
