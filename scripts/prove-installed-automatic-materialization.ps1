<##
.SYNOPSIS
    Proves one installed automatic recurring-Draft checkpoint across one controlled service boundary.

.DESCRIPTION
    This is an exact-candidate installed proof, not a test fixture creator. It
    reads the authenticated channel, recurring-rule, schedule, alert, and
    materialization-history surfaces before it considers the service boundary.
    A current-UTC-day automatic run made by the reserved system actor and at
    least one of that run's current Drafts are required. Missing evidence is a
    blocked fixture result and never controls the Windows Service Control Manager.

    The service boundary is opt-in. With -EnableServiceRestartProof and an
    elevated session, the script serializes on its named proof mutex and uses
    only %SystemRoot%\System32\sc.exe to stop then start exactly
    TownLightStation. It never starts, stops, reconfigures, or otherwise
    controls the updater service, and never uses a process-oriented restart.
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
    [ValidateRange(5, 300)]
    [int]$ServiceTransitionTimeoutSeconds = 120,
    [ValidateRange(61, 900)]
    [int]$PostRestartObservationSeconds = 65,
    [ValidateRange(1, 8)]
    [int]$MaxHistoryPages = 8,
    [switch]$EnableServiceRestartProof,
    [string]$ResultPath = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'candidate-pair-verifier.ps1')
. (Join-Path $PSScriptRoot 'installed-proof-api.ps1')
. (Join-Path $PSScriptRoot 'installed-automatic-materialization-proof-state.ps1')
. (Join-Path $PSScriptRoot 'installed-private-runtime-verifier.ps1')

$repository = Split-Path $PSScriptRoot -Parent
$trustedWindowsRoot = [Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)
$trustedSystemDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::System)
$trustedProgramFilesRoot = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
$trustedCommonApplicationDataRoot = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
$trustedScPath = Join-Path $trustedSystemDirectory 'sc.exe'
$dataRoot = Join-Path $trustedCommonApplicationDataRoot 'TownLight Station'
$applicationRoot = Join-Path $trustedProgramFilesRoot 'TownLight Station'
$stationServiceName = 'TownLightStation'
$updaterServiceName = 'TownLightStationUpdater'
$automaticActorId = '00000000-0000-4000-8000-000000000022'
$proofMutexName = 'Global\TownLightStationAutomaticMaterializationInstalledProof'
$mutex = $null
$mutexHeld = $false
$serviceControlEntered = $false
$stationStartIssued = $false
$stationStartCompleted = $false
$recoveryStartIssued = $false
$stopIssued = $false
$stopOutcome = 'not_run'
$script:apiCoordinates = $null

$result = [ordered]@{
    schema_version = 2
    proof = 'installed-automatic-materialization'
    status = 'failed'
    run_id = [guid]::NewGuid().ToString('D')
    started_at_utc = [DateTime]::UtcNow.ToString('o')
    completed_at_utc = $null
    source_commit = $null
    channel_id = $ChannelId.ToLowerInvariant()
    candidate = $null
    observations = [ordered]@{}
    restart_boundary = [ordered]@{
        requested = [bool]$EnableServiceRestartProof
        attempted = $false
        controlled_service = $stationServiceName
        updater_controlled = $false
        stop_issued = $false
        stop_outcome = 'not_run'
        start_issued = $false
        start_outcome = 'not_run'
        recovery_start_issued = $false
    }
    error_code = $null
}

function Write-ProofResult {
    $result.completed_at_utc = [DateTime]::UtcNow.ToString('o')
    $json = ($result | ConvertTo-Json -Depth 32) + [Environment]::NewLine
    if ($ResultPath) {
        $parent = Split-Path -Parent ([IO.Path]::GetFullPath($ResultPath))
        if ($parent -and -not (Test-Path -LiteralPath $parent -PathType Container)) {
            throw 'ResultPath parent directory does not exist.'
        }
        [IO.File]::WriteAllText($ResultPath, $json, [Text.UTF8Encoding]::new($false))
    }
    Write-Output $json
}

function Stop-Proof([string]$Code) { throw "proof:$Code" }
function Stop-ProofBlocked([string]$Code) { throw "proof-blocked:$Code" }

function Get-RequiredJson([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Stop-Proof 'required_receipt_missing' }
    try { Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -ErrorAction Stop }
    catch { Stop-Proof 'required_receipt_invalid' }
}

function Get-FileSha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Stop-Proof 'required_installed_file_missing' }
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-Sha256([string]$Value, [string]$Code) {
    if ([string]$Value -notmatch '^[0-9a-f]{64}$') { Stop-Proof $Code }
}

function Get-TextSha256([string]$Value) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value)) | ForEach-Object { $_.ToString('x2') })
    }
    finally { $sha.Dispose() }
}

function Test-CanonicalPathEqual([string]$Left, [string]$Right) {
    if ([string]::IsNullOrWhiteSpace($Left) -or [string]::IsNullOrWhiteSpace($Right)) { return $false }
    [IO.Path]::GetFullPath($Left).TrimEnd('\') -ieq [IO.Path]::GetFullPath($Right).TrimEnd('\')
}

function Assert-TrustedWindowsRoots {
    if (-not [Environment]::Is64BitProcess -or
        -not (Test-CanonicalPathEqual -Left $trustedSystemDirectory -Right (Join-Path $trustedWindowsRoot 'System32')) -or
        -not (Test-Path -LiteralPath $trustedScPath -PathType Leaf)) {
        Stop-Proof 'trusted_windows_root_invalid'
    }
    foreach ($check in @(
            [pscustomobject]@{ supplied = [string]$env:SystemRoot; trusted = $trustedWindowsRoot },
            [pscustomobject]@{ supplied = [string]$env:ProgramFiles; trusted = $trustedProgramFilesRoot },
            [pscustomobject]@{ supplied = [string]$env:ProgramData; trusted = $trustedCommonApplicationDataRoot }
        )) {
        if (-not (Test-CanonicalPathEqual -Left $check.supplied -Right $check.trusted)) {
            Stop-Proof 'known_folder_environment_drift'
        }
    }
}

function Get-CurrentUtcDay {
    Get-TownLightAutomaticMaterializationProofUtcDay -UnixTimeMilliseconds ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
}

function Invoke-ProofApi {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('Get')][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path
    )
    Invoke-TownLightProofApi -Session $script:apiSession -Method $Method -Path $Path -TimeoutSec $TimeoutSeconds
}

function Get-ExactService([string]$Name) {
    @(Get-CimInstance Win32_Service -Filter "Name='$Name'") | Select-Object -First 1
}

function Get-VerifiedServiceIdentity {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('TownLightStation', 'TownLightStationUpdater')]
        [string]$Name,
        [string]$ExpectedState = ''
    )
    $service = Get-ExactService -Name $Name
    $identity = Get-TownLightAutomaticMaterializationProofServiceIdentity -Service $service `
        -ApplicationRoot $applicationRoot -DataRoot $dataRoot -ServiceName $Name
    if (-not $identity.valid) { Stop-Proof ("{0}_{1}" -f $Name.ToLowerInvariant(), $identity.code) }
    if ($ExpectedState -and $identity.state -cne $ExpectedState) {
        Stop-Proof ("{0}_state_{1}" -f $Name.ToLowerInvariant(), $identity.state.ToLowerInvariant())
    }
    $identity
}

function Get-ReadyHealth {
    try {
        $health = Invoke-RestMethod -Method Get -Uri ($script:apiCoordinates.Origin + '/health') -TimeoutSec $TimeoutSeconds -ErrorAction Stop
    }
    catch { Stop-Proof 'installed_health_unavailable' }
    if ($health.status -ne 'ready' -or $health.database -ne 'ready' -or
        $null -eq $health.orchestration -or $health.orchestration.status -ne 'ready') {
        Stop-Proof 'installed_health_not_ready'
    }
    $health
}

function Assert-ExactInstalledCandidate {
    $head = (& git -C $repository rev-parse HEAD).Trim()
    $dirty = @(& git -C $repository status --porcelain)
    if ($LASTEXITCODE -ne 0 -or $head -notmatch '^[0-9a-f]{40}$') { Stop-Proof 'source_commit_unavailable' }
    if ($dirty.Count -ne 0) { Stop-Proof 'dirty_candidate' }

    $manifestPath = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $CandidateManifestPath -ErrorAction Stop).Path)
    try { $candidatePair = Get-TownLightCandidateIdentity -ManifestPath $manifestPath -ExpectedSourceCommit $head }
    catch { Stop-Proof 'candidate_pair_invalid' }
    $manifest = $candidatePair.manifest
    if ([string]$manifest.source_commit -cne $head -or ($ExpectedSourceCommit -and $ExpectedSourceCommit -cne $head)) {
        Stop-Proof 'candidate_manifest_source_mismatch'
    }
    foreach ($field in @('media_runtime_sha256', 'stationd_sha256', 'channel_worker_sha256', 'station_updater_sha256')) {
        Assert-Sha256 -Value ([string]$manifest.$field) -Code 'candidate_manifest_invalid'
    }

    $receipt = Get-RequiredJson (Join-Path $dataRoot 'install-receipt.json')
    $active = Get-RequiredJson (Join-Path $dataRoot 'active-slot.json')
    $runtimeManifestPath = Join-Path $dataRoot 'runtime-manifest.json'
    $runtimeManifest = Get-RequiredJson $runtimeManifestPath
    $runtimeManifestHash = Get-FileSha256 $runtimeManifestPath
    if ([string]$receipt.source_commit -cne $head -or
        [string]$receipt.media_runtime_sha256 -cne [string]$manifest.media_runtime_sha256 -or
        [string]$receipt.runtime_manifest_sha256 -cne $runtimeManifestHash -or
        [int]$runtimeManifest.schema_version -ne 1 -or [string]$runtimeManifest.product -cne 'TownLight Station' -or
        [string]$runtimeManifest.version -cne [string]$manifest.version -or
        [string]$runtimeManifest.source_commit -cne $head -or
        [string]$runtimeManifest.runtime_package_sha256 -cne [string]$manifest.media_runtime_sha256) {
        Stop-Proof 'installed_receipt_runtime_mismatch'
    }
    try {
        $runtimeVerification = Assert-TownLightInstalledPrivateRuntime `
            -RuntimeRoot (Join-Path $applicationRoot 'runtime\gstreamer') -RuntimeManifest $runtimeManifest
    }
    catch { Stop-Proof 'private_runtime_verification_failed' }
    $runtimeFileCount = [int]$runtimeVerification.file_count

    $installedHashes = [ordered]@{
        stationd_sha256 = Get-FileSha256 (Join-Path $applicationRoot 'stationd.exe')
        channel_worker_sha256 = Get-FileSha256 (Join-Path $applicationRoot 'channel-worker.exe')
        station_updater_sha256 = Get-FileSha256 (Join-Path $applicationRoot 'station-updater.exe')
    }
    foreach ($name in $installedHashes.Keys) {
        if ([string]$installedHashes[$name] -cne ([string]$manifest.$name).ToLowerInvariant()) {
            Stop-Proof 'installed_binary_manifest_mismatch'
        }
    }
    if ($null -eq $active.active -or [string]$active.active.relative_directory -cne '.' -or
        [string]$active.active.package_manifest.source_commit -cne $head -or
        @($active.active.package_manifest.files).Count -ne 2) {
        Stop-Proof 'active_slot_candidate_mismatch'
    }
    $activeHashes = @{}
    foreach ($file in @($active.active.package_manifest.files)) {
        if ([string]::IsNullOrWhiteSpace([string]$file.path) -or $activeHashes.ContainsKey([string]$file.path)) {
            Stop-Proof 'active_slot_candidate_mismatch'
        }
        $activeHashes[[string]$file.path] = ([string]$file.sha256).ToLowerInvariant()
    }
    if ($activeHashes['stationd.exe'] -cne $installedHashes.stationd_sha256 -or
        $activeHashes['channel-worker.exe'] -cne $installedHashes.channel_worker_sha256) {
        Stop-Proof 'active_slot_binary_mismatch'
    }
    [pscustomobject]@{
        source_commit = $head; manifest = $manifest; receipt = $receipt
        runtime_manifest_sha256 = $runtimeManifestHash; runtime_file_count = $runtimeFileCount
        installed_hashes = $installedHashes
    }
}

function Get-CurrentDayHistoryEntries([int64]$CurrentUtcDay) {
    $path = "/scheduling/channels/$ChannelId/materializations/history?limit=128"
    $entries = [Collections.Generic.List[object]]::new()
    $seenCursors = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    for ($page = 1; $page -le $MaxHistoryPages; $page++) {
        $document = Invoke-ProofApi -Method Get -Path $path
        if ($null -eq $document -or $null -eq $document.entries) { Stop-Proof 'materialization_history_invalid' }
        $pageEntries = @($document.entries)
        foreach ($entry in $pageEntries) { $entries.Add($entry) }
        if ($pageEntries.Count -eq 0 -or $null -eq $document.next_cursor) { break }

        $last = $pageEntries[$pageEntries.Count - 1]
        if ([int64]$last.checkpoint_utc_day -lt $CurrentUtcDay) { break }
        $cursorTime = [int64]$document.next_cursor.occurred_at_unix_ms
        $cursorRunId = [string]$document.next_cursor.run_id
        if ($cursorRunId -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' -or
            -not $seenCursors.Add("$cursorTime/$cursorRunId")) { Stop-Proof 'materialization_history_cursor_invalid' }
        if ($page -eq $MaxHistoryPages) { Stop-Proof 'materialization_history_capacity' }
        $path = "/scheduling/channels/$ChannelId/materializations/history?limit=128&before_occurred_at_unix_ms=$cursorTime&before_run_id=$cursorRunId"
    }
    @($entries | Where-Object { [int64]$_.checkpoint_utc_day -eq $CurrentUtcDay })
}

function Get-AuthenticatedEvidenceSnapshot {
    param([Parameter(Mandatory = $true)][int64]$CurrentUtcDay)
    $channels = @(Invoke-ProofApi -Method Get -Path '/channels')
    $matchingChannels = @($channels | Where-Object { [string]$_.channel_id -ceq $ChannelId })
    if ($matchingChannels.Count -ne 1) { Stop-Proof 'proof_channel_not_found' }
    if ($matchingChannels[0].enabled -ne $true) { Stop-Proof 'proof_channel_not_enabled' }
    $rules = @(Invoke-ProofApi -Method Get -Path "/scheduling/recurring-rules?channel_id=$ChannelId&limit=128")
    $enabledRules = @($rules | Where-Object { $_.enabled -eq $true })
    $schedule = @(Invoke-ProofApi -Method Get -Path "/schedule/items?channel_id=$ChannelId")
    $alerts = @(Invoke-ProofApi -Method Get -Path '/alerts')
    [pscustomobject]@{
        channel = $matchingChannels[0]; enabled_rules = $enabledRules; schedule = $schedule; alerts = $alerts
        current_day_history = @(Get-CurrentDayHistoryEntries -CurrentUtcDay $CurrentUtcDay); current_utc_day = $CurrentUtcDay
    }
}

function Get-ValidatedAutomaticRunEvidence {
    param([Parameter(Mandatory = $true)]$Snapshot, [switch]$AllowFixtureBlock)
    if ($Snapshot.enabled_rules.Count -eq 0) {
        if ($AllowFixtureBlock) { return [pscustomobject]@{ ready = $false; reason = 'no_enabled_recurring_rule' } }
        Stop-Proof 'enabled_recurring_rule_disappeared'
    }
    $automatic = @($Snapshot.current_day_history | Where-Object {
            [string]$_.origin -ceq 'automatic' -and [string]$_.channel_id -ceq $ChannelId
        })
    if ($automatic.Count -eq 0) {
        if ($AllowFixtureBlock) { return [pscustomobject]@{ ready = $false; reason = 'no_current_utc_day_automatic_run' } }
        Stop-Proof 'automatic_run_missing_after_restart'
    }
    if ($automatic.Count -ne 1) { Stop-Proof 'duplicate_current_utc_day_automatic_run' }
    $run = $automatic[0]
    if ([string]$run.actor_id -cne $automaticActorId -or [string]$run.run_id -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') {
        Stop-Proof 'automatic_run_identity_invalid'
    }
    Assert-Sha256 -Value ([string]$run.request_sha256) -Code 'automatic_run_request_hash_invalid'
    Assert-Sha256 -Value ([string]$run.result_sha256) -Code 'automatic_run_result_hash_invalid'
    if ([int64]$run.checkpoint_utc_day -ne [int64]$Snapshot.current_utc_day) { Stop-Proof 'automatic_run_checkpoint_day_invalid' }

    $scheduleIds = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($item in $Snapshot.schedule) {
        $itemId = [string]$item.item_id
        if ([string]::IsNullOrWhiteSpace($itemId) -or -not $scheduleIds.Add($itemId)) { Stop-Proof 'duplicate_current_schedule_identifier' }
    }
    $draftIds = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $occurrenceIds = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $draftBindings = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $currentScheduleHashes = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($draft in @($run.generated_drafts)) {
        $draftId = [string]$draft.schedule_item_id
        $occurrenceId = [string]$draft.occurrence_id
        if ([string]::IsNullOrWhiteSpace($draftId) -or -not $draftIds.Add($draftId)) { Stop-Proof 'duplicate_generated_draft_identifier' }
        $binding = ConvertTo-TownLightAutomaticMaterializationProofCanonicalJson -Value ([ordered]@{
                occurrence_id = $occurrenceId; schedule_item_id = $draftId
            })
        if ([string]::IsNullOrWhiteSpace($occurrenceId) -or -not $occurrenceIds.Add($occurrenceId) -or
            -not $draftBindings.Add($binding)) { Stop-Proof 'duplicate_generated_draft_occurrence_identifier' }
        $matchingSchedule = @($Snapshot.schedule | Where-Object {
                [string]$_.item_id -ceq $draftId -and [string]$_.channel_id -ceq $ChannelId -and [string]$_.state -ceq 'draft'
            })
        if ($matchingSchedule.Count -ne 1 -or $null -eq $draft.current_schedule -or
            [string]$draft.current_schedule.item_id -cne $draftId -or [string]$draft.current_schedule.channel_id -cne $ChannelId -or
            [string]$draft.current_schedule.state -cne 'draft') { Stop-Proof 'generated_draft_not_current_channel_draft' }
        $historySchedule = ConvertTo-TownLightAutomaticMaterializationProofCanonicalJson -Value $draft.current_schedule
        $currentSchedule = ConvertTo-TownLightAutomaticMaterializationProofCanonicalJson -Value $matchingSchedule[0]
        if ($historySchedule -cne $currentSchedule) { Stop-Proof 'generated_draft_current_schedule_mismatch' }
        $null = $currentScheduleHashes.Add("$binding=$(Get-TextSha256 -Value $currentSchedule)")
    }
    if ($draftIds.Count -eq 0) {
        if ($AllowFixtureBlock) { return [pscustomobject]@{ ready = $false; reason = 'automatic_run_has_no_generated_draft' } }
        Stop-Proof 'automatic_generated_draft_missing_after_restart'
    }
    [pscustomobject]@{
        ready = $true; run_id = [string]$run.run_id; automatic_actor_id = [string]$run.actor_id
        request_sha256 = [string]$run.request_sha256; result_sha256 = [string]$run.result_sha256
        checkpoint_utc_day = [int64]$run.checkpoint_utc_day; generated_draft_ids = @($draftIds | Sort-Object)
        generated_draft_bindings = @($draftBindings | Sort-Object)
        current_schedule_sha256_by_binding = @($currentScheduleHashes | Sort-Object)
        current_day_automatic_run_count = $automatic.Count
    }
}

function Get-AlertObservation {
    param([Parameter(Mandatory = $true)]$Alerts)
    $failure = @($Alerts | Where-Object { [string]$_.condition -ceq 'schedule-materialization-failure' -and [string]$_.resource_ref -ceq $ChannelId })
    $noMatch = @($Alerts | Where-Object { [string]$_.condition -ceq 'schedule-no-match' -and [string]$_.resource_ref -ceq $ChannelId })
    [ordered]@{
        failure_alert_count = $failure.Count; no_match_alert_count = $noMatch.Count
        failure_alerts = @($failure | ForEach-Object { [ordered]@{ state = [string]$_.state; severity = [string]$_.severity; occurrence_count = $_.occurrence_count } })
        no_match_alerts = @($noMatch | ForEach-Object { [ordered]@{ state = [string]$_.state; severity = [string]$_.severity; occurrence_count = $_.occurrence_count } })
        binding_claim = 'none: alert episodes are observed independently and are not asserted to bind to a materialization run.'
    }
}

function Assert-ElevatedSession {
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { Stop-Proof 'restart_proof_requires_elevated_session' }
}

function Invoke-ExactStationSc {
    param([Parameter(Mandatory = $true)][ValidateSet('stop', 'start')][string]$Verb)
    if (-not (Test-CanonicalPathEqual -Left $trustedScPath -Right (Join-Path $trustedSystemDirectory 'sc.exe')) -or
        -not (Test-Path -LiteralPath $trustedScPath -PathType Leaf)) { Stop-Proof 'sc_executable_missing' }
    $output = if ($Verb -ceq 'stop') {
        @(& $trustedScPath stop TownLightStation 2>&1)
    } else {
        @(& $trustedScPath start TownLightStation 2>&1)
    }
    $exitCode = $LASTEXITCODE
    $text = ($output | ForEach-Object { [string]$_ }) -join "`n"
    [pscustomobject]@{ outcome = if ($exitCode -eq 0) { 'success' } elseif ($text -match '(?i)access is denied|error 5') { 'access_denied' } else { 'failed' }; exit_code = $exitCode }
}

function Wait-ForExactStationState {
    param([Parameter(Mandatory = $true)][ValidateSet('Running', 'Stopped')][string]$ExpectedState)
    $deadline = [DateTime]::UtcNow.AddSeconds($ServiceTransitionTimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $identity = Get-VerifiedServiceIdentity -Name $stationServiceName
        if ($identity.state -ceq $ExpectedState) { return $identity }
        Start-Sleep -Milliseconds 500
    }
    Stop-Proof ("station_service_{0}_timeout" -f $ExpectedState.ToLowerInvariant())
}

function Connect-AfterStationRestart {
    $deadline = [DateTime]::UtcNow.AddSeconds($ServiceTransitionTimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $null = Get-VerifiedServiceIdentity -Name $stationServiceName -ExpectedState 'Running'
        try {
            $script:apiSession = New-TownLightProofSession -BaseUri ([uri]$BaseUri) -Credential $Credential -TimeoutSec $TimeoutSeconds
            return
        }
        catch { Start-Sleep -Milliseconds 500 }
    }
    Stop-Proof 'post_restart_reauthentication_timeout'
}

function Wait-PostRestartObservationWindow {
    $deadline = [DateTime]::UtcNow.AddSeconds($PostRestartObservationSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $null = Get-VerifiedServiceIdentity -Name $stationServiceName -ExpectedState 'Running'
        Start-Sleep -Seconds 1
    }
}

function Try-RecoverExactStationStart {
    if (-not $serviceControlEntered -or -not $stopIssued -or $stationStartCompleted -or $stationStartIssued -or $recoveryStartIssued) { return }
    try {
        $identity = Get-VerifiedServiceIdentity -Name $stationServiceName
        $state = if ($identity.state -ceq 'Stopped') { 'Stopped' } elseif ($identity.state -ceq 'Running') { 'Running' } else { 'Other' }
        $decision = Get-TownLightAutomaticMaterializationProofRestartDecision -Stage stop_timeout `
            -ServiceIdentityValid ([bool]$identity.valid) -ServiceState $state -ScmOutcome $stopOutcome -StartAlreadyIssued $false
        if ($decision -ceq 'issue_exact_start_recovery') {
            $recoveryStartIssued = $true
            $stationStartIssued = $true
            $result.restart_boundary.start_issued = $true
            $recovery = Invoke-ExactStationSc -Verb start
            $result.restart_boundary.recovery_start_issued = $true
            $result.restart_boundary.recovery_start_outcome = $recovery.outcome
            $result.restart_boundary.start_outcome = $recovery.outcome
        }
    }
    catch {
        $result.restart_boundary.recovery_start_issued = $false
        $result.restart_boundary.recovery_not_attempted_reason = 'service was not definitively stopped with its exact verified identity'
    }
}

try {
    Assert-TrustedWindowsRoots
    $script:apiCoordinates = Get-TownLightProofApiCoordinates -BaseUri ([uri]$BaseUri)
    $candidate = Assert-ExactInstalledCandidate
    $result.source_commit = $candidate.source_commit
    $result.candidate = [ordered]@{
        manifest_source_commit = [string]$candidate.manifest.source_commit; receipt_source_commit = [string]$candidate.receipt.source_commit
        runtime_manifest_sha256 = [string]$candidate.runtime_manifest_sha256; runtime_file_count = [int]$candidate.runtime_file_count
        stationd_sha256 = [string]$candidate.installed_hashes.stationd_sha256
        channel_worker_sha256 = [string]$candidate.installed_hashes.channel_worker_sha256; station_updater_sha256 = [string]$candidate.installed_hashes.station_updater_sha256
    }
    $stationIdentity = Get-VerifiedServiceIdentity -Name $stationServiceName -ExpectedState 'Running'
    $updaterIdentity = Get-VerifiedServiceIdentity -Name $updaterServiceName -ExpectedState 'Running'
    $health = Get-ReadyHealth
    $script:apiSession = New-TownLightProofSession -BaseUri ([uri]$BaseUri) -Credential $Credential -TimeoutSec $TimeoutSeconds
    $currentUtcDay = Get-CurrentUtcDay
    $before = Get-AuthenticatedEvidenceSnapshot -CurrentUtcDay $currentUtcDay
    $beforeAccountId = [string]$script:apiSession.Identity.account_id
    $beforeEvidence = Get-ValidatedAutomaticRunEvidence -Snapshot $before -AllowFixtureBlock
    $result.observations = [ordered]@{
        preflight = [ordered]@{
            current_utc_day = $currentUtcDay; authenticated_account_id = $beforeAccountId
            enabled_recurring_rule_count = $before.enabled_rules.Count; schedule_item_count = $before.schedule.Count
            station_service_identity = $stationIdentity.fingerprint; updater_service_identity = $updaterIdentity.fingerprint
            health = $health; alerts = Get-AlertObservation -Alerts $before.alerts
        }
    }
    if (-not $beforeEvidence.ready) {
        $result.observations.fixture = [ordered]@{ ready = $false; reason = $beforeEvidence.reason }
        Stop-ProofBlocked 'fixture_not_ready'
    }
    $result.observations.preflight.automatic_run = $beforeEvidence
    if (-not $EnableServiceRestartProof) { Stop-ProofBlocked 'restart_proof_not_enabled' }
    Assert-ElevatedSession
    $mutex = [Threading.Mutex]::new($false, $proofMutexName)
    try { $mutexHeld = $mutex.WaitOne(0) } catch { Stop-Proof 'proof_mutex_unavailable' }
    if (-not $mutexHeld) { Stop-Proof 'proof_mutex_busy' }

    # All control below this line is deliberately limited to exact Windows Service Control Manager calls for TownLightStation.
    $serviceControlEntered = $true
    $stationIdentity = Get-VerifiedServiceIdentity -Name $stationServiceName -ExpectedState 'Running'
    $stopIssued = $true
    $result.restart_boundary.stop_issued = $true
    $stop = Invoke-ExactStationSc -Verb stop
    $result.restart_boundary.attempted = $true
    $stopOutcome = [string]$stop.outcome
    $result.restart_boundary.stop_outcome = $stopOutcome
    if ($stop.outcome -ne 'success') { Stop-Proof ("station_service_stop_{0}" -f $stop.outcome) }
    $null = Wait-ForExactStationState -ExpectedState 'Stopped'
    $stationStartIssued = $true
    $result.restart_boundary.start_issued = $true
    $start = Invoke-ExactStationSc -Verb start
    $result.restart_boundary.start_outcome = [string]$start.outcome
    if ($start.outcome -ne 'success') { Stop-Proof ("station_service_start_{0}" -f $start.outcome) }
    $stationIdentity = Wait-ForExactStationState -ExpectedState 'Running'
    $stationStartCompleted = $true
    Connect-AfterStationRestart
    if ([string]$script:apiSession.Identity.account_id -cne $beforeAccountId) { Stop-Proof 'authenticated_account_changed_after_restart' }
    Wait-PostRestartObservationWindow
    $candidateAfter = Assert-ExactInstalledCandidate
    if ($candidateAfter.source_commit -cne $candidate.source_commit) { Stop-Proof 'candidate_changed_during_restart_proof' }
    $healthAfter = Get-ReadyHealth
    if ((Get-CurrentUtcDay) -ne $currentUtcDay) { Stop-Proof 'utc_day_rolled_over_during_restart_proof' }
    $after = Get-AuthenticatedEvidenceSnapshot -CurrentUtcDay $currentUtcDay
    $afterEvidence = Get-ValidatedAutomaticRunEvidence -Snapshot $after
    $matchesBaseline = $afterEvidence.run_id -ceq $beforeEvidence.run_id -and $afterEvidence.automatic_actor_id -ceq $beforeEvidence.automatic_actor_id -and
        $afterEvidence.request_sha256 -ceq $beforeEvidence.request_sha256 -and $afterEvidence.result_sha256 -ceq $beforeEvidence.result_sha256 -and
        (@($afterEvidence.generated_draft_ids) -join '|') -ceq (@($beforeEvidence.generated_draft_ids) -join '|') -and
        (@($afterEvidence.generated_draft_bindings) -join '|') -ceq (@($beforeEvidence.generated_draft_bindings) -join '|') -and
        (@($afterEvidence.current_schedule_sha256_by_binding) -join '|') -ceq (@($beforeEvidence.current_schedule_sha256_by_binding) -join '|')
    if (-not $matchesBaseline) { Stop-Proof 'post_restart_automatic_run_duplicate_or_drift' }
    $stationIdentity = Get-VerifiedServiceIdentity -Name $stationServiceName -ExpectedState 'Running'
    $result.observations.post_restart = [ordered]@{
        current_utc_day = $currentUtcDay; authenticated_account_id = [string]$script:apiSession.Identity.account_id
        station_service_identity = $stationIdentity.fingerprint; health = $healthAfter; automatic_run = $afterEvidence
        alerts = Get-AlertObservation -Alerts $after.alerts; duplicate_current_day_automatic_run = $false
        duplicate_generated_draft_identifier = $false; newer_automatic_run = $false
    }
    $result.status = 'passed'
}
catch {
    $message = [string]$_.Exception.Message
    if ($message -match '^proof-blocked:(?<code>[A-Za-z0-9_-]+)$') {
        $result.status = 'blocked'; $result.error_code = $Matches.code
    } elseif ($message -match '^proof:(?<code>[A-Za-z0-9_-]+)$') {
        $result.status = 'failed'; $result.error_code = $Matches.code
    } else {
        $result.status = 'failed'; $result.error_code = 'installed_automatic_materialization_proof_failed'
    }
}
finally {
    Try-RecoverExactStationStart
    if ($mutexHeld -and $null -ne $mutex) { $mutex.ReleaseMutex() }
    if ($null -ne $mutex) { $mutex.Dispose() }
}

Write-ProofResult
if ($result.status -ne 'passed') { exit 1 }
