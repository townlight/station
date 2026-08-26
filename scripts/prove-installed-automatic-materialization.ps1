<##
.SYNOPSIS
    Runs the installed-machine gate for automatic recurring Draft evidence.

.DESCRIPTION
    This gate authenticates to the installed loopback API, verifies the exact
    installed candidate, and reads the supported scheduling, schedule, alert,
    and health surfaces. The current public API has no read/history boundary
    for automatic materialization runs. Its alert surface exposes durable
    typed failure episodes, but not their automatic run identity, so this
    script deliberately returns `blocked` after the observable preflight. It
    never calls the operator materialization POST: that would prove only a
    manual run, not automatic execution.

    No service restart is attempted. Restart durability is therefore reported
    as unobservable until a managed installed-service restart boundary and a
    materialization-run history endpoint exist.
##>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [Management.Automation.PSCredential]$Credential,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')]
    [string]$ChannelId,
    [string]$ExpectedSourceCommit = '',
    [string]$CandidateManifestPath = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\candidate-manifest.json'),
    [string]$BaseUri = 'http://127.0.0.1:4070',
    [ValidateRange(5, 300)]
    [int]$TimeoutSeconds = 30,
    [string]$ResultPath = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'installed-proof-api.ps1')

$repository = Split-Path $PSScriptRoot -Parent
$dataRoot = Join-Path $env:ProgramData 'TownLight Station'
$applicationRoot = Join-Path $env:ProgramFiles 'TownLight Station'
$result = [ordered]@{
    schema_version = 1
    proof = 'installed-automatic-materialization'
    status = 'failed'
    run_id = [guid]::NewGuid().ToString('D')
    started_at_utc = [DateTime]::UtcNow.ToString('o')
    completed_at_utc = $null
    source_commit = $null
    channel_id = $ChannelId.ToLowerInvariant()
    candidate = $null
    observations = [ordered]@{}
    missing_interface_requirements = @()
    error_code = $null
}

function Write-ProofResult {
    $result.completed_at_utc = [DateTime]::UtcNow.ToString('o')
    $json = ($result | ConvertTo-Json -Depth 16) + [Environment]::NewLine
    if ($ResultPath) {
        $parent = Split-Path -Parent ([IO.Path]::GetFullPath($ResultPath))
        if ($parent -and -not (Test-Path -LiteralPath $parent -PathType Container)) {
            throw 'ResultPath parent directory does not exist.'
        }
        [IO.File]::WriteAllText($ResultPath, $json, [Text.UTF8Encoding]::new($false))
    }
    Write-Output $json
}

function Fail-Proof([string]$Code) {
    $result.status = 'failed'
    $result.error_code = $Code
    Write-ProofResult
    exit 1
}

function Get-RequiredJson([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'required receipt is missing'
    }
    Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -ErrorAction Stop
}

function Get-FileSha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'required installed file is missing'
    }
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Invoke-ProofApi {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('Get')][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path
    )
    Invoke-TownLightProofApi -Session $script:apiSession -Method $Method -Path $Path -TimeoutSec $TimeoutSeconds
}

try {
    $head = (& git -C $repository rev-parse HEAD).Trim()
    $dirty = @(& git -C $repository status --porcelain)
    if ($LASTEXITCODE -ne 0 -or $head -notmatch '^[0-9a-f]{40}$') {
        Fail-Proof 'source_commit_unavailable'
    }
    if ($dirty.Count -ne 0) {
        Fail-Proof 'dirty_candidate'
    }

    $manifestPath = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $CandidateManifestPath -ErrorAction Stop).Path)
    $manifest = Get-RequiredJson $manifestPath
    if ($manifest.source_commit -ne $head) {
        Fail-Proof 'candidate_manifest_source_mismatch'
    }
    if ($ExpectedSourceCommit -and $ExpectedSourceCommit -cne $head) {
        Fail-Proof 'expected_source_commit_mismatch'
    }

    $receipt = Get-RequiredJson (Join-Path $dataRoot 'install-receipt.json')
    $active = Get-RequiredJson (Join-Path $dataRoot 'active-slot.json')
    $runtimeManifest = Get-RequiredJson (Join-Path $dataRoot 'runtime-manifest.json')
    if ($receipt.source_commit -ne $head -or
        $receipt.source_commit -ne $manifest.source_commit -or
        $receipt.media_runtime_sha256 -ne $manifest.media_runtime_sha256 -or
        $runtimeManifest.source_commit -ne $head -or
        $runtimeManifest.runtime_package_sha256 -ne $manifest.media_runtime_sha256) {
        Fail-Proof 'installed_receipt_candidate_mismatch'
    }

    $installedHashes = [ordered]@{
        stationd_sha256 = Get-FileSha256 (Join-Path $applicationRoot 'stationd.exe')
        channel_worker_sha256 = Get-FileSha256 (Join-Path $applicationRoot 'channel-worker.exe')
        station_updater_sha256 = Get-FileSha256 (Join-Path $applicationRoot 'station-updater.exe')
    }
    if ($installedHashes.stationd_sha256 -ne ([string]$manifest.stationd_sha256).ToLowerInvariant() -or
        $installedHashes.channel_worker_sha256 -ne ([string]$manifest.channel_worker_sha256).ToLowerInvariant() -or
        $installedHashes.station_updater_sha256 -ne ([string]$manifest.station_updater_sha256).ToLowerInvariant()) {
        Fail-Proof 'installed_binary_manifest_mismatch'
    }
    if ($active.active.package_manifest.source_commit -ne $head -or
        $active.active.package_manifest.files.Count -ne 2) {
        Fail-Proof 'active_slot_candidate_mismatch'
    }

    $stationService = Get-CimInstance Win32_Service -Filter "Name='TownLightStation'"
    $updaterService = Get-CimInstance Win32_Service -Filter "Name='TownLightStationUpdater'"
    if (-not $stationService -or $stationService.State -ne 'Running' -or $stationService.StartName -ne 'LocalSystem' -or
        -not $updaterService -or $updaterService.State -ne 'Running' -or $updaterService.StartName -ne 'LocalSystem') {
        Fail-Proof 'installed_services_not_ready'
    }

    $health = Invoke-RestMethod -Method Get -Uri ($BaseUri.TrimEnd('/') + '/health') -TimeoutSec $TimeoutSeconds
    if ($health.status -ne 'ready' -or $health.database -ne 'ready') {
        Fail-Proof 'installed_health_not_ready'
    }

    $script:apiSession = New-TownLightProofSession -BaseUri ([uri]$BaseUri) -Credential $Credential -TimeoutSec $TimeoutSeconds
    $channels = @(Invoke-ProofApi -Method Get -Path '/channels')
    $channel = @($channels | Where-Object { [string]$_.channel_id -ceq $ChannelId })
    if ($channel.Count -ne 1) {
        Fail-Proof 'proof_channel_not_found'
    }
    $rules = @(Invoke-ProofApi -Method Get -Path "/scheduling/recurring-rules?channel_id=$ChannelId&limit=128")
    $schedule = @(Invoke-ProofApi -Method Get -Path "/schedule/items?channel_id=$ChannelId")
    $alerts = @(Invoke-ProofApi -Method Get -Path '/alerts')
    $enabledRules = @($rules | Where-Object { $_.enabled -eq $true })

    $result.source_commit = $head
    $result.candidate = [ordered]@{
        manifest_source_commit = $manifest.source_commit
        installed_source_commit = $receipt.source_commit
        stationd_sha256 = $installedHashes.stationd_sha256
        channel_worker_sha256 = $installedHashes.channel_worker_sha256
        station_updater_sha256 = $installedHashes.station_updater_sha256
    }
    $failureAlerts = @($alerts | Where-Object {
            [string]$_.condition -eq 'schedule-materialization-failure' -and
            [string]$_.resource_ref -ceq $ChannelId
        })
    $noMatchAlerts = @($alerts | Where-Object {
            [string]$_.condition -eq 'schedule-no-match' -and
            [string]$_.resource_ref -ceq $ChannelId
        })
    $result.observations = [ordered]@{
        authenticated_account_id = [string]$script:apiSession.Identity.account_id
        channel_found = $true
        enabled_recurring_rule_count = $enabledRules.Count
        schedule_item_count = $schedule.Count
        durable_draft_surface = 'schedule_items_readable_but_automatic_origin_not_exposed'
        alert_count = $alerts.Count
        failure_alert_count = $failureAlerts.Count
        failure_alert_evidence = if ($failureAlerts.Count) {
            'durable_channel_scoped_typed_schedule_materialization_failure_alert_observed'
        } else {
            'no_current_channel_scoped_schedule_materialization_failure_alert'
        }
        no_match_alert_count = $noMatchAlerts.Count
        no_match_alert_evidence = if ($noMatchAlerts.Count) {
            'durable_channel_scoped_schedule_no_match_alert_observed'
        } else {
            'no_current_channel_scoped_schedule_no_match_alert'
        }
        health = $health
        idempotence = 'not_observable_without_automatic_materialization_history'
        restart_durability = 'not_observable_without_managed_service_restart_boundary'
        automatic_run_binding = 'success_failure_and_no_match_surfaces_not_bindable_without_automatic_run_identity'
    }
    $result.missing_interface_requirements = @(
        'GET /api/v1/scheduling/channels/{channel_id}/materializations/history?limit={1..128} returning automatic-vs-operator origin, run identity, checkpoint UTC day, disposition, outcomes, generated Draft identities, source commit, and the associated failure/retry state so current /api/v1/alerts episodes can be bound to a run.',
        'A bounded installed-service restart control or equivalent proof boundary so this harness can observe restart-stable once-per-UTC-day checkpointing without killing or guessing at a process.'
    )
    $result.status = 'blocked'
}
catch {
    if ($result.status -ne 'failed') { $result.status = 'failed' }
    if (-not $result.error_code) { $result.error_code = 'installed_automatic_materialization_preflight_failed' }
}

Write-ProofResult
if ($result.status -ne 'passed') {
    exit 1
}
