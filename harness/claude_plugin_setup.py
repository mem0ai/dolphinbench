"""Official Claude Code memory-plugin configuration for disposable diagnostics.

``source`` is an already-built, read-only official distribution (or its
checkout root).  This module only resolves paths below it; it never installs,
builds, or changes the upstream distribution.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any


_PROVIDERS = frozenset({"builtin", "mem0", "honcho", "hindsight", "supermemory"})


def _required(settings: dict[str, Any], name: str) -> str:
    value = settings.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"settings.{name} is required")
    return value.strip()


def _url(value: str) -> str:
    return value.rstrip("/")


def _plugin_root(source: Path, *relative_candidates: str) -> Path:
    """Find one official Claude plugin root without changing the source tree."""
    candidates = (Path("."), *(Path(candidate) for candidate in relative_candidates))
    for relative in candidates:
        root = (source / relative).resolve()
        if (root / ".claude-plugin" / "plugin.json").is_file():
            return root
    expected = ", ".join(str(path / ".claude-plugin/plugin.json") for path in candidates)
    raise ValueError(f"official Claude plugin build is missing ({expected})")


def _hindsight_root(source: Path) -> Path:
    candidates = (Path("."), Path("hindsight-integrations/coding-agents"))
    required = (
        "package.json",
        "dist/claude-sessionstart-hook.js",
        "dist/claude-hook.js",
        "dist/claude-stop-hook.js",
        "dist/mcp-server.js",
    )
    for relative in candidates:
        root = (source / relative).resolve()
        if all((root / path).is_file() for path in required):
            return root
    raise ValueError("official Hindsight build must contain package.json and Claude hook/MCP dist files")


def _honcho_root(source: Path) -> Path:
    root = _plugin_root(source, "plugins/honcho/.stage", "plugins/honcho")
    if not (root / "dist" / "mcp-server.js").is_file():
        raise ValueError("official Honcho plugin build must contain dist/mcp-server.js")
    return root


def _hindsight_settings(dist: Path) -> dict[str, Any]:
    def command(name: str, timeout: int) -> dict[str, Any]:
        return {
            "hooks": [{
                "type": "command",
                "command": f'node "{dist / name}"',
                "timeout": timeout,
            }]
        }

    # These are the three hooks the official installer writes for Claude Code.
    return {
        "hooks": {
            "SessionStart": [command("claude-sessionstart-hook.js", 30)],
            "UserPromptSubmit": [command("claude-hook.js", 30)],
            "Stop": [command("claude-stop-hook.js", 60)],
        }
    }


def _write_hindsight_config(home: Path, settings: dict[str, Any]) -> None:
    config_dir = home / ".hindsight"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "apiUrl": _url(_required(settings, "api_url")),
        "apiToken": _required(settings, "api_key"),
        "bankId": _required(settings, "namespace"),
        # A diagnostic must exercise the conversation hooks, not import the
        # repository or start a background codebase survey.
        "autoSeed": False,
        "codebaseSurvey": False,
        "autoUpdate": False,
    }
    (config_dir / "coding-agent.json").write_text(json.dumps(config, indent=2) + "\n")


def configure_plugin(
    provider: str,
    home: Path,
    project: Path,
    source: Path,
    settings: dict,
) -> dict:
    """Return official Claude Code wiring for one disposable provider diagnostic.

    ``home`` is the caller's isolated Unix-home directory.  ``project`` is
    deliberately accepted as part of the runner contract but is not used to
    write provider configuration: the supplied project/source roots stay
    untouched.  Common settings are ``namespace``, ``api_url``, ``api_key``,
    and, where applicable, ``mcp_url``.
    """
    if provider not in _PROVIDERS:
        raise ValueError(f"unsupported Claude memory provider: {provider}")
    if not isinstance(settings, dict):
        raise TypeError("settings must be a dict")

    home = Path(home).expanduser().resolve()
    Path(project).expanduser().resolve()  # Validate path handling without writing the project.
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"official plugin source does not exist: {source}")

    result: dict[str, Any] = {"env": {}, "plugin_dirs": [], "settings": {}}
    if provider == "builtin":
        return result

    namespace = _required(settings, "namespace")
    api_key = _required(settings, "api_key")
    api_url = _url(_required(settings, "api_url"))

    if provider == "mem0":
        root = _plugin_root(source, "integrations/claude-code-plugin")
        result["plugin_dirs"] = [str(root)]
        result["env"] = {
            "MEM0_API_KEY": api_key,
            "MEM0_USER_ID": namespace,
            "MEM0_API_URL": api_url,
        }
    elif provider == "honcho":
        root = _honcho_root(source)
        result["plugin_dirs"] = [str(root)]
        endpoint = api_url if api_url.endswith("/v3") else f"{api_url}/v3"
        result["env"] = {
            "HONCHO_API_KEY": api_key,
            "HONCHO_ENDPOINT": endpoint,
            "HONCHO_WORKSPACE": namespace,
            "HONCHO_PEER_NAME": namespace,
            "HONCHO_AI_PEER": "claude",
        }
    elif provider == "hindsight":
        root = _hindsight_root(source)
        _write_hindsight_config(home, settings)
        dist = root / "dist"
        result["settings"] = _hindsight_settings(dist)
        result["mcp_servers"] = {
            "hindsight": {
                "command": "node",
                "args": [str(dist / "mcp-server.js")],
                "env": {"HINDSIGHT_MCP_HARNESS": "claude-code"},
            }
        }
    else:  # supermemory
        root = _plugin_root(source, "plugin")
        if settings.get("capture_session_end"):
            target = home / ".supermemory-sessionend-plugin"
            shutil.copytree(root, target, dirs_exist_ok=True)
            manifest = target / "hooks/hooks.json"
            value = json.loads(manifest.read_text())
            hooks = value["hooks"]
            expected = [{"hooks": [{"type": "command", "command": 'node "${CLAUDE_PLUGIN_ROOT}/hooks/capture.js"',
                                    "async": True, "timeout": 30}]}]
            if hooks.get("Stop") != expected or "SessionEnd" in hooks:
                raise ValueError("Supermemory capture manifest differs from the inspected build")
            hooks["SessionEnd"] = hooks.pop("Stop")
            hooks["SessionEnd"][0]["hooks"][0].pop("async")
            for event in ("SessionStart", "UserPromptSubmit", "SessionEnd"):
                for group in hooks.get(event, []):
                    for hook in group.get("hooks", []):
                        hook["timeout"] = 45
            api = target / "hooks/lib/api.js"
            if api.exists():
                source_text = api.read_text()
                old = "const REQUEST_TIMEOUT_MS = 3000;"
                if source_text.count(old) != 1:
                    raise ValueError("Supermemory API timeout differs from the inspected build")
                api.write_text(source_text.replace(old, "const REQUEST_TIMEOUT_MS = 30000;"))
            value["description"] = "One-shot CLI compatibility: synchronous SessionEnd capture with a 30-second local API timeout."
            manifest.write_text(json.dumps(value, indent=2) + "\n")
            root = target
        mcp_url = _url(_required(settings, "mcp_url"))
        result["plugin_dirs"] = [str(root)]
        result["env"] = {
            "SUPERMEMORY_CC_API_KEY": api_key,
            "SUPERMEMORY_API_URL": api_url,
            "SUPERMEMORY_MCP_URL": mcp_url,
            "SUPERMEMORY_REPO_TAG": namespace,
        }

    return result


def configure_hindsight_evaluation(
    home: Path, project: Path, source: Path, settings: dict,
) -> dict:
    """Prepare an isolated profile using the official retrieval hooks and tools.

    api_url must be the frozen-bank read proxy, never the writable service.
    """
    home = Path(home).expanduser().resolve()
    root = _hindsight_root(Path(source).expanduser().resolve())
    skill = root / "skill"
    if not (skill / "SKILL.md").is_file():
        raise ValueError("official Hindsight skill/SKILL.md is missing")
    target = home / ".claude/skills/hindsight-coding-agent"
    for parent in (home / ".claude", target.parent, target):
        if parent.is_symlink():
            raise ValueError("evaluation skill destination must not be a symlink")
    if target.exists():
        raise ValueError("evaluation skill destination already exists; use a fresh profile")
    result = configure_plugin("hindsight", home, project, root, settings)
    shutil.copytree(skill, target)
    result["env"] = {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude")}
    result["settings"]["hooks"].pop("Stop")
    result["mcp_servers"]["hindsight"].update({
        "command": sys.executable,
        "args": [str(Path(__file__).with_name("hindsight_readonly_mcp.py")),
                 str(root / "dist/mcp-server.js")],
    })
    return result
