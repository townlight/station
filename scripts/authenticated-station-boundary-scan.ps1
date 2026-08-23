function New-TownLightSecretPatterns {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Secrets
    )

    $patterns = [Collections.Generic.List[byte[]]]::new()
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($secret in $Secrets) {
        if ([string]::IsNullOrEmpty($secret) -or $secret.Length -gt 4096) {
            throw 'Authentication scan secrets must contain between 1 and 4096 characters.'
        }
        foreach ($bytes in @(
            [Text.Encoding]::UTF8.GetBytes($secret),
            [Text.Encoding]::Unicode.GetBytes($secret),
            [Text.Encoding]::BigEndianUnicode.GetBytes($secret)
        )) {
            $key = [Convert]::ToBase64String($bytes)
            if ($seen.Add($key)) { $patterns.Add($bytes) }
        }
    }
    if ($patterns.Count -eq 0) { throw 'Authentication scan requires at least one secret pattern.' }
    return ,$patterns.ToArray()
}

function Test-TownLightBytePattern {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][byte[]]$Pattern
    )

    if ($Pattern.Length -gt $Bytes.Length) { return $false }
    for ($offset = 0; $offset -le $Bytes.Length - $Pattern.Length; $offset++) {
        $matched = $true
        for ($index = 0; $index -lt $Pattern.Length; $index++) {
            if ($Bytes[$offset + $index] -ne $Pattern[$index]) {
                $matched = $false
                break
            }
        }
        if ($matched) { return $true }
    }
    return $false
}

function Test-TownLightSecretInStream {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][IO.Stream]$Stream,
        [Parameter(Mandatory = $true)][byte[][]]$Patterns,
        [Parameter(Mandatory = $true)][long]$MaxBytes,
        [Parameter(Mandatory = $true)][DateTime]$DeadlineUtc,
        [ValidateRange(16, 65536)][int]$ChunkSize = 65536
    )

    if (-not $Stream.CanRead -or $Patterns.Count -eq 0 -or $MaxBytes -lt 1) {
        throw 'Authentication artifact scan parameters are invalid.'
    }
    $longestPattern = ($Patterns | ForEach-Object Length | Measure-Object -Maximum).Maximum
    if ($longestPattern -lt 1 -or $longestPattern -gt 16384) {
        throw 'Authentication artifact scan patterns are invalid.'
    }

    $buffer = [byte[]]::new($ChunkSize)
    $carry = [byte[]]::new(0)
    [long]$totalRead = 0
    while ($true) {
        if ([DateTime]::UtcNow -gt $DeadlineUtc) {
            throw 'The authentication artifact scan exceeded its time bound.'
        }
        $read = $Stream.Read($buffer, 0, $buffer.Length)
        if ($read -eq 0) { return $false }
        $totalRead += $read
        if ($totalRead -gt $MaxBytes) {
            throw 'An authentication artifact exceeded its byte scan bound.'
        }

        $window = [byte[]]::new($carry.Length + $read)
        if ($carry.Length) { [Array]::Copy($carry, 0, $window, 0, $carry.Length) }
        [Array]::Copy($buffer, 0, $window, $carry.Length, $read)
        foreach ($pattern in $Patterns) {
            if (Test-TownLightBytePattern -Bytes $window -Pattern $pattern) { return $true }
        }

        $carryLength = [Math]::Min($longestPattern - 1, $window.Length)
        $carry = [byte[]]::new($carryLength)
        if ($carryLength) {
            [Array]::Copy($window, $window.Length - $carryLength, $carry, 0, $carryLength)
        }
    }
}
