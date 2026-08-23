param(
    [Parameter(Mandatory = $true)]
    [Management.Automation.PSCredential]$Credential,
    [string]$BaseUrl = 'http://127.0.0.1:4070/api/v1',
    [int]$Port = 47881
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'installed-proof-api.ps1')
$apiSession = New-TownLightProofSession -BaseUri $BaseUrl -Credential $Credential

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
$channelId = '4b46ea93-86c3-483d-8b72-dbe5f119f008'
$alertChannelId = '93669cb1-949c-4ca0-b27d-41359d54e6ac'
$signingSecret = 'installed-machine-proof-signing-key'
$target = "http://127.0.0.1:$Port/operator-alert"

$receiver = Start-Job -ArgumentList $Port, $signingSecret -ScriptBlock {
    param($ListenPort, $Secret)
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $ListenPort)
    $listener.Start()
    $received = @()
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    try {
        while ([DateTime]::UtcNow -lt $deadline -and $received.Count -lt 6) {
            if (-not $listener.Pending()) {
                Start-Sleep -Milliseconds 50
                continue
            }
            $client = $listener.AcceptTcpClient()
            try {
                $stream = $client.GetStream()
                $stream.ReadTimeout = 5000
                $bytes = [Collections.Generic.List[byte]]::new()
                $buffer = New-Object byte[] 4096
                $headerEnd = -1
                $contentLength = 0
                while ($true) {
                    $count = $stream.Read($buffer, 0, $buffer.Length)
                    if ($count -le 0) { break }
                    for ($index = 0; $index -lt $count; $index++) {
                        $bytes.Add($buffer[$index])
                    }
                    if ($headerEnd -lt 0) {
                        for ($index = 0; $index -le $bytes.Count - 4; $index++) {
                            if ($bytes[$index] -eq 13 -and $bytes[$index + 1] -eq 10 -and
                                $bytes[$index + 2] -eq 13 -and $bytes[$index + 3] -eq 10) {
                                $headerEnd = $index + 4
                                $headerText = [Text.Encoding]::ASCII.GetString($bytes.ToArray(), 0, $headerEnd)
                                $lengthLine = ($headerText -split "`r`n" | Where-Object { $_ -match '^Content-Length:\s*\d+$' } | Select-Object -First 1)
                                $contentLength = [int](($lengthLine -split ':', 2)[1].Trim())
                                break
                            }
                        }
                    }
                    if ($headerEnd -ge 0 -and $bytes.Count -ge $headerEnd + $contentLength) { break }
                }
                $allBytes = $bytes.ToArray()
                $headers = [Text.Encoding]::ASCII.GetString($allBytes, 0, $headerEnd)
                $bodyBytes = New-Object byte[] $contentLength
                [Array]::Copy($allBytes, $headerEnd, $bodyBytes, 0, $contentLength)
                $signatureLine = ($headers -split "`r`n" | Where-Object { $_ -match '^X-TownLight-Signature:' } | Select-Object -First 1)
                $signature = ($signatureLine -split ':', 2)[1].Trim()
                $hmac = [Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($Secret))
                try {
                    $expected = -join ($hmac.ComputeHash($bodyBytes) | ForEach-Object { $_.ToString('x2') })
                }
                finally {
                    $hmac.Dispose()
                }
                $response = [Text.Encoding]::ASCII.GetBytes("HTTP/1.1 204 No Content`r`nContent-Length: 0`r`nConnection: close`r`n`r`n")
                $stream.Write($response, 0, $response.Length)
                $received += [pscustomobject]@{
                    signature_valid = $signature -eq $expected
                    body = [Text.Encoding]::UTF8.GetString($bodyBytes)
                }
                $deadline = [DateTime]::UtcNow.AddSeconds(3)
            }
            finally {
                $client.Dispose()
            }
        }
    }
    finally {
        $listener.Stop()
    }
    $received
}

try {
    Start-Sleep -Milliseconds 500
    $alertBody = @{
        channel_id = $alertChannelId
        kind = 'webhook'
        label = 'Installed machine proof pager'
        enabled = $true
        target = $target
        credential = $signingSecret
    } | ConvertTo-Json -Compress
    Invoke-InstalledProofApi -Method Put -Uri "$BaseUrl/alert-channels" -ContentType 'application/json' -Body $alertBody | Out-Null

    $channelBody = @{
        channel_id = $channelId
        display_name = 'Installed alert delivery proof'
        output_profile = 'proof-360p30'
        output_sinks = @([ordered]@{ sink_id = $channelId; label = 'Primary transport'; kind = 'udp-ts'; destination = '127.0.0.1:5791'; loudness_target_lufs = -16.0; required = $true; enabled = $true })
        enabled = $true
    } | ConvertTo-Json -Compress
    Invoke-InstalledProofApi -Method Put -Uri "$BaseUrl/channels" -ContentType 'application/json' -Body $channelBody | Out-Null

    $worker = $null
    $workerDeadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not $worker -and [DateTime]::UtcNow -lt $workerDeadline) {
        $worker = Get-Process -Name 'channel-worker' -ErrorAction SilentlyContinue |
            Sort-Object Id -Descending |
            Select-Object -First 1
        if (-not $worker) { Start-Sleep -Milliseconds 100 }
    }
    if (-not $worker) { throw 'Installed channel worker did not start for the proof channel.' }
    $killedWorkerId = $worker.Id
    try {
        Stop-Process -Id $killedWorkerId -Force -ErrorAction Stop
    }
    catch {
        $terminator = Start-Process -FilePath "$env:SystemRoot\System32\taskkill.exe" `
            -ArgumentList @('/PID', $killedWorkerId, '/F') -Verb RunAs -Wait -PassThru -WindowStyle Hidden
        if ($terminator.ExitCode -ne 0) {
            throw "Elevated worker termination failed with exit code $($terminator.ExitCode)."
        }
    }

    if (-not (Wait-Job -Job $receiver -Timeout 20)) {
        throw 'Installed alert receiver did not finish before the proof deadline.'
    }
    $deliveries = @(Receive-Job -Job $receiver)
    if ($deliveries.Count -lt 2) {
        throw "Expected at least firing notifications after induced worker loss; received $($deliveries.Count)."
    }
    if ($deliveries.signature_valid -contains $false) {
        throw 'At least one installed webhook signature was invalid.'
    }
    $parsed = @($deliveries | ForEach-Object { $_.body | ConvertFrom-Json })
    if (-not ($parsed.event.condition -contains 'encoder-death')) {
        throw 'Installed proof did not deliver the encoder-death condition.'
    }
    if (-not ($parsed.notification_kind -contains 'firing')) {
        throw 'Installed proof did not deliver a firing notification.'
    }
    $alerts = @(Invoke-InstalledProofApi -Uri "$BaseUrl/alerts")
    [pscustomobject]@{
        killed_worker_id = $killedWorkerId
        signed_deliveries_received = $deliveries.Count
        notification_kinds = @($parsed.notification_kind | Sort-Object -Unique)
        conditions = @($parsed.event.condition | Sort-Object -Unique)
        durable_alert_events = $alerts.Count
        proof_target = $target
    } | ConvertTo-Json -Depth 5
}
finally {
    $disabledAlertBody = @{
        channel_id = $alertChannelId
        kind = 'webhook'
        label = 'Installed machine proof pager'
        enabled = $false
        target = $target
        credential = $signingSecret
    } | ConvertTo-Json -Compress
    try { Invoke-InstalledProofApi -Method Put -Uri "$BaseUrl/alert-channels" -ContentType 'application/json' -Body $disabledAlertBody | Out-Null } catch {}
    $disabledChannelBody = @{
        channel_id = $channelId
        display_name = 'Installed alert delivery proof'
        output_profile = 'proof-360p30'
        output_sinks = @([ordered]@{ sink_id = $channelId; label = 'Primary transport'; kind = 'udp-ts'; destination = '127.0.0.1:5791'; loudness_target_lufs = -16.0; required = $true; enabled = $true })
        enabled = $false
    } | ConvertTo-Json -Compress
    try { Invoke-InstalledProofApi -Method Put -Uri "$BaseUrl/channels" -ContentType 'application/json' -Body $disabledChannelBody | Out-Null } catch {}
    if ($receiver) {
        Stop-Job -Job $receiver -ErrorAction SilentlyContinue
        Remove-Job -Job $receiver -Force -ErrorAction SilentlyContinue
    }
}
