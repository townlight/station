# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded input check in the capture account; never a canonical meeting record."""
import json
from pathlib import Path
import subprocess
import time

from .boundary import private_path
from .live import sources


def record_test(settings):
    source = sources(settings).get('local-chamber')
    if not source:
        raise ValueError('Save a camera and microphone before testing')
    folder = private_path(settings.data, 'temp', 'input-test')
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / 'preview.ts'
    marker = output.with_suffix('.stop')
    marker.unlink(missing_ok=True)
    preview = folder / 'preview.mp4'
    preview.unlink(missing_ok=True)
    with (folder / 'capture.log').open('w', encoding='utf-8') as log:
        child = subprocess.Popen([str(settings.media_python), '-B',
            str(Path(__file__).with_name('media_job.py')), str(settings.media_source),
            str(folder), str(output), json.dumps(settings.encoding_profile()), json.dumps(source)],
            env=settings.worker_environment(), stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            time.sleep(10)
            marker.write_text('input-test-stop', encoding='ascii')
            if child.wait(timeout=20) != 0:
                raise ValueError('Camera test failed; check Windows privacy permissions, device use and input selection')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
    subprocess.run([str(settings.ffprobe.with_name('ffmpeg.exe')), '-nostdin', '-y',
        '-i', str(output), '-map', '0:v:0', '-map', '0:a:0', '-c', 'copy', '-movflags', '+faststart', str(preview)],
        capture_output=True, timeout=30, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return {'ready': True, 'preview_url': '/operator/input-test/content',
            'message': 'Play the test clip and confirm picture and sound before recording the meeting.'}
