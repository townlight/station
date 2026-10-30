# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Portable, encrypted station backups and offline restore operations.

The older :mod:`civiccast.dr.backup` module is a database restore-drill
primitive. This module packages that real database snapshot with selected
configuration, credential exports, and complete media files into a portable
encrypted archive. Runtime paths and credential adapters are supplied by the
station integration layer so this module never guesses where live state is.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from civiccast.dr.backup import (
    read_backup_manifest,
    run_full_backup,
    run_postgres_restore,
    verify_backup_integrity,
)

_MAGIC = b"CCSBK01\0"
_HEADER_SIZE = len(_MAGIC) + 16 + 12 + 16
_PBKDF2_ITERATIONS = 600_000
_CHUNK_SIZE = 1024 * 1024
_ARCHIVE_GLOB = "station-backup-*.ccbackup"
_MAX_RETENTION = 100
_MAX_SCHEDULED_ATTEMPTS = 3
_SCHEDULED_RETRY_DELAYS_SECONDS = (15 * 60, 60 * 60)
_STATE_LOAD_ERROR = "_load_error"


class BackupSchedule(BaseModel):
    """One daily UTC backup schedule and the maximum retained archive count."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    destination: str = Field(min_length=1, max_length=1024)
    hour_utc: int = Field(ge=0, le=23)
    minute_utc: int = Field(ge=0, le=59)
    retention_count: int = Field(ge=1, le=_MAX_RETENTION)
    selected_media_roots: list[str] = Field(default_factory=list, max_length=256)

    @field_validator("destination")
    @classmethod
    def destination_must_be_absolute(cls, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("backup destination must be an absolute path")
        return str(path)

    @field_validator("selected_media_roots")
    @classmethod
    def media_roots_must_be_absolute(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ValueError("selected media roots must be absolute paths")
            canonical = str(path)
            key = canonical.casefold()
            if key not in seen:
                normalized.append(canonical)
                seen.add(key)
        return normalized

    @model_validator(mode="after")
    def destination_must_be_separate_from_media(self) -> BackupSchedule:
        _validate_destination_media_overlap(self.destination, self.selected_media_roots)
        return self


@dataclass(frozen=True)
class BackupArtifact:
    backup_id: str
    path: Path
    created_at: datetime
    size_bytes: int


@dataclass(frozen=True)
class BackupVerification:
    backup_id: str
    valid: bool
    database_engine: str
    files_verified: int
    created_at: datetime


@dataclass(frozen=True)
class RestoreResult:
    backup_id: str
    database_restored: bool
    files_restored: int
    database_engine: str
    finished_at: datetime


CredentialExporter = Callable[[], Mapping[str, str | bytes]]
CredentialImporter = Callable[[Mapping[str, bytes]], None]
CredentialValidator = Callable[[Mapping[str, bytes]], None]
ScheduledPassphraseSetter = Callable[[str], None]
ScheduledPassphraseProvider = Callable[[], str]
ScheduledPassphraseAvailable = Callable[[], bool]
BackupPathsProvider = Callable[[], Sequence[Path]]
ProgressCallback = Callable[[str, int], None]


class StationBackupService:
    """Create, schedule, verify, and restore encrypted station backup archives.

    ``config_paths``, ``credential_paths`` and ``media_roots`` are resolved by
    the station runtime and passed explicitly. Keyring-backed secrets use the
    exporter/importer callbacks; a machine-protected vault file must not be
    copied as if it were portable. Scheduled passphrases come from a caller
    supplied OS-protected secret provider and are never persisted here.
    """

    def __init__(
        self,
        *,
        database_url: str,
        destination: Path,
        config_paths: Sequence[Path] = (),
        credential_paths: Sequence[Path] = (),
        config_paths_provider: BackupPathsProvider | None = None,
        credential_paths_provider: BackupPathsProvider | None = None,
        media_roots: Sequence[Path] = (),
        state_path: Path | None = None,
        credential_exporter: CredentialExporter | None = None,
        credential_importer: CredentialImporter | None = None,
        credential_validator: CredentialValidator | None = None,
        scheduled_passphrase_setter: ScheduledPassphraseSetter | None = None,
        scheduled_passphrase_provider: ScheduledPassphraseProvider | None = None,
        scheduled_passphrase_available: ScheduledPassphraseAvailable | None = None,
        pg_dump_command: list[str] | None = None,
        pg_dumpall_command: list[str] | None = None,
        pg_restore_command: list[str] | None = None,
    ) -> None:
        self.database_url = database_url
        self.destination = Path(destination).expanduser().absolute()
        self.config_paths = tuple(Path(path).expanduser().absolute() for path in config_paths)
        self.credential_paths = tuple(
            Path(path).expanduser().absolute() for path in credential_paths
        )
        self.config_paths_provider = config_paths_provider
        self.credential_paths_provider = credential_paths_provider
        self.media_roots = tuple(Path(path).expanduser().absolute() for path in media_roots)
        self.state_path = (
            Path(state_path).expanduser().absolute()
            if state_path is not None
            else self.destination / ".station-backup-state.json"
        )
        self.credential_exporter = credential_exporter
        self.credential_importer = credential_importer
        self.credential_validator = credential_validator
        self._scheduled_passphrase_setter = scheduled_passphrase_setter
        self._scheduled_passphrase_provider = scheduled_passphrase_provider
        self._scheduled_passphrase_available = scheduled_passphrase_available
        self.pg_dump_command = pg_dump_command
        self.pg_dumpall_command = pg_dumpall_command
        self.pg_restore_command = pg_restore_command
        self._lock = threading.RLock()
        self._job_lock = threading.Lock()
        self._worker_stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._last_progress: tuple[str, int] | None = None
        self._last_progress_write = 0.0
        self._state = self._load_state()
        persisted_schedule = self._schedule()
        self.destination = Path(persisted_schedule.destination).expanduser().absolute()
        self.media_roots = tuple(
            Path(root).expanduser().absolute() for root in persisted_schedule.selected_media_roots
        )

    def _load_state(self) -> dict[str, Any]:
        try:
            serialized = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            if os.path.lexists(self.state_path) or any(
                parent.is_symlink() and not parent.exists() for parent in self.state_path.parents
            ):
                return {
                    _STATE_LOAD_ERROR: "FileNotFoundError: station backup state path is redirected"
                }
            return {}
        except OSError as exc:
            return {
                _STATE_LOAD_ERROR: (f"{type(exc).__name__}: station backup state could not be read")
            }
        except UnicodeError:
            return {_STATE_LOAD_ERROR: "UnicodeError: station backup state is not valid UTF-8"}
        try:
            raw = json.loads(serialized)
        except json.JSONDecodeError:
            return {_STATE_LOAD_ERROR: "JSONDecodeError: station backup state is invalid"}
        if not isinstance(raw, dict):
            return {_STATE_LOAD_ERROR: "ValueError: station backup state must be a JSON object"}
        return raw

    def _save_state(self) -> None:
        if _STATE_LOAD_ERROR in self._state:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp.write_text(json.dumps(self._state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp.replace(self.state_path)

    def _progress(self, phase: str, percent: int, callback: ProgressCallback | None) -> None:
        clamped_percent = max(0, min(100, percent))
        status = {"phase": phase, "percent": clamped_percent, "updated_at": _now()}
        with self._lock:
            progress_value = (phase, clamped_percent)
            now = time.monotonic()
            if progress_value != self._last_progress or now - self._last_progress_write >= 1.0:
                self._state["job"] = status
                self._save_state()
                self._last_progress = progress_value
                self._last_progress_write = now
        if callback is not None:
            callback(phase, clamped_percent)

    def create_backup(
        self,
        passphrase: str,
        *,
        progress: ProgressCallback | None = None,
        retention_count: int | None = None,
    ) -> BackupArtifact:
        """Snapshot live state, encrypt it, atomically publish, then prune old archives."""

        _require_passphrase(passphrase)
        schedule = self._schedule()
        if _STATE_LOAD_ERROR in self._state:
            raise RuntimeError("repair the persisted backup state before creating another backup")
        _validate_destination_media_overlap(self.destination, self.media_roots)
        if not self._job_lock.acquire(blocking=False):
            raise RuntimeError("another station backup operation is already running")
        try:
            self.destination.mkdir(parents=True, exist_ok=True)
            effective_retention = (
                schedule.retention_count if retention_count is None else retention_count
            )
            _validate_retention(effective_retention)
            started = datetime.now(UTC)
            backup_id = f"station-backup-{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
            with tempfile.TemporaryDirectory(prefix="civiccast-backup-") as scratch_name:
                scratch = Path(scratch_name)
                _protect_staging_directory(scratch)
                bundle_dir = scratch / "bundle"
                bundle_dir.mkdir()
                self._progress("database", 5, progress)
                database_dir = bundle_dir / "database"
                manifest = run_full_backup(
                    database_url=self.database_url,
                    dest_dir=database_dir,
                    pg_dump_command=self.pg_dump_command,
                    pg_dumpall_command=self.pg_dumpall_command,
                )
                payload = self._copy_payload(
                    bundle_dir,
                    lambda phase, percent: self._progress(phase, percent, progress),
                )
                credentials = self.credential_exporter() if self.credential_exporter else {}
                if credentials:
                    credential_payload = {
                        name: {
                            "encoding": "base64" if isinstance(value, bytes) else "utf-8",
                            "value": base64.b64encode(value).decode("ascii")
                            if isinstance(value, bytes)
                            else value,
                        }
                        for name, value in credentials.items()
                    }
                    credential_file = bundle_dir / "portable-credentials.json"
                    credential_file.write_text(
                        json.dumps(credential_payload, sort_keys=True), encoding="utf-8"
                    )
                    payload.append(
                        _file_record(
                            credential_file, "credential-export", "portable-credentials.json"
                        )
                    )
                station_manifest = {
                    "schema_version": 1,
                    "backup_id": backup_id,
                    "created_at": started.isoformat(),
                    "database_engine": manifest.engine,
                    "database_manifest": "database/manifest.json",
                    "payload": payload,
                    "credential_export_member": (
                        "portable-credentials.json" if credentials else None
                    ),
                }
                station_manifest["members"] = _hash_members(bundle_dir)
                (bundle_dir / "station-backup.json").write_text(
                    json.dumps(station_manifest, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                self._progress("encrypting", 80, progress)
                final_path = self.destination / f"{backup_id}.ccbackup"
                partial_path = final_path.with_suffix(final_path.suffix + ".partial")
                zip_path = scratch / "bundle.zip"
                _write_zip(bundle_dir, zip_path)
                try:
                    _encrypt_file(zip_path, partial_path, passphrase)
                    partial_path.replace(final_path)
                finally:
                    partial_path.unlink(missing_ok=True)
            self._prune(effective_retention)
            artifact = BackupArtifact(
                backup_id=backup_id,
                path=final_path,
                created_at=started,
                size_bytes=final_path.stat().st_size,
            )
            with self._lock:
                self._state["last_success"] = {
                    "backup_id": backup_id,
                    "path": str(final_path),
                    "created_at": started.isoformat(),
                    "size_bytes": artifact.size_bytes,
                }
                self._state["last_error"] = None
                self._state["job"] = {"phase": "complete", "percent": 100, "updated_at": _now()}
                self._save_state()
            if progress is not None:
                progress("complete", 100)
            return artifact
        except Exception as exc:
            with self._lock:
                self._state["last_error"] = _safe_error(exc)
                self._state["job"] = {"phase": "failed", "percent": 0, "updated_at": _now()}
                self._save_state()
            raise
        finally:
            self._job_lock.release()

    def _copy_payload(
        self, bundle_dir: Path, progress: ProgressCallback | None
    ) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        media_files = [
            (index, source, child)
            for index, source in enumerate(self.media_roots)
            if source.exists() and source.is_dir() and not source.is_symlink()
            for child in sorted(source.rglob("*"))
            if child.is_file()
        ]
        media_total = sum(path.stat().st_size for _, _, path in media_files)
        media_done = 0
        for kind, configured, provider in (
            ("config", self.config_paths, self.config_paths_provider),
            ("credential-file", self.credential_paths, self.credential_paths_provider),
        ):
            sources = _merge_paths(configured, provider() if provider else ())
            for index, source in enumerate(sources):
                if not source.is_file():
                    continue
                if source.is_symlink():
                    raise ValueError(f"selected source file must not be a symbolic link: {source}")
                member = f"payload/{kind}/{index:03d}-{source.name}"
                target = bundle_dir / member
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                payload.append({**_file_record(target, kind, member), "source_path": str(source)})
        for index, source in enumerate(self.media_roots):
            if not source.exists():
                continue
            if source.is_symlink() or not source.is_dir():
                raise ValueError(f"selected media root must be a real directory: {source}")
            root_member = f"payload/media/{index:03d}"
            for child in sorted(source.rglob("*")):
                if child.is_symlink():
                    raise ValueError(f"selected media contains a symbolic link: {child}")
                if not child.is_file():
                    continue
                relative = child.relative_to(source)
                member = f"{root_member}/{relative.as_posix()}"
                target = bundle_dir / PurePosixPath(member)
                target.parent.mkdir(parents=True, exist_ok=True)
                last_percent = -1

                def media_progress(copied: int) -> None:
                    nonlocal last_percent, media_done
                    media_done += copied
                    if progress is not None:
                        fraction = media_done / media_total if media_total else 1
                        percent = 30 + round(fraction * 45)
                        if percent != last_percent:
                            progress("copying-media", percent)
                            last_percent = percent

                _copy_file(child, target, media_progress)
                payload.append(
                    {
                        **_file_record(target, "media", member),
                        "source_path": str(source),
                        "relative_path": relative.as_posix(),
                    }
                )
            payload.append(
                {
                    "kind": "media-root",
                    "source_path": str(source),
                    "member_prefix": root_member,
                }
            )
        return payload

    def verify_backup(self, archive_path: Path, passphrase: str) -> BackupVerification:
        _require_passphrase(passphrase)
        with tempfile.TemporaryDirectory(prefix="civiccast-verify-") as temp_name:
            bundle = _decrypt_and_extract(Path(archive_path), Path(temp_name), passphrase)
            manifest = _read_station_manifest(bundle)
            verified = _verify_members(bundle, manifest)
            core_dir = bundle / "database"
            core_manifest = read_backup_manifest(core_dir)
            errors = verify_backup_integrity(core_dir, core_manifest)
            if errors:
                raise ValueError("database backup integrity verification failed")
            return BackupVerification(
                backup_id=str(manifest["backup_id"]),
                valid=True,
                database_engine=str(manifest["database_engine"]),
                files_verified=verified,
                created_at=datetime.fromisoformat(str(manifest["created_at"])),
            )

    def inspect_backup(self, archive_path: Path, passphrase: str) -> dict[str, Any]:
        """Return the authenticated restore source-path inventory for mapping."""

        with tempfile.TemporaryDirectory(prefix="civiccast-inspect-") as temp_name:
            bundle = _decrypt_and_extract(Path(archive_path), Path(temp_name), passphrase)
            manifest = _read_station_manifest(bundle)
            _verify_members(bundle, manifest)
            return {
                "backup_id": manifest["backup_id"],
                "created_at": manifest["created_at"],
                "database_engine": manifest["database_engine"],
                "payload": [
                    {
                        "kind": record.get("kind"),
                        "source_path": record.get("source_path"),
                        "relative_path": record.get("relative_path"),
                        "size_bytes": record.get("size_bytes"),
                    }
                    for record in manifest["payload"]
                ],
            }

    def restore_backup(
        self,
        archive_path: Path,
        passphrase: str,
        *,
        target_database_url: str,
        target_paths: Mapping[str, Path] | None = None,
        writers_stopped: bool = False,
        progress: ProgressCallback | None = None,
    ) -> RestoreResult:
        """Restore actual database contents and mapped files to a stopped station.

        The caller must stop CivicCast writers and provide the replacement
        station's database URL/absolute path mappings. Postgres replacement
        installs may already have provisioned their schema; the archived
        objects are replaced transactionally. Managed SQLite targets are
        atomically replaced after the restore file passes SQLite's integrity check.
        """

        _require_passphrase(passphrase)
        if not writers_stopped:
            raise RuntimeError(
                "restore is offline-only; confirm that CivicCast writers are stopped"
            )
        if not self._job_lock.acquire(blocking=False):
            raise RuntimeError("another station backup operation is already running")
        target_paths = target_paths or {}
        try:
            with tempfile.TemporaryDirectory(prefix="civiccast-restore-") as temp_name:
                bundle = _decrypt_and_extract(Path(archive_path), Path(temp_name), passphrase)
                manifest = _read_station_manifest(bundle)
                _verify_members(bundle, manifest)
                _validate_payload_targets(manifest["payload"], target_paths)
                media_path_mappings = _media_path_mappings(manifest["payload"], target_paths)
                _remap_station_state_payload(bundle, manifest["payload"], media_path_mappings)
                credentials_member = manifest.get("credential_export_member")
                if credentials_member and self.credential_importer is None:
                    raise RuntimeError(
                        "portable credentials are in this archive but no station credential importer is configured"
                    )
                credentials = _decode_portable_credentials(bundle, credentials_member)
                if credentials is not None and self.credential_validator is not None:
                    self.credential_validator(credentials)
                database_dir = bundle / "database"
                core_manifest = read_backup_manifest(database_dir)
                if verify_backup_integrity(database_dir, core_manifest):
                    raise ValueError("database backup integrity verification failed")
                self._progress("restoring-database", 25, progress)
                if core_manifest.engine == "sqlite":
                    _restore_sqlite(database_dir / core_manifest.db_artifact, target_database_url)
                    _remap_database_media_paths(target_database_url, media_path_mappings)
                elif core_manifest.engine == "postgres":
                    _restore_postgres(
                        database_dir / core_manifest.db_artifact,
                        target_database_url,
                        self.pg_restore_command,
                    )
                    _remap_database_media_paths(target_database_url, media_path_mappings)
                else:
                    raise ValueError(f"unsupported database engine: {core_manifest.engine}")
                restored = _restore_payload(
                    bundle,
                    manifest["payload"],
                    target_paths,
                    progress=lambda phase, percent: self._progress(phase, percent, progress),
                )
                if credentials is not None and self.credential_importer is not None:
                    self.credential_importer(credentials)
                self._progress("complete", 100, progress)
                return RestoreResult(
                    backup_id=str(manifest["backup_id"]),
                    database_restored=True,
                    files_restored=restored,
                    database_engine=str(manifest["database_engine"]),
                    finished_at=datetime.now(UTC),
                )
        except Exception as exc:
            with self._lock:
                self._state["last_error"] = _safe_error(exc)
                self._state["job"] = {"phase": "failed", "percent": 0, "updated_at": _now()}
                self._save_state()
            raise
        finally:
            self._job_lock.release()

    def list_backups(self) -> list[BackupArtifact]:
        if not self.destination.is_dir():
            return []
        artifacts: list[BackupArtifact] = []
        for path in self.destination.glob(_ARCHIVE_GLOB):
            if not path.is_file() or path.is_symlink():
                continue
            stat = path.stat()
            artifacts.append(
                BackupArtifact(
                    backup_id=path.stem,
                    path=path,
                    created_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                    size_bytes=stat.st_size,
                )
            )
        return sorted(artifacts, key=lambda item: item.created_at, reverse=True)

    def set_schedule(self, schedule: BackupSchedule) -> BackupSchedule:
        if schedule.enabled and not self.has_scheduled_passphrase():
            raise ValueError("set an unattended recovery passphrase before enabling the schedule")
        destination = Path(schedule.destination).expanduser().absolute()
        media_roots = tuple(
            Path(root).expanduser().absolute() for root in schedule.selected_media_roots
        )
        _validate_destination_media_overlap(destination, media_roots)
        with self._lock:
            previous_destination = self.destination
            previous_media_roots = self.media_roots
            previous_state = self._state.copy()
            self.destination = destination
            self.media_roots = media_roots
            if self._state.pop(_STATE_LOAD_ERROR, None) is not None:
                self._state["last_error"] = None
            self._state["schedule"] = schedule.model_dump(mode="json")
            try:
                self._save_state()
            except OSError:
                self.destination = previous_destination
                self.media_roots = previous_media_roots
                self._state = previous_state
                raise
        return schedule

    def set_scheduled_passphrase(self, passphrase: str) -> None:
        """Store the scheduler key through the caller's OS-protected vault only."""

        _require_passphrase(passphrase)
        if self._scheduled_passphrase_setter is None:
            raise RuntimeError("OS-protected scheduled passphrase storage is unavailable")
        self._scheduled_passphrase_setter(passphrase)

    def has_scheduled_passphrase(self) -> bool:
        return bool(self._scheduled_passphrase_available and self._scheduled_passphrase_available())

    def _schedule(self) -> BackupSchedule:
        raw = self._state.get("schedule")
        if "schedule" in self._state and isinstance(raw, dict):
            raw = dict(raw)
            try:
                if "selected_media_roots" not in raw:
                    raw["selected_media_roots"] = [str(path) for path in self.media_roots]
                return BackupSchedule.model_validate(raw)
            except ValueError:
                self._state.setdefault(
                    _STATE_LOAD_ERROR, "ValueError: persisted station backup schedule is invalid"
                )
        elif "schedule" in self._state:
            self._state.setdefault(
                _STATE_LOAD_ERROR, "ValueError: persisted station backup schedule is invalid"
            )
        return BackupSchedule(
            enabled=False,
            destination=str(self.destination),
            hour_utc=3,
            minute_utc=0,
            retention_count=14,
            selected_media_roots=[str(path) for path in self.media_roots],
        )

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schedule": self._schedule().model_dump(mode="json"),
                "scheduled_passphrase_configured": self.has_scheduled_passphrase(),
                "job": self._state.get("job", {"phase": "idle", "percent": 0}),
                "last_success": self._state.get("last_success"),
                "last_error": self._state.get(_STATE_LOAD_ERROR) or self._state.get("last_error"),
                "backups": [
                    asdict(item) | {"path": str(item.path)} for item in self.list_backups()
                ],
            }

    def start_worker(self, passphrase_provider: Callable[[], str] | None = None) -> None:
        """Start the daily scheduler; failed runs retry after 15 minutes, then one hour.

        The worker makes at most three attempts per UTC day. The passphrase
        provider must use OS-protected storage.
        """

        passphrase_provider = passphrase_provider or self._scheduled_passphrase_provider
        if passphrase_provider is None:
            raise RuntimeError("OS-protected scheduled passphrase provider is unavailable")
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker_stop.clear()

        def run() -> None:
            while not self._worker_stop.wait(30):
                schedule = self._schedule()
                now = datetime.now(UTC)
                today = now.date().isoformat()
                scheduled = now.hour > schedule.hour_utc or (
                    now.hour == schedule.hour_utc and now.minute >= schedule.minute_utc
                )
                if (
                    not schedule.enabled
                    or not scheduled
                    or self._state.get("last_success_day") == today
                ):
                    continue
                attempt_count = (
                    self._state.get("attempt_count", 0)
                    if self._state.get("last_attempt_day") == today
                    else 0
                )
                if not isinstance(attempt_count, int) or attempt_count < 0:
                    attempt_count = 0
                if attempt_count >= _MAX_SCHEDULED_ATTEMPTS:
                    continue
                retry_at = self._state.get("next_attempt_at")
                if self._state.get("last_attempt_day") == today and retry_at:
                    try:
                        retry_time = datetime.fromisoformat(str(retry_at))
                    except ValueError:
                        retry_time = now
                    if retry_time.tzinfo is None:
                        retry_time = retry_time.replace(tzinfo=UTC)
                    if now < retry_time:
                        continue
                attempt_count += 1
                with self._lock:
                    self._state["last_attempt_day"] = today
                    self._state["attempt_count"] = attempt_count
                    self._state["next_attempt_at"] = None
                    self._save_state()
                try:
                    self.create_backup(
                        passphrase_provider(), retention_count=schedule.retention_count
                    )
                except Exception as exc:
                    with self._lock:
                        self._state["last_error"] = _safe_error(exc)
                        if attempt_count < _MAX_SCHEDULED_ATTEMPTS:
                            delay = _SCHEDULED_RETRY_DELAYS_SECONDS[attempt_count - 1]
                            next_attempt = datetime.fromtimestamp(
                                datetime.now(UTC).timestamp() + delay, UTC
                            )
                            self._state["next_attempt_at"] = next_attempt.isoformat()
                        self._save_state()
                else:
                    with self._lock:
                        self._state["last_success_day"] = today
                        self._state["next_attempt_at"] = None
                        self._state["last_error"] = None
                        self._save_state()

        self._worker = threading.Thread(target=run, name="civiccast-backup", daemon=True)
        self._worker.start()

    def stop_worker(self, timeout: float = 5.0) -> None:
        self._worker_stop.set()
        if self._worker is not None:
            self._worker.join(timeout)
        self._worker = None

    def _prune(self, retention_count: int) -> None:
        _validate_retention(retention_count)
        artifacts = self.list_backups()
        for artifact in artifacts[retention_count:]:
            # Only delete our own exact extension/pattern inside the configured
            # destination, and only after the replacement archive is durable.
            if artifact.path.parent == self.destination and artifact.path.match(_ARCHIVE_GLOB):
                artifact.path.unlink(missing_ok=True)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _merge_paths(configured: Sequence[Path], dynamic: Sequence[Path]) -> tuple[Path, ...]:
    ordered: list[Path] = []
    seen: set[str] = set()
    for raw_path in (*configured, *dynamic):
        path = Path(raw_path).expanduser().absolute()
        key = str(path).casefold()
        if key not in seen:
            ordered.append(path)
            seen.add(key)
    return tuple(ordered)


def _validate_destination_media_overlap(
    destination: str | Path, media_roots: Sequence[str | Path]
) -> None:
    """Reject backup/media paths that overlap after resolving symlink components."""

    try:
        backup_path = Path(destination).expanduser().resolve()
        resolved_roots = [Path(root).expanduser().resolve() for root in media_roots]
    except (OSError, RuntimeError) as exc:
        raise ValueError("backup destination and media roots must resolve safely") from exc
    for media_root in resolved_roots:
        if (
            backup_path == media_root
            or backup_path in media_root.parents
            or media_root in backup_path.parents
        ):
            raise ValueError("backup destination must not overlap a selected media root")


def _protect_staging_directory(path: Path) -> None:
    """Restrict temporary plaintext bundles before writing secrets or media."""

    from civiccast.native.service_identity import restrict_data_directory

    restrict_data_directory(path)


def _safe_error(exc: Exception) -> str:
    """Store a diagnostic label without risking credentials in exception text."""

    return f"{type(exc).__name__}: station backup operation failed"


def _require_passphrase(passphrase: str) -> None:
    if not isinstance(passphrase, str) or len(passphrase) < 12:
        raise ValueError("recovery passphrase must contain at least 12 characters")


def _validate_retention(count: int) -> None:
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= _MAX_RETENTION:
        raise ValueError(f"retention must be between 1 and {_MAX_RETENTION} backups")


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_PBKDF2_ITERATIONS
    ).derive(passphrase.encode("utf-8"))


def _encrypt_file(source: Path, destination: Path, passphrase: str) -> None:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(_derive_key(passphrase, salt)), modes.GCM(nonce)).encryptor()
    with source.open("rb") as src, destination.open("wb") as dst:
        dst.write(_MAGIC + salt + nonce + (b"\0" * 16))
        for chunk in iter(lambda: src.read(_CHUNK_SIZE), b""):
            dst.write(encryptor.update(chunk))
        dst.write(encryptor.finalize())
        dst.seek(len(_MAGIC) + 16 + 12)
        dst.write(encryptor.tag)
        dst.flush()
        os.fsync(dst.fileno())


def _decrypt_file(source: Path, destination: Path, passphrase: str) -> None:
    with source.open("rb") as src:
        header = src.read(_HEADER_SIZE)
        if len(header) != _HEADER_SIZE or header[: len(_MAGIC)] != _MAGIC:
            raise ValueError("not a supported CivicCast backup archive")
        offset = len(_MAGIC)
        salt, nonce, tag = (
            header[offset : offset + 16],
            header[offset + 16 : offset + 28],
            header[-16:],
        )
        decryptor = Cipher(
            algorithms.AES(_derive_key(passphrase, salt)), modes.GCM(nonce, tag)
        ).decryptor()
        try:
            with destination.open("wb") as dst:
                for chunk in iter(lambda: src.read(_CHUNK_SIZE), b""):
                    dst.write(decryptor.update(chunk))
                dst.write(decryptor.finalize())
        except Exception:
            destination.unlink(missing_ok=True)
            raise ValueError("backup decryption or authentication failed") from None


def _write_zip(root: Path, output: Path) -> None:
    # Most station media is already compressed. Stored members avoid wasting
    # CPU; the outer AES-GCM pass remains streamed and keeps memory bounded.
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"backup source contains a symbolic link: {path}")
            if path.is_file():
                archive.write(path, path.relative_to(root).as_posix())


def _decrypt_and_extract(archive_path: Path, scratch: Path, passphrase: str) -> Path:
    _protect_staging_directory(scratch)
    zip_path = scratch / "bundle.zip"
    _decrypt_file(archive_path, zip_path, passphrase)
    bundle = scratch / "bundle"
    bundle.mkdir()
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                path = PurePosixPath(member.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or not path.parts
                    or "\\" in member.filename
                    or ":" in path.parts[0]
                ):
                    raise ValueError("backup contains an unsafe member path")
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("backup contains an unsupported symbolic link")
                target = bundle.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                if member.is_dir():
                    target.mkdir(exist_ok=True)
                    continue
                with archive.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=_CHUNK_SIZE)
    except Exception:
        shutil.rmtree(bundle, ignore_errors=True)
        raise
    return bundle


def _file_record(path: Path, kind: str, member: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "member": member,
        "size_bytes": path.stat().st_size,
    }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file(
    source: Path, destination: Path, progress: Callable[[int], None] | None = None
) -> int:
    copied = 0
    with source.open("rb") as src, destination.open("wb") as dst:
        while chunk := src.read(_CHUNK_SIZE):
            dst.write(chunk)
            copied += len(chunk)
            if progress is not None:
                progress(len(chunk))
        dst.flush()
        os.fsync(dst.fileno())
    shutil.copystat(source, destination)
    return copied


def _hash_members(bundle: Path) -> list[dict[str, Any]]:
    return [
        {"member": path.relative_to(bundle).as_posix(), "sha256": _hash_file(path)}
        for path in sorted(bundle.rglob("*"))
        if path.is_file()
    ]


def _read_station_manifest(bundle: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((bundle / "station-backup.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("backup station manifest is missing or invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("unsupported station backup manifest version")
    if not isinstance(manifest.get("members"), list) or not isinstance(
        manifest.get("payload"), list
    ):
        raise ValueError("backup station manifest has invalid member records")
    return manifest


def _safe_member(bundle: Path, member: str) -> Path:
    pure = PurePosixPath(member)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or not pure.parts
        or "\\" in member
        or ":" in pure.parts[0]
    ):
        raise ValueError("backup manifest contains an unsafe member path")
    path = bundle.joinpath(*pure.parts)
    if not path.is_file():
        raise ValueError(f"backup member is missing: {member}")
    return path


def _decode_portable_credentials(bundle: Path, member: object) -> dict[str, bytes] | None:
    """Validate and decode portable credentials before any restore mutation."""

    if member is None:
        return None
    if not isinstance(member, str):
        raise ValueError("backup credential member is invalid")
    try:
        raw = json.loads(_safe_member(bundle, member).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("backup credential export is missing or invalid") from exc
    if not isinstance(raw, dict):
        raise ValueError("backup credential export must be an object")
    decoded: dict[str, bytes] = {}
    for name, item in raw.items():
        if (
            not isinstance(name, str)
            or not name
            or "\0" in name
            or not isinstance(item, dict)
            or not isinstance(item.get("encoding"), str)
            or not isinstance(item.get("value"), str)
        ):
            raise ValueError("backup credential export contains an invalid entry")
        encoding = item["encoding"]
        value = item["value"]
        if encoding == "base64":
            try:
                decoded[name] = base64.b64decode(value, validate=True)
            except ValueError as exc:
                raise ValueError("backup credential export contains invalid base64") from exc
        elif encoding == "utf-8":
            decoded[name] = value.encode("utf-8")
        else:
            raise ValueError("backup credential export uses an unsupported encoding")
    return decoded


def _verify_members(bundle: Path, manifest: Mapping[str, Any]) -> int:
    records = manifest["members"]
    verified = 0
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("member"), str):
            raise ValueError("backup member record is invalid")
        path = _safe_member(bundle, record["member"])
        if _hash_file(path) != record.get("sha256"):
            raise ValueError(f"backup member checksum mismatch: {record['member']}")
        verified += 1
    return verified


def _restore_sqlite(artifact: Path, target_database_url: str) -> None:
    from sqlalchemy.engine import make_url

    from civiccast.db.url import normalize_database_url

    parsed_url = make_url(normalize_database_url(target_database_url))
    if not parsed_url.drivername.startswith("sqlite") or not parsed_url.database:
        raise ValueError("SQLite backup requires a sqlite:/// target database URL")
    target = Path(parsed_url.database).expanduser().absolute()
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError("SQLite restore target must be a regular file, not a link or directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.{uuid4().hex}.restore")
    try:
        with (
            closing(sqlite3.connect(_sqlite_readonly_uri(artifact), uri=True)) as source,
            closing(sqlite3.connect(staged)) as destination,
        ):
            source.backup(destination)
            check = destination.execute("PRAGMA integrity_check").fetchone()
            if check is None or check[0] != "ok":
                raise RuntimeError("restored SQLite database failed integrity_check")
        if target.exists():
            # The caller has stopped all writers. Keep a timestamped rollback
            # copy so a mistaken path choice is recoverable after the swap.
            rollback = target.with_name(
                f"{target.name}.pre-restore-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:6]}"
            )
            target.replace(rollback)
        staged.replace(target)
    finally:
        staged.unlink(missing_ok=True)


def _restore_postgres(artifact: Path, target_database_url: str, command: list[str] | None) -> None:
    if not target_database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise ValueError("PostgreSQL backup requires a PostgreSQL target database URL")
    # Replacement installs provision the current schema before data restore.
    # Drop archived objects that already exist, then replay the snapshot in a
    # single transaction so a failed replay leaves that provisioned target
    # intact. Source roles/ACLs do not travel across clusters.
    run_postgres_restore(
        artifact,
        target_database_url,
        pg_restore_command=command,
        preserve_ownership=False,
        single_transaction=True,
        no_acls=True,
        clean_if_exists=True,
    )


def _sqlite_readonly_uri(path: Path) -> str:
    from urllib.parse import quote

    return f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode=ro"


def _validate_payload_targets(
    records: Sequence[Mapping[str, Any]], target_paths: Mapping[str, Path]
) -> None:
    required = {
        str(record["source_path"])
        for record in records
        if isinstance(record.get("source_path"), str)
    }
    missing = sorted(required - target_paths.keys())
    if missing:
        raise ValueError(f"restore target mapping is required for: {', '.join(missing)}")
    for source_path in required:
        target_path = Path(target_paths[source_path]).expanduser()
        if not target_path.is_absolute():
            raise ValueError("restore target paths must be absolute")
    for record in records:
        if record.get("kind") == "media":
            _safe_media_relative_path(record.get("relative_path"))


def _safe_media_relative_path(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ValueError("backup media record contains an unsafe relative path")
    relative = PurePosixPath(value)
    windows_relative = PureWindowsPath(value)
    if (
        relative.is_absolute()
        or windows_relative.is_absolute()
        or windows_relative.drive
        or ".." in relative.parts
        or ".." in windows_relative.parts
        or not relative.parts
    ):
        raise ValueError("backup media record contains an unsafe relative path")
    return relative


def _media_path_mappings(
    records: Sequence[Mapping[str, Any]], target_paths: Mapping[str, Path]
) -> tuple[tuple[Path, Path], ...]:
    mappings: dict[str, tuple[Path, Path]] = {}
    for record in records:
        source = record.get("source_path")
        if record.get("kind") != "media-root" or not isinstance(source, str):
            continue
        source_path = Path(source).expanduser()
        target_path = Path(target_paths[source]).expanduser()
        if not source_path.is_absolute() or not target_path.is_absolute():
            raise ValueError("media restore source and target paths must be absolute")
        mappings[str(source_path).casefold()] = (source_path, target_path)
    return tuple(
        sorted(
            (item for item in mappings.values() if item[0] != item[1]),
            key=lambda item: len(item[0].parts),
            reverse=True,
        )
    )


def _mapped_media_path(value: object, mappings: Sequence[tuple[Path, Path]]) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        return None
    for source, target in mappings:
        try:
            relative = candidate.relative_to(source)
        except ValueError:
            continue
        mapped = target if relative == Path() else target / relative
        return str(mapped)
    return None


def _remap_station_state_payload(
    bundle: Path,
    records: Sequence[Mapping[str, Any]],
    mappings: Sequence[tuple[Path, Path]],
) -> None:
    if not mappings:
        return
    for record in records:
        source_path = record.get("source_path")
        member = record.get("member")
        if (
            record.get("kind") != "config"
            or not isinstance(source_path, str)
            or Path(source_path).name.casefold() != "station-state.json"
            or not isinstance(member, str)
        ):
            continue
        staged = _safe_member(bundle, member)
        try:
            state = json.loads(staged.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("station-state backup is not valid JSON") from exc
        station = state.get("station") if isinstance(state, dict) else None
        locations = station.get("storage_locations") if isinstance(station, dict) else None
        if not isinstance(locations, dict):
            continue
        changed = False
        for field in ("media_library", "recordings", "backups"):
            mapped = _mapped_media_path(locations.get(field), mappings)
            if mapped is not None and mapped != locations[field]:
                locations[field] = mapped
                changed = True
        if changed:
            staged.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _remap_database_media_paths(database_url: str, mappings: Sequence[tuple[Path, Path]]) -> int:
    """Rewrite the product's known media-path references after restoring its DB."""

    if not mappings:
        return 0
    from sqlalchemy import (
        MetaData,
        Table,
        and_,
        bindparam,
        create_engine,
        inspect,
        or_,
        select,
        update,
    )
    from sqlalchemy.sql.elements import BindParameter

    from civiccast.db.session import connect_options
    from civiccast.db.url import normalize_database_url

    normalized_database_url = normalize_database_url(database_url)
    engine = create_engine(
        normalized_database_url,
        future=True,
        **connect_options(normalized_database_url),
    )
    total_updated = 0
    try:
        schema = "civiccast" if engine.dialect.name == "postgresql" else None
        known_path_columns = {
            "assets": ("file_path", "thumbnail_path"),
            "watch_folder_configs": ("monitor_path",),
            "watch_folder_file_state": ("file_path",),
        }
        with engine.begin() as connection:
            inspector = inspect(connection)
            for table_name, candidate_columns in known_path_columns.items():
                if not inspector.has_table(table_name, schema=schema):
                    continue
                table = Table(table_name, MetaData(), schema=schema, autoload_with=connection)
                path_columns = [table.c[name] for name in candidate_columns if name in table.c]
                primary_key = list(table.primary_key.columns)
                if not path_columns or not primary_key:
                    continue
                rows = connection.execute(
                    select(*primary_key, *path_columns).where(
                        or_(*(column.is_not(None) for column in path_columns))
                    )
                ).mappings()
                key_parameters: list[BindParameter[Any]] = [
                    bindparam(f"_station_backup_pk_{index}") for index in range(len(primary_key))
                ]
                batches: dict[tuple[str, ...], list[dict[str, Any]]] = {}
                statements: dict[tuple[str, ...], Any] = {}
                for row in rows:
                    changes = {
                        column: mapped
                        for column in path_columns
                        if (mapped := _mapped_media_path(row[column.name], mappings)) is not None
                        and mapped != row[column.name]
                    }
                    if not changes:
                        continue
                    changed_columns = tuple(
                        column.name for column in path_columns if column in changes
                    )
                    update_statement = statements.get(changed_columns)
                    if update_statement is None:
                        update_statement = (
                            update(table)
                            .where(
                                and_(
                                    *(
                                        column == parameter
                                        for column, parameter in zip(
                                            primary_key, key_parameters, strict=True
                                        )
                                    )
                                )
                            )
                            .values(
                                {
                                    table.c[name]: bindparam(f"_station_backup_{name}")
                                    for name in changed_columns
                                }
                            )
                        )
                        statements[changed_columns] = update_statement
                    parameters = {
                        f"_station_backup_pk_{index}": row[column.name]
                        for index, column in enumerate(primary_key)
                    }
                    parameters.update(
                        {
                            f"_station_backup_{column.name}": value
                            for column, value in changes.items()
                        }
                    )
                    batch = batches.setdefault(changed_columns, [])
                    batch.append(parameters)
                    if len(batch) >= 500:
                        connection.execute(update_statement, batch)
                        total_updated += len(batch)
                        batch.clear()
                for changed_columns, batch in batches.items():
                    if batch:
                        connection.execute(statements[changed_columns], batch)
                        total_updated += len(batch)
    finally:
        engine.dispose()
    return total_updated


def _restore_payload(
    bundle: Path,
    records: Sequence[Mapping[str, Any]],
    target_paths: Mapping[str, Path],
    *,
    progress: ProgressCallback | None = None,
) -> int:
    restored = 0
    media_roots: set[str] = set()
    file_records = [
        record
        for record in records
        if record.get("kind") not in {"credential-export", "media-root"}
    ]
    total_bytes = sum(int(record.get("size_bytes", 0)) for record in file_records)
    copied_bytes = 0
    for record in file_records:
        kind = record.get("kind")
        source_path = record.get("source_path")
        if not isinstance(source_path, str):
            raise ValueError("backup payload is missing its source path mapping key")
        target_base = target_paths.get(source_path)
        if target_base is None:
            raise ValueError(f"restore target mapping is required for {source_path}")
        member = record.get("member")
        if not isinstance(member, str):
            raise ValueError("backup payload member is invalid")
        source = _safe_member(bundle, member)
        if kind == "media":
            relative = _safe_media_relative_path(record.get("relative_path"))
            target = Path(target_base).joinpath(*relative.parts)
        else:
            target = Path(target_base)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.restore")

        def report_copy(copied: int) -> None:
            nonlocal copied_bytes
            copied_bytes += copied
            if progress is not None:
                fraction = copied_bytes / total_bytes if total_bytes else 1
                progress("restoring-files", 55 + round(fraction * 40))

        _copy_file(source, temporary, report_copy)
        temporary.replace(target)
        restored += 1
    for record in records:
        if record.get("kind") == "media-root" and isinstance(record.get("source_path"), str):
            media_roots.add(str(record["source_path"]))
    for media_root_source in media_roots:
        Path(target_paths[media_root_source]).mkdir(parents=True, exist_ok=True)
    return restored
