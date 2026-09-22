"""Build a Hermes-only image from tracked files in an explicit source checkout."""

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

import yaml


DOCKERFILE = """FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates nodejs npm && rm -rf /var/lib/apt/lists/*
COPY source/ /opt/hermes/
COPY plugin-requirements.txt /tmp/plugin-requirements.txt
RUN python -m pip install --no-cache-dir -e '/opt/hermes[mcp,honcho,cli,pty,cron]' -r /tmp/plugin-requirements.txt
WORKDIR /payload/work
"""


def stage_source(source: Path, destination: Path):
    names = subprocess.run(["git", "-C", str(source), "ls-files", "--stage", "-z"],
                           check=True, capture_output=True).stdout.decode().split("\x00")
    for record in filter(None, names):
        metadata, name = record.split("\t", 1)
        if metadata.split()[0] == "160000":
            # Optional training submodules are not part of the Hermes CLI package.
            continue
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe runtime source path")
        if any(part in {".env", "auth.json", "credentials.json", "tokens.json", ".git"}
               for part in relative.parts):
            raise ValueError("Runtime source contains a credential file")
        path = source / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Runtime source must contain regular tracked files: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    for name in ("pyproject.toml", "hermes_cli/main.py", "run_agent.py"):
        if not (destination / name).is_file():
            raise ValueError(f"Incomplete Hermes source: {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--tag", default="dolphinbench-hermes-agent:local")
    parser.add_argument("--backend", choices=("docker", "modal"), default="docker")
    parser.add_argument("--memory", choices=("builtin", "mem0", "honcho", "hindsight", "supermemory", "all"), default="builtin")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="db-image-") as raw:
        context = Path(raw)
        stage_source(args.source.resolve(), context / "source")
        dependencies = []
        providers = ("mem0", "honcho", "hindsight", "supermemory") if args.memory == "all" else (args.memory,)
        for provider in providers:
            if provider == "builtin":
                continue
            metadata = context / "source/plugins/memory" / provider / "plugin.yaml"
            requirements = yaml.safe_load(metadata.read_text()).get("pip_dependencies", [])
            if not isinstance(requirements, list) or not all(
                isinstance(value, str) and "\n" not in value and "\r" not in value
                for value in requirements
            ):
                raise ValueError("Plugin pip_dependencies must be a list of requirement strings")
            dependencies.extend(requirements)
        (context / "plugin-requirements.txt").write_text("\n".join(dependencies) + "\n")
        (context / "Dockerfile").write_text(DOCKERFILE)
        if args.backend == "modal":
            import modal
            app = modal.App.lookup("dolphinbench-agent-runtime", create_if_missing=True)
            image = (modal.Image.debian_slim(python_version="3.11")
                     .apt_install("git", "curl", "ca-certificates", "nodejs", "npm", "tar")
                     .add_local_dir(str(context / "source"), "/opt/hermes", copy=True)
                     .add_local_file(str(context / "plugin-requirements.txt"), "/tmp/plugin-requirements.txt", copy=True)
                     .run_commands("python -m pip install --no-cache-dir -e '/opt/hermes[mcp,honcho,cli,pty,cron]' -r /tmp/plugin-requirements.txt"))
            with modal.enable_output():
                print(image.build(app).object_id)
            return
        subprocess.run(["docker", "--context", "default", "build", "--tag", args.tag, str(context)], check=True)
    subprocess.run(["docker", "--context", "default", "image", "inspect", "--format", "{{.Id}}", args.tag], check=True)


if __name__ == "__main__":
    main()
