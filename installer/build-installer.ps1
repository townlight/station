param(
    [string]$GStreamerInstaller,
    [string]$CaptionDecoderArchive,
    [string]$InnoCompiler,
    [switch]$AllowDirty
)

$ErrorActionPreference = 'Stop'

function Get-TrustedKnownFolder([string]$Name) {
    $knownPath = switch ($Name) {
        'UserProfile' { [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile) }
        'LocalAppData' { [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData) }
        default { throw "Unsupported Windows known folder: $Name" }
    }
    if ([string]::IsNullOrWhiteSpace($knownPath)) { throw "Could not resolve trusted Windows known folder: $Name" }
    return [IO.Path]::GetFullPath($knownPath).TrimEnd('\')
}

$trustedUserProfile = Get-TrustedKnownFolder 'UserProfile'
$trustedLocalAppData = Get-TrustedKnownFolder 'LocalAppData'
if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA) -or [string]::IsNullOrWhiteSpace($env:USERPROFILE) -or
    -not ([string]$env:LOCALAPPDATA).Equals($trustedLocalAppData, [StringComparison]::OrdinalIgnoreCase) -or
    -not ([string]$env:USERPROFILE).Equals($trustedUserProfile, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'LOCALAPPDATA or USERPROFILE environment drifted from the trusted Windows known-folder values.'
}

$expectedGStreamerInstallerPath = Join-Path $trustedLocalAppData 'Temp\TownLightStation-Toolchain-20260820\gstreamer-1.0-msvc-x86_64-1.28.6.exe'
$expectedCaptionDecoderArchivePath = Join-Path $trustedLocalAppData 'Temp\TownLightStation-Toolchain-20260825\ffmpeg-n9.0.1-6-g9d4ca21220-win64-lgpl-9.0.zip'
$expectedInnoPath = Join-Path $trustedLocalAppData 'Programs\Inno Setup 6\ISCC.exe'
if ([string]::IsNullOrWhiteSpace($GStreamerInstaller)) { $GStreamerInstaller = $expectedGStreamerInstallerPath }
if ([string]::IsNullOrWhiteSpace($CaptionDecoderArchive)) { $CaptionDecoderArchive = $expectedCaptionDecoderArchivePath }
if ([string]::IsNullOrWhiteSpace($InnoCompiler)) { $InnoCompiler = $expectedInnoPath }

$expectedRuntimeHash = '059251444d1267b486eba390b18d25fed87e10315e72f757ec6c7e912fa746b5'
$expectedCaptionDecoderArchiveHash = 'ccaa910db95451121482a5ebcb8c682123cb4f8a5bd5170fde4a814d7b437772'
$expectedCaptionDecoderHash = 'b6b61f0d99c55e422bbd59fad6880a8d4f96d99a6c865f3bb58c8f1ce3ff7b31'
$expectedCaptionDecoderSize = 114400768
$expectedCaptionDecoderLicenseHash = 'da7eabb7bafdf7d3ae5e9f223aa5bdc1eece45ac569dc21b3b037520b4464768'
$expectedCaptionDecoderLicenseSize = 7651
$captionDecoderArchiveRoot = 'ffmpeg-n9.0.1-6-g9d4ca21220-win64-lgpl-9.0'

# Release builds use one explicit toolchain. Wrapper environment overrides are
# accepted only when they resolve to these exact canonical paths.
$expectedInnoHash = '0a8757031b33777e4c9cbffee40f11a5062b36d25cbe144c1db73b6102b80ad7'
$expectedInnoSize = 1456272
$expectedInnoSignerThumbprint = 'E0AB19C8D38CBF9C44709925122A7A02F8C70CB7'
$expectedCargoPath = Join-Path $trustedUserProfile '.cargo\bin\cargo.exe'
$expectedRustcPath = Join-Path $trustedUserProfile '.cargo\bin\rustc.exe'
$expectedCargoVersion = '1.97.1'
$expectedCargoCommit = 'c980f4866141969fab6254a680546a277789d6f0'
$expectedRustcVersion = '1.97.1'
$expectedRustcCommit = '8bab26f4f68e0e26f0bb7960be334d5b520ea452'
$expectedRustToolchain = '1.97.1-x86_64-pc-windows-msvc'
$expectedGStreamerRoot = Join-Path $trustedLocalAppData 'Programs\gstreamer\1.0\msvc_x86_64'
$expectedGStreamerVersion = '1.28.6'
$expectedMsvcRoot = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC\14.44.35207'
$expectedMsvcVersion = '14.44.35228.0'
$expectedWindowsSdkRoot = 'C:\Program Files (x86)\Windows Kits\10'
$expectedWindowsSdkVersion = '10.0.26100.0'

$repository = Split-Path $PSScriptRoot -Parent
$outputDirectory = Join-Path $repository 'dist'
$stationd = Join-Path $repository 'target\release\stationd.exe'
$channelWorker = Join-Path $repository 'target\release\channel-worker.exe'
$stationUpdater = Join-Path $repository 'target\release\station-updater.exe'
$contract = Join-Path $PSScriptRoot 'verify-contract.ps1'
$innoScript = Join-Path $PSScriptRoot 'TownLightStation.iss'
$operatorRoot = Join-Path $repository 'web\operator'
$cargoWrapper = Join-Path $repository 'scripts\invoke-cargo.ps1'
$installerName = 'TownLight-Station-0.1.0-x64-setup.exe'
$publishedInstaller = Join-Path $outputDirectory $installerName
$publishedManifest = Join-Path $outputDirectory 'candidate-manifest.json'

function Get-CanonicalPath([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { throw "Path does not exist: $Path" }
    return (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path.TrimEnd('\')
}

function Assert-CanonicalPath([string]$Actual, [string]$Expected, [string]$Label) {
    $actualPath = [IO.Path]::GetFullPath($Actual).TrimEnd('\')
    $expectedPath = [IO.Path]::GetFullPath($Expected).TrimEnd('\')
    if (-not $actualPath.Equals($expectedPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label is not the pinned canonical path. Expected '$expectedPath', got '$actualPath'."
    }
}

function Assert-ContainedPath([string]$Child, [string]$Parent, [string]$Label) {
    $parentPath = [IO.Path]::GetFullPath($Parent).TrimEnd('\')
    $childPath = [IO.Path]::GetFullPath($Child).TrimEnd('\')
    if (-not $childPath.StartsWith($parentPath + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label escaped its containment root: '$childPath' is not below '$parentPath'."
    }
    return $childPath
}

function Get-FileIdentity([string]$Path, [switch]$RequireValidSignature) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Pinned toolchain file is a reparse point: $Path" }
    $version = [Diagnostics.FileVersionInfo]::GetVersionInfo($item.FullName)
    $signature = Get-AuthenticodeSignature -FilePath $item.FullName
    if ($RequireValidSignature -and ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate)) { throw "Pinned toolchain file is not Authenticode-valid: $($item.FullName)" }
    [ordered]@{
        path = $item.FullName
        size_bytes = [int64]$item.Length
        sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        file_version = [string]$version.FileVersion
        product_version = [string]$version.ProductVersion
        signer_status = [string]$signature.Status
        signer_thumbprint = if ($null -ne $signature.SignerCertificate) { $signature.SignerCertificate.Thumbprint.ToUpperInvariant() } else { '' }
        signer_subject = if ($null -ne $signature.SignerCertificate) { [string]$signature.SignerCertificate.Subject } else { '' }
    }
}

function Get-GStreamerIdentity([string]$Root) {
    $rootPath = Get-CanonicalPath $Root
    Assert-CanonicalPath $rootPath $expectedGStreamerRoot 'GStreamer compile root'
    $uninstaller = Join-Path $rootPath 'unins000.exe'
    $loader = Join-Path $rootPath 'bin\gstreamer-1.0-0.dll'
    $importLibrary = Join-Path $rootPath 'lib\gstreamer-1.0.lib'
    $header = Join-Path $rootPath 'include\gstreamer-1.0\gst\gst.h'
    foreach ($file in @($uninstaller, $loader, $importLibrary, $header)) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Pinned GStreamer compile input is missing: $file" }
    }
    $uninstallerVersion = [Diagnostics.FileVersionInfo]::GetVersionInfo($uninstaller).ProductVersion.Trim()
    if ($uninstallerVersion -ne $expectedGStreamerVersion) { throw "GStreamer compile root version mismatch. Expected $expectedGStreamerVersion, got $uninstallerVersion." }
    [ordered]@{
        root = $rootPath
        version = $uninstallerVersion
        loader = Get-FileIdentity $loader
        import_library = Get-FileIdentity $importLibrary
        header = Get-FileIdentity $header
    }
}

function Invoke-CargoWrapperCapture([string[]]$Arguments) {
    if ([string]::IsNullOrWhiteSpace($cargoWrapper)) { throw 'Cargo wrapper path is not configured.' }
    $shellCommand = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    if ($null -eq $shellCommand) { $shellCommand = Get-Command powershell.exe -ErrorAction Stop }
    $escapedWrapper = $cargoWrapper.Replace("'", "''")
    $escapedArguments = @($Arguments | ForEach-Object { "'" + $_.Replace("'", "''") + "'" })
    $childScript = "& '$escapedWrapper' @($($escapedArguments -join ', ')); exit `$LASTEXITCODE"
    $encodedScript = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($childScript))
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $shellCommand.Source
    $startInfo.Arguments = "-NoLogo -NoProfile -NonInteractive -EncodedCommand $encodedScript"
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw 'Could not start the bounded Cargo wrapper probe.' }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        # Windows PowerShell/.NET Framework cannot reliably terminate a Cargo
        # descendant tree. Every Cargo invocation therefore waits for its
        # child while the mutex and pinned environment remain in force.
        $process.WaitForExit()
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        $result = [pscustomobject]@{
            standard_output = $stdout.Trim()
            standard_error = $stderr.Trim()
            exit_code = $process.ExitCode
        }
        if ($result.exit_code -ne 0) { throw "Cargo wrapper command failed (exit $($result.exit_code)): $($result.standard_error)" }
        return $result
    }
    finally { $process.Dispose() }
}

function Get-RustIdentity([string]$Path, [string]$Kind, [string]$ExpectedVersion, [string]$ExpectedCommit) {
    if ($Kind -notin @('cargo', 'rustc')) { throw "Unsupported Rust tool identity kind: $Kind" }
    $canonical = Get-CanonicalPath $Path
    $expectedPath = if ($Kind -eq 'cargo') { $expectedCargoPath } else { $expectedRustcPath }
    Assert-CanonicalPath $canonical $expectedPath "$Kind executable"
    $exitCode = 0
    $output = if ($Kind -eq 'cargo') {
        # Cargo must always be reached through the runtime-boundary wrapper.
        (Invoke-CargoWrapperCapture -Arguments @('--version', '--verbose')).standard_output
    } else {
        $rustcOutput = (& $canonical --version --verbose | Out-String).Trim()
        $exitCode = $LASTEXITCODE
        $rustcOutput
    }
    if ($exitCode -ne 0) { throw "$Kind version probe failed." }
    $firstLine = ($output -split "`r?`n")[0]
    $namedFields = @{}
    foreach ($line in ($output -split "`r?`n")) {
        if ($line -match '^\s*([^:]+):\s*(.*?)\s*$') {
            $namedFields[$matches[1].Trim().ToLowerInvariant()] = $matches[2].Trim()
        }
    }
    $release = [string]$namedFields['release']
    $commit = [string]$namedFields['commit-hash']
    $host = [string]$namedFields['host']
    if ($firstLine -notmatch "^$Kind\s+$([regex]::Escape($release))\s+\([0-9a-f]{9,40}(\s|\))" -or
        $release -cne $ExpectedVersion -or $commit -cnotmatch '^[0-9a-f]{40}$' -or
        $commit -cne $ExpectedCommit -or $host -cne 'x86_64-pc-windows-msvc') {
        throw "$Kind toolchain identity mismatch; expected named release $ExpectedVersion, full commit $ExpectedCommit, and host x86_64-pc-windows-msvc. Got '$firstLine'."
    }
    [ordered]@{
        path = $canonical
        version = $release
        commit = $commit
        host = $host
        executable = Get-FileIdentity $canonical
    }
}

function Get-MsvcIdentity {
    $clPath = Join-Path $expectedMsvcRoot 'bin\Hostx64\x64\cl.exe'
    $linkPath = Join-Path $expectedMsvcRoot 'bin\Hostx64\x64\link.exe'
    $resourcePath = Join-Path $expectedWindowsSdkRoot "bin\$expectedWindowsSdkVersion\x64\rc.exe"
    $msvcInclude = Join-Path $expectedMsvcRoot 'include'
    $msvcLib = Join-Path $expectedMsvcRoot 'lib\x64'
    $sdkInclude = Join-Path $expectedWindowsSdkRoot "Include\$expectedWindowsSdkVersion"
    $sdkUmLib = Join-Path $expectedWindowsSdkRoot "Lib\$expectedWindowsSdkVersion\um\x64"
    $sdkUcrtLib = Join-Path $expectedWindowsSdkRoot "Lib\$expectedWindowsSdkVersion\ucrt\x64"
    foreach ($file in @($clPath, $linkPath, $resourcePath)) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Pinned MSVC/SDK input is missing: $file" }
    }
    foreach ($directory in @($msvcInclude, $msvcLib, $sdkInclude, $sdkUmLib, $sdkUcrtLib)) {
        if (-not (Test-Path -LiteralPath $directory -PathType Container)) { throw "Pinned MSVC/SDK input is missing: $directory" }
    }
    foreach ($file in @($clPath, $linkPath)) {
        $version = [Diagnostics.FileVersionInfo]::GetVersionInfo($file).ProductVersion
        if ($version -ne $expectedMsvcVersion) { throw "MSVC tool version mismatch at '$file'. Expected $expectedMsvcVersion, got '$version'." }
    }
    [ordered]@{
        root = $expectedMsvcRoot
        version = $expectedMsvcVersion
        cl = Get-FileIdentity $clPath -RequireValidSignature
        link = Get-FileIdentity $linkPath -RequireValidSignature
        sdk_root = $expectedWindowsSdkRoot
        sdk_version = $expectedWindowsSdkVersion
        include_root = $msvcInclude
        lib_root = $msvcLib
        sdk_um_lib = $sdkUmLib
        sdk_ucrt_lib = $sdkUcrtLib
        rc = Get-FileIdentity $resourcePath -RequireValidSignature
    }
}

function Get-InnoIdentity([string]$Path) {
    $canonical = Get-CanonicalPath $Path
    Assert-CanonicalPath $canonical $expectedInnoPath 'Inno compiler'
    $item = Get-Item -LiteralPath $canonical -Force
    if ([int64]$item.Length -ne $expectedInnoSize) { throw "Inno compiler size mismatch. Expected $expectedInnoSize, got $($item.Length)." }
    $hash = (Get-FileHash -LiteralPath $canonical -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne $expectedInnoHash) { throw "Inno compiler hash mismatch. Expected $expectedInnoHash, got $hash." }
    $signature = Get-AuthenticodeSignature -FilePath $canonical
    if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate -or $signature.SignerCertificate.Thumbprint.ToUpperInvariant() -ne $expectedInnoSignerThumbprint) { throw 'Inno compiler signature does not match the pinned valid signer.' }
    [ordered]@{ path = $canonical; size_bytes = [int64]$item.Length; sha256 = $hash; signer_thumbprint = $signature.SignerCertificate.Thumbprint.ToUpperInvariant(); signer_subject = [string]$signature.SignerCertificate.Subject }
}

function Get-SourceCommit {
    $commit = (& git rev-parse --verify HEAD 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-f]{40}$') { throw 'Could not resolve an immutable source commit.' }
    return $commit
}

function Assert-ExactCleanSource([string]$ExpectedCommit) {
    $actualCommit = Get-SourceCommit
    if ($actualCommit -cne $ExpectedCommit) { throw "Source HEAD changed during candidate build. Expected $ExpectedCommit, got $actualCommit." }
    $dirty = @(git status --porcelain --untracked-files=all)
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect Git state.' }
    if ($dirty.Count -ne 0) { throw 'Source worktree changed during candidate build; refusing to package a non-exact candidate.' }
}

function Remove-PublishedCandidateFile([string]$Path, [string]$Label) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) { return }
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Label is a reparse point; refusing to remove it." }
    Remove-Item -LiteralPath $Path -Force -ErrorAction Stop
}

foreach ($requiredFile in @($GStreamerInstaller, $CaptionDecoderArchive, $InnoCompiler, $contract, $innoScript, $cargoWrapper)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) { throw "Required installer input is missing: $requiredFile" }
}
Assert-CanonicalPath $GStreamerInstaller $expectedGStreamerInstallerPath 'GStreamer installer'
Assert-CanonicalPath $CaptionDecoderArchive $expectedCaptionDecoderArchivePath 'Caption decoder archive'
Assert-CanonicalPath $InnoCompiler $expectedInnoPath 'Inno compiler'

Push-Location $repository
try {
    $sourceCommit = Get-SourceCommit
    if (-not $AllowDirty) { Assert-ExactCleanSource $sourceCommit }
}
finally { Pop-Location }

$configuredCargo = [Environment]::GetEnvironmentVariable('TOWNLIGHT_CARGO_EXECUTABLE')
$configuredGStreamer = [Environment]::GetEnvironmentVariable('TOWNLIGHT_GSTREAMER_ROOT')
if (-not [string]::IsNullOrWhiteSpace($configuredCargo)) { Assert-CanonicalPath $configuredCargo $expectedCargoPath 'TOWNLIGHT_CARGO_EXECUTABLE' }
if (-not [string]::IsNullOrWhiteSpace($configuredGStreamer)) { Assert-CanonicalPath $configuredGStreamer $expectedGStreamerRoot 'TOWNLIGHT_GSTREAMER_ROOT' }

$runtimeHash = (Get-FileHash -LiteralPath $GStreamerInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
if ($runtimeHash -ne $expectedRuntimeHash) { throw "GStreamer runtime hash mismatch. Expected $expectedRuntimeHash, got $runtimeHash." }
$captionDecoderArchiveHash = (Get-FileHash -LiteralPath $CaptionDecoderArchive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($captionDecoderArchiveHash -ne $expectedCaptionDecoderArchiveHash) { throw "Caption decoder archive hash mismatch. Expected $expectedCaptionDecoderArchiveHash, got $captionDecoderArchiveHash." }

$captionDecoderStage = Join-Path ([IO.Path]::GetTempPath()) ("TownLight-caption-decoder-" + [guid]::NewGuid().ToString('N'))
$captionDecoder = Join-Path $captionDecoderStage 'ffmpeg.exe'
$captionDecoderLicense = Join-Path $captionDecoderStage 'FFmpeg-LICENSE.txt'
$releaseRunDirectory = Join-Path (Join-Path $outputDirectory '.candidate-runs') ("run-" + [guid]::NewGuid().ToString('N'))
$captionStageOwned = $false
$releaseRunOwned = $false
$locationPushed = $false
$priorCargoExecutable = [Environment]::GetEnvironmentVariable('TOWNLIGHT_CARGO_EXECUTABLE')
$priorGStreamerRoot = [Environment]::GetEnvironmentVariable('TOWNLIGHT_GSTREAMER_ROOT')
$priorPath = $env:PATH
$priorVctoolsInstallDir = $env:VCToolsInstallDir
$priorVctoolsVersion = $env:VCToolsVersion
$priorWindowsSdkDir = $env:WindowsSdkDir
$priorWindowsSdkVersion = $env:WindowsSDKVersion
$priorInclude = $env:INCLUDE
$priorLib = $env:LIB
$priorCargoHttpCheckRevoke = $env:CARGO_HTTP_CHECK_REVOKE
$priorRustc = $env:RUSTC
$priorRustupToolchain = $env:RUSTUP_TOOLCHAIN
$buildMutex = [Threading.Mutex]::new($false, 'Global\TownLightStation.InstallerBuild.v1')
$buildMutexHeld = $false
try {
    try {
        $buildMutexHeld = $buildMutex.WaitOne([TimeSpan]::FromMinutes(5))
    }
    catch [Threading.AbandonedMutexException] {
        throw 'The installer build mutex was abandoned; refusing publication until a clean retry.'
    }
    if (-not $buildMutexHeld) { throw 'Timed out waiting for the installer build mutex.' }

    # Only the exact two public candidate names are stale-output cleanup targets.
    # This lock remains held until the matching manifest is published and rehashed.
    if (Test-Path -LiteralPath $outputDirectory -PathType Container) {
        $outputItem = Get-Item -LiteralPath $outputDirectory -Force -ErrorAction Stop
        if (($outputItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Output directory is a reparse point; refusing publication.' }
        Remove-PublishedCandidateFile $publishedInstaller 'Published installer'
        Remove-PublishedCandidateFile $publishedManifest 'Published manifest'
    }

    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $tempRootItem = Get-Item -LiteralPath $tempRoot -Force -ErrorAction Stop
    if (($tempRootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Temporary root is a reparse point; refusing extraction.' }
    Assert-ContainedPath $captionDecoderStage $tempRoot 'Caption decoder stage'
    New-Item -ItemType Directory -Path $captionDecoderStage -ErrorAction Stop | Out-Null
    $captionStageOwned = $true
    $runParent = Split-Path $releaseRunDirectory -Parent
    New-Item -ItemType Directory -Path $runParent -Force -ErrorAction Stop | Out-Null
    $runParentItem = Get-Item -LiteralPath $runParent -Force -ErrorAction Stop
    if (($runParentItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Release run parent is a reparse point; refusing publication.' }
    New-Item -ItemType Directory -Path $releaseRunDirectory -ErrorAction Stop | Out-Null
    $releaseRunOwned = $true

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($CaptionDecoderArchive)
    try {
        $decoderEntry = $archive.GetEntry("$captionDecoderArchiveRoot/bin/ffmpeg.exe")
        $licenseEntry = $archive.GetEntry("$captionDecoderArchiveRoot/LICENSE.txt")
        if ($null -eq $decoderEntry -or $decoderEntry.Length -ne $expectedCaptionDecoderSize -or $null -eq $licenseEntry -or $licenseEntry.Length -ne $expectedCaptionDecoderLicenseSize) { throw 'Caption decoder archive does not contain the exact bounded executable and license payload.' }
        [IO.Compression.ZipFileExtensions]::ExtractToFile($decoderEntry, $captionDecoder, $false)
        [IO.Compression.ZipFileExtensions]::ExtractToFile($licenseEntry, $captionDecoderLicense, $false)
    }
    finally { $archive.Dispose() }

    $captionDecoderHash = (Get-FileHash -LiteralPath $captionDecoder -Algorithm SHA256).Hash.ToLowerInvariant()
    $captionDecoderLicenseHash = (Get-FileHash -LiteralPath $captionDecoderLicense -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($captionDecoderHash -ne $expectedCaptionDecoderHash -or $captionDecoderLicenseHash -ne $expectedCaptionDecoderLicenseHash) { throw 'Extracted caption decoder payload does not match its pinned executable and license digests.' }

    [Environment]::SetEnvironmentVariable('TOWNLIGHT_CARGO_EXECUTABLE', $expectedCargoPath, 'Process')
    [Environment]::SetEnvironmentVariable('TOWNLIGHT_GSTREAMER_ROOT', $expectedGStreamerRoot, 'Process')
    $env:RUSTC = $expectedRustcPath
    $env:RUSTUP_TOOLCHAIN = $expectedRustToolchain
    $innoIdentity = Get-InnoIdentity $InnoCompiler
    $rustIdentity = Get-RustIdentity $expectedRustcPath 'rustc' $expectedRustcVersion $expectedRustcCommit
    $cargoIdentity = Get-RustIdentity $expectedCargoPath 'cargo' $expectedCargoVersion $expectedCargoCommit
    $msvcIdentity = Get-MsvcIdentity
    $gstreamerIdentityBefore = Get-GStreamerIdentity $expectedGStreamerRoot
    $msvcBin = Join-Path $expectedMsvcRoot 'bin\Hostx64\x64'
    $sdkBin = Join-Path $expectedWindowsSdkRoot "bin\$expectedWindowsSdkVersion\x64"
    $env:PATH = "$msvcBin;$sdkBin;$priorPath"
    $env:VCToolsInstallDir = $expectedMsvcRoot
    $env:VCToolsVersion = '14.44.35207'
    $env:WindowsSdkDir = $expectedWindowsSdkRoot
    $env:WindowsSDKVersion = "$expectedWindowsSdkVersion\"
    $env:INCLUDE = "$(Join-Path $expectedMsvcRoot 'include');$(Join-Path $expectedWindowsSdkRoot "Include\$expectedWindowsSdkVersion\shared");$(Join-Path $expectedWindowsSdkRoot "Include\$expectedWindowsSdkVersion\um");$(Join-Path $expectedWindowsSdkRoot "Include\$expectedWindowsSdkVersion\ucrt");$(Join-Path $expectedWindowsSdkRoot "Include\$expectedWindowsSdkVersion\winrt");$priorInclude"
    $env:LIB = "$(Join-Path $expectedMsvcRoot 'lib\x64');$(Join-Path $expectedWindowsSdkRoot "Lib\$expectedWindowsSdkVersion\um\x64");$(Join-Path $expectedWindowsSdkRoot "Lib\$expectedWindowsSdkVersion\ucrt\x64");$priorLib"
    Push-Location $repository
    $locationPushed = $true
    & $contract
    Push-Location $operatorRoot
    try {
        npm ci --ignore-scripts --no-audit
        if ($LASTEXITCODE -ne 0) { throw 'Operator console dependency restore failed.' }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Operator console production build failed.' }
    }
    finally { Pop-Location }
    if (-not $AllowDirty) { Assert-ExactCleanSource $sourceCommit }
    $gstreamerIdentityAfterFrontend = Get-GStreamerIdentity $expectedGStreamerRoot
    if ((ConvertTo-Json $gstreamerIdentityBefore -Compress) -cne (ConvertTo-Json $gstreamerIdentityAfterFrontend -Compress)) { throw 'GStreamer compile inputs changed during the operator build.' }

    $env:CARGO_HTTP_CHECK_REVOKE = 'false'
    $cargoBuild = Invoke-CargoWrapperCapture -Arguments @('build', '--release', '--offline', '-p', 'stationd', '-p', 'channel-worker', '-p', 'station-updater')
    if (-not [string]::IsNullOrWhiteSpace($cargoBuild.standard_output)) { Write-Host $cargoBuild.standard_output }
    if (-not [string]::IsNullOrWhiteSpace($cargoBuild.standard_error)) { Write-Host $cargoBuild.standard_error }
    foreach ($binary in @($stationd, $channelWorker, $stationUpdater)) {
        if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) { throw "Release binary is missing: $binary" }
    }
    $gstreamerIdentityAfterCargo = Get-GStreamerIdentity $expectedGStreamerRoot
    if ((ConvertTo-Json $gstreamerIdentityBefore -Compress) -cne (ConvertTo-Json $gstreamerIdentityAfterCargo -Compress)) { throw 'GStreamer compile inputs changed during the native build.' }
    if (-not $AllowDirty) { Assert-ExactCleanSource $sourceCommit }

    $stationdHash = (Get-FileHash -LiteralPath $stationd -Algorithm SHA256).Hash.ToLowerInvariant()
    $workerHash = (Get-FileHash -LiteralPath $channelWorker -Algorithm SHA256).Hash.ToLowerInvariant()
    $updaterHash = (Get-FileHash -LiteralPath $stationUpdater -Algorithm SHA256).Hash.ToLowerInvariant()
    $stationdSize = (Get-Item -LiteralPath $stationd).Length
    $workerSize = (Get-Item -LiteralPath $channelWorker).Length
    $updaterSize = (Get-Item -LiteralPath $stationUpdater).Length

    & $InnoCompiler "/DSourceCommit=$sourceCommit" "/DStationdPath=$stationd" "/DChannelWorkerPath=$channelWorker" "/DStationUpdaterPath=$stationUpdater" "/DStationdSha256=$stationdHash" "/DChannelWorkerSha256=$workerHash" "/DStationUpdaterSha256=$updaterHash" "/DStationdSize=$stationdSize" "/DChannelWorkerSize=$workerSize" "/DStationUpdaterSize=$updaterSize" "/DGStreamerInstaller=$GStreamerInstaller" "/DCaptionDecoderPath=$captionDecoder" "/DCaptionDecoderLicensePath=$captionDecoderLicense" "/DCaptionDecoderArchiveSha256=$captionDecoderArchiveHash" "/DCaptionDecoderSha256=$captionDecoderHash" "/DCaptionDecoderSize=$expectedCaptionDecoderSize" "/DCaptionDecoderLicenseSha256=$captionDecoderLicenseHash" "/DCaptionDecoderLicenseSize=$expectedCaptionDecoderLicenseSize" "/DOutputDirectory=$releaseRunDirectory" $innoScript
    if ($LASTEXITCODE -ne 0) { throw 'Inno Setup compilation failed.' }
    $stagedInstaller = Join-Path $releaseRunDirectory $installerName
    if (-not (Test-Path -LiteralPath $stagedInstaller -PathType Leaf)) { throw "Expected installer was not produced: $stagedInstaller" }
    $stagedInstallerHash = (Get-FileHash -LiteralPath $stagedInstaller -Algorithm SHA256).Hash.ToLowerInvariant()

    $manifest = [ordered]@{
        product = 'TownLight Station'
        version = '0.1.0'
        source_commit = $sourceCommit
        installer = $installerName
        installer_sha256 = $stagedInstallerHash
        stationd_sha256 = $stationdHash
        channel_worker_sha256 = $workerHash
        station_updater_sha256 = $updaterHash
        media_runtime_sha256 = $runtimeHash
        caption_decoder_archive_sha256 = $captionDecoderArchiveHash
        caption_decoder_sha256 = $captionDecoderHash
        caption_decoder_size_bytes = $expectedCaptionDecoderSize
        caption_decoder_license_sha256 = $captionDecoderLicenseHash
        caption_decoder_license_size_bytes = $expectedCaptionDecoderLicenseSize
        build_toolchain = [ordered]@{ inno = $innoIdentity; rustc = $rustIdentity; cargo = $cargoIdentity; msvc = $msvcIdentity; gstreamer_compile = $gstreamerIdentityBefore }
    }
    $stagedManifest = Join-Path $releaseRunDirectory 'candidate-manifest.json'
    $manifestJson = $manifest | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText($stagedManifest, $manifestJson, (New-Object Text.UTF8Encoding($false)))
    if (-not $AllowDirty) { Assert-ExactCleanSource $sourceCommit }

    $stagedManifestDocument = Get-Content -LiteralPath $stagedManifest -Raw | ConvertFrom-Json -ErrorAction Stop
    $stagedInstallerHashForPublication = (Get-FileHash -LiteralPath $stagedInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($stagedManifestDocument.source_commit -cne $sourceCommit -or
        $stagedManifestDocument.installer -cne $installerName -or
        $stagedManifestDocument.installer_sha256 -cne $stagedInstallerHashForPublication) {
        throw 'Staged candidate manifest does not authenticate the exact staged installer.'
    }

    # Publish the installer first and its manifest last. No fallible operation is
    # allowed between the moves; any failure removes only these exact orphan names.
    $publishedInstallerMoved = $false
    try {
        Move-Item -LiteralPath $stagedInstaller -Destination $publishedInstaller -Force -ErrorAction Stop
        $publishedInstallerMoved = $true
        Move-Item -LiteralPath $stagedManifest -Destination $publishedManifest -Force -ErrorAction Stop

        $consumerManifest = Get-Content -LiteralPath $publishedManifest -Raw | ConvertFrom-Json -ErrorAction Stop
        $consumerInstallerHash = (Get-FileHash -LiteralPath $publishedInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($consumerManifest.source_commit -cne $sourceCommit -or
            $consumerManifest.installer -cne $installerName -or
            $consumerManifest.installer_sha256 -cne $consumerInstallerHash) {
            throw 'Published candidate manifest does not authenticate the exact published installer.'
        }
    }
    catch {
        Remove-PublishedCandidateFile $publishedManifest 'Failed publication manifest'
        if ($publishedInstallerMoved) { Remove-PublishedCandidateFile $publishedInstaller 'Failed publication installer' }
        throw
    }
    $manifestJson
}
finally {
    if ($buildMutexHeld) { $buildMutex.ReleaseMutex() }
    $buildMutex.Dispose()
    if ($locationPushed) { Pop-Location }
    [Environment]::SetEnvironmentVariable('TOWNLIGHT_CARGO_EXECUTABLE', $priorCargoExecutable, 'Process')
    [Environment]::SetEnvironmentVariable('TOWNLIGHT_GSTREAMER_ROOT', $priorGStreamerRoot, 'Process')
    $env:PATH = $priorPath
    $env:VCToolsInstallDir = $priorVctoolsInstallDir
    $env:VCToolsVersion = $priorVctoolsVersion
    $env:WindowsSdkDir = $priorWindowsSdkDir
    $env:WindowsSDKVersion = $priorWindowsSdkVersion
    $env:INCLUDE = $priorInclude
    $env:LIB = $priorLib
    $env:CARGO_HTTP_CHECK_REVOKE = $priorCargoHttpCheckRevoke
    $env:RUSTC = $priorRustc
    $env:RUSTUP_TOOLCHAIN = $priorRustupToolchain
    if ($captionStageOwned) {
        $stageItem = Get-Item -LiteralPath $captionDecoderStage -Force -ErrorAction SilentlyContinue
        if ($null -ne $stageItem -and ($stageItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) {
            Remove-Item -LiteralPath $captionDecoder -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $captionDecoderLicense -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $captionDecoderStage -Force -Recurse -ErrorAction SilentlyContinue
        }
    }
    if ($releaseRunOwned) {
        $runRoot = Join-Path (Join-Path $outputDirectory '.candidate-runs') ''
        $safeRun = Assert-ContainedPath $releaseRunDirectory $runRoot 'Release run directory'
        $runItem = Get-Item -LiteralPath $safeRun -Force -ErrorAction SilentlyContinue
        if ($null -ne $runItem -and ($runItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) {
            Remove-Item -LiteralPath $safeRun -Force -Recurse -ErrorAction SilentlyContinue
        }
    }
}
