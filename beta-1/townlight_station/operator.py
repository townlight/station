# SPDX-License-Identifier: AGPL-3.0-only
"""Short-lived operator sessions launched explicitly by authenticated Townlight staff."""
from pathlib import Path
import secrets
import subprocess
import time
from uuid import UUID
import httpx
from fastapi import Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from .imported_recording import stage_recording


class Ticket(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ticket: str = Field(min_length=32, max_length=128)


class Handoff(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    recording_id: UUID
    expected_sha256: str = Field(pattern='^[0-9a-f]{64}$')


class ImportedRecording(BaseModel):
    model_config = ConfigDict(extra='forbid')
    index: int = Field(ge=0, strict=True)


def install_operator(app, settings, suite):
    import threading
    tickets, sessions = {}, {}
    input_lock = threading.Lock()
    static = Path(__file__).with_name('ui')

    @app.middleware('http')
    async def browser_boundary(request: Request, call_next):
        now = time.monotonic()
        for mapping in (tickets, sessions):
            for key in list(mapping):
                if mapping[key][1] < now:
                    del mapping[key]
        token = request.cookies.get('station_operator')
        browser = token in sessions
        if request.url.path.startswith('/operator/') and request.method != 'GET' or browser and request.method not in ('GET', 'HEAD'):
            if request.headers.get('origin') != str(request.base_url).rstrip('/') or request.headers.get('X-Station-UI') != '1':
                return JSONResponse({'detail': 'Same-origin operator action required'}, status_code=403)
        if browser and not request.headers.get('X-Townlight-Session'):
            request.scope['headers'] = [*request.scope['headers'], (b'x-townlight-session', sessions[token][0].encode())]
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.post('/v1/operator-ticket')
    def issue(proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        suite.get('/desktop/v1/station/context?capability=read', proof)
        if len(tickets) >= 256:
            raise HTTPException(429, 'Too many outstanding operator launches')
        token = secrets.token_urlsafe(32)
        tickets[token] = (proof, time.monotonic()+60)
        return {'ticket': token, 'expires_in': 60}

    @app.post('/operator/session')
    def exchange(data: Ticket, request: Request):
        entry = tickets.pop(data.ticket, None)
        if not entry or entry[1] < time.monotonic():
            raise HTTPException(401, 'Launch link expired or already used; reopen Station from the signed-in staff session')
        suite.get('/desktop/v1/station/context?capability=read', entry[0])
        if len(sessions) >= 256:
            raise HTTPException(429, 'Operator session capacity reached')
        token = secrets.token_urlsafe(32)
        sessions[token] = (entry[0], time.monotonic()+900)
        response = JSONResponse({'connected': True})
        response.set_cookie('station_operator', token, httponly=True, secure=request.url.scheme == 'https',
                            samesite='strict', max_age=900, path='/')
        return response

    @app.post('/operator/logout')
    def logout(request: Request):
        sessions.pop(request.cookies.get('station_operator'), None)
        response = JSONResponse({'connected': False})
        response.delete_cookie('station_operator', path='/')
        return response

    @app.post('/operator/renew')
    def renew(request: Request):
        token = request.cookies.get('station_operator')
        entry = sessions.get(token)
        if not entry:
            raise HTTPException(401, 'Reconnect through your signed-in Townlight session; recording continues')
        try:
            suite.get('/desktop/v1/station/context?capability=read', entry[0])
        except HTTPException as exc:
            if exc.status_code in (401, 403):
                sessions.pop(token, None)
            raise
        sessions[token] = (entry[0], time.monotonic()+900)
        response = JSONResponse({'connected': True, 'expires_in': 900})
        response.set_cookie('station_operator', token, httponly=True,
                            secure=request.url.scheme == 'https', samesite='strict',
                            max_age=900, path='/')
        return response

    @app.get('/operator/devices')
    def device_list(proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        suite.get('/desktop/v1/station/context?capability=capture', proof)
        from .devices import discover
        try:
            return [{'id': d['id'], 'label': d['label'], 'kind': d['kind']} for d in discover(settings)]
        except (OSError, ValueError, subprocess.SubprocessError):
            raise HTTPException(503, 'Input discovery failed. Check the installed media runtime and Windows camera/microphone permissions.') from None

    class DeviceSelection(BaseModel):
        model_config = ConfigDict(extra='forbid')
        video_id: str = Field(pattern='^[a-f0-9]{64}$')
        audio_id: str = Field(pattern='^[a-f0-9]{64}$')

    @app.post('/operator/devices')
    def select_devices(data: DeviceSelection, proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        suite.get('/desktop/v1/station/context?capability=capture', proof)
        from .devices import save_selection
        try:
            return save_selection(settings, data.video_id, data.audio_id)
        except (OSError, ValueError, subprocess.SubprocessError):
            raise HTTPException(422, 'Selected inputs are unavailable; refresh the device list and check Windows permissions.') from None

    @app.post('/operator/input-test')
    def test_inputs(proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        suite.get('/desktop/v1/station/context?capability=capture', proof)
        from .store import Store, recordings
        import sqlalchemy as sa
        from .input_test import record_test
        store = Store(settings.database_url)
        try:
            with store.engine.begin() as conn:
                # Same lock as capture admission: testing must not seize a meeting's device.
                conn.execute(sa.text('SELECT pg_advisory_xact_lock(1953721717)'))
                if conn.scalar(sa.select(sa.func.count()).select_from(recordings)
                               .where(recordings.c.status.in_(('queued', 'running')))):
                    raise HTTPException(409, 'Finish the active recording before testing inputs')
                if not input_lock.acquire(blocking=False):
                    raise HTTPException(409, 'An input test is already running')
                try:
                    return record_test(settings)
                except (OSError, ValueError, subprocess.SubprocessError):
                    raise HTTPException(422, 'Input test failed. Check device access and the Station input-test log, then retry.') from None
                finally:
                    input_lock.release()
        finally:
            store.engine.dispose()

    @app.get('/operator/input-test/content')
    def input_preview(proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        suite.get('/desktop/v1/station/context?capability=capture', proof)
        from .boundary import private_path
        path = private_path(settings.data, 'temp', 'input-test', 'preview.mp4')
        if not path.is_file():
            raise HTTPException(404, 'Run an input test first')
        return FileResponse(path, media_type='video/mp4')

    @app.get('/operator/storage')
    def capacity(proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        suite.get('/desktop/v1/station/context?capability=read', proof)
        from .storage_policy import storage_status
        return storage_status(settings)

    @app.get('/operator/catalog')
    def catalog(proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        suite.get('/desktop/v1/station/context?capability=read', proof)
        result = {'meetings': [], 'agenda': [], 'requests': [], 'can_capture': False, 'can_attach': False,
                  'max_seconds': settings.max_seconds}
        try:
            result.update(suite.get('/desktop/v1/meetings/station/catalog', proof))
            result['can_capture'] = True
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
        try:
            state = suite.get('/desktop/v1/records-state', proof)
            result['requests'] = [{'id': r['request_id'], 'summary': r['summary']} for r in state['records_requests']]
            result['can_attach'] = True
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
        return result

    @app.get('/operator/meeting/{identity}')
    def meeting(identity: UUID, proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        return suite.get(f'/desktop/v1/meetings/meetings/{identity}', proof)

    @app.get('/operator/meeting/{identity}/imported-recordings')
    def imported_recordings(identity: UUID, proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        suite.get('/desktop/v1/station/context?capability=capture', proof)
        receipt = suite.get(f'/desktop/v1/meetings/imports/{identity}', proof)
        return [
            {'index': index, 'direct': bool(recording.get('video_url'))}
            for index, recording in enumerate(receipt['payload'].get('recordings', []))
        ]

    @app.post('/operator/meeting/{identity}/imported-recordings')
    def stage_imported_recording(
        identity: UUID, data: ImportedRecording, proof: str | None = Header(default=None, alias='X-Townlight-Session'),
    ):
        suite.get('/desktop/v1/station/context?capability=capture', proof)
        receipt = suite.get(f'/desktop/v1/meetings/imports/{identity}', proof)
        return stage_recording(settings, identity, data.index, receipt)

    @app.post('/operator/handoff')
    def handoff(data: Handoff, proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        suite.get('/desktop/v1/station/context?capability=read', proof)
        try:
            # Records streams and verifies a separate copy; a multi-hour meeting
            # can legitimately take longer than the ordinary interactive timeout.
            with httpx.Client(base_url=settings.suite_url, timeout=httpx.Timeout(30, read=1800), trust_env=False, follow_redirects=False) as client:
                result = client.post(f'/desktop/v1/requests/{data.request_id}/station-recordings',
                    headers={'X-Townlight-Session': proof}, json={'recording_id': str(data.recording_id), 'expected_sha256': data.expected_sha256})
            if result.status_code != 200:
                # Upstream detail stays in the Station log; staff get the
                # actionable message only.
                print(f'Records refused station handoff: {result.status_code} {result.text[:400]}',
                      flush=True)
                raise HTTPException(result.status_code if result.status_code in (401,403,404,409,422) else 503,
                                    'Records did not accept the recording. Check your Records role and request, then retry.')
            return result.json()
        except httpx.HTTPError:
            raise HTTPException(503, 'Records unavailable. Retry safely; existing attachments are preserved.') from None

    @app.get('/')
    def page():
        return FileResponse(static/'index.html', media_type='text/html')

    @app.get('/operator.js')
    def script():
        return FileResponse(static/'operator.js', media_type='text/javascript')

    @app.get('/operator.css')
    def style():
        return FileResponse(static/'operator.css', media_type='text/css')
