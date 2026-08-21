# ADR 0003: Supervised channel-worker lifecycle

Date: 2026-08-20

Status: Accepted

## Context

Each channel worker is a fault-isolated process that owns live media execution. Starting a process is not sufficient evidence that it is safe to command: the station must know that the intended worker connected to the intended private boundary, recovered its durable history, and is responding at the expected sequence. A failed or abandoned launch must not leave an orphan process or block station startup forever.

## Decision

`station-runtime` is the station-side owner of each worker process and session.

- The supervisor creates a unique, exclusive, ACL-protected pipe before launching the worker.
- Worker standard input and output are null handles; only the named pipe carries protocol traffic.
- One total startup deadline covers process creation, pipe connection, and the first event.
- The first event must be a nonzero `Ready` from the configured worker and channel. Its durable sequence may be greater than one after journal recovery.
- Each command carries the next expected sequence. Every response must match the configured identities and that exact sequence before supervisor state advances.
- Reads are bounded by response deadlines; incomplete or missing responses fail visibly.
- Clean shutdown requires a `ShutdownComplete` event followed by a successful process exit within the same deadline.
- Failed launch, timeout, unexpected event, abandoned supervisor, and failed shutdown all terminate and reap the child process.

## Consequences

The station service has one explicit lifecycle owner rather than scattered process and pipe calls. A worker cannot be treated as ready solely because its process exists, and a stale or wrong-channel event cannot advance station state. Restart continues from the journal-derived sequence without special transport behavior.

The current supervisor performs one command/response exchange at a time. Unsolicited telemetry and concurrent command scheduling will require a dedicated session loop, but they must preserve the same identity, ordering, deadline, and cleanup rules.
