$ErrorActionPreference = 'Stop'

$configuredCargo = [Environment]::GetEnvironmentVariable('TOWNLIGHT_CARGO_EXECUTABLE')
$cargoExecutable = if ([string]::IsNullOrWhiteSpace($configuredCargo)) {
    'cargo.exe'
} else {
    $configuredCargo
}
$cargoArguments = @($args)
$repositoryRoot = Split-Path -Parent $PSScriptRoot

if (-not ('TownLightCargoFileIdentity' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

public static class TownLightCargoFileIdentity {
    [StructLayout(LayoutKind.Sequential)]
    private struct ByHandleFileInformation {
        public uint FileAttributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh;
        public uint FileIndexLow;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(
        SafeFileHandle file,
        out ByHandleFileInformation information);

    private static ByHandleFileInformation Read(string path) {
        using (var file = new FileStream(path, FileMode.Open, FileAccess.Read,
            FileShare.ReadWrite | FileShare.Delete)) {
            ByHandleFileInformation information;
            if (!GetFileInformationByHandle(file.SafeFileHandle, out information)) {
                throw new Win32Exception(Marshal.GetLastWin32Error(),
                    "GetFileInformationByHandle failed for " + path);
            }
            return information;
        }
    }

    public static bool SameFile(string first, string second) {
        var firstInformation = Read(first);
        var secondInformation = Read(second);
        return firstInformation.VolumeSerialNumber == secondInformation.VolumeSerialNumber &&
            firstInformation.FileIndexHigh == secondInformation.FileIndexHigh &&
            firstInformation.FileIndexLow == secondInformation.FileIndexLow;
    }
}
'@
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

function Assert-TownLightNonReparsePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "The $Description is a reparse point and cannot receive the GStreamer runtime: '$Path'."
    }
}

function Publish-TownLightGStreamerRuntimeCopy {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SourcePath,
        [Parameter(Mandatory = $true)]
        [string]$SourceHash,
        [Parameter(Mandatory = $true)]
        [string]$DestinationPath,
        [Parameter(Mandatory = $true)]
        [string]$ArtifactDirectory
    )

    $temporaryPath = Join-Path $ArtifactDirectory ".$(Split-Path -Leaf $DestinationPath).townlight-$PID-$([guid]::NewGuid().ToString('N')).tmp"
    $backupPath = "$temporaryPath.bak"
    try {
        Copy-Item -LiteralPath $SourcePath -Destination $temporaryPath -ErrorAction Stop
        if ((Get-FileHash -LiteralPath $temporaryPath -Algorithm SHA256).Hash -ne $SourceHash) {
            throw "The staged GStreamer runtime DLL did not verify: '$temporaryPath'."
        }
        Assert-TownLightNonReparsePath -Path $ArtifactDirectory -Description 'artifact directory'

        if (Test-Path -LiteralPath $DestinationPath) {
            Assert-TownLightNonReparsePath -Path $DestinationPath -Description 'artifact target'
            [IO.File]::Replace($temporaryPath, $DestinationPath, $backupPath)
        }
        else {
            [IO.File]::Move($temporaryPath, $DestinationPath)
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $backupPath) {
            Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
        }
    }
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

    # The target directory is writable build output. Do not hard-link runtime
    # DLLs into it: an in-place write through a hard link would mutate the
    # installed GStreamer runtime. Independent copies give direct artifact
    # launches their own complete DLL closure without that aliasing hazard.
    Assert-TownLightNonReparsePath -Path $TargetRoot -Description 'Cargo target root'
    Get-ChildItem -LiteralPath $TargetRoot -Directory -Force -Recurse | ForEach-Object {
        Assert-TownLightNonReparsePath -Path $_.FullName -Description 'Cargo target directory'
    }
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

    $artifactDirectories = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    Get-ChildItem -LiteralPath $TargetRoot -File -Filter '*.exe' -Recurse | ForEach-Object {
        $candidateDirectory = $_.Directory
        $candidateLeaf = $candidateDirectory.Name
        $isProfileRoot = $candidateLeaf -in @('debug', 'release')
        $isProfileDepsDirectory = $candidateLeaf -eq 'deps' -and $candidateDirectory.Parent -and
            $candidateDirectory.Parent.Name -in @('debug', 'release')
        if ($isProfileRoot -or $isProfileDepsDirectory) {
            [void]$artifactDirectories.Add($candidateDirectory.FullName)
        }
    }

    foreach ($artifactDirectory in $artifactDirectories) {
        Assert-TownLightNonReparsePath -Path $artifactDirectory -Description 'artifact directory'
        foreach ($runtimeDll in $runtimeDlls) {
            $deployedDll = Join-Path $artifactDirectory $runtimeDll.name
            if (Test-Path -LiteralPath $deployedDll) {
                if (-not (Test-Path -LiteralPath $deployedDll -PathType Leaf)) {
                    throw "The deployed GStreamer runtime target is not a file: '$deployedDll'."
                }
                Assert-TownLightNonReparsePath -Path $deployedDll -Description 'artifact target'
                $deployedHash = (Get-FileHash -LiteralPath $deployedDll -Algorithm SHA256).Hash
                $aliasesSource = [TownLightCargoFileIdentity]::SameFile($runtimeDll.path, $deployedDll)
                if ($deployedHash -eq $runtimeDll.sha256 -and -not $aliasesSource) {
                    continue
                }
            }

            Publish-TownLightGStreamerRuntimeCopy `
                -SourcePath $runtimeDll.path `
                -SourceHash $runtimeDll.sha256 `
                -DestinationPath $deployedDll `
                -ArtifactDirectory $artifactDirectory
            $verifiedHash = (Get-FileHash -LiteralPath $deployedDll -Algorithm SHA256).Hash
            if ($verifiedHash -ne $runtimeDll.sha256 -or
                [TownLightCargoFileIdentity]::SameFile($runtimeDll.path, $deployedDll)) {
                throw "The deployed GStreamer runtime DLL did not verify: '$deployedDll'."
            }
        }
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
