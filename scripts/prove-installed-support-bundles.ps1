param(
    [Parameter(Mandatory = $true)]
    [Management.Automation.PSCredential]$Credential,
    [string]$BaseUrl = 'http://127.0.0.1:4070/api/v1',
    [string]$Manifest = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\candidate-manifest.json')
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'candidate-pair-verifier.ps1')
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
$bundleDirectory = Join-Path $dataDirectory 'support-bundles'
$receiptPath = Join-Path $dataDirectory 'install-receipt.json'
$proofId = [Guid]::NewGuid().ToString('N')
$alertChannelId = [Guid]::NewGuid().ToString()
$credentialCanary = "NEVER-EXPORT-SUPPORT-CREDENTIAL-$proofId"
$fullTargetCanary = "https://support-canary.invalid/private/$proofId"
$downloadPath = Join-Path ([IO.Path]::GetTempPath()) "TownLight-support-proof-$proofId.zip"
$expectedEntries = @(
    'alert-channels-redacted.json',
    'alert-rules.json',
    'channels.json',
    'installation.json',
    'maintenance-latest.json',
    'manifest.json',
    'recent-alert-deliveries-redacted.json',
    'recent-alerts-redacted.json',
    'recent-system-resources.json',
    'runtime-safe-to-air.json',
    'self-test-schedules.json',
    'self-tests-redacted.json',
    'station-profile.json'
)
$expectedExcludes = @(
    'station database and database sidecars',
    'encrypted or plaintext credentials, passwords, tokens, and signing keys',
    'alert payloads, HMAC signatures, and raw delivery errors',
    'full media paths, raw environment variables, and unrestricted logs',
    'resident or subscriber data'
)

function Get-ByteSha256 {
    param([byte[]]$Bytes)

    $sha = [Security.Cryptography.SHA256]::Create()
    try { return -join ($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('X2') }) }
    finally { $sha.Dispose() }
}

function Read-ZipEntries {
    param([string]$Path)

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $result = @{}
    $archive = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        foreach ($entry in $archive.Entries) {
            if (-not $entry.Name -or $entry.FullName -ne $entry.Name) {
                throw "Support archive contains a directory or non-flat entry: $($entry.FullName)"
            }
            $input = $entry.Open()
            $memory = [IO.MemoryStream]::new()
            try {
                $input.CopyTo($memory)
                $result[$entry.FullName] = $memory.ToArray()
            }
            finally {
                $memory.Dispose()
                $input.Dispose()
            }
        }
    }
    finally { $archive.Dispose() }
    return $result
}

$receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
$candidate = (Get-TownLightCandidateIdentity -ManifestPath $Manifest -ExpectedSourceCommit $receipt.source_commit).manifest
if ($receipt.source_commit -ne $candidate.source_commit) {
    throw 'The installed receipt does not identify the candidate under proof.'
}
$service = Get-CimInstance Win32_Service -Filter "Name='TownLightStation'"
if (-not $service -or $service.State -ne 'Running' -or $service.StartName -ne 'LocalSystem') {
    throw 'The installed TownLight Station service is not running as LocalSystem.'
}

$configured = $false
try {
    $channelBody = @{
        channel_id = $alertChannelId
        kind = 'webhook'
        label = 'Installed support redaction proof'
        enabled = $false
        target = $fullTargetCanary
        credential = $credentialCanary
    } | ConvertTo-Json -Compress
    $redactedChannel = Invoke-InstalledProofApi -Method Put -Uri "$BaseUrl/alert-channels" `
        -ContentType 'application/json' -Body $channelBody
    $configured = $true
    if ($redactedChannel.target_redacted -eq $fullTargetCanary -or
        $redactedChannel.PSObject.Properties.Name -contains 'credential') {
        throw 'The alert-channel API exposed the redaction canary before bundle creation.'
    }

    $first = Invoke-InstalledProofApi -Method Post -Uri "$BaseUrl/support-bundles"
    $managedRoot = [IO.Path]::GetFullPath($bundleDirectory) + [IO.Path]::DirectorySeparatorChar
    $storedPath = [IO.Path]::GetFullPath([string]$first.path)
    if (-not $storedPath.StartsWith($managedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The installed service published a support bundle outside its managed directory.'
    }
    if (-not (Test-Path -LiteralPath $storedPath -PathType Leaf)) {
        throw 'The installed service reported a support bundle that does not exist.'
    }
    if ([uint64](Get-Item -LiteralPath $storedPath).Length -ne [uint64]$first.size_bytes -or
        (Get-FileHash -LiteralPath $storedPath -Algorithm SHA256).Hash -ne $first.sha256) {
        throw 'The stored support bundle size or SHA-256 disagrees with the API record.'
    }
    $actualRecordEntries = @($first.entries | Sort-Object)
    if (($actualRecordEntries -join '|') -ne (($expectedEntries | Sort-Object) -join '|')) {
        throw 'The support-bundle record did not report the exact bounded entry set.'
    }
    if ((@($first.excludes | Sort-Object) -join '|') -ne (($expectedExcludes | Sort-Object) -join '|')) {
        throw 'The support-bundle record did not report the exact exclusion contract.'
    }
    if ($first.retention_warning) { throw "Retention warning: $($first.retention_warning)" }

    $downloadUri = [uri]("http://127.0.0.1:4070" + $first.download_uri)
    $downloadApiPath = $downloadUri.PathAndQuery -replace '^/api/v1', ''
    $download = Invoke-TownLightProofApi -Session $apiSession -Method Get -Path $downloadApiPath `
        -ResponseMode Download -OutFile $downloadPath
    if ($download.StatusCode -ne 200 -or
        $download.Headers['Content-Type'] -notmatch '^application/zip' -or
        $download.Headers['Content-Disposition'] -notmatch [Regex]::Escape($first.file_name) -or
        $download.Headers['Cache-Control'] -ne 'no-store' -or
        $download.Headers['X-Content-Type-Options'] -ne 'nosniff') {
        throw 'The installed support-bundle download response lacks required safety headers.'
    }
    if ((Get-FileHash -LiteralPath $downloadPath -Algorithm SHA256).Hash -ne $first.sha256) {
        throw 'Downloaded support-bundle bytes do not match the service record.'
    }

    $zipBytes = [IO.File]::ReadAllBytes($downloadPath)
    $zipText = [Text.Encoding]::UTF8.GetString($zipBytes)
    if ($zipText.Contains($credentialCanary) -or $zipText.Contains($fullTargetCanary)) {
        throw 'A plaintext credential or full destination canary reached the ZIP bytes.'
    }
    $entries = Read-ZipEntries -Path $downloadPath
    $actualNames = @($entries.Keys | Sort-Object)
    if (($actualNames -join '|') -ne (($expectedEntries | Sort-Object) -join '|')) {
        throw 'The downloaded ZIP does not contain the exact bounded entry set.'
    }
    if ($actualNames.Where({ $_ -match '(?i)(\.db($|-)|-wal$|-shm$)' }).Count -ne 0) {
        throw 'A station database or sidecar reached the support archive.'
    }
    foreach ($name in $actualNames) {
        $text = [Text.Encoding]::UTF8.GetString([byte[]]$entries[$name])
        if ($text.Contains($credentialCanary) -or $text.Contains($fullTargetCanary)) {
            throw "A plaintext canary reached support entry $name."
        }
    }

    $bundleManifest = [Text.Encoding]::UTF8.GetString([byte[]]$entries['manifest.json']) | ConvertFrom-Json
    if ($bundleManifest.product -ne 'TownLight Station' -or
        $bundleManifest.schema_version -ne 1 -or
        $bundleManifest.bundle_id -ne $first.bundle_id -or
        $bundleManifest.source_commit -ne $candidate.source_commit) {
        throw 'The embedded support manifest does not identify the installed candidate.'
    }
    if ((@($bundleManifest.excludes | Sort-Object) -join '|') -ne (($expectedExcludes | Sort-Object) -join '|')) {
        throw 'The embedded manifest exclusion contract is incomplete.'
    }
    foreach ($entryRecord in $bundleManifest.entries) {
        if (-not $entries.ContainsKey([string]$entryRecord.name)) {
            throw "The manifest references a missing entry: $($entryRecord.name)"
        }
        $entryBytes = [byte[]]$entries[[string]$entryRecord.name]
        if ([uint64]$entryBytes.Length -ne [uint64]$entryRecord.size_bytes -or
            (Get-ByteSha256 -Bytes $entryBytes) -ne $entryRecord.sha256) {
            throw "Manifest size or hash mismatch for $($entryRecord.name)."
        }
    }
    if ($bundleManifest.entries.Count -ne ($expectedEntries.Count - 1)) {
        throw 'The embedded manifest did not account for every non-manifest entry.'
    }

    $installation = [Text.Encoding]::UTF8.GetString([byte[]]$entries['installation.json']) | ConvertFrom-Json
    if ($installation.source_commit -ne $candidate.source_commit) {
        throw 'The support installation snapshot identifies the wrong source commit.'
    }
    $stationArtifact = @($installation.artifacts).Where({ $_.name -eq 'stationd.exe' }, 'First')
    $workerArtifact = @($installation.artifacts).Where({ $_.name -eq 'channel-worker.exe' }, 'First')
    $nasPublisherArtifact = @($installation.artifacts).Where({ $_.name -eq 'nas-publisher.exe' }, 'First')
    $updaterArtifact = @($installation.artifacts).Where({ $_.name -eq 'station-updater.exe' }, 'First')
    if (-not $stationArtifact.present -or $stationArtifact.sha256 -ne $candidate.stationd_sha256 -or
        -not $workerArtifact.present -or $workerArtifact.sha256 -ne $candidate.channel_worker_sha256 -or
        -not $nasPublisherArtifact.present -or $nasPublisherArtifact.sha256 -ne $candidate.nas_publisher_sha256 -or
        -not $updaterArtifact.present -or $updaterArtifact.sha256 -ne $candidate.station_updater_sha256) {
        throw 'The support installation snapshot does not match the installed candidate binaries.'
    }
    $maintenance = [Text.Encoding]::UTF8.GetString([byte[]]$entries['maintenance-latest.json']) | ConvertFrom-Json
    if ($maintenance -and
        (($maintenance.PSObject.Properties.Name -contains 'detail') -or
        -not $maintenance.summary -or -not $maintenance.phase)) {
        throw 'The maintenance diagnostic is missing its bounded summary or exposes raw detail.'
    }
    $runtime = [Text.Encoding]::UTF8.GetString([byte[]]$entries['runtime-safe-to-air.json']) | ConvertFrom-Json
    $resources = @([Text.Encoding]::UTF8.GetString([byte[]]$entries['recent-system-resources.json']) | ConvertFrom-Json)
    $selfTests = @([Text.Encoding]::UTF8.GetString([byte[]]$entries['self-tests-redacted.json']) | ConvertFrom-Json)
    if (-not $runtime.status -or $resources.Count -lt 1 -or $selfTests.Count -lt 1) {
        throw 'The support archive omitted installed runtime, resource, or self-test evidence.'
    }

    $created = [Collections.Generic.List[object]]::new()
    $created.Add($first)
    for ($index = 0; $index -lt 10; $index++) {
        Start-Sleep -Milliseconds 10
        $record = Invoke-InstalledProofApi -Method Post -Uri "$BaseUrl/support-bundles"
        if ($record.retention_warning) { throw "Retention warning: $($record.retention_warning)" }
        $created.Add($record)
    }
    $retained = @(Get-ChildItem -LiteralPath $bundleDirectory -Filter 'support-bundle-*.zip' -File)
    $temporaryLeaks = @(Get-ChildItem -LiteralPath $bundleDirectory -Filter '*.tmp' -File -ErrorAction SilentlyContinue)
    if ($retained.Count -ne 10 -or $temporaryLeaks.Count -ne 0) {
        throw 'Installed support-bundle retention did not leave exactly ten ZIPs and zero temporary files.'
    }
    if (Test-Path -LiteralPath $first.path) {
        throw 'The oldest support bundle survived after the retention limit was exceeded.'
    }
    $latest = $created[$created.Count - 1]
    if (-not (Test-Path -LiteralPath $latest.path -PathType Leaf)) {
        throw 'The newest support bundle was removed by retention.'
    }

    [ordered]@{
        source_commit = $receipt.source_commit
        service_account = $service.StartName
        first_bundle_sha256 = $first.sha256
        downloaded_sha256_match = $true
        exact_entry_count = $actualNames.Count
        manifested_entry_hashes_verified = $bundleManifest.entries.Count
        credential_canary_absent = $true
        full_target_canary_absent = $true
        database_files_present = 0
        active_relative_directory = $installation.active_relative_directory
        active_generation = $installation.active_generation
        updater_root_hash_verified = $true
        maintenance_phase = if ($maintenance) { $maintenance.phase } else { $null }
        maintenance_raw_detail_absent = -not $maintenance -or
            -not ($maintenance.PSObject.Properties.Name -contains 'detail')
        runtime_status = $runtime.status.color
        resource_records = $resources.Count
        self_test_records = $selfTests.Count
        retained_zip_count = $retained.Count
        temporary_files_remaining = $temporaryLeaks.Count
        oldest_removed = $true
        newest_retained = $true
    } | ConvertTo-Json -Depth 5
}
finally {
    if ($configured) {
        try {
            Invoke-InstalledProofApi -Method Put -Uri "$BaseUrl/alert-channels" `
                -ContentType 'application/json' -Body $channelBody | Out-Null
        }
        catch {}
    }
    Remove-Item -LiteralPath $downloadPath -Force -ErrorAction SilentlyContinue
}
