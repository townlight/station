$ErrorActionPreference = 'Stop'

$configuredCargo = [Environment]::GetEnvironmentVariable('TOWNLIGHT_CARGO_EXECUTABLE')
$cargoExecutable = if ([string]::IsNullOrWhiteSpace($configuredCargo)) {
    'cargo.exe'
} else {
    $configuredCargo
}
$cargoArguments = @($args)
$repositoryRoot = Split-Path -Parent $PSScriptRoot

if (-not ('TownLightCargoArtifactRuntimePublisher' -as [type])) {
    Add-Type -Path (Join-Path $repositoryRoot '.cargo\TownLightCargoArtifactRuntime.cs')
}

function Get-TownLightCargoTargetRoot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryRoot
    )

    $configuredTarget = [Environment]::GetEnvironmentVariable('CARGO_TARGET_DIR')
    if ([string]::IsNullOrWhiteSpace($configuredTarget)) {
        return Join-Path $RepositoryRoot 'target'
    }

    if ([IO.Path]::IsPathRooted($configuredTarget)) {
        return [IO.Path]::GetFullPath($configuredTarget)
    }

    return [IO.Path]::GetFullPath((Join-Path $RepositoryRoot $configuredTarget))
}

function Sync-TownLightGStreamerArtifactRuntime {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RuntimeBin,
        [Parameter(Mandatory = $true)]
        [string]$TargetRoot
    )

    if (-not (Test-Path -LiteralPath $TargetRoot -PathType Container)) {
        return
    }

    # Writable build output must never alias or redirect writes from the
    # installed runtime. The native publisher pins each selected directory,
    # creates and renames leaves relative to that retained no-follow handle,
    # and verifies hashes plus file identities before releasing it.
    $runtimeDlls = @(Get-ChildItem -LiteralPath $RuntimeBin -File -Filter '*.dll' | ForEach-Object {
        [pscustomobject]@{
            name = $_.Name
            path = $_.FullName
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
        }
    })
    if ($runtimeDlls.Count -eq 0) {
        throw "The explicit GStreamer runtime has no DLL closure: '$RuntimeBin'."
    }

    $targetRootItem = Get-Item -LiteralPath $TargetRoot -Force
    if (($targetRootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "The Cargo target root is a reparse point: '$TargetRoot'."
    }

    # Cargo places runnable artifacts only in a profile root or its direct
    # deps child. Construct those lexical paths instead of recursively deriving
    # a parent from an executable reached through a junction.
    $profileDirectories = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($profileName in @('debug', 'release')) {
        [void]$profileDirectories.Add((Join-Path $TargetRoot $profileName))
    }
    foreach ($targetChild in @(Get-ChildItem -LiteralPath $TargetRoot -Directory -Force)) {
        if (($targetChild.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "A Cargo target child is a reparse point: '$($targetChild.FullName)'."
        }
        if ($targetChild.Name -notin @('debug', 'release')) {
            foreach ($profileName in @('debug', 'release')) {
                [void]$profileDirectories.Add((Join-Path $targetChild.FullName $profileName))
            }
        }
    }

    $artifactDirectories = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($profileDirectory in $profileDirectories) {
        if (-not (Test-Path -LiteralPath $profileDirectory -PathType Container)) {
            continue
        }
        $profileItem = Get-Item -LiteralPath $profileDirectory -Force
        if (($profileItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "A Cargo profile directory is a reparse point: '$profileDirectory'."
        }
        if (@(Get-ChildItem -LiteralPath $profileDirectory -File -Filter '*.exe').Count -gt 0) {
            [void]$artifactDirectories.Add($profileDirectory)
        }
        $depsDirectory = Join-Path $profileDirectory 'deps'
        if (Test-Path -LiteralPath $depsDirectory -PathType Container) {
            $depsItem = Get-Item -LiteralPath $depsDirectory -Force
            if (($depsItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "A Cargo deps directory is a reparse point: '$depsDirectory'."
            }
            if (@(Get-ChildItem -LiteralPath $depsDirectory -File -Filter '*.exe').Count -gt 0) {
                [void]$artifactDirectories.Add($depsDirectory)
            }
        }
    }

    foreach ($artifactDirectory in $artifactDirectories) {
        [TownLightCargoArtifactRuntimePublisher]::PublishClosure(
            $artifactDirectory,
            [string[]]$runtimeDlls.path,
            [string[]]$runtimeDlls.sha256)
    }
}

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
        $cargoExitCode = $LASTEXITCODE
        if ($cargoExitCode -eq 0 -and -not ($cargoArguments -contains 'clean')) {
            Sync-TownLightGStreamerArtifactRuntime `
                -RuntimeBin $runtimeBin `
                -TargetRoot (Get-TownLightCargoTargetRoot -RepositoryRoot $repositoryRoot)
        }
        exit $cargoExitCode
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
