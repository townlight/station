# Installed CG image apply, change, and clear proof

Date: 2026-08-21

## Candidate identity

- Source commit: `c2cf23d7302695214ed27711b50e61e2eb2ae31f`
- Installer SHA-256: `030D526CE96CE2757EC379165ADE4486AAF529D0A72B1DF766F327AD8F233BB2`
- Installed `stationd` SHA-256: `55B414BF541A61A6037BB61611114D1F7426E90D8AE718F53A1C737F0585B163`
- Installed `channel-worker` SHA-256: `30ED39861B2ED7735028148A9AF445828D7A61E6D8752A094CE750D0B84526D4`
- Private GStreamer runtime SHA-256: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`

The clean candidate passed all 31 installer contract requirements, installed in place with exit code
0, preserved station data, and passed `installer/verify-installed.ps1`. Both LocalSystem services
were running, `/health` was ready, installed/active-slot receipts and binary hashes matched the
manifest, and all required private media factories—including PNG/JPEG image support—loaded.

## Installed path exercised

The installed station service created a dedicated `proof-360p30` channel and one persistent worker.
The proof then:

1. Captured and decoded the baseline MPEG-TS program pixel.
2. Generated two bounded 96×96 PNGs and uploaded their raw bytes through
   `POST /api/v1/cg/images`.
3. Confirmed both server-issued SHA-256 identities, ready state, MIME, dimensions, and size through
   the installed `GET /api/v1/cg/images` route.
4. Created the proof board and bound the red identity to its sidebar through revisioned, audited API
   writes.
5. Polled resolved preview authority, captured the installed UDP output, checked 188-byte transport
   alignment/sync, and decoded the sidebar pixel as RGB `[217, 19, 18]`.
6. Updated the same live zone to the green identity without restarting the worker, captured a second
   valid transport, and decoded RGB `[17, 218, 19]`.
7. Disabled the board, kept the same worker alive, allowed the acknowledged live-clear transition,
   captured again, and confirmed every decoded channel returned within 20 levels of the captured
   baseline.

The red and green captures contained 931,728 and 939,624 bytes respectively. The proof exited 0.
Post-proof verification again matched installed hashes and ready services. Cleanup left the proof
channel disabled, the proof board inactive at revision 4, and zero proof zones.

Two harness-only defects were exposed and corrected before the passing run: PowerShell preserved an
empty JSON array as one pipeline object when the function call was wrapped directly, and FFmpeg
rounds a 1×1 crop to zero for chroma-subsampled input. The final harness normalizes responses through
variables and samples an even 2×2 crop before RGB conversion. Neither defect was in the installed
product.

## Non-claims

This proves the installed local API, persistent worker, managed image authority, image apply/change,
live clear, decoded output pixels, transport continuity, and cleanup on one Windows machine. It does
not prove a human browser click path, 24-hour soak, seven-day one-station operation, three-station
operation, WPE/HTML, live video, background audio, or animated ticker behavior.
