# TownLight Station project status

**Status date:** 2026-08-29  
**Release verdict:** not a beta release; not a complete PEG/Cablecast-equivalent product  
**Repository truth:** `main`, `README.md`, `CAPABILITIES.md`, and the evidence records under `docs/proof/`

## Bottom line

TownLight Station is a substantial, test-heavy Windows station kernel and control-plane vertical
slice. Its strongest areas are process isolation, persistent GStreamer playout, durable authority,
failure handling, exact artifact identity, transactional update/rollback, and evidence that refuses
to turn partial results green.

It is not presently a product that can operate an entire PEG station. It does not provide the full
operator, resident, civic-integration, media-generation, transport, publishing, business, and device
surface required for functional parity with an established PEG automation product. The capability
ledger marks only S4, S8, and S9 `implemented`; 22 rows are `partial`, and S18 is a distributed
comparison row.

No current-source installer has passed the complete beta gate defined below. A reachable service,
green unit tests, source-level proof, an older installed candidate, or a component receipt must not
be represented as that result.

## Current beta gate

One immutable source commit and its exact installer must complete all of the following on a fresh
Windows machine:

1. install through the ordinary double-click operator path;
2. start both Windows services and verify installed payload identity;
3. bootstrap the first operator and configure the station and at least one channel;
4. ingest real media, commit a schedule, and prove that program identity—not only fallback—is on air;
5. configure and independently decode the expected captions;
6. complete the required commissioning evidence without browser-authored completion;
7. restart the services and recover the same station/channel authority; and
8. run continuously for four to eight hours with observed output, health, resource, and failure
   coverage.

This is a narrow station-kernel beta gate. Passing it would not close the remaining S1-S26 product
scope or prove production-headend acceptance.

## What is materially implemented

- Native Windows service and maintenance-service packaging, clean-install/reinstall protections,
  installed payload manifests, and transactional signed-update/rollback machinery.
- One isolated worker process per channel over protected named pipes, typed protocol sequencing,
  bounded supervision, durable journals, restart recovery, output-progress heartbeats, and fail-closed
  corruption handling.
- Persistent GStreamer playout with fallback, scheduled assets, UDP MPEG-TS live takeover and
  wall-clock handback, progressive UDP/SRT output variants, per-sink loudness normalization, and
  exact capture/proof paths.
- Content-addressed ingest/transcode, watch folders, readiness/missing-media authority, immutable
  replacement relationships, and non-destructive retention review intent.
- Draft/prepare/approve/commit scheduling, acknowledged dispatch, recurring materialization, and
  journal-derived as-run projection/export.
- Revisioned station, channel, fallback-slate, caption, publishing, automation-policy, metadata,
  agenda, recording, catalog, alert, and commissioning authorities.
- CEA-708 cue transport, transition clearing, missed-cue incidents, asset-bound WebVTT/SRT review,
  bounded egress capture, and independent decode-back verification.
- CG board/zone/bulletin/image authority, moderation, safe feed ingestion, transparent still-image
  compositor, snapshot/proof support, and operator console surfaces.
- Durable alerts and delivery attempts, resource sampling, self-tests, support bundles, and an
  authenticated same-origin React operator console.

These statements describe implementation and targeted evidence. They do not imply complete operator
journeys, field acceptance, or full product parity.

## Major executable product gaps

The detailed source of truth is `CAPABILITIES.md`. The largest gaps for a station operator or resident
are:

- no complete public/resident portal or native OTT applications;
- no live ASR caption generation or full caption review/generation operation;
- live input is limited to UDP MPEG-TS; no complete RTMP/RTSP/HLS/NDI/SDI/contribution matrix;
- no production EAS/CAP/IPAWS orchestration or certification claim;
- progressive IP headend profiles exist, but interlaced/MPEG-2/AC-3/file-drop and physical SDI/NDI
  execution are incomplete;
- no complete production AI adapter, credential, and semantic-quality workflow;
- remote contribution and control-room/device integrations are primarily contracts, not executable
  station workflows;
- analytics, underwriting, subscription, agenda-provider import, report delivery, external publishing,
  and public playback remain incomplete or only partially wired;
- accessibility has source-contract coverage, not full rendered-browser/manual assistive-technology
  proof;
- no production cable-headend acceptance or field deployment evidence.

## Evidence and release state

- The repository contains extensive Rust unit/integration tests, real child-process and GStreamer
  tests, PowerShell contract harnesses, and Node contract tests.
- The web suite is largely contract/static testing. It does not provide a real-browser end-to-end
  acceptance spine for bootstrap, ingest, scheduling, output, restart, and recovery.
- Historical installed proof records are candidate-specific. They cannot be combined across commits
  to manufacture one all-up pass.
- `dist/` is intentionally ignored, and this repository currently has no public release artifact,
  tag, or release workflow.
- Development installer production is pinned to exact local Rust, MSVC/SDK, GStreamer, Inno Setup,
  FFmpeg, and TSDuck identities. A clean clone alone cannot acquire that complete toolchain or
  reproduce a candidate.
- Development installers are unsigned. Code signing and timestamping are mandatory before external
  distribution.
- The most recent source changes have not been demonstrated by one current-source, signed,
  fresh-machine, four-to-eight-hour passing receipt.

### Handoff verification on 2026-08-29

- Rust formatting check passed.
- Operator-console tests passed: 109/109.
- Operator-console production build passed; the tracked bundle was regenerated from the changed
  source. Vite reported the existing warning that `operator.js` exceeds 500 kB.
- The corrected commissioning-report regression passed in isolation: 1/1.
- The complete `station_profile_api` integration file did **not** pass as one run: 27 passed and six
  failed. Five failures were loopback HTTP timing/response tests; one was a Windows media temporary-file
  lock. Representative HTTP and media failures both passed when rerun individually, so this is an
  unresolved suite-interaction/reliability defect, not a green gate.
- Installed-proof caller contract passed: 144 assertions.
- Installed beta-soak static harness passed: 55 assertions.
- Windows Sandbox beta-soak static harness passed: 47 assertions.
- Installer contract passed: 95 requirements.

No installer, service mutation, Windows Sandbox, or soak was run for this handoff verification.

## Architecture strengths worth preserving

- Single SQLite writer with transactional, revisioned, append-only evidence boundaries.
- Per-channel worker isolation instead of one shared media failure domain.
- Persistent media graphs instead of per-item process churn.
- Exact candidate, installed-runtime, configuration, media, and receipt binding.
- Fail-closed sequence/journal/recovery behavior.
- Handle-relative and no-follow safety work around managed artifacts and NAS publication.
- Honest separation of configuration, execution, decoded measurement, receiver acceptance, and field
  proof.

## Architecture and delivery risks

- `station-api` and `station-storage` are very large integration choke points despite the wider crate
  decomposition.
- Strict authority boundaries can become operator dead ends when no actionable recovery path exists.
- The flat, feature-oriented operator navigation does not yet match how a volunteer runs a station.
- The repository has accumulated broad domain contracts faster than complete installed vertical
  workflows.
- Local build-tool pinning improved provenance on one workstation but did not create a reproducible
  public release pipeline.

## Project execution failure

The project was authorized as a fresh architecture that would deliberately mine an archived
predecessor for reusable code, algorithms, fixtures, tests, and proven workflows. The archive was
created, but implementation proceeded almost entirely as a cleanroom build. A whole-file comparison
found no direct predecessor file reuse, and no systematic port history exists. Requirements and
behavior were consulted, but working implementation was not meaningfully quarried.

That was a material execution failure. It invalidated the early assumption that the fresh build
could reach the predecessor's functional breadth in roughly the time required to finish/refactor the
existing product. Attempting a comprehensive late quarry would now amount to a second refactor and
is not recommended without a new product decision.

## Recommended takeover order

1. Decide whether TownLight is a narrow station kernel or a funded full replacement program. Do not
   use one schedule or status label for both.
2. If pursuing the narrow beta, freeze one clean commit, make installer production reproducible,
   sign it, and run the exact gate above. Do not add another domain before that receipt passes.
3. Add a rendered browser acceptance spine and correct station-time/DST handling before a volunteer
   operator trial.
4. If pursuing full replacement, plan executable vertical slices—storage, API, runtime, UI,
   installer, and proof together—for the gaps listed above. Do not count a crate, schema, contract,
   or screen shell as a capability.
5. Preserve evidence-tier language in every status: claimed, source-wired, test-proven,
   installer-proven, or field-proven.

## Handoff integrity

Before this handoff change, the canonical repository and all registered worktrees were clean.
Branches not reachable from `main` by commit identity contained only patch-equivalent changes
already integrated into `main`; no unique hidden implementation was awaiting merge. The repository
should be handed off from `origin/main`, not from an old feature worktree or an ignored `dist/`
candidate.
