$ErrorActionPreference = 'Stop'

$configuredCargo = [Environment]::GetEnvironmentVariable('TOWNLIGHT_CARGO_EXECUTABLE')
$cargoExecutable = if ([string]::IsNullOrWhiteSpace($configuredCargo)) {
    'cargo.exe'
} else {
    $configuredCargo
}
$cargoArguments = @($args)

$configuredRoot = [Environment]::GetEnvironmentVariable('TOWNLIGHT_GSTREAMER_ROOT')
$runtimeRoot = if ([string]::IsNullOrWhiteSpace($configuredRoot)) {
    Join-Path $env:LOCALAPPDATA 'Programs\gstreamer\1.0\msvc_x86_64'
} else {
    $configuredRoot
}

if (-not (Test-Path -LiteralPath $runtimeRoot -PathType Container)) {
    throw "The explicit GStreamer runtime root does not exist: '$runtimeRoot'."
}

$runtimeRoot = (Resolve-Path -LiteralPath $runtimeRoot).Path
$runtimeBin = Join-Path $runtimeRoot 'bin'
$pluginPath = Join-Path $runtimeRoot 'lib\gstreamer-1.0'
$loaderDll = Join-Path $runtimeBin 'gstreamer-1.0-0.dll'
if (-not (Test-Path -LiteralPath $loaderDll -PathType Leaf)) {
    throw "The explicit GStreamer loader DLL is missing: '$loaderDll'."
}
if (-not (Test-Path -LiteralPath $pluginPath -PathType Container)) {
    throw "The explicit GStreamer plugin directory is missing: '$pluginPath'."
}

$priorPath = $env:PATH
$priorPluginPath = $env:GST_PLUGIN_PATH_1_0
$priorPluginSystemPath = $env:GST_PLUGIN_SYSTEM_PATH_1_0
try {
    $env:PATH = $runtimeBin + ';' + $priorPath
    $env:GST_PLUGIN_PATH_1_0 = $pluginPath
    $env:GST_PLUGIN_SYSTEM_PATH_1_0 = $pluginPath

    & $cargoExecutable @cargoArguments
    exit $LASTEXITCODE
}
finally {
    $env:PATH = $priorPath
    $env:GST_PLUGIN_PATH_1_0 = $priorPluginPath
    $env:GST_PLUGIN_SYSTEM_PATH_1_0 = $priorPluginSystemPath
}
