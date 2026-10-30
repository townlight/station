# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Render a ResolvedBoard into one composed ffmpeg board segment via ADR-0007.

The returned arguments exclude the ffmpeg binary because the ADR-0007 wrapper
prepends it before executing the composed board segment render.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from civiccast.cg.board_resolver import ResolvedBoard
from civiccast.cg.models import CgBulletinSubmission, CgZone
from civiccast.egress.source_plan import _escape_drawtext

_SAFE_AREA_PERCENT = 5
_SCHEDULE_ITEM_LIMIT = 3
#: Per-item cap on ticker titles before they're joined and drawn (gate
#: finding F-5, cosmetic/defensive): an unbounded feed-item title would
#: otherwise scroll a single, ever-longer line across the ticker forever.
_TICKER_ITEM_MAX_CHARS = 120

ImageResolver = Callable[[str], Path | None]


def build_board_segment_args(
    *,
    board: ResolvedBoard,
    bulletin: CgBulletinSubmission | None,
    width: int,
    height: int,
    frame_rate: int,
    background_color: str,
    station_short_name: str,
    segment_seconds: float,
    out_path: Path,
    include_text: bool,
    now: datetime,
    image_resolver: ImageResolver,
    codec_args: list[str] | tuple[str, ...] | None = None,
    audio_path: Path | None = None,
    audio_offset_seconds: float = 0.0,
    audio_gain_db: float = 0.0,
    audio_sample_rate: int = 48000,
    audio_channels: int = 2,
    audio_codec: str = "aac",
    audio_bitrate_kbps: int = 128,
) -> list[str]:
    """Build wrapper-ready ffmpeg args for one multi-zone board segment."""

    args = _build_board_render_args(
        board=board,
        bulletin=bulletin,
        width=width,
        height=height,
        frame_rate=frame_rate,
        background_color=background_color,
        station_short_name=station_short_name,
        segment_seconds=segment_seconds,
        now=now,
        image_resolver=image_resolver,
        include_text=include_text,
    )
    audio_index = args.count("-i")
    audio_input = (
        ["-stream_loop", "-1", "-ss", str(audio_offset_seconds), "-i", str(audio_path)]
        if audio_path is not None
        else ["-f", "lavfi", "-i", f"anullsrc=r={audio_sample_rate}:cl=stereo"]
    )
    args[args.index("-filter_complex") : args.index("-filter_complex")] = audio_input
    filter_index = args.index("-filter_complex") + 1
    audio_filter = (
        f"[{audio_index}:a]aresample={audio_sample_rate},"
        f"loudnorm=I=-23:TP=-2:LRA=7,volume={audio_gain_db}dB,"
        "alimiter=limit=0.89:level=false[aout]"
        if audio_path is not None
        else f"[{audio_index}:a]anull[aout]"
    )
    args[filter_index] += f";{audio_filter}"
    args.extend(["-map", "[aout]"])
    args.extend(
        [
            "-t",
            str(segment_seconds),
            "-r",
            str(frame_rate),
            *list(codec_args if codec_args is not None else ["-c:v", "mpeg2video"]),
            "-c:a",
            audio_codec,
            "-b:a",
            f"{audio_bitrate_kbps}k",
            "-ar",
            str(audio_sample_rate),
            "-ac",
            str(audio_channels),
            "-f",
            "mpegts",
            "-y",
            str(out_path),
        ]
    )
    return args


def build_board_preview_args(
    *,
    board: ResolvedBoard,
    bulletin: CgBulletinSubmission | None,
    width: int,
    height: int,
    frame_rate: int,
    background_color: str,
    station_short_name: str,
    out_path: Path,
    include_text: bool,
    now: datetime,
    image_resolver: ImageResolver,
) -> list[str]:
    """Build wrapper-ready ffmpeg args for one PNG board preview frame."""

    args = _build_board_render_args(
        board=board,
        bulletin=bulletin,
        width=width,
        height=height,
        frame_rate=frame_rate,
        background_color=background_color,
        station_short_name=station_short_name,
        segment_seconds=1.0,
        now=now,
        image_resolver=image_resolver,
        include_text=include_text,
    )
    args.extend(
        [
            "-frames:v",
            "1",
            "-f",
            "image2",
            "-pix_fmt",
            "rgb24",
            "-vcodec",
            "png",
            "-y",
            str(out_path),
        ]
    )
    return args


def board_segment_cache_key(
    *,
    board: ResolvedBoard,
    bulletin: CgBulletinSubmission | None,
    background_color: str,
    station_short_name: str,
    segment_seconds: float,
    width: int,
    height: int,
    frame_rate: int,
    now: datetime,
    audio_asset_ref: str | None = None,
    audio_gain_db: float = 0.0,
    audio_source_version: str | None = None,
    audio_offset_seconds: float = 0.0,
) -> str:
    """Return the stable content-addressed cache key for one board segment."""

    material: dict[str, object] = {
        "text_renderer": "text-fit-v1",
        "board": {
            "board_id": board.board_id,
            "snapshot": board.snapshot.model_dump(mode="json"),
            "backfilled_kinds": list(board.backfilled_kinds),
            "degraded_zone_ids": list(board.degraded_zone_ids),
        },
        "bulletin": None if bulletin is None else bulletin.model_dump(mode="json"),
        "branding": {
            "background_color": background_color,
            "station_short_name": station_short_name,
        },
        "geometry": {
            "width": width,
            "height": height,
            "frame_rate": frame_rate,
            "segment_seconds": segment_seconds,
            "safe_area_pixels": _safe_area_pixels(width, height),
        },
        "audio": {
            "asset_ref": audio_asset_ref,
            "gain_db": audio_gain_db,
            "source_version": audio_source_version,
            "offset_seconds": audio_offset_seconds,
        },
    }
    if _has_clock_zone(board):
        material["clock_minute"] = now.replace(second=0, microsecond=0).isoformat()
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _build_board_render_args(
    *,
    board: ResolvedBoard,
    bulletin: CgBulletinSubmission | None,
    width: int,
    height: int,
    frame_rate: int,
    background_color: str,
    station_short_name: str,
    segment_seconds: float,
    now: datetime,
    image_resolver: ImageResolver,
    include_text: bool,
) -> list[str]:
    args = [
        "-f",
        "lavfi",
        "-i",
        (f"color=c={background_color}:s={width}x{height}:r={frame_rate}:d={segment_seconds}"),
    ]
    filters: list[str] = []
    current = "0:v"
    image_input_index = 1
    zones_by_kind = {zone.kind: zone for zone in board.snapshot.zones}
    safe_area_pixels = _safe_area_pixels(width, height)

    for region in sorted(board.snapshot.template.regions, key=lambda item: item.order):
        box = _region_box(
            region.region,
            width=width,
            height=height,
            safe_area_pixels=safe_area_pixels,
        )
        zone = zones_by_kind.get(region.zone_kind)
        if zone is None:
            continue
        if zone.kind == "logo":
            image_path = _resolve_zone_image(zone, image_resolver)
            if image_path is not None:
                args.extend(["-loop", "1", "-i", str(image_path)])
                output = f"logo_{image_input_index}"
                x, y, box_width, box_height = box
                filters.append(f"[{image_input_index}:v]scale={box_width}:{box_height}[{output}]")
                filters.append(f"[{current}][{output}]overlay=x={x}:y={y}[v_{image_input_index}]")
                current = f"v_{image_input_index}"
                image_input_index += 1
            continue
        if not include_text:
            continue
        texts = _zone_text(
            zone,
            bulletin=bulletin,
            station_short_name=station_short_name,
            now=now,
        )
        line_count = len(texts)
        for text, line_index, scroll in texts:
            output = f"text_{len(filters)}"
            filters.append(
                _drawtext_filter(
                    current,
                    output,
                    text=text,
                    box=box,
                    line_index=line_index,
                    scroll=scroll,
                    line_count=line_count,
                )
            )
            current = output

    filters.append(f"[{current}]format=yuv420p[vout]")
    args.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
        ]
    )
    return args


def _safe_area_pixels(width: int, height: int) -> int:
    return max(1, min(width, height) * _SAFE_AREA_PERCENT // 100)


def _region_box(
    region: str,
    *,
    width: int,
    height: int,
    safe_area_pixels: int,
) -> tuple[int, int, int, int]:
    """Return the local ADR-compatible region geometry for a board zone."""

    safe = safe_area_pixels
    if region == "main":
        return safe, safe, max(1, width - (safe * 2)), max(1, height - (safe * 2))
    if region == "lower":
        lower_height = max(1, height // 5)
        return safe, height - safe - lower_height, max(1, width - (safe * 2)), lower_height
    if region == "side":
        side_width = max(1, width // 4)
        return width - safe - side_width, safe, side_width, max(1, height - (safe * 2))
    if region == "bug":
        bug_size = max(1, min(width, height) // 8)
        return width - safe - bug_size, safe, bug_size, bug_size
    if region == "background":
        return 0, 0, width, height
    raise ValueError(f"unsupported CG template region: {region}")


def _resolve_zone_image(zone: CgZone, image_resolver: ImageResolver) -> Path | None:
    asset_ref = zone.content.get("image_asset_ref")
    if not isinstance(asset_ref, str) or not asset_ref:
        return None
    return image_resolver(asset_ref)


def _zone_text(
    zone: CgZone,
    *,
    bulletin: CgBulletinSubmission | None,
    station_short_name: str,
    now: datetime,
) -> list[tuple[str, int, bool]]:
    if zone.kind == "primary":
        manual_text = zone.content.get("text")
        if zone.source == "manual" and isinstance(manual_text, str) and manual_text.strip():
            return [(manual_text.strip(), 0, False)]
        if bulletin is None:
            return []
        return [
            (bulletin.title, 0, False),
            (bulletin.organization, 1, False),
            (bulletin.message, 2, False),
        ]
    if zone.kind == "ticker":
        items = zone.content.get("items", [])
        titles = (
            [item.get("title") for item in items if isinstance(item, dict)]
            if isinstance(items, list)
            else []
        )
        if not titles:
            manual_text = zone.content.get("text")
            if isinstance(manual_text, str) and manual_text.strip():
                return [(_cap_ticker_item(manual_text.strip()), 0, True)]
            return []
        capped = [_cap_ticker_item(title) for title in titles if isinstance(title, str)]
        text = "   |   ".join(capped)
        return [(text, 0, True)] if text else []
    if zone.kind == "schedule":
        if zone.content == {"mode": "clock"}:
            return [(f"{station_short_name}  {now.strftime('%H:%M')}", 0, False)]
        items = zone.content.get("items", [])
        if not isinstance(items, list):
            return []
        lines: list[tuple[str, int, bool]] = []
        for line_index, item in enumerate(items[:_SCHEDULE_ITEM_LIMIT]):
            if not isinstance(item, dict):
                continue
            time = item.get("time")
            title = item.get("title")
            if isinstance(time, str) and isinstance(title, str):
                lines.append((f"{time}  {title}", line_index, False))
        return lines
    return []


def _cap_ticker_item(title: str) -> str:
    """Cap one ticker item's title before it's joined into the scroll line.

    Cosmetic/defensive (gate finding F-5): an unbounded feed-item title would
    scroll a single, ever-longer line across the ticker with no upper bound.
    """
    if len(title) <= _TICKER_ITEM_MAX_CHARS:
        return title
    return title[: _TICKER_ITEM_MAX_CHARS - 1].rstrip() + "…"


def _fit_text_to_slot(
    text: str, slot_width: int, slot_height: int, max_font_size: int
) -> tuple[int, list[str], int]:
    import textwrap

    def wrap(text: str, width: int) -> list[str]:
        lines = []
        for paragraph in text.split("\n"):
            if paragraph == "":
                lines.append("")
            else:
                wrapped = textwrap.wrap(
                    paragraph,
                    width=width,
                    break_long_words=True,
                    break_on_hyphens=False,
                    replace_whitespace=False,
                    drop_whitespace=False,
                )
                if not wrapped:
                    lines.append("")
                else:
                    lines.extend(wrapped)
        return lines or [""]

    if "\n" not in text:
        single_size = min(max_font_size, slot_width // max(1, len(text)), slot_height)
        if single_size >= 12:
            line_spacing = max(0, min(8, single_size // 2))
            line_height = single_size + line_spacing
            if line_height <= slot_height:
                return single_size, [text], line_height

    for font_size in range(max_font_size, 0, -1):
        chars_per_line = max(1, slot_width // font_size)
        lines = wrap(text, chars_per_line)
        line_spacing = max(0, min(8, font_size // 2))
        line_height = font_size + line_spacing
        if len(lines) * line_height <= slot_height:
            return font_size, lines, line_height
    chars_per_line = max(1, slot_width)
    lines = wrap(text, chars_per_line)
    return 1, lines, 1


def _drawtext_filter(
    input_label: str,
    output_label: str,
    *,
    text: str,
    box: tuple[int, int, int, int],
    line_index: int,
    scroll: bool,
    line_count: int = 1,
) -> str:
    if scroll:
        x, y, box_width, box_height = box
        escaped_text = _escape_drawtext(text)
        font_size = max(18, min(box_height // 7, box_width // 20))
        y_position = y + min(box_height - font_size, line_index * (font_size + 8))
        x_expression = f"{x}+{box_width}-mod(t*100,{box_width}+text_w)"
        return (
            f"[{input_label}]drawtext=expansion=none:text='{escaped_text}':"
            f"x='{x_expression}':y={y_position}:"
            f"fontsize={font_size}:fontcolor=white:box=1:boxcolor=black@0.45[{output_label}]"
        )
    x, y, box_width, box_height = box
    slot_y_start = y + (line_index * box_height) // max(1, line_count)
    slot_y_end = y + ((line_index + 1) * box_height) // max(1, line_count)
    slot_height = slot_y_end - slot_y_start
    max_font_size = max(18, min(box_height // 7, box_width // 20))
    font_size, lines, line_height = _fit_text_to_slot(text, box_width, slot_height, max_font_size)
    filters = []
    for i, line in enumerate(lines):
        escaped_line = _escape_drawtext(line)
        y_pos = slot_y_start + i * line_height
        input_lbl = input_label if i == 0 else f"{output_label}_line{i - 1}"
        output_lbl = output_label if i == len(lines) - 1 else f"{output_label}_line{i}"
        filters.append(
            f"[{input_lbl}]drawtext=expansion=none:text='{escaped_line}':"
            f"x='{x}':y={y_pos}:"
            f"fontsize={font_size}:fontcolor=white:box=1:boxcolor=black@0.45[{output_lbl}]"
        )
    return ";".join(filters)


def _has_clock_zone(board: ResolvedBoard) -> bool:
    return any(zone.content == {"mode": "clock"} for zone in board.snapshot.zones)
