# SPDX-License-Identifier: AGPL-3.0-only
import json
from pathlib import Path
import subprocess
import uuid

from .boundary import private_path


def discover(settings):
    settings.verify_media()
    result = subprocess.run([str(settings.media_python), '-B',
        str(Path(__file__).with_name('device_probe.py')), str(settings.media_source)],
        env=settings.worker_environment(), capture_output=True, text=True,
        timeout=20, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return json.loads(result.stdout.strip().splitlines()[-1])


def save_selection(settings, video_id, audio_id):
    from .device_probe import resolve_device
    devices = discover(settings)
    video = resolve_device(devices, video_id, 'video')
    audio = resolve_device(devices, audio_id, 'audio')
    value = {'id': 'local-chamber', 'label': video['label']+' / '+audio['label'],
             'kind': 'windows-devices', 'video_id': video_id, 'audio_id': audio_id}
    path = private_path(settings.data, 'local-input.json')
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value), encoding='utf-8')
    temporary.replace(path)
    return value
