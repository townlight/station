# Installed watch-folder ingest proof — 2026-08-20

## Candidate identity

- Source commit: `4b4650ece6d03a1f1563c8d879e5ce8a880b026b`
- Installer SHA-256: `BB1EB99F971A0A375D9736C890B174662F4215A0D341C27DE0F7E2194DBF40A5`
- Installed `stationd.exe` SHA-256: `DC174263860698D05668C4ADF66B4D7D7129537DE0EF12679095FDB7AF74CE7E`
- Installed `channel-worker.exe` SHA-256: `4E6449D4B74D73665C98BA1B13AE5E2CB8FCAAF1D65F9D5A1F14BBDAD5ABD0E1`
- Private media-runtime SHA-256: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`

The candidate passed the 18-requirement installer contract, installed with exit code zero, preserved the existing station database, and passed the elevated installed verifier. The verifier observed the exact source receipt and binary hashes above, a running `LocalSystem` service, ready media factories, and a ready health response.

## Unattended stable-file scenario

The installed API configured enabled watch folder `7d73ff14-2296-4588-a6ca-06919794d307` with a five-second settle window and `.ts` allow-list. The service scanned the empty directory successfully with no error.

A real finite H.264/AAC MPEG-TS file was then copied to `council-installed-proof.ts`. The readiness dashboard contained zero matching jobs immediately after the copy. The service created durable watch job `fc0d0fe4-d2ae-50b2-959f-e0342ba30a8b` 9,886 milliseconds later, after the required matching polls and settle duration; no upload endpoint was called.

The job reported `source_kind=watch_folder`, completed on its first attempt, and resolved to content identity `39f07788314d8bd53cc7d4d57d34a9e14af800a09ead5cdd6600980652dfdd6d`. The installed service independently measured 11,992 milliseconds of finite A/V and returned the station-owned path beneath `%ProgramData%\TownLight Station\media`. Because identical bytes had already been admitted, content-addressed ingest correctly converged on the existing asset instead of creating a duplicate object.

## Restart deduplication

The Windows service was restarted after ingest and returned to ready health. After another twelve seconds—more than two poll intervals and the complete settle window—the dashboard still contained exactly one job for the watched file, with the same deterministic job identity and completed state. The persisted file observation therefore survived restart and prevented a false re-ingest.

## Offline-path isolation

A second enabled configuration, `c40c32a9-c5db-489c-90be-8ccc0daa77be`, targeted an unavailable path. Its next installed-service scan stored the operator-readable error:

```text
Watch folder 'Installed offline share proof' is unavailable: The system cannot find the path specified. (os error 3)
```

The station health endpoint remained ready. The outage did not stop the API, existing media authority, or other watch folders. Both proof configurations were disabled afterward so their durable evidence remains without ongoing polling.

## Scope

The workspace gate for this candidate passed formatting, clippy with warnings denied, 49 tests with zero failures or ignores—including real GStreamer, process, migration, transient-retry, and watch-folder tests—diff validation, and the zero-legacy-name scan. This installed proof advances watch-folder ingest to a machine-proven scenario on one Windows station. It does not replace the required 24-hour media-lifecycle soak or the final seven-day one-station and three-station field gates.
