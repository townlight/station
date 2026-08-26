[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$helperPath = Join-Path $PSScriptRoot 'installed-private-runtime-verifier.ps1'
$installerVerifierPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'installer\verify-installed.ps1'
$proofPath = Join-Path $PSScriptRoot 'prove-installed-automatic-materialization.ps1'
$assertions = 0

function Assert-Contract([bool]$Condition, [string]$Message) {
    $script:assertions++
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
}

Assert-Contract (Test-Path -LiteralPath $helperPath -PathType Leaf) 'the shared private-runtime verifier must exist.'
$helperSource = Get-Content -LiteralPath $helperPath -Raw
$tokens = $null
$parseErrors = $null
[Management.Automation.Language.Parser]::ParseFile($helperPath, [ref]$tokens, [ref]$parseErrors) | Out-Null
Assert-Contract ($parseErrors.Count -eq 0) 'the shared private-runtime verifier must parse.'
Assert-Contract ($helperSource -notmatch '(?s)Get-ChildItem[^\r\n]*-Recurse') `
    'the verifier must never enumerate recursively before rejecting reparse points.'
Assert-Contract ($helperSource -match 'pendingDirectories\.Push' -and
    $helperSource.IndexOf('ReparsePoint', [StringComparison]::Ordinal) -lt
        $helperSource.LastIndexOf('pendingDirectories.Push', [StringComparison]::Ordinal)) `
    'the verifier must reject a child reparse point before pushing a directory for descent.'
Assert-Contract ($helperSource -match '\[int32\]' -and $helperSource -match '\[int64\]') `
    'the verifier must explicitly accept only safe integral Int32 or Int64 manifest sizes.'
foreach ($caller in @($installerVerifierPath, $proofPath)) {
    $callerSource = Get-Content -LiteralPath $caller -Raw
    Assert-Contract ($callerSource -match 'installed-private-runtime-verifier\.ps1' -and
        $callerSource -match 'Assert-TownLightInstalledPrivateRuntime') `
        "the runtime-verification caller must use the shared helper: $caller"
}

. $helperPath
$temporaryBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
$temporaryRoot = [IO.Path]::GetFullPath((Join-Path $temporaryBase ("townlight-runtime-verifier-" + [guid]::NewGuid().ToString('N'))))
if (-not $temporaryRoot.StartsWith($temporaryBase, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The runtime verifier fixture escaped the operating-system temp root.'
}
$runtimeRoot = Join-Path $temporaryRoot 'runtime'
$linkPath = Join-Path $runtimeRoot 'reparse-fixture'
$reparseFixture = 'skipped'
try {
    New-Item -ItemType Directory -Path (Join-Path $runtimeRoot 'bin') -Force | Out-Null
    [IO.File]::WriteAllBytes((Join-Path $runtimeRoot 'bin\one.dll'), [byte[]](1, 2, 3, 4))
    [IO.File]::WriteAllBytes((Join-Path $runtimeRoot 'two.dat'), [byte[]](9, 8, 7))
    $manifest = [pscustomobject]@{
        files = @(
            [pscustomobject]@{
                path = 'bin/one.dll'; size_bytes = [int64]4
                sha256 = (Get-FileHash -LiteralPath (Join-Path $runtimeRoot 'bin\one.dll') -Algorithm SHA256).Hash.ToLowerInvariant()
            },
            [pscustomobject]@{
                path = 'two.dat'; size_bytes = [int64]3
                sha256 = (Get-FileHash -LiteralPath (Join-Path $runtimeRoot 'two.dat') -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        )
    }
    $verified = Assert-TownLightInstalledPrivateRuntime -RuntimeRoot $runtimeRoot -RuntimeManifest $manifest
    Assert-Contract ($verified.file_count -eq 2) 'the exact safe fixture must pass with both files verified.'

    $jsonManifest = ($manifest | ConvertTo-Json -Depth 4) | ConvertFrom-Json
    $jsonSize = $jsonManifest.files[0].size_bytes
    Assert-Contract ($jsonSize -is [int32] -or $jsonSize -is [int64]) `
        'the current PowerShell JSON reader must materialize a small manifest size as a safe integral type.'
    $jsonVerified = Assert-TownLightInstalledPrivateRuntime -RuntimeRoot $runtimeRoot -RuntimeManifest $jsonManifest
    Assert-Contract ($jsonVerified.file_count -eq 2) `
        'a ConvertFrom-Json runtime manifest must verify with its exact Int32 or Int64 file sizes.'

    $typedFailure = $null
    $badTypedManifest = [pscustomobject]@{ files = @([pscustomobject]@{
                path = 'two.dat'; size_bytes = '3'; sha256 = $manifest.files[1].sha256
            }) }
    try { Assert-TownLightInstalledPrivateRuntime -RuntimeRoot $runtimeRoot -RuntimeManifest $badTypedManifest | Out-Null }
    catch { $typedFailure = $_.Exception.Message }
    Assert-Contract ($typedFailure -match 'runtime_manifest_file_invalid') `
        'string-coerced manifest sizes must fail before filesystem comparison.'

    foreach ($invalidSize in @([double]3, [decimal]3, $true, -1, [uint64]::MaxValue)) {
        $invalidFailure = $null
        $invalidManifest = [pscustomobject]@{ files = @([pscustomobject]@{
                    path = 'two.dat'; size_bytes = $invalidSize; sha256 = $manifest.files[1].sha256
                }) }
        try { Assert-TownLightInstalledPrivateRuntime -RuntimeRoot $runtimeRoot -RuntimeManifest $invalidManifest | Out-Null }
        catch { $invalidFailure = $_.Exception.Message }
        Assert-Contract ($invalidFailure -match 'runtime_manifest_file_invalid') `
            "manifest size type '$($invalidSize.GetType().FullName)' must fail before filesystem comparison."
    }

    $outsideDirectory = Join-Path $temporaryRoot 'owned-link-target'
    New-Item -ItemType Directory -Path $outsideDirectory | Out-Null
    [IO.File]::WriteAllText((Join-Path $outsideDirectory 'must-not-be-read.txt'), 'owned canary')
    try {
        [IO.Directory]::CreateSymbolicLink($linkPath, $outsideDirectory) | Out-Null
        $reparseFailure = $null
        try { Assert-TownLightInstalledPrivateRuntime -RuntimeRoot $runtimeRoot -RuntimeManifest $manifest | Out-Null }
        catch { $reparseFailure = $_.Exception.Message }
        Assert-Contract ($reparseFailure -match 'private_runtime_reparse_point') `
            'a harness-owned directory link must be rejected before descent.'
        $reparseFixture = 'passed'
    }
    catch {
        if ($_.Exception -is [MissingMethodException] -or
            $_.Exception.Message -match 'CreateSymbolicLink|does not contain a method named' ) {
            $reparseFixture = 'skipped-no-link-api'
        } elseif ($_.Exception.InnerException -is [UnauthorizedAccessException] -or
            $_.Exception.Message -match '(?i)required privilege|not held by the client|access.*denied') {
            $reparseFixture = 'skipped-no-link-privilege'
        } elseif ($_.Exception.InnerException -is [PlatformNotSupportedException]) {
            $reparseFixture = 'skipped-platform'
        } else { throw }
    }
}
finally {
    if (Test-Path -LiteralPath $linkPath) { Remove-Item -LiteralPath $linkPath -Force -ErrorAction SilentlyContinue }
    $resolvedForCleanup = [IO.Path]::GetFullPath($temporaryRoot)
    if ($resolvedForCleanup.StartsWith($temporaryBase, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path $resolvedForCleanup -Leaf) -like 'townlight-runtime-verifier-*') {
        Remove-Item -LiteralPath $resolvedForCleanup -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Output "Installed private-runtime verifier contract passed ($assertions assertions; reparse fixture: $reparseFixture)."
