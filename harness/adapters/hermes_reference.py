"""Read-only reference preflight and profile generation; no Hermes/model startup."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from pathlib import Path

import yaml
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from harness.adapters.hermes_profile import _load_personas, _profile_config, toolsets_for


ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = ROOT / "examples/reference/hermes-builtin.json"
PATCH_PATH = ROOT / "examples/reference/hermes-evaluation.patch"
DIFF_FLAGS = ["--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--no-renames",
              "--src-prefix=a/", "--dst-prefix=b/", "--unified=3"]


def sha(content):
    return hashlib.sha256(content).hexdigest()


def interpreter_identity(python: str) -> dict:
    script = (
        "import json,sys,importlib.metadata as m; "
        "print(json.dumps({'python':list(sys.version_info[:3]),'packages':"
        "{d.metadata['Name']:d.version for d in m.distributions()}}))"
    )
    result = subprocess.run([python, "-I", "-c", script], check=True,
                            capture_output=True, text=True, timeout=30)
    value = json.loads(result.stdout)
    value["packages"] = {canonicalize_name(name): version for name, version in value["packages"].items()}
    return value


def verify_source(source: Path, spec: dict) -> dict:
    if sha(PATCH_PATH.read_bytes()) != spec["patch_sha256"]:
        raise ValueError("Bundled Hermes reference patch changed")
    for file, key in (("uv.lock", "uv_lock_sha256"), ("pyproject.toml", "pyproject_sha256")):
        if sha((source / file).read_bytes()) != spec[key]:
            raise ValueError(f"Hermes {file} differs from the reference")
    patch = subprocess.run(["git", "-C", str(source), "diff", *DIFF_FLAGS, spec["source_base"]],
                           check=True, capture_output=True, timeout=60).stdout
    if sha(patch) != spec["patch_sha256"]:
        raise ValueError("Hermes source differs from the reference; use the pinned base and git apply --index")
    untracked = subprocess.run(["git", "-C", str(source), "ls-files", "--others", "--exclude-standard"],
                               check=True, capture_output=True, text=True, timeout=30).stdout.splitlines()
    if untracked:
        raise ValueError(f"Unexpected untracked Hermes source files: {untracked[:3]}")
    return {key: spec[key] for key in ("source_base", "evaluated_commit", "patch_sha256",
                                      "uv_lock_sha256", "pyproject_sha256")}


def verify_dependencies(source: Path, spec: dict, runtime: dict, apps: dict) -> None:
    if runtime["python"][:2] != spec["python_minor"] or apps["python"][:2] != spec["python_minor"]:
        raise ValueError("The reference Hermes and app environments require Python 3.11")
    project = tomllib.loads((source / "pyproject.toml").read_text())["project"]
    requirements = list(project["dependencies"])
    for extra in spec["extras"]:
        requirements.extend(project["optional-dependencies"][extra])
    environment = default_environment()
    environment.update(python_version="3.11", python_full_version=".".join(map(str, runtime["python"])))
    for text in requirements:
        requirement = Requirement(text)
        if requirement.marker and not requirement.marker.evaluate(environment):
            continue
        name = canonicalize_name(requirement.name)
        version = runtime["packages"].get(name)
        if version is None or version not in requirement.specifier:
            raise ValueError(f"Hermes dependency mismatch: {name} requires {requirement.specifier}, found {version}")
    lock = tomllib.loads((source / "uv.lock").read_text())
    locked = {(canonicalize_name(package["name"]), package["version"]) for package in lock["package"]}
    for name, version in runtime["packages"].items():
        if name not in {"pip", "setuptools", "wheel"} and (name, version) not in locked:
            raise ValueError(f"Hermes dependency is not in the supplied lock: {name}=={version}")
    for name, version in spec["app_packages"].items():
        if apps["packages"].get(name) != version:
            raise ValueError(f"App environment requires {name}=={version}")


def prepare_reference(options: dict, home: Path) -> dict:
    if set(options) != {"reference", "source_env", "python_env", "app_python_env", "base_url_env"}:
        raise ValueError("Reference options must name source, Hermes Python, app Python, and endpoint environment variables")
    spec = json.loads(SPEC_PATH.read_text())
    if options["reference"] != spec["name"]:
        raise ValueError("Unknown Hermes reference configuration")
    values = {}
    for key in ("source", "python", "app_python", "base_url"):
        value = os.environ.get(options[f"{key}_env"], "")
        if not value:
            raise ValueError(f"Set {options[f'{key}_env']} before preparing the reference")
        values[key] = value
    source = Path(values["source"]).resolve()
    python = str(Path(values["python"]).absolute())
    app_python = str(Path(values["app_python"]).absolute())
    source_identity = verify_source(source, spec)
    runtime = interpreter_identity(python)
    apps = interpreter_identity(app_python)
    verify_dependencies(source, spec, runtime, apps)
    binary = Path(python).parent / "hermes"
    shebang = binary.read_text().splitlines()[0]
    interpreter = Path(shebang[2:]) if shebang.startswith("#!") else Path("/missing")
    if (interpreter.parent != Path(python).parent or interpreter.resolve() != Path(python).resolve()):
        raise ValueError("Hermes executable must use the configured Python interpreter")
    agent = {**spec["agent"], "base_url": values["base_url"]}
    templates = {}
    for persona, name in _load_personas(ROOT).items():
        directory = home / "profiles" / persona
        job = {"id": f"builtin-{persona}", "configuration": "builtin", "persona": persona,
               "toolsets": toolsets_for("builtin"), "provider_identity": {},
               "run_id": home.parent.name, "state_path": str(directory / "state.json"),
               "log_path": str(directory / "calls.jsonl"), "tool_config_path": str(directory / "tools.json")}
        manifest = {"root": str(ROOT), "agent": agent, "launch": {"mock_mcp_python": app_python}}
        config = _profile_config(job, manifest, {})
        soul = (ROOT / "harness/benchmark_soul.md").read_text().replace("{{persona_name}}", name)
        templates[persona] = {"config.yaml": yaml.safe_dump(config, sort_keys=False), "SOUL.md": soul}
    return {"binary": str(binary), "source": str(source), "app_python": app_python,
            "templates": templates, "identity": {"source": source_identity, "runtime": runtime,
                "apps": apps, "reference_sha256": sha(SPEC_PATH.read_bytes()),
                "profile_builder_sha256": sha((ROOT / "harness/adapters/hermes_profile.py").read_bytes()),
                "soul_sha256": sha((ROOT / "harness/benchmark_soul.md").read_bytes())}}
