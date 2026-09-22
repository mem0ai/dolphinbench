"""Honcho service verification and profile finalization."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
import copy
from typing import Any


import yaml


VERIFICATION_VERSION = 2
EXPECTED_SOURCE_COUNT = 3400
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _content_sha256(contents: list[str]) -> str:
    canonical = json.dumps(contents, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def _profile_identity(profile: Path) -> dict[str, str]:
    data = _read_json(profile / "honcho.json", "honcho.json")
    required = ("workspace", "peerName", "aiPeer")
    missing = [key for key in required if not isinstance(data.get(key), str) or not data[key]]
    if missing:
        raise ValueError("honcho.json is missing identity field(s): " + ", ".join(missing))
    host = (data.get("hosts") or {}).get("hermes")
    if not isinstance(host, dict) or any(host.get(key) != data[key] for key in required):
        raise ValueError("honcho.json hosts.hermes does not match its workspace and peer identity")
    return {
        "workspace": data["workspace"],
        "peer_name": data["peerName"],
        "ai_peer": data["aiPeer"],
    }


def validate_tailscale_url(raw_url: str) -> str:
    """Accept a Tailscale DNS name or Tailscale IPv4 address, never loopback."""
    url = raw_url.strip().rstrip("/")
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError("Honcho service URL must be an explicit http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Honcho service URL must not contain credentials, a query, or a fragment")
    parts = hostname.split(".")
    tailscale_ipv4 = len(parts) == 4 and all(part.isdigit() for part in parts)
    if tailscale_ipv4:
        octets = [int(part) for part in parts]
        is_tailscale = octets[0] == 100 and 64 <= octets[1] <= 127 and all(0 <= value <= 255 for value in octets)
    else:
        is_tailscale = hostname.endswith(".ts.net")
    if not is_tailscale:
        raise ValueError("Honcho service URL must use this host's Tailscale address")
    return url


def _expected_source_data(seed_manifest: Path, profile: Path) -> tuple[set[str], list[str]]:
    manifest = _read_json(seed_manifest, "completed Honcho seed manifest")
    jobs = [
        job for job in manifest.get("jobs", [])
        if isinstance(job, dict)
        and job.get("configuration") == "honcho"
        and job.get("profile") == profile.name
    ]
    if len(jobs) != 1:
        raise ValueError("completed Honcho seed manifest must contain exactly one matching Honcho job")
    sim_path = jobs[0].get("sim_path")
    if not isinstance(sim_path, str) or not sim_path:
        raise ValueError("completed Honcho seed manifest has no simulation path")
    try:
        simulation = yaml.safe_load(Path(sim_path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read Honcho seed simulation: {sim_path}") from exc
    sessions = simulation.get("sessions") if isinstance(simulation, dict) else None
    if not isinstance(sessions, list):
        raise ValueError("Honcho seed simulation has no sessions")
    source_ids: set[str] = set()
    contents: list[str] = []
    for session in sessions:
        if not isinstance(session, dict) or not session.get("id"):
            raise ValueError("Honcho seed simulation has a session without an id")
        narrative_date = session.get("narrative_date")
        if not isinstance(narrative_date, str) or not narrative_date:
            raise ValueError("Honcho seed simulation has a session without a narrative date")
        messages = session.get("messages")
        if not isinstance(messages, list):
            raise ValueError("Honcho seed simulation has a session without messages")
        for index, message in enumerate(messages):
            if not isinstance(message, str):
                raise ValueError("Honcho seed simulation has a non-text message")
            source_id = f"{session['id']}:{index}"
            if source_id in source_ids:
                raise ValueError(f"Honcho seed simulation repeats source ID: {source_id}")
            source_ids.add(source_id)
            contents.append(f"[{narrative_date}] {message}")
    if len(source_ids) != EXPECTED_SOURCE_COUNT:
        raise ValueError(
            f"Honcho seed simulation must contain {EXPECTED_SOURCE_COUNT} source messages, found {len(source_ids)}"
        )
    return source_ids, contents


def _receipt_source_ids(receipt_path: Path, identity: dict[str, str]) -> set[str]:
    source_id_counts: dict[str, int] = {}
    try:
        lines = receipt_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read Honcho seed receipts: {receipt_path}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid Honcho receipt at line {line_number}") from exc
        if not isinstance(row, dict) or row.get("operation_kind") != "automatic_sync_turn":
            continue
        if row.get("provider") != "honcho" or row.get("receipt_type") != "memory_seed_operation":
            raise ValueError(f"Honcho receipt line {line_number} is not an automatic seed receipt")
        if row.get("peer") != identity["peer_name"] or row.get("assistant_peer") != identity["ai_peer"]:
            raise ValueError(f"Honcho receipt line {line_number} does not match honcho.json peer identity")
        if row.get("user_chunk_count") != 1 or row.get("assistant_chunk_count") != 1:
            raise ValueError(f"Honcho receipt line {line_number} does not represent one source message")
        source_id = row.get("seed_item_id")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError(f"Honcho receipt line {line_number} has no source ID")
        source_id_counts[source_id] = source_id_counts.get(source_id, 0) + 1
    duplicates = sorted(source_id for source_id, count in source_id_counts.items() if count > 1)
    if duplicates:
        raise ValueError("Honcho seed receipts contain duplicate source IDs: " + ", ".join(duplicates[:5]))
    return set(source_id_counts)


DEFAULT_DATABASE_CONTAINER = "honcho-database-1"


def _psql_command(database_container: str, workspace: str, peer_name: str) -> tuple[list[str], str]:
    query = """
WITH failed_queue AS (
  SELECT id, task_type, message_id, payload->>'session_name' AS session_name
  FROM queue
  WHERE workspace_name = :'workspace_name' AND error IS NOT NULL
), failure_status AS (
  SELECT failed.*,
    CASE
      WHEN failed.task_type = 'summary' THEN EXISTS (
        SELECT 1
        FROM sessions
        CROSS JOIN LATERAL jsonb_each(
          COALESCE(sessions.internal_metadata->'summaries', '{}'::jsonb)
        ) AS saved_summary
        WHERE sessions.workspace_name = :'workspace_name'
          AND sessions.name = failed.session_name
          AND NULLIF(saved_summary.value->>'message_id', '')::bigint >= failed.message_id
      )
      ELSE FALSE
    END AS superseded
  FROM failed_queue AS failed
)
SELECT json_build_object(
  'workspace_count', (SELECT count(*) FROM workspaces WHERE name = :'workspace_name'),
  'profile_peer_count', (SELECT count(*) FROM peers WHERE workspace_name = :'workspace_name' AND name = :'peer_name'),
  'profile_peer_message_count', (SELECT count(*) FROM messages WHERE workspace_name = :'workspace_name' AND peer_name = :'peer_name'),
  'profile_peer_distinct_message_count', (SELECT count(DISTINCT public_id) FROM messages WHERE workspace_name = :'workspace_name' AND peer_name = :'peer_name'),
  'profile_peer_contents', COALESCE((
    SELECT json_agg(messages.content ORDER BY messages.id)
    FROM messages
    WHERE workspace_name = :'workspace_name' AND peer_name = :'peer_name'
  ), '[]'::json),
  'pending_queue_item_count', (SELECT count(*) FROM queue WHERE workspace_name = :'workspace_name' AND NOT processed),
  'failed_queue_item_count', (SELECT count(*) FROM failure_status),
  'superseded_failed_queue_item_count', (SELECT count(*) FROM failure_status WHERE superseded),
  'unresolved_failed_queue_item_count', (SELECT count(*) FROM failure_status WHERE NOT superseded),
  'last_failed_queue_item_id', (SELECT id FROM failure_status ORDER BY id DESC LIMIT 1),
  'last_failed_task_type', (SELECT task_type FROM failure_status ORDER BY id DESC LIMIT 1)
);
"""
    return [
        "docker", "exec", "-i", database_container, "psql", "-U", "postgres",
        "--tuples-only", "--no-align", "--quiet",
        "--set", "ON_ERROR_STOP=1", "--set", f"workspace_name={workspace}",
        "--set", f"peer_name={peer_name}",
    ], query


def _read_database_evidence(
    database_container: str,
    identity: dict[str, str],
    *,
    runner: CommandRunner = subprocess.run,
) -> dict[str, Any]:
    if not database_container.strip():
        raise ValueError("a local Honcho database container name is required")
    if shutil.which("docker") is None:
        raise RuntimeError("docker is required for local Honcho verification")
    command, query = _psql_command(database_container, identity["workspace"], identity["peer_name"])
    try:
        result = runner(command, input=query, text=True, capture_output=True, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("local Honcho verification query failed") from exc
    try:
        value = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError("local Honcho verification query returned invalid JSON") from exc
    required = {
        "workspace_count", "profile_peer_count", "profile_peer_message_count",
        "profile_peer_distinct_message_count", "profile_peer_contents", "pending_queue_item_count",
        "failed_queue_item_count", "last_failed_queue_item_id", "last_failed_task_type",
        "superseded_failed_queue_item_count", "unresolved_failed_queue_item_count",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise RuntimeError("local Honcho verification query returned incomplete evidence")
    for key in required - {"last_failed_queue_item_id", "last_failed_task_type"}:
        if key == "profile_peer_contents":
            continue
        if not isinstance(value[key], int) or value[key] < 0:
            raise RuntimeError("local Honcho verification query returned invalid counts")
    if not isinstance(value["profile_peer_contents"], list) or not all(isinstance(content, str) for content in value["profile_peer_contents"]):
        raise RuntimeError("local Honcho verification query returned invalid message content")
    if value["last_failed_queue_item_id"] is not None and not isinstance(value["last_failed_queue_item_id"], int):
        raise RuntimeError("local Honcho verification query returned an invalid failure ID")
    if value["last_failed_task_type"] is not None and not isinstance(value["last_failed_task_type"], str):
        raise RuntimeError("local Honcho verification query returned an invalid failure task type")
    return value


def verify_honcho_service(
    *,
    profile: Path,
    seed_manifest: Path,
    seed_receipts: Path,
    service_url: str,
    database_container: str = DEFAULT_DATABASE_CONTAINER,
    output_path: Path,
    runner: CommandRunner = subprocess.run,
) -> dict[str, Any]:
    """Read local Honcho evidence and write one immutable verification report."""
    profile = profile.expanduser().resolve()
    seed_manifest = seed_manifest.expanduser().resolve()
    seed_receipts = seed_receipts.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    service_url = validate_tailscale_url(service_url)
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite verification report: {output_path}")
    identity = _profile_identity(profile)
    expected_ids, expected_contents = _expected_source_data(seed_manifest, profile)
    receipt_ids = _receipt_source_ids(seed_receipts, identity)
    if receipt_ids != expected_ids:
        missing = sorted(expected_ids - receipt_ids)
        extra = sorted(receipt_ids - expected_ids)
        raise ValueError(f"Honcho receipts do not match the final corpus: missing={missing[:3]} extra={extra[:3]}")
    evidence = _read_database_evidence(database_container, identity, runner=runner)
    if evidence["workspace_count"] != 1 or evidence["profile_peer_count"] != 1:
        raise ValueError("local Honcho database does not contain the exact workspace and peer from honcho.json")
    if evidence["profile_peer_message_count"] != EXPECTED_SOURCE_COUNT or evidence["profile_peer_distinct_message_count"] != EXPECTED_SOURCE_COUNT:
        raise ValueError("local Honcho database does not contain 3,400 distinct messages for the configured peer")
    expected_content_sha256 = _content_sha256(expected_contents)
    observed_content_sha256 = _content_sha256(evidence["profile_peer_contents"])
    if observed_content_sha256 != expected_content_sha256:
        raise ValueError("local Honcho database message content does not match the final simulation")
    del evidence["profile_peer_contents"]
    if evidence["pending_queue_item_count"] != 0:
        raise ValueError("local Honcho database still has pending processing work")
    failed = int(evidence["failed_queue_item_count"])
    last_failed_task_type = evidence["last_failed_task_type"]
    superseded_count = int(evidence["superseded_failed_queue_item_count"])
    unresolved_count = int(evidence["unresolved_failed_queue_item_count"])
    if superseded_count + unresolved_count != failed:
        raise ValueError("Honcho processing failure counts are inconsistent")
    superseded = unresolved_count == 0
    if not superseded:
        raise ValueError("historical Honcho processing failures are not proven superseded by later completed work")
    report = {
        "verification_version": VERIFICATION_VERSION,
        "kind": "honcho_local_read_only",
        "workspace": identity["workspace"],
        "peer_name": identity["peer_name"],
        "ai_peer": identity["ai_peer"],
        "service_endpoint": {
            "url": service_url,
            "evidence": "local read-only PostgreSQL query for the same workspace and peer; no HTTP request was made",
        },
        "seed_manifest": str(seed_manifest),
        "seed_manifest_sha256": _sha256(seed_manifest),
        "seed_receipts": str(seed_receipts),
        "seed_receipts_sha256": _sha256(seed_receipts),
        "source_receipts": {
            "expected_count": EXPECTED_SOURCE_COUNT,
            "unique_source_id_count": len(receipt_ids),
            "duplicate_source_id_count": 0,
            "matches_final_corpus": True,
        },
        "messages": {
            "peer_name": identity["peer_name"],
            "count": evidence["profile_peer_message_count"],
            "distinct_message_count": evidence["profile_peer_distinct_message_count"],
            "expected_content_sha256": expected_content_sha256,
            "observed_content_sha256": observed_content_sha256,
            "exact_content_match": True,
        },
        "processing": {
            "pending_queue_item_count": evidence["pending_queue_item_count"],
            "failed_queue_item_count": failed,
            "superseded_failed_queue_item_count": superseded_count,
            "unresolved_failed_queue_item_count": unresolved_count,
            "last_failed_queue_item_id": evidence["last_failed_queue_item_id"],
            "last_failed_task_type": last_failed_task_type,
            "historical_failures_superseded": superseded,
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--seed-manifest", type=Path, required=True)
    parser.add_argument("--seed-receipts", type=Path, required=True)
    parser.add_argument("--service-url", required=True)
    parser.add_argument("--database-container", default=DEFAULT_DATABASE_CONTAINER)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = verify_honcho_service(
        profile=args.profile,
        seed_manifest=args.seed_manifest,
        seed_receipts=args.seed_receipts,
        service_url=args.service_url,
        database_container=args.database_container,
        output_path=args.output,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


from reference import evaluate as matrix
from reference.memory import honcho as verifier
from reference.runtimes.hermes import profile_config_hashes


FINALIZATION_VERSION = 2
HONCHO_CREDENTIAL_KEYS = ("apiKey",)
HERMES_CREDENTIAL_FILES = (
    ".env",
    "auth.json",
    "credentials.json",
    "tokens.json",
)
IDENTITY_KEYS = ("workspace", "peerName", "aiPeer")


def _finalize_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): _finalize_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _finalize_profile_identity(honcho: dict[str, Any]) -> dict[str, str]:
    missing = [key for key in IDENTITY_KEYS if not isinstance(honcho.get(key), str) or not honcho[key]]
    if missing:
        raise ValueError("Honcho profile is missing identity field(s): " + ", ".join(missing))
    host = (honcho.get("hosts") or {}).get("hermes")
    if not isinstance(host, dict) or any(host.get(key) != honcho[key] for key in IDENTITY_KEYS):
        raise ValueError("Honcho profile hosts.hermes does not match its workspace and peer identity")
    return {"workspace": honcho["workspace"], "peer_name": honcho["peerName"], "ai_peer": honcho["aiPeer"]}


def _verify_local_service_report(
    path: Path,
    *,
    identity: dict[str, str],
    tailscale_url: str,
    seed_manifest: Path,
) -> dict[str, Any]:
    report = _read_json(path, "local Honcho verification report")
    if report.get("verification_version") != verifier.VERIFICATION_VERSION:
        raise ValueError("local Honcho verification report has an unsupported version")
    for key in ("workspace", "peer_name", "ai_peer"):
        if report.get(key) != identity[key]:
            raise ValueError(f"local Honcho verification report {key} does not match honcho.json")
    endpoint = report.get("service_endpoint")
    if not isinstance(endpoint, dict) or endpoint.get("url") != tailscale_url:
        raise ValueError("local Honcho verification report does not match the Tailscale service URL")
    if report.get("seed_manifest_sha256") != _finalize_sha256(seed_manifest):
        raise ValueError("local Honcho verification report belongs to a different seed manifest")
    receipts = report.get("source_receipts")
    messages = report.get("messages")
    processing = report.get("processing")
    if not isinstance(receipts, dict) or not isinstance(messages, dict) or not isinstance(processing, dict):
        raise ValueError("local Honcho verification report has incomplete evidence")
    expected = verifier.EXPECTED_SOURCE_COUNT
    if (
        receipts.get("expected_count") != expected
        or receipts.get("unique_source_id_count") != expected
        or receipts.get("duplicate_source_id_count") != 0
        or receipts.get("matches_final_corpus") is not True
    ):
        raise ValueError("local Honcho verification report does not prove all source receipts are unique and complete")
    if (
        messages.get("peer_name") != identity["peer_name"]
        or messages.get("count") != expected
        or messages.get("distinct_message_count") != expected
        or messages.get("exact_content_match") is not True
        or not isinstance(messages.get("expected_content_sha256"), str)
        or not isinstance(messages.get("observed_content_sha256"), str)
        or messages["expected_content_sha256"] != messages["observed_content_sha256"]
    ):
        raise ValueError("local Honcho verification report does not prove the seeded message content is complete")
    if processing.get("pending_queue_item_count") != 0:
        raise ValueError("local Honcho verification report shows pending processing work")
    if processing.get("failed_queue_item_count", 0) > 0:
        if (
            processing.get("historical_failures_superseded") is not True
            or processing.get("unresolved_failed_queue_item_count") != 0
            or processing.get("superseded_failed_queue_item_count")
            != processing.get("failed_queue_item_count")
            or not isinstance(processing.get("last_failed_task_type"), str)
            or not processing["last_failed_task_type"]
        ):
            raise ValueError("local Honcho verification report does not prove historical failures were superseded by the same task type")
    return report


def _validate_seed_artifacts(
    manifest_path: Path,
    state_path: Path,
    source_profile: Path,
    persona: str,
    identity: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = _read_json(manifest_path, "completed Honcho seed manifest")
    state = _read_json(state_path, "completed Honcho seed state")
    if manifest.get("manifest_version") != matrix.MANIFEST_VERSION:
        raise ValueError("completed Honcho seed manifest has an unsupported version")
    if manifest.get("phase") != "prepared_offline":
        raise ValueError("completed Honcho seed manifest is not a prepared offline manifest")
    if state.get("manifest") != str(manifest_path.resolve()):
        raise ValueError("completed Honcho seed state points to a different manifest")
    if not state.get("phases"):
        raise ValueError("completed Honcho seed state has no recorded phases")
    job_id = f"honcho-{persona}"
    jobs = [job for job in manifest.get("jobs", []) if isinstance(job, dict) and job.get("id") == job_id]
    if len(jobs) != 1:
        raise ValueError(f"completed Honcho seed manifest must contain exactly one job {job_id}")
    job = jobs[0]
    if job.get("profile") != source_profile.name:
        raise ValueError("completed Honcho seed manifest does not reference the source profile")
    if job.get("agent_runtime", "hermes") != "hermes" or job.get("configuration") != "honcho":
        raise ValueError("completed Honcho seed job is not the Hermes Honcho job")
    provider_identity = job.get("provider_identity")
    expected_identity = {
        "kind": "honcho",
        **identity,
    }
    if provider_identity != expected_identity:
        raise ValueError("completed Honcho seed job identity does not match honcho.json")
    state_profiles = state.get("profiles") or {}
    if job_id not in state_profiles:
        raise ValueError("completed Honcho seed state has no profile configuration hashes")
    if not matrix._profile_hashes_match(
        state_profiles[job_id], profile_config_hashes(source_profile)
    ):
        raise ValueError("source Honcho profile changed after completed ingestion")
    if not matrix._seed_is_complete_for_job(state, job):
        raise ValueError("completed Honcho seed job does not contain all ingested sessions")
    return manifest, state, expected_identity


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def finalize_honcho_profile(
    *,
    source_profile: Path,
    destination_profile: Path,
    tailscale_url: str,
    verification_report: Path,
    seed_manifest: Path,
    seed_state: Path,
    output_dir: Path,
    persona: str,
) -> dict[str, Any]:
    """Clone a completed profile and point only its Honcho URL at Tailscale."""

    source_profile = source_profile.expanduser().resolve()
    destination_profile = destination_profile.expanduser().resolve()
    verification_report = verification_report.expanduser().resolve()
    seed_manifest = seed_manifest.expanduser().resolve()
    seed_state = seed_state.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    tailscale_url = verifier.validate_tailscale_url(tailscale_url)
    if not source_profile.is_dir():
        raise ValueError(f"source Hermes profile does not exist: {source_profile}")
    source_honcho_path = source_profile / "honcho.json"
    if not source_honcho_path.is_file():
        raise ValueError(f"source Hermes profile has no honcho.json: {source_honcho_path}")
    for path, label in (
        (verification_report, "local Honcho verification report"),
        (seed_manifest, "completed Honcho seed manifest"),
        (seed_state, "completed Honcho seed state"),
    ):
        if not path.is_file():
            raise ValueError(f"{label} does not exist: {path}")
    if destination_profile == source_profile:
        raise ValueError("destination profile must differ from source profile")
    if _is_within(destination_profile, source_profile):
        raise ValueError("destination profile cannot be inside the source profile")
    if destination_profile.exists():
        raise FileExistsError(f"refusing to overwrite destination profile: {destination_profile}")
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite finalization output: {output_dir}")
    if _is_within(output_dir, source_profile) or _is_within(output_dir, destination_profile):
        raise ValueError("finalization output cannot be inside a profile")

    original_source_hashes = _tree_hashes(source_profile)
    source_honcho = _read_json(source_honcho_path, "source honcho.json")
    identity = _finalize_profile_identity(source_honcho)
    manifest, state, provider_identity = _validate_seed_artifacts(
        seed_manifest, seed_state, source_profile, persona, identity,
    )
    report = _verify_local_service_report(
        verification_report,
        identity=identity,
        tailscale_url=tailscale_url,
        seed_manifest=seed_manifest,
    )

    removed_credentials: list[str] = []
    try:
        destination_profile.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_profile, destination_profile)
        for filename in HERMES_CREDENTIAL_FILES:
            credential_path = destination_profile / filename
            if credential_path.exists():
                _remove_path(credential_path)
                removed_credentials.append(filename)

        finalized_honcho = copy.deepcopy(source_honcho)
        for key in HONCHO_CREDENTIAL_KEYS:
            if key in finalized_honcho:
                finalized_honcho.pop(key)
                removed_credentials.append(f"honcho.json:{key}")
        finalized_honcho["baseUrl"] = tailscale_url
        _write_json(destination_profile / "honcho.json", finalized_honcho)

        if _tree_hashes(source_profile) != original_source_hashes:
            raise RuntimeError("source Hermes profile changed while it was being copied")
        source_non_credentials = {
            name: digest
            for name, digest in original_source_hashes.items()
            if name not in HERMES_CREDENTIAL_FILES and name != "honcho.json"
        }
        destination_non_credentials = {
            name: digest
            for name, digest in _tree_hashes(destination_profile).items()
            if name not in HERMES_CREDENTIAL_FILES and name != "honcho.json"
        }
        if source_non_credentials != destination_non_credentials:
            raise RuntimeError("profile clone changed a non-Honcho, non-credential file")

        updated_hashes = profile_config_hashes(destination_profile)
        output_dir.mkdir(parents=True, exist_ok=False)
        manifest_path = output_dir / "launch_manifest.json"
        state_path = output_dir / "launch_state.json"
        record_path = output_dir / "completed_seed_record.json"
        finalization_provenance = {
            "version": FINALIZATION_VERSION,
            "local_verification_report": str(verification_report),
            "local_verification_report_sha256": _finalize_sha256(verification_report),
            "tailscale_url": tailscale_url,
            "source_profile": str(source_profile),
            "finalized_profile": str(destination_profile),
            "removed_credentials": sorted(set(removed_credentials)),
        }

        derived_manifest = copy.deepcopy(manifest)
        derived_manifest["honcho_profile_finalization"] = finalization_provenance
        derived_job = next(job for job in derived_manifest["jobs"] if job.get("id") == f"honcho-{persona}")
        derived_job["profile"] = destination_profile.name
        derived_state = copy.deepcopy(state)
        derived_state["manifest"] = str(manifest_path)
        derived_state.setdefault("profiles", {})[f"honcho-{persona}"] = updated_hashes
        derived_state["honcho_profile_finalization"] = finalization_provenance
        _write_json(manifest_path, derived_manifest)
        _write_json(state_path, derived_state)

        completed_seed = {
            "manifest": str(manifest_path),
            "state": str(state_path),
            "manifest_sha256": _finalize_sha256(manifest_path),
            "state_sha256": _finalize_sha256(state_path),
            "provider_identity": provider_identity,
            "profile": destination_profile.name,
            "profile_path": str(destination_profile),
            "profile_config_hashes": updated_hashes,
            "local_verification_report": str(verification_report),
            "local_verification_report_sha256": _finalize_sha256(verification_report),
        }
        _write_json(record_path, {"honcho": completed_seed})
        return {
            "finalization_version": FINALIZATION_VERSION,
            "persona": persona,
            "source_profile": str(source_profile),
            "finalized_profile": str(destination_profile),
            "profile_config_hashes": updated_hashes,
            "removed_credentials": sorted(set(removed_credentials)),
            "local_verification_report": {
                "path": str(verification_report),
                "sha256": _finalize_sha256(verification_report),
                "workspace": report["workspace"],
                "service_url": report["service_endpoint"]["url"],
                "source_receipts": report["source_receipts"],
                "messages": report["messages"],
                "processing": report["processing"],
            },
            "completed_seeds": {"honcho": completed_seed},
            "artifacts": {
                "manifest": str(manifest_path),
                "state": str(state_path),
                "completed_seed_record": str(record_path),
            },
        }
    except Exception:
        shutil.rmtree(destination_profile, ignore_errors=True)
        shutil.rmtree(output_dir, ignore_errors=True)
        raise


def _finalize_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-profile", type=Path, required=True)
    parser.add_argument("--destination-profile", type=Path, required=True)
    parser.add_argument("--tailscale-url", required=True)
    parser.add_argument("--verification-report", type=Path, required=True)
    parser.add_argument("--seed-manifest", type=Path, required=True)
    parser.add_argument("--seed-state", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--persona", required=True)
    return parser


def finalize_main(argv: list[str] | None = None) -> int:
    args = _finalize_parser().parse_args(argv)
    result = finalize_honcho_profile(
        source_profile=args.source_profile,
        destination_profile=args.destination_profile,
        tailscale_url=args.tailscale_url,
        verification_report=args.verification_report,
        seed_manifest=args.seed_manifest,
        seed_state=args.seed_state,
        output_dir=args.output_dir,
        persona=args.persona,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
