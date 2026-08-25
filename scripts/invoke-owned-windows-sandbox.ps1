[CmdletBinding(DefaultParameterSetName = 'Launch')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Launch')]
    [ValidateNotNullOrEmpty()]
    [string]$ConfigurationPath,

    [Parameter(Mandatory, ParameterSetName = 'Check')]
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$sharedMutexName = 'Global\Scott.SharedWindowsSandbox.Launch.v1'
$sandboxProcessNames = @(
    'WindowsSandbox.exe',
    'WindowsSandboxClient.exe',
    'WindowsSandboxRemoteSession.exe',
    'WindowsSandboxServer.exe'
)

function Get-CurrentWindowsSandboxProcesses {
    try {
        $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $_.Name -in $sandboxProcessNames
        })
    } catch {
        throw "Windows Sandbox ownership is unknown because host processes could not be inspected: $($_.Exception.Message)"
    }

    return @($processes | Sort-Object ProcessId | ForEach-Object {
        [pscustomobject]@{
            process_id = [int]$_.ProcessId
            process_name = [string]$_.Name
            creation_time = if ($null -eq $_.CreationDate) { $null } else { [string]$_.CreationDate }
        }
    })
}

function Write-NewOwnershipRecord {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][Collections.IDictionary]$Record
    )

    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(
        (($Record | ConvertTo-Json -Depth 5) + [Environment]::NewLine)
    )
    $stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::Read
    )
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally {
        $stream.Dispose()
    }
}

$mutex = [Threading.Mutex]::new($false, $sharedMutexName)
$mutexAcquired = $false
try {
    try {
        $mutexAcquired = $mutex.WaitOne(0)
    } catch [Threading.AbandonedMutexException] {
        $mutexAcquired = $true
        throw 'Windows Sandbox ownership is unknown because the shared launch mutex was abandoned.'
    }
    if (-not $mutexAcquired) {
        throw 'Windows Sandbox ownership is unknown because another launch decision is in progress.'
    }

    $activeProcesses = @(Get-CurrentWindowsSandboxProcesses)
    if ($activeProcesses.Count -gt 0) {
        $identities = ($activeProcesses | ForEach-Object {
            "$($_.process_name) PID $($_.process_id)"
        }) -join ', '
        throw "Windows Sandbox is occupied by an unclaimed owner ($identities). No launch or cleanup was attempted."
    }

    if ($CheckOnly) {
        [pscustomobject]@{
            schema_version = 1
            status = 'available'
            launch_allowed = $true
            sandbox_process_count = 0
            checked_at_utc = [datetime]::UtcNow.ToString('o')
            shared_mutex = $sharedMutexName
        }
        return
    }

    $repositoryRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
    $configurationRoot = [IO.Path]::GetFullPath(
        (Join-Path $repositoryRoot 'harness\windows-sandbox\configs')
    )
    $resolvedConfiguration = [IO.Path]::GetFullPath($ConfigurationPath)
    $configurationPrefix = $configurationRoot.TrimEnd('\') + '\'
    if (-not $resolvedConfiguration.StartsWith(
        $configurationPrefix,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "The sandbox configuration must be a TownLight-owned file beneath '$configurationRoot'."
    }
    if ([IO.Path]::GetExtension($resolvedConfiguration) -ine '.wsb' -or
        -not (Test-Path -LiteralPath $resolvedConfiguration -PathType Leaf)) {
        throw 'The TownLight-owned Windows Sandbox configuration must be an existing .wsb file.'
    }

    $sandboxExecutable = Join-Path $env:WINDIR 'System32\WindowsSandbox.exe'
    if (-not (Test-Path -LiteralPath $sandboxExecutable -PathType Leaf)) {
        throw 'Windows Sandbox is not installed at the required Windows System32 boundary.'
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $sandboxExecutable
    if ($signature.Status -ne [Management.Automation.SignatureStatus]::Valid -or
        $signature.SignerCertificate.Subject -notmatch '(?i)\bMicrosoft\b') {
        throw 'The Windows Sandbox host executable does not have a valid Microsoft signature.'
    }

    $ownershipRoot = Join-Path $env:ProgramData 'Scott Shared Harness\WindowsSandbox\ownership-v1'
    [void][IO.Directory]::CreateDirectory($ownershipRoot)
    $runId = [guid]::NewGuid().ToString('D')
    $requestRecordPath = Join-Path $ownershipRoot "$runId.request.json"
    $configurationHash = (Get-FileHash -LiteralPath $resolvedConfiguration -Algorithm SHA256).Hash.ToLowerInvariant()
    $requestedAtUtc = [datetime]::UtcNow
    Write-NewOwnershipRecord -Path $requestRecordPath -Record ([ordered]@{
        schema_version = 1
        record_type = 'launch_request'
        owner_namespace = 'townlight-station-codex'
        run_id = $runId
        requested_at_utc = $requestedAtUtc.ToString('o')
        configuration_file = [IO.Path]::GetFileName($resolvedConfiguration)
        configuration_sha256 = $configurationHash
        shared_mutex = $sharedMutexName
    })

    $launchedProcess = Start-Process -FilePath $sandboxExecutable `
        -ArgumentList @($resolvedConfiguration) -PassThru -WindowStyle Normal
    $launchedProcess.Refresh()
    $launchedAtUtc = $launchedProcess.StartTime.ToUniversalTime()
    $observed = Get-CimInstance Win32_Process -Filter "ProcessId = $($launchedProcess.Id)" -ErrorAction Stop
    if ($null -eq $observed -or $observed.Name -ne 'WindowsSandbox.exe') {
        throw 'The launched Windows Sandbox process identity could not be proved; it was left untouched.'
    }

    $launchRecordPath = Join-Path $ownershipRoot "$runId.launched.json"
    Write-NewOwnershipRecord -Path $launchRecordPath -Record ([ordered]@{
        schema_version = 1
        record_type = 'launched_process'
        owner_namespace = 'townlight-station-codex'
        run_id = $runId
        requested_at_utc = $requestedAtUtc.ToString('o')
        launched_at_utc = $launchedAtUtc.ToString('o')
        process_id = [int]$launchedProcess.Id
        process_name = [string]$observed.Name
        process_creation_time = [string]$observed.CreationDate
        executable_sha256 = (Get-FileHash -LiteralPath $sandboxExecutable -Algorithm SHA256).Hash.ToLowerInvariant()
        configuration_file = [IO.Path]::GetFileName($resolvedConfiguration)
        configuration_sha256 = $configurationHash
        request_record_file = [IO.Path]::GetFileName($requestRecordPath)
    })

    [pscustomobject]@{
        schema_version = 1
        status = 'launched'
        owner_namespace = 'townlight-station-codex'
        run_id = $runId
        process_id = [int]$launchedProcess.Id
        process_creation_time = [string]$observed.CreationDate
        ownership_record = $launchRecordPath
        automatic_cleanup = $false
    }
} finally {
    if ($mutexAcquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
