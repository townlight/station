# Installed scheduled-recording proof boundary

This document defines the installed-candidate proof for S21. It is a runnable
proof contract, not a claim that the proof has already been run and not field
proof. It must be run against a real installed candidate with an operator-owned
media fixture:

```powershell
$credential = Get-Credential
& .\scripts\prove-installed-scheduled-recording.ps1 `
    -Credential $credential `
    -FixturePath 'C:\path\to\real-proof-fixture.ts'
```

The proof authenticates the installed receipt and exact candidate manifest,
admits the fixture through the API, commits a proof-owned schedule, creates and
arms a scheduled recording, and waits for the running station controller to
capture and finalize real media. It then verifies the completed lifecycle,
durable transition chain, content-addressed asset readiness, and generated Draft
catalog record. A failed or missing transition is a failure; the proof never
fabricates worker evidence or completion facts.

The proof emits its JSON receipt only to standard output. It deliberately has
no output-file parameter and cannot overwrite station data, installed proof
state, or an arbitrary caller-selected path.

Cleanup disables the proof-owned channel. The API has no delete operation for
the durable proof schedule, recording, or catalog history; those records remain
auditable and are bound to the run UUID.

This installed proof does not claim uninterrupted external receiver output,
production readiness, or field proof for the required one-station/three-station
pilot.
It does not claim field proof.
Those remain separate operational gates.
