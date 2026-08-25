use sha2::{Digest, Sha256};
use station_maintenance::{
    INSTALLED_RUNTIME_MANIFEST_NAME, read_verified_candidate_identity,
    write_installed_runtime_manifest,
};
use station_update::{UpdatePackageFile, UpdatePackageManifest};
use std::path::PathBuf;

const VERSION: &str = "0.2.0";
const COMMIT: &str = "0123456789abcdef0123456789abcdef01234567";
const RUNTIME_SHA256: &str = "abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd";
const RUNTIME_PACKAGE_SHA256: &str = RUNTIME_SHA256;

struct Fixture {
    root: PathBuf,
    data: PathBuf,
    application: PathBuf,
    runtime: PathBuf,
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

fn fixture() -> Fixture {
    let root = std::env::temp_dir().join(format!(
        "townlight-candidate-identity-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let data = root.join("data");
    let application = root.join("application");
    let runtime = application.join("runtime").join("gstreamer");
    std::fs::create_dir_all(&data).unwrap();
    std::fs::create_dir_all(&application).unwrap();
    std::fs::create_dir_all(runtime.join("bin")).unwrap();
    std::fs::create_dir_all(runtime.join("lib").join("gstreamer-1.0")).unwrap();
    std::fs::create_dir_all(
        runtime
            .join("lib")
            .join("gst-validate-launcher")
            .join("python")
            .join("launcher")
            .join("apps"),
    )
    .unwrap();
    std::fs::write(runtime.join("bin").join("gstreamer.dll"), b"runtime core").unwrap();
    std::fs::write(
        runtime.join("lib").join("gstreamer-1.0").join("plugin.dll"),
        b"runtime plugin",
    )
    .unwrap();
    std::fs::write(
        runtime
            .join("lib")
            .join("gst-validate-launcher")
            .join("python")
            .join("launcher")
            .join("apps")
            .join("empty.py"),
        b"",
    )
    .unwrap();

    let files = [
        ("stationd.exe", b"stationd-candidate".as_slice()),
        ("channel-worker.exe", b"worker-candidate".as_slice()),
    ];
    let manifest_files = files
        .iter()
        .map(|(name, bytes)| UpdatePackageFile {
            path: (*name).into(),
            size_bytes: bytes.len() as u64,
            sha256: format!("{:x}", Sha256::digest(bytes)),
        })
        .collect::<Vec<_>>();
    for (name, bytes) in files {
        std::fs::write(application.join(name), bytes).unwrap();
    }
    let manifest = UpdatePackageManifest {
        schema_version: 2,
        product: "TownLight Station".into(),
        version: VERSION.into(),
        source_commit: COMMIT.into(),
        files: manifest_files,
    };
    std::fs::write(
        data.join("active-slot.json"),
        serde_json::json!({
            "schema_version": 1,
            "generation": 1,
            "activated_at_unix_ms": 1,
            "transaction_id": "bootstrap",
            "active": {
                "relative_directory": ".",
                "package_manifest": manifest,
            },
            "previous": null,
        })
        .to_string(),
    )
    .unwrap();
    let runtime_manifest_path = data.join(INSTALLED_RUNTIME_MANIFEST_NAME);
    let runtime_manifest_sha256 = write_installed_runtime_manifest(
        &runtime,
        &runtime_manifest_path,
        VERSION,
        COMMIT,
        RUNTIME_PACKAGE_SHA256,
    )
    .unwrap();
    std::fs::write(
        data.join("install-receipt.json"),
        serde_json::json!({
            "product": "TownLight Station",
            "version": VERSION,
            "source_commit": COMMIT,
            "media_runtime_sha256": RUNTIME_SHA256,
            "runtime_manifest_sha256": runtime_manifest_sha256,
            "installed_at_unix_ms": 1,
            "health_uri": "http://127.0.0.1:4070/health",
        })
        .to_string(),
    )
    .unwrap();
    Fixture {
        root,
        data,
        application,
        runtime,
    }
}

#[test]
fn returns_the_verified_active_candidate_and_approved_runtime_identity() {
    let fixture = fixture();
    let identity = read_verified_candidate_identity(&fixture.data, &fixture.application).unwrap();
    assert_eq!(identity.product, "TownLight Station");
    assert_eq!(identity.version, VERSION);
    assert_eq!(identity.source_commit, COMMIT);
    assert_eq!(identity.media_runtime_sha256, RUNTIME_SHA256);
}

#[test]
fn accepts_a_legacy_uppercase_runtime_identity_and_canonicalizes_it() {
    let fixture = fixture();
    let path = fixture.data.join("install-receipt.json");
    let mut receipt: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    receipt["media_runtime_sha256"] =
        serde_json::Value::String(RUNTIME_SHA256.to_ascii_uppercase());
    std::fs::write(path, receipt.to_string()).unwrap();
    let identity = read_verified_candidate_identity(&fixture.data, &fixture.application).unwrap();
    assert_eq!(identity.media_runtime_sha256, RUNTIME_SHA256);
}

#[test]
fn rejects_a_runtime_identity_that_is_not_sha256() {
    let fixture = fixture();
    let path = fixture.data.join("install-receipt.json");
    let mut receipt: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    receipt["media_runtime_sha256"] = serde_json::Value::String("not-a-sha256".into());
    std::fs::write(path, receipt.to_string()).unwrap();
    let error = read_verified_candidate_identity(&fixture.data, &fixture.application).unwrap_err();
    assert!(error.contains("runtime"), "unexpected error: {error}");
}

#[test]
fn rejects_a_tampered_active_application_even_when_receipts_agree() {
    let fixture = fixture();
    std::fs::write(fixture.application.join("stationd.exe"), b"tampered").unwrap();
    let error = read_verified_candidate_identity(&fixture.data, &fixture.application).unwrap_err();
    assert!(
        error.contains("active application"),
        "unexpected error: {error}"
    );
}

#[test]
fn rejects_a_tampered_installed_runtime_file() {
    let fixture = fixture();
    std::fs::write(
        fixture
            .runtime
            .join("lib")
            .join("gstreamer-1.0")
            .join("plugin.dll"),
        b"tampered runtime",
    )
    .unwrap();
    let error = read_verified_candidate_identity(&fixture.data, &fixture.application).unwrap_err();
    assert!(error.contains("runtime"), "unexpected error: {error}");
}

#[test]
fn rejects_a_missing_installed_runtime_file() {
    let fixture = fixture();
    std::fs::remove_file(fixture.runtime.join("bin").join("gstreamer.dll")).unwrap();
    let error = read_verified_candidate_identity(&fixture.data, &fixture.application).unwrap_err();
    assert!(error.contains("runtime"), "unexpected error: {error}");
}

#[test]
fn rejects_an_extra_installed_runtime_file() {
    let fixture = fixture();
    std::fs::write(fixture.runtime.join("unexpected.dll"), b"extra runtime").unwrap();
    let error = read_verified_candidate_identity(&fixture.data, &fixture.application).unwrap_err();
    assert!(error.contains("runtime"), "unexpected error: {error}");
}

#[test]
fn rejects_a_malformed_runtime_manifest_path() {
    let fixture = fixture();
    let path = fixture.data.join(INSTALLED_RUNTIME_MANIFEST_NAME);
    let mut manifest: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    manifest["files"][0]["path"] = serde_json::Value::String("../outside.dll".into());
    let bytes = serde_json::to_vec(&manifest).unwrap();
    std::fs::write(&path, &bytes).unwrap();
    update_runtime_manifest_receipt(&fixture, &bytes);
    let error = read_verified_candidate_identity(&fixture.data, &fixture.application).unwrap_err();
    assert!(
        error.contains("manifest") || error.contains("path"),
        "unexpected error: {error}"
    );
}

#[test]
fn rejects_case_insensitive_duplicate_runtime_manifest_paths() {
    let fixture = fixture();
    let path = fixture.data.join(INSTALLED_RUNTIME_MANIFEST_NAME);
    let mut manifest: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    let duplicate = manifest["files"][0].clone();
    manifest["files"].as_array_mut().unwrap().push(duplicate);
    let bytes = serde_json::to_vec(&manifest).unwrap();
    std::fs::write(&path, &bytes).unwrap();
    update_runtime_manifest_receipt(&fixture, &bytes);
    let error = read_verified_candidate_identity(&fixture.data, &fixture.application).unwrap_err();
    assert!(error.contains("manifest"), "unexpected error: {error}");
}

#[test]
fn rejects_an_empty_runtime_manifest() {
    let fixture = fixture();
    let path = fixture.data.join(INSTALLED_RUNTIME_MANIFEST_NAME);
    let mut manifest: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    manifest["files"] = serde_json::json!([]);
    let bytes = serde_json::to_vec(&manifest).unwrap();
    std::fs::write(&path, &bytes).unwrap();
    update_runtime_manifest_receipt(&fixture, &bytes);
    let error = read_verified_candidate_identity(&fixture.data, &fixture.application).unwrap_err();
    assert!(error.contains("manifest"), "unexpected error: {error}");
}

#[test]
fn rejects_a_runtime_manifest_receipt_digest_mismatch() {
    let fixture = fixture();
    let path = fixture.data.join("install-receipt.json");
    let mut receipt: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    receipt["runtime_manifest_sha256"] = serde_json::Value::String("a".repeat(64));
    std::fs::write(path, serde_json::to_vec(&receipt).unwrap()).unwrap();
    let error = read_verified_candidate_identity(&fixture.data, &fixture.application).unwrap_err();
    assert!(error.contains("manifest"), "unexpected error: {error}");
}

#[test]
fn rejects_a_receipt_pair_with_different_product_version_or_commit() {
    for (field, value) in [
        ("product", "Other Product"),
        ("version", "0.3.0"),
        ("source_commit", "fedcba9876543210fedcba9876543210fedcba98"),
    ] {
        let fixture = fixture();
        let path = fixture.data.join("install-receipt.json");
        let mut receipt: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        receipt[field] = serde_json::Value::String(value.into());
        std::fs::write(path, receipt.to_string()).unwrap();
        let error =
            read_verified_candidate_identity(&fixture.data, &fixture.application).unwrap_err();
        assert!(
            error.contains("agree") || error.contains("identity"),
            "{field}: {error}"
        );
    }
}

fn update_runtime_manifest_receipt(fixture: &Fixture, manifest_bytes: &[u8]) {
    let path = fixture.data.join("install-receipt.json");
    let mut receipt: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    receipt["runtime_manifest_sha256"] =
        serde_json::Value::String(format!("{:x}", Sha256::digest(manifest_bytes)));
    std::fs::write(path, serde_json::to_vec(&receipt).unwrap()).unwrap();
}
