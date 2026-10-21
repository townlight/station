[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 1800
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'candidate-pair-verifier.ps1')
$repository = Split-Path $PSScriptRoot -Parent
$configuration = Join-Path $repository 'harness\windows-sandbox\configs\caption-proof.wsb'
$evidenceRoot = Join-Path $env:ProgramData 'TownLight Station\SandboxEvidence'
$inputRoot = Join-Path $env:ProgramData 'TownLight Station\SandboxInputs'
$fixturePath = Join-Path $inputRoot 'caption-source.ts'
$manifestPath = Join-Path $repository 'dist\candidate-manifest.json'
$installerPath = Join-Path $repository 'dist\TownLight-Station-0.1.0-x64-setup.exe'
$guard = Join-Path $PSScriptRoot 'invoke-owned-windows-sandbox.ps1'
$fixtureSha256 = '4fb0c4cdc9a6b0ab7709b4ceb3b1ff4ce02c21e43d12cb84bc23bc1922de7c93'
$fixtureSize = 33985512

foreach ($required in @($configuration, $manifestPath, $installerPath, $guard, $fixturePath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Owned caption proof input is missing: $required"
    }
}
if ($TimeoutSeconds -lt 300 -or $TimeoutSeconds -gt 3600) {
    throw 'Owned caption proof timeout must be between 300 and 3600 seconds.'
}
$fixture = Get-Item -LiteralPath $fixturePath
$observedFixtureHash = (Get-FileHash -LiteralPath $fixturePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($fixture.Length -ne $fixtureSize -or $observedFixtureHash -ne $fixtureSha256) {
    throw 'Owned caption proof fixture does not match the exact retained Townlight media input.'
}

$head = (& git -C $repository rev-parse HEAD).Trim()
$dirty = & git -C $repository status --porcelain
if ($LASTEXITCODE -ne 0 -or $head -notmatch '^[0-9a-f]{40}$' -or $dirty) {
    throw 'Owned caption proof requires one clean immutable source commit.'
}
$candidate = Get-TownLightCandidateIdentity -ManifestPath $manifestPath -ExpectedSourceCommit $head
$manifest = $candidate.manifest
$installerHash = $candidate.installer_sha256

[void][IO.Directory]::CreateDirectory($evidenceRoot)
$existing = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
Get-ChildItem -LiteralPath $evidenceRoot -Filter 'caption-proof-*.json' -File -ErrorAction SilentlyContinue |
    ForEach-Object { [void]$existing.Add($_.FullName) }

$launch = & $guard -ConfigurationPath $configuration
if ($launch.status -ne 'launched' -or $launch.owner_namespace -ne 'townlight-station-codex') {
    throw 'The shared ownership guard did not return exact Townlight launch authority.'
}

# Wait for guest-authored caption evidence without terminating the Sandbox host.
$deadline = [datetime]::UtcNow.AddSeconds($TimeoutSeconds)
do {
    $candidate = Get-ChildItem -LiteralPath $evidenceRoot -Filter 'caption-proof-*.json' -File |
        Where-Object { -not $existing.Contains($_.FullName) } |
        Sort-Object LastWriteTimeUtc |
        Select-Object -First 1
    if ($candidate) {
        $evidence = Get-Content -LiteralPath $candidate.FullName -Raw | ConvertFrom-Json
        if ($evidence.schema_version -ne 1 -or
            $evidence.status -ne 'passed' -or
            $evidence.source_commit -ne $head -or
            $evidence.installer_sha256 -ne $installerHash -or
            $evidence.fixture_sha256 -ne $fixtureSha256 -or
            $evidence.verifier.source_commit -ne $head -or
            $evidence.caption_job.state -ne 'completed' -or
            $evidence.caption_proof.outcome -ne 'verified' -or
            $evidence.caption_proof.source_commit -ne $head -or
            $evidence.caption_proof.decoder_engine -ne "ffmpeg-sha256=$($manifest.caption_decoder_sha256)" -or
            $evidence.caption_proof.expected_text_sha256 -ne $evidence.caption_proof.decoded_text_sha256 -or
            $evidence.caption_proof.expected_cue_count -ne $evidence.caption_proof.decoded_cue_count) {
            throw "Caption-proof Sandbox evidence failed or mismatched: $($evidence | ConvertTo-Json -Depth 12 -Compress)"
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

throw "Timed out waiting for Townlight-owned caption proof evidence after $TimeoutSeconds seconds; the Sandbox host was left untouched."
