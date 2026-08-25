# ADR 0063: Remote-contribution authority

## Decision

`station-contribution` owns a pure, fail-closed authority for remote-contribution
rooms, expiring single-use invitations, claimed contribution sessions, and
connection/session receipt validation. A room binds permanently to one channel,
creating operator, VDO.Ninja bridge configuration identity, signalling origin, and
optional TURN relay intent. It has only `Draft -> Open -> Closed|Failed` lifecycle
transitions and revisioned CAS. Channel, operator, bridge configuration, endpoints,
and terminal room state cannot be rebound or revived.

VDO.Ninja intent contains a bounded HTTPS signalling origin and a non-secret
configuration UUID. Optional TURN intent contains only a bounded `turns://` relay
origin and credential-reference UUID. URLs with userinfo, paths, queries, fragments,
whitespace, or arbitrary/unbounded bytes are rejected. The contract has no password,
username, token, invite code, ICE credential, cookie, URL query, private key, or
other secret-bearing field. A credential reference is an opaque pointer for a later
server-side secret boundary, not credential material.

Only the bound operator can open/close a room and issue/revoke its invitations. An
invitation binds exactly one contributor and has a bounded lifetime. It can be
claimed once by that contributor for one declared session, revoked before use, or
expired after its deadline; claimed, revoked, and expired invitations are terminal.
An operator creates a session only from that exact claimed invitation and exact open
room, binding one worker and bridge configuration. The session has only
`AwaitingWorker -> Connecting -> Connected -> Ended|Failed` transitions. A worker
may append the exact next receipt in a bounded ordered receipt ledger. Receipt
identity, sequence, room/session/invitation/worker binding, time order, and
single-use append rules are verified; replay or replacement fails closed. A
`RecoveryStopped` receipt can only transition a nonterminal session to `Failed`,
never resume or reanimate it.

## Boundaries and non-claims

This slice has no SQLite persistence, durable receipt ledger, authentication or
authorization implementation, invitation delivery, secret store, credential
resolution, VDO.Ninja bridge, TURN relay, WebRTC/ICE/media adapter, network listener,
runtime worker, supervision, API, operator UI, or installed-machine proof. It does
not establish that an endpoint exists, an invitation reached a contributor, a TURN
credential resolves, a bridge or relay connected, a media session ran, a receipt was
durably recorded, or media was received or delivered. Future work must derive roles
server-side, provision secrets outside this contract, append receipts through the one
SQLite writer, implement supervised runtime adapters and operator workflow, and gain
installed-machine and field evidence.
