$ErrorActionPreference = 'Stop'

$expected = @(
    'prove-installed-activation.ps1',
    'prove-installed-alert-delivery.ps1',
    'prove-installed-alert-realert.ps1',
    'prove-installed-alert-send-test.ps1',
    'prove-installed-automatic-materialization.ps1',
    'prove-installed-cg-images.ps1',
    'prove-installed-commissioning.ps1',
    'prove-installed-encrypted-srt-output.ps1',
    'prove-installed-live-schedule-catch-up.ps1',
    'prove-installed-live-takeover.ps1',
    'prove-installed-output-loudness.ps1',
    'prove-installed-output-profile.ps1',
    'prove-installed-required-output-stall.ps1',
    'prove-installed-rollback.ps1',
    'prove-installed-self-tests.ps1',
    'prove-installed-signed-update.ps1',
    'prove-installed-srt-output.ps1',
    'prove-installed-support-bundles.ps1',
    'prove-installed-system-resource-sampling.ps1'
)

$failures = [Collections.Generic.List[string]]::new()
$assertions = 0
function Assert-Contract([bool]$Condition, [string]$Message) {
    $script:assertions++
    if (-not $Condition) { $script:failures.Add($Message) }
}

$actual = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter 'prove-installed-*.ps1' | Sort-Object Name | ForEach-Object Name)
Assert-Contract (($actual -join '|') -eq (($expected | Sort-Object) -join '|')) 'The authenticated migration contract must enumerate the exact 19 installed proof harnesses.'

foreach ($name in $expected) {
    $path = Join-Path $PSScriptRoot $name
    $source = Get-Content -LiteralPath $path -Raw
    $tokens = $null
    $parseErrors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$parseErrors)
    Assert-Contract ($parseErrors.Count -eq 0) "$name must parse as PowerShell."
    Assert-Contract ($source -match '(?s)\[Parameter\(Mandatory\s*=\s*\$true\)\]\s*\[(?:Management\.Automation\.)?PSCredential\]\s*\$Credential') "$name must require a PSCredential."
    $topLevelCredential = @($ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'Credential' })
    Assert-Contract ($topLevelCredential.Count -eq 1 -and ([regex]::Matches($source, '\[(?:Management\.Automation\.)?PSCredential\]\s*\$Credential')).Count -eq 1) "$name must declare Credential exactly once, in its top-level param block."
    Assert-Contract ($source -match "installed-proof-api\.ps1") "$name must import the installed-proof API helper."
    Assert-Contract ($source -match 'New-TownLightProofSession') "$name must create an authenticated proof session."
    Assert-Contract ($source -match 'Invoke-TownLightProofApi') "$name must route protected API calls through the authenticated helper."
    Assert-Contract ($source -notmatch '(?im)^\s*(?:&\s*)?curl\.exe\b') "$name must not call curl directly."
    Assert-Contract ($source -notmatch '(?i)\bInvoke-WebRequest\b') "$name must not call Invoke-WebRequest directly."
    $directRest = @(Select-String -LiteralPath $path -Pattern 'Invoke-RestMethod' | Where-Object { $_.Line -notmatch '/health' })
    Assert-Contract ($directRest.Count -eq 0) "$name has a direct protected Invoke-RestMethod call."
    Assert-Contract ($source -notmatch '(?im)(?:^|[;{])\s*(?:operator_id|actor_id|approved_by|acknowledged_by|updated_by|requested_by|submitted_by|moderated_by|created_by)\s*=') "$name fabricates a browser/client authority field."
    Assert-Contract ($source -notmatch 'ConvertFrom-SecureString|Start-Job[^\r\n]*Credential|-File\s+\$script[^\r\n]*Credential') "$name must not serialize credentials into jobs, files, or child-process argv."
}

$restartReconnectMinimum = @{
    'prove-installed-activation.ps1' = 3
    'prove-installed-encrypted-srt-output.ps1' = 2
    'prove-installed-rollback.ps1' = 3
    'prove-installed-signed-update.ps1' = 2
}
foreach ($entry in $restartReconnectMinimum.GetEnumerator()) {
    $source = Get-Content -LiteralPath (Join-Path $PSScriptRoot $entry.Key) -Raw
    $count = ([regex]::Matches($source, '(?m)^\s*Connect-AfterServiceBoundary\s*$')).Count
    Assert-Contract ($count -ge $entry.Value) "$($entry.Key) must explicitly reconnect after each restart/update boundary before protected API use."
}
$commissioning = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'prove-installed-commissioning.ps1') -Raw
Assert-Contract (([regex]::Matches($commissioning, 'Connect-TownLightProofSession')).Count -ge 1) 'Commissioning must reconnect after its service restart before protected API use.'

Assert-Contract ($commissioning -notmatch '(?s)-File\s+\$script') 'Commissioning must not launch the CG proof as a credential-bearing child process.'
Assert-Contract ($commissioning -match '(?m)&\s*\$script\b' -and $commissioning -match '(?m)-Credential\s+\$Credential') 'Commissioning must run the CG proof in-process with its PSCredential object.'

if ($failures.Count -gt 0) {
    $summary = $failures | ForEach-Object { " - $_" }
    throw "Installed proof auth migration contract failed ($($failures.Count) failures / $assertions assertions):`n$($summary -join "`n")"
}

Write-Output "Installed proof auth migration contract passed ($assertions assertions across $($expected.Count) harnesses)."
