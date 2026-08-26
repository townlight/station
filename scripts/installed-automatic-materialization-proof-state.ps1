Set-StrictMode -Version Latest

function Get-TownLightAutomaticMaterializationProofUtcDay {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][int64]$UnixTimeMilliseconds)

    # PowerShell's / produces a floating-point value. An explicit floor avoids
    # parameter coercion rounding a late-day timestamp into tomorrow.
    [int64][Math]::Floor($UnixTimeMilliseconds / 86400000.0)
}

function ConvertTo-TownLightAutomaticMaterializationProofCanonicalJson {
    [CmdletBinding()]
    param([Parameter(Mandatory = $false)]$Value)

    if ($null -eq $Value) { return 'null' }
    if ($Value -is [string] -or $Value -is [char] -or $Value -is [bool] -or
        $Value -is [byte] -or $Value -is [sbyte] -or $Value -is [int16] -or
        $Value -is [uint16] -or $Value -is [int32] -or $Value -is [uint32] -or
        $Value -is [int64] -or $Value -is [uint64] -or $Value -is [single] -or
        $Value -is [double] -or $Value -is [decimal] -or $Value -is [DateTime] -or
        $Value -is [DateTimeOffset] -or $Value -is [guid]) {
        return (ConvertTo-Json -InputObject $Value -Compress)
    }
    if ($Value -is [Collections.IDictionary]) {
        $members = foreach ($key in @($Value.Keys | ForEach-Object { [string]$_ } | Sort-Object)) {
            (ConvertTo-Json -InputObject $key -Compress) + ':' +
                (ConvertTo-TownLightAutomaticMaterializationProofCanonicalJson -Value $Value[$key])
        }
        return '{' + ($members -join ',') + '}'
    }
    if ($Value -is [Collections.IEnumerable]) {
        $items = foreach ($item in $Value) {
            ConvertTo-TownLightAutomaticMaterializationProofCanonicalJson -Value $item
        }
        return '[' + ($items -join ',') + ']'
    }

    $properties = @($Value.PSObject.Properties | Where-Object {
            $_.MemberType -in @('NoteProperty', 'Property', 'AliasProperty', 'ScriptProperty')
        } | Sort-Object -Property Name)
    if ($properties.Count -eq 0) { return (ConvertTo-Json -InputObject $Value -Compress) }
    $members = foreach ($property in $properties) {
        (ConvertTo-Json -InputObject ([string]$property.Name) -Compress) + ':' +
            (ConvertTo-TownLightAutomaticMaterializationProofCanonicalJson -Value $property.Value)
    }
    '{' + ($members -join ',') + '}'
}

function Get-TownLightAutomaticMaterializationProofServiceIdentity {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        $Service,
        [Parameter(Mandatory = $true)]
        [string]$ApplicationRoot,
        [Parameter(Mandatory = $true)]
        [string]$DataRoot,
        [Parameter(Mandatory = $true)]
        [ValidateSet('TownLightStation', 'TownLightStationUpdater')]
        [string]$ServiceName
    )

    $binaryName = if ($ServiceName -ceq 'TownLightStation') {
        'stationd.exe'
    } else {
        'station-updater.exe'
    }
    $binaryPath = [IO.Path]::GetFullPath((Join-Path $ApplicationRoot $binaryName))
    $databasePath = [IO.Path]::GetFullPath((Join-Path $DataRoot 'station.db'))
    $expectedPath = if ($ServiceName -ceq 'TownLightStation') {
        '"' + $binaryPath + '" service --database "' + $databasePath + '" --address 127.0.0.1:4070'
    } else {
        '"' + $binaryPath + '" service --database "' + $databasePath +
        '" --application-root "' + [IO.Path]::GetFullPath($ApplicationRoot) + '"'
    }

    if ($null -eq $Service) {
        return [pscustomobject]@{ valid = $false; code = 'service_missing'; fingerprint = $null; state = 'Unknown' }
    }
    if ([string]$Service.Name -cne $ServiceName) {
        return [pscustomobject]@{ valid = $false; code = 'service_name_mismatch'; fingerprint = $null; state = [string]$Service.State }
    }
    if ([string]$Service.PathName -cne $expectedPath) {
        return [pscustomobject]@{ valid = $false; code = 'service_path_mismatch'; fingerprint = $null; state = [string]$Service.State }
    }
    if ([string]$Service.StartName -cne 'LocalSystem') {
        return [pscustomobject]@{ valid = $false; code = 'service_account_mismatch'; fingerprint = $null; state = [string]$Service.State }
    }
    if ([string]$Service.StartMode -cne 'Auto') {
        return [pscustomobject]@{ valid = $false; code = 'service_start_mode_mismatch'; fingerprint = $null; state = [string]$Service.State }
    }

    [pscustomobject]@{
        valid = $true
        code = 'ok'
        state = [string]$Service.State
        fingerprint = [ordered]@{
            name = [string]$Service.Name
            path_name = [string]$Service.PathName
            start_name = [string]$Service.StartName
            start_mode = [string]$Service.StartMode
        }
    }
}

function Get-TownLightAutomaticMaterializationProofRestartDecision {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('preflight', 'stop_result', 'stop_timeout', 'start_result', 'start_timeout', 'post_restart')]
        [string]$Stage,
        [Parameter(Mandatory = $true)]
        [bool]$ServiceIdentityValid,
        [Parameter(Mandatory = $true)]
        [ValidateSet('Running', 'Stopped', 'Other', 'Unknown')]
        [string]$ServiceState,
        [Parameter(Mandatory = $true)]
        [ValidateSet('success', 'access_denied', 'failed', 'not_run')]
        [string]$ScmOutcome,
        [Parameter(Mandatory = $true)]
        [bool]$StartAlreadyIssued
    )

    if (-not $ServiceIdentityValid) {
        return 'fail_identity_drift'
    }
    if ($Stage -ceq 'preflight') {
        if ($ServiceState -ceq 'Running') { return 'issue_exact_stop' }
        return 'fail_not_running'
    }
    if ($ScmOutcome -ceq 'access_denied') { return 'fail_access_denied' }
    if ($Stage -in @('stop_result', 'stop_timeout') -and $ScmOutcome -ceq 'success' -and
        $ServiceState -ceq 'Stopped' -and -not $StartAlreadyIssued) {
        return 'issue_exact_start_recovery'
    }
    if ($Stage -in @('stop_result', 'stop_timeout') -and $ScmOutcome -in @('failed', 'not_run')) {
        return 'fail_no_recovery'
    }
    if ($Stage -eq 'stop_result' -and $ScmOutcome -eq 'success') {
        return 'wait_stopped'
    }
    if ($Stage -eq 'start_result' -and $ScmOutcome -eq 'success') {
        return 'wait_running'
    }
    if ($Stage -in @('stop_timeout', 'start_timeout')) { return 'fail_transition_timeout' }
    if ($Stage -eq 'post_restart' -and $ServiceState -eq 'Running') { return 'continue' }
    'fail_no_recovery'
}

function Get-TownLightAutomaticMaterializationProofHistoryDecision {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [int]$CurrentDayAutomaticRunCount,
        [Parameter(Mandatory = $true)]
        [int]$GeneratedDraftCount,
        [Parameter(Mandatory = $true)]
        [bool]$DraftIdentifiersUnique,
        [Parameter(Mandatory = $true)]
        [bool]$PostRestartMatchesBaseline
    )

    if ($CurrentDayAutomaticRunCount -eq 0 -or $GeneratedDraftCount -eq 0) {
        return 'blocked_fixture_not_ready'
    }
    if ($CurrentDayAutomaticRunCount -ne 1) { return 'fail_duplicate_automatic_run' }
    if (-not $DraftIdentifiersUnique) { return 'fail_duplicate_draft_identifier' }
    if (-not $PostRestartMatchesBaseline) { return 'fail_post_restart_duplicate_or_drift' }
    'passed'
}
