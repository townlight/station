# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Acquisition helpers for the reviewed, offline Whistle asset pair."""

from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from civiccast.native.app_payload import (
    WHISTLE_ENGINE_DLL_BYTES,
    WHISTLE_ENGINE_DLL_SHA256,
    WHISTLE_ENGINE_MEMBER,
    WHISTLE_ENGINE_WHEEL_BYTES,
    WHISTLE_ENGINE_WHEEL_SHA256,
    WHISTLE_ENGINE_WHEEL_URL,
    WHISTLE_MODEL_BYTES,
    WHISTLE_MODEL_SHA256,
    WHISTLE_MODEL_SOURCE_URL,
    WHISTLE_PACK_FILES,
)


class WhistleAssetError(RuntimeError):
    """A Whistle model or native-engine asset is missing or unreviewed."""


_DOWNLOAD_CHUNK_BYTES = 1024 * 1024


def _validate_https_url(url: str) -> None:
    """Reject non-HTTPS or malformed asset URLs before urllib handles them."""

    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError as exc:
        raise WhistleAssetError(f"Whistle asset URL is invalid: {url}") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise WhistleAssetError(f"Whistle asset URL must use HTTPS: {url}")


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep urllib redirects on TLS; content hashes authenticate the bytes."""

    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Any:
        _validate_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _matches(path: Path, *, size: int, sha256: str) -> bool:
    return path.is_file() and _file_identity(path) == (size, sha256)


def _write_atomic(destination: Path, body: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".partial",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def download_verified_file(
    url: str,
    destination: Path,
    *,
    expected_bytes: int,
    expected_sha256: str,
) -> Path:
    """Stream one HTTPS asset to disk and promote it only after exact checks."""

    if destination.exists() and _matches(destination, size=expected_bytes, sha256=expected_sha256):
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    digest = hashlib.sha256()
    size = 0
    try:
        _validate_https_url(url)
        request = urllib.request.Request(  # noqa: S310  # nosec B310 - URL and redirects are restricted to HTTPS
            url, headers={"User-Agent": "CivicCast-native-builder"}
        )
        opener = urllib.request.build_opener(_HttpsOnlyRedirectHandler())
        with (
            opener.open(request, timeout=120) as response,
            tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{destination.name}.",
                suffix=".partial",
                dir=destination.parent,
                delete=False,
            ) as handle,
        ):
            temporary = Path(handle.name)
            while True:
                read_limit = min(_DOWNLOAD_CHUNK_BYTES, expected_bytes - size + 1)
                chunk = response.read(read_limit)
                if not chunk:
                    break
                size += len(chunk)
                if size > expected_bytes:
                    raise WhistleAssetError(
                        f"Whistle asset from {url} exceeds its reviewed size of "
                        f"{expected_bytes} bytes"
                    )
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        actual = digest.hexdigest()
        if size != expected_bytes or actual != expected_sha256:
            raise WhistleAssetError(
                f"Whistle asset from {url} has {size} bytes SHA-256 {actual}; "
                f"expected {expected_bytes} bytes SHA-256 {expected_sha256}"
            )
        temporary.replace(destination)
        return destination
    except WhistleAssetError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    except Exception as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise WhistleAssetError(
            f"could not acquire pinned Whistle asset from {url}: {exc}"
        ) from exc


def download_engine_wheel(cache: Path) -> Path:
    """Acquire the pinned upstream Windows wheel used for its Needle DLL."""

    wheel = cache / "cactus_needle-3.1.0-py3-none-win_amd64.whl"
    return download_verified_file(
        WHISTLE_ENGINE_WHEEL_URL,
        wheel,
        expected_bytes=WHISTLE_ENGINE_WHEEL_BYTES,
        expected_sha256=WHISTLE_ENGINE_WHEEL_SHA256,
    )


def extract_engine_library(wheel: Path) -> bytes:
    """Read only the reviewed native DLL member from its exact pinned wheel."""

    if not _matches(
        wheel,
        size=WHISTLE_ENGINE_WHEEL_BYTES,
        sha256=WHISTLE_ENGINE_WHEEL_SHA256,
    ):
        raise WhistleAssetError("pinned cactus-needle wheel does not match its reviewed identity")
    try:
        with zipfile.ZipFile(wheel) as archive:
            members = [
                info for info in archive.infolist() if info.filename == WHISTLE_ENGINE_MEMBER
            ]
            if len(members) != 1:
                raise WhistleAssetError(
                    f"pinned cactus-needle wheel must contain exactly one {WHISTLE_ENGINE_MEMBER}"
                )
            info = members[0]
            if info.file_size != WHISTLE_ENGINE_DLL_BYTES:
                raise WhistleAssetError("pinned Needle DLL member has an unexpected size")
            body = archive.read(info)
    except WhistleAssetError:
        raise
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise WhistleAssetError(f"pinned cactus-needle wheel is unreadable: {exc}") from exc
    if (
        len(body) != WHISTLE_ENGINE_DLL_BYTES
        or hashlib.sha256(body).hexdigest() != WHISTLE_ENGINE_DLL_SHA256
    ):
        raise WhistleAssetError("pinned Needle DLL member does not match its reviewed identity")
    return body


def stage_engine_library(destination: Path, *, cache: Path) -> Path:
    """Stage the pinned DLL at a caller-selected canonical package/pack path."""

    if destination.exists():
        if _matches(
            destination,
            size=WHISTLE_ENGINE_DLL_BYTES,
            sha256=WHISTLE_ENGINE_DLL_SHA256,
        ):
            return destination
        raise WhistleAssetError(f"refusing to replace an unreviewed Needle DLL: {destination}")
    wheel = download_engine_wheel(cache)
    _write_atomic(destination, extract_engine_library(wheel))
    return destination


def provision_whistle_assets(output: Path, *, cache: Path) -> dict[str, Path]:
    """Write exactly ``whistle.cact`` and ``libneedle.dll`` to a pack root."""

    output.mkdir(parents=True, exist_ok=True)
    expected_names = set(WHISTLE_PACK_FILES)
    existing_names = {path.name for path in output.iterdir() if path.is_file()}
    unexpected = existing_names - expected_names
    if unexpected:
        raise WhistleAssetError(
            "Whistle asset root contains unexpected files: " + ", ".join(sorted(unexpected))
        )
    model = download_verified_file(
        WHISTLE_MODEL_SOURCE_URL,
        cache / "whistle.cact",
        expected_bytes=WHISTLE_MODEL_BYTES,
        expected_sha256=WHISTLE_MODEL_SHA256,
    )
    staged_model = output / "whistle.cact"
    if staged_model.exists() and not _matches(
        staged_model, size=WHISTLE_MODEL_BYTES, sha256=WHISTLE_MODEL_SHA256
    ):
        raise WhistleAssetError(
            f"refusing to replace unreviewed Whistle model bytes: {staged_model}"
        )
    if not staged_model.exists():
        _write_atomic(staged_model, model.read_bytes())

    engine = stage_engine_library(output / "libneedle.dll", cache=cache)
    return {"whistle.cact": staged_model, "libneedle.dll": engine}
