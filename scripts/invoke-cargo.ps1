$ErrorActionPreference = 'Stop'

$configuredCargo = [Environment]::GetEnvironmentVariable('TOWNLIGHT_CARGO_EXECUTABLE')
$cargoExecutable = if ([string]::IsNullOrWhiteSpace($configuredCargo)) {
    'cargo.exe'
} else {
    $configuredCargo
}
$cargoArguments = @($args)
$repositoryRoot = Split-Path -Parent $PSScriptRoot

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

if (-not ('TownLightCargoNativeErrorMode' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class TownLightCargoNativeErrorMode {
    [DllImport("kernel32.dll")]
    public static extern uint SetErrorMode(uint mode);
}
'@
}

# Missing native dependencies must fail in the invoking terminal. Descendant
# processes inherit this mode, so a bypassed Cargo runner cannot create a modal
# Windows loader/crash dialog that survives the test command.
$priorErrorMode = [TownLightCargoNativeErrorMode]::SetErrorMode(0x0001 -bor 0x0002)

$priorPath = $env:PATH
$priorPluginPath = $env:GST_PLUGIN_PATH_1_0
$priorPluginSystemPath = $env:GST_PLUGIN_SYSTEM_PATH_1_0
try {
    $env:PATH = $runtimeBin + ';' + $priorPath
    $env:GST_PLUGIN_PATH_1_0 = $pluginPath
    $env:GST_PLUGIN_SYSTEM_PATH_1_0 = $pluginPath

    Push-Location -LiteralPath $repositoryRoot
    try {
        & $cargoExecutable @cargoArguments
        exit $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}
finally {
    [void][TownLightCargoNativeErrorMode]::SetErrorMode($priorErrorMode)
    $env:PATH = $priorPath
    $env:GST_PLUGIN_PATH_1_0 = $priorPluginPath
    $env:GST_PLUGIN_SYSTEM_PATH_1_0 = $priorPluginSystemPath
}
