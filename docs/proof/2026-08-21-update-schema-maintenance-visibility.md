# Installed update-schema and maintenance-visibility proof — 2026-08-21

## Candidate under proof

- Source commit: `eadb2c083cdf4148647d323e7bcde74f4fd9ec1d`
- Installer SHA-256: `58B0FA4E861C038E85B1AF7A9058B5152F7AE803A148884C936D6012CF6FBCC1`
- Installed `stationd.exe` SHA-256: `099F2B0276292C5C8C9EFA843658D21FD50076C427FA58B77CA186D839777666`
- Installed `channel-worker.exe` SHA-256: `BE8C4368BD26C86ADE793698D9D6B52277C8832D8157DCA28F7EB825102253D0`
- Installed root watchdog SHA-256: `30CAEB57B6B9B632484C6E624E63A5B2D90C5BE3650BC1B9E2DFB9929D188C34`
- Private media-runtime installer SHA-256: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`

The clean candidate installer completed successfully. `TownLightStation` and
`TownLightStationUpdater` were both automatic, running LocalSystem services. Loopback health was
ready and every required GStreamer factory remained available.

## Machine-discovered schema defect and correction

The first installed build correctly narrowed application slots to `stationd.exe` and
`channel-worker.exe`, leaving the independent updater watchdog owned by the full installer. That
build then failed `/api/v1/updates/status` while reading the prior three-payload maintenance ledger.
Changing the payload set without changing the package schema had made durable version 1 history
unreadable.

The corrected contract is explicit:

- schema 2 is the only package schema that can be newly prepared and activated;
- schema 2 contains exactly the station service and channel worker;
- the updater remains at the stable application root and changes only through a verified installer;
- schema 1 three-payload manifests remain readable for historical active receipts, rollback, and
  audit, but are rejected for new preparation or activation.

The reinstalled bootstrap receipt reported schema 2 and exactly the two slot-owned payloads. The
live status API simultaneously read the historical schema 1 rollback transaction and reported:

- phase `rolled_back`;
- previous version `0.2.0`;
- rejected candidate version `0.3.0`;
- transaction `update-46ec3147bb9a25ab94703d8f41f0172bd19e9ae9-1787315946713`.

## Installed support-bundle proof

The installed service created, served, and retained support bundles through its loopback API. The
proof verified:

- archive SHA-256 exactly matched the service record and downloaded bytes;
- 13 exact flat ZIP entries and 12 exact per-entry manifest hashes;
- no station database or sidecar;
- no plaintext credential canary or full destination canary;
- active application directory `.` and generation `1`;
- station and worker hashes from the active application;
- updater hash from the actual installer-owned root watchdog;
- latest maintenance phase `rolled_back` through a bounded phase summary;
- raw transaction error detail absent from `maintenance-latest.json`;
- runtime safe-to-air green;
- exactly ten retained ZIPs, zero temporary fragments, oldest removed, newest retained.

This closes the installed-evidence gap for maintenance history and makes updater ownership truthful
across package validation, active receipts, status, operator UI, and support diagnostics.
