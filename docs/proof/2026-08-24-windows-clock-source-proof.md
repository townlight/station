# Windows configured-source clock proof — 2026-08-24

## Boundary

This source and installed-machine gate proves that StationBox clock readiness measures the current
Windows Time source rather than treating a successful status command as synchronization. It does not
prove long-duration clock stability or any external headend/field gate.

The probe follows Microsoft's documented Windows Time interfaces: `/query /source` identifies the
current source, while `/stripchart /rdtsc` returns offset samples. Microsoft documents Windows Time as
the Windows 11/Server 2022 synchronization service and the `/rdtsc` output as comma-separated
`RdtscStart`, `RdtscEnd`, `FileTime`, `RoundtripDelay`, and `NtpOffset` values.

## Failing test

The test-first run failed to compile because `WINDOWS_CLOCK_PROBE_SCRIPT` and
`parse_windows_clock_probe_result` did not exist. This establishes that the new contract was absent.

## Contract

- The station service queries only its current Windows Time source; no HTTP value supplies a peer or
  command.
- The source is length- and character-bounded before it becomes one process argument.
- Exactly three one-second `/rdtsc` samples are required.
- Every sample must be finite and within two seconds to report `synchronized`.
- The PowerShell boundary emits one exact ASCII status, and the Rust boundary accepts no additional
  text or casing variation.
- Command failure, malformed output, missing samples, and excessive offsets fail closed.

## Evidence

`scripts/invoke-cargo.ps1 test --offline -p station-api windows_clock_probe_ -- --test-threads=1`
passed both focused contract tests. The complete `station-api` package then passed 80 tests with the
one explicit machine-inventory proof lane ignored by design, and warning-denied Clippy passed for all
package targets and features.

A direct read-only Windows probe identified `time.windows.com` as this machine's configured source
and returned three finite `/rdtsc` offset samples. That source observation validates the command
shape but is not installed-product evidence; the exact committed candidate must still be built,
installed, and queried through its authenticated StationBox authority.

## Sources

- Microsoft, [Windows Time Service tools and settings](https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/windows-time-service-tools-and-settings)
- Microsoft, [How the Windows Time Service works](https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/how-the-windows-time-service-works)
