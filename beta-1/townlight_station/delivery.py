# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit Station-owned delivery. No donor app, account or on-air state."""
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Header, HTTPException
import httpx
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa
from .boundary import Settings, private_path, sha256
from .store import Store, recordings, uploads


def feeds(cfg):
    path = private_path(cfg.data, 'civiccast-feeds.json')
    rows = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    if not isinstance(rows, list) or len(rows) > 32:
        raise ValueError('Invalid CivicCast feed configuration')
    result = {}
    for row in rows:
        if set(row) != {'id', 'label', 'address', 'port', 'secret_env'}:
            raise ValueError('CivicCast feeds require id, label, address, port, secret_env')
        if not isinstance(row['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', row['id']) or row['id'] in result:
            raise ValueError('Invalid or duplicate CivicCast feed ID')
        if not isinstance(row['label'], str) or not 1 <= len(row['label']) <= 100:
            raise ValueError('Invalid CivicCast feed label')
        ipaddress.IPv4Address(row['address'])
        if type(row['port']) is not int or not 1024 <= row['port'] <= 65535:
            raise ValueError('Invalid CivicCast SRT port')
        if not isinstance(row['secret_env'], str) or not re.fullmatch(r'TOWNLIGHT_STATION_SRT_[A-Z0-9_]{1,64}', row['secret_env']):
            raise ValueError('A Station-namespaced SRT secret reference is required')
        result[row['id']] = row
    return result


class Upload(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=100, pattern=r'^[^<>\x00-\x1f]+$')
    privacy: Literal['private', 'unlisted', 'public'] = 'private'
    approval_note: str = Field(min_length=3, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=100)


def install_delivery(app, cfg, suite, store):
    @app.get('/v1/civiccast-feeds')
    def catalog(proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        suite.get('/desktop/v1/station/context?capability=capture', proof)
        return [{'id': row['id'], 'label': row['label']} for row in feeds(cfg).values()]

    @app.post('/v1/recordings/{identity}/youtube', status_code=202)
    def upload(identity: UUID, data: Upload, proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        actor = suite.get('/desktop/v1/station/context?capability=capture', proof)
        digest = hashlib.sha256(json.dumps({'recording_id': str(identity), **data.model_dump()}, sort_keys=True).encode()).hexdigest()
        with store.engine.begin() as conn:
            conn.execute(sa.text('SELECT pg_advisory_xact_lock(1953721717)'))
            prior = conn.execute(sa.select(uploads).where(uploads.c.creator == actor['actor_id'],
                uploads.c.idempotency_key == data.idempotency_key)).mappings().first()
            if prior:
                if prior['request_hash'] != digest:
                    raise HTTPException(409, 'Idempotency key describes a different upload')
                return dict(prior)
            record = conn.execute(sa.select(recordings).where(recordings.c.id == str(identity))).mappings().first()
            if not record:
                raise HTTPException(404, 'Recording not found')
            if record['status'] != 'complete':
                raise HTTPException(409, 'Only completed recordings can be uploaded')
            source = private_path(cfg.data, 'recordings', str(identity), record['details']['output_file'])
            if not source.is_file() or sha256(source) != record['details']['sha256']:
                raise HTTPException(409, 'Recording integrity failure')
            # One external attempt per recording. Unknown results must be reconciled
            # in YouTube Studio; another key must not silently duplicate a video.
            if conn.scalar(sa.select(uploads.c.id).where(uploads.c.recording_id == str(identity))):
                raise HTTPException(409, 'Upload already exists; inspect its receipt before any new external action')
            job = {'id': str(uuid4()), 'creator': actor['actor_id'], 'idempotency_key': data.idempotency_key,
                'request_hash': digest, 'recording_id': str(identity), 'status': 'queued',
                'details': {'title': data.title, 'privacy': data.privacy, 'approval_note': data.approval_note,
                    'approved_at': datetime.now(timezone.utc).isoformat(), 'recording_sha256': record['details']['sha256']}}
            conn.execute(uploads.insert().values(**job))
        return job

    @app.get('/v1/youtube-uploads')
    def list_uploads(proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        suite.get('/desktop/v1/station/context?capability=read', proof)
        with store.engine.connect() as conn:
            return [dict(row) for row in conn.execute(sa.select(uploads).order_by(uploads.c.created_at.desc()).limit(100)).mappings()]

    @app.post('/v1/youtube-uploads/{identity}/retry', status_code=202)
    def retry(identity: UUID, proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        suite.get('/desktop/v1/station/context?capability=capture', proof)
        with store.engine.begin() as conn:
            row = conn.execute(sa.select(uploads).where(uploads.c.id == str(identity)).with_for_update()).mappings().first()
            if not row:
                raise HTTPException(404, 'Upload not found')
            if row['status'] != 'failed':
                raise HTTPException(409, 'Only preparation failures can be retried; uncertain external results require reconciliation')
            conn.execute(uploads.update().where(uploads.c.id == str(identity)).values(status='queued'))
        return {'id': str(identity), 'status': 'queued'}


class GoogleTransport(httpx.BaseTransport):
    """Keep donor OAuth/upload requests on Google HTTPS, without proxy inheritance."""
    def __init__(self, inner=None):
        self.inner = inner or httpx.HTTPTransport(trust_env=False)

    def handle_request(self, request):
        if request.url.scheme != 'https' or request.url.host not in ('oauth2.googleapis.com', 'www.googleapis.com') or request.url.port not in (None, 443):
            raise ValueError('Unexpected YouTube destination')
        return self.inner.handle_request(request)

    def close(self):
        self.inner.close()


def youtube_client(cfg, privacy, transport=None):
    cfg.verify_media()
    sys.path.insert(0, str(cfg.media_source))
    import civiccast
    if not Path(civiccast.__file__).resolve().is_relative_to(cfg.media_source.resolve()):
        raise ValueError('Wrong media implementation imported')
    from civiccast.syndicate.youtube import YouTubeClient, YouTubeSettings
    credentials = [os.environ.get('TOWNLIGHT_STATION_YOUTUBE_' + name, '').strip()
        for name in ('CLIENT_ID', 'CLIENT_SECRET', 'REFRESH_TOKEN')]
    if not all(credentials):
        raise ValueError('Station YouTube OAuth configuration is missing')
    return YouTubeClient(YouTubeSettings(*credentials, privacy_status=privacy), transport=GoogleTransport(transport))


def process_upload(cfg, store, row, transport=None):
    details = dict(row['details'])
    external = False
    try:
        record = store.get(row['recording_id'])
        source = private_path(cfg.data, 'recordings', row['recording_id'], record['details']['output_file'])
        if record['status'] != 'complete' or sha256(source) != details['recording_sha256']:
            raise ValueError('Recording integrity failure')
        client = youtube_client(cfg, details['privacy'], transport)
        folder = private_path(cfg.data, 'uploads', row['id'], str(uuid4()))
        folder.mkdir(parents=True, exist_ok=False)
        output = folder / 'recording.mp4'
        result = subprocess.run([str(cfg.media_python), '-B', str(Path(__file__).with_name('vod_job.py')),
            str(cfg.media_source), str(source), str(output)], env=cfg.worker_environment(),
            capture_output=True, timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
        (folder/'remux.log').write_bytes(result.stdout + result.stderr)
        if result.returncode or not output.is_file() or not output.stat().st_size:
            raise RuntimeError('Recording remux failed')
        details.update(mp4_sha256=sha256(output), size_bytes=output.stat().st_size)
        details['mp4_file'] = str(output.relative_to(cfg.data))
        details.pop('error', None)
        # Commit intent before the first external request. A crash after here is
        # uncertain, never an automatic retry. Canonical recording stays private.
        with store.engine.begin() as conn:
            conn.execute(uploads.update().where(uploads.c.id == row['id']).values(details=details))
        external = True
        proof = client.upload_vod_path(asset_id=details['title'], path=output)
        video_id = proof.url.removeprefix('https://www.youtube.com/watch?v=')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            raise ValueError('Unexpected YouTube video identity')
        details.update(url=proof.url, video_id=video_id, finished_at=datetime.now(timezone.utc).isoformat())
        status = 'uploaded'  # Upload acknowledgement, not a claim processing/playback is finished.
    except Exception as exc:
        status = 'needs-reconciliation' if external else 'failed'
        details.update(error=type(exc).__name__, finished_at=datetime.now(timezone.utc).isoformat())
    with store.engine.begin() as conn:
        conn.execute(uploads.update().where(uploads.c.id == row['id']).values(status=status, details=details))


def run_uploads(once=False):
    """Separate queue consumer: slow uploads never occupy the capture worker."""
    from .worker import claim
    cfg = Settings.from_env()
    cfg.initialize_storage()
    cfg.verify_media()
    sys.path.insert(0, str(cfg.media_source))
    from civiccast.platform.child_containment import contain_own_descendants
    containment = contain_own_descendants()
    if not containment.active:
        raise RuntimeError('Upload worker requires child containment')
    store = Store(cfg.database_url)
    store.ready()
    try:
        with store.worker_lock(1953721718) as acquired:
            if not acquired:
                raise RuntimeError('Another upload worker owns this database')
            with store.engine.begin() as conn:
                conn.execute(uploads.update().where(uploads.c.status == 'running').values(status='needs-reconciliation'))
            while True:
                row = claim(store.engine, uploads, 'queued')
                if row:
                    process_upload(cfg, store, row)
                if once:
                    return
                if not row:
                    time.sleep(.5)
    finally:
        store.engine.dispose()
