# ADR 0080: Station-driven scheduled-recording finalization

## Status

Accepted for the source implementation. Installed-candidate and field proof
remain outstanding, so this ADR does not claim operational completion.

## Decision

Scheduled recording execution belongs to the existing per-channel
`ChannelController`. A second process must not compete with that controller for
the channel worker's duplex command stream. On every reconciliation tick, the
controller reads a bounded set of actionable recording authorities for its
channel and applies at most one command per recording against the same
`WorkerSupervisor` that owns playout, captions, live control, and proof taps.

An armed recording is bound to the exact verified installed candidate and
channel-configuration hash already stored by the S21 authority. The controller
must re-derive both values before starting capture. Because the operator does
not select an output, the capture binding is deterministic: the first enabled
required sink in configured order, or the first enabled sink when none is
required. The selected output index and sink identity are persisted before a
later terminal event can be accepted. Reordering, replacing, disabling, or
otherwise changing the output configuration after arm makes the recording fail
closed; it cannot silently move to another output.

The controller starts capture at the server-authoritative schedule start. A
small explicit start deadline covers controller tick and command latency; a
recording that misses that deadline is failed rather than represented as a
complete capture. Duration is the remaining authoritative schedule window and
the byte ceiling is derived from the selected output profile's transport rate
with bounded overhead, never exceeding protocol v12's 128-GiB ceiling. The
controller validates every Started, Pending, Ready, or Failed event against the
recording identity, channel, worker instance, graph revision, output index,
sink, duration, byte ceiling, and managed relative path expected for that
attempt. An unexpected or delivery-ambiguous response retires the worker before
durable failure is recorded.

The database keeps an append-only execution ledger beside the existing
recording transition history. It records the exact start acknowledgement and
terminal worker evidence, including worker identity/sequence, graph revision,
output binding, managed path, bytes, capture hash, and duration. The ordinary
`Armed -> Capturing -> Finalizing -> Completed` transition receipts and this
worker evidence are committed together at their corresponding boundaries. A
worker failure transition remains writeable using the recording's stored armed
execution authority even when the schedule, candidate, or output dependency
has become stale; otherwise the very condition that caused failure would strand
the recording in a nonterminal state. Start and successful finalization still
require every current dependency to match inside the same SQLite write
transaction.

Ready bytes are not accepted by pathname trust. One controller-owned background
finalizer performs all potentially long-running file and media work, while the
station-critical reconciliation loop only polls a bounded result channel. No
second finalization task can start for that channel until the first returns.
The finalizer resolves the
worker-owned relative path only beneath the managed recording root, rejects
reparse points and unexpected file types, retains a no-delete handle, and
rehashes and recounts the exact opened object against the terminal event. It
validates finite decodable audio/video media, then copies from that retained
handle into a create-new content-addressed part in the managed media library,
flushes and synchronizes it, atomically renames it, and reopens and verifies the
final object. A database transaction then:

- admits the exact media object as Ready;
- creates an idempotent Draft catalog record using the authoritative schedule
  title, without inferring meeting body or publication state;
- appends immutable recording-finalization evidence binding source capture and
  destination asset; and
- commits the `FinalizeSuccess` transition with the same hash and measured
  duration.

The filesystem publication necessarily precedes the SQLite commit. A crash may
therefore leave an unreferenced content-addressed object, but cannot leave a
completed recording that references missing or different bytes. Retrying
finalization is idempotent: existing destination bytes must match exactly, and
the same evidence/transition is accepted only as the same completed result.

## Recovery

The controller does not claim to resume an interrupted continuous capture.

- `Armed` before its start remains eligible for the normal start.
- `Armed` after the start deadline becomes Failed.
- `Capturing` after a controller restart has immutable start evidence, but the
  lost worker session cannot be resumed and therefore becomes Failed. A stale
  `.part` file is never promoted.
- Worker Ready facts and the `Capturing -> Finalizing` transition commit in one
  SQLite transaction. Thus a restart observes either Capturing with no accepted
  Ready authority, or Finalizing with the exact durable Ready evidence needed
  for publication; it never searches unbounded journal history to guess.
- `Finalizing` reruns handle-based verification and content-addressed
  publication. It does not invent a new capture or change the recorded worker
  facts.
- A sealed Ready capture survives a stationd restart. An unsealed or ambiguous
  capture does not.

The worker journal durably precedes each emitted worker event. The exact emitted
start and Ready envelopes are then copied into the SQLite execution ledger at
their lifecycle boundaries. SQLite remains the sole recovery authority for
recording lifecycle and catalog state; the controller does not perform an
unbounded journal replay inside its critical loop.

## Boundaries and proof

Unit and child-process tests must cover exact start timing, deterministic sink
selection, configuration/candidate staleness, command-delivery ambiguity,
worker failure, Ready/hash/path mismatch, graph revision changes, restart from
each nonterminal state, idempotent file-before-database recovery, and recording
plus commissioning proof on the same mux branch. Source completion still does
not prove an installed recording. The capability remains partial until the
same installed candidate records and finalizes real scheduled media through
the operator workflow, and it remains short of beta until the one-station and
three-station field gates pass.
