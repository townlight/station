# ADR 0024: Protected worker bootstrap

Date: 2026-08-21

Status: Accepted

## Context

Worker process arguments are visible to local process-inspection tools and are routinely copied into diagnostics. Passing output destinations there was unnecessary exposure; passing a future SRT passphrase there would be unacceptable. The worker still needs a complete immutable output contract before it can construct its media graph and claim readiness.

## Decision

The worker command line contains only its worker identity, channel identity, journal path, and private named-pipe name. After the worker connects, the supervisor sends one versioned length-framed bootstrap envelope over that pipe before accepting any worker event.

- The bootstrap carries the expected worker/channel identities, typed output profile, and complete output set.
- The worker rejects an incompatible protocol major, mismatched identity, unsupported profile, malformed destination, or empty output set before constructing GStreamer.
- The worker cannot emit `Ready` until it has decoded the bootstrap and started the exact requested fallback graph.
- The pipe already rejects remote clients, permits one server instance, and is ACL-restricted to SYSTEM, Administrators, and the creating identity. Output configuration therefore uses the same protected local boundary as later commands and events.
- Bootstrap values are not journaled. Durable public configuration remains in SQLite; secrets added to this envelope in the future must originate from machine-protected storage and exist only in service/worker memory.

## Consequences

Output destinations and transport credentials do not appear in process listings or installer/service arguments. Protocol minor version 1.3 carries a debug-redacted optional SRT passphrase in the bootstrap envelope without changing the command/event major contract. A worker can no longer be launched as a useful standalone playout process by supplying destinations on its command line; it requires an authenticated local supervisor connection and valid bootstrap frame.
