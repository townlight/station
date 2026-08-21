param(
    [string]$BaseUri = 'http://127.0.0.1:4070',
    [int]$ListenerPort = 5930,
    [int]$CaptureSeconds = 5
)

$ErrorActionPreference = 'Stop'
$channel = [ordered]@{
    channel_id = '3157bcad-9395-43c5-bc77-915c77c67717'
    display_name = 'Installed SRT caller proof'
    output_profile = 'atsc-720p5994'
    output_sinks = @([ordered]@{
        sink_id = 'e91cc5fb-2b13-4819-a66c-81c09854052f'
        label = 'Required SRT caller'
        kind = 'srt'
        destination = "srt://127.0.0.1:$ListenerPort"
        loudness_target_lufs = -24.0
        required = $true
        enabled = $true
    })
    enabled = $true
}
$capture = Join-Path ([IO.Path]::GetTempPath()) "townlight-installed-srt-$PID.ts"
$runtime = Join-Path $env:ProgramFiles 'TownLight Station\runtime\gstreamer'
$launcher = Join-Path $runtime 'bin\gst-launch-1.0.exe'
$listener = $null
$configured = $false

try {
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "The installed SRT receiver runtime is missing: $launcher"
    }
    $env:PATH = (Join-Path $runtime 'bin') + ';' + $env:PATH
    $env:GST_PLUGIN_PATH_1_0 = Join-Path $runtime 'lib\gstreamer-1.0'
    $env:GST_PLUGIN_SYSTEM_PATH_1_0 = $env:GST_PLUGIN_PATH_1_0
    $listenerArguments = @(
        '-q', 'srtsrc', "uri=srt://127.0.0.1:$ListenerPort", 'mode=listener',
        'localaddress=127.0.0.1', "localport=$ListenerPort", 'latency=200',
        '!', 'filesink', "location=$capture", 'sync=false', 'async=false'
    )
    $listener = Start-Process -FilePath $launcher -ArgumentList $listenerArguments `
        -PassThru -WindowStyle Hidden
    Start-Sleep -Milliseconds 500
    if ($listener.HasExited) {
        throw "The SRT listener exited before channel startup with code $($listener.ExitCode)."
    }

    Invoke-RestMethod -Method Put -Uri "$BaseUri/api/v1/channels" -ContentType 'application/json' `
        -Body ($channel | ConvertTo-Json -Compress) -TimeoutSec 10 | Out-Null
    $configured = $true
    $readyClock = [Diagnostics.Stopwatch]::StartNew()
    do {
        $captured = if (Test-Path -LiteralPath $capture) { (Get-Item -LiteralPath $capture).Length } else { 0 }
        if ($captured -lt 100000) {
            Start-Sleep -Milliseconds 100
        }
    } while ($captured -lt 100000 -and $readyClock.Elapsed.TotalSeconds -lt 10)
    if ($captured -lt 100000) {
        throw 'The installed SRT branch did not deliver transport within 10 seconds.'
    }

    $startingLength = $captured
    $clock = [Diagnostics.Stopwatch]::StartNew()
    Start-Sleep -Seconds $CaptureSeconds
    $clock.Stop()
    $endingLength = (Get-Item -LiteralPath $capture).Length
    $measuredBitrate = [math]::Round((($endingLength - $startingLength) * 8) / $clock.Elapsed.TotalSeconds)
    if ($measuredBitrate -lt 7200000 -or $measuredBitrate -gt 8800000) {
        throw "Installed SRT transport measured $measuredBitrate bps instead of the 8 Mb/s contract."
    }

    $channel.enabled = $false
    Invoke-RestMethod -Method Put -Uri "$BaseUri/api/v1/channels" -ContentType 'application/json' `
        -Body ($channel | ConvertTo-Json -Compress) -TimeoutSec 10 | Out-Null
    $configured = $false
    Start-Sleep -Milliseconds 500
    if (-not $listener.HasExited) {
        Stop-Process -Id $listener.Id -Force
        $listener.WaitForExit(5000) | Out-Null
    }

    $bytes = [IO.File]::ReadAllBytes($capture)
    if ($bytes.Length -lt 1000000 -or $bytes.Length % 188 -ne 0) {
        throw "The installed SRT capture was not a whole MPEG-TS stream ($($bytes.Length) bytes)."
    }
    for ($offset = 0; $offset + 188 -le $bytes.Length; $offset += 188) {
        if ($bytes[$offset] -ne 0x47) {
            throw "Installed SRT capture lost MPEG-TS sync at byte $offset."
        }
    }

    $ffprobe = (Get-Command ffprobe.exe -ErrorAction Stop).Source
    $probeJson = (& $ffprobe -v error -show_entries `
        stream=codec_name,width,height,r_frame_rate,field_order,sample_rate,channels -of json $capture 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "FFprobe could not inspect the installed SRT capture.`n$probeJson"
    }
    $streams = ($probeJson | ConvertFrom-Json).streams
    $video = $streams | Where-Object codec_name -eq 'h264' | Select-Object -First 1
    $audio = $streams | Where-Object codec_name -eq 'aac' | Select-Object -First 1
    if (-not $video -or $video.width -ne 1280 -or $video.height -ne 720 -or
        $video.r_frame_rate -ne '60000/1001' -or $video.field_order -ne 'progressive') {
        throw "Installed SRT video did not match the 720p59.94 contract.`n$probeJson"
    }
    if (-not $audio -or $audio.sample_rate -ne '48000' -or $audio.channels -ne 2) {
        throw "Installed SRT audio did not match the AAC stereo 48 kHz contract.`n$probeJson"
    }

    [ordered]@{
        channel_id = $channel.channel_id
        sink_kind = 'srt'
        srt_mode = 'caller'
        latency_ms = 200
        encryption = 'none'
        dimensions = '1280x720'
        frame_rate = $video.r_frame_rate
        video_codec = $video.codec_name
        audio_codec = $audio.codec_name
        audio_sample_rate_hz = [int]$audio.sample_rate
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
                -Body ($channel | ConvertTo-Json -Compress) -TimeoutSec 10 | Out-Null
        }
        catch {
            Write-Warning "Could not disable the SRT proof output: $($_.Exception.Message)"
        }
    }
    if ($listener -and -not $listener.HasExited) {
        Stop-Process -Id $listener.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $capture -Force -ErrorAction SilentlyContinue
}
