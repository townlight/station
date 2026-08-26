param()

$ErrorActionPreference = 'Stop'
$repository = Split-Path $PSScriptRoot -Parent
$configuration = Join-Path $repository 'harness\windows-sandbox\configs\caption-proof.wsb'
$guest = Join-Path $repository 'harness\windows-sandbox\run-caption-proof.ps1'
$hostRunner = Join-Path $PSScriptRoot 'prove-caption-in-owned-sandbox.ps1'
$verifier = Join-Path $repository 'installer\verify-installed.ps1'
$candidateHelper = Join-Path $PSScriptRoot 'candidate-pair-verifier.ps1'
$privateRuntimeHelper = Join-Path $PSScriptRoot 'installed-private-runtime-verifier.ps1'

foreach ($required in @($configuration, $guest, $hostRunner, $verifier, $candidateHelper, $privateRuntimeHelper)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Caption-proof Sandbox harness input is missing: $required"
    }
}

$configurationText = Get-Content -LiteralPath $configuration -Raw
$guestText = Get-Content -LiteralPath $guest -Raw
$hostText = Get-Content -LiteralPath $hostRunner -Raw
$verifierText = Get-Content -LiteralPath $verifier -Raw
$combined = $configurationText + "`n" + $guestText + "`n" + $hostText + "`n" + $verifierText
$fixtureHash = '4fb0c4cdc9a6b0ab7709b4ceb3b1ff4ce02c21e43d12cb84bc23bc1922de7c93'
$requirements = [ordered]@{
    'network is disabled' = '<Networking>Disable</Networking>'
    'candidate input is read-only' = '<SandboxFolder>C:\TownLightCandidate</SandboxFolder>'
    'verifier input is read-only' = '<SandboxFolder>C:\TownLightVerifier</SandboxFolder>'
    'fixture input is read-only' = '<SandboxFolder>C:\TownLightInputs</SandboxFolder>'
    'harness input is read-only' = '<SandboxFolder>C:\TownLightHarness</SandboxFolder>'
    'shared candidate verifier input is read-only' = '<SandboxFolder>C:\TownLightShared</SandboxFolder>'
    'evidence output is separate' = '<SandboxFolder>C:\TownLightEvidence</SandboxFolder>'
    'guest launch is explicit' = 'run-caption-proof.ps1'
    'host uses only the ownership guard' = 'invoke-owned-windows-sandbox.ps1'
    'fixture bytes are exact' = $fixtureHash
    'guest verifies installed candidate' = 'verify-installed.ps1'
    'guest uses the shared candidate verifier' = 'candidate-pair-verifier.ps1'
    'guest passes the exact mapped shared root' = "-SharedScriptsRoot 'C:\TownLightShared'"
    'installed verifier uses explicit shared helper resolver' = 'function Resolve-TownLightSharedScript'
    'installed verifier loads candidate helper through resolver' = ". (Resolve-TownLightSharedScript 'candidate-pair-verifier.ps1' `$sharedScriptsRoot)"
    'installed verifier loads private-runtime helper through resolver' = ". (Resolve-TownLightSharedScript 'installed-private-runtime-verifier.ps1' `$sharedScriptsRoot)"
    'guest authenticates through public API' = '/auth/bootstrap'
    'guest uses the canonical protected handoff' = 'operator-bootstrap.capability'
    'guest retains exact cookies only in memory' = 'Get-ExactResponseCookie'
    'guest bootstraps through byte-exact HTTP' = '-Unauthenticated -IncludeResponseHeaders'
    'guest proves authentication service continuity' = 'Get-StationServiceIdentity'
    'guest sends an explicit bounded cookie header' = 'Cookie = $script:apiCookieHeader'
    'guest uses byte-exact loopback HTTP for authenticated calls' = '[Net.Sockets.TcpClient]::new()'
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
if ($verifierText.Contains("'C:\scripts'") -or $verifierText.Contains('$candidateRoots')) {
    throw 'Caption-proof installed verifier must never probe C:\scripts or alternate helper roots.'
}
if (-not $guestText.Contains("-SharedScriptsRoot 'C:\TownLightShared'")) {
    throw 'Caption-proof guest must pass the exact read-only shared helper root.'
}
if ($configurationText -match '(?i)<HostFolder>.*Claude.*</HostFolder>') {
    throw 'Caption-proof Sandbox configuration must never map a Claude-owned path.'
}
[xml]$configurationDocument = $configurationText
$mappings = @($configurationDocument.Configuration.MappedFolders.MappedFolder)
if ($configurationText -notmatch '(?s)<HostFolder>.*townlight-station\\scripts</HostFolder>\s*<SandboxFolder>C:\\TownLightShared</SandboxFolder>\s*<ReadOnly>true</ReadOnly>') {
    throw 'Caption-proof Sandbox shared-verifier mapping is not explicitly read-only.'
}
if ($mappings.Count -ne 6) {
    throw 'Caption-proof Sandbox configuration must expose exactly five read-only inputs and one writable evidence boundary.'
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
