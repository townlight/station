<##
.SYNOPSIS
    Runs the installed-machine commissioning gate against the loopback API.

.DESCRIPTION
    This is an evidence-producing gate, not a provisioning script. It reads the
    installed receipt and service state, admits a caller-supplied real media
    fixture through the API, waits for readiness, exercises the transactional
    schedule lifecycle, restarts the installed station service, and optionally
    composes the existing installed CG image/pixel proof.

    It deliberately does not write the station database or station data tree.
    The proof channel and schedule identities are new UUIDs on every run. The
    API has no channel/schedule delete operation, so the disabled proof channel
    and durable proof schedule row remain as auditable evidence.
##>
param(
    [Parameter(Mandatory = $true)]
    [Management.Automation.PSCredential]$Credential,
    [Parameter(Mandatory = $true)]
    [string]$FixturePath,
    [string]$ExpectedSourceCommit = '',
    [string]$CandidateManifestPath = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\candidate-manifest.json'),
    [string]$BaseUri = 'http://127.0.0.1:4070',
    [int]$StartLeadSeconds = 15,
    [int]$TimeoutSeconds = 180,
    [string]$ResultPath = '',
    [switch]$SkipCgProof,
    [switch]$NoServiceRestart
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'candidate-pair-verifier.ps1')
. (Join-Path $PSScriptRoot 'installed-proof-api.ps1')
$apiSession = New-TownLightProofSession -BaseUri $BaseUri -Credential $Credential

function Invoke-InstalledProofApi {
    [CmdletBinding()]
    param(
        [Parameter(Position = 0, Mandatory = $true)][string]$Uri,
        [string]$Method = 'Get',
        [string]$ContentType = 'application/json',
        $Body = $null,
        [int]$TimeoutSec = 30
    )
    $requestUri = [uri]$Uri
    $path = $requestUri.PathAndQuery -replace '^/api/v1', ''
    $parameters = @{ Session = $apiSession; Method = $Method; Path = $path; TimeoutSec = $TimeoutSec }
    if ($PSBoundParameters.ContainsKey('Body')) { $parameters.Body = $Body; $parameters.ContentType = $ContentType }
    Invoke-TownLightProofApi @parameters
}
$serviceUri = $BaseUri.TrimEnd('/') -replace '/api/v1$', ''
$apiUri = "$serviceUri/api/v1"
$runId = [guid]::NewGuid().ToString()
$channelId = [guid]::NewGuid().ToString()
$sinkId = [guid]::NewGuid().ToString()
$ingestJobId = [guid]::NewGuid().ToString()
$scheduleItemId = [guid]::NewGuid().ToString()
$planId = "commissioning-$runId"
$reportId = "commissioning-$runId"
$proofChannel = $null
$result = [ordered]@{
    schema_version = 1
    product = 'TownLight Station'
    result_kind = 'installed_commissioning'
    run_id = $runId
    started_at_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    gate_status = 'not_run'
    evidence = [ordered]@{}
}

function Invoke-Api {
    param(
        [ValidateSet('Get', 'Put', 'Post')]
        [string]$Method,
        [string]$Path,
        $Body = $null,
        [int]$Timeout = 30
    )
    try {
        $parameters = @{ Method = $Method; Uri = "$apiUri$Path"; TimeoutSec = $Timeout }
        if ($null -ne $Body) {
            $parameters.ContentType = 'application/json'
            $parameters.Body = $Body | ConvertTo-Json -Depth 30 -Compress
        }
        return Invoke-InstalledProofApi @parameters
    }
    catch {
        # Do not echo response bodies: an API error must not become a place where
        # future credentials, URLs, or media metadata are accidentally logged.
        throw "Installed API request failed: $Method $Path ($($_.Exception.GetType().Name))."
    }
}

function Wait-Until {
    param(
        [scriptblock]$Action,
        [scriptblock]$Predicate,
        [string]$Description,
        [int]$Seconds = $TimeoutSeconds
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    $last = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $last = & $Action
            if (& $Predicate $last) { return $last }
        }
        catch { }
        Start-Sleep -Milliseconds 300
    }
    throw "Timed out waiting for $Description."
}

function Get-InstalledService {
    param([string]$Name)
    $service = Get-CimInstance Win32_Service -Filter "Name='$Name'"
    if (-not $service -or $service.State -ne 'Running' -or $service.StartName -ne 'LocalSystem' -or
        [int]$service.ProcessId -le 0) {
        throw "Installed service $Name is not running as LocalSystem."
    }
    return $service
}

function Get-FreeUdpPort {
    $probe = [Net.Sockets.UdpClient]::new(0)
    try { return ([Net.IPEndPoint]$probe.Client.LocalEndPoint).Port }
    finally { $probe.Dispose() }
}

function Invoke-CgProof {
    $mutex = [Threading.Mutex]::new($false, 'Global\TownLightStationInstalledCgProof')
    $acquired = $false
    try {
        $acquired = $mutex.WaitOne(0)
        if (-not $acquired) { throw 'Another installed CG proof is already running.' }
        $script = Join-Path $PSScriptRoot 'prove-installed-cg-images.ps1'
        if (-not (Test-Path -LiteralPath $script -PathType Leaf)) {
            throw "The existing CG proof script is missing: $script"
        }
        $output = & $script -Credential $Credential -BaseUri $serviceUri `
            -DestinationPort (Get-FreeUdpPort) -ValidateClear 2>&1
        $text = ($output | ForEach-Object { [string]$_ }) -join "`n"
        $firstBrace = $text.IndexOf('{')
        $lastBrace = $text.LastIndexOf('}')
        if ($firstBrace -lt 0 -or $lastBrace -lt $firstBrace) {
            throw 'The composed installed CG proof returned no JSON evidence.'
        }
        return ($text.Substring($firstBrace, $lastBrace - $firstBrace + 1) | ConvertFrom-Json)
    }
    finally {
        if ($acquired) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

try {
    if ($StartLeadSeconds -lt 5 -or $StartLeadSeconds -gt 300) {
        throw 'StartLeadSeconds must be between 5 and 300 seconds.'
    }
    if ($TimeoutSeconds -lt 30 -or $TimeoutSeconds -gt 3600) {
        throw 'TimeoutSeconds must be between 30 and 3600 seconds.'
    }
    $fixture = Resolve-Path -LiteralPath $FixturePath -ErrorAction Stop
    $fixtureInfo = Get-Item -LiteralPath $fixture.Path
    if (-not $fixtureInfo.PSIsContainer -and $fixtureInfo.Length -gt 0) { }
    else { throw 'FixturePath must identify a non-empty regular media file.' }

    $stationService = Get-InstalledService 'TownLightStation'
    $updaterService = Get-InstalledService 'TownLightStationUpdater'
    $dataPath = Join-Path $env:ProgramData 'TownLight Station'
    $receiptPath = Join-Path $dataPath 'install-receipt.json'
    if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) {
        throw "Installed receipt is missing: $receiptPath"
    }
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    $manifestPath = Resolve-Path -LiteralPath $CandidateManifestPath -ErrorAction Stop
    $candidatePair = Get-TownLightCandidateIdentity -ManifestPath $manifestPath.Path -ExpectedSourceCommit $receipt.source_commit
    $manifest = $candidatePair.manifest
    $result.source_commit = [string]$manifest.source_commit
    $result.candidate_manifest_sha256 = (Get-FileHash -LiteralPath $manifestPath.Path -Algorithm SHA256).Hash.ToLowerInvariant()
    $result.installer_sha256 = [string]$manifest.installer_sha256
    if ($ExpectedSourceCommit -and $receipt.source_commit -ne $ExpectedSourceCommit) {
        throw "Installed source identity does not match ExpectedSourceCommit."
    }
    if ($manifest.source_commit -ne $receipt.source_commit -or
        $manifest.media_runtime_sha256 -ne $receipt.media_runtime_sha256) {
        throw 'The candidate manifest does not match the installed receipt.'
    }
    $applicationPath = Join-Path $env:ProgramFiles 'TownLight Station'
    $stationdHash = (Get-FileHash -LiteralPath (Join-Path $applicationPath 'stationd.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
    $workerHash = (Get-FileHash -LiteralPath (Join-Path $applicationPath 'channel-worker.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
    $updaterHash = (Get-FileHash -LiteralPath (Join-Path $applicationPath 'station-updater.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($stationdHash -ne $manifest.stationd_sha256 -or
        $workerHash -ne $manifest.channel_worker_sha256 -or
        $updaterHash -ne $manifest.station_updater_sha256) {
        throw 'Installed executable hashes do not match the candidate manifest.'
    }
    $result.evidence.installed_identity = [ordered]@{
        source_commit = $receipt.source_commit
        version = $receipt.version
        installer_sha256 = $manifest.installer_sha256
        stationd_sha256 = $stationdHash
        channel_worker_sha256 = $workerHash
        station_updater_sha256 = $updaterHash
        media_runtime_sha256 = $receipt.media_runtime_sha256
        receipt_path = $receiptPath
        candidate_manifest_path = $manifestPath.Path
    }
    $result.evidence.services = [ordered]@{
        station = [ordered]@{ state = $stationService.State; start_name = $stationService.StartName; process_id = $stationService.ProcessId }
        updater = [ordered]@{ state = $updaterService.State; start_name = $updaterService.StartName; process_id = $updaterService.ProcessId }
    }

    $health = Wait-Until -Action { Invoke-RestMethod -Uri "$serviceUri/health" -TimeoutSec 5 } `
        -Predicate { param($value) $value.status -eq 'ready' -and $value.database -eq 'ready' } `
        -Description 'installed database health' -Seconds 30
    $profileBefore = Invoke-Api 'Get' '/station'
    $result.evidence.health_before_restart = $health
    $result.evidence.station_profile_before_restart = $profileBefore

    if (-not $SkipCgProof) {
        $result.evidence.cg = Invoke-CgProof
    }
    else {
        $result.evidence.cg = [ordered]@{ status = 'skipped'; reason = 'SkipCgProof was supplied.' }
    }

    $port = Get-FreeUdpPort
    $proofChannel = [ordered]@{
        channel_id = $channelId
        display_name = "Installed commissioning proof $runId"
        output_profile = 'proof-360p30'
        output_sinks = @([ordered]@{
            sink_id = $sinkId
            label = 'Installed commissioning proof'
            kind = 'udp-ts'
            destination = "127.0.0.1:$port"
            loudness_target_lufs = -16.0
            required = $true
            enabled = $true
        })
        enabled = $true
    }
    Invoke-Api 'Put' '/channels' $proofChannel | Out-Null
    $safeBefore = Wait-Until -Action { Invoke-Api 'Get' '/runtime-safe-to-air' } `
        -Predicate {
            param($value)
            $value.color -eq 'green' -and @($value.channels | Where-Object {
                $_.channel_id -eq $channelId -and ($_.state -in @('healthy_fallback', 'on_air'))
            }).Count -eq 1
        } -Description 'proof channel safe-to-air runtime' -Seconds 60
    $result.evidence.safe_to_air_before = $safeBefore

    $ingestBody = [ordered]@{ job_id = $ingestJobId; source_path = $fixture.Path }
    $ingest = Invoke-Api 'Post' '/assets/ingest' $ingestBody
    $job = Wait-Until -Action { Invoke-Api 'Get' "/assets/ingest/jobs/$ingestJobId" } `
        -Predicate {
            param($value)
            if ($value.status -in @('failed', 'rejected')) { throw 'Installed media ingest entered a terminal failure.' }
            $value.status -eq 'completed' -and $value.progress_percent -eq 100 -and $null -ne $value.asset_id
        } -Description 'durable media ingest completion'
    $assetId = [string]$job.asset_id
    $readiness = Wait-Until -Action { Invoke-Api 'Get' "/assets/$assetId/readiness" } `
        -Predicate { param($value) $value.readiness_state -eq 'ready' } `
        -Description 'canonical media readiness'
    $dashboard = Invoke-Api 'Get' '/assets/readiness-dashboard'
    $result.evidence.media = [ordered]@{
        ingest_job = $job
        asset_id = $assetId
        readiness = $readiness
        readiness_dashboard = $dashboard
    }

    $start = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + ([int64]$StartLeadSeconds * 1000)
    $item = [ordered]@{
        item_id = $scheduleItemId
        channel_id = $channelId
        asset_id = $assetId
        title = "Installed commissioning proof $runId"
        starts_at_unix_ms = $start
        duration_ms = 3000
        state = 'draft'
    }
    Invoke-Api 'Put' '/schedule/items' $item | Out-Null
    $plan = Invoke-Api 'Post' '/schedule/prepare' ([ordered]@{ plan_id = $planId; schedule_item_id = $scheduleItemId })
    if (-not $plan.dry_run_passed -or @($plan.conflicts).Count -ne 0 -or $plan.missing_media_detail) {
        throw 'Installed schedule prepare did not pass the media/conflict gate.'
    }
    $report = Invoke-Api 'Post' '/schedule/commit' ([ordered]@{
        report_id = $reportId
        plan_id = $planId
        schedule_item_id = $scheduleItemId
        operator_notes = 'Installed commissioning proof.'
    })
    $dispatchStatuses = [Collections.Generic.List[string]]::new()
    $dispatchStatuses.Add([string]$report.dispatch_status)
    $completed = Wait-Until -Action {
        $current = Invoke-Api 'Get' "/schedule/commits/$reportId"
        if ($dispatchStatuses.Count -eq 0 -or $dispatchStatuses[$dispatchStatuses.Count - 1] -ne $current.dispatch_status) {
            $dispatchStatuses.Add([string]$current.dispatch_status)
        }
        return $current
    } `
        -Predicate {
            param($value)
            if ($value.dispatch_status -eq 'error') { throw 'Installed dispatch entered error state.' }
            $value.dispatch_status -eq 'completed'
        } -Description 'durable dispatch acknowledgement and completion' -Seconds 90
    $requiredStatuses = @('pending', 'queued', 'acknowledged', 'completed')
    foreach ($requiredStatus in $requiredStatuses) {
        if (-not $dispatchStatuses.Contains($requiredStatus)) {
            throw "Installed dispatch did not expose required status '$requiredStatus'; observed $($dispatchStatuses -join ', ')."
        }
    }
    $result.evidence.schedule = [ordered]@{
        draft = $item
        prepared = $plan
        committed = $report
        completed = $completed
        observed_statuses = @($dispatchStatuses)
    }

    if (-not $NoServiceRestart) {
        Restart-Service -Name TownLightStation -Force
        $healthAfter = Wait-Until -Action { Invoke-RestMethod -Uri "$serviceUri/health" -TimeoutSec 5 } `
            -Predicate { param($value) $value.status -eq 'ready' -and $value.database -eq 'ready' } `
            -Description 'health after installed service restart' -Seconds 60
        Connect-TownLightProofSession -Session $apiSession -Credential $Credential | Out-Null
        $profileAfter = Invoke-Api 'Get' '/station'
        if ($profileAfter.station_id -ne $profileBefore.station_id -or
            $profileAfter.display_name -ne $profileBefore.display_name -or
            $profileAfter.timezone -ne $profileBefore.timezone) {
            throw 'Persisted station identity changed across service restart.'
        }
        $safeAfter = Wait-Until -Action { Invoke-Api 'Get' '/runtime-safe-to-air' } `
            -Predicate {
                param($value)
                $value.color -eq 'green' -and @($value.channels | Where-Object {
                    $_.channel_id -eq $channelId -and ($_.state -in @('healthy_fallback', 'on_air'))
                }).Count -eq 1
            } -Description 'proof channel safe-to-air after restart' -Seconds 60
        $result.evidence.restart_recovery = [ordered]@{
            health = $healthAfter
            station_profile = $profileAfter
            safe_to_air = $safeAfter
            commit_report = Invoke-Api 'Get' "/schedule/commits/$reportId"
        }
    }
    else {
        $result.evidence.restart_recovery = [ordered]@{ status = 'skipped'; reason = 'NoServiceRestart was supplied.' }
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
        try { Invoke-Api 'Put' '/channels' $proofChannel | Out-Null } catch { }
    }
    $result.finished_at_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    if ($ResultPath) {
        $parent = Split-Path -Parent $ResultPath
        if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        [IO.File]::WriteAllText($ResultPath,(($result | ConvertTo-Json -Depth 30)+[Environment]::NewLine),[Text.UTF8Encoding]::new($false))
    }
}
