# Clean Windows Sandbox installation proof — 2026-08-25

## Candidate boundary

This is clean-machine evidence for exact source commit
`b6ae66e71e57cb2d48e7d5de917bc78349bb8d60` and installer SHA-256
`f8ab0b5e4ef0b3809018b0713dc68d5efb633a7b83f4142d88a7e32dc5d86bff`.
The candidate was produced from a clean worktree by `installer/build-installer.ps1` with the pinned
private GStreamer and LGPL FFmpeg inputs.

The proof ran in a fresh Windows Sandbox through the shared-machine ownership guard. The guest was
given only separate read-only candidate, installed-verifier, shared candidate-verifier, and harness mappings. Its only
host-writable path was `%ProgramData%\TownLight Station\SandboxEvidence`. Networking, clipboard,
audio/video input, GPU, and printer redirection were disabled. The host harness did not terminate,
adopt, or clean any Sandbox process; the guest published its result atomically and shut itself down.

## Defect exposed and corrected

The first exact candidate, commit `f1caaf6e726e92a59673783779f80464439827cb`, authenticated its
installer and reached running station and updater services, but then returned installer exit code
3 and rolled the candidate back. Its retained Inno log proved that activation failed only because
the network-isolated guest could not open the operator console with `ShellExecute`.

The installer now has a contract-tested unattended authentication boundary. Silent setup still
queries the service-owned authentication state and provisions the protected one-time bootstrap
authority, but it does not read that capability into a URL or launch a browser. Interactive
double-click setup retains the one-time fragment launch path. The installer contract passed 81
requirements after the correction.

## Passing executed proof

Guest run `edda59e9-d4f3-4c1f-9e13-9344e12b5233` started at
`2026-08-25T14:28:16.9570765Z` and completed at `2026-08-25T14:33:40.2902181Z` with:

- installer exit code 0;
- `TownLightStation` running automatically as `LocalSystem`;
- `TownLightStationUpdater` running automatically as `LocalSystem`;
- loopback health `ready`;
- installed source receipt equal to the exact candidate commit;
- installed `stationd.exe` SHA-256
  `25592597e7b08b3b05fe7950e100f1c63ce5178dcfabf314913b8473d91fa6d0`;
- installed `channel-worker.exe` SHA-256
  `c8cac6db3b3ec56650dce549092736d2190d8d79d6022322c305f2d0b98d704a`;
- installed `station-updater.exe` SHA-256
  `8427eb1f210a0ce4787e18d0d69866124e073229526b3388d2b023206e687a5b`;
- pinned caption decoder SHA-256
  `b6b61f0d99c55e422bbd59fad6880a8d4f96d99a6c865f3bb58c8f1ce3ff7b31`;
- pinned decoder license SHA-256
  `da7eabb7bafdf7d3ae5e9f223aa5bdc1eece45ac569dc21b3b037520b4464768`;
- installed runtime manifest SHA-256
  `4402e5d45c413a360ccae9a723882fa7b171c7133217a055a35d629dd0ff2ed5`;
- every required private media factory loadable.

The append-only guest JSON and both first-attempt and passing installer logs remain under the
dedicated TownLight evidence directory. A post-result ownership check still observed one delegated
`WindowsSandboxServer.exe` identity as `external_or_unclaimed`; the harness correctly blocked any
new launch and left it untouched rather than inferring ownership from the completed result.

## Non-claims

This proves an exact candidate can install and verify on one fresh virtual Windows machine. It does
not prove physical StationBox hardware, double-click operator usability, downstream headend or
receiver acceptance, continuous broadcast operation, the one-station seven-day gate, or the
three-station seven-day gate.
