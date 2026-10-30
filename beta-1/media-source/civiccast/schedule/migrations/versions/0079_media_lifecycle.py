# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S7 media lifecycle & readiness -- the five net-new S7 tables + archival gate.

Revision ID: 0079_media_lifecycle
Revises: 0078_agenda_item_confidence
Create Date: 2026-08-21

``docs/spec/3.0/sections/S7-media-lifecycle-and-readiness.md`` §3 calls for
"a single migration `0041_media_lifecycle`" creating all five S7 tables in
one revision. `0041` was already claimed by `0041_commit_rollback_fields`
by the time this landed (revision numbers are repo-global, per the
`0075_offline_caption_jobs` note) -- this is that migration, parented on
the current single head instead. Renumbered `0076` -> `0077` (and
re-chained onto `0076_analytics_viewership`) when S14's migration reached
`main` first and claimed `0076_media_lifecycle`'s original slot. Re-chained
a second time onto `0078_agenda_item_confidence` (PR #21) when that
migration also merged to `main` ahead of this branch and independently
claimed the `0077` numeric slot's chain position; the `0079_media_lifecycle`
name is kept, only `down_revision` moved.

Creates, in one revision:

* ``media_ingest_jobs``        -- durable async-ingest job record
* ``transcode_jobs``           -- durable ingest-time transcode job record
* ``asset_readiness``          -- denormalized readiness badge (1 row/asset)
* ``watch_folder_configs``     -- operator-configured auto-ingest directories
* ``asset_retention_policies`` -- retention automation rules
* ``asset_archive_proofs``     -- durable, verified ``ArchiveProof`` rows
  (closes CLAUDE.md §4.6: nothing was persisting these before)
* ``media_lifecycle_audit_log``-- append-only worker action trail

...and adds two columns to the existing ``assets`` table:

* ``legal_hold`` (boolean, default false)
* ``legal_hold_reason`` (text, nullable)

See ``civiccast/schedule/media_lifecycle_models.py`` for the SQLAlchemy
models these tables back and the full rationale.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0079_media_lifecycle"
down_revision: str | None = "0078_agenda_item_confidence"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def _schema() -> str | None:
    return "civiccast" if _use_schema() else None


def upgrade() -> None:
    schema = _schema()

    op.add_column(
        "assets",
        sa.Column("legal_hold", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema=schema,
    )
    op.add_column(
        "assets",
        sa.Column("legal_hold_reason", sa.Text(), nullable=True),
        schema=schema,
    )

    op.create_table(
        "media_ingest_jobs",
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("source_kind", sa.String(length=20), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("job_id", name="media_ingest_jobs_pkey"),
        sa.CheckConstraint(
            "source_kind IN ('http_upload', 'watch_folder', 'live_finalization')",
            name="media_ingest_jobs_source_kind_check",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="media_ingest_jobs_status_check",
        ),
        sa.CheckConstraint(
            "progress_percent >= 0 AND progress_percent <= 100",
            name="media_ingest_jobs_progress_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_media_ingest_jobs_asset_id", "media_ingest_jobs", ["asset_id"], schema=schema
    )
    op.create_index("ix_media_ingest_jobs_status", "media_ingest_jobs", ["status"], schema=schema)

    op.create_table(
        "transcode_jobs",
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("output_format", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_path", sa.Text(), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("job_id", name="transcode_jobs_pkey"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="transcode_jobs_status_check",
        ),
        sa.CheckConstraint(
            "progress_percent >= 0 AND progress_percent <= 100",
            name="transcode_jobs_progress_check",
        ),
        schema=schema,
    )
    op.create_index("ix_transcode_jobs_asset_id", "transcode_jobs", ["asset_id"], schema=schema)
    op.create_index("ix_transcode_jobs_status", "transcode_jobs", ["status"], schema=schema)

    op.create_table(
        "asset_readiness",
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column(
            "readiness_state", sa.String(length=20), nullable=False, server_default="not_ready"
        ),
        sa.Column("readiness_reason", sa.Text(), nullable=True),
        sa.Column("loudness_status", sa.String(length=20), nullable=True),
        sa.Column("measured_lufs", sa.Numeric(6, 2), nullable=True),
        sa.Column("archive_portal_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archive_ia_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archive_nas_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archive_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("asset_id", name="asset_readiness_pkey"),
        sa.CheckConstraint(
            "readiness_state IN ('not_ready', 'pending_transcode', 'transcoding', "
            "'ready', 'missing_file', 'rejected')",
            name="asset_readiness_state_check",
        ),
        sa.CheckConstraint(
            "loudness_status IS NULL OR loudness_status IN ('ok', 'failed', 'not_checked')",
            name="asset_readiness_loudness_status_check",
        ),
        schema=schema,
    )

    op.create_table(
        "watch_folder_configs",
        sa.Column("config_id", sa.String(length=64), nullable=False),
        sa.Column("monitor_path", sa.Text(), nullable=False),
        sa.Column("import_naming_pattern", sa.String(length=200), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("settle_window_seconds", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("retention_policy_default", sa.String(length=16), nullable=True),
        sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scan_files_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("config_id", name="watch_folder_configs_pkey"),
        sa.CheckConstraint(
            "settle_window_seconds >= 1", name="watch_folder_configs_settle_window_check"
        ),
        sa.CheckConstraint(
            "retention_policy_default IS NULL OR retention_policy_default IN "
            "('default', 'permanent', 'meeting', 'short')",
            name="watch_folder_configs_retention_check",
        ),
        schema=schema,
    )

    op.create_table(
        "asset_retention_policies",
        sa.Column("policy_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("match_meeting_body", sa.String(length=120), nullable=True),
        sa.Column("retention_policy", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("policy_id", name="asset_retention_policies_pkey"),
        sa.CheckConstraint(
            "retention_policy IN ('default', 'permanent', 'meeting', 'short')",
            name="asset_retention_policies_policy_check",
        ),
        schema=schema,
    )

    op.create_table(
        "asset_archive_proofs",
        sa.Column("proof_id", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=40), nullable=False),
        sa.Column("target_url_or_path", sa.Text(), nullable=False),
        sa.Column("verification_hash", sa.String(length=71), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("proof_id", name="asset_archive_proofs_pkey"),
        sa.CheckConstraint(
            "target_type IN ('internet_archive', 'local_nas_rsync', 'local_nas_zfs', "
            "'local_nas_copy', 'local_nas_snapshot_copy')",
            name="asset_archive_proofs_target_type_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_asset_archive_proofs_asset_id", "asset_archive_proofs", ["asset_id"], schema=schema
    )

    op.create_table(
        "media_lifecycle_audit_log",
        sa.Column("entry_id", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=60), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("entry_id", name="media_lifecycle_audit_log_pkey"),
        schema=schema,
    )
    op.create_index(
        "ix_media_lifecycle_audit_log_asset_id",
        "media_lifecycle_audit_log",
        ["asset_id"],
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_index(
        "ix_media_lifecycle_audit_log_asset_id",
        table_name="media_lifecycle_audit_log",
        schema=schema,
    )
    op.drop_table("media_lifecycle_audit_log", schema=schema)
    op.drop_index(
        "ix_asset_archive_proofs_asset_id", table_name="asset_archive_proofs", schema=schema
    )
    op.drop_table("asset_archive_proofs", schema=schema)
    op.drop_table("asset_retention_policies", schema=schema)
    op.drop_table("watch_folder_configs", schema=schema)
    op.drop_table("asset_readiness", schema=schema)
    op.drop_index("ix_transcode_jobs_status", table_name="transcode_jobs", schema=schema)
    op.drop_index("ix_transcode_jobs_asset_id", table_name="transcode_jobs", schema=schema)
    op.drop_table("transcode_jobs", schema=schema)
    op.drop_index("ix_media_ingest_jobs_status", table_name="media_ingest_jobs", schema=schema)
    op.drop_index("ix_media_ingest_jobs_asset_id", table_name="media_ingest_jobs", schema=schema)
    op.drop_table("media_ingest_jobs", schema=schema)
    op.drop_column("assets", "legal_hold_reason", schema=schema)
    op.drop_column("assets", "legal_hold", schema=schema)
