# SPDX-License-Identifier: AGPL-3.0-only
"""Public-playback packaging: turn an approved, published playout into what a
browser can actually play.

The rendered broadcast program is MPEG-TS, which no browser plays (measured on
this task's Playwright Chromium: ``canPlayType('video/mp2t')`` is empty). So
publication packages that program once, through the pinned GStreamer closure
Station already ships, into:

* ``playback.mp4`` - H.264/AAC remux, faststart, no re-encode;
* ``captions.vtt`` - the recording's supplied caption sidecar converted to
  WebVTT, when the recording has one.

Packaging is a copy of what staff approved: no cut, no re-encode, no generated
caption text. The packaged files are hashed and recorded with the publication so
a later public request can re-verify them, and a packaging failure fails the
publish rather than serving a partial artifact.
"""
import json
import subprocess
from pathlib import Path

from fastapi import HTTPException

from .boundary import private_path, sha256


def package(cfg, playout_id, recording_id, details):
    """Package one published playout; returns the publication record fields.

    ``recording_id`` is the playout's own source-recording column, which is
    where the supplied caption sidecar lives.

    Raises ``HTTPException`` on any packaging failure: callers must not record a
    publication whose public artifact was not produced and verified.
    """
    folder = private_path(cfg.data, 'playouts', playout_id)
    source = folder / details['output_file']
    if not source.is_file() or sha256(source) != details.get('sha256'):
        raise HTTPException(409, 'Playout program is missing or no longer matches its recorded hash')
    packaged = folder / 'playback.mp4'
    captions_vtt = folder / 'captions.vtt'
    # Only a supplied sidecar is packaged. ``preserve-embedded`` means the
    # captions live inside the encoded stream, which a WebVTT track would not
    # represent -- so nothing is generated in that case.
    sidecar = private_path(cfg.data, 'recordings', str(recording_id), 'captions.srt') \
        if recording_id else None
    if sidecar is not None and not (sidecar.is_file()
                                    and details.get('caption_policy') == 'replace-with-supplied-sidecar'):
        sidecar = None
    for stale in (packaged, captions_vtt):
        stale.unlink(missing_ok=True)
    arguments = [str(cfg.media_python), '-B', str(Path(__file__).with_name('vod_job.py')),
                 str(cfg.media_source), str(source), str(packaged)]
    if sidecar is not None:
        arguments += [str(sidecar), str(captions_vtt)]
    log = folder / 'packaging.log'
    with log.open('a', encoding='utf-8') as stream:
        child = subprocess.run(arguments, env=cfg.worker_environment(), stdout=stream,
            stderr=subprocess.STDOUT, timeout=cfg.max_seconds, creationflags=subprocess.CREATE_NO_WINDOW)
    if child.returncode or not packaged.is_file() or not packaged.stat().st_size:
        raise HTTPException(503, 'Public playback packaging failed; the publication was not recorded')
    result = {'playback_file': packaged.name, 'playback_sha256': sha256(packaged),
              'playback_bytes': packaged.stat().st_size, 'playback_type': 'video/mp4',
              'captions_sha256': None, 'captions_file': None, 'packaging': 'remux'}
    if captions_vtt.is_file() and captions_vtt.stat().st_size:
        result.update(captions_file=captions_vtt.name, captions_sha256=sha256(captions_vtt),
                      captions_type='text/vtt')
    (folder / 'packaging.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result
