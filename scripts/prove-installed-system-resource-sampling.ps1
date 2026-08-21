param(
    [string]$BaseUrl = 'http://127.0.0.1:4070/api/v1',
    [string]$Manifest = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\candidate-manifest.json'),
    [int]$CadenceDeadlineSeconds = 75
)

$ErrorActionPreference = 'Stop'
$dataDirectory = Join-Path $env:ProgramData 'TownLight Station'
$backupDirectory = Join-Path $dataDirectory 'backups'
$receiptPath = Join-Path $dataDirectory 'install-receipt.json'

$manifestDocument = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
$receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
if ($receipt.source_commit -ne $manifestDocument.source_commit) {
    throw 'The installed receipt does not identify the candidate under proof.'
}

$service = Get-CimInstance Win32_Service -Filter "Name='TownLightStation'"
if (-not $service -or $service.State -ne 'Running' -or $service.StartName -ne 'LocalSystem') {
    throw 'The installed TownLight Station service is not running as LocalSystem.'
}

function Assert-HealthySample {
    param($Sample)

    if ($null -eq $Sample -or [uint64]$Sample.sample_id -eq 0) {
        throw 'The installed service did not expose a persisted resource sample.'
    }
    if ($null -eq $Sample.cpu_percent -or [double]$Sample.cpu_percent -lt 0 -or [double]$Sample.cpu_percent -gt 100) {
        throw 'The CPU measurement is missing or outside 0..100 percent.'
    }
    if ($null -eq $Sample.ram_used_gb -or $null -eq $Sample.ram_total_gb -or
        [double]$Sample.ram_used_gb -lt 0 -or [double]$Sample.ram_total_gb -le 0 -or
        [double]$Sample.ram_used_gb -gt [double]$Sample.ram_total_gb) {
        throw 'The physical-memory measurement is invalid.'
    }
    if ($null -eq $Sample.media_volume_free_gb -or [double]$Sample.media_volume_free_gb -le 0 -or
        $null -eq $Sample.backup_volume_free_gb -or [double]$Sample.backup_volume_free_gb -le 0) {
        throw 'The installed service did not report positive media and backup free space.'
    }
    if (-not $Sample.database_reachable -or -not $Sample.backup_volume_writable -or -not $Sample.service_running) {
        throw 'A required station resource-health assertion is false.'
    }
    $ageMilliseconds = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() - [int64]$Sample.sampled_at_unix_ms
    if ($ageMilliseconds -lt -5000 -or $ageMilliseconds -gt 90000) {
        throw "The installed resource sample is stale or future-dated: $ageMilliseconds ms."
    }
}

$first = Invoke-RestMethod -Uri "$BaseUrl/system-resources/latest"
Assert-HealthySample $first

$deadline = [DateTime]::UtcNow.AddSeconds($CadenceDeadlineSeconds)
$second = $null
while ($null -eq $second -and [DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Seconds 2
    $candidate = Invoke-RestMethod -Uri "$BaseUrl/system-resources/latest"
    if ([uint64]$candidate.sample_id -gt [uint64]$first.sample_id) {
        $second = $candidate
    }
}
if ($null -eq $second) {
    throw "No newer installed resource sample arrived within $CadenceDeadlineSeconds seconds."
}
Assert-HealthySample $second

$intervalMilliseconds = [int64]$second.sampled_at_unix_ms - [int64]$first.sampled_at_unix_ms
if ($intervalMilliseconds -lt 55000 -or $intervalMilliseconds -gt 70000) {
    throw "The installed sample cadence was outside its bounded window: $intervalMilliseconds ms."
}

$probeLeaks = @(Get-ChildItem -LiteralPath $backupDirectory -Filter '.write-probe-*' -Force -ErrorAction SilentlyContinue)
if ($probeLeaks.Count -ne 0) {
    throw 'The installed backup write probe left temporary files behind.'
}

[ordered]@{
    source_commit = $receipt.source_commit
    service_account = $service.StartName
    first_sample_id = [uint64]$first.sample_id
    second_sample_id = [uint64]$second.sample_id
    cadence_milliseconds = $intervalMilliseconds
    cpu_percent = [double]$second.cpu_percent
    ram_used_gb = [double]$second.ram_used_gb
    ram_total_gb = [double]$second.ram_total_gb
    media_volume_free_gb = [double]$second.media_volume_free_gb
    backup_volume_free_gb = [double]$second.backup_volume_free_gb
    database_reachable = [bool]$second.database_reachable
    backup_volume_writable = [bool]$second.backup_volume_writable
    service_running = [bool]$second.service_running
    probe_files_remaining = $probeLeaks.Count
} | ConvertTo-Json
