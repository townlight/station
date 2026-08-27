# ADR 0078: Independent scheduled-recording capture

## Decision

Protocol v12 gives the persistent per-channel worker a scheduled-recording
capture contract separate from the existing commissioning output-proof
contract. A start command binds one canonical recording identity to an exact
configured output index and sink identity, a finite duration, and a hard byte
limit. Poll and abort commands carry only that recording identity. Started,
pending, ready, and failed events bind the worker instance and graph revision;
a ready event also reports the exact managed relative path, byte count, SHA-256
digest, and contractual capture duration.

Scheduled recordings are limited to three seconds through 24 hours and 128
GiB. Commissioning proof remains limited to its existing 604 seconds and 1
GiB. Each configured post-mux output branch owns two independent bounded tap
ingresses, so recording cannot monopolize or weaken the proof capture. Both
write through finite queues to `create_new` part files, flush and sync bytes,
then atomically rename inside separate managed roots. Graph revision drift
causes a recording to fail rather than attach evidence from a replacement
graph. Worker shutdown and drop abort and join both capture classes.

The channel worker derives `recordings/<channel UUID>` as a sibling of its
already confined `output-proofs` root under the journal-owned managed data
root, rejects an escaped/non-directory parent or channel root before Ready,
and removes stale part files only inside its own channel directory at startup.
One channel can therefore never clean another channel's active recording. A
real child-process contract proves simultaneous recording and proof
capture from one mux branch, independent terminal events, managed artifacts,
and independent SHA-256 verification.

## Boundaries and non-claims

This slice is the native capture boundary. It does not yet claim stationd
schedules start/poll/finalization, that a worker crash can resume a continuous
capture, or that the transport artifact has been promoted into the catalog as
a managed media asset. Those remain controller, recovery, and finalization
work. Protocol presence and source tests are not installed-machine or field
proof.
