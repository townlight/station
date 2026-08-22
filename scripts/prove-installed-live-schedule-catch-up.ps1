<#
.SYNOPSIS
    Proves installed schedule catch-up after a live takeover crosses an item boundary.

.DESCRIPTION
    This is an installed-machine proof.  It creates two distinct proof-owned A/V
    fixtures, admits and schedules them through the public API, observes the output
    over loopback UDP, takes a live source across the A-to-B boundary, and verifies
    that returning at a positive offset resumes item B. It reads the installed
    receipt only to authenticate candidate identity; it never reads or writes the
    station database, worker journal, or station runtime state directly.
#>
param(
    [string]$BaseUri = 'http://127.0.0.1:4070',
    [string]$Manifest = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\candidate-manifest.json'),
    [ValidateRange(1024, 65535)][int]$InputPort = 5972,
    [ValidateRange(1024, 65535)][int]$OutputPort = 5973,
    [int]$TimeoutSeconds = 180,
    [int]$ItemDurationSeconds = 12,
    [string]$ResultPath = '',
    [switch]$KeepCapture
)

$ErrorActionPreference = 'Stop'
$serviceUri = $BaseUri.TrimEnd('/') -replace '/api/v1$', ''
$apiUri = "$serviceUri/api/v1"
$runId = [guid]::NewGuid().ToString()
$channelId = [guid]::NewGuid().ToString()
$sourceId = [guid]::NewGuid().ToString()
$sinkId = [guid]::NewGuid().ToString()
$sessionId = [guid]::NewGuid().ToString()
$itemAId = [guid]::NewGuid().ToString()
$itemBId = [guid]::NewGuid().ToString()
$calibrationItemId = [guid]::NewGuid().ToString()
$calibrationPlanId = "catch-up-calibration-$runId"
$calibrationReportId = "catch-up-report-calibration-$runId"
$planAId = "catch-up-a-$runId"
$planBId = "catch-up-b-$runId"
$reportAId = "catch-up-report-a-$runId"
$reportBId = "catch-up-report-b-$runId"
$operatorId = 'installed-live-schedule-catch-up-proof'
$reason = 'Installed live schedule catch-up proof.'
$captureRoot = Join-Path ([IO.Path]::GetTempPath()) "townlight-live-schedule-catch-up-$PID-$runId"
$fixtureA = Join-Path $captureRoot 'item-a.ts'
$fixtureB = Join-Path $captureRoot 'item-b.ts'
$calibrationCapture = Join-Path $captureRoot 'item-b-calibration.ts'
$beforeCapture = Join-Path $captureRoot 'before-live.ts'
$liveCapture = Join-Path $captureRoot 'during-live.ts'
$afterCapture = Join-Path $captureRoot 'after-return.ts'
$receiver = $null
$sender = $null
$proofChannel = $null
$source = $null
$channelConfigured = $false
$sourceConfigured = $false
$mutex = [Threading.Mutex]::new($false, 'Global\TownLightStationInstalledLiveScheduleCatchUpProof')
$mutexHeld = $false
$result = [ordered]@{
    run_id = $runId
    channel_id = $channelId
    source_id = $sourceId
    session_id = $sessionId
    started_at_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    status = 'not_run'
    evidence = [ordered]@{}
}

function Invoke-Api {
    param(
        [ValidateSet('Get', 'Put', 'Post')][string]$Method,
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
        $value = Invoke-RestMethod @parameters
        if ($value -is [string] -and $value.Trim() -eq 'null') { return $null }
        return $value
    }
    catch {
        throw "Installed API request failed: $Method $Path ($($_.Exception.GetType().Name))."
    }
}

function Wait-For {
    param(
        [scriptblock]$Action,
        [scriptblock]$Predicate,
        [string]$Description,
        [int]$Seconds = $TimeoutSeconds
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $value = & $Action
            if (& $Predicate $value) { return $value }
        }
        catch { }
        Start-Sleep -Milliseconds 250
    }
    throw "Timed out waiting for $Description."
}

function Invoke-Native([string]$FilePath, [string[]]$Arguments) {
    $id = [guid]::NewGuid().ToString('N')
    $stdoutPath = Join-Path $captureRoot "native-$id.stdout"
    $stderrPath = Join-Path $captureRoot "native-$id.stderr"
    try {
        $commandLine = (($Arguments | ForEach-Object {
            $argument = [string]$_
            if ($argument -notmatch '[\s"]') { return $argument }
            $escaped = $argument -replace '(\\*)"', '$1$1\"'
            $escaped = $escaped -replace '(\\+)$', '$1$1'
            return '"' + $escaped + '"'
        }) -join ' ')
        $process = Start-Process -FilePath $FilePath -ArgumentList $commandLine -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        return [pscustomobject]@{
            exit_code = $process.ExitCode
            stdout = Get-Content -LiteralPath $stdoutPath -Raw -ErrorAction SilentlyContinue
            stderr = Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue
        }
    }
    finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

function Assert-UdpPortUnowned([int]$Port, [int]$AllowedPid = 0) {
    $owners = @(Get-NetUDPEndpoint -LocalPort $Port -ErrorAction SilentlyContinue |
        Where-Object { $AllowedPid -eq 0 -or $_.OwningProcess -ne $AllowedPid })
    if ($owners.Count -ne 0) {
        throw "UDP port $Port already has an owner; refusing ambiguous proof-port ownership."
    }
}

function New-Fixture([string]$Path, [string]$Pattern, [int]$Frequency) {
    $ffmpeg = (Get-Command ffmpeg.exe -ErrorAction Stop).Source
    $videoSource = "color=c=${Pattern}:size=320x180:rate=30"
    $args = @('-y', '-v', 'error', '-f', 'lavfi', '-i', $videoSource,
        '-f', 'lavfi', '-i', "sine=frequency=${Frequency}:sample_rate=48000", '-t', $ItemDurationSeconds,
        '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '30', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '128k', '-f', 'mpegts', $Path)
    $run = Invoke-Native $ffmpeg $args
    if ($run.exit_code -ne 0 -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Could not create proof fixture $Path. $($run.stderr)"
    }
}

function Capture-Udp([int]$Seconds, [string]$Path, [int]$MinimumBytes = 100000) {
    $memory = [IO.MemoryStream]::new()
    $remote = [Net.IPEndPoint]::new([Net.IPAddress]::Any, 0)
    $clock = [Diagnostics.Stopwatch]::StartNew()
    try {
        while ($clock.Elapsed.TotalSeconds -lt $Seconds) {
            while ($receiver.Available -gt 0) {
                $datagram = $receiver.Receive([ref]$remote)
                if ($datagram.Length -lt 188 -or $datagram.Length % 188 -ne 0) {
                    throw "Proof output sent a non-MPEG-TS UDP datagram ($($datagram.Length) bytes)."
                }
                $memory.Write($datagram, 0, $datagram.Length)
            }
            Start-Sleep -Milliseconds 1
        }
        $bytes = $memory.ToArray()
        if ($bytes.Length -lt $MinimumBytes) { throw "Captured only $($bytes.Length) bytes of installed UDP output." }
        for ($offset = 0; $offset + 188 -le $bytes.Length; $offset += 188) {
            if ($bytes[$offset] -ne 0x47) { throw "Captured output lost MPEG-TS sync at byte $offset." }
        }
        [IO.File]::WriteAllBytes($Path, $bytes)
        return [ordered]@{ path = $Path; bytes = $bytes.Length; seconds = [math]::Round($clock.Elapsed.TotalSeconds, 3) }
    }
    finally { $memory.Dispose() }
}

function Clear-UdpBacklog {
    $remote = [Net.IPEndPoint]::new([Net.IPAddress]::Any, 0)
    while ($receiver.Available -gt 0) { [void]$receiver.Receive([ref]$remote) }
}

function Get-VideoFingerprint([string]$Path) {
    $ffmpeg = (Get-Command ffmpeg.exe -ErrorAction Stop).Source
    $probe = Invoke-Native $ffmpeg @('-v', 'error', '-i', $Path, '-map', '0:v:0', '-frames:v', '1', '-f', 'md5', '-')
    if ($probe.exit_code -ne 0 -or $probe.stdout -notmatch 'MD5=([0-9a-fA-F]{32})') {
        throw "FFmpeg could not fingerprint proof output $Path. $($probe.stderr)"
    }
    return $Matches[1].ToLowerInvariant()
}

function Get-ColorEvidence([string]$Path) {
    $ffmpeg = (Get-Command ffmpeg.exe -ErrorAction Stop).Source
    $sample = Invoke-Native $ffmpeg @('-v', 'error', '-i', $Path, '-frames:v', '1', '-vf', 'signalstats,metadata=print:file=-', '-f', 'null', 'NUL')
    if ($sample.exit_code -ne 0 -or $sample.stdout -notmatch 'lavfi\.signalstats\.YAVG=([0-9.]+)') {
        throw "FFmpeg could not measure proof video color in $Path. $($sample.stderr)"
    }
    $y = [double]$Matches[1]
    if ($sample.stdout -notmatch 'lavfi\.signalstats\.UAVG=([0-9.]+)') { throw "Proof video omitted UAVG color evidence in $Path." }
    $u = [double]$Matches[1]
    if ($sample.stdout -notmatch 'lavfi\.signalstats\.VAVG=([0-9.]+)') { throw "Proof video omitted VAVG color evidence in $Path." }
    $v = [double]$Matches[1]
    $classification = if ($u -gt $v + 40) { 'blue' } elseif ($v -gt $u + 40) { 'red' } elseif ($u -lt 100 -and $v -lt 100) { 'green' } else { 'unknown' }
    return [ordered]@{ classification = $classification; y_average = $y; u_average = $u; v_average = $v }
}

function Get-DecodedEvidence([string]$Path) {
    $ffprobe = (Get-Command ffprobe.exe -ErrorAction Stop).Source
    $ffmpeg = (Get-Command ffmpeg.exe -ErrorAction Stop).Source
    $probe = Invoke-Native $ffprobe @('-v', 'error', '-count_frames', '-show_entries', 'stream=codec_type,codec_name,nb_read_frames', '-of', 'json', $Path)
    if ($probe.exit_code -ne 0) { throw "FFprobe could not inspect proof output $Path. $($probe.stderr)" }
    $streams = ($probe.stdout | ConvertFrom-Json).streams
    $video = @($streams | Where-Object codec_type -eq 'video')[0]
    $audio = @($streams | Where-Object codec_type -eq 'audio')[0]
    if (-not $video -or -not $audio -or [int]$video.nb_read_frames -lt 30 -or [int]$audio.nb_read_frames -lt 30) {
        throw "Proof output did not contain continuous decodable H.264/AAC audio and video."
    }
    if ($video.codec_name -ne 'h264' -or $audio.codec_name -ne 'aac') {
        throw "Proof output codecs were not H.264/AAC ($($video.codec_name)/$($audio.codec_name))."
    }
    $volume = Invoke-Native $ffmpeg @('-v', 'info', '-i', $Path, '-t', '2', '-map', '0:a:0', '-af', 'volumedetect', '-f', 'null', 'NUL')
    if ($volume.exit_code -ne 0 -or $volume.stderr -notmatch 'max_volume:\s*(-?[0-9.]+) dB') {
        throw "FFmpeg could not measure proof audio in $Path."
    }
    $maxVolume = [double]$Matches[1]
    if ($maxVolume -lt -60) { throw "Proof audio was effectively silent ($maxVolume dB)." }
    return [ordered]@{
        video_codec = $video.codec_name
        audio_codec = $audio.codec_name
        decoded_video_frames = [int]$video.nb_read_frames
        decoded_audio_frames = [int]$audio.nb_read_frames
        max_audio_db = $maxVolume
    }
}

function Get-ProofRuntime {
    return (Invoke-Api 'Get' '/runtime-safe-to-air').channels |
        Where-Object channel_id -eq $channelId | Select-Object -First 1
}

function Stop-Sender {
    if ($sender) {
        if (-not $sender.HasExited) {
            Stop-Process -Id $sender.Id -Force -ErrorAction SilentlyContinue
            $sender.WaitForExit(5000) | Out-Null
        }
        $sender.Dispose()
        $sender = $null
    }
}

try {
    if ($TimeoutSeconds -lt 60 -or $TimeoutSeconds -gt 900) { throw 'TimeoutSeconds must be between 60 and 900.' }
    if ($ItemDurationSeconds -lt 10 -or $ItemDurationSeconds -gt 30) { throw 'ItemDurationSeconds must be between 10 and 30.' }
    if ($InputPort -eq $OutputPort) { throw 'InputPort and OutputPort must be different.' }
    $baseAddress = [Uri]$serviceUri
    if ($baseAddress.Host -notin @('127.0.0.1', '::1', 'localhost')) { throw 'BaseUri must be an installed loopback API address.' }
    $mutexHeld = $mutex.WaitOne(0)
    if (-not $mutexHeld) { throw 'Another installed schedule catch-up proof is already running.' }
    New-Item -ItemType Directory -Path $captureRoot -Force | Out-Null

    $manifestPath = Resolve-Path -LiteralPath $Manifest -ErrorAction Stop
    $manifestDocument = Get-Content -LiteralPath $manifestPath.Path -Raw | ConvertFrom-Json
    $receiptPath = Join-Path $env:ProgramData 'TownLight Station\install-receipt.json'
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    $applicationPath = Join-Path $env:ProgramFiles 'TownLight Station'
    $stationdHash = (Get-FileHash -LiteralPath (Join-Path $applicationPath 'stationd.exe') -Algorithm SHA256).Hash
    $workerHash = (Get-FileHash -LiteralPath (Join-Path $applicationPath 'channel-worker.exe') -Algorithm SHA256).Hash
    $updaterHash = (Get-FileHash -LiteralPath (Join-Path $applicationPath 'station-updater.exe') -Algorithm SHA256).Hash
    if ($receipt.source_commit -ne $manifestDocument.source_commit -or
        $stationdHash -ne $manifestDocument.stationd_sha256 -or
        $workerHash -ne $manifestDocument.channel_worker_sha256 -or
        $updaterHash -ne $manifestDocument.station_updater_sha256 -or
        $receipt.media_runtime_sha256 -ne $manifestDocument.media_runtime_sha256) {
        throw 'Installed receipt or executable hashes do not identify the candidate manifest.'
    }
    $result.evidence.installed_identity = [ordered]@{
        source_commit = $receipt.source_commit
        version = $receipt.version
        stationd_sha256 = $stationdHash
        channel_worker_sha256 = $workerHash
        station_updater_sha256 = $updaterHash
        media_runtime_sha256 = $receipt.media_runtime_sha256
        candidate_manifest_path = $manifestPath.Path
    }

    $health = Wait-For -Action { Invoke-RestMethod -Uri "$serviceUri/health" -TimeoutSec 5 } `
        -Predicate { param($value) $value.status -eq 'ready' -and $value.database -eq 'ready' } `
        -Description 'installed database health' -Seconds 30
    New-Fixture $fixtureA 'red' 697
    New-Fixture $fixtureB 'blue' 997

    Assert-UdpPortUnowned $InputPort
    Assert-UdpPortUnowned $OutputPort
    $receiver = [Net.Sockets.UdpClient]::new([Net.IPEndPoint]::new([Net.IPAddress]::Loopback, $OutputPort))
    $receiver.Client.ReceiveBufferSize = 8388608
    Assert-UdpPortUnowned $OutputPort $PID
    $proofChannel = [ordered]@{
        channel_id = $channelId; display_name = "Installed live schedule catch-up proof $runId"; output_profile = 'proof-360p30'
        output_sinks = @([ordered]@{ sink_id = $sinkId; label = 'Installed live schedule catch-up output'; kind = 'udp-ts'; destination = "127.0.0.1:$OutputPort"; loudness_target_lufs = -16.0; required = $true; enabled = $true })
        enabled = $true
    }
    Invoke-Api 'Put' '/channels' $proofChannel | Out-Null
    $channelConfigured = $true
    Wait-For -Action { Get-ProofRuntime } -Predicate { param($value) $null -ne $value -and $value.state -in @('healthy_fallback', 'on_air') } `
        -Description 'proof channel runtime readiness' -Seconds 90 | Out-Null

    $source = [ordered]@{
        source_id = $sourceId; channel_id = $channelId; label = 'Installed live schedule catch-up input'; enabled = $true
        endpoint = [ordered]@{ UdpMpegTs = [ordered]@{ bind_ip = '127.0.0.1'; port = $InputPort } }; revision = 1; expected_revision = 0
    }
    Invoke-Api 'Put' "/live-sources/$sourceId" $source | Out-Null
    $sourceConfigured = $true

    $ingestAId = [guid]::NewGuid().ToString()
    $ingestBId = [guid]::NewGuid().ToString()
    Invoke-Api 'Post' '/assets/ingest' ([ordered]@{ job_id = $ingestAId; source_path = $fixtureA }) | Out-Null
    Invoke-Api 'Post' '/assets/ingest' ([ordered]@{ job_id = $ingestBId; source_path = $fixtureB }) | Out-Null
    $jobA = Wait-For -Action { Invoke-Api 'Get' "/assets/ingest/jobs/$ingestAId" } -Predicate { param($value) if ($value.status -in @('failed', 'rejected')) { throw 'Fixture A ingest failed.' }; $value.status -eq 'completed' -and $null -ne $value.asset_id } -Description 'fixture A ingest' -Seconds 120
    $jobB = Wait-For -Action { Invoke-Api 'Get' "/assets/ingest/jobs/$ingestBId" } -Predicate { param($value) if ($value.status -in @('failed', 'rejected')) { throw 'Fixture B ingest failed.' }; $value.status -eq 'completed' -and $null -ne $value.asset_id } -Description 'fixture B ingest' -Seconds 120
    $assetA = [string]$jobA.asset_id; $assetB = [string]$jobB.asset_id
    Wait-For -Action { Invoke-Api 'Get' "/assets/$assetA/readiness" } -Predicate { param($value) $value.readiness_state -eq 'ready' } -Description 'fixture A readiness' -Seconds 120 | Out-Null
    Wait-For -Action { Invoke-Api 'Get' "/assets/$assetB/readiness" } -Predicate { param($value) $value.readiness_state -eq 'ready' } -Description 'fixture B readiness' -Seconds 120 | Out-Null

    $calibrationStart = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + 8000
    $calibrationDuration = 4000
    $startA = $calibrationStart + $calibrationDuration + 3000
    $startB = $startA + ([int64]$ItemDurationSeconds * 1000)
    foreach ($item in @(
        [ordered]@{ item_id = $calibrationItemId; channel_id = $channelId; asset_id = $assetB; title = "Item B transport calibration $runId"; starts_at_unix_ms = $calibrationStart; duration_ms = $calibrationDuration; state = 'draft' },
        [ordered]@{ item_id = $itemAId; channel_id = $channelId; asset_id = $assetA; title = "Catch-up item A $runId"; starts_at_unix_ms = $startA; duration_ms = [int64]$ItemDurationSeconds * 1000; state = 'draft' },
        [ordered]@{ item_id = $itemBId; channel_id = $channelId; asset_id = $assetB; title = "Catch-up item B $runId"; starts_at_unix_ms = $startB; duration_ms = [int64]$ItemDurationSeconds * 1000; state = 'draft' }
    )) { Invoke-Api 'Put' '/schedule/items' $item | Out-Null }
    $calibrationPlan = Invoke-Api 'Post' '/schedule/prepare' ([ordered]@{ plan_id = $calibrationPlanId; schedule_item_id = $calibrationItemId })
    $planA = Invoke-Api 'Post' '/schedule/prepare' ([ordered]@{ plan_id = $planAId; schedule_item_id = $itemAId })
    $planB = Invoke-Api 'Post' '/schedule/prepare' ([ordered]@{ plan_id = $planBId; schedule_item_id = $itemBId })
    if (-not $calibrationPlan.dry_run_passed -or -not $planA.dry_run_passed -or -not $planB.dry_run_passed) { throw 'Schedule prepare rejected one of the proof items.' }
    Invoke-Api 'Post' '/schedule/commit' ([ordered]@{ report_id = $calibrationReportId; plan_id = $calibrationPlanId; schedule_item_id = $calibrationItemId; approved_by = $operatorId; operator_notes = 'Independent item B transport calibration.' }) | Out-Null
    Invoke-Api 'Post' '/schedule/commit' ([ordered]@{ report_id = $reportAId; plan_id = $planAId; schedule_item_id = $itemAId; approved_by = $operatorId; operator_notes = 'Installed catch-up item A.' }) | Out-Null
    Invoke-Api 'Post' '/schedule/commit' ([ordered]@{ report_id = $reportBId; plan_id = $planBId; schedule_item_id = $itemBId; approved_by = $operatorId; operator_notes = 'Installed catch-up item B.' }) | Out-Null

    while ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() -lt $calibrationStart) { Start-Sleep -Milliseconds 100 }
    Wait-For -Action { Invoke-Api 'Get' "/schedule/commits/$calibrationReportId" } -Predicate { param($value) $value.dispatch_status -eq 'acknowledged' } -Description 'independent item B calibration on air' -Seconds 30 | Out-Null
    Clear-UdpBacklog
    $calibrationTransport = Capture-Udp 2 $calibrationCapture
    $calibrationFingerprint = Get-VideoFingerprint $calibrationCapture
    $calibrationColor = Get-ColorEvidence $calibrationCapture
    $calibrationDecoded = Get-DecodedEvidence $calibrationCapture
    if ($calibrationColor.classification -ne 'blue') { throw 'Independent item B calibration was not blue.' }
    Wait-For -Action { Invoke-Api 'Get' "/schedule/commits/$calibrationReportId" } -Predicate { param($value) $value.dispatch_status -eq 'completed' } -Description 'item B calibration completion' -Seconds 30 | Out-Null

    while ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() -lt $startA) { Start-Sleep -Milliseconds 100 }
    $statusA = Wait-For -Action { Invoke-Api 'Get' "/schedule/commits/$reportAId" } -Predicate { param($value) $value.dispatch_status -in @('acknowledged', 'completed') } -Description 'item A on-air dispatch' -Seconds 90
    Clear-UdpBacklog
    $beforeTransport = Capture-Udp 2 $beforeCapture
    $beforeFingerprint = Get-VideoFingerprint $beforeCapture
    $beforeColor = Get-ColorEvidence $beforeCapture
    $beforeDecoded = Get-DecodedEvidence $beforeCapture
    if ($beforeColor.classification -ne 'red') { throw 'Scheduled item A output was not red.' }

    $ffmpeg = (Get-Command ffmpeg.exe -ErrorAction Stop).Source
    $sender = Start-Process -FilePath $ffmpeg -ArgumentList @('-re', '-f', 'lavfi', '-i', 'color=c=green:size=640x360:rate=30', '-re', '-f', 'lavfi', '-i', 'sine=frequency=1231:sample_rate=48000', '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency', '-g', '30', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k', '-f', 'mpegts', "udp://127.0.0.1:${InputPort}?pkt_size=1316") -PassThru -WindowStyle Hidden
    Start-Sleep -Milliseconds 750
    if ($sender.HasExited) { throw "FFmpeg live sender exited early with code $($sender.ExitCode)." }
    $authority = Invoke-Api 'Get' "/channels/$channelId/live-authority"
    $expires = [DateTimeOffset]::UtcNow.AddMinutes(5).ToUnixTimeMilliseconds()
    Invoke-Api 'Post' '/live-takeovers' ([ordered]@{ session_id = $sessionId; channel_id = $channelId; source_id = $sourceId; operator_id = $operatorId; reason = $reason; expires_at_unix_ms = $expires; expected_authority_revision = [uint64]$authority.authority_revision }) | Out-Null
    Wait-For -Action { Invoke-Api 'Get' "/live-takeovers/$sessionId" } -Predicate { param($value) if ($value.state -eq 'failed') { throw 'Takeover arming failed.' }; $value.state -eq 'armed' } -Description 'live takeover armed' | Out-Null
    $authority = Invoke-Api 'Get' "/channels/$channelId/live-authority"
    Invoke-Api 'Post' "/live-takeovers/$sessionId/take" ([ordered]@{ actor_id = $operatorId; expected_authority_revision = [uint64]$authority.authority_revision }) | Out-Null
    $onAir = Wait-For -Action { Invoke-Api 'Get' "/live-takeovers/$sessionId" } -Predicate { param($value) if ($value.state -eq 'failed') { throw 'Takeover take failed.' }; $value.state -eq 'on_air' } -Description 'live takeover on air'
    $runtimeLive = Wait-For -Action { Get-ProofRuntime } -Predicate { param($value) $value.state -eq 'live' } -Description 'runtime live state' -Seconds 90
    if ([int64]$onAir.taken_at_unix_ms -ge $startB) {
        throw 'Live takeover did not reach air before the adjacent schedule boundary.'
    }
    Clear-UdpBacklog
    $liveTransport = Capture-Udp 2 $liveCapture
    $liveFingerprint = Get-VideoFingerprint $liveCapture
    $liveColor = Get-ColorEvidence $liveCapture
    $liveDecoded = Get-DecodedEvidence $liveCapture
    if ($liveColor.classification -ne 'green' -or $liveFingerprint -eq $beforeFingerprint) { throw 'Live output did not prove the distinct green source.' }

    $returnAt = $startB + 2000
    while ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() -lt $returnAt) { Start-Sleep -Milliseconds 100 }
    $beforeReturnA = Invoke-Api 'Get' "/schedule/commits/$reportAId"
    $beforeReturnB = Invoke-Api 'Get' "/schedule/commits/$reportBId"
    if ($beforeReturnA.dispatch_status -ne 'acknowledged' -or $beforeReturnB.dispatch_status -ne 'pending') {
        throw "Live did not block schedule dispatch at the boundary (A=$($beforeReturnA.dispatch_status), B=$($beforeReturnB.dispatch_status))."
    }
    $authority = Invoke-Api 'Get' "/channels/$channelId/live-authority"
    Invoke-Api 'Post' "/live-takeovers/$sessionId/return" ([ordered]@{ actor_id = $operatorId; expected_authority_revision = [uint64]$authority.authority_revision }) | Out-Null
    $completed = Wait-For -Action { Invoke-Api 'Get' "/live-takeovers/$sessionId" } -Predicate { param($value) if ($value.state -eq 'failed') { throw 'Takeover return failed.' }; $value.state -eq 'completed' -and $null -ne $value.returned_at_unix_ms } -Description 'live takeover completed' -Seconds 90
    $runtimeAfter = Wait-For -Action { Get-ProofRuntime } -Predicate { param($value) $value.state -eq 'on_air' } -Description 'runtime after live return to scheduled item B' -Seconds 90
    $statusA = Wait-For -Action { Invoke-Api 'Get' "/schedule/commits/$reportAId" } -Predicate { param($value) $value.dispatch_status -eq 'completed' } -Description 'item A retired after live return' -Seconds 90
    $statusB = Wait-For -Action { Invoke-Api 'Get' "/schedule/commits/$reportBId" } -Predicate { param($value) $value.dispatch_status -eq 'acknowledged' } -Description 'item B resumed on air' -Seconds 90
    Clear-UdpBacklog
    $afterTransport = Capture-Udp 3 $afterCapture
    $afterFingerprint = Get-VideoFingerprint $afterCapture
    $afterColor = Get-ColorEvidence $afterCapture
    $afterDecoded = Get-DecodedEvidence $afterCapture
    if ($afterColor.classification -ne 'blue') { throw 'Post-return output did not match the independently proven blue item B identity.' }
    if ([int64]$completed.returned_at_unix_ms -lt $startB) { throw 'Live return did not occur after item B began.' }
    $audit = Invoke-Api 'Get' "/live-takeovers/$sessionId/audit"
    $states = @($audit | ForEach-Object state)
    foreach ($required in @('arming', 'armed', 'take_requested', 'on_air', 'return_requested', 'returning', 'completed')) { if ($states -notcontains $required) { throw "Takeover audit omitted state $required." } }
    $completedAudit = @($audit | Where-Object state -eq 'completed')[-1]
    $escapedAssetB = [regex]::Escape($assetB)
    if ($null -eq $completedAudit -or $completedAudit.detail -notmatch "^Returned to scheduled asset $escapedAssetB at media offset ([0-9]+) ms\.$") {
        throw 'Completion audit did not identify item B and its exact media offset.'
    }
    $auditedOffset = [int64]$Matches[1]
    $observedOffset = [int64]$completed.returned_at_unix_ms - $startB
    if ($auditedOffset -le 0 -or [math]::Abs($auditedOffset - $observedOffset) -gt 2500) {
        throw "Audited media offset $auditedOffset ms does not match the wall-clock return offset $observedOffset ms."
    }
    $result.evidence = [ordered]@{
        installed_identity = $result.evidence.installed_identity
        health = $health
        schedule = [ordered]@{ calibration_start_unix_ms = $calibrationStart; item_a_start_unix_ms = $startA; item_b_start_unix_ms = $startB; live_boundary_item_a_report = $beforeReturnA; live_boundary_item_b_report = $beforeReturnB; item_a_report = $statusA; item_b_report = $statusB; audited_return_offset_ms = $auditedOffset; observed_return_offset_ms = $observedOffset }
        takeover = [ordered]@{ on_air = $onAir; completed = $completed; runtime_live = $runtimeLive.state; runtime_after = $runtimeAfter.state; audit_states = $states; audit = $audit }
        transport = [ordered]@{ item_b_calibration = $calibrationTransport; before = $beforeTransport; live = $liveTransport; after = $afterTransport; calibration_decoded = $calibrationDecoded; before_decoded = $beforeDecoded; live_decoded = $liveDecoded; after_decoded = $afterDecoded; calibration_color = $calibrationColor; item_a_color = $beforeColor; live_color = $liveColor; returned_item_b_color = $afterColor; calibrated_item_b_video_md5 = $calibrationFingerprint; item_a_video_md5 = $beforeFingerprint; live_video_md5 = $liveFingerprint; returned_item_b_video_md5 = $afterFingerprint; calibrated_item_b_match = $true }
    }
    $result.status = 'passed'
}
catch {
    $result.status = 'failed'
    $result.failure = $_.Exception.Message
    throw
}
finally {
    if ($channelConfigured) {
        try {
            $active = Invoke-Api 'Get' "/channels/$channelId/active-live-takeover"
            if ($null -ne $active -and $active.state -eq 'on_air') {
                $authority = Invoke-Api 'Get' "/channels/$channelId/live-authority"
                Invoke-Api 'Post' "/live-takeovers/$($active.session_id)/return" ([ordered]@{ actor_id = $operatorId; expected_authority_revision = [uint64]$authority.authority_revision }) | Out-Null
                $active = Invoke-Api 'Get' "/live-takeovers/$($active.session_id)"
            }
            if ($null -ne $active -and $active.state -in @('return_requested', 'returning')) {
                Wait-For -Action { Invoke-Api 'Get' "/live-takeovers/$($active.session_id)" } -Predicate { param($value) $value.state -in @('completed', 'failed') } -Description 'proof takeover cleanup' -Seconds 30 | Out-Null
            }
            elseif ($null -ne $active -and $active.state -in @('arming', 'armed', 'take_requested')) {
                $proofChannel.enabled = $false
                Invoke-Api 'Put' '/channels' $proofChannel | Out-Null
                Wait-For -Action { Invoke-Api 'Get' "/live-takeovers/$($active.session_id)" } -Predicate { param($value) $value.state -eq 'failed' } -Description 'pre-take proof takeover cleanup' -Seconds 30 | Out-Null
            }
        }
        catch { Write-Warning "Could not return an active proof takeover: $($_.Exception.Message)" }
    }
    Stop-Sender
    if ($sourceConfigured -and $source) {
        try { $source.enabled = $false; $source.expected_revision = $source.revision; $source.revision = [uint64]$source.revision + 1; Invoke-Api 'Put' "/live-sources/$sourceId" $source | Out-Null } catch { Write-Warning "Could not disable proof source: $($_.Exception.Message)" }
    }
    if ($channelConfigured -and $proofChannel) {
        try { $proofChannel.enabled = $false; Invoke-Api 'Put' '/channels' $proofChannel | Out-Null } catch { Write-Warning "Could not disable proof channel: $($_.Exception.Message)" }
    }
    if ($receiver) { $receiver.Dispose() }
    $result.finished_at_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    if ($ResultPath) {
        $parent = Split-Path -Parent $ResultPath
        if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        $result | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $ResultPath -Encoding utf8
    }
    elseif ($result.status -eq 'passed') { $result | ConvertTo-Json -Depth 30 }
    if (-not $KeepCapture) { Remove-Item -LiteralPath $captureRoot -Recurse -Force -ErrorAction SilentlyContinue }
    if ($mutexHeld) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
