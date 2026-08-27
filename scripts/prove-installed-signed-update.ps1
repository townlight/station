param(
    [Parameter(Mandatory = $true)]
    [Management.Automation.PSCredential]$Credential,
    [Parameter(Mandatory = $true)]
    [string]$FixtureDirectory,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedSourceCommit,
    [string]$BaseUrl = 'http://127.0.0.1:4070/api/v1',
    [int]$FeedPort = 47886,
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
$receiptPath = Join-Path $dataDirectory 'install-receipt.json'
$trustPath = Join-Path $applicationDirectory 'release-trust.json'
$feedPath = Join-Path $FixtureDirectory 'stable.json'
$fixturePackagePath = Join-Path $FixtureDirectory 'TownLight-Station-0.2.0.tlu'
$fixtureTrustPath = Join-Path $FixtureDirectory 'release-trust.json'
$serverReadyPath = Join-Path $FixtureDirectory 'server-ready.txt'
$serverStopPath = Join-Path $FixtureDirectory 'server-stop.txt'
$serverLogPath = Join-Path $FixtureDirectory 'server-log.txt'
$server = $null
$trustInstalled = $false
$stagedProofPath = ''
$preparedProofPath = ''

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

function Wait-ForUpdateStatus {
    param([scriptblock]$Predicate, [int]$TimeoutSeconds = 30)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $value = Invoke-InstalledProofApi -Uri "$BaseUrl/updates/status" -Method Get
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
    -not (Test-Path -LiteralPath $fixturePackagePath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $fixtureTrustPath -PathType Leaf)) {
    throw 'The signed update fixture is incomplete.'
}

try {
    Remove-Item -LiteralPath $serverReadyPath,$serverStopPath,$serverLogPath -Force -ErrorAction SilentlyContinue
    $serverScript = Join-Path $PSScriptRoot 'serve-update-proof-fixture.ps1'
    $serverArguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $serverScript + '"'),
        '-Port', [string]$FeedPort, '-FeedPath', ('"' + $feedPath + '"'),
        '-PackagePath', ('"' + $fixturePackagePath + '"'),
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
    Connect-AfterServiceBoundary
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
    $notes = Invoke-TownLightProofApi -Session $apiSession -Method Get `
        -Path '/updates/release-notes?version=0.2.0' -ResponseMode Text
    if ($notes -ne "# TownLight Station 0.2.0`n`nInstalled signed-discovery proof release.") {
        throw 'The installed API did not return the exact verified release notes.'
    }
    $alerts = Invoke-InstalledProofApi -Uri "$BaseUrl/alerts"
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

    $staged = Invoke-InstalledProofApi -Uri "$BaseUrl/updates/stage" -Method Post
    $stagedProofPath = Join-Path (Join-Path $dataDirectory 'updates\staged') $staged.package_file_name
    if ($staged.manifest.version -ne '0.2.0' -or
        $staged.manifest.source_commit -ne ('b' * 40) -or
        -not (Test-Path -LiteralPath $stagedProofPath -PathType Leaf)) {
        throw 'The installed staging action did not publish the exact verified offer.'
    }
    $stagedFile = Get-Item -LiteralPath $stagedProofPath
    $expectedPackageHash = (Get-FileHash -LiteralPath $fixturePackagePath -Algorithm SHA256).Hash
    if ([uint64]($stagedFile.Length) -ne [uint64]($staged.manifest.package_size_bytes) -or
        (Get-FileHash -LiteralPath $stagedProofPath -Algorithm SHA256).Hash -ne $expectedPackageHash -or
        $staged.manifest.package_sha256 -ne $expectedPackageHash -or
        -not $stagedFile.IsReadOnly) {
        throw 'The installed staged package bytes, digest, size, or read-only state are invalid.'
    }
    $stagedStatus = Wait-ForUpdateStatus -Predicate {
        param($Status)
        $Status.staged_package.manifest.version -eq '0.2.0'
    }
    if ($stagedStatus.staged_package.package_file_name -ne $staged.package_file_name) {
        throw 'The installed status did not expose the exact durable staged record.'
    }
    $quarantineLeaks = @(
        Get-ChildItem -LiteralPath (Join-Path $dataDirectory 'updates\quarantine') `
            -Filter '*.part' -File -ErrorAction SilentlyContinue
    )
    if ($quarantineLeaks.Count -ne 0) {
        throw 'Verified staging left quarantine fragments behind.'
    }
    $prepared = Invoke-InstalledProofApi -Uri "$BaseUrl/updates/prepare" -Method Post
    $preparedProofPath = Join-Path (Join-Path $applicationDirectory 'slots') $prepared.slot_directory_name
    if ($prepared.manifest.version -ne '0.2.0' -or
        $prepared.manifest.source_commit -ne ('b' * 40) -or
        -not (Test-Path -LiteralPath $preparedProofPath -PathType Container)) {
        throw 'The installed prepare action did not publish the exact immutable slot.'
    }
    $slotManifestPath = Join-Path $preparedProofPath 'slot-manifest.json'
    $slotManifest = Get-Content -LiteralPath $slotManifestPath -Raw | ConvertFrom-Json
    $slotFiles = @(Get-ChildItem -LiteralPath $preparedProofPath -File)
    if ($slotFiles.Count -notin @(3, 4) -or $slotManifest.files.Count -notin @(2, 3) -or
        -not (Get-Item -LiteralPath $slotManifestPath).IsReadOnly) {
        throw 'The installed prepared slot does not have the exact read-only file set.'
    }
    foreach ($specification in $slotManifest.files) {
        $payloadPath = Join-Path $preparedProofPath $specification.path
        $payload = Get-Item -LiteralPath $payloadPath
        if (-not $payload.IsReadOnly -or
            [uint64]($payload.Length) -ne [uint64]($specification.size_bytes) -or
            (Get-FileHash -LiteralPath $payloadPath -Algorithm SHA256).Hash -ne $specification.sha256) {
            throw "Prepared slot payload failed exact verification: $($specification.path)"
        }
    }
    $preparedStatus = Wait-ForUpdateStatus -Predicate {
        param($Status)
        $Status.prepared_slot.slot_directory_name -eq $prepared.slot_directory_name
    }
    $slotQuarantineLeaks = @(
        Get-ChildItem -LiteralPath (Join-Path $applicationDirectory 'slots') `
            -Filter '.incoming-*' -Directory -ErrorAction SilentlyContinue
    )
    if ($slotQuarantineLeaks.Count -ne 0) {
        throw 'Immutable slot preparation left a quarantine directory behind.'
    }
    $serverRequests = @(Get-Content -LiteralPath $serverLogPath)
    if ($serverRequests.Count -ne 2 -or
        $serverRequests[0] -ne 'GET /stable.json HTTP/1.1' -or
        $serverRequests[1] -ne 'GET /TownLight-Station-0.2.0.tlu HTTP/1.1') {
        throw 'The installed proof server did not observe exactly one feed and one package request.'
    }

    Remove-Item -LiteralPath $trustPath -Force
    $trustInstalled = $false
    Restart-Service -Name TownLightStation -Force
    Connect-AfterServiceBoundary
    $clean = Wait-ForUpdateStatus -Predicate {
        param($Status)
        -not $Status.configured -and $null -eq $Status.latest_check -and
            $null -eq $Status.staged_package -and $null -eq $Status.prepared_slot
    }
    $resolvedAlert = $null
    $alertDeadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not $resolvedAlert -and [DateTime]::UtcNow -lt $alertDeadline) {
        $cleanAlerts = Invoke-InstalledProofApi -Uri "$BaseUrl/alerts"
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
        discovery_package_files = $stagedFiles.Count
        staged_package_sha256 = $expectedPackageHash
        staged_package_size = [uint64]($stagedFile.Length)
        staged_package_read_only = $stagedFile.IsReadOnly
        quarantine_fragments = $quarantineLeaks.Count
        server_requests = $serverRequests
        stage_record_cleared_with_trust = $null -eq $clean.staged_package
        prepared_slot_name = $prepared.slot_directory_name
        prepared_slot_files = $slotFiles.Count
        prepared_payloads_verified = $slotManifest.files.Count
        prepared_slot_read_only = @($slotFiles.Where({ -not $_.IsReadOnly })).Count -eq 0
        slot_quarantine_directories = $slotQuarantineLeaks.Count
        prepare_record_cleared_with_trust = $null -eq $clean.prepared_slot
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
            Invoke-InstalledProofApi -Uri "$BaseUrl/updates/status" -Method Get
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
    if ($stagedProofPath -and (Test-Path -LiteralPath $stagedProofPath -PathType Leaf)) {
        try { (Get-Item -LiteralPath $stagedProofPath).IsReadOnly = $false } catch {}
        Remove-Item -LiteralPath $stagedProofPath -Force -ErrorAction SilentlyContinue
    }
    if ($preparedProofPath -and (Test-Path -LiteralPath $preparedProofPath -PathType Container)) {
        $slotsRoot = [IO.Path]::GetFullPath((Join-Path $applicationDirectory 'slots'))
        $resolvedSlot = [IO.Path]::GetFullPath($preparedProofPath)
        if ((Split-Path -Parent $resolvedSlot) -eq $slotsRoot -and
            (Split-Path -Leaf $resolvedSlot) -eq 'slot-0.2.0-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb') {
            Get-ChildItem -LiteralPath $resolvedSlot -File | ForEach-Object { $_.IsReadOnly = $false }
            Remove-Item -LiteralPath $resolvedSlot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    if ($server) {
        Set-Content -LiteralPath $serverStopPath -Value 'stop' -Encoding ascii -ErrorAction SilentlyContinue
        if (-not $server.WaitForExit(3000)) { $server.Kill() }
        $server.Dispose()
    }
}
