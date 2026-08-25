param()

$ErrorActionPreference = 'Stop'

$candidateRoot = 'C:\TownLightCandidate'
$evidenceRoot = 'C:\TownLightEvidence'
$inputPath = 'C:\TownLightInputs\caption-source.ts'
$manifestPath = Join-Path $candidateRoot 'candidate-manifest.json'
$installerPath = Join-Path $candidateRoot 'TownLight-Station-0.1.0-x64-setup.exe'
$verifierPath = 'C:\TownLightVerifier\verify-installed.ps1'
$fixtureSha256 = '4fb0c4cdc9a6b0ab7709b4ceb3b1ff4ce02c21e43d12cb84bc23bc1922de7c93'
$runId = [guid]::NewGuid().ToString('D')
$startedAt = [datetime]::UtcNow
$resultPath = Join-Path $evidenceRoot ("caption-proof-$($startedAt.ToString('yyyyMMddTHHmmssZ'))-$runId.json")
$partPath = "$resultPath.part"
$installerLog = Join-Path $evidenceRoot ("caption-proof-$runId-installer.log")
$baseUri = [uri]'http://127.0.0.1:4070'
$origin = $baseUri.GetLeftPart([UriPartial]::Authority)
$apiCookieHeader = $null
$apiCsrfToken = $null
$proofChannel = $null
$result = [ordered]@{
    schema_version = 1
    run_id = $runId
    status = 'failed'
    started_at_utc = $startedAt.ToString('o')
    completed_at_utc = $null
    source_commit = $null
    installer_sha256 = $null
    fixture_sha256 = $null
    verifier = $null
    auth_diagnostics = [ordered]@{
        before_bootstrap = $null
        after_bootstrap = $null
        after_failure = $null
    }
    caption_job = $null
    caption_proof = $null
    error = $null
}

function Get-StationServiceIdentity {
    $service = Get-CimInstance -ClassName Win32_Service -Filter "Name='TownLightStation'"
    if ($null -eq $service -or $service.State -ne 'Running' -or [uint32]$service.ProcessId -eq 0) {
        throw 'TownLight station service identity is unavailable during authentication proof.'
    }
    $process = Get-Process -Id ([uint32]$service.ProcessId) -ErrorAction Stop
    [ordered]@{
        process_id = [uint32]$service.ProcessId
        process_start_utc = $process.StartTime.ToUniversalTime().ToString('o')
    }
}

function Invoke-Api {
    param(
        [ValidateSet('Get', 'Post', 'Put')][string]$Method,
        [string]$Path,
        $Body = $null,
        [int]$TimeoutSec = 30,
        [switch]$Unauthenticated,
        [switch]$IncludeResponseHeaders
    )
    if (-not $Unauthenticated -and
        ([string]::IsNullOrWhiteSpace($script:apiCookieHeader) -or
        [string]::IsNullOrWhiteSpace($script:apiCsrfToken))) {
        throw 'Caption proof API session authority is unavailable.'
    }
    if ([string]::IsNullOrWhiteSpace($Path) -or -not $Path.StartsWith('/') -or
        $Path.StartsWith('//') -or $Path.Contains('://') -or $Path.Contains('#') -or
        $Path.Contains('..')) {
        throw 'Caption proof API path is invalid or unbounded.'
    }
    $headers = [ordered]@{}
    if (-not $Unauthenticated) { $headers.Cookie = $script:apiCookieHeader }
    if ($Method -ne 'Get') {
        $headers.Origin = $script:origin
        $headers['Sec-Fetch-Site'] = 'same-origin'
        if (-not $Unauthenticated) {
            $headers['X-TownLight-CSRF'] = $script:apiCsrfToken
        }
    }
    $requestBody = $null
    if ($null -ne $Body) {
        $requestBody = [Text.UTF8Encoding]::new($false).GetBytes(
            ($Body | ConvertTo-Json -Depth 32 -Compress)
        )
    } else {
        $requestBody = [byte[]]::new(0)
    }
    $headerLines = [Collections.Generic.List[string]]::new()
    $headerLines.Add("$($Method.ToUpperInvariant()) /api/v1$Path HTTP/1.1")
    $headerLines.Add('Host: 127.0.0.1:4070')
    $headerLines.Add('Accept: application/json')
    $headerLines.Add('Connection: close')
    foreach ($name in $headers.Keys) { $headerLines.Add("$name`: $($headers[$name])") }
    if ($requestBody.Length -gt 0) { $headerLines.Add('Content-Type: application/json') }
    $headerLines.Add("Content-Length: $($requestBody.Length)")
    $requestHead = [Text.Encoding]::ASCII.GetBytes(($headerLines -join "`r`n") + "`r`n`r`n")
    $requestBytes = [byte[]]::new($requestHead.Length + $requestBody.Length)
    [Array]::Copy($requestHead, 0, $requestBytes, 0, $requestHead.Length)
    if ($requestBody.Length -gt 0) {
        [Array]::Copy($requestBody, 0, $requestBytes, $requestHead.Length, $requestBody.Length)
    }
    $client = [Net.Sockets.TcpClient]::new()
    $stream = $null
    $responseBuffer = $null
    $responseBytes = $null
    $connect = $null
    try {
        $connect = $client.BeginConnect([Net.IPAddress]::Loopback, 4070, $null, $null)
        if (-not $connect.AsyncWaitHandle.WaitOne($TimeoutSec * 1000)) {
            throw 'Caption proof API loopback connection timed out.'
        }
        $client.EndConnect($connect)
        $client.SendTimeout = $TimeoutSec * 1000
        $client.ReceiveTimeout = $TimeoutSec * 1000
        $stream = $client.GetStream()
        $stream.Write($requestBytes, 0, $requestBytes.Length)
        $stream.Flush()
        $responseBuffer = [byte[]]::new(8192)
        $responseMemory = [IO.MemoryStream]::new()
        try {
            while (($read = $stream.Read($responseBuffer, 0, $responseBuffer.Length)) -gt 0) {
                if ($responseMemory.Length + $read -gt 16842752) {
                    throw 'Caption proof API response exceeded its bounded size.'
                }
                $responseMemory.Write($responseBuffer, 0, $read)
            }
            $responseBytes = $responseMemory.ToArray()
        }
        finally { $responseMemory.Dispose() }
        $headerEnd = -1
        for ($index = 0; $index -le $responseBytes.Length - 4; $index++) {
            if ($responseBytes[$index] -eq 13 -and $responseBytes[$index + 1] -eq 10 -and
                $responseBytes[$index + 2] -eq 13 -and $responseBytes[$index + 3] -eq 10) {
                $headerEnd = $index
                break
            }
        }
        if ($headerEnd -lt 0 -or $headerEnd -gt 65536) {
            throw 'Caption proof API returned invalid or oversized HTTP headers.'
        }
        $responseHead = [Text.Encoding]::ASCII.GetString($responseBytes, 0, $headerEnd)
        $responseLines = $responseHead -split "`r`n"
        if ($responseLines[0] -notmatch '^HTTP/1\.1 ([0-9]{3}) ') {
            throw 'Caption proof API returned an invalid HTTP status line.'
        }
        $status = [int]$Matches[1]
        $lengthHeaders = @($responseLines | Where-Object { $_ -match '(?i)^Content-Length:\s*([0-9]+)\s*$' })
        if ($lengthHeaders.Count -ne 1 -or $lengthHeaders[0] -notmatch '(?i)^Content-Length:\s*([0-9]+)\s*$') {
            throw 'Caption proof API returned invalid response framing.'
        }
        $contentLength = [int64]$Matches[1]
        $bodyOffset = $headerEnd + 4
        if ($contentLength -gt 16777216 -or $contentLength -ne ($responseBytes.Length - $bodyOffset)) {
            throw 'Caption proof API returned a mismatched or oversized response body.'
        }
        if ($status -lt 200 -or $status -ge 300) {
            throw "Caption proof API $Method $Path failed with HTTP status $status; response content was not recorded."
        }
        $document = $null
        if ($contentLength -gt 0) {
            $content = [Text.UTF8Encoding]::new($false, $true).GetString(
                $responseBytes,
                $bodyOffset,
                [int]$contentLength
            )
            $document = $content | ConvertFrom-Json -ErrorAction Stop
        }
        if ($IncludeResponseHeaders) {
            return [pscustomobject]@{
                Document = $document
                ResponseHeaders = @($responseLines | Select-Object -Skip 1)
            }
        }
        $document
    }
    catch {
        if ($_.Exception.Message -match '^Caption proof API .+ failed with HTTP status [0-9]+;') {
            throw
        }
        throw "Caption proof API $Method $Path failed; response content was not recorded."
    }
    finally {
        $requestBody = $null
        $requestHead = $null
        $requestBytes = $null
        $responseBuffer = $null
        $responseBytes = $null
        if ($connect -and $connect.AsyncWaitHandle) { $connect.AsyncWaitHandle.Dispose() }
        if ($stream) { $stream.Dispose() }
        $client.Dispose()
    }
}

function Wait-For {
    param(
        [scriptblock]$Action,
        [scriptblock]$Predicate,
        [string]$Description,
        [int]$Seconds = 180
    )
    $deadline = [datetime]::UtcNow.AddSeconds($Seconds)
    $last = $null
    while ([datetime]::UtcNow -lt $deadline) {
        $last = & $Action
        if (& $Predicate $last) { return $last }
        Start-Sleep -Milliseconds 200
    }
    throw "Timed out waiting for $Description."
}

function New-RandomProofCredential {
    $bytes = [byte[]]::new(24)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) }
    finally { $generator.Dispose() }
    $secret = ([Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')) + '!aA1'
    $secure = ConvertTo-SecureString -String $secret -AsPlainText -Force
    [pscustomobject]@{
        PlainText = $secret
        Credential = [Management.Automation.PSCredential]::new('sandbox-admin', $secure)
    }
}

function Get-ExactResponseCookie {
    param([string[]]$ResponseHeaders, [string]$Name)
    $matches = @($ResponseHeaders | ForEach-Object {
        if ($_ -match ('(?i)^Set-Cookie:\s*' + [regex]::Escape($Name) + '=([A-Za-z0-9_-]{43});')) {
            $Matches[1]
        }
    })
    if ($matches.Count -ne 1) {
        throw "Bootstrap response did not contain exactly one bounded $Name cookie."
    }
    $matches[0]
}

try {
    foreach ($required in @($manifestPath, $installerPath, $verifierPath, $inputPath)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Caption-proof guest input is missing: $required"
        }
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $installerHash = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $observedFixtureHash = (Get-FileHash -LiteralPath $inputPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($manifest.product -ne 'TownLight Station' -or
        $manifest.source_commit -notmatch '^[0-9a-f]{40}$' -or
        $installerHash -ne $manifest.installer_sha256 -or
        $observedFixtureHash -ne $fixtureSha256) {
        throw 'Caption-proof guest inputs do not match the exact candidate and fixture authority.'
    }
    $result.source_commit = $manifest.source_commit
    $result.installer_sha256 = $installerHash
    $result.fixture_sha256 = $observedFixtureHash

    $installer = Start-Process -FilePath $installerPath -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=$installerLog"
    ) -Wait -PassThru
    if ($installer.ExitCode -ne 0) {
        throw "Caption-proof candidate returned installer exit code $($installer.ExitCode)."
    }
    $verification = (& $verifierPath -Manifest $manifestPath | Out-String) | ConvertFrom-Json
    if ($verification.source_commit -ne $manifest.source_commit -or
        $verification.caption_decoder_sha256 -ne $manifest.caption_decoder_sha256 -or
        $verification.health -ne 'ready') {
        throw 'Installed verification did not preserve exact candidate and decoder identity.'
    }
    $result.verifier = $verification

    $authStatus = Invoke-RestMethod -Method Get -Uri "$origin/api/v1/auth/status" -TimeoutSec 10
    if ($authStatus.state -ne 'bootstrap_required') {
        throw 'Fresh caption-proof guest did not require first-account bootstrap.'
    }
    $handoffPath = 'C:\ProgramData\TownLight Station\operator-bootstrap.capability'
    $rawCapability = [IO.File]::ReadAllText($handoffPath, [Text.UTF8Encoding]::new($false))
    if ($rawCapability -notmatch '\A[A-Za-z0-9_-]{43}\n\z') {
        throw 'Silent installer did not preserve one exact protected bootstrap capability.'
    }
    $capability = $rawCapability.Substring(0, 43)
    $proofCredential = New-RandomProofCredential
    $bootstrapResponse = $null
    $result.auth_diagnostics.before_bootstrap = Get-StationServiceIdentity
    try {
        $bootstrapBody = [ordered]@{
            capability = $capability
            username = $proofCredential.Credential.UserName
            display_name = 'Sandbox Setup Admin'
            password = $proofCredential.PlainText
        }
        $bootstrapResponse = Invoke-Api -Method Post -Path '/auth/bootstrap' `
            -Body $bootstrapBody -TimeoutSec 20 -Unauthenticated -IncludeResponseHeaders
        $bootstrapDocument = $bootstrapResponse.Document
        $result.auth_diagnostics.after_bootstrap = Get-StationServiceIdentity
        if ($result.auth_diagnostics.after_bootstrap.process_id -ne
            $result.auth_diagnostics.before_bootstrap.process_id -or
            $result.auth_diagnostics.after_bootstrap.process_start_utc -ne
            $result.auth_diagnostics.before_bootstrap.process_start_utc) {
            throw 'TownLight station service restarted while issuing bootstrap authority.'
        }
        if ($bootstrapDocument.session.username -ne $proofCredential.Credential.UserName -or
            @($bootstrapDocument.session.roles | Where-Object { $_ -eq 'setup_admin' }).Count -ne 1) {
            throw 'Bootstrap response did not establish the exact Setup Admin identity.'
        }
        Remove-Item -LiteralPath $handoffPath -Force
    }
    finally {
        $bootstrapBody = $null
        $capability = $null
        $rawCapability = $null
    }
    # Parse both exact Set-Cookie lines from the raw HTTP bytes and retain the bounded tokens only
    # in process memory. Windows PowerShell 5 may alter repeated response headers.
    $sessionToken = Get-ExactResponseCookie -ResponseHeaders $bootstrapResponse.ResponseHeaders `
        -Name 'townlight_session_v1'
    $csrfToken = Get-ExactResponseCookie -ResponseHeaders $bootstrapResponse.ResponseHeaders `
        -Name 'townlight_csrf_v1'
    $bootstrapResponse = $null
    $apiCookieHeader = "townlight_session_v1=$sessionToken; townlight_csrf_v1=$csrfToken"
    $apiCsrfToken = $csrfToken
    $confirmed = Invoke-Api 'Get' '/auth/session'
    if ($confirmed.session.username -ne $proofCredential.Credential.UserName) {
        throw 'Bootstrap-established session could not be confirmed through the public API.'
    }
    $sessionToken = $null
    $csrfToken = $null
    $proofCredential.PlainText = $null

    Invoke-Api 'Put' '/station' ([ordered]@{
        station_id = [guid]::NewGuid().ToString('D')
        display_name = 'TownLight Caption Proof Station'
        timezone = 'America/Denver'
        expected_revision = 0
    }) | Out-Null

    $channelId = [guid]::NewGuid().ToString('D')
    $sinkId = [guid]::NewGuid().ToString('D')
    $trackId = [guid]::NewGuid().ToString('D')
    $cueId = [guid]::NewGuid().ToString('D')
    $ingestJobId = [guid]::NewGuid().ToString('D')
    $scheduleItemId = [guid]::NewGuid().ToString('D')
    $reportId = "caption-$runId"
    $planId = "caption-$runId"
    $proofChannel = [ordered]@{
        channel_id = $channelId
        display_name = "Installed caption proof $runId"
        output_profile = 'proof-360p30'
        output_sinks = @([ordered]@{
            sink_id = $sinkId
            label = 'Installed caption proof sink'
            kind = 'udp-ts'
            destination = '127.0.0.1:56080'
            loudness_target_lufs = -16.0
            required = $true
            enabled = $true
        })
        enabled = $false
    }
    Invoke-Api 'Put' '/channels' $proofChannel | Out-Null
    $policy = Invoke-Api 'Put' "/channels/$channelId/caption-policy" ([ordered]@{
        mode = 'cea708'
        service_number = 1
        language = 'en-US'
        expected_revision = 0
    })
    if ($policy.revision -ne 1 -or $policy.mode -ne 'cea708') {
        throw 'Installed caption policy did not become revision-one CEA-708 authority.'
    }

    $proofInputRoot = 'C:\ProgramData\TownLight Station\proof-inputs'
    [void][IO.Directory]::CreateDirectory($proofInputRoot)
    $fixturePath = Join-Path $proofInputRoot "caption-$runId.ts"
    Copy-Item -LiteralPath $inputPath -Destination $fixturePath
    if ((Get-FileHash -LiteralPath $fixturePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $fixtureSha256) {
        throw 'Guest copy of the caption fixture changed identity.'
    }
    Invoke-Api 'Post' '/assets/ingest' ([ordered]@{
        job_id = $ingestJobId
        source_path = $fixturePath
    }) | Out-Null
    $ingest = Wait-For -Action { Invoke-Api 'Get' "/assets/ingest/jobs/$ingestJobId" } `
        -Predicate {
            param($value)
            if ($value.status -in @('failed', 'rejected')) { throw 'Installed caption fixture ingest failed.' }
            $value.status -eq 'completed' -and $null -ne $value.asset_id
        } -Description 'caption fixture ingest completion' -Seconds 240
    $assetId = [string]$ingest.asset_id
    $readiness = Wait-For -Action { Invoke-Api 'Get' "/assets/$assetId/readiness" } `
        -Predicate {
            param($value)
            if ($value.readiness_state -in @('missing_file', 'rejected')) { throw 'Installed caption fixture was rejected.' }
            $value.readiness_state -eq 'ready'
        } -Description 'caption fixture readiness' -Seconds 240

    $track = Invoke-Api 'Put' "/assets/$assetId/caption-tracks/$trackId" ([ordered]@{
        language = 'en-US'
        service_number = 1
        source_kind = 'external'
        label = 'Installed reviewed English'
        cues = @([ordered]@{
            cue_id = $cueId
            start_ms = 8000
            duration_ms = 2500
            text = 'TownLight caption path verified'
        })
        expected_revision = 0
    })
    if ($track.revision -ne 1 -or $track.cues.Count -ne 1) {
        throw 'Installed caption track did not preserve exact revision-one cue authority.'
    }

    $proofChannel.enabled = $true
    Invoke-Api 'Put' '/channels' $proofChannel | Out-Null
    Wait-For -Action { Invoke-Api 'Get' '/runtime-safe-to-air' } -Predicate {
        param($value)
        @($value.channels | Where-Object {
            $_.channel_id -eq $channelId -and $_.state -eq 'healthy_fallback'
        }).Count -eq 1
    } -Description 'caption proof channel healthy fallback' -Seconds 90 | Out-Null

    $startsAt = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + 12000
    Invoke-Api 'Put' '/schedule/items' ([ordered]@{
        item_id = $scheduleItemId
        channel_id = $channelId
        asset_id = $assetId
        title = 'Installed caption proof program'
        starts_at_unix_ms = $startsAt
        duration_ms = 30000
        state = 'draft'
    }) | Out-Null
    $prepared = Invoke-Api 'Post' '/schedule/prepare' ([ordered]@{
        plan_id = $planId
        schedule_item_id = $scheduleItemId
    })
    if (-not $prepared.dry_run_passed) {
        throw 'Installed caption schedule did not pass commit-to-air preparation.'
    }
    Invoke-Api 'Post' '/schedule/commit' ([ordered]@{
        report_id = $reportId
        plan_id = $planId
        schedule_item_id = $scheduleItemId
        operator_notes = 'Installed exact-candidate caption proof.'
    }) | Out-Null
    Wait-For -Action { Invoke-Api 'Get' "/schedule/commits/$reportId" } -Predicate {
        param($value)
        if ($value.dispatch_status -eq 'error') { throw 'Installed caption schedule dispatch failed.' }
        $value.dispatch_status -eq 'acknowledged'
    } -Description 'captioned schedule on-air acknowledgment' -Seconds 90 | Out-Null

    $job = Invoke-Api 'Post' "/channels/$channelId/caption-proof" ([ordered]@{
        report_id = $reportId
        sink_id = $sinkId
        capture_duration_ms = 4000
        max_capture_bytes = 16777216
    })
    if ($job.state -ne 'pending' -or $job.expected_cue_count -ne 1 -or
        $job.source_commit -ne $manifest.source_commit) {
        throw 'Installed caption proof request did not bind exact pending cue/candidate authority.'
    }
    $jobId = [string]$job.job_id
    $completedJob = Wait-For -Action {
        Invoke-Api 'Get' "/channels/$channelId/caption-proof/jobs/$jobId"
    } -Predicate {
        param($value)
        if ($value.state -eq 'failed') { throw "Installed caption proof job failed: $($value.failure_detail)" }
        $value.state -eq 'completed'
    } -Description 'installed caption capture and independent decode' -Seconds 90

    $history = Invoke-Api 'Get' "/channels/$channelId/caption-proof"
    $proofs = @($history.proofs | Where-Object { $_.proof_id -eq $jobId })
    if ($proofs.Count -ne 1) {
        throw 'Installed caption proof history did not contain exactly one job-bound immutable proof.'
    }
    $proof = $proofs[0]
    if ($proof.outcome -ne 'verified' -or
        $proof.expected_cue_count -ne 1 -or
        $proof.decoded_cue_count -ne 1 -or
        $proof.expected_text_sha256 -ne $proof.decoded_text_sha256 -or
        $proof.source_commit -ne $manifest.source_commit -or
        $proof.decoder_engine -ne "ffmpeg-sha256=$($manifest.caption_decoder_sha256)" -or
        $proof.decoder_method -ne 'lavfi movie subcc to bounded SRT canonical text v1' -or
        $proof.capture_sha256 -ne $completedJob.capture.capture_sha256) {
        throw 'Installed immutable caption proof did not verify exact decode-back equality.'
    }
    if ($proof.artifact_relative_path -notmatch '^caption-proofs/[0-9a-f-]{36}\.ts$') {
        throw 'Installed caption proof returned an unsafe artifact identity.'
    }
    $artifactPath = Join-Path 'C:\ProgramData\TownLight Station' $proof.artifact_relative_path
    if (-not (Test-Path -LiteralPath $artifactPath -PathType Leaf) -or
        (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $proof.capture_sha256) {
        throw 'Installed caption proof artifact bytes do not match the immutable capture digest.'
    }
    $result.caption_job = $completedJob
    $result.caption_proof = $proof
    $result.status = 'passed'
}
catch {
    $result.error = $_.Exception.Message
    try { $result.auth_diagnostics.after_failure = Get-StationServiceIdentity }
    catch { $result.auth_diagnostics.after_failure = [ordered]@{ unavailable = $true } }
}
finally {
    $apiCookieHeader = $null
    $apiCsrfToken = $null
    $result.completed_at_utc = [datetime]::UtcNow.ToString('o')
    $json = ($result | ConvertTo-Json -Depth 16) + [Environment]::NewLine
    [IO.File]::WriteAllText($partPath, $json, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $partPath -Destination $resultPath
    & shutdown.exe /s /t 5 /d p:0:0 /c "TownLight installed caption proof completed"
}

if ($result.status -ne 'passed') {
    exit 1
}
