param(
    [string]$GStreamerInstaller = (Join-Path $env:LOCALAPPDATA 'Temp\TownLightStation-Toolchain-20260820\gstreamer-1.0-msvc-x86_64-1.28.6.exe'),
    [string]$InnoCompiler = (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
    [switch]$AllowDirty
)

$ErrorActionPreference = 'Stop'
$expectedRuntimeHash = '059251444d1267b486eba390b18d25fed87e10315e72f757ec6c7e912fa746b5'
$repository = Split-Path $PSScriptRoot -Parent
$outputDirectory = Join-Path $repository 'dist'
$stationd = Join-Path $repository 'target\release\stationd.exe'
$channelWorker = Join-Path $repository 'target\release\channel-worker.exe'
$stationUpdater = Join-Path $repository 'target\release\station-updater.exe'
$contract = Join-Path $PSScriptRoot 'verify-contract.ps1'
$innoScript = Join-Path $PSScriptRoot 'TownLightStation.iss'
$operatorRoot = Join-Path $repository 'web\operator'
$cargoWrapper = Join-Path $repository 'scripts\invoke-cargo.ps1'

foreach ($requiredFile in @($GStreamerInstaller, $InnoCompiler, $contract, $innoScript, $cargoWrapper)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required installer input is missing: $requiredFile"
    }
}

$runtimeHash = (Get-FileHash -LiteralPath $GStreamerInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
if ($runtimeHash -ne $expectedRuntimeHash) {
    throw "GStreamer runtime hash mismatch. Expected $expectedRuntimeHash, got $runtimeHash."
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
    & $InnoCompiler "/DSourceCommit=$sourceCommit" "/DStationdPath=$stationd" "/DChannelWorkerPath=$channelWorker" "/DStationUpdaterPath=$stationUpdater" "/DStationdSha256=$stationdHash" "/DChannelWorkerSha256=$workerHash" "/DStationUpdaterSha256=$updaterHash" "/DStationdSize=$stationdSize" "/DChannelWorkerSize=$workerSize" "/DStationUpdaterSize=$updaterSize" "/DGStreamerInstaller=$GStreamerInstaller" "/DOutputDirectory=$outputDirectory" $innoScript
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
    }
    $manifestPath = Join-Path $outputDirectory 'candidate-manifest.json'
    $manifestJson = $manifest | ConvertTo-Json
    [IO.File]::WriteAllText($manifestPath, $manifestJson, (New-Object Text.UTF8Encoding($false)))
    $manifestJson
}
finally {
    Pop-Location
}
