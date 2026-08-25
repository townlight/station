# Configured-source outbound UDP proof — 2026-08-25

## Boundary

This slice replaces a route-selection-only StationBox network observation with one bounded real
request/reply proof. The service queries the current Windows Time source and requires exactly three
finite `w32tm /stripchart /rdtsc` samples. Completing those samples proves one outbound UDP path to
that source. When the source is a hostname, the completed command also proves that Windows name
resolution succeeded. An IP-literal source leaves DNS unknown.

This does not prove arbitrary UDP destinations, SRT, a configured cable headend, MPEG-TS receipt,
decoder acceptance, or field operation. Those facts remain unknown on their owning authorities.

## Test-first evidence

The focused RED run executed one test and failed on its assertion because the old single-word parser
returned `Unknown` for `synchronized dns udp`:

`scripts/invoke-cargo.ps1 test --offline -p station-api windows_clock_probe_result_accepts_only_one_exact_ascii_status -- --test-threads=1`

`test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 37 filtered out; finished in 0.00s`

The implemented boundary accepts only exact ASCII combinations for synchronized/unsynchronized,
hostname/IP-literal, and UDP evidence. Added text, case changes, legacy single-word status, or an SRT
claim degrades to unknown. Command failure, unsafe source identity, missing samples, malformed or
non-finite offsets, timeout, and bounded-output failure cannot produce outbound support.

## Verification

The focused contract run passed two tests and retained the explicit machine lane as ignored:

`scripts/invoke-cargo.ps1 test --offline -p station-api windows_time_probe_ -- --test-threads=1`

`test result: ok. 2 passed; 0 failed; 1 ignored; 0 measured; 36 filtered out; finished in 0.00s`

The ordinary development token received Windows error `0x80070005` from `w32tm /query /source`, so
the ignored machine lane correctly failed unavailable rather than manufacturing evidence. The same
exact lane was then run elevated to match the installed LocalSystem service boundary:

`scripts/invoke-cargo.ps1 test --offline -p station-api tests::windows_time_probe_current_machine_completes_a_real_udp_exchange -- --ignored --exact --test-threads=1`

`test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 38 filtered out; finished in 2.41s`

The complete `station-api` package passed 80 tests with the Windows inventory and configured-source
machine lanes explicitly ignored during the deterministic suite. Warning-denied Clippy passed for
all package targets and features.

## Remaining installed boundary

The exact committed candidate must still be built and installed. Authenticated observation through
`GET /api/v1/station-box-profile` also requires the station owner to create the first operator
account; the current installed station reports `bootstrap_required`, so that route result is not yet
claimed.

## Sources

- Microsoft, [Windows Time Service tools and settings](https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/windows-time-service-tools-and-settings)
- Microsoft, [How the Windows Time Service works](https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/how-the-windows-time-service-works)
