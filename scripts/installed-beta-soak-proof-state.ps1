Set-StrictMode -Version Latest

function ConvertTo-TownLightBetaSoakCanonicalJson {
    [CmdletBinding()]
    param([AllowNull()]$Value)
    if ($null -eq $Value) { return 'null' }
    if ($Value -is [string] -or $Value -is [char] -or $Value -is [bool] -or
        $Value -is [byte] -or $Value -is [sbyte] -or $Value -is [int16] -or
        $Value -is [uint16] -or $Value -is [int32] -or $Value -is [uint32] -or
        $Value -is [int64] -or $Value -is [uint64] -or $Value -is [single] -or
        $Value -is [double] -or $Value -is [decimal]) {
        return (ConvertTo-Json -InputObject $Value -Compress)
    }
    if ($Value -is [Collections.IDictionary]) {
        $members = foreach ($key in @($Value.Keys | ForEach-Object { [string]$_ } | Sort-Object)) {
            (ConvertTo-Json -InputObject $key -Compress) + ':' +
                (ConvertTo-TownLightBetaSoakCanonicalJson -Value $Value[$key])
        }
        return '{' + ($members -join ',') + '}'
    }
    if ($Value -is [Collections.IEnumerable]) {
        $items = foreach ($item in $Value) { ConvertTo-TownLightBetaSoakCanonicalJson -Value $item }
        return '[' + ($items -join ',') + ']'
    }
    $members = foreach ($property in @($Value.PSObject.Properties | Sort-Object Name)) {
        (ConvertTo-Json -InputObject ([string]$property.Name) -Compress) + ':' +
            (ConvertTo-TownLightBetaSoakCanonicalJson -Value $property.Value)
    }
    '{' + ($members -join ',') + '}'
}

function Get-TownLightBetaSoakObjectHash {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Value)
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(
        (ConvertTo-TownLightBetaSoakCanonicalJson -Value $Value)
    )
    $sha = [Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Test-TownLightBetaSoakConfigurationResult {
    [CmdletBinding()]
    param($Value,[string]$StationId,[uint64]$StationRevision,[string]$ChannelId,[string]$ExpectedState,[string]$SourceCommit,[string]$CandidateManifestSha256,[string]$InstallerSha256,[string]$CommissioningResultSha256)
    $required=@('schema_version','product','result_kind','gate_status','run_id','finished_at_unix_ms','station_id','station_revision','channel_id','expected_state','source_commit','candidate_manifest_sha256','installer_sha256','commissioning_result_sha256')
    if ($null -eq $Value -or (@($Value.PSObject.Properties.Name|Sort-Object)-join '|') -cne (@($required|Sort-Object)-join '|')) { return 'configuration_result_schema_invalid' }
    if ([int]$Value.schema_version -ne 1 -or [string]$Value.product -cne 'TownLight Station' -or [string]$Value.result_kind -cne 'fresh_configuration_run' -or [string]$Value.gate_status -cne 'passed' -or
        [string]$Value.run_id -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' -or [int64]$Value.finished_at_unix_ms -le 0) { return 'configuration_result_schema_invalid' }
    if ([string]$Value.station_id -cne $StationId -or [uint64]$Value.station_revision -ne $StationRevision -or [string]$Value.channel_id -cne $ChannelId -or [string]$Value.expected_state -cne $ExpectedState -or
        [string]$Value.source_commit -cne $SourceCommit -or [string]$Value.candidate_manifest_sha256 -cne $CandidateManifestSha256 -or [string]$Value.installer_sha256 -cne $InstallerSha256 -or [string]$Value.commissioning_result_sha256 -cne $CommissioningResultSha256) { return 'configuration_result_binding_mismatch' }
    'ok'
}

function Test-TownLightBetaSoakAuthority {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedStationHash,
        [Parameter(Mandatory = $true)][string]$ExpectedChannelHash,
        [Parameter(Mandatory = $true)][string]$ExpectedChannelId,
        [Parameter(Mandatory = $true)][ValidateSet('healthy_fallback', 'on_air')][string]$ExpectedState,
        [Parameter(Mandatory = $true)]$Station,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$Channels,
        [Parameter(Mandatory = $true)]$Runtime
    )
    if ((Get-TownLightBetaSoakObjectHash $Station) -cne $ExpectedStationHash) { return 'station_authority_changed' }
    $selected = @($Channels | Where-Object { [string]$_.channel_id -ceq $ExpectedChannelId })
    if ($selected.Count -ne 1) { return 'selected_channel_not_exactly_once' }
    if ((Get-TownLightBetaSoakObjectHash $selected[0]) -cne $ExpectedChannelHash) { return 'selected_channel_configuration_changed' }
    $runtimeSelected = @($Runtime.channels | Where-Object { [string]$_.channel_id -ceq $ExpectedChannelId })
    if ($runtimeSelected.Count -ne 1) { return 'selected_runtime_not_exactly_once' }
    if ([string]$Runtime.color -cne 'green' -or [string]$runtimeSelected[0].state -cne $ExpectedState) {
        return 'selected_runtime_state_changed'
    }
    'ok'
}

function Get-TownLightBetaSoakAlertDecision {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object[]]$Alerts,
        [Parameter(Mandatory = $true)][string]$StationId,
        [Parameter(Mandatory = $true)][string]$ChannelId,
        [Parameter(Mandatory = $true)][hashtable]$BaselineOccurrences
    )
    $fatalConditions = @('off-air', 'output-stall', 'encoder-death')
    foreach ($alert in $Alerts) {
        $state = ([string]$alert.state).ToLowerInvariant()
        $severity = ([string]$alert.severity).ToLowerInvariant()
        $condition = (([string]$alert.condition).ToLowerInvariant() -replace '_','-')
        $relevant = [string]$alert.resource_ref -in @($StationId, $ChannelId, 'station', 'system')
        $firing = $state -ceq 'firing'
        $fatalSeverity = $severity -in @('critical', 'error')
        $fatalCondition = $condition -in $fatalConditions
        if (-not $relevant -or -not $firing -or (-not $fatalSeverity -and -not $fatalCondition)) { continue }
        $key = "$($alert.event_id)|$condition|$($alert.resource_ref)"
        if (-not $BaselineOccurrences.ContainsKey($key)) { return 'new_firing_alert' }
        if ([uint64]$alert.occurrence_count -gt [uint64]$BaselineOccurrences[$key]) { return 'firing_alert_recurred' }
        return 'preflight_firing_alert'
    }
    'ok'
}

function Get-TownLightBetaSoakEventAdvance {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][int64]$Cursor, [AllowEmptyCollection()][object[]]$Records)
    $ordered = @($Records | Where-Object { [int64]$_.record_id -gt $Cursor } | Sort-Object record_id)
    if ($ordered.Count -ge 512) { throw 'event_log_batch_capacity_exceeded' }
    $next = $Cursor
    if ($ordered.Count -gt 0) { $next = [int64]$ordered[-1].record_id }
    [pscustomobject]@{ cursor = $next; records = $ordered }
}

function Get-TownLightBetaSoakEventCheckpointDecision {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)]$Checkpoint,[int64]$LatestRecordId,[int64]$OldestRecordId,[int64]$OldestTimeUtcTicks)
    if ($LatestRecordId -lt [int64]$Checkpoint.cursor -or $LatestRecordId -lt [int64]$Checkpoint.latest_observed_record_id -or $OldestRecordId -lt [int64]$Checkpoint.oldest_record_id -or
        ($OldestRecordId -eq [int64]$Checkpoint.oldest_record_id -and $OldestTimeUtcTicks -ne [int64]$Checkpoint.oldest_time_utc_ticks)) { return 'event_log_checkpoint_reset' }
    if ($OldestRecordId -gt ([int64]$Checkpoint.cursor + 1)) { return 'event_log_checkpoint_gap' }
    'ok'
}

function New-TownLightBetaSoakCoverageState {
    [CmdletBinding()]
    param([double]$StartElapsedSeconds=0.0,[Parameter(Mandatory=$true)][int64]$StartUtcMs)
    [ordered]@{ actual = 0; prior_elapsed_seconds = $StartElapsedSeconds; prior_utc_ms = $StartUtcMs; max_normal_gap_seconds = 0.0; max_restart_gap_seconds = 0.0 }
}

function Add-TownLightBetaSoakCoverageCompletion {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$State,[double]$ElapsedSeconds,[int64]$UtcMs,[switch]$RestartBoundary)
    if ($null -ne $State.prior_elapsed_seconds -and $null -ne $State.prior_utc_ms) {
        $mono = $ElapsedSeconds - [double]$State.prior_elapsed_seconds
        $utc = ($UtcMs - [int64]$State.prior_utc_ms) / 1000.0
        if ($mono -le 0 -or $utc -le 0 -or [Math]::Abs($mono - $utc) -gt 5.0) { throw 'host_clock_or_suspend_discontinuity' }
        if ($RestartBoundary) {
            $State.max_restart_gap_seconds = [Math]::Max([double]$State.max_restart_gap_seconds,$mono)
            if ($mono -gt 100.0) { throw 'restart_observation_gap_exceeded' }
        } else {
            $State.max_normal_gap_seconds = [Math]::Max([double]$State.max_normal_gap_seconds,$mono)
            if ($mono -gt 15.0) { throw 'normal_observation_gap_exceeded' }
        }
    }
    $State.actual++
    $State.prior_elapsed_seconds=$ElapsedSeconds; $State.prior_utc_ms=$UtcMs
}

function Get-TownLightBetaSoakCoverageResult {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$State,[double]$DurationSeconds,[double]$IntervalSeconds)
    $expected=[int][Math]::Floor($DurationSeconds/$IntervalSeconds)+1
    $minimum=[int][Math]::Ceiling($expected*0.98)
    $ratio=if($expected -gt 0){[double]$State.actual/$expected}else{0.0}
    [pscustomobject]@{ expected=$expected; minimum_required=$minimum; actual=[int]$State.actual; coverage_ratio=$ratio; max_normal_gap_seconds=[double]$State.max_normal_gap_seconds; max_restart_gap_seconds=[double]$State.max_restart_gap_seconds; passed=([int]$State.actual -ge $minimum -and [double]$State.max_normal_gap_seconds -le 15.0 -and [double]$State.max_restart_gap_seconds -le 100.0) }
}

function Get-TownLightBetaSoakResourceDecision {
    [CmdletBinding()]
    param(
        [AllowNull()]$Prior,
        [Parameter(Mandatory = $true)]$Current,
        [switch]$RestartBoundary
    )
    if ([uint64]$Current.sample_id -eq 0) { return 'resource_sample_invalid' }
    if ($null -eq $Prior) { return 'ok' }
    if ([uint64]$Current.sample_id -lt [uint64]$Prior.sample_id) { return 'resource_sample_identity_regressed' }
    if ($RestartBoundary) {
        if ([uint64]$Current.sample_id -le [uint64]$Prior.sample_id) { return 'restart_resource_not_newer' }
        $gap = [int64]$Current.sampled_at_unix_ms - [int64]$Prior.sampled_at_unix_ms
        if ($gap -le 0 -or $gap -gt 75000) { return 'restart_resource_gap_invalid' }
        return 'ok'
    }
    if ([uint64]$Current.sample_id -gt [uint64]$Prior.sample_id) {
        $cadence = [int64]$Current.sampled_at_unix_ms - [int64]$Prior.sampled_at_unix_ms
        if ($cadence -lt 55000 -or $cadence -gt 70000) { return 'resource_sample_cadence_invalid' }
    }
    'ok'
}

function Get-TownLightBetaSoakRestartDecision {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][int64]$PrePid,
        [Parameter(Mandatory = $true)][int64]$PostPid,
        [Parameter(Mandatory = $true)][string]$PreAuthorityHash,
        [Parameter(Mandatory = $true)][string]$PostAuthorityHash,
        [Parameter(Mandatory = $true)][string]$ResourceDecision,
        [Parameter(Mandatory = $true)][double]$RecoverySeconds,
        [Parameter(Mandatory = $true)][bool]$AuthenticatedIdentityMatches
    )
    if ($PrePid -le 0 -or $PostPid -le 0 -or $PrePid -eq $PostPid) { return 'restart_process_identity_invalid' }
    if ($PreAuthorityHash -cne $PostAuthorityHash) { return 'restart_authority_changed' }
    if ($ResourceDecision -cne 'ok') { return $ResourceDecision }
    if ($RecoverySeconds -gt 90) { return 'restart_recovery_exceeded_90_seconds' }
    if (-not $AuthenticatedIdentityMatches) { return 'authenticated_identity_changed' }
    'ok'
}

function Test-TownLightBetaSoakMayPass {
    [CmdletBinding()]
    param([double]$ElapsedHours, [int]$RestartCount, [bool]$Failed, [bool]$CoveragePassed = $true)
    -not $Failed -and $ElapsedHours -ge 4 -and $RestartCount -eq 1 -and $CoveragePassed
}

function Invoke-TownLightBetaSoakFinalizationStateMachine {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)][scriptblock]$CloseEvidence,[Parameter(Mandatory=$true)][scriptblock]$FinalizeEvidence,[Parameter(Mandatory=$true)][scriptblock]$PersistFailure)
    try { & $CloseEvidence; & $FinalizeEvidence; [pscustomobject]@{passed=$true;failure=''} }
    catch { $detail=$_.Exception.Message; try { & $PersistFailure $detail } catch { }; [pscustomobject]@{passed=$false;failure=$detail} }
}

function Write-TownLightBetaSoakFailureReceipt {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$RunId,
        [Parameter(Mandatory = $true)][string]$Detail
    )
    $safe = ($Detail -replace '[\u0000-\u001f\u007f-\u009f]', ' ').Trim()
    if ($safe.Length -gt 1024) { $safe = $safe.Substring(0, 1024) }
    $value = [ordered]@{ schema_version = 1; run_id = $RunId; status = 'failed'; failed_at_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds(); detail = $safe }
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($value | ConvertTo-Json -Depth 8))
    $stream = [IO.FileStream]::new($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) }
    finally { $stream.Dispose() }
    $value
}

function New-TownLightBetaSoakMutexSecurity {
    $security = [Security.AccessControl.MutexSecurity]::new()
    $security.SetAccessRuleProtection($true, $false)
    foreach ($sidValue in @('S-1-5-18', 'S-1-5-32-544')) {
        $sid = [Security.Principal.SecurityIdentifier]::new($sidValue)
        $rule = [Security.AccessControl.MutexAccessRule]::new(
            $sid, [Security.AccessControl.MutexRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow
        )
        $security.AddAccessRule($rule)
    }
    $security
}

function New-TownLightBetaSoakMutexHandle {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)][string]$Name)
    $security=New-TownLightBetaSoakMutexSecurity
    $arguments=[object[]]@($false,$Name,$false,$security)
    $mutexAclType='System.Threading.MutexAcl' -as [type]
    if($null -ne $mutexAclType){
        $create=$mutexAclType.GetMethods()|Where-Object{$_.Name -eq 'Create' -and $_.IsStatic -and $_.GetParameters().Count -eq 4}|Select-Object -First 1
        if($null -eq $create){throw 'beta_soak_mutex_secure_factory_unavailable'}
        $mutex=$create.Invoke($null,$arguments)
    }else{
        $securityType='System.Security.AccessControl.MutexSecurity' -as [type]
        $constructor=[Threading.Mutex].GetConstructors()|Where-Object{$p=$_.GetParameters();$p.Count -eq 4 -and $p[0].ParameterType -eq [bool] -and $p[1].ParameterType -eq [string] -and $p[2].ParameterType.IsByRef -and $p[3].ParameterType -eq $securityType}|Select-Object -First 1
        if($null -eq $constructor){throw 'beta_soak_mutex_secure_factory_unavailable'}
        $mutex=$constructor.Invoke($arguments)
    }
    [pscustomobject]@{mutex=$mutex;created_new=[bool]$arguments[2]}
}

function Get-TownLightBetaSoakMutexAccessControl {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)][Threading.Mutex]$Mutex)
    $instance=$Mutex.GetType().GetMethods()|Where-Object{$_.Name -eq 'GetAccessControl' -and -not $_.IsStatic -and $_.GetParameters().Count -eq 0}|Select-Object -First 1
    if($null -ne $instance){return $instance.Invoke($Mutex,@())}
    $mutexAclType='System.Threading.MutexAcl' -as [type]
    if($null -ne $mutexAclType){$method=$mutexAclType.GetMethods()|Where-Object{$_.Name -eq 'GetAccessControl' -and $_.IsStatic -and $_.GetParameters().Count -eq 1}|Select-Object -First 1;if($null -ne $method){return $method.Invoke($null,@($Mutex))}}
    $null
}

function Enter-TownLightBetaSoakFence {
    [CmdletBinding()]
    param(
        [string]$Name = 'Global\TownLightStation.InstalledBetaSoak.v1',
        [ValidateRange(0, 5000)][int]$WaitMilliseconds = 0
    )
    $created=New-TownLightBetaSoakMutexHandle -Name $Name
    $mutex=$created.mutex
    try {
        $acquired = Wait-TownLightBetaSoakFenceMutex -Mutex $mutex -WaitMilliseconds $WaitMilliseconds
        [pscustomobject]@{ mutex = $mutex; acquired = $true; created_new = $created.created_new }
    }
    catch {
        $mutex.Dispose()
        throw
    }
}

function Wait-TownLightBetaSoakFenceMutex {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][Threading.Mutex]$Mutex, [ValidateRange(0, 5000)][int]$WaitMilliseconds = 0)
    try { $acquired = $Mutex.WaitOne($WaitMilliseconds) }
    catch [Threading.AbandonedMutexException] {
        $Mutex.ReleaseMutex()
        throw 'beta_soak_fence_abandoned'
    }
    if (-not $acquired) { throw 'beta_soak_fence_busy' }
    $true
}

function Exit-TownLightBetaSoakFence {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Fence)
    if ($Fence.acquired) { $Fence.mutex.ReleaseMutex(); $Fence.acquired = $false }
    $Fence.mutex.Dispose()
}
