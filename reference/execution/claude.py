"""Reference claude implementation."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any


import modal

from reference import plan as planning
from reference.execution import claude_native as native
from reference.memory import services as diagnostic
from reference.execution import images as checked
from reference.memory import services as services


REPLACEMENT_RUN = os.environ.get("DOLPHINBENCH_CLAUDE_SUPERMEMORY_REPLACEMENT", "")
RUN_NAME = REPLACEMENT_RUN or f"{planning.PERSONA}-final-v1"
PRIVATE = Path("/root/dolphinbench-controller")
REMOTE_CORPUS = PRIVATE / "life_sim.yaml"
REMOTE_SCRIPTS = Path("/opt/dolphinbench/reference")
VOLUME_ROOT = Path("/mnt/claude-ingestion")
app = modal.App("dolphinbench-claude-five-ingestions" if planning.PERSONA == "morgan"
                else f"dolphinbench-claude-{planning.PERSONA}-five-ingestions")
app.include(native.app)


def _attach_runtime(image: Any, provider: str) -> Any:
    source = planning.PLUGIN_PATHS[provider]
    remote_source = checked._PLUGIN_SOURCES[provider]
    ignore = ["**/.git/**", "**/__pycache__/**"]
    if provider in {"mem0", "honcho"}:
        # Their former base layer included installed plugin dependencies.
        image = image.add_local_file(
            checked._require_file(planning.SOURCES / "SOURCE_MANIFEST.md"),
            str(checked.REMOTE_SOURCES / "SOURCE_MANIFEST.md"), copy=True,
        )
    else:
        ignore.append("**/node_modules/**")
    if provider == "honcho":
        # Tailscale starts at runtime, so package downloads cannot use its proxy during the build.
        image = image.run_commands(
            "env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY "
            "-u http_proxy -u https_proxy -u all_proxy "
            "python -m pip install pyyaml==6.0.3 modal uv"
        )
    elif provider == "hindsight":
        image = image.run_commands(
            "uv pip install --python /app/api/.venv/bin/python pyyaml==6.0.3 modal uv"
        )
    else:
        image = image.pip_install("pyyaml==6.0.3", "modal", "uv")
    image = (
        image.add_local_dir(str(planning.ROOT / "reference"), str(REMOTE_SCRIPTS), copy=True,
                       ignore=["**/__pycache__/**", "**/*.pyc"])
        .add_local_dir(str(planning.ROOT / "harness"), "/opt/dolphinbench/harness", copy=True,
                       ignore=["**/__pycache__/**", "**/*.pyc"])
        .add_local_dir(checked._require_source(source), str(remote_source), copy=True,
                       ignore=ignore)
        .add_local_file(str(planning.CORPUS), str(REMOTE_CORPUS), copy=True)
        .add_local_file(str(checked.claude_binary()), "/usr/local/bin/claude", copy=True)
        .env({"PYTHONPATH": "/opt/dolphinbench", "DOLPHINBENCH_CLAUDE_PERSONA": planning.PERSONA,
              native.native.ACCOUNT_ENV: native.native.ACCOUNT})
    )
    uid = planning.condition(provider, RUN_NAME)["uid"]
    return image.run_commands(
        f"chmod 0700 {PRIVATE}; chmod 0600 {REMOTE_CORPUS}; chmod 0755 /usr/local/bin/claude",
        f"getent passwd {uid} >/dev/null || useradd --uid {uid} --user-group --no-create-home claude-{provider}",
    )


def _with_node(image: Any) -> Any:
    return image.apt_install("curl", "git").run_commands(
        "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -; apt-get install -y nodejs",
        "npm install --global bun@1.3.6",
    )


images = {
    "mem0": _attach_runtime(checked.base_image, "mem0"),
    "honcho": _attach_runtime(checked.private_network_image, "honcho"),
    "hindsight": _attach_runtime(_with_node(services.claude_hindsight_image), "hindsight"),
    "supermemory": _attach_runtime(diagnostic.claude_supermemory_image, "supermemory").env({
        "BUN_JSC_useJIT": "false",
        "SUPERMEMORY_INGEST_CONCURRENCY": "1",
        "SUPERMEMORY_EMBEDDING_RAM_LIMIT": "4gb",
    }),
}


def _register(provider: str) -> Any:
    from reference.runtimes.claude_history import make_worker
    job = planning.condition(provider, RUN_NAME)
    volume = modal.Volume.from_name(job["volume_name"], create_if_missing=True)
    worker = make_worker(
        provider=provider, volume_name=job["volume_name"], run_name=RUN_NAME,
        corpus_path=REMOTE_CORPUS, plugin_source=checked._PLUGIN_SOURCES[provider],
        controller=PRIVATE / "state", volume_root=VOLUME_ROOT, honcho_url=checked.HONCHO_URL,
    )
    return app.function(
        name=f"ingest_{provider}", image=images[provider], serialized=True,
        cpu=job["cpu_request"], memory=job["memory_request_mib"], timeout=86400,
        max_containers=1, retries=0, nonpreemptible=True, volumes={str(VOLUME_ROOT): volume},
        secrets=[checked.evaluation_secret, checked.memory_services_secret, services.hindsight_secret]
        if provider in {"hindsight", "supermemory"} else [checked.evaluation_secret],
    )(worker)


workers = {provider: _register(provider) for provider in
           (("supermemory",) if REPLACEMENT_RUN else planning.PROVIDERS) if provider != "builtin"}


def submit_all(plan: dict, functions: dict, record: Any = None, providers: tuple = planning.PROVIDERS, calls: dict | None = None) -> dict:
    """Submit all five before waiting for any result."""
    calls = dict(calls or {})
    for provider in providers:
        calls[provider] = functions[provider](plan)
        if record is not None:
            record(calls)
    return calls


def recorded_call_ids(calls: dict, previous_calls: dict, previous_ids: dict) -> dict:
    """Retain known IDs without hydrating handles for old or failed calls."""
    return {provider: previous_ids[provider] if call is previous_calls.get(provider)
            else call.object_id for provider, call in calls.items()}


@app.local_entrypoint()
def main(confirm_paid_calls: bool = False, resume: bool = False, output: str = "artifacts/claude-ingestion-launch.json",
         providers: str = "", recovery_plan: str = "", background: bool = False,
         verify_only: bool = False) -> None:
    if not confirm_paid_calls:
        raise RuntimeError("No jobs launched: full ingestion requires --confirm-paid-calls")
    if planning.PERSONA != "morgan" and output == "artifacts/claude-ingestion-launch.json":
        output = f"artifacts/claude-{planning.PERSONA}-ingestion-launch.json"
    if REPLACEMENT_RUN and (providers != "supermemory" or RUN_NAME == "morgan-final-v1"
                            or output == "artifacts/claude-ingestion-launch.json"):
        raise RuntimeError("A replacement requires only Supermemory, a new store name, and a separate launch record")
    plan = planning.build_claude_ingestion_plan(RUN_NAME, supermemory_sessionend=bool(REPLACEMENT_RUN),
                               capture_supermemory=planning.PERSONA != "morgan")
    if native.native.ACCOUNT != "1":
        plan["claude_account"] = native.native.ACCOUNT
    native_plan = native.build_plan()
    if native_plan.corpus_sha256 != plan["corpus_sha256"]:
        raise RuntimeError("Built-in and plugin histories differ; no jobs launched")
    adjudications = json.loads(Path(recovery_plan).read_text()) if recovery_plan else None
    if adjudications and adjudications.get("runner_files_sha256") != plan["runner_files_sha256"]:
        raise RuntimeError("Recovery runner files differ from the reviewed code")
    recoveries = adjudications["recoveries"] if adjudications else {}
    functions = {provider: (lambda p, worker=worker, provider=provider: worker.spawn(
        p, confirm_paid_calls=True, recovery=recoveries.get(provider))) for provider, worker in workers.items()}
    functions["builtin"] = lambda p: native.ingest.spawn(confirm_paid_calls=True, recovery=recoveries.get("builtin"))
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    calls = {}
    selected = providers.split(",") if providers else list(planning.PROVIDERS)
    if not selected or len(set(selected)) != len(selected) or set(selected) - set(planning.PROVIDERS):
        raise RuntimeError("Select known, nonduplicated Claude providers")
    if adjudications and (not resume or set(selected) - set(recoveries)):
        raise RuntimeError("Every selected recovery needs an explicit adjudication and --resume")
    providers = list(selected)
    saved = {}
    if resume:
        saved = json.loads(destination.read_text())
        if saved["plan"] != plan:
            validate_recovery_plan(saved["plan"], plan, adjudications)
        calls = {provider: modal.FunctionCall.from_id(call_id) for provider, call_id in saved["call_ids"].items()}
        for provider, call_id in saved["call_ids"].items():
            if provider not in selected:
                continue
            if verify_only:
                continue
            call = modal.FunctionCall.from_id(call_id)
            cancelled_startup = (adjudications or {}).get("cancelled_startup_calls", {}).get(provider)
            if cancelled_startup:
                if cancelled_startup != call_id:
                    raise RuntimeError("The cancelled startup call differs from the saved call")
                call.cancel(terminate_containers=True)
                continue
            try:
                result = call.get(timeout=0)
            except TimeoutError:
                calls[provider] = call
                providers.remove(provider)
            except Exception:
                continue
            else:
                if result.get("status") in {"completed", "messages_completed"}:
                    providers.remove(provider)
                    calls[provider] = call
    elif destination.exists():
        raise RuntimeError("This launch record already exists; inspect its jobs before submitting any more")
    containers = json.loads(subprocess.run(["modal", "container", "list", "--json"], capture_output=True, text=True, check=True).stdout)
    container_limit = plan.get("maximum_total_modal_containers", 100)
    if not isinstance(containers, list) or len(containers) + len(providers) > container_limit:
        raise RuntimeError(f"Starting these jobs would exceed {container_limit} containers; no jobs launched")
    if verify_only:
        for provider in providers:
            if provider == "builtin":
                raise RuntimeError("Use the existing native auth check for native verification")
            print(json.dumps(workers[provider].remote(plan, confirm_paid_calls=True, verify_only=True)), flush=True)
        return
    previous_calls = dict(calls)
    def record(calls):
        from reference.runtimes.claude_code import _atomic_json
        history = list(saved.get("launch_history", []))
        if adjudications:
            history.append({"previous_plan": saved["plan"], "previous_call_ids": saved["call_ids"],
                            "recovery_plan": adjudications, "selected": selected})
        _atomic_json(destination, {"plan": plan, "call_ids": recorded_call_ids(calls, previous_calls, saved.get("call_ids", {})),
                                   "launch_history": history})
    record(calls)
    calls = submit_all(plan, functions, record, tuple(providers), calls)
    print(f"Submitted {len(providers)} ingestions; run identifiers saved to {destination}", flush=True)
    if background:
        return
    for provider, call in calls.items():
        try:
            print(json.dumps({"provider": provider, "result": call.get()}), flush=True)
        except Exception as exc:
            print(json.dumps({"provider": provider, "error": str(exc)}), flush=True)


def validate_recovery_plan(previous: dict, current: dict, adjudications: dict | None) -> None:
    """Permit only the reviewed runner update, never changed benchmark inputs."""
    previous_hash = hashlib.sha256(json.dumps(previous, sort_keys=True).encode()).hexdigest()
    if not adjudications or adjudications.get("previous_plan_sha256") != previous_hash:
        raise RuntimeError("The run configuration changed; inspect the difference before resuming")
    if adjudications.get("runner_files_sha256") != current["runner_files_sha256"]:
        raise RuntimeError("Recovery runner files differ from the reviewed code")
    unchanged_previous = {k: v for k, v in previous.items() if k != "runner_files_sha256"}
    unchanged_current = {k: v for k, v in current.items() if k != "runner_files_sha256"}
    account_change = adjudications.get("approved_claude_account_change")
    if account_change:
        previous_account = unchanged_previous.get("claude_account", "1")
        current_account = unchanged_current.get("claude_account", "1")
        if (account_change != {"from": previous_account, "to": current_account}
                or previous_account == current_account):
            raise RuntimeError("Claude account change differs from its recovery approval")
        unchanged_previous["claude_account"] = current_account
    if (adjudications.get("approved_total_modal_containers") == 100
            and unchanged_previous.get("maximum_total_modal_containers") == 9
            and unchanged_current.get("maximum_total_modal_containers") == 100):
        unchanged_previous["maximum_total_modal_containers"] = 100
    approved_resources = adjudications.get("approved_condition_resources", {})
    if approved_resources:
        previous_jobs = {job["provider"]: job for job in unchanged_previous["conditions"]}
        current_jobs = {job["provider"]: job for job in unchanged_current["conditions"]}
        for provider, changes in approved_resources.items():
            if provider not in previous_jobs or provider not in current_jobs:
                raise RuntimeError("Approved recovery resource names an unknown provider")
            for field, value in changes.items():
                if current_jobs[provider].get(field) != value:
                    raise RuntimeError("Current recovery resource differs from its approval")
                previous_jobs[provider][field] = value
    if unchanged_previous != unchanged_current:
        raise RuntimeError("Recovery cannot change the corpus, plugins, or run configuration")


import modal

from reference.execution import claude as launch

check_app = modal.App("dolphinbench-claude-ingestion-check")


def _register_check(provider):
    job = launch.planning.condition(provider, launch.RUN_NAME)
    image = launch.native.image if provider == "builtin" else launch.images[provider]
    secrets = [launch.checked.evaluation_secret]
    if provider in {"hindsight", "supermemory"}:
        secrets += [launch.checked.memory_services_secret, launch.services.hindsight_secret]

    @check_app.function(name=f"check_{provider}", image=image, serialized=True,
                  cpu=job["cpu_request"], memory=job["memory_request_mib"],
                  timeout=1800, retries=0, max_containers=1, secrets=secrets)
    def check():
        import os
        import subprocess
        import json
        from pathlib import Path

        home = Path(job["home"])
        home.mkdir(parents=True, exist_ok=True)
        home.chmod(0o700)
        os.chown(home, job["uid"], job["uid"])
        environment = {
            "HOME": str(home), "PATH": "/usr/local/bin:/usr/bin:/bin",
            "CLAUDE_CODE_OAUTH_TOKEN": os.environ["CLAUDE_CODE_OAUTH_TOKEN"],
        }
        binary = "/home/ubuntu/.local/bin/claude" if provider == "builtin" else "/usr/local/bin/claude"
        def cli(*args):
            return subprocess.run([binary, *args], env=environment, user=job["uid"],
                                  group=job["uid"], capture_output=True, text=True, check=True).stdout
        version = cli("--version").strip()
        auth = json.loads(cli("auth", "status"))
        result = {"provider": provider, "version": version,
                  "authentication": {key: auth.get(key) for key in
                                     ("loggedIn", "authMethod", "apiProvider", "subscriptionType")},
                  "model_calls": 0, "history_messages": 0}
        if not version.startswith(job["claude_version"]):
            raise RuntimeError("Claude version does not match the ingestion plan")
        if not (auth.get("loggedIn") and auth.get("authMethod") == "oauth_token"
                and auth.get("apiProvider") == "firstParty"):
            raise RuntimeError("Claude subscription authentication is unavailable")
        if provider in {"hindsight", "supermemory"}:
            from reference.runtimes.claude_history import check_database
            result["database"] = check_database(provider)
        return result
    return check


checks = {provider: _register_check(provider) for provider in launch.planning.PROVIDERS}


@check_app.local_entrypoint()
def check_images(output: str = "artifacts/claude-ingestion-remote-check.json", provider: str = "all"):
    selected = checks if provider == "all" else {provider: checks[provider]}
    containers = json.loads(subprocess.run(["modal", "container", "list", "--json"],
                                           capture_output=True, text=True, check=True).stdout)
    if len(containers) + len(selected) > 9:
        raise RuntimeError("These checks would exceed the approved nine-container limit")
    destination = Path(output)
    if destination.exists():
        raise RuntimeError("Choose a new output path; existing results will not be overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True)
    calls = {provider: check.spawn() for provider, check in selected.items()}
    report = {"call_ids": {p: c.object_id for p, c in calls.items()}, "results": {}}
    destination.write_text(json.dumps(report, indent=2) + "\n")
    for provider, call in calls.items():
        try:
            report["results"][provider] = call.get()
        except Exception as error:
            report["results"][provider] = {"error_type": type(error).__name__, "error": str(error)}
        destination.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({provider: report["results"][provider]}), flush=True)
