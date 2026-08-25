param(
    [string]$GStreamerInstaller = (Join-Path $env:LOCALAPPDATA 'Temp\TownLightStation-Toolchain-20260820\gstreamer-1.0-msvc-x86_64-1.28.6.exe'),
    [string]$CaptionDecoderArchive = (Join-Path $env:LOCALAPPDATA 'Temp\TownLightStation-Toolchain-20260825\ffmpeg-n9.0.1-6-g9d4ca21220-win64-lgpl-9.0.zip'),
    [string]$InnoCompiler = (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
    [switch]$AllowDirty
)

$ErrorActionPreference = 'Stop'
$expectedRuntimeHash = '059251444d1267b486eba390b18d25fed87e10315e72f757ec6c7e912fa746b5'
$expectedCaptionDecoderArchiveHash = 'ccaa910db95451121482a5ebcb8c682123cb4f8a5bd5170fde4a814d7b437772'
$expectedCaptionDecoderHash = 'b6b61f0d99c55e422bbd59fad6880a8d4f96d99a6c865f3bb58c8f1ce3ff7b31'
$expectedCaptionDecoderSize = 114400768
$expectedCaptionDecoderLicenseHash = 'da7eabb7bafdf7d3ae5e9f223aa5bdc1eece45ac569dc21b3b037520b4464768'
$expectedCaptionDecoderLicenseSize = 7651
$captionDecoderArchiveRoot = 'ffmpeg-n9.0.1-6-g9d4ca21220-win64-lgpl-9.0'
$repository = Split-Path $PSScriptRoot -Parent
$outputDirectory = Join-Path $repository 'dist'
$stationd = Join-Path $repository 'target\release\stationd.exe'
$channelWorker = Join-Path $repository 'target\release\channel-worker.exe'
$stationUpdater = Join-Path $repository 'target\release\station-updater.exe'
$contract = Join-Path $PSScriptRoot 'verify-contract.ps1'
$innoScript = Join-Path $PSScriptRoot 'TownLightStation.iss'
$operatorRoot = Join-Path $repository 'web\operator'
$cargoWrapper = Join-Path $repository 'scripts\invoke-cargo.ps1'

foreach ($requiredFile in @($GStreamerInstaller, $CaptionDecoderArchive, $InnoCompiler, $contract, $innoScript, $cargoWrapper)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required installer input is missing: $requiredFile"
    }
}

$runtimeHash = (Get-FileHash -LiteralPath $GStreamerInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
if ($runtimeHash -ne $expectedRuntimeHash) {
    throw "GStreamer runtime hash mismatch. Expected $expectedRuntimeHash, got $runtimeHash."
}

$captionDecoderArchiveHash = (Get-FileHash -LiteralPath $CaptionDecoderArchive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($captionDecoderArchiveHash -ne $expectedCaptionDecoderArchiveHash) {
    throw "Caption decoder archive hash mismatch. Expected $expectedCaptionDecoderArchiveHash, got $captionDecoderArchiveHash."
}

$captionDecoderStage = Join-Path ([IO.Path]::GetTempPath()) ("TownLight-caption-decoder-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $captionDecoderStage | Out-Null
$captionDecoder = Join-Path $captionDecoderStage 'ffmpeg.exe'
$captionDecoderLicense = Join-Path $captionDecoderStage 'FFmpeg-LICENSE.txt'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead($CaptionDecoderArchive)
try {
    $decoderEntry = $archive.GetEntry("$captionDecoderArchiveRoot/bin/ffmpeg.exe")
    $licenseEntry = $archive.GetEntry("$captionDecoderArchiveRoot/LICENSE.txt")
    if ($null -eq $decoderEntry -or $decoderEntry.Length -ne $expectedCaptionDecoderSize -or
        $null -eq $licenseEntry -or $licenseEntry.Length -ne $expectedCaptionDecoderLicenseSize) {
        throw 'Caption decoder archive does not contain the exact bounded executable and license payload.'
    }
    [IO.Compression.ZipFileExtensions]::ExtractToFile($decoderEntry, $captionDecoder, $false)
    [IO.Compression.ZipFileExtensions]::ExtractToFile($licenseEntry, $captionDecoderLicense, $false)
}
finally {
    $archive.Dispose()
}
$captionDecoderHash = (Get-FileHash -LiteralPath $captionDecoder -Algorithm SHA256).Hash.ToLowerInvariant()
$captionDecoderLicenseHash = (Get-FileHash -LiteralPath $captionDecoderLicense -Algorithm SHA256).Hash.ToLowerInvariant()
if ($captionDecoderHash -ne $expectedCaptionDecoderHash -or
    $captionDecoderLicenseHash -ne $expectedCaptionDecoderLicenseHash) {
    throw 'Extracted caption decoder payload does not match its pinned executable and license digests.'
}

Push-Location $repository
try {
    & $contract

    Push-Location $operatorRoot
    try {
        npm ci --ignore-scripts --no-audit
        if ($LASTEXITCODE -ne 0) {
            throw 'Operator console dependency restore failed.'
        }
        npm run build
        if ($LASTEXITCODE -ne 0) {
            throw 'Operator console production build failed.'
        }
    }
    finally {
        Pop-Location
    }

    if (-not $AllowDirty) {
        $dirty = git status --porcelain
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not inspect Git state.'
        }
        if ($dirty) {
            throw 'Refusing a release installer from a dirty worktree. Commit or use -AllowDirty for a non-release proof.'
        }
    }

    $env:CARGO_HTTP_CHECK_REVOKE = 'false'
    & $cargoWrapper build --release --offline -p stationd -p channel-worker -p station-updater
    if ($LASTEXITCODE -ne 0) {
        throw 'Release binary build failed.'
    }

    foreach ($binary in @($stationd, $channelWorker, $stationUpdater)) {
        if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) {
            throw "Release binary is missing: $binary"
        }
    }

    $sourceCommit = (git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $sourceCommit -notmatch '^[0-9a-f]{40}$') {
        throw 'Could not resolve an immutable source commit.'
    }

    $stationdHash = (Get-FileHash -LiteralPath $stationd -Algorithm SHA256).Hash.ToLowerInvariant()
    $workerHash = (Get-FileHash -LiteralPath $channelWorker -Algorithm SHA256).Hash.ToLowerInvariant()
    $updaterHash = (Get-FileHash -LiteralPath $stationUpdater -Algorithm SHA256).Hash.ToLowerInvariant()
    $stationdSize = (Get-Item -LiteralPath $stationd).Length
    $workerSize = (Get-Item -LiteralPath $channelWorker).Length
    $updaterSize = (Get-Item -LiteralPath $stationUpdater).Length

    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
    & $InnoCompiler "/DSourceCommit=$sourceCommit" "/DStationdPath=$stationd" "/DChannelWorkerPath=$channelWorker" "/DStationUpdaterPath=$stationUpdater" "/DStationdSha256=$stationdHash" "/DChannelWorkerSha256=$workerHash" "/DStationUpdaterSha256=$updaterHash" "/DStationdSize=$stationdSize" "/DChannelWorkerSize=$workerSize" "/DStationUpdaterSize=$updaterSize" "/DGStreamerInstaller=$GStreamerInstaller" "/DCaptionDecoderPath=$captionDecoder" "/DCaptionDecoderLicensePath=$captionDecoderLicense" "/DCaptionDecoderArchiveSha256=$captionDecoderArchiveHash" "/DCaptionDecoderSha256=$captionDecoderHash" "/DCaptionDecoderSize=$expectedCaptionDecoderSize" "/DCaptionDecoderLicenseSha256=$captionDecoderLicenseHash" "/DCaptionDecoderLicenseSize=$expectedCaptionDecoderLicenseSize" "/DOutputDirectory=$outputDirectory" $innoScript
    if ($LASTEXITCODE -ne 0) {
        throw 'Inno Setup compilation failed.'
    }

    $installer = Join-Path $outputDirectory 'TownLight-Station-0.1.0-x64-setup.exe'
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        throw "Expected installer was not produced: $installer"
    }

    $manifest = [ordered]@{
        product = 'TownLight Station'
        version = '0.1.0'
        source_commit = $sourceCommit
        installer = [IO.Path]::GetFileName($installer)
        installer_sha256 = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
        stationd_sha256 = $stationdHash
        channel_worker_sha256 = $workerHash
        station_updater_sha256 = $updaterHash
        media_runtime_sha256 = $runtimeHash
        caption_decoder_archive_sha256 = $captionDecoderArchiveHash
        caption_decoder_sha256 = $captionDecoderHash
        caption_decoder_size_bytes = $expectedCaptionDecoderSize
        caption_decoder_license_sha256 = $captionDecoderLicenseHash
        caption_decoder_license_size_bytes = $expectedCaptionDecoderLicenseSize
    }
    $manifestPath = Join-Path $outputDirectory 'candidate-manifest.json'
    $manifestJson = $manifest | ConvertTo-Json
    [IO.File]::WriteAllText($manifestPath, $manifestJson, (New-Object Text.UTF8Encoding($false)))
    $manifestJson
}
finally {
    Pop-Location
    Remove-Item -LiteralPath $captionDecoder -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $captionDecoderLicense -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $captionDecoderStage -Force -ErrorAction SilentlyContinue
}
