# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Create a real, locally served audio enclosure for a published episode."""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from urllib.request import url2pathname

from civiccast.podcast.models import PodcastEpisode, PodcastEpisodeCreate


class PodcastMediaError(RuntimeError):
    """The episode's audio enclosure could not be produced."""


def _source_path(source_media_url: str) -> Path:
    direct_path = Path(source_media_url)
    if direct_path.is_absolute():
        if not direct_path.is_file():
            raise PodcastMediaError("The local recording file is missing.")
        return direct_path
    parsed = urlparse(source_media_url)
    if parsed.scheme == "file":
        if parsed.netloc not in ("", "localhost"):
            raise PodcastMediaError("Podcast source must be a local recording file.")
        path = Path(url2pathname(unquote(parsed.path)))
    else:
        raise PodcastMediaError("Podcast source must be a local recording file.")
    if not path.is_file():
        raise PodcastMediaError("The local recording file is missing.")
    return path


def podcast_media_dir() -> Path:
    configured = os.environ.get("CIVICCAST_PODCAST_MEDIA_DIR")
    if configured:
        return Path(configured)
    if os.name == "nt":
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "CivicCast" / "podcast"
    return (
        Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
        / "CivicCast"
        / "podcast"
    )


def create_podcast_episode(request: PodcastEpisodeCreate) -> PodcastEpisode:
    """Extract and normalize audio before an episode can enter the public feed."""
    source = _source_path(request.source_media_url)
    base_url = os.environ.get("CIVICCAST_PUBLIC_BASE_URL", "").rstrip("/")
    if urlparse(base_url).scheme not in ("http", "https") or not urlparse(base_url).netloc:
        raise PodcastMediaError("Set CIVICCAST_PUBLIC_BASE_URL before publishing a podcast.")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise PodcastMediaError("FFmpeg and FFprobe are required to publish podcast audio.")
    output_dir = podcast_media_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(request.asset_id.encode("utf-8")).hexdigest()
    filename = f"{digest[:32]}.mp3"
    output_path = output_dir / filename
    with tempfile.NamedTemporaryFile(dir=output_dir, suffix=".mp3", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        encoded = subprocess.run(  # noqa: S603 - executable resolved from PATH; shell is disabled
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-protocol_whitelist",
                "file,pipe",
                "-filter_threads",
                "1",
                "-i",
                str(source),
                "-vn",
                "-af",
                "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-codec:a",
                "libmp3lame",
                "-threads",
                "1",
                "-b:a",
                "128k",
                str(temporary_path),
            ],
            capture_output=True,
            text=True,
            timeout=7200,
            check=False,
        )
        if encoded.returncode != 0 or temporary_path.stat().st_size == 0:
            raise PodcastMediaError("FFmpeg could not create podcast audio from this recording.")
        duration_result = subprocess.run(  # noqa: S603 - fixed ffprobe argv, no shell
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(temporary_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if duration_result.returncode != 0:
            raise PodcastMediaError("The encoded podcast audio could not be verified.")
        duration = math.ceil(float(duration_result.stdout.strip()))
        if duration <= 0:
            raise PodcastMediaError("The encoded podcast audio has no playable duration.")
        temporary_path.replace(output_path)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise PodcastMediaError("Podcast audio conversion failed or timed out.") from exc
    finally:
        temporary_path.unlink(missing_ok=True)
    return PodcastEpisode(
        episode_id=f"pod-{digest[:24]}",
        asset_id=request.asset_id,
        channel_id=request.channel_id,
        title=request.title,
        audio_url=f"{base_url}/api/public/podcast/audio/{quote(request.asset_id, safe='')}.mp3",
        rss_guid=f"civiccast:podcast:{request.asset_id}",
        duration_seconds=duration,
        loudness_lufs=-16.0,
        chapters=request.chapters,
        transcript_url=request.signed_transcript_url,
        summary=request.summary,
        published_at=datetime.now(UTC),
    )
