[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$statePath = Join-Path $PSScriptRoot 'installed-automatic-materialization-proof-state.ps1'
$assertions = 0

function Assert-Contract([bool]$Condition, [string]$Message) {
    $script:assertions++
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
}

Assert-Contract (Test-Path -LiteralPath $statePath -PathType Leaf) `
    'the automatic-materialization proof state helper must exist.'
. $statePath

Assert-Contract ((Get-TownLightAutomaticMaterializationProofUtcDay -UnixTimeMilliseconds 0) -eq 0) `
    'the UTC-day helper must preserve the Unix epoch boundary.'
Assert-Contract ((Get-TownLightAutomaticMaterializationProofUtcDay -UnixTimeMilliseconds 86399999) -eq 0) `
    'the UTC-day helper must not round late-day milliseconds into tomorrow.'
Assert-Contract ((Get-TownLightAutomaticMaterializationProofUtcDay -UnixTimeMilliseconds 86400000) -eq 1) `
    'the UTC-day helper must advance only at the next exact UTC-day boundary.'

$scheduleA = [pscustomobject]@{ title = 'Council'; revision = 1; nested = [pscustomobject]@{ z = $true; a = 7 } }
$scheduleB = [pscustomobject]@{ nested = [pscustomobject]@{ a = 7; z = $true }; revision = 1; title = 'Council' }
$scheduleChanged = [pscustomobject]@{ nested = [pscustomobject]@{ a = 7; z = $true }; revision = 2; title = 'Council' }
$canonicalA = ConvertTo-TownLightAutomaticMaterializationProofCanonicalJson -Value $scheduleA
$canonicalB = ConvertTo-TownLightAutomaticMaterializationProofCanonicalJson -Value $scheduleB
$canonicalChanged = ConvertTo-TownLightAutomaticMaterializationProofCanonicalJson -Value $scheduleChanged
Assert-Contract ($canonicalA -ceq $canonicalB) `
    'canonical ScheduleItem evidence must ignore JSON property ordering.'
Assert-Contract ($canonicalA -cne $canonicalChanged) `
    'canonical ScheduleItem evidence must detect every exposed field change.'

$applicationRoot = 'C:\Program Files\TownLight Station'
$dataRoot = 'C:\ProgramData\TownLight Station'
$stationPath = '"C:\Program Files\TownLight Station\stationd.exe" service --database "C:\ProgramData\TownLight Station\station.db" --address 127.0.0.1:4070'
$service = [pscustomobject]@{
    Name = 'TownLightStation'; PathName = $stationPath; StartName = 'LocalSystem'; StartMode = 'Auto'; State = 'Running'
}
$identity = Get-TownLightAutomaticMaterializationProofServiceIdentity -Service $service `
    -ApplicationRoot $applicationRoot -DataRoot $dataRoot -ServiceName TownLightStation
Assert-Contract $identity.valid 'an exact TownLightStation service identity must validate.'
Assert-Contract ($identity.fingerprint.path_name -ceq $stationPath) 'the identity fingerprint must retain the exact service command line.'

$drift = [pscustomobject]@{ Name = 'TownLightStation'; PathName = 'C:\wrong.exe'; StartName = 'LocalSystem'; StartMode = 'Auto'; State = 'Running' }
$driftIdentity = Get-TownLightAutomaticMaterializationProofServiceIdentity -Service $drift `
    -ApplicationRoot $applicationRoot -DataRoot $dataRoot -ServiceName TownLightStation
Assert-Contract (-not $driftIdentity.valid -and $driftIdentity.code -eq 'service_path_mismatch') `
    'service path drift must fail closed before control.'

Assert-Contract ((Get-TownLightAutomaticMaterializationProofRestartDecision -Stage preflight `
        -ServiceIdentityValid $true -ServiceState Running -ScmOutcome not_run -StartAlreadyIssued $false) -eq 'issue_exact_stop') `
    'only an exact running service may enter the stop transition.'
Assert-Contract ((Get-TownLightAutomaticMaterializationProofRestartDecision -Stage stop_result `
        -ServiceIdentityValid $true -ServiceState Running -ScmOutcome access_denied -StartAlreadyIssued $false) -eq 'fail_access_denied') `
    'access denied must not cause a recovery action.'
Assert-Contract ((Get-TownLightAutomaticMaterializationProofRestartDecision -Stage stop_timeout `
        -ServiceIdentityValid $true -ServiceState Stopped -ScmOutcome access_denied -StartAlreadyIssued $false) -eq 'fail_access_denied') `
    'access denied must forbid recovery even if a later read reports stopped.'
Assert-Contract ((Get-TownLightAutomaticMaterializationProofRestartDecision -Stage stop_timeout `
        -ServiceIdentityValid $true -ServiceState Stopped -ScmOutcome failed -StartAlreadyIssued $false) -eq 'fail_no_recovery') `
    'an unverified failed stop must forbid recovery even if a later read reports stopped.'
Assert-Contract ((Get-TownLightAutomaticMaterializationProofRestartDecision -Stage stop_timeout `
        -ServiceIdentityValid $true -ServiceState Running -ScmOutcome success -StartAlreadyIssued $false) -eq 'fail_transition_timeout') `
    'a stop timeout while the service is not definitively stopped must not recover.'
Assert-Contract ((Get-TownLightAutomaticMaterializationProofRestartDecision -Stage stop_timeout `
        -ServiceIdentityValid $true -ServiceState Stopped -ScmOutcome success -StartAlreadyIssued $false) -eq 'issue_exact_start_recovery') `
    'recovery may issue one exact start only for the verified stopped station service.'
Assert-Contract ((Get-TownLightAutomaticMaterializationProofRestartDecision -Stage stop_timeout `
        -ServiceIdentityValid $false -ServiceState Stopped -ScmOutcome success -StartAlreadyIssued $false) -eq 'fail_identity_drift') `
    'identity drift must forbid recovery even when a service reports stopped.'
Assert-Contract ((Get-TownLightAutomaticMaterializationProofRestartDecision -Stage start_timeout `
        -ServiceIdentityValid $true -ServiceState Stopped -ScmOutcome success -StartAlreadyIssued $true) -eq 'fail_transition_timeout') `
    'the state machine must forbid a second start after a start timeout.'

Assert-Contract ((Get-TownLightAutomaticMaterializationProofHistoryDecision -CurrentDayAutomaticRunCount 0 `
        -GeneratedDraftCount 0 -DraftIdentifiersUnique $true -PostRestartMatchesBaseline $true) -eq 'blocked_fixture_not_ready') `
    'no automatic Draft fixture must block before Windows Service Control Manager control.'
Assert-Contract ((Get-TownLightAutomaticMaterializationProofHistoryDecision -CurrentDayAutomaticRunCount 2 `
        -GeneratedDraftCount 2 -DraftIdentifiersUnique $true -PostRestartMatchesBaseline $true) -eq 'fail_duplicate_automatic_run') `
    'a duplicate current-day automatic run must fail closed.'
Assert-Contract ((Get-TownLightAutomaticMaterializationProofHistoryDecision -CurrentDayAutomaticRunCount 1 `
        -GeneratedDraftCount 2 -DraftIdentifiersUnique $false -PostRestartMatchesBaseline $true) -eq 'fail_duplicate_draft_identifier') `
    'duplicate generated Draft identities must fail closed.'
Assert-Contract ((Get-TownLightAutomaticMaterializationProofHistoryDecision -CurrentDayAutomaticRunCount 1 `
        -GeneratedDraftCount 1 -DraftIdentifiersUnique $true -PostRestartMatchesBaseline $false) -eq 'fail_post_restart_duplicate_or_drift') `
    'a newer post-restart run or changed identity must fail closed.'

Write-Output "Installed automatic-materialization proof state contract passed ($assertions assertions; no Windows Service Control Manager control or mutation)."
