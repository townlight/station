use sha2::{Digest, Sha256};
use station_update::{
    ReleaseManifest, StagedPackageRecord, UPDATE_PACKAGE_SCHEMA_VERSION, UpdatePackageFile,
    UpdatePackageManifest, prepare_release_slot, prepared_slot_path, verify_prepared_slot,
};
use std::io::{Cursor, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};
use zip::CompressionMethod;
use zip::write::SimpleFileOptions;

const NOW: i64 = 1_800_000_000_000;
const COMMIT: &str = "0123456789abcdef0123456789abcdef01234567";
static FIXTURE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

fn payloads() -> Vec<(&'static str, &'static [u8])> {
    vec![
        ("stationd.exe", b"station service proof bytes"),
        ("channel-worker.exe", b"channel worker proof bytes"),
    ]
}

fn package_manifest() -> UpdatePackageManifest {
    UpdatePackageManifest {
        schema_version: UPDATE_PACKAGE_SCHEMA_VERSION,
        product: "TownLight Station".into(),
        version: "1.3.0".into(),
        source_commit: COMMIT.into(),
        files: payloads()
            .into_iter()
            .map(|(path, bytes)| UpdatePackageFile {
                path: path.into(),
                size_bytes: bytes.len() as u64,
                sha256: format!("{:X}", Sha256::digest(bytes)),
            })
            .collect(),
    }
}

fn package_bytes(manifest: &UpdatePackageManifest, extra: Option<(&str, &[u8])>) -> Vec<u8> {
    let cursor = Cursor::new(Vec::new());
    let mut archive = zip::ZipWriter::new(cursor);
    let options = SimpleFileOptions::default()
        .compression_method(CompressionMethod::Stored)
        .unix_permissions(0o644);
    archive
        .start_file("package-manifest.json", options)
        .unwrap();
    archive
        .write_all(&serde_json::to_vec(manifest).unwrap())
        .unwrap();
    for (path, bytes) in payloads() {
        archive
            .start_file(format!("payload/{path}"), options)
            .unwrap();
        archive.write_all(bytes).unwrap();
    }
    if let Some((path, bytes)) = extra {
        archive.start_file(path, options).unwrap();
        archive.write_all(bytes).unwrap();
    }
    archive.finish().unwrap().into_inner()
}

fn staged_fixture(bytes: &[u8]) -> (PathBuf, PathBuf, StagedPackageRecord) {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let sequence = FIXTURE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let root = std::env::temp_dir().join(format!(
        "townlight-package-slot-{}-{nonce}-{sequence}",
        std::process::id()
    ));
    let updates = root.join("updates");
    let slots = root.join("slots");
    std::fs::create_dir_all(updates.join("staged")).unwrap();
    let package_hash = format!("{:X}", Sha256::digest(bytes));
    let manifest = ReleaseManifest {
        schema_version: 1,
        product: "TownLight Station".into(),
        release_channel: "stable".into(),
        key_id: "release-2026-a".into(),
        version: "1.3.0".into(),
        source_commit: COMMIT.into(),
        published_at_unix_ms: NOW - 1_000,
        expires_at_unix_ms: NOW + 86_400_000,
        minimum_updater_version: "1.0.0".into(),
        package_url: "https://releases.example.test/station/1.3.0.tlu".into(),
        package_size_bytes: bytes.len() as u64,
        package_sha256: package_hash.clone(),
        release_notes_markdown: "# 1.3.0\n\nImmutable slot proof.".into(),
    };
    let staged = StagedPackageRecord {
        staged_at_unix_ms: NOW,
        manifest,
        package_file_name: format!("{}.tlu", package_hash.to_ascii_lowercase()),
    };
    std::fs::write(
        updates.join("staged").join(&staged.package_file_name),
        bytes,
    )
    .unwrap();
    (updates, slots, staged)
}

#[test]
fn prepares_and_reverifies_one_content_bound_immutable_slot() {
    let package = package_bytes(&package_manifest(), None);
    let (updates, slots, staged) = staged_fixture(&package);
    let prepared = prepare_release_slot(&staged, &updates, &slots, NOW + 1).unwrap();
    let slot_path = prepared_slot_path(&slots, &prepared);

    assert_eq!(prepared.manifest.version, "1.3.0");
    assert_eq!(
        std::fs::read(slot_path.join("stationd.exe")).unwrap(),
        b"station service proof bytes"
    );
    assert!(
        std::fs::metadata(slot_path.join("stationd.exe"))
            .unwrap()
            .permissions()
            .readonly()
    );
    verify_prepared_slot(&slots, &prepared).unwrap();
    assert_eq!(
        prepare_release_slot(&staged, &updates, &slots, NOW + 2)
            .unwrap()
            .slot_directory_name,
        prepared.slot_directory_name
    );

    remove_fixture(slots.parent().unwrap());
}

#[test]
fn rejects_extra_or_path_traversal_entries_before_extraction() {
    let package = package_bytes(&package_manifest(), Some(("payload/../outside.exe", b"no")));
    let (updates, slots, staged) = staged_fixture(&package);
    let error = prepare_release_slot(&staged, &updates, &slots, NOW + 1).unwrap_err();
    assert!(error.contains("unexpected number") || error.contains("unsafe"));
    assert_eq!(std::fs::read_dir(&slots).unwrap().count(), 0);
    assert!(!slots.parent().unwrap().join("outside.exe").exists());
    remove_fixture(slots.parent().unwrap());
}

#[test]
fn reads_legacy_three_payload_identity_but_refuses_to_prepare_it() {
    let updater = b"legacy station updater proof bytes";
    let mut legacy = package_manifest();
    legacy.schema_version = 1;
    legacy.files.push(UpdatePackageFile {
        path: "station-updater.exe".into(),
        size_bytes: updater.len() as u64,
        sha256: format!("{:X}", Sha256::digest(updater)),
    });
    legacy.validate().unwrap();

    let package = package_bytes(&legacy, Some(("payload/station-updater.exe", updater)));
    let (updates, slots, staged) = staged_fixture(&package);
    let error = prepare_release_slot(&staged, &updates, &slots, NOW + 1).unwrap_err();
    assert!(error.contains("unexpected number") || error.contains("legacy package schema"));
    remove_fixture(slots.parent().unwrap());
}

#[test]
fn rejects_internal_payload_digest_mismatch_and_removes_slot_quarantine() {
    let mut internal = package_manifest();
    internal
        .files
        .iter_mut()
        .find(|file| file.path == "channel-worker.exe")
        .unwrap()
        .sha256 = "A".repeat(64);
    let package = package_bytes(&internal, None);
    let (updates, slots, staged) = staged_fixture(&package);
    let error = prepare_release_slot(&staged, &updates, &slots, NOW + 1).unwrap_err();
    assert!(error.contains("payload bytes"));
    assert_eq!(std::fs::read_dir(&slots).unwrap().count(), 0);
    remove_fixture(slots.parent().unwrap());
}

#[test]
#[allow(clippy::permissions_set_readonly_false)]
fn refuses_to_reuse_a_tampered_existing_slot() {
    let package = package_bytes(&package_manifest(), None);
    let (updates, slots, staged) = staged_fixture(&package);
    let prepared = prepare_release_slot(&staged, &updates, &slots, NOW + 1).unwrap();
    let stationd = prepared_slot_path(&slots, &prepared).join("stationd.exe");
    let mut permissions = std::fs::metadata(&stationd).unwrap().permissions();
    permissions.set_readonly(false);
    std::fs::set_permissions(&stationd, permissions).unwrap();
    std::fs::write(&stationd, b"tampered").unwrap();

    assert!(prepare_release_slot(&staged, &updates, &slots, NOW + 2).is_err());
    assert_eq!(std::fs::read(&stationd).unwrap(), b"tampered");
    remove_fixture(slots.parent().unwrap());
}

#[allow(clippy::permissions_set_readonly_false)]
fn remove_fixture(root: &Path) {
    if let Ok(entries) = std::fs::read_dir(root.join("slots")) {
        for slot in entries.flatten() {
            if let Ok(files) = std::fs::read_dir(slot.path()) {
                for file in files.flatten() {
                    if let Ok(metadata) = file.metadata() {
                        let mut permissions = metadata.permissions();
                        permissions.set_readonly(false);
                        let _ = std::fs::set_permissions(file.path(), permissions);
                    }
                }
            }
        }
    }
    std::fs::remove_dir_all(root).unwrap();
}
