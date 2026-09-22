"""Reference services implementation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import socket
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
import hmac
import http.client
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


import modal


APP_NAME = "dolphinbench-memory-services"
ROOT = Path(__file__).resolve().parents[2]
SERVICE_SECRET_NAME = "dolphinbench-memory-services"
AZURE_ENDPOINT_ENV = "AZURE_OPENAI_ENDPOINT"
AZURE_API_KEY_ENV = "AZURE_OPENAI_API_KEY"
HINDSIGHT_API_KEY_ENV = "HINDSIGHT_API_KEY"
SUPERMEMORY_API_KEY_ENV = "SUPERMEMORY_API_KEY"

HINDSIGHT_VERSION = "0.9.2"
SUPERMEMORY_VERSION = "0.0.8"
HINDSIGHT_IMAGE = (
    "ghcr.io/vectorize-io/hindsight:0.9.2@sha256:"
    "3b46e26ec69355422c46ceb496cd758ae226d751be4a0799b4e844251d524d46"
)
SUPERMEMORY_BINARY_URL = (
    "https://github.com/supermemoryai/supermemory/releases/download/"
    "server-v0.0.8/supermemory-server-linux-x64"
)
SUPERMEMORY_BINARY_SHA256 = (
    "87f32433d0179be80bb9d8a1bafbac65af4128324342a27ecb8bd1a77b5506f3"
)
MODEL_ID = "gpt-5.6-luna"
FINAL_PERSONA = "morgan"
FINAL_SOURCE_COUNT = 3400
FINAL_CORPUS_SHA256 = "082f1b1bda22954ae586c7e80567a9f85b0163023d726078f179d4e8aea42f74"
FINAL_MEMORY_IDENTITY = "dolphinbench-morgan-500k-final-v1"

HINDSIGHT_SNAPSHOT_VOLUME_NAME = "dolphinbench-hindsight-final-snapshot"
SUPERMEMORY_SNAPSHOT_VOLUME_NAME = "dolphinbench-supermemory-final-snapshot"
HINDSIGHT_SNAPSHOT_ROOT = Path("/mnt/hindsight-snapshot")
SUPERMEMORY_SNAPSHOT_ROOT = Path("/mnt/supermemory-snapshot")
HINDSIGHT_RUNTIME_ROOT = Path("/home/hindsight/.pg0")
SUPERMEMORY_RUNTIME_ROOT = Path("/tmp/dolphinbench-supermemory")
SUPERMEMORY_SERVER_LOG_PATH = Path("/tmp/dolphinbench-supermemory-server.log")
SUPERMEMORY_ENGINE_PID_PATH = Path("runtime/rivet/engine.pid")
SNAPSHOT_ARCHIVE_NAME = "snapshot.tar.gz"
SNAPSHOT_CHECKSUM_NAME = "snapshot.sha256"
INGESTION_RECEIPT_NAME = "ingestion-receipt.json"
HEALTH_MARKER_NAME = "health-marker.json"


class SnapshotError(RuntimeError):
    """A snapshot cannot safely be restored."""


@dataclass(frozen=True)
class ProviderSnapshot:
    """Names and paths for one provider's frozen data."""

    provider: str
    version: str
    volume_name: str
    volume_root: Path

    @property
    def archive_path(self) -> Path:
        return self.volume_root / SNAPSHOT_ARCHIVE_NAME

    @property
    def checksum_path(self) -> Path:
        return self.volume_root / SNAPSHOT_CHECKSUM_NAME

    @property
    def receipt_path(self) -> Path:
        return self.volume_root / INGESTION_RECEIPT_NAME

    @property
    def marker_path(self) -> Path:
        return self.volume_root / HEALTH_MARKER_NAME


HINDSIGHT_SNAPSHOT = ProviderSnapshot(
    provider="hindsight",
    version=HINDSIGHT_VERSION,
    volume_name=HINDSIGHT_SNAPSHOT_VOLUME_NAME,
    volume_root=HINDSIGHT_SNAPSHOT_ROOT,
)
SUPERMEMORY_SNAPSHOT = ProviderSnapshot(
    provider="supermemory",
    version=SUPERMEMORY_VERSION,
    volume_name=SUPERMEMORY_SNAPSHOT_VOLUME_NAME,
    volume_root=SUPERMEMORY_SNAPSHOT_ROOT,
)


def _validate_final_receipt(
    provider: str,
    receipt: Mapping[str, Any],
    *,
    expected_identity: str = FINAL_MEMORY_IDENTITY,
) -> str | None:
    """Return why a receipt cannot represent the final Morgan ingestion."""

    if receipt.get("provider") != provider or receipt.get("persona") != FINAL_PERSONA:
        return "ingestion receipt has the wrong provider or persona"
    if receipt.get("ingestion_path") == "hermes_official_memory_plugin":
        completed = receipt.get("completed_source_ids")
        if (
            receipt.get("schema_version") != 1
            or receipt.get("identity") != expected_identity
            or receipt.get("status") != "completed"
            or receipt.get("source_count") != FINAL_SOURCE_COUNT
            or receipt.get("corpus_sha256") != FINAL_CORPUS_SHA256
            or not isinstance(completed, list)
            or len(completed) != FINAL_SOURCE_COUNT
            or len(set(completed)) != FINAL_SOURCE_COUNT
        ):
            return "receipt does not describe 3,400 unique messages ingested through Hermes"
        return None
    if provider == "hindsight":
        if (
            receipt.get("schema_version") != 1
            or receipt.get("hindsight_version") != HINDSIGHT_VERSION
            or receipt.get("bank_id") != expected_identity
            or receipt.get("status") != "completed"
            or receipt.get("documents_total") != FINAL_SOURCE_COUNT
            or receipt.get("corpus_sha256") != FINAL_CORPUS_SHA256
        ):
            return "Hindsight receipt does not describe the completed final Morgan ingestion"
        batches = receipt.get("batches")
        if not isinstance(batches, list) or not batches:
            return "Hindsight receipt has no completed batches"
        document_ids: list[str] = []
        for batch in batches:
            if not isinstance(batch, Mapping) or batch.get("status") != "completed":
                return "Hindsight receipt contains an incomplete batch"
            planned = batch.get("document_ids")
            completed = batch.get("completed_document_ids")
            if not isinstance(planned, list) or not isinstance(completed, list):
                return "Hindsight receipt batch is missing document IDs"
            if set(planned) != set(completed):
                return "Hindsight receipt batch completion does not match its planned documents"
            document_ids.extend(str(item) for item in planned)
        if len(document_ids) != FINAL_SOURCE_COUNT or len(set(document_ids)) != FINAL_SOURCE_COUNT:
            return "Hindsight receipt does not contain 3,400 unique document IDs"
        verification = receipt.get("verification")
        if not isinstance(verification, Mapping) or any(
            verification.get(key) != value
            for key, value in {
                "expected_count": FINAL_SOURCE_COUNT,
                "actual_count": FINAL_SOURCE_COUNT,
                "missing": [],
                "unexpected": [],
            }.items()
        ):
            return "Hindsight receipt does not verify exact source coverage"
        return None

    if (
        receipt.get("receipt_version") != 2
        or receipt.get("container_tag") != expected_identity
        or receipt.get("status") != "complete"
    ):
        return "Supermemory receipt does not describe the completed final Morgan ingestion"
    corpus = receipt.get("corpus")
    sources = receipt.get("sources")
    if not isinstance(corpus, Mapping) or (
        corpus.get("sha256") != FINAL_CORPUS_SHA256
        or corpus.get("source_count") != FINAL_SOURCE_COUNT
    ):
        return "Supermemory receipt has the wrong corpus hash or source count"
    if not isinstance(sources, Mapping) or len(sources) != FINAL_SOURCE_COUNT:
        return "Supermemory receipt does not contain 3,400 sources"
    if any(
        not isinstance(record, Mapping) or record.get("status") != "complete"
        for record in sources.values()
    ):
        return "Supermemory receipt contains an incomplete source"
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_checksum(path: Path) -> str:
    if not path.is_file():
        raise SnapshotError(f"missing snapshot checksum: {path}")
    value = path.read_text(encoding="ascii").strip().split(maxsplit=1)
    if not value or len(value[0]) != 64:
        raise SnapshotError(f"invalid snapshot checksum: {path}")
    try:
        int(value[0], 16)
    except ValueError as exc:
        raise SnapshotError(f"invalid snapshot checksum: {path}") from exc
    return value[0].lower()


def snapshot_status(
    snapshot: ProviderSnapshot,
    *,
    expected_identity: str = FINAL_MEMORY_IDENTITY,
) -> dict[str, Any]:
    """Describe a provider volume without changing its frozen snapshot."""

    archive_present = snapshot.archive_path.is_file()
    checksum_present = snapshot.checksum_path.is_file()
    receipt_present = snapshot.receipt_path.is_file()
    marker_present = snapshot.marker_path.is_file()
    result: dict[str, Any] = {
        "provider": snapshot.provider,
        "version": snapshot.version,
        "volume_name": snapshot.volume_name,
        "archive_present": archive_present,
        "checksum_present": checksum_present,
        "receipt_present": receipt_present,
        "marker_present": marker_present,
        "snapshot_ready": False,
    }
    if not archive_present:
        return result
    if not checksum_present or not receipt_present:
        result["error"] = "an archive requires both a checksum file and an ingestion receipt"
        return result
    expected = _read_checksum(snapshot.checksum_path)
    actual = _sha256_file(snapshot.archive_path)
    result["archive_sha256"] = actual
    if actual != expected:
        result["error"] = "snapshot archive SHA-256 does not match its checksum file"
        return result
    try:
        receipt = json.loads(snapshot.receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result["error"] = f"invalid ingestion receipt: {exc}"
        return result
    if not isinstance(receipt, Mapping):
        result["error"] = "ingestion receipt must be a JSON object"
        return result
    receipt_error = _validate_final_receipt(
        snapshot.provider, receipt, expected_identity=expected_identity,
    )
    if receipt_error:
        result["error"] = receipt_error
        return result
    result["snapshot_ready"] = True
    return result


def _snapshot_extract_filter(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo | None:
    """Apply tarfile's path checks without changing saved Unix permissions."""

    saved_mode = member.mode
    filtered = tarfile.data_filter(member, destination)
    if filtered is not None:
        filtered.mode = saved_mode & 0o777
    return filtered


def discard_restored_supermemory_pid(destination: Path) -> None:
    """A saved process ID cannot identify an engine in a new container."""

    (destination / SUPERMEMORY_ENGINE_PID_PATH).unlink(missing_ok=True)


def restore_snapshot(
    snapshot: ProviderSnapshot,
    destination: Path,
    *,
    expected_identity: str = FINAL_MEMORY_IDENTITY,
) -> bool:
    """Restore a verified snapshot archive to local container disk.

    Returns ``False`` when the provider has no snapshot yet. That state is
    allowed only for the empty-service health check; it is never an ingestion
    success and cannot be used for evaluation.
    """

    state = snapshot_status(snapshot, expected_identity=expected_identity)
    if not state["archive_present"]:
        destination.mkdir(parents=True, exist_ok=True)
        return False
    if not state["snapshot_ready"]:
        raise SnapshotError(str(state.get("error") or "snapshot is incomplete"))
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    try:
        with tarfile.open(snapshot.archive_path, "r:gz") as archive:
            archive.extractall(destination, filter=_snapshot_extract_filter)
    except (OSError, tarfile.TarError) as exc:
        raise SnapshotError(f"cannot restore {snapshot.provider} snapshot: {exc}") from exc
    if snapshot.provider == "supermemory":
        discard_restored_supermemory_pid(destination)
    return True


def write_health_marker(snapshot: ProviderSnapshot) -> dict[str, Any]:
    """Write a tiny marker. It never writes a provider database to a volume."""

    snapshot.volume_root.mkdir(parents=True, exist_ok=True)
    marker = {
        "provider": snapshot.provider,
        "version": snapshot.version,
        "kind": "empty-service-health-marker",
    }
    snapshot.marker_path.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
    return marker


def azure_openai_base_url(endpoint: str) -> str:
    """Return the OpenAI-compatible Azure URL used by both providers."""

    endpoint = endpoint.strip().rstrip("/")
    if not endpoint.startswith("https://"):
        raise RuntimeError("AZURE_OPENAI_ENDPOINT must be an https URL")
    return f"{endpoint}/openai/v1/"


def _wait_for_port(port: int, process: subprocess.Popen[bytes], timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"memory service exited before opening port {port}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"memory service did not open port {port} within {timeout_seconds} seconds")


def _wait_for_supermemory_api(
    process: subprocess.Popen[bytes],
    api_key: str,
    *,
    timeout_seconds: float = 180,
) -> None:
    """Wait until Supermemory can read its data, not merely accept TCP connections."""

    deadline = time.monotonic() + timeout_seconds
    last_error = "the API did not respond"
    body = json.dumps({
        "containerTags": ["dolphinbench-startup-check"],
        "limit": 1,
        "page": 1,
    }).encode("utf-8")
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Supermemory exited before its API became ready")
        request = urllib.request.Request(
            "http://127.0.0.1:6767/v3/documents/list",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            if "Service is starting" not in response_body:
                raise RuntimeError(
                    f"Supermemory readiness request failed with HTTP {exc.code}: {response_body}"
                ) from exc
            last_error = response_body
        except (TimeoutError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(
        f"Supermemory API did not become ready within {timeout_seconds} seconds: {last_error}"
    )


def _stop_failed_supermemory_start(process: subprocess.Popen[bytes]) -> None:
    """Stop the isolated server process group after a startup failure."""

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


def _start_supermemory(*, require_snapshot: bool = True) -> subprocess.Popen[bytes]:
    """Restore Supermemory to local disk and start one server process."""

    restored = restore_snapshot(SUPERMEMORY_SNAPSHOT, SUPERMEMORY_RUNTIME_ROOT)
    if require_snapshot and not restored:
        raise SnapshotError("Supermemory cannot start for evaluation without a final snapshot")
    api_key = os.environ.get(SUPERMEMORY_API_KEY_ENV, "").strip()
    endpoint = os.environ.get(AZURE_ENDPOINT_ENV, "").strip()
    azure_key = os.environ.get(AZURE_API_KEY_ENV, "").strip()
    if not api_key or not endpoint or not azure_key:
        raise RuntimeError(
            "dolphinbench-memory-services must contain SUPERMEMORY_API_KEY, "
            "AZURE_OPENAI_ENDPOINT, and AZURE_OPENAI_API_KEY"
        )
    SUPERMEMORY_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    _start_supermemory_openai_proxy()
    environment = {
        **os.environ,
        "SUPERMEMORY_DATA_DIR": str(SUPERMEMORY_RUNTIME_ROOT),
        "SUPERMEMORY_PORT": "6767",
        "SUPERMEMORY_DISABLE_TELEMETRY": "1",
        "OPENAI_API_KEY": azure_key,
        "OPENAI_BASE_URL": "http://127.0.0.1:6766/v1",
        "OPENAI_MODEL": MODEL_ID,
        "DOLPHINBENCH_SUPERMEMORY_SNAPSHOT_RESTORED": "1" if restored else "0",
    }
    SUPERMEMORY_SERVER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SUPERMEMORY_SERVER_LOG_PATH.open("wb", buffering=0) as server_log:
        server_log.write(
            f"\n--- Supermemory start {time.time_ns()} ---\n".encode("ascii")
        )
        process = subprocess.Popen(
            ["/usr/local/bin/supermemory-server"],
            env=environment,
            start_new_session=True,
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
    try:
        _wait_for_port(6767, process, timeout_seconds=60)
        api_key = _supermemory_server_key()
        _wait_for_supermemory_api(process, api_key)
    except BaseException:
        _stop_failed_supermemory_start(process)
        if SUPERMEMORY_SERVER_LOG_PATH.is_file():
            print(SUPERMEMORY_SERVER_LOG_PATH.read_text(errors="replace")[-8000:], flush=True)
        raise
    return process


def _start_supermemory_openai_proxy() -> subprocess.Popen[bytes] | None:
    """Start the local request adapter once per container."""

    try:
        with socket.create_connection(("127.0.0.1", 6766), timeout=0.2):
            return None
    except OSError:
        pass
    environment = {
        **os.environ,
        "AZURE_OPENAI_UPSTREAM_BASE_URL": azure_openai_base_url(
            os.environ[AZURE_ENDPOINT_ENV]
        ),
    }
    process = subprocess.Popen(
        ["python3", "/opt/dolphinbench/openai_compat_proxy.py", "openai"],
        env=environment,
    )
    _wait_for_port(6766, process, timeout_seconds=30)
    return process


def _supermemory_server_key(*, timeout_seconds: float = 30) -> str:
    """Read the bearer key generated by the self-hosted Supermemory server."""

    path = SUPERMEMORY_RUNTIME_ROOT / "api-key"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.is_file():
            key = path.read_text(encoding="utf-8").strip()
            if key:
                return key
        time.sleep(0.1)
    raise RuntimeError("Supermemory did not create its API key")


def _start_supermemory_read_proxy() -> subprocess.Popen[bytes]:
    """Require the Modal secret and expose only retrieval endpoints."""

    external_key = os.environ.get(SUPERMEMORY_API_KEY_ENV, "").strip()
    if not external_key:
        raise RuntimeError("SUPERMEMORY_API_KEY is required for the retrieval proxy")
    environment = {
        **os.environ,
        "SUPERMEMORY_PROXY_API_KEY": external_key,
        "SUPERMEMORY_UPSTREAM_API_KEY": _supermemory_server_key(),
    }
    process = subprocess.Popen(
        ["python3", "/opt/dolphinbench/supermemory_read_proxy.py", "supermemory"],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_for_port(6768, process, timeout_seconds=30)
    return process


def _hindsight_environment() -> dict[str, str]:
    """Configure the embedded-pg0 Hindsight API without starting ingestion."""

    api_key = os.environ.get(HINDSIGHT_API_KEY_ENV, "").strip()
    endpoint = os.environ.get(AZURE_ENDPOINT_ENV, "").strip()
    azure_key = os.environ.get(AZURE_API_KEY_ENV, "").strip()
    if not api_key or not endpoint or not azure_key:
        raise RuntimeError(
            "dolphinbench-memory-services must contain HINDSIGHT_API_KEY, "
            "AZURE_OPENAI_ENDPOINT, and AZURE_OPENAI_API_KEY"
        )
    return {
        **os.environ,
        "HOME": "/home/hindsight",
        "HINDSIGHT_ENABLE_API": "true",
        "HINDSIGHT_ENABLE_CP": "false",
        "HINDSIGHT_API_HOST": "0.0.0.0",
        "HINDSIGHT_API_PORT": "8888",
        "HINDSIGHT_API_LLM_PROVIDER": "openai",
        "HINDSIGHT_API_LLM_MODEL": MODEL_ID,
        "HINDSIGHT_API_LLM_BASE_URL": azure_openai_base_url(endpoint),
        "HINDSIGHT_API_LLM_API_KEY": azure_key,
        "HINDSIGHT_API_SKIP_LLM_VERIFICATION": "true",
        "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "local",
        "HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL": "BAAI/bge-small-en-v1.5",
        "HINDSIGHT_API_TENANT_EXTENSION": "hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension",
        "HINDSIGHT_API_TENANT_API_KEY": api_key,
        "HINDSIGHT_API_LOG_LEVEL": "info",
    }


def _start_hindsight(
    *,
    require_snapshot: bool = True,
    environment_overrides: Mapping[str, str] | None = None,
    snapshot: ProviderSnapshot = HINDSIGHT_SNAPSHOT,
    expected_identity: str = FINAL_MEMORY_IDENTITY,
) -> subprocess.Popen[bytes]:
    """Restore embedded pg0 state to local disk and start Hindsight 0.9.2."""

    restored = restore_snapshot(
        snapshot,
        HINDSIGHT_RUNTIME_ROOT,
        expected_identity=expected_identity,
    )
    if require_snapshot and not restored:
        raise SnapshotError("Hindsight cannot start for evaluation without a final snapshot")
    subprocess.run(
        ["chown", "-R", "1000:1000", str(HINDSIGHT_RUNTIME_ROOT)],
        check=True,
    )
    environment = _hindsight_environment()
    if environment_overrides:
        environment.update(environment_overrides)
    process = subprocess.Popen(
        ["/app/start-all.sh"],
        env=environment,
        user=1000,
        group=1000,
    )
    _wait_for_port(8888, process, timeout_seconds=180)
    return process


def _local_hindsight_health() -> int:
    """Read only the local health endpoint. This does not call a model."""

    request = urllib.request.Request(
        "http://127.0.0.1:8888/health",
        headers={"Authorization": f"Bearer {os.environ[HINDSIGHT_API_KEY_ENV]}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


app = modal.App(APP_NAME)
supermemory_secret = modal.Secret.from_name(
    SERVICE_SECRET_NAME,
    required_keys=[
        AZURE_ENDPOINT_ENV,
        AZURE_API_KEY_ENV,
        SUPERMEMORY_API_KEY_ENV,
    ],
)
hindsight_secret = modal.Secret.from_name(
    SERVICE_SECRET_NAME,
    required_keys=[
        AZURE_ENDPOINT_ENV,
        AZURE_API_KEY_ENV,
        HINDSIGHT_API_KEY_ENV,
    ],
)
hindsight_snapshot_volume = modal.Volume.from_name(
    HINDSIGHT_SNAPSHOT_VOLUME_NAME,
    create_if_missing=True,
)
supermemory_snapshot_volume = modal.Volume.from_name(
    SUPERMEMORY_SNAPSHOT_VOLUME_NAME,
    create_if_missing=True,
)

supermemory_image = (
    modal.Image.debian_slim()
    .apt_install("ca-certificates", "curl", "tar")
    .run_commands(
        "set -eu; "
        f"curl --fail --location --silent --show-error '{SUPERMEMORY_BINARY_URL}' "
        "--output /tmp/supermemory-server; "
        f"echo '{SUPERMEMORY_BINARY_SHA256}  /tmp/supermemory-server' "
        "| sha256sum --check --strict; "
        "install --mode=0755 /tmp/supermemory-server /usr/local/bin/supermemory-server; "
        "rm /tmp/supermemory-server",
    )
    .add_local_file(
        str(ROOT / 'reference' / 'memory' / 'proxy.py'),
        "/opt/dolphinbench/supermemory_read_proxy.py",
        copy=True,
    )
    .add_local_file(
        str(ROOT / 'reference' / 'memory' / 'proxy.py'),
        "/opt/dolphinbench/openai_compat_proxy.py",
        copy=True,
    )
)
hindsight_image = modal.Image.from_registry(HINDSIGHT_IMAGE)
hindsight_evaluation_image = hindsight_image.add_local_file(
    str(ROOT / 'reference' / 'memory' / 'proxy.py'),
    "/opt/dolphinbench/hindsight_read_proxy.py", copy=True,
)

# Keep existing Hermes runs on their original image. Claude uses the tested
# named-tool request format with the same Hindsight version and plugin.
if modal.is_local():
    from reference.memory.hindsight import use_named_openai_tool_choice

    claude_hindsight_image = hindsight_image.run_function(use_named_openai_tool_choice)
else:
    claude_hindsight_image = hindsight_image


@app.function(
    image=hindsight_evaluation_image,
    cpu=2,
    memory=4096,
    min_containers=0,
    max_containers=1,
    scaledown_window=600,
    timeout=86_400,
    startup_timeout=300,
    secrets=[hindsight_secret],
    volumes={
        str(HINDSIGHT_SNAPSHOT_ROOT): hindsight_snapshot_volume.with_mount_options(read_only=True),
    },
)
@modal.web_server(8889, startup_timeout=210, label="dolphinbench-hindsight-final")
def hindsight_service() -> None:
    """Run one authenticated Hindsight 0.9.2 service from a frozen snapshot."""

    _start_hindsight(environment_overrides={"HINDSIGHT_API_HOST": "127.0.0.1"})
    proxy = subprocess.Popen(
        ["python", "/opt/dolphinbench/hindsight_read_proxy.py", "hindsight"],
        env={**os.environ, "DOLPHINBENCH_HINDSIGHT_BANK_ID": FINAL_MEMORY_IDENTITY},
    )
    _wait_for_port(8889, proxy, timeout_seconds=20)


@app.function(
    image=supermemory_image,
    cpu=2,
    memory=4096,
    min_containers=0,
    max_containers=1,
    scaledown_window=600,
    timeout=86_400,
    startup_timeout=300,
    secrets=[supermemory_secret],
    volumes={
        str(SUPERMEMORY_SNAPSHOT_ROOT): supermemory_snapshot_volume.with_mount_options(read_only=True),
    },
)
@modal.web_server(6768, startup_timeout=90)
def supermemory_service() -> None:
    """Run one authenticated Supermemory 0.0.8 service from a frozen snapshot."""

    _start_supermemory()
    _start_supermemory_read_proxy()


@app.function(
    volumes={str(HINDSIGHT_SNAPSHOT_ROOT): hindsight_snapshot_volume},
    timeout=60,
)
def check_hindsight_volume() -> dict[str, Any]:
    """Persist and report the Hindsight health marker without starting Hindsight."""

    marker = write_health_marker(HINDSIGHT_SNAPSHOT)
    hindsight_snapshot_volume.commit()
    return {"marker": marker, "snapshot": snapshot_status(HINDSIGHT_SNAPSHOT)}


@app.function(
    volumes={str(SUPERMEMORY_SNAPSHOT_ROOT): supermemory_snapshot_volume},
    timeout=60,
)
def check_supermemory_volume() -> dict[str, Any]:
    """Persist and report the Supermemory health marker without starting its service."""

    marker = write_health_marker(SUPERMEMORY_SNAPSHOT)
    supermemory_snapshot_volume.commit()
    return {"marker": marker, "snapshot": snapshot_status(SUPERMEMORY_SNAPSHOT)}


@app.function(
    image=hindsight_image,
    cpu=2,
    memory=4096,
    timeout=300,
    startup_timeout=210,
    single_use_containers=True,
    secrets=[hindsight_secret],
    volumes={str(HINDSIGHT_SNAPSHOT_ROOT): hindsight_snapshot_volume},
)
def hindsight_startup_health() -> dict[str, Any]:
    """Start an empty or restored Hindsight service and check only /health.

    This function is an explicit pre-deployment smoke check. It disables
    Hindsight's startup model request, starts no ingestion, and requests only
    the local health endpoint. It must not run while the persistent Hindsight
    service is deployed.
    """

    process = _start_hindsight(require_snapshot=False)
    try:
        return {
            "provider": "hindsight",
            "version": HINDSIGHT_VERSION,
            "snapshot_restored": snapshot_status(HINDSIGHT_SNAPSHOT)["snapshot_ready"],
            "health_status": _local_hindsight_health(),
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


@app.function(
    image=supermemory_image,
    cpu=2,
    memory=4096,
    timeout=120,
    startup_timeout=90,
    single_use_containers=True,
    secrets=[supermemory_secret],
    volumes={str(SUPERMEMORY_SNAPSHOT_ROOT): supermemory_snapshot_volume},
)
def supermemory_startup_health() -> dict[str, Any]:
    """Start Supermemory, confirm its local port opens, and stop it.

    This function starts no ingestion and makes no model request. It must not
    run while the persistent Supermemory service is deployed.
    """

    process = _start_supermemory(require_snapshot=False)
    try:
        return {
            "provider": "supermemory",
            "version": SUPERMEMORY_VERSION,
            "snapshot_restored": snapshot_status(SUPERMEMORY_SNAPSHOT)["snapshot_ready"],
            "port_open": True,
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def check_http_health(url: str, *, timeout_seconds: float = 180) -> dict[str, Any]:
    """Check that a deployed endpoint responds without sending model traffic."""

    request = urllib.request.Request(url.rstrip("/") + "/health")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return {"url": request.full_url, "reachable": True, "status": response.status}
    except urllib.error.HTTPError as error:
        # Authentication can legitimately return 401 or 403. It still proves
        # the service is alive and did not contact a model.
        return {
            "url": request.full_url,
            "reachable": error.code in {401, 403, 404},
            "status": error.code,
        }
    except (TimeoutError, urllib.error.URLError) as error:
        return {
            "url": request.full_url,
            "reachable": False,
            "error": str(getattr(error, "reason", error)),
        }


@app.function(timeout=300)
def check_worker_access(hindsight_url: str, supermemory_url: str) -> dict[str, Any]:
    """Confirm that a separate Modal worker can reach both service URLs."""

    result = {
        "hindsight": check_http_health(hindsight_url),
        "supermemory": check_http_health(supermemory_url),
    }
    print(json.dumps(result, sort_keys=True))
    if not all(item["reachable"] for item in result.values()):
        raise RuntimeError("a Modal worker could not reach both memory services")
    return result


@app.local_entrypoint()
def health(url: str) -> None:
    """Run ``modal run ...::health --url <service-url>`` after deployment."""

    print(json.dumps(check_http_health(url), sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot-status", help="inspect local snapshot files without Modal")
    snapshot.add_argument("provider", choices=("hindsight", "supermemory"))
    snapshot.add_argument("--root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "snapshot-status":
        template = HINDSIGHT_SNAPSHOT if args.provider == "hindsight" else SUPERMEMORY_SNAPSHOT
        snapshot = ProviderSnapshot(
            provider=template.provider,
            version=template.version,
            volume_name=template.volume_name,
            volume_root=args.root.expanduser().resolve(),
        )
        print(json.dumps(snapshot_status(snapshot), indent=2, sort_keys=True))
        return 0
    raise AssertionError(f"unsupported command {args.command!r}")


import modal

from reference.memory import services as memory_services


OFFICIAL_MCP_COMMIT = "4d8a4ebfddadc3430f7f59a752cd374670833f50"
OFFICIAL_MCP_SOURCE = Path(
    "/tmp/dolphinbench-supermemory-claude-check/upstream/apps/mcp"
)
OFFICIAL_MCP_ROOT = Path("/opt/dolphinbench/supermemory-mcp")

HINDSIGHT_PORT = 8888
SUPERMEMORY_PORT = 6767
SUPERMEMORY_MCP_PORT = 18768
SUPERMEMORY_PUBLIC_PORT = 8767
CLAUDE_SUPERMEMORY_RUNTIME_ROOT = memory_services.SUPERMEMORY_RUNTIME_ROOT
_HOP_BY_HOP_HEADERS = frozenset({
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
})


class ServiceError(RuntimeError):
    """The isolated diagnostic service cannot be started safely."""


def _wait_for_claude_port(port: int, process: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ServiceError(
                f"process for port {port} exited with {process.returncode}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.25)
    raise ServiceError(f"service did not open local port {port}")


class SupermemoryProxy(BaseHTTPRequestHandler):
    """Expose the official MCP and local API on one authenticated HTTPS URL."""

    protocol_version = "HTTP/1.1"
    external_api_key = ""
    internal_api_key = ""

    def do_GET(self) -> None:  # noqa: N802
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802
        self._proxy()

    def do_DELETE(self) -> None:  # noqa: N802
        self._proxy()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._proxy()

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _proxy(self) -> None:
        target_port = SUPERMEMORY_MCP_PORT if self.path.startswith("/mcp") else SUPERMEMORY_PORT
        body_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(body_length) if body_length else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP_BY_HOP_HEADERS and key.lower() != "host"
        }
        if target_port == SUPERMEMORY_PORT and self.path != "/health":
            authorization = self.headers.get("Authorization", "")
            expected = f"Bearer {self.external_api_key}"
            if not hmac.compare_digest(authorization, expected):
                self.send_error(401, "Unauthorized")
                return
            headers = {key: value for key, value in headers.items() if key.lower() != "authorization"}
            headers["Authorization"] = f"Bearer {self.internal_api_key}"

        connection = http.client.HTTPConnection("127.0.0.1", target_port, timeout=120)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in _HOP_BY_HOP_HEADERS:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)
        finally:
            connection.close()


def _start_supermemory_proxy(internal_api_key: str) -> ThreadingHTTPServer:
    external_api_key = os.environ.get(memory_services.SUPERMEMORY_API_KEY_ENV, "").strip()
    if not external_api_key:
        raise ServiceError("SUPERMEMORY_API_KEY is required for the diagnostic proxy")
    handler = type(
        "AuthenticatedSupermemoryProxy",
        (SupermemoryProxy,),
        {"external_api_key": external_api_key, "internal_api_key": internal_api_key},
    )
    server = ThreadingHTTPServer(("0.0.0.0", SUPERMEMORY_PUBLIC_PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _assert_official_mcp_checkout() -> None:
    if not (OFFICIAL_MCP_ROOT / "node_modules/.bin/wrangler").is_file():
        raise ServiceError(
            "pinned official Supermemory MCP source or built node_modules is unavailable"
        )


def _start_official_mcp() -> subprocess.Popen[bytes]:
    command = [
        str(OFFICIAL_MCP_ROOT / "node_modules/.bin/wrangler"),
        "dev",
        "--ip",
        "127.0.0.1",
        "--port",
        str(SUPERMEMORY_MCP_PORT),
        "--local",
        "--var",
        f"API_URL:http://127.0.0.1:{SUPERMEMORY_PUBLIC_PORT}",
        "--var",
        "MCP_RESOURCE:https://mcp.supermemory.ai/mcp",
    ]
    process = subprocess.Popen(command, cwd=OFFICIAL_MCP_ROOT, env=os.environ, start_new_session=True)
    _wait_for_claude_port(SUPERMEMORY_MCP_PORT, process, timeout=120)
    return process


claude_service_hindsight_image = memory_services.claude_hindsight_image
claude_supermemory_image = (
    memory_services.supermemory_image
    .run_commands(
        "set -eu; "
        "curl --fail --silent --show-error --location https://deb.nodesource.com/setup_22.x "
        "| bash -; "
        "apt-get install --yes nodejs; "
        "node --version | grep '^v22\\.'",
    )
    .add_local_dir(
        str(OFFICIAL_MCP_SOURCE), str(OFFICIAL_MCP_ROOT), copy=True,
        ignore=["node_modules/**", ".wrangler/**", ".dev.vars"],
    )
    .run_commands(
        "set -eu; "
        "npm install --global bun@1.3.6 pnpm@9.15.9; "
        f"cd {OFFICIAL_MCP_ROOT}; "
        "pnpm install --frozen-lockfile; "
        "bun run build:widget; "
        "pnpm exec wrangler --version",
    )
)

if __name__ == "__main__":
    raise SystemExit(main())
