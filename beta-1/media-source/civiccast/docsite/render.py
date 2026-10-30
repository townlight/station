# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Small HTML helpers shared by the docsite build script and its tests.

HTML parsing uses the standard library. Pillow is imported only when the
build-time image embedder runs; the request-time service can import
``sanitize_html`` and ``extract_toc`` without a documentation dependency.
"""

from __future__ import annotations

import base64
import io
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}

_IMG_SRC_RE = re.compile(r'(<img\b[^>]*\bsrc=")([^"]+)(")')


def embed_local_images(html: str, base_dir: Path) -> str:
    """Inline local images as base64 data URIs, compressing raster images to WebP.

    Pandoc leaves an image reference exactly as written in the Markdown
    source (``docs/USER-MANUAL.md``'s images use paths relative to ``docs/``)
    -- there is no
    server-relative path that would resolve once this HTML is embedded in
    ``civiccast/docsite/manual.json`` and served from ``/api/public/manual``
    with no filesystem underneath it, and ``render.py``'s own sanitizer
    (deliberately) refuses any ``src`` that isn't ``http(s)``, ``#``,
    ``mailto:``, ``/``, or already a ``data:image/`` URI -- so an
    unresolved relative path would silently render as a broken image (PR
    #74 review). Embedding once here, at commit-render time, keeps the
    manual fully self-contained and working with no internet connection,
    matching the rest of this pipeline's offline-first posture. WebP quality
    85 keeps the complete built-in manual below the 5 MiB source-blob limit;
    pixel dimensions are checked after conversion. SVG and animated GIF
    sources keep their original image format.

    An image this can't resolve (missing file, remote URL, already a data
    URI) is left exactly as-is -- sanitize_html's existing allowlist is the
    backstop that decides whether an untouched src ultimately survives.
    """

    def _data_uri(candidate: Path, mime: str) -> str:
        raw = candidate.read_bytes()
        if candidate.suffix.lower() == ".svg":
            return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

        # Build-only dependency: the docsite service reads pre-rendered HTML
        # and never calls this image conversion path.
        from PIL import Image

        try:
            with Image.open(candidate) as source:
                if getattr(source, "is_animated", False) and getattr(source, "n_frames", 1) > 1:
                    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
                original_size = source.size
                has_alpha = "A" in source.getbands() or "transparency" in source.info
                raster = source.convert("RGBA" if has_alpha else "RGB")
                optimized = io.BytesIO()
                raster.save(optimized, format="WEBP", quality=85, method=6)
            optimized_bytes = optimized.getvalue()
            with Image.open(io.BytesIO(optimized_bytes)) as decoded:
                if decoded.size != original_size:
                    raise ValueError(
                        f"WebP dimensions {decoded.size} differ from source {original_size}"
                    )
        except Exception as exc:
            raise RuntimeError(f"could not optimize manual image {candidate}: {exc}") from exc
        return f"data:image/webp;base64,{base64.b64encode(optimized_bytes).decode('ascii')}"

    def _replace(match: re.Match[str]) -> str:
        prefix, src, suffix = match.group(1), match.group(2), match.group(3)
        if re.match(r"^(https?://|data:|#|mailto:|/)", src, re.IGNORECASE):
            return match.group(0)
        candidate = (base_dir / src).resolve()
        try:
            candidate.relative_to(base_dir.resolve())
        except ValueError:
            return match.group(0)  # refuse to embed anything outside base_dir
        mime = _MIME_BY_SUFFIX.get(candidate.suffix.lower())
        if mime is None or not candidate.is_file():
            return match.group(0)
        return f"{prefix}{_data_uri(candidate, mime)}{suffix}"

    return _IMG_SRC_RE.sub(_replace, html)


#: Allowlisted tags. Pandoc's HTML5 writer for this manual only ever emits
#: prose/table/list markup (plus the odd inline image) -- no forms, no
#: scripts, no iframes. Anything else is dropped, not merely its attributes.
_ALLOWED_TAGS = frozenset(
    {
        "p",
        "br",
        "hr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "li",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "a",
        "strong",
        "em",
        "code",
        "pre",
        "blockquote",
        "img",
        # Pandoc wraps every standalone Markdown image (docs/USER-MANUAL.md
        # has two: the system and egress-proof architecture diagrams) in
        # <figure>/<figcaption>. An earlier version of this allowlist did
        # not include them, and _SanitizingParser drops a disallowed tag
        # together with all of its content -- so both diagrams AND their
        # caption text silently vanished from the in-product manual (PR
        # #74 review).
        "figure",
        "figcaption",
        "span",
        "div",
        "sup",
        "sub",
        "del",
    }
)

#: Allowlisted attributes per tag. Every other attribute (in particular any
#: ``on*`` event handler, ``style``, or ``srcdoc``) is stripped even on an
#: otherwise-allowed tag.
_ALLOWED_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "id", "title"}),
    "img": frozenset({"src", "alt", "title", "width", "height"}),
    "*": frozenset({"id", "class", "aria-hidden"}),
}

#: Schemes an ``href``/``src`` may use. Blocks ``javascript:``, ``data:``
#: (except images, handled separately), and anything else unexpected.
_SAFE_URL_PREFIXES = ("http://", "https://", "#", "mailto:", "/")


def _safe_url(value: str) -> str | None:
    stripped = value.strip()
    lowered = stripped.lower()
    if lowered.startswith("data:image/"):
        return stripped
    if any(lowered.startswith(prefix) for prefix in _SAFE_URL_PREFIXES):
        return stripped
    return None


class _SanitizingParser(HTMLParser):
    """Rebuilds an allowlisted-only version of the input HTML.

    Disallowed tags (``<script>``, ``<style>``, ``<iframe>``, ``<form>``,
    ...) are dropped along with all of their content -- not just unwrapped --
    so ``<script>alert(1)</script>`` never reaches the client as inline text
    either.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._emit(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._emit(tag, attrs, self_closing=True)

    def _emit(self, tag: str, attrs: list[tuple[str, str | None]], *, self_closing: bool) -> None:
        if tag not in _ALLOWED_TAGS:
            # A self-closing/void tag (e.g. pandoc's <col style="..." />)
            # never gets a matching handle_endtag call, so it must NOT bump
            # skip_depth -- doing so would leave the parser stuck skipping
            # every subsequent tag for the rest of the document, since
            # nothing would ever decrement it back down.
            if not self_closing:
                self._skip_depth += 1
            return
        if self._skip_depth:
            return
        allowed_names = _ALLOWED_ATTRS.get(tag, frozenset()) | _ALLOWED_ATTRS["*"]
        kept: list[str] = []
        for name, value in attrs:
            if name not in allowed_names or value is None:
                continue
            if name in ("href", "src"):
                safe = _safe_url(value)
                if safe is None:
                    continue
                value = safe
            escaped = value.replace("&", "&amp;").replace('"', "&quot;")
            kept.append(f' {name}="{escaped}"')
        self._out.append(f"<{tag}{''.join(kept)}{' /' if self_closing else ''}>")

    def handle_endtag(self, tag: str) -> None:
        if tag not in _ALLOWED_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        self._out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        escaped = data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self._out.append(escaped)

    def get_html(self) -> str:
        return "".join(self._out)


def sanitize_html(raw_html: str) -> str:
    """Return an allowlisted-tag/attribute copy of ``raw_html``.

    Defense in depth: ``raw_html`` is pandoc's own rendering of a markdown
    file this repository controls and reviews via PR, not arbitrary user
    input. This still runs so a stray raw-HTML block in the source (or a
    future pandoc extension) can never smuggle a ``<script>`` or an
    ``onerror=`` handler into the operator console.
    """

    parser = _SanitizingParser()
    parser.feed(raw_html)
    parser.close()
    return parser.get_html()


class _HeadingCollector(HTMLParser):
    """Collects ``(level, id, title)`` for every ``id``-bearing heading."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headings: list[dict[str, Any]] = []
        self._active: dict[str, Any] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
            heading_id = dict(attrs).get("id")
            if heading_id:
                self._active = {"level": int(tag[1]), "id": heading_id}
                self._text = []

    def handle_data(self, data: str) -> None:
        if self._active is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._active is not None and tag == f"h{self._active['level']}":
            title = "".join(self._text).strip()
            if title:
                self.headings.append({**self._active, "title": title})
            self._active = None


def extract_toc(sanitized_html: str) -> list[dict[str, Any]]:
    """Return a flat table of contents from already-sanitized HTML.

    Only headings that carry an ``id`` are included -- every heading in
    ``docs/USER-MANUAL.md`` either has an explicit ``{#anchor}`` or gets one
    of pandoc's auto-generated (GitHub-style) slugs, so this should be every
    heading, but a heading pandoc could not give an id to (vanishingly
    unlikely) is silently omitted from navigation rather than crashing the
    build.
    """

    collector = _HeadingCollector()
    collector.feed(sanitized_html)
    collector.close()
    return collector.headings
