# Installer in-place upgrade rejection contract — 2026-08-24

## Boundary

This is source-level hostile-contract evidence for the current uncommitted installer slice. It is
not a compiled installer, installed-machine, clean-machine, field, or release proof.

The full setup executable is intentionally not an in-place upgrade mechanism. The installed signed
maintenance updater owns `.tlu` activation and rollback. This setup path is restricted to clean
install and reinstall after uninstall.

## Contract

`PrepareToInstall` runs before Inno Setup's `ssInstall` phase. It rejects setup when any of these
product-presence signals is true:

- `%ProgramFiles%\TownLight Station` exists;
- `TownLightStation` exists; or
- `TownLightStationUpdater` exists; or
- `HKLM64\Software\Microsoft\Windows\CurrentVersion\Uninstall\{D70BC1A2-A047-4F3A-87D9-811385248D2C}_is1` exists.

The preflight does not inspect `%ProgramData%\TownLight Station`, its database, installation
receipt, active-slot receipt, or runtime manifest. Those artifacts survive an intentional uninstall
and therefore cannot distinguish a permitted reinstall from an unsafe in-place replacement.

As a result, an existing installation reaches no destructive installer action: it has no service
stop/delete, application-binary copy, private-runtime removal or installation, receipt/manifest
write, or candidate service registration from this setup executable. The `ssInstall` activation
boundary itself contains no service deletion; candidate rollback tracks ownership only after each
candidate service is successfully created, so a raced preflight cannot turn cleanup into deletion of
an unrelated existing service. The setup next claims the empty application directory with `CreateDir`;
it never creates that directory recursively, stages each binary with `FailIfExists`, and rejects a
runtime root that appears before runtime installation. Application rollback is limited to that
candidate-owned directory. SCM registration treats `ERROR_SERVICE_EXISTS` (1073) as a raced
pre-existing service, while another failed create that leaves a previously absent service present is
marked candidate-owned before rollback.

## Hostile static verification

The following command completed successfully:

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\installer\verify-contract.ps1
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\installer\test-bootstrap-contract.ps1
```

The verifier reported `Installer contract passed (75 requirements).` The falsification harness
reported `Installer bootstrap falsification passed (5 malformed handoffs and 18 hostile source
mutations rejected).` The added hostile mutations separately bypassed the preflight, application
directory detection, station-service detection, updater-service detection, candidate-service
ownership record, AppId uninstall-registration detection, atomic directory claim, no-overwrite
staging, pre-existing-runtime rejection, `ERROR_SERVICE_EXISTS` handling, and the no-delete
`ssInstall` boundary; each was rejected by the verifier.

Inno Setup 6.7.3 compiled the script with the current local release binaries and pinned runtime into
a temporary 535,213,053-byte setup executable. This is a syntax/package compilation check only; an
elevated installed-machine check must still confirm that setup refusal preserves a running prior
station byte-for-byte and leaves both service configurations unchanged.
