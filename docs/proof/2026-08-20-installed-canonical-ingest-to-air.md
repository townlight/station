# Installed canonical ingest-to-air proof — 2026-08-20

## Exact candidate

- Source commit: `75d2590101bd6fa074e62d866e73834ab74adf4e`
- Installer SHA-256: `813687AE4E8D74A2FA5E412E9BD3EFCB6B7F1D463621E384B4F5A6F5A06CBDDA`
- Installed `stationd.exe` SHA-256: `AECCEB7E8EFE855882A0532AB0C00034E70599B6FE38754F458CAE1340CE97BB`
- Installed `channel-worker.exe` SHA-256: `61E6812735BCDADA8455CF679B262A42EC20CD2B9710C8EB7A0A26F7252903D5`
- Private media-runtime SHA-256: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`

The installer contract passed all 19 requirements. The setup executable exited zero. The installed verifier confirmed the exact hashes above, the immutable install receipt, all required media factories, a running automatic `LocalSystem` service, and a ready loopback health endpoint.

## Durable two-stage media lifecycle

The installed private runtime generated a fresh finite MPEG-TS fixture containing real H.264 video and AAC audio. The fixture was admitted only through `POST /api/v1/assets/ingest` as job `75d25901-01bd-4fa0-b4e6-2d866e73834a`.

The installed service copied and revalidated the source into the station-owned source store, then atomically moved the parent ingest to `awaiting_transcode` while creating transcode job `7cf3bb09-14f7-504f-9db8-177c6184aedc`. The transcode job used format `h264_720p4mbps_aac192k_ts` and completed in one attempt. Its source and output identities were independently verified from disk:

- Submitted input SHA-256: `18AE0A7F6F15246C3FDB3585938CDB88272EDA34B6A91A2F4E136280384256BF`
- Managed source-copy SHA-256: `18AE0A7F6F15246C3FDB3585938CDB88272EDA34B6A91A2F4E136280384256BF`
- Canonical output SHA-256: `7C47513C3870A6C8D51556E3843139BFE4C16EE7D57B56471AD8D37B87D975F8`

The different output identity proves that the scheduled asset represents the derivative bytes rather than reusing the source identity. No source object was published as a media asset. Only the canonical output became Ready, under `%ProgramData%\TownLight Station\media`.

The canonical output was 3.012 seconds long and passed the installed post-transcode loudness measurement at `-23.973 LUFS` against the `-24.0 LUFS` target. The parent ingest and transcode both finished at 100 percent, with the parent asset identity set to the canonical output identity. The readiness dashboard then showed zero awaiting, pending, running, or failed transcode work.

## Installed schedule-to-air execution

Using only the installed loopback API, the proof enabled channel `75d25901-0200-4fa0-b4e6-2d866e73834a` at `127.0.0.1:5702`, created a two-second draft schedule item against the canonical output, ran the commit gate, and approved it. The gate reported zero missing-media findings and zero conflicts.

The installed worker then advanced the durable report through:

```text
pending -> queued -> acknowledged -> completed
```

The channel was disabled after completion and the service remained healthy. The CRC-protected worker journal is `%ProgramData%\TownLight Station\channel-journals\75d25901-0200-4fa0-b4e6-2d866e73834a.tlj`, SHA-256 `FD2A29E4C1E132A3AC2A4C00099BB96B8E1BA1DA752400F6553F17721C0220E0`.

## Test evidence

Before implementation, the focused storage contract failed because the transcode persistence methods did not exist, the lifecycle contract failed because `AwaitingTranscode` did not exist, the atomic handoff contract failed because its transaction methods did not exist, the real-media pipeline contract failed because the worker types did not exist, and the operator API contract returned 400 for the transcode-job route.

After implementation:

- The full workspace completed 60 tests with zero failures and zero ignored tests.
- Workspace Clippy passed for all targets with warnings denied.
- Formatting and `git diff --check` passed.
- Real-media tests proved source validation, distinct canonical bytes, H.264/AAC transport output, normalized loudness, retry behavior, watch-folder behavior, and restart recovery.
- The installed candidate independently repeated canonical ingest, readiness, commit, worker acknowledgment, air-window completion, and post-proof health.

## Scope

This proves the exact candidate's installable, recovery-safe canonical ingest-to-air spine on this Windows machine. It does not replace the required seven-day one-station and seven-day three-station field gates, and it does not claim completion of the remaining S1–S26 product capabilities.
