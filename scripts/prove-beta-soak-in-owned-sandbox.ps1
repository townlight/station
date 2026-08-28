[CmdletBinding()]
param([ValidateRange(4,8)][double]$DurationHours=4,[ValidateRange(600,7200)][int]$SetupTimeoutSeconds=3600)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'candidate-pair-verifier.ps1')
$repository=Split-Path $PSScriptRoot -Parent
$configuration=Join-Path $repository 'harness\windows-sandbox\configs\beta-soak-proof.wsb'
$guard=Join-Path $PSScriptRoot 'invoke-owned-windows-sandbox.ps1'
$manifestPath=Join-Path $repository 'dist\candidate-manifest.json'
$installerPath=Join-Path $repository 'dist\TownLight-Station-0.1.0-x64-setup.exe'
$inputRoot=Join-Path $env:ProgramData 'TownLight Station\SandboxInputs'
$fixturePath=Join-Path $inputRoot 'caption-source.ts'
$requestPath=Join-Path $inputRoot 'beta-soak-request.json'
$evidenceRoot=Join-Path $env:ProgramData 'TownLight Station\SandboxEvidence'
$fixtureSha256='4fb0c4cdc9a6b0ab7709b4ceb3b1ff4ce02c21e43d12cb84bc23bc1922de7c93'
$fixtureSize=[int64]33985512

function Assert-NoReparsePath([string]$Path,[bool]$LeafFile=$false){$full=[IO.Path]::GetFullPath($Path);$cursor=if($LeafFile){Get-Item -LiteralPath $full -Force -ErrorAction Stop}else{Get-Item -LiteralPath $full -Force -ErrorAction Stop};while($null-ne$cursor){if(($cursor.Attributes-band[IO.FileAttributes]::ReparsePoint)-ne 0){throw "Unsafe reparse path: $full"};$cursor=$cursor.Parent};$full}
function Open-PinnedFile([string]$Path,[int64]$MaximumBytes,[switch]$Json){$full=Assert-NoReparsePath $Path $true;$stream=[IO.File]::Open($full,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read);try{if($stream.Length-le0-or$stream.Length-gt$MaximumBytes){throw 'Pinned file is empty or unbounded.'};$sha=[Security.Cryptography.SHA256]::Create();try{$hash=([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()};$stream.Position=0;$document=$null;if($Json){$bytes=[byte[]]::new([int]$stream.Length);if($stream.Read($bytes,0,$bytes.Length)-ne$bytes.Length){throw 'Pinned JSON could not be read exactly.'};$stream.Position=0;$document=([Text.UTF8Encoding]::new($false,$true).GetString($bytes)|ConvertFrom-Json)};return [pscustomobject]@{path=$full;stream=$stream;size=[int64]$stream.Length;sha256=$hash;document=$document}}catch{$stream.Dispose();throw}}
function Copy-PinnedFile($Pin,[string]$Destination){$Pin.stream.Position=0;$output=[IO.File]::Open($Destination,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read);try{$Pin.stream.CopyTo($output);$output.Flush($true)}finally{$output.Dispose()};if((Get-FileHash $Destination -Algorithm SHA256).Hash.ToLowerInvariant()-cne$Pin.sha256){throw 'Immutable evidence copy hash mismatch.'}}
function New-OwnedDirectoryAcl{$acl=[Security.AccessControl.DirectorySecurity]::new();$acl.SetAccessRuleProtection($true,$false);foreach($sidText in @('S-1-5-18','S-1-5-32-544')){$sid=[Security.Principal.SecurityIdentifier]::new($sidText);$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,[Security.AccessControl.FileSystemRights]::FullControl,[Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit',[Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]::Allow))};$acl}
function Assert-OwnedDirectoryAcl([string]$Path){$full=Assert-NoReparsePath $Path;$acl=Get-Acl -LiteralPath $full;if(-not$acl.AreAccessRulesProtected){throw 'Harness-owned evidence ACL inherits authority.'};$rules=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]));if($rules.Count-ne2){throw 'Harness-owned evidence ACL has an unexpected rule count.'};foreach($sid in @('S-1-5-18','S-1-5-32-544')){$match=@($rules|Where-Object{$_.IdentityReference.Value-ceq$sid-and$_.AccessControlType-eq[Security.AccessControl.AccessControlType]::Allow-and$_.FileSystemRights-eq[Security.AccessControl.FileSystemRights]::FullControl-and$_.InheritanceFlags-eq([Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit')-and$_.PropagationFlags-eq[Security.AccessControl.PropagationFlags]::None-and-not$_.IsInherited});if($match.Count-ne1){throw 'Harness-owned evidence ACL is not exact SYSTEM/Admin full control.'}};$item=Get-Item -LiteralPath $full -Force;[pscustomobject]@{path=$full;creation_time_utc=$item.CreationTimeUtc.ToString('o')}}
function New-OwnedDirectory([IO.DirectoryInfo]$Info,[Security.AccessControl.DirectorySecurity]$Acl){$overload=@([IO.DirectoryInfo].GetMethods()|Where-Object{$_.Name-eq'Create'-and$_.GetParameters().Count-eq1-and$_.GetParameters()[0].ParameterType-eq[Security.AccessControl.DirectorySecurity]});if($overload.Count-eq1){[void]$overload[0].Invoke($Info,@($Acl));return};$extensions=[type]::GetType('System.IO.FileSystemAclExtensions, System.IO.FileSystem.AccessControl');if($null-eq$extensions){throw 'Atomic protected directory creation is unavailable.'};$method=@($extensions.GetMethods()|Where-Object{$_.Name-eq'Create'-and$_.GetParameters().Count-eq2-and$_.GetParameters()[0].ParameterType-eq[IO.DirectoryInfo]});if($method.Count-ne1){throw 'Atomic protected directory creation authority is ambiguous.'};[void]$method[0].Invoke($null,@($Info,$Acl))}
function New-ProtectedImmutableRoot([string]$RunId){$parent=Join-Path $env:ProgramData 'TownLight Station\SandboxAcceptedEvidence';$parentInfo=[IO.DirectoryInfo]::new($parent);if(-not$parentInfo.Exists){New-OwnedDirectory $parentInfo (New-OwnedDirectoryAcl)};$parentIdentity=Assert-OwnedDirectoryAcl $parent;$path=Join-Path $parent "beta-soak-$RunId";$leafInfo=[IO.DirectoryInfo]::new($path);if($leafInfo.Exists){throw 'Harness-owned immutable run directory already exists.'};New-OwnedDirectory $leafInfo (New-OwnedDirectoryAcl);$leafIdentity=Assert-OwnedDirectoryAcl $path;[pscustomobject]@{path=$path;parent_identity=$parentIdentity;leaf_identity=$leafIdentity}}
function Assert-OwnedDirectoryIdentity($Identity){$current=Assert-OwnedDirectoryAcl $Identity.path;if($current.creation_time_utc-cne$Identity.creation_time_utc){throw 'Harness-owned evidence directory identity changed.'}}
function Get-MappedPayloadSnapshot($Mappings,[string]$ExcludedRequest,[switch]$PinAll){
    $entries=[Collections.Generic.List[object]]::new();$ownedPins=[Collections.Generic.List[object]]::new();$total=[int64]0;$transferred=$false
    try {
        foreach($mapping in $Mappings.GetEnumerator()){
            $root=Assert-NoReparsePath ([IO.Path]::GetFullPath([string]$mapping.Value))
            foreach($file in @(Get-ChildItem -LiteralPath $root -File -Recurse -Force -ErrorAction Stop|Sort-Object FullName)){
                Assert-NoReparsePath $file.FullName $true|Out-Null;$relative=$file.FullName.Substring($root.Length).TrimStart('\').Replace('\','/')
                if($ExcludedRequest-and$file.FullName-ieq$ExcludedRequest){continue}
                $pin=Open-PinnedFile $file.FullName 2147483647
                if($PinAll){$ownedPins.Add($pin)}
                try{$total+=$pin.size;if($entries.Count-ge4096-or$total-gt8589934592){throw 'Mapped payload count or bytes are unbounded.'};$entries.Add([ordered]@{mapping=[string]$mapping.Key;relative_path=$relative;size_bytes=$pin.size;sha256=$pin.sha256})}finally{if(-not$PinAll){$pin.stream.Dispose()}}
            }
        }
        $bytes=[Text.UTF8Encoding]::new($false).GetBytes((@($entries)|ConvertTo-Json -Depth 5 -Compress));$sha=[Security.Cryptography.SHA256]::Create();try{$manifestSha=([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
        $result=[pscustomobject]@{entries=@($entries);manifest_sha256=$manifestSha;pins=$ownedPins};$transferred=$true;return $result
    } finally {if(-not$transferred){foreach($held in $ownedPins){try{$held.stream.Dispose()}catch{}}}}
}
function Test-SubstantiveCommissioningReceipt($Receipt,[string]$SourceCommit,$Candidate,[string]$ManifestSha,[string]$ReceiptSha){try{$e=$Receipt.evidence;$s=$e.schedule;$r=$e.restart_recovery;if([int]$Receipt.schema_version-ne1-or$Receipt.product-cne'TownLight Station'-or$Receipt.result_kind-cne'installed_commissioning'-or$Receipt.gate_status-cne'passed'-or[string]$Receipt.run_id-cnotmatch'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'-or[int64]$Receipt.finished_at_unix_ms-le[int64]$Receipt.started_at_unix_ms-or$Receipt.source_commit-cne$SourceCommit-or$Receipt.candidate_manifest_sha256-cne$ManifestSha-or$Receipt.installer_sha256-cne$Candidate.installer_sha256){return $false};if($e.installed_identity.source_commit-cne$SourceCommit-or$e.installed_identity.installer_sha256-cne$Candidate.installer_sha256-or$e.installed_identity.stationd_sha256-cne$Candidate.manifest.stationd_sha256-or$e.installed_identity.channel_worker_sha256-cne$Candidate.manifest.channel_worker_sha256-or$e.installed_identity.station_updater_sha256-cne$Candidate.manifest.station_updater_sha256-or$e.installed_identity.media_runtime_sha256-cne$Candidate.manifest.media_runtime_sha256){return $false};if($e.services.station.state-ne'Running'-or$e.services.station.start_name-ne'LocalSystem'-or[int]$e.services.station.process_id-le0-or$e.services.updater.state-ne'Running'-or$e.services.updater.start_name-ne'LocalSystem'-or[int]$e.services.updater.process_id-le0-or$e.health_before_restart.status-ne'ready'-or$e.health_before_restart.database-ne'ready'-or[string]::IsNullOrWhiteSpace([string]$e.station_profile_before_restart.station_id)){return $false};if($e.cg.clear_validated-ne$true-or[int64]$e.cg.red_capture_bytes-le0-or[int64]$e.cg.green_capture_bytes-le0-or$e.safe_to_air_before.color-ne'green'-or@($e.safe_to_air_before.channels|Where-Object{$_.channel_id-eq$s.draft.channel_id-and$_.state-in@('healthy_fallback','on_air')}).Count-ne1-or$e.media.ingest_job.status-ne'completed'-or[int]$e.media.ingest_job.progress_percent-ne100-or$e.media.readiness.readiness_state-ne'ready'){return $false};if($s.prepared.dry_run_passed-ne$true-or@($s.prepared.conflicts).Count-ne0-or$s.prepared.missing_media_detail-or$s.committed.report_id-cne$s.completed.report_id-or$s.committed.schedule_item_id-cne$s.draft.item_id-or$s.completed.schedule_item_id-cne$s.draft.item_id-or$s.completed.dispatch_status-cne'completed'-or((@($s.observed_statuses|Sort-Object -Unique)-join'|')-cne'acknowledged|completed|pending|queued')){return $false};if($r.status-eq'skipped'-or$r.health.status-ne'ready'-or$r.health.database-ne'ready'-or$r.station_profile.station_id-cne$e.station_profile_before_restart.station_id-or$r.safe_to_air.color-ne'green'-or@($r.safe_to_air.channels|Where-Object{$_.channel_id-eq$s.draft.channel_id-and$_.state-in@('healthy_fallback','on_air')}).Count-ne1-or$r.commit_report.report_id-cne$s.completed.report_id-or$r.commit_report.dispatch_status-cne'completed'){return $false};return $true}catch{return $false}}
foreach($required in @($configuration,$guard,$manifestPath,$installerPath,$fixturePath)){if(-not(Test-Path -LiteralPath $required -PathType Leaf)){throw "Owned beta-soak input is missing: $required"}}
foreach($root in @($inputRoot,$evidenceRoot,(Join-Path $repository 'dist'),(Join-Path $repository 'harness\windows-sandbox'),(Join-Path $repository 'scripts'))){if(Test-Path $root){Assert-NoReparsePath $root|Out-Null}}
$head=(& git -C $repository rev-parse HEAD).Trim();$dirty=@(& git -C $repository status --porcelain)
if($LASTEXITCODE -ne 0 -or $head -cnotmatch '^[0-9a-f]{40}$' -or $dirty.Count -ne 0){throw 'Owned beta-soak requires one clean immutable source commit.'}
$candidate=Get-TownLightCandidateIdentity -ManifestPath $manifestPath -ExpectedSourceCommit $head
$manifestSha=(Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
$fixture=Get-Item -LiteralPath $fixturePath -Force
if($fixture.Length -ne $fixtureSize -or (Get-FileHash $fixturePath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $fixtureSha256){throw 'Owned beta-soak caption fixture identity is invalid.'}
[xml]$wsb=Get-Content -LiteralPath $configuration -Raw
$mappings=@($wsb.Configuration.MappedFolders.MappedFolder);$writable=@($mappings|Where-Object{[string]$_.ReadOnly -ceq 'false'})
$expectedReadOnly=[ordered]@{'C:\TownLightCandidate'=(Join-Path $repository 'dist');'C:\TownLightVerifier'=(Join-Path $repository 'installer');'C:\TownLightInputs'=$inputRoot;'C:\TownLightHarness'=(Join-Path $repository 'harness\windows-sandbox');'C:\TownLightShared'=(Join-Path $repository 'scripts')}
if([string]$wsb.Configuration.Networking -cne 'Disable' -or $mappings.Count -ne 6 -or $writable.Count -ne 1 -or [string]$writable[0].SandboxFolder -cne 'C:\TownLightEvidence' -or [IO.Path]::GetFullPath([string]$writable[0].HostFolder) -ine [IO.Path]::GetFullPath($evidenceRoot)){throw 'Owned beta-soak WSB mappings are unsafe.'}
foreach($expected in $expectedReadOnly.GetEnumerator()){$match=@($mappings|Where-Object{[string]$_.SandboxFolder -ceq $expected.Key -and [string]$_.ReadOnly -ceq 'true' -and [IO.Path]::GetFullPath([string]$_.HostFolder) -ieq [IO.Path]::GetFullPath([string]$expected.Value)});if($match.Count -ne 1){throw "Owned beta-soak mapping is not exact: $($expected.Key)"}}

[void][IO.Directory]::CreateDirectory($inputRoot);[void][IO.Directory]::CreateDirectory($evidenceRoot)
Assert-NoReparsePath $inputRoot|Out-Null;Assert-NoReparsePath $evidenceRoot|Out-Null;if(Test-Path -LiteralPath $requestPath){Assert-NoReparsePath $requestPath $true|Out-Null}
$payloadBeforeRequest=Get-MappedPayloadSnapshot $expectedReadOnly ([IO.Path]::GetFullPath($requestPath))
$request=[ordered]@{schema_version=1;duration_hours=$DurationHours;source_commit=$head;candidate_manifest_sha256=$manifestSha;installer_sha256=$candidate.installer_sha256;payload_manifest_sha256=$payloadBeforeRequest.manifest_sha256}
$requestTemp="$requestPath.part-$([guid]::NewGuid().ToString('N'))"
[IO.File]::WriteAllText($requestTemp,(($request|ConvertTo-Json -Compress)+[Environment]::NewLine),[Text.UTF8Encoding]::new($false));Move-Item -LiteralPath $requestTemp -Destination $requestPath -Force
function Open-ValidatedLaunchPayloadAuthority {
$fullPayloadSnapshot=$null
try {
$fullPayloadSnapshot=Get-MappedPayloadSnapshot $expectedReadOnly '' -PinAll
$payloadAfterRequest=Get-MappedPayloadSnapshot $expectedReadOnly ([IO.Path]::GetFullPath($requestPath))
if($payloadAfterRequest.manifest_sha256-cne$payloadBeforeRequest.manifest_sha256){foreach($pin in $fullPayloadSnapshot.pins){$pin.stream.Dispose()};throw 'Mapped payload changed while the bound request was created.'}
$authorityPins=$fullPayloadSnapshot.pins
$pinsByPath=@{};foreach($pin in $authorityPins){$pinsByPath[$pin.path]=$pin}
if($pinsByPath[[IO.Path]::GetFullPath($manifestPath)].sha256-cne$manifestSha-or$pinsByPath[[IO.Path]::GetFullPath($installerPath)].sha256-cne$candidate.installer_sha256-or$pinsByPath[[IO.Path]::GetFullPath($fixturePath)].sha256-cne$fixtureSha256){throw 'Pinned candidate or fixture does not match the verified authority.'}
$configurationPin=$pinsByPath[[IO.Path]::GetFullPath($configuration)];$configurationBytes=[byte[]]::new([int]$configurationPin.size);[void]$configurationPin.stream.Read($configurationBytes,0,$configurationBytes.Length);$configurationPin.stream.Position=0;[xml]$pinnedWsb=[Text.UTF8Encoding]::new($false,$true).GetString($configurationBytes)
$pinnedMappings=@($pinnedWsb.Configuration.MappedFolders.MappedFolder);$pinnedWritable=@($pinnedMappings|Where-Object{[string]$_.ReadOnly -ceq 'false'})
if([string]$pinnedWsb.Configuration.Networking-cne'Disable'-or$pinnedMappings.Count-ne6-or$pinnedWritable.Count-ne1-or[string]$pinnedWritable[0].SandboxFolder-cne'C:\TownLightEvidence'-or[IO.Path]::GetFullPath([string]$pinnedWritable[0].HostFolder)-ine[IO.Path]::GetFullPath($evidenceRoot)){throw 'Pinned WSB authority differs from the validated configuration.'}
foreach($expected in $expectedReadOnly.GetEnumerator()){$match=@($pinnedMappings|Where-Object{[string]$_.SandboxFolder -ceq $expected.Key -and [string]$_.ReadOnly -ceq 'true' -and [IO.Path]::GetFullPath([string]$_.HostFolder) -ieq [IO.Path]::GetFullPath([string]$expected.Value)});if($match.Count-ne1){throw "Pinned WSB mapping differs from authority: $($expected.Key)"}}
$requestPin=$pinsByPath[[IO.Path]::GetFullPath($requestPath)];$requestBytes=[byte[]]::new([int]$requestPin.size);[void]$requestPin.stream.Read($requestBytes,0,$requestBytes.Length);$requestPin.stream.Position=0;$pinnedRequest=[Text.UTF8Encoding]::new($false,$true).GetString($requestBytes)|ConvertFrom-Json
if($pinnedRequest.source_commit-cne$head-or$pinnedRequest.candidate_manifest_sha256-cne$manifestSha-or$pinnedRequest.installer_sha256-cne$candidate.installer_sha256-or$pinnedRequest.payload_manifest_sha256-cne$payloadBeforeRequest.manifest_sha256-or[double]$pinnedRequest.duration_hours-ne$DurationHours){throw 'Pinned beta-soak request differs from host authority.'}
    [pscustomobject]@{snapshot=$fullPayloadSnapshot;pins=$authorityPins}
} catch {if($null-ne$fullPayloadSnapshot){foreach($pin in $fullPayloadSnapshot.pins){try{$pin.stream.Dispose()}catch{}}};throw}
}
$existing=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
Get-ChildItem -LiteralPath $evidenceRoot -Filter 'beta-soak-*-index.json' -File -ErrorAction SilentlyContinue|ForEach-Object{[void]$existing.Add($_.FullName)}
$existingFailures=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase);Get-ChildItem $evidenceRoot -Filter 'beta-soak-*-combined-failure.json' -File -ErrorAction SilentlyContinue|ForEach-Object{[void]$existingFailures.Add($_.FullName)}
$existingResults=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase);Get-ChildItem $evidenceRoot -Filter 'caption-proof-*.json' -File -ErrorAction SilentlyContinue|ForEach-Object{[void]$existingResults.Add($_.FullName)}

$waitDeadline=[DateTime]::UtcNow.AddMinutes(90)
$payloadAuthority=$null;$launch=$null
do{
    $check=& $guard -CheckOnly
    if($check.status -eq 'no_active_sandbox_detected' -and $check.launch_allowed -eq $true){
        $payloadAuthority=Open-ValidatedLaunchPayloadAuthority
        try{$launch=& $guard -ConfigurationPath $configuration}catch{foreach($pin in $payloadAuthority.pins){$pin.stream.Dispose()};$payloadAuthority=$null;throw}
        if($launch.status -eq 'launched' -and $launch.owner_namespace -eq 'townlight-station-codex'){break}
        foreach($pin in $payloadAuthority.pins){$pin.stream.Dispose()};$payloadAuthority=$null
        if($launch.status-ne'occupied'){throw 'Shared guard refused the atomic launch after payload acquisition.'}
        $check=$launch
    }
    if($check.status -ne 'occupied'){throw 'Shared Sandbox guard returned an unknown state; no launch was attempted.'}
    $remaining=($waitDeadline-[DateTime]::UtcNow).TotalSeconds
    if($remaining -le 0){throw 'Shared Sandbox remained occupied for 90 minutes; it was left untouched.'}
    Start-Sleep -Seconds ([int][Math]::Min(480,[Math]::Ceiling($remaining)))
}while($true)
$fullPayloadSnapshot=$payloadAuthority.snapshot;$authorityPins=$payloadAuthority.pins
$trackedPins=[Collections.Generic.List[object]]::new()
function Open-TrackedPinnedFile([string]$Path,[int64]$MaximumBytes,[switch]$Json){$pin=Open-PinnedFile $Path $MaximumBytes -Json:$Json;$trackedPins.Add($pin);$pin}
try {
foreach($pin in $authorityPins){$pin.stream.Position=0;$sha=[Security.Cryptography.SHA256]::Create();try{$observed=([BitConverter]::ToString($sha.ComputeHash($pin.stream))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()};if($observed-cne$pin.sha256){throw 'Pinned input authority changed across guarded launch.'};$pin.stream.Position=0}

$timeoutSeconds=[int][Math]::Ceiling($SetupTimeoutSeconds+($DurationHours*3600)+1800)
$deadline=[DateTime]::UtcNow.AddSeconds($timeoutSeconds)
do{
    $failureItem=Get-ChildItem $evidenceRoot -Filter 'beta-soak-*-combined-failure.json' -File -ErrorAction SilentlyContinue|Where-Object{-not$existingFailures.Contains($_.FullName)}|Sort-Object LastWriteTimeUtc|Select-Object -First 1
    if($failureItem){$failurePin=Open-TrackedPinnedFile $failureItem.FullName 1048576 -Json;$failure=$failurePin.document;if($failure.status-ne'failed'-or$failure.result_kind-ne'combined_fresh_beta_soak'-or([string]$failure.source_commit-and$failure.source_commit-ne$head)-or([string]$failure.installer_sha256-and$failure.installer_sha256-ne$candidate.installer_sha256)-or([string]$failure.payload_manifest_sha256-and$failure.payload_manifest_sha256-ne$payloadBeforeRequest.manifest_sha256)){throw 'New combined failure evidence is malformed or candidate-mismatched.'};$code=([string]$failure.error-replace'[^A-Za-z0-9_.:-]','_');if($code.Length-gt160){$code=$code.Substring(0,160)};$path=$failurePin.path;$hash=$failurePin.sha256;throw "Beta-soak guest failed early: code=$code path=$path sha256=$hash"}
    $failedResultItem=Get-ChildItem $evidenceRoot -Filter 'caption-proof-*.json' -File -ErrorAction SilentlyContinue|Where-Object{-not$existingResults.Contains($_.FullName)}|Sort-Object LastWriteTimeUtc|Select-Object -First 1
    if($failedResultItem){$resultPin=Open-TrackedPinnedFile $failedResultItem.FullName 4194304 -Json;$failedResult=$resultPin.document;if($failedResult.status-eq'failed'){if(([string]$failedResult.source_commit-and$failedResult.source_commit-ne$head)-or([string]$failedResult.installer_sha256-and$failedResult.installer_sha256-ne$candidate.installer_sha256)){throw 'New failed combined result is candidate-mismatched.'};$code=([string]$failedResult.error-replace'[^A-Za-z0-9_.:-]','_');if($code.Length-gt160){$code=$code.Substring(0,160)};$path=$resultPin.path;$hash=$resultPin.sha256;throw "Beta-soak combined result failed early: code=$code path=$path sha256=$hash"}}
    $indexItem=Get-ChildItem -LiteralPath $evidenceRoot -Filter 'beta-soak-*-index.json' -File|Where-Object{-not $existing.Contains($_.FullName)}|Sort-Object LastWriteTimeUtc|Select-Object -First 1
    if($indexItem){
        $evidencePins=[Collections.Generic.List[object]]::new();$indexPin=Open-TrackedPinnedFile $indexItem.FullName 1048576 -Json;$evidencePins.Add($indexPin);$index=$indexPin.document
        if([int]$index.schema_version -ne 1 -or [string]$index.result_kind -cne 'combined_fresh_beta_soak' -or [string]$index.status -cne 'passed' -or
            [string]$index.source_commit -cne $head -or [string]$index.candidate_manifest_sha256 -cne $manifestSha -or [string]$index.installer_sha256 -cne $candidate.installer_sha256 -or [string]$index.payload_manifest_sha256-cne$payloadBeforeRequest.manifest_sha256-or [double]$index.duration_hours -ne $DurationHours){throw 'Beta-soak index candidate/duration binding is invalid.'}
        $artifacts=@($index.artifacts);if($artifacts.Count -ne 6){throw 'Passed beta-soak index must declare exactly six artifacts and no failure artifact.'}
        $seen=[Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        $artifactPins=@{}
        foreach($artifact in $artifacts){
            $name=[string]$artifact.name
            if($name -cnotmatch "^beta-soak-$([regex]::Escape([string]$index.run_id))-(commissioning|configuration|run-state|observations|restart|final-summary)\.(json|ndjson)$" -or -not $seen.Add($name) -or [string]$artifact.sha256 -cnotmatch '^[0-9a-f]{64}$' -or [int64]$artifact.size_bytes -le 0 -or [int64]$artifact.size_bytes -gt 16777216){throw 'Beta-soak artifact declaration is invalid.'}
            $path=Join-Path $evidenceRoot $name;$pin=Open-TrackedPinnedFile $path 16777216 -Json:($name -notlike '*-observations.ndjson');$evidencePins.Add($pin);$artifactPins[$name]=$pin
            if($pin.size -ne [int64]$artifact.size_bytes -or $pin.sha256 -cne [string]$artifact.sha256){throw 'Beta-soak artifact bytes do not match the index.'}
        }
        foreach($requiredSuffix in @('commissioning.json','configuration.json','run-state.json','observations.ndjson','restart.json','final-summary.json')){if(@($artifacts|Where-Object{$_.name -like "*-$requiredSuffix"}).Count -ne 1){throw "Beta-soak required artifact is missing: $requiredSuffix"}}
        $summaryArtifact=@($artifacts|Where-Object{$_.name -like '*-final-summary.json'})[0]
        $commissioningArtifact=@($artifacts|Where-Object{$_.name -like '*-commissioning.json'})[0];$configurationArtifact=@($artifacts|Where-Object{$_.name -like '*-configuration.json'})[0];$observationsArtifact=@($artifacts|Where-Object{$_.name -like '*-observations.ndjson'})[0];$restartArtifact=@($artifacts|Where-Object{$_.name -like '*-restart.json'})[0]
        $commissioning=$artifactPins[[string]$commissioningArtifact.name].document;$configurationResult=$artifactPins[[string]$configurationArtifact.name].document;$restart=$artifactPins[[string]$restartArtifact.name].document
        $summary=$artifactPins[[string]$summaryArtifact.name].document
        if($summary.status -ne 'passed' -or [double]$summary.duration_hours -lt $DurationHours -or [double]$summary.elapsed_seconds -lt ($DurationHours*3600) -or [int]$summary.restart_count -ne 1 -or [double]$summary.coverage_ratio -lt 0.98 -or [int]$summary.actual_observations -lt [int]$summary.minimum_required_observations){throw 'Beta-soak final summary failed duration, restart, or coverage validation.'}
        if(-not(Test-SubstantiveCommissioningReceipt $commissioning $head $candidate $manifestSha ([string]$commissioningArtifact.sha256))){throw 'Published commissioning receipt is not substantive or does not bind the installed candidate.'}
        if($index.commissioning_run_id-cne$commissioning.run_id-or$index.commissioning_result_sha256-cne$commissioningArtifact.sha256-or$configurationResult.schema_version -ne 1 -or $configurationResult.result_kind -ne 'fresh_configuration_run' -or $configurationResult.gate_status -ne 'passed' -or $configurationResult.source_commit -ne $head -or $configurationResult.candidate_manifest_sha256 -ne $manifestSha -or $configurationResult.installer_sha256 -ne $candidate.installer_sha256 -or$configurationResult.payload_manifest_sha256-cne$payloadBeforeRequest.manifest_sha256-or$configurationResult.commissioning_run_id-cne$commissioning.run_id-or $configurationResult.commissioning_result_sha256 -ne $commissioningArtifact.sha256 -or $configurationResult.expected_state -ne 'healthy_fallback' -or $summary.expected_station_id -ne $configurationResult.station_id -or [uint64]$summary.expected_station_revision -ne [uint64]$configurationResult.station_revision -or $summary.expected_channel_id -ne $configurationResult.channel_id -or $summary.configuration_result_sha256 -ne $configurationArtifact.sha256 -or $summary.observations_sha256 -ne $observationsArtifact.sha256 -or $restart.kind -ne 'planned_restart'){throw 'Published configuration/restart/summary evidence is not exactly cross-bound.'}
        $indexSha=$indexPin.sha256
        $combinedItem=Get-ChildItem -LiteralPath $evidenceRoot -Filter 'caption-proof-*.json' -File|Where-Object{$_.Name -match [regex]::Escape([string]$index.run_id)}|Sort-Object LastWriteTimeUtc|Select-Object -Last 1
        if(-not $combinedItem){foreach($pin in $evidencePins){$pin.stream.Dispose()};Start-Sleep -Seconds 1;continue}
        $combinedPin=Open-TrackedPinnedFile $combinedItem.FullName 4194304 -Json;$evidencePins.Add($combinedPin);$combined=$combinedPin.document
        if($combined.status -ne 'passed' -or $combined.source_commit -ne $head -or $combined.installer_sha256 -ne $candidate.installer_sha256 -or $combined.caption_proof.outcome -ne 'verified' -or $combined.beta_soak.index_name -ne $indexItem.Name -or $combined.beta_soak.index_sha256 -ne $indexSha -or$combined.beta_soak.payload_manifest_sha256-cne$payloadBeforeRequest.manifest_sha256-or$combined.beta_soak.commissioning_run_id-cne$commissioning.run_id-or$combined.beta_soak.commissioning_result_sha256-cne$commissioningArtifact.sha256-or $combined.beta_soak.summary.status -ne 'passed'){throw 'Combined beta-soak result does not bind the passed caption and soak evidence.'}
        $terminalPayload=Get-MappedPayloadSnapshot $expectedReadOnly ''
        if($terminalPayload.manifest_sha256-cne$fullPayloadSnapshot.manifest_sha256){throw 'Mapped payload set or identity changed before terminal acceptance.'}
        foreach($pin in $authorityPins){$pin.stream.Position=0;$sha=[Security.Cryptography.SHA256]::Create();try{$observed=([BitConverter]::ToString($sha.ComputeHash($pin.stream))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()};if($observed-cne$pin.sha256){throw 'Pinned mapped payload bytes changed before terminal acceptance.'};$pin.stream.Position=0}
        $immutableAuthority=New-ProtectedImmutableRoot ([string]$index.run_id);$immutableRoot=$immutableAuthority.path
        foreach($pin in $evidencePins){Copy-PinnedFile $pin (Join-Path $immutableRoot ([IO.Path]::GetFileName($pin.path)))}
        Assert-OwnedDirectoryIdentity $immutableAuthority.parent_identity;Assert-OwnedDirectoryIdentity $immutableAuthority.leaf_identity
        [pscustomobject]@{launch=$launch;immutable_root=$immutableRoot;result_path=(Join-Path $immutableRoot $combinedItem.Name);index_path=(Join-Path $immutableRoot $indexItem.Name);index_sha256=$indexSha;summary=$summary};return
    }
    Start-Sleep -Seconds 10
}while([DateTime]::UtcNow -lt $deadline)
throw "Timed out after $timeoutSeconds seconds waiting for owned beta-soak evidence; Sandbox was left untouched."
} finally {foreach($pin in @($trackedPins)){try{$pin.stream.Dispose()}catch{}};foreach($pin in @($authorityPins)){try{$pin.stream.Dispose()}catch{}}}
