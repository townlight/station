$ErrorActionPreference = 'Stop'

$repository = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $repository '.cargo\config.toml'
$runnerPath = Join-Path $repository '.cargo\run-with-gstreamer.ps1'
$cargoWrapperPath = Join-Path $repository 'scripts\invoke-cargo.ps1'

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw 'Cargo GStreamer runner configuration is missing.'
}
if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
    throw 'Cargo GStreamer runner script is missing.'
}
if (-not (Test-Path -LiteralPath $cargoWrapperPath -PathType Leaf)) {
    throw 'Cargo GStreamer process wrapper is missing.'
}

$config = (Get-Content -LiteralPath $configPath -Raw) -replace "`r`n", "`n"
if ($config -notmatch '(?m)^\[target\.x86_64-pc-windows-msvc\]$' -or
    $config -notmatch '(?m)^runner\s*=.*run-with-gstreamer\.ps1') {
    throw 'Cargo does not bind Windows MSVC executables to the GStreamer runner.'
}

$wrapper = Get-Content -LiteralPath $cargoWrapperPath -Raw
foreach ($requiredFragment in @(
    'gstpbutils-1.0-0.dll',
    'gobject-2.0-0.dll',
    'glib-2.0-0.dll',
    'SetErrorMode',
    'Push-Location -LiteralPath $repositoryRoot'
)) {
    if (-not $wrapper.Contains($requiredFragment)) {
        throw "Cargo process wrapper is missing the required protection '$requiredFragment'."
    }
}

$runner = Get-Content -LiteralPath $runnerPath -Raw
foreach ($requiredDll in @(
    'gstreamer-1.0-0.dll',
    'gstpbutils-1.0-0.dll',
    'gobject-2.0-0.dll',
    'glib-2.0-0.dll'
)) {
    if (-not $runner.Contains($requiredDll)) {
        throw "Cargo target runner does not preflight '$requiredDll'."
    }
}

$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) "townlight-cargo-runner-$([guid]::NewGuid().ToString('N'))"
$runtimeRoot = Join-Path $temporaryRoot 'runtime'
$bin = Join-Path $runtimeRoot 'bin'
$plugins = Join-Path $runtimeRoot 'lib\gstreamer-1.0'
$probe = Join-Path $temporaryRoot 'probe.ps1'

New-Item -ItemType Directory -Path $bin -Force | Out-Null
New-Item -ItemType Directory -Path $plugins -Force | Out-Null
foreach ($requiredDll in @(
    'gstreamer-1.0-0.dll',
    'gstpbutils-1.0-0.dll',
    'gobject-2.0-0.dll',
    'glib-2.0-0.dll'
)) {
    [IO.File]::WriteAllBytes((Join-Path $bin $requiredDll), [byte[]]@(1))
}
[IO.File]::WriteAllText(
    $probe,
    @'
param([string]$ExpectedBin, [string]$ExpectedPlugins)
$paths = $env:PATH -split ';'
if ($ExpectedBin -notin $paths) {
    [Console]::Error.WriteLine("PATH did not contain the explicit runtime bin '$ExpectedBin'.")
    exit 11
}
if ($env:GST_PLUGIN_PATH_1_0 -ne $ExpectedPlugins) {
    [Console]::Error.WriteLine("GST_PLUGIN_PATH_1_0 did not equal '$ExpectedPlugins'.")
    exit 12
}
if ($env:GST_PLUGIN_SYSTEM_PATH_1_0 -ne $ExpectedPlugins) {
    [Console]::Error.WriteLine("GST_PLUGIN_SYSTEM_PATH_1_0 did not equal '$ExpectedPlugins'.")
    exit 13
}
exit 0
'@,
    [Text.UTF8Encoding]::new($false)
)

$priorRoot = $env:TOWNLIGHT_GSTREAMER_ROOT
$priorCargoExecutable = $env:TOWNLIGHT_CARGO_EXECUTABLE
try {
    $env:TOWNLIGHT_GSTREAMER_ROOT = $runtimeRoot
    & $runnerPath (Get-Command pwsh.exe -ErrorAction Stop).Source -NoProfile -NonInteractive `
        -File $probe $bin $plugins
    if ($LASTEXITCODE -ne 0) {
        throw "The Cargo GStreamer runner did not propagate the exact runtime environment (exit $LASTEXITCODE)."
    }

    $env:TOWNLIGHT_CARGO_EXECUTABLE = (Get-Command pwsh.exe -ErrorAction Stop).Source
    & $cargoWrapperPath -NoProfile -NonInteractive -File $probe $bin $plugins
    if ($LASTEXITCODE -ne 0) {
        throw "The Cargo process wrapper did not propagate the exact runtime environment (exit $LASTEXITCODE)."
    }

    Remove-Item -LiteralPath (Join-Path $bin 'gstpbutils-1.0-0.dll') -Force
    $rejectedIncompleteRunnerRuntime = $false
    try {
        & $runnerPath (Get-Command pwsh.exe -ErrorAction Stop).Source -NoProfile -NonInteractive `
            -Command 'exit 0' *> $null
    } catch {
        $rejectedIncompleteRunnerRuntime = $true
    }
    if (-not $rejectedIncompleteRunnerRuntime) {
        throw 'The Cargo GStreamer runner launched with an incomplete native runtime.'
    }

    $rejectedIncompleteWrapperRuntime = $false
    try {
        & $cargoWrapperPath -NoProfile -NonInteractive -Command 'exit 0' *> $null
    } catch {
        $rejectedIncompleteWrapperRuntime = $true
    }
    if (-not $rejectedIncompleteWrapperRuntime) {
        throw 'The Cargo process wrapper launched with an incomplete native runtime.'
    }

    $env:TOWNLIGHT_GSTREAMER_ROOT = Join-Path $temporaryRoot 'missing'
    $rejectedMissingRuntime = $false
    try {
        & $runnerPath (Get-Command pwsh.exe -ErrorAction Stop).Source -NoProfile -NonInteractive `
            -Command 'exit 0' *> $null
    } catch {
        $rejectedMissingRuntime = $true
    }
    if (-not $rejectedMissingRuntime) {
        throw 'The Cargo GStreamer runner launched with a missing explicit runtime.'
    }

    $rejectedMissingWrapperRuntime = $false
    try {
        & $cargoWrapperPath -NoProfile -NonInteractive -Command 'exit 0' *> $null
    } catch {
        $rejectedMissingWrapperRuntime = $true
    }
    if (-not $rejectedMissingWrapperRuntime) {
        throw 'The Cargo process wrapper launched with a missing explicit runtime.'
    }
}
finally {
    $env:TOWNLIGHT_GSTREAMER_ROOT = $priorRoot
    $env:TOWNLIGHT_CARGO_EXECUTABLE = $priorCargoExecutable
    Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output 'Cargo GStreamer runner contract passed.'
