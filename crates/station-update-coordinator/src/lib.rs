use semver::Version;
use serde::{Deserialize, Serialize};
use station_domain::{
    AlertConditionKind, AlertEvent, AlertEventState, AlertSeverity, SafeToAirColor,
    derive_runtime_safe_to_air,
};
use station_maintenance::{
    MAINTENANCE_SCHEMA_VERSION, MaintenancePhase, MaintenanceRequest, enqueue_maintenance_request,
    latest_maintenance_transaction, pending_request_paths,
};
use station_storage::StationStore;
use station_update::{
    PreparedSlotRecord, ReleaseSource, StagedPackageRecord, UpdateCheckOutcome, UpdateCheckRecord,
    UpdateCheckTrigger, fetch_and_verify_release, prepare_release_slot, stage_release_package,
    staged_package_path, verify_prepared_slot,
};
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::Duration;
use std::time::{SystemTime, UNIX_EPOCH};

const TRUST_FILE_NAME: &str = "release-trust.json";
const RECEIPT_FILE_NAME: &str = "install-receipt.json";
const MAX_CONFIGURATION_BYTES: u64 = 64 * 1024;
const MAX_RECEIPT_BYTES: u64 = 16 * 1024;
pub const AUTOMATIC_CHECK_INTERVAL_MS: i64 = 60 * 60 * 1_000;
const CHECK_TIMEOUT: Duration = Duration::from_secs(10);
const PACKAGE_DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(10 * 60);

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct UpdateStatus {
    pub configured: bool,
    pub current_version: String,
    pub source_commit: String,
    pub release_channel: Option<String>,
    pub feed_origin: Option<String>,
    pub latest_check: Option<UpdateCheckRecord>,
    pub staged_package: Option<StagedPackageRecord>,
    pub prepared_slot: Option<PreparedSlotRecord>,
    pub maintenance: Option<MaintenanceStatus>,
    pub next_step: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct MaintenanceStatus {
    pub transaction_id: String,
    pub requested_at_unix_ms: i64,
    pub requested_by: String,
    pub updated_at_unix_ms: i64,
    pub phase: MaintenancePhase,
    pub from_version: String,
    pub candidate_version: String,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CoordinatorError {
    NotConfigured,
    NoAvailableRelease,
    NoStagedRelease,
    NoPreparedRelease,
    MaintenanceAlreadyQueued,
    MaintenanceGate(String),
    Fault(String),
}

impl std::fmt::Display for CoordinatorError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotConfigured => formatter.write_str(
                "No signed TownLight Station release source is installed on this station.",
            ),
            Self::NoAvailableRelease => formatter
                .write_str("No current verified release is available to stage on this station."),
            Self::NoStagedRelease => formatter.write_str(
                "No current verified package is staged and available to prepare on this station.",
            ),
            Self::NoPreparedRelease => formatter.write_str(
                "No current verified application slot is prepared for maintenance activation.",
            ),
            Self::MaintenanceAlreadyQueued => {
                formatter.write_str("A maintenance activation request is already pending.")
            }
            Self::MaintenanceGate(message) => formatter.write_str(message),
            Self::Fault(message) => formatter.write_str(message),
        }
    }
}

pub fn request_maintenance_activation(
    database_path: &Path,
    application_directory: &Path,
    requested_by: &str,
    confirmation: &str,
    now_unix_ms: i64,
) -> Result<MaintenanceRequest, CoordinatorError> {
    if confirmation != "INSTALL UPDATE" {
        return Err(CoordinatorError::MaintenanceGate(
            "Type INSTALL UPDATE exactly to authorize the maintenance interruption.".into(),
        ));
    }
    if requested_by.trim().is_empty()
        || requested_by.len() > 120
        || requested_by.chars().any(char::is_control)
    {
        return Err(CoordinatorError::MaintenanceGate(
            "requested_by must contain 1 to 120 visible characters.".into(),
        ));
    }
    if now_unix_ms < 0 {
        return Err(CoordinatorError::Fault(
            "maintenance request time must be nonnegative".into(),
        ));
    }

    let identity = read_install_identity(database_path)?;
    let source =
        read_release_source(application_directory)?.ok_or(CoordinatorError::NotConfigured)?;
    let store = StationStore::open(database_path).map_err(CoordinatorError::Fault)?;
    let offered = store
        .read_update_check()
        .map_err(CoordinatorError::Fault)?
        .filter(|check| {
            check.outcome == UpdateCheckOutcome::UpdateAvailable
                && check.current_version == identity.version
                && check.release_channel == source.release_channel
        })
        .and_then(|check| check.offered_release)
        .ok_or(CoordinatorError::NoPreparedRelease)?;
    let prepared = store
        .read_prepared_slot()
        .map_err(CoordinatorError::Fault)?
        .filter(|prepared| prepared.manifest == offered)
        .ok_or(CoordinatorError::NoPreparedRelease)?;
    if !source
        .trusted_keys
        .iter()
        .any(|key| key.key_id == prepared.manifest.key_id)
        || verify_prepared_slot(&slots_directory(application_directory), &prepared).is_err()
    {
        return Err(CoordinatorError::NoPreparedRelease);
    }
    let candidate_version = Version::parse(&prepared.manifest.version)
        .map_err(|_| CoordinatorError::NoPreparedRelease)?;
    let current_version =
        Version::parse(&identity.version).map_err(|_| CoordinatorError::NoPreparedRelease)?;
    if candidate_version <= current_version {
        return Err(CoordinatorError::NoPreparedRelease);
    }

    let channels = store.list_channels().map_err(CoordinatorError::Fault)?;
    let mut runtime_channels = Vec::new();
    for channel in channels.into_iter().filter(|channel| channel.enabled) {
        let observation = store
            .read_channel_runtime(&channel.channel_id)
            .map_err(CoordinatorError::Fault)?;
        runtime_channels.push((channel.channel_id, channel.display_name, observation));
    }
    let alerts = store.list_alert_events().map_err(CoordinatorError::Fault)?;
    let active_critical = alerts
        .iter()
        .filter(|event| {
            event.state == AlertEventState::Firing && event.severity == AlertSeverity::Critical
        })
        .count();
    let active_warning = alerts
        .iter()
        .filter(|event| {
            event.state == AlertEventState::Firing && event.severity == AlertSeverity::Warning
        })
        .count();
    let safe_to_air = derive_runtime_safe_to_air(
        now_unix_ms,
        &runtime_channels,
        active_critical,
        active_warning,
    )
    .map_err(|error| CoordinatorError::MaintenanceGate(error.into()))?;
    if safe_to_air.color != SafeToAirColor::Green {
        return Err(CoordinatorError::MaintenanceGate(format!(
            "Maintenance is blocked while runtime safe-to-air is {}: {}",
            match safe_to_air.color {
                SafeToAirColor::Green => "green",
                SafeToAirColor::Yellow => "yellow",
                SafeToAirColor::Red => "red",
            },
            safe_to_air.operator_message
        )));
    }

    let data_directory = database_path
        .parent()
        .ok_or_else(|| CoordinatorError::Fault("station database has no data directory".into()))?;
    if !pending_request_paths(data_directory)
        .map_err(CoordinatorError::Fault)?
        .is_empty()
    {
        return Err(CoordinatorError::MaintenanceAlreadyQueued);
    }
    let request = MaintenanceRequest {
        schema_version: MAINTENANCE_SCHEMA_VERSION,
        transaction_id: format!("update-{}-{now_unix_ms}", prepared.manifest.source_commit),
        requested_at_unix_ms: now_unix_ms,
        requested_by: requested_by.into(),
        prepared_slot: prepared,
    };
    enqueue_maintenance_request(data_directory, &request).map_err(|error| {
        if error == "a maintenance request is already pending" {
            CoordinatorError::MaintenanceAlreadyQueued
        } else {
            CoordinatorError::Fault(error)
        }
    })?;
    Ok(request)
}

pub fn read_status(
    database_path: &Path,
    application_directory: &Path,
) -> Result<UpdateStatus, CoordinatorError> {
    let identity = read_install_identity(database_path)?;
    let data_directory = database_path
        .parent()
        .ok_or_else(|| CoordinatorError::Fault("station database has no data directory".into()))?;
    let maintenance = latest_maintenance_transaction(data_directory)
        .map_err(CoordinatorError::Fault)?
        .map(|transaction| MaintenanceStatus {
            transaction_id: transaction.request.transaction_id,
            requested_at_unix_ms: transaction.request.requested_at_unix_ms,
            requested_by: transaction.request.requested_by,
            updated_at_unix_ms: transaction.updated_at_unix_ms,
            phase: transaction.phase,
            from_version: transaction.previous_receipt.active.package_manifest.version,
            candidate_version: transaction.candidate.package_manifest.version,
            detail: transaction.detail,
        });
    let source = read_release_source(application_directory)?;
    let stored_check = StationStore::open(database_path)
        .and_then(|store| store.read_update_check())
        .map_err(CoordinatorError::Fault)?;
    let store = StationStore::open(database_path).map_err(CoordinatorError::Fault)?;
    let staged = store
        .read_staged_package()
        .map_err(CoordinatorError::Fault)?;
    let prepared = store
        .read_prepared_slot()
        .map_err(CoordinatorError::Fault)?;
    let (configured, release_channel, feed_origin, latest_check, staged_package, prepared_slot, mut next_step) = match source {
        Some(source) => {
            let visible_stage = staged.filter(|stage| {
                stored_check
                    .as_ref()
                    .and_then(|check| check.offered_release.as_ref())
                    .is_some_and(|release| release == &stage.manifest)
                    && std::fs::metadata(staged_package_path(
                        &updates_directory(database_path),
                        stage,
                    ))
                    .is_ok_and(|metadata| {
                        metadata.is_file() && metadata.len() == stage.manifest.package_size_bytes
                    })
            });
            let visible_prepared = prepared.filter(|slot| {
                visible_stage.as_ref().is_some_and(|stage| {
                    stage.manifest == slot.manifest
                        && stage.package_file_name == slot.package_file_name
                }) && verify_prepared_slot(&slots_directory(application_directory), slot).is_ok()
            });
            let next_step = if visible_prepared.is_some() {
                "The verified application slot is prepared. Activation requires an explicit maintenance window."
            } else if visible_stage.is_some() {
                "The verified package is staged. Activation still requires an explicit maintenance window."
            } else {
                "Check for a signed update. Download and installation always require explicit operator action."
            };
            (
                true,
                Some(source.release_channel),
                Some(redact_feed_origin(&source.feed_url)),
                stored_check,
                visible_stage,
                visible_prepared,
                next_step.into(),
            )
        }
        None => (
            false,
            None,
            None,
            None,
            None,
            None,
            "Install a release trust file containing the official feed and public verification key."
                .into(),
        ),
    };
    if let Some(status) = &maintenance {
        next_step = match status.phase {
            MaintenancePhase::Requested
            | MaintenancePhase::ServiceStopped
            | MaintenancePhase::CandidateConfigured
            | MaintenancePhase::CandidateStarted
            | MaintenancePhase::CommitStarted
            | MaintenancePhase::RollbackStarted => {
                "A maintenance transaction is in progress under the independent watchdog.".into()
            }
            MaintenancePhase::RollbackFailed => {
                "MAINTENANCE RECOVERY FAILED. Keep the station out of unattended service and inspect the latest transaction immediately."
                    .into()
            }
            MaintenancePhase::Committed | MaintenancePhase::RolledBack => next_step,
        };
    }
    Ok(UpdateStatus {
        configured,
        current_version: identity.version,
        source_commit: identity.source_commit,
        release_channel,
        feed_origin,
        latest_check,
        staged_package,
        prepared_slot,
        maintenance,
        next_step,
    })
}

pub fn stage_available_release(
    database_path: &Path,
    application_directory: &Path,
    now_unix_ms: i64,
) -> Result<StagedPackageRecord, CoordinatorError> {
    let identity = read_install_identity(database_path)?;
    let source =
        read_release_source(application_directory)?.ok_or(CoordinatorError::NotConfigured)?;
    let store = StationStore::open(database_path).map_err(CoordinatorError::Fault)?;
    let check = store
        .read_update_check()
        .map_err(CoordinatorError::Fault)?
        .filter(|check| {
            check.outcome == UpdateCheckOutcome::UpdateAvailable
                && check.current_version == identity.version
                && check.release_channel == source.release_channel
        })
        .ok_or(CoordinatorError::NoAvailableRelease)?;
    let manifest = check
        .offered_release
        .ok_or(CoordinatorError::NoAvailableRelease)?;
    if !source
        .trusted_keys
        .iter()
        .any(|key| key.key_id == manifest.key_id)
    {
        return Err(CoordinatorError::NoAvailableRelease);
    }
    let staged = stage_release_package(
        &manifest,
        &updates_directory(database_path),
        now_unix_ms,
        PACKAGE_DOWNLOAD_TIMEOUT,
    )
    .map_err(CoordinatorError::Fault)?;
    let stored = store
        .write_staged_package(&staged)
        .map_err(CoordinatorError::Fault)?;
    if store
        .read_prepared_slot()
        .map_err(CoordinatorError::Fault)?
        .is_some_and(|prepared| prepared.manifest != stored.manifest)
    {
        store
            .clear_prepared_slot()
            .map_err(CoordinatorError::Fault)?;
    }
    Ok(stored)
}

pub fn prepare_staged_release(
    database_path: &Path,
    application_directory: &Path,
    now_unix_ms: i64,
) -> Result<PreparedSlotRecord, CoordinatorError> {
    let identity = read_install_identity(database_path)?;
    let source =
        read_release_source(application_directory)?.ok_or(CoordinatorError::NotConfigured)?;
    let store = StationStore::open(database_path).map_err(CoordinatorError::Fault)?;
    let check = store
        .read_update_check()
        .map_err(CoordinatorError::Fault)?
        .filter(|check| {
            check.outcome == UpdateCheckOutcome::UpdateAvailable
                && check.current_version == identity.version
                && check.release_channel == source.release_channel
        })
        .ok_or(CoordinatorError::NoStagedRelease)?;
    let offered = check
        .offered_release
        .ok_or(CoordinatorError::NoStagedRelease)?;
    let staged = store
        .read_staged_package()
        .map_err(CoordinatorError::Fault)?
        .filter(|staged| staged.manifest == offered)
        .ok_or(CoordinatorError::NoStagedRelease)?;
    if !source
        .trusted_keys
        .iter()
        .any(|key| key.key_id == staged.manifest.key_id)
    {
        return Err(CoordinatorError::NoStagedRelease);
    }
    let prepared = prepare_release_slot(
        &staged,
        &updates_directory(database_path),
        &slots_directory(application_directory),
        now_unix_ms,
    )
    .map_err(CoordinatorError::Fault)?;
    store
        .write_prepared_slot(&prepared)
        .map_err(CoordinatorError::Fault)
}

pub fn check_now(
    database_path: &Path,
    application_directory: &Path,
    trigger: UpdateCheckTrigger,
    now_unix_ms: i64,
) -> Result<UpdateCheckRecord, CoordinatorError> {
    let identity = read_install_identity(database_path)?;
    let source =
        read_release_source(application_directory)?.ok_or(CoordinatorError::NotConfigured)?;
    let store = StationStore::open(database_path).map_err(CoordinatorError::Fault)?;
    let previous = store.read_update_check().map_err(CoordinatorError::Fault)?;
    let result = fetch_and_verify_release(
        &source,
        &identity.version,
        env!("CARGO_PKG_VERSION"),
        now_unix_ms,
        CHECK_TIMEOUT,
    );
    let record = match result {
        Ok(verified) => UpdateCheckRecord {
            checked_at_unix_ms: now_unix_ms,
            next_automatic_check_at_unix_ms: now_unix_ms + AUTOMATIC_CHECK_INTERVAL_MS,
            trigger,
            outcome: UpdateCheckOutcome::from(verified.availability),
            current_version: identity.version,
            updater_version: env!("CARGO_PKG_VERSION").into(),
            release_channel: source.release_channel,
            offered_release: Some(verified.manifest),
            error: None,
        },
        Err(error) => UpdateCheckRecord {
            checked_at_unix_ms: now_unix_ms,
            next_automatic_check_at_unix_ms: now_unix_ms + AUTOMATIC_CHECK_INTERVAL_MS,
            trigger,
            outcome: UpdateCheckOutcome::Failed,
            current_version: identity.version,
            updater_version: env!("CARGO_PKG_VERSION").into(),
            release_channel: source.release_channel,
            offered_release: None,
            error: Some(sanitize_error(&error)),
        },
    };
    let stored = store
        .write_update_check(&record)
        .map_err(CoordinatorError::Fault)?;
    synchronize_update_alerts(&store, previous.as_ref(), &stored)
        .map_err(CoordinatorError::Fault)?;
    Ok(stored)
}

fn synchronize_update_alerts(
    store: &StationStore,
    previous: Option<&UpdateCheckRecord>,
    current: &UpdateCheckRecord,
) -> Result<(), String> {
    let now = current.checked_at_unix_ms;
    match current.outcome {
        UpdateCheckOutcome::UpdateAvailable | UpdateCheckOutcome::UpdaterTooOld => {
            store.resolve_alert(AlertConditionKind::UpdateCheckFailed, "stable-feed", now)?;
            if let Some(previous_version) = previous
                .and_then(|record| record.offered_release.as_ref())
                .map(|release| release.version.as_str())
                && current
                    .offered_release
                    .as_ref()
                    .is_some_and(|release| release.version != previous_version)
            {
                store.resolve_alert(AlertConditionKind::UpdateAvailable, previous_version, now)?;
            }
            let release = current
                .offered_release
                .as_ref()
                .ok_or_else(|| "available update check has no release".to_string())?;
            let (summary, detail) = if current.outcome == UpdateCheckOutcome::UpdaterTooOld {
                (
                    format!(
                        "TownLight Station {} requires a full installer",
                        release.version
                    ),
                    format!(
                        "Updater {} is older than the release minimum {}. Use the signed full installer; no package was applied.",
                        current.updater_version, release.minimum_updater_version
                    ),
                )
            } else {
                (
                    format!("TownLight Station {} is available", release.version),
                    "Review the signed release notes. Download and installation require explicit operator action and a maintenance window."
                        .into(),
                )
            };
            store.observe_alert_and_route(&AlertEvent {
                event_id: format!("update-available-{}-{now}", release.source_commit),
                condition: AlertConditionKind::UpdateAvailable,
                severity: AlertSeverity::Info,
                state: AlertEventState::Firing,
                resource_ref: release.version.clone(),
                summary,
                detail,
                source_section: "S8".into(),
                first_observed_at_unix_ms: now,
                last_observed_at_unix_ms: now,
                resolved_at_unix_ms: None,
                occurrence_count: 1,
                acknowledged_at_unix_ms: None,
                acknowledged_by: None,
            })?;
        }
        UpdateCheckOutcome::Failed => {
            store.observe_alert_and_route(&AlertEvent {
                event_id: format!("update-check-failed-{now}"),
                condition: AlertConditionKind::UpdateCheckFailed,
                severity: AlertSeverity::Warning,
                state: AlertEventState::Firing,
                resource_ref: "stable-feed".into(),
                summary: "Signed release check failed".into(),
                detail: current
                    .error
                    .clone()
                    .unwrap_or_else(|| "No trusted release response was received.".into()),
                source_section: "S8".into(),
                first_observed_at_unix_ms: now,
                last_observed_at_unix_ms: now,
                resolved_at_unix_ms: None,
                occurrence_count: 1,
                acknowledged_at_unix_ms: None,
                acknowledged_by: None,
            })?;
        }
        UpdateCheckOutcome::UpToDate => {
            store.resolve_alert(AlertConditionKind::UpdateCheckFailed, "stable-feed", now)?;
            if let Some(version) = previous
                .and_then(|record| record.offered_release.as_ref())
                .map(|release| release.version.as_str())
                .or_else(|| {
                    current
                        .offered_release
                        .as_ref()
                        .map(|release| release.version.as_str())
                })
            {
                store.resolve_alert(AlertConditionKind::UpdateAvailable, version, now)?;
            }
        }
    }
    Ok(())
}

fn deactivate_release_source(database_path: &Path, now_unix_ms: i64) -> Result<(), String> {
    let store = StationStore::open(database_path)?;
    let previous = store.read_update_check()?;
    store.resolve_alert(
        AlertConditionKind::UpdateCheckFailed,
        "stable-feed",
        now_unix_ms,
    )?;
    if let Some(version) = previous
        .and_then(|record| record.offered_release)
        .map(|release| release.version)
    {
        store.resolve_alert(AlertConditionKind::UpdateAvailable, &version, now_unix_ms)?;
    }
    store.clear_update_check()?;
    store.clear_staged_package()?;
    store.clear_prepared_slot()
}

pub fn release_notes(
    database_path: &Path,
    requested_version: &str,
) -> Result<Option<String>, CoordinatorError> {
    if requested_version.is_empty()
        || requested_version.len() > 80
        || requested_version.chars().any(char::is_control)
    {
        return Err(CoordinatorError::Fault(
            "requested release-notes version is invalid".into(),
        ));
    }
    let record = StationStore::open(database_path)
        .and_then(|store| store.read_update_check())
        .map_err(CoordinatorError::Fault)?;
    Ok(record
        .and_then(|record| record.offered_release)
        .filter(|release| release.version == requested_version)
        .map(|release| release.release_notes_markdown))
}

pub fn automatic_check_due(database_path: &Path, now_unix_ms: i64) -> Result<bool, String> {
    if now_unix_ms < 0 {
        return Err("automatic update-check time must be nonnegative".into());
    }
    Ok(StationStore::open(database_path)?
        .read_update_check()?
        .is_none_or(|record| record.next_automatic_check_at_unix_ms <= now_unix_ms))
}

pub fn run_until(
    database_path: &Path,
    application_directory: &Path,
    stop: Arc<AtomicBool>,
) -> Result<(), String> {
    let error_receipt = database_path.with_file_name("update-monitor-error.txt");
    let mut retry_at_unix_ms = 0;
    while !stop.load(Ordering::Acquire) {
        let now = system_time_millis()?;
        if now >= retry_at_unix_ms {
            match read_release_source(application_directory) {
                Ok(None) => {
                    if let Err(error) = deactivate_release_source(database_path, now) {
                        record_monitor_error(&error_receipt, &error);
                    } else {
                        let _ = std::fs::remove_file(&error_receipt);
                    }
                    retry_at_unix_ms = now.saturating_add(60_000);
                }
                Ok(Some(_)) => match automatic_check_due(database_path, now) {
                    Ok(true) => match check_now(
                        database_path,
                        application_directory,
                        UpdateCheckTrigger::Automatic,
                        now,
                    ) {
                        Ok(record) => {
                            retry_at_unix_ms = record.next_automatic_check_at_unix_ms;
                            let _ = std::fs::remove_file(&error_receipt);
                        }
                        Err(error) => {
                            record_monitor_error(&error_receipt, &error.to_string());
                            retry_at_unix_ms = now.saturating_add(60_000);
                        }
                    },
                    Ok(false) => {
                        let next = StationStore::open(database_path)?
                            .read_update_check()?
                            .map(|record| record.next_automatic_check_at_unix_ms)
                            .unwrap_or_else(|| now.saturating_add(60_000));
                        retry_at_unix_ms = next;
                    }
                    Err(error) => {
                        record_monitor_error(&error_receipt, &error);
                        retry_at_unix_ms = now.saturating_add(60_000);
                    }
                },
                Err(error) => {
                    record_monitor_error(&error_receipt, &error.to_string());
                    retry_at_unix_ms = now.saturating_add(60_000);
                }
            }
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    Ok(())
}

fn record_monitor_error(path: &Path, message: &str) {
    let safe = sanitize_error(message);
    let _ = std::fs::write(path, safe);
}

fn updates_directory(database_path: &Path) -> PathBuf {
    database_path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join("updates")
}

fn slots_directory(application_directory: &Path) -> PathBuf {
    application_directory.join("slots")
}

fn system_time_millis() -> Result<i64, String> {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_millis();
    i64::try_from(millis).map_err(|_| "system time exceeds the supported range".into())
}

#[derive(Debug, Deserialize)]
struct InstallIdentity {
    product: String,
    version: String,
    source_commit: String,
}

fn read_install_identity(database_path: &Path) -> Result<InstallIdentity, CoordinatorError> {
    let data_directory = database_path
        .parent()
        .ok_or_else(|| CoordinatorError::Fault("station database has no data directory".into()))?;
    let path = data_directory.join(RECEIPT_FILE_NAME);
    let bytes = read_bounded_file(&path, MAX_RECEIPT_BYTES, "installation receipt")?;
    let identity: InstallIdentity = serde_json::from_slice(&bytes).map_err(|error| {
        CoordinatorError::Fault(format!("installation receipt is invalid: {error}"))
    })?;
    if identity.product != "TownLight Station"
        || Version::parse(&identity.version).is_err()
        || !is_commit_hash(&identity.source_commit)
    {
        return Err(CoordinatorError::Fault(
            "installation receipt identity is invalid".into(),
        ));
    }
    Ok(identity)
}

fn read_release_source(
    application_directory: &Path,
) -> Result<Option<ReleaseSource>, CoordinatorError> {
    let path = application_directory.join(TRUST_FILE_NAME);
    if !path.exists() {
        return Ok(None);
    }
    let bytes = read_bounded_file(&path, MAX_CONFIGURATION_BYTES, "release trust file")?;
    let source: ReleaseSource = serde_json::from_slice(&bytes).map_err(|error| {
        CoordinatorError::Fault(format!("release trust file is invalid: {error}"))
    })?;
    source.validate().map_err(CoordinatorError::Fault)?;
    Ok(Some(source))
}

fn read_bounded_file(
    path: &Path,
    maximum_bytes: u64,
    label: &str,
) -> Result<Vec<u8>, CoordinatorError> {
    let metadata = std::fs::metadata(path)
        .map_err(|error| CoordinatorError::Fault(format!("could not read {label}: {error}")))?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > maximum_bytes {
        return Err(CoordinatorError::Fault(format!(
            "{label} size is outside the accepted bounds"
        )));
    }
    std::fs::read(path)
        .map_err(|error| CoordinatorError::Fault(format!("could not read {label}: {error}")))
}

fn is_commit_hash(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn redact_feed_origin(value: &str) -> String {
    let (scheme, remainder) = if let Some(remainder) = value.strip_prefix("https://") {
        ("https://", remainder)
    } else if let Some(remainder) = value.strip_prefix("http://") {
        ("http://", remainder)
    } else {
        return "configured".into();
    };
    format!(
        "{scheme}{}",
        remainder.split('/').next().unwrap_or_default()
    )
}

fn sanitize_error(value: &str) -> String {
    let cleaned = value
        .chars()
        .filter(|character| !character.is_control())
        .collect::<String>();
    let cleaned = cleaned.trim();
    if cleaned.is_empty() {
        return "release check failed".into();
    }
    cleaned.chars().take(500).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::Engine;
    use base64::engine::general_purpose::STANDARD as BASE64;
    use ed25519_dalek::{Signer, SigningKey};
    use sha2::{Digest, Sha256};
    use station_domain::{ChannelRuntimeObservation, ChannelRuntimeState};
    use station_schedule::{ChannelConfiguration, OutputSinkConfiguration};
    use station_update::{
        ReleaseManifest, SignedReleaseEnvelope, TrustedReleaseKey, UPDATE_PACKAGE_SCHEMA_VERSION,
        UpdatePackageFile, UpdatePackageManifest, prepared_slot_path,
    };
    use std::io::{Cursor, Read, Write};
    use std::net::TcpListener;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    #[allow(clippy::permissions_set_readonly_false)]
    fn checks_a_signed_feed_persists_status_and_serves_only_matching_notes() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("townlight-update-coordinator-{nonce}"));
        let application = root.join("application");
        let data = root.join("data");
        std::fs::create_dir_all(&application).unwrap();
        std::fs::create_dir_all(&data).unwrap();
        let database = data.join("station.db");
        StationStore::open(&database).unwrap();
        std::fs::write(
            data.join(RECEIPT_FILE_NAME),
            br#"{"product":"TownLight Station","version":"0.1.0","source_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}"#,
        )
        .unwrap();

        let now = 1_800_000_000_000;
        let signing_key = SigningKey::from_bytes(&[12; 32]);
        let payloads: [(&str, &[u8]); 2] = [
            ("stationd.exe", b"coordinator station service"),
            ("channel-worker.exe", b"coordinator channel worker"),
        ];
        let internal_manifest = UpdatePackageManifest {
            schema_version: UPDATE_PACKAGE_SCHEMA_VERSION,
            product: "TownLight Station".into(),
            version: "0.2.0".into(),
            source_commit: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb".into(),
            files: payloads
                .iter()
                .map(|(path, bytes)| UpdatePackageFile {
                    path: (*path).into(),
                    size_bytes: bytes.len() as u64,
                    sha256: format!("{:X}", Sha256::digest(bytes)),
                })
                .collect(),
        };
        let mut writer = zip::ZipWriter::new(Cursor::new(Vec::new()));
        let options = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Stored)
            .unix_permissions(0o644);
        writer.start_file("package-manifest.json", options).unwrap();
        writer
            .write_all(&serde_json::to_vec(&internal_manifest).unwrap())
            .unwrap();
        for (path, bytes) in payloads {
            writer
                .start_file(format!("payload/{path}"), options)
                .unwrap();
            writer.write_all(bytes).unwrap();
        }
        let package = writer.finish().unwrap().into_inner();
        let package_hash = format!("{:X}", Sha256::digest(&package));
        let package_size = package.len() as u64;
        let package_listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let package_address = package_listener.local_addr().unwrap();
        let package_server = std::thread::spawn(move || {
            let (mut stream, _) = package_listener.accept().unwrap();
            let mut request = [0_u8; 2_048];
            let _ = stream.read(&mut request).unwrap();
            let headers = format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                package.len()
            );
            stream.write_all(headers.as_bytes()).unwrap();
            stream.write_all(&package).unwrap();
        });
        let manifest = ReleaseManifest {
            schema_version: 1,
            product: "TownLight Station".into(),
            release_channel: "stable".into(),
            key_id: "release-2026-a".into(),
            version: "0.2.0".into(),
            source_commit: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb".into(),
            published_at_unix_ms: now - 1_000,
            expires_at_unix_ms: now + 86_400_000,
            minimum_updater_version: "0.1.0".into(),
            package_url: format!("http://{package_address}/station-0.2.0.tlu"),
            package_size_bytes: package_size,
            package_sha256: package_hash,
            release_notes_markdown: "# 0.2.0\n\nA safer update.".into(),
        };
        let manifest_bytes = serde_json::to_vec(&manifest).unwrap();
        let envelope = serde_json::to_vec(&SignedReleaseEnvelope {
            key_id: manifest.key_id.clone(),
            manifest_base64: BASE64.encode(&manifest_bytes),
            signature_base64: BASE64.encode(signing_key.sign(&manifest_bytes).to_bytes()),
        })
        .unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 2_048];
            let _ = stream.read(&mut request).unwrap();
            let headers = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                envelope.len()
            );
            stream.write_all(headers.as_bytes()).unwrap();
            stream.write_all(&envelope).unwrap();
        });
        let source = ReleaseSource {
            schema_version: 1,
            feed_url: format!("http://{address}/stable.json"),
            release_channel: "stable".into(),
            trusted_keys: vec![TrustedReleaseKey {
                key_id: "release-2026-a".into(),
                public_key_base64: BASE64.encode(signing_key.verifying_key().as_bytes()),
            }],
        };
        std::fs::write(
            application.join(TRUST_FILE_NAME),
            serde_json::to_vec(&source).unwrap(),
        )
        .unwrap();

        let checked = check_now(&database, &application, UpdateCheckTrigger::Manual, now).unwrap();
        assert_eq!(checked.outcome, UpdateCheckOutcome::UpdateAvailable);
        server.join().unwrap();
        let status = read_status(&database, &application).unwrap();
        assert!(status.configured);
        assert_eq!(status.feed_origin, Some(format!("http://{address}")));
        assert!(!automatic_check_due(&database, now).unwrap());
        assert!(automatic_check_due(&database, checked.next_automatic_check_at_unix_ms).unwrap());
        assert_eq!(status.latest_check, Some(checked));
        let alerts = StationStore::open(&database)
            .unwrap()
            .list_alert_events()
            .unwrap();
        assert_eq!(alerts.len(), 1);
        assert_eq!(alerts[0].condition, AlertConditionKind::UpdateAvailable);
        assert_eq!(alerts[0].severity, AlertSeverity::Info);
        assert_eq!(
            release_notes(&database, "0.2.0").unwrap().as_deref(),
            Some("# 0.2.0\n\nA safer update.")
        );
        assert_eq!(release_notes(&database, "0.3.0").unwrap(), None);

        let staged = stage_available_release(&database, &application, now + 1).unwrap();
        package_server.join().unwrap();
        assert_eq!(staged.manifest.version, "0.2.0");
        assert_eq!(
            read_status(&database, &application).unwrap().staged_package,
            Some(staged.clone())
        );
        let prepared = prepare_staged_release(&database, &application, now + 2).unwrap();
        assert_eq!(prepared.manifest.version, "0.2.0");
        assert_eq!(
            read_status(&database, &application).unwrap().prepared_slot,
            Some(prepared.clone())
        );

        assert!(matches!(
            request_maintenance_activation(
                &database,
                &application,
                "Station Operator",
                "install update",
                now + 3,
            ),
            Err(CoordinatorError::MaintenanceGate(_))
        ));
        assert!(matches!(
            request_maintenance_activation(
                &database,
                &application,
                "Station Operator",
                "INSTALL UPDATE",
                now + 3,
            ),
            Err(CoordinatorError::MaintenanceGate(_))
        ));
        let channel_id = "8ea48bc4-7f2e-4f89-9bbc-2c58472f0e8e";
        let store = StationStore::open(&database).unwrap();
        store
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Government Channel".into(),
                output_profile: Default::default(),
                output_sinks: vec![OutputSinkConfiguration::required_udp(
                    channel_id,
                    "Primary transport",
                    "127.0.0.1:5100",
                    -16.0,
                )],
                enabled: true,
            })
            .unwrap();
        store
            .put_channel_runtime(&ChannelRuntimeObservation {
                channel_id: channel_id.into(),
                state: ChannelRuntimeState::HealthyFallback,
                on_air_asset_id: None,
                state_started_at_unix_ms: now - 10_000,
                heartbeat_at_unix_ms: now + 3,
                detail: None,
            })
            .unwrap();
        let request = request_maintenance_activation(
            &database,
            &application,
            "Station Operator",
            "INSTALL UPDATE",
            now + 4,
        )
        .unwrap();
        assert_eq!(request.prepared_slot, prepared);
        assert_eq!(
            pending_request_paths(&data).unwrap(),
            vec![
                data.join("updates")
                    .join(station_maintenance::REQUEST_DIRECTORY_NAME)
                    .join("pending.json")
            ]
        );
        assert_eq!(
            request_maintenance_activation(
                &database,
                &application,
                "Station Operator",
                "INSTALL UPDATE",
                now + 5,
            ),
            Err(CoordinatorError::MaintenanceAlreadyQueued)
        );

        std::fs::remove_file(application.join(TRUST_FILE_NAME)).unwrap();
        deactivate_release_source(&database, now + 1).unwrap();
        let unconfigured = read_status(&database, &application).unwrap();
        assert!(!unconfigured.configured);
        assert_eq!(unconfigured.latest_check, None);
        let alerts = StationStore::open(&database)
            .unwrap()
            .list_alert_events()
            .unwrap();
        assert_eq!(alerts.len(), 1);
        assert_eq!(alerts[0].state, AlertEventState::Resolved);
        assert_eq!(
            StationStore::open(&database)
                .unwrap()
                .read_update_check()
                .unwrap(),
            None
        );
        assert_eq!(
            StationStore::open(&database)
                .unwrap()
                .read_prepared_slot()
                .unwrap(),
            None
        );
        assert_eq!(
            StationStore::open(&database)
                .unwrap()
                .read_staged_package()
                .unwrap(),
            None
        );

        let staged_path = staged_package_path(&updates_directory(&database), &staged);
        let mut permissions = std::fs::metadata(&staged_path).unwrap().permissions();
        permissions.set_readonly(false);
        std::fs::set_permissions(staged_path, permissions).unwrap();

        let slot_path = prepared_slot_path(&slots_directory(&application), &prepared);
        for entry in std::fs::read_dir(&slot_path).unwrap().flatten() {
            let mut permissions = entry.metadata().unwrap().permissions();
            permissions.set_readonly(false);
            std::fs::set_permissions(entry.path(), permissions).unwrap();
        }

        let _ = std::fs::remove_dir_all(root);
    }
}
