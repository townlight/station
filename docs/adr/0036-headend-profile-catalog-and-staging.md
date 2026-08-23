# ADR 0036: Headend profile catalog and fail-closed staging

Date: 2026-08-22

Status: Accepted

## Context

Operators need named cable-delivery presets rather than reconstructing video, audio, multiplex,
packet, and destination rules from carriage documents. The product also needs to expose the full
required contract without making formats selectable before the media engine can execute them.
Configuration, execution, local measurement, receiver acceptance, and field proof are different
facts and must not collapse into one status.

## Decision

`station-schedule` owns a static six-entry headend catalog:

- Generic CBR SPTS over UDP;
- Comcast MTD SD;
- Comcast MTD HD;
- TelVue HyperCaster IP;
- Harmonic Spectrum transport-stream ingest;
- Leightronix UltraNEXUS file handoff.

Every entry includes stable identity, label, vendor, source URLs, canonical cable format,
transport, default mux rate, 1316-byte UDP packet contract, vendor minimum port,
operator-supplied fields, current execution state, and explicit field/headend non-claims. The
catalog also defines 720p59.94, 1080i59.94, and 480i29.97 format contracts. Only the two profiles
the current engine executes remain in the runnable output-profile list; the interlaced contracts
cannot appear as selectable runnable outputs merely because their metadata exists.

`GET /api/v1/headend-profiles` serves this catalog without requiring station or database state.
`POST /api/v1/channels/{channel_id}/headend-profile` accepts a profile, destination, optional mux
override, and replace-or-keep choice. Application acquires the SQLite write lock, rereads channel
state, checks that it is stopped, applies the contract, and persists it in the same transaction.
Ordinary channel writes compare their previously read complete channel snapshot again under the
write lock; an intervening profile stage or any other channel change returns a conflict instead of
overwriting newer authority. The generic route cannot create, edit, or remove headend metadata.
The pure application function rejects unknown profiles, non-IP UDP authorities, credentials or
URI options, missing or vendor-disallowed ports, unicast/multicast mismatch, and an impossible or
unbounded mux rate before storage changes.

The applied UDP sink stores a typed headend contract containing profile identity, mux rate, and
packet size. SQLite migration adds those fields with empty/zero defaults for existing ordinary
outputs. Profile proof boundaries remain derived from the immutable catalog; no mutable
`headend_proven` field is persisted.

The current worker still has one shared audio encoder and transport mux, so it cannot enforce two
different per-sink loudness targets. All network headend entries therefore report `staged_only`,
and a channel carrying a staged headend contract cannot be enabled. The Leightronix entry reports
`unimplemented` and cannot yet be applied because file delivery is a separate output worker, not a
UDP sink. The worker and media engine also independently reject every profile absent from the
runnable catalog, preventing a bypassed bootstrap from rendering an interlaced contract as
progressive video. These gates are removed only with corresponding executable and proof-bearing
slices.

## Consequences

The complete product contract is visible and persistable without creating a false runnable or
field-proven claim. Staged headend choices survive restart and retain cable `-24 LUFS` separately
from an existing streaming `-16 LUFS` sink. The next engine slice must group outputs by loudness
target, keep one video encoder, and create one audio-normalization/encoder/mux chain per distinct
target before any staged headend channel may be enabled. AC-3, MPEG-2, interlaced execution,
file-drop delivery, compliance measurement, receiver acceptance, and field operation remain
separate gates.
