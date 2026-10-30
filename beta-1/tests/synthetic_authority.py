# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic Townlight authority stand-in for isolated Station acceptance.

This is a test double for the *authority* service only. It is not a Townlight
product, is not installed, is not shipped in the wheel and uses no city data. It
lets Station's real HTTP permission checks, meeting/agenda snapshot retrieval and
actor identity run without any external system or credentials. Requests are
authorized from an explicit synthetic role table: unknown proofs get 401, roles
without the capability get 403 -- the same contract the real desktop bridge uses.

Catalog entries are persisted to the path in STATION_FIXTURE_CATALOG so the
multi-stage acceptance run (one foreground process per stage) keeps the same
synthetic meeting across Station restarts.
"""
import json
import os
import sys

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import uvicorn

ROLES = {'clerk': ('capture', 'read'), 'records-staff': ('read',)}
SESSIONS = {}
CATALOG = {'meetings': {}, 'agenda': {}, 'requests': []}
STORE = os.environ.get('STATION_FIXTURE_CATALOG')

app = FastAPI()


def load():
    if STORE and os.path.exists(STORE):
        CATALOG.update(json.loads(open(STORE, encoding='utf-8').read()))


def save():
    if STORE:
        with open(STORE, 'w', encoding='utf-8') as stream:
            json.dump(CATALOG, stream, indent=2)


def principal(proof, capability):
    role = SESSIONS.get(proof)
    if not role:
        raise HTTPException(401, 'Synthetic authority: unknown session')
    if capability not in ROLES[role]:
        raise HTTPException(403, 'Synthetic authority: role lacks capability')
    return {'actor_id': f'actor-{role}', 'role': role, 'capability': capability}


load()


@app.post('/__fixture/session')
def add_session(proof: str, role: str):
    if role not in ROLES:
        raise HTTPException(422, 'unknown role')
    SESSIONS[proof] = role
    return {'ok': True}


class Meeting(BaseModel):
    id: str
    title: str


class Item(BaseModel):
    id: str
    meeting_id: str | None = None
    title: str


@app.get('/desktop/v1/station/context')
def context(capability: str, proof: str | None = Header(default=None, alias='X-Townlight-Session')):
    return principal(proof, capability)


@app.post('/desktop/v1/meetings/meetings')
def make_meeting(data: Meeting):
    CATALOG['meetings'][data.id] = data.model_dump()
    save()
    return CATALOG['meetings'][data.id]


@app.post('/desktop/v1/meetings/agenda-items')
def make_item(data: Item):
    CATALOG['agenda'][data.id] = data.model_dump()
    save()
    return CATALOG['agenda'][data.id]


@app.get('/desktop/v1/meetings/meetings/{identity}')
def get_meeting(identity: str):
    if identity not in CATALOG['meetings']:
        raise HTTPException(404, 'no such meeting')
    return CATALOG['meetings'][identity]


@app.get('/desktop/v1/meetings/agenda-items/{identity}')
def get_item(identity: str):
    if identity not in CATALOG['agenda']:
        raise HTTPException(404, 'no such agenda item')
    return CATALOG['agenda'][identity]


@app.get('/desktop/v1/meetings/station/catalog')
def station_catalog(proof: str | None = Header(default=None, alias='X-Townlight-Session')):
    """The station capture catalog: same capability gate as the real bridge."""
    principal(proof, 'capture')
    return {'meetings': list(CATALOG['meetings'].values()),
            'agenda': list(CATALOG['agenda'].values())}


@app.get('/desktop/v1/records-state')
def records_state(proof: str | None = Header(default=None, alias='X-Townlight-Session')):
    """Synthetic Records state: only requests staff may see, never documents."""
    principal(proof, 'read')
    return {'records_requests': CATALOG['requests']}


@app.post('/__fixture/request')
def add_request(request_id: str, summary: str):
    CATALOG['requests'].append({'request_id': request_id, 'summary': summary})
    save()
    return {'ok': True}


if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[1]), log_level='warning')
