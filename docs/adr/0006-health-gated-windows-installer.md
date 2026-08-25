# ADR 0006: Health-gated Windows installer

Date: 2026-08-20

Status: Accepted

## Context

A double-click installer is not successful merely because it copied files. A station operator needs one candidate that carries its media dependencies, starts without an interactive login, proves local readiness, can be removed, and never destroys commissioned station data. Prior packaging failures are contained only if activation failure occurs before setup reports success.

## Decision

TownLight Station builds one elevated x64 setup executable with Inno Setup.

- The candidate embeds optimized `stationd` and `channel-worker` binaries, notices, the full official GStreamer 1.28.6 MSVC x64 runtime installer, and its pinned SHA-256 digest.
- Before Inno Setup commits product registration, code extracts the candidate binaries, installs GStreamer to a private application prefix, registers `TownLightStation` as an immediate automatic LocalSystem service, sets bounded restart recovery, and starts it. Channel recovery therefore begins during boot rather than after Windows' delayed-start window.
- Before `ssInstall`, the setup executable detects an existing TownLight application directory, either TownLight service, or the exact 64-bit Inno Setup uninstall registration for this AppId. If any is present, setup refuses before it stages files, stops a service, installs or removes the private runtime, or changes receipts/manifests. This is deliberately a clean-install/reinstall-only boundary: a signed `.tlu` update through the installed maintenance updater is the only in-place upgrade path.
- After that preflight, setup atomically claims `{app}` with `CreateDir`; a concurrent directory appearance is a failure, not an upgrade. Candidate binaries are copied with `FailIfExists`, and a pre-existing private runtime root is rejected rather than removed. Rollback removes `{app}` only when this setup claimed it.
- The preflight deliberately ignores `%ProgramData%\TownLight Station`, including its database, receipts, and runtime manifest, because a proper uninstall retains that state and a subsequent reinstall must be allowed. In contrast, proper Inno uninstall removes the exact AppId registration, allowing that reinstall.
- The pinned private media runtime is installed only after clean-install preflight accepts the candidate. Before either service starts, the candidate `stationd` enumerates and hashes the exact bounded runtime file tree into `%ProgramData%\TownLight Station\runtime-manifest.json`; the manifest carries candidate and runtime-package identity, and its SHA-256 is bound into the installation receipt. A merely presence-complete prior tree is never blessed as current.
- The activation transaction allows 75 seconds and succeeds only after `GET /health` returns HTTP 200 with database readiness and current-boot orchestration readiness. It then writes `%ProgramData%\TownLight Station\install-receipt.json` containing product version, immutable source commit, media-runtime package digest, installed-runtime manifest digest, time, and health URI. Post-install verification parses those fields structurally and does not depend on JSON property order.
- Failure during a clean installation's staging, runtime installation, service registration, startup, health, or receipt writing removes the staged service, private runtime, and binaries and returns a nonzero setup result.
- The service receives explicit private GStreamer `PATH` and plugin paths rather than relying on machine-wide developer configuration.
- Uninstall stops and deletes the service and removes application/runtime files. It deliberately does not delete the station database or receipts in ProgramData.
- Release packaging refuses a dirty worktree. The installer and binary hashes are emitted to `candidate-manifest.json`. `verify-installed.ps1` also verifies the receipt-bound runtime-manifest digest, exact file set, sizes, hashes, and absence of reparse points before probing factories.

## Evidence

Three adversarial machine paths were executed on Windows 11:

1. A malformed service-registration command caused activation to fail. Setup returned nonzero and removed the staged runtime, service, and binaries rather than leaving a registered product.
2. A clean candidate installed successfully, reached `RUNNING` under LocalSystem, exposed all required media factories used by the current graph, matched installed binary hashes to the candidate manifest, answered health with HTTP 200, and wrote its database and receipt.
3. The installed API commissioned a station at revision 1. Uninstall removed Program Files and the service while retaining ProgramData. Reinstall started successfully and returned the same station identity, name, timezone, and revision.

## Consequences

The repository now has an executable clean-install boundary and a repeatable post-install verifier. The current runtime payload is large because it carries the complete official runtime; dependency-minimized packaging remains an optimization only after factory and license closure can be proved. Development candidates are unsigned. Authenticode signing, trusted timestamping, clean-VM validation, signed updater activation/rollback between immutable application slots, and operator-facing setup remain release gates. The full installer intentionally has no in-place rollback path because it must never mutate an extant installation.
