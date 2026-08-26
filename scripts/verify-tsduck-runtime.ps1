[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'tsduck-runtime.ps1')

$layoutValidator = Get-Command Assert-TownLightTSDuckArchiveLayout -ErrorAction SilentlyContinue
if ($null -eq $layoutValidator) {
    throw 'ASSERTION FAILED: TSDuck archive entry layout must be validated before extraction.'
}

function New-TownLightTSDuckFixtureArchive {
    param([Parameter(Mandatory = $true)][string]$Path, [switch]$UnsafeEntry)
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::Open($Path, [IO.Compression.ZipArchiveMode]::Create)
    try {
        $entries = @(
            'TSDuck/LICENSE.txt', 'TSDuck/OTHERS.txt', 'TSDuck/bin/tsp.exe',
            'TSDuck/bin/tsanalyze.exe', 'TSDuck/bin/tsduck.dll', 'TSDuck/bin/tscore.dll'
        )
        if ($UnsafeEntry) { $entries += 'TSDuck/../escaped.txt' }
        foreach ($name in $entries) {
            $entry = $archive.CreateEntry($name)
            $writer = [IO.StreamWriter]::new($entry.Open())
            try { $writer.Write('fixture') } finally { $writer.Dispose() }
        }
    }
    finally { $archive.Dispose() }
}

$layoutRoot = Join-Path ([IO.Path]::GetTempPath()) ('townlight-tsduck-layout-' + [guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path $layoutRoot -Force | Out-Null
    $validLayout = Join-Path $layoutRoot 'valid.zip'
    New-TownLightTSDuckFixtureArchive -Path $validLayout
    Assert-TownLightTSDuckArchiveLayout $validLayout
    $priorArchiveName = $script:TownLightTSDuckArchiveName
    $priorArchiveSize = $script:TownLightTSDuckArchiveSizeBytes
    $priorArchiveHash = $script:TownLightTSDuckArchiveSha256
    try {
        $script:TownLightTSDuckArchiveName = 'valid.zip'
        $script:TownLightTSDuckArchiveSizeBytes = [int64](Get-Item -LiteralPath $validLayout).Length
        $script:TownLightTSDuckArchiveSha256 = (Get-FileHash -LiteralPath $validLayout -Algorithm SHA256).Hash.ToLowerInvariant()
        $staging = Join-Path $layoutRoot 'validated-staging'
        $extractedRoot = Expand-TownLightTSDuckRuntime -ArchivePath $validLayout -DestinationPath $staging
        if ($extractedRoot -cne (Join-Path $staging 'TSDuck') -or -not (Test-Path -LiteralPath (Join-Path $extractedRoot 'bin\tsp.exe') -PathType Leaf)) {
            throw 'Validated TSDuck extraction did not produce its bounded root.'
        }
    }
    finally {
        $script:TownLightTSDuckArchiveName = $priorArchiveName
        $script:TownLightTSDuckArchiveSizeBytes = $priorArchiveSize
        $script:TownLightTSDuckArchiveSha256 = $priorArchiveHash
    }
    $unsafeLayout = Join-Path $layoutRoot 'unsafe.zip'
    New-TownLightTSDuckFixtureArchive -Path $unsafeLayout -UnsafeEntry
    $unsafeRejected = $false
    try { Assert-TownLightTSDuckArchiveLayout $unsafeLayout } catch { $unsafeRejected = $_.Exception.Message -match 'unsafe entry' }
    if (-not $unsafeRejected) { throw 'A path-traversal archive entry must fail before extraction.' }
    $realParent = Join-Path $layoutRoot 'real-parent'
    $junctionParent = Join-Path $layoutRoot 'junction-parent'
    New-Item -ItemType Directory -Path $realParent -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $realParent 'probe.txt'), 'fixture')
    New-Item -ItemType Junction -Path $junctionParent -Target $realParent | Out-Null
    $junctionRejected = $false
    try { Assert-TownLightTSDuckNoReparseAncestors (Join-Path $junctionParent 'probe.txt') } catch { $junctionRejected = $_.Exception.Message -match 'reparse point' }
    if (-not $junctionRejected) { throw 'A TSDuck input below a reparse parent must fail before hashing.' }
}
finally {
    if (Test-Path -LiteralPath $layoutRoot -PathType Container) {
        Remove-Item -LiteralPath $layoutRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

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
