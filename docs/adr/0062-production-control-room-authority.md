# ADR 0062: Production-control-room authority

## Decision

`station-control-room` owns a pure, fail-closed optional-tier contract for revisioned
control devices, sessions, cues, bounded command intent, and immutable worker
receipts. It covers typed non-secret contracts for OBS, vMix, ATEM, HyperDeck, PTZ,
OSC, GPI, and serial devices. Each device has a stable identity, kind, endpoint form,
and supported capability set; identities, kinds, and endpoints cannot be rebound.
Sessions bind permanently to one channel and bounded unique device-ID set. Cues bind
exactly one current session and device revision, require that session to admit that
device, and may only name a command supported by that exact device capability.

Only an operator actor can create or replace devices, sessions, cues, or command
intent. A command intent must reproduce the exact current cue, session, device, and
typed command. Only a worker actor may append a receipt. Receipts are append-only:
receipt identity and command-intent identity are each accepted once, so replayed
acknowledgements fail closed. Creation is revision one with no expected revision;
replacement advances exactly one revision from the named current revision.

Endpoints are typed and bounded rather than free-form URLs: network devices have a
host and nonzero port, GPI has a bank, and serial has a visible port name. The model
contains no credential, URL path, secret, arbitrary byte stream, connection status,
or physical-execution field. Commands are likewise typed and bounded: an OBS scene,
switcher input, HyperDeck transport action, bounded PTZ vector, bounded OSC address
and arguments, bounded GPI pulse, or bounded visible serial payload.

`accepted_for_dispatch` and `rejected_before_dispatch` receipts are deliberately
worker acknowledgements only. They are not evidence of connection, device delivery,
device execution, program output, recording, or physical state.

## Boundaries and non-claims

This crate has no SQLite persistence/history, authentication implementation,
operator UI, API, adapter, device discovery, network/serial/GPI/OSC connection,
command worker, retry policy, session runtime, supervision, audit delivery, or
installed-device proof. It does not establish that an endpoint exists, credentials
are valid, a device accepts a command, a command reaches a device, or a production
state changes. Later slices must derive actors server-side, persist CAS history and
immutable receipts through the single SQLite writer, securely provision secrets,
implement supervised adapters and UI, and acquire installed-machine and real-device
evidence.
