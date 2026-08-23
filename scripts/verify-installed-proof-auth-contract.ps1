param()

$ErrorActionPreference = 'Stop'
$helperPath = Join-Path $PSScriptRoot 'installed-proof-api.ps1'
$assertionCount = 0

function Assert-True {
    param([bool]$Condition, [string]$Message)
    $script:assertionCount++
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
}

function Assert-Equal {
    param($Expected, $Actual, [string]$Message)
    $script:assertionCount++
    if ($Expected -ne $Actual) {
        throw "ASSERTION FAILED: $Message (expected '$Expected', got '$Actual')"
    }
}

Assert-True (Test-Path -LiteralPath $helperPath -PathType Leaf) `
    'the reusable installed-proof API helper must exist'
. $helperPath
Assert-True ($null -ne (Get-Command New-TownLightProofSession -ErrorAction SilentlyContinue)) `
    'the helper must export New-TownLightProofSession'
Assert-True ($null -ne (Get-Command Connect-TownLightProofSession -ErrorAction SilentlyContinue)) `
    'the helper must export Connect-TownLightProofSession'
Assert-True ($null -ne (Get-Command Invoke-TownLightProofApi -ErrorAction SilentlyContinue)) `
    'the helper must export Invoke-TownLightProofApi'

$portProbe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$portProbe.Start()
$port = ([Net.IPEndPoint]$portProbe.LocalEndpoint).Port
$portProbe.Stop()

$runId = [guid]::NewGuid().ToString('N')
$passwordCanary = "Proof-Password-$runId!"
$sessionOne = "session-one-$runId"
$csrfOne = "csrf-one-$runId"
$sessionTwo = "session-two-$runId"
$csrfTwo = "csrf-two-$runId"
$username = "proof-admin-$runId"
$origin = "http://127.0.0.1:$port"
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) "townlight-proof-auth-contract-$runId"
$readyPath = Join-Path $temporaryRoot 'ready.txt'
$stopPath = Join-Path $temporaryRoot 'stop.txt'
$transcriptPath = Join-Path $temporaryRoot 'transcript.jsonl'
$downloadPath = Join-Path $temporaryRoot 'download.bin'
$server = $null

New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
$server = Start-Job -ArgumentList @(
    $port, $readyPath, $stopPath, $transcriptPath, $origin, $username, $passwordCanary,
    $sessionOne, $csrfOne, $sessionTwo, $csrfTwo
) -ScriptBlock {
    param(
        [int]$Port,
        [string]$ReadyPath,
        [string]$StopPath,
        [string]$TranscriptPath,
        [string]$ExpectedOrigin,
        [string]$ExpectedUsername,
        [string]$ExpectedPassword,
        [string]$SessionOne,
        [string]$CsrfOne,
        [string]$SessionTwo,
        [string]$CsrfTwo
    )

    $ErrorActionPreference = 'Stop'

    function Send-HttpResponse {
        param(
            [Net.Sockets.NetworkStream]$Stream,
            [int]$StatusCode,
            [string]$Reason,
            [byte[]]$Body,
            [string]$ContentType,
            [string[]]$ExtraHeaders = @()
        )
        $lines = [Collections.Generic.List[string]]::new()
        $lines.Add("HTTP/1.1 $StatusCode $Reason")
        $lines.Add("Content-Length: $($Body.Length)")
        $lines.Add("Content-Type: $ContentType")
        $lines.Add('Connection: close')
        foreach ($header in $ExtraHeaders) { $lines.Add($header) }
        $head = [Text.Encoding]::ASCII.GetBytes(($lines -join "`r`n") + "`r`n`r`n")
        $Stream.Write($head, 0, $head.Length)
        if ($Body.Length -gt 0) { $Stream.Write($Body, 0, $Body.Length) }
        $Stream.Flush()
    }

    function Send-Json {
        param(
            [Net.Sockets.NetworkStream]$Stream,
            [int]$StatusCode,
            [string]$Reason,
            [string]$Json,
            [string[]]$ExtraHeaders = @()
        )
        Send-HttpResponse -Stream $Stream -StatusCode $StatusCode -Reason $Reason `
            -Body ([Text.Encoding]::UTF8.GetBytes($Json)) -ContentType 'application/json' `
            -ExtraHeaders $ExtraHeaders
    }

    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
    $listener.Start()
    Set-Content -LiteralPath $ReadyPath -Value 'ready' -Encoding ascii
    $loginGeneration = 0
    try {
        while (-not (Test-Path -LiteralPath $StopPath)) {
            if (-not $listener.Pending()) {
                Start-Sleep -Milliseconds 20
                continue
            }

            $client = $listener.AcceptTcpClient()
            try {
                $stream = $client.GetStream()
                $stream.ReadTimeout = 5000
                $received = [Collections.Generic.List[byte]]::new()
                $headerEnd = -1
                while ($headerEnd -lt 0) {
                    $value = $stream.ReadByte()
                    if ($value -lt 0) { break }
                    $received.Add([byte]$value)
                    $count = $received.Count
                    if ($count -ge 4 -and $received[$count - 4] -eq 13 -and
                        $received[$count - 3] -eq 10 -and $received[$count - 2] -eq 13 -and
                        $received[$count - 1] -eq 10) {
                        $headerEnd = $count
                    }
                    if ($received.Count -gt 32768) { throw 'Request headers exceeded the fake-server bound.' }
                }
                if ($headerEnd -lt 0) { continue }

                $headerText = [Text.Encoding]::ASCII.GetString($received.ToArray(), 0, $headerEnd)
                $headerLines = $headerText -split "`r`n"
                $requestParts = $headerLines[0].Split(' ')
                $method = $requestParts[0]
                $path = $requestParts[1]
                $headers = @{}
                foreach ($line in $headerLines[1..($headerLines.Count - 1)]) {
                    $separator = $line.IndexOf(':')
                    if ($separator -gt 0) {
                        $headers[$line.Substring(0, $separator).Trim().ToLowerInvariant()] =
                            $line.Substring($separator + 1).Trim()
                    }
                }
                $contentLength = if ($headers.ContainsKey('content-length')) {
                    [int]$headers['content-length']
                } else { 0 }
                $body = [byte[]]::new($contentLength)
                $bodyOffset = 0
                while ($bodyOffset -lt $contentLength) {
                    $read = $stream.Read($body, $bodyOffset, $contentLength - $bodyOffset)
                    if ($read -le 0) { break }
                    $bodyOffset += $read
                }

                $expectedSession = if ($loginGeneration -ge 2) { $SessionTwo } else { $SessionOne }
                $expectedCsrf = if ($loginGeneration -ge 2) { $CsrfTwo } else { $CsrfOne }
                $cookie = if ($headers.ContainsKey('cookie')) { $headers['cookie'] } else { '' }
                $originOk = $headers.ContainsKey('origin') -and $headers['origin'] -eq $ExpectedOrigin
                $secFetchSiteOk = $headers.ContainsKey('sec-fetch-site') -and
                    $headers['sec-fetch-site'] -ceq 'same-origin'
                $csrfOk = $headers.ContainsKey('x-townlight-csrf') -and
                    $headers['x-townlight-csrf'] -eq $expectedCsrf
                $sessionOk = $cookie.Contains("townlight_session_v1=$expectedSession") -and
                    $cookie.Contains("townlight_csrf_v1=$expectedCsrf")
                $loginBodyOk = $false
                if ($path -eq '/api/v1/auth/login') {
                    try {
                        $loginDocument = [Text.Encoding]::UTF8.GetString($body) | ConvertFrom-Json
                        $loginBodyOk = $loginDocument.username -eq $ExpectedUsername -and
                            $loginDocument.password -eq $ExpectedPassword
                    } catch { $loginBodyOk = $false }
                }
                $uploadBodyOk = $body.Length -eq 5 -and $body[0] -eq 0 -and $body[1] -eq 255 -and
                    $body[2] -eq 16 -and $body[3] -eq 128 -and $body[4] -eq 65

                [ordered]@{
                    method = $method
                    path = $path
                    origin_ok = $originOk
                    sec_fetch_site_ok = $secFetchSiteOk
                    session_ok = $sessionOk
                    csrf_ok = $csrfOk
                    login_body_ok = $loginBodyOk
                    upload_body_ok = $uploadBodyOk
                    content_type = if ($headers.ContainsKey('content-type')) { $headers['content-type'] } else { '' }
                    process_command_clean = @(
                        $ExpectedPassword, $SessionOne, $CsrfOne, $SessionTwo, $CsrfTwo
                    ).Where({ [Environment]::CommandLine.Contains($_) }).Count -eq 0
                    environment_clean = @(
                        $ExpectedPassword, $SessionOne, $CsrfOne, $SessionTwo, $CsrfTwo
                    ).Where({
                        (((Get-ChildItem Env:).Value | ForEach-Object { [string]$_ }) -join "`n").Contains($_)
                    }).Count -eq 0
                } | ConvertTo-Json -Compress | Add-Content -LiteralPath $TranscriptPath -Encoding utf8

                if ($path -eq '/api/v1/auth/login' -and $method -eq 'POST' -and $originOk -and $secFetchSiteOk -and $loginBodyOk) {
                    $loginGeneration++
                    $newSession = if ($loginGeneration -eq 1) { $SessionOne } else { $SessionTwo }
                    $newCsrf = if ($loginGeneration -eq 1) { $CsrfOne } else { $CsrfTwo }
                    Send-Json $stream 200 'OK' '{"session":{"account_id":"proof-account","username":"proof","display_name":"Proof Operator","roles":["setup_admin"],"absolute_expires_at_unix_ms":4102444800000}}' @(
                        "Set-Cookie: townlight_session_v1=$newSession; HttpOnly; SameSite=Strict; Path=/api/v1/",
                        "Set-Cookie: townlight_csrf_v1=$newCsrf; SameSite=Strict; Path=/"
                    )
                } elseif ($path -eq '/api/v1/auth/session' -and $method -eq 'GET' -and $sessionOk) {
                    Send-Json $stream 200 'OK' ('{"session":{"account_id":"proof-account","username":"' + $ExpectedUsername + '","display_name":"Proof Operator","roles":["setup_admin"],"absolute_expires_at_unix_ms":4102444800000}}')
                } elseif ($path -eq '/api/v1/safe-json' -and $method -eq 'GET' -and $sessionOk) {
                    Send-Json $stream 200 'OK' '{"value":"safe"}'
                } elseif ($path -eq '/api/v1/safe-text' -and $method -eq 'GET' -and $sessionOk) {
                    Send-HttpResponse $stream 200 'OK' ([Text.Encoding]::UTF8.GetBytes('plain proof text')) 'text/plain; charset=utf-8'
                } elseif ($path -eq '/api/v1/mutate' -and $method -eq 'PUT' -and $sessionOk -and $originOk -and $secFetchSiteOk -and $csrfOk) {
                    Send-Json $stream 200 'OK' '{"changed":true}'
                } elseif ($path -eq '/api/v1/upload' -and $method -eq 'POST' -and $sessionOk -and $originOk -and $secFetchSiteOk -and $csrfOk -and $uploadBodyOk -and $headers['content-type'] -eq 'application/octet-stream') {
                    Send-HttpResponse $stream 200 'OK' ([byte[]](0, 255, 16, 128, 65)) 'application/octet-stream'
                } elseif ($path -eq '/api/v1/download' -and $method -eq 'GET' -and $sessionOk) {
                    Send-HttpResponse $stream 200 'OK' ([byte[]](80, 75, 3, 4, 1, 2, 3)) 'application/zip' @(
                        'Content-Disposition: attachment; filename="proof.zip"',
                        'Cache-Control: no-store',
                        'X-Content-Type-Options: nosniff'
                    )
                } elseif ($path.StartsWith('/api/v1/stale-mutation?') -and $method -eq 'POST') {
                    Send-Json $stream 401 'Unauthorized' '{"error":{"code":"unauthenticated","message":"session expired"}}'
                } elseif ($path -eq '/api/v1/after-reconnect' -and $method -eq 'GET' -and $sessionOk -and $loginGeneration -ge 2) {
                    Send-Json $stream 200 'OK' '{"generation":2}'
                } elseif ($path -eq '/api/v1/bootstrap-required' -and $method -eq 'GET') {
                    Send-Json $stream 428 'Precondition Required' '{"error":{"code":"operator_bootstrap_required","message":"bootstrap required"}}'
                } else {
                    Send-Json $stream 403 'Forbidden' '{"error":{"code":"request_denied","message":"proof request rejected"}}'
                }
            }
            finally {
                $client.Dispose()
            }
        }
    }
    finally {
        $listener.Stop()
    }
}

try {
    $readyDeadline = [DateTime]::UtcNow.AddSeconds(15)
    while (-not (Test-Path -LiteralPath $readyPath) -and [DateTime]::UtcNow -lt $readyDeadline) {
        Start-Sleep -Milliseconds 25
    }
    Assert-True (Test-Path -LiteralPath $readyPath) 'the loopback fake must start'

    $securePassword = ConvertTo-SecureString $passwordCanary -AsPlainText -Force
    $credential = [Management.Automation.PSCredential]::new($username, $securePassword)
    $invalidBaseError = $null
    try {
        New-TownLightProofSession -BaseUri 'https://example.invalid' -Credential $credential | Out-Null
    } catch { $invalidBaseError = $_.Exception.Message }
    Assert-True ($invalidBaseError -match 'HTTP loopback URI') `
        'non-loopback or HTTPS proof endpoints must be rejected before authentication'

    $session = New-TownLightProofSession -BaseUri $origin -Credential $credential
    Assert-Equal $username $session.Identity.username 'login must be confirmed through the session endpoint'
    $sessionDisplay = $session | Out-String
    foreach ($canary in @($passwordCanary, $sessionOne, $csrfOne)) {
        Assert-True (-not $sessionDisplay.Contains($canary)) 'default session output must not expose secret canaries'
    }

    $safe = Invoke-TownLightProofApi -Session $session -Method Get -Path '/safe-json'
    Assert-Equal 'safe' $safe.value 'authenticated safe GET must return JSON'

    $text = Invoke-TownLightProofApi -Session $session -Method Get -Path '/safe-text' -ResponseMode Text
    Assert-Equal 'plain proof text' $text 'text mode must preserve response text'

    $mutation = Invoke-TownLightProofApi -Session $session -Method Put -Path '/mutate' -Body @{ value = 7 }
    Assert-True ([bool]$mutation.changed) 'unsafe JSON request must succeed with CSRF authority'

    [byte[]]$upload = 0, 255, 16, 128, 65
    [byte[]]$raw = Invoke-TownLightProofApi -Session $session -Method Post -Path '/upload' `
        -Body $upload -ContentType 'application/octet-stream' -ResponseMode Raw
    Assert-Equal 5 $raw.Length 'raw response mode must preserve byte count'
    Assert-Equal 255 $raw[1] 'raw response mode must preserve non-text bytes'

    $download = Invoke-TownLightProofApi -Session $session -Method Get -Path '/download' `
        -ResponseMode Download -OutFile $downloadPath
    Assert-Equal 200 $download.StatusCode 'download mode must return safe response metadata'
    Assert-True (Test-Path -LiteralPath $downloadPath -PathType Leaf) 'download mode must create the target file'
    Assert-Equal 7 (Get-Item -LiteralPath $downloadPath).Length 'download mode must preserve bytes'
    Assert-Equal 'no-store' $download.Headers['Cache-Control'] 'download mode must preserve required public headers'

    $sessionBeforeStale = $session.WebSession
    $staleError = $null
    try {
        Invoke-TownLightProofApi -Session $session -Method Post -Path '/stale-mutation?detail=do-not-echo' -Body @{} | Out-Null
    } catch { $staleError = $_.Exception.Message }
    Assert-True ($staleError -match 'HTTP 401' -and $staleError -match 'not replayed') `
        'stale session failure must be sanitized and explicitly promise no replay'
    Assert-True ($staleError -match 'unauthenticated') 'bounded nested API error codes may remain actionable'
    Assert-True (-not $staleError.Contains('detail=do-not-echo')) `
        'sanitized errors must not echo request query strings'
    foreach ($canary in @($passwordCanary, $sessionOne, $csrfOne)) {
        Assert-True (-not $staleError.Contains($canary)) 'sanitized errors must not expose secret canaries'
    }
    Assert-True ($null -eq $session.Identity) '401 must clear the stale authenticated identity'
    Assert-True (-not [object]::ReferenceEquals($sessionBeforeStale, $session.WebSession)) `
        '401 must discard the entire stale cookie jar'
    $staleRequests = @(Get-Content -LiteralPath $transcriptPath | ConvertFrom-Json | Where-Object path -like '/api/v1/stale-mutation*')
    Assert-Equal 1 $staleRequests.Count 'an unsafe stale request must never be replayed automatically'

    $postStaleError = $null
    try {
        Invoke-TownLightProofApi -Session $session -Method Post -Path '/mutate' -Body @{} | Out-Null
    } catch { $postStaleError = $_.Exception.Message }
    Assert-True ($postStaleError -match 'no usable CSRF cookie') `
        'a second unsafe request after 401 must fail locally until explicit reconnect'
    $mutationRequestsAfterStale = @(Get-Content -LiteralPath $transcriptPath | ConvertFrom-Json | Where-Object path -eq '/api/v1/mutate')
    Assert-Equal 1 $mutationRequestsAfterStale.Count 'post-401 unsafe work must not reach the network'

    $emptySession = [pscustomobject]@{
        Origin = $session.Origin
        ApiRoot = $session.ApiRoot
        WebSession = [Microsoft.PowerShell.Commands.WebRequestSession]::new()
        Identity = $session.Identity
    }
    $emptySession.PSObject.TypeNames.Insert(0, 'TownLight.InstalledProofSession')
    $missingCsrfError = $null
    try {
        Invoke-TownLightProofApi -Session $emptySession -Method Post -Path '/mutate' -Body @{} | Out-Null
    } catch { $missingCsrfError = $_.Exception.Message }
    Assert-True ($missingCsrfError -match 'no usable CSRF cookie') `
        'unsafe requests without a CSRF cookie must fail locally'

    $invalidPathError = $null
    try {
        Invoke-TownLightProofApi -Session $session -Method Get -Path '//evil.invalid/path' | Out-Null
    } catch { $invalidPathError = $_.Exception.Message }
    Assert-True ($invalidPathError -match 'bounded root-relative') `
        'absolute-form request targets must be rejected locally'

    $priorWebSession = $session.WebSession
    $reconnectOutput = @(Connect-TownLightProofSession -Session $session -Credential $credential)
    Assert-Equal 0 $reconnectOutput.Count 'explicit reconnect must not emit session or credential material'
    Assert-True (-not [object]::ReferenceEquals($priorWebSession, $session.WebSession)) `
        'explicit reconnect must discard the old cookie jar'
    $afterReconnect = Invoke-TownLightProofApi -Session $session -Method Get -Path '/after-reconnect'
    Assert-Equal 2 $afterReconnect.generation 'fresh login after restart must restore authenticated access'
    $sessionBeforeBootstrapGate = $session.WebSession
    $bootstrapGateError = $null
    try {
        Invoke-TownLightProofApi -Session $session -Method Get -Path '/bootstrap-required' | Out-Null
    } catch { $bootstrapGateError = $_.Exception.Message }
    Assert-True ($bootstrapGateError -match 'HTTP 428' -and $bootstrapGateError -match 'operator_bootstrap_required') `
        'bootstrap-required responses must remain actionable without response detail leakage'
    Assert-True ($null -eq $session.Identity -and -not [object]::ReferenceEquals($sessionBeforeBootstrapGate, $session.WebSession)) `
        '428 must clear identity and discard the cookie jar'

    $transcript = @(Get-Content -LiteralPath $transcriptPath | ConvertFrom-Json)
    $loginRequests = @($transcript | Where-Object path -eq '/api/v1/auth/login')
    Assert-Equal 2 $loginRequests.Count 'initial connection and explicit reconnect must each login exactly once'
    Assert-True (@($loginRequests | Where-Object { -not $_.origin_ok -or -not $_.sec_fetch_site_ok -or -not $_.login_body_ok }).Count -eq 0) `
        'login requests must carry exact Origin, Sec-Fetch-Site, and PSCredential values'
    $safeRequest = @($transcript | Where-Object path -eq '/api/v1/safe-json')[0]
    Assert-True ($safeRequest.session_ok -and -not $safeRequest.csrf_ok) `
        'safe GET must carry cookies without manufacturing a CSRF header'
    $mutationRequest = @($transcript | Where-Object path -eq '/api/v1/mutate')[0]
    Assert-True ($mutationRequest.session_ok -and $mutationRequest.origin_ok -and $mutationRequest.sec_fetch_site_ok -and $mutationRequest.csrf_ok) `
        'unsafe request must carry cookie, exact Origin, Sec-Fetch-Site, and exact X-TownLight-CSRF'
    $uploadRequest = @($transcript | Where-Object path -eq '/api/v1/upload')[0]
    Assert-True ($uploadRequest.upload_body_ok -and $uploadRequest.content_type -eq 'application/octet-stream') `
        'upload mode must preserve opaque bytes and content type'

    $artifactText = (Get-ChildItem -LiteralPath $temporaryRoot -File | ForEach-Object {
        [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($_.FullName))
    }) -join "`n"
    foreach ($canary in @($passwordCanary, $sessionOne, $csrfOne, $sessionTwo, $csrfTwo)) {
        Assert-True (-not $artifactText.Contains($canary)) 'secret canaries must not reach test artifacts'
    }
    $environmentText = ((Get-ChildItem Env:).Value | ForEach-Object { [string]$_ }) -join "`n"
    foreach ($canary in @($passwordCanary, $sessionOne, $csrfOne, $sessionTwo, $csrfTwo)) {
        Assert-True (-not $environmentText.Contains($canary)) 'secret canaries must not reach environment variables'
    }
    $processText = [Environment]::CommandLine
    foreach ($canary in @($passwordCanary, $sessionOne, $csrfOne, $sessionTwo, $csrfTwo)) {
        Assert-True (-not $processText.Contains($canary)) 'secret canaries must not reach the proof process command line'
    }
    Assert-True (@($transcript | Where-Object { -not $_.process_command_clean }).Count -eq 0) `
        'secret canaries must not reach the fake-server process command line'
    Assert-True (@($transcript | Where-Object { -not $_.environment_clean }).Count -eq 0) `
        'secret canaries must not reach the fake-server environment'

    Write-Output "Installed proof auth contract passed ($assertionCount assertions)."
}
finally {
    if ($server) {
        Set-Content -LiteralPath $stopPath -Value 'stop' -Encoding ascii -ErrorAction SilentlyContinue
        Wait-Job -Job $server -Timeout 5 -ErrorAction SilentlyContinue | Out-Null
        if ($server.State -eq 'Running') { Stop-Job -Job $server -ErrorAction SilentlyContinue }
        Remove-Job -Job $server -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $temporaryRoot -PathType Container) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
