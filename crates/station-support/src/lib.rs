use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use station_domain::{
    AlertConditionKind, AlertDelivery, AlertDeliveryState, AlertEvent, AlertEventState,
    AlertNotificationKind, AlertSeverity, ChannelRuntimeObservation, RuntimeSafeToAirStatus,
    SelfTestKind, SelfTestStatus, SelfTestTrigger, derive_runtime_safe_to_air,
};
use station_maintenance::{
    ACTIVE_SLOT_RECEIPT_NAME, MaintenancePhase, latest_maintenance_transaction,
    read_active_slot_receipt,
};
use station_storage::StationStore;
use zip::CompressionMethod;
use zip::write::SimpleFileOptions;

const PRODUCT_NAME: &str = "TownLight Station";
const SCHEMA_VERSION: u32 = 1;
const RESOURCE_LIMIT: u32 = 120;
const ALERT_LIMIT: u32 = 100;
const SELF_TEST_LIMIT: u32 = 100;
const RETAINED_BUNDLES: usize = 10;
const MAX_BUNDLE_BYTES: u64 = 16 * 1024 * 1024;
static BUNDLE_NONCE: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SupportBundleEntry {
    pub name: String,
    pub size_bytes: u64,
    pub sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SupportBundleManifest {
    pub schema_version: u32,
    pub product: String,
    pub bundle_id: String,
    pub created_at_unix_ms: i64,
    pub source_commit: Option<String>,
    pub entries: Vec<SupportBundleEntry>,
    pub excludes: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SupportBundleRecord {
    pub bundle_id: String,
    pub created_at_unix_ms: i64,
    pub file_name: String,
    pub path: String,
    pub download_uri: String,
    pub sha256: String,
    pub size_bytes: u64,
    pub entries: Vec<String>,
    pub contains: Vec<String>,
    pub excludes: Vec<String>,
    pub retention_warning: Option<String>,
}

#[derive(Debug, Serialize)]
struct InstallationSnapshot {
    product: String,
    version: Option<String>,
    source_commit: Option<String>,
    media_runtime_sha256: Option<String>,
    installed_local_time: Option<String>,
    active_relative_directory: Option<String>,
    active_generation: Option<u64>,
    operating_system: &'static str,
    architecture: &'static str,
    artifacts: Vec<InstalledArtifact>,
}

#[derive(Debug, Serialize)]
struct MaintenanceDiagnostic {
    transaction_id: String,
    requested_at_unix_ms: i64,
    requested_by: String,
    updated_at_unix_ms: i64,
    phase: MaintenancePhase,
    from_version: String,
    candidate_version: String,
    summary: &'static str,
}

#[derive(Debug, Deserialize)]
struct InstallReceipt {
    #[serde(default)]
    product: Option<String>,
    #[serde(default)]
    version: Option<String>,
    #[serde(default)]
    source_commit: Option<String>,
    #[serde(default)]
    media_runtime_sha256: Option<String>,
    #[serde(default)]
    installed_local_time: Option<String>,
}

#[derive(Debug, Serialize)]
struct InstalledArtifact {
    name: String,
    present: bool,
    size_bytes: Option<u64>,
    sha256: Option<String>,
}

#[derive(Debug, Serialize)]
struct RuntimeSnapshot {
    status: RuntimeSafeToAirStatus,
    observations: Vec<ChannelRuntimeObservation>,
}

#[derive(Debug, Serialize)]
struct RedactedAlertEvent {
    event_id: String,
    condition: AlertConditionKind,
    severity: AlertSeverity,
    state: AlertEventState,
    resource_ref: String,
    summary: String,
    detail_present: bool,
    source_section: String,
    first_observed_at_unix_ms: i64,
    last_observed_at_unix_ms: i64,
    resolved_at_unix_ms: Option<i64>,
    occurrence_count: u64,
    acknowledged: bool,
}

impl From<AlertEvent> for RedactedAlertEvent {
    fn from(event: AlertEvent) -> Self {
        Self {
            event_id: event.event_id,
            condition: event.condition,
            severity: event.severity,
            state: event.state,
            resource_ref: event.resource_ref,
            summary: event.summary,
            detail_present: !event.detail.is_empty(),
            source_section: event.source_section,
            first_observed_at_unix_ms: event.first_observed_at_unix_ms,
            last_observed_at_unix_ms: event.last_observed_at_unix_ms,
            resolved_at_unix_ms: event.resolved_at_unix_ms,
            occurrence_count: event.occurrence_count,
            acknowledged: event.acknowledged_at_unix_ms.is_some(),
        }
    }
}

#[derive(Debug, Serialize)]
struct RedactedAlertDelivery {
    delivery_id: String,
    event_id: String,
    alert_channel_id: String,
    notification_kind: AlertNotificationKind,
    state: AlertDeliveryState,
    attempts: u32,
    next_attempt_at_unix_ms: i64,
    lease_expires_at_unix_ms: Option<i64>,
    failure_detail_present: bool,
    created_at_unix_ms: i64,
    updated_at_unix_ms: i64,
    sent_at_unix_ms: Option<i64>,
}

impl From<AlertDelivery> for RedactedAlertDelivery {
    fn from(delivery: AlertDelivery) -> Self {
        Self {
            delivery_id: delivery.delivery_id,
            event_id: delivery.event_id,
            alert_channel_id: delivery.alert_channel_id,
            notification_kind: delivery.notification_kind,
            state: delivery.state,
            attempts: delivery.attempts,
            next_attempt_at_unix_ms: delivery.next_attempt_at_unix_ms,
            lease_expires_at_unix_ms: delivery.lease_expires_at_unix_ms,
            failure_detail_present: !delivery.last_error.is_empty(),
            created_at_unix_ms: delivery.created_at_unix_ms,
            updated_at_unix_ms: delivery.updated_at_unix_ms,
            sent_at_unix_ms: delivery.sent_at_unix_ms,
        }
    }
}

#[derive(Debug, Serialize)]
struct RedactedSelfTest {
    self_test_id: String,
    kind: SelfTestKind,
    trigger: SelfTestTrigger,
    schedule_period_key: Option<String>,
    started_at_unix_ms: i64,
    finished_at_unix_ms: i64,
    status: SelfTestStatus,
    checks: BTreeMap<String, bool>,
    summary: String,
    evidence_file_name: Option<String>,
}

pub fn create_support_bundle(
    database_path: &Path,
    application_directory: &Path,
    created_at_unix_ms: i64,
) -> Result<SupportBundleRecord, String> {
    if created_at_unix_ms < 0 {
        return Err("support bundle time must be nonnegative".into());
    }
    let data_directory = database_path
        .parent()
        .ok_or_else(|| "station database has no parent directory".to_string())?;
    let store = StationStore::open(database_path)?;
    let install = read_installation_snapshot(data_directory, application_directory)?;
    let source_commit = install.source_commit.clone();
    let channels = store.list_channels()?;
    let observations = store.list_channel_runtime()?;
    let active_alerts = store.list_alert_events()?;
    let runtime_channels = channels
        .iter()
        .filter(|channel| channel.enabled)
        .map(|channel| {
            (
                channel.channel_id.clone(),
                channel.display_name.clone(),
                observations
                    .iter()
                    .find(|observation| observation.channel_id == channel.channel_id)
                    .cloned(),
            )
        })
        .collect::<Vec<_>>();
    let active_critical = active_alerts
        .iter()
        .filter(|event| {
            event.state == AlertEventState::Firing && event.severity == AlertSeverity::Critical
        })
        .count();
    let active_warning = active_alerts
        .iter()
        .filter(|event| {
            event.state == AlertEventState::Firing && event.severity == AlertSeverity::Warning
        })
        .count();
    let runtime_at_unix_ms = observations
        .iter()
        .fold(created_at_unix_ms, |latest, observation| {
            latest
                .max(observation.state_started_at_unix_ms)
                .max(observation.heartbeat_at_unix_ms)
        });
    let runtime = RuntimeSnapshot {
        status: derive_runtime_safe_to_air(
            runtime_at_unix_ms,
            &runtime_channels,
            active_critical,
            active_warning,
        )
        .map_err(str::to_string)?,
        observations,
    };

    let recent_alerts = store
        .list_recent_alert_events(ALERT_LIMIT)?
        .into_iter()
        .map(RedactedAlertEvent::from)
        .collect::<Vec<_>>();
    let recent_deliveries = store
        .list_recent_alert_deliveries(ALERT_LIMIT)?
        .into_iter()
        .map(RedactedAlertDelivery::from)
        .collect::<Vec<_>>();
    let self_tests = store
        .list_system_self_tests(SELF_TEST_LIMIT)?
        .into_iter()
        .map(|record| RedactedSelfTest {
            self_test_id: record.self_test_id,
            kind: record.kind,
            trigger: record.trigger,
            schedule_period_key: record.schedule_period_key,
            started_at_unix_ms: record.started_at_unix_ms,
            finished_at_unix_ms: record.finished_at_unix_ms,
            status: record.status,
            checks: record.checks,
            summary: record.summary,
            evidence_file_name: record.evidence_path.as_deref().and_then(|path| {
                Path::new(path)
                    .file_name()
                    .map(|name| name.to_string_lossy().into_owned())
            }),
        })
        .collect::<Vec<_>>();
    let maintenance =
        latest_maintenance_transaction(data_directory)?.map(|transaction| MaintenanceDiagnostic {
            transaction_id: transaction.request.transaction_id,
            requested_at_unix_ms: transaction.request.requested_at_unix_ms,
            requested_by: transaction.request.requested_by,
            updated_at_unix_ms: transaction.updated_at_unix_ms,
            phase: transaction.phase,
            from_version: transaction.previous_receipt.active.package_manifest.version,
            candidate_version: transaction.candidate.package_manifest.version,
            summary: maintenance_phase_summary(transaction.phase),
        });

    let mut documents = BTreeMap::new();
    insert_json(&mut documents, "installation.json", &install)?;
    insert_json(
        &mut documents,
        "station-profile.json",
        &store.read_profile()?,
    )?;
    insert_json(&mut documents, "channels.json", &channels)?;
    insert_json(&mut documents, "runtime-safe-to-air.json", &runtime)?;
    insert_json(
        &mut documents,
        "alert-rules.json",
        &store.list_alert_rules()?,
    )?;
    insert_json(
        &mut documents,
        "alert-channels-redacted.json",
        &store.list_alert_channels()?,
    )?;
    insert_json(
        &mut documents,
        "recent-alerts-redacted.json",
        &recent_alerts,
    )?;
    insert_json(
        &mut documents,
        "recent-alert-deliveries-redacted.json",
        &recent_deliveries,
    )?;
    insert_json(
        &mut documents,
        "recent-system-resources.json",
        &store.list_system_resource_samples(RESOURCE_LIMIT)?,
    )?;
    insert_json(&mut documents, "self-tests-redacted.json", &self_tests)?;
    insert_json(&mut documents, "maintenance-latest.json", &maintenance)?;
    insert_json(
        &mut documents,
        "self-test-schedules.json",
        &store.list_self_test_schedules()?,
    )?;

    let bundle_id = format!(
        "support-bundle-{created_at_unix_ms}-{}",
        BUNDLE_NONCE.fetch_add(1, Ordering::Relaxed)
    );
    let excludes = exclusion_contract();
    let entry_manifest = documents
        .iter()
        .map(|(name, bytes)| SupportBundleEntry {
            name: name.clone(),
            size_bytes: u64::try_from(bytes.len()).unwrap_or(u64::MAX),
            sha256: sha256_bytes(bytes),
        })
        .collect::<Vec<_>>();
    let manifest = SupportBundleManifest {
        schema_version: SCHEMA_VERSION,
        product: PRODUCT_NAME.into(),
        bundle_id: bundle_id.clone(),
        created_at_unix_ms,
        source_commit,
        entries: entry_manifest,
        excludes: excludes.clone(),
    };
    insert_json(&mut documents, "manifest.json", &manifest)?;

    let bundle_directory = data_directory.join("support-bundles");
    std::fs::create_dir_all(&bundle_directory)
        .map_err(|error| format!("could not create support bundle directory: {error}"))?;
    let file_name = format!("{bundle_id}.zip");
    let final_path = bundle_directory.join(&file_name);
    let temporary_path = bundle_directory.join(format!("{file_name}.tmp"));
    let publish = write_zip_atomically(&temporary_path, &final_path, &documents);
    if let Err(error) = publish {
        let _ = std::fs::remove_file(&temporary_path);
        return Err(error);
    }
    let size_bytes = std::fs::metadata(&final_path)
        .map_err(|error| format!("could not inspect support bundle: {error}"))?
        .len();
    if size_bytes > MAX_BUNDLE_BYTES {
        let _ = std::fs::remove_file(&final_path);
        return Err("support bundle exceeded the 16 MiB safety limit".into());
    }
    let sha256 = sha256_file(&final_path)?;
    let retention_warning = retain_recent_bundles(&bundle_directory, RETAINED_BUNDLES).err();
    let mut entries = documents.keys().cloned().collect::<Vec<_>>();
    entries.sort();
    Ok(SupportBundleRecord {
        bundle_id: bundle_id.clone(),
        created_at_unix_ms,
        file_name,
        path: final_path.to_string_lossy().into_owned(),
        download_uri: format!("/api/v1/support-bundles/{bundle_id}/download"),
        sha256,
        size_bytes,
        entries,
        contains: vec![
            "installation identity and artifact hashes".into(),
            "station profile, channel configuration, and current runtime truth".into(),
            "bounded alert, delivery, resource, and self-test history".into(),
            "latest durable maintenance transaction and active application identity".into(),
        ],
        excludes,
        retention_warning,
    })
}

pub fn read_support_bundle(database_path: &Path, bundle_id: &str) -> Result<Vec<u8>, String> {
    if !valid_bundle_id(bundle_id) {
        return Err("support bundle identity is invalid".into());
    }
    let data_directory = database_path
        .parent()
        .ok_or_else(|| "station database has no parent directory".to_string())?;
    let path = data_directory
        .join("support-bundles")
        .join(format!("{bundle_id}.zip"));
    let metadata =
        std::fs::metadata(&path).map_err(|_| "support bundle does not exist".to_string())?;
    if !metadata.is_file() || metadata.len() > MAX_BUNDLE_BYTES {
        return Err("support bundle is not a readable bounded file".into());
    }
    std::fs::read(path).map_err(|error| format!("could not read support bundle: {error}"))
}

fn read_installation_snapshot(
    data_directory: &Path,
    application_directory: &Path,
) -> Result<InstallationSnapshot, String> {
    let receipt_path = data_directory.join("install-receipt.json");
    let receipt = match std::fs::metadata(&receipt_path) {
        Ok(metadata) if metadata.is_file() && metadata.len() <= 16_384 => {
            let bytes = std::fs::read(&receipt_path)
                .map_err(|error| format!("could not read install receipt: {error}"))?;
            Some(
                serde_json::from_slice::<InstallReceipt>(&bytes)
                    .map_err(|error| format!("install receipt is invalid: {error}"))?,
            )
        }
        Ok(_) => return Err("install receipt is not a bounded regular file".into()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => return Err(format!("could not inspect install receipt: {error}")),
    };
    let active_path = data_directory.join(ACTIVE_SLOT_RECEIPT_NAME);
    let active = match std::fs::symlink_metadata(&active_path) {
        Ok(metadata) if metadata.is_file() && !metadata.file_type().is_symlink() => {
            Some(read_active_slot_receipt(&active_path)?)
        }
        Ok(_) => return Err("active-slot receipt is not a regular file".into()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => return Err(format!("could not inspect active-slot receipt: {error}")),
    };
    let payload_directory = active
        .as_ref()
        .map(|receipt| receipt.active.directory(application_directory))
        .transpose()?
        .unwrap_or_else(|| application_directory.to_path_buf());
    let artifacts = [
        ("stationd.exe", payload_directory.join("stationd.exe")),
        (
            "channel-worker.exe",
            payload_directory.join("channel-worker.exe"),
        ),
        (
            "station-updater.exe",
            application_directory.join("station-updater.exe"),
        ),
        (
            "private-media-runtime",
            application_directory.join("runtime/gstreamer/bin/gstreamer-1.0-0.dll"),
        ),
    ]
    .into_iter()
    .map(|(name, path)| installed_artifact(name, &path))
    .collect::<Result<Vec<_>, _>>()?;
    Ok(InstallationSnapshot {
        product: receipt
            .as_ref()
            .and_then(|receipt| receipt.product.clone())
            .unwrap_or_else(|| PRODUCT_NAME.into()),
        version: receipt.as_ref().and_then(|receipt| receipt.version.clone()),
        source_commit: receipt
            .as_ref()
            .and_then(|receipt| receipt.source_commit.clone()),
        media_runtime_sha256: receipt
            .as_ref()
            .and_then(|receipt| receipt.media_runtime_sha256.clone()),
        installed_local_time: receipt
            .as_ref()
            .and_then(|receipt| receipt.installed_local_time.clone()),
        active_relative_directory: active
            .as_ref()
            .map(|receipt| receipt.active.relative_directory.clone()),
        active_generation: active.as_ref().map(|receipt| receipt.generation),
        operating_system: std::env::consts::OS,
        architecture: std::env::consts::ARCH,
        artifacts,
    })
}

fn maintenance_phase_summary(phase: MaintenancePhase) -> &'static str {
    match phase {
        MaintenancePhase::Requested => "Activation was requested and is awaiting the watchdog.",
        MaintenancePhase::ServiceStopped => "The station service was stopped for activation.",
        MaintenancePhase::CandidateConfigured => "The candidate service was configured.",
        MaintenancePhase::CandidateStarted => "The candidate service was started for proof.",
        MaintenancePhase::CommitStarted => "Candidate proof passed and commit began.",
        MaintenancePhase::Committed => "The candidate was proved and committed.",
        MaintenancePhase::RollbackStarted => "Candidate proof failed and rollback began.",
        MaintenancePhase::RolledBack => "The previous application was restored and proved.",
        MaintenancePhase::RollbackFailed => {
            "Candidate proof and automatic rollback proof both failed; local inspection is required."
        }
    }
}

fn installed_artifact(name: &str, path: &Path) -> Result<InstalledArtifact, String> {
    match std::fs::metadata(path) {
        Ok(metadata) if metadata.is_file() => Ok(InstalledArtifact {
            name: name.into(),
            present: true,
            size_bytes: Some(metadata.len()),
            sha256: Some(sha256_file(path)?),
        }),
        Ok(_) => Err(format!("installed artifact is not a regular file: {name}")),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(InstalledArtifact {
            name: name.into(),
            present: false,
            size_bytes: None,
            sha256: None,
        }),
        Err(error) => Err(format!(
            "could not inspect installed artifact {name}: {error}"
        )),
    }
}

fn insert_json<T: Serialize>(
    documents: &mut BTreeMap<String, Vec<u8>>,
    name: &str,
    value: &T,
) -> Result<(), String> {
    let mut bytes = serde_json::to_vec_pretty(value)
        .map_err(|error| format!("could not encode support document {name}: {error}"))?;
    bytes.push(b'\n');
    documents.insert(name.into(), bytes);
    Ok(())
}

fn write_zip_atomically(
    temporary_path: &Path,
    final_path: &Path,
    documents: &BTreeMap<String, Vec<u8>>,
) -> Result<(), String> {
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(temporary_path)
        .map_err(|error| format!("could not create support bundle: {error}"))?;
    let mut writer = zip::ZipWriter::new(file);
    let options = SimpleFileOptions::default()
        .compression_method(CompressionMethod::Stored)
        .unix_permissions(0o600);
    for (name, bytes) in documents {
        writer
            .start_file(name, options)
            .map_err(|error| format!("could not add {name} to support bundle: {error}"))?;
        writer
            .write_all(bytes)
            .map_err(|error| format!("could not write {name} to support bundle: {error}"))?;
    }
    let file = writer
        .finish()
        .map_err(|error| format!("could not finalize support bundle: {error}"))?;
    file.sync_all()
        .map_err(|error| format!("could not durably sync support bundle: {error}"))?;
    drop(file);
    std::fs::rename(temporary_path, final_path)
        .map_err(|error| format!("could not publish support bundle: {error}"))
}

fn retain_recent_bundles(directory: &Path, retain: usize) -> Result<(), String> {
    let mut bundles = std::fs::read_dir(directory)
        .map_err(|error| format!("could not list support bundles: {error}"))?
        .filter_map(Result::ok)
        .filter(|entry| {
            entry.file_type().is_ok_and(|kind| kind.is_file())
                && entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with("support-bundle-")
                && entry
                    .path()
                    .extension()
                    .is_some_and(|extension| extension == "zip")
        })
        .collect::<Vec<_>>();
    bundles.sort_by_key(|entry| std::cmp::Reverse(entry.file_name()));
    for entry in bundles.into_iter().skip(retain) {
        std::fs::remove_file(entry.path())
            .map_err(|error| format!("could not remove expired support bundle: {error}"))?;
    }
    Ok(())
}

fn exclusion_contract() -> Vec<String> {
    vec![
        "station database and database sidecars".into(),
        "encrypted or plaintext credentials, passwords, tokens, and signing keys".into(),
        "alert payloads, HMAC signatures, and raw delivery errors".into(),
        "full media paths, raw environment variables, and unrestricted logs".into(),
        "resident or subscriber data".into(),
    ]
}

fn valid_bundle_id(value: &str) -> bool {
    value.starts_with("support-bundle-")
        && (18..=120).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
}

fn sha256_bytes(bytes: &[u8]) -> String {
    format!("{:X}", Sha256::digest(bytes))
}

fn sha256_file(path: &Path) -> Result<String, String> {
    let mut file = File::open(path).map_err(|error| format!("could not hash file: {error}"))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|error| format!("could not hash file: {error}"))?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(format!("{:X}", hasher.finalize()))
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::io::Read;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    use station_domain::{
        AlertChannel, AlertChannelKind, ChannelRuntimeObservation, ChannelRuntimeState,
        StationProfile, SystemResourceSample, redact_alert_channel_target,
    };
    use station_schedule::{ChannelConfiguration, OutputSinkConfiguration};
    use station_storage::{AlertChannelSecret, StationStore};

    use super::*;

    fn temp_directory(name: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!("townlight-support-{name}-{nonce}"))
    }

    #[test]
    fn creates_a_bounded_manifested_bundle_without_the_stored_secret_or_database() {
        let root = temp_directory("redaction");
        let data = root.join("data");
        let app = root.join("app");
        std::fs::create_dir_all(&data).unwrap();
        std::fs::create_dir_all(app.join("runtime/gstreamer/bin")).unwrap();
        std::fs::write(app.join("stationd.exe"), b"station-service").unwrap();
        std::fs::write(app.join("channel-worker.exe"), b"channel-worker").unwrap();
        std::fs::write(
            app.join("runtime/gstreamer/bin/gstreamer-1.0-0.dll"),
            b"runtime-core",
        )
        .unwrap();
        std::fs::write(
            data.join("install-receipt.json"),
            br#"{"product":"TownLight Station","version":"0.1.0","source_commit":"0123456789abcdef0123456789abcdef01234567","media_runtime_sha256":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","installed_local_time":"2026-08-21 04:00:00","ignored":"must-not-export"}"#,
        )
        .unwrap();
        let database = data.join("station.db");
        let store = StationStore::open(&database).unwrap();
        store
            .write_profile(
                &StationProfile {
                    station_id: "10101010-2020-4030-8040-505050505050".into(),
                    display_name: "Support proof station".into(),
                    timezone: "America/Denver".into(),
                },
                0,
            )
            .unwrap();
        let channel_id = "60606060-7070-4080-8090-a0a0a0a0a0a0";
        let srt_sink_id = "61616161-7171-4181-8191-a1a1a1a1a1a1";
        let mut srt_sink = OutputSinkConfiguration::required_srt(
            srt_sink_id,
            "Encrypted contribution",
            "srt://127.0.0.1:5799",
            -16.0,
        );
        srt_sink.encryption_configured = true;
        let srt_canary = "NEVER-EXPORT-SRT-PASSPHRASE-84126";
        let mut srt_passphrases = HashMap::new();
        srt_passphrases.insert(srt_sink_id.into(), srt_canary.into());
        store
            .put_channel_with_srt_passphrases(
                &ChannelConfiguration {
                    channel_id: channel_id.into(),
                    display_name: "Government".into(),
                    output_profile: Default::default(),
                    output_sinks: vec![
                        OutputSinkConfiguration::required_udp(
                            channel_id,
                            "Primary transport",
                            "127.0.0.1:5798",
                            -16.0,
                        ),
                        srt_sink,
                    ],
                    enabled: true,
                },
                &srt_passphrases,
            )
            .unwrap();
        store
            .put_channel_runtime(&ChannelRuntimeObservation {
                channel_id: channel_id.into(),
                state: ChannelRuntimeState::HealthyFallback,
                on_air_asset_id: None,
                state_started_at_unix_ms: 1_000,
                heartbeat_at_unix_ms: 2_000,
                detail: None,
            })
            .unwrap();
        store
            .record_system_resource_sample(&SystemResourceSample {
                sample_id: None,
                sampled_at_unix_ms: 2_000,
                cpu_percent: Some(10.0),
                ram_used_gb: Some(4.0),
                ram_total_gb: Some(16.0),
                gpu_percent: None,
                gpu_vram_used_gb: None,
                media_volume_free_gb: Some(100.0),
                backup_volume_free_gb: Some(100.0),
                database_reachable: true,
                backup_volume_writable: true,
                service_running: true,
                clock_skew_seconds: None,
            })
            .unwrap();
        let canary = "NEVER-EXPORT-THIS-CREDENTIAL-92741";
        let target = "http://127.0.0.1:47890/support";
        store
            .put_alert_channel(
                &AlertChannel {
                    channel_id: "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff".into(),
                    kind: AlertChannelKind::Webhook,
                    label: "Proof pager".into(),
                    enabled: true,
                    target_redacted: redact_alert_channel_target(AlertChannelKind::Webhook, target)
                        .unwrap(),
                    credential_configured: true,
                    quiet_hours_start_utc: None,
                    quiet_hours_end_utc: None,
                    created_at_unix_ms: 1_000,
                    updated_at_unix_ms: 1_000,
                },
                &AlertChannelSecret {
                    target: target.into(),
                    endpoint: None,
                    sender: None,
                    identity: None,
                    credential: canary.into(),
                },
            )
            .unwrap();
        drop(store);

        let bundle = create_support_bundle(&database, &app, 2_500).unwrap();
        let bytes = read_support_bundle(&database, &bundle.bundle_id).unwrap();
        assert_eq!(bundle.sha256, sha256_bytes(&bytes));
        assert!(bundle.size_bytes <= MAX_BUNDLE_BYTES);
        assert!(!String::from_utf8_lossy(&bytes).contains(canary));
        assert!(!String::from_utf8_lossy(&bytes).contains(srt_canary));
        assert!(!String::from_utf8_lossy(&bytes).contains("must-not-export"));
        assert!(!String::from_utf8_lossy(&bytes).contains(target));

        let mut archive = zip::ZipArchive::new(std::io::Cursor::new(bytes)).unwrap();
        let names = (0..archive.len())
            .map(|index| archive.by_index(index).unwrap().name().to_string())
            .collect::<Vec<_>>();
        assert!(names.contains(&"manifest.json".into()));
        assert!(names.contains(&"runtime-safe-to-air.json".into()));
        assert!(names.contains(&"alert-channels-redacted.json".into()));
        assert!(names.contains(&"maintenance-latest.json".into()));
        assert!(!names.iter().any(|name| name.ends_with(".db")));
        let mut manifest_json = String::new();
        archive
            .by_name("manifest.json")
            .unwrap()
            .read_to_string(&mut manifest_json)
            .unwrap();
        let manifest: SupportBundleManifest = serde_json::from_str(&manifest_json).unwrap();
        assert_eq!(manifest.bundle_id, bundle.bundle_id);
        assert_eq!(manifest.entries.len() + 1, names.len());
        for entry in &manifest.entries {
            let mut entry_bytes = Vec::new();
            archive
                .by_name(&entry.name)
                .unwrap()
                .read_to_end(&mut entry_bytes)
                .unwrap();
            assert_eq!(entry.size_bytes, entry_bytes.len() as u64);
            assert_eq!(entry.sha256, sha256_bytes(&entry_bytes));
        }
        assert!(
            manifest
                .excludes
                .iter()
                .any(|item| item.contains("credentials"))
        );
        assert!(read_support_bundle(&database, "support-bundle-..-station").is_err());

        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn hashes_slot_payloads_but_the_installer_owned_root_updater() {
        let root = temp_directory("active-artifacts");
        let data = root.join("data");
        let app = root.join("app");
        let slot = app.join("slots/slot-0.2.0-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
        std::fs::create_dir_all(&data).unwrap();
        std::fs::create_dir_all(app.join("runtime/gstreamer/bin")).unwrap();
        std::fs::create_dir_all(&slot).unwrap();
        std::fs::write(slot.join("stationd.exe"), b"active-stationd").unwrap();
        std::fs::write(slot.join("channel-worker.exe"), b"active-worker").unwrap();
        std::fs::write(slot.join("station-updater.exe"), b"dormant-slot-updater").unwrap();
        std::fs::write(app.join("station-updater.exe"), b"running-root-updater").unwrap();
        std::fs::write(
            app.join("runtime/gstreamer/bin/gstreamer-1.0-0.dll"),
            b"runtime-core",
        )
        .unwrap();
        let receipt = serde_json::json!({
            "schema_version": station_maintenance::ACTIVE_SLOT_SCHEMA_VERSION,
            "generation": 2,
            "activated_at_unix_ms": 2_000,
            "transaction_id": "update-support-proof",
            "active": {
                "relative_directory":
                    "slots/slot-0.2.0-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "package_manifest": {
                    "schema_version": station_update::UPDATE_PACKAGE_SCHEMA_VERSION,
                    "product": "TownLight Station",
                    "version": "0.2.0",
                    "source_commit": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "files": [
                        {
                            "path": "stationd.exe",
                            "size_bytes": b"active-stationd".len(),
                            "sha256": sha256_bytes(b"active-stationd")
                        },
                        {
                            "path": "channel-worker.exe",
                            "size_bytes": b"active-worker".len(),
                            "sha256": sha256_bytes(b"active-worker")
                        }
                    ]
                }
            },
            "previous": null
        });
        std::fs::write(
            data.join(ACTIVE_SLOT_RECEIPT_NAME),
            serde_json::to_vec(&receipt).unwrap(),
        )
        .unwrap();

        let snapshot = read_installation_snapshot(&data, &app).unwrap();
        assert_eq!(
            snapshot.active_relative_directory.as_deref(),
            Some("slots/slot-0.2.0-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        );
        let hashes = snapshot
            .artifacts
            .iter()
            .map(|artifact| (artifact.name.as_str(), artifact.sha256.as_deref()))
            .collect::<std::collections::BTreeMap<_, _>>();
        let stationd_hash = sha256_bytes(b"active-stationd");
        let root_updater_hash = sha256_bytes(b"running-root-updater");
        let dormant_updater_hash = sha256_bytes(b"dormant-slot-updater");
        assert_eq!(hashes["stationd.exe"], Some(stationd_hash.as_str()));
        assert_eq!(
            hashes["station-updater.exe"],
            Some(root_updater_hash.as_str())
        );
        assert_ne!(
            hashes["station-updater.exe"],
            Some(dormant_updater_hash.as_str())
        );

        std::fs::remove_dir_all(root).unwrap();
    }
}
