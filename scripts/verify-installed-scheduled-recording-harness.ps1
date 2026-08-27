[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$proofPath = Join-Path $PSScriptRoot 'prove-installed-scheduled-recording.ps1'
$runtimeVerifierPath = Join-Path $PSScriptRoot 'installed-private-runtime-verifier.ps1'
$documentationPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'docs\proof\2026-08-27-installed-scheduled-recording.md'
$assertions = 0
function Assert-Contract([bool]$Condition, [string]$Message) {
    $script:assertions++
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
}

Assert-Contract (Test-Path -LiteralPath $proofPath -PathType Leaf) 'scheduled-recording installed proof must exist.'
$source = Get-Content -LiteralPath $proofPath -Raw
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($proofPath, [ref]$tokens, [ref]$parseErrors)
Assert-Contract ($parseErrors.Count -eq 0) 'scheduled-recording proof must parse as PowerShell.'
Assert-Contract ($source -match '\[Management\.Automation\.PSCredential\]\$Credential' -and
    $source -match 'installed-proof-api\.ps1' -and $source -match 'New-TownLightProofSession' -and
    $source -match 'Invoke-TownLightProofApi') 'proof must use the in-memory authenticated API session.'
Assert-Contract ($source -match 'candidate-pair-verifier\.ps1' -and
    $source -match 'Get-TownLightCandidateIdentity' -and $source -match 'install-receipt\.json') `
    'proof must bind the installed result to the exact candidate pair and receipt.'
Assert-Contract ($source -match 'installed-private-runtime-verifier\.ps1' -and
    $source -match 'runtime-manifest\.json' -and $source -match 'runtime_manifest_sha256' -and
    $source -match 'runtime_package_sha256' -and $source -match 'media_runtime_sha256' -and
    $source -match 'Assert-TownLightInstalledPrivateRuntime') `
    'proof must bind and fully verify the receipt-authenticated private-runtime manifest and tree.'
$candidateGate = $source.IndexOf('$result.evidence.installed_identity = Assert-InstalledCandidate', [StringComparison]::Ordinal)
$sessionGate = $source.IndexOf('$apiSession = New-TownLightProofSession', [StringComparison]::Ordinal)
$armGate = $source.IndexOf('scheduled-recordings/$recordingId/arm', [StringComparison]::Ordinal)
Assert-Contract ($candidateGate -ge 0 -and $sessionGate -gt $candidateGate -and $armGate -gt $sessionGate) `
    'installed candidate and private runtime must be verified before authentication or arm mutation.'
Assert-Contract ($source -match 'POST.*assets/ingest|''Post'' ''/assets/ingest''' -and
    $source -match 'POST.*scheduled-recordings|''Post'' ''/scheduled-recordings''' -and
    $source -match 'scheduled-recordings/\$recordingId/arm') `
    'proof must exercise authenticated ingest, scheduled-recording creation, and arm workflow.'
Assert-Contract ($source -match '/schedule/items' -and $source -match '/schedule/prepare' -and
    $source -match '/schedule/commit' -and $source -match '/schedule/commits/\$reportId') `
    'proof must commit the authoritative schedule before recording.'
Assert-Contract ($source -match 'start_capture' -and $source -match 'begin_finalization' -and
    $source -match 'finalize_success' -and $source -match 'captured_asset_hash') `
    'proof must require the complete durable capture/finalization transition chain and hash.'
Assert-Contract ($source -match '/assets/\$assetHash' -and $source -match '/catalog/assets/\$assetHash' -and
    $source -match "editorial_state -cne 'draft'") `
    'proof must verify the finalized media asset and generated Draft catalog record.'
Assert-Contract ($source -match 'proof-owned channel' -or $source -match 'proofChannel') `
    'proof must isolate its channel identity and disable it during cleanup.'
Assert-Contract ($source -notmatch '(?i)\b(Start-Process|Restart-Service|Stop-Process|taskkill|sc\.exe)\b') `
    'proof must not launch or control processes/services.'
Assert-Contract ($source -notmatch '(?i)\b(Invoke-WebRequest|curl\.exe)\b' -and
    $source -notmatch '(?i)ConvertTo-SecureString.*-AsPlainText|\$env:[A-Za-z0-9_]*(?:PASSWORD|CREDENTIAL|SECRET)|-Password\b') `
    'proof must not bypass the authenticated helper or serialize credentials.'
Assert-Contract ($source -notmatch '(?i)\b(SQLiteConnection|sqlite|station\.db|database\.write|ReadAllText.*ProgramData)\b') `
    'proof must not read or write the station database directly.'
$topLevelParameters = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($parameter in $ast.ParamBlock.Parameters) { $null = $topLevelParameters.Add($parameter.Name.VariablePath.UserPath) }
foreach ($hostileResultPath in @(
        'C:\ProgramData\TownLight Station\station.db',
        'C:\ProgramData\TownLight Station\installed-proof-state.json',
        (Join-Path $PSScriptRoot 'installed-proof-api.ps1')
    )) {
    Assert-Contract (-not $topLevelParameters.Contains('ResultPath')) `
        "hostile ResultPath '$hostileResultPath' must have no accepted parameter binding."
}
Assert-Contract ($source -notmatch '(?i)\b(Set-Content|Out-File|Export-Clixml|Tee-Object)\b|\[IO\.File\]::Write') `
    'proof receipts must remain stdout-only and must not have a direct filesystem write primitive.'

# Exercise the exact shared verifier with a same-size substituted DLL and an
# injected extra file. These are harness-owned temp fixtures; no installed tree
# or service is read or mutated.
Assert-Contract (Test-Path -LiteralPath $runtimeVerifierPath -PathType Leaf) `
    'shared installed private-runtime verifier must exist.'
. $runtimeVerifierPath
$temporaryBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
$runtimeFixtureRoot = [IO.Path]::GetFullPath((Join-Path $temporaryBase ("townlight-s21-runtime-hostile-" + [guid]::NewGuid().ToString('N'))))
Assert-Contract ($runtimeFixtureRoot.StartsWith($temporaryBase, [StringComparison]::OrdinalIgnoreCase)) `
    'runtime hostile fixture must remain inside the operating-system temp root.'
try {
    $runtimeBin = Join-Path $runtimeFixtureRoot 'bin'
    New-Item -ItemType Directory -Path $runtimeBin -Force | Out-Null
    $runtimeFile = Join-Path $runtimeBin 'runtime.dll'
    [IO.File]::WriteAllBytes($runtimeFile, [byte[]](1, 2, 3, 4))
    $runtimeManifest = [pscustomobject]@{ files = @([pscustomobject]@{
                path = 'bin/runtime.dll'; size_bytes = [int64]4
                sha256 = (Get-FileHash -LiteralPath $runtimeFile -Algorithm SHA256).Hash.ToLowerInvariant()
            }) }
    $verified = Assert-TownLightInstalledPrivateRuntime -RuntimeRoot $runtimeFixtureRoot -RuntimeManifest $runtimeManifest
    Assert-Contract ($verified.file_count -eq 1) 'exact private-runtime fixture must pass before substitution.'

    [IO.File]::WriteAllBytes($runtimeFile, [byte[]](4, 3, 2, 1))
    $substitutionFailure = ''
    try { Assert-TownLightInstalledPrivateRuntime -RuntimeRoot $runtimeFixtureRoot -RuntimeManifest $runtimeManifest | Out-Null }
    catch { $substitutionFailure = $_.Exception.Message }
    Assert-Contract ($substitutionFailure -match 'private_runtime_file_mismatch') `
        'same-size private-runtime content substitution must fail by hash.'

    [IO.File]::WriteAllBytes($runtimeFile, [byte[]](1, 2, 3, 4))
    [IO.File]::WriteAllBytes((Join-Path $runtimeBin 'injected.dll'), [byte[]](9))
    $injectionFailure = ''
    try { Assert-TownLightInstalledPrivateRuntime -RuntimeRoot $runtimeFixtureRoot -RuntimeManifest $runtimeManifest | Out-Null }
    catch { $injectionFailure = $_.Exception.Message }
    Assert-Contract ($injectionFailure -match 'private_runtime_file_set_mismatch') `
        'an unmanifested private-runtime file injection must fail the exact file-set check.'
}
finally {
    if ($runtimeFixtureRoot.StartsWith($temporaryBase, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path $runtimeFixtureRoot -Leaf) -like 'townlight-s21-runtime-hostile-*') {
        Remove-Item -LiteralPath $runtimeFixtureRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
Assert-Contract (Test-Path -LiteralPath $documentationPath -PathType Leaf) 'proof boundary documentation must exist.'
$documentation = Get-Content -LiteralPath $documentationPath -Raw
Assert-Contract ($documentation -match '\$credential\s*=\s*Get-Credential' -and
    $documentation -match '-Credential\s+\$credential' -and
    $documentation -match 'does not claim field proof') `
    'documentation must show in-memory credential invocation and truthful field boundary.'

Write-Output "Installed scheduled-recording proof harness passed ($assertions assertions; no machine mutation)."
