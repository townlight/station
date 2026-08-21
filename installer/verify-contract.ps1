$ErrorActionPreference = 'Stop'

$installer = Join-Path $PSScriptRoot 'TownLightStation.iss'
$source = Get-Content -LiteralPath $installer -Raw
$requirements = [ordered]@{
    'requires administrative installation' = 'PrivilegesRequired=admin'
    'targets supported 64-bit Windows' = 'ArchitecturesAllowed=x64compatible'
    'carries the official media runtime' = 'ExtractTemporaryFile'
    'registers the native station service' = 'create TownLightStation'
    'sets automatic delayed service start' = 'start= delayed-auto'
    'configures service recovery' = 'failure TownLightStation'
    'starts the station service' = 'start TownLightStation'
    'health-gates installation success' = 'WinHttp.WinHttpRequest.5.1'
    'rolls back a failed service activation' = 'RollbackService'
    'removes the service on uninstall' = 'delete TownLightStation'
    'records the immutable source commit' = 'source_commit'
    'preserves station data on uninstall' = 'Station data is deliberately preserved'
}

$missing = @()
foreach ($requirement in $requirements.GetEnumerator()) {
    if (-not $source.Contains($requirement.Value)) {
        $missing += $requirement.Key
    }
}

if ($missing.Count -gt 0) {
    throw "Installer contract is incomplete: $($missing -join '; ')"
}

Write-Output "Installer contract passed ($($requirements.Count) requirements)."
