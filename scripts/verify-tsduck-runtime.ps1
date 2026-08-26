[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'tsduck-runtime.ps1')

$pin = Get-TownLightTSDuckPin
if ($pin.version -cne '3.44-4676' -or
    $pin.archive_name -cne 'TSDuck-Win64-3.44-4676-Portable.zip' -or
    $pin.source_url -cne 'https://github.com/tsduck/tsduck/releases/download/v3.44-4676/TSDuck-Win64-3.44-4676-Portable.zip' -or
    $pin.archive_sha256 -cne 'b0ca0f963fcd77488b8c32d6f9d85030daa891ed753c36e57bbb458807a44eb1' -or
    [int64]$pin.archive_size_bytes -ne 38476542) {
    throw 'TSDuck pin is not the authenticated official release identity.'
}

$missing = Join-Path ([IO.Path]::GetTempPath()) 'townlight-tsduck-missing.zip'
$missingRejected = $false
try { Assert-TownLightTSDuckArchive $missing | Out-Null } catch { $missingRejected = $_.Exception.Message -match 'archive is missing' }
if (-not $missingRejected) { throw 'A missing TSDuck source must fail closed with a deterministic error.' }

$wrong = Join-Path ([IO.Path]::GetTempPath()) 'not-tsduck.zip'
[IO.File]::WriteAllBytes($wrong, [byte[]](1, 2, 3))
try {
    $wrongRejected = $false
    try { Assert-TownLightTSDuckArchive $wrong | Out-Null } catch { $wrongRejected = $true }
    if (-not $wrongRejected) { throw 'A non-pinned archive must fail closed.' }
}
finally { Remove-Item -LiteralPath $wrong -Force -ErrorAction SilentlyContinue }

Write-Output 'TSDuck runtime pin fixture passed (official URL/hash/size + fail-closed missing and mismatch cases).'
