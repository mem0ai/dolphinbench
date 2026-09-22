"""Claude history execution, checkpoints, and database verification."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence
import gzip
import subprocess
import traceback
from pathlib import Path
from typing import Any, Callable
import urllib.request
from typing import Any


class RecoveryStoreError(RuntimeError):
    """Raised when a recovery bundle cannot be safely saved or restored."""


class _Mem0PendingRace(RecoveryStoreError):
    """A pending Mem0 packet changed while a source archive was being read."""


class RecoveryStore:
    """Publish and restore complete, immutable ingestion recovery generations."""

    _GENERATIONS = "generations"
    _CURRENT = "CURRENT"
    _MANIFEST = "manifest.json"
    _HOME_ARCHIVE = "home.tar.gz"
    _CONTROLLER_ARCHIVE = "controller.tar.gz"
    _DATABASE_ARCHIVE = "database.archive"
    _MEM0_TELEMETRY_DIRECTORY = PurePosixPath(".claude/plugins/data/mem0-inline")
    _MEM0_PENDING_DIRECTORY = _MEM0_TELEMETRY_DIRECTORY / "pending"
    _MEM0_DATABASE = _MEM0_TELEMETRY_DIRECTORY / "evidence.sqlite3"
    _MAX_ARCHIVE_ATTEMPTS = 3

    def __init__(self, root: Path, commit: Callable[[], None], *, compression_level: int = 9) -> None:
        if type(compression_level) is not int or not 0 <= compression_level <= 9:
            raise ValueError("Compression level must be an integer from 0 through 9")
        self.root = Path(root)
        self.commit = commit
        self.compression_level = compression_level

    def save(
        self,
        home: Path,
        controller: Path,
        database_archive: Path | None,
        secrets: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Save one complete generation, then publish it only after a commit."""
        home = Path(home)
        controller = Path(controller)
        database_archive = Path(database_archive) if database_archive is not None else None
        self._validate_save_inputs(home, controller, database_archive, secrets)

        self.root.mkdir(parents=True, exist_ok=True)
        generations = self.root / self._GENERATIONS
        generations.mkdir(exist_ok=True)
        current = self.root / self._CURRENT
        previous_current = current.read_bytes() if current.exists() else None
        previous_name = self._pointer_name(previous_current) if previous_current is not None else None
        generation_name = uuid.uuid4().hex
        generation = generations / generation_name
        generation.mkdir()
        pointer_written = False
        try:
            self._write_generation_archives(home, controller, generation, secrets)
            if database_archive is not None:
                shutil.copyfile(database_archive, generation / self._DATABASE_ARCHIVE)

            manifest = self._manifest(generation, generation_name, database_archive is not None)
            self._atomic_json(generation / self._MANIFEST, manifest)
            self.commit()
            self._atomic_text(current, generation_name + "\n")
            pointer_written = True
            self.commit()
            try:
                self._prune_generations(generation_name, previous_name)
                self.commit()
            except Exception:
                # The published generation remains valid if best-effort cleanup fails.
                pass
            return manifest
        except Exception:
            if pointer_written:
                if previous_current is None:
                    current.unlink(missing_ok=True)
                else:
                    self._atomic_bytes(current, previous_current)
            # This restores the local pointer only. A failed remote commit is ambiguous.
            raise

    def _write_generation_archives(
        self, home: Path, controller: Path, generation: Path, secrets: Sequence[str],
    ) -> None:
        for attempt in range(self._MAX_ARCHIVE_ATTEMPTS):
            try:
                # Re-capture both inputs so a retry is a complete generation, not a
                # mixture of archives read before and after the pending-file race.
                self._write_archive(home, generation / self._HOME_ARCHIVE, secrets)
                self._write_archive(controller, generation / self._CONTROLLER_ARCHIVE, secrets)
                return
            except _Mem0PendingRace as exc:
                if attempt + 1 == self._MAX_ARCHIVE_ATTEMPTS:
                    raise RecoveryStoreError(
                        "Mem0 pending file changed during archive capture after "
                        f"{self._MAX_ARCHIVE_ATTEMPTS} attempts"
                    ) from exc

    def restore(
        self,
        home: Path,
        controller: Path,
        database_archive: Path | None,
    ) -> dict[str, Any] | None:
        """Validate a complete generation before replacing any caller targets."""
        home = Path(home)
        controller = Path(controller)
        database_archive = Path(database_archive) if database_archive is not None else None
        self._validate_restore_targets(home, controller, database_archive)
        generation = self._current_generation()
        if generation is None:
            return None
        manifest = self._validate_generation(generation)

        # All untrusted archive contents are validated before a target is created.
        home_members = self._validated_members(generation / self._HOME_ARCHIVE)
        controller_members = self._validated_members(generation / self._CONTROLLER_ARCHIVE)
        if bool(manifest["database_present"]) != (database_archive is not None):
            raise RecoveryStoreError("database archive presence does not match the recovery generation")

        with tempfile.TemporaryDirectory(dir=self.root, prefix=".restore-") as raw_stage:
            stage = Path(raw_stage)
            staged_home = stage / "home"
            staged_controller = stage / "controller"
            self._extract_members(generation / self._HOME_ARCHIVE, home_members, staged_home)
            self._extract_members(generation / self._CONTROLLER_ARCHIVE, controller_members, staged_controller)
            staged_database = stage / self._DATABASE_ARCHIVE
            if database_archive is not None:
                shutil.copyfile(generation / self._DATABASE_ARCHIVE, staged_database)

            # The saved files can be on a Modal Volume while the targets are on
            # container-local disk. Copy across those filesystems after validation.
            home.parent.mkdir(parents=True, exist_ok=True)
            controller.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staged_home, home, symlinks=True, dirs_exist_ok=True)
            shutil.copytree(staged_controller, controller, symlinks=True, dirs_exist_ok=True)
            if database_archive is not None:
                database_archive.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(staged_database, database_archive)
        return manifest

    def _validate_save_inputs(
        self, home: Path, controller: Path, database_archive: Path | None, secrets: Sequence[str],
    ) -> None:
        if not home.is_dir() or not controller.is_dir():
            raise RecoveryStoreError("home and controller must be existing directories")
        self._require_separate(home, controller, database_archive)
        if database_archive is not None and not database_archive.is_file():
            raise RecoveryStoreError("database archive must be an existing regular file")
        if any(not isinstance(secret, str) or not secret for secret in secrets):
            raise RecoveryStoreError("secrets must be non-empty strings")
        self._validate_source_links(home)
        self._validate_source_links(controller)
        if database_archive is not None and database_archive.is_symlink():
            raise RecoveryStoreError("database archive must not be a symlink")

    def _validate_restore_targets(
        self, home: Path, controller: Path, database_archive: Path | None,
    ) -> None:
        self._require_separate(home, controller, database_archive)
        for target in (home, controller):
            if target.exists() and (not target.is_dir() or any(target.iterdir())):
                raise RecoveryStoreError("restore targets must be empty directories or absent")
        if database_archive is not None and database_archive.exists():
            raise RecoveryStoreError("database restore target must be absent")

    def _require_separate(self, home: Path, controller: Path, database_archive: Path | None) -> None:
        paths = [self.root, home, controller] + ([database_archive] if database_archive is not None else [])
        resolved = [path.resolve(strict=False) for path in paths]
        for index, left in enumerate(resolved):
            for right in resolved[index + 1:]:
                if left == right or left in right.parents or right in left.parents:
                    raise RecoveryStoreError("recovery root, inputs, and targets must be separate paths")

    @staticmethod
    def _validate_source_links(source: Path) -> None:
        source_root = source.resolve()
        for attempt in range(RecoveryStore._MAX_ARCHIVE_ATTEMPTS):
            try:
                for item in source.rglob("*"):
                    if RecoveryStore._is_mem0_sqlite_sidecar(item.relative_to(source)):
                        continue
                    if item.is_symlink() and item.relative_to(source).as_posix() == ".supermemory-claude/statusline-current":
                        continue
                    if item.is_symlink():
                        try:
                            target = item.resolve(strict=True)
                        except OSError as exc:
                            if RecoveryStore._is_mem0_pending_path(source, item):
                                raise _Mem0PendingRace(str(item)) from exc
                            raise RecoveryStoreError(f"refusing broken symlink: {item}") from exc
                        if target != source_root and source_root not in target.parents:
                            raise RecoveryStoreError(f"refusing external symlink: {item}")
                    if not item.is_dir() and not item.is_file():
                        if RecoveryStore._is_mem0_pending_path(source, item):
                            raise _Mem0PendingRace(str(item))
                        raise RecoveryStoreError(f"refusing to archive non-regular path: {item}")
                return
            except _Mem0PendingRace as exc:
                if attempt + 1 == RecoveryStore._MAX_ARCHIVE_ATTEMPTS:
                    raise RecoveryStoreError(
                        "Mem0 pending file changed during source validation after "
                        f"{RecoveryStore._MAX_ARCHIVE_ATTEMPTS} attempts"
                    ) from exc

    def _write_archive(self, source: Path, destination: Path, secrets: Sequence[str]) -> None:
        secret_bytes = tuple(secret.encode() for secret in secrets)
        with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT,
                          compresslevel=self.compression_level) as archive:
            for item in sorted(source.rglob("*"), key=lambda path: path.relative_to(source).as_posix()):
                relative = item.relative_to(source).as_posix()
                if item.is_symlink() and relative == ".supermemory-claude/statusline-current":
                    continue
                if self._is_transient_mem0_telemetry(PurePosixPath(relative)):
                    continue
                if self._is_mem0_sqlite_sidecar(PurePosixPath(relative)):
                    continue
                try:
                    info = archive.gettarinfo(str(item), arcname=relative)
                    if item.is_symlink():
                        info.linkname = self._archive_linkname(source, item)
                        archive.addfile(info)
                        continue
                    if item.is_dir():
                        archive.addfile(info)
                        continue
                    data = (self._sqlite_snapshot(item) if PurePosixPath(relative) == self._MEM0_DATABASE
                            else item.read_bytes())
                    for secret in secret_bytes:
                        data = data.replace(secret, b"")
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
                except FileNotFoundError as exc:
                    if self._is_mem0_pending_path(source, item):
                        raise _Mem0PendingRace(str(item)) from exc
                    raise

    @classmethod
    def _is_mem0_sqlite_sidecar(cls, path: PurePosixPath) -> bool:
        return path.parent == cls._MEM0_DATABASE.parent and path.name in {
            "evidence.sqlite3-wal", "evidence.sqlite3-shm", "evidence.sqlite3-journal",
        }

    @staticmethod
    def _sqlite_snapshot(path: Path) -> bytes:
        # Backup includes committed WAL data without racing SQLite's temporary files.
        deadline = time.monotonic() + 30

        def progress(status: int, remaining: int, total: int) -> None:
            if time.monotonic() > deadline:
                raise RecoveryStoreError("Mem0 SQLite backup exceeded 30 seconds")

        with tempfile.TemporaryDirectory(prefix="mem0-sqlite-backup-") as raw:
            snapshot = Path(raw) / "evidence.sqlite3"
            source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
            destination = sqlite3.connect(snapshot)
            try:
                source.backup(destination, pages=256, progress=progress, sleep=0.05)
                if destination.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise RecoveryStoreError("Mem0 SQLite backup failed integrity verification")
            finally:
                destination.close()
                source.close()
            return snapshot.read_bytes()

    @classmethod
    def _is_transient_mem0_telemetry(cls, path: PurePosixPath) -> bool:
        """Exclude only the Mem0 telemetry spool claimed by its detached flusher."""
        return path.parent == cls._MEM0_TELEMETRY_DIRECTORY and (
            path.name == "telemetry.jsonl"
            or (path.name.startswith("telemetry-") and path.name.endswith(".sending"))
        )

    @classmethod
    def _is_mem0_pending_path(cls, source: Path, item: Path) -> bool:
        try:
            relative = item.relative_to(source)
        except ValueError:
            return False
        pending_parts = cls._MEM0_PENDING_DIRECTORY.parts
        return relative.parts[:len(pending_parts)] == pending_parts

    @staticmethod
    def _archive_linkname(source: Path, item: Path) -> str:
        linkname = os.readlink(item)
        if not os.path.isabs(linkname):
            return linkname
        target = item.resolve(strict=True)
        return Path(os.path.relpath(target, start=item.parent.resolve())).as_posix()

    def _manifest(self, generation: Path, generation_name: str, database_present: bool) -> dict[str, Any]:
        names = [self._HOME_ARCHIVE, self._CONTROLLER_ARCHIVE]
        if database_present:
            names.append(self._DATABASE_ARCHIVE)
        return {
            "generation": generation_name,
            "database_present": database_present,
            "files": {name: self._sha256(generation / name) for name in names},
        }

    def _current_generation(self) -> Path | None:
        pointer = self.root / self._CURRENT
        if not pointer.exists():
            return None
        try:
            name = pointer.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise RecoveryStoreError("cannot read CURRENT pointer") from exc
        if self._pointer_name(name.encode("ascii")) is None:
            raise RecoveryStoreError("CURRENT pointer is invalid")
        generation = self.root / self._GENERATIONS / name
        if not generation.is_dir():
            raise RecoveryStoreError("CURRENT points to a missing generation")
        return generation

    @staticmethod
    def _pointer_name(value: bytes) -> str | None:
        try:
            name = value.decode("ascii").strip()
        except UnicodeDecodeError:
            return None
        return name if name.isalnum() and len(name) == 32 else None

    def _prune_generations(self, current: str, previous: str | None) -> None:
        keep = {current}
        if previous is not None:
            keep.add(previous)
        generations = self.root / self._GENERATIONS
        for candidate in generations.iterdir():
            if candidate.is_dir() and candidate.name not in keep:
                shutil.rmtree(candidate)

    def _validate_generation(self, generation: Path) -> dict[str, Any]:
        manifest_path = generation / self._MANIFEST
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryStoreError("recovery generation has no valid manifest") from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("database_present"), bool):
            raise RecoveryStoreError("recovery manifest is invalid")
        expected = {self._HOME_ARCHIVE, self._CONTROLLER_ARCHIVE}
        if manifest["database_present"]:
            expected.add(self._DATABASE_ARCHIVE)
        files = manifest.get("files")
        if not isinstance(files, dict) or set(files) != expected:
            raise RecoveryStoreError("recovery manifest file list is invalid")
        if set(path.name for path in generation.iterdir()) != expected | {self._MANIFEST}:
            raise RecoveryStoreError("recovery generation contains unexpected or missing files")
        for name, expected_hash in files.items():
            if not isinstance(expected_hash, str) or len(expected_hash) != 64:
                raise RecoveryStoreError("recovery manifest hash is invalid")
            if self._sha256(generation / name) != expected_hash:
                raise RecoveryStoreError(f"recovery generation hash mismatch for {name}")
        return manifest

    @staticmethod
    def _validated_members(archive_path: Path) -> list[tarfile.TarInfo]:
        try:
            with tarfile.open(archive_path, "r:gz") as archive:
                members = archive.getmembers()
        except (OSError, tarfile.TarError) as exc:
            raise RecoveryStoreError(f"invalid recovery archive: {archive_path.name}") from exc
        seen: set[str] = set()
        for member in members:
            path = PurePosixPath(member.name)
            canonical_name = path.as_posix()
            if (
                not member.name
                or path.is_absolute()
                or ".." in path.parts
                or member.name != canonical_name
                or canonical_name in seen
                or not (member.isdir() or member.isreg() or member.issym())
                or member.islnk()
            ):
                raise RecoveryStoreError("recovery archive contains an unsafe path")
            seen.add(canonical_name)
        names = seen | {"."}
        for member in members:
            if not member.issym():
                continue
            target = RecoveryStore._relative_link_target(member.name, member.linkname)
            if target not in names:
                raise RecoveryStoreError("recovery archive contains an unsafe symlink")
        return members

    @staticmethod
    def _relative_link_target(name: str, linkname: str) -> str:
        target = PurePosixPath(linkname)
        if target.is_absolute():
            raise RecoveryStoreError("recovery archive contains an unsafe symlink")
        parts: list[str] = []
        for part in [*PurePosixPath(name).parent.parts, *target.parts]:
            if part in {"", "."}:
                continue
            if part == "..":
                if not parts:
                    raise RecoveryStoreError("recovery archive contains an unsafe symlink")
                parts.pop()
            else:
                parts.append(part)
        return "/".join(parts) or "."

    @staticmethod
    def _extract_members(archive_path: Path, members: list[tarfile.TarInfo], destination: Path) -> None:
        destination.mkdir()
        try:
            with tarfile.open(archive_path, "r:gz") as archive:
                directories = [member for member in members if member.isdir()]
                regular_files = [member for member in members if member.isreg()]
                links = [member for member in members if member.issym()]
                for member in directories:
                    output = destination.joinpath(*PurePosixPath(member.name).parts)
                    output.mkdir(parents=True, exist_ok=True)
                for member in regular_files:
                    output = destination.joinpath(*PurePosixPath(member.name).parts)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise RecoveryStoreError("recovery archive member cannot be read")
                    with extracted, output.open("wb") as handle:
                        shutil.copyfileobj(extracted, handle)
                    os.chmod(output, member.mode & 0o777)
                for member in directories:
                    output = destination.joinpath(*PurePosixPath(member.name).parts)
                    os.chmod(output, member.mode & 0o777)
                for member in links:
                    output = destination.joinpath(*PurePosixPath(member.name).parts)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(member.linkname, output)
        except (OSError, tarfile.TarError) as exc:
            raise RecoveryStoreError(f"cannot extract recovery archive: {archive_path.name}") from exc

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _atomic_text(path: Path, value: str) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="ascii", dir=path.parent, delete=False) as handle:
            handle.write(value)
            temporary = Path(handle.name)
        os.replace(temporary, path)

    @staticmethod
    def _atomic_bytes(path: Path, value: bytes) -> None:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
            handle.write(value)
            temporary = Path(handle.name)
        os.replace(temporary, path)

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, path)


from reference.runtimes import claude_code as loop
from reference.runtimes.claude_history import RecoveryStore


def journal_supermemory_turn(*, config: loop.PluginIngestionConfig, volume_root: Path,
                             commit: Callable[[], None], secrets: tuple[str, ...],
                             index: int, source: Any, result: Any) -> None:
    """Commit a completed Claude answer before waiting on its local memory write."""
    payload = loop._trace_payload(result)
    session = payload.get("session_id")
    transcripts = list((config.claude_config_dir / "projects").rglob(str(session) + ".jsonl"))
    if not isinstance(session, str) or not session or len(transcripts) != 1:
        raise RuntimeError("Cannot journal the completed Supermemory Claude session")
    transcript = transcripts[0].read_bytes()
    encoded_payload = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    if any(secret.encode() in transcript or secret in encoded_payload for secret in secrets):
        raise RuntimeError("Refusing to journal a Claude turn containing a credential")
    digest = hashlib.sha256(source.source_id.encode()).hexdigest()[:16]
    target = volume_root / "turn-journal" / f"{index:05d}-{digest}"
    record_path = target / "record.json"
    transcript_path = target / "transcript.jsonl.gz"
    record = {
        "schema_version": 1,
        "source_id": source.source_id,
        "content_sha256": source.content_sha256,
        "session_id": session,
        "transcript_sha256": hashlib.sha256(transcript).hexdigest(),
        "result": payload,
    }
    if record_path.exists() or transcript_path.exists():
        if (not record_path.exists() or not transcript_path.exists()
                or json.loads(record_path.read_text()) != record
                or gzip.decompress(transcript_path.read_bytes()) != transcript):
            raise RuntimeError("Existing Supermemory turn journal differs from this Claude turn")
        return
    target.mkdir(parents=True)
    temporary = target / ".transcript.jsonl.gz.tmp"
    try:
        with gzip.open(temporary, "wb", compresslevel=6) as stream:
            stream.write(transcript)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, transcript_path)
        loop._atomic_json(record_path, record)
        commit()
    finally:
        temporary.unlink(missing_ok=True)


def make_worker(*, provider: str, volume_name: str, run_name: str, corpus_path: Path,
                plugin_source: Path, controller: Path, volume_root: Path, honcho_url: str) -> Callable:
    """Serialize workers from a module that exists in the remote image."""
    def worker(plan: dict, confirm_paid_calls: bool = False, recovery: dict | None = None,
               verify_only: bool = False) -> dict:
        import modal
        if not confirm_paid_calls and not verify_only:
            raise RuntimeError("Full ingestion requires explicit approval")
        if plan["run_name"] != run_name:
            raise RuntimeError("This worker's persistent storage belongs to another run")
        volume = modal.Volume.from_name(volume_name)
        return run_provider(provider=provider, plan=plan, commit=volume.commit, recovery=recovery,
                            corpus_path=corpus_path, plugin_source=plugin_source, controller=controller,
                            volume_root=volume_root, honcho_url=honcho_url, verify_only=verify_only)
    return worker


def run_provider(*, provider: str, plan: dict, commit: Any, corpus_path: Path,
                 plugin_source: Path, controller: Path, volume_root: Path,
                 honcho_url: str, recovery: dict | None = None, verify_only: bool = False) -> dict:
    """Worker entrypoint kept separate from Modal's local image definitions."""
    from reference.runtimes.claude_code import load_morgan_source_messages
    from reference.runtimes.claude_code import TOKEN_KEY
    from reference.plan import source_hash

    job = next(job for job in plan["conditions"] if job["provider"] == provider)
    token = os.environ.get(TOKEN_KEY)
    if not token:
        raise RuntimeError(f"Selected Claude account credential is missing: {TOKEN_KEY}")
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
    messages, corpus_hash = load_morgan_source_messages(corpus_path)
    if corpus_hash != plan["corpus_sha256"] or source_hash(plugin_source) != job["plugin_files_sha256"]:
        raise RuntimeError("The uploaded history or plugin differs from the approved plan")
    root = Path(__file__).resolve().parents[2]
    for name, expected in plan["runner_files_sha256"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"The uploaded runner differs from the approved plan: {name}")
    version = subprocess.run(["/usr/local/bin/claude", "--version"], capture_output=True, text=True, check=True)
    if not version.stdout.startswith(job["claude_version"]):
        raise RuntimeError("The Claude binary differs from the approved plan")
    if verify_only:
        auth_result = subprocess.run(["/usr/local/bin/claude", "auth", "status"],
                                     capture_output=True, text=True, timeout=30, check=True)
        auth_value = json.loads(auth_result.stdout)
        auth = {key: auth_value.get(key) for key in ("loggedIn", "authMethod", "apiProvider")}
        if auth != {"loggedIn": True, "authMethod": "oauth_token", "apiProvider": "firstParty"}:
            raise RuntimeError("Claude recovery image has the wrong OAuth authentication mode")
        return {"status": "verified", "provider": provider, "claude_version": version.stdout.strip(),
                "runner_files_verified": len(plan["runner_files_sha256"]), "messages": len(messages),
                "auth": auth}
    environment = dict(os.environ)
    settings = {"namespace": job["namespace"]}
    if provider == "mem0":
        settings.update(api_url="https://api.mem0.ai", api_key=environment["MEM0_API_KEY"])
    elif provider == "honcho":
        settings.update(api_url=honcho_url, api_key=environment.get("HONCHO_API_KEY") or "self-hosted")
    else:
        url = "http://127.0.0.1:8888" if provider == "hindsight" else "http://127.0.0.1:8767"
        settings.update(api_url=url, api_key=environment[provider.upper() + "_API_KEY"])
        if provider == "supermemory":
            settings["mcp_url"] = url + "/mcp"
            if job.get("capture_session_end"):
                settings["capture_session_end"] = True
                for key in ("retrieval_probe_prompt", "retrieval_probe_terms"):
                    if key in job:
                        settings[key] = job[key]
    volume_root.chmod(0o700)
    config = loop.PluginIngestionConfig(
        provider=provider, model=job["model"], home=Path(job["home"]), project=Path(job["project"]),
        claude_config_dir=Path(job["config_dir"]), plugin_source=plugin_source,
        plugin_identity={"sha256": job["plugin_files_sha256"], "claude_version": job["claude_version"],
                         "hindsight_named_tool_fix": provider == "hindsight"},
        plugin_settings=settings, claude_command="/usr/local/bin/claude", environment=environment,
        run_as_user=job["uid"], run_as_group=job["uid"],
    )
    if settings.get("capture_session_end"):
        config.plugin_identity["capture_scheduling"] = "synchronous_session_end_v1"
    return run_history(config=config, messages=messages, controller=controller,
                       volume_root=volume_root, commit=commit, recovery=recovery,
                       run_budget_seconds=(recovery or {}).get("run_budget_seconds", 22 * 3600))


class LocalMemoryService:
    """Own only the new service inside this condition's container."""

    def __init__(self, provider: str):
        self.provider = provider
        self.process = None
        self.mcp = None
        self.proxy = None

    def _admin(self, *args: str) -> None:
        from reference.memory import services as services
        environment = services._hindsight_environment()
        environment.update(HINDSIGHT_API_DATABASE_URL="pg0", HINDSIGHT_API_DATABASE_SCHEMA="public")
        subprocess.run(["hindsight-admin", *args], env=environment, user=1000, group=1000, check=True)

    def start(self, saved_database: Path | None) -> None:
        from reference.memory import services as services
        from reference.memory import services as wiring
        if self.provider == "hindsight":
            root = services.HINDSIGHT_RUNTIME_ROOT
            root.mkdir(parents=True, exist_ok=True)
            os.chown(root, 1000, 1000)
            if saved_database is not None:
                restore = Path("/tmp/claude-hindsight-restore.zip")
                shutil.copyfile(saved_database, restore)
                restore.chmod(0o644)
                self._admin("run-db-migration", "--schema", "public")
                self._admin("restore", str(restore), "--schema", "public", "--yes")
                restore.unlink()
                subprocess.run(["/app/api/.venv/bin/python", "-m", "reference.memory.hindsight"],
                               env=services._hindsight_environment(), user=1000, group=1000, check=True)
            environment = services._hindsight_environment()
            if saved_database is not None:
                from reference.memory.hindsight import WORKER_ENVIRONMENT
                environment.update(WORKER_ENVIRONMENT)
            environment.update(HINDSIGHT_API_HOST="127.0.0.1", HINDSIGHT_API_LLM_TRACE_ENABLED="true", HINDSIGHT_API_LLM_TRACE_MAX_CHARS="0")
            self.process = subprocess.Popen(["/app/start-all.sh"], env=environment, user=1000, group=1000)
            services._wait_for_port(8888, self.process, timeout_seconds=180)
        elif self.provider == "supermemory":
            root = services.SUPERMEMORY_RUNTIME_ROOT
            root.mkdir(parents=True, exist_ok=True)
            if saved_database is not None:
                with tarfile.open(saved_database) as archive:
                    archive.extractall(root, filter="data")
                services.discard_restored_supermemory_pid(root)
            services._start_supermemory_openai_proxy()
            environment = {
                **os.environ,
                "SUPERMEMORY_DATA_DIR": str(root), "SUPERMEMORY_PORT": "6767",
                "SUPERMEMORY_DISABLE_TELEMETRY": "1",
                "BUN_JSC_useJIT": "false",
                "SUPERMEMORY_INGEST_CONCURRENCY": "1",
                "SUPERMEMORY_EMBEDDING_RAM_LIMIT": "4gb",
                "OPENAI_API_KEY": os.environ[services.AZURE_API_KEY_ENV],
                "OPENAI_BASE_URL": "http://127.0.0.1:6766/v1", "OPENAI_MODEL": services.MODEL_ID,
            }
            self.process = subprocess.Popen(["/usr/local/bin/supermemory-server"], env=environment, start_new_session=True)
            services._wait_for_port(6767, self.process, timeout_seconds=90)
            key = services._supermemory_server_key()
            services._wait_for_supermemory_api(self.process, key)
            self.proxy = wiring._start_supermemory_proxy(key)
            self.mcp = wiring._start_official_mcp()

    def backup(self, destination: Path) -> Path | None:
        if self.provider == "hindsight":
            backup = Path("/tmp/claude-hindsight-backup.zip")
            backup.unlink(missing_ok=True)
            self._admin("backup", str(backup), "--schema", "public")
            shutil.move(str(backup), destination)
            return destination
        if self.provider == "supermemory":
            if self.process is not None:
                raise RuntimeError("Supermemory must have stopped before copying its database")
            from reference.memory import services as services
            with tarfile.open(destination, "w:gz") as archive:
                def exclude_runtime_pid(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
                    return None if member.name == services.SUPERMEMORY_ENGINE_PID_PATH.as_posix() else member
                for path in services.SUPERMEMORY_RUNTIME_ROOT.iterdir():
                    if path.name != ".instance.lock":
                        archive.add(path, arcname=path.name, filter=exclude_runtime_pid)
            return destination
        return None

    def stop(self) -> None:
        if self.proxy is not None:
            self.proxy.shutdown()
            self.proxy.server_close()
            self.proxy = None
        if self.mcp is not None:
            self.mcp.terminate()
            self.mcp.wait()
            self.mcp = None
        if self.process is not None:
            if self.provider == "supermemory":
                from reference.execution.hermes import _stop_supermemory_service
                _stop_supermemory_service(self.process)
            else:
                self.process.terminate()
                self.process.wait()
            self.process = None


def run_history(*, config: loop.PluginIngestionConfig, messages: list,
                controller: Path, volume_root: Path, commit: Callable[[], None],
                run_call: Callable = loop.run_claude, service: Any = None,
                recovery: dict | None = None, run_budget_seconds: float = 22 * 3600) -> dict:
    """Restore once, run sequentially, and save progress without changing plugins."""
    if run_budget_seconds <= 0:
        raise ValueError("run_budget_seconds must be positive")
    message_limit = recovery.get("max_messages", len(messages)) if recovery else len(messages)
    if isinstance(message_limit, bool) or not isinstance(message_limit, int) or message_limit <= 0:
        raise ValueError("Recovery max_messages must be a positive integer")
    deadline = time.monotonic() + run_budget_seconds
    hosted = config.provider in {"mem0", "honcho"}
    store = RecoveryStore(volume_root / "saved", commit, compression_level=1 if hosted else 9)
    fresh_supermemory = (config.provider == "supermemory" and recovery is not None
                         and recovery.get("action") == "restart_empty")
    finish_supermemory_pilot = (config.provider == "supermemory" and recovery is not None
                               and recovery.get("action") == "finish_capture_pilot"
                               and config.plugin_settings.get("capture_session_end"))
    if fresh_supermemory and volume_root.exists() and any(volume_root.iterdir()):
        raise RuntimeError("Supermemory fresh recovery requires the inspected empty Volume")
    database = controller.parent / "database.backup"
    restored = store.restore(home=config.home, controller=controller, database_archive=None if hosted else database)
    controller.mkdir(parents=True, exist_ok=True)
    controller.chmod(0o700)
    receipt = controller / "receipt.json"
    service = service or LocalMemoryService(config.provider)
    attempt = volume_root / "attempt.json"
    if not receipt.exists():
        loop.initialize_plugin_receipt(receipt_path=receipt, config=config, messages=messages)
    state = loop._load_plugin_receipt(receipt)
    loop._validate_receipt(state, loop._config_contract(config, messages))
    recovery_progress = controller / "hindsight-recovery-progress.json"
    if recovery_progress.exists():
        proof = json.loads(recovery_progress.read_text())
        inspected_finish = (config.provider == "hindsight" and recovery and recovery.get("action") in {"continue", "finish"}
                            and proof.get("stage") == "pending"
                            and all(proof.get(key) == recovery.get(key) for key in ("source_id", "content_sha256")))
        verified = (proof.get("stage") == "verified"
                    and state["completed"].get(proof.get("source_id"), {}).get("content_sha256") == proof.get("content_sha256"))
        if not inspected_finish and not verified:
            raise RuntimeError("Hindsight recovery did not pass its first-turn gate; inspect it before resuming")
    active_recovery = None
    if recovery is not None and not fresh_supermemory and not finish_supermemory_pilot:
        from reference.runtimes.claude_recovery import prepare_plugin_recovery
        applied = prepare_plugin_recovery(config=config, messages=messages, controller=controller,
                                          volume_root=volume_root, restored=restored,
                                          recovery=recovery, commit=commit)
        if applied:
            active_recovery = recovery
            if recovery["action"] == "acknowledge":
                state = json.loads(receipt.read_text())
                # Persist the corrected controller with the unchanged, inspected
                # database before any service startup can fail.
                store.save(home=config.home, controller=controller, database_archive=None if hosted else database,
                           secrets=loop.credential_values(config.environment or {}, config.plugin_settings))
    if hosted and attempt.exists():
        previous = json.loads(attempt.read_text())
        reviewed_continuation = (active_recovery is not None and active_recovery.get("action") in {"continue", "retry"}
                                 and previous["source_id"] == active_recovery["source_id"]
                                 and previous["content_sha256"] == active_recovery["content_sha256"])
        if previous["source_id"] not in state["completed"] and not reviewed_continuation:
            raise RuntimeError("The last Claude message may already have reached the hosted service; inspect it before resuming")
    loop._require_safe_inflight(inflight_path=loop._inflight_path(receipt),
                               completed={key: value["content_sha256"] for key, value in state["completed"].items()},
                               expected_hashes=state["config"]["source_hashes"])
    if state["status"] == "completed":
        return {"status": "messages_completed", "completed_messages": len(state["completed"]), "processing_verified": False}
    if recovery and "max_messages" in recovery:
        # A repeated bounded recovery cannot spill into later history messages.
        message_limit -= max(0, len(state["completed"]) - recovery["completed_count"])
        if message_limit <= 0:
            return {"status": "in_progress", "completed_messages": len(state["completed"]), "processing_verified": False}
    secrets = loop.credential_values(config.environment or {}, config.plugin_settings)
    supermemory_gate = None
    if config.provider == "supermemory" and config.plugin_settings.get("capture_session_end"):
        from reference.runtimes.claude_recovery import CaptureGate
        supermemory_gate = CaptureGate(config, controller, deadline)
    hindsight_gate = None
    if config.provider == "hindsight" and active_recovery and active_recovery["action"] in {"continue", "finish"}:
        from reference.runtimes.claude_recovery import RecoveryGate
        hindsight_gate = RecoveryGate(config, active_recovery, deadline)
        loop._atomic_json(recovery_progress, {
            "stage": "pending", "source_id": active_recovery["source_id"],
            "content_sha256": active_recovery["content_sha256"],
        })

    def save() -> None:
        db = service.backup(database)
        store.save(home=config.home, controller=controller, database_archive=db, secrets=secrets)

    def record_failure(stage: str) -> None:
        evidence = {"provider": config.provider, "stage": stage, "error": traceback.format_exc()}
        try:
            marker = loop._inflight_path(receipt)
            latest_source = None
            if marker.exists():
                evidence["inflight"] = json.loads(marker.read_text())
                latest_source = evidence["inflight"]["source_id"]
            elif receipt.exists():
                completed_ids = json.loads(receipt.read_text())["completed"]
                latest_source = next((source.source_id for source in reversed(messages)
                                      if source.source_id in completed_ids), None)
            for index, source in enumerate(messages):
                if source.source_id == latest_source:
                    trace = loop._trace_path(controller / "traces", index, source.source_id)
                    if trace.exists():
                        evidence["trace"] = json.loads(trace.read_text())
                        session = evidence["trace"].get("result", {}).get("session_id")
                        evidence["transcripts"] = [
                            {"path": str(path.relative_to(config.claude_config_dir)), "text": path.read_text(errors="replace")}
                            for path in (config.claude_config_dir / "projects").rglob("*.jsonl")
                            if path.name == str(session) + ".jsonl" and not path.is_symlink()]
                    break
        except Exception as exc:
            evidence["evidence_collection_error"] = str(exc)
        evidence = loop.redact_credentials(evidence, secrets)
        try:
            loop._atomic_json(volume_root / "diagnostics" / (uuid.uuid4().hex + ".json"), evidence)
            commit()
        except Exception:
            print(json.dumps({"diagnostic_save_failed": True, "evidence": evidence}), flush=True)

    def preserve_unverified_supermemory(error: BaseException) -> None:
        """Keep a stopped failed runtime for inspection, never as a resume point."""
        if config.provider != "supermemory":
            return
        try:
            if not loop._inflight_path(receipt).exists():
                return
            from reference.memory import services as services
            process = getattr(service, "process", None)
            if process is not None and process.poll() is None:
                raise RuntimeError("Refusing to archive a running Supermemory process")
            destination_root = volume_root / "failed-runs"
            destination_root.mkdir(parents=True, exist_ok=True)
            name = f"failure-{time.time_ns()}-{os.getpid()}"
            staging = destination_root / ("." + name + ".tmp")
            destination = destination_root / name
            staging.mkdir()
            runtime_root = services.SUPERMEMORY_RUNTIME_ROOT
            if not runtime_root.is_dir():
                staging.rmdir()
                return
            archive_path = staging / "unverified-runtime.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                def exclude_runtime_pid(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
                    return None if Path(member.name) == services.SUPERMEMORY_ENGINE_PID_PATH else member
                for path in runtime_root.iterdir():
                    if path.name != ".instance.lock":
                        archive.add(path, arcname=path.name, filter=exclude_runtime_pid)
            log_path = services.SUPERMEMORY_SERVER_LOG_PATH
            if log_path.is_file():
                shutil.copy2(log_path, staging / "supermemory-server.log")
            loop._atomic_json(staging / "failure.json", {
                "schema_version": 1,
                "provider": "supermemory",
                "error": f"{type(error).__name__}: {error}",
                "eligible_for_automatic_resume": False,
                "archive_sha256": services._sha256_file(archive_path),
            })
            os.replace(staging, destination)
            commit()
        except Exception as archive_error:
            print(json.dumps({"unverified_supermemory_archive_failed": str(archive_error)}), flush=True)

    def persist(path: Path, value: dict) -> None:
        loop._atomic_json(path, value)
        if hosted and path.name == "in-flight.json":
            loop._atomic_json(attempt, value)
            commit()
        if path == receipt:
            count = len(value["completed"])
            if hosted or (config.provider == "hindsight" and count % 100 == 0):
                save()

    started = False
    original_run_call = run_call
    supermemory_journal = (lambda index, source, result: journal_supermemory_turn(
        config=config, volume_root=volume_root, commit=commit, secrets=secrets,
        index=index, source=source, result=result,
    )) if original_run_call is loop.run_claude else None
    if active_recovery is not None and active_recovery["action"] in {"continue", "finish"}:
        from reference.runtimes.claude_recovery import HONCHO_CONTINUATION
        source = next(source for source in messages if source.source_id == recovery["source_id"])
        continued = False

        def run_call(message: str, **kwargs: Any) -> Any:
            nonlocal continued
            if not continued:
                if message != source.content or kwargs.get("narrative_time") != source.timestamp:
                    raise RuntimeError("Continuation must be the next original source")
                continued = True
                if active_recovery["action"] == "finish":
                    result = json.loads((controller / recovery["trace_relative_path"]).read_text())["result"]
                else:
                    kwargs.update(narrative_time=None, recovery_session_id=recovery["session_id"])
                    result = original_run_call(HONCHO_CONTINUATION, **kwargs)
                result_session = result.get("session_id") if isinstance(result, dict) else result.session_id
                if result_session != recovery["session_id"]:
                    raise RuntimeError("Claude did not resume the original interrupted session")
                return result
            return original_run_call(message, **kwargs)
    ingestion_error = None
    try:
        service.start(database if restored and database.exists() else None)
        started = True
        if supermemory_gate is not None:
            proof = controller / "supermemory-pilot.json"
            if not proof.exists() and finish_supermemory_pilot:
                supermemory_gate.finish_saved_probe(recovery, restored, state)
                loop._atomic_json(proof, {"status": "verified", "completed_messages": 2,
                                          "saved_probe_adjudication": recovery})
                service.stop()
                save()
                service.start(database)
                print(json.dumps({"supermemory_pilot": "verified_from_saved_probe", "completed_messages": 2}), flush=True)
            if not proof.exists():
                if state["completed"]:
                    raise RuntimeError("The Supermemory pilot is incomplete; inspect before continuing")
                first = loop.ingest(receipt_path=receipt, trace_dir=controller / "traces", config=config,
                                    messages=messages, max_messages=min(2, message_limit), run_call=run_call,
                                    persist=persist, deadline=deadline, verify_turn=supermemory_gate.verify_turn,
                                    before_turn=supermemory_gate.preflight,
                                    journal_turn=supermemory_journal)
                if first.get("pause") or len(first["completed"]) != 2:
                    return {"status": "rate_limited" if first.get("pause") else "in_progress",
                            "completed_messages": len(first["completed"]), "pause": first.get("pause")}
                supermemory_gate.wait_ready()
                service.stop()
                save()
                service.start(database)
                supermemory_gate.wait_ready()
                supermemory_gate.verify_fresh_retrieval()
                loop._atomic_json(proof, {"status": "verified", "completed_messages": 2})
                service.stop()
                save()
                service.start(database)
                message_limit -= 2
                print(json.dumps({"supermemory_pilot": "verified", "completed_messages": 2}), flush=True)
                if message_limit <= 0:
                    return {"status": "in_progress", "completed_messages": 2, "processing_verified": True}
            else:
                supermemory_gate.verify_saved(state)
            if active_recovery and active_recovery["action"] == "continue":
                source = next(source for source in messages if source.source_id == active_recovery["source_id"])
                supermemory_gate.begin_continuation(source, active_recovery)
        if fresh_supermemory:
            first = loop.ingest(receipt_path=receipt, trace_dir=controller / "traces", config=config,
                                messages=messages, max_messages=1, run_call=run_call, persist=persist, deadline=deadline)
            service.stop()
            save()
            if first.get("pause") or first["status"] == "completed" or message_limit == 1:
                return {"status": "rate_limited" if first.get("pause") else "messages_completed"
                        if first["status"] == "completed" else "in_progress",
                        "completed_messages": len(first["completed"]), "processing_verified": False}
            service.start(database)
            message_limit -= 1
            print(json.dumps({"supermemory_first_checkpoint_restarted": True,
                              "completed_messages": len(first["completed"])}), flush=True)
        if hindsight_gate is not None:
            hindsight_gate.before_claude()
            first = loop.ingest(receipt_path=receipt, trace_dir=controller / "traces", config=config,
                                messages=messages, max_messages=1, run_call=run_call, persist=persist,
                                deadline=deadline, verify_turn=hindsight_gate.verify_turn)
            if active_recovery["source_id"] not in first["completed"]:
                return {"status": "rate_limited" if first.get("pause") else "in_progress",
                        "completed_messages": len(first["completed"]), "processing_verified": False,
                        "pause": first.get("pause")}
            if (len(first["completed"]) != len(state["completed"]) + 1
                    or any(first["completed"].get(key) != value for key, value in state["completed"].items())):
                raise RuntimeError("Hindsight recovery changed earlier completed receipts")
            loop._atomic_json(recovery_progress, {
                "stage": "verified", "source_id": active_recovery["source_id"],
                "content_sha256": active_recovery["content_sha256"],
                "completed_messages": len(first["completed"]), **hindsight_gate.evidence,
            })
            save()
            print(json.dumps({"hindsight_recovery_stage": "checkpoint_verified",
                              "completed_messages": len(first["completed"])}), flush=True)
            message_limit -= 1
            if message_limit == 0 or first["status"] == "completed":
                return {"status": "messages_completed" if first["status"] == "completed" else "in_progress",
                        "completed_messages": len(first["completed"]), "processing_verified": True}
        if supermemory_gate is None:
            result = loop.ingest(receipt_path=receipt, trace_dir=controller / "traces", config=config,
                                 messages=messages, max_messages=message_limit, run_call=run_call,
                                 persist=persist, deadline=deadline)
        else:
            remaining_limit = message_limit
            while True:
                before = len(loop._load_plugin_receipt(receipt)["completed"])
                to_checkpoint = 100 - (before % 100)
                batch = min(remaining_limit, to_checkpoint)
                result = loop.ingest(
                    receipt_path=receipt, trace_dir=controller / "traces", config=config,
                    messages=messages, max_messages=batch, run_call=run_call, persist=persist,
                    deadline=deadline, verify_turn=supermemory_gate.verify_turn,
                    before_turn=supermemory_gate.preflight,
                    journal_turn=supermemory_journal,
                )
                processed = len(result["completed"]) - before
                remaining_limit -= processed
                if result.get("pause") or result["status"] == "completed" or processed < batch or remaining_limit <= 0:
                    break
                if len(result["completed"]) % 100 == 0:
                    supermemory_gate.wait_ready()
                    service.stop()
                    started = False
                    save()
                    service.start(database)
                    started = True
                    supermemory_gate.verify_saved(result)
                    supermemory_gate.preflight()
                    print(json.dumps({"supermemory_checkpoint_restarted": True,
                                      "completed_messages": len(result["completed"])}), flush=True)
        if result.get("pause", {}).get("reason") == "claude_usage_limit":
            return {"status": "rate_limited", "completed_messages": len(result["completed"]),
                    "processing_verified": False, "pause": result["pause"]}
        processing_verified = False
        if result["status"] == "completed" and hindsight_gate is not None:
            hindsight_gate.drain(set())
            processing_verified = True
        return {"status": "messages_completed" if result["status"] == "completed" else "in_progress",
                "completed_messages": len(result["completed"]), "processing_verified": processing_verified}
    except Exception as error:
        ingestion_error = error
        record_failure("ingestion")
        raise
    finally:
        if config.provider == "supermemory" and ingestion_error is not None:
            try:
                service.stop()
            except Exception:
                record_failure("checkpoint")
            preserve_unverified_supermemory(ingestion_error)
        else:
            try:
                if not started:
                    service.stop()
                elif config.provider == "supermemory":
                    service.stop()
                    save()
                else:
                    try:
                        save()
                    finally:
                        service.stop()
            except Exception as checkpoint_error:
                preserve_unverified_supermemory(checkpoint_error)
                record_failure("checkpoint")
                raise


from reference.runtimes.claude_history import LocalMemoryService


_PROVIDERS = frozenset({"hindsight", "supermemory"})


def _request(
    url: str, *, method: str, api_key: str, body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status not in {200, 201}:
            raise RuntimeError(f"metadata request returned HTTP {response.status}")
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("metadata request returned a non-object response")
    return value


def _remove_disposable_database(provider: str) -> Path:
    from reference.memory import services as services

    roots = {
        "hindsight": services.HINDSIGHT_RUNTIME_ROOT,
        "supermemory": services.SUPERMEMORY_RUNTIME_ROOT,
    }
    root = roots[provider]
    if not root.is_absolute() or root not in set(roots.values()):
        raise RuntimeError("refusing to remove a non-disposable database directory")
    shutil.rmtree(root)
    return root


def _check_hindsight(service: LocalMemoryService, archive: Path) -> dict[str, Any]:
    from reference.memory import services as services

    bank_id = f"database-check-{uuid.uuid4().hex}"
    bank_name = "Disposable database backup check"
    mission = "Persist this metadata-only bank without retaining or extracting content."
    api_key = os.environ[services.HINDSIGHT_API_KEY_ENV]
    try:
        service.start(None)
        created = _request(
            f"http://127.0.0.1:8888/v1/default/banks/{bank_id}",
            method="PUT",
            api_key=api_key,
            body={"name": bank_name, "mission": mission},
        )
        if created.get("bank_id") != bank_id or created.get("name") != bank_name:
            raise RuntimeError("Hindsight did not create the disposable metadata bank")
        if created.get("mission") != mission:
            raise RuntimeError("Hindsight did not persist the disposable bank config")
        if service.backup(archive) != archive or not archive.is_file():
            raise RuntimeError("Hindsight did not produce a backup archive")
    finally:
        service.stop()

    _remove_disposable_database("hindsight")
    try:
        service.start(archive)
        restored = _request(
            f"http://127.0.0.1:8888/v1/default/banks/{bank_id}/profile",
            method="GET",
            api_key=api_key,
        )
        if restored.get("bank_id") != bank_id or restored.get("name") != bank_name:
            raise RuntimeError("Hindsight restore lost the disposable metadata bank")
        if restored.get("mission") != mission:
            raise RuntimeError("Hindsight restore lost the disposable bank config")
    finally:
        service.stop()
    return {"record": "metadata_bank", "bank_id": bank_id, "verified": True}


def _check_supermemory(service: LocalMemoryService, archive: Path) -> dict[str, Any]:
    from reference.memory import services as services

    try:
        service.start(None)
        # v0.0.8 has no metadata-only container creation endpoint. Documents
        # materialize tags, but their processing can invoke extraction/models.
        key_path = services.SUPERMEMORY_RUNTIME_ROOT / "api-key"
        if not key_path.is_file():
            raise RuntimeError("Supermemory did not initialize its local API-key record")
        key_sha256 = hashlib.sha256(key_path.read_bytes()).hexdigest()
        sentinel = services.SUPERMEMORY_RUNTIME_ROOT / "database-check-sentinel.bin"
        sentinel_bytes = b"dolphinbench-supermemory-disposable-backup-check-v1\n"
        sentinel.write_bytes(sentinel_bytes)
    finally:
        service.stop()

    if service.backup(archive) != archive or not archive.is_file():
        raise RuntimeError("Supermemory did not produce a stopped-service backup archive")
    _remove_disposable_database("supermemory")
    try:
        service.start(archive)
        key_path = services.SUPERMEMORY_RUNTIME_ROOT / "api-key"
        if not key_path.is_file() or hashlib.sha256(key_path.read_bytes()).hexdigest() != key_sha256:
            raise RuntimeError("Supermemory restore lost the initialized local API-key record")
        if sentinel.read_bytes() != sentinel_bytes:
            raise RuntimeError("Supermemory restore lost the disposable filesystem sentinel")
    finally:
        service.stop()
    return {
        "record": "initialized_database_and_api_key",
        "metadata_only_container_api": False,
        "sufficient_to_find_backup_failure": True,
        "verified": True,
    }


def check_database(provider: str) -> dict[str, Any]:
    """Round-trip one disposable provider database using LocalMemoryService."""
    if provider not in _PROVIDERS:
        raise ValueError("provider must be 'hindsight' or 'supermemory'")
    service = LocalMemoryService(provider)
    with tempfile.TemporaryDirectory(prefix=f"claude-{provider}-database-check-") as raw:
        archive = Path(raw) / "database.backup"
        if provider == "hindsight":
            result = _check_hindsight(service, archive)
        else:
            result = _check_supermemory(service, archive)
        return {"provider": provider, "archive_bytes": archive.stat().st_size, **result}
