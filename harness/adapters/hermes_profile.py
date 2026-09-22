"""Shared Hermes profile configuration for the reference and internal evaluation."""

import hashlib
from pathlib import Path
from typing import Any

import yaml


CONFIGURATIONS = ("builtin", "mem0", "honcho", "hindsight", "supermemory")
REQUIRED_TOOLSETS = ("hermes-cli", "dolphinbench-apps", "memory", "session_search")


def toolsets_for(configuration: str) -> list[str]:
    if configuration not in CONFIGURATIONS:
        raise ValueError(f"unknown configuration {configuration!r}")
    return [*REQUIRED_TOOLSETS, *([] if configuration == "builtin" else [configuration])]


def _profile_config(job: dict[str, Any], manifest: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    """Return the profile configuration without putting credential values in it."""
    root = Path(manifest["root"])
    toolsets = toolsets_for(job["configuration"])
    if job["toolsets"] != toolsets:
        raise RuntimeError(f"manifest job {job['id']} has an invalid toolset")
    agent = manifest["agent"]
    cfg: dict[str, Any] = {
        "model": {
            "provider": agent["provider"],
            "default": agent["model"],
            "base_url": agent["base_url"],
            "api_mode": agent["api_mode"],
            "context_length": agent["context_length"],
        },
        "memory": {
            "memory_enabled": True,
            "user_profile_enabled": True,
            "flush_min_turns": 6,
            "nudge_interval": 10,
            # Built-in memory is the only condition without an external store.
            "provider": "builtin" if job["configuration"] == "builtin" else job["configuration"],
        },
        "toolsets": toolsets,
        "platform_toolsets": {
            platform: list(toolsets)
            for platform in (
                "cli", "slack", "discord", "telegram", "whatsapp", "signal", "homeassistant",
            )
        },
        "mcp_servers": {
            "dolphinbench-apps": {
                "command": manifest["launch"]["mock_mcp_python"],
                "args": [str(root / "mock_mcp" / "server.py")],
                "tools": {
                    "resources": False,
                    "prompts": False,
                },
                "env": {
                    "DOLPHINBENCH_PERSONA": job["persona"],
                    "DOLPHINBENCH_MOCK_MANIFEST": str(root / "mock_mcp" / "manifests" / f"{job['persona']}.yaml"),
                    "DOLPHINBENCH_STATE_PATH": job["state_path"],
                    "DOLPHINBENCH_LOG_PATH": job["log_path"],
                    "DOLPHINBENCH_TOOL_CONFIG_PATH": job["tool_config_path"],
                    "DOLPHINBENCH_RUN_ID": job["run_id"],
                },
            }
        },
        "agent": {
            "personalities": {
                "default": "You are Hermes Agent. Be direct, helpful, concise, and use tools to complete requested actions.",
            },
            # Ordinary transient failures keep a finite retry budget. Rate limits
            # keep waiting because provider throttling must not terminate a run.
            "api_max_retries": 8,
            "retry_rate_limits_until_success": True,
            "reasoning_effort": agent["reasoning_effort"],
            "max_turns": agent["max_turns"],
        },
    }
    identity = job["provider_identity"]
    if job["configuration"] == "mem0":
        # Mem0 reads MEM0_API_KEY from the process environment.  The profile
        # only stores the run-specific, non-secret namespace configuration.
        cfg["mem0"] = {
            "user_id": identity["user_id"],
            "organization_id": env["MEM0_ORGANIZATION_ID"],
            "project_id": env["MEM0_PROJECT_ID"],
            "rerank": False,
        }
    elif job["configuration"] == "honcho":
        honcho: dict[str, Any] = {
            "enabled": True,
            "workspace": identity["workspace"],
            "peerName": identity["peer_name"],
            "aiPeer": identity["ai_peer"],
            "hosts": {
                "hermes": {
                    "enabled": True,
                    "workspace": identity["workspace"],
                    "peerName": identity["peer_name"],
                    "aiPeer": identity["ai_peer"],
                }
            },
        }
        if env.get("HONCHO_BASE_URL"):
            honcho["baseUrl"] = env["HONCHO_BASE_URL"].rstrip("/")
        cfg["honcho"] = honcho
    elif job["configuration"] == "hindsight":
        cfg["hindsight"] = {
            "mode": env.get("HINDSIGHT_MODE", "local_external").strip() or "local_external",
            "api_url": env.get("HINDSIGHT_API_URL", "").strip(),
            "bank_id": identity["bank_id"],
            "budget": env.get("HINDSIGHT_BUDGET", "mid").strip() or "mid",
            "memory_mode": "hybrid",
            "auto_recall": True,
            "auto_retain": True,
            "retain_async": False,
        }
    elif job["configuration"] == "supermemory":
        cfg["supermemory"] = {
            "container_tag": identity["container_tag"],
            "base_url": env.get("SUPERMEMORY_BASE_URL", "").strip(),
            "auto_recall": True,
            "auto_capture": True,
            "max_recall_results": 10,
            "search_mode": "hybrid",
        }
    return cfg


def _load_personas(root: Path) -> dict[str, str]:
    data = yaml.safe_load((root / "registry" / "personas.yaml").read_text()) or {}
    return {str(entry["id"]): str(entry.get("name") or entry["id"]) for entry in data.get("personas") or []}

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def profile_config_hashes(profile_dir: Path) -> dict[str, str | None]:
    """Return the non-secret profile inputs that determine the tool surface."""
    hashes: dict[str, str | None] = {}
    for filename in (
        "config.yaml", "mem0.json", "honcho.json", "supermemory.json",
        "hindsight/config.json",
    ):
        path = profile_dir / filename
        hashes[filename] = _sha256(path) if path.is_file() else None
    if hashes["config.yaml"] is None:
        raise RuntimeError(f"created profile has no config.yaml: {profile_dir}")
    return hashes
