# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded input check in the capture account; never a canonical meeting record."""
import json
from pathlib import Path
import subprocess
import time

from .boundary import private_path
from .live import sources


PREVIEW_SECONDS = 10
STARTUP_TIMEOUT_SECONDS = 30


def _wait_for_capture(child, output):
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while child.poll() is None and time.monotonic() < deadline:
        try:
            if output.stat().st_size > 0:
                return
        except FileNotFoundError:
            pass
        time.sleep(0.1)
    raise ValueError('Camera test received no media; check Windows permissions, device use and input selection')


def record_test(settings):
    source = sources(settings).get('local-chamber')
    if not source:
        raise ValueError('Save a camera and microphone before testing')
    folder = private_path(settings.data, 'temp', 'input-test')
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / 'preview.ts'
    output.unlink(missing_ok=True)
    marker = output.with_suffix('.stop')
    marker.unlink(missing_ok=True)
    preview = folder / 'preview.mp4'
    preview.unlink(missing_ok=True)
    staging = folder / 'preview.partial.mp4'
    staging.unlink(missing_ok=True)
    with (folder / 'capture.log').open('w', encoding='utf-8') as log:
        child = subprocess.Popen([str(settings.media_python), '-B',
            str(Path(__file__).with_name('media_job.py')), str(settings.media_source),
            str(folder), str(output), json.dumps(settings.encoding_profile()), json.dumps(source)],
            env=settings.worker_environment(), stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            _wait_for_capture(child, output)
            # Allow the live encoder and mux to drain enough media for the fixed-length clip.
            time.sleep(PREVIEW_SECONDS + 2)
            if child.poll() is not None:
                raise ValueError('Camera test ended before the preview was complete')
            marker.write_text('input-test-stop', encoding='ascii')
            if child.wait(timeout=20) != 0:
                raise ValueError('Camera test failed; check Windows privacy permissions, device use and input selection')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
    try:
        subprocess.run([str(settings.ffprobe.with_name('ffmpeg.exe')), '-nostdin', '-y',
            '-i', str(output), '-map', '0:v:0', '-map', '0:a:0', '-t', str(PREVIEW_SECONDS),
            '-c', 'copy', '-movflags', '+faststart', str(staging)],
            capture_output=True, timeout=30, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        probe = subprocess.run([str(settings.ffprobe), '-v', 'error', '-show_entries',
            'stream=codec_type,duration', '-of', 'json', str(staging)],
            capture_output=True, timeout=10, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        durations = {stream['codec_type']: float(stream.get('duration', 'nan'))
                     for stream in json.loads(probe.stdout)['streams']}
        if not all(PREVIEW_SECONDS - 0.5 <= durations.get(kind, 0) <= PREVIEW_SECONDS + 0.5
                   for kind in ('video', 'audio')):
            raise ValueError('Camera test did not capture ten seconds of both picture and sound')
        staging.replace(preview)
    finally:
        staging.unlink(missing_ok=True)
    return {'ready': True, 'preview_url': '/operator/input-test/content',
            'message': 'Play the test clip and confirm picture and sound before recording the meeting.'}
