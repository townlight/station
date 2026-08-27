$ErrorActionPreference = 'Stop'

if ($args.Count -lt 1) {
    throw 'Cargo did not supply an executable to the GStreamer runner.'
}

$executable = [string]$args[0]
$executableArguments = if ($args.Count -gt 1) { @($args[1..($args.Count - 1)]) } else { @() }
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
$requiredRuntimeDlls = @(
    'gstreamer-1.0-0.dll',
    'gstpbutils-1.0-0.dll',
    'gobject-2.0-0.dll',
    'glib-2.0-0.dll'
)
foreach ($requiredRuntimeDll in $requiredRuntimeDlls) {
    $requiredRuntimePath = Join-Path $runtimeBin $requiredRuntimeDll
    if (-not (Test-Path -LiteralPath $requiredRuntimePath -PathType Leaf)) {
        throw "The explicit GStreamer runtime DLL is missing: '$requiredRuntimePath'."
    }
}
if (-not (Test-Path -LiteralPath $pluginPath -PathType Container)) {
    throw "The explicit GStreamer plugin directory is missing: '$pluginPath'."
}

$env:PATH = $runtimeBin + ';' + $env:PATH
$env:GST_PLUGIN_PATH_1_0 = $pluginPath
$env:GST_PLUGIN_SYSTEM_PATH_1_0 = $pluginPath

& $executable @executableArguments
exit $LASTEXITCODE
