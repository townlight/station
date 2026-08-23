param(
    [Parameter(Mandatory = $true)]
    [Management.Automation.PSCredential]$Credential,
    [string]$BaseUrl = 'http://127.0.0.1:4070/api/v1',
    [int]$Port = 47884
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
$channelId = 'b9482efd-ee4c-451e-ab67-7fc118cf39a6'
$alertChannelId = 'bb72f9e4-40d5-42ed-ae4e-d78174d144ed'
$signingSecret = 'installed-realert-proof-signing-key'
$target = "http://127.0.0.1:$Port/operator-alert"
$helper = Join-Path $PSScriptRoot 'hold-installed-worker-down.ps1'
$originalRules = @()
$receiver = $null

function Write-Rule([object]$Rule, [string[]]$DestinationIds, [int]$RealertSeconds) {
    $body = @{
        condition = $Rule.condition
        enabled = $Rule.enabled
        severity = $Rule.severity
        channel_ids = $DestinationIds
        dedupe_window_seconds = $Rule.dedupe_window_seconds
        re_alert_after_seconds = $RealertSeconds
        scope_channel_id = $Rule.scope_channel_id
        notify_on_resolve = $Rule.notify_on_resolve
    } | ConvertTo-Json -Depth 5 -Compress
    Invoke-InstalledProofApi -Method Put -Uri "$BaseUrl/alert-rules/$($Rule.rule_id)" -ContentType 'application/json' -Body $body
}

$receiver = Start-Job -ArgumentList $Port, $signingSecret -ScriptBlock {
    param($ListenPort, $Secret)
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $ListenPort)
    $listener.Start()
    $received = @()
    $deadline = [DateTime]::UtcNow.AddSeconds(75)
    try {
        while ([DateTime]::UtcNow -lt $deadline -and $received.Count -lt 20) {
            if (-not $listener.Pending()) { Start-Sleep -Milliseconds 50; continue }
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
                    for ($index = 0; $index -lt $count; $index++) { $bytes.Add($buffer[$index]) }
                    if ($headerEnd -lt 0) {
                        for ($index = 0; $index -le $bytes.Count - 4; $index++) {
                            if ($bytes[$index] -eq 13 -and $bytes[$index + 1] -eq 10 -and $bytes[$index + 2] -eq 13 -and $bytes[$index + 3] -eq 10) {
                                $headerEnd = $index + 4
                                $headerText = [Text.Encoding]::ASCII.GetString($bytes.ToArray(), 0, $headerEnd)
                                $lengthLine = $headerText -split "`r`n" | Where-Object { $_ -match '^Content-Length:\s*\d+$' } | Select-Object -First 1
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
                $signatureLine = $headers -split "`r`n" | Where-Object { $_ -match '^X-TownLight-Signature:' } | Select-Object -First 1
                $signature = ($signatureLine -split ':', 2)[1].Trim()
                $hmac = [Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($Secret))
                try { $expected = -join ($hmac.ComputeHash($bodyBytes) | ForEach-Object { $_.ToString('x2') }) }
                finally { $hmac.Dispose() }
                $response = [Text.Encoding]::ASCII.GetBytes("HTTP/1.1 204 No Content`r`nContent-Length: 0`r`nConnection: close`r`n`r`n")
                $stream.Write($response, 0, $response.Length)
                $bodyText = [Text.Encoding]::UTF8.GetString($bodyBytes)
                $received += [pscustomobject]@{ signature_valid = $signature -eq $expected; body = $bodyText }
                $parsed = $bodyText | ConvertFrom-Json
                $kinds = @($received | ForEach-Object { ($_.body | ConvertFrom-Json).notification_kind })
                if ($parsed.notification_kind -eq 'resolved' -and $kinds -contains 'realert') { break }
            }
            finally { $client.Dispose() }
        }
    }
    finally { $listener.Stop() }
    $received
}

try {
    Start-Sleep -Milliseconds 500
    $alertBody = @{
        channel_id = $alertChannelId
        kind = 'webhook'
        label = 'Installed re-alert proof'
        enabled = $true
        target = $target
        credential = $signingSecret
    } | ConvertTo-Json -Compress
    Invoke-InstalledProofApi -Method Put -Uri "$BaseUrl/alert-channels" -ContentType 'application/json' -Body $alertBody | Out-Null

    $rules = Invoke-InstalledProofApi "$BaseUrl/alert-rules"
    $originalRules = @($rules.Where({ $_.condition -in @('encoder-death', 'off-air') }))
    if ($originalRules.Count -ne 2) { throw 'The installed encoder-death/off-air rules are missing.' }
    foreach ($rule in $originalRules) { Write-Rule -Rule $rule -DestinationIds @($alertChannelId) -RealertSeconds 1 | Out-Null }

    $channelBody = @{
        channel_id = $channelId
        display_name = 'Installed re-alert proof'
        output_profile = 'proof-360p30'
        output_sinks = @([ordered]@{ sink_id = $channelId; label = 'Primary transport'; kind = 'udp-ts'; destination = '127.0.0.1:5794'; loudness_target_lufs = -16.0; required = $true; enabled = $true })
        enabled = $true
    } | ConvertTo-Json -Compress
    Invoke-InstalledProofApi -Method Put -Uri "$BaseUrl/channels" -ContentType 'application/json' -Body $channelBody | Out-Null

    $workerDeadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        $worker = Get-Process -Name 'channel-worker' -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $worker) { Start-Sleep -Milliseconds 50 }
    } while (-not $worker -and [DateTime]::UtcNow -lt $workerDeadline)
    if (-not $worker) { throw 'The temporary installed proof worker did not start.' }

    $fault = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',$helper,'-ChannelId',$channelId,'-Seconds','8') -Verb RunAs -WindowStyle Hidden -Wait -PassThru
    if ($fault.ExitCode -ne 0) { throw "The bounded installed fault injector exited $($fault.ExitCode)." }

    $recovered = $false
    $recoveryDeadline = [DateTime]::UtcNow.AddSeconds(70)
    while (-not $recovered -and [DateTime]::UtcNow -lt $recoveryDeadline) {
        $events = Invoke-InstalledProofApi "$BaseUrl/alerts"
        $proofEvents = @($events.Where({ $_.resource_ref -eq $channelId }))
        $recovered = $proofEvents.Count -ge 1 -and -not ($proofEvents.state -contains 'firing')
        if (-not $recovered) { Start-Sleep -Milliseconds 200 }
    }
    if (-not $recovered) { throw 'The installed proof channel did not recover after the bounded fault.' }

    if (-not (Wait-Job -Job $receiver -Timeout 20)) { throw 'The installed re-alert receiver did not finish.' }
    $received = @(Receive-Job -Job $receiver)
    if ($received.signature_valid -contains $false) { throw 'An installed re-alert webhook signature was invalid.' }
    $payloads = @($received | ForEach-Object { $_.body | ConvertFrom-Json })
    if (-not ($payloads.notification_kind -contains 'firing') -or -not ($payloads.notification_kind -contains 'realert') -or -not ($payloads.notification_kind -contains 'resolved')) {
        throw 'The installed proof did not deliver firing, re-alert, and resolution notifications.'
    }
    $ledger = Invoke-InstalledProofApi "$BaseUrl/alert-deliveries"
    $proofLedger = @($ledger.Where({ $_.alert_channel_id -eq $alertChannelId -and $_.state -eq 'sent' }))
    $realertIds = @($proofLedger.Where({ $_.notification_kind -eq 'realert' }).delivery_id | Sort-Object -Unique)
    if ($realertIds.Count -lt 1) { throw 'The installed ledger contains no distinct sent re-alert.' }

    [pscustomobject]@{
        signed_deliveries_received = $received.Count
        notification_kinds = @($payloads.notification_kind | Sort-Object -Unique)
        distinct_realert_deliveries = $realertIds.Count
        event_conditions = @($payloads.event.condition | Sort-Object -Unique)
        recovered = $recovered
        source_service = 'installed LocalSystem'
    } | ConvertTo-Json -Depth 5
}
finally {
    foreach ($rule in $originalRules) {
        try { Write-Rule -Rule $rule -DestinationIds @($rule.channel_ids) -RealertSeconds $rule.re_alert_after_seconds | Out-Null } catch {}
    }
    $disabledChannelBody = @{
        channel_id = $channelId
        display_name = 'Installed re-alert proof'
        output_profile = 'proof-360p30'
        output_sinks = @([ordered]@{ sink_id = $channelId; label = 'Primary transport'; kind = 'udp-ts'; destination = '127.0.0.1:5794'; loudness_target_lufs = -16.0; required = $true; enabled = $true })
        enabled = $false
    } | ConvertTo-Json -Compress
    try { Invoke-InstalledProofApi -Method Put -Uri "$BaseUrl/channels" -ContentType 'application/json' -Body $disabledChannelBody | Out-Null } catch {}
    $disabledAlertBody = @{
        channel_id = $alertChannelId
        kind = 'webhook'
        label = 'Installed re-alert proof'
        enabled = $false
        target = $target
        credential = $signingSecret
    } | ConvertTo-Json -Compress
    try { Invoke-InstalledProofApi -Method Put -Uri "$BaseUrl/alert-channels" -ContentType 'application/json' -Body $disabledAlertBody | Out-Null } catch {}
    if ($receiver) {
        Stop-Job -Job $receiver -ErrorAction SilentlyContinue
        Remove-Job -Job $receiver -Force -ErrorAction SilentlyContinue
    }
}
