# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded large-meeting storage; no file is read into memory for budgeting."""
import shutil


def storage_status(settings):
    # Include AAC audio and TS overhead above configured video bitrate.
    estimated = int((settings.bitrate_kbps + 256) * 1000 / 8 * settings.max_seconds * 1.12)
    reserve = settings.reserve_bytes
    free = shutil.disk_usage(settings.data).free
    return {'free_bytes': free, 'reserve_bytes': reserve,
            'estimated_recording_bytes': estimated,
            'max_bytes': settings.max_bytes, 'max_seconds': settings.max_seconds,
            'ready': estimated <= settings.max_bytes and free >= estimated + reserve,
            'low_space': free < reserve + 256*1024**2}


def require_capacity(settings, *, source_bytes=0):
    status = storage_status(settings)
    needed = status['estimated_recording_bytes'] + source_bytes + status['reserve_bytes']
    if status['estimated_recording_bytes'] > settings.max_bytes:
        raise ValueError('Recording size policy cannot hold the configured meeting duration')
    if status['free_bytes'] < needed:
        raise ValueError('Insufficient recording space; free space or choose another recording drive in setup')
    return status
