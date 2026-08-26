[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 1200
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'candidate-pair-verifier.ps1')
$repository = Split-Path $PSScriptRoot -Parent
$configuration = Join-Path $repository 'harness\windows-sandbox\configs\clean-install.wsb'
$evidenceRoot = Join-Path $env:ProgramData 'TownLight Station\SandboxEvidence'
$manifestPath = Join-Path $repository 'dist\candidate-manifest.json'
$installerPath = Join-Path $repository 'dist\TownLight-Station-0.1.0-x64-setup.exe'
$guard = Join-Path $PSScriptRoot 'invoke-owned-windows-sandbox.ps1'

foreach ($required in @($configuration, $manifestPath, $installerPath, $guard)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Owned clean-install proof input is missing: $required"
    }
}
if ($TimeoutSeconds -lt 60 -or $TimeoutSeconds -gt 3600) {
    throw 'Owned clean-install proof timeout must be between 60 and 3600 seconds.'
}

$head = (& git -C $repository rev-parse HEAD).Trim()
$dirty = & git -C $repository status --porcelain
if ($LASTEXITCODE -ne 0 -or $head -notmatch '^[0-9a-f]{40}$' -or $dirty) {
    throw 'Owned clean-install proof requires one clean immutable source commit.'
}
$candidate = Get-TownLightCandidateIdentity -ManifestPath $manifestPath -ExpectedSourceCommit $head
$manifest = $candidate.manifest
$installerHash = $candidate.installer_sha256

[void][IO.Directory]::CreateDirectory($evidenceRoot)
$existing = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
Get-ChildItem -LiteralPath $evidenceRoot -Filter 'clean-install-*.json' -File -ErrorAction SilentlyContinue |
    ForEach-Object { [void]$existing.Add($_.FullName) }

$launch = & $guard -ConfigurationPath $configuration
if ($launch.status -ne 'launched' -or $launch.owner_namespace -ne 'townlight-station-codex') {
    throw 'The shared ownership guard did not return exact TownLight launch authority.'
}

# Wait for the guest-authored evidence instead of terminating the Sandbox host.
$deadline = [datetime]::UtcNow.AddSeconds($TimeoutSeconds)
do {
    $candidate = Get-ChildItem -LiteralPath $evidenceRoot -Filter 'clean-install-*.json' -File |
        Where-Object { -not $existing.Contains($_.FullName) } |
        Sort-Object LastWriteTimeUtc |
        Select-Object -First 1
    if ($candidate) {
        $evidence = Get-Content -LiteralPath $candidate.FullName -Raw | ConvertFrom-Json
        if ($evidence.schema_version -ne 1 -or
            $evidence.status -ne 'passed' -or
            $evidence.source_commit -ne $head -or
            $evidence.installer_sha256 -ne $installerHash -or
            $evidence.verifier.source_commit -ne $head -or
            $evidence.verifier.caption_decoder_sha256 -ne $manifest.caption_decoder_sha256) {
            throw "Clean-install Sandbox evidence failed or mismatched: $($evidence | ConvertTo-Json -Depth 8 -Compress)"
        }
        [pscustomobject]@{
            launch = $launch
            evidence_path = $candidate.FullName
            evidence = $evidence
        }
        return
    }
    Start-Sleep -Milliseconds 500
} while ([datetime]::UtcNow -lt $deadline)

throw "Timed out waiting for TownLight-owned clean-install evidence after $TimeoutSeconds seconds; the Sandbox host was left untouched."
