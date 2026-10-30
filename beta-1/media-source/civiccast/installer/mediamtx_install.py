# SPDX-License-Identifier: Apache-2.0
"""Pinned Windows MediaMTX relay for remote guest publishing and mixes."""

from __future__ import annotations

import hashlib
import os
import shutil
import urllib.request
import zipfile
from pathlib import Path

VERSION = "v1.21.1"
WINDOWS_ZIP_URL = (
    "https://github.com/bluenviron/mediamtx/releases/download/"
    f"{VERSION}/mediamtx_{VERSION}_windows_amd64.zip"
)
WINDOWS_ZIP_SHA256 = "faa97974861eb75a68b5aa326c78e7e7a6f670b5ef191bace78e715130381f23"
WINDOWS_EXE_SHA256 = "7de0b5c1060f716cc813c6fab302c938f1fe7faccbd92eba6567f3bf18c9054e"
LICENSE_SHA256 = "ecae73b0a23185a35a1222edc0aae5245bb9739420bad0125b0c42e935301a80"

# RTSP and SRT carry local media. The browser-facing WHIP listener needs the
# operator's HTTPS reverse proxy and reachable WebRTC candidates.
CONFIG = """rtsp: yes
rtspAddress: 127.0.0.1:8554
rtspTransports: [tcp]
rtmp: no
hls: no
srt: yes
srtAddress: 127.0.0.1:8890
webrtc: yes
webrtcAddress: 127.0.0.1:8889
moq: no
paths:
  all_others:
    source: publisher
"""


def managed_relay_dir() -> Path:
    override = os.environ.get("CIVICCAST_REMOTE_CONTRIBUTION_HOME")
    if override:
        return Path(override).expanduser() / "mediamtx"
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return root / "CivicCast" / "remote-contribution" / "mediamtx"
    return (
        Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        / "civiccast"
        / "remote-contribution"
        / "mediamtx"
    )


def relay_paths(dest: Path | None = None) -> tuple[Path, Path]:
    target = dest or managed_relay_dir()
    return target / "mediamtx.exe", target / "mediamtx.yml"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relay_verified(dest: Path | None = None) -> bool:
    executable, config = relay_paths(dest)
    license_path = executable.parent / "LICENSE"
    return (
        executable.is_file()
        and config.is_file()
        and license_path.is_file()
        and _file_sha256(executable) == WINDOWS_EXE_SHA256
        and _file_sha256(license_path) == LICENSE_SHA256
    )


def install_mediamtx(*, dest: Path | None = None) -> Path:
    """Install the exact Windows release; never activate unchecked bytes."""
    if os.name != "nt":
        raise RuntimeError("Install MediaMTX with the host OS package on this platform.")
    target = dest or managed_relay_dir()
    if relay_verified(target):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    try:
        archive = staging / "release.zip"
        with (
            urllib.request.urlopen(WINDOWS_ZIP_URL, timeout=60) as response,  # noqa: S310  # nosec B310 -- fixed HTTPS URL and pinned archive hash
            archive.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)
        if _file_sha256(archive) != WINDOWS_ZIP_SHA256:
            raise RuntimeError("MediaMTX archive checksum mismatch; installation refused.")
        with (
            zipfile.ZipFile(archive) as package,
            package.open("mediamtx.exe") as source,
            (staging / "mediamtx.exe").open("wb") as output,
        ):
            shutil.copyfileobj(source, output)
        with (
            zipfile.ZipFile(archive) as package,
            package.open("LICENSE") as source,
            (staging / "LICENSE").open("wb") as output,
        ):
            shutil.copyfileobj(source, output)
        if _file_sha256(staging / "mediamtx.exe") != WINDOWS_EXE_SHA256:
            raise RuntimeError("MediaMTX executable checksum mismatch; installation refused.")
        if _file_sha256(staging / "LICENSE") != LICENSE_SHA256:
            raise RuntimeError("MediaMTX license checksum mismatch; installation refused.")
        (staging / "mediamtx.yml").write_text(CONFIG, encoding="utf-8")
        archive.unlink()
        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)
