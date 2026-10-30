# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Bulletin filler renderer (cable automation CA-3).

Gaps between scheduled programs can show a rotating community bulletin board
(incoming airable bulletins rendered into the board's primary zone) instead of
plain slides. When no active board exists, the legacy per-bulletin slide flow
remains unchanged.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from civiccast.cable.channel import ChannelBranding
from civiccast.cg.board_resolver import ResolvedBoard, airable_bulletins
from civiccast.cg.board_runtime import build_board_snapshot_from_store
from civiccast.cg.board_store import CgBoardStore
from civiccast.cg.feed_fetcher import FeedCache
from civiccast.cg.models import CgBulletinSubmission
from civiccast.egress.board_compositor import (
    ImageResolver,
    _fit_text_to_slot,
    _region_box,
    _safe_area_pixels,
    board_segment_cache_key,
    build_board_preview_args,
    build_board_segment_args,
)
from civiccast.egress.errors import SourcePrepareError
from civiccast.egress.gst.bridge import source_first_element
from civiccast.egress.gst.graph import ElementSpec, GraphicsOverlayLayer
from civiccast.egress.models import (
    MAX_PLAYLIST_SUBCHAINS,
    EgressConfig,
    EgressSourcePlan,
    EgressSourceSegment,
)
from civiccast.egress.runtime import FfmpegRunner
from civiccast.egress.source_plan import (
    SlateSourceGenerator,
    _escape_concat_path,
    _escape_drawtext,
    concat_copy_args,
)
from civiccast.schedule.store import PostgresAssetStore
from civiccast.stream._ffmpeg import FfmpegError, FfmpegNotFoundError, run_ffmpeg

BulletinsProvider = Callable[[str], Sequence[CgBulletinSubmission]]
BoardProvider = Callable[[str, datetime], ResolvedBoard | None]
BoardAudioProvider = Callable[[str], "BoardAudioBed | None"]
#: (channel_id, config) -> board raster for the S15 engine overlay, or None.
#: Takes the config because frame geometry lives on ``canonical_profile``.
BoardOverlayProvider = Callable[[str, "EgressConfig"], Path | None]
BoardLiveVideoProvider = Callable[[str, "EgressConfig"], tuple[GraphicsOverlayLayer, ...]]
BrandingProvider = Callable[[str], ChannelBranding | None]

_DEFAULT_BACKGROUND = "0x1a2744"
_LOG = logging.getLogger(__name__)
_SLIDE_SECONDS = 10


@dataclass(frozen=True)
class BoardAudioBed:
    asset_ref: str
    path: Path
    gain_db: float
    source_version: str
    duration_seconds: int


@dataclass(frozen=True)
class _BoardAudioPhase:
    identity: str
    anchor: datetime
    plan_start: datetime
    plan_end: datetime


__all__ = [
    "BulletinFillerSourceGenerator",
    "FillerSourceProvider",
    "build_board_live_video_provider",
    "build_board_overlay_provider",
    "build_bulletin_slide_args",
    "build_filler_source_provider",
]


class BulletinFillerSourceGenerator:
    """Render the approved bulletin rotation as an egress source plan."""

    def __init__(
        self,
        *,
        work_dir: Path,
        bulletins_provider: BulletinsProvider,
        branding_provider: BrandingProvider | None = None,
        board_provider: BoardProvider | None = None,
        board_audio_provider: BoardAudioProvider | None = None,
        ffmpeg_runner: FfmpegRunner = run_ffmpeg,
        slate_generator: SlateSourceGenerator | None = None,
        board_image_resolver: ImageResolver | None = None,
        slide_seconds: int = _SLIDE_SECONDS,
        target_fill_seconds: int = 3600,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._work_dir = work_dir
        self._bulletins_provider = bulletins_provider
        self._branding_provider = branding_provider
        self._board_provider = board_provider
        self._board_audio_provider = board_audio_provider
        self._board_audio_phase: dict[str, _BoardAudioPhase] = {}
        self._ffmpeg_runner = ffmpeg_runner
        self._slate_generator = slate_generator or SlateSourceGenerator(
            work_dir=work_dir, ffmpeg_runner=ffmpeg_runner
        )
        self._board_image_resolver = board_image_resolver
        self._slide_seconds = slide_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._target_fill_seconds = target_fill_seconds

    def __call__(self, config: EgressConfig) -> EgressSourcePlan:
        now = self._clock()
        board = self._board_provider(config.channel_id, now) if self._board_provider else None
        if board is None:
            self._board_audio_phase.pop(config.channel_id, None)
            return self._render_slide_plan(config, now=now)
        return self._render_board_plan(config, board, now=now)

    def _render_slide_plan(self, config: EgressConfig, *, now: datetime) -> EgressSourcePlan:
        # Only bulletins inside their [requested_start, requested_end) window
        # air right now; scheduled-for-later and expired ones are filtered out.
        bulletins = airable_bulletins(list(self._bulletins_provider(config.channel_id)), now=now)
        if not bulletins:
            return self._slate_generator(config)
        branding = self._branding(config.channel_id)
        slide_dir = self._work_dir / config.channel_id / "bulletins"
        slide_dir.mkdir(parents=True, exist_ok=True)
        segments: list[EgressSourceSegment] = []
        for bulletin in bulletins:
            slide_path = slide_dir / f"{self._slide_hash(bulletin, branding, config)}.ts"
            if not slide_path.exists():
                self._render_slide(slide_path, config, bulletin, branding)
            segments.append(
                EgressSourceSegment(
                    label=bulletin.title,
                    path=str(slide_path),
                    duration_seconds=self._slide_seconds,
                    kind="cg",
                    source_ref=f"bulletin-{bulletin.submission_id}",
                )
            )
        return self._plan_with_cycle(config, segments)

    def _prune_board_audio_cache(
        self, board_dir: Path, segments: list[EgressSourceSegment]
    ) -> None:
        """Bound generated audio segments without touching an active board plan."""
        protected = {Path(segment.path) for segment in segments}
        cutoff = time.time() - max(600, self._slide_seconds * len(segments) * 2)
        for path in board_dir.glob("audio-*.ts"):
            if path in protected:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError as exc:
                _LOG.warning("could not clear stale generated board audio %s: %s", path, exc)

    def _render_board_plan(
        self,
        config: EgressConfig,
        board: ResolvedBoard,
        *,
        now: datetime,
    ) -> EgressSourcePlan:
        bulletins = airable_bulletins(list(self._bulletins_provider(config.channel_id)), now=now)
        board_bulletins: list[CgBulletinSubmission | None] = list(bulletins) or [None]
        audio_bed = (
            self._board_audio_provider(config.channel_id) if self._board_audio_provider else None
        )
        branding = self._branding(config.channel_id)
        profile = config.canonical_profile
        board_dir = self._work_dir / config.channel_id / "board"
        board_dir.mkdir(parents=True, exist_ok=True)

        if audio_bed is not None:
            identity = f"{audio_bed.asset_ref}:{audio_bed.source_version}:{audio_bed.gain_db}"
            previous = self._board_audio_phase.get(config.channel_id)
            if previous is None or previous.identity != identity or now < previous.anchor:
                anchor = now
                plan_start = now
            else:
                anchor = previous.anchor
                plan_start = (
                    previous.plan_start
                    if now < previous.plan_start
                    else max(now, previous.plan_end)
                )
            phase_seconds = int((plan_start - anchor).total_seconds()) % audio_bed.duration_seconds
            slide_count = max(
                len(board_bulletins),
                min(MAX_PLAYLIST_SUBCHAINS, -(-self._target_fill_seconds // self._slide_seconds)),
            )
            board_bulletins = [
                board_bulletins[index % len(board_bulletins)] for index in range(slide_count)
            ]
        else:
            self._board_audio_phase.pop(config.channel_id, None)
            identity = None
            phase_seconds = 0

        segments: list[EgressSourceSegment] = []
        for index, bulletin in enumerate(board_bulletins):
            audio_offset = (
                (phase_seconds + index * self._slide_seconds) % audio_bed.duration_seconds
                if audio_bed is not None
                else 0
            )
            key = board_segment_cache_key(
                board=board,
                bulletin=bulletin,
                background_color=self._background(branding),
                station_short_name=branding.short_name if branding else "",
                segment_seconds=float(self._slide_seconds),
                width=profile.width,
                height=profile.height,
                frame_rate=profile.fps,
                now=now,
                audio_asset_ref=audio_bed.asset_ref if audio_bed else None,
                audio_gain_db=audio_bed.gain_db if audio_bed else 0.0,
                audio_source_version=audio_bed.source_version if audio_bed else None,
                audio_offset_seconds=audio_offset,
            )
            segment_path = board_dir / f"{'audio-' if audio_bed else ''}{key}.ts"
            if not segment_path.exists():
                # Render to a temp path and atomically publish: writing ffmpeg
                # output straight to the cache path lets a concurrent renderer
                # (or a crash mid-write) leave a partial segment that a later
                # exists() check happily airs (gate finding: cache TOCTOU).
                staging = segment_path.with_name(f".{key}.{os.getpid()}.partial.ts")
                rendered_with_audio = self._render_board_segment(
                    segment_path=staging,
                    config=config,
                    board=board,
                    bulletin=bulletin,
                    branding=branding,
                    now=now,
                    audio_bed=audio_bed,
                    audio_offset_seconds=audio_offset,
                )
                if audio_bed is not None and not rendered_with_audio:
                    segment_path = board_dir / f"audio-{key}.silent.ts"
                    # A broken track should be retried on the next plan, not
                    # on every remaining slide in this one.
                    audio_bed = None
                staging.replace(segment_path)
            segments.append(
                EgressSourceSegment(
                    label="Board" if bulletin is None else bulletin.title,
                    path=str(segment_path),
                    duration_seconds=self._slide_seconds,
                    kind="cg",
                    board_audio_gain_db=audio_bed.gain_db if audio_bed else None,
                    source_ref=(
                        "board-empty" if bulletin is None else f"bulletin-{bulletin.submission_id}"
                    ),
                )
            )
        plan = self._plan_with_cycle(config, segments)
        if identity is not None:
            self._board_audio_phase[config.channel_id] = _BoardAudioPhase(
                identity=identity,
                anchor=anchor,
                plan_start=plan_start,
                plan_end=plan_start
                + timedelta(seconds=sum(segment.duration_seconds for segment in plan.segments)),
            )
        self._prune_board_audio_cache(board_dir, segments)
        return plan

    def _plan_with_cycle(
        self, config: EgressConfig, segments: list[EgressSourceSegment]
    ) -> EgressSourcePlan:
        if len(segments) <= MAX_PLAYLIST_SUBCHAINS:
            cycle_seconds = self._slide_seconds * len(segments)
            cycles = min(
                MAX_PLAYLIST_SUBCHAINS // len(segments),
                max(1, -(-self._target_fill_seconds // cycle_seconds)),
            )
            return EgressSourcePlan(channel_id=config.channel_id, segments=segments * cycles)
        group_size = -(-len(segments) // MAX_PLAYLIST_SUBCHAINS)
        rotations: list[EgressSourceSegment] = []
        for index in range(0, len(segments), group_size):
            group = segments[index : index + group_size]
            rotation_path = self._render_rotation(config, group)
            rotations.append(
                EgressSourceSegment(
                    label="Bulletin rotation",
                    path=str(rotation_path),
                    duration_seconds=sum(segment.duration_seconds for segment in group),
                    kind="cg",
                    source_ref=f"bulletin-rotation-{index // group_size}",
                )
            )
        return EgressSourcePlan(
            channel_id=config.channel_id,
            segments=rotations,
        )

    def _render_rotation(self, config: EgressConfig, segments: list[EgressSourceSegment]) -> Path:
        rotation_dir = self._work_dir / config.channel_id / "rotations"
        rotation_dir.mkdir(parents=True, exist_ok=True)
        key = self._rotation_hash(config, segments)
        rotation_path = rotation_dir / f"rotation-{key}.ts"
        if rotation_path.exists() and rotation_path.stat().st_size > 0:
            return rotation_path
        manifest = rotation_dir / f".{key}.{uuid.uuid4().hex}.concat.txt"
        staging = rotation_dir / f".{key}.{uuid.uuid4().hex}.partial.ts"
        try:
            manifest.write_text(
                "".join(f"file '{_escape_concat_path(segment.path)}'\n" for segment in segments),
                encoding="utf-8",
            )
            result = self._run_ffmpeg_or_fail_open(
                concat_copy_args(manifest=manifest, destination=staging),
                what=f"bulletin rotation for channel {config.channel_id}",
            )
            if result.returncode != 0:
                raise SourcePrepareError(
                    f"Could not concatenate bulletin rotation for channel {config.channel_id!r}."
                )
            staging.replace(rotation_path)
        finally:
            manifest.unlink(missing_ok=True)
            staging.unlink(missing_ok=True)
        return rotation_path

    @staticmethod
    def _rotation_hash(config: EgressConfig, segments: list[EgressSourceSegment]) -> str:
        digest = hashlib.sha256()
        digest.update(config.canonical_profile.model_dump_json().encode("utf-8"))
        digest.update(str(_SLIDE_SECONDS).encode("utf-8"))
        for segment in segments:
            digest.update(segment.path.encode("utf-8"))
            digest.update(str(segment.duration_seconds).encode("utf-8"))
        return digest.hexdigest()[:24]

    def _render_board_segment(
        self,
        *,
        segment_path: Path,
        config: EgressConfig,
        board: ResolvedBoard,
        bulletin: CgBulletinSubmission | None,
        branding: ChannelBranding | None,
        now: datetime,
        audio_bed: BoardAudioBed | None,
        audio_offset_seconds: float = 0.0,
    ) -> bool:
        profile = config.canonical_profile

        def attempt(include_text: bool, bed: BoardAudioBed | None) -> bool:
            args = build_board_segment_args(
                board=board,
                bulletin=bulletin,
                width=profile.width,
                height=profile.height,
                frame_rate=profile.fps,
                background_color=self._background(branding),
                station_short_name=(branding.short_name if branding else ""),
                segment_seconds=self._slide_seconds,
                out_path=segment_path,
                include_text=include_text,
                now=now,
                image_resolver=self._board_image_resolver or (lambda _: None),
                codec_args=self._board_codec_args(profile),
                audio_path=bed.path if bed else None,
                audio_offset_seconds=audio_offset_seconds if bed else 0.0,
                audio_gain_db=bed.gain_db if bed else 0.0,
                audio_sample_rate=profile.audio_sample_rate,
                audio_channels=profile.audio_channels,
                audio_codec=profile.audio_codec,
                audio_bitrate_kbps=profile.audio_bitrate_kbps,
            )
            try:
                result = self._run_ffmpeg_or_fail_open(
                    args, what=f"board segment for channel {config.channel_id}"
                )
            except SourcePrepareError as exc:
                if isinstance(exc.__cause__, FfmpegError):
                    return False
                raise
            return bool(result.returncode == 0)

        if attempt(True, audio_bed):
            return audio_bed is not None
        if audio_bed is not None:
            _LOG.warning(
                "channel %s: board render failed with audio; retrying without text",
                config.channel_id,
            )
            if attempt(False, audio_bed):
                return True
            _LOG.warning(
                "channel %s: configured board audio could not be rendered; airing the board silently",
                config.channel_id,
            )
            if attempt(True, None):
                return False
        _LOG.warning(
            "channel %s: board text render failed; retrying image-only",
            config.channel_id,
        )
        if attempt(False, None):
            return False
        ref = "empty" if bulletin is None else bulletin.submission_id
        raise SourcePrepareError(
            f"Could not render board segment for bulletin {ref!r}; "
            "inspect FFmpeg output before retrying."
        )

    def _run_ffmpeg_or_fail_open(self, args: list[str], *, what: str) -> Any:
        """Run ffmpeg, converting an absent/failed binary into the daemon's contract.

        ``run_ffmpeg`` raises ``FfmpegNotFoundError``/``FfmpegError`` BEFORE returning
        a result when the binary is missing or unusable. Those escaped the filler
        uncaught and crashed the egress service (gate finding QA-1); the daemon
        already treats ``SourcePrepareError`` as "this channel cannot be filled right
        now" and keeps running, so translate into that contract.
        """
        try:
            return self._ffmpeg_runner(args)
        except (FfmpegError, FfmpegNotFoundError) as exc:
            raise SourcePrepareError(
                f"Could not render {what}: FFmpeg is unavailable ({exc}). "
                "Install or repair the bundled FFmpeg runtime; the channel falls "
                "back to its slate until then."
            ) from exc

    @staticmethod
    def _board_codec_args(profile: Any) -> list[str]:
        return [
            "-c:v",
            profile.video_codec,
            "-b:v",
            f"{profile.video_bitrate_kbps}k",
            "-g",
            str(profile.gop_size),
        ]

    def _render_slide(
        self,
        slide_path: Path,
        config: EgressConfig,
        bulletin: CgBulletinSubmission,
        branding: ChannelBranding | None,
    ) -> None:
        args = build_bulletin_slide_args(
            output_path=slide_path,
            config=config,
            bulletin=bulletin,
            branding=branding,
            duration_seconds=self._slide_seconds,
        )
        result = self._run_ffmpeg_or_fail_open(
            args, what=f"bulletin slide {bulletin.submission_id!r}"
        )
        if result.returncode != 0:
            # Mirror the slate generator's posture: a host without usable
            # fonts can still air a plain-color slide rather than dead air.
            _LOG.warning(
                "channel %s: bulletin slide text render failed; retrying image-only. "
                "The bulletin will air WITHOUT its text until the host's fonts are fixed.",
                config.channel_id,
            )
            args = build_bulletin_slide_args(
                output_path=slide_path,
                config=config,
                bulletin=bulletin,
                branding=branding,
                duration_seconds=self._slide_seconds,
                include_text=False,
            )
            result = self._run_ffmpeg_or_fail_open(
                args, what=f"image-only bulletin slide {bulletin.submission_id!r}"
            )
        if result.returncode != 0:
            raise SourcePrepareError(
                f"Could not render bulletin slide {bulletin.submission_id!r}; "
                "inspect FFmpeg output before retrying."
            )

    def _slide_hash(
        self,
        bulletin: CgBulletinSubmission,
        branding: ChannelBranding | None,
        config: EgressConfig,
    ) -> str:
        digest = hashlib.sha256()
        for part in (
            "text-fit-v1",
            bulletin.submission_id,
            bulletin.title,
            bulletin.message,
            bulletin.organization,
            branding.color if branding else "",
            branding.short_name if branding else "",
            str(self._slide_seconds),
            config.canonical_profile.model_dump_json(),
        ):
            digest.update(part.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()[:24]

    def _branding(self, channel_id: str) -> ChannelBranding | None:
        if self._branding_provider is None:
            return None
        return self._branding_provider(channel_id)

    @staticmethod
    def _background(branding: ChannelBranding | None) -> str:
        return "0x" + branding.color.lstrip("#") if branding is not None else _DEFAULT_BACKGROUND


class FillerSourceProvider:
    """Per-channel gap filler: branch on the config's fill policy."""

    def __init__(
        self,
        *,
        bulletin_generator: BulletinFillerSourceGenerator,
        slate_generator: SlateSourceGenerator,
    ) -> None:
        self._bulletin_generator = bulletin_generator
        self._slate_generator = slate_generator

    def __call__(self, config: EgressConfig) -> EgressSourcePlan:
        if config.fill_policy == "bulletins":
            return self._bulletin_generator(config)
        return self._slate_generator(config)


def _upload_confinement_root() -> Path | None:
    """Return the configured CivicCast upload root, or None if unconfigured.

    Same env var and resolution convention ``civiccast/schedule/router.py``
    already uses to confine asset paths (``CIVICCAST_UPLOAD_DIR``).
    """
    raw = os.environ.get("CIVICCAST_UPLOAD_DIR", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _default_image_resolver(session_factory: Any) -> ImageResolver:
    asset_store = PostgresAssetStore(session_factory)

    def resolve(ref: str) -> Path | None:
        if not isinstance(ref, str) or not ref:
            return None
        row = asset_store.get_staff_row(ref)
        if row is None or row.file_path is None:
            return None
        path = Path(row.file_path).expanduser().resolve()
        # Defense-in-depth confinement (gate finding F-4): file_path is a
        # staff-set DB column, not community-controlled, so this isn't
        # exploitable today -- but board image and audio assets should never
        # be able to resolve to an arbitrary filesystem path outside
        # CivicCast's own upload storage. Fail closed (render without the
        # image, same as an unresolvable ref) rather than serve it.
        upload_root = _upload_confinement_root()
        if upload_root is None or not path.is_relative_to(upload_root):
            _LOG.warning(
                "CG asset ref %r resolved to %s, which is outside the "
                "configured CivicCast upload root (%s); refusing to render "
                "it into the board/bulletin.",
                ref,
                path,
                upload_root,
            )
            return None
        return path if path.is_file() else None

    return resolve


def _default_audio_resolver(session_factory: Any) -> ImageResolver:
    asset_store = PostgresAssetStore(session_factory)
    confined_path = _default_image_resolver(session_factory)

    def resolve(ref: str) -> Path | None:
        row = asset_store.get_staff_row(ref)
        if (
            row is None
            or not row.codec_audio
            or not row.duration_seconds
            or row.duration_seconds <= 0
        ):
            return None
        return confined_path(ref)

    return resolve


def build_filler_source_provider(
    session_factory: Any,
    *,
    work_dir: Path,
) -> FillerSourceProvider:
    """Construct the wired per-channel gap filler (durable bulletin store)."""

    from civiccast.cable.channel import default_channel_profiles
    from civiccast.cg.bulletin_store import PostgresCgBulletinStore
    from civiccast.cg.depth_store import CgDepthStore

    bulletin_store = PostgresCgBulletinStore(session_factory)
    board_store = CgBoardStore(session_factory)
    audio_store = CgDepthStore(session_factory)
    asset_store = PostgresAssetStore(session_factory)
    feed_cache = FeedCache()
    asset_resolver = _default_image_resolver(session_factory)
    audio_resolver = _default_audio_resolver(session_factory)

    def board_resolver(channel_id: str, now: datetime) -> ResolvedBoard | None:
        return build_board_snapshot_from_store(
            board_store,
            channel_id,
            now=now,
            cache=feed_cache,
        )

    def _branding(channel_id: str) -> ChannelBranding | None:
        for profile in default_channel_profiles():
            if profile.channel_id == channel_id:
                return profile.branding
        return None

    def _board_audio(channel_id: str) -> BoardAudioBed | None:
        configured = audio_store.list_audio(scope="channel", target_id=channel_id)
        if not configured:
            return None
        audio = configured[-1]
        asset = asset_store.get_staff_row(audio.asset_ref)
        path = audio_resolver(audio.asset_ref)
        if (
            path is None
            or asset is None
            or not asset.duration_seconds
            or asset.duration_seconds <= 0
        ):
            _LOG.warning(
                "channel %s: configured board audio asset %s is unavailable; airing silence",
                channel_id,
                audio.asset_ref,
            )
            return None
        try:
            stat = path.stat()
        except OSError:
            _LOG.warning(
                "channel %s: board audio asset %s disappeared before rendering; airing silence",
                channel_id,
                audio.asset_ref,
            )
            return None
        return BoardAudioBed(
            asset_ref=audio.asset_ref,
            path=path,
            gain_db=audio.gain_db,
            source_version=f"{stat.st_size}:{stat.st_mtime_ns}",
            duration_seconds=asset.duration_seconds,
        )

    slate_generator = SlateSourceGenerator(work_dir=work_dir)
    return FillerSourceProvider(
        bulletin_generator=BulletinFillerSourceGenerator(
            work_dir=work_dir,
            bulletins_provider=bulletin_store.list_approved,
            branding_provider=_branding,
            board_provider=board_resolver,
            board_audio_provider=_board_audio,
            board_image_resolver=asset_resolver,
            ffmpeg_runner=run_ffmpeg,
            slate_generator=slate_generator,
        ),
        slate_generator=slate_generator,
    )


def build_board_live_video_provider(session_factory: Any) -> BoardLiveVideoProvider:
    """Resolve configured board live sources to real-time compositor layers.

    Source ids are resolved against the channel's own live-source inventory;
    the board never stores an arbitrary URL or a credential in its zone row.
    A removed or unsupported source leaves that zone on the ordinary board.
    """
    from civiccast.live.store import LiveSourceStore

    board_store = CgBoardStore(session_factory)
    source_store = LiveSourceStore(session_factory)

    def resolve(channel_id: str, config: EgressConfig) -> tuple[GraphicsOverlayLayer, ...]:
        board = build_board_snapshot_from_store(board_store, channel_id, now=datetime.now(UTC))
        if board is None:
            return ()
        profile = config.canonical_profile
        zones_by_kind = {zone.kind: zone for zone in board.snapshot.zones}
        safe = _safe_area_pixels(profile.width, profile.height)
        layers: list[GraphicsOverlayLayer] = []
        for region in sorted(board.snapshot.template.regions, key=lambda item: item.order):
            zone = zones_by_kind.get(region.zone_kind)
            if zone is None or zone.source != "live_video":
                continue
            source_id = zone.content.get("live_source_id")
            if not isinstance(source_id, str):
                continue
            source = source_store.get(source_id)
            if source is None or source.channel_id != channel_id:
                _LOG.warning(
                    "channel %s: board live-video source %s is unavailable; leaving the board visible",
                    channel_id,
                    source_id,
                )
                continue
            if source.source_type == "ndi":
                _LOG.warning(
                    "channel %s: NDI board source %s has no native playout path",
                    channel_id,
                    source_id,
                )
                continue
            try:
                first = source_first_element(
                    EgressSourceSegment(
                        label=source.name,
                        path=source.endpoint_url,
                        duration_seconds=3600,
                        kind="live",
                        source_ref=source.live_source_id,
                        secret_ref=source.credentials_handle,
                    )
                )
            except ValueError:
                _LOG.warning(
                    "channel %s: board live-video source %s has no supported playout path",
                    channel_id,
                    source_id,
                )
                continue
            x, y, width, height = _region_box(
                region.region,
                width=profile.width,
                height=profile.height,
                safe_area_pixels=safe,
            )
            layers.append(
                GraphicsOverlayLayer(
                    name=f"board_live_{zone.zone_id}",
                    source_chain=(first, ElementSpec("decodebin")),
                    xpos=x,
                    ypos=y,
                    width=width,
                    height=height,
                    repeat_after_eos=False,
                )
            )
        return tuple(layers)

    return resolve


def build_board_overlay_provider(
    session_factory: Any,
    *,
    work_dir: Path,
    ffmpeg_runner: FfmpegRunner = run_ffmpeg,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BoardOverlayProvider:
    """Per-channel board raster for the S15 engine overlay leg.

    Returns the cached PNG of the channel's active board (rendered through the
    same compositor internals the filler and preview use), or None when the
    channel has no active board or the render fails — the engine then airs
    without the overlay, and the failure is logged, never silent.

    Geometry comes from the channel's own ``EgressConfig.canonical_profile`` —
    the same source the filler renders against, so the overlay raster always
    matches the encoded frame. ``ChannelProfile`` (the branding lineup) carries
    no frame geometry; reading it for width/height/fps raised AttributeError on
    every configured channel (gate finding: crash instead of fail-open).

    ``clock`` exists so a caller can pin the wall time this provider reads.
    A board carrying a clock zone folds ``now`` (truncated to the minute) into
    its cache key on purpose -- the overlay must re-render when the displayed
    minute changes. That makes two back-to-back calls return different cache
    keys whenever they straddle a minute boundary, which is correct in
    production and unreproducible in a test that asserts a cache hit. Injecting
    the clock makes that dependency explicit instead of implicit; the default
    is the same ``datetime.now(UTC)`` the closure used before.
    """

    from civiccast.cable.channel import default_channel_profiles
    from civiccast.cg.bulletin_store import PostgresCgBulletinStore

    board_store = CgBoardStore(session_factory)
    bulletin_store = PostgresCgBulletinStore(session_factory)
    feed_cache = FeedCache()
    image_resolver = _default_image_resolver(session_factory)

    def provider(channel_id: str, config: EgressConfig) -> Path | None:
        now = clock()
        board = build_board_snapshot_from_store(
            board_store,
            channel_id,
            now=now,
            cache=feed_cache,
        )
        if board is None:
            return None
        frame = config.canonical_profile
        width, height, fps = frame.width, frame.height, frame.fps
        branding = next(
            (p.branding for p in default_channel_profiles() if p.channel_id == channel_id),
            None,
        )
        background = (
            "0x" + branding.color.lstrip("#") if branding is not None else _DEFAULT_BACKGROUND
        )
        short_name = branding.short_name if branding is not None else ""
        airable = airable_bulletins(list(bulletin_store.list_approved(channel_id)), now=now)
        bulletin = airable[0] if airable else None
        key = board_segment_cache_key(
            board=board,
            bulletin=bulletin,
            background_color=background,
            station_short_name=short_name,
            segment_seconds=0.0,
            width=width,
            height=height,
            frame_rate=fps,
            now=now,
        )
        out_path = work_dir / channel_id / "board-overlay" / f"{key}.png"
        if not out_path.exists():
            out_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                ffmpeg_runner(
                    build_board_preview_args(
                        board=board,
                        bulletin=bulletin,
                        width=width,
                        height=height,
                        frame_rate=fps,
                        background_color=background,
                        station_short_name=short_name,
                        out_path=out_path,
                        include_text=True,
                        now=now,
                        image_resolver=image_resolver,
                    )
                )
            except Exception:
                _LOG.exception(
                    "channel %s: board overlay raster failed to render; airing without it.",
                    channel_id,
                )
                return None
        return out_path

    return provider


def build_bulletin_slide_args(
    *,
    output_path: Path,
    config: EgressConfig,
    bulletin: CgBulletinSubmission,
    branding: ChannelBranding | None,
    duration_seconds: int,
    include_text: bool = True,
) -> list[str]:
    """Build FFmpeg args for one canonical MPEG-TS bulletin slide."""

    profile = config.canonical_profile
    background = "0x" + branding.color.lstrip("#") if branding is not None else _DEFAULT_BACKGROUND
    video_input = (
        f"color=c={background}:size={profile.width}x{profile.height}"
        f":rate={profile.fps}:duration={duration_seconds}"
    )
    args = [
        "-f",
        "lavfi",
        "-i",
        video_input,
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r={profile.audio_sample_rate}:cl=stereo",
    ]
    if include_text:
        safe_width = max(1, int(profile.width * 0.9))
        title_font, title_lines, title_line_height = _fit_text_to_slot(
            bulletin.title, safe_width, max(1, int(profile.height * 0.22)), 40
        )
        body_font, body_lines, body_line_height = _fit_text_to_slot(
            bulletin.message, safe_width, max(1, int(profile.height * 0.40)), 26
        )
        org_font, org_lines, org_line_height = _fit_text_to_slot(
            bulletin.organization, safe_width, max(1, int(profile.height * 0.10)), 20
        )
        title_text = _escape_drawtext("\n".join(title_lines))
        body_text = _escape_drawtext("\n".join(body_lines))
        org_text = _escape_drawtext("\n".join(org_lines))
        filters = [
            (
                f"drawtext=expansion=none:text='{title_text}':fontsize={title_font}:fontcolor=white:"
                "box=1:boxcolor=black@0.4:boxborderw=10:x=(w-text_w)/2:y=h*0.18:"
                f"line_spacing={title_line_height - title_font}"
            ),
            (
                f"drawtext=expansion=none:text='{body_text}':fontsize={body_font}:fontcolor=white:"
                "box=1:boxcolor=black@0.4:boxborderw=8:x=(w-text_w)/2:y=h*0.42:"
                f"line_spacing={body_line_height - body_font}"
            ),
            (
                f"drawtext=expansion=none:text='{org_text}':fontsize={org_font}:fontcolor=white@0.85:"
                f"x=(w-text_w)/2:y=h*0.86:line_spacing={org_line_height - org_font}"
            ),
        ]
        if branding is not None:
            bug = _escape_drawtext(branding.short_name)
            filters.append(
                f"drawtext=expansion=none:text='{bug}':fontsize=22:fontcolor=white@0.85:"
                "box=1:boxcolor=black@0.35:boxborderw=6:x=w-text_w-24:y=24"
            )
        args.extend(["-vf", ",".join(filters)])
    args.extend(
        [
            "-c:v",
            profile.video_codec,
            "-b:v",
            f"{profile.video_bitrate_kbps}k",
            "-g",
            str(profile.gop_size),
            "-c:a",
            profile.audio_codec,
            "-b:a",
            f"{profile.audio_bitrate_kbps}k",
            "-ar",
            str(profile.audio_sample_rate),
            "-ac",
            str(profile.audio_channels),
            "-shortest",
            "-f",
            "mpegts",
            str(output_path),
        ]
    )
    return args
