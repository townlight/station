# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Real YouTube reach client (Beta sprint B5, decision #6).

Talks to the YouTube Data API v3 with an OAuth refresh token the station
obtained out-of-band. Selected with ``CIVICCAST_PROVIDER_YOUTUBE=real``; the
deterministic mock remains the default. The three credential variables are
validated fail-fast at resolution.

- ``publish_live`` sends the selected running channel through the app-owned
  output. It returns a watch link and starting status; destination polling
  confirms delivery. Private ingest addresses never appear in publish proofs.
- ``upload_vod`` / ``upload_vod_path`` perform a resumable upload of the
  recording media; the asset-id-only form resolves
  ``CIVICCAST_YOUTUBE_MEDIA_ROOT/<asset_id>.mp4``.

No credentials live in code or tests; contract tests use
``httpx.MockTransport`` and never call googleapis.com. ``credential_key``
values in proofs are pointers to where the credential lives, never the
credential itself.
"""

from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from civiccast.platform.provider_settings import setting
from civiccast.syndicate.models import YouTubePublishProof

if TYPE_CHECKING:
    from civiccast.syndicate.live_output import YouTubeLiveOutput

_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint URL, not a secret
_API_BASE = "https://www.googleapis.com/youtube/v3"
_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
_CREDENTIAL_POINTER = "env://CIVICCAST_YOUTUBE_REFRESH_TOKEN"

__all__ = ["YouTubeClient", "YouTubeSettings"]


@dataclass(frozen=True)
class YouTubeSettings:
    """Station OAuth credentials and upload defaults, read from the environment."""

    client_id: str
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    media_root: Path | None = None
    privacy_status: str = "unlisted"

    @classmethod
    def from_env(cls) -> YouTubeSettings:
        client_id = setting("youtube", "client_id", "CIVICCAST_YOUTUBE_CLIENT_ID", "").strip()
        client_secret = setting(
            "youtube", "client_secret", "CIVICCAST_YOUTUBE_CLIENT_SECRET", ""
        ).strip()
        refresh_token = setting(
            "youtube", "refresh_token", "CIVICCAST_YOUTUBE_REFRESH_TOKEN", ""
        ).strip()
        missing = [
            name
            for name, value in (
                ("CIVICCAST_YOUTUBE_CLIENT_ID", client_id),
                ("CIVICCAST_YOUTUBE_CLIENT_SECRET", client_secret),
                ("CIVICCAST_YOUTUBE_REFRESH_TOKEN", refresh_token),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "CIVICCAST_PROVIDER_YOUTUBE=real requires "
                f"{', '.join(missing)} to be set (OAuth client + refresh "
                "token obtained out-of-band; see docs/ops/cdn-and-providers.md)."
            )
        media_root_raw = os.environ.get("CIVICCAST_YOUTUBE_MEDIA_ROOT", "").strip()
        privacy = (
            setting("youtube", "privacy_status", "CIVICCAST_YOUTUBE_PRIVACY", "").strip().lower()
            or "unlisted"
        )
        if privacy not in ("public", "unlisted", "private"):
            raise ValueError(
                f"CIVICCAST_YOUTUBE_PRIVACY must be public, unlisted, or private; got {privacy!r}."
            )
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            media_root=Path(media_root_raw) if media_root_raw else None,
            privacy_status=privacy,
        )


class YouTubeClient:
    """Reach client satisfying the same protocol as the mock."""

    def __init__(
        self,
        settings: YouTubeSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 600.0,
        live_output: YouTubeLiveOutput | None = None,
    ) -> None:
        self._live_output = live_output
        self._settings = settings
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    def _client(self, *, live: bool = False) -> httpx.Client:
        return httpx.Client(
            transport=self._transport,
            timeout=min(self._timeout_seconds, 15.0) if live else self._timeout_seconds,
        )

    def _access_token(self, client: httpx.Client) -> str:
        response = client.post(
            _TOKEN_URL,
            data={
                "client_id": self._settings.client_id,
                "client_secret": self._settings.client_secret,
                "refresh_token": self._settings.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        response.raise_for_status()
        token = response.json().get("access_token", "")
        if not token:
            raise RuntimeError("YouTube token endpoint returned no access_token.")
        return str(token)

    def publish_live(
        self,
        *,
        asset_id: str,
        title: str | None = None,
        channel_id: str | None = None,
        scheduled_start: datetime | None = None,
        scheduled_end: datetime | None = None,
        broadcast_id: str | None = None,
        stream_id: str | None = None,
    ) -> YouTubePublishProof:
        if self._live_output is None or not channel_id:
            raise RuntimeError("Choose an on-air channel for YouTube Live in Publish.")
        return self._live_output.start(
            self,
            asset_id=asset_id,
            title=title or asset_id,
            channel_id=channel_id,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            broadcast_id=broadcast_id,
            stream_id=stream_id,
        )

    def configure_live(
        self,
        *,
        title: str,
        scheduled_start: datetime | None = None,
        scheduled_end: datetime | None = None,
    ) -> tuple[str, str, str]:
        with self._client(live=True) as client:
            token = self._access_token(client)
            auth = {"Authorization": f"Bearer {token}"}
            broadcast_id = stream_id = ""
            try:
                broadcast = client.post(
                    f"{_API_BASE}/liveBroadcasts",
                    params={"part": "snippet,contentDetails,status"},
                    headers=auth,
                    json={
                        "snippet": {
                            "title": title[:100],
                            "scheduledStartTime": (scheduled_start or datetime.now(UTC))
                            .isoformat()
                            .replace("+00:00", "Z"),
                            **(
                                {"scheduledEndTime": scheduled_end.isoformat()}
                                if scheduled_end
                                else {}
                            ),
                        },
                        "contentDetails": {"enableAutoStart": True, "enableAutoStop": True},
                        "status": {
                            "privacyStatus": self._settings.privacy_status,
                            "selfDeclaredMadeForKids": False,
                        },
                    },
                )
                broadcast.raise_for_status()
                broadcast_id = str(broadcast.json()["id"])
                stream = client.post(
                    f"{_API_BASE}/liveStreams",
                    params={"part": "snippet,cdn"},
                    headers=auth,
                    json={
                        "snippet": {"title": title[:100]},
                        "cdn": {
                            "frameRate": "variable",
                            "ingestionType": "rtmp",
                            "resolution": "variable",
                        },
                    },
                )
                stream.raise_for_status()
                stream_body = stream.json()
                stream_id = str(stream_body["id"])
                ingestion = stream_body["cdn"]["ingestionInfo"]
                address = str(ingestion.get("rtmpsIngestionAddress", ""))
                parsed = urlsplit(address)
                if parsed.scheme != "rtmps" or not (parsed.hostname or "").endswith(".youtube.com"):
                    raise RuntimeError("YouTube did not return a supported secure ingest address.")
                stream_name = str(ingestion["streamName"])
                if not stream_name:
                    raise RuntimeError("YouTube returned no stream key.")
                bind = client.post(
                    f"{_API_BASE}/liveBroadcasts/bind",
                    params={"id": broadcast_id, "part": "id", "streamId": stream_id},
                    headers=auth,
                )
                bind.raise_for_status()
            except Exception:
                for resource, resource_id in (
                    ("liveBroadcasts", broadcast_id),
                    ("liveStreams", stream_id),
                ):
                    if resource_id:
                        with suppress(Exception):
                            client.delete(
                                f"{_API_BASE}/{resource}", params={"id": resource_id}, headers=auth
                            )
                raise RuntimeError(
                    "YouTube event setup failed. Check channel authorization and YouTube Studio."
                ) from None
        return broadcast_id, stream_id, f"{address.rstrip('/')}/{stream_name}"

    def update_live(
        self,
        broadcast_id: str,
        stream_id: str,
        *,
        title: str,
        scheduled_start: datetime | None,
        scheduled_end: datetime | None,
    ) -> None:
        """Edit the existing event without replacing its identity or privacy settings."""
        with self._client(live=True) as client:
            auth = {"Authorization": f"Bearer {self._access_token(client)}"}
            response = client.get(
                f"{_API_BASE}/liveBroadcasts",
                params={"id": broadcast_id, "part": "snippet"},
                headers=auth,
            )
            response.raise_for_status()
            snippet = response.json()["items"][0]["snippet"]
            snippet = {
                key: snippet[key]
                for key in (
                    "title",
                    "description",
                    "categoryId",
                    "scheduledStartTime",
                    "scheduledEndTime",
                )
                if key in snippet
            }
            snippet.update(
                title=title[:100],
                scheduledStartTime=(scheduled_start or datetime.now(UTC)).isoformat(),
            )
            if scheduled_end:
                snippet["scheduledEndTime"] = scheduled_end.isoformat()
            else:
                snippet.pop("scheduledEndTime", None)
            response = client.put(
                f"{_API_BASE}/liveBroadcasts",
                params={"part": "snippet"},
                headers=auth,
                json={"id": broadcast_id, "snippet": snippet},
            )
            response.raise_for_status()

    def live_ingest(self, stream_id: str) -> str:
        """Retrieve the private ingest address only when launching the destination."""
        with self._client(live=True) as client:
            response = client.get(
                f"{_API_BASE}/liveStreams",
                params={"id": stream_id, "part": "cdn"},
                headers={"Authorization": f"Bearer {self._access_token(client)}"},
            )
            response.raise_for_status()
            ingestion = response.json()["items"][0]["cdn"]["ingestionInfo"]
            address = str(ingestion.get("rtmpsIngestionAddress", ""))
            if (
                urlsplit(address).scheme != "rtmps"
                or not (urlsplit(address).hostname or "").endswith(".youtube.com")
                or not ingestion.get("streamName")
            ):
                raise RuntimeError("YouTube did not return a supported secure ingest address.")
            return f"{address.rstrip('/')}/{ingestion['streamName']}"

    def live_status(self, broadcast_id: str, stream_id: str) -> tuple[str, str]:
        with self._client(live=True) as client:
            auth = {"Authorization": f"Bearer {self._access_token(client)}"}
            stream = client.get(
                f"{_API_BASE}/liveStreams", params={"id": stream_id, "part": "status"}, headers=auth
            )
            stream.raise_for_status()
            broadcast = client.get(
                f"{_API_BASE}/liveBroadcasts",
                params={"id": broadcast_id, "part": "status"},
                headers=auth,
            )
            broadcast.raise_for_status()
            return str(stream.json()["items"][0]["status"]["streamStatus"]), str(
                broadcast.json()["items"][0]["status"]["lifeCycleStatus"]
            )

    def finish_live(self, broadcast_id: str, stream_id: str) -> None:
        """Finish a started event or remove an unused one, then release its stream."""
        with self._client(live=True) as client:
            auth = {"Authorization": f"Bearer {self._access_token(client)}"}
            response = client.get(
                f"{_API_BASE}/liveBroadcasts",
                params={"id": broadcast_id, "part": "status"},
                headers=auth,
            )
            response.raise_for_status()
            items = response.json().get("items", [])
            state = items[0]["status"]["lifeCycleStatus"] if items else "complete"
            if state in {"live", "testing"}:
                response = client.post(
                    f"{_API_BASE}/liveBroadcasts/transition",
                    params={"id": broadcast_id, "part": "status", "broadcastStatus": "complete"},
                    headers=auth,
                )
                response.raise_for_status()
            elif state != "complete":
                response = client.delete(
                    f"{_API_BASE}/liveBroadcasts",
                    params={"id": broadcast_id},
                    headers=auth,
                )
                response.raise_for_status()
            response = client.delete(
                f"{_API_BASE}/liveStreams",
                params={"id": stream_id},
                headers=auth,
            )
            response.raise_for_status()

    def upload_vod(self, *, asset_id: str) -> YouTubePublishProof:
        if self._settings.media_root is None:
            raise RuntimeError(
                "CIVICCAST_YOUTUBE_MEDIA_ROOT must point at the recordings "
                "directory for asset-id VOD uploads (or call upload_vod_path)."
            )
        return self.upload_vod_path(
            asset_id=asset_id, path=self._settings.media_root / f"{asset_id}.mp4"
        )

    def upload_vod_path(self, *, asset_id: str, path: Path) -> YouTubePublishProof:
        if not path.exists():
            raise RuntimeError(f"Recording media for {asset_id!r} not found at {path}.")
        with self._client() as client:
            token = self._access_token(client)
            auth = {"Authorization": f"Bearer {token}"}
            init = client.post(
                _UPLOAD_URL,
                params={"uploadType": "resumable", "part": "snippet,status"},
                headers={
                    **auth,
                    "X-Upload-Content-Type": "video/mp4",
                    "X-Upload-Content-Length": str(path.stat().st_size),
                },
                json={
                    "snippet": {"title": asset_id},
                    "status": {
                        "privacyStatus": self._settings.privacy_status,
                        "selfDeclaredMadeForKids": False,
                    },
                },
            )
            init.raise_for_status()
            upload_url = init.headers.get("location", "")
            if not upload_url:
                raise RuntimeError("YouTube resumable-upload init returned no Location URL.")
            with path.open("rb") as handle:
                upload = client.put(
                    upload_url,
                    content=handle,
                    headers={**auth, "Content-Type": "video/mp4"},
                )
            upload.raise_for_status()
            video_id = str(upload.json()["id"])
        return YouTubePublishProof(
            target_type="youtube_vod",
            delivery_state="live",
            url=f"https://www.youtube.com/watch?v={video_id}",
            credential_key=_CREDENTIAL_POINTER,
        )
