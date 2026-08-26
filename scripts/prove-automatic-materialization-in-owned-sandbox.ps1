[CmdletBinding()]
param(
    [ValidateRange(300, 3600)]
    [int]$TimeoutSeconds = 1800
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'candidate-pair-verifier.ps1')

$repository = Split-Path $PSScriptRoot -Parent
$configuration = Join-Path $repository 'harness\windows-sandbox\configs\automatic-materialization-proof.wsb'
$evidenceRoot = Join-Path $env:ProgramData 'TownLight Station\SandboxEvidence'
$inputRoot = Join-Path $env:ProgramData 'TownLight Station\SandboxInputs'
$challengeRoot = Join-Path $env:ProgramData 'TownLight Station\SandboxChallenges'
$fixtureName = 'caption-source.ts'
$fixturePath = Join-Path $inputRoot $fixtureName
$fixtureSize = [int64]33985512
$fixtureSha256 = '4fb0c4cdc9a6b0ab7709b4ceb3b1ff4ce02c21e43d12cb84bc23bc1922de7c93'
$manifestPath = Join-Path $repository 'dist\candidate-manifest.json'
$installerPath = Join-Path $repository 'dist\TownLight-Station-0.1.0-x64-setup.exe'
$guard = Join-Path $PSScriptRoot 'invoke-owned-windows-sandbox.ps1'
$guestEvidenceFilter = 'automatic-materialization-guest-*.json'
$hostFinalReceiptFilter = 'automatic-materialization-host-final-*.json'
$challengeFilter = 'automatic-materialization-challenge-*.json'

function Get-CanonicalPath([string]$Path) {
    [IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Assert-ExactSandboxMappings {
    param(
        [Parameter(Mandatory = $true)][string]$ConfigurationPath,
        [Parameter(Mandatory = $true)][string]$Repository
    )

    try { [xml]$configurationDocument = Get-Content -LiteralPath $ConfigurationPath -Raw -ErrorAction Stop }
    catch { throw 'Owned automatic-materialization proof configuration is not valid XML.' }
    $mappings = @($configurationDocument.Configuration.MappedFolders.MappedFolder)
    $expectedReadOnlyMappings = [ordered]@{
        'C:\TownLightCandidate' = (Join-Path $Repository 'dist')
        'C:\TownLightVerifier' = (Join-Path $Repository 'installer')
        'C:\TownLightInputs' = $inputRoot
        'C:\TownLightChallenge' = $challengeRoot
        'C:\TownLightHarness' = (Join-Path $Repository 'harness\windows-sandbox')
        'C:\TownLightShared' = (Join-Path $Repository 'scripts')
    }
    if ($mappings.Count -ne ($expectedReadOnlyMappings.Count + 1)) {
        throw 'Owned automatic-materialization proof configuration has an unexpected mapping count.'
    }
    foreach ($expected in $expectedReadOnlyMappings.GetEnumerator()) {
        $matches = @($mappings | Where-Object {
                [string]$_.SandboxFolder -ceq $expected.Key -and
                [string]$_.ReadOnly -ceq 'true' -and
                (Get-CanonicalPath ([string]$_.HostFolder)) -ieq (Get-CanonicalPath ([string]$expected.Value))
            })
        if ($matches.Count -ne 1) {
            throw "Owned automatic-materialization proof configuration must bind exactly $($expected.Key) to this repository."
        }
    }
    $writable = @($mappings | Where-Object { [string]$_.ReadOnly -ceq 'false' })
    if ($writable.Count -ne 1 -or [string]$writable[0].SandboxFolder -cne 'C:\TownLightEvidence' -or
        (Get-CanonicalPath ([string]$writable[0].HostFolder)) -ine (Get-CanonicalPath $evidenceRoot)) {
        throw 'Owned automatic-materialization proof configuration has an unexpected writable evidence mapping.'
    }
}

function Assert-OuterHostAttestedProvenance {
    param(
        [Parameter(Mandatory = $true)]$Evidence,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceCommit,
        [Parameter(Mandatory = $true)][string]$ExpectedManifestSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedInstallerSha256
    )

    if ($evidence.status -ne 'outer_host_attested' -or
        $evidence.installed_proof.status -ne 'outer_host_attested') {
        throw 'Automatic-materialization guest evidence must remain a provisional outer-host attestation.'
    }
    foreach ($provenance in @($evidence.candidate_provenance, $evidence.installed_proof.candidate_provenance)) {
        if ($null -eq $provenance -or
            $provenance.mode -isnot [string] -or
            $provenance.standalone -isnot [bool] -or
            $provenance.host_upgrade_required -isnot [bool] -or
            $provenance.expected_source_commit -isnot [string] -or
            $provenance.candidate_manifest_sha256 -isnot [string] -or
            $provenance.installer_sha256 -isnot [string] -or
            $provenance.mode -cne 'outer_host_attested' -or
            $provenance.standalone -ne $false -or
            $provenance.host_upgrade_required -ne $true -or
            $provenance.expected_source_commit -cne $ExpectedSourceCommit -or
            $provenance.candidate_manifest_sha256 -cne $ExpectedManifestSha256 -or
            $provenance.installer_sha256 -cne $ExpectedInstallerSha256) {
            throw 'Automatic-materialization guest candidate provenance does not match the host-attested candidate.'
        }
    }
}

function New-HostAttestedProvenance {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedSourceCommit,
        [Parameter(Mandatory = $true)][string]$ManifestSha256,
        [Parameter(Mandatory = $true)][string]$InstallerSha256
    )

    [ordered]@{
        mode = 'outer_host_attested'
        standalone = $false
        host_upgrade_required = $true
        expected_source_commit = $ExpectedSourceCommit
        candidate_manifest_sha256 = $ManifestSha256
        installer_sha256 = $InstallerSha256
    }
}

function Write-AtomicHostFinalReceipt {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][Collections.IDictionary]$Receipt
    )

    $partPath = "$Path.part-$([guid]::NewGuid().ToString('N'))"
    $json = ($Receipt | ConvertTo-Json -Depth 24) + [Environment]::NewLine
    try {
        [IO.File]::WriteAllText($partPath, $json, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $partPath -Destination $Path -ErrorAction Stop
    }
    finally {
        if (Test-Path -LiteralPath $partPath -PathType Leaf) {
            Remove-Item -LiteralPath $partPath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-ObservedSandboxProcesses {
    $names = @('WindowsSandbox', 'WindowsSandboxClient', 'WindowsSandboxRemoteSession', 'WindowsSandboxServer')
    @(Get-Process -ErrorAction Stop | Where-Object { $_.ProcessName -in $names } | ForEach-Object {
        [pscustomobject]@{
            process_id = [int]$_.Id
            process_name = [string]$_.ProcessName
            creation_time = $_.StartTime.ToUniversalTime().ToString('o')
        }
    })
}

function Wait-ForObservedSandboxExit {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][array]$Processes,
        [ValidateRange(5, 120)][int]$TimeoutSeconds = 60
    )

    if ($Processes.Count -eq 0) {
        throw 'Owned automatic-materialization Sandbox process snapshot was empty before shutdown observation.'
    }
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $stillRunning = 0
        foreach ($expected in $Processes) {
            try {
                $process = Get-Process -Id ([int]$expected.process_id) -ErrorAction Stop
                if ($process.ProcessName -ceq [string]$expected.process_name -and
                    $process.StartTime.ToUniversalTime().ToString('o') -ceq [string]$expected.creation_time) {
                    $stillRunning++
                }
            }
            catch {
                if ([string]$_.FullyQualifiedErrorId -notlike 'NoProcessFoundForGivenId*') { throw }
            }
        }
        if ($stillRunning -eq 0) { return }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Owned automatic-materialization Sandbox did not exit after publishing evidence; it was left untouched.'
}

foreach ($required in @($configuration, $manifestPath, $installerPath, $guard, $fixturePath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Owned automatic-materialization proof input is missing: $required"
    }
}
Assert-ExactSandboxMappings -ConfigurationPath $configuration -Repository $repository
$fixture = Get-Item -LiteralPath $fixturePath
$observedFixtureSha256 = (Get-FileHash -LiteralPath $fixturePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($fixture.Length -ne $fixtureSize -or $observedFixtureSha256 -ne $fixtureSha256) {
    throw 'Owned automatic-materialization proof fixture does not match the exact retained caption source.'
}

$head = (& git -C $repository rev-parse HEAD).Trim()
$dirty = @(& git -C $repository status --porcelain --untracked-files=all)
if ($LASTEXITCODE -ne 0 -or $head -notmatch '^[0-9a-f]{40}$' -or $dirty.Count -ne 0) {
    throw 'Owned automatic-materialization proof requires one clean immutable source commit.'
}
$candidate = Get-TownLightCandidateIdentity -ManifestPath $manifestPath -ExpectedSourceCommit $head
$manifestSha256 = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
$installerSha256 = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($candidate.installer_sha256 -cne $installerSha256 -or
    (Get-CanonicalPath $candidate.installer_path) -ine (Get-CanonicalPath $installerPath)) {
    throw 'Owned automatic-materialization proof installer rehash does not match the exact candidate pair.'
}
$candidateAfterRehash = Get-TownLightCandidateIdentity -ManifestPath $manifestPath -ExpectedSourceCommit $head
$manifestSha256After = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
$installerSha256After = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($manifestSha256After -cne $manifestSha256 -or $installerSha256After -cne $installerSha256 -or
    $candidateAfterRehash.installer_sha256 -cne $installerSha256) {
    throw 'Owned automatic-materialization candidate changed while the host was rehashing it.'
}

[void][IO.Directory]::CreateDirectory($evidenceRoot)
[void][IO.Directory]::CreateDirectory($challengeRoot)
$existingGuestEvidence = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
Get-ChildItem -LiteralPath $evidenceRoot -Filter $guestEvidenceFilter -File -ErrorAction SilentlyContinue |
    ForEach-Object { [void]$existingGuestEvidence.Add($_.FullName) }

$challengeId = [guid]::NewGuid().ToString('D')
$challengePath = Join-Path $challengeRoot "automatic-materialization-challenge-$challengeId.json"
if ((Split-Path $challengePath -Leaf) -notlike $challengeFilter) {
    throw 'Automatic-materialization launch challenge filename is invalid.'
}
$challenge = [ordered]@{
    schema_version = 1
    status = 'pending'
    challenge_id = $challengeId
    created_at_utc = [DateTime]::UtcNow.ToString('o')
    source_commit = $head
    candidate_manifest_sha256 = $manifestSha256
    installer_sha256 = $installerSha256
    fixture_name = $fixtureName
    fixture_size = $fixtureSize
    fixture_sha256 = $fixtureSha256
}
Write-AtomicHostFinalReceipt -Path $challengePath -Receipt $challenge
$challengeSha256 = (Get-FileHash -LiteralPath $challengePath -Algorithm SHA256).Hash.ToLowerInvariant()

$launch = & $guard -ConfigurationPath $configuration
if ($launch.status -ne 'launched' -or $launch.owner_namespace -ne 'townlight-station-codex') {
    throw 'The shared ownership guard did not return exact TownLight launch authority.'
}

# Wait for guest-authored provisional evidence without terminating the Sandbox host.
$deadline = [datetime]::UtcNow.AddSeconds($TimeoutSeconds)
do {
    $guestEvidencePath = Get-ChildItem -LiteralPath $evidenceRoot -Filter $guestEvidenceFilter -File |
        Where-Object { -not $existingGuestEvidence.Contains($_.FullName) } |
        Sort-Object LastWriteTimeUtc |
        Select-Object -First 1
    if ($guestEvidencePath) {
        try { $evidence = Get-Content -LiteralPath $guestEvidencePath.FullName -Raw | ConvertFrom-Json -ErrorAction Stop }
        catch { throw "Automatic-materialization Sandbox guest evidence is not valid JSON: $($guestEvidencePath.FullName)" }
        $guestEvidenceSha256 = (Get-FileHash -LiteralPath $guestEvidencePath.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($evidence.schema_version -ne 2 -or
            $evidence.proof -cne 'automatic-materialization-owned-sandbox-guest' -or
            [string]$evidence.run_id -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' -or
            $evidence.challenge_id -cne $challengeId -or
            $evidence.challenge_sha256 -cne $challengeSha256 -or
            $evidence.source_commit -cne $head -or
            $evidence.installer_sha256 -cne $installerSha256 -or
            $evidence.fixture_name -cne $fixtureName -or
            $evidence.fixture_size -ne $fixtureSize -or
            $evidence.fixture_sha256 -cne $fixtureSha256 -or
            $evidence.verifier.source_commit -cne $head -or
            $evidence.installed_proof.proof -cne 'installed-automatic-materialization' -or
            $evidence.installed_proof.source_commit -cne $head -or
            $evidence.automatic_run.origin -cne 'automatic' -or
            $evidence.automatic_run.actor_id -cne '00000000-0000-4000-8000-000000000022' -or
            [string]$evidence.automatic_run.run_id -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' -or
            [string]$evidence.automatic_run.request_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            [string]$evidence.automatic_run.result_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            ($evidence.automatic_run.checkpoint_utc_day -isnot [int32] -and
                $evidence.automatic_run.checkpoint_utc_day -isnot [int64]) -or
            @($evidence.automatic_run.generated_draft_ids).Count -eq 0 -or
            $evidence.installed_proof.observations.preflight.automatic_run.run_id -cne $evidence.automatic_run.run_id -or
            $evidence.installed_proof.observations.post_restart.automatic_run.run_id -cne $evidence.automatic_run.run_id -or
            [int64]$evidence.installed_proof.observations.preflight.current_utc_day -ne [int64]$evidence.automatic_run.checkpoint_utc_day -or
            [int64]$evidence.installed_proof.observations.post_restart.current_utc_day -ne [int64]$evidence.automatic_run.checkpoint_utc_day -or
            $evidence.installed_proof.observations.preflight.automatic_run.request_sha256 -cne $evidence.automatic_run.request_sha256 -or
            $evidence.installed_proof.observations.preflight.automatic_run.result_sha256 -cne $evidence.automatic_run.result_sha256 -or
            (@($evidence.installed_proof.observations.preflight.automatic_run.generated_draft_ids) -join '|') -cne
                (@($evidence.automatic_run.generated_draft_ids) -join '|') -or
            $evidence.installed_proof.restart_boundary.attempted -isnot [bool] -or
            $evidence.installed_proof.restart_boundary.stop_issued -isnot [bool] -or
            $evidence.installed_proof.restart_boundary.start_issued -isnot [bool] -or
            $evidence.installed_proof.restart_boundary.updater_controlled -isnot [bool] -or
            $evidence.installed_proof.restart_boundary.attempted -ne $true -or
            $evidence.installed_proof.restart_boundary.stop_issued -ne $true -or
            $evidence.installed_proof.restart_boundary.start_issued -ne $true -or
            $evidence.installed_proof.restart_boundary.updater_controlled -ne $false) {
            throw "Automatic-materialization Sandbox guest evidence failed or mismatched: $($guestEvidencePath.FullName)"
        }
        $guestDraftIds = @($evidence.automatic_run.generated_draft_ids | ForEach-Object { [string]$_ })
        if ($guestDraftIds.Count -gt 128 -or
            @($guestDraftIds | Where-Object {
                $_ -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            }).Count -ne 0 -or
            @($guestDraftIds | Sort-Object -Unique).Count -ne $guestDraftIds.Count) {
            throw 'Automatic-materialization Sandbox guest Draft identities are invalid or duplicated.'
        }
        Assert-OuterHostAttestedProvenance -Evidence $evidence -ExpectedSourceCommit $head `
            -ExpectedManifestSha256 $manifestSha256 -ExpectedInstallerSha256 $installerSha256

        $challengeSha256After = (Get-FileHash -LiteralPath $challengePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($challengeSha256After -cne $challengeSha256) {
            throw 'Owned automatic-materialization launch challenge changed during the guest proof.'
        }
        $observedSandboxProcesses = @(Get-ObservedSandboxProcesses)
        Wait-ForObservedSandboxExit -Processes $observedSandboxProcesses -TimeoutSeconds 60

        $finalCandidate = Get-TownLightCandidateIdentity -ManifestPath $manifestPath -ExpectedSourceCommit $head
        $finalManifestSha256 = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $finalInstallerSha256 = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($finalManifestSha256 -cne $manifestSha256 -or
            $finalInstallerSha256 -cne $installerSha256 -or
            $finalCandidate.installer_sha256 -cne $installerSha256) {
            throw 'Owned automatic-materialization candidate changed before final host attestation.'
        }

        $completedAt = [datetime]::UtcNow
        $hostReceiptFileName = "automatic-materialization-host-final-$($completedAt.ToString('yyyyMMddTHHmmssZ'))-$([guid]::NewGuid().ToString('D')).json"
        if ($hostReceiptFileName -notlike $hostFinalReceiptFilter) {
            throw 'Automatic-materialization host final receipt filename does not match its dedicated filter.'
        }
        $hostReceiptPath = Join-Path $evidenceRoot $hostReceiptFileName
        $hostReceipt = [ordered]@{
            schema_version = 1
            proof = 'automatic-materialization-owned-sandbox-host-final'
            status = 'passed'
            completed_at_utc = $completedAt.ToString('o')
            source_commit = $head
            provenance = [ordered]@{
                mode = 'outer_host_authenticated'
                trust_boundary = 'signed_in_windows_user_and_guarded_sandbox'
                challenge_id = $challengeId
                challenge_sha256 = $challengeSha256
                observed_sandbox_process_count = $observedSandboxProcesses.Count
                source_commit = $head
                candidate_manifest_sha256 = $manifestSha256
                installer_sha256 = $installerSha256
                guest_evidence_sha256 = $guestEvidenceSha256
            }
            candidate = [ordered]@{
                manifest_path = $manifestPath
                manifest_sha256 = $manifestSha256
                installer_path = $installerPath
                installer_sha256 = $installerSha256
            }
            fixture = [ordered]@{
                name = $fixtureName
                size_bytes = $fixtureSize
                sha256 = $fixtureSha256
            }
            guest_evidence = [ordered]@{
                path = $guestEvidencePath.FullName
                sha256 = $guestEvidenceSha256
            }
            provisional_child_receipt = [ordered]@{
                schema_version = $evidence.schema_version
                challenge_id = $evidence.challenge_id
                status = $evidence.status
                source_commit = $evidence.source_commit
                installer_sha256 = $evidence.installer_sha256
                fixture_name = $evidence.fixture_name
                fixture_size = $evidence.fixture_size
                fixture_sha256 = $evidence.fixture_sha256
                automatic_run = [ordered]@{
                    run_id = $evidence.automatic_run.run_id
                    origin = $evidence.automatic_run.origin
                    actor_id = $evidence.automatic_run.actor_id
                    checkpoint_utc_day = $evidence.automatic_run.checkpoint_utc_day
                    request_sha256 = $evidence.automatic_run.request_sha256
                    result_sha256 = $evidence.automatic_run.result_sha256
                    generated_draft_ids = @($evidence.automatic_run.generated_draft_ids)
                }
                candidate_provenance = New-HostAttestedProvenance -ExpectedSourceCommit $head `
                    -ManifestSha256 $manifestSha256 -InstallerSha256 $installerSha256
                installed_proof = [ordered]@{
                    proof = $evidence.installed_proof.proof
                    status = $evidence.installed_proof.status
                    source_commit = $evidence.installed_proof.source_commit
                    restart_boundary = [ordered]@{
                        attempted = $evidence.installed_proof.restart_boundary.attempted
                        stop_issued = $evidence.installed_proof.restart_boundary.stop_issued
                        start_issued = $evidence.installed_proof.restart_boundary.start_issued
                        updater_controlled = $evidence.installed_proof.restart_boundary.updater_controlled
                    }
                    candidate_provenance = New-HostAttestedProvenance -ExpectedSourceCommit $head `
                        -ManifestSha256 $manifestSha256 -InstallerSha256 $installerSha256
                }
            }
        }
        Write-AtomicHostFinalReceipt -Path $hostReceiptPath -Receipt $hostReceipt
        [pscustomobject]@{
            launch = $launch
            guest_evidence_path = $guestEvidencePath.FullName
            host_final_receipt_path = $hostReceiptPath
            receipt = $hostReceipt
        }
        return
    }
    Start-Sleep -Milliseconds 500
} while ([datetime]::UtcNow -lt $deadline)

throw "Timed out waiting for TownLight-owned automatic-materialization provisional evidence after $TimeoutSeconds seconds; the Sandbox host was left untouched."
