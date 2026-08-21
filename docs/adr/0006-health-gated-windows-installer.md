# ADR 0006: Health-gated Windows installer

Date: 2026-08-20

Status: Accepted

## Context

A double-click installer is not successful merely because it copied files. A station operator needs one candidate that carries its media dependencies, starts without an interactive login, proves local readiness, can be removed, and never destroys commissioned station data. Prior packaging failures are contained only if activation failure occurs before setup reports success.

## Decision

TownLight Station builds one elevated x64 setup executable with Inno Setup.

- The candidate embeds optimized `stationd` and `channel-worker` binaries, notices, the full official GStreamer 1.28.6 MSVC x64 runtime installer, and its pinned SHA-256 digest.
- Before Inno Setup commits product registration, code extracts the candidate binaries, installs GStreamer to a private application prefix, registers `TownLightStation` as a delayed-auto LocalSystem service, sets bounded restart recovery, and starts it.
- The activation transaction succeeds only after `GET /health` returns HTTP 200 with database readiness. It then writes `%ProgramData%\TownLight Station\install-receipt.json` containing product version, immutable source commit, media-runtime digest, time, and health URI.
- Failure during staging, runtime installation, service registration, startup, health, or receipt writing removes the staged service, private runtime, and binaries and returns a nonzero setup result.
- The service receives explicit private GStreamer `PATH` and plugin paths rather than relying on machine-wide developer configuration.
- Uninstall stops and deletes the service and removes application/runtime files. It deliberately does not delete the station database or receipts in ProgramData.
- Release packaging refuses a dirty worktree. The installer and binary hashes are emitted to `candidate-manifest.json` and can be checked by `verify-installed.ps1`.

## Evidence

Three adversarial machine paths were executed on Windows 11:

1. A malformed service-registration command caused activation to fail. Setup returned nonzero and removed the staged runtime, service, and binaries rather than leaving a registered product.
2. A clean candidate installed successfully, reached delayed-auto `RUNNING` under LocalSystem, exposed all five media factories used by the current graph, matched installed binary hashes to the candidate manifest, answered health with HTTP 200, and wrote its database and receipt.
3. The installed API commissioned a station at revision 1. Uninstall removed Program Files and the service while retaining ProgramData. Reinstall started successfully and returned the same station identity, name, timezone, and revision.

## Consequences

The repository now has an executable installer boundary and a repeatable post-install verifier. The current runtime payload is large because it carries the complete official runtime; dependency-minimized packaging remains an optimization only after factory and license closure can be proved. Development candidates are unsigned. Authenticode signing, trusted timestamping, clean-VM validation, upgrade rollback between immutable application slots, and operator-facing setup remain release gates.
