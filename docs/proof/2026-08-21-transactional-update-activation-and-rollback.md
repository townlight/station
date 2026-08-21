# Transactional update activation and rollback proof — 2026-08-21

## Exact installed candidate

- Source commit: `46ec3147bb9a25ab94703d8f41f0172bd19e9ae9`
- Installer SHA-256: `9EA915991FBEF3D5608A561670CF19433C92E2AABF2DA271D4BE78A6C2DB1845`
- Installed and candidate `stationd.exe` SHA-256: `2E4CD06D562AD72B22F4316ADC94644135DB65F756A3D94ADEA9713A825E1E0C`
- Installed `channel-worker.exe` SHA-256: `DE265AF7CBB60F75FE96FCA0040D98586EA29C93ED6407C8173B4FDCCA6F7688`
- Installed `station-updater.exe` SHA-256: `710453402453E490AE778C2021DF859E243B27C7FCEDD48B46EAD882423887D2`
- Private media-runtime installer SHA-256: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`

The clean installer passed its 24-point contract and installed both `TownLightStation` and
`TownLightStationUpdater` as running, automatic `LocalSystem` services. The installed verifier
authenticated all three binaries, the bootstrap active-slot receipt, loopback health, and every
required private GStreamer factory.

## Real signed activation

A fresh deterministic Ed25519 proof authority signed a one-hour 0.2.0 release manifest. Its 9,373,698-byte
`.tlu` contained the exact release `stationd`, worker, and updater binaries. The station observed exactly
two loopback release-server requests: one manifest and one package. Discovery, staging, preparation, and
the explicit `INSTALL UPDATE` maintenance action then ran through installed production boundaries.

Transaction `update-46ec3147bb9a25ab94703d8f41f0172bd19e9ae9-1787315928747` committed. The independent watchdog
survived the primary-service interruption in the same process, changed SCM to
`slots/slot-0.2.0-46ec3147bb9a25ab94703d8f41f0172bd19e9ae9/stationd.exe`, proved the exact running process image,
required ready health and green safe-to-air, preserved the private-runtime identity, archived the request,
and advanced the durable active/install receipts from 0.1.0 to 0.2.0. A later ordinary restart remained on
the committed slot and retained green safe-to-air.

## Deliberate candidate failure and automatic rollback

A separate correctly signed 0.3.0 package contained valid, hash-bound worker and updater payloads but a
deliberately non-executable station payload. It passed signed discovery and immutable preparation, then
failed at the real Windows service-start boundary.

Transaction `update-46ec3147bb9a25ab94703d8f41f0172bd19e9ae9-1787315946713` reached `rolled_back`. The watchdog:

- recorded `Candidate failed and the prior application was restored: could not start station service: IO error in winapi call`;
- restored the exact 0.2.0 SCM target and running process image;
- reverified SHA-256 `2E4CD06D562AD72B22F4316ADC94644135DB65F756A3D94ADEA9713A825E1E0C`;
- required green safe-to-air before completing rollback;
- preserved the active-slot receipt byte-for-byte and the install identity exactly;
- archived the failed maintenance request; and
- remained alive in the same watchdog process throughout the failure and recovery.

The restored 0.2.0 application stayed green and survived another station-service restart.

## Defect found by the machine proof

The first rollback attempt found that the background update monitor used the shared application root while
HTTP request handlers independently derived the current slot directory. After a slot restart, API status and
actions therefore could not see root-owned release trust. No 0.3 maintenance request was accepted and the
station remained healthy on 0.2.0. Commit `46ec3147bb9a25ab94703d8f41f0172bd19e9ae9` removed that split by
passing one explicit shared application root into every API request handler. The installer, successful
activation, failed-candidate rollback, and post-rollback restart were all rerun from that exact fixed commit.
