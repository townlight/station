# SPDX-License-Identifier: AGPL-3.0-only
"""Bring a saved Townlight meeting's direct recording into managed ingest."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from fastapi import HTTPException

from .boundary import private_path, sha256


def fetch_public(*args, **kwargs):
    # The shared reader is installed alongside Station in the Townlight suite.
    from townlight_runtime.meeting_import_fetch import fetch_public as fetch

    return fetch(*args, **kwargs)


def _publish_once(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except FileExistsError:
        if sha256(source) != sha256(target):
            raise HTTPException(409, "Managed import evidence changed. Ask a Station administrator to reconcile it.")


def stage_recording(settings, meeting, index, receipt):
    recordings = receipt["payload"].get("recordings", [])
    if not 0 <= index < len(recordings):
        raise HTTPException(404, "This recording is absent from the saved import.")
    recording = recordings[index]
    media_url = recording.get("video_url")
    if not media_url:
        raise HTTPException(422, "This source has a player link but no direct MP4 to import.")
    ingest = private_path(settings.data, "ingest")
    path = None
    receipt_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=ingest, delete=False) as temporary:
            path = Path(temporary.name)
            try:
                fetch_public(media_url, byte_limit=settings.max_bytes, destination=temporary, deadline_seconds=120)
            except (OSError, ValueError) as error:
                raise HTTPException(422, "Station could not download the public recording.") from error
        with path.open("rb") as video:
            if video.read(8)[4:] != b"ftyp":
                raise HTTPException(422, "The source did not serve an MP4 recording.")
        digest = sha256(path)
        identity = hashlib.sha256(f"{meeting}:{receipt['source_key']}:{index}".encode()).hexdigest()[:24]
        filename = f"townlight-import-{identity}-{digest}.mp4"
        provenance = {
            "meeting_id": str(meeting), "source_key": receipt["source_key"],
            "connector": receipt["source"]["connector"], "source_url": receipt["source"]["url"],
            "recording_index": index, "recording_url": media_url, "sha256": digest,
        }
        with tempfile.NamedTemporaryFile(dir=ingest, delete=False) as temporary:
            receipt_path = Path(temporary.name)
            temporary.write(json.dumps(provenance, sort_keys=True).encode())
        _publish_once(receipt_path, private_path(ingest, filename + ".json"))
        _publish_once(path, private_path(ingest, filename))
        return {"source_file": filename, "provenance": provenance}
    finally:
        for temporary in (path, receipt_path):
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def read_provenance(ingest, filename, meeting, digest):
    if not filename or not filename.startswith("townlight-import-"):
        return None
    try:
        receipt = json.loads(private_path(ingest, filename + ".json").read_bytes())
        if receipt["meeting_id"] != str(meeting) or receipt["sha256"] != digest:
            raise ValueError("receipt mismatch")
        return receipt
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise HTTPException(409, "The imported recording does not match this meeting and source file.") from error
