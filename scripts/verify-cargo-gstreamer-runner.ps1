$ErrorActionPreference = 'Stop'

$repository = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $repository '.cargo\config.toml'
$runnerPath = Join-Path $repository '.cargo\run-with-gstreamer.ps1'
$publisherPath = Join-Path $repository '.cargo\TownLightCargoArtifactRuntime.cs'
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
if (-not (Test-Path -LiteralPath $publisherPath -PathType Leaf)) {
    throw 'Cargo artifact runtime publisher is missing.'
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
    'Push-Location -LiteralPath $repositoryRoot',
    'Sync-TownLightGStreamerArtifactRuntime',
    'CARGO_TARGET_DIR',
    'TownLightCargoArtifactRuntimePublisher',
    'PublishClosure'
)) {
    if (-not $wrapper.Contains($requiredFragment)) {
        throw "Cargo process wrapper is missing the required protection '$requiredFragment'."
    }
}
if ($wrapper -match 'HardLink') {
    throw 'Cargo process wrapper must not hard-link writable target output to the installed GStreamer runtime.'
}
$publisher = Get-Content -LiteralPath $publisherPath -Raw
foreach ($requiredFragment in @(
    'FileFlagOpenReparsePoint',
    'NtCreateFile',
    'NtSetInformationFile',
    'RootDirectory',
    'GetFileInformationByHandle',
    'SameIdentity',
    'ProvePinnedDirectorySwapCannotRedirect',
    'PinDirectoryAncestryNoFollow',
    'NumberOfLinks == 1',
    'ProveCheckedTemporaryCleanup'
)) {
    if (-not $publisher.Contains($requiredFragment)) {
        throw "Cargo artifact runtime publisher is missing '$requiredFragment'."
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
$fakeCargo = Join-Path $temporaryRoot 'fake-cargo.ps1'
$targetRoot = Join-Path $temporaryRoot 'target'
$artifactDirectory = Join-Path $targetRoot 'debug\deps'
$artifact = Join-Path $artifactDirectory 'contained-direct-artifact.exe'
$buildScriptDirectory = Join-Path $targetRoot 'debug\build\ignored'

New-Item -ItemType Directory -Path $bin -Force | Out-Null
New-Item -ItemType Directory -Path $plugins -Force | Out-Null
foreach ($requiredDll in @(
    'gstreamer-1.0-0.dll',
    'gstpbutils-1.0-0.dll',
    'gobject-2.0-0.dll',
    'glib-2.0-0.dll',
    'gdk_pixbuf-2.0-0.dll'
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
[IO.File]::WriteAllText(
    $fakeCargo,
    @'
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CargoArguments)
if ($CargoArguments -contains 'clean') {
    Remove-Item -LiteralPath $env:TOWNLIGHT_CARGO_TEST_ARTIFACT_DIRECTORY -Recurse -Force -ErrorAction SilentlyContinue
    exit 0
}
$artifactDirectory = $env:TOWNLIGHT_CARGO_TEST_ARTIFACT_DIRECTORY
[IO.Directory]::CreateDirectory($artifactDirectory) | Out-Null
Copy-Item -LiteralPath $env:ComSpec -Destination (Join-Path $artifactDirectory 'contained-direct-artifact.exe') -Force
[IO.Directory]::CreateDirectory($env:TOWNLIGHT_CARGO_TEST_BUILD_SCRIPT_DIRECTORY) | Out-Null
Copy-Item -LiteralPath $env:ComSpec -Destination (Join-Path $env:TOWNLIGHT_CARGO_TEST_BUILD_SCRIPT_DIRECTORY 'build-script-build.exe') -Force
exit 0
'@,
    [Text.UTF8Encoding]::new($false)
)

$priorRoot = $env:TOWNLIGHT_GSTREAMER_ROOT
$priorCargoExecutable = $env:TOWNLIGHT_CARGO_EXECUTABLE
$priorTargetRoot = $env:CARGO_TARGET_DIR
$priorArtifactDirectory = $env:TOWNLIGHT_CARGO_TEST_ARTIFACT_DIRECTORY
$priorBuildScriptDirectory = $env:TOWNLIGHT_CARGO_TEST_BUILD_SCRIPT_DIRECTORY
try {
    $env:TOWNLIGHT_GSTREAMER_ROOT = $runtimeRoot
    # Keep every fake-runtime wrapper invocation in the temporary Cargo target.
    # Otherwise the environment propagation probe would deploy its one-byte fake
    # DLLs into this checkout's real target directory.
    $env:CARGO_TARGET_DIR = $targetRoot
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

    # This fake artifact proves deployment integrity only. It is deliberately
    # never launched; a real GStreamer-dependent loader proof is a separate,
    # root-controlled manual gate after exact runtime hash preflight.
    $env:TOWNLIGHT_CARGO_EXECUTABLE = (Get-Command pwsh.exe -ErrorAction Stop).Source
    $env:TOWNLIGHT_CARGO_TEST_ARTIFACT_DIRECTORY = $artifactDirectory
    $env:TOWNLIGHT_CARGO_TEST_BUILD_SCRIPT_DIRECTORY = $buildScriptDirectory
    & $cargoWrapperPath -NoProfile -NonInteractive -File $fakeCargo test --no-run
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $artifact -PathType Leaf)) {
        throw 'The Cargo process wrapper did not create the contained direct-launch artifact.'
    }
    foreach ($runtimeDll in @(Get-ChildItem -LiteralPath $bin -File -Filter '*.dll')) {
        $deployedDll = Join-Path $artifactDirectory $runtimeDll.Name
        if (-not (Test-Path -LiteralPath $deployedDll -PathType Leaf) -or
            (Get-FileHash -LiteralPath $deployedDll -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $runtimeDll.FullName -Algorithm SHA256).Hash) {
            throw "The complete GStreamer DLL closure was not independently deployed beside '$artifact'."
        }
    }
    if (Test-Path -LiteralPath (Join-Path $buildScriptDirectory 'gstreamer-1.0-0.dll') -PathType Leaf) {
        throw 'The Cargo process wrapper seeded the full runtime beside a build-script executable.'
    }
    if (-not ('TownLightCargoArtifactRuntimePublisher' -as [type])) {
        Add-Type -Path $publisherPath
    }
    [TownLightCargoArtifactRuntimePublisher]::ProvePinnedDirectorySwapCannotRedirect($artifactDirectory)
    [TownLightCargoArtifactRuntimePublisher]::ProveCheckedTemporaryCleanup($artifactDirectory)

    $sourceProbeDll = Join-Path $bin 'gstreamer-1.0-0.dll'
    $deployedProbeDll = Join-Path $artifactDirectory 'gstreamer-1.0-0.dll'
    $sourceHashBeforeMutation = (Get-FileHash -LiteralPath $sourceProbeDll -Algorithm SHA256).Hash
    [IO.File]::WriteAllBytes($deployedProbeDll, [byte[]]@(9, 9, 9))
    if ((Get-FileHash -LiteralPath $sourceProbeDll -Algorithm SHA256).Hash -ne $sourceHashBeforeMutation) {
        throw 'A writable target DLL aliases and can mutate the source GStreamer runtime.'
    }
    & $cargoWrapperPath -NoProfile -NonInteractive -File $fakeCargo test --no-run

    $externalProbeDll = Join-Path $temporaryRoot 'external-equal-content.dll'
    Copy-Item -LiteralPath $sourceProbeDll -Destination $externalProbeDll
    $externalHashBeforeMutation = (Get-FileHash -LiteralPath $externalProbeDll -Algorithm SHA256).Hash
    Remove-Item -LiteralPath $deployedProbeDll -Force
    New-Item -ItemType HardLink -Path $deployedProbeDll -Target $externalProbeDll | Out-Null
    & $cargoWrapperPath -NoProfile -NonInteractive -File $fakeCargo test --no-run
    [IO.File]::WriteAllBytes($deployedProbeDll, [byte[]]@(7, 7, 7))
    if ((Get-FileHash -LiteralPath $externalProbeDll -Algorithm SHA256).Hash -ne $externalHashBeforeMutation) {
        throw 'The Cargo process wrapper left an equal-content external hard link deployed.'
    }
    & $cargoWrapperPath -NoProfile -NonInteractive -File $fakeCargo test --no-run
    if ((Get-FileHash -LiteralPath $deployedProbeDll -Algorithm SHA256).Hash -ne $sourceHashBeforeMutation) {
        throw 'The Cargo process wrapper did not repair a modified deployed runtime DLL.'
    }
    $idempotentWriteTime = (Get-Item -LiteralPath $deployedProbeDll).LastWriteTimeUtc
    & $cargoWrapperPath -NoProfile -NonInteractive -File $fakeCargo test --no-run
    if ((Get-Item -LiteralPath $deployedProbeDll).LastWriteTimeUtc -ne $idempotentWriteTime) {
        throw 'The Cargo process wrapper rewrote an already verified deployed runtime DLL.'
    }
    Remove-Item -LiteralPath $deployedProbeDll -Force
    New-Item -ItemType HardLink -Path $deployedProbeDll -Target $sourceProbeDll | Out-Null
    & $cargoWrapperPath -NoProfile -NonInteractive -File $fakeCargo test --no-run
    [IO.File]::WriteAllBytes($deployedProbeDll, [byte[]]@(8, 8, 8))
    if ((Get-FileHash -LiteralPath $sourceProbeDll -Algorithm SHA256).Hash -ne $sourceHashBeforeMutation) {
        throw 'The Cargo process wrapper left a deployed runtime DLL hard-linked to the source runtime.'
    }
    & $cargoWrapperPath -NoProfile -NonInteractive -File $fakeCargo test --no-run

    & $cargoWrapperPath -NoProfile -NonInteractive -File $fakeCargo clean
    if (Test-Path -LiteralPath (Join-Path $artifactDirectory 'gdk_pixbuf-2.0-0.dll') -PathType Leaf) {
        throw 'The Cargo process wrapper seeded a runtime into a cargo clean artifact directory.'
    }

    $reparsePhysicalDirectory = Join-Path $temporaryRoot 'reparse-physical-artifact-directory'
    New-Item -ItemType Directory -Path (Split-Path -Parent $artifactDirectory) -Force | Out-Null
    New-Item -ItemType Directory -Path $reparsePhysicalDirectory -Force | Out-Null
    New-Item -ItemType Junction -Path $artifactDirectory -Target $reparsePhysicalDirectory | Out-Null
    $rejectedReparseArtifactDirectory = $false
    try {
        & $cargoWrapperPath -NoProfile -NonInteractive -File $fakeCargo test --no-run *> $null
    }
    catch {
        $rejectedReparseArtifactDirectory = $true
    }
    if (-not $rejectedReparseArtifactDirectory) {
        throw 'The Cargo process wrapper seeded a reparse-point artifact directory.'
    }

    [IO.Directory]::Delete($artifactDirectory)
    $ancestryPhysicalRoot = Join-Path $temporaryRoot 'ancestry-physical-target'
    $ancestryJunctionRoot = Join-Path $temporaryRoot 'ancestry-junction-target'
    New-Item -ItemType Directory -Path $ancestryPhysicalRoot -Force | Out-Null
    New-Item -ItemType Junction -Path $ancestryJunctionRoot -Target $ancestryPhysicalRoot | Out-Null
    $env:CARGO_TARGET_DIR = $ancestryJunctionRoot
    $env:TOWNLIGHT_CARGO_TEST_ARTIFACT_DIRECTORY = Join-Path $ancestryJunctionRoot 'debug\deps'
    $env:TOWNLIGHT_CARGO_TEST_BUILD_SCRIPT_DIRECTORY = Join-Path $ancestryJunctionRoot 'debug\build\ignored'
    $rejectedReparseAncestry = $false
    try {
        & $cargoWrapperPath -NoProfile -NonInteractive -File $fakeCargo test --no-run *> $null
    }
    catch {
        $rejectedReparseAncestry = $true
    }
    if (-not $rejectedReparseAncestry) {
        throw 'The Cargo process wrapper accepted a reparse-point target ancestry.'
    }
    $env:CARGO_TARGET_DIR = $targetRoot
    $env:TOWNLIGHT_CARGO_TEST_ARTIFACT_DIRECTORY = $artifactDirectory
    $env:TOWNLIGHT_CARGO_TEST_BUILD_SCRIPT_DIRECTORY = $buildScriptDirectory

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
    $env:CARGO_TARGET_DIR = $priorTargetRoot
    $env:TOWNLIGHT_CARGO_TEST_ARTIFACT_DIRECTORY = $priorArtifactDirectory
    $env:TOWNLIGHT_CARGO_TEST_BUILD_SCRIPT_DIRECTORY = $priorBuildScriptDirectory
    Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output 'Cargo GStreamer runner contract passed.'
