param(
    [string]$BaseUrl = 'http://127.0.0.1:4070/api/v1',
    [int]$Port = 47882
)

$ErrorActionPreference = 'Stop'
$alertChannelId = '83d2171d-993f-4d8e-8dc4-40573c1f5f86'
$signingSecret = 'installed-send-test-signing-key'
$target = "http://127.0.0.1:$Port/operator-test"

$receiver = Start-Job -ArgumentList $Port, $signingSecret -ScriptBlock {
    param($ListenPort, $Secret)
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $ListenPort)
    $listener.Start()
    try {
        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        while (-not $listener.Pending() -and [DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 50
        }
        if (-not $listener.Pending()) { throw 'No webhook arrived before the receiver deadline.' }
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
                        if ($bytes[$index] -eq 13 -and $bytes[$index + 1] -eq 10 -and
                            $bytes[$index + 2] -eq 13 -and $bytes[$index + 3] -eq 10) {
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
            [pscustomobject]@{
                signature_valid = $signature -eq $expected
                body = [Text.Encoding]::UTF8.GetString($bodyBytes)
            }
        }
        finally { $client.Dispose() }
    }
    finally { $listener.Stop() }
}

try {
    Start-Sleep -Milliseconds 500
    $configuration = @{
        channel_id = $alertChannelId
        kind = 'webhook'
        label = 'Installed send-test proof'
        enabled = $false
        target = $target
        credential = $signingSecret
    } | ConvertTo-Json -Compress
    $publicChannel = Invoke-RestMethod -Method Put -Uri "$BaseUrl/alert-channels" -ContentType 'application/json' -Body $configuration
    if ($publicChannel.enabled) { throw 'The proof destination was not stored disabled.' }
    if ($publicChannel.PSObject.Properties.Name -contains 'target' -or $publicChannel.PSObject.Properties.Name -contains 'credential') {
        throw 'The public destination response exposed a secret field.'
    }

    $queued = Invoke-RestMethod -Method Post -Uri "$BaseUrl/alert-channels/$alertChannelId/test" -ContentType 'application/json' -Body ''
    if ($queued.notification_kind -ne 'test' -or $queued.state -ne 'pending') {
        throw 'The send-test request did not return a pending test delivery.'
    }
    if ($queued.PSObject.Properties.Name -contains 'target' -or $queued.PSObject.Properties.Name -contains 'credential') {
        throw 'The public delivery response exposed a secret field.'
    }

    if (-not (Wait-Job -Job $receiver -Timeout 20)) { throw 'The installed webhook receiver did not finish.' }
    $received = Receive-Job -Job $receiver
    if (-not $received.signature_valid) { throw 'The installed send-test signature was invalid.' }
    $payload = $received.body | ConvertFrom-Json
    if ($payload.notification_kind -ne 'test' -or $payload.event.severity -ne 'info' -or $null -eq $payload.event.resolved_at_unix_ms) {
        throw 'The installed send-test payload did not preserve its non-firing audit semantics.'
    }

    $delivery = $null
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not $delivery -and [DateTime]::UtcNow -lt $deadline) {
        $ledger = Invoke-RestMethod -Uri "$BaseUrl/alert-deliveries"
        $delivery = $ledger.Where({ $_.delivery_id -eq $queued.delivery_id -and $_.state -eq 'sent' }, 'First')
        if (-not $delivery) { Start-Sleep -Milliseconds 100 }
    }
    if (-not $delivery) { throw 'The installed delivery ledger did not reach sent.' }
    $alertLedger = Invoke-RestMethod -Uri "$BaseUrl/alerts"
    $auditEvent = $alertLedger.Where({ $_.event_id -eq $queued.event_id }, 'First')
    if (-not $auditEvent -or $auditEvent.state -ne 'resolved' -or $auditEvent.severity -ne 'info') {
        throw 'The installed audit event was missing or unhealthy.'
    }

    [pscustomobject]@{
        configured_enabled = $publicChannel.enabled
        public_target = $publicChannel.target_redacted
        notification_kind = $payload.notification_kind
        audit_state = $auditEvent.state
        audit_severity = $auditEvent.severity
        delivery_state = $delivery.state
        attempts = $delivery.attempts
        signature_valid = $received.signature_valid
        source_service = 'installed LocalSystem'
    } | ConvertTo-Json
}
finally {
    if ($receiver) {
        Stop-Job -Job $receiver -ErrorAction SilentlyContinue
        Remove-Job -Job $receiver -Force -ErrorAction SilentlyContinue
    }
}
