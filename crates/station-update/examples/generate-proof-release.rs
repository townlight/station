use base64::Engine;
use base64::engine::general_purpose::STANDARD as BASE64;
use ed25519_dalek::{Signer, SigningKey};
use station_update::{ReleaseManifest, ReleaseSource, SignedReleaseEnvelope, TrustedReleaseKey};
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

fn main() -> Result<(), String> {
    let mut arguments = std::env::args_os().skip(1);
    let output_directory =
        PathBuf::from(arguments.next().ok_or_else(|| {
            "usage: generate-proof-release OUTPUT_DIRECTORY FEED_URL".to_string()
        })?);
    let feed_url = arguments
        .next()
        .and_then(|value| value.into_string().ok())
        .ok_or_else(|| "usage: generate-proof-release OUTPUT_DIRECTORY FEED_URL".to_string())?;
    if arguments.next().is_some() {
        return Err("usage: generate-proof-release OUTPUT_DIRECTORY FEED_URL".into());
    }

    let now_unix_ms = i64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|error| error.to_string())?
            .as_millis(),
    )
    .map_err(|_| "system time exceeds the supported range".to_string())?;
    let signing_key = SigningKey::from_bytes(&[42; 32]);
    let manifest = ReleaseManifest {
        schema_version: 1,
        product: "TownLight Station".into(),
        release_channel: "stable".into(),
        key_id: "installed-proof-2026".into(),
        version: "0.2.0".into(),
        source_commit: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb".into(),
        published_at_unix_ms: now_unix_ms.saturating_sub(1_000),
        expires_at_unix_ms: now_unix_ms.saturating_add(3_600_000),
        minimum_updater_version: "0.1.0".into(),
        package_url: feed_url.replace("stable.json", "TownLight-Station-0.2.0.tlu"),
        package_size_bytes: 1_024,
        package_sha256: "A".repeat(64),
        release_notes_markdown:
            "# TownLight Station 0.2.0\n\nInstalled signed-discovery proof release.".into(),
    };
    let manifest_bytes = serde_json::to_vec(&manifest).map_err(|error| error.to_string())?;
    let envelope = SignedReleaseEnvelope {
        key_id: manifest.key_id.clone(),
        manifest_base64: BASE64.encode(&manifest_bytes),
        signature_base64: BASE64.encode(signing_key.sign(&manifest_bytes).to_bytes()),
    };
    let source = ReleaseSource {
        schema_version: 1,
        feed_url,
        release_channel: "stable".into(),
        trusted_keys: vec![TrustedReleaseKey {
            key_id: manifest.key_id,
            public_key_base64: BASE64.encode(signing_key.verifying_key().as_bytes()),
        }],
    };

    std::fs::create_dir_all(&output_directory).map_err(|error| error.to_string())?;
    std::fs::write(
        output_directory.join("stable.json"),
        serde_json::to_vec(&envelope).map_err(|error| error.to_string())?,
    )
    .map_err(|error| error.to_string())?;
    std::fs::write(
        output_directory.join("release-trust.json"),
        serde_json::to_vec(&source).map_err(|error| error.to_string())?,
    )
    .map_err(|error| error.to_string())?;
    Ok(())
}
