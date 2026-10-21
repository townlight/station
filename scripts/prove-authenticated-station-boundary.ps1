[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [Management.Automation.PSCredential]$Credential,
    [uri]$BaseUri = 'http://127.0.0.1:4070',
    [ValidateRange(30, 300)]
    [int]$RecoveryTimeoutSec = 90
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'installed-proof-api.ps1')
. (Join-Path $PSScriptRoot 'authenticated-station-boundary-scan.ps1')

function Assert-Proof {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Get-SanitizedHttpStatus {
    param([Management.Automation.ErrorRecord]$ErrorRecord)
    try {
        if ($null -ne $ErrorRecord.Exception.Response.StatusCode) {
            return [int]$ErrorRecord.Exception.Response.StatusCode
        }
    } catch {}
    return 0
}

function Get-AuthenticationCookieValue {
    param(
        [Parameter(Mandatory = $true)]$Session,
        [Parameter(Mandatory = $true)][string]$Name
    )
    $cookieUri = [uri]($Session.ApiRoot.TrimEnd('/') + '/')
    $cookies = @($Session.WebSession.Cookies.GetCookies($cookieUri) | Where-Object Name -CEQ $Name)
    if ($cookies.Count -ne 1 -or [string]::IsNullOrEmpty([string]$cookies[0].Value)) {
        throw 'The authenticated session cookie contract was incomplete.'
    }
    return [string]$cookies[0].Value
}

function Wait-ForTownLightHealth {
    param([uri]$Origin, [int]$TimeoutSec)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Method Get -Uri "$($Origin.AbsoluteUri.TrimEnd('/'))/health" `
                -TimeoutSec 3 -MaximumRedirection 0 -UseBasicParsing -ErrorAction Stop
            $document = $response.Content | ConvertFrom-Json -ErrorAction Stop
            if ([int]$response.StatusCode -eq 200 -and $document.status -eq 'ready' -and
                $document.database -eq 'ready' -and $document.orchestration.status -eq 'ready') {
                return
            }
        } catch {}
        Start-Sleep -Milliseconds 250
    }
    throw 'Townlight Station did not return ready health after its service restart.'
}

function Get-BoundedFiles {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Filter,
        [Parameter(Mandatory = $true)][int]$MaxFiles,
        [switch]$Recurse
    )
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { return @() }
    $option = if ($Recurse) { [IO.SearchOption]::AllDirectories } else { [IO.SearchOption]::TopDirectoryOnly }
    $enumerator = [IO.Directory]::EnumerateFiles($Root, $Filter, $option).GetEnumerator()
    $files = [Collections.Generic.List[string]]::new()
    try {
        while ($enumerator.MoveNext()) {
            if ($files.Count -ge $MaxFiles) { throw 'An authentication artifact group exceeded its file-count bound.' }
            $files.Add([IO.Path]::GetFullPath([string]$enumerator.Current))
        }
    } finally { $enumerator.Dispose() }
    return $files.ToArray()
}

function Assert-FileContainsNoAuthenticationSecret {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][byte[][]]$Patterns,
        [Parameter(Mandatory = $true)][long]$MaxBytes,
        [Parameter(Mandatory = $true)][DateTime]$DeadlineUtc,
        [Parameter(Mandatory = $true)][string]$Group
    )
    $share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
    $stream = [IO.FileStream]::new($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
    try {
        if (Test-TownLightSecretInStream -Stream $stream -Patterns $Patterns `
            -MaxBytes $MaxBytes -DeadlineUtc $DeadlineUtc) {
            throw "A plaintext authentication secret was present in the bounded $Group artifact set."
        }
    } finally { $stream.Dispose() }
}

function Invoke-BoundedAuthenticationArtifactScan {
    param(
        [Parameter(Mandatory = $true)][string]$DataDirectory,
        [Parameter(Mandatory = $true)][byte[][]]$Patterns,
        [Parameter(Mandatory = $true)][string]$RequiredSupportBundlePath
    )
    $scanDeadlineUtc = [DateTime]::UtcNow.AddSeconds(120)
    [long]$scannedBytes = 0
    [int]$scannedFiles = 0
    $databaseFiles = @(
        'station.db', 'station.db-wal', 'station.db-shm'
    ) | ForEach-Object { Join-Path $DataDirectory $_ } | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }
    foreach ($path in $databaseFiles) {
        $length = [long](Get-Item -LiteralPath $path).Length
        if ($length -gt 536870912 -or ($scannedBytes + $length) -gt 1073741824) {
            throw 'The installed database artifact set exceeded its bounded scan budget.'
        }
        Assert-FileContainsNoAuthenticationSecret -Path $path -Patterns $Patterns `
            -MaxBytes 536870912 -DeadlineUtc $scanDeadlineUtc -Group 'database'
        $scannedBytes += $length
        $scannedFiles++
    }
    Assert-Proof ($databaseFiles.Count -ge 1) 'The installed station database was absent from the authentication scan.'

    $receiptFiles = @(Get-BoundedFiles -Root $DataDirectory -Filter '*.json' -MaxFiles 64)
    $requiredReceiptPath = [IO.Path]::GetFullPath((Join-Path $DataDirectory 'install-receipt.json'))
    Assert-Proof (@($receiptFiles | Where-Object {
        [string]::Equals($_, $requiredReceiptPath, [StringComparison]::OrdinalIgnoreCase)
    }).Count -eq 1) 'The required installed receipt was absent from the bounded receipt scan.'
    foreach ($path in $receiptFiles) {
        $length = [long](Get-Item -LiteralPath $path).Length
        if ($length -gt 8388608 -or ($scannedBytes + $length) -gt 1140850688) {
            throw 'The installed receipt artifact set exceeded its bounded scan budget.'
        }
        Assert-FileContainsNoAuthenticationSecret -Path $path -Patterns $Patterns `
            -MaxBytes 8388608 -DeadlineUtc $scanDeadlineUtc -Group 'receipt'
        $scannedBytes += $length
        $scannedFiles++
    }

    $logsDirectory = Join-Path $DataDirectory 'logs'
    $logFiles = @(Get-BoundedFiles -Root $logsDirectory -Filter '*' -MaxFiles 256 -Recurse)
    [long]$logBytes = 0
    foreach ($path in $logFiles) {
        $length = [long](Get-Item -LiteralPath $path).Length
        $logBytes += $length
        if ($length -gt 8388608 -or $logBytes -gt 67108864) {
            throw 'The installed log artifact set exceeded its bounded scan budget.'
        }
        Assert-FileContainsNoAuthenticationSecret -Path $path -Patterns $Patterns `
            -MaxBytes 8388608 -DeadlineUtc $scanDeadlineUtc -Group 'log'
        $scannedBytes += $length
        $scannedFiles++
    }

    Add-Type -AssemblyName System.IO.Compression
    $supportDirectory = Join-Path $DataDirectory 'support-bundles'
    $supportFiles = @(Get-BoundedFiles -Root $supportDirectory -Filter '*' -MaxFiles 32)
    [long]$supportCompressedBytes = 0
    [long]$supportExpandedBytes = 0
    [int]$supportEntries = 0
    $requiredSupportPath = [IO.Path]::GetFullPath($RequiredSupportBundlePath)
    $requiredSupportFound = $false
    $requiredSupportExpanded = $false
    foreach ($path in $supportFiles) {
        $isRequiredSupport = [string]::Equals(
            $path, $requiredSupportPath, [StringComparison]::OrdinalIgnoreCase)
        if ($isRequiredSupport) { $requiredSupportFound = $true }
        $length = [long](Get-Item -LiteralPath $path).Length
        $supportCompressedBytes += $length
        if ($length -gt 67108864 -or $supportCompressedBytes -gt 268435456) {
            throw 'The installed support artifact set exceeded its compressed scan budget.'
        }
        Assert-FileContainsNoAuthenticationSecret -Path $path -Patterns $Patterns `
            -MaxBytes 67108864 -DeadlineUtc $scanDeadlineUtc -Group 'support'
        $scannedBytes += $length
        $scannedFiles++

        if ([IO.Path]::GetExtension($path) -cne '.zip') { continue }
        $share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
        $zipStream = [IO.FileStream]::new($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
        $archive = [System.IO.Compression.ZipArchive]::new($zipStream, [IO.Compression.ZipArchiveMode]::Read, $false)
        [int]$entryCountForFile = 0
        try {
            foreach ($entry in $archive.Entries) {
                if (-not $entry.Name) { continue }
                $entryCountForFile++
                $supportEntries++
                $supportExpandedBytes += [long]$entry.Length
                if ($supportEntries -gt 640 -or [long]$entry.Length -gt 16777216 -or
                    $supportExpandedBytes -gt 268435456) {
                    throw 'The installed support artifact set exceeded its expanded scan budget.'
                }
                $entryStream = $entry.Open()
                try {
                    if (Test-TownLightSecretInStream -Stream $entryStream -Patterns $Patterns `
                        -MaxBytes 16777216 -DeadlineUtc $scanDeadlineUtc) {
                        throw 'A plaintext authentication secret was present in the bounded support artifact set.'
                    }
                } finally { $entryStream.Dispose() }
            }
            if ($isRequiredSupport -and $entryCountForFile -gt 0) { $requiredSupportExpanded = $true }
        } finally {
            $archive.Dispose()
            $zipStream.Dispose()
        }
    }
    Assert-Proof $requiredSupportFound 'The freshly generated support bundle was absent from the bounded support scan.'
    Assert-Proof $requiredSupportExpanded 'The freshly generated support bundle had no expanded entries in the bounded scan.'
    return [pscustomobject]@{
        files = $scannedFiles
        bytes = $scannedBytes
        support_entries = $supportEntries
    }
}

$passwordPlain = $null
$sessionTokenBefore = $null
$csrfTokenBefore = $null
$sessionTokenAfter = $null
$csrfTokenAfter = $null
$networkCredential = $null
try {
    $windowsIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        $windowsPrincipal = [Security.Principal.WindowsPrincipal]::new($windowsIdentity)
        Assert-Proof ($windowsPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) `
            'The authenticated station boundary proof must run from an elevated administrator session.'
    } finally { $windowsIdentity.Dispose() }

    $coordinates = Get-TownLightProofApiCoordinates -BaseUri $BaseUri
    $origin = [uri]$coordinates.Origin
    $apiSession = New-TownLightProofSession -BaseUri $BaseUri -Credential $Credential
    $sessionBefore = (Invoke-TownLightProofApi -Session $apiSession -Method Get -Path '/auth/session').session
    Assert-Proof ($null -ne $sessionBefore -and
        [string]$sessionBefore.username -ceq [string]$Credential.UserName -and
        -not [string]::IsNullOrWhiteSpace([string]$sessionBefore.account_id)) `
        'The authenticated session endpoint did not confirm the requested operator identity.'
    $accountId = [string]$sessionBefore.account_id

    $emptyJar = [Microsoft.PowerShell.Commands.WebRequestSession]::new()
    $emptyStatus = 0
    try {
        Invoke-WebRequest -Method Get -Uri "$($coordinates.ApiRoot)/station" -WebSession $emptyJar `
            -TimeoutSec 10 -MaximumRedirection 0 -UseBasicParsing -ErrorAction Stop | Out-Null
    } catch { $emptyStatus = Get-SanitizedHttpStatus -ErrorRecord $_ }
    Assert-Proof ($emptyStatus -eq 401) 'The protected GET with an empty cookie jar did not receive HTTP 401.'

    $schedules = @(Invoke-TownLightProofApi -Session $apiSession -Method Get -Path '/self-test-schedules')
    $dailySchedule = @($schedules | Where-Object kind -CEQ 'daily')
    Assert-Proof ($dailySchedule.Count -eq 1) 'The durable daily self-test schedule was unavailable for the identity proof.'
    $dailySchedule = $dailySchedule[0]
    # CONTRACT: identity-free mutation body begins
    $mutationBody = [ordered]@{
        enabled = [bool]$dailySchedule.enabled
        local_hour = [int]$dailySchedule.local_hour
        local_minute = [int]$dailySchedule.local_minute
        weekday = $null
    }
    # CONTRACT: identity-free mutation body ends
    $mutationResponse = Invoke-TownLightProofApi -Session $apiSession -Method Put -Path '/self-test-schedules/daily' -Body $mutationBody
    Assert-Proof ([string]$mutationResponse.updated_by -ceq $accountId) `
        'The identity-free mutation response was not attributed to the authenticated account.'
    $durableSchedules = @(Invoke-TownLightProofApi -Session $apiSession -Method Get -Path '/self-test-schedules')
    $durableMutation = @($durableSchedules | Where-Object {
        $_.kind -ceq 'daily' -and [int64]$_.updated_at_unix_ms -eq [int64]$mutationResponse.updated_at_unix_ms
    }) | Select-Object -First 1
    Assert-Proof ($null -ne $durableMutation -and [string]$durableMutation.updated_by -ceq $accountId) `
        'The durable mutation record was not attributed to the authenticated account.'

    $channelsBefore = @(Invoke-TownLightProofApi -Session $apiSession -Method Get -Path '/channels')
    $enabledChannelIdsBefore = @($channelsBefore | Where-Object enabled | ForEach-Object channel_id | Sort-Object)
    $channelSnapshotBefore = $channelsBefore | ConvertTo-Json -Depth 32 -Compress
    $runtimeBefore = if ($enabledChannelIdsBefore.Count) {
        Invoke-TownLightProofApi -Session $apiSession -Method Get -Path '/runtime-safe-to-air'
    } else { $null }

    $sessionTokenBefore = Get-AuthenticationCookieValue -Session $apiSession -Name 'townlight_session_v1'
    $csrfTokenBefore = Get-AuthenticationCookieValue -Session $apiSession -Name 'townlight_csrf_v1'
    $oldJar = $apiSession.WebSession

    $restartJob = Start-Job -ScriptBlock { Restart-Service -Name 'TownLightStation' -Force -ErrorAction Stop }
    try {
        $completed = Wait-Job -Job $restartJob -Timeout 30
        Assert-Proof ($null -ne $completed) 'TownLight Station did not complete its bounded service restart.'
        try { Receive-Job -Job $restartJob -ErrorAction Stop | Out-Null }
        catch { throw 'Townlight Station failed its bounded service restart.' }
    } finally {
        Stop-Job -Job $restartJob -ErrorAction SilentlyContinue
        Remove-Job -Job $restartJob -Force -ErrorAction SilentlyContinue
    }
    Wait-ForTownLightHealth -Origin $origin -TimeoutSec $RecoveryTimeoutSec

    $staleFailure = ''
    try {
        Invoke-TownLightProofApi -Session $apiSession -Method Get -Path '/auth/session' | Out-Null
        throw 'The old session remained valid across the service restart.'
    } catch { $staleFailure = [string]$_.Exception.Message }
    Assert-Proof ($staleFailure -match 'HTTP 401') 'The old session did not fail with sanitized HTTP 401 after restart.'
    Assert-Proof ($null -eq $apiSession.Identity) 'The authenticated helper retained stale identity after HTTP 401.'
    Assert-Proof (-not [object]::ReferenceEquals($oldJar, $apiSession.WebSession)) `
        'The authenticated helper retained its stale cookie jar after HTTP 401.'

    Connect-TownLightProofSession -Session $apiSession -Credential $Credential
    $sessionAfter = (Invoke-TownLightProofApi -Session $apiSession -Method Get -Path '/auth/session').session
    Assert-Proof ($null -ne $sessionAfter -and [string]$sessionAfter.account_id -ceq $accountId -and
        [string]$sessionAfter.username -ceq [string]$Credential.UserName) `
        'The operator account or authenticated session did not persist across explicit reconnect.'
    $sessionTokenAfter = Get-AuthenticationCookieValue -Session $apiSession -Name 'townlight_session_v1'
    $csrfTokenAfter = Get-AuthenticationCookieValue -Session $apiSession -Name 'townlight_csrf_v1'
    Assert-Proof ($sessionTokenAfter -cne $sessionTokenBefore -and $csrfTokenAfter -cne $csrfTokenBefore) `
        'Explicit reconnect did not rotate both authentication cookies.'

    $channelsAfter = @(Invoke-TownLightProofApi -Session $apiSession -Method Get -Path '/channels')
    $enabledChannelIdsAfter = @($channelsAfter | Where-Object enabled | ForEach-Object channel_id | Sort-Object)
    Assert-Proof (($channelSnapshotBefore -ceq ($channelsAfter | ConvertTo-Json -Depth 32 -Compress)) -and
        (($enabledChannelIdsBefore -join '|') -ceq ($enabledChannelIdsAfter -join '|'))) `
        'Installed channel configuration changed across the authentication restart proof.'
    $runtimeRecovered = $enabledChannelIdsBefore.Count -eq 0
    $runtimeAfter = $null
    if ($enabledChannelIdsBefore.Count) {
        $runtimeDeadline = [DateTime]::UtcNow.AddSeconds($RecoveryTimeoutSec)
        $greenBefore = @($runtimeBefore.channels | Where-Object color -CEQ 'green' | ForEach-Object channel_id)
        while ([DateTime]::UtcNow -lt $runtimeDeadline) {
            $runtimeAfter = Invoke-TownLightProofApi -Session $apiSession -Method Get -Path '/runtime-safe-to-air'
            $afterIds = @($runtimeAfter.channels | Where-Object { $_.channel_id -in $enabledChannelIdsBefore } | ForEach-Object channel_id | Sort-Object)
            $greenRecovered = @($greenBefore | Where-Object {
                $id = $_
                @($runtimeAfter.channels | Where-Object { $_.channel_id -ceq $id -and $_.color -ceq 'green' }).Count -eq 1
            }).Count -eq $greenBefore.Count
            if (($afterIds -join '|') -ceq ($enabledChannelIdsBefore -join '|') -and $greenRecovered) {
                $runtimeRecovered = $true
                break
            }
            Start-Sleep -Milliseconds 250
        }
        Assert-Proof $runtimeRecovered 'Configured channel runtime did not recover its pre-restart represented/green boundary.'
    }

    $networkCredential = $Credential.GetNetworkCredential()
    $passwordPlain = [string]$networkCredential.Password
    $patterns = New-TownLightSecretPatterns -Secrets @(
        $passwordPlain, $sessionTokenBefore, $csrfTokenBefore, $sessionTokenAfter, $csrfTokenAfter
    )
    $dataDirectory = Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)) 'TownLight Station'
    $supportRoot = [IO.Path]::GetFullPath((Join-Path $dataDirectory 'support-bundles')) + [IO.Path]::DirectorySeparatorChar
    $freshSupport = Invoke-TownLightProofApi -Session $apiSession -Method Post -Path '/support-bundles'
    $freshSupportId = [Guid]::Empty
    $freshSupportPath = [IO.Path]::GetFullPath([string]$freshSupport.path)
    Assert-Proof ([Guid]::TryParse([string]$freshSupport.bundle_id, [ref]$freshSupportId) -and
        $freshSupportId -ne [Guid]::Empty -and
        $freshSupportPath.StartsWith($supportRoot, [StringComparison]::OrdinalIgnoreCase) -and
        [IO.Path]::GetExtension($freshSupportPath) -ceq '.zip' -and
        [IO.Path]::GetFileName($freshSupportPath) -ceq [string]$freshSupport.file_name -and
        (Test-Path -LiteralPath $freshSupportPath -PathType Leaf)) `
        'The authenticated support-bundle response did not identify one managed ZIP artifact.'
    $freshSupportItem = Get-Item -LiteralPath $freshSupportPath
    Assert-Proof ([uint64]$freshSupportItem.Length -eq [uint64]$freshSupport.size_bytes -and
        [string]::Equals(
            (Get-FileHash -LiteralPath $freshSupportPath -Algorithm SHA256).Hash,
            [string]$freshSupport.sha256,
            [StringComparison]::OrdinalIgnoreCase)) `
        'The freshly generated support bundle did not match its bounded API size/hash record.'
    $scan = Invoke-BoundedAuthenticationArtifactScan -DataDirectory $dataDirectory -Patterns $patterns `
        -RequiredSupportBundlePath $freshSupportPath

    [ordered]@{
        account_id = $accountId
        session_identity_confirmed = $true
        empty_cookie_jar_rejected = $true
        identity_free_mutation_attributed = $true
        old_session_rejected_after_restart = $true
        helper_identity_and_jar_invalidated = $true
        explicit_reconnect_succeeded = $true
        account_persisted = $true
        configured_enabled_channels = $enabledChannelIdsBefore.Count
        runtime_recovered = $runtimeRecovered
        plaintext_authentication_secrets_absent = $true
        fresh_support_bundle_scanned = $true
        scanned_artifact_files = $scan.files
        scanned_artifact_bytes = $scan.bytes
        scanned_support_entries = $scan.support_entries
    } | ConvertTo-Json -Compress
}
finally {
    $passwordPlain = $null
    $sessionTokenBefore = $null
    $csrfTokenBefore = $null
    $sessionTokenAfter = $null
    $csrfTokenAfter = $null
    $networkCredential = $null
}
