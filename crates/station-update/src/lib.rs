use base64::Engine;
use base64::engine::general_purpose::STANDARD as BASE64;
use ed25519_dalek::{Signature, VerifyingKey};
use semver::Version;
use serde::{Deserialize, Serialize};

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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct VerifiedRelease {
    pub manifest: ReleaseManifest,
    pub availability: Availability,
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
    let availability = if offered <= current {
        Availability::UpToDate
    } else if updater < minimum_updater {
        Availability::UpdaterTooOld
    } else {
        Availability::UpdateAvailable
    };
    Ok(VerifiedRelease {
        manifest,
        availability,
    })
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
}
