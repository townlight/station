# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Postgres-backed store for S7 media lifecycle endpoints.

Session-factory posture mirrors :class:`civiccast.schedule.store.PostgresAssetStore`
-- no I/O at construction, one short-lived session per call.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from civiccast.schedule.media_lifecycle_models import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    READINESS_MISSING_FILE,
    READINESS_PENDING_TRANSCODE,
    READINESS_READY,
    READINESS_REJECTED,
    READINESS_TRANSCODING,
    AssetReadiness,
    AssetReadinessResponse,
    AssetRetentionPolicy,
    AssetRetentionPolicyInput,
    AssetRetentionPolicyResponse,
    InFlightTranscodeJob,
    LifecycleAuditEntryResponse,
    MediaIngestJob,
    MediaLifecycleAuditEntry,
    ReadinessDashboardResponse,
    ReadinessDashboardRow,
    StorageBudgetResponse,
    StorageBudgetTierRow,
    TranscodeJob,
    WatchFolderConfig,
    WatchFolderConfigInput,
    WatchFolderConfigResponse,
)
from civiccast.schedule.models import Asset

SessionFactory = Callable[[], AbstractContextManager[Session]]


class AssetNotFoundError(KeyError):
    """Raised when an operation targets an asset_id that does not exist."""

    def __init__(self, asset_id: str) -> None:
        super().__init__(asset_id)
        self.asset_id = asset_id


class WatchFolderConfigNotFoundError(KeyError):
    def __init__(self, config_id: str) -> None:
        super().__init__(config_id)
        self.config_id = config_id


class AssetRetentionPolicyNotFoundError(KeyError):
    def __init__(self, policy_id: str) -> None:
        super().__init__(policy_id)
        self.policy_id = policy_id


def _in_flight_jobs(session: Session, asset_id: str) -> list[InFlightTranscodeJob]:
    jobs = session.execute(
        select(TranscodeJob).where(
            TranscodeJob.asset_id == asset_id,
            TranscodeJob.status.in_((JOB_STATUS_PENDING, JOB_STATUS_RUNNING)),
        )
    ).scalars()
    return [
        InFlightTranscodeJob(
            job_id=j.job_id,
            output_format=j.output_format,
            progress_percent=j.progress_percent,
            estimated_remaining_secs=None,
        )
        for j in jobs
    ]


def _live_readiness(state: str, reason: str | None, active_jobs: int) -> tuple[str, str | None]:
    # The worker snapshot can predate dispatch/completion in the same pass.
    # An empty current queue cannot truthfully be reported as queued/running.
    if state in (READINESS_PENDING_TRANSCODE, READINESS_TRANSCODING) and not active_jobs:
        return READINESS_READY, None
    return state, reason


def _to_readiness_response(
    row: AssetReadiness, jobs: list[InFlightTranscodeJob]
) -> AssetReadinessResponse:
    state, reason = _live_readiness(row.readiness_state, row.readiness_reason, len(jobs))
    return AssetReadinessResponse(
        asset_id=row.asset_id,
        readiness_state=state,  # type: ignore[arg-type]
        readiness_reason=reason,
        loudness_status=row.loudness_status,  # type: ignore[arg-type]
        measured_lufs=row.measured_lufs,
        in_flight_transcode_jobs=jobs,
        archive_complete=row.archive_complete,
        archive_portal_verified=row.archive_portal_verified_at is not None,
        archive_ia_verified=row.archive_ia_verified_at is not None,
        archive_nas_verified=row.archive_nas_verified_at is not None,
        legal_hold=False,  # overwritten by caller once the Asset row is joined
        updated_at=row.updated_at,
    )


class MediaLifecycleStore:
    """Query/CRUD surface backing ``civiccast.schedule.media_lifecycle_router``."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    # -- Readiness ---------------------------------------------------------

    def get_readiness(self, asset_id: str) -> AssetReadinessResponse | None:
        with self._session_factory() as session:
            asset = session.get(Asset, asset_id)
            if asset is None:
                return None
            row = session.get(AssetReadiness, asset_id)
            if row is None:
                # No worker pass has touched this asset yet -- report the
                # honest "not computed" state rather than 404ing an asset
                # that genuinely exists (spec: readiness_state includes
                # not_ready for exactly this case).
                return AssetReadinessResponse(
                    asset_id=asset_id,
                    readiness_state="not_ready",
                    readiness_reason="Readiness has not been computed yet; the lifecycle worker "
                    "runs on its normal poll interval.",
                    loudness_status=None,
                    measured_lufs=None,
                    in_flight_transcode_jobs=[],
                    archive_complete=False,
                    archive_portal_verified=False,
                    archive_ia_verified=False,
                    archive_nas_verified=False,
                    legal_hold=asset.legal_hold,
                    updated_at=datetime.now(UTC),
                )
            response = _to_readiness_response(row, _in_flight_jobs(session, asset_id))
            return response.model_copy(update={"legal_hold": asset.legal_hold})

    def dashboard(self) -> ReadinessDashboardResponse:
        with self._session_factory() as session:
            assets = list(session.execute(select(Asset).order_by(Asset.asset_id.asc())).scalars())
            readiness_by_id = {
                row.asset_id: row for row in session.execute(select(AssetReadiness)).scalars()
            }
            in_flight_counts: dict[str, int] = {}
            for job in session.execute(
                select(TranscodeJob).where(
                    TranscodeJob.status.in_((JOB_STATUS_PENDING, JOB_STATUS_RUNNING))
                )
            ).scalars():
                in_flight_counts[job.asset_id] = in_flight_counts.get(job.asset_id, 0) + 1

            by_asset: list[ReadinessDashboardRow] = []
            ready = transcoding = missing = rejected = 0
            for asset in assets:
                row = readiness_by_id.get(asset.asset_id)
                state = row.readiness_state if row is not None else "not_ready"
                # Mirror get()'s honest "not computed" reason: a dashboard row
                # with no AssetReadiness record used to ship reason=None, so
                # the operator UI showed a bare "Not ready" dot with no
                # explanation (candidate #17 field finding, second report).
                reason = (
                    row.readiness_reason
                    if row is not None
                    else "Readiness has not been computed yet; the lifecycle worker "
                    "runs on its normal poll interval."
                )
                state, reason = _live_readiness(
                    state, reason, in_flight_counts.get(asset.asset_id, 0)
                )
                by_asset.append(
                    ReadinessDashboardRow(
                        asset_id=asset.asset_id,
                        title=asset.title,
                        readiness_state=state,  # type: ignore[arg-type]
                        readiness_reason=reason,
                        in_flight_jobs_count=in_flight_counts.get(asset.asset_id, 0),
                    )
                )
                if state == READINESS_READY:
                    ready += 1
                elif state == READINESS_TRANSCODING:
                    transcoding += 1
                elif state == READINESS_MISSING_FILE:
                    missing += 1
                elif state == READINESS_REJECTED:
                    rejected += 1
            return ReadinessDashboardResponse(
                total_assets=len(assets),
                ready_count=ready,
                transcoding_count=transcoding,
                missing_count=missing,
                rejected_count=rejected,
                by_asset=by_asset,
            )

    # -- Legal hold ----------------------------------------------------------

    def set_legal_hold(self, asset_id: str, *, hold: bool, reason: str | None) -> None:
        with self._session_factory() as session:
            asset = session.get(Asset, asset_id)
            if asset is None:
                raise AssetNotFoundError(asset_id)
            asset.legal_hold = hold
            asset.legal_hold_reason = reason if hold else None
            session.add(
                MediaLifecycleAuditEntry(
                    asset_id=asset_id,
                    action="legal_hold_set" if hold else "legal_hold_cleared",
                    detail=reason or "",
                    dry_run=False,
                )
            )
            session.commit()

    # -- Watch-folder configs -------------------------------------------------

    def list_watch_folder_configs(self) -> list[WatchFolderConfigResponse]:
        with self._session_factory() as session:
            rows = session.execute(
                select(WatchFolderConfig).order_by(WatchFolderConfig.created_at.asc())
            ).scalars()
            return [WatchFolderConfigResponse.model_validate(r) for r in rows]

    def create_watch_folder_config(
        self, payload: WatchFolderConfigInput
    ) -> WatchFolderConfigResponse:
        with self._session_factory() as session:
            row = WatchFolderConfig(**payload.model_dump())
            session.add(row)
            session.flush()
            session.refresh(row)
            response = WatchFolderConfigResponse.model_validate(row)
            session.commit()
            return response

    def update_watch_folder_config(
        self, config_id: str, payload: WatchFolderConfigInput
    ) -> WatchFolderConfigResponse:
        with self._session_factory() as session:
            row = session.get(WatchFolderConfig, config_id)
            if row is None:
                raise WatchFolderConfigNotFoundError(config_id)
            for key, value in payload.model_dump().items():
                setattr(row, key, value)
            row.updated_at = datetime.now(UTC)
            session.flush()
            session.refresh(row)
            response = WatchFolderConfigResponse.model_validate(row)
            session.commit()
            return response

    def delete_watch_folder_config(self, config_id: str) -> None:
        with self._session_factory() as session:
            row = session.get(WatchFolderConfig, config_id)
            if row is None:
                raise WatchFolderConfigNotFoundError(config_id)
            session.delete(row)
            session.commit()

    # -- Retention policies ----------------------------------------------------

    def list_retention_policies(self) -> list[AssetRetentionPolicyResponse]:
        with self._session_factory() as session:
            rows = session.execute(
                select(AssetRetentionPolicy).order_by(
                    AssetRetentionPolicy.priority.desc(), AssetRetentionPolicy.created_at.asc()
                )
            ).scalars()
            return [AssetRetentionPolicyResponse.model_validate(r) for r in rows]

    def create_retention_policy(
        self, payload: AssetRetentionPolicyInput
    ) -> AssetRetentionPolicyResponse:
        with self._session_factory() as session:
            row = AssetRetentionPolicy(**payload.model_dump())
            session.add(row)
            session.flush()
            session.refresh(row)
            response = AssetRetentionPolicyResponse.model_validate(row)
            session.commit()
            return response

    def update_retention_policy(
        self, policy_id: str, payload: AssetRetentionPolicyInput
    ) -> AssetRetentionPolicyResponse:
        with self._session_factory() as session:
            row = session.get(AssetRetentionPolicy, policy_id)
            if row is None:
                raise AssetRetentionPolicyNotFoundError(policy_id)
            for key, value in payload.model_dump().items():
                setattr(row, key, value)
            row.updated_at = datetime.now(UTC)
            session.flush()
            session.refresh(row)
            response = AssetRetentionPolicyResponse.model_validate(row)
            session.commit()
            return response

    def delete_retention_policy(self, policy_id: str) -> None:
        with self._session_factory() as session:
            row = session.get(AssetRetentionPolicy, policy_id)
            if row is None:
                raise AssetRetentionPolicyNotFoundError(policy_id)
            session.delete(row)
            session.commit()

    def apply_retention_policies(self) -> int:
        """Apply enabled automation rules (highest ``priority`` wins) to every asset.

        Returns the number of assets whose ``retention_policy`` changed.
        Called by the lifecycle worker's periodic pass and available
        standalone for the "apply now" operator action.
        """

        changed = 0
        with self._session_factory() as session:
            rules = list(
                session.execute(
                    select(AssetRetentionPolicy)
                    .where(AssetRetentionPolicy.enabled.is_(True))
                    .order_by(AssetRetentionPolicy.priority.desc())
                ).scalars()
            )
            if not rules:
                return 0
            assets = list(session.execute(select(Asset)).scalars())
            for asset in assets:
                for rule in rules:
                    if rule.match_meeting_body and rule.match_meeting_body == asset.meeting_body:
                        if asset.retention_policy != rule.retention_policy:
                            asset.retention_policy = rule.retention_policy
                            changed += 1
                            session.add(
                                MediaLifecycleAuditEntry(
                                    asset_id=asset.asset_id,
                                    action="retention_policy_applied",
                                    detail=f"rule={rule.name!r} -> retention_policy="
                                    f"{rule.retention_policy!r}",
                                    dry_run=False,
                                )
                            )
                        break
            session.commit()
        return changed

    # -- Storage budget --------------------------------------------------------

    def storage_budget(self, *, budget_bytes: int | None) -> StorageBudgetResponse:
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    Asset.retention_policy,
                    func.count(Asset.asset_id),
                    func.coalesce(func.sum(Asset.file_size_bytes), 0),
                ).group_by(Asset.retention_policy)
            ).all()
            by_policy = [
                StorageBudgetTierRow(
                    retention_policy=policy, asset_count=count, bytes_used=int(total)
                )
                for policy, count, total in rows
            ]
            total_bytes = sum(r.bytes_used for r in by_policy)
            percent = (total_bytes / budget_bytes * 100.0) if budget_bytes else None
            return StorageBudgetResponse(
                total_bytes_used=total_bytes,
                budget_bytes=budget_bytes,
                percent_used=percent,
                by_retention_policy=by_policy,
            )

    # -- Replace-source ----------------------------------------------------

    def get_asset_source_path(self, asset_id: str) -> str | None:
        """Read the primary source path before the router archives old files."""
        with self._session_factory() as session:
            asset = session.get(Asset, asset_id)
            return asset.file_path if asset is not None else None

    def apply_replace_source(
        self,
        asset_id: str,
        *,
        new_file_path: str,
        file_size_bytes: int,
        codec_video: str | None,
        codec_audio: str | None,
        width_px: int | None,
        height_px: int | None,
        bitrate_bps: int | None,
        format_name: str | None,
        duration_seconds: int | None,
        content_hash: str | None,
        thumbnail_path: str | None,
        archived_old_path: str | None,
        source_kind: str = "http_upload",
    ) -> str:
        """Persist a replace-source outcome: point the asset at the new file.

        The caller (the router, or the watch-folder daemon's reprocess-on-
        change path) has already validated the new file via ffprobe and
        placed it at ``new_file_path`` -- this method is DB-only, same
        division of labor as ``upload_asset``/``PostgresAssetStore.ingest_upload``.

        ``source_kind`` defaults to ``"http_upload"`` (the original,
        operator-initiated replace-source flow); the watch-folder daemon
        passes ``"watch_folder"`` so the resulting :class:`MediaIngestJob`'s
        provenance reflects where the replacement file actually came from
        (:data:`INGEST_SOURCE_WATCH_FOLDER`).

        Returns the new :class:`MediaIngestJob`'s ``job_id``.
        """

        with self._session_factory() as session:
            asset = session.get(Asset, asset_id)
            if asset is None:
                raise AssetNotFoundError(asset_id)
            asset.file_path = new_file_path
            asset.file_size_bytes = file_size_bytes
            asset.codec_video = codec_video
            asset.codec_audio = codec_audio
            asset.width_px = width_px
            asset.height_px = height_px
            asset.bitrate_bps = bitrate_bps
            asset.format_name = format_name
            asset.duration_seconds = duration_seconds
            asset.content_hash = content_hash
            asset.thumbnail_path = thumbnail_path
            asset.file_status = "ok"
            asset.file_status_checked_at = datetime.now(UTC)
            asset.state = "validated"
            asset.version += 1

            job = MediaIngestJob(
                asset_id=asset_id,
                source_kind=source_kind,
                source_path=new_file_path,
                status=JOB_STATUS_COMPLETED,
                progress_percent=100,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
            session.add(job)
            # Drop stale transcode jobs + readiness so the next lifecycle
            # worker pass re-seeds transcodes and recomputes the badge for
            # the NEW file, not the one that was just archived.
            session.query(TranscodeJob).filter(TranscodeJob.asset_id == asset_id).delete()
            existing_readiness = session.get(AssetReadiness, asset_id)
            if existing_readiness is not None:
                session.delete(existing_readiness)

            session.add(
                MediaLifecycleAuditEntry(
                    asset_id=asset_id,
                    action="source_replaced",
                    detail=f"Old file archived to {archived_old_path or '(none -- no prior file)'}; "
                    f"new file {new_file_path}.",
                    dry_run=False,
                )
            )
            session.flush()
            job_id = job.job_id
            session.commit()
            return job_id

    # -- Audit log -----------------------------------------------------------

    def list_audit_log(
        self, *, asset_id: str | None = None, limit: int = 100
    ) -> list[LifecycleAuditEntryResponse]:
        with self._session_factory() as session:
            stmt = select(MediaLifecycleAuditEntry).order_by(
                MediaLifecycleAuditEntry.created_at.desc()
            )
            if asset_id is not None:
                stmt = stmt.where(MediaLifecycleAuditEntry.asset_id == asset_id)
            stmt = stmt.limit(max(1, min(limit, 500)))
            rows = session.execute(stmt).scalars()
            return [LifecycleAuditEntryResponse.model_validate(r) for r in rows]
