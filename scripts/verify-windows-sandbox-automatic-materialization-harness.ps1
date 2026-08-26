param()

$ErrorActionPreference = 'Stop'
$repository = Split-Path $PSScriptRoot -Parent
$configuration = Join-Path $repository 'harness\windows-sandbox\configs\automatic-materialization-proof.wsb'
$guestRunner = Join-Path $repository 'harness\windows-sandbox\run-automatic-materialization.ps1'
$hostRunner = Join-Path $PSScriptRoot 'prove-automatic-materialization-in-owned-sandbox.ps1'
$candidateHelper = Join-Path $PSScriptRoot 'candidate-pair-verifier.ps1'
$ownershipGuard = Join-Path $PSScriptRoot 'invoke-owned-windows-sandbox.ps1'

foreach ($required in @($configuration, $guestRunner, $hostRunner, $candidateHelper, $ownershipGuard)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Automatic-materialization Sandbox harness input is missing: $required"
    }
}

$configurationText = Get-Content -LiteralPath $configuration -Raw
$guestText = Get-Content -LiteralPath $guestRunner -Raw
$hostText = Get-Content -LiteralPath $hostRunner -Raw
$combined = $configurationText + "`n" + $guestText + "`n" + $hostText
$requirements = [ordered]@{
    'network is disabled' = '<Networking>Disable</Networking>'
    'audio input is disabled' = '<AudioInput>Disable</AudioInput>'
    'video input is disabled' = '<VideoInput>Disable</VideoInput>'
    'printer redirection is disabled' = '<PrinterRedirection>Disable</PrinterRedirection>'
    'clipboard redirection is disabled' = '<ClipboardRedirection>Disable</ClipboardRedirection>'
    'candidate input is read-only' = '<SandboxFolder>C:\TownLightCandidate</SandboxFolder>'
    'verifier input is read-only' = '<SandboxFolder>C:\TownLightVerifier</SandboxFolder>'
    'media fixture input is read-only' = '<SandboxFolder>C:\TownLightInputs</SandboxFolder>'
    'launch challenge input is read-only' = '<SandboxFolder>C:\TownLightChallenge</SandboxFolder>'
    'harness input is read-only' = '<SandboxFolder>C:\TownLightHarness</SandboxFolder>'
    'shared helper input is read-only' = '<SandboxFolder>C:\TownLightShared</SandboxFolder>'
    'evidence output is dedicated' = '<SandboxFolder>C:\TownLightEvidence</SandboxFolder>'
    'guest launch is explicit' = 'run-automatic-materialization.ps1'
    'host uses ownership guard only' = 'invoke-owned-windows-sandbox.ps1'
    'host verifies exact candidate pair' = 'Get-TownLightCandidateIdentity -ManifestPath $manifestPath -ExpectedSourceCommit $head'
    'host rehashes the manifest' = 'Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256'
    'host requires one clean source commit' = 'status --porcelain --untracked-files=all'
    'host binds every mapped input before launch' = 'Assert-ExactSandboxMappings -ConfigurationPath $configuration -Repository $repository'
    'host binds the exact retained fixture' = "'caption-source.ts'"
    'host observes a distinct provisional child receipt' = 'automatic-materialization-guest-*.json'
    'host emits a distinct final receipt' = 'automatic-materialization-host-final-*.json'
    'host writes final receipt atomically' = 'Write-AtomicHostFinalReceipt'
    'host allowlists child provenance into its receipt' = 'New-HostAttestedProvenance'
    'host requires provisional attestation status' = "-ne 'outer_host_attested'"
    'host requires candidate provenance' = 'candidate_provenance'
    'host requires upgrade acknowledgement' = 'host_upgrade_required'
    'host final evidence is explicitly authenticated' = "mode = 'outer_host_authenticated'"
    'host names the local-user trust boundary' = "trust_boundary = 'signed_in_windows_user_and_guarded_sandbox'"
    'provenance uses one manifest hash field' = 'candidate_manifest_sha256'
    'host checks fixture evidence name' = 'evidence.fixture_name'
    'host checks fixture evidence size' = 'evidence.fixture_size'
    'host leaves Sandbox untouched on timeout' = 'the Sandbox host was left untouched'
    'guest authenticates the exact candidate pair' = 'Get-TownLightCandidateIdentity -ManifestPath $manifestPath'
    'guest invokes installed restart proof' = '& $installedProofPath -Credential $proofCredential.Credential -ChannelId $channelId'
    'guest passes outer host attestation' = '-OuterHostAttested -ExpectedSourceCommit'
    'guest passes exact manifest authority' = '-ExpectedCandidateManifestSha256 $manifestSha256'
    'guest passes exact installer authority' = '-ExpectedInstallerSha256 $installerSha256'
    'guest suppresses only child process exit' = '-EnableServiceRestartProof -NoProcessExit -ResultPath $installedResultPath'
    'guest requires automatic origin' = "`$_.origin -ceq 'automatic'"
    'guest requires reserved automatic actor' = "`$_.actor_id -ceq `$automaticActorId"
    'guest requires generated Drafts' = "`$_.current_schedule.state -ceq 'draft'"
    'guest writes its receipt atomically' = 'Move-Item -LiteralPath $partPath -Destination $resultPath'
    'host creates a launch challenge' = 'automatic-materialization-challenge-$challengeId.json'
    'guest binds the host challenge' = 'Get-HostLaunchChallenge -ExpectedSourceCommit'
    'host verifies the returned challenge' = '$evidence.challenge_id -cne $challengeId'
    'host rehashes candidate after guest evidence' = '$finalCandidate = Get-TownLightCandidateIdentity'
    'host final provenance records the challenge hash' = 'challenge_sha256 = $challengeSha256'
    'guest returns the exact challenge hash' = '$result.challenge_sha256 = [string]$challengeAuthority.Sha256'
    'host verifies the returned challenge hash' = '$evidence.challenge_sha256 -cne $challengeSha256'
    'host rehashes the challenge after guest evidence' = '$challengeSha256After = (Get-FileHash -LiteralPath $challengePath'
    'host validates automatic run identity' = "$evidence.automatic_run.origin -cne 'automatic'"
    'host validates restart evidence' = '$evidence.installed_proof.restart_boundary.attempted -ne $true'
    'host snapshots all observed Sandbox processes' = '$observedSandboxProcesses = @(Get-ObservedSandboxProcesses)'
    'host waits for the exact observed Sandbox identities to exit' = 'Wait-ForObservedSandboxExit -Processes $observedSandboxProcesses'
    'guest checks native shutdown acceptance' = "if (`$LASTEXITCODE -ne 0) { throw 'sandbox_shutdown_request_failed' }"
}
foreach ($requirement in $requirements.GetEnumerator()) {
    if (-not $combined.Contains($requirement.Value)) {
        throw "Automatic-materialization Sandbox harness contract is incomplete: $($requirement.Key)"
    }
}

foreach ($rejection in @(
        "`$evidence.status -ne 'outer_host_attested'",
        "`$evidence.installed_proof.status -ne 'outer_host_attested'",
        "`$provenance.standalone -ne `$false",
        "`$provenance.host_upgrade_required -ne `$true"
    )) {
    if (-not $hostText.Contains($rejection)) {
        throw "Automatic-materialization host must reject a non-provisional or standalone child: $rejection"
    }
}
if ($configurationText.Contains('TownLightSource') -or $hostText.Contains('TownLightSource')) {
    throw 'Automatic-materialization proof must not map source into a fresh Sandbox or claim guest Git authority.'
}
if ($guestText.Contains('TownLightSource') -or $guestText -match '(?i)\bgit(?:\.exe)?\b') {
    throw 'Automatic-materialization guest must not claim source-checkout or Git authority.'
}
if ($guestText -match '(?i)SourceRepositoryRoot|\.git(?:\\|/|\b)') {
    throw 'Automatic-materialization guest must not reference a source checkout or Git metadata.'
}
if ($guestText -match '(?i)Invoke-ProofApi\s+Post\s+["'']?/scheduling/channels/[^\r\n ]+/materializations(?:["''\s]|$)' -or
    $guestText -match '(?i)materialize_scheduled_channel|schedule_materialization\.rs|station\.db') {
    throw 'Automatic-materialization guest must not invoke manual materialization or mutate storage directly.'
}
foreach ($forbidden in @(
        '\$result\.(?:password|credential|capability|sessionToken|csrfToken)',
        '\$env:[A-Za-z0-9_]*(?:PASSWORD|CREDENTIAL|CAPABILITY|SESSION|CSRF)',
        'ConvertTo-Json[^\r\n]*(?:bootstrapJson|bootstrapResponse|proofCredential)',
        'installerLog[^\r\n]*(?:password|capability|session|csrf)'
    )) {
    if ($guestText -match $forbidden) {
        throw "Automatic-materialization guest may expose authentication authority: $forbidden"
    }
}
$moveIndex = $guestText.LastIndexOf('Move-Item -LiteralPath $partPath -Destination $resultPath')
$shutdownIndex = $guestText.LastIndexOf('shutdown.exe')
if ($moveIndex -lt 0 -or $shutdownIndex -le $moveIndex -or
    ([regex]::Matches($guestText, '(?i)shutdown\.exe')).Count -ne 1) {
    throw 'Automatic-materialization guest must shut down exactly once and only after atomic evidence publication.'
}
if ($guestText -notmatch '(?ms)finally\s*\{\s*& "\$env:SystemRoot\\System32\\shutdown\.exe"') {
    throw 'Automatic-materialization guest must shut down even when evidence publication itself fails.'
}
if ($guestText -match '(?i)\bStop-Process\b|\btaskkill(?:\.exe)?\b|\.Kill\s*\(') {
    throw 'Automatic-materialization guest must not terminate processes directly.'
}
if ($hostText -match '(?m)^\s*manifest_sha256\s*=.*New-HostAttestedProvenance' -or
    $guestText -match '(?m)^\s*manifest_sha256\s*=.*candidate_provenance') {
    throw 'Candidate provenance must use candidate_manifest_sha256, never the ambiguous legacy alias.'
}
if ($hostText -match '(?i)\bStart-Process\b[^\r\n]*WindowsSandbox(?:\.exe)?|\bStop-Process\b|\btaskkill(?:\.exe)?\b|\.Kill\s*\(') {
    throw 'Automatic-materialization host harness must not manage or terminate a Sandbox process directly.'
}
if ($configurationText -match '(?i)<HostFolder>.*Claude.*</HostFolder>') {
    throw 'Automatic-materialization Sandbox configuration must never map a Claude-owned path.'
}

[xml]$configurationDocument = $configurationText
$mappings = @($configurationDocument.Configuration.MappedFolders.MappedFolder)
if ($mappings.Count -ne 7) {
    throw 'Automatic-materialization Sandbox configuration must expose exactly six read-only inputs and one writable evidence boundary.'
}
$expectedReadOnlyMappings = [ordered]@{
    'C:\TownLightCandidate' = 'C:\Users\scott\Desktop\Code\townlight-station\dist'
    'C:\TownLightVerifier' = 'C:\Users\scott\Desktop\Code\townlight-station\installer'
    'C:\TownLightInputs' = 'C:\ProgramData\TownLight Station\SandboxInputs'
    'C:\TownLightChallenge' = 'C:\ProgramData\TownLight Station\SandboxChallenges'
    'C:\TownLightHarness' = 'C:\Users\scott\Desktop\Code\townlight-station\harness\windows-sandbox'
    'C:\TownLightShared' = 'C:\Users\scott\Desktop\Code\townlight-station\scripts'
}
foreach ($expected in $expectedReadOnlyMappings.GetEnumerator()) {
    $mapping = @($mappings | Where-Object {
            $_.SandboxFolder -ceq $expected.Key -and $_.HostFolder -ceq $expected.Value
        })
    if ($mapping.Count -ne 1 -or $mapping[0].ReadOnly -ne 'true') {
        throw "Automatic-materialization Sandbox mapping must be the exact read-only canonical input: $($expected.Key)"
    }
}
$writable = @($mappings | Where-Object { $_.ReadOnly -eq 'false' })
if ($writable.Count -ne 1 -or $writable[0].SandboxFolder -ne 'C:\TownLightEvidence' -or
    $writable[0].HostFolder -ne 'C:\ProgramData\TownLight Station\SandboxEvidence') {
    throw 'Automatic-materialization Sandbox configuration has an unexpected host-writable mapping.'
}
foreach ($mapping in @($mappings | Where-Object { $_.SandboxFolder -ne 'C:\TownLightEvidence' })) {
    if ($mapping.ReadOnly -ne 'true') {
        throw "Automatic-materialization Sandbox input mapping is not explicitly read-only: $($mapping.SandboxFolder)"
    }
}

Write-Output "Automatic-materialization Windows Sandbox host-attestation contract passed ($($requirements.Count) requirements)."
