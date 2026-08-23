use sha2::{Digest, Sha256};
use station_maintenance::read_verified_candidate_identity;
use station_update::{UpdatePackageFile, UpdatePackageManifest};
use std::path::PathBuf;

const VERSION: &str = "0.2.0";
const COMMIT: &str = "0123456789abcdef0123456789abcdef01234567";
const RUNTIME_SHA256: &str = "abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd";

struct Fixture {
    root: PathBuf,
    data: PathBuf,
    application: PathBuf,
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
    std::fs::create_dir_all(&data).unwrap();
    std::fs::create_dir_all(&application).unwrap();

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
    std::fs::write(
        data.join("install-receipt.json"),
        serde_json::json!({
            "product": "TownLight Station",
            "version": VERSION,
            "source_commit": COMMIT,
            "media_runtime_sha256": RUNTIME_SHA256,
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
