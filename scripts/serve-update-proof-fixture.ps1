param(
    [Parameter(Mandatory = $true)]
    [int]$Port,
    [Parameter(Mandatory = $true)]
    [string]$FeedPath,
    [Parameter(Mandatory = $true)]
    [string]$ReadyPath,
    [Parameter(Mandatory = $true)]
    [string]$StopPath,
    [Parameter(Mandatory = $true)]
    [string]$LogPath
)

$ErrorActionPreference = 'Stop'
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
try {
    [byte[]]$body = [IO.File]::ReadAllBytes($FeedPath)
    $listener.Start()
    Set-Content -LiteralPath $ReadyPath -Value "127.0.0.1:$Port" -Encoding ascii
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    while (-not (Test-Path -LiteralPath $StopPath) -and [DateTime]::UtcNow -lt $deadline) {
        if (-not $listener.Pending()) {
            Start-Sleep -Milliseconds 25
            continue
        }
        $client = $listener.AcceptTcpClient()
        try {
            $stream = $client.GetStream()
            $stream.ReadTimeout = 5000
            $request = New-Object byte[] 4096
            $count = $stream.Read($request, 0, $request.Length)
            $requestLine = [Text.Encoding]::ASCII.GetString($request, 0, $count).Split("`r`n")[0]
            Add-Content -LiteralPath $LogPath -Value $requestLine -Encoding ascii
            if ($requestLine -eq 'GET /stable.json HTTP/1.1') {
                $headers = [Text.Encoding]::ASCII.GetBytes(
                    "HTTP/1.1 200 OK`r`nContent-Type: application/json`r`nContent-Length: $($body.Length)`r`nConnection: close`r`n`r`n"
                )
                $stream.Write($headers, 0, $headers.Length)
                $stream.Write($body, 0, $body.Length)
            }
            else {
                $response = [Text.Encoding]::ASCII.GetBytes(
                    "HTTP/1.1 404 Not Found`r`nContent-Length: 0`r`nConnection: close`r`n`r`n"
                )
                $stream.Write($response, 0, $response.Length)
            }
        }
        finally { $client.Dispose() }
    }
}
catch {
    Set-Content -LiteralPath $LogPath -Value $_.Exception.Message -Encoding utf8NoBOM
    throw
}
finally {
    $listener.Stop()
}
