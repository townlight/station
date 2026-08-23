# Installed commissioning gate — 2026-08-21

`scripts/prove-installed-commissioning.ps1` is the repeatable installed-machine gate for the TownLight Station beta spine. It is an evidence harness, not an installer or a database repair tool. It talks to the installed loopback API and reads the installed service/receipt state; it does not create, edit, or delete files in `%ProgramData%\TownLight Station`.

## What the gate proves when it passes

The script proves one exact installed candidate, on one Windows machine, in this order:

1. `TownLightStation` and `TownLightStationUpdater` are running as `LocalSystem`, the immutable install receipt is present, and the installed executable hashes match `dist/candidate-manifest.json` (or the manifest supplied with `-CandidateManifestPath`). An optional `-ExpectedSourceCommit` adds an explicit source-identity assertion.
2. `/health` reports `{"database":"ready","status":"ready"}` and `/api/v1/station` returns a persisted station profile.
3. The existing `scripts/prove-installed-cg-images.ps1` is composed by default. It uploads two server-identified images, binds them to a board/zone, captures MPEG-TS, decodes the pixels with FFmpeg, and validates the board-inactive clear path. Use `-SkipCgProof` only when the installed candidate cannot yet run that proof; the result records the skip.
4. A fresh UUID proof channel reaches green safe-to-air runtime.
5. A caller-supplied real media fixture is admitted only through `POST /api/v1/assets/ingest`; the durable job reaches `completed`, the canonical asset reaches `ready`, and the readiness dashboard is captured.
6. A fresh draft schedule item passes `/schedule/prepare`, is committed through `/schedule/commit`, and its durable report reaches `pending -> queued -> acknowledged -> completed`.
7. Unless `-NoServiceRestart` is supplied, the installed station service restarts. Health, the persisted station identity, the proof channel's safe-to-air state, and the completed commit report are checked again.
8. A JSON evidence file may be written with `-ResultPath`. It includes the observed receipt identity, service identities, API evidence, IDs, timestamps, and the exact failure message if the gate fails.

## Invocation

The fixture must already be a non-empty, real media file suitable for the installed media pipeline. The harness never copies or edits it. Supply an existing local operator account through an in-memory `PSCredential`; the harness does not accept a plaintext password argument, environment variable, or credential file. `Get-Credential` keeps the password out of the command line, transcript text, and JSON evidence.

```powershell
$proofCredential = Get-Credential -Message 'TownLight Station installed-proof operator'
if ($null -eq $proofCredential) { throw 'An operator credential is required.' }

.\scripts\prove-installed-commissioning.ps1 `
  -Credential $proofCredential `
  -FixturePath 'C:\proof-fixtures\real-h264-aac.ts' `
  -ExpectedSourceCommit '<installed-candidate-commit>' `
  -CandidateManifestPath '.\dist\candidate-manifest.json' `
  -ResultPath "$env:TEMP\townlight-commissioning-$PID.json"
```

For a contract-only run on a candidate that does not yet contain the CG image proof, use `-SkipCgProof`; this is recorded as skipped and is not a pass for the full CG gate. `-NoServiceRestart` is similarly a diagnostic exception, not a full commissioning pass.

The proof channel and schedule IDs are new UUIDs per run, so sequential and concurrent runs do not overwrite an operator's channel or schedule. A named Windows mutex prevents concurrent use of the existing fixed-identity CG proof. The API currently has no channel or schedule delete operation, so completed runs leave a disabled, uniquely named proof channel and an auditable schedule row; this is intentional and documented rather than hidden filesystem cleanup.

## What this does not claim

This is not the required field acceptance: one live station for a week followed by three stations for another week. It does not prove every S6/S18 source variant, a seven-day soak, installer creation or first-account bootstrap, recovery, restart-driven session invalidation, or pixel parity for non-image CG content. It also cannot prove a candidate that is not actually installed; static parsing of the script and a successful authenticated local API call are not substitutes for those gates.

The harness itself was validated end to end against the already-installed candidate `c2cf23d7302695214ed27711b50e61e2eb2ae31f`. Run `3b00c091-41de-4bb5-9f54-0a1ce735aa48` passed exact manifest/hash checks, both LocalSystem services, decoded CG red-to-green-to-clear, real canonical ingest and -23.973 LUFS readiness, observed `pending → queued → acknowledged → completed`, station-service restart, preserved station identity, a persisted completed report, and green safe-to-air recovery. Its JSON evidence is retained outside the repository at `C:\Users\scott\Documents\Codex\commissioning-proof-c2cf23d-elevated.json`.

That run validates the harness and the historical installed candidate. It does not prove source changes made after `c2cf23d`; each new candidate must be built, installed, and passed independently before it is described as installed-machine-proven.

The commissioning implementation candidate `8e9a27d3286815e0b34a0fad961cb2e5dc9a5a84` was then built and installed from installer SHA-256 `7A8EC0074368CEC4BAD0944437E6939387964551762420AE138DA3B895470C68`. The independent post-install verifier matched the installed station, worker, and updater hashes, both services running as `LocalSystem`, ready health, and the private media factories. The preserved station profile and data reopened successfully.

Installed run `983a784f-4b29-4c52-bfc1-eae2e48b9754` passed the complete harness against that exact candidate. It recorded decoded CG pixels changing from red `[217,19,18]` to green `[17,218,19]` and clearing to baseline; canonical media identity `7c47513c3870a6c8d51556e3843139bfe4c16ee7d57b56471ad8d37b87d975f8` at measured `-23.973` LUFS; observed dispatch states `pending → queued → acknowledged → completed`; and, after restarting `TownLightStation`, ready health, green safe-to-air, preserved station identity, and the completed commit report. The complete JSON evidence is retained at `C:\Users\scott\Documents\Codex\commissioning-proof-8e9a27d.json`.

This advances the exact candidate through installed-machine commissioning on this machine. It is not a clean-machine image proof, an external headend/receiver acceptance, or either required field week.
