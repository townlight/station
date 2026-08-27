<##
.SYNOPSIS
    Proves one installed scheduled recording through the authenticated operator API.

.DESCRIPTION
    This is an installed-candidate proof, not a source or unit test. It admits a
    caller-supplied media fixture, schedules and commits it on a proof-owned
    channel, creates and arms a scheduled recording, then waits for the running
    station controller to capture and finalize the real output. Completion is
    accepted only when the API exposes the complete lifecycle, exact transition
    history, a ready content-addressed media asset, and a draft catalog record.

    The script never writes the station database or data tree and never calls a
    trusted-process or service-control boundary. It cannot claim field proof;
    one-station and three-station operation remain separate gates.
##>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [Management.Automation.PSCredential]$Credential,
    [Parameter(Mandatory = $true)]
    [string]$FixturePath,
    [string]$BaseUri = 'http://127.0.0.1:4070',
    [string]$CandidateManifestPath = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\candidate-manifest.json'),
    [ValidateRange(5, 300)][int]$StartLeadSeconds = 20,
    [ValidateRange(3, 300)][int]$RecordingSeconds = 5,
    [ValidateRange(30, 1800)][int]$TimeoutSeconds = 300,
    [string]$ResultPath = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'candidate-pair-verifier.ps1')
. (Join-Path $PSScriptRoot 'installed-proof-api.ps1')

$runId = [guid]::NewGuid().ToString()
$channelId = [guid]::NewGuid().ToString()
$sinkId = [guid]::NewGuid().ToString()
$ingestJobId = [guid]::NewGuid().ToString()
$scheduleItemId = [guid]::NewGuid().ToString()
$recordingId = [guid]::NewGuid().ToString()
$planId = "scheduled-recording-$runId"
$reportId = "scheduled-recording-$runId"
$serviceUri = $BaseUri.TrimEnd('/') -replace '/api/v1$', ''
$apiUri = "$serviceUri/api/v1"
$apiSession = $null
$proofChannel = $null
$mutex = [Threading.Mutex]::new($false, 'Global\TownLightStationInstalledScheduledRecordingProof')
$mutexHeld = $false
$result = [ordered]@{
    run_id = $runId
    started_at_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    gate_status = 'not_run'
    evidence = [ordered]@{}
}

function Invoke-Api {
    param(
        [ValidateSet('Get', 'Put', 'Post')][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
        $Body = $null,
        [int]$Timeout = 30
    )
    try {
        $parameters = @{ Session = $apiSession; Method = $Method; Path = $Path; TimeoutSec = $Timeout }
        if ($null -ne $Body) {
            $parameters.Body = $Body | ConvertTo-Json -Depth 30 -Compress
            $parameters.ContentType = 'application/json'
        }
        return Invoke-TownLightProofApi @parameters
    }
    catch {
        throw "Installed scheduled-recording API request failed: $Method $Path ($($_.Exception.GetType().Name))."
    }
}

function Wait-Until {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Action,
        [Parameter(Mandatory = $true)][scriptblock]$Predicate,
        [Parameter(Mandatory = $true)][string]$Description,
        [int]$Seconds = $TimeoutSeconds
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $value = & $Action
            if (& $Predicate $value) { return $value }
        }
        catch { }
        Start-Sleep -Milliseconds 500
    }
    throw "Timed out waiting for $Description."
}

function Get-FreeUdpPort {
    $probe = [Net.Sockets.UdpClient]::new(0)
    try { return ([Net.IPEndPoint]$probe.Client.LocalEndPoint).Port }
    finally { $probe.Dispose() }
}

function Assert-InstalledCandidate {
    $receiptPath = Join-Path $env:ProgramData 'TownLight Station\install-receipt.json'
    if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) { throw 'Installed receipt is missing.' }
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    $manifest = Resolve-Path -LiteralPath $CandidateManifestPath -ErrorAction Stop
    $pair = Get-TownLightCandidateIdentity -ManifestPath $manifest.Path -ExpectedSourceCommit $receipt.source_commit
    $manifestHash = (Get-FileHash -LiteralPath $manifest.Path -Algorithm SHA256).Hash.ToLowerInvariant()
    $application = Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'TownLight Station'
    $stationd = (Get-FileHash -LiteralPath (Join-Path $application 'stationd.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
    $worker = (Get-FileHash -LiteralPath (Join-Path $application 'channel-worker.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
    $updater = (Get-FileHash -LiteralPath (Join-Path $application 'station-updater.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($stationd -cne [string]$pair.manifest.stationd_sha256 -or
        $worker -cne [string]$pair.manifest.channel_worker_sha256 -or
        $updater -cne [string]$pair.manifest.station_updater_sha256) {
        throw 'Installed executable hashes do not match the authenticated candidate manifest.'
    }
    [ordered]@{
        source_commit = [string]$receipt.source_commit
        version = [string]$receipt.version
        installer_sha256 = [string]$pair.manifest.installer_sha256
        candidate_manifest_sha256 = $manifestHash
        stationd_sha256 = $stationd
        channel_worker_sha256 = $worker
        station_updater_sha256 = $updater
        receipt_path = $receiptPath
        candidate_manifest_path = $manifest.Path
    }
}

function Get-LifecycleName($Recording) {
    if ($null -eq $Recording.lifecycle) { return '' }
    return [string](@($Recording.lifecycle.PSObject.Properties.Name)[0])
}

try {
    $base = [Uri]$serviceUri
    if ($base.Host -notin @('127.0.0.1', '::1', 'localhost')) { throw 'BaseUri must be an installed loopback API address.' }
    $fixture = Resolve-Path -LiteralPath $FixturePath -ErrorAction Stop
    $fixtureItem = Get-Item -LiteralPath $fixture.Path
    if ($fixtureItem.PSIsContainer -or $fixtureItem.Length -le 0) { throw 'FixturePath must identify a non-empty regular media file.' }
    $mutexHeld = $mutex.WaitOne(0)
    if (-not $mutexHeld) { throw 'Another installed scheduled-recording proof is already running.' }
    $result.evidence.installed_identity = Assert-InstalledCandidate
    $apiSession = New-TownLightProofSession -BaseUri ([Uri]$BaseUri) -Credential $Credential -TimeoutSec 30
    $health = Wait-Until -Action { Invoke-RestMethod -Uri "$serviceUri/health" -TimeoutSec 5 } `
        -Predicate { param($value) $value.status -eq 'ready' -and $value.database -eq 'ready' } `
        -Description 'installed database health' -Seconds 30
    $result.evidence.health = $health

    $port = Get-FreeUdpPort
    $proofChannel = [ordered]@{
        channel_id = $channelId
        display_name = "Installed scheduled recording proof $runId"
        output_profile = 'proof-360p30'
        output_sinks = @([ordered]@{
            sink_id = $sinkId; label = 'Installed scheduled recording proof'
            kind = 'udp-ts'; destination = "127.0.0.1:$port"
            loudness_target_lufs = -16.0; required = $true; enabled = $true
        })
        enabled = $true
    }
    Invoke-Api 'Put' '/channels' $proofChannel | Out-Null
    $safe = Wait-Until -Action { Invoke-Api 'Get' '/runtime-safe-to-air' } -Description 'proof channel safe-to-air' `
        -Predicate { param($value) $value.color -eq 'green' -and @($value.channels | Where-Object { $_.channel_id -eq $channelId -and $_.state -in @('healthy_fallback', 'on_air') }).Count -eq 1 } -Seconds 60
    $result.evidence.safe_to_air = $safe

    Invoke-Api 'Post' '/assets/ingest' ([ordered]@{ job_id = $ingestJobId; source_path = $fixture.Path }) | Out-Null
    $job = Wait-Until -Action { Invoke-Api 'Get' "/assets/ingest/jobs/$ingestJobId" } -Description 'proof fixture ingest' `
        -Predicate { param($value) if ($value.status -in @('failed', 'rejected')) { throw 'Proof fixture ingest failed.' }; $value.status -eq 'completed' -and $null -ne $value.asset_id } -Seconds $TimeoutSeconds
    $assetId = [string]$job.asset_id
    $readiness = Wait-Until -Action { Invoke-Api 'Get' "/assets/$assetId/readiness" } -Description 'proof fixture readiness' `
        -Predicate { param($value) $value.readiness_state -eq 'ready' } -Seconds $TimeoutSeconds

    $start = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + ([int64]$StartLeadSeconds * 1000)
    $item = [ordered]@{
        item_id = $scheduleItemId; channel_id = $channelId; asset_id = $assetId
        title = "Installed scheduled recording proof $runId"
        starts_at_unix_ms = $start; duration_ms = [int64]$RecordingSeconds * 1000; state = 'draft'
    }
    Invoke-Api 'Put' '/schedule/items' $item | Out-Null
    $plan = Invoke-Api 'Post' '/schedule/prepare' ([ordered]@{ plan_id = $planId; schedule_item_id = $scheduleItemId })
    if ($plan.dry_run_passed -ne $true -or @($plan.conflicts).Count -ne 0 -or $plan.missing_media_detail) { throw 'Proof schedule prepare did not pass.' }
    $commit = Invoke-Api 'Post' '/schedule/commit' ([ordered]@{ report_id = $reportId; plan_id = $planId; schedule_item_id = $scheduleItemId; operator_notes = 'Installed scheduled recording proof.' })
    $commit = Wait-Until -Action { Invoke-Api 'Get' "/schedule/commits/$reportId" } -Description 'proof schedule dispatch' `
        -Predicate { param($value) if ($value.dispatch_status -eq 'error') { throw 'Proof schedule dispatch failed.' }; $value.dispatch_status -eq 'completed' } -Seconds 120

    $created = Invoke-Api 'Post' '/scheduled-recordings' ([ordered]@{ recording_id = $recordingId; schedule_item_id = $scheduleItemId })
    if ([string](Get-LifecycleName $created) -cne 'draft') { throw 'Scheduled recording did not start in Draft lifecycle.' }
    $armed = Invoke-Api 'Post' "/scheduled-recordings/$recordingId/arm" ([ordered]@{ expected_revision = [int64]$created.revision })
    if ([string](Get-LifecycleName $armed) -cne 'armed') { throw 'Scheduled recording did not enter Armed lifecycle.' }

    $completed = Wait-Until -Action { Invoke-Api 'Get' "/scheduled-recordings/$recordingId" } -Description 'real scheduled recording capture and finalization' `
        -Predicate {
            param($value)
            $state = Get-LifecycleName $value
            if ($state -eq 'failed' -or $state -eq 'cancelled_after_arm') { throw "Scheduled recording entered terminal failure lifecycle '$state'." }
            $state -eq 'completed'
        } -Seconds $TimeoutSeconds
    $facts = $completed.lifecycle.completed
    $assetHash = [string]$facts.captured_asset_hash
    if ($assetHash -notmatch '^[0-9a-f]{64}$' -or [int64]$facts.captured_duration_ms -le 0) { throw 'Completed recording facts are invalid.' }
    $asset = Invoke-Api 'Get' "/assets/$assetHash"
    $assetReady = Invoke-Api 'Get' "/assets/$assetHash/readiness"
    $catalog = Invoke-Api 'Get' "/catalog/assets/$assetHash"
    if ($asset.readiness -ne 'ready' -or $assetReady.readiness_state -ne 'ready') { throw 'Finalized recording asset is not ready.' }
    if ([string]$catalog.asset_sha256 -cne $assetHash -or [string]$catalog.editorial_state -cne 'draft') { throw 'Finalized recording catalog draft is missing or rebound.' }

    $history = @(Invoke-Api 'Get' "/scheduled-recordings/$recordingId/history?limit=128")
    $kinds = @($history | ForEach-Object { [string]$_.receipt.kind })
    foreach ($required in @('arm', 'start_capture', 'begin_finalization', 'finalize_success')) {
        if (-not $kinds.Contains($required)) { throw "Scheduled recording history omitted required transition '$required'." }
    }
    $result.evidence.recording = [ordered]@{
        recording = $completed; history = $history; transition_kinds = $kinds
        captured_asset_hash = $assetHash; captured_duration_ms = [int64]$facts.captured_duration_ms
        media_asset = $asset; readiness = $assetReady; catalog = $catalog
    }
    $result.gate_status = 'passed'
}
catch {
    $result.gate_status = 'failed'
    $result.failure = $_.Exception.Message
    throw
}
finally {
    if ($proofChannel) {
        $proofChannel.enabled = $false
        try { Invoke-Api 'Put' '/channels' $proofChannel | Out-Null } catch { Write-Warning 'Could not disable the proof channel during cleanup.' }
    }
    $result.finished_at_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    if ($ResultPath) {
        $parent = Split-Path -Parent $ResultPath
        if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        $result | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $ResultPath -Encoding utf8
    }
    if ($mutexHeld) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}

$result | ConvertTo-Json -Depth 40
