<##
.SYNOPSIS
    Proves an installed candidate's post-encode output loudness through its loopback API.

.DESCRIPTION
    This is an evidence-producing installed-machine gate.  It reads only the
    installed candidate receipts/manifest for identity, admits the caller's
    audible media fixture through the public API, schedules enough of that
    fixture to keep the proof channel active, requests the bounded output
    loudness proof, and verifies the completed job and immutable per-sink proof
    bind to the exact candidate and output configuration.

    The harness never reads or writes the station database, journal, worker
    state, or output-proof artifact directly.  Receiver acceptance is expected
    to remain explicitly not_attempted or unknown: this is local post-mux
    evidence, not receiver or headend acceptance.  A unique proof channel is
    disabled in finally; unrelated channels and station-wide alert state are
    never changed or used as a gate.
##>
param(
    [Parameter(Mandatory = $true)]
    [Management.Automation.PSCredential]$Credential,
    [Parameter(Mandatory = $true)]
    [string]$FixturePath,
    [string]$CandidateManifestPath = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\candidate-manifest.json'),
    [string]$ExpectedSourceCommit = '',
    [string]$BaseUri = 'http://127.0.0.1:4070',
    [ValidateRange(3, 300)]
    [int]$SampleDurationSeconds = 5,
    [ValidateRange(5, 300)]
    [int]$StartLeadSeconds = 15,
    [ValidateRange(64KB, 256MB)]
    [long]$MaxCaptureBytes = 64MB,
    [ValidateRange(60, 1800)]
    [int]$TimeoutSeconds = 600,
    [ValidateRange(3, 60)]
    [int]$ClaimTimeoutSeconds = 15,
    [string]$HeadendProfileId = '',
    [string]$ResultPath = ''
)

$ErrorActionPreference = 'Stop'
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
$planId = "output-loudness-$runId"
$reportId = "output-loudness-$runId"
$targetLufs = -16.0
$targetLufsMilli = -16000
$udpReceiver = $null
$proofChannel = $null
$channelConfigured = $false
$primaryFailure = $null
$cleanupFailure = $null
$result = [ordered]@{
    run_id = $runId
    channel_id = $channelId
    sink_id = $sinkId
    headend_profile_id = $HeadendProfileId
    started_at_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    status = 'not_run'
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
        $value = Invoke-InstalledProofApi @parameters
        if ($value -is [string] -and $value.Trim() -eq 'null') { return $null }
        return $value
    }
    catch {
        # Do not echo response bodies into evidence or logs.  They may gain
        # operator-supplied paths or future credentials as the API evolves.
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
    $lastError = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $value = & $Action
        }
        catch {
            $lastError = $_.Exception.Message
            Start-Sleep -Milliseconds 300
            continue
        }
        # Predicate failures are intentional gate failures and must not be
        # turned into a timeout (for example, a durable job entering Failed).
        if (& $Predicate $value) { return $value }
        Start-Sleep -Milliseconds 300
    }
    $suffix = if ($lastError) { " Last API error: $lastError" } else { '' }
    throw "Timed out waiting for $Description.$suffix"
}

function Get-FreeUdpPort {
    $probe = [Net.Sockets.UdpClient]::new(0)
    try { return ([Net.IPEndPoint]$probe.Client.LocalEndPoint).Port }
    finally { $probe.Dispose() }
}

function Measure-HeadendUdp {
    param(
        [Net.Sockets.UdpClient]$Receiver,
        [ValidateRange(2, 30)]
        [int]$Seconds = 5
    )
    $Receiver.Client.ReceiveTimeout = 2000
    $remote = [Net.IPEndPoint]::new([Net.IPAddress]::Any, 0)
    $packetLengths = [Collections.Generic.HashSet[int]]::new()
    $drainDeadline = [DateTime]::UtcNow.AddSeconds(2)
    while ($Receiver.Available -gt 0 -and [DateTime]::UtcNow -lt $drainDeadline) {
        [void]$Receiver.Receive([ref]$remote)
    }
    if ($Receiver.Available -gt 0) {
        throw 'Could not establish a bounded clean UDP measurement interval.'
    }
    try {
        $firstPacket = $Receiver.Receive([ref]$remote)
    }
    catch [Net.Sockets.SocketException] {
        throw 'No UDP transport packets reached the local headend receiver.'
    }
    [void]$packetLengths.Add($firstPacket.Length)
    $packets = 1L
    $bytes = [int64]$firstPacket.Length
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.Elapsed.TotalSeconds -lt $Seconds) {
        try {
            $packet = $Receiver.Receive([ref]$remote)
        }
        catch [Net.Sockets.SocketException] { continue }
        [void]$packetLengths.Add($packet.Length)
        $packets++
        $bytes += $packet.Length
    }
    $watch.Stop()
    if ($packets -lt 100 -or $packetLengths.Count -ne 1 -or -not $packetLengths.Contains(1316)) {
        throw 'Installed headend UDP output did not preserve the 1,316-byte datagram contract.'
    }
    $bitrateBps = [int64][Math]::Round(($bytes * 8.0) / $watch.Elapsed.TotalSeconds)
    if ($bitrateBps -lt 7200000 -or $bitrateBps -gt 8800000) {
        throw "Installed headend UDP output bitrate $bitrateBps bps is outside the 8 Mb/s contract tolerance."
    }
    return [ordered]@{
        sample_duration_ms = [int64][Math]::Round($watch.Elapsed.TotalMilliseconds)
        packet_count = $packets
        received_bytes = $bytes
        packet_sizes_bytes = @($packetLengths)
        measured_bitrate_bps = $bitrateBps
        expected_bitrate_bps = 8000000
        expected_packet_size_bytes = 1316
    }
}

function Assert-InstalledIdentity {
    param([string]$ManifestPath)
    $manifestResolved = Resolve-Path -LiteralPath $ManifestPath -ErrorAction Stop
    $manifest = Get-Content -LiteralPath $manifestResolved.Path -Raw | ConvertFrom-Json
    $dataPath = Join-Path $env:ProgramData 'TownLight Station'
    $receiptPath = Join-Path $dataPath 'install-receipt.json'
    $activePath = Join-Path $dataPath 'active-slot.json'
    if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $activePath -PathType Leaf)) {
        throw 'Installed candidate receipts are missing.'
    }
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    $active = Get-Content -LiteralPath $activePath -Raw | ConvertFrom-Json
    if ($manifest.product -ne 'TownLight Station' -or $receipt.product -ne 'TownLight Station') {
        throw 'The candidate manifest or installation receipt has the wrong product identity.'
    }
    if ($ExpectedSourceCommit -and $receipt.source_commit -ne $ExpectedSourceCommit) {
        throw 'Installed source identity does not match ExpectedSourceCommit.'
    }
    if ($manifest.version -ne $receipt.version -or
        $manifest.source_commit -ne $receipt.source_commit -or
        $manifest.media_runtime_sha256 -ne $receipt.media_runtime_sha256) {
        throw 'The candidate manifest does not match the installed receipt.'
    }
    if ($active.active.relative_directory -ne '.' -or
        $active.active.package_manifest.version -ne $manifest.version -or
        $active.active.package_manifest.source_commit -ne $manifest.source_commit) {
        throw 'The active-slot receipt does not match the installed candidate.'
    }
    $applicationPath = Join-Path $env:ProgramFiles 'TownLight Station'
    $stationdPath = Join-Path $applicationPath 'stationd.exe'
    $workerPath = Join-Path $applicationPath 'channel-worker.exe'
    $updaterPath = Join-Path $applicationPath 'station-updater.exe'
    foreach ($path in @($stationdPath, $workerPath, $updaterPath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Installed candidate executable is missing: $path"
        }
    }
    $stationdHash = (Get-FileHash -LiteralPath $stationdPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $workerHash = (Get-FileHash -LiteralPath $workerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $updaterHash = (Get-FileHash -LiteralPath $updaterPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($stationdHash -ne ([string]$manifest.stationd_sha256).ToLowerInvariant() -or
        $workerHash -ne ([string]$manifest.channel_worker_sha256).ToLowerInvariant() -or
        $updaterHash -ne ([string]$manifest.station_updater_sha256).ToLowerInvariant()) {
        throw 'Installed executable hashes do not match the candidate manifest.'
    }
    return [ordered]@{
        source_commit = $receipt.source_commit
        version = $receipt.version
        media_runtime_sha256 = $receipt.media_runtime_sha256
        stationd_sha256 = $stationdHash
        channel_worker_sha256 = $workerHash
        station_updater_sha256 = $updaterHash
        receipt_path = $receiptPath
        active_slot_path = $activePath
        candidate_manifest_path = $manifestResolved.Path
    }
}

function Get-ProofRuntime {
    param([object]$Value)
    @($Value.channels | Where-Object { $_.channel_id -eq $channelId }) | Select-Object -First 1
}

try {
    $baseAddress = [Uri]$serviceUri
    if ($baseAddress.Scheme -ne 'http' -or
        $baseAddress.Host -notin @('127.0.0.1', '::1', 'localhost')) {
        throw 'BaseUri must be an installed loopback HTTP API address.'
    }
    if ($HeadendProfileId -notin @('', 'generic-udp-spts', 'telvue-hypercaster-ip')) {
        throw 'HeadendProfileId must be empty, generic-udp-spts, or telvue-hypercaster-ip.'
    }
    if ($HeadendProfileId) {
        $targetLufs = -24.0
        $targetLufsMilli = -24000
    }
    $fixture = Resolve-Path -LiteralPath $FixturePath -ErrorAction Stop
    $fixtureInfo = Get-Item -LiteralPath $fixture.Path
    if ($fixtureInfo.PSIsContainer -or $fixtureInfo.Length -le 0) {
        throw 'FixturePath must identify a non-empty regular audible media file.'
    }
    $sampleDurationMs = [int64]$SampleDurationSeconds * 1000
    $identity = Assert-InstalledIdentity $CandidateManifestPath
    $result.evidence.installed_identity = $identity

    $health = Wait-Until -Action { Invoke-RestMethod -Uri "$serviceUri/health" -TimeoutSec 5 } `
        -Predicate { param($value) $value.status -eq 'ready' -and $value.database -eq 'ready' } `
        -Description 'installed database health' -Seconds 30
    $result.evidence.health = $health

    if ($HeadendProfileId) {
        $udpReceiver = [Net.Sockets.UdpClient]::new(0)
        $port = ([Net.IPEndPoint]$udpReceiver.Client.LocalEndPoint).Port
    }
    else {
        $port = Get-FreeUdpPort
    }
    if ($HeadendProfileId) {
        $proofChannel = [ordered]@{
            channel_id = $channelId
            display_name = "Installed headend output proof $runId"
            output_profile = 'proof-360p30'
            output_sinks = @([ordered]@{
                sink_id = $sinkId
                label = 'Temporary profile-application sink'
                kind = 'udp-ts'
                destination = "127.0.0.1:$port"
                loudness_target_lufs = -16.0
                required = $true
                enabled = $true
            })
            enabled = $false
        }
        Invoke-Api 'Put' '/channels' $proofChannel | Out-Null
        $channelConfigured = $true
        $application = Invoke-Api 'Post' "/channels/$channelId/headend-profile" ([ordered]@{
            profile_id = $HeadendProfileId
            destination_uri = "udp://127.0.0.1:$port"
            muxrate_kbps = $null
            keep_existing_sinks = $false
        })
        if ($application.profile_id -ne $HeadendProfileId -or
            $application.muxrate_kbps -ne 8000 -or
            $application.channel.output_profile -ne 'atsc-720p5994' -or
            $application.channel.enabled) {
            throw 'Applied headend profile did not return the exact stopped 720p/8000 contract.'
        }
        $headendSink = @($application.channel.output_sinks | Where-Object {
            $null -ne $_.headend -and $_.headend.profile_id -eq $HeadendProfileId
        }) | Select-Object -First 1
        if ($null -eq $headendSink -or $headendSink.headend.packet_size_bytes -ne 1316 -or
            $headendSink.loudness_target_lufs -ne $targetLufs) {
            throw 'Applied headend sink did not retain the exact 1316-byte/-24 LUFS contract.'
        }
        $sinkId = [string]$headendSink.sink_id
        $result.sink_id = $sinkId
        $proofChannel = $application.channel
        $proofChannel.enabled = $true
        Invoke-Api 'Put' '/channels' $proofChannel | Out-Null
        $result.evidence.headend_application = $application
    }
    else {
        $proofChannel = [ordered]@{
            channel_id = $channelId
            display_name = "Installed output loudness proof $runId"
            output_profile = 'proof-360p30'
            output_sinks = @([ordered]@{
                sink_id = $sinkId
                label = 'Installed output loudness proof'
                kind = 'udp-ts'
                destination = "127.0.0.1:$port"
                loudness_target_lufs = $targetLufs
                required = $true
                enabled = $true
            })
            enabled = $true
        }
        Invoke-Api 'Put' '/channels' $proofChannel | Out-Null
        $channelConfigured = $true
    }
    $runtimeReady = Wait-Until -Action { Invoke-Api 'Get' '/runtime-safe-to-air' } `
        -Predicate {
            param($value)
            $runtime = Get-ProofRuntime $value
            $null -ne $runtime -and $runtime.state -in @('healthy_fallback', 'on_air')
        } -Description 'proof channel runtime readiness' -Seconds 90
    $result.evidence.proof_channel_runtime_before_schedule = $runtimeReady
    if ($HeadendProfileId) {
        $headendReadiness = Invoke-Api 'Get' "/channels/$channelId/headend-readiness"
        $readinessChannel = @($headendReadiness.channels | Where-Object {
            $_.channel_id -eq $channelId
        }) | Select-Object -First 1
        $readinessSink = @($readinessChannel.sinks | Where-Object {
            $_.sink_id -eq $sinkId
        }) | Select-Object -First 1
        if ($null -eq $readinessSink -or $readinessSink.local_transport -ne 'pass' -or
            $readinessSink.external_receiver -notin @('not_attempted', 'unknown') -or
            $readinessSink.state -eq 'pass' -or $headendReadiness.rollup.color -eq 'green') {
            throw 'Headend readiness did not preserve local progress and the unverified receiver boundary.'
        }
        $result.evidence.headend_readiness = $headendReadiness
        $result.evidence.headend_udp_transport = Measure-HeadendUdp -Receiver $udpReceiver -Seconds 5
    }

    Invoke-Api 'Post' '/assets/ingest' ([ordered]@{ job_id = $ingestJobId; source_path = $fixture.Path }) | Out-Null
    $ingest = Wait-Until -Action { Invoke-Api 'Get' "/assets/ingest/jobs/$ingestJobId" } `
        -Predicate {
            param($value)
            if ($value.status -in @('failed', 'rejected')) { throw 'Installed audible fixture ingest failed.' }
            $value.status -eq 'completed' -and $value.progress_percent -eq 100 -and $null -ne $value.asset_id
        } -Description 'audible fixture ingest completion' -Seconds 180
    $assetId = [string]$ingest.asset_id
    $readiness = Wait-Until -Action { Invoke-Api 'Get' "/assets/$assetId/readiness" } `
        -Predicate {
            param($value)
            if ($value.readiness_state -in @('missing_file', 'rejected')) { throw 'Installed audible fixture was rejected.' }
            $value.readiness_state -eq 'ready'
        } -Description 'audible fixture readiness' -Seconds 180
    if ([int64]$readiness.duration_ms -lt ($sampleDurationMs + 4000)) {
        throw "Fixture duration $($readiness.duration_ms) ms is shorter than the requested proof plus four-second capture headroom."
    }
    $result.evidence.media = [ordered]@{
        fixture_path = $fixture.Path
        fixture_bytes = $fixtureInfo.Length
        ingest_job = $ingest
        asset_id = $assetId
        readiness = $readiness
    }

    $scheduleStart = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + ([int64]$StartLeadSeconds * 1000)
    $item = [ordered]@{
        item_id = $scheduleItemId
        channel_id = $channelId
        asset_id = $assetId
        title = "Installed output loudness proof $runId"
        starts_at_unix_ms = $scheduleStart
        duration_ms = [int64]$readiness.duration_ms
        state = 'draft'
    }
    Invoke-Api 'Put' '/schedule/items' $item | Out-Null
    $plan = Invoke-Api 'Post' '/schedule/prepare' ([ordered]@{ plan_id = $planId; schedule_item_id = $scheduleItemId })
    if (-not $plan.dry_run_passed -or @($plan.conflicts).Count -ne 0 -or $plan.missing_media_detail) {
        throw 'Installed loudness-proof schedule prepare did not pass the media/conflict gate.'
    }
    $report = Invoke-Api 'Post' '/schedule/commit' ([ordered]@{
        report_id = $reportId
        plan_id = $planId
        schedule_item_id = $scheduleItemId
        operator_notes = 'Installed output loudness proof.'
    })
    $dispatch = Wait-Until -Action { Invoke-Api 'Get' "/schedule/commits/$reportId" } `
        -Predicate {
            param($value)
            if ($value.dispatch_status -eq 'error') { throw 'Installed proof dispatch entered error state.' }
            $value.dispatch_status -in @('acknowledged', 'completed')
        } -Description 'proof schedule dispatch acknowledgement' -Seconds 120
    $onAir = Wait-Until -Action { Invoke-Api 'Get' '/runtime-safe-to-air' } `
        -Predicate {
            param($value)
            $runtime = Get-ProofRuntime $value
            $null -ne $runtime -and $runtime.state -eq 'on_air'
        } -Description 'proof fixture on-air runtime' -Seconds 120
    $result.evidence.schedule = [ordered]@{
        draft = $item
        prepared = $plan
        committed = $report
        dispatch = $dispatch
        on_air_runtime = $onAir
    }
    $request = Invoke-Api 'Post' "/channels/$channelId/loudness-proof" ([ordered]@{
        sink_id = $sinkId
        sample_duration_ms = $sampleDurationMs
        max_capture_bytes = $MaxCaptureBytes
    })
    $result.evidence.output_loudness = [ordered]@{
        request = $request
        claimed_job = $null
        completed_job = $null
        per_sink_proof = $null
        receiver_acceptance_boundary = 'not_attempted_or_unknown'
    }
    if ($request.state -ne 'pending' -or $request.channel_id -ne $channelId -or
        $request.sink_id -ne $sinkId -or $request.target_lufs_milli -ne $targetLufsMilli -or
        $request.source_commit -ne $identity.source_commit -or $request.candidate_version -ne $identity.version) {
        throw 'Created loudness-proof job did not bind to the exact proof channel or installed candidate.'
    }
    $configurationHash = [string]$request.configuration_hash
    $jobId = [string]$request.job_id
    try {
        $claimedJob = Wait-Until -Action {
            Invoke-Api 'Get' "/channels/$channelId/loudness-proof/jobs/$jobId"
        } -Predicate {
            param($value)
            $value.state -ne 'pending'
        } -Description 'output loudness proof executor claim' -Seconds $ClaimTimeoutSeconds
        $result.evidence.output_loudness.claimed_job = $claimedJob
    }
    catch {
        $result.evidence.output_loudness.executor_diagnostic = [ordered]@{
            job = Invoke-Api 'Get' "/channels/$channelId/loudness-proof/jobs/$jobId"
            runtime = Invoke-Api 'Get' '/runtime-safe-to-air'
        }
        throw
    }
    $job = Wait-Until -Action { Invoke-Api 'Get' "/channels/$channelId/loudness-proof/jobs/$jobId" } `
        -Predicate {
            param($value)
            $value.state -in @('completed', 'failed')
        } -Description 'installed output loudness proof completion' -Seconds $TimeoutSeconds
    $result.evidence.output_loudness.completed_job = $job
    if ($job.state -eq 'failed') {
        throw "Installed output loudness proof failed: $($job.failure_detail)"
    }
    if ($job.job_id -ne $jobId -or $job.channel_id -ne $channelId -or $job.sink_id -ne $sinkId -or
        $job.target_lufs_milli -ne $targetLufsMilli -or $job.configuration_hash -ne $configurationHash -or
        $job.source_commit -ne $identity.source_commit -or $job.candidate_version -ne $identity.version -or
        $null -eq $job.capture -or $job.capture.sample_duration_ms -lt $sampleDurationMs -or
        $job.capture.capture_bytes -le 0 -or [string]::IsNullOrWhiteSpace($job.capture.capture_sha256)) {
        throw 'Completed loudness-proof job facts did not retain the exact requested identity and capture evidence.'
    }
    $snapshot = Invoke-Api 'Get' "/channels/$channelId/loudness-proof"
    $sinkProof = @($snapshot.sinks | Where-Object { $_.sink_id -eq $sinkId }) | Select-Object -First 1
    if ($null -eq $sinkProof) { throw 'Immutable loudness proof did not expose the proof sink.' }
    $result.evidence.output_loudness.per_sink_proof = $sinkProof
    if ($sinkProof.proof_id -ne $jobId -or $sinkProof.configuration_hash -ne $configurationHash -or
        $sinkProof.source_commit -ne $identity.source_commit -or $sinkProof.candidate_version -ne $identity.version -or
        $sinkProof.sample_duration_ms -ne $job.capture.sample_duration_ms -or
        $sinkProof.capture_bytes -ne $job.capture.capture_bytes -or
        $sinkProof.capture_sha256 -ne $job.capture.capture_sha256 -or
        $sinkProof.configured_target_lufs -ne $targetLufs -or
        [double]$sinkProof.measured_lufs -lt ($targetLufs - 2.0) -or
        [double]$sinkProof.measured_lufs -gt ($targetLufs + 2.0) -or
        $sinkProof.receiver_acceptance -notin @('not_attempted', 'unknown')) {
        throw 'Immutable per-sink proof is not bound to the completed job, candidate, configuration, or receiver-acceptance boundary.'
    }
    if ($sinkProof.measurement_status -ne 'pass') {
        throw "Installed loudness proof did not pass: $($sinkProof.measurement_status)."
    }
    $result.status = 'passed'
}
catch {
    $result.status = 'failed'
    $result.failure = $_.Exception.Message
    $primaryFailure = $_.Exception
}
finally {
    if ($channelConfigured -and $proofChannel) {
        try {
            $proofChannel.enabled = $false
            Invoke-Api 'Put' '/channels' $proofChannel | Out-Null
            $storedChannels = @(Invoke-Api 'Get' '/channels')
            $storedProofChannel = @($storedChannels | Where-Object {
                $_.channel_id -eq $channelId
            }) | Select-Object -First 1
            if ($null -eq $storedProofChannel -or $storedProofChannel.enabled) {
                throw 'The proof channel disable operation was not confirmed by public API readback.'
            }
            $result.evidence.cleanup = [ordered]@{
                channel_id = $channelId
                disabled = $true
                verified_by = 'GET /api/v1/channels'
            }
        }
        catch {
            $cleanupFailure = $_.Exception
            $result.status = 'failed'
            $result.cleanup_failure = $_.Exception.Message
        }
    }
    if ($null -ne $udpReceiver) { $udpReceiver.Dispose() }
    $result.finished_at_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    if ($ResultPath) {
        $parent = Split-Path -Parent $ResultPath
        if ($parent -and -not (Test-Path -LiteralPath $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        $result | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $ResultPath -Encoding utf8
    }
    elseif ($result.status -eq 'passed') {
        $result | ConvertTo-Json -Depth 30
    }
}
if ($null -ne $primaryFailure) { throw $primaryFailure }
if ($null -ne $cleanupFailure) { throw $cleanupFailure }
