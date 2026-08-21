param(
    [Parameter(Mandatory = $true)]
    [string]$ChannelId,
    [int]$Seconds = 8
)

$ErrorActionPreference = 'Stop'
$expectedExecutable = [IO.Path]::GetFullPath((Join-Path $env:ProgramFiles 'TownLight Station\channel-worker.exe'))
$deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
$killed = 0
while ([DateTime]::UtcNow -lt $deadline) {
    $workers = Get-CimInstance Win32_Process -Filter "Name='channel-worker.exe'" |
        Where-Object {
            $_.CommandLine -like "*$ChannelId*" -and
            $_.ExecutablePath -and
            [IO.Path]::GetFullPath($_.ExecutablePath) -eq $expectedExecutable
        }
    foreach ($worker in $workers) {
        Stop-Process -Id $worker.ProcessId -Force -ErrorAction SilentlyContinue
        $killed++
    }
    Start-Sleep -Milliseconds 25
}
[pscustomobject]@{ channel_id = $ChannelId; killed = $killed } | ConvertTo-Json
