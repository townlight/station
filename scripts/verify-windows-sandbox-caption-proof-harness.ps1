param()

$ErrorActionPreference = 'Stop'
$repository = Split-Path $PSScriptRoot -Parent
$configuration = Join-Path $repository 'harness\windows-sandbox\configs\caption-proof.wsb'
$guest = Join-Path $repository 'harness\windows-sandbox\run-caption-proof.ps1'
$hostRunner = Join-Path $PSScriptRoot 'prove-caption-in-owned-sandbox.ps1'

foreach ($required in @($configuration, $guest, $hostRunner)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Caption-proof Sandbox harness input is missing: $required"
    }
}

$configurationText = Get-Content -LiteralPath $configuration -Raw
$guestText = Get-Content -LiteralPath $guest -Raw
$hostText = Get-Content -LiteralPath $hostRunner -Raw
$combined = $configurationText + "`n" + $guestText + "`n" + $hostText
$fixtureHash = '4fb0c4cdc9a6b0ab7709b4ceb3b1ff4ce02c21e43d12cb84bc23bc1922de7c93'
$requirements = [ordered]@{
    'network is disabled' = '<Networking>Disable</Networking>'
    'candidate input is read-only' = '<SandboxFolder>C:\TownLightCandidate</SandboxFolder>'
    'verifier input is read-only' = '<SandboxFolder>C:\TownLightVerifier</SandboxFolder>'
    'fixture input is read-only' = '<SandboxFolder>C:\TownLightInputs</SandboxFolder>'
    'harness input is read-only' = '<SandboxFolder>C:\TownLightHarness</SandboxFolder>'
    'evidence output is separate' = '<SandboxFolder>C:\TownLightEvidence</SandboxFolder>'
    'guest launch is explicit' = 'run-caption-proof.ps1'
    'host uses only the ownership guard' = 'invoke-owned-windows-sandbox.ps1'
    'fixture bytes are exact' = $fixtureHash
    'guest verifies installed candidate' = 'verify-installed.ps1'
    'guest authenticates through public API' = '/auth/bootstrap'
    'guest uses the canonical protected handoff' = 'operator-bootstrap.capability'
    'guest retains exact cookies only in memory' = 'Get-ExactResponseCookie'
    'guest sends an explicit bounded cookie header' = 'Cookie = $script:apiCookieHeader'
    'guest creates caption authority' = '/caption-tracks/'
    'guest requests final-sink proof' = '/caption-proof'
    'guest requires verified decode-back' = "outcome -ne 'verified'"
    'guest shuts down itself' = 'shutdown.exe'
    'host never kills Sandbox' = 'Wait for guest-authored caption evidence without terminating the Sandbox host'
}
foreach ($requirement in $requirements.GetEnumerator()) {
    if (-not $combined.Contains($requirement.Value)) {
        throw "Caption-proof Sandbox harness contract is incomplete: $($requirement.Key)"
    }
}
if ($combined -match '(?i)\bStop-Process\b|\btaskkill(?:\.exe)?\b|\.Kill\s*\(') {
    throw 'Caption-proof Sandbox harness must not terminate a host process.'
}
if ($configurationText -match '(?i)<HostFolder>.*Claude.*</HostFolder>') {
    throw 'Caption-proof Sandbox configuration must never map a Claude-owned path.'
}
[xml]$configurationDocument = $configurationText
$mappings = @($configurationDocument.Configuration.MappedFolders.MappedFolder)
if ($mappings.Count -ne 5) {
    throw 'Caption-proof Sandbox configuration must expose exactly four read-only inputs and one writable evidence boundary.'
}
if (@($mappings | Where-Object { $_.ReadOnly -eq 'false' }).Count -ne 1 -or
    @($mappings | Where-Object { $_.ReadOnly -eq 'false' })[0].SandboxFolder -ne 'C:\TownLightEvidence') {
    throw 'Caption-proof Sandbox configuration has an unexpected host-writable mapping.'
}
foreach ($mapping in @($mappings | Where-Object { $_.SandboxFolder -ne 'C:\TownLightEvidence' })) {
    if ($mapping.ReadOnly -ne 'true') {
        throw "Caption-proof Sandbox input mapping is not explicitly read-only: $($mapping.SandboxFolder)"
    }
}

Write-Output "Caption-proof Windows Sandbox harness contract passed ($($requirements.Count) requirements)."
