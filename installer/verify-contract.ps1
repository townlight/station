param(
    [string]$Installer = (Join-Path $PSScriptRoot 'TownLightStation.iss')
)

$ErrorActionPreference = 'Stop'

$source = Get-Content -LiteralPath $Installer -Raw
$requirements = [ordered]@{
    'requires administrative installation' = 'PrivilegesRequired=admin'
    'serializes setup attempts around the one-time handoff' = 'SetupMutex=Global\TownLightStationSetup-'
    'targets supported 64-bit Windows' = 'ArchitecturesAllowed=x64compatible'
    'carries the official media runtime' = 'ExtractTemporaryFile'
    'checks the required AAC encoder' = 'gstvoaacenc.dll'
    'checks the required audio rate adjuster' = 'gstaudiorate.dll'
    'checks the required video rate adjuster' = 'gstvideorate.dll'
    'checks the required loudness analyzer' = 'gstrsaudiofx.dll'
    'checks the required file playback plugin' = 'gstplayback.dll'
    'checks the required transport stream demuxer' = 'gstmpegtsdemux.dll'
    'checks the required software decoder bundle' = 'gstlibav.dll'
    'checks the required SRT plugin' = 'gstsrt.dll'
    'checks the required SRT transport library' = 'srt.dll'
    'checks the static image overlay plugin' = 'gstgdkpixbuf.dll'
    'checks the PNG image decoder plugin' = 'gstpng.dll'
    'checks the JPEG image decoder plugin' = 'gstjpeg.dll'
    'never blesses a merely presence-complete prior runtime' = 'if DirExists(RuntimeRoot) then'
    'removes the previous private runtime before replacement' = "if FileExists(RuntimeRoot + '\unins000.exe') then"
    'rejects an incompletely removed prior runtime tree' = 'The previous private media runtime root still exists after removal.'
    'creates an exact installed-runtime manifest before service start' = 'release write-runtime-manifest --runtime-root "'
    'binds the installed-runtime manifest digest into the receipt' = 'runtime_manifest_sha256'
    'registers the native station service' = 'create TownLightStation'
    'sets immediate automatic service start' = 'config TownLightStation start= auto'
    'configures service recovery' = 'failure TownLightStation'
    'starts the station service' = 'start TownLightStation'
    'registers the independent updater service' = 'create '' + UpdaterServiceName'
    'configures updater recovery' = '''failure '' + UpdaterServiceName'
    'starts the updater service' = '''start '' + UpdaterServiceName'
    'health-gates installation success' = 'WinHttp.WinHttpRequest.5.1'
    'queries the public authentication state after health' = 'http://127.0.0.1:4070/api/v1/auth/status'
    'recognizes preserved-account login state' = '"state":"login_required"'
    'recognizes first-install bootstrap state' = '"state":"bootstrap_required"'
    'recognizes an existing authenticated session' = '"state":"authenticated"'
    'uses the canonical station database path for bootstrap' = 'function StationDatabasePath: String;'
    'uses one canonical protected handoff path' = 'function BootstrapHandoffPath: String;'
    'invokes only the stationd bootstrap handoff command' = 'auth issue-bootstrap-handoff --database "'
    'passes the canonical handoff path to stationd' = '" --handoff "'
    'strictly requires token plus one LF byte' = 'if Length(RawCapability) <> 44 then'
    'strictly requires an LF terminator' = 'if RawCapability[44] <> #10 then'
    'validates every base64url token character' = 'IsBase64UrlCharacter(RawCapability[Index])'
    'deletes the protected handoff immediately after reading' = 'if not DeleteFile(HandoffPath) then'
    'clears the raw bootstrap capability from installer memory' = 'RawCapability := '''';'
    'clears the setup URL from installer memory' = 'SetupUrl := '''';'
    'uses a native non-Inno browser launch boundary' = 'ShellExecuteW@shell32.dll stdcall'
    'opens preserved stations at normal login root' = 'LaunchOperatorUrl(OperatorRootUrl)'
    'opens first install with a fragment capability' = 'OperatorRootUrl + ''#/setup/'' + Capability'
    'detects and explicitly rejects crash-remnant handoffs' = '  RejectCrashRemnantHandoff;'
    'cleans only the canonical handoff during rollback' = 'DeleteBootstrapHandoff'
    'tracks whether this setup owns rollback cleanup' = 'BootstrapHandoffOwned: Boolean;'
    'claims cleanup responsibility before launching stationd' = 'BootstrapHandoffOwned := True;'
    'releases cleanup responsibility after exact deletion' = 'BootstrapHandoffOwned := False;'
    'auth-state-gates installation activation' = 'PrepareOperatorAuthentication'
    'rolls back a failed service activation' = 'RollbackService'
    'removes the station service on uninstall' = 'StopAndDeleteServiceIfPresent(StationServiceName)'
    'removes the updater service on uninstall' = 'StopAndDeleteServiceIfPresent(UpdaterServiceName)'
    'records the immutable source commit' = 'source_commit'
    'records the active application slot' = 'active-slot.json'
    'preserves station data on uninstall' = 'Station data is deliberately preserved'
}

$missing = @()
foreach ($requirement in $requirements.GetEnumerator()) {
    if (-not $source.Contains($requirement.Value)) {
        $missing += $requirement.Key
    }
}

if ($missing.Count -gt 0) {
    throw "Installer contract is incomplete: $($missing -join '; ')"
}

if ($source.Contains('start= delayed-auto')) {
    throw 'Installer contract defers station recovery with delayed automatic startup.'
}

$forbiddenSecretSinks = @(
    '(?im)^\s*ShellExec\s*\(',
    '(?im)^\s*Log\s*\([^\r\n]*(Capability|SetupUrl)',
    '(?im)^\s*RaiseException\s*\([^\r\n]*(Capability|SetupUrl)',
    '(?im)^\s*SaveStringToFile\s*\([^\r\n]*(Capability|SetupUrl)',
    '(?im)^\s*SetEnvironmentVariable\w*\s*\([^\r\n]*(Capability|SetupUrl)',
    '(?im)^\s*Exec\s*\([^\r\n]*(Capability|SetupUrl)'
)
foreach ($pattern in $forbiddenSecretSinks) {
    if ($source -match $pattern) {
        throw "Installer contract can expose the bootstrap capability through forbidden sink: $pattern"
    }
}

$healthGate = $source.LastIndexOf('if not WaitForHealthyStation then')
$authGate = $source.LastIndexOf('PrepareOperatorAuthentication;')
if ($healthGate -lt 0 -or $authGate -le $healthGate) {
    throw 'Installer authentication state is not gated after service health.'
}
$activation = $source.LastIndexOf('CandidateActivated := True;')
if ($activation -le $authGate) {
    throw 'Installer marks activation successful before authentication state is resolved.'
}

$deleteHandoff = $source.LastIndexOf('if not DeleteFile(HandoffPath) then')
$buildSecretUrl = $source.IndexOf('OperatorRootUrl + ''#/setup/'' + Capability')
if ($deleteHandoff -lt 0 -or $buildSecretUrl -le $deleteHandoff) {
    throw 'Installer does not delete the handoff before constructing the one-time setup URL.'
}

$issueStart = $source.IndexOf('procedure IssueBootstrapHandoff;')
$issueEnd = $source.IndexOf('function IsBase64UrlCharacter', $issueStart)
if ($issueStart -lt 0 -or $issueEnd -le $issueStart) {
    throw 'Installer bootstrap command boundary could not be isolated.'
}
$issueBoundary = $source.Substring($issueStart, $issueEnd - $issueStart)
if ($issueBoundary -match '(Capability|SetupUrl|RawCapability)') {
    throw 'Installer bootstrap command boundary can place secret material in argv or environment.'
}

if ($source -match '(?is)Delete(File|Dir)[^;\r\n]*station\.db' -or
    $source.Contains('DeleteFile(StationDatabasePath')) {
    throw 'Installer rollback or recovery can delete the preserved station database.'
}

Write-Output "Installer contract passed ($($requirements.Count) requirements)."
