# Machine-protected encrypted SRT proof — 2026-08-21

## Candidate identity

- Source commit: `ea55d27ff75ea2c2819c9a323190d000701285d2`
- Installer SHA-256: `F625BC35E1C345E5180E59C727DED8E65879799F5FD31AE03B2B3CD16824BA3D`
- Installed `stationd.exe`: `EA99D511113F0A0C1F6F204632EA26CD60DA2AC13B62EFEDD865B3561FFFC53E`
- Installed `channel-worker.exe`: `636D61224AD54CDA624CBE970D329F1E9D52870DFC463D05DF884EAF4CE24937`
- Installed `station-updater.exe`: `B924857A4202FD55A31DB1B5570B4C82F458D7D9C1F24BE3CAD0CF3B2FFFDC83`
- Bundled media runtime: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`

## Automated gates

- The full Rust workspace test suite passed across all targets. Media-engine tests include a real matching AES-256 SRT caller/listener transport and a wrong-passphrase listener that requires sender startup to fail closed.
- Full Clippy passed across the workspace, all targets, and all features with warnings denied.
- API tests prove machine-scope protected storage, write-only create and rotation, refusal to rotate a live output, prevention of silent downgrade by an older client, explicit stopped-output clearing, public presence-only state, and absence of plaintext in the SQLite database, WAL, and shared-memory files.
- Protocol tests prove the passphrase crosses the protected named-pipe bootstrap and its debug representation is redacted. Process-argument proof rejects either the destination URI or passphrase on a service-owned worker command line.
- Support-bundle tests inject a unique encrypted SRT canary and prove the complete archive does not contain it.
- The production operator build accepts optional primary and redundant SRT passphrases, never reads a passphrase back, and displays only encryption presence.

## Installer upgrade correction

The first exact installer attempt exposed a real upgrade defect: setup attempted to reinstall the same complete private GStreamer runtime and the nested runtime installer stranded an error dialog. The outer installer returned exit code 3 and correctly rolled the candidate back. The installer now checks the full required runtime contract, reuses a complete private runtime during upgrade, and replaces only an incomplete runtime. Its static contract expanded from 26 to 28 requirements.

The rebuilt exact installer then completed with exit code 0. The independent installed verifier reported both services running automatically as `LocalSystem`, `GET /health` ready, all executable hashes and the source receipt matching the candidate manifest, and every required media factory loadable from the private runtime.

## Installed encrypted transport gate

`scripts/prove-installed-encrypted-srt-output.ps1` configured a disabled required SRT caller with a write-only passphrase, verified the public API returned only `encryption_configured: true`, stopped the service, and scanned `station.db`, `station.db-wal`, and `station.db-shm` offline for the exact UTF-8 secret. No plaintext match existed. It restarted the service, reached ready health, started an independent AES-256 installed-runtime listener, and enabled the channel without resubmitting the secret. This proves DPAPI recovery under the service identity across restart rather than merely proving same-process memory reuse.

The listener capture contained whole 188-byte MPEG-TS packets with valid sync bytes. Independent FFprobe inspection and measured transport evidence were:

```json
{
  "channel_id": "9f55c6a1-12c7-4a90-9f90-7dbd4b6663f8",
  "encryption": "AES-256",
  "passphrase_publicly_exposed": false,
  "passphrase_plaintext_in_sqlite": false,
  "persisted_across_service_restart": true,
  "worker_arguments_redacted": true,
  "dimensions": "1280x720",
  "frame_rate": "60000/1001",
  "video_codec": "h264",
  "audio_codec": "aac",
  "measured_transport_bitrate_bps": 8444799,
  "captured_bytes": 5503888,
  "service_health": "ready"
}
```

The proof disabled the channel and removed its transient capture in `finally`.

## Honest boundary

This proves one AES-256 authenticated required SRT caller through operator/API contract, machine-protected SQLite storage, real service stop and restart, protected worker bootstrap, installed private runtime, loopback listener, transport integrity, codec/cadence inspection, and process/API/support redaction. The secret necessarily exists transiently in the API, orchestration, worker, GStreamer, and receiver process memory while configuring or operating the link. This does not yet prove remote third-party receiver interoperability, stream IDs, key rotation without stopping output, adverse-network recovery, long-duration operation, or the one-week/three-station field gates.
