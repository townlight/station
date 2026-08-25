# ADR 0047: External receiver acceptance receipts

## Status

Accepted

## Context

Local transport progress and decoded exact-branch loudness prove facts inside TownLight Station. They
do not prove that a cable headend, downstream receiver, or return path accepted the signal. Treating
those local facts as receiver acceptance would make commissioning green without the external event
having occurred. External evidence must also remain auditable after an output or installed candidate
changes.

## Decision

`station-storage` owns an append-only receiver-acceptance ledger. A Setup Admin may record an
accepted or rejected observation for one enabled output sink using one explicit evidence method:
headend-operator confirmation, receiver telemetry, return-path observation, or visual/audio
observation. The request supplies only the external facts and their observation time. The API derives
the receipt UUID, recording time, authenticated actor, verified installed candidate identity, and
canonical current channel-output configuration hash. Callers cannot submit or override those binding
fields.

SQLite triggers reject update and delete. Append holds a write transaction while it verifies the
channel, enabled sink, and exact configuration hash. Evidence observed in the future, more than seven
days before recording, missing, or outside bounded text limits is rejected. The newest receipt for an
exact candidate/configuration/sink binding is current; later rejection supersedes earlier acceptance
without deleting history. Reconfiguration, sink disablement, or candidate change makes prior receipts
historical automatically.

The headend-readiness API maps a current accepted receipt to `verified`, a current rejected receipt to
`not_verified`, and no current receipt to `unknown`. Local transport evidence and external receiver
evidence are separate response fields. The loudness view reads the same external authority while
retaining an independent local measurement state. Commissioning consumes the resulting headend
authority; no browser route can directly set a commissioning check.

## Consequences

- TownLight records an external fact but never creates or self-awards it.
- Audit history survives output and release changes while current readiness fails closed.
- A receipt is operator/device evidence, not TownLight certification of cable standards, packet-loss
  tolerance, clean-machine installation, or week-long field operation.
- Real receiver/headend and field execution remain required acceptance gates.
