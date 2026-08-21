# ADR 0022: Typed output-profile contracts

Date: 2026-08-21

Status: Accepted

## Context

A station output cannot be represented honestly by only a UDP destination. Resolution, frame cadence, elementary-stream rates, transport rate, GOP cadence, audio rate, loudness policy, and scan type are one operational contract. Allowing arbitrary independent values would create combinations that compile but cannot be commissioned or supported. Keeping those values inside the media engine would make the operator interface and durable configuration lie about what the worker actually runs.

## Decision

TownLight Station uses a versioned, typed catalog of runnable output profiles owned by `station-schedule` and exposed read-only through `GET /api/v1/output-profiles`.

- Every channel persists one explicit profile identifier. Existing databases migrate to `proof-360p30`, preserving every row and retaining the prior low-load behavior.
- The profile identifier crosses every boundary unchanged: operator console, API, SQLite, channel controller, worker startup, and GStreamer construction.
- The worker receives the profile at process creation. It does not read mutable station configuration and cannot invent or partially apply a profile.
- `proof-360p30` remains a loopback commissioning profile. `atsc-720p5994` defines 1280×720 progressive video at 60000/1001, 6 Mb/s OpenH264 rate control, a two-second GOP, H.264 configuration recurrence every second for late receivers, 192 kb/s AAC at 48 kHz, and an 8 Mb/s constant-rate MPEG-TS with seven packets per UDP datagram and explicit PAT, PMT, and PCR intervals. Canonical media is normalized to the profile's -24 LUFS broadcast target during admission.
- A running output must be stopped before its destination or profile can change. The API returns `409 channel_must_be_stopped` instead of silently mutating durable configuration beneath a live worker.
- The machine gate captures the real UDP output, verifies MPEG-TS continuity, measures the transport rate and video PTS cadence, requires null-packet CBR padding, and independently discovers the encoded dimensions.

## Consequences

New output formats are catalog additions with code review and machine proof, not free-form operator fields. The current catalog proves one low-load profile and one progressive HD IP transport profile. It does not claim 1080i, 480i, SDI, NDI, SRT, SCTE signaling, or regulatory end-to-end compliance; those require their own runtime support, typed contracts, and hardware or receiving-system proofs. Redundant UDP-TS fanout is governed separately by ADR 0023. Profile edits intentionally require a visible stop/edit/start sequence until a dedicated maintenance transition can prove a bounded output restart.
