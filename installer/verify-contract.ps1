$ErrorActionPreference = 'Stop'

$installer = Join-Path $PSScriptRoot 'TownLightStation.iss'
$source = Get-Content -LiteralPath $installer -Raw
$requirements = [ordered]@{
    'requires administrative installation' = 'PrivilegesRequired=admin'
    'targets supported 64-bit Windows' = 'ArchitecturesAllowed=x64compatible'
    'carries the official media runtime' = 'ExtractTemporaryFile'
    'checks the required AAC encoder' = 'gstvoaacenc.dll'
    'checks the required audio rate adjuster' = 'gstaudiorate.dll'
    'checks the required video rate adjuster' = 'gstvideorate.dll'
    'checks the required loudness analyzer' = 'gstrsaudiofx.dll'
    'checks the required file playback plugin' = 'gstplayback.dll'
    'checks the required transport stream demuxer' = 'gstmpegtsdemux.dll'
    'checks the required software decoder bundle' = 'gstlibav.dll'
    'checks the required SRT plugin' = 'gstsrt.dll'
    'checks the required SRT transport library' = 'srt.dll'
    'reuses an already-complete private runtime during upgrade' = 'if PrivateRuntimeIsComplete(RuntimeRoot) then'
    'removes an incomplete private runtime before replacement' = "if FileExists(RuntimeRoot + '\unins000.exe') then"
    'registers the native station service' = 'create TownLightStation'
    'sets immediate automatic service start' = 'config TownLightStation start= auto'
    'configures service recovery' = 'failure TownLightStation'
    'starts the station service' = 'start TownLightStation'
    'registers the independent updater service' = 'create '' + UpdaterServiceName'
    'configures updater recovery' = '''failure '' + UpdaterServiceName'
    'starts the updater service' = '''start '' + UpdaterServiceName'
    'health-gates installation success' = 'WinHttp.WinHttpRequest.5.1'
    'rolls back a failed service activation' = 'RollbackService'
    'removes the station service on uninstall' = 'StopAndDeleteServiceIfPresent(StationServiceName)'
    'removes the updater service on uninstall' = 'StopAndDeleteServiceIfPresent(UpdaterServiceName)'
    'records the immutable source commit' = 'source_commit'
    'records the active application slot' = 'active-slot.json'
    'preserves station data on uninstall' = 'Station data is deliberately preserved'
}

$missing = @()
foreach ($requirement in $requirements.GetEnumerator()) {
    if (-not $source.Contains($requirement.Value)) {
        $missing += $requirement.Key
    }
}

if ($missing.Count -gt 0) {
    throw "Installer contract is incomplete: $($missing -join '; ')"
}

if ($source.Contains('start= delayed-auto')) {
    throw 'Installer contract defers station recovery with delayed automatic startup.'
}

Write-Output "Installer contract passed ($($requirements.Count) requirements)."
