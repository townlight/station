# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S15 graphics-overlay leg: build a station bug/logo + lower-third text banner
composite for ``GstPlayoutEngine`` (see ``graph.GraphicsOverlayLeg``).

Gi-free (importable on Windows without ``gi``, mirroring ``graph.py``'s own
constraint) — everything here is stdlib: a minimal PNG writer (``zlib`` + ``struct``)
and a small built-in 5x7 bitmap font for the lower-third banner text. The bundled
native-Windows GStreamer runtime ships no text-rendering element at all (no
``textoverlay``/pango, no ``cairo``/``rsvg`` — confirmed by a real ``gst-inspect``
enumeration of the installed runtime, see the PR notes), so the banner text is
rasterized here, in Python, into an RGBA PNG that rides the SAME real-alpha-decode
compositor path (``filesrc ! decodebin ! ... ! d3d11compositor``) as an
operator-supplied logo PNG — one code path for both overlay kinds, not a second
"fake" rendering mechanism.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from civiccast.egress.gst.graph import GraphicsOverlayLayer, GraphicsOverlayLeg

#: The compositor element this leg's ``GraphicsOverlayLeg`` defaults to. The bundled
#: runtime ships no plain ``compositor``/``videomixer`` — only the D3D11/D3D12
#: hardware family — so this is the one real, provable compositor on this product's
#: curated GStreamer install.
GRAPHICS_OVERLAY_ELEMENT = "d3d11compositor"

RGBA = tuple[int, int, int, int]


def write_rgba_png(path: str | Path, width: int, height: int, rgba: bytes) -> None:
    """Write a raw RGBA buffer (``width * height * 4`` bytes, row-major, top-down) as
    a standard 8-bit RGBA PNG. Stdlib-only (``zlib`` deflate + ``struct`` chunk
    framing) — no Pillow/other imaging dependency, so the graphics-overlay leg has no
    new third-party dependency at all."""
    if len(rgba) != width * height * 4:
        raise ValueError(
            f"rgba buffer is {len(rgba)} bytes, expected {width * height * 4} "
            f"for {width}x{height} RGBA"
        )

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (none) per scanline
        raw += rgba[y * stride : (y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # colour type 6 = RGBA
    idat = zlib.compress(bytes(raw), 9)
    out = bytearray(b"\x89PNG\r\n\x1a\n")
    out += chunk(b"IHDR", ihdr)
    out += chunk(b"IDAT", idat)
    out += chunk(b"IEND", b"")
    Path(path).write_bytes(bytes(out))


# --- built-in 5x7 bitmap font (lower-third banner text) -----------------------------
#
# A small, deliberately plain block font this project owns outright (not extracted
# from any system/bundled font file — the bundled GStreamer runtime ships none, and
# pulling in a real typeface would be a licensing question this increment doesn't
# need to open). Uppercase letters, digits, space, and the punctuation a station
# banner actually needs. Unknown characters render as a blank cell rather than
# raising — a bad character must never take a channel's graphics overlay down.
_FONT_ROWS = 7
_FONT_COLS = 5
_FONT: dict[str, tuple[str, ...]] = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "11110", "10001", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "11110", "10000", "10000", "10000", "11111"),
    "F": ("11111", "10000", "11110", "10000", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10011", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10101", "10011", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "0": ("01110", "10011", "10101", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00110", "01000", "10000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    ".": ("00000", "00000", "00000", "00000", "00000", "00000", "00100"),
    ",": ("00000", "00000", "00000", "00000", "00000", "00100", "01000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ":": ("00000", "00100", "00000", "00000", "00000", "00100", "00000"),
    "'": ("00100", "00100", "00000", "00000", "00000", "00000", "00000"),
    "!": ("00100", "00100", "00100", "00100", "00100", "00000", "00100"),
    "?": ("01110", "10001", "00001", "00010", "00100", "00000", "00100"),
    "/": ("00001", "00010", "00010", "00100", "01000", "01000", "10000"),
    "&": ("01100", "10010", "10100", "01000", "10101", "10010", "01101"),
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
}


def render_lower_third_png(
    path: str | Path,
    text: str,
    *,
    canvas_width: int,
    banner_height: int = 60,
    scale: int = 4,
    fg: RGBA = (255, 255, 255, 255),
    bg: RGBA = (10, 20, 60, 200),
    margin: int = 16,
) -> tuple[int, int]:
    """Rasterize ``text`` into a lower-third banner PNG: a solid (translucent) bar
    ``canvas_width`` x ``banner_height`` with the text left-aligned in the built-in
    5x7 font, scaled by ``scale``. Returns the written ``(width, height)``. Unsupported
    characters (anything not in the built-in font, case-folded to upper) render as a
    blank cell — never raises on odd input, since a bad banner string must not be able
    to take graphics overlay down."""
    if canvas_width <= 0 or banner_height <= 0:
        raise ValueError("render_lower_third_png requires a positive canvas size")
    width, height = canvas_width, banner_height
    buf = bytearray(bg * (width * height))
    cell_w = (_FONT_COLS + 1) * scale
    cell_h = _FONT_ROWS * scale
    y0 = max(0, (height - cell_h) // 2)
    x = margin
    for ch in text.upper():
        glyph = _FONT.get(ch, _FONT[" "])
        if x + _FONT_COLS * scale > width - margin:
            break  # never overflow the banner canvas
        for row_index, row in enumerate(glyph):
            for col_index, bit in enumerate(row):
                if bit != "1":
                    continue
                px0 = x + col_index * scale
                py0 = y0 + row_index * scale
                for dy in range(scale):
                    py = py0 + dy
                    if not 0 <= py < height:
                        continue
                    row_off = py * width * 4
                    for dx in range(scale):
                        px = px0 + dx
                        if not 0 <= px < width:
                            continue
                        off = row_off + px * 4
                        buf[off : off + 4] = bytes(fg)
        x += cell_w
    write_rgba_png(path, width, height, bytes(buf))
    return (width, height)


def station_bug_and_lower_third_leg(
    *,
    logo_path: str | Path,
    logo_corner: str = "top-left",
    logo_width: int = 160,
    logo_height: int = 0,
    logo_alpha: float = 0.9,
    canvas_width: int,
    canvas_height: int,
    banner_text: str | None = None,
    banner_path: str | Path | None = None,
    banner_height: int = 60,
    margin: int = 24,
) -> GraphicsOverlayLeg:
    """Build the two-layer graphics-overlay leg this increment ships: a station
    bug/logo PNG pinned to a configurable corner, and (when ``banner_text`` is given)
    a lower-third text banner rendered by ``render_lower_third_png`` into
    ``banner_path`` (default: alongside the logo, named ``lower_third.png``).

    ``logo_corner`` is one of ``top-left``/``top-right``/``bottom-left``/
    ``bottom-right`` — position is computed against ``canvas_width``/``canvas_height``
    (the program video's output size) so the bug lands fully on-canvas regardless of
    the configured encoder resolution.
    """
    valid_corners = {"top-left", "top-right", "bottom-left", "bottom-right"}
    if logo_corner not in valid_corners:
        raise ValueError(f"logo_corner must be one of {sorted(valid_corners)}, got {logo_corner!r}")
    if logo_width <= 0:
        raise ValueError("logo_width must be positive")

    # logo_height=0 keeps the decoded image's native aspect (compositor pad height=0
    # is not scaled — see GraphicsOverlayLayer's width/height=0 contract).
    effective_logo_h = logo_height if logo_height else logo_width
    xpos = margin if "left" in logo_corner else max(margin, canvas_width - logo_width - margin)
    ypos = (
        margin if "top" in logo_corner else max(margin, canvas_height - effective_logo_h - margin)
    )

    layers = [
        GraphicsOverlayLayer(
            name="station_bug",
            image_path=str(logo_path),
            xpos=xpos,
            ypos=ypos,
            width=logo_width,
            height=logo_height,
            alpha=logo_alpha,
        )
    ]
    if banner_text is not None:
        resolved_banner_path = (
            Path(banner_path)
            if banner_path is not None
            else Path(logo_path).with_name("lower_third.png")
        )
        render_lower_third_png(
            resolved_banner_path,
            banner_text,
            canvas_width=canvas_width,
            banner_height=banner_height,
        )
        layers.append(
            GraphicsOverlayLayer(
                name="lower_third",
                image_path=str(resolved_banner_path),
                xpos=0,
                ypos=canvas_height - banner_height,
                width=canvas_width,
                height=banner_height,
                alpha=1.0,
            )
        )
    return GraphicsOverlayLeg(layers=tuple(layers))
