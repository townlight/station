# Installed required-output stall and recovery proof

Date: 2026-08-21

## Exact candidate

- Source commit: `4a047e4501f5a45e1961fad4fd79448caf50d2d7`
- Installer SHA-256: `46E882E9E5257F8AE998A9C4CD24A155A8C57C088938F31A198741DACEA09662`
- `stationd.exe`: `C87A655459D2302C72240D330FF2867F083BB49FC624C8945A4F2D405E8B28FA`
- `channel-worker.exe`: `ED82693B989D72BDD8F8B2F6A0D20D65E0E8C4B073420DDCECCEBC7CB9E2B7F5`
- `station-updater.exe`: `E63F1A3181E21B18E12FCC72818D5F1207CA40C08CFC2EA0B969958AADAA886F`
- Pinned media runtime: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`

The 28-requirement installer contract passed. The exact installer exited zero while upgrading the preserved station. The independent installed verifier matched the source receipt and every executable hash, reported both automatic services running as `LocalSystem`, received ready database/HTTP health, and loaded every required factory from the private media runtime.

## Product contract under proof

Protocol 1.4 carries an ordered transmitted-byte observation for every enabled sink. Every five seconds the service must complete a real protected named-pipe round trip to the worker. A required output must report nonzero bytes on its first heartbeat and strict progress thereafter. Pipeline errors or a stalled required counter fail red, fire `output-stall` plus `off-air`, stop and reap the worker, and enter the bounded 1/5/15/60-second restart path. Process/control failures retain the separate `encoder-death` classification.

The complete serial Rust workspace suite passed with zero failures or ignores, including nine real GStreamer media tests. Warnings-denied Clippy, formatting, diff validation, and the zero-old-product-name scan also passed.

## Installed failure and recovery

`scripts/prove-installed-required-output-stall.ps1` created a unique required SRT caller output against the installed private runtime and waited through multiple real progress heartbeats. It then forcibly removed the receiver while leaving the channel enabled.

Observed installed results:

- initial worker PID `19368` delivered `11,328,504` bytes;
- receiver loss was detected in `13.801` seconds and the channel failed red;
- firing critical `output-stall` and `off-air` incidents existed for the exact channel;
- no firing `encoder-death` incident existed for the transport failure;
- the original worker PID was reaped;
- restoring the receiver produced replacement worker PID `19152`;
- recovery completed in `12.691` seconds and delivered `11,264,020` bytes;
- the replacement survived another eleven seconds of real progress-heartbeat polling;
- both incidents resolved and service health remained ready.

The first proof-instrument draft tried to attribute a LocalSystem worker through its command line. Windows returned blank command lines to that non-service proof process even though the durable journal showed successful ready and advancing heartbeat events. The corrected instrument identifies the unique proof worker by the before/after worker PID set, and the full adversarial run above passed. This was an evidence-instrument correction, not a product change.

Cleanup disabled the proof channel and stopped its listener. Final inspection found the station green, its remaining enabled channel on healthy fallback with a fresh heartbeat, zero firing critical or warning incidents, and zero firing incidents for the proof channel.

## Boundary

This proves installed local output-progress detection, red failure truth, incident classification, worker reaping, bounded restart, and green recovery on this Windows station. It does not prove UDP receiver acknowledgment, remote headend telemetry, bounded worker-journal retention, the seven-day one-station field gate, the subsequent three-station gate, or completion of the remaining S1-S26 capability set.
