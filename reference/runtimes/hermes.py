"""Reference hermes implementation."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


import yaml


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.environment import load_environment
from harness.adapters.hermes_profile import REQUIRED_TOOLSETS, _load_personas, _profile_config, profile_config_hashes

from reference.evaluate import HERMES_HOME
from reference.evaluate import load_manifest
from reference.evaluate import verify_manifest_hashes


def _required_environment(job: dict[str, Any], agent: dict[str, Any]) -> tuple[str, ...]:
    names = [agent["api_key_env"]]
    if job["configuration"] == "mem0":
        names.extend(("MEM0_API_KEY", "MEM0_ORGANIZATION_ID", "MEM0_PROJECT_ID"))
    elif job["configuration"] == "honcho":
        names.append("HONCHO_API_KEY or HONCHO_BASE_URL")
    elif job["configuration"] == "hindsight":
        names.append("HINDSIGHT_API_KEY or a self-hosted HINDSIGHT_API_URL")
    elif job["configuration"] == "supermemory":
        names.append("SUPERMEMORY_API_KEY")
    return tuple(names)


def _require_environment(job: dict[str, Any], agent: dict[str, Any], env: dict[str, str]) -> None:
    missing: list[str] = []
    for name in _required_environment(job, agent):
        if name == "HONCHO_API_KEY or HONCHO_BASE_URL":
            if not (env.get("HONCHO_API_KEY") or env.get("HONCHO_BASE_URL")):
                missing.append(name)
        elif name == "HINDSIGHT_API_KEY or a self-hosted HINDSIGHT_API_URL":
            mode = env.get("HINDSIGHT_MODE", "local_external").strip().lower()
            has_local_endpoint = mode == "local" or (
                mode == "local_external" and bool(env.get("HINDSIGHT_API_URL"))
            )
            if not (env.get("HINDSIGHT_API_KEY") or has_local_endpoint):
                missing.append(name)
        elif not env.get(name):
            missing.append(name)
    if missing:
        raise RuntimeError(f"missing required runtime environment variables: {missing}")


def _assert_exact_profile_surface(cfg: dict[str, Any], expected_toolsets: list[str]) -> None:
    configured = cfg.get("toolsets")
    if configured != expected_toolsets or "all" in configured:
        raise RuntimeError("fresh profile toolsets are not the exact benchmark toolsets")
    platform_toolsets = cfg.get("platform_toolsets") or {}
    if not platform_toolsets or any(value != expected_toolsets for value in platform_toolsets.values()):
        raise RuntimeError("fresh profile platform toolsets are not the exact benchmark toolsets")
    mcp_servers = cfg.get("mcp_servers") or {}
    if set(mcp_servers) != {"dolphinbench-apps"}:
        raise RuntimeError("fresh profile must expose only the DolphinBench mock MCP server")
    if not set(REQUIRED_TOOLSETS).issubset(expected_toolsets):
        raise RuntimeError("fresh profile is missing a required benchmark toolset")


def _write_runtime_files(job: dict[str, Any], manifest: dict[str, Any], cfg: dict[str, Any]) -> None:
    run_dir = Path(job["run_dir"])
    run_dir.mkdir(parents=True, exist_ok=False)
    for path, initial in (
        (Path(job["state_path"]), "{}"),
        (Path(job["log_path"]), ""),
        (Path(job["tool_config_path"]), "{}"),
        (Path(job["ledger_path"]), ""),
    ):
        path.write_text(initial)
    persona_name = job["persona"]
    personas = _load_personas(Path(manifest["root"]))
    persona_name = personas.get(job["persona"], job["persona"])
    soul_template = (Path(manifest["root"]) / "harness" / "benchmark_soul.md").read_text()
    soul = soul_template.replace("{{persona_name}}", persona_name)
    Path(job["profile_soul_path"]).write_text(soul)


def profile_create_command(hermes_bin: str, profile_name: str) -> list[str]:
    """Create an empty profile without copying global skills or credentials."""
    return [hermes_bin, "profile", "create", profile_name, "--no-alias", "--no-skills"]


def create_profile_from_manifest(manifest: dict[str, Any], job: dict[str, Any]) -> dict[str, str | None]:
    """Create exactly one new profile during the explicit launch phase."""
    if manifest.get("phase") != "prepared_offline":
        raise RuntimeError("profile creation requires an unlaunched prepared manifest")
    env = load_environment(HERMES_HOME / ".env")
    gateway = job.get("gateway_config") or {}
    routing_keys = {
        "mem0": {"organization_id": "MEM0_ORGANIZATION_ID", "project_id": "MEM0_PROJECT_ID"},
        "honcho": {"honcho_base_url": "HONCHO_BASE_URL"},
    }
    for field, variable in routing_keys.get(job["configuration"], {}).items():
        if gateway.get(field):
            env[variable] = str(gateway[field])
    _require_environment(job, manifest["agent"], env)
    profile_name = job["profile"]
    profile_dir = HERMES_HOME / "profiles" / profile_name
    if profile_dir.exists():
        raise RuntimeError(f"profile identity was already used: {profile_dir}")
    run_dir = Path(job["run_dir"])
    if run_dir.exists():
        raise RuntimeError(f"run path was already used: {run_dir}")
    cfg = _profile_config(job, manifest, env)
    _assert_exact_profile_surface(cfg, job["toolsets"])
    subprocess.run(
        profile_create_command(manifest["launch"]["hermes_bin"], profile_name),
        check=True,
        stdout=subprocess.DEVNULL,
    )
    try:
        # Runtime credentials stay in the launch process environment.
        for filename in (
            ".env", "auth.json", "credentials.json", "tokens.json", "mem0.json",
            "honcho.json", "supermemory.json",
        ):
            (profile_dir / filename).unlink(missing_ok=True)
        for directory in ("sessions", "logs", "memories", "memory"):
            shutil.rmtree(profile_dir / directory, ignore_errors=True)
        _write_runtime_files(job, manifest, cfg)
        (profile_dir / "SOUL.md").write_text(Path(job["profile_soul_path"]).read_text())
        # Write the exact benchmark configuration so no external connectors
        # are exposed.
        (profile_dir / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        if "mem0" in cfg:
            (profile_dir / "mem0.json").write_text(json.dumps(cfg["mem0"], indent=2) + "\n")
        if "honcho" in cfg:
            (profile_dir / "honcho.json").write_text(json.dumps(cfg["honcho"], indent=2) + "\n")
        if "hindsight" in cfg:
            hindsight_dir = profile_dir / "hindsight"
            hindsight_dir.mkdir(parents=True, exist_ok=True)
            (hindsight_dir / "config.json").write_text(
                json.dumps(cfg["hindsight"], indent=2) + "\n"
            )
        if "supermemory" in cfg:
            (profile_dir / "supermemory.json").write_text(
                json.dumps(cfg["supermemory"], indent=2) + "\n"
            )
        return profile_config_hashes(profile_dir)
    except Exception:
        shutil.rmtree(profile_dir, ignore_errors=True)
        shutil.rmtree(run_dir, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch", action="store_true", help="required explicit launch acknowledgement")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    if not args.launch:
        parser.error("profile creation is allowed only during explicit launch; pass --launch")
    manifest = load_manifest(args.manifest)
    verify_manifest_hashes(manifest)
    job = next((candidate for candidate in manifest["jobs"] if candidate["id"] == args.job_id), None)
    if job is None:
        parser.error(f"manifest has no job {args.job_id!r}")
    create_profile_from_manifest(manifest, job)
    print(job["profile"])
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
