# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable store for the CG depth entities (S18 gap 6 — build step 7, slice 6).

CRUD over bulletin media (image / fullscreen slide / live-video), bulletin or
channel background audio, and channel-scoped zone tags. Domain integrity (a
live-video media names a live source, an image media names an asset) is enforced
by the :mod:`~civiccast.cg.depth_models` validators; the store persists what the
model already validated. ``created_at`` is taken from the model on insert
(matching :class:`~civiccast.cg.board_store.CgBoardStore`).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from civiccast.cg.depth_models import (
    BulletinAudio,
    BulletinAudioDb,
    BulletinMedia,
    BulletinMediaDb,
    ZoneTag,
    ZoneTagDb,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]

__all__ = ["CgDepthStore"]


class CgDepthStore:
    """SQLAlchemy-backed store for CG depth entities (Postgres or SQLite)."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    # -- Bulletin media ----------------------------------------------------

    def add_media(self, media: BulletinMedia) -> BulletinMedia:
        with self._session_factory() as session:
            session.add(
                BulletinMediaDb(
                    media_id=media.media_id,
                    bulletin_id=media.bulletin_id,
                    kind=media.kind,
                    asset_ref=media.asset_ref,
                    live_source=media.live_source,
                    created_at=media.created_at,
                )
            )
            session.commit()
            return media

    def list_media(self, bulletin_id: str) -> list[BulletinMedia]:
        with self._session_factory() as session:
            stmt = (
                select(BulletinMediaDb)
                .where(BulletinMediaDb.bulletin_id == bulletin_id)
                .order_by(BulletinMediaDb.created_at.asc(), BulletinMediaDb.media_id.asc())
            )
            return [_media_from_row(row) for row in session.scalars(stmt).all()]

    def delete_media(self, media_id: str) -> bool:
        return self._delete(BulletinMediaDb, media_id)

    # -- Bulletin / channel audio -----------------------------------------

    def set_audio(self, audio: BulletinAudio) -> BulletinAudio:
        with self._session_factory() as session:
            row = session.get(BulletinAudioDb, audio.audio_id)
            if row is None:
                session.add(
                    BulletinAudioDb(
                        audio_id=audio.audio_id,
                        scope=audio.scope,
                        target_id=audio.target_id,
                        asset_ref=audio.asset_ref,
                        loudness_regime=audio.loudness_regime,
                        gain_db=audio.gain_db,
                        created_at=audio.created_at,
                    )
                )
            else:
                row.scope = audio.scope
                row.target_id = audio.target_id
                row.asset_ref = audio.asset_ref
                row.loudness_regime = audio.loudness_regime
                row.gain_db = audio.gain_db
            session.commit()
            return audio

    def list_audio(self, *, scope: str, target_id: str) -> list[BulletinAudio]:
        with self._session_factory() as session:
            stmt = (
                select(BulletinAudioDb)
                .where(BulletinAudioDb.scope == scope, BulletinAudioDb.target_id == target_id)
                .order_by(BulletinAudioDb.created_at.asc(), BulletinAudioDb.audio_id.asc())
            )
            return [_audio_from_row(row) for row in session.scalars(stmt).all()]

    def delete_audio(self, audio_id: str) -> bool:
        return self._delete(BulletinAudioDb, audio_id)

    # -- Zone tags ---------------------------------------------------------

    def add_tag(self, tag: ZoneTag) -> ZoneTag:
        with self._session_factory() as session:
            session.add(
                ZoneTagDb(
                    tag_id=tag.tag_id,
                    channel_id=tag.channel_id,
                    label=tag.label,
                    created_at=tag.created_at,
                )
            )
            session.commit()
            return tag

    def list_tags(self, channel_id: str) -> list[ZoneTag]:
        with self._session_factory() as session:
            stmt = (
                select(ZoneTagDb)
                .where(ZoneTagDb.channel_id == channel_id)
                .order_by(ZoneTagDb.label.asc(), ZoneTagDb.tag_id.asc())
            )
            return [_tag_from_row(row) for row in session.scalars(stmt).all()]

    def delete_tag(self, tag_id: str) -> bool:
        return self._delete(ZoneTagDb, tag_id)

    # -- shared ------------------------------------------------------------

    def _delete(
        self,
        model: type[BulletinMediaDb] | type[BulletinAudioDb] | type[ZoneTagDb],
        pk: str,
    ) -> bool:
        with self._session_factory() as session:
            row = session.get(model, pk)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True


def _media_from_row(row: BulletinMediaDb) -> BulletinMedia:
    return BulletinMedia(
        media_id=row.media_id,
        bulletin_id=row.bulletin_id,
        kind=row.kind,  # type: ignore[arg-type]
        asset_ref=row.asset_ref,
        live_source=row.live_source,
        created_at=_coerce(row.created_at),
    )


def _audio_from_row(row: BulletinAudioDb) -> BulletinAudio:
    return BulletinAudio(
        audio_id=row.audio_id,
        scope=row.scope,  # type: ignore[arg-type]
        target_id=row.target_id,
        asset_ref=row.asset_ref,
        loudness_regime=row.loudness_regime,
        gain_db=row.gain_db,
        created_at=_coerce(row.created_at),
    )


def _tag_from_row(row: ZoneTagDb) -> ZoneTag:
    return ZoneTag(
        tag_id=row.tag_id,
        channel_id=row.channel_id,
        label=row.label,
        created_at=_coerce(row.created_at),
    )


def _coerce(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
