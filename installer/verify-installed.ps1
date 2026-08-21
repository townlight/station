param(
    [string]$Manifest = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\candidate-manifest.json')
)

$ErrorActionPreference = 'Stop'
$application = Join-Path $env:ProgramFiles 'TownLight Station'
$data = Join-Path $env:ProgramData 'TownLight Station'
$runtime = Join-Path $application 'runtime\gstreamer'
$manifestDocument = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
$receipt = Get-Content -LiteralPath (Join-Path $data 'install-receipt.json') -Raw | ConvertFrom-Json

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

$health = & curl.exe --silent --show-error --fail-with-body --max-time 5 'http://127.0.0.1:4070/health'
if ($LASTEXITCODE -ne 0 -or $health -ne '{"database":"ready","status":"ready"}') {
    throw "Installed health contract failed: $health"
}

$stationdHash = (Get-FileHash -LiteralPath (Join-Path $application 'stationd.exe') -Algorithm SHA256).Hash
$workerHash = (Get-FileHash -LiteralPath (Join-Path $application 'channel-worker.exe') -Algorithm SHA256).Hash
if ($stationdHash -ne $manifestDocument.stationd_sha256) {
    throw 'Installed stationd does not match the candidate manifest.'
}
if ($workerHash -ne $manifestDocument.channel_worker_sha256) {
    throw 'Installed channel-worker does not match the candidate manifest.'
}
if ($receipt.source_commit -ne $manifestDocument.source_commit -or
    $receipt.media_runtime_sha256 -ne $manifestDocument.media_runtime_sha256) {
    throw 'The installation receipt does not match the candidate manifest.'
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
    'udpsink'
)) {
    & (Join-Path $runtime 'bin\gst-inspect-1.0.exe') $factory *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Installed media factory is unavailable: $factory"
    }
}

[ordered]@{
    service = $service.State
    service_account = $service.StartName
    health = 'ready'
    source_commit = $receipt.source_commit
    stationd_sha256 = $stationdHash
    channel_worker_sha256 = $workerHash
    media_factories = 'ready'
    data_path = $data
} | ConvertTo-Json
