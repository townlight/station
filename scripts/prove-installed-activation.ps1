param(
    [Parameter(Mandatory = $true)]
    [Management.Automation.PSCredential]$Credential,
    [Parameter(Mandatory = $true)]
    [string]$FixtureDirectory,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedSourceCommit,
    [string]$BaseUrl = 'http://127.0.0.1:4070/api/v1',
    [int]$FeedPort = 47887,
    [string]$ResultPath = ''
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
$applicationDirectory = Join-Path $env:ProgramFiles 'TownLight Station'
$dataDirectory = Join-Path $env:ProgramData 'TownLight Station'
$trustPath = Join-Path $applicationDirectory 'release-trust.json'
$feedPath = Join-Path $FixtureDirectory 'stable.json'
$packagePath = Join-Path $FixtureDirectory 'TownLight-Station-0.2.0.tlu'
$fixtureTrustPath = Join-Path $FixtureDirectory 'release-trust.json'
$readyPath = Join-Path $FixtureDirectory 'server-ready.txt'
$stopPath = Join-Path $FixtureDirectory 'server-stop.txt'
$logPath = Join-Path $FixtureDirectory 'server-log.txt'
$server = $null
$trustInstalled = $false

function Connect-AfterServiceBoundary {
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            Connect-TownLightProofSession -Session $apiSession -Credential $Credential | Out-Null
            return
        }
        catch { Start-Sleep -Milliseconds 250 }
    }
    throw 'The installed station did not accept a fresh authenticated session after its service boundary.'
}

function Wait-Until {
    param([scriptblock]$Action, [scriptblock]$Predicate, [int]$TimeoutSeconds = 90)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $last = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $last = & $Action
            if (& $Predicate $last) { return $last }
        }
        catch {}
        Start-Sleep -Milliseconds 250
    }
    throw "Timed out waiting for installed activation state. Last value: $($last | ConvertTo-Json -Depth 5 -Compress)"
}

foreach ($required in @($feedPath, $packagePath, $fixtureTrustPath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Activation fixture is missing: $required"
    }
}

try {
    $installed = Get-Content -LiteralPath (Join-Path $dataDirectory 'install-receipt.json') -Raw | ConvertFrom-Json
    if ($installed.source_commit -ne $ExpectedSourceCommit -or $installed.version -ne '0.1.0') {
        throw 'Installed identity does not match the activation proof starting point.'
    }
    $updaterBefore = Get-CimInstance Win32_Service -Filter "Name='TownLightStationUpdater'"
    if (-not $updaterBefore -or $updaterBefore.State -ne 'Running' -or
        $updaterBefore.StartName -ne 'LocalSystem' -or -not $updaterBefore.ProcessId) {
        throw 'The independent maintenance watchdog is not running as LocalSystem.'
    }

    $channel = [ordered]@{
        channel_id = '70707070-8080-4090-80a0-b0b0b0b0b0b0'
        display_name = 'Activation Proof Output'
        output_profile = 'proof-360p30'
        output_sinks = @([ordered]@{ sink_id = '70707070-8080-4090-80a0-b0b0b0b0b0b0'; label = 'Primary transport'; kind = 'udp-ts'; destination = '127.0.0.1:5910'; loudness_target_lufs = -16.0; required = $true; enabled = $true })
        enabled = $true
    } | ConvertTo-Json
    Invoke-InstalledProofApi -Uri "$BaseUrl/channels" -Method Put -ContentType 'application/json' -Body $channel | Out-Null
    $greenBefore = Wait-Until -Action { Invoke-InstalledProofApi -Uri "$BaseUrl/runtime-safe-to-air" } `
        -Predicate { param($value) $value.color -eq 'green' -and $value.channels.Count -eq 1 }

    Remove-Item -LiteralPath $readyPath,$stopPath,$logPath -Force -ErrorAction SilentlyContinue
    $serverScript = Join-Path $PSScriptRoot 'serve-update-proof-fixture.ps1'
    $arguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $serverScript + '"'),
        '-Port', [string]$FeedPort, '-FeedPath', ('"' + $feedPath + '"'),
        '-PackagePath', ('"' + $packagePath + '"'), '-ReadyPath', ('"' + $readyPath + '"'),
        '-StopPath', ('"' + $stopPath + '"'), '-LogPath', ('"' + $logPath + '"')
    )
    $server = Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList $arguments `
        -WindowStyle Hidden -PassThru
    $readyDeadline = [DateTime]::UtcNow.AddSeconds(5)
    while (-not (Test-Path -LiteralPath $readyPath) -and -not $server.HasExited -and
        [DateTime]::UtcNow -lt $readyDeadline) {
        Start-Sleep -Milliseconds 50
    }
    if (-not (Test-Path -LiteralPath $readyPath) -or $server.HasExited) {
        throw 'The activation proof server did not start.'
    }

    Copy-Item -LiteralPath $fixtureTrustPath -Destination $trustPath -Force
    $trustInstalled = $true
    Restart-Service -Name TownLightStation -Force
    Connect-AfterServiceBoundary
    $available = Wait-Until -Action { Invoke-InstalledProofApi -Uri "$BaseUrl/updates/status" } `
        -Predicate { param($value) $value.latest_check.outcome -eq 'update_available' }
    if ($available.latest_check.offered_release.version -ne '0.2.0' -or
        $available.latest_check.offered_release.source_commit -ne $ExpectedSourceCommit) {
        throw 'Signed offer identity did not match the real candidate.'
    }
    Invoke-InstalledProofApi -Uri "$BaseUrl/updates/stage" -Method Post | Out-Null
    $prepared = Invoke-InstalledProofApi -Uri "$BaseUrl/updates/prepare" -Method Post
    $slotPath = Join-Path (Join-Path $applicationDirectory 'slots') $prepared.slot_directory_name
    $candidateStationd = Join-Path $slotPath 'stationd.exe'
    $expectedCandidateHash = (Get-FileHash -LiteralPath $candidateStationd -Algorithm SHA256).Hash
    if ($expectedCandidateHash -ne (Get-FileHash -LiteralPath (Join-Path $applicationDirectory 'stationd.exe') -Algorithm SHA256).Hash) {
        throw 'The candidate payload is not the exact real station binary under proof.'
    }

    $greenForGate = Wait-Until -Action { Invoke-InstalledProofApi -Uri "$BaseUrl/runtime-safe-to-air" } `
        -Predicate { param($value) $value.color -eq 'green' }
    $applyBody = @{ confirmation = 'INSTALL UPDATE' } | ConvertTo-Json
    $request = Invoke-InstalledProofApi -Uri "$BaseUrl/updates/apply" -Method Post `
        -ContentType 'application/json' -Body $applyBody
    if ($request.prepared_slot.slot_directory_name -ne $prepared.slot_directory_name) {
        throw 'Maintenance request did not preserve the prepared candidate identity.'
    }

    $active = Wait-Until -Action {
        Get-Content -LiteralPath (Join-Path $dataDirectory 'active-slot.json') -Raw | ConvertFrom-Json
    } -Predicate { param($value) $value.active.package_manifest.version -eq '0.2.0' } -TimeoutSeconds 120
    $stationService = Wait-Until -Action {
        Get-CimInstance Win32_Service -Filter "Name='TownLightStation'"
    } -Predicate { param($value) $value.State -eq 'Running' -and $value.ProcessId -gt 0 } -TimeoutSeconds 30
    $stationProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($stationService.ProcessId)"
    if (-not $stationProcess -or
        [IO.Path]::GetFullPath($stationProcess.ExecutablePath) -ne [IO.Path]::GetFullPath($candidateStationd)) {
        throw 'The running station process image is not the authenticated candidate slot.'
    }
    if ($stationService.PathName -notlike ('"*\slots\' + $prepared.slot_directory_name + '\stationd.exe" service *')) {
        throw "The SCM command does not point to the candidate slot: $($stationService.PathName)"
    }
    Connect-AfterServiceBoundary
    $greenAfter = Wait-Until -Action { Invoke-InstalledProofApi -Uri "$BaseUrl/runtime-safe-to-air" } `
        -Predicate { param($value) $value.color -eq 'green' }
    $transactionPath = Join-Path (Join-Path $dataDirectory 'updates\maintenance-transactions') ($request.transaction_id + '.json')
    $transaction = Get-Content -LiteralPath $transactionPath -Raw | ConvertFrom-Json
    if ($transaction.phase -ne 'committed' -or
        $transaction.previous_receipt.active.package_manifest.version -ne '0.1.0') {
        throw 'The durable maintenance transaction did not commit with its rollback identity.'
    }
    $completedRequest = Join-Path (Join-Path $dataDirectory 'updates\maintenance-requests-completed') ($request.transaction_id + '.json')
    if (-not (Test-Path -LiteralPath $completedRequest -PathType Leaf) -or
        (Test-Path -LiteralPath (Join-Path $dataDirectory 'updates\maintenance-requests\pending.json'))) {
        throw 'The watchdog did not archive the completed request or clear its atomic inbox.'
    }
    $receipt = Get-Content -LiteralPath (Join-Path $dataDirectory 'install-receipt.json') -Raw | ConvertFrom-Json
    if ($receipt.version -ne '0.2.0' -or $receipt.source_commit -ne $ExpectedSourceCommit -or
        $receipt.media_runtime_sha256 -ne $installed.media_runtime_sha256) {
        throw 'Committed install identity is incomplete or did not preserve the runtime identity.'
    }
    $updaterAfter = Get-CimInstance Win32_Service -Filter "Name='TownLightStationUpdater'"
    if ($updaterAfter.State -ne 'Running' -or $updaterAfter.ProcessId -ne $updaterBefore.ProcessId) {
        throw 'The independent watchdog did not survive the station service switch.'
    }

    Remove-Item -LiteralPath $trustPath -Force
    $trustInstalled = $false
    Restart-Service -Name TownLightStation -Force
    Connect-AfterServiceBoundary
    $rootResolution = Wait-Until -Action { Invoke-InstalledProofApi -Uri "$BaseUrl/updates/status" } `
        -Predicate { param($value) -not $value.configured -and $value.current_version -eq '0.2.0' }
    $stationAfterRestart = Get-CimInstance Win32_Service -Filter "Name='TownLightStation'"
    $processAfterRestart = Get-CimInstance Win32_Process -Filter "ProcessId=$($stationAfterRestart.ProcessId)"
    if ([IO.Path]::GetFullPath($processAfterRestart.ExecutablePath) -ne [IO.Path]::GetFullPath($candidateStationd)) {
        throw 'The committed slot did not survive an ordinary station restart.'
    }

    $requests = @(Get-Content -LiteralPath $logPath)
    if ($requests.Count -ne 2) {
        throw "Activation proof expected exactly two release-server requests, observed $($requests.Count)."
    }
    $result = [ordered]@{
        source_commit = $ExpectedSourceCommit
        installer_version_before = $installed.version
        committed_version = $receipt.version
        transaction_id = $request.transaction_id
        transaction_phase = $transaction.phase
        previous_version = $transaction.previous_receipt.active.package_manifest.version
        active_slot = $active.active.relative_directory
        station_service_account = $stationService.StartName
        updater_service_account = $updaterAfter.StartName
        watchdog_process_survived = $updaterAfter.ProcessId -eq $updaterBefore.ProcessId
        running_candidate_image = $stationProcess.ExecutablePath
        running_candidate_sha256 = $expectedCandidateHash
        safe_to_air_before = $greenForGate.color
        safe_to_air_after = $greenAfter.color
        completed_request_archived = Test-Path -LiteralPath $completedRequest
        runtime_identity_preserved = $receipt.media_runtime_sha256 -eq $installed.media_runtime_sha256
        committed_slot_survived_restart = $processAfterRestart.ExecutablePath -eq $candidateStationd
        application_root_resolution_proved = -not $rootResolution.configured
        release_server_requests = $requests
    } | ConvertTo-Json -Depth 6
    if ($ResultPath) {
        Set-Content -LiteralPath $ResultPath -Value $result -Encoding utf8NoBOM
    }
    $result
}
catch {
    if ($ResultPath) {
        [ordered]@{
            error = $_.Exception.Message
            position = $_.InvocationInfo.PositionMessage
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
        Set-Content -LiteralPath $stopPath -Value 'stop' -Encoding ascii -ErrorAction SilentlyContinue
        if (-not $server.WaitForExit(3000)) { $server.Kill() }
        $server.Dispose()
    }
}
