param()

$ErrorActionPreference = 'Stop'
$candidateRoot = 'C:\TownLightCandidate'
$evidenceRoot = 'C:\TownLightEvidence'
$manifestPath = Join-Path $candidateRoot 'candidate-manifest.json'
$installerPath = Join-Path $candidateRoot 'TownLight-Station-0.1.0-x64-setup.exe'
$verifierPath = 'C:\TownLightVerifier\verify-installed.ps1'
$runId = [guid]::NewGuid().ToString('D')
$startedAt = [datetime]::UtcNow
$resultPath = Join-Path $evidenceRoot ("clean-install-$($startedAt.ToString('yyyyMMddTHHmmssZ'))-$runId.json")
$partPath = "$resultPath.part"
$installerLog = Join-Path $evidenceRoot ("clean-install-$runId-installer.log")
$result = [ordered]@{
    schema_version = 1
    run_id = $runId
    status = 'failed'
    started_at_utc = $startedAt.ToString('o')
    completed_at_utc = $null
    source_commit = $null
    installer_sha256 = $null
    installer_exit_code = $null
    verifier = $null
    error = $null
}

try {
    foreach ($required in @($manifestPath, $installerPath, $verifierPath)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Clean-install guest input is missing: $required"
        }
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $installerHash = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($manifest.product -ne 'TownLight Station' -or
        $manifest.source_commit -notmatch '^[0-9a-f]{40}$' -or
        $installerHash -ne $manifest.installer_sha256) {
        throw 'Clean-install candidate manifest does not authenticate the exact installer.'
    }
    $result.source_commit = $manifest.source_commit
    $result.installer_sha256 = $installerHash

    $arguments = @(
        '/VERYSILENT',
        '/SUPPRESSMSGBOXES',
        '/NORESTART',
        "/LOG=$installerLog"
    )
    $installer = Start-Process -FilePath $installerPath -ArgumentList $arguments -Wait -PassThru
    $result.installer_exit_code = $installer.ExitCode
    if ($installer.ExitCode -ne 0) {
        throw "Clean-install candidate returned exit code $($installer.ExitCode)."
    }

    $verificationText = & $verifierPath -Manifest $manifestPath | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw 'Installed verification returned nonzero.'
    }
    $verification = $verificationText | ConvertFrom-Json
    if ($verification.source_commit -ne $manifest.source_commit -or
        $verification.caption_decoder_sha256 -ne $manifest.caption_decoder_sha256 -or
        $verification.health -ne 'ready') {
        throw 'Installed verification did not preserve exact candidate and decoder identity.'
    }
    $result.verifier = $verification
    $result.status = 'passed'
}
catch {
    $result.error = $_.Exception.Message
}
finally {
    $result.completed_at_utc = [datetime]::UtcNow.ToString('o')
    $json = ($result | ConvertTo-Json -Depth 8) + [Environment]::NewLine
    [IO.File]::WriteAllText($partPath, $json, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $partPath -Destination $resultPath
    & shutdown.exe /s /t 5 /d p:0:0 /c "TownLight clean-install harness completed"
}

if ($result.status -ne 'passed') {
    exit 1
}
