# ADR 0065: OTT application contract

## Decision

`station-ott-contract` owns a pure, versioned public interchange contract (current
wire version 2) for Roku,
Apple, Android, and Fire TV clients. It defines bounded catalog pagination, tagged
unambiguous live/VOD identity, HLS playback source, WebVTT subtitle tracks,
accessibility semantics, explicit platform capability declarations, server-issued
playback-session authority, and bounded client playback beacons.

Every contract carries the exact supported contract version. Catalogs require a
positive revision, bounded unique content and tagged source identities, one default
subtitle, a closed-caption track, caption availability, a screen-reader summary, and
remote navigation support. Public playback/subtitle URLs are bounded HTTPS URLs with
no credentials, query, or fragment. A client must declare all common playback,
caption, accessibility, navigation, and beacon capabilities before a server session
may bind it to an exact catalog item/revision.

The playback session is server-issued authority represented by its own record and a
digest. The session response separately returns one random bearer capability; only
its protected copy and digest are durable, and the bearer is never written into a
beacon record. A beacon presents that bearer at the transport boundary and only
references the independently loaded current session after constant-time digest and
exact capability checks. Beacons contain no account, plan, entitlement, grant,
content selection, or server-session-authority field. Mismatched, stale, expired,
replayed-with-different-content, or client-invented session references fail closed.

## Boundaries and non-claims

The pure contract crate contains no Roku, tvOS/iOS, Android, Fire TV, web, API,
storage, session issuer, authentication, entitlement checker, CDN, HLS packager,
playback runtime, beacon collector, subtitle renderer, accessibility implementation,
build pipeline, or installed-machine proof. A later source slice provides a bounded
single-page server-side catalog read, public session issuance, and privacy-minimized
beacon collection through API/storage adapters. A continuation cursor is rejected by
that adapter until multi-page query and storage authority exists. Catalog publication
remains a trusted in-process source seam: there is no production publisher or external
entitlement provider yet. The persistence prototype has fixed session/beacon capacity
and no reclamation policy, so it must not be treated as an indefinite telemetry service.
Nothing here proves a client is authentic, a URL resolves, media plays, captions
render, accessibility works, or a field station collects beacons. Later slices must
establish catalog publication, entitlement/access policy, platform applications, safe
delivery, retention/reclamation, accessibility verification, and platform/field
evidence.
