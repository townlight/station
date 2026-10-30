# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Persistence seam for v0.8 podcast episodes."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from civiccast.podcast.models import PodcastEpisode

SessionFactory = Callable[[], AbstractContextManager[Session]]


class PodcastStore(Protocol):
    def upsert_episode(self, episode: PodcastEpisode) -> PodcastEpisode: ...
    def get_episode(self, asset_id: str) -> PodcastEpisode | None: ...
    def list_for_channel(self, channel_id: str) -> list[PodcastEpisode]: ...


class InMemoryPodcastStore:
    def __init__(self) -> None:
        self._episodes: dict[str, PodcastEpisode] = {}

    def upsert_episode(self, episode: PodcastEpisode) -> PodcastEpisode:
        self._episodes[episode.asset_id] = episode
        return episode

    def get_episode(self, asset_id: str) -> PodcastEpisode | None:
        return self._episodes.get(asset_id)

    def list_for_channel(self, channel_id: str) -> list[PodcastEpisode]:
        return [episode for episode in self._episodes.values() if episode.channel_id == channel_id]


class PostgresPodcastStore:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def upsert_episode(self, episode: PodcastEpisode) -> PodcastEpisode:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            existing = session.execute(
                text(f"SELECT asset_id FROM {table}podcast_episodes WHERE asset_id = :asset_id"),  # nosec B608
                {"asset_id": episode.asset_id},
            ).first()
            params = {
                **episode.model_dump(mode="json"),
                "chapters_json": json.dumps(
                    [chapter.model_dump(mode="json") for chapter in episode.chapters],
                    sort_keys=True,
                ),
            }
            if existing is None:
                session.execute(
                    text(
                        f"INSERT INTO {table}podcast_episodes "  # nosec B608
                        "(episode_id, asset_id, channel_id, title, audio_url, rss_guid, "
                        "duration_seconds, loudness_lufs, chapters_json, transcript_url, "
                        "summary, published_at) VALUES (:episode_id, :asset_id, "
                        ":channel_id, :title, :audio_url, :rss_guid, :duration_seconds, "
                        ":loudness_lufs, :chapters_json, :transcript_url, :summary, :published_at)"
                    ),
                    params,
                )
            else:
                session.execute(
                    text(
                        f"UPDATE {table}podcast_episodes SET title = :title, "  # nosec B608
                        "audio_url = :audio_url, rss_guid = :rss_guid, "
                        "duration_seconds = :duration_seconds, loudness_lufs = :loudness_lufs, "
                        "chapters_json = :chapters_json, transcript_url = :transcript_url, "
                        "summary = :summary, published_at = :published_at "
                        "WHERE asset_id = :asset_id"
                    ),
                    params,
                )
            session.commit()
            return episode

    def get_episode(self, asset_id: str) -> PodcastEpisode | None:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            row = session.execute(
                text(f"SELECT * FROM {table}podcast_episodes WHERE asset_id = :asset_id"),  # nosec B608
                {"asset_id": asset_id},
            ).first()
            return None if row is None else self._row_to_episode(row._mapping)

    def list_for_channel(self, channel_id: str) -> list[PodcastEpisode]:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            rows = session.execute(
                text(
                    f"SELECT * FROM {table}podcast_episodes "  # nosec B608
                    "WHERE channel_id = :channel_id ORDER BY published_at DESC"
                ),
                {"channel_id": channel_id},
            ).fetchall()
            return [self._row_to_episode(row._mapping) for row in rows]

    @staticmethod
    def _row_to_episode(mapping) -> PodcastEpisode:  # type: ignore[no-untyped-def]
        data = dict(mapping)
        data["chapters"] = json.loads(data.pop("chapters_json"))
        return PodcastEpisode.model_validate(data)

    @staticmethod
    def _table_prefix(session: Session) -> str:
        bind = session.get_bind()
        return "" if bind.dialect.name == "sqlite" else "civiccast."
