# ADR 0058: Automation policy API authority

## Decision

The authenticated API exposes read-only adapter and policy listings, one policy read, bounded policy
history, and current policy status. Policy writes require an enabled Setup Admin session and use a
path-owned policy identity plus compare-and-set revision. The API derives actor identity and time
from the authenticated server context; request bodies that try to include either are rejected.

Connection-test evidence has no browser route. A trusted in-process entrypoint derives the verified
installed candidate and current time on the server, applies a fixed 24-hour evidence TTL, then
delegates exact binding checks to the single SQLite writer. Its trusted observation type has no
candidate, timestamp, or expiry fields. Browser callers therefore cannot self-assert candidates,
credentials, model calls, test times, expiry, or results.

## Boundary

This API slice does not execute a model, make a provider request, handle credential material,
download a model, or render an operator UI. A trusted future adapter may record connection evidence;
that evidence remains a connection-test fact, not proof of model availability, output quality, or
automation execution.
