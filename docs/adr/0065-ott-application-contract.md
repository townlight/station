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
but no permanent lifetime cap: playback sessions and their beacon/state rows are
TTL-scoped ephemeral authority, not an indefinite audit log. Each session request or
beacon request first commits a separate bounded maintenance transaction that deletes
at most 4,096 beacon rows and then at most 64 now-childless
sessions whose 15-minute session expiry is at least 24 hours old, together with only
those sessions' state rows. Admission then runs in its own transaction and counts the
post-maintenance retained rows. A rejected capacity, stale-session, replay-conflict,
or invalid-sequence outcome therefore cannot roll back already completed maintenance;
an interrupted maintenance transaction itself rolls back all of its partial deletions.
Live and within-retention request identities
are never reclaimed; request replay remains exact until reclamation, after which the
same request identity may establish a new session.

Anonymous native clients currently reach an API transport that does not expose a
trusted peer identity to the route handler. The station therefore uses durable global
one-minute admission windows rather than pretending a browser client ID is an
identity: at most 5 new sessions and 48 new beacons are admitted per window, and
each session's non-replayed beacons are paced at least one second apart and retain the
10,000-event lifetime bound. Replays and requests rejected before admission do not
consume a window. A full window or retained-row cap returns a typed 429 with a bounded
`Retry-After`; the windows survive process restart and reset at their exact server-time
boundary. Every OTT `Retry-After` is capped at 87,300 seconds, the 15-minute
session lifetime plus the 24-hour post-expiry retention horizon. A retained capacity
row whose expiry lies beyond the server-owned session lifetime is corrupt authority,
not a client retry schedule, and fails closed without a `Retry-After` header. This is
conservative station-wide protection, not per-device fairness or
cryptographic client authentication. Even if every window is filled throughout the
maximum 1,455-minute session-plus-retention lifetime, boundary alignment spans at
most 1,456 windows: 7,280 sessions and 69,888 beacons, leaving headroom under the
10,000/100,000 caps.
The two admission rows are durable; reclaimed
session/beacon rows are intentionally deletable and must not be used as compliance or
as-run evidence.

Nothing here proves a client is authentic, a URL resolves, media plays, captions
render, accessibility works, or a field station collects beacons. Later slices must
establish catalog publication, entitlement/access policy, platform applications, safe
delivery, operational privacy-policy approval, trusted transport peer authority where
needed, accessibility verification, and platform/field
evidence.
