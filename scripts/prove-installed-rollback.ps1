param(
    [Parameter(Mandatory = $true)][string]$FixtureDirectory,
    [Parameter(Mandatory = $true)][string]$ExpectedSourceCommit,
    [string]$BaseUrl = 'http://127.0.0.1:4070/api/v1',
    [int]$FeedPort = 47888,
    [string]$ResultPath = ''
)

$ErrorActionPreference = 'Stop'
$app = Join-Path $env:ProgramFiles 'TownLight Station'
$data = Join-Path $env:ProgramData 'TownLight Station'
$trustPath = Join-Path $app 'release-trust.json'
$feedPath = Join-Path $FixtureDirectory 'stable.json'
$packagePath = Join-Path $FixtureDirectory 'TownLight-Station-0.3.0.tlu'
$fixtureTrust = Join-Path $FixtureDirectory 'release-trust.json'
$readyPath = Join-Path $FixtureDirectory 'server-ready.txt'
$stopPath = Join-Path $FixtureDirectory 'server-stop.txt'
$logPath = Join-Path $FixtureDirectory 'server-log.txt'
$server = $null
$trustInstalled = $false

function Wait-Until {
    param([scriptblock]$Action, [scriptblock]$Predicate, [int]$TimeoutSeconds = 120)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $last = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $last = & $Action
            if (& $Predicate $last) { return $last }
        } catch {}
        Start-Sleep -Milliseconds 250
    }
    throw "Timed out waiting for rollback state. Last value: $($last | ConvertTo-Json -Depth 5 -Compress)"
}

foreach ($required in @($feedPath, $packagePath, $fixtureTrust)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Rollback fixture is missing: $required"
    }
}

try {
    $installPath = Join-Path $data 'install-receipt.json'
    $activePath = Join-Path $data 'active-slot.json'
    $receiptBefore = Get-Content -LiteralPath $installPath -Raw | ConvertFrom-Json
    $activeBefore = Get-Content -LiteralPath $activePath -Raw | ConvertFrom-Json
    $activeReceiptHash = (Get-FileHash -LiteralPath $activePath -Algorithm SHA256).Hash
    if ($receiptBefore.version -ne '0.2.0' -or $receiptBefore.source_commit -ne $ExpectedSourceCommit -or
        $activeBefore.active.package_manifest.version -ne '0.2.0') {
        throw 'Installed identity does not match the rollback proof starting point.'
    }
    $relative = $activeBefore.active.relative_directory.Replace('/', '\')
    $previousStationd = Join-Path (Join-Path $app $relative) 'stationd.exe'
    $previousHash = (Get-FileHash -LiteralPath $previousStationd -Algorithm SHA256).Hash
    $updaterBefore = Get-CimInstance Win32_Service -Filter "Name='TownLightStationUpdater'"
    $greenBefore = Wait-Until { Invoke-RestMethod "$BaseUrl/runtime-safe-to-air" } `
        { param($value) $value.color -eq 'green' }

    Remove-Item -LiteralPath $readyPath,$stopPath,$logPath -Force -ErrorAction SilentlyContinue
    $serverScript = Join-Path $PSScriptRoot 'serve-update-proof-fixture.ps1'
    $serverArgs = @(
        '-NoProfile','-ExecutionPolicy','Bypass','-File',('"' + $serverScript + '"'),
        '-Port',[string]$FeedPort,'-FeedPath',('"' + $feedPath + '"'),
        '-PackagePath',('"' + $packagePath + '"'),'-ReadyPath',('"' + $readyPath + '"'),
        '-StopPath',('"' + $stopPath + '"'),'-LogPath',('"' + $logPath + '"')
    )
    $server = Start-Process (Get-Process -Id $PID).Path -ArgumentList $serverArgs -WindowStyle Hidden -PassThru
    $readyDeadline = [DateTime]::UtcNow.AddSeconds(5)
    while (-not (Test-Path -LiteralPath $readyPath) -and -not $server.HasExited -and
        [DateTime]::UtcNow -lt $readyDeadline) { Start-Sleep -Milliseconds 50 }
    if (-not (Test-Path -LiteralPath $readyPath) -or $server.HasExited) {
        throw 'The rollback proof server did not start.'
    }

    Copy-Item -LiteralPath $fixtureTrust -Destination $trustPath -Force
    $trustInstalled = $true
    Restart-Service TownLightStation -Force
    $offer = Wait-Until { Invoke-RestMethod "$BaseUrl/updates/status" } `
        { param($value) $value.latest_check.outcome -eq 'update_available' }
    if ($offer.current_version -ne '0.2.0' -or
        $offer.latest_check.offered_release.version -ne '0.3.0') {
        throw 'Signed rollback-test offer identity was not accepted exactly.'
    }
    Invoke-RestMethod "$BaseUrl/updates/stage" -Method Post | Out-Null
    $prepared = Invoke-RestMethod "$BaseUrl/updates/prepare" -Method Post
    $candidate = Join-Path (Join-Path (Join-Path $app 'slots') $prepared.slot_directory_name) 'stationd.exe'
    if ((Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash -eq $previousHash) {
        throw 'Rollback proof candidate did not contain the deliberate executable failure.'
    }
    Wait-Until { Invoke-RestMethod "$BaseUrl/runtime-safe-to-air" } `
        { param($value) $value.color -eq 'green' } | Out-Null
    $body = @{ requested_by = 'installed-rollback-proof'; confirmation = 'INSTALL UPDATE' } | ConvertTo-Json
    $request = Invoke-RestMethod "$BaseUrl/updates/apply" -Method Post -ContentType 'application/json' -Body $body

    $transactionPath = Join-Path (Join-Path $data 'updates\maintenance-transactions') ($request.transaction_id + '.json')
    $transaction = Wait-Until {
        if (Test-Path -LiteralPath $transactionPath) {
            Get-Content -LiteralPath $transactionPath -Raw | ConvertFrom-Json
        }
    } { param($value) $value.phase -eq 'rolled_back' }
    $stationService = Wait-Until { Get-CimInstance Win32_Service -Filter "Name='TownLightStation'" } `
        { param($value) $value.State -eq 'Running' -and $value.ProcessId -gt 0 }
    $stationProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($stationService.ProcessId)"
    if ([IO.Path]::GetFullPath($stationProcess.ExecutablePath) -ne [IO.Path]::GetFullPath($previousStationd) -or
        (Get-FileHash -LiteralPath $stationProcess.ExecutablePath -Algorithm SHA256).Hash -ne $previousHash) {
        throw 'Rollback did not restore the exact prior running application.'
    }
    $greenAfter = Wait-Until { Invoke-RestMethod "$BaseUrl/runtime-safe-to-air" } `
        { param($value) $value.color -eq 'green' }
    if ((Get-FileHash -LiteralPath $activePath -Algorithm SHA256).Hash -ne $activeReceiptHash) {
        throw 'Rollback changed the previously committed active-slot receipt.'
    }
    $receiptAfter = Get-Content -LiteralPath $installPath -Raw | ConvertFrom-Json
    if ($receiptAfter.version -ne $receiptBefore.version -or
        $receiptAfter.source_commit -ne $receiptBefore.source_commit -or
        $receiptAfter.media_runtime_sha256 -ne $receiptBefore.media_runtime_sha256) {
        throw 'Rollback changed the previously committed install identity.'
    }
    $completed = Join-Path (Join-Path $data 'updates\maintenance-requests-completed') ($request.transaction_id + '.json')
    if (-not (Test-Path -LiteralPath $completed -PathType Leaf)) {
        throw 'The rolled-back request was not archived.'
    }
    $updaterAfter = Get-CimInstance Win32_Service -Filter "Name='TownLightStationUpdater'"
    if ($updaterAfter.State -ne 'Running' -or $updaterAfter.ProcessId -ne $updaterBefore.ProcessId) {
        throw 'The watchdog did not survive the failed candidate and rollback.'
    }

    Remove-Item -LiteralPath $trustPath -Force
    $trustInstalled = $false
    Restart-Service TownLightStation -Force
    $greenAfterRestart = Wait-Until { Invoke-RestMethod "$BaseUrl/runtime-safe-to-air" } `
        { param($value) $value.color -eq 'green' }
    $serviceRestarted = Get-CimInstance Win32_Service -Filter "Name='TownLightStation'"
    $processRestarted = Get-CimInstance Win32_Process -Filter "ProcessId=$($serviceRestarted.ProcessId)"
    if ([IO.Path]::GetFullPath($processRestarted.ExecutablePath) -ne [IO.Path]::GetFullPath($previousStationd)) {
        throw 'The restored prior application did not survive a later restart.'
    }

    $result = [ordered]@{
        source_commit = $ExpectedSourceCommit
        attempted_version = '0.3.0'
        restored_version = $receiptAfter.version
        transaction_id = $request.transaction_id
        transaction_phase = $transaction.phase
        failure_detail = $transaction.detail
        restored_process_image = $stationProcess.ExecutablePath
        restored_process_sha256 = $previousHash
        active_receipt_byte_exact = $true
        install_identity_preserved = $true
        safe_to_air_before = $greenBefore.color
        safe_to_air_after = $greenAfter.color
        safe_to_air_after_restart = $greenAfterRestart.color
        watchdog_process_survived = $updaterAfter.ProcessId -eq $updaterBefore.ProcessId
        completed_request_archived = Test-Path -LiteralPath $completed
        restored_application_survived_restart = $processRestarted.ExecutablePath -eq $previousStationd
        release_server_requests = @(Get-Content -LiteralPath $logPath)
    } | ConvertTo-Json -Depth 6
    if ($ResultPath) { Set-Content -LiteralPath $ResultPath -Value $result -Encoding utf8NoBOM }
    $result
} catch {
    if ($ResultPath) {
        @{ error = $_.Exception.Message; position = $_.InvocationInfo.PositionMessage } |
            ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ResultPath -Encoding utf8NoBOM
    }
    throw
} finally {
    if ($trustInstalled -and (Test-Path -LiteralPath $trustPath -PathType Leaf)) {
        Remove-Item -LiteralPath $trustPath -Force -ErrorAction SilentlyContinue
        Restart-Service TownLightStation -Force -ErrorAction SilentlyContinue
    }
    if ($server) {
        Set-Content -LiteralPath $stopPath -Value stop -Encoding ascii -ErrorAction SilentlyContinue
        if (-not $server.WaitForExit(3000)) { $server.Kill() }
        $server.Dispose()
    }
}
