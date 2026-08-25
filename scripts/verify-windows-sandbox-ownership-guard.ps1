param()

$ErrorActionPreference = 'Stop'
$launcherPath = Join-Path $PSScriptRoot 'invoke-owned-windows-sandbox.ps1'
$assertionCount = 0

function Assert-True {
    param([bool]$Condition, [string]$Message)
    $script:assertionCount++
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
}

Assert-True (Test-Path -LiteralPath $launcherPath -PathType Leaf) `
    'the owned Windows Sandbox launcher must exist'

$source = Get-Content -LiteralPath $launcherPath -Raw
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $launcherPath,
    [ref]$tokens,
    [ref]$parseErrors
)
Assert-True ($parseErrors.Count -eq 0) 'the launcher must parse as PowerShell'
Assert-True ($source -match 'Global\\Scott\.SharedWindowsSandbox\.Launch\.v1') `
    'the launcher must serialize the shared pre-launch decision with a global mutex'
Assert-True ($source -match '\.WaitOne\(0\)') `
    'the launcher must fail immediately when another launch decision is in progress'
Assert-True ($source -match '\bGet-CimInstance\s+Win32_Process\b') `
    'the launcher must inspect current host process ownership before launch'
foreach ($processName in @('WindowsSandbox.exe', 'WindowsSandboxClient.exe', 'WindowsSandboxRemoteSession.exe')) {
    Assert-True ($source -match [regex]::Escape($processName)) `
        "the launcher must treat $processName as occupied Windows Sandbox state"
}
Assert-True ($source -match '\bStart-Process\b' -and $source -match '\bPassThru\b') `
    'the launcher must retain the exact process object it launched'
Assert-True ($source -match 'FileMode\]::CreateNew') `
    'the launcher must create a new immutable ownership record instead of overwriting a file'
Assert-True ($source -match '\[Collections\.IDictionary\]\$Record') `
    'the immutable writer must accept ordered ownership evidence'
Assert-True ($source -match '\bGet-FileHash\b') `
    'the ownership record must bind the exact configuration bytes'
Assert-True ($source -notmatch '(?i)\bStop-Process\b|\btaskkill(?:\.exe)?\b|\.Kill\s*\(') `
    'the launcher must not terminate any sandbox process'
Assert-True ($source -notmatch '(?i)\bRemove-Item\b|\bMove-Item\b|\bRename-Item\b') `
    'the launcher must not mutate or clean up another harness owner file'

$check = & $launcherPath -CheckOnly
Assert-True ($check.status -eq 'available') 'the current machine must report available when no sandbox is active'
Assert-True ($check.launch_allowed) 'the current machine check must explicitly allow launch'
Assert-True ($check.sandbox_process_count -eq 0) 'the available result must contain no sandbox processes'

$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) "townlight-sandbox-guard-$([guid]::NewGuid().ToString('N'))"
$fakeSandboxPath = Join-Path $temporaryRoot 'WindowsSandbox.exe'
[void][IO.Directory]::CreateDirectory($temporaryRoot)
Copy-Item -LiteralPath (Join-Path $env:WINDIR 'System32\ping.exe') -Destination $fakeSandboxPath
$fakeSandbox = Start-Process -FilePath $fakeSandboxPath `
    -ArgumentList @('127.0.0.1', '-n', '4') -WindowStyle Hidden -PassThru
try {
    $occupiedCaught = $false
    try {
        & $launcherPath -CheckOnly | Out-Null
    } catch {
        $occupiedCaught = $_.Exception.Message -match 'occupied by an unclaimed owner'
    }
    Assert-True $occupiedCaught `
        'an unclaimed WindowsSandbox.exe process must make the pre-launch guard fail closed'
} finally {
    if (-not $fakeSandbox.WaitForExit(10000)) {
        throw 'The verifier-owned sandbox-name simulation did not exit on its own.'
    }
    $fakeSandbox.Dispose()
}

$foreignConfiguration = Join-Path $temporaryRoot 'claude-owned-or-unclaimed.wsb'
[IO.File]::WriteAllText($foreignConfiguration, '<Configuration />', [Text.UTF8Encoding]::new($false))
$foreignPathCaught = $false
try {
    & $launcherPath -ConfigurationPath $foreignConfiguration | Out-Null
} catch {
    $foreignPathCaught = $_.Exception.Message -match 'must be a TownLight-owned file'
}
Assert-True $foreignPathCaught `
    'the launcher must reject a configuration outside the TownLight-owned configuration root'

$mutexMarker = Join-Path $temporaryRoot 'mutex-acquired.txt'
$mutexHelper = Join-Path $temporaryRoot 'hold-mutex.ps1'
$helperSource = @'
$mutex = [Threading.Mutex]::new($false, 'Global\Scott.SharedWindowsSandbox.Launch.v1')
try {
    [void]$mutex.WaitOne()
    [IO.File]::WriteAllText($args[0], 'held')
    Start-Sleep -Seconds 4
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
'@
[IO.File]::WriteAllText($mutexHelper, $helperSource, [Text.UTF8Encoding]::new($false))
$pwshPath = (Get-Command pwsh.exe -ErrorAction Stop).Source
$mutexHolder = Start-Process -FilePath $pwshPath `
    -ArgumentList @('-NoProfile', '-NonInteractive', '-File', $mutexHelper, $mutexMarker) `
    -WindowStyle Hidden -PassThru
try {
    $markerDeadline = [datetime]::UtcNow.AddSeconds(2)
    while (-not (Test-Path -LiteralPath $mutexMarker -PathType Leaf) -and
        [datetime]::UtcNow -lt $markerDeadline) {
        Start-Sleep -Milliseconds 20
    }
    Assert-True (Test-Path -LiteralPath $mutexMarker -PathType Leaf) `
        'the verifier-owned helper must acquire the shared mutex before contention is tested'
    $contentionCaught = $false
    try {
        & $launcherPath -CheckOnly | Out-Null
    } catch {
        $contentionCaught = $_.Exception.Message -match 'another launch decision is in progress'
    }
    Assert-True $contentionCaught `
        'a concurrent shared launch decision must make the guard fail closed'
} finally {
    if (-not $mutexHolder.WaitForExit(10000)) {
        throw 'The verifier-owned mutex helper did not exit on its own.'
    }
    $mutexHolder.Dispose()
}

$resolvedTemporaryRoot = [IO.Path]::GetFullPath($temporaryRoot)
if ($resolvedTemporaryRoot.StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()))) {
    Remove-Item -LiteralPath $resolvedTemporaryRoot -Recurse -Force
}

Write-Output "Windows Sandbox ownership guard contract passed ($assertionCount assertions)."
