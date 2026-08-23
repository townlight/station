# TownLight Station capability ledger

This ledger is the release-scope truth for the complete Station product. It separates product
implementation from proof: source code, tests, one-machine installed evidence, clean-machine
acceptance, headend acceptance, and field acceptance are different states. A capability is not
complete merely because an adjacent primitive exists.

The beta bar is unchanged: one operator must install from a double-click installer, commission and
operate one real station for one week, then three stations for another week, with every required
workflow and capability working. The ledger must be updated in the same commit that materially
changes a capability state.

Status vocabulary:

- `implemented`: the required product behavior exists across authority, API, operator workflow,
  and runtime boundaries. Proof may still be incomplete.
- `partial`: useful product behavior exists, but at least one required workflow or runtime boundary
  is absent.
- `missing`: no meaningful implementation of the required capability exists.
- `distributed`: a comparison/closure section whose requirements are judged in its owning sections.

| ID | Capability | Product status | Current truth | Remaining product work |
|---|---|---|---|---|
| S1 | StationBox profile and reference station | partial | One typed authority now derives live hardware, installed-engine factories, clock, route selection, backup, release-integrity limits, output gaps, and a fail-closed PEG readiness roll-up. | Reference hardware tiers, verified time/outbound/headend probes, authenticated installed-runtime manifest, operator workflow, and machine proof. |
| S2 | Headend handoff | partial | The six named, sourced headend profiles and three cable format contracts exist. Five network profiles can be durably staged on a stopped channel with strict URI/multicast/mux validation, per-sink loudness and 1316-byte packet contracts; file drop remains explicitly unimplemented. Staged contracts cannot be enabled until the engine enforces them. Fresh per-channel/per-sink readiness distinguishes local transport from external receiver acceptance. | Per-target audio/mux engine variants, AC-3/MPEG-2/interlaced execution, file-drop worker, TSDuck compliance, receiver/device probes, SDI/NDI paths, operator workflow, and installed/headend proof. |
| S3 | Commissioning | partial | Identity/timezone setup, channel configuration, authoritative core checks, headend-output readiness, and first-air guidance exist. | Canonical 11-screen workflow, tier-aware cable checks, headend profile setup, bounded output proof, complete evidence report, and phone-first completion workflow. |
| S4 | Commit to air | implemented | Draft, prepare, conflict/missing-media checks, operator approval, durable report, and acknowledged worker dispatch exist. | Same-candidate clean-machine and field proof; richer schedule authoring belongs to S19. |
| S5 | Software force matrix | partial | Revision-guarded, audited, channel-scoped UDP MPEG-TS live takeover and safe wall-clock handback exist. | Additional live transports, operator matrix depth, and complete failure/field proof. |
| S6 | CG and bulletin boards | partial | Durable boards, zones, bulletins, safe feeds, moderation, image assets, fixed compositor, and operator UI exist. | Full designer modes, scheduled/emergency behavior, live-video/audio zones, and remaining feed integrations. |
| S7 | Media lifecycle | partial | Durable ingest/transcode jobs, canonical media, loudness gate, watch folders, readiness dashboard, retries, and missing-media checks exist. | Replacement-source, retention policy, thumbnail/review depth, and lifecycle soak. |
| S8 | Health, alerting, support, and updates | implemented | Runtime truth, incidents, encrypted email/SMS/webhook delivery, re-alerting, resources, self-tests, support bundles, and transactional signed updates exist. | Production signing/trust provisioning and external/field proof. |
| S9 | Reliability and process identity | implemented | Supervised isolated workers, protected IPC, persistent graphs, output-progress heartbeats, bounded durable journals, restart recovery, and fail-closed corruption behavior exist. | Long-duration multi-station proof and remaining device-specific supervision introduced by later sections. |
| S10 | Proof ladder | partial | Installed proof records and an authoritative commissioning report distinguish configuration, execution, and external proof. | Unified capability/proof status surface and automatic evidence binding without self-authored release claims. |
| S11 | Captions, loudness, and EAS | partial | Canonical ingest loudness and per-output loudness targets exist. | CEA-708 pass/embed/decode proof, secondary audio, CAP/IPAWS/NWS ingestion, crawl/overlay/slate behavior, and EAS tone handling. |
| S12 | OTT applications | missing | No resident Roku, tvOS/iOS, Android TV/mobile, or Fire TV application is present. | Shared contracts, all required platform clients, accessibility, build automation, and machine compilation proof. |
| S13 | AI model selection | missing | No model registry, local/cloud adapters, selection authority, or operator workflow exists. | Resource-aware defaults, per-feature selection, local inference sidecars, optional cloud escalation, and proof. |
| S14 | Analytics and audience measurement | missing | No durable analytics event, rollup, snapshot, dashboard, PDF, year-over-year, or OTT beacon path exists. | Complete privacy-respecting live/VOD measurement and board/franchise reporting workflow. |
| S15 | Persistent playout engine | partial | Native GStreamer persistent raw-domain switching, one encode/mux, UDP/SRT branches, CG overlay, live takeover, and installed proofs exist. | Interlaced profiles, SDI, NDI, HLS/streaming outputs, additional live inputs, captions/EAS, and hardware/headend proof. |
| S16 | Production control room | missing | No device, cue, session, OBS/vMix/ATEM/HyperDeck/PTZ/OSC, GPI, or serial subsystem exists. | Complete optional-tier control-room authority, supervised adapters, UI, audit, and real-device proof. |
| S17 | Remote contribution | missing | No rooms, invitations, contribution sessions, VDO.Ninja bridge, TURN boundary, or operator workflow exists. | Complete contribution lifecycle, supervised runtime, security boundary, and field proof. |
| S18 | Comparative parity closure | distributed | Parity requirements are assigned to S19-S26 and the owning core sections. | Close every owning row; multi-site federation remains outside the current beta scope. |
| S19 | Scheduling automation | partial | One-off schedule items, prepare/commit, dispatch, wall-clock catch-up, and live handback exist. | Saved searches, recurring rules, blocks/dayparts, automatic materialization, and full schedule UI. |
| S20 | Accessibility | partial | Semantic HTML and selected keyboard/ARIA behavior exist in the installed operator console. | Complete WCAG 2.1 AA coverage, contrast enforcement, public surfaces, and OTT semantics. |
| S21 | Scheduled recording | missing | A live-finalization ingest enum is not a recording product. | Recording state machine, capture runtime, schedule integration, finalization, recovery, API, and UI. |
| S22 | Custom metadata | missing | No custom-field schema, values, facets, or management workflow exists. | Typed definitions, per-asset values, search/public facets, validation, and UI. |
| S23 | As-run, EPG, and franchise reporting | partial | The worker owns a CRC-protected, sequence-complete, rollback-safe durable on-air event journal. | UTC-timed as-run projection, reports, hours-by-category, X-List/XMLTV/CSV, EPG configuration, and UI. |
| S24 | Underwriting spots | missing | No spot, flight, placement, affidavit, or billing-export authority exists. | Complete trafficking, scheduling constraints, proof-of-airing join, reporting, and UI. |
| S25 | Meeting agendas | missing | No agenda, chapter, sync, import, staff, or public agenda workflow exists. | Agenda authority, text/PDF import, chapter sync, APIs, UI, and proof. |
| S26 | Optional subscription paywall | missing | No checkout, entitlement, magic-link, webhook, or operator workflow exists. | Default-off hosted checkout and entitlement system with signed webhooks and PCI-minimized boundaries. |

## Evidence state

The current exact installed candidate proves substantial portions of S3-S9, S15, and S23 on one
Windows machine. It does not prove a clean-machine install, external headend acceptance, the
one-station week, or the three-station week. Those gates remain false until their external evidence
exists. Detailed same-candidate records live in `docs/proof/`.

## Dependency order

The next vertical slice is S1 → S2 → S3: one computed StationBox/readiness authority, consumed by
headend/output checks and the commissioning workflow. This prevents later caption, SDI/NDI,
recording, contribution, and proof screens from inventing incompatible readiness vocabularies.
After that foundation, build the remaining operational slices in dependency order while keeping
the field gates external and honest.
