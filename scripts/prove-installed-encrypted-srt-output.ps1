param(
    [string]$BaseUri = 'http://127.0.0.1:4070',
    [int]$ListenerPort = 5937,
    [int]$CaptureSeconds = 5,
    [string]$Passphrase = 'TownLight-AES256-proof-2026'
)

$ErrorActionPreference = 'Stop'
$channelId = '9f55c6a1-12c7-4a90-9f90-7dbd4b6663f8'
$sinkId = '7a8dd98f-bcaa-46fe-b237-846ceae42e86'
$channel = [ordered]@{
    channel_id = $channelId
    display_name = 'Installed encrypted SRT proof'
    output_profile = 'atsc-720p5994'
    output_sinks = @([ordered]@{
        sink_id = $sinkId
        label = 'AES-256 SRT caller'
        kind = 'srt'
        destination = "srt://127.0.0.1:$ListenerPort"
        loudness_target_lufs = -24.0
        required = $true
        enabled = $true
        encryption_configured = $true
    })
    enabled = $false
}
$capture = Join-Path ([IO.Path]::GetTempPath()) "townlight-installed-encrypted-srt-$PID.ts"
$runtime = Join-Path $env:ProgramFiles 'TownLight Station\runtime\gstreamer'
$launcher = Join-Path $runtime 'bin\gst-launch-1.0.exe'
$listener = $null
$configured = $false

function Test-ContainsBytes([byte[]]$Bytes, [byte[]]$Needle) {
    if ($Needle.Length -eq 0 -or $Bytes.Length -lt $Needle.Length) { return $false }
    for ($offset = 0; $offset -le $Bytes.Length - $Needle.Length; $offset++) {
        $matched = $true
        for ($index = 0; $index -lt $Needle.Length; $index++) {
            if ($Bytes[$offset + $index] -ne $Needle[$index]) { $matched = $false; break }
        }
        if ($matched) { return $true }
    }
    return $false
}

try {
    if (!(10..79).Contains($Passphrase.Length) -or $Passphrase -notmatch '^[!-~]+$') {
        throw 'The proof passphrase must contain 10 to 79 printable ASCII characters without spaces.'
    }
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "The installed SRT receiver runtime is missing: $launcher"
    }
    $passphrases = @{}
    $passphrases[$sinkId] = $Passphrase
    $write = [ordered]@{}
    foreach ($entry in $channel.GetEnumerator()) { $write[$entry.Key] = $entry.Value }
    $write.srt_passphrases = $passphrases
    $response = Invoke-RestMethod -Method Put -Uri "$BaseUri/api/v1/channels" `
        -ContentType 'application/json' -Body ($write | ConvertTo-Json -Depth 8 -Compress) -TimeoutSec 10
    $configured = $true
    if (-not $response.output_sinks[0].encryption_configured) {
        throw 'The public channel record did not report encrypted SRT presence.'
    }
    $publicJson = & curl.exe --silent --show-error --fail-with-body "$BaseUri/api/v1/channels"
    if ($LASTEXITCODE -ne 0 -or $publicJson.Contains($Passphrase) -or $publicJson.Contains('srt_passphrases')) {
        throw 'The public channel API exposed the write-only SRT passphrase contract.'
    }

    $needle = [Text.Encoding]::UTF8.GetBytes($Passphrase)
    foreach ($candidate in @(
        (Join-Path $env:ProgramData 'TownLight Station\station.db'),
        (Join-Path $env:ProgramData 'TownLight Station\station.db-wal'),
        (Join-Path $env:ProgramData 'TownLight Station\station.db-shm')
    )) {
        if ((Test-Path -LiteralPath $candidate) -and
            (Test-ContainsBytes ([IO.File]::ReadAllBytes($candidate)) $needle)) {
            throw "The plaintext SRT passphrase reached $candidate."
        }
    }

    Restart-Service -Name TownLightStation -Force
    $healthDeadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        try { $health = Invoke-RestMethod -Uri "$BaseUri/health" -TimeoutSec 2 }
        catch { $health = $null }
        if (-not $health -or $health.status -ne 'ready') { Start-Sleep -Milliseconds 200 }
    } while ((!$health -or $health.status -ne 'ready') -and [DateTime]::UtcNow -lt $healthDeadline)
    if (-not $health -or $health.status -ne 'ready') {
        throw 'The station service did not recover after the encrypted-secret persistence restart.'
    }

    $env:PATH = (Join-Path $runtime 'bin') + ';' + $env:PATH
    $env:GST_PLUGIN_PATH_1_0 = Join-Path $runtime 'lib\gstreamer-1.0'
    $env:GST_PLUGIN_SYSTEM_PATH_1_0 = $env:GST_PLUGIN_PATH_1_0
    $listenerArguments = @(
        '-q', 'srtsrc', "uri=srt://127.0.0.1:$ListenerPort", 'mode=listener',
        'localaddress=127.0.0.1', "localport=$ListenerPort", 'latency=200',
        "passphrase=$Passphrase", 'pbkeylen=32',
        '!', 'fdsink', 'fd=1', 'sync=false', 'async=false'
    )
    $listener = Start-Process -FilePath $launcher -ArgumentList $listenerArguments `
        -RedirectStandardOutput $capture -PassThru -WindowStyle Hidden
    $listenerClock = [Diagnostics.Stopwatch]::StartNew()
    $listenerBound = $false
    do {
        if ($listener.HasExited) {
            throw "The encrypted SRT listener exited before channel startup with code $($listener.ExitCode)."
        }
        $listenerBound = @(
            Get-NetUDPEndpoint -LocalPort $ListenerPort -ErrorAction SilentlyContinue |
                Where-Object OwningProcess -eq $listener.Id
        ).Count -gt 0
        if (-not $listenerBound) { Start-Sleep -Milliseconds 100 }
    } while (-not $listenerBound -and $listenerClock.Elapsed.TotalSeconds -lt 20)
    if (-not $listenerBound) { throw 'The encrypted SRT listener did not bind within 20 seconds.' }

    $channel.enabled = $true
    Invoke-RestMethod -Method Put -Uri "$BaseUri/api/v1/channels" -ContentType 'application/json' `
        -Body ($channel | ConvertTo-Json -Depth 8 -Compress) -TimeoutSec 10 | Out-Null
    $readyClock = [Diagnostics.Stopwatch]::StartNew()
    do {
        $captured = if (Test-Path -LiteralPath $capture) { (Get-Item -LiteralPath $capture).Length } else { 0 }
        if ($captured -lt 100000) { Start-Sleep -Milliseconds 100 }
    } while ($captured -lt 100000 -and $readyClock.Elapsed.TotalSeconds -lt 12)
    if ($captured -lt 100000) { throw 'The installed encrypted SRT branch delivered no transport.' }

    $workerProcesses = @(Get-CimInstance Win32_Process | Where-Object Name -eq 'channel-worker.exe')
    foreach ($worker in $workerProcesses) {
        if ($worker.CommandLine -and
            ($worker.CommandLine.Contains($Passphrase) -or $worker.CommandLine.Contains($channel.output_sinks[0].destination))) {
            throw 'A service-owned worker exposed SRT configuration in its process arguments.'
        }
    }

    $startingLength = $captured
    $clock = [Diagnostics.Stopwatch]::StartNew()
    Start-Sleep -Seconds $CaptureSeconds
    $clock.Stop()
    $endingLength = (Get-Item -LiteralPath $capture).Length
    $measuredBitrate = [math]::Round((($endingLength - $startingLength) * 8) / $clock.Elapsed.TotalSeconds)
    if ($measuredBitrate -lt 7200000 -or $measuredBitrate -gt 8800000) {
        throw "Encrypted SRT measured $measuredBitrate bps instead of the 8 Mb/s contract."
    }

    $channel.enabled = $false
    Invoke-RestMethod -Method Put -Uri "$BaseUri/api/v1/channels" -ContentType 'application/json' `
        -Body ($channel | ConvertTo-Json -Depth 8 -Compress) -TimeoutSec 10 | Out-Null
    $configured = $false
    Start-Sleep -Milliseconds 500
    if (-not $listener.HasExited) {
        Stop-Process -Id $listener.Id -Force -ErrorAction SilentlyContinue
        $listener.WaitForExit(5000) | Out-Null
    }

    $bytes = [IO.File]::ReadAllBytes($capture)
    if ($bytes.Length -lt 1000000 -or $bytes.Length % 188 -ne 0) {
        throw "The encrypted SRT capture was not a whole MPEG-TS stream ($($bytes.Length) bytes)."
    }
    for ($offset = 0; $offset + 188 -le $bytes.Length; $offset += 188) {
        if ($bytes[$offset] -ne 0x47) { throw "MPEG-TS sync was lost at byte $offset." }
    }
    $ffprobe = (Get-Command ffprobe.exe -ErrorAction Stop).Source
    $probeJson = (& $ffprobe -v error -show_entries `
        stream=codec_name,width,height,r_frame_rate,field_order,sample_rate,channels -of json $capture 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) { throw "FFprobe rejected the encrypted SRT capture.`n$probeJson" }
    $streams = ($probeJson | ConvertFrom-Json).streams
    $video = $streams | Where-Object codec_name -eq 'h264' | Select-Object -First 1
    $audio = $streams | Where-Object codec_name -eq 'aac' | Select-Object -First 1
    if (-not $video -or $video.width -ne 1280 -or $video.height -ne 720 -or
        $video.r_frame_rate -ne '60000/1001' -or $video.field_order -ne 'progressive') {
        throw "Encrypted SRT video missed the 720p59.94 contract.`n$probeJson"
    }
    if (-not $audio -or $audio.sample_rate -ne '48000' -or $audio.channels -ne 2) {
        throw "Encrypted SRT audio missed the AAC stereo 48 kHz contract.`n$probeJson"
    }

    [ordered]@{
        channel_id = $channelId
        encryption = 'AES-256'
        passphrase_publicly_exposed = $false
        passphrase_plaintext_in_sqlite = $false
        persisted_across_service_restart = $true
        worker_arguments_redacted = $true
        dimensions = '1280x720'
        frame_rate = $video.r_frame_rate
        video_codec = $video.codec_name
        audio_codec = $audio.codec_name
        measured_transport_bitrate_bps = $measuredBitrate
        captured_bytes = $bytes.Length
        service_health = (Invoke-RestMethod -Uri "$BaseUri/health" -TimeoutSec 5).status
    } | ConvertTo-Json
}
finally {
    if ($configured) {
        try {
            $channel.enabled = $false
            Invoke-RestMethod -Method Put -Uri "$BaseUri/api/v1/channels" -ContentType 'application/json' `
                -Body ($channel | ConvertTo-Json -Depth 8 -Compress) -TimeoutSec 10 | Out-Null
        }
        catch { Write-Warning "Could not disable the encrypted SRT proof: $($_.Exception.Message)" }
    }
    if ($listener -and -not $listener.HasExited) {
        Stop-Process -Id $listener.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $capture -Force -ErrorAction SilentlyContinue
}
