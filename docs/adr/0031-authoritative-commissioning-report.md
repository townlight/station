# ADR 0031: Authoritative commissioning report

## Decision

Commissioning progress is derived from current station authority, not from browser checkboxes or a mutable completion flag. `GET /api/v1/commissioning/report` returns an ordered set of typed checks with status, blocking behavior, operator detail, and the evidence source used for each conclusion.

The report keeps these facts separate:

- station identity exists;
- the API and SQLite authority are serving the request;
- enabled channels have fresh heartbeat-derived safe-to-air state, including active alerts;
- at least one ready managed media record still has present bytes;
- committed schedule authority exists;
- worker-acknowledged or completed dispatch execution exists;
- clean-machine, receiver, and field proof remain externally evidenced and `not_verified` by this control-plane report.

The operator console consumes this report, adds CG-board state from its own authoritative API, and guides the operator to the surface that can resolve each incomplete core check. Browser-local state remembers only navigation position; it cannot make a check pass.

## Consequences

A committed schedule row cannot be presented as proof that a worker aired it. A stale healthy runtime observation cannot pass commissioning because the normal safe-to-air derivation enforces heartbeat freshness and alert truth. The console may say core commissioning is complete while still showing installed/field proof as not verified; it may never turn the one-station or three-station field gates green on its own.

Installed evidence is produced separately by `scripts/prove-installed-commissioning.ps1`. That harness validates exact installed hashes against the candidate manifest, operates only through public loopback APIs, captures actual dispatch states, composes decoded CG apply/change/clear evidence, restarts the Windows service, and verifies recovery. Passing this harness proves one candidate on one machine, not the required week-long field gates.
