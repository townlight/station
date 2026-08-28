<##
.SYNOPSIS
    Observes an already commissioned TownLight Station for the beta soak gate.

.DESCRIPTION
    This is a non-provisioning installed-machine proof. It performs authenticated
    reads, writes evidence, and performs exactly one planned midpoint restart of
    TownLightStation. It does not configure the station, mutate API authority,
    control the updater, clean services/evidence, or adopt foreign processes.
    PowerShell cannot retain no-follow directory handles for the full run, so the
    fixed ProgramData ancestry, protected ACL, root, and unique leaf are checked
    at creation and revalidated before finalization; this is observational path
    evidence, not a claim of kernel-pinned directory identity.
##>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [Management.Automation.PSCredential]$Credential,
    [Parameter(Mandatory = $true)]
    [string]$CommissioningResultPath,
    [Parameter(Mandatory = $true)]
    [string]$ConfigurationResultPath,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')]
    [string]$ExpectedStationId,
    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 18446744073709551615)]
    [uint64]$ExpectedStationRevision,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')]
    [string]$ExpectedChannelId,
    [Parameter(Mandatory = $true)]
    [ValidateSet('healthy_fallback', 'on_air')]
    [string]$ExpectedChannelState,
    [ValidateRange(4, 8)]
    [double]$DurationHours = 4,
    [string]$ExpectedSourceCommit = '',
    [string]$CandidateManifestPath = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\candidate-manifest.json'),
    [uri]$BaseUri = 'http://127.0.0.1:4070'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'candidate-pair-verifier.ps1')
. (Join-Path $PSScriptRoot 'installed-proof-api.ps1')
. (Join-Path $PSScriptRoot 'installed-private-runtime-verifier.ps1')
. (Join-Path $PSScriptRoot 'installed-beta-soak-proof-state.ps1')

$ObservationIntervalSeconds = 10
$IdentityIntervalSeconds = 300
$RestartRecoveryLimitSeconds = 90
$runId = [guid]::NewGuid().ToString('D')
$runDirectory = $null
$observationWriter = $null
$observationStream = $null
$exitCode = 0
$firstFailure = ''
$summary = $null
$stopwatch = $null
$installedIdentity = $null
$commissioningItem = $null
$candidateManifestItem = $null
$configurationItem = $null
$inputPins = [Collections.Generic.List[IDisposable]]::new()
$soakFence = $null
$failureReceiptWritten = $false
$commissioningResultSha256 = ''
$configurationResultSha256 = ''
$candidateResultSha256 = ''

function Get-CanonicalExistingFile([string]$Path, [string]$Label) {
    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction Stop
    $item = Get-Item -LiteralPath $resolved.Path -Force -ErrorAction Stop
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label must be a regular non-reparse file."
    }
    $item
}

function Get-CanonicalExistingDirectory([string]$Path, [string]$Label) {
    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction Stop
    $item = Get-Item -LiteralPath $resolved.Path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label must be an existing non-reparse directory."
    }
    $item
}

function Get-LowerHash([string]$Path) {
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
}

function Open-FrozenInput([string]$Path, [string]$Label, [switch]$ReadJson) {
    $item = Get-CanonicalExistingFile $Path $Label
    $stream = [IO.FileStream]::new(
        $item.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
    )
    $inputPins.Add($stream)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
    $stream.Position = 0
    $document = $null
    if ($ReadJson) {
        if ($stream.Length -le 0 -or $stream.Length -gt 16MB) { throw "$Label JSON size is invalid." }
        $bytes = [byte[]]::new([int]$stream.Length)
        $read = $stream.Read($bytes, 0, $bytes.Length)
        if ($read -ne $bytes.Length) { throw "$Label bytes could not be frozen." }
        try {
            $text = [Text.UTF8Encoding]::new($false, $true).GetString($bytes)
            $document = $text | ConvertFrom-Json -ErrorAction Stop
        }
        catch { throw "$Label is not strict UTF-8 JSON." }
        $stream.Position = 0
    }
    [pscustomobject]@{ item = $item; stream = $stream; sha256 = $hash; document = $document }
}

function Assert-NonReparseAncestry([string]$Path, [string]$Label) {
    $cursor = [IO.DirectoryInfo]::new([IO.Path]::GetFullPath($Path))
    while ($null -ne $cursor) {
        if ($cursor.Exists -and ($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Label ancestry contains a reparse point." }
        $cursor = $cursor.Parent
    }
}

function Assert-ProofAcl([string]$Path) {
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    if (-not $acl.AreAccessRulesProtected) { throw 'proof_evidence_acl_not_protected' }
    $allowed = @('S-1-5-18','S-1-5-32-544')
    foreach ($rule in $acl.Access) {
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($sid -notin $allowed -or $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { throw 'proof_evidence_acl_invalid' }
    }
    foreach ($sid in $allowed) {
        if (-not @($acl.Access | Where-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $sid -and ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -ne 0 }).Count) { throw 'proof_evidence_acl_incomplete' }
    }
}

function Assert-SharedProofsAcl([string]$Path) {
    $acl=Get-Acl -LiteralPath $Path -ErrorAction Stop
    $broad=@('S-1-1-0','S-1-5-11','S-1-5-32-545')
    $writeMask=[Security.AccessControl.FileSystemRights]::WriteData -bor [Security.AccessControl.FileSystemRights]::CreateDirectories -bor [Security.AccessControl.FileSystemRights]::Modify
    foreach($rule in $acl.Access){$sid=$rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value;if($sid -in $broad -and $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and ($rule.FileSystemRights -band $writeMask) -ne 0){throw 'shared_proofs_directory_broad_write_acl'}}
}

function Initialize-ProofRoot([string]$DataRoot) {
    $proofParent = Join-Path $DataRoot 'proofs'
    $proofRoot = Join-Path $proofParent 'beta-soak'
    Assert-NonReparseAncestry $DataRoot 'TownLight ProgramData root'
    if (-not (Test-Path -LiteralPath $proofParent)) { New-Item -ItemType Directory -Path $proofParent -ErrorAction Stop | Out-Null }
    $sharedParent = Get-CanonicalExistingDirectory $proofParent 'shared proofs directory'
    Assert-NonReparseAncestry $sharedParent.FullName 'shared proofs directory'
    Assert-SharedProofsAcl $sharedParent.FullName
    if (-not (Test-Path -LiteralPath $proofRoot)) { New-Item -ItemType Directory -Path $proofRoot -ErrorAction Stop | Out-Null }
    $item = Get-Item -LiteralPath $proofRoot -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'proof_evidence_root_invalid' }
    $security = [Security.AccessControl.DirectorySecurity]::new()
    $security.SetAccessRuleProtection($true, $false)
    foreach ($sidValue in @('S-1-5-18', 'S-1-5-32-544')) {
        $sid = [Security.Principal.SecurityIdentifier]::new($sidValue)
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid,[Security.AccessControl.FileSystemRights]::FullControl,[Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit',[Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]::Allow)
        $security.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $proofRoot -AclObject $security -ErrorAction Stop
    Assert-ProofAcl $proofRoot
    (Get-CanonicalExistingDirectory $proofRoot 'fixed beta-soak proof root').FullName
}

function Assert-ProofRunDirectory {
    $root = Get-CanonicalExistingDirectory $script:proofRoot 'fixed beta-soak proof root'
    $run = Get-CanonicalExistingDirectory $script:runDirectory 'beta-soak run directory'
    if (-not $run.FullName.StartsWith($root.FullName.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($run.FullName) -cne "beta-soak-$runId") {
        throw 'proof_run_directory_rebound'
    }
    Assert-NonReparseAncestry $run.FullName 'beta-soak run directory'
    Assert-ProofAcl $root.FullName
    Assert-ProofAcl $run.FullName
}

function Write-AtomicJson([string]$Path, $Value) {
    $directory = Split-Path -Parent $Path
    $temporary = Join-Path $directory ('.' + [IO.Path]::GetFileName($Path) + '.' + [guid]::NewGuid().ToString('N') + '.tmp')
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 32))
    $stream = [IO.FileStream]::new($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally { $stream.Dispose() }
    Move-Item -LiteralPath $temporary -Destination $Path -ErrorAction Stop
}

function Write-Observation($Value) {
    $json = $Value | ConvertTo-Json -Depth 20 -Compress
    $observationWriter.WriteLine($json)
    $observationWriter.Flush()
    $observationStream.Flush($true)
}

function Get-ServiceIdentity([string]$Name, [string]$ExpectedPath) {
    $service = Get-CimInstance Win32_Service -Filter "Name='$Name'" -ErrorAction Stop
    if ($null -eq $service -or [string]$service.StartName -cne 'LocalSystem' -or
        [string]$service.StartMode -cne 'Auto' -or [string]$service.State -cne 'Running' -or
        [int64]$service.ProcessId -le 0 -or
        -not ([string]$service.PathName).Equals($ExpectedPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw "installed_service_identity_invalid:$Name"
    }
    [ordered]@{
        name = $Name
        path_name = [string]$service.PathName
        start_name = [string]$service.StartName
        start_mode = [string]$service.StartMode
        state = [string]$service.State
        process_id = [int64]$service.ProcessId
    }
}

function Get-EventLogCheckpoint([string]$LogName) {
    try { $latest = Get-WinEvent -LogName $LogName -MaxEvents 1 -ErrorAction Stop }
    catch {
        if ($_.FullyQualifiedErrorId -match 'NoMatchingEventsFound') { return [pscustomobject]@{cursor=[int64]0;latest_observed_record_id=[int64]0;oldest_record_id=[int64]0;oldest_time_utc_ticks=[int64]0} }
        throw
    }
    if ($null -eq $latest) { return [pscustomobject]@{cursor=[int64]0;latest_observed_record_id=[int64]0;oldest_record_id=[int64]0;oldest_time_utc_ticks=[int64]0} }
    if ([int64]$latest.RecordId -le 0) { throw "event_log_checkpoint_invalid:$LogName" }
    $oldest=Get-WinEvent -LogName $LogName -Oldest -MaxEvents 1 -ErrorAction Stop
    [pscustomobject]@{cursor=[int64]$latest.RecordId;latest_observed_record_id=[int64]$latest.RecordId;oldest_record_id=[int64]$oldest.RecordId;oldest_time_utc_ticks=[int64]$oldest.TimeCreated.ToUniversalTime().Ticks}
}

function Assert-EventLogCheckpoint([string]$LogName,$Checkpoint) {
    try { $latest=Get-WinEvent -LogName $LogName -MaxEvents 1 -ErrorAction Stop;$oldest=Get-WinEvent -LogName $LogName -Oldest -MaxEvents 1 -ErrorAction Stop }
    catch {
        if ($_.FullyQualifiedErrorId -match 'NoMatchingEventsFound' -and [int64]$Checkpoint.cursor -eq 0) { return }
        if ($_.FullyQualifiedErrorId -match 'NoMatchingEventsFound') { throw 'event_log_checkpoint_reset' }
        throw
    }
    $decision=Get-TownLightBetaSoakEventCheckpointDecision $Checkpoint ([int64]$latest.RecordId) ([int64]$oldest.RecordId) ([int64]$oldest.TimeCreated.ToUniversalTime().Ticks)
    if($decision -cne 'ok'){throw $decision}
    $Checkpoint.latest_observed_record_id=[int64]$latest.RecordId;$Checkpoint.oldest_record_id=[int64]$oldest.RecordId;$Checkpoint.oldest_time_utc_ticks=[int64]$oldest.TimeCreated.ToUniversalTime().Ticks
}

function Read-EventRecords([hashtable]$Filter,$Checkpoint) {
    Assert-EventLogCheckpoint ([string]$Filter.LogName) $Checkpoint
    try { $records = @(Get-WinEvent -FilterHashtable $Filter -MaxEvents 512 -ErrorAction Stop) }
    catch {
        if ($_.FullyQualifiedErrorId -match 'NoMatchingEventsFound') { return @() }
        throw
    }
    $new=@($records | Where-Object { [int64]$_.RecordId -gt [int64]$Checkpoint.cursor })
    if ($new.Count -ge 512) { throw 'event_log_batch_capacity_exceeded' }
    $new
}

function Get-NewFailureEvents([ref]$ApplicationCursor, [ref]$SystemCursor) {
    $failures = [Collections.Generic.List[object]]::new()
    $applicationRecords = @(Read-EventRecords @{ LogName = 'Application'; ProviderName = @('Application Error', 'Windows Error Reporting'); Id = @(1000, 1001) } $ApplicationCursor.Value)
    $applicationAdvance = Get-TownLightBetaSoakEventAdvance -Cursor $ApplicationCursor.Value.cursor -Records @($applicationRecords | ForEach-Object { [pscustomobject]@{ record_id = [int64]$_.RecordId; source = $_ } })
    $ApplicationCursor.Value.cursor = $applicationAdvance.cursor
    foreach ($record in @($applicationAdvance.records | ForEach-Object { $_.source })) {
        $provider = [string]$record.ProviderName
        $message = [string]$record.Message
        if ($provider -in @('Application Error', 'Windows Error Reporting') -and
            $message -match '(?i)stationd|channel-worker|nas-publisher|station-updater|station-automation-launch-helper') {
            $failures.Add([ordered]@{ log = 'Application'; provider = $provider; event_id = [int]$record.Id; record_id = [int64]$record.RecordId; time_utc = $record.TimeCreated.ToUniversalTime().ToString('o') })
        }
    }
    $systemRecords = @(Read-EventRecords @{ LogName = 'System'; ProviderName = 'Service Control Manager'; Id = @(7023, 7024, 7031, 7032, 7034) } $SystemCursor.Value)
    $systemAdvance = Get-TownLightBetaSoakEventAdvance -Cursor $SystemCursor.Value.cursor -Records @($systemRecords | ForEach-Object { [pscustomobject]@{ record_id = [int64]$_.RecordId; source = $_ } })
    $SystemCursor.Value.cursor = $systemAdvance.cursor
    foreach ($record in @($systemAdvance.records | ForEach-Object { $_.source })) {
        if ([string]$record.Message -match '(?i)TownLightStation') {
            $failures.Add([ordered]@{ log = 'System'; provider = 'Service Control Manager'; event_id = [int]$record.Id; record_id = [int64]$record.RecordId; time_utc = $record.TimeCreated.ToUniversalTime().ToString('o') })
        }
    }
    @($failures | Sort-Object log, record_id)
}

function Assert-ResourceSample($Sample, [ref]$PriorSample, [switch]$RestartBoundary) {
    if ($null -eq $Sample -or [uint64]$Sample.sample_id -eq 0 -or
        [double]$Sample.cpu_percent -lt 0 -or [double]$Sample.cpu_percent -gt 100 -or
        [double]$Sample.ram_total_gb -le 0 -or [double]$Sample.ram_used_gb -lt 0 -or
        [double]$Sample.ram_used_gb -gt [double]$Sample.ram_total_gb -or
        [double]$Sample.media_volume_free_gb -le 0 -or [double]$Sample.backup_volume_free_gb -le 0 -or
        -not [bool]$Sample.database_reachable -or -not [bool]$Sample.backup_volume_writable -or
        -not [bool]$Sample.service_running) {
        throw 'resource_sample_invalid'
    }
    $age = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() - [int64]$Sample.sampled_at_unix_ms
    if ($age -lt -5000 -or $age -gt 90000) { throw 'resource_sample_stale' }
    $decision = Get-TownLightBetaSoakResourceDecision -Prior $PriorSample.Value -Current $Sample `
        -RestartBoundary:$RestartBoundary
    if ($decision -cne 'ok') { throw $decision }
    if ($null -eq $PriorSample.Value -or [uint64]$Sample.sample_id -gt [uint64]$PriorSample.Value.sample_id) {
        $PriorSample.Value = $Sample
    }
}

function Assert-InstalledIdentity {
    if ((Get-LowerHash $script:candidateManifestItem.FullName) -cne $script:frozenCandidateManifestSha256) { throw 'candidate_manifest_bytes_changed' }
    $receiptItem = Get-CanonicalExistingFile $script:installReceiptPath 'install receipt'
    $receipt = Get-Content -LiteralPath $receiptItem.FullName -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    $receiptHash = Get-LowerHash $receiptItem.FullName
    if ($script:frozenInstallReceiptSha256 -and $receiptHash -cne $script:frozenInstallReceiptSha256) {
        throw 'install_receipt_bytes_changed'
    }
    $candidate = Get-TownLightCandidateIdentity -ManifestPath $script:candidateManifestItem.FullName -ExpectedSourceCommit $receipt.source_commit
    if ($script:frozenInstallerSha256 -and [string]$candidate.installer_sha256 -cne $script:frozenInstallerSha256) { throw 'candidate_installer_bytes_changed' }
    if ($script:ExpectedSourceCommit -and [string]$receipt.source_commit -cne $script:ExpectedSourceCommit) {
        throw 'installed_source_commit_mismatch'
    }
    $runtimeItem = Get-CanonicalExistingFile $script:runtimeManifestPath 'runtime manifest'
    $runtimeHash = Get-LowerHash $runtimeItem.FullName
    if ($script:frozenRuntimeManifestSha256 -and $runtimeHash -cne $script:frozenRuntimeManifestSha256) {
        throw 'runtime_manifest_bytes_changed'
    }
    $runtimeManifest = Get-Content -LiteralPath $runtimeItem.FullName -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    if ([string]$receipt.version -cne [string]$candidate.manifest.version -or
        [string]$receipt.source_commit -cne [string]$candidate.manifest.source_commit -or
        [string]$receipt.runtime_manifest_sha256 -cne $runtimeHash -or
        [string]$receipt.media_runtime_sha256 -cne [string]$candidate.manifest.media_runtime_sha256 -or
        [int]$runtimeManifest.schema_version -ne 1 -or
        [string]$runtimeManifest.product -cne 'TownLight Station' -or
        [string]$runtimeManifest.version -cne [string]$candidate.manifest.version -or
        [string]$runtimeManifest.source_commit -cne [string]$candidate.manifest.source_commit -or
        [string]$runtimeManifest.runtime_package_sha256 -cne [string]$candidate.manifest.media_runtime_sha256) {
        throw 'installed_runtime_binding_mismatch'
    }
    $runtimeVerification = Assert-TownLightInstalledPrivateRuntime `
        -RuntimeRoot (Join-Path $script:applicationRoot 'runtime\gstreamer') -RuntimeManifest $runtimeManifest
    $tsduckItem = Get-CanonicalExistingFile $script:tsduckRuntimeManifestPath 'TSDuck runtime manifest'
    $tsduckHash = Get-LowerHash $tsduckItem.FullName
    if ($script:frozenTsdDuckManifestSha256 -and $tsduckHash -cne $script:frozenTsdDuckManifestSha256) {
        throw 'tsduck_runtime_manifest_bytes_changed'
    }
    $tsduckManifest = Get-Content -LiteralPath $tsduckItem.FullName -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    if ([string]$receipt.tsduck_version -cne [string]$candidate.manifest.tsduck_version -or
        [string]$receipt.tsduck_source_url -cne [string]$candidate.manifest.tsduck_source_url -or
        [string]$receipt.tsduck_archive_name -cne [string]$candidate.manifest.tsduck_archive_name -or
        [string]$receipt.tsduck_runtime_sha256 -cne [string]$candidate.manifest.tsduck_runtime_sha256 -or
        [string]$receipt.tsduck_runtime_manifest_sha256 -cne $tsduckHash -or
        [int]$tsduckManifest.schema_version -ne 1 -or
        [string]$tsduckManifest.product -cne 'TownLight Station' -or
        [string]$tsduckManifest.runtime_package_sha256 -cne [string]$candidate.manifest.tsduck_runtime_sha256) {
        throw 'installed_tsduck_runtime_binding_mismatch'
    }
    $tsduckVerification = Assert-TownLightInstalledPrivateRuntime `
        -RuntimeRoot (Join-Path $script:applicationRoot 'runtime\tsduck') -RuntimeManifest $tsduckManifest
    $binaryHashes = [ordered]@{}
    foreach ($binding in @(
        @('stationd.exe', 'stationd_sha256'),
        @('channel-worker.exe', 'channel_worker_sha256'),
        @('nas-publisher.exe', 'nas_publisher_sha256'),
        @('station-automation-launch-helper.exe', 'automation_launch_helper_sha256'),
        @('station-updater.exe', 'station_updater_sha256')
    )) {
        $path = Join-Path $script:applicationRoot $binding[0]
        $hash = Get-LowerHash $path
        if ($hash -cne [string]$candidate.manifest.($binding[1])) { throw "installed_binary_hash_mismatch:$($binding[0])" }
        $binaryHashes[$binding[0]] = $hash
    }
    $activeItem = Get-CanonicalExistingFile (Join-Path $script:dataRoot 'active-slot.json') 'active slot receipt'
    $activeHash = Get-LowerHash $activeItem.FullName
    if ($script:frozenActiveSlotSha256 -and $activeHash -cne $script:frozenActiveSlotSha256) { throw 'active_slot_bytes_changed' }
    $active = Get-Content -LiteralPath $activeItem.FullName -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    $activeHashes = @{}
    foreach ($file in @($active.active.package_manifest.files)) {
        if ([string]::IsNullOrWhiteSpace([string]$file.path) -or $activeHashes.ContainsKey([string]$file.path)) { throw 'active_slot_candidate_mismatch' }
        $activeHashes[[string]$file.path] = ([string]$file.sha256).ToLowerInvariant()
    }
    if ([string]$active.active.relative_directory -cne '.' -or
        [string]$active.active.package_manifest.source_commit -cne [string]$receipt.source_commit -or
        $activeHashes.Count -ne 4 -or $activeHashes['stationd.exe'] -cne $binaryHashes['stationd.exe'] -or
        $activeHashes['channel-worker.exe'] -cne $binaryHashes['channel-worker.exe'] -or
        $activeHashes['nas-publisher.exe'] -cne $binaryHashes['nas-publisher.exe'] -or
        $activeHashes['station-automation-launch-helper.exe'] -cne $binaryHashes['station-automation-launch-helper.exe']) { throw 'active_slot_candidate_mismatch' }
    $station = Get-ServiceIdentity 'TownLightStation' $script:expectedStationServicePath
    $updater = Get-ServiceIdentity 'TownLightStationUpdater' $script:expectedUpdaterServicePath
    $authority = [ordered]@{
        source_commit = [string]$receipt.source_commit
        installer_sha256 = [string]$candidate.installer_sha256
        candidate_manifest_sha256 = $script:frozenCandidateManifestSha256
        install_receipt_sha256 = $receiptHash
        runtime_manifest_sha256 = $runtimeHash
        runtime_file_count = [int]$runtimeVerification.file_count
        tsduck_runtime_manifest_sha256 = $tsduckHash
        tsduck_runtime_file_count = [int]$tsduckVerification.file_count
        active_slot_sha256 = $activeHash
        binaries = $binaryHashes
        station_service = $station
        updater_service = $updater
    }
    $frozenAuthority = [ordered]@{}
    foreach ($key in $authority.Keys) { $frozenAuthority[$key] = $authority[$key] }
    $frozenAuthority.station_service = [ordered]@{
        name = $station.name; path_name = $station.path_name; start_name = $station.start_name; start_mode = $station.start_mode
    }
    $frozenAuthority.updater_service = [ordered]@{
        name = $updater.name; path_name = $updater.path_name; start_name = $updater.start_name; start_mode = $updater.start_mode
    }
    $authority['authority_sha256'] = Get-TownLightBetaSoakObjectHash $frozenAuthority
    $authority
}

function Invoke-Read([string]$Path) {
    Invoke-TownLightProofApi -Session $script:apiSession -Method Get -Path $Path -TimeoutSec 8
}

function Get-Observation([ref]$PriorResource, [ref]$ApplicationCursor, [ref]$SystemCursor) {
    $health = Invoke-RestMethod -Method Get -Uri ($script:apiCoordinates.Origin + '/health') -TimeoutSec 8 -MaximumRedirection 0 -ErrorAction Stop
    if ([string]$health.status -cne 'ready' -or [string]$health.database -cne 'ready' -or
        $null -eq $health.orchestration -or [string]$health.orchestration.status -cne 'ready') {
        throw 'installed_health_not_ready'
    }
    $station = Invoke-Read '/station'
    $channels = @(Invoke-Read '/channels')
    $runtime = Invoke-Read '/runtime-safe-to-air'
    $authorityDecision = Test-TownLightBetaSoakAuthority -ExpectedStationHash $script:frozenStationSha256 `
        -ExpectedChannelHash $script:frozenChannelSha256 -ExpectedChannelId $ExpectedChannelId `
        -ExpectedState $ExpectedChannelState -Station $station -Channels $channels -Runtime $runtime
    if ($authorityDecision -cne 'ok') { throw $authorityDecision }
    $resource = Invoke-Read '/system-resources/latest'
    Assert-ResourceSample $resource $PriorResource
    $alerts = @(Invoke-Read '/alerts')
    $alertDecision = Get-TownLightBetaSoakAlertDecision -Alerts $alerts -StationId $ExpectedStationId `
        -ChannelId $ExpectedChannelId -BaselineOccurrences $script:baselineAlertOccurrences
    if ($alertDecision -cne 'ok') { throw $alertDecision }
    $failureEvents = @(Get-NewFailureEvents $ApplicationCursor $SystemCursor)
    if ($failureEvents.Count -ne 0) { throw 'new_crash_or_scm_failure_event' }
    [ordered]@{
        kind = 'observation'
        run_id = $runId
        sequence = ++$script:observationSequence
        elapsed_seconds = [Math]::Round($stopwatch.Elapsed.TotalSeconds, 3)
        observed_at_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        health = [ordered]@{ status = [string]$health.status; database = [string]$health.database; orchestration = [string]$health.orchestration.status }
        station_id = [string]$station.station_id
        runtime_color = [string]$runtime.color
        runtime_channels = @($runtime.channels).Count
        resource_sample_id = [uint64]$resource.sample_id
        resource_sampled_at_unix_ms = [int64]$resource.sampled_at_unix_ms
        firing_alerts = @($alerts | Where-Object { [string]$_.state -ceq 'firing' }).Count
        new_critical_alerts = 0
        new_failure_events = 0
        application_event_cursor = [int64]$ApplicationCursor.Value.cursor
        application_event_oldest_record_id = [int64]$ApplicationCursor.Value.oldest_record_id
        system_event_cursor = [int64]$SystemCursor.Value.cursor
        system_event_oldest_record_id = [int64]$SystemCursor.Value.oldest_record_id
    }
}

function Invoke-PlannedRestart([ref]$PriorResource, [ref]$ApplicationCursor, [ref]$SystemCursor) {
    if ($script:restartCount -ne 0) { throw 'planned_restart_count_invalid' }
    $beforeIdentity = "$($apiSession.Identity.account_id)|$($apiSession.Identity.username)"
    $preInstalled = Assert-InstalledIdentity
    if ($preInstalled.authority_sha256 -cne $script:frozenInstalledAuthoritySha256) { throw 'installed_identity_changed' }
    $preStation = Invoke-Read '/station'
    $preChannels = @(Invoke-Read '/channels')
    $preRuntime = Invoke-Read '/runtime-safe-to-air'
    $preAuthorityDecision = Test-TownLightBetaSoakAuthority -ExpectedStationHash $script:frozenStationSha256 `
        -ExpectedChannelHash $script:frozenChannelSha256 -ExpectedChannelId $ExpectedChannelId `
        -ExpectedState $ExpectedChannelState -Station $preStation -Channels $preChannels -Runtime $preRuntime
    if ($preAuthorityDecision -cne 'ok') { throw $preAuthorityDecision }
    $preResource = $PriorResource.Value
    if ($null -eq $preResource) { throw 'restart_resource_baseline_missing' }
    $recovery = [Diagnostics.Stopwatch]::StartNew()
    Restart-Service -Name 'TownLightStation' -ErrorAction Stop
    $script:restartCount++
    $health = $null
    while ($recovery.Elapsed.TotalSeconds -le $RestartRecoveryLimitSeconds -and $null -eq $health) {
        try {
            $candidate = Invoke-RestMethod -Method Get -Uri ($script:apiCoordinates.Origin + '/health') -TimeoutSec 3 -MaximumRedirection 0 -ErrorAction Stop
            if ([string]$candidate.status -ceq 'ready' -and [string]$candidate.database -ceq 'ready' -and
                $null -ne $candidate.orchestration -and [string]$candidate.orchestration.status -ceq 'ready') {
                $health = $candidate
            }
        }
        catch { }
        if ($null -eq $health) { Start-Sleep -Milliseconds 500 }
    }
    if ($null -eq $health -or $recovery.Elapsed.TotalSeconds -gt $RestartRecoveryLimitSeconds) {
        throw 'restart_recovery_exceeded_90_seconds'
    }
    Connect-TownLightProofSession -Session $apiSession -Credential $Credential -TimeoutSec 15
    $afterIdentity = "$($apiSession.Identity.account_id)|$($apiSession.Identity.username)"
    $postResource = $null
    $resourceDeadline = [Diagnostics.Stopwatch]::StartNew()
    while ($resourceDeadline.Elapsed.TotalSeconds -le 75 -and $null -eq $postResource) {
        $candidateResource = Invoke-Read '/system-resources/latest'
        if ([uint64]$candidateResource.sample_id -gt [uint64]$preResource.sample_id) { $postResource = $candidateResource }
        if ($null -eq $postResource) { Start-Sleep -Milliseconds 500 }
    }
    if ($null -eq $postResource) { throw 'restart_resource_not_newer_within_75_seconds' }
    $resourceDecision = Get-TownLightBetaSoakResourceDecision -Prior $preResource -Current $postResource -RestartBoundary
    if ($resourceDecision -cne 'ok') { throw $resourceDecision }
    Assert-ResourceSample $postResource $PriorResource -RestartBoundary
    $postInstalled = Assert-InstalledIdentity
    $postStation = Invoke-Read '/station'
    $postChannels = @(Invoke-Read '/channels')
    $postRuntime = Invoke-Read '/runtime-safe-to-air'
    $postAuthorityDecision = Test-TownLightBetaSoakAuthority -ExpectedStationHash $script:frozenStationSha256 `
        -ExpectedChannelHash $script:frozenChannelSha256 -ExpectedChannelId $ExpectedChannelId `
        -ExpectedState $ExpectedChannelState -Station $postStation -Channels $postChannels -Runtime $postRuntime
    if ($postAuthorityDecision -cne 'ok') { throw $postAuthorityDecision }
    $restartDecision = Get-TownLightBetaSoakRestartDecision -PrePid $preInstalled.station_service.process_id `
        -PostPid $postInstalled.station_service.process_id -PreAuthorityHash $preInstalled.authority_sha256 `
        -PostAuthorityHash $postInstalled.authority_sha256 -ResourceDecision $resourceDecision `
        -RecoverySeconds $recovery.Elapsed.TotalSeconds -AuthenticatedIdentityMatches ($afterIdentity -ceq $beforeIdentity)
    if ($restartDecision -cne 'ok') { throw $restartDecision }
    $failureEvents = @(Get-NewFailureEvents $ApplicationCursor $SystemCursor)
    if ($failureEvents.Count -ne 0) { throw 'new_crash_or_scm_failure_event' }
    [ordered]@{
        kind = 'planned_restart'
        run_id = $runId
        elapsed_seconds = [Math]::Round($stopwatch.Elapsed.TotalSeconds, 3)
        recovery_seconds = [Math]::Round($recovery.Elapsed.TotalSeconds, 3)
        authenticated_identity = $afterIdentity
        before = [ordered]@{
            station_process_id = $preInstalled.station_service.process_id
            installed_authority_sha256 = $preInstalled.authority_sha256
            station_sha256 = Get-TownLightBetaSoakObjectHash $preStation
            channel_sha256 = Get-TownLightBetaSoakObjectHash (@($preChannels | Where-Object channel_id -eq $ExpectedChannelId)[0])
            runtime_state = [string](@($preRuntime.channels | Where-Object channel_id -eq $ExpectedChannelId)[0]).state
            resource_sample_id = [uint64]$preResource.sample_id
        }
        after = [ordered]@{
            station_process_id = $postInstalled.station_service.process_id
            installed_authority_sha256 = $postInstalled.authority_sha256
            station_sha256 = Get-TownLightBetaSoakObjectHash $postStation
            channel_sha256 = Get-TownLightBetaSoakObjectHash (@($postChannels | Where-Object channel_id -eq $ExpectedChannelId)[0])
            runtime_state = [string](@($postRuntime.channels | Where-Object channel_id -eq $ExpectedChannelId)[0]).state
            resource_sample_id = [uint64]$postResource.sample_id
        }
    }
}

try {
    $soakFence = Enter-TownLightBetaSoakFence -WaitMilliseconds 0
    $trustedProgramFiles = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
    $trustedProgramData = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
    if ([string]::IsNullOrWhiteSpace($trustedProgramFiles) -or [string]::IsNullOrWhiteSpace($trustedProgramData)) {
        throw 'trusted_known_folder_unavailable'
    }
    $applicationRoot = Join-Path $trustedProgramFiles 'TownLight Station'
    $dataRoot = Join-Path $trustedProgramData 'TownLight Station'
    $installReceiptPath = Join-Path $dataRoot 'install-receipt.json'
    $runtimeManifestPath = Join-Path $dataRoot 'runtime-manifest.json'
    $tsduckRuntimeManifestPath = Join-Path $dataRoot 'tsduck-runtime-manifest.json'
    $activeSlotPath = Join-Path $dataRoot 'active-slot.json'
    $databasePath = Join-Path $dataRoot 'station.db'
    $expectedStationServicePath = '"' + (Join-Path $applicationRoot 'stationd.exe') + '" service --database "' + $databasePath + '" --address 127.0.0.1:4070'
    $expectedUpdaterServicePath = '"' + (Join-Path $applicationRoot 'station-updater.exe') + '" service --database "' + $databasePath + '" --application-root "' + $applicationRoot + '"'
    $apiCoordinates = Get-TownLightProofApiCoordinates -BaseUri $BaseUri

    $proofRoot = Initialize-ProofRoot $dataRoot
    $runDirectory = Join-Path $proofRoot ("beta-soak-$runId")
    New-Item -ItemType Directory -Path $runDirectory -ErrorAction Stop | Out-Null
    Set-Acl -LiteralPath $runDirectory -AclObject (Get-Acl -LiteralPath $proofRoot -ErrorAction Stop) -ErrorAction Stop
    Assert-ProofRunDirectory
    $commissioningPin = Open-FrozenInput $CommissioningResultPath 'CommissioningResultPath' -ReadJson
    $configurationPin = Open-FrozenInput $ConfigurationResultPath 'ConfigurationResultPath' -ReadJson
    $candidatePin = Open-FrozenInput $CandidateManifestPath 'CandidateManifestPath' -ReadJson
    $commissioningItem = $commissioningPin.item
    $configurationItem = $configurationPin.item
    $candidateManifestItem = $candidatePin.item
    $commissioning = $commissioningPin.document
    $configuration = $configurationPin.document
    $commissioningResultSha256=$commissioningPin.sha256
    $configurationResultSha256=$configurationPin.sha256
    $candidateResultSha256=$candidatePin.sha256
    if ([string]$commissioning.gate_status -cne 'passed') { throw 'commissioning_gate_status_not_passed' }
    $installReceiptPin = Open-FrozenInput $installReceiptPath 'install receipt' -ReadJson
    $runtimeManifestPin = Open-FrozenInput $runtimeManifestPath 'runtime manifest' -ReadJson
    $tsduckManifestPin = Open-FrozenInput $tsduckRuntimeManifestPath 'TSDuck runtime manifest' -ReadJson
    $activeSlotPin = Open-FrozenInput $activeSlotPath 'active slot receipt' -ReadJson
    $frozenInstallReceiptSha256 = $installReceiptPin.sha256
    $frozenRuntimeManifestSha256 = $runtimeManifestPin.sha256
    $frozenTsdDuckManifestSha256 = $tsduckManifestPin.sha256
    $frozenActiveSlotSha256 = $activeSlotPin.sha256
    $frozenCandidateManifestSha256 = $candidatePin.sha256
    $candidatePair = Get-TownLightCandidateIdentity -ManifestPath $candidateManifestItem.FullName -ExpectedSourceCommit ([string]$installReceiptPin.document.source_commit)
    $installerPin = Open-FrozenInput $candidatePair.installer_path 'candidate installer'
    if ($installerPin.sha256 -cne $candidatePair.installer_sha256) { throw 'candidate_installer_bytes_changed' }
    $frozenInstallerSha256 = $installerPin.sha256
    $configurationDecision=Test-TownLightBetaSoakConfigurationResult -Value $configuration -StationId $ExpectedStationId -StationRevision $ExpectedStationRevision -ChannelId $ExpectedChannelId -ExpectedState $ExpectedChannelState -SourceCommit ([string]$installReceiptPin.document.source_commit) -CandidateManifestSha256 $candidatePin.sha256 -InstallerSha256 $installerPin.sha256 -CommissioningResultSha256 $commissioningPin.sha256
    if ($configurationDecision -cne 'ok') { throw $configurationDecision }
    Write-AtomicJson (Join-Path $runDirectory 'run-state.json') ([ordered]@{
        schema_version = 1; run_id = $runId; status = 'running'
        started_at_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        duration_hours = $DurationHours; observation_interval_seconds = $ObservationIntervalSeconds
        expected_station_id = $ExpectedStationId; expected_station_revision = $ExpectedStationRevision
        expected_channel_id = $ExpectedChannelId; expected_channel_state = $ExpectedChannelState
        commissioning_result_sha256 = $commissioningPin.sha256
        configuration_result_sha256 = $configurationPin.sha256
        candidate_manifest_sha256 = $candidatePin.sha256
        installer_sha256 = $installerPin.sha256
    })
    $observationPath = Join-Path $runDirectory 'observations.ndjson'
    $observationStream = [IO.FileStream]::new($observationPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    $observationWriter = [IO.StreamWriter]::new($observationStream, [Text.UTF8Encoding]::new($false), 4096, $true)
    $observationWriter.AutoFlush = $false

    $installedIdentity = Assert-InstalledIdentity
    $frozenInstalledAuthoritySha256 = $installedIdentity.authority_sha256
    if ([string]$commissioning.evidence.installed_identity.source_commit -cne [string]$installedIdentity.source_commit -or
        [string]$commissioning.evidence.installed_identity.installer_sha256 -cne [string]$installedIdentity.installer_sha256 -or
        [string]$commissioning.evidence.installed_identity.stationd_sha256 -cne [string]$installedIdentity.binaries['stationd.exe'] -or
        [string]$commissioning.evidence.installed_identity.channel_worker_sha256 -cne [string]$installedIdentity.binaries['channel-worker.exe'] -or
        [string]$commissioning.evidence.installed_identity.station_updater_sha256 -cne [string]$installedIdentity.binaries['station-updater.exe'] -or
        [string]$commissioning.evidence.installed_identity.media_runtime_sha256 -cne [string]$installReceiptPin.document.media_runtime_sha256) {
        throw 'commissioning_candidate_install_binding_mismatch'
    }
    $apiSession = New-TownLightProofSession -BaseUri $BaseUri -Credential $Credential -TimeoutSec 15
    $baselineIdentity = "$($apiSession.Identity.account_id)|$($apiSession.Identity.username)"
    $initialStation = Invoke-Read '/station'
    $initialChannels = @(Invoke-Read '/channels')
    $initialRuntime = Invoke-Read '/runtime-safe-to-air'
    if ([string]$initialStation.station_id -cne $ExpectedStationId -or [uint64]$initialStation.revision -ne $ExpectedStationRevision) { throw 'expected_station_binding_mismatch' }
    $frozenStationSha256 = Get-TownLightBetaSoakObjectHash $initialStation
    $selectedInitial = @($initialChannels | Where-Object { [string]$_.channel_id -ceq $ExpectedChannelId })
    if ($selectedInitial.Count -ne 1) { throw 'selected_channel_not_exactly_once' }
    $frozenChannelSha256 = Get-TownLightBetaSoakObjectHash $selectedInitial[0]
    $initialAuthority = Test-TownLightBetaSoakAuthority -ExpectedStationHash $frozenStationSha256 -ExpectedChannelHash $frozenChannelSha256 -ExpectedChannelId $ExpectedChannelId -ExpectedState $ExpectedChannelState -Station $initialStation -Channels $initialChannels -Runtime $initialRuntime
    if ($initialAuthority -cne 'ok') { throw $initialAuthority }
    $baselineAlerts = @(Invoke-Read '/alerts')
    $baselineAlertOccurrences = @{}
    foreach ($alert in $baselineAlerts) {
        $alertState = ([string]$alert.state).ToLowerInvariant()
        $alertSeverity = ([string]$alert.severity).ToLowerInvariant()
        $alertCondition = (([string]$alert.condition).ToLowerInvariant() -replace '_','-')
        if ($alertState -ceq 'firing' -and ($alertSeverity -in @('critical','error') -or $alertCondition -in @('off-air','output-stall','encoder-death')) -and [string]$alert.resource_ref -in @($ExpectedStationId,$ExpectedChannelId,'station','system')) { throw 'preflight_firing_alert' }
        $baselineAlertOccurrences["$($alert.event_id)|$alertCondition|$($alert.resource_ref)"] = [uint64]$alert.occurrence_count
    }
    $applicationCheckpoint = Get-EventLogCheckpoint 'Application'
    $systemCheckpoint = Get-EventLogCheckpoint 'System'

    $observationSequence = 0
    $restartCount = 0
    $priorResource = $null
    $durationSeconds = $DurationHours * 3600.0
    $midpointSeconds = $durationSeconds / 2.0
    $nextObservationSeconds = 0.0
    $nextIdentitySeconds = $IdentityIntervalSeconds
    $restartBoundaryPending = $false
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    $soakStartedUtcMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $coverageState = New-TownLightBetaSoakCoverageState -StartElapsedSeconds 0.0 -StartUtcMs $soakStartedUtcMs
    Write-Observation ([ordered]@{ kind='coverage_anchor'; run_id=$runId; elapsed_seconds=0.0; observed_at_unix_ms=$soakStartedUtcMs; counted_observation=$false })
    while ($stopwatch.Elapsed.TotalSeconds -lt $durationSeconds) {
        if ($restartCount -eq 0 -and $stopwatch.Elapsed.TotalSeconds -ge $midpointSeconds) {
            Write-Observation (Invoke-PlannedRestart ([ref]$priorResource) ([ref]$applicationCheckpoint) ([ref]$systemCheckpoint))
            $restartBoundaryPending = $true
        }
        $observation = Get-Observation ([ref]$priorResource) ([ref]$applicationCheckpoint) ([ref]$systemCheckpoint)
        Add-TownLightBetaSoakCoverageCompletion -State $coverageState -ElapsedSeconds $observation.elapsed_seconds -UtcMs $observation.observed_at_unix_ms -RestartBoundary:$restartBoundaryPending
        $restartBoundaryPending = $false
        Write-Observation $observation
        if ($stopwatch.Elapsed.TotalSeconds -ge $nextIdentitySeconds) {
            $identity = Assert-InstalledIdentity
            if ($identity.authority_sha256 -cne $frozenInstalledAuthoritySha256) { throw 'installed_identity_changed' }
            Write-Observation ([ordered]@{ kind = 'identity_check'; run_id = $runId; elapsed_seconds = [Math]::Round($stopwatch.Elapsed.TotalSeconds, 3); identity = $identity })
            $nextIdentitySeconds += $IdentityIntervalSeconds
        }
        $nextObservationSeconds += $ObservationIntervalSeconds
        $remaining = $nextObservationSeconds - $stopwatch.Elapsed.TotalSeconds
        if ($remaining -gt 0) { Start-Sleep -Milliseconds ([int][Math]::Min(10000, [Math]::Ceiling($remaining * 1000.0))) }
    }
    if ($restartCount -ne 1) { throw 'planned_restart_count_invalid' }
    $finalIdentity = Assert-InstalledIdentity
    if ($finalIdentity.authority_sha256 -cne $frozenInstalledAuthoritySha256) { throw 'installed_identity_changed' }
    $finalObservation = Get-Observation ([ref]$priorResource) ([ref]$applicationCheckpoint) ([ref]$systemCheckpoint)
    Add-TownLightBetaSoakCoverageCompletion -State $coverageState -ElapsedSeconds $finalObservation.elapsed_seconds -UtcMs $finalObservation.observed_at_unix_ms
    Write-Observation $finalObservation
    $coverage = Get-TownLightBetaSoakCoverageResult -State $coverageState -DurationSeconds $durationSeconds -IntervalSeconds $ObservationIntervalSeconds
    if (-not (Test-TownLightBetaSoakMayPass -ElapsedHours $stopwatch.Elapsed.TotalHours -RestartCount $restartCount -Failed $false -CoveragePassed $coverage.passed)) { throw 'beta_soak_duration_or_coverage_not_met' }
    $summary = [ordered]@{
        schema_version = 1; run_id = $runId; status = 'passed'
        started_at_unix_ms = [int64](Get-Content -LiteralPath (Join-Path $runDirectory 'run-state.json') -Raw | ConvertFrom-Json).started_at_unix_ms
        finished_at_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        soak_started_at_unix_ms = $soakStartedUtcMs
        elapsed_seconds = [Math]::Round($stopwatch.Elapsed.TotalSeconds, 3)
        duration_hours = $DurationHours; observation_count = $observationSequence; restart_count = $restartCount
        coverage_ratio = $coverage.coverage_ratio; minimum_required_observations = $coverage.minimum_required
        actual_observations = $coverage.actual; expected_observations = $coverage.expected
        max_normal_observation_gap_seconds = $coverage.max_normal_gap_seconds
        max_restart_observation_gap_seconds = $coverage.max_restart_gap_seconds
        authenticated_identity = $baselineIdentity
        expected_station_id = $ExpectedStationId; expected_station_revision = $ExpectedStationRevision
        expected_channel_id = $ExpectedChannelId; expected_channel_state = $ExpectedChannelState
        station_sha256 = $frozenStationSha256; channel_sha256 = $frozenChannelSha256
        application_event_checkpoint_record_id = $applicationCheckpoint.cursor
        system_event_checkpoint_record_id = $systemCheckpoint.cursor
        application_event_checkpoint = $applicationCheckpoint
        system_event_checkpoint = $systemCheckpoint
        commissioning_result_sha256 = $commissioningPin.sha256
        configuration_result_sha256 = $configurationPin.sha256
        candidate_manifest_sha256 = $candidatePin.sha256
        installer_sha256 = $finalIdentity.installer_sha256
        install_receipt_sha256 = $finalIdentity.install_receipt_sha256
        runtime_manifest_sha256 = $finalIdentity.runtime_manifest_sha256
        tsduck_runtime_manifest_sha256 = $finalIdentity.tsduck_runtime_manifest_sha256
        observations_sha256 = ''
        first_failure = $null
    }
}
catch {
    $exitCode = 1
    $firstFailure = $_.Exception.Message
    if ($null -ne $runDirectory -and (Test-Path -LiteralPath $runDirectory -PathType Container)) {
        try { Write-TownLightBetaSoakFailureReceipt -Path (Join-Path $runDirectory 'failure.json') -RunId $runId -Detail $firstFailure | Out-Null; $failureReceiptWritten = $true } catch { }
    }
    $summary = [ordered]@{
        schema_version = 1; run_id = $runId; status = 'failed'
        finished_at_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        elapsed_seconds = if ($null -ne $stopwatch) { [Math]::Round($stopwatch.Elapsed.TotalSeconds, 3) } else { 0 }
        restart_count = if (Get-Variable restartCount -ErrorAction SilentlyContinue) { $restartCount } else { 0 }
        commissioning_result_sha256 = $commissioningResultSha256
        configuration_result_sha256 = $configurationResultSha256
        candidate_manifest_sha256 = $candidateResultSha256
        installer_sha256 = if ($null -ne $installedIdentity) { [string]$installedIdentity.installer_sha256 } else { '' }
        install_receipt_sha256 = if ($null -ne $installedIdentity) { [string]$installedIdentity.install_receipt_sha256 } else { '' }
        runtime_manifest_sha256 = if ($null -ne $installedIdentity) { [string]$installedIdentity.runtime_manifest_sha256 } else { '' }
        tsduck_runtime_manifest_sha256 = if ($null -ne $installedIdentity) { [string]$installedIdentity.tsduck_runtime_manifest_sha256 } else { '' }
        observations_sha256 = ''
        first_failure = $firstFailure
    }
}
finally {
    $finalization = Invoke-TownLightBetaSoakFinalizationStateMachine -CloseEvidence {
        $closeFailure = $null
        if ($null -ne $observationWriter) { try { $observationWriter.Flush(); $observationWriter.Dispose() } catch { $closeFailure = $_ } }
        if ($null -ne $observationStream) { try { $observationStream.Flush($true); $observationStream.Dispose() } catch { if ($null -eq $closeFailure) { $closeFailure = $_ } } }
        if ($null -ne $closeFailure) { throw $closeFailure }
    } -FinalizeEvidence {
        if ($null -ne $runDirectory -and (Test-Path -LiteralPath $runDirectory -PathType Container)) {
            Assert-ProofRunDirectory
            $observationPath = Join-Path $runDirectory 'observations.ndjson'
            if ($null -ne $summary -and (Test-Path -LiteralPath $observationPath -PathType Leaf)) { $summary.observations_sha256 = Get-LowerHash $observationPath }
            $summaryTemp = Join-Path $runDirectory ('.final-summary.' + [guid]::NewGuid().ToString('N') + '.tmp')
            Write-AtomicJson $summaryTemp $summary
            Move-Item -LiteralPath $summaryTemp -Destination (Join-Path $runDirectory 'final-summary.json') -ErrorAction Stop
        }
    } -PersistFailure {
        param($Detail)
        $script:exitCode = 1
        if ($null -ne $summary) { $summary.status='failed'; $summary.first_failure=$Detail }
        if (-not $failureReceiptWritten -and $null -ne $runDirectory -and (Test-Path -LiteralPath $runDirectory -PathType Container)) {
            Write-TownLightBetaSoakFailureReceipt -Path (Join-Path $runDirectory 'failure.json') -RunId $runId -Detail $Detail | Out-Null
            $script:failureReceiptWritten=$true
        }
    }
    if (-not $finalization.passed) { $exitCode=1 }
    foreach ($pin in $inputPins) { try { $pin.Dispose() } catch { } }
    if ($null -ne $soakFence) { Exit-TownLightBetaSoakFence $soakFence }
}

$summary | ConvertTo-Json -Depth 32
if ($exitCode -ne 0) { exit 1 }
