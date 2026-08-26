$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path $PSScriptRoot -Parent
$proofNames = @(
    'prove-installed-activation.ps1',
    'prove-installed-automatic-materialization.ps1',
    'prove-installed-alert-delivery.ps1',
    'prove-installed-alert-realert.ps1',
    'prove-installed-alert-send-test.ps1',
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
$scriptInvocations = 0
$documentedInvocations = 0
$documentationReferences = 0
$candidateConsumerNames = @(
    'prove-installed-automatic-materialization.ps1',
    'prove-installed-commissioning.ps1',
    'prove-installed-live-schedule-catch-up.ps1',
    'prove-installed-live-takeover.ps1',
    'prove-installed-output-loudness.ps1',
    'prove-installed-required-output-stall.ps1',
    'prove-installed-self-tests.ps1',
    'prove-installed-support-bundles.ps1',
    'prove-installed-system-resource-sampling.ps1',
    'prove-clean-install-in-owned-sandbox.ps1',
    'prove-caption-in-owned-sandbox.ps1',
    'run-clean-install.ps1',
    'run-caption-proof.ps1',
    'run-automatic-materialization.ps1'
)
function Assert-Contract([bool]$Condition, [string]$Message) {
    $script:assertions++
    if (-not $Condition) { $script:failures.Add($Message) }
}

$tracked = @(& git -C $repoRoot ls-files)
if ($LASTEXITCODE -ne 0) { throw 'Could not enumerate tracked installed-proof callers.' }
foreach ($proofName in $proofNames) {
    Assert-Contract (Test-Path -LiteralPath (Join-Path $PSScriptRoot $proofName) -PathType Leaf) "Installed proof target is missing: scripts/$proofName."
}

foreach ($consumerName in $candidateConsumerNames) {
    $consumerPath = if ($consumerName -like 'run-*') {
        Join-Path $repoRoot "harness\windows-sandbox\$consumerName"
    } else {
        Join-Path $PSScriptRoot $consumerName
    }
    Assert-Contract (Test-Path -LiteralPath $consumerPath -PathType Leaf) "Candidate consumer is missing: $consumerPath"
    $tokens = $null
    $parseErrors = $null
    $consumerAst = [Management.Automation.Language.Parser]::ParseFile($consumerPath, [ref]$tokens, [ref]$parseErrors)
    Assert-Contract ($parseErrors.Count -eq 0) "$consumerPath must parse as PowerShell."
    $candidateCalls = @($consumerAst.FindAll({
        param($node)
        $node -is [Management.Automation.Language.CommandAst] -and
            $node.GetCommandName() -eq 'Get-TownLightCandidateIdentity'
    }, $true))
    Assert-Contract ($candidateCalls.Count -ge 1) "$consumerPath must invoke the shared bounded candidate-pair verifier."
}

foreach ($relative in $tracked | Where-Object { $_ -like '*.ps1' }) {
    $name = Split-Path $relative -Leaf
    if ($name -in $proofNames) { continue }
    $path = Join-Path $repoRoot $relative
    $tokens = $null
    $parseErrors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$parseErrors)
    Assert-Contract ($parseErrors.Count -eq 0) "$relative must parse as PowerShell."
    foreach ($command in $ast.FindAll({
        param($node)
        if ($node -isnot [Management.Automation.Language.CommandAst]) { return $false }
        $commandName = $node.GetCommandName()
        $commandName -and (Split-Path $commandName -Leaf) -in $proofNames
    }, $true)) {
        $scriptInvocations++
        $credentialParameters = @($command.CommandElements | Where-Object {
            $_ -is [Management.Automation.Language.CommandParameterAst] -and $_.ParameterName -eq 'Credential'
        })
        Assert-Contract ($credentialParameters.Count -eq 1) "${relative}:$($command.Extent.StartLineNumber) invokes an installed proof without exactly one -Credential parameter."
    }
}

foreach ($relative in $tracked | Where-Object { $_ -like '*.md' }) {
    $source = Get-Content -LiteralPath (Join-Path $repoRoot $relative) -Raw
    $documentationReferences += ([regex]::Matches($source, 'prove-installed-[A-Za-z0-9-]+\.ps1')).Count
    foreach ($match in [regex]::Matches($source, '(?ms)^```(?:powershell|pwsh)\s*\r?\n(?<body>.*?)^```\s*$')) {
        $body = $match.Groups['body'].Value
        if ($body -notmatch 'prove-installed-[A-Za-z0-9-]+\.ps1') { continue }
        $documentedInvocations++
        Assert-Contract ($body -match '(?m)-Credential\s+\$[A-Za-z_][A-Za-z0-9_]*') "$relative has a documented installed-proof invocation without -Credential <PSCredential>."
        Assert-Contract ($body -match '(?m)^\s*\$[A-Za-z_][A-Za-z0-9_]*\s*=\s*Get-Credential\b') "$relative must acquire the documented installed-proof credential interactively in memory."
        Assert-Contract ($body -notmatch '(?i)ConvertTo-SecureString.*-AsPlainText|\$env:[A-Za-z0-9_]*(?:PASSWORD|CREDENTIAL|SECRET)|-Password\b') "$relative documents a forbidden plaintext credential path."
    }
}

$commissioningSource = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'prove-installed-commissioning.ps1') -Raw
Assert-Contract ($commissioningSource -match '(?ms)&\s*\$script\s+-Credential\s+\$Credential\b') 'Commissioning must pass its in-memory PSCredential to the in-process CG proof.'
$automaticProofSource = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'prove-installed-automatic-materialization.ps1') -Raw
Assert-Contract ($automaticProofSource -match 'EnableServiceRestartProof' -and
    $automaticProofSource -match 'fixture_not_ready' -and
    $automaticProofSource -match '& \$trustedScPath stop TownLightStation' -and
    $automaticProofSource -match '& \$trustedScPath start TownLightStation') `
    'The automatic-materialization installed proof must keep its explicit fixture gate and exact scoped SCM boundary.'
Assert-Contract ($documentedInvocations -gt 0) 'At least one operator-facing installed-proof invocation must remain under the credential contract.'

& (Join-Path $PSScriptRoot 'verify-candidate-pair-verifier.ps1')

if ($failures.Count -gt 0) {
    $summary = $failures | ForEach-Object { " - $_" }
    throw "Installed proof caller contract failed ($($failures.Count) failures / $assertions assertions):`n$($summary -join "`n")"
}

Write-Output "Installed proof caller contract passed ($assertions assertions; $scriptInvocations non-proof script callers; $documentedInvocations documented invocations; $documentationReferences documentation references)."
