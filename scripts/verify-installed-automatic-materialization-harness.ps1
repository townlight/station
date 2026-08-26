[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repository = Split-Path $PSScriptRoot -Parent
$proofPath = Join-Path $PSScriptRoot 'prove-installed-automatic-materialization.ps1'
$documentationPath = Join-Path $repository 'docs\proof\2026-08-25-installed-automatic-materialization.md'

$assertions = 0
function Assert-Contract([bool]$Condition, [string]$Message) {
    $script:assertions++
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
}

Assert-Contract (Test-Path -LiteralPath $proofPath -PathType Leaf) `
    'the installed automatic-materialization proof script must exist.'
$source = Get-Content -LiteralPath $proofPath -Raw
$tokens = $null
$parseErrors = $null
[Management.Automation.Language.Parser]::ParseFile($proofPath, [ref]$tokens, [ref]$parseErrors) | Out-Null

Assert-Contract ($parseErrors.Count -eq 0) 'the installed automatic-materialization proof must parse as PowerShell.'
Assert-Contract ($source -match '\[Management\.Automation\.PSCredential\]\$Credential') `
    'the installed proof must require an in-memory PSCredential.'
Assert-Contract ($source -match 'installed-proof-api\.ps1') `
    'the installed proof must use the supported authenticated proof API helper.'
Assert-Contract ($source -match 'status\s*=\s*''blocked''') `
    'the proof must represent unavailable automatic evidence as blocked.'
Assert-Contract ($source -match 'missing_interface') `
    'the blocked result must name the missing public interface.'
Assert-Contract ($source -match 'schedule/materialization|materialization.*history|automatic.*materialization') `
    'the proof must target the automatic materialization evidence boundary.'
Assert-Contract ($source -match '/alerts') `
    'the proof must inspect the durable operator alert surface.'
Assert-Contract ($source -match '/schedule/items') `
    'the proof must inspect the durable generated schedule surface.'
Assert-Contract ($source -match 'schedule-materialization-failure' -and $source -match 'schedule-no-match') `
    'the proof must distinguish durable automatic-failure and no-match alert evidence.'
Assert-Contract ($source -match 'ValidateSet\(''Get''\)' -and $source -notmatch '-Method\s+[''\"]Post[''\"]') `
    'the automatic proof must not turn the manual materialization POST into evidence.'
Assert-Contract ($source -match 'candidate-manifest\.json' -and $source -match 'install-receipt\.json') `
    'the proof must authenticate the exact installed candidate and receipt.'
Assert-Contract ($source -match 'git .*status|status --porcelain') `
    'the proof must fail closed for a dirty source candidate.'
Assert-Contract ($source -match 'source_commit') `
    'the proof must bind every result to the source commit.'
Assert-Contract ($source -notmatch '(?i)ConvertTo-SecureString.*-AsPlainText|\$env:[A-Za-z0-9_]*(?:PASSWORD|CREDENTIAL|SECRET)|-Password\b') `
    'the proof must not introduce a plaintext credential path.'
Assert-Contract ($source -notmatch '(?i)\bStart-Process\b|\bStop-Process\b|\btaskkill(?:\.exe)?\b|\.Kill\s*\(') `
    'the proof must not start or terminate an external process.'
Assert-Contract ($source -match 'restart|idempotence|once.?per.?day') `
    'the proof must state the restart/idempotence observation boundary.'

Assert-Contract (Test-Path -LiteralPath $documentationPath -PathType Leaf) `
    'the installed automatic-materialization proof must have a dedicated evidence document.'
$documentation = Get-Content -LiteralPath $documentationPath -Raw
Assert-Contract ($documentation -match '\$credential\s*=\s*Get-Credential' -and
    $documentation -match '-Credential\s+\$credential') `
    'the evidence document must show the in-memory credential invocation contract.'
Assert-Contract ($documentation -match 'status:\s*blocked' -and
    $documentation -match 'materializations/history') `
    'the evidence document must preserve the blocked result and missing interface requirement.'

Write-Output "Installed automatic-materialization proof harness contract passed ($assertions assertions)."
