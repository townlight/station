use base64::Engine;
use base64::engine::general_purpose::STANDARD as BASE64;
use ed25519_dalek::{Signer, SigningKey};
use sha2::{Digest, Sha256};
use station_update::{
    RELEASE_SCHEMA_VERSION, ReleaseManifest, ReleaseSource, SignedReleaseEnvelope,
    TrustedReleaseKey, UpdatePackageFile, UpdatePackageManifest,
};
use std::fs::File;
use std::io::{Cursor, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};
use zip::{CompressionMethod, ZipWriter, write::SimpleFileOptions};

fn main() -> Result<(), String> {
    let arguments = std::env::args_os().skip(1).collect::<Vec<_>>();
    if arguments.len() != 6 {
        return Err(
            "usage: create_update_fixture OUTPUT VERSION SOURCE_COMMIT PORT STATIOND WORKER".into(),
        );
    }
    let output = PathBuf::from(&arguments[0]);
    let version = arguments[1]
        .to_str()
        .ok_or("version must be Unicode")?
        .to_string();
    let source_commit = arguments[2]
        .to_str()
        .ok_or("source commit must be Unicode")?
        .to_string();
    let port = arguments[3]
        .to_str()
        .ok_or("port must be Unicode")?
        .parse::<u16>()
        .map_err(|_| "port is invalid")?;
    let payload_paths = [
        ("stationd.exe", PathBuf::from(&arguments[4])),
        ("channel-worker.exe", PathBuf::from(&arguments[5])),
    ];
    std::fs::create_dir_all(&output)
        .map_err(|error| format!("could not create fixture directory: {error}"))?;

    let mut payloads = Vec::new();
    for (name, path) in payload_paths {
        let bytes = std::fs::read(&path)
            .map_err(|error| format!("could not read {}: {error}", path.display()))?;
        if bytes.is_empty() {
            return Err(format!("fixture payload is empty: {}", path.display()));
        }
        payloads.push((name, bytes));
    }
    let package_manifest = UpdatePackageManifest {
        schema_version: 1,
        product: "TownLight Station".into(),
        version: version.clone(),
        source_commit: source_commit.clone(),
        files: payloads
            .iter()
            .map(|(path, bytes)| UpdatePackageFile {
                path: (*path).into(),
                size_bytes: bytes.len() as u64,
                sha256: format!("{:X}", Sha256::digest(bytes)),
            })
            .collect(),
    };
    package_manifest.validate()?;
    let package = create_package(&package_manifest, &payloads)?;
    let package_name = format!("TownLight-Station-{version}.tlu");
    write_new(&output.join(&package_name), &package)?;

    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_millis() as i64;
    let key_id = "local-machine-proof-2026";
    let manifest = ReleaseManifest {
        schema_version: RELEASE_SCHEMA_VERSION,
        product: "TownLight Station".into(),
        release_channel: "stable".into(),
        key_id: key_id.into(),
        version,
        source_commit,
        published_at_unix_ms: now - 1_000,
        expires_at_unix_ms: now + 60 * 60 * 1_000,
        minimum_updater_version: "0.1.0".into(),
        package_url: format!("http://127.0.0.1:{port}/{package_name}"),
        package_size_bytes: package.len() as u64,
        package_sha256: format!("{:X}", Sha256::digest(&package)),
        release_notes_markdown: format!(
            "# TownLight Station {}\n\nTransactional activation machine proof.",
            package_manifest.version
        ),
    };
    let signing_key = SigningKey::from_bytes(&[42; 32]);
    let manifest_bytes = serde_json::to_vec(&manifest)
        .map_err(|error| format!("could not encode release manifest: {error}"))?;
    let envelope = SignedReleaseEnvelope {
        key_id: key_id.into(),
        manifest_base64: BASE64.encode(&manifest_bytes),
        signature_base64: BASE64.encode(signing_key.sign(&manifest_bytes).to_bytes()),
    };
    write_new(
        &output.join("stable.json"),
        &serde_json::to_vec(&envelope)
            .map_err(|error| format!("could not encode signed envelope: {error}"))?,
    )?;
    let trust = ReleaseSource {
        schema_version: RELEASE_SCHEMA_VERSION,
        feed_url: format!("http://127.0.0.1:{port}/stable.json"),
        release_channel: "stable".into(),
        trusted_keys: vec![TrustedReleaseKey {
            key_id: key_id.into(),
            public_key_base64: BASE64.encode(signing_key.verifying_key().as_bytes()),
        }],
    };
    trust.validate()?;
    write_new(
        &output.join("release-trust.json"),
        &serde_json::to_vec(&trust)
            .map_err(|error| format!("could not encode release trust: {error}"))?,
    )?;
    println!("{}", serde_json::to_string_pretty(&manifest).unwrap());
    Ok(())
}

fn create_package(
    manifest: &UpdatePackageManifest,
    payloads: &[(&str, Vec<u8>)],
) -> Result<Vec<u8>, String> {
    let cursor = Cursor::new(Vec::new());
    let mut archive = ZipWriter::new(cursor);
    let options = SimpleFileOptions::default()
        .compression_method(CompressionMethod::Stored)
        .unix_permissions(0o644);
    archive
        .start_file("package-manifest.json", options)
        .map_err(|error| format!("could not create package manifest entry: {error}"))?;
    archive
        .write_all(&serde_json::to_vec(manifest).unwrap())
        .map_err(|error| format!("could not write package manifest: {error}"))?;
    for (name, bytes) in payloads {
        archive
            .start_file(format!("payload/{name}"), options)
            .map_err(|error| format!("could not create payload entry: {error}"))?;
        archive
            .write_all(bytes)
            .map_err(|error| format!("could not write payload entry: {error}"))?;
    }
    archive
        .finish()
        .map(|cursor| cursor.into_inner())
        .map_err(|error| format!("could not finalize update package: {error}"))
}

fn write_new(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let mut file = File::options()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|error| format!("could not create {}: {error}", path.display()))?;
    file.write_all(bytes)
        .and_then(|()| file.sync_all())
        .map_err(|error| format!("could not write {}: {error}", path.display()))
}
