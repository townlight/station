[CmdletBinding()]
param(
    [ValidateSet('Caption','BetaSoak')][string]$Mode='Caption',
    [ValidateRange(4,8)][double]$DurationHours=4
)

$ErrorActionPreference = 'Stop'
. 'C:\TownLightShared\candidate-pair-verifier.ps1'

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
    service_failure_diagnostics = $null
    caption_job = $null
    caption_proof = $null
    error = $null
}

function Write-NewAtomicEvidence([string]$Path,$Value,[int]$Depth=24) {
    $temporary="$Path.part-$([guid]::NewGuid().ToString('N'))"
    $bytes=[Text.UTF8Encoding]::new($false).GetBytes((($Value|ConvertTo-Json -Depth $Depth)+[Environment]::NewLine))
    $stream=[IO.File]::Open($temporary,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
    Move-Item -LiteralPath $temporary -Destination $Path -ErrorAction Stop
}

function Copy-NewBoundedEvidence([string]$Source,[string]$Destination,[int64]$MaximumBytes=16777216) {
    $item=Get-Item -LiteralPath $Source -Force -ErrorAction Stop
    if($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $item.Length -lt 0 -or $item.Length -gt $MaximumBytes){throw 'Beta-soak artifact is missing, unsafe, or unbounded.'}
    $input=[IO.File]::Open($item.FullName,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $output=[IO.File]::Open($Destination,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try{$input.CopyTo($output);$output.Flush($true)}finally{$output.Dispose();$input.Dispose()}
    [ordered]@{name=[IO.Path]::GetFileName($Destination);size_bytes=[int64](Get-Item $Destination).Length;sha256=(Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()}
}

function Get-RedactedError([string]$Message){
    $value=($Message -replace '[\u0000-\u001f\u007f-\u009f]',' ') -replace '(?i)\b(?:https?://)\S+','[uri-redacted]' -replace '(?i)\b[A-Z]:\\[^\r\n]+','[path-redacted]' -replace '(?i)\b(password|capability|cookie|csrf|session)\s*[:=]\s*\S+','$1=[redacted]'
    $value=$value.Trim();if($value.Length -gt 1024){$value=$value.Substring(0,1024)};$value
}

function Open-FrozenJson([string]$Path,[int64]$MaximumBytes=1048576){
    $stream=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    if($stream.Length -le 0 -or $stream.Length -gt $MaximumBytes){$stream.Dispose();throw 'Frozen JSON input is empty or unbounded.'}
    $bytes=[byte[]]::new([int]$stream.Length);if($stream.Read($bytes,0,$bytes.Length)-ne$bytes.Length){$stream.Dispose();throw 'Frozen JSON input could not be read exactly.'};$stream.Position=0
    $sha=[Security.Cryptography.SHA256]::Create();try{$hash=([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
    [pscustomobject]@{stream=$stream;sha256=$hash;document=([Text.UTF8Encoding]::new($false,$true).GetString($bytes)|ConvertFrom-Json)}
}

function Get-MappedPayloadManifestSha256 {
    $entries=[Collections.Generic.List[object]]::new()
    foreach($mapping in @([ordered]@{root='C:\TownLightCandidate';name='C:\TownLightCandidate'},[ordered]@{root='C:\TownLightVerifier';name='C:\TownLightVerifier'},[ordered]@{root='C:\TownLightInputs';name='C:\TownLightInputs'},[ordered]@{root='C:\TownLightHarness';name='C:\TownLightHarness'},[ordered]@{root='C:\TownLightShared';name='C:\TownLightShared'})){
        $files=@(Get-ChildItem -LiteralPath $mapping.root -File -Recurse -Force -ErrorAction Stop|Sort-Object FullName)
        foreach($file in $files){
            if(($file.Attributes-band[IO.FileAttributes]::ReparsePoint)-ne0){throw 'Mapped payload contains a reparse leaf.'}
            $relative=$file.FullName.Substring($mapping.root.Length).TrimStart('\').Replace('\','/')
            if($mapping.name-ceq'C:\TownLightInputs'-and$relative-ceq'beta-soak-request.json'){continue}
            $entries.Add([ordered]@{mapping=$mapping.name;relative_path=$relative;size_bytes=[int64]$file.Length;sha256=(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()})
            if($entries.Count-gt4096){throw 'Mapped payload file count is unbounded.'}
        }
    }
    $bytes=[Text.UTF8Encoding]::new($false).GetBytes((@($entries)|ConvertTo-Json -Depth 5 -Compress))
    $sha=[Security.Cryptography.SHA256]::Create();try{([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
}

function Assert-SubstantiveCommissioningReceipt($Receipt) {
    $e=$Receipt.evidence;$schedule=$e.schedule;$restart=$e.restart_recovery;$decoder=$e.installed_identity.caption_decoder
    if([int]$Receipt.schema_version-ne1-or$Receipt.product-cne'TownLight Station'-or$Receipt.result_kind-cne'installed_commissioning'-or[string]$Receipt.run_id-cnotmatch'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'-or
       [int64]$Receipt.finished_at_unix_ms-le[int64]$Receipt.started_at_unix_ms-or$e.services.station.state-ne'Running'-or$e.services.station.start_name-ne'LocalSystem'-or[int]$e.services.station.process_id-le0-or$e.services.updater.state-ne'Running'-or$e.services.updater.start_name-ne'LocalSystem'-or[int]$e.services.updater.process_id-le0-or
       $e.health_before_restart.status-ne'ready'-or$e.health_before_restart.database-ne'ready'-or[string]::IsNullOrWhiteSpace([string]$e.station_profile_before_restart.station_id)-or$decoder.path-cne(Join-Path $env:ProgramFiles 'TownLight Station\runtime\caption-decoder\ffmpeg.exe')-or$decoder.sha256-cne[string]$script:manifest.caption_decoder_sha256-or[int64]$decoder.size_bytes-ne[int64]$script:manifest.caption_decoder_size_bytes-or$e.cg.decoder_path-cne$decoder.path-or$e.cg.decoder_sha256-cne$decoder.sha256-or[int64]$e.cg.decoder_size_bytes-ne[int64]$decoder.size_bytes-or[int]$e.cg.decoder_directory_handle_count-ne4-or$e.cg.clear_validated-ne$true-or[int64]$e.cg.red_capture_bytes-le0-or[int64]$e.cg.green_capture_bytes-le0-or
       $e.safe_to_air_before.color-ne'green'-or@($e.safe_to_air_before.channels|Where-Object{$_.channel_id-eq$schedule.draft.channel_id-and$_.state-in@('healthy_fallback','on_air')}).Count-ne1-or$e.media.ingest_job.status-ne'completed'-or[int]$e.media.ingest_job.progress_percent-ne100-or$e.media.readiness.readiness_state-ne'ready'-or$schedule.prepared.dry_run_passed-ne$true-or@($schedule.prepared.conflicts).Count-ne0-or$schedule.prepared.missing_media_detail-or
       $schedule.committed.report_id-cne$schedule.completed.report_id-or$schedule.committed.schedule_item_id-cne$schedule.draft.item_id-or$schedule.completed.schedule_item_id-cne$schedule.draft.item_id-or$schedule.completed.dispatch_status-cne'completed'-or((@($schedule.observed_statuses|Sort-Object -Unique)-join'|')-cne'acknowledged|completed|pending|queued')-or
       $restart.health.status-ne'ready'-or$restart.health.database-ne'ready'-or$restart.station_profile.station_id-cne$e.station_profile_before_restart.station_id-or$restart.safe_to_air.color-ne'green'-or@($restart.safe_to_air.channels|Where-Object{$_.channel_id-eq$schedule.draft.channel_id-and$_.state-in@('healthy_fallback','on_air')}).Count-ne1-or$restart.commit_report.report_id-cne$schedule.completed.report_id-or$restart.commit_report.dispatch_status-cne'completed') { throw 'Commissioning receipt lacks required substantive passed evidence.' }
}

function Get-StationServiceIdentity {
    $service = Get-CimInstance -ClassName Win32_Service -Filter "Name='TownLightStation'"
    if ($null -eq $service -or $service.State -ne 'Running' -or [uint32]$service.ProcessId -eq 0) {
        throw 'Townlight station service identity is unavailable during authentication proof.'
    }
    $process = Get-Process -Id ([uint32]$service.ProcessId) -ErrorAction Stop
    [ordered]@{
        process_id = [uint32]$service.ProcessId
        process_start_utc = $process.StartTime.ToUniversalTime().ToString('o')
    }
}

function Read-BoundedFailureReceipt {
    param([string]$Name)
    if ([string]::IsNullOrWhiteSpace($Name) -or
        -not [string]::Equals([IO.Path]::GetFileName($Name), $Name, [StringComparison]::Ordinal)) {
        throw 'Diagnostic receipt name is invalid.'
    }
    $path = Join-Path 'C:\ProgramData\TownLight Station' $Name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    $item = Get-Item -LiteralPath $path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $item.Length -gt 65536) {
        return [ordered]@{ state = 'unsafe_or_oversized'; bytes = [int64]$item.Length }
    }
    $text = [IO.File]::ReadAllText($path, [Text.UTF8Encoding]::new($false, $true))
    [ordered]@{ state = 'present'; bytes = [int64]$item.Length; text = $text }
}

function Get-ServiceFailureDiagnostics {
    $service = Get-CimInstance -ClassName Win32_Service -Filter "Name='TownLightStation'" -ErrorAction SilentlyContinue
    $updater = Get-CimInstance -ClassName Win32_Service -Filter "Name='TownLightStationUpdater'" -ErrorAction SilentlyContinue
    $receipts = [ordered]@{}
    foreach ($name in @(
        'stationd-startup-error.txt',
        'orchestration-error.txt',
        'feed-fetch-error.txt',
        'automatic-materialization-error.txt',
        'ingest-error.txt',
        'alerting-error.txt',
        'update-monitor-error.txt'
    )) {
        $value = Read-BoundedFailureReceipt -Name $name
        if ($null -ne $value) { $receipts[$name] = $value }
    }
    $events = @(Get-WinEvent -FilterHashtable @{
        LogName = 'System'
        ProviderName = 'Service Control Manager'
        StartTime = $script:startedAt.AddMinutes(-1)
    } -ErrorAction SilentlyContinue | Where-Object {
        $_.Message -match '(?i)TownLightStation'
    } | Select-Object -First 64 | ForEach-Object {
        [ordered]@{
            time_utc = $_.TimeCreated.ToUniversalTime().ToString('o')
            event_id = [int]$_.Id
            level = [string]$_.LevelDisplayName
            message = if ($_.Message.Length -le 4096) { [string]$_.Message } else { [string]$_.Message.Substring(0, 4096) }
        }
    })
    [ordered]@{
        station_service = if ($null -eq $service) { $null } else { [ordered]@{
            state = [string]$service.State
            process_id = [uint32]$service.ProcessId
            exit_code = [uint32]$service.ExitCode
            service_specific_exit_code = [uint32]$service.ServiceSpecificExitCode
            started = [bool]$service.Started
        } }
        updater_service = if ($null -eq $updater) { $null } else { [ordered]@{
            state = [string]$updater.State
            process_id = [uint32]$updater.ProcessId
            exit_code = [uint32]$updater.ExitCode
            service_specific_exit_code = [uint32]$updater.ServiceSpecificExitCode
            started = [bool]$updater.Started
        } }
        bounded_receipts = $receipts
        service_control_events = $events
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

function Invoke-CombinedBetaSoak {
    Wait-For -Action { Invoke-Api 'Get' '/runtime-safe-to-air' } -Predicate {
        param($value)
        $selected=@($value.channels|Where-Object{$_.channel_id -eq $script:channelId})
        $value.color -eq 'green' -and $selected.Count -eq 1 -and $selected[0].state -eq 'healthy_fallback'
    } -Description 'configured channel stable healthy fallback before beta soak' -Seconds 180 | Out-Null

    $manifestSha=(Get-FileHash -LiteralPath $script:manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $receiptRoot='C:\ProgramData\TownLight Station\proof-inputs'
    [void][IO.Directory]::CreateDirectory($receiptRoot)
    $commissioningPath=Join-Path $receiptRoot "beta-soak-commissioning-$script:runId.json"
    $configurationPath=Join-Path $receiptRoot "beta-soak-configuration-$script:runId.json"
    $localFixture=Get-Item -LiteralPath $script:fixturePath -Force -ErrorAction Stop
    if($localFixture.PSIsContainer -or ($localFixture.Attributes-band[IO.FileAttributes]::ReparsePoint)-ne 0 -or (Get-FileHash $localFixture.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -cne $script:fixtureSha256){throw 'Guest-local commissioning fixture is not the verified frozen caption input.'}
    $commissioningScript='C:\TownLightShared\prove-installed-commissioning.ps1'
    if(-not(Test-Path -LiteralPath $commissioningScript -PathType Leaf)){throw 'Installed commissioning proof script is missing.'}
    $localFixturePin=[IO.File]::Open($localFixture.FullName,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    try{& $commissioningScript -Credential $script:proofCredential.Credential -FixturePath $localFixture.FullName -ExpectedSourceCommit ([string]$script:manifest.source_commit) -CandidateManifestPath $script:manifestPath -BaseUri ([string]$script:baseUri) -ResultPath $commissioningPath}
    finally{$localFixturePin.Dispose()}
    if($LASTEXITCODE -ne 0){throw 'Installed commissioning proof returned nonzero.'}
    $commissioningPin=Open-FrozenJson $commissioningPath
    $commissioning=$commissioningPin.document
    $installed=$commissioning.evidence.installed_identity
    if($commissioning.gate_status -ne 'passed' -or$commissioning.source_commit-cne[string]$script:manifest.source_commit-or$commissioning.candidate_manifest_sha256-cne$manifestSha-or$commissioning.installer_sha256-cne[string]$script:installerHash-or $installed.source_commit -cne [string]$script:manifest.source_commit -or $installed.installer_sha256 -cne [string]$script:installerHash -or
        $installed.stationd_sha256 -cne [string]$script:manifest.stationd_sha256 -or $installed.channel_worker_sha256 -cne [string]$script:manifest.channel_worker_sha256 -or
        $installed.station_updater_sha256 -cne [string]$script:manifest.station_updater_sha256 -or $installed.media_runtime_sha256 -cne [string]$script:manifest.media_runtime_sha256 -or $installed.caption_decoder.path -cne (Join-Path $env:ProgramFiles 'TownLight Station\runtime\caption-decoder\ffmpeg.exe') -or $installed.caption_decoder.sha256 -cne [string]$script:manifest.caption_decoder_sha256 -or [int64]$installed.caption_decoder.size_bytes -ne [int64]$script:manifest.caption_decoder_size_bytes -or
        $null -eq $commissioning.evidence.restart_recovery -or $commissioning.evidence.restart_recovery.status -eq 'skipped'){throw 'Real installed commissioning receipt failed or does not bind the exact candidate/restart evidence.'}
    Assert-SubstantiveCommissioningReceipt $commissioning
    $commissioningSha=$commissioningPin.sha256
    Wait-For -Action { Invoke-Api 'Get' '/runtime-safe-to-air' } -Predicate {param($value)$selected=@($value.channels|Where-Object{$_.channel_id -eq $script:channelId});$value.color -eq 'green' -and $selected.Count -eq 1 -and $selected[0].state -eq 'healthy_fallback'} -Description 'original caption channel recovery after real commissioning' -Seconds 180|Out-Null
    $station=Invoke-Api 'Get' '/station';$channels=@(Invoke-Api 'Get' '/channels');$selected=@($channels|Where-Object{$_.channel_id -eq $script:channelId})
    if($selected.Count -ne 1 -or [uint64]$station.revision -lt 1){throw 'Beta-soak configuration authority is unavailable.'}
    $configuration=[ordered]@{
        schema_version=1;product='TownLight Station';result_kind='fresh_configuration_run';gate_status='passed'
        run_id=$script:runId;finished_at_unix_ms=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        station_id=[string]$station.station_id;station_revision=[uint64]$station.revision
        channel_id=[string]$script:channelId;expected_state='healthy_fallback'
        source_commit=[string]$script:manifest.source_commit;candidate_manifest_sha256=$manifestSha
        installer_sha256=[string]$script:installerHash;payload_manifest_sha256=$script:payloadManifestSha256
        commissioning_run_id=[string]$commissioning.run_id;commissioning_result_sha256=$commissioningSha
    }
    Write-NewAtomicEvidence $configurationPath $configuration

    $proofRoot='C:\ProgramData\TownLight Station\proofs\beta-soak'
    $before=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    if(Test-Path -LiteralPath $proofRoot -PathType Container){Get-ChildItem -LiteralPath $proofRoot -Directory|ForEach-Object{[void]$before.Add($_.FullName)}}
    $proofScript='C:\TownLightShared\prove-installed-beta-soak.ps1'
    if(-not(Test-Path -LiteralPath $proofScript -PathType Leaf)){throw 'Installed beta-soak proof script is missing.'}
    $proofOutput=& $proofScript -Credential $script:proofCredential.Credential -CommissioningResultPath $commissioningPath `
        -ConfigurationResultPath $configurationPath -ExpectedStationId ([string]$station.station_id) `
        -ExpectedStationRevision ([uint64]$station.revision) -ExpectedChannelId ([string]$script:channelId) `
        -ExpectedChannelState healthy_fallback -DurationHours $DurationHours -ExpectedSourceCommit ([string]$script:manifest.source_commit) `
        -CandidateManifestPath $script:manifestPath -BaseUri $script:baseUri
    if($LASTEXITCODE -ne 0){throw 'Installed beta-soak proof returned nonzero.'}
    $proofDocument=($proofOutput|Out-String)|ConvertFrom-Json
    $runDirectories=@(Get-ChildItem -LiteralPath $proofRoot -Directory|Where-Object{-not $before.Contains($_.FullName)})
    if($runDirectories.Count -ne 1){throw 'Installed beta-soak proof did not create exactly one new run directory.'}
    $runDirectory=$runDirectories[0].FullName
    $summaryPath=Join-Path $runDirectory 'final-summary.json'
    $summary=Get-Content -LiteralPath $summaryPath -Raw -ErrorAction Stop|ConvertFrom-Json
    if($proofDocument.status -ne 'passed' -or $summary.status -ne 'passed' -or [double]$summary.duration_hours -lt $DurationHours -or
        [int]$summary.restart_count -ne 1 -or [double]$summary.coverage_ratio -lt 0.98 -or [int]$summary.actual_observations -lt [int]$summary.minimum_required_observations){throw 'Installed beta-soak final summary did not pass the selected duration and coverage contract.'}

    $observationsPath=Join-Path $runDirectory 'observations.ndjson'
    $restartRecords=@(Get-Content -LiteralPath $observationsPath|ForEach-Object{$_|ConvertFrom-Json}|Where-Object{$_.kind -eq 'planned_restart'})
    if($restartRecords.Count -ne 1){throw 'Installed beta-soak observations did not contain exactly one restart record.'}
    $restartPath=Join-Path $receiptRoot "beta-soak-restart-$script:runId.json"
    Write-NewAtomicEvidence $restartPath $restartRecords[0]

    $prefix="beta-soak-$script:runId"
    $artifacts=[Collections.Generic.List[object]]::new()
    foreach($binding in @(
        @($commissioningPath,"$prefix-commissioning.json",1048576),@($configurationPath,"$prefix-configuration.json",1048576),
        @((Join-Path $runDirectory 'run-state.json'),"$prefix-run-state.json",1048576),@($observationsPath,"$prefix-observations.ndjson",16777216),
        @($restartPath,"$prefix-restart.json",1048576),@($summaryPath,"$prefix-final-summary.json",1048576)
    )){$artifacts.Add((Copy-NewBoundedEvidence $binding[0] (Join-Path $script:evidenceRoot $binding[1]) ([int64]$binding[2])))}
    $failurePath=Join-Path $runDirectory 'failure.json'
    if(Test-Path -LiteralPath $failurePath -PathType Leaf){throw 'A passed beta-soak run must not contain failure evidence.'}
    $indexPath=Join-Path $script:evidenceRoot "$prefix-index.json"
    $index=[ordered]@{schema_version=1;result_kind='combined_fresh_beta_soak';run_id=$script:runId;status='passed';source_commit=[string]$script:manifest.source_commit;candidate_manifest_sha256=$manifestSha;installer_sha256=[string]$script:installerHash;payload_manifest_sha256=$script:payloadManifestSha256;commissioning_run_id=[string]$commissioning.run_id;commissioning_result_sha256=$commissioningSha;duration_hours=$DurationHours;artifacts=@($artifacts)}
    Write-NewAtomicEvidence $indexPath $index
    $script:result['beta_soak']=[ordered]@{index_name=[IO.Path]::GetFileName($indexPath);index_sha256=(Get-FileHash $indexPath -Algorithm SHA256).Hash.ToLowerInvariant();payload_manifest_sha256=$script:payloadManifestSha256;commissioning_run_id=[string]$commissioning.run_id;commissioning_result_sha256=$commissioningSha;summary=$summary}
    $commissioningPin.stream.Dispose()
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
    $candidate = Get-TownLightCandidateIdentity -ManifestPath $manifestPath
    $manifest = $candidate.manifest
    $installerHash = $candidate.installer_sha256
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
    if($Mode -eq 'BetaSoak'){
        $request=Get-Content -LiteralPath 'C:\TownLightInputs\beta-soak-request.json' -Raw -ErrorAction Stop|ConvertFrom-Json
        $properties=@($request.PSObject.Properties.Name|Sort-Object)
        if(($properties -join '|') -cne 'candidate_manifest_sha256|duration_hours|installer_sha256|payload_manifest_sha256|schema_version|source_commit' -or [int]$request.schema_version -ne 1 -or
            [double]$request.duration_hours -lt 4 -or [double]$request.duration_hours -gt 8 -or [string]$request.source_commit -cne [string]$manifest.source_commit -or
            [string]$request.candidate_manifest_sha256 -cne (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant() -or [string]$request.installer_sha256 -cne $installerHash -or
            [string]$request.payload_manifest_sha256 -cne (Get-MappedPayloadManifestSha256)){throw 'Beta-soak host request is invalid or does not bind the exact mapped payload.'}
        $script:payloadManifestSha256=[string]$request.payload_manifest_sha256
        $DurationHours=[double]$request.duration_hours
    }

    $installer = Start-Process -FilePath $installerPath -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=$installerLog"
    ) -Wait -PassThru
    if ($installer.ExitCode -ne 0) {
        throw "Caption-proof candidate returned installer exit code $($installer.ExitCode)."
    }
    $verification = (& $verifierPath -Manifest $manifestPath -SharedScriptsRoot 'C:\TownLightShared' | Out-String) | ConvertFrom-Json
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
            throw 'Townlight station service restarted while issuing bootstrap authority.'
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
            start_ms = 20000
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

    $startsAt = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + 30000
    Invoke-Api 'Put' '/schedule/items' ([ordered]@{
        item_id = $scheduleItemId
        revision = 1
        channel_id = $channelId
        asset_id = $assetId
        title = 'Installed caption proof program'
        starts_at_unix_ms = $startsAt
        duration_ms = 60000
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
    if($Mode -eq 'BetaSoak'){Invoke-CombinedBetaSoak}
    $result.status = 'passed'
}
catch {
    $result.error = if($Mode -eq 'BetaSoak'){Get-RedactedError $_.Exception.Message}else{$_.Exception.Message}
    try { $result.auth_diagnostics.after_failure = Get-StationServiceIdentity }
    catch { $result.auth_diagnostics.after_failure = [ordered]@{ unavailable = $true } }
    if($Mode -eq 'BetaSoak'){
        try{Write-NewAtomicEvidence (Join-Path $evidenceRoot "beta-soak-$runId-combined-failure.json") ([ordered]@{schema_version=1;result_kind='combined_fresh_beta_soak';run_id=$runId;status='failed';failed_at_utc=[datetime]::UtcNow.ToString('o');source_commit=$result.source_commit;installer_sha256=$result.installer_sha256;payload_manifest_sha256=$script:payloadManifestSha256;error=$result.error})}catch{}
    }
    try { $result.service_failure_diagnostics = Get-ServiceFailureDiagnostics }
    catch { $result.service_failure_diagnostics = [ordered]@{ diagnostic_collection_failed = $true } }
}
finally {
    $apiCookieHeader = $null
    $apiCsrfToken = $null
    $result.completed_at_utc = [datetime]::UtcNow.ToString('o')
    $json = ($result | ConvertTo-Json -Depth 16) + [Environment]::NewLine
    [IO.File]::WriteAllText($partPath, $json, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $partPath -Destination $resultPath
    & (Join-Path $env:SystemRoot 'System32\shutdown.exe') /s /t 5 /d p:0:0 /c "TownLight installed caption proof completed"
}

if ($result.status -ne 'passed') {
    exit 1
}
