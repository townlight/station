param(
    [string]$Proof = (Join-Path $PSScriptRoot 'prove-authenticated-station-boundary.ps1'),
    [string]$ScanLibrary = (Join-Path $PSScriptRoot 'authenticated-station-boundary-scan.ps1'),
    [switch]$SkipAdversarial
)

$ErrorActionPreference = 'Stop'
$assertions = 0

function Assert-Contract {
    param([bool]$Condition, [string]$Message)
    $script:assertions++
    if (-not $Condition) { throw "Authenticated station boundary contract failed: $Message" }
}

Assert-Contract (Test-Path -LiteralPath $Proof -PathType Leaf) 'the dedicated proof script is missing'
Assert-Contract (Test-Path -LiteralPath $ScanLibrary -PathType Leaf) 'the bounded scan library is missing'
Assert-Contract ((Split-Path $Proof -Leaf) -notlike 'prove-installed-*') 'the proof name must not enter the exact installed-proof migration set'

$source = (Get-Content -LiteralPath $Proof -Raw).Replace("`r`n", "`n")
$scanSource = (Get-Content -LiteralPath $ScanLibrary -Raw).Replace("`r`n", "`n")
$requirements = [ordered]@{
    'requires an in-memory PSCredential' = '(?s)\[Parameter\(Mandatory\s*=\s*\$true\)\]\s*\[Management\.Automation\.PSCredential\]\s*\$Credential'
    'requires an elevated administrator' = 'IsInRole\(\[Security\.Principal\.WindowsBuiltInRole\]::Administrator\)'
    'loads the committed authenticated helper' = "installed-proof-api\.ps1"
    'confirms the authenticated session endpoint' = "Invoke-TownLightProofApi[^\r\n]*-Path '/auth/session'"
    'creates a distinct empty cookie jar' = 'WebRequestSession\]::new\(\)'
    'requires an empty jar 401' = 'empty cookie jar did not receive HTTP 401'
    'uses the low-impact self-test schedule mutation' = "-Method Put -Path '/self-test-schedules/daily'"
    'asserts response attribution' = 'mutationResponse\.updated_by'
    'asserts durable attribution' = 'durableMutation\.updated_by'
    'captures the session cookie in memory' = "Get-AuthenticationCookieValue[^\r\n]*'townlight_session_v1'"
    'captures the CSRF cookie in memory' = "Get-AuthenticationCookieValue[^\r\n]*'townlight_csrf_v1'"
    'restarts the station service' = "Restart-Service -Name 'TownLightStation' -Force"
    'bounds the restart operation' = 'Wait-Job -Job \$restartJob -Timeout 30'
    'health-gates the restarted service' = 'Wait-ForTownLightHealth'
    'parses nested orchestration readiness' = 'document\.orchestration\.status -eq ''ready'''
    'requires stale-session HTTP 401' = 'old session remained valid across the service restart'
    'asserts helper identity invalidation' = '\$null -eq \$apiSession\.Identity'
    'asserts helper cookie-jar replacement' = 'ReferenceEquals\(\$oldJar, \$apiSession\.WebSession\)'
    'explicitly reconnects after invalidation' = 'Connect-TownLightProofSession -Session \$apiSession -Credential \$Credential'
    'confirms account persistence after reconnect' = 'sessionAfter\.account_id'
    'compares enabled channel configuration' = 'enabledChannelIdsAfter'
    'compares runtime recovery when configured' = 'runtime_recovered'
    'uses the canonical ProgramData root' = "GetFolderPath\(\[Environment\+SpecialFolder\]::CommonApplicationData\)"
    'scans the exact database and sidecars' = "'station.db', 'station.db-wal', 'station.db-shm'"
    'scans only the bounded logs root' = 'Join-Path \$DataDirectory ''logs'''
    'scans only bounded root receipts' = "Get-BoundedFiles[^\r\n]*-Filter '\*\.json'"
    'scans only the bounded support root' = 'Join-Path \$DataDirectory ''support-bundles'''
    'creates a fresh authenticated support bundle' = "-Method Post -Path '/support-bundles'"
    'validates the fresh support identity' = 'freshSupport\.bundle_id'
    'validates the managed support path' = 'freshSupportPath\.StartsWith\(\$supportRoot'
    'binds the exact fresh bundle into the scan' = '-RequiredSupportBundlePath \$freshSupportPath'
    'requires the exact fresh support file' = '\$requiredSupportFound'
    'requires expanded fresh support entries' = '\$requiredSupportExpanded'
    'requires the installed receipt in the scan' = 'requiredReceiptPath'
    'scans ZIP entries rather than only compressed bytes' = 'System\.IO\.Compression\.ZipArchive'
    'enforces a scan deadline' = '\$scanDeadlineUtc'
    'emits only a sanitized evidence object' = 'plaintext_authentication_secrets_absent = \$true'
}
foreach ($requirement in $requirements.GetEnumerator()) {
    Assert-Contract ($source -match $requirement.Value) $requirement.Key
}

$scanRequirements = [ordered]@{
    'builds UTF-8 patterns' = '\[Text\.Encoding\]::UTF8\.GetBytes'
    'builds UTF-16LE patterns' = '\[Text\.Encoding\]::Unicode\.GetBytes'
    'builds UTF-16BE patterns' = '\[Text\.Encoding\]::BigEndianUnicode\.GetBytes'
    'uses a fixed-size streaming buffer' = '\[ValidateRange\(16, 65536\)\]'
    'checks chunk overlap' = '\$carryLength'
    'fails closed on byte bounds' = 'authentication artifact exceeded its byte scan bound'
    'fails closed on scan deadline' = 'authentication artifact scan exceeded its time bound'
}
foreach ($requirement in $scanRequirements.GetEnumerator()) {
    Assert-Contract ($scanSource -match $requirement.Value) $requirement.Key
}

$forbidden = [ordered]@{
    'credential serialization' = '(?i)(ConvertFrom-SecureString|Export-Clixml|Import-Clixml)'
    'secret output' = '(?im)^\s*(Write-(Output|Host|Verbose|Debug|Information)|echo)\b[^\r\n]*(password|sessionToken|csrf|sessionSecret)'
    'secret file writes' = '(?im)^\s*(Set-Content|Add-Content|Out-File|Export-Clixml)[^\r\n]*(password|sessionToken|csrf|sessionSecret)'
    'secret process arguments' = '(?im)^\s*(Start-Process|&)[^\r\n]*(password|sessionToken|csrf|sessionSecret)'
    'secret environment assignment' = '(?im)\$env:[A-Za-z_][A-Za-z0-9_]*\s*=\s*[^\r\n]*(password|sessionToken|csrf|sessionSecret)'
    'unbounded ProgramData recursion' = '(?im)Get-ChildItem[^\r\n]*\$dataDirectory[^\r\n]*-Recurse'
    'caller-selected evidence file' = '(?i)ResultPath'
}
foreach ($rule in $forbidden.GetEnumerator()) {
    Assert-Contract (-not ($source -match $rule.Value)) $rule.Key
}

$identityStart = $source.IndexOf('# CONTRACT: identity-free mutation body begins')
$identityEnd = $source.IndexOf('# CONTRACT: identity-free mutation body ends')
Assert-Contract ($identityStart -ge 0 -and $identityEnd -gt $identityStart) 'identity-free mutation boundary is explicit'
$identityBody = $source.Substring($identityStart, $identityEnd - $identityStart)
Assert-Contract (-not ($identityBody -match '(?i)(updated_by|account_id|operator|username)')) 'the mutation body must not carry caller identity'

$orderedMarkers = @(
    'New-TownLightProofSession',
    "-Path '/auth/session'",
    'empty cookie jar did not receive HTTP 401',
    "-Method Put -Path '/self-test-schedules/daily'",
    "Get-AuthenticationCookieValue -Session `$apiSession -Name 'townlight_session_v1'",
    "Restart-Service -Name 'TownLightStation' -Force",
    'Wait-ForTownLightHealth',
    'old session remained valid across the service restart',
    'Connect-TownLightProofSession -Session $apiSession -Credential $Credential',
    'sessionAfter.account_id',
    "-Method Post -Path '/support-bundles'",
    'Invoke-BoundedAuthenticationArtifactScan'
)
$last = -1
foreach ($marker in $orderedMarkers) {
    $next = $source.IndexOf($marker, $last + 1, [StringComparison]::Ordinal)
    Assert-Contract ($next -gt $last) "required operation is absent or out of order: $marker"
    $last = $next
}

. $ScanLibrary
$canary = 'BoundaryParserCanary-42'
$patterns = New-TownLightSecretPatterns -Secrets @($canary)
foreach ($encoding in @([Text.Encoding]::UTF8, [Text.Encoding]::Unicode, [Text.Encoding]::BigEndianUnicode)) {
    $prefix = [byte[]](1..15)
    $payload = $prefix + $encoding.GetBytes($canary) + [byte[]](16..24)
    $stream = [IO.MemoryStream]::new($payload, $false)
    try {
        Assert-Contract (Test-TownLightSecretInStream -Stream $stream -Patterns $patterns -MaxBytes 4096 -DeadlineUtc ([DateTime]::UtcNow.AddSeconds(5)) -ChunkSize 16) "stream parser missed $($encoding.WebName) across a chunk boundary"
    } finally { $stream.Dispose() }
}
$cleanStream = [IO.MemoryStream]::new([Text.Encoding]::UTF8.GetBytes('bounded clean artifact'), $false)
try {
    Assert-Contract (-not (Test-TownLightSecretInStream -Stream $cleanStream -Patterns $patterns -MaxBytes 4096 -DeadlineUtc ([DateTime]::UtcNow.AddSeconds(5)) -ChunkSize 16)) 'stream parser reported a false positive'
} finally { $cleanStream.Dispose() }
$oversizedStream = [IO.MemoryStream]::new([byte[]](1..32), $false)
try {
    $boundFailure = ''
    try { Test-TownLightSecretInStream -Stream $oversizedStream -Patterns $patterns -MaxBytes 16 -DeadlineUtc ([DateTime]::UtcNow.AddSeconds(5)) -ChunkSize 16 | Out-Null }
    catch { $boundFailure = [string]$_.Exception.Message }
    Assert-Contract ($boundFailure -eq 'An authentication artifact exceeded its byte scan bound.') 'stream parser did not fail closed on its byte bound'
} finally { $oversizedStream.Dispose() }
$expiredStream = [IO.MemoryStream]::new([Text.Encoding]::UTF8.GetBytes('bounded clean artifact'), $false)
try {
    $deadlineFailure = ''
    try { Test-TownLightSecretInStream -Stream $expiredStream -Patterns $patterns -MaxBytes 4096 -DeadlineUtc ([DateTime]::UtcNow.AddSeconds(-1)) -ChunkSize 16 | Out-Null }
    catch { $deadlineFailure = [string]$_.Exception.Message }
    Assert-Contract ($deadlineFailure -eq 'The authentication artifact scan exceeded its time bound.') 'stream parser did not fail closed on its deadline'
} finally { $expiredStream.Dispose() }

if (-not $SkipAdversarial) {
    $temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ('townlight-auth-boundary-contract-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
    try {
        $mutations = [ordered]@{
            'optional credential' = @("[Parameter(Mandatory = `$true)]`n    [Management.Automation.PSCredential]`$Credential", '[Management.Automation.PSCredential]$Credential')
            'secret output' = @('$sessionTokenBefore = Get-AuthenticationCookieValue', "Write-Output `$sessionTokenBefore`n`$sessionTokenBefore = Get-AuthenticationCookieValue")
            'caller identity injection' = @('        local_minute = [int]$dailySchedule.local_minute', "        local_minute = [int]`$dailySchedule.local_minute`n        updated_by = `$accountId")
            'restart removed' = @("Restart-Service -Name 'TownLightStation' -Force", "Get-Service -Name 'TownLightStation'")
            'stale-session assertion removed' = @("throw 'The old session remained valid across the service restart.'", "`$null = `$staleFailure")
            'unbounded ProgramData scan' = @('$databaseFiles = @(', "Get-ChildItem -LiteralPath `$dataDirectory -Recurse`n    `$databaseFiles = @(")
            'administrator gate removed' = @('$windowsPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)', '$true')
            'fresh support generation removed' = @("`$freshSupport = Invoke-TownLightProofApi -Session `$apiSession -Method Post -Path '/support-bundles'", '$freshSupport = $null')
            'fresh support scan binding redirected' = @('-RequiredSupportBundlePath $freshSupportPath', '-RequiredSupportBundlePath (Join-Path $dataDirectory ''unrelated.zip'')')
        }
        foreach ($mutation in $mutations.GetEnumerator()) {
            $old, $new = $mutation.Value
            Assert-Contract ($source.Contains($old)) "adversarial fixture drifted: $($mutation.Key)"
            $candidate = Join-Path $temporaryRoot (([Guid]::NewGuid().ToString('N')) + '.ps1')
            [IO.File]::WriteAllText($candidate, $source.Replace($old, $new), [Text.UTF8Encoding]::new($false))
            & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $PSCommandPath -Proof $candidate -ScanLibrary $ScanLibrary -SkipAdversarial *> $null
            Assert-Contract ($LASTEXITCODE -ne 0) "verifier accepted hostile mutation: $($mutation.Key)"
        }
    }
    finally {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Output "Authenticated station boundary static contract passed ($assertions assertions)."
