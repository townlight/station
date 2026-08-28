Set-StrictMode -Version Latest

if ($null -eq ('TownLight.CaptionDecoderPinNative' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace TownLight {
    public static class CaptionDecoderPinNative {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern SafeFileHandle CreateFileW(
            string fileName,
            uint desiredAccess,
            uint shareMode,
            IntPtr securityAttributes,
            uint creationDisposition,
            uint flagsAndAttributes,
            IntPtr templateFile);
    }
}
'@
}

function Open-TownLightNoDeleteDirectoryHandle {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    # FILE_READ_ATTRIBUTES; FILE_SHARE_READ only; OPEN_EXISTING;
    # FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT.
    $handle = [TownLight.CaptionDecoderPinNative]::CreateFileW(
        $Path, 0x00000080, 0x00000001, [IntPtr]::Zero, 3, 0x02200000, [IntPtr]::Zero)
    if ($null -eq $handle -or $handle.IsInvalid) {
        if ($null -ne $handle) { $handle.Dispose() }
        throw 'TownLight private caption decoder directory could not be retained without write/delete sharing.'
    }
    return $handle
}

function Get-TownLightSha256FromStream {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [IO.FileStream]$Stream
    )

    if (-not $Stream.CanRead -or -not $Stream.CanSeek) {
        throw 'TownLight caption decoder pin is not a readable seekable file stream.'
    }
    $Stream.Position = 0
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha256.ComputeHash($Stream))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
        $Stream.Position = 0
    }
}

function Assert-TownLightCaptionDecoderPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$DecoderPath,
        [Parameter(Mandatory = $true)]
        [string]$ProgramFilesRoot
    )

    $programFilesRootPath = [IO.Path]::GetFullPath($ProgramFilesRoot).TrimEnd('\')
    $applicationRootPath = [IO.Path]::GetFullPath((Join-Path $programFilesRootPath 'TownLight Station')).TrimEnd('\')
    $expectedDecoderPath = [IO.Path]::GetFullPath(
        (Join-Path $applicationRootPath 'runtime\caption-decoder\ffmpeg.exe'))
    $decoderPath = [IO.Path]::GetFullPath($DecoderPath)
    if (-not [string]::Equals($decoderPath, $expectedDecoderPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'TownLight caption decoder path is outside the trusted installed runtime location.'
    }

    # File.GetAttributes reads link metadata for the named path. Walk from the
    # executable through every existing private-runtime ancestor before opening
    # it, so a junction/symlink is rejected rather than trusted as an installed
    # decoder location.
    $cursor = $decoderPath
    while ($true) {
        if (-not (Test-Path -LiteralPath $cursor)) {
            throw 'TownLight private caption decoder payload is missing.'
        }
        if (([IO.File]::GetAttributes($cursor) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'TownLight private caption decoder path contains a reparse point.'
        }
        if ([string]::Equals($cursor, $programFilesRootPath, [StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $parent = [IO.Directory]::GetParent($cursor)
        if ($null -eq $parent -or [string]::IsNullOrWhiteSpace($parent.FullName)) {
            throw 'TownLight caption decoder path escaped the trusted application root.'
        }
        $cursor = [IO.Path]::GetFullPath($parent.FullName).TrimEnd('\')
    }

    $decoder = Get-Item -LiteralPath $decoderPath -Force -ErrorAction Stop
    if ($decoder.PSIsContainer -or $decoder.Name -cne 'ffmpeg.exe' -or
        ($decoder.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'TownLight private caption decoder is not a regular non-reparse ffmpeg.exe payload.'
    }
    return [IO.FileInfo]$decoder
}

function Open-TownLightPinnedCaptionDecoder {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProgramFilesRoot,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedSha256,
        [Parameter(Mandatory = $true)]
        [int64]$ExpectedSizeBytes
    )

    if ($ExpectedSha256 -cnotmatch '^[0-9a-f]{64}$' -or $ExpectedSizeBytes -le 0) {
        throw 'TownLight caption decoder manifest identity is invalid.'
    }
    $programFilesRootPath = [IO.Path]::GetFullPath($ProgramFilesRoot).TrimEnd('\')
    $applicationRoot = [IO.Path]::GetFullPath((Join-Path $programFilesRootPath 'TownLight Station'))
    $decoderPath = [IO.Path]::GetFullPath((Join-Path $applicationRoot 'runtime\caption-decoder\ffmpeg.exe'))
    $decoder = Assert-TownLightCaptionDecoderPath -DecoderPath $decoderPath -ProgramFilesRoot $ProgramFilesRoot
    $directoryHandles = [Collections.Generic.List[Microsoft.Win32.SafeHandles.SafeFileHandle]]::new()
    $stream = $null
    try {
        foreach ($directoryPath in @(
            $programFilesRootPath,
            $applicationRoot,
            (Join-Path $applicationRoot 'runtime'),
            (Join-Path $applicationRoot 'runtime\caption-decoder')
        )) {
            $directoryHandles.Add((Open-TownLightNoDeleteDirectoryHandle -Path $directoryPath))
        }
        $stream = [IO.File]::Open($decoder.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        # Re-walk after the no-write/no-delete share pin is acquired. The stream
        # and all directory handles remain owned by commissioning until every CG
        # decoder invocation completes; bytes, length, path, and ancestry are
        # checked again before the executable pathname is used.
        $afterPin = Assert-TownLightCaptionDecoderPath -DecoderPath $decoder.FullName -ProgramFilesRoot $ProgramFilesRoot
        $actualSha256 = Get-TownLightSha256FromStream -Stream $stream
        if ($stream.Name -cne $decoder.FullName -or $stream.Length -ne $ExpectedSizeBytes -or
            $afterPin.Length -ne $ExpectedSizeBytes -or $actualSha256 -cne $ExpectedSha256) {
            throw 'TownLight private caption decoder does not match the candidate manifest.'
        }
        return [pscustomobject]@{
            path = $decoder.FullName
            stream = $stream
            sha256 = $actualSha256
            size_bytes = [int64]$stream.Length
            directory_handles = $directoryHandles.ToArray()
        }
    }
    catch {
        if ($null -ne $stream) { $stream.Dispose() }
        foreach ($directoryHandle in $directoryHandles) { $directoryHandle.Dispose() }
        throw
    }
}

function Assert-TownLightPinnedCaptionDecoder {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [IO.FileStream]$Stream,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedPath,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedSha256,
        [Parameter(Mandatory = $true)]
        [int64]$ExpectedSizeBytes
    )

    if ($ExpectedSha256 -cnotmatch '^[0-9a-f]{64}$' -or $ExpectedSizeBytes -le 0 -or
        -not $Stream.CanRead -or -not $Stream.CanSeek -or
        $Stream.Name -cne [IO.Path]::GetFullPath($ExpectedPath) -or $Stream.Length -ne $ExpectedSizeBytes -or
        (Get-TownLightSha256FromStream -Stream $Stream) -cne $ExpectedSha256) {
        throw 'Pinned CG decoder does not retain the expected private payload identity.'
    }
}
