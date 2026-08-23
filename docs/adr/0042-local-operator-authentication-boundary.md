# ADR 0042: Local operator authentication boundary

## Decision

TownLight Station requires a local authenticated operator before any control-plane API can read station authority or mutate station state. Loopback binding is defense in depth, not identity. Static console assets, `/health`, authentication status, bootstrap, login, and recovery are the only public routes.

The first vertical slice creates one enabled `setup_admin` account and leaves the five-role schema extensible. Account identity, password authority, sessions, recovery codes, bootstrap capability, throttle state, and append-only authentication audit are durable SQLite records owned by `stationd`. Existing station, channel, schedule, and playout data migrate additively; migration never creates a default account or password.

Passwords use Argon2id with a random 16-byte salt, 64 MiB memory, three iterations, one lane, and a 32-byte output. Passwords are 12–128 UTF-8 bytes without forced composition rules. Unknown usernames execute the same bounded Argon2 path as a wrong password. Expensive verification is concurrency-bounded and repeated failures produce a durable exponential delay.

Session and CSRF tokens are independent random 256-bit values. SQLite stores only SHA-256 digests. A session is bound to its account credential version and the current service boot, expires after 30 minutes idle or 12 hours absolute, and is invalidated by service restart, password change, or recovery. The session cookie is `HttpOnly`, `SameSite=Strict`, has no `Domain`, and is limited to `/api/v1/`; the separate CSRF cookie is readable by the console and bound to the stored session. Unsafe requests require an exact same-origin `Origin`, the session cookie, the CSRF cookie, and the matching `X-TownLight-CSRF` header.

HTTP parsing preserves bounded headers and rejects duplicate Host, Origin, Cookie, Content-Length, and CSRF headers, absolute-form request targets, invalid control bytes, and response-header injection. The production dispatcher validates the actual loopback Host/origin before routing. Browser-supplied operator identifiers are not authority: schedule approvals, live operations, alert actions, maintenance requests, and CG audit identities are derived from the authenticated session.

## First-run bootstrap

The installer creates a one-time, 15-minute, 256-bit bootstrap capability before starting the service. Only its digest is stored. The raw capability crosses the elevated installer boundary in a create-new, ACL-restricted handoff file; it is never placed in process arguments or logs. The installer deletes the handoff file and opens `http://127.0.0.1:4070/#/setup/<capability>` after health is ready.

The console captures the fragment once, immediately removes the capability from the address with `history.replaceState`, and retains it only in component memory. Bootstrap runs under one immediate SQLite transaction that rechecks zero accounts, consumes the capability, creates the account and `setup_admin` role, stores eight one-use recovery-code digests, records audit, and creates the first session. Concurrent or repeated claims cannot create a second administrator.

Authentication therefore precedes the first persistent station-profile mutation. Station identity and operator identity remain separate authorities even when the first-run console presents them as one guided journey.

## Recovery and field behavior

Normal recovery consumes one high-entropy recovery code, rotates the credential version, and invalidates all sessions. No plaintext recovery code is stored. An administrator-only offline reset command may issue a short-lived recovery capability while the service is stopped; it never installs a default password.

Playout, scheduling execution, alerts, and recovery continue while no browser session exists. A service restart deliberately requires browser re-login but does not disable channels. Existing installed stations receive `operator_bootstrap_required` for protected APIs until an account is created. Installed proof tools must authenticate, preserve cookies and CSRF state, and re-login after service restart.

`operator_account_auth` passes only when an enabled account has a supported password hash and `setup_admin` role, the bootstrap was consumed, and the commissioning report itself was reached through a valid authenticated session. Additional accounts, role-management UI, passkeys, remote administration, and automation credentials are deferred without weakening this boundary.

## Required proof

- populated-database migration preserves all existing authority and creates no default account;
- two racing bootstrap claims yield exactly one administrator;
- wrong, expired, reused, cross-origin, duplicate-header, missing-CSRF, expired-session, old-boot, and old-credential requests fail before side effects;
- password, token, capability, and recovery plaintext never reaches SQLite, logs, receipts, responses other than one-time issuance, or support bundles;
- every non-public API route is centrally guarded and body-supplied actor identity cannot change durable attribution;
- recovery consumes one code and invalidates all sessions;
- installed first run creates the account, authenticates a station write, survives re-login after service restart, and reports `operator_account_auth=pass`;
- uninstall/reinstall preserves the account and the added setup time remains inside the measured first-broadcast commissioning target.
