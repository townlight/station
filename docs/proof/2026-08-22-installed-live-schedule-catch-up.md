# Installed live schedule catch-up decoded proof

Date: 2026-08-22

## Candidate identity

- Source commit: `2513d4dfd91cf6aed66ebc56fd9bf81b6679739a`
- Installer SHA-256: `95B13E0FC8D7E538DD0467647BD1BC03C9007F1686498EE66513DE7F7736984C`
- Installed `stationd` SHA-256: `DBC8C5144EB3771CE376D177AF393EC67691A1A0F1E6C3638D03122C17892AFD`
- Installed `channel-worker` SHA-256: `EF5264AE25EC12883BCAD8846C3AD49D413022707C425158506576E06CD4210D`
- Installed `station-updater` SHA-256: `0DC9424A65498CDF6C0C0908FE27486683EA6B36EC3133DAA8E5FDD44134AEE8`
- Private GStreamer runtime SHA-256: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`

The clean candidate passed all 31 installer contract requirements, installed in place with exit code
0, and passed `installer/verify-installed.ps1`. Both automatic LocalSystem services were running,
`/health` was ready, the installed receipt matched the candidate manifest, all executable hashes
matched, and every required private media factory loaded.

## Installed path exercised

`scripts/prove-installed-live-schedule-catch-up.ps1` used only the installed loopback API and
proof-owned UUIDs. Run `05b00b1f-24c1-4bef-a2b7-bde6d1e1600a` created takeover session
`b8127743-6fc9-46b9-a9ff-c94220944a8a` and then:

1. Created two distinct 20-second H.264/AAC assets: red item A and blue item B.
2. Independently scheduled and decoded blue item B as a calibration capture.
3. Scheduled adjacent A and B windows and observed red A on the installed UDP output.
4. Armed and took a real-time green H.264/AAC UDP MPEG-TS source while A was active.
5. Held live across the A-to-B boundary and decoded sustained green live output.
6. Requested return after B had begun. The controller prepared B, took it at an audited 4,915 ms
   media offset, acknowledged the return, and disarmed live.
7. Captured and decoded the post-return transport, classified it as blue, and matched its video MD5
   exactly to the independent B calibration.
8. Disabled the proof channel and source during cleanup.

The durable audit contained the complete ordered lifecycle:

```text
arming -> armed -> take_requested -> on_air -> return_requested -> returning -> completed
```

The installed output evidence was:

| Phase | Bytes | Video frames | Audio frames | Color | Video MD5 | Peak audio |
|---|---:|---:|---:|---|---|---:|
| B calibration | 73,508 | 53 | 139 | blue | `75b2fb963bb8f9b9ad4bbe403218b1ee` | -23.1 dB |
| Before live | 82,532 | 86 | 140 | red | `30a6e1ed4785aeb372e88790fa60e197` | -23.1 dB |
| Live | 157,920 | 133 | 280 | green | `120419e7299990219cb9726c829a5ef3` | -17.6 dB |
| After return | 81,216 | 82 | 139 | blue | `75b2fb963bb8f9b9ad4bbe403218b1ee` | -23.7 dB |

The observed B media position was 4,850 ms, 65 ms from the audited 4,915 ms return offset. The
post-return capture contained 3.015 seconds of transport and sustained decoded audio. The exact B
fingerprint match proves the station returned to the scheduled successor rather than fallback,
stale A, residual live output, or an unrelated blue frame. The proof result status was `passed`.

## Regression and failure evidence

The preceding installed candidate `122c067ddcccc0bd5342548a50e650f54ac4a614` failed this same
installed path after returning at 4,914 ms: its final capture contained only 22,372 bytes. A focused
real-media regression reproduced the defect with only 2,048 decoded stereo samples after handback.
The retained GStreamer trace showed the file selector timeline approximately 4.9 seconds behind the
active stream because the requested seek position was subtracted twice. The fix applies the full
current pipeline running time to both file selector pads; the seek segment already owns the media
position. The focused regression then decoded 321,536 returned stereo samples across six audible
half-second chunks, and all 16 persistent-media tests passed before packaging.

## Non-claims

This proves one exact installed candidate on one Windows machine, using real adjacent schedule
windows, a live source crossing their boundary, positive-offset return, durable authority, decoded
audio/video, and proof cleanup. It does not prove a clean first-ever machine install, the human
double-click/browser workflow, headend output, a 24-hour soak, the required one-week single-station
field run, or the required one-week three-station run.
