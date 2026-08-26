[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repository = Split-Path $PSScriptRoot -Parent
$buildPath = Join-Path $repository 'installer\build-installer.ps1'
$verifyPath = Join-Path $repository 'installer\verify-installed.ps1'
$assertions = 0

function Assert-Contract([bool]$Condition, [string]$Message) {
    $script:assertions++
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
}

function Read-ParsedSource([string]$Path) {
    Assert-Contract (Test-Path -LiteralPath $Path -PathType Leaf) "missing script: $Path"
    $tokens = $null
    $parseErrors = $null
    [Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$parseErrors) | Out-Null
    Assert-Contract ($parseErrors.Count -eq 0) "script must parse: $Path"
    return Get-Content -LiteralPath $Path -Raw
}

$build = Read-ParsedSource $buildPath
$verify = Read-ParsedSource $verifyPath
$knownFolderBootstrapEnd = $build.IndexOf('$expectedRuntimeHash', [StringComparison]::Ordinal)
Assert-Contract ($knownFolderBootstrapEnd -gt 0) 'build script must retain a known-folder bootstrap boundary.'
# Execute only the parameter/default/known-folder prefix. This proves the trusted
# folder initialization itself is invocation-safe without starting a build.
& ([scriptblock]::Create($build.Substring(0, $knownFolderBootstrapEnd)))
Assert-Contract $true 'trusted known-folder bootstrap must execute without starting an installer build.'

Assert-Contract ($build -match '\$expectedInnoHash\s*=\s*\x27[0-9a-f]{64}\x27' -and
    $build -match '\$expectedInnoSignerThumbprint\s*=\s*\x27[0-9A-F]{40}\x27' -and
    $build -match 'Get-InnoIdentity') 'Inno compiler must be pinned by canonical path, digest, and signature.'
Assert-Contract ($build -match 'Get-RustIdentity' -and $build -match 'Get-MsvcIdentity' -and
    $build -match 'Get-GStreamerIdentity' -and $build -match 'expectedCargoCommit' -and
    $build -match 'expectedWindowsSdkVersion') 'native toolchain identities must be explicit and recorded.'
Assert-Contract ($build -notmatch '\$host\s*=' -and $build -match '\$toolHost\s*=') `
    "Rust identity parsing must not assign PowerShell's read-only Host automatic variable."
Assert-Contract ($build -match '\.rustup\\toolchains\\' -and
    $build -match '\$expectedRustToolchainRoot' -and
    $build -match 'RUSTUP_HOME or RUSTUP_TOOLCHAIN environment drifted' -and
    $build -notmatch '\$env:RUSTUP_TOOLCHAIN\s*=') `
    'release Cargo and rustc must bind direct pinned toolchain binaries without Rustup proxy or environment drift.'
Assert-Contract ($build -match 'Get-TrustedKnownFolder' -and
    $build -match 'LOCALAPPDATA or USERPROFILE environment drifted' -and
    $build -match '\$env:RUSTC\s*=\s*\$expectedRustcPath' -and
    $build -match 'Invoke-CargoWrapperCapture' -and
    $build -match 'release' -and $build -match 'commit-hash' -and $build -match 'host') `
    'known folders, pinned Rust compiler selection, and named full Cargo/Rust identity fields must be enforced.'
Assert-Contract ($build -match 'function Invoke-CargoWrapperCapture' -and
    $build -match 'WaitForExit\(\)' -and $build -match 'RedirectStandardOutput' -and
    $build -match 'RedirectStandardError' -and $build -match 'StandardOutput\.ReadToEndAsync' -and
    $build -notmatch 'Process\.Kill\(' -and $build -notmatch 'TimeoutSeconds') `
    'every Cargo wrapper call must synchronously capture output without a forced child timeout that can leak descendants.'
Assert-Contract ($build -match 'Invoke-CargoWrapperCapture\s+-Arguments\s+@\(\x27build\x27,\s*\x27--release\x27,\s*\x27--offline\x27' -and
    $build -notmatch '& \$cargoWrapper build') `
    'the actual release build must use the synchronous child wrapper path rather than inheriting wrapper exit.'
$probeFunctionStart = $build.IndexOf('function Invoke-CargoWrapperCapture', [StringComparison]::Ordinal)
$probeFunctionEnd = $build.IndexOf('function Get-RustIdentity', [StringComparison]::Ordinal)
Assert-Contract ($probeFunctionStart -ge 0 -and $probeFunctionEnd -gt $probeFunctionStart) `
    'Cargo wrapper capture function must be independently runnable as a fixture boundary.'
$probeFixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ("townlight-cargo-probe-$([guid]::NewGuid().ToString('N'))")
try {
    New-Item -ItemType Directory -Path $probeFixtureRoot -ErrorAction Stop | Out-Null
    $fakeCargoWrapper = Join-Path $probeFixtureRoot 'invoke-cargo.ps1'
    [IO.File]::WriteAllText($fakeCargoWrapper, "Write-Output 'cargo-probe-output'`r`nexit 0`r`n", [Text.UTF8Encoding]::new($false))
    . ([scriptblock]::Create($build.Substring($probeFunctionStart, $probeFunctionEnd - $probeFunctionStart)))
    $cargoWrapper = $fakeCargoWrapper
    $probeOutput = Invoke-CargoWrapperCapture -Arguments @('--version', '--verbose')
    $returnedAfterCargoProbe = $true
    Assert-Contract ($returnedAfterCargoProbe -and $probeOutput.exit_code -eq 0 -and
        $probeOutput.standard_output -ceq 'cargo-probe-output' -and [string]::IsNullOrWhiteSpace($probeOutput.standard_error)) `
        'a wrapper exit must end only the bounded child and return captured output to the build script.'

    [IO.File]::WriteAllText($fakeCargoWrapper, @'
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CargoArguments)
if (($CargoArguments -join ' ') -cne 'build --release --offline -p stationd -p channel-worker -p station-updater') {
    Write-Error "unexpected cargo arguments: $($CargoArguments -join ' ')"
    exit 41
}
Write-Output 'cargo-release-output'
exit 0
'@, [Text.UTF8Encoding]::new($false))
    $releaseOutput = Invoke-CargoWrapperCapture -Arguments @('build', '--release', '--offline', '-p', 'stationd', '-p', 'channel-worker', '-p', 'station-updater')
    $publicationSentinel = 'reached'
    Assert-Contract ($publicationSentinel -ceq 'reached' -and $releaseOutput.exit_code -eq 0 -and
        $releaseOutput.standard_output -ceq 'cargo-release-output') `
        'a successful release-build wrapper exit must return to the build script so candidate publication can continue.'

    [IO.File]::WriteAllText($fakeCargoWrapper, "Write-Error 'cargo-release-failure'`r`nexit 17`r`n", [Text.UTF8Encoding]::new($false))
    $nonzeroFailure = $null
    try { Invoke-CargoWrapperCapture -Arguments @('build') | Out-Null }
    catch { $nonzeroFailure = $_.Exception.Message }
    Assert-Contract ($nonzeroFailure -match 'exit 17' -and $nonzeroFailure -match 'cargo-release-failure') `
        'a nonzero child-wrapper exit must fail the build path with its bounded diagnostic.'
}
finally {
    $probeFixture = Get-Item -LiteralPath $probeFixtureRoot -Force -ErrorAction SilentlyContinue
    if ($null -ne $probeFixture -and ($probeFixture.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) {
        Remove-Item -LiteralPath $probeFixtureRoot -Force -Recurse -ErrorAction Stop
    }
}
$rustIdentityStart = $build.IndexOf('function Get-RustIdentity', [StringComparison]::Ordinal)
$rustIdentityEnd = $build.IndexOf('function Get-MsvcIdentity', [StringComparison]::Ordinal)
Assert-Contract ($rustIdentityStart -ge 0 -and $rustIdentityEnd -gt $rustIdentityStart) `
    'Rust identity parser must remain independently runnable as a fixture boundary.'
$rustFixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ("townlight-rust-identity-$([guid]::NewGuid().ToString('N'))")
try {
    New-Item -ItemType Directory -Path $rustFixtureRoot -ErrorAction Stop | Out-Null
    $fakeCargo = Join-Path $rustFixtureRoot 'cargo.ps1'
    [IO.File]::WriteAllText($fakeCargo, "exit 0`r`n", [Text.UTF8Encoding]::new($false))
    function Get-CanonicalPath([string]$Path) { return (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path }
    function Assert-CanonicalPath([string]$Actual, [string]$Expected, [string]$Label) {
        if ($Actual -cne $Expected) { throw "$Label fixture path mismatch" }
    }
    function Get-FileIdentity([string]$Path) { [ordered]@{ path = $Path } }
    function Invoke-CargoWrapperCapture([string[]]$Arguments) {
        [pscustomobject]@{ standard_output = "cargo 1.97.1 (c980f4866141969fab6254a680546a277789d6f0 2026-01-01)`nrelease: 1.97.1`ncommit-hash: c980f4866141969fab6254a680546a277789d6f0`nhost: x86_64-pc-windows-msvc"; standard_error = ''; exit_code = 0 }
    }
    $expectedCargoPath = $fakeCargo
    $expectedRustcPath = $fakeCargo
    . ([scriptblock]::Create($build.Substring($rustIdentityStart, $rustIdentityEnd - $rustIdentityStart)))
    $rustIdentity = Get-RustIdentity $fakeCargo 'cargo' '1.97.1' 'c980f4866141969fab6254a680546a277789d6f0'
    Assert-Contract ($rustIdentity.version -ceq '1.97.1' -and $rustIdentity.commit -ceq 'c980f4866141969fab6254a680546a277789d6f0' -and $rustIdentity.host -ceq 'x86_64-pc-windows-msvc') `
        'realistic named Cargo release, full commit-hash, and host output must parse without touching automatic variables.'
}
finally {
    if (Test-Path -LiteralPath $rustFixtureRoot -PathType Container) { Remove-Item -LiteralPath $rustFixtureRoot -Recurse -Force -ErrorAction SilentlyContinue }
}
Assert-Contract ($build.IndexOf('$trustedLocalAppData = Get-TrustedKnownFolder', [StringComparison]::Ordinal) -ge 0 -and
    $build.IndexOf('$trustedLocalAppData = Get-TrustedKnownFolder', [StringComparison]::Ordinal) -lt
        $build.IndexOf('$expectedInnoPath = Join-Path $trustedLocalAppData', [StringComparison]::Ordinal) -and
    ($build.Substring(0, $build.IndexOf('$ErrorActionPreference', [StringComparison]::Ordinal)) -notmatch '\$env:')) `
    'trusted known-folder values must be established before defaults derive paths from them.'
Assert-Contract ($build -match 'configuredCargo' -and $build -match 'configuredGStreamer' -and
    $build -match 'Assert-CanonicalPath') 'wrapper overrides must be rejected unless they resolve to pinned paths.'
Assert-Contract (($build -split 'npm ci')[0].Contains('Assert-ExactCleanSource $sourceCommit')) `
    'the exact clean source snapshot must be captured before frontend build steps.'
Assert-Contract (($build | Select-String -AllMatches 'Assert-ExactCleanSource').Matches.Count -ge 4) `
    'the source snapshot must be rechecked before packaging and publication.'
Assert-Contract ($build -match 'Assert-ContainedPath \$captionDecoderStage' -and
    $build -match '\$archive = .*OpenRead' -and $build -match 'finally \{ \$archive\.Dispose\(\) \}') `
    'caption extraction must be containment-checked and archive-disposed on every path.'
Assert-Contract ($build -match '\.candidate-runs' -and $build -match '\$stagedInstaller' -and
    $build -match '\$stagedManifest' -and $build -match 'Move-Item -LiteralPath \$stagedInstaller' -and
    $build -match 'Move-Item -LiteralPath \$stagedManifest' -and
    $build -match 'Remove-PublishedCandidateFile' -and $build -match 'Global\\TownLightStation\.InstallerBuild\.v1' -and
    $build -match 'Get-FileHash -LiteralPath \$publishedInstaller') `
    'release outputs must be mutex-protected, rehashed, and published as a manifest-last matched pair.'
$mutexWait = $build.IndexOf('$buildMutex.WaitOne', [StringComparison]::Ordinal)
$staleCleanup = $build.IndexOf("Remove-PublishedCandidateFile `$publishedInstaller 'Published installer'", [StringComparison]::Ordinal)
$installerMove = $build.IndexOf('Move-Item -LiteralPath $stagedInstaller -Destination $publishedInstaller', [StringComparison]::Ordinal)
$manifestMove = $build.IndexOf('Move-Item -LiteralPath $stagedManifest -Destination $publishedManifest', [StringComparison]::Ordinal)
$publicationGap = if ($installerMove -ge 0 -and $manifestMove -gt $installerMove) {
    $build.Substring($installerMove, $manifestMove - $installerMove)
} else { '' }
Assert-Contract ($mutexWait -ge 0 -and $staleCleanup -gt $mutexWait -and
    $installerMove -gt $staleCleanup -and $manifestMove -gt $installerMove -and
    $publicationGap -notmatch 'Get-FileHash|Assert-ExactCleanSource|throw ' -and
    $build -match 'Remove-PublishedCandidateFile \$publishedManifest ''Failed publication manifest''' -and
    $build -match 'Remove-PublishedCandidateFile \$publishedInstaller ''Failed publication installer''') `
    'the mutex must cover exact stale cleanup and a rollback-protected, failure-free installer-to-manifest publication boundary.'
Assert-Contract ($verify -match '\$stationdHash[^\r\n]*ToLowerInvariant' -and
    $verify -match '\$workerHash[^\r\n]*ToLowerInvariant' -and
    $verify -match '\$updaterHash[^\r\n]*ToLowerInvariant') 'installed binary hashes must use manifest casing.'
Assert-Contract ($verify -match "Get-TrustedKnownFolder 'ProgramFilesX64'" -and
    $verify -match "Get-TrustedKnownFolder 'CommonApplicationData'" -and
    $verify -match '\$expectedStationPath' -and $verify -match '\$expectedUpdaterPath' -and
    $verify -notmatch 'PathName\s+-notlike') 'service identity must use canonical paths and exact command lines.'
Assert-Contract ($verify -match '\$candidateInstaller' -and
    $verify -match 'Get-FileHash -LiteralPath \$candidateInstaller' -and
    $verify -match 'installer_sha256') `
    'the installed verifier must rehash the manifest-named candidate installer before trusting it.'
Assert-Contract ($build -notmatch '(?i)invoke-owned-windows-sandbox|Start-Service|Stop-Service|Restart-Service|sc\.exe') `
    'the release build must not control services or Windows Sandbox.'

Write-Output "Installer build-safety harness passed ($assertions assertions; static/fixture-only, no product build, service, or Sandbox control; synchronous fake-wrapper child only)."
