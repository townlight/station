# Installed output loudness proof — 2026-08-23

## Defect found and corrected

The first installed proof against source commit
`7c13a0f3112cbe99899c9cffe49dd994a4a9058a` failed honestly. A canonical
asset measured `-24.049 LUFS`, but two independent post-mux captures measured
approximately `-2.5 LUFS` for a configured `-16 LUFS` streaming output. FFmpeg
EBU R128 analysis independently reproduced the stored result, so this was not
an API, persistence, or analyzer-reporting error.

The cause was an explicit zero-nanosecond tolerance on all three `audiorate`
elements in the persistent, file, and live audio paths. The setting forced
pathological timestamp correction ahead of `audioloudnorm`; the normalizer
then over-gained the signal into its true-peak limiter. A direct graph A/B
measured `-2.54 LUFS` with zero tolerance and `-15.97 LUFS` with the documented
40-millisecond tolerance.

## Automated evidence

- The engine now applies one `40_000_000` nanosecond audio timestamp tolerance
  to the persistent, file, and live paths.
- The regression generates real MPEG-TS/H.264/AAC media, canonicalizes it to
  the `-24 LUFS` playout contract, loads the content-addressed file while
  fallback remains on air, takes the file at offset zero, captures the
  streaming variant after the mux, and independently requires `-16 ±2 LUFS`.
- A controlled red-phase run restored the old zero-tolerance behavior and the
  exact regression failed at `-2.594 LUFS`. Restoring the fix made it pass.
- Warnings-denied Clippy, the complete Rust workspace, the operator loudness
  contract tests, the production console build, and the 31-requirement
  installer contract all passed from the corrected source tree.
- An adversarial review found no remaining P1 or P2 issue and confirmed there
  is no zero-tolerance audio path left in the engine.

## Installed-candidate evidence

The clean installer was built from source commit
`27ae20d527968c685a31b2ce8fb1481af934c95c`.

- Installer SHA-256:
  `6397b6a9b1456be8bdaedacc13c75cc30ab4e8d297552d39acc3c264dab34aaf`
- Installed `stationd.exe` SHA-256:
  `22f707cb896fb463f0df2716e688a2851a0d403c175c0138838de0c04c2cbc23`
- Installed `channel-worker.exe` SHA-256:
  `21e025941ba8a98360d20af209ca7af4765c200d3d23a9a4247f4d2ff758a0b5`
- Installed `station-updater.exe` SHA-256:
  `7db7b67f1a4af02bb3b798b49b889de8bb8d4e93e44d05595f2f22db36831c6d`
- Private media runtime SHA-256:
  `059251444d1267b486eba390b18d25fed87e10315e72f757ec6c7e912fa746b5`

The silent installer exited successfully. Both `LocalSystem` services were
running automatically, health was ready, all required media factories loaded,
and installed receipts and executable hashes matched the candidate manifest.

`scripts/prove-installed-output-loudness.ps1` then admitted a real audible
fixture through the public loopback API, waited for the proof-owned channel to
take the scheduled canonical asset on air, and requested a 30-second proof for
its `-16 LUFS` sink. The immutable completed result was:

- Run ID: `ba8c2160-3c78-4dd3-9c35-573e45f08fd7`
- Channel ID: `3e43a3df-2fa4-474b-8949-993f08a753a1`
- Sink ID: `0f11aa7f-6026-494e-a351-082b3643a9c3`
- Job and proof ID: `2b25fc47-0e5a-4d0f-b2da-e11d9e4d17d2`
- Configuration hash:
  `5e7f040add3819fe22fd4e5bb96ab6906be2820ac126c9a790e9038b9a2cce1b`
- Configured target: `-16.0 LUFS`
- Measured result: `-15.976 LUFS`
- Measurement status: `pass`
- Requested sample: `30,000 ms`; analyzed duration: `31,016 ms`
- Capture bytes: `5,997,012`
- Capture SHA-256:
  `0fca2e7dba8162bf449c3831149b1de5d1eeaadc6e7eba737a599f332e2d1f09`
- Receiver acceptance: `not_attempted`

An independent filesystem hash matched the immutable record exactly. FFprobe
identified H.264 video and stereo 48 kHz AAC over 31.968 seconds. Independent
FFmpeg EBU R128 analysis measured `-16.0 LUFS`. The harness disabled only its
unique proof channel during cleanup and left unrelated installed history and
channels untouched.

## Boundary

This proves local post-encode, post-mux output loudness on one installed Windows
machine and exact candidate. It does not claim cable-headend or downstream
receiver acceptance, long-duration output stability, the one-station week, or
the three-station week. Because capture begins on an already-running transport,
the independent decoder reported expected initial missing-PPS warnings before
the repeated codec configuration arrived; it subsequently decoded 901 video
frames and the complete audio measurement window.
