<#
.SYNOPSIS
    Proves the installed live-takeover path using only a proof-owned loopback channel.

.DESCRIPTION
    This script is deliberately an installed-machine harness: all station mutations
    travel through the loopback API, and it never reads or writes the station database
    or ProgramData directly.  The channel and source UUIDs below are reserved for this
    harness. It reads the installer receipt for identity proof but never reads or
    writes the station database or mutates ProgramData directly. An existing object
    with either UUID must exactly match the proof shape;
    otherwise the run refuses to guess who owns it.
#>
param(
    [Parameter(Mandatory = $true)]
    [Management.Automation.PSCredential]$Credential,
    [string]$BaseUri = 'http://127.0.0.1:4070',
    [string]$Manifest = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\candidate-manifest.json'),
    [ValidateRange(1024, 65535)][int]$InputPort = 5962,
    [ValidateRange(1024, 65535)][int]$OutputPort = 5963,
    [int]$TimeoutSeconds = 90,
    [int]$CaptureSeconds = 4,
    [string]$ResultPath = '',
    [switch]$KeepCapture
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
$channelId = 'b0177d6f-58d3-4ad0-9ad9-ec18bb4d7d13'
$sourceId = 'a1177d6f-58d3-4ad0-9ad9-ec18bb4d7d13'
$sinkId = 'c1177d6f-58d3-4ad0-9ad9-ec18bb4d7d13'
$reason = 'Installed loopback live-takeover proof.'
$runId = [guid]::NewGuid().ToString()
$sessionId = [guid]::NewGuid().ToString()
$inputPortNumber = 0
$outputPortNumber = 0
$receiver = $null
$sender = $null
$proofChannel = $null
$source = $null
$sourceConfigured = $false
$channelConfigured = $false
$mutex = [Threading.Mutex]::new($false, 'Global\TownLightStationInstalledLiveTakeoverProof')
$mutexHeld = $false
$captureRoot = Join-Path ([IO.Path]::GetTempPath()) "townlight-live-takeover-$PID-$runId"
$fallbackCapture = Join-Path $captureRoot 'fallback.ts'
$liveCapture = Join-Path $captureRoot 'live.ts'
$recoveryCapture = Join-Path $captureRoot 'recovery.ts'
$result = [ordered]@{
    run_id = $runId
    session_id = $sessionId
    channel_id = $channelId
    source_id = $sourceId
    started_at_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    status = 'not_run'
    evidence = [ordered]@{}
}

function Invoke-Api {
    param(
        [ValidateSet('Get', 'Put', 'Post')][string]$Method,
        [string]$Path,
        $Body = $null,
        [int]$Timeout = 15
    )
    try {
        $parameters = @{ Method = $Method; Uri = "$apiUri$Path"; TimeoutSec = $Timeout }
        if ($null -ne $Body) {
            $parameters.ContentType = 'application/json'
            $parameters.Body = $Body | ConvertTo-Json -Depth 12 -Compress
        }
        $response = Invoke-InstalledProofApi @parameters
        if ($response -is [string] -and $response.Trim() -eq 'null') { return $null }
        return $response
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
    $last = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        $last = & $Action
        if (& $Predicate $last) { return $last }
        Start-Sleep -Milliseconds 200
    }
    throw "Timed out waiting for $Description."
}

function Get-FreeLoopbackUdpPort {
    $probe = [Net.Sockets.UdpClient]::new([Net.IPEndPoint]::new([Net.IPAddress]::Loopback, 0))
    try { return ([Net.IPEndPoint]$probe.Client.LocalEndPoint).Port }
    finally { $probe.Dispose() }
}

function Assert-UdpPortUnowned([int]$Port, [int]$AllowedPid = 0) {
    $owners = @(Get-NetUDPEndpoint -LocalPort $Port -ErrorAction SilentlyContinue |
        Where-Object { $AllowedPid -eq 0 -or $_.OwningProcess -ne $AllowedPid })
    if ($owners.Count -ne 0) {
        throw "UDP port $Port already has an owner; refusing ambiguous proof-port ownership."
    }
}

function Test-ProofChannel($Channel, [int]$Port) {
    if ($Channel.channel_id -ne $channelId -or $Channel.display_name -ne 'Installed live-takeover proof' -or
        $Channel.output_profile -ne 'proof-360p30' -or @($Channel.output_sinks).Count -ne 1) { return $false }
    $sink = @($Channel.output_sinks)[0]
    return $sink.sink_id -eq $sinkId -and $sink.label -eq 'Installed live-takeover proof output' -and
        $sink.kind -eq 'udp-ts' -and $sink.destination -eq "127.0.0.1:$Port" -and
        [bool]$sink.required -and [bool]$sink.enabled
}

function Test-ProofSource($Candidate, [int]$Port) {
    if ($Candidate.source_id -ne $sourceId -or $Candidate.channel_id -ne $channelId -or
        $Candidate.label -ne 'Installed live-takeover proof input') { return $false }
    $endpoint = $Candidate.endpoint.UdpMpegTs
    return $null -ne $endpoint -and $endpoint.bind_ip -eq '127.0.0.1' -and [int]$endpoint.port -eq $Port
}

function Capture-UdpTransport([int]$Seconds, [string]$Path, [int]$MinimumBytes = 100000) {
    $memory = [IO.MemoryStream]::new()
    $remote = [Net.IPEndPoint]::new([Net.IPAddress]::Any, 0)
    $clock = [Diagnostics.Stopwatch]::StartNew()
    try {
        while ($clock.Elapsed.TotalSeconds -lt $Seconds) {
            for ($batch = 0; $batch -lt 64 -and $receiver.Available -gt 0; $batch++) {
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

function Invoke-NativeCapture([string]$FilePath, [string[]]$Arguments) {
    $captureId = [guid]::NewGuid().ToString('N')
    $stdoutPath = Join-Path $captureRoot "native-$captureId.stdout"
    $stderrPath = Join-Path $captureRoot "native-$captureId.stderr"
    try {
        # Windows PowerShell 5.1 flattens ArgumentList to one command line. Quote
        # arguments here so captures continue to work when the temp path contains
        # spaces (the .NET ArgumentList API is unavailable there).
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

function Get-DecodedEvidence([string]$Path, [bool]$RequireAudible = $false) {
    $ffprobe = (Get-Command ffprobe.exe -ErrorAction Stop).Source
    $ffmpeg = (Get-Command ffmpeg.exe -ErrorAction Stop).Source
    $probeResult = Invoke-NativeCapture $ffprobe @('-v', 'error', '-count_frames', '-show_entries', 'stream=codec_type,codec_name,nb_read_frames', '-of', 'json', $Path)
    $probeText = $probeResult.stdout
    if ($probeResult.exit_code -ne 0) { throw "FFprobe could not inspect $Path." }
    $streams = ($probeText | ConvertFrom-Json).streams
    $video = @($streams | Where-Object codec_type -eq 'video')[0]
    $audio = @($streams | Where-Object codec_type -eq 'audio')[0]
    if (-not $video -or -not $audio -or [int]$video.nb_read_frames -lt 30 -or [int]$audio.nb_read_frames -lt 30) {
        throw "Capture did not contain decodable continuous audio and video: $probeText"
    }
    $decodeResult = Invoke-NativeCapture $ffmpeg @('-v', 'error', '-i', $Path, '-t', '2', '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', 'NUL')
    if ($decodeResult.exit_code -ne 0) { throw "FFmpeg could not decode proof output $Path. $($decodeResult.stderr)" }
    $digestResult = Invoke-NativeCapture $ffmpeg @('-v', 'error', '-i', $Path, '-map', '0:v:0', '-frames:v', '1', '-f', 'md5', '-')
    $digestText = $digestResult.stdout
    if ($digestResult.exit_code -ne 0 -or $digestText -notmatch 'MD5=([0-9a-fA-F]{32})') {
        throw "FFmpeg could not fingerprint proof video pixels in $Path."
    }
    $videoMd5 = $Matches[1].ToLowerInvariant()
    $volumeResult = Invoke-NativeCapture $ffmpeg @('-v', 'info', '-i', $Path, '-t', '2', '-map', '0:a:0', '-af', 'volumedetect', '-f', 'null', 'NUL')
    $volumeText = $volumeResult.stderr
    if ($volumeResult.exit_code -ne 0 -or $volumeText -notmatch 'max_volume:\s*(-?[0-9.]+) dB') {
        throw "FFmpeg could not measure proof audio in $Path."
    }
    $maxVolume = [double]$Matches[1]
    if ($RequireAudible -and $maxVolume -lt -60) { throw "Proof audio was effectively silent ($maxVolume dB)." }
    return [ordered]@{
        video_codec = $video.codec_name
        audio_codec = $audio.codec_name
        decoded_video_frames = [int]$video.nb_read_frames
        decoded_audio_frames = [int]$audio.nb_read_frames
        first_video_md5 = $videoMd5
        max_audio_db = $maxVolume
    }
}

function Get-ProofRuntime { return (Invoke-Api 'Get' '/runtime-safe-to-air').channels | Where-Object channel_id -eq $channelId | Select-Object -First 1 }

function Clear-UdpBacklog {
    $remote = [Net.IPEndPoint]::new([Net.IPAddress]::Any, 0)
    while ($receiver.Available -gt 0) { [void]$receiver.Receive([ref]$remote) }
}

function Resolve-FFmpegExecutable {
    $launcher = (Get-Command ffmpeg.exe -CommandType Application -ErrorAction Stop).Source
    $shimMetadata = [IO.Path]::ChangeExtension($launcher, '.shim')
    if (-not (Test-Path -LiteralPath $shimMetadata -PathType Leaf)) {
        return (Resolve-Path -LiteralPath $launcher -ErrorAction Stop).Path
    }
    $metadata = (Get-Content -LiteralPath $shimMetadata -Raw -ErrorAction Stop).Trim()
    if ($metadata -notmatch '^path\s*=\s*"([^"]+)"$') {
        throw "FFmpeg shim metadata is malformed: $shimMetadata"
    }
    $target = (Resolve-Path -LiteralPath $Matches[1] -ErrorAction Stop).Path
    if ([IO.Path]::GetFileName($target) -ne 'ffmpeg.exe') {
        throw "FFmpeg shim target is not ffmpeg.exe: $target"
    }
    return $target
}

function Stop-ProofSender {
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
    if ($TimeoutSeconds -lt 30 -or $TimeoutSeconds -gt 600) { throw 'TimeoutSeconds must be between 30 and 600.' }
    if ($CaptureSeconds -lt 2 -or $CaptureSeconds -gt 30) { throw 'CaptureSeconds must be between 2 and 30.' }
    $baseAddress = [Uri]$serviceUri
    if ($baseAddress.Host -notin @('127.0.0.1', '::1', 'localhost')) {
        throw 'BaseUri must be an installed loopback API address.'
    }
    $mutexHeld = $mutex.WaitOne(0)
    if (-not $mutexHeld) { throw 'Another installed live-takeover proof is already running.' }
    New-Item -ItemType Directory -Path $captureRoot -Force | Out-Null

    $receiptPath = Join-Path $env:ProgramData 'TownLight Station\install-receipt.json'
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    $manifestPath = Resolve-Path -LiteralPath $Manifest -ErrorAction Stop
    $candidatePair = Get-TownLightCandidateIdentity -ManifestPath $manifestPath.Path -ExpectedSourceCommit $receipt.source_commit
    $manifestDocument = $candidatePair.manifest
    $applicationPath = Join-Path $env:ProgramFiles 'TownLight Station'
    $stationdPath = Join-Path $applicationPath 'stationd.exe'
    $workerPath = Join-Path $applicationPath 'channel-worker.exe'
    $stationdHash = (Get-FileHash -LiteralPath $stationdPath -Algorithm SHA256).Hash
    $workerHash = (Get-FileHash -LiteralPath $workerPath -Algorithm SHA256).Hash
    if ($receipt.source_commit -ne $manifestDocument.source_commit -or
        $stationdHash -ne $manifestDocument.stationd_sha256 -or $workerHash -ne $manifestDocument.channel_worker_sha256) {
        throw 'Installed receipt or executable hashes do not identify the candidate manifest.'
    }
    $result.evidence.installed_identity = [ordered]@{
        source_commit = $receipt.source_commit
        version = $receipt.version
        stationd_sha256 = $stationdHash
        channel_worker_sha256 = $workerHash
        media_runtime_sha256 = $receipt.media_runtime_sha256
    }
    $health = Invoke-RestMethod -Uri "$serviceUri/health" -TimeoutSec 5
    if ($health.status -ne 'ready' -or $health.database -ne 'ready') { throw 'Installed loopback API is not ready.' }

    $inputPortNumber = $InputPort
    $outputPortNumber = $OutputPort
    if ($inputPortNumber -eq $outputPortNumber) { throw 'InputPort and OutputPort must be different loopback UDP ports.' }
    Assert-UdpPortUnowned $outputPortNumber
    Assert-UdpPortUnowned $inputPortNumber
    $receiver = [Net.Sockets.UdpClient]::new([Net.IPEndPoint]::new([Net.IPAddress]::Loopback, $outputPortNumber))
    $receiver.Client.ReceiveBufferSize = 8388608
    Assert-UdpPortUnowned $outputPortNumber $PID

    $existingChannel = @((Invoke-Api 'Get' '/channels') | Where-Object channel_id -eq $channelId | Select-Object -First 1)
    if ($existingChannel.Count -gt 0) {
        if (-not (Test-ProofChannel $existingChannel[0] $outputPortNumber) -or [bool]$existingChannel[0].enabled) {
            throw 'The proof channel UUID already exists with ambiguous ownership or is enabled.'
        }
    }
    $baselineWorkerIds = @(Get-Process -Name channel-worker -ErrorAction SilentlyContinue | ForEach-Object Id)
    $proofChannel = [ordered]@{
        channel_id = $channelId
        display_name = 'Installed live-takeover proof'
        output_profile = 'proof-360p30'
        output_sinks = @([ordered]@{
            sink_id = $sinkId
            label = 'Installed live-takeover proof output'
            kind = 'udp-ts'
            destination = "127.0.0.1:$outputPortNumber"
            loudness_target_lufs = -16.0
            required = $true
            enabled = $true
        })
        enabled = $true
    }
    Invoke-Api 'Put' '/channels' $proofChannel | Out-Null
    $channelConfigured = $true
    $active = Invoke-Api 'Get' "/channels/$channelId/active-live-takeover"
    if ($null -ne $active) { throw 'The proof channel has an active takeover; refusing to disturb it.' }

    $existingSource = @((Invoke-Api 'Get' "/channels/$channelId/live-sources") | Where-Object source_id -eq $sourceId | Select-Object -First 1)
    $expectedSourceRevision = 0
    $sourceRevision = 1
    if ($existingSource.Count -gt 0) {
        if (-not (Test-ProofSource $existingSource[0] $inputPortNumber)) {
            throw 'The proof live-source UUID already exists with ambiguous ownership.'
        }
        $expectedSourceRevision = [uint64]$existingSource[0].revision
        $sourceRevision = $expectedSourceRevision + 1
    }
    $source = [ordered]@{
        source_id = $sourceId
        channel_id = $channelId
        label = 'Installed live-takeover proof input'
        enabled = $true
        endpoint = [ordered]@{ UdpMpegTs = [ordered]@{ bind_ip = '127.0.0.1'; port = $inputPortNumber } }
        revision = $sourceRevision
        expected_revision = $expectedSourceRevision
    }
    Invoke-Api 'Put' "/live-sources/$sourceId" $source | Out-Null
    $sourceConfigured = $true

    $worker = Wait-For -Action { Get-Process -Name channel-worker -ErrorAction SilentlyContinue | Where-Object { $_.Id -notin $baselineWorkerIds } | Select-Object -First 1 } `
        -Predicate { param($value) $null -ne $value } -Description 'proof channel worker'
    $workerPid = $worker.Id
    Wait-For -Action { Get-ProofRuntime } `
        -Predicate { param($value) $null -ne $value -and $value.state -in @('healthy_fallback', 'on_air') } -Description 'proof fallback runtime readiness' | Out-Null
    Clear-UdpBacklog
    Start-Sleep -Milliseconds 750
    $fallbackTransport = Capture-UdpTransport $CaptureSeconds $fallbackCapture
    $fallbackDecoded = Get-DecodedEvidence $fallbackCapture

    $ffmpeg = Resolve-FFmpegExecutable
    $senderArgs = @('-re', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=30', '-re', '-f', 'lavfi', '-i',
        'sine=frequency=997:sample_rate=48000', '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
        '-g', '30', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k', '-f', 'mpegts', "udp://127.0.0.1:${inputPortNumber}?pkt_size=1316")
    $sender = Start-Process -FilePath $ffmpeg -ArgumentList $senderArgs -PassThru -WindowStyle Hidden
    Start-Sleep -Milliseconds 500
    if ($sender.HasExited) { throw "FFmpeg live sender exited early with code $($sender.ExitCode)." }

    $authority = Invoke-Api 'Get' "/channels/$channelId/live-authority"
    $expires = [DateTimeOffset]::UtcNow.AddMinutes(5).ToUnixTimeMilliseconds()
    $requested = Invoke-Api 'Post' '/live-takeovers' ([ordered]@{
        session_id = $sessionId; channel_id = $channelId; source_id = $sourceId
        reason = $reason; expires_at_unix_ms = $expires; expected_authority_revision = [uint64]$authority.authority_revision
    })
    if ($requested.state -ne 'arming') { throw 'Live takeover did not begin in arming state.' }
    $authenticatedOperatorId = [string]$requested.operator_id
    if ([string]::IsNullOrWhiteSpace($authenticatedOperatorId)) { throw 'Live takeover response did not expose server-attributed operator identity.' }
    $armed = Wait-For -Action {
        $value = Invoke-Api 'Get' "/live-takeovers/$sessionId"
        if ($value.state -eq 'failed') { throw "Live takeover failed while arming: $($value.failure_detail)" }
        return $value
    } `
        -Predicate { param($value) $value.state -eq 'armed' } -Description 'durable Armed takeover state'

    $authority = Invoke-Api 'Get' "/channels/$channelId/live-authority"
    Invoke-Api 'Post' "/live-takeovers/$sessionId/take" ([ordered]@{ expected_authority_revision = [uint64]$authority.authority_revision }) | Out-Null
    $onAir = Wait-For -Action {
        $value = Invoke-Api 'Get' "/live-takeovers/$sessionId"
        if ($value.state -eq 'failed') { throw "Live takeover failed while taking air: $($value.failure_detail)" }
        return $value
    } `
        -Predicate { param($value) $value.state -eq 'on_air' -and $null -ne $value.taken_at_unix_ms } -Description 'durable OnAir takeover state'
    $liveRuntime = Wait-For -Action { Get-ProofRuntime } `
        -Predicate { param($value) $null -ne $value -and $value.state -eq 'live' } -Description 'runtime live state'
    Clear-UdpBacklog
    Start-Sleep -Milliseconds 750
    $liveTransport = Capture-UdpTransport $CaptureSeconds $liveCapture
    $liveDecoded = Get-DecodedEvidence $liveCapture $true
    if ($liveDecoded.first_video_md5 -eq $fallbackDecoded.first_video_md5) {
        throw 'Live output pixels did not differ from the fallback capture; source-switch evidence is ambiguous.'
    }
    $takeAudit = Invoke-Api 'Get' "/live-takeovers/$sessionId/audit"
    $takeStates = @($takeAudit | ForEach-Object state)
    if ($takeStates -notcontains 'arming' -or $takeStates -notcontains 'armed' -or $takeStates -notcontains 'take_requested' -or
        $takeStates -notcontains 'on_air' -or -not (@($takeAudit | Where-Object { $_.operator_id -eq $authenticatedOperatorId -and $_.detail -eq $reason }).Count -ge 1) -or
        -not (@($takeAudit | Where-Object { $_.operator_id -eq 'stationd-live-controller' }).Count -ge 1)) {
        throw 'Takeover audit lacks the requested actors, reason, or durable state trail.'
    }

    $authority = Invoke-Api 'Get' "/channels/$channelId/live-authority"
    Invoke-Api 'Post' "/live-takeovers/$sessionId/return" ([ordered]@{ expected_authority_revision = [uint64]$authority.authority_revision }) | Out-Null
    $completed = Wait-For -Action {
        $value = Invoke-Api 'Get' "/live-takeovers/$sessionId"
        if ($value.state -eq 'failed') { throw "Live takeover failed while returning: $($value.failure_detail)" }
        return $value
    } `
        -Predicate { param($value) $value.state -eq 'completed' -and $null -ne $value.returned_at_unix_ms } -Description 'durable Completed takeover state'
    $recoveryRuntime = Wait-For -Action { Get-ProofRuntime } `
        -Predicate { param($value) $null -ne $value -and $value.state -in @('healthy_fallback', 'on_air') } -Description 'fallback runtime recovery'
    Clear-UdpBacklog
    Start-Sleep -Milliseconds 750
    $recoveryTransport = Capture-UdpTransport $CaptureSeconds $recoveryCapture
    $recoveryDecoded = Get-DecodedEvidence $recoveryCapture
    if ($recoveryDecoded.first_video_md5 -ne $fallbackDecoded.first_video_md5) {
        throw 'Returned output pixels did not recover to the original fallback source.'
    }
    if (-not (Get-Process -Id $workerPid -ErrorAction SilentlyContinue)) { throw 'Proof channel worker did not remain alive through the takeover.' }
    $completionAudit = Invoke-Api 'Get' "/live-takeovers/$sessionId/audit"
    $completionStates = @($completionAudit | ForEach-Object state)
    if ($completionStates -notcontains 'return_requested' -or $completionStates -notcontains 'returning' -or $completionStates -notcontains 'completed' -or
        -not (@($completionAudit | Where-Object occurred_at_unix_ms -gt 0).Count -eq @($completionAudit).Count)) {
        throw 'Completion audit lacks durable return states or timestamps.'
    }

    $result.evidence.takeover = [ordered]@{
        requested_at_unix_ms = $requested.requested_at_unix_ms
        armed_authority_revision = $armed.authority_revision
        taken_at_unix_ms = $onAir.taken_at_unix_ms
        returned_at_unix_ms = $completed.returned_at_unix_ms
        take_audit_states = $takeStates
        completion_audit_states = $completionStates
        runtime_live_state = $liveRuntime.state
        recovery_runtime_state = $recoveryRuntime.state
        authenticated_operator_id = $authenticatedOperatorId
        reason = $reason
        controller_actor_observed = $true
        audit_timestamps_valid = $true
        worker_pid = $workerPid
        worker_alive_after_return = $true
    }
    $result.evidence.transport = [ordered]@{
        fallback = [ordered]@{ bytes = $fallbackTransport.bytes; seconds = $fallbackTransport.seconds; video_codec = $fallbackDecoded.video_codec; audio_codec = $fallbackDecoded.audio_codec; decoded_video_frames = $fallbackDecoded.decoded_video_frames; decoded_audio_frames = $fallbackDecoded.decoded_audio_frames; first_video_md5 = $fallbackDecoded.first_video_md5; max_audio_db = $fallbackDecoded.max_audio_db }
        live = [ordered]@{ bytes = $liveTransport.bytes; seconds = $liveTransport.seconds; video_codec = $liveDecoded.video_codec; audio_codec = $liveDecoded.audio_codec; decoded_video_frames = $liveDecoded.decoded_video_frames; decoded_audio_frames = $liveDecoded.decoded_audio_frames; first_video_md5 = $liveDecoded.first_video_md5; max_audio_db = $liveDecoded.max_audio_db }
        recovery = [ordered]@{ bytes = $recoveryTransport.bytes; seconds = $recoveryTransport.seconds; video_codec = $recoveryDecoded.video_codec; audio_codec = $recoveryDecoded.audio_codec; decoded_video_frames = $recoveryDecoded.decoded_video_frames; decoded_audio_frames = $recoveryDecoded.decoded_audio_frames; first_video_md5 = $recoveryDecoded.first_video_md5; max_audio_db = $recoveryDecoded.max_audio_db }
        source_switch_pixel_evidence = $true
        fallback_recovered_pixel_evidence = $true
    }
    $result.status = 'passed'
}
catch {
    $result.status = 'failed'
    $result.failure = $_.Exception.Message
    throw
}
finally {
    Stop-ProofSender
    if ($sourceConfigured -and $source) {
        try {
            $source.enabled = $false
            $source.expected_revision = $source.revision
            $source.revision = [uint64]$source.revision + 1
            Invoke-Api 'Put' "/live-sources/$sourceId" $source | Out-Null
        }
        catch { Write-Warning "Could not disable the proof live source: $($_.Exception.Message)" }
    }
    if ($channelConfigured -and $proofChannel) {
        try { $proofChannel.enabled = $false; Invoke-Api 'Put' '/channels' $proofChannel | Out-Null }
        catch { Write-Warning "Could not disable the proof channel: $($_.Exception.Message)" }
    }
    if ($receiver) { $receiver.Dispose() }
    $result.finished_at_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    if ($ResultPath) {
        $parent = Split-Path -Parent $ResultPath
        if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        $result | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath $ResultPath -Encoding utf8
    }
    elseif ($result.status -eq 'passed') { $result | ConvertTo-Json -Depth 16 }
    if (-not $KeepCapture) { Remove-Item -LiteralPath $captureRoot -Recurse -Force -ErrorAction SilentlyContinue }
    if ($mutexHeld) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
