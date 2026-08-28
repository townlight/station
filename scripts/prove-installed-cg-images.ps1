param(
    [Parameter(Mandatory = $true)]
    [Management.Automation.PSCredential]$Credential,
    [string]$BaseUri = 'http://127.0.0.1:4070',
    [int]$DestinationPort = 5940,
    [int]$CaptureSeconds = 5,
    [switch]$ValidateClear,
    [string]$DecoderPath = '',
    [string]$ExpectedDecoderSha256 = '',
    [int64]$ExpectedDecoderSizeBytes = 0,
    [IO.FileStream]$PinnedDecoderStream = $null,
    [object[]]$PinnedDecoderDirectoryHandles = @()
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'installed-proof-api.ps1')
. (Join-Path $PSScriptRoot 'installed-caption-decoder-pin.ps1')
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
$channelId = 'c8c8c8c8-7d4e-4b31-9b3f-1f7b9a0c5940'
$boardId = 'b8b8b8b8-4d3e-4a21-8b2f-2f6a8b0c5940'
$zoneId = 'd8d8d8d8-5e4f-4a32-9c4f-3f7b9a0c5940'
$channelSinkId = 'a8a8a8a8-6f5a-4b43-8d5f-4f8b0a1c5940'
$redCapture = Join-Path ([IO.Path]::GetTempPath()) "townlight-cg-red-$PID.ts"
$greenCapture = Join-Path ([IO.Path]::GetTempPath()) "townlight-cg-green-$PID.ts"
$redPixel = Join-Path ([IO.Path]::GetTempPath()) "townlight-cg-red-$PID.rgb"
$greenPixel = Join-Path ([IO.Path]::GetTempPath()) "townlight-cg-green-$PID.rgb"
$clearCapture = Join-Path ([IO.Path]::GetTempPath()) "townlight-cg-clear-$PID.ts"
$clearPixel = Join-Path ([IO.Path]::GetTempPath()) "townlight-cg-clear-$PID.rgb"
$baselineCapture = Join-Path ([IO.Path]::GetTempPath()) "townlight-cg-baseline-$PID.ts"
$baselinePixel = Join-Path ([IO.Path]::GetTempPath()) "townlight-cg-baseline-$PID.rgb"
$receiver = [Net.Sockets.UdpClient]::new([Net.IPEndPoint]::new([Net.IPAddress]::Loopback, $DestinationPort))
$DestinationPort = ([Net.IPEndPoint]$receiver.Client.LocalEndPoint).Port
$receiver.Client.ReceiveBufferSize = 8388608
$originalChannel = $null
$existingBoard = $null
$workerPid = $null
$boardRevision = 0
$zoneRevision = 0
$clearValidated = $false

function Invoke-Json {
    param([string]$Method, [string]$Path, $Body)
    $params = @{ Method = $Method; Uri = "$BaseUri$Path"; TimeoutSec = 15 }
    if ($null -ne $Body) {
        $params.ContentType = 'application/json'
        $params.Body = ($Body | ConvertTo-Json -Depth 20 -Compress)
    }
    Invoke-InstalledProofApi @params
}

function New-Audit {
    param([string]$Kind, [string]$Board, [int64]$Time, [string]$Suffix)
    [ordered]@{
        audit_id = [guid]::NewGuid().ToString()
        board_id = $Board
        channel_id = $channelId
        event_kind = $Kind
        occurred_at_unix_ms = $Time
        details = [ordered]@{}
    }
}

function Write-Cg {
    param([string]$Path, [string]$Property, $Value, [uint64]$ExpectedRevision, $Audit)
    Invoke-Json 'Put' $Path ([ordered]@{
        $Property = $Value
        expected_revision = $ExpectedRevision
        audit = $Audit
    })
}

function New-ImageBytes {
    param([int]$Red, [int]$Green, [int]$Blue)
    Add-Type -AssemblyName System.Drawing
    $bitmap = [System.Drawing.Bitmap]::new(96, 96, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    try {
        for ($x = 0; $x -lt 96; $x++) {
            for ($y = 0; $y -lt 96; $y++) {
                $bitmap.SetPixel($x, $y, [System.Drawing.Color]::FromArgb(255, $Red, $Green, $Blue))
            }
        }
        $stream = [IO.MemoryStream]::new()
        try {
            $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
            return $stream.ToArray()
        }
        finally { $stream.Dispose() }
    }
    finally { $bitmap.Dispose() }
}

function Upload-CgImage {
    param([byte[]]$Bytes)
    [byte[]]$response = Invoke-TownLightProofApi -Session $apiSession -Method Post -Path '/cg/images' `
        -ContentType 'application/octet-stream' -Body $Bytes -ResponseMode Raw -TimeoutSec 15
    [Text.Encoding]::UTF8.GetString($response) | ConvertFrom-Json
}

function Assert-CgStartupDatagram {
    param(
        [byte[]]$Datagram,
        [string]$Phase
    )
    if ($Datagram.Length -eq 0 -or ($Datagram.Length % 188) -ne 0) {
        throw "CG capture phase $Phase startup datagram is not packet-aligned MPEG-TS."
    }
    for ($offset = 0; $offset -lt $Datagram.Length; $offset += 188) {
        if ($Datagram[$offset] -ne 0x47) {
            throw "CG capture phase $Phase startup datagram lost MPEG-TS sync at byte $offset."
        }
    }
}

function Capture-Udp {
    param(
        [string]$Path,
        [string]$Phase,
        [int]$StartupTimeoutSeconds = 30
    )
    $minimumCaptureBytes = 250000
    $memory = [IO.MemoryStream]::new()
    try {
        $remote = [Net.IPEndPoint]::new([Net.IPAddress]::Any, 0)
        $startupDeadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
        $receivedFirstDatagram = $false
        do {
            while ($receiver.Available -gt 0) {
                $datagram = $receiver.Receive([ref]$remote)
                Assert-CgStartupDatagram -Datagram $datagram -Phase $Phase
                $receivedFirstDatagram = $true
                break
            }
            if (-not $receivedFirstDatagram) { Start-Sleep -Milliseconds 25 }
        } while (-not $receivedFirstDatagram -and [DateTime]::UtcNow -lt $startupDeadline)
        if (-not $receivedFirstDatagram) {
            throw "CG capture phase $Phase received 0 bytes before its $StartupTimeoutSeconds-second startup deadline."
        }
        $clock = [Diagnostics.Stopwatch]::StartNew()
        while ($clock.Elapsed.TotalSeconds -lt $CaptureSeconds) {
            while ($receiver.Available -gt 0) {
                $datagram = $receiver.Receive([ref]$remote)
                $memory.Write($datagram, 0, $datagram.Length)
            }
            Start-Sleep -Milliseconds 2
        }
        [IO.File]::WriteAllBytes($Path, $memory.ToArray())
        if ($memory.Length -lt $minimumCaptureBytes) {
            throw "CG capture phase $Phase received $($memory.Length) bytes; expected at least $minimumCaptureBytes."
        }
    }
    finally { $memory.Dispose() }
}

function Clear-UdpBuffer {
    $remote = [Net.IPEndPoint]::new([Net.IPAddress]::Any, 0)
    while ($receiver.Available -gt 0) {
        $null = $receiver.Receive([ref]$remote)
    }
}

function Assert-TsContinuity {
    param([string]$Path)
    $bytes = [IO.File]::ReadAllBytes($Path)
    if (($bytes.Length % 188) -ne 0) { throw "CG capture is not packet aligned: $Path" }
    for ($offset = 0; $offset -lt $bytes.Length; $offset += 188) {
        if ($bytes[$offset] -ne 0x47) { throw "MPEG-TS sync lost at $offset in $Path" }
    }
    return $bytes.Length
}

function Read-Pixel {
    param([string]$DecoderExecutable, [string]$InputPath, [string]$OutputPath)
    # Crop an even 2x2 region because the MPEG-TS proof profile decodes to chroma-subsampled video;
    # a 1x1 crop is rounded to zero by FFmpeg before conversion to RGB.
    & $DecoderExecutable -hide_banner -loglevel error -i $InputPath -vf 'crop=2:2:560:120,format=rgb24' `
        -frames:v 1 -an -f rawvideo -pix_fmt rgb24 -y $OutputPath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $OutputPath)) {
        throw "ffmpeg could not decode a CG proof pixel from $InputPath."
    }
    $pixel = [IO.File]::ReadAllBytes($OutputPath)
    if ($pixel.Length -lt 3) { throw "Decoded CG pixel is incomplete: $InputPath" }
    return $pixel
}

function Wait-PreviewAsset {
    param([string]$AssetId)
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        try {
            $preview = Invoke-Json 'Get' "/api/v1/cg/preview?channel_id=$channelId&at_unix_ms=$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())" $null
            $image = @($preview.zones | ForEach-Object { $_.content }) |
                Where-Object { $_.kind -eq 'image' -and $_.asset.asset_id -eq $AssetId } |
                Select-Object -First 1
            if ($image) { return }
        } catch { }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Installed CG preview did not resolve image asset $AssetId."
}

function Assert-WorkerAlive {
    if ($workerPid -and -not (Get-Process -Id $workerPid -ErrorAction SilentlyContinue)) {
        throw "The proof channel worker PID $workerPid did not remain alive."
    }
}

function Wait-ProofChannelRuntimeReady {
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Assert-WorkerAlive
        try {
            $safeToAir = Invoke-Json 'Get' '/api/v1/runtime-safe-to-air' $null
            $proofChannel = @($safeToAir.channels | Where-Object { $_.channel_id -eq $channelId })
            if ($safeToAir.color -eq 'green' -and $proofChannel.Count -eq 1 -and
                $proofChannel[0].state -in @('healthy_fallback', 'on_air')) {
                return
            }
        } catch { }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'The proof channel did not reach an authoritative healthy runtime state within 30 seconds.'
}

function Wait-PreviewCleared {
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        try {
            $preview = Invoke-Json 'Get' "/api/v1/cg/preview?channel_id=$channelId&at_unix_ms=$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())" $null
            $image = @($preview.zones | ForEach-Object { $_.content }) |
                Where-Object { $_.kind -eq 'image' } | Select-Object -First 1
            if (-not $image) { return }
        } catch {
            if ($_.Exception.Message -match 'HTTP (409|422)') { return }
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Installed CG preview did not clear within 20 seconds.'
}

try {
    $decoderExecutable = 'ffmpeg.exe'
    $explicitDecoderRequested = -not [string]::IsNullOrWhiteSpace($DecoderPath)
    $decoderIdentitySupplied = -not [string]::IsNullOrWhiteSpace($ExpectedDecoderSha256) -or $ExpectedDecoderSizeBytes -ne 0
    if ($explicitDecoderRequested -or $decoderIdentitySupplied) {
        if (-not $explicitDecoderRequested -or [string]::IsNullOrWhiteSpace($ExpectedDecoderSha256) -or
            $ExpectedDecoderSha256 -cnotmatch '^[0-9a-f]{64}$' -or $ExpectedDecoderSizeBytes -le 0 -or
            $null -eq $PinnedDecoderStream) {
            throw 'Explicit CG decoder requires a retained regular path and exact lowercase SHA-256/size identity.'
        }
        Assert-TownLightPinnedCaptionDecoder -Stream $PinnedDecoderStream -ExpectedPath $DecoderPath `
            -ExpectedSha256 $ExpectedDecoderSha256 -ExpectedSizeBytes $ExpectedDecoderSizeBytes
        if (@($PinnedDecoderDirectoryHandles).Count -ne 4 -or @($PinnedDecoderDirectoryHandles | Where-Object {
            $null -eq $_ -or $_.IsInvalid -or $_.IsClosed
        }).Count -ne 0) {
            throw 'Explicit CG decoder directory chain is not retained through pixel assertions.'
        }
        $decoderExecutable = [IO.Path]::GetFullPath($DecoderPath)
    }
    elseif (-not (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue)) {
        throw 'ffmpeg.exe is required on PATH for standalone CG pixel assertions.'
    }
    $channelResponse = Invoke-Json 'Get' '/api/v1/channels' $null
    $channels = @($channelResponse)
    $originalChannel = $channels | Where-Object channel_id -eq $channelId | Select-Object -First 1
    if ($originalChannel -and $originalChannel.display_name -ne 'Installed CG image proof') {
        throw "Refusing to overwrite existing channel $channelId with display name '$($originalChannel.display_name)'."
    }
    if ($originalChannel) {
        # This fixed identity is proof-owned. Stop its current destination before
        # changing it, and ensure a prior interrupted proof cannot leave it on air.
        $originalChannel.enabled = $false
        Invoke-Json 'Put' '/api/v1/channels' $originalChannel | Out-Null
    }
    try {
        $existingBoard = Invoke-Json 'Get' "/api/v1/cg/board?channel_id=$channelId" $null
        if ($existingBoard -and ($existingBoard.board_id -ne $boardId -or
            $existingBoard.template_id -ne 'l-bar')) {
            throw "Refusing stale CG image proof board $($existingBoard.board_id)."
        }
    } catch {
        if ($_.Exception.Message -like '*Refusing stale*' -or
            $_.Exception.Message -notmatch 'HTTP 404') { throw }
    }
    try {
        $existingZones = @(Invoke-Json 'Get' "/api/v1/cg/zones?board_id=$boardId" $null)
        if ($existingZones.Count -gt 0) { throw "Refusing stale CG image proof zones for board $boardId." }
    } catch {
        if ($_.Exception.Message -like '*Refusing stale*' -or
            $_.Exception.Message -notmatch 'HTTP 404') { throw }
    }

    $channel = [ordered]@{
        channel_id = $channelId
        display_name = 'Installed CG image proof'
        output_profile = 'proof-360p30'
        output_sinks = @([ordered]@{
            sink_id = $channelSinkId; label = 'CG image proof'; kind = 'udp-ts'
            destination = "127.0.0.1:$DestinationPort"; loudness_target_lufs = -16.0
            required = $true; enabled = $true
        })
        enabled = $true
    }
    $baselineWorkerIds = @(Get-Process channel-worker -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
    Invoke-Json 'Put' '/api/v1/channels' $channel | Out-Null
    $workerDeadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        $newWorkers = @(Get-Process channel-worker -ErrorAction SilentlyContinue |
            Where-Object { $baselineWorkerIds -notcontains $_.Id })
        if ($newWorkers.Count -gt 1) {
            throw 'More than one channel worker started during proof setup; refusing an ambiguous PID assertion.'
        }
        $workerPid = $newWorkers | Select-Object -First 1 -ExpandProperty Id
        if (-not $workerPid) { Start-Sleep -Milliseconds 100 }
    } while (-not $workerPid -and [DateTime]::UtcNow -lt $workerDeadline)
    if (-not $workerPid) { throw 'The dedicated installed CG proof worker did not start.' }
    Wait-ProofChannelRuntimeReady
    Clear-UdpBuffer
    Capture-Udp $baselineCapture -Phase 'baseline'
    Assert-WorkerAlive
    Assert-TsContinuity $baselineCapture | Out-Null
    $baselinePixelValue = Read-Pixel $decoderExecutable $baselineCapture $baselinePixel

    $red = Upload-CgImage (New-ImageBytes 220 20 20)
    $green = Upload-CgImage (New-ImageBytes 20 220 20)
    $imageResponse = Invoke-Json 'Get' '/api/v1/cg/images' $null
    $images = @($imageResponse)
    if (-not ($images.asset_id -contains $red.asset_id) -or -not ($images.asset_id -contains $green.asset_id)) {
        throw 'The installed CG image library did not list both server-issued identities.'
    }
    foreach ($uploaded in @($red, $green)) {
        $image = $images | Where-Object asset_id -eq $uploaded.asset_id | Select-Object -First 1
        if (-not $image) { throw "Installed CG image $($uploaded.asset_id) is absent from the verified library." }
        if ($image.readiness -ne 'ready' -or $image.mime_type -ne 'image/png' -or
            $image.width -ne 96 -or $image.height -ne 96 -or $image.byte_size -le 0 -or $image.byte_size -gt 8388608) {
            throw "Installed CG image metadata is not ready/bounded for $($image.asset_id)."
        }
    }

    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $priorBoardRevision = if ($existingBoard) { [uint64]$existingBoard.revision } else { [uint64]0 }
    $boardRevision = $priorBoardRevision + 1
    $board = [ordered]@{
        board_id = $boardId; channel_id = $channelId; template_id = 'l-bar'; active = $true
        background_audio_asset_id = $null; revision = $boardRevision
        created_at_unix_ms = if ($existingBoard) { [int64]$existingBoard.created_at_unix_ms } else { $now }
        updated_at_unix_ms = $now
    }
    $boardAuditKind = if ($existingBoard) { 'board_updated' } else { 'board_created' }
    Write-Cg '/api/v1/cg/board' 'board' $board $priorBoardRevision (New-Audit $boardAuditKind $boardId $now 'board') | Out-Null
    $zone = [ordered]@{
        zone_id = $zoneId; board_id = $boardId; template_zone_key = 'sidebar'; zone_kind = 'sidebar'
        content_source = [ordered]@{ kind = 'image'; asset_id = $red.asset_id }
        rotation_seconds = $null; approval_required = $false; allowed_tags = @(); revision = 1
        created_at_unix_ms = $now; updated_at_unix_ms = $now
    }
    Write-Cg '/api/v1/cg/zones' 'zone' $zone 0 (New-Audit 'zone_created' $boardId ($now + 1) 'zone-1') | Out-Null
    $zoneRevision = 1
    Wait-PreviewAsset $red.asset_id
    Start-Sleep -Seconds 2
    Assert-WorkerAlive
    Clear-UdpBuffer
    Capture-Udp $redCapture -Phase 'red'

    $zone.content_source = [ordered]@{ kind = 'image'; asset_id = $green.asset_id }
    $zone.revision = 2
    $zone.updated_at_unix_ms = $now + 2
    Write-Cg '/api/v1/cg/zones' 'zone' $zone 1 (New-Audit 'zone_updated' $boardId ($now + 2) 'zone-2') | Out-Null
    $zoneRevision = 2
    Wait-PreviewAsset $green.asset_id
    Start-Sleep -Seconds 2
    Assert-WorkerAlive
    Clear-UdpBuffer
    Capture-Udp $greenCapture -Phase 'green'

    $redBytes = Assert-TsContinuity $redCapture
    $greenBytes = Assert-TsContinuity $greenCapture
    $redPixelValue = Read-Pixel $decoderExecutable $redCapture $redPixel
    $greenPixelValue = Read-Pixel $decoderExecutable $greenCapture $greenPixel
    if ($redPixelValue[0] -le $redPixelValue[1] + 30) { throw "Red CG image was not visible: RGB $($redPixelValue -join ',')." }
    if ($greenPixelValue[1] -le $greenPixelValue[0] + 30) { throw "Green CG image was not visible: RGB $($greenPixelValue -join ',')." }

    if ($ValidateClear) {
        $board.active = $false
        $board.revision = $boardRevision + 1
        $board.updated_at_unix_ms = $now + 3
        Write-Cg '/api/v1/cg/board' 'board' $board $boardRevision (New-Audit 'board_updated' $boardId ($now + 3) 'board-disable') | Out-Null
        $boardRevision = $board.revision
        Wait-PreviewCleared
        $clearDeadline = [DateTime]::UtcNow.AddSeconds(20)
        do {
            Assert-WorkerAlive
            Start-Sleep -Milliseconds 250
        } while ([DateTime]::UtcNow -lt $clearDeadline)
        Clear-UdpBuffer
        Capture-Udp $clearCapture -Phase 'clear'
        Assert-TsContinuity $clearCapture | Out-Null
        $clearPixelValue = Read-Pixel $decoderExecutable $clearCapture $clearPixel
        for ($index = 0; $index -lt 3; $index++) {
            if ([Math]::Abs([int]$clearPixelValue[$index] - [int]$baselinePixelValue[$index]) -gt 20) {
                throw "CG clear did not return to the baseline pixel: baseline $($baselinePixelValue -join ','), clear $($clearPixelValue -join ',')."
            }
        }
        $clearValidated = $true
    }

    [ordered]@{
        channel_id = $channelId; board_id = $boardId; red_asset_id = $red.asset_id
        green_asset_id = $green.asset_id; red_capture_bytes = $redBytes
        green_capture_bytes = $greenBytes; red_pixel = @($redPixelValue[0..2])
        green_pixel = @($greenPixelValue[0..2]); clear_validated = $clearValidated
        decoder_path = if ($explicitDecoderRequested) { $decoderExecutable } else { $null }
        decoder_sha256 = if ($explicitDecoderRequested) { Get-TownLightSha256FromStream -Stream $PinnedDecoderStream } else { $null }
        decoder_size_bytes = if ($explicitDecoderRequested) { $ExpectedDecoderSizeBytes } else { $null }
        decoder_directory_handle_count = if ($explicitDecoderRequested) { @($PinnedDecoderDirectoryHandles).Count } else { $null }
    } | ConvertTo-Json -Depth 5
}
finally {
    if ($zoneRevision -gt 0) {
        try {
            Invoke-Json 'Delete' "/api/v1/cg/zones/$zoneId" ([ordered]@{
                expected_revision = $zoneRevision
                audit = New-Audit 'zone_removed' $boardId ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) 'zone-remove'
            }) | Out-Null
        } catch { Write-Warning "Could not remove the CG image proof zone: $($_.Exception.Message)" }
    }
    $disabled = [ordered]@{
        channel_id = $channelId; display_name = 'Installed CG image proof'; output_profile = 'proof-360p30'
        output_sinks = @([ordered]@{ sink_id = $channelSinkId; label = 'CG image proof'; kind = 'udp-ts'; destination = "127.0.0.1:$DestinationPort"; loudness_target_lufs = -16.0; required = $true; enabled = $true })
        enabled = $false
    }
    try {
        # The API deliberately rejects output/profile changes while a channel is
        # enabled. Stop the exact proof destination first, then restore the prior
        # proof-owned configuration if one existed.
        Invoke-Json 'Put' '/api/v1/channels' $disabled | Out-Null
        if ($originalChannel) { Invoke-Json 'Put' '/api/v1/channels' $originalChannel | Out-Null }
    } catch { Write-Warning "Could not restore/disable the CG image proof channel: $($_.Exception.Message)" }
    $receiver.Dispose()
    foreach ($path in @($baselineCapture, $baselinePixel, $redCapture, $greenCapture, $redPixel, $greenPixel, $clearCapture, $clearPixel)) {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }
}
