use semver::Version;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use station_update::{
    PreparedSlotRecord, UpdatePackageManifest, prepared_slot_path, read_prepared_slot_manifest,
    verify_prepared_slot,
};
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;
use windows_sys::Win32::Storage::FileSystem::{
    MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH, MoveFileExW,
};

pub const ACTIVE_SLOT_SCHEMA_VERSION: u32 = 1;
pub const MAINTENANCE_SCHEMA_VERSION: u32 = 1;
pub const ACTIVE_SLOT_RECEIPT_NAME: &str = "active-slot.json";
pub const INSTALL_RECEIPT_NAME: &str = "install-receipt.json";
pub const TRANSACTION_DIRECTORY_NAME: &str = "maintenance-transactions";
pub const REQUEST_DIRECTORY_NAME: &str = "maintenance-requests";
pub const COMPLETED_REQUEST_DIRECTORY_NAME: &str = "maintenance-requests-completed";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ActiveApplication {
    pub relative_directory: String,
    pub package_manifest: UpdatePackageManifest,
}

impl ActiveApplication {
    pub fn validate(&self) -> Result<(), String> {
        self.package_manifest.validate()?;
        let expected_slot = format!(
            "slots/slot-{}-{}",
            self.package_manifest.version, self.package_manifest.source_commit
        );
        if self.relative_directory != "." && self.relative_directory != expected_slot {
            return Err("active application directory is invalid".into());
        }
        Ok(())
    }

    pub fn directory(&self, application_root: &Path) -> Result<PathBuf, String> {
        self.validate()?;
        if self.relative_directory == "." {
            Ok(application_root.to_path_buf())
        } else {
            Ok(application_root.join(
                self.relative_directory
                    .strip_prefix("slots/")
                    .map(|slot| Path::new("slots").join(slot))
                    .ok_or_else(|| "active application slot path is invalid".to_string())?,
            ))
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ActiveSlotReceipt {
    pub schema_version: u32,
    pub generation: u64,
    pub activated_at_unix_ms: i64,
    pub transaction_id: String,
    pub active: ActiveApplication,
    pub previous: Option<ActiveApplication>,
}

impl ActiveSlotReceipt {
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != ACTIVE_SLOT_SCHEMA_VERSION
            || self.activated_at_unix_ms < 0
            || !visible_identifier(&self.transaction_id, 120)
        {
            return Err("active-slot receipt fields are invalid".into());
        }
        self.active.validate()?;
        if let Some(previous) = &self.previous {
            previous.validate()?;
            if previous == &self.active {
                return Err("active and previous applications cannot be identical".into());
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MaintenanceRequest {
    pub schema_version: u32,
    pub transaction_id: String,
    pub requested_at_unix_ms: i64,
    pub requested_by: String,
    pub prepared_slot: PreparedSlotRecord,
}

impl MaintenanceRequest {
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != MAINTENANCE_SCHEMA_VERSION
            || !visible_identifier(&self.transaction_id, 120)
            || !visible_operator(&self.requested_by)
            || self.requested_at_unix_ms < self.prepared_slot.prepared_at_unix_ms
        {
            return Err("maintenance request fields are invalid".into());
        }
        self.prepared_slot.validate().map_err(str::to_string)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MaintenancePhase {
    Requested,
    ServiceStopped,
    CandidateConfigured,
    CandidateStarted,
    CommitStarted,
    Committed,
    RollbackStarted,
    RolledBack,
    RollbackFailed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MaintenanceTransactionRecord {
    pub schema_version: u32,
    pub request: MaintenanceRequest,
    pub phase: MaintenancePhase,
    pub updated_at_unix_ms: i64,
    pub previous_receipt: ActiveSlotReceipt,
    pub candidate: ActiveApplication,
    pub detail: String,
}

impl MaintenanceTransactionRecord {
    pub fn validate(&self) -> Result<(), String> {
        self.request.validate()?;
        self.previous_receipt.validate()?;
        self.candidate.validate()?;
        if self.schema_version != MAINTENANCE_SCHEMA_VERSION
            || self.updated_at_unix_ms < self.request.requested_at_unix_ms
            || self.previous_receipt.active == self.candidate
            || !bounded_detail(&self.detail)
        {
            return Err("maintenance transaction fields are invalid".into());
        }
        Ok(())
    }
}

pub trait MaintenanceHost {
    fn stop_station(&mut self, timeout: Duration) -> Result<(), String>;
    fn configure_station(
        &mut self,
        stationd_path: &Path,
        database_path: &Path,
    ) -> Result<(), String>;
    fn start_station(&mut self) -> Result<(), String>;
    fn prove_station(
        &mut self,
        expected: &ActiveApplication,
        timeout: Duration,
    ) -> Result<(), String>;
}

pub fn activate_prepared_slot(
    request: &MaintenanceRequest,
    database_path: &Path,
    application_root: &Path,
    now_unix_ms: i64,
    host: &mut impl MaintenanceHost,
) -> Result<MaintenanceTransactionRecord, String> {
    request.validate()?;
    if !database_path.is_absolute() || !application_root.is_absolute() {
        return Err("maintenance paths must be absolute".into());
    }
    let data_directory = database_path
        .parent()
        .ok_or_else(|| "station database has no data directory".to_string())?;
    let active_path = data_directory.join(ACTIVE_SLOT_RECEIPT_NAME);
    let current = read_active_slot_receipt(&active_path)?;
    verify_active_application(application_root, &current.active)?;
    verify_prepared_slot(&application_root.join("slots"), &request.prepared_slot)?;
    let candidate_manifest = read_prepared_slot_manifest(&prepared_slot_path(
        &application_root.join("slots"),
        &request.prepared_slot,
    ))?;
    let candidate = ActiveApplication {
        relative_directory: format!("slots/{}", request.prepared_slot.slot_directory_name),
        package_manifest: candidate_manifest,
    };
    candidate.validate()?;
    if Version::parse(&candidate.package_manifest.version)
        .map_err(|_| "candidate version is invalid".to_string())?
        <= Version::parse(&current.active.package_manifest.version)
            .map_err(|_| "active version is invalid".to_string())?
    {
        return Err("maintenance activation refuses an equal or older candidate".into());
    }
    let transaction_directory = data_directory
        .join("updates")
        .join(TRANSACTION_DIRECTORY_NAME);
    ensure_real_directory(&transaction_directory)?;
    let transaction_path = transaction_directory.join(format!("{}.json", request.transaction_id));
    if transaction_path.exists() {
        return Err("maintenance transaction identity already exists".into());
    }
    let mut transaction = MaintenanceTransactionRecord {
        schema_version: MAINTENANCE_SCHEMA_VERSION,
        request: request.clone(),
        phase: MaintenancePhase::Requested,
        updated_at_unix_ms: now_unix_ms,
        previous_receipt: current.clone(),
        candidate: candidate.clone(),
        detail: "Maintenance request accepted; station service has not been stopped.".into(),
    };
    write_transaction(&transaction_path, &transaction)?;

    let activation_result = (|| {
        host.stop_station(Duration::from_secs(30))?;
        advance(
            &transaction_path,
            &mut transaction,
            MaintenancePhase::ServiceStopped,
            now_unix_ms,
            "Station service stopped cleanly.",
        )?;
        let candidate_directory = candidate.directory(application_root)?;
        host.configure_station(&candidate_directory.join("stationd.exe"), database_path)?;
        advance(
            &transaction_path,
            &mut transaction,
            MaintenancePhase::CandidateConfigured,
            now_unix_ms,
            "Service command points to the verified candidate slot.",
        )?;
        host.start_station()?;
        advance(
            &transaction_path,
            &mut transaction,
            MaintenancePhase::CandidateStarted,
            now_unix_ms,
            "Candidate service start was requested.",
        )?;
        host.prove_station(&candidate, Duration::from_secs(60))?;
        Ok::<(), String>(())
    })();

    if let Err(activation_error) = activation_result {
        return rollback(
            &transaction_path,
            &mut transaction,
            database_path,
            application_root,
            now_unix_ms,
            host,
            &activation_error,
        );
    }

    advance(
        &transaction_path,
        &mut transaction,
        MaintenancePhase::CommitStarted,
        now_unix_ms,
        "Candidate proof passed; durable receipt commit started.",
    )?;
    let receipt = ActiveSlotReceipt {
        schema_version: ACTIVE_SLOT_SCHEMA_VERSION,
        generation: current.generation.saturating_add(1),
        activated_at_unix_ms: now_unix_ms,
        transaction_id: request.transaction_id.clone(),
        active: candidate,
        previous: Some(current.active),
    };
    receipt.validate()?;
    write_json_atomic(&active_path, &receipt, "active-slot receipt")?;
    write_install_identity(data_directory, &receipt.active, now_unix_ms)?;
    advance(
        &transaction_path,
        &mut transaction,
        MaintenancePhase::Committed,
        now_unix_ms,
        "Candidate passed post-update proof and is now active.",
    )?;
    Ok(transaction)
}

pub fn enqueue_maintenance_request(
    data_directory: &Path,
    request: &MaintenanceRequest,
) -> Result<PathBuf, String> {
    request.validate()?;
    let directory = data_directory.join("updates").join(REQUEST_DIRECTORY_NAME);
    ensure_real_directory(&directory)?;
    // A fixed inbox name makes the single-pending-request invariant atomic. `create_new`
    // arbitrates concurrent operator requests without a process-local mutex or a race-prone
    // list-then-create sequence.
    let path = directory.join("pending.json");
    let bytes = serde_json::to_vec(request)
        .map_err(|error| format!("could not encode maintenance request: {error}"))?;
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)
        .map_err(|error| {
            if error.kind() == std::io::ErrorKind::AlreadyExists {
                "a maintenance request is already pending".to_string()
            } else {
                format!("could not create maintenance request: {error}")
            }
        })?;
    output
        .write_all(&bytes)
        .and_then(|()| output.sync_all())
        .map_err(|error| format!("could not synchronize maintenance request: {error}"))?;
    Ok(path)
}

pub fn read_maintenance_request(path: &Path) -> Result<MaintenanceRequest, String> {
    let bytes = read_bounded_file(path, 512 * 1024, "maintenance request")?;
    let request: MaintenanceRequest = serde_json::from_slice(&bytes)
        .map_err(|error| format!("maintenance request is invalid: {error}"))?;
    request.validate()?;
    Ok(request)
}

pub fn pending_request_paths(data_directory: &Path) -> Result<Vec<PathBuf>, String> {
    let directory = data_directory.join("updates").join(REQUEST_DIRECTORY_NAME);
    ensure_real_directory(&directory)?;
    let mut paths = std::fs::read_dir(&directory)
        .map_err(|error| format!("could not list maintenance requests: {error}"))?
        .map(|entry| {
            let entry =
                entry.map_err(|error| format!("could not inspect maintenance request: {error}"))?;
            let metadata = std::fs::symlink_metadata(entry.path())
                .map_err(|error| format!("could not inspect maintenance request: {error}"))?;
            if !metadata.is_file() || metadata.file_type().is_symlink() {
                return Err("maintenance request inbox contains a non-file entry".into());
            }
            let path = entry.path();
            if path.extension().and_then(|value| value.to_str()) != Some("json") {
                return Err("maintenance request inbox contains an unexpected file".into());
            }
            Ok(path)
        })
        .collect::<Result<Vec<_>, String>>()?;
    paths.sort();
    Ok(paths)
}

pub fn process_maintenance_request(
    request_path: &Path,
    database_path: &Path,
    application_root: &Path,
    now_unix_ms: i64,
    host: &mut impl MaintenanceHost,
) -> Result<MaintenanceTransactionRecord, String> {
    let request = read_maintenance_request(request_path)?;
    let data_directory = database_path
        .parent()
        .ok_or_else(|| "station database has no data directory".to_string())?;
    let transaction_path = data_directory
        .join("updates")
        .join(TRANSACTION_DIRECTORY_NAME)
        .join(format!("{}.json", request.transaction_id));
    if transaction_path.exists() {
        recover_incomplete_transaction(
            &transaction_path,
            database_path,
            application_root,
            now_unix_ms,
            host,
        )
    } else {
        activate_prepared_slot(&request, database_path, application_root, now_unix_ms, host)
    }
}

pub fn archive_completed_request(
    data_directory: &Path,
    request_path: &Path,
) -> Result<PathBuf, String> {
    let expected_parent = data_directory.join("updates").join(REQUEST_DIRECTORY_NAME);
    if request_path.parent() != Some(expected_parent.as_path()) {
        return Err("maintenance request is outside the managed inbox".into());
    }
    let request = read_maintenance_request(request_path)?;
    let completed = data_directory
        .join("updates")
        .join(COMPLETED_REQUEST_DIRECTORY_NAME);
    ensure_real_directory(&completed)?;
    let destination = completed.join(format!("{}.json", request.transaction_id));
    if destination.exists() {
        return Err("completed maintenance request identity already exists".into());
    }
    replace_file(request_path, &destination)
        .map_err(|error| format!("could not archive completed maintenance request: {error}"))?;
    Ok(destination)
}

fn rollback(
    transaction_path: &Path,
    transaction: &mut MaintenanceTransactionRecord,
    database_path: &Path,
    application_root: &Path,
    now_unix_ms: i64,
    host: &mut impl MaintenanceHost,
    activation_error: &str,
) -> Result<MaintenanceTransactionRecord, String> {
    advance(
        transaction_path,
        transaction,
        MaintenancePhase::RollbackStarted,
        now_unix_ms,
        &format!("Candidate failed proof; rollback started: {activation_error}"),
    )?;
    let result = (|| {
        let _ = host.stop_station(Duration::from_secs(30));
        let previous_directory = transaction
            .previous_receipt
            .active
            .directory(application_root)?;
        host.configure_station(&previous_directory.join("stationd.exe"), database_path)?;
        host.start_station()?;
        host.prove_station(
            &transaction.previous_receipt.active,
            Duration::from_secs(60),
        )
    })();
    match result {
        Ok(()) => {
            advance(
                transaction_path,
                transaction,
                MaintenancePhase::RolledBack,
                now_unix_ms,
                &format!(
                    "Candidate failed and the prior application was restored: {activation_error}"
                ),
            )?;
            Ok(transaction.clone())
        }
        Err(rollback_error) => {
            advance(
                transaction_path,
                transaction,
                MaintenancePhase::RollbackFailed,
                now_unix_ms,
                &format!(
                    "Candidate failed ({activation_error}); prior application recovery also failed ({rollback_error})"
                ),
            )?;
            Err(transaction.detail.clone())
        }
    }
}

pub fn recover_incomplete_transaction(
    transaction_path: &Path,
    database_path: &Path,
    application_root: &Path,
    now_unix_ms: i64,
    host: &mut impl MaintenanceHost,
) -> Result<MaintenanceTransactionRecord, String> {
    let mut transaction = read_transaction(transaction_path)?;
    if matches!(
        transaction.phase,
        MaintenancePhase::Committed | MaintenancePhase::RolledBack
    ) {
        return Ok(transaction);
    }
    verify_active_application(application_root, &transaction.previous_receipt.active)?;
    advance(
        transaction_path,
        &mut transaction,
        MaintenancePhase::RollbackStarted,
        now_unix_ms,
        "Interrupted maintenance transaction detected; conservative rollback started.",
    )?;
    let recovery = (|| {
        let _ = host.stop_station(Duration::from_secs(30));
        let previous_directory = transaction
            .previous_receipt
            .active
            .directory(application_root)?;
        host.configure_station(&previous_directory.join("stationd.exe"), database_path)?;
        host.start_station()?;
        host.prove_station(
            &transaction.previous_receipt.active,
            Duration::from_secs(60),
        )?;
        write_json_atomic(
            &database_path.with_file_name(ACTIVE_SLOT_RECEIPT_NAME),
            &transaction.previous_receipt,
            "active-slot receipt",
        )?;
        write_install_identity(
            database_path
                .parent()
                .ok_or_else(|| "station database has no data directory".to_string())?,
            &transaction.previous_receipt.active,
            now_unix_ms,
        )
    })();
    match recovery {
        Ok(()) => {
            advance(
                transaction_path,
                &mut transaction,
                MaintenancePhase::RolledBack,
                now_unix_ms,
                "Interrupted maintenance transaction rolled back and prior application proved.",
            )?;
            Ok(transaction)
        }
        Err(error) => {
            advance(
                transaction_path,
                &mut transaction,
                MaintenancePhase::RollbackFailed,
                now_unix_ms,
                &format!("Interrupted maintenance recovery failed: {error}"),
            )?;
            Err(transaction.detail.clone())
        }
    }
}

pub fn read_transaction(path: &Path) -> Result<MaintenanceTransactionRecord, String> {
    let bytes = read_bounded_file(path, 512 * 1024, "maintenance transaction")?;
    let transaction: MaintenanceTransactionRecord = serde_json::from_slice(&bytes)
        .map_err(|error| format!("maintenance transaction is invalid: {error}"))?;
    transaction.validate()?;
    Ok(transaction)
}

pub fn read_active_slot_receipt(path: &Path) -> Result<ActiveSlotReceipt, String> {
    let bytes = read_bounded_file(path, 256 * 1024, "active-slot receipt")?;
    let receipt: ActiveSlotReceipt = serde_json::from_slice(&bytes)
        .map_err(|error| format!("active-slot receipt is invalid: {error}"))?;
    receipt.validate()?;
    Ok(receipt)
}

pub fn verify_active_application(
    application_root: &Path,
    application: &ActiveApplication,
) -> Result<(), String> {
    application.validate()?;
    let directory = application.directory(application_root)?;
    let actual = std::fs::read_dir(&directory)
        .map_err(|error| format!("could not list active application directory: {error}"))?
        .filter_map(Result::ok)
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .collect::<std::collections::BTreeSet<_>>();
    for file in &application.package_manifest.files {
        if !actual.contains(&file.path) {
            return Err(format!("active application file is missing: {}", file.path));
        }
        verify_file(&directory.join(&file.path), file.size_bytes, &file.sha256)?;
    }
    Ok(())
}

fn write_install_identity(
    data_directory: &Path,
    application: &ActiveApplication,
    now_unix_ms: i64,
) -> Result<(), String> {
    #[derive(Deserialize)]
    struct PreviousInstallIdentity {
        #[serde(default)]
        media_runtime_sha256: Option<String>,
    }
    #[derive(Serialize)]
    struct InstallIdentity<'a> {
        product: &'static str,
        version: &'a str,
        source_commit: &'a str,
        installed_at_unix_ms: i64,
        #[serde(skip_serializing_if = "Option::is_none")]
        media_runtime_sha256: Option<&'a str>,
        health_uri: &'static str,
    }
    let previous_path = data_directory.join(INSTALL_RECEIPT_NAME);
    let previous = read_bounded_file(&previous_path, 64 * 1024, "installation receipt")
        .ok()
        .and_then(|bytes| serde_json::from_slice::<PreviousInstallIdentity>(&bytes).ok())
        .and_then(|identity| identity.media_runtime_sha256)
        .filter(|hash| is_sha256(hash));
    write_json_atomic(
        &previous_path,
        &InstallIdentity {
            product: "TownLight Station",
            version: &application.package_manifest.version,
            source_commit: &application.package_manifest.source_commit,
            installed_at_unix_ms: now_unix_ms,
            media_runtime_sha256: previous.as_deref(),
            health_uri: "http://127.0.0.1:4070/health",
        },
        "installation receipt",
    )
}

fn advance(
    path: &Path,
    transaction: &mut MaintenanceTransactionRecord,
    phase: MaintenancePhase,
    now_unix_ms: i64,
    detail: &str,
) -> Result<(), String> {
    transaction.phase = phase;
    transaction.updated_at_unix_ms = transaction.updated_at_unix_ms.max(now_unix_ms);
    transaction.detail = bounded(detail, 1_000);
    transaction.validate()?;
    write_transaction(path, transaction)
}

fn write_transaction(
    path: &Path,
    transaction: &MaintenanceTransactionRecord,
) -> Result<(), String> {
    transaction.validate()?;
    write_json_atomic(path, transaction, "maintenance transaction")
}

fn write_json_atomic(path: &Path, value: &impl Serialize, label: &str) -> Result<(), String> {
    let bytes =
        serde_json::to_vec(value).map_err(|error| format!("could not encode {label}: {error}"))?;
    let parent = path
        .parent()
        .ok_or_else(|| format!("{label} has no parent directory"))?;
    ensure_real_directory(parent)?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("receipt"),
        std::process::id()
    ));
    let result = (|| {
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
            .map_err(|error| format!("could not create temporary {label}: {error}"))?;
        output
            .write_all(&bytes)
            .and_then(|()| output.sync_all())
            .map_err(|error| format!("could not synchronize {label}: {error}"))?;
        drop(output);
        replace_file(&temporary, path)
            .map_err(|error| format!("could not publish {label}: {error}"))
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    result
}

fn read_bounded_file(path: &Path, maximum: u64, label: &str) -> Result<Vec<u8>, String> {
    let metadata = std::fs::symlink_metadata(path)
        .map_err(|error| format!("could not inspect {label}: {error}"))?;
    if !metadata.is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() == 0
        || metadata.len() > maximum
    {
        return Err(format!(
            "{label} size or type is outside the accepted bounds"
        ));
    }
    std::fs::read(path).map_err(|error| format!("could not read {label}: {error}"))
}

fn replace_file(source: &Path, destination: &Path) -> std::io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    let source = source
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    let destination = destination
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    // SAFETY: both vectors are valid NUL-terminated UTF-16 strings for the duration of the call.
    let status = unsafe {
        MoveFileExW(
            source.as_ptr(),
            destination.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if status == 0 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

fn ensure_real_directory(path: &Path) -> Result<(), String> {
    std::fs::create_dir_all(path)
        .map_err(|error| format!("could not create maintenance directory: {error}"))?;
    let metadata = std::fs::symlink_metadata(path)
        .map_err(|error| format!("could not inspect maintenance directory: {error}"))?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err("maintenance directory is not a real directory".into());
    }
    Ok(())
}

fn verify_file(path: &Path, expected_size: u64, expected_hash: &str) -> Result<(), String> {
    let metadata = std::fs::symlink_metadata(path)
        .map_err(|error| format!("could not inspect application file: {error}"))?;
    if !metadata.is_file() || metadata.file_type().is_symlink() || metadata.len() != expected_size {
        return Err("application file metadata does not match its receipt".into());
    }
    let mut input =
        File::open(path).map_err(|error| format!("could not open application file: {error}"))?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = input
            .read(&mut buffer)
            .map_err(|error| format!("could not verify application file: {error}"))?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    if !format!("{:X}", digest.finalize()).eq_ignore_ascii_case(expected_hash) {
        return Err("application file SHA-256 does not match its receipt".into());
    }
    Ok(())
}

fn visible_identifier(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum
        && value.trim() == value
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn visible_operator(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 120
        && value.trim() == value
        && !value.chars().any(char::is_control)
}

fn bounded_detail(value: &str) -> bool {
    !value.is_empty() && value.len() <= 1_000 && !value.chars().any(|character| character == '\0')
}

fn bounded(value: &str, maximum: usize) -> String {
    let clean = value
        .chars()
        .filter(|character| *character != '\0')
        .collect::<String>();
    clean.chars().take(maximum).collect()
}
