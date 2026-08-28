[CmdletBinding()]
param([switch]$InstalledElevatedMutexEvidence)
$ErrorActionPreference = 'Stop'
$proofPath = Join-Path $PSScriptRoot 'prove-installed-beta-soak.ps1'
$statePath = Join-Path $PSScriptRoot 'installed-beta-soak-proof-state.ps1'
$assertions = 0
function Assert-Contract([bool]$Condition, [string]$Message) { $script:assertions++; if (-not $Condition) { throw "ASSERTION FAILED: $Message" } }
foreach ($path in @($proofPath, $statePath)) {
    Assert-Contract (Test-Path -LiteralPath $path -PathType Leaf) "$path must exist."
    $tokens=$null; $errors=$null
    [Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errors)|Out-Null
    Assert-Contract ($errors.Count -eq 0) "$path must parse."
}
. $statePath
$source=Get-Content -LiteralPath $proofPath -Raw
$stateSource=Get-Content -LiteralPath $statePath -Raw
if($InstalledElevatedMutexEvidence){$principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent());if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'InstalledElevatedMutexEvidence requires an elevated Administrator or LocalSystem context.'}}
Assert-Contract ($source -match 'ValidateRange\(4, 8\)' -and $source -match '\$ObservationIntervalSeconds\s*=\s*10' -and $source -match '\[Diagnostics\.Stopwatch\]::StartNew') 'duration/cadence must be 4-8h/10s/monotonic.'
Assert-Contract ($source -match 'ConfigurationResultPath' -and $source -match 'ExpectedStationId' -and $source -match 'ExpectedStationRevision' -and $source -match 'ExpectedChannelId' -and $source -match 'ExpectedChannelState') 'fresh configuration and explicit station/channel/state binding are mandatory.'
Assert-Contract ($source -match "Invoke-Read '/station'" -and $source -match "Invoke-Read '/channels'" -and $source -match "Invoke-Read '/runtime-safe-to-air'" -and $source -match 'Test-TownLightBetaSoakAuthority') 'samples must validate exact authority.'
Assert-Contract ($source -match 'Open-FrozenInput' -and $source -match 'FileShare\]::Read' -and $source -match 'active-slot\.json' -and $source -match 'frozenInstalledAuthoritySha256' -and $source -match 'Assert-TownLightInstalledPrivateRuntime') 'input bytes and complete installed identity must stay frozen.'
Assert-Contract ($stateSource -match 'New-TownLightBetaSoakMutexHandle' -and $stateSource -match 'S-1-5-18' -and $stateSource -match 'S-1-5-32-544' -and $source -match 'Enter-TownLightBetaSoakFence' -and $source -match 'Exit-TownLightBetaSoakFence') 'full-lifetime Global fence requires protected SYSTEM/Admin DACL.'
Assert-Contract ($stateSource -match "System\.Threading\.MutexAcl' -as \[type\]" -and $stateSource -match '\[Threading\.Mutex\]\.GetConstructors' -and $stateSource -match 'MutexSecurity') 'mutex factory must support both modern .NET and Windows PowerShell 5.1 secure constructors.'
Assert-Contract ($source -notmatch '\[string\]\$EvidenceRoot' -and $source -match 'CommonApplicationData' -and $source -match "proofs'" -and $source -match "beta-soak'" -and $source -match 'Assert-ProofRunDirectory') 'evidence must use fixed validated ProgramData proof root.'
Assert-Contract (($source|Select-String -AllMatches 'Set-Acl -LiteralPath \$proofRoot').Matches.Count -eq 1 -and $source -notmatch 'Set-Acl -LiteralPath \$proofParent') 'shared proofs directory must be validated but never ACL-rewritten.'
Assert-Contract ($source -match 'preflight_firing_alert' -and $source -match "off-air','output-stall','encoder-death" -and $stateSource -match '\$\(\$alert\.event_id\).*\$condition.*\$\(\$alert\.resource_ref\)') 'fatal alerts and recurrence require occurrence identity.'
Assert-Contract ($source -match 'NoMatchingEventsFound' -and $source -match 'Get-TownLightBetaSoakEventAdvance' -and $source -match '\[ref\]\$ApplicationCursor' -and $source -match '\[ref\]\$SystemCursor') 'event logs need incremental cursors and exact empty handling.'
Assert-Contract ($source -match 'event_log_batch_capacity_exceeded' -and $stateSource -match 'event_log_batch_capacity_exceeded') 'capped event batches must fail closed.'
Assert-Contract ($source -match 'Add-TownLightBetaSoakCoverageCompletion' -and $source -match 'coverage_ratio' -and $source -match 'minimum_required_observations' -and $source -match 'max_normal_observation_gap_seconds' -and $source -match 'max_restart_observation_gap_seconds') 'summary must prove continuous observation coverage and bounded gaps.'
Assert-Contract ($stateSource -match 'configuration_result_schema_invalid' -and $stateSource -match 'fresh_configuration_run' -and $stateSource -match 'commissioning_result_sha256') 'configuration result schema 1 must bind candidate/install/commissioning.'
Assert-Contract ($source -match 'restart_resource_not_newer_within_75_seconds' -and $stateSource -match 'resource_sample_identity_regressed' -and $stateSource -match '55000' -and $stateSource -match '70000') 'resource identity/cadence must remain monotonic.'
Assert-Contract (($source|Select-String -AllMatches 'Restart-Service').Matches.Count -eq 1 -and $source -match "Restart-Service -Name 'TownLightStation'" -and $stateSource -match 'restart_process_identity_invalid' -and $source -match 'Connect-TownLightProofSession') 'exactly one restart must prove new PID and reconnect.'
Assert-Contract ($source -match 'Write-TownLightBetaSoakFailureReceipt' -and $stateSource -match 'FileMode\]::CreateNew' -and $stateSource -match 'Flush\(\$true\)' -and $source -match 'final-summary\.json') 'failure receipt and final summary must be durable/atomic.'
Assert-Contract ($source -notmatch '(?i)\b(Start-Process|Stop-Process|taskkill|\.Kill\s*\()\b' -and $source -notmatch '(?i)Remove-(Item|Service)|DeleteService|sc\.exe\s+(?:delete|config)') 'proof must not touch foreign resources.'
Assert-Contract ($source -notmatch '(?i)Invoke-TownLightProofApi[^\r\n]*(Post|Put|Patch|Delete)' -and $source -notmatch '(?i)SQLiteConnection|sqlite3') 'proof must remain non-provisioning.'

$station=[pscustomobject]@{station_id='11111111-1111-4111-8111-111111111111';revision=7}
$channel=[pscustomobject]@{channel_id='22222222-2222-4222-8222-222222222222';name='Main';enabled=$true}
$runtime=[pscustomobject]@{color='green';channels=@([pscustomobject]@{channel_id=$channel.channel_id;state='on_air'})}
$stationHash=Get-TownLightBetaSoakObjectHash $station; $channelHash=Get-TownLightBetaSoakObjectHash $channel
Assert-Contract ((Test-TownLightBetaSoakAuthority $stationHash $channelHash $channel.channel_id 'on_air' $station @($channel) $runtime) -ceq 'ok') 'valid authority must pass.'
Assert-Contract ((Test-TownLightBetaSoakAuthority $stationHash $channelHash $channel.channel_id 'on_air' $station @() $runtime) -ceq 'selected_channel_not_exactly_once') 'channel disappearance must fail.'
$changed=[pscustomobject]@{channel_id=$channel.channel_id;name='Changed';enabled=$true}
Assert-Contract ((Test-TownLightBetaSoakAuthority $stationHash $channelHash $channel.channel_id 'on_air' $station @($changed) $runtime) -ceq 'selected_channel_configuration_changed') 'config drift must fail.'
$swapped=[pscustomobject]@{station_id=$station.station_id;revision=8}
Assert-Contract ((Test-TownLightBetaSoakAuthority $stationHash $channelHash $channel.channel_id 'on_air' $swapped @($channel) $runtime) -ceq 'station_authority_changed') 'station identity swap must fail.'
$alert=[pscustomobject]@{event_id='evt-1';condition='off-air';resource_ref=$channel.channel_id;severity='critical';state='firing';occurrence_count=1}
Assert-Contract ((Get-TownLightBetaSoakAlertDecision @($alert) $station.station_id $channel.channel_id @{}) -ceq 'new_firing_alert') 'active preflight alert must fail.'
$baseline=@{'evt-1|off-air|22222222-2222-4222-8222-222222222222'=[uint64]1}
Assert-Contract ((Get-TownLightBetaSoakAlertDecision @($alert) $station.station_id $channel.channel_id $baseline) -ceq 'preflight_firing_alert') 'active fatal baseline cannot be grandfathered.'
$alert.occurrence_count=2
Assert-Contract ((Get-TownLightBetaSoakAlertDecision @($alert) $station.station_id $channel.channel_id $baseline) -ceq 'firing_alert_recurred') 'recurrence must fail.'
$empty=Get-TownLightBetaSoakEventAdvance 12 @(); Assert-Contract ($empty.cursor -eq 12 -and $empty.records.Count -eq 0) 'empty events preserve cursor.'
$advanced=Get-TownLightBetaSoakEventAdvance 12 @([pscustomobject]@{record_id=11},[pscustomobject]@{record_id=14},[pscustomobject]@{record_id=13})
Assert-Contract ($advanced.cursor -eq 14 -and @($advanced.records).Count -eq 2) 'event cursor advances incrementally.'
$capacityRejected=$false;try{Get-TownLightBetaSoakEventAdvance 0 @(1..512|ForEach-Object{[pscustomobject]@{record_id=$_;hidden_relevant_failure=($_ -eq 512)}})|Out-Null}catch{$capacityRejected=$_.Exception.Message -match 'event_log_batch_capacity_exceeded'}
Assert-Contract $capacityRejected 'a full capped event batch with a hidden terminal failure must fail closed.'
$oldBatch=Get-TownLightBetaSoakEventAdvance 512 @(1..512|ForEach-Object{[pscustomobject]@{record_id=$_}})
Assert-Contract ($oldBatch.cursor -eq 512 -and @($oldBatch.records).Count -eq 0) '512 old records at/below cursor must be empty and preserve cursor.'
$checkpoint=[pscustomobject]@{cursor=[int64]100;latest_observed_record_id=[int64]100;oldest_record_id=[int64]1;oldest_time_utc_ticks=[int64]1000}
Assert-Contract ((Get-TownLightBetaSoakEventCheckpointDecision $checkpoint 5 1 2000) -ceq 'event_log_checkpoint_reset') 'cleared/recreated log with reset latest/time identity must fail closed.'
Assert-Contract ((Get-TownLightBetaSoakEventCheckpointDecision $checkpoint 150 1 2000) -ceq 'event_log_checkpoint_reset') 'recreated/refilled log with changed oldest identity must fail closed.'
$rapidRefill=[pscustomobject]@{cursor=[int64]100;latest_observed_record_id=[int64]1000;oldest_record_id=[int64]1;oldest_time_utc_ticks=[int64]1000}
Assert-Contract ((Get-TownLightBetaSoakEventCheckpointDecision $rapidRefill 500 20 2000) -ceq 'event_log_checkpoint_reset') 'rapid clear/refill below prior latest observed record must fail closed.'
$r1=[pscustomobject]@{sample_id=[uint64]10;sampled_at_unix_ms=[int64]100000};$r2=[pscustomobject]@{sample_id=[uint64]11;sampled_at_unix_ms=[int64]160000};$rr=[pscustomobject]@{sample_id=[uint64]9;sampled_at_unix_ms=[int64]160000}
Assert-Contract ((Get-TownLightBetaSoakResourceDecision $r1 $r2) -ceq 'ok') 'resource cadence must pass.'
Assert-Contract ((Get-TownLightBetaSoakResourceDecision $r1 $rr) -ceq 'resource_sample_identity_regressed') 'resource regression must fail.'
Assert-Contract ((Get-TownLightBetaSoakResourceDecision $r1 $r2 -RestartBoundary) -ceq 'ok') 'new restart resource passes.'
Assert-Contract ((Get-TownLightBetaSoakRestartDecision 10 10 'a' 'a' 'ok' 10 $true) -ceq 'restart_process_identity_invalid') 'same PID must fail.'
Assert-Contract ((Get-TownLightBetaSoakRestartDecision 10 11 'a' 'b' 'ok' 10 $true) -ceq 'restart_authority_changed') 'restart identity swap must fail.'
Assert-Contract ((Get-TownLightBetaSoakRestartDecision 10 11 'a' 'a' 'ok' 75 $true) -ceq 'ok') 'bounded recovery passes.'
Assert-Contract (-not (Test-TownLightBetaSoakMayPass 3.999 1 $false)) 'under four hours cannot pass.'
Assert-Contract (Test-TownLightBetaSoakMayPass 4 1 $false) 'four hours may pass.'
$sparse=New-TownLightBetaSoakCoverageState -StartUtcMs 100000
Add-TownLightBetaSoakCoverageCompletion $sparse 0.001 100001
$largeGapRejected=$false;try{Add-TownLightBetaSoakCoverageCompletion $sparse 3600 3700000}catch{$largeGapRejected=$_.Exception.Message -match 'normal_observation_gap_exceeded'}
$sparseResult=Get-TownLightBetaSoakCoverageResult $sparse 14400 10
Assert-Contract ($largeGapRejected -and -not $sparseResult.passed -and -not (Test-TownLightBetaSoakMayPass 4 1 $false $sparseResult.passed)) 'four elapsed hours with sparse samples/large gap cannot pass.'
$suspend=New-TownLightBetaSoakCoverageState -StartUtcMs 100000;Add-TownLightBetaSoakCoverageCompletion $suspend 0.001 100001
$suspendRejected=$false;try{Add-TownLightBetaSoakCoverageCompletion $suspend 10 130000}catch{$suspendRejected=$_.Exception.Message -match 'host_clock_or_suspend_discontinuity'}
Assert-Contract $suspendRejected 'host suspend/clock discontinuity must fail.'
$lateFirst=New-TownLightBetaSoakCoverageState -StartUtcMs 100000
$lateFirstRejected=$false;try{Add-TownLightBetaSoakCoverageCompletion $lateFirst 3600 3700000}catch{$lateFirstRejected=$_.Exception.Message -match 'normal_observation_gap_exceeded'}
Assert-Contract $lateFirstRejected 'a first completion one hour after soak start must fail even if later samples catch up.'
$laterFirst=New-TownLightBetaSoakCoverageState -StartUtcMs 100000
$laterFirstRejected=$false;try{Add-TownLightBetaSoakCoverageCompletion $laterFirst 7200 7300000}catch{$laterFirstRejected=$_.Exception.Message -match 'normal_observation_gap_exceeded'}
Assert-Contract $laterFirstRejected 'a first completion two hours after soak start must fail.'
$configuration=[pscustomobject][ordered]@{schema_version=1;product='TownLight Station';result_kind='fresh_configuration_run';gate_status='passed';run_id='44444444-4444-4444-8444-444444444444';finished_at_unix_ms=1;station_id=$station.station_id;station_revision=[uint64]7;channel_id=$channel.channel_id;expected_state='on_air';source_commit=('a'*40);candidate_manifest_sha256=('b'*64);installer_sha256=('c'*64);commissioning_result_sha256=('d'*64)}
Assert-Contract ((Test-TownLightBetaSoakConfigurationResult $configuration $station.station_id 7 $channel.channel_id 'on_air' ('a'*40) ('b'*64) ('c'*64) ('d'*64)) -ceq 'ok') 'exact configuration producer contract must pass.'
$configuration.installer_sha256=('e'*64)
Assert-Contract ((Test-TownLightBetaSoakConfigurationResult $configuration $station.station_id 7 $channel.channel_id 'on_air' ('a'*40) ('b'*64) ('c'*64) ('d'*64)) -ceq 'configuration_result_binding_mismatch') 'fabricated/unbound configuration result must fail.'

if(-not('TownLightBetaSoakMutexFixture' -as [type])){Add-Type @'
using System.Threading;
public static class TownLightBetaSoakMutexFixture {
 public static bool TryWait(Mutex m,int ms){bool r=false;var t=new Thread(()=>{r=m.WaitOne(ms);if(r)m.ReleaseMutex();});t.Start();t.Join();return r;}
 public static void Abandon(Mutex m){var t=new Thread(()=>{m.WaitOne();});t.Start();t.Join();}
}
'@}
$mutexName='Global\TownLightStation.InstalledBetaSoak.Fixture.'+[guid]::NewGuid().ToString('N')
$mutexInstalledGap=-not $InstalledElevatedMutexEvidence
$fence=Enter-TownLightBetaSoakFence -Name $mutexName -WaitMilliseconds 0
try{
 $acl=Get-TownLightBetaSoakMutexAccessControl $fence.mutex
 if($null -eq $acl){$acl=New-TownLightBetaSoakMutexSecurity}
 $aclRules=$acl.GetAccessRules($true,$false,[Security.Principal.SecurityIdentifier])
 $aclSids=@($aclRules|ForEach-Object{$_.IdentityReference.Value}|Sort-Object)
 $fullRules=@($aclRules|Where-Object{($_.MutexRights -band [Security.AccessControl.MutexRights]::FullControl) -eq [Security.AccessControl.MutexRights]::FullControl -and $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow})
 Assert-Contract ($fence.created_new -and $acl.AreAccessRulesProtected -and @($aclRules).Count -eq 2 -and $fullRules.Count -eq 2 -and ($aclSids -join '|') -ceq 'S-1-5-18|S-1-5-32-544') 'production fence must create unique protected SYSTEM/Admin authority.'
 $ps=[PowerShell]::Create();try{[void]$ps.AddScript(". '$($statePath.Replace("'","''"))'; try { Enter-TownLightBetaSoakFence -Name '$mutexName' -WaitMilliseconds 0 | Out-Null; 'unexpected' } catch { `$_.Exception.Message }");$async=$ps.BeginInvoke();$result=$ps.EndInvoke($async);$refusal=[string]@($result)[-1];if($InstalledElevatedMutexEvidence){Assert-Contract ($refusal -match '^beta_soak_fence_busy$') 'elevated installed evidence requires exact busy refusal.'}else{Assert-Contract ($refusal -match 'beta_soak_fence_busy|Access.*denied') 'ordinary verifier requires a safe same-name refusal.'}}finally{$ps.Dispose()}
}finally{Exit-TownLightBetaSoakFence $fence}
$abandonName='Global\TownLightStation.InstalledBetaSoak.Abandon.'+[guid]::NewGuid().ToString('N');$abandonHandle=New-TownLightBetaSoakMutexHandle -Name $abandonName;$am=$abandonHandle.mutex
try{[TownLightBetaSoakMutexFixture]::Abandon($am);$outcome='unexpected';try{Enter-TownLightBetaSoakFence -Name $abandonName -WaitMilliseconds 0|Out-Null}catch{$outcome=$_.Exception.Message};if($InstalledElevatedMutexEvidence){Assert-Contract ($outcome -match '^beta_soak_fence_abandoned$') 'elevated installed evidence requires exact production abandonment outcome.'}else{Assert-Contract ($outcome -match 'beta_soak_fence_abandoned|Access.*denied') 'ordinary verifier requires abandonment to fail safely.'}}finally{$am.Dispose()}

$fixture=Join-Path ([IO.Path]::GetTempPath()) ('townlight-beta-soak-fixture-'+[guid]::NewGuid().ToString('N'))
try{New-Item -ItemType Directory -Path $fixture|Out-Null;foreach($phase in @('close','hash')){$fp=Join-Path $fixture "failure-$phase.json";$close={};$finalize={};if($phase -eq 'close'){$close={throw 'forced_close_failure'}}else{$finalize={throw 'forced_hash_failure'}};$finalization=Invoke-TownLightBetaSoakFinalizationStateMachine -CloseEvidence $close -FinalizeEvidence $finalize -PersistFailure { param($detail) Write-TownLightBetaSoakFailureReceipt $fp '33333333-3333-4333-8333-333333333333' $detail|Out-Null };$before=Get-Content $fp -Raw;$overwrite=$false;try{Write-TownLightBetaSoakFailureReceipt $fp '33333333-3333-4333-8333-333333333333' 'overwrite'|Out-Null}catch{$overwrite=$true};Assert-Contract (-not $finalization.passed -and $overwrite -and (Get-Content $fp -Raw) -ceq $before) "production $phase finalization failure must preserve truth and reject overwrite."}}finally{if(Test-Path $fixture){Remove-Item -LiteralPath $fixture -Recurse -Force}}
$gap=if($mutexInstalledGap){' Elevated installed mutex evidence was not requested; run with -InstalledElevatedMutexEvidence from an elevated Administrator/LocalSystem context to require exact busy and abandoned outcomes.'}else{' Elevated installed mutex evidence passed.'}
Write-Output "Installed beta-soak proof harness passed ($assertions assertions; static/fixture-only, no service, process, installer, Sandbox, or API activity).$gap"
