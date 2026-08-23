param(
    [Parameter(Mandatory = $true)]
    [Management.Automation.PSCredential]$Credential,
    [string]$BaseUri = 'http://127.0.0.1:4070',
    [string]$Manifest = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\candidate-manifest.json'),
    [int]$ListenerPort = 5941,
    [string]$ResultPath = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'installed-proof-api.ps1')
$apiSession = New-TownLightProofSession -BaseUri $BaseUri -Credential $Credential

function Invoke-InstalledProofApi {
    [CmdletBinding()]
    param(
        [Parameter(Position = 0, Mandatory = $true)][string]$Uri,
        [string]$Method = 'Get',
        [string]$ContentType = 'application/json',
        $Body = $null,
        [int]$TimeoutSec = 30
    )
    $requestUri = [uri]$Uri
    $path = $requestUri.PathAndQuery -replace '^/api/v1', ''
    $parameters = @{ Session = $apiSession; Method = $Method; Path = $path; TimeoutSec = $TimeoutSec }
    if ($PSBoundParameters.ContainsKey('Body')) { $parameters.Body = $Body; $parameters.ContentType = $ContentType }
    Invoke-TownLightProofApi @parameters
}
$channelId = '5fc64b78-bf2d-4fd9-8295-0cfc78cd7482'
$sinkId = '14a23aa9-eb09-4209-a308-c4865ebacdef'
$channel = [ordered]@{
    channel_id = $channelId
    display_name = 'Installed required-output stall proof'
    output_profile = 'atsc-720p5994'
    output_sinks = @([ordered]@{
        sink_id = $sinkId
        label = 'Required SRT watchdog proof'
        kind = 'srt'
        destination = "srt://127.0.0.1:$ListenerPort"
        loudness_target_lufs = -24.0
        required = $true
        enabled = $true
    })
    enabled = $true
}
$runtime = Join-Path $env:ProgramFiles 'TownLight Station\runtime\gstreamer'
$launcher = Join-Path $runtime 'bin\gst-launch-1.0.exe'
$firstCapture = Join-Path ([IO.Path]::GetTempPath()) "townlight-output-stall-first-$PID.ts"
$recoveryCapture = Join-Path ([IO.Path]::GetTempPath()) "townlight-output-stall-recovery-$PID.ts"
$listener = $null
$configured = $false

function Start-ProofListener([string]$CapturePath) {
    $arguments = @(
        '-q', 'srtsrc', "uri=srt://127.0.0.1:$ListenerPort", 'mode=listener',
        'localaddress=127.0.0.1', "localport=$ListenerPort", 'latency=200',
        '!', 'fdsink', 'fd=1', 'sync=false', 'async=false'
    )
    $process = Start-Process -FilePath $launcher -ArgumentList $arguments `
        -RedirectStandardOutput $CapturePath -PassThru -WindowStyle Hidden
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        if ($process.HasExited) {
            throw "The SRT proof listener exited before binding with code $($process.ExitCode)."
        }
        $bound = @(
            Get-NetUDPEndpoint -LocalPort $ListenerPort -ErrorAction SilentlyContinue |
                Where-Object OwningProcess -eq $process.Id
        ).Count -gt 0
        if (-not $bound) { Start-Sleep -Milliseconds 100 }
    } while (-not $bound -and [DateTime]::UtcNow -lt $deadline)
    if (-not $bound) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw 'The SRT proof listener did not bind within 20 seconds.'
    }
    return $process
}

function Stop-ProofListener($Process) {
    if ($Process -and -not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction Stop
        if (-not $Process.WaitForExit(5000)) {
            throw 'The SRT proof listener could not be reaped.'
        }
    }
}

function Get-ProofRuntime {
    $status = Invoke-InstalledProofApi -Uri "$BaseUri/api/v1/runtime-safe-to-air" -TimeoutSec 5
    return $status.channels | Where-Object channel_id -eq $channelId | Select-Object -First 1
}

function Get-WorkerExcluding([uint32[]]$ExcludedIds) {
    return Get-Process -Name 'channel-worker' -ErrorAction SilentlyContinue |
        Where-Object { [uint32]$_.Id -notin $ExcludedIds } |
        Sort-Object StartTime -Descending |
        Select-Object -First 1
}

try {
    $candidate = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
    $receipt = Get-Content -LiteralPath (Join-Path $env:ProgramData 'TownLight Station\install-receipt.json') -Raw | ConvertFrom-Json
    if ($receipt.source_commit -ne $candidate.source_commit) {
        throw 'The installed receipt does not identify the candidate under proof.'
    }
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "The installed SRT proof runtime is missing: $launcher"
    }
    $env:PATH = (Join-Path $runtime 'bin') + ';' + $env:PATH
    $env:GST_PLUGIN_PATH_1_0 = Join-Path $runtime 'lib\gstreamer-1.0'
    $env:GST_PLUGIN_SYSTEM_PATH_1_0 = $env:GST_PLUGIN_PATH_1_0
    $baselineWorkerIds = @(
        Get-Process -Name 'channel-worker' -ErrorAction SilentlyContinue |
            ForEach-Object { [uint32]$_.Id }
    )

    $listener = Start-ProofListener $firstCapture
    Invoke-InstalledProofApi -Method Put -Uri "$BaseUri/api/v1/channels" -ContentType 'application/json' `
        -Body ($channel | ConvertTo-Json -Depth 8 -Compress) -TimeoutSec 10 | Out-Null
    $configured = $true

    $initialDeadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        $initialRuntime = Get-ProofRuntime
        $initialWorker = Get-WorkerExcluding $baselineWorkerIds
        $initialBytes = if (Test-Path -LiteralPath $firstCapture) { (Get-Item -LiteralPath $firstCapture).Length } else { 0 }
        if (-not $initialRuntime -or $initialRuntime.color -ne 'green' -or -not $initialWorker -or $initialBytes -lt 100000) {
            Start-Sleep -Milliseconds 200
        }
    } while ((-not $initialRuntime -or $initialRuntime.color -ne 'green' -or -not $initialWorker -or $initialBytes -lt 100000) -and [DateTime]::UtcNow -lt $initialDeadline)
    if (-not $initialRuntime -or $initialRuntime.color -ne 'green' -or -not $initialWorker -or $initialBytes -lt 100000) {
        throw 'The installed required SRT output did not reach initial green transport.'
    }
    $initialWorkerId = [uint32]$initialWorker.Id
    Start-Sleep -Seconds 11
    $confirmedRuntime = Get-ProofRuntime
    $confirmedWorker = Get-Process -Id $initialWorkerId -ErrorAction SilentlyContinue
    if (-not $confirmedRuntime -or $confirmedRuntime.color -ne 'green' -or
        $confirmedRuntime.heartbeat_age_seconds -gt 6 -or -not $confirmedWorker -or
        [uint32]$confirmedWorker.Id -ne $initialWorkerId) {
        throw 'Initial green state did not survive enough time for real output-progress heartbeats.'
    }

    $failureClock = [Diagnostics.Stopwatch]::StartNew()
    Stop-ProofListener $listener
    $listener = $null
    $failureDeadline = [DateTime]::UtcNow.AddSeconds(45)
    do {
        $failedRuntime = Get-ProofRuntime
        $alerts = Invoke-InstalledProofApi -Uri "$BaseUri/api/v1/alerts" -TimeoutSec 5
        $outputStall = $alerts | Where-Object {
            $_.resource_ref -eq $channelId -and $_.condition -eq 'output-stall' -and $_.state -eq 'firing'
        } | Select-Object -First 1
        $offAir = $alerts | Where-Object {
            $_.resource_ref -eq $channelId -and $_.condition -eq 'off-air' -and $_.state -eq 'firing'
        } | Select-Object -First 1
        $originalReaped = -not [bool](Get-Process -Id $initialWorkerId -ErrorAction SilentlyContinue)
        if (-not $failedRuntime -or $failedRuntime.color -ne 'red' -or -not $outputStall -or -not $offAir -or -not $originalReaped) {
            Start-Sleep -Milliseconds 100
        }
    } while ((-not $failedRuntime -or $failedRuntime.color -ne 'red' -or -not $outputStall -or -not $offAir -or -not $originalReaped) -and [DateTime]::UtcNow -lt $failureDeadline)
    $failureClock.Stop()
    if (-not $failedRuntime -or $failedRuntime.color -ne 'red') {
        throw 'Receiver loss did not fail the installed channel red within 45 seconds.'
    }
    if (-not $outputStall -or -not $offAir) {
        throw 'Receiver loss did not fire output-stall and off-air incidents.'
    }
    if (-not $originalReaped) {
        throw 'The failed channel worker was not reaped.'
    }
    $falseEncoderDeath = $alerts | Where-Object {
        $_.resource_ref -eq $channelId -and $_.condition -eq 'encoder-death' -and $_.state -eq 'firing'
    } | Select-Object -First 1
    if ($falseEncoderDeath) {
        throw 'A transport failure was incorrectly classified as encoder-death.'
    }

    $recoveryClock = [Diagnostics.Stopwatch]::StartNew()
    $listener = Start-ProofListener $recoveryCapture
    $recoveryDeadline = [DateTime]::UtcNow.AddSeconds(90)
    $excludedRecoveryIds = @($baselineWorkerIds) + @($initialWorkerId)
    do {
        $recoveredRuntime = Get-ProofRuntime
        $replacementWorker = Get-WorkerExcluding $excludedRecoveryIds
        $recoveryBytes = if (Test-Path -LiteralPath $recoveryCapture) { (Get-Item -LiteralPath $recoveryCapture).Length } else { 0 }
        if (-not $recoveredRuntime -or $recoveredRuntime.color -ne 'green' -or -not $replacementWorker -or
            [uint32]$replacementWorker.Id -eq $initialWorkerId -or $recoveryBytes -lt 100000) {
            Start-Sleep -Milliseconds 200
        }
    } while ((-not $recoveredRuntime -or $recoveredRuntime.color -ne 'green' -or -not $replacementWorker -or
        [uint32]$replacementWorker.Id -eq $initialWorkerId -or $recoveryBytes -lt 100000) -and
        [DateTime]::UtcNow -lt $recoveryDeadline)
    if (-not $recoveredRuntime -or $recoveredRuntime.color -ne 'green' -or -not $replacementWorker -or
        [uint32]$replacementWorker.Id -eq $initialWorkerId -or $recoveryBytes -lt 100000) {
        throw 'The installed channel did not recover through bounded restart within 90 seconds.'
    }
    $replacementWorkerId = [uint32]$replacementWorker.Id
    Start-Sleep -Seconds 11
    $recoveredRuntime = Get-ProofRuntime
    $replacementWorker = Get-Process -Id $replacementWorkerId -ErrorAction SilentlyContinue
    $alerts = Invoke-InstalledProofApi -Uri "$BaseUri/api/v1/alerts" -TimeoutSec 5
    $unresolved = $alerts | Where-Object {
        $_.resource_ref -eq $channelId -and $_.condition -in @('output-stall', 'off-air') -and $_.state -eq 'firing'
    }
    if (-not $recoveredRuntime -or $recoveredRuntime.color -ne 'green' -or
        $recoveredRuntime.heartbeat_age_seconds -gt 6 -or -not $replacementWorker -or
        [uint32]$replacementWorker.Id -ne $replacementWorkerId -or $unresolved) {
        throw 'Recovered output did not remain green through real progress heartbeats with incidents resolved.'
    }
    $recoveryClock.Stop()

    $result = [ordered]@{
        source_commit = $candidate.source_commit
        channel_id = $channelId
        sink_id = $sinkId
        initial_worker_pid = $initialWorkerId
        initial_capture_bytes = (Get-Item -LiteralPath $firstCapture).Length
        receiver_loss_detected_seconds = [math]::Round($failureClock.Elapsed.TotalSeconds, 3)
        channel_failed_red = $true
        output_stall_fired = $true
        off_air_fired = $true
        encoder_death_absent = $true
        original_worker_reaped = $true
        replacement_worker_pid = $replacementWorkerId
        recovery_seconds = [math]::Round($recoveryClock.Elapsed.TotalSeconds, 3)
        recovery_capture_bytes = (Get-Item -LiteralPath $recoveryCapture).Length
        output_stall_resolved = $true
        off_air_resolved = $true
        progress_heartbeat_survived = $true
        service_health = (Invoke-RestMethod -Uri "$BaseUri/health" -TimeoutSec 5).status
    }
    $json = $result | ConvertTo-Json
    if ($ResultPath) {
        [IO.File]::WriteAllText($ResultPath, $json, (New-Object Text.UTF8Encoding($false)))
    }
    $json
}
finally {
    if ($configured) {
        try {
            $channel.enabled = $false
            Invoke-InstalledProofApi -Method Put -Uri "$BaseUri/api/v1/channels" -ContentType 'application/json' `
                -Body ($channel | ConvertTo-Json -Depth 8 -Compress) -TimeoutSec 10 | Out-Null
        }
        catch {
            Write-Warning "Could not disable the output-stall proof channel: $($_.Exception.Message)"
        }
    }
    try { Stop-ProofListener $listener } catch { Write-Warning $_.Exception.Message }
    Remove-Item -LiteralPath $firstCapture, $recoveryCapture -Force -ErrorAction SilentlyContinue
}
