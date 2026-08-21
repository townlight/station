use base64::Engine;
use base64::engine::general_purpose::STANDARD as BASE64;
use ed25519_dalek::{Signature, VerifyingKey};
use semver::Version;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;
use ureq::Agent;
use ureq::tls::{RootCerts, TlsConfig};

mod package;

pub use package::{
    PreparedSlotRecord, UpdatePackageFile, UpdatePackageManifest, prepare_release_slot,
    prepared_slot_path, verify_prepared_slot,
};

pub const RELEASE_SCHEMA_VERSION: u32 = 1;
pub const MAX_ENVELOPE_BYTES: usize = 256 * 1024;
pub const MAX_MANIFEST_BYTES: usize = 128 * 1024;
pub const MAX_RELEASE_NOTES_BYTES: usize = 64 * 1024;
pub const MAX_PACKAGE_BYTES: u64 = 2 * 1024 * 1024 * 1024;
pub const MAX_FUTURE_CLOCK_SKEW_MS: i64 = 15 * 60 * 1_000;
pub const MAX_MANIFEST_LIFETIME_MS: i64 = 31 * 24 * 60 * 60 * 1_000;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SignedReleaseEnvelope {
    pub key_id: String,
    pub manifest_base64: String,
    pub signature_base64: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReleaseManifest {
    pub schema_version: u32,
    pub product: String,
    pub release_channel: String,
    pub key_id: String,
    pub version: String,
    pub source_commit: String,
    pub published_at_unix_ms: i64,
    pub expires_at_unix_ms: i64,
    pub minimum_updater_version: String,
    pub package_url: String,
    pub package_size_bytes: u64,
    pub package_sha256: String,
    pub release_notes_markdown: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Availability {
    UpToDate,
    UpdateAvailable,
    UpdaterTooOld,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum UpdateCheckTrigger {
    Automatic,
    Manual,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum UpdateCheckOutcome {
    UpToDate,
    UpdateAvailable,
    UpdaterTooOld,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct VerifiedRelease {
    pub manifest: ReleaseManifest,
    pub availability: Availability,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UpdateCheckRecord {
    pub checked_at_unix_ms: i64,
    pub next_automatic_check_at_unix_ms: i64,
    pub trigger: UpdateCheckTrigger,
    pub outcome: UpdateCheckOutcome,
    pub current_version: String,
    pub updater_version: String,
    pub release_channel: String,
    pub offered_release: Option<ReleaseManifest>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StagedPackageRecord {
    pub staged_at_unix_ms: i64,
    pub manifest: ReleaseManifest,
    pub package_file_name: String,
}

impl StagedPackageRecord {
    pub fn validate(&self) -> Result<(), &'static str> {
        let expected_name = staged_package_file_name(&self.manifest.package_sha256);
        if self.staged_at_unix_ms < 0
            || self.staged_at_unix_ms < self.manifest.published_at_unix_ms
            || self.staged_at_unix_ms > self.manifest.expires_at_unix_ms
            || self.package_file_name != expected_name
            || validate_manifest(
                &self.manifest,
                &self.manifest.key_id,
                &self.manifest.release_channel,
                self.staged_at_unix_ms,
            )
            .is_err()
        {
            return Err("staged package record fields are invalid");
        }
        Ok(())
    }
}

impl UpdateCheckRecord {
    pub fn validate(&self) -> Result<(), &'static str> {
        let versions_valid = Version::parse(&self.current_version).is_ok()
            && Version::parse(&self.updater_version).is_ok();
        let timing_valid = self.checked_at_unix_ms >= 0
            && self.next_automatic_check_at_unix_ms > self.checked_at_unix_ms
            && self
                .next_automatic_check_at_unix_ms
                .saturating_sub(self.checked_at_unix_ms)
                <= 24 * 60 * 60 * 1_000;
        let release_valid = self.offered_release.as_ref().is_none_or(|release| {
            validate_manifest(
                release,
                &release.key_id,
                &self.release_channel,
                self.checked_at_unix_ms,
            )
            .is_ok()
                && Version::parse(&release.version).is_ok()
                && Version::parse(&release.minimum_updater_version).is_ok()
        });
        let offered_outcome = self.offered_release.as_ref().and_then(|release| {
            let current = Version::parse(&self.current_version).ok()?;
            let updater = Version::parse(&self.updater_version).ok()?;
            let offered = Version::parse(&release.version).ok()?;
            let minimum = Version::parse(&release.minimum_updater_version).ok()?;
            Some(UpdateCheckOutcome::from(classify_availability(
                &current, &offered, &updater, &minimum,
            )))
        });
        let state_valid = match self.outcome {
            UpdateCheckOutcome::Failed => {
                self.offered_release.is_none() && self.error.as_deref().is_some_and(bounded_error)
            }
            UpdateCheckOutcome::UpToDate
            | UpdateCheckOutcome::UpdateAvailable
            | UpdateCheckOutcome::UpdaterTooOld => {
                offered_outcome == Some(self.outcome) && self.error.is_none()
            }
        };
        if versions_valid
            && timing_valid
            && visible_identifier(&self.release_channel, 32)
            && release_valid
            && state_valid
        {
            Ok(())
        } else {
            Err("update-check record fields are invalid")
        }
    }
}

impl From<Availability> for UpdateCheckOutcome {
    fn from(value: Availability) -> Self {
        match value {
            Availability::UpToDate => Self::UpToDate,
            Availability::UpdateAvailable => Self::UpdateAvailable,
            Availability::UpdaterTooOld => Self::UpdaterTooOld,
        }
    }
}

fn bounded_error(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 500
        && value.trim() == value
        && !value.chars().any(char::is_control)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TrustedReleaseKey {
    pub key_id: String,
    pub public_key_base64: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReleaseSource {
    pub schema_version: u32,
    pub feed_url: String,
    pub release_channel: String,
    pub trusted_keys: Vec<TrustedReleaseKey>,
}

impl ReleaseSource {
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != 1
            || !valid_package_url(&self.feed_url)
            || !visible_identifier(&self.release_channel, 32)
            || self.trusted_keys.is_empty()
            || self.trusted_keys.len() > 4
        {
            return Err("release source fields are invalid".into());
        }
        let mut ids = std::collections::BTreeSet::new();
        for key in &self.trusted_keys {
            if !visible_identifier(&key.key_id, 80) || !ids.insert(&key.key_id) {
                return Err("release source key ids are invalid or duplicated".into());
            }
            let bytes = BASE64
                .decode(&key.public_key_base64)
                .map_err(|_| "release source public key is not valid base64".to_string())?;
            let bytes: [u8; 32] = bytes
                .try_into()
                .map_err(|_| "release source public key has the wrong length".to_string())?;
            let verifying_key = VerifyingKey::from_bytes(&bytes)
                .map_err(|_| "release source public key is invalid".to_string())?;
            if verifying_key.is_weak() {
                return Err("release source public key is weak".into());
            }
        }
        Ok(())
    }

    fn decoded_keys(&self) -> Result<Vec<(&str, [u8; 32])>, String> {
        self.validate()?;
        self.trusted_keys
            .iter()
            .map(|key| {
                let decoded = BASE64
                    .decode(&key.public_key_base64)
                    .map_err(|_| "release source public key is not valid base64".to_string())?;
                let bytes = decoded
                    .try_into()
                    .map_err(|_| "release source public key has the wrong length".to_string())?;
                Ok((key.key_id.as_str(), bytes))
            })
            .collect()
    }
}

pub fn stage_release_package(
    manifest: &ReleaseManifest,
    updates_directory: &Path,
    staged_at_unix_ms: i64,
    timeout: Duration,
) -> Result<StagedPackageRecord, String> {
    validate_manifest(
        manifest,
        &manifest.key_id,
        &manifest.release_channel,
        staged_at_unix_ms,
    )?;
    if timeout.is_zero() || timeout > Duration::from_secs(15 * 60) {
        return Err("package-download timeout is outside the accepted bounds".into());
    }
    let quarantine_directory = updates_directory.join("quarantine");
    let staged_directory = updates_directory.join("staged");
    ensure_managed_directory(&quarantine_directory)?;
    ensure_managed_directory(&staged_directory)?;
    let package_file_name = staged_package_file_name(&manifest.package_sha256);
    let final_path = staged_directory.join(&package_file_name);
    if final_path.exists() {
        verify_package_file(&final_path, manifest)?;
        return staged_record(manifest, package_file_name, staged_at_unix_ms);
    }

    let temporary_path = create_quarantine_file(&quarantine_directory, staged_at_unix_ms)?;
    let result = (|| {
        let mut output = OpenOptions::new()
            .write(true)
            .open(&temporary_path)
            .map_err(|error| format!("could not open update quarantine file: {error}"))?;
        let agent = Agent::config_builder()
            .timeout_global(Some(timeout))
            .max_redirects(0)
            .tls_config(
                TlsConfig::builder()
                    .root_certs(RootCerts::PlatformVerifier)
                    .build(),
            )
            .user_agent("TownLight-Station-Update/1")
            .build()
            .new_agent();
        let mut response = agent
            .get(&manifest.package_url)
            .call()
            .map_err(release_request_error)?;
        if !response.status().is_success() {
            return Err(format!(
                "release package service returned HTTP {}",
                response.status().as_u16()
            ));
        }
        if let Some(length) = response.headers().get("Content-Length") {
            let length = length
                .to_str()
                .ok()
                .and_then(|value| value.parse::<u64>().ok())
                .ok_or_else(|| "release package Content-Length is invalid".to_string())?;
            if length != manifest.package_size_bytes {
                return Err("release package Content-Length does not match its signed size".into());
            }
        }
        let mut reader = response
            .body_mut()
            .with_config()
            .limit(manifest.package_size_bytes.saturating_add(1))
            .reader();
        let mut digest = Sha256::new();
        let mut total = 0_u64;
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let count = reader.read(&mut buffer).map_err(|_| {
                "release package could not be read within signed bounds".to_string()
            })?;
            if count == 0 {
                break;
            }
            total = total
                .checked_add(count as u64)
                .ok_or_else(|| "release package byte count overflowed".to_string())?;
            if total > manifest.package_size_bytes {
                return Err("release package exceeds its signed size".into());
            }
            digest.update(&buffer[..count]);
            output
                .write_all(&buffer[..count])
                .map_err(|error| format!("could not write update quarantine file: {error}"))?;
        }
        if total != manifest.package_size_bytes {
            return Err("release package size does not match its signed size".into());
        }
        let actual_hash = format!("{:X}", digest.finalize());
        if !actual_hash.eq_ignore_ascii_case(&manifest.package_sha256) {
            return Err("release package SHA-256 does not match its signed digest".into());
        }
        output
            .sync_all()
            .map_err(|error| format!("could not synchronize update quarantine file: {error}"))?;
        drop(output);
        match std::fs::rename(&temporary_path, &final_path) {
            Ok(()) => {}
            Err(_) if final_path.exists() => {
                verify_package_file(&final_path, manifest)?;
                std::fs::remove_file(&temporary_path).map_err(|error| {
                    format!("could not remove duplicate update quarantine file: {error}")
                })?;
            }
            Err(error) => {
                return Err(format!(
                    "could not publish verified update package: {error}"
                ));
            }
        }
        let mut permissions = std::fs::metadata(&final_path)
            .map_err(|error| format!("could not inspect staged update package: {error}"))?
            .permissions();
        permissions.set_readonly(true);
        std::fs::set_permissions(&final_path, permissions)
            .map_err(|error| format!("could not make staged update package read-only: {error}"))?;
        staged_record(manifest, package_file_name, staged_at_unix_ms)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary_path);
    }
    result
}

pub fn staged_package_path(updates_directory: &Path, record: &StagedPackageRecord) -> PathBuf {
    updates_directory
        .join("staged")
        .join(&record.package_file_name)
}

fn staged_record(
    manifest: &ReleaseManifest,
    package_file_name: String,
    staged_at_unix_ms: i64,
) -> Result<StagedPackageRecord, String> {
    let record = StagedPackageRecord {
        staged_at_unix_ms,
        manifest: manifest.clone(),
        package_file_name,
    };
    record.validate().map_err(str::to_string)?;
    Ok(record)
}

fn staged_package_file_name(sha256: &str) -> String {
    format!("{}.tlu", sha256.to_ascii_lowercase())
}

fn ensure_managed_directory(path: &Path) -> Result<(), String> {
    std::fs::create_dir_all(path)
        .map_err(|error| format!("could not create managed update directory: {error}"))?;
    let metadata = std::fs::symlink_metadata(path)
        .map_err(|error| format!("could not inspect managed update directory: {error}"))?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err("managed update directory is not a real directory".into());
    }
    Ok(())
}

fn create_quarantine_file(directory: &Path, staged_at_unix_ms: i64) -> Result<PathBuf, String> {
    for attempt in 0..100_u32 {
        let path = directory.join(format!(
            "download-{staged_at_unix_ms}-{}-{attempt}.part",
            std::process::id()
        ));
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(file) => {
                drop(file);
                return Ok(path);
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => {
                return Err(format!("could not create update quarantine file: {error}"));
            }
        }
    }
    Err("could not allocate a unique update quarantine file".into())
}

fn verify_package_file(path: &Path, manifest: &ReleaseManifest) -> Result<(), String> {
    let metadata = std::fs::symlink_metadata(path)
        .map_err(|error| format!("could not inspect staged update package: {error}"))?;
    if !metadata.is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() != manifest.package_size_bytes
    {
        return Err("staged update package size does not match signed metadata".into());
    }
    let mut file = File::open(path)
        .map_err(|error| format!("could not open staged update package: {error}"))?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|error| format!("could not verify staged update package: {error}"))?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    let actual_hash = format!("{:X}", digest.finalize());
    if !actual_hash.eq_ignore_ascii_case(&manifest.package_sha256) {
        return Err("staged update package SHA-256 does not match signed metadata".into());
    }
    Ok(())
}

pub fn fetch_and_verify_release(
    source: &ReleaseSource,
    current_version: &str,
    updater_version: &str,
    now_unix_ms: i64,
    timeout: Duration,
) -> Result<VerifiedRelease, String> {
    let trusted_keys = source.decoded_keys()?;
    if timeout.is_zero() || timeout > Duration::from_secs(60) {
        return Err("release-check timeout is outside the accepted bounds".into());
    }
    let agent = Agent::config_builder()
        .timeout_global(Some(timeout))
        .max_redirects(0)
        .tls_config(
            TlsConfig::builder()
                .root_certs(RootCerts::PlatformVerifier)
                .build(),
        )
        .user_agent("TownLight-Station-Update/1")
        .build()
        .new_agent();
    let mut response = agent
        .get(&source.feed_url)
        .call()
        .map_err(release_request_error)?;
    if !response.status().is_success() {
        return Err(format!(
            "release service returned HTTP {}",
            response.status().as_u16()
        ));
    }
    let content_type = response
        .headers()
        .get("Content-Type")
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default();
    if !content_type
        .split(';')
        .next()
        .is_some_and(|value| value.trim().eq_ignore_ascii_case("application/json"))
    {
        return Err("release service response is not application/json".into());
    }
    let bytes = response
        .body_mut()
        .with_config()
        .limit((MAX_ENVELOPE_BYTES + 1) as u64)
        .read_to_vec()
        .map_err(|_| "release service response could not be read within bounds".to_string())?;
    verify_release_envelope(
        &bytes,
        &trusted_keys,
        &source.release_channel,
        current_version,
        updater_version,
        now_unix_ms,
    )
}

fn release_request_error(error: ureq::Error) -> String {
    match error {
        ureq::Error::StatusCode(status) => {
            format!("release service returned HTTP {status}")
        }
        _ => "release check failed before a trusted response was received".into(),
    }
}

pub fn verify_release_envelope(
    envelope_json: &[u8],
    trusted_keys: &[(&str, [u8; 32])],
    expected_channel: &str,
    current_version: &str,
    updater_version: &str,
    now_unix_ms: i64,
) -> Result<VerifiedRelease, String> {
    if envelope_json.is_empty() || envelope_json.len() > MAX_ENVELOPE_BYTES {
        return Err("release envelope size is outside the accepted bounds".into());
    }
    if !visible_identifier(expected_channel, 32) {
        return Err("expected release channel is invalid".into());
    }
    if now_unix_ms < 0 {
        return Err("current release-check time must be nonnegative".into());
    }
    let envelope: SignedReleaseEnvelope = serde_json::from_slice(envelope_json)
        .map_err(|error| format!("release envelope is invalid JSON: {error}"))?;
    if !visible_identifier(&envelope.key_id, 80) {
        return Err("release envelope key id is invalid".into());
    }
    let manifest_bytes = BASE64
        .decode(&envelope.manifest_base64)
        .map_err(|_| "release manifest is not valid base64".to_string())?;
    if manifest_bytes.is_empty() || manifest_bytes.len() > MAX_MANIFEST_BYTES {
        return Err("release manifest size is outside the accepted bounds".into());
    }
    let signature_bytes = BASE64
        .decode(&envelope.signature_base64)
        .map_err(|_| "release signature is not valid base64".to_string())?;
    let signature = Signature::from_slice(&signature_bytes)
        .map_err(|_| "release signature has the wrong length".to_string())?;
    let (_, key_bytes) = trusted_keys
        .iter()
        .find(|(key_id, _)| *key_id == envelope.key_id)
        .ok_or_else(|| "release envelope was signed by an unknown key".to_string())?;
    let key = VerifyingKey::from_bytes(key_bytes)
        .map_err(|_| "configured release verification key is invalid".to_string())?;
    if key.is_weak() {
        return Err("configured release verification key is weak".into());
    }
    key.verify_strict(&manifest_bytes, &signature)
        .map_err(|_| "release manifest signature is invalid".to_string())?;

    let manifest: ReleaseManifest = serde_json::from_slice(&manifest_bytes)
        .map_err(|error| format!("signed release manifest is invalid: {error}"))?;
    validate_manifest(&manifest, &envelope.key_id, expected_channel, now_unix_ms)?;
    let current = Version::parse(current_version)
        .map_err(|_| "installed product version is not valid semantic versioning".to_string())?;
    let offered = Version::parse(&manifest.version)
        .map_err(|_| "offered product version is not valid semantic versioning".to_string())?;
    let updater = Version::parse(updater_version)
        .map_err(|_| "installed updater version is not valid semantic versioning".to_string())?;
    let minimum_updater = Version::parse(&manifest.minimum_updater_version)
        .map_err(|_| "minimum updater version is not valid semantic versioning".to_string())?;
    let availability = classify_availability(&current, &offered, &updater, &minimum_updater);
    Ok(VerifiedRelease {
        manifest,
        availability,
    })
}

fn classify_availability(
    current: &Version,
    offered: &Version,
    updater: &Version,
    minimum_updater: &Version,
) -> Availability {
    if offered <= current {
        Availability::UpToDate
    } else if updater < minimum_updater {
        Availability::UpdaterTooOld
    } else {
        Availability::UpdateAvailable
    }
}

fn validate_manifest(
    manifest: &ReleaseManifest,
    envelope_key_id: &str,
    expected_channel: &str,
    now_unix_ms: i64,
) -> Result<(), String> {
    if manifest.schema_version != RELEASE_SCHEMA_VERSION {
        return Err("release manifest schema version is unsupported".into());
    }
    if manifest.product != "TownLight Station" {
        return Err("release manifest product identity is invalid".into());
    }
    if manifest.key_id != envelope_key_id {
        return Err("release manifest and envelope key ids disagree".into());
    }
    if manifest.release_channel != expected_channel {
        return Err("release manifest channel does not match this station".into());
    }
    if !visible_identifier(&manifest.release_channel, 32)
        || !visible_identifier(&manifest.key_id, 80)
        || !is_commit_hash(&manifest.source_commit)
        || manifest.published_at_unix_ms < 0
        || manifest.expires_at_unix_ms <= manifest.published_at_unix_ms
        || manifest
            .expires_at_unix_ms
            .saturating_sub(manifest.published_at_unix_ms)
            > MAX_MANIFEST_LIFETIME_MS
        || manifest.published_at_unix_ms > now_unix_ms.saturating_add(MAX_FUTURE_CLOCK_SKEW_MS)
        || manifest.expires_at_unix_ms < now_unix_ms
        || manifest.package_size_bytes == 0
        || manifest.package_size_bytes > MAX_PACKAGE_BYTES
        || !is_sha256(&manifest.package_sha256)
        || manifest.release_notes_markdown.is_empty()
        || manifest.release_notes_markdown.len() > MAX_RELEASE_NOTES_BYTES
        || manifest
            .release_notes_markdown
            .chars()
            .any(|character| character == '\0')
        || !valid_package_url(&manifest.package_url)
    {
        return Err("signed release manifest fields are invalid".into());
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

fn is_commit_hash(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn valid_package_url(value: &str) -> bool {
    if value.len() > 2_000 || value.trim() != value || value.chars().any(char::is_control) {
        return false;
    }
    let remainder = if let Some(remainder) = value.strip_prefix("https://") {
        remainder
    } else if let Some(remainder) = value
        .strip_prefix("http://127.0.0.1:")
        .or_else(|| value.strip_prefix("http://localhost:"))
    {
        remainder
    } else {
        return false;
    };
    let authority = remainder.split('/').next().unwrap_or_default();
    !authority.is_empty()
        && !authority.contains('@')
        && !authority.contains('#')
        && !authority.chars().any(char::is_whitespace)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signer, SigningKey};
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;
    use std::time::{SystemTime, UNIX_EPOCH};

    const NOW: i64 = 1_800_000_000_000;

    fn manifest() -> ReleaseManifest {
        ReleaseManifest {
            schema_version: 1,
            product: "TownLight Station".into(),
            release_channel: "stable".into(),
            key_id: "release-2026-a".into(),
            version: "1.3.0".into(),
            source_commit: "0123456789abcdef0123456789abcdef01234567".into(),
            published_at_unix_ms: NOW - 60_000,
            expires_at_unix_ms: NOW + 86_400_000,
            minimum_updater_version: "1.0.0".into(),
            package_url: "https://releases.example.test/station/1.3.0.tlu".into(),
            package_size_bytes: 123_456,
            package_sha256: "A".repeat(64),
            release_notes_markdown: "# 1.3.0\n\nSafer output recovery.".into(),
        }
    }

    fn signed_envelope(manifest: &ReleaseManifest, signing_key: &SigningKey) -> Vec<u8> {
        let manifest_bytes = serde_json::to_vec(manifest).unwrap();
        let envelope = SignedReleaseEnvelope {
            key_id: manifest.key_id.clone(),
            manifest_base64: BASE64.encode(&manifest_bytes),
            signature_base64: BASE64.encode(signing_key.sign(&manifest_bytes).to_bytes()),
        };
        serde_json::to_vec(&envelope).unwrap()
    }

    #[test]
    fn accepts_a_strictly_signed_newer_release_for_the_expected_channel() {
        let signing_key = SigningKey::from_bytes(&[7; 32]);
        let envelope = signed_envelope(&manifest(), &signing_key);
        let verified = verify_release_envelope(
            &envelope,
            &[("release-2026-a", signing_key.verifying_key().to_bytes())],
            "stable",
            "1.2.9",
            "1.1.0",
            NOW,
        )
        .unwrap();
        assert_eq!(verified.availability, Availability::UpdateAvailable);
        assert_eq!(verified.manifest.version, "1.3.0");
    }

    #[test]
    fn rejects_tampering_unknown_keys_expiry_and_channel_substitution() {
        let signing_key = SigningKey::from_bytes(&[8; 32]);
        let trusted = [("release-2026-a", signing_key.verifying_key().to_bytes())];

        let mut envelope: SignedReleaseEnvelope =
            serde_json::from_slice(&signed_envelope(&manifest(), &signing_key)).unwrap();
        let mut bytes = BASE64.decode(&envelope.manifest_base64).unwrap();
        let index = bytes.iter().position(|byte| *byte == b'1').unwrap();
        bytes[index] = b'2';
        envelope.manifest_base64 = BASE64.encode(bytes);
        let tampered = serde_json::to_vec(&envelope).unwrap();
        assert!(
            verify_release_envelope(&tampered, &trusted, "stable", "1.0.0", "1.0.0", NOW)
                .unwrap_err()
                .contains("signature")
        );

        let unknown = signed_envelope(&manifest(), &signing_key);
        assert!(
            verify_release_envelope(&unknown, &[], "stable", "1.0.0", "1.0.0", NOW)
                .unwrap_err()
                .contains("unknown key")
        );

        let mut expired = manifest();
        expired.expires_at_unix_ms = NOW - 1;
        assert!(
            verify_release_envelope(
                &signed_envelope(&expired, &signing_key),
                &trusted,
                "stable",
                "1.0.0",
                "1.0.0",
                NOW,
            )
            .is_err()
        );

        assert!(
            verify_release_envelope(
                &signed_envelope(&manifest(), &signing_key),
                &trusted,
                "preview",
                "1.0.0",
                "1.0.0",
                NOW,
            )
            .is_err()
        );
    }

    #[test]
    fn classifies_equal_older_and_minimum_updater_versions_without_downgrading() {
        let signing_key = SigningKey::from_bytes(&[9; 32]);
        let trusted = [("release-2026-a", signing_key.verifying_key().to_bytes())];
        let envelope = signed_envelope(&manifest(), &signing_key);

        let equal =
            verify_release_envelope(&envelope, &trusted, "stable", "1.3.0", "1.0.0", NOW).unwrap();
        assert_eq!(equal.availability, Availability::UpToDate);
        let older =
            verify_release_envelope(&envelope, &trusted, "stable", "2.0.0", "1.0.0", NOW).unwrap();
        assert_eq!(older.availability, Availability::UpToDate);

        let too_old =
            verify_release_envelope(&envelope, &trusted, "stable", "1.2.0", "0.9.0", NOW).unwrap();
        assert_eq!(too_old.availability, Availability::UpdaterTooOld);
    }

    #[test]
    fn rejects_insecure_remote_packages_and_oversized_artifacts() {
        let signing_key = SigningKey::from_bytes(&[10; 32]);
        let trusted = [("release-2026-a", signing_key.verifying_key().to_bytes())];
        let mut offered = manifest();
        offered.package_url = "http://releases.example.test/update.tlu".into();
        assert!(
            verify_release_envelope(
                &signed_envelope(&offered, &signing_key),
                &trusted,
                "stable",
                "1.0.0",
                "1.0.0",
                NOW,
            )
            .is_err()
        );
        offered.package_url = "http://127.0.0.1:47890/update.tlu".into();
        offered.package_size_bytes = MAX_PACKAGE_BYTES + 1;
        assert!(
            verify_release_envelope(
                &signed_envelope(&offered, &signing_key),
                &trusted,
                "stable",
                "1.0.0",
                "1.0.0",
                NOW,
            )
            .is_err()
        );
    }

    #[test]
    fn fetches_one_bounded_json_envelope_without_following_redirects() {
        let signing_key = SigningKey::from_bytes(&[11; 32]);
        let envelope = signed_envelope(&manifest(), &signing_key);
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 2_048];
            let size = stream.read(&mut request).unwrap();
            assert!(String::from_utf8_lossy(&request[..size]).starts_with("GET /stable.json "));
            let headers = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
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
        let verified =
            fetch_and_verify_release(&source, "1.2.0", "1.0.0", NOW, Duration::from_secs(2))
                .unwrap();
        assert_eq!(verified.availability, Availability::UpdateAvailable);
        server.join().unwrap();

        let redirect_listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let redirect_address = redirect_listener.local_addr().unwrap();
        let redirect_server = thread::spawn(move || {
            let (mut stream, _) = redirect_listener.accept().unwrap();
            let mut request = [0_u8; 2_048];
            let _ = stream.read(&mut request).unwrap();
            stream
                .write_all(
                    b"HTTP/1.1 302 Found\r\nLocation: https://example.test/elsewhere\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                )
                .unwrap();
        });
        let redirect_source = ReleaseSource {
            feed_url: format!("http://{redirect_address}/stable.json"),
            ..source
        };
        assert!(
            fetch_and_verify_release(
                &redirect_source,
                "1.2.0",
                "1.0.0",
                NOW,
                Duration::from_secs(2),
            )
            .is_err()
        );
        redirect_server.join().unwrap();
    }

    #[test]
    #[allow(clippy::permissions_set_readonly_false)]
    fn stages_only_exact_signed_package_bytes_and_reuses_the_content_address() {
        let package = b"TownLight update package proof bytes".to_vec();
        let package_hash = format!("{:X}", Sha256::digest(&package));
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server_package = package.clone();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 2_048];
            let size = stream.read(&mut request).unwrap();
            assert!(String::from_utf8_lossy(&request[..size]).starts_with("GET /proof.tlu "));
            let headers = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                server_package.len()
            );
            stream.write_all(headers.as_bytes()).unwrap();
            stream.write_all(&server_package).unwrap();
        });
        let mut offered = manifest();
        offered.package_url = format!("http://{address}/proof.tlu");
        offered.package_size_bytes = package.len() as u64;
        offered.package_sha256 = package_hash.clone();
        let root = std::env::temp_dir().join(format!(
            "townlight-stage-package-{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let staged = stage_release_package(&offered, &root, NOW, Duration::from_secs(2)).unwrap();
        server.join().unwrap();
        assert_eq!(
            staged.package_file_name,
            format!("{}.tlu", package_hash.to_ascii_lowercase())
        );
        assert_eq!(
            std::fs::read(staged_package_path(&root, &staged)).unwrap(),
            package
        );
        assert!(
            std::fs::metadata(staged_package_path(&root, &staged))
                .unwrap()
                .permissions()
                .readonly()
        );
        let reused =
            stage_release_package(&offered, &root, NOW + 1, Duration::from_secs(2)).unwrap();
        assert_eq!(reused.package_file_name, staged.package_file_name);
        let mut permissions = std::fs::metadata(staged_package_path(&root, &staged))
            .unwrap()
            .permissions();
        permissions.set_readonly(false);
        std::fs::set_permissions(staged_package_path(&root, &staged), permissions).unwrap();
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn removes_quarantine_bytes_when_the_signed_package_digest_disagrees() {
        let package = b"tampered package bytes".to_vec();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 2_048];
            let _ = stream.read(&mut request).unwrap();
            let headers = format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                package.len()
            );
            stream.write_all(headers.as_bytes()).unwrap();
            stream.write_all(&package).unwrap();
        });
        let mut offered = manifest();
        offered.package_url = format!("http://{address}/tampered.tlu");
        offered.package_size_bytes = 22;
        offered.package_sha256 = "B".repeat(64);
        let root = std::env::temp_dir().join(format!(
            "townlight-reject-package-{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        assert!(
            stage_release_package(&offered, &root, NOW, Duration::from_secs(2))
                .unwrap_err()
                .contains("SHA-256")
        );
        server.join().unwrap();
        assert_eq!(
            std::fs::read_dir(root.join("quarantine")).unwrap().count(),
            0
        );
        assert_eq!(std::fs::read_dir(root.join("staged")).unwrap().count(), 0);
        std::fs::remove_dir_all(root).unwrap();
    }
}
