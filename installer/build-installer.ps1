param(
    [string]$GStreamerInstaller = (Join-Path $env:LOCALAPPDATA 'Temp\TownLightStation-Toolchain-20260820\gstreamer-1.0-msvc-x86_64-1.28.6.exe'),
    [string]$InnoCompiler = (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
    [switch]$AllowDirty
)

$ErrorActionPreference = 'Stop'
$expectedRuntimeHash = '059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5'
$repository = Split-Path $PSScriptRoot -Parent
$outputDirectory = Join-Path $repository 'dist'
$stationd = Join-Path $repository 'target\release\stationd.exe'
$channelWorker = Join-Path $repository 'target\release\channel-worker.exe'
$contract = Join-Path $PSScriptRoot 'verify-contract.ps1'
$innoScript = Join-Path $PSScriptRoot 'TownLightStation.iss'

foreach ($requiredFile in @($GStreamerInstaller, $InnoCompiler, $contract, $innoScript)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required installer input is missing: $requiredFile"
    }
}

$runtimeHash = (Get-FileHash -LiteralPath $GStreamerInstaller -Algorithm SHA256).Hash
if ($runtimeHash -ne $expectedRuntimeHash) {
    throw "GStreamer runtime hash mismatch. Expected $expectedRuntimeHash, got $runtimeHash."
}

Push-Location $repository
try {
    & $contract

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
    $gstreamerBin = Join-Path $env:LOCALAPPDATA 'Programs\gstreamer\1.0\msvc_x86_64\bin'
    $env:PATH = $gstreamerBin + ';' + $env:PATH
    cargo build --release --offline -p stationd -p channel-worker
    if ($LASTEXITCODE -ne 0) {
        throw 'Release binary build failed.'
    }

    foreach ($binary in @($stationd, $channelWorker)) {
        if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) {
            throw "Release binary is missing: $binary"
        }
    }

    $sourceCommit = (git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $sourceCommit -notmatch '^[0-9a-f]{40}$') {
        throw 'Could not resolve an immutable source commit.'
    }

    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
    & $InnoCompiler "/DSourceCommit=$sourceCommit" "/DStationdPath=$stationd" "/DChannelWorkerPath=$channelWorker" "/DGStreamerInstaller=$GStreamerInstaller" "/DOutputDirectory=$outputDirectory" $innoScript
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
        installer_sha256 = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash
        stationd_sha256 = (Get-FileHash -LiteralPath $stationd -Algorithm SHA256).Hash
        channel_worker_sha256 = (Get-FileHash -LiteralPath $channelWorker -Algorithm SHA256).Hash
        media_runtime_sha256 = $runtimeHash
    }
    $manifestPath = Join-Path $outputDirectory 'candidate-manifest.json'
    $manifest | ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding utf8NoBOM
    $manifest | ConvertTo-Json
}
finally {
    Pop-Location
}
