param(
    [string]$Manifest = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\candidate-manifest.json'),
    [string]$SharedScriptsRoot = ''
)

$ErrorActionPreference = 'Stop'

function Get-TrustedKnownFolder([string]$Name) {
    $knownPath = switch ($Name) {
        'CommonApplicationData' { [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData) }
        'ProgramFilesX64' {
            $registry64 = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
                [Microsoft.Win32.RegistryHive]::LocalMachine,
                [Microsoft.Win32.RegistryView]::Registry64)
            try {
                $currentVersion = $registry64.OpenSubKey('SOFTWARE\Microsoft\Windows\CurrentVersion', $false)
                try { if ($null -ne $currentVersion) { [string]$currentVersion.GetValue('ProgramW6432Dir', $null) } }
                finally { if ($null -ne $currentVersion) { $currentVersion.Dispose() } }
            }
            finally { $registry64.Dispose() }
        }
        default { throw "Unsupported Windows known folder: $Name" }
    }
    if ([string]::IsNullOrWhiteSpace($knownPath)) { throw "Could not resolve trusted Windows known folder: $Name" }
    return [IO.Path]::GetFullPath($knownPath).TrimEnd('\')
}

function Resolve-TownLightSharedRoot([string]$RequestedRoot) {
    $repositoryScripts = [IO.Path]::GetFullPath((Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts')).TrimEnd('\')
    $expectedRoot = if ([string]::IsNullOrWhiteSpace($RequestedRoot)) { $repositoryScripts } else { [IO.Path]::GetFullPath($RequestedRoot).TrimEnd('\') }
    $allowedRoots = @($repositoryScripts, 'C:\TownLightShared')
    if (-not (@($allowedRoots | Where-Object { [string]::Equals($_, $expectedRoot, [StringComparison]::OrdinalIgnoreCase) }).Count -eq 1)) {
        throw "TownLight shared helper root is not an allowed canonical root: $RequestedRoot"
    }
    try { $rootItem = Get-Item -LiteralPath $expectedRoot -Force -ErrorAction Stop }
    catch { throw "TownLight shared helper root is missing: $expectedRoot" }
    if (-not $rootItem.PSIsContainer -or ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        -not [string]::Equals($rootItem.FullName.TrimEnd('\'), $expectedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "TownLight shared helper root drifted or is a reparse point: $expectedRoot"
    }
    $parent = $rootItem.Parent
    while ($null -ne $parent) {
        if (($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "TownLight shared helper root parent is a reparse point: $($parent.FullName)"
        }
        $next = $parent.Parent
        if ($null -eq $next -or $next.FullName -ieq $parent.FullName) { break }
        $parent = $next
    }
    return $rootItem.FullName.TrimEnd('\')
}

function Resolve-TownLightSharedScript([string]$Name, [string]$Root) {
    if ([string]::IsNullOrWhiteSpace($Name) -or -not [string]::Equals([IO.Path]::GetFileName($Name), $Name, [StringComparison]::Ordinal)) {
        throw 'TownLight shared script name is invalid.'
    }
    $path = Join-Path $Root $Name
    try { $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop }
    catch { throw "TownLight shared script is missing: $path" }
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        -not [string]::Equals($item.Directory.FullName.TrimEnd('\'), $Root.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase) -or
        -not [string]::Equals([IO.Path]::GetFileName($item.Name), $Name, [StringComparison]::Ordinal)) {
        throw "TownLight shared script drifted or is not a regular non-reparse file: $path"
    }
    return $item.FullName
}

$sharedScriptsRoot = Resolve-TownLightSharedRoot $SharedScriptsRoot
. (Resolve-TownLightSharedScript 'candidate-pair-verifier.ps1' $sharedScriptsRoot)
. (Resolve-TownLightSharedScript 'installed-private-runtime-verifier.ps1' $sharedScriptsRoot)
$programFiles = Get-TrustedKnownFolder 'ProgramFilesX64'
$commonApplicationData = Get-TrustedKnownFolder 'CommonApplicationData'
$application = Join-Path $programFiles 'TownLight Station'
$data = Join-Path $commonApplicationData 'TownLight Station'
$runtime = Join-Path $application 'runtime\gstreamer'
$tsduckRuntime = Join-Path $application 'runtime\tsduck'
$captionDecoder = Join-Path $application 'runtime\caption-decoder\ffmpeg.exe'
$captionDecoderLicense = Join-Path $application 'runtime\caption-decoder\LICENSE.txt'
$candidatePair = Get-TownLightCandidateIdentity -ManifestPath $Manifest
$manifestItem = Get-Item -LiteralPath $candidatePair.manifest_path -Force -ErrorAction Stop
$manifestDocument = $candidatePair.manifest
$candidateInstaller = $candidatePair.installer_path
$candidateInstallerItem = Get-Item -LiteralPath $candidateInstaller -Force -ErrorAction Stop
$candidateInstallerHash = (Get-FileHash -LiteralPath $candidateInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
if ($candidateInstallerHash -cne [string]$candidatePair.installer_sha256) {
    throw 'Candidate installer changed after candidate-pair validation.'
}
$receipt = Get-Content -LiteralPath (Join-Path $data 'install-receipt.json') -Raw | ConvertFrom-Json
$activeReceipt = Get-Content -LiteralPath (Join-Path $data 'active-slot.json') -Raw | ConvertFrom-Json
$runtimeManifestPath = Join-Path $data 'runtime-manifest.json'
$runtimeManifest = Get-Content -LiteralPath $runtimeManifestPath -Raw | ConvertFrom-Json
$tsduckRuntimeManifestPath = Join-Path $data 'tsduck-runtime-manifest.json'
$tsduckRuntimeManifest = Get-Content -LiteralPath $tsduckRuntimeManifestPath -Raw | ConvertFrom-Json

$expectedStationPath = '"' + (Join-Path $application 'stationd.exe') + '" service --database "' + (Join-Path $data 'station.db') + '" --address 127.0.0.1:4070'
$expectedUpdaterPath = '"' + (Join-Path $application 'station-updater.exe') + '" service --database "' + (Join-Path $data 'station.db') + '" --application-root "' + $application + '"'

$service = Get-CimInstance Win32_Service -Filter "Name='TownLightStation'"
if (-not $service -or $service.State -ne 'Running' -or $service.StartMode -ne 'Auto') {
    throw 'TownLightStation is not a running automatic Windows service.'
}
if ($service.StartName -ne 'LocalSystem') {
    throw "Unexpected service account: $($service.StartName)"
}
if (-not ([string]$service.PathName).Equals($expectedStationPath, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unexpected service command line: $($service.PathName)"
}

$updaterService = Get-CimInstance Win32_Service -Filter "Name='TownLightStationUpdater'"
if (-not $updaterService -or $updaterService.State -ne 'Running' -or $updaterService.StartMode -ne 'Auto') {
    throw 'TownLightStationUpdater is not a running automatic Windows service.'
}
if ($updaterService.StartName -ne 'LocalSystem') {
    throw "Unexpected updater service account: $($updaterService.StartName)"
}
if (-not ([string]$updaterService.PathName).Equals($expectedUpdaterPath, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unexpected updater service command line: $($updaterService.PathName)"
}

$healthDocument = & curl.exe --silent --show-error --fail-with-body --max-time 5 'http://127.0.0.1:4070/health'
if ($LASTEXITCODE -ne 0) {
    throw "Installed health endpoint failed: $healthDocument"
}
try {
    $health = ConvertFrom-Json -InputObject $healthDocument -ErrorAction Stop
}
catch {
    throw "Installed health endpoint returned invalid JSON: $healthDocument"
}
if ($health.database -ne 'ready' -or
    $health.status -ne 'ready' -or
    $null -eq $health.orchestration -or
    $health.orchestration.status -ne 'ready') {
    throw "Installed health contract failed: $healthDocument"
}

$stationdHash = (Get-FileHash -LiteralPath (Join-Path $application 'stationd.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
$workerHash = (Get-FileHash -LiteralPath (Join-Path $application 'channel-worker.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
$updaterHash = (Get-FileHash -LiteralPath (Join-Path $application 'station-updater.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
$captionDecoderHash = (Get-FileHash -LiteralPath $captionDecoder -Algorithm SHA256).Hash.ToLowerInvariant()
$captionDecoderLicenseHash = (Get-FileHash -LiteralPath $captionDecoderLicense -Algorithm SHA256).Hash.ToLowerInvariant()
if ($stationdHash -ne $manifestDocument.stationd_sha256) {
    throw 'Installed stationd does not match the candidate manifest.'
}
if ($workerHash -ne $manifestDocument.channel_worker_sha256) {
    throw 'Installed channel-worker does not match the candidate manifest.'
}
if ($updaterHash -ne $manifestDocument.station_updater_sha256) {
    throw 'Installed station-updater does not match the candidate manifest.'
}
if ($captionDecoderHash -ne $manifestDocument.caption_decoder_sha256 -or
    (Get-Item -LiteralPath $captionDecoder).Length -ne [int64]$manifestDocument.caption_decoder_size_bytes -or
    $captionDecoderLicenseHash -ne $manifestDocument.caption_decoder_license_sha256 -or
    (Get-Item -LiteralPath $captionDecoderLicense).Length -ne [int64]$manifestDocument.caption_decoder_license_size_bytes) {
    throw 'Installed caption decoder payload does not match the candidate manifest.'
}
$captionDecoderEntries = @(
    Get-Item -LiteralPath (Split-Path $captionDecoder -Parent), $captionDecoder, $captionDecoderLicense -Force
)
if (@($captionDecoderEntries | Where-Object {
    ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
}).Count -ne 0) {
    throw 'Installed caption decoder payload contains a reparse-point entry.'
}
if ($receipt.source_commit -ne $manifestDocument.source_commit -or
    $receipt.media_runtime_sha256 -ne $manifestDocument.media_runtime_sha256 -or
    $receipt.caption_decoder_archive_sha256 -ne $manifestDocument.caption_decoder_archive_sha256 -or
    $receipt.caption_decoder_sha256 -ne $manifestDocument.caption_decoder_sha256 -or
    $receipt.caption_decoder_license_sha256 -ne $manifestDocument.caption_decoder_license_sha256) {
    throw 'The installation receipt does not match the candidate manifest.'
}
$runtimeManifestHash = (Get-FileHash -LiteralPath $runtimeManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($receipt.runtime_manifest_sha256 -ne $runtimeManifestHash) {
    throw 'The installed runtime manifest does not match the installation receipt.'
}
if ($runtimeManifest.schema_version -ne 1 -or
    $runtimeManifest.product -ne 'TownLight Station' -or
    $runtimeManifest.version -ne $manifestDocument.version -or
    $runtimeManifest.source_commit -ne $manifestDocument.source_commit -or
    $runtimeManifest.runtime_package_sha256 -ne $manifestDocument.media_runtime_sha256) {
    throw 'The installed runtime manifest identity does not match the candidate.'
}
$tsduckRuntimeManifestHash = (Get-FileHash -LiteralPath $tsduckRuntimeManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($receipt.tsduck_version -cne [string]$manifestDocument.tsduck_version -or
    $receipt.tsduck_source_url -cne [string]$manifestDocument.tsduck_source_url -or
    $receipt.tsduck_archive_name -cne [string]$manifestDocument.tsduck_archive_name -or
    $receipt.tsduck_runtime_sha256 -cne [string]$manifestDocument.tsduck_runtime_sha256 -or
    $receipt.tsduck_runtime_manifest_sha256 -cne $tsduckRuntimeManifestHash) {
    throw 'The installation receipt does not match the pinned TSDuck candidate identity.'
}
if ($tsduckRuntimeManifest.schema_version -ne 1 -or
    $tsduckRuntimeManifest.product -ne 'TownLight Station' -or
    $tsduckRuntimeManifest.version -ne $manifestDocument.version -or
    $tsduckRuntimeManifest.source_commit -ne $manifestDocument.source_commit -or
    $tsduckRuntimeManifest.runtime_package_sha256 -ne $manifestDocument.tsduck_runtime_sha256) {
    throw 'The installed TSDuck runtime manifest identity does not match the candidate.'
}

$runtimeVerification = Assert-TownLightInstalledPrivateRuntime `
    -RuntimeRoot $runtime -RuntimeManifest $runtimeManifest
$tsduckRuntimeVerification = Assert-TownLightInstalledPrivateRuntime `
    -RuntimeRoot $tsduckRuntime -RuntimeManifest $tsduckRuntimeManifest
foreach ($requiredTSDuckFile in @('LICENSE.txt', 'OTHERS.txt', 'bin\tsp.exe', 'bin\tsanalyze.exe', 'bin\tsduck.dll', 'bin\tscore.dll')) {
    if (-not (Test-Path -LiteralPath (Join-Path $tsduckRuntime $requiredTSDuckFile) -PathType Leaf)) {
        throw "Installed TSDuck runtime is missing required payload: $requiredTSDuckFile"
    }
}
if ($activeReceipt.active.relative_directory -ne '.' -or
    $activeReceipt.active.package_manifest.source_commit -ne $manifestDocument.source_commit -or
    $activeReceipt.active.package_manifest.version -ne $manifestDocument.version -or
    $activeReceipt.active.package_manifest.files.Count -ne 2) {
    throw 'The active application receipt does not match the installed candidate.'
}
$activeHashes = @{}
foreach ($file in $activeReceipt.active.package_manifest.files) {
    $activeHashes[$file.path] = $file.sha256
}
if ($activeHashes['stationd.exe'] -ne $stationdHash -or
    $activeHashes['channel-worker.exe'] -ne $workerHash) {
    throw 'The active application receipt does not authenticate both slot-owned binaries.'
}

$env:PATH = (Join-Path $runtime 'bin') + ';' + $env:PATH
$env:GST_PLUGIN_PATH_1_0 = Join-Path $runtime 'lib\gstreamer-1.0'
$env:GST_PLUGIN_SYSTEM_PATH_1_0 = $env:GST_PLUGIN_PATH_1_0
foreach ($factory in @(
    'input-selector',
    'audiotestsrc',
    'audioconvert',
    'audioresample',
    'audiorate',
    'ebur128level',
    'videorate',
    'clocksync',
    'voaacenc',
    'aacparse',
    'uridecodebin',
    'tsdemux',
    'openh264dec',
    'avdec_aac',
    'openh264enc',
    'h264parse',
    'mpegtsmux',
    'udpsink',
    'srtsink',
    'gdkpixbufoverlay',
    'pngdec',
    'jpegdec'
)) {
    & (Join-Path $runtime 'bin\gst-inspect-1.0.exe') $factory *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Installed media factory is unavailable: $factory"
    }
}
& $captionDecoder -nostdin -hide_banner -version *> $null
if ($LASTEXITCODE -ne 0) {
    throw 'Installed caption decoder executable did not start successfully.'
}

[ordered]@{
    service = $service.State
    service_account = $service.StartName
    updater_service = $updaterService.State
    updater_service_account = $updaterService.StartName
    health = 'ready'
    source_commit = $receipt.source_commit
    candidate_installer_sha256 = $candidateInstallerHash
    stationd_sha256 = $stationdHash
    channel_worker_sha256 = $workerHash
    station_updater_sha256 = $updaterHash
    caption_decoder_sha256 = $captionDecoderHash
    caption_decoder_license_sha256 = $captionDecoderLicenseHash
    runtime_manifest_sha256 = $runtimeManifestHash
    tsduck_runtime_manifest_sha256 = $tsduckRuntimeManifestHash
    tsduck_runtime_file_count = $tsduckRuntimeVerification.file_count
    media_factories = 'ready'
    data_path = $data
} | ConvertTo-Json
