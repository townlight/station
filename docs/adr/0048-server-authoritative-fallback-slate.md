# ADR 0048: Server-authoritative channel fallback slate

## Status

Accepted, 2026-08-25.

## Decision

Each channel may select one immutable managed PNG/JPEG as its fallback slate. The selection is a
SQLite compare-and-swap authority, may change only while the channel is stopped, and records a
server-generated audit identity, authenticated Setup Admin account, server time, and monotonically
increasing revision. Audit rows are append-only. The caller supplies only the image identity and
expected revision.

The image library remains content-addressed and is re-opened, rehashed, format-checked, bounded,
and decoded at each worker start. Protocol major 5 carries only the selected SHA-256 and format;
paths never cross the control boundary. The worker installs the image in a fallback-only overlay
branch, preserves aspect ratio against black, and reports `Ready` only after the exact image and
the complete output graph have started. A missing, corrupt, substituted, escaping, or undecodable
selection is a startup failure. Program, live, ordinary CG, and EAS retain separate priorities and
authorities.

Commissioning passes `fallback_slate` only when at least one channel is enabled, every enabled
channel has a currently verified selection, and current heartbeat-derived runtime is green.

## Consequences

- Changing the slate requires a deliberate stop/start so worker state cannot diverge from storage.
- Historical selections remain attributable without treating stale images as current readiness.
- This closes the ordinary station fallback requirement. EAS forced slate/tone is still separate
  S11 work and receives higher priority when implemented.
