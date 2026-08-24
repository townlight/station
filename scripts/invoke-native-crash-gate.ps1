[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$CommandPath,

    [string[]]$ArgumentList = @(),

    [Parameter(Mandatory)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })]
    [string]$WorkingDirectory,

    [Parameter(Mandatory)]
    [string]$EvidencePath,

    [ValidateNotNullOrEmpty()]
    [string[]]$ComponentPattern = @('channel-worker.exe'),

    [ValidateRange(1, 86400)]
    [int]$TimeoutSeconds = 900,

    [ValidateRange(0, 60000)]
    [int]$EventDrainMilliseconds = 10000
)

$ErrorActionPreference = 'Stop'

foreach ($pattern in $ComponentPattern) {
    if ($pattern -notmatch '^[A-Za-z0-9][A-Za-z0-9._*-]{0,127}\.exe$') {
        throw "ComponentPattern must be an executable file-name pattern, not a path: '$pattern'."
    }
}

$eventContracts = @{
    'Application Error' = @(1000)
    'Windows Error Reporting' = @(1001)
    'SideBySide' = @(33, 35, 59)
}
$eventIds = @($eventContracts.Values | ForEach-Object { $_ } | Sort-Object -Unique)

function Get-ApplicationLogCheckpoint {
    $latest = Get-WinEvent -LogName Application -MaxEvents 1 -ErrorAction Stop
    if ($null -eq $latest.RecordId) {
        throw 'The Windows Application log did not provide a record-ID checkpoint.'
    }
    return [long]$latest.RecordId
}

function Get-NewNativeFailureEvents {
    param(
        [long]$AfterRecordId,
        [datetime]$StartedAtUtc
    )

    try {
        $records = @(Get-WinEvent -FilterHashtable @{
            LogName = 'Application'
            Id = $eventIds
            StartTime = $StartedAtUtc.AddSeconds(-1)
        } -ErrorAction Stop)
    } catch {
        if ($_.FullyQualifiedErrorId -match 'NoMatchingEventsFound') { return @() }
        throw
    }

    $matches = [Collections.Generic.List[object]]::new()
    foreach ($record in $records) {
        if ([long]$record.RecordId -le $AfterRecordId) { continue }
        if (-not $eventContracts.ContainsKey($record.ProviderName)) { continue }
        if ([int]$record.Id -notin $eventContracts[$record.ProviderName]) { continue }

        $eventText = [string]$record.Message
        foreach ($component in $ComponentPattern) {
            $matcher = [Management.Automation.WildcardPattern]::new(
                "*$component*",
                [Management.Automation.WildcardOptions]::IgnoreCase
            )
            if (-not $matcher.IsMatch($eventText)) { continue }
            $matches.Add([ordered]@{
                provider = [string]$record.ProviderName
                event_id = [int]$record.Id
                record_id = [long]$record.RecordId
                time_created_utc = $record.TimeCreated.ToUniversalTime().ToString('o')
                component_pattern = $component
            })
            break
        }
    }
    return @($matches | Sort-Object record_id)
}

$resolvedCommand = (Resolve-Path -LiteralPath $CommandPath).Path
$resolvedWorkingDirectory = (Resolve-Path -LiteralPath $WorkingDirectory).Path
$checkpoint = Get-ApplicationLogCheckpoint
$startedAtUtc = [datetime]::UtcNow

$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $resolvedCommand
$startInfo.WorkingDirectory = $resolvedWorkingDirectory
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
foreach ($argument in $ArgumentList) { [void]$startInfo.ArgumentList.Add($argument) }

$process = [Diagnostics.Process]::new()
$process.StartInfo = $startInfo
if (-not $process.Start()) { throw 'The native crash gate could not start the requested process.' }
$stdoutTask = $process.StandardOutput.ReadToEndAsync()
$stderrTask = $process.StandardError.ReadToEndAsync()
$timedOut = -not $process.WaitForExit($TimeoutSeconds * 1000)
if ($timedOut) {
    $process.Kill($true)
    $process.WaitForExit()
}
$stdout = $stdoutTask.GetAwaiter().GetResult()
$stderr = $stderrTask.GetAwaiter().GetResult()
$exitCode = [int]$process.ExitCode
$launchedProcessId = [int]$process.Id
$process.Dispose()

$eventObservationStartedUtc = [datetime]::UtcNow
$eventObservationDeadlineUtc = $eventObservationStartedUtc.AddMilliseconds($EventDrainMilliseconds)
do {
    $events = @(Get-NewNativeFailureEvents -AfterRecordId $checkpoint -StartedAtUtc $startedAtUtc)
    if ($events.Count -gt 0 -or [datetime]::UtcNow -ge $eventObservationDeadlineUtc) { break }
    $remainingMilliseconds = [int][Math]::Ceiling(
        ($eventObservationDeadlineUtc - [datetime]::UtcNow).TotalMilliseconds
    )
    if ($remainingMilliseconds -gt 0) {
        Start-Sleep -Milliseconds ([Math]::Min(250, $remainingMilliseconds))
    }
} while ($true)
$eventObservationFinishedUtc = [datetime]::UtcNow
$finishedAtUtc = [datetime]::UtcNow
$passed = -not $timedOut -and $exitCode -eq 0 -and $events.Count -eq 0

$evidence = [ordered]@{
    schema_version = 1
    result = if ($passed) { 'pass' } else { 'fail' }
    gate_started_utc = $startedAtUtc.ToString('o')
    gate_finished_utc = $finishedAtUtc.ToString('o')
    application_log_checkpoint_record_id = $checkpoint
    command = [ordered]@{
        file = [IO.Path]::GetFileName($resolvedCommand)
        argument_count = $ArgumentList.Count
        launched_process_id = $launchedProcessId
    }
    timeout_seconds = $TimeoutSeconds
    timed_out = $timedOut
    exit_code = $exitCode
    stdout_character_count = $stdout.Length
    stderr_character_count = $stderr.Length
    component_patterns = @($ComponentPattern)
    event_correlation = [ordered]@{
        mode = 'checkpoint-time-and-component-pattern'
        observation_started_utc = $eventObservationStartedUtc.ToString('o')
        observation_deadline_utc = $eventObservationDeadlineUtc.ToString('o')
        observation_finished_utc = $eventObservationFinishedUtc.ToString('o')
        requires_isolated_component_execution = $true
    }
    matching_event_count = $events.Count
    matching_events = $events
}

$evidenceDirectory = Split-Path -Parent $EvidencePath
if ([string]::IsNullOrWhiteSpace($evidenceDirectory)) {
    $evidenceDirectory = (Get-Location).Path
}
New-Item -ItemType Directory -Path $evidenceDirectory -Force | Out-Null
$finalEvidencePath = [IO.Path]::GetFullPath($EvidencePath)
$temporaryEvidencePath = "$finalEvidencePath.$([guid]::NewGuid().ToString('N')).tmp"
try {
    [IO.File]::WriteAllText(
        $temporaryEvidencePath,
        (($evidence | ConvertTo-Json -Depth 6) + [Environment]::NewLine),
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporaryEvidencePath -Destination $finalEvidencePath -Force
} finally {
    if (Test-Path -LiteralPath $temporaryEvidencePath) {
        Remove-Item -LiteralPath $temporaryEvidencePath -Force
    }
}

if ($stdout.Length -gt 0) { [Console]::Out.Write($stdout) }
if ($stderr.Length -gt 0) { [Console]::Error.Write($stderr) }

if (-not $passed) {
    throw "Native crash gate failed; inspect '$finalEvidencePath'."
}

[pscustomobject]@{
    Result = 'pass'
    ExitCode = $exitCode
    MatchingEventCount = $events.Count
    EvidencePath = $finalEvidencePath
}
