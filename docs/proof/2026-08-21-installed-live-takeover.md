# Installed live takeover decoded proof

Date: 2026-08-21

## Candidate identity

- Source commit: `77eca154885690d06eb3712021ae1e4fedd36e8a`
- Installer SHA-256: `AD73E96F51BB37749C35ED04EEE8305ACB2AFC81D3D7C05DA7C29FA8557E9D24`
- Installed `stationd` SHA-256: `F5C846989BB6E5E79B2C0E4903F911812D7D2355BEA8BA31190B1BAC241F4F63`
- Installed `channel-worker` SHA-256: `8F2922322CCCB8B769114E497A88B3CDC2FA170E1E3A7616A7017B1BE28E6229`
- Installed `station-updater` SHA-256: `89262EF4BD377FB3A82C2255D53119DAA91D44C9AA7FEFA89B7923F474F5D67C`
- Private GStreamer runtime SHA-256: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`

The clean candidate passed all 31 installer contract requirements, installed in place with exit code
0, preserved station data, and passed `installer/verify-installed.ps1`. Both LocalSystem services
were running automatically, `/health` was ready, installed and active-slot receipts matched the
manifest, all three installed binary hashes matched, and every required private media factory
loaded.

## Installed path exercised

`scripts/prove-installed-live-takeover.ps1` used only the installed loopback API and proof-owned
UUIDs. Run `20c90467-81b9-454a-a957-71670a03e5ce` created takeover session
`e1412967-96d2-4b65-917e-be93080c59db` and then:

1. Enabled a dedicated `proof-360p30` channel with one required UDP MPEG-TS output.
2. Captured and decoded the healthy fallback transport.
3. Started a real-time FFmpeg H.264/AAC MPEG-TS source with a 997 Hz audio tone.
4. Requested and armed the revisioned live source through the installed API.
5. Took live, observed the installed runtime state `live`, captured station UDP output, and decoded
   both streams with FFmpeg.
6. Returned through the durable authority path, observed `healthy_fallback`, captured again, and
   decoded the recovered transport.
7. Verified that the same worker PID (`6820`) remained alive and disabled the proof channel during
   cleanup.

The durable audit contained the complete ordered lifecycle:

```text
arming -> armed -> take_requested -> on_air -> return_requested -> returning -> completed
```

The installed output evidence was:

| Phase | Bytes | Video frames | Audio frames | First video MD5 | Peak audio |
|---|---:|---:|---:|---|---:|
| Fallback | 886,984 | 83 | 222 | `bc68d328e87549cdd8c7b39021d94c1b` | -91.0 dB |
| Live | 260,568 | 85 | 54 | `22d3bad4fd0d3e1f236c1b6e5eb5c9f2` | -16.8 dB |
| Recovery | 896,196 | 116 | 224 | `bc68d328e87549cdd8c7b39021d94c1b` | -91.0 dB |

The changed live fingerprint proves that live video replaced fallback. The audible live level proves
that decoded source audio, rather than encoded silence, reached the station output. The exact
fallback fingerprint returned after the durable return. The proof result status was `passed`.

## Regression evidence

The worker-level regression pre-rolls the external sender for ten seconds, clears pre-take UDP
backlog, captures raw station output, decodes signed 16-bit PCM, and measures sample energy. With the
prior live timing behavior temporarily restored, the unchanged regression failed with 450,560
decoded samples at `0.00` mean absolute energy. It passed after the live branch was made to skip to
its first observed audio buffer, use `clocksync(sync-to-first)` as its sole audio running-time rebase,
and stop adding pipeline running time again at the audio selector pad. The shared post-selector
`audiorate` remains in place and the repeated stored-file test proves monotonic output PTS.

## Non-claims

This proves one exact installed candidate on one Windows machine, using a local UDP MPEG-TS source,
the installed API, durable takeover authority, a persistent worker, decoded output audio/video,
return to fallback, and proof cleanup. It does not prove a clean first-ever machine install, a human
browser click path, schedule catch-up after a long takeover, SDI/NDI or remote SRT input, a 24-hour
soak, the required one-week single-station field run, or the required one-week three-station run.
