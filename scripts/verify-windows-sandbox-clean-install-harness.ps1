param()

$ErrorActionPreference = 'Stop'
$repository = Split-Path $PSScriptRoot -Parent
$configuration = Join-Path $repository 'harness\windows-sandbox\configs\clean-install.wsb'
$guest = Join-Path $repository 'harness\windows-sandbox\run-clean-install.ps1'
$hostRunner = Join-Path $PSScriptRoot 'prove-clean-install-in-owned-sandbox.ps1'
$verifier = Join-Path $repository 'installer\verify-installed.ps1'
$candidateHelper = Join-Path $PSScriptRoot 'candidate-pair-verifier.ps1'
$privateRuntimeHelper = Join-Path $PSScriptRoot 'installed-private-runtime-verifier.ps1'

foreach ($required in @($configuration, $guest, $hostRunner, $verifier, $candidateHelper, $privateRuntimeHelper)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Clean-install Sandbox harness input is missing: $required"
    }
}

$configurationText = Get-Content -LiteralPath $configuration -Raw
$guestText = Get-Content -LiteralPath $guest -Raw
$hostText = Get-Content -LiteralPath $hostRunner -Raw
$verifierText = Get-Content -LiteralPath $verifier -Raw

$requirements = [ordered]@{
    'candidate mapping is read-only' = '<SandboxFolder>C:\TownLightCandidate</SandboxFolder>'
    'verifier mapping is read-only' = '<SandboxFolder>C:\TownLightVerifier</SandboxFolder>'
    'harness mapping is read-only' = '<SandboxFolder>C:\TownLightHarness</SandboxFolder>'
    'shared candidate verifier mapping is read-only' = '<SandboxFolder>C:\TownLightShared</SandboxFolder>'
    'evidence mapping is separate' = '<SandboxFolder>C:\TownLightEvidence</SandboxFolder>'
    'network is disabled' = '<Networking>Disable</Networking>'
    'guest launch is explicit' = 'run-clean-install.ps1'
    'guest verifies the installed candidate' = 'verify-installed.ps1'
    'guest uses the shared candidate verifier' = 'candidate-pair-verifier.ps1'
    'guest passes the exact mapped shared root' = "-SharedScriptsRoot 'C:\TownLightShared'"
    'installed verifier uses explicit shared helper resolver' = 'function Resolve-TownLightSharedScript'
    'installed verifier loads candidate helper through resolver' = ". (Resolve-TownLightSharedScript 'candidate-pair-verifier.ps1' `$sharedScriptsRoot)"
    'installed verifier loads private-runtime helper through resolver' = ". (Resolve-TownLightSharedScript 'installed-private-runtime-verifier.ps1' `$sharedScriptsRoot)"
    'guest records the candidate commit' = 'source_commit'
    'guest shuts down itself through the exact Windows system path' = "Join-Path `$env:SystemRoot 'System32\shutdown.exe'"
    'host uses only the ownership guard' = 'invoke-owned-windows-sandbox.ps1'
    'host never kills a Sandbox process' = 'Wait for the guest-authored evidence instead of terminating the Sandbox host'
}

$combined = $configurationText + "`n" + $guestText + "`n" + $hostText + "`n" + $verifierText
foreach ($requirement in $requirements.GetEnumerator()) {
    if (-not $combined.Contains($requirement.Value)) {
        throw "Clean-install Sandbox harness contract is incomplete: $($requirement.Key)"
    }
}
if ($combined -match '(?i)\bStop-Process\b|\btaskkill(?:\.exe)?\b|\.Kill\s*\(') {
    throw 'Clean-install Sandbox harness must not terminate a host process.'
}
if ($verifierText.Contains("'C:\scripts'") -or $verifierText.Contains('$candidateRoots')) {
    throw 'Clean-install installed verifier must never probe C:\scripts or alternate helper roots.'
}
if (-not $guestText.Contains("-SharedScriptsRoot 'C:\TownLightShared'")) {
    throw 'Clean-install guest must pass the exact read-only shared helper root.'
}
if ($configurationText -notmatch '(?s)<HostFolder>.*townlight-station\\dist</HostFolder>\s*<SandboxFolder>C:\\TownLightCandidate</SandboxFolder>\s*<ReadOnly>true</ReadOnly>') {
    throw 'Clean-install Sandbox candidate mapping is not explicitly read-only.'
}
if ($configurationText -notmatch '(?s)<HostFolder>.*townlight-station\\installer</HostFolder>\s*<SandboxFolder>C:\\TownLightVerifier</SandboxFolder>\s*<ReadOnly>true</ReadOnly>') {
    throw 'Clean-install Sandbox verifier mapping is not explicitly read-only.'
}
if ($configurationText -notmatch '(?s)<HostFolder>.*townlight-station\\harness\\windows-sandbox</HostFolder>\s*<SandboxFolder>C:\\TownLightHarness</SandboxFolder>\s*<ReadOnly>true</ReadOnly>') {
    throw 'Clean-install Sandbox harness mapping is not explicitly read-only.'
}
if ($configurationText -notmatch '(?s)<HostFolder>.*townlight-station\\scripts</HostFolder>\s*<SandboxFolder>C:\\TownLightShared</SandboxFolder>\s*<ReadOnly>true</ReadOnly>') {
    throw 'Clean-install Sandbox shared-verifier mapping is not explicitly read-only.'
}
if ($configurationText -notmatch '(?s)<HostFolder>C:\\ProgramData\\TownLight Station\\SandboxEvidence</HostFolder>\s*<SandboxFolder>C:\\TownLightEvidence</SandboxFolder>\s*<ReadOnly>false</ReadOnly>') {
    throw 'Clean-install Sandbox evidence mapping is not the dedicated TownLight-owned writable boundary.'
}
if ($configurationText -match '(?i)<HostFolder>.*Claude.*</HostFolder>') {
    throw 'Clean-install Sandbox configuration must never map a Claude-owned path.'
}

Write-Output "Clean-install Windows Sandbox harness contract passed ($($requirements.Count) requirements)."
