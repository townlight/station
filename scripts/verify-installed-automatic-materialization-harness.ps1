[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repository = Split-Path $PSScriptRoot -Parent
$proofPath = Join-Path $PSScriptRoot 'prove-installed-automatic-materialization.ps1'
$statePath = Join-Path $PSScriptRoot 'installed-automatic-materialization-proof-state.ps1'
$stateHarnessPath = Join-Path $PSScriptRoot 'verify-installed-automatic-materialization-state.ps1'
$runtimeHelperPath = Join-Path $PSScriptRoot 'installed-private-runtime-verifier.ps1'
$runtimeHarnessPath = Join-Path $PSScriptRoot 'verify-installed-private-runtime-verifier.ps1'
$installerVerifierPath = Join-Path $repository 'installer\verify-installed.ps1'
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
Assert-Contract ($source -match 'installed-automatic-materialization-proof-state\.ps1') `
    'the proof must use the injectable automatic-proof state machine.'
Assert-Contract ($source -match 'EnableServiceRestartProof') `
    'the service boundary must require an explicit proof flag.'
Assert-Contract ($source -match 'restart_proof_requires_elevated_session') `
    'the service boundary must reject a non-elevated session before Windows Service Control Manager control.'
Assert-Contract ($source -match 'Global\\TownLightStationAutomaticMaterializationInstalledProof') `
    'the service boundary must serialize on its named proof mutex.'
Assert-Contract ($source -match '/scheduling/channels/\$ChannelId/materializations/history\?limit=128') `
    'the proof must read the bounded authenticated materialization history contract.'
Assert-Contract ($source -match '/channels' -and $source -match '/scheduling/recurring-rules' -and
    $source -match '/schedule/items' -and $source -match '/alerts') `
    'the proof must read exact authenticated channel, rule, schedule, and alert surfaces.'
Assert-Contract ($source -match '00000000-0000-4000-8000-000000000022') `
    'the proof must require the fixed automatic materialization actor identity.'
Assert-Contract ($source -match 'request_sha256' -and $source -match 'result_sha256' -and
    $source -match 'generated_draft_ids' -and $source -match 'generated_draft_bindings') `
    'the proof must bind the automatic run to stable hashes and occurrence/Draft identities.'
Assert-Contract ($source -match 'Get-TownLightAutomaticMaterializationProofUtcDay' -and
    $source -match 'utc_day_rolled_over_during_restart_proof') `
    'the proof must floor UTC days and fail closed across a proof-window rollover.'
Assert-Contract ($source -match 'proof_channel_not_enabled') `
    'the proof must require the current channel to remain enabled.'
Assert-Contract ($source -match 'fixture_not_ready' -and $source -match 'Stop-ProofBlocked') `
    'missing automatic Draft evidence must return blocked fixture-not-ready before Windows Service Control Manager control.'
Assert-Contract ($source -match 'duplicate_current_utc_day_automatic_run' -and
    $source -match 'duplicate_generated_draft_identifier' -and $source -match 'post_restart_automatic_run_duplicate_or_drift') `
    'duplicate or newer post-restart automatic evidence must fail closed.'
Assert-Contract ($source -match 'Get-TownLightAutomaticMaterializationProofServiceIdentity' -and
    (Get-Content -LiteralPath $statePath -Raw) -match 'StartName' -and
    (Get-Content -LiteralPath $statePath -Raw) -match 'StartMode' -and
    (Get-Content -LiteralPath $statePath -Raw) -match 'PathName') `
    'the proof must check exact Win32 service identity, path, account, and start mode.'
Assert-Contract ($source -match '& \$trustedScPath stop TownLightStation' -and
    $source -match '& \$trustedScPath start TownLightStation') `
    'the proof may control only TownLightStation through the exact System32 sc.exe stop/start calls.'
Assert-Contract ($source -match 'PostRestartObservationSeconds' -and $source -match 'ValidateRange\(61, 900\)') `
    'the proof must require a bounded observation window greater than sixty seconds.'
Assert-Contract ($source -match 'Connect-AfterStationRestart' -and $source -match 'New-TownLightProofSession') `
    'the proof must establish a new authenticated session after the station restart.'
Assert-Contract ($source -match 'authenticated_account_changed_after_restart') `
    'the proof must fail if post-restart authentication resolves to a different account.'
Assert-Contract ($source -match 'Try-RecoverExactStationStart' -and $source -match 'issue_exact_start_recovery') `
    'recovery must be mediated by the state machine and exact stopped-service identity.'
Assert-Contract ($source -match 'stop_issued' -and $source -match 'stop_outcome' -and
    $source -match 'start_issued' -and $source -match 'start_outcome') `
    'the receipt must distinguish restart entry from each exact Service Control Manager issuance and outcome.'
$stopInvocationIndex = $source.IndexOf('$stop = Invoke-ExactStationSc -Verb stop', [StringComparison]::Ordinal)
$attemptedIndex = $source.IndexOf('$result.restart_boundary.attempted = $true', [StringComparison]::Ordinal)
Assert-Contract ($stopInvocationIndex -ge 0 -and $attemptedIndex -gt $stopInvocationIndex) `
    'restart attempted may become true only after the exact stop invocation returns.'
Assert-Contract ($source -match '-ScmOutcome \$stopOutcome' -and
    $source -notmatch '-ScmOutcome success -StartAlreadyIssued \$false') `
    'recovery must consume the actual recorded stop outcome rather than assume success.'
Assert-Contract ($source -match 'current_schedule_sha256_by_binding') `
    'the proof must compare canonical current ScheduleItem evidence by occurrence/Draft binding.'
Assert-Contract ($source -match 'SpecialFolder\]::System' -and
    $source -match 'SpecialFolder\]::ProgramFiles' -and
    $source -match 'SpecialFolder\]::CommonApplicationData') `
    'trusted executable and product roots must come from canonical Windows known folders.'
Assert-Contract ($source -match 'known_folder_environment_drift' -and
    $source -match 'Test-CanonicalPathEqual -Left \$trustedScPath') `
    'environment drift and a non-canonical Service Control Manager executable path must fail closed.'
Assert-Contract ($source -notmatch '\$env:SystemRoot\\System32\\sc\.exe') `
    'the Service Control Manager executable path must not trust an environment-controlled SystemRoot.'
Assert-Contract ($source -match '\$script:apiCoordinates\.Origin' -and
    $source -notmatch '\$BaseUri\.TrimEnd\(''\/''\) \+ ''\/health''') `
    'health must use the proof helper canonical origin, including when BaseUri ends in /api/v1.'
Assert-Contract ($source -match 'binding_claim = ''none') `
    'the proof must preserve alert observations without claiming alert-to-run binding.'
Assert-Contract ($source -match 'candidate-manifest\.json' -and $source -match 'install-receipt\.json' -and
    $source -match 'runtime-manifest\.json' -and $source -match 'active-slot\.json') `
    'the proof must fail closed on candidate, receipt, runtime, and active-slot mismatch.'
Assert-Contract (Test-Path -LiteralPath $runtimeHelperPath -PathType Leaf) `
    'the shared installed private-runtime verifier must exist.'
$runtimeHelperSource = if (Test-Path -LiteralPath $runtimeHelperPath -PathType Leaf) {
    Get-Content -LiteralPath $runtimeHelperPath -Raw
} else { '' }
$installerVerifierSource = Get-Content -LiteralPath $installerVerifierPath -Raw
Assert-Contract ($source -match 'installed-private-runtime-verifier\.ps1' -and
    $installerVerifierSource -match 'installed-private-runtime-verifier\.ps1') `
    'both installed proof and installer verifier must use the shared private-runtime boundary.'
Assert-Contract ($runtimeHelperSource -match 'pendingDirectories' -and
    $runtimeHelperSource -match 'ReparsePoint' -and $runtimeHelperSource -match 'expectedRuntimeFiles' -and
    $runtimeHelperSource -match 'actualRuntimeFiles' -and $runtimeHelperSource -match 'size_bytes' -and
    $runtimeHelperSource -match 'Get-FileHash') `
    'the shared verifier must verify the bounded private-runtime file set, sizes, hashes, and reparse safety.'
Assert-Contract ($runtimeHelperSource -notmatch '(?s)Get-ChildItem[^\r\n]*-Recurse') `
    'the shared verifier must inspect each immediate child before descending and must never use recursive enumeration.'
Assert-Contract ($installerVerifierSource -notmatch '(?s)Get-ChildItem[^\r\n]*\$runtime[^\r\n]*-Recurse') `
    'the installer verifier must not retain its recursive-before-reparse runtime traversal.'
Assert-Contract (Test-Path -LiteralPath $runtimeHarnessPath -PathType Leaf) `
    'the shared private-runtime verifier must have a safe fixture/static harness.'
& $runtimeHarnessPath | Out-Null
Assert-Contract ($source -match 'git .*status|status --porcelain') `
    'the proof must fail closed for a dirty source candidate.'
Assert-Contract ($source -match 'source_commit') `
    'the proof must bind every result to the exact source commit.'
Assert-Contract ($source -notmatch '(?i)ConvertTo-SecureString.*-AsPlainText|\$env:[A-Za-z0-9_]*(?:PASSWORD|CREDENTIAL|SECRET)|-Password\b') `
    'the proof must not introduce a plaintext credential path.'
Assert-Contract ($source -notmatch '(?i)\bStart-Process\b|\bRestart-Service\b|\bStop-Process\b|\btaskkill(?:\.exe)?\b|\.Kill\s*\(|restart HTTP endpoint') `
    'the proof must not use process control, force restart, taskkill, or a restart HTTP endpoint.'
Assert-Contract ($source -notmatch '(?i)\bsc\.exe\b.*(?:config|delete|failure)|TownLightStationUpdater\s+(?:start|stop)') `
    'the proof must not reconfigure or control the updater service.'
Assert-Contract ($source -match 'ValidateSet\(\x27Get\x27\)' -and $source -notmatch '-Method\s+[\x27\"](?:Post|Put|Patch|Delete)[\x27\"]') `
    'the proof must not turn an unsafe scheduling action into automatic evidence.'

Assert-Contract (Test-Path -LiteralPath $stateHarnessPath -PathType Leaf) `
    'the static automatic-proof state-machine harness must exist.'
& $stateHarnessPath | Out-Null

Assert-Contract (Test-Path -LiteralPath $documentationPath -PathType Leaf) `
    'the installed automatic-materialization proof must have a dedicated evidence document.'
$documentation = Get-Content -LiteralPath $documentationPath -Raw
Assert-Contract ($documentation -match '\$credential\s*=\s*Get-Credential' -and
    $documentation -match '-Credential\s+\$credential' -and $documentation -match 'EnableServiceRestartProof') `
    'the evidence document must show the in-memory credential and explicit restart-proof invocation contract.'
Assert-Contract ($documentation -match 'fixture-not-ready' -and $documentation -match 'materializations/history' -and
    $documentation -match 'does not claim uninterrupted output' -and $documentation -match 'does not claim field proof') `
    'the evidence document must state the fixture boundary and non-claims truthfully.'

Write-Output "Installed automatic-materialization proof harness contract passed ($assertions assertions; no Windows Service Control Manager control or mutation)."
