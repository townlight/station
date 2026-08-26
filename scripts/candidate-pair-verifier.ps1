<##
.SYNOPSIS
    Validates one bounded TownLight candidate manifest and its exact sibling installer.

.DESCRIPTION
    This helper is intentionally side-effect free. It is shared by host proofs and
    guest harnesses so no consumer can treat a manifest as a publication marker
    without authenticating the exact installer bytes beside it.
##>

function Get-TownLightCandidateSafeLeaf {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    try { $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop }
    catch { throw "candidate_pair_${Label}_missing" }
    if (-not $item.PSIsContainer -and $item -isnot [IO.FileInfo]) {
        throw "candidate_pair_${Label}_not_a_file"
    }
    if ($item.PSIsContainer) { throw "candidate_pair_${Label}_not_a_file" }
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "candidate_pair_${Label}_reparse_point"
    }

    $parent = $item.Directory
    while ($null -ne $parent) {
        if (($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "candidate_pair_${Label}_parent_reparse_point"
        }
        $next = $parent.Parent
        if ($null -eq $next -or $next.FullName -ieq $parent.FullName) { break }
        $parent = $next
    }
    return $item
}

function Assert-TownLightCandidateString {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Pattern,
        [Parameter(Mandatory = $true)][string]$Code
    )
    $property = $Manifest.PSObject.Properties[$Name]
    if ($null -eq $property -or $property.Value -isnot [string] -or
        -not [Text.RegularExpressions.Regex]::IsMatch([string]$property.Value, $Pattern, [Text.RegularExpressions.RegexOptions]::CultureInvariant)) {
        throw "candidate_pair_$Code"
    }
    return [string]$property.Value
}

function Test-TownLightCandidateCanonicalPattern {
    param([AllowNull()][string]$Value, [Parameter(Mandatory = $true)][string]$Pattern)
    return [Text.RegularExpressions.Regex]::IsMatch($Value, $Pattern, [Text.RegularExpressions.RegexOptions]::CultureInvariant)
}

function Assert-TownLightCandidateJsonShape {
    param([Parameter(Mandatory = $true)][string]$Json)
    if ($null -eq ('TownLightCandidateJsonGuard' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Text;

public static class TownLightCandidateJsonGuard {
    public static void ValidateTopLevel(string json, string[] allowedNames) {
        if (json == null || allowedNames == null || json.Length > 2000000) throw new InvalidOperationException("null_or_bounded_json");
        new Parser(json, allowedNames).Run();
    }

    private sealed class Parser {
        private readonly string text;
        private readonly HashSet<string> allowed;
        private readonly HashSet<string> seen = new HashSet<string>(StringComparer.Ordinal);
        private int position;

        public Parser(string value, string[] names) {
            text = value;
            allowed = new HashSet<string>(names, StringComparer.Ordinal);
        }

        public void Run() {
            SkipWhitespace();
            Expect('{');
            SkipWhitespace();
            if (Consume('}')) Fail("missing_top_level_key");
            while (true) {
                string name = ReadString();
                if (!allowed.Contains(name)) Fail("unknown_top_level_key");
                if (!seen.Add(name)) Fail("duplicate_top_level_key");
                SkipWhitespace();
                Expect(':');
                SkipWhitespace();
                SkipValue(0);
                SkipWhitespace();
                if (Consume('}')) break;
                Expect(',');
                SkipWhitespace();
                if (Peek('}')) Fail("trailing_comma");
            }
            SkipWhitespace();
            if (position != text.Length) Fail("trailing_json");
            if (seen.Count != allowed.Count) Fail("missing_top_level_key");
        }

        private void SkipValue(int depth) {
            if (depth > 16) Fail("max_depth");
            char current = Current();
            if (current == '"') { ReadString(); return; }
            if (current == '{') { SkipObject(depth + 1); return; }
            if (current == '[') { SkipArray(depth + 1); return; }
            if (current == 't') { ReadLiteral("true"); return; }
            if (current == 'f') { ReadLiteral("false"); return; }
            if (current == 'n') { ReadLiteral("null"); return; }
            if (current == '-' || (current >= '0' && current <= '9')) { ReadNumber(); return; }
            Fail("invalid_value");
        }

        private void SkipObject(int depth) {
            Expect('{');
            SkipWhitespace();
            if (Consume('}')) return;
            var objectSeen = new HashSet<string>(StringComparer.Ordinal);
            while (true) {
                string name = ReadString();
                if (!objectSeen.Add(name)) Fail("duplicate_object_key");
                SkipWhitespace();
                Expect(':');
                SkipWhitespace();
                SkipValue(depth);
                SkipWhitespace();
                if (Consume('}')) return;
                Expect(',');
                SkipWhitespace();
                if (Peek('}')) Fail("trailing_comma");
            }
        }

        private void SkipArray(int depth) {
            Expect('[');
            SkipWhitespace();
            if (Consume(']')) return;
            while (true) {
                SkipValue(depth);
                SkipWhitespace();
                if (Consume(']')) return;
                Expect(',');
                SkipWhitespace();
                if (Peek(']')) Fail("trailing_comma");
            }
        }

        private string ReadString() {
            Expect('"');
            var value = new StringBuilder();
            while (position < text.Length) {
                char current = text[position++];
                if (current == '"') return value.ToString();
                if (current < 0x20) Fail("control_in_string");
                if (current != '\\') { value.Append(current); continue; }
                if (position >= text.Length) Fail("unterminated_escape");
                char escaped = text[position++];
                switch (escaped) {
                    case '"': value.Append('"'); break;
                    case '\\': value.Append('\\'); break;
                    case '/': value.Append('/'); break;
                    case 'b': value.Append('\b'); break;
                    case 'f': value.Append('\f'); break;
                    case 'n': value.Append('\n'); break;
                    case 'r': value.Append('\r'); break;
                    case 't': value.Append('\t'); break;
                    case 'u': value.Append(ReadUnicodeEscape()); break;
                    default: Fail("invalid_escape"); break;
                }
            }
            Fail("unterminated_string");
            return null;
        }

        private char ReadUnicodeEscape() {
            if (position + 4 > text.Length) Fail("short_unicode_escape");
            int result = 0;
            for (int i = 0; i < 4; i++) {
                int digit = Hex(text[position++]);
                if (digit < 0) Fail("invalid_unicode_escape");
                result = (result * 16) + digit;
            }
            return (char)result;
        }

        private void ReadLiteral(string literal) {
            if (position + literal.Length > text.Length || !text.Substring(position, literal.Length).Equals(literal, StringComparison.Ordinal)) Fail("invalid_literal");
            position += literal.Length;
        }

        private void ReadNumber() {
            int start = position;
            Consume('-');
            if (Consume('0')) { }
            else {
                RequireDigit(false);
                while (IsDigit(Current())) position++;
            }
            if (Consume('.')) {
                RequireDigit(true);
                while (IsDigit(Current())) position++;
            }
            if (Consume('e') || Consume('E')) {
                Consume('+');
                Consume('-');
                RequireDigit(true);
                while (IsDigit(Current())) position++;
            }
            if (position == start) Fail("invalid_number");
        }

        private void RequireDigit(bool allowZero) {
            if (!IsDigit(Current())) Fail("invalid_number");
            if (!allowZero && Current() == '0') Fail("leading_zero");
            position++;
        }

        private static bool IsDigit(char value) { return value >= '0' && value <= '9'; }
        private static int Hex(char value) {
            if (value >= '0' && value <= '9') return value - '0';
            if (value >= 'a' && value <= 'f') return value - 'a' + 10;
            if (value >= 'A' && value <= 'F') return value - 'A' + 10;
            return -1;
        }

        private char Current() { return position < text.Length ? text[position] : '\0'; }
        private bool Peek(char expected) { return Current() == expected; }
        private bool Consume(char expected) { if (!Peek(expected)) return false; position++; return true; }
        private void Expect(char expected) { if (!Consume(expected)) Fail("expected_token"); }
        private void SkipWhitespace() { while (Current() == ' ' || Current() == '\t' || Current() == '\r' || Current() == '\n') position++; }
        private static void Fail(string code) { throw new InvalidOperationException(code); }
    }
}
'@
    }
    $allowedNames = @(
        'product', 'version', 'source_commit', 'installer', 'installer_sha256',
        'stationd_sha256', 'channel_worker_sha256', 'station_updater_sha256',
        'media_runtime_sha256', 'caption_decoder_archive_sha256',
        'caption_decoder_sha256', 'caption_decoder_size_bytes',
        'caption_decoder_license_sha256', 'caption_decoder_license_size_bytes',
        'build_toolchain'
    )
    try { [TownLightCandidateJsonGuard]::ValidateTopLevel($Json, $allowedNames) }
    catch { throw 'candidate_pair_manifest_shape_invalid' }
}

function Assert-TownLightCandidateObjectShape {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string[]]$Names,
        [Parameter(Mandatory = $true)][string]$Code
    )
    if ($null -eq $Value -or $Value -is [array] -or $Value -isnot [pscustomobject]) {
        throw "candidate_pair_${Code}"
    }
    $actualNames = @($Value.PSObject.Properties | ForEach-Object { [string]$_.Name })
    if ($actualNames.Count -ne $Names.Count) { throw "candidate_pair_${Code}" }
    foreach ($actualName in $actualNames) {
        if ($Names -cnotcontains $actualName) { throw "candidate_pair_${Code}" }
    }
    foreach ($name in $Names) {
        if ($actualNames -cnotcontains $name) { throw "candidate_pair_${Code}" }
    }
}

function Assert-TownLightCandidateTextProperty {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Code
    )
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $property.Value -isnot [string] -or [string]::IsNullOrWhiteSpace([string]$property.Value)) {
        throw "candidate_pair_${Code}"
    }
}

function Assert-TownLightCandidateSizeProperty {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Code
    )
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) {
        throw "candidate_pair_${Code}"
    }
    $valueType = $property.Value.GetType()
    if (($valueType -ne [int32] -and $valueType -ne [int64]) -or [int64]$property.Value -lt 0) {
        throw "candidate_pair_${Code}"
    }
}

function Assert-TownLightCandidateFileIdentity {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string]$Code,
        [switch]$RequireValidSignature
    )
    Assert-TownLightCandidateObjectShape -Value $Value -Names @(
        'path', 'size_bytes', 'sha256', 'file_version', 'product_version',
        'signer_status', 'signer_thumbprint', 'signer_subject'
    ) -Code $Code
    foreach ($name in @('path', 'signer_status')) {
        Assert-TownLightCandidateTextProperty -Object $Value -Name $name -Code "${Code}_${name}"
    }
    foreach ($name in @('file_version', 'product_version', 'signer_thumbprint', 'signer_subject')) {
        $property = $Value.PSObject.Properties[$name]
        if ($null -eq $property -or $property.Value -isnot [string]) { throw "candidate_pair_${Code}_${name}" }
    }
    Assert-TownLightCandidateSizeProperty -Object $Value -Name 'size_bytes' -Code "${Code}_size"
    $hash = [string]$Value.PSObject.Properties['sha256'].Value
    if ($Value.PSObject.Properties['sha256'].Value -isnot [string] -or
        -not (Test-TownLightCandidateCanonicalPattern -Value $hash -Pattern '^[0-9a-f]{64}$')) {
        throw "candidate_pair_${Code}_sha256"
    }
    $status = [string]$Value.PSObject.Properties['signer_status'].Value
    $thumbprint = [string]$Value.PSObject.Properties['signer_thumbprint'].Value
    $subject = [string]$Value.PSObject.Properties['signer_subject'].Value
    if ([string]::Equals($status, 'Valid', [StringComparison]::Ordinal) -and
        ([string]::IsNullOrWhiteSpace($thumbprint) -or [string]::IsNullOrWhiteSpace($subject))) {
        throw "candidate_pair_${Code}_signature_incoherent"
    }
    if ([string]::Equals($status, 'NotSigned', [StringComparison]::Ordinal) -and
        (-not [string]::IsNullOrWhiteSpace($thumbprint) -or -not [string]::IsNullOrWhiteSpace($subject))) {
        throw "candidate_pair_${Code}_signature_incoherent"
    }
    if ($RequireValidSignature -and (-not [string]::Equals($status, 'Valid', [StringComparison]::Ordinal) -or
        [string]::IsNullOrWhiteSpace($thumbprint) -or [string]::IsNullOrWhiteSpace($subject))) {
        throw "candidate_pair_${Code}_signature_required"
    }
}

function Assert-TownLightCandidateBuildToolchain {
    param([Parameter(Mandatory = $true)]$Value)
    Assert-TownLightCandidateObjectShape -Value $Value -Names @('inno', 'rustc', 'cargo', 'msvc', 'gstreamer_compile') -Code 'build_toolchain_shape_invalid'

    $inno = $Value.PSObject.Properties['inno'].Value
    Assert-TownLightCandidateObjectShape -Value $inno -Names @('path', 'size_bytes', 'sha256', 'signer_thumbprint', 'signer_subject') -Code 'build_toolchain_inno_invalid'
    foreach ($name in @('path', 'signer_thumbprint', 'signer_subject')) { Assert-TownLightCandidateTextProperty -Object $inno -Name $name -Code "build_toolchain_inno_${name}" }
    Assert-TownLightCandidateSizeProperty -Object $inno -Name 'size_bytes' -Code 'build_toolchain_inno_size'
    if ($inno.PSObject.Properties['sha256'].Value -isnot [string] -or
        -not (Test-TownLightCandidateCanonicalPattern -Value ([string]$inno.sha256) -Pattern '^[0-9a-f]{64}$')) { throw 'candidate_pair_build_toolchain_inno_sha256' }

    foreach ($name in @('rustc', 'cargo')) {
        $rust = $Value.PSObject.Properties[$name].Value
        Assert-TownLightCandidateObjectShape -Value $rust -Names @('path', 'version', 'commit', 'host', 'executable') -Code "build_toolchain_${name}_invalid"
        foreach ($field in @('path', 'version', 'host')) { Assert-TownLightCandidateTextProperty -Object $rust -Name $field -Code "build_toolchain_${name}_${field}" }
        $commit = $rust.PSObject.Properties['commit'].Value
        if ($commit -isnot [string] -or -not (Test-TownLightCandidateCanonicalPattern -Value ([string]$commit) -Pattern '^[0-9a-f]{40}$')) { throw "candidate_pair_build_toolchain_${name}_commit" }
        Assert-TownLightCandidateFileIdentity -Value $rust.PSObject.Properties['executable'].Value -Code "build_toolchain_${name}_executable"
    }

    $msvc = $Value.PSObject.Properties['msvc'].Value
    Assert-TownLightCandidateObjectShape -Value $msvc -Names @('root', 'version', 'cl', 'link', 'sdk_root', 'sdk_version', 'include_root', 'lib_root', 'sdk_um_lib', 'sdk_ucrt_lib', 'rc') -Code 'build_toolchain_msvc_invalid'
    foreach ($field in @('root', 'version', 'sdk_root', 'sdk_version', 'include_root', 'lib_root', 'sdk_um_lib', 'sdk_ucrt_lib')) { Assert-TownLightCandidateTextProperty -Object $msvc -Name $field -Code "build_toolchain_msvc_${field}" }
    foreach ($field in @('cl', 'link', 'rc')) { Assert-TownLightCandidateFileIdentity -Value $msvc.PSObject.Properties[$field].Value -Code "build_toolchain_msvc_${field}" -RequireValidSignature }

    $gstreamer = $Value.PSObject.Properties['gstreamer_compile'].Value
    Assert-TownLightCandidateObjectShape -Value $gstreamer -Names @('root', 'version', 'loader', 'import_library', 'header') -Code 'build_toolchain_gstreamer_invalid'
    foreach ($field in @('root', 'version')) { Assert-TownLightCandidateTextProperty -Object $gstreamer -Name $field -Code "build_toolchain_gstreamer_${field}" }
    foreach ($field in @('loader', 'import_library', 'header')) { Assert-TownLightCandidateFileIdentity -Value $gstreamer.PSObject.Properties[$field].Value -Code "build_toolchain_gstreamer_${field}" }
}

function Get-TownLightCandidateIdentity {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$ManifestPath,
        [string]$ExpectedSourceCommit = ''
    )

    $manifestItem = Get-TownLightCandidateSafeLeaf -Path $ManifestPath -Label 'manifest'
    if (-not [string]::Equals([IO.Path]::GetFileName($manifestItem.Name), 'candidate-manifest.json', [StringComparison]::Ordinal)) {
        throw 'candidate_pair_manifest_filename_invalid'
    }
    try { $json = Get-Content -LiteralPath $manifestItem.FullName -Raw -ErrorAction Stop }
    catch { throw 'candidate_pair_manifest_json_invalid' }
    Assert-TownLightCandidateJsonShape -Json $json
    try { $manifest = $json | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'candidate_pair_manifest_json_invalid' }
    if ($null -eq $manifest -or $manifest -is [array] -or $manifest -isnot [pscustomobject]) {
        throw 'candidate_pair_manifest_shape_invalid'
    }

    $product = Assert-TownLightCandidateString -Manifest $manifest -Name 'product' -Pattern '^TownLight Station$' -Code 'product_invalid'
    $version = Assert-TownLightCandidateString -Manifest $manifest -Name 'version' -Pattern '^0\.1\.0$' -Code 'version_invalid'
    $sourceCommit = Assert-TownLightCandidateString -Manifest $manifest -Name 'source_commit' -Pattern '^[0-9a-f]{40}$' -Code 'source_commit_invalid'
    $installerName = Assert-TownLightCandidateString -Manifest $manifest -Name 'installer' -Pattern '^TownLight-Station-0\.1\.0-x64-setup\.exe$' -Code 'installer_name_invalid'
    $installerHash = Assert-TownLightCandidateString -Manifest $manifest -Name 'installer_sha256' -Pattern '^[0-9a-f]{64}$' -Code 'installer_hash_invalid'

    foreach ($name in @(
        'stationd_sha256', 'channel_worker_sha256', 'station_updater_sha256',
        'media_runtime_sha256', 'caption_decoder_archive_sha256',
        'caption_decoder_sha256', 'caption_decoder_license_sha256'
    )) {
        Assert-TownLightCandidateString -Manifest $manifest -Name $name -Pattern '^[0-9a-f]{64}$' -Code "${name}_invalid" | Out-Null
    }
    foreach ($name in @('caption_decoder_size_bytes', 'caption_decoder_license_size_bytes')) {
        Assert-TownLightCandidateSizeProperty -Object $manifest -Name $name -Code "${name}_invalid"
    }
    $toolchain = $manifest.PSObject.Properties['build_toolchain']
    if ($null -eq $toolchain -or $toolchain.Value -is [array] -or $toolchain.Value -isnot [pscustomobject]) {
        throw 'candidate_pair_build_toolchain_invalid'
    }
    Assert-TownLightCandidateBuildToolchain -Value $toolchain.Value
    if ($ExpectedSourceCommit -and (-not (Test-TownLightCandidateCanonicalPattern -Value $ExpectedSourceCommit -Pattern '^[0-9a-f]{40}$') -or
        -not [string]::Equals($ExpectedSourceCommit, $sourceCommit, [StringComparison]::Ordinal))) {
        throw 'candidate_pair_source_commit_mismatch'
    }

    $installerPath = Join-Path $manifestItem.Directory.FullName $installerName
    $installerItem = Get-TownLightCandidateSafeLeaf -Path $installerPath -Label 'installer'
    if (-not [string]::Equals($installerItem.Directory.FullName, $manifestItem.Directory.FullName, [StringComparison]::OrdinalIgnoreCase) -or
        -not [string]::Equals([IO.Path]::GetFileName($installerItem.Name), 'TownLight-Station-0.1.0-x64-setup.exe', [StringComparison]::Ordinal)) {
        throw 'candidate_pair_installer_sibling_invalid'
    }
    $observedHash = (Get-FileHash -LiteralPath $installerItem.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if (-not [string]::Equals($observedHash, $installerHash, [StringComparison]::Ordinal)) { throw 'candidate_pair_installer_hash_mismatch' }

    [pscustomobject][ordered]@{
        manifest_path = $manifestItem.FullName
        installer_path = $installerItem.FullName
        product = $product
        version = $version
        source_commit = $sourceCommit
        installer = $installerName
        installer_sha256 = $observedHash
        manifest = $manifest
    }
}
