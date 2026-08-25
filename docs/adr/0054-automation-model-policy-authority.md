# ADR 0054: Automation model-policy authority

## Decision

`station-automation-policy` owns a pure, server-authoritative domain contract for the three
automation features: captions, summaries, and translation. Each feature has one bounded,
revisioned policy. A policy can name an explicitly operator-selected adapter and can retain an
adaptive-default hint, but a hint is advisory data only: it never creates a selection, starts a
model, or records an execution decision. Its distinct preferred and fallback adapter references
must still resolve to valid descriptors that support the policy's feature, preventing a stale hint
from accompanying an otherwise passing selected policy.

Adapter descriptors are capability descriptions only. They state a stable adapter identity,
local/cloud location, model key and identifier, supported features, revision, and a non-secret
credential availability fact. Cloud descriptors require an explicit `available` or `unavailable`
credential fact; they cannot claim that no credential is needed. No policy or descriptor contains
credential material.

A connection-test result can pass only when it binds the exact selected policy and adapter, their
current revisions, policy configuration hash, candidate version and source commit, and a bounded
non-future test interval. Missing operator selection, unsupported or unknown adapters, unavailable
credentials, malformed/future/expired/stale/failed evidence, and duplicate authority records fail
closed. Overall automation readiness is ready only when all three feature policies have current
passing evidence; it is otherwise blocked or unknown without inferring execution.

## Boundaries and non-claims

This slice makes no model call, provider request, download, hardware probe, adaptive-default
decision, adapter discovery, SQLite write, API/UI response, or job execution. It does not establish
that an adapter is installed, network-reachable, licensed, affordable, private, capable of a
quality bar, or able to produce captions, summaries, or translations. A passed connection test is
only server-supplied current connection-test evidence, not a model-output or operational proof.

Provider and credential stores remain outside this crate. Later trusted server-side adapters must
collect connection evidence and separately prove model availability and feature-specific output
quality before any automation execution or readiness claim is broadened.
