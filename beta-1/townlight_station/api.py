# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import json
from contextlib import asynccontextmanager
from uuid import UUID, uuid4
from datetime import datetime, timezone
from .live import sources

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
import httpx
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa

from .boundary import IDENTITY, Settings, private_path, sha256
from . import VERSION
from .store import Store, recordings


def copy_snapshot(source, destination, expected_hash, max_bytes):
    size = 0
    digest = hashlib.sha256()
    with source.open('rb') as incoming, destination.open('xb') as output:
        while chunk := incoming.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(422, 'Source exceeds the capture size limit')
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != expected_hash:
        raise HTTPException(409, 'Source changed during capture; incomplete evidence retained')


class Capture(BaseModel):
    model_config = ConfigDict(extra='forbid')
    meeting_id: UUID
    agenda_ids: list[UUID] = Field(default_factory=list, max_length=100)
    source_file: str | None = Field(default=None,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,180}$')
    live_source_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    captions_file: str | None = Field(default=None, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\.srt$')
    idempotency_key: str = Field(min_length=1, max_length=100)


class Suite:
    def __init__(self, settings):
        self.url = settings.suite_url

    def get(self, path, proof):
        if not proof or len(proof) != 64:
            raise HTTPException(401, 'Townlight staff session required')
        try:
            with httpx.Client(base_url=self.url, timeout=15, trust_env=False, follow_redirects=False) as client:
                response = client.get(path, headers={'X-Townlight-Session': proof})
        except httpx.HTTPError:
            raise HTTPException(503, 'Townlight authority unavailable; no cached permission used') from None
        if response.status_code in (401, 403, 404):
            raise HTTPException(response.status_code, 'Townlight denied this operation')
        if response.status_code != 200:
            raise HTTPException(503, 'Townlight authority unavailable')
        return response.json()


def create_app(settings=None):
    settings = settings or Settings.from_env()
    settings.initialize_storage()
    source_commit = settings.verify_media()
    store = Store(settings.database_url)
    store.ready()  # Explicit migration; never silently create tables in requests.
    suite = Suite(settings)

    @asynccontextmanager
    async def lifespan(app):
        yield
        store.engine.dispose()

    app = FastAPI(title='Townlight Station', version=VERSION, lifespan=lifespan)

    @app.get('/health')
    def health():
        store.ready()
        return {'product': IDENTITY['product'], 'ready': True}

    @app.get('/v1/identity')
    def identity(proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        suite.get('/desktop/v1/station/context?capability=read', proof)
        return {**IDENTITY, 'media_source_commit': source_commit}

    @app.post('/v1/recordings', status_code=202)
    def capture(data: Capture, proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        principal = suite.get('/desktop/v1/station/context?capability=capture', proof)
        meeting = suite.get(f'/desktop/v1/meetings/meetings/{data.meeting_id}', proof)
        agendas = []
        for item_id in data.agenda_ids:
            item = suite.get(f'/desktop/v1/meetings/agenda-items/{item_id}', proof)
            if item.get('meeting_id') != str(data.meeting_id):
                raise HTTPException(409, 'Agenda item does not belong to this meeting')
            agendas.append(item)
        if bool(data.source_file) == bool(data.live_source_id) or (data.live_source_id and data.captions_file):
            raise HTTPException(422, 'Select one recorded file or live input; live input preserves embedded captions')
        live = sources(settings).get(data.live_source_id) if data.live_source_id else None
        if data.live_source_id and not live:
            raise HTTPException(422, 'Configured live input not found')
        original = private_path(settings.data, 'ingest', data.source_file) if data.source_file else None
        if original and (not original.is_file() or original.stat().st_size > settings.max_bytes):
            raise HTTPException(422, 'Source file missing or exceeds configured limit')
        input_hash = sha256(original) if original else None
        captions = private_path(settings.data, 'ingest', data.captions_file) if data.captions_file else None
        if captions and (not captions.is_file() or captions.stat().st_size > 5_000_000):
            raise HTTPException(422, 'Caption sidecar missing or too large')
        caption_hash = sha256(captions) if captions else None
        request_hash = hashlib.sha256(json.dumps({'request': data.model_dump(mode='json'),
            'input_sha256': input_hash, 'live_source': live, 'caption_sha256': caption_hash, 'encoding_profile': settings.encoding_profile(),
            'meeting': meeting, 'agenda': agendas}, sort_keys=True).encode()).hexdigest()
        with store.engine.begin() as conn:
            # Serialize idempotency checks including bytes and captured meeting version.
            conn.execute(sa.text('SELECT pg_advisory_xact_lock(1953721717)'))
            prior = conn.execute(sa.select(recordings).where(recordings.c.creator == principal['actor_id'],
                recordings.c.idempotency_key == data.idempotency_key)).mappings().first()
            if prior:
                if prior['request_hash'] != request_hash:
                    raise HTTPException(409, 'Idempotency key already describes different source evidence')
                return {'id': prior['id'], 'status': prior['status']}
            pending = conn.scalar(sa.select(sa.func.count()).select_from(recordings)
                .where(recordings.c.status.in_(('queued', 'running'))))
            if live and pending:
                raise HTTPException(409, 'Station is busy; live recording must start immediately')
            if pending >= 100:
                raise HTTPException(429, 'Station queue is full; no capture accepted')
            identity = str(uuid4())
            folder = private_path(settings.data, 'recordings', identity)
            folder.mkdir()
            captured = folder / 'source.media'
            # Exclusive destination. Never hand a mutable ingest path to the worker.
            if original:
                copy_snapshot(original, captured, input_hash, settings.max_bytes)
            if captions:
                copy_snapshot(captions, folder / 'captions.srt', caption_hash, 5_000_000)
            details = {'meeting': meeting, 'agenda': agendas, 'input_sha256': input_hash,
                'caption_sha256': caption_hash, 'media_source_commit': source_commit,
                'caption_policy': 'replace-with-supplied-sidecar' if captions else 'preserve-embedded',
                'encoding_profile': settings.encoding_profile(), 'visibility': 'private', 'attempts': [], 'live_source': live,
                'capture_kind': 'live' if live else 'file'}
            conn.execute(recordings.insert().values(id=identity, creator=principal['actor_id'],
                idempotency_key=data.idempotency_key, request_hash=request_hash,
                meeting_id=str(data.meeting_id), status='queued', details=details))
        return {'id': identity, 'status': 'queued'}

    def live_progress(record):
        """Bytes already retained for a running live capture, or nothing.

        Read from the retained output file, never from the encoder's word: staff
        see growth, which is what proves packets are actually being recorded.
        """
        details = record['details']
        if record['status'] not in ('queued', 'running') or not details.get('live_source'):
            return None
        attempts = details.get('attempts') or []
        if not attempts:
            return None
        folder = private_path(settings.data, 'recordings', record['id'])
        output = folder / (attempts[-1]['id'] + '.ts')
        try:
            size = output.stat().st_size
        except OSError:
            return {'bytes_written': 0}
        return {'bytes_written': size, 'updated_at': record['created_at'].isoformat()
                if record.get('created_at') else None}

    def progress(record):
        # ``creator`` is the capturing staff actor. Records' private attach reads
        # it for provenance (measured: omitting it failed the handoff with
        # KeyError: 'creator'), and it is already visible to Station readers.
        return {'id': record['id'], 'creator': record['creator'], 'status': record['status'],
            'meeting_id': record['meeting_id'],
            'created_at': record['created_at'].isoformat() if record.get('created_at') else None,
            'control': record['control'], 'details': record['details'],
            'live_progress': live_progress(record)}

    def authorized_record(identity, proof):
        suite.get('/desktop/v1/station/context?capability=read', proof)
        record = store.get(str(identity))
        if not record:
            raise HTTPException(404, 'Recording not found')
        return record

    @app.get('/v1/recordings/{identity}')
    def get_record(identity: UUID, proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        return progress(authorized_record(identity, proof))

    @app.get('/v1/recordings/{identity}/content')
    def content(identity: UUID, proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        record = authorized_record(identity, proof)
        if record['status'] != 'complete':
            raise HTTPException(409, 'Recording is not complete')
        path = private_path(settings.data, 'recordings', str(identity), record['details']['output_file'])
        if not path.is_file() or sha256(path) != record['details']['sha256']:
            raise HTTPException(409, 'Recording integrity failure')
        return FileResponse(path, media_type='video/mp2t', filename=f'{identity}.ts',
            headers={'Cache-Control': 'no-store', 'X-Content-SHA256': record['details']['sha256']})

    @app.post('/v1/recordings/{identity}/retry', status_code=202)
    def retry(identity: UUID, proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        suite.get('/desktop/v1/station/context?capability=capture', proof)
        with store.engine.begin() as conn:
            row = conn.execute(sa.select(recordings).where(recordings.c.id == str(identity)).with_for_update()).mappings().first()
            if not row:
                raise HTTPException(404, 'Recording not found')
            if row['status'] not in ('failed', 'interrupted'):
                raise HTTPException(409, 'Only failed/interrupted recordings can be retried')
            if row['details'].get('live_source'):
                raise HTTPException(409, 'A live gap cannot be retried; start a new recording')
            conn.execute(recordings.update().where(recordings.c.id == str(identity)).values(status='queued'))
        return {'id': str(identity), 'status': 'queued'}
    @app.get('/v1/live-sources')
    def live_sources(proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        suite.get('/desktop/v1/station/context?capability=capture', proof)
        return [{'id': s['id'], 'label': s['label']} for s in sources(settings).values()]

    @app.get('/v1/recordings')
    def list_records(proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        suite.get('/desktop/v1/station/context?capability=read', proof)
        with store.engine.connect() as conn:
            return [progress(dict(row)) for row in conn.execute(sa.select(recordings).order_by(recordings.c.created_at.desc()).limit(100)).mappings()]

    @app.post('/v1/recordings/{identity}/stop', status_code=202)
    def stop_live(identity: UUID, proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        actor = suite.get('/desktop/v1/station/context?capability=capture', proof)
        with store.engine.begin() as conn:
            row = conn.execute(sa.select(recordings).where(recordings.c.id == str(identity)).with_for_update()).mappings().first()
            if not row:
                raise HTTPException(404, 'Recording not found')
            if not row['details'].get('live_source'):
                raise HTTPException(409, 'This recording is not live')
            if not row['control'].get('stop_requested_at'):
                if row['status'] not in ('queued', 'running'):
                    raise HTTPException(409, 'Recording already ended')
                conn.execute(recordings.update().where(recordings.c.id == str(identity)).values(control={
                    'stop_requested_at': datetime.now(timezone.utc).isoformat(), 'stopped_by': actor['actor_id']}))
        return {'id': str(identity), 'stop_requested': True}

    from .operations import install_operations
    install_operations(app, settings, suite, store)

    from .operator import install_operator
    install_operator(app, settings, suite)
    return app
