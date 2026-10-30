# SPDX-License-Identifier: Apache-2.0
"""Render public emergency text for the existing GStreamer image compositor."""

from __future__ import annotations

import os
import textwrap
import uuid
from pathlib import Path
from typing import Any

from civiccast.cg.models import EmergencyOverlay
from civiccast.eas.models import EasDisplayMode
from civiccast.stream._ffmpeg import run_ffmpeg


def presentation_enabled() -> bool:
    return os.environ.get("CIVICCAST_EAS", "off").strip().lower() not in {"", "off", "0", "false"}


def render_presentation(
    mode: EasDisplayMode, overlay: EmergencyOverlay, directory: Path, *, width: int, height: int
) -> dict[str, Any]:
    """Rasterize UTF-8 text with the installed FFmpeg/fonts, without shell interpolation."""
    directory.mkdir(parents=True, exist_ok=True)
    image = directory / f"emergency-overlay.{uuid.uuid4().hex}.png"
    text_file = image.with_suffix(".txt")
    font_size = max(18, width // 60)
    margin = max(12, width // 50)
    # Instructions take priority over repeated headline/description text in the panel.
    text = f"{overlay.title}\n{overlay.instructions}\n{overlay.message}"
    if mode == "crawl":
        text = "  |  ".join(text.splitlines())
        limit = max(1, (16384 - margin * 2) // font_size)
        if len(text) > limit:
            more = " | More information: resident portal / local authorities"
            text = text[: limit - len(more)] + more
        render_width = max(width, len(text) * font_size + margin * 2)
        render_height = font_size * 3
    else:
        render_width = width
        render_height = height if mode == "forced_slate" else max(height // 2, font_size * 7)
        columns = max(15, int((width - 2 * margin) / (font_size * 0.65)))
        lines = []
        for paragraph in text.splitlines():
            lines.extend(textwrap.wrap(paragraph, width=columns))
        limit = max(2, (render_height - margin * 2) // (font_size + 5))
        if len(lines) > limit:
            lines = [*lines[: limit - 1], "More information: resident portal / local authorities"]
        text = "\n".join(lines)
    text_file.write_text(text, encoding="utf-8")

    def quoted(path: Path) -> str:
        return "'" + path.as_posix().replace(":", r"\:").replace("'", r"\'") + "'"

    font = ""
    if os.name == "nt":
        font = f"fontfile={quoted(Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts/segoeui.ttf')}:"
    try:
        run_ffmpeg(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x861f22:s={render_width}x{render_height}:r=1",
                "-vf",
                f"drawtext={font}textfile={quoted(text_file)}:expansion=none:fontcolor=white:fontsize={font_size}:line_spacing=5:x={margin}:y={margin}",
                "-frames:v",
                "1",
                "-threads",
                "1",
                "-y",
                str(image),
            ],
            timeout=8,
        )
    except Exception:
        image.unlink(missing_ok=True)
        raise
    finally:
        text_file.unlink(missing_ok=True)
    return {
        "mode": mode,
        "image_path": str(image),
        "width": render_width,
        "height": render_height,
        "ypos": 0 if mode == "forced_slate" else height - render_height,
        "canvas_width": width,
    }
