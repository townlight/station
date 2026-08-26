Set-StrictMode -Version Latest

# TSDuck is deliberately an input to a release build, never an implicit
# download.  The archive is pinned to an official upstream release and must be
# supplied from the local toolchain cache with this exact identity.
$script:TownLightTSDuckVersion = '3.44-4676'
$script:TownLightTSDuckArchiveName = 'TSDuck-Win64-3.44-4676-Portable.zip'
$script:TownLightTSDuckSourceUrl = 'https://github.com/tsduck/tsduck/releases/download/v3.44-4676/TSDuck-Win64-3.44-4676-Portable.zip'
$script:TownLightTSDuckArchiveSha256 = 'b0ca0f963fcd77488b8c32d6f9d85030daa891ed753c36e57bbb458807a44eb1'
$script:TownLightTSDuckArchiveSizeBytes = [int64]38476542
$script:TownLightTSDuckArchiveRoot = 'TSDuck'

function Get-TownLightTSDuckPin {
    [ordered]@{
        version = $script:TownLightTSDuckVersion
        archive_name = $script:TownLightTSDuckArchiveName
        source_url = $script:TownLightTSDuckSourceUrl
        archive_sha256 = $script:TownLightTSDuckArchiveSha256
        archive_size_bytes = $script:TownLightTSDuckArchiveSizeBytes
        archive_root = $script:TownLightTSDuckArchiveRoot
    }
}

function Assert-TownLightTSDuckArchive {
    param([Parameter(Mandatory = $true)][string]$ArchivePath)
    if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) {
        throw "Pinned TSDuck archive is missing: $ArchivePath"
    }
    $item = Get-Item -LiteralPath $ArchivePath -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Pinned TSDuck archive is a reparse point.'
    }
    if ([IO.Path]::GetFileName($item.Name) -cne $script:TownLightTSDuckArchiveName) {
        throw "Pinned TSDuck archive filename mismatch. Expected $script:TownLightTSDuckArchiveName."
    }
    if ([int64]$item.Length -ne $script:TownLightTSDuckArchiveSizeBytes) {
        throw "Pinned TSDuck archive size mismatch. Expected $script:TownLightTSDuckArchiveSizeBytes bytes."
    }
    $hash = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -cne $script:TownLightTSDuckArchiveSha256) {
        throw "Pinned TSDuck archive SHA-256 mismatch. Expected $script:TownLightTSDuckArchiveSha256, got $hash."
    }
    return $item.FullName
}

function Expand-TownLightTSDuckRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$ArchivePath,
        [Parameter(Mandatory = $true)][string]$DestinationPath
    )
    $archive = Assert-TownLightTSDuckArchive $ArchivePath
    $destination = [IO.Path]::GetFullPath($DestinationPath).TrimEnd('\')
    if (Test-Path -LiteralPath $destination) {
        throw "TSDuck staging destination already exists: $destination"
    }
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    try {
        Expand-Archive -LiteralPath $archive -DestinationPath $destination -ErrorAction Stop
        $root = Join-Path $destination $script:TownLightTSDuckArchiveRoot
        $required = @('LICENSE.txt', 'OTHERS.txt', 'bin\tsp.exe', 'bin\tsanalyze.exe', 'bin\tsduck.dll', 'bin\tscore.dll')
        foreach ($relative in $required) {
            $path = Join-Path $root $relative
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
                throw "Pinned TSDuck archive is missing required payload: $relative"
            }
        }
        $rootItem = Get-Item -LiteralPath $root -Force -ErrorAction Stop
        if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Pinned TSDuck archive root is a reparse point.'
        }
        foreach ($file in @(Get-ChildItem -LiteralPath $root -Recurse -Force -File)) {
            if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Pinned TSDuck archive contains a reparse-point payload: $($file.FullName)"
            }
        }
        return $root
    }
    catch {
        if (Test-Path -LiteralPath $destination) {
            Remove-Item -LiteralPath $destination -Recurse -Force -ErrorAction SilentlyContinue
        }
        throw
    }
}
