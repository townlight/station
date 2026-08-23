param(
    [Parameter(Mandatory = $true)]
    [Management.Automation.PSCredential]$Credential,
    [string]$BaseUrl = 'http://127.0.0.1:4070/api/v1',
    [string]$Manifest = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\candidate-manifest.json'),
    [int]$WebhookPort = 47885
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
$dataDirectory = Join-Path $env:ProgramData 'TownLight Station'
$receiptPath = Join-Path $dataDirectory 'install-receipt.json'
$outputChannelId = '5f0ea69e-63b5-4ea2-9748-85540fc669fa'
$alertChannelId = '8fce373f-7461-42cb-80c1-d6ef56395454'
$signingSecret = 'installed-self-test-signing-key'
$target = "http://127.0.0.1:$WebhookPort/self-test"

$manifestDocument = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
$receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
if ($receipt.source_commit -ne $manifestDocument.source_commit) {
    throw 'The installed receipt does not identify the candidate under proof.'
}
$service = Get-CimInstance Win32_Service -Filter "Name='TownLightStation'"
if (-not $service -or $service.State -ne 'Running' -or $service.StartName -ne 'LocalSystem') {
    throw 'The installed TownLight Station service is not running as LocalSystem.'
}

function Assert-SelfTestRecord {
    param(
        $Record,
        [string]$ExpectedKind,
        [string[]]$ExpectedChecks
    )

    if ($Record.kind -ne $ExpectedKind -or $Record.trigger -ne 'manual' -or
        $null -ne $Record.schedule_period_key -or $Record.status -ne 'pass') {
        throw "The installed $ExpectedKind self-test did not produce a manual passing result."
    }
    $actualChecks = @($Record.checks.PSObject.Properties.Name | Sort-Object)
    $wantedChecks = @($ExpectedChecks | Sort-Object)
    if (($actualChecks -join '|') -ne ($wantedChecks -join '|')) {
        throw "The installed $ExpectedKind self-test returned an unexpected check set."
    }
    foreach ($check in $ExpectedChecks) {
        if ($Record.checks.$check -ne $true) {
            throw "The installed $ExpectedKind check failed: $check"
        }
    }
    if (-not $Record.evidence_path -or -not (Test-Path -LiteralPath $Record.evidence_path -PathType Leaf)) {
        throw "The installed $ExpectedKind evidence file is missing."
    }
    $expectedRoot = [IO.Path]::GetFullPath((Join-Path $dataDirectory 'self-tests'))
    $actualPath = [IO.Path]::GetFullPath($Record.evidence_path)
    if (-not $actualPath.StartsWith($expectedRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The installed $ExpectedKind evidence escaped the managed data directory."
    }
    $evidence = Get-Content -LiteralPath $actualPath -Raw | ConvertFrom-Json
    if ($evidence.self_test_id -ne $Record.self_test_id -or
        $evidence.status -ne $Record.status -or $evidence.summary -ne $Record.summary) {
        throw "The installed $ExpectedKind evidence does not match the durable API record."
    }
    foreach ($check in $ExpectedChecks) {
        if ($evidence.checks.$check -ne $Record.checks.$check) {
            throw "The installed $ExpectedKind evidence disagrees on check: $check"
        }
    }
}

$receiver = $null
$outputConfigured = $false
$alertConfigured = $false
try {
    $schedules = Invoke-InstalledProofApi -Uri "$BaseUrl/self-test-schedules"
    $dailySchedule = $schedules.Where({ $_.kind -eq 'daily' }, 'First')
    $weeklySchedule = $schedules.Where({ $_.kind -eq 'weekly' }, 'First')
    if ($schedules.Count -ne 2 -or -not $dailySchedule -or -not $weeklySchedule -or
        -not $dailySchedule.enabled -or [int]$dailySchedule.local_hour -ne 2 -or
        [int]$dailySchedule.local_minute -ne 0 -or $null -ne $dailySchedule.weekday -or
        -not $weeklySchedule.enabled -or [int]$weeklySchedule.local_hour -ne 3 -or
        [int]$weeklySchedule.local_minute -ne 0 -or [int]$weeklySchedule.weekday -ne 0) {
        throw 'The installed daily and weekly schedule defaults are not intact.'
    }

    $outputBody = @{
        channel_id = $outputChannelId
        display_name = 'Installed self-test proof output'
        output_profile = 'proof-360p30'
        output_sinks = @([ordered]@{ sink_id = $outputChannelId; label = 'Primary transport'; kind = 'udp-ts'; destination = '127.0.0.1:5795'; loudness_target_lufs = -16.0; required = $true; enabled = $true })
        enabled = $true
    } | ConvertTo-Json -Compress
    Invoke-InstalledProofApi -Method Put -Uri "$BaseUrl/channels" -ContentType 'application/json' -Body $outputBody | Out-Null
    $outputConfigured = $true

    $runtimeChannel = $null
    $runtimeDeadline = [DateTime]::UtcNow.AddSeconds(15)
    while (-not $runtimeChannel -and [DateTime]::UtcNow -lt $runtimeDeadline) {
        $runtime = Invoke-InstalledProofApi -Uri "$BaseUrl/runtime-safe-to-air"
        $runtimeChannel = @($runtime.channels).Where({
            $_.channel_id -eq $outputChannelId -and $_.color -eq 'green' -and
            ($_.state -eq 'healthy_fallback' -or $_.state -eq 'on_air')
        }, 'First')
        if (-not $runtimeChannel) { Start-Sleep -Milliseconds 250 }
    }
    if (-not $runtimeChannel) {
        throw 'The installed proof output did not reach a fresh healthy runtime state.'
    }

    $resource = $null
    $resourceDeadline = [DateTime]::UtcNow.AddSeconds(75)
    while (-not $resource -and [DateTime]::UtcNow -lt $resourceDeadline) {
        $candidate = Invoke-InstalledProofApi -Uri "$BaseUrl/system-resources/latest"
        $age = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() - [int64]$candidate.sampled_at_unix_ms
        if ($age -ge -5000 -and $age -le 90000 -and $candidate.database_reachable -and
            $candidate.backup_volume_writable -and $candidate.service_running) {
            $resource = $candidate
        }
        if (-not $resource) { Start-Sleep -Seconds 1 }
    }
    if (-not $resource) { throw 'No fresh healthy installed resource sample became available.' }

    $dailyChecks = @(
        'database_integrity', 'backup_write_read_delete', 'station_service_binary',
        'channel_worker_binary', 'private_media_runtime', 'station_profile_configured',
        'enabled_output_configured', 'enabled_output_health', 'recent_resource_sample'
    )
    $daily = Invoke-InstalledProofApi -Method Post -Uri "$BaseUrl/self-tests/run?kind=daily"
    Assert-SelfTestRecord -Record $daily -ExpectedKind 'daily' -ExpectedChecks $dailyChecks

    $alertBody = @{
        channel_id = $alertChannelId
        kind = 'webhook'
        label = 'Installed weekly self-test proof'
        enabled = $true
        target = $target
        credential = $signingSecret
    } | ConvertTo-Json -Compress
    Invoke-InstalledProofApi -Method Put -Uri "$BaseUrl/alert-channels" -ContentType 'application/json' -Body $alertBody | Out-Null
    $alertConfigured = $true

    $receiver = Start-Job -ArgumentList $WebhookPort, $signingSecret -ScriptBlock {
        param($ListenPort, $Secret)
        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $ListenPort)
        $listener.Start()
        try {
            $deadline = [DateTime]::UtcNow.AddSeconds(30)
            while (-not $listener.Pending() -and [DateTime]::UtcNow -lt $deadline) {
                Start-Sleep -Milliseconds 50
            }
            if (-not $listener.Pending()) { throw 'No weekly alert-channel test arrived.' }
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
                                $lengthLine = $headerText -split "`r`n" |
                                    Where-Object { $_ -match '^Content-Length:\s*\d+$' } |
                                    Select-Object -First 1
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
                $signatureLine = $headers -split "`r`n" |
                    Where-Object { $_ -match '^X-TownLight-Signature:' } |
                    Select-Object -First 1
                $signature = ($signatureLine -split ':', 2)[1].Trim()
                $hmac = [Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($Secret))
                try {
                    $expected = -join ($hmac.ComputeHash($bodyBytes) | ForEach-Object { $_.ToString('x2') })
                }
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
    Start-Sleep -Milliseconds 500

    $weeklyChecks = $dailyChecks + @('database_restore_rehearsal', 'alert_destination_test_queued')
    $weekly = Invoke-InstalledProofApi -Method Post -Uri "$BaseUrl/self-tests/run?kind=weekly"
    Assert-SelfTestRecord -Record $weekly -ExpectedKind 'weekly' -ExpectedChecks $weeklyChecks

    if (-not (Wait-Job -Job $receiver -Timeout 35)) {
        throw 'The installed weekly alert-channel receiver did not finish.'
    }
    $received = Receive-Job -Job $receiver
    if (-not $received.signature_valid) { throw 'The installed weekly send-test signature was invalid.' }
    $payload = $received.body | ConvertFrom-Json
    if ($payload.notification_kind -ne 'test' -or $payload.event.severity -ne 'info' -or
        $null -eq $payload.event.resolved_at_unix_ms -or
        $payload.event.resource_ref -ne $alertChannelId) {
        throw 'The installed weekly destination payload did not preserve safe test semantics.'
    }

    $history = Invoke-InstalledProofApi -Uri "$BaseUrl/self-tests"
    foreach ($record in @($daily, $weekly)) {
        if (-not $history.Where({ $_.self_test_id -eq $record.self_test_id }, 'First')) {
            throw "The installed history omitted $($record.self_test_id)."
        }
    }
    $delivery = $null
    $deliveryDeadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not $delivery -and [DateTime]::UtcNow -lt $deliveryDeadline) {
        $ledger = Invoke-InstalledProofApi -Uri "$BaseUrl/alert-deliveries"
        $delivery = $ledger.Where({
            $_.event_id -eq $payload.event.event_id -and $_.alert_channel_id -eq $alertChannelId -and
            $_.notification_kind -eq 'test' -and $_.state -eq 'sent'
        }, 'First')
        if (-not $delivery) { Start-Sleep -Milliseconds 100 }
    }
    if (-not $delivery) { throw 'The installed weekly alert-channel test did not reach sent.' }

    $restoreLeaks = @(
        Get-ChildItem -LiteralPath (Join-Path $dataDirectory 'self-tests') `
            -Filter 'database-restore-rehearsal.db*' -Force -ErrorAction SilentlyContinue
    )
    if ($restoreLeaks.Count -ne 0) {
        throw 'The installed weekly restore rehearsal left database artifacts behind.'
    }

    [ordered]@{
        source_commit = $receipt.source_commit
        service_account = $service.StartName
        schedules = @('daily 02:00 local', 'weekly Sunday 03:00 local')
        healthy_output_state = $runtimeChannel.state
        resource_sample_id = [uint64]$resource.sample_id
        daily_status = $daily.status
        daily_checks_passed = $dailyChecks.Count
        daily_evidence = $daily.evidence_path
        weekly_status = $weekly.status
        weekly_checks_passed = $weeklyChecks.Count
        weekly_evidence = $weekly.evidence_path
        restore_artifacts_remaining = $restoreLeaks.Count
        alert_delivery_state = $delivery.state
        alert_signature_valid = $received.signature_valid
        durable_history_records = $history.Count
    } | ConvertTo-Json -Depth 5
}
finally {
    if ($alertConfigured) {
        $disabledAlertBody = @{
            channel_id = $alertChannelId
            kind = 'webhook'
            label = 'Installed weekly self-test proof'
            enabled = $false
            target = $target
            credential = $signingSecret
        } | ConvertTo-Json -Compress
        try {
            Invoke-InstalledProofApi -Method Put -Uri "$BaseUrl/alert-channels" `
                -ContentType 'application/json' -Body $disabledAlertBody | Out-Null
        }
        catch {}
    }
    if ($outputConfigured) {
        $disabledOutputBody = @{
            channel_id = $outputChannelId
            display_name = 'Installed self-test proof output'
            output_profile = 'proof-360p30'
            output_sinks = @([ordered]@{ sink_id = $outputChannelId; label = 'Primary transport'; kind = 'udp-ts'; destination = '127.0.0.1:5795'; loudness_target_lufs = -16.0; required = $true; enabled = $true })
            enabled = $false
        } | ConvertTo-Json -Compress
        try {
            Invoke-InstalledProofApi -Method Put -Uri "$BaseUrl/channels" `
                -ContentType 'application/json' -Body $disabledOutputBody | Out-Null
        }
        catch {}
    }
    if ($receiver) {
        Stop-Job -Job $receiver -ErrorAction SilentlyContinue
        Remove-Job -Job $receiver -Force -ErrorAction SilentlyContinue
    }
}
