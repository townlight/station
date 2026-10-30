# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Typed report shapes for the disaster-recovery drills."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TableSnapshot(BaseModel):
    """A per-table fingerprint: row count + a deterministic content checksum.

    The checksum is sha256 over every row (as its column values, in PK order)
    concatenated with the row separator. Two databases with the same table
    schema produce the same checksum iff every row matches exactly.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    row_count: int = Field(ge=0)
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MediaManifestEntry(BaseModel):
    """One media file's bounded-sample fingerprint (not a full-file hash)."""

    model_config = ConfigDict(extra="forbid")

    path: str
    size_bytes: int = Field(ge=0)
    sampled_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sample_bytes: int = Field(ge=0, description="Bytes actually hashed (head + tail, bounded).")


class IntegrityManifestEntry(BaseModel):
    """sha256 of one file that shipped inside the backup set."""

    model_config = ConfigDict(extra="forbid")

    member: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BackupManifest(BaseModel):
    """The manifest recorded at backup time; the restore drill's baseline."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    backup_id: str
    created_at: datetime
    engine: str  # "sqlite" | "postgres"
    db_artifact: str  # filename of the db snapshot/dump inside the backup dir
    tables: list[TableSnapshot] = Field(default_factory=list)
    media_entries: list[MediaManifestEntry] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)
    integrity: list[IntegrityManifestEntry] = Field(default_factory=list)
    globals_artifact: str | None = Field(
        default=None,
        description=(
            "Filename of the pg_dumpall --globals-only capture (Postgres only). "
            "pg_dump never captures cluster-global roles; None on SQLite backups "
            "and on any Postgres backup taken before this field existed."
        ),
    )


class RestoreTableResult(BaseModel):
    """One table's expected-vs-actual comparison from a restore drill.

    ``expected_*`` is ``None`` for a table that showed up in the restored
    copy but was never in the backup manifest -- an unexpected extra table
    is exactly as real a piece of drift as a missing one, and this shape
    lets :func:`civiccast.dr.restore_drill._table_results` report it without
    inventing a fake "expected" value.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    expected_row_count: int | None
    actual_row_count: int | None
    expected_checksum: str | None
    actual_checksum: str | None
    matched: bool


class RestoreDrillReport(BaseModel):
    """Result of restoring a backup set into a completely fresh database."""

    model_config = ConfigDict(extra="forbid")

    backup_id: str
    started_at: datetime
    finished_at: datetime
    schema_ok: bool
    db_revision: str | None
    expected_head: str | None
    tables: list[RestoreTableResult] = Field(default_factory=list)
    app_store_reads: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Did this drill PROVE the restore is faithful?

        <installer-path-audit MA-11> ``all(t.matched for t in [])`` is True,
        so a drill that compared ZERO tables reported ``ok=True`` -- and
        ``_table_results`` returns ``[]`` whenever both sides are empty, which
        every Postgres cross-check can produce because they all hardcode
        ``schema="civiccast"``. ``report.py`` then printed "confirmed every row
        came back exactly as it was" and ``installer/service.py`` summarised
        "0 tables verified, schema_ok=True". An empty comparison is a failure
        to observe, not an observation of correctness, and this gate is what
        the pre-upgrade backup is allowed to proceed on.
        """

        if not self.tables:
            return False
        return self.schema_ok and not self.errors and all(t.matched for t in self.tables)


class CrashDrillResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    ok: bool
    detail: str
    duration_seconds: float = Field(ge=0)


class CrashDrillReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[CrashDrillResult] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)


class DrillReport(BaseModel):
    """The full disaster-recovery drill report: backup + restore + crash."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    backup: BackupManifest
    restore: RestoreDrillReport
    crash: CrashDrillReport
    honest_notes: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.restore.ok and self.crash.ok
