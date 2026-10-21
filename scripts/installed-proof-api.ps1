function Get-TownLightProofApiCoordinates {
    param(
        [Parameter(Mandatory = $true)]
        [uri]$BaseUri
    )

    if (-not $BaseUri.IsAbsoluteUri -or $BaseUri.Scheme -ne 'http' -or
        -not $BaseUri.IsLoopback -or $BaseUri.UserInfo -or $BaseUri.Query -or $BaseUri.Fragment) {
        throw 'Townlight installed proofs require an absolute, credential-free HTTP loopback URI.'
    }
    $path = $BaseUri.AbsolutePath.TrimEnd('/')
    if ($path -and $path -ne '/api/v1') {
        throw 'Townlight installed proof BaseUri must end at the service origin or /api/v1.'
    }

    $origin = $BaseUri.GetLeftPart([UriPartial]::Authority)
    [pscustomobject]@{
        Origin = $origin
        ApiRoot = "$origin/api/v1"
    }
}

function Get-TownLightProofFailure {
    param(
        [Parameter(Mandatory = $true)]
        [Management.Automation.ErrorRecord]$ErrorRecord
    )

    $statusCode = 0
    try {
        if ($null -ne $ErrorRecord.Exception.Response -and
            $null -ne $ErrorRecord.Exception.Response.StatusCode) {
            $statusCode = [int]$ErrorRecord.Exception.Response.StatusCode
        }
    } catch { $statusCode = 0 }

    $code = ''
    try {
        if ($ErrorRecord.ErrorDetails.Message) {
            $document = $ErrorRecord.ErrorDetails.Message | ConvertFrom-Json -ErrorAction Stop
            $candidate = if ($document.error -is [string]) {
                [string]$document.error
            } elseif ($document.error.code) {
                [string]$document.error.code
            } elseif ($document.code) {
                [string]$document.code
            } else { '' }
            if ($candidate -match '^[A-Za-z0-9_-]{1,64}$') { $code = $candidate }
        }
    } catch { $code = '' }

    [pscustomobject]@{
        StatusCode = $statusCode
        Code = $code
    }
}

function Get-TownLightProofCsrfToken {
    param(
        [Parameter(Mandatory = $true)]
        $Session
    )

    $cookieUri = [uri]($Session.ApiRoot.TrimEnd('/') + '/')
    $matches = @($Session.WebSession.Cookies.GetCookies($cookieUri) | Where-Object {
        $_.Name -ceq 'townlight_csrf_v1'
    })
    if ($matches.Count -ne 1 -or [string]::IsNullOrWhiteSpace($matches[0].Value)) {
        throw 'Townlight proof session has no usable CSRF cookie; reconnect explicitly before an unsafe request.'
    }
    [string]$matches[0].Value
}

function Assert-TownLightProofSession {
    param(
        [Parameter(Mandatory = $true)]
        $Session
    )

    if ($Session.PSObject.TypeNames -notcontains 'TownLight.InstalledProofSession' -or
        [string]::IsNullOrWhiteSpace([string]$Session.Origin) -or
        [string]::IsNullOrWhiteSpace([string]$Session.ApiRoot) -or
        $null -eq $Session.WebSession) {
        throw 'A connected Townlight installed-proof session is required.'
    }
}

function Get-TownLightProofRequestUri {
    param(
        [Parameter(Mandatory = $true)]
        $Session,
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if ([string]::IsNullOrWhiteSpace($Path) -or -not $Path.StartsWith('/') -or
        $Path.StartsWith('//') -or $Path.Contains('://') -or $Path.Contains('#') -or
        $Path.Contains('..')) {
        throw 'Townlight proof API paths must be bounded root-relative paths without fragments or traversal.'
    }
    [uri]($Session.ApiRoot.TrimEnd('/') + $Path)
}

function Connect-TownLightProofSession {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        $Session,
        [Parameter(Mandatory = $true)]
        [Management.Automation.PSCredential]$Credential,
        [ValidateRange(1, 300)]
        [int]$TimeoutSec = 30
    )

    if ($Session.PSObject.TypeNames -notcontains 'TownLight.InstalledProofSession') {
        throw 'Connect-TownLightProofSession requires a Townlight installed-proof session.'
    }
    if ([string]::IsNullOrWhiteSpace($Credential.UserName)) {
        throw 'Townlight proof credential username is required.'
    }

    # A boot change invalidates every prior session. Replace the entire jar before
    # login so stale cookies cannot cross an explicit reconnect boundary.
    $Session.WebSession = [Microsoft.PowerShell.Commands.WebRequestSession]::new()
    $Session.Identity = $null
    $networkCredential = $null
    $password = $null
    $loginJson = $null
    try {
        $networkCredential = $Credential.GetNetworkCredential()
        $password = $networkCredential.Password
        $loginJson = [ordered]@{
            username = $Credential.UserName
            password = $password
        } | ConvertTo-Json -Compress
        $parameters = @{
            Method = 'Post'
            Uri = "$($Session.ApiRoot)/auth/login"
            WebSession = $Session.WebSession
            Headers = @{
                Origin = $Session.Origin
                'Sec-Fetch-Site' = 'same-origin'
            }
            ContentType = 'application/json'
            Body = $loginJson
            TimeoutSec = $TimeoutSec
            MaximumRedirection = 0
            UseBasicParsing = $true
            ErrorAction = 'Stop'
        }
        Invoke-WebRequest @parameters | Out-Null
    }
    catch {
        $failure = Get-TownLightProofFailure -ErrorRecord $_
        $status = if ($failure.StatusCode) { "HTTP $($failure.StatusCode)" } else { 'transport failure' }
        $code = if ($failure.Code) { ", $($failure.Code)" } else { '' }
        throw "Townlight operator login failed ($status$code); no credential or response detail was recorded."
    }
    finally {
        $loginJson = $null
        $password = $null
        $networkCredential = $null
    }

    try {
        $verification = Invoke-WebRequest -Method Get -Uri "$($Session.ApiRoot)/auth/session" `
            -WebSession $Session.WebSession -TimeoutSec $TimeoutSec -MaximumRedirection 0 `
            -UseBasicParsing -ErrorAction Stop
        $document = $verification.Content | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $document.session -or
            [string]$document.session.username -cne [string]$Credential.UserName) {
            throw 'identity mismatch'
        }
        $Session.Identity = $document.session
    }
    catch {
        $failure = Get-TownLightProofFailure -ErrorRecord $_
        $status = if ($failure.StatusCode) { "HTTP $($failure.StatusCode)" } else { 'invalid response' }
        $code = if ($failure.Code) { ", $($failure.Code)" } else { '' }
        $Session.WebSession = [Microsoft.PowerShell.Commands.WebRequestSession]::new()
        $Session.Identity = $null
        throw "Townlight operator session confirmation failed ($status$code); reconnect is required."
    }
}

function New-TownLightProofSession {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [uri]$BaseUri,
        [Parameter(Mandatory = $true)]
        [Management.Automation.PSCredential]$Credential,
        [ValidateRange(1, 300)]
        [int]$TimeoutSec = 30
    )

    $coordinates = Get-TownLightProofApiCoordinates -BaseUri $BaseUri
    $session = [pscustomobject]@{
        Origin = $coordinates.Origin
        ApiRoot = $coordinates.ApiRoot
        WebSession = $null
        Identity = $null
    }
    $session.PSObject.TypeNames.Insert(0, 'TownLight.InstalledProofSession')
    Connect-TownLightProofSession -Session $session -Credential $Credential -TimeoutSec $TimeoutSec
    return $session
}

function Invoke-TownLightProofApi {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        $Session,
        [Parameter(Mandatory = $true)]
        [ValidateSet('Get', 'Head', 'Options', 'Post', 'Put', 'Patch', 'Delete')]
        [string]$Method,
        [Parameter(Mandatory = $true)]
        [string]$Path,
        $Body = $null,
        [string]$ContentType = 'application/json',
        [ValidateSet('Json', 'Text', 'Raw', 'Download')]
        [string]$ResponseMode = 'Json',
        [string]$OutFile = '',
        [ValidateRange(1, 1800)]
        [int]$TimeoutSec = 30
    )

    Assert-TownLightProofSession -Session $Session
    $requestUri = Get-TownLightProofRequestUri -Session $Session -Path $Path
    $safe = $Method -in @('Get', 'Head', 'Options')
    if ($ResponseMode -eq 'Download' -and [string]::IsNullOrWhiteSpace($OutFile)) {
        throw 'Townlight proof download mode requires an explicit OutFile.'
    }
    if ($ResponseMode -ne 'Download' -and $OutFile) {
        throw 'Townlight proof OutFile is valid only in download mode.'
    }

    $headers = @{}
    if (-not $safe) {
        $headers.Origin = $Session.Origin
        $headers['Sec-Fetch-Site'] = 'same-origin'
        $headers['X-TownLight-CSRF'] = Get-TownLightProofCsrfToken -Session $Session
    }
    $parameters = @{
        Method = $Method
        Uri = $requestUri
        WebSession = $Session.WebSession
        Headers = $headers
        TimeoutSec = $TimeoutSec
        MaximumRedirection = 0
        UseBasicParsing = $true
        ErrorAction = 'Stop'
    }
    if ($null -ne $Body) {
        if ([string]::IsNullOrWhiteSpace($ContentType)) {
            throw 'Townlight proof requests with a body require an explicit content type.'
        }
        $parameters.ContentType = $ContentType
        if ($ContentType -match '^application/json(?:;|$)' -and $Body -isnot [string]) {
            $parameters.Body = $Body | ConvertTo-Json -Depth 32 -Compress
        } else {
            $parameters.Body = $Body
        }
    }
    if ($ResponseMode -eq 'Download') {
        $parameters.OutFile = $OutFile
        $parameters.PassThru = $true
    }

    try {
        $response = Invoke-WebRequest @parameters
    }
    catch {
        $failure = Get-TownLightProofFailure -ErrorRecord $_
        if ($failure.StatusCode -in @(401, 428)) {
            $Session.WebSession = [Microsoft.PowerShell.Commands.WebRequestSession]::new()
            $Session.Identity = $null
        }
        $status = if ($failure.StatusCode) { "HTTP $($failure.StatusCode)" } else { 'transport failure' }
        $code = if ($failure.Code) { ", $($failure.Code)" } else { '' }
        $pathLabel = $Path.Split('?', 2)[0]
        $replay = if ($safe) {
            'The request was not retried.'
        } else {
            'The unsafe request was not replayed; reconnect explicitly before trying again.'
        }
        throw "Townlight API $Method $pathLabel failed ($status$code). $replay"
    }

    switch ($ResponseMode) {
        'Json' {
            if ([string]::IsNullOrWhiteSpace([string]$response.Content)) { return $null }
            try { return ($response.Content | ConvertFrom-Json -ErrorAction Stop) }
            catch { throw "Townlight API $Method $Path returned invalid JSON; response content was not recorded." }
        }
        'Text' { return [string]$response.Content }
        'Raw' {
            $memory = [IO.MemoryStream]::new()
            try {
                if ($response.RawContentStream.CanSeek) { $response.RawContentStream.Position = 0 }
                $response.RawContentStream.CopyTo($memory)
                return ,$memory.ToArray()
            }
            finally { $memory.Dispose() }
        }
        'Download' {
            $safeHeaders = [ordered]@{}
            foreach ($name in @(
                'Content-Type', 'Content-Length', 'Content-Disposition', 'Cache-Control',
                'X-Content-Type-Options'
            )) {
                if ($null -ne $response.Headers[$name]) {
                    $safeHeaders[$name] = [string]($response.Headers[$name] -join ', ')
                }
            }
            return [pscustomobject]@{
                StatusCode = [int]$response.StatusCode
                Headers = $safeHeaders
                OutFile = $OutFile
            }
        }
    }
}
