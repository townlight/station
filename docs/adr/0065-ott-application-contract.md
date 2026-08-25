# ADR 0065: OTT application contract

## Decision

`station-ott-contract` owns a pure, versioned public interchange contract for Roku,
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
digest; a beacon only references that supplied current session. Beacons contain no
account, plan, entitlement, grant, content selection, or server-session-authority
field. Mismatched, stale, expired, or client-invented session references fail closed.

## Boundaries and non-claims

This crate contains no Roku, tvOS/iOS, Android, Fire TV, web, API, storage, session
issuer, authentication, entitlement checker, CDN, HLS packager, playback runtime,
beacon collector, subtitle renderer, accessibility implementation, build pipeline,
or installed-machine proof. It does not prove that a client is authentic, a session
was actually issued, a URL resolves, media plays, captions render, accessibility works,
or a beacon is collected. Later slices must establish server-side issuance/access
authority, platform applications, safe public delivery, telemetry collection,
accessibility verification, and platform build/field evidence.
