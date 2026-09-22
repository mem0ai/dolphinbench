"""Controller-side Hermes launch through a separate agent container."""

import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import urlsplit, urlunsplit

import yaml

from reference.execution.agent_container import AgentContainer, image_backend


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_FILES = (
    "__init__.py", "hermes_observed_cli.py", "memory_diagnostics.py",
    "submission_capture.py", "submission.py", "costing.py",
)
PROVIDER_ENV = {
    "OPENAI_API_KEY", "CUSTOM_API_KEY", "OPENAI_BASE_URL", "OPENROUTER_API_KEY",
    "MINIMAX_OPENROUTER_API_KEY", "AZURE_FOUNDRY_API_KEY", "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT", "AZURE_FOUNDRY_ENDPOINT", "ANTHROPIC_API_KEY",
    "MEM0_API_KEY", "MEM0_ORGANIZATION_ID", "MEM0_PROJECT_ID", "MEM0_BASE_URL",
    "HONCHO_API_KEY", "HONCHO_BASE_URL", "HINDSIGHT_API_KEY", "HINDSIGHT_API_URL",
    "SUPERMEMORY_API_KEY", "SUPERMEMORY_BASE_URL",
    "MEM0_USER_ID", "MEM0_AGENT_ID", "MEM0_MODE", "MEM0_HOST",
    "HONCHO_WORKSPACE_ID", "HONCHO_PEER_NAME",
}
CONTROL_ENV = {
    "DOLPHINBENCH_HERMES_TOOLSETS", "DOLPHINBENCH_SEED_ITEM_ID",
    "DOLPHINBENCH_MEMORY_READ_ONLY", "DOLPHINBENCH_MEM0_READ_ONLY",
    "DOLPHINBENCH_HONCHO_READ_ONLY", "DOLPHINBENCH_HINDSIGHT_READ_ONLY",
    "DOLPHINBENCH_SUPERMEMORY_READ_ONLY", "DOLPHINBENCH_NATIVE_MEMORY_READ_ONLY",
    "ENACT_NATIVE_MEMORY_READ_ONLY", "DOLPHINBENCH_SINGLE_TURN",
    "DOLPHINBENCH_MEM0_SYNC_WRITES", "HERMES_MEMORY_SYNC_DRAIN_TIMEOUT_SECONDS",
    "DOLPHINBENCH_MEM0_FORCE_ENV_API_KEY", "DOLPHINBENCH_MEM0_WAIT_EVENTS_AT_END",
    "DOLPHINBENCH_MEM0_EVENT_BARRIER_TIMEOUT", "DOLPHINBENCH_MEM0_EVENT_POLL",
    "ENACT_NARRATIVE_TIMEZONE", "DOLPHINBENCH_NARRATIVE_TIMEZONE",
    "HERMES_DUMP_REQUESTS", "HERMES_PLUGIN_PAYLOAD_MAX_CHARS",
    "HERMES_EXIT_WATCHDOG_S", "HINDSIGHT_MODE", "HINDSIGHT_BUDGET",
}
OUTPUT_ENV = {
    "DOLPHINBENCH_TURN_STATUS_PATH", "DOLPHINBENCH_MEMORY_DIAGNOSTICS_PATH",
    "DOLPHINBENCH_COST_LEDGER_PATH",
    *(f"DOLPHINBENCH_{provider}_SEED_RECEIPTS"
      for provider in ("MEM0", "HONCHO", "HINDSIGHT", "SUPERMEMORY")),
}
PRIVATE_FILES = {".env", "auth.json", "credentials.json", "tokens.json"}
SERVICE_CONFIG_FILES = ("honcho.json", "supermemory.json", "hindsight/config.json")
SERVICE_URL_KEYS = {"base_url", "baseUrl", "api_url"}


def agent_environment(config, environment):
    memory = config.get("memory", {}).get("provider", "builtin")
    model = config.get("model", {}).get("provider", "custom")
    allowed = {"OPENAI_API_KEY", "CUSTOM_API_KEY", "OPENAI_BASE_URL"}
    model_prefixes = {
        "azure-foundry": ("AZURE_FOUNDRY_",),
        "azure": ("AZURE_OPENAI_",),
        "openrouter": ("OPENROUTER_", "MINIMAX_OPENROUTER_"),
        "anthropic": ("ANTHROPIC_",),
    }.get(model, ())
    prefixes = (*model_prefixes, f"{memory.upper()}_")
    allowed.update(key for key in PROVIDER_ENV if key.startswith(prefixes))
    return {key: value for key, value in environment.items() if key in allowed | CONTROL_ENV}


def route_memory_endpoints(config, environment, profile):
    """Give the sandbox local forwards to the controller's existing memory URLs."""
    endpoints, ports, origins = {}, {}, {}

    def rewrite(url):
        if not url:
            return url
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Memory endpoints must be HTTP(S) URLs without embedded credentials")
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        if origin not in origins:
            route = f"memory-{len(origins)}"
            origins[origin] = route
            endpoints[route] = origin
            ports[route] = 19000 + len(ports)
        return urlunsplit(("http", f"127.0.0.1:{ports[origins[origin]]}", parsed.path, parsed.query, parsed.fragment))

    for name in ("MEM0_BASE_URL", "MEM0_HOST", "HONCHO_BASE_URL", "HINDSIGHT_API_URL", "SUPERMEMORY_BASE_URL"):
        if environment.get(name):
            environment[name] = rewrite(environment[name])
    for section in ("mem0", "honcho", "hindsight", "supermemory"):
        for key in SERVICE_URL_KEYS:
            if config.get(section, {}).get(key):
                config[section][key] = rewrite(config[section][key])
    for name in SERVICE_CONFIG_FILES:
        path = profile / name
        if path.is_file():
            value = json.loads(path.read_text())
            for key in SERVICE_URL_KEYS:
                if value.get(key):
                    value[key] = rewrite(value[key])
            path.write_text(json.dumps(value))
    return endpoints, ports


def copy_regular_tree(source: Path, target: Path):
    if source.is_symlink():
        raise ValueError("Agent input must not be a symlink")
    target.mkdir(parents=True, exist_ok=True)
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Agent input contains a symlink: {path}")
        if set(path.relative_to(source).parts) & PRIVATE_FILES:
            continue
        destination = target / path.relative_to(source)
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        else:
            raise ValueError(f"Agent input contains a special file: {path}")


def run_cli(command, *, profile: str, profile_path: Path, env: dict, timeout=None, **_kwargs):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", profile):
        raise ValueError("Profile must be a single directory name")
    image = env.get("DOLPHINBENCH_AGENT_IMAGE", "")
    backend = image_backend(image)
    config = yaml.safe_load((profile_path / "config.yaml").read_text())
    servers = config.get("mcp_servers", {})
    if set(servers) != {"dolphinbench-apps"}:
        raise ValueError("Configure real app connectors outside the reference agent")
    apps = servers["dolphinbench-apps"]
    app_command = [apps["command"], *apps.get("args", [])]
    app_env = {**os.environ, **apps.get("env", {})}
    for key, value in env.items():
        if backend == "local-docker" and key in PROVIDER_ENV and (key.endswith("URL") or key.endswith("ENDPOINT")):
            if urlsplit(value).hostname in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError(f"{key} must be reachable from the agent container, not controller loopback")
    for section, key in (("model", "base_url"), ("hindsight", "api_url"),
                         ("honcho", "baseUrl"), ("supermemory", "base_url")):
        url = config.get(section, {}).get(key, "")
        if backend == "local-docker" and urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError(f"{section}.{key} must be reachable from the agent container")
    with tempfile.TemporaryDirectory(prefix="db-agent-") as raw:
        payload, returned = Path(raw) / "payload", Path(raw) / "returned"
        runtime = payload / "runtime" / "harness"
        runtime.mkdir(parents=True)
        for name in RUNTIME_FILES:
            shutil.copyfile(ROOT / "harness" / name, runtime / name)
        shutil.copyfile(Path(__file__).with_name("app_client.py"), payload / "app_client.py")
        work = payload / "work"
        work.mkdir()
        if workspace := env.get("DOLPHINBENCH_AGENT_WORKSPACE"):
            copy_regular_tree(Path(workspace), work)
        staged_profile = payload / "output" / "home" / "profiles" / profile
        copy_regular_tree(profile_path, staged_profile)
        selected_env = agent_environment(config, env)
        container_class, options = AgentContainer, {}
        client_args = ["/payload/app_client.py"]
        if backend == "modal-sandbox":
            from reference.execution.modal_agent import ModalAgentContainer
            container_class = ModalAgentContainer
            endpoints, ports = route_memory_endpoints(config, selected_env, staged_profile)
            options = {"endpoints": endpoints, "ports": ports}
            shutil.copyfile(Path(__file__).with_name("agent_bridge_client.py"), payload / "agent_bridge_client.py")
            client_args = ["/payload/agent_bridge_client.py", "mcp"]
        config["mcp_servers"] = {"dolphinbench-apps": {
            **apps, "command": "python", "args": client_args, "env": {},
        }}
        (staged_profile / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
        selected_env.update({"HOME": "/payload/output/home", "HERMES_HOME": "/payload/output/home",
                             "PYTHONPATH": "/payload/runtime", "PYTHONUNBUFFERED": "1"})
        if env.get("DOLPHINBENCH_SUBMISSION_TRACE_DIR"):
            selected_env["DOLPHINBENCH_SUBMISSION_TRACE_DIR"] = (
                f"/payload/output/home/profiles/{profile}/submission-traces")
        files = {}
        for key in OUTPUT_ENV:
            if env.get(key):
                source = Path(env[key])
                destination = payload / "output" / "files" / key
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source.exists():
                    if source.is_symlink() or not source.is_file():
                        raise ValueError("Output evidence path must be a regular file")
                    shutil.copyfile(source, destination)
                selected_env[key] = "/payload/output/files/" + key
                files[key] = source
        # Keep all original chat arguments; only executable paths change.
        arguments = command[command.index("-p"):]
        isolated = container_class(image, payload, [
            "python", "-m", "harness.hermes_observed_cli", "/usr/local/bin/hermes", *arguments,
        ], selected_env, app_command, app_env, **options)
        returned.mkdir()
        try:
            return isolated.run(returned, timeout=timeout)
        finally:
            # Returned config is untrusted: never use it to launch controller processes.
            restored_profile = returned / "home" / "profiles" / profile
            if isolated.output_valid and restored_profile.exists():
                # SQLite may remove its journal after checkpointing in the container.
                for database in restored_profile.rglob("*.db"):
                    for suffix in ("-wal", "-shm", "-journal"):
                        journal = database.with_name(database.name + suffix)
                        if not journal.exists():
                            old = profile_path / journal.relative_to(restored_profile)
                            if old.is_symlink() or any(p.is_symlink() for p in old.parents):
                                raise ValueError("Controller output path contains a symlink")
                            old.unlink(missing_ok=True)
                for path in restored_profile.rglob("*"):
                    if str(path.relative_to(restored_profile)) in SERVICE_CONFIG_FILES:
                        continue
                    if path.is_file() and path.name not in PRIVATE_FILES | {"config.yaml"}:
                        target = profile_path / path.relative_to(restored_profile)
                        if target.is_symlink() or any(p.is_symlink() for p in target.parents):
                            raise ValueError("Controller output path contains a symlink")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, target)
            for key, target in files.items():
                path = returned / "files" / key
                if isolated.output_valid and path.is_file():
                    if target.is_symlink() or any(p.is_symlink() for p in target.parents):
                        raise ValueError("Controller output path contains a symlink")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)
