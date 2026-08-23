# Installed progressive headend output proof — 2026-08-23

## Exact installed candidate

The tested installer was built from clean source commit
`bd032f00f142dfd3475a76b406391fde611b5788` after the complete workspace test
suite, strict workspace Clippy, the production operator build, and all 31 installer
contract checks passed.

- Installer SHA-256: `eaf5b87320380e80f7530d5774ca85aaa7d59c3ef2d4e4eaf084431c3a15da54`
- `stationd.exe` SHA-256: `d3c7495d5c07a38418292bc1da773d258cf79ccf04a961464eca32ebec32c5f7`
- `channel-worker.exe` SHA-256: `cc32b0b580f355a5df5f75f7dfa39d5ef1ff421e124c2708ae63fe720cca2c5d`
- `station-updater.exe` SHA-256: `2b7f2e34d95bf6ca3be28086a75789cbfa07dcbf0ef2f2958b41e958f76dfa11`
- Private media-runtime SHA-256: `059251444d1267b486eba390b18d25fed87e10315e72f757ec6c7e912fa746b5`

The installer exited successfully without requiring a reboot. The independent
post-install verifier matched all installed hashes and receipts, found both
automatic services running as `LocalSystem`, parsed current-boot database and
orchestration health as ready, and loaded the required private media factories.
The same verifier passed again after both output proofs.

## Failure that established the boundary

Installed candidate `2c474144def3b4d837bd2589cd45bbf919f4b11c` was not accepted as
proof-complete. It emitted real transport and preserved durable failure truth, but
its fixed two-second physical capture headroom was insufficient at an arbitrary
MPEG-TS/GOP boundary:

- A 5,000 ms operator request captured 4,704,700 bytes but independently decoded
  only 4,006 ms. Durable job `4d3b521d-e28d-41ef-a45b-e147478b047e` failed.
- A 3,000 ms operator request captured 2,904,412 bytes but independently decoded
  only 2,010 ms. Durable job `966edfce-364c-471d-b1d2-619e9fae6a34` failed.
- Both attempts emitted only 1,316-byte UDP datagrams at approximately 7.5 Mb/s,
  and both proof channels were disabled by cleanup.

A controlled real Generic 720p59.94 regression then began capture 250 ms after the
initial codec-header burst. The old 5,000 ms physical window reproduced the defect
with 2,012–2,013 ms analyzed against a 3,000 ms request. The corrected contract
keeps the operator request at 3–300 seconds and 256 MiB, but adds four seconds of
bounded physical capture headroom. The identical regression passed with a 7,000 ms
physical window. The protocol maximum is therefore 304,000 ms, derived from one
shared constant rather than duplicated limits.

## Installed Generic UDP SPTS result

`scripts/prove-installed-output-loudness.ps1` ran through only the installed
loopback API with profile `generic-udp-spts` and a real audible scheduled asset.
Result status was `passed`.

- Channel: `4dd675f5-0bbb-4dee-9354-650eceea368e`
- Durable proof job: `8ed64c96-7ed0-468f-9779-fe439768b9e4`, state `completed`
- Installed source identity: exact commit `bd032f00f142dfd3475a76b406391fde611b5788`
- UDP sample: 3,637 datagrams; every datagram was 1,316 bytes; measured bitrate
  7,513,532 b/s against the locked 8 Mb/s contract tolerance
- Post-mux artifact: 4,623,108 bytes; SHA-256
  `d6e51cc63984f39ce75409b88f505a5397d34ffdbd559fec33d3d1a4f46a0162`
- Loudness: `-23.932 LUFS` against `-24 LUFS`, status `pass`
- Timing: 7,000 ms physical capture window; 4,006 ms independently analyzed for
  the 3,000 ms operator request
- Runtime: the proof channel was observed `on_air` and green; local headend
  transport was `pass`
- External receiver acceptance: `not_attempted`; headend roll-up remained yellow
- Cleanup: proof channel disabled and verified through `GET /api/v1/channels`

## Installed TelVue HyperCaster IP result

The harness then ran separately with profile `telvue-hypercaster-ip`. Result status
was `passed`.

- Channel: `bf64b23f-bd2c-4f53-af3a-ef6f3eaad062`
- Durable proof job: `f641aafa-858b-4b21-9d39-2de9a47a27a4`, state `completed`
- Installed source identity: exact commit `bd032f00f142dfd3475a76b406391fde611b5788`
- UDP sample: 3,534 datagrams; every datagram was 1,316 bytes; measured bitrate
  7,290,922 b/s against the locked 8 Mb/s contract tolerance
- Post-mux artifact: 4,904,732 bytes; SHA-256
  `10ea88cd377b825406698e11e5718ce04393ddd64aabf478ee76503cff922aa3`
- Loudness: `-23.943 LUFS` against `-24 LUFS`, status `pass`
- Timing: 7,000 ms physical capture window; 4,008 ms independently analyzed for
  the 3,000 ms operator request
- Runtime: scheduled media reached `on_air`; local headend transport was `pass`
- External receiver acceptance: `not_attempted`; headend roll-up remained yellow
- Cleanup: proof channel disabled and verified through `GET /api/v1/channels`

After both runs the installed API reported database and current-boot orchestration
health ready, no proof-owned FFmpeg process remained, and all 43 persisted channels
were disabled.

## What this proves—and what it does not

This proves that the exact installed candidate can apply both currently executable
progressive headend profiles, put scheduled audible media on air, emit locally
observed 1,316-byte 8 Mb/s-class UDP MPEG-TS, capture bounded post-mux bytes without
interrupting output, and independently pass the configured cable loudness target.
It also proves failure and cleanup behavior for the superseded short-headroom
candidate.

It does not claim that a TelVue or other downstream receiver accepted or decoded
the stream. It does not prove interlaced, SDI, NDI, or file-drop outputs, a clean
first-ever machine installation, operator completion of first-run commissioning,
or the required one-station and three-station field weeks. Those remain separate
gates. Historical critical incidents already present in this machine's retained
database kept the aggregate station safe-to-air roll-up red during the proof; the
new proof channel itself was green and `on_air`, and this harness does not erase or
reinterpret unrelated historical operating truth.
