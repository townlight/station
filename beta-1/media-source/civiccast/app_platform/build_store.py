# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""File-backed store for OTT app build records + store submissions (S12 / step 8).

Auxiliary persistence for :mod:`civiccast.app_platform.build_models` — separate
from the core app-platform config (no schema migration), mirroring
:class:`~civiccast.app_platform.store.AppPlatformConfigStore`'s in-process,
thread-safe, atomically-persisted JSON pattern. Build records are append-only
(an immutable audit log); store submissions are upserted per app target.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from threading import Lock

from civiccast.app_platform.build_models import AppBuildRecord, StoreSubmissionMetadata
from civiccast.installer.storage import default_storage_dir

_STORE_FILE_NAME = "app-builds.json"

__all__ = ["AppBuildStore", "AppBuildStoreError", "default_app_build_store_path"]


class AppBuildStoreError(RuntimeError):
    """Raised when the app-build store cannot be loaded or saved."""


class AppBuildStore:
    """In-process append-only build log + per-target submission tracker."""

    def __init__(self, store_path: Path | None = None) -> None:
        self._lock = Lock()
        self._store_path = store_path
        self._builds: dict[str, AppBuildRecord] = {}
        self._submissions: dict[str, StoreSubmissionMetadata] = {}
        self._load()

    # -- build records (append-only) --------------------------------------

    def add_build(self, record: AppBuildRecord) -> AppBuildRecord:
        with self._lock:
            if record.record_id in self._builds:
                raise AppBuildStoreError(f"build record {record.record_id!r} already exists")
            self._builds[record.record_id] = record
            self._persist_locked()
            return record

    def get_build(self, record_id: str) -> AppBuildRecord | None:
        with self._lock:
            record = self._builds.get(record_id)
            return record.model_copy(deep=True) if record is not None else None

    def list_builds(self, *, app_target: str | None = None) -> list[AppBuildRecord]:
        with self._lock:
            records = list(self._builds.values())
        if app_target is not None:
            records = [r for r in records if r.app_target == app_target]
        # Newest first; record_id breaks ties deterministically.
        records.sort(key=lambda r: (r.built_at, r.record_id), reverse=True)
        return [r.model_copy(deep=True) for r in records]

    # -- store submissions (upsert per target) ----------------------------

    def upsert_submission(self, submission: StoreSubmissionMetadata) -> StoreSubmissionMetadata:
        with self._lock:
            self._submissions[submission.app_target] = submission
            self._persist_locked()
            return submission.model_copy(deep=True)

    def get_submission(self, app_target: str) -> StoreSubmissionMetadata | None:
        with self._lock:
            submission = self._submissions.get(app_target)
            return submission.model_copy(deep=True) if submission is not None else None

    def list_submissions(self) -> list[StoreSubmissionMetadata]:
        with self._lock:
            submissions = list(self._submissions.values())
        submissions.sort(key=lambda s: s.app_target)
        return [s.model_copy(deep=True) for s in submissions]

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if self._store_path is None or not self._store_path.exists():
            return
        try:
            payload = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AppBuildStoreError(f"Could not read app-build store: {exc}") from exc
        for raw in payload.get("builds", []):
            record = AppBuildRecord.model_validate(raw)
            self._builds[record.record_id] = record
        for raw in payload.get("submissions", []):
            submission = StoreSubmissionMetadata.model_validate(raw)
            self._submissions[submission.app_target] = submission

    def _persist_locked(self) -> None:
        if self._store_path is None:
            return
        payload = {
            "builds": [r.model_dump(mode="json") for r in self._builds.values()],
            "submissions": [s.model_dump(mode="json") for s in self._submissions.values()],
        }
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._store_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            if os.name != "nt":
                tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            tmp_path.replace(self._store_path)
            if os.name != "nt":
                self._store_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            raise AppBuildStoreError(f"Could not write app-build store: {exc}") from exc


def default_app_build_store_path() -> Path | None:
    configured = os.environ.get("CIVICCAST_APP_BUILD_STORE_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.environ.get("CIVICCAST_ALLOW_EPHEMERAL_STORES") == "1":
        return None
    return (default_storage_dir() / _STORE_FILE_NAME).expanduser().resolve()
