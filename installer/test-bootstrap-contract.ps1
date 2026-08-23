$ErrorActionPreference = 'Stop'

$installer = Join-Path $PSScriptRoot 'TownLightStation.iss'
$verifier = Join-Path $PSScriptRoot 'verify-contract.ps1'
$source = Get-Content -LiteralPath $installer -Raw
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("townlight-installer-contract-" + [guid]::NewGuid().ToString('N'))
$utf8NoBom = New-Object Text.UTF8Encoding($false)

function Test-StrictCapabilityBytes([byte[]]$Bytes) {
    if ($Bytes.Count -ne 44 -or $Bytes[43] -ne 10) {
        return $false
    }
    foreach ($byte in $Bytes[0..42]) {
        if (-not (
            ($byte -ge [byte][char]'A' -and $byte -le [byte][char]'Z') -or
            ($byte -ge [byte][char]'a' -and $byte -le [byte][char]'z') -or
            ($byte -ge [byte][char]'0' -and $byte -le [byte][char]'9') -or
            $byte -eq [byte][char]'_' -or
            $byte -eq [byte][char]'-'
        )) {
            return $false
        }
    }
    return $true
}

$valid = [Text.Encoding]::ASCII.GetBytes(('A' * 43) + "`n")
if (-not (Test-StrictCapabilityBytes $valid)) {
    throw 'Falsification harness rejected the exact token-plus-LF contract.'
}
$hostileVectors = @(
    [Text.Encoding]::ASCII.GetBytes('A' * 43),
    [Text.Encoding]::ASCII.GetBytes(('A' * 43) + "`r"),
    [Text.Encoding]::ASCII.GetBytes(('A' * 43) + "`r`n"),
    [Text.Encoding]::ASCII.GetBytes(('A' * 42) + '=' + "`n"),
    [Text.Encoding]::ASCII.GetBytes(('A' * 42) + '/' + "`n")
)
foreach ($hostile in $hostileVectors) {
    if (Test-StrictCapabilityBytes $hostile) {
        throw 'Falsification harness accepted a malformed bootstrap handoff.'
    }
}

$mutations = [ordered]@{
    'short token accepted' = @(
        'if Length(RawCapability) <> 44 then',
        'if Length(RawCapability) < 43 then'
    )
    'terminator unchecked' = @(
        'if RawCapability[44] <> #10 then',
        'if False then'
    )
    'alphabet unchecked' = @(
        'IsBase64UrlCharacter(RawCapability[Index])',
        'True'
    )
    'capability logged' = @(
        'Capability := Copy(RawCapability, 1, 43);',
        "Capability := Copy(RawCapability, 1, 43);`r`n    Log(Capability);"
    )
    'secret appended to stationd argv' = @(
        "OldPath := GetEnv('PATH');",
        "Parameters := Parameters + Capability;`r`n  OldPath := GetEnv('PATH');"
    )
    'crash remnant bypassed' = @(
        '  RejectCrashRemnantHandoff;',
        '  { crash remnant check removed }'
    )
    'preserved database deleted' = @(
        '  RollbackService;',
        "  DeleteFile(StationDatabasePath);`r`n  RollbackService;"
    )
}

New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
try {
    foreach ($mutation in $mutations.GetEnumerator()) {
        $old, $new = $mutation.Value
        if (-not $source.Contains($old)) {
            throw "Falsification fixture drifted for: $($mutation.Key)"
        }
        $mutated = $source.Replace($old, $new)
        $candidate = Join-Path $temporaryRoot (([guid]::NewGuid().ToString('N')) + '.iss')
        [IO.File]::WriteAllText($candidate, $mutated, $utf8NoBom)
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $verifier -Installer $candidate *> $null
        if ($LASTEXITCODE -eq 0) {
            throw "Installer verifier accepted hostile mutation: $($mutation.Key)"
        }
    }
}
finally {
    Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output "Installer bootstrap falsification passed ($($hostileVectors.Count) malformed handoffs and $($mutations.Count) hostile source mutations rejected)."
