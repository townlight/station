param()

$ErrorActionPreference = 'Stop'
$gatePath = Join-Path $PSScriptRoot 'invoke-native-crash-gate.ps1'
$assertionCount = 0

function Assert-True {
    param([bool]$Condition, [string]$Message)
    $script:assertionCount++
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
}

Assert-True (Test-Path -LiteralPath $gatePath -PathType Leaf) `
    'the bounded native crash-evidence gate must exist'

$source = Get-Content -LiteralPath $gatePath -Raw
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $gatePath,
    [ref]$tokens,
    [ref]$parseErrors
)
Assert-True ($parseErrors.Count -eq 0) 'the native crash gate must parse as PowerShell'
Assert-True ($source -match '\bGet-WinEvent\b') 'the gate must inspect the Windows event log'
Assert-True ($source -match '\bRecordId\b') 'the gate must checkpoint event-log record IDs'
Assert-True ($source -match 'Application Error') 'the gate must inspect Application Error events'
Assert-True ($source -match 'Windows Error Reporting') `
    'the gate must inspect Windows Error Reporting events'
Assert-True ($source -match '\b1000\b' -and $source -match '\b1001\b') `
    'the gate must bound the native crash and WER event IDs'
Assert-True ($source -match '\bWaitForExit\b') 'the gate must wait for the launched process'
Assert-True ($source -match '\bKill\s*\(') 'the gate must terminate a timed-out process tree'
Assert-True ($source -match 'eventObservationDeadlineUtc') `
    'the gate must poll through a declared event-observation deadline'
Assert-True ($source -match 'requires_isolated_component_execution') `
    'the gate must disclose its component-name correlation isolation requirement'
Assert-True ($source -match 'ConvertTo-Json') 'the gate must write machine-readable evidence'
Assert-True ($source -notmatch '(?i)Clear-EventLog|wevtutil\s+cl|Remove-Item.+(?:ReportArchive|WER)') `
    'the gate must never clear or delete Windows crash evidence'
Assert-True ($source -notmatch '(?i)\bMessage\s*=') `
    'the evidence must not copy raw Windows event messages containing machine paths'

$pwshPath = (Get-Command pwsh.exe -ErrorAction Stop).Source
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) "townlight-native-crash-gate-$([guid]::NewGuid().ToString('N'))"
$successEvidence = Join-Path $temporaryRoot 'success.json'
$failureEvidence = Join-Path $temporaryRoot 'failure.json'
$timeoutEvidence = Join-Path $temporaryRoot 'timeout.json'
New-Item -ItemType Directory -Path $temporaryRoot | Out-Null

try {
    & $gatePath -CommandPath $pwshPath -ArgumentList @('-NoProfile', '-Command', 'exit 0') `
        -WorkingDirectory $temporaryRoot -EvidencePath $successEvidence `
        -ComponentPattern @("townlight-no-such-component-$([guid]::NewGuid().ToString('N')).exe") `
        -EventDrainMilliseconds 0 | Out-Null
    Assert-True (Test-Path -LiteralPath $successEvidence -PathType Leaf) `
        'a successful gate must write evidence'
    $success = Get-Content -LiteralPath $successEvidence -Raw | ConvertFrom-Json
    Assert-True ($success.schema_version -eq 1) 'evidence must declare schema version 1'
    Assert-True ($success.result -eq 'pass') 'a clean zero-exit process must pass'
    Assert-True ($success.exit_code -eq 0) 'success evidence must preserve the zero exit code'
    Assert-True (-not $success.timed_out) 'a clean process must not be marked timed out'
    Assert-True ($success.matching_event_count -eq 0) 'a clean process must have no matching events'
    Assert-True ($null -eq $success.command.arguments) 'evidence must not retain command arguments'
    Assert-True ($success.command.file -eq 'pwsh.exe') 'evidence may retain only the command file name'
    Assert-True ($success.command.launched_process_id -gt 0) `
        'evidence must identify the directly launched process'
    Assert-True ($success.event_correlation.mode -eq 'checkpoint-time-and-component-pattern') `
        'evidence must declare the event-correlation mode'
    Assert-True $success.event_correlation.requires_isolated_component_execution `
        'evidence must declare the isolated-execution requirement'
    Assert-True ($null -ne $success.event_correlation.observation_deadline_utc) `
        'evidence must preserve the event-observation deadline'

    $failureCaught = $false
    try {
        & $gatePath -CommandPath $pwshPath -ArgumentList @('-NoProfile', '-Command', 'exit 7') `
            -WorkingDirectory $temporaryRoot -EvidencePath $failureEvidence `
            -ComponentPattern @("townlight-no-such-component-$([guid]::NewGuid().ToString('N')).exe") `
            -EventDrainMilliseconds 0 | Out-Null
    } catch { $failureCaught = $true }
    Assert-True $failureCaught 'a nonzero process exit must fail the gate'
    $failure = Get-Content -LiteralPath $failureEvidence -Raw | ConvertFrom-Json
    Assert-True ($failure.result -eq 'fail') 'nonzero-exit evidence must fail'
    Assert-True ($failure.exit_code -eq 7) 'failure evidence must preserve the process exit code'

    $timeoutCaught = $false
    try {
        & $gatePath -CommandPath $pwshPath `
            -ArgumentList @('-NoProfile', '-Command', 'Start-Sleep -Seconds 10') `
            -WorkingDirectory $temporaryRoot -EvidencePath $timeoutEvidence `
            -ComponentPattern @("townlight-no-such-component-$([guid]::NewGuid().ToString('N')).exe") `
            -TimeoutSeconds 1 -EventDrainMilliseconds 0 | Out-Null
    } catch { $timeoutCaught = $true }
    Assert-True $timeoutCaught 'a timed-out process must fail the gate'
    $timeout = Get-Content -LiteralPath $timeoutEvidence -Raw | ConvertFrom-Json
    Assert-True ($timeout.result -eq 'fail') 'timeout evidence must fail'
    Assert-True $timeout.timed_out 'timeout evidence must identify the timeout'
} finally {
    Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output "Native crash gate contract passed ($assertionCount assertions)."
