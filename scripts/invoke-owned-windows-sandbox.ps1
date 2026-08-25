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
$sandboxProcessBaseNames = @(
    'WindowsSandbox',
    'WindowsSandboxClient',
    'WindowsSandboxRemoteSession',
    'WindowsSandboxServer'
)

function Get-CurrentWindowsSandboxProcesses {
    try {
        $processes = @(Get-Process -ErrorAction Stop | Where-Object {
            $_.ProcessName -in $sandboxProcessBaseNames
        })
    } catch {
        throw "Windows Sandbox ownership is unknown because host processes could not be inspected: $($_.Exception.Message)"
    }

    try {
        return @($processes | Sort-Object Id | ForEach-Object {
            $executablePath = [IO.Path]::GetFullPath($_.Path)
            [pscustomobject]@{
                process_id = [int]$_.Id
                process_name = "$($_.ProcessName).exe"
                creation_time = $_.StartTime.ToUniversalTime().ToString('o')
                executable_path = $executablePath
            }
        })
    } catch {
        throw "Windows Sandbox ownership is unknown because a detected process identity could not be read: $($_.Exception.Message)"
    }
}

function Resolve-WindowsSandboxOwnership {
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][array]$Processes,
        [Parameter(Mandatory)][string]$OwnershipRoot
    )

    if ($Processes.Count -eq 0) {
        return [pscustomobject]@{
            ownership = 'none_observed'
            owner_namespace = $null
            run_id = $null
            ownership_record = $null
        }
    }

    if (-not (Test-Path -LiteralPath $OwnershipRoot -PathType Container)) {
        return [pscustomobject]@{
            ownership = 'external_or_unclaimed'
            owner_namespace = $null
            run_id = $null
            ownership_record = $null
        }
    }

    try {
        $records = @(Get-ChildItem -LiteralPath $OwnershipRoot -Filter '*.launched.json' -File |
            ForEach-Object {
                $record = Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json
                [pscustomobject]@{ path = $_.FullName; record = $record }
            })
    } catch {
        throw "Windows Sandbox ownership is unknown because ownership records could not be inspected: $($_.Exception.Message)"
    }

    $matches = @()
    foreach ($process in $Processes) {
        $processMatches = @($records | Where-Object {
            $_.record.schema_version -eq 1 -and
            $_.record.record_type -eq 'launched_process' -and
            $_.record.owner_namespace -eq 'townlight-station-codex' -and
            [int]$_.record.process_id -eq $process.process_id -and
            $_.record.process_name -eq $process.process_name -and
            $_.record.process_creation_time -eq $process.creation_time -and
            ([IO.Path]::GetFullPath([string]$_.record.executable_path)).Equals(
                $process.executable_path,
                [StringComparison]::OrdinalIgnoreCase
            )
        })
        if ($processMatches.Count -ne 1) {
            return [pscustomobject]@{
                ownership = 'external_or_unclaimed'
                owner_namespace = $null
                run_id = $null
                ownership_record = $null
            }
        }
        $matches += $processMatches[0]
    }

    $runIds = @($matches | ForEach-Object { [string]$_.record.run_id } | Sort-Object -Unique)
    if ($runIds.Count -ne 1) {
        return [pscustomobject]@{
            ownership = 'external_or_unclaimed'
            owner_namespace = $null
            run_id = $null
            ownership_record = $null
        }
    }

    return [pscustomobject]@{
        ownership = 'townlight_harness'
        owner_namespace = 'townlight-station-codex'
        run_id = $runIds[0]
        ownership_record = $matches[0].path
    }
}

function Get-ExactLaunchedWindowsSandboxProcess {
    param(
        [Parameter(Mandatory)][int]$ProcessId,
        [Parameter(Mandatory)][string]$ExpectedExecutable
    )

    try {
        $process = Get-Process -Id $ProcessId -ErrorAction Stop
        $observedPath = [IO.Path]::GetFullPath($process.Path)
        $expectedPath = [IO.Path]::GetFullPath($ExpectedExecutable)
        if ($process.ProcessName -ne 'WindowsSandbox' -or
            -not $observedPath.Equals($expectedPath, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'the observed process name or executable path did not match the requested Windows Sandbox host'
        }
        return [pscustomobject]@{
            process_id = [int]$process.Id
            process_name = "$($process.ProcessName).exe"
            creation_time = $process.StartTime.ToUniversalTime().ToString('o')
            executable_path = $observedPath
        }
    } catch {
        throw "The launched Windows Sandbox process identity could not be proved; it was left untouched. $($_.Exception.Message)"
    }
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

    $ownershipRoot = Join-Path $env:ProgramData 'Scott Shared Harness\WindowsSandbox\ownership-v1'
    $activeProcesses = @(Get-CurrentWindowsSandboxProcesses)
    $observedOwnership = Resolve-WindowsSandboxOwnership `
        -Processes $activeProcesses `
        -OwnershipRoot $ownershipRoot
    if ($activeProcesses.Count -gt 0) {
        $identities = ($activeProcesses | ForEach-Object {
            "$($_.process_name) PID $($_.process_id)"
        }) -join ', '
        if ($CheckOnly) {
            [pscustomobject]@{
                schema_version = 1
                status = 'occupied'
                ownership = $observedOwnership.ownership
                owner_namespace = $observedOwnership.owner_namespace
                run_id = $observedOwnership.run_id
                ownership_record = $observedOwnership.ownership_record
                launch_allowed = $false
                sandbox_process_count = $activeProcesses.Count
                processes = $activeProcesses
                checked_at_utc = [datetime]::UtcNow.ToString('o')
                shared_mutex = $sharedMutexName
                guard_reason = 'An active Sandbox process is a no-touch boundary unless exact harness ownership is proved.'
            }
            return
        }
        $ownerDescription = if ($observedOwnership.ownership -eq 'townlight_harness') {
            "the TownLight harness run $($observedOwnership.run_id)"
        } else {
            'an external or unclaimed owner'
        }
        throw "Windows Sandbox is occupied by $ownerDescription ($identities). No launch or cleanup was attempted."
    }

    if ($CheckOnly) {
        [pscustomobject]@{
            schema_version = 1
            status = 'no_active_sandbox_detected'
            ownership = 'none_observed'
            owner_namespace = $null
            run_id = $null
            ownership_record = $null
            launch_allowed = $true
            sandbox_process_count = 0
            processes = @()
            checked_at_utc = [datetime]::UtcNow.ToString('o')
            shared_mutex = $sharedMutexName
            guard_reason = 'This is an instantaneous guarded observation, not an assumption that the shared Sandbox remains free.'
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
    $observed = Get-ExactLaunchedWindowsSandboxProcess `
        -ProcessId $launchedProcess.Id `
        -ExpectedExecutable $sandboxExecutable

    $launchRecordPath = Join-Path $ownershipRoot "$runId.launched.json"
    Write-NewOwnershipRecord -Path $launchRecordPath -Record ([ordered]@{
        schema_version = 1
        record_type = 'launched_process'
        owner_namespace = 'townlight-station-codex'
        run_id = $runId
        requested_at_utc = $requestedAtUtc.ToString('o')
        launched_at_utc = $observed.creation_time
        process_id = $observed.process_id
        process_name = $observed.process_name
        process_creation_time = $observed.creation_time
        executable_path = $observed.executable_path
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
        process_id = $observed.process_id
        process_creation_time = $observed.creation_time
        ownership_record = $launchRecordPath
        automatic_cleanup = $false
    }
} finally {
    if ($mutexAcquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
