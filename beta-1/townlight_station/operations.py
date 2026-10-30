# SPDX-License-Identifier: AGPL-3.0-only
"""Basic operating workflow: schedule, playout/commit-to-air, approval, playback.

Nothing here publishes automatically. A playout is commit-to-air of a completed
recording; staff approval is a separate explicit act and only then does a local
loopback playback route serve the rendered broadcast output. No donor
application, installer, OTT/EAS/underwriting/publish-provider path is used.

On-air control (``/v1/on-air``) writes the durable decision the Station air
supervisor airs through the pinned engine: schedule, a configured live override,
or an explicit return to schedule.

Publication packages the rendered MPEG-TS program into a browser-playable MP4
(plus a WebVTT track when the recording carries supplied captions) so the public
route serves something a browser can actually play. Packaging is a copy of what
staff approved -- see ``packaging.py``.
"""
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa

from .boundary import private_path, sha256
from .live import sources
from .packaging import package
from .store import playouts, recordings, schedules, station_state

NOW = lambda: datetime.now(timezone.utc).isoformat()


def _one(conn, table, identity, label):
    row = conn.execute(sa.select(table).where(table.c.id == str(identity))).mappings().first()
    if not row:
        raise HTTPException(404, f'{label} not found')
    return row


def _insert_idempotent(conn, table, principal, key, request_hash, values, label):
    existing = conn.execute(sa.select(table).where(
        table.c.creator == principal['actor_id'], table.c.idempotency_key == key)).mappings().first()
    if existing:
        if existing['request_hash'] != request_hash:
            raise HTTPException(409, f'{label} idempotency key was already used for a different request')
        return existing
    conn.execute(table.insert().values(**values))
    return conn.execute(sa.select(table).where(table.c.id == values['id'])).mappings().first()


def _hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    meeting_id: UUID
    agenda_ids: list[UUID] = Field(default_factory=list, max_length=100)
    source_file: str | None = Field(default=None, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,180}$')
    live_source_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{1,64}$')
    captions_file: str | None = Field(default=None, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\.srt$')
    start_at: datetime
    idempotency_key: str = Field(min_length=1, max_length=100)


class PlayoutRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    recording_id: UUID
    mode: str = Field(pattern='^(dry-run|commit)$')
    idempotency_key: str = Field(min_length=1, max_length=100)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    note: str = Field(min_length=3, max_length=500)


class LiveOverrideRequest(BaseModel):
    """An on-air override naming an administrator-configured live input."""

    model_config = ConfigDict(extra='forbid')
    live_source_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    reason: str = Field(min_length=3, max_length=200)


def _playout_view(row):
    details = row['details']
    return {'id': row['id'], 'status': row['status'], 'recording_id': row['recording_id'],
            'mode': details.get('mode'), 'approved': details.get('approved'),
            'published': details.get('published'), 'output': details.get('output_file'),
            'sha256': details.get('sha256'), 'duration_seconds': details.get('duration_seconds'),
            'on_air': details.get('on_air', False), 'attempts': details.get('attempts', []),
            'caption_policy': details.get('caption_policy'), 'error': details.get('error'),
            'packaged': bool(details.get('packaged')), 'playback_ready': bool(
                (details.get('published') or {}).get('playback_file'))}


def install_operations(app, settings, suite, store):
    capture = lambda proof: suite.get('/desktop/v1/station/context?capability=capture', proof)
    read = lambda proof: suite.get('/desktop/v1/station/context?capability=read', proof)

    @app.post('/v1/schedules', status_code=202)
    def create_schedule(data: ScheduleRequest, proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        principal = capture(proof)
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
        if original and not original.is_file():
            raise HTTPException(422, 'Source file missing from managed ingest')
        captions = private_path(settings.data, 'ingest', data.captions_file) if data.captions_file else None
        if captions and not captions.is_file():
            raise HTTPException(422, 'Caption sidecar missing from managed ingest')
        start_at = data.start_at if data.start_at.tzinfo else data.start_at.replace(tzinfo=timezone.utc)
        request_hash = _hash({'request': data.model_dump(mode='json'), 'meeting': meeting,
            'agenda': agendas, 'live_source': live})
        values = {'id': str(uuid4()), 'creator': principal['actor_id'],
            'idempotency_key': data.idempotency_key, 'request_hash': request_hash,
            'meeting_id': str(data.meeting_id), 'start_at': start_at, 'status': 'scheduled',
            'details': {'meeting': meeting, 'agenda': agendas, 'live_source': live,
                'source_file': data.source_file, 'captions_file': data.captions_file,
                'capture_kind': 'live' if live else 'file', 'created_by': principal['actor_id'],
                'scheduled_at': NOW()}}
        with store.engine.begin() as conn:
            conn.execute(sa.text('SELECT pg_advisory_xact_lock(1953721717)'))
            row = _insert_idempotent(conn, schedules, principal, data.idempotency_key,
                request_hash, values, 'schedule')
        return {'id': row['id'], 'status': row['status'], 'start_at': start_at.isoformat()}

    @app.get('/v1/schedules')
    def list_schedules(proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        read(proof)
        with store.engine.connect() as conn:
            return [{**dict(r), 'start_at': r['start_at'].isoformat()}
                for r in conn.execute(sa.select(schedules).order_by(schedules.c.start_at)
                    .limit(100)).mappings()]

    @app.post('/v1/schedules/{identity}/cancel', status_code=202)
    def cancel_schedule(identity: UUID, proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        actor = capture(proof)
        with store.engine.begin() as conn:
            row = _one(conn, schedules, identity, 'Schedule')
            if row['status'] != 'scheduled':
                raise HTTPException(409, 'Only a schedule that has not started can be cancelled')
            details = {**row['details'], 'cancelled_at': NOW(), 'cancelled_by': actor['actor_id']}
            conn.execute(schedules.update().where(schedules.c.id == row['id'])
                .values(status='cancelled', details=details))
        return {'id': row['id'], 'status': 'cancelled'}

    @app.post('/v1/playouts', status_code=202)
    def create_playout(data: PlayoutRequest, proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        principal = capture(proof)
        with store.engine.begin() as conn:
            source = conn.execute(sa.select(recordings)
                .where(recordings.c.id == str(data.recording_id))).mappings().first()
            if not source:
                raise HTTPException(404, 'Recording not found')
            if source['status'] != 'complete':
                raise HTTPException(409, 'Only a complete, verified recording can be committed to air')
            request_hash = _hash({'request': data.model_dump(mode='json'),
                'recording_sha256': source['details'].get('sha256')})
            values = {'id': str(uuid4()), 'creator': principal['actor_id'],
                'idempotency_key': data.idempotency_key, 'request_hash': request_hash,
                'recording_id': str(data.recording_id), 'status': 'queued',
                'details': {'mode': data.mode, 'recording_sha256': source['details'].get('sha256'),
                    'caption_policy': source['details'].get('caption_policy'),
                    'created_by': principal['actor_id'], 'approved': None, 'published': None,
                    'on_air': False, 'requested_at': NOW(), 'attempts': []}}
            conn.execute(sa.text('SELECT pg_advisory_xact_lock(1953721717)'))
            row = _insert_idempotent(conn, playouts, principal, data.idempotency_key,
                request_hash, values, 'playout')
        return {'id': row['id'], 'status': row['status'], 'mode': data.mode}

    @app.get('/v1/playouts')
    def list_playouts(proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        read(proof)
        with store.engine.connect() as conn:
            return [_playout_view(dict(r)) for r in conn.execute(sa.select(playouts)
                .order_by(playouts.c.created_at.desc()).limit(100)).mappings()]

    @app.get('/v1/playouts/{identity}')
    def get_playout(identity: UUID, proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        read(proof)
        with store.engine.connect() as conn:
            row = conn.execute(sa.select(playouts).where(playouts.c.id == str(identity))).mappings().first()
            if not row:
                raise HTTPException(404, 'Playout not found')
            return _playout_view(dict(row))

    @app.get('/v1/on-air')
    def on_air(proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        read(proof)
        from .airplay import plan, scheduled_program
        current = store.on_air() or {'mode': 'schedule'}
        # Report the program the supervisor can actually air, so staff never read
        # an on-air claim the engine cannot back with verified bytes.
        program = scheduled_program(store, settings)
        return {**current, 'air': {'leg': (plan(store, settings) or {}).get('leg'),
            'program_playout_id': program['playout_id'] if program else None,
            'program_sha256': program['sha256'] if program else None}}

    @app.post('/v1/on-air/live', status_code=202)
    def live_override(data: LiveOverrideRequest,
                      proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        """Put a configured live input on air, in place of the scheduled program."""
        actor = capture(proof)
        live = sources(settings).get(data.live_source_id)
        if not live:
            raise HTTPException(422, 'Configured live input not found')
        with store.engine.begin() as conn:
            conn.execute(sa.text('SELECT pg_advisory_xact_lock(1953721717)'))
            conn.execute(station_state.update().where(station_state.c.id == 1).values(on_air={
                'mode': 'live', 'live_source_id': live['id'], 'reason': data.reason,
                'since': NOW(), 'overridden_by': actor['actor_id'],
                'superseded': conn.scalar(sa.select(station_state.c.on_air)
                    .where(station_state.c.id == 1)) or {}}))
        return {'mode': 'live', 'live_source_id': live['id'], 'overridden_by': actor['actor_id']}

    @app.post('/v1/on-air/return-to-schedule', status_code=202)
    def return_to_schedule(proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        actor = capture(proof)
        with store.engine.begin() as conn:
            conn.execute(sa.text('SELECT pg_advisory_xact_lock(1953721717)'))
            previous = conn.scalar(sa.select(station_state.c.on_air).where(station_state.c.id == 1)) or {}
            conn.execute(station_state.update().where(station_state.c.id == 1).values(on_air={
                'mode': 'schedule', 'returned_at': NOW(), 'returned_by': actor['actor_id'],
                'returned_from': previous.get('live_source_id') or previous.get('mode')}))
        return {'mode': 'schedule', 'returned_by': actor['actor_id']}

    @app.post('/v1/playouts/{identity}/approve', status_code=202)
    def approve(identity: UUID, data: ApprovalRequest,
                proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        actor = capture(proof)
        with store.engine.begin() as conn:
            row = _one(conn, playouts, identity, 'Playout')
            if row['status'] != 'complete':
                raise HTTPException(409, 'Only a rendered playout can be approved for public playback')
            details = {**row['details'], 'approved': {'actor_id': actor['actor_id'], 'role': actor.get('role'),
                'note': data.note, 'approved_at': NOW()}}
            conn.execute(playouts.update().where(playouts.c.id == row['id']).values(details=details))
        return {'id': row['id'], 'approved': details['approved']}

    @app.post('/v1/playouts/{identity}/publish', status_code=202)
    def publish(identity: UUID, proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        actor = capture(proof)
        with store.engine.connect() as conn:
            row = conn.execute(sa.select(playouts)
                .where(playouts.c.id == str(identity))).mappings().first()
        if not row:
            raise HTTPException(404, 'Playout not found')
        approved = row['details'].get('approved')
        if not approved:
            raise HTTPException(409, 'Explicit staff approval is required before public playback')
        if row['details'].get('published'):
            return {'id': row['id'], 'published': row['details']['published'],
                    'playback_path': f'/v1/public/playback/{row["id"]}'}
        # Package outside the transaction: it runs a bounded child process, and a
        # failure must leave the playout un-published rather than half-published.
        packed = package(settings, row['id'], row['recording_id'], row['details'])
        with store.engine.begin() as conn:
            current = _one(conn, playouts, identity, 'Playout')
            if current['details'].get('published'):
                return {'id': current['id'], 'published': current['details']['published'],
                        'playback_path': f'/v1/public/playback/{current["id"]}'}
            published = {'published_at': NOW(), 'published_by': actor['actor_id'],
                'approved_by': approved['actor_id'], 'target': 'local-loopback', **packed}
            details = {**current['details'], 'published': published, 'packaged': packed}
            conn.execute(playouts.update().where(playouts.c.id == current['id']).values(details=details))
        return {'id': row['id'], 'published': published,
                'playback_path': f'/v1/public/playback/{row["id"]}'}

    @app.get('/v1/public/playback/{identity}')
    def public_playback(identity: UUID):
        # Public route: only staff-published playouts exist here. Unpublished or
        # unapproved playouts are indistinguishable from missing ones. The MP4 is
        # what a browser can actually play; the broadcast program is not served.
        with store.engine.connect() as conn:
            row = conn.execute(sa.select(playouts).where(playouts.c.id == str(identity))).mappings().first()
        published = (row['details'].get('published') if row else None) or {}
        if not row or not published or row['status'] != 'complete' or not published.get('playback_file'):
            raise HTTPException(404, 'Playback not found')
        path = private_path(settings.data, 'playouts', row['id'], published['playback_file'])
        if not path.is_file() or sha256(path) != published.get('playback_sha256'):
            raise HTTPException(409, 'Playback integrity failure')
        return FileResponse(path, media_type='video/mp4',
            headers={'Cache-Control': 'no-store', 'X-Content-SHA256': published['playback_sha256']})

    @app.get('/v1/public/playback/{identity}/captions')
    def public_captions(identity: UUID):
        # The packaged WebVTT track for a published playout, and nothing else.
        with store.engine.connect() as conn:
            row = conn.execute(sa.select(playouts).where(playouts.c.id == str(identity))).mappings().first()
        published = (row['details'].get('published') if row else None) or {}
        if not row or not published or row['status'] != 'complete' or not published.get('captions_file'):
            raise HTTPException(404, 'Captions not found')
        path = private_path(settings.data, 'playouts', row['id'], published['captions_file'])
        if not path.is_file() or sha256(path) != published.get('captions_sha256'):
            raise HTTPException(409, 'Caption integrity failure')
        return FileResponse(path, media_type='text/vtt',
            headers={'Cache-Control': 'no-store', 'X-Content-SHA256': published['captions_sha256']})

    @app.get('/v1/public/playback/{identity}/program')
    def public_program(identity: UUID):
        # Retained for continuity with the earlier broadcast-output route; still
        # only published, verified playouts. Browsers cannot play MPEG-TS.
        with store.engine.connect() as conn:
            row = conn.execute(sa.select(playouts).where(playouts.c.id == str(identity))).mappings().first()
        if not row or not row['details'].get('published') or row['status'] != 'complete':
            raise HTTPException(404, 'Playback not found')
        path = private_path(settings.data, 'playouts', row['id'], row['details']['output_file'])
        if not path.is_file() or sha256(path) != row['details']['sha256']:
            raise HTTPException(409, 'Playback integrity failure')
        return FileResponse(path, media_type='video/mp2t',
            headers={'Cache-Control': 'no-store', 'X-Content-SHA256': row['details']['sha256']})
