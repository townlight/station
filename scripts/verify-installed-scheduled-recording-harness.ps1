[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$proofPath = Join-Path $PSScriptRoot 'prove-installed-scheduled-recording.ps1'
$documentationPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'docs\proof\2026-08-27-installed-scheduled-recording.md'
$assertions = 0
function Assert-Contract([bool]$Condition, [string]$Message) {
    $script:assertions++
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
}

Assert-Contract (Test-Path -LiteralPath $proofPath -PathType Leaf) 'scheduled-recording installed proof must exist.'
$source = Get-Content -LiteralPath $proofPath -Raw
$tokens = $null
$parseErrors = $null
[Management.Automation.Language.Parser]::ParseFile($proofPath, [ref]$tokens, [ref]$parseErrors) | Out-Null
Assert-Contract ($parseErrors.Count -eq 0) 'scheduled-recording proof must parse as PowerShell.'
Assert-Contract ($source -match '\[Management\.Automation\.PSCredential\]\$Credential' -and
    $source -match 'installed-proof-api\.ps1' -and $source -match 'New-TownLightProofSession' -and
    $source -match 'Invoke-TownLightProofApi') 'proof must use the in-memory authenticated API session.'
Assert-Contract ($source -match 'candidate-pair-verifier\.ps1' -and
    $source -match 'Get-TownLightCandidateIdentity' -and $source -match 'install-receipt\.json') `
    'proof must bind the installed result to the exact candidate pair and receipt.'
Assert-Contract ($source -match 'POST.*assets/ingest|''Post'' ''/assets/ingest''' -and
    $source -match 'POST.*scheduled-recordings|''Post'' ''/scheduled-recordings''' -and
    $source -match 'scheduled-recordings/\$recordingId/arm') `
    'proof must exercise authenticated ingest, scheduled-recording creation, and arm workflow.'
Assert-Contract ($source -match '/schedule/items' -and $source -match '/schedule/prepare' -and
    $source -match '/schedule/commit' -and $source -match '/schedule/commits/\$reportId') `
    'proof must commit the authoritative schedule before recording.'
Assert-Contract ($source -match 'start_capture' -and $source -match 'begin_finalization' -and
    $source -match 'finalize_success' -and $source -match 'captured_asset_hash') `
    'proof must require the complete durable capture/finalization transition chain and hash.'
Assert-Contract ($source -match '/assets/\$assetHash' -and $source -match '/catalog/assets/\$assetHash' -and
    $source -match "editorial_state -cne 'draft'") `
    'proof must verify the finalized media asset and generated Draft catalog record.'
Assert-Contract ($source -match 'proof-owned channel' -or $source -match 'proofChannel') `
    'proof must isolate its channel identity and disable it during cleanup.'
Assert-Contract ($source -notmatch '(?i)\b(Start-Process|Restart-Service|Stop-Process|taskkill|sc\.exe)\b') `
    'proof must not launch or control processes/services.'
Assert-Contract ($source -notmatch '(?i)\b(Invoke-WebRequest|curl\.exe)\b' -and
    $source -notmatch '(?i)ConvertTo-SecureString.*-AsPlainText|\$env:[A-Za-z0-9_]*(?:PASSWORD|CREDENTIAL|SECRET)|-Password\b') `
    'proof must not bypass the authenticated helper or serialize credentials.'
Assert-Contract ($source -notmatch '(?i)\b(SQLiteConnection|sqlite|station\.db|database\.write|ReadAllText.*ProgramData)\b') `
    'proof must not read or write the station database directly.'
Assert-Contract (Test-Path -LiteralPath $documentationPath -PathType Leaf) 'proof boundary documentation must exist.'
$documentation = Get-Content -LiteralPath $documentationPath -Raw
Assert-Contract ($documentation -match '\$credential\s*=\s*Get-Credential' -and
    $documentation -match '-Credential\s+\$credential' -and
    $documentation -match 'does not claim field proof') `
    'documentation must show in-memory credential invocation and truthful field boundary.'

Write-Output "Installed scheduled-recording proof harness passed ($assertions assertions; no machine mutation)."
