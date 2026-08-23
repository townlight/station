# ADR 0031: Authoritative commissioning report

## Decision

Commissioning progress is derived from current station authority, not from browser checkboxes or a mutable completion flag. `GET /api/v1/commissioning/report` schema version 2 returns the server-owned current step, required-step progress, `core_complete`, and an ordered set of typed checks. Every check identifies its phase, status, required/blocking behavior, operator action, detail, evidence source, and whether its authority is local or external. The browser has no completion-write route; its local storage may remember navigation only.

The ordered required local checks keep these facts and obligations separate:

- station identity and an IANA timezone exist;
- operator account/authentication authority exists;
- the API, SQLite, StationBox hardware/engine/media storage, synchronized time, and backup probes pass;
- an enabled watch folder has a present directory and durable successful scan;
- fallback/slate, CEA-708, publish-target, and automation/model-policy authorities exist;
- a named headend contract has fresh local required-sink transport;
- TSDuck transport analysis and target-device reachability evidence exist;
- one completed ingest job resolves to a ready managed asset whose bytes remain present;
- an exact committed schedule/report references that same asset after ingest completed;
- that exact report reaches worker `acknowledged` or `completed` for guided air proof, and `completed` for the first-broadcast report;
- a durable ten-minute selected-sink output proof and commissioning-linked support-bundle receipt exist.

Missing current authorities are explicit required `blocked` checks. Generic output sinks do not satisfy publish-target account authority; headend destinations belong only to headend setup. Current fallback output does not invent an operator-selected slate, current caption-free media does not invent CEA-708, short output proofs do not satisfy ten minutes, and an ephemeral support-bundle response is not a durable commissioning receipt.

External receiver acceptance, clean-machine installation, one-station one-week operation, and three-station one-week operation remain distinct non-required `not_verified` checks until authenticated external evidence exists. `overall_status` remains `blocked` while any required check is not `pass`; after every local requirement passes it may remain `warning` because external checks are not verified. `core_complete` refers only to the complete required local set and cannot be written by a client.

## Consequences

A ready catalog row, an unrelated historical commit report, or a report approved before the qualifying ingest completed cannot advance the guided chain. The report joins durable ingest completion, current asset/file readiness, exact channel/item/asset commit authority, and worker-owned dispatch state. SQLite authorities survive service restart, while current filesystem, StationBox, and runtime probes can regress a previously passing step when evidence disappears or becomes stale.

External-receiver roll-up is fail-closed: at least one channel must be enabled, every enabled channel must have at least one enabled required sink, and every such sink must carry verified receiver evidence. Disabled-only and empty-required-sink configurations cannot pass vacuously.

Installed evidence is produced separately by `scripts/prove-installed-commissioning.ps1`. That harness validates exact installed hashes against the candidate manifest, operates only through public loopback APIs, captures actual dispatch states, composes decoded CG apply/change/clear evidence, restarts the Windows service, and verifies recovery. Passing this harness proves one candidate on one machine, not the required week-long field gates.
