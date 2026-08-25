param(
    [string]$Manifest = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\candidate-manifest.json')
)

$ErrorActionPreference = 'Stop'
$application = Join-Path $env:ProgramFiles 'TownLight Station'
$data = Join-Path $env:ProgramData 'TownLight Station'
$runtime = Join-Path $application 'runtime\gstreamer'
$manifestDocument = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
$receipt = Get-Content -LiteralPath (Join-Path $data 'install-receipt.json') -Raw | ConvertFrom-Json
$activeReceipt = Get-Content -LiteralPath (Join-Path $data 'active-slot.json') -Raw | ConvertFrom-Json
$runtimeManifestPath = Join-Path $data 'runtime-manifest.json'
$runtimeManifest = Get-Content -LiteralPath $runtimeManifestPath -Raw | ConvertFrom-Json

$service = Get-CimInstance Win32_Service -Filter "Name='TownLightStation'"
if (-not $service -or $service.State -ne 'Running' -or $service.StartMode -ne 'Auto') {
    throw 'TownLightStation is not a running automatic Windows service.'
}
if ($service.StartName -ne 'LocalSystem') {
    throw "Unexpected service account: $($service.StartName)"
}
if ($service.PathName -notlike '"*\TownLight Station\stationd.exe" service --database *') {
    throw "Unexpected service command line: $($service.PathName)"
}

$updaterService = Get-CimInstance Win32_Service -Filter "Name='TownLightStationUpdater'"
if (-not $updaterService -or $updaterService.State -ne 'Running' -or $updaterService.StartMode -ne 'Auto') {
    throw 'TownLightStationUpdater is not a running automatic Windows service.'
}
if ($updaterService.StartName -ne 'LocalSystem') {
    throw "Unexpected updater service account: $($updaterService.StartName)"
}
if ($updaterService.PathName -notlike '"*\TownLight Station\station-updater.exe" service --database * --application-root *') {
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

$stationdHash = (Get-FileHash -LiteralPath (Join-Path $application 'stationd.exe') -Algorithm SHA256).Hash
$workerHash = (Get-FileHash -LiteralPath (Join-Path $application 'channel-worker.exe') -Algorithm SHA256).Hash
$updaterHash = (Get-FileHash -LiteralPath (Join-Path $application 'station-updater.exe') -Algorithm SHA256).Hash
if ($stationdHash -ne $manifestDocument.stationd_sha256) {
    throw 'Installed stationd does not match the candidate manifest.'
}
if ($workerHash -ne $manifestDocument.channel_worker_sha256) {
    throw 'Installed channel-worker does not match the candidate manifest.'
}
if ($updaterHash -ne $manifestDocument.station_updater_sha256) {
    throw 'Installed station-updater does not match the candidate manifest.'
}
if ($receipt.source_commit -ne $manifestDocument.source_commit -or
    $receipt.media_runtime_sha256 -ne $manifestDocument.media_runtime_sha256) {
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

$reparseEntries = @(Get-ChildItem -LiteralPath $runtime -Force -Recurse | Where-Object {
    ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
})
if ($reparseEntries.Count -ne 0) {
    throw 'The installed runtime contains a reparse-point entry.'
}
$expectedRuntimeFiles = [Collections.Generic.Dictionary[string,object]]::new(
    [StringComparer]::OrdinalIgnoreCase)
foreach ($file in @($runtimeManifest.files)) {
    $relative = [string]$file.path
    if ([string]::IsNullOrWhiteSpace($relative) -or
        $relative.Contains('\') -or
        $relative.StartsWith('/') -or
        $relative.Split('/') -contains '..' -or
        $expectedRuntimeFiles.ContainsKey($relative)) {
        throw 'The installed runtime manifest contains an unsafe or duplicate path.'
    }
    $expectedRuntimeFiles.Add($relative, $file)
}
$runtimePrefix = $runtime.TrimEnd('\') + '\'
$actualRuntimeFiles = @(Get-ChildItem -LiteralPath $runtime -File -Force -Recurse)
if ($actualRuntimeFiles.Count -ne $expectedRuntimeFiles.Count) {
    throw 'The installed runtime file set does not match its manifest.'
}
foreach ($actual in $actualRuntimeFiles) {
    if (-not $actual.FullName.StartsWith($runtimePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The installed runtime file escaped its managed root.'
    }
    $relative = $actual.FullName.Substring($runtimePrefix.Length).Replace('\', '/')
    $specification = $null
    if (-not $expectedRuntimeFiles.TryGetValue($relative, [ref]$specification) -or
        $actual.Length -ne [int64]$specification.size_bytes -or
        (Get-FileHash -LiteralPath $actual.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -ne
            ([string]$specification.sha256).ToLowerInvariant()) {
        throw "Installed runtime file does not match its manifest: $relative"
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

[ordered]@{
    service = $service.State
    service_account = $service.StartName
    updater_service = $updaterService.State
    updater_service_account = $updaterService.StartName
    health = 'ready'
    source_commit = $receipt.source_commit
    stationd_sha256 = $stationdHash
    channel_worker_sha256 = $workerHash
    station_updater_sha256 = $updaterHash
    runtime_manifest_sha256 = $runtimeManifestHash
    media_factories = 'ready'
    data_path = $data
} | ConvertTo-Json
