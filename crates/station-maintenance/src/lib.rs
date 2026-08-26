use semver::Version;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use station_update::{
    PreparedSlotRecord, UpdatePackageManifest, prepared_slot_path, read_prepared_slot_manifest,
    verify_prepared_slot,
};
use std::collections::BTreeSet;
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
pub const INSTALLED_RUNTIME_MANIFEST_NAME: &str = "runtime-manifest.json";
pub const TSDUCK_RUNTIME_MANIFEST_NAME: &str = "tsduck-runtime-manifest.json";
pub const INSTALLED_RUNTIME_MANIFEST_SCHEMA_VERSION: u32 = 1;
pub const TRANSACTION_DIRECTORY_NAME: &str = "maintenance-transactions";
pub const REQUEST_DIRECTORY_NAME: &str = "maintenance-requests";
pub const COMPLETED_REQUEST_DIRECTORY_NAME: &str = "maintenance-requests-completed";
pub const JOURNAL_SNAPSHOT_SCHEMA_VERSION: u32 = 1;
const JOURNAL_SNAPSHOT_DIRECTORY_SUFFIX: &str = ".journal-snapshot";
const JOURNAL_SNAPSHOT_MANIFEST_NAME: &str = "manifest.json";
const JOURNAL_RESTORE_STAGE_NAME: &str = "restore-stage";
const JOURNAL_RESTORE_QUARANTINE_NAME: &str = "restore-quarantine";
const MAX_RUNTIME_MANIFEST_BYTES: u64 = 512 * 1024;
const MAX_RUNTIME_MANIFEST_ENTRIES: usize = 4_096;
const MAX_RUNTIME_FILE_BYTES: u64 = 512 * 1024 * 1024;
const MAX_RUNTIME_TOTAL_BYTES: u64 = 1024 * 1024 * 1024;
const MAX_JOURNAL_SNAPSHOT_ENTRIES: usize = 10_000;
const MAX_JOURNAL_SNAPSHOT_PATH_BYTES: usize = 512;
const MAX_JOURNAL_SNAPSHOT_PATH_DEPTH: usize = 32;
const TSDUCK_VERSION: &str = "3.44-4676";
const TSDUCK_SOURCE_URL: &str = "https://github.com/tsduck/tsduck/releases/download/v3.44-4676/TSDuck-Win64-3.44-4676-Portable.zip";
const TSDUCK_ARCHIVE_NAME: &str = "TSDuck-Win64-3.44-4676-Portable.zip";
const TSDUCK_ARCHIVE_SHA256: &str =
    "b0ca0f963fcd77488b8c32d6f9d85030daa891ed753c36e57bbb458807a44eb1";
const REQUIRED_TSDUCK_RUNTIME_FILES: [&str; 6] = [
    "LICENSE.txt",
    "OTHERS.txt",
    "bin/tsp.exe",
    "bin/tsanalyze.exe",
    "bin/tsduck.dll",
    "bin/tscore.dll",
];

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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstalledRuntimeManifest {
    pub schema_version: u32,
    pub product: String,
    pub version: String,
    pub source_commit: String,
    pub runtime_package_sha256: String,
    pub files: Vec<InstalledRuntimeFile>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstalledRuntimeFile {
    pub path: String,
    pub size_bytes: u64,
    pub sha256: String,
}

impl InstalledRuntimeManifest {
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != INSTALLED_RUNTIME_MANIFEST_SCHEMA_VERSION
            || self.product != "TownLight Station"
            || Version::parse(&self.version).is_err()
            || !is_commit_hash_lowercase(&self.source_commit)
            || !is_sha256_lowercase(&self.runtime_package_sha256)
            || self.files.is_empty()
            || self.files.len() > MAX_RUNTIME_MANIFEST_ENTRIES
        {
            return Err("installed runtime manifest identity or bounds are invalid".into());
        }
        let mut paths = BTreeSet::new();
        let mut folded_paths = BTreeSet::new();
        let mut total = 0_u64;
        for file in &self.files {
            validate_runtime_relative_path(&file.path)?;
            if !paths.insert(file.path.clone())
                || !folded_paths.insert(file.path.to_lowercase())
                || file.size_bytes > MAX_RUNTIME_FILE_BYTES
                || !is_sha256_lowercase(&file.sha256)
            {
                return Err("installed runtime manifest file entry is invalid".into());
            }
            total = total
                .checked_add(file.size_bytes)
                .ok_or_else(|| "installed runtime manifest total size overflowed".to_string())?;
        }
        if total > MAX_RUNTIME_TOTAL_BYTES {
            return Err("installed runtime manifest total size exceeds its bound".into());
        }
        Ok(())
    }
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
pub struct JournalSnapshot {
    pub schema_version: u32,
    pub relative_directory: String,
    pub manifest_name: String,
}

impl JournalSnapshot {
    fn validate(&self) -> Result<(), String> {
        let transaction_id = self
            .relative_directory
            .strip_suffix(JOURNAL_SNAPSHOT_DIRECTORY_SUFFIX);
        if self.schema_version != JOURNAL_SNAPSHOT_SCHEMA_VERSION
            || !transaction_id.is_some_and(|transaction_id| {
                visible_identifier(transaction_id, 120)
                    && transaction_id.chars().any(|character| character != '.')
            })
            || self.manifest_name != JOURNAL_SNAPSHOT_MANIFEST_NAME
        {
            return Err("journal snapshot fields are invalid".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JournalSnapshotManifest {
    pub schema_version: u32,
    pub entries: Vec<JournalSnapshotEntry>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JournalSnapshotEntry {
    pub relative_path: String,
    pub present: bool,
    pub directory: bool,
    pub size_bytes: u64,
    pub sha256: String,
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
    #[serde(default)]
    pub journal_snapshot: Option<JournalSnapshot>,
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
        if let Some(snapshot) = &self.journal_snapshot {
            snapshot.validate()?;
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
        journal_snapshot: None,
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
        let snapshot = create_journal_snapshot(
            data_directory,
            &transaction_directory,
            &request.transaction_id,
        )?;
        transaction.journal_snapshot = Some(snapshot);
        write_transaction(&transaction_path, &transaction)?;
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

    let receipt = ActiveSlotReceipt {
        schema_version: ACTIVE_SLOT_SCHEMA_VERSION,
        generation: current.generation.saturating_add(1),
        activated_at_unix_ms: now_unix_ms,
        transaction_id: request.transaction_id.clone(),
        active: candidate,
        previous: Some(current.active),
    };
    let commit_result = (|| {
        // The first proof runs the candidate against the old receipts. Stop it
        // again before changing candidate authority so no running stationd can
        // append evidence across the receipt-generation boundary.
        host.stop_station(Duration::from_secs(30))?;
        advance(
            &transaction_path,
            &mut transaction,
            MaintenancePhase::CommitStarted,
            now_unix_ms,
            "Candidate proof passed; candidate stopped and durable receipt commit started.",
        )?;
        receipt.validate()?;
        write_json_atomic(&active_path, &receipt, "active-slot receipt")?;
        write_install_identity(data_directory, &receipt.active, now_unix_ms)?;
        host.start_station()?;
        host.prove_station(&receipt.active, Duration::from_secs(60))?;
        advance(
            &transaction_path,
            &mut transaction,
            MaintenancePhase::Committed,
            now_unix_ms,
            "Committed candidate restarted, proved against current receipts, and is active.",
        )?;
        Ok::<(), String>(())
    })();
    if let Err(commit_error) = commit_result {
        return rollback(
            &transaction_path,
            &mut transaction,
            database_path,
            application_root,
            now_unix_ms,
            host,
            &commit_error,
        );
    }
    if let Some(snapshot) = &transaction.journal_snapshot {
        discard_journal_snapshot(&transaction_directory, snapshot)?;
    }
    Ok(transaction)
}

fn create_journal_snapshot(
    data_directory: &Path,
    transaction_directory: &Path,
    transaction_id: &str,
) -> Result<JournalSnapshot, String> {
    let snapshot = JournalSnapshot {
        schema_version: JOURNAL_SNAPSHOT_SCHEMA_VERSION,
        relative_directory: format!("{transaction_id}{JOURNAL_SNAPSHOT_DIRECTORY_SUFFIX}"),
        manifest_name: JOURNAL_SNAPSHOT_MANIFEST_NAME.into(),
    };
    snapshot.validate()?;
    let snapshot_directory = journal_snapshot_directory(transaction_directory, &snapshot)?;
    if snapshot_directory.exists() {
        return Err("journal snapshot directory already exists".into());
    }
    ensure_real_directory(&snapshot_directory)?;
    let result = (|| {
        let backup_root = snapshot_directory.join("tree").join("channel-journals");
        let source_root = data_directory.join("channel-journals");
        let mut entries = Vec::new();
        snapshot_tree(&source_root, &backup_root, ".", &mut entries)?;
        entries.sort_by(|left, right| left.relative_path.cmp(&right.relative_path));
        let manifest = JournalSnapshotManifest {
            schema_version: JOURNAL_SNAPSHOT_SCHEMA_VERSION,
            entries,
        };
        validate_journal_snapshot_manifest(&manifest)?;
        write_json_atomic(
            &snapshot_directory.join(&snapshot.manifest_name),
            &manifest,
            "channel-journal snapshot manifest",
        )?;
        Ok(snapshot.clone())
    })();
    if result.is_err() {
        let _ = remove_tree(&snapshot_directory);
    }
    result
}

fn snapshot_tree(
    source: &Path,
    destination: &Path,
    relative_path: &str,
    entries: &mut Vec<JournalSnapshotEntry>,
) -> Result<(), String> {
    let metadata = match std::fs::symlink_metadata(source) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            entries.push(JournalSnapshotEntry {
                relative_path: relative_path.into(),
                present: false,
                directory: true,
                size_bytes: 0,
                sha256: String::new(),
            });
            return Ok(());
        }
        Err(error) => return Err(format!("could not inspect channel-journal tree: {error}")),
    };
    if metadata.file_type().is_symlink() {
        return Err("channel-journal tree contains a symlink".into());
    }
    if metadata.is_dir() {
        ensure_real_directory(destination)?;
        entries.push(JournalSnapshotEntry {
            relative_path: relative_path.into(),
            present: true,
            directory: true,
            size_bytes: 0,
            sha256: String::new(),
        });
        let mut children = std::fs::read_dir(source)
            .map_err(|error| format!("could not list channel-journal tree: {error}"))?
            .map(|entry| {
                entry
                    .map(|entry| entry.path())
                    .map_err(|error| format!("could not inspect channel-journal tree: {error}"))
            })
            .collect::<Result<Vec<_>, String>>()?;
        children.sort();
        for child in children {
            let name = child
                .file_name()
                .and_then(|name| name.to_str())
                .ok_or_else(|| "channel-journal path is not valid UTF-8".to_string())?;
            let child_relative = if relative_path == "." {
                name.to_string()
            } else {
                format!("{relative_path}/{name}")
            };
            validate_snapshot_relative_path(&child_relative, false)?;
            snapshot_tree(&child, &destination.join(name), &child_relative, entries)?;
        }
    } else if metadata.is_file() {
        let (size_bytes, sha256) = copy_hashed_file(source, destination)?;
        entries.push(JournalSnapshotEntry {
            relative_path: relative_path.into(),
            present: true,
            directory: false,
            size_bytes,
            sha256,
        });
    } else {
        return Err("channel-journal tree contains an unsupported entry".into());
    }
    Ok(())
}

fn restore_journal_snapshot(
    data_directory: &Path,
    transaction_directory: &Path,
    snapshot: &JournalSnapshot,
) -> Result<(), String> {
    snapshot.validate()?;
    let snapshot_directory = journal_snapshot_directory(transaction_directory, snapshot)?;
    require_real_directory(&snapshot_directory, "journal snapshot directory")?;
    let manifest_path = snapshot_directory.join(&snapshot.manifest_name);
    let manifest_bytes = read_bounded_file(
        &manifest_path,
        32 * 1024 * 1024,
        "channel-journal snapshot manifest",
    )?;
    let manifest: JournalSnapshotManifest = serde_json::from_slice(&manifest_bytes)
        .map_err(|error| format!("channel-journal snapshot manifest is invalid: {error}"))?;
    validate_journal_snapshot_manifest(&manifest)?;
    let root = manifest
        .entries
        .iter()
        .find(|entry| entry.relative_path == ".")
        .expect("validated journal snapshot manifest has a root entry");
    let live_root = data_directory.join("channel-journals");
    let backup_root = snapshot_directory.join("tree").join("channel-journals");
    let stage_root = snapshot_directory.join(JOURNAL_RESTORE_STAGE_NAME);
    let quarantine_root = snapshot_directory.join(JOURNAL_RESTORE_QUARANTINE_NAME);

    if path_entry_exists(&quarantine_root)? {
        if root.present && tree_matches_manifest(&live_root, &manifest)? {
            remove_tree(&quarantine_root)?;
            remove_tree(&stage_root)?;
            return Ok(());
        }
        if !root.present {
            if path_entry_exists(&live_root)? {
                return Err(
                    "journal rollback recovery found an unexpected live tree beside quarantine"
                        .into(),
                );
            }
            remove_tree(&quarantine_root)?;
            remove_tree(&stage_root)?;
            return Ok(());
        }
    } else if tree_matches_manifest(&live_root, &manifest)? {
        remove_tree(&stage_root)?;
        return Ok(());
    }

    remove_tree(&stage_root)?;
    if root.present {
        require_real_directory(&backup_root, "channel-journal snapshot tree")?;
        ensure_real_directory(&stage_root)?;
        materialize_snapshot_tree(&backup_root, &stage_root, &manifest)?;
        if !tree_matches_manifest(&stage_root, &manifest)? {
            return Err("staged channel-journal restore does not match its manifest".into());
        }
    }

    if !path_entry_exists(&quarantine_root)? && path_entry_exists(&live_root)? {
        move_tree(&live_root, &quarantine_root).map_err(|error| {
            format!("could not quarantine candidate channel-journal tree: {error}")
        })?;
    }
    if root.present && !path_entry_exists(&live_root)? {
        move_tree(&stage_root, &live_root)
            .map_err(|error| format!("could not publish restored channel-journal tree: {error}"))?;
    }
    if root.present {
        if !tree_matches_manifest(&live_root, &manifest)? {
            if !path_entry_exists(&live_root)? && path_entry_exists(&quarantine_root)? {
                let _ = move_tree(&quarantine_root, &live_root);
            }
            return Err("published channel-journal restore does not match its manifest".into());
        }
    } else if path_entry_exists(&live_root)? {
        return Err("absent channel-journal snapshot restored an unexpected live tree".into());
    }
    remove_tree(&quarantine_root)?;
    remove_tree(&stage_root)?;
    Ok(())
}

fn materialize_snapshot_tree(
    backup_root: &Path,
    stage_root: &Path,
    manifest: &JournalSnapshotManifest,
) -> Result<(), String> {
    let mut directories = manifest
        .entries
        .iter()
        .filter(|entry| entry.present && entry.directory && entry.relative_path != ".")
        .collect::<Vec<_>>();
    directories.sort_by_key(|entry| entry.relative_path.matches('/').count());
    for entry in directories {
        ensure_real_directory(&stage_root.join(snapshot_relative_path(&entry.relative_path)?))?;
    }
    for entry in manifest
        .entries
        .iter()
        .filter(|entry| entry.present && !entry.directory)
    {
        let relative = snapshot_relative_path(&entry.relative_path)?;
        let source = backup_root.join(&relative);
        let destination = stage_root.join(&relative);
        let (size_bytes, sha256) = copy_hashed_file(&source, &destination)?;
        if size_bytes != entry.size_bytes || !sha256.eq_ignore_ascii_case(&entry.sha256) {
            return Err(format!(
                "channel-journal snapshot entry does not match its manifest: {}",
                entry.relative_path
            ));
        }
    }
    Ok(())
}

fn tree_matches_manifest(root: &Path, manifest: &JournalSnapshotManifest) -> Result<bool, String> {
    let expected_root = manifest
        .entries
        .iter()
        .find(|entry| entry.relative_path == ".")
        .expect("validated journal snapshot manifest has a root entry");
    let metadata = match std::fs::symlink_metadata(root) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(!expected_root.present);
        }
        Err(error) => return Err(format!("could not inspect channel-journal tree: {error}")),
    };
    if !expected_root.present || !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Ok(false);
    }
    let mut actual = Vec::new();
    collect_tree_evidence(root, ".", &mut actual)?;
    actual.sort_by(|left, right| left.relative_path.cmp(&right.relative_path));
    Ok(actual == manifest.entries)
}

fn collect_tree_evidence(
    path: &Path,
    relative_path: &str,
    entries: &mut Vec<JournalSnapshotEntry>,
) -> Result<(), String> {
    if entries.len() >= MAX_JOURNAL_SNAPSHOT_ENTRIES {
        return Err("channel-journal tree exceeds the snapshot entry bound".into());
    }
    let metadata = std::fs::symlink_metadata(path)
        .map_err(|error| format!("could not inspect channel-journal tree: {error}"))?;
    if metadata.file_type().is_symlink() {
        return Err("channel-journal tree contains a symlink".into());
    }
    if metadata.is_dir() {
        entries.push(JournalSnapshotEntry {
            relative_path: relative_path.into(),
            present: true,
            directory: true,
            size_bytes: 0,
            sha256: String::new(),
        });
        let mut children = std::fs::read_dir(path)
            .map_err(|error| format!("could not list channel-journal tree: {error}"))?
            .map(|entry| {
                entry
                    .map(|entry| entry.path())
                    .map_err(|error| format!("could not inspect channel-journal tree: {error}"))
            })
            .collect::<Result<Vec<_>, String>>()?;
        children.sort();
        for child in children {
            let name = child
                .file_name()
                .and_then(|name| name.to_str())
                .ok_or_else(|| "channel-journal path is not valid UTF-8".to_string())?;
            let child_relative = if relative_path == "." {
                name.to_string()
            } else {
                format!("{relative_path}/{name}")
            };
            validate_snapshot_relative_path(&child_relative, false)?;
            collect_tree_evidence(&child, &child_relative, entries)?;
        }
    } else if metadata.is_file() {
        let (size_bytes, sha256) = hash_file(path)?;
        entries.push(JournalSnapshotEntry {
            relative_path: relative_path.into(),
            present: true,
            directory: false,
            size_bytes,
            sha256,
        });
    } else {
        return Err("channel-journal tree contains an unsupported entry".into());
    }
    Ok(())
}

fn discard_journal_snapshot(
    transaction_directory: &Path,
    snapshot: &JournalSnapshot,
) -> Result<(), String> {
    let snapshot_directory = journal_snapshot_directory(transaction_directory, snapshot)?;
    if snapshot_directory.exists() {
        require_real_directory(&snapshot_directory, "journal snapshot directory")?;
    }
    remove_tree(&snapshot_directory)
}

fn discover_journal_snapshot(
    transaction_path: &Path,
    transaction: &MaintenanceTransactionRecord,
) -> Result<Option<JournalSnapshot>, String> {
    if transaction.journal_snapshot.is_some() {
        return Ok(transaction.journal_snapshot.clone());
    }
    let transaction_directory = transaction_path
        .parent()
        .ok_or_else(|| "maintenance transaction has no parent directory".to_string())?;
    let snapshot = JournalSnapshot {
        schema_version: JOURNAL_SNAPSHOT_SCHEMA_VERSION,
        relative_directory: format!(
            "{}{}",
            transaction.request.transaction_id, JOURNAL_SNAPSHOT_DIRECTORY_SUFFIX
        ),
        manifest_name: JOURNAL_SNAPSHOT_MANIFEST_NAME.into(),
    };
    snapshot.validate()?;
    let snapshot_directory = journal_snapshot_directory(transaction_directory, &snapshot)?;
    match std::fs::symlink_metadata(&snapshot_directory) {
        Ok(metadata) => {
            if !metadata.is_dir() || metadata.file_type().is_symlink() {
                return Err("journal snapshot directory is not a real directory".into());
            }
            let manifest_result = read_bounded_file(
                &snapshot_directory.join(&snapshot.manifest_name),
                32 * 1024 * 1024,
                "channel-journal snapshot manifest",
            )
            .and_then(|manifest_bytes| {
                let manifest: JournalSnapshotManifest = serde_json::from_slice(&manifest_bytes)
                    .map_err(|error| {
                        format!("channel-journal snapshot manifest is invalid: {error}")
                    })?;
                validate_journal_snapshot_manifest(&manifest)
            });
            match manifest_result {
                Ok(()) => Ok(Some(snapshot)),
                Err(_) if transaction.phase == MaintenancePhase::ServiceStopped => {
                    remove_tree(&snapshot_directory)?;
                    Ok(None)
                }
                Err(error) => Err(error),
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!(
            "could not inspect journal snapshot directory: {error}"
        )),
    }
}

fn journal_snapshot_directory(
    transaction_directory: &Path,
    snapshot: &JournalSnapshot,
) -> Result<PathBuf, String> {
    snapshot.validate()?;
    let directory = transaction_directory.join(&snapshot.relative_directory);
    if directory.parent() != Some(transaction_directory) {
        return Err("journal snapshot directory escapes its transaction directory".into());
    }
    Ok(directory)
}

fn validate_journal_snapshot_manifest(manifest: &JournalSnapshotManifest) -> Result<(), String> {
    if manifest.schema_version != JOURNAL_SNAPSHOT_SCHEMA_VERSION
        || manifest.entries.is_empty()
        || manifest.entries.len() > MAX_JOURNAL_SNAPSHOT_ENTRIES
    {
        return Err("channel-journal snapshot manifest fields are invalid".into());
    }
    let mut paths = std::collections::BTreeSet::new();
    for entry in &manifest.entries {
        validate_snapshot_relative_path(&entry.relative_path, true)?;
        if !paths.insert(entry.relative_path.clone()) {
            return Err("channel-journal snapshot manifest contains duplicate paths".into());
        }
        if !entry.present {
            if entry.relative_path != "."
                || !entry.directory
                || entry.size_bytes != 0
                || !entry.sha256.is_empty()
            {
                return Err("channel-journal snapshot absence entry is invalid".into());
            }
        } else if entry.directory {
            if entry.size_bytes != 0 || !entry.sha256.is_empty() {
                return Err("channel-journal snapshot directory entry is invalid".into());
            }
        } else if !is_sha256(&entry.sha256) {
            return Err("channel-journal snapshot file hash is invalid".into());
        }
    }
    let root = manifest
        .entries
        .iter()
        .find(|entry| entry.relative_path == ".")
        .ok_or_else(|| "channel-journal snapshot manifest has no root entry".to_string())?;
    if !root.directory {
        return Err("channel-journal snapshot root entry is invalid".into());
    }
    if !root.present && manifest.entries.len() != 1 {
        return Err("absent channel-journal snapshot root has child entries".into());
    }
    for entry in manifest
        .entries
        .iter()
        .filter(|entry| entry.relative_path != ".")
    {
        let parent = entry
            .relative_path
            .rsplit_once('/')
            .map_or(".", |(parent, _)| parent);
        let Some(parent) = manifest
            .entries
            .iter()
            .find(|candidate| candidate.relative_path == parent)
        else {
            return Err("channel-journal snapshot entry has no parent directory".into());
        };
        if !parent.present || !parent.directory {
            return Err("channel-journal snapshot entry parent is invalid".into());
        }
    }
    Ok(())
}

fn validate_snapshot_relative_path(path: &str, allow_root: bool) -> Result<(), String> {
    if path.is_empty()
        || path.len() > MAX_JOURNAL_SNAPSHOT_PATH_BYTES
        || path.contains('\\')
        || path.chars().any(char::is_control)
        || (!allow_root && path == ".")
    {
        return Err("channel-journal snapshot path is invalid".into());
    }
    if path == "." {
        return Ok(());
    }
    let candidate = Path::new(path);
    let components = candidate.components().collect::<Vec<_>>();
    if candidate.is_absolute()
        || components.len() > MAX_JOURNAL_SNAPSHOT_PATH_DEPTH
        || components
            .iter()
            .any(|component| !matches!(component, std::path::Component::Normal(_)))
    {
        return Err("channel-journal snapshot path is invalid".into());
    }
    Ok(())
}

fn snapshot_relative_path(path: &str) -> Result<PathBuf, String> {
    validate_snapshot_relative_path(path, false)?;
    Ok(path.split('/').collect())
}

fn copy_hashed_file(source: &Path, destination: &Path) -> Result<(u64, String), String> {
    let metadata = std::fs::symlink_metadata(source)
        .map_err(|error| format!("could not inspect channel-journal file: {error}"))?;
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        return Err("channel-journal snapshot encountered a non-file entry".into());
    }
    if let Some(parent) = destination.parent() {
        ensure_real_directory(parent)?;
    }
    let mut input = File::open(source)
        .map_err(|error| format!("could not open channel-journal file: {error}"))?;
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(destination)
        .map_err(|error| format!("could not create channel-journal snapshot file: {error}"))?;
    let mut digest = Sha256::new();
    let mut total = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    let result = (|| {
        loop {
            let count = input
                .read(&mut buffer)
                .map_err(|error| format!("could not read channel-journal file: {error}"))?;
            if count == 0 {
                break;
            }
            output.write_all(&buffer[..count]).map_err(|error| {
                format!("could not write channel-journal snapshot file: {error}")
            })?;
            digest.update(&buffer[..count]);
            total = total.saturating_add(count as u64);
        }
        output.sync_all().map_err(|error| {
            format!("could not synchronize channel-journal snapshot file: {error}")
        })?;
        Ok::<_, String>(format!("{:X}", digest.finalize()))
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(destination);
    }
    Ok((total, result?))
}

fn hash_file(path: &Path) -> Result<(u64, String), String> {
    let metadata = std::fs::symlink_metadata(path)
        .map_err(|error| format!("could not inspect channel-journal file: {error}"))?;
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        return Err("channel-journal evidence encountered a non-file entry".into());
    }
    let mut input = File::open(path)
        .map_err(|error| format!("could not open channel-journal file: {error}"))?;
    let mut digest = Sha256::new();
    let mut total = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = input
            .read(&mut buffer)
            .map_err(|error| format!("could not read channel-journal file: {error}"))?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
        total = total.saturating_add(count as u64);
    }
    Ok((total, format!("{:X}", digest.finalize())))
}

fn require_real_directory(path: &Path, label: &str) -> Result<(), String> {
    let metadata = std::fs::symlink_metadata(path)
        .map_err(|error| format!("could not inspect {label}: {error}"))?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err(format!("{label} is not a real directory"));
    }
    Ok(())
}

fn path_entry_exists(path: &Path) -> Result<bool, String> {
    match std::fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(format!("could not inspect maintenance tree entry: {error}")),
    }
}

#[allow(clippy::permissions_set_readonly_false)]
fn remove_tree(path: &Path) -> Result<(), String> {
    let metadata = match std::fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(format!("could not inspect maintenance tree: {error}")),
    };
    if metadata.file_type().is_symlink() || metadata.is_file() {
        if metadata.is_file() && !metadata.file_type().is_symlink() {
            let mut permissions = metadata.permissions();
            permissions.set_readonly(false);
            let _ = std::fs::set_permissions(path, permissions);
        }
        return std::fs::remove_file(path)
            .or_else(|file_error| {
                if metadata.file_type().is_symlink() {
                    std::fs::remove_dir(path).map_err(|_| file_error)
                } else {
                    Err(file_error)
                }
            })
            .map_err(|error| format!("could not remove maintenance tree entry: {error}"));
    }
    if !metadata.is_dir() {
        return Err("maintenance tree contains an unsupported entry".into());
    }
    let children = std::fs::read_dir(path)
        .map_err(|error| format!("could not list maintenance tree: {error}"))?;
    for child in children {
        let child =
            child.map_err(|error| format!("could not inspect maintenance tree: {error}"))?;
        remove_tree(&child.path())?;
    }
    std::fs::remove_dir(path)
        .map_err(|error| format!("could not remove maintenance tree directory: {error}"))
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
    let restore_candidate_receipts = matches!(
        transaction.phase,
        MaintenancePhase::CommitStarted | MaintenancePhase::Committed
    );
    advance(
        transaction_path,
        transaction,
        MaintenancePhase::RollbackStarted,
        now_unix_ms,
        &format!("Candidate failed proof; rollback started: {activation_error}"),
    )?;
    let result = (|| {
        host.stop_station(Duration::from_secs(30))?;
        let previous_directory = transaction
            .previous_receipt
            .active
            .directory(application_root)?;
        host.configure_station(&previous_directory.join("stationd.exe"), database_path)?;
        if let Some(snapshot) = &transaction.journal_snapshot {
            restore_journal_snapshot(
                database_path
                    .parent()
                    .ok_or_else(|| "station database has no data directory".to_string())?,
                transaction_path
                    .parent()
                    .ok_or_else(|| "maintenance transaction has no parent directory".to_string())?,
                snapshot,
            )?;
        }
        if restore_candidate_receipts {
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
            )?;
        }
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
            if let Some(snapshot) = &transaction.journal_snapshot {
                discard_journal_snapshot(
                    transaction_path.parent().ok_or_else(|| {
                        "maintenance transaction has no parent directory".to_string()
                    })?,
                    snapshot,
                )?;
            }
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
    if transaction.journal_snapshot.is_none() {
        transaction.journal_snapshot = discover_journal_snapshot(transaction_path, &transaction)?;
        if transaction.journal_snapshot.is_some() {
            write_transaction(transaction_path, &transaction)?;
        }
    }
    if transaction.phase == MaintenancePhase::RolledBack {
        if let Some(snapshot) = &transaction.journal_snapshot {
            discard_journal_snapshot(
                transaction_path
                    .parent()
                    .ok_or_else(|| "maintenance transaction has no parent directory".to_string())?,
                snapshot,
            )?;
        }
        return Ok(transaction);
    }
    if transaction.phase == MaintenancePhase::Committed {
        if let Some(snapshot) = &transaction.journal_snapshot {
            discard_journal_snapshot(
                transaction_path
                    .parent()
                    .ok_or_else(|| "maintenance transaction has no parent directory".to_string())?,
                snapshot,
            )?;
        }
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
        host.stop_station(Duration::from_secs(30))?;
        let previous_directory = transaction
            .previous_receipt
            .active
            .directory(application_root)?;
        host.configure_station(&previous_directory.join("stationd.exe"), database_path)?;
        if let Some(snapshot) = &transaction.journal_snapshot {
            restore_journal_snapshot(
                database_path
                    .parent()
                    .ok_or_else(|| "station database has no data directory".to_string())?,
                transaction_path
                    .parent()
                    .ok_or_else(|| "maintenance transaction has no parent directory".to_string())?,
                snapshot,
            )?;
        }
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
            if let Some(snapshot) = &transaction.journal_snapshot {
                discard_journal_snapshot(
                    transaction_path.parent().ok_or_else(|| {
                        "maintenance transaction has no parent directory".to_string()
                    })?,
                    snapshot,
                )?;
            }
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

pub fn latest_maintenance_transaction(
    data_directory: &Path,
) -> Result<Option<MaintenanceTransactionRecord>, String> {
    let directory = data_directory
        .join("updates")
        .join(TRANSACTION_DIRECTORY_NAME);
    let metadata = match std::fs::symlink_metadata(&directory) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(format!("could not inspect maintenance history: {error}")),
    };
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err("maintenance history is not a real directory".into());
    }
    let mut latest: Option<MaintenanceTransactionRecord> = None;
    let mut count = 0_usize;
    for entry in std::fs::read_dir(&directory)
        .map_err(|error| format!("could not list maintenance history: {error}"))?
    {
        count += 1;
        if count > 1_000 {
            return Err("maintenance history exceeds its bounded entry count".into());
        }
        let entry =
            entry.map_err(|error| format!("could not inspect maintenance history: {error}"))?;
        let path = entry.path();
        let metadata = std::fs::symlink_metadata(&path)
            .map_err(|error| format!("could not inspect maintenance history entry: {error}"))?;
        if metadata.is_dir()
            && !metadata.file_type().is_symlink()
            && path
                .file_name()
                .and_then(|name| name.to_str())
                .and_then(|name| name.strip_suffix(JOURNAL_SNAPSHOT_DIRECTORY_SUFFIX))
                .is_some_and(|transaction_id| visible_identifier(transaction_id, 120))
        {
            continue;
        }
        if !metadata.is_file()
            || metadata.file_type().is_symlink()
            || path.extension().and_then(|value| value.to_str()) != Some("json")
        {
            return Err("maintenance history contains an unexpected entry".into());
        }
        let transaction = read_transaction(&path)?;
        let replace = latest.as_ref().is_none_or(|current| {
            (
                transaction.updated_at_unix_ms,
                &transaction.request.transaction_id,
            ) > (current.updated_at_unix_ms, &current.request.transaction_id)
        });
        if replace {
            latest = Some(transaction);
        }
    }
    Ok(latest)
}

pub fn read_active_slot_receipt(path: &Path) -> Result<ActiveSlotReceipt, String> {
    let bytes = read_bounded_file(path, 256 * 1024, "active-slot receipt")?;
    let receipt: ActiveSlotReceipt = serde_json::from_slice(&bytes)
        .map_err(|error| format!("active-slot receipt is invalid: {error}"))?;
    receipt.validate()?;
    Ok(receipt)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifiedCandidateIdentity {
    pub product: String,
    pub version: String,
    pub source_commit: String,
    pub media_runtime_sha256: String,
    pub runtime_manifest_sha256: String,
}

#[derive(Debug, Deserialize)]
struct InstallReceiptIdentity {
    product: String,
    version: String,
    source_commit: String,
    media_runtime_sha256: String,
    runtime_manifest_sha256: String,
    #[serde(default)]
    tsduck_version: Option<String>,
    #[serde(default)]
    tsduck_source_url: Option<String>,
    #[serde(default)]
    tsduck_archive_name: Option<String>,
    #[serde(default)]
    tsduck_runtime_sha256: Option<String>,
    #[serde(default)]
    tsduck_runtime_manifest_sha256: Option<String>,
    #[serde(default)]
    installed_at_unix_ms: Option<i64>,
    #[serde(default)]
    health_uri: Option<String>,
}

pub fn read_verified_candidate_identity(
    data_directory: &Path,
    application_root: &Path,
) -> Result<VerifiedCandidateIdentity, String> {
    let install_bytes = read_bounded_file(
        &data_directory.join(INSTALL_RECEIPT_NAME),
        64 * 1024,
        "installation receipt",
    )?;
    let install: InstallReceiptIdentity = serde_json::from_slice(&install_bytes)
        .map_err(|error| format!("installation receipt is invalid: {error}"))?;
    let _receipt_metadata = (&install.installed_at_unix_ms, &install.health_uri);
    if install.product != "TownLight Station" {
        return Err("installation receipt product identity is invalid".into());
    }
    if !is_commit_hash_lowercase(&install.source_commit) {
        return Err("installation receipt source commit is not canonical lowercase".into());
    }
    if !is_sha256(&install.media_runtime_sha256) {
        return Err("installation receipt runtime SHA-256 is invalid".into());
    }
    if !is_sha256_lowercase(&install.runtime_manifest_sha256) {
        return Err("installation receipt runtime manifest SHA-256 is invalid".into());
    }
    let tsduck_identity = match (
        install.tsduck_version.as_deref(),
        install.tsduck_source_url.as_deref(),
        install.tsduck_archive_name.as_deref(),
        install.tsduck_runtime_sha256.as_deref(),
        install.tsduck_runtime_manifest_sha256.as_deref(),
    ) {
        (
            Some(version),
            Some(source_url),
            Some(archive_name),
            Some(runtime_sha256),
            Some(manifest_sha256),
        ) if version == TSDUCK_VERSION
            && source_url == TSDUCK_SOURCE_URL
            && archive_name == TSDUCK_ARCHIVE_NAME
            && runtime_sha256 == TSDUCK_ARCHIVE_SHA256
            && is_sha256_lowercase(manifest_sha256) =>
        {
            Some(manifest_sha256)
        }
        (None, None, None, None, None) => None,
        _ => return Err("installation receipt TSDuck identity is invalid".into()),
    };

    let active = read_active_slot_receipt(&data_directory.join(ACTIVE_SLOT_RECEIPT_NAME))?;
    let manifest = &active.active.package_manifest;
    if manifest.product != install.product
        || manifest.version != install.version
        || manifest.source_commit != install.source_commit
    {
        return Err("installation and active-slot receipt identities do not agree".into());
    }
    verify_active_application(application_root, &active.active)
        .map_err(|error| format!("active application verification failed: {error}"))?;
    let runtime_manifest_path = data_directory.join(INSTALLED_RUNTIME_MANIFEST_NAME);
    let runtime_manifest_bytes = read_bounded_file(
        &runtime_manifest_path,
        MAX_RUNTIME_MANIFEST_BYTES,
        "installed runtime manifest",
    )?;
    let actual_manifest_sha256 = sha256_lowercase(&runtime_manifest_bytes);
    if actual_manifest_sha256 != install.runtime_manifest_sha256 {
        return Err(
            "installation receipt runtime manifest SHA-256 does not match the manifest".into(),
        );
    }
    let runtime_manifest: InstalledRuntimeManifest =
        serde_json::from_slice(&runtime_manifest_bytes)
            .map_err(|error| format!("installed runtime manifest is invalid: {error}"))?;
    runtime_manifest.validate()?;
    if runtime_manifest.product != install.product
        || runtime_manifest.runtime_package_sha256
            != install.media_runtime_sha256.to_ascii_lowercase()
    {
        return Err("installation and runtime manifest identities do not agree".into());
    }
    verify_installed_runtime(
        &application_root.join("runtime").join("gstreamer"),
        &runtime_manifest,
    )?;
    if let Some(tsduck_manifest_sha256) = tsduck_identity {
        let tsduck_runtime_manifest_bytes = read_bounded_file(
            &data_directory.join(TSDUCK_RUNTIME_MANIFEST_NAME),
            MAX_RUNTIME_MANIFEST_BYTES,
            "installed TSDuck runtime manifest",
        )?;
        if sha256_lowercase(&tsduck_runtime_manifest_bytes) != tsduck_manifest_sha256 {
            return Err(
                "installation receipt TSDuck runtime manifest SHA-256 does not match the manifest"
                    .into(),
            );
        }
        let tsduck_runtime_manifest: InstalledRuntimeManifest =
            serde_json::from_slice(&tsduck_runtime_manifest_bytes).map_err(|error| {
                format!("installed TSDuck runtime manifest is invalid: {error}")
            })?;
        tsduck_runtime_manifest.validate()?;
        if tsduck_runtime_manifest.product != install.product
            || tsduck_runtime_manifest.runtime_package_sha256 != TSDUCK_ARCHIVE_SHA256
            || REQUIRED_TSDUCK_RUNTIME_FILES.iter().any(|required| {
                !tsduck_runtime_manifest
                    .files
                    .iter()
                    .any(|file| file.path == *required)
            })
        {
            return Err("installation and TSDuck runtime manifest identities do not agree".into());
        }
        verify_installed_runtime(
            &application_root.join("runtime").join("tsduck"),
            &tsduck_runtime_manifest,
        )
        .map_err(|error| format!("TSDuck runtime verification failed: {error}"))?;
    }
    Ok(VerifiedCandidateIdentity {
        product: install.product,
        version: install.version,
        source_commit: install.source_commit,
        media_runtime_sha256: install.media_runtime_sha256.to_ascii_lowercase(),
        runtime_manifest_sha256: install.runtime_manifest_sha256,
    })
}

pub fn write_installed_runtime_manifest(
    runtime_root: &Path,
    output_path: &Path,
    version: &str,
    source_commit: &str,
    runtime_package_sha256: &str,
) -> Result<String, String> {
    if Version::parse(version).is_err()
        || !is_commit_hash_lowercase(source_commit)
        || !is_sha256_lowercase(runtime_package_sha256)
    {
        return Err("installed runtime manifest identity is invalid".into());
    }
    let files = enumerate_runtime_files(runtime_root)?;
    let mut manifest_files = Vec::with_capacity(files.len());
    for (path, full_path, size_bytes) in files {
        let sha256 = hash_runtime_file(&full_path)?;
        manifest_files.push(InstalledRuntimeFile {
            path,
            size_bytes,
            sha256,
        });
    }
    let manifest = InstalledRuntimeManifest {
        schema_version: INSTALLED_RUNTIME_MANIFEST_SCHEMA_VERSION,
        product: "TownLight Station".into(),
        version: version.into(),
        source_commit: source_commit.into(),
        runtime_package_sha256: runtime_package_sha256.into(),
        files: manifest_files,
    };
    manifest.validate()?;
    let bytes = serde_json::to_vec(&manifest)
        .map_err(|error| format!("could not encode installed runtime manifest: {error}"))?;
    if bytes.len() as u64 > MAX_RUNTIME_MANIFEST_BYTES {
        return Err("installed runtime manifest exceeds its size bound".into());
    }
    let digest = sha256_lowercase(&bytes);
    write_bytes_atomic(output_path, &bytes, "installed runtime manifest")?;
    Ok(digest)
}

fn verify_installed_runtime(
    runtime_root: &Path,
    manifest: &InstalledRuntimeManifest,
) -> Result<(), String> {
    let actual_files = enumerate_runtime_files(runtime_root)?;
    let expected_paths = manifest
        .files
        .iter()
        .map(|file| file.path.clone())
        .collect::<BTreeSet<_>>();
    let actual_paths = actual_files
        .iter()
        .map(|(path, _, _)| path.clone())
        .collect::<BTreeSet<_>>();
    if actual_paths != expected_paths {
        return Err("installed runtime file set does not match its manifest".into());
    }
    let expected = manifest
        .files
        .iter()
        .map(|file| (file.path.as_str(), file))
        .collect::<std::collections::BTreeMap<_, _>>();
    for (path, full_path, size_bytes) in actual_files {
        let file = expected
            .get(path.as_str())
            .ok_or_else(|| "installed runtime contains an unexpected file".to_string())?;
        if size_bytes != file.size_bytes || hash_runtime_file(&full_path)? != file.sha256 {
            return Err(format!(
                "installed runtime file does not match its manifest: {path}"
            ));
        }
    }
    Ok(())
}

fn enumerate_runtime_files(runtime_root: &Path) -> Result<Vec<(String, PathBuf, u64)>, String> {
    reject_reparse_path_chain(runtime_root)?;
    let metadata = std::fs::symlink_metadata(runtime_root)
        .map_err(|error| format!("could not inspect installed runtime root: {error}"))?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() || is_reparse_point(&metadata) {
        return Err("installed runtime root is not a real directory".into());
    }
    let mut stack = vec![(runtime_root.to_path_buf(), String::new())];
    let mut files = Vec::new();
    let mut total = 0_u64;
    let mut entry_count = 0_usize;
    while let Some((directory, relative_directory)) = stack.pop() {
        let entries = std::fs::read_dir(&directory)
            .map_err(|error| format!("could not enumerate installed runtime: {error}"))?;
        for entry in entries {
            let entry =
                entry.map_err(|error| format!("could not enumerate installed runtime: {error}"))?;
            entry_count = entry_count.saturating_add(1);
            if entry_count > MAX_RUNTIME_MANIFEST_ENTRIES {
                return Err("installed runtime contains too many entries".into());
            }
            let entry_path = entry.path();
            let metadata = std::fs::symlink_metadata(&entry_path)
                .map_err(|error| format!("could not inspect installed runtime entry: {error}"))?;
            if metadata.file_type().is_symlink() || is_reparse_point(&metadata) {
                return Err("installed runtime contains a symlink or reparse point".into());
            }
            let name = entry
                .file_name()
                .into_string()
                .map_err(|_| "installed runtime contains a non-Unicode entry name".to_string())?;
            let relative_path = if relative_directory.is_empty() {
                name
            } else {
                format!("{relative_directory}/{name}")
            };
            if metadata.is_dir() {
                stack.push((entry_path, relative_path));
                continue;
            }
            if !metadata.is_file() {
                return Err("installed runtime contains a non-file entry".into());
            }
            validate_runtime_relative_path(&relative_path)?;
            if metadata.len() > MAX_RUNTIME_FILE_BYTES {
                return Err("installed runtime file size is outside its bounds".into());
            }
            total = total
                .checked_add(metadata.len())
                .ok_or_else(|| "installed runtime total size overflowed".to_string())?;
            if total > MAX_RUNTIME_TOTAL_BYTES {
                return Err("installed runtime total size exceeds its bound".into());
            }
            files.push((relative_path, entry_path, metadata.len()));
            if files.len() > MAX_RUNTIME_MANIFEST_ENTRIES {
                return Err("installed runtime contains too many files".into());
            }
        }
    }
    files.sort_by(|left, right| left.0.cmp(&right.0));
    Ok(files)
}

fn reject_reparse_path_chain(path: &Path) -> Result<(), String> {
    if !path.is_absolute() {
        return Err("installed runtime root must be absolute".into());
    }
    let mut ancestors = path
        .ancestors()
        .filter(|ancestor| !ancestor.as_os_str().is_empty())
        .collect::<Vec<_>>();
    ancestors.reverse();
    for ancestor in ancestors {
        let metadata = std::fs::symlink_metadata(ancestor)
            .map_err(|error| format!("could not inspect installed runtime path chain: {error}"))?;
        if metadata.file_type().is_symlink() || is_reparse_point(&metadata) {
            return Err("installed runtime path chain contains a symlink or reparse point".into());
        }
    }
    Ok(())
}

fn validate_runtime_relative_path(path: &str) -> Result<(), String> {
    if path.is_empty()
        || path.starts_with('/')
        || path.contains('\\')
        || path.contains("//")
        || path.ends_with('/')
        || Path::new(path).is_absolute()
        || path
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == ".." || part.contains(':'))
    {
        return Err(format!(
            "installed runtime manifest path is not canonical: {path}"
        ));
    }
    Ok(())
}

fn hash_runtime_file(path: &Path) -> Result<String, String> {
    let mut input = File::open(path)
        .map_err(|error| format!("could not open installed runtime file: {error}"))?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = input
            .read(&mut buffer)
            .map_err(|error| format!("could not read installed runtime file: {error}"))?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn sha256_lowercase(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn is_sha256_lowercase(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte: u8| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[cfg(windows)]
fn is_reparse_point(metadata: &std::fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;
    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
    metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
}

#[cfg(not(windows))]
fn is_reparse_point(_metadata: &std::fs::Metadata) -> bool {
    false
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
        #[serde(default)]
        runtime_manifest_sha256: Option<String>,
        #[serde(default)]
        tsduck_version: Option<String>,
        #[serde(default)]
        tsduck_source_url: Option<String>,
        #[serde(default)]
        tsduck_archive_name: Option<String>,
        #[serde(default)]
        tsduck_runtime_sha256: Option<String>,
        #[serde(default)]
        tsduck_runtime_manifest_sha256: Option<String>,
    }
    #[derive(Serialize)]
    struct InstallIdentity<'a> {
        product: &'static str,
        version: &'a str,
        source_commit: &'a str,
        installed_at_unix_ms: i64,
        #[serde(skip_serializing_if = "Option::is_none")]
        media_runtime_sha256: Option<&'a str>,
        #[serde(skip_serializing_if = "Option::is_none")]
        runtime_manifest_sha256: Option<&'a str>,
        #[serde(skip_serializing_if = "Option::is_none")]
        tsduck_version: Option<&'a str>,
        #[serde(skip_serializing_if = "Option::is_none")]
        tsduck_source_url: Option<&'a str>,
        #[serde(skip_serializing_if = "Option::is_none")]
        tsduck_archive_name: Option<&'a str>,
        #[serde(skip_serializing_if = "Option::is_none")]
        tsduck_runtime_sha256: Option<&'a str>,
        #[serde(skip_serializing_if = "Option::is_none")]
        tsduck_runtime_manifest_sha256: Option<&'a str>,
        health_uri: &'static str,
    }
    let previous_path = data_directory.join(INSTALL_RECEIPT_NAME);
    let previous = read_bounded_file(&previous_path, 64 * 1024, "installation receipt")
        .ok()
        .and_then(|bytes| serde_json::from_slice::<PreviousInstallIdentity>(&bytes).ok());
    let previous_runtime = previous
        .as_ref()
        .and_then(|identity| identity.media_runtime_sha256.as_ref())
        .filter(|hash| is_sha256(hash))
        .map(|hash| hash.to_ascii_lowercase());
    let previous_manifest = previous
        .as_ref()
        .and_then(|identity| identity.runtime_manifest_sha256.as_ref())
        .filter(|hash| is_sha256_lowercase(hash))
        .map(|hash| hash.to_ascii_lowercase());
    let previous_tsduck = match previous.as_ref().map(|identity| {
        (
            identity.tsduck_version.as_deref(),
            identity.tsduck_source_url.as_deref(),
            identity.tsduck_archive_name.as_deref(),
            identity.tsduck_runtime_sha256.as_deref(),
            identity.tsduck_runtime_manifest_sha256.as_deref(),
        )
    }) {
        None | Some((None, None, None, None, None)) => None,
        Some((
            Some(version),
            Some(source_url),
            Some(archive_name),
            Some(runtime_sha256),
            Some(manifest_sha256),
        )) if version == TSDUCK_VERSION
            && source_url == TSDUCK_SOURCE_URL
            && archive_name == TSDUCK_ARCHIVE_NAME
            && runtime_sha256 == TSDUCK_ARCHIVE_SHA256
            && is_sha256_lowercase(manifest_sha256) =>
        {
            Some((
                version,
                source_url,
                archive_name,
                runtime_sha256,
                manifest_sha256,
            ))
        }
        _ => return Err("existing installation receipt TSDuck identity is invalid".into()),
    };
    write_json_atomic(
        &previous_path,
        &InstallIdentity {
            product: "TownLight Station",
            version: &application.package_manifest.version,
            source_commit: &application.package_manifest.source_commit,
            installed_at_unix_ms: now_unix_ms,
            media_runtime_sha256: previous_runtime.as_deref(),
            runtime_manifest_sha256: previous_manifest.as_deref(),
            tsduck_version: previous_tsduck.map(|identity| identity.0),
            tsduck_source_url: previous_tsduck.map(|identity| identity.1),
            tsduck_archive_name: previous_tsduck.map(|identity| identity.2),
            tsduck_runtime_sha256: previous_tsduck.map(|identity| identity.3),
            tsduck_runtime_manifest_sha256: previous_tsduck.map(|identity| identity.4),
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

fn write_bytes_atomic(path: &Path, bytes: &[u8], label: &str) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| format!("{label} has no parent directory"))?;
    ensure_real_directory(parent)?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("runtime-manifest"),
        std::process::id()
    ));
    let result = (|| {
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
            .map_err(|error| format!("could not create temporary {label}: {error}"))?;
        output
            .write_all(bytes)
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

fn move_tree(source: &Path, destination: &Path) -> std::io::Result<()> {
    use std::os::windows::ffi::OsStrExt;

    if destination.exists() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::AlreadyExists,
            "tree move destination already exists",
        ));
    }
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
    // SAFETY: both vectors contain valid NUL-terminated UTF-16 strings for the call.
    let status = unsafe {
        MoveFileExW(
            source.as_ptr(),
            destination.as_ptr(),
            MOVEFILE_WRITE_THROUGH,
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

fn is_commit_hash_lowercase(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
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

#[cfg(test)]
mod tests {
    use std::time::{SystemTime, UNIX_EPOCH};

    use super::*;

    fn root(name: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock is before Unix epoch")
            .as_nanos();
        std::env::temp_dir().join(format!("townlight-maintenance-{name}-{nonce}"))
    }

    #[test]
    fn corrupt_snapshot_is_rejected_before_the_live_tree_is_touched() {
        let root = root("corrupt-snapshot");
        let data = root.join("data");
        let transactions = data.join("updates").join(TRANSACTION_DIRECTORY_NAME);
        let live = data.join("channel-journals");
        ensure_real_directory(&transactions).unwrap();
        ensure_real_directory(&live).unwrap();
        std::fs::write(live.join("channel.tlj"), b"prior-v1").unwrap();
        let snapshot = create_journal_snapshot(&data, &transactions, "corrupt-proof").unwrap();
        std::fs::write(live.join("channel.tlj"), b"candidate-v2").unwrap();
        let backup = transactions
            .join(&snapshot.relative_directory)
            .join("tree/channel-journals/channel.tlj");
        std::fs::write(backup, b"corrupt").unwrap();

        assert!(restore_journal_snapshot(&data, &transactions, &snapshot).is_err());
        assert_eq!(
            std::fs::read(live.join("channel.tlj")).unwrap(),
            b"candidate-v2"
        );
        remove_tree(&root).unwrap();
    }

    #[test]
    fn restore_resumes_after_the_candidate_tree_was_quarantined() {
        let root = root("resume-quarantine");
        let data = root.join("data");
        let transactions = data.join("updates").join(TRANSACTION_DIRECTORY_NAME);
        let live = data.join("channel-journals");
        ensure_real_directory(&transactions).unwrap();
        ensure_real_directory(&live).unwrap();
        std::fs::write(live.join("channel.tlj"), b"prior-v1").unwrap();
        let snapshot = create_journal_snapshot(&data, &transactions, "resume-proof").unwrap();
        std::fs::write(live.join("channel.tlj"), b"candidate-v2").unwrap();
        let snapshot_directory = transactions.join(&snapshot.relative_directory);
        let manifest: JournalSnapshotManifest = serde_json::from_slice(
            &read_bounded_file(
                &snapshot_directory.join(JOURNAL_SNAPSHOT_MANIFEST_NAME),
                32 * 1024 * 1024,
                "snapshot manifest",
            )
            .unwrap(),
        )
        .unwrap();
        let stage = snapshot_directory.join(JOURNAL_RESTORE_STAGE_NAME);
        ensure_real_directory(&stage).unwrap();
        materialize_snapshot_tree(
            &snapshot_directory.join("tree/channel-journals"),
            &stage,
            &manifest,
        )
        .unwrap();
        move_tree(
            &live,
            &snapshot_directory.join(JOURNAL_RESTORE_QUARANTINE_NAME),
        )
        .unwrap();

        restore_journal_snapshot(&data, &transactions, &snapshot).unwrap();
        assert_eq!(
            std::fs::read(live.join("channel.tlj")).unwrap(),
            b"prior-v1"
        );
        assert!(!snapshot_directory.join(JOURNAL_RESTORE_STAGE_NAME).exists());
        assert!(
            !snapshot_directory
                .join(JOURNAL_RESTORE_QUARANTINE_NAME)
                .exists()
        );
        remove_tree(&root).unwrap();
    }

    #[test]
    fn dot_only_snapshot_paths_cannot_escape_or_delete_transaction_storage() {
        let root = root("snapshot-confinement");
        let transactions = root.join("updates/maintenance-transactions");
        ensure_real_directory(&transactions).unwrap();
        let sentinel = root.join("updates/sentinel.txt");
        std::fs::write(&sentinel, b"retain").unwrap();
        for relative_directory in [".", "..", ".journal-snapshot", "...journal-snapshot"] {
            let snapshot = JournalSnapshot {
                schema_version: JOURNAL_SNAPSHOT_SCHEMA_VERSION,
                relative_directory: relative_directory.into(),
                manifest_name: JOURNAL_SNAPSHOT_MANIFEST_NAME.into(),
            };
            assert!(discard_journal_snapshot(&transactions, &snapshot).is_err());
            assert_eq!(std::fs::read(&sentinel).unwrap(), b"retain");
            assert!(transactions.is_dir());
        }
        remove_tree(&root).unwrap();
    }

    #[test]
    fn absent_snapshot_removes_a_dangling_live_root_symlink() {
        use std::os::windows::fs::symlink_file;

        let root = root("absent-dangling-root");
        let data = root.join("data");
        let transactions = data.join("updates").join(TRANSACTION_DIRECTORY_NAME);
        ensure_real_directory(&transactions).unwrap();
        let snapshot = create_journal_snapshot(&data, &transactions, "absent-proof").unwrap();
        if let Err(error) = symlink_file(root.join("missing-target"), data.join("channel-journals"))
        {
            if error.raw_os_error() == Some(1314) {
                remove_tree(&root).unwrap();
                return;
            }
            panic!("could not create dangling symlink fixture: {error}");
        }

        restore_journal_snapshot(&data, &transactions, &snapshot).unwrap();
        assert!(matches!(
            std::fs::symlink_metadata(data.join("channel-journals")),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound
        ));
        remove_tree(&root).unwrap();
    }
}
