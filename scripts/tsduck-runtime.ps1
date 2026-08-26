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
$script:TownLightTSDuckMaximumArchiveEntries = 4096
$script:TownLightTSDuckMaximumPayloadBytes = [int64]1073741824

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

function Assert-TownLightTSDuckNoReparseAncestors {
    param([Parameter(Mandatory = $true)][string]$Path)
    $fullPath = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($fullPath)
    if ([string]::IsNullOrWhiteSpace($root)) { throw "TSDuck path has no volume root: $Path" }
    $current = $root.TrimEnd('\')
    if ([string]::IsNullOrWhiteSpace($current)) { $current = $root }
    $segments = @($fullPath.Substring($root.Length).Trim('\') -split '\\')
    foreach ($segment in $segments) {
        if ([string]::IsNullOrWhiteSpace($segment)) { continue }
        $current = Join-Path $current $segment
        $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "TSDuck path contains a reparse point: $($item.FullName)"
        }
    }
}

function Assert-TownLightTSDuckArchiveLayout {
    param([Parameter(Mandatory = $true)][string]$ArchivePath)
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        $entries = @($archive.Entries)
        if ($entries.Count -eq 0 -or $entries.Count -gt $script:TownLightTSDuckMaximumArchiveEntries) {
            throw 'Pinned TSDuck archive entry count is outside the accepted bounds.'
        }
        $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        $required = @{}
        foreach ($entry in $entries) {
            $rawName = [string]$entry.FullName
            # The official Windows archive uses backslashes in ZIP entry names.
            # Normalize both legal ZIP separator spellings before applying one
            # traversal, collision, root, and required-file policy.
            $name = $rawName.Replace('\', '/')
            $directoryEntry = $name.EndsWith('/')
            $trimmed = $name.TrimEnd('/')
            $segments = @($trimmed -split '/')
            if ([string]::IsNullOrWhiteSpace($name) -or $name.Length -gt 1024 -or
                $name.StartsWith('/') -or $name.Contains(':') -or
                $segments.Count -eq 0 -or $segments[0] -cne $script:TownLightTSDuckArchiveRoot -or
                $segments -contains '' -or $segments -contains '.' -or $segments -contains '..' -or
                -not $names.Add($name) -or
                (([int64]$entry.ExternalAttributes -shr 16) -band 0xF000) -eq 0xA000) {
                throw "Pinned TSDuck archive contains an unsafe entry: $rawName"
            }
            if (-not $directoryEntry) {
                if ([int64]$entry.Length -gt $script:TownLightTSDuckMaximumPayloadBytes) {
                    throw "Pinned TSDuck archive entry exceeds its size bound: $name"
                }
                $required[$name] = $true
            }
        }
        foreach ($relative in @('LICENSE.txt', 'OTHERS.txt', 'bin/tsp.exe', 'bin/tsanalyze.exe', 'bin/tsduck.dll', 'bin/tscore.dll')) {
            if (-not $required.ContainsKey($script:TownLightTSDuckArchiveRoot + '/' + $relative)) {
                throw "Pinned TSDuck archive is missing required payload: $relative"
            }
        }
    }
    finally { $archive.Dispose() }
}

function Assert-TownLightTSDuckExtractedTree {
    param([Parameter(Mandatory = $true)][string]$Root)
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $prefix = $rootPath + '\'
    $rootItem = Get-Item -LiteralPath $rootPath -Force -ErrorAction Stop
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Pinned TSDuck archive root is a reparse point.'
    }
    $pending = [Collections.Generic.Stack[IO.DirectoryInfo]]::new()
    $pending.Push([IO.DirectoryInfo]$rootItem)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($entry in @(Get-ChildItem -LiteralPath $directory.FullName -Force -ErrorAction Stop)) {
            $fullPath = [IO.Path]::GetFullPath($entry.FullName)
            if (-not $fullPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or
                ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Pinned TSDuck archive contains an unsafe extracted entry: $($entry.FullName)"
            }
            if ($entry.PSIsContainer) { $pending.Push([IO.DirectoryInfo]$entry) }
        }
    }
}

function Assert-TownLightTSDuckArchive {
    param([Parameter(Mandatory = $true)][string]$ArchivePath)
    if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) {
        throw "Pinned TSDuck archive is missing: $ArchivePath"
    }
    $item = Get-Item -LiteralPath $ArchivePath -Force
    Assert-TownLightTSDuckNoReparseAncestors $ArchivePath
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
    Assert-TownLightTSDuckNoReparseAncestors ([IO.Path]::GetDirectoryName($destination))
    Assert-TownLightTSDuckArchiveLayout $archive
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
        Assert-TownLightTSDuckExtractedTree $root
        return $root
    }
    catch {
        if (Test-Path -LiteralPath $destination) {
            try {
                Assert-TownLightTSDuckExtractedTree $destination
                Remove-Item -LiteralPath $destination -Recurse -Force -ErrorAction Stop
            }
            catch { }
        }
        throw
    }
}
