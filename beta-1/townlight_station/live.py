# SPDX-License-Identifier: AGPL-3.0-only
"""Administrator-configured live inputs; callers select IDs, never pipelines."""
import ipaddress
import json
import re
from .boundary import private_path


def sources(settings):
    path = private_path(settings.data, 'live-sources.json')
    rows = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    if not isinstance(rows, list) or len(rows) > 32:
        raise ValueError('Invalid live source configuration')
    result = {}
    for row in rows:
        if set(row) != {'id', 'label', 'kind', 'address', 'port'} or row['kind'] != 'udp-mpegts':
            raise ValueError('Live sources require an explicit UDP MPEG-TS receiver')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', row['id']) or row['id'] in result:
            raise ValueError('Invalid or duplicate live source ID')
        if not isinstance(row['label'], str) or not 1 <= len(row['label']) <= 100:
            raise ValueError('Invalid live source label')
        ipaddress.IPv4Address(row['address'])
        if type(row['port']) is not int or not 1024 <= row['port'] <= 65535:
            raise ValueError('Invalid live receive port')
        result[row['id']] = row
    local = private_path(settings.data, 'local-input.json')
    if local.exists():
        row = json.loads(local.read_text(encoding='utf-8'))
        if (set(row) != {'id', 'label', 'kind', 'video_id', 'audio_id'}
            or row['id'] != 'local-chamber' or row['kind'] != 'windows-devices'
            or any(not isinstance(row.get(k), str) or not re.fullmatch(r'[a-f0-9]{64}', row[k])
                   for k in ('video_id', 'audio_id'))):
            raise ValueError('Invalid saved camera/microphone selection; reopen input setup')
        result[row['id']] = row
    return result
