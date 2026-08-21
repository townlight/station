use super::{
    ReleaseManifest, StagedPackageRecord, ensure_managed_directory, is_commit_hash, is_sha256,
    staged_package_path, validate_manifest, verify_package_file,
};
use semver::Version;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use zip::{CompressionMethod, ZipArchive};

pub const UPDATE_PACKAGE_SCHEMA_VERSION: u32 = 1;
const PACKAGE_MANIFEST_NAME: &str = "package-manifest.json";
const SLOT_MANIFEST_NAME: &str = "slot-manifest.json";
const MAX_PACKAGE_MANIFEST_BYTES: u64 = 64 * 1024;
const MAX_SLOT_FILE_BYTES: u64 = 512 * 1024 * 1024;
const MAX_SLOT_BYTES: u64 = 1024 * 1024 * 1024;
const REQUIRED_FILES: [&str; 3] = ["stationd.exe", "channel-worker.exe", "station-updater.exe"];

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UpdatePackageManifest {
    pub schema_version: u32,
    pub product: String,
    pub version: String,
    pub source_commit: String,
    pub files: Vec<UpdatePackageFile>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UpdatePackageFile {
    pub path: String,
    pub size_bytes: u64,
    pub sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PreparedSlotRecord {
    pub prepared_at_unix_ms: i64,
    pub manifest: ReleaseManifest,
    pub package_file_name: String,
    pub slot_directory_name: String,
}

impl PreparedSlotRecord {
    pub fn validate(&self) -> Result<(), &'static str> {
        let expected_package = format!("{}.tlu", self.manifest.package_sha256.to_ascii_lowercase());
        if self.prepared_at_unix_ms < self.manifest.published_at_unix_ms
            || self.prepared_at_unix_ms > self.manifest.expires_at_unix_ms
            || self.package_file_name != expected_package
            || self.slot_directory_name != slot_directory_name(&self.manifest)
            || validate_manifest(
                &self.manifest,
                &self.manifest.key_id,
                &self.manifest.release_channel,
                self.prepared_at_unix_ms,
            )
            .is_err()
        {
            return Err("prepared slot record fields are invalid");
        }
        Ok(())
    }
}

impl UpdatePackageManifest {
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != UPDATE_PACKAGE_SCHEMA_VERSION
            || self.product != "TownLight Station"
            || Version::parse(&self.version).is_err()
            || !is_commit_hash(&self.source_commit)
            || self.files.len() != REQUIRED_FILES.len()
        {
            return Err("update package identity does not match its signed release".into());
        }
        let mut paths = BTreeSet::new();
        let mut total = 0_u64;
        for file in &self.files {
            if !REQUIRED_FILES.contains(&file.path.as_str())
                || !paths.insert(file.path.as_str())
                || file.size_bytes == 0
                || file.size_bytes > MAX_SLOT_FILE_BYTES
                || !is_sha256(&file.sha256)
            {
                return Err("update package file manifest is invalid".into());
            }
            total = total
                .checked_add(file.size_bytes)
                .ok_or_else(|| "update package extracted size overflowed".to_string())?;
        }
        if total > MAX_SLOT_BYTES || REQUIRED_FILES.iter().any(|path| !paths.contains(path)) {
            return Err("update package required file set is invalid".into());
        }
        Ok(())
    }

    fn validate_against(&self, release: &ReleaseManifest) -> Result<(), String> {
        self.validate()?;
        if self.product != release.product
            || self.version != release.version
            || self.source_commit != release.source_commit
        {
            return Err("update package identity does not match its signed release".into());
        }
        Ok(())
    }
}

pub fn prepare_release_slot(
    staged: &StagedPackageRecord,
    updates_directory: &Path,
    slots_directory: &Path,
    prepared_at_unix_ms: i64,
) -> Result<PreparedSlotRecord, String> {
    staged.validate().map_err(str::to_string)?;
    if prepared_at_unix_ms < staged.staged_at_unix_ms {
        return Err("application slot cannot be prepared before package staging".into());
    }
    let record = PreparedSlotRecord {
        prepared_at_unix_ms,
        manifest: staged.manifest.clone(),
        package_file_name: staged.package_file_name.clone(),
        slot_directory_name: slot_directory_name(&staged.manifest),
    };
    record.validate().map_err(str::to_string)?;
    let package_path = staged_package_path(updates_directory, staged);
    verify_package_file(&package_path, &staged.manifest)?;
    ensure_managed_directory(slots_directory)?;
    let final_path = prepared_slot_path(slots_directory, &record);
    if final_path.exists() {
        verify_prepared_slot(slots_directory, &record)?;
        return Ok(record);
    }

    let temporary_path = create_slot_directory(slots_directory, &record)?;
    let result = (|| {
        let package_manifest = read_package_manifest(&package_path, &staged.manifest)?;
        extract_package(&package_path, &temporary_path, &package_manifest)?;
        make_slot_files_read_only(&temporary_path, &package_manifest)?;
        verify_slot_directory(&temporary_path, &package_manifest, &staged.manifest)?;
        match std::fs::rename(&temporary_path, &final_path) {
            Ok(()) => Ok(()),
            Err(_) if final_path.exists() => {
                verify_prepared_slot(slots_directory, &record)?;
                remove_slot_tree(&temporary_path);
                Ok(())
            }
            Err(error) => Err(format!(
                "could not publish immutable application slot: {error}"
            )),
        }
    })();
    if result.is_err() {
        remove_slot_tree(&temporary_path);
    }
    result?;
    verify_prepared_slot(slots_directory, &record)?;
    Ok(record)
}

pub fn verify_prepared_slot(
    slots_directory: &Path,
    record: &PreparedSlotRecord,
) -> Result<(), String> {
    record.validate().map_err(str::to_string)?;
    let slot_path = prepared_slot_path(slots_directory, record);
    let metadata = std::fs::symlink_metadata(&slot_path)
        .map_err(|error| format!("could not inspect prepared application slot: {error}"))?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err("prepared application slot is not a real directory".into());
    }
    let manifest = read_prepared_slot_manifest(&slot_path)?;
    verify_slot_directory(&slot_path, &manifest, &record.manifest)
}

pub fn read_prepared_slot_manifest(slot_path: &Path) -> Result<UpdatePackageManifest, String> {
    let manifest_path = slot_path.join(SLOT_MANIFEST_NAME);
    let bytes = read_bounded_file(
        &manifest_path,
        MAX_PACKAGE_MANIFEST_BYTES,
        "prepared slot manifest",
    )?;
    let manifest: UpdatePackageManifest = serde_json::from_slice(&bytes)
        .map_err(|error| format!("prepared slot manifest is invalid: {error}"))?;
    manifest.validate()?;
    Ok(manifest)
}

pub fn prepared_slot_path(slots_directory: &Path, record: &PreparedSlotRecord) -> PathBuf {
    slots_directory.join(&record.slot_directory_name)
}

fn slot_directory_name(manifest: &ReleaseManifest) -> String {
    format!("slot-{}-{}", manifest.version, manifest.source_commit)
}

fn create_slot_directory(
    slots_directory: &Path,
    record: &PreparedSlotRecord,
) -> Result<PathBuf, String> {
    for attempt in 0..100_u32 {
        let path = slots_directory.join(format!(
            ".incoming-{}-{}-{}-{attempt}",
            record.manifest.source_commit,
            record.prepared_at_unix_ms,
            std::process::id()
        ));
        match std::fs::create_dir(&path) {
            Ok(()) => return Ok(path),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => {
                return Err(format!(
                    "could not create application-slot quarantine: {error}"
                ));
            }
        }
    }
    Err("could not allocate a unique application-slot quarantine".into())
}

fn read_package_manifest(
    package_path: &Path,
    release: &ReleaseManifest,
) -> Result<UpdatePackageManifest, String> {
    let file = File::open(package_path)
        .map_err(|error| format!("could not open staged update package: {error}"))?;
    let mut archive = ZipArchive::new(file)
        .map_err(|error| format!("staged update package is not a valid ZIP container: {error}"))?;
    inspect_archive(&mut archive)?;
    let mut entry = archive
        .by_name(PACKAGE_MANIFEST_NAME)
        .map_err(|_| "update package manifest is missing".to_string())?;
    let mut bytes = Vec::with_capacity(entry.size() as usize);
    entry
        .by_ref()
        .take(MAX_PACKAGE_MANIFEST_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| format!("could not read update package manifest: {error}"))?;
    if bytes.is_empty() || bytes.len() as u64 > MAX_PACKAGE_MANIFEST_BYTES {
        return Err("update package manifest size is outside the accepted bounds".into());
    }
    let manifest: UpdatePackageManifest = serde_json::from_slice(&bytes)
        .map_err(|error| format!("update package manifest is invalid: {error}"))?;
    manifest.validate_against(release)?;
    Ok(manifest)
}

fn inspect_archive(archive: &mut ZipArchive<File>) -> Result<(), String> {
    if archive.len() != REQUIRED_FILES.len() + 1 {
        return Err("update package contains an unexpected number of entries".into());
    }
    let mut names = BTreeSet::new();
    for index in 0..archive.len() {
        let entry = archive
            .by_index(index)
            .map_err(|error| format!("could not inspect update package entry: {error}"))?;
        let name = entry.name();
        let allowed = name == PACKAGE_MANIFEST_NAME
            || REQUIRED_FILES
                .iter()
                .any(|path| name == format!("payload/{path}"));
        let unix_type = entry.unix_mode().map(|mode| mode & 0o170000);
        if !allowed
            || !names.insert(name.to_string())
            || !entry.is_file()
            || entry.encrypted()
            || entry.compression() != CompressionMethod::Stored
            || unix_type.is_some_and(|kind| kind != 0 && kind != 0o100000)
        {
            return Err("update package contains an unsafe or unsupported entry".into());
        }
        let maximum = if name == PACKAGE_MANIFEST_NAME {
            MAX_PACKAGE_MANIFEST_BYTES
        } else {
            MAX_SLOT_FILE_BYTES
        };
        if entry.size() == 0 || entry.size() > maximum || entry.compressed_size() != entry.size() {
            return Err("update package entry size is outside the accepted bounds".into());
        }
    }
    Ok(())
}

fn extract_package(
    package_path: &Path,
    slot_path: &Path,
    manifest: &UpdatePackageManifest,
) -> Result<(), String> {
    let file = File::open(package_path)
        .map_err(|error| format!("could not reopen staged update package: {error}"))?;
    let mut archive = ZipArchive::new(file)
        .map_err(|error| format!("staged update package is not a valid ZIP container: {error}"))?;
    for specification in &manifest.files {
        let archive_name = format!("payload/{}", specification.path);
        let mut entry = archive
            .by_name(&archive_name)
            .map_err(|_| format!("update package payload is missing: {}", specification.path))?;
        if entry.size() != specification.size_bytes {
            return Err(format!(
                "update package payload size disagrees with its internal manifest: {}",
                specification.path
            ));
        }
        let destination = slot_path.join(&specification.path);
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&destination)
            .map_err(|error| format!("could not create application-slot payload: {error}"))?;
        let mut digest = Sha256::new();
        let mut total = 0_u64;
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let count = entry
                .read(&mut buffer)
                .map_err(|error| format!("could not read update package payload: {error}"))?;
            if count == 0 {
                break;
            }
            total = total
                .checked_add(count as u64)
                .ok_or_else(|| "update package payload byte count overflowed".to_string())?;
            if total > specification.size_bytes {
                return Err("update package payload exceeds its declared size".into());
            }
            digest.update(&buffer[..count]);
            output
                .write_all(&buffer[..count])
                .map_err(|error| format!("could not write application-slot payload: {error}"))?;
        }
        if total != specification.size_bytes
            || !format!("{:X}", digest.finalize()).eq_ignore_ascii_case(&specification.sha256)
        {
            return Err(format!(
                "update package payload bytes do not match the internal manifest: {}",
                specification.path
            ));
        }
        output
            .sync_all()
            .map_err(|error| format!("could not synchronize application-slot payload: {error}"))?;
    }
    let manifest_bytes = serde_json::to_vec(manifest)
        .map_err(|error| format!("could not encode prepared slot manifest: {error}"))?;
    let manifest_path = slot_path.join(SLOT_MANIFEST_NAME);
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&manifest_path)
        .map_err(|error| format!("could not create prepared slot manifest: {error}"))?;
    output
        .write_all(&manifest_bytes)
        .and_then(|()| output.sync_all())
        .map_err(|error| format!("could not synchronize prepared slot manifest: {error}"))
}

fn verify_slot_directory(
    slot_path: &Path,
    manifest: &UpdatePackageManifest,
    release: &ReleaseManifest,
) -> Result<(), String> {
    manifest.validate_against(release)?;
    let expected: BTreeSet<&str> = manifest
        .files
        .iter()
        .map(|file| file.path.as_str())
        .chain(std::iter::once(SLOT_MANIFEST_NAME))
        .collect();
    let actual = std::fs::read_dir(slot_path)
        .map_err(|error| format!("could not list prepared application slot: {error}"))?
        .map(|entry| {
            let entry = entry.map_err(|error| format!("could not inspect slot entry: {error}"))?;
            let name = entry
                .file_name()
                .into_string()
                .map_err(|_| "prepared slot entry name is not Unicode".to_string())?;
            let metadata = std::fs::symlink_metadata(entry.path())
                .map_err(|error| format!("could not inspect prepared slot entry: {error}"))?;
            if !metadata.is_file() || metadata.file_type().is_symlink() {
                return Err("prepared application slot contains a non-file entry".into());
            }
            if !metadata.permissions().readonly() {
                return Err("prepared application slot contains a writable file".into());
            }
            Ok(name)
        })
        .collect::<Result<BTreeSet<_>, String>>()?;
    if actual.len() != expected.len() || actual.iter().any(|name| !expected.contains(name.as_str()))
    {
        return Err("prepared application slot contains an unexpected file set".into());
    }
    for specification in &manifest.files {
        verify_slot_file(&slot_path.join(&specification.path), specification)?;
    }
    Ok(())
}

fn verify_slot_file(path: &Path, specification: &UpdatePackageFile) -> Result<(), String> {
    let metadata = std::fs::symlink_metadata(path)
        .map_err(|error| format!("could not inspect application-slot payload: {error}"))?;
    if !metadata.is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() != specification.size_bytes
    {
        return Err("application-slot payload metadata does not match its manifest".into());
    }
    let mut file = File::open(path)
        .map_err(|error| format!("could not open application-slot payload: {error}"))?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|error| format!("could not verify application-slot payload: {error}"))?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    if !format!("{:X}", digest.finalize()).eq_ignore_ascii_case(&specification.sha256) {
        return Err("application-slot payload SHA-256 does not match its manifest".into());
    }
    Ok(())
}

fn make_slot_files_read_only(
    slot_path: &Path,
    manifest: &UpdatePackageManifest,
) -> Result<(), String> {
    for path in manifest
        .files
        .iter()
        .map(|file| slot_path.join(&file.path))
        .chain(std::iter::once(slot_path.join(SLOT_MANIFEST_NAME)))
    {
        let mut permissions = std::fs::metadata(&path)
            .map_err(|error| format!("could not inspect application-slot permissions: {error}"))?
            .permissions();
        permissions.set_readonly(true);
        std::fs::set_permissions(&path, permissions)
            .map_err(|error| format!("could not make application-slot file read-only: {error}"))?;
    }
    Ok(())
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

#[allow(clippy::permissions_set_readonly_false)]
fn remove_slot_tree(path: &Path) {
    let Ok(metadata) = std::fs::symlink_metadata(path) else {
        return;
    };
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return;
    }
    if let Ok(entries) = std::fs::read_dir(path) {
        for entry in entries.flatten() {
            if let Ok(metadata) = std::fs::symlink_metadata(entry.path())
                && metadata.is_file()
                && !metadata.file_type().is_symlink()
            {
                let mut permissions = metadata.permissions();
                permissions.set_readonly(false);
                let _ = std::fs::set_permissions(entry.path(), permissions);
            }
        }
    }
    let _ = std::fs::remove_dir_all(path);
}
