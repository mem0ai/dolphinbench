"""Reference images implementation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


import modal


ROOT = Path(__file__).resolve().parents[2]
REMOTE_ROOT = Path("/opt/dolphinbench")
REMOTE_SOURCES = REMOTE_ROOT / "sources"
REMOTE_CLAUDE = Path("/usr/local/bin/claude")
ARTIFACT_DIRECTORY = "claude-plugin-diagnostic-artifacts"
HONCHO_URL = "http://100.126.191.23:18000"
MODEL = "claude-sonnet-5"
TAILSCALE_AUTH_KEY_ENV = "TS_AUTHKEY"
_PLUGIN_SOURCES = {
    "mem0": REMOTE_SOURCES / "mem0-plugin",
    "honcho": REMOTE_SOURCES / "honcho-plugin",
    "hindsight": REMOTE_SOURCES / "hindsight-coding-agents",
    "supermemory": REMOTE_SOURCES / "supermemory-plugin",
}
def _require_source(path: Path) -> str:
    if modal.is_local() and not path.is_dir():
        raise RuntimeError(f"required reference source is missing: {path}")
    return str(path)


def _require_file(path: Path) -> str:
    if modal.is_local() and not path.is_file():
        raise RuntimeError(f"required reference file is missing: {path}")
    return str(path)


def hermes_source() -> Path:
    """Resolve the operator's source checkout without guessing a private path."""
    value = os.environ.get("DOLPHINBENCH_HERMES_SOURCE", "").strip()
    if not value:
        raise RuntimeError("Set DOLPHINBENCH_HERMES_SOURCE to the Hermes source checkout")
    path = Path(value).expanduser().resolve()
    for relative in ("pyproject.toml", "hermes_cli/main.py"):
        if not (path / relative).is_file():
            raise RuntimeError(f"Hermes source is missing {relative}: {path}")
    return path


def claude_binary() -> Path:
    """Resolve and check the pinned executable; --version makes no model call."""
    from reference.runtimes.claude_code import REQUIRED_CLAUDE_CODE_VERSION
    value = os.environ.get("DOLPHINBENCH_CLAUDE_BIN", "").strip() or shutil.which("claude")
    if not value:
        raise RuntimeError("Set DOLPHINBENCH_CLAUDE_BIN to the Claude Code executable")
    path = Path(value).expanduser().resolve()
    if modal.is_local():
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError(f"Claude Code executable is missing or not executable: {path}")
        result = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=30)
        version = re.search(r"\b(\d+\.\d+\.\d+)\b", result.stdout)
        if result.returncode or not version or version.group(1) != REQUIRED_CLAUDE_CODE_VERSION:
            raise RuntimeError(f"Claude Code requires version {REQUIRED_CLAUDE_CODE_VERSION}: {path}")
    return path


base_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates", "curl", "git", "tar")
    .pip_install("modal", "uv")
    .run_commands(
        "curl --fail --silent --show-error --location https://deb.nodesource.com/setup_22.x | bash -; "
        "apt-get install --yes nodejs; node --version | grep '^v22\\.'; "
        "npm install --global bun@1.3.6; bun --version | grep '^1.3.6$'",
        "for id in 2001 2002 2003 2004 2005; do "
        "groupadd --gid \"$id\" \"claude-plugin-$id\"; "
        "useradd --uid \"$id\" --gid \"$id\" --create-home --shell /bin/bash \"claude-plugin-$id\"; "
        "chmod 0700 \"/home/claude-plugin-$id\"; "
        "done",
    )
    .env({"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONPATH": str(REMOTE_ROOT)})
)

private_network_image = (
    base_image
    .run_commands(
        "set -eu; archive=/tmp/tailscale_1.102.2_amd64.tgz; "
        "curl -fsSL https://pkgs.tailscale.com/stable/tailscale_1.102.2_amd64.tgz -o \"$archive\"; "
        "printf '%s  %s\\n' 'ad2cde12f8de95f7b93a1e0401e652291c603d42b9d60a33fb1741eb38ab04d8' \"$archive\" "
        "| sha256sum --check --strict; "
        "tar -xzf \"$archive\" -C /tmp; "
        "install -m 0755 /tmp/tailscale_1.102.2_amd64/tailscale /usr/local/bin/tailscale; "
        "install -m 0755 /tmp/tailscale_1.102.2_amd64/tailscaled /usr/local/bin/tailscaled; "
        "rm -rf \"$archive\" /tmp/tailscale_1.102.2_amd64",
    )
    .add_local_file(
        _require_file(ROOT / 'reference' / 'execution' / 'tailscale_entrypoint.sh'),
        "/root/tailscale-entrypoint.sh",
        copy=True,
    )
    .run_commands("chmod 0755 /root/tailscale-entrypoint.sh")
    .env({
        "ALL_PROXY": "socks5h://localhost:1080",
        "HTTP_PROXY": "http://localhost:1080",
        "HTTPS_PROXY": "http://localhost:1080",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    })
    .entrypoint(["/root/tailscale-entrypoint.sh"])
)

evaluation_secret = modal.Secret.from_name(
    "dolphinbench-evaluation",
    required_keys=["CLAUDE_CODE_OAUTH_TOKEN", "MEM0_API_KEY", TAILSCALE_AUTH_KEY_ENV],
)
memory_services_secret = modal.Secret.from_name(
    "dolphinbench-memory-services",
    required_keys=["HINDSIGHT_API_KEY", "SUPERMEMORY_API_KEY"],
)
