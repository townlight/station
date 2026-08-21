param(
    [string]$BaseUri = 'http://127.0.0.1:4070',
    [int]$DestinationPort = 5920,
    [int]$RedundantDestinationPort = 5921,
    [int]$CaptureSeconds = 3
)

$ErrorActionPreference = 'Stop'
$channel = [ordered]@{
    channel_id = '205ba78d-eb32-4464-9e3f-5ba780dcd2c4'
    display_name = 'Installed 720p59.94 profile proof'
    output_profile = 'atsc-720p5994'
    output_sinks = @(
        [ordered]@{ sink_id = '205ba78d-eb32-4464-9e3f-5ba780dcd2c4'; label = 'Primary transport'; kind = 'udp-ts'; destination = "127.0.0.1:$DestinationPort"; loudness_target_lufs = -24.0; required = $true; enabled = $true }
        [ordered]@{ sink_id = '7a68be4c-f96f-4e26-b483-c5853748293e'; label = 'Redundant transport'; kind = 'udp-ts'; destination = "127.0.0.1:$RedundantDestinationPort"; loudness_target_lufs = -24.0; required = $true; enabled = $true }
    )
    enabled = $true
}
$capture = Join-Path ([IO.Path]::GetTempPath()) "townlight-installed-720p-$PID.ts"
$receiver = [Net.Sockets.UdpClient]::new($DestinationPort)
$redundantReceiver = [Net.Sockets.UdpClient]::new($RedundantDestinationPort)
$receiver.Client.ReceiveBufferSize = 8388608
$redundantReceiver.Client.ReceiveBufferSize = 8388608
$memory = [IO.MemoryStream]::new()
$redundantMemory = [IO.MemoryStream]::new()

try {
    $profiles = Invoke-RestMethod -Uri "$BaseUri/api/v1/output-profiles" -TimeoutSec 5
    $profile = $profiles | Where-Object profile_id -eq 'atsc-720p5994'
    if (-not $profile -or $profile.width -ne 1280 -or $profile.height -ne 720 -or
        $profile.frame_rate_numerator -ne 60000 -or $profile.frame_rate_denominator -ne 1001 -or
        $profile.transport_bitrate_bps -ne 8000000) {
        throw 'The installed output-profile catalog does not expose the expected 720p59.94 contract.'
    }

    Invoke-RestMethod -Method Put -Uri "$BaseUri/api/v1/channels" -ContentType 'application/json' `
        -Body ($channel | ConvertTo-Json -Compress) -TimeoutSec 10 | Out-Null
    $readyClock = [Diagnostics.Stopwatch]::StartNew()
    while (($receiver.Available -eq 0 -or $redundantReceiver.Available -eq 0) -and $readyClock.Elapsed.TotalSeconds -lt 10) {
        Start-Sleep -Milliseconds 2
    }
    if ($receiver.Available -eq 0 -or $redundantReceiver.Available -eq 0) {
        throw 'Both installed UDP branches did not become active within 10 seconds.'
    }
    $remote = [Net.IPEndPoint]::new([Net.IPAddress]::Any, 0)
    $redundantRemote = [Net.IPEndPoint]::new([Net.IPAddress]::Any, 0)
    $clock = [Diagnostics.Stopwatch]::StartNew()
    while ($clock.Elapsed.TotalSeconds -lt $CaptureSeconds) {
        while ($receiver.Available -gt 0) {
            $datagram = $receiver.Receive([ref]$remote)
            $memory.Write($datagram, 0, $datagram.Length)
        }
        while ($redundantReceiver.Available -gt 0) {
            $datagram = $redundantReceiver.Receive([ref]$redundantRemote)
            $redundantMemory.Write($datagram, 0, $datagram.Length)
        }
        Start-Sleep -Milliseconds 1
    }
    $clock.Stop()
    $bytes = $memory.ToArray()
    $redundantBytes = $redundantMemory.ToArray()
    [IO.File]::WriteAllBytes($capture, $bytes)

    foreach ($branch in @($bytes, $redundantBytes)) {
        if ($branch.Length -lt 1000000) {
            throw "An installed UDP branch captured only $($branch.Length) bytes."
        }
        for ($syncOffset = 0; $syncOffset + 188 -le $branch.Length; $syncOffset += 188) {
            if ($branch[$syncOffset] -ne 0x47) {
                throw "An installed UDP branch lost MPEG-TS sync at byte $syncOffset."
            }
        }
    }

    $nullPackets = 0
    $pmtPid = -1
    $videoPid = -1
    $lastVideoPts = $null
    $videoPtsDeltaTotal = [uint64]0
    $videoPtsDeltaSamples = 0
    for ($offset = 0; $offset + 188 -le $bytes.Length; $offset += 188) {
        if ($bytes[$offset] -ne 0x47) {
            throw "MPEG-TS sync was lost at byte $offset."
        }
        $packetPid = (($bytes[$offset + 1] -band 0x1f) -shl 8) -bor $bytes[$offset + 2]
        if ($packetPid -eq 0x1fff) {
            $nullPackets++
        }
        $payloadUnitStart = ($bytes[$offset + 1] -band 0x40) -ne 0
        $adaptationControl = ($bytes[$offset + 3] -shr 4) -band 0x03
        if ($adaptationControl -eq 1) {
            $payloadStart = $offset + 4
        }
        elseif ($adaptationControl -eq 3) {
            $payloadStart = $offset + 5 + $bytes[$offset + 4]
        }
        else {
            continue
        }
        if (-not $payloadUnitStart -or $payloadStart -ge $offset + 188) {
            continue
        }
        if ($packetPid -eq 0 -or $packetPid -eq $pmtPid) {
            $sectionStart = $payloadStart + 1 + $bytes[$payloadStart]
            if ($sectionStart + 12 -gt $offset + 188) {
                continue
            }
            $sectionLength = (($bytes[$sectionStart + 1] -band 0x0f) -shl 8) -bor $bytes[$sectionStart + 2]
            $sectionEnd = $sectionStart + 3 + $sectionLength - 4
            if ($packetPid -eq 0 -and $bytes[$sectionStart] -eq 0) {
                for ($programOffset = $sectionStart + 8; $programOffset + 4 -le $sectionEnd; $programOffset += 4) {
                    $programNumber = ($bytes[$programOffset] -shl 8) -bor $bytes[$programOffset + 1]
                    if ($programNumber -ne 0) {
                        $pmtPid = (($bytes[$programOffset + 2] -band 0x1f) -shl 8) -bor $bytes[$programOffset + 3]
                        break
                    }
                }
            }
            elseif ($packetPid -eq $pmtPid -and $bytes[$sectionStart] -eq 2) {
                $programInfoLength = (($bytes[$sectionStart + 10] -band 0x0f) -shl 8) -bor $bytes[$sectionStart + 11]
                $streamOffset = $sectionStart + 12 + $programInfoLength
                while ($streamOffset + 5 -le $sectionEnd) {
                    $streamType = $bytes[$streamOffset]
                    if ($streamType -eq 0x1b) {
                        $videoPid = (($bytes[$streamOffset + 1] -band 0x1f) -shl 8) -bor $bytes[$streamOffset + 2]
                        break
                    }
                    $streamInfoLength = (($bytes[$streamOffset + 3] -band 0x0f) -shl 8) -bor $bytes[$streamOffset + 4]
                    $streamOffset += 5 + $streamInfoLength
                }
            }
        }
        elseif ($packetPid -eq $videoPid -and $payloadStart + 14 -le $offset + 188 -and
            $bytes[$payloadStart] -eq 0 -and $bytes[$payloadStart + 1] -eq 0 -and
            $bytes[$payloadStart + 2] -eq 1 -and ($bytes[$payloadStart + 7] -band 0x80) -ne 0) {
            $part0 = [uint64](($bytes[$payloadStart + 9] -shr 1) -band 0x07)
            $part1 = [uint64]$bytes[$payloadStart + 10]
            $part2 = [uint64](($bytes[$payloadStart + 11] -shr 1) -band 0x7f)
            $part3 = [uint64]$bytes[$payloadStart + 12]
            $part4 = [uint64](($bytes[$payloadStart + 13] -shr 1) -band 0x7f)
            $videoPts = ($part0 -shl 30) -bor ($part1 -shl 22) -bor ($part2 -shl 15) -bor ($part3 -shl 7) -bor $part4
            if ($null -ne $lastVideoPts -and $videoPts -gt $lastVideoPts) {
                $videoPtsDeltaTotal += $videoPts - $lastVideoPts
                $videoPtsDeltaSamples++
            }
            $lastVideoPts = $videoPts
        }
    }
    $bitrate = [math]::Round(($bytes.Length * 8) / $clock.Elapsed.TotalSeconds)
    $redundantBitrate = [math]::Round(($redundantBytes.Length * 8) / $clock.Elapsed.TotalSeconds)
    if ($bitrate -lt 7600000 -or $bitrate -gt 8400000) {
        throw "Installed transport measured $bitrate bps instead of the 8 Mb/s contract."
    }
    if ($redundantBitrate -lt 7600000 -or $redundantBitrate -gt 8400000) {
        throw "Installed redundant transport measured $redundantBitrate bps instead of the 8 Mb/s contract."
    }
    if ($nullPackets -eq 0) {
        throw 'Installed transport contained no CBR null-packet padding.'
    }
    if ($videoPtsDeltaSamples -lt 100) {
        throw "Installed transport exposed only $videoPtsDeltaSamples video PTS intervals."
    }
    $averageVideoTicks = $videoPtsDeltaTotal / $videoPtsDeltaSamples
    if ($averageVideoTicks -lt 1500 -or $averageVideoTicks -gt 1503) {
        throw "Installed video cadence averaged $averageVideoTicks 90 kHz ticks per frame instead of 60000/1001."
    }

    $runtime = Join-Path $env:ProgramFiles 'TownLight Station\runtime\gstreamer'
    $discoverer = Join-Path $runtime 'bin\gst-discoverer-1.0.exe'
    $env:PATH = (Join-Path $runtime 'bin') + ';' + $env:PATH
    $env:GST_PLUGIN_PATH_1_0 = Join-Path $runtime 'lib\gstreamer-1.0'
    $env:GST_PLUGIN_SYSTEM_PATH_1_0 = $env:GST_PLUGIN_PATH_1_0
    $discovery = (& $discoverer $capture 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0 -or $discovery -notmatch 'Width:\s+1280' -or $discovery -notmatch 'Height:\s+720') {
        throw "Installed transport discovery did not prove 1280x720 video.`n$discovery"
    }

    [ordered]@{
        channel_id = $channel.channel_id
        profile_id = $profile.profile_id
        dimensions = '1280x720'
        frame_rate_contract = '60000/1001'
        measured_frame_rate = [math]::Round(90000 / $averageVideoTicks, 3)
        video_pts_intervals = $videoPtsDeltaSamples
        measured_transport_bitrate_bps = $bitrate
        redundant_transport_bitrate_bps = $redundantBitrate
        captured_bytes = $bytes.Length
        redundant_captured_bytes = $redundantBytes.Length
        proven_output_branches = 2
        null_packets = $nullPackets
        service_health = (Invoke-RestMethod -Uri "$BaseUri/health" -TimeoutSec 5).status
    } | ConvertTo-Json
}
finally {
    try {
        $channel.enabled = $false
        Invoke-RestMethod -Method Put -Uri "$BaseUri/api/v1/channels" -ContentType 'application/json' `
            -Body ($channel | ConvertTo-Json -Compress) -TimeoutSec 10 | Out-Null
    }
    catch {
        Write-Warning "Could not disable the proof output: $($_.Exception.Message)"
    }
    $receiver.Dispose()
    $redundantReceiver.Dispose()
    $memory.Dispose()
    $redundantMemory.Dispose()
    Remove-Item -LiteralPath $capture -Force -ErrorAction SilentlyContinue
}
