[CmdletBinding()]
param(
    [string]$EvidenceRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) 'target\evidence\native-worker-teardown'),

    [ValidateRange(30, 3600)]
    [int]$TimeoutSeconds = 1200,

    [ValidateRange(1000, 60000)]
    [int]$EventObservationMilliseconds = 10000
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$gatePath = Join-Path $PSScriptRoot 'invoke-native-crash-gate.ps1'
$cargoPath = (Get-Command cargo.exe -ErrorAction Stop).Source
$gstreamerRoot = [Environment]::GetEnvironmentVariable('GSTREAMER_1_0_ROOT_MSVC_X86_64')
if ([string]::IsNullOrWhiteSpace($gstreamerRoot)) {
    throw 'GSTREAMER_1_0_ROOT_MSVC_X86_64 is not configured.'
}
$gstreamerBin = Join-Path $gstreamerRoot 'bin'
$gstreamerPlugins = Join-Path $gstreamerRoot 'lib\gstreamer-1.0'
if (-not (Test-Path -LiteralPath $gstreamerBin -PathType Container)) {
    throw "The configured GStreamer bin directory does not exist: '$gstreamerBin'."
}
if (-not (Test-Path -LiteralPath $gstreamerPlugins -PathType Container)) {
    throw "The configured GStreamer plugin directory does not exist: '$gstreamerPlugins'."
}

New-Item -ItemType Directory -Path $EvidenceRoot -Force | Out-Null
$originalPath = $env:PATH
$originalPluginPath = $env:GST_PLUGIN_SYSTEM_PATH_1_0
try {
    $env:PATH = "$gstreamerBin;$originalPath"
    $env:GST_PLUGIN_SYSTEM_PATH_1_0 = $gstreamerPlugins

    & $gatePath -CommandPath $cargoPath `
        -ArgumentList @('test', '-p', 'channel-worker', '--all-targets', '--', '--test-threads=1') `
        -WorkingDirectory $repositoryRoot `
        -EvidencePath (Join-Path $EvidenceRoot 'channel-worker-all-targets.json') `
        -ComponentPattern @(
            'channel-worker.exe',
            'channel_worker-*.exe',
            'process_contract-*.exe',
            'supervision-*.exe',
            'output_proof_*.exe'
        ) `
        -TimeoutSeconds $TimeoutSeconds `
        -EventDrainMilliseconds $EventObservationMilliseconds | Out-Host

    & $gatePath -CommandPath $cargoPath `
        -ArgumentList @('test', '-p', 'station-media-engine', '--all-targets', '--', '--test-threads=1') `
        -WorkingDirectory $repositoryRoot `
        -EvidencePath (Join-Path $EvidenceRoot 'station-media-engine-all-targets.json') `
        -ComponentPattern @(
            'station_media_engine-*.exe',
            'output_loudness_variants-*.exe',
            'output_mux_contracts-*.exe',
            'output_proof_capture-*.exe',
            'persistent_udp-*.exe'
        ) `
        -TimeoutSeconds $TimeoutSeconds `
        -EventDrainMilliseconds $EventObservationMilliseconds | Out-Host
} finally {
    $env:PATH = $originalPath
    $env:GST_PLUGIN_SYSTEM_PATH_1_0 = $originalPluginPath
}

Write-Output "Native worker teardown proof passed. Evidence: $([IO.Path]::GetFullPath($EvidenceRoot))"
