param(
    [Parameter(Mandatory = $true)]
    [string]$FixtureDirectory,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedSourceCommit,
    [string]$BaseUrl = 'http://127.0.0.1:4070/api/v1',
    [int]$FeedPort = 47886,
    [string]$ResultPath = ''
)

$ErrorActionPreference = 'Stop'
$applicationDirectory = Join-Path $env:ProgramFiles 'TownLight Station'
$dataDirectory = Join-Path $env:ProgramData 'TownLight Station'
$receiptPath = Join-Path $dataDirectory 'install-receipt.json'
$trustPath = Join-Path $applicationDirectory 'release-trust.json'
$feedPath = Join-Path $FixtureDirectory 'stable.json'
$fixtureTrustPath = Join-Path $FixtureDirectory 'release-trust.json'
$serverReadyPath = Join-Path $FixtureDirectory 'server-ready.txt'
$serverStopPath = Join-Path $FixtureDirectory 'server-stop.txt'
$serverLogPath = Join-Path $FixtureDirectory 'server-log.txt'
$server = $null
$trustInstalled = $false

function Wait-ForUpdateStatus {
    param([scriptblock]$Predicate, [int]$TimeoutSeconds = 30)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $value = Invoke-RestMethod -Uri "$BaseUrl/updates/status" -Method Get
            if (& $Predicate $value) { return $value }
        }
        catch {}
        Start-Sleep -Milliseconds 250
    }
    throw 'The installed update status did not reach the required state.'
}

$receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
if ($receipt.source_commit -ne $ExpectedSourceCommit) {
    throw 'The installed receipt does not identify the candidate under proof.'
}
$service = Get-CimInstance Win32_Service -Filter "Name='TownLightStation'"
if (-not $service -or $service.State -ne 'Running' -or $service.StartName -ne 'LocalSystem') {
    throw 'The installed TownLight Station service is not running as LocalSystem.'
}
if (-not (Test-Path -LiteralPath $feedPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $fixtureTrustPath -PathType Leaf)) {
    throw 'The signed update fixture is incomplete.'
}

try {
    Remove-Item -LiteralPath $serverReadyPath,$serverStopPath,$serverLogPath -Force -ErrorAction SilentlyContinue
    $serverScript = Join-Path $PSScriptRoot 'serve-update-proof-fixture.ps1'
    $serverArguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $serverScript + '"'),
        '-Port', [string]$FeedPort, '-FeedPath', ('"' + $feedPath + '"'),
        '-ReadyPath', ('"' + $serverReadyPath + '"'), '-StopPath', ('"' + $serverStopPath + '"'),
        '-LogPath', ('"' + $serverLogPath + '"')
    )
    $server = Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList $serverArguments `
        -WindowStyle Hidden -PassThru
    $readyDeadline = [DateTime]::UtcNow.AddSeconds(5)
    while (-not (Test-Path -LiteralPath $serverReadyPath) -and
        -not $server.HasExited -and [DateTime]::UtcNow -lt $readyDeadline) {
        Start-Sleep -Milliseconds 50
    }
    if (-not (Test-Path -LiteralPath $serverReadyPath) -or $server.HasExited) {
        throw 'The dedicated local signed-feed fixture could not start.'
    }

    Copy-Item -LiteralPath $fixtureTrustPath -Destination $trustPath -Force
    $trustInstalled = $true
    Restart-Service -Name TownLightStation -Force
    $available = Wait-ForUpdateStatus -Predicate {
        param($Status)
        $Status.configured -and $Status.latest_check.outcome -eq 'update_available' -and
            $Status.latest_check.trigger -eq 'automatic'
    }
    if ($available.current_version -ne '0.1.0' -or
        $available.source_commit -ne $ExpectedSourceCommit -or
        $available.release_channel -ne 'stable' -or
        $available.feed_origin -ne "http://127.0.0.1:$FeedPort" -or
        $available.latest_check.offered_release.version -ne '0.2.0' -or
        $available.latest_check.offered_release.source_commit -ne ('b' * 40) -or
        $available.latest_check.error) {
        throw 'The installed automatic check did not preserve the verified release identity.'
    }
    $notes = (Invoke-WebRequest -Uri "$BaseUrl/updates/release-notes?version=0.2.0" -UseBasicParsing).Content
    if ($notes -ne "# TownLight Station 0.2.0`n`nInstalled signed-discovery proof release.") {
        throw 'The installed API did not return the exact verified release notes.'
    }
    $alerts = Invoke-RestMethod -Uri "$BaseUrl/alerts"
    $availableAlerts = @($alerts.Where({
        $_.condition -eq 'update-available' -and $_.severity -eq 'info' -and
        $_.state -eq 'firing' -and $_.resource_ref -eq '0.2.0'
    }))
    if ($availableAlerts.Count -ne 1 -or [uint64]($availableAlerts[0].occurrence_count) -lt 1) {
        throw 'The verified installed offer did not create one firing informational alert.'
    }
    $availableAlert = $availableAlerts[0]
    $stagedFiles = @(
        Get-ChildItem -LiteralPath $dataDirectory -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '(?i)\.tlu$|TownLight-Station-0\.2\.0' }
    )
    if ($stagedFiles.Count -ne 0) {
        throw 'Discovery downloaded or staged a package without operator approval.'
    }

    Remove-Item -LiteralPath $trustPath -Force
    $trustInstalled = $false
    Restart-Service -Name TownLightStation -Force
    $clean = Wait-ForUpdateStatus -Predicate {
        param($Status)
        -not $Status.configured -and $null -eq $Status.latest_check
    }
    $resolvedAlert = $null
    $alertDeadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not $resolvedAlert -and [DateTime]::UtcNow -lt $alertDeadline) {
        $cleanAlerts = Invoke-RestMethod -Uri "$BaseUrl/alerts"
        $firingOffers = @($cleanAlerts.Where({
            $_.condition -eq 'update-available' -and $_.resource_ref -eq '0.2.0' -and
            $_.state -eq 'firing'
        }))
        if ($firingOffers.Count -eq 0) {
            $resolvedAlert = @($cleanAlerts.Where({
                $_.condition -eq 'update-available' -and $_.resource_ref -eq '0.2.0' -and
                $_.state -eq 'resolved' -and $null -ne $_.resolved_at_unix_ms
            }))[0]
        }
        if (-not $resolvedAlert) { Start-Sleep -Milliseconds 100 }
    }
    if (-not $resolvedAlert) {
        throw 'Removing installer-owned trust did not resolve the prior offer alert.'
    }

    $result = [ordered]@{
        source_commit = $receipt.source_commit
        service_account = $service.StartName
        check_trigger = $available.latest_check.trigger
        check_outcome = $available.latest_check.outcome
        feed_origin = $available.feed_origin
        verified_offer_version = $available.latest_check.offered_release.version
        verified_offer_commit = $available.latest_check.offered_release.source_commit
        release_notes_exact = $true
        alert_severity = $availableAlert.severity
        alert_occurrences = [uint64]($availableAlert.occurrence_count)
        packages_downloaded_or_staged = $stagedFiles.Count
        proof_trust_removed = -not (Test-Path -LiteralPath $trustPath)
        clean_configured_state = $clean.configured
        stale_offer_hidden = $null -eq $clean.latest_check
        offer_alert_resolved = $resolvedAlert.state -eq 'resolved'
    } | ConvertTo-Json -Depth 6
    if ($ResultPath) {
        Set-Content -LiteralPath $ResultPath -Value $result -Encoding utf8NoBOM
    }
    $result
}
catch {
    if ($ResultPath) {
        $latestStatus = try {
            Invoke-RestMethod -Uri "$BaseUrl/updates/status" -Method Get
        }
        catch { $null }
        [ordered]@{
            error = $_.Exception.Message
            position = $_.InvocationInfo.PositionMessage
            latest_status = $latestStatus
            server_state = if ($server -and $server.HasExited) { "exited-$($server.ExitCode)" } elseif ($server) { 'running' } else { 'not-started' }
            server_output = if (Test-Path -LiteralPath $serverLogPath) { (Get-Content -Raw -LiteralPath $serverLogPath).Trim() } else { '' }
        } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ResultPath -Encoding utf8NoBOM
    }
    throw
}
finally {
    if ($trustInstalled -and (Test-Path -LiteralPath $trustPath -PathType Leaf)) {
        Remove-Item -LiteralPath $trustPath -Force -ErrorAction SilentlyContinue
        Restart-Service -Name TownLightStation -Force -ErrorAction SilentlyContinue
    }
    if ($server) {
        Set-Content -LiteralPath $serverStopPath -Value 'stop' -Encoding ascii -ErrorAction SilentlyContinue
        if (-not $server.WaitForExit(3000)) { $server.Kill() }
        $server.Dispose()
    }
}
