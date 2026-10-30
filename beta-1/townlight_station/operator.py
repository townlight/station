# SPDX-License-Identifier: AGPL-3.0-only
"""Short-lived operator sessions launched explicitly by authenticated Townlight staff."""
from pathlib import Path
import secrets
import time
from uuid import UUID
import httpx
from fastapi import Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field


class Ticket(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ticket: str = Field(min_length=32, max_length=128)


class Handoff(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    recording_id: UUID
    expected_sha256: str = Field(pattern='^[0-9a-f]{64}$')


def install_operator(app, settings, suite):
    tickets, sessions = {}, {}
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

    @app.post('/operator/handoff')
    def handoff(data: Handoff, proof: str | None = Header(default=None, alias='X-Townlight-Session')):
        suite.get('/desktop/v1/station/context?capability=read', proof)
        try:
            with httpx.Client(base_url=settings.suite_url, timeout=150, trust_env=False, follow_redirects=False) as client:
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
