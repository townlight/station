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
    'carries the pinned private caption decoder' = 'caption-decoder-ffmpeg.exe'
    'carries the caption decoder license' = 'caption-decoder-LICENSE.txt'
    'carries the pinned TSDuck archive' = 'tsduck-runtime.zip'
    'uses the pinned TSDuck archive hash' = 'TSDuckRuntimeSha256Value'
    'extracts the pinned TSDuck runtime' = 'Expand-Archive -LiteralPath'
    'checks the TSDuck analyzer executable' = 'bin\tsp.exe'
    'checks the TSDuck analyzer companion' = 'bin\tsanalyze.exe'
    'checks the TSDuck native runtime' = 'bin\tsduck.dll'
    'records the TSDuck runtime source identity' = 'tsduck_source_url'
    'records the TSDuck runtime manifest digest' = 'tsduck-runtime-manifest.json'
    'hash-verifies the staged caption decoder' = 'CaptionDecoderSha256'
    'records the caption decoder archive identity' = 'caption_decoder_archive_sha256'
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
    'rejects any pre-existing private runtime during clean installation' = 'if DirExists(RuntimeRoot) or FileExists(RuntimeRoot) then'
    'does not remove a pre-existing private runtime during clean installation' = 'The private media runtime root already exists before clean installation.'
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
    'rejects an in-place product installation before activation' = 'function ExistingProductInstallationDetected: Boolean;'
    'rejects an existing application directory' = 'DirExists(ApplicationDirectory)'
    'rejects an existing station service' = 'ServiceExists(StationServiceName)'
    'rejects an existing updater service' = 'ServiceExists(UpdaterServiceName)'
    'rejects the same AppId uninstall registration' = 'RegKeyExists(HKLM64, UninstallRegistrySubkey)'
    'runs the clean-install-only preflight hook' = 'function PrepareToInstall(var NeedsRestart: Boolean): String;'
    'directs installed stations to the signed maintenance updater' = 'TownLight Station Maintenance updater for a signed in-place upgrade'
    'tracks station service ownership before candidate configuration' = 'StationServiceCreatedByCandidate := True;'
    'tracks updater service ownership before candidate configuration' = 'UpdaterServiceCreatedByCandidate := True;'
    'rolls back only candidate-owned services' = 'if StationServiceCreatedByCandidate then'
    'claims an empty candidate application directory atomically' = 'procedure ClaimCandidateApplicationDirectory;'
    'uses CreateDir to claim the candidate application directory' = 'CreateDir(AppDirectory)'
    'tracks candidate application directory ownership' = 'CandidateApplicationDirectoryClaimed: Boolean;'
    'removes the application directory only when candidate-owned' = 'procedure RemoveCandidateApplicationDirectory;'
    'fails if staged binary files already exist' = "AppDirectory + '\stationd.exe', True"
    'recognizes SCM already-exists errors during station registration' = 'ResultCode = 1073'
    'recognizes uncertain station service creation' = 'StationServiceExistedBeforeCreate'
    'recognizes uncertain updater service creation' = 'UpdaterServiceExistedBeforeCreate'
    'auth-state-gates installation activation' = 'PrepareOperatorAuthentication'
    'unattended setup has a noninteractive authentication boundary' = 'procedure PrepareSilentOperatorAuthentication;'
    'unattended setup selects that boundary explicitly' = 'if WizardSilent then'
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

$silentAuthStart = $source.IndexOf('procedure PrepareSilentOperatorAuthentication;')
$interactiveAuthStart = $source.IndexOf('procedure PrepareOperatorAuthentication;')
if ($silentAuthStart -lt 0 -or $interactiveAuthStart -le $silentAuthStart) {
    throw 'Installer unattended authentication boundary could not be isolated.'
}
$silentAuth = $source.Substring($silentAuthStart, $interactiveAuthStart - $silentAuthStart)
if (-not $silentAuth.Contains('IssueBootstrapHandoff;')) {
    throw 'Installer unattended first-run path does not provision protected bootstrap authority.'
}
if ($silentAuth.Contains('LaunchOperatorUrl') -or
    $silentAuth.Contains('ReadDeleteAndLaunchBootstrap')) {
    throw 'Installer unattended authentication boundary can launch an interactive operator console.'
}

$preflightStart = $source.IndexOf('function ExistingProductInstallationDetected: Boolean;')
$preflightEnd = $source.IndexOf('procedure DeinitializeSetup;', $preflightStart)
if ($preflightStart -lt 0 -or $preflightEnd -le $preflightStart) {
    throw 'Installer clean-install preflight boundary could not be isolated.'
}
$preflightBoundary = $source.Substring($preflightStart, $preflightEnd - $preflightStart)
if (-not $preflightBoundary.Contains('if ExistingProductInstallationDetected then')) {
    throw 'Installer preflight can continue into ssInstall despite an existing product installation.'
}

$detectorEnd = $source.IndexOf('function PrepareToInstall(var NeedsRestart: Boolean): String;', $preflightStart)
if ($detectorEnd -le $preflightStart) {
    throw 'Installer product-presence detector boundary could not be isolated.'
}
$detector = $source.Substring($preflightStart, $detectorEnd - $preflightStart)
foreach ($requiredPresenceSignal in @(
    'DirExists(ApplicationDirectory)',
    'ServiceExists(StationServiceName)',
    'ServiceExists(UpdaterServiceName)',
    'RegKeyExists(HKLM64, UninstallRegistrySubkey)'
)) {
    if (-not $detector.Contains($requiredPresenceSignal)) {
        throw "Installer product-presence detector omits: $requiredPresenceSignal"
    }
}

$claimStart = $source.IndexOf('procedure ClaimCandidateApplicationDirectory;')
$claimEnd = $source.IndexOf('procedure RemoveCandidateApplicationDirectory;', $claimStart)
if ($claimStart -lt 0 -or $claimEnd -le $claimStart) {
    throw 'Installer candidate application-directory claim boundary could not be isolated.'
}
$claimBoundary = $source.Substring($claimStart, $claimEnd - $claimStart)
foreach ($claimRequirement in @(
    'if DirExists(AppDirectory) or FileExists(AppDirectory) then',
    'if not CreateDir(AppDirectory) then',
    'CandidateApplicationDirectoryClaimed := True;'
)) {
    if (-not $claimBoundary.Contains($claimRequirement)) {
        throw "Installer candidate application-directory claim omits: $claimRequirement"
    }
}

$removeCandidateEnd = $source.IndexOf('procedure RemovePrivateRuntime;', $claimEnd)
if ($removeCandidateEnd -le $claimEnd) {
    throw 'Installer candidate application-directory rollback boundary could not be isolated.'
}
$removeCandidate = $source.Substring($claimEnd, $removeCandidateEnd - $claimEnd)
if (-not $removeCandidate.Contains('if not CandidateApplicationDirectoryClaimed then')) {
    throw 'Installer rollback can remove an application directory it did not claim.'
}

$installStart = $source.IndexOf('procedure CurStepChanged(CurStep: TSetupStep);')
$installEnd = $source.IndexOf('function ExistingProductInstallationDetected: Boolean;', $installStart)
if ($installStart -lt 0 -or $installEnd -le $installStart) {
    throw 'Installer ssInstall activation boundary could not be isolated.'
}
$installBoundary = $source.Substring($installStart, $installEnd - $installStart)
if ($installBoundary.Contains('StopAndDeleteServiceIfPresent')) {
    throw 'Installer ssInstall can stop/delete a pre-existing service after a raced or bypassed preflight.'
}
if ($installBoundary.IndexOf('ClaimCandidateApplicationDirectory;') -lt 0 -or
    $installBoundary.IndexOf('ClaimCandidateApplicationDirectory;') -gt
    $installBoundary.IndexOf('StageStationBinaries;')) {
    throw 'Installer can stage candidate files before atomically claiming the application directory.'
}

$stationCreateStart = $source.IndexOf('procedure CreateAndStartService;')
$stationCreateEnd = $source.IndexOf('procedure CreateAndStartUpdaterService;', $stationCreateStart)
$updaterCreateEnd = $source.IndexOf('function WaitForHealthyStation:', $stationCreateEnd)
if ($stationCreateStart -lt 0 -or $stationCreateEnd -le $stationCreateStart -or
    $updaterCreateEnd -le $stationCreateEnd) {
    throw 'Installer candidate service ownership boundaries could not be isolated.'
}
$stationCreate = $source.Substring($stationCreateStart, $stationCreateEnd - $stationCreateStart)
$updaterCreate = $source.Substring($stationCreateEnd, $updaterCreateEnd - $stationCreateEnd)
if ($stationCreate.IndexOf('StationServiceCreatedByCandidate := True;') -lt 0 -or
    $stationCreate.IndexOf('StationServiceCreatedByCandidate := True;') -gt
    $stationCreate.IndexOf('ConfigureServiceEnvironment;')) {
    throw 'Installer can mutate a station service before taking candidate ownership.'
}
if ($updaterCreate.IndexOf('UpdaterServiceCreatedByCandidate := True;') -lt 0 -or
    $updaterCreate.IndexOf('UpdaterServiceCreatedByCandidate := True;') -gt
    $updaterCreate.IndexOf('config '' + UpdaterServiceName')) {
    throw 'Installer can mutate an updater service before taking candidate ownership.'
}
foreach ($uncertainCreateRequirement in @(
    'ResultCode = 1073',
    'ServiceExists(StationServiceName)',
    'ServiceExists(UpdaterServiceName)'
)) {
    if (($stationCreate + $updaterCreate).IndexOf($uncertainCreateRequirement) -lt 0) {
        throw "Installer SCM create handling omits: $uncertainCreateRequirement"
    }
}
if ($preflightBoundary.Contains('StationDataDirectory') -or
    $preflightBoundary.Contains('install-receipt.json') -or
    $preflightBoundary.Contains('active-slot.json')) {
    throw 'Installer preflight treats deliberately preserved ProgramData as an installed application.'
}

foreach ($forbiddenUpgradeMechanism in @(
    'procedure BeginInstallerTransaction;',
    'procedure SnapshotPriorServices;',
    'function QueryServiceState',
    'procedure RestorePriorServices;',
    'reg.exe restore'
)) {
    if ($source.Contains($forbiddenUpgradeMechanism)) {
        throw "Installer attempts an unsupported in-place service restoration path: $forbiddenUpgradeMechanism"
    }
}

$runtimeInstallStart = $source.IndexOf('procedure InstallPrivateRuntime;')
$runtimeInstallEnd = $source.IndexOf('procedure WriteRuntimeManifest;', $runtimeInstallStart)
if ($runtimeInstallStart -lt 0 -or $runtimeInstallEnd -le $runtimeInstallStart) {
    throw 'Installer private-runtime installation boundary could not be isolated.'
}
$runtimeInstall = $source.Substring($runtimeInstallStart, $runtimeInstallEnd - $runtimeInstallStart)
if ($runtimeInstall.Contains('DelTree(RuntimeRoot') -or $runtimeInstall.Contains('unins000.exe')) {
    throw 'Installer can remove a pre-existing private runtime instead of rejecting the clean install.'
}

$runtimeInvocationStart = $source.IndexOf('function RunStationdWithPrivateRuntime')
$runtimeInvocationEnd = $source.IndexOf('procedure WriteRuntimeManifest;', $runtimeInvocationStart)
if ($runtimeInvocationStart -lt 0 -or $runtimeInvocationEnd -le $runtimeInvocationStart) {
    throw 'Installer private-runtime invocation boundary could not be isolated.'
}
$runtimeInvocation = $source.Substring($runtimeInvocationStart, $runtimeInvocationEnd - $runtimeInvocationStart)
foreach ($runtimeVariable in @('PATH', 'GST_PLUGIN_PATH_1_0', 'GST_PLUGIN_SYSTEM_PATH_1_0')) {
    if (-not $runtimeInvocation.Contains("NativeSetEnvironmentVariable('$runtimeVariable'")) {
        throw "Installer can execute stationd without explicitly setting $runtimeVariable to the private runtime."
    }
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
