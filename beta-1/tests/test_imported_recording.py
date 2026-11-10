from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from townlight_station.imported_recording import stage_recording


def test_stages_only_direct_media_and_keeps_the_import_identity(tmp_path, monkeypatch):
    meeting = uuid4()
    (tmp_path / "ingest").mkdir()
    source = "https://publisher.example/agenda"
    video = "https://publisher.example/meeting.mp4"
    receipt = {
        "source_key": "a" * 64,
        "source": {"url": source, "connector": "granicus"},
        "payload": {"recordings": [{"url": "https://publisher.example/player"}, {"video_url": video}]},
    }
    media = b"\x00\x00\x00\x14ftypisomTest"

    def fetch(url, *, byte_limit, destination, deadline_seconds):
        assert url == video and byte_limit == 1024 and deadline_seconds == 120
        destination.write(media)
        return url, b"", "video/mp4"

    monkeypatch.setattr("townlight_station.imported_recording.fetch_public", fetch)
    settings = SimpleNamespace(data=tmp_path, max_bytes=1024)
    with pytest.raises(HTTPException) as error:
        stage_recording(settings, meeting, 0, receipt)
    assert error.value.status_code == 422
    saved = stage_recording(settings, meeting, 1, receipt)
    assert Path(tmp_path, "ingest", saved["source_file"]).read_bytes() == media
    assert saved["provenance"]["meeting_id"] == str(meeting)
    assert saved["provenance"]["source_url"] == source
    assert stage_recording(settings, meeting, 1, receipt) == saved
