param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. 'C:\TownLightShared\candidate-pair-verifier.ps1'
. 'C:\TownLightShared\installed-proof-api.ps1'

$candidateRoot = 'C:\TownLightCandidate'
$evidenceRoot = 'C:\TownLightEvidence'
$inputPath = 'C:\TownLightInputs\caption-source.ts'
$challengeRoot = 'C:\TownLightChallenge'
$manifestPath = Join-Path $candidateRoot 'candidate-manifest.json'
$installerPath = Join-Path $candidateRoot 'TownLight-Station-0.1.0-x64-setup.exe'
$verifierPath = 'C:\TownLightVerifier\verify-installed.ps1'
$installedProofPath = 'C:\TownLightShared\prove-installed-automatic-materialization.ps1'
$fixtureName = 'caption-source.ts'
$fixtureSize = [int64]33985512
$fixtureSha256 = '4fb0c4cdc9a6b0ab7709b4ceb3b1ff4ce02c21e43d12cb84bc23bc1922de7c93'
$automaticActorId = '00000000-0000-4000-8000-000000000022'
$baseUri = [uri]'http://127.0.0.1:4070'
$runId = [guid]::NewGuid().ToString('D')
$startedAt = [DateTime]::UtcNow
$resultPath = Join-Path $evidenceRoot (
    "automatic-materialization-guest-$($startedAt.ToString('yyyyMMddTHHmmssZ'))-$runId.json"
)
$partPath = "$resultPath.part"
$installedResultPath = Join-Path $evidenceRoot (
    "installed-automatic-materialization-child-$($startedAt.ToString('yyyyMMddTHHmmssZ'))-$runId.json"
)
$installerLog = Join-Path $evidenceRoot "automatic-materialization-$runId-installer.log"
$stage = 'preflight'
$apiSession = $null
$proofCredential = $null
$result = [ordered]@{
    schema_version = 2
    proof = 'automatic-materialization-owned-sandbox-guest'
    run_id = $runId
    status = 'failed'
    started_at_utc = $startedAt.ToString('o')
    completed_at_utc = $null
    challenge_id = $null
    challenge_sha256 = $null
    source_commit = $null
    installer_sha256 = $null
    fixture_name = $fixtureName
    fixture_size = $fixtureSize
    fixture_sha256 = $fixtureSha256
    candidate_provenance = $null
    verifier = $null
    automatic_run = $null
    installed_proof = $null
    failure_stage = $null
    error_code = $null
}

function Assert-ElevatedGuest {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'guest_not_elevated'
    }
}

function New-RandomProofCredential {
    $bytes = [byte[]]::new(24)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) }
    finally { $generator.Dispose() }
    $plainText = ([Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')) + '!aA1'
    $secure = ConvertTo-SecureString -String $plainText -AsPlainText -Force
    [pscustomobject]@{
        PlainText = $plainText
        Credential = [Management.Automation.PSCredential]::new('sandbox-admin', $secure)
    }
}

function Invoke-ProofApi {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('Get', 'Post', 'Put')]
        [string]$Method,
        [Parameter(Mandatory = $true)]
        [string]$Path,
        $Body = $null,
        [ValidateRange(1, 900)]
        [int]$TimeoutSec = 30
    )
    Invoke-TownLightProofApi -Session $script:apiSession -Method $Method -Path $Path `
        -Body $Body -TimeoutSec $TimeoutSec
}

function Wait-ForProofState {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Action,
        [Parameter(Mandatory = $true)][scriptblock]$Predicate,
        [Parameter(Mandatory = $true)][string]$Description,
        [ValidateRange(1, 900)][int]$Seconds = 180
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $value = & $Action
        if (& $Predicate $value) { return $value }
        Start-Sleep -Milliseconds 500
    }
    throw "timeout_$Description"
}

function Get-CurrentUtcDay {
    [int64][Math]::Floor([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 86400000.0)
}

function Invoke-FreshBootstrap {
    param([Parameter(Mandatory = $true)]$CredentialBundle)

    $status = Invoke-RestMethod -Method Get -Uri "$($baseUri.AbsoluteUri.TrimEnd('/'))/api/v1/auth/status" `
        -TimeoutSec 15
    if ($status.state -cne 'bootstrap_required') { throw 'bootstrap_not_required' }

    $handoffPath = 'C:\ProgramData\TownLight Station\operator-bootstrap.capability'
    if (-not (Test-Path -LiteralPath $handoffPath -PathType Leaf)) { throw 'bootstrap_capability_missing' }
    $rawCapability = [IO.File]::ReadAllText($handoffPath, [Text.UTF8Encoding]::new($false))
    if ($rawCapability -notmatch '\A[A-Za-z0-9_-]{43}\n\z') { throw 'bootstrap_capability_invalid' }
    $capability = $rawCapability.Substring(0, 43)
    $bootstrapJson = $null
    $bootstrapResponse = $null
    try {
        $bootstrapJson = [ordered]@{
            capability = $capability
            username = $CredentialBundle.Credential.UserName
            display_name = 'Sandbox Setup Admin'
            password = $CredentialBundle.PlainText
        } | ConvertTo-Json -Compress
        $bootstrapResponse = Invoke-WebRequest -Method Post `
            -Uri "$($baseUri.AbsoluteUri.TrimEnd('/'))/api/v1/auth/bootstrap" `
            -Headers @{ Origin = $baseUri.GetLeftPart([UriPartial]::Authority); 'Sec-Fetch-Site' = 'same-origin' } `
            -ContentType 'application/json' -Body $bootstrapJson -TimeoutSec 30 `
            -MaximumRedirection 0 -UseBasicParsing -ErrorAction Stop
        $document = $bootstrapResponse.Content | ConvertFrom-Json -ErrorAction Stop
        if ($bootstrapResponse.StatusCode -ne 201 -or
            $document.session.username -cne $CredentialBundle.Credential.UserName -or
            @($document.session.roles | Where-Object { $_ -ceq 'setup_admin' }).Count -ne 1) {
            throw 'bootstrap_identity_invalid'
        }
        Remove-Item -LiteralPath $handoffPath -Force -ErrorAction Stop
    }
    catch {
        throw 'bootstrap_request_failed'
    }
    finally {
        $bootstrapJson = $null
        $bootstrapResponse = $null
        $capability = $null
        $rawCapability = $null
    }
}

function Get-HostLaunchChallenge {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedSourceCommit,
        [Parameter(Mandatory = $true)][string]$ExpectedManifestSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedInstallerSha256
    )

    $now = [DateTime]::UtcNow
    $valid = [Collections.Generic.List[object]]::new()
    foreach ($file in @(Get-ChildItem -LiteralPath $challengeRoot `
            -Filter 'automatic-materialization-challenge-*.json' -File -ErrorAction Stop)) {
        try {
            $challenge = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json -ErrorAction Stop
            $created = [DateTimeOffset]::ParseExact(
                [string]$challenge.created_at_utc,
                'o',
                [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::RoundtripKind
            ).UtcDateTime
            if ($challenge.schema_version -eq 1 -and
                $challenge.status -ceq 'pending' -and
                [string]$challenge.challenge_id -cmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' -and
                $file.Name -ceq "automatic-materialization-challenge-$($challenge.challenge_id).json" -and
                $created -le $now -and $created -ge $now.AddMinutes(-5) -and
                $challenge.source_commit -ceq $ExpectedSourceCommit -and
                $challenge.candidate_manifest_sha256 -ceq $ExpectedManifestSha256 -and
                $challenge.installer_sha256 -ceq $ExpectedInstallerSha256 -and
                $challenge.fixture_name -ceq $fixtureName -and
                [int64]$challenge.fixture_size -eq $fixtureSize -and
                $challenge.fixture_sha256 -ceq $fixtureSha256) {
                $valid.Add([pscustomobject]@{
                    challenge = $challenge
                    created = $created
                    path = $file.FullName
                })
            }
        }
        catch { }
    }
    if ($valid.Count -eq 0) { throw 'host_launch_challenge_missing' }
    $newest = @($valid | Sort-Object created -Descending)
    if ($newest.Count -gt 1 -and $newest[0].created -eq $newest[1].created) {
        throw 'host_launch_challenge_ambiguous'
    }
    [pscustomobject]@{
        Document = $newest[0].challenge
        Sha256 = (Get-FileHash -LiteralPath $newest[0].path -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

try {
    Assert-ElevatedGuest
    foreach ($required in @($manifestPath, $installerPath, $verifierPath, $installedProofPath, $inputPath)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw 'required_input_missing'
        }
    }
    $fixture = Get-Item -LiteralPath $inputPath
    $observedFixtureSha256 = (Get-FileHash -LiteralPath $inputPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($fixture.Length -ne $fixtureSize -or $observedFixtureSha256 -cne $fixtureSha256) {
        throw 'fixture_identity_mismatch'
    }

    $candidate = Get-TownLightCandidateIdentity -ManifestPath $manifestPath
    $manifest = $candidate.manifest
    $manifestSha256 = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $installerSha256 = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($manifest.product -cne 'TownLight Station' -or
        [string]$manifest.source_commit -cnotmatch '^[0-9a-f]{40}$' -or
        [string]$candidate.installer_sha256 -cne $installerSha256 -or
        [string]$manifest.installer_sha256 -cne $installerSha256) {
        throw 'candidate_identity_mismatch'
    }
    $result.source_commit = [string]$manifest.source_commit
    $result.installer_sha256 = $installerSha256
    $result.candidate_provenance = [ordered]@{
        mode = 'outer_host_attested'
        standalone = $false
        host_upgrade_required = $true
        expected_source_commit = [string]$manifest.source_commit
        candidate_manifest_sha256 = $manifestSha256
        installer_sha256 = $installerSha256
    }
    $challengeAuthority = Get-HostLaunchChallenge -ExpectedSourceCommit ([string]$manifest.source_commit) `
        -ExpectedManifestSha256 $manifestSha256 -ExpectedInstallerSha256 $installerSha256
    $challenge = $challengeAuthority.Document
    $result.challenge_id = [string]$challenge.challenge_id
    $result.challenge_sha256 = [string]$challengeAuthority.Sha256

    $stage = 'install'
    $installer = Start-Process -FilePath $installerPath -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=$installerLog"
    ) -Wait -PassThru
    if ($installer.ExitCode -ne 0) { throw 'installer_failed' }
    $verification = (& $verifierPath -Manifest $manifestPath `
        -SharedScriptsRoot 'C:\TownLightShared' | Out-String) | ConvertFrom-Json -ErrorAction Stop
    if ($verification.source_commit -cne [string]$manifest.source_commit -or
        $verification.health -cne 'ready') {
        throw 'installed_verification_failed'
    }
    $result.verifier = $verification

    $stage = 'bootstrap'
    $proofCredential = New-RandomProofCredential
    Invoke-FreshBootstrap -CredentialBundle $proofCredential
    $script:apiSession = New-TownLightProofSession -BaseUri $baseUri `
        -Credential $proofCredential.Credential -TimeoutSec 30
    if ($script:apiSession.Identity.username -cne $proofCredential.Credential.UserName -or
        @($script:apiSession.Identity.roles | Where-Object { $_ -ceq 'setup_admin' }).Count -ne 1) {
        throw 'authenticated_identity_mismatch'
    }
    $proofCredential.PlainText = $null

    $stage = 'station_and_channel'
    Invoke-ProofApi Put '/station' ([ordered]@{
        station_id = [guid]::NewGuid().ToString('D')
        display_name = 'TownLight Automatic Materialization Proof Station'
        timezone = 'UTC'
        expected_revision = 0
    }) | Out-Null

    $channelId = [guid]::NewGuid().ToString('D')
    $sinkId = [guid]::NewGuid().ToString('D')
    $channel = [ordered]@{
        channel_id = $channelId
        display_name = "Automatic materialization proof $runId"
        output_profile = 'proof-360p30'
        output_sinks = @([ordered]@{
            sink_id = $sinkId
            label = 'Automatic materialization proof sink'
            kind = 'udp-ts'
            destination = '127.0.0.1:56080'
            loudness_target_lufs = -16.0
            required = $true
            enabled = $true
        })
        enabled = $false
    }
    Invoke-ProofApi Put '/channels' $channel | Out-Null

    $stage = 'asset_ingest'
    $proofInputRoot = 'C:\ProgramData\TownLight Station\proof-inputs'
    [void][IO.Directory]::CreateDirectory($proofInputRoot)
    $guestFixturePath = Join-Path $proofInputRoot "automatic-materialization-$runId.ts"
    Copy-Item -LiteralPath $inputPath -Destination $guestFixturePath -ErrorAction Stop
    if ((Get-FileHash -LiteralPath $guestFixturePath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $fixtureSha256) {
        throw 'guest_fixture_copy_mismatch'
    }
    $ingestJobId = [guid]::NewGuid().ToString('D')
    Invoke-ProofApi Post '/assets/ingest' ([ordered]@{
        job_id = $ingestJobId
        source_path = $guestFixturePath
    }) | Out-Null
    $ingest = Wait-ForProofState -Action {
        Invoke-ProofApi Get "/assets/ingest/jobs/$ingestJobId"
    } -Predicate {
        param($value)
        if ($value.status -in @('failed', 'rejected')) { throw 'asset_ingest_failed' }
        $value.status -ceq 'completed' -and $null -ne $value.asset_id
    } -Description 'asset_ingest' -Seconds 300
    $assetId = [string]$ingest.asset_id
    if ($assetId -cnotmatch '^[0-9a-f]{64}$') { throw 'asset_identity_invalid' }
    Wait-ForProofState -Action {
        Invoke-ProofApi Get "/assets/$assetId/readiness"
    } -Predicate {
        param($value)
        if ($value.readiness_state -in @('missing_file', 'rejected')) { throw 'asset_readiness_failed' }
        $value.readiness_state -ceq 'ready'
    } -Description 'asset_readiness' -Seconds 300 | Out-Null

    $stage = 'catalog_authority'
    $meetingBody = "Automatic Proof $runId"
    $catalog = Invoke-ProofApi Put "/catalog/assets/$assetId" ([ordered]@{
        title = 'Automatic materialization proof meeting'
        meeting_body = $meetingBody
        expected_revision = $null
    })
    if ($catalog.revision -ne 1 -or $catalog.editorial_state -cne 'draft') {
        throw 'catalog_draft_invalid'
    }
    $published = Invoke-ProofApi Post "/catalog/assets/$assetId/publish" ([ordered]@{
        expected_revision = 1
    })
    if ($published.revision -ne 2 -or $published.editorial_state -cne 'published') {
        throw 'catalog_publish_invalid'
    }

    $stage = 'recurring_authority'
    $searchId = [guid]::NewGuid().ToString('D')
    $blockId = [guid]::NewGuid().ToString('D')
    $ruleId = [guid]::NewGuid().ToString('D')
    $search = Invoke-ProofApi Put "/scheduling/saved-searches/$searchId" ([ordered]@{
        name = 'Automatic proof search'
        query = [ordered]@{
            all_of = @([ordered]@{ MeetingBodyEquals = $meetingBody })
            any_of = @()
            order_by = 'PublishedNewest'
            limit = 1
        }
        expected_revision = $null
    })
    if ($search.revision -ne 1 -or $search.search_id -cne $searchId) {
        throw 'saved_search_invalid'
    }
    $block = Invoke-ProofApi Put "/scheduling/daypart-blocks/$blockId" ([ordered]@{
        channel_id = $channelId
        label = 'All day automatic proof'
        starts_local_minute = 0
        ends_local_minute = 1440
        weekday_mask = 127
        season_starts_on = $null
        season_ends_on = $null
        allow_rule_ids = @($ruleId)
        expected_revision = $null
    })
    if ($block.revision -ne 1 -or $block.block_id -cne $blockId) {
        throw 'daypart_invalid'
    }
    $anchor = [DateTime]::UtcNow.Date.AddDays(2)
    $rule = Invoke-ProofApi Put "/scheduling/recurring-rules/$ruleId" ([ordered]@{
        channel_id = $channelId
        saved_search_id = $searchId
        saved_search_revision = 1
        pick_strategy = 'Newest'
        rolling_window_days = 14
        avoid_repeat_within_days = $null
        enabled = $true
        priority = 10
        block_id = $blockId
        block_revision = 1
        weekday_mask = 127
        interval_weeks = 1
        anchor_date = [ordered]@{ year = $anchor.Year; month = $anchor.Month; day = $anchor.Day }
        timezone = 'Utc'
        local_start_minute = 60
        duration_minutes = 60
        expected_revision = $null
    })
    if ($rule.revision -ne 1 -or $rule.rule_id -cne $ruleId -or -not $rule.enabled) {
        throw 'recurring_rule_invalid'
    }

    $stage = 'automatic_materialization'
    $channel.enabled = $true
    Invoke-ProofApi Put '/channels' $channel | Out-Null
    $history = Wait-ForProofState -Action {
        Invoke-ProofApi Get "/scheduling/channels/$channelId/materializations/history?limit=128"
    } -Predicate {
        param($value)
        $expectedUtcDay = Get-CurrentUtcDay
        $matches = @($value.entries | Where-Object {
            $_.origin -ceq 'automatic' -and
            $_.actor_id -ceq $automaticActorId -and
            [int64]$_.checkpoint_utc_day -eq $expectedUtcDay -and
            @($_.generated_drafts).Count -gt 0 -and
            @($_.generated_drafts | Where-Object {
                $_.current_schedule.channel_id -ceq $channelId -and
                $_.current_schedule.state -ceq 'draft'
            }).Count -gt 0
        })
        $matches.Count -eq 1
    } -Description 'automatic_materialization' -Seconds 240
    $currentUtcDay = Get-CurrentUtcDay
    $automaticRuns = @($history.entries | Where-Object {
        $_.origin -ceq 'automatic' -and
        $_.actor_id -ceq $automaticActorId -and
        [int64]$_.checkpoint_utc_day -eq $currentUtcDay
    })
    if ($automaticRuns.Count -ne 1) { throw 'automatic_run_count_invalid' }
    $automaticRun = $automaticRuns[0]
    $schedule = @(Invoke-ProofApi Get "/schedule/items?channel_id=$channelId")
    $drafts = @($automaticRun.generated_drafts)
    foreach ($draft in $drafts) {
        $matches = @($schedule | Where-Object {
            $_.item_id -ceq $draft.schedule_item_id -and
            $_.channel_id -ceq $channelId -and
            $_.state -ceq 'draft'
        })
        if ($matches.Count -ne 1) { throw 'automatic_draft_not_current' }
    }
    $result.automatic_run = [ordered]@{
        run_id = [string]$automaticRun.run_id
        origin = [string]$automaticRun.origin
        actor_id = [string]$automaticRun.actor_id
        checkpoint_utc_day = [int64]$automaticRun.checkpoint_utc_day
        request_sha256 = [string]$automaticRun.request_sha256
        result_sha256 = [string]$automaticRun.result_sha256
        generated_draft_ids = @($drafts | ForEach-Object { [string]$_.schedule_item_id })
    }

    $stage = 'restart_proof'
    & $installedProofPath -Credential $proofCredential.Credential -ChannelId $channelId `
        -OuterHostAttested -ExpectedSourceCommit ([string]$manifest.source_commit) `
        -ExpectedCandidateManifestSha256 $manifestSha256 `
        -ExpectedInstallerSha256 $installerSha256 -CandidateManifestPath $manifestPath `
        -TimeoutSeconds 60 -ServiceTransitionTimeoutSeconds 120 `
        -PostRestartObservationSeconds 65 -MaxHistoryPages 8 `
        -EnableServiceRestartProof -NoProcessExit -ResultPath $installedResultPath | Out-Null
    if (-not (Test-Path -LiteralPath $installedResultPath -PathType Leaf)) {
        throw 'installed_proof_receipt_missing'
    }
    $installedProof = Get-Content -LiteralPath $installedResultPath -Raw | ConvertFrom-Json -ErrorAction Stop
    if ($installedProof.proof -cne 'installed-automatic-materialization' -or
        $installedProof.status -cne 'outer_host_attested' -or
        $installedProof.source_commit -cne [string]$manifest.source_commit -or
        $installedProof.channel_id -cne $channelId -or
        $installedProof.candidate_provenance.mode -cne 'outer_host_attested' -or
        $installedProof.candidate_provenance.standalone -ne $false -or
        $installedProof.candidate_provenance.host_upgrade_required -ne $true -or
        $installedProof.candidate_provenance.expected_source_commit -cne [string]$manifest.source_commit -or
        $installedProof.candidate_provenance.candidate_manifest_sha256 -cne $manifestSha256 -or
        $installedProof.candidate_provenance.installer_sha256 -cne $installerSha256) {
        throw 'installed_proof_invalid'
    }
    $result.installed_proof = $installedProof
    $result.status = 'outer_host_attested'
}
catch {
    $result.failure_stage = $stage
    $code = [string]$_.Exception.Message
    $result.error_code = if ($code -match '^[a-z0-9_]{1,64}$') { $code } else { 'guest_proof_failed' }
}
finally {
    if ($null -ne $proofCredential) { $proofCredential.PlainText = $null }
    $script:apiSession = $null
    $result.completed_at_utc = [DateTime]::UtcNow.ToString('o')
    $json = ($result | ConvertTo-Json -Depth 32) + [Environment]::NewLine
    try {
        [IO.File]::WriteAllText($partPath, $json, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $partPath -Destination $resultPath -ErrorAction Stop
    }
    finally {
        & "$env:SystemRoot\System32\shutdown.exe" /s /t 15 /d p:0:0 /c "TownLight automatic materialization proof completed"
        if ($LASTEXITCODE -ne 0) { throw 'sandbox_shutdown_request_failed' }
    }
}

if ($result.status -cne 'outer_host_attested') { exit 1 }
