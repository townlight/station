Set-StrictMode -Version Latest

function Assert-TownLightInstalledPrivateRuntime {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RuntimeRoot,
        [Parameter(Mandatory = $true)]
        $RuntimeManifest,
        [ValidateRange(1, 50000)]
        [int]$MaximumManifestFiles = 50000,
        [ValidateRange(1, 100000)]
        [int]$MaximumEntries = 100000
    )

    if (-not (Test-Path -LiteralPath $RuntimeRoot -PathType Container)) {
        throw 'TownLight private runtime verification failed: private_runtime_missing'
    }
    $runtimeRootPath = [IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\')
    $runtimePrefix = $runtimeRootPath + '\'
    $runtimeRootItem = Get-Item -LiteralPath $runtimeRootPath -Force -ErrorAction Stop
    if (($runtimeRootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'TownLight private runtime verification failed: private_runtime_reparse_point'
    }

    $manifestFiles = @($RuntimeManifest.files)
    if ($manifestFiles.Count -eq 0 -or $manifestFiles.Count -gt $MaximumManifestFiles) {
        throw 'TownLight private runtime verification failed: runtime_manifest_file_capacity'
    }
    $expectedRuntimeFiles = [Collections.Generic.Dictionary[string,object]]::new(
        [StringComparer]::OrdinalIgnoreCase)
    foreach ($file in $manifestFiles) {
        $propertyNames = @($file.PSObject.Properties.Name)
        if ($propertyNames -notcontains 'path' -or $propertyNames -notcontains 'size_bytes' -or
            $propertyNames -notcontains 'sha256' -or $file.path -isnot [string] -or
            $file.size_bytes -isnot [int64] -or $file.sha256 -isnot [string]) {
            throw 'TownLight private runtime verification failed: runtime_manifest_file_invalid'
        }
        $relative = [string]$file.path
        $segments = @($relative -split '/')
        if ([string]::IsNullOrWhiteSpace($relative) -or $relative.Length -gt 1024 -or
            $relative.Contains('\') -or $relative.Contains(':') -or $relative.StartsWith('/') -or
            $relative.EndsWith('/') -or $segments -contains '' -or $segments -contains '.' -or
            $segments -contains '..' -or [int64]$file.size_bytes -lt 0 -or
            [string]$file.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            $expectedRuntimeFiles.ContainsKey($relative)) {
            throw 'TownLight private runtime verification failed: runtime_manifest_file_invalid'
        }
        $resolved = [IO.Path]::GetFullPath((Join-Path $runtimeRootPath $relative.Replace('/', '\')))
        if (-not $resolved.StartsWith($runtimePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'TownLight private runtime verification failed: runtime_manifest_file_escaped_root'
        }
        $expectedRuntimeFiles.Add($relative, $file)
    }

    # Walk one directory at a time. Each child is path-bounded and checked for
    # ReparsePoint before a directory is placed on the descent stack, so a
    # junction or symbolic link is never followed even when it targets a
    # location outside the TownLight runtime.
    $pendingDirectories = [Collections.Generic.Stack[IO.DirectoryInfo]]::new()
    $pendingDirectories.Push($runtimeRootItem)
    $actualRuntimeFiles = [Collections.Generic.List[IO.FileInfo]]::new()
    $entryCount = 0
    while ($pendingDirectories.Count -gt 0) {
        $directory = $pendingDirectories.Pop()
        foreach ($entry in @(Get-ChildItem -LiteralPath $directory.FullName -Force -ErrorAction Stop)) {
            $entryCount++
            if ($entryCount -gt $MaximumEntries) {
                throw 'TownLight private runtime verification failed: private_runtime_entry_capacity'
            }
            $entryPath = [IO.Path]::GetFullPath($entry.FullName)
            if (-not $entryPath.StartsWith($runtimePrefix, [StringComparison]::OrdinalIgnoreCase) -or
                ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'TownLight private runtime verification failed: private_runtime_reparse_point'
            }
            if ($entry.PSIsContainer) {
                $pendingDirectories.Push([IO.DirectoryInfo]$entry)
            } else {
                $actualRuntimeFiles.Add([IO.FileInfo]$entry)
            }
        }
    }

    if ($actualRuntimeFiles.Count -ne $expectedRuntimeFiles.Count) {
        throw 'TownLight private runtime verification failed: private_runtime_file_set_mismatch'
    }
    foreach ($actual in $actualRuntimeFiles) {
        $relative = $actual.FullName.Substring($runtimePrefix.Length).Replace('\', '/')
        $specification = $null
        if (-not $expectedRuntimeFiles.TryGetValue($relative, [ref]$specification) -or
            $actual.Length -ne [int64]$specification.size_bytes -or
            (Get-FileHash -LiteralPath $actual.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -cne
                [string]$specification.sha256) {
            throw 'TownLight private runtime verification failed: private_runtime_file_mismatch'
        }
    }

    [pscustomobject]@{
        file_count = $actualRuntimeFiles.Count
        entry_count = $entryCount
    }
}
