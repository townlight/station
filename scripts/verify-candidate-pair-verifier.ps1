[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$helperPath = Join-Path $PSScriptRoot 'candidate-pair-verifier.ps1'
if (Test-Path -LiteralPath $helperPath -PathType Leaf) {
    . $helperPath
}

$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ("townlight-candidate-pair-test-" + [guid]::NewGuid().ToString('N'))
$manifestPath = Join-Path $fixtureRoot 'candidate-manifest.json'
$installerPath = Join-Path $fixtureRoot 'TownLight-Station-0.1.0-x64-setup.exe'
try {
    New-Item -ItemType Directory -Path $fixtureRoot -Force | Out-Null
    [IO.File]::WriteAllBytes($installerPath, [byte[]](1, 2, 3, 4))
    $installerHash = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $fileIdentity = [ordered]@{
        path = 'C:\tool.exe'
        size_bytes = 1
        sha256 = ('a' * 64)
        file_version = '1.0.0.0'
        product_version = '1.0.0.0'
        signer_status = 'Valid'
        signer_thumbprint = 'ABC123'
        signer_subject = 'CN=TownLight Fixture'
    }
    $unsignedFileIdentity = [ordered]@{
        path = 'C:\gst.h'
        size_bytes = 1
        sha256 = ('9' * 64)
        file_version = ''
        product_version = ''
        signer_status = 'NotSigned'
        signer_thumbprint = ''
        signer_subject = ''
    }
    $manifest = [ordered]@{
        product = 'TownLight Station'
        version = '0.1.0'
        source_commit = ('a' * 40)
        installer = 'TownLight-Station-0.1.0-x64-setup.exe'
        installer_sha256 = $installerHash
        stationd_sha256 = ('b' * 64)
        channel_worker_sha256 = ('c' * 64)
        nas_publisher_sha256 = ('e' * 64)
        station_updater_sha256 = ('d' * 64)
        media_runtime_sha256 = ('e' * 64)
        caption_decoder_archive_sha256 = ('f' * 64)
        caption_decoder_sha256 = ('0' * 64)
        caption_decoder_size_bytes = 4
        caption_decoder_license_sha256 = ('1' * 64)
        caption_decoder_license_size_bytes = 4
        tsduck_version = '3.44-4676'
        tsduck_source_url = 'https://github.com/tsduck/tsduck/releases/download/v3.44-4676/TSDuck-Win64-3.44-4676-Portable.zip'
        tsduck_archive_name = 'TSDuck-Win64-3.44-4676-Portable.zip'
        tsduck_runtime_sha256 = 'b0ca0f963fcd77488b8c32d6f9d85030daa891ed753c36e57bbb458807a44eb1'
        build_toolchain = [ordered]@{
            inno = [ordered]@{ path = 'C:\iscc.exe'; size_bytes = 1; sha256 = ('b' * 64); signer_thumbprint = 'ABC123'; signer_subject = 'CN=TownLight Fixture' }
            rustc = [ordered]@{ path = 'C:\rustc.exe'; version = '1.0.0'; commit = ('a' * 40); host = 'x86_64-pc-windows-msvc'; executable = $fileIdentity }
            cargo = [ordered]@{ path = 'C:\cargo.exe'; version = '1.0.0'; commit = ('a' * 40); host = 'x86_64-pc-windows-msvc'; executable = $fileIdentity }
            msvc = [ordered]@{ root = 'C:\msvc'; version = '1.0.0'; cl = $fileIdentity; link = $fileIdentity; sdk_root = 'C:\sdk'; sdk_version = '1.0.0'; include_root = 'C:\msvc\include'; lib_root = 'C:\msvc\lib'; sdk_um_lib = 'C:\sdk\um'; sdk_ucrt_lib = 'C:\sdk\ucrt'; rc = $fileIdentity }
            gstreamer_compile = [ordered]@{ root = 'C:\gstreamer'; version = '1.0.0'; loader = $fileIdentity; import_library = $fileIdentity; header = $unsignedFileIdentity }
        }
    }
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))

    $valid = $false
    if (Get-Command Get-TownLightCandidateIdentity -ErrorAction SilentlyContinue) {
        try {
            $identity = Get-TownLightCandidateIdentity -ManifestPath $manifestPath -ExpectedSourceCommit $manifest.source_commit
            $valid = $identity.installer_sha256 -ceq $installerHash
        } catch { $valid = $false }
    }
    if (-not $valid) {
        throw 'ASSERTION FAILED: a canonical manifest and exact sibling installer must validate as one candidate pair.'
    }

    $manifest.tsduck_source_url = 'https://example.invalid/TSDuck.zip'
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    $rejectedTSDuckSource = $false
    try { Get-TownLightCandidateIdentity -ManifestPath $manifestPath | Out-Null } catch { $rejectedTSDuckSource = $true }
    if (-not $rejectedTSDuckSource) {
        throw 'ASSERTION FAILED: a TSDuck source URL outside the pinned official asset must fail.'
    }
    $manifest.tsduck_source_url = 'https://github.com/tsduck/tsduck/releases/download/v3.44-4676/TSDuck-Win64-3.44-4676-Portable.zip'
    $manifest.tsduck_runtime_sha256 = ('3' * 64)
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    $rejectedTSDuckHash = $false
    try { Get-TownLightCandidateIdentity -ManifestPath $manifestPath | Out-Null } catch { $rejectedTSDuckHash = $true }
    if (-not $rejectedTSDuckHash) {
        throw 'ASSERTION FAILED: a TSDuck archive hash outside the pinned official asset must fail.'
    }
    $manifest.tsduck_runtime_sha256 = 'b0ca0f963fcd77488b8c32d6f9d85030daa891ed753c36e57bbb458807a44eb1'

    $sizeCases = @(
        [pscustomobject]@{ label = 'Int32'; value = [int32]4; accepted = $true }
        [pscustomobject]@{ label = 'Int64'; value = [int64]4; accepted = $true }
        [pscustomobject]@{ label = 'byte'; value = [byte]4; accepted = $false }
        [pscustomobject]@{ label = 'Int16'; value = [int16]4; accepted = $false }
        [pscustomobject]@{ label = 'UInt16'; value = [uint16]4; accepted = $false }
        [pscustomobject]@{ label = 'UInt32'; value = [uint32]4; accepted = $false }
        [pscustomobject]@{ label = 'UInt64'; value = [uint64]4; accepted = $false }
        [pscustomobject]@{ label = 'Decimal'; value = [decimal]4; accepted = $false }
        [pscustomobject]@{ label = 'Double'; value = [double]4; accepted = $false }
        [pscustomobject]@{ label = 'Boolean'; value = $true; accepted = $false }
        [pscustomobject]@{ label = 'String'; value = '4'; accepted = $false }
        [pscustomobject]@{ label = 'Null'; value = $null; accepted = $false }
        [pscustomobject]@{ label = 'NegativeInt32'; value = [int32]-1; accepted = $false }
        [pscustomobject]@{ label = 'NegativeInt64'; value = [int64]-1; accepted = $false }
        [pscustomobject]@{ label = 'Int64Max'; value = [int64]::MaxValue; accepted = $true }
    )
    foreach ($sizeCase in $sizeCases) {
        $accepted = $true
        try { Assert-TownLightCandidateSizeProperty -Object ([pscustomobject]@{ size_bytes = $sizeCase.value }) -Name 'size_bytes' -Code 'fixture_size_type' }
        catch { $accepted = $false }
        if ($accepted -ne $sizeCase.accepted) {
            throw "ASSERTION FAILED: size type $($sizeCase.label) acceptance was unexpected."
        }
    }

    $manifest.installer = 'wrong-name.exe'
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    $rejectedName = $false
    try { Get-TownLightCandidateIdentity -ManifestPath $manifestPath | Out-Null } catch { $rejectedName = $true }
    if (-not $rejectedName) {
        throw 'ASSERTION FAILED: a manifest naming any installer other than the exact setup filename must fail.'
    }

    $manifest.installer = 'TownLight-Station-0.1.0-x64-setup.exe'
    $manifest.installer_sha256 = ('2' * 64)
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    $rejectedHash = $false
    try { Get-TownLightCandidateIdentity -ManifestPath $manifestPath | Out-Null } catch { $rejectedHash = $true }
    if (-not $rejectedHash) {
        throw 'ASSERTION FAILED: an installer hash mismatch must fail closed.'
    }

    $manifest.installer_sha256 = $installerHash
    $manifest.source_commit = ('A' * 40)
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    $rejectedCase = $false
    try { Get-TownLightCandidateIdentity -ManifestPath $manifestPath | Out-Null } catch { $rejectedCase = $true }
    if (-not $rejectedCase) {
        throw 'ASSERTION FAILED: uppercase source identity must fail canonical lowercase validation.'
    }

    $manifest.source_commit = ('a' * 40)
    $manifest.installer_sha256 = $installerHash.ToUpperInvariant()
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    $rejectedHashCase = $false
    try { Get-TownLightCandidateIdentity -ManifestPath $manifestPath | Out-Null } catch { $rejectedHashCase = $true }
    if (-not $rejectedHashCase) {
        throw 'ASSERTION FAILED: uppercase installer hash must fail canonical lowercase validation.'
    }

    $manifest.installer_sha256 = $installerHash
    $manifest.installer_sha256 = $installerHash
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $manifestPath -Destination (Join-Path $fixtureRoot 'wrong-name.json')
    $rejectedCase = $false
    try { Get-TownLightCandidateIdentity -ManifestPath (Join-Path $fixtureRoot 'wrong-name.json') | Out-Null } catch { $rejectedCase = $true }
    if (-not $rejectedCase) {
        throw 'ASSERTION FAILED: a non-candidate manifest filename must fail closed.'
    }

    $manifestPath = Join-Path $fixtureRoot 'candidate-manifest.json'
    $validJson = $manifest | ConvertTo-Json -Depth 8 -Compress
    $duplicateJson = $validJson -replace '"product":"TownLight Station"', '"product":"TownLight Station","Product":"TownLight Station"'
    [IO.File]::WriteAllText($manifestPath, $duplicateJson, [Text.UTF8Encoding]::new($false))
    $rejectedCase = $false
    try { Get-TownLightCandidateIdentity -ManifestPath $manifestPath | Out-Null } catch { $rejectedCase = $true }
    if (-not $rejectedCase) {
        throw 'ASSERTION FAILED: case-variant duplicate top-level keys must fail before conversion.'
    }

    $duplicateExactJson = $validJson -replace '"product":"TownLight Station"', '"product":"TownLight Station","product":"TownLight Station"'
    [IO.File]::WriteAllText($manifestPath, $duplicateExactJson, [Text.UTF8Encoding]::new($false))
    $rejectedCase = $false
    try { Get-TownLightCandidateIdentity -ManifestPath $manifestPath | Out-Null } catch { $rejectedCase = $true }
    if (-not $rejectedCase) {
        throw 'ASSERTION FAILED: duplicate top-level keys must fail before conversion.'
    }

    $unknownJson = $validJson -replace '\}\s*$', ',"unexpected":1}'
    [IO.File]::WriteAllText($manifestPath, $unknownJson, [Text.UTF8Encoding]::new($false))
    $rejectedCase = $false
    try { Get-TownLightCandidateIdentity -ManifestPath $manifestPath | Out-Null } catch { $rejectedCase = $true }
    if (-not $rejectedCase) {
        throw 'ASSERTION FAILED: unknown top-level keys must fail the exact manifest contract.'
    }

    $nestedDuplicateJson = $validJson.Replace('"path":"C:\\iscc.exe"', '"path":"C:\\iscc.exe","Path":"C:\\iscc.exe"')
    [IO.File]::WriteAllText($manifestPath, $nestedDuplicateJson, [Text.UTF8Encoding]::new($false))
    $rejectedNestedDuplicate = $false
    try { Get-TownLightCandidateIdentity -ManifestPath $manifestPath | Out-Null } catch { $rejectedNestedDuplicate = $true }
    if (-not $rejectedNestedDuplicate) {
        throw 'ASSERTION FAILED: duplicate or case-variant nested keys must fail before conversion.'
    }

    $manifest = ($validJson | ConvertFrom-Json)
    $manifest.build_toolchain.inno | Add-Member -NotePropertyName unexpected -NotePropertyValue 1
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    $rejectedToolchain = $false
    try { Get-TownLightCandidateIdentity -ManifestPath $manifestPath | Out-Null } catch { $rejectedToolchain = $true }
    if (-not $rejectedToolchain) {
        throw 'ASSERTION FAILED: build_toolchain must reject producer-incompatible nested keys.'
    }

    $incoherentJson = $validJson -replace '"signer_status":"NotSigned"', '"signer_status":"NotSigned"'
    $incoherentJson = $incoherentJson -replace '"signer_subject":""', '"signer_subject":"CN=Unexpected"'
    [IO.File]::WriteAllText($manifestPath, $incoherentJson, [Text.UTF8Encoding]::new($false))
    $rejectedIncoherentSignature = $false
    try { Get-TownLightCandidateIdentity -ManifestPath $manifestPath | Out-Null } catch { $rejectedIncoherentSignature = $true }
    if (-not $rejectedIncoherentSignature) {
        throw 'ASSERTION FAILED: an unsigned identity with signer metadata must fail signature coherence validation.'
    }

    Write-Output 'Candidate-pair verifier fixture harness passed (size-type matrix + 14 hostile assertions).'
}
finally {
    if (Test-Path -LiteralPath $fixtureRoot -PathType Container) {
        Remove-Item -LiteralPath $fixtureRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
