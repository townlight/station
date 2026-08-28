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
    'prove-installed-scheduled-recording.ps1',
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
    'prove-installed-scheduled-recording.ps1',
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
$decoderPinPath = Join-Path $PSScriptRoot 'installed-caption-decoder-pin.ps1'
Assert-Contract (Test-Path -LiteralPath $decoderPinPath -PathType Leaf) 'Commissioning caption decoder pin helper is missing.'
$decoderPinSource = Get-Content -LiteralPath $decoderPinPath -Raw
Assert-Contract ($commissioningSource -match 'installed-caption-decoder-pin\.ps1' -and
    $commissioningSource -match 'caption_decoder_sha256' -and $commissioningSource -match 'caption_decoder_size_bytes' -and
    $commissioningSource -match 'Open-TownLightPinnedCaptionDecoder' -and
    $commissioningSource -match '-DecoderPath\s+\$captionDecoderPin\.path' -and
    $commissioningSource -match '-ExpectedDecoderSha256\s+\$captionDecoderPin\.sha256' -and
    $commissioningSource -match '-ExpectedDecoderSizeBytes\s+\(\[int64\]\$captionDecoderPin\.size_bytes\)' -and
    $commissioningSource -match '-PinnedDecoderStream\s+\$captionDecoderPin\.stream' -and
    $commissioningSource -match 'PinnedDecoderDirectoryHandles' -and
    $commissioningSource -match '\$captionDecoderPin\.stream\.Dispose\(\)' -and
    $commissioningSource -match '(?s)directory_handles.*Dispose' -and
    $decoderPinSource -match 'runtime\\caption-decoder\\ffmpeg\.exe' -and
    $decoderPinSource -match '\[IO\.File\]::Open\(.+\[IO\.FileShare\]::Read' -and
    $decoderPinSource -match 'FILE_FLAG_BACKUP_SEMANTICS \| FILE_FLAG_OPEN_REPARSE_POINT' -and
    $decoderPinSource -match 'Open-TownLightNoDeleteDirectoryHandle') `
    'Commissioning must execute the manifest-bound private caption decoder by exact path, hash, and size.'
$cgProofSource = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'prove-installed-cg-images.ps1') -Raw
Assert-Contract ($cgProofSource -match '\[string\]\$DecoderPath' -and $cgProofSource -match '&\s+\$DecoderExecutable' -and
    $cgProofSource -match '\[IO\.FileStream\]\$PinnedDecoderStream' -and
    $cgProofSource -match 'Assert-TownLightPinnedCaptionDecoder' -and
    $cgProofSource -notmatch '&\s+ffmpeg\.exe') `
    'CG pixel assertions must execute an explicit verified decoder when commissioning supplies one.'
function Get-CgZonesCountUnderStrictMode([scriptblock]$Action) {
    Set-StrictMode -Version Latest
    $existingZones = @(& $Action)
    $existingZones.Count
}
$oneCgZone = [pscustomobject]@{ zone_id = 'one' }
Assert-Contract ((Get-CgZonesCountUnderStrictMode {}) -eq 0 -and
    (Get-CgZonesCountUnderStrictMode { $oneCgZone }) -eq 1 -and
    (Get-CgZonesCountUnderStrictMode { $oneCgZone; [pscustomobject]@{ zone_id = 'two' } }) -eq 2 -and
    $cgProofSource -match '\$existingZones\s*=\s*@\(Invoke-Json' -and $cgProofSource -match '\$existingZones\.Count -gt 0') `
    'CG proof must safely count empty, singleton, and nonempty zones responses under strict mode.'
$cgCaptureTokens = $null
$cgCaptureParseErrors = $null
$cgCaptureAst = [Management.Automation.Language.Parser]::ParseInput($cgProofSource, [ref]$cgCaptureTokens, [ref]$cgCaptureParseErrors)
$cgCaptureFunction = @($cgCaptureAst.FindAll({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Capture-Udp'
}, $true))
$cgCaptureSource = if ($cgCaptureFunction.Count -eq 1) { $cgCaptureFunction[0].Extent.Text } else { '' }
Assert-Contract ($cgCaptureParseErrors.Count -eq 0 -and $cgCaptureFunction.Count -eq 1 -and
    $cgCaptureSource -match '\[string\]\$Phase' -and
    $cgCaptureSource -match '\[int\]\$StartupTimeoutSeconds\s*=\s*30' -and
    $cgCaptureSource -match '\$startupDeadline\s*=\s*\[DateTime\]::UtcNow\.AddSeconds\(\$StartupTimeoutSeconds\)' -and
    $cgCaptureSource -match '\$receivedFirstDatagram\s*=\s*\$true' -and
    $cgCaptureSource -match 'CG capture phase \$Phase received 0 bytes' -and
    $cgCaptureSource -match 'CG capture phase \$Phase received \$\(\$memory\.Length\) bytes' -and
    $cgProofSource -match 'function Assert-CgStartupDatagram' -and
    $cgCaptureSource -match 'Assert-CgStartupDatagram -Datagram \$datagram -Phase \$Phase' -and
    $cgProofSource -match 'Capture-Udp \$baselineCapture -Phase ''baseline''' -and
    $cgProofSource -match 'Capture-Udp \$redCapture -Phase ''red''' -and
    $cgProofSource -match 'Capture-Udp \$greenCapture -Phase ''green''' -and
    $cgProofSource -match 'Capture-Udp \$clearCapture -Phase ''clear''') `
    'CG proof must give a fresh worker a bounded actual-UDP startup window before rejecting a short capture.'
$cgProofTokens = $null
$cgProofParseErrors = $null
$cgProofAst = [Management.Automation.Language.Parser]::ParseInput($cgProofSource, [ref]$cgProofTokens, [ref]$cgProofParseErrors)
$cgRuntimeReadinessFunction = @($cgProofAst.FindAll({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Wait-ProofChannelRuntimeReady'
}, $true))
$cgRuntimeReadinessSource = if ($cgRuntimeReadinessFunction.Count -eq 1) {
    $cgRuntimeReadinessFunction[0].Extent.Text
} else {
    ''
}
$cgRuntimeReadyBeforeBaselineCapture = [bool]($cgProofSource -match
    'Wait-ProofChannelRuntimeReady\s*\r?\n\s*Clear-UdpBuffer\s*\r?\n\s*Capture-Udp \$baselineCapture')
Assert-Contract ($cgProofParseErrors.Count -eq 0 -and $cgRuntimeReadinessFunction.Count -eq 1) `
    'CG proof must define one parseable proof-channel runtime readiness guard.'
Assert-Contract ($cgRuntimeReadinessSource -match 'runtime-safe-to-air') `
    'CG proof runtime readiness must read authoritative safe-to-air state.'
Assert-Contract ($cgRuntimeReadinessSource -match '\$safeToAir\.color -eq ''green''') `
    'CG proof runtime readiness must require green safe-to-air authority.'
Assert-Contract ($cgRuntimeReadinessSource -match "state -in @\('healthy_fallback', 'on_air'\)") `
    'CG proof runtime readiness must require a healthy or on-air proof channel.'
Assert-Contract ($cgRuntimeReadinessSource -match 'Assert-WorkerAlive') `
    'CG proof runtime readiness must keep the proof worker alive.'
Assert-Contract $cgRuntimeReadyBeforeBaselineCapture `
    'CG proof must wait for authoritative runtime readiness before its first UDP capture.'
Assert-Contract ($cgProofSource -match '\[Net\.Sockets\.UdpClient\]::new\(\[Net\.IPEndPoint\]::new\(\[Net\.IPAddress\]::Loopback, \$DestinationPort\)\)' -and
    $cgProofSource -match '\$DestinationPort\s*=\s*\(\[Net\.IPEndPoint\]\$receiver\.Client\.LocalEndPoint\)\.Port' -and
    $commissioningSource -match 'function Get-FreeUdpPort' -and
    $commissioningSource -match '\$port\s*=\s*Get-FreeUdpPort' -and
    $commissioningSource -match '(?ms)function Invoke-CgProof \{.*?-DestinationPort 0.*?^\}') `
    'Commissioning CG proof must retain its own port-zero UDP binding through channel configuration.'
. $decoderPinPath
function Test-ExpectedThrow([scriptblock]$Action) { try { & $Action; return $false } catch { return $true } }
$decoderFixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ('townlight-decoder-pin-' + [guid]::NewGuid().ToString('N'))
try {
    $programFilesFixture = Join-Path $decoderFixtureRoot 'Program Files'
    $applicationFixture = Join-Path $programFilesFixture 'TownLight Station'
    $decoderFixture = Join-Path $applicationFixture 'runtime\caption-decoder\ffmpeg.exe'
    [void][IO.Directory]::CreateDirectory((Split-Path $decoderFixture -Parent))
    [IO.File]::WriteAllBytes($decoderFixture, [Text.Encoding]::UTF8.GetBytes('TownLight pinned decoder fixture'))
    $decoderFixtureHash = (Get-FileHash -LiteralPath $decoderFixture -Algorithm SHA256).Hash.ToLowerInvariant()
    $decoderFixtureSize = [int64](Get-Item -LiteralPath $decoderFixture).Length
    Assert-Contract (Test-ExpectedThrow { Open-TownLightPinnedCaptionDecoder -ProgramFilesRoot $programFilesFixture -ExpectedSha256 ('0' * 64) -ExpectedSizeBytes $decoderFixtureSize }) 'Caption decoder pin must reject a wrong manifest hash.'
    Assert-Contract (Test-ExpectedThrow { Open-TownLightPinnedCaptionDecoder -ProgramFilesRoot (Join-Path $decoderFixtureRoot 'missing-program-files') -ExpectedSha256 $decoderFixtureHash -ExpectedSizeBytes $decoderFixtureSize }) 'Caption decoder pin must reject a missing private decoder.'
    $pin = Open-TownLightPinnedCaptionDecoder -ProgramFilesRoot $programFilesFixture -ExpectedSha256 $decoderFixtureHash -ExpectedSizeBytes $decoderFixtureSize
    try {
        Assert-Contract (@($pin.directory_handles).Count -eq 4 -and @($pin.directory_handles | Where-Object { $_.IsInvalid -or $_.IsClosed }).Count -eq 0) 'Caption decoder pin must retain four healthy no-delete directory handles.'
        Assert-Contract (Test-ExpectedThrow { [IO.File]::Open($decoderFixture, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::Read).Dispose() }) 'Caption decoder pin must deny a concurrent write open.'
        Assert-Contract (Test-ExpectedThrow { Remove-Item -LiteralPath $decoderFixture -Force -ErrorAction Stop }) 'Caption decoder pin must deny a concurrent delete.'
        Assert-Contract (Test-ExpectedThrow { Move-Item -LiteralPath $applicationFixture -Destination "$applicationFixture-moved" -ErrorAction Stop }) 'Caption decoder directory pins must deny a parent rename.'
        Assert-TownLightPinnedCaptionDecoder -Stream $pin.stream -ExpectedPath $pin.path -ExpectedSha256 $decoderFixtureHash -ExpectedSizeBytes $decoderFixtureSize
        Assert-Contract $true 'Caption decoder pin must retain its expected stream identity.'
    }
    finally { $pin.stream.Dispose() }
    $reparseTarget = Join-Path $decoderFixtureRoot 'reparse-target'
    [void][IO.Directory]::CreateDirectory((Join-Path $reparseTarget 'TownLight Station\runtime\caption-decoder'))
    [IO.File]::WriteAllBytes((Join-Path $reparseTarget 'TownLight Station\runtime\caption-decoder\ffmpeg.exe'), [byte[]](1,2,3))
    $reparseProgramFiles = Join-Path $decoderFixtureRoot 'reparse-program-files'
    New-Item -ItemType Junction -Path $reparseProgramFiles -Target $reparseTarget -ErrorAction Stop | Out-Null
    try {
        Assert-Contract (Test-ExpectedThrow { Open-TownLightPinnedCaptionDecoder -ProgramFilesRoot $reparseProgramFiles -ExpectedSha256 $decoderFixtureHash -ExpectedSizeBytes $decoderFixtureSize }) 'Caption decoder pin must reject a reparse-point ancestor.'
    }
    finally { if (Test-Path -LiteralPath $reparseProgramFiles) { [IO.Directory]::Delete($reparseProgramFiles) } }
}
finally {
    if (Test-Path -LiteralPath $decoderFixtureRoot) { Remove-Item -LiteralPath $decoderFixtureRoot -Recurse -Force }
}
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
